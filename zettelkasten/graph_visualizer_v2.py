"""
Интерактивный граф знаний v2: Canvas 2D на CPU.
Самодостаточный пайплайн: раскладка, упаковка данных, HTML и gzip.
"""
from __future__ import annotations

import array
import base64
import gzip
import json
import math
import os
import re
import struct
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


_XU = 52.0
_YU = 36.0
_CHAIN = 5
_WRAP = 5


def _absorb_chain(lid: str, kids_of) -> tuple[list[str], list[str]]:
    """Прямая цепочка от узла до первого ветвления."""
    nodes: list[str] = []
    seen: set[str] = set()
    current = lid
    tail: list[str] = []
    while current and current not in seen:
        seen.add(current)
        nodes.append(current)
        kids = [child for child in kids_of(current) if child not in seen]
        if len(kids) != 1:
            tail = kids
            break
        current = kids[0]
    return nodes, tail


def _pack_size(shapes: list[tuple[float, float]]) -> tuple[float, float]:
    total_w = 0.0
    total_h = 0.0
    row_w = 0.0
    row_h = 0.0
    filled = 0
    for width, height in shapes:
        if filled == _WRAP:
            total_w = max(total_w, row_w)
            total_h += row_h
            row_w = 0.0
            row_h = 0.0
            filled = 0
        row_w += width
        row_h = max(row_h, height)
        filled += 1
    total_w = max(total_w, row_w)
    total_h += row_h
    return total_w, total_h


def _block_size(lid: str, kids_of, memo: dict[str, tuple[float, float]]) -> tuple[float, float]:
    nodes, tail = _absorb_chain(lid, kids_of)
    cols = max(1, math.ceil(len(nodes) / _CHAIN))
    rows = min(len(nodes), _CHAIN)
    width = cols * _XU
    height = rows * _YU
    if tail:
        shapes = [memo.get(child, (_XU, _YU)) for child in tail]
        child_w, child_h = _pack_size(shapes)
        width = max(width, child_w)
        height += child_h
    return width, height


def _subtree_spans(order: list[str], kids_of, x_unit: float, y_gap: float) -> tuple[dict[str, float], dict[str, float]]:
    """Родитель сверху, дети снизу. Длинная цепочка ложится короткими столбцами."""
    memo: dict[str, tuple[float, float]] = {}
    color: dict[str, int] = {}
    for start in order:
        if color.get(start) == 2:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            lid, expanded = stack.pop()
            if expanded:
                memo[lid] = _block_size(lid, kids_of, memo)
                color[lid] = 2
                continue
            if color.get(lid) == 2:
                continue
            if color.get(lid) == 1:
                memo[lid] = (_XU, _YU)
                color[lid] = 2
                continue
            color[lid] = 1
            stack.append((lid, True))
            for child in reversed(kids_of(lid)):
                if color.get(child, 0) == 0:
                    stack.append((child, False))
    width = {lid: memo.get(lid, (_XU, _YU))[0] for lid in order}
    height = {lid: memo.get(lid, (_XU, _YU))[1] for lid in order}
    return width, height


def _place_tree(
    root: str,
    x_left: float,
    y: float,
    kids_of,
    width: dict[str, float],
    height: dict[str, float],
    x_unit: float,
    y_gap: float,
    luhmann_to_id: dict[str, str],
    pos: dict,
) -> None:
    """Сверху родитель, под ним дети слева направо. Цепочка читается сверху вниз."""
    guard: set[str] = set()

    def place(lid: str, left: float, top: float) -> None:
        if lid in guard:
            return
        nodes, tail = _absorb_chain(lid, kids_of)
        block_w = width.get(lid, _XU)
        cols = max(1, math.ceil(len(nodes) / _CHAIN))
        rows = min(len(nodes), _CHAIN)
        chain_w = cols * _XU
        origin_x = left + max(0.0, (block_w - chain_w) / 2.0)
        for index, node in enumerate(nodes):
            if node in guard:
                continue
            guard.add(node)
            col = index // _CHAIN
            row = index % _CHAIN
            zid = luhmann_to_id.get(node)
            if zid:
                pos[zid] = (origin_x + col * _XU + _XU / 2.0, top + row * _YU)
        if not tail:
            return
        shapes = [(width.get(child, _XU), height.get(child, _YU)) for child in tail]
        pack_w, _pack_h = _pack_size(shapes)
        cursor_x = left + max(0.0, (block_w - pack_w) / 2.0)
        cursor_y = top + rows * _YU
        row_h = 0.0
        filled = 0
        origin_row = cursor_x
        for child, (child_w, child_h) in zip(tail, shapes):
            if filled == _WRAP:
                cursor_y += row_h
                cursor_x = origin_row
                row_h = 0.0
                filled = 0
            place(child, cursor_x, cursor_y)
            cursor_x += child_w
            row_h = max(row_h, child_h)
            filled += 1

    place(root, x_left, y)


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


