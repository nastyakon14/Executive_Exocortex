"""
Интерактивный граф знаний v2: Canvas 2D на CPU.
Самодостаточный пайплайн: раскладка, упаковка данных, HTML и gzip.
"""

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


def _find_root_luhmann(luhmann_id: str, parent_by_luhmann: dict) -> str:
    current = luhmann_id
    seen = set()
    while parent_by_luhmann.get(current) and current not in seen:
        seen.add(current)
        current = parent_by_luhmann[current]
    return current


def _assign_branch_colors(zettels: list, parent_by_luhmann: dict) -> dict:
    roots = sorted(
        {_find_root_luhmann(z["luhmann_id"], parent_by_luhmann) for z in zettels},
        key=_luhmann_sort_key,
    )
    return {root: _BRANCH_PALETTE[i % len(_BRANCH_PALETTE)] for i, root in enumerate(roots)}


def _compute_node_positions(graph_data: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
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

    def place(lid: str, x_left: float, y: float, stack: set[str]) -> None:
        if lid in stack:
            return
        stack.add(lid)
        width = leaf_count(lid, set()) * x_unit
        zid = luhmann_to_id.get(lid)
        if zid:
            pos[zid] = (x_left + width / 2, y)
        cursor = x_left
        for child in children[lid]:
            if child not in known:
                continue
            child_w = leaf_count(child, set()) * x_unit
            place(child, cursor, y + y_gap, stack)
            cursor += child_w
        stack.remove(lid)

    depth_memo: dict[str, int] = {}

    def tree_depth(lid: str, stack: set[str]) -> int:
        cached = depth_memo.get(lid)
        if cached is not None:
            return cached
        if lid in stack:
            return 0
        stack.add(lid)
        kids = [c for c in children[lid] if c in known]
        depth = 1 + max((tree_depth(c, stack) for c in kids), default=0)
        stack.remove(lid)
        depth_memo[lid] = depth
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
        place(root, x_cursor, y_cursor, set())
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
            pos[eid] = (ax + 34 * math.cos(i * 1.7), ay + 72 + 34 * math.sin(i * 1.7))
        else:
            pos[eid] = (i * 88.0, y_cursor + row_h + 220)
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
    depth_by_luhmann: dict[str, int] = {}

    def calc_depth(luhmann_id: str, stack=None) -> int:
        if luhmann_id in depth_by_luhmann:
            return depth_by_luhmann[luhmann_id]
        if stack is None:
            stack = set()
        if luhmann_id in stack:
            depth_by_luhmann[luhmann_id] = 0
            return 0
        parent_l = parent_by_luhmann.get(luhmann_id)
        if not parent_l:
            depth_by_luhmann[luhmann_id] = 0
            return 0
        stack.add(luhmann_id)
        depth = calc_depth(parent_l, stack) + 1
        stack.remove(luhmann_id)
        depth_by_luhmann[luhmann_id] = depth
        return depth

    branch_colors = _assign_branch_colors(graph_data["zettels"], parent_by_luhmann)
    color_index = {root: i % len(_BRANCH_PALETTE) for i, root in enumerate(branch_colors)}
    positions = _compute_node_positions(graph_data)

    nodes = []
    meta = []
    id_index: dict[str, int] = {}
    for z in graph_data["zettels"]:
        nid = z["zettel_id"]
        if nid in id_index:
            continue
        depth = calc_depth(z["luhmann_id"])
        topic = z.get("topic") or ""
        content = z.get("content") or ""
        tags = list(z.get("tags") or [])
        tt = z.get("thought_type") or ""
        display = topic.strip() or content
        xy = positions.get(nid, (0.0, 0.0))
        root = _find_root_luhmann(z["luhmann_id"], parent_by_luhmann)
        id_index[nid] = len(nodes)
        nodes.append({
            "x": round(xy[0], 1),
            "y": round(xy[1], 1),
            "r": _thought_size(depth),
            "g": 0,
            "c": color_index[root],
            "l": _node_label(display),
        })
        preview = f"{topic}: {_short_text(content, 100)}" if topic else _short_text(content, 120)
        meta.append([
            z["luhmann_id"],
            z.get("parent_luhmann") or "",
            topic,
            tt,
            ", ".join(tags),
            _short_text(content, 2500),
            preview,
            " ".join([z["luhmann_id"], topic, content[:180], " ".join(tags), tt, z.get("source_input") or ""]).lower(),
            z.get("source_input") or "text",
        ])

    for e in graph_data["entities"]:
        eid = f"entity:{e['name']}"
        if eid in id_index:
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
        edge_set.add(key)
        edges.append([a, b, kind.get(edge["rel_type"], 0)])
    return nodes, edges, meta, len(graph_data["zettels"]), len(graph_data["entities"]), len(edges)


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
        "r": [max(8, int(round(nodes[i]["r"] * 0.5))) for i in range(n)],
        "g": [int(nodes[i]["g"]) for i in range(n)],
        "c": [int(nodes[i]["c"]) for i in range(n)],
        "l": [nodes[i]["l"] for i in range(n)],
        "m": meta,
        "e": eflat,
        "tz": int(total_z),
        "te": int(total_e),
        "tr": int(total_r),
    }


