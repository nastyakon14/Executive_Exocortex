"""
Генератор интерактивного html-графа знаний пользователя.
Использует vis.js network для визуализации zettel-графа из neo4j.
"""

import json
import math
import os
import re
import tempfile
from collections import defaultdict
from typing import Dict, Any, Tuple

from storage.neo4j.client import Neo4jClient
from storage.neo4j.repository import ZettelRepository


_REL_LABELS = {
    "CHILD_OF": "дочерняя",
    "RELATED_TO": "связана с",
    "MENTIONS": "упоминает",
}

# цвета веток: одна ветка — один цвет от корня до всех потомков
# без голубых оттенков, чтобы не путать с тегами (#38bdf8)
_BRANCH_PALETTE = [
    {"bg": "#D4A055", "border": "#B8862E", "hi_bg": "#E4B065", "hi_border": "#A8761E"},
    {"bg": "#72B862", "border": "#529842", "hi_bg": "#82C872", "hi_border": "#428832"},
    {"bg": "#C46888", "border": "#A44868", "hi_bg": "#D47898", "hi_border": "#943858"},
    {"bg": "#9078C8", "border": "#7058A8", "hi_bg": "#A088D8", "hi_border": "#604898"},
    {"bg": "#C89848", "border": "#A87828", "hi_bg": "#D8A858", "hi_border": "#986818"},
    {"bg": "#68A898", "border": "#488878", "hi_bg": "#78B8A8", "hi_border": "#387868"},
    {"bg": "#C87858", "border": "#A85838", "hi_bg": "#D88868", "hi_border": "#984828"},
    {"bg": "#78A878", "border": "#588858", "hi_bg": "#88B888", "hi_border": "#487848"},
    {"bg": "#D48068", "border": "#B46048", "hi_bg": "#E49078", "hi_border": "#A45038"},
    {"bg": "#88B848", "border": "#689828", "hi_bg": "#98C858", "hi_border": "#588818"},
]

_ENTITY_NODE_BG = "#38BDF8"
_ENTITY_BORDER = "#FFFFFF"
_ENTITY_HIGHLIGHT_BG = "#7DD3FC"
_ENTITY_HIGHLIGHT_BORDER = "#E0F2FE"


def _luhmann_sort_key(luhmann_id: str) -> tuple:
    """Сортировка корней: 1, 2, 3, … 10 (не 1, 10, 2)."""
    m = re.match(r"^(\d+)", luhmann_id)
    return (int(m.group(1)), luhmann_id) if m else (9999, luhmann_id)


def _find_root_luhmann(luhmann_id: str, parent_by_luhmann: dict) -> str:
    """Поднимается по CHILD_OF до корневой мысли ветки."""
    current = luhmann_id
    seen = set()
    while parent_by_luhmann.get(current) and current not in seen:
        seen.add(current)
        current = parent_by_luhmann[current]
    return current


def _assign_branch_colors(zettels: list, parent_by_luhmann: dict) -> dict:
    """Назначает цвет каждой корневой ветке; дочерние наследуют цвет корня."""
    roots = sorted(
        {_find_root_luhmann(z["luhmann_id"], parent_by_luhmann) for z in zettels},
        key=_luhmann_sort_key,
    )
    return {
        root: _BRANCH_PALETTE[i % len(_BRANCH_PALETTE)]
        for i, root in enumerate(roots)
    }


