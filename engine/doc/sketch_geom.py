"""Sketch trim / extend / offset geometry (C29-A).

Pure 2D math for the three most-used daily sketch tools. No document/op imports — callers in
``ops.py`` hand in plain entity dicts (the worker-authoritative sketch entity shape: ``line``
{p1,p2}, ``circle`` {center,radius}, ``arc`` {center,radius,start,end,ccw} with start/end in
DEGREES, matching sketch_solver.py) and get back plain entity dicts (no ids — the op layer
assigns them).

Only line / circle / arc are supported; anything else raises :class:`UnsupportedEntity` so the op
layer can surface a clean ``unsupported_entity`` error.

Conventions
-----------
* Angles for arcs are degrees, CCW from +x, exactly like the solver's ``arc`` parameterization.
* An arc spans from ``start`` to ``end`` going CCW when ``ccw`` is true, else CW. The contained
  angular sweep is what trimming/extension reason over.
* TOL = 1e-7 sketch units for all coincidence / on-curve / parameter-range filtering.
"""

from __future__ import annotations

import math
from typing import Any

TOL = 1e-7
_DEG = math.pi / 180.0
_RAD = 180.0 / math.pi


class UnsupportedEntity(Exception):
    """Raised when a path entity is not a line/circle/arc."""

    def __init__(self, etype: str) -> None:
        super().__init__(f"unsupported entity type '{etype}'")
        self.etype = etype


