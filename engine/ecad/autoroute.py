"""Basic grid (Lee/maze) PCB autorouter for aicad.pcb/1 boards.

A best-effort, *honestly basic* 2-layer grid router -- NOT a production push-and-shove engine like
KiCad's interactive router. It connects the ratsnest airwires (see :func:`engine.ecad.drc.ratsnest`)
with orthogonal track segments and layer-change vias, respecting copper clearance against
already-placed copper of other nets.

Design (documented trade-offs):

* **Grid.** A uniform square grid is laid over the board bounding box (the Edge.Cuts outline if
  present, else the copper extent) at a configurable ``pitch`` (default 0.25 mm). Two routing layers
  (``F.Cu``, ``B.Cu``). Finer pitch -> routes thread tighter gaps and look cleaner, but cost grows as
  O(cells) = O(area / pitch^2) per layer per net, so the grid is *capped*: if width*height in cells
  would exceed :data:`MAX_GRID_CELLS` the pitch is coarsened until it fits (reported in
  ``report["grid"]``). For a big board this means the router silently uses a coarser grid rather than
  blowing up -- coarser grid = it may fail to find gaps a finer grid would.

* **Obstacles.** Every cell is tested against the board's copper that belongs to a *different* net
  (pads of other nets, plus tracks/vias from earlier-routed nets). A cell is blocked on a layer if a
  point there would sit closer than ``clearance + half_track_width`` to that copper -- reusing the
  exact DRC shape/edge-distance geometry (:mod:`engine.ecad.drc`) so a routed track does not create a
  new ``clearance`` violation. Pads of the *current* net are carved back out as routable
  sources/targets. Cells outside the outline (or within ``edge_clearance`` of it) are blocked.

* **Search.** Per airwire, A* (Lee BFS with a Manhattan heuristic) from the source pad's cell to the
  target pad's cell across the two-layer cell graph. Moving to an orthogonal neighbour on the same
  layer costs 1 (plus a small bend penalty to discourage zig-zag and an HV-bias so F.Cu prefers
  horizontal and B.Cu prefers vertical, reducing crossings). Switching layer at a cell costs
  :data:`VIA_COST` and drops a via. A net is *failed* (reported, never fatal) when no path exists.

* **Apply.** The cell path is compressed into maximal colinear runs -> one ``segment`` track per run on
  its layer, with a ``via`` inserted at each layer change. Newly-routed copper is then baked into the
  obstacle map (with its own clearance halo) so later nets avoid it.

Net ordering: airwires are routed shortest-Euclidean-first (a cheap, deterministic heuristic -- short
nets are easy and grabbing them first frees the map; it is not an optimal global order).

v2 additions (additive; the single-pass behaviour above is unchanged when the new opts are off):

* **Rip-up-and-reroute** (``opts.ripup_passes``, default 2). After the greedy pass, each FAILED
  airwire gets a recovery attempt: the already-routed copper of OTHER nets that blocks its best path
  is temporarily RIPPED UP (removed from the occupied map), the failed net is routed into the freed
  space, then the ripped victims are re-routed (possibly on a different layer/path). Each victim has
  a per-net rip-up budget (``opts.ripup_budget``, default 2 times it may be torn out) and the whole
  thing is bounded by ``ripup_passes`` so it always terminates. The net effect is *more nets routed*
  than the greedy pass on boards where the greedy net order walls a later net off. Honest limit: this
  is recovery-by-eviction, not a true negotiated-congestion (PathFinder) router -- it improves the
  common "bad order" failures, not every topological dead end.

* **Differential pairs** (``opts.diff_pairs`` or auto-detect by ``*_P``/``*_N``, ``*+``/``*-``,
  ``*_p``/``*_n`` naming). A pair is routed COUPLED: the primary (``_P``) net is routed first, then
  the complement (``_N``) is routed as a track offset by the net-class ``diff_gap`` (or
  ``opts.diff_gap``) running parallel to the primary along its length, on the same layer. The two are
  then **length-matched**: the shorter track gets a basic accordion/serpentine meander inserted to
  add length until the skew is within ``opts.length_tol``. Honest limits: the coupling is a geometric
  parallel-offset of the primary's centreline (not a simultaneous two-net search), the meander is a
  simple rectangular accordion on a straight run with a bounded number of teeth, and both are kept
  DRC-clean against OTHER nets but the intra-pair gap is intentional (same-net, never a violation).

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

# -- tunables (documented in the module docstring) ----------------------------------

DEFAULT_PITCH = 0.25          # mm; grid resolution
MAX_GRID_CELLS = 240_000      # per-layer cell cap; pitch coarsens until w*h <= this
VIA_COST = 12.0               # A* cost of a layer change (in grid steps)
BEND_COST = 0.6               # extra cost to turn (discourages staircasing)
HV_BIAS = 0.35                # extra cost for the non-preferred direction on a layer
DEFAULT_VIA_DIAMETER = 0.8    # mm, used when no net-class via_diameter is set
DEFAULT_VIA_DRILL = 0.4       # mm

# v2 rip-up-and-reroute
DEFAULT_RIPUP_PASSES = 2      # how many recovery sweeps over the failed set
DEFAULT_RIPUP_BUDGET = 2      # max times any one net may be torn out (prevents thrash)

# v2 differential pairs
DEFAULT_DIFF_GAP = 0.2        # mm, intra-pair edge gap when no net-class diff_gap is set
DEFAULT_LENGTH_TOL = 0.2      # mm, max acceptable P/N skew after meandering
MAX_MEANDER_TEETH = 200       # hard cap on accordion teeth (bounds the meander loop)


# -- small helpers -------------------------------------------------------------------

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


def _diff_gap(board: dict, net: Any, override: float | None = None) -> float:
    """Target intra-pair EDGE gap for a diff pair: explicit ``override`` > net-class ``diff_gap`` >
    DEFAULT_DIFF_GAP. This is the copper-to-copper spacing the complement is offset by."""
    if override is not None:
        return float(override)
    nc = _net_class(board, net)
    if "diff_gap" in nc:
        return float(nc["diff_gap"])
    return DEFAULT_DIFF_GAP


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


# -- board bounding box ---------------------------------------------------------------

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


# -- obstacle grid ------------------------------------------------------------------

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


# -- A* over the two-layer cell graph -------------------------------------------------

def _astar(grid: _Grid, blocked: list[list[bool]],
           starts: set[tuple[int, int, int]], goals: set[tuple[int, int, int]],
           goal_xy: tuple[float, float],
           via_cost: float = VIA_COST, bend_cost: float = BEND_COST,
           ) -> list[tuple[int, int, int]] | None:
    """A* from any start node to any goal node. Nodes are (layer_idx, ix, iy).

    Cost: 1 per orthogonal same-layer step + ``bend_cost`` on a turn + HV bias; ``via_cost`` per
    layer change. Heuristic: Manhattan distance (in cells) to the goal centroid -- admissible since
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