def _compute_node_positions(graph_data: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    """
    Быстрая раскладка дерева без vis.js physics.
    Мысли — лес по CHILD_OF, сущности — рядом с упоминающими их узлами.
    """
    zettels = graph_data.get("zettels") or []
    entities = graph_data.get("entities") or []
    edges = graph_data.get("edges") or []

    children: dict[str, list[str]] = defaultdict(list)
    luhmann_to_id: dict[str, str] = {}
    parent_of: dict[str, str | None] = {}
    order: list[str] = []
    for z in zettels:
        lid = z["luhmann_id"]
        luhmann_to_id[lid] = z["zettel_id"]
        order.append(lid)
        parent = z.get("parent_luhmann")
        parent_of[lid] = parent
        if parent:
            children[parent].append(lid)

    known = set(luhmann_to_id)
    roots: list[str] = []
    seen_roots = set()
    for lid in order:
        parent = parent_of.get(lid)
        if (not parent or parent not in known) and lid not in seen_roots:
            seen_roots.add(lid)
            roots.append(lid)
    roots.sort(key=_luhmann_sort_key)

    x_unit = 150
    y_gap = 165
    leaf_memo: dict[str, int] = {}

    def leaf_count(lid: str, stack: set[str]) -> int:
        cached = leaf_memo.get(lid)
        if cached is not None:
            return cached
        if lid in stack:
            return 1
        stack.add(lid)
        kids = [c for c in children[lid] if c in known]
        total = sum(leaf_count(c, stack) for c in kids) if kids else 1
        stack.remove(lid)
        leaf_memo[lid] = max(total, 1)
        return leaf_memo[lid]

    pos: Dict[str, Tuple[float, float]] = {}

    def place(lid: str, x_left: float, y: float) -> None:
        width = leaf_count(lid, set()) * x_unit
        zid = luhmann_to_id.get(lid)
        if zid:
            pos[zid] = (x_left + width / 2, y)
        cursor = x_left
        for child in children[lid]:
            if child not in known:
                continue
            child_w = leaf_count(child, set()) * x_unit
            place(child, cursor, y + y_gap)
            cursor += child_w

    def tree_depth(lid: str, stack: set[str]) -> int:
        if lid in stack:
            return 0
        stack.add(lid)
        kids = [c for c in children[lid] if c in known]
        depth = 1 + max((tree_depth(c, stack) for c in kids), default=0)
        stack.remove(lid)
        return depth

    row_limit = 3200
    x_cursor = 0.0
    y_cursor = 0.0
    row_h = 0.0
    for root in roots:
        width = leaf_count(root, set()) * x_unit
        height = tree_depth(root, set()) * y_gap
        if x_cursor > 0 and x_cursor + width > row_limit:
            x_cursor = 0.0
            y_cursor += row_h + 260
            row_h = 0.0
        place(root, x_cursor, y_cursor)
        x_cursor += width + 110
        row_h = max(row_h, height)

    neighbors: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        to_id = str(edge.get("to_id") or "")
        from_id = edge.get("from_id")
        if to_id.startswith("entity:") and from_id:
            neighbors[to_id].append(from_id)

    for i, entity in enumerate(entities):
        eid = f"entity:{entity['name']}"
        pts = [pos[zid] for zid in neighbors.get(eid, []) if zid in pos]
        if pts:
            ax = sum(p[0] for p in pts) / len(pts)
            ay = sum(p[1] for p in pts) / len(pts)
            pos[eid] = (
                ax + 34 * math.cos(i * 1.7),
                ay + 72 + 34 * math.sin(i * 1.7),
            )
        else:
            pos[eid] = (i * 88.0, y_cursor + row_h + 220)
    return pos


def _build_html(graph_data: Dict[str, Any], user_label: str = "") -> str:
    """Собирает полный HTML-документ с vis.js графом."""

    vis_nodes = []
    vis_edges = []
    node_ids_seen = set()
    depth_by_luhmann = {}

    parent_by_luhmann = {}
    for z in graph_data["zettels"]:
        parent_by_luhmann[z["luhmann_id"]] = z.get("parent_luhmann")

    def calc_depth(luhmann_id: str) -> int:
        """Глубина узла от корня: корень=0, ребёнок=1 и т.д."""
        if luhmann_id in depth_by_luhmann:
            return depth_by_luhmann[luhmann_id]
        parent_l = parent_by_luhmann.get(luhmann_id)
        if not parent_l:
            depth_by_luhmann[luhmann_id] = 0
            return 0
        depth = calc_depth(parent_l) + 1
        depth_by_luhmann[luhmann_id] = depth
        return depth

    def _short_text(content: str, max_len: int) -> str:
        """Обрезает текст до нужной длины."""
        text = " ".join(content.split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    def _wrap_text(content: str, line_len: int) -> str:
        """Переносит текст по словам в несколько строк."""
        words = content.split()
        if not words:
            return content

        lines = []
        current = words[0]
        for w in words[1:]:
            if len(current) + 1 + len(w) <= line_len:
                current += " " + w
            else:
                lines.append(current)
                current = w
        lines.append(current)
        return "\n".join(lines)

    def make_thought_label(topic: str, content: str, depth: int) -> str:
        """
        Отображает тему мысли на узле. Если тема пустая — показывает обрезанный контент.
        """
        # Используем тему, если она есть
        display_text = topic.strip() if topic else content
        
        if depth <= 0:
            # Корневые: показываем полную тему или до 34 символов
            return _short_text(display_text, 34)
        elif depth == 1:
            max_len, line_len = 18, 8
        elif depth == 2:
            max_len, line_len = 14, 6
        elif depth == 3:
            max_len, line_len = 12, 5
        else:
            max_len, line_len = 10, 5
        return _wrap_text(_short_text(display_text, max_len), line_len)

    def make_entity_label(content: str, max_len: int = 9) -> str:
        """Короткий заголовок сущности, чтобы текст помещался в маленький круг."""
        return _short_text(content, max_len)

    def thought_size_by_depth(depth: int) -> int:
        """
        Фиксированные размеры мыслей по глубине:
        корневые самые большие, далее ступенчато меньше.
        """
        if depth <= 0:
            return 62
        if depth == 1:
            return 48
        if depth == 2:
            return 34
        if depth == 3:
            return 28
        return 24

    branch_colors = _assign_branch_colors(graph_data["zettels"], parent_by_luhmann)
    positions = _compute_node_positions(graph_data)

    for z in graph_data["zettels"]:
        nid = z["zettel_id"]
        if nid in node_ids_seen:
            continue
        node_ids_seen.add(nid)

        depth = calc_depth(z["luhmann_id"])
        topic = z.get("topic", "")
        label = make_thought_label(topic, z["content"], depth)
        # Корни заметно крупнее, дочерние ступенчато меньше.
        size = thought_size_by_depth(depth)
        font_size = 12 if depth == 0 else (11 if depth == 1 else (10 if depth == 2 else 9))

        content_escaped = json.dumps(z["content"], ensure_ascii=False)[1:-1]
        topic_escaped = json.dumps(topic, ensure_ascii=False)[1:-1] if topic else ""
        tags_str = ", ".join(z["tags"]) if z["tags"] else "—"
        tt = z["thought_type"]
        parent_info = f"← [{z['parent_luhmann']}]" if z.get("parent_luhmann") else "корневая"

        # При клике показываем полный текст заметки
        title = f"<b>[{z['luhmann_id']}]</b> {parent_info}<br/>"
        if topic_escaped:
            title += f"<b>Тема:</b> {topic_escaped}<br/>"
        title += (
            f"<i>Тип:</i> {tt}<br/>"
            f"<i>Теги:</i> {tags_str}<br/><hr/>"
            f"{content_escaped}"
        )

        root_luhmann = _find_root_luhmann(z["luhmann_id"], parent_by_luhmann)
        branch = branch_colors[root_luhmann]

        xy = positions.get(nid, (0.0, 0.0))
        vis_nodes.append({
            "id": nid,
            "label": label,
            "preview": f"{topic}: {_short_text(z['content'], 100)}" if topic else _short_text(z["content"], 120),
            "title": title,
            "searchText": " ".join([
                z["luhmann_id"], topic, z["content"], tags_str, tt, z.get("parent_luhmann") or "",
            ]).lower(),
            "x": xy[0],
            "y": xy[1],
            "color": {
                "background": branch["bg"],
                "border": branch["border"],
                "highlight": {"background": branch["hi_bg"], "border": branch["hi_border"]},
            },
            "size": size,
            "shape": "circle",
            "font": {"size": font_size, "color": "#0f172a", "face": "Arial", "multi": True},
            "borderWidth": 2,
            "group": "zettel",
        })

    for e in graph_data["entities"]:
        eid = f"entity:{e['name']}"
        if eid in node_ids_seen:
            continue
        node_ids_seen.add(eid)

        mentions = e.get("mention_count", 0)

        title = (
            f"<b>🏷 {e['display_name']}</b><br/>"
            f"<i>Тип:</i> {e['entity_type']}<br/>"
            f"<i>Упоминаний:</i> {mentions}"
        )

        xy = positions.get(eid, (0.0, 0.0))
        vis_nodes.append({
            "id": eid,
            "label": make_entity_label(e["display_name"]),
            "preview": e["display_name"],
            "title": title,
            "searchText": f"{e['display_name']} {e.get('entity_type', '')} {e.get('name', '')}".lower(),
            "x": xy[0],
            "y": xy[1],
            "color": {
                "background": _ENTITY_NODE_BG,
                "border": _ENTITY_BORDER,
                "highlight": {"background": _ENTITY_HIGHLIGHT_BG, "border": _ENTITY_HIGHLIGHT_BORDER},
            },
            "size": 18,
            "shape": "circle",
            "font": {"size": 11, "color": "#0c4a6e", "face": "Arial", "multi": True},
            "borderWidth": 3,
            "group": "entity",
        })

    edge_set = set()
    for edge in graph_data["edges"]:
        key = (edge["from_id"], edge["to_id"], edge["rel_type"])
        if key in edge_set:
            continue
        edge_set.add(key)

        rel = edge["rel_type"]
        label = _REL_LABELS.get(rel, rel.lower())
        if rel in {"CHILD_OF", "MENTIONS"}:
            label = ""

        if rel == "CHILD_OF":
            color, dashes, width = "#9CA3AF", False, 1.0
        elif rel == "RELATED_TO":
            color, dashes, width = "#D1D5DB", [4, 4], 1.0
        elif rel == "MENTIONS":
            color, dashes, width = "#9CA3AF", [2, 4], 0.8
        else:
            color, dashes, width = "#9CA3AF", False, 0.8

        vis_edges.append({
            "from": edge["from_id"],
            "to": edge["to_id"],
            "rel_type": rel,
            "label": label,
            "color": {"color": color, "highlight": "#6B7280"},
            "dashes": dashes,
            "width": width,
            "font": {"size": 9, "color": "#9CA3AF", "strokeWidth": 0},
            "arrows": {"to": {"enabled": True, "scaleFactor": 0.4, "type": "arrow"}},
            "smooth": {"type": "continuous"},
        })

    nodes_json = json.dumps(vis_nodes, ensure_ascii=False)
    edges_json = json.dumps(vis_edges, ensure_ascii=False)
    total_z = len(graph_data["zettels"])
    total_e = len(graph_data["entities"])
    total_r = len(edge_set)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Цифровой экзокортекс{(' — ' + user_label) if user_label else ''}</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #0b1220; color: #e5e7eb;
    font-family: 'Segoe UI', Arial, sans-serif;
    overflow: hidden; height: 100vh;
  }}
  #header {{
    background: linear-gradient(90deg, #0f172a 0%, #111827 100%);
    padding: 14px 24px; display: flex;
    align-items: center; justify-content: space-between;
    border-bottom: 1px solid #334155;
    z-index: 10; position: relative;
  }}
  #header h1 {{ font-size: 20px; color: #e2e8f0; font-weight: 600; }}
  #header-right {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  #stats {{
    font-size: 13px; color: #94a3b8;
    display: flex; gap: 18px;
  }}
  #stats span {{ color: #cbd5e1; font-weight: 600; }}
  #theme-toggle {{
    border: 1px solid #334155;
    background: #0f172a;
    color: #e2e8f0;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
    cursor: pointer;
  }}
  #theme-toggle:hover {{
    background: #1e293b;
    border-color: #475569;
  }}
  #legend {{
    position: absolute; bottom: 16px; left: 16px;
    background: rgba(15, 23, 42, 0.95); border: 1px solid #334155;
    border-radius: 10px; padding: 12px 16px; z-index: 10;
    font-size: 12px; max-width: 200px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.25);
  }}
  #legend h3 {{ margin-bottom: 8px; color: #e2e8f0; font-size: 13px; }}
  .leg-item {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }}
  .leg-dot {{
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid; flex-shrink: 0;
  }}
  .leg-dot-thought {{
    background: #D4A055;
    border-color: #B8862E;
  }}
  .leg-dot-entity {{
    background: #38BDF8;
    border-color: #FFFFFF;
    box-shadow: 0 0 8px rgba(56, 189, 248, 0.75);
  }}
  #graph {{ width: 100%; height: calc(100vh - 56px); }}
  #detail-panel {{
    position: absolute; top: 64px; right: 16px;
    width: 340px; max-height: calc(100vh - 100px);
    background: rgba(15, 23, 42, 0.98); border: 1px solid #334155;
    border-radius: 12px; padding: 18px;
    overflow-y: auto; z-index: 10;
    display: none;
    box-shadow: 0 8px 28px rgba(0,0,0,0.35);
  }}
  #detail-panel h2 {{ color: #e2e8f0; margin-bottom: 10px; font-size: 15px; }}
  #detail-panel .field {{ margin: 6px 0; }}
  #detail-panel .field-label {{ color: #94a3b8; font-size: 12px; }}
  #detail-panel .field-value {{ color: #e2e8f0; font-size: 13px; line-height: 1.5; }}
  #detail-panel .close-btn {{
    position: absolute; top: 10px; right: 14px;
    cursor: pointer; color: #94a3b8; font-size: 20px;
  }}
  #detail-panel .close-btn:hover {{ color: #e2e8f0; }}
  #detail-panel .tag {{
    display: inline-block; background: #334155; color: #e2e8f0;
    padding: 2px 8px; border-radius: 12px; font-size: 11px;
    margin: 2px 3px;
  }}
  #detail-panel .neighbors {{ margin-top: 12px; }}
  #detail-panel .neighbor-item {{
    padding: 8px 10px; margin: 5px 0;
    background: rgba(255,255,255,0.05); border-radius: 6px;
    cursor: pointer; font-size: 13px; color: #e2e8f0;
    line-height: 1.35;
    white-space: normal;
    word-break: break-word;
  }}
  #detail-panel .neighbor-item:hover {{ background: rgba(59,130,246,0.2); }}
  #detail-panel hr {{ border-color: #334155; margin: 10px 0; }}
  #search-box {{
    position: absolute; top: 64px; left: 16px;
    z-index: 10;
  }}
  #search-box input {{
    background: rgba(15, 23, 42, 0.95); border: 1px solid #334155;
    border-radius: 8px; padding: 8px 14px; color: #e2e8f0;
    font-size: 13px; width: 200px; outline: none;
    box-shadow: 0 2px 6px rgba(0,0,0,0.2);
  }}
  #search-box input:focus {{ border-color: #60a5fa; }}
  body.light-theme {{
    background: #f8fafc;
    color: #1f2937;
  }}
  body.light-theme #header {{
    background: #ffffff;
    border-bottom: 1px solid #e5e7eb;
  }}
  body.light-theme #header h1 {{ color: #1f2937; }}
  body.light-theme #stats {{ color: #6b7280; }}
  body.light-theme #stats span {{ color: #374151; }}
  body.light-theme #theme-toggle {{
    border: 1px solid #d1d5db;
    background: #ffffff;
    color: #374151;
  }}
  body.light-theme #theme-toggle:hover {{
    background: #f3f4f6;
  }}
  body.light-theme #legend {{
    background: rgba(255,255,255,0.96);
    border: 1px solid #e5e7eb;
    box-shadow: 0 3px 10px rgba(0,0,0,0.08);
  }}
  body.light-theme #legend h3 {{ color: #374151; }}
  body.light-theme #detail-panel {{
    background: #ffffff;
    border: 1px solid #e5e7eb;
    box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  }}
  body.light-theme #detail-panel h2 {{ color: #1f2937; }}
  body.light-theme #detail-panel .field-label {{ color: #6b7280; }}
  body.light-theme #detail-panel .field-value {{ color: #374151; }}
  body.light-theme #detail-panel .close-btn {{ color: #9ca3af; }}
  body.light-theme #detail-panel .close-btn:hover {{ color: #374151; }}
  body.light-theme #detail-panel .tag {{
    background: #e5e7eb;
    color: #374151;
  }}
  body.light-theme #detail-panel .neighbor-item {{
    background: #f3f4f6;
    color: #374151;
  }}
  body.light-theme #detail-panel .neighbor-item:hover {{
    background: #e5e7eb;
  }}
  body.light-theme #detail-panel hr {{ border-color: #e5e7eb; }}
  body.light-theme #search-box input {{
    background: #ffffff;
    border: 1px solid #d1d5db;
    color: #374151;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  }}
  body.light-theme #search-box input:focus {{ border-color: #3b82f6; }}
  #spray-layer {{
    position: fixed;
    inset: 0;
    pointer-events: none;
    overflow: hidden;
    z-index: 20;
  }}
  .spray-dot {{
    position: fixed;
    width: 110px;
    height: 110px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(56, 189, 248, 0.2) 0%, rgba(14, 165, 233, 0.1) 40%, transparent 72%);
    transform: translate(-50%, -50%);
    filter: blur(22px);
    animation: spray-haze 1.55s ease-out forwards;
    will-change: transform, opacity, filter;
    pointer-events: none;
  }}
  body.light-theme .spray-dot {{
    background: radial-gradient(circle, rgba(14, 165, 233, 0.18) 0%, rgba(56, 189, 248, 0.08) 42%, transparent 72%);
  }}
  .spray-follow {{
    position: fixed;
    width: 160px;
    height: 160px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(56, 189, 248, 0.16) 0%, rgba(14, 165, 233, 0.08) 45%, transparent 70%);
    transform: translate(-50%, -50%);
    filter: blur(28px);
    pointer-events: none;
    opacity: 0;
  }}
  body.light-theme .spray-follow {{
    background: radial-gradient(circle, rgba(14, 165, 233, 0.14) 0%, rgba(56, 189, 248, 0.06) 45%, transparent 72%);
  }}
  @keyframes spray-haze {{
    0% {{ opacity: 0.5; transform: translate(-50%, -50%) scale(0.55); filter: blur(16px); }}
    100% {{ opacity: 0; transform: translate(calc(-50% + var(--dx, 0px)), calc(-50% + var(--dy, 0px))) scale(1.85); filter: blur(34px); }}
  }}
