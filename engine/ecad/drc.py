"""Design Rule Check (DRC) + ratsnest — advisory geometry audit over a board document.

Executable form of ``docs/contracts/ecad/pcb_schema.md`` §6 (the DRC rule set) and the ratsnest
definition. ``run_drc(board)`` returns a deterministic list of :data:`Violation` dicts; it never
raises on a "bad" board and never mutates its input. DRC is **advisory** — it surfaces issues in
the RPC response / AI feedback block but never blocks an op.

A :data:`Violation` follows the PCB contract shape (distinct from ERC's pin-centric shape)::

    {"rule": str, "severity": "error"|"warning",
     "locs": [[x, y, layer], ...],     # world locations involved (mm + layer id)
     "items": [id, ...],               # object ids involved (pad/track/via/footprint)
     "message": str}

**Net-awareness is the headline guarantee**: two pieces of copper on the *same* net never raise a
clearance violation, no matter how close they are (that is the whole point of routing a net). The
clearance check only fires between items of *different* nets. ``null``/empty nets are treated as
distinct from every other net (an unassigned copper island must still clear its neighbours).

Determinism mirrors :mod:`engine.ecad.netlist` / :mod:`engine.ecad.erc`: every iteration is sorted
and the final violation list is sorted, so the output is byte-stable for a given board.

Geometry approximations (documented, "reasonable" per the task):
  * **Track**: a thick segment — distance to another track is the segment-to-segment minimum
    distance minus *half* of each track's width (so the check is edge-to-edge copper spacing).
  * **Round / circle / oval pad, via**: a circle. For a non-round pad the radius is *half the
    larger* size dimension (a conservative bounding circle); a via is a circle of half its
    ``diameter``. Distance to a segment/circle is centre-distance minus the involved radii.
  * **Rect / roundrect / trapezoid / custom pad**: an axis-aligned bounding box of its ``size``
    half-extents at the pad's world centre. Pad rotation/footprint-angle is folded into the box by
    taking the larger half-extent on each axis after rotation (an AABB over the rotated rectangle),
    which is a safe over-approximation of the copper extent. Segment-to-box and box-to-box use
    point/box closest-feature distance.

These are intentionally simple closed forms — exact polygon offsetting is a fabrication-grade
concern left to the board compiler; DRC here is the fast advisory pass.
"""

from __future__ import annotations

import math
from typing import Any

from engine.ecad import schema

Point = tuple[float, float]

# Numerical slack so "exactly at the limit" geometry doesn't flap on float noise. A violation fires
# only when the measured value is short of the rule by more than this (mm). 1e-6 mm = 1 nm: far
# below any meaningful PCB feature, but enough to absorb rotate/translate rounding.
EPS = 1e-9
_TOUCH = 1e-9  # endpoints/pads within this distance count as physically connected (ratsnest)


# ── Violation constructor ────────────────────────────────────────────────────────

def _violation(rule: str, severity: str, locs: list, items: list, message: str) -> dict:
    return {
        "rule": rule,
        "severity": severity,
        "locs": [[float(x), float(y), str(layer)] for (x, y, layer) in locs],
        "items": sorted(str(i) for i in items),
        "message": message,
    }


# ── segment / geometry utilities ─────────────────────────────────────────────────

