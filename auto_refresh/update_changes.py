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

from app.handlers.confluence import get_confluence_page_content
from app.handlers.folders_mac import FileTooLargeError, extract_file_text
from storage.postgres.db_connect import list_watch_sources, mark_watch_synced

from auto_refresh.confluence_checker import check_confluence_change
from auto_refresh.folder_checker import check_folder_changes, folder_hashes


_scheduler_started = False


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _ingest_source(slug: str, graph_id: str, text: str, source_input: str, log_text: str) -> tuple[bool, str]:
    from web_app_v2 import _ingest_run_lock, linker, log_event, save_user_note, set_ingest_phase

    with _ingest_run_lock:
        linker.repository.delete_by_source_input(graph_id, source_input)
        set_ingest_phase(slug, "refreshing")
        ok, ans = save_user_note(graph_id, text, source_input=source_input)
    log_event(slug, log_text, "auto_refresh", ans)
    return ok, ans


def _apply_folder(source: dict, slug: str, probe: dict) -> tuple[int, int, int, str]:
    if probe.get("error"):
        mark_watch_synced(source["id"], error=probe["error"], synced=False)
        return 0, 0, 1, probe["error"]

    graph_id = source["graph_id"]
    hashes = folder_hashes(source.get("content_hash"))
    updated = skipped = failed = 0
    last_err = ""

    from web_app_v2 import _ingest_run_lock, linker

    for stale in probe.get("stale") or []:
        try:
            with _ingest_run_lock:
                linker.repository.delete_by_source_input(graph_id, stale)
            hashes.pop(stale, None)
            hashes.pop(os.path.abspath(stale), None)
        except Exception as e:
            failed += 1
            last_err = str(e)

    for path in probe.get("files") or []:
        try:
            text = extract_file_text(path)
            if not (text or "").strip():
                skipped += 1
                continue
            digest = _text_hash(text)
            if hashes.get(path) == digest:
                skipped += 1
                continue
            ok, ans = _ingest_source(slug, graph_id, text, path, f"[auto] {os.path.basename(path)}")
            if ok:
                hashes[path] = digest
                updated += 1
            else:
                failed += 1
                last_err = ans
        except FileTooLargeError as e:
            failed += 1
            last_err = str(e)
        except Exception as e:
            failed += 1
            last_err = str(e)

    if failed and not updated:
        mark_watch_synced(source["id"], error=last_err, synced=False)
    else:
        mark_watch_synced(source["id"], content_hash=json.dumps(hashes, ensure_ascii=False), synced=True)
        if failed:
            mark_watch_synced(source["id"], error=last_err, synced=False)
    return updated, skipped, failed, last_err


def _apply_confluence(source: dict, slug: str, probe: dict) -> tuple[int, int, int, str]:
    if probe.get("error"):
        mark_watch_synced(source["id"], error=probe["error"], synced=False)
        return 0, 0, 1, probe["error"]
    if not probe.get("changed"):
        return 0, 1, 0, ""

    url = source["source_path"]
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
    if digest and digest == (source.get("content_hash") or ""):
        mark_watch_synced(source["id"], content_hash=digest, synced=True)
        return 0, 1, 0, ""

    ok, ans = _ingest_source(slug, source["graph_id"], text, url, f"[auto] {url}")
    if ok:
        mark_watch_synced(source["id"], content_hash=digest, synced=True)
        return 1, 0, 0, ""
    mark_watch_synced(source["id"], error=ans, synced=False)
    return 0, 0, 1, ans


def _archived_slugs() -> set[str]:
    try:
        from web_app_v2 import _load_projects_unlocked
        return {p["slug"] for p in _load_projects_unlocked() if p.get("archived") and p.get("slug")}
    except Exception:
        return set()


def refresh_project(slug: str, manage_status: bool = True) -> dict:
    """Обновляет все watch-источники одного проекта: сначала параллельная сверка, затем ingest."""
    from web_app_v2 import begin_ingest, end_ingest

    sources = list_watch_sources(watch_only=True, project_slug=slug)
    if not sources:
        return {"ok": True, "updated": 0, "skipped": 0, "failed": 0, "message": "Нет источников с отслеживанием изменений"}

    if manage_status:
        begin_ingest(slug, "refreshing")
    updated = skipped = failed = 0
    errors: list[str] = []
    try:
        folders = [s for s in sources if s.get("source_kind") == "folder"]
        pages = [s for s in sources if s.get("source_kind") == "confluence"]
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

        for kind, src, probe in probes.values():
            if kind == "folder":
                u, s, f, err = _apply_folder(src, slug, probe)
            else:
                u, s, f, err = _apply_confluence(src, slug, probe)
            updated += u
            skipped += s
            failed += f
            if err:
                errors.append(err)
    except Exception as e:
        failed += 1
        errors.append(str(e))
    finally:
        if manage_status:
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
    if result["ok"]:
        from web_app_v2 import touch_project_data
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