</style>
</head>
<body class="light-theme">
<div id="header">
  <h1>🧠 Цифровой экзокортекс</h1>
  <div id="header-right">
    <div id="stats">
      Мысли: <span>{total_z}</span> &nbsp;|&nbsp;
      Сущности: <span>{total_e}</span> &nbsp;|&nbsp;
      Связи: <span>{total_r}</span>
    </div>
    <button id="theme-toggle" type="button">Темная тема</button>
  </div>
</div>
<div id="search-box">
  <input type="text" id="searchInput" placeholder="🔍 Найти мысль или сущность..."/>
</div>
<div id="spray-layer"></div>
<div id="graph"></div>
<div id="legend">
  <h3>Легенда</h3>
  <div class="leg-item"><div class="leg-dot leg-dot-thought"></div> Мысль</div>
  <div class="leg-item"><div class="leg-dot leg-dot-entity"></div> Тег / Сущность</div>
</div>
<div id="detail-panel">
  <span class="close-btn" onclick="closeDetail()">&times;</span>
  <div id="detail-content"></div>
</div>
<script>
const nodesData = {nodes_json};
const edgesData = {edges_json};

const nodes = new vis.DataSet(nodesData);
const edges = new vis.DataSet(edgesData);

const container = document.getElementById('graph');
const data = {{ nodes, edges }};
const options = {{
  physics: {{ enabled: false }},
  interaction: {{
    hover: nodesData.length < 800,
    tooltipDelay: 220,
    zoomSpeed: 0.22,
    zoomView: true,
    dragView: true,
    dragNodes: true,
    navigationButtons: false,
    keyboard: true,
    hideEdgesOnDrag: nodesData.length > 400,
    hideEdgesOnZoom: nodesData.length > 400,
  }},
  edges: {{
    smooth: {{ enabled: nodesData.length < 600, type: 'continuous' }},
    selectionWidth: 1.5,
    hoverWidth: 1.2,
  }},
  nodes: {{
    shadow: false,
  }},
  layout: {{
    improvedLayout: false,
    hierarchical: false,
  }},
}};

