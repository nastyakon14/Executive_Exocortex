# оркестратор auto-refresh: сверка → тот же ingest-пайплайн → метка в Postgres
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from app.handlers.confluence import get_confluence_page_content, is_confluence_fetch_error, page_plain_text
from app.handlers.folders_mac import FileTooLargeError, extract_file_text, is_temp_open_file
from storage.postgres.db_connect import (
    delete_ingest_digest,
    get_ingest_digest,
    list_watch_sources,
    mark_watch_synced,
    upsert_ingest_digest,
)

from auto_refresh.confluence_checker import check_confluence_change
from auto_refresh.folder_checker import (
    check_folder_changes,
    db_moment,
    dump_watch_state,
    file_meta,
    file_mtime_utc,
    file_needs_extract,
    log_date_check,
    parse_watch_state,
)


_scheduler_started = False


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _same_source_text(graph_id: str, source_input: str, digest: str, stored_hash: str = "") -> bool:
    """Пропуск только если не изменился текст этого пути или URL, не чужого источника."""
    if not digest:
        return False
    if stored_hash and digest == stored_hash:
        return True
    try:
        prev = get_ingest_digest(graph_id, source_input)
    except Exception as e:
        print(f"[auto_refresh] digest lookup warning: {e}")
        return False
    return bool(prev and prev == digest)


def _drop_source(graph_id: str, source_input: str) -> None:
    from web_app import linker

    linker.repository.delete_by_source_input(graph_id, source_input)
    try:
        delete_ingest_digest(graph_id, source_input)
    except Exception as e:
        print(f"[auto_refresh] digest delete warning: {e}")


def _ingest_source(slug: str, graph_id: str, text: str, source_input: str, log_text: str) -> tuple[bool, str]:
    from web_app import _ingest_run_lock, ingest_replacing, log_event, set_ingest_phase

    with _ingest_run_lock:
        set_ingest_phase(slug, "refreshing")
        ok, ans = ingest_replacing(graph_id, text, source_input)
    if ok:
        try:
            upsert_ingest_digest(graph_id, source_input, _text_hash(text))
        except Exception as e:
            print(f"[auto_refresh] digest save warning: {e}")
    log_event(slug, log_text, "auto_refresh", ans)
    return ok, ans


def _guard_ingest(slug: str, detail: str = "") -> None:
    from web_app import ingest_checkpoint, note_live
    ingest_checkpoint(slug)
    if detail:
        note_live(slug, "refreshing", "Автообновление графа", detail)


