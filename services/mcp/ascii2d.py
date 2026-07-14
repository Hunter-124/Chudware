"""Deterministic ASCII / plaintext 2D representations for non-vision models.

Owner item 5 (2D translation half) — PCB layer grids, sketch polylines, and an
experimental single-view 3D silhouette. Every tool returns both an ASCII block
and an English summary, plus structured ``findings``.

Pure helpers accept plain dicts so tests run offline; MCP wrappers optionally
load live ECAD / sketch sessions when a doc_id is given.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Optional

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- util


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=False)


def _err(reason: str, hint: str = "", **extra: Any) -> str:
    out = {"ok": False, "error": str(reason), "hint": hint or str(reason)}
    out.update(extra)
    return _dumps(out)


def _xy(p: Any) -> tuple[float, float]:
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        return float(p[0]), float(p[1])
    if isinstance(p, dict):
        if "x" in p and "y" in p:
            return float(p["x"]), float(p["y"])
        if "u" in p and "v" in p:
            return float(p["u"]), float(p["v"])
    raise ValueError(f"not a 2D point: {p!r}")


def _clamp_int(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


# =========================================================================== PCB
# Layer ASCII grid: components as refdes boxes, traces as path chars, vias, pads.

_PCB_EMPTY = "."
_PCB_VIA = "o"
_PCB_PAD = "#"
_PCB_H = "-"
_PCB_V = "|"
_PCB_D1 = "/"   # rising (y grows with x in image coords → drawn inverted)
_PCB_D2 = "\\"
_PCB_CROSS = "X"
_PCB_JUNCT = "+"


def _board_bbox(board: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    outline = board.get("outline") or {}
    for sh in outline.get("shapes") or []:
        for key in ("p1", "p2", "start", "end"):
            p = sh.get(key) if isinstance(sh, dict) else None
            if p and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
        for p in (sh.get("points") or []) if isinstance(sh, dict) else []:
            if p and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
    for fp in board.get("footprints") or []:
        pos = fp.get("pos") or [0.0, 0.0]
        xs.append(float(pos[0])); ys.append(float(pos[1]))
        for pad in fp.get("pads") or []:
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
                xs.append(float(pos[0]) + px); ys.append(float(pos[1]) + py)
            except Exception:
                pass
    for tr in board.get("tracks") or []:
        for key in ("p1", "p2"):
            p = tr.get(key)
            if p and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
    for v in board.get("vias") or []:
        p = v.get("pos") or v.get("at")
        if p and len(p) >= 2:
            xs.append(float(p[0])); ys.append(float(p[1]))
    if not xs:
        return (0.0, 0.0, 10.0, 10.0)
    pad = 1.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _copper_layers(board: dict) -> list[str]:
    stack = board.get("stackup") or {}
    layers = []
    for L in stack.get("layers") or []:
        if isinstance(L, dict) and L.get("type") == "copper" and L.get("id"):
            layers.append(str(L["id"]))
    if not layers:
        # fall back to layers seen on tracks
        seen: list[str] = []
        for tr in board.get("tracks") or []:
            ly = tr.get("layer") or "F.Cu"
            if ly not in seen:
                seen.append(ly)
        layers = seen or ["F.Cu"]
    return layers


def _world_fp_aabb(fp: dict) -> tuple[float, float, float, float]:
    """World AABB of courtyard (graphics) or pads; mirrors place._fp_extent_local."""
    pos = fp.get("pos") or [0.0, 0.0]
    ox, oy = float(pos[0]), float(pos[1])
    xs: list[float] = []
    ys: list[float] = []
    for g in fp.get("graphics") or []:
        if g.get("layer") not in ("F.CrtYd", "B.CrtYd", None):
            # only courtyard layers when present; if graphics lack layer, still use
            if g.get("layer") not in ("F.CrtYd", "B.CrtYd"):
                continue
        gtype = g.get("type")
        if gtype == "line":
            for key in ("p1", "p2"):
                p = g.get(key)
                if p and len(p) >= 2:
                    xs.append(ox + float(p[0])); ys.append(oy + float(p[1]))
        elif gtype == "rect":
            p1 = g.get("p1") or g.get("start")
            p2 = g.get("p2") or g.get("end")
            if p1 and p2:
                xs.extend([ox + float(p1[0]), ox + float(p2[0])])
                ys.extend([oy + float(p1[1]), oy + float(p2[1])])
        elif gtype == "circle":
            c = g.get("center") or [0.0, 0.0]
            r = float(g.get("radius", 0.0))
            xs.extend([ox + float(c[0]) - r, ox + float(c[0]) + r])
            ys.extend([oy + float(c[1]) - r, oy + float(c[1]) + r])
        elif gtype == "poly" and g.get("points"):
            for p in g["points"]:
                xs.append(ox + float(p[0])); ys.append(oy + float(p[1]))
    if not xs:
        for pad in fp.get("pads") or []:
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            size = pad.get("size") or [1.0, 1.0]
            w = float(size[0]); h = float(size[1] if len(size) > 1 else size[0])
            xs.extend([ox + px - w / 2.0, ox + px + w / 2.0])
            ys.extend([oy + py - h / 2.0, oy + py + h / 2.0])
    if not xs:
        return (ox - 1.0, oy - 1.0, ox + 1.0, oy + 1.0)
    # small courtyard margin so bare pads still occupy a cell
    m = 0.25
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _aabb_overlap(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)


def _on_segment(ax: float, ay: float, bx: float, by: float, cx: float, cy: float,
                eps: float = 1e-9) -> bool:
    return (min(ax, cx) - eps <= bx <= max(ax, cx) + eps and
            min(ay, cy) - eps <= by <= max(ay, cy) + eps)


def segments_cross(a1: tuple[float, float], a2: tuple[float, float],
                   b1: tuple[float, float], b2: tuple[float, float],
                   *, share_endpoint_ok: bool = True) -> bool:
    """Proper intersection of open segments (shared endpoint alone is not a cross)."""
    ax, ay = a1; bx, by = a2; cx, cy = b1; dx, dy = b2
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)

    def _z(v: float) -> bool:
        return abs(v) < 1e-9

    # shared endpoint (or collinear endpoint touch) — not an "unintended cross"
    if share_endpoint_ok:
        for p in (a1, a2):
            for q in (b1, b2):
                if abs(p[0] - q[0]) < 1e-9 and abs(p[1] - q[1]) < 1e-9:
                    return False

    if ((o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0)) and \
       ((o3 > 0 and o4 < 0) or (o3 < 0 and o4 > 0)):
        return True

    # collinear overlaps count as crossings for different nets
    if _z(o1) and _on_segment(ax, ay, cx, cy, bx, by):
        return True
    if _z(o2) and _on_segment(ax, ay, dx, dy, bx, by):
        return True
    if _z(o3) and _on_segment(cx, cy, ax, ay, dx, dy):
        return True
    if _z(o4) and _on_segment(cx, cy, bx, by, dx, dy):
        return True
    return False


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    pts: list[tuple[int, int]] = []
    dx = abs(x1 - x0); sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0); sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        pts.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy; x += sx
        if e2 <= dx:
            err += dx; y += sy
    return pts


def _trace_char(x0: int, y0: int, x1: int, y1: int) -> str:
    dx = x1 - x0; dy = y1 - y0
    if dx == 0 and dy == 0:
        return _PCB_JUNCT
    if abs(dx) >= 2 * abs(dy):
        return _PCB_H
    if abs(dy) >= 2 * abs(dx):
        return _PCB_V
    # image y increases downward; world y increases upward — map after flip
    # in grid, y grows down so rising world slope appears as '\'
    if (dx > 0 and dy > 0) or (dx < 0 and dy < 0):
        return _PCB_D2
    return _PCB_D1


def _paint(grid: list[list[str]], x: int, y: int, ch: str,
           net_grid: list[list[Optional[str]]], net: Optional[str]) -> None:
    h = len(grid); w = len(grid[0]) if h else 0
    if not (0 <= x < w and 0 <= y < h):
        return
    prev = grid[y][x]
    prev_net = net_grid[y][x]
    if prev == _PCB_EMPTY:
        grid[y][x] = ch
        net_grid[y][x] = net
        return
    # same-net junction
    if prev_net is not None and net is not None and prev_net == net:
        if prev != ch and prev not in (_PCB_PAD, _PCB_VIA) and ch not in (_PCB_PAD, _PCB_VIA):
            grid[y][x] = _PCB_JUNCT
        elif ch in (_PCB_PAD, _PCB_VIA):
            grid[y][x] = ch
        return
    # different nets → mark cross (unless one is pad/via of the same cell)
    if prev_net is not None and net is not None and prev_net != net:
        grid[y][x] = _PCB_CROSS
        return
    # overwrite empty-ish with stronger glyph
    priority = {_PCB_EMPTY: 0, _PCB_H: 1, _PCB_V: 1, _PCB_D1: 1, _PCB_D2: 1,
                _PCB_JUNCT: 2, _PCB_VIA: 3, _PCB_PAD: 4, _PCB_CROSS: 5}
    if priority.get(ch, 0) >= priority.get(prev, 0):
        grid[y][x] = ch
        if net is not None:
            net_grid[y][x] = net


def _to_cell(x: float, y: float, xmin: float, ymax: float, pitch: float,
             w: int, h: int) -> tuple[int, int]:
    # flip Y so north is up in the printed block
    cx = _clamp_int((x - xmin) / pitch, 0, w - 1)
    cy = _clamp_int((ymax - y) / pitch, 0, h - 1)
    return cx, cy


def render_board_layer_ascii(board: dict, layer: str = "F.Cu",
                             pitch_mm: float = 1.0,
                             max_dim: int = 80) -> dict:
    """Render one copper layer of *board* to a deterministic ASCII grid."""
    if pitch_mm <= 0:
        pitch_mm = 1.0
    xmin, ymin, xmax, ymax = _board_bbox(board)
    bw = max(xmax - xmin, pitch_mm)
    bh = max(ymax - ymin, pitch_mm)
    # auto-coarsen pitch so the grid stays readable
    pitch = pitch_mm
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)

    grid = [[_PCB_EMPTY for _ in range(w)] for _ in range(h)]
    net_grid: list[list[Optional[str]]] = [[None for _ in range(w)] for _ in range(h)]

    # tracks on this layer
    for tr in board.get("tracks") or []:
        if (tr.get("layer") or "F.Cu") != layer:
            continue
        p1 = tr.get("p1"); p2 = tr.get("p2")
        if not (p1 and p2):
            continue
        x0, y0 = _to_cell(float(p1[0]), float(p1[1]), xmin, ymax, pitch, w, h)
        x1, y1 = _to_cell(float(p2[0]), float(p2[1]), xmin, ymax, pitch, w, h)
        ch = _trace_char(x0, y0, x1, y1)
        net = tr.get("net") or None
        for cx, cy in _bresenham(x0, y0, x1, y1):
            _paint(grid, cx, cy, ch, net_grid, net)

    # vias (all layers)
    for v in board.get("vias") or []:
        p = v.get("pos") or v.get("at")
        if not p:
            continue
        cx, cy = _to_cell(float(p[0]), float(p[1]), xmin, ymax, pitch, w, h)
        _paint(grid, cx, cy, _PCB_VIA, net_grid, v.get("net"))

    # pads + component boxes on this copper layer (top side → F.Cu, bottom → B.Cu)
    side_ok = {"F.Cu": "top", "B.Cu": "bottom"}
    want_side = side_ok.get(layer)
    for fp in board.get("footprints") or []:
        side = (fp.get("side") or "top").lower()
        if want_side and side not in (want_side, "both"):
            # still draw if pad layers include this copper
            pass
        pos = fp.get("pos") or [0.0, 0.0]
        ox, oy = float(pos[0]), float(pos[1])
        for pad in fp.get("pads") or []:
            layers = pad.get("layers") or [layer]
            if layer not in layers and "F.Cu" not in layers and "B.Cu" not in layers:
                # permissive: if layers empty, treat as this layer
                if pad.get("layers"):
                    continue
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            cx, cy = _to_cell(ox + px, oy + py, xmin, ymax, pitch, w, h)
            _paint(grid, cx, cy, _PCB_PAD, net_grid, pad.get("net"))

        # stamp refdes into courtyard AABB (deterministic: top-left cell)
        ax0, ay0, ax1, ay1 = _world_fp_aabb(fp)
        c0x, c0y = _to_cell(ax0, ay1, xmin, ymax, pitch, w, h)  # top-left in image
        c1x, c1y = _to_cell(ax1, ay0, xmin, ymax, pitch, w, h)  # bottom-right
        x_lo, x_hi = min(c0x, c1x), max(c0x, c1x)
        y_lo, y_hi = min(c0y, c1y), max(c0y, c1y)
        ref = str(fp.get("ref") or fp.get("id") or "?")
        # draw box border with refdes chars inside top edge
        for x in range(x_lo, x_hi + 1):
            for y in range(y_lo, y_hi + 1):
                edge = (x in (x_lo, x_hi) or y in (y_lo, y_hi))
                if not edge:
                    continue
                if 0 <= x < w and 0 <= y < h and grid[y][x] == _PCB_EMPTY:
                    grid[y][x] = "+"
        # write refdes on the top border (left-justified, truncate)
        for i, ch in enumerate(ref[: max(1, x_hi - x_lo)]):
            x = x_lo + i
            y = y_lo
            if 0 <= x < w and 0 <= y < h:
                grid[y][x] = ch

    rows = ["".join(r) for r in grid]
    ascii_block = "\n".join(rows)
    return {
        "layer": layer,
        "pitch_mm": pitch,
        "width": w,
        "height": h,
        "bbox_mm": [xmin, ymin, xmax, ymax],
        "ascii": ascii_block,
    }


def _pad_nodes(board: dict) -> dict[str, list[str]]:
    """net → list of pad node ids (ref.pin)."""
    out: dict[str, list[str]] = {}
    for fp in board.get("footprints") or []:
        ref = fp.get("ref") or fp.get("id") or "?"
        for pad in fp.get("pads") or []:
            net = pad.get("net") or ""
            if not net:
                continue
            num = str(pad.get("number") or pad.get("pin") or pad.get("id") or "?")
            out.setdefault(net, []).append(f"{ref}.{num}")
    return out


def check_board(board: dict) -> list[dict]:
    """Structured findings: unconnected nets, same-layer crossings, courtyard overlaps."""
    findings: list[dict] = []

    # --- unconnected named nets (multi-pad with no copper path) ---------------
    # Build undirected graph of copper endpoints (pad centers + track ends + vias)
    # keyed by rounded mm so floating noise does not split nodes.
    def _key(x: float, y: float) -> tuple[float, float]:
        return (round(x, 3), round(y, 3))

    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def _find(a: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def _union(a: tuple[float, float], b: tuple[float, float]) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    pad_pos: dict[str, list[tuple[str, tuple[float, float]]]] = {}  # net → [(node, key)]
    for fp in board.get("footprints") or []:
        ref = fp.get("ref") or fp.get("id") or "?"
        pos = fp.get("pos") or [0.0, 0.0]
        ox, oy = float(pos[0]), float(pos[1])
        for pad in fp.get("pads") or []:
            net = pad.get("net") or ""
            if not net:
                continue
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            k = _key(ox + px, oy + py)
            parent.setdefault(k, k)
            num = str(pad.get("number") or pad.get("pin") or pad.get("id") or "?")
            pad_pos.setdefault(net, []).append((f"{ref}.{num}", k))

    for tr in board.get("tracks") or []:
        p1 = tr.get("p1"); p2 = tr.get("p2")
        if not (p1 and p2):
            continue
        a = _key(float(p1[0]), float(p1[1]))
        b = _key(float(p2[0]), float(p2[1]))
        parent.setdefault(a, a); parent.setdefault(b, b)
        _union(a, b)
        # also attach to net via endpoint proximity is implicit through pad keys

    for v in board.get("vias") or []:
        p = v.get("pos") or v.get("at")
        if not p:
            continue
        k = _key(float(p[0]), float(p[1]))
        parent.setdefault(k, k)

    # For each net with ≥2 pads: if pad keys not all in one component via tracks that
    # carry that net, flag unconnected. Restrict unions to same-net tracks.
    # Rebuild per-net connectivity:
    named_nets = []
    for n in board.get("nets") or []:
        if isinstance(n, dict) and n.get("name"):
            named_nets.append(str(n["name"]))
        elif isinstance(n, str):
            named_nets.append(n)
    # include nets only present on pads
    for net in pad_pos:
        if net not in named_nets:
            named_nets.append(net)

    for net in sorted(named_nets):
        pads = pad_pos.get(net) or []
        if len(pads) < 2:
            # single-pad named net is a stub — flag as unconnected only if named & multi? skip stubs
            continue
        # per-net UF
        pmap: dict[tuple[float, float], tuple[float, float]] = {}

        def find(a: tuple[float, float]) -> tuple[float, float]:
            pmap.setdefault(a, a)
            while pmap[a] != a:
                pmap[a] = pmap[pmap[a]]
                a = pmap[a]
            return a

        def union(a: tuple[float, float], b: tuple[float, float]) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                pmap[rb] = ra

        for _, k in pads:
            pmap.setdefault(k, k)
        for tr in board.get("tracks") or []:
            if (tr.get("net") or "") != net:
                continue
            p1 = tr.get("p1"); p2 = tr.get("p2")
            if not (p1 and p2):
                continue
            a = _key(float(p1[0]), float(p1[1])); b = _key(float(p2[0]), float(p2[1]))
            union(a, b)
            # snap near pads (within 0.5 mm) onto the track ends
            for _, pk in pads:
                if math.hypot(pk[0] - a[0], pk[1] - a[1]) <= 0.5:
                    union(pk, a)
                if math.hypot(pk[0] - b[0], pk[1] - b[1]) <= 0.5:
                    union(pk, b)
        for v in board.get("vias") or []:
            if (v.get("net") or "") != net:
                continue
            p = v.get("pos") or v.get("at")
            if not p:
                continue
            vk = _key(float(p[0]), float(p[1]))
            pmap.setdefault(vk, vk)
            for _, pk in pads:
                if math.hypot(pk[0] - vk[0], pk[1] - vk[1]) <= 0.5:
                    union(pk, vk)

        roots = {find(k) for _, k in pads}
        if len(roots) > 1:
            findings.append({
                "code": "unconnected_net",
                "severity": "error",
                "net": net,
                "pads": [n for n, _ in pads],
                "components": len(roots),
                "message": (f"Named net {net!r} has {len(pads)} pads in "
                            f"{len(roots)} disconnected copper islands"),
            })

    # --- same-layer unintended crossings (different nets) --------------------
    tracks = board.get("tracks") or []
    for i in range(len(tracks)):
        ti = tracks[i]
        li = ti.get("layer") or "F.Cu"
        ni = ti.get("net") or ""
        p1 = ti.get("p1"); p2 = ti.get("p2")
        if not (p1 and p2):
            continue
        a1 = (float(p1[0]), float(p1[1])); a2 = (float(p2[0]), float(p2[1]))
        for j in range(i + 1, len(tracks)):
            tj = tracks[j]
            if (tj.get("layer") or "F.Cu") != li:
                continue
            nj = tj.get("net") or ""
            if ni and nj and ni == nj:
                continue
            q1 = tj.get("p1"); q2 = tj.get("p2")
            if not (q1 and q2):
                continue
            b1 = (float(q1[0]), float(q1[1])); b2 = (float(q2[0]), float(q2[1]))
            if segments_cross(a1, a2, b1, b2):
                findings.append({
                    "code": "trace_crossing",
                    "severity": "error",
                    "layer": li,
                    "nets": sorted({n for n in (ni, nj) if n}),
                    "tracks": [ti.get("id") or f"t{i}", tj.get("id") or f"t{j}"],
                    "message": (f"Unintended crossing on {li}: "
                                f"net {ni or '?'} track {ti.get('id') or i} crosses "
                                f"net {nj or '?'} track {tj.get('id') or j}"),
                })

    # --- overlapping courtyards ----------------------------------------------
    fps = board.get("footprints") or []
    boxes = []
    for fp in fps:
        boxes.append((fp.get("ref") or fp.get("id") or "?", _world_fp_aabb(fp)))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _aabb_overlap(boxes[i][1], boxes[j][1]):
                findings.append({
                    "code": "courtyard_overlap",
                    "severity": "warning",
                    "refs": [boxes[i][0], boxes[j][0]],
                    "message": (f"Overlapping component courtyards: "
                                f"{boxes[i][0]} and {boxes[j][0]}"),
                })

    # stable order
    findings.sort(key=lambda f: (f.get("code", ""), f.get("message", "")))
    return findings


def summarize_board(board: dict, findings: list[dict],
                    layers_rendered: list[str]) -> str:
    fps = board.get("footprints") or []
    tracks = board.get("tracks") or []
    vias = board.get("vias") or []
    nets = board.get("nets") or []
    net_names = []
    for n in nets:
        if isinstance(n, dict) and n.get("name"):
            net_names.append(str(n["name"]))
        elif isinstance(n, str):
            net_names.append(n)
    pad_nets = _pad_nodes(board)
    lines = [
        f"Board {board.get('name') or board.get('id') or '(unnamed)'}: "
        f"{len(fps)} footprint(s), {len(tracks)} track(s), {len(vias)} via(s), "
        f"{len(net_names)} named net(s).",
        f"Copper layers rendered: {', '.join(layers_rendered) or '(none)'}.",
    ]
    if pad_nets:
        lines.append("Net connectivity:")
        for net in sorted(pad_nets):
            pads = pad_nets[net]
            lines.append(f"  {net}: {len(pads)} pad(s) — {', '.join(pads[:8])}"
                         + (f" … +{len(pads)-8}" if len(pads) > 8 else ""))
    else:
        lines.append("Net connectivity: no netted pads.")

    n_cross = sum(1 for f in findings if f.get("code") == "trace_crossing")
    n_unc = sum(1 for f in findings if f.get("code") == "unconnected_net")
    n_ov = sum(1 for f in findings if f.get("code") == "courtyard_overlap")
    if not findings:
        lines.append("Checks: clean — no unconnected multi-pad nets, no same-layer "
                     "crossings, no courtyard overlaps.")
    else:
        lines.append(f"Checks: {n_unc} unconnected net(s), {n_cross} same-layer "
                     f"crossing(s), {n_ov} courtyard overlap(s).")
        for f in findings[:12]:
            lines.append(f"  [{f.get('severity','?')}] {f.get('message')}")
        if len(findings) > 12:
            lines.append(f"  … and {len(findings) - 12} more finding(s)")
    return "\n".join(lines)


def board_ascii_report(board: dict, layer: Optional[str] = None,
                       pitch_mm: float = 1.0) -> dict:
    """Full PCB report: per-layer ascii + summary + findings."""
    layers = [layer] if layer else _copper_layers(board)
    # de-dupe preserve order
    seen: set[str] = set()
    layers = [L for L in layers if not (L in seen or seen.add(L))]  # type: ignore[func-returns-value]
    layer_blocks = []
    ascii_parts = []
    for L in layers:
        block = render_board_layer_ascii(board, layer=L, pitch_mm=pitch_mm)
        layer_blocks.append(block)
        ascii_parts.append(f"[layer {L} pitch={block['pitch_mm']:.3g}mm "
                           f"{block['width']}x{block['height']}]\n{block['ascii']}")
    findings = check_board(board)
    summary = summarize_board(board, findings, layers)
    ascii_all = "\n\n".join(ascii_parts)
    return {
        "ok": True,
        "kind": "pcb",
        "ascii": ascii_all,
        "summary": summary,
        "findings": findings,
        "layers": layer_blocks,
        "pitch_mm": pitch_mm,
    }


# ========================================================================= SKETCH


def _entity_polyline(ent: dict, n_arc: int = 24) -> list[tuple[float, float]]:
    """Control-point / sampled polyline for one sketch entity (deterministic)."""
    etype = (ent.get("type") or "").lower()
    # support both authored (p1/p2) and solved-coords shapes
    coords = ent.get("coords") or ent.get("solved_coords") or {}

    def g(*keys, default=None):
        for k in keys:
            if k in ent and ent[k] is not None:
                return ent[k]
            if k in coords and coords[k] is not None:
                return coords[k]
        return default

    if etype == "line":
        p1 = g("p1", "start")
        p2 = g("p2", "end")
        if p1 is not None and p2 is not None:
            return [_xy(p1), _xy(p2)]
        x1 = g("start_x", "x1", default=0.0)
        y1 = g("start_y", "y1", default=0.0)
        x2 = g("end_x", "x2", default=0.0)
        y2 = g("end_y", "y2", default=0.0)
        return [(float(x1), float(y1)), (float(x2), float(y2))]

    if etype == "circle":
        c = g("center", "ctr")
        if c is not None:
            cx, cy = _xy(c)
        else:
            cx = float(g("center_x", "cx", default=0.0))
            cy = float(g("center_y", "cy", default=0.0))
        r = float(g("radius", "r", default=1.0))
        pts = []
        for i in range(n_arc + 1):
            a = 2.0 * math.pi * i / n_arc
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    if etype == "arc":
        c = g("center", "ctr")
        if c is not None:
            cx, cy = _xy(c)
        else:
            cx = float(g("center_x", "cx", default=0.0))
            cy = float(g("center_y", "cy", default=0.0))
        r = float(g("radius", "r", default=1.0))
        a0 = g("start", "start_angle", "a1", default=0.0)
        a1 = g("end", "end_angle", "a2", default=math.pi)
        a0 = float(a0); a1 = float(a1)
        # degrees if |a| > 2π roughly and looks like degrees
        if abs(a0) > 2 * math.pi + 0.1 or abs(a1) > 2 * math.pi + 0.1:
            a0 = math.radians(a0); a1 = math.radians(a1)
        # sweep CCW from a0 to a1
        sweep = a1 - a0
        if sweep <= 0:
            sweep += 2 * math.pi
        n = max(4, int(round(n_arc * abs(sweep) / (2 * math.pi))))
        pts = []
        for i in range(n + 1):
            a = a0 + sweep * i / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    if etype in ("spline", "polyline", "eq_curve"):
        pts = g("points", "control_points", "pts", default=[]) or []
        out = []
        for p in pts:
            try:
                out.append(_xy(p))
            except Exception:
                continue
        return out

    if etype == "point":
        p = g("p", "pos")
        if p is not None:
            return [_xy(p)]
        return [(float(g("x", default=0.0)), float(g("y", default=0.0)))]

    return []


def _sketch_bbox(polylines: list[list[tuple[float, float]]]
                 ) -> tuple[float, float, float, float]:
    xs: list[float] = []; ys: list[float] = []
    for pl in polylines:
        for x, y in pl:
            xs.append(x); ys.append(y)
    if not xs:
        return (0.0, 0.0, 10.0, 10.0)
    pad = 1.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def render_sketch_ascii(entities: list[dict], pitch: float = 0.5,
                        max_dim: int = 60) -> dict:
    """ASCII render of sketch entities as control-point polylines."""
    if pitch <= 0:
        pitch = 0.5
    polys = []
    labels: list[tuple[int, int, str]] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if ent.get("construction"):
            continue
        pl = _entity_polyline(ent)
        if not pl:
            continue
        polys.append(pl)

    xmin, ymin, xmax, ymax = _sketch_bbox(polys)
    bw = max(xmax - xmin, pitch); bh = max(ymax - ymin, pitch)
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)

    grid = [[" " for _ in range(w)] for _ in range(h)]

    def cell(x: float, y: float) -> tuple[int, int]:
        return (_clamp_int((x - xmin) / pitch, 0, w - 1),
                _clamp_int((ymax - y) / pitch, 0, h - 1))

    for ent, pl in zip(
            [e for e in entities if isinstance(e, dict) and not e.get("construction")
             and _entity_polyline(e)],
            polys):
        if len(pl) == 1:
            cx, cy = cell(pl[0][0], pl[0][1])
            grid[cy][cx] = "+"
        else:
            for i in range(len(pl) - 1):
                x0, y0 = cell(pl[i][0], pl[i][1])
                x1, y1 = cell(pl[i + 1][0], pl[i + 1][1])
                ch = _trace_char(x0, y0, x1, y1)
                for cx, cy in _bresenham(x0, y0, x1, y1):
                    if grid[cy][cx] in (" ", "."):
                        grid[cy][cx] = ch
                    elif grid[cy][cx] != ch:
                        grid[cy][cx] = "+"
        # label near first point
        eid = str(ent.get("id") or "")
        if eid and pl:
            cx, cy = cell(pl[0][0], pl[0][1])
            labels.append((cx, max(0, cy - 1), eid[:4]))

    for cx, cy, lab in labels:
        for i, ch in enumerate(lab):
            x = cx + i
            if 0 <= x < w and 0 <= cy < h and grid[cy][x] == " ":
                grid[cy][x] = ch

    # empty → '.'
    for y in range(h):
        for x in range(w):
            if grid[y][x] == " ":
                grid[y][x] = "."

    ascii_block = "\n".join("".join(r) for r in grid)
    return {
        "ascii": ascii_block,
        "pitch": pitch,
        "width": w,
        "height": h,
        "bbox": [xmin, ymin, xmax, ymax],
        "entity_count": len(polys),
    }


def _endpoints(ent: dict) -> list[tuple[float, float]]:
    etype = (ent.get("type") or "").lower()
    pl = _entity_polyline(ent)
    if not pl:
        return []
    if etype == "circle":
        return []  # closed by definition
    if etype == "point":
        return [pl[0]]
    if len(pl) == 1:
        return [pl[0]]
    return [pl[0], pl[-1]]


def check_sketch_geometry(entities: list[dict],
                          *, near_tol: float = 1e-2,
                          health: Optional[dict] = None) -> list[dict]:
    """Open profiles, near-coincident endpoints, under-dimensioned entities.

    When *health* (sketch_health result) is provided, open-profile and under-
    dimensioned findings prefer that source (wrap, don't re-solve).
    """
    findings: list[dict] = []
    ents = [e for e in entities if isinstance(e, dict) and not e.get("construction")]

    # --- open profiles / dangling endpoints ----------------------------------
    if health and isinstance(health.get("closure"), dict):
        open_eps = health["closure"].get("open_endpoints") or []
        if open_eps:
            findings.append({
                "code": "open_profile",
                "severity": "error",
                "open_endpoints": list(open_eps),
                "message": (f"Open profile: {len(open_eps)} dangling endpoint(s) "
                            f"at {', '.join(map(str, open_eps[:6]))}"),
                "source": "sketch_health",
            })
    else:
        # chain endpoints with union-find; degree-1 nodes are open
        keys: list[tuple[float, float]] = []
        ep_owner: list[tuple[str, tuple[float, float]]] = []
        for e in ents:
            eid = str(e.get("id") or "?")
            for p in _endpoints(e):
                k = (round(p[0], 4), round(p[1], 4))
                keys.append(k)
                ep_owner.append((eid, k))
        parent: dict[tuple[float, float], tuple[float, float]] = {}

        def find(a: tuple[float, float]) -> tuple[float, float]:
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: tuple[float, float], b: tuple[float, float]) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for k in keys:
            parent.setdefault(k, k)
        # merge near-equal keys
        uniq = sorted(set(keys))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                if math.hypot(uniq[i][0] - uniq[j][0],
                              uniq[i][1] - uniq[j][1]) <= near_tol:
                    union(uniq[i], uniq[j])
        degree: dict[tuple[float, float], int] = {}
        for _, k in ep_owner:
            r = find(k)
            degree[r] = degree.get(r, 0) + 1
        dangling = [k for k, d in degree.items() if d == 1]
        # circles alone don't create dangling; pure closed loops have degree 2
        if dangling and ents:
            findings.append({
                "code": "open_profile",
                "severity": "error",
                "open_endpoints": [list(k) for k in sorted(dangling)],
                "message": (f"Open profile: {len(dangling)} dangling endpoint(s) "
                            f"(near_tol={near_tol})"),
                "source": "geometry",
            })

    # --- near-coincident endpoints (distinct but within 10x near_tol, not merged) --
    eps_list: list[tuple[str, float, float]] = []
    for e in ents:
        eid = str(e.get("id") or "?")
        for p in _endpoints(e):
            eps_list.append((eid, p[0], p[1]))
    near_pairs = []
    for i in range(len(eps_list)):
        for j in range(i + 1, len(eps_list)):
            ei, xi, yi = eps_list[i]
            ej, xj, yj = eps_list[j]
            if ei == ej:
                continue
            d = math.hypot(xi - xj, yi - yj)
            if 1e-9 < d <= near_tol * 10 and d > near_tol:
                # close but not snapped — near-coincident
                pair = tuple(sorted([ei, ej]))
                near_pairs.append((pair, d, (xi, yi), (xj, yj)))
            elif 1e-12 < d <= near_tol:
                # within snap tol but still distinct ids — also report
                pair = tuple(sorted([ei, ej]))
                near_pairs.append((pair, d, (xi, yi), (xj, yj)))
    # de-dupe by entity pair
    seen_pairs: set[tuple[str, str]] = set()
    for pair, d, p, q in sorted(near_pairs, key=lambda t: (t[0], t[1])):
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        findings.append({
            "code": "near_coincident",
            "severity": "warning",
            "entities": list(pair),
            "distance": round(d, 6),
            "message": (f"Near-coincident endpoints between {pair[0]} and {pair[1]} "
                        f"(d={d:.4g}); run sketch_auto_constrain"),
        })

    # --- under-dimensioned ---------------------------------------------------
    if health is not None:
        dof = health.get("dof")
        status = health.get("status")
        if dof is not None and (dof > 0 or status in ("under", "unknown")):
            findings.append({
                "code": "under_dimensioned",
                "severity": "warning",
                "dof": dof,
                "status": status,
                "message": (f"Sketch under-dimensioned: dof={dof}, status={status!r}. "
                            f"{(health.get('dof_narration') or '')}".strip()),
                "source": "sketch_health",
            })
        # also surface per-entity under status if present
        statuses = health.get("statuses") or {}
        under_ents = [k for k, v in statuses.items() if v == "under"]
        if under_ents:
            findings.append({
                "code": "under_dimensioned_entities",
                "severity": "info",
                "entities": under_ents,
                "message": f"Under-constrained entities: {', '.join(map(str, under_ents[:12]))}",
                "source": "sketch_health",
            })
    else:
        # offline: any non-construction non-point entity without a matching dimension
        # is reported as potentially free — weak heuristic only
        dims = []  # caller may pass via entities container
        _ = dims
        free = [str(e.get("id") or "?") for e in ents
                if (e.get("type") or "").lower() not in ("point",)]
        if free:
            findings.append({
                "code": "under_dimensioned",
                "severity": "info",
                "entities": free,
                "message": (f"{len(free)} entity(ies) present without sketch_health; "
                            "pass health or call sketch_health for DOF"),
                "source": "heuristic",
            })

    findings.sort(key=lambda f: (f.get("code", ""), f.get("message", "")))
    return findings


def summarize_sketch(entities: list[dict], constraints: list[dict],
                     findings: list[dict], health: Optional[dict] = None) -> str:
    type_counts: dict[str, int] = {}
    for e in entities:
        if not isinstance(e, dict):
            continue
        t = (e.get("type") or "?").lower()
        type_counts[t] = type_counts.get(t, 0) + 1
    rel_counts: dict[str, int] = {}
    dims: list[str] = []
    for c in constraints or []:
        if not isinstance(c, dict):
            continue
        ct = c.get("type") or "?"
        if ct in ("distance", "angle", "radius", "diameter", "horizontal_distance",
                  "vertical_distance"):
            val = c.get("value")
            dims.append(f"{ct}({c.get('id', '?')}={val})")
        else:
            rel_counts[ct] = rel_counts.get(ct, 0) + 1

    lines = [
        f"Sketch: {sum(type_counts.values())} entit(y/ies) "
        f"({', '.join(f'{n} {t}' for t, n in sorted(type_counts.items())) or 'none'}).",
    ]
    if dims:
        lines.append("Dimensions: " + "; ".join(dims[:12])
                     + (f" … +{len(dims)-12}" if len(dims) > 12 else ""))
    else:
        lines.append("Dimensions: (none listed)")
    if rel_counts:
        lines.append("Constraints: " + ", ".join(f"{n}× {t}" for t, n in sorted(rel_counts.items())))
    else:
        lines.append("Constraints: (none listed)")

    if health is not None:
        try:
            from mcp_server.gsl_tools import sketch_health_text  # type: ignore
            lines.append("")
            lines.append(sketch_health_text(health))
        except Exception:
            lines.append(f"Health: status={health.get('status')!r} dof={health.get('dof')}")

    if findings:
        lines.append(f"Findings ({len(findings)}):")
        for f in findings[:12]:
            lines.append(f"  [{f.get('severity','?')}] {f.get('message')}")
    else:
        lines.append("Findings: none")
    return "\n".join(lines)


def sketch_ascii_report(sketch: dict, *, pitch: float = 0.5,
                        health: Optional[dict] = None,
                        near_tol: float = 1e-2) -> dict:
    """Full sketch report from a dict with entities/constraints (and optional health)."""
    entities = list(sketch.get("entities") or [])
    constraints = list(sketch.get("constraints") or [])
    if health is None and isinstance(sketch.get("health"), dict):
        health = sketch["health"]
    rendered = render_sketch_ascii(entities, pitch=pitch)
    findings = check_sketch_geometry(entities, near_tol=near_tol, health=health)
    summary = summarize_sketch(entities, constraints, findings, health=health)
    return {
        "ok": True,
        "kind": "sketch",
        "ascii": rendered["ascii"],
        "summary": summary,
        "findings": findings,
        "pitch": rendered["pitch"],
        "width": rendered["width"],
        "height": rendered["height"],
        "bbox": rendered["bbox"],
    }


# ============================================================== 3D experimental


def _project_polylines_ascii(polylines: list[list[tuple[float, float]]],
                             pitch: float = 1.0, max_dim: int = 70) -> dict:
    xmin, ymin, xmax, ymax = _sketch_bbox(polylines)
    bw = max(xmax - xmin, pitch); bh = max(ymax - ymin, pitch)
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)
    grid = [["." for _ in range(w)] for _ in range(h)]

    def cell(x: float, y: float) -> tuple[int, int]:
        return (_clamp_int((x - xmin) / pitch, 0, w - 1),
                _clamp_int((ymax - y) / pitch, 0, h - 1))

    for pl in polylines:
        if len(pl) < 2:
            if pl:
                cx, cy = cell(pl[0][0], pl[0][1])
                grid[cy][cx] = "+"
            continue
        for i in range(len(pl) - 1):
            x0, y0 = cell(pl[i][0], pl[i][1])
            x1, y1 = cell(pl[i + 1][0], pl[i + 1][1])
            ch = _trace_char(x0, y0, x1, y1)
            for cx, cy in _bresenham(x0, y0, x1, y1):
                if grid[cy][cx] == ".":
                    grid[cy][cx] = ch
                elif grid[cy][cx] != ch:
                    grid[cy][cx] = "+"
    return {
        "ascii": "\n".join("".join(r) for r in grid),
        "pitch": pitch,
        "width": w,
        "height": h,
        "bbox": [xmin, ymin, xmax, ymax],
    }


def _view_basis(view: str) -> tuple[tuple[float, float, float],
                                    tuple[float, float, float],
                                    tuple[float, float, float]]:
    """Return (view_dir, screen_x, screen_y) unit-ish axes for named views."""
    v = (view or "front").lower()
    # screen_y is "up" on the page
    if v == "front":
        return (0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    if v == "back":
        return (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    if v == "right":
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    if v == "left":
        return (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)
    if v == "top":
        return (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    if v == "bottom":
        return (0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)
    # iso
    d = (1.0, -1.0, 1.0)
    # approximate screen axes
    return d, (1.0, 1.0, 0.0), (-0.5, 0.5, 1.0)


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def project_points_2d(pts3: Iterable[tuple[float, float, float]],
                      view: str = "front") -> list[tuple[float, float]]:
    vd, sx, sy = _view_basis(view)
    sx = _norm(sx); sy = _norm(sy)
    out = []
    for p in pts3:
        out.append((_dot(p, sx), _dot(p, sy)))
    return out


def silhouette_from_hlr(view_json: dict) -> list[list[tuple[float, float]]]:
    """Extract visible polylines from a project_view / doc_drawing_views entry."""
    polys: list[list[tuple[float, float]]] = []
    for key in ("visible", "outline", "OutLineV", "VCompound"):
        raw = view_json.get(key)
        if not raw:
            continue
        for pl in raw:
            if not isinstance(pl, (list, tuple)) or len(pl) < 2:
                continue
            pts = []
            for p in pl:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((float(p[0]), float(p[1])))
            if len(pts) >= 2:
                polys.append(pts)
    return polys


def silhouette_from_bbox(bbox: dict | list | tuple, view: str = "front"
                         ) -> list[list[tuple[float, float]]]:
    """Wireframe of an axis-aligned bbox projected to *view* (fallback)."""
    if isinstance(bbox, dict):
        mn = bbox.get("min") or bbox.get("mins") or [0, 0, 0]
        mx = bbox.get("max") or bbox.get("maxs") or [1, 1, 1]
        if "xmin" in bbox:
            mn = [bbox["xmin"], bbox["ymin"], bbox["zmin"]]
            mx = [bbox["xmax"], bbox["ymax"], bbox["zmax"]]
    else:
        # [xmin,ymin,zmin,xmax,ymax,zmax] or two corners
        if len(bbox) == 6:
            mn, mx = bbox[:3], bbox[3:]
        else:
            mn, mx = [0, 0, 0], [1, 1, 1]
    x0, y0, z0 = map(float, mn)
    x1, y1, z1 = map(float, mx)
    corners = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    proj = project_points_2d(corners, view)
    return [[proj[i], proj[j]] for i, j in edges]


def render_ascii_3d(polylines: Optional[list] = None,
                    bbox: Any = None,
                    view: str = "front",
                    pitch: float = 1.0,
                    source: str = "experimental") -> dict:
    """Experimental single-view silhouette ASCII for non-vision models."""
    if polylines:
        pls = []
        for pl in polylines:
            if not pl:
                continue
            if len(pl[0]) >= 3:
                pls.append(project_points_2d([tuple(p[:3]) for p in pl], view))  # type: ignore
            else:
                pls.append([(float(p[0]), float(p[1])) for p in pl])
    elif bbox is not None:
        pls = silhouette_from_bbox(bbox, view)
        source = source or "bbox_wireframe"
    else:
        return {
            "ok": False,
            "error": "render_ascii requires polylines or bbox",
            "experimental": True,
        }

    rendered = _project_polylines_ascii(pls, pitch=pitch)
    summary = (
        f"[EXPERIMENTAL] 3D silhouette ASCII (view={view}, source={source}). "
        f"Grid {rendered['width']}x{rendered['height']} at pitch={rendered['pitch']:.3g}. "
        f"{len(pls)} polyline(s). Lossy — for 'is there a hole?' class questions, "
        f"not dimensions. Prefer describe_part / multi-view render when available."
    )
    return {
        "ok": True,
        "kind": "silhouette3d",
        "experimental": True,
        "view": view,
        "source": source,
        "ascii": rendered["ascii"],
        "summary": summary,
        "findings": [],
        "pitch": rendered["pitch"],
        "width": rendered["width"],
        "height": rendered["height"],
        "bbox": rendered["bbox"],
    }


# ================================================================= MCP register


def _load_board(doc_id: str = "") -> dict:
    from engine.ecad import session as ecad_session  # noqa: PLC0415
    if doc_id:
        session = ecad_session.get_session(doc_id)
    else:
        # current pcb if any
        try:
            from mcp_server.ecad_tools import _ECAD  # noqa: PLC0415
            did = _ECAD.get("current_pcb") or _ECAD.get("current")
            if not did:
                raise ValueError("no current PCB — pass doc_id or call ecad_new_pcb first")
            session = ecad_session.get_session(did)
        except Exception as e:
            raise ValueError(f"no PCB session: {e}") from e
    if session.kind != "pcb":
        raise ValueError(f"document {session.doc_id!r} is {session.kind}, not pcb")
    return session.doc


def _load_sketch(sketch: str, doc_id: Optional[str]) -> tuple[dict, Optional[dict]]:
    """Return (sketch_dict with entities/constraints, optional health)."""
    from mcp_server.cad_tools import _doc  # noqa: PLC0415
    import services.mcp.core_bridge as cb  # noqa: PLC0415
    d = _doc(doc_id)
    overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
    # merge authored params if present
    entities = list(overview.get("entities") or [])
    constraints = list(overview.get("constraints") or [])
    # prefer solved entity coords already on overview entities
    health = None
    try:
        health = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
    except Exception:
        health = None
    return {"id": sketch, "entities": entities, "constraints": constraints}, health


def register_ascii2d_tools(mcp: FastMCP) -> None:
    """Register render_board_ascii / render_sketch_ascii / render_ascii."""

    @mcp.tool()
    def render_board_ascii(doc_id: str = "", layer: str = "",
                           pitch_mm: float = 1.0,
                           board_json: str = "") -> str:
        """Per-layer ASCII grid of a PCB + net connectivity summary + checks.

        Renders components as refdes boxes, traces as path characters (- | / \\),
        vias as 'o', pads as '#', and marks same-cell different-net collisions as 'X'.

        Args:
          doc_id: open PCB session id (default: current PCB).
          layer: copper layer id (e.g. 'F.Cu'); empty = all copper layers.
          pitch_mm: world-mm per ASCII cell (auto-coarsened to keep width ≤ 80).
          board_json: optional full board document JSON string — when set, skips
            the live session (useful for offline / unit tests).

        Returns JSON: {ok, ascii, summary, findings, layers}.
        Findings codes: unconnected_net, trace_crossing, courtyard_overlap.
        """
        try:
            if board_json:
                board = json.loads(board_json) if isinstance(board_json, str) else board_json
            else:
                board = _load_board(doc_id)
            report = board_ascii_report(
                board, layer=(layer or None), pitch_mm=float(pitch_mm) or 1.0)
            return _dumps(report)
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass board_json=... or open a PCB with ecad_new_pcb / "
                             "board_from_schematic first.")

    @mcp.tool()
    def render_sketch_ascii(sketch: str = "", doc_id: Optional[str] = None,
                            pitch: float = 0.5,
                            sketch_json: str = "",
                            near_tol: float = 0.01) -> str:
        """ASCII/plaintext rendering of sketch entities + dimensions/constraints + checks.

        Lines/arcs/circles/splines are drawn as control-point polylines. Dimensions and
        constraints are narrated in English. Checks flag open profiles, near-coincident
        endpoints, and under-dimensioned entities (wraps sketch_health when a live
        doc/sketch id is available — does not re-solve).

        Args:
          sketch: sketch feature id (e.g. 's1') for a live part doc.
          doc_id: part document id (default: current part).
          pitch: sketch-units per ASCII cell.
          sketch_json: optional offline payload
            ``{"entities":[...], "constraints":[...], "health"?:{...}}``.
          near_tol: endpoint snap tolerance for open/near-coincident checks (mm).

        Returns JSON: {ok, ascii, summary, findings, ...}.
        """
        try:
            health = None
            if sketch_json:
                data = json.loads(sketch_json) if isinstance(sketch_json, str) else sketch_json
                health = data.get("health")
            elif sketch:
                data, health = _load_sketch(sketch, doc_id)
            else:
                return _err("sketch id or sketch_json required",
                            hint="render_sketch_ascii(sketch='s1') or pass sketch_json")
            report = sketch_ascii_report(data, pitch=float(pitch) or 0.5,
                                         health=health, near_tol=float(near_tol))
            return _dumps(report)
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass sketch_json for offline use, or a live sketch id.")

    @mcp.tool()
    def render_ascii(doc_id: Optional[str] = None, view: str = "front",
                     pitch: float = 1.0,
                     bbox_json: str = "",
                     polylines_json: str = "") -> str:
        """[EXPERIMENTAL] Single-view silhouette ASCII of a 3D part for non-vision models.

        Prefers HLR visible edges from the drawing projector when the core is available;
        falls back to an axis-aligned bbox wireframe. Lossy — good for coarse shape
        questions, not dimensions. Prefer describe_part / render_part when vision or
        GSL text is available.

        Args:
          doc_id: part document id (default: current part).
          view: front|back|left|right|top|bottom|iso.
          pitch: model-units per ASCII cell.
          bbox_json: optional offline bbox ``{"min":[x,y,z],"max":[x,y,z]}``.
          polylines_json: optional offline 2D/3D polylines JSON list.

        Returns JSON: {ok, experimental:true, ascii, summary, view, source}.
        """
        try:
            if polylines_json:
                pls = json.loads(polylines_json) if isinstance(polylines_json, str) \
                    else polylines_json
                return _dumps(render_ascii_3d(polylines=pls, view=view,
                                              pitch=float(pitch) or 1.0,
                                              source="polylines"))
            if bbox_json:
                bb = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
                return _dumps(render_ascii_3d(bbox=bb, view=view,
                                              pitch=float(pitch) or 1.0,
                                              source="bbox_wireframe"))

            # live path: try HLR drawing views, then measure bbox
            from mcp_server.cad_tools import _doc  # noqa: PLC0415
            import services.mcp.core_bridge as cb  # noqa: PLC0415
            d = _doc(doc_id)
            source = "experimental"
            pls2: list = []
            try:
                # Prefer associative drawing projection if available
                tree = cb.call("doc_tree", {"doc_id": d}) or {}
                doc_body = tree.get("tree") or tree.get("doc") or tree
                dv = cb.call("doc_drawing_views", {
                    "doc": doc_body,
                    "views": [{"orientation": view, "view_id": "v0",
                               "scale": 1.0, "position": [0, 0],
                               "show_hidden": False}],
                })
                views = dv.get("views") or []
                if views:
                    pls2 = silhouette_from_hlr(views[0])
                    source = "hlr"
            except Exception:
                pls2 = []

            if not pls2:
                # bbox fallback via measure / tree
                bb = None
                try:
                    m = cb.call("doc_measure", {"doc_id": d, "of": "bbox"})
                    bb = m.get("bbox") or m.get("result") or m
                except Exception:
                    try:
                        # describe-style digest sometimes carries bbox
                        from mcp_server.gsl_tools import _digest  # noqa: PLC0415
                        dig = _digest(d)
                        bb = dig.get("bbox")
                    except Exception:
                        bb = None
                if bb is None:
                    return _err(
                        "could not obtain HLR edges or bbox for render_ascii",
                        hint="Pass bbox_json= or polylines_json= for offline use.",
                        experimental=True)
                return _dumps(render_ascii_3d(bbox=bb, view=view,
                                              pitch=float(pitch) or 1.0,
                                              source="bbox_wireframe"))
            return _dumps(render_ascii_3d(polylines=pls2, view=view,
                                          pitch=float(pitch) or 1.0,
                                          source=source))
        except Exception as e:  # noqa: BLE001
            return _err(str(e), experimental=True,
                        hint="[EXPERIMENTAL] Pass bbox_json / polylines_json offline.")
