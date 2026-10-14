"""Чтение Outlook .msg: текст письма и вложения из истории переписки."""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import extract_msg


_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00]')
_MSG_DEPTH = 0


def read_msg(file_path: str) -> str:
    """Текст письма и всех вложений, включая вложенные письма переписки."""
    global _MSG_DEPTH
    if _MSG_DEPTH > 8:
        return ""
    path = os.path.abspath(file_path)
    msg = extract_msg.openMsg(path)
    _MSG_DEPTH += 1
    try:
        items: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="outlook_attachments_") as folder:
            _collect_message(msg, items, seen=set(), depth=0, folder=folder)
        items.sort(key=lambda item: item["stamp"])
        parts: list[str] = []
        for item in items:
            letter = (item.get("letter") or "").strip()
            if letter:
                parts.append(letter)
            parts.extend(
                file_text for file_text in (item.get("files") or []) if (file_text or "").strip()
            )
        return "\n\n".join(parts)
    finally:
        _MSG_DEPTH -= 1
        try:
            msg.close()
        except Exception:
            pass


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _message_stamp(msg) -> datetime:
    """Дата письма для сортировки: сначала старые, потом новые."""
    raw = getattr(msg, "date", None)
    if isinstance(raw, datetime):
        return _aware(raw)
    text = str(raw or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return _aware(parsed)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return _aware(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


def _collect_message(msg, items: list[dict], seen: set, depth: int, folder: str) -> None:
    if msg is None or depth > 20 or id(msg) in seen:
        return
    seen.add(id(msg))
    item = {
        "stamp": _message_stamp(msg),
        "letter": _message_text(msg),
        "files": [],
    }
    items.append(item)
    for attachment in list(getattr(msg, "attachments", None) or []):
        nested = _embedded_message(attachment)
        if nested is not None:
            _collect_message(nested, items, seen, depth + 1, folder)
            continue
        name = _attachment_name(attachment)
        if not name or name.startswith("~"):
            continue
        text = _attachment_text(attachment, folder, name)
        if text:
            item["files"].append(f"Вложение «{name}»:\n\n{text}")


def _message_text(msg) -> str:
    sender = getattr(msg, "sender", None) or ""
    subject = getattr(msg, "subject", None) or ""
    date = getattr(msg, "date", None) or ""
    body = (getattr(msg, "body", None) or "").strip()
    if not body:
        body = _html_to_text(getattr(msg, "htmlBody", None))
    lines = []
    if sender:
        lines.append(f"Отправитель: {sender}")
    if subject:
        lines.append(f"Тема: {subject}")
    if date:
        lines.append(f"Дата: {date}")
    header = "\n".join(lines)
    if header and body:
        return f"{header}\n\n{body}"
    return header or body


def _html_to_text(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", str(raw))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _embedded_message(attachment):
    data = getattr(attachment, "data", None)
    if data is None or isinstance(data, (bytes, bytearray)):
        return None
    if hasattr(data, "body") or hasattr(data, "attachments"):
        return data
    return None


def _attachment_name(attachment) -> str:
    raw = (
        getattr(attachment, "longFilename", None)
        or getattr(attachment, "name", None)
        or getattr(attachment, "shortFilename", None)
        or "attachment"
    )
    name = _INVALID_NAME.sub("_", os.path.basename(str(raw))).strip(" .")
    return name[:180] or "attachment"


def _unique_name(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    candidate = name
    n = 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{base}_{n}{ext}"
        n += 1
    return candidate


def _save_attachment(attachment, folder: str, name: str) -> str | None:
    filename = _unique_name(folder, name)
    dest = os.path.join(folder, filename)
    data = getattr(attachment, "data", None)
    if isinstance(data, (bytes, bytearray)) and data:
        with open(dest, "wb") as handle:
            handle.write(data)
        return dest
    try:
        saved = attachment.save(customPath=folder, customFilename=filename)
    except Exception as exc:
        print(f"outlook attachment save warning: {filename}: {exc}")
        return None
    if isinstance(saved, tuple) and len(saved) >= 2 and saved[1]:
        return str(saved[1])
    if os.path.isfile(dest):
        return dest
    return None


def _attachment_text(attachment, folder: str, name: str) -> str:
    """Тот же разбор, что у файлов в папке: формат, размер, извлечение текста."""
    try:
        from app.handlers.folders_mac import EXTRACTABLE_EXTENSIONS, FileTooLargeError, extract_file_text
    except ImportError:
        from folders_mac import EXTRACTABLE_EXTENSIONS, FileTooLargeError, extract_file_text

    ext = os.path.splitext(name)[1].lower()
    if ext not in EXTRACTABLE_EXTENSIONS:
        print(f"outlook attachment skip unsupported: {name}")
        return ""
    saved = _save_attachment(attachment, folder, name)
    if not saved or not os.path.isfile(saved):
        print(f"outlook attachment skip unreadable: {name}")
        return ""
    try:
        return (extract_file_text(saved) or "").strip()
    except FileTooLargeError as exc:
        print(f"outlook attachment skip: {name}: {exc}")
        return ""
    except Exception as exc:
        print(f"outlook attachment skip: {name}: {exc}")
        return ""
