"""
Интерактивный граф знаний v2: Canvas 2D на CPU.
Самодостаточный пайплайн: раскладка, упаковка данных, HTML и gzip.
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from html import escape as html_escape
from typing import Any, Dict, Optional, Tuple

from storage.neo4j.client import Neo4jClient
from storage.neo4j.repository import ZettelRepository


_BRANCH_PALETTE = [
    {"bg": "#D4A055", "border": "#B8862E"},
    {"bg": "#72B862", "border": "#529842"},
    {"bg": "#C46888", "border": "#A44868"},
    {"bg": "#9078C8", "border": "#7058A8"},
    {"bg": "#C89848", "border": "#A87828"},
    {"bg": "#68A898", "border": "#488878"},
    {"bg": "#C87858", "border": "#A85838"},
    {"bg": "#78A878", "border": "#588858"},
    {"bg": "#D48068", "border": "#B46048"},
    {"bg": "#88B848", "border": "#689828"},
]


def _luhmann_sort_key(luhmann_id: str) -> tuple:
    m = re.match(r"^(\d+)", luhmann_id)
    return (int(m.group(1)), luhmann_id) if m else (9999, luhmann_id)


def _public_source_url(url: str) -> str:
    text = (url or "").strip()
    if not text or text == "text":
        return text or "text"
    if not (text.lower().startswith(("http://", "https://")) or text.isdigit()):
        return text
    try:
        from app.handlers.confluence import display_confluence_source
        return display_confluence_source(text)
    except Exception:
        return text


def _root_lookup(parent_by_luhmann: dict):
    """Корень каждой ветки за один проход по цепочке, без рекурсии."""
    memo: dict[str, str] = {}

    def root_of(luhmann_id: str) -> str:
        cached = memo.get(luhmann_id)
        if cached:
            return cached
        chain: list[str] = []
        seen: set[str] = set()
        current = luhmann_id
        while current and current not in memo and current not in seen:
            seen.add(current)
            chain.append(current)
            current = parent_by_luhmann.get(current)
        if current in memo:
            found = memo[current]
        elif chain:
            found = chain[-1]
        else:
            found = luhmann_id
        for node in chain:
            memo[node] = found
        return found

    return root_of


def _node_depths(parent_by_luhmann: dict, ids: list[str]) -> dict[str, int]:
    depth: dict[str, int] = {}
    for lid in ids:
        if lid in depth:
            continue
        chain: list[str] = []
        seen: set[str] = set()
        current = lid
        while current and current not in depth and current not in seen:
            seen.add(current)
            chain.append(current)
            current = parent_by_luhmann.get(current)
        if current in depth and current not in seen:
            base = depth[current] + 1
        else:
            base = 0
        for node in reversed(chain):
            depth[node] = base
            base += 1
    return depth


def _tree_metrics(order: list[str], kids_of) -> tuple[dict[str, int], dict[str, int]]:
    """Число листьев и глубина дерева. Явный стек, чтобы длинная ветка не падала по рекурсии."""
    leaves: dict[str, int] = {}
    depths: dict[str, int] = {}
    color: dict[str, int] = {}
    for start in order:
        if color.get(start) == 2:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            lid, expanded = stack.pop()
            if expanded:
                children = kids_of(lid)
                if children:
                    leaves[lid] = max(sum(leaves.get(child, 1) for child in children), 1)
                    depths[lid] = 1 + max(depths.get(child, 0) for child in children)
                else:
                    leaves[lid] = 1
                    depths[lid] = 1
                color[lid] = 2
                continue
            if color.get(lid) == 2:
                continue
            if color.get(lid) == 1:
                leaves[lid] = 1
                depths[lid] = 1
                color[lid] = 2
                continue
            color[lid] = 1
            stack.append((lid, True))
            for child in reversed(kids_of(lid)):
                state = color.get(child, 0)
                if state == 0:
                    stack.append((child, False))
    return leaves, depths


def _place_tree(
    root: str,
    x_left: float,
    y: float,
    kids_of,
    leaves: dict[str, int],
    x_unit: float,
    y_gap: float,
    luhmann_to_id: dict[str, str],
    pos: dict,
) -> None:
    stack: list[tuple[str, float, float]] = [(root, x_left, y)]
    guard: set[str] = set()
    while stack:
        lid, left, row_y = stack.pop()
        if lid in guard:
            continue
        guard.add(lid)
        width = leaves.get(lid, 1) * x_unit
        zid = luhmann_to_id.get(lid)
        if zid:
            pos[zid] = (left + width / 2, row_y)
        cursor = left
        frames: list[tuple[str, float, float]] = []
        for child in kids_of(lid):
            child_w = leaves.get(child, 1) * x_unit
            frames.append((child, cursor, row_y + y_gap))
            cursor += child_w
        for frame in reversed(frames):
            stack.append(frame)


def _assign_branch_colors(zettels: list, parent_by_luhmann: dict) -> dict:
    root_of = _root_lookup(parent_by_luhmann)
    roots = sorted({root_of(z["luhmann_id"]) for z in zettels}, key=_luhmann_sort_key)
    return {root: _BRANCH_PALETTE[i % len(_BRANCH_PALETTE)] for i, root in enumerate(roots)}


_EXCEL_TYPES = frozenset({"excel_sheet", "excel_column", "excel_value", "excel_row"})


def _excel_value_circle(count: int) -> tuple[float, list[tuple[float, float]]]:
    """Значения по окружности вокруг столбца. Если их много — несколько колец."""
    if count <= 0:
        return 40.0, []
    spacing = 58.0
    if count == 1:
        return 104.0, [(86.0, 0.0)]
    one_r = max(86.0, spacing * count / (2 * math.pi))
    if one_r <= 155.0:
        offsets = [
            (one_r * math.cos(ang), one_r * math.sin(ang))
            for ang in (
                -math.pi / 2 + 2 * math.pi * index / count
                for index in range(count)
            )
        ]
        return one_r + 14.0, offsets
    offsets: list[tuple[float, float]] = []
    placed = 0
    ring = 0
    outer = 48.0
    while placed < count and ring < 40:
        vr = 86.0 + ring * spacing
        cap = max(6, int(2 * math.pi * vr / spacing))
        take = min(cap, count - placed)
        phase = (math.pi / take) if ring % 2 else 0.0
        for index in range(take):
            ang = phase - math.pi / 2 + 2 * math.pi * index / take
            offsets.append((vr * math.cos(ang), vr * math.sin(ang)))
        outer = vr + 14.0
        placed += take
        ring += 1
    return outer, offsets


def _column_orbits(outers: list[float]) -> list[float]:
    """Радиус столбца за один проход: соседние круги не пересекаются."""
    n = len(outers)
    if n == 0:
        return []
    if n == 1:
        return [max(120.0, outers[0] + 72.0)]
    sin_half = math.sin(math.pi / n) or 1e-6
    radii = []
    for index, outer in enumerate(outers):
        nxt = outers[(index + 1) % n]
        need = (outer + nxt + 18.0) / (2 * sin_half)
        radii.append(min(4800.0, max(outer + 72.0, need)))
    if max(outers) <= min(outers) * 1.35:
        peak = max(radii)
        return [peak for _ in radii]
    return radii


def _palette_index(name: str) -> int:
    total = 0
    for ch in name:
        total = (total * 33 + ord(ch)) & 0xFFFFFFFF
    return total % len(_BRANCH_PALETTE)


def _scatter_origins(sizes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Коробки по спирали: пятно, а не колонна кластеров."""
    golden = math.pi * (3 - math.sqrt(5))
    origins: list[tuple[float, float]] = []
    radius = 0.0
    for index, (width, height) in enumerate(sizes):
        reach = 0.42 * math.hypot(width, height) + 28.0
        if index == 0:
            cx = cy = 0.0
        else:
            ang = index * golden
            jitter = ((index * 37) % 50) - 25.0
            radius = max(radius * 0.92, reach * 0.35)
            cx = radius * math.cos(ang) + jitter
            cy = radius * math.sin(ang) * 0.78 + jitter * 0.4
            radius += reach * 0.48
        origins.append((cx - width / 2.0, cy - height / 2.0))
    return origins


