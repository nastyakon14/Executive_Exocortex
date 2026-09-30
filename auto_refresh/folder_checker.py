# сверка файлов в отслеживаемых директориях
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from app.handlers.folders_mac import EXTRACTABLE_EXTENSIONS, list_folder_files
except ImportError:
    from folders_mac import EXTRACTABLE_EXTENSIONS, list_folder_files


def as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def file_mtime_utc(path: str) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)


def parse_watch_state(raw) -> dict:
    """Хэши файлов плюс очереди: pending догружаются, excluded больше не отслеживаются."""
    empty = {"files": {}, "pending": [], "excluded": []}
    if not raw:
        return empty
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except Exception:
            return empty
    if not isinstance(data, dict):
        return empty
    if any(key in data for key in ("files", "pending", "excluded")):
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        pending = [str(item) for item in (data.get("pending") or [])]
        excluded = [str(item) for item in (data.get("excluded") or [])]
        return {"files": dict(files), "pending": pending, "excluded": excluded}
    return {"files": dict(data), "pending": [], "excluded": []}


def dump_watch_state(files: dict, pending: list, excluded: list) -> str:
    return json.dumps(
        {"files": files, "pending": list(pending), "excluded": list(excluded)},
        ensure_ascii=False,
    )


def folder_hashes(raw) -> dict:
    return parse_watch_state(raw)["files"]


def list_watch_files(folder_path: str, extract_child: bool) -> tuple[list[str], str | None]:
    files, err = list_folder_files(folder_path, extract_child_content=bool(extract_child))
    if err and "нет поддерживаемых" in (err or ""):
        return [], None
    if err:
        return [], err
    return [os.path.abspath(p) for p in files], None


def check_folder_changes(source: dict) -> dict:
    """
    Возвращает {files, stale, error}.
    В files попадают пути новее last_synced_at и пути из очереди догрузки.
    Отменённые пути не попадают никогда.
    Если даты обновления ещё нет, берётся вся папка, кроме отменённых.
    stale — source_input в графе, которых уже нет на диске.
    """
    folder = os.path.abspath(os.path.expanduser(source.get("source_path") or ""))
    extract_child = bool(source.get("extract_child"))
    files, err = list_watch_files(folder, extract_child)
    if err:
        return {"files": [], "stale": [], "error": err}

    synced = as_utc(source.get("last_synced_at"))
    state = parse_watch_state(source.get("content_hash"))
    pending = set(state["pending"])
    excluded = set(state["excluded"])
    changed = []
    for path in files:
        if path in excluded:
            continue
        try:
            mtime = file_mtime_utc(path)
        except OSError:
            continue
        if path in pending or synced is None or mtime > synced:
            changed.append(path)

    prefix = folder if folder.endswith(os.sep) else folder + os.sep
    stale = []
    try:
        from web_app import linker
        stored = linker.repository.list_source_inputs(source["graph_id"], prefix=prefix)
        current = set(files)
        for src in stored:
            abs_src = os.path.abspath(src)
            if abs_src not in current and (abs_src == folder or abs_src.startswith(prefix)):
                if Path(abs_src).suffix.lower() in EXTRACTABLE_EXTENSIONS:
                    stale.append(abs_src)
    except Exception as e:
        print(f"[auto_refresh] stale scan warning: {e}")

    return {"files": changed, "stale": stale, "error": None}
