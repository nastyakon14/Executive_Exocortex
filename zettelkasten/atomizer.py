# декомпозиция заметки на атомарные мысли (zettel-карточки)
# метод zettelkasten: одна мысль = одна карточка

import uuid
import re
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional
from enum import Enum

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage

from dotenv import load_dotenv
from config.settings import settings
from observability.llm import make_chat_openai, print_llm_request

load_dotenv()

ATOMIZER_CHUNK_CHARS = 5000
ATOMIZER_CHUNK_OVERLAP = 1200
ATOMIZER_PRIOR_THOUGHTS = 16
ATOMIZER_PRIOR_CHARS = 4000


@dataclass
class TextChunk:
    """Уникальный фрагмент документа плюс хвост предыдущего — только как контекст."""
    body: str
    overlap: str = ""


# pydantic-схемы для structured output llm
class ThoughtType(str, Enum):
    # типы мыслей: факт, решение, задача, риск, идея, контекст, вопрос
    FACT = "fact"         # информация, данные, метрики
    DECISION = "decision" # принятые решения
    ACTION = "action"     # задачи, поручения, todo
    RISK = "risk"         # проблемы, риски, угрозы
    IDEA = "idea"         # гипотезы, инициативы, предложения
    QUESTION = "question" # открытые вопросы, требующие разбора
    CONTEXT = "context"   # фоновое окружение, важные условия
    OTHER = "other"       # прочее


