# Декомпозиция заметки на атомарные мысли (Zettel-карточки)
# Метод Zettelkasten — одна мысль = одна карточка

import os
import uuid
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from dotenv import load_dotenv
from config.settings import settings

load_dotenv()

# structured_output with pydatic schemas for LLM
class ThoughtType(str, Enum):
    # типы мыслей: факт, решение, задача, риск, идея или контекст
    FACT = "fact"         # Информация, данные, метрики
    DECISION = "decision" # Принятые решения
    ACTION = "action"     # Задачи, поручения, todo
    RISK = "risk"         # Проблемы, риски, угрозы
    IDEA = "idea"         # Гипотезы, инициативы, предложения
    QUESTION = "question" # Открытые вопросы, факапы, требующие разбора
    CONTEXT = "context"   # Фоновое окружение, важные условия
    OTHER = "other"       # Информация, не подошедшая ни под один из критериев выше



class AtomicThought(BaseModel):
    '''Объект атомарной мысли'''
    content: str = Field(
        description=(
            "Одно краткое самодостаточное атомарное утверждение (1-2 предложения). "
            "Утверждение должно быть самодостаточным и понятным без контекста других мыслей даже спустя время."
            "Одна мысль = один объект атомарной мысли."
        )
    )
    thought_type: ThoughtType = Field(
        description="Тип мысли (характеристика мысли): факт, решение, задача, риск, идея, контекст, вопрос, ответ, объяснение, комментарий, заметка, память, напоминание, побуждение к действию, другое"
    )
    tags: list[str] = Field(
        description=(
            "Ключевые теги — сущности: имена людей, проекты, организации, технологии. "
            "snake_case, на языке оригинала. От 1 до 5 тегов."
            "Теги должны быть связаны с содержанием мысли и помогать найти её в базе знаний."
            "Теги должны быть на языке оригинала."
            "Теги должны быть уникальными."
        )
    )
    parent_hint: Optional[str] = Field(
        default=None,
        description=(
            "Точная цитата другой мысли из этого же списка атомарных мыслей, "
            "которую данная мысль уточняет или объясняет. "
            "Точная цитата должна быть полностью идентична тексту мысли, из которой она взята."
            "None если мысль самостоятельна."
            "Если мысль уточняет или объясняет другую мысль, то она должна быть ссылкой на эту мысль."
        )
    )
    is_root_topic: bool = Field(
        description=(
            "True если эта мысль открывает новую независимую тему (корневую мысль). "
            "False если она развивает,раскрывает или уточняет другую мысль из этого текста преложенную раньше."
        )
    )


class AtomicThoughtList(BaseModel):
    '''Список всех атомарных мыслей, извлечённых из текста'''
    thoughts: list[AtomicThought] = Field(
        description="Список всех атомарных мыслей, извлечённых из текста"
    )
    created_at: datetime = Field(default_factory=datetime.now(timezone.utc))  # дата время создания списка атомарных  мыслей


class ZettelCard(BaseModel):
    '''Zettel-карточка - атомарная мысль с информацией о связях и контексте'''
    zettel_id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # уникальный идентификатор карточки
    luhmann_id: str = Field(description="Идентификатор по методу Лумана (например: 1, 1.1, 1.1a)") 
    parent_id: Optional[str] = Field(default=None, description="UUID родительской карточки") 
    parent_luhmann_id: Optional[str] = Field(default=None, description="Луман-ID родительской карточки")
    content: str = Field(description="Контент карточки")    # одно краткое самодостаточное атомарное утверждение (1-2 предложения)  
    thought_type: ThoughtType = Field(description="Тип мысли")   # тип мысли: факт, решение, задача, риск, идея, контекст, вопрос, ответ, объяснение, комментарий, заметка, память, напоминание, побуждение к действию, другое
    tags: list[str] = Field(description="Теги карточки")         # ключевые теги — сущности: имена людей, проекты, организации, технологии. snake_case, на языке оригинала. От 1 до 5 тегов.
    parent_hint: Optional[str] = Field(default=None, description="Точная цитата другой мысли из этого же списка, которую данная мысль уточняет или объясняет") # точная цитата другой мысли из этого же списка, которую данная мысль уточняет или объясняет None если мысль самостоятельна
    is_root_topic: bool = Field(description="True если эта мысль открывает новую независимую тему") # True если эта мысль открывает новую независимую тему False если она развивает,раскрывает или уточняет другую мысль из этого текста преложенную раньше.    
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Дата время создания карточки") # дата время создания карточки
    embedding: Optional[list[float]] = Field(default=None, description="Векторное представление карточки") # векторное представление карточки

    class Config:
        use_enum_values = True


