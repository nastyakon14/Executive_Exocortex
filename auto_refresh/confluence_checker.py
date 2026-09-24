# сверка отслеживаемых страниц Confluence
from datetime import datetime, timezone

from app.handlers.confluence import get_confluence_page_version_when

from auto_refresh.folder_checker import as_utc


def check_confluence_change(source: dict) -> dict:
    """
    Возвращает {changed, remote_mtime, error}.
    changed=True если version.when новее last_synced_at.
    """
    url = (source.get("source_path") or "").strip()
    if not url:
        return {"changed": False, "remote_mtime": None, "error": "Пустая ссылка Confluence"}
    try:
        remote = get_confluence_page_version_when(url)
    except Exception as e:
        return {"changed": False, "remote_mtime": None, "error": str(e)}

    if remote.tzinfo is None:
        remote = remote.replace(tzinfo=timezone.utc)
    else:
        remote = remote.astimezone(timezone.utc)

    synced = as_utc(source.get("last_synced_at"))
    return {
        "changed": remote > synced,
        "remote_mtime": remote,
        "error": None,
    }