def _clamp(t: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if t < lo else hi if t > hi else t


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_seg_dist(p: Point, a: Point, b: Point) -> float:
    """Shortest distance from point ``p`` to segment ``a``-``b``."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= EPS:  # degenerate segment == a point
        return _dist(p, a)
    t = _clamp(((p[0] - ax) * dx + (p[1] - ay) * dy) / denom)
    proj = (ax + t * dx, ay + t * dy)
    return _dist(p, proj)


def _segs_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True if closed segments ab and cd cross or touch (proper or improper)."""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_box(p, q, r):  # r within the bounding box of p-q (collinear case)
        return (min(p[0], q[0]) - EPS <= r[0] <= max(p[0], q[0]) + EPS and
                min(p[1], q[1]) - EPS <= r[1] <= max(p[1], q[1]) + EPS)

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if ((o1 > EPS) != (o2 > EPS) or (o1 < -EPS) != (o2 < -EPS)) and \
       ((o3 > EPS) != (o4 > EPS) or (o3 < -EPS) != (o4 < -EPS)):
        # robust sign-difference test
        if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)):
            return True
    # collinear / touching endpoint cases
    if abs(o1) <= EPS and on_box(a, b, c):
        return True
    if abs(o2) <= EPS and on_box(a, b, d):
        return True
    if abs(o3) <= EPS and on_box(c, d, a):
        return True
    if abs(o4) <= EPS and on_box(c, d, b):
        return True
    return False


def seg_seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    """Minimum distance between segments ``a``-``b`` and ``c``-``d`` (0 if they intersect).

    Robust: handles degenerate (point) segments and crossing segments. The min of the four
    point-to-segment distances is the answer whenever the segments do not cross.
    """
    if _segs_intersect(a, b, c, d):
        return 0.0
    return min(
        _point_seg_dist(a, c, d),
        _point_seg_dist(b, c, d),
        _point_seg_dist(c, a, b),
        _point_seg_dist(d, a, b),
    )


def _point_box_dist(p: Point, cx: float, cy: float, hx: float, hy: float) -> float:
    """Distance from point ``p`` to an axis-aligned box (centre ``c``, half-extents ``hx,hy``).

    0 if the point is inside the box.
    """
    dx = max(abs(p[0] - cx) - hx, 0.0)
    dy = max(abs(p[1] - cy) - hy, 0.0)
    return math.hypot(dx, dy)


def _seg_box_dist(a: Point, b: Point, cx: float, cy: float, hx: float, hy: float) -> float:
    """Minimum distance from segment ``a``-``b`` to an axis-aligned box. 0 if they overlap."""
    # box corners + edges
    x0, x1 = cx - hx, cx + hx
    y0, y1 = cy - hy, cy + hy
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    # if either endpoint is inside the box, distance is 0
    if (x0 - EPS <= a[0] <= x1 + EPS and y0 - EPS <= a[1] <= y1 + EPS) or \
       (x0 - EPS <= b[0] <= x1 + EPS and y0 - EPS <= b[1] <= y1 + EPS):
        return 0.0
    # if the segment crosses any box edge -> 0
    edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
    for (c, d) in edges:
        if _segs_intersect(a, b, c, d):
            return 0.0
    # otherwise min over: each box corner to segment, and each box edge to each endpoint
    best = min(_point_seg_dist(corner, a, b) for corner in corners)
    for (c, d) in edges:
        best = min(best, _point_seg_dist(a, c, d), _point_seg_dist(b, c, d))
    return best


def _box_box_dist(c1: Point, h1: Point, c2: Point, h2: Point) -> float:
    """Distance between two axis-aligned boxes (centre, half-extents). 0 if they overlap."""
    dx = max(abs(c1[0] - c2[0]) - (h1[0] + h2[0]), 0.0)
    dy = max(abs(c1[1] - c2[1]) - (h1[1] + h2[1]), 0.0)
    return math.hypot(dx, dy)


# ── copper item abstraction ───────────────────────────────────────────────────────
#
# Every clearance-bearing copper object is normalized to a "shape": either a thick segment (track),
# a circle (round pad / via), or a box (rect-ish pad). Each carries its net + layer set + the source
# object id so violations can name it. The world geometry lives in the shape; the half-width / radius
# is the copper margin to subtract when measuring edge-to-edge spacing.


