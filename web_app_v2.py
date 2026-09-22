from __future__ import annotations
import asyncio
import hashlib
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
import uvicorn

from config.settings import settings
from storage.postgres.db_connect import  create_database, create_tables, update_history_messages
from app.handlers.confluence import CONFLUENCE_HOST, get_confluence_page_content
from app.handlers.folders_mac import (
    EXTRACTABLE_EXTENSIONS,
    FileTooLargeError,
    extract_file_text,
    list_folder_files,
)
from zettelkasten.anonymizer import Anonymizer, EntityMap, unmask_card
from zettelkasten.atomizer import NoteAtomizer
from zettelkasten.graph_rag import GraphRAG
from zettelkasten.graph_visualizer_v2 import (
    encode_graph_html,
    encode_graph_payload,
    pack_graph_payload,
    render_graph_html,
)
from zettelkasten.linker import GraphLinker, LocalEmbeddingModel
# from zettelkasten.embeddings import MTSEmbeddings
from zettelkasten.source_quote import find_source_quote

load_dotenv()

app = FastAPI(title="BVA Exocortex Web App")

create_database()
create_tables()

pii_anonymizer = Anonymizer(
    use_ner=True,
    use_fake_values=False,
    mask_dates=False,
    mask_urls=True,
    mask_ip=True,
)

embedding_model = LocalEmbeddingModel(model_name=settings.embedding_model_name)
atomizer = NoteAtomizer(
    model_name=settings.zettel_atomizer_model_name,
    temperature=settings.zettel_atomizer_temperature,
    system_prompt=settings.zettel_atomizer_system_prompt,
    user_prompt_template=settings.zettel_atomizer_user_prompt_template,
)
linker = GraphLinker(
    embedding_model=embedding_model,
    model_name=settings.linker_model_name,
    temperature=settings.linker_temperature,
    system_prompt=settings.linker_system_prompt,
    user_prompt_template=settings.linker_user_prompt_template,
    similarity_threshold=settings.linker_similarity_threshold,
    max_candidates=settings.linker_max_candidates,
    privacy_anonymizer=pii_anonymizer,
)
graphrag = GraphRAG(
    embedding_model=embedding_model,
    model_name=settings.graphrag_model_name,
    temperature=settings.graphrag_temperature,
    system_prompt=settings.graphrag_system_prompt,
    user_prompt_template=settings.graphrag_user_prompt_template,
    no_context_response=settings.graphrag_no_context_response,
    similarity_threshold=settings.graphrag_similarity_threshold,
    privacy_anonymizer=pii_anonymizer,
)

DELETE_CACHE: dict[str, list[dict]] = {}
COMMON_SLUG = "all"
PROJECTS_FILE = Path(__file__).resolve().parent / "storage" / "web_projects.json"
RESERVED_SLUGS = {COMMON_SLUG, "login", "home", "overview", "projects", "api", "contour"}
CONTOUR_MIN_PROJECTS = 2
CONTOUR_MAX_PROJECTS = 5
PROJECT_ACCENTS = ["#6366f1", "#a855f7", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6"]
MAX_PROJECT_DESC = 240
EDIT_ICON = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
)


def slugify_project(name: str) -> str:
    text = " ".join(name.strip().lower().split())
    text = re.sub(r"[\s]+", "_", text)
    text = "".join(ch for ch in text if ch.isalnum() or ch in "_-")
    return (text[:48] or uuid.uuid4().hex[:8]).strip("_-")


def project_accent(slug: str) -> str:
    digest = hashlib.md5(slug.encode("utf-8")).hexdigest()
    return PROJECT_ACCENTS[int(digest, 16) % len(PROJECT_ACCENTS)]


_state_lock = threading.RLock()
_ingest_jobs: dict[str, int] = {}
_ingest_phase: dict[str, str] = {}
_ingest_error: dict[str, str] = {}
_ingest_snapshot: dict[str, tuple[str, str]] = {}
_ingest_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")
_ingest_run_lock = threading.Lock()
INGEST_STATUS_LABELS = {
    "indexed": "Индексирован",
    "linking": "Встраивание в граф",
    "ready": "Готов",
    "error": "Ошибка обработки",
}
INGEST_ACCEPTED_MSG = (
    "Материал принят. Сущности разбиваются в фонеовом режиме "
    "На проекте загорится зелёный статус, когда граф будет готов."
)


def _normalize_project(item: dict) -> dict:
    item.setdefault("description", "")
    item.setdefault("archived", False)
    item.setdefault("ingest_status", "ready")
    item.setdefault("ingest_error", "")
    if item.get("ingest_status") not in INGEST_STATUS_LABELS:
        item["ingest_status"] = "ready"
    return item


def _stamp_ingest_fields(item: dict) -> dict:
    slug = item.get("slug")
    if slug and slug in _ingest_snapshot:
        status, error = _ingest_snapshot[slug]
        item["ingest_status"] = status
        item["ingest_error"] = error
    return item


def _set_ingest_snapshot(slug: str, status: str, error: str = "") -> None:
    _ingest_snapshot[slug] = (status, error or "")
    if error:
        _ingest_error[slug] = error
    elif status != "error":
        _ingest_error.pop(slug, None)


def _load_projects_unlocked() -> list[dict]:
    if not PROJECTS_FILE.exists():
        return []
    try:
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        projects = [_normalize_project(p) for p in (data.get("projects") or []) if isinstance(p, dict)]
    except Exception as e:
        print(f"[web_app_v2] projects load warning: {e}")
        return []
    for item in projects:
        slug = item.get("slug")
        if slug and slug not in _ingest_snapshot:
            _ingest_snapshot[slug] = (
                item.get("ingest_status") or "ready",
                item.get("ingest_error") or "",
            )
        _stamp_ingest_fields(item)
    return projects


def _save_projects_unlocked(projects: list[dict]) -> None:
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamped = []
    for item in projects:
        row = dict(item)
        _stamp_ingest_fields(row)
        stamped.append(row)
    tmp = PROJECTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"projects": stamped}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(PROJECTS_FILE)


def load_projects() -> list[dict]:
    with _state_lock:
        return _load_projects_unlocked()


def save_projects(projects: list[dict]) -> None:
    with _state_lock:
        _save_projects_unlocked(projects)


