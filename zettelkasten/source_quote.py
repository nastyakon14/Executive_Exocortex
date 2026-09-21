from __future__ import annotations

import re
from typing import Optional


_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{2,}")


def find_source_quote(source_text: str, thought: str, max_chars: int = 420) -> str:
    """
    Находит в исходном тексте абзац, из которого выросла мысль.
    Если совпадения нет — возвращает саму мысль, обрезанную до max_chars.
    """
    source_text = (source_text or "").strip()
    thought = (thought or "").strip()
    if not thought:
        return ""
    if not source_text:
        return _clip(thought, max_chars)

    thought_words = set(_WORD_RE.findall(thought.lower()))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", source_text) if p.strip()]
    if not paragraphs:
        paragraphs = [source_text]

    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars * 2:
            pieces.append(paragraph)
        else:
            pieces.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip())

    best = ""
    best_score = 0.0
    for piece in pieces:
        words = set(_WORD_RE.findall(piece.lower()))
        if not thought_words or not words:
            continue
        overlap = len(thought_words & words)
        score = overlap / max(len(thought_words), 1)
        if piece.lower() in thought.lower() or thought.lower() in piece.lower():
            score += 0.4
        if score > best_score:
            best_score = score
            best = piece

    if best_score < 0.15:
        return _clip(thought, max_chars)
    return _clip(best, max_chars)


def _clip(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return (cut or text[:max_chars]).rstrip(".,;:") + "…"