const network = new vis.Network(container, data, options);
network.fit({{ animation: false }});

// переключатель темы (светлая / тёмная)
const themeToggleBtn = document.getElementById('theme-toggle');
const savedTheme = localStorage.getItem('theme') || localStorage.getItem('exocortex_theme') || 'light';
if (savedTheme === 'dark') {{
  document.body.classList.remove('light-theme');
  themeToggleBtn.textContent = 'Светлая тема';
}}

themeToggleBtn.addEventListener('click', function() {{
  const isLight = document.body.classList.toggle('light-theme');
  const next = isLight ? 'light' : 'dark';
  themeToggleBtn.textContent = isLight ? 'Темная тема' : 'Светлая тема';
  localStorage.setItem('exocortex_theme', next);
  localStorage.setItem('theme', next);
}});

// мягкая дымка за курсором
(function() {{
  const layer = document.getElementById('spray-layer');
  if (!layer) return;
  if (window.matchMedia('(pointer: coarse)').matches) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const follow = document.createElement('div');
  follow.className = 'spray-follow';
  layer.appendChild(follow);

  let lastSpawn = 0;
  let tx = 0, ty = 0, cx = 0, cy = 0, hasMove = false;

  function spawnHaze(x, y) {{
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
    puff.addEventListener('animationend', () => puff.remove(), {{ once: true }});
  }}

  window.addEventListener('mousemove', function(e) {{
    tx = e.clientX;
    ty = e.clientY;
    if (!hasMove) {{
      cx = tx;
      cy = ty;
      hasMove = true;
      follow.style.opacity = '1';
    }}
    const now = performance.now();
    if (now - lastSpawn < 42) return;
    lastSpawn = now;
    spawnHaze(e.clientX, e.clientY);
  }});

  function tick() {{
    if (hasMove) {{
      cx += (tx - cx) * 0.14;
      cy += (ty - cy) * 0.14;
      follow.style.left = cx + 'px';
      follow.style.top = cy + 'px';
    }}
    requestAnimationFrame(tick);
  }}
  tick();
}})();

