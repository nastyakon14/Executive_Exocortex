"""
Интерактивный граф знаний: Canvas 2D, без vis.js.
Рисуется только видимая область — одинаково для 10 и для десятков тысяч узлов.
"""

import gzip
import json
import math
import os
import re
import tempfile
from html import escape as html_escape
from collections import defaultdict
from typing import Any, Dict, Tuple

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


def _label_len(depth: int) -> int:
    return (34, 18, 14, 12, 10)[depth] if depth < 4 else 10


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
            "l": _short_text(display, _label_len(depth)),
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
            " ".join([z["luhmann_id"], topic, content[:180], " ".join(tags), tt]).lower(),
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
            "l": _short_text(name, 9),
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


_GRAPH_JS = r"""
<script>
const NODES = __NODES_JSON__;
const EDGES = __EDGES_JSON__;
const META = __META_JSON__;

const PAL = [["#D4A055","#B8862E"],["#72B862","#529842"],["#C46888","#A44868"],["#9078C8","#7058A8"],["#C89848","#A87828"],["#68A898","#488878"],["#C87858","#A85838"],["#78A878","#588858"],["#D48068","#B46048"],["#88B848","#689828"]];
const canvas = document.getElementById('graph');
const ctx = canvas.getContext('2d', { alpha: false });
const adj = NODES.map(() => []);
for (let i = 0; i < EDGES.length; i++) {
  const a = EDGES[i][0], b = EDGES[i][1], k = EDGES[i][2];
  adj[a].push({ j: b, k: k, from: a, to: b });
  adj[b].push({ j: a, k: k, from: a, to: b });
}
const CELL = 140;
const grid = new Map();
for (let i = 0; i < NODES.length; i++) {
  const n = NODES[i];
  const key = (n.x / CELL | 0) + ':' + (n.y / CELL | 0);
  let bucket = grid.get(key);
  if (!bucket) { bucket = []; grid.set(key, bucket); }
  bucket.push(i);
}

let W = 0, H = 0, dpr = 1;
let camX = 0, camY = 0, zoom = 1;
let tagsOn = NODES.length < 800 && EDGES.length < 2500;
let filterIds = null;
let selected = -1;
let dragging = false, lastX = 0, lastY = 0, moved = false;
let raf = 0;
function fillOf(n) { return n.g ? '#38BDF8' : PAL[n.c][0]; }
function strokeOf(n) { return n.g ? '#FFFFFF' : PAL[n.c][1]; }

function resize() {
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  W = canvas.clientWidth;
  H = canvas.clientHeight;
  canvas.width = Math.floor(W * dpr);
  canvas.height = Math.floor(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}
function sx(x) { return (x - camX) * zoom + W / 2; }
function sy(y) { return (y - camY) * zoom + H / 2; }
function wx(x) { return (x - W / 2) / zoom + camX; }
function wy(y) { return (y - H / 2) / zoom + camY; }
function isLight() { return document.body.classList.contains('light-theme'); }
function fit() {
  if (!NODES.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of NODES) {
    if (!tagsOn && n.g === 1) continue;
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x > maxX) maxX = n.x;
    if (n.y > maxY) maxY = n.y;
  }
  if (!isFinite(minX)) return;
  camX = (minX + maxX) / 2;
  camY = (minY + maxY) / 2;
  const dx = Math.max(maxX - minX, 200);
  const dy = Math.max(maxY - minY, 200);
  zoom = Math.max(0.04, Math.min(1.4, Math.min((W - 80) / dx, (H - 80) / dy)));
}
function cellsInView(x0, y0, x1, y1) {
  const cx0 = Math.floor(x0 / CELL), cy0 = Math.floor(y0 / CELL);
  const cx1 = Math.floor(x1 / CELL), cy1 = Math.floor(y1 / CELL);
  const ncells = (cx1 - cx0 + 1) * (cy1 - cy0 + 1);
  if (ncells > 450) {
    const out = [];
    for (let i = 0; i < NODES.length; i++) {
      const n = NODES[i];
      if (n.x >= x0 && n.x <= x1 && n.y >= y0 && n.y <= y1) out.push(i);
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
function requestDraw() {
  if (raf) return;
  raf = requestAnimationFrame(function() { raf = 0; draw(); });
}
function draw() {
  const light = isLight();
  ctx.fillStyle = light ? '#f8fafc' : '#0b1220';
  ctx.fillRect(0, 0, W, H);
  if (!NODES.length) {
    ctx.fillStyle = light ? '#6b7280' : '#94a3b8';
    ctx.font = '16px Arial';
    ctx.textAlign = 'center';
    ctx.fillText('Граф пуст', W / 2, H / 2);
    return;
  }
  const pad = 80 / zoom;
  const x0 = wx(0) - pad, y0 = wy(0) - pad, x1 = wx(W) + pad, y1 = wy(H) + pad;
  const vis = cellsInView(x0, y0, x1, y1);
  const showTags = tagsOn && zoom >= 0.38;
  const showLabels = zoom >= 0.5;
  const showEdges = zoom >= 0.18;
  const maxN = zoom < 0.2 ? 2500 : 5000;
  const maxE = zoom < 0.25 ? 1500 : 8000;

  if (showEdges) {
    let drawn = 0;
    ctx.lineWidth = Math.max(0.6, Math.min(1.4, zoom));
    for (let i = 0; i < EDGES.length && drawn < maxE; i++) {
      const e = EDGES[i];
      const k = e[2];
      if (k === 1 && !showTags) continue;
      const ai = e[0], bi = e[1];
      if (filterIds && (!filterIds.has(ai) || !filterIds.has(bi))) continue;
      const a = NODES[ai], b = NODES[bi];
      const ax = a.x, ay = a.y, bx = b.x, by = b.y;
      if ((ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1)) continue;
      ctx.strokeStyle = k === 2 ? '#D1D5DB' : (light ? '#9ca3af' : '#9CA3AF');
      ctx.setLineDash(k ? [4, 4] : []);
      ctx.beginPath();
      ctx.moveTo(sx(ax), sy(ay));
      ctx.lineTo(sx(bx), sy(by));
      ctx.stroke();
      drawn++;
    }
    ctx.setLineDash([]);
  }

  let drawnN = 0;
  for (let i = 0; i < vis.length && drawnN < maxN; i++) {
    const idx = vis[i];
    const n = NODES[idx];
    if (n.g === 1 && !showTags) continue;
    if (filterIds && !filterIds.has(idx)) continue;
    const x = sx(n.x), y = sy(n.y);
    const r = Math.max(2, n.r * zoom);
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = fillOf(n);
    ctx.fill();
    ctx.lineWidth = n.g ? 2 : 1.5;
    ctx.strokeStyle = selected === idx ? '#2563eb' : strokeOf(n);
    ctx.stroke();
    if (showLabels && r >= 8) {
      ctx.fillStyle = n.g ? '#0c4a6e' : '#0f172a';
      ctx.font = Math.max(8, Math.min(13, 11 * zoom)) + 'px Arial';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(n.l, x, y);
    }
    drawnN++;
  }
}

function hit(px, py) {
  const x = wx(px), y = wy(py);
  const cand = cellsInView(x - 80, y - 80, x + 80, y + 80);
  let best = -1, bestD = 1e12;
  for (const i of cand) {
    const n = NODES[i];
    if (n.g === 1 && !(tagsOn && zoom >= 0.38)) continue;
    if (filterIds && !filterIds.has(i)) continue;
    const dx = n.x - x, dy = n.y - y;
    const d = dx * dx + dy * dy;
    const lim = Math.max(n.r, 14 / zoom);
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
  requestDraw();
}
function showDetail(idx) {
  selected = idx;
  const n = NODES[idx];
  const m = META[idx] || [];
  const panel = document.getElementById('detail-panel');
  const content = document.getElementById('detail-content');
  let html = '<h2>' + esc(n.l) + '</h2>';
  if (n.g === 0) {
    html += '<div class="field-value"><b>[' + esc(m[0]) + ']</b> ' +
      (m[1] ? '← [' + esc(m[1]) + ']' : 'корневая') + '</div>';
    if (m[2]) html += '<div class="field"><div class="field-label">Тема</div><div class="field-value">' + esc(m[2]) + '</div></div>';
    html += '<div class="field"><div class="field-label">Тип</div><div class="field-value">' + esc(m[3]) + '</div></div>';
    html += '<div class="field"><div class="field-label">Теги</div><div class="field-value">' + esc(m[4] || '—') + '</div></div>';
    html += '<hr/><div class="field-value">' + esc(m[5]) + '</div>';
  } else {
    html += '<div class="field-value">🏷 ' + esc(m[2]) + '</div>';
    html += '<div class="field-label">' + esc(m[3]) + '</div>';
    html += '<div class="field-value">' + esc(m[5]) + '</div>';
  }
  const nbrs = adj[idx] || [];
  if (nbrs.length) {
    const entityNeighbors = [];
    const thoughtNeighbors = [];
    const limit = Math.min(nbrs.length, 40);
    for (let i = 0; i < limit; i++) {
      const link = nbrs[i];
      const ni = link.j;
      const nn = NODES[ni];
      const pm = META[ni] || [];
      let arrow = '';
      if (nn.g !== 1) {
        if (link.k === 0) {
          if (link.to === idx && link.from === ni) arrow = '→ ';
          else if (link.from === idx && link.to === ni) arrow = '← ';
        } else {
          arrow = link.from === idx ? '→ ' : '← ';
        }
      }
      const item = '<div class="neighbor-item" onclick="focusNode(' + ni + ')">' +
        arrow + '<b>' + esc(pm[6] || nn.l) + '</b></div>';
      if (nn.g === 1) entityNeighbors.push(item);
      else thoughtNeighbors.push(item);
    }
    html += '<hr/><div class="neighbors"><div class="field-label">Связи (' + nbrs.length + ')</div>' +
      entityNeighbors.join('') + thoughtNeighbors.join('');
    if (nbrs.length > limit) html += '<div class="field-label">ещё ' + (nbrs.length - limit) + '</div>';
    html += '</div>';
  }
  content.innerHTML = html;
  panel.style.display = 'block';
  requestDraw();
}
function focusNode(idx) {
  if (idx < 0 || idx >= NODES.length) return;
  const n = NODES[idx];
  if (n.g === 1) {
    tagsOn = true;
    const b = document.getElementById('btn-tags');
    if (b) b.classList.add('active');
  }
  camX = n.x; camY = n.y;
  zoom = Math.max(zoom, 0.9);
  showDetail(idx);
}

canvas.addEventListener('mousedown', function(e) {
  dragging = true; moved = false; lastX = e.clientX; lastY = e.clientY;
});
window.addEventListener('mouseup', function(e) {
  if (!dragging) return;
  dragging = false;
  if (!moved) {
    const rect = canvas.getBoundingClientRect();
    const i = hit(e.clientX - rect.left, e.clientY - rect.top);
    if (i >= 0) showDetail(i); else closeDetail();
  }
});
window.addEventListener('mousemove', function(e) {
  if (!dragging) return;
  const dx = e.clientX - lastX, dy = e.clientY - lastY;
  if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
  lastX = e.clientX; lastY = e.clientY;
  camX -= dx / zoom; camY -= dy / zoom;
  requestDraw();
});
canvas.addEventListener('wheel', function(e) {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const beforeX = wx(mx), beforeY = wy(my);
  const factor = e.deltaY > 0 ? 0.9 : 1.11;
  zoom = Math.max(0.03, Math.min(3.5, zoom * factor));
  camX = beforeX - (mx - W / 2) / zoom;
  camY = beforeY - (my - H / 2) / zoom;
  requestDraw();
}, { passive: false });
canvas.addEventListener('dblclick', function(e) {
  const rect = canvas.getBoundingClientRect();
  const i = hit(e.clientX - rect.left, e.clientY - rect.top);
  if (i >= 0) focusNode(i);
});

const searchIndex = META.map((m, i) => ({ i, hay: m[7] || '' }));
let searchTimer = 0;
function applySearch(raw) {
  const q = (raw || '').toLowerCase().trim();
  if (!q) { filterIds = null; requestDraw(); return; }
  const matched = new Set();
  for (const item of searchIndex) {
    if (item.hay.includes(q)) matched.add(item.i);
  }
  const visible = new Set(matched);
  matched.forEach(i => { (adj[i] || []).slice(0, 12).forEach(link => visible.add(link.j)); });
  filterIds = visible;
  if (matched.size === 1) {
    const only = [...matched][0];
    camX = NODES[only].x; camY = NODES[only].y;
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
if (tagsOn) tagsBtn.classList.add('active');
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
resize();
fit();
requestDraw();
</script>
</body>
</html>
"""


