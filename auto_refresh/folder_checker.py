# сверка файлов в отслеживаемых директориях
from __future__ import annotations
import json
import os
from datetime import datetime, timedelta, timezone
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


def format_stamp(value: datetime | None) -> str:
    """Дата для лога в московском времени, как её видит пользователь."""
    if value is None:
        return "нет"
    moscow = timezone(timedelta(hours=3))
    return value.astimezone(moscow).strftime("%d.%m.%Y %H:%M")


def db_moment(source: dict) -> tuple[datetime | None, str]:
    """Дата последней сверки в базе, а если сверки ещё не было — дата занесения источника."""
    synced = as_utc(source.get("last_synced_at"))
    if synced is not None:
        return synced, "дата последнего обновления в базе"
    return as_utc(source.get("created_at")), "дата занесения в базу"


def log_date_check(
    kind: str,
    name: str,
    changed_at: datetime | None,
    db_at: datetime | None,
    need: bool,
    db_label: str = "дата последнего обновления в базе",
) -> None:
    decision = "обновление требуется" if need else "обновление не требуется"
    action = "запуск обновления" if need else "пропуск"
    change_label = "дата последнего изменения файла" if kind == "файл" else "дата последнего изменения страницы"
    print(
        f"[auto_refresh] {kind} «{name}»: "
        f"{change_label} {format_stamp(changed_at)}, "
        f"{db_label} {format_stamp(db_at)}. "
        f"{decision}. {action}."
    )


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


def file_meta(raw) -> tuple[str, datetime | None]:
    """Хэш и mtime прошлой успешной сверки. Старый формат — просто строка хэша."""
    if isinstance(raw, dict):
        digest = str(raw.get("hash") or "")
        stamp = raw.get("mtime")
        if isinstance(stamp, (int, float)):
            return digest, datetime.fromtimestamp(float(stamp), tz=timezone.utc)
        return digest, None
    if isinstance(raw, str):
        return raw, None
    return "", None


def file_needs_extract(path: str, synced: datetime | None, mtime: datetime | None = None) -> bool:
    """Открываем файл только если его дата изменения новее даты в базе.

    В synced передаётся дата последней сверки, а если её ещё нет — дата занесения источника.
    """
    if synced is None:
        return True
    if mtime is None:
        mtime = file_mtime_utc(path)
    return mtime > synced


def list_watch_files(folder_path: str, extract_child: bool) -> tuple[list[str], str | None]:
    files, err = list_folder_files(folder_path, extract_child_content=bool(extract_child))
    if err and "нет поддерживаемых" in (err or ""):
        return [], None
    if err:
        return [], err
    return [os.path.abspath(p) for p in files], None


def check_folder_changes(source: dict) -> dict:
    """
    Возвращает {files, unchanged, stale, error}.
    В files только пути новее даты последней сверки.
    Если дата обновления позже даты изменения, файл не открывается.
    Отменённые пути не попадают никогда.
    unchanged — без изменений, их не надо читать.
    stale — source_input в графе, которых уже нет на диске.
    """
    folder = os.path.abspath(os.path.expanduser(source.get("source_path") or ""))
    extract_child = bool(source.get("extract_child"))
    files, err = list_watch_files(folder, extract_child)
    if err:
        return {"files": [], "unchanged": [], "stale": [], "error": err}

    db_at, db_label = db_moment(source)
    state = parse_watch_state(source.get("content_hash"))
    excluded = set()
    for raw in state["excluded"]:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            excluded.add(os.path.abspath(os.path.expanduser(text)))
        except Exception:
            excluded.add(text)
    changed = []
    unchanged = []
    print(
        f"[auto_refresh] папка «{folder}»: файлов {len(files)}, "
        f"{db_label} {format_stamp(db_at)}"
    )
    for path in files:
        name = os.path.basename(path)
        if path in excluded:
            print(f"[auto_refresh] файл «{name}»: отменён при загрузке. пропуск.")
            continue
        try:
            mtime = file_mtime_utc(path)
        except OSError as e:
            print(f"[auto_refresh] файл «{name}»: не удалось прочитать дату изменения ({e}). пропуск.")
            continue
        need = file_needs_extract(path, db_at, mtime)
        log_date_check("файл", name, mtime, db_at, need, db_label)
        if need:
            changed.append(path)
        else:
            unchanged.append(path)

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
                    print(f"[auto_refresh] файл «{os.path.basename(abs_src)}»: на диске больше нет. удаление из графа.")
    except Exception as e:
        print(f"[auto_refresh] stale scan warning: {e}")

    return {
        "files": changed,
        "unchanged": unchanged,
        "stale": stale,
        "error": None,
        "watched": len(changed) + len(unchanged),
    }