class AtomicThought(BaseModel):
    '''Объект атомарной мысли'''
    topic: str = Field(
        description=(
            "Краткая тема мысли (1-3 слова). "
            "Суть мысли в максимально сжатой форме. "
            "Например: 'Поправки ПДД', 'Встреча с клиентом', 'Идея продукта', 'Риск проекта'."
        )
    )
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
    topic: str = Field(description="Краткая тема мысли (1-3 слова)")  # тема мысли для отображения на графе
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
    """Генератор идентификаторов по методу лумана."""

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
        model_name: str = settings.zettel_atomizer_model_name,
        temperature: float = settings.zettel_atomizer_temperature,
        system_prompt: str = settings.zettel_atomizer_system_prompt,
        user_prompt_template: str = settings.zettel_atomizer_user_prompt_template,
    ):

        self.model_name = model_name
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template

        base_llm = make_chat_openai(
            component="atomizer",
            model_name=self.model_name,
            temperature=self.temperature,
        )
        self.structured_llm = base_llm.with_structured_output(AtomicThoughtList)

    def atomize(
        self,
        text: str,
        current_db_max_root_id: int = 0,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[ZettelCard]:
        """
        Текст заметки → список Zettel-карточек.

        Длинный документ режется на фрагменты с перекрытием. Каждый следующий
        вызов видит уже извлечённые мысли, поэтому иерархия остаётся как у
        цельного разбора. Карточки и Luhmann-id собираются один раз в конце.
        """
        text = text.strip()
        if not text:
            return ("Пустой текст заметки")
        try:
            chunks = self._split_text_for_llm(text)
            all_thoughts: list[AtomicThought] = []
            total = len(chunks)
            for i, chunk in enumerate(chunks, 1):
                if on_progress:
                    on_progress(i, total)
                part = (i, total) if total > 1 else None
                thoughts = self._extract_thoughts(
                    chunk.body,
                    prior_thoughts=all_thoughts,
                    overlap=chunk.overlap,
                    part=part,
                )
                all_thoughts.extend(self._dedupe_thoughts(all_thoughts, thoughts))
            cards = self._build_cards(all_thoughts, current_db_max_root_id)
            return self._validate_and_fix(cards)
        except Exception as e:
            return (f"Ошибка при извлечении атомарных мыслей (atomizer.atomize) --> {e}")

    @staticmethod
    def _is_length_limit_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = (
            "length limit",
            "max_tokens",
            "maximum context",
            "json_invalid",
            "invalid json",
            "eof while parsing",
            "unterminated string",
            "unterminated string starting",
        )
        return any(m in msg for m in markers)

    def _extract_thoughts(
        self,
        text: str,
        prior_thoughts: Optional[list[AtomicThought]] = None,
        overlap: str = "",
        part: Optional[tuple[int, int]] = None,
        depth: int = 0,
    ) -> list[AtomicThought]:
        prior_thoughts = prior_thoughts or []
        try:
            raw_result: AtomicThoughtList = self._invoke_llm(
                text,
                prior_thoughts=prior_thoughts,
                overlap=overlap,
                part=part,
            )
            return list(raw_result.thoughts or [])
        except Exception as e:
            if depth >= 3 or not self._is_length_limit_error(e) or len(text) < 800:
                raise
            left, right, mid_overlap = self._bisect_text(text)
            first = self._extract_thoughts(left, prior_thoughts, overlap, part, depth + 1)
            second = self._extract_thoughts(
                right,
                prior_thoughts + first,
                mid_overlap,
                part,
                depth + 1,
            )
            return first + second

    def _bisect_text(self, text: str) -> tuple[str, str, str]:
        mid = len(text) // 2
        split_at = text.rfind("\n", 200, mid + 200)
        if split_at < 200:
            split_at = text.rfind(". ", 200, mid + 200)
            if split_at >= 200:
                split_at += 1
        if split_at < 200:
            split_at = mid
        left = text[:split_at].strip()
        right = text[split_at:].strip()
        if not left or not right:
            left, right = text[:mid].strip(), text[mid:].strip()
        return left, right, self._tail_overlap(left)

    def _split_text_for_llm(self, text: str, max_chars: int = ATOMIZER_CHUNK_CHARS) -> list[TextChunk]:
        """Режет документ по абзацам и предложениям. Перекрытие не атомизируется повторно."""
        if len(text) <= max_chars:
            return [TextChunk(body=text)]

        pieces = self._pack_blocks(text, max_chars)
        chunks: list[TextChunk] = []
        for i, piece in enumerate(pieces):
            overlap = self._tail_overlap(pieces[i - 1]) if i else ""
            chunks.append(TextChunk(body=piece, overlap=overlap))
        return chunks or [TextChunk(body=text[:max_chars])]

    def _pack_blocks(self, text: str, max_chars: int) -> list[str]:
        blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
        if not blocks:
            return self._split_long_block(text, max_chars)
        chunks: list[str] = []
        buf = ""
        for block in blocks:
            if len(block) > max_chars:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.extend(self._split_long_block(block, max_chars))
                continue
            candidate = f"{buf}\n\n{block}" if buf else block
            if len(candidate) > max_chars and buf:
                chunks.append(buf)
                buf = block
            else:
                buf = candidate
        if buf:
            chunks.append(buf)
        return chunks

    def _split_long_block(self, text: str, max_chars: int) -> list[str]:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
        if not sentences:
            return self._hard_wrap(text, max_chars)
        chunks: list[str] = []
        buf = ""
        for sentence in sentences:
            if len(sentence) > max_chars:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.extend(self._hard_wrap(sentence, max_chars))
                continue
            candidate = f"{buf} {sentence}".strip() if buf else sentence
            if len(candidate) > max_chars and buf:
                chunks.append(buf)
                buf = sentence
            else:
                buf = candidate
        if buf:
            chunks.append(buf)
        return chunks

    def _hard_wrap(self, text: str, max_chars: int) -> list[str]:
        step = max(max_chars - ATOMIZER_CHUNK_OVERLAP, 1)
        return [text[i:i + max_chars].strip() for i in range(0, len(text), step) if text[i:i + max_chars].strip()]

    def _tail_overlap(self, text: str, size: int = ATOMIZER_CHUNK_OVERLAP) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        tail = text[-size:] if len(text) > size else text
        for sep in ("\n\n", "\n", ". "):
            pos = tail.find(sep)
            if 0 <= pos < len(tail) - 40:
                tail = tail[pos + len(sep):].strip()
                break
        return tail

    def _dedupe_thoughts(
        self,
        existing: list[AtomicThought],
        incoming: list[AtomicThought],
    ) -> list[AtomicThought]:
        seen = [self._norm_content(t.content) for t in existing]
        unique: list[AtomicThought] = []
        for thought in incoming:
            key = self._norm_content(thought.content)
            if not key or self._is_duplicate_key(key, seen):
                continue
            seen.append(key)
            unique.append(thought)
        return unique

    @staticmethod
    def _norm_content(content: str) -> str:
        return re.sub(r"\s+", " ", (content or "").strip().lower()).rstrip(".,;:!?")

    @staticmethod
    def _is_duplicate_key(key: str, seen: list[str]) -> bool:
        if key in seen:
            return True
        for prev in seen:
            shorter, longer = (key, prev) if len(key) <= len(prev) else (prev, key)
            if len(shorter) >= 24 and shorter in longer and len(shorter) / max(len(longer), 1) >= 0.82:
                return True
        return False

    def _continuation_preamble(
        self,
        prior_thoughts: list[AtomicThought],
        overlap: str,
        part: Optional[tuple[int, int]],
    ) -> str:
        if not part and not prior_thoughts and not overlap:
            return ""
        lines = []
        if part and part[1] > 1:
            lines.append(
                f"Это часть {part[0]} из {part[1]} одного документа. "
                "Обрабатывай так, будто читаешь документ целиком: не дублируй уже извлечённые мысли, "
                "продолжай иерархию через parent_hint (дословная цитата content родительской мысли, "
                "в том числе из списка ниже). Новую корневую тему открывай только если она действительно новая."
            )
        prior_block = self._format_prior_thoughts(prior_thoughts)
        if prior_block:
            lines.append("Уже извлечённые мысли этого документа (можно ссылаться через parent_hint, повторять нельзя):")
            lines.append(prior_block)
        if overlap:
            lines.append("Контекст конца предыдущей части (уже обработан, не атомизируй повторно):")
            lines.append(overlap)
        return "\n\n".join(lines) + "\n\n" if lines else ""

    def _format_prior_thoughts(self, thoughts: list[AtomicThought]) -> str:
        if not thoughts:
            return ""
        roots = [t for t in thoughts if t.is_root_topic]
        recent = thoughts[-ATOMIZER_PRIOR_THOUGHTS:]
        ordered: list[AtomicThought] = []
        seen = set()
        for thought in roots[:8] + recent:
            key = self._norm_content(thought.content)
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(thought)
        lines = []
        used = 0
        for thought in ordered:
            line = f"- {thought.content.strip()}"
            if used + len(line) > ATOMIZER_PRIOR_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def _invoke_llm(
        self,
        text: str,
        prior_thoughts: Optional[list[AtomicThought]] = None,
        overlap: str = "",
        part: Optional[tuple[int, int]] = None,
    ) -> AtomicThoughtList:
        preamble = self._continuation_preamble(prior_thoughts or [], overlap, part)
        body = self.user_prompt_template.format(text=text)
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=preamble + body),
        ]
        print_llm_request("atomizer", messages, model_name=self.model_name)
        return self.structured_llm.invoke(messages)

    def _build_cards(
        self,
        thoughts: list[AtomicThought],
        current_db_max_root_id: int = 0
    ) -> list[ZettelCard]:
        """Строит zettel-карточки: назначает uuid, luhmann id и связи parent_hint."""
        cards = []
        content_to_uuid = {}
        content_to_luhmann = {}
        children_registry = defaultdict(list)
        root_luhmann_ids = []

        def remember(raw: str, uid: str, luhmann: str) -> None:
            if not raw:
                return
            content_to_uuid[raw] = uid
            content_to_luhmann[raw] = luhmann
            cleaned = self._clean_content(raw)
            content_to_uuid[cleaned] = uid
            content_to_luhmann[cleaned] = luhmann

        def lookup_parent(hint: Optional[str]):
            if not hint:
                return None, None
            cleaned = self._clean_content(hint)
            uid = content_to_uuid.get(hint) or content_to_uuid.get(cleaned)
            if uid:
                luhmann = content_to_luhmann.get(hint) or content_to_luhmann.get(cleaned)
                return uid, luhmann
            hint_norm = self._norm_content(hint)
            for stored, luhmann in content_to_luhmann.items():
                if self._norm_content(stored) == hint_norm:
                    return content_to_uuid.get(stored), luhmann
            return None, None

        for thought in thoughts:
            current_uuid = str(uuid.uuid4())
            parent_uuid = None
            parent_luhmann = None

            if not thought.is_root_topic and thought.parent_hint:
                parent_uuid, parent_luhmann = lookup_parent(thought.parent_hint)

            if not parent_uuid:
                thought.is_root_topic = True

            existing_siblings = children_registry[parent_luhmann] if parent_luhmann else root_luhmann_ids
            current_luhmann = ZettelIdGenerator.get_next_id(parent_luhmann, existing_siblings, current_db_max_root_id)

            remember(thought.content, current_uuid, current_luhmann)
            if parent_luhmann:
                children_registry[parent_luhmann].append(current_luhmann)
            else:
                root_luhmann_ids.append(current_luhmann)

            card = ZettelCard(
                zettel_id=current_uuid,
                luhmann_id=current_luhmann,
                parent_id=parent_uuid,
                parent_luhmann_id=parent_luhmann,
                topic=self._clean_topic(thought.topic),
                content=self._clean_content(thought.content),
                thought_type=thought.thought_type,
                tags=self._normalize_tags(thought.tags),
                parent_hint=thought.parent_hint,
                is_root_topic=thought.is_root_topic,
            )
            cards.append(card)
        return cards

    def _clean_topic(self, topic: str) -> str:
        topic = " ".join(topic.split())
        topic = topic.rstrip(".,;:!?")
        words = topic.split()[:3]
        return " ".join(words) if words else "Без темы"

    def _clean_content(self, content: str) -> str:
        content = " ".join(content.split())
        if content and content[-1] not in ".!?":
            content += "."
        return content

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        normalized = []
        seen = set()
        for tag in tags:
            tag = tag.lower().strip().replace(" ", "_").replace("-", "_")
            if tag and tag not in seen:
                normalized.append(tag)
                seen.add(tag)
        return normalized[:5]

    def _validate_and_fix(self, cards: list[ZettelCard]) -> list[ZettelCard]:
        """Пост-валидация: убираем пустые карточки и чиним битые parent_hint."""
        cards = [c for c in cards if c.content.strip()]
        contents = {c.content for c in cards}
        contents.update(self._clean_content(c.content) for c in cards)
        for card in cards:
            if not card.parent_hint:
                continue
            hint = card.parent_hint
            if hint in contents or self._clean_content(hint) in contents:
                continue
            hint_norm = self._norm_content(hint)
            if any(self._norm_content(c.content) == hint_norm for c in cards):
                continue
            card.parent_hint = None
        if cards and not any(c.is_root_topic for c in cards):
            cards[0].is_root_topic = True
        return cards