class _Shape:
    """A normalized copper item for clearance math."""

    __slots__ = ("kind", "net", "layers", "item_id", "pts", "margin",
                 "center", "half", "loc")

    def __init__(self, kind, net, layers, item_id, *, pts=None, margin=0.0,
                 center=None, half=None, loc=None):
        self.kind = kind            # "seg" | "circle" | "box"
        self.net = net              # net name or None
        self.layers = set(layers)   # copper layers occupied
        self.item_id = item_id
        self.pts = pts              # (p1, p2) for seg / circle uses center
        self.margin = margin        # half-width (seg) / radius (circle); box uses half
        self.center = center        # (x, y) for circle / box
        self.half = half            # (hx, hy) for box
        self.loc = loc              # representative (x, y) for the violation locs


def _shared_layer(a: _Shape, b: _Shape) -> bool:
    return bool(a.layers & b.layers)


def edge_distance(a: _Shape, b: _Shape) -> float:
    """Edge-to-edge copper distance between two normalized shapes (negative clamped to 0)."""
    # center-to-center / segment metric, minus the copper margins of each shape.
    if a.kind == "seg" and b.kind == "seg":
        d = seg_seg_dist(a.pts[0], a.pts[1], b.pts[0], b.pts[1])
        return max(d - a.margin - b.margin, 0.0)
    if a.kind == "seg" and b.kind == "circle":
        d = _point_seg_dist(b.center, a.pts[0], a.pts[1])
        return max(d - a.margin - b.margin, 0.0)
    if a.kind == "circle" and b.kind == "seg":
        return edge_distance(b, a)
    if a.kind == "circle" and b.kind == "circle":
        return max(_dist(a.center, b.center) - a.margin - b.margin, 0.0)
    if a.kind == "seg" and b.kind == "box":
        d = _seg_box_dist(a.pts[0], a.pts[1], b.center[0], b.center[1], b.half[0], b.half[1])
        return max(d - a.margin, 0.0)
    if a.kind == "box" and b.kind == "seg":
        return edge_distance(b, a)
    if a.kind == "circle" and b.kind == "box":
        d = _point_box_dist(a.center, b.center[0], b.center[1], b.half[0], b.half[1])
        return max(d - a.margin, 0.0)
    if a.kind == "box" and b.kind == "circle":
        return edge_distance(b, a)
    # box / box
    return _box_box_dist(a.center, a.half, b.center, b.half)


# ── building shapes from a board ───────────────────────────────────────────────────

def _rotated_aabb_half(w: float, h: float, angle_deg: float) -> Point:
    """Half-extents of the axis-aligned bounding box of a w×h rect rotated by ``angle_deg``."""
    r = math.radians(angle_deg)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    hw, hh = w / 2.0, h / 2.0
    return (hw * c + hh * s, hw * s + hh * c)


def _pad_layers(pad: dict, n_cu: int) -> set[str]:
    """Copper layers a pad sits on. tht/np_tht/connector span all copper; smd uses its layer list."""
    cu = set(schema.copper_layer_ids(n_cu))
    ptype = pad.get("type", "smd")
    if ptype in ("tht", "np_tht", "connector"):
        return set(cu)
    return {lyr for lyr in pad.get("layers", []) if lyr in cu} or set()


def _pad_shape(fp: dict, pad: dict, n_cu: int) -> _Shape | None:
    """Normalize a footprint pad into a clearance :class:`_Shape` at its world position."""
    layers = _pad_layers(pad, n_cu)
    if not layers:
        return None
    wx, wy = schema.pad_world(fp, pad)
    size = pad.get("size", [0.0, 0.0]) or [0.0, 0.0]
    w = float(size[0]) if len(size) > 0 else 0.0
    h = float(size[1]) if len(size) > 1 else w
    net = pad.get("net")
    pid = pad.get("id", "")
    shape = pad.get("shape", "rect")
    if shape in ("circle", "oval"):
        # circle of half the larger dimension (conservative for an oval)
        radius = max(w, h) / 2.0
        return _Shape("circle", net, layers, pid, center=(wx, wy), margin=radius, loc=(wx, wy))
    # rect / roundrect / trapezoid / custom -> bounding box, folding pad+footprint rotation in.
    pad_angle = float(pad.get("angle", 0.0)) + float(fp.get("angle", 0.0))
    hx, hy = _rotated_aabb_half(w, h, pad_angle)
    return _Shape("box", net, layers, pid, center=(wx, wy), half=(hx, hy), loc=(wx, wy))


