import re
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
from datetime import datetime

from natasha import (
    Segmenter,
    NewsEmbedding,
    NewsNERTagger,
    Doc
)
from faker import Faker

# языки русский и английский
fake_ru = Faker('ru_RU')
fake_en = Faker('en_US')

# Типы сущностей и их фейковые генераторы имен, фамилий, адресов и др конф данных
FAKE_GENERATORS = {
    'ИМЯ':          lambda: fake_ru.name(),
    'ОРГАНИЗАЦИЯ':  lambda: fake_ru.large_company(),
    'АДРЕС':        lambda: fake_ru.address().replace('\n', ', '),
    'ТЕЛЕФОН':      lambda: fake_ru.phone_number(),
    'EMAIL':        lambda: fake_ru.email(),
    'ДАТА':         lambda: fake_ru.date(pattern='%d.%m.%Y'),
    'КАРТА':        lambda: fake_ru.credit_card_number(card_type='visa'),
    'ИНН':          lambda: ''.join([str(fake_ru.random_digit()) for _ in range(12)]),
    'СНИЛС':        lambda: '{}-{}-{} {}'.format(
                        ''.join([str(fake_ru.random_digit()) for _ in range(3)]),
                        ''.join([str(fake_ru.random_digit()) for _ in range(3)]),
                        ''.join([str(fake_ru.random_digit()) for _ in range(3)]),
                        ''.join([str(fake_ru.random_digit()) for _ in range(2)])
                    ),
    'ПАСПОРТ':      lambda: '{} {}'.format(
                        ''.join([str(fake_ru.random_digit()) for _ in range(4)]),
                        ''.join([str(fake_ru.random_digit()) for _ in range(6)])
                    ),
    'ОГРН':         lambda: ''.join([str(fake_ru.random_digit()) for _ in range(13)]),
    'СЧЕТ':         lambda: ''.join([str(fake_ru.random_digit()) for _ in range(20)]),
    'БИК':          lambda: '04' + ''.join([str(fake_ru.random_digit()) for _ in range(7)]),
    'IP':           lambda: fake_ru.ipv4(),
    'URL':          lambda: fake_ru.url(),
    'ГОРОД':        lambda: fake_ru.city_name(),
    'СТРАНА':       lambda: fake_ru.country(),
    'ВАЛЮТА':       lambda: fake_ru.currency_name()
}

# regex
REGEX_PATTERNS = {
    'ТЕЛЕФОН': [
        r'\+7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}',
        r'8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}',
        r'\+7\d{10}',
    ],
    'EMAIL': [
        r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
    ],
    'КАРТА': [
        r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
    ],
    'СЧЕТ': [
        r'\b\d{20}\b',
    ],
    'БИК': [
        r'\bБИК[\s:]*\d{9}\b',
        r'\b04\d{7}\b',
    ],
    'ИНН': [
        r'\bИНН[\s:]*\d{10,12}\b',
    ],
    'СНИЛС': [
        r'\b\d{3}-\d{3}-\d{3}\s\d{2}\b',
        r'\bСНИЛС[\s:]*\d{11}\b',
    ],
    'ПАСПОРТ': [
        r'(?:паспорт|серия)[\s:]*\d{4}[\s]?\d{6}',
        r'\b\d{4}\s\d{6}\b',
    ],
    'ОГРН': [
        r'\bОГРН[\s:]*\d{13,15}\b',
    ],
    'IP': [
        r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    ],
    'URL': [
        r'https?://[^\s]+',
        r'www\.[^\s]+',
    ],
    'ДАТА': [
        r'\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b',
        r'\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}\b',
    ],
}

# Типы NER из Natasha
NER_TYPE_MAP = {
    'PER': 'ИМЯ',
    'ORG': 'ОРГАНИЗАЦИЯ',
    'LOC': 'АДРЕС',
}

@dataclass
class Entity:
    """Найденная сущность"""
    token:       str            # [ИМЯ_1]
    entity_type: str            # ИМЯ
    original:    str            # Иван Иванов
    fake_value:  str            # Пётр Петров
    start:       int            # позиция в оригинальном тексте
    end:         int
    source:      str            # regex / ner


