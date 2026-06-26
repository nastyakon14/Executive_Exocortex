# Линкер — встраивает новые Zettel-карточки в существующий граф
import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from enum import Enum
from datetime import datetime, timezone
from dataclasses import dataclass

import torch
from sentence_transformers import SentenceTransformer  # HuggingFace embeddings
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from storage.chromadb.factory import get_chroma_client # ДОБАВЛЕНО: импорт фабрики
from zettelkasten.atomizer import (
    NoteAtomizer,
    ZettelCard,
    ZettelIdGenerator,
    ThoughtType,
)

load_dotenv()


class LocalEmbeddingModel:
    """
    intfloat/multilingual-e5-base обучена с префиксами:
      - "query: текст"   — для поискового запроса (новая карточка)
      - "passage: текст" — для индексируемых документов (карточки в базе)
    """

    # Название модели — можно переопределить при создании объекта
    DEFAULT_MODEL_NAME = settings.embedding_model_name
    
    # ИСПРАВЛЕНО: type hint исправлен на str
    def __init__(self, model_name: str = settings.embedding_model_name):
        self.model_name = model_name
        if torch.cuda.is_available():
            self.device = "cuda"
        elif torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"

        start = time.time()

        self.model = SentenceTransformer(
            model_name_or_path=self.model_name,
            device=self.device,
        )

        elapsed = time.time() - start
        print(f"[LocalEmbeddingModel] Модель {self.model_name} загружена за {elapsed:.1f}с. "
              f"Размерность вектора: {self.model.get_sentence_embedding_dimension()}")

    def embed_passage(self, text: str) -> List[float]:
        """
        Генерирует эмбеддинг для документа (карточки в базе).
        Добавляет префикс "passage: "
        """    
        # Префикс "passage:" - это требование модели e5 для индексируемых документов
        prefixed = f"passage: {text}"
        
        # encode() возвращает numpy array, .tolist() конвертирует в list[float]
        # normalize_embeddings=True → нормализуем вектор (длина = 1)
        # Это ускоряет косинусное сходство и делает его эквивалентным скалярному произведению
        vector = self.model.encode(
            prefixed,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vector.tolist()

    def embed_query(self, text: str) -> List[float]:
        """
        Генерирует эмбеддинг для поискового запроса (новая карточка).
        Добавляет префикс "query: "
        """
        # Префикс "query:" - это требование модели e5 для поисковых запросов
        prefixed = f"query: {text}"
        
        vector = self.model.encode(
            prefixed,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vector.tolist()

    def embed_passages_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Batch-генерация эмбеддингов для нескольких документов сразу.
        Значительно быстрее, чем вызывать embed_passage() в цикле,
        потому что модель обрабатывает все тексты параллельно на GPU/CPU
        """
        prefixed = [f"passage: {t}" for t in texts]
        
        # batch_size=32 - обрабатываем по 32 текста за раз
        vectors = self.model.encode(
            prefixed,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=len(texts) > 10,  # Прогресс-бар если текстов много
        )
        return [v.tolist() for v in vectors]

    @property
    def embedding_dimension(self) -> int:
        """Размерность вектора эмбеддинга (768 для e5-base)"""
        return self.model.get_sentence_embedding_dimension()

# llm linker schema
class LinkAction(str, Enum):
    """
    Три возможных решения линкера для каждой новой карточки:
    NEW_ROOT  — карточка начинает совершенно новую тему
    CHILD_OF  — карточка развивает/уточняет существующую карточку из базы
    UPDATE_OF — карточка изменяет/перезаписывает факт в существующей карточке
    """
    NEW_ROOT  = "new_root"
    CHILD_OF  = "child_of"
    UPDATE_OF = "update_of"


class LinkDecision(BaseModel):
    """Структурированный ответ LLM о связи новой мысли со старыми."""
    
    action: LinkAction = Field(
        description=(
            "Тип связи: "
            "'new_root' — новая независимая тема; "
            "'child_of' — развивает/уточняет существующую карточку; "
            "'update_of' — изменяет/перезаписывает факт в существующей карточке."
        )
    )
    target_zettel_id: Optional[str] = Field(
        default=None,
        description=(
            "UUID карточки из базы, к которой привязываемся. "
            "Обязателен для 'child_of' и 'update_of'. "
            "None для 'new_root'."
        )
    )
    reasoning: str = Field(
        description="Краткое объяснение (1-2 предложения): почему выбрано именно это действие."
    )


# helper dataclass

@dataclass
class LinkResult:
    """Результат встраивания одной карточки в граф"""
    card: ZettelCard                        # Итоговая карточка (с обновлёнными ID)
    action: LinkAction                      # Что сделал линкер (new_root, child_of, update_of)
    reasoning: str                          # Почему именно так
    candidates_found: int = 0              # Сколько кандидатов нашёл векторный поиск
    target_zettel_id: Optional[str] = None # ID карточки-цели (для child_of или update_of)


# vector database (chromadb + huggingface embeddings)
class ZettelVectorDB:
    """
    Хранилище Zettel-карточек с векторным поиском
    """

    def __init__(
        self,
        embedding_model: LocalEmbeddingModel,
    ):
        # ИСПРАВЛЕНО: Вызываем фабрику для получения клиента (поддерживает Docker HTTP)
        self.chroma_client = get_chroma_client()

        self.collection = self.chroma_client.get_or_create_collection(
            name=settings.chroma_collection_name,
            # cosine: 1.0 = идентичные векторы, 0.0 = несвязанные
            metadata={"hnsw:space": "cosine"}
        )

        self.embedding_model = embedding_model

        # local cache of cards
        # ChromaDB хранит id + вектор + текст + метаданные
        self.cards_store: Dict[str, ZettelCard] = {}

        # максимальный числовой luhman-ID среди корневых карточек
        self.current_max_root_id: int = 0
        
        # ДОБАВЛЕНО: Важно для Telegram-бота! Восстанавливает память после перезапуска скрипта из Docker-БД
        self._restore_cache_from_db()

        print(f"[ZettelVectorDB] Инициализирована. "
              f"Карточек в коллекции: {self.collection.count()}")

    def _restore_cache_from_db(self) -> None:
        """
        При старте подгружает все существующие карточки из ChromaDB в локальный кэш (cards_store).
        Необходимо, чтобы после перезапуска бота граф знаний не ломался.
        """
        try:
            all_data = self.collection.get(include=["documents", "metadatas"])
        except Exception:
            return

        if not all_data or not all_data.get("ids"):
            return

        for doc_id, document, meta in zip(all_data["ids"], all_data["documents"], all_data["metadatas"]):
            card = ZettelCard(
                zettel_id=doc_id,
                luhmann_id=meta.get("luhmann_id", "0"),
                parent_luhmann_id=meta.get("parent_luhmann_id") or None,
                content=document,
                thought_type=meta.get("thought_type", "other"),
                tags=meta.get("tags", "").split(",") if meta.get("tags") else [],
                is_root_topic=meta.get("is_root_topic", "False") == "True",
            )
            self.cards_store[doc_id] = card

            if card.luhmann_id.isdigit():
                self.current_max_root_id = max(self.current_max_root_id, int(card.luhmann_id))

    # add one card
    def add_card(self, card: ZettelCard) -> None:
        """
        генерирует эмбеддинг и сохраняет карточку в chromadb
        """
        # embed_passage добавляет "passage: " перед текстом
        vector: List[float] = self.embedding_model.embed_passage(card.content)
        card.embedding = vector

        # сохраняем в chromadb
        self.collection.add(
            ids=[card.zettel_id],
            embeddings=[vector],
            documents=[card.content],
            metadatas=[{
                "luhmann_id":        card.luhmann_id,
                "parent_luhmann_id": card.parent_luhmann_id or "",
                "thought_type":      str(card.thought_type),
                "tags":              ",".join(card.tags),
                "is_root_topic":     str(card.is_root_topic),
            }]
        )

        # Сохраняем в кэш
        self.cards_store[card.zettel_id] = card

        # Обновляем счётчик корней
        if card.luhmann_id.isdigit():
            self.current_max_root_id = max(
                self.current_max_root_id,
                int(card.luhmann_id)
            )

        print(f"  [DB] Добавлена [{card.luhmann_id}]: {card.content[:65]}...")

    def add_cards_batch(self, cards: List[ZettelCard]) -> None:
        """
        Batch-добавление нескольких карточек за один вызов модели.
        
        быстрее, чем add_card() в цикле, особенно на GPU:
        - add_card() x10 карточек: 10 отдельных вызовов модели
        - add_cards_batch() x10 карточек: 1 вызов модели (batch)
        
        Разница в скорости: в 5-10 раз на CPU, в 20-50 раз на GPU.
        """
        if not cards:
            return

        # Генерируем все векторы за один batch-вызов
        texts = [card.content for card in cards]
        vectors = self.embedding_model.embed_passages_batch(texts)

        ids, embeddings, documents, metadatas = [], [], [], []

        for card, vector in zip(cards, vectors):
            card.embedding = vector
            ids.append(card.zettel_id)
            embeddings.append(vector)
            documents.append(card.content)
            metadatas.append({
                "luhmann_id":        card.luhmann_id,
                "parent_luhmann_id": card.parent_luhmann_id or "",
                "thought_type":      str(card.thought_type),
                "tags":              ",".join(card.tags),
                "is_root_topic":     str(card.is_root_topic),
            })

            self.cards_store[card.zettel_id] = card

            if card.luhmann_id.isdigit():
                self.current_max_root_id = max(
                    self.current_max_root_id,
                    int(card.luhmann_id)
                )

        # Одна bulk-операция в ChromaDB
        self.collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        print(f"  [DB] Batch-добавлено {len(cards)} карточек")

    # vector search
    def search_candidates(self, query_text: str, limit: int = 5, similarity_threshold: float = 0.3) -> List[Tuple[ZettelCard, float]]:
        """
        ищет карточки, близкие по смыслу к query_text
        """
        total_cards = len(self.cards_store)
        if total_cards == 0:
            return []

        n_results = min(limit, total_cards)

        # embed_query добавляет "query: " перед текстом
        query_vector = self.embedding_model.embed_query(query_text)

        results = self.collection.query(
            query_embeddings=[query_vector],
            n_results=n_results,
            include=["distances", "documents", "metadatas"]
        )

        # ChromaDB с cosine space возвращает расстояние [0..2]
        # 0 = идентичные векторы, 2 = противоположные
        # Переводим в similarity [0..1]: similarity = 1 - distance/2
        found_ids       = results["ids"][0]
        found_distances = results["distances"][0]

        candidates = []
        for doc_id, distance in zip(found_ids, found_distances):
            similarity = 1.0 - (distance / 2.0)

            if similarity < similarity_threshold:
                continue

            card = self.cards_store.get(doc_id)
            if card:
                candidates.append((card, round(similarity, 4)))

        return candidates

    # обновление карточки
    def update_card_content(self, zettel_id: str, new_content: str) -> Optional[ZettelCard]:
        """
        Обновляет текст и эмбеддинг существующей карточки (сценарий UPDATE_OF).
        пересчитываем вектор
        """
        if zettel_id not in self.cards_store:
            print(f"  [DB] ПРЕДУПРЕЖДЕНИЕ: карточка {zettel_id} не найдена для обновления")
            return None

        card = self.cards_store[zettel_id]
        old_content = card.content

        # Формируем текст с меткой обновления
        timestamp = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        updated_content = f"[ОБНОВЛЕНО {timestamp}] {new_content}"
        card.content = updated_content

        # Пересчитываем вектор локально (это "passage" — документ в базе)
        new_vector = self.embedding_model.embed_passage(updated_content)
        card.embedding = new_vector

        # Обновляем ChromaDB
        self.collection.update(
            ids=[zettel_id],
            embeddings=[new_vector],
            documents=[updated_content],
        )

        print(f"  [DB] ♻️  Карточка [{card.luhmann_id}] перезаписана:")
        print(f"       БЫЛО:  {old_content[:65]}...")
        print(f"       СТАЛО: {updated_content[:65]}...")

        return card

    def get_siblings(self, parent_luhmann_id: Optional[str]) -> List[str]:
        """Возвращает Луман-ID всех детей указанного родителя."""
        if not parent_luhmann_id:
            return [
                c.luhmann_id
                for c in self.cards_store.values()
                if c.luhmann_id.isdigit()
            ]
        return [
            c.luhmann_id
            for c in self.cards_store.values()
            if c.parent_luhmann_id == parent_luhmann_id
        ]

    def get_card_by_id(self, zettel_id: str) -> Optional[ZettelCard]:
        """Получает карточку из кэша по UUID."""
        return self.cards_store.get(zettel_id)

    def total_count(self) -> int:
        """Общее число карточек в базе."""
        return len(self.cards_store)

    def print_graph(self) -> None:
        """Выводит текущее состояние графа в виде дерева."""
        print("\n" + "═" * 65)
        print("🌳 ТЕКУЩИЙ ГРАФ ЗНАНИЙ")
        print("═" * 65)

        if not self.cards_store:
            print("   (пусто)")
            print("═" * 65 + "\n")
            return

        sorted_cards = sorted(
            self.cards_store.values(),
            key=lambda c: (len(c.luhmann_id), c.luhmann_id)
        )

        for card in sorted_cards:
            parent_info = f" ← [{card.parent_luhmann_id}]" if card.parent_luhmann_id else ""
            prefix = "📌" if card.is_root_topic else "  └─"
            print(f"{prefix} [{card.luhmann_id}]{parent_info}: {card.content[:68]}")

        print("═" * 65 + "\n")


# linker
class GraphLinker:
    """
    Встраивает новые Zettel-карточки в существующий граф знаний.
    
    Pipeline для каждой карточки:
    1. Если карточка уже привязана к родителю внутри текущего сообщения
       (is_root_topic=False) — пересчитываем luhmann-ID, сохраняем.
    2. Если карточка корневая (is_root_topic=True):
       a. Векторный поиск → топ-N похожих карточек из базы 
       b. LLM анализирует кандидатов → new_root / child_of / update_of
       c. Применяем решение, обновляем граф
    """

    LINKER_SYSTEM_PROMPT = settings.linker_system_prompt

    def __init__(
        self,
        model_name: str = settings.linker_model_name,
        system_prompt: str = settings.linker_system_prompt,
        similarity_threshold: float = 0.3,
        max_candidates: int = 5,
    ):
        """
        Args:
            model_name:           LLM для принятия решений (только для Linker Brain).
                                  Эмбеддинги — всегда локально через HuggingFace.
            similarity_threshold: Порог похожести [0..1]. Рекомендуется 0.3-0.4.
            max_candidates:       Максимум кандидатов для LLM (не более 5-7).
        """
        self.similarity_threshold = similarity_threshold
        self.max_candidates = max_candidates
        self.system_prompt = system_prompt
        self.model_name = model_name
        # LLM нужна только для принятия решений (child_of / update_of / new_root)
        # Эмбеддинги полностью локальные — LLM для них не используется
        base_llm = ChatOpenAI(
            model=self.model_name,
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            temperature=0.0,  # Строгая логика, никакого творчества
        )
        self.structured_llm = base_llm.with_structured_output(LinkDecision)


    def link_and_insert(
        self,
        new_cards: List[ZettelCard],
        db: ZettelVectorDB,
    ) -> List[LinkResult]:
        """
        Обрабатывает список новых карточек (вывод Атомайзера) и встраивает в граф.
        
        Args:
            new_cards: Карточки от Атомайзера (с временными Луман-ID).
            db:        Векторная база данных.
        
        Returns:
            Список LinkResult — что произошло с каждой карточкой.
        """
        results: List[LinkResult] = []

        # Маппинг: временный Луман-ID → реальный Луман-ID в базе
        # Нужен для пересчёта ID у дочерних карточек.
        luhmann_remap: Dict[str, str] = {}

        for card in new_cards:
            print(f"\n{'─' * 55}")
            print(f"📋 [{card.luhmann_id}] {card.content[:60]}...")
            print(f"   root={card.is_root_topic}, parent_luhmann={card.parent_luhmann_id}")

            # ── Ветка 1: Дочерняя карточка (привязана внутри сообщения) ────────
            # Атомайзер уже решил, что это дочерняя мысль.
            # Нам нужно только пересчитать её Луман-ID относительно
            # реального места родителя в базе.
            if not card.is_root_topic and card.parent_luhmann_id in luhmann_remap:
                result = self._handle_inner_child(card, db, luhmann_remap)

            # ── Ветка 2: Корневая карточка — проверяем по базе ──────────────────
            else:
                result = self._handle_root_card(card, db, luhmann_remap)

            results.append(result)

        return results

    # обработка дочерней карточки

    def _handle_inner_child(
        self,
        card: ZettelCard,
        db: ZettelVectorDB,
        luhmann_remap: Dict[str, str],
    ) -> LinkResult:
        """
        Карточка уже привязана к родителю ВНУТРИ текущего сообщения.
        Пересчитываем Луман-ID: временный → реальный.
        """
        # реальный luhmann-ID родителя из маппинга
        real_parent_luhmann = luhmann_remap[card.parent_luhmann_id]

        existing_siblings = db.get_siblings(real_parent_luhmann)
        new_luhmann = ZettelIdGenerator.get_next_id(
            real_parent_luhmann,
            existing_siblings,
            db.current_max_root_id
        )

        # запоминаем маппинг (у этой карточки тоже могут быть дети)
        luhmann_remap[card.luhmann_id] = new_luhmann

        # Обновляем карточку
        card.parent_luhmann_id = real_parent_luhmann
        card.luhmann_id = new_luhmann

        db.add_card(card)
        print(f"   ✅ Дочерняя (внутри сообщения) → [{new_luhmann}]")

        return LinkResult(
            card=card,
            action=LinkAction.CHILD_OF,
            reasoning="Дочерняя карточка внутри текущего сообщения (решение Атомайзера).",
            candidates_found=0,
        )

    # обработка корневой карточки

    def _handle_root_card(
        self,
        card: ZettelCard,
        db: ZettelVectorDB,
        luhmann_remap: Dict[str, str],
    ) -> LinkResult:
        """
        Карточка считается новым корнем. Проверяем через векторный поиск + LLM.
        """
        # Векторный поиск
        candidates = db.search_candidates(
            query_text=card.content,
            limit=self.max_candidates,
            similarity_threshold=self.similarity_threshold,
        )

        print(f"   🔍 Векторный поиск (локально): {len(candidates)} кандидатов")
        for cand_card, score in candidates:
            print(f"      sim={score:.3f} [{cand_card.luhmann_id}]: {cand_card.content[:50]}...")

        # Нет кандидатов → сразу NEW_ROOT, без LLM-вызова
        if not candidates:
            return self._apply_new_root(
                card, db, luhmann_remap,
                reasoning="Нет похожих карточек в базе.",
            )

        # Есть кандидаты → спрашиваем LLM
        decision = self._ask_llm(card, candidates)
        print(f"   🤖 LLM: {decision.action.upper()} | {decision.reasoning}")

        # Применяем решение
        if decision.action == LinkAction.NEW_ROOT:
            return self._apply_new_root(
                card, db, luhmann_remap,
                reasoning=decision.reasoning,
                candidates_found=len(candidates),
            )
        elif decision.action == LinkAction.CHILD_OF:
            return self._apply_child_of(
                card, db, luhmann_remap, decision,
                candidates_found=len(candidates),
            )
        elif decision.action == LinkAction.UPDATE_OF:
            return self._apply_update_of(
                card, db, decision,
                candidates_found=len(candidates),
            )

        # Fallback на случай непредвиденного ответа
        return self._apply_new_root(
            card, db, luhmann_remap,
            reasoning=f"Непредвиденный ответ LLM ({decision.action}). Fallback → new_root.",
        )

    # llm-запрос

    def _ask_llm(
        self,
        card: ZettelCard,
        candidates: List[Tuple[ZettelCard, float]],
    ) -> LinkDecision:
        """Формирует промпт и запрашивает решение у LLM."""
        candidates_text = "\n".join([
            f"  • UUID: {c.zettel_id}\n"
            f"    Луман-ID: [{c.luhmann_id}]\n"
            f"    Тип: {c.thought_type}\n"
            f"    Сходство: {score:.3f}\n"
            f"    Текст: \"{c.content}\"\n"
            for c, score in candidates
        ])

        user_prompt = (
            f"НОВАЯ МЫСЛЬ:\n"
            f"  Тип: {card.thought_type}\n"
            f"  Теги: {', '.join(card.tags)}\n"
            f"  Текст: \"{card.content}\"\n\n"
            f"КАНДИДАТЫ ИЗ БАЗЫ:\n{candidates_text}\n"
            f"Реши, как встроить новую мысль в граф."
        )

        return self.structured_llm.invoke([
            SystemMessage(content=self.LINKER_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])

    # применение решений

    def _apply_new_root(
        self,
        card: ZettelCard,
        db: ZettelVectorDB,
        luhmann_remap: Dict[str, str],
        reasoning: str,
        candidates_found: int = 0,
    ) -> LinkResult:
        """Добавляет карточку как новый корень графа."""
        new_luhmann = ZettelIdGenerator.get_next_id(
            parent_luhmann_id=None,
            existing_sibling_ids=db.get_siblings(None),
            current_max_root=db.current_max_root_id,
        )

        luhmann_remap[card.luhmann_id] = new_luhmann
        card.luhmann_id        = new_luhmann
        card.parent_id         = None
        card.parent_luhmann_id = None
        card.is_root_topic     = True

        db.add_card(card)
        print(f"   ✅ NEW_ROOT → [{new_luhmann}]")

        return LinkResult(
            card=card,
            action=LinkAction.NEW_ROOT,
            reasoning=reasoning,
            candidates_found=candidates_found,
        )

    def _apply_child_of(
        self,
        card: ZettelCard,
        db: ZettelVectorDB,
        luhmann_remap: Dict[str, str],
        decision: LinkDecision,
        candidates_found: int,
    ) -> LinkResult:
        """Добавляет карточку как дочернюю к существующей карточке в базе."""
        parent_card = db.get_card_by_id(decision.target_zettel_id)

        if not parent_card:
            print(f"   ⚠️  Родитель {decision.target_zettel_id} не найден! Fallback → new_root")
            return self._apply_new_root(
                card, db, luhmann_remap,
                reasoning=f"Родитель не найден (UUID={decision.target_zettel_id}). Fallback.",
                candidates_found=candidates_found,
            )

        card.is_root_topic     = False
        card.parent_id         = parent_card.zettel_id
        card.parent_luhmann_id = parent_card.luhmann_id

        existing_siblings = db.get_siblings(parent_card.luhmann_id)
        new_luhmann = ZettelIdGenerator.get_next_id(
            parent_card.luhmann_id,
            existing_siblings,
            db.current_max_root_id,
        )

        luhmann_remap[card.luhmann_id] = new_luhmann
        card.luhmann_id = new_luhmann

        db.add_card(card)
        print(f"   ✅ CHILD_OF [{parent_card.luhmann_id}] → [{new_luhmann}]")

        return LinkResult(
            card=card,
            action=LinkAction.CHILD_OF,
            reasoning=decision.reasoning,
            candidates_found=candidates_found,
            target_zettel_id=decision.target_zettel_id,
        )

    def _apply_update_of(
        self,
        card: ZettelCard,
        db: ZettelVectorDB,
        decision: LinkDecision,
        candidates_found: int,
    ) -> LinkResult:
        """Перезаписывает существующую карточку. Новая карточка не добавляется."""
        updated_card = db.update_card_content(decision.target_zettel_id, card.content)

        if not updated_card:
            return LinkResult(
                card=card,
                action=LinkAction.UPDATE_OF,
                reasoning=f"Ошибка: карточка {decision.target_zettel_id} не найдена.",
                candidates_found=candidates_found,
                target_zettel_id=decision.target_zettel_id,
            )

        print(f"   ✅ UPDATE_OF [{updated_card.luhmann_id}]")

        return LinkResult(
            card=updated_card,
            action=LinkAction.UPDATE_OF,
            reasoning=decision.reasoning,
            candidates_found=candidates_found,
            target_zettel_id=decision.target_zettel_id,
        )