def _track_shape(t: dict) -> _Shape | None:
    if t.get("kind", "segment") != "segment":
        return None  # arc tracks: out of scope for v1 clearance (documented)
    p1 = tuple(float(c) for c in t.get("p1", [0, 0]))
    p2 = tuple(float(c) for c in t.get("p2", [0, 0]))
    width = float(t.get("width", 0.0))
    layer = t.get("layer")
    mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    return _Shape("seg", t.get("net"), {layer}, t.get("id", ""),
                  pts=(p1, p2), margin=width / 2.0, loc=mid)


def _via_shape(v: dict) -> _Shape:
    at = tuple(float(c) for c in v.get("at", [0, 0]))
    radius = float(v.get("diameter", 0.0)) / 2.0
    layers = set(v.get("layers", [])) or {"F.Cu", "B.Cu"}
    return _Shape("circle", v.get("net"), layers, v.get("id", ""),
                  center=at, margin=radius, loc=at)


def _copper_shapes(board: dict) -> list[_Shape]:
    """All clearance-bearing copper shapes on the board (pads, tracks, vias), deterministic order."""
    n_cu = int(board.get("stackup", {}).get("copper_layers", 2))
    shapes: list[_Shape] = []
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            sh = _pad_shape(fp, pad, n_cu)
            if sh is not None:
                shapes.append(sh)
    for t in board.get("tracks", []):
        sh = _track_shape(t)
        if sh is not None:
            shapes.append(sh)
    for v in board.get("vias", []):
        shapes.append(_via_shape(v))
    shapes.sort(key=lambda s: s.item_id)
    return shapes


# ── net helpers ─────────────────────────────────────────────────────────────────

def same_net(a: Any, b: Any) -> bool:
    """True iff ``a`` and ``b`` are the same, non-empty net.

    A ``None``/empty net is NEVER the same as anything (including another empty net): an unassigned
    copper island still has to clear its neighbours. This is the net-aware core of the clearance
    rule — same-net copper is allowed to touch.
    """
    if a is None or b is None:
        return False
    if a == "" or b == "":
        return False
    return a == b


def _net_class_of(board: dict, net: Any) -> str | None:
    for n in board.get("nets", []):
        if isinstance(n, dict) and n.get("name") == net:
            return n.get("class")
    return None


def _clearance_for(board: dict, a: _Shape, b: _Shape) -> float:
    """Required clearance between two shapes: net-class value when both share a class, else global."""
    rules = board.get("rules", {})
    g = float(rules.get("clearance", 0.0))
    classes = board.get("net_classes", {})
    ca = _net_class_of(board, a.net)
    cb = _net_class_of(board, b.net)
    if ca is not None and ca == cb and ca in classes:
        nc = classes[ca]
        if "clearance" in nc:
            return max(float(nc["clearance"]), 0.0)
    return g


# ── drilled-hole extraction (for hole_to_hole + annular_ring + via_size) ───────────

def _drilled_holes(board: dict) -> list[dict]:
    """All drilled holes: ``{id, at, drill, kind}`` for vias and tht/np_tht pads. Deterministic."""
    holes: list[dict] = []
    for v in board.get("vias", []):
        d = float(v.get("drill", 0.0))
        if d > 0:
            holes.append({"id": v.get("id", ""), "at": tuple(float(c) for c in v.get("at", [0, 0])),
                          "drill": d, "kind": "via", "layer": "F.Cu"})
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            drill = pad.get("drill")
            if not drill:
                continue
            dia = drill.get("diameter") if isinstance(drill, dict) else drill
            if dia is None:
                continue
            wx, wy = schema.pad_world(fp, pad)
            holes.append({"id": pad.get("id", ""), "at": (wx, wy), "drill": float(dia),
                          "kind": "pad", "layer": "F.Cu"})
    holes.sort(key=lambda h: h["id"])
    return holes


