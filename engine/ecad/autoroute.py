"""Basic grid (Lee/maze) PCB autorouter for aicad.pcb/1 boards.

A best-effort, *honestly basic* 2-layer grid router — NOT a production push-and-shove engine like
KiCad's interactive router. It connects the ratsnest airwires (see :func:`engine.ecad.drc.ratsnest`)
with orthogonal track segments and layer-change vias, respecting copper clearance against
already-placed copper of other nets.

Design (documented trade-offs):

* **Grid.** A uniform square grid is laid over the board bounding box (the Edge.Cuts outline if
  present, else the copper extent) at a configurable ``pitch`` (default 0.25 mm). Two routing layers
  (``F.Cu``, ``B.Cu``). Finer pitch → routes thread tighter gaps and look cleaner, but cost grows as
  O(cells) = O(area / pitch²) per layer per net, so the grid is *capped*: if width·height in cells
  would exceed :data:`MAX_GRID_CELLS` the pitch is coarsened until it fits (reported in
  ``report["grid"]``). For a big board this means the router silently uses a coarser grid rather than
  blowing up — coarser grid = it may fail to find gaps a finer grid would.

* **Obstacles.** Every cell is tested against the board's copper that belongs to a *different* net
  (pads of other nets, plus tracks/vias from earlier-routed nets). A cell is blocked on a layer if a
  point there would sit closer than ``clearance + half_track_width`` to that copper — reusing the
  exact DRC shape/edge-distance geometry (:mod:`engine.ecad.drc`) so a routed track does not create a
  new ``clearance`` violation. Pads of the *current* net are carved back out as routable
  sources/targets. Cells outside the outline (or within ``edge_clearance`` of it) are blocked.

* **Search.** Per airwire, A* (Lee BFS with a Manhattan heuristic) from the source pad's cell to the
  target pad's cell across the two-layer cell graph. Moving to an orthogonal neighbour on the same
  layer costs 1 (plus a small bend penalty to discourage zig-zag and an HV-bias so F.Cu prefers
  horizontal and B.Cu prefers vertical, reducing crossings). Switching layer at a cell costs
  :data:`VIA_COST` and drops a via. A net is *failed* (reported, never fatal) when no path exists.

* **Apply.** The cell path is compressed into maximal colinear runs → one ``segment`` track per run on
  its layer, with a ``via`` inserted at each layer change. Newly-routed copper is then baked into the
  obstacle map (with its own clearance halo) so later nets avoid it.

Net ordering: airwires are routed shortest-Euclidean-first (a cheap, deterministic heuristic — short
nets are easy and grabbing them first frees the map; it is not an optimal global order).

Purity: :func:`autoroute` never mutates ``board``; it returns *new* track/via dicts (ids allocated
from a counter seeded past the board's existing ids, nets assigned, widths from the net class or the
global rule). :func:`route_board` is a convenience that returns a deep-copied board with them added.
"""

from __future__ import annotations

import copy
import heapq
import math
import re
from typing import Any

from engine.ecad import drc, schema

# ── tunables (documented in the module docstring) ──────────────────────────────────

DEFAULT_PITCH = 0.25          # mm; grid resolution
MAX_GRID_CELLS = 240_000      # per-layer cell cap; pitch coarsens until w*h <= this
VIA_COST = 12.0               # A* cost of a layer change (in grid steps)
BEND_COST = 0.6               # extra cost to turn (discourages staircasing)
HV_BIAS = 0.35                # extra cost for the non-preferred direction on a layer
DEFAULT_VIA_DIAMETER = 0.8    # mm, used when no net-class via_diameter is set
DEFAULT_VIA_DRILL = 0.4       # mm


# ── small helpers ───────────────────────────────────────────────────────────────────

def _copper_layer_ids(board: dict) -> list[str]:
    n_cu = int(board.get("stackup", {}).get("copper_layers", 2))
    return schema.copper_layer_ids(n_cu)