def _apply_folder(source: dict, slug: str, probe: dict) -> tuple[int, int, int, str]:
    if probe.get("error"):
        mark_watch_synced(source["id"], error=probe["error"], synced=False)
        return 0, 0, 1, probe["error"]

    graph_id = source["graph_id"]
    state = parse_watch_state(source.get("content_hash"), root=source.get("source_path") or "")
    hashes = dict(state["files"])
    pending = set(state["pending"])
    excluded = set(state["excluded"])
    db_at, _db_label = db_moment(source)
    updated = skipped = failed = 0
    last_err = ""

    from web_app import _ingest_run_lock

    for stale in probe.get("stale") or []:
        try:
            with _ingest_run_lock:
                _drop_source(graph_id, stale)
            hashes.pop(stale, None)
            hashes.pop(os.path.abspath(stale), None)
            updated += 1
        except Exception as e:
            failed += 1
            last_err = str(e)

    for path in probe.get("unchanged") or []:
        pending.discard(path)
        prev, _stored = file_meta(hashes.get(path))
        if prev:
            hashes[path] = prev
        skipped += 1

    for path in probe.get("files") or []:
        try:
            if path in excluded:
                continue
            try:
                mtime = file_mtime_utc(path)
            except OSError:
                continue
            if not file_needs_extract(path, db_at, mtime):
                pending.discard(path)
                skipped += 1
                print(f"[auto_refresh] файл «{os.path.basename(path)}»: пропуск, чтение не запускается.")
                continue
            name = os.path.basename(path)
            print(f"[auto_refresh] файл «{name}»: запуск чтения.")
            _guard_ingest(slug, name)
            text = extract_file_text(path)
            digest = _text_hash(text)
            if _same_source_text(graph_id, path, digest, file_meta(hashes.get(path))[0]):
                hashes[path] = digest
                pending.discard(path)
                skipped += 1
                print(f"[auto_refresh] файл «{os.path.basename(path)}»: текст не изменился, запись в граф пропущена.")
                continue
            if not (text or "").strip():
                print(f"[auto_refresh] файл «{os.path.basename(path)}»: текст пустой, старые карточки удаляются.")
                with _ingest_run_lock:
                    _drop_source(graph_id, path)
                hashes[path] = digest
                pending.discard(path)
                updated += 1
                continue
            print(f"[auto_refresh] файл «{os.path.basename(path)}»: запуск обновления графа.")
            ok, ans = _ingest_source(slug, graph_id, text, path, f"[auto] {os.path.basename(path)}")
            if ok:
                hashes[path] = digest
                pending.discard(path)
                updated += 1
                print(f"[auto_refresh] файл «{os.path.basename(path)}»: обновление завершено.")
            else:
                hashes.pop(path, None)
                failed += 1
                last_err = ans
                print(f"[auto_refresh] файл «{os.path.basename(path)}»: обновление не удалось. {ans}")
        except FileTooLargeError as e:
            hashes.pop(path, None)
            failed += 1
            last_err = str(e)
        except Exception as e:
            if type(e).__name__ == "IngestCancelled":
                raise
            hashes.pop(path, None)
            failed += 1
            last_err = str(e)

    if updated == 0 and failed == 0:
        print(f"[auto_refresh] папка: обновлять нечего, пропущено {skipped}.")
        return updated, skipped, failed, last_err
    root = source.get("source_path") or ""
    payload = dump_watch_state(hashes, sorted(pending), sorted(excluded), root=root)
    if failed and updated == 0:
        mark_watch_synced(source["id"], error=last_err, synced=False)
    else:
        mark_watch_synced(
            source["id"],
            content_hash=payload,
            error=last_err if failed else "",
            synced=True,
        )
    return updated, skipped, failed, last_err


def _apply_confluence_children(source: dict, slug: str, probe: dict) -> tuple[int, int, int, str]:
    """Каждая дочерняя страница сверяется и встраивается отдельно, как файл в папке."""
    graph_id = source["graph_id"]
    root = source.get("source_path") or ""
    state = parse_watch_state(source.get("content_hash"), root=root)
    hashes = dict(state["files"])
    pending = set(state["pending"])
    excluded = set(state["excluded"])
    raw_hash = (source.get("content_hash") or "").strip()
    if not hashes and raw_hash and not raw_hash.startswith("{"):
        hashes = {root: raw_hash}

    updated = skipped = failed = 0
    last_err = ""
    if not (probe.get("pages") or probe.get("stale")):
        print("[auto_refresh] страницы Confluence: обновлять нечего, пропуск.")
        return 0, 1, 0, ""
    from web_app import _ingest_run_lock

    for stale in probe.get("stale") or []:
        try:
            with _ingest_run_lock:
                _drop_source(graph_id, stale)
            hashes.pop(stale, None)
            updated += 1
        except Exception as e:
            failed += 1
            last_err = str(e)

    for page in probe.get("pages") or []:
        _guard_ingest(slug, str(page.get("title") or page.get("id") or ""))
        source_input = page.get("source_input") or ""
        if page.get("error"):
            hashes.pop(source_input, None)
            failed += 1
            last_err = page["error"]
            continue
        try:
            print(f"[auto_refresh] страница «{source_input}»: запуск загрузки текста.")
            text = page_plain_text(page["page_id"])
            if is_confluence_fetch_error(text):
                hashes.pop(source_input, None)
                failed += 1
                last_err = text.strip()
                continue
            digest = _text_hash(text)
            if _same_source_text(graph_id, source_input, digest, file_meta(hashes.get(source_input))[0]):
                hashes[source_input] = digest
                pending.discard(source_input)
                skipped += 1
                print(f"[auto_refresh] страница «{source_input}»: текст не изменился, запись в граф пропущена.")
                continue
            if not (text or "").strip():
                with _ingest_run_lock:
                    _drop_source(graph_id, source_input)
                hashes[source_input] = digest
                pending.discard(source_input)
                updated += 1
                continue
            print(f"[auto_refresh] страница «{source_input}»: запуск обновления графа.")
            ok, ans = _ingest_source(slug, graph_id, text, source_input, f"[auto] {source_input}")
            if ok:
                hashes[source_input] = digest
                pending.discard(source_input)
                updated += 1
            else:
                hashes.pop(source_input, None)
                failed += 1
                last_err = ans
        except Exception as e:
            if type(e).__name__ == "IngestCancelled":
                raise
            hashes.pop(source_input, None)
            failed += 1
            last_err = str(e)

    payload = dump_watch_state(hashes, sorted(pending), sorted(excluded), root=root)
    if failed and updated == 0 and skipped == 0:
        mark_watch_synced(source["id"], error=last_err, synced=False)
    else:
        mark_watch_synced(
            source["id"],
            content_hash=payload,
            error=last_err if failed else "",
            synced=True,
        )
    return updated, skipped, failed, last_err