# ── outline / edge sampling ─────────────────────────────────────────────────────

def _outline_segments(board: dict) -> list[tuple[Point, Point]]:
    """Approximate the Edge.Cuts outline as line segments (arcs/circles polygonized)."""
    segs: list[tuple[Point, Point]] = []
    for shp in board.get("outline", {}).get("shapes", []):
        typ = shp.get("type")
        if typ == "line":
            p1 = tuple(float(c) for c in shp.get("p1", [0, 0]))
            p2 = tuple(float(c) for c in shp.get("p2", [0, 0]))
            segs.append((p1, p2))
        elif typ == "arc":
            cx, cy = (float(c) for c in shp.get("center", [0, 0]))
            r = float(shp.get("radius", 0.0))
            a0, a1 = math.radians(float(shp.get("a0", 0))), math.radians(float(shp.get("a1", 0)))
            n = max(8, int(abs(a1 - a0) / (math.pi / 18)))  # ~10deg steps
            prev = None
            for i in range(n + 1):
                a = a0 + (a1 - a0) * i / n
                pt = (cx + r * math.cos(a), cy + r * math.sin(a))
                if prev is not None:
                    segs.append((prev, pt))
                prev = pt
        elif typ == "circle":
            cx, cy = (float(c) for c in shp.get("center", [0, 0]))
            r = float(shp.get("radius", 0.0))
            n = 48
            prev = None
            for i in range(n + 1):
                a = 2 * math.pi * i / n
                pt = (cx + r * math.cos(a), cy + r * math.sin(a))
                if prev is not None:
                    segs.append((prev, pt))
                prev = pt
    return segs


def _shape_edge_dist(s: _Shape, edge: tuple[Point, Point]) -> float:
    """Edge-to-edge distance from a copper shape to one outline segment."""
    a, b = edge
    if s.kind == "seg":
        return max(seg_seg_dist(s.pts[0], s.pts[1], a, b) - s.margin, 0.0)
    if s.kind == "circle":
        return max(_point_seg_dist(s.center, a, b) - s.margin, 0.0)
    # box
    return _seg_box_dist(a, b, s.center[0], s.center[1], s.half[0], s.half[1])


# ── ratsnest ──────────────────────────────────────────────────────────────────────

class _UF:
    """Tiny union-find over arbitrary hashable keys (deterministic-root not required here)."""

    def __init__(self) -> None:
        self._p: dict = {}

    def add(self, k):
        self._p.setdefault(k, k)

    def find(self, k):
        self.add(k)
        root = k
        while self._p[root] != root:
            root = self._p[root]
        while self._p[k] != root:
            self._p[k], k = root, self._p[k]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra


def _net_terminals(board: dict) -> dict[str, list[dict]]:
    """Per-net list of connectable terminals (pads + vias) with world position + id.

    Skips null/empty nets. Each terminal: ``{id, at, layers}``.
    """
    n_cu = int(board.get("stackup", {}).get("copper_layers", 2))
    out: dict[str, list[dict]] = {}
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            net = pad.get("net")
            if not net:
                continue
            wx, wy = schema.pad_world(fp, pad)
            out.setdefault(net, []).append({
                "id": pad.get("id", ""), "at": (wx, wy), "layers": _pad_layers(pad, n_cu)})
    for v in board.get("vias", []):
        net = v.get("net")
        if not net:
            continue
        at = tuple(float(c) for c in v.get("at", [0, 0]))
        out.setdefault(net, []).append({
            "id": v.get("id", ""), "at": at, "layers": set(v.get("layers", []))})
    for net in out:
        out[net].sort(key=lambda r: r["id"])
    return out