# -- path -> tracks + vias -------------------------------------------------------------

def _path_to_geometry(grid: _Grid, path: list[tuple[int, int, int]], net: Any,
                      width: float, tcnt: _IdCounter, vcnt: _IdCounter,
                      via_dia: float, via_drill: float,
                      p1_world: tuple[float, float],
                      p2_world: tuple[float, float]) -> tuple[list[dict], list[dict]]:
    """Compress a cell path into colinear track segments + insert a via at each layer change.

    The first and last points are snapped to the true pad world coordinates (``p1_world`` /
    ``p2_world``) so the routed copper physically reaches the pad centres -- otherwise the grid-cell
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


# -- routed-copper registry (per-net ownership, for rip-up) ----------------------------
#
# To rip a net up we must be able to remove exactly its tracks/vias from the obstacle map. A flat
# shape list cannot do that, so we key routed geometry by net. ``active_shapes`` (the obstacle source
# fed to build_blocked-style marking) is rebuilt from the registry whenever a net is added/removed.

class _RoutedRegistry:
    """Per-net store of the tracks/vias the router has placed, with obstacle shapes derived on demand.

    ``tracks[net]`` / ``vias[net]`` hold the emitted dicts; ``shapes_for(exclude_net)`` returns the
    obstacle shapes belonging to every OTHER net (the current net's own copper is source/target, not
    an obstacle). Ripping a net is just ``pop(net)``; re-adding is ``add(net, tracks, vias)``.
    """

    def __init__(self) -> None:
        self.tracks: dict[Any, list[dict]] = {}
        self.vias: dict[Any, list[dict]] = {}
        self.ripup_count: dict[Any, int] = {}

    def add(self, net: Any, tracks: list[dict], vias: list[dict]) -> None:
        self.tracks.setdefault(net, []).extend(tracks)
        self.vias.setdefault(net, []).extend(vias)

    def pop(self, net: Any) -> tuple[list[dict], list[dict]]:
        t = self.tracks.pop(net, [])
        v = self.vias.pop(net, [])
        self.ripup_count[net] = self.ripup_count.get(net, 0) + 1
        return t, v

    def shapes_for(self, exclude_net: Any) -> list[drc._Shape]:
        out: list[drc._Shape] = []
        for net in sorted(self.tracks, key=str):
            if drc.same_net(net, exclude_net):
                continue
            for t in self.tracks[net]:
                sh = drc._track_shape(t)
                if sh is not None:
                    out.append(sh)
        for net in sorted(self.vias, key=str):
            if drc.same_net(net, exclude_net):
                continue
            for v in self.vias[net]:
                out.append(drc._via_shape(v))
        return out

    def all_tracks(self) -> list[dict]:
        out: list[dict] = []
        for net in sorted(self.tracks, key=str):
            out.extend(self.tracks[net])
        return out

    def all_vias(self) -> list[dict]:
        out: list[dict] = []
        for net in sorted(self.vias, key=str):
            out.extend(self.vias[net])
        return out


def _blocked_for(grid: _Grid, net: Any, reg: _RoutedRegistry,
                 extra_shapes: list[drc._Shape] | None = None) -> list[list[bool]]:
    """Per-layer blocked map for ``net``: static other-net copper (from the board) + the registry's
    routed copper of other nets + any ``extra_shapes`` (e.g. a coupled diff partner to avoid)."""
    blocked = grid.build_blocked(net)
    clr = _clearance(grid.board, net)
    halo = clr + _track_width(grid.board, net) / 2.0
    obstacles = reg.shapes_for(net)
    if extra_shapes:
        obstacles = obstacles + [s for s in extra_shapes if not drc.same_net(s.net, net)]
    for li, ly in enumerate(grid.layers):
        shp = [s for s in obstacles if ly in s.layers]
        if shp:
            grid._mark_shapes(blocked[li], shp, halo)
    return blocked


def _route_airwire(grid: _Grid, board: dict, air: dict, reg: _RoutedRegistry,
                   tcnt: _IdCounter, vcnt: _IdCounter,
                   via_cost: float, bend_cost: float,
                   extra_shapes: list[drc._Shape] | None = None,
                   ) -> tuple[list[dict], list[dict]] | None:
    """Route one airwire against the current registry. Returns (tracks, vias) or None on failure.

    Does NOT register the result (the caller decides whether to keep it). ``extra_shapes`` are
    additional obstacles to avoid (used so a diff-pair complement avoids its own primary's halo
    except for the intentional gap, and so coupled pairs avoid each other where required)."""
    net = air["net"]
    blocked = _blocked_for(grid, net, reg, extra_shapes)
    starts = _terminal_cells(grid, blocked, air["p1"])
    goals = _terminal_cells(grid, blocked, air["p2"])
    if not starts or not goals:
        return None
    path = _astar(grid, blocked, starts, goals, tuple(air["p2"]), via_cost, bend_cost)
    if path is None:
        return None
    width = _track_width(board, net)
    via_dia, via_drill = _via_dims(board, net)
    return _path_to_geometry(
        grid, path, net, width, tcnt, vcnt, via_dia, via_drill,
        (float(air["p1"][0]), float(air["p1"][1])),
        (float(air["p2"][0]), float(air["p2"][1])))


# -- public API ----------------------------------------------------------------------

def autoroute(board: dict, opts: dict | None = None) -> dict:
    """Route the ratsnest airwires of ``board`` with a basic 2-layer grid (Lee/A*) router.

    Returns ``{"tracks": [...], "vias": [...], "report": {...}}``. PURE: ``board`` is not mutated;
    the returned tracks/vias are new dicts with fresh ids (allocated past the board's existing ids),
    net assigned, layer F.Cu/B.Cu, width from the net class or ``rules.track_width_min``. The caller
    applies them (or use :func:`route_board`).

    ``opts`` (all optional): ``pitch`` (mm, default 0.25), ``max_grid_cells`` (override the cap),
    ``via_cost``/``bend_cost`` (A* tuning), ``ripup_passes`` (int, default 2 -- set 0 for the legacy
    single greedy pass), ``ripup_budget`` (int, default 2), ``diff_pairs`` (list of ``[net_p, net_n]``;
    omitted/empty -> auto-detect by ``*_P/*_N`` naming), ``diff_gap`` (mm intra-pair gap override),
    ``length_tol`` (mm P/N skew tolerance), ``no_auto_diff`` (bool, skip naming auto-detect).

    The report contains ``routed`` (net names with at least one airwire connected), ``failed`` (net
    names with an airwire that could not be routed), ``length_mm`` (total routed track length),
    ``airwires_before``/``airwires_after`` (ratsnest counts), ``vias`` (count), ``grid`` (the
    pitch/dimensions actually used), ``ripup_iterations`` (rip-up recovery attempts performed),
    ``diff_pairs`` (per-pair coupling/length-match stats), and ``note``.
    """
    opts = opts or {}
    report: dict[str, Any] = {
        "routed": [], "failed": [], "length_mm": 0.0,
        "airwires_before": 0, "airwires_after": 0, "vias": 0, "grid": {},
        "ripup_iterations": 0, "diff_pairs": [],
        "note": ("basic grid (Lee/A*) router: orthogonal tracks + through-vias, best-effort, "
                 "with bounded rip-up-and-reroute + parallel-offset differential pairs; "
                 "no push-and-shove / negotiated-congestion; failed nets are reported"),
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
    ripup_passes = max(0, int(opts.get("ripup_passes", DEFAULT_RIPUP_PASSES)))
    ripup_budget = max(0, int(opts.get("ripup_budget", DEFAULT_RIPUP_BUDGET)))

    tcnt = _IdCounter(board, "t", "tracks")
    vcnt = _IdCounter(board, "v", "vias")

    reg = _RoutedRegistry()

    # -- 1) differential pairs first (they want the cleanest, most-coupled space) --
    diff_pairs = _resolve_diff_pairs(board, airwires, opts)
    diff_nets: set = set()
    for (np_net, nn_net) in diff_pairs:
        diff_nets.add(np_net)
        diff_nets.add(nn_net)
        stats = _route_diff_pair_into(grid, board, np_net, nn_net, reg,
                                      tcnt, vcnt, via_cost, bend_cost, opts)
        report["diff_pairs"].append(stats)

    # -- 2) greedy single pass over the remaining airwires, shortest-first --
    def _len(a):
        return math.hypot(a["p1"][0] - a["p2"][0], a["p1"][1] - a["p2"][1])

    single_airwires = [a for a in airwires if a["net"] not in diff_nets]
    ordered = sorted(single_airwires, key=lambda a: (_len(a), a["net"], a["p1"], a["p2"]))

    failed_airwires: list[dict] = []
    for air in ordered:
        geom = _route_airwire(grid, board, air, reg, tcnt, vcnt, via_cost, bend_cost)
        if geom is None:
            failed_airwires.append(air)
            continue
        reg.add(air["net"], geom[0], geom[1])

    # -- 3) bounded rip-up-and-reroute: try to rescue the failed airwires --
    if ripup_passes > 0 and failed_airwires:
        # map net -> its TRUE pad-to-pad airwire(s) (so a ripped victim re-routes its real
        # connection, not a span reconstructed from its possibly-blocked routed copper)
        airwire_map: dict[Any, list[dict]] = {}
        for a in single_airwires:
            airwire_map.setdefault(a["net"], []).append(a)
        report["ripup_iterations"] = _ripup_reroute(
            grid, board, failed_airwires, airwire_map, reg, tcnt, vcnt,
            via_cost, bend_cost, ripup_passes, ripup_budget)

    out_tracks = reg.all_tracks()
    out_vias = reg.all_vias()

    # build a routed board (copy) to measure the resulting ratsnest honestly
    routed_board = copy.deepcopy(board)
    routed_board["tracks"] = list(routed_board.get("tracks", [])) + out_tracks
    routed_board["vias"] = list(routed_board.get("vias", [])) + out_vias
    after = drc.ratsnest(routed_board)
    report["airwires_after"] = len(after)

    length = sum(math.hypot(t["p2"][0] - t["p1"][0], t["p2"][1] - t["p1"][1]) for t in out_tracks)
    report["length_mm"] = round(length, 4)
    report["vias"] = len(out_vias)
    # Ground truth from the post-route ratsnest: a net is FULLY routed iff it has no airwire left.
    after_nets = {a["net"] for a in after}
    touched = {a["net"] for a in airwires}
    report["routed"] = sorted(touched - after_nets)
    report["failed"] = sorted(after_nets)
    return {"tracks": out_tracks, "vias": out_vias, "report": report}


# -- rip-up-and-reroute ----------------------------------------------------------------

def _ripup_reroute(grid: _Grid, board: dict, failed: list[dict],
                   airwire_map: dict[Any, list[dict]],
                   reg: _RoutedRegistry, tcnt: _IdCounter, vcnt: _IdCounter,
                   via_cost: float, bend_cost: float,
                   passes: int, budget: int) -> int:
    """Try to route each still-failed airwire by tearing out the routed copper of OTHER nets that
    blocks its best path, routing it into the freed space, then re-routing the evicted victims.

    Strategy (recovery-by-eviction, deterministic, bounded):
      For each failed airwire (in a stable order), find the obstacle nets whose halo currently sits on
      the cells along the straight pad-to-pad corridor (the cheapest thing to remove). Rip ALL of them
      out at once (respecting each victim's per-net rip-up budget), route the failed net into the now-
      emptier map, then greedily re-route every ripped victim using its TRUE pad airwire(s). If the
      failed net AND every victim all route, commit; otherwise roll back so we never make things worse.

    Bounded by ``passes`` sweeps and per-net ``budget`` (a net torn out ``budget`` times is no longer
    eligible as a victim). Returns the number of rip-up recovery attempts performed.
    """
    iterations = 0
    pending = list(failed)
    for _pass in range(passes):
        if not pending:
            break
        still: list[dict] = []
        # deterministic order: longest corridor first (hardest), tiebreak by net + endpoints
        pending.sort(key=lambda a: (-math.hypot(a["p1"][0] - a["p2"][0], a["p1"][1] - a["p2"][1]),
                                    a["net"], a["p1"], a["p2"]))
        for air in pending:
            iterations += 1
            if _try_rescue(grid, board, air, airwire_map, reg, tcnt, vcnt,
                           via_cost, bend_cost, budget):
                continue
            still.append(air)
        if len(still) == len(pending):
            break  # a whole sweep rescued nothing -> no progress, stop
        pending = still
    return iterations


def _try_rescue(grid: _Grid, board: dict, air: dict, airwire_map: dict[Any, list[dict]],
                reg: _RoutedRegistry, tcnt: _IdCounter, vcnt: _IdCounter,
                via_cost: float, bend_cost: float, budget: int) -> bool:
    """Attempt to rescue one failed airwire by rip-up. Returns True iff it (and every ripped victim's
    real connection) ended up routed; commits on success, fully rolls back on failure.

    Each ripped victim is re-routed from its TRUE pad-to-pad airwire(s) (``airwire_map``), so a victim
    can only be 'rerouted' if its real pads reconnect -- a path that fails to physically reach the pads
    is treated as a failure and triggers rollback (so rip-up never trades one routed net for another)."""
    net = air["net"]
    victims = _blocking_nets(grid, board, air, reg, budget)
    if not victims:
        # nothing eligible to remove; maybe the map changed since the greedy pass -> one more try
        geom = _route_airwire(grid, board, air, reg, tcnt, vcnt, via_cost, bend_cost)
        if geom is not None:
            reg.add(net, geom[0], geom[1])
            return True
        return False

    # snapshot the victims so we can restore on rollback
    snapshot: dict[Any, tuple[list[dict], list[dict]]] = {}
    victim_airwires: list[dict] = []
    for v in victims:
        t, vi = reg.pop(v)
        snapshot[v] = (t, vi)
        # re-make the victim's REAL connection(s) from the original ratsnest, not a span guessed from
        # its (now-removed) copper. Fall back to the routed-span if the net has no recorded airwire.
        vws = airwire_map.get(v)
        if vws:
            victim_airwires.extend(vws)
        else:
            victim_airwires.append(_airwire_from_geometry(v, t, vi))

    # route the previously-failed net into the freed space
    geom = _route_airwire(grid, board, air, reg, tcnt, vcnt, via_cost, bend_cost)
    if geom is None:
        _restore(reg, snapshot)
        return False
    reg.add(net, geom[0], geom[1])

    # re-route every ripped victim (shortest corridor first, deterministic)
    victim_airwires.sort(key=lambda a: (math.hypot(a["p1"][0] - a["p2"][0], a["p1"][1] - a["p2"][1]),
                                        a["net"], a["p1"], a["p2"]))
    placed: list[Any] = [net]
    ok = True
    for rv in victim_airwires:
        g = _route_airwire(grid, board, rv, reg, tcnt, vcnt, via_cost, bend_cost)
        if g is None or not _geometry_connects(g[0], g[1], rv):
            ok = False
            break
        reg.add(rv["net"], g[0], g[1])
        placed.append(rv["net"])
    if not ok:
        # rollback: drop everything we placed in this rescue, restore the snapshot
        for r in placed:
            reg.tracks.pop(r, None)
            reg.vias.pop(r, None)
        _restore(reg, snapshot)
        return False
    return True


def _geometry_connects(tracks: list[dict], vias: list[dict], air: dict) -> bool:
    """True if the routed copper physically reaches BOTH airwire endpoints (a touch at each pad).

    Guards against an A* 'success' that ended at a near-but-blocked cell instead of the real pad,
    which would leave a dangling track that the ratsnest still counts as unrouted."""
    pts: list[tuple[float, float]] = []
    for t in tracks:
        pts.append((float(t["p1"][0]), float(t["p1"][1])))
        pts.append((float(t["p2"][0]), float(t["p2"][1])))
    for v in vias:
        pts.append((float(v["at"][0]), float(v["at"][1])))
    if not pts:
        return False
    p1 = (float(air["p1"][0]), float(air["p1"][1]))
    p2 = (float(air["p2"][0]), float(air["p2"][1]))
    near1 = any(drc._dist(p, p1) <= 1e-6 for p in pts)
    near2 = any(drc._dist(p, p2) <= 1e-6 for p in pts)
    return near1 and near2


def _blocking_nets(grid: _Grid, board: dict, air: dict, reg: _RoutedRegistry,
                   budget: int) -> list[Any]:
    """Routed OTHER-net(s) whose halo currently blocks a cell on the straight corridor between the
    airwire's pads. These are the cheapest victims to evict. Each is returned at most once, in a
    deterministic order; nets over their rip-up budget are skipped."""
    net = air["net"]
    p1 = grid.nearest_cell((air["p1"][0], air["p1"][1]))
    p2 = grid.nearest_cell((air["p2"][0], air["p2"][1]))
    corridor = _bresenham(p1, p2)
    clr = _clearance(board, net)
    halo = clr + _track_width(board, net) / 2.0
    victims: set = set()
    for vnet in sorted(reg.tracks, key=str):
        if drc.same_net(vnet, net):
            continue
        if reg.ripup_count.get(vnet, 0) >= budget:
            continue
        shapes = [drc._track_shape(t) for t in reg.tracks[vnet]]
        shapes += [drc._via_shape(v) for v in reg.vias.get(vnet, [])]
        shapes = [s for s in shapes if s is not None]
        for (ix, iy) in corridor:
            p = grid.cell_xy(ix, iy)
            if any(_Grid._cell_clear_dist(s, p) < halo for s in shapes):
                victims.add(vnet)
                break
    return sorted(victims, key=str)


def _bresenham(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """Integer grid line from ``a`` to ``b`` (the straight pad-to-pad corridor in cell space)."""
    (x0, y0), (x1, y1) = a, b
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    out = []
    while True:
        out.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return out


def _airwire_from_geometry(net: Any, tracks: list[dict], vias: list[dict]) -> dict:
    """Synthesize the airwire a ripped net needs re-made: the two extreme track endpoints (the
    routed copper's overall span). Falls back to a via pair if there are no tracks."""
    pts: list[tuple[float, float]] = []
    for t in tracks:
        pts.append((float(t["p1"][0]), float(t["p1"][1])))
        pts.append((float(t["p2"][0]), float(t["p2"][1])))
    if not pts:
        for v in vias:
            pts.append((float(v["at"][0]), float(v["at"][1])))
    if len(pts) < 2:
        p = pts[0] if pts else (0.0, 0.0)
        return {"net": net, "p1": [p[0], p[1]], "p2": [p[0], p[1]]}
    # the two endpoints farthest apart approximate the pad-to-pad span (deterministic)
    best = (-1.0, pts[0], pts[0])
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = drc._dist(pts[i], pts[j])
            if d > best[0]:
                best = (d, pts[i], pts[j])
    _, a, b = best
    # canonical endpoint order for determinism
    if (a[0], a[1]) > (b[0], b[1]):
        a, b = b, a
    return {"net": net, "p1": [a[0], a[1]], "p2": [b[0], b[1]]}


def _restore(reg: _RoutedRegistry, snapshot: dict[Any, tuple[list[dict], list[dict]]]) -> None:
    for net, (t, v) in snapshot.items():
        reg.tracks[net] = t
        reg.vias[net] = v


# -- differential pairs ----------------------------------------------------------------
#
# Coupling approach (documented limits): we route the PRIMARY (positive) net normally with A*, then
# build the COMPLEMENT by offsetting the primary's polyline sideways by the pitch that yields the
# target intra-pair edge gap (gap + half(width_p) + half(width_n)), choosing the offset side that
# keeps the complement inside the board and clear of OTHER nets. Both share a layer. This is a
# geometric parallel-offset, not a simultaneous two-net maze search, so it is exact on straight runs
# and corners but does not negotiate obstacles independently for the two members. If the offset
# complement collides with other-net copper we fall back to routing the complement independently with
# A* (still same layer when possible) so the pair still connects (reported coupled_pct < 100).
#
# Length match: after both are routed we compare path lengths and add a rectangular accordion meander
# to the SHORTER member on its longest straight segment until skew <= length_tol or the tooth cap is
# hit. The accordion amplitude is sized to keep its own clearance; teeth are bounded by
# MAX_MEANDER_TEETH. This adds length, never removes it, so it cannot break the existing connection.

_DIFF_SUFFIXES = (("_P", "_N"), ("_p", "_n"), ("+", "-"))


def _diff_complement(name: str) -> tuple[str, bool] | None:
    """If ``name`` looks like one half of a diff pair, return (partner_name, is_positive)."""
    for pos, neg in _DIFF_SUFFIXES:
        if name.endswith(pos):
            return name[: -len(pos)] + neg, True
        if name.endswith(neg):
            return name[: -len(neg)] + pos, False
    return None


def _resolve_diff_pairs(board: dict, airwires: list[dict], opts: dict) -> list[tuple[str, str]]:
    """Build the ordered list of (positive_net, negative_net) pairs to route coupled.

    Explicit ``opts.diff_pairs`` wins; otherwise auto-detect by naming unless ``opts.no_auto_diff``.
    Only pairs where BOTH nets actually have an airwire to route are kept. Deterministic order."""
    have = {a["net"] for a in airwires}
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    explicit = opts.get("diff_pairs")
    if explicit:
        for entry in explicit:
            if not entry or len(entry) < 2:
                continue
            a, b = str(entry[0]), str(entry[1])
            if a in seen or b in seen or a not in have or b not in have:
                continue
            # canonical: positive member first if we can tell from the name, else as given
            comp = _diff_complement(a)
            if comp is not None and not comp[1]:
                a, b = b, a
            pairs.append((a, b))
            seen.add(a); seen.add(b)
        return pairs

    if opts.get("no_auto_diff"):
        return pairs

    for net in sorted(have):
        if net in seen:
            continue
        comp = _diff_complement(net)
        if comp is None:
            continue
        partner, is_pos = comp
        if partner not in have or partner in seen:
            continue
        pair = (net, partner) if is_pos else (partner, net)
        pairs.append(pair)
        seen.add(pair[0]); seen.add(pair[1])
    return pairs


def _polyline_of(net: Any, tracks: list[dict]) -> list[tuple[float, float]]:
    """Order a net's segment tracks into a single open polyline from one end to the other.

    Greedy endpoint chaining; tolerant to float noise. Returns [] if it cannot form a chain."""
    segs = [((float(t["p1"][0]), float(t["p1"][1])), (float(t["p2"][0]), float(t["p2"][1])))
            for t in tracks if t.get("kind", "segment") == "segment"]
    if not segs:
        return []
    # endpoint multiplicity: an endpoint touched once is a terminus
    def key(p):
        return (round(p[0], 4), round(p[1], 4))

    from collections import defaultdict
    adj: dict = defaultdict(list)
    for (a, b) in segs:
        adj[key(a)].append((key(b), b))
        adj[key(b)].append((key(a), a))
    ends = [k for k, v in adj.items() if len(v) == 1]
    if not ends:
        return []
    start = min(ends)  # deterministic terminus
    # reconstruct real coords: start xy is the seg endpoint matching `start`
    start_xy = None
    for (a, b) in segs:
        if key(a) == start:
            start_xy = a
            break
        if key(b) == start:
            start_xy = b
            break
    if start_xy is None:
        return []
    # walk the chain, consuming each edge once
    poly: list[tuple[float, float]] = [start_xy]
    visited_edges: set = set()
    cur = start
    steps = 0
    while steps <= len(segs) + 1:
        steps += 1
        nxt = None
        for (nk, nxy) in adj[cur]:
            edge = tuple(sorted([cur, nk]))
            if edge in visited_edges:
                continue
            nxt = (nk, nxy)
            visited_edges.add(edge)
            break
        if nxt is None:
            break
        poly.append(nxt[1])
        cur = nxt[0]
    return poly


def _polyline_length(poly: list[tuple[float, float]]) -> float:
    return sum(drc._dist(poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def _polyline_to_tracks(poly: list[tuple[float, float]], net: Any, width: float, layer: str,
                        tcnt: _IdCounter) -> list[dict]:
    """Emit merged colinear segment tracks for a polyline (reuses the colinear-merge of _emit_run)."""
    tracks: list[dict] = []
    _emit_run(list(poly), layer, net, width, tracks, tcnt)
    return tracks


def _poly_clear_of_others(grid: _Grid, board: dict, poly: list[tuple[float, float]], layer: str,
                          net: Any, reg: _RoutedRegistry, partner: Any) -> bool:
    """True if every vertex/midpoint of ``poly`` keeps clearance to OTHER-net copper (not the
    partner, not this net) and stays inside the routable outline."""
    clr = _clearance(board, net)
    halo = clr + _track_width(board, net) / 2.0
    others = [s for s in (drc._copper_shapes(board) + reg.shapes_for(net))
              if layer in s.layers and not drc.same_net(s.net, net)
              and not drc.same_net(s.net, partner)]
    # sample vertices + segment midpoints
    samples: list[tuple[float, float]] = list(poly)
    for i in range(len(poly) - 1):
        samples.append(((poly[i][0] + poly[i + 1][0]) / 2.0,
                        (poly[i][1] + poly[i + 1][1]) / 2.0))
    for p in samples:
        ix, iy = grid.nearest_cell(p)
        if not grid._outline_mask[grid.idx(ix, iy)]:
            # outside outline (sampled); allow a small slack near the pad endpoints only
            if p not in (poly[0], poly[-1]):
                return False
        for s in others:
            if _Grid._cell_clear_dist(s, p) < halo - 1e-9:
                return False
    return True


def _couple_complement(poly_p: list[tuple[float, float]], air_n: dict,
                       offset: float) -> tuple[list[tuple[float, float]] | None, int]:
    """Build the complement polyline by offsetting the primary's DOMINANT straight run sideways by
    ``offset`` toward the N pads, then stubbing from the real N pads to the run ends.

    Returns ``(polyline, side)`` where ``side`` is +1/-1 (which side of the run the complement sits
    on, the LEFT-normal sense used by :func:`_offset_polyline`), or ``(None, 0)`` if the primary has
    no usable straight run. Offsetting only the longest segment (not every jog) avoids the corner
    artefacts a full-polyline offset produces on the little pad-approach jogs."""
    # longest segment of the primary = the coupled run
    best_i, best_len = -1, 0.0
    for i in range(len(poly_p) - 1):
        d = drc._dist(poly_p[i], poly_p[i + 1])
        if d > best_len:
            best_len, best_i = d, i
    if best_i < 0 or best_len <= 1e-6:
        return None, 0
    a, b = poly_p[best_i], poly_p[best_i + 1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(dx, dy)
    lnx, lny = -dy / ln, dx / ln  # left normal (the +1 side, matching _offset_polyline)
    np1 = (float(air_n["p1"][0]), float(air_n["p1"][1]))
    np2 = (float(air_n["p2"][0]), float(air_n["p2"][1]))
    # choose the side (sign) that puts the offset run on the same side as the N pads
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    npmid = ((np1[0] + np2[0]) / 2.0, (np1[1] + np2[1]) / 2.0)
    dot = (npmid[0] - mid[0]) * lnx + (npmid[1] - mid[1]) * lny
    side = 1 if dot >= 0 else -1
    oa = (a[0] + side * offset * lnx, a[1] + side * offset * lny)
    ob = (b[0] + side * offset * lnx, b[1] + side * offset * lny)
    # order the run ends so the N pads connect to the nearer one
    if (drc._dist(np1, oa) + drc._dist(np2, ob)
            <= drc._dist(np1, ob) + drc._dist(np2, oa)):
        run = [np1, oa, ob, np2]
    else:
        run = [np1, ob, oa, np2]
    # drop duplicate consecutive points
    poly: list[tuple[float, float]] = [run[0]]
    for p in run[1:]:
        if drc._dist(p, poly[-1]) > 1e-9:
            poly.append(p)
    if len(poly) < 2:
        return None, 0
    return poly, side


def route_diff_pair(board: dict, net_p: str, net_n: str, opts: dict | None = None) -> dict:
    """Route a single differential pair coupled + length-matched. PURE: ``board`` is not mutated.

    Returns ``{"tracks": [...], "vias": [...], "report": {...}}`` where ``report`` holds the per-pair
    stats dict (``nets``, ``length_p``, ``length_n``, ``skew_mm``, ``coupled_pct``, ``routed``).
    """
    opts = opts or {}
    out = {"tracks": [], "vias": [], "report": {}}
    if not schema.is_board(board):
        out["report"] = {"nets": [net_p, net_n], "routed": False,
                         "length_p": 0.0, "length_n": 0.0, "skew_mm": 0.0, "coupled_pct": 0.0}
        return out

    pitch = float(opts.get("pitch", DEFAULT_PITCH))
    max_cells = int(opts.get("max_grid_cells", MAX_GRID_CELLS))
    minx, miny, maxx, maxy = _board_bbox(board)
    w, hgt = maxx - minx, maxy - miny
    while pitch > 0 and ((int(w / pitch) + 1) * (int(hgt / pitch) + 1)) > max_cells:
        pitch *= 1.5
    grid = _Grid(board, pitch)
    via_cost = float(opts.get("via_cost", VIA_COST))
    bend_cost = float(opts.get("bend_cost", BEND_COST))
    tcnt = _IdCounter(board, "t", "tracks")
    vcnt = _IdCounter(board, "v", "vias")
    reg = _RoutedRegistry()
    stats = _route_diff_pair_into(grid, board, net_p, net_n, reg,
                                  tcnt, vcnt, via_cost, bend_cost, opts)
    out["tracks"] = reg.all_tracks()
    out["vias"] = reg.all_vias()
    out["report"] = stats
    return out


def _route_diff_pair_into(grid: _Grid, board: dict, net_p: str, net_n: str,
                          reg: _RoutedRegistry, tcnt: _IdCounter, vcnt: _IdCounter,
                          via_cost: float, bend_cost: float, opts: dict) -> dict:
    """Route the pair and register both members; return the per-pair stats dict.

    Steps: (1) route primary with A*; (2) try the parallel-offset complement, else independent A*;
    (3) length-match by meandering the shorter member."""
    stats = {"nets": [net_p, net_n], "routed": False,
             "length_p": 0.0, "length_n": 0.0, "skew_mm": 0.0, "coupled_pct": 0.0}

    # which single airwire connects each member? (diff pairs are point-to-point; take the net's airwire)
    rats = drc.ratsnest(board)
    air_p = next((a for a in rats if a["net"] == net_p), None)
    air_n = next((a for a in rats if a["net"] == net_n), None)
    if air_p is None:
        return stats  # nothing to route on the primary

    gap = _diff_gap(board, net_p, opts.get("diff_gap"))
    wp = _track_width(board, net_p)
    wn = _track_width(board, net_n)
    # centreline-to-centreline offset that yields the target EDGE gap
    offset = gap + wp / 2.0 + wn / 2.0
    length_tol = float(opts.get("length_tol", DEFAULT_LENGTH_TOL))

    # (1) primary
    gp = _route_airwire(grid, board, air_p, reg, tcnt, vcnt, via_cost, bend_cost)
    if gp is None:
        return stats
    reg.add(net_p, gp[0], gp[1])

    poly_p = _polyline_of(net_p, gp[0])
    # pick the dominant layer of the primary for the complement
    layer = gp[0][0]["layer"] if gp[0] else grid.layers[0]

    coupled = False
    coupled_side = 0  # which side the complement sits on relative to the primary (+1/-1)
    gn = None
    if poly_p and air_n is not None and not gp[1]:  # only couple a single-layer (no-via) primary
        cand, coupled_side = _couple_complement(poly_p, air_n, offset)
        if cand is not None and _poly_clear_of_others(grid, board, cand, layer, net_n, reg, net_p):
            tn = _polyline_to_tracks(cand, net_n, wn, layer, tcnt)
            reg.add(net_n, tn, [])
            gn = (tn, [])
            coupled = True

    if gn is None and air_n is not None:
        # fall back: route the complement independently (it must still avoid the primary's halo)
        gn = _route_airwire(grid, board, air_n, reg, tcnt, vcnt, via_cost, bend_cost)
        if gn is not None:
            reg.add(net_n, gn[0], gn[1])

    if gn is None:
        # primary routed, complement failed
        stats["length_p"] = round(_polyline_length(poly_p), 4)
        return stats

    # (3) length match -- meander the shorter member, folding AWAY from the partner so the accordion
    # never eats into the intra-pair gap. The complement sits on ``coupled_side`` relative to the
    # primary, so the primary folds to ``-coupled_side`` and the complement folds to ``coupled_side``.
    poly_p = _polyline_of(net_p, reg.tracks.get(net_p, []))
    poly_n = _polyline_of(net_n, reg.tracks.get(net_n, []))
    len_p = _polyline_length(poly_p)
    len_n = _polyline_length(poly_n)
    skew = abs(len_p - len_n)
    if skew > length_tol:
        if len_p < len_n:
            short_net, short_poly, short_w, short_partner, away = (
                net_p, poly_p, wp, net_n, -coupled_side if coupled_side else 0)
        else:
            short_net, short_poly, short_w, short_partner, away = (
                net_n, poly_n, wn, net_p, coupled_side if coupled_side else 0)
        new_poly = _meander_away(grid, board, short_poly, skew, gap, short_w, layer,
                                 short_net, short_partner, reg, away)
        if new_poly is not None:
            reg.tracks[short_net] = _polyline_to_tracks(new_poly, short_net, short_w, layer, tcnt)
            if short_net == net_p:
                poly_p = _polyline_of(net_p, reg.tracks[net_p]); len_p = _polyline_length(poly_p)
            else:
                poly_n = _polyline_of(net_n, reg.tracks[net_n]); len_n = _polyline_length(poly_n)
        skew = abs(len_p - len_n)

    stats["routed"] = True
    stats["length_p"] = round(len_p, 4)
    stats["length_n"] = round(len_n, 4)
    stats["skew_mm"] = round(skew, 4)
    stats["coupled_pct"] = round(_coupled_pct(poly_p, poly_n, offset), 1) if coupled else 0.0
    return stats


def _meander_away(grid: _Grid, board: dict, poly: list[tuple[float, float]], extra: float,
                  gap: float, width: float, layer: str, net: Any, partner: Any,
                  reg: _RoutedRegistry, away: int) -> list[tuple[float, float]] | None:
    """Meander ``poly`` to add ``extra`` mm, folding to the ``away`` side (away from the partner) when
    known (+1/-1), else trying both. Validates the result stays clear of OTHER nets + inside the board
    AND does not cross into the partner; returns the new polyline or None if no side works."""
    sides = [away] if away in (1, -1) else [1, -1]
    for s in sides:
        cand = _meander(poly, extra, gap, width, side=s)
        if cand is None:
            continue
        # must clear other nets / board edge, and not bury itself in the partner's copper
        if not _poly_clear_of_others(grid, board, cand, layer, net, reg, partner):
            continue
        if _crosses_partner(cand, reg.tracks.get(partner, [])):
            continue
        return cand
    return None


def _crosses_partner(poly: list[tuple[float, float]], partner_tracks: list[dict]) -> bool:
    """True if any segment of ``poly`` actually crosses a partner track (teeth bending the wrong way).
    A coupled run merely runs alongside the partner (no crossing); a tooth folded the wrong way would
    intersect it."""
    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        for t in partner_tracks:
            if t.get("kind", "segment") != "segment":
                continue
            c = (float(t["p1"][0]), float(t["p1"][1]))
            d = (float(t["p2"][0]), float(t["p2"][1]))
            if drc.seg_seg_dist(a, b, c, d) <= 1e-9:
                return True
    return False


def _meander(poly: list[tuple[float, float]], extra: float, gap: float,
             width: float, side: int = 1) -> list[tuple[float, float]] | None:
    """Insert a rectangular accordion into ``poly``'s longest straight segment to add ``extra`` mm.

    A basic, honest accordion: on the chosen segment we replace a central span with ``teeth``
    rectangular teeth of amplitude ``amp`` and tooth pitch ``tp = 2*amp``, all folded to one ``side``
    (+1/-1) of the segment. Each tooth detours sideways and back, adding exactly ``2*amp`` of length
    over the straight it spans, so the total added length is ``teeth * 2 * amp``. We pick the largest
    tooth count whose amplitude stays >= a minimum, then SOLVE ``amp = extra/(2*teeth)`` so the added
    length matches ``extra`` to float precision (tight skew control).

    Limits: only meanders a single straight segment (the longest), needs that segment long enough to
    host the teeth at the minimum amplitude. Returns the new polyline, or None when no segment can
    host a clean meander for the required length."""
    if extra <= 0 or len(poly) < 2:
        return list(poly)
    # find the longest segment to host the accordion
    best_i, best_len = -1, 0.0
    for i in range(len(poly) - 1):
        d = drc._dist(poly[i], poly[i + 1])
        if d > best_len:
            best_len, best_i = d, i
    # the tooth tip steps AWAY from the partner, so the intra-pair gap does not bound the amplitude;
    # we only need a minimum so teeth are physically distinguishable (>= the track width, min 0.2mm).
    min_amp = max(width, 0.2)
    if best_i < 0 or best_len < 4.0 * min_amp:
        return None                                # nothing long enough to fold cleanly
    a = poly[best_i]
    b = poly[best_i + 1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg_len = math.hypot(dx, dy)
    ux, uy = dx / seg_len, dy / seg_len            # along-segment unit
    sgn = 1.0 if side >= 0 else -1.0
    nx, ny = -uy * sgn, ux * sgn                   # sideways unit (folded to the chosen side)
    usable = seg_len * 0.9                          # leave 10% of the segment as straight lead-in/out
    # added length = teeth * 2 * amp == extra exactly when amp = extra/(2*teeth). Use the LARGEST
    # tooth count whose amp stays >= min_amp (teeth = floor(extra/(2*min_amp))) -> amp >= min_amp and
    # the added length matches extra to float precision. More teeth = smaller, denser accordion.
    teeth = max(1, int(math.floor(extra / (2.0 * min_amp))))
    teeth = min(teeth, MAX_MEANDER_TEETH)
    amp = extra / (2.0 * teeth)
    tp = 2.0 * amp
    span = teeth * tp                               # == extra
    if span > usable:
        # too much length for this segment at a safe amplitude -> cannot host it cleanly
        return None
    start_off = (seg_len - span) / 2.0
    # build the meandered REPLACEMENT for segment a->b as a ONE-SIDED square wave (a "trombone"): the
    # path alternates a top run (at +amp) and a baseline run (at 0) joined by vertical legs, all on the
    # chosen side. Each tooth = one top excursion: baseline corner -> up -> along top -> down. The two
    # vertical legs add 2*amp of length over the straight span they replace, so the total added length
    # is teeth*2*amp == extra. No segment is retraced (the up and down legs are at different x), so the
    # resulting polyline is a clean open chain. Staying on one side keeps it clear of the partner.
    mid: list[tuple[float, float]] = [a]

    def at(off, lift):
        return (a[0] + ux * off + nx * lift, a[1] + uy * off + ny * lift)

    pos = start_off
    mid.append(at(pos, 0.0))                       # enter the meander on the baseline
    half = tp / 2.0                                # top run and baseline run each span half a pitch
    for _k in range(teeth):
        mid.append(at(pos, amp))                   # up
        mid.append(at(pos + half, amp))            # along the top
        mid.append(at(pos + half, 0.0))            # down
        mid.append(at(pos + tp, 0.0))              # along the baseline to the next tooth
        pos += tp
    mid.append(b)
    # splice the meandered segment back into the FULL polyline (preserve all other vertices, esp. the
    # pad-snapped endpoints poly[0]/poly[-1]).
    return list(poly[:best_i]) + mid + list(poly[best_i + 2:])


def _coupled_pct(poly_p: list[tuple[float, float]], poly_n: list[tuple[float, float]],
                 offset: float) -> float:
    """Fraction (%) of the primary's length whose nearest point on the complement sits within ~the
    target offset (i.e. the two run parallel and adjacent). A coarse coupling-quality metric."""
    if len(poly_p) < 2 or len(poly_n) < 2:
        return 0.0
    tol = offset * 0.6 + 1e-6
    total = 0.0
    coupled = 0.0
    for i in range(len(poly_p) - 1):
        a, b = poly_p[i], poly_p[i + 1]
        seg = drc._dist(a, b)
        if seg <= 1e-9:
            continue
        total += seg
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        # nearest distance from this segment's midpoint to any complement segment
        dmin = math.inf
        for j in range(len(poly_n) - 1):
            dmin = min(dmin, drc._point_seg_dist(mid, poly_n[j], poly_n[j + 1]))
        if abs(dmin - offset) <= tol:
            coupled += seg
    return 100.0 * coupled / total if total > 0 else 0.0


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