def dumps_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_graph_payload(payload: dict, accept_encoding: str = "") -> tuple[bytes, dict]:
    raw = dumps_payload(payload)
    headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    }
    if "gzip" in (accept_encoding or "").lower() and len(raw) > 8_000:
        return gzip.compress(raw, compresslevel=4), {
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
let selected = -1;
let panDrag = false, nodeDrag = -1, lastX = 0, lastY = 0, moved = false;
let raf = 0;
let hay = [];
const CELL = 160;
let grid = new Map();

function fillOf(i) { return G[i] ? '#38BDF8' : PAL[C[i]][0]; }
function strokeOf(i) { return G[i] ? '#FFFFFF' : PAL[C[i]][1]; }
function isLight() { return document.body.classList.contains('light-theme'); }
function sx(x) { return (x - camX) * zoom + W / 2; }
function sy(y) { return (y - camY) * zoom + H / 2; }
function wx(x) { return (x - W / 2) / zoom + camX; }
function wy(y) { return (y - H / 2) / zoom + camY; }
function nodeVisible(i) {
  if (G[i] === 1 && !(tagsOn && zoom >= 0.32)) return 0;
  if (visMask && visMask[i] === 0) return 0;
  return 1;
}
function isHot(i) { return !hot || hot[i] > 0; }
function setHot(idx) {
  if (idx < 0) { hot = null; return; }
  hot = new Uint8Array(N);
  hot[idx] = 2;
  const start = adjOff[idx], end = adjOff[idx + 1];
  for (let p = start; p < end; p++) hot[adjTo[p]] = 1;
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
    const key = (X[i] / CELL | 0) + ':' + (Y[i] / CELL | 0);
    let bucket = grid.get(key);
    if (!bucket) { bucket = []; grid.set(key, bucket); }
    bucket.push(i);
  }
  hay = new Array(N);
  for (let i = 0; i < N; i++) hay[i] = (META[i] && META[i][7]) || '';
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
  if (!nb) { nb = []; grid.set(newKey, nb); }
  nb.push(i);
}

function cellsInView(x0, y0, x1, y1) {
  const cx0 = Math.floor(x0 / CELL), cy0 = Math.floor(y0 / CELL);
  const cx1 = Math.floor(x1 / CELL), cy1 = Math.floor(y1 / CELL);
  const ncells = (cx1 - cx0 + 1) * (cy1 - cy0 + 1);
  if (ncells > 450) {
    const out = [];
    for (let i = 0; i < N; i++) {
      if (X[i] >= x0 && X[i] <= x1 && Y[i] >= y0 && Y[i] <= y1) out.push(i);
    }
    return out;
  }
  const out = [];
  for (let cx = cx0; cx <= cx1; cx++) {
    for (let cy = cy0; cy <= cy1; cy++) {
      const b = grid.get(cx + ':' + cy);
      if (b) for (let i = 0; i < b.length; i++) out.push(b[i]);
    }
  }
  return out;
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
  requestDraw();
}

function requestDraw() {
  if (raf) return;
  raf = requestAnimationFrame(function() { raf = 0; draw(); });
}