@dataclass
class AnonymizationResult:
    """Результат анонимизации"""
    session_id:      str
    original_text:   str
    anonymized_text: str
    entities:        list[Entity] = field(default_factory=list)
    created_at:      str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            'session_id':      self.session_id,
            'created_at':      self.created_at,
            'original_text':   self.original_text,
            'anonymized_text': self.anonymized_text,
            'entities': [
                {
                    'token':       e.token,
                    'entity_type': e.entity_type,
                    'original':    e.original,
                    'fake_value':  e.fake_value,
                    'start':       e.start,
                    'end':         e.end,
                    'source':      e.source,
                }
                for e in self.entities
            ],
            'stats': {
                'total_entities': len(self.entities),
                'by_type': self._count_by_type(),
            }
        }

    def _count_by_type(self) -> dict:
        counts = {}
        for e in self.entities:
            counts[e.entity_type] = counts.get(e.entity_type, 0) + 1
        return counts


_TOKEN_INNER_RE = re.compile(r'^\[(.+)_(\d+)\]$')
_WORD_CHARS = r'A-Za-zА-Яа-яЁё0-9'

# Всегда маскируем бренд МТС, даже если NER его пропустил.
HARDCODE_ORG_ALIASES = (
    'МТС', 'мтс', 'Мтс',
    'MTS', 'mts', 'Mts',
    'Мобильные ТелеСистемы',
    'Мобильные Телесистемы',
    'Мобильные телесистемы',
    'Мобильные теле системы',
    'Мобильные Теле Системы',
    'Мобильных ТелеСистем',
    'Мобильными ТелеСистемами',
    'Mobile TeleSystems',
    'Mobile Telesystems',
    'Mobile Tele Systems',
    'ПАО МТС',
    'ПАО «МТС»',
    'ПАО "МТС"',
    'АО МТС',
    'МТС Банк',
    'МТС-Банк',
    'MTS Bank',
)
HARDCODE_ORG_PATTERNS = (
    r'Мобильн[а-яё]*\s+Теле[\s\-]*[Сс]истем[а-яё]*',
    r'Mobile\s+Tele[\s\-]*Systems?',
    r'(?:ПАО|ОАО|АО)\s*[«"\']?\s*МТС\s*[»"\']?',
    r'(?:ПАО|ОАО|АО)\s*[«"\']?\s*MTS\s*[»"\']?',
    r'МТС[\s\-]?[Бб]анк[а-яё]*',
    r'MTS[\s\-]?Bank',
    rf'(?<![{_WORD_CHARS}])МТС(?![{_WORD_CHARS}])',
    rf'(?<![{_WORD_CHARS}])MTS(?![{_WORD_CHARS}])',
)
_NAME_ENTITY_TYPES = {'ИМЯ', 'ОРГАНИЗАЦИЯ', 'АДРЕС', 'ГОРОД', 'СТРАНА'}
_RU_SUFFIXES = (
    'ого', 'ему', 'ами', 'ями', 'ыми', 'ими',
    'овым', 'евым', 'овой', 'евой', 'овою',
    'ова', 'ева', 'ина', 'ына', 'ову', 'еву', 'ину',
    'ове', 'еве',
    'ой', 'ий', 'ый', 'ая', 'ое', 'ые', 'ие', 'ую', 'юю',
    'ов', 'ев', 'ин', 'ын', 'ым', 'им', 'ом', 'ем',
    'ах', 'ях', 'ам', 'ям',
    'а', 'я', 'у', 'ю', 'е', 'о', 'ы', 'и', 'й',
)


def _word_stem(word: str) -> str:
    w = word.casefold()
    if len(w) <= 3:
        return w
    for suf in _RU_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    return w[:5] if len(w) > 5 else w


def _name_stems(text: str) -> tuple[str, ...]:
    parts = re.findall(rf'[{_WORD_CHARS}]+', text.casefold())
    return tuple(_word_stem(p) for p in parts if p)


def _token_index(token: str) -> int:
    match = _TOKEN_INNER_RE.match(token)
    return int(match.group(2)) if match else 10**9


