# сверка файлов в отслеживаемых директориях
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


def folder_hashes(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
    files — пути, у которых mtime новее last_synced_at, либо ещё нет хеша.
    stale — source_input в графе, которых уже нет на диске.
    """
    folder = os.path.abspath(os.path.expanduser(source.get("source_path") or ""))
    extract_child = bool(source.get("extract_child"))
    files, err = list_watch_files(folder, extract_child)
    if err:
        return {"files": [], "stale": [], "error": err}

    synced = as_utc(source.get("last_synced_at"))
    hashes = folder_hashes(source.get("content_hash"))
    changed = []
    for path in files:
        try:
            newer = synced is not None and file_mtime_utc(path) > synced
        except OSError:
            continue
        if newer or (hashes and path not in hashes):
            changed.append(path)

    prefix = folder if folder.endswith(os.sep) else folder + os.sep
    stale = []
    try:
        from web_app_v2 import linker
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
