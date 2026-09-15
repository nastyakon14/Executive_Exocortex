"""Форматирование ответов LLM для Telegram parse_mode=HTML."""

import re
from html import escape


def prepare_telegram_html(text: str) -> str:
    """
    Приводит ответ LLM к HTML, совместимому с Telegram.
    Если модель вернула Markdown — конвертирует в HTML.
    """
    text = text.strip()
    if not text:
        return text
    if _looks_like_markdown(text):
        text = _markdown_to_telegram_html(text)
    return text


def plain_fallback(text: str) -> str:
    """Убирает HTML-теги и экранирует текст для отправки без parse_mode."""
    plain = re.sub(r"<[^>]+>", "", text)
    return escape(plain)


def _looks_like_markdown(text: str) -> bool:
    if re.search(r"</?(?:b|strong|i|em|u|code|pre)\b", text, re.I):
        return False
    return "**" in text


def _markdown_to_telegram_html(text: str) -> str:
    lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue

        numbered = re.match(r"^(\d+\.)\s*\*\*(.+?)\*\*:?\s*(.*)$", line)
        if numbered:
            num, title, rest = numbered.groups()
            if rest:
                lines.append(f"<b>{num} {title}</b> {rest}")
            else:
                lines.append(f"<b>{num} {title}</b>")
            continue

        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        lines.append(line)
    return "\n".join(lines)