def _compute_node_positions(graph_data: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    zettels = graph_data.get("zettels") or []
    entities = graph_data.get("entities") or []
    edges = graph_data.get("edges") or []

    children: dict[str, list[str]] = defaultdict(list)
    luhmann_to_id: dict[str, str] = {}
    parent_of: dict[str, str | None] = {}
    type_by_lid: dict[str, str] = {}
    zid_to_lid: dict[str, str] = {}
    order: list[str] = []
    for z in zettels:
        lid = z["luhmann_id"]
        zid = z["zettel_id"]
        luhmann_to_id[lid] = zid
        zid_to_lid[zid] = lid
        type_by_lid[lid] = z.get("thought_type") or ""
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

    related_values: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.get("rel_type") != "RELATED_TO":
            continue
        left, right = edge.get("from_id"), edge.get("to_id")
        if not left or not right:
            continue
        left_t = type_by_lid.get(zid_to_lid.get(left, ""), "")
        right_t = type_by_lid.get(zid_to_lid.get(right, ""), "")
        if left_t == "excel_row" and right_t == "excel_value":
            related_values[left].append(right)
        elif right_t == "excel_row" and left_t == "excel_value":
            related_values[right].append(left)

    x_unit = 118
    y_gap = 128

    def structural_kids(lid: str) -> list[str]:
        return [
            child for child in children[lid]
            if child in known and type_by_lid.get(child) != "excel_row"
        ]

    leaf_memo, depth_memo = _tree_metrics(order, structural_kids)
    pos: Dict[str, Tuple[float, float]] = {}

    def excel_layout(sheet_lid: str) -> tuple[list[tuple[str, list[str], float, list[tuple[float, float]]]], float]:
        cols = [c for c in children[sheet_lid] if type_by_lid.get(c) == "excel_column"]
        prepared: list[tuple[str, list[str], float, list[tuple[float, float]]]] = []
        outers: list[float] = []
        for col in cols:
            vals = [v for v in children[col] if type_by_lid.get(v) == "excel_value"]
            outer, offsets = _excel_value_circle(len(vals))
            prepared.append((col, vals, outer, offsets))
            outers.append(outer)
        orbits = _column_orbits(outers)
        clusters = [
            (col, vals, orbits[index], offsets)
            for index, (col, vals, _outer, offsets) in enumerate(prepared)
        ]
        bound = 80.0
        for index, outer in enumerate(outers):
            bound = max(bound, orbits[index] + outer)
        return clusters, bound

    def place_excel_sheet(sheet_lid: str, origin_x: float, origin_y: float) -> tuple[float, float]:
        """Лист в центре. Столбцы по кругу, значения — кольцом вокруг своего столбца."""
        clusters, bound = excel_layout(sheet_lid)
        n_cols = max(len(clusters), 1)
        sheet_zid = luhmann_to_id.get(sheet_lid)
        if sheet_zid:
            pos[sheet_zid] = (origin_x, origin_y)
        for index, (col, vals, radius, offsets) in enumerate(clusters):
            ang = -math.pi / 2 + (2 * math.pi * index / n_cols)
            cx = origin_x + radius * math.cos(ang)
            cy = origin_y + radius * math.sin(ang)
            col_zid = luhmann_to_id.get(col)
            if col_zid:
                pos[col_zid] = (cx, cy)
            for v_index, val in enumerate(vals):
                if v_index >= len(offsets):
                    break
                dx, dy = offsets[v_index]
                val_zid = luhmann_to_id.get(val)
                if val_zid:
                    pos[val_zid] = (cx + dx, cy + dy)
        for row in children[sheet_lid]:
            if type_by_lid.get(row) != "excel_row":
                continue
            row_zid = luhmann_to_id.get(row)
            if row_zid:
                pos[row_zid] = (origin_x - 80, origin_y)
        span = 2.0 * bound + 36.0
        return span, span

    excel_roots = [lid for lid in roots if type_by_lid.get(lid) == "excel_sheet"]
    other_roots = [lid for lid in roots if type_by_lid.get(lid) != "excel_sheet"]

    boxes: list[tuple[str, str, float, float]] = []
    for sheet in excel_roots:
        _clusters, bound = excel_layout(sheet)
        span = 2.0 * bound + 36.0
        boxes.append(("excel", sheet, span, span))
    for root in other_roots:
        boxes.append((
            "tree",
            root,
            leaf_memo.get(root, 1) * x_unit,
            depth_memo.get(root, 1) * y_gap,
        ))
    origins = _scatter_origins([(width, height) for _kind, _lid, width, height in boxes])
    for (kind, lid, width, height), (left, top) in zip(boxes, origins):
        if kind == "excel":
            place_excel_sheet(lid, left + width / 2, top + height / 2)
        else:
            _place_tree(lid, left, top, structural_kids, leaf_memo, x_unit, y_gap, luhmann_to_id, pos)

    missing = [z for z in zettels if z["zettel_id"] not in pos]
    if missing:
        placed_x = [point[0] for point in pos.values()] or [0.0]
        placed_y = [point[1] for point in pos.values()] or [0.0]
        left = max(placed_x) + 180.0
        top = min(placed_y)
        cols = max(1, int(math.sqrt(len(missing))))
        for index, zettel in enumerate(missing):
            pos[zettel["zettel_id"]] = (left + (index % cols) * 72.0, top + (index // cols) * 72.0)

    excel_zids = {
        z["zettel_id"] for z in zettels if (z.get("thought_type") or "") in _EXCEL_TYPES
    }
    neighbors: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        to_id = str(edge.get("to_id") or "")
        from_id = edge.get("from_id")
        if to_id.startswith("entity:") and from_id:
            neighbors[to_id].append(from_id)

    loose: list[tuple[int, str]] = []
    for i, entity in enumerate(entities):
        eid = f"entity:{entity['name']}"
        linked = neighbors.get(eid, [])
        if linked and all(zid in excel_zids for zid in linked):
            continue
        pts = [pos[zid] for zid in linked if zid in pos]
        if pts:
            ax = sum(p[0] for p in pts) / len(pts)
            ay = sum(p[1] for p in pts) / len(pts)
            pos[eid] = (ax + 34 * math.cos(i * 1.7), ay + 72 + 34 * math.sin(i * 1.7))
        else:
            loose.append((i, eid))
    if loose:
        floor = max((point[1] for point in pos.values()), default=0.0)
        for i, eid in loose:
            pos[eid] = (i * 88.0, floor + 180)
    return pos


def _short_text(content: str, max_len: int) -> str:
    text = " ".join((content or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _node_label(text: str) -> str:
    """Подпись узла: 2 слова, или 3 если среди них есть короткое (≤3 символа)."""
    words = (text or "").split()
    if not words:
        return ""
    take = 3 if any(len(w) <= 3 for w in words[:2]) else 2
    take = min(take, 3)
    if len(words) <= take:
        return " ".join(words)
    return " ".join(words[:take]) + "…"


def _excel_label(thought_type: str, topic: str, content: str) -> str:
    text = " ".join((topic or content or "").split())
    if not text:
        return ""
    limit = 42 if thought_type == "excel_column" else 22
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _excel_node_style(
    thought_type: str,
    luhmann_id: str,
    parent_by_luhmann: dict,
    color_index: dict,
    root: str,
    depth: int,
    value_degree: int = 0,
) -> tuple[int, int, int]:
    if thought_type == "excel_row":
        return 2, 11, 7
    if thought_type == "excel_sheet":
        return 0, 96, color_index.get(root, 0)
    if thought_type in {"excel_column", "excel_value"}:
        source = luhmann_id if thought_type == "excel_column" else (parent_by_luhmann.get(luhmann_id) or luhmann_id)
        parts = [p for p in (source or "").split(".") if p.isdigit()]
        color = int(parts[-1]) % len(_BRANCH_PALETTE) if parts else 1
        if thought_type == "excel_column":
            return 0, 52, color
        return 0, 32, color
    return 0, _thought_size(depth), color_index.get(root, 0)


def _thought_size(depth: int) -> int:
    if depth <= 0:
        return 62
    if depth == 1:
        return 48
    if depth == 2:
        return 34
    if depth == 3:
        return 28
    return 24


def _pack_graph(graph_data: Dict[str, Any]) -> tuple[list, list, list, int, int, int]:
    parent_by_luhmann = {z["luhmann_id"]: z.get("parent_luhmann") for z in graph_data["zettels"]}
    depth_by_luhmann = _node_depths(parent_by_luhmann, [z["luhmann_id"] for z in graph_data["zettels"]])
    root_of = _root_lookup(parent_by_luhmann)

    branch_colors = _assign_branch_colors(graph_data["zettels"], parent_by_luhmann)
    color_index = {root: i % len(_BRANCH_PALETTE) for i, root in enumerate(branch_colors)}
    positions = _compute_node_positions(graph_data)
    type_by_zid = {z["zettel_id"]: z.get("thought_type") or "" for z in graph_data["zettels"]}
    value_degree: dict[str, int] = defaultdict(int)
    for edge in graph_data.get("edges") or []:
        if edge.get("rel_type") != "RELATED_TO":
            continue
        for zid in (edge.get("from_id"), edge.get("to_id")):
            if type_by_zid.get(zid) == "excel_value":
                value_degree[zid] += 1

    nodes = []
    meta = []
    id_index: dict[str, int] = {}
    card_count = len(graph_data["zettels"])
    if card_count >= 8000:
        lim_topic, lim_content, lim_quote, lim_preview = 90, 160, 80, 60
    else:
        lim_topic, lim_content, lim_quote, lim_preview = 180, 480, 360, 120
    for z in graph_data["zettels"]:
        nid = z["zettel_id"]
        if nid in id_index:
            continue
        depth = depth_by_luhmann.get(z["luhmann_id"], 0)
        topic = z.get("topic") or ""
        content = z.get("content") or ""
        tags = list(z.get("tags") or [])
        tt = z.get("thought_type") or ""
        display = topic.strip() or content
        xy = positions.get(nid, (0.0, 0.0))
        root = root_of(z["luhmann_id"])
        id_index[nid] = len(nodes)
        project = " ".join((z.get("project") or "").split())
        group, radius, color = _excel_node_style(
            tt, z["luhmann_id"], parent_by_luhmann, color_index, root, depth, value_degree.get(nid, 0),
        )
        if project:
            color = _palette_index(project)
        label = _excel_label(tt, topic, content) if tt in _EXCEL_TYPES else _node_label(display)
        nodes.append({
            "x": round(xy[0], 1),
            "y": round(xy[1], 1),
            "r": radius,
            "g": group,
            "c": color,
            "l": label,
        })
        preview = f"{topic}: {_short_text(content, lim_preview)}" if topic else _short_text(content, lim_preview)
        quote = z.get("source_quote") or ""
        source = z.get("source_input") or ""
        meta.append([
            z["luhmann_id"],
            z.get("parent_luhmann") or "",
            _short_text(topic, lim_topic),
            tt,
            ", ".join(tags[:8]),
            _short_text(content, lim_content),
            preview,
            "",
            _public_source_url(source or "text"),
            _short_text(quote, lim_quote),
            project,
        ])

    for e in graph_data["entities"]:
        eid = f"entity:{e['name']}"
        if eid in id_index or eid not in positions:
            continue
        xy = positions.get(eid, (0.0, 0.0))
        name = e.get("display_name") or e.get("name") or ""
        id_index[eid] = len(nodes)
        nodes.append({
            "x": round(xy[0], 1),
            "y": round(xy[1], 1),
            "r": 18,
            "g": 1,
            "c": 0,
            "l": _node_label(name),
        })
        meta.append([
            "",
            "",
            name,
            e.get("entity_type") or "tag",
            "",
            f"Упоминаний: {e.get('mention_count') or 0}",
            name,
            f"{name} {e.get('entity_type', '')} {e.get('name', '')}".lower(),
            "",
        ])

    kind = {"CHILD_OF": 0, "MENTIONS": 1, "RELATED_TO": 2}
    edges = []
    edge_set = set()
    for edge in graph_data["edges"]:
        key = (edge["from_id"], edge["to_id"], edge["rel_type"])
        if key in edge_set:
            continue
        a = id_index.get(edge["from_id"])
        b = id_index.get(edge["to_id"])
        if a is None or b is None:
            continue
        if edge["rel_type"] == "CHILD_OF" and (
            type_by_zid.get(edge["from_id"]) == "excel_row"
            or type_by_zid.get(edge["to_id"]) == "excel_row"
        ):
            continue
        edge_set.add(key)
        edges.append([a, b, kind.get(edge["rel_type"], 0)])
    _append_excel_bridges(graph_data, id_index, edges)
    return nodes, edges, meta, len(graph_data["zettels"]), len(graph_data["entities"]), len(edges)


def _bridge_key(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def _append_excel_bridges(graph_data: Dict[str, Any], id_index: dict, edges: list) -> None:
    """Одинаковые столбцы и значения разных листов связаны постоянно. Строки сюда не входят."""
    topic_by_lid = {z["luhmann_id"]: (z.get("topic") or "") for z in graph_data["zettels"]}
    columns: dict[str, list[int]] = defaultdict(list)
    values: dict[tuple[str, str], list[int]] = defaultdict(list)
    for z in graph_data["zettels"]:
        idx = id_index.get(z["zettel_id"])
        if idx is None:
            continue
        name = _bridge_key(z.get("topic") or "")
        if not name:
            continue
        thought = z.get("thought_type") or ""
        if thought == "excel_column":
            columns[name].append(idx)
        elif thought == "excel_value":
            column_name = _bridge_key(topic_by_lid.get(z.get("parent_luhmann") or "", ""))
            if column_name:
                values[(column_name, name)].append(idx)

    def link(members: list[int], limit: int) -> int:
        if len(members) < 2 or len(members) > limit:
            return 0
        hub = members[0]
        for other in members[1:]:
            edges.append([hub, other, 3])
        return len(members) - 1

    for members in columns.values():
        link(members, 8)
    budget = 70
    for members in values.values():
        if budget <= 0:
            break
        budget -= link(members, 4)


def encode_graph_html(html: str, accept_encoding: str = "") -> tuple[bytes, dict]:
    """Сжимает большой HTML, если браузер принимает gzip."""
    raw = html.encode("utf-8")
    headers = {"content-type": "text/html; charset=utf-8"}
    if "gzip" in (accept_encoding or "").lower() and len(raw) > 20_000:
        return gzip.compress(raw, compresslevel=4), {
            **headers,
            "content-encoding": "gzip",
            "vary": "Accept-Encoding",
        }
    return raw, headers


def pack_graph_payload(graph_data: Dict[str, Any]) -> dict:
    """Компактный payload: параллельные массивы вместо массива объектов."""
    nodes, edges, meta, total_z, total_e, total_r = _pack_graph(graph_data)
    n = len(nodes)
    eflat: list[int] = []
    for a, b, k in edges:
        eflat.extend((int(a), int(b), int(k)))
    return {
        "n": n,
        "x": [float(nodes[i]["x"]) for i in range(n)],
        "y": [float(nodes[i]["y"]) for i in range(n)],
        "r": [max(11, int(round(nodes[i]["r"] * 0.66))) for i in range(n)],
        "g": [int(nodes[i]["g"]) for i in range(n)],
        "c": [int(nodes[i]["c"]) for i in range(n)],
        "l": [nodes[i]["l"] for i in range(n)],
        "m": meta,
        "e": eflat,
        "tz": int(total_z),
        "te": int(total_e),
        "tr": int(total_r),
        "gp": 1 if any(len(row) > 10 and row[10] for row in meta) else 0,
    }


def dumps_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_graph_payload(payload: dict, accept_encoding: str = "") -> tuple[bytes, dict]:
    raw = dumps_payload(payload)
    headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-graph-size": str(len(raw)),
    }
    if "gzip" in (accept_encoding or "").lower() and len(raw) > 8_000:
        level = 1 if len(raw) > 1_500_000 else 4
        return gzip.compress(raw, compresslevel=level), {
            **headers,
            "content-encoding": "gzip",
            "vary": "Accept-Encoding",
        }
    return raw, headers


_GRAPH_JS = r"""
<script>
const DATA_URL = __DATA_URL__;
const DATA_B64 = __DATA_B64__;
const PAL = [["#D4A055","#B8862E"],["#72B862","#529842"],["#C46888","#A44868"],["#9078C8","#7058A8"],["#C89848","#A87828"],["#68A898","#488878"],["#C87858","#A85838"],["#78A878","#588858"],["#D48068","#B46048"],["#88B848","#689828"]];

const wrap = document.getElementById('graph-wrap');
const canvas = document.getElementById('graph');
const ctx = canvas.getContext('2d', { alpha: false });

let N = 0, E = 0;
let X, Y, R, G, C, L, META, EFLAT;
let visMask = null;
let hot = null;
let adjOff, adjTo, adjK, adjFrom;
let W = 0, H = 0, dpr = 1;
let camX = 0, camY = 0, zoom = 1;
let tagsOn = true;
let rowsOn = false;
let selected = -1;
let panDrag = false, nodeDrag = -1, lastX = 0, lastY = 0, moved = false;
let raf = 0;
let hay = [];
const CELL = 160;
let grid = new Map();
let hubs = [];
let hubMark = null;
let dotPick = null;
let dotCols = 0;
let dotBin = 12;
let fitted = false;
let clusters = [];
let groupByProject = false;
let clusterHot = -1;
let hoverNode = -2;
let hoverX = 0, hoverY = 0;

function fillOf(i) {
  if (G[i] === 1) return isLight() ? '#e2e8f0' : '#64748b';
  if (G[i] === 2) return '#64748B';
  return PAL[C[i]][0];
}
function strokeOf(i) {
  if (G[i] === 1) return isLight() ? '#94a3b8' : '#cbd5e1';
  if (G[i] === 2) return '#94A3B8';
  return PAL[C[i]][1];
}
function isLight() { return document.body.classList.contains('light-theme'); }
function sx(x) { return (x - camX) * zoom + W / 2; }
function sy(y) { return (y - camY) * zoom + H / 2; }
function wx(x) { return (x - W / 2) / zoom + camX; }
function wy(y) { return (y - H / 2) / zoom + camY; }
let revealedMark = null;
let revealedRows = [];
function nodeVisible(i) {
  if (G[i] === 1 && !(tagsOn && zoom >= 0.32)) return 0;
  if (G[i] === 2) return revealedMark && revealedMark[i] ? 1 : 0;
  if (visMask && visMask[i] === 0) return 0;
  return 1;
}
function hideRevealedRows() {
  for (let i = 0; i < revealedRows.length; i++) {
    const id = revealedRows[i];
    const oldX = X[id], oldY = Y[id];
    X[id] = -100000;
    Y[id] = -100000;
    reindexNode(id, oldX, oldY);
  }
  revealedRows = [];
  revealedMark = null;
}
function outwardOf(idx) {
  const start = adjOff[idx], end = adjOff[idx + 1];
  for (let p = start; p < end; p++) {
    if (adjK[p] !== 0 || adjFrom[p] !== idx) continue;
    const parent = adjTo[p];
    if (G[parent] === 2) continue;
    const dx = X[idx] - X[parent], dy = Y[idx] - Y[parent];
    const len = Math.hypot(dx, dy) || 1;
    return [dx / len, dy / len];
  }
  return [0, -1];
}
function placeNode(id, x, y) {
  const oldX = X[id], oldY = Y[id];
  X[id] = x;
  Y[id] = y;
  reindexNode(id, oldX, oldY);
}
function revealExcelRows(idx) {
  hideRevealedRows();
  if (idx < 0 || !adjOff) return;
  const tt = String((META[idx] || [])[3] || '');
  if (tt !== 'excel_value' && G[idx] !== 2) return;
  revealedMark = new Uint8Array(N);
  if (G[idx] === 2) {
    revealedMark[idx] = 1;
    revealedRows = [idx];
    const vals = [];
    const start = adjOff[idx], end = adjOff[idx + 1];
    for (let p = start; p < end; p++) {
      if (adjK[p] === 2 && G[adjTo[p]] !== 2) vals.push(adjTo[p]);
    }
    if (!vals.length) {
      placeNode(idx, camX + 160, camY);
      return;
    }
    let ax = 0, ay = 0;
    for (let i = 0; i < vals.length; i++) { ax += X[vals[i]]; ay += Y[vals[i]]; }
    placeNode(idx, ax / vals.length, ay / vals.length - 72);
    return;
  }
  const rows = [];
  const start = adjOff[idx], end = adjOff[idx + 1];
  for (let p = start; p < end; p++) {
    if (G[adjTo[p]] === 2) rows.push(adjTo[p]);
  }
  const use = rows.slice(0, 12);
  revealedRows = use;
  const dir = outwardOf(idx);
  const px = -dir[1], py = dir[0];
  for (let i = 0; i < use.length; i++) {
    const id = use[i];
    revealedMark[id] = 1;
    const side = (i - (use.length - 1) / 2) * 54;
    const along = 168 + (i % 2) * 28;
    placeNode(id, X[idx] + dir[0] * along + px * side, Y[idx] + dir[1] * along + py * side);
  }
}
function isHot(i) { return !hot || hot[i] > 0; }
function setHot(idx) {
  if (idx < 0) {
    hot = null;
    hideRevealedRows();
    return;
  }
  hot = new Uint8Array(N);
  hot[idx] = 2;
  const start = adjOff[idx], end = adjOff[idx + 1];
  for (let p = start; p < end; p++) hot[adjTo[p]] = 1;
  revealExcelRows(idx);
}
function setHotMany(ids) {
  if (!ids || !ids.length) { hot = null; return; }
  hot = new Uint8Array(N);
  for (let k = 0; k < ids.length; k++) hot[ids[k]] = 2;
}

function buildIndex() {
  const cnt = new Uint32Array(N);
  for (let i = 0; i < E; i++) {
    cnt[EFLAT[i * 3]]++;
    cnt[EFLAT[i * 3 + 1]]++;
  }
  adjOff = new Uint32Array(N + 1);
  for (let i = 0; i < N; i++) adjOff[i + 1] = adjOff[i] + cnt[i];
  adjTo = new Uint32Array(adjOff[N]);
  adjK = new Uint8Array(adjOff[N]);
  adjFrom = new Uint32Array(adjOff[N]);
  const cur = adjOff.slice();
  for (let i = 0; i < E; i++) {
    const a = EFLAT[i * 3], b = EFLAT[i * 3 + 1], k = EFLAT[i * 3 + 2];
    let p = cur[a]++;
    adjTo[p] = b; adjK[p] = k; adjFrom[p] = a;
    p = cur[b]++;
    adjTo[p] = a; adjK[p] = k; adjFrom[p] = a;
  }
  grid = new Map();
  for (let i = 0; i < N; i++) {
    const cx = X[i] / CELL | 0, cy = Y[i] / CELL | 0;
    const key = cx + ':' + cy;
    let bucket = grid.get(key);
    if (!bucket) {
      bucket = [];
      bucket.cx = cx;
      bucket.cy = cy;
      grid.set(key, bucket);
    }
    bucket.push(i);
  }
  const rank = [];
  for (let i = 0; i < N; i++) {
    if (G[i] === 1 || G[i] === 2) continue;
    rank.push(i);
  }
  rank.sort(function(a, b) { return R[b] - R[a]; });
  hubs = rank.slice(0, Math.min(rank.length, N > 2500 ? 180 : 320));
  hubMark = new Uint8Array(N);
  for (let h = 0; h < hubs.length; h++) hubMark[hubs[h]] = 1;
  hay = new Array(N);
  for (let i = 0; i < N; i++) {
    const m = META[i] || [];
    hay[i] = [m[0], m[2], m[4], m[5], m[7], m[10], L[i]].join(' ').toLowerCase();
  }
}

function sourceLabel(raw) {
  const src = String(raw || '').trim();
  if (!src) return '';
  if (src === 'text') return 'текст';
  if (/^https?:\/\//i.test(src)) return src;
  const parts = src.split(/[\\/]/);
  return parts[parts.length - 1] || src;
}
function sourceExample(raw) {
  const src = String(raw || '').trim();
  if (/^https?:\/\//i.test(src)) return esc(src);
  return esc(sourceLabel(src));
}

function projectColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 33 + name.charCodeAt(i)) >>> 0;
  return h % PAL.length;
}
function buildClusters() {
  const grouped = new Map();
  for (let i = 0; i < N; i++) {
    if (G[i] === 1 || G[i] === 2) continue;
    const tt = String((META[i] || [])[3] || '');
    if (tt.indexOf('excel_') === 0) continue;
    const m = META[i] || [];
    const project = String(m[10] || '').trim();
    const key = (groupByProject && project) ? ('p:' + project) : (C[i] | 0);
    let g = grouped.get(key);
    if (!g) {
      g = {
        color: (groupByProject && project) ? projectColor(project) : (C[i] | 0),
        nodes: [],
        sumX: 0,
        sumY: 0,
        topic: new Map(),
        source: new Map(),
        project: project
      };
      grouped.set(key, g);
    }
    g.nodes.push(i);
    g.sumX += X[i];
    g.sumY += Y[i];
    const topic = String(m[2] || '').trim();
    if (topic) g.topic.set(topic, (g.topic.get(topic) || 0) + 1);
    const src = String(m[8] || '').trim();
    if (src) g.source.set(src, (g.source.get(src) || 0) + 1);
  }

  clusters = [];
  grouped.forEach(function(g, cid) {
    const count = g.nodes.length;
    if (!count) return;
    const cx = g.sumX / count;
    const cy = g.sumY / count;
    let outer = 60;
    for (let k = 0; k < g.nodes.length; k++) {
      const i = g.nodes[k];
      const dx = X[i] - cx, dy = Y[i] - cy;
      outer = Math.max(outer, Math.sqrt(dx * dx + dy * dy) + R[i] + 10);
    }
    // "Облако" кластера делаем компактным, чтобы не перекрывать весь экран.
    const drawR = Math.max(46, Math.min(190, outer * 0.45 + 26));
    const hitR = Math.max(drawR * 1.18, 64);
    const topics = (groupByProject && g.project)
      ? [g.project]
      : [...g.topic.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(x => x[0]);
    const sources = [...g.source.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4).map(x => x[0]);
    clusters.push({
      id: cid,
      color: g.color,
      n: count,
      cx: cx,
      cy: cy,
      r: drawR,
      hr: hitR,
      topics: topics,
      sources: sources
    });
  });
}

function reindexNode(i, oldX, oldY) {
  const oldKey = (oldX / CELL | 0) + ':' + (oldY / CELL | 0);
  const newKey = (X[i] / CELL | 0) + ':' + (Y[i] / CELL | 0);
  if (oldKey === newKey) return;
  const ob = grid.get(oldKey);
  if (ob) {
    const ix = ob.indexOf(i);
    if (ix >= 0) ob.splice(ix, 1);
  }
  let nb = grid.get(newKey);
  if (!nb) {
    nb = [];
    nb.cx = X[i] / CELL | 0;
    nb.cy = Y[i] / CELL | 0;
    grid.set(newKey, nb);
  }
  nb.push(i);
}

function cellsInView(x0, y0, x1, y1) {
  const cx0 = Math.floor(x0 / CELL), cy0 = Math.floor(y0 / CELL);
  const cx1 = Math.floor(x1 / CELL), cy1 = Math.floor(y1 / CELL);
  const ncell = (cx1 - cx0 + 1) * (cy1 - cy0 + 1);
  const out = [];
  if (ncell > 2500) {
    grid.forEach(function(bucket) {
      if (bucket.cx < cx0 || bucket.cx > cx1 || bucket.cy < cy0 || bucket.cy > cy1) return;
      for (let i = 0; i < bucket.length; i++) out.push(bucket[i]);
    });
    return out;
  }
  for (let cx = cx0; cx <= cx1; cx++) {
    for (let cy = cy0; cy <= cy1; cy++) {
      const b = grid.get(cx + ':' + cy);
      if (!b) continue;
      for (let i = 0; i < b.length; i++) out.push(b[i]);
    }
  }
  return out;
}
function nodeAlpha(i, base) {
  return G[i] === 1 ? base * 0.68 : base;
}
function screenRadius(i) {
  const raw = R[i] * zoom;
  const minR = zoom < 0.18 ? 3.2 : (zoom < 0.45 ? 4.5 : 6);
  return Math.max(minR, raw);
}
function hubRadius(i) {
  return Math.max(8, Math.min(18, R[i] * Math.max(zoom, 0.14)));
}

function resize() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  W = wrap.clientWidth;
  H = wrap.clientHeight;
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (!fitted) fit();
  requestDraw();
}

function requestDraw() {
  if (raf) return;
  raf = requestAnimationFrame(function() { raf = 0; draw(); });
}

function drawNode(i, alpha, ring) {
  const x = sx(X[i]), y = sy(Y[i]);
  const r = screenRadius(i);
  ctx.globalAlpha = nodeAlpha(i, alpha);
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = fillOf(i);
  ctx.fill();
  ctx.lineWidth = ring ? 2.6 : (G[i] ? 2.2 : 1.8);
  ctx.strokeStyle = ring ? '#60a5fa' : strokeOf(i);
  ctx.stroke();
}

let labelBoxes = [];
let hitLabels = [];
function pushHitLabel(i, tx, ty, align, baseline) {
  const tw = ctx.measureText(L[i] || '').width;
  let left = tx, top = ty;
  if (align === 'center') left = tx - tw / 2;
  else if (align === 'right') left = tx - tw;
  if (baseline === 'middle') top = ty - 8;
  hitLabels.push({ i: i, x: left - 6, y: top - 3, w: tw + 12, h: 20 });
}
function labelRank(i) {
  if (hot && hot[i] === 2) return -1;
  const tt = String((META[i] || [])[3] || '');
  if (tt === 'excel_sheet') return 0;
  if (tt === 'excel_column') return 1;
  if (tt === 'excel_row') return 2;
  if (tt === 'excel_value') return 3;
  return 4;
}
function reserveLabel(x, y, text, align, baseline) {
  const w = ctx.measureText(text).width;
  const h = 12;
  let left = x;
  let top = y;
  if (align === 'center') left = x - w / 2;
  else if (align === 'right') left = x - w;
  if (baseline === 'middle') top = y - h / 2;
  const box = { x: left - 2, y: top - 1, w: w + 4, h: h + 2 };
  for (let i = 0; i < labelBoxes.length; i++) {
    const b = labelBoxes[i];
    if (box.x < b.x + b.w && box.x + box.w > b.x && box.y < b.y + b.h && box.y + box.h > b.y) return false;
  }
  labelBoxes.push(box);
  return true;
}
function wantsLabel(i) {
  if (hot && hot[i]) return true;
  const rank = labelRank(i);
  if (zoom < 0.22) return rank <= 1 || R[i] >= 40;
  if (zoom < 0.5) return rank <= 1 || R[i] >= 30;
  return true;
}
function drawLabel(i, alpha, light) {
  const tt = String((META[i] || [])[3] || '');
  const excelNode = tt.indexOf('excel_') === 0;
  const r = screenRadius(i);
  if (!wantsLabel(i)) return false;
  if (!L[i]) return false;
  const excel = excelNode && tt !== 'excel_sheet';
  ctx.font = '12px Arial';
  let tx, ty, align, baseline;
  if (excel) {
    const dir = outwardOf(i);
    align = dir[0] >= 0 ? 'left' : 'right';
    baseline = 'middle';
    tx = sx(X[i]) + dir[0] * (r + 8);
    ty = sy(Y[i]) + dir[1] * (r + 4);
  } else {
    align = 'center';
    baseline = 'top';
    tx = sx(X[i]);
    ty = sy(Y[i]) + r + 3;
  }
  if (!reserveLabel(tx, ty, L[i], align, baseline)) return false;
  pushHitLabel(i, tx, ty, align, baseline);
  ctx.globalAlpha = Math.max(alpha, 0.92);
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  const tw = ctx.measureText(L[i]).width;
  const padX = 4;
  let left = tx - padX, top = ty - 1;
  if (align === 'center') left = tx - tw / 2 - padX;
  else if (align === 'right') left = tx - tw - padX;
  if (baseline === 'middle') top = ty - 8;
  ctx.fillStyle = light ? 'rgba(248,250,252,0.94)' : 'rgba(15,23,42,0.9)';
  roundBox(left, top, tw + padX * 2, 16, 4);
  ctx.fill();
  ctx.fillStyle = light ? '#0f172a' : '#f8fafc';
  ctx.fillText(L[i], tx, ty);
  return true;
}

function roundBox(x, y, w, h, radius) {
  const r = Math.min(radius, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function drawBranchCaptions(light) {
  const items = clusters.slice().sort(function(a, b) { return b.n - a.n; });
  ctx.font = '600 13px Arial';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const taken = [];
  for (let i = 0; i < items.length; i++) {
    const c = items[i];
    const px = sx(c.cx), py = sy(c.cy);
    if (px < 24 || py < 20 || px > W - 24 || py > H - 20) continue;
    let crowded = false;
    for (let k = 0; k < taken.length; k++) {
      if (Math.abs(taken[k][0] - px) < 130 && Math.abs(taken[k][1] - py) < 34) {
        crowded = true;
        break;
      }
    }
    if (crowded) continue;
    taken.push([px, py]);
    const raw = (c.topics && c.topics[0]) ? c.topics[0] : 'Ветка';
    const name = raw.length > 22 ? raw.slice(0, 21) + '…' : raw;
    const text = name + ' · ' + c.n;
    const w = Math.min(240, ctx.measureText(text).width + 18);
    const pal = PAL[c.color % PAL.length];
    ctx.globalAlpha = 0.94;
    ctx.fillStyle = light ? 'rgba(255,255,255,0.94)' : 'rgba(15,23,42,0.9)';
    roundBox(px - w / 2, py - 13, w, 26, 8);
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = pal[1];
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = light ? '#0f172a' : '#f8fafc';
    ctx.fillText(text, px, py);
  }
}
function drawHubs(light) {
  labelBoxes = [];
  ctx.font = '12px Arial';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let h = 0; h < hubs.length; h++) {
    const i = hubs[h];
    if (!nodeVisible(i)) continue;
    const x = sx(X[i]), y = sy(Y[i]);
    if (x < -24 || y < -24 || x > W + 24 || y > H + 24) continue;
    const r = hubRadius(i);
    ctx.globalAlpha = 1;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = fillOf(i);
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = light ? '#ffffff' : '#0b1220';
    ctx.stroke();
    if (!L[i]) continue;
    const ty = y + r + 3;
    if (!reserveLabel(x, ty, L[i], 'center', 'top')) continue;
    pushHitLabel(i, x, ty, 'center', 'top');
    const tw = ctx.measureText(L[i]).width;
    ctx.fillStyle = light ? 'rgba(248,250,252,0.94)' : 'rgba(15,23,42,0.9)';
    roundBox(x - tw / 2 - 4, ty - 1, tw + 8, 16, 4);
    ctx.fill();
    ctx.fillStyle = light ? '#0f172a' : '#f8fafc';
    ctx.fillText(L[i], x, ty);
  }
}
function drawOverview(x0, y0, x1, y1, light) {
  const bin = 12;
  const cols = Math.ceil(W / bin) + 2;
  const rows = Math.ceil(H / bin) + 2;
  const best = new Int32Array(cols * rows);
  const bestR = new Uint16Array(cols * rows);
  best.fill(-1);
  dotPick = best;
  dotCols = cols;
  dotBin = bin;
  const outline = light ? '#ffffff' : '#0b1220';
  for (let i = 0; i < N; i++) {
    if (G[i] === 2) continue;
    if (G[i] === 1 && !tagsOn) continue;
    const px = sx(X[i]), py = sy(Y[i]);
    if (px < -bin || py < -bin || px > W + bin || py > H + bin) continue;
    const bx = (px / bin) | 0, by = (py / bin) | 0;
    if (bx < 0 || by < 0 || bx >= cols || by >= rows) continue;
    const id = bx + by * cols;
    if (best[id] >= 0 && R[i] <= bestR[id]) continue;
    best[id] = i;
    bestR[id] = R[i];
  }
  for (let id = 0; id < best.length; id++) {
    const i = best[id];
    if (i < 0) continue;
    const bx = id % cols, by = (id / cols) | 0;
    ctx.globalAlpha = nodeAlpha(i, 0.9);
    ctx.beginPath();
    ctx.arc(bx * bin + bin / 2, by * bin + bin / 2, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = fillOf(i);
    ctx.fill();
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = outline;
    ctx.stroke();
  }
  drawBranchCaptions(light);
  drawHubs(light);
}

function draw() {
  hitLabels = [];
  const light = isLight();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalAlpha = 1;
  ctx.fillStyle = light ? '#f8fafc' : '#0b1220';
  ctx.fillRect(0, 0, W, H);
  if (!fitted) fit();
  if (!N) {
    ctx.fillStyle = light ? '#6b7280' : '#94a3b8';
    ctx.font = '16px Arial';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Граф пуст', W / 2, H / 2);
    return;
  }
  const pad = 90 / Math.max(zoom, 0.02);
  const x0 = wx(0) - pad, y0 = wy(0) - pad, x1 = wx(W) + pad, y1 = wy(H) + pad;
  let overview = zoom < 0.07 || (N > 2000 && zoom < 0.24);
  let vis = null;
  if (!overview) {
    vis = cellsInView(x0, y0, x1, y1);
    if (vis.length > 1600) overview = true;
  }
  if (overview) {
    drawOverview(x0, y0, x1, y1, light);
    drawHotMarks();
    ctx.globalAlpha = 1;
    return;
  }
  dotPick = null;
  const showTags = tagsOn && zoom >= 0.28;
  const showEdges = true;
  const showLabels = true;
  const dim = hot ? 0.16 : 1;
  const maxE = vis.length < 900 ? 24000 : 8000;
  const maxN = vis.length;

  // Показываем облака кластеров только на общем плане, чтобы не шуметь при приближении.
  if (zoom <= 0.4 || clusterHot >= 0) {
    for (let ci = 0; ci < clusters.length; ci++) {
      const c = clusters[ci];
      const sr = c.r * zoom;
      if (sr < 12) continue;
      const sx0 = sx(c.cx), sy0 = sy(c.cy);
      if (sx0 + sr < -30 || sx0 - sr > W + 30 || sy0 + sr < -30 || sy0 - sr > H + 30) continue;
      const pal = PAL[c.color % PAL.length];
      const active = clusterHot === ci;
      ctx.globalAlpha = active ? 0.16 : 0.1;
      ctx.beginPath();
      ctx.arc(sx0, sy0, sr, 0, Math.PI * 2);
      ctx.fillStyle = pal[0];
      ctx.fill();
      ctx.globalAlpha = active ? 0.7 : 0.34;
      ctx.lineWidth = active ? 2.2 : 1.4;
      ctx.strokeStyle = pal[1];
      ctx.stroke();
    }
  }

  const painted = new Uint8Array(N);
  let budget = maxN;
  for (let v = 0; v < vis.length && budget > 0; v++) {
    const i = vis[v];
    if (!nodeVisible(i)) continue;
    painted[i] = 1;
    budget--;
  }
  if (hot) {
    for (let i = 0; i < N; i++) {
      if (!hot[i]) continue;
      if (X[i] < x0 || X[i] > x1 || Y[i] < y0 || Y[i] > y1) continue;
      if (!nodeVisible(i)) continue;
      painted[i] = 1;
    }
  }

  if (showEdges && adjOff) {
    let drawn = 0;
    for (let v = 0; v < vis.length && drawn < maxE; v++) {
      const a = vis[v];
      if (!painted[a]) continue;
      const start = adjOff[a], end = adjOff[a + 1];
      for (let p = start; p < end && drawn < maxE; p++) {
        const b = adjTo[p];
        if (painted[b] && b < a) continue;
        const k = adjK[p];
        if (k === 1 && !showTags) continue;
        const rowEdge = G[a] === 2 || G[b] === 2;
        const tagEdge = k === 1;
        const focusEdge = hot && (hot[a] === 2 || hot[b] === 2);
        const bridge = k === 3;
        if (!bridge && k === 2 && !(selected >= 0 && (a === selected || b === selected) && rowEdge)) continue;
        if (!bridge && rowEdge && selected !== a && selected !== b) continue;
        const ax = X[a], ay = Y[a], bx = X[b], by = Y[b];
        if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
        const dx = bx - ax, dy = by - ay;
        const len = Math.hypot(dx, dy);
        if (len < 1) continue;
        const ra = screenRadius(a) / zoom, rb = screenRadius(b) / zoom;
        if ((len - ra - rb) * zoom < 1) continue;
        const ux = dx / len, uy = dy / len;
        ctx.globalAlpha = focusEdge ? 0.9 : (tagEdge ? 0.34 : (bridge ? 0.72 : Math.max(dim, 0.5)));
        ctx.strokeStyle = focusEdge ? '#2563eb' : (tagEdge ? (light ? '#cbd5e1' : '#64748b') : (bridge ? '#c026d3' : (light ? '#64748b' : '#94a3b8')));
        ctx.lineWidth = tagEdge ? 1 : (bridge ? 2 : 1.2);
        ctx.setLineDash(k === 1 || bridge ? [6, 4] : []);
        ctx.beginPath();
        ctx.moveTo(sx(ax + ux * ra), sy(ay + uy * ra));
        ctx.lineTo(sx(bx - ux * rb), sy(by - uy * rb));
        ctx.stroke();
        drawn++;
      }
    }
    ctx.setLineDash([]);
  }

  let drawnN = 0;
  for (let v = 0; v < vis.length && drawnN < maxN; v++) {
    const i = vis[v];
    if (!nodeVisible(i)) continue;
    if (hot && isHot(i)) continue;
    drawNode(i, dim, false);
    drawnN++;
  }
  if (hot) {
    let marked = 0;
    for (let i = 0; i < N && marked < 900; i++) {
      if (!hot[i]) continue;
      if (X[i] < x0 || X[i] > x1 || Y[i] < y0 || Y[i] > y1) continue;
      if (!nodeVisible(i)) continue;
      drawNode(i, 1, hot[i] === 2);
      marked++;
    }
  }
  if (showLabels) {
    labelBoxes = [];
    const ids = [];
    for (let v = 0; v < vis.length; v++) {
      const i = vis[v];
      if (!nodeVisible(i)) continue;
      if (hot && !isHot(i)) continue;
      ids.push(i);
    }
    ids.sort(function(a, b) { return labelRank(a) - labelRank(b) || R[b] - R[a]; });
    let drawnL = 0;
    for (let k = 0; k < ids.length && drawnL < 320; k++) {
      if (drawLabel(ids[k], hot ? 1 : dim, light)) drawnL++;
    }
  }
  ctx.globalAlpha = 1;
}

function hit(px, py) {
  for (let k = hitLabels.length - 1; k >= 0; k--) {
    const b = hitLabels[k];
    if (px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h) return b.i;
  }
  const reach = 64 / Math.max(zoom, 0.04);
  const x = wx(px), y = wy(py);
  const cand = cellsInView(x - reach, y - reach, x + reach, y + reach);
  let best = -1, bestD = 1e12;
  for (let c = 0; c < cand.length; c++) {
    const i = cand[c];
    const hub = dotPick && hubMark && hubMark[i];
    if (!nodeVisible(i) && !hub) continue;
    const cx = sx(X[i]), cy = sy(Y[i]);
    const dx = cx - px, dy = cy - py;
    const d2 = dx * dx + dy * dy;
    const r = hub ? hubRadius(i) : Math.max(screenRadius(i), 11);
    if (d2 <= (r + 8) * (r + 8) && d2 < bestD) { best = i; bestD = d2; }
  }
  if (best >= 0) return best;
  if (!dotPick) return -1;
  const bin = dotBin;
  const bx = (px / bin) | 0, by = (py / bin) | 0;
  for (let oy = -1; oy <= 1; oy++) {
    for (let ox = -1; ox <= 1; ox++) {
      const cx = bx + ox, cy = by + oy;
      if (cx < 0 || cy < 0 || cx >= dotCols) continue;
      const id = cx + cy * dotCols;
      if (id < 0 || id >= dotPick.length) continue;
      const i = dotPick[id];
      if (i < 0) continue;
      const dx = cx * bin + bin / 2 - px;
      const dy = cy * bin + bin / 2 - py;
      const d2 = dx * dx + dy * dy;
      if (d2 <= 144 && d2 < bestD) { best = i; bestD = d2; }
    }
  }
  return best;
}
function drawHotMarks() {
  if (!hot) return;
  let marked = 0;
  for (let i = 0; i < N && marked < 240; i++) {
    if (!hot[i]) continue;
    const x = sx(X[i]), y = sy(Y[i]);
    if (x < -30 || y < -30 || x > W + 30 || y > H + 30) continue;
    const r = Math.max(hot[i] === 2 ? 10 : 6, Math.min(18, screenRadius(i)));
    ctx.globalAlpha = nodeAlpha(i, 1);
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = fillOf(i);
    ctx.fill();
    ctx.lineWidth = hot[i] === 2 ? 3 : 1.6;
    ctx.strokeStyle = hot[i] === 2 ? '#60a5fa' : strokeOf(i);
    ctx.stroke();
    marked++;
  }
}

function hitCluster(px, py) {
  const x = wx(px), y = wy(py);
  let best = -1;
  let bestScore = Infinity;
  for (let i = 0; i < clusters.length; i++) {
    const c = clusters[i];
    const dx = x - c.cx, dy = y - c.cy;
    const d = Math.sqrt(dx * dx + dy * dy);
    const hr = c.hr || c.r;
    if (d > hr) continue;
    const score = (hr - d);
    if (score < bestScore) {
      best = i;
      bestScore = score;
    }
  }
  return best;
}
function clusterByColor(colorId) {
  const cid = colorId | 0;
  for (let i = 0; i < clusters.length; i++) {
    if ((clusters[i].color | 0) === cid) return i;
  }
  return -1;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function hideClusterTip() {
  const tip = document.getElementById('cluster-tip');
  if (!tip) return;
  tip.style.display = 'none';
}
function showClusterTip(ci, px, py) {
  const tip = document.getElementById('cluster-tip');
  if (!tip || ci < 0 || ci >= clusters.length) return;
  const c = clusters[ci];
  const topic = c.topics.length ? c.topics.map(esc).join(' / ') : (groupByProject ? 'Проект' : 'Темы без названия');
  const samples = c.sources.length
    ? c.sources.map(function(s) { return '<li>' + sourceExample(s) + '</li>'; }).join('')
    : '<li>источник не указан</li>';
  tip.innerHTML =
    '<div class="ct-title">' + (groupByProject ? 'Проект' : 'О чем этот кластер') + '</div>' +
    '<div class="ct-row"><b>Карточек:</b> ' + c.n + '</div>' +
    '<div class="ct-row"><b>' + (groupByProject ? 'Название:' : 'Ключевые темы:') + '</b> ' + topic + '</div>' +
    '<div class="ct-row"><b>Примеры источников:</b><ul>' + samples + '</ul></div>';
  tip.style.display = 'block';
  const pad = 12;
  const tw = tip.offsetWidth || 320;
  const th = tip.offsetHeight || 160;
  let left = Math.round(px + 14);
  let top = Math.round(py + 14);
  if (left + tw > W - pad) left = Math.max(pad, W - tw - pad);
  if (top + th > H - pad) top = Math.max(pad, H - th - pad);
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function closeDetail() {
  document.getElementById('detail-panel').style.display = 'none';
  selected = -1;
  setHot(-1);
  requestDraw();
}
function showDetail(idx) {
  selected = idx;
  setHot(idx);
  const m = META[idx] || [];
  const panel = document.getElementById('detail-panel');
  const content = document.getElementById('detail-content');
  let html = '<h2>' + esc(L[idx]) + '</h2>';
  if (G[idx] === 0) {
    html += '<div class="field-value"><b>[' + esc(m[0]) + ']</b> ' +
      (m[1] ? '← [' + esc(m[1]) + ']' : 'корневая') + '</div>';
    if (m[2]) html += '<div class="field"><div class="field-label">Тема</div><div class="field-value">' + esc(m[2]) + '</div></div>';
    html += '<div class="field"><div class="field-label">Тип</div><div class="field-value">' + esc(m[3]) + '</div></div>';
    html += '<div class="field"><div class="field-label">Теги</div><div class="field-value">' + esc(m[4] || '—') + '</div></div>';
    const src = (m[8] || '').trim();
    if (src) {
      html += '<div class="field"><div class="field-label">Источник</div><div class="field-value">';
      if (/^https?:\/\//i.test(src)) {
        html += '<a href="' + esc(src) + '" target="_blank" rel="noopener noreferrer">' + esc(src) + '</a>';
      } else if (src === 'text') {
        html += 'текст';
      } else {
        html += esc(src);
      }
      html += '</div></div>';
    }
    const quote = (m[9] || '').trim();
    if (quote) {
      html += '<div class="field"><div class="field-label">Абзац в источнике</div><div class="field-value">' + esc(quote) + '</div></div>';
    }
    html += '<hr/><div class="field-value">' + esc(m[5]) + '</div>';
  } else {
    html += '<div class="field-value">🏷 ' + esc(m[2]) + '</div>';
    html += '<div class="field-label">' + esc(m[3]) + '</div>';
    html += '<div class="field-value">' + esc(m[5]) + '</div>';
  }
  const start = adjOff[idx], end = adjOff[idx + 1];
  const nbrCount = end - start;
  if (nbrCount) {
    const entityNeighbors = [];
    const rowNeighbors = [];
    const thoughtNeighbors = [];
    const seen = new Uint8Array(N);
    for (let i = 0; i < nbrCount; i++) {
      const ni = adjTo[start + i];
      if (seen[ni]) continue;
      seen[ni] = 1;
      const k = adjK[start + i];
      const from = adjFrom[start + i];
      const pm = META[ni] || [];
      const tt = String(pm[3] || '');
      let arrow = '';
      if (G[ni] !== 1) {
        if (k === 0) {
          if (ni === from) arrow = '→ ';
          else if (idx === from) arrow = '← ';
        } else {
          arrow = from === idx ? '→ ' : '← ';
        }
      }
      const item = '<div class="neighbor-item" onclick="focusNode(' + ni + ')">' +
        arrow + '<b>' + esc(pm[6] || L[ni]) + '</b></div>';
      if (G[ni] === 1) entityNeighbors.push(item);
      else if (G[ni] === 2 || tt === 'excel_row') rowNeighbors.push(item);
      else thoughtNeighbors.push(item);
    }
    const rowLimit = 80;
    const shownRows = rowNeighbors.slice(0, rowLimit);
    html += '<hr/><div class="neighbors">';
    if (rowNeighbors.length) {
      html += '<div class="field-label">Записи таблицы (' + rowNeighbors.length + ')</div>' + shownRows.join('');
      if (rowNeighbors.length > rowLimit) html += '<div class="field-label">ещё ' + (rowNeighbors.length - rowLimit) + '</div>';
    }
    if (thoughtNeighbors.length || entityNeighbors.length) {
      html += '<div class="field-label">Связи (' + (thoughtNeighbors.length + entityNeighbors.length) + ')</div>' +
        entityNeighbors.join('') + thoughtNeighbors.join('');
    }
    html += '</div>';
  }
  content.innerHTML = html;
  panel.style.display = 'block';
  requestDraw();
}
function focusNode(idx) {
  if (idx < 0 || idx >= N) return;
  if (G[idx] === 1) {
    tagsOn = true;
    const b = document.getElementById('btn-tags');
    if (b) b.classList.add('active');
  }
  if (G[idx] === 2) {
    rowsOn = true;
    const b = document.getElementById('btn-rows');
    if (b) b.classList.add('active');
  }
  camX = X[idx]; camY = Y[idx];
  zoom = Math.max(zoom, 0.9);
  showDetail(idx);
}

function fit() {
  if (!N || W < 80 || H < 80) return;
  const useHubs = hubs.length > 2;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const total = useHubs ? hubs.length : N;
  for (let k = 0; k < total; k++) {
    const i = useHubs ? hubs[k] : k;
    if (!useHubs && !tagsOn && G[i] === 1) continue;
    if (!useHubs && G[i] === 2) continue;
    const x = X[i], y = Y[i];
    if (!isFinite(x) || !isFinite(y)) continue;
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  if (!isFinite(minX)) return;
  camX = (minX + maxX) / 2;
  camY = (minY + maxY) / 2;
  const dx = Math.max(maxX - minX, 200);
  const dy = Math.max(maxY - minY, 200);
  const next = Math.min((W - 80) / dx, (H - 80) / dy);
  zoom = Math.min(1.4, next > 0 && isFinite(next) ? next : 0.2);
  fitted = true;
}

wrap.addEventListener('mousedown', function(e) {
  const rect = wrap.getBoundingClientRect();
  const i = hit(e.clientX - rect.left, e.clientY - rect.top);
  moved = false;
  lastX = e.clientX;
  lastY = e.clientY;
  if (i >= 0) { nodeDrag = i; panDrag = false; }
  else { nodeDrag = -1; panDrag = true; }
});
window.addEventListener('mouseup', function(e) {
  if (nodeDrag < 0 && !panDrag) return;
  if (!moved) {
    const rect = wrap.getBoundingClientRect();
    const i = hit(e.clientX - rect.left, e.clientY - rect.top);
    if (i >= 0) showDetail(i); else closeDetail();
  }
  nodeDrag = -1;
  panDrag = false;
  wrap.style.cursor = 'grab';
});
window.addEventListener('mousemove', function(e) {
  if (nodeDrag < 0 && !panDrag) {
    const rect = wrap.getBoundingClientRect();
    const lx = e.clientX - rect.left;
    const ly = e.clientY - rect.top;
    hoverX = lx; hoverY = ly;
    const ni = hit(lx, ly);
    let nextCluster = -1;
    if (ni >= 0) {
      if (G[ni] === 0) nextCluster = clusterByColor(C[ni]);
      wrap.style.cursor = 'pointer';
    } else {
      nextCluster = hitCluster(lx, ly);
      wrap.style.cursor = nextCluster >= 0 ? 'help' : 'grab';
    }
    if (ni === hoverNode && nextCluster === clusterHot) return;
    hoverNode = ni;
    clusterHot = nextCluster;
    if (clusterHot >= 0) showClusterTip(clusterHot, lx, ly);
    else hideClusterTip();
    requestDraw();
    return;
  }
  const dx = e.clientX - lastX, dy = e.clientY - lastY;
  if (Math.abs(dx) + Math.abs(dy) > 6) moved = true;
  lastX = e.clientX; lastY = e.clientY;
  if (nodeDrag >= 0 && moved) {
    const oldX = X[nodeDrag], oldY = Y[nodeDrag];
    X[nodeDrag] += dx / zoom;
    Y[nodeDrag] += dy / zoom;
    reindexNode(nodeDrag, oldX, oldY);
    wrap.style.cursor = 'grabbing';
  } else if (panDrag) {
    camX -= dx / zoom; camY -= dy / zoom;
    wrap.style.cursor = 'grabbing';
  }
  requestDraw();
});
function applyZoomAt(mx, my, factor) {
  if (!factor || factor === 1) return;
  const beforeX = wx(mx), beforeY = wy(my);
  zoom = Math.max(1e-8, Math.min(3.5, zoom * factor));
  camX = beforeX - (mx - W / 2) / zoom;
  camY = beforeY - (my - H / 2) / zoom;
  requestDraw();
}
function zoomByWheel(e, mx, my) {
  let dy = e.deltaY;
  if (e.deltaMode === 1) dy *= 16;
  else if (e.deltaMode === 2) dy *= 400;
  if (!dy) return;
  const pinch = e.ctrlKey || e.metaKey;
  const abs = Math.abs(dy);
  const gain = pinch ? 0.012 : (abs < 50 ? 0.01 : 0.0016);
  const factor = Math.max(0.82, Math.min(1.22, Math.exp(-dy * gain)));
  applyZoomAt(mx, my, factor);
}
let pinchGesture = false;
window.addEventListener('wheel', function(e) {
  if (!wrap.contains(e.target)) return;
  e.preventDefault();
  if (pinchGesture) return;
  const rect = wrap.getBoundingClientRect();
  zoomByWheel(e, e.clientX - rect.left, e.clientY - rect.top);
}, { passive: false, capture: true });
wrap.addEventListener('gesturestart', function(e) {
  e.preventDefault();
  pinchGesture = true;
  wrap._gScale = e.scale || 1;
}, { passive: false });
wrap.addEventListener('gesturechange', function(e) {
  e.preventDefault();
  const prev = wrap._gScale || 1;
  const next = e.scale || prev;
  wrap._gScale = next;
  if (!(prev > 0) || next === prev) return;
  const rect = wrap.getBoundingClientRect();
  applyZoomAt(e.clientX - rect.left, e.clientY - rect.top, next / prev);
}, { passive: false });
wrap.addEventListener('gestureend', function() {
  wrap._gScale = 1;
  setTimeout(function() { pinchGesture = false; }, 60);
});
wrap.addEventListener('dblclick', function(e) {
  const rect = wrap.getBoundingClientRect();
  const i = hit(e.clientX - rect.left, e.clientY - rect.top);
  if (i >= 0) focusNode(i);
});

let searchTimer = 0;
function applySearch(raw) {
  const q = (raw || '').toLowerCase().trim();
  if (!q) {
    visMask = null;
    if (selected >= 0) setHot(selected);
    else setHot(-1);
    requestDraw();
    return;
  }
  visMask = new Uint8Array(N);
  const matched = [];
  let tagHit = false;
  let rowHit = false;
  for (let i = 0; i < N; i++) {
    if (hay[i].indexOf(q) !== -1) {
      visMask[i] = 1;
      matched.push(i);
      if (G[i] === 1) tagHit = true;
      if (G[i] === 2) rowHit = true;
    }
  }
  hot = matched.length ? new Uint8Array(N) : null;
  for (let m = 0; m < matched.length; m++) {
    const i = matched[m];
    hot[i] = 2;
    const start = adjOff[i], end = adjOff[i + 1];
    for (let p = start; p < end; p++) {
      const ni = adjTo[p];
      visMask[ni] = 1;
      if (hot[ni] !== 2) hot[ni] = 1;
    }
  }
  if (tagHit && !tagsOn) {
    tagsOn = true;
    const b = document.getElementById('btn-tags');
    if (b) b.classList.add('active');
  }
  if (rowHit && !rowsOn) {
    rowsOn = true;
    const b = document.getElementById('btn-rows');
    if (b) b.classList.add('active');
  }
  if (matched.length) frameMatches(matched);
  requestDraw();
}
function frameMatches(ids) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const step = Math.max(1, Math.ceil(ids.length / 120));
  for (let i = 0; i < ids.length; i += step) {
    const id = ids[i];
    if (X[id] < minX) minX = X[id];
    if (Y[id] < minY) minY = Y[id];
    if (X[id] > maxX) maxX = X[id];
    if (Y[id] > maxY) maxY = Y[id];
  }
  if (!isFinite(minX)) return;
  camX = (minX + maxX) / 2;
  camY = (minY + maxY) / 2;
  const dx = Math.max(maxX - minX, 260);
  const dy = Math.max(maxY - minY, 260);
  zoom = Math.max(0.12, Math.min(1.2, Math.min((W - 140) / dx, (H - 140) / dy)));
}
document.getElementById('searchInput').addEventListener('input', function() {
  const v = this.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function() { applySearch(v); }, 40);
});

const tagsBtn = document.getElementById('btn-tags');
tagsBtn.addEventListener('click', function() {
  tagsOn = !tagsOn;
  tagsBtn.classList.toggle('active', tagsOn);
  requestDraw();
});
const rowsBtn = document.getElementById('btn-rows');
if (rowsBtn) rowsBtn.addEventListener('click', function() {
  rowsOn = !rowsOn;
  rowsBtn.classList.toggle('active', rowsOn);
  requestDraw();
});
const themeToggleBtn = document.getElementById('theme-toggle');
const savedTheme = localStorage.getItem('theme') || localStorage.getItem('exocortex_theme') || 'light';
if (savedTheme === 'dark') {
  document.body.classList.remove('light-theme');
  themeToggleBtn.textContent = 'Светлая тема';
}
themeToggleBtn.addEventListener('click', function() {
  const isLightNow = document.body.classList.toggle('light-theme');
  const next = isLightNow ? 'light' : 'dark';
  themeToggleBtn.textContent = isLightNow ? 'Темная тема' : 'Светлая тема';
  localStorage.setItem('exocortex_theme', next);
  localStorage.setItem('theme', next);
  requestDraw();
});

window.addEventListener('resize', resize);
window.focusNode = focusNode;
window.closeDetail = closeDetail;

async function inflateB64(b64) {
  const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  if (typeof DecompressionStream === 'function') {
    const ds = new DecompressionStream('gzip');
    const buf = await new Response(new Blob([bin]).stream().pipeThrough(ds)).arrayBuffer();
    return JSON.parse(new TextDecoder().decode(buf));
  }
  throw new Error('Нет DecompressionStream');
}

function applyPayload(data) {
  N = data.n || 0;
  X = Float32Array.from(data.x || []);
  Y = Float32Array.from(data.y || []);
  R = Uint16Array.from(data.r || []);
  G = Uint8Array.from(data.g || []);
  C = Uint8Array.from(data.c || []);
  L = data.l || [];
  META = data.m || [];
  EFLAT = data.e || [];
  E = (EFLAT.length / 3) | 0;
  groupByProject = !!data.gp;
  hot = null;
  visMask = null;
  revealedRows = [];
  revealedMark = null;
  tagsOn = N < 800 && E < 2500;
  rowsOn = false;
  if (tagsOn) tagsBtn.classList.add('active');
  else tagsBtn.classList.remove('active');
  if (rowsBtn) rowsBtn.classList.remove('active');
  document.getElementById('stat-z').textContent = data.tz || 0;
  document.getElementById('stat-e').textContent = data.te || 0;
  document.getElementById('stat-r').textContent = data.tr || 0;
  buildIndex();
  clusterHot = -1;
  hideClusterTip();
  fitted = false;
  resize();
  requestDraw();
  setTimeout(function() {
    buildClusters();
    requestDraw();
  }, 0);
}

function setLoad(pct, caption) {
  const n = Math.max(0, Math.min(100, Math.round(pct)));
  const el = document.getElementById('load-pct');
  const arc = document.getElementById('load-arc');
  const cap = document.getElementById('load-caption');
  if (el) el.textContent = n + '%';
  if (cap && caption) cap.textContent = caption;
  if (arc) {
    const circ = 175.9;
    arc.classList.toggle('spin', n <= 0);
    arc.style.strokeDashoffset = n <= 0 ? '132' : String(circ * (1 - n / 100));
  }
}
function paintLoad() {
  return new Promise(function(resolve) { requestAnimationFrame(resolve); });
}
async function readGraph(resp) {
  const total = Number(resp.headers.get('x-graph-size') || resp.headers.get('content-length') || 0);
  if (!resp.body || !resp.body.getReader) {
    setLoad(30, 'Загрузка графа');
    return resp.json();
  }
  const reader = resp.body.getReader();
  const chunks = [];
  let got = 0;
  setLoad(0, 'Загрузка графа');
  while (true) {
    const step = await reader.read();
    if (step.done) break;
    chunks.push(step.value);
    got += step.value.length;
    const ratio = total > 0 ? got / total : Math.min(0.85, 0.08 + Math.log10(got + 10) / 8);
    setLoad(Math.min(90, ratio * 90), 'Загрузка графа');
  }
  setLoad(94, 'Сборка графа');
  await paintLoad();
  let len = 0;
  for (let i = 0; i < chunks.length; i++) len += chunks[i].length;
  const buf = new Uint8Array(len);
  let off = 0;
  for (let i = 0; i < chunks.length; i++) {
    buf.set(chunks[i], off);
    off += chunks[i].length;
  }
  const data = JSON.parse(new TextDecoder().decode(buf));
  setLoad(100, 'Готово');
  await paintLoad();
  return data;
}
(async function boot() {
  const hint = document.getElementById('load-hint');
  try {
    let data;
    if (DATA_URL) {
      const resp = await fetch(DATA_URL, { headers: { 'Accept': 'application/json' } });
      if (!resp.ok) throw new Error('Не удалось загрузить граф');
      data = await readGraph(resp);
    } else if (DATA_B64) {
      setLoad(20, 'Загрузка графа');
      await paintLoad();
      data = await inflateB64(DATA_B64);
      setLoad(100, 'Готово');
      await paintLoad();
    } else {
      data = { n: 0, x: [], y: [], r: [], g: [], c: [], l: [], m: [], e: [], tz: 0, te: 0, tr: 0 };
    }
    if (hint) hint.remove();
    applyPayload(data);
  } catch (err) {
    setLoad(0, 'Ошибка загрузки');
    const cap = document.getElementById('load-caption');
    if (cap) cap.textContent = 'Ошибка загрузки графа: ' + err.message;
    else if (hint) hint.textContent = 'Ошибка загрузки графа: ' + err.message;
  }
})();
</script>
</body>
</html>
"""


def _build_html(
    graph_data: Optional[Dict[str, Any]] = None,
    user_label: str = "",
    data_url: Optional[str] = None,
    back_url: str = "",
    back_label: str = "← В проект",
) -> str:
    heading = html_escape((user_label or "").strip() or "Цифровой экзокортекс")
    data_url_js = json.dumps(data_url or "")
    back_href = html_escape((back_url or "").strip())
    back_text = html_escape((back_label or "← В проект").strip())
    back_html = (
        f'<a id="graph-back" href="{back_href}">{back_text}</a>'
        if back_href
        else ""
    )
    data_b64 = ""
    total_z = total_e = total_r = 0
    if not data_url and graph_data:
        payload = pack_graph_payload(graph_data)
        total_z, total_e, total_r = payload["tz"], payload["te"], payload["tr"]
        data_b64 = base64.b64encode(gzip.compress(dumps_payload(payload), compresslevel=4)).decode("ascii")
    data_b64_js = json.dumps(data_b64)
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{heading}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0b1220; color: #e5e7eb; font-family: 'Segoe UI', Arial, sans-serif; overflow: hidden; height: 100vh; }}
  #header {{
    background: linear-gradient(90deg, #0f172a 0%, #111827 100%);
    padding: 14px 24px; display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid #334155; z-index: 10; position: relative;
  }}
  #header-left {{ display: flex; align-items: center; gap: 14px; min-width: 0; flex: 1; }}
  #graph-back {{
    color: #93c5fd; text-decoration: none; font-size: 14px; white-space: nowrap; flex-shrink: 0;
  }}
  #graph-back:hover {{ text-decoration: underline; }}
  #header h1 {{ font-size: 20px; color: #e2e8f0; font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-right: 16px; }}
  #header-right {{ display: flex; align-items: center; gap: 12px; }}
  #stats {{ font-size: 13px; color: #94a3b8; display: flex; gap: 18px; }}
  #stats span {{ color: #cbd5e1; font-weight: 600; }}
  #theme-toggle, #graph-controls button {{
    border: 1px solid #334155; background: #0f172a; color: #e2e8f0;
    border-radius: 8px; padding: 6px 10px; font-size: 12px; cursor: pointer;
  }}
  #theme-toggle:hover, #graph-controls button:hover {{ background: #1e293b; }}
  #graph-controls button.active {{ border-color: #60a5fa; background: #1e3a5f; }}
  #legend {{
    position: absolute; bottom: 16px; left: 16px;
    background: rgba(15, 23, 42, 0.95); border: 1px solid #334155;
    border-radius: 10px; padding: 12px 16px; z-index: 10; font-size: 12px; max-width: 220px;
  }}
  #legend h3 {{ margin-bottom: 8px; color: #e2e8f0; font-size: 13px; }}
  .leg-item {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; }}
  .leg-dot {{ width: 14px; height: 14px; border-radius: 50%; border: 2px solid; flex-shrink: 0; }}
  .leg-bridge {{ width: 22px; height: 0; border-top: 2px dashed #e879f9; flex-shrink: 0; }}
  .leg-dot-thought {{ background: #D4A055; border-color: #B8862E; }}
  .leg-dot-entity {{ background: #e2e8f0; border-color: #94a3b8; opacity: 0.72; }}
  .leg-dot-column {{ background: #9078C8; border-color: #7058A8; }}
  .leg-dot-value {{ background: #72B862; border-color: #529842; }}
  .leg-dot-row {{ background: #64748B; border-color: #94A3B8; }}
  #graph-wrap {{ position: relative; width: 100%; height: calc(100vh - 56px); cursor: grab; touch-action: none; }}
  #graph-wrap:active {{ cursor: grabbing; }}
  #graph {{ position: absolute; inset: 0; width: 100%; height: 100%; display: block; }}
  #load-hint {{
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    z-index: 6; pointer-events: none; background: rgba(11, 18, 32, 0.55);
  }}
  .load-box {{ display: flex; flex-direction: column; align-items: center; gap: 12px; }}
  .load-dial {{ position: relative; width: 92px; height: 92px; }}
  .load-ring {{ width: 92px; height: 92px; display: block; }}
  .load-track {{ fill: none; stroke: #334155; stroke-width: 6; }}
  .load-arc {{
    fill: none; stroke: #60a5fa; stroke-width: 6; stroke-linecap: round;
    transform-origin: 36px 36px; transform: rotate(-90deg);
    stroke-dasharray: 175.9; stroke-dashoffset: 132;
  }}
  .load-arc.spin {{ animation: load-spin 0.9s linear infinite; }}
  @keyframes load-spin {{ to {{ transform: rotate(270deg); }} }}
  #load-pct {{
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 16px; font-weight: 700; color: #f8fafc;
  }}
  #load-caption {{ font-size: 13px; color: #cbd5e1; text-align: center; max-width: 280px; }}
  #detail-panel {{
    position: absolute; top: 64px; right: 16px; width: 340px; max-height: calc(100vh - 100px);
    background: rgba(15, 23, 42, 0.98); border: 1px solid #334155; border-radius: 12px;
    padding: 18px; overflow-y: auto; z-index: 10; display: none;
  }}
  #detail-panel h2 {{ color: #e2e8f0; margin-bottom: 10px; font-size: 15px; }}
  #detail-panel .field {{ margin: 6px 0; }}
  #detail-panel .field-label {{ color: #94a3b8; font-size: 12px; }}
  #detail-panel .field-value {{ color: #e2e8f0; font-size: 13px; line-height: 1.5; }}
  #detail-panel a {{ color: #93c5fd; word-break: break-all; }}
  #detail-panel .close-btn {{ position: absolute; top: 10px; right: 14px; cursor: pointer; color: #94a3b8; font-size: 20px; }}
  #detail-panel .neighbors {{ margin-top: 12px; }}
  #detail-panel .neighbor-item {{
    padding: 8px 10px; margin: 5px 0; background: rgba(255,255,255,0.05);
    border-radius: 6px; cursor: pointer; font-size: 13px; color: #e2e8f0; word-break: break-word;
  }}
  #detail-panel .neighbor-item:hover {{ background: rgba(59,130,246,0.2); }}
  #detail-panel hr {{ border-color: #334155; margin: 10px 0; }}
  #cluster-tip {{
    position: absolute; display: none; z-index: 12; max-width: 460px;
    background: rgba(15, 23, 42, 0.97); border: 1px solid #334155; border-radius: 10px;
    padding: 10px 12px; pointer-events: none; box-shadow: 0 8px 20px rgba(0,0,0,.25);
    font-size: 12px; color: #e2e8f0; line-height: 1.45;
  }}
  #cluster-tip li {{ word-break: break-all; }}
  #cluster-tip .ct-title {{ font-weight: 700; margin-bottom: 6px; color: #f8fafc; }}
  #cluster-tip .ct-row {{ margin: 3px 0; }}
  #cluster-tip ul {{ margin: 4px 0 0 16px; }}
  #search-box {{ position: absolute; top: 64px; left: 16px; z-index: 10; display: flex; flex-direction: column; gap: 8px; }}
  #search-box input {{
    background: rgba(15, 23, 42, 0.95); border: 1px solid #334155; border-radius: 8px;
    padding: 8px 14px; color: #e2e8f0; font-size: 13px; width: 240px; outline: none;
  }}
  #graph-controls {{ display: flex; gap: 6px; }}
  #graph-hint {{
    position: absolute; right: 16px; bottom: 16px; z-index: 8; pointer-events: none;
    max-width: 340px; font-size: 11px; color: #94a3b8; line-height: 1.35; text-align: right;
  }}
  body.light-theme {{ background: #f8fafc; color: #1f2937; }}
  body.light-theme #header {{ background: #ffffff; border-bottom: 1px solid #e5e7eb; }}
  body.light-theme #header h1 {{ color: #1f2937; }}
  body.light-theme #graph-back {{ color: #2563eb; }}
  body.light-theme #stats {{ color: #6b7280; }}
  body.light-theme #stats span {{ color: #374151; }}
  body.light-theme #theme-toggle, body.light-theme #graph-controls button {{
    border: 1px solid #d1d5db; background: #ffffff; color: #374151;
  }}
  body.light-theme #graph-controls button.active {{ border-color: #3b82f6; background: #eff6ff; }}
  body.light-theme #legend {{ background: rgba(255,255,255,0.96); border: 1px solid #e5e7eb; }}
  body.light-theme #legend h3 {{ color: #374151; }}
  body.light-theme #detail-panel {{ background: #ffffff; border: 1px solid #e5e7eb; }}
  body.light-theme #detail-panel h2, body.light-theme #detail-panel .field-value {{ color: #1f2937; }}
  body.light-theme #detail-panel .field-label, body.light-theme #graph-hint {{ color: #6b7280; }}
  body.light-theme #load-hint {{ background: rgba(248, 250, 252, 0.72); }}
  body.light-theme .load-track {{ stroke: #e2e8f0; }}
  body.light-theme .load-arc {{ stroke: #2563eb; }}
  body.light-theme #load-pct {{ color: #0f172a; }}
  body.light-theme #load-caption {{ color: #64748b; }}
  body.light-theme #detail-panel a {{ color: #2563eb; }}
  body.light-theme #detail-panel .neighbor-item {{ background: #f3f4f6; color: #374151; }}
  body.light-theme #search-box input {{ background: #ffffff; border: 1px solid #d1d5db; color: #374151; }}
  body.light-theme #cluster-tip {{ background: rgba(255,255,255,0.98); border: 1px solid #d1d5db; color: #374151; }}
  body.light-theme #cluster-tip .ct-title {{ color: #111827; }}
</style>
</head>
<body class="light-theme">
<div id="header">
  <div id="header-left">
    {back_html}
    <h1>{heading}</h1>
  </div>
  <div id="header-right">
    <div id="stats">Карточки: <span id="stat-z">{total_z}</span> &nbsp;|&nbsp; Теги: <span id="stat-e">{total_e}</span> &nbsp;|&nbsp; Связи: <span id="stat-r">{total_r}</span></div>
    <button id="theme-toggle" type="button">Темная тема</button>
  </div>
</div>
<div id="search-box">
  <input type="text" id="searchInput" placeholder="🔍 Найти карточку или тег..."/>
  <div id="graph-controls">
    <button type="button" id="btn-tags">Теги</button>
  </div>
</div>
<div id="graph-wrap">
  <canvas id="graph"></canvas>
  <div id="graph-hint">На среднем масштабе видны узлы, связи между ними и названия крупных карточек. Приблизьте, чтобы подписать остальные. Поиск переносит к совпадениям.</div>
  <div id="load-hint">
    <div class="load-box">
      <div class="load-dial">
        <svg class="load-ring" viewBox="0 0 72 72" aria-hidden="true">
          <circle class="load-track" cx="36" cy="36" r="28"></circle>
          <circle id="load-arc" class="load-arc spin" cx="36" cy="36" r="28"></circle>
        </svg>
        <div id="load-pct">0%</div>
      </div>
      <div id="load-caption">Загрузка графа</div>
    </div>
  </div>
  <div id="cluster-tip"></div>
</div>
<div id="legend">
  <h3>Легенда</h3>
  <div class="leg-item"><div class="leg-dot leg-dot-thought"></div> Карточка</div>
  <div class="leg-item"><div class="leg-dot leg-dot-column"></div> Столбец таблицы</div>
  <div class="leg-item"><div class="leg-dot leg-dot-value"></div> Значение</div>
  <div class="leg-item"><div class="leg-dot leg-dot-row"></div> Строка таблицы</div>
  <div class="leg-item"><div class="leg-dot leg-dot-entity"></div> Тег</div>
  <div class="leg-item"><div class="leg-bridge"></div> То же на другом листе</div>
</div>
<div id="detail-panel">
  <span class="close-btn" onclick="closeDetail()">&times;</span>
  <div id="detail-content"></div>
</div>
"""
    html += _GRAPH_JS.replace("__DATA_URL__", data_url_js).replace("__DATA_B64__", data_b64_js)
    return html


def render_graph_html(
    graph_data: Optional[Dict[str, Any]] = None,
    user_label: str = "",
    data_url: Optional[str] = None,
    back_url: str = "",
    back_label: str = "← В проект",
) -> str:
    """Собирает HTML-граф. data_url — отдельная подгрузка payload без вставки в страницу."""
    return _build_html(
        graph_data,
        user_label=user_label,
        data_url=data_url,
        back_url=back_url,
        back_label=back_label,
    )


def generate_graph_html(user_id: str, output_path: str | None = None) -> str:
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
    graph_data = repo.export_graph_data(user_id)
    html_content = render_graph_html(graph_data, user_label=user_id)
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="graph_")
        os.close(fd)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return output_path