# линкер — встраивает новые zettel-карточки в граф знаний (neo4j)
# изоляция по user_id: у каждого пользователя свой граф

import os
import sys
import time
import re
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from enum import Enum
from dataclasses import dataclass

import torch
from sentence_transformers import SentenceTransformer
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from storage.neo4j.client import get_neo4j_client
from storage.neo4j.schema import init_schema
from storage.neo4j.repository import ZettelRepository, ZettelNode, GraphContext
from zettelkasten.atomizer import ZettelCard

load_dotenv()


# локальная модель эмбеддингов (multilingual-e5)

class LocalEmbeddingModel:
    """
    Локальная модель эмбеддингов (intfloat/multilingual-e5-base).
    
    E5 модели обучены с префиксами:
    - "query: ..." для поисковых запросов
    - "passage: ..." для индексируемых документов
    """
    
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
        
        print(f"[EmbeddingModel] {self.model_name} загружена за {elapsed:.1f}с "
              f"(dim={self.embedding_dimension}, device={self.device})")
    
    def embed_passage(self, text: str) -> List[float]:
        """Эмбеддинг для документа (карточки в базе)."""
        prefixed = f"passage: {text}"
        vector = self.model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return vector.tolist()
    
    def embed_query(self, text: str) -> List[float]:
        """Эмбеддинг для поискового запроса."""
        prefixed = f"query: {text}"
        vector = self.model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return vector.tolist()
    
    def embed_passages_batch(self, texts: List[str]) -> List[List[float]]:
        """Batch-генерация эмбеддингов."""
        prefixed = [f"passage: {t}" for t in texts]
        vectors = self.model.encode(prefixed, normalize_embeddings=True, batch_size=32, show_progress_bar=len(texts) > 10)
        return [v.tolist() for v in vectors]
    
    @property
    def embedding_dimension(self) -> int:
        return self.model.get_sentence_embedding_dimension()


# pydantic-схемы решений линкера

class LinkAction(str, Enum):
    """Три возможных решения линкера."""
    NEW_ROOT = "new_root"    # Новая независимая тема
    CHILD_OF = "child_of"    # Развивает существующую карточку
    UPDATE_OF = "update_of"  # Перезаписывает факт в существующей карточке


class LinkDecision(BaseModel):
    """Структурированный ответ LLM."""
    action: LinkAction = Field(
        description=(
            "Тип связи: "
            "'new_root' — новая независимая тема; "
            "'child_of' — развивает существующую карточку; "
            "'update_of' — перезаписывает факт в существующей карточке."
        )
    )
    target_zettel_id: Optional[str] = Field(
        default=None,
        description="UUID карточки-цели. Обязателен для 'child_of' и 'update_of'. None для 'new_root'."
    )
    reasoning: str = Field(
        description="Краткое объяснение (1-2 предложения): почему выбрано именно это действие."
    )


@dataclass
class LinkResult:
    """Результат встраивания одной карточки."""
    card: ZettelNode
    action: LinkAction
    reasoning: str
    candidates_found: int = 0
    target_zettel_id: Optional[str] = None


# генератор luhmann id по правилам метода зеттелькастен