def _net_class(board: dict, net: Any) -> dict:
    """Net-class dict for ``net`` (falls back to the 'default' class, else {})."""
    classes = board.get("net_classes", {}) or {}
    cls_name = None
    for n in board.get("nets", []):
        if isinstance(n, dict) and n.get("name") == net:
            cls_name = n.get("class")
            break
    if cls_name and cls_name in classes:
        return classes[cls_name]
    return classes.get("default", {})


def _track_width(board: dict, net: Any) -> float:
    """Track width for a net: net-class ``track_width`` if present, else rules.track_width_min."""
    nc = _net_class(board, net)
    if "track_width" in nc:
        return float(nc["track_width"])
    return float(board.get("rules", {}).get("track_width_min", 0.15))


def _via_dims(board: dict, net: Any) -> tuple[float, float]:
    nc = _net_class(board, net)
    dia = float(nc.get("via_diameter", DEFAULT_VIA_DIAMETER))
    drill = float(nc.get("via_drill", DEFAULT_VIA_DRILL))
    return dia, drill


def _clearance(board: dict, net: Any) -> float:
    """Clearance to honour for this net: net-class clearance if present, else global."""
    nc = _net_class(board, net)
    if "clearance" in nc:
        return float(nc["clearance"])
    return float(board.get("rules", {}).get("clearance", 0.2))


class _IdCounter:
    """Allocates ``<prefix><n>`` ids past the highest already used on the board (no reuse)."""

    def __init__(self, board: dict, prefix: str, key: str) -> None:
        self._prefix = prefix
        pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        n = 0
        for it in board.get(key, []):
            if isinstance(it, dict):
                m = pat.match(it.get("id", ""))
                if m:
                    n = max(n, int(m.group(1)))
        self._n = n

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}{self._n}"


# ── board bounding box ───────────────────────────────────────────────────────────────