def _scatter_islands(sizes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Пятна проектов: крупное в центре, остальные кольцом, без наложения."""
    gap = 220.0
    if not sizes:
        return []
    if len(sizes) == 1:
        width, height = sizes[0]
        return [(-width / 2.0, -height / 2.0)]
    first_w, first_h = sizes[0]
    first_half = 0.5 * math.hypot(first_w, first_h)
    rest = sizes[1:]
    max_rest = max(0.5 * math.hypot(width, height) for width, height in rest)
    ring = first_half + max_rest + gap
    count = len(rest)
    if count > 1:
        need = (2.0 * max_rest + gap) / (2.0 * math.sin(math.pi / count))
        ring = max(ring, need)
    origins = [(-first_w / 2.0, -first_h / 2.0)]
    for index, (width, height) in enumerate(rest):
        ang = 0.0 if count == 1 else (-math.pi / 2 + 2 * math.pi * index / count)
        cx = ring * math.cos(ang)
        cy = ring * math.sin(ang) * 0.92
        origins.append((cx - width / 2.0, cy - height / 2.0))
    return origins


def _scatter_origins(sizes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Подсолнух: у края то же расстояние между ветками, что и в центре."""
    golden = math.pi * (3 - math.sqrt(5))
    if not sizes:
        return []
    extents = sorted(0.5 * math.hypot(width, height) for width, height in sizes)
    typical = extents[len(extents) // 2]
    gap = min(max(typical * 1.05, 88.0), 360.0)
    half0 = 0.5 * math.hypot(sizes[0][0], sizes[0][1])
    origins: list[tuple[float, float]] = []
    for index, (width, height) in enumerate(sizes):
        if index == 0:
            cx = cy = 0.0
        else:
            radius = half0 * 0.82 + gap * math.sqrt(index)
            ang = index * golden
            cx = radius * math.cos(ang)
            cy = radius * math.sin(ang) * 0.94
        origins.append((cx - width / 2.0, cy - height / 2.0))
    return origins


_LEAF_R = 12.0
_FAMILY_MIN = 600


def _family_enclose(child_radii: list[float]) -> float:
    """Плотный круг: дети рядом с родителем, без пустого кольца."""
    if not child_radii:
        return _LEAF_R
    area = 0.0
    max_r = _LEAF_R
    for radius in child_radii:
        padded = radius + 3.5
        area += padded * padded
        if padded > max_r:
            max_r = padded
    return max(math.sqrt(area) * 0.92, max_r + 10.0)


def _place_families(
    members: list[str],
    by_zid: dict,
    center: tuple[float, float],
) -> dict[str, tuple[float, float]]:
    """Каждая ветка — плотная семья: родитель в середине, дети вокруг."""
    zid_of: dict[str, str] = {}
    lid_of: dict[str, str] = {}
    for zid in members:
        zettel = by_zid.get(zid)
        if not zettel:
            continue
        lid = str(zettel.get("luhmann_id") or zid)
        lid_of[zid] = lid
        zid_of[lid] = zid
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for zid, lid in lid_of.items():
        parent = str((by_zid[zid].get("parent_luhmann") or ""))
        if parent and parent in zid_of and parent != lid:
            children[parent].append(lid)
        else:
            roots.append(lid)
    if not roots:
        roots = list(zid_of)

    radius: dict[str, float] = {}
    color: dict[str, int] = {}
    stack: list[tuple[str, bool]] = [(lid, False) for lid in roots]
    while stack:
        lid, expanded = stack.pop()
        if expanded:
            kids = children.get(lid) or []
            radius[lid] = _family_enclose([radius.get(kid, _LEAF_R) for kid in kids])
            kids.sort(key=lambda kid: radius.get(kid, _LEAF_R))
            color[lid] = 2
            continue
        state = color.get(lid, 0)
        if state == 2:
            continue
        if state == 1:
            radius[lid] = _LEAF_R
            color[lid] = 2
            continue
        color[lid] = 1
        stack.append((lid, True))
        for kid in reversed(children.get(lid) or []):
            if color.get(kid, 0) == 0:
                stack.append((kid, False))
    for lid in zid_of:
        if lid not in radius:
            roots.append(lid)
            radius[lid] = _LEAF_R

    roots.sort(key=lambda lid: radius.get(lid, _LEAF_R), reverse=True)
    golden = math.pi * (3 - math.sqrt(5))
    anchors: dict[str, tuple[float, float]] = {}
    count = len(roots)
    if count == 1:
        anchors[roots[0]] = center
    else:
        area = sum((radius.get(lid, _LEAF_R) + 3.5) ** 2 for lid in roots)
        outer = math.sqrt(area) * 0.92
        for index, lid in enumerate(roots):
            own = radius.get(lid, _LEAF_R)
            span = max(outer - own, 14.0)
            dist = math.sqrt((index + 0.5) / count) * span
            ang = index * golden
            anchors[lid] = (
                center[0] + dist * math.cos(ang),
                center[1] + dist * math.sin(ang) * 0.96,
            )

    pos: dict[str, tuple[float, float]] = {}
    place: list[tuple[str, float, float]] = [
        (lid, anchors[lid][0], anchors[lid][1]) for lid in roots if lid in anchors
    ]
    while place:
        lid, x, y = place.pop()
        zid = zid_of.get(lid)
        if zid and zid not in pos:
            pos[zid] = (x, y)
        kids = children.get(lid) or []
        n = len(kids)
        if not n:
            continue
        outer = radius.get(lid, _LEAF_R)
        for index, kid in enumerate(kids):
            own = radius.get(kid, _LEAF_R)
            span = max(outer - own, 12.0)
            dist = max(12.0, math.sqrt((index + 0.5) / n) * span)
            ang = (index + 1) * golden
            place.append((
                kid,
                x + dist * math.cos(ang),
                y + dist * math.sin(ang) * 0.96,
            ))
    return pos


def _subtree_members(root: str, children: dict[str, list[str]], zid_of: dict[str, str]) -> list[str]:
    members: list[str] = []
    stack = [root]
    seen: set[str] = set()
    while stack:
        lid = stack.pop()
        if lid in seen:
            continue
        seen.add(lid)
        zid = zid_of.get(lid)
        if zid:
            members.append(zid)
        stack.extend(children.get(lid) or ())
    return members


def _island_centers(radii: list[float]) -> list[tuple[float, float]]:
    """Крупные ветки держатся вместе в центре, мелкие стоят отдельными пятнами вокруг."""
    count = len(radii)
    if count == 0:
        return []
    if count == 1:
        return [(0.0, 0.0)]
    total = sum(radius * radius for radius in radii) or 1.0
    inner_n = 1
    filled = radii[0] * radii[0]
    for index in range(1, count):
        if filled >= total * 0.55 or inner_n >= 5:
            break
        inner_n += 1
        filled += radii[index] * radii[index]
    centers = [(0.0, 0.0)]
    for index in range(1, inner_n):
        ang = -math.pi / 2 + 2 * math.pi * (index - 1) / max(inner_n - 1, 1)
        gap = max(56.0, 0.42 * max(radii[0], radii[index]))
        dist = radii[0] + radii[index] + gap
        centers.append((dist * math.cos(ang), dist * math.sin(ang) * 0.92))
    inner_extent = 0.0
    for (x, y), radius in zip(centers, radii[:inner_n]):
        inner_extent = max(inner_extent, math.hypot(x, y) + radius)
    outer = radii[inner_n:]
    if not outer:
        return centers
    ring_count = max(10, int(2 * math.pi * max(inner_extent, 80.0) / 90.0))
    cursor = 0
    ring = 0
    while cursor < len(outer):
        take = min(ring_count + ring * 6, len(outer) - cursor)
        ring_r = inner_extent + 88.0 + ring * 120.0
        need = take * 78.0 / (2 * math.pi)
        ring_r = max(ring_r, need)
        for index in range(take):
            ang = -math.pi / 2 + 2 * math.pi * index / take + ring * 0.4
            radius = outer[cursor + index]
            dist = ring_r + radius
            centers.append((dist * math.cos(ang), dist * math.sin(ang) * 0.9))
        cursor += take
        ring += 1
    return centers


def _place_branch_islands(
    members: list[str],
    by_zid: dict,
    center: tuple[float, float],
) -> dict[str, tuple[float, float]]:
    """Каждая крупная ветка — отдельное пятно, а не один общий круг."""
    zid_of: dict[str, str] = {}
    lid_of: dict[str, str] = {}
    for zid in members:
        zettel = by_zid.get(zid)
        if not zettel:
            continue
        lid = str(zettel.get("luhmann_id") or zid)
        lid_of[zid] = lid
        zid_of[lid] = zid
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for zid, lid in lid_of.items():
        parent = str(by_zid[zid].get("parent_luhmann") or "")
        if parent and parent in zid_of and parent != lid:
            children[parent].append(lid)
        else:
            roots.append(lid)
    if not roots:
        return _place_families(members, by_zid, center)
    attach_hub = ""
    islands = roots
    if len(roots) == 1:
        kids = children.get(roots[0]) or []
        branched = [kid for kid in kids if children.get(kid)]
        if len(branched) >= 2 or len(kids) >= 8:
            attach_hub = roots[0]
            islands = list(kids)
    packed: list[tuple[dict[str, tuple[float, float]], float]] = []
    for lid in islands:
        group = _subtree_members(lid, children, zid_of)
        if len(group) <= 1:
            local = {group[0]: (0.0, 0.0)} if group else {}
            packed.append((local, 18.0))
            continue
        local = _place_families(group, by_zid, (0.0, 0.0))
        radius = 18.0
        for zid, point in local.items():
            radius = max(radius, math.hypot(point[0], point[1]) + 14.0)
        packed.append((local, radius))
    packed.sort(key=lambda item: len(item[0]), reverse=True)
    centers = _island_centers([radius for _local, radius in packed])
    pos: dict[str, tuple[float, float]] = {}
    for (local, _radius), (cx, cy) in zip(packed, centers):
        for zid, point in local.items():
            pos[zid] = (center[0] + cx + point[0], center[1] + cy + point[1])
    if attach_hub:
        zid = zid_of.get(attach_hub)
        if zid:
            pos[zid] = center
    return pos


def _spread_like_obsidian(
    pos: Dict[str, Tuple[float, float]],
    zettels: list,
    edges: list,
    excel_zids: set[str],
) -> None:
    """Свободное поле: узлы заполняют круг, связанные держатся рядом."""
    golden = math.pi * (3 - math.sqrt(5))
    groups: dict[str, list[str]] = defaultdict(list)
    for zettel in zettels:
        zid = zettel["zettel_id"]
        if zid not in pos or zid in excel_zids:
            continue
        groups[" ".join((zettel.get("project") or "").split())].append(zid)
    if not groups:
        return
    by_zid = {zettel["zettel_id"]: zettel for zettel in zettels}
    names = sorted(groups, key=lambda name: len(groups[name]), reverse=True)
    radii = [26.0 * math.sqrt(max(len(groups[name]), 1)) + 36.0 for name in names]
    if len(names) == 1:
        centers = [(0.0, 0.0)]
    else:
        boxes = [(radius * 1.15, radius * 1.15) for radius in radii]
        origins = _scatter_islands(boxes)
        centers = [
            (left + width / 2.0, top + height / 2.0)
            for (left, top), (width, height) in zip(origins, boxes)
        ]
    seeded: dict[str, tuple[float, float]] = {}
    used_families = False
    for name, center, radius in zip(names, centers, radii):
        members = groups[name]
        count = len(members)
        if count >= _FAMILY_MIN:
            used_families = True
            seeded.update(_place_branch_islands(members, by_zid, center))
            continue
        for index, zid in enumerate(members):
            if count == 1:
                seeded[zid] = center
                continue
            dist = radius * math.sqrt((index + 0.5) / count)
            ang = index * golden
            seeded[zid] = (
                center[0] + dist * math.cos(ang),
                center[1] + dist * math.sin(ang) * 0.96,
            )
    will_relax = len(seeded) < 30_000 and not used_families
    mentions: dict[str, list[str]] = defaultdict(list)
    links: list[tuple[str, str]] = []
    for edge in edges:
        rel = edge.get("rel_type")
        left, right = edge.get("from_id"), edge.get("to_id")
        if not left or not right or left in excel_zids or right in excel_zids:
            continue
        if rel == "MENTIONS":
            bucket = mentions[str(right)]
            if will_relax or len(bucket) < 64:
                bucket.append(str(left))
            if will_relax:
                links.append((str(left), str(right)))
        elif rel == "CHILD_OF" and will_relax:
            links.append((str(left), str(right)))
    for eid, point in list(pos.items()):
        if not str(eid).startswith("entity:"):
            continue
        pts = [seeded[zid] for zid in mentions.get(str(eid), ()) if zid in seeded]
        if pts:
            ax = sum(p[0] for p in pts) / len(pts)
            ay = sum(p[1] for p in pts) / len(pts)
            seeded[eid] = (ax, ay)
        else:
            seeded[eid] = point
    if will_relax:
        _relax_cloud(seeded, links)
    for zid, xy in seeded.items():
        pos[zid] = xy


def _relax_cloud(seeded: dict[str, tuple[float, float]], links: list[tuple[str, str]]) -> None:
    ids = list(seeded)
    count = len(ids)
    if count < 3:
        return
    index = {zid: i for i, zid in enumerate(ids)}
    xs = [seeded[zid][0] for zid in ids]
    ys = [seeded[zid][1] for zid in ids]
    pairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for left, right in links:
        ia = index.get(left)
        ib = index.get(right)
        if ia is None or ib is None or ia == ib:
            continue
        pair = (ia, ib) if ia < ib else (ib, ia)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    ideal = 40.0
    steps = 34 if count < 900 else (22 if count < 4500 else 12)
    cell = ideal * 1.7
    for step in range(steps):
        temp = ideal * (1.0 - step / steps) * 1.35 + 4.0
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in range(count):
            buckets[(int(math.floor(xs[i] / cell)), int(math.floor(ys[i] / cell)))].append(i)
        disp_x = [0.0] * count
        disp_y = [0.0] * count
        for (cx, cy), members in buckets.items():
            nearby: list[int] = []
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    nearby.extend(buckets.get((cx + ox, cy + oy), ()))
            if len(nearby) > 28:
                nearby = nearby[:: max(1, len(nearby) // 24)]
            for i in members:
                xi, yi = xs[i], ys[i]
                for j in nearby:
                    if j <= i:
                        continue
                    dx = xi - xs[j]
                    dy = yi - ys[j]
                    dist2 = dx * dx + dy * dy
                    if dist2 < 0.01 or dist2 > cell * cell * 3.2:
                        continue
                    dist = math.sqrt(dist2)
                    force = (ideal * ideal) / dist
                    ux, uy = dx / dist, dy / dist
                    disp_x[i] += ux * force
                    disp_y[i] += uy * force
                    disp_x[j] -= ux * force
                    disp_y[j] -= uy * force
        rest = ideal * 0.9
        for ia, ib in pairs:
            dx = xs[ia] - xs[ib]
            dy = ys[ia] - ys[ib]
            dist = math.hypot(dx, dy) or 0.01
            pull = (dist - rest) * 0.055
            ux, uy = dx / dist, dy / dist
            disp_x[ia] -= ux * pull
            disp_y[ia] -= uy * pull
            disp_x[ib] += ux * pull
            disp_y[ib] += uy * pull
        for i in range(count):
            dist = math.hypot(disp_x[i], disp_y[i])
            if dist < 0.001:
                continue
            scale = min(temp, dist) / dist
            xs[i] += disp_x[i] * scale
            ys[i] += disp_y[i] * scale
    for i, zid in enumerate(ids):
        seeded[zid] = (xs[i], ys[i])


def _tag_anchor(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Тег стоит у самой плотной группы своих карточек, а не в пустоте между ними."""
    if len(points) == 1:
        return points[0]
    cell = 160.0
    bins: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for x, y in points:
        bins[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append((x, y))
    best = max(bins.values(), key=len)
    return (
        sum(point[0] for point in best) / len(best),
        sum(point[1] for point in best) / len(best),
    )


def _positions_large(zettels: list, entities: list, edges: list) -> Dict[str, Tuple[float, float]]:
    """Сотни тысяч узлов: сразу круги проектов, без дерева и силового расклада."""
    excel_zids = {
        z["zettel_id"] for z in zettels if (z.get("thought_type") or "") in _EXCEL_TYPES
    }
    pos: Dict[str, Tuple[float, float]] = {}
    for zettel in zettels:
        zid = zettel["zettel_id"]
        if zid not in excel_zids:
            pos[zid] = (0.0, 0.0)
    for entity in entities:
        pos[f"entity:{entity['name']}"] = (0.0, 0.0)
    for index, zid in enumerate(excel_zids):
        pos[zid] = (1_000_000.0 + (index % 64) * 40.0, (index // 64) * 40.0)
    _spread_like_obsidian(pos, zettels, edges, excel_zids)
    return pos


def _compute_node_positions(graph_data: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    zettels = graph_data.get("zettels") or []
    entities = graph_data.get("entities") or []
    edges = graph_data.get("edges") or []
    if len(zettels) >= 80_000:
        return _positions_large(zettels, entities, edges)

    children: dict[str, list[str]] = defaultdict(list)
    luhmann_to_id: dict[str, str] = {}
    parent_of: dict[str, str | None] = {}
    type_by_lid: dict[str, str] = {}
    project_by_lid: dict[str, str] = {}
    zid_to_lid: dict[str, str] = {}
    order: list[str] = []
    for z in zettels:
        lid = z["luhmann_id"]
        zid = z["zettel_id"]
        luhmann_to_id[lid] = zid
        zid_to_lid[zid] = lid
        type_by_lid[lid] = z.get("thought_type") or ""
        project_by_lid[lid] = " ".join((z.get("project") or "").split())
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

    x_unit = 96
    y_gap = 104

    def structural_kids(lid: str) -> list[str]:
        return [
            child for child in children[lid]
            if child in known and type_by_lid.get(child) != "excel_row"
        ]

    span_w, span_h = _subtree_spans(order, structural_kids, x_unit, y_gap)
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
            span_w.get(root, x_unit),
            span_h.get(root, y_gap),
        ))
    def place_box_list(box_list: list[tuple[str, str, float, float]]) -> list[str]:
        box_list = sorted(box_list, key=lambda box: box[2] * box[3], reverse=True)
        origins = _scatter_origins([(width, height) for _kind, _lid, width, height in box_list])
        before = set(pos)
        for (kind, lid, width, height), (left, top) in zip(box_list, origins):
            if kind == "excel":
                place_excel_sheet(lid, left + width / 2, top + height / 2)
            else:
                _place_tree(lid, left, top, structural_kids, span_w, span_h, x_unit, y_gap, luhmann_to_id, pos)
        return [zid for zid in pos if zid not in before]

    boxes_by_project: dict[str, list[tuple[str, str, float, float]]] = defaultdict(list)
    for box in boxes:
        boxes_by_project[project_by_lid.get(box[1], "")].append(box)
    split_projects = len([name for name in boxes_by_project if name]) > 1

    if not split_projects:
        place_box_list(boxes)
    else:
        order_names = sorted(
            boxes_by_project,
            key=lambda name: sum(width * height for _k, _l, width, height in boxes_by_project[name]),
            reverse=True,
        )
        placed_groups: list[tuple[list[str], float, float, float, float]] = []
        for name in order_names:
            ids = place_box_list(boxes_by_project[name])
            pending = [
                z for z in zettels
                if z["zettel_id"] not in pos and project_by_lid.get(z["luhmann_id"], "") == name
            ]
            if pending and ids:
                xs = [pos[zid][0] for zid in ids]
                ys = [pos[zid][1] for zid in ids]
                left = max(xs) + 80.0
                top = min(ys)
                cols = max(1, int(math.sqrt(len(pending))))
                for index, zettel in enumerate(pending):
                    pos[zettel["zettel_id"]] = (
                        left + (index % cols) * 72.0,
                        top + (index // cols) * 72.0,
                    )
                    ids.append(zettel["zettel_id"])
            if not ids:
                continue
            xs = [pos[zid][0] for zid in ids]
            ys = [pos[zid][1] for zid in ids]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            placed_groups.append((ids, max(max_x - min_x, 160.0), max(max_y - min_y, 160.0), min_x, min_y))
        island_origins = _scatter_islands(
            [(width, height) for _ids, width, height, _min_x, _min_y in placed_groups]
        )
        for (ids, _width, _height, min_x, min_y), (left, top) in zip(placed_groups, island_origins):
            dx, dy = left - min_x, top - min_y
            if not dx and not dy:
                continue
            for zid in ids:
                x, y = pos[zid]
                pos[zid] = (x + dx, y + dy)

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
            ax, ay = _tag_anchor(pts)
            ang = i * 2.399
            pos[eid] = (ax + 42.0 * math.cos(ang), ay + 42.0 * math.sin(ang))
        else:
            loose.append((i, eid))
    if loose:
        anchor_x = [point[0] for point in pos.values()]
        anchor_y = [point[1] for point in pos.values()]
        if split_projects and anchor_x:
            cx = max(anchor_x) + 140.0
            cy = min(anchor_y)
        else:
            cx = sorted(anchor_x)[len(anchor_x) // 2] if anchor_x else 0.0
            cy = sorted(anchor_y)[len(anchor_y) // 2] if anchor_y else 0.0
        for n, (_i, eid) in enumerate(loose):
            ang = n * 2.399
            ring = 48.0 + 10.0 * math.sqrt(n)
            pos[eid] = (cx + ring * math.cos(ang), cy + ring * math.sin(ang))
    _spread_like_obsidian(pos, zettels, edges, excel_zids)
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
        return 2, 8, 7
    if thought_type == "excel_sheet":
        return 0, 22, color_index.get(root, 0)
    if thought_type in {"excel_column", "excel_value"}:
        source = luhmann_id if thought_type == "excel_column" else (parent_by_luhmann.get(luhmann_id) or luhmann_id)
        parts = [p for p in (source or "").split(".") if p.isdigit()]
        color = int(parts[-1]) % len(_BRANCH_PALETTE) if parts else 1
        if thought_type == "excel_column":
            return 0, 14, color
        return 0, 10, color
    return 0, _thought_size(depth), color_index.get(root, 0)


def _thought_size(depth: int) -> int:
    if depth <= 0:
        return 24
    if depth == 1:
        return 17
    if depth == 2:
        return 13
    return 8


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
        if card_count >= 25_000 and group == 0 and radius < 13:
            label = ""
        nodes.append({
            "x": round(xy[0], 1),
            "y": round(xy[1], 1),
            "r": radius,
            "g": group,
            "c": color,
            "l": label,
        })
        if card_count >= 25_000:
            meta.append([project] if project else [])
        else:
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
            "r": 8,
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
    _append_project_bridges(graph_data, id_index, edges)
    return nodes, edges, meta, len(graph_data["zettels"]), len(graph_data["entities"]), len(edges)


def _append_project_bridges(graph_data: Dict[str, Any], id_index: dict[str, int], edges: list) -> None:
    """На общем графе проекты с общим тегом соединяются парой карточек."""
    project_of: dict[str, str] = {}
    for zettel in graph_data.get("zettels") or []:
        name = " ".join((zettel.get("project") or "").split())
        if name:
            project_of[zettel["zettel_id"]] = name
    if len(set(project_of.values())) < 2:
        return
    chosen: dict[str, dict[str, str]] = defaultdict(dict)
    for zettel in graph_data.get("zettels") or []:
        project = project_of.get(zettel["zettel_id"])
        if not project:
            continue
        for tag in zettel.get("tags") or []:
            key = " ".join(str(tag).lower().split())
            if len(key) < 2:
                continue
            chosen[key].setdefault(project, zettel["zettel_id"])
    entity_key: dict[str, str] = {}
    for entity in graph_data.get("entities") or []:
        raw = str(entity.get("name") or "")
        label = " ".join((entity.get("display_name") or "").lower().split())
        if "::" in raw:
            raw = raw.split("::", 1)[1]
        key = label or " ".join(raw.lower().split())
        if len(key) >= 2:
            entity_key[f"entity:{entity['name']}"] = key
    for edge in graph_data.get("edges") or []:
        if edge.get("rel_type") != "MENTIONS":
            continue
        project = project_of.get(edge.get("from_id"))
        key = entity_key.get(str(edge.get("to_id") or ""))
        if project and key:
            chosen[key].setdefault(project, edge["from_id"])
    seen_nodes: set[tuple[int, int]] = set()
    pair_count: dict[tuple[str, str], int] = defaultdict(int)
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
            edges.append([ends[0], ends[1], 4])
            added += 1
            if added >= 480:
                break


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


def _thin_edges(edges: list[list[int]], limit: int = 2_000_000) -> list[list[int]]:
    """На очень большом графе оставляем структуру и немного связей тегов."""
    if len(edges) <= limit:
        return edges
    mention_budget = 20_000 if limit <= 400_000 else 150_000
    structural: list[list[int]] = []
    mentions: list[list[int]] = []
    for edge in edges:
        if edge[2] == 1:
            if len(mentions) < mention_budget:
                mentions.append(edge)
            continue
        structural.append(edge)
        if len(structural) >= limit:
            break
    room = limit - len(structural)
    if room > 0:
        structural.extend(mentions[:room])
    return structural


def pack_graph_payload(graph_data: Dict[str, Any]) -> dict:
    """Компактный payload: параллельные массивы вместо массива объектов."""
    nodes, edges, meta, total_z, total_e, total_r = _pack_graph(graph_data)
    n = len(nodes)
    edges = _thin_edges(edges, limit=300_000 if n >= 40_000 else 2_000_000)
    eflat: list[int] = []
    for a, b, k in edges:
        eflat.extend((int(a), int(b), int(k)))
    return {
        "n": n,
        "x": [float(nodes[i]["x"]) for i in range(n)],
        "y": [float(nodes[i]["y"]) for i in range(n)],
        "r": [max(5, int(round(nodes[i]["r"] * 0.7))) for i in range(n)],
        "g": [int(nodes[i]["g"]) for i in range(n)],
        "c": [int(nodes[i]["c"]) for i in range(n)],
        "l": [nodes[i]["l"] for i in range(n)],
        "m": meta,
        "e": eflat,
        "tz": int(total_z),
        "te": int(total_e),
        "tr": int(total_r),
        "gp": 1 if any(_meta_project(row) for row in meta) else 0,
    }


def _meta_project(row: list) -> str:
    if not row:
        return ""
    if len(row) > 10:
        return str(row[10] or "")
    return str(row[0] or "")


def dumps_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def dumps_binary(payload: dict) -> bytes:
    """Плотный формат для сотен тысяч узлов: числа массивами, без JSON."""
    n = int(payload["n"])
    ea_ready = payload.get("ea")
    if ea_ready is not None:
        ea = ea_ready
        eb = payload["eb"]
        ek = payload["ek"]
        edge_count = len(ea)
        eflat = None
    else:
        eflat = payload["e"]
        edge_count = len(eflat) // 3
    labels = [
        (index, str(text).encode("utf-8")[:120])
        for index, text in enumerate(payload.get("l") or [])
        if text
    ]
    meta = payload.get("m") or []
    projects: list[bytes] = []
    proj_of: array.array | None = None
    if payload.get("gp") and meta:
        names: dict[str, int] = {}
        proj_of = array.array("H")
        for row in meta:
            name = _meta_project(row)
            slot = names.get(name)
            if slot is None:
                slot = len(projects)
                names[name] = slot
                projects.append(str(name).encode("utf-8")[:80])
            proj_of.append(slot)
        if len(proj_of) < n:
            proj_of.extend([0] * (n - len(proj_of)))
    flags = 1 if proj_of is not None else 0
    xs = array.array("f", payload["x"])
    ys = array.array("f", payload["y"])
    rs = array.array("B", (min(255, int(v)) for v in payload["r"]))
    gs = array.array("B", (min(255, int(v)) for v in payload["g"]))
    cs = array.array("B", (min(255, int(v)) for v in payload["c"]))
    if eflat is not None:
        ea = array.array("I", eflat[0::3])
        eb = array.array("I", eflat[1::3])
        ek = array.array("B", eflat[2::3])
    header = struct.pack(
        "<4sIIIIIIII",
        b"ZGB1",
        n,
        edge_count,
        len(labels),
        len(projects),
        flags,
        int(payload.get("tz") or 0),
        int(payload.get("te") or 0),
        int(payload.get("tr") or 0),
    )
    chunks = [header, xs.tobytes(), ys.tobytes(), rs.tobytes(), gs.tobytes(), cs.tobytes()]
    raw_len = sum(len(part) for part in chunks)
    pad = (4 - (raw_len % 4)) % 4
    if pad:
        chunks.append(b"\0" * pad)
    chunks.extend((ea.tobytes(), eb.tobytes(), ek.tobytes()))
    if proj_of is not None:
        if edge_count & 1:
            chunks.append(b"\0")
        chunks.append(proj_of.tobytes())
        for name in projects:
            chunks.append(struct.pack("<H", len(name)))
            chunks.append(name)
    for index, text in labels:
        chunks.append(struct.pack("<IH", index, len(text)))
        chunks.append(text)
    return b"".join(chunks)


def build_overview_binary(zettels: list) -> bytes:
    """Короткий ответ для большого графа: координаты и цвет, без текстов и связей."""
    n = len(zettels)
    if n >= 4_000:
        positions = _positions_large(zettels, [], [])
    else:
        positions = _compute_node_positions({"zettels": zettels, "entities": [], "edges": []})
    parent_by = {z["luhmann_id"]: z.get("parent_luhmann") or "" for z in zettels}
    depths = _node_depths(parent_by, [z["luhmann_id"] for z in zettels])
    root_of = _root_lookup(parent_by)
    roots = sorted({root_of(z["luhmann_id"]) for z in zettels}, key=_luhmann_sort_key)
    color_index = {root: i % len(_BRANCH_PALETTE) for i, root in enumerate(roots)}
    xs = array.array("f")
    ys = array.array("f")
    rs = array.array("B")
    gs = array.array("B")
    cs = array.array("B")
    names: dict[str, int] = {}
    project_names: list[bytes] = []
    proj_of = array.array("H")
    labels: list[tuple[int, bytes]] = []
    has_project = any(z.get("project") for z in zettels)
    index_of = {zettel["luhmann_id"]: index for index, zettel in enumerate(zettels)}
    ea = array.array("I")
    eb = array.array("I")
    ek = array.array("B")
    for index, zettel in enumerate(zettels):
        point = positions.get(zettel["zettel_id"], (0.0, 0.0))
        xs.append(float(point[0]))
        ys.append(float(point[1]))
        lid = zettel["luhmann_id"]
        depth = depths.get(lid, 0)
        root = root_of(lid)
        thought = zettel.get("thought_type") or ""
        group, radius, color = _excel_node_style(
            thought, lid, parent_by, color_index, root, depth, 0,
        )
        project = zettel.get("project") or ""
        if project:
            color = _palette_index(project)
        rs.append(min(255, max(5, int(round(radius * 0.7)))))
        gs.append(group & 255)
        cs.append(color & 255)
        if has_project:
            slot = names.get(project)
            if slot is None:
                slot = len(project_names)
                names[project] = slot
                project_names.append(project.encode("utf-8")[:80])
            proj_of.append(slot)
        topic = " ".join((zettel.get("topic") or "").split())
        if topic and depth <= 1 and group == 0:
            labels.append((index, topic.encode("utf-8")[:80]))
        parent = index_of.get(zettel.get("parent_luhmann") or "")
        if parent is not None and parent != index:
            ea.append(index)
            eb.append(parent)
            ek.append(0)
    flags = 1 if has_project else 0
    edge_count = len(ea)
    header = struct.pack(
        "<4sIIIIIIII",
        b"ZGB1",
        n,
        edge_count,
        len(labels),
        len(project_names),
        flags,
        n,
        edge_count,
        0,
    )
    chunks = [header, xs.tobytes(), ys.tobytes(), rs.tobytes(), gs.tobytes(), cs.tobytes()]
    raw_len = sum(len(part) for part in chunks)
    pad = (4 - (raw_len % 4)) % 4
    if pad:
        chunks.append(b"\0" * pad)
    chunks.extend((ea.tobytes(), eb.tobytes(), ek.tobytes()))
    if has_project and edge_count & 1:
        chunks.append(b"\0")
    if has_project:
        chunks.append(proj_of.tobytes())
        for name in project_names:
            chunks.append(struct.pack("<H", len(name)))
            chunks.append(name)
    for index, text in labels:
        chunks.append(struct.pack("<IH", index, len(text)))
        chunks.append(text)
    return b"".join(chunks)


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
let X, Y, R, G, C, L, META, EFLAT, EA, EB, EK;
let indexReady = true;
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
function cellIndex(v) { return Math.floor(v / CELL); }
function cellKey(cx, cy) { return (cx + 524288) * 1048576 + (cy + 524288); }
let grid = new Map();
let hubs = [];
let hubMark = null;
let dotPick = null;
let dotCols = 0;
let dotBin = 12;
let fitted = false;
let clusters = [];
let groupByProject = false;
let projectLinks = [];
let PINDEX = null;
let PNAMES = null;
let searchIds = null;
let parentOf = null;
let childN = null;
let frameXY = 1;
let xyStamp = null;
let xyX = null;
let xyY = null;
let lastVis = null;
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
function beginNodeXY() {}
function nodeXY(i) {
  return [X[i], Y[i]];
}
function overviewRadius(i) {
  if (R[i] >= 15) return 7.4;
  if (R[i] >= 11) return 5.4;
  if (R[i] >= 8) return 3.6;
  return 2.15;
}
function drawLocalLinks(light) {
  if (!E) return;
  ctx.save();
  ctx.lineWidth = 0.7;
  ctx.strokeStyle = light ? '#94a3b8' : '#475569';
  ctx.globalAlpha = 0.34;
  const step = E > 24000 ? 3 : 1;
  let drawn = 0;
  for (let i = 0; i < E && drawn < 4200; i += step) {
    const a = EA ? EA[i] : EFLAT[i * 3];
    const b = EA ? EB[i] : EFLAT[i * 3 + 1];
    const ax = X[a], ay = Y[a], bx = X[b], by = Y[b];
    const dx = ax - bx, dy = ay - by;
    const dist2 = dx * dx + dy * dy;
    if (dist2 < 4 || dist2 > 160 * 160) continue;
    if (R[a] < 8 && R[b] < 8 && (i & 1)) continue;
    const x1 = sx(ax), y1 = sy(ay), x2 = sx(bx), y2 = sy(by);
    if ((x1 < -30 && x2 < -30) || (x1 > W + 30 && x2 > W + 30) || (y1 < -30 && y2 < -30) || (y1 > H + 30 && y2 > H + 30)) continue;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    drawn++;
  }
  ctx.restore();
}
function sx(x) { return (x - camX) * zoom + W / 2; }
function sy(y) { return (y - camY) * zoom + H / 2; }
function wx(x) { return (x - W / 2) / zoom + camX; }
function wy(y) { return (y - H / 2) / zoom + camY; }
let revealedMark = null;
let revealedRows = [];
function nodeVisible(i) {
  const marked = hot && hot[i];
  if (G[i] === 1 && !(tagsOn && zoom >= 0.32) && !marked) return 0;
  if (G[i] === 2 && !(revealedMark && revealedMark[i]) && !(marked === 2)) return 0;
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

function buildAdj() {
  const cnt = new Uint32Array(N);
  for (let i = 0; i < E; i++) {
    const a = EA ? EA[i] : EFLAT[i * 3];
    const b = EA ? EB[i] : EFLAT[i * 3 + 1];
    cnt[a]++;
    cnt[b]++;
  }
  adjOff = new Uint32Array(N + 1);
  for (let i = 0; i < N; i++) adjOff[i + 1] = adjOff[i] + cnt[i];
  adjTo = new Uint32Array(adjOff[N]);
  adjK = new Uint8Array(adjOff[N]);
  adjFrom = new Uint32Array(adjOff[N]);
  const cur = adjOff.slice();
  projectLinks = [];
  parentOf = new Int32Array(N);
  parentOf.fill(-1);
  childN = new Uint32Array(N);
  for (let i = 0; i < E; i++) {
    const a = EA ? EA[i] : EFLAT[i * 3];
    const b = EA ? EB[i] : EFLAT[i * 3 + 1];
    const k = EA ? EK[i] : EFLAT[i * 3 + 2];
    let p = cur[a]++;
    adjTo[p] = b; adjK[p] = k; adjFrom[p] = a;
    p = cur[b]++;
    adjTo[p] = a; adjK[p] = k; adjFrom[p] = a;
    if (k === 4) projectLinks.push(a, b);
    if (k === 0 && parentOf[a] < 0) {
      parentOf[a] = b;
      childN[b]++;
    }
  }
}
function buildGrid() {
  projectLinks = [];
  grid = new Map();
  const storeIds = N < 4000;
  for (let i = 0; i < N; i++) {
    const cx = cellIndex(X[i]), cy = cellIndex(Y[i]);
    const key = cellKey(cx, cy);
    let bucket = grid.get(key);
    if (!bucket) {
      bucket = {cx: cx, cy: cy, maxI: i, ids: storeIds ? [i] : null};
      grid.set(key, bucket);
      continue;
    }
    if (storeIds) bucket.ids.push(i);
    if (R[i] >= R[bucket.maxI]) bucket.maxI = i;
  }
  const rank = [];
  const selective = N > 20000;
  for (let i = 0; i < N; i++) {
    if (G[i] === 1 || G[i] === 2) continue;
    if (selective && R[i] < 11) continue;
    rank.push(i);
  }
  if (selective && rank.length < 40) {
    const step = Math.max(1, Math.floor(N / 180));
    for (let i = 0; i < N && rank.length < 180; i += step) {
      if (G[i] === 1 || G[i] === 2) continue;
      rank.push(i);
    }
  }
  rank.sort(function(a, b) { return R[b] - R[a]; });
  hubs = rank.slice(0, Math.min(rank.length, N > 2500 ? 180 : 320));
  hubMark = new Uint8Array(N);
  for (let h = 0; h < hubs.length; h++) hubMark[hubs[h]] = 1;
}
function buildHay() {
  hay = new Array(N);
  searchIds = N > 40000 ? [] : null;
  for (let i = 0; i < N; i++) {
    const m = META[i] || [];
    const label = L[i] || '';
    if (searchIds) {
      if (label || m[2] || m[10]) {
        hay[i] = [m[2], m[10], label].join(' ').toLowerCase();
        searchIds.push(i);
      } else hay[i] = '';
    } else {
      hay[i] = [m[0], m[2], m[4], m[5], m[7], m[10], label].join(' ').toLowerCase();
    }
  }
}
function buildIndex() {
  buildGrid();
  buildAdj();
  buildHay();
}
function projectOf(i) {
  if (PNAMES && PINDEX) return PNAMES[PINDEX[i]] || '';
  const m = META[i] || [];
  return String(m[10] || '').trim();
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
    const project = projectOf(i);
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
        project: project,
        count: 0,
        nodes: N > 40000 ? null : []
      };
      grouped.set(key, g);
    }
    g.count++;
    if (g.nodes) g.nodes.push(i);
    g.sumX += X[i];
    g.sumY += Y[i];
    const topic = String(m[2] || '').trim();
    if (topic) g.topic.set(topic, (g.topic.get(topic) || 0) + 1);
    const src = String(m[8] || '').trim();
    if (src) g.source.set(src, (g.source.get(src) || 0) + 1);
  }

  clusters = [];
  if (N > 40000) {
    const acc = new Map();
    grouped.forEach(function(g, key) {
      acc.set(key, { outer: 60, cx: g.sumX / g.count, cy: g.sumY / g.count });
    });
    for (let i = 0; i < N; i++) {
      if (G[i] === 1 || G[i] === 2) continue;
      const tt = String((META[i] || [])[3] || '');
      if (tt.indexOf('excel_') === 0) continue;
      const project = projectOf(i);
      const key = (groupByProject && project) ? ('p:' + project) : (C[i] | 0);
      const box = acc.get(key);
      if (!box) continue;
      const dx = X[i] - box.cx, dy = Y[i] - box.cy;
      const dist = Math.sqrt(dx * dx + dy * dy) + R[i] + 10;
      if (dist > box.outer) box.outer = dist;
    }
    grouped.forEach(function(g, cid) {
      const box = acc.get(cid);
      const count = g.count;
      const drawR = Math.max(42, Math.min(110, box.outer * 0.18 + 24));
      const topics = (groupByProject && g.project)
        ? [g.project]
        : [...g.topic.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(x => x[0]);
      const sources = [...g.source.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4).map(x => x[0]);
      clusters.push({
        id: cid, color: g.color, n: count, cx: box.cx, cy: box.cy,
        r: drawR, hr: Math.max(drawR * 1.18, 64), topics: topics, sources: sources
      });
    });
    return;
  }
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
    const drawR = Math.max(42, Math.min(110, outer * 0.18 + 24));
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
  const oldKey = cellKey(cellIndex(oldX), cellIndex(oldY));
  const newKey = cellKey(cellIndex(X[i]), cellIndex(Y[i]));
  if (oldKey === newKey) return;
  const ob = grid.get(oldKey);
  if (ob && ob.ids) {
    const ix = ob.ids.indexOf(i);
    if (ix >= 0) ob.ids.splice(ix, 1);
    if (ob.maxI === i) {
      ob.maxI = ob.ids.length ? ob.ids[0] : null;
      for (let k = 0; k < ob.ids.length; k++) {
        const j = ob.ids[k];
        if (ob.maxI == null || R[j] >= R[ob.maxI]) ob.maxI = j;
      }
    }
  }
  let nb = grid.get(newKey);
  if (!nb) {
    nb = {cx: cellIndex(X[i]), cy: cellIndex(Y[i]), maxI: i, ids: []};
    grid.set(newKey, nb);
  }
  if (!nb.ids) nb.ids = [];
  nb.ids.push(i);
  if (nb.maxI == null || R[i] >= R[nb.maxI]) nb.maxI = i;
}

function cellsInView(x0, y0, x1, y1) {
  const cx0 = Math.floor(x0 / CELL), cy0 = Math.floor(y0 / CELL);
  const cx1 = Math.floor(x1 / CELL), cy1 = Math.floor(y1 / CELL);
  const ncell = (cx1 - cx0 + 1) * (cy1 - cy0 + 1);
  const out = [];
  if (ncell > 2500) {
    grid.forEach(function(bucket) {
      if (bucket.cx < cx0 || bucket.cx > cx1 || bucket.cy < cy0 || bucket.cy > cy1) return;
      const ids = bucket.ids;
      if (!ids) return;
      for (let i = 0; i < ids.length; i++) out.push(ids[i]);
    });
    return out;
  }
  for (let cx = cx0; cx <= cx1; cx++) {
    for (let cy = cy0; cy <= cy1; cy++) {
      const b = grid.get(cellKey(cx, cy));
      if (!b) continue;
      if (!b.ids) continue;
      for (let i = 0; i < b.ids.length; i++) out.push(b.ids[i]);
    }
  }
  return out;
}
function nodeAlpha(i, base) {
  return G[i] === 1 ? base * 0.68 : base;
}
function screenRadius(i) {
  const raw = R[i] * Math.min(zoom, 0.9);
  const minR = zoom < 0.2 ? 2.2 : 3.1;
  let cap = 5.6;
  if (G[i] === 1) cap = 6;
  else if (R[i] >= 15) cap = 14;
  else if (R[i] >= 11) cap = 11;
  else if (R[i] >= 8) cap = 8.2;
  return Math.min(cap, Math.max(minR, raw));
}
function hubRadius(i) {
  const cap = R[i] >= 15 ? 13 : (R[i] >= 11 ? 10 : (R[i] >= 8 ? 8 : 6));
  return Math.max(4, Math.min(cap, R[i] * Math.max(zoom, 0.14)));
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
  const xy = nodeXY(i, 0);
  const x = sx(xy[0]), y = sy(xy[1]);
  const r = screenRadius(i);
  ctx.globalAlpha = nodeAlpha(i, alpha);
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = fillOf(i);
  ctx.fill();
  ctx.lineWidth = ring ? 1.6 : 1;
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
  if (zoom < 0.45) return rank <= 1;
  if (zoom < 0.95) return rank <= 1 || R[i] >= 10;
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
    const xy = nodeXY(i, 0);
    tx = sx(xy[0]) + dir[0] * (r + 8);
    ty = sy(xy[1]) + dir[1] * (r + 4);
  } else {
    align = 'center';
    baseline = 'top';
    const xy = nodeXY(i, 0);
    tx = sx(xy[0]);
    ty = sy(xy[1]) + r + 3;
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
    const xy = nodeXY(i, 0);
    const x = sx(xy[0]), y = sy(xy[1]);
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
function drawProjectLinks(light) {
  if (!projectLinks.length) return;
  ctx.save();
  ctx.globalAlpha = 0.82;
  ctx.strokeStyle = light ? '#0369a1' : '#7dd3fc';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([7, 5]);
  for (let i = 0; i < projectLinks.length; i += 2) {
    const a = projectLinks[i], b = projectLinks[i + 1];
    const ax = sx(X[a]), ay = sy(Y[a]), bx = sx(X[b]), by = sy(Y[b]);
    if ((ax < -40 && bx < -40) || (ax > W + 40 && bx > W + 40) || (ay < -40 && by < -40) || (ay > H + 40 && by > H + 40)) continue;
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();
  }
  ctx.restore();
}
function drawOverviewBuckets(x0, y0, x1, y1, light) {
  const bin = 12;
  const cols = Math.ceil(W / bin) + 2;
  const rows = Math.ceil(H / bin) + 2;
  const best = new Int32Array(cols * rows);
  const bestR = new Uint16Array(cols * rows);
  best.fill(-1);
  dotPick = best;
  drawProjectLinks(light);
  dotCols = cols;
  dotBin = bin;
  const outline = light ? '#ffffff' : '#0b1220';
  const cx0 = Math.floor(x0 / CELL), cy0 = Math.floor(y0 / CELL);
  const cx1 = Math.floor(x1 / CELL), cy1 = Math.floor(y1 / CELL);
  const ncell = (cx1 - cx0 + 1) * (cy1 - cy0 + 1);
  function consider(bucket) {
    const i = bucket.maxI;
    if (i == null) return;
    if (visMask && !visMask[i]) return;
    const px = sx(X[i]), py = sy(Y[i]);
    if (px < -bin || py < -bin || px > W + bin || py > H + bin) return;
    const bx = (px / bin) | 0, by = (py / bin) | 0;
    if (bx < 0 || by < 0 || bx >= cols || by >= rows) return;
    const id = bx + by * cols;
    if (best[id] >= 0 && R[i] <= bestR[id]) return;
    best[id] = i;
    bestR[id] = R[i];
  }
  if (ncell > 2500) {
    grid.forEach(function(bucket) {
      if (bucket.cx < cx0 || bucket.cx > cx1 || bucket.cy < cy0 || bucket.cy > cy1) return;
      consider(bucket);
    });
  } else {
    for (let cx = cx0; cx <= cx1; cx++) {
      for (let cy = cy0; cy <= cy1; cy++) {
        const bucket = grid.get(cellKey(cx, cy));
        if (bucket) consider(bucket);
      }
    }
  }
  for (let id = 0; id < best.length; id++) {
    const i = best[id];
    if (i < 0) continue;
    const bx = id % cols, by = (id / cols) | 0;
    ctx.globalAlpha = nodeAlpha(i, 0.9);
    ctx.beginPath();
    ctx.arc(bx * bin + bin / 2, by * bin + bin / 2, overviewRadius(i), 0, Math.PI * 2);
    ctx.fillStyle = fillOf(i);
    ctx.fill();
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = outline;
    ctx.stroke();
  }
  drawBranchCaptions(light);
  drawHubs(light);
}
function collectVisible(x0, y0, x1, y1, limit) {
  const out = [];
  const step = Math.max(1, Math.floor(N / Math.max(limit, 1)));
  for (let i = 0; i < N; i += step) {
    if (X[i] < x0 || X[i] > x1 || Y[i] < y0 || Y[i] > y1) continue;
    if (G[i] === 2 && !(hot && hot[i])) continue;
    out.push(i);
    if (out.length >= limit) break;
  }
  for (let i = 0; i < N; i++) {
    if (R[i] < 8 || G[i] === 2) continue;
    if (X[i] < x0 || X[i] > x1 || Y[i] < y0 || Y[i] > y1) continue;
    out.push(i);
  }
  return out;
}
function drawOverview(x0, y0, x1, y1, light) {
  const bin = 12;
  const cols = Math.ceil(W / bin) + 2;
  const rows = Math.ceil(H / bin) + 2;
  const best = new Int32Array(cols * rows);
  const bestR = new Uint16Array(cols * rows);
  best.fill(-1);
  dotPick = best;
  drawLocalLinks(light);
  drawProjectLinks(light);
  dotCols = cols;
  dotBin = bin;
  const outline = light ? '#ffffff' : '#0b1220';
  const step = N > 60000 ? 4 : 1;
  function considerNode(i) {
    if (G[i] === 2 && !(hot && hot[i] === 2)) return;
    if (G[i] === 1 && !tagsOn && !(hot && hot[i])) return;
    if (visMask && !visMask[i]) return;
    if (X[i] < x0 || X[i] > x1 || Y[i] < y0 || Y[i] > y1) return;
    const px = sx(X[i]), py = sy(Y[i]);
    if (px < -bin || py < -bin || px > W + bin || py > H + bin) return;
    const bx = (px / bin) | 0, by = (py / bin) | 0;
    if (bx < 0 || by < 0 || bx >= cols || by >= rows) return;
    const id = bx + by * cols;
    if (best[id] >= 0 && R[i] <= bestR[id]) return;
    best[id] = i;
    bestR[id] = R[i];
  }
  for (let i = 0; i < N; i += step) considerNode(i);
  if (step > 1) {
    for (let i = 0; i < N; i++) {
      if (R[i] < 8 || G[i] === 2) continue;
      considerNode(i);
    }
  }
  for (let id = 0; id < best.length; id++) {
    const i = best[id];
    if (i < 0) continue;
    const bx = id % cols, by = (id / cols) | 0;
    ctx.globalAlpha = nodeAlpha(i, 0.9);
    ctx.beginPath();
    ctx.arc(bx * bin + bin / 2, by * bin + bin / 2, overviewRadius(i), 0, Math.PI * 2);
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
  beginNodeXY();
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
    if (!vis.length) vis = collectVisible(x0, y0, x1, y1, 2400);
    if (vis.length > 1600) overview = true;
  }
  if (overview) {
    drawOverview(x0, y0, x1, y1, light);
    drawHotMarks();
    ctx.globalAlpha = 1;
    return;
  }
  dotPick = null;
  lastVis = vis;
  drawProjectLinks(light);
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
      ctx.globalAlpha = active ? 0.12 : 0.05;
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
        if (k === 4) continue;
        if (k === 1 && !showTags) continue;
        const rowEdge = G[a] === 2 || G[b] === 2;
        const tagEdge = k === 1;
        const focusEdge = hot && (hot[a] === 2 || hot[b] === 2);
        const bridge = k === 3;
        const projectLink = k === 4;
        if (!bridge && !projectLink && k === 2 && !(selected >= 0 && (a === selected || b === selected) && rowEdge)) continue;
        if (!bridge && !projectLink && rowEdge && selected !== a && selected !== b) continue;
        const pa = nodeXY(a, 0), pb = nodeXY(b, 0);
        const ax = pa[0], ay = pa[1], bx = pb[0], by = pb[1];
        if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
        const dx = bx - ax, dy = by - ay;
        const len = Math.hypot(dx, dy);
        if (len < 1) continue;
        const ra = screenRadius(a) / zoom, rb = screenRadius(b) / zoom;
        if ((len - ra - rb) * zoom < 1) continue;
        const ux = dx / len, uy = dy / len;
        ctx.globalAlpha = focusEdge ? 0.9 : (projectLink ? 0.55 : (tagEdge ? 0.2 : (bridge ? 0.4 : (hot ? 0.16 : 0.32))));
        ctx.strokeStyle = focusEdge ? '#2563eb' : (projectLink ? (light ? '#0369a1' : '#7dd3fc') : (tagEdge ? (light ? '#cbd5e1' : '#64748b') : (bridge ? '#c026d3' : (light ? '#64748b' : '#94a3b8'))));
        ctx.lineWidth = projectLink ? 1.15 : (tagEdge ? 0.7 : (bridge ? 1.2 : 0.7));
        ctx.setLineDash(projectLink || k === 1 || bridge ? [6, 4] : []);
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
  let best = -1, bestD = 1e12;
  if (!dotPick && lastVis && zoom > 0.55) {
    for (let c = 0; c < lastVis.length; c++) {
      const i = lastVis[c];
      if (!nodeVisible(i)) continue;
      const xy = nodeXY(i, 0);
      const cx = sx(xy[0]), cy = sy(xy[1]);
      const dx = cx - px, dy = cy - py;
      const d2 = dx * dx + dy * dy;
      const r = Math.max(screenRadius(i), 11);
      if (d2 <= (r + 8) * (r + 8) && d2 < bestD) { best = i; bestD = d2; }
    }
    if (best >= 0) return best;
  }
  const reach = 64 / Math.max(zoom, 0.04);
  const x = wx(px), y = wy(py);
  const cand = cellsInView(x - reach, y - reach, x + reach, y + reach);
  for (let c = 0; c < cand.length; c++) {
    const i = cand[c];
    const hub = dotPick && hubMark && hubMark[i];
    if (!nodeVisible(i) && !hub) continue;
    const xy = nodeXY(i, 0);
    const cx = sx(xy[0]), cy = sy(xy[1]);
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
  const labeled = [];
  for (let i = 0; i < N && marked < 240; i++) {
    if (!hot[i]) continue;
    const xy = nodeXY(i, 0);
    const x = sx(xy[0]), y = sy(xy[1]);
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
    if (hot[i] === 2 && labeled.length < 48 && L[i]) labeled.push(i);
    marked++;
  }
  if (!labeled.length) return;
  ctx.font = '12px Arial';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const light = isLight();
  for (let k = 0; k < labeled.length; k++) drawLabel(labeled[k], 1, light);
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
  if (G[idx] === 0 && !m[0] && !m[2] && !m[5]) {
    const project = projectOf(idx);
    if (project) html += '<div class="field"><div class="field-label">Проект</div><div class="field-value">' + esc(project) + '</div></div>';
  } else if (G[idx] === 0) {
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
  if (!adjOff) {
    content.innerHTML = html;
    panel.style.display = 'block';
    requestDraw();
    return;
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
  zoom = Math.max(1e-8, Math.min(12, zoom * factor));
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
  if (!hay) return;
  const matched = [];
  let tagHit = false;
  let rowHit = false;
  const scan = searchIds || null;
  const scanN = scan ? scan.length : N;
  for (let s = 0; s < scanN; s++) {
    const i = scan ? scan[s] : s;
    if (hay[i] && hay[i].indexOf(q) !== -1) {
      matched.push(i);
      if (G[i] === 1) tagHit = true;
      if (G[i] === 2) rowHit = true;
    }
  }
  if (!matched.length) {
    visMask = null;
    hot = null;
    requestDraw();
    return;
  }
  visMask = new Uint8Array(N);
  for (let m = 0; m < matched.length; m++) visMask[matched[m]] = 1;
  hot = new Uint8Array(N);
  for (let m = 0; m < matched.length; m++) {
    const i = matched[m];
    hot[i] = 2;
    const start = adjOff[i], end = adjOff[i + 1];
    const nbrCap = N > 40000 ? Math.min(end, start + 48) : end;
    for (let p = start; p < nbrCap; p++) {
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
  const finite = [];
  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    if (isFinite(X[id]) && isFinite(Y[id])) finite.push(id);
  }
  if (!finite.length) return;
  let focus = finite;
  if (finite.length > 1) {
    const cell = 240;
    const bins = new Map();
    for (let i = 0; i < finite.length; i++) {
      const id = finite[i];
      const key = Math.floor(X[id] / cell) + ':' + Math.floor(Y[id] / cell);
      let bin = bins.get(key);
      if (!bin) { bin = []; bins.set(key, bin); }
      bin.push(id);
    }
    let bestKey = '', bestN = 0;
    bins.forEach(function(bin, key) {
      if (bin.length > bestN) { bestN = bin.length; bestKey = key; }
    });
    const parts = bestKey.split(':');
    const bx = parseInt(parts[0], 10), by = parseInt(parts[1], 10);
    focus = [];
    for (let oy = -1; oy <= 1; oy++) {
      for (let ox = -1; ox <= 1; ox++) {
        const bin = bins.get((bx + ox) + ':' + (by + oy));
        if (!bin) continue;
        for (let k = 0; k < bin.length; k++) focus.push(bin[k]);
      }
    }
    if (!focus.length) focus = finite;
  }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let i = 0; i < focus.length; i++) {
    const id = focus[i];
    if (X[id] < minX) minX = X[id];
    if (Y[id] < minY) minY = Y[id];
    if (X[id] > maxX) maxX = X[id];
    if (Y[id] > maxY) maxY = Y[id];
  }
  if (!isFinite(minX)) return;
  camX = (minX + maxX) / 2;
  camY = (minY + maxY) / 2;
  const dx = Math.max(maxX - minX, 160);
  const dy = Math.max(maxY - minY, 160);
  const fit = Math.min((W - 120) / dx, (H - 120) / dy);
  zoom = Math.min(1.05, fit > 0 && isFinite(fit) ? fit : 0.4);
  if (!(zoom > 1e-8)) zoom = 1e-8;
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

function parseBinary(bytes) {
  const raw = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const buf = raw.buffer;
  const base = raw.byteOffset;
  const view = new DataView(buf, base, raw.byteLength);
  if (view.getUint8(0) !== 90 || view.getUint8(1) !== 71 || view.getUint8(2) !== 66 || view.getUint8(3) !== 49) {
    throw new Error('Неизвестный формат графа');
  }
  let o = 4;
  const n = view.getUint32(o, true); o += 4;
  const e = view.getUint32(o, true); o += 4;
  const nlab = view.getUint32(o, true); o += 4;
  const nproj = view.getUint32(o, true); o += 4;
  const flags = view.getUint32(o, true); o += 4;
  const tz = view.getUint32(o, true); o += 4;
  const te = view.getUint32(o, true); o += 4;
  const tr = view.getUint32(o, true); o += 4;
  const x = new Float32Array(buf, base + o, n); o += n * 4;
  const y = new Float32Array(buf, base + o, n); o += n * 4;
  const r = new Uint8Array(buf, base + o, n); o += n;
  const g = new Uint8Array(buf, base + o, n); o += n;
  const c = new Uint8Array(buf, base + o, n); o += n;
  o = (o + 3) & ~3;
  const ea = new Uint32Array(buf, base + o, e); o += e * 4;
  const eb = new Uint32Array(buf, base + o, e); o += e * 4;
  const ek = new Uint8Array(buf, base + o, e); o += e;
  let pi = null;
  let pnames = null;
  const dec = new TextDecoder();
  if (flags & 1) {
    if (o & 1) o += 1;
    pi = new Uint16Array(buf, base + o, n);
    o += n * 2;
    pnames = new Array(nproj);
    for (let i = 0; i < nproj; i++) {
      const len = view.getUint16(o, true); o += 2;
      pnames[i] = dec.decode(new Uint8Array(buf, base + o, len));
      o += len;
    }
  }
  const labels = new Array(n);
  for (let i = 0; i < nlab; i++) {
    const index = view.getUint32(o, true); o += 4;
    const len = view.getUint16(o, true); o += 2;
    labels[index] = dec.decode(new Uint8Array(buf, base + o, len));
    o += len;
  }
  return {
    n: n, x: x, y: y, r: r, g: g, c: c, l: labels, m: [], e: [],
    ea: ea, eb: eb, ek: ek,
    tz: tz, te: te, tr: tr, gp: flags & 1, pi: pi, pnames: pnames
  };
}
function applyPayload(data) {
  N = data.n || 0;
  X = data.x instanceof Float32Array ? data.x : Float32Array.from(data.x || []);
  Y = data.y instanceof Float32Array ? data.y : Float32Array.from(data.y || []);
  R = (data.r instanceof Uint8Array || data.r instanceof Uint16Array) ? data.r : Uint16Array.from(data.r || []);
  G = data.g instanceof Uint8Array ? data.g : Uint8Array.from(data.g || []);
  C = data.c instanceof Uint8Array ? data.c : Uint8Array.from(data.c || []);
  L = data.l || [];
  META = data.m || [];
  if (data.ea) {
    EA = data.ea;
    EB = data.eb;
    EK = data.ek;
    EFLAT = [];
    E = EA.length;
  } else {
    EA = null;
    EB = null;
    EK = null;
    EFLAT = data.e || [];
    E = (EFLAT.length / 3) | 0;
  }
  PINDEX = data.pi || null;
  PNAMES = data.pnames || null;
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
  clusterHot = -1;
  hideClusterTip();
  buildGrid();
  fitted = false;
  if (N < 4000) {
    buildAdj();
    buildHay();
    indexReady = true;
    resize();
    requestDraw();
    setTimeout(function() {
      buildClusters();
      requestDraw();
    }, 0);
    return;
  }
  indexReady = false;
  resize();
  requestDraw();
  setTimeout(function() { fillGridIds(0); }, 0);
}
function fillGridIds(start) {
  const end = Math.min(N, start + 40000);
  for (let i = start; i < end; i++) {
    const bucket = grid.get(cellKey(cellIndex(X[i]), cellIndex(Y[i])));
    if (!bucket) continue;
    if (!bucket.ids) bucket.ids = [];
    bucket.ids.push(i);
  }
  if (end < N) {
    setTimeout(function() { fillGridIds(end); }, 0);
    return;
  }
  setTimeout(function() {
    buildAdj();
    buildHay();
    buildClusters();
    indexReady = true;
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
  setLoad(4, 'Передача графа');
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
  let data;
  if (buf.length >= 4 && buf[0] === 90 && buf[1] === 71 && buf[2] === 66 && buf[3] === 49) {
    data = parseBinary(buf);
  } else {
    data = JSON.parse(new TextDecoder().decode(buf));
  }
  setLoad(100, 'Готово');
  await paintLoad();
  return data;
}
(async function boot() {
  const hint = document.getElementById('load-hint');
  try {
    let data;
    if (DATA_URL) {
      setLoad(0, 'Сервер собирает граф');
      await paintLoad();
      const resp = await fetch(DATA_URL, { headers: { 'Accept': 'application/octet-stream, application/json' } });
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
    setLoad(1, 'Ошибка загрузки');
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
      <div id="load-caption">Сервер собирает граф</div>
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