def sync_projects_from_graph() -> list[dict]:
    """Подтягивает графы из Neo4j, чтобы старые данные не потерялись."""
    try:
        graph_ids = linker.repository.list_graph_ids()
    except Exception as e:
        print(f"[web_app_v2] graph sync warning: {e}")
        graph_ids = []

    with _state_lock:
        projects = _load_projects_unlocked()
        by_graph = {p.get("graph_id"): p for p in projects if p.get("graph_id")}
        by_slug = {p.get("slug"): p for p in projects if p.get("slug")}
        changed = False

        for gid in graph_ids:
            if gid in by_graph:
                continue
            if gid.startswith("proj_"):
                slug = gid[5:] or uuid.uuid4().hex[:8]
                name = slug.replace("_", " ")
            elif gid.startswith("web_"):
                slug = f"legacy_{gid[4:]}"[:48]
                name = gid[4:] or gid
            else:
                continue
            base = slug
            n = 2
            while slug in by_slug or slug in RESERVED_SLUGS:
                slug = f"{base}_{n}"
                n += 1
            item = {
                "slug": slug,
                "name": name,
                "graph_id": gid,
                "description": "",
                "archived": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _normalize_project(item)
            _set_ingest_snapshot(slug, "ready")
            _stamp_ingest_fields(item)
            projects.append(item)
            by_graph[gid] = item
            by_slug[slug] = item
            changed = True

        if changed:
            _save_projects_unlocked(projects)
        return projects


def get_project(slug: str) -> dict | None:
    for item in load_projects():
        if item.get("slug") == slug:
            return with_live_ingest(item)
    return None


def project_labels_map(projects: list[dict] | None = None) -> dict[str, str]:
    projects = projects if projects is not None else load_projects()
    return {
        p["graph_id"]: p.get("name") or p["slug"]
        for p in projects
        if p.get("graph_id")
    }


def update_project(slug: str, **fields) -> dict | None:
    with _state_lock:
        projects = _load_projects_unlocked()
        for item in projects:
            if item.get("slug") == slug:
                item.update(fields)
                _save_projects_unlocked(projects)
                return with_live_ingest(item)
        return None


def remove_project(slug: str) -> dict | None:
    with _state_lock:
        projects = _load_projects_unlocked()
        item = next((p for p in projects if p.get("slug") == slug), None)
        if not item:
            return None
        _ingest_jobs.pop(slug, None)
        _ingest_phase.pop(slug, None)
        _ingest_error.pop(slug, None)
        _ingest_snapshot.pop(slug, None)
        _save_projects_unlocked([p for p in projects if p.get("slug") != slug])
        return item


def with_live_ingest(item: dict) -> dict:
    row = dict(item)
    slug = row.get("slug")
    with _state_lock:
        if slug and _ingest_jobs.get(slug, 0) > 0:
            status = _ingest_phase.get(slug) or "linking"
            error = ""
        elif slug and slug in _ingest_snapshot:
            status, error = _ingest_snapshot[slug]
        else:
            status = row.get("ingest_status") or "ready"
            error = row.get("ingest_error") or ""
            if status in {"indexed", "linking"}:
                status, error = "error", "Обработка прервана. Загрузите материал снова."
    if status not in INGEST_STATUS_LABELS:
        status = "ready"
    row["ingest_status"] = status
    row["ingest_error"] = error
    return row


def ingest_status_html(status: str, with_label: bool = True, error: str = "") -> str:
    key = status if status in INGEST_STATUS_LABELS else "ready"
    label = error.strip() if key == "error" and error.strip() else INGEST_STATUS_LABELS[key]
    dot = f'<span class="status-dot {key}" title="{escape(label)}"></span>'
    if not with_label:
        return dot
    extra_class = " status-error" if key == "error" else ""
    return f'<span class="status-row{extra_class}">{dot}<span>{escape(label)}</span></span>'


def _patch_ingest_project(slug: str, status: str, error: str = "") -> None:
    projects = _load_projects_unlocked()
    found = False
    for item in projects:
        if item.get("slug") == slug:
            item["ingest_status"] = status
            item["ingest_error"] = error
            found = True
            break
    if found:
        _save_projects_unlocked(projects)
        return
    print(f"[web_app_v2] ingest status: проект {slug!r} не найден в web_projects.json")


def begin_ingest(slug: str, phase: str = "indexed") -> None:
    with _state_lock:
        _ingest_jobs[slug] = _ingest_jobs.get(slug, 0) + 1
        _ingest_phase[slug] = phase
        _set_ingest_snapshot(slug, phase)
        _patch_ingest_project(slug, phase)


def set_ingest_phase(slug: str, phase: str) -> None:
    with _state_lock:
        if _ingest_jobs.get(slug, 0) <= 0:
            return
        _ingest_phase[slug] = phase
        _set_ingest_snapshot(slug, phase)
        _patch_ingest_project(slug, phase)


def end_ingest(slug: str, ok: bool = True, message: str = "") -> None:
    with _state_lock:
        _ingest_jobs[slug] = max(0, _ingest_jobs.get(slug, 0) - 1)
        if not ok:
            _set_ingest_snapshot(slug, "error", message or "Не удалось встроить материал в граф")
        if _ingest_jobs[slug] > 0:
            _ingest_phase[slug] = "linking"
            status, error = _ingest_snapshot.get(slug, ("linking", ""))
            if status == "error":
                _patch_ingest_project(slug, "error", error)
            else:
                _set_ingest_snapshot(slug, "linking")
                _patch_ingest_project(slug, "linking")
            return
        _ingest_phase.pop(slug, None)
        error = _ingest_error.get(slug) or (message if not ok else "")
        if error:
            _set_ingest_snapshot(slug, "error", error)
            _patch_ingest_project(slug, "error", error)
        else:
            _set_ingest_snapshot(slug, "ready")
            _patch_ingest_project(slug, "ready")


def reset_stale_ingest_status() -> None:
    with _state_lock:
        projects = _load_projects_unlocked()
        changed = False
        for item in projects:
            slug = item.get("slug")
            if not slug:
                continue
            if _ingest_jobs.get(slug, 0) > 0:
                continue
            if item.get("ingest_status") in {"indexed", "linking"}:
                item["ingest_status"] = "error"
                item["ingest_error"] = "Обработка прервана. Загрузите материал снова."
                _set_ingest_snapshot(slug, "error", item["ingest_error"])
                changed = True
            else:
                _set_ingest_snapshot(
                    slug,
                    item.get("ingest_status") or "ready",
                    item.get("ingest_error") or "",
                )
        if changed:
            _save_projects_unlocked(projects)


def run_ingest(
    slug: str,
    graph_id: str,
    text: str,
    source_input: str,
    log_type: str,
    log_text: str,
    on_stage=None,
) -> tuple[bool, str]:
    begin_ingest(slug, "indexed")
    ok = False
    ans = ""
    try:
        print(f"[_v2] ingest start slug={slug} chars={len(text or '')}")
        with _ingest_run_lock:
            set_ingest_phase(slug, "linking")
            ok, ans = save_user_note(graph_id, text, on_stage=on_stage, source_input=source_input)
        log_event(slug, log_text, log_type, ans)
        print(f"[web_app_v2] ingest done slug={slug} ok={ok}")
        return ok, ans
    except Exception as e:
        ok = False
        ans = str(e)
        log_event(slug, log_text, log_type, f"Ошибка: {e}")
        print(f"[web_app_v2] ingest background error: {e}")
        return False, ans
    finally:
        end_ingest(slug, ok=ok, message="" if ok else (ans or "Не удалось встроить материал в граф"))


def _queue_ingest(slug: str, graph_id: str, text: str, source_input: str, log_type: str, log_text: str) -> None:
    _ingest_pool.submit(run_ingest, slug, graph_id, text, source_input, log_type, log_text)


reset_stale_ingest_status()


def resolve_scope(slug: str) -> dict | None:
    if slug == COMMON_SLUG:
        projects = sync_projects_from_graph()
        active = [p for p in projects if p.get("graph_id") and not p.get("archived")]
        graph_ids = [p["graph_id"] for p in active]
        return {
            "slug": COMMON_SLUG,
            "name": "Общий граф",
            "graph_id": "__all__",
            "graph_ids": graph_ids,
            "project_labels": project_labels_map(active),
            "readonly": True,
            "description": "",
            "archived": False,
            "ingest_status": "ready",
            "ingest_error": "",
        }
    projects = sync_projects_from_graph()
    item = next((p for p in projects if p.get("slug") == slug), None)
    if not item:
        return None
    live = with_live_ingest(item)
    return {
        "slug": live["slug"],
        "name": live.get("name") or live["slug"],
        "graph_id": live["graph_id"],
        "graph_ids": [live["graph_id"]],
        "project_labels": {live["graph_id"]: live.get("name") or live["slug"]},
        "readonly": False,
        "description": (live.get("description") or "").strip(),
        "archived": bool(live.get("archived")),
        "ingest_status": live.get("ingest_status") or "ready",
        "ingest_error": live.get("ingest_error") or "",
    }


def active_search_projects() -> list[dict]:
    return [p for p in sync_projects_from_graph() if p.get("graph_id") and not p.get("archived")]


def resolve_contour_slugs(slugs: list[str]) -> tuple[list[dict], str]:
    active = {p["slug"]: p for p in active_search_projects()}
    selected: list[dict] = []
    seen: set[str] = set()
    for raw in slugs:
        key = (raw or "").strip()
        if not key or key in seen or key not in active:
            continue
        seen.add(key)
        selected.append(active[key])
        if len(selected) >= CONTOUR_MAX_PROJECTS:
            break
    if len(selected) < CONTOUR_MIN_PROJECTS:
        return [], f"Выберите от {CONTOUR_MIN_PROJECTS} до {CONTOUR_MAX_PROJECTS} проектов"
    return selected, ""


def rag_search_payload(query: str, graph_ids: list[str], labels: dict[str, str], log_key: str) -> dict:
    resp = graphrag.query(
        "__all__",
        query,
        user_ids=graph_ids,
        project_labels=labels or None,
    )
    log_event(log_key, query, "search_query", resp.answer)
    sources = []
    seen = set()
    for node in resp.context.all_nodes:
        name = labels.get(node.user_id or "")
        if name and name not in seen:
            seen.add(name)
            sources.append(name)
    meta = (
        f"⏱ {resp.processing_time_ms}ms · {len(resp.context.entry_points)} точек · "
        f"{len(resp.context.expanded_nodes)} узлов"
    )
    if sources:
        meta += " · из: " + ", ".join(sources)
    return {
        "answer_html": format_llm_response(resp.answer),
        "meta": meta,
        "sources": sources,
        "input_sources": collect_input_sources(resp.context.all_nodes, labels),
    }


def save_user_note(
    user_id: str,
    text: str,
    on_stage=None,
    source_input: str = "text",
) -> tuple[bool, str]:
    def stage(key: str, title: str, sub: str = "") -> None:
        if on_stage:
            on_stage(key, title, sub)

    def atom_progress(index: int, total: int) -> None:
        detail = (
            f"Фрагмент {index} из {total}"
            if total > 1
            else "Атомизатор разбивает текст на сущности"
        )
        stage("atomize", "Извлечение атомарных сущностей", detail)

    stage("mask", "Обезличивание данных", "Конфиденциальные данные скрываются перед моделью")
    entity_map = EntityMap()
    masked_text = pii_anonymizer.mask(text, entity_map)

    stage("atomize", "Извлечение атомарных сущностей", "Атомизатор разбивает текст на фрагменты")
    raw_cards = atomizer.atomize(
        text=masked_text,
        current_db_max_root_id=linker.repository.get_max_root_id(user_id),
        on_progress=atom_progress,
    )
    if isinstance(raw_cards, str):
        return False, f"Ошибка: {raw_cards}"

    for card in raw_cards:
        unmask_card(card, entity_map)
        card.source_input = (source_input or "").strip() or "text"
        card.source_quote = find_source_quote(text, card.content)

    total = len(raw_cards)

    def link_progress(index: int, count: int, topic: str = "") -> None:
        hint = f" · «{topic}»" if topic else ""
        stage("link", "Связывание в граф", f"Карточка {index} из {count}{hint}")

    stage("link", "Связывание в граф", f"Линкер встраивает {total} карточек")
    linker.link_and_insert(user_id=user_id, new_cards=raw_cards, on_progress=link_progress)
    stats = linker.get_user_stats(user_id)
    return True, f"✅ Записано в граф знаний.\n📚 Размер базы: {stats['total_cards']} карточек"


SUPPORTED_UPLOAD_EXTS = set(EXTRACTABLE_EXTENSIONS)

FILE_FORMAT_LABELS = {
    ".pdf": "PDF",
    ".txt": "TXT",
    ".pptx": "PPTX",
    ".ppt": "PPT",
    ".doc": "DOC",
    ".docx": "DOCX",
    ".png": "PNG",
    ".jpg": "JPG",
    ".jpeg": "JPEG",
}


def _file_format_label(path: str) -> str:
    ext = Path(path).suffix.lower()
    return FILE_FORMAT_LABELS.get(ext, ext.lstrip(".").upper() or "файл")


def describe_source_input(raw: str) -> dict:
    value = (raw or "").strip() or "text"
    if value.lower().startswith(("http://", "https://")):
        return {
            "kind": "confluence",
            "title": "Страница Confluence",
            "label": value,
            "href": value,
        }
    if value == "text":
        return {
            "kind": "text",
            "title": "Текст",
            "label": "введённый текст",
            "href": None,
        }
    path = Path(value)
    href = None
    if path.is_absolute():
        try:
            href = path.as_uri()
        except ValueError:
            href = None
    return {
        "kind": "file",
        "title": "Файл",
        "label": value,
        "href": href,
    }


def collect_input_sources(nodes, project_labels: dict | None = None) -> list[dict]:
    items = []
    for node in nodes:
        raw = (getattr(node, "source_input", None) or "").strip() or "text"
        item = describe_source_input(raw)
        item["topic"] = (getattr(node, "topic", None) or "").strip()
        item["luhmann_id"] = getattr(node, "luhmann_id", "") or ""
        item["quote"] = (getattr(node, "source_quote", None) or "").strip()
        if project_labels:
            item["project"] = project_labels.get(getattr(node, "user_id", None) or "", "")
        items.append(item)
    return items


def log_event(scope_key: str, message_text: str, message_type: str, bot_answer: str) -> None:
    try:
        numeric = abs(hash(scope_key)) % 2_000_000_000
        update_history_messages(numeric, int(time.time() * 1000),
            message_text, datetime.now(), message_type, bot_answer)
    except Exception as e:
        print(f"[web_app_v2] log warning: {e}")


def project_nav(scope: dict) -> str:
    return (
        f'<a href="/" class="back-link">← Проекты</a>'
        f'<div class="page-kicker">{escape(scope["name"])}'
        f'{" · только просмотр" if scope["readonly"] else ""}</div>'
    )


def format_llm_response(text: str) -> str:
    """Format LLM response - supports both HTML and markdown."""
    
    # Check if text already contains HTML tags
    has_html = bool(re.search(r'<(b|i|strong|em|p|br|ul|ol|li|h[1-6]|code|pre|a|span|div)\b', text, re.IGNORECASE))
    
    if has_html:
        # Text already has HTML - just sanitize dangerous tags but keep safe ones
        # Remove script, style, iframe, etc.
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<iframe[^>]*>.*?</iframe>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<(script|style|iframe|object|embed|form|input|button)[^>]*/?>', '', text, flags=re.IGNORECASE)
        
        # Convert <b> to <strong>, <i> to <em> for consistency
        text = re.sub(r'<b\b([^>]*)>', r'<strong\1>', text, flags=re.IGNORECASE)
        text = re.sub(r'</b>', '</strong>', text, flags=re.IGNORECASE)
        text = re.sub(r'<i\b([^>]*)>', r'<em\1>', text, flags=re.IGNORECASE)
        text = re.sub(r'</i>', '</em>', text, flags=re.IGNORECASE)
        
        # Handle [number] references - make them smaller/styled
        text = re.sub(r'\[(\d+(?:\.\d+)?)\]', r'<sup class="ref">[\1]</sup>', text)
        
        # Ensure line breaks are preserved
        text = text.replace('\n\n', '</p><p>')
        text = text.replace('\n', '<br>')
        
        # Wrap in paragraph if not already wrapped
        if not text.strip().startswith('<p') and not text.strip().startswith('<h') and not text.strip().startswith('<ul') and not text.strip().startswith('<ol'):
            text = f'<p>{text}</p>'
    else:
        # No HTML - apply markdown formatting
        # Escape HTML first
        text = escape(text)
        
        # Bold: **text** or __text__
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
        
        # Italic: *text* or _text_
        text = re.sub(r'\*([^*]+?)\*', r'<em>\1</em>', text)
        text = re.sub(r'_([^_]+?)_', r'<em>\1</em>', text)
        
        # Code: `code`
        text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
        
        # Headers: # ## ###
        text = re.sub(r'^### (.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
        text = re.sub(r'^## (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
        text = re.sub(r'^# (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
        
        # Lists: - item or * item or numbered
        text = re.sub(r'^[\-\*] (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
        text = re.sub(r'^(\d+)\. (.+)$', r'<li>\2</li>', text, flags=re.MULTILINE)
        
        # Wrap consecutive <li> in <ul>
        text = re.sub(r'((?:<li>.+?</li>\n?)+)', r'<ul>\1</ul>', text)
        
        # Handle [number] references
        text = re.sub(r'\[(\d+(?:\.\d+)?)\]', r'<sup class="ref">[\1]</sup>', text)
        
        # Line breaks
        text = text.replace('\n\n', '</p><p>')
        text = text.replace('\n', '<br>')
        text = f'<p>{text}</p>'
    
    # Clean up empty paragraphs
    text = re.sub(r'<p>\s*</p>', '', text)
    text = re.sub(r'<p>\s*<(h[234]|ul|ol)', r'<\1', text)
    text = re.sub(r'</(h[234]|ul)>\s*</p>', r'</\1>', text)
    
    return text


CSS = """
:root {
  --bg: #f8fafc; --card: #ffffff; --card2: #f1f5f9;
  --text: #0f172a; --text2: #334155; --muted: #64748b;
  --accent: #6366f1; --accent2: #a855f7;
  --success: #10b981; --error: #ef4444; --border: #e2e8f0;
  --glow: rgba(99,102,241,0.1);
  --code-bg: #f1f5f9;
}
[data-theme="dark"] {
  --bg: #0a0a0f; --card: #12121a; --card2: #1a1a24;
  --text: #fff; --text2: #e5e5e5; --muted: #6b7280;
  --border: #1f1f2e; --glow: rgba(99,102,241,0.15);
  --code-bg: #1e1e2e;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  transition: background 0.3s, color 0.3s;
}
[data-theme="dark"] body {
  background-image: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(99,102,241,0.15), transparent);
}
.container { max-width: 480px; margin: 0 auto; padding: 20px 16px; min-height: 100vh; }
.container.wide { max-width: 600px; }
.container.hub { max-width: 880px; }
.page-kicker { color: var(--muted); font-size: 13px; margin: 4px 0 12px; }
.readonly-banner { background: linear-gradient(135deg, rgba(99,102,241,0.12), rgba(168,85,247,0.12)); border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px; margin-bottom: 16px; font-size: 14px; color: var(--text2); line-height: 1.5; }
.hub-hero { text-align: center; padding: 28px 0 8px; }
.hub-hero h1 { font-size: 32px; background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.hub-hero p { color: var(--muted); margin-top: 8px; font-size: 15px; }
.hub-search { width: 100%; padding: 14px 16px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; color: var(--text); font-size: 15px; margin: 16px 0 20px; }
.hub-search:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); }
.hub-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 22px; }
@media (max-width: 720px) { .hub-actions { grid-template-columns: 1fr; } }
.common-card { display: block; text-decoration: none; color: inherit; background: linear-gradient(135deg, rgba(99,102,241,0.18), rgba(168,85,247,0.12)); border: 1px solid rgba(99,102,241,0.45); border-radius: 20px; padding: 22px; margin-bottom: 0; transition: transform 0.2s, box-shadow 0.2s; }
.common-card:hover { transform: translateY(-3px); box-shadow: 0 16px 40px var(--glow); }
.common-card h2 { font-size: 20px; margin-bottom: 6px; }
.common-card p { color: var(--text2); font-size: 14px; line-height: 1.5; }
.common-meta { margin-top: 12px; color: var(--muted); font-size: 13px; }
.common-card.contour-card { background: linear-gradient(135deg, rgba(6,182,212,0.16), rgba(99,102,241,0.12)); border-color: rgba(6,182,212,0.45); }
.container.contour { max-width: 880px; }
.contour-banner { background: linear-gradient(135deg, rgba(6,182,212,0.12), rgba(99,102,241,0.12)); border: 1px solid rgba(6,182,212,0.28); border-radius: 16px; padding: 16px 18px; margin-bottom: 18px; }
.contour-banner h2 { font-size: 15px; margin-bottom: 6px; }
.contour-banner p { color: var(--text2); font-size: 13px; line-height: 1.55; }
.contour-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 4px 0 10px; flex-wrap: wrap; }
.contour-count { font-size: 13px; color: var(--muted); }
.contour-count strong { color: var(--text); font-weight: 600; }
.contour-reset { background: transparent; border: none; color: var(--accent); font: inherit; font-size: 13px; cursor: pointer; padding: 0; }
.contour-reset:hover { text-decoration: underline; }
.contour-search { width: 100%; padding: 12px 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; color: var(--text); font-size: 14px; margin: 0 0 10px; }
.contour-search:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); }
.contour-rail { position: relative; margin: 0 -4px 12px; }
.contour-rail-btn { position: absolute; top: 50%; transform: translateY(-50%); z-index: 2; width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer; font-size: 18px; line-height: 1; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 14px var(--glow); }
.contour-rail-btn:hover { border-color: var(--accent); color: var(--accent); }
.contour-rail-btn.prev { left: 0; }
.contour-rail-btn.next { right: 0; }
.contour-pick { display: flex; gap: 10px; overflow-x: auto; scroll-snap-type: x mandatory; scroll-padding: 0 40px; padding: 6px 40px 14px; -webkit-overflow-scrolling: touch; scrollbar-width: thin; }
.contour-pick::-webkit-scrollbar { height: 6px; }
.contour-pick::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }
.contour-tile { flex: 0 0 210px; scroll-snap-align: start; text-align: left; background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 14px 16px; cursor: pointer; color: inherit; font: inherit; position: relative; overflow: hidden; min-height: 112px; transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s; }
.contour-tile.hidden { display: none; }
.contour-tile::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--dot, var(--accent)); }
.contour-tile:hover { border-color: var(--dot, var(--accent)); transform: translateY(-1px); }
.contour-tile.selected { border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); background: rgba(99,102,241,0.08); }
.contour-tile.disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
.contour-tile h3 { font-size: 14px; margin: 2px 0 6px; padding-right: 22px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.contour-tile .desc { color: var(--muted); font-size: 12px; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.contour-tile .meta { color: var(--muted); font-size: 11px; margin-top: 8px; }
.contour-check { position: absolute; top: 12px; right: 12px; width: 18px; height: 18px; border-radius: 50%; border: 1.5px solid var(--border); background: var(--card); }
.contour-tile.selected .contour-check { background: var(--accent); border-color: var(--accent); }
.contour-tile.selected .contour-check::after { content: ''; position: absolute; left: 5px; top: 2px; width: 5px; height: 8px; border: solid #fff; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
.contour-filter-empty { display: none; text-align: center; color: var(--muted); font-size: 13px; padding: 8px 0 14px; }
.contour-selected { display: flex; flex-wrap: wrap; gap: 6px; min-height: 28px; margin-bottom: 14px; }
.contour-chip { font-size: 12px; padding: 4px 8px 4px 10px; border-radius: 999px; background: rgba(6,182,212,0.14); border: 1px solid rgba(6,182,212,0.35); color: var(--text2); display: inline-flex; align-items: center; gap: 6px; }
.contour-chip button { border: none; background: transparent; color: var(--muted); cursor: pointer; font-size: 14px; line-height: 1; padding: 0 2px; }
.contour-chip button:hover { color: var(--error); }
.contour-wait { background: var(--card); border: 1px dashed var(--border); border-radius: 16px; padding: 28px 20px; text-align: center; color: var(--muted); font-size: 14px; line-height: 1.55; margin-bottom: 8px; }
.contour-wait strong { color: var(--text); font-weight: 600; }
.rag-sources .src-project { font-size: 11px; color: var(--accent); font-weight: 600; }
.section-title { font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin: 8px 0 12px; }
.project-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; margin-bottom: 24px; }
.project-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; min-height: 130px; transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s, opacity 0.2s; position: relative; overflow: hidden; }
.project-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 4px; background: var(--dot, var(--accent)); }
.project-card:hover { transform: translateY(-3px); border-color: var(--dot, var(--accent)); box-shadow: 0 10px 28px var(--glow); }
.project-card-link { display: block; padding: 18px 40px 18px 18px; color: inherit; text-decoration: none; min-height: 130px; }
.project-card h3 { font-size: 16px; margin-bottom: 8px; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.project-card p { color: var(--muted); font-size: 13px; }
.project-card .desc { color: var(--text2); font-size: 13px; line-height: 1.45; margin-bottom: 8px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.project-card.archived { opacity: 0.58; }
.project-card .badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--card2); color: var(--muted); font-weight: 500; }
.status-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.status-dot.ready { background: #22c55e; }
.status-dot.indexed { background: #f97316; }
.status-dot.linking { background: #eab308; }
.status-dot.error { background: #ef4444; }
.status-dot.indexed, .status-dot.linking { animation: status-pulse 1.2s ease-in-out infinite; }
@keyframes status-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
.status-row { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.status-row.status-error { color: #ef4444; align-items: flex-start; }
.status-row.status-error span { line-height: 1.35; }
.project-card .status-row { margin-top: 8px; }
.icon-edit { position: absolute; top: 12px; right: 10px; z-index: 2; width: 30px; height: 30px; border: none; background: transparent; color: var(--muted); border-radius: 8px; cursor: pointer; display: flex; align-items: center; justify-content: center; opacity: 0.35; transition: opacity 0.15s, background 0.15s, color 0.15s; }
.project-card:hover .icon-edit, .icon-edit:focus { opacity: 1; }
.icon-edit:hover { background: var(--card2); color: var(--accent); }
.icon-edit.inline { position: static; opacity: 0.4; flex-shrink: 0; }
.icon-edit.inline:hover { opacity: 1; }
.project-card.create-card { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; width: 100%; background: transparent; border: 1.5px dashed var(--border); opacity: 0.5; cursor: pointer; color: var(--muted); padding: 18px; font: inherit; }
.project-card.create-card::before { opacity: 0.25; background: var(--muted); }
.project-card.create-card:hover { opacity: 0.95; border-color: var(--accent); color: var(--text); background: rgba(99,102,241,0.04); }
.create-plus { font-size: 38px; font-weight: 300; line-height: 1; color: var(--accent); }
.create-label { font-size: 13px; }
.hub-tools { display: flex; justify-content: flex-end; margin: -4px 0 12px; }
.hub-tools label { color: var(--muted); font-size: 13px; display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.title-row { display: flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: wrap; }
.title-row h1 { margin: 0; }
.project-desc { color: var(--text2); font-size: 15px; line-height: 1.6; margin-top: 10px; }
.project-desc.empty { color: var(--muted); font-style: italic; }
.project-count { color: var(--muted); font-size: 13px; margin-top: 10px; }
.meta-actions { display: flex; gap: 10px; margin: 8px 0 28px; }
.meta-actions form { flex: 1; margin: 0; }
.btn-ghost { display: block; width: 100%; padding: 12px 10px; background: transparent; border: 1px solid var(--border); border-radius: 12px; color: var(--muted); font-size: 13px; cursor: pointer; transition: all 0.15s; }
.btn-ghost:hover { border-color: var(--accent); color: var(--text); background: var(--card); }
.btn-ghost.danger { color: var(--error); border-color: rgba(239,68,68,0.35); }
.btn-ghost.danger:hover { background: rgba(239,68,68,0.08); border-color: var(--error); }
.empty-projects { color: var(--muted); font-size: 14px; padding: 12px 0 20px; }
.archived-banner { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35); color: #f59e0b; border-radius: 14px; padding: 14px 16px; margin-bottom: 16px; font-size: 14px; line-height: 1.5; }
.source-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.source-chip { font-size: 11px; padding: 4px 10px; border-radius: 999px; background: rgba(99,102,241,0.14); border: 1px solid rgba(99,102,241,0.35); color: var(--text2); }

/* Theme toggle */
.theme-toggle { position: fixed; top: 16px; right: 16px; z-index: 100; background: var(--card); border: 1px solid var(--border); border-radius: 50%; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 20px; transition: all 0.2s; }
.theme-toggle:hover { transform: scale(1.1); }

/* Auth */
.auth-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.auth-box { width: 100%; max-width: 360px; }
.logo { text-align: center; margin-bottom: 32px; }
.logo h1 { font-size: 28px; background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.logo p { color: var(--muted); font-size: 14px; margin-top: 6px; }
.auth-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 28px 24px; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; color: var(--muted); font-size: 13px; margin-bottom: 6px; }
.form-group input { width: 100%; padding: 14px 16px; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; color: var(--text); font-size: 15px; transition: border 0.2s; }
.form-group input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); }
.btn { display: block; width: 100%; padding: 14px; background: linear-gradient(135deg, var(--accent), var(--accent2)); border: none; border-radius: 10px; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; transition: transform 0.1s, opacity 0.2s; }
.btn:hover { opacity: 0.9; }
.btn:active { transform: scale(0.98); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
.btn-outline:hover { background: var(--card2); }
.btn-danger { background: var(--error); }
.error-msg { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); color: var(--error); padding: 12px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; text-align: center; }

/* Header */
.header { text-align: center; padding: 28px 0 20px; }
.header h1 { font-size: 26px; background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header p { color: var(--muted); font-size: 14px; margin-top: 4px; }
.header .project-desc { color: var(--text2); font-size: 15px; line-height: 1.6; margin-top: 10px; }
.header .project-desc.empty { color: var(--muted); font-style: italic; }
.user-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; background: var(--card); border: 1px solid var(--border); border-radius: 20px; font-size: 13px; color: var(--muted); margin-bottom: 16px; }
.user-pill a { color: var(--accent); text-decoration: none; margin-left: 8px; }
.user-pill a:hover { text-decoration: underline; }

/* Welcome */
.welcome { background: linear-gradient(135deg, var(--card) 0%, var(--card2) 100%); border: 1px solid var(--border); border-radius: 16px; padding: 20px; margin-bottom: 20px; }
.welcome h3 { font-size: 15px; margin-bottom: 12px; color: var(--text); }
.welcome ul { margin: 8px 0 14px 18px; color: var(--text2); font-size: 14px; line-height: 1.7; }
.welcome p { color: var(--text2); font-size: 14px; line-height: 1.6; }

/* Menu */
.menu-btn { display: flex; align-items: center; gap: 14px; width: 100%; padding: 18px 20px; margin-bottom: 12px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; color: var(--text); font-size: 15px; text-decoration: none; transition: all 0.2s; position: relative; overflow: hidden; }
.menu-btn::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(135deg, var(--accent), var(--accent2)); opacity: 0; transition: opacity 0.2s; }
.menu-btn:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 8px 24px var(--glow); }
.menu-btn:hover::before { opacity: 0.08; }
.menu-btn .icon { font-size: 22px; width: 32px; text-align: center; position: relative; z-index: 1; }
.menu-btn span { position: relative; z-index: 1; }

/* Page */
.page-header { display: flex; align-items: center; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
.page-header h2 { font-size: 18px; font-weight: 500; }
.back-link { color: var(--accent); text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; gap: 4px; margin-bottom: 12px; }
.back-link:hover { text-decoration: underline; }
.msg-box { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; color: var(--text2); font-size: 14px; }

/* Forms */
textarea { width: 100%; padding: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; color: var(--text); font-size: 14px; resize: vertical; min-height: 100px; font-family: inherit; }
textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); }
input[type="file"] { width: 100%; padding: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; color: var(--text); font-size: 14px; }
input[type="file"]::file-selector-button { background: var(--accent); color: #fff; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; margin-right: 12px; }

/* Tabs */
.tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.tab { flex: 1; padding: 12px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; color: var(--muted); font-size: 14px; text-align: center; cursor: pointer; transition: all 0.2s; }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.file-modes { display: flex; gap: 8px; margin-bottom: 14px; }
.file-mode { flex: 1; padding: 10px 12px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; color: var(--muted); font-size: 13px; text-align: center; cursor: pointer; }
.file-mode.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.tick-check { display: flex; align-items: center; gap: 8px; margin: 12px 0 16px; cursor: pointer; color: var(--text2); font-size: 13px; user-select: none; }
.tick-check input { position: absolute; opacity: 0; width: 0; height: 0; }
.tick-box { width: 18px; height: 18px; border: 1.5px solid var(--border); border-radius: 5px; display: inline-flex; align-items: center; justify-content: center; color: transparent; background: var(--card); flex-shrink: 0; }
.tick-box svg { width: 12px; height: 12px; }
.tick-check input:checked + .tick-box { border-color: var(--accent); background: rgba(99,102,241,0.12); color: var(--accent); }
.folder-log { display: none; margin-top: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; max-height: 240px; overflow-y: auto; font-size: 13px; line-height: 1.55; }
.folder-log.active { display: block; }
.folder-log .log-ok { color: var(--success); }
.folder-log .log-err { color: var(--error); }
.folder-log .log-info { color: var(--muted); }

/* Alerts */
.alert { padding: 14px 16px; border-radius: 12px; margin-bottom: 16px; font-size: 14px; display: flex; align-items: flex-start; gap: 10px; }
.alert-success { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); color: var(--success); }
.alert-error { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); color: var(--error); }

/* Chat */
.chat-container { display: flex; flex-direction: column; height: calc(100vh - 200px); min-height: 400px; }
.container.contour .chat-container { height: calc(100vh - 430px); min-height: 280px; }
.chat-messages { flex: 1; overflow-y: auto; padding: 16px 0; display: flex; flex-direction: column; gap: 12px; }
.chat-msg { max-width: 85%; padding: 14px 18px; border-radius: 18px; font-size: 14px; line-height: 1.6; animation: fadeIn 0.3s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.chat-msg.user { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; }
.chat-msg.bot { background: var(--card); border: 1px solid var(--border); align-self: flex-start; border-bottom-left-radius: 4px; color: var(--text); }
.chat-msg .meta { font-size: 11px; color: var(--muted); margin-top: 8px; opacity: 0.8; }
.rag-sources { margin-top: 12px; font-size: 13px; }
.rag-sources summary {
    cursor: pointer; color: var(--accent); font-weight: 600; list-style: none;
    display: inline-flex; align-items: center; gap: 6px;
}
.rag-sources summary::-webkit-details-marker { display: none; }
.rag-sources summary::before { content: "▸"; font-size: 12px; }
.rag-sources[open] summary::before { content: "▾"; }
.rag-sources ul { margin: 10px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.rag-sources li { display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border-radius: 10px; background: rgba(99,102,241,0.08); border: 1px solid rgba(99,102,241,0.22); }
.rag-sources .src-kind { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.rag-sources a { color: var(--accent); word-break: break-all; text-decoration: none; }
.rag-sources a:hover { text-decoration: underline; }
.rag-sources .src-label { color: var(--text2); word-break: break-all; }
.rag-sources .src-quote {
  margin: 6px 0 0;
  padding: 8px 10px;
  border-left: 2px solid var(--accent);
  color: var(--text2);
  font-size: 13px;
  line-height: 1.45;
}
.chat-msg h2, .chat-msg h3, .chat-msg h4 { margin: 12px 0 8px; font-size: 15px; }
.chat-msg h2 { font-size: 17px; }
.chat-msg ul { margin: 8px 0; padding-left: 20px; }
.chat-msg li { margin: 4px 0; }
.chat-msg code { background: var(--code-bg); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.chat-msg strong { font-weight: 600; }
.chat-msg em { font-style: italic; }
.chat-msg p { margin: 8px 0; }
.chat-msg p:first-child { margin-top: 0; }
.chat-msg p:last-child { margin-bottom: 0; }
.chat-msg .ref { font-size: 10px; color: var(--accent); opacity: 0.7; vertical-align: super; margin: 0 1px; }
.chat-input-wrap { display: flex; gap: 10px; padding-top: 16px; border-top: 1px solid var(--border); }
.chat-input-wrap textarea { flex: 1; min-height: 48px; max-height: 120px; padding: 12px 16px; }
.chat-input-wrap .btn { width: auto; padding: 12px 24px; }

/* Delete cards */
.delete-cards { display: flex; flex-direction: column; gap: 12px; margin: 16px 0; }
.delete-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; cursor: pointer; transition: all 0.2s; }
.delete-card:hover { border-color: var(--accent); transform: translateX(4px); }
.delete-card .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.delete-card .card-id { font-size: 12px; color: var(--accent); font-weight: 500; }
.delete-card .card-score { font-size: 11px; color: var(--muted); background: var(--card2); padding: 2px 8px; border-radius: 10px; }
.delete-card .card-preview { font-size: 14px; color: var(--text2); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* Loading */
.loading-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(248,250,252,0.92); display: none; align-items: center; justify-content: center; z-index: 1100; backdrop-filter: blur(8px); }
[data-theme="dark"] .loading-overlay { background: rgba(10,10,15,0.92); }
.loading-overlay.active { display: flex; }
.loading-box { text-align: center; min-width: 280px; max-width: 440px; padding: 8px 12px; }
.spinner { width: 56px; height: 56px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-text { color: var(--text); font-size: 15px; font-weight: 500; }
.loading-subtext { color: var(--muted); font-size: 13px; margin-top: 8px; min-height: 18px; }
.loading-dots::after { content: ''; animation: dots 1.5s steps(4) infinite; }
@keyframes dots { 0% { content: ''; } 25% { content: '.'; } 50% { content: '..'; } 75% { content: '...'; } }
.loading-progress { width: 200px; height: 4px; background: var(--border); border-radius: 2px; margin: 16px auto 0; overflow: hidden; }
.loading-progress-bar { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); width: 30%; animation: progress 1.5s ease-in-out infinite; }
@keyframes progress { 0% { transform: translateX(-100%); } 100% { transform: translateX(400%); } }
.loading-overlay.has-batch .loading-progress { display: none; }
.loading-file-progress { display: none; margin: 16px 0 0; text-align: left; }
.loading-file-progress.active { display: block; }
.loading-file-meta { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.loading-file-count { font-weight: 600; color: var(--text); font-size: 13px; }
.loading-file-bar { height: 10px; background: var(--border); border-radius: 99px; overflow: hidden; }
.loading-file-bar-fill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent), var(--accent2)); border-radius: 99px; transition: width 0.35s ease; }
.loading-file-name { margin-top: 8px; font-size: 13px; color: var(--text2); word-break: break-word; }
.loading-stages { display: none; text-align: left; margin: 18px 0 0; padding: 12px 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; }
.loading-stages.active { display: block; }
.loading-hint { color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 14px; }
.loading-stage { display: flex; align-items: flex-start; gap: 10px; padding: 7px 0; font-size: 13px; color: var(--muted); line-height: 1.35; }
.loading-stage + .loading-stage { border-top: 1px solid var(--border); }
.loading-stage.current { color: var(--text); font-weight: 500; }
.loading-stage.done { color: var(--success); }
.loading-stage-mark { width: 16px; height: 16px; flex-shrink: 0; margin-top: 1px; border-radius: 50%; border: 1.5px solid var(--border); box-sizing: border-box; position: relative; }
.loading-stage.current .loading-stage-mark { border-color: var(--accent); border-top-color: transparent; animation: spin 0.8s linear infinite; }
.loading-stage.done .loading-stage-mark { background: var(--success); border-color: var(--success); }
.loading-stage.done .loading-stage-mark::after { content: ''; position: absolute; left: 4px; top: 1px; width: 5px; height: 8px; border: solid #fff; border-width: 0 1.5px 1.5px 0; transform: rotate(45deg); }
.loading-stage-copy { flex: 1; min-width: 0; }
.loading-stage-detail { display: block; color: var(--muted); font-weight: 400; font-size: 12px; margin-top: 2px; }

/* Particles */
.particles { position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; z-index: -1; opacity: 0.5; }
[data-theme="dark"] .particles { opacity: 1; }
.particle { position: absolute; width: 4px; height: 4px; background: var(--accent); border-radius: 50%; opacity: 0.3; animation: float 15s infinite; }
@keyframes float { 0%, 100% { transform: translateY(100vh) rotate(0deg); opacity: 0; } 10% { opacity: 0.3; } 90% { opacity: 0.3; } }

/* Mouse spray trail */
.spray-layer {
  position: fixed;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
  z-index: 50;
}
.spray-dot {
  position: fixed;
  width: 110px;
  height: 110px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(99,102,241,0.22) 0%, rgba(168,85,247,0.12) 38%, rgba(99,102,241,0) 72%);
  transform: translate(-50%, -50%);
  filter: blur(22px);
  animation: spray-haze 1.55s ease-out forwards;
  will-change: transform, opacity, filter;
  pointer-events: none;
}
[data-theme="dark"] .spray-dot {
  background: radial-gradient(circle, rgba(168,85,247,0.28) 0%, rgba(99,102,241,0.16) 40%, rgba(99,102,241,0) 72%);
}
.spray-follow {
  position: fixed;
  width: 160px;
  height: 160px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(99,102,241,0.16) 0%, rgba(168,85,247,0.08) 42%, transparent 70%);
  transform: translate(-50%, -50%);
  filter: blur(28px);
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.35s;
}
[data-theme="dark"] .spray-follow {
  background: radial-gradient(circle, rgba(168,85,247,0.22) 0%, rgba(99,102,241,0.1) 45%, transparent 72%);
}
@keyframes spray-haze {
  0% { opacity: 0.55; transform: translate(-50%, -50%) scale(0.55); filter: blur(16px); }
  100% { opacity: 0; transform: translate(calc(-50% + var(--dx, 0px)), calc(-50% + var(--dy, 0px))) scale(1.85); filter: blur(34px); }
}

/* Modal */
.modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(248,250,252,0.92); display: none; align-items: center; justify-content: center; z-index: 1000; backdrop-filter: blur(8px); padding: 20px; }
[data-theme="dark"] .modal-overlay { background: rgba(10,10,15,0.92); }
.modal-overlay.active { display: flex; }
.modal-box { background: var(--card); border: 1px solid var(--border); border-radius: 20px; padding: 28px; max-width: 450px; width: 100%; animation: modalIn 0.25s ease; }
@keyframes modalIn { from { opacity: 0; transform: scale(0.95) translateY(10px); } to { opacity: 1; transform: scale(1) translateY(0); } }
.modal-box h3 { margin-bottom: 12px; font-size: 18px; }
.modal-box p { color: var(--muted); font-size: 14px; margin-bottom: 16px; }
.modal-box textarea { min-height: 88px; background: var(--bg); }
.modal-box .quote { background: var(--card2); padding: 16px; border-radius: 12px; margin-bottom: 20px; font-size: 14px; line-height: 1.6; color: var(--text2); max-height: 200px; overflow-y: auto; border-left: 3px solid var(--accent); }
.modal-box .card-id { font-size: 12px; color: var(--accent); margin-bottom: 8px; font-weight: 500; }
.modal-btns { display: flex; gap: 12px; }
.modal-btns button { flex: 1; padding: 14px; border-radius: 10px; font-size: 14px; font-weight: 500; cursor: pointer; border: none; transition: all 0.2s; }
.modal-btns .cancel { background: var(--card2); color: var(--text); border: 1px solid var(--border); }
.modal-btns .cancel:hover { background: var(--border); }
.modal-btns .confirm { background: var(--error); color: #fff; }
.modal-btns .confirm:hover { opacity: 0.9; }
"""

JS_COMMON = """
// Theme
function initTheme() {
    const saved = localStorage.getItem('theme') || 'light';
    document.documentElement.setAttribute('data-theme', saved);
    updateThemeIcon();
}
function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    localStorage.setItem('exocortex_theme', next);
    updateThemeIcon();
}
function updateThemeIcon() {
    const btn = document.querySelector('.theme-toggle');
    if (btn) {
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        btn.textContent = isDark ? '☀️' : '🌙';
    }
}
initTheme();

function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
}
function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
}
document.addEventListener('click', function(e) {
    if (e.target.classList && e.target.classList.contains('modal-overlay')) {
        e.target.classList.remove('active');
    }
});
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(function(el) {
            el.classList.remove('active');
        });
    }
});

// Loading
function showLoading(text, subtext, stages) {
    document.getElementById('loadingText').textContent = text || 'Обработка';
    const sub = document.getElementById('loadingSubtext');
    if (sub) sub.textContent = subtext || '';
    renderLoadingStages(stages || []);
    document.getElementById('loadingOverlay').classList.add('active');
}
function hideLoading() {
    document.getElementById('loadingOverlay').classList.remove('active');
    clearBatchProgress();
}
function setBatchProgress(index, total, name, completed) {
    const overlay = document.getElementById('loadingOverlay');
    const wrap = document.getElementById('loadingFileProgress');
    const count = document.getElementById('loadingFileCount');
    const pct = document.getElementById('loadingFilePct');
    const fill = document.getElementById('loadingFileBarFill');
    const fname = document.getElementById('loadingFileName');
    if (!overlay || !wrap || !total) return;
    overlay.classList.add('has-batch');
    wrap.classList.add('active');
    const done = completed != null ? completed : Math.max(0, (index || 0) - 1);
    const percent = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
    if (count) {
        count.textContent = index
            ? ('Файл ' + index + ' из ' + total)
            : ('0 из ' + total);
    }
    if (pct) pct.textContent = percent + '%';
    if (fill) fill.style.width = percent + '%';
    if (fname) fname.textContent = name ? ('«' + name + '»') : 'Ожидание файлов';
}
function clearBatchProgress() {
    const overlay = document.getElementById('loadingOverlay');
    const wrap = document.getElementById('loadingFileProgress');
    const fill = document.getElementById('loadingFileBarFill');
    if (overlay) overlay.classList.remove('has-batch');
    if (wrap) wrap.classList.remove('active');
    if (fill) fill.style.width = '0%';
}
function setLoadingHeadline(text, subtext) {
    const title = document.getElementById('loadingText');
    if (title && text) title.textContent = text;
    const sub = document.getElementById('loadingSubtext');
    if (sub) sub.textContent = subtext || '';
}
function renderLoadingStages(stages) {
    const el = document.getElementById('loadingStages');
    if (!el) return;
    if (!stages || !stages.length) {
        el.classList.remove('active');
        el.innerHTML = '';
        return;
    }
    el.classList.add('active');
    el.innerHTML = stages.map(function(s) {
        const status = s.status || 'pending';
        const detail = s.detail ? '<span class="loading-stage-detail">' + s.detail + '</span>' : '<span class="loading-stage-detail"></span>';
        return '<div class="loading-stage ' + status + '" data-key="' + s.key + '">'
            + '<span class="loading-stage-mark"></span>'
            + '<span class="loading-stage-copy"><span class="loading-stage-label">' + (s.label || '') + '</span>' + detail + '</span>'
            + '</div>';
    }).join('');
}
function activateLoadingStage(key, title, detail) {
    const el = document.getElementById('loadingStages');
    if (!el) return;
    const items = Array.from(el.querySelectorAll('.loading-stage'));
    if (!items.length) return;
    let found = false;
    items.forEach(function(item) {
        if (item.dataset.key === key) {
            found = true;
            item.className = 'loading-stage current';
            const label = item.querySelector('.loading-stage-label');
            if (label && title) label.textContent = title;
            const d = item.querySelector('.loading-stage-detail');
            if (d) d.textContent = detail || '';
        } else if (!found) {
            item.className = 'loading-stage done';
        } else if (!item.classList.contains('done')) {
            item.className = 'loading-stage pending';
        }
    });
    if (found) setLoadingHeadline(title || key, detail || '');
}
function submitWithLoading(form, text, subtext) {
    const textarea = form.querySelector('textarea');
    const fileInput = form.querySelector('input[type="file"]');
    if (textarea && !textarea.value.trim()) {
        textarea.focus();
        return false;
    }
    if (fileInput && fileInput.files.length === 0) {
        return false;
    }
    showLoading(text, subtext);
    return true;
}
async function readSseEvents(resp, onEvent) {
    if (!resp.ok || !resp.body) return false;
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split('\\n\\n');
        buf = parts.pop() || '';
        for (const part of parts) {
            const line = part.split('\\n').find(function(l) { return l.startsWith('data: '); });
            if (!line) continue;
            let data;
            try { data = JSON.parse(line.slice(6)); } catch (err) { continue; }
            onEvent(data);
        }
    }
    return true;
}

// Enter to submit
document.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        const active = document.activeElement;
        if (active && active.tagName === 'TEXTAREA') {
            const form = active.closest('form');
            if (form && form.dataset.enterSubmit === 'true') {
                e.preventDefault();
                form.requestSubmit();
            }
        }
    }
});

// Particles
(function() {
    const container = document.querySelector('.particles');
    if (!container) return;
    for (let i = 0; i < 15; i++) {
        const p = document.createElement('div');
        p.className = 'particle';
        p.style.left = Math.random() * 100 + '%';
        p.style.animationDelay = Math.random() * 15 + 's';
        p.style.animationDuration = (12 + Math.random() * 8) + 's';
        container.appendChild(p);
    }
})();

// Mouse haze trail
(function() {
    const layer = document.getElementById('sprayLayer');
    if (!layer) return;
    if (window.matchMedia('(pointer: coarse)').matches) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    const follow = document.createElement('div');
    follow.className = 'spray-follow';
    layer.appendChild(follow);

    let lastSpawn = 0;
    let tx = 0, ty = 0, cx = 0, cy = 0, hasMove = false;

    function spawnHaze(x, y) {
        const puff = document.createElement('div');
        puff.className = 'spray-dot';
        const angle = Math.random() * Math.PI * 2;
        const radius = 6 + Math.random() * 18;
        const size = 80 + Math.random() * 70;
        puff.style.left = x + 'px';
        puff.style.top = y + 'px';
        puff.style.width = size + 'px';
        puff.style.height = size + 'px';
        puff.style.setProperty('--dx', (Math.cos(angle) * radius).toFixed(2) + 'px');
        puff.style.setProperty('--dy', (Math.sin(angle) * radius - 8).toFixed(2) + 'px');
        layer.appendChild(puff);
        puff.addEventListener('animationend', () => puff.remove(), { once: true });
    }

    window.addEventListener('mousemove', function(e) {
        tx = e.clientX;
        ty = e.clientY;
        if (!hasMove) {
            cx = tx;
            cy = ty;
            hasMove = true;
            follow.style.opacity = '1';
        }
        const now = performance.now();
        if (now - lastSpawn < 42) return;
        lastSpawn = now;
        spawnHaze(e.clientX, e.clientY);
    });

    function tick() {
        if (hasMove) {
            cx += (tx - cx) * 0.14;
            cy += (ty - cy) * 0.14;
            follow.style.left = cx + 'px';
            follow.style.top = cy + 'px';
        }
        requestAnimationFrame(tick);
    }
    tick();
})();

(function() {
    if (document.querySelector('.status-dot.indexed, .status-dot.linking')) {
        setTimeout(function() { location.reload(); }, 3000);
    }
})();
"""


def html_page(title: str, body: str, extra_js: str = "", show_theme_toggle: bool = True) -> str:
    theme_btn = '<button class="theme-toggle" onclick="toggleTheme()">🌙</button>' if show_theme_toggle else ''
    return f"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script>document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="particles"></div>
<div id="sprayLayer" class="spray-layer"></div>
{theme_btn}
<div id="loadingOverlay" class="loading-overlay">
    <div class="loading-box">
        <div class="spinner"></div>
        <div class="loading-text"><span id="loadingText">Обработка</span><span class="loading-dots"></span></div>
        <div class="loading-subtext" id="loadingSubtext"></div>
        <div class="loading-file-progress" id="loadingFileProgress">
            <div class="loading-file-meta">
                <span class="loading-file-count" id="loadingFileCount">Файл 0 из 0</span>
                <span id="loadingFilePct">0%</span>
            </div>
            <div class="loading-file-bar"><div class="loading-file-bar-fill" id="loadingFileBarFill"></div></div>
            <div class="loading-file-name" id="loadingFileName"></div>
        </div>
        <div class="loading-stages" id="loadingStages"></div>
        <div class="loading-progress"><div class="loading-progress-bar"></div></div>
        <div class="loading-hint" id="loadingHint">Можно закрыть страницу — обработка продолжится в фоне. На проекте загорится зелёный кружок, когда граф будет готов.</div>
    </div>
</div>
{body}
<script>{JS_COMMON}{extra_js}</script>
</body>
</html>"""


# ========== PROJECTS HUB ==========

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, msg: str = "", st: str = ""):
    projects = [with_live_ingest(p) for p in sync_projects_from_graph()]
    counts = {}
    total_all = 0
    active_count = 0
    archived_count = 0
    try:
        for p in projects:
            n = linker.repository.total_count(p["graph_id"])
            counts[p["slug"]] = n
            if p.get("archived"):
                archived_count += 1
            else:
                active_count += 1
                total_all += n
    except Exception as e:
        print(f"[web_app_v2] stats warning: {e}")

    alert = ""
    if msg:
        cls = "alert-success" if st == "ok" else "alert-error"
        alert = f'<div class="alert {cls}">{escape(msg)}</div>'

    cards = ""
    for p in sorted(projects, key=lambda x: (bool(x.get("archived")), (x.get("name") or "").lower())):
        slug = p["slug"]
        n = counts.get(slug, 0)
        color = project_accent(slug)
        archived = bool(p.get("archived"))
        desc = (p.get("description") or "").strip()
        desc_html = f'<div class="desc">{escape(desc)}</div>' if desc else ""
        badge = '<span class="badge">архив</span>' if archived else ""
        search_blob = escape(f"{p.get('name', slug)} {desc}".lower())
        name = p.get("name") or slug
        cards += f"""
        <div class="project-card{' archived' if archived else ''}" data-name="{search_blob}" data-archived="{'1' if archived else '0'}" style="--dot:{color}">
            <button type="button" class="icon-edit" title="Редактировать" aria-label="Редактировать проект"
                data-slug="{escape(slug)}" data-name="{escape(name)}" data-desc="{escape(desc)}">{EDIT_ICON}</button>
            <a class="project-card-link" href="/p/{escape(slug)}">
                <h3><span>{escape(name)}</span>{badge}</h3>
                {desc_html}
                <p>{n} сущности(ей) в графе</p>
                {ingest_status_html(p.get("ingest_status") or "ready", error=p.get("ingest_error") or "")}
            </a>
        </div>
        """
    cards += """
        <button type="button" class="project-card create-card" id="openCreateModal" aria-label="Создать новый проект">
            <span class="create-plus">+</span>
            <span class="create-label">Создать новый проект</span>
        </button>
    """

    archive_toggle = ""
    if archived_count:
        archive_toggle = f"""
        <div class="hub-tools">
            <label><input type="checkbox" id="showArchived"> Показать архив ({archived_count})</label>
        </div>
        """

    body = f"""
    <div class="container hub">
        <div class="hub-hero">
            <h1>BVA Exocortex</h1>
            <p>Пространство проектов: у каждого свой граф знаний, общий слой собирает всё вместе.</p>
        </div>
        {alert}
        <input class="hub-search" id="hubSearch" type="search" placeholder="Найти проект по названию или описанию..." />

        <div class="hub-actions">
            <a class="common-card" href="/p/{COMMON_SLUG}">
                <h2>Общий граф</h2>
                <p>Единый слой по всем активным проектам. Здесь можно искать и смотреть связи, но нельзя добавлять или удалять данные.</p>
                <div class="common-meta">{active_count} проектов · {total_all} сущности(ей)</div>
            </a>
            <a class="common-card contour-card" href="/contour">
                <h2>Совместный поиск</h2>
                <p>Соберите изолированный контур из нескольких проектов и задайте вопрос только по ним.</p>
                <div class="common-meta">до {CONTOUR_MAX_PROJECTS} проектов · только поиск</div>
            </a>
        </div>

        <div class="section-title">Проекты</div>
        {archive_toggle}
        <div class="project-grid" id="projectGrid">{cards}</div>
    </div>

    <div class="modal-overlay" id="createModal">
        <div class="modal-box">
            <h3>Новый проект</h3>
            <p>Название можно будет изменить позже.</p>
            <form action="/projects/create" method="post" onsubmit="showLoading('Создание проекта')">
                <div class="form-group">
                    <label>Название</label>
                    <input type="text" name="name" placeholder="Название проекта" required maxlength="80">
                </div>
                <div class="form-group">
                    <label>Краткое описание</label>
                    <textarea name="description" placeholder="О чём этот проект (необязательно)" maxlength="{MAX_PROJECT_DESC}"></textarea>
                </div>
                <div class="modal-btns">
                    <button type="button" class="cancel" onclick="closeModal('createModal')">Отмена</button>
                    <button type="submit" class="confirm" style="background:linear-gradient(135deg,var(--accent),var(--accent2))">Создать</button>
                </div>
            </form>
        </div>
    </div>

    <div class="modal-overlay" id="editModal">
        <div class="modal-box">
            <h3>Редактировать проект</h3>
            <form id="editForm" method="post">
                <input type="hidden" name="next" value="/">
                <div class="form-group">
                    <label>Название</label>
                    <input type="text" name="name" id="editName" required maxlength="80">
                </div>
                <div class="form-group">
                    <label>Описание</label>
                    <textarea name="description" id="editDesc" maxlength="{MAX_PROJECT_DESC}"></textarea>
                </div>
                <div class="modal-btns">
                    <button type="button" class="cancel" onclick="closeModal('editModal')">Отмена</button>
                    <button type="submit" class="confirm" style="background:linear-gradient(135deg,var(--accent),var(--accent2))">Сохранить</button>
                </div>
            </form>
        </div>
    </div>
    """
    js = """
    const search = document.getElementById('hubSearch');
    const grid = document.getElementById('projectGrid');
    const toggle = document.getElementById('showArchived');
    function applyHubFilters() {
        if (!grid) return;
        const q = search ? search.value.trim().toLowerCase() : '';
        const showArchived = toggle && toggle.checked;
        grid.querySelectorAll('.project-card:not(.create-card)').forEach(function(card) {
            const name = card.getAttribute('data-name') || '';
            const archived = card.getAttribute('data-archived') === '1';
            const match = !q || name.includes(q);
            card.style.display = match && (!archived || showArchived) ? '' : 'none';
        });
    }
    if (search) search.addEventListener('input', applyHubFilters);
    if (toggle) toggle.addEventListener('change', applyHubFilters);
    applyHubFilters();

    const createBtn = document.getElementById('openCreateModal');
    if (createBtn) createBtn.addEventListener('click', function() { openModal('createModal'); });

    document.querySelectorAll('.icon-edit').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            const form = document.getElementById('editForm');
            form.action = '/p/' + btn.getAttribute('data-slug') + '/edit';
            document.getElementById('editName').value = btn.getAttribute('data-name') || '';
            document.getElementById('editDesc').value = btn.getAttribute('data-desc') || '';
            openModal('editModal');
        });
    });
    """
    return HTMLResponse(html_page("Проекты — BVA Exocortex", body, js))


@app.post("/projects/create")
async def create_project(name: str = Form(""), description: str = Form("")):
    title = " ".join((name or "").split())
    if not title:
        return RedirectResponse("/?msg=Введите название проекта&st=err", status_code=303)
    if len(title) > 80:
        return RedirectResponse("/?msg=Название слишком длинное&st=err", status_code=303)

    desc = " ".join((description or "").split())
    if len(desc) > MAX_PROJECT_DESC:
        return RedirectResponse("/?msg=Описание слишком длинное&st=err", status_code=303)

    projects = sync_projects_from_graph()
    if any((p.get("name") or "").strip().lower() == title.lower() for p in projects):
        return RedirectResponse("/?msg=Проект с таким названием уже есть&st=err", status_code=303)

    slug = slugify_project(title)
    existing = {p["slug"] for p in projects}
    if slug in RESERVED_SLUGS or slug in existing:
        base = slug
        n = 2
        while f"{base}_{n}" in existing or f"{base}_{n}" in RESERVED_SLUGS:
            n += 1
        slug = f"{base}_{n}"

    projects.append({
        "slug": slug,
        "name": title,
        "graph_id": f"proj_{slug}",
        "description": desc,
        "archived": False,
        "ingest_status": "ready",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_projects(projects)
    return RedirectResponse(f"/p/{slug}", status_code=303)


@app.get("/home")
async def home_legacy():
    return RedirectResponse("/", status_code=303)


@app.get("/login")
async def login_legacy():
    return RedirectResponse("/", status_code=303)


@app.post("/login")
async def login_legacy_post():
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout_legacy():
    return RedirectResponse("/", status_code=303)


# ========== PROJECT HOME ==========

@app.get("/p/{slug}", response_class=HTMLResponse)
async def project_home(slug: str, msg: str = "", st: str = ""):
    scope = resolve_scope(slug)
    if not scope:
        return RedirectResponse("/?msg=Проект не найден&st=err", status_code=303)

    alert = ""
    if msg:
        cls = "alert-success" if st == "ok" else "alert-error"
        alert = f'<div class="alert {cls}">{escape(msg)}</div>'

    if scope["readonly"]:
        n = 0
        try:
            n = linker.repository.total_count_many(scope["graph_ids"])
        except Exception:
            pass
        body = f"""
        <div class="container">
            {project_nav(scope)}
            {alert}
            <div class="header">
                <h1>Общий граф</h1>
                <p>Сводный слой знаний по активным проектам</p>
            </div>
            <div class="readonly-banner">Этот граф только для навигации и поиска. Новые данные добавляйте в конкретный проект — они автоматически появятся здесь. Архивные проекты скрыты из общего слоя.</div>
            <div class="msg-box">Сейчас объединено {n} сущности(ей) из {len(scope["graph_ids"])} проектов.</div>
            <a href="/p/{COMMON_SLUG}/search" class="menu-btn"><span class="icon">🔍</span><span>Поиск по всем проектам</span></a>
            <a href="/contour" class="menu-btn"><span class="icon">🧩</span><span>Совместный поиск по выбранным проектам</span></a>
            <a href="/p/{COMMON_SLUG}/view" class="menu-btn"><span class="icon">💡</span><span>Посмотреть общий граф</span></a>
        </div>
        """
        return HTMLResponse(html_page("Общий граф", body))

    n = 0
    try:
        n = linker.repository.total_count(scope["graph_id"])
    except Exception:
        pass
    desc = scope.get("description") or ""
    archived = bool(scope.get("archived"))
    archived_banner = (
        '<div class="archived-banner">Проект в архиве: он скрыт из общего графа и поиска по всем проектам.</div>'
        if archived else ""
    )
    archive_action = "0" if archived else "1"
    archive_label = "Вернуть из архива" if archived else "В архив"
    desc_html = (
        f'<p class="project-desc">{escape(desc)}</p>'
        if desc
        else '<p class="project-desc empty">Нет описания — нажмите карандаш, чтобы добавить.</p>'
    )
    destroy_copy = (
        f'Это действие нельзя отменить. Проект «{escape(scope["name"])}» и все связанные сущности будут удалены.'
        if n > 0
        else f'Это действие нельзя отменить. Проект «{escape(scope["name"])}» будет удалён.'
    )
    body = f"""
    <div class="container">
        {project_nav(scope)}
        {alert}
        {archived_banner}
        <div class="header">
            <div class="title-row">
                <h1>{escape(scope["name"])}</h1>
                <button type="button" class="icon-edit inline" id="openEditModal" title="Редактировать" aria-label="Редактировать проект">{EDIT_ICON}</button>
            </div>
            {desc_html}
            <p class="project-count">📚 {n} сущности(ей) в проекте</p>
            <p class="project-count">{ingest_status_html(scope.get("ingest_status") or "ready", error=scope.get("ingest_error") or "")}</p>
            {('<p class="project-count">Новые сущности появятся в графе и поиске, когда статус станет зелёным.</p>' if scope.get("ingest_status") in {"indexed", "linking"} else "")}
        </div>
        <a href="/p/{escape(slug)}/add" class="menu-btn"><span class="icon">➕</span><span>Загрузить новые данные</span></a>
        <a href="/p/{escape(slug)}/search" class="menu-btn"><span class="icon">🔍</span><span>Поиск фрагментов по запросу</span></a>
        <a href="/p/{escape(slug)}/view" class="menu-btn"><span class="icon">💡</span><span>Посмотреть базу знаний</span></a>
        <a href="/p/{escape(slug)}/delete" class="menu-btn"><span class="icon">🗑</span><span>Удалить данные</span></a>
        <div class="meta-actions">
            <form action="/p/{escape(slug)}/archive" method="post">
                <input type="hidden" name="archived" value="{archive_action}">
                <button type="submit" class="btn-ghost">{archive_label}</button>
            </form>
            <form id="destroyForm" action="/p/{escape(slug)}/destroy" method="post">
                <button type="button" class="btn-ghost danger" onclick="openModal('destroyModal')">{"Удалить проект" if n > 0 else "Удалить пустой проект"}</button>
            </form>
        </div>
    </div>
    <div class="modal-overlay" id="editModal">
        <div class="modal-box">
            <h3>Редактировать проект</h3>
            <form action="/p/{escape(slug)}/edit" method="post">
                <div class="form-group">
                    <label>Название</label>
                    <input type="text" name="name" value="{escape(scope['name'])}" required maxlength="80">
                </div>
                <div class="form-group">
                    <label>Описание</label>
                    <textarea name="description" maxlength="{MAX_PROJECT_DESC}" placeholder="О чём этот проект">{escape(desc)}</textarea>
                </div>
                <div class="modal-btns">
                    <button type="button" class="cancel" onclick="closeModal('editModal')">Отмена</button>
                    <button type="submit" class="confirm" style="background:linear-gradient(135deg,var(--accent),var(--accent2))">Сохранить</button>
                </div>
            </form>
        </div>
    </div>
    <div class="modal-overlay" id="destroyModal">
        <div class="modal-box">
            <h3>🗑 Удалить проект?</h3>
            <p>{destroy_copy}</p>
            <div class="modal-btns">
                <button type="button" class="cancel" onclick="closeModal('destroyModal')">Отмена</button>
                <button type="button" class="confirm" onclick="confirmDestroy()">Удалить</button>
            </div>
        </div>
    </div>
    """
    js = """
    const openEdit = document.getElementById('openEditModal');
    if (openEdit) openEdit.addEventListener('click', function() { openModal('editModal'); });
    function confirmDestroy() {
        showLoading('Удаление проекта');
        document.getElementById('destroyForm').submit();
    }
    """
    return HTMLResponse(html_page(scope["name"], body, js))


@app.post("/p/{slug}/edit")
async def project_edit(
    slug: str,
    name: str = Form(""),
    description: str = Form(""),
    next: str = Form(""),
):
    scope, err = _writable_scope(slug)
    if err:
        return err
    dest = "/" if next.strip() in {"/", "hub"} else f"/p/{slug}"
    title = " ".join((name or "").split())
    if not title:
        return RedirectResponse(f"{dest}?msg=Введите название проекта&st=err", status_code=303)
    if len(title) > 80:
        return RedirectResponse(f"{dest}?msg=Название слишком длинное&st=err", status_code=303)
    desc = " ".join((description or "").split())
    if len(desc) > MAX_PROJECT_DESC:
        return RedirectResponse(f"{dest}?msg=Описание слишком длинное&st=err", status_code=303)
    projects = load_projects()
    if any(
        (p.get("name") or "").strip().lower() == title.lower() and p.get("slug") != slug
        for p in projects
    ):
        return RedirectResponse(f"{dest}?msg=Проект с таким названием уже есть&st=err", status_code=303)
    update_project(slug, name=title, description=desc)
    return RedirectResponse(f"{dest}?msg=Проект обновлён&st=ok", status_code=303)


@app.post("/p/{slug}/rename")
async def project_rename(slug: str, name: str = Form("")):
    scope, err = _writable_scope(slug)
    if err:
        return err
    title = " ".join((name or "").split())
    if not title:
        return RedirectResponse(f"/p/{slug}?msg=Введите название проекта&st=err", status_code=303)
    if len(title) > 80:
        return RedirectResponse(f"/p/{slug}?msg=Название слишком длинное&st=err", status_code=303)
    if title == scope["name"]:
        return RedirectResponse(f"/p/{slug}", status_code=303)
    projects = load_projects()
    if any(
        (p.get("name") or "").strip().lower() == title.lower() and p.get("slug") != slug
        for p in projects
    ):
        return RedirectResponse(f"/p/{slug}?msg=Проект с таким названием уже есть&st=err", status_code=303)
    update_project(slug, name=title)
    return RedirectResponse(f"/p/{slug}?msg=Проект переименован&st=ok", status_code=303)


@app.post("/p/{slug}/settings")
async def project_settings(slug: str, description: str = Form("")):
    scope, err = _writable_scope(slug)
    if err:
        return err
    desc = " ".join((description or "").split())
    if len(desc) > MAX_PROJECT_DESC:
        return RedirectResponse(f"/p/{slug}?msg=Описание слишком длинное&st=err", status_code=303)
    update_project(slug, description=desc)
    return RedirectResponse(f"/p/{slug}?msg=Описание сохранено&st=ok", status_code=303)


@app.post("/p/{slug}/archive")
async def project_archive(slug: str, archived: str = Form("1")):
    scope, err = _writable_scope(slug)
    if err:
        return err
    is_archived = archived.strip() not in {"0", "false", "no"}
    update_project(slug, archived=is_archived)
    msg = "Проект отправлен в архив" if is_archived else "Проект возвращён из архива"
    return RedirectResponse(f"/p/{slug}?msg={escape(msg)}&st=ok", status_code=303)


@app.post("/p/{slug}/destroy")
async def project_destroy(slug: str):
    scope, err = _writable_scope(slug)
    if err:
        return err
    n = 0
    try:
        n = linker.repository.total_count(scope["graph_id"])
    except Exception:
        pass
    try:
        linker.repository.delete_graph(scope["graph_id"])
    except Exception as e:
        print(f"[web_app_v2] delete graph warning: {e}")
        return RedirectResponse(f"/p/{slug}?msg=Не удалось удалить граф проекта&st=err", status_code=303)
    remove_project(slug)
    label = "пустой проект" if n == 0 else "проект и все его сущности"
    return RedirectResponse(f"/?msg=Удалён {label}: {escape(scope['name'])}&st=ok", status_code=303)


# ========== ADD ==========

@app.get("/p/{slug}/add", response_class=HTMLResponse)
async def add_page(slug: str, msg: str = "", st: str = ""):
    scope = resolve_scope(slug)
    if not scope:
        return RedirectResponse("/?msg=Проект не найден&st=err", status_code=303)
    if scope["readonly"]:
        return RedirectResponse(f"/p/{slug}?msg=В общий граф нельзя добавлять новые данные&st=err", status_code=303)

    alert = ""
    if msg:
        cls = "alert-success" if st == "ok" else "alert-error"
        alert = f'<div class="alert {cls}">{escape(msg)}</div>'

    body = f"""
    <div class="container">
        {project_nav(scope)}
        <a href="/p/{escape(slug)}" class="back-link">← В проект</a>
        <div class="page-header"><h2>➕ Загрузить новые данные</h2></div>
        
        {alert}
        
        <div class="msg-box">Отправьте текст, файл, путь к папке или ссылку Confluence — информация встроится в граф проекта.</div>
        
        <div class="tabs">
            <div class="tab active" onclick="showTab('text', this)">📝 Текст</div>
            <div class="tab" onclick="showTab('file', this)">📄 Файл</div>
            <div class="tab" onclick="showTab('confluence', this)">🔗 Confluence</div>
        </div>
        
        <div id="tab-text" class="tab-content active">
            <form action="/p/{escape(slug)}/add/text" method="post" data-enter-submit="true" onsubmit="return submitIngest(event, this, PIPELINES.text, 'Добавление новых данных', 'Готовим текст к разбору')">
                <div class="form-group"><textarea name="note_text" placeholder="Введите текст..." required></textarea></div>
                <button type="submit" class="btn">Сохранить</button>
            </form>
        </div>
        
        <div id="tab-file" class="tab-content">
            <div class="file-modes">
                <button type="button" class="file-mode active" data-mode="upload" onclick="setFileMode('upload', this)">Загрузить файл</button>
                <button type="button" class="file-mode" data-mode="folder" onclick="setFileMode('folder', this)">Путь к директории</button>
            </div>
            <div id="file-mode-upload">
                <form action="/p/{escape(slug)}/add/file" method="post" enctype="multipart/form-data" onsubmit="return submitFileIngest(event, this)">
                    <div class="form-group"><label>Файл (.pdf, .txt, .pptx, .ppt, .doc, .docx, .png, .jpg, .jpeg)</label><input type="file" name="file" accept=".pdf,.txt,.pptx,.ppt,.doc,.docx,.png,.jpg,.jpeg" required></div>
                    <button type="submit" class="btn">Обработать</button>
                </form>
            </div>
            <div id="file-mode-folder" style="display:none">
                <form id="folderForm" action="/p/{escape(slug)}/add/folder" method="post" onsubmit="return submitFolder(event, this)">
                    <div class="form-group">
                        <label>Извлечь из директории</label>
                        <input type="text" name="folder_path" placeholder="Вставьте путь до директории..." required>
                    </div>
                    <label class="tick-check">
                        <input type="checkbox" name="extract_child_content" value="1">
                        <span class="tick-box">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>
                        </span>
                        извлечь все дочерние файлы
                    </label>
                    <button type="submit" class="btn">Обработать</button>
                </form>
                <div class="folder-log" id="folderLog"></div>
            </div>
        </div>
        
        <div id="tab-confluence" class="tab-content">
            <form action="/p/{escape(slug)}/add/confluence" method="post" onsubmit="return submitIngest(event, this, PIPELINES.confluence, 'Загрузка Confluence', 'Подключаемся к странице')">
                <div class="form-group">
                    <label>Извлечь из страницы Confluence</label>
                    <input type="text" name="url" placeholder="Вставьте ссылку на страницу Confluence..." required>
                </div>
                <button type="submit" class="btn">Извлечь</button>
            </form>
        </div>
    </div>
    """

    js = """
    const PIPELINES = {
        text: [
            {key: 'prepare', label: 'Подготовка данных'},
            {key: 'mask', label: 'Обезличивание данных'},
            {key: 'atomize', label: 'Извлечение атомарных сущностей'},
            {key: 'link', label: 'Связывание в граф'}
        ],
        file: [
            {key: 'read', label: 'Чтение файла'},
            {key: 'mask', label: 'Обезличивание данных'},
            {key: 'atomize', label: 'Извлечение атомарных сущностей'},
            {key: 'link', label: 'Связывание в граф'}
        ],
        confluence: [
            {key: 'fetch', label: 'Загрузка страницы Confluence'},
            {key: 'read', label: 'Разбор содержимого'},
            {key: 'mask', label: 'Обезличивание данных'},
            {key: 'atomize', label: 'Извлечение атомарных сущностей'},
            {key: 'link', label: 'Связывание в граф'}
        ],
        folder: [
            {key: 'scan', label: 'Поиск файлов в директории'},
            {key: 'read', label: 'Чтение файла'},
            {key: 'mask', label: 'Обезличивание данных'},
            {key: 'atomize', label: 'Извлечение атомарных сущностей'},
            {key: 'link', label: 'Связывание в граф'}
        ]
    };
    function showTab(name, el) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        el.classList.add('active');
        document.getElementById('tab-' + name).classList.add('active');
    }
    function setFileMode(mode, el) {
        document.querySelectorAll('.file-mode').forEach(b => b.classList.remove('active'));
        el.classList.add('active');
        document.getElementById('file-mode-upload').style.display = mode === 'upload' ? '' : 'none';
        document.getElementById('file-mode-folder').style.display = mode === 'folder' ? '' : 'none';
    }
    function addPageUrl(form) {
        return form.action.replace(/\\/add\\/[^/]+$/, '/add');
    }
    function applyStage(data) {
        if (!data || data.type !== 'stage') return;
        activateLoadingStage(data.key, data.title, data.sub || '');
    }
    async function submitIngest(e, form, stages, title, subtext) {
        e.preventDefault();
        const textarea = form.querySelector('textarea');
        const textInp = form.querySelector('input[type="text"]');
        if (textarea && !textarea.value.trim()) { textarea.focus(); return false; }
        if (textInp && textInp.required && !textInp.value.trim()) { textInp.focus(); return false; }
        const btn = form.querySelector('button[type="submit"]');
        if (btn) btn.disabled = true;
        showLoading(title, subtext, stages);
        if (stages && stages[0]) activateLoadingStage(stages[0].key, stages[0].label, subtext || '');
        try {
            const resp = await fetch(form.action, {
                method: 'POST',
                body: new FormData(form),
                headers: { 'Accept': 'text/event-stream' }
            });
            const ct = (resp.headers.get('content-type') || '');
            if (!resp.ok || !resp.body || !ct.includes('event-stream')) {
                hideLoading();
                if (btn) btn.disabled = false;
                if (resp.redirected) location.href = resp.url;
                return false;
            }
            let finished = false;
            await readSseEvents(resp, function(data) {
                if (data.type === 'stage') {
                    applyStage(data);
                } else if (data.type === 'result' || data.type === 'error') {
                    finished = true;
                    const ok = data.type === 'result' && data.ok;
                    const msg = data.message || (ok ? 'Готово' : 'Ошибка');
                    location.href = addPageUrl(form) + '?msg=' + encodeURIComponent(msg) + '&st=' + (ok ? 'ok' : 'err');
                }
            });
            if (!finished) {
                location.href = addPageUrl(form) + '?msg=' + encodeURIComponent(
                    'Обработка продолжается в фоне. На проекте загорится зелёный статус, когда граф будет готов.'
                ) + '&st=ok';
                return false;
            }
        } catch (err) {
            hideLoading();
        }
        if (btn) btn.disabled = false;
        return false;
    }
    function submitFileIngest(e, form) {
        const fileInput = form.querySelector('input[type="file"]');
        if (!fileInput || fileInput.files.length === 0) {
            if (fileInput) fileInput.focus();
            return false;
        }
        const name = fileInput.files[0].name;
        return submitIngest(e, form, PIPELINES.file, 'Обработка файла', '«' + name + '»');
    }
    function appendFolderLog(text, cls) {
        const log = document.getElementById('folderLog');
        if (!log) return;
        log.classList.add('active');
        const line = document.createElement('div');
        if (cls) line.className = cls;
        line.textContent = text;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
    }
    async function submitFolder(e, form) {
        e.preventDefault();
        const inp = form.querySelector('input[name="folder_path"]');
        if (!inp || !inp.value.trim()) {
            if (inp) inp.focus();
            return false;
        }
        const btn = form.querySelector('button[type="submit"]');
        if (btn) btn.disabled = true;
        const log = document.getElementById('folderLog');
        if (log) { log.innerHTML = ''; log.classList.add('active'); }
        showLoading('Обработка директории', 'Ищем файлы в папке', PIPELINES.folder);
        activateLoadingStage('scan', 'Поиск файлов в директории', 'Смотрим содержимое папки');
        appendFolderLog('Поиск файлов в директории...', 'log-info');
        let batchTotal = 0;
        let batchIndex = 0;
        try {
            const resp = await fetch(form.action, {
                method: 'POST',
                body: new FormData(form),
                headers: { 'Accept': 'text/event-stream' }
            });
            if (!resp.ok || !resp.body) {
                hideLoading();
                appendFolderLog('Не удалось начать обработку', 'log-err');
                if (btn) btn.disabled = false;
                return false;
            }
            await readSseEvents(resp, function(data) {
                if (data.type === 'stage') {
                    applyStage(data);
                } else if (data.type === 'start') {
                    batchTotal = data.total || 0;
                    appendFolderLog('Найдено файлов: ' + batchTotal, 'log-info');
                    setBatchProgress(0, batchTotal, '', 0);
                    setLoadingHeadline('Обработка файлов', 'Найдено ' + batchTotal);
                } else if (data.type === 'file_start') {
                    batchIndex = data.index || (batchIndex + 1);
                    batchTotal = data.total || batchTotal;
                    renderLoadingStages(PIPELINES.file);
                    setBatchProgress(batchIndex, batchTotal, data.name);
                    setLoadingHeadline('Обработка файлов', 'Файл ' + batchIndex + ' из ' + batchTotal);
                    appendFolderLog('Выполняется обработка файла «' + data.name + '»', 'log-info');
                } else if (data.type === 'file') {
                    const idx = data.index || batchIndex;
                    const tot = data.total || batchTotal;
                    setBatchProgress(idx, tot, data.name, idx);
                    appendFolderLog(
                        data.ok
                            ? 'Файл «' + data.name + '» обработан'
                            : 'Файл «' + data.name + '»: ' + (data.message || 'ошибка'),
                        data.ok ? 'log-ok' : 'log-err'
                    );
                } else if (data.type === 'error') {
                    appendFolderLog(data.message || 'Ошибка', 'log-err');
                } else if (data.type === 'done') {
                    if (batchTotal) setBatchProgress(batchTotal, batchTotal, '', batchTotal);
                    appendFolderLog('Готово. Успешно: ' + data.ok + ', с ошибкой: ' + data.fail, data.fail ? 'log-info' : 'log-ok');
                }
            });
        } catch (err) {
            appendFolderLog('Соединение закрыто. Обработка продолжается в фоне.', 'log-info');
        }
        hideLoading();
        if (btn) btn.disabled = false;
        return false;
    }
    """
    return HTMLResponse(html_page("Добавить", body, js))


def _writable_scope(slug: str):
    scope = resolve_scope(slug)
    if not scope:
        return None, RedirectResponse("/?msg=Проект не найден&st=err", status_code=303)
    if scope["readonly"]:
        return None, RedirectResponse(f"/p/{slug}?msg=В общем граф нельзя добавлять или удалять данные&st=err", status_code=303)
    return scope, None


def _wants_sse(request: Request) -> bool:
    return "text/event-stream" in (request.headers.get("accept") or "").lower()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_response(generate):
    return StreamingResponse(
        generate,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_queue_job(run_sync):
    loop = asyncio.get_running_loop()
    q: queue.Queue = queue.Queue()

    def runner():
        try:
            run_sync(q)
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)

    _ingest_pool.submit(runner)

    async def generate():
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is None:
                break
            yield _sse(item)

    return _sse_response(generate())


def _stage_put(q: queue.Queue):
    def on_stage(key: str, title: str, sub: str = "") -> None:
        q.put({"type": "stage", "key": key, "title": title, "sub": sub})

    return on_stage


@app.post("/p/{slug}/add/text")
async def add_text(slug: str, request: Request, note_text: str = Form("")):
    scope, err = _writable_scope(slug)
    if err:
        return err
    text = note_text.strip()
    if not text:
        return RedirectResponse(f"/p/{slug}/add?msg=Введите текст&st=err", status_code=303)

    def work(on_stage):
        if on_stage:
            on_stage("prepare", "Подготовка данных", "Проверяем текст")
        return run_ingest(slug, scope["graph_id"], text, "text", "text_artifact", text, on_stage=on_stage)

    if not _wants_sse(request):
        _queue_ingest(slug, scope["graph_id"], text, "text", "text_artifact", text)
        return RedirectResponse(f"/p/{slug}/add?msg={escape(INGEST_ACCEPTED_MSG)}&st=ok", status_code=303)

    def run(q):
        ok, ans = work(_stage_put(q))
        q.put({"type": "result", "ok": bool(ok), "message": ans})

    return await _stream_queue_job(run)


@app.post("/p/{slug}/add/file")
async def add_file(slug: str, request: Request, file: UploadFile = File(None)):
    scope, err = _writable_scope(slug)
    if err:
        return err

    if not file or not file.filename:
        return RedirectResponse(f"/p/{slug}/add?msg=Выберите файл&st=err", status_code=303)

    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_UPLOAD_EXTS:
        return RedirectResponse(
            f"/p/{slug}/add?msg=Поддерживаются .pdf, .txt, .pptx, .ppt, .doc, .docx, .png, .jpg, .jpeg&st=err",
            status_code=303,
        )

    filename = file.filename
    fmt = _file_format_label(filename)
    tmp = Path(tempfile.gettempdir()) / f"web_{uuid.uuid4().hex}{ext}"
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    def work(on_stage):
        try:
            if on_stage:
                on_stage("read", f"Чтение {fmt}", f"«{filename}»")
            text = extract_file_text(str(tmp))
            if not (text or "").strip():
                return False, "Текст не извлечен"
            return run_ingest(
                slug,
                scope["graph_id"],
                text,
                filename,
                ext[1:].upper(),
                f"[{filename}]",
                on_stage=on_stage,
            )
        except FileTooLargeError as e:
            log_event(slug, f"[{filename}]", ext[1:].upper(), str(e))
            return False, str(e)
        except Exception as e:
            err = f"Не удалось прочитать файл: {e}"
            log_event(slug, f"[{filename}]", ext[1:].upper(), err)
            return False, err
        finally:
            tmp.unlink(missing_ok=True)

    if not _wants_sse(request):
        _ingest_pool.submit(work, None)
        return RedirectResponse(f"/p/{slug}/add?msg={escape(INGEST_ACCEPTED_MSG)}&st=ok", status_code=303)

    def run(q):
        ok, ans = work(_stage_put(q))
        q.put({"type": "result", "ok": bool(ok), "message": ans})

    return await _stream_queue_job(run)


@app.post("/p/{slug}/add/folder")
async def add_folder(
    slug: str,
    folder_path: str = Form(""),
    extract_child_content: str = Form(""),
):
    scope, err = _writable_scope(slug)
    if err:
        return err

    child = extract_child_content.strip().lower() in {"1", "on", "true", "yes"}
    uid = scope["graph_id"]

    def run(q):
        on_stage = _stage_put(q)
        on_stage("scan", "Поиск файлов в директории", "Смотрим содержимое папки")
        files, error = list_folder_files(folder_path, extract_child_content=child)
        if error:
            q.put({"type": "error", "message": error})
            q.put({"type": "done", "ok": 0, "fail": 0})
            return

        q.put({"type": "start", "total": len(files)})
        ok_n = 0
        fail_n = 0
        total = len(files)
        for i, path in enumerate(files, 1):
            name = Path(path).name
            fmt = _file_format_label(path)
            q.put({"type": "file_start", "name": name, "index": i, "total": total})
            try:
                on_stage("read", f"Чтение {fmt}", f"«{name}»")
                text = extract_file_text(path)
                if not (text or "").strip():
                    ok, ans = False, "Текст не извлечен"
                else:
                    ok, ans = run_ingest(
                        slug,
                        uid,
                        text,
                        os.path.abspath(path),
                        "folder",
                        f"[{name}]",
                        on_stage=on_stage,
                    )
            except FileTooLargeError as e:
                ok, ans = False, str(e)
            except Exception as e:
                ok, ans = False, str(e)
            if ok:
                ok_n += 1
            else:
                fail_n += 1
                log_event(slug, f"[{name}]", "folder", ans)
            q.put({"type": "file", "name": name, "ok": bool(ok), "message": ans, "index": i, "total": total})
        q.put({"type": "done", "ok": ok_n, "fail": fail_n})

    return await _stream_queue_job(run)


def _is_confluence_fetch_error(text: str) -> bool:
    t = (text or "").strip()
    return (
        not t
        or t.startswith("Ошибка URL:")
        or t.startswith("Отсутствует доступ к странице")
        or t.startswith("Произошла ошибка при загрузке страницы:")
    )


@app.post("/p/{slug}/add/confluence")
async def add_confluence(slug: str, request: Request, url: str = Form("")):
    scope, err = _writable_scope(slug)
    if err:
        return err
    page_url = url.strip()

    if not page_url:
        return RedirectResponse(f"/p/{slug}/add?msg=Вставьте ссылку на страницу Confluence&st=err", status_code=303)

    if CONFLUENCE_HOST not in page_url.lower():
        return RedirectResponse(
            f"/p/{slug}/add?msg={escape(f'Ссылка должна быть на Confluence и содержать в себе {CONFLUENCE_HOST}')}&st=err",
            status_code=303,
        )

    def work(on_stage):
        if on_stage:
            on_stage("fetch", "Загрузка страницы Confluence", "Запрашиваем содержимое")
        text = get_confluence_page_content(page_url)
        if _is_confluence_fetch_error(text):
            err_text = text.strip() or "Не удалось извлечь текст со страницы Confluence"
            log_event(slug, page_url, "confluence", err_text)
            return False, err_text
        if on_stage:
            on_stage("read", "Разбор содержимого", "Достаём текст со страницы")
        return run_ingest(slug, scope["graph_id"], text, page_url, "confluence", page_url, on_stage=on_stage)

    if not _wants_sse(request):
        _ingest_pool.submit(work, None)
        return RedirectResponse(f"/p/{slug}/add?msg={escape(INGEST_ACCEPTED_MSG)}&st=ok", status_code=303)

    def run(q):
        ok, ans = work(_stage_put(q))
        q.put({"type": "result", "ok": bool(ok), "message": ans})

    return await _stream_queue_job(run)


# ========== CONTOUR SEARCH ==========

@app.get("/contour", response_class=HTMLResponse)
async def contour_page(request: Request):
    active = active_search_projects()
    counts = {}
    for p in active:
        try:
            counts[p["slug"]] = linker.repository.total_count(p["graph_id"])
        except Exception:
            counts[p["slug"]] = 0

    catalog = [
        {
            "slug": p["slug"],
            "name": p.get("name") or p["slug"],
            "desc": (p.get("description") or "").strip(),
            "accent": project_accent(p["slug"]),
            "count": counts.get(p["slug"], 0),
        }
        for p in sorted(active, key=lambda x: (x.get("name") or "").lower())
    ]
    preselect = [s for s in request.query_params.getlist("p") if s]
    tiles = ""
    for item in catalog:
        desc = escape(item["desc"]) if item["desc"] else "Без описания"
        tiles += f"""
        <button type="button" class="contour-tile" data-slug="{escape(item['slug'])}" data-name="{escape((item['name'] + ' ' + item['desc']).lower())}" style="--dot:{item['accent']}">
            <span class="contour-check"></span>
            <h3>{escape(item['name'])}</h3>
            <div class="desc">{desc}</div>
            <div class="meta">{item['count']} сущности(ей)</div>
        </button>
        """
    if not catalog:
        tiles = '<div class="contour-wait">Сначала создайте хотя бы два активных проекта.</div>'

    body = f"""
    <div class="container contour">
        <a href="/" class="back-link">← Проекты</a>
        <div class="page-kicker">Изолированный контур · только поиск</div>
        <div class="page-header"><h2>Совместный поиск</h2></div>
        <div class="contour-banner">
            <h2>Графы не сливаются</h2>
            <p>Выберите от {CONTOUR_MIN_PROJECTS} до {CONTOUR_MAX_PROJECTS} проектов. Вопрос пойдёт в в базу знаний только по ним.</p>
        </div>
        <div class="contour-toolbar">
            <div class="contour-count" id="contourCount">Выбрано <strong>0</strong> из {CONTOUR_MAX_PROJECTS}</div>
            <button type="button" class="contour-reset" id="contourReset">Сбросить</button>
        </div>
        <input class="contour-search" id="contourSearch" type="search" placeholder="Найти проект по названию..." autocomplete="off">
        <div class="contour-rail" id="contourRail">
            <button type="button" class="contour-rail-btn prev" id="contourPrev" aria-label="Листать влево">‹</button>
            <div class="contour-pick" id="contourPick">{tiles}</div>
            <button type="button" class="contour-rail-btn next" id="contourNext" aria-label="Листать вправо">›</button>
        </div>
        <div class="contour-filter-empty" id="contourFilterEmpty">Нет проектов с таким названием</div>
        <div class="contour-selected" id="contourChips"></div>
        <div class="contour-wait" id="contourWait">Выберите <strong>ещё два проекта</strong>, чтобы задать вопрос по контуру.</div>
        <div class="chat-container" id="contourChat" style="display:none">
            <div class="chat-messages" id="chatMessages">
                <div class="chat-msg bot" id="contourHint"><p></p></div>
            </div>
            <form class="chat-input-wrap" id="searchForm" onsubmit="return sendMessage(event)">
                <textarea id="queryInput" name="q" placeholder="Вопрос только по выбранным проектам..." rows="1"></textarea>
                <button type="submit" class="btn" id="sendBtn">→</button>
            </form>
        </div>
    </div>
    """

    js = f"""
    const PROJECTS = {json.dumps(catalog, ensure_ascii=False)};
    const MAX = {CONTOUR_MAX_PROJECTS};
    const MIN = {CONTOUR_MIN_PROJECTS};
    const selected = new Set({json.dumps(preselect, ensure_ascii=False)});
    const pick = document.getElementById('contourPick');
    const searchEl = document.getElementById('contourSearch');
    const emptyEl = document.getElementById('contourFilterEmpty');
    const railEl = document.getElementById('contourRail');
    const countEl = document.getElementById('contourCount');
    const chipsEl = document.getElementById('contourChips');
    const waitEl = document.getElementById('contourWait');
    const chatEl = document.getElementById('contourChat');
    const hintEl = document.getElementById('contourHint');
    const chatMessages = document.getElementById('chatMessages');
    const queryInput = document.getElementById('queryInput');
    const sendBtn = document.getElementById('sendBtn');

    function bySlug(slug) {{
        return PROJECTS.find(function(p) {{ return p.slug === slug; }});
    }}
    function esc(s) {{
        return String(s).replace(/[&<>"']/g, function(c) {{
            return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];
        }});
    }}
    function selectedList() {{
        return PROJECTS.filter(function(p) {{ return selected.has(p.slug); }});
    }}
    function remaining() {{
        const n = selected.size;
        if (n >= MIN) return 0;
        return MIN - n;
    }}
    function syncUrl() {{
        const params = new URLSearchParams();
        selectedList().forEach(function(p) {{ params.append('p', p.slug); }});
        const q = params.toString();
        history.replaceState(null, '', q ? ('/contour?' + q) : '/contour');
    }}
    function render() {{
        const q = (searchEl && searchEl.value || '').trim().toLowerCase();
        const items = selectedList();
        let visible = 0;
        pick.querySelectorAll('.contour-tile').forEach(function(btn) {{
            const slug = btn.getAttribute('data-slug');
            const name = btn.getAttribute('data-name') || '';
            const match = !q || name.indexOf(q) !== -1;
            btn.classList.toggle('hidden', !match);
            if (match) visible += 1;
            const on = selected.has(slug);
            btn.classList.toggle('selected', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            const locked = !on && selected.size >= MAX;
            btn.classList.toggle('disabled', locked);
        }});
        const order = items.map(function(p) {{ return p.slug; }});
        const tiles = Array.from(pick.querySelectorAll('.contour-tile'));
        tiles.sort(function(a, b) {{
            const as = selected.has(a.getAttribute('data-slug')) ? 0 : 1;
            const bs = selected.has(b.getAttribute('data-slug')) ? 0 : 1;
            if (as !== bs) return as - bs;
            if (as === 0) {{
                return order.indexOf(a.getAttribute('data-slug')) - order.indexOf(b.getAttribute('data-slug'));
            }}
            return tiles.indexOf(a) - tiles.indexOf(b);
        }});
        tiles.forEach(function(btn) {{ pick.appendChild(btn); }});
        if (!q) pick.scrollLeft = 0;
        if (emptyEl) emptyEl.style.display = (q && !visible) ? '' : 'none';
        if (railEl) railEl.style.display = (!q || visible) ? '' : 'none';
        countEl.innerHTML = 'Выбрано <strong>' + selected.size + '</strong> из ' + MAX;
        chipsEl.innerHTML = items.map(function(p) {{
            return '<span class="contour-chip">' + esc(p.name) + '<button type="button" data-remove="' + esc(p.slug) + '" aria-label="Убрать">×</button></span>';
        }}).join('');
        const ready = selected.size >= MIN;
        waitEl.style.display = ready ? 'none' : '';
        chatEl.style.display = ready ? '' : 'none';
        const need = remaining();
        if (!ready) {{
            waitEl.innerHTML = need === 2
                ? 'Выберите <strong>ещё два проекта</strong>, чтобы задать вопрос по контуру.'
                : 'Выберите <strong>ещё один проект</strong>, чтобы открыть поиск.';
        }} else if (hintEl) {{
            const names = items.map(function(p) {{ return '«' + p.name + '»'; }}).join(', ');
            hintEl.innerHTML = '<p>Контур: ' + names + '. Спрашивайте только по этим проектам. Источники в ответе будут подписаны.</p>';
        }}
        syncUrl();
    }}
    pick.addEventListener('click', function(e) {{
        const btn = e.target.closest('.contour-tile');
        if (!btn || btn.classList.contains('disabled') || btn.classList.contains('hidden')) return;
        const slug = btn.getAttribute('data-slug');
        if (selected.has(slug)) selected.delete(slug);
        else if (selected.size < MAX) selected.add(slug);
        render();
    }});
    chipsEl.addEventListener('click', function(e) {{
        const btn = e.target.closest('[data-remove]');
        if (!btn) return;
        selected.delete(btn.getAttribute('data-remove'));
        render();
    }});
    if (searchEl) searchEl.addEventListener('input', render);
    document.getElementById('contourPrev').addEventListener('click', function() {{
        pick.scrollBy({{ left: -230, behavior: 'smooth' }});
    }});
    document.getElementById('contourNext').addEventListener('click', function() {{
        pick.scrollBy({{ left: 230, behavior: 'smooth' }});
    }});
    document.getElementById('contourReset').addEventListener('click', function() {{
        selected.clear();
        render();
    }});
    [...selected].forEach(function(slug) {{ if (!bySlug(slug)) selected.delete(slug); }});
    render();

    if (queryInput) {{
        queryInput.addEventListener('input', function() {{
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 120) + 'px';
        }});
        queryInput.addEventListener('keydown', function(e) {{
            if (e.key === 'Enter' && !e.shiftKey) {{
                e.preventDefault();
                sendMessage(e);
            }}
        }});
    }}

    async function sendMessage(e) {{
        e.preventDefault();
        if (selected.size < MIN) return false;
        const query = queryInput.value.trim();
        if (!query) {{ queryInput.focus(); return false; }}
        addMessage(query, 'user');
        queryInput.value = '';
        queryInput.style.height = 'auto';
        const loadingId = 'loading-' + Date.now();
        chatMessages.innerHTML += `<div class="chat-msg bot" id="${{loadingId}}">
            <div style="display:flex;align-items:center;gap:12px">
                <div class="spinner" style="width:20px;height:20px;margin:0;border-width:2px"></div>
                <span style="color:var(--muted)">Поиск по выбранному контуру...</span>
            </div>
        </div>`;
        chatMessages.scrollTop = chatMessages.scrollHeight;
        sendBtn.disabled = true;
        try {{
            const resp = await fetch('/contour/api/search', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{ q: query, projects: selectedList().map(function(p) {{ return p.slug; }}) }})
            }});
            const data = await resp.json();
            document.getElementById(loadingId).remove();
            if (data.error) addMessage('Ошибка: ' + data.error, 'bot');
            else addMessageHtml(data.answer_html, data.meta, data.sources, data.input_sources);
        }} catch (err) {{
            document.getElementById(loadingId).remove();
            addMessage('Ошибка соединения', 'bot');
        }}
        sendBtn.disabled = false;
        queryInput.focus();
        return false;
    }}
    function addMessage(text, type) {{
        const div = document.createElement('div');
        div.className = 'chat-msg ' + type;
        div.textContent = text;
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }}
    function addMessageHtml(html, meta, sources, inputSources) {{
        const div = document.createElement('div');
        div.className = 'chat-msg bot';
        div.innerHTML = html;
        if (sources && sources.length) {{
            const wrap = document.createElement('div');
            wrap.className = 'source-chips';
            sources.forEach(function(name) {{
                const chip = document.createElement('span');
                chip.className = 'source-chip';
                chip.textContent = name;
                wrap.appendChild(chip);
            }});
            div.appendChild(wrap);
        }}
        if (inputSources && inputSources.length) {{
            const details = document.createElement('details');
            details.className = 'rag-sources';
            const summary = document.createElement('summary');
            summary.textContent = 'Посмотреть источники';
            details.appendChild(summary);
            const list = document.createElement('ul');
            inputSources.forEach(function(src) {{
                const li = document.createElement('li');
                const kind = document.createElement('span');
                kind.className = 'src-kind';
                kind.textContent = src.title || 'Источник';
                li.appendChild(kind);
                if (src.project) {{
                    const proj = document.createElement('span');
                    proj.className = 'src-project';
                    proj.textContent = src.project;
                    li.appendChild(proj);
                }}
                if (src.topic) {{
                    const topic = document.createElement('span');
                    topic.className = 'src-label';
                    topic.textContent = (src.luhmann_id ? '[' + src.luhmann_id + '] ' : '') + src.topic;
                    li.appendChild(topic);
                }}
                if (src.quote) {{
                    const quote = document.createElement('blockquote');
                    quote.className = 'src-quote';
                    quote.textContent = src.quote;
                    li.appendChild(quote);
                }}
                if (src.href) {{
                    const a = document.createElement('a');
                    a.href = src.href;
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    a.textContent = src.label || src.href;
                    li.appendChild(a);
                }} else {{
                    const span = document.createElement('span');
                    span.className = 'src-label';
                    span.textContent = src.label || '';
                    li.appendChild(span);
                }}
                list.appendChild(li);
            }});
            details.appendChild(list);
            div.appendChild(details);
        }}
        if (meta) {{
            const metaDiv = document.createElement('div');
            metaDiv.className = 'meta';
            metaDiv.textContent = meta;
            div.appendChild(metaDiv);
        }}
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }}
    """
    return HTMLResponse(html_page("Совместный поиск", body, js))


@app.post("/contour/api/search")
async def contour_api_search(request: Request):
    try:
        data = await request.json()
        query = (data.get("q") or "").strip()
        slugs = data.get("projects") or []
    except Exception:
        return JSONResponse({"error": "Неверный формат"}, status_code=400)
    if not query:
        return JSONResponse({"error": "Введите запрос"}, status_code=400)
    if not isinstance(slugs, list):
        return JSONResponse({"error": "Выберите проекты"}, status_code=400)
    selected, err = resolve_contour_slugs([str(s) for s in slugs])
    if err:
        return JSONResponse({"error": err}, status_code=400)
    graph_ids = [p["graph_id"] for p in selected]
    labels = {p["graph_id"]: (p.get("name") or p["slug"]) for p in selected}
    return JSONResponse(rag_search_payload(query, graph_ids, labels, "contour"))


# ========== SEARCH (Chat style with formatting) ==========

@app.get("/p/{slug}/search", response_class=HTMLResponse)
async def search_page(slug: str):
    scope = resolve_scope(slug)
    if not scope:
        return RedirectResponse("/?msg=Проект не найден&st=err", status_code=303)

    hint = (
        "Задайте вопрос по всем активным проектам сразу. У каждой найденной сущности будет подпись, из какого проекта она пришла."
        if scope["readonly"]
        else "Задайте вопрос, и я найду релевантную информацию из графа этого проекта."
    )
    body = f"""
    <div class="container wide">
        {project_nav(scope)}
        <a href="/p/{escape(slug)}" class="back-link">← В проект</a>
        <div class="page-header"><h2>🔍 Поиск фрагментов</h2></div>
        {"<div class='readonly-banner'>Поиск идёт по объединённому графу. Добавлять данные здесь нельзя.</div>" if scope["readonly"] else ""}
        
        <div class="chat-container">
            <div class="chat-messages" id="chatMessages">
                <div class="chat-msg bot">
                    <p>{hint}</p>
                </div>
            </div>
            
            <form class="chat-input-wrap" id="searchForm" onsubmit="return sendMessage(event)">
                <textarea id="queryInput" name="q" placeholder="Введите вопрос..." rows="1"></textarea>
                <button type="submit" class="btn" id="sendBtn">→</button>
            </form>
        </div>
    </div>
    """

    js = f"""
    const chatMessages = document.getElementById('chatMessages');
    const queryInput = document.getElementById('queryInput');
    const sendBtn = document.getElementById('sendBtn');
    const searchUrl = '/p/{slug}/api/search';
    
    queryInput.addEventListener('input', function() {{
        this.style.height = 'auto';
        this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    }});
    
    queryInput.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter' && !e.shiftKey) {{
            e.preventDefault();
            sendMessage(e);
        }}
    }});
    
    async function sendMessage(e) {{
        e.preventDefault();
        const query = queryInput.value.trim();
        if (!query) {{
            queryInput.focus();
            return false;
        }}
        
        addMessage(query, 'user');
        queryInput.value = '';
        queryInput.style.height = 'auto';
        
        const loadingId = 'loading-' + Date.now();
        chatMessages.innerHTML += `<div class="chat-msg bot" id="${{loadingId}}">
            <div style="display:flex;align-items:center;gap:12px">
                <div class="spinner" style="width:20px;height:20px;margin:0;border-width:2px"></div>
                <span style="color:var(--muted)">Поиск по базе знаний...</span>
            </div>
        </div>`;
        chatMessages.scrollTop = chatMessages.scrollHeight;
        sendBtn.disabled = true;
        
        try {{
            const resp = await fetch(searchUrl, {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{q: query}})
            }});
            const data = await resp.json();
            
            document.getElementById(loadingId).remove();
            
            if (data.error) {{
                addMessage('Ошибка: ' + data.error, 'bot');
            }} else {{
                addMessageHtml(data.answer_html, data.meta, data.sources, data.input_sources);
            }}
        }} catch (err) {{
            document.getElementById(loadingId).remove();
            addMessage('Ошибка соединения', 'bot');
        }}
        
        sendBtn.disabled = false;
        queryInput.focus();
        return false;
    }}
    
    function addMessage(text, type) {{
        const div = document.createElement('div');
        div.className = 'chat-msg ' + type;
        div.textContent = text;
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }}
    
    function addMessageHtml(html, meta, sources, inputSources) {{
        const div = document.createElement('div');
        div.className = 'chat-msg bot';
        div.innerHTML = html;
        if (sources && sources.length) {{
            const wrap = document.createElement('div');
            wrap.className = 'source-chips';
            sources.forEach(function(name) {{
                const chip = document.createElement('span');
                chip.className = 'source-chip';
                chip.textContent = name;
                wrap.appendChild(chip);
            }});
            div.appendChild(wrap);
        }}
        if (inputSources && inputSources.length) {{
            const details = document.createElement('details');
            details.className = 'rag-sources';
            const summary = document.createElement('summary');
            summary.textContent = 'Посмотреть источники';
            details.appendChild(summary);
            const list = document.createElement('ul');
            inputSources.forEach(function(src) {{
                const li = document.createElement('li');
                const kind = document.createElement('span');
                kind.className = 'src-kind';
                kind.textContent = src.title || 'Источник';
                li.appendChild(kind);
                if (src.project) {{
                    const proj = document.createElement('span');
                    proj.className = 'src-project';
                    proj.textContent = src.project;
                    li.appendChild(proj);
                }}
                if (src.topic) {{
                    const topic = document.createElement('span');
                    topic.className = 'src-label';
                    topic.textContent = (src.luhmann_id ? '[' + src.luhmann_id + '] ' : '') + src.topic;
                    li.appendChild(topic);
                }}
                if (src.quote) {{
                    const quote = document.createElement('blockquote');
                    quote.className = 'src-quote';
                    quote.textContent = src.quote;
                    li.appendChild(quote);
                }}
                if (src.href) {{
                    const a = document.createElement('a');
                    a.href = src.href;
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    a.textContent = src.label || src.href;
                    li.appendChild(a);
                }} else {{
                    const span = document.createElement('span');
                    span.className = 'src-label';
                    span.textContent = src.label || '';
                    li.appendChild(span);
                }}
                list.appendChild(li);
            }});
            details.appendChild(list);
            div.appendChild(details);
        }}
        if (meta) {{
            const metaDiv = document.createElement('div');
            metaDiv.className = 'meta';
            metaDiv.textContent = meta;
            div.appendChild(metaDiv);
        }}
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }}
    """
    return HTMLResponse(html_page("Поиск", body, js))


@app.post("/p/{slug}/api/search")
async def api_search(slug: str, request: Request):
    scope = resolve_scope(slug)
    if not scope:
        return JSONResponse({"error": "Проект не найден"}, status_code=404)

    try:
        data = await request.json()
        query = data.get("q", "").strip()
    except Exception:
        return JSONResponse({"error": "Неверный формат"}, status_code=400)

    if not query:
        return JSONResponse({"error": "Введите запрос"}, status_code=400)

    labels = scope.get("project_labels") or {}
    if scope["readonly"]:
        payload = rag_search_payload(query, scope["graph_ids"], labels, slug)
        return JSONResponse(payload)

    graph_key = scope["graph_id"]
    resp = graphrag.query(graph_key, query)
    log_event(slug, query, "search_query", resp.answer)
    formatted = format_llm_response(resp.answer)
    meta = (
        f"⏱ {resp.processing_time_ms}ms · {len(resp.context.entry_points)} точек · "
        f"{len(resp.context.expanded_nodes)} узлов"
    )
    input_sources = collect_input_sources(resp.context.all_nodes, labels)
    return JSONResponse({
        "answer_html": formatted,
        "meta": meta,
        "sources": [],
        "input_sources": input_sources,
    })


# ========== VIEW ==========

def _graph_page(request: Request, user_label: str, data_url: str):
    body, headers = encode_graph_html(
        render_graph_html(None, user_label=user_label, data_url=data_url),
        request.headers.get("accept-encoding", ""),
    )
    return Response(content=body, headers=headers)


@app.get("/p/{slug}/view", response_class=HTMLResponse)
async def view_page(slug: str, request: Request):
    scope = resolve_scope(slug)
    if not scope:
        return RedirectResponse("/?msg=Проект не найден&st=err", status_code=303)

    if scope["readonly"]:
        graph_ids = scope["graph_ids"]
        n = linker.repository.total_count_many(graph_ids)
        if n == 0:
            body = f"""
            <div class="container">
                {project_nav(scope)}
                <a href="/p/{escape(slug)}" class="back-link">← В проект</a>
                <div class="page-header"><h2>💡 Общий граф</h2></div>
                <div class="alert alert-error">📭 Пока нет сущностей ни в одном проекте.</div>
            </div>
            """
            return HTMLResponse(html_page("Общий граф", body))
        return _graph_page(request, scope["name"], f"/p/{slug}/api/graph")

    stats = linker.get_user_stats(scope["graph_id"])
    if stats["total_cards"] == 0:
        body = f"""
        <div class="container">
            {project_nav(scope)}
            <a href="/p/{escape(slug)}" class="back-link">← В проект</a>
            <div class="page-header"><h2>💡 База знаний</h2></div>
            <div class="alert alert-error">📭 Граф пуст. Загрузите первые данные!</div>
            <a href="/p/{escape(slug)}/add" class="btn" style="text-decoration:none;text-align:center;display:block;margin-top:16px">➕ Добавить данные</a>
        </div>
        """
        return HTMLResponse(html_page("База знаний", body))

    return _graph_page(request, scope["name"], f"/p/{slug}/api/graph")


@app.get("/p/{slug}/api/graph")
async def api_graph(slug: str, request: Request):
    scope = resolve_scope(slug)
    if not scope:
        return JSONResponse({"error": "Проект не найден"}, status_code=404)
    if scope["readonly"]:
        labels = scope.get("project_labels") or {}
        data = linker.repository.export_graph_data_combined(scope["graph_ids"], labels)
    else:
        data = linker.repository.export_graph_data(scope["graph_id"])
    payload = pack_graph_payload(data)
    body, headers = encode_graph_payload(payload, request.headers.get("accept-encoding", ""))
    return Response(content=body, headers=headers)


# ========== DELETE (Card-based) ==========

@app.get("/p/{slug}/delete", response_class=HTMLResponse)
async def delete_page(slug: str, msg: str = "", st: str = ""):
    scope, err = _writable_scope(slug)
    if err:
        return err

    alert = ""
    if msg:
        cls = "alert-success" if st == "ok" else "alert-error"
        alert = f'<div class="alert {cls}">{escape(msg)}</div>'

    body = f"""
    <div class="container">
        {project_nav(scope)}
        <a href="/p/{escape(slug)}" class="back-link">← В проект</a>
        <div class="page-header"><h2>🗑 Удалить данные</h2></div>
        
        {alert}
        
        <div class="msg-box">Опишите фрагмент, который хотите найти и удалить.</div>
        
        <form action="/p/{escape(slug)}/delete/search" method="post" data-enter-submit="true" data-loading-text="Поиск" data-loading-subtext="Ищем похожие фрагменты" onsubmit="return submitWithLoading(this, 'Поиск заметок', 'Ищем похожие фрагменты в базе знаний')">
            <div class="form-group"><textarea name="q" placeholder="Что удалить..." required></textarea></div>
            <button type="submit" class="btn">Найти</button>
        </form>
    </div>
    """
    return HTMLResponse(html_page("Удалить", body))


@app.post("/p/{slug}/delete/search", response_class=HTMLResponse)
async def delete_search(slug: str, q: str = Form("")):
    scope, err = _writable_scope(slug)
    if err:
        return err
    uid = scope["graph_id"]
    query = q.strip()

    if not query:
        return RedirectResponse(f"/p/{slug}/delete?msg=Введите запрос&st=err", status_code=303)

    emb = embedding_model.embed_query(query)
    threshold = max(0.35, settings.linker_similarity_threshold)
    cands = linker.repository.vector_search(user_id=uid, query_embedding=emb, limit=5, similarity_threshold=threshold)

    if not cands:
        log_event(slug, query, "delete_query", "Не найдено")
        return RedirectResponse(f"/p/{slug}/delete?msg=Ничего не найдено. Попробуйте другую формулировку.&st=err", status_code=303)

    token = uuid.uuid4().hex
    cached = []
    cards_html = ""

    for i, (node, score) in enumerate(cands):
        topic = getattr(node, 'topic', '') or ''
        cached.append({
            "zettel_id": node.zettel_id,
            "luhmann_id": node.luhmann_id,
            "topic": topic,
            "content": node.content
        })
        if topic:
            short_content = node.content[:80] + ("..." if len(node.content) > 80 else "")
            preview = f"<strong>{escape(topic)}</strong>: {escape(short_content)}"
        else:
            preview = escape(node.content[:120]) + ("..." if len(node.content) > 120 else "")
        full_content = escape(node.content).replace("\n", "<br>")
        topic_escaped = escape(topic) if topic else ""

        cards_html += f"""
        <div class="delete-card" onclick="showDeleteModal({i}, '{escape(node.luhmann_id)}', `{full_content}`, `{topic_escaped}`)">
            <div class="card-header">
                <span class="card-id">[{escape(node.luhmann_id)}]</span>
                <span class="card-score">{score:.0%} совпадение</span>
            </div>
            <div class="card-preview">{preview}</div>
        </div>
        """

    DELETE_CACHE[token] = cached

    body = f"""
    <div class="container">
        {project_nav(scope)}
        <a href="/p/{escape(slug)}" class="back-link">← В проект</a>
        <div class="page-header"><h2>🗑 Удалить данные</h2></div>
        
        <div class="msg-box">Найдено {len(cached)} заметок. Нажмите на карточку, чтобы просмотреть и удалить.</div>
        
        <div class="delete-cards">{cards_html}</div>
        
        <form id="deleteForm" action="/p/{escape(slug)}/delete/confirm" method="post" style="display:none">
            <input type="hidden" name="token" value="{token}">
            <input type="hidden" name="idx" id="deleteIdx">
        </form>
        
        <div id="deleteModal" class="modal-overlay">
            <div class="modal-box">
                <h3>🗑 Удалить эту сущность?</h3>
                <div class="card-id" id="modalCardId"></div>
                <div class="modal-topic" id="modalTopic" style="font-weight:600;color:var(--accent);margin:8px 0;font-size:15px"></div>
                <div class="quote" id="modalQuote"></div>
                <p>Это действие нельзя отменить. Сущность и все связанные данные будут удалены.</p>
                <div class="modal-btns">
                    <button type="button" class="cancel" onclick="hideDeleteModal()">Отмена</button>
                    <button type="button" class="confirm" onclick="confirmDelete()">Удалить</button>
                </div>
            </div>
        </div>
        
        <a href="/p/{escape(slug)}/delete" class="btn btn-outline" style="text-decoration:none;text-align:center;display:block;margin-top:20px">Новый поиск</a>
    </div>
    """

    js = """
    let pendingDeleteIdx = null;
    
    function showDeleteModal(idx, cardId, content, topic) {
        pendingDeleteIdx = idx;
        document.getElementById('modalCardId').textContent = '[' + cardId + ']';
        const topicEl = document.getElementById('modalTopic');
        if (topic) {
            topicEl.textContent = 'Тема: ' + topic;
            topicEl.style.display = 'block';
        } else {
            topicEl.style.display = 'none';
        }
        document.getElementById('modalQuote').innerHTML = content;
        document.getElementById('deleteModal').classList.add('active');
    }
    
    function hideDeleteModal() {
        document.getElementById('deleteModal').classList.remove('active');
        pendingDeleteIdx = null;
    }
    
    function confirmDelete() {
        if (pendingDeleteIdx !== null) {
            document.getElementById('deleteIdx').value = pendingDeleteIdx + 1;
            showLoading('Удаление', 'Удаляем сущность из базы знаний');
            document.getElementById('deleteForm').submit();
        }
    }
    
    document.getElementById('deleteModal').addEventListener('click', function(e) {
        if (e.target === this) hideDeleteModal();
    });
    
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') hideDeleteModal();
    });
    """
    return HTMLResponse(html_page("Найденные фрагменты", body, js))


@app.post("/p/{slug}/delete/confirm")
async def delete_confirm(slug: str, token: str = Form(""), idx: int = Form(0)):
    scope, err = _writable_scope(slug)
    if err:
        return err
    uid = scope["graph_id"]
    cands = DELETE_CACHE.pop(token, [])

    if not cands or idx < 1 or idx > len(cands):
        return RedirectResponse(f"/p/{slug}/delete?msg=Данные устарели. Повторите поиск.&st=err", status_code=303)

    sel = cands[idx - 1]
    res = linker.repository.delete_zettel(uid, sel["zettel_id"])

    if not res:
        return RedirectResponse(f"/p/{slug}/delete?msg=Не удалось удалить&st=err", status_code=303)

    stats = linker.get_user_stats(uid)
    msg = f"🗑 Удалено [{res['luhmann_id']}]\nУдалено: {res['deleted_count']} сущности(ей)\n📚 Осталось: {stats['total_cards']}"
    log_event(slug, f"Удаление: {sel['content'][:50]}", "delete_query", msg)
    return RedirectResponse(f"/p/{slug}/delete?msg={escape(msg)}&st=ok", status_code=303)


if __name__ == "__main__":
    uvicorn.run("web_app_v2:app", host="0.0.0.0", port=8009, reload=False)