def ratsnest(board: dict) -> list[dict]:
    """Compute the ratsnest: the still-needed airwires for each net.

    For each net (skipping null/empty), we union-find the terminals (pads/vias) that are already
    *physically* connected by copper of the same net — a terminal joins a track when the track
    endpoint touches it, and two terminals/tracks join when their endpoints coincide. Then, over the
    resulting connected components, we emit a Euclidean minimum spanning tree of airwires linking the
    components that are still separate. A fully-routed net yields zero airwires.

    Returns a deterministic list of ``{"net", "p1", "p2"}`` (``p1``/``p2`` are ``[x, y]`` mm).
    """
    out: list[dict] = []
    terminals = _net_terminals(board)

    # group same-net track endpoints so terminals/tracks that touch get unioned
    tracks_by_net: dict[str, list[tuple[Point, Point]]] = {}
    for t in board.get("tracks", []):
        if t.get("kind", "segment") != "segment":
            continue
        net = t.get("net")
        if not net:
            continue
        p1 = tuple(float(c) for c in t.get("p1", [0, 0]))
        p2 = tuple(float(c) for c in t.get("p2", [0, 0]))
        tracks_by_net.setdefault(net, []).append((p1, p2))

    for net in sorted(terminals):
        terms = terminals[net]
        if len(terms) < 2:
            continue  # a net with <2 terminals has nothing to connect

        uf = _UF()
        # node keys: terminal ids ("T:<id>") and track endpoints ("P:<x>,<y>")
        for r in terms:
            uf.add(("T", r["id"]))

        def _ep_key(p: Point):
            return ("P", round(p[0] * 1e6) / 1e6 + 0.0, round(p[1] * 1e6) / 1e6 + 0.0)

        segs = tracks_by_net.get(net, [])
        # union the two endpoints of each track segment
        for (p1, p2) in segs:
            uf.union(_ep_key(p1), _ep_key(p2))
        # union a terminal into any track endpoint it touches
        for r in terms:
            for (p1, p2) in segs:
                if _dist(r["at"], p1) <= _TOUCH or _dist(r["at"], p2) <= _TOUCH:
                    uf.union(("T", r["id"]), _ep_key(p1))
                    uf.union(("T", r["id"]), _ep_key(p2))
        # union terminals that coincide (e.g. a via stacked on a pad)
        for i in range(len(terms)):
            for j in range(i + 1, len(terms)):
                if _dist(terms[i]["at"], terms[j]["at"]) <= _TOUCH:
                    uf.union(("T", terms[i]["id"]), ("T", terms[j]["id"]))

        # connected components OF TERMINALS, keyed by their UF root
        comp: dict[Any, list[dict]] = {}
        for r in terms:
            comp.setdefault(uf.find(("T", r["id"])), []).append(r)
        reps = sorted(comp.keys(), key=lambda root: min(t["id"] for t in comp[root]))
        if len(reps) < 2:
            continue  # already one connected component -> fully routed, no airwires

        # MST over component representatives by min Euclidean terminal-pair distance.
        # Distance between two components = nearest terminal pair across them.
        def _comp_dist(ca, cb):
            best = (math.inf, None, None)
            for ta in comp[ca]:
                for tb in comp[cb]:
                    d = _dist(ta["at"], tb["at"])
                    if d < best[0]:
                        best = (d, ta, tb)
            return best

        # Prim's algorithm from the first representative (deterministic order).
        in_tree = {reps[0]}
        remaining = set(reps[1:])
        while remaining:
            best = (math.inf, None, None, None, None)  # (d, from_rep, to_rep, ta, tb)
            for fr in sorted(in_tree, key=lambda r: min(t["id"] for t in comp[r])):
                for to in sorted(remaining, key=lambda r: min(t["id"] for t in comp[r])):
                    d, ta, tb = _comp_dist(fr, to)
                    if d < best[0]:
                        best = (d, fr, to, ta, tb)
            _, fr, to, ta, tb = best
            out.append({
                "net": net,
                "p1": [float(ta["at"][0]), float(ta["at"][1])],
                "p2": [float(tb["at"][0]), float(tb["at"][1])],
            })
            in_tree.add(to)
            remaining.discard(to)

    # deterministic order
    out.sort(key=lambda a: (a["net"], a["p1"], a["p2"]))
    return out


