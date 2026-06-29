"""Copper zone fill (copper pours) for aicad.pcb/1 boards.

Given a board with zones (ground/power pours), :func:`fill_zones` computes each zone's filled
copper polygon set and writes it back to ``zone["filled_polys"]`` on a *copy* of the board. The
fill is the zone outline, clipped to the board edge, minus a clearance region around every
other-net copper item, with thermal-relief gaps (4 spokes) around same-net through-hole/connector
pads, and small islands dropped.

This is the executable counterpart of the copper-pour step the board compiler runs before Gerber
export. It never mutates its input and is deterministic.

Fill approach — GRID RASTER (numpy) + boundary tracing
======================================================
Polygon booleans without shapely are heavy and error-prone, so we use a robust raster:

  1. Rasterize the zone's bounding box (clipped to the board outline bbox) on a regular grid at
     ``resolution`` mm (default 0.1mm). A cell is *copper* when its centre is

       * inside the zone ``polygon`` (even-odd point-in-polygon), AND
       * inside the board outline (if an outline exists), AND
       * at least ``clearance`` away from every DIFFERENT-net copper item on the zone's layer
         (pads, tracks, vias) — measured edge-to-edge with the same shape abstraction DRC uses
         (:mod:`engine.ecad.drc`), so the pour respects exactly the spacing DRC enforces, AND
       * not inside the *thermal gap ring* of a same-net through-hole / connector pad, UNLESS it
         lies in one of the 4 axis-aligned spoke corridors (so the pad keeps a thin tether to the
         pour but is not a solid heatsink). Same-net SMD pads and same-net tracks/vias are flooded
         solid (a planar connection — the documented choice; SMD pads have little thermal mass).

  2. Drop copper components whose area is below ``min_island_area`` (derived from ``min_width``:
     a sliver thinner than ``min_width`` cannot host a square of side ``min_width``, so the
     threshold is ``min_width**2``; tiny disconnected specks are removed).

  3. Trace each surviving copper component to a polygon by walking the boundary between copper and
     non-copper cells (a Moore-neighbour contour trace on the cell grid). Interior holes (e.g. the
     clearance void fully enclosed by a different-net pad sitting inside the pour) are stitched into
     the component's outer ring with a thin "keyhole" bridge — see the convention note.

Resolution / accuracy
---------------------
Default ``resolution = 0.1mm``. Edges land on the grid, so a feature position is accurate to ±half
a cell (±0.05mm by default) and areas to roughly ±(perimeter · res/2). Clearance is enforced
conservatively: a cell counts as copper only if its centre clears the obstacle, so the *actual*
copper-to-obstacle gap is at least ``clearance - res/2`` and at most ``clearance + res/2``. Smaller
``resolution`` → finer fidelity at quadratic cost. This is intentionally a fab-preview-grade fill,
not an analytic polygon offset.

filled_polys convention (matches fab/gerber.py G36 emission)
============================================================
``zone["filled_polys"]`` is a flat ``list`` of *simple* polygons, each ``[[x, y], ...]`` (no
explicit hole arrays, no nesting). ``fab/gerber.py`` emits **one G36/G37 solid region per polygon**
with no clear-polarity, so every polygon we emit is filled solid. Therefore:

  * Disjoint copper islands are separate polygons.
  * A hole *inside* a copper island (a fully-enclosed clearance void) is folded into that island's
    single polygon via a zero-width keyhole bridge: the ring runs out to the hole, around it in the
    opposite sense, and back along the same seam. Filled solid, the seam closes on itself and the
    hole reads as bare laminate — exactly how KiCad serialises pours-with-holes for solid-fill
    Gerber. We therefore never emit a separate "hole" polygon that the gerber writer would wrongly
    flood with copper.

Public API
----------
  * :func:`fill_zones(board) -> board_copy` — deep-copy, fill every copper zone, return the copy.
  * :func:`fill_zone(board, zone) -> [polygon, ...]` — the filled polys for one zone.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np

from engine.ecad import drc, schema

Point = tuple[float, float]

# Defaults / tunables (mm). resolution is the grid cell size.
DEFAULT_RESOLUTION = 0.1


# ── point-in-polygon (even-odd) ─────────────────────────────────────────────────

def _poly_bbox(poly: list) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _points_in_polygon(xs: np.ndarray, ys: np.ndarray, poly: list) -> np.ndarray:
    """Vectorised even-odd point-in-polygon for grids of points (xs, ys same shape).

    Boundary points are treated inclusively-ish (ray-cast); sub-cell error is dominated by the grid
    resolution anyway. Returns a boolean array shaped like ``xs``.
    """
    pts = np.asarray(poly, dtype=float)
    n = len(pts)
    inside = np.zeros(xs.shape, dtype=bool)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i, 0], pts[i, 1]
        xj, yj = pts[j, 0], pts[j, 1]
        # edges straddling the horizontal ray at y
        cond = ((yi > ys) != (yj > ys))
        # x of the edge at height ys (avoid div-by-zero for horizontal edges via the cond mask)
        denom = (yj - yi)
        denom = np.where(denom == 0.0, 1e-30, denom)
        xint = (xj - xi) * (ys - yi) / denom + xi
        inside ^= cond & (xs < xint)
        j = i
    return inside


# ── copper obstacle model (reuse DRC's shape abstraction) ───────────────────────

def _layer_shapes(board: dict, layer: str) -> list:
    """All DRC copper :class:`drc._Shape` items present on ``layer`` (deterministic order)."""
    return [s for s in drc._copper_shapes(board) if layer in s.layers]


def _point_to_shape_dist(px: float, py: float, s) -> float:
    """Edge-to-edge distance from point (px,py) to a DRC shape (0 if inside the copper)."""
    if s.kind == "circle":
        return max(math.hypot(px - s.center[0], py - s.center[1]) - s.margin, 0.0)
    if s.kind == "box":
        return drc._point_box_dist((px, py), s.center[0], s.center[1], s.half[0], s.half[1])
    # seg (track): point to centreline minus half-width
    return max(drc._point_seg_dist((px, py), s.pts[0], s.pts[1]) - s.margin, 0.0)


def _net_class_clearance(board: dict, net: Any) -> float:
    """The net-class clearance for ``net`` if it has one, else 0 (so max() ignores it)."""
    rules = board.get("rules", {})
    classes = board.get("net_classes", {})
    cls = drc._net_class_of(board, net)
    if cls is not None and cls in classes and "clearance" in classes[cls]:
        return max(float(classes[cls]["clearance"]), 0.0)
    return 0.0


# ── thermal relief geometry ──────────────────────────────────────────────────────

class _ThermalPad:
    """A same-net THT/connector pad that gets a thermal-relief ring + 4 spokes.

    ``r_copper`` is the pad's copper radius (bounding circle), ``r_gap`` the outer radius of the
    no-copper relief ring (``r_copper + thermal.gap``). Cells within ``[r_copper, r_gap]`` of the
    centre are voided UNLESS they fall inside one of the 4 spoke corridors (half-width
    ``spoke_width/2``) along ±x / ±y. Cells inside ``r_copper`` are the pad itself (left bare by the
    pour; the pad's own copper provides the connection).
    """

    __slots__ = ("cx", "cy", "r_copper", "r_gap", "spoke_half")

    def __init__(self, cx, cy, r_copper, gap, spoke_width):
        self.cx = cx
        self.cy = cy
        self.r_copper = r_copper
        self.r_gap = r_copper + max(gap, 0.0)
        self.spoke_half = max(spoke_width, 0.0) / 2.0

    def voids(self, px: float, py: float) -> bool:
        """True if (px,py) is in the relief gap ring and NOT in a spoke corridor → no copper."""
        dx = px - self.cx
        dy = py - self.cy
        d = math.hypot(dx, dy)
        if d >= self.r_gap or d <= self.r_copper:
            return False  # outside the ring, or inside the pad copper itself
        # inside the ring: kept only if within a ±x or ±y spoke corridor
        in_spoke = (abs(dy) <= self.spoke_half) or (abs(dx) <= self.spoke_half)
        return not in_spoke


def _pad_copper_radius(fp: dict, pad: dict) -> float:
    size = pad.get("size", [0.0, 0.0]) or [0.0, 0.0]
    w = float(size[0]) if len(size) > 0 else 0.0
    h = float(size[1]) if len(size) > 1 else w
    return max(w, h) / 2.0


def _thermal_pads(board: dict, zone: dict, layer: str, net: Any) -> list:
    """Same-net THT/connector pads on ``layer`` that should get thermal relief."""
    thermal = zone.get("thermal", {}) or {}
    gap = float(thermal.get("gap", 0.0))
    spoke_w = float(thermal.get("spoke_width", 0.0))
    n_cu = int(board.get("stackup", {}).get("copper_layers", 2))
    out: list[_ThermalPad] = []
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            if not drc.same_net(pad.get("net"), net):
                continue
            if pad.get("type", "smd") not in ("tht", "np_tht", "connector"):
                continue  # SMD same-net pads flood solid (documented)
            if layer not in drc._pad_layers(pad, n_cu):
                continue
            wx, wy = schema.pad_world(fp, pad)
            out.append(_ThermalPad(wx, wy, _pad_copper_radius(fp, pad), gap, spoke_w))
    out.sort(key=lambda t: (t.cx, t.cy))
    return out


# ── grid contour tracing (cell mask -> polygons, holes keyholed in) ─────────────

def _mask_to_polygons(mask: np.ndarray, x0: float, y0: float, res: float,
                      min_cells: int) -> list[list[list[float]]]:
    """Trace a boolean copper-cell ``mask`` (shape [ny, nx]) into world-coordinate polygons.

    Cell (r, c) covers the square whose lower-left corner is at world
    ``(x0 + c*res, y0 + r*res)`` and spans ``res``. We label 4-connected copper components, drop
    those below ``min_cells``, and for each surviving component emit ONE simple polygon: its outer
    boundary with every interior hole bridged in by a keyhole seam (see module docstring).
    """
    ny, nx = mask.shape
    polys: list[list[list[float]]] = []

    # 4-connected component labelling (iterative flood fill; deterministic scan order).
    labels = np.zeros((ny, nx), dtype=np.int32)
    cur = 0
    for sr in range(ny):
        for sc in range(nx):
            if not mask[sr, sc] or labels[sr, sc]:
                continue
            cur += 1
            stack = [(sr, sc)]
            labels[sr, sc] = cur
            count = 0
            while stack:
                r, c = stack.pop()
                count += 1
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < ny and 0 <= nc < nx and mask[nr, nc] and not labels[nr, nc]:
                        labels[nr, nc] = cur
                        stack.append((nr, nc))
            if count < min_cells:
                # erase this too-small island so it is not traced
                labels[labels == cur] = 0

    for lab in range(1, cur + 1):
        comp = labels == lab
        if not comp.any():
            continue
        ring = _trace_component(comp, x0, y0, res)
        if ring and len(ring) >= 3:
            polys.append(ring)
    return polys


def _trace_component(comp: np.ndarray, x0: float, y0: float, res: float) -> list[list[float]]:
    """Trace one 4-connected component into a single simple polygon (holes keyholed in).

    Strategy that is robust without a full boolean library: build the component's outer ring and
    each hole ring by following the boundary between filled and empty cells on the dual grid of
    cell corners, then merge holes into the outer ring with zero-width bridges.
    """
    outer, holes = _boundary_loops(comp)
    if outer is None:
        return []
    ring = [list(p) for p in outer]
    for hole in holes:
        ring = _bridge_hole(ring, [list(p) for p in hole])
    # to world coords
    return [[x0 + px * res, y0 + py * res] for (px, py) in ring]


def _boundary_loops(comp: np.ndarray):
    """Return (outer_loop, [hole_loops]) in GRID-CORNER coordinates for a 4-connected component.

    We trace edges of the cell region. An edge exists between a filled cell and an empty cell (or
    the grid border). Loops are assembled by chaining these unit edges. The loop with the largest
    absolute signed area is the outer boundary; the rest are holes. Coordinates are in cell units
    (corner (c, r) of cell grid → world via x0 + c*res).
    """
    ny, nx = comp.shape
    # collect directed boundary edges so the filled region is on the left (CCW outer).
    # For a filled cell (r,c), its 4 corners are (c,r),(c+1,r),(c+1,r+1),(c,r+1).
    # Edge present on a side where the neighbour is empty/out-of-bounds.
    edges: dict[tuple[float, float], list[tuple[float, float]]] = {}

    def add_edge(p: tuple[float, float], q: tuple[float, float]) -> None:
        edges.setdefault(p, []).append(q)

    for r in range(ny):
        for c in range(nx):
            if not comp[r, c]:
                continue
            # corners
            bl = (c, r)
            br = (c + 1, r)
            tr = (c + 1, r + 1)
            tl = (c, r + 1)
            # left neighbour empty -> left edge goes bottom->top? keep region on left: walk CCW.
            # We want directed edges with filled cell interior on the LEFT of travel.
            # bottom side (neighbour r-1): travel +x (bl->br) keeps interior above-left.
            if r == 0 or not comp[r - 1, c]:
                add_edge(bl, br)
            # right side (neighbour c+1): travel +y (br->tr)
            if c == nx - 1 or not comp[r, c + 1]:
                add_edge(br, tr)
            # top side (neighbour r+1): travel -x (tr->tl)
            if r == ny - 1 or not comp[r + 1, c]:
                add_edge(tr, tl)
            # left side (neighbour c-1): travel -y (tl->bl)
            if c == 0 or not comp[r, c - 1]:
                add_edge(tl, bl)

    # chain edges into closed loops
    loops: list[list[tuple[float, float]]] = []
    # work on a mutable copy of adjacency
    adj = {k: list(v) for k, v in edges.items()}
    total = sum(len(v) for v in adj.values())
    used = 0
    while used < total:
        # find a start vertex with an outgoing edge
        start = None
        for k, v in adj.items():
            if v:
                start = k
                break
        if start is None:
            break
        loop = [start]
        cur = start
        while True:
            outs = adj.get(cur)
            if not outs:
                break
            nxt = outs.pop()
            used += 1
            loop.append(nxt)
            cur = nxt
            if cur == start:
                break
        # collapse collinear runs to keep polygons compact
        loops.append(_simplify_collinear(loop[:-1] if loop[-1] == loop[0] else loop))

    if not loops:
        return None, []
    # outer = max abs signed area; sign convention: positive area == CCW.
    areas = [(_signed_area(lp), lp) for lp in loops]
    areas.sort(key=lambda a: abs(a[0]), reverse=True)
    outer = areas[0][1]
    if _signed_area(outer) < 0:
        outer = outer[::-1]
    holes = []
    for a, lp in areas[1:]:
        h = lp if a < 0 else lp[::-1]  # holes clockwise
        holes.append(h)
    return outer, holes


def _simplify_collinear(loop: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop interior vertices that are collinear with their neighbours (axis-aligned grid)."""
    if len(loop) < 3:
        return loop
    out: list[tuple[float, float]] = []
    n = len(loop)
    for i in range(n):
        a = loop[(i - 1) % n]
        b = loop[i]
        c = loop[(i + 1) % n]
        # cross product of (b-a)x(c-b); 0 => collinear => skip b
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-12:
            out.append(b)
    return out or loop


def _signed_area(loop: list[tuple[float, float]]) -> float:
    s = 0.0
    n = len(loop)
    for i in range(n):
        x1, y1 = loop[i]
        x2, y2 = loop[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _bridge_hole(outer: list[list[float]], hole: list[list[float]]) -> list[list[float]]:
    """Stitch a hole loop into the outer ring with a zero-width keyhole bridge.

    Find the closest pair (outer vertex i, hole vertex j), then splice the hole ring (starting at j,
    traversed in its own order) into the outer ring after i, returning to i. The doubled seam has
    zero width, so a solid fill of the resulting simple polygon leaves the hole bare.
    """
    best = (math.inf, 0, 0)
    for i, op in enumerate(outer):
        for j, hp in enumerate(hole):
            d = (op[0] - hp[0]) ** 2 + (op[1] - hp[1]) ** 2
            if d < best[0]:
                best = (d, i, j)
    _, i, j = best
    hole_seq = hole[j:] + hole[:j] + [list(hole[j])]  # close the hole back to its start
    # splice: outer[..i] + bridge to hole + around hole + back + outer[i..]
    return (outer[: i + 1]
            + hole_seq
            + [list(outer[i])]
            + outer[i + 1:])


# ── one-zone fill ────────────────────────────────────────────────────────────────

def fill_zone(board: dict, zone: dict, resolution: float = DEFAULT_RESOLUTION) -> list:
    """Compute the filled copper polygons for a single ``zone`` of ``board``.

    Returns a list of simple polygons (the value to store in ``zone["filled_polys"]``). Does not
    mutate ``board`` or ``zone``. Returns ``[]`` for a degenerate/empty zone.
    """
    poly = zone.get("polygon")
    if not poly or len(poly) < 3:
        return []
    layer = zone.get("layer")
    net = zone.get("net")
    rules = board.get("rules", {})

    # required clearance: max(zone, global rules, net-class of the zone's net)
    base_clear = max(
        float(zone.get("clearance", 0.0)),
        float(rules.get("clearance", 0.0)),
        _net_class_clearance(board, net),
    )

    # grid bbox: zone bbox, clipped to outline bbox if an outline exists.
    minx, miny, maxx, maxy = _poly_bbox(poly)
    outline = board.get("outline", {})
    outline_polys = _outline_polygons(board)
    if outline_polys:
        oxs = [p[0] for op in outline_polys for p in op]
        oys = [p[1] for op in outline_polys for p in op]
        minx = max(minx, min(oxs))
        miny = max(miny, min(oys))
        maxx = min(maxx, max(oxs))
        maxy = min(maxy, max(oys))
    if maxx <= minx or maxy <= miny:
        return []

    res = float(resolution)
    nx = max(1, int(math.ceil((maxx - minx) / res)))
    ny = max(1, int(math.ceil((maxy - miny) / res)))
    # cap grid size so a giant board can't blow up memory; coarsen if needed.
    MAX_CELLS = 4_000_000
    if nx * ny > MAX_CELLS:
        scale = math.sqrt(nx * ny / MAX_CELLS)
        res *= scale
        nx = max(1, int(math.ceil((maxx - minx) / res)))
        ny = max(1, int(math.ceil((maxy - miny) / res)))

    # cell-centre coordinates
    cx = minx + (np.arange(nx) + 0.5) * res
    cy = miny + (np.arange(ny) + 0.5) * res
    gx, gy = np.meshgrid(cx, cy)  # shape [ny, nx]

    # 1) inside the zone polygon
    mask = _points_in_polygon(gx, gy, poly)

    # 2) inside the board outline (every outline sub-polygon; even-odd union of fills)
    if outline_polys:
        inside_outline = np.zeros_like(mask)
        for op in outline_polys:
            inside_outline ^= _points_in_polygon(gx, gy, op)
        mask &= inside_outline

    # 3) subtract clearance around DIFFERENT-net copper on this layer
    shapes = _layer_shapes(board, layer)
    flat_x = gx.ravel()
    flat_y = gy.ravel()
    flat_mask = mask.ravel()
    cand = np.flatnonzero(flat_mask)
    if cand.size:
        for s in shapes:
            if drc.same_net(s.net, net):
                continue  # same-net copper does NOT carve the pour
            # required clearance: the larger of the zone-level base and the DRC pair clearance
            # (so a shared net-class between pour-net and obstacle-net is honoured consistently).
            req = max(base_clear, _pair_clearance(board, net, s.net))
            still = cand[flat_mask[cand]]
            if still.size == 0:
                break
            kill = np.array(
                [_point_to_shape_dist(flat_x[i], flat_y[i], s) < req for i in still],
                dtype=bool,
            )
            flat_mask[still[kill]] = False
    mask = flat_mask.reshape(mask.shape)

    # 4) thermal relief: void the gap ring of same-net THT/connector pads (keep spoke corridors)
    tpads = _thermal_pads(board, zone, layer, net)
    if tpads:
        flat_mask = mask.ravel()
        cand = np.flatnonzero(flat_mask)
        for tp in tpads:
            still = cand[flat_mask[cand]]
            if still.size == 0:
                break
            kill = np.array([tp.voids(flat_x[i], flat_y[i]) for i in still], dtype=bool)
            flat_mask[still[kill]] = False
        mask = flat_mask.reshape(mask.shape)

    # 5) trace to polygons, dropping islands below min_width^2
    min_width = float(zone.get("min_width", 0.0))
    min_island_area = max(min_width * min_width, 0.0)
    min_cells = max(1, int(math.floor(min_island_area / (res * res)))) if min_island_area > 0 else 1

    polys = _mask_to_polygons(mask, minx, miny, res, min_cells)
    return polys


def _pair_clearance(board: dict, net_a: Any, net_b: Any) -> float:
    """Required clearance between the pour net and an obstacle net (mirrors drc._clearance_for)."""
    rules = board.get("rules", {})
    g = float(rules.get("clearance", 0.0))
    classes = board.get("net_classes", {})
    ca = drc._net_class_of(board, net_a)
    cb = drc._net_class_of(board, net_b)
    if ca is not None and ca == cb and ca in classes and "clearance" in classes[ca]:
        return max(float(classes[ca]["clearance"]), g)
    return g


def _outline_polygons(board: dict) -> list[list[list[float]]]:
    """Board outline(s) as closed polygons (polygonized arcs/circles). Empty if no usable outline."""
    segs = drc._outline_segments(board)
    if not segs:
        return []
    # chain segments into closed loops by endpoint matching
    tol = 1e-6
    remaining = list(segs)
    loops: list[list[list[float]]] = []
    while remaining:
        a, b = remaining.pop(0)
        loop = [list(a), list(b)]
        changed = True
        while changed and remaining:
            changed = False
            end = loop[-1]
            for k, (c, d) in enumerate(remaining):
                if abs(c[0] - end[0]) < tol and abs(c[1] - end[1]) < tol:
                    loop.append(list(d)); remaining.pop(k); changed = True; break
                if abs(d[0] - end[0]) < tol and abs(d[1] - end[1]) < tol:
                    loop.append(list(c)); remaining.pop(k); changed = True; break
        if len(loop) >= 3:
            # drop closing duplicate if present
            if abs(loop[0][0] - loop[-1][0]) < tol and abs(loop[0][1] - loop[-1][1]) < tol:
                loop = loop[:-1]
            loops.append(loop)
    return loops


# ── whole-board fill ─────────────────────────────────────────────────────────────

def fill_zones(board: dict, resolution: float = DEFAULT_RESOLUTION) -> dict:
    """Return a deep copy of ``board`` with every copper zone's ``filled_polys`` recomputed.

    Pure: the input ``board`` is never mutated. Non-board inputs are returned as a shallow no-op
    copy. Deterministic for a given board + resolution.
    """
    out = copy.deepcopy(board)
    if not schema.is_board(out):
        return out
    cu = set(schema.copper_layer_ids(int(out.get("stackup", {}).get("copper_layers", 2))))
    for zone in out.get("zones", []):
        if zone.get("layer") not in cu:
            zone["filled_polys"] = []
            continue
        zone["filled_polys"] = fill_zone(out, zone, resolution=resolution)
    return out


if __name__ == "__main__":  # pragma: no cover - smoke
    import json
    import sys
    doc = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else schema.new_board()
    filled = fill_zones(doc)
    print(json.dumps([{"id": z.get("id"), "n_polys": len(z.get("filled_polys", [])),
                       "polys": z.get("filled_polys", [])}
                      for z in filled.get("zones", [])], indent=2))