def _apply_confluence(source: dict, slug: str, probe: dict) -> tuple[int, int, int, str]:
    if probe.get("error"):
        mark_watch_synced(source["id"], error=probe["error"], synced=False)
        return 0, 0, 1, probe["error"]
    if probe.get("extract_child"):
        return _apply_confluence_children(source, slug, probe)
    if not probe.get("changed"):
        print(f"[auto_refresh] страница «{source['source_path']}»: пропуск, текст не загружается.")
        return 0, 1, 0, ""

    url = source["source_path"]
    print(f"[auto_refresh] страница «{url}»: запуск загрузки текста.")
    graph_id = source["graph_id"]
    state = parse_watch_state(source.get("content_hash"), root=url)
    stored = file_meta(state["files"].get(url))[0]
    raw_hash = (source.get("content_hash") or "").strip()
    if not stored and raw_hash and not raw_hash.startswith("{"):
        stored = raw_hash
    text = get_confluence_page_content(url)
    if (
        not (text or "").strip()
        or str(text).startswith("Ошибка")
        or str(text).startswith("Отсутствует")
        or str(text).startswith("Произошла ошибка")
    ):
        err = (text or "Не удалось прочитать страницу").strip()
        mark_watch_synced(source["id"], error=err, synced=False)
        return 0, 0, 1, err

    digest = _text_hash(text)
    files = dict(state["files"])
    if stored and url not in files:
        files[url] = stored
    pending = [item for item in state["pending"] if item != url]
    excluded = list(state["excluded"])

    def _keep(page_digest: str) -> None:
        files[url] = page_digest
        mark_watch_synced(
            source["id"],
            content_hash=dump_watch_state(
                files, pending, [item for item in excluded if item != url], root=url,
            ),
            synced=True,
        )

    if _same_source_text(graph_id, url, digest, stored):
        print(f"[auto_refresh] страница «{url}»: текст не изменился, запись в граф пропущена.")
        _keep(digest)
        try:
            upsert_ingest_digest(graph_id, url, digest)
        except Exception as e:
            print(f"[auto_refresh] digest save warning: {e}")
        return 0, 1, 0, ""

    print(f"[auto_refresh] страница «{url}»: запуск обновления графа.")
    ok, ans = _ingest_source(slug, graph_id, text, url, f"[auto] {url}")
    if ok:
        _keep(digest)
        return 1, 0, 0, ""
    mark_watch_synced(source["id"], error=ans, synced=False)
    return 0, 0, 1, ans