class EntityMap:
    """
    Накапливает соответствия оригинал ↔ токен в рамках одной сессии
    (загрузка заметки или один RAG-запрос), чтобы одно и то же имя
    всегда маскировалось как [ИМЯ_1], а не получало новый номер.
    """

    def __init__(self) -> None:
        self.orig_to_token: dict[str, str] = {}
        self.token_to_orig: dict[str, str] = {}
        self.type_counter: dict[str, int] = {}
        self.entities: list[Entity] = []
        self._token_type: dict[str, str] = {}

    def apply_known(self, text: str) -> str:
        if not text or not self.orig_to_token:
            return text
        items = sorted(self.orig_to_token.items(), key=lambda x: len(x[0]), reverse=True)
        for original, token in items:
            orig = (original or "").strip()
            if not orig or orig.startswith('['):
                continue
            pattern = rf'(?<![{_WORD_CHARS}]){re.escape(orig)}(?![{_WORD_CHARS}])'
            text = re.sub(pattern, token, text, flags=re.IGNORECASE)
        return text

    def remember(self, entity: Entity) -> None:
        if not entity.original:
            return
        if entity.original not in self.orig_to_token:
            self.orig_to_token[entity.original] = entity.token
            self.entities.append(entity)
        self.token_to_orig[entity.token] = entity.original
        self._token_type[entity.token] = entity.entity_type
        if entity.fake_value and entity.fake_value != entity.token:
            self.token_to_orig[entity.fake_value] = entity.original
        match = _TOKEN_INNER_RE.match(entity.token)
        if match:
            entity_type, idx = match.group(1), int(match.group(2))
            self.type_counter[entity_type] = max(self.type_counter.get(entity_type, 0), idx)

    def unify_similar(self) -> dict[str, str]:
        """
        Сводит падежи и короткие формы к одному токену:
        Иван Петров / Ивана Петрова / Петров → [ИМЯ_1], если это однозначно.
        """
        from collections import defaultdict
        groups: dict[tuple[str, tuple[str, ...]], list[tuple[str, str]]] = defaultdict(list)
        for original, token in self.orig_to_token.items():
            etype = self._token_type.get(token, '')
            if etype not in _NAME_ENTITY_TYPES:
                continue
            stems = _name_stems(original)
            if not stems:
                continue
            groups[(etype, stems)].append((original, token))

        full_keys = [k for k in groups if len(k[1]) >= 2]
        for key, members in list(groups.items()):
            etype, stems = key
            if len(stems) != 1:
                continue
            hits = [fk for fk in full_keys if fk[0] == etype and stems[0] in fk[1]]
            if len(hits) == 1:
                groups[hits[0]].extend(members)
                del groups[key]

        replacements: dict[str, str] = {}
        for members in groups.values():
            if not members:
                continue
            tokens = list(dict.fromkeys(t for _, t in members))
            canon_token = min(tokens, key=_token_index)
            canon_original = min((o for o, _ in members), key=lambda s: (len(s), s))
            for original, token in members:
                self.orig_to_token[original] = canon_token
                self.token_to_orig[token] = canon_original
            self.token_to_orig[canon_token] = canon_original
            for token in tokens:
                if token != canon_token:
                    replacements[token] = canon_token
        return replacements

    def canonicalize_text(self, text: str) -> str:
        replacements = self.unify_similar()
        for old, new in sorted(replacements.items(), key=lambda x: len(x[0]), reverse=True):
            text = text.replace(old, new)
        return self.apply_known(text)

    def unmask(self, text: str) -> str:
        """Вернуть исходные значения вместо токенов (и типичных артефактов LLM)."""
        if not text or not self.token_to_orig:
            return text
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for token, original in self.token_to_orig.items():
            variants = [token]
            inner = token[1:-1] if token.startswith('[') and token.endswith(']') else token
            variants.extend([
                inner,
                inner.lower(),
                inner.replace('_', ' '),
                f'[{inner.replace("_", " ")}]',
            ])
            for src in variants:
                if src and src not in seen:
                    seen.add(src)
                    pairs.append((src, original))
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        restored = text
        for src, original in pairs:
            restored = restored.replace(src, original)
        return restored


def unmask_card(card, entity_map: EntityMap):
    """Восстановить поля zettel-карточки после атомайзера."""
    card.content = entity_map.unmask(card.content or "")
    card.topic = entity_map.unmask(card.topic or "")
    if card.parent_hint:
        card.parent_hint = entity_map.unmask(card.parent_hint)
    card.tags = [entity_map.unmask(t) for t in (card.tags or [])]
    return card