# ── main entry point ───────────────────────────────────────────────────────────────

def run_drc(board: dict) -> list[dict]:
    """Run the DRC rule catalog (``pcb_schema.md`` §6) over a board document.

    Pure + advisory: returns a deterministic, sorted list of violations; never raises on a bad
    board, never mutates the input. Net-aware: same-net copper never raises a clearance violation.
    """
    out: list[dict] = []
    if not schema.is_board(board):
        return out

    rules = board.get("rules", {})

    # ── clearance: copper-copper spacing between DIFFERENT nets, on a shared layer ──
    shapes = _copper_shapes(board)
    for i in range(len(shapes)):
        for k in range(i + 1, len(shapes)):
            a, b = shapes[i], shapes[k]
            if same_net(a.net, b.net):
                continue  # net-aware: same net never clears
            if not _shared_layer(a, b):
                continue  # copper on different layers cannot clash
            req = _clearance_for(board, a, b)
            d = edge_distance(a, b)
            if d < req - EPS:
                layer = sorted(a.layers & b.layers)[0]
                out.append(_violation(
                    "clearance", "error",
                    [(a.loc[0], a.loc[1], layer), (b.loc[0], b.loc[1], layer)],
                    [a.item_id, b.item_id],
                    f"clearance {d:.4g}mm < required {req:.4g}mm between {a.item_id} "
                    f"(net {a.net!r}) and {b.item_id} (net {b.net!r})"))

    # ── track_width: track.width < rules.track_width_min ──
    tw_min = float(rules.get("track_width_min", 0.0))
    for t in board.get("tracks", []):
        w = float(t.get("width", 0.0))
        if w < tw_min - EPS:
            p1 = t.get("p1", [0, 0])
            mid = ((float(p1[0]) + float(t.get("p2", [0, 0])[0])) / 2.0,
                   (float(p1[1]) + float(t.get("p2", [0, 0])[1])) / 2.0)
            out.append(_violation(
                "track_width", "error", [(mid[0], mid[1], t.get("layer", "F.Cu"))],
                [t.get("id", "")],
                f"track {t.get('id', '')} width {w:.4g}mm < minimum {tw_min:.4g}mm"))

    # ── via_size: diameter < via_diameter_min or drill < via_drill_min ──
    dia_min = float(rules.get("via_diameter_min", 0.0))
    drill_min = float(rules.get("via_drill_min", 0.0))
    for v in board.get("vias", []):
        at = v.get("at", [0, 0])
        loc = (float(at[0]), float(at[1]), "F.Cu")
        dia = float(v.get("diameter", 0.0))
        drill = float(v.get("drill", 0.0))
        if dia < dia_min - EPS:
            out.append(_violation(
                "via_size", "error", [loc], [v.get("id", "")],
                f"via {v.get('id', '')} diameter {dia:.4g}mm < minimum {dia_min:.4g}mm"))
        if drill < drill_min - EPS:
            out.append(_violation(
                "via_size", "error", [loc], [v.get("id", "")],
                f"via {v.get('id', '')} drill {drill:.4g}mm < minimum {drill_min:.4g}mm"))

    # ── annular_ring: (diameter - drill)/2 < annular_ring_min (vias + tht pads) ──
    ar_min = float(rules.get("annular_ring_min", 0.0))
    for v in board.get("vias", []):
        at = v.get("at", [0, 0])
        ring = (float(v.get("diameter", 0.0)) - float(v.get("drill", 0.0))) / 2.0
        if ring < ar_min - EPS:
            out.append(_violation(
                "annular_ring", "error", [(float(at[0]), float(at[1]), "F.Cu")],
                [v.get("id", "")],
                f"via {v.get('id', '')} annular ring {ring:.4g}mm < minimum {ar_min:.4g}mm"))
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            drill = pad.get("drill")
            if not drill:
                continue
            dia = drill.get("diameter") if isinstance(drill, dict) else drill
            if dia is None:
                continue
            size = pad.get("size", [0.0, 0.0]) or [0.0, 0.0]
            pad_min_dim = min(float(size[0]), float(size[1])) if len(size) >= 2 else float(size[0])
            ring = (pad_min_dim - float(dia)) / 2.0
            if ring < ar_min - EPS:
                wx, wy = schema.pad_world(fp, pad)
                out.append(_violation(
                    "annular_ring", "error", [(wx, wy, "F.Cu")], [pad.get("id", "")],
                    f"pad {pad.get('id', '')} annular ring {ring:.4g}mm < minimum {ar_min:.4g}mm"))

    # ── hole_to_hole: drilled-hole center spacing < hole_to_hole_min ──
    h2h_min = float(rules.get("hole_to_hole_min", 0.0))
    holes = _drilled_holes(board)
    for i in range(len(holes)):
        for k in range(i + 1, len(holes)):
            ha, hb = holes[i], holes[k]
            # edge-to-edge between hole walls = center distance - both hole radii
            gap = _dist(ha["at"], hb["at"]) - ha["drill"] / 2.0 - hb["drill"] / 2.0
            if gap < h2h_min - EPS:
                out.append(_violation(
                    "hole_to_hole", "error",
                    [(ha["at"][0], ha["at"][1], "F.Cu"), (hb["at"][0], hb["at"][1], "F.Cu")],
                    [ha["id"], hb["id"]],
                    f"hole-to-hole gap {gap:.4g}mm < minimum {h2h_min:.4g}mm between "
                    f"{ha['id']} and {hb['id']}"))

    # ── edge_clearance: copper closer than edge_clearance to the Edge.Cuts outline ──
    edge_min = float(rules.get("edge_clearance", 0.0))
    edges = _outline_segments(board)
    if edges:
        for s in shapes:
            best = min(_shape_edge_dist(s, e) for e in edges)
            if best < edge_min - EPS:
                lyr = sorted(s.layers)[0] if s.layers else "F.Cu"
                out.append(_violation(
                    "edge_clearance", "error", [(s.loc[0], s.loc[1], lyr)], [s.item_id],
                    f"copper {s.item_id} is {best:.4g}mm from the board edge "
                    f"(< {edge_min:.4g}mm)"))

    # ── unrouted: one warning per remaining ratsnest airwire ──
    for air in ratsnest(board):
        out.append(_violation(
            "unrouted", "warning",
            [(air["p1"][0], air["p1"][1], "F.Cu"), (air["p2"][0], air["p2"][1], "F.Cu")],
            [],
            f"net {air['net']!r} has an unrouted airwire "
            f"({air['p1'][0]:.4g},{air['p1'][1]:.4g}) -> "
            f"({air['p2'][0]:.4g},{air['p2'][1]:.4g})"))

    # deterministic final ordering
    out.sort(key=lambda v: (v["rule"], v["severity"], tuple(v["items"]),
                            tuple(tuple(loc) for loc in v["locs"]), v["message"]))
    return out


if __name__ == "__main__":  # pragma: no cover - smoke
    import json
    import sys
    doc = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else schema.new_board()
    print(json.dumps({"violations": run_drc(doc), "ratsnest": ratsnest(doc)},
                     indent=2, sort_keys=True))