def _build_html(graph_data: Dict[str, Any], user_label: str = "") -> str:
    nodes, edges, meta, total_z, total_e, total_r = _pack_graph(graph_data)
    dump = {"ensure_ascii": False, "separators": (",", ":")}
    nodes_json = json.dumps(nodes, **dump).replace("<", "\\u003c")
    edges_json = json.dumps(edges, **dump).replace("<", "\\u003c")
    meta_json = json.dumps(meta, **dump).replace("<", "\\u003c")
    heading = html_escape((user_label or "").strip() or "Цифровой экзокортекс")
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
  #graph {{ width: 100%; height: calc(100vh - 56px); display: block; cursor: grab; }}
  #graph:active {{ cursor: grabbing; }}
  #detail-panel {{
    position: absolute; top: 64px; right: 16px; width: 340px; max-height: calc(100vh - 100px);
    background: rgba(15, 23, 42, 0.98); border: 1px solid #334155; border-radius: 12px;
    padding: 18px; overflow-y: auto; z-index: 10; display: none;
  }}
  #detail-panel h2 {{ color: #e2e8f0; margin-bottom: 10px; font-size: 15px; }}
  #detail-panel .field {{ margin: 6px 0; }}
  #detail-panel .field-label {{ color: #94a3b8; font-size: 12px; }}
  #detail-panel .field-value {{ color: #e2e8f0; font-size: 13px; line-height: 1.5; }}
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
  body.light-theme #detail-panel .field-label, body.light-theme #graph-hint {{ color: #6b7280; }}
  body.light-theme #detail-panel .neighbor-item {{ background: #f3f4f6; color: #374151; }}
  body.light-theme #search-box input {{ background: #ffffff; border: 1px solid #d1d5db; color: #374151; }}
</style>
</head>
<body class="light-theme">
<div id="header">
  <h1>{heading}</h1>
  <div id="header-right">
    <div id="stats">Мысли: <span>{total_z}</span> &nbsp;|&nbsp; Сущности: <span>{total_e}</span> &nbsp;|&nbsp; Связи: <span>{total_r}</span></div>
    <button id="theme-toggle" type="button">Темная тема</button>
  </div>
</div>
<div id="search-box">
  <input type="text" id="searchInput" placeholder="🔍 Найти мысль или сущность..."/>
  <div id="graph-controls">
    <button type="button" id="btn-tags">Теги</button>
  </div>
  <div id="graph-hint">Колесо — масштаб, перетаскивание — панорама. На большом графе теги лучше включать после приближения.</div>
</div>
<canvas id="graph"></canvas>
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
    html += _GRAPH_JS.replace("__NODES_JSON__", nodes_json).replace(
        "__EDGES_JSON__", edges_json
    ).replace("__META_JSON__", meta_json)
    return html


def render_graph_html(graph_data: Dict[str, Any], user_label: str = "") -> str:
    """Собирает HTML-граф в память — без временного файла."""
    return _build_html(graph_data, user_label=user_label)


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
