"""
Интерактивный граф знаний v3: Canvas 2D на CPU, рассчитан на 500k+ узлов.

Что изменилось относительно v2:
  * Стабильная версия графа (ver). Клиент всегда запрашивает карточку/поиск/подписи
    вместе с версией, сервер отвечает 409, если граф успел измениться.
    Раньше индекс узла из старого снимка указывал на другую карточку или в пустоту.
  * GraphSnapshot: единый снимок для /graph, /node/<idx>, /search, /titles.
  * Новая раскладка: «планетарная упаковка» (проект -> ветка -> карточка), без наложений
    и без экспоненциального роста цепочек. Проекты и крупные ветки приходят как
    области (regions): подписи, подсветка, навигация.
  * Клиент: CSR-индекс, LOD-режимы, ленивые подписи для видимых узлов,
    серверный поиск с выпадающим списком, поиск не прячет остальной граф.
  * Формат ZGB2: добавлены области (regions), выравнивание, версия в заголовке ответа.
"""
from __future__ import annotations

import array
import base64
import gzip
import hashlib
import json
import math
import os
import re
import struct
import tempfile
import threading
import time
from collections import defaultdict
from html import escape as html_escape
from typing import Any, Dict, Optional

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

_EXCEL_TYPES = frozenset({"excel_sheet", "excel_column", "excel_value", "excel_row"})
_FULL_META_LIMIT = 25_000


# --------------------------------------------------------------------------- #
# Общие помощники
# --------------------------------------------------------------------------- #

def _luhmann_sort_key(luhmann_id: str) -> tuple:
    m = re.match(r"^(\d+)", luhmann_id or "")
    return (int(m.group(1)), luhmann_id) if m else (9999, luhmann_id or "")


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


def _norm_project(value: Any) -> str:
    return " ".join((value or "").split()) if isinstance(value, str) else ""


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _root_lookup(parent_by_luhmann: dict):
    """Корень ветки за один проход по цепочке (только по известным родителям)."""
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
            parent = parent_by_luhmann.get(current)
            current = parent if parent in parent_by_luhmann else None
        if current and current in memo:
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
    """Глубина узла. Родитель вне набора не считается: такой узел — корень (глубина 0)."""
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
            parent = parent_by_luhmann.get(current)
            current = parent if parent in parent_by_luhmann else None
        base = depth[current] + 1 if (current and current in depth) else 0
        for node in reversed(chain):
            depth[node] = base
            base += 1
    return depth


def _palette_index(name: str) -> int:
    total = 0
    for ch in name:
        total = (total * 33 + ord(ch)) & 0xFFFFFFFF
    return total % len(_BRANCH_PALETTE)