def _apply_file(source: dict, slug: str) -> tuple[int, int, int, str]:
    """Один файл, отмеченный галочкой вне папки."""
    path = os.path.abspath(os.path.expanduser(source.get("source_path") or ""))
    name = os.path.basename(path) or path
    if is_temp_open_file(path):
        print(f"[auto_refresh] файл «{name}»: временный файл с префиксом '~'. пропуск.")
        return 0, 1, 0, ""
    if not os.path.isfile(path):
        print(f"[auto_refresh] файл «{name}»: не найден. пропуск.")
        return 0, 0, 1, f"Файл не найден: {name}"
    db_at, db_label = db_moment(source)
    try:
        mtime = file_mtime_utc(path)
    except OSError as e:
        print(f"[auto_refresh] файл «{name}»: не удалось прочитать дату изменения ({e}). пропуск.")
        return 0, 0, 1, str(e)
    need = file_needs_extract(path, db_at, mtime)
    log_date_check("файл", name, mtime, db_at, need, db_label)
    if not need:
        print(f"[auto_refresh] файл «{name}»: пропуск, чтение не запускается.")
        return 0, 1, 0, ""
    print(f"[auto_refresh] файл «{name}»: запуск чтения.")
    graph_id = source["graph_id"]
    try:
        text = extract_file_text(path)
    except Exception as e:
        if type(e).__name__ == "IngestCancelled":
            raise
        mark_watch_synced(source["id"], error=str(e), synced=False)
        return 0, 0, 1, str(e)
    digest = _text_hash(text)
    if _same_source_text(graph_id, path, digest, ""):
        print(f"[auto_refresh] файл «{name}»: текст не изменился, запись в граф пропущена.")
        mark_watch_synced(source["id"], content_hash=digest, synced=True)
        return 0, 1, 0, ""
    if not (text or "").strip():
        print(f"[auto_refresh] файл «{name}»: текст пустой, старые карточки удаляются.")
        from web_app import _ingest_run_lock
        with _ingest_run_lock:
            _drop_source(graph_id, path)
        mark_watch_synced(source["id"], content_hash=digest, synced=True)
        return 1, 0, 0, ""
    print(f"[auto_refresh] файл «{name}»: запуск обновления графа.")
    ok, ans = _ingest_source(slug, graph_id, text, path, f"[auto] {name}")
    if ok:
        mark_watch_synced(source["id"], content_hash=digest, synced=True)
        print(f"[auto_refresh] файл «{name}»: обновление завершено.")
        return 1, 0, 0, ""
    mark_watch_synced(source["id"], error=ans, synced=False)
    print(f"[auto_refresh] файл «{name}»: обновление не удалось. {ans}")
    return 0, 0, 1, ans


def _archived_slugs() -> set[str]:
    try:
        from web_app import _load_projects_unlocked
        return {p["slug"] for p in _load_projects_unlocked() if p.get("archived") and p.get("slug")}
    except Exception:
        return set()


def _page_label(page: dict) -> str:
    return str(page.get("title") or page.get("source_input") or "страница")


def _lone_file_needs_update(source: dict) -> tuple[bool, str]:
    path = os.path.abspath(os.path.expanduser(source.get("source_path") or ""))
    name = os.path.basename(path) or path
    if is_temp_open_file(path):
        return False, name
    if not os.path.isfile(path):
        return False, name
    try:
        mtime = file_mtime_utc(path)
    except OSError:
        return False, name
    db_at, _db_label = db_moment(source)
    return file_needs_extract(path, db_at, mtime), name