class ZettelIdGenerator:
    """Генератор идентификаторов по методу Лумана"""
    # генерировать уникальные идентификаторы для каждой мысли в зависимости от её типа и родителя
    @staticmethod
    def get_next_id(parent_luhmann_id: Optional[str], existing_sibling_ids: list[str], current_max_root: int = 0) -> str:
        if not parent_luhmann_id:
            if not existing_sibling_ids:
                return str(current_max_root + 1)
            roots = [int(i) for i in existing_sibling_ids if i.isdigit()]
            return str(max(roots) + 1) if roots else str(current_max_root + 1)

        if not existing_sibling_ids:
            if parent_luhmann_id.isdigit(): return f"{parent_luhmann_id}.1"
            elif parent_luhmann_id[-1].isdigit(): return f"{parent_luhmann_id}a"
            elif parent_luhmann_id[-1].isalpha(): return f"{parent_luhmann_id}1"

        if parent_luhmann_id.isdigit():
            nums = [int(re.search(rf"^{re.escape(parent_luhmann_id)}\.(\d+)$", cid).group(1)) 
                    for cid in existing_sibling_ids if re.match(rf"^{re.escape(parent_luhmann_id)}\.(\d+)$", cid)]
            return f"{parent_luhmann_id}.{max(nums) + 1}" if nums else f"{parent_luhmann_id}.1"

        elif parent_luhmann_id[-1].isdigit():
            chars = [re.search(rf"^{re.escape(parent_luhmann_id)}([a-z])$", cid).group(1) 
                     for cid in existing_sibling_ids if re.match(rf"^{re.escape(parent_luhmann_id)}([a-z])$", cid)]
            if chars: return f"{parent_luhmann_id}{chr(ord(max(chars)) + 1)}"
            return f"{parent_luhmann_id}a"

        elif parent_luhmann_id[-1].isalpha():
            nums = [int(re.search(rf"^{re.escape(parent_luhmann_id)}(\d+)$", cid).group(1)) 
                    for cid in existing_sibling_ids if re.match(rf"^{re.escape(parent_luhmann_id)}(\d+)$", cid)]
            return f"{parent_luhmann_id}{max(nums) + 1}" if nums else f"{parent_luhmann_id}1"