def _short_text(content: str, max_len: int) -> str:
    text = " ".join((content or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _node_label(text: str) -> str:
    """Подпись узла: 2 слова, или 3 если среди первых двух есть короткое (≤3 символа)."""
    words = (text or "").split()
    if not words:
        return ""
    take = 3 if any(len(w) <= 3 for w in words[:2]) else 2
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


def _node_title(item: dict) -> str:
    topic = (item.get("topic") or "").strip()
    if topic:
        return _short_text(topic, 140)
    content = " ".join((item.get("content") or "").split())
    if content:
        return _short_text(content, 90)
    return str(item.get("luhmann_id") or "")


def _thought_size(depth: int) -> int:
    if depth <= 0:
        return 24
    if depth == 1:
        return 17
    if depth == 2:
        return 13
    return 8


def _excel_node_style(
    thought_type: str,
    luhmann_id: str,
    parent_by_luhmann: dict,
    color_index: dict,
    root: str,
    depth: int,
) -> tuple[int, int, int]:
    """(group, radius, color). group: 0 карточка, 1 тег, 2 строка таблицы."""
    if thought_type == "excel_row":
        return 2, 8, 7
    if thought_type == "excel_sheet":
        return 0, 22, color_index.get(root, 0)
    if thought_type in {"excel_column", "excel_value"}:
        source = luhmann_id if thought_type == "excel_column" else (parent_by_luhmann.get(luhmann_id) or luhmann_id)
        parts = [p for p in (source or "").split(".") if p.isdigit()]
        color = int(parts[-1]) % len(_BRANCH_PALETTE) if parts else 1
        return (0, 14, color) if thought_type == "excel_column" else (0, 10, color)
    return 0, _thought_size(depth), color_index.get(root, 0)


# --------------------------------------------------------------------------- #
# Раскладка: планетарная упаковка
#
# Каждый блок (поддерево) — круг. Самый крупный участник блока стоит в центре,
# остальные (родитель и дочерние поддеревья) — кольцами вокруг без наложений.
# Радиус блока растёт ЛИНЕЙНО вдоль цепочек и как корень из площади для «веера».
# Проекты — блоки верхнего уровня, крупные в центре, мелкие вокруг.
# --------------------------------------------------------------------------- #

_LEAF = 11.0
_GAP = 5.0


def _pack_members(radii: list[float], phase: float = 0.0, gap: float = _GAP) -> tuple[list[tuple[float, float]], float]:
    """radii отсортированы по убыванию. Первый в центре. -> (смещения, радиус блока)."""
    n = len(radii)
    if n == 0:
        return [], 0.0
    offsets: list[tuple[float, float]] = [(0.0, 0.0)]
    inner = radii[0]
    two_pi = 2.0 * math.pi
    i = 1
    while i < n:
        r0 = radii[i]
        dist = inner + gap + r0
        widths: list[float] = []
        total = 0.0
        j = i
        while j < n:
            w = 2.0 * math.asin(min(1.0, (radii[j] + gap * 0.5) / dist))
            if j > i and total + w > two_pi:
                break
            widths.append(w)
            total += w
            j += 1
        count = j - i
        slack = max(0.0, (two_pi - total) / count)
        cursor = phase
        for k in range(count):
            span = widths[k] + slack
            ang = cursor + span * 0.5
            offsets.append((dist * math.cos(ang), dist * math.sin(ang)))
            cursor += span
        inner = dist + r0
        phase += 0.9
        i = j
    return offsets, inner


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
            (one_r * math.cos(-math.pi / 2 + 2 * math.pi * k / count),
             one_r * math.sin(-math.pi / 2 + 2 * math.pi * k / count))
            for k in range(count)
        ]
        return one_r + 14.0, offsets
    offsets = []
    placed = 0
    ring = 0
    outer = 48.0
    while placed < count and ring < 400:
        vr = 86.0 + ring * spacing
        cap = max(6, int(2 * math.pi * vr / spacing))
        take = min(cap, count - placed)
        phase = (math.pi / take) if ring % 2 else 0.0
        for k in range(take):
            ang = phase - math.pi / 2 + 2 * math.pi * k / take
            offsets.append((vr * math.cos(ang), vr * math.sin(ang)))
        outer = vr + 14.0
        placed += take
        ring += 1
    return outer, offsets


def _column_orbits(outers: list[float]) -> list[float]:
    """Радиус орбиты столбца: соседние круги не пересекаются."""
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


def _excel_block(sheet: int, kids_of: list, type_of: list) -> tuple[float, list[tuple[int, float, float]]]:
    """Лист в центре, столбцы по орбите, значения — кольцом вокруг столбца."""
    prepared = []
    outers: list[float] = []
    for col in kids_of[sheet]:
        if type_of[col] != "excel_column":
            continue
        vals = [v for v in kids_of[col] if type_of[v] == "excel_value"]
        outer, offs = _excel_value_circle(len(vals))
        prepared.append((col, vals, offs))
        outers.append(outer)
    orbits = _column_orbits(outers)
    inner: list[tuple[int, float, float]] = [(sheet, 0.0, 0.0)]
    bound = 80.0
    n_cols = max(len(prepared), 1)
    for k, (col, vals, offs) in enumerate(prepared):
        ang = -math.pi / 2 + 2 * math.pi * k / n_cols
        cx = orbits[k] * math.cos(ang)
        cy = orbits[k] * math.sin(ang)
        inner.append((col, cx, cy))
        for val, (dx, dy) in zip(vals, offs):
            inner.append((val, cx + dx, cy + dy))
        bound = max(bound, orbits[k] + outers[k])
    for row in kids_of[sheet]:
        if type_of[row] == "excel_row":
            inner.append((row, -80.0, 0.0))
    return bound + 18.0, inner


def _own_radius(kid_count: int) -> float:
    return _LEAF if not kid_count else _LEAF + min(9.0, 2.0 * math.log2(1 + kid_count))


def _layout_graph(zs: list[dict]) -> tuple[list[tuple[float, float]], list[dict]]:
    """Позиции всех карточек (по индексу в zs) и области (проекты и крупные ветки)."""
    n = len(zs)
    if not n:
        return [], []
    idx_of: dict[str, int] = {}
    for i, z in enumerate(zs):
        idx_of[z["luhmann_id"]] = i
    type_of = [z.get("thought_type") or "" for z in zs]
    proj_of = [_norm_project(z.get("project")) for z in zs]
    parent = [-1] * n
    kids_of: list[list[int]] = [[] for _ in range(n)]
    for i, z in enumerate(zs):
        p = z.get("parent_luhmann")
        j = idx_of.get(p) if p else None
        if j is not None and j != i:
            parent[i] = j
            kids_of[j].append(i)

    own = [_LEAF] * n
    inner_of: dict[int, list[tuple[int, float, float]]] = {}
    absorbed = bytearray(n)
    for i in range(n):
        if type_of[i] == "excel_sheet":
            bound, inner = _excel_block(i, kids_of, type_of)
            own[i] = bound
            inner_of[i] = inner
            for node, _dx, _dy in inner[1:]:
                absorbed[node] = 1

    visited = bytearray(absorbed)
    tkids: list[list[int]] = [[] for _ in range(n)]
    order: list[int] = []

    def walk(root: int) -> None:
        visited[root] = 1
        stack = [root]
        while stack:
            i = stack.pop()
            order.append(i)
            for c in kids_of[i]:
                if not visited[c]:
                    visited[c] = 1
                    tkids[i].append(c)
                    stack.append(c)

    roots = [i for i in range(n) if parent[i] < 0 and not absorbed[i]]
    for r in roots:
        walk(r)
    for i in range(n):
        if not visited[i]:
            roots.append(i)
            walk(i)

    radius = [0.0] * n
    count = [1] * n
    memb: list = [None] * n
    moff: list = [None] * n
    for i in reversed(order):
        kids = tkids[i]
        if i in inner_of:
            count[i] += len(inner_of[i]) - 1
        if not kids:
            radius[i] = own[i]
            continue
        if i not in inner_of:
            own[i] = _own_radius(len(kids))
        items = [(own[i], 0, -1)] + [(radius[k], 1, k) for k in kids]
        items.sort(key=lambda t: (-t[0], t[1], t[2]))
        offs, total = _pack_members([t[0] for t in items], ((i * 0.6180339887) % 1.0) * 6.2831853)
        radius[i] = total
        memb[i] = [t[2] for t in items]
        moff[i] = offs
        c = count[i]
        for k in kids:
            c += count[k]
        count[i] = c

    by_proj: dict[str, list[int]] = defaultdict(list)
    for r in roots:
        by_proj[proj_of[r]].append(r)
    proj_roots: dict[str, list[int]] = {}
    proj_offs: dict[str, list[tuple[float, float]]] = {}
    proj_radius: dict[str, float] = {}
    proj_count: dict[str, int] = {}
    for name, rs in by_proj.items():
        rs.sort(key=lambda r: (-radius[r], r))
        offs, total = _pack_members([radius[r] for r in rs], 0.4)
        proj_roots[name] = rs
        proj_offs[name] = offs
        proj_radius[name] = total
        proj_count[name] = sum(count[r] for r in rs)
    names = sorted(by_proj, key=lambda p: (-proj_radius[p], p))
    g_offs, _ = _pack_members([proj_radius[p] for p in names], 0.3, gap=60.0)

    pos: list[tuple[float, float]] = [(0.0, 0.0)] * n
    regions: list[dict] = []
    stack: list[tuple[int, float, float]] = []
    multi = len(names) > 1
    branch_regions: list[dict] = []
    for name, (gx, gy) in zip(names, g_offs):
        if multi:
            regions.append({
                "name": name or "Без проекта", "x": gx, "y": gy, "r": proj_radius[name] + 12.0,
                "n": proj_count[name], "lv": 0, "proj": name, "root": "",
            })
        for r, (dx, dy) in zip(proj_roots[name], proj_offs[name]):
            cx, cy = gx + dx, gy + dy
            stack.append((r, cx, cy))
            if count[r] >= 60:
                branch_regions.append({
                    "name": _node_title(zs[r])[:40] or zs[r]["luhmann_id"], "x": cx, "y": cy,
                    "r": radius[r] + 8.0, "n": count[r], "lv": 1, "proj": name, "root": zs[r]["luhmann_id"],
                })
    branch_regions.sort(key=lambda g: -g["n"])
    regions.extend(branch_regions[:400])

    while stack:
        i, cx, cy = stack.pop()
        members = memb[i]
        if members is None:
            pos[i] = (cx, cy)
        else:
            for member, (dx, dy) in zip(members, moff[i]):
                if member < 0:
                    pos[i] = (cx + dx, cy + dy)
                else:
                    stack.append((member, cx + dx, cy + dy))
        if i in inner_of:
            px, py = pos[i]
            for node, dx, dy in inner_of[i][1:]:
                pos[node] = (px + dx, py + dy)
    return pos, regions


def _tag_anchor(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Тег стоит у самой плотной группы своих карточек."""
    if len(points) == 1:
        return points[0]
    cell = 160.0
    bins: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for x, y in points:
        bins[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append((x, y))
    best = max(bins.values(), key=len)
    return (sum(p[0] for p in best) / len(best), sum(p[1] for p in best) / len(best))


def _place_entities(entities: list, raw_edges: list, zid_pos: dict, excel_zids: set) -> dict[str, tuple[float, float]]:
    pos: dict[str, tuple[float, float]] = {}
    if not entities:
        return pos
    wanted = {f"entity:{e['name']}" for e in entities}
    linked: dict[str, list[tuple[float, float]]] = defaultdict(list)
    has_card: set[str] = set()
    excel_linked: set[str] = set()
    for edge in raw_edges:
        to_id = edge.get("to_id")
        if to_id not in wanted:
            continue
        frm = edge.get("from_id")
        if frm not in zid_pos:
            continue
        if frm in excel_zids:
            excel_linked.add(to_id)
            continue
        has_card.add(to_id)
        bucket = linked[to_id]
        if len(bucket) < 48:
            bucket.append(zid_pos[frm])
    xs = [p[0] for p in zid_pos.values()] or [0.0]
    ys = [p[1] for p in zid_pos.values()] or [0.0]
    loose_x = max(xs) + 260.0
    loose_y = (min(ys) + max(ys)) / 2.0
    loose = 0
    for i, entity in enumerate(entities):
        eid = f"entity:{entity['name']}"
        if eid in has_card:
            ax, ay = _tag_anchor(linked[eid])
            ang = i * 2.399
            pos[eid] = (ax + 42.0 * math.cos(ang), ay + 42.0 * math.sin(ang))
        elif eid in excel_linked:
            continue
        else:
            ang = loose * 2.399
            ring = 40.0 + 14.0 * math.sqrt(loose)
            pos[eid] = (loose_x + ring * math.cos(ang), loose_y + ring * math.sin(ang))
            loose += 1
    return pos


# --------------------------------------------------------------------------- #
# Упаковка данных
# --------------------------------------------------------------------------- #

def graph_version(graph_data: Dict[str, Any]) -> str:
    """Короткий отпечаток снимка. Меняется при добавлении/удалении карточек и связей."""
    zs = graph_data.get("zettels") or []
    h = hashlib.blake2s(digest_size=6)
    h.update(f"{len(zs)}:{len(graph_data.get('entities') or [])}:{len(graph_data.get('edges') or [])}".encode())
    step = max(1, len(zs) // 97)
    for z in zs[::step]:
        h.update(str(z.get("zettel_id")).encode())
        h.update(str(z.get("topic") or "")[:40].encode())
    return h.hexdigest()


def _unique_zettels(items: list) -> list:
    seen: set = set()
    out = []
    for z in items:
        zid = z["zettel_id"]
        if zid in seen:
            continue
        seen.add(zid)
        out.append(z)
    return out


def _bridge_key(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def _append_excel_bridges(zs: list, ea: array.array, eb: array.array, ek: array.array) -> None:
    """Одинаковые столбцы и значения разных листов связаны постоянно. Строки не входят."""
    topic_by_lid = {z["luhmann_id"]: (z.get("topic") or "") for z in zs}
    columns: dict[str, list[int]] = defaultdict(list)
    values: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, z in enumerate(zs):
        thought = z.get("thought_type") or ""
        if thought not in ("excel_column", "excel_value"):
            continue
        name = _bridge_key(z.get("topic") or "")
        if not name:
            continue
        if thought == "excel_column":
            columns[name].append(idx)
        else:
            column_name = _bridge_key(topic_by_lid.get(z.get("parent_luhmann") or "", ""))
            if column_name:
                values[(column_name, name)].append(idx)

    def link(members: list[int], limit: int) -> int:
        if len(members) < 2 or len(members) > limit:
            return 0
        hub = members[0]
        for other in members[1:]:
            ea.append(hub)
            eb.append(other)
            ek.append(3)
        return len(members) - 1

    for members in columns.values():
        link(members, 8)
    budget = 70
    for members in values.values():
        if budget <= 0:
            break
        budget -= link(members, 4)


def _append_project_bridges(zs: list, entities: list, raw_edges: list, id_index: dict,
                            ea: array.array, eb: array.array, ek: array.array) -> None:
    """Проекты с общим тегом соединяются парой карточек (пунктир между островами)."""
    project_of: dict[str, str] = {}
    for z in zs:
        name = _norm_project(z.get("project"))
        if name:
            project_of[z["zettel_id"]] = name
    if len(set(project_of.values())) < 2:
        return
    chosen: dict[str, dict[str, str]] = defaultdict(dict)
    for z in zs:
        project = project_of.get(z["zettel_id"])
        if not project:
            continue
        for tag in z.get("tags") or []:
            key = " ".join(str(tag).lower().split())
            if len(key) >= 2:
                chosen[key].setdefault(project, z["zettel_id"])
    entity_key: dict[str, str] = {}
    for entity in entities:
        raw = str(entity.get("name") or "")
        label = " ".join((entity.get("display_name") or "").lower().split())
        if "::" in raw:
            raw = raw.split("::", 1)[1]
        key = label or " ".join(raw.lower().split())
        if len(key) >= 2:
            entity_key[f"entity:{entity['name']}"] = key
    for edge in raw_edges:
        if edge.get("rel_type") != "MENTIONS":
            continue
        project = project_of.get(edge.get("from_id"))
        key = entity_key.get(str(edge.get("to_id") or ""))
        if project and key:
            chosen[key].setdefault(project, edge["from_id"])
    seen_nodes: set = set()
    pair_count: dict = defaultdict(int)
    added = 0
    for by_project in chosen.values():
        if len(by_project) < 2 or added >= 480:
            continue
        items = list(by_project.items())
        hub_name, hub_zid = items[0]
        hub = id_index.get(hub_zid)
        if hub is None:
            continue
        for project, zid in items[1:]:
            other = id_index.get(zid)
            if other is None:
                continue
            ends = (hub, other) if hub < other else (other, hub)
            if ends in seen_nodes:
                continue
            names = (hub_name, project) if hub_name < project else (project, hub_name)
            if pair_count[names] >= 4:
                continue
            seen_nodes.add(ends)
            pair_count[names] += 1
            ea.append(ends[0])
            eb.append(ends[1])
            ek.append(4)
            added += 1
            if added >= 480:
                break


def _edge_limit(n: int) -> int:
    if n < 40_000:
        return 2_000_000
    if n < 150_000:
        return 500_000
    return max(900_000, n + 250_000)


def _thin_edges(ea, eb, ek, limit: int):
    """Структура (дерево, мосты, связи проектов) сохраняется целиком, связи значение↔строка и теги — по бюджету."""
    total = len(ea)
    if total <= limit:
        return ea, eb, ek
    keep = bytearray(total)
    used = 0
    for i in range(total):
        if ek[i] in (0, 3, 4):
            keep[i] = 1
            used += 1
    room = max(0, limit - used)
    related_room = room // 2
    for i in range(total):
        if related_room <= 0:
            break
        if not keep[i] and ek[i] == 2:
            keep[i] = 1
            related_room -= 1
            used += 1
    room = max(0, limit - used)
    for i in range(total):
        if room <= 0:
            break
        if not keep[i] and ek[i] == 1:
            keep[i] = 1
            room -= 1
    na, nb, nk = array.array("I"), array.array("I"), array.array("B")
    for i in range(total):
        if keep[i]:
            na.append(ea[i])
            nb.append(eb[i])
            nk.append(ek[i])
    return na, nb, nk


def _pack_graph(graph_data: Dict[str, Any], derive_tree_edges: bool = False) -> dict:
    all_z = graph_data.get("zettels") or []
    entities = graph_data.get("entities") or []
    raw_edges = graph_data.get("edges") or []
    zs = _unique_zettels(all_z)
    card_count = len(zs)

    parent_by = {z["luhmann_id"]: z.get("parent_luhmann") for z in zs}
    depth_by = _node_depths(parent_by, [z["luhmann_id"] for z in zs])
    root_of = _root_lookup(parent_by)
    roots_sorted = sorted({root_of(z["luhmann_id"]) for z in zs}, key=_luhmann_sort_key)
    color_index = {root: i % len(_BRANCH_PALETTE) for i, root in enumerate(roots_sorted)}

    pos, regions = _layout_graph(zs)
    zid_pos = {z["zettel_id"]: pos[i] for i, z in enumerate(zs)}
    excel_zids = {z["zettel_id"] for z in zs if (z.get("thought_type") or "") in _EXCEL_TYPES}
    ent_pos = _place_entities(entities, raw_edges, zid_pos, excel_zids)

    full_meta = card_count < _FULL_META_LIMIT
    label_mode = 0 if card_count <= 60_000 else (1 if card_count <= 200_000 else 2)
    xs, ys = array.array("f"), array.array("f")
    rs, gs, cs = array.array("B"), array.array("B"), array.array("B")
    proj_slot = array.array("H")
    proj_names: list[str] = [""]
    slot_of: dict[str, int] = {"": 0}
    labels: list[str] = []
    keys: list[str] = []
    meta: list = []
    types: list[str] = []

    if full_meta:
        lim_topic, lim_content, lim_quote, lim_preview = 180, 480, 360, 120
    for i, z in enumerate(zs):
        lid = z["luhmann_id"]
        tt = z.get("thought_type") or ""
        topic = z.get("topic") or ""
        content = z.get("content") or ""
        depth = depth_by.get(lid, 0)
        root = root_of(lid)
        project = _norm_project(z.get("project"))
        group, radius, color = _excel_node_style(tt, lid, parent_by, color_index, root, depth)
        if project:
            color = _palette_index(project)
        if tt in _EXCEL_TYPES:
            label = _excel_label(tt, topic, content)
        else:
            label = _node_label(topic.strip() or content)
        if label_mode and tt not in ("excel_sheet", "excel_column"):
            if depth > (3 if label_mode == 1 else 1):
                label = ""
        px, py = pos[i]
        xs.append(px)
        ys.append(py)
        rs.append(min(255, max(5, int(round(radius * 0.7)))))
        gs.append(group)
        cs.append(color % len(_BRANCH_PALETTE))
        labels.append(label)
        keys.append(z["zettel_id"])
        types.append(tt)
        slot = slot_of.get(project)
        if slot is None:
            slot = min(len(proj_names), 65534)
            slot_of[project] = slot
            if slot == len(proj_names):
                proj_names.append(project[:80])
        proj_slot.append(slot)
        if full_meta:
            preview = f"{topic}: {_short_text(content, lim_preview)}" if topic else _short_text(content, lim_preview)
            meta.append([
                lid, z.get("parent_luhmann") or "", _short_text(topic, lim_topic), tt,
                ", ".join(list(z.get("tags") or [])[:8]), _short_text(content, lim_content), preview, "",
                _public_source_url(z.get("source_input") or "text"),
                _short_text(z.get("source_quote") or "", lim_quote), project,
            ])

    id_index = {k: i for i, k in enumerate(keys)}
    for e in entities:
        eid = f"entity:{e['name']}"
        if eid in id_index or eid not in ent_pos:
            continue
        px, py = ent_pos[eid]
        name = e.get("display_name") or e.get("name") or ""
        id_index[eid] = len(keys)
        keys.append(eid)
        types.append("tag")
        xs.append(px)
        ys.append(py)
        rs.append(5)
        gs.append(1)
        cs.append(0)
        labels.append(_node_label(name))
        proj_slot.append(0)
        if full_meta:
            meta.append(["", "", name, e.get("entity_type") or "tag", "",
                         f"Упоминаний: {e.get('mention_count') or 0}", name,
                         f"{name} {e.get('entity_type', '')} {e.get('name', '')}".lower(), ""])

    kind = {"CHILD_OF": 0, "MENTIONS": 1, "RELATED_TO": 2}
    ea, eb, ek = array.array("I"), array.array("I"), array.array("B")
    seen = set() if len(raw_edges) <= 400_000 else None
    for edge in raw_edges:
        a = id_index.get(edge.get("from_id"))
        b = id_index.get(edge.get("to_id"))
        if a is None or b is None:
            continue
        k = kind.get(edge.get("rel_type"), 0)
        if k == 0 and ((a < card_count and types[a] == "excel_row") or (b < card_count and types[b] == "excel_row")):
            continue
        if seen is not None:
            key = (a, b, k)
            if key in seen:
                continue
            seen.add(key)
        ea.append(a)
        eb.append(b)
        ek.append(k)
    if derive_tree_edges:
        lid_idx = {z["luhmann_id"]: i for i, z in enumerate(zs)}
        for i, z in enumerate(zs):
            j = lid_idx.get(z.get("parent_luhmann") or "")
            if j is not None and j != i and types[i] != "excel_row":
                ea.append(i)
                eb.append(j)
                ek.append(0)
    _append_excel_bridges(zs, ea, eb, ek)
    _append_project_bridges(zs, entities, raw_edges, id_index, ea, eb, ek)
    total_edges = len(ea)
    ea, eb, ek = _thin_edges(ea, eb, ek, _edge_limit(len(keys)))

    region_rows = []
    for g in regions:
        proj = g["proj"]
        color = _palette_index(proj) if proj else color_index.get(g["root"], 0)
        region_rows.append((g["name"], g["x"], g["y"], g["r"], int(g["n"]), color, int(g["lv"])))

    has_project = any(name for name in proj_names)
    return {
        "n": len(keys), "x": xs, "y": ys, "r": rs, "g": gs, "c": cs, "l": labels, "m": meta,
        "keys": keys, "ea": ea, "eb": eb, "ek": ek,
        "pi": proj_slot if has_project else None, "pn": proj_names if has_project else [],
        "rg": region_rows, "tz": len(all_z), "te": len(entities), "tr": total_edges,
        "gp": 1 if has_project else 0,
    }


def pack_graph_payload(graph_data: Dict[str, Any], derive_tree_edges: bool = False) -> dict:
    """Компактный payload: параллельные массивы. keys (zettel_id по индексу) остаётся на сервере."""
    payload = _pack_graph(graph_data, derive_tree_edges)
    payload["ver"] = graph_version(graph_data)
    return payload


def dumps_payload(payload: dict) -> bytes:
    """JSON-вариант для небольших графов (клиент понимает и его)."""
    ea, eb, ek = payload["ea"], payload["eb"], payload["ek"]
    flat = [0] * (3 * len(ea))
    flat[0::3] = ea.tolist()
    flat[1::3] = eb.tolist()
    flat[2::3] = ek.tolist()
    out = {
        "n": payload["n"],
        "x": [round(v, 1) for v in payload["x"]],
        "y": [round(v, 1) for v in payload["y"]],
        "r": list(payload["r"]), "g": list(payload["g"]), "c": list(payload["c"]),
        "l": payload["l"], "m": payload["m"], "e": flat,
        "pi": list(payload["pi"]) if payload.get("pi") is not None else None,
        "pn": payload.get("pn") or [],
        "rg": [list(r) for r in payload.get("rg") or []],
        "tz": payload["tz"], "te": payload["te"], "tr": payload["tr"],
        "gp": payload["gp"], "ver": payload.get("ver", ""),
    }
    return json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def dumps_binary(payload: dict) -> bytes:
    """Формат ZGB2: числа массивами, без JSON. Выравнивание для typed arrays на клиенте."""
    n = int(payload["n"])
    if payload.get("ea") is not None:
        ea = array.array("I", payload["ea"])
        eb = array.array("I", payload["eb"])
        ek = array.array("B", payload["ek"])
    else:
        flat = payload["e"]
        ea, eb, ek = array.array("I", flat[0::3]), array.array("I", flat[1::3]), array.array("B", flat[2::3])
    labels = [(i, str(t).encode("utf-8")[:120]) for i, t in enumerate(payload.get("l") or []) if t]
    pi = payload.get("pi")
    pn = [str(x).encode("utf-8")[:80] for x in (payload.get("pn") or [])]
    flags = 1 if (pi is not None and pn) else 0
    regions = payload.get("rg") or []
    parts: list[bytes] = []
    size = 0

    def put(chunk: bytes) -> None:
        nonlocal size
        parts.append(chunk)
        size += len(chunk)

    def align(k: int) -> None:
        pad = (-size) % k
        if pad:
            put(b"\0" * pad)

    put(struct.pack("<4s9I", b"ZGB2", n, len(ea), len(labels), len(pn), flags,
                    int(payload.get("tz") or 0), int(payload.get("te") or 0), int(payload.get("tr") or 0),
                    len(regions)))
    put(array.array("f", payload["x"]).tobytes())
    put(array.array("f", payload["y"]).tobytes())
    put(array.array("B", (min(255, int(v)) for v in payload["r"])).tobytes())
    put(array.array("B", (min(255, int(v)) for v in payload["g"])).tobytes())
    put(array.array("B", (min(255, int(v)) for v in payload["c"])).tobytes())
    align(4)
    put(ea.tobytes())
    put(eb.tobytes())
    put(ek.tobytes())
    if flags:
        align(2)
        put(array.array("H", pi).tobytes())
        for name in pn:
            put(struct.pack("<H", len(name)))
            put(name)
    for index, text in labels:
        put(struct.pack("<IH", index, len(text)))
        put(text)
    for name, x, y, r, count, color, level in regions:
        raw = str(name).encode("utf-8")[:96]
        put(struct.pack("<fffIBBH", float(x), float(y), float(r), int(count), int(color) & 255, int(level) & 255, len(raw)))
        put(raw)
    return b"".join(parts)


def build_overview_binary(zettels: list) -> bytes:
    """Лёгкий ответ для очень больших графов: координаты, цвета, структура дерева, без тегов."""
    payload = pack_graph_payload({"zettels": zettels, "entities": [], "edges": []}, derive_tree_edges=True)
    return dumps_binary(payload)


def encode_graph_html(html: str, accept_encoding: str = "") -> tuple[bytes, dict]:
    """Сжимает большой HTML, если браузер принимает gzip."""
    raw = html.encode("utf-8")
    headers = {"content-type": "text/html; charset=utf-8"}
    if "gzip" in (accept_encoding or "").lower() and len(raw) > 20_000:
        return gzip.compress(raw, compresslevel=4), {**headers, "content-encoding": "gzip", "vary": "Accept-Encoding"}
    return raw, headers


def _encode(raw: bytes, content_type: str, version: str, accept_encoding: str) -> tuple[bytes, dict]:
    headers = {
        "content-type": content_type,
        "cache-control": "no-store",
        "x-graph-size": str(len(raw)),
        "x-graph-version": version,
        "access-control-expose-headers": "x-graph-size, x-graph-version",
    }
    if "gzip" in (accept_encoding or "").lower() and len(raw) > 8_000:
        level = 1 if len(raw) > 1_500_000 else 4
        return gzip.compress(raw, compresslevel=level), {**headers, "content-encoding": "gzip", "vary": "Accept-Encoding"}
    return raw, headers


def encode_graph_payload(payload: dict, accept_encoding: str = "") -> tuple[bytes, dict]:
    return _encode(dumps_payload(payload), "application/json; charset=utf-8", payload.get("ver", ""), accept_encoding)


def encode_graph_binary(payload: dict, accept_encoding: str = "") -> tuple[bytes, dict]:
    return _encode(dumps_binary(payload), "application/octet-stream", payload.get("ver", ""), accept_encoding)


# --------------------------------------------------------------------------- #
# Серверный снимок графа: детали карточки, поиск, подписи по версии
# --------------------------------------------------------------------------- #

class GraphSnapshot:
    """
    Один снимок = один набор индексов. Держите его в кэше (по user_id/проекту) и отдавайте:
        GET  /graph            -> snapshot.binary_response(accept_encoding)
        GET  /graph/node/{i}   -> snapshot.node(i)        (v=<ver> в query)
        GET  /graph/search?q=  -> snapshot.search(q)
        GET  /graph/titles?ids=1,2,3 -> snapshot.titles(ids)
    Если query v не совпадает с snapshot.version — отвечайте 409.
    """

    def __init__(self, graph_data: Dict[str, Any], derive_tree_edges: bool = False):
        self.graph_data = graph_data
        self.payload = pack_graph_payload(graph_data, derive_tree_edges)
        self.version: str = self.payload["ver"]
        self.keys: list[str] = self.payload["keys"]
        self.n: int = self.payload["n"]
        z_by = {z["zettel_id"]: z for z in graph_data.get("zettels") or []}
        e_by = {f"entity:{e['name']}": e for e in graph_data.get("entities") or []}
        self.items: list[dict] = [z_by.get(k) or e_by.get(k) or {} for k in self.keys]
        self._binary: Optional[bytes] = None
        self._adj = None
        self._hay: Optional[list[str]] = None
        self._lock = threading.Lock()

    # --- общее ---
    def check(self, version: Optional[str]) -> bool:
        return not version or version == self.version

    def binary(self) -> bytes:
        if self._binary is None:
            self._binary = dumps_binary(self.payload)
        return self._binary

    def binary_response(self, accept_encoding: str = "") -> tuple[bytes, dict]:
        return _encode(self.binary(), "application/octet-stream", self.version, accept_encoding)

    def _is_entity(self, idx: int) -> bool:
        return self.keys[idx].startswith("entity:")

    def title(self, idx: int) -> str:
        item = self.items[idx]
        if self._is_entity(idx):
            return str(item.get("display_name") or item.get("name") or "")
        return _node_title(item)

    def titles(self, ids: list[int]) -> list[str]:
        out = []
        for i in ids[:600]:
            if 0 <= i < self.n:
                item = self.items[i]
                if self._is_entity(i):
                    out.append(_node_label(self.title(i)))
                else:
                    out.append(_node_label((item.get("topic") or "").strip() or item.get("content") or ""))
            else:
                out.append("")
        return out

    # --- смежность (лениво, CSR) ---
    def _ensure_adj(self):
        if self._adj is not None:
            return self._adj
        with self._lock:
            if self._adj is not None:
                return self._adj
            index_of = {k: i for i, k in enumerate(self.keys)}
            kind = {"CHILD_OF": 0, "MENTIONS": 1, "RELATED_TO": 2}
            pairs = []
            counts = array.array("I", [0]) * (self.n + 1)
            for edge in self.graph_data.get("edges") or []:
                a = index_of.get(edge.get("from_id"))
                b = index_of.get(edge.get("to_id"))
                if a is None or b is None:
                    continue
                k = kind.get(edge.get("rel_type"), 0)
                pairs.append((a, b, k))
                counts[a + 1] += 1
                counts[b + 1] += 1
            for i in range(self.n):
                counts[i + 1] += counts[i]
            total = counts[self.n]
            to = array.array("I", [0]) * total
            kd = array.array("B", [0]) * total
            out = array.array("B", [0]) * total
            cur = array.array("I", counts)
            for a, b, k in pairs:
                p = cur[a]
                to[p], kd[p], out[p] = b, k, 1
                cur[a] = p + 1
                p = cur[b]
                to[p], kd[p], out[p] = a, k, 0
                cur[b] = p + 1
            self._adj = (counts, to, kd, out)
            return self._adj

    def _rel(self, idx: int, other: int, k: int, outgoing: int) -> tuple[str, str]:
        if k == 0:
            return ("parent", "← ") if outgoing else ("child", "→ ")
        if k == 1:
            return ("tag", "# ") if outgoing else ("mention", "# ")
        if (self.items[other].get("thought_type") or "") == "excel_row":
            return "row", ""
        return "related", "↔ "

    def node(self, idx: int, max_neighbors: int = 90) -> Optional[dict]:
        if not 0 <= idx < self.n:
            return None
        item = self.items[idx]
        off, to, kd, out = self._ensure_adj()
        start, end = off[idx], off[idx + 1]
        buckets: dict[str, list] = {k: [] for k in ("parent", "child", "related", "tag", "mention", "row")}
        for p in range(start, end):
            rel, arrow = self._rel(idx, to[p], kd[p], out[p])
            buckets[rel].append((to[p], rel, arrow))
        caps = {"parent": 3, "child": 40, "related": 25, "tag": 25, "mention": 25, "row": 40}
        neighbors = []
        hidden = 0
        for rel in ("parent", "child", "related", "tag", "mention", "row"):
            chosen = buckets[rel][:caps[rel]]
            hidden += len(buckets[rel]) - len(chosen)
            for other, r, arrow in chosen:
                if len(neighbors) >= max_neighbors:
                    hidden += 1
                    continue
                neighbors.append({"i": other, "t": self.title(other) or f"#{other}", "rel": r, "arrow": arrow,
                                  "k": (self.items[other].get("thought_type") or "")})
        info: dict[str, Any] = {"i": idx, "v": self.version, "link_count": end - start,
                                "neighbors": neighbors, "more": hidden}
        if self._is_entity(idx):
            info.update({
                "title": self.title(idx), "type": item.get("entity_type") or "tag", "project": "",
                "content": f"Упоминаний: {item.get('mention_count') or 0}",
            })
            return info
        info.update({
            "title": self.title(idx) or f"#{idx}",
            "topic": (item.get("topic") or "").strip(),
            "luhmann": item.get("luhmann_id") or "",
            "parent_luhmann": item.get("parent_luhmann") or "",
            "type": item.get("thought_type") or "",
            "tags": ", ".join(str(t) for t in (item.get("tags") or [])[:20]),
            "content": (item.get("content") or "")[:8000],
            "source": _public_source_url(item.get("source_input") or "text"),
            "quote": (item.get("source_quote") or "")[:1500],
            "project": _norm_project(item.get("project")),
        })
        return info

    # --- поиск ---
    def _ensure_hay(self) -> list[str]:
        if self._hay is not None:
            return self._hay
        with self._lock:
            if self._hay is not None:
                return self._hay
            hay: list[str] = []
            for i, item in enumerate(self.items):
                if self._is_entity(i):
                    hay.append(_norm(f"{item.get('display_name') or ''} {item.get('name') or ''}"))
                elif (item.get("thought_type") or "") == "excel_row":
                    hay.append("")
                else:
                    tags = " ".join(str(t) for t in (item.get("tags") or [])[:10])
                    hay.append(_norm(f"{item.get('topic') or ''} {tags} {_norm_project(item.get('project'))} {item.get('luhmann_id') or ''}"))
            self._hay = hay
            return hay

    def search(self, query: str, limit: int = 40, id_limit: int = 5000, time_budget: float = 1.5) -> dict:
        toks = [t for t in _norm(query).split() if t]
        if not toks:
            return {"total": 0, "items": [], "ids": [], "v": self.version}
        cap = 20_000
        hay = self._ensure_hay()
        ids: list[int] = []
        for i, h in enumerate(hay):
            if h and all(t in h for t in toks):
                ids.append(i)
                if len(ids) >= cap:
                    break
        if len(ids) < 20 and len("".join(toks)) >= 3:
            deadline = time.monotonic() + time_budget
            have = set(ids)
            for i, item in enumerate(self.items):
                if i in have or not hay[i]:
                    continue
                if (i & 1023) == 0 and time.monotonic() > deadline:
                    break
                text = _norm((item.get("content") or "")[:2000])
                if text and all(t in text for t in toks):
                    ids.append(i)
                    if len(ids) >= 2000:
                        break
        scored = []
        phrase = " ".join(toks)
        for i in ids[:4000]:
            title = _norm(self.title(i))
            if title == phrase:
                s = 0
            elif title.startswith(phrase):
                s = 1
            elif phrase in title:
                s = 2
            else:
                s = 3
            tt = self.items[i].get("thought_type") or ""
            scored.append((s, 2 if tt in ("excel_value", "excel_column") else 0, len(title), i))
        scored.sort()
        rows = []
        for _s, _r, _l, i in scored[:limit]:
            item = self.items[i]
            rows.append({"i": i, "t": self.title(i), "p": _norm_project(item.get("project")),
                         "k": item.get("thought_type") or ("tag" if self._is_entity(i) else "")})
        return {"total": len(ids), "capped": len(ids) >= cap, "items": rows, "ids": ids[:id_limit], "v": self.version}


# --------------------------------------------------------------------------- #
# HTML / CSS
# --------------------------------------------------------------------------- #

_GRAPH_CSS = r"""
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0b1220; color: #e5e7eb; font-family: 'Segoe UI', Arial, sans-serif; overflow: hidden; height: 100vh; }
  #header { background: linear-gradient(90deg, #0f172a 0%, #111827 100%); padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #334155; z-index: 10; position: relative; }
  #header-left { display: flex; align-items: center; gap: 14px; min-width: 0; flex: 1; }
  #graph-back { color: #93c5fd; text-decoration: none; font-size: 14px; white-space: nowrap; flex-shrink: 0; }
  #graph-back:hover { text-decoration: underline; }
  #header h1 { font-size: 20px; color: #e2e8f0; font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-right: 16px; }
  #header-right { display: flex; align-items: center; gap: 12px; }
  #stats { font-size: 13px; color: #94a3b8; display: flex; gap: 18px; }
  #stats span { color: #cbd5e1; font-weight: 600; }
  #theme-toggle, #graph-controls button { border: 1px solid #334155; background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
  #theme-toggle:hover, #graph-controls button:hover { background: #1e293b; }
  #graph-controls button.active { border-color: #60a5fa; background: #1e3a5f; }
  #legend { position: absolute; bottom: 16px; left: 16px; background: rgba(15, 23, 42, 0.95); border: 1px solid #334155; border-radius: 10px; padding: 12px 16px; z-index: 10; font-size: 12px; max-width: 230px; }
  #legend h3 { margin-bottom: 8px; color: #e2e8f0; font-size: 13px; }
  .leg-item { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .leg-dot { width: 14px; height: 14px; border-radius: 50%; border: 2px solid; flex-shrink: 0; }
  .leg-bridge { width: 22px; height: 0; border-top: 2px dashed #e879f9; flex-shrink: 0; }
  .leg-region { width: 14px; height: 14px; border-radius: 50%; border: 1.5px solid #94a3b8; background: rgba(148,163,184,.15); flex-shrink: 0; }
  .leg-dot-thought { background: #D4A055; border-color: #B8862E; }
  .leg-dot-entity { background: #e2e8f0; border-color: #94a3b8; opacity: 0.72; }
  .leg-dot-column { background: #9078C8; border-color: #7058A8; }
  .leg-dot-value { background: #72B862; border-color: #529842; }
  .leg-dot-row { background: #64748B; border-color: #94A3B8; }
  #graph-wrap { position: relative; width: 100%; height: calc(100vh - 56px); cursor: grab; touch-action: none; }
  #graph { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
  #load-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; z-index: 6; pointer-events: none; background: rgba(11, 18, 32, 0.55); }
  .load-box { display: flex; flex-direction: column; align-items: center; gap: 12px; }
  .load-dial { position: relative; width: 92px; height: 92px; }
  .load-ring { width: 92px; height: 92px; display: block; }
  .load-track { fill: none; stroke: #334155; stroke-width: 6; }
  .load-arc { fill: none; stroke: #60a5fa; stroke-width: 6; stroke-linecap: round; transform-origin: 36px 36px; transform: rotate(-90deg); stroke-dasharray: 175.9; stroke-dashoffset: 132; }
  .load-arc.spin { animation: load-spin 0.9s linear infinite; }
  @keyframes load-spin { to { transform: rotate(270deg); } }
  #load-pct { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 700; color: #f8fafc; }
  #load-caption { font-size: 13px; color: #cbd5e1; text-align: center; max-width: 320px; }
  #detail-panel { position: absolute; top: 64px; right: 16px; width: 360px; max-height: calc(100vh - 100px); background: rgba(15, 23, 42, 0.98); border: 1px solid #334155; border-radius: 12px; padding: 18px; overflow-y: auto; z-index: 10; display: none; }
  #detail-panel h2 { color: #e2e8f0; margin: 0 24px 8px 0; font-size: 16px; line-height: 1.3; word-break: break-word; }
  #detail-panel .field { margin: 8px 0; }
  #detail-panel .field-label { color: #94a3b8; font-size: 12px; margin-bottom: 2px; }
  #detail-panel .field-value { color: #e2e8f0; font-size: 13px; line-height: 1.5; word-break: break-word; white-space: pre-wrap; }
  #detail-panel a { color: #93c5fd; word-break: break-all; }
  #detail-close { position: absolute; top: 10px; right: 14px; cursor: pointer; color: #94a3b8; font-size: 22px; line-height: 1; }
  #detail-panel .group-title { color: #94a3b8; font-size: 12px; margin: 12px 0 4px; }
  #detail-panel .neighbor-item { padding: 7px 10px; margin: 4px 0; background: rgba(255,255,255,0.05); border-radius: 6px; cursor: pointer; font-size: 13px; color: #e2e8f0; word-break: break-word; display: flex; gap: 6px; align-items: baseline; }
  #detail-panel .neighbor-item:hover { background: rgba(59,130,246,0.2); }
  #detail-panel .arrow { color: #94a3b8; flex-shrink: 0; }
  #detail-panel .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
  #detail-panel .chip { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: rgba(148,163,184,.18); color: #cbd5e1; cursor: default; }
  #detail-panel .chip.tag { cursor: pointer; }
  #detail-panel .chip.tag:hover { background: rgba(59,130,246,.35); }
  #detail-panel .note { margin: 10px 0; padding: 8px 10px; border-radius: 8px; background: rgba(239,68,68,.12); color: #fca5a5; font-size: 12px; line-height: 1.4; }
  #detail-panel .note.info { background: rgba(59,130,246,.12); color: #bfdbfe; }
  #detail-panel button.btn { margin: 6px 6px 0 0; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
  #detail-panel hr { border: 0; border-top: 1px solid #334155; margin: 12px 0; }
  #node-tip { position: absolute; display: none; z-index: 12; max-width: 380px; background: rgba(15, 23, 42, 0.97); border: 1px solid #334155; border-radius: 10px; padding: 8px 10px; pointer-events: none; box-shadow: 0 8px 20px rgba(0,0,0,.25); font-size: 12px; color: #e2e8f0; line-height: 1.45; }
  #node-tip .tt { font-weight: 700; color: #f8fafc; margin-bottom: 2px; word-break: break-word; }
  #node-tip .ts { color: #94a3b8; }
  #search-box { position: absolute; top: 64px; left: 16px; z-index: 11; display: flex; flex-direction: column; gap: 8px; width: 340px; max-width: calc(100vw - 32px); }
  #search-box input { background: rgba(15, 23, 42, 0.95); border: 1px solid #334155; border-radius: 8px; padding: 8px 14px; color: #e2e8f0; font-size: 13px; width: 100%; outline: none; }
  #search-box input:focus { border-color: #60a5fa; }
  #search-results { display: none; max-height: 52vh; overflow-y: auto; background: rgba(15, 23, 42, 0.98); border: 1px solid #334155; border-radius: 10px; padding: 6px; }
  #search-results .sr-head { font-size: 11px; color: #94a3b8; padding: 4px 8px; }
  #search-results .sr-item { padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; color: #e2e8f0; display: flex; flex-direction: column; gap: 1px; }
  #search-results .sr-item:hover, #search-results .sr-item.sel { background: rgba(59,130,246,.22); }
  #search-results .sr-item span { font-size: 11px; color: #94a3b8; }
  #graph-controls { display: flex; gap: 6px; flex-wrap: wrap; }
  #nav-list { display: none; max-height: 38vh; overflow-y: auto; background: rgba(15, 23, 42, 0.95); border: 1px solid #334155; border-radius: 10px; padding: 6px; }
  #nav-list .nav-head { font-size: 11px; color: #94a3b8; padding: 4px 8px; }
  #nav-list .nav-item { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; cursor: pointer; font-size: 12px; color: #e2e8f0; }
  #nav-list .nav-item:hover { background: rgba(59,130,246,.22); }
  #nav-list .nav-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  #nav-list .nav-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #nav-list .nav-n { color: #94a3b8; }
  #graph-hint { position: absolute; right: 16px; bottom: 16px; z-index: 8; pointer-events: none; max-width: 360px; font-size: 11px; color: #94a3b8; line-height: 1.35; text-align: right; }
  #stale-banner { display: none; position: absolute; top: 60px; left: 50%; transform: translateX(-50%); z-index: 30; background: #1e3a5f; color: #e2e8f0; border: 1px solid #60a5fa; border-radius: 10px; padding: 8px 14px; font-size: 13px; }
  #stale-banner a { color: #93c5fd; cursor: pointer; margin-left: 8px; text-decoration: underline; }
  body.light-theme { background: #f8fafc; color: #1f2937; }
  body.light-theme #header { background: #ffffff; border-bottom: 1px solid #e5e7eb; }
  body.light-theme #header h1 { color: #1f2937; }
  body.light-theme #graph-back { color: #2563eb; }
  body.light-theme #stats { color: #6b7280; }
  body.light-theme #stats span { color: #374151; }
  body.light-theme #theme-toggle, body.light-theme #graph-controls button { border: 1px solid #d1d5db; background: #ffffff; color: #374151; }
  body.light-theme #graph-controls button.active { border-color: #3b82f6; background: #eff6ff; }
  body.light-theme #legend { background: rgba(255,255,255,0.96); border: 1px solid #e5e7eb; }
  body.light-theme #legend h3 { color: #374151; }
  body.light-theme #detail-panel { background: #ffffff; border: 1px solid #e5e7eb; }
  body.light-theme #detail-panel h2, body.light-theme #detail-panel .field-value { color: #1f2937; }
  body.light-theme #detail-panel .field-label, body.light-theme #detail-panel .group-title, body.light-theme #graph-hint { color: #6b7280; }
  body.light-theme #detail-panel a { color: #2563eb; }
  body.light-theme #detail-panel .neighbor-item { background: #f3f4f6; color: #374151; }
  body.light-theme #detail-panel .neighbor-item:hover { background: #dbeafe; }
  body.light-theme #detail-panel .chip { background: #e5e7eb; color: #374151; }
  body.light-theme #detail-panel button.btn { background: #fff; color: #374151; border-color: #d1d5db; }
  body.light-theme #detail-panel .note { background: #fee2e2; color: #991b1b; }
  body.light-theme #detail-panel .note.info { background: #dbeafe; color: #1e3a8a; }
  body.light-theme #detail-panel hr { border-top-color: #e5e7eb; }
  body.light-theme #load-hint { background: rgba(248, 250, 252, 0.72); }
  body.light-theme .load-track { stroke: #e2e8f0; }
  body.light-theme .load-arc { stroke: #2563eb; }
  body.light-theme #load-pct { color: #0f172a; }
  body.light-theme #load-caption { color: #64748b; }
  body.light-theme #search-box input { background: #ffffff; border: 1px solid #d1d5db; color: #374151; }
  body.light-theme #search-results, body.light-theme #nav-list { background: rgba(255,255,255,0.98); border: 1px solid #d1d5db; }
  body.light-theme #search-results .sr-item, body.light-theme #nav-list .nav-item { color: #374151; }
  body.light-theme #search-results .sr-item:hover, body.light-theme #search-results .sr-item.sel, body.light-theme #nav-list .nav-item:hover { background: #dbeafe; }
  body.light-theme #node-tip { background: rgba(255,255,255,0.98); border: 1px solid #d1d5db; color: #374151; }
  body.light-theme #node-tip .tt { color: #111827; }
  body.light-theme #stale-banner { background: #eff6ff; color: #1e3a8a; }
"""

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body class="light-theme">
<div id="header">
  <div id="header-left">
    __BACK__
    <h1>__TITLE__</h1>
  </div>
  <div id="header-right">
    <div id="stats">Карточки: <span id="stat-z">__TZ__</span> &nbsp;|&nbsp; Теги: <span id="stat-e">__TE__</span> &nbsp;|&nbsp; Связи: <span id="stat-r">__TR__</span></div>
    <button id="theme-toggle" type="button">Темная тема</button>
  </div>
</div>
<div id="search-box">
  <input type="text" id="searchInput" placeholder="🔍 Найти карточку, тег или проект…" autocomplete="off"/>
  <div id="search-results"></div>
  <div id="graph-controls">
    <button type="button" id="btn-tags">Теги</button>
    <button type="button" id="btn-fit">Показать всё</button>
    <button type="button" id="btn-nav">Структура</button>
  </div>
  <div id="nav-list"></div>
</div>
<div id="stale-banner">Граф на сервере обновился. <a id="stale-reload">Обновить страницу</a></div>
<div id="graph-wrap">
  <canvas id="graph"></canvas>
  <div id="graph-hint">Крупные области — проекты и ветки: нажмите, чтобы приблизить. Приближайте колесом или щипком: сначала появятся связи и названия важных карточек, затем остальных. Двойной клик по карточке открывает её с окружением. Клавиша «/» — поиск.</div>
  <div id="load-hint">
    <div class="load-box">
      <div class="load-dial">
        <svg class="load-ring" viewBox="0 0 72 72" aria-hidden="true">
          <circle class="load-track" cx="36" cy="36" r="28"></circle>
          <circle id="load-arc" class="load-arc spin" cx="36" cy="36" r="28"></circle>
        </svg>
        <div id="load-pct">0%</div>
      </div>
      <div id="load-caption">Сервер собирает граф</div>
    </div>
  </div>
  <div id="node-tip"></div>
</div>
<div id="legend">
  <h3>Легенда</h3>
  <div class="leg-item"><div class="leg-dot leg-dot-thought"></div> Карточка (цвет = проект/ветка)</div>
  <div class="leg-item"><div class="leg-region"></div> Проект или крупная ветка</div>
  <div class="leg-item"><div class="leg-dot leg-dot-column"></div> Столбец таблицы</div>
  <div class="leg-item"><div class="leg-dot leg-dot-value"></div> Значение</div>
  <div class="leg-item"><div class="leg-dot leg-dot-row"></div> Строка таблицы</div>
  <div class="leg-item"><div class="leg-dot leg-dot-entity"></div> Тег</div>
  <div class="leg-item"><div class="leg-bridge"></div> Связь между листами/проектами</div>
</div>
<div id="detail-panel">
  <span id="detail-close">&times;</span>
  <div id="detail-content"></div>
</div>
"""


_GRAPH_JS_TEMPLATE = r"""
<script>
(function () {
'use strict';
const DATA_URL = __DATA_URL__;
const DATA_B64 = __DATA_B64__;
const PAL = [["#D4A055","#B8862E"],["#72B862","#529842"],["#C46888","#A44868"],["#9078C8","#7058A8"],["#C89848","#A87828"],["#68A898","#488878"],["#C87858","#A85838"],["#78A878","#588858"],["#D48068","#B46048"],["#88B848","#689828"]];
const $ = function (id) { return document.getElementById(id); };
const wrap = $('graph-wrap'), canvas = $('graph'), ctx = canvas.getContext('2d', { alpha: false });
const DOT = 12, MAX_DETAIL = 6500;
const TYPE_RU = { excel_sheet: 'Лист таблицы', excel_column: 'Столбец таблицы', excel_value: 'Значение', excel_row: 'Строка таблицы', tag: 'Тег' };
const REL_TITLE = { parent: 'Родитель', child: 'Дочерние карточки', related: 'Связанные', bridge: 'То же на другом листе', project: 'Связь между проектами', tag: 'Теги', mention: 'Карточки с этим тегом', row: 'Записи таблицы' };
const REL_ORDER = ['parent', 'child', 'related', 'bridge', 'project', 'tag', 'mention', 'row'];

let N = 0, E = 0, VER = '';
let X = new Float32Array(0), Y = new Float32Array(0), R = new Uint8Array(0), G = new Uint8Array(0), C = new Uint8Array(0);
let L = [], META = [], EA = null, EB = null, EK = null;
let PINDEX = null, PNAMES = null, REGIONS = [];
let W = 0, H = 0, dpr = 1, camX = 0, camY = 0, zoom = 1, minZoom = 1e-5, fitted = false, LIGHT = true;
let tagsOn = false, selected = -1, hoverNode = -1, hoverRegion = -1;
let hot = null, hotList = [], searchHits = [];
const rowPos = new Map();
let adjOff = null, adjTo = null, adjK = null, adjOut = null, projectLinks = [];
let gMinX = 0, gMinY = 0, gCell = 64, gW = 1, gH = 1, gStart = null, gItems = null;
let lodOrder = null, stamp = null, stampId = 0, tried = null;
let lastVis = [], dotPick = null, dotCols = 0;
let labelBoxes = [], hitLabels = [], regionHits = [], capBoxes = [];
let hayLocal = null;
const FULL = new Map();
const infoCache = new Map();
let raf = 0, flyId = 0;

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
}
function fillOf(i) {
  if (G[i] === 1) return LIGHT ? '#e2e8f0' : '#64748b';
  if (G[i] === 2) return '#64748B';
  return PAL[C[i] % PAL.length][0];
}
function strokeOf(i) {
  if (G[i] === 1) return LIGHT ? '#94a3b8' : '#cbd5e1';
  if (G[i] === 2) return '#94A3B8';
  return PAL[C[i] % PAL.length][1];
}
function sx(x) { return (x - camX) * zoom + W / 2; }
function sy(y) { return (y - camY) * zoom + H / 2; }
function wx(x) { return (x - W / 2) / zoom + camX; }
function wy(y) { return (y - H / 2) / zoom + camY; }
function ix(i) { if (G[i] === 2) { const p = rowPos.get(i); if (p) return p[0]; } return X[i]; }
function iy(i) { if (G[i] === 2) { const p = rowPos.get(i); if (p) return p[1]; } return Y[i]; }
function projectOf(i) {
  if (PNAMES && PINDEX) return PNAMES[PINDEX[i]] || '';
  const m = META[i]; return m && m.length > 10 ? String(m[10] || '') : '';
}
function titleOf(i) {
  const f = FULL.get(i); if (f) return f;
  if (L[i]) return L[i];
  const m = META[i]; if (m && m[2]) return String(m[2]);
  return '';
}
function typeOf(i) { const m = META[i]; return m && m.length > 3 ? String(m[3] || '') : ''; }
function hasMeta(i) { const m = META[i]; return !!(m && m.length >= 6); }

/* ---------- состояние подсветки ---------- */
function clearHot() {
  if (hot) for (let k = 0; k < hotList.length; k++) hot[hotList[k]] = 0;
  hotList = [];
}
function markHot(i, v) {
  if (!hot || i < 0 || i >= N) return;
  if (!hot[i]) hotList.push(i);
  if (v > hot[i]) hot[i] = v;
}
function restoreSearchHot() {
  clearHot();
  for (let k = 0; k < searchHits.length; k++) markHot(searchHits[k], 2);
}
function nodeVisible(i) {
  const h = hot ? hot[i] : 0;
  if (G[i] === 1 && !tagsOn && !h) return false;
  if (G[i] === 2 && !rowPos.has(i)) return false;
  return true;
}
function localNeighbors(idx) {
  const out = [];
  if (!adjOff) return out;
  for (let p = adjOff[idx]; p < adjOff[idx + 1]; p++) out.push([adjTo[p], adjK[p], adjOut[p]]);
  return out;
}
function setHotFocus(idx, extra) {
  clearHot();
  if (idx < 0) return;
  markHot(idx, 2);
  if (adjOff) {
    const end = Math.min(adjOff[idx + 1], adjOff[idx] + 4000);
    for (let p = adjOff[idx]; p < end; p++) markHot(adjTo[p], 1);
  }
  if (extra) for (let k = 0; k < extra.length; k++) markHot(extra[k], 1);
}
function parentDir(idx) {
  if (adjOff) {
    for (let p = adjOff[idx]; p < adjOff[idx + 1]; p++) {
      if (adjK[p] === 0 && adjOut[p] === 1 && G[adjTo[p]] !== 2) {
        const pr = adjTo[p];
        const dx = X[idx] - X[pr], dy = Y[idx] - Y[pr], len = Math.hypot(dx, dy) || 1;
        return [dx / len, dy / len];
      }
    }
  }
  return [0, -1];
}
function revealRows(idx, extra) {
  rowPos.clear();
  if (idx < 0) return;
  const rows = [], vals = [];
  const consider = function (j) {
    if (j === idx || j < 0 || j >= N) return;
    if (G[j] === 2) rows.push(j); else vals.push(j);
  };
  if (adjOff) for (let p = adjOff[idx]; p < adjOff[idx + 1]; p++) if (adjK[p] === 2) consider(adjTo[p]);
  if (extra) for (let k = 0; k < extra.length; k++) consider(extra[k]);
  if (G[idx] === 2) {
    if (!vals.length) { rowPos.set(idx, [camX + 160, camY]); return; }
    let ax = 0, ay = 0;
    for (let k = 0; k < vals.length; k++) { ax += X[vals[k]]; ay += Y[vals[k]]; }
    rowPos.set(idx, [ax / vals.length, ay / vals.length - 72]);
    return;
  }
  const use = Array.from(new Set(rows)).slice(0, 12);
  if (!use.length) return;
  const dir = parentDir(idx), px = -dir[1], py = dir[0];
  for (let k = 0; k < use.length; k++) {
    const side = (k - (use.length - 1) / 2) * 54, along = 168 + (k % 2) * 28;
    rowPos.set(use[k], [X[idx] + dir[0] * along + px * side, Y[idx] + dir[1] * along + py * side]);
  }
}

/* ---------- индексы ---------- */
function buildLod() {
  const cnt = new Uint32Array(258);
  let m = 0;
  for (let i = 0; i < N; i++) { if (G[i] === 2) continue; cnt[256 - R[i]]++; m++; }
  for (let k = 1; k < 258; k++) cnt[k] += cnt[k - 1];
  lodOrder = new Int32Array(m);
  const cur = new Uint32Array(258);
  for (let k = 1; k < 258; k++) cur[k] = cnt[k - 1];
  for (let i = 0; i < N; i++) { if (G[i] === 2) continue; lodOrder[cur[255 - R[i] + 1]++] = i; }
}
function buildGrid() {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, used = 0;
  for (let i = 0; i < N; i++) {
    if (G[i] === 2 || !isFinite(X[i]) || !isFinite(Y[i])) continue;
    if (X[i] < minX) minX = X[i]; if (X[i] > maxX) maxX = X[i];
    if (Y[i] < minY) minY = Y[i]; if (Y[i] > maxY) maxY = Y[i];
    used++;
  }
  if (!used) { gStart = new Int32Array(2); gItems = new Int32Array(0); gW = gH = 1; return; }
  const bw = Math.max(maxX - minX, 1), bh = Math.max(maxY - minY, 1);
  let cell = Math.max(24, Math.sqrt(bw * bh * 6 / used));
  let gw, gh;
  for (;;) {
    gw = Math.floor(bw / cell) + 1; gh = Math.floor(bh / cell) + 1;
    if (gw * gh <= 3000000) break;
    cell *= 1.3;
  }
  gMinX = minX; gMinY = minY; gCell = cell; gW = gw; gH = gh;
  const cells = gw * gh;
  const cellOf = new Int32Array(N).fill(-1);
  gStart = new Int32Array(cells + 1);
  for (let i = 0; i < N; i++) {
    if (G[i] === 2 || !isFinite(X[i]) || !isFinite(Y[i])) continue;
    const c = Math.floor((Y[i] - minY) / cell) * gw + Math.floor((X[i] - minX) / cell);
    cellOf[i] = c; gStart[c + 1]++;
  }
  for (let c = 0; c < cells; c++) gStart[c + 1] += gStart[c];
  gItems = new Int32Array(gStart[cells]);
  const cur = gStart.slice(0, cells);
  for (let i = 0; i < N; i++) { const c = cellOf[i]; if (c >= 0) gItems[cur[c]++] = i; }
}
function buildAdj() {
  E = EA ? EA.length : 0;
  const cnt = new Uint32Array(N + 1);
  for (let i = 0; i < E; i++) { cnt[EA[i] + 1]++; cnt[EB[i] + 1]++; }
  for (let i = 0; i < N; i++) cnt[i + 1] += cnt[i];
  adjOff = cnt;
  const total = cnt[N];
  adjTo = new Uint32Array(total); adjK = new Uint8Array(total); adjOut = new Uint8Array(total);
  const cur = cnt.slice(0, N);
  const links = [];
  for (let i = 0; i < E; i++) {
    const a = EA[i], b = EB[i], k = EK[i];
    let p = cur[a]++; adjTo[p] = b; adjK[p] = k; adjOut[p] = 1;
    p = cur[b]++; adjTo[p] = a; adjK[p] = k; adjOut[p] = 0;
    if (k === 4) links.push(a, b);
  }
  projectLinks = links;
}

/* ---------- вспомогательные размеры ---------- */
function overviewRadius(i) { const r = R[i]; return r >= 15 ? 7.4 : (r >= 11 ? 5.4 : (r >= 8 ? 3.6 : 2.3)); }
function screenRadius(i) {
  const raw = R[i] * Math.min(zoom, 0.9);
  const minR = zoom < 0.2 ? 2.2 : 3.1;
  let cap = 5.6;
  if (G[i] === 1) cap = 6; else if (R[i] >= 15) cap = 14; else if (R[i] >= 11) cap = 11; else if (R[i] >= 8) cap = 8.2;
  return Math.min(cap, Math.max(minR, raw));
}
function labelMinZoom(i) {
  if (G[i] === 1) return 0.45;
  const r = R[i];
  return r >= 15 ? 0.07 : (r >= 12 ? 0.2 : (r >= 9 ? 0.4 : (r >= 7 ? 0.7 : 1.0)));
}
function roundBox(x, y, w, h, radius) {
  const r = Math.min(radius, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function overlaps(list, box) {
  for (let i = 0; i < list.length; i++) {
    const b = list[i];
    if (box.x < b.x + b.w && box.x + box.w > b.x && box.y < b.y + b.h && box.y + box.h > b.y) return true;
  }
  return false;
}

/* ---------- рисование ---------- */
function resize() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  W = wrap.clientWidth; H = wrap.clientHeight;
  canvas.width = Math.floor(W * dpr); canvas.height = Math.floor(H * dpr);
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  if (!fitted && N) fit();
  requestDraw();
}
function requestDraw() { if (raf) return; raf = requestAnimationFrame(function () { raf = 0; draw(); }); }

function drawNode(i, alpha, ring) {
  const x = sx(ix(i)), y = sy(iy(i));
  if (x < -30 || y < -30 || x > W + 30 || y > H + 30) return;
  const r = screenRadius(i);
  ctx.globalAlpha = G[i] === 1 ? alpha * 0.68 : alpha;
  ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832);
  ctx.fillStyle = fillOf(i); ctx.fill();
  ctx.lineWidth = ring ? 2 : 1; ctx.strokeStyle = ring ? '#2563eb' : strokeOf(i); ctx.stroke();
}
function drawLabel(i, alpha) {
  const text = L[i];
  if (!text) return false;
  const r = screenRadius(i);
  const tx = sx(ix(i)), ty = sy(iy(i)) + r + 3;
  const tw = ctx.measureText(text).width;
  const box = { x: tx - tw / 2 - 4, y: ty - 1, w: tw + 8, h: 16 };
  if (overlaps(labelBoxes, box)) return false;
  labelBoxes.push(box);
  hitLabels.push({ i: i, x: box.x - 2, y: box.y - 2, w: box.w + 4, h: box.h + 4 });
  ctx.globalAlpha = Math.max(alpha, 0.92);
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  ctx.fillStyle = LIGHT ? 'rgba(248,250,252,0.94)' : 'rgba(15,23,42,0.9)';
  roundBox(box.x, box.y, box.w, box.h, 4); ctx.fill();
  ctx.fillStyle = LIGHT ? '#0f172a' : '#f8fafc';
  ctx.fillText(text, tx, ty);
  return true;
}
function drawProjectLinks() {
  if (!projectLinks.length) return;
  ctx.save();
  ctx.globalAlpha = 0.7; ctx.strokeStyle = LIGHT ? '#0369a1' : '#7dd3fc'; ctx.lineWidth = 1.4; ctx.setLineDash([7, 5]);
  ctx.beginPath();
  for (let i = 0; i < projectLinks.length; i += 2) {
    const a = projectLinks[i], b = projectLinks[i + 1];
    ctx.moveTo(sx(X[a]), sy(Y[a])); ctx.lineTo(sx(X[b]), sy(Y[b]));
  }
  ctx.stroke();
  ctx.restore();
}
function regionVisible(g) {
  const sr = g.r * zoom, vmin = Math.min(W, H);
  if (sr < 10 || sr > vmin * 3) return false;
  const x = sx(g.x), y = sy(g.y);
  return !(x + sr < -20 || x - sr > W + 20 || y + sr < -20 || y - sr > H + 20);
}
function drawRegions() {
  if (!REGIONS.length) return;
  for (let lv = 0; lv <= 1; lv++) {
    for (let k = 0; k < REGIONS.length; k++) {
      const g = REGIONS[k];
      if (g.lv !== lv || !regionVisible(g)) continue;
      const pal = PAL[g.c % PAL.length], sr = g.r * zoom, active = hoverRegion === k;
      ctx.globalAlpha = active ? 0.14 : (lv === 0 ? 0.05 : 0.06);
      ctx.beginPath(); ctx.arc(sx(g.x), sy(g.y), sr, 0, 6.2832); ctx.fillStyle = pal[0]; ctx.fill();
      ctx.globalAlpha = active ? 0.75 : (lv === 0 ? 0.4 : 0.26);
      ctx.lineWidth = lv === 0 ? 1.8 : 1.1; ctx.strokeStyle = pal[1]; ctx.stroke();
    }
  }
}
function drawRegionCaptions() {
  if (!REGIONS.length) return;
  const vmin = Math.min(W, H);
  const cand = [];
  for (let k = 0; k < REGIONS.length; k++) {
    const g = REGIONS[k];
    if (!regionVisible(g)) continue;
    const sr = g.r * zoom;
    if (g.lv === 0 ? (sr < 30 || sr > vmin * 0.95) : (sr < 38 || sr > vmin * 0.9)) continue;
    cand.push(k);
  }
  cand.sort(function (a, b) { return REGIONS[a].lv - REGIONS[b].lv || REGIONS[b].n - REGIONS[a].n; });
  capBoxes = [];
  for (let c = 0; c < cand.length && c < 60; c++) {
    const g = REGIONS[cand[c]];
    const big = g.lv === 0;
    ctx.font = (big ? '700 15px' : '600 12px') + ' Arial';
    const name = g.name.length > 28 ? g.name.slice(0, 27) + '…' : g.name;
    const text = name + ' · ' + g.n.toLocaleString('ru-RU');
    const tw = ctx.measureText(text).width + 18, th = big ? 28 : 22;
    const px = sx(g.x), py = sy(g.y);
    const box = { x: px - tw / 2, y: py - th / 2, w: tw, h: th };
    if (box.x < 0 || box.y < 0 || box.x + box.w > W || box.y + box.h > H) continue;
    if (overlaps(capBoxes, box)) continue;
    capBoxes.push(box);
    regionHits.push({ k: cand[c], x: box.x, y: box.y, w: box.w, h: box.h });
    const pal = PAL[g.c % PAL.length];
    ctx.globalAlpha = 0.95;
    ctx.fillStyle = LIGHT ? 'rgba(255,255,255,0.95)' : 'rgba(15,23,42,0.92)';
    roundBox(box.x, box.y, box.w, box.h, 9); ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = pal[1]; ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = LIGHT ? '#0f172a' : '#f8fafc';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, px, py + 0.5);
  }
}
function pickVisible(x0, y0, x1, y1) {
  if (!gStart || !N) return null;
  if (N > 1500 && zoom < 0.3) return null;
  if (zoom < 0.06) return null;
  const cx0 = Math.max(0, Math.floor((x0 - gMinX) / gCell)), cx1 = Math.min(gW - 1, Math.floor((x1 - gMinX) / gCell));
  const cy0 = Math.max(0, Math.floor((y0 - gMinY) / gCell)), cy1 = Math.min(gH - 1, Math.floor((y1 - gMinY) / gCell));
  const out = [];
  if (cx1 < cx0 || cy1 < cy0) { appendRows(out); return out; }
  if ((cx1 - cx0 + 1) * (cy1 - cy0 + 1) > 40000) return null;
  let est = 0;
  for (let cy = cy0; cy <= cy1; cy++) {
    let c = cy * gW + cx0;
    for (let cx = cx0; cx <= cx1; cx++, c++) est += gStart[c + 1] - gStart[c];
  }
  if (est > MAX_DETAIL && N > 1500) return null;
  for (let cy = cy0; cy <= cy1; cy++) {
    for (let cx = cx0; cx <= cx1; cx++) {
      const c = cy * gW + cx;
      for (let p = gStart[c]; p < gStart[c + 1]; p++) {
        const i = gItems[p];
        if (G[i] === 1 && !tagsOn && !(hot && hot[i])) continue;
        out.push(i);
      }
    }
  }
  appendRows(out);
  return out;
}
function appendRows(out) { rowPos.forEach(function (_v, i) { out.push(i); }); }

function drawOverview(x0, y0, x1, y1) {
  const cols = Math.ceil(W / DOT) + 2, rows = Math.ceil(H / DOT) + 2;
  if (!dotPick || dotPick.length !== cols * rows) dotPick = new Int32Array(cols * rows);
  dotPick.fill(-1); dotCols = cols;
  lastVis = [];
  drawProjectLinks();
  const ord = lodOrder, hotOn = hotList.length > 0;
  for (let k = 0; k < ord.length; k++) {
    const i = ord[k], x = X[i], y = Y[i];
    if (x < x0 || x > x1 || y < y0 || y > y1) continue;
    if (G[i] === 1 && !tagsOn) continue;
    const bx = (((x - camX) * zoom + W / 2) / DOT) | 0, by = (((y - camY) * zoom + H / 2) / DOT) | 0;
    if (bx < 0 || by < 0 || bx >= cols || by >= rows) continue;
    const id = bx + by * cols;
    if (dotPick[id] < 0) dotPick[id] = i;
  }
  const outline = LIGHT ? '#ffffff' : '#0b1220';
  ctx.lineWidth = 1.3; ctx.strokeStyle = outline;
  for (let id = 0; id < dotPick.length; id++) {
    const i = dotPick[id];
    if (i < 0) continue;
    ctx.globalAlpha = (hotOn && !hot[i] ? 0.28 : 0.92) * (G[i] === 1 ? 0.7 : 1);
    ctx.beginPath(); ctx.arc(sx(X[i]), sy(Y[i]), overviewRadius(i), 0, 6.2832);
    ctx.fillStyle = fillOf(i); ctx.fill(); ctx.stroke();
  }
  if (hotOn) {
    for (let k = 0; k < hotList.length && k < 6000; k++) {
      const i = hotList[k];
      if (!nodeVisible(i)) continue;
      const x = sx(ix(i)), y = sy(iy(i));
      if (x < -20 || y < -20 || x > W + 20 || y > H + 20) continue;
      ctx.globalAlpha = 1; ctx.beginPath();
      ctx.arc(x, y, hot[i] === 2 ? 7 : 4.5, 0, 6.2832);
      ctx.fillStyle = fillOf(i); ctx.fill();
      ctx.lineWidth = hot[i] === 2 ? 2.4 : 1.4; ctx.strokeStyle = hot[i] === 2 ? '#2563eb' : strokeOf(i); ctx.stroke();
    }
  }
  // подписи самых крупных карточек
  ctx.font = '12px Arial';
  let drawnL = 0;
  for (let id = 0; id < dotPick.length && drawnL < 140; id++) {
    const i = dotPick[id];
    if (i < 0 || R[i] < 12 || !L[i] || zoom < labelMinZoom(i)) continue;
    if (drawLabel(i, 1)) drawnL++;
  }
}

function drawDetail(vis, x0, y0, x1, y1) {
  lastVis = vis; dotPick = null;
  stampId++;
  if (stampId > 4e9) { stamp.fill(0); stampId = 1; }
  const hotOn = hotList.length > 0, dim = hotOn ? 0.16 : 1, showTags = tagsOn && zoom >= 0.28;
  for (let v = 0; v < vis.length; v++) { const i = vis[v]; if (nodeVisible(i)) stamp[i] = stampId; }
  if (hotOn) {
    for (let k = 0; k < hotList.length; k++) {
      const i = hotList[k];
      if (stamp[i] === stampId || !nodeVisible(i)) continue;
      const x = ix(i), y = iy(i);
      if (x < x0 || x > x1 || y < y0 || y > y1) continue;
      stamp[i] = stampId; vis.push(i);
    }
  }
  drawProjectLinks();
  if (adjOff) {
    const pN = new Path2D(), pT = new Path2D(), pB = new Path2D(), pF = new Path2D();
    const maxE = vis.length < 1200 ? 24000 : 9000;
    let drawn = 0;
    for (let v = 0; v < vis.length && drawn < maxE; v++) {
      const a = vis[v];
      if (stamp[a] !== stampId) continue;
      for (let p = adjOff[a]; p < adjOff[a + 1] && drawn < maxE; p++) {
        const b = adjTo[p], k = adjK[p];
        if (k === 4) continue;
        if (k === 1 && !showTags) continue;
        if (stamp[b] === stampId && b < a) continue;
        const rowEdge = G[a] === 2 || G[b] === 2;
        if (k === 2 && !(selected >= 0 && (a === selected || b === selected) && rowEdge)) continue;
        if (k !== 3 && rowEdge && selected !== a && selected !== b) continue;
        if ((G[b] === 2 && !rowPos.has(b)) || (G[a] === 2 && !rowPos.has(a))) continue;
        const ax = sx(ix(a)), ay = sy(iy(a)), bx = sx(ix(b)), by = sy(iy(b));
        if ((ax < -20 && bx < -20) || (ax > W + 20 && bx > W + 20) || (ay < -20 && by < -20) || (ay > H + 20 && by > H + 20)) continue;
        const focus = hotOn && (hot[a] === 2 || hot[b] === 2);
        const path = focus ? pF : (k === 1 ? pT : (k === 3 ? pB : pN));
        path.moveTo(ax, ay); path.lineTo(bx, by); drawn++;
      }
    }
    ctx.lineWidth = 0.8; ctx.setLineDash([]);
    ctx.globalAlpha = hotOn ? 0.14 : 0.3; ctx.strokeStyle = LIGHT ? '#64748b' : '#94a3b8'; ctx.stroke(pN);
    ctx.globalAlpha = 0.22; ctx.strokeStyle = LIGHT ? '#94a3b8' : '#64748b'; ctx.setLineDash([6, 4]); ctx.stroke(pT);
    ctx.globalAlpha = 0.5; ctx.strokeStyle = '#c026d3'; ctx.lineWidth = 1.2; ctx.stroke(pB);
    ctx.globalAlpha = 0.9; ctx.strokeStyle = '#2563eb'; ctx.lineWidth = 1.4; ctx.setLineDash([]); ctx.stroke(pF);
  }
  for (let v = 0; v < vis.length; v++) {
    const i = vis[v];
    if (stamp[i] !== stampId || (hotOn && hot[i])) continue;
    drawNode(i, dim, false);
  }
  if (hotOn) {
    for (let k = 0; k < hotList.length; k++) {
      const i = hotList[k];
      if (stamp[i] === stampId) drawNode(i, 1, hot[i] === 2);
    }
  }
  ctx.font = '12px Arial';
  const ids = [];
  for (let v = 0; v < vis.length; v++) {
    const i = vis[v];
    if (stamp[i] !== stampId) continue;
    if (hotOn) { if (!hot[i]) continue; }
    else if (zoom < labelMinZoom(i)) continue;
    ids.push(i);
  }
  ids.sort(function (a, b) { return ((hotOn ? hot[b] - hot[a] : 0) || R[b] - R[a]); });
  const need = [];
  let drawnL = 0;
  for (let k = 0; k < ids.length && drawnL < 320; k++) {
    const i = ids[k];
    if (!L[i] && G[i] !== 2 && !tried[i] && need.length < 300) need.push(i);
    if (drawLabel(i, hotOn ? 1 : dim)) drawnL++;
  }
  if (need.length && DATA_URL && zoom >= 0.45) scheduleTitles(need);
}
function drawSelection() {
  if (selected < 0 || !nodeVisible(selected)) return;
  const x = sx(ix(selected)), y = sy(iy(selected));
  if (x < -20 || y < -20 || x > W + 20 || y > H + 20) return;
  ctx.globalAlpha = 1; ctx.lineWidth = 2.4; ctx.strokeStyle = '#2563eb';
  ctx.beginPath(); ctx.arc(x, y, Math.max(9, screenRadius(selected) + 4), 0, 6.2832); ctx.stroke();
}
function draw() {
  LIGHT = document.body.classList.contains('light-theme');
  hitLabels = []; regionHits = []; labelBoxes = [];
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalAlpha = 1;
  ctx.fillStyle = LIGHT ? '#f8fafc' : '#0b1220';
  ctx.fillRect(0, 0, W, H);
  if (!N) {
    ctx.fillStyle = LIGHT ? '#6b7280' : '#94a3b8'; ctx.font = '16px Arial';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('Граф пуст', W / 2, H / 2);
    return;
  }
  if (!fitted) fit();
  const pad = 40 / Math.max(zoom, 1e-6);
  const x0 = wx(0) - pad, y0 = wy(0) - pad, x1 = wx(W) + pad, y1 = wy(H) + pad;
  drawRegions();
  const vis = pickVisible(x0, y0, x1, y1);
  if (vis === null) drawOverview(x0, y0, x1, y1); else drawDetail(vis, x0, y0, x1, y1);
  drawSelection();
  drawRegionCaptions();
  ctx.globalAlpha = 1;
}

/* ---------- камера ---------- */
function fit() {
  if (!N || W < 80 || H < 80) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const top = REGIONS.filter(function (g) { return g.lv === 0; });
  if (top.length) {
    for (let k = 0; k < top.length; k++) {
      const g = top[k];
      minX = Math.min(minX, g.x - g.r); maxX = Math.max(maxX, g.x + g.r);
      minY = Math.min(minY, g.y - g.r); maxY = Math.max(maxY, g.y + g.r);
    }
  } else {
    const xs = [], ys = [], step = Math.max(1, Math.floor(N / 60000));
    for (let i = 0; i < N; i += step) {
      if (G[i] !== 0 || !isFinite(X[i]) || !isFinite(Y[i])) continue;
      xs.push(X[i]); ys.push(Y[i]);
    }
    if (!xs.length) return;
    xs.sort(function (a, b) { return a - b; }); ys.sort(function (a, b) { return a - b; });
    const lo = Math.floor(xs.length * 0.005), hi = Math.max(lo, Math.ceil(xs.length * 0.995) - 1);
    minX = xs[lo]; maxX = xs[hi]; minY = ys[lo]; maxY = ys[hi];
  }
  if (!isFinite(minX)) return;
  camX = (minX + maxX) / 2; camY = (minY + maxY) / 2;
  const dx = Math.max(maxX - minX, 200), dy = Math.max(maxY - minY, 200);
  const next = Math.min((W - 60) / dx, (H - 60) / dy);
  zoom = Math.min(1.4, next > 0 && isFinite(next) ? next : 0.2);
  minZoom = Math.max(1e-6, zoom * 0.4);
  fitted = true;
}
function applyZoomAt(mx, my, factor) {
  if (!factor || factor === 1) return;
  const bx = wx(mx), by = wy(my);
  zoom = Math.max(minZoom, Math.min(16, zoom * factor));
  camX = bx - (mx - W / 2) / zoom; camY = by - (my - H / 2) / zoom;
  requestDraw();
}
function flyTo(x, y, z) {
  z = Math.max(minZoom, Math.min(16, z));
  const x0 = camX, y0 = camY, z0 = Math.log(zoom), z1 = Math.log(z), t0 = performance.now(), id = ++flyId;
  (function step(now) {
    if (id !== flyId) return;
    let t = Math.min(1, (now - t0) / 420); t = 1 - Math.pow(1 - t, 3);
    camX = x0 + (x - x0) * t; camY = y0 + (y - y0) * t; zoom = Math.exp(z0 + (z1 - z0) * t);
    requestDraw();
    if (t < 1) requestAnimationFrame(step);
  })(t0);
}
function zoomForRadius(r) { return Math.min(Math.max(W - 400, 300), H) / (2.3 * Math.max(r, 40)); }
function flyToRegion(k) { const g = REGIONS[k]; if (g) flyTo(g.x, g.y, zoomForRadius(g.r)); }
function frameIds(ids) {
  const pts = [];
  for (let k = 0; k < ids.length; k++) { const i = ids[k]; if (G[i] !== 2 && isFinite(X[i]) && isFinite(Y[i])) pts.push(i); }
  if (!pts.length) return;
  if (pts.length === 1) { flyTo(X[pts[0]], Y[pts[0]], Math.max(zoom, 1.0)); return; }
  const cell = 400, bins = new Map();
  for (let k = 0; k < pts.length; k++) {
    const i = pts[k], key = Math.floor(X[i] / cell) + ':' + Math.floor(Y[i] / cell);
    let b = bins.get(key); if (!b) { b = []; bins.set(key, b); } b.push(i);
  }
  let best = null;
  bins.forEach(function (b) { if (!best || b.length > best.length) best = b; });
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let k = 0; k < best.length; k++) {
    const i = best[k];
    minX = Math.min(minX, X[i]); maxX = Math.max(maxX, X[i]); minY = Math.min(minY, Y[i]); maxY = Math.max(maxY, Y[i]);
  }
  const dx = Math.max(maxX - minX, 160), dy = Math.max(maxY - minY, 160);
  flyTo((minX + maxX) / 2, (minY + maxY) / 2, Math.min(1.05, Math.min((W - 440) / dx, (H - 120) / dy)));
}

/* ---------- попадание курсора ---------- */
function hit(px, py) {
  for (let k = hitLabels.length - 1; k >= 0; k--) {
    const b = hitLabels[k];
    if (px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h) return b.i;
  }
  let best = -1, bestD = 1e12;
  if (dotPick) {
    const bx = (px / DOT) | 0, by = (py / DOT) | 0;
    for (let oy = -1; oy <= 1; oy++) for (let ox = -1; ox <= 1; ox++) {
      const cx = bx + ox, cy = by + oy;
      if (cx < 0 || cy < 0 || cx >= dotCols) continue;
      const id = cx + cy * dotCols;
      if (id < 0 || id >= dotPick.length) continue;
      const i = dotPick[id];
      if (i < 0) continue;
      const dx = sx(X[i]) - px, dy = sy(Y[i]) - py, d2 = dx * dx + dy * dy, r = overviewRadius(i) + 4;
      if (d2 <= r * r && d2 < bestD) { best = i; bestD = d2; }
    }
    return best;
  }
  for (let c = 0; c < lastVis.length; c++) {
    const i = lastVis[c];
    if (!nodeVisible(i)) continue;
    const dx = sx(ix(i)) - px, dy = sy(iy(i)) - py, d2 = dx * dx + dy * dy, r = Math.max(screenRadius(i), 9) + 3;
    if (d2 <= r * r && d2 < bestD) { best = i; bestD = d2; }
  }
  return best;
}
function hitRegion(px, py) {
  for (let k = regionHits.length - 1; k >= 0; k--) {
    const b = regionHits[k];
    if (px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h) return b.k;
  }
  let best = -1, bestR = Infinity;
  for (let k = 0; k < REGIONS.length; k++) {
    const g = REGIONS[k];
    if (!regionVisible(g) || g.r * zoom < 18) continue;
    const dx = sx(g.x) - px, dy = sy(g.y) - py;
    if (dx * dx + dy * dy <= g.r * zoom * g.r * zoom && g.r < bestR) { best = k; bestR = g.r; }
  }
  return best;
}

/* ---------- подсказки ---------- */
const tip = $('node-tip');
function placeTip(px, py) {
  const tw = tip.offsetWidth || 240, th = tip.offsetHeight || 60;
  let left = px + 16, top = py + 16;
  if (left + tw > W - 8) left = Math.max(8, px - tw - 12);
  if (top + th > H - 8) top = Math.max(8, py - th - 12);
  tip.style.left = left + 'px'; tip.style.top = top + 'px';
}
function showNodeTip(i, px, py) {
  const title = titleOf(i) || ('Карточка #' + i);
  const proj = projectOf(i), tt = typeOf(i);
  const sub = [TYPE_RU[tt] || (G[i] === 1 ? 'Тег' : ''), proj].filter(Boolean).join(' · ');
  tip.innerHTML = '<div class="tt">' + esc(title) + '</div>' + (sub ? '<div class="ts">' + esc(sub) + '</div>' : '');
  tip.style.display = 'block'; placeTip(px, py);
  if (!titleOf(i) && DATA_URL && !tried[i]) {
    scheduleTitles([i], function () { if (hoverNode === i) showNodeTip(i, px, py); });
  }
}
function showRegionTip(k, px, py) {
  const g = REGIONS[k];
  tip.innerHTML = '<div class="tt">' + esc(g.name) + '</div><div class="ts">' + (g.lv === 0 ? 'Проект' : 'Ветка') + ' · ' + g.n.toLocaleString('ru-RU') + ' карточек · нажмите, чтобы приблизить</div>';
  tip.style.display = 'block'; placeTip(px, py);
}
function hideTip() { tip.style.display = 'none'; }

/* ---------- ленивые подписи ---------- */
let titleQueue = new Set(), titleTimer = 0, titleCbs = [];
function scheduleTitles(ids, cb) {
  for (let k = 0; k < ids.length; k++) if (!tried[ids[k]]) titleQueue.add(ids[k]);
  if (cb) titleCbs.push(cb);
  if (titleTimer || !titleQueue.size) return;
  titleTimer = setTimeout(flushTitles, 170);
}
async function flushTitles() {
  titleTimer = 0;
  const ids = Array.from(titleQueue).slice(0, 300);
  ids.forEach(function (i) { titleQueue.delete(i); tried[i] = 1; });
  const cbs = titleCbs; titleCbs = [];
  if (!ids.length || !DATA_URL) return;
  try {
    const resp = await fetch(DATA_URL + '/titles?v=' + encodeURIComponent(VER) + '&ids=' + ids.join(','));
    if (resp.status === 409) { markStale(); return; }
    if (!resp.ok) return;
    const data = await resp.json();
    const arr = Array.isArray(data) ? data : (data.t || []);
    for (let k = 0; k < ids.length; k++) if (arr[k]) L[ids[k]] = arr[k];
    cbs.forEach(function (f) { try { f(); } catch (e) { /* ignore */ } });
    requestDraw();
  } catch (e) { /* сеть: подписи просто не появятся */ }
  if (titleQueue.size) titleTimer = setTimeout(flushTitles, 250);
}
function markStale() { $('stale-banner').style.display = 'block'; }

/* ---------- панель карточки ---------- */
let detailTicket = 0, detailAbort = null;
function sourceHtml(src) {
  src = String(src || '').trim();
  if (!src) return '';
  let body;
  if (/^https?:\/\//i.test(src)) body = '<a href="' + esc(src) + '" target="_blank" rel="noopener noreferrer">' + esc(src) + '</a>';
  else if (src === 'text') body = 'текст';
  else body = esc(src);
  return '<div class="field"><div class="field-label">Источник</div><div class="field-value">' + body + '</div></div>';
}
function localInfo(idx) {
  const m = META[idx] || [];
  if (m.length >= 11) {
    return { title: m[2] || L[idx] || m[0] || ('#' + idx), project: m[10] || projectOf(idx), luhmann: m[0], parent_luhmann: m[1], topic: m[2], type: m[3], tags: m[4], content: m[5], source: m[8], quote: m[9], neighbors: null };
  }
  if (m.length >= 6) return { title: m[2] || L[idx] || ('#' + idx), type: m[3], content: m[5], project: '', neighbors: null };
  return { title: titleOf(idx) || ('Карточка #' + idx), project: projectOf(idx), neighbors: null, partial: true };
}
function localNeighborItems(idx) {
  const items = [], seen = new Set();
  const nb = localNeighbors(idx);
  for (let n = 0; n < nb.length; n++) {
    const j = nb[n][0], k = nb[n][1], out = nb[n][2];
    if (seen.has(j * 8 + k)) continue;
    seen.add(j * 8 + k);
    let rel, arrow = '';
    if (k === 0) { rel = out ? 'parent' : 'child'; arrow = out ? '← ' : '→ '; }
    else if (k === 1) { rel = out ? 'tag' : 'mention'; arrow = '# '; }
    else if (k === 2) { rel = G[j] === 2 ? 'row' : 'related'; arrow = G[j] === 2 ? '' : '↔ '; }
    else if (k === 3) { rel = 'bridge'; arrow = '↔ '; }
    else { rel = 'project'; arrow = '↔ '; }
    items.push({ i: j, t: titleOf(j) || ('Карточка #' + j), rel: rel, arrow: arrow });
  }
  return items;
}
function renderPanel(idx, info, opts) {
  opts = opts || {};
  const content = $('detail-content');
  const title = info.title || titleOf(idx) || ('Карточка #' + idx);
  let html = '<h2>' + esc(title) + '</h2>';
  const chips = [];
  const tlabel = TYPE_RU[info.type] || (info.type && String(info.type).indexOf('excel_') === 0 ? info.type : '');
  if (tlabel) chips.push('<span class="chip">' + esc(tlabel) + '</span>');
  if (info.project) chips.push('<span class="chip">' + esc(info.project) + '</span>');
  if (chips.length) html += '<div class="chips">' + chips.join('') + '</div>';
  if (opts.note) html += '<div class="note' + (opts.noteInfo ? ' info' : '') + '">' + esc(opts.note) + '</div>';
  if (opts.actions) html += opts.actions;
  if (info.luhmann) html += '<div class="field-value"><b>[' + esc(info.luhmann) + ']</b> ' + (info.parent_luhmann ? '← [' + esc(info.parent_luhmann) + ']' : 'корневая') + '</div>';
  if (info.topic && info.topic !== title) html += '<div class="field"><div class="field-label">Тема</div><div class="field-value">' + esc(info.topic) + '</div></div>';
  if (info.type && !tlabel) html += '<div class="field"><div class="field-label">Тип</div><div class="field-value">' + esc(info.type) + '</div></div>';
  if (info.tags) {
    const tags = String(info.tags).split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (tags.length) html += '<div class="field"><div class="field-label">Теги</div><div class="chips">' + tags.map(function (t) { return '<span class="chip tag" data-tag="' + esc(t) + '">' + esc(t) + '</span>'; }).join('') + '</div></div>';
  }
  html += sourceHtml(info.source);
  if (info.quote) html += '<div class="field"><div class="field-label">Абзац в источнике</div><div class="field-value">' + esc(info.quote) + '</div></div>';
  if (info.content) html += '<hr/><div class="field-value">' + esc(info.content) + '</div>';
  const items = info.neighbors || localNeighborItems(idx);
  if (items.length) {
    const groups = {};
    items.forEach(function (it) { (groups[it.rel] = groups[it.rel] || []).push(it); });
    html += '<hr/>';
    REL_ORDER.forEach(function (rel) {
      const list = groups[rel];
      if (!list) return;
      html += '<div class="group-title">' + esc(REL_TITLE[rel] || rel) + ' (' + list.length + ')</div>';
      list.slice(0, 40).forEach(function (it) {
        html += '<div class="neighbor-item" data-i="' + it.i + '"><span class="arrow">' + esc(it.arrow || '') + '</span><b>' + esc(it.t || ('Карточка #' + it.i)) + '</b></div>';
      });
      if (list.length > 40) html += '<div class="group-title">ещё ' + (list.length - 40) + '</div>';
    });
    const total = info.link_count || items.length;
    if (info.more) html += '<div class="group-title">Всего связей: ' + total + ', показаны не все</div>';
  } else if (!opts.loading) {
    html += '<div class="group-title">Связей нет</div>';
  }
  content.innerHTML = html;
  $('detail-panel').style.display = 'block';
}
function showDetail(idx) {
  selected = idx;
  hideTip();
  if (!adjOff && !DATA_URL) { renderPanel(idx, localInfo(idx)); requestDraw(); return; }
  setHotFocus(idx); revealRows(idx);
  if (hasMeta(idx) || !DATA_URL) { renderPanel(idx, localInfo(idx)); requestDraw(); return; }
  const cached = infoCache.get(idx);
  if (cached) { applyServerInfo(idx, cached); return; }
  renderPanel(idx, localInfo(idx), { loading: true, note: 'Загружаю карточку…', noteInfo: true });
  requestDraw();
  loadNode(idx);
}
function applyServerInfo(idx, info) {
  if (info.title) FULL.set(idx, info.title);
  (info.neighbors || []).forEach(function (n) { if (n.t && n.i >= 0 && n.i < N) FULL.set(n.i, n.t); });
  const nbIdx = (info.neighbors || []).map(function (n) { return n.i; });
  setHotFocus(idx, nbIdx);
  revealRows(idx, (info.neighbors || []).filter(function (n) { return n.rel === 'row' || n.rel === 'related'; }).map(function (n) { return n.i; }));
  renderPanel(idx, info);
  requestDraw();
}
async function loadNode(idx) {
  const ticket = ++detailTicket;
  if (detailAbort) detailAbort.abort();
  detailAbort = new AbortController();
  try {
    const resp = await fetch(DATA_URL + '/node/' + idx + '?v=' + encodeURIComponent(VER), { signal: detailAbort.signal });
    if (ticket !== detailTicket || selected !== idx) return;
    if (resp.status === 409) { markStale(); throw Object.assign(new Error('Граф обновился на сервере, номера карточек изменились.'), { stale: true }); }
    if (!resp.ok) throw new Error('Сервер вернул ' + resp.status + ' для /node/' + idx);
    const info = await resp.json();
    if (ticket !== detailTicket || selected !== idx) return;
    if (infoCache.size > 300) infoCache.clear();
    infoCache.set(idx, info);
    applyServerInfo(idx, info);
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    if (ticket !== detailTicket || selected !== idx) return;
    const items = localNeighborItems(idx);
    const ids = items.map(function (it) { return it.i; }).filter(function (j) { return !titleOf(j); }).slice(0, 120);
    const actions = '<div><button class="btn" data-act="retry">Повторить</button>' + (err && err.stale ? '<button class="btn" data-act="reload">Обновить граф</button>' : '') + '</div>';
    renderPanel(idx, localInfo(idx), { note: 'Не удалось загрузить полную карточку: ' + ((err && err.message) || 'ошибка сети'), actions: actions });
    if (ids.length && DATA_URL && !(err && err.stale)) {
      ids.forEach(function (j) { tried[j] = 0; });
      scheduleTitles(ids, function () {
        if (selected === idx) renderPanel(idx, localInfo(idx), { note: 'Полная карточка недоступна, показаны данные из карты.', actions: actions });
      });
    }
  }
}
function closeDetail() {
  $('detail-panel').style.display = 'none';
  selected = -1; detailTicket++;
  if (detailAbort) detailAbort.abort();
  rowPos.clear();
  restoreSearchHot();
  requestDraw();
}
function focusNode(idx) {
  if (idx < 0 || idx >= N) return;
  if (G[idx] === 1) { tagsOn = true; $('btn-tags').classList.add('active'); }
  let target = idx;
  if (G[idx] === 2) {
    const nb = localNeighbors(idx);
    for (let k = 0; k < nb.length; k++) if (G[nb[k][0]] !== 2) { target = nb[k][0]; break; }
  }
  flyTo(X[target], Y[target], Math.max(zoom, 0.9));
  showDetail(idx);
}

/* ---------- поиск ---------- */
const searchInput = $('searchInput'), resultsBox = $('search-results');
let searchTimer = 0, searchTicket = 0, searchAbort = null, resultItems = [], resultSel = -1;
function ensureHay() {
  if (hayLocal) return;
  hayLocal = new Array(N);
  for (let i = 0; i < N; i++) {
    if (G[i] === 2) { hayLocal[i] = ''; continue; }
    const m = META[i];
    let s = (L[i] || '') + ' ' + projectOf(i);
    if (m && m.length >= 6) s += ' ' + [m[0], m[2], m[4], m[5]].join(' ');
    hayLocal[i] = s.toLowerCase().replace(/ё/g, 'е');
  }
}
function localSearch(q) {
  ensureHay();
  const toks = q.toLowerCase().replace(/ё/g, 'е').split(/\s+/).filter(Boolean);
  const ids = [];
  for (let i = 0; i < N && ids.length < 5000; i++) {
    const h = hayLocal[i];
    if (!h) continue;
    let ok = true;
    for (let t = 0; t < toks.length; t++) if (h.indexOf(toks[t]) < 0) { ok = false; break; }
    if (ok) ids.push(i);
  }
  const ranked = ids.slice(0, 600).sort(function (a, b) {
    const sa = (L[a] || '').toLowerCase().indexOf(toks[0]) === 0 ? 0 : 1, sb = (L[b] || '').toLowerCase().indexOf(toks[0]) === 0 ? 0 : 1;
    return sa - sb || R[b] - R[a];
  });
  return { total: ids.length, ids: ids, items: ranked.slice(0, 40).map(function (i) { return { i: i, t: titleOf(i) || ('Карточка #' + i), p: projectOf(i), k: typeOf(i) || (G[i] === 1 ? 'tag' : '') }; }) };
}
function renderResults(res, note) {
  resultItems = res.items || [];
  resultSel = resultItems.length ? 0 : -1;
  if (!resultItems.length) {
    resultsBox.innerHTML = '<div class="sr-head">' + esc(note || 'Ничего не найдено') + '</div>';
    resultsBox.style.display = 'block';
    return;
  }
  let html = '<div class="sr-head">Найдено: ' + res.total.toLocaleString('ru-RU') + (res.capped ? '+' : '') + (note ? ' · ' + esc(note) : '') + '</div>';
  resultItems.forEach(function (it, k) {
    const sub = [TYPE_RU[it.k] || '', it.p].filter(Boolean).join(' · ');
    html += '<div class="sr-item' + (k === resultSel ? ' sel' : '') + '" data-k="' + k + '"><b>' + esc(it.t || ('Карточка #' + it.i)) + '</b>' + (sub ? '<span>' + esc(sub) + '</span>' : '') + '</div>';
  });
  resultsBox.innerHTML = html;
  resultsBox.style.display = 'block';
}
function applyHits(ids) {
  searchHits = ids.slice(0, 5000);
  if (selected < 0) restoreSearchHot();
}
function clearSearch() {
  clearTimeout(searchTimer); searchTicket++;
  if (searchAbort) searchAbort.abort();
  searchHits = []; resultItems = [];
  resultsBox.style.display = 'none';
  if (selected >= 0) setHotFocus(selected); else clearHot();
  requestDraw();
}
function openResult(k) {
  const it = resultItems[k];
  if (!it) return;
  resultsBox.style.display = 'none';
  focusNode(it.i);
}
async function runSearch(q) {
  const ticket = ++searchTicket;
  const bigServer = DATA_URL && !(N < _FULL_META_N && META.length === N);
  const local = localSearch(q);
  if (ticket !== searchTicket) return;
  renderResults(local, bigServer ? 'ищу на сервере…' : '');
  applyHits(local.ids);
  if (local.ids.length && selected < 0) frameIds(local.ids);
  requestDraw();
  if (!bigServer) return;
  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();
  try {
    const resp = await fetch(DATA_URL + '/search?v=' + encodeURIComponent(VER) + '&q=' + encodeURIComponent(q), { signal: searchAbort.signal });
    if (ticket !== searchTicket) return;
    if (resp.status === 409) { markStale(); return; }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const res = await resp.json();
    if (ticket !== searchTicket) return;
    (res.items || []).forEach(function (it) { if (it.t) FULL.set(it.i, it.t); });
    renderResults(res, '');
    applyHits(res.ids || []);
    if ((res.ids || []).length && selected < 0) frameIds(res.ids);
    requestDraw();
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    renderResults(local, 'серверный поиск недоступен, показаны результаты по подписям');
  }
}
const _FULL_META_N = 25000;
searchInput.addEventListener('input', function () {
  clearTimeout(searchTimer);
  const v = searchInput.value.trim();
  if (!v) { clearSearch(); return; }
  searchTimer = setTimeout(function () { runSearch(v); }, v.length < 3 ? 260 : 170);
});
searchInput.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') { e.preventDefault(); if (resultSel >= 0) openResult(resultSel); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!resultItems.length) return;
    e.preventDefault();
    resultSel = (resultSel + (e.key === 'ArrowDown' ? 1 : -1) + resultItems.length) % resultItems.length;
    Array.prototype.forEach.call(resultsBox.querySelectorAll('.sr-item'), function (el, k) { el.classList.toggle('sel', k === resultSel); });
  } else if (e.key === 'Escape') { searchInput.value = ''; clearSearch(); searchInput.blur(); }
});
resultsBox.addEventListener('click', function (e) {
  const el = e.target.closest('.sr-item');
  if (el) openResult(+el.getAttribute('data-k'));
});

/* ---------- навигация по структуре ---------- */
function buildNav() {
  const box = $('nav-list'), btn = $('btn-nav');
  if (!REGIONS.length) { btn.style.display = 'none'; box.style.display = 'none'; return; }
  const items = REGIONS.map(function (g, k) { return { g: g, k: k }; }).sort(function (a, b) { return a.g.lv - b.g.lv || b.g.n - a.g.n; }).slice(0, 80);
  let html = '', lastLv = -1;
  items.forEach(function (it) {
    if (it.g.lv !== lastLv) { lastLv = it.g.lv; html += '<div class="nav-head">' + (lastLv === 0 ? 'Проекты' : 'Крупные ветки') + '</div>'; }
    html += '<div class="nav-item" data-k="' + it.k + '"><span class="nav-dot" style="background:' + PAL[it.g.c % PAL.length][0] + '"></span><span class="nav-name">' + esc(it.g.name) + '</span><span class="nav-n">' + it.g.n.toLocaleString('ru-RU') + '</span></div>';
  });
  box.innerHTML = html;
}
$('nav-list').addEventListener('click', function (e) {
  const el = e.target.closest('.nav-item');
  if (el) flyToRegion(+el.getAttribute('data-k'));
});
$('btn-nav').addEventListener('click', function () {
  const box = $('nav-list'), on = box.style.display !== 'block';
  box.style.display = on ? 'block' : 'none';
  $('btn-nav').classList.toggle('active', on);
});
$('btn-fit').addEventListener('click', function () { fitted = false; fit(); const z = zoom, x = camX, y = camY; zoom = z; flyTo(x, y, z); });
$('btn-tags').addEventListener('click', function () { tagsOn = !tagsOn; this.classList.toggle('active', tagsOn); requestDraw(); });
$('stale-reload').addEventListener('click', function () { location.reload(); });

/* ---------- ввод: pointer events + колесо ---------- */
const ptrs = new Map();
let downX = 0, downY = 0, moved = false, lastX = 0, lastY = 0, pinchDist = 0, pinchGesture = false;
function localPoint(e) { const r = wrap.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
wrap.addEventListener('pointerdown', function (e) {
  try { wrap.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
  ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  flyId++;
  if (ptrs.size === 1) { downX = lastX = e.clientX; downY = lastY = e.clientY; moved = false; }
  else if (ptrs.size === 2) {
    moved = true;
    const a = Array.from(ptrs.values());
    pinchDist = Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y);
  }
});
wrap.addEventListener('pointermove', function (e) {
  const lp = localPoint(e);
  if (!ptrs.has(e.pointerId)) {
    if (e.pointerType === 'mouse') onHover(lp[0], lp[1]);
    return;
  }
  const prev = ptrs.get(e.pointerId);
  ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (ptrs.size === 2) {
    const a = Array.from(ptrs.values()), d = Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y);
    const r = wrap.getBoundingClientRect(), mx = (a[0].x + a[1].x) / 2 - r.left, my = (a[0].y + a[1].y) / 2 - r.top;
    if (pinchDist > 0) applyZoomAt(mx, my, d / pinchDist);
    pinchDist = d;
    camX -= (e.clientX - prev.x) / 2 / zoom; camY -= (e.clientY - prev.y) / 2 / zoom;
    requestDraw();
    return;
  }
  if (!moved && Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) > 6) { moved = true; hideTip(); }
  if (moved) {
    camX -= (e.clientX - lastX) / zoom; camY -= (e.clientY - lastY) / zoom;
    wrap.style.cursor = 'grabbing'; requestDraw();
  }
  lastX = e.clientX; lastY = e.clientY;
});
function endPointer(e, cancelled) {
  const had = ptrs.has(e.pointerId), wasSingle = ptrs.size === 1;
  ptrs.delete(e.pointerId);
  wrap.style.cursor = 'grab';
  if (!had || cancelled || !wasSingle || moved) { if (ptrs.size === 1) { const r = Array.from(ptrs.values())[0]; lastX = r.x; lastY = r.y; } return; }
  const lp = localPoint(e);
  const i = hit(lp[0], lp[1]);
  if (i >= 0) { showDetail(i); return; }
  const rg = hitRegion(lp[0], lp[1]);
  if (rg >= 0) { const g = REGIONS[rg], z = zoomForRadius(g.r); if (z > zoom * 1.15) { flyToRegion(rg); return; } }
  if (selected >= 0) closeDetail();
}
wrap.addEventListener('pointerup', function (e) { endPointer(e, false); });
wrap.addEventListener('pointercancel', function (e) { endPointer(e, true); });
wrap.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse' && !ptrs.size) { hoverNode = -1; if (hoverRegion >= 0) { hoverRegion = -1; requestDraw(); } hideTip(); } });
function onHover(lx, ly) {
  const i = hit(lx, ly);
  const rg = i < 0 ? hitRegion(lx, ly) : -1;
  if (i === hoverNode && rg === hoverRegion) { if (tip.style.display === 'block') placeTip(lx, ly); return; }
  const regionChanged = rg !== hoverRegion;
  hoverNode = i; hoverRegion = rg;
  wrap.style.cursor = i >= 0 ? 'pointer' : (rg >= 0 ? 'zoom-in' : 'grab');
  if (i >= 0) showNodeTip(i, lx, ly); else if (rg >= 0) showRegionTip(rg, lx, ly); else hideTip();
  if (regionChanged) requestDraw();
}
wrap.addEventListener('wheel', function (e) {
  e.preventDefault();
  if (pinchGesture) return;
  let dy = e.deltaY;
  if (e.deltaMode === 1) dy *= 16; else if (e.deltaMode === 2) dy *= 400;
  if (!dy) return;
  const abs = Math.abs(dy), pinch = e.ctrlKey || e.metaKey;
  const gain = pinch ? 0.012 : (abs < 50 ? 0.01 : 0.0016);
  const lp = localPoint(e);
  flyId++;
  applyZoomAt(lp[0], lp[1], Math.max(0.82, Math.min(1.22, Math.exp(-dy * gain))));
}, { passive: false });
wrap.addEventListener('gesturestart', function (e) { e.preventDefault(); pinchGesture = true; wrap._gScale = e.scale || 1; }, { passive: false });
wrap.addEventListener('gesturechange', function (e) {
  e.preventDefault();
  if (ptrs.size >= 2) return;
  const prev = wrap._gScale || 1, next = e.scale || prev;
  wrap._gScale = next;
  if (!(prev > 0) || next === prev) return;
  const lp = localPoint(e);
  applyZoomAt(lp[0], lp[1], next / prev);
}, { passive: false });
wrap.addEventListener('gestureend', function () { wrap._gScale = 1; setTimeout(function () { pinchGesture = false; }, 60); });
wrap.addEventListener('dblclick', function (e) {
  const lp = localPoint(e), i = hit(lp[0], lp[1]);
  if (i >= 0) focusNode(i); else applyZoomAt(lp[0], lp[1], 2);
});
window.addEventListener('keydown', function (e) {
  if (e.key === '/' && document.activeElement !== searchInput) { e.preventDefault(); searchInput.focus(); searchInput.select(); }
  else if (e.key === 'Escape' && document.activeElement !== searchInput) { if (selected >= 0) closeDetail(); }
});
$('detail-close').addEventListener('click', closeDetail);
$('detail-content').addEventListener('click', function (e) {
  const nb = e.target.closest('.neighbor-item');
  if (nb) { focusNode(+nb.getAttribute('data-i')); return; }
  const tg = e.target.closest('.chip.tag');
  if (tg) { searchInput.value = tg.getAttribute('data-tag'); runSearch(searchInput.value); return; }
  const act = e.target.closest('button[data-act]');
  if (act) {
    if (act.getAttribute('data-act') === 'reload') location.reload();
    else if (selected >= 0) { infoCache.delete(selected); showDetail(selected); }
  }
});

/* ---------- тема ---------- */
const themeBtn = $('theme-toggle');
const savedTheme = localStorage.getItem('theme') || localStorage.getItem('exocortex_theme') || 'light';
if (savedTheme === 'dark') { document.body.classList.remove('light-theme'); themeBtn.textContent = 'Светлая тема'; }
themeBtn.addEventListener('click', function () {
  const light = document.body.classList.toggle('light-theme');
  themeBtn.textContent = light ? 'Темная тема' : 'Светлая тема';
  localStorage.setItem('exocortex_theme', light ? 'light' : 'dark');
  localStorage.setItem('theme', light ? 'light' : 'dark');
  requestDraw();
});
window.addEventListener('resize', resize);

/* ---------- загрузка данных ---------- */
function setLoad(pct, caption) {
  const n = Math.max(0, Math.min(100, Math.round(pct)));
  const el = $('load-pct'), arc = $('load-arc'), cap = $('load-caption');
  if (el) el.textContent = n + '%';
  if (cap && caption) cap.textContent = caption;
  if (arc) { arc.classList.toggle('spin', n <= 0); arc.style.strokeDashoffset = n <= 0 ? '132' : String(175.9 * (1 - n / 100)); }
}
function paintLoad() { return new Promise(function (r) { requestAnimationFrame(function () { setTimeout(r, 0); }); }); }
async function inflateB64(b64) {
  const bin = Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
  if (typeof DecompressionStream !== 'function') throw new Error('Браузер не поддерживает DecompressionStream');
  const ds = new DecompressionStream('gzip');
  const buf = await new Response(new Blob([bin]).stream().pipeThrough(ds)).arrayBuffer();
  return JSON.parse(new TextDecoder().decode(buf));
}
function parseBinary(raw) {
  if (raw.byteOffset & 3) raw = raw.slice();
  const buf = raw.buffer, base = raw.byteOffset;
  const view = new DataView(buf, base, raw.byteLength);
  if (view.getUint8(0) !== 90 || view.getUint8(1) !== 71 || view.getUint8(2) !== 66 || view.getUint8(3) !== 50) throw new Error('Неизвестный формат графа (ожидается ZGB2)');
  let o = 4;
  const rd = function () { const v = view.getUint32(o, true); o += 4; return v; };
  const n = rd(), e = rd(), nlab = rd(), nproj = rd(), flags = rd(), tz = rd(), te = rd(), tr = rd(), nreg = rd();
  const x = new Float32Array(buf, base + o, n); o += n * 4;
  const y = new Float32Array(buf, base + o, n); o += n * 4;
  const r = new Uint8Array(buf, base + o, n); o += n;
  const g = new Uint8Array(buf, base + o, n); o += n;
  const c = new Uint8Array(buf, base + o, n); o += n;
  o = (o + 3) & ~3;
  const ea = new Uint32Array(buf, base + o, e); o += e * 4;
  const eb = new Uint32Array(buf, base + o, e); o += e * 4;
  const ek = new Uint8Array(buf, base + o, e); o += e;
  const dec = new TextDecoder();
  let pi = null, pn = null;
  if (flags & 1) {
    if (o & 1) o += 1;
    pi = new Uint16Array(buf, base + o, n); o += n * 2;
    pn = new Array(nproj);
    for (let i = 0; i < nproj; i++) { const len = view.getUint16(o, true); o += 2; pn[i] = dec.decode(new Uint8Array(buf, base + o, len)); o += len; }
  }
  const labels = new Array(n);
  for (let i = 0; i < nlab; i++) {
    const index = view.getUint32(o, true), len = view.getUint16(o + 4, true); o += 6;
    labels[index] = dec.decode(new Uint8Array(buf, base + o, len)); o += len;
  }
  const regions = [];
  for (let i = 0; i < nreg; i++) {
    const len = view.getUint16(o + 18, true);
    regions.push({ x: view.getFloat32(o, true), y: view.getFloat32(o + 4, true), r: view.getFloat32(o + 8, true), n: view.getUint32(o + 12, true), c: view.getUint8(o + 16), lv: view.getUint8(o + 17), name: '' });
    o += 20;
    regions[i].name = dec.decode(new Uint8Array(buf, base + o, len)); o += len;
  }
  return { n: n, x: x, y: y, r: r, g: g, c: c, l: labels, m: [], ea: ea, eb: eb, ek: ek, tz: tz, te: te, tr: tr, pi: pi, pn: pn, rg: regions };
}
async function applyPayload(data) {
  N = data.n || 0;
  X = data.x instanceof Float32Array ? data.x : Float32Array.from(data.x || []);
  Y = data.y instanceof Float32Array ? data.y : Float32Array.from(data.y || []);
  R = data.r instanceof Uint8Array ? data.r : Uint8Array.from(data.r || []);
  G = data.g instanceof Uint8Array ? data.g : Uint8Array.from(data.g || []);
  C = data.c instanceof Uint8Array ? data.c : Uint8Array.from(data.c || []);
  L = data.l || []; META = data.m || [];
  if (data.ea) { EA = data.ea; EB = data.eb; EK = data.ek; }
  else {
    const f = data.e || [], m = (f.length / 3) | 0;
    EA = new Uint32Array(m); EB = new Uint32Array(m); EK = new Uint8Array(m);
    for (let i = 0; i < m; i++) { EA[i] = f[i * 3]; EB[i] = f[i * 3 + 1]; EK[i] = f[i * 3 + 2]; }
  }
  PINDEX = data.pi ? (data.pi instanceof Uint16Array ? data.pi : Uint16Array.from(data.pi)) : null;
  PNAMES = data.pn || null;
  REGIONS = (data.rg || []).map(function (g) {
    return Array.isArray(g) ? { name: String(g[0]), x: g[1], y: g[2], r: g[3], n: g[4], c: g[5], lv: g[6] } : g;
  });
  if (data.ver) VER = data.ver;
  $('stat-z').textContent = data.tz || 0; $('stat-e').textContent = data.te || 0; $('stat-r').textContent = data.tr || 0;
  setLoad(96, 'Строю индекс'); await paintLoad();
  hot = new Uint8Array(N); hotList = []; searchHits = []; rowPos.clear();
  stamp = new Uint32Array(N); stampId = 0; tried = new Uint8Array(N);
  hayLocal = null; infoCache.clear(); FULL.clear();
  tagsOn = N < 800 && (EA ? EA.length : 0) < 2500;
  $('btn-tags').classList.toggle('active', tagsOn);
  buildLod(); buildGrid(); buildAdj(); buildNav();
  fitted = false; clearHot();
  $('load-hint') && $('load-hint').remove();
  resize();
}
async function readGraph(resp) {
  VER = resp.headers.get('x-graph-version') || '';
  const total = Number(resp.headers.get('x-graph-size') || resp.headers.get('content-length') || 0);
  if (!resp.body || !resp.body.getReader) { setLoad(30, 'Загрузка графа'); return resp.json(); }
  const reader = resp.body.getReader(), chunks = [];
  let got = 0;
  setLoad(4, 'Передача графа');
  for (;;) {
    const step = await reader.read();
    if (step.done) break;
    chunks.push(step.value); got += step.value.length;
    const ratio = total > 0 ? got / total : Math.min(0.85, 0.08 + Math.log10(got + 10) / 8);
    setLoad(Math.min(90, ratio * 90), 'Загрузка графа');
  }
  setLoad(93, 'Сборка графа'); await paintLoad();
  const buf = new Uint8Array(got);
  let off = 0;
  for (let i = 0; i < chunks.length; i++) { buf.set(chunks[i], off); off += chunks[i].length; }
  if (buf.length >= 4 && buf[0] === 90 && buf[1] === 71 && buf[2] === 66) return parseBinary(buf);
  return JSON.parse(new TextDecoder().decode(buf));
}
(async function boot() {
  try {
    let data;
    if (DATA_URL) {
      setLoad(0, 'Сервер собирает граф'); await paintLoad();
      const resp = await fetch(DATA_URL, { headers: { 'Accept': 'application/octet-stream, application/json' } });
      if (!resp.ok) throw new Error('Не удалось загрузить граф (HTTP ' + resp.status + ')');
      data = await readGraph(resp);
    } else if (DATA_B64) {
      setLoad(20, 'Загрузка графа'); await paintLoad();
      data = await inflateB64(DATA_B64);
    } else {
      data = { n: 0, x: [], y: [], r: [], g: [], c: [], l: [], m: [], e: [], tz: 0, te: 0, tr: 0 };
    }
    await applyPayload(data);
  } catch (err) {
    setLoad(1, 'Ошибка загрузки графа: ' + (err && err.message ? err.message : err));
  }
})();
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
    back_href = html_escape((back_url or "").strip())
    back_text = html_escape((back_label or "← В проект").strip())
    back_html = f'<a id="graph-back" href="{back_href}">{back_text}</a>' if back_href else ""
    data_b64 = ""
    total_z = total_e = total_r = 0
    if not data_url and graph_data:
        payload = pack_graph_payload(graph_data)
        total_z, total_e, total_r = payload["tz"], payload["te"], payload["tr"]
        data_b64 = base64.b64encode(gzip.compress(dumps_payload(payload), compresslevel=4)).decode("ascii")
    html = (
        _HTML_TEMPLATE
        .replace("__CSS__", _GRAPH_CSS)
        .replace("__TITLE__", heading)
        .replace("__BACK__", back_html)
        .replace("__TZ__", str(total_z))
        .replace("__TE__", str(total_e))
        .replace("__TR__", str(total_r))
    )
    script = _GRAPH_JS_TEMPLATE.replace("__DATA_URL__", json.dumps(data_url or "")).replace("__DATA_B64__", json.dumps(data_b64))
    return html + script


def render_graph_html(
    graph_data: Optional[Dict[str, Any]] = None,
    user_label: str = "",
    data_url: Optional[str] = None,
    back_url: str = "",
    back_label: str = "← В проект",
) -> str:
    """Собирает HTML-граф. data_url — базовый адрес API снимка (см. GraphSnapshot)."""
    return _build_html(graph_data, user_label=user_label, data_url=data_url, back_url=back_url, back_label=back_label)


def _write_html(html_content: str, output_path: str | None) -> str:
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".html", prefix="graph_")
        os.close(fd)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return output_path


def generate_graph_html(user_id: str, output_path: str | None = None) -> str:
    repo = ZettelRepository(Neo4jClient())
    return _write_html(render_graph_html(repo.export_graph_data(user_id), user_label=user_id), output_path)


def generate_graph_html_from_data(graph_data: dict, user_label: str = "", output_path: str | None = None) -> str:
    return _write_html(render_graph_html(graph_data, user_label=user_label), output_path)


def generate_graph_html_from_repo(repo: ZettelRepository, user_id: str, output_path: str | None = None) -> str:
    return _write_html(render_graph_html(repo.export_graph_data(user_id), user_label=user_id), output_path)