def _has_refresh_actions(probes: dict, lone_files: list[dict]) -> bool:
    """Нужно ли запускать фазу обновления графа после проверки дат."""
    for kind, _src, probe in probes.values():
        if probe.get("error"):
            return True
        if kind == "folder":
            if (probe.get("files") or []) or (probe.get("stale") or []):
                return True
            continue
        # Confluence
        if probe.get("extract_child"):
            if (probe.get("pages") or []) or (probe.get("stale") or []):
                return True
        elif probe.get("changed"):
            return True

    for src in lone_files:
        path = os.path.abspath(os.path.expanduser(src.get("source_path") or ""))
        if is_temp_open_file(path):
            continue
        # Отсутствующий файл — это не "без изменений", нужно пройти _apply_file и зафиксировать ошибку.
        if not os.path.isfile(path):
            return True
        needs, _name = _lone_file_needs_update(src)
        if needs:
            return True
    return False


def _publish_refresh_plan(slug: str, probes: dict, lone_files: list[dict]) -> None:
    file_names: list[str] = []
    page_names: list[str] = []
    files_tracked = 0
    pages_tracked = 0
    for kind, src, probe in probes.values():
        if kind == "folder":
            files_tracked += int(probe.get("watched") or 0)
            for path in probe.get("files") or []:
                file_names.append(os.path.basename(path) or path)
            continue
        pages_tracked += int(probe.get("watched") or 0)
        if probe.get("extract_child"):
            for page in probe.get("pages") or []:
                page_names.append(_page_label(page))
        elif probe.get("changed"):
            page_names.append(str(probe.get("title") or src.get("source_path") or "страница"))
    for src in lone_files:
        path = os.path.abspath(os.path.expanduser(src.get("source_path") or ""))
        if is_temp_open_file(path):
            continue
        if not os.path.isfile(path):
            continue
        files_tracked += 1
        needs, name = _lone_file_needs_update(src)
        if needs:
            file_names.append(name)
    summary = {
        "files_tracked": files_tracked,
        "files_update": len(file_names),
        "files_names": file_names[:5],
        "pages_tracked": pages_tracked,
        "pages_update": len(page_names),
        "pages_names": page_names[:5],
    }
    shown_files = ", ".join(summary["files_names"]) if summary["files_names"] else "нет"
    shown_pages = ", ".join(summary["pages_names"]) if summary["pages_names"] else "нет"
    print(
        f"[auto_refresh] файлы: отслеживается {files_tracked}, "
        f"нужно обновить {len(file_names)}. первые названия: {shown_files}"
    )
    print(
        f"[auto_refresh] страницы Confluence: отслеживается {pages_tracked}, "
        f"нужно обновить {len(page_names)}. первые названия: {shown_pages}"
    )
    from web_app import set_refresh_summary
    set_refresh_summary(slug, summary)