# анонимизатор
class Anonymizer:
    """
    Двухслойный анонимизатор для русского языка.

    Слой 1 — Regex:  телефоны, email, ИНН, СНИЛС, карты, даты и т.д.
    Слой 2 — Natasha NER: имена, организации, адреса
    """

    def __init__(
        self,
        use_ner:           bool = True,
        use_fake_values:   bool = False,   # True=подставить фейк, False=токен [ИМЯ_1]
        # False, чтобы не зависеть от падежей (не генерируем новое имя и тд а просто заменяем на [ИМЯ_1],[ОРГАНИЗАЦИЯ_2] и тд)
        mask_dates:        bool = False,
        mask_urls:         bool = True,
        mask_ip:           bool = True,
    ):
        self.use_ner         = use_ner
        self.use_fake_values = use_fake_values
        self.mask_dates      = mask_dates
        self.mask_urls       = mask_urls
        self.mask_ip         = mask_ip

        # Собираем активные паттерны
        self._active_patterns = {
            k: v for k, v in REGEX_PATTERNS.items()
            if self._is_active(k)
        }

        if use_ner:
            self._init_natasha()

        self._lock = threading.Lock()

    def _is_active(self, entity_type: str) -> bool:
        if entity_type == 'ДАТА' and not self.mask_dates:
            return False
        if entity_type == 'URL' and not self.mask_urls:
            return False
        if entity_type == 'IP' and not self.mask_ip:
            return False
        return True

    def _init_natasha(self):
        self.segmenter = Segmenter()
        emb = NewsEmbedding()
        self.ner_tagger = NewsNERTagger(emb)

    # API
    def anonymize(self, text: str, counter: Optional[dict] = None) -> AnonymizationResult:
        """
        Анонимизировать текст.
        Возвращает AnonymizationResult с замаскированным текстом и маппингом.
        counter — опциональный счётчик типов сущностей, чтобы продолжить нумерацию
        токенов ([ИМЯ_3] после уже выданных [ИМЯ_1], [ИМЯ_2]).
        """
        session_id = str(uuid.uuid4())
        entities: list[Entity] = []
        if counter is None:
            counter = {}

        with self._lock:
            text_after_hard, entities_hard = self._hardcode_pass(text, counter)
            entities.extend(entities_hard)

            text_after_regex, entities_regex = self._regex_pass(text_after_hard, counter)
            entities.extend(entities_regex)

            if self.use_ner:
                text_final, entities_ner = self._ner_pass(text_after_regex, counter)
                entities.extend(entities_ner)
            else:
                text_final = text_after_regex

        return AnonymizationResult(
            session_id=session_id,
            original_text=text,
            anonymized_text=text_final,
            entities=entities,
        )

    def mask(self, text: str, entity_map: Optional[EntityMap] = None) -> str:
        """
        Замаскировать текст, переиспользуя токены из entity_map.
        Новые сущности дописываются в ту же карту.
        """
        if not text:
            return text
        emap = entity_map or EntityMap()
        prepared = emap.apply_known(text)
        result = self.anonymize(prepared, counter=emap.type_counter)
        for entity in result.entities:
            emap.remember(entity)
        self._bind_hardcode_aliases(emap, result.entities)
        return emap.canonicalize_text(result.anonymized_text)

    def unmask(self, text: str, entity_map: EntityMap) -> str:
        return entity_map.unmask(text)

    def deanonymize(self, anonymized_text: str, result: AnonymizationResult) -> str:
        """
        Восстановить исходный текст из анонимизированного
        по сохранённому AnonymizationResult.
        """
        restored = anonymized_text
        # Сортируем по длине токена (длинные сначала) — избегаем частичных замен
        sorted_entities = sorted(result.entities, key=lambda e: len(e.token), reverse=True)
        for entity in sorted_entities:
            restored = restored.replace(entity.fake_value, entity.original)
            restored = restored.replace(entity.token, entity.original)
        return restored

    def deanonymize_from_json(self, anonymized_text: str, json_path: str) -> str:
        """
        Восстановить текст, используя сохранённый JSON-файл.
        """
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        restored = anonymized_text
        entities = data.get('entities', [])
        entities_sorted = sorted(entities, key=lambda e: len(e['token']), reverse=True)

        for entity in entities_sorted:
            restored = restored.replace(entity['fake_value'], entity['original'])
            restored = restored.replace(entity['token'],      entity['original'])

        return restored

    # ВНУТРЕННИЕ МЕТОДЫ
    def _make_replacement(self, entity_type: str, token: str) -> str:
        """Определить, чем заменить: фейком или токеном"""
        if self.use_fake_values and entity_type in FAKE_GENERATORS:
            return FAKE_GENERATORS[entity_type]()
        return token

    def _generate_token(self, entity_type: str, counter: dict) -> str:
        counter[entity_type] = counter.get(entity_type, 0) + 1
        return f'[{entity_type}_{counter[entity_type]}]'

    def _bind_hardcode_aliases(self, emap: EntityMap, entities: list[Entity]) -> None:
        """Все написания МТС в сессии ведут на один токен организации."""
        token = None
        for entity in entities:
            if entity.source == 'hardcode':
                token = entity.token
                break
        if not token:
            for alias in HARDCODE_ORG_ALIASES:
                if alias in emap.orig_to_token:
                    token = emap.orig_to_token[alias]
                    break
        if not token:
            return
        for alias in HARDCODE_ORG_ALIASES:
            if alias not in emap.orig_to_token:
                emap.orig_to_token[alias] = token

    def _hardcode_pass(
        self,
        text: str,
        counter: dict,
    ) -> tuple[str, list[Entity]]:
        """Костыльная маскировка фиксированных брендов (МТС и вариации)."""
        entities: list[Entity] = []
        all_matches: list[tuple[int, int, str, str]] = []

        for pattern in HARDCODE_ORG_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                original = m.group()
                if self._is_token_like(original) or self._overlaps_existing_token(text, m.start(), m.end()):
                    continue
                all_matches.append((m.start(), m.end(), original, 'ОРГАНИЗАЦИЯ'))

        all_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        filtered: list[tuple[int, int, str, str]] = []
        last_end = -1
        for match in all_matches:
            if match[0] >= last_end:
                filtered.append(match)
                last_end = match[1]

        shared_token = None
        shared_replacement = None
        seen: dict[str, Entity] = {}
        for start, end, original, entity_type in reversed(filtered):
            if shared_token is None:
                shared_token = self._generate_token(entity_type, counter)
                shared_replacement = self._make_replacement(entity_type, shared_token)
            if original not in seen:
                entity = Entity(
                    token=shared_token,
                    entity_type=entity_type,
                    original=original,
                    fake_value=shared_replacement,
                    start=start,
                    end=end,
                    source='hardcode',
                )
                seen[original] = entity
                entities.append(entity)
            text = text[:start] + shared_replacement + text[end:]

        return text, entities

    @staticmethod
    def _is_token_like(original: str) -> bool:
        value = original.strip()
        return bool(re.match(r'^\[?[^\[\]\s]+_\d+\]?$', value))

    @staticmethod
    def _overlaps_existing_token(text: str, start: int, end: int) -> bool:
        for match in re.finditer(r'\[[^\[\]\s]+_\d+\]', text):
            if start < match.end() and end > match.start():
                return True
        return False

    def _regex_pass(
        self,
        text: str,
        counter: dict,
    ) -> tuple[str, list[Entity]]:
        """Первый проход: замена по regex"""
        entities: list[Entity] = []

        # original_value -> Entity (дедупликация)
        seen: dict[str, Entity] = {}

        # Собираем все совпадения
        all_matches: list[tuple[int, int, str, str]] = []  # start, end, original, type

        for entity_type, patterns in self._active_patterns.items():
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.IGNORECASE):
                    original = m.group()
                    if self._is_token_like(original) or self._overlaps_existing_token(text, m.start(), m.end()):
                        continue
                    all_matches.append((m.start(), m.end(), original, entity_type))

        # Сортируем: сначала по позиции (с конца), при совпадении — длиннее вперёд
        all_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

        # Убираем перекрывающиеся совпадения (жадно, первое выигрывает)
        filtered: list[tuple[int, int, str, str]] = []
        last_end = -1
        for match in all_matches:
            if match[0] >= last_end:
                filtered.append(match)
                last_end = match[1]

        # Заменяем с конца текста
        for start, end, original, entity_type in reversed(filtered):
            if original in seen:
                entity = seen[original]
                replacement = entity.fake_value if self.use_fake_values else entity.token
            else:
                token = self._generate_token(entity_type, counter)
                replacement = self._make_replacement(entity_type, token)
                entity = Entity(
                    token=token,
                    entity_type=entity_type,
                    original=original,
                    fake_value=replacement,
                    start=start,
                    end=end,
                    source='regex',
                )
                seen[original] = entity
                entities.append(entity)

            text = text[:start] + replacement + text[end:]

        return text, entities

    def _ner_pass(
        self,
        text: str,
        counter: dict,
    ) -> tuple[str, list[Entity]]:
        """Второй проход: NER через Natasha"""
        if len(text) > 16000:
            parts: list[str] = []
            entities: list[Entity] = []
            i = 0
            while i < len(text):
                end = min(i + 16000, len(text))
                if end < len(text):
                    cut = text.rfind("\n", i + 8000, end)
                    if cut <= i:
                        cut = text.rfind(". ", i + 8000, end)
                        end = cut + 1 if cut > i else end
                    else:
                        end = cut
                masked, ents = self._ner_pass(text[i:end], counter)
                parts.append(masked)
                entities.extend(ents)
                i = end
            return "".join(parts), entities

        entities: list[Entity] = []
        seen: dict[str, Entity] = {}

        try:
            doc = Doc(text)
        except MemoryError:
            return text, []
        try:
            doc.segment(self.segmenter)
            doc.tag_ner(self.ner_tagger)
        except MemoryError:
            return text, []
        # doc.sents - разбивает по предложения 
        # doc.tokens - разбивает по словам 
        # doc.spans - разбивает по сущностям (org, per, loc)


        spans = [
            (span.start, span.stop, span.text, NER_TYPE_MAP[span.type])
            for span in doc.spans
            if span.type in NER_TYPE_MAP
        ]

        # С конца
        for start, stop, original, entity_type in sorted(spans, key=lambda x: x[0], reverse=True):
            if self._is_token_like(original) or self._overlaps_existing_token(text, start, stop):
                continue

            if original in seen:
                entity = seen[original]
                replacement = entity.fake_value if self.use_fake_values else entity.token
            else:
                token = self._generate_token(entity_type, counter)
                replacement = self._make_replacement(entity_type, token)
                entity = Entity(
                    token=token,
                    entity_type=entity_type,
                    original=original,
                    fake_value=replacement,
                    start=start,
                    end=stop,
                    source='ner',
                )
                seen[original] = entity
                entities.append(entity)

            text = text[:start] + replacement + text[stop:]

        return text, entities