class ZettelIdGenerator:
    """Генератор идентификаторов по методу Лумана."""
    
    @staticmethod
    def get_next_id(
        parent_luhmann_id: Optional[str],
        existing_sibling_ids: List[str],
        current_max_root: int = 0
    ) -> str:
        """
        Генерирует следующий Luhmann ID.
        
        Правила:
        - Корни: 1, 2, 3, ...
        - Дочерние от числа: 1.1, 1.2, ...
        - Дочерние от точки+числа: 1.1a, 1.1b, ...
        - Дочерние от буквы: 1.1a1, 1.1a2, ...
        """
        if not parent_luhmann_id:
            if not existing_sibling_ids:
                return str(current_max_root + 1)
            roots = [int(i) for i in existing_sibling_ids if i.isdigit()]
            return str(max(roots) + 1) if roots else str(current_max_root + 1)
        
        if not existing_sibling_ids:
            if parent_luhmann_id.isdigit():
                return f"{parent_luhmann_id}.1"
            elif parent_luhmann_id[-1].isdigit():
                return f"{parent_luhmann_id}a"
            elif parent_luhmann_id[-1].isalpha():
                return f"{parent_luhmann_id}1"
        
        if parent_luhmann_id.isdigit():
            nums = [
                int(re.search(rf"^{re.escape(parent_luhmann_id)}\.(\d+)$", cid).group(1))
                for cid in existing_sibling_ids
                if re.match(rf"^{re.escape(parent_luhmann_id)}\.(\d+)$", cid)
            ]
            return f"{parent_luhmann_id}.{max(nums) + 1}" if nums else f"{parent_luhmann_id}.1"
        
        elif parent_luhmann_id[-1].isdigit():
            chars = [
                re.search(rf"^{re.escape(parent_luhmann_id)}([a-z])$", cid).group(1)
                for cid in existing_sibling_ids
                if re.match(rf"^{re.escape(parent_luhmann_id)}([a-z])$", cid)
            ]
            if chars:
                return f"{parent_luhmann_id}{chr(ord(max(chars)) + 1)}"
            return f"{parent_luhmann_id}a"
        
        elif parent_luhmann_id[-1].isalpha():
            nums = [
                int(re.search(rf"^{re.escape(parent_luhmann_id)}(\d+)$", cid).group(1))
                for cid in existing_sibling_ids
                if re.match(rf"^{re.escape(parent_luhmann_id)}(\d+)$", cid)
            ]
            return f"{parent_luhmann_id}{max(nums) + 1}" if nums else f"{parent_luhmann_id}1"
        
        return f"{parent_luhmann_id}.1"


# основной класс линкера