class NoteAtomizer:
    """
    Разбивает входной текст заметки на атомарные Zettel-карточки.
    """

    def __init__(
        self,
        # из конфига
        model_name: str = settings.zettel_atomizer_model_name,
        temperature: float = settings.zettel_atomizer_temperature,
        system_prompt: str = settings.zettel_atomizer_prompt,
    ):

        self.model_name = model_name
        self.temperature = temperature
        self.system_prompt = system_prompt

        base_llm = ChatOpenAI(
            model=self.model_name,
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            temperature=self.temperature,
        )
        self.structured_llm = base_llm.with_structured_output(AtomicThoughtList)



    def atomize(self, text: str, current_db_max_root_id: int = 0) -> list[ZettelCard]:
        """
        Основной метод: текст заметки → список Zettel-карточек.

        Args:
            text: исходный текст заметки
            current_db_max_root_id: текущий максимальный ID корневой заметки в базе (добавлено для нумерации Лумана)

        Returns:
            Список ZettelCard, готовых для вставки в граф.
        """
        text = text.strip()
        if not text:
            return ("Пустой текст заметки")
        try:
            raw_result: AtomicThoughtList = self._invoke_llm(text) # вызов LLM для извлечения атомарных мыслей
            cards = self._build_cards(raw_result.thoughts, current_db_max_root_id) # построение Zettel-карточек из атомарных мыслей
            cards = self._validate_and_fix(cards) # валидация и исправление Zettel-карточек
            return cards
        except Exception as e:
            return (f"Ошибка при извлечении атомарных мыслей (atomizer.atomize) --> {e}")


    def _invoke_llm(self, text: str) -> AtomicThoughtList:
        '''Вызов LLM для извлечения атомарных мыслей'''
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=f"Разбей следующую заметку на атомарные мысли:\n\n{text}"),
        ]
        return self.structured_llm.invoke(messages)

    def _build_cards(
        self,
        thoughts: list[AtomicThought],
        current_db_max_root_id: int = 0
    ) -> list[ZettelCard]:
        '''Построение Zettel-карточек из атомарных мыслей'''
        cards = []
        content_to_uuid = {}          # Сопоставление: текст мысли -> её новый UUID
        content_to_luhmann = {}       # Сопоставление: текст мысли -> её новый Луман-ID
        children_registry = defaultdict(list) 
        root_luhmann_ids = []

        for thought in thoughts: # построение Zettel-карточек из атомарных мыслей
            current_uuid = str(uuid.uuid4())
            content_to_uuid[thought.content] = current_uuid
            
            parent_uuid = None
            parent_luhmann = None

            if not thought.is_root_topic and thought.parent_hint:
                parent_uuid = content_to_uuid.get(thought.parent_hint)
                parent_luhmann = content_to_luhmann.get(thought.parent_hint)

            if not parent_uuid:
                thought.is_root_topic = True
                
            existing_siblings = children_registry[parent_luhmann] if parent_luhmann else root_luhmann_ids
            current_luhmann = ZettelIdGenerator.get_next_id(parent_luhmann, existing_siblings, current_db_max_root_id)
            
            content_to_luhmann[thought.content] = current_luhmann
            if parent_luhmann:
                children_registry[parent_luhmann].append(current_luhmann)
            else:
                root_luhmann_ids.append(current_luhmann)

            card = ZettelCard(
                zettel_id=current_uuid,             # уникальный идентификатор карточки
                luhmann_id=current_luhmann,         # идентификатор по методу Лумана
                parent_id=parent_uuid,              # UUID родительской карточки
                parent_luhmann_id=parent_luhmann,   # Луман-ID родительской карточки        
                content=self._clean_content(thought.content),# очистка контента
                thought_type=thought.thought_type,  # тип мысли
                tags=self._normalize_tags(thought.tags),# нормализация тегов
                parent_hint=thought.parent_hint,    # может быть None — всё ок
                is_root_topic=thought.is_root_topic,# признак корневой мысли
            )
            cards.append(card) # добавление Zettel-карточки в список
        return cards

    def _clean_content(self, content: str) -> str:
        '''Очистка контента Zettel-карточки от лишних пробелов и символов'''
        content = " ".join(content.split())
        if content and content[-1] not in ".!?":
            content += "."
        return content

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        '''Нормализация тегов Zettel-карточки. От 1 до 5 тегов.'''
        normalized = []
        seen = set()
        for tag in tags:
            tag = tag.lower().strip().replace(" ", "_").replace("-", "_")
            if tag and tag not in seen:
                normalized.append(tag)
                seen.add(tag)
        return normalized[:5]  # ограничение на первые 5 тегов

    def _validate_and_fix(self, cards: list[ZettelCard]) -> list[ZettelCard]:
        # Удаляем пустые
        cards = [c for c in cards if c.content.strip()]

        # Проверяем parent_hint — должен совпадать с content одной из карточек
        contents = {c.content for c in cards}
        for card in cards:
            if card.parent_hint and card.parent_hint not in contents:
                card.parent_hint = None

        # Если ни одна не помечена как корневая — первую делаем корневой
        if cards and not any(c.is_root_topic for c in cards):
            cards[0].is_root_topic = True

        return cards