def _board_bbox(board: dict) -> tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) of the routing area: outline if present, else copper extent."""
    xs: list[float] = []
    ys: list[float] = []
    edges = drc._outline_segments(board)
    for (a, b) in edges:
        xs += [a[0], b[0]]
        ys += [a[1], b[1]]
    if not xs:
        # no outline: use the copper extent with a margin
        for sh in drc._copper_shapes(board):
            if sh.kind == "circle":
                xs += [sh.center[0] - sh.margin, sh.center[0] + sh.margin]
                ys += [sh.center[1] - sh.margin, sh.center[1] + sh.margin]
            elif sh.kind == "box":
                xs += [sh.center[0] - sh.half[0], sh.center[0] + sh.half[0]]
                ys += [sh.center[1] - sh.half[1], sh.center[1] + sh.half[1]]
            else:  # seg
                xs += [sh.pts[0][0], sh.pts[1][0]]
                ys += [sh.pts[0][1], sh.pts[1][1]]
        if not xs:
            return (0.0, 0.0, 1.0, 1.0)
        pad = 2.0
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    return (min(xs), min(ys), max(xs), max(ys))


def _point_in_outline(board: dict, p: tuple[float, float], edges) -> bool:
    """Even-odd ray cast: is point ``p`` inside the (closed) Edge.Cuts polygon approximated by edges."""
    x, y = p
    inside = False
    for (a, b) in edges:
        (x1, y1), (x2, y2) = a, b
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


# ── obstacle grid ──────────────────────────────────────────────────────────────────

class _Grid:
    """Two-layer occupancy grid over the board bbox.

    ``blocked[layer_idx]`` is an ``nx*ny`` bool array: True = a track centre there would violate
    clearance against other-net copper (or fall outside the routable area). The current net's own
    pads are carved out per-route as routable cells.
    """

    def __init__(self, board: dict, pitch: float) -> None:
        self.board = board
        self.layers = _copper_layer_ids(board)
        self.minx, self.miny, self.maxx, self.maxy = _board_bbox(board)
        self.pitch = pitch
        self.nx = max(1, int(round((self.maxx - self.minx) / pitch)) + 1)
        self.ny = max(1, int(round((self.maxy - self.miny) / pitch)) + 1)
        # base[layer] excludes nothing-net-specific clearance; per net we add a halo. We bake the
        # static other-net copper into a list of (shape) and rebuild blocked per net below.
        self._edges = drc._outline_segments(board)
        self._edge_clear = float(board.get("rules", {}).get("edge_clearance", 0.0))
        self._shapes_by_layer = self._index_shapes()
        # outline mask: cells that are inside the board and clear of the edge (layer-independent)
        self._outline_mask = self._build_outline_mask()

    # cell <-> world ------------------------------------------------------------------

    def cell_xy(self, ix: int, iy: int) -> tuple[float, float]:
        return (self.minx + ix * self.pitch, self.miny + iy * self.pitch)

    def nearest_cell(self, p: tuple[float, float]) -> tuple[int, int]:
        ix = int(round((p[0] - self.minx) / self.pitch))
        iy = int(round((p[1] - self.miny) / self.pitch))
        ix = min(max(ix, 0), self.nx - 1)
        iy = min(max(iy, 0), self.ny - 1)
        return ix, iy

    def idx(self, ix: int, iy: int) -> int:
        return iy * self.nx + ix

    # shape indexing ------------------------------------------------------------------

    def _index_shapes(self) -> dict[str, list[drc._Shape]]:
        """Copper shapes grouped by the copper layer they sit on (for fast per-layer queries)."""
        out: dict[str, list[drc._Shape]] = {ly: [] for ly in self.layers}
        for sh in drc._copper_shapes(self.board):
            for ly in sh.layers:
                if ly in out:
                    out[ly].append(sh)
        return out

    def _build_outline_mask(self) -> list[bool]:
        """Per-cell: True if the cell is a legal place for copper (inside outline, edge-clear)."""
        n = self.nx * self.ny
        if not self._edges:
            return [True] * n  # no outline -> whole bbox routable
        mask = [False] * n
        for iy in range(self.ny):
            for ix in range(self.nx):
                p = self.cell_xy(ix, iy)
                if not _point_in_outline(self.board, p, self._edges):
                    continue
                if self._edge_clear > 0:
                    d = min(drc._point_seg_dist(p, a, b) for (a, b) in self._edges)
                    if d < self._edge_clear:
                        continue
                mask[self.idx(ix, iy)] = True
        return mask

    def build_blocked(self, net: Any) -> list[list[bool]]:
        """Per-layer blocked map for routing ``net``: other-net copper inflated by clearance +
        half track width blocks cells; this net's own copper does NOT block (it is source/target);
        outside-outline / edge cells block."""
        clr = _clearance(self.board, net)
        halo = clr + _track_width(self.board, net) / 2.0
        blocked: list[list[bool]] = []
        for ly in self.layers:
            row = [not m for m in self._outline_mask]  # start: outside-outline cells blocked
            shapes = [s for s in self._shapes_by_layer[ly]
                      if not drc.same_net(s.net, net)]
            if shapes:
                self._mark_shapes(row, shapes, halo)
            blocked.append(row)
        return blocked

    def _mark_shapes(self, row: list[bool], shapes: list[drc._Shape], halo: float) -> None:
        """Mark every cell within ``halo`` of any shape as blocked, scanning only each shape's
        local bbox (the halo'd extent) rather than the whole grid."""
        for sh in shapes:
            # shape bbox + halo, clamped to grid index range
            if sh.kind == "circle":
                bx0, by0 = sh.center[0] - sh.margin, sh.center[1] - sh.margin
                bx1, by1 = sh.center[0] + sh.margin, sh.center[1] + sh.margin
            elif sh.kind == "box":
                bx0, by0 = sh.center[0] - sh.half[0], sh.center[1] - sh.half[1]
                bx1, by1 = sh.center[0] + sh.half[0], sh.center[1] + sh.half[1]
            else:  # seg
                (p0, p1) = sh.pts
                bx0, bx1 = min(p0[0], p1[0]) - sh.margin, max(p0[0], p1[0]) + sh.margin
                by0, by1 = min(p0[1], p1[1]) - sh.margin, max(p0[1], p1[1]) + sh.margin
            ix0 = max(0, int(math.floor((bx0 - halo - self.minx) / self.pitch)))
            ix1 = min(self.nx - 1, int(math.ceil((bx1 + halo - self.minx) / self.pitch)))
            iy0 = max(0, int(math.floor((by0 - halo - self.miny) / self.pitch)))
            iy1 = min(self.ny - 1, int(math.ceil((by1 + halo - self.miny) / self.pitch)))
            for iy in range(iy0, iy1 + 1):
                base = iy * self.nx
                for ix in range(ix0, ix1 + 1):
                    if row[base + ix]:
                        continue
                    if self._cell_clear_dist(sh, self.cell_xy(ix, iy)) < halo:
                        row[base + ix] = True

    @staticmethod
    def _cell_clear_dist(sh: drc._Shape, p: tuple[float, float]) -> float:
        """Edge distance from a copper shape to a point (a routed-track centre candidate)."""
        if sh.kind == "circle":
            return max(drc._dist(sh.center, p) - sh.margin, 0.0)
        if sh.kind == "box":
            return drc._point_box_dist(p, sh.center[0], sh.center[1], sh.half[0], sh.half[1])
        # seg
        return max(drc._point_seg_dist(p, sh.pts[0], sh.pts[1]) - sh.margin, 0.0)


# ── A* over the two-layer cell graph ─────────────────────────────────────────────────

def _astar(grid: _Grid, blocked: list[list[bool]],
           starts: set[tuple[int, int, int]], goals: set[tuple[int, int, int]],
           goal_xy: tuple[float, float],
           via_cost: float = VIA_COST, bend_cost: float = BEND_COST,
           ) -> list[tuple[int, int, int]] | None:
    """A* from any start node to any goal node. Nodes are (layer_idx, ix, iy).

    Cost: 1 per orthogonal same-layer step + ``bend_cost`` on a turn + HV bias; ``via_cost`` per
    layer change. Heuristic: Manhattan distance (in cells) to the goal centroid — admissible since
    each move advances at most one cell. Deterministic: ties broken by node tuple in the heap.
    """
    gi = grid.nearest_cell(goal_xy)

    def h(layer: int, ix: int, iy: int) -> float:
        return abs(ix - gi[0]) + abs(iy - gi[1])

    # came_from[node] = (prev_node, move_dir) ; dir is (dx,dy) or None for a via
    came: dict[tuple[int, int, int], tuple] = {}
    gscore: dict[tuple[int, int, int], float] = {}
    # heap entries: (f, g, node, dir_into_node)
    heap: list[tuple] = []
    for s in sorted(starts):
        if blocked[s[0]][grid.idx(s[1], s[2])]:
            continue
        gscore[s] = 0.0
        heapq.heappush(heap, (h(*s), 0.0, s, None))
        came[s] = (None, None)

    moves = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    while heap:
        f, g, node, _ = heapq.heappop(heap)
        if g > gscore.get(node, math.inf):
            continue
        if node in goals:
            return _reconstruct(came, node)
        layer, ix, iy = node
        prev_dir = came[node][1]
        # same-layer orthogonal moves
        for (dx, dy) in moves:
            nx_, ny_ = ix + dx, iy + dy
            if nx_ < 0 or ny_ < 0 or nx_ >= grid.nx or ny_ >= grid.ny:
                continue
            if blocked[layer][grid.idx(nx_, ny_)]:
                continue
            step = 1.0
            if prev_dir is not None and prev_dir != (dx, dy):
                step += bend_cost
            # HV bias: layer 0 (F.Cu) prefers horizontal (dy==0); last layer prefers vertical.
            if layer == 0 and dy != 0:
                step += HV_BIAS
            elif layer == len(grid.layers) - 1 and dx != 0:
                step += HV_BIAS
            nb = (layer, nx_, ny_)
            ng = g + step
            if ng < gscore.get(nb, math.inf):
                gscore[nb] = ng
                came[nb] = (node, (dx, dy))
                heapq.heappush(heap, (ng + h(*nb), ng, nb, (dx, dy)))
        # layer change (via) at this cell
        for nl in range(len(grid.layers)):
            if nl == layer:
                continue
            if blocked[nl][grid.idx(ix, iy)]:
                continue
            nb = (nl, ix, iy)
            ng = g + via_cost
            if ng < gscore.get(nb, math.inf):
                gscore[nb] = ng
                came[nb] = (node, None)  # None dir = via
                heapq.heappush(heap, (ng + h(*nb), ng, nb, prev_dir))
    return None


def _reconstruct(came: dict, node) -> list[tuple[int, int, int]]:
    path = [node]
    while came[node][0] is not None:
        node = came[node][0]
        path.append(node)
    path.reverse()
    return path


# ── path -> tracks + vias ─────────────────────────────────────────────────────────────

def _path_to_geometry(grid: _Grid, path: list[tuple[int, int, int]], net: Any,
                      width: float, tcnt: _IdCounter, vcnt: _IdCounter,
                      via_dia: float, via_drill: float,
                      p1_world: tuple[float, float],
                      p2_world: tuple[float, float]) -> tuple[list[dict], list[dict]]:
    """Compress a cell path into colinear track segments + insert a via at each layer change.

    The first and last points are snapped to the true pad world coordinates (``p1_world`` /
    ``p2_world``) so the routed copper physically reaches the pad centres — otherwise the grid-cell
    endpoint could miss the pad by up to half a pitch and the ratsnest would still see the net as
    unconnected.
    """
    tracks: list[dict] = []
    vias: list[dict] = []
    layers = grid.layers
    n = len(path)
    # split the path into same-layer runs separated by via points
    i = 0
    while i < n:
        layer = path[i][0]
        run = [path[i]]
        j = i + 1
        while j < n and path[j][0] == layer:
            run.append(path[j])
            j += 1
        # emit colinear-merged segments for this run
        pts = [grid.cell_xy(ix, iy) for (_, ix, iy) in run]
        if i == 0:
            pts[0] = p1_world          # snap start to the real source pad centre
        if j >= n:
            pts[-1] = p2_world         # snap end to the real target pad centre
        elif len(pts) == 1:
            # a single-cell run immediately before a via still needs a stub from the (snapped) pad
            # point to the via location so the copper is continuous; duplicate the cell so _emit_run
            # has two distinct points only if they differ (snapped start), else it is a no-op.
            via_xy = grid.cell_xy(path[j][1], path[j][2])
            if pts[0] != via_xy:
                pts.append(via_xy)
        _emit_run(pts, layers[layer], net, width, tracks, tcnt)
        # if there is a next run, the transition cell is a via
        if j < n:
            via_cell = path[j]  # same ix,iy, different layer
            vx, vy = grid.cell_xy(via_cell[1], via_cell[2])
            vias.append({
                "id": vcnt.next(), "net": net, "at": [float(vx), float(vy)],
                "drill": float(via_drill), "diameter": float(via_dia),
                "layers": [layers[0], layers[-1]], "type": "through",
            })
        i = j
    return tracks, vias


def _emit_run(pts: list[tuple[float, float]], layer: str, net: Any, width: float,
              tracks: list[dict], tcnt: _IdCounter) -> None:
    """Emit merged colinear segment tracks for a run of grid points on one layer."""
    if len(pts) < 2:
        return
    seg_start = pts[0]
    prev = pts[0]
    cur_dir: tuple[float, float] | None = None
    for p in pts[1:]:
        d = (_sign(p[0] - prev[0]), _sign(p[1] - prev[1]))
        if cur_dir is None:
            cur_dir = d
        elif d != cur_dir:
            # direction changed -> close the run at prev
            tracks.append(_mk_track(tcnt.next(), seg_start, prev, net, width, layer))
            seg_start = prev
            cur_dir = d
        prev = p
    if seg_start != prev:
        tracks.append(_mk_track(tcnt.next(), seg_start, prev, net, width, layer))


def _sign(v: float) -> int:
    return (v > 1e-9) - (v < -1e-9)


def _mk_track(tid: str, p1, p2, net, width, layer) -> dict:
    return {"id": tid, "net": net, "layer": layer, "width": float(width), "kind": "segment",
            "p1": [float(p1[0]), float(p1[1])], "p2": [float(p2[0]), float(p2[1])]}


# ── public API ──────────────────────────────────────────────────────────────────────

def autoroute(board: dict, opts: dict | None = None) -> dict:
    """Route the ratsnest airwires of ``board`` with a basic 2-layer grid (Lee/A*) router.

    Returns ``{"tracks": [...], "vias": [...], "report": {...}}``. PURE: ``board`` is not mutated;
    the returned tracks/vias are new dicts with fresh ids (allocated past the board's existing ids),
    net assigned, layer F.Cu/B.Cu, width from the net class or ``rules.track_width_min``. The caller
    applies them (or use :func:`route_board`).

    ``opts`` (all optional): ``pitch`` (mm, default 0.25), ``max_grid_cells`` (override the cap),
    ``via_cost``/``bend_cost`` (A* tuning).

    The report contains ``routed`` (net names with at least one airwire connected), ``failed`` (net
    names with an airwire that could not be routed), ``length_mm`` (total routed track length),
    ``airwires_before``/``airwires_after`` (ratsnest counts), ``vias`` (count), ``grid`` (the
    pitch/dimensions actually used), and ``note`` (an honest capability statement).
    """
    opts = opts or {}
    report: dict[str, Any] = {
        "routed": [], "failed": [], "length_mm": 0.0,
        "airwires_before": 0, "airwires_after": 0, "vias": 0, "grid": {},
        "note": ("basic grid (Lee/A*) router: orthogonal tracks + through-vias, best-effort, "
                 "single-pass, no rip-up/reroute or push-and-shove; failed nets are reported"),
    }
    out_tracks: list[dict] = []
    out_vias: list[dict] = []

    if not schema.is_board(board):
        return {"tracks": out_tracks, "vias": out_vias, "report": report}

    airwires = drc.ratsnest(board)
    report["airwires_before"] = len(airwires)
    if not airwires:
        report["grid"] = {"pitch": float(opts.get("pitch", DEFAULT_PITCH)), "nx": 0, "ny": 0}
        return {"tracks": out_tracks, "vias": out_vias, "report": report}

    # choose a pitch that keeps the grid within the cell cap (coarsen for big boards)
    pitch = float(opts.get("pitch", DEFAULT_PITCH))
    max_cells = int(opts.get("max_grid_cells", MAX_GRID_CELLS))
    minx, miny, maxx, maxy = _board_bbox(board)
    w, hgt = maxx - minx, maxy - miny
    while pitch > 0 and ((int(w / pitch) + 1) * (int(hgt / pitch) + 1)) > max_cells:
        pitch *= 1.5
    grid = _Grid(board, pitch)
    report["grid"] = {"pitch": round(pitch, 4), "nx": grid.nx, "ny": grid.ny,
                      "layers": len(grid.layers)}

    # per-call A* tuning (passed into _astar; no global mutation)
    via_cost = float(opts.get("via_cost", VIA_COST))
    bend_cost = float(opts.get("bend_cost", BEND_COST))

    tcnt = _IdCounter(board, "t", "tracks")
    vcnt = _IdCounter(board, "v", "vias")

    # route order: shortest airwire first (deterministic tiebreak by net + endpoints)
    def _len(a):
        return math.hypot(a["p1"][0] - a["p2"][0], a["p1"][1] - a["p2"][1])

    ordered = sorted(airwires, key=lambda a: (_len(a), a["net"], a["p1"], a["p2"]))

    # working copy of the board's copper for incremental obstacle updates: we re-add routed tracks/
    # vias as shapes so later nets avoid them. We never touch the input board.
    routed_shapes: list[drc._Shape] = []
    routed_nets: set = set()
    failed_nets: set = set()

    for air in ordered:
        net = air["net"]
        blocked = grid.build_blocked(net)
        # bake previously-routed copper (this run) into the blocked maps with its own halo
        _apply_routed_obstacles(grid, blocked, routed_shapes, net)

        starts = _terminal_cells(grid, blocked, air["p1"])
        goals = _terminal_cells(grid, blocked, air["p2"])
        if not starts or not goals:
            failed_nets.add(net)
            continue
        path = _astar(grid, blocked, starts, goals, tuple(air["p2"]), via_cost, bend_cost)
        if path is None:
            failed_nets.add(net)
            continue
        width = _track_width(board, net)
        via_dia, via_drill = _via_dims(board, net)
        segs, vias = _path_to_geometry(
            grid, path, net, width, tcnt, vcnt, via_dia, via_drill,
            (float(air["p1"][0]), float(air["p1"][1])),
            (float(air["p2"][0]), float(air["p2"][1])))
        out_tracks.extend(segs)
        out_vias.extend(vias)
        routed_nets.add(net)
        # register the new copper as obstacles for subsequent nets
        for t in segs:
            sh = drc._track_shape(t)
            if sh is not None:
                routed_shapes.append(sh)
        for v in vias:
            routed_shapes.append(drc._via_shape(v))

    # build a routed board (copy) to measure the resulting ratsnest honestly
    routed_board = copy.deepcopy(board)
    routed_board["tracks"] = list(routed_board.get("tracks", [])) + out_tracks
    routed_board["vias"] = list(routed_board.get("vias", [])) + out_vias
    after = drc.ratsnest(routed_board)
    report["airwires_after"] = len(after)

    length = sum(math.hypot(t["p2"][0] - t["p1"][0], t["p2"][1] - t["p1"][1]) for t in out_tracks)
    report["length_mm"] = round(length, 4)
    report["vias"] = len(out_vias)
    # Ground truth from the post-route ratsnest: a net is FULLY routed iff it has no airwire left;
    # any net that still has an airwire (A* failure on a leg, or a multi-leg net only partly done)
    # is reported as failed. Nets considered = every net we touched plus any still-unrouted net.
    after_nets = {a["net"] for a in after}
    touched = routed_nets | failed_nets
    report["routed"] = sorted(touched - after_nets)
    report["failed"] = sorted(after_nets)
    return {"tracks": out_tracks, "vias": out_vias, "report": report}


def _apply_routed_obstacles(grid: _Grid, blocked: list[list[bool]],
                            routed_shapes: list[drc._Shape], net: Any) -> None:
    """Block cells near already-routed copper of OTHER nets (this run's incremental obstacles)."""
    clr = _clearance(grid.board, net)
    halo = clr + _track_width(grid.board, net) / 2.0
    for li, ly in enumerate(grid.layers):
        shapes = [s for s in routed_shapes if ly in s.layers and not drc.same_net(s.net, net)]
        if shapes:
            grid._mark_shapes(blocked[li], shapes, halo)


def _terminal_cells(grid: _Grid, blocked: list[list[bool]],
                    at: list[float]) -> set[tuple[int, int, int]]:
    """Routable cells representing a pad terminal: the nearest unblocked cell on each layer, plus
    its immediate unblocked neighbours (so a pad sitting between grid lines still has an anchor)."""
    cx, cy = grid.nearest_cell((at[0], at[1]))
    out: set[tuple[int, int, int]] = set()
    for li in range(len(grid.layers)):
        # search outward in rings for the closest unblocked cell on this layer
        found = None
        for r in range(0, 4):
            cands = []
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    ix, iy = cx + dx, cy + dy
                    if 0 <= ix < grid.nx and 0 <= iy < grid.ny \
                            and not blocked[li][grid.idx(ix, iy)]:
                        cands.append((ix, iy))
            if cands:
                # deterministic: closest to the true pad point, then index order
                cands.sort(key=lambda c: (drc._dist(grid.cell_xy(*c), (at[0], at[1])), c))
                found = cands[0]
                break
        if found is not None:
            out.add((li, found[0], found[1]))
    return out


def route_board(board: dict, opts: dict | None = None) -> dict:
    """Convenience: return a deep-copied board with the autorouted tracks/vias applied.

    The input ``board`` is not mutated. Equivalent to applying :func:`autoroute`'s output.
    """
    res = autoroute(board, opts)
    out = copy.deepcopy(board)
    out["tracks"] = list(out.get("tracks", [])) + res["tracks"]
    out["vias"] = list(out.get("vias", [])) + res["vias"]
    return out