class GraphLinker:
    """
    Встраивает новые zettel-карточки в граф знаний (neo4j).
    Все операции изолированы по user_id.
    """
    
    def __init__(
        self,
        embedding_model: LocalEmbeddingModel = None,
        repository: ZettelRepository = None,
        model_name: str = settings.linker_model_name,
        temperature: float = settings.linker_temperature,
        system_prompt: str = settings.linker_system_prompt,
        user_prompt_template: str = settings.linker_user_prompt_template,
        similarity_threshold: float = settings.linker_similarity_threshold,
        max_candidates: int = settings.linker_max_candidates,
    ):
        self._embedding_model = embedding_model
        self._repository = repository
        
        self.model_name = model_name
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        
        self.llm = ChatOpenAI(
            model=self.model_name,
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            temperature=temperature,
        )
        self.structured_llm = self.llm.with_structured_output(LinkDecision)
        
        self.similarity_threshold = similarity_threshold
        self.max_candidates = max_candidates
        
        self._luhmann_remap: Dict[str, str] = {}  # временный id → реальный id в neo4j
    
    @property
    def embedding_model(self) -> LocalEmbeddingModel:
        if self._embedding_model is None:
            self._embedding_model = LocalEmbeddingModel()
        return self._embedding_model
    
    @property
    def repository(self) -> ZettelRepository:
        if self._repository is None:
            client = get_neo4j_client()
            init_schema(client)
            self._repository = ZettelRepository(client)
            # Дополнительная гарантия: удаляем legacy-связи между разными user_id.
            removed = self._repository.remove_cross_user_links()
            if removed:
                print(f"[Neo4j] Удалено кросс-пользовательских связей: {removed}")
        return self._repository
    
    def link_and_insert(self, user_id: str, new_cards: List[ZettelCard]) -> List[LinkResult]:
        """
        Обрабатывает список карточек от Atomizer и встраивает в граф пользователя.
        
        Args:
            user_id: ID пользователя (из Telegram)
            new_cards: Список карточек от Atomizer
        """
        results: List[LinkResult] = []
        self._luhmann_remap = {}
        
        for card in new_cards:
            print(f"\n{'─' * 55}")
            print(f"📋 [{card.luhmann_id}] {card.content[:55]}...")
            print(f"   root={card.is_root_topic}, parent={card.parent_luhmann_id}")
            
            embedding = self.embedding_model.embed_passage(card.content)
            
            # дочерние мысли внутри одного сообщения не требуют llm-решения
            if not card.is_root_topic and card.parent_luhmann_id in self._luhmann_remap:
                result = self._handle_inner_child(user_id, card, embedding)
            else:
                result = self._handle_root_card(user_id, card, embedding)
            
            results.append(result)
        
        return results
    
    def _handle_inner_child(self, user_id: str, card: ZettelCard, embedding: List[float]) -> LinkResult:
        """Карточка уже привязана к родителю внутри текущего сообщения."""
        real_parent_luhmann = self._luhmann_remap[card.parent_luhmann_id]
        
        parent_node = self.repository.get_by_luhmann_id(user_id, real_parent_luhmann)
        if not parent_node:
            return self._apply_new_root(user_id, card, embedding, "Родитель не найден в графе")
        
        siblings = self.repository.get_siblings(user_id, real_parent_luhmann)
        new_luhmann = ZettelIdGenerator.get_next_id(
            real_parent_luhmann,
            siblings,
            self.repository.get_max_root_id(user_id)
        )
        
        self._luhmann_remap[card.luhmann_id] = new_luhmann
        
        node = self.repository.create_child_of(
            user_id=user_id,
            content=card.content,
            luhmann_id=new_luhmann,
            thought_type=str(card.thought_type),
            tags=card.tags,
            embedding=embedding,
            parent_zettel_id=parent_node.zettel_id,
        )
        
        print(f"   ✅ Дочерняя → [{new_luhmann}] ← [{real_parent_luhmann}]")
        
        return LinkResult(
            card=node,
            action=LinkAction.CHILD_OF,
            reasoning="Дочерняя карточка внутри текущего сообщения.",
            candidates_found=0,
        )
    
    def _handle_root_card(self, user_id: str, card: ZettelCard, embedding: List[float]) -> LinkResult:
        """Корневая карточка: ищем похожие мысли и спрашиваем llm, куда встроить."""
        
        candidates = self.repository.vector_search(
            user_id=user_id,
            query_embedding=self.embedding_model.embed_query(card.content),
            limit=self.max_candidates,
            similarity_threshold=self.similarity_threshold,
        )
        
        print(f"   🔍 Vector search: {len(candidates)} кандидатов")
        for cand, score in candidates:
            print(f"      sim={score:.3f} [{cand.luhmann_id}]: {cand.content[:45]}...")
        
        if not candidates:
            # нет похожих мыслей — сразу создаём новый корень без llm
            return self._apply_new_root(user_id, card, embedding, "Нет похожих карточек в графе")
        
        # для каждого кандидата поднимаем окрестность графа (родитель, дети, теги)
        contexts: List[GraphContext] = []
        for cand, score in candidates:
            ctx = self.repository.get_context(user_id, cand.zettel_id, hops=1)
            if ctx:
                ctx.similarity = score
                contexts.append(ctx)
        
        decision = self._ask_llm(card, contexts)
        print(f"   🤖 LLM: {decision.action.value.upper()} | {decision.reasoning}")
        
        if decision.action == LinkAction.NEW_ROOT:
            return self._apply_new_root(user_id, card, embedding, decision.reasoning, len(candidates))
        elif decision.action == LinkAction.CHILD_OF:
            return self._apply_child_of(user_id, card, embedding, decision, len(candidates))
        elif decision.action == LinkAction.UPDATE_OF:
            return self._apply_update_of(user_id, card, embedding, decision, len(candidates))
        
        return self._apply_new_root(user_id, card, embedding, f"Непредвиденный ответ: {decision.action}")
    
    def _ask_llm(self, card: ZettelCard, contexts: List[GraphContext]) -> LinkDecision:
        """Формирует промпт с контекстом графа и спрашивает LLM."""
        
        candidates_text = ""
        for ctx in contexts:
            cand = ctx.candidate
            candidates_text += f"\n• UUID: {cand.zettel_id}\n"
            candidates_text += f"  Luhmann-ID: [{cand.luhmann_id}]\n"
            candidates_text += f"  Тип: {cand.thought_type}\n"
            candidates_text += f"  Сходство: {ctx.similarity:.3f}\n"
            candidates_text += f"  Текст: \"{cand.content}\"\n"
            
            if ctx.parent:
                candidates_text += f"  Родитель: [{ctx.parent.luhmann_id}] \"{ctx.parent.content[:40]}...\"\n"
            if ctx.children:
                children_str = ", ".join([f"[{c.luhmann_id}]" for c in ctx.children[:3]])
                candidates_text += f"  Дети: {children_str}\n"
            if ctx.entities:
                entities_str = ", ".join([e.name for e in ctx.entities[:5]])
                candidates_text += f"  Сущности: {entities_str}\n"
        
        user_prompt = self.user_prompt_template.format(
            thought_type=card.thought_type,
            tags=", ".join(card.tags),
            content=card.content,
            candidates_text=candidates_text,
        )
        
        return self.structured_llm.invoke([
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=user_prompt),
        ])
    
    def _apply_new_root(
        self,
        user_id: str,
        card: ZettelCard,
        embedding: List[float],
        reasoning: str,
        candidates_found: int = 0,
    ) -> LinkResult:
        """Создаёт новый корневой узел."""
        siblings = self.repository.get_siblings(user_id, None)
        new_luhmann = ZettelIdGenerator.get_next_id(
            None,
            siblings,
            self.repository.get_max_root_id(user_id)
        )
        
        self._luhmann_remap[card.luhmann_id] = new_luhmann
        
        node = self.repository.create_zettel(
            user_id=user_id,
            content=card.content,
            luhmann_id=new_luhmann,
            thought_type=str(card.thought_type),
            tags=card.tags,
            embedding=embedding,
            is_root_topic=True,
        )
        
        print(f"   ✅ NEW_ROOT → [{new_luhmann}]")
        
        return LinkResult(
            card=node,
            action=LinkAction.NEW_ROOT,
            reasoning=reasoning,
            candidates_found=candidates_found,
        )
    
    def _apply_child_of(
        self,
        user_id: str,
        card: ZettelCard,
        embedding: List[float],
        decision: LinkDecision,
        candidates_found: int,
    ) -> LinkResult:
        """Создаёт дочерний узел."""
        parent_node = self.repository.get_by_id(user_id, decision.target_zettel_id)
        
        if not parent_node:
            print(f"   ⚠️  Родитель {decision.target_zettel_id} не найден! Fallback → new_root")
            return self._apply_new_root(user_id, card, embedding, "Родитель не найден", candidates_found)
        
        siblings = self.repository.get_siblings(user_id, parent_node.luhmann_id)
        new_luhmann = ZettelIdGenerator.get_next_id(
            parent_node.luhmann_id,
            siblings,
            self.repository.get_max_root_id(user_id)
        )
        
        self._luhmann_remap[card.luhmann_id] = new_luhmann
        
        node = self.repository.create_child_of(
            user_id=user_id,
            content=card.content,
            luhmann_id=new_luhmann,
            thought_type=str(card.thought_type),
            tags=card.tags,
            embedding=embedding,
            parent_zettel_id=parent_node.zettel_id,
        )
        
        print(f"   ✅ CHILD_OF [{parent_node.luhmann_id}] → [{new_luhmann}]")
        
        return LinkResult(
            card=node,
            action=LinkAction.CHILD_OF,
            reasoning=decision.reasoning,
            candidates_found=candidates_found,
            target_zettel_id=decision.target_zettel_id,
        )
    
    def _apply_update_of(
        self,
        user_id: str,
        card: ZettelCard,
        embedding: List[float],
        decision: LinkDecision,
        candidates_found: int,
    ) -> LinkResult:
        """Обновляет существующий узел."""
        updated_node = self.repository.update_zettel_content(
            user_id=user_id,
            zettel_id=decision.target_zettel_id,
            new_content=card.content,
            new_embedding=embedding,
            reason=decision.reasoning,
        )
        
        if not updated_node:
            return LinkResult(
                card=ZettelNode(
                    zettel_id="",
                    luhmann_id="",
                    content=card.content,
                    thought_type=str(card.thought_type),
                    tags=card.tags,
                    is_root_topic=False,
                ),
                action=LinkAction.UPDATE_OF,
                reasoning=f"Ошибка: карточка {decision.target_zettel_id} не найдена",
                candidates_found=candidates_found,
                target_zettel_id=decision.target_zettel_id,
            )
        
        print(f"   ✅ UPDATE_OF [{updated_node.luhmann_id}]")
        
        return LinkResult(
            card=updated_node,
            action=LinkAction.UPDATE_OF,
            reasoning=decision.reasoning,
            candidates_found=candidates_found,
            target_zettel_id=decision.target_zettel_id,
        )
    
    def print_graph(self, user_id: str) -> None:
        """Выводит граф пользователя."""
        self.repository.print_graph(user_id)
    
    def get_user_stats(self, user_id: str) -> dict:
        """Возвращает статистику по графу пользователя."""
        return {
            "total_cards": self.repository.total_count(user_id),
            "max_root_id": self.repository.get_max_root_id(user_id),
        }