function drawNode(i, alpha, ring) {
  const x = sx(X[i]), y = sy(Y[i]);
  const r = Math.max(3, R[i] * zoom);
  ctx.globalAlpha = alpha;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = fillOf(i);
  ctx.fill();
  ctx.lineWidth = G[i] ? 2 : 1.6;
  ctx.strokeStyle = ring ? '#60a5fa' : strokeOf(i);
  ctx.stroke();
}

function drawLabel(i, alpha, light) {
  const r = Math.max(3, R[i] * zoom);
  if (r < 7) return false;
  ctx.globalAlpha = alpha;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.font = Math.max(10, Math.min(14, 11 * zoom)) + 'px Arial';
  ctx.fillStyle = light ? '#0f172a' : '#ffffff';
  ctx.fillText(L[i], sx(X[i]), sy(Y[i]) + r + 4);
  return true;
}

function draw() {
  const light = isLight();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.globalAlpha = 1;
  ctx.fillStyle = light ? '#f8fafc' : '#0b1220';
  ctx.fillRect(0, 0, W, H);
  if (!N) {
    ctx.fillStyle = light ? '#6b7280' : '#94a3b8';
    ctx.font = '16px Arial';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Граф пуст', W / 2, H / 2);
    return;
  }
  const pad = 90 / zoom;
  const x0 = wx(0) - pad, y0 = wy(0) - pad, x1 = wx(W) + pad, y1 = wy(H) + pad;
  const vis = cellsInView(x0, y0, x1, y1);
  const showTags = tagsOn && zoom >= 0.32;
  const showEdges = zoom >= 0.16;
  const showLabels = zoom >= 0.42;
  const dim = hot ? 0.16 : 1;
  const maxE = zoom < 0.25 ? 1800 : 7000;
  const maxN = zoom < 0.2 ? 2800 : 5500;

  if (showEdges) {
    let drawn = 0;
    ctx.lineWidth = Math.max(0.7, Math.min(2.2, zoom * 1.2));
    for (let i = 0; i < E && drawn < maxE; i++) {
      const k = EFLAT[i * 3 + 2];
      if (k === 1 && !showTags) continue;
      const a = EFLAT[i * 3], b = EFLAT[i * 3 + 1];
      if (!nodeVisible(a) || !nodeVisible(b)) continue;
      const ax = X[a], ay = Y[a], bx = X[b], by = Y[b];
      if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
      const focusEdge = hot && (hot[a] === 2 || hot[b] === 2);
      ctx.globalAlpha = focusEdge ? 1 : dim;
      ctx.strokeStyle = focusEdge ? '#60a5fa' : (k === 2 ? '#D1D5DB' : (light ? '#9ca3af' : '#9CA3AF'));
      ctx.setLineDash(k ? [5, 4] : []);
      ctx.beginPath();
      ctx.moveTo(sx(ax), sy(ay));
      ctx.lineTo(sx(bx), sy(by));
      ctx.stroke();
      drawn++;
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
    for (let v = 0; v < vis.length; v++) {
      const i = vis[v];
      if (!nodeVisible(i) || !isHot(i)) continue;
      drawNode(i, 1, hot[i] === 2);
    }
  }
  if (showLabels) {
    let drawnL = 0;
    for (let v = 0; v < vis.length && drawnL < 260; v++) {
      const i = vis[v];
      if (!nodeVisible(i)) continue;
      if (hot && !isHot(i)) continue;
      if (drawLabel(i, hot ? 1 : dim, light)) drawnL++;
    }
  }
  ctx.globalAlpha = 1;
}

function hit(px, py) {
  const x = wx(px), y = wy(py);
  const cand = cellsInView(x - 80, y - 80, x + 80, y + 80);
  let best = -1, bestD = 1e12;
  for (let c = 0; c < cand.length; c++) {
    const i = cand[c];
    if (!nodeVisible(i)) continue;
    const dx = X[i] - x, dy = Y[i] - y;
    const d = dx * dx + dy * dy;
    const lim = Math.max(R[i], 14 / zoom);
    if (d < lim * lim && d < bestD) { best = i; bestD = d; }
  }
  return best;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
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
    const thoughtNeighbors = [];
    const limit = Math.min(nbrCount, 40);
    for (let i = 0; i < limit; i++) {
      const p = start + i;
      const ni = adjTo[p];
      const k = adjK[p];
      const from = adjFrom[p];
      let arrow = '';
      if (G[ni] !== 1) {
        if (k === 0) {
          if (ni === from) arrow = '→ ';
          else if (idx === from) arrow = '← ';
        } else {
          arrow = from === idx ? '→ ' : '← ';
        }
      }
      const pm = META[ni] || [];
      const item = '<div class="neighbor-item" onclick="focusNode(' + ni + ')">' +
        arrow + '<b>' + esc(pm[6] || L[ni]) + '</b></div>';
      if (G[ni] === 1) entityNeighbors.push(item);
      else thoughtNeighbors.push(item);
    }
    html += '<hr/><div class="neighbors"><div class="field-label">Связи (' + nbrCount + ')</div>' +
      entityNeighbors.join('') + thoughtNeighbors.join('');
    if (nbrCount > limit) html += '<div class="field-label">ещё ' + (nbrCount - limit) + '</div>';
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
  camX = X[idx]; camY = Y[idx];
  zoom = Math.max(zoom, 0.9);
  showDetail(idx);
}

function fit() {
  if (!N) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let i = 0; i < N; i++) {
    if (!tagsOn && G[i] === 1) continue;
    if (X[i] < minX) minX = X[i];
    if (Y[i] < minY) minY = Y[i];
    if (X[i] > maxX) maxX = X[i];
    if (Y[i] > maxY) maxY = Y[i];
  }
  if (!isFinite(minX)) return;
  camX = (minX + maxX) / 2;
  camY = (minY + maxY) / 2;
  const dx = Math.max(maxX - minX, 200);
  const dy = Math.max(maxY - minY, 200);
  zoom = Math.max(0.04, Math.min(1.4, Math.min((W - 80) / dx, (H - 80) / dy)));
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
    wrap.style.cursor = hit(e.clientX - rect.left, e.clientY - rect.top) >= 0 ? 'pointer' : 'grab';
    return;
  }
  const dx = e.clientX - lastX, dy = e.clientY - lastY;
  if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
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
wrap.addEventListener('wheel', function(e) {
  e.preventDefault();
  const rect = wrap.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const beforeX = wx(mx), beforeY = wy(my);
  const factor = e.deltaY > 0 ? 0.9 : 1.11;
  zoom = Math.max(0.03, Math.min(3.5, zoom * factor));
  camX = beforeX - (mx - W / 2) / zoom;
  camY = beforeY - (my - H / 2) / zoom;
  requestDraw();
}, { passive: false });
wrap.addEventListener('dblclick', function(e) {
  const rect = wrap.getBoundingClientRect();
  const i = hit(e.clientX - rect.left, e.clientY - rect.top);
  if (i >= 0) focusNode(i);
});