// панель деталей выбранного узла
function closeDetail() {{
  document.getElementById('detail-panel').style.display = 'none';
}}

function showDetail(nodeId) {{
  const node = nodes.get(nodeId);
  if (!node) return;

  const panel = document.getElementById('detail-panel');
  const content = document.getElementById('detail-content');

  const connected = network.getConnectedNodes(nodeId);
  const connEdges = network.getConnectedEdges(nodeId);
  let entityNeighbors = [];
  let thoughtNeighbors = [];
  connected.forEach(cid => {{
    const cn = nodes.get(cid);
    if (cn) {{
      const relevantEdges = connEdges.map(eid => edges.get(eid)).filter(e =>
        (e.from === nodeId && e.to === cid) || (e.from === cid && e.to === nodeId)
      );
      const edge = relevantEdges.length > 0 ? relevantEdges[0] : null;
      const relType = edge ? edge.rel_type : '';
      let arrow = '';

      // Для тегов стрелки не показываем.
      if (cn.group !== 'entity') {{
        if (relType === 'CHILD_OF') {{
          // CHILD_OF: child -> parent
          // Вправо: идем вниз к дочерним; влево: поднимаемся к родителю.
          if (edge && edge.to === nodeId && edge.from === cid) {{
            arrow = '→ ';
          }} else if (edge && edge.from === nodeId && edge.to === cid) {{
            arrow = '← ';
          }}
        }} else if (edge) {{
          arrow = edge.from === nodeId ? '→ ' : '← ';
        }}
      }}

      const neighborText = cn.preview || cn.label;
      const neighborHtml = '<div class="neighbor-item" onclick="focusNode(\\'' +
        cid.replace(/'/g, "\\\\'") + '\\')">' +
        arrow + '<b>' + neighborText + '</b></div>';

      if (cn.group === 'entity') {{
        entityNeighbors.push(neighborHtml);
      }} else {{
        thoughtNeighbors.push(neighborHtml);
      }}
    }}
  }});

  let html = '<h2>' + node.label + '</h2>';
  if (node.title) {{
    html += '<div class="field"><div class="field-value">' + node.title + '</div></div>';
  }}
  const neighborsHtml = entityNeighbors.join('') + thoughtNeighbors.join('');
  if (neighborsHtml) {{
    html += '<hr/><div class="neighbors"><div class="field-label">Связи (' +
      connected.length + ')</div>' + neighborsHtml + '</div>';
  }}

  content.innerHTML = html;
  panel.style.display = 'block';
}}

function focusNode(nodeId) {{
  network.focus(nodeId, {{ scale: 1.25, animation: {{ duration: 300, easingFunction: 'easeInOutQuad' }} }});
  network.selectNodes([nodeId]);
  showDetail(nodeId);
}}

network.on('click', function(params) {{
  if (params.nodes.length > 0) {{
    showDetail(params.nodes[0]);
  }} else {{
    closeDetail();
  }}
}});

network.on('doubleClick', function(params) {{
  if (params.nodes.length > 0) {{
    focusNode(params.nodes[0]);
  }}
}});

// фильтрация узлов по тексту поиска
const searchInput = document.getElementById('searchInput');
const adj = new Map();
edgesData.forEach(e => {{
  if (!adj.has(e.from)) adj.set(e.from, []);
  if (!adj.has(e.to)) adj.set(e.to, []);
  adj.get(e.from).push(e.to);
  adj.get(e.to).push(e.from);
}});
const searchIndex = nodesData.map(n => ({{
  id: n.id,
  hay: (n.searchText || ((n.label || '') + ' ' + (n.preview || '') + ' ' + (n.title || ''))).toLowerCase()
}}));
let searchTimer = 0;
function applyGraphSearch(raw) {{
  const q = (raw || '').toLowerCase().trim();
  if (!q) {{
    nodes.update(nodesData.map(n => ({{ id: n.id, hidden: false }})));
    edges.update(edges.getIds().map(id => ({{ id, hidden: false }})));
    return;
  }}
  const matched = new Set();
  for (const item of searchIndex) {{
    if (item.hay.includes(q)) matched.add(item.id);
  }}
  const visible = new Set(matched);
  matched.forEach(nid => {{
    const nbrs = adj.get(nid);
    if (nbrs) nbrs.forEach(cid => visible.add(cid));
  }});
  nodes.update(nodesData.map(n => ({{ id: n.id, hidden: !visible.has(n.id) }})));
  const edgeUpdates = [];
  edges.forEach(e => {{
    edgeUpdates.push({{ id: e.id, hidden: !(visible.has(e.from) && visible.has(e.to)) }});
  }});
  edges.update(edgeUpdates);
}}
searchInput.addEventListener('input', function() {{
  const value = this.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function() {{ applyGraphSearch(value); }}, 60);
}});
</script>
</body>
</html>"""
    return html


def render_graph_html(graph_data: Dict[str, Any], user_label: str = "") -> str:
    """Собирает HTML-граф в память — без временного файла."""
    return _build_html(graph_data, user_label=user_label)


def generate_graph_html(user_id: str, output_path: str | None = None) -> str:
    """
    Генерирует интерактивный HTML-граф для пользователя и сохраняет в файл.
    Возвращает путь к файлу.
    """
    client = Neo4jClient()
    repo = ZettelRepository(client)
    graph_data = repo.export_graph_data(user_id)

    html_content = render_graph_html(graph_data, user_label=user_id)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="graph_")
        os.close(fd)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path


def generate_graph_html_from_data(
    graph_data: dict,
    user_label: str = "",
    output_path: str | None = None,
) -> str:
    """Собирает HTML-граф из уже выгруженных данных."""
    html_content = render_graph_html(graph_data, user_label=user_label)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="graph_")
        os.close(fd)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path


def generate_graph_html_from_repo(
    repo: ZettelRepository, user_id: str, output_path: str | None = None,
) -> str:
    """Вариант без создания нового клиента — переиспользует существующий repository."""
    graph_data = repo.export_graph_data(user_id)
    html_content = render_graph_html(graph_data, user_label=user_id)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="graph_")
        os.close(fd)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path