def save_anomyzed_result(
    result: AnonymizationResult,
    output_dir: str = '.',
    filename: Optional[str] = None,   # ← уже есть этот параметр
) -> tuple[str, str]:
    """
    Сохраняет:
      - JSON с маппингом сущностей
      - TXT с анонимизированным текстом

    Возвращает пути к файлам (json_path, txt_path)
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    base_name = filename or result.session_id

    json_path = output_path / f'{base_name}_mapping.json'
    txt_path  = output_path / f'{base_name}_anonymized.txt'

    # JSON
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)

    # сохраняем анонимизированный текст в TXT
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(result.anonymized_text)

    print(f'[✓] JSON маппинг: {json_path}')
    print(f'[✓] Анонимизированный текст: {txt_path}')

    return str(json_path), str(txt_path)


def load_result_from_json(json_path: str) -> AnonymizationResult:
    """Восстановить AnonymizationResult из JSON"""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    entities = [
        Entity(
            token=e['token'],
            entity_type=e['entity_type'],
            original=e['original'],
            fake_value=e['fake_value'],
            start=e['start'],
            end=e['end'],
            source=e['source'],
        )
        for e in data['entities']
    ]

    return AnonymizationResult(
        session_id=data['session_id'],
        original_text=data['original_text'],
        anonymized_text=data['anonymized_text'],
        entities=entities,
        created_at=data['created_at'],
    )



# anonymizer = Anonymizer(
#         use_ner=True,
#         use_fake_values=False,   # заменяем реалистичными фейками или обычными сущностями
#         mask_dates=True,
#         mask_urls=True,
#         mask_ip=True,
#     )

# # обезличивание конф данных через regex с faker и через  NER natasha для русского текста
# result = anonymizer.anonymize(text)
# # print(result.anonymized_text)
# json_path, txt_path = save_anomyzed_result(result, output_dir='./output')

# # обратная деанонимизация из объекта 
# restored_text = anonymizer.deanonymize(result.anonymized_text, result)
# # print(restored_text)

# restored_from_file = anonymizer.deanonymize_from_json(
#         anonymized_text=result.anonymized_text,
#         json_path=json_path,
# )