let searchTimer = 0;
function applySearch(raw) {
  const q = (raw || '').toLowerCase().trim();
  if (!q) { visMask = null; requestDraw(); return; }
  visMask = new Uint8Array(N);
  const matched = [];
  for (let i = 0; i < N; i++) {
    if (hay[i].indexOf(q) !== -1) {
      visMask[i] = 1;
      matched.push(i);
    }
  }
  for (let m = 0; m < matched.length; m++) {
    const i = matched[m];
    const start = adjOff[i], end = Math.min(adjOff[i + 1], start + 12);
    for (let p = start; p < end; p++) visMask[adjTo[p]] = 1;
  }
  if (matched.length === 1) {
    camX = X[matched[0]]; camY = Y[matched[0]];
    zoom = Math.max(zoom, 0.8);
  }
  requestDraw();
}
document.getElementById('searchInput').addEventListener('input', function() {
  const v = this.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(function() { applySearch(v); }, 80);
});

const tagsBtn = document.getElementById('btn-tags');
tagsBtn.addEventListener('click', function() {
  tagsOn = !tagsOn;
  tagsBtn.classList.toggle('active', tagsOn);
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
  hot = null;
  visMask = null;
  tagsOn = N < 800 && E < 2500;
  if (tagsOn) tagsBtn.classList.add('active');
  else tagsBtn.classList.remove('active');
  document.getElementById('stat-z').textContent = data.tz || 0;
  document.getElementById('stat-e').textContent = data.te || 0;
  document.getElementById('stat-r').textContent = data.tr || 0;
  buildIndex();
  resize();
  fit();
  requestDraw();
}

(async function boot() {
  const hint = document.getElementById('load-hint');
  try {
    let data;
    if (DATA_URL) {
      const resp = await fetch(DATA_URL, { headers: { 'Accept': 'application/json' } });
      if (!resp.ok) throw new Error('Не удалось загрузить граф');
      data = await resp.json();
    } else if (DATA_B64) {
      data = await inflateB64(DATA_B64);
    } else {
      data = { n: 0, x: [], y: [], r: [], g: [], c: [], l: [], m: [], e: [], tz: 0, te: 0, tr: 0 };
    }
    if (hint) hint.remove();
    applyPayload(data);
  } catch (err) {
    if (hint) hint.textContent = 'Ошибка загрузки графа: ' + err.message;
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
) -> str:
    heading = html_escape((user_label or "").strip() or "Цифровой экзокортекс")
    data_url_js = json.dumps(data_url or "")
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
  .leg-dot-thought {{ background: #D4A055; border-color: #B8862E; }}
  .leg-dot-entity {{ background: #38BDF8; border-color: #FFFFFF; }}
  #graph-wrap {{ position: relative; width: 100%; height: calc(100vh - 56px); cursor: grab; }}
  #graph-wrap:active {{ cursor: grabbing; }}
  #graph {{ position: absolute; inset: 0; width: 100%; height: 100%; display: block; }}
  #load-hint {{
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    color: #94a3b8; font-size: 15px; z-index: 5; pointer-events: none;
  }}
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
  #search-box {{ position: absolute; top: 64px; left: 16px; z-index: 10; display: flex; flex-direction: column; gap: 8px; }}
  #search-box input {{
    background: rgba(15, 23, 42, 0.95); border: 1px solid #334155; border-radius: 8px;
    padding: 8px 14px; color: #e2e8f0; font-size: 13px; width: 240px; outline: none;
  }}
  #graph-controls {{ display: flex; gap: 6px; }}
  #graph-hint {{ font-size: 11px; color: #94a3b8; max-width: 260px; line-height: 1.35; }}
  body.light-theme {{ background: #f8fafc; color: #1f2937; }}
  body.light-theme #header {{ background: #ffffff; border-bottom: 1px solid #e5e7eb; }}
  body.light-theme #header h1 {{ color: #1f2937; }}
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
  body.light-theme #detail-panel .field-label, body.light-theme #graph-hint, body.light-theme #load-hint {{ color: #6b7280; }}
  body.light-theme #detail-panel a {{ color: #2563eb; }}
  body.light-theme #detail-panel .neighbor-item {{ background: #f3f4f6; color: #374151; }}
  body.light-theme #search-box input {{ background: #ffffff; border: 1px solid #d1d5db; color: #374151; }}
</style>
</head>
<body class="light-theme">
<div id="header">
  <h1>{heading}</h1>
  <div id="header-right">
    <div id="stats">Мысли: <span id="stat-z">{total_z}</span> &nbsp;|&nbsp; Сущности: <span id="stat-e">{total_e}</span> &nbsp;|&nbsp; Связи: <span id="stat-r">{total_r}</span></div>
    <button id="theme-toggle" type="button">Темная тема</button>
  </div>
</div>
<div id="search-box">
  <input type="text" id="searchInput" placeholder="🔍 Найти мысль или сущность..."/>
  <div id="graph-controls">
    <button type="button" id="btn-tags">Теги</button>
  </div>
  <div id="graph-hint">Колесо — масштаб. Пустое место — панорама. Узел можно перетащить. На большом графе теги лучше включать после приближения.</div>
</div>
<div id="graph-wrap">
  <canvas id="graph"></canvas>
  <div id="load-hint">Загрузка графа…</div>
</div>
<div id="legend">
  <h3>Легенда</h3>
  <div class="leg-item"><div class="leg-dot leg-dot-thought"></div> Мысль</div>
  <div class="leg-item"><div class="leg-dot leg-dot-entity"></div> Тег / Сущность</div>
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
) -> str:
    """Собирает HTML-граф. data_url — отдельная подгрузка payload без вставки в страницу."""
    return _build_html(graph_data, user_label=user_label, data_url=data_url)


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
