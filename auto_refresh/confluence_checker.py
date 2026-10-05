# сверка отслеживаемых страниц Confluence
from __future__ import annotations
from datetime import datetime, timezone

from app.handlers.confluence import (
    get_confluence_page_version_info,
    get_page_version_info_by_id,
    list_confluence_pages,
)

from auto_refresh.folder_checker import db_moment, format_stamp, log_date_check, parse_watch_state


def _utc(remote: datetime) -> datetime:
    if remote.tzinfo is None:
        return remote.replace(tzinfo=timezone.utc)
    return remote.astimezone(timezone.utc)


def _check_single_page(url: str, source: dict) -> dict:
    try:
        remote, title = get_confluence_page_version_info(url)
        remote = _utc(remote)
    except Exception as e:
        print(f"[auto_refresh] страница «{url}»: не удалось прочитать дату версии ({e}). пропуск.")
        return {
            "changed": False,
            "remote_mtime": None,
            "error": str(e),
            "extract_child": False,
            "watched": 0,
            "title": url,
        }
    title = title or url

    state = parse_watch_state(source.get("content_hash"), root=url)
    db_at, db_label = db_moment(source)
    if url in set(state["excluded"]):
        print(f"[auto_refresh] страница «{title}»: отменена при загрузке. пропуск.")
        return {
            "changed": False,
            "remote_mtime": remote,
            "error": None,
            "extract_child": False,
            "watched": 0,
            "title": title,
        }
    need = db_at is None or remote > db_at
    log_date_check("страница", title, remote, db_at, need, db_label)
    return {
        "changed": need,
        "remote_mtime": remote,
        "error": None,
        "extract_child": False,
        "watched": 1,
        "title": title,
    }


def _check_child_pages(url: str, source: dict) -> dict:
    pages, err = list_confluence_pages(url, extract_child=True)
    if err:
        return {
            "changed": False,
            "error": err,
            "extract_child": True,
            "pages": [],
            "stale": [],
            "watched": 0,
        }

    state = parse_watch_state(source.get("content_hash"), root=url)
    hashes = state["files"]
    raw_hash = (source.get("content_hash") or "").strip()
    if not hashes and raw_hash and not raw_hash.startswith("{"):
        hashes = {url: raw_hash}
    excluded = set(state["excluded"])

    db_at, db_label = db_moment(source)
    print(
        f"[auto_refresh] страницы Confluence «{url}»: страниц {len(pages)}, "
        f"{db_label} {format_stamp(db_at)}"
    )
    changed_pages = []
    watched = 0
    for page in pages:
        key = page["source_input"]
        title = page.get("title") or key
        if key in excluded:
            print(f"[auto_refresh] страница «{title}»: отменена при загрузке. пропуск.")
            continue
        watched += 1
        try:
            remote, fetched = get_page_version_info_by_id(page["page_id"])
            remote = _utc(remote)
            if fetched:
                title = fetched
                page["title"] = fetched
        except Exception as e:
            print(f"[auto_refresh] страница «{title}»: не удалось прочитать дату версии ({e}). пропуск.")
            continue
        need = db_at is None or remote > db_at
        log_date_check("страница", str(title), remote, db_at, need, db_label)
        if need:
            changed_pages.append(page)

    current = {page["source_input"] for page in pages}
    stale = [key for key in hashes if key not in current]
    for key in stale:
        print(f"[auto_refresh] страница «{key}»: в Confluence больше нет. удаление из графа.")
    print(
        f"[auto_refresh] страницы Confluence «{url}»: "
        f"к обновлению {len(changed_pages)}, удалить из графа {len(stale)}"
    )
    return {
        "changed": bool(changed_pages or stale),
        "error": None,
        "extract_child": True,
        "pages": changed_pages,
        "stale": stale,
        "watched": watched,
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