# --------------------------------------------------------------------------- vector helpers


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _scale(a, s):
    return (a[0] * s, a[1] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _len(a):
    return math.hypot(a[0], a[1])


def _norm(a):
    L = _len(a)
    if L < TOL:
        return (0.0, 0.0)
    return (a[0] / L, a[1] / L)


def _perp(a):
    """90deg CCW perpendicular."""
    return (-a[1], a[0])


def _near(a, b, tol=TOL):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


# --------------------------------------------------------------------------- entity accessors


def _require_supported(ent: dict) -> str:
    et = ent.get("type")
    if et not in ("line", "circle", "arc"):
        raise UnsupportedEntity(str(et))
    return et


def _angle_of(center, pt) -> float:
    """Degrees, CCW from +x, in [0, 360)."""
    a = math.degrees(math.atan2(pt[1] - center[1], pt[0] - center[0]))
    return a % 360.0


def _pt_on_circle(center, r, ang_deg):
    a = ang_deg * _DEG
    return (center[0] + r * math.cos(a), center[1] + r * math.sin(a))


def arc_endpoints(arc: dict):
    """Return (start_xy, end_xy) of an arc."""
    c = tuple(arc["center"])
    r = arc["radius"]
    return _pt_on_circle(c, r, arc["start"]), _pt_on_circle(c, r, arc["end"])


def _arc_sweep_contains(arc: dict, ang_deg: float) -> bool:
    """Is angle (deg) within the arc's drawn sweep (start->end, direction = ccw)?"""
    a0 = arc["start"] % 360.0
    a1 = arc["end"] % 360.0
    ccw = bool(arc.get("ccw", True))
    a = ang_deg % 360.0
    if ccw:
        span = (a1 - a0) % 360.0
        rel = (a - a0) % 360.0
    else:
        span = (a0 - a1) % 360.0
        rel = (a0 - a) % 360.0
    if span < TOL:
        span = 360.0  # full circle degenerate guard
    return rel <= span + 1e-6


# --------------------------------------------------------------------------- intersections
#
# Each returns a list of (point_xy, t_on_a) where t_on_a is a natural parameter along entity A:
#   line:   t in [0,1] from p1->p2
#   circle: t = angle in degrees [0,360)
#   arc:    t = angle in degrees
# Range filtering for the *target* (A) is applied by callers (split wants in-range hits).


def _inter_line_line(la, lb):
    a1 = tuple(la["p1"]); a2 = tuple(la["p2"])
    b1 = tuple(lb["p1"]); b2 = tuple(lb["p2"])
    d1 = _sub(a2, a1)
    d2 = _sub(b2, b1)
    den = _cross(d1, d2)
    if abs(den) < TOL:
        return []  # parallel / collinear -> no isolated crossing
    diff = _sub(b1, a1)
    t = _cross(diff, d2) / den
    u = _cross(diff, d1) / den
    pt = _add(a1, _scale(d1, t))
    return [(pt, t, u)]


def _circle_params(ent):
    """center, radius for circle or arc."""
    return tuple(ent["center"]), ent["radius"]


def _inter_line_circle(la, cb):
    a1 = tuple(la["p1"]); a2 = tuple(la["p2"])
    c, r = _circle_params(cb)
    d = _sub(a2, a1)
    L2 = _dot(d, d)
    if L2 < TOL * TOL:
        return []
    f = _sub(a1, c)
    a = L2
    b = 2.0 * _dot(f, d)
    cc = _dot(f, f) - r * r
    disc = b * b - 4 * a * cc
    if disc < -TOL:
        return []
    disc = max(disc, 0.0)
    sq = math.sqrt(disc)
    out = []
    for t in ((-b - sq) / (2 * a), (-b + sq) / (2 * a)):
        pt = _add(a1, _scale(d, t))
        ang = _angle_of(c, pt)
        out.append((pt, t, ang))
    # dedupe tangent double-root
    if len(out) == 2 and _near(out[0][0], out[1][0]):
        out = [out[0]]
    return out


def _inter_circle_circle(ca, cb):
    c0, r0 = _circle_params(ca)
    c1, r1 = _circle_params(cb)
    d = _len(_sub(c1, c0))
    if d < TOL:
        return []  # concentric
    if d > r0 + r1 + TOL or d < abs(r0 - r1) - TOL:
        return []
    a = (r0 * r0 - r1 * r1 + d * d) / (2 * d)
    h2 = r0 * r0 - a * a
    h = math.sqrt(max(h2, 0.0))
    ux, uy = _scale(_sub(c1, c0), 1.0 / d)
    mid = _add(c0, _scale((ux, uy), a))
    out = []
    for sgn in (1.0, -1.0):
        pt = (mid[0] + sgn * h * (-uy), mid[1] + sgn * h * ux)
        out.append((pt, _angle_of(c0, pt), _angle_of(c1, pt)))
    if len(out) == 2 and _near(out[0][0], out[1][0]):
        out = [out[0]]
    return out


def intersect(a: dict, b: dict):
    """All intersections of entities a,b. Returns list of (pt, ta, tb) where ta/tb are the
    natural parameter on a / b (line: 0..1 ; circle/arc: angle deg). No range filtering."""
    ta = _require_supported(a)
    tb = _require_supported(b)
    a_line = ta == "line"
    b_line = tb == "line"
    if a_line and b_line:
        return [(pt, t, u) for (pt, t, u) in _inter_line_line(a, b)]
    if a_line and not b_line:
        return [(pt, t, ang) for (pt, t, ang) in _inter_line_circle(a, b)]
    if not a_line and b_line:
        # swap: compute line vs circle, then report (pt, ang_on_a, t_on_b)
        return [(pt, ang, t) for (pt, t, ang) in _inter_line_circle(b, a)]
    # both curved
    return _inter_circle_circle(a, b)


def _param_on_target_in_range(target: dict, t: float) -> bool:
    """Is parameter t within the drawn span of the target entity?"""
    et = target["type"]
    if et == "line":
        return -TOL <= t <= 1.0 + TOL
    if et == "circle":
        return True
    if et == "arc":
        return _arc_sweep_contains(target, t)
    return False


# --------------------------------------------------------------------------- trim (split + drop)


def split_entity(target: dict, others: list[dict], point_xy):
    """Split *target* at every (in-range, interior) crossing with *others*, then drop the span
    nearest *point_xy*. Returns a list of surviving entity dicts (no ids), in original order
    minus the removed span. If there are no interior crossings, returns ``[]`` (delete whole).

    Surviving spans are returned as full entity dicts (same type as target).
    """
    et = _require_supported(target)
    for o in others:
        _require_supported(o)

    # Gather cut parameters in the target's natural coordinate.
    cuts: list[float] = []
    for o in others:
        for (_pt, ta, _tb) in intersect(target, o):
            if not _param_on_target_in_range(o, _tb):
                continue
            if not _param_on_target_in_range(target, ta):
                continue
            cuts.append(ta)

    if et == "line":
        return _split_line(target, cuts, point_xy)
    if et == "circle":
        return _split_circle(target, cuts, point_xy)
    return _split_arc(target, cuts, point_xy)


def _dedupe_sorted(vals, lo, hi):
    out = []
    for v in sorted(vals):
        if v <= lo + TOL or v >= hi - TOL:
            continue  # ignore cuts at the very endpoints
        if out and abs(v - out[-1]) <= 1e-6:
            continue
        out.append(v)
    return out


def _split_line(line, cuts, point_xy):
    p1 = tuple(line["p1"]); p2 = tuple(line["p2"])
    inner = _dedupe_sorted(cuts, 0.0, 1.0)
    if not inner:
        return []  # no interior crossing -> delete whole
    bounds = [0.0] + inner + [1.0]
    d = _sub(p2, p1)
    spans = []
    for i in range(len(bounds) - 1):
        ta, tb = bounds[i], bounds[i + 1]
        a = _add(p1, _scale(d, ta))
        b = _add(p1, _scale(d, tb))
        spans.append({"type": "line", "p1": [a[0], a[1]], "p2": [b[0], b[1]]})
    drop = _nearest_span_line(spans, point_xy)
    return [s for i, s in enumerate(spans) if i != drop]


def _nearest_span_line(spans, point_xy):
    best, bestd = 0, float("inf")
    for i, s in enumerate(spans):
        mid = _scale(_add(tuple(s["p1"]), tuple(s["p2"])), 0.5)
        dd = _len(_sub(mid, tuple(point_xy)))
        if dd < bestd:
            bestd, best = dd, i
    return best


def _split_circle(circle, cuts, point_xy):
    """A circle cut at >=2 points becomes arcs; drop the arc nearest the point."""
    c, r = _circle_params(circle)
    angs = sorted({a % 360.0 for a in cuts})
    # need at least 2 distinct cut angles to form removable arcs
    angs = _dedupe_angles(angs)
    if len(angs) < 2:
        return []  # 0 or 1 crossing -> delete whole circle
    spans = []
    n = len(angs)
    for i in range(n):
        a0 = angs[i]
        a1 = angs[(i + 1) % n]
        spans.append({"type": "arc", "center": [c[0], c[1]], "radius": r,
                      "start": a0, "end": a1, "ccw": True})
    drop = _nearest_span_arc(spans, point_xy)
    return [s for i, s in enumerate(spans) if i != drop]


def _dedupe_angles(angs):
    out = []
    for a in angs:
        if out and abs(a - out[-1]) <= 1e-6:
            continue
        out.append(a)
    if len(out) >= 2 and abs((out[0] + 360.0) - out[-1]) <= 1e-6:
        out = out[:-1]
    return out


def _split_arc(arc, cuts, point_xy):
    """Split an arc's drawn sweep at interior crossings; drop the nearest sub-arc."""
    c, r = _circle_params(arc)
    a0 = arc["start"] % 360.0
    ccw = bool(arc.get("ccw", True))
    # express each cut as a relative sweep offset from a0 in the arc's direction
    rels = []
    full = _arc_full_sweep(arc)
    for a in cuts:
        if ccw:
            rel = (a - a0) % 360.0
        else:
            rel = (a0 - a) % 360.0
        if TOL < rel < full - TOL:
            rels.append(rel)
    rels = _dedupe_sorted(rels, 0.0, full)
    if not rels:
        return []  # no interior crossing -> delete whole arc
    bounds = [0.0] + rels + [full]
    spans = []
    for i in range(len(bounds) - 1):
        r0, r1 = bounds[i], bounds[i + 1]
        if ccw:
            s = (a0 + r0) % 360.0
            e = (a0 + r1) % 360.0
        else:
            s = (a0 - r0) % 360.0
            e = (a0 - r1) % 360.0
        spans.append({"type": "arc", "center": [c[0], c[1]], "radius": r,
                      "start": s, "end": e, "ccw": ccw})
    drop = _nearest_span_arc(spans, point_xy)
    return [s for i, s in enumerate(spans) if i != drop]


def _arc_full_sweep(arc):
    a0 = arc["start"] % 360.0
    a1 = arc["end"] % 360.0
    ccw = bool(arc.get("ccw", True))
    span = (a1 - a0) % 360.0 if ccw else (a0 - a1) % 360.0
    if span < TOL:
        span = 360.0
    return span


def _arc_midpoint(arc):
    c, r = _circle_params(arc)
    a0 = arc["start"] % 360.0
    ccw = bool(arc.get("ccw", True))
    full = _arc_full_sweep(arc)
    mid = (a0 + full / 2.0) if ccw else (a0 - full / 2.0)
    return _pt_on_circle(c, r, mid)


def _nearest_span_arc(spans, point_xy):
    best, bestd = 0, float("inf")
    for i, s in enumerate(spans):
        mid = _arc_midpoint(s)
        dd = _len(_sub(mid, tuple(point_xy)))
        if dd < bestd:
            bestd, best = dd, i
    return best


# --------------------------------------------------------------------------- extend


def extend_entity(target: dict, end: str, others: list[dict]):
    """Extend the ``"start"`` or ``"end"`` of *target* (line or arc) along its natural
    continuation until the FIRST crossing with another entity. Returns a NEW entity dict
    (whole replacement of target) or ``None`` if nothing lies ahead (caller raises
    ``extend_no_target``).
    """
    et = _require_supported(target)
    if et == "circle":
        raise UnsupportedEntity("circle")  # circles have no ends to extend
    if et == "line":
        return _extend_line(target, end, others)
    return _extend_arc(target, end, others)


def _extend_line(line, end, others):
    p1 = tuple(line["p1"]); p2 = tuple(line["p2"])
    if end == "start":
        anchor, moving = p2, p1
        d = _norm(_sub(p1, p2))   # ray going outward from start
    else:
        anchor, moving = p1, p2
        d = _norm(_sub(p2, p1))
    if d == (0.0, 0.0):
        return None
    # find smallest positive distance s along ray moving + s*d that hits another entity
    best_s = None
    best_pt = None
    for o in others:
        _require_supported(o)
        ray = {"type": "line", "p1": [moving[0], moving[1]],
               "p2": [moving[0] + d[0], moving[1] + d[1]]}  # t units = world distance
        for (pt, t, tb) in intersect(ray, o):
            if t <= TOL:
                continue  # behind/at the moving endpoint
            if not _param_on_target_in_range(o, tb):
                continue
            if best_s is None or t < best_s:
                best_s, best_pt = t, pt
    if best_pt is None:
        return None
    new = dict(line)
    if end == "start":
        new["p1"] = [best_pt[0], best_pt[1]]
    else:
        new["p2"] = [best_pt[0], best_pt[1]]
    return new


def _extend_arc(arc, end, others):
    """Continue the arc's circle from the chosen end (in the arc's drawn direction) to the first
    crossing. Moves only that end's angle."""
    c, r = _circle_params(arc)
    ccw = bool(arc.get("ccw", True))
    if end == "start":
        base_ang = arc["start"] % 360.0
        # extending the start grows the sweep backwards (opposite the drawn direction)
        grow_ccw = not ccw
    else:
        base_ang = arc["end"] % 360.0
        grow_ccw = ccw
    circle = {"type": "circle", "center": [c[0], c[1]], "radius": r}
    best_rel = None
    best_ang = None
    for o in others:
        _require_supported(o)
        for (pt, ta, tb) in intersect(circle, o):
            if not _param_on_target_in_range(o, tb):
                continue
            ang = _angle_of(c, pt)
            rel = (ang - base_ang) % 360.0 if grow_ccw else (base_ang - ang) % 360.0
            if rel <= TOL:
                continue
            if best_rel is None or rel < best_rel:
                best_rel, best_ang = rel, ang
    if best_ang is None:
        return None
    new = dict(arc)
    if end == "start":
        new["start"] = best_ang % 360.0
    else:
        new["end"] = best_ang % 360.0
    return new


# --------------------------------------------------------------------------- offset


def offset_chain(entities: list[dict], distance: float, side_xy):
    """Offset a connected chain of lines/arcs by *distance*, on the side of *side_xy*.

    Returns a list of NEW entity dicts (no ids), the chain neighbors trimmed/extended at their
    mutual offset intersections. Closed chains -> closed offset loop. Open chains -> per-segment
    offset joined at intersections, no end caps.
    """
    for e in entities:
        _require_supported(e)
    if not entities:
        return []

    closed = _chain_is_closed(entities)
    # A single side hint can't sit on the consistent offset side of every segment in a non-convex
    # chain (e.g. an L). So pick ONE winding sign from the hint vs. the nearest point on the chain
    # and apply that same left/right offset to every segment (SolidWorks chain-offset behaviour).
    side = _chain_side_sign(entities, side_xy)
    raws = [_offset_one(e, distance, side) for e in entities]

    n = len(raws)
    if n == 1:
        return [raws[0]]

    # Join consecutive offset segments at their intersection (trim/extend both to meet).
    pairs = n if closed else n - 1
    for i in range(pairs):
        a = raws[i]
        b = raws[(i + 1) % n]
        _join_at_intersection(a, b)
    return raws


def _chain_side_sign(entities, side_xy):
    """Pick a single winding sign (+1 = left of travel direction, -1 = right) from the side hint.

    Uses the chain segment whose nearest point is closest to the hint, and tests which side of that
    segment's travel direction the hint falls on. The same sign then offsets every segment so the
    result is a coherent parallel chain (not a self-crossing mix of inward/outward pieces)."""
    s = tuple(side_xy)
    best_d = float("inf")
    best_sign = 1.0
    for e in entities:
        nearest, tangent = _nearest_and_tangent(e, s)
        d = _len(_sub(s, nearest))
        if d < best_d:
            best_d = d
            left = _perp(tangent)  # +90deg CCW of travel = "left"
            best_sign = 1.0 if _dot(_sub(s, nearest), left) >= 0 else -1.0
    return best_sign


def _nearest_and_tangent(e, pt):
    """Closest point on entity *e* to *pt* and the travel-direction tangent there (unit)."""
    if e["type"] == "line":
        p1 = tuple(e["p1"]); p2 = tuple(e["p2"])
        d = _sub(p2, p1)
        L2 = _dot(d, d)
        t = 0.0 if L2 < TOL * TOL else max(0.0, min(1.0, _dot(_sub(pt, p1), d) / L2))
        return _add(p1, _scale(d, t)), _norm(d)
    # circle / arc: nearest point along the radius toward pt; tangent = perp of radial, CCW
    c, r = _circle_params(e)
    radial = _norm(_sub(pt, c))
    if radial == (0.0, 0.0):
        radial = (1.0, 0.0)
    near = _add(c, _scale(radial, r))
    return near, _perp(radial)  # CCW tangent


def _chain_is_closed(entities):
    if len(entities) < 2:
        return False
    first_s, _ = _entity_ends(entities[0])
    _, last_e = _entity_ends(entities[-1])
    return _near(first_s, last_e, 1e-5)


def _entity_ends(e):
    if e["type"] == "line":
        return tuple(e["p1"]), tuple(e["p2"])
    if e["type"] == "arc":
        s, en = arc_endpoints(e)
        return s, en
    c, r = _circle_params(e)
    return c, c


def _offset_one(e, distance, side):
    """Offset a single line/arc by *distance* on the winding *side* (+1 = left of travel, -1 right).

    For a line the offset normal is ``side * perp(dir)`` (perp = +90deg CCW of travel). For an arc the
    same left/right convention maps to grow/shrink radius via the arc's CCW-ness so neighbouring
    line+arc offsets stay on a coherent parallel side."""
    if e["type"] == "line":
        p1 = tuple(e["p1"]); p2 = tuple(e["p2"])
        nrm = _norm(_perp(_sub(p2, p1)))  # left of travel
        off = _scale(nrm, side * distance)
        return {"type": "line", "p1": [p1[0] + off[0], p1[1] + off[1]],
                "p2": [p2[0] + off[0], p2[1] + off[1]]}
    # arc: left-of-travel on a CCW arc points toward the center (radius shrinks); on a CW arc it
    # points away (radius grows). delta_r = -side * ccw_sign * distance.
    c, r = _circle_params(e)
    ccw_sign = 1.0 if bool(e.get("ccw", True)) else -1.0
    new_r = r - side * ccw_sign * distance
    new_r = max(new_r, TOL)
    out = dict(e)
    out["radius"] = new_r
    out["center"] = [c[0], c[1]]
    return out


def _join_at_intersection(a, b):
    """Trim/extend the touching ends of consecutive offset segments a (end) and b (start) so they
    meet at their geometric intersection. Mutates a and b in place. Falls back to the midpoint of
    the gap when the offsets do not intersect (keeps the loop watertight)."""
    inters = intersect(a, b)
    if inters:
        # pick the intersection closest to the original shared joint (a.end / b.start)
        _, a_end = _entity_ends(a)
        b_start, _ = _entity_ends(b)
        joint = _scale(_add(a_end, b_start), 0.5)
        inters.sort(key=lambda it: _len(_sub(it[0], joint)))
        pt = inters[0][0]
    else:
        _, a_end = _entity_ends(a)
        b_start, _ = _entity_ends(b)
        pt = _scale(_add(a_end, b_start), 0.5)
    _set_end(a, "end", pt)
    _set_end(b, "start", pt)


def _set_end(e, which, pt):
    if e["type"] == "line":
        e["p1" if which == "start" else "p2"] = [pt[0], pt[1]]
    elif e["type"] == "arc":
        c, r = _circle_params(e)
        ang = _angle_of(c, pt)
        e["start" if which == "start" else "end"] = ang % 360.0
