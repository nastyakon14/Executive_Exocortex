# сверка отслеживаемых страниц Confluence
from __future__ import annotations
from datetime import datetime, timezone

from app.handlers.confluence import (
    get_confluence_page_version_when,
    get_page_version_when_by_id,
    list_confluence_pages,
)

from auto_refresh.folder_checker import as_utc, folder_hashes


def _utc(remote: datetime) -> datetime:
    if remote.tzinfo is None:
        return remote.replace(tzinfo=timezone.utc)
    return remote.astimezone(timezone.utc)


def _check_single_page(url: str, source: dict) -> dict:
    try:
        remote = _utc(get_confluence_page_version_when(url))
    except Exception as e:
        return {"changed": False, "remote_mtime": None, "error": str(e), "extract_child": False}

    synced = as_utc(source.get("last_synced_at"))
    has_hash = bool((source.get("content_hash") or "").strip())
    if synced is None or not has_hash:
        print(f"[auto_refresh] confluence needs body check {url}")
        return {
            "changed": True,
            "remote_mtime": remote,
            "error": None,
            "extract_child": False,
        }
    changed = remote > synced
    print(
        f"[auto_refresh] confluence check changed={changed} "
        f"remote={remote.isoformat()} synced={synced.isoformat()}"
    )
    return {
        "changed": changed,
        "remote_mtime": remote,
        "error": None,
        "extract_child": False,
    }


def _check_child_pages(url: str, source: dict) -> dict:
    pages, err = list_confluence_pages(url, extract_child=True)
    if err:
        return {"changed": False, "error": err, "extract_child": True, "pages": [], "stale": []}

    hashes = folder_hashes(source.get("content_hash"))
    raw_hash = (source.get("content_hash") or "").strip()
    if not hashes and raw_hash and not raw_hash.startswith("{"):
        hashes = {url: raw_hash}

    synced = as_utc(source.get("last_synced_at"))
    changed_pages = []
    for page in pages:
        key = page["source_input"]
        try:
            remote = _utc(get_page_version_when_by_id(page["page_id"]))
        except Exception as e:
            changed_pages.append({**page, "error": str(e)})
            continue
        newer = synced is not None and remote > synced
        if synced is None or key not in hashes or newer:
            changed_pages.append(page)

    current = {page["source_input"] for page in pages}
    stale = [key for key in hashes if key not in current]
    print(
        f"[auto_refresh] confluence children url={url} "
        f"pages={len(pages)} changed={len(changed_pages)} stale={len(stale)}"
    )
    return {
        "changed": bool(changed_pages or stale),
        "error": None,
        "extract_child": True,
        "pages": changed_pages,
        "stale": stale,
    }


def check_confluence_change(source: dict) -> dict:
    """
    Возвращает {changed, remote_mtime, error} для одной страницы.
    Если включены дочерние страницы — ещё pages и stale.
    """
    url = (source.get("source_path") or "").strip()
    if not url:
        return {"changed": False, "remote_mtime": None, "error": "Пустая ссылка Confluence", "extract_child": False}
    if source.get("extract_child"):
        return _check_child_pages(url, source)
    return _check_single_page(url, source)