def refresh_project(slug: str, manage_status: bool = True) -> dict:
    """Обновляет все watch-источники одного проекта: сначала параллельная сверка, затем ingest."""
    from web_app import begin_ingest, end_ingest

    sources = list_watch_sources(watch_only=True, project_slug=slug)
    if not sources:
        return {"ok": True, "updated": 0, "skipped": 0, "failed": 0, "message": "Нет источников с отслеживанием изменений"}

    if manage_status:
        begin_ingest(slug, "checking")
    from web_app import ensure_refresh_progress
    opened_live = ensure_refresh_progress(slug)
    updated = skipped = failed = 0
    errors: list[str] = []
    cancelled = False
    try:
        folders = [s for s in sources if s.get("source_kind") == "folder"]
        pages = [s for s in sources if s.get("source_kind") == "confluence"]
        lone_files = [s for s in sources if s.get("source_kind") == "file"]
        lone_files.sort(
            key=lambda s: (
                os.path.getmtime(os.path.abspath(os.path.expanduser(s.get("source_path") or "")))
                if os.path.isfile(os.path.abspath(os.path.expanduser(s.get("source_path") or "")))
                else 0.0
            )
        )
        probes: dict[int, tuple[str, dict, dict]] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {}
            for src in folders:
                jobs[pool.submit(check_folder_changes, src)] = ("folder", src)
            for src in pages:
                jobs[pool.submit(check_confluence_change, src)] = ("confluence", src)
            for fut in as_completed(jobs):
                kind, src = jobs[fut]
                try:
                    probes[src["id"]] = (kind, src, fut.result())
                except Exception as e:
                    probes[src["id"]] = (kind, src, {"error": str(e), "changed": False, "files": [], "stale": []})

        _publish_refresh_plan(slug, probes, lone_files)
        if not _has_refresh_actions(probes, lone_files):
            print("[auto_refresh] источники не новее последней сверки: обновление графа не запускается.")
            return {
                "ok": True,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "message": "Изменений нет: источники не новее последней сверки",
            }
        for kind, src, probe in probes.values():
            _guard_ingest(slug, src.get("source_path") or src.get("source_kind") or "")
            if kind == "folder":
                u, s, f, err = _apply_folder(src, slug, probe)
            else:
                u, s, f, err = _apply_confluence(src, slug, probe)
            updated += u
            skipped += s
            failed += f
            if err:
                errors.append(err)
        for src in lone_files:
            _guard_ingest(slug, src.get("source_path") or "file")
            u, s, f, err = _apply_file(src, slug)
            updated += u
            skipped += s
            failed += f
            if err:
                errors.append(err)
    except Exception as e:
        if type(e).__name__ == "IngestCancelled":
            cancelled = True
            raise
        failed += 1
        errors.append(str(e))
    finally:
        if manage_status:
            if opened_live:
                from web_app import finish_live_progress
                if cancelled:
                    finish_live_progress(slug, ok=False, cancelled=True)
                else:
                    detail = "; ".join(errors[:3]) if failed else (
                        f"Обновлено: {updated}" if updated else "Изменений нет"
                    )
                    finish_live_progress(slug, ok=failed == 0, message=detail)
            msg = "; ".join(errors[:3]) if failed else ""
            end_ingest(slug, ok=failed == 0, message=msg)

    parts = []
    if updated:
        parts.append(f"обновлено: {updated}")
    if skipped:
        parts.append(f"без изменений: {skipped}")
    if failed:
        parts.append(f"ошибок: {failed}")
    message = ", ".join(parts) or "Изменений нет"
    result = {
        "ok": failed == 0,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "message": message,
    }
    if result["ok"] and updated > 0:
        from web_app import touch_project_data
        touch_project_data(slug, "refresh")
    return result


def refresh_all() -> dict:
    sources = list_watch_sources(watch_only=True)
    archived = _archived_slugs()
    slugs = sorted({s.get("project_slug") for s in sources if s.get("project_slug") and s.get("project_slug") not in archived})
    print(f"[auto_refresh] nightly start projects={len(slugs)}")
    summary = {"ok": True, "updated": 0, "skipped": 0, "failed": 0, "projects": len(slugs)}
    for slug in slugs:
        try:
            result = refresh_project(slug)
            summary["updated"] += result.get("updated") or 0
            summary["skipped"] += result.get("skipped") or 0
            summary["failed"] += result.get("failed") or 0
            if not result.get("ok"):
                summary["ok"] = False
            print(f"[auto_refresh] {slug}: {result.get('message')}")
        except Exception as e:
            summary["ok"] = False
            summary["failed"] += 1
            print(f"[auto_refresh] {slug} error: {e}")
    print(f"[auto_refresh] nightly done {summary}")
    return summary


def _seconds_until_hour(hour: int = 2, tz=None) -> float:
    tz = tz or timezone(timedelta(hours=3))
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def start_nightly_scheduler(hour: int = 2) -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Moscow")
        except Exception:
            tz = timezone(timedelta(hours=3))
        while True:
            delay = _seconds_until_hour(hour, tz)
            print(f"[auto_refresh] next run in {delay / 3600:.1f}h")
            time.sleep(delay)
            try:
                refresh_all()
            except Exception as e:
                print(f"[auto_refresh] nightly crash: {e}")

    threading.Thread(target=loop, name="auto-refresh", daemon=True).start()


if __name__ == "__main__":
    refresh_all()
