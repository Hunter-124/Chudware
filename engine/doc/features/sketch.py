"""Sketch feature compiler — no-solve path (M1).

feature_params.md §Sketch.

Entity stored coordinates are AUTHORITATIVE until M2 (solver not wired here).
The compiler:
1. Resolves refs.plane → build123d Plane (reserved datum, datum feature, or face selector).
2. Builds edges in sketch-plane space from entity coords (lines/arcs/circles/slots/splines).
   Construction entities are noted but not included in region detection.
3. Detects closed loops via a connectivity walk on shared endpoints.
4. Builds region faces (build123d Face objects in 3-D sketch-plane space) ordered by (cx, cy)
   for region-selector stability (selectors.md §4).
5. Exposes the regions as:
   a. ``CompileOutput.bodies`` — a build123d Compound (or Sketch) registered as pseudo-body
      ``sketch:<id>`` for tessellation (C02/AICM).
   b. ``ctx._sketch_regions[feat_id]`` — list of region dicts for resolve_region calls from
      extrude/revolve.

C09 wires face-selector plane resolution here (ctx.datum() only handles datum refs; face
selectors require describe_faces + resolve).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from build123d import (
    Axis,
    Edge,
    Face,
    GeomType,
    Line,
    Plane,
    Pos,
    Vector,
    Wire,
    Compound,
)

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_faces, resolve

# --------------------------------------------------------------------------- plane resolution


def _resolve_plane_ref(plane_ref: Any, ctx: CompileContext) -> Plane:
    """Resolve refs.plane to a build123d Plane.

    Accepts:
    - reserved datum string (e.g. ``"plane.top"``)
    - datum feature dict ``{"kind":"datum","feature":"f2"}``
    - face selector dict ``{"kind":"face",...}`` → centroid + normal → Plane
    """
    if isinstance(plane_ref, str) or (
        isinstance(plane_ref, dict) and plane_ref.get("kind") == "datum"
    ):
        d = ctx.datum(plane_ref)
        if d.kind != "plane":
            raise FeatureError(
                f"sketch refs.plane: datum '{plane_ref}' has kind '{d.kind}', expected 'plane'"
            )
        return d.obj

    if isinstance(plane_ref, dict) and plane_ref.get("kind") == "face":
        # Resolve face selector against the current top body
        bodies = ctx.bodies
        if not bodies:
            raise FeatureError("sketch refs.plane: face selector given but no bodies exist yet")
        body = bodies[-1]

        # Describe faces without owners first, then patch owner fields using stored topo owners.
        # We use index-based matching (info.index is 0-based; topo_owners keys are 1-based).
        infos = describe_faces(body.wrapped)

        from engine.doc.features.extrude import _get_topo_owners
        face_topo_owners = _get_topo_owners(ctx, "face")
        for info in infos:
            owner = face_topo_owners.get(info.index + 1)  # 1-based index
            if owner is not None:
                info.owner = owner

        try:
            bb = body.bounding_box()
            dx = bb.max.X - bb.min.X
            dy = bb.max.Y - bb.min.Y
            dz = bb.max.Z - bb.min.Z
            diag = math.sqrt(dx * dx + dy * dy + dz * dz)
        except Exception:
            diag = 1.0

        rr = resolve(plane_ref, infos, diag)
        resolved_index = rr.index

        # Fallback: if dangling because of margin (not score), and the best candidate has
        # near-zero score (clearly the intended face), accept it if hint_index matches or
        # the score is < 0.01. This handles the geometry ambiguity of thin extrudes where
        # top and bottom faces score close.
        if resolved_index is None and rr.dangling_reason and "margin" in rr.dangling_reason:
            if rr.score < 0.01:
                # Find the best candidate manually (sort by score, then hint_index)
                kind = plane_ref.get("kind")
                s_owner = plane_ref.get("owner_feature")
                candidates = [i for i in infos if i.kind == kind]
                if s_owner:
                    owner_set = [i for i in candidates if i.owner == s_owner]
                    if owner_set:
                        candidates = owner_set
                from engine.doc.selectors import _score_candidate
                scored = sorted(
                    [((_score_candidate(plane_ref, c, diag)), c) for c in candidates],
                    key=lambda t: (t[0], t[1].hint_index)
                )
                if scored and scored[0][0] < 0.01:
                    resolved_index = scored[0][1].index

        if resolved_index is None:
            raise FeatureError(
                f"sketch refs.plane: face selector dangling — {rr.dangling_reason}",
                dangling_refs=["plane[0]: " + (rr.dangling_reason or "dangling")],
            )
        info = infos[resolved_index]
        # Build a Plane from centroid + normal
        origin = Vector(*info.centroid.tolist())
        if info.axis is None:
            raise FeatureError("sketch refs.plane: resolved face has no axis (non-planar?)")
        normal = Vector(*info.axis.tolist())
        # Construct a Plane: origin + z_dir = normal
        plane = Plane(origin=origin, z_dir=normal)
        return plane

    raise FeatureError(
        f"sketch refs.plane: unrecognized ref form {plane_ref!r}"
    )


# --------------------------------------------------------------------------- entity → Edge helpers


def _to_vec2(v: Any, field: str) -> tuple[float, float]:
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        raise FeatureError(f"sketch entity {field}: expected [x,y]")
    return float(v[0]), float(v[1])


def _entity_to_edges(ent: dict) -> list[Any]:
    """Convert a sketch entity to a list of build123d Edges (in XY sketch-space).

    Returns an empty list for unknown/construction entities (caller decides what to do).
    """
    etype = ent.get("type")

    if etype == "line":
        p1 = _to_vec2(ent["p1"], "p1")
        p2 = _to_vec2(ent["p2"], "p2")
        if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) < 1e-9:
            return []
        return [Edge.make_line(Vector(p1[0], p1[1], 0), Vector(p2[0], p2[1], 0))]

    elif etype == "circle":
        cx, cy = _to_vec2(ent["center"], "center")
        r = float(ent["radius"])
        if r <= 0:
            return []
        return [Edge.make_circle(r, Plane(origin=Vector(cx, cy, 0)))]

    elif etype == "arc":
        cx, cy = _to_vec2(ent["center"], "center")
        r = float(ent["radius"])
        start_a = float(ent["start"])
        end_a = float(ent["end"])
        ccw = bool(ent.get("ccw", True))
        if r <= 0:
            return []
        if not ccw:
            start_a, end_a = end_a, start_a
        return [Edge.make_circle(r, Plane(origin=Vector(cx, cy, 0)),
                                 start_angle=start_a, end_angle=end_a)]

    elif etype == "slot":
        p1 = _to_vec2(ent["p1"], "p1")
        p2 = _to_vec2(ent["p2"], "p2")
        w = float(ent["width"])
        # Slot = two half-circles + two lines; build as a wire and extract edges
        try:
            # Direction vector
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            length = math.hypot(dx, dy)
            if length < 1e-9:
                # degenerate slot → circle
                return [Edge.make_circle(w / 2, Plane(origin=Vector(p1[0], p1[1], 0)))]
            # Perpendicular unit
            nx, ny = -dy / length, dx / length
            r = w / 2.0
            # Four corner arcs + lines (rough):
            # We build a slot Wire via ShapeFactory
            from build123d import SlotOverall
            # Use build123d's slot: SlotOverall(length+w, w) then translate/rotate
            import math as _math
            angle_deg = _math.degrees(_math.atan2(dy, dx))
            mid_x = (p1[0] + p2[0]) / 2
            mid_y = (p1[1] + p2[1]) / 2
            slot_shape = SlotOverall(length + w, w)
            # Rotate + translate
            from build123d import Rot, Location
            loc = Location(Vector(mid_x, mid_y, 0)) * Rot(0, 0, angle_deg)
            slot_shape = loc * slot_shape
            return list(slot_shape.edges())
        except Exception:  # noqa: BLE001
            return []

    elif etype == "arc_slot":
        return _arc_slot_edges(ent)

    elif etype == "spline":
        pts = ent.get("points", [])
        if len(pts) < 2:
            return []
        try:
            vecs = [Vector(float(p[0]), float(p[1]), 0) for p in pts]
            return [Edge.make_spline(vecs)]
        except Exception:  # noqa: BLE001
            return []

    elif etype == "ellipse":
        cx, cy = _to_vec2(ent["center"], "center")
        rx = float(ent["rx"])
        ry = float(ent["ry"])
        rot = float(ent.get("rot", 0))
        if rx <= 0 or ry <= 0:
            return []
        try:
            from build123d import Rot
            e = Edge.make_ellipse(rx, ry, Plane(origin=Vector(cx, cy, 0)))
            if abs(rot) > 1e-9:
                e = Rot(0, 0, rot) * e
            return [e]
        except Exception:  # noqa: BLE001
            return []

    elif etype == "ellipse_arc":
        # Partial ellipse: trimmed ellipse via make_ellipse(start_angle,end_angle)
        # in the un-rotated frame, then rotate by rot about the center.
        cx, cy = _to_vec2(ent["center"], "center")
        rx = float(ent["rx"])
        ry = float(ent["ry"])
        rot = float(ent.get("rot", 0))
        a0 = float(ent["a0"])
        a1 = float(ent["a1"])
        if rx <= 0 or ry <= 0 or abs(a1 - a0) < 1e-9:
            return []
        try:
            from build123d import Rot
            e = Edge.make_ellipse(rx, ry, Plane(origin=Vector(cx, cy, 0)),
                                  start_angle=a0, end_angle=a1)
            if abs(rot) > 1e-9:
                e = Rot(0, 0, rot) * e
            return [e]
        except Exception:  # noqa: BLE001
            return []

    elif etype == "parabola":
        # Approximated by a cubic spline through sampled parabola points
        # (build123d has no native parabola edge). Sampling density below is
        # ample for visual fidelity and region closure at sketch scale.
        pts = _sample_parabola(ent)
        if len(pts) < 2:
            return []
        try:
            vecs = [Vector(px, py, 0) for px, py in pts]
            return [Edge.make_spline(vecs)]
        except Exception:  # noqa: BLE001
            return []

    elif etype == "conic":
        # SolidWorks rho conic == rational quadratic Bezier with apex shoulder.
        # Use Edge.make_bezier with control weights (1, w, 1), w = rho/(1-rho).
        # Falls back to a sampled spline if the weighted bezier is rejected.
        try:
            p0 = _to_vec2(ent["p0"], "p0")
            p1 = _to_vec2(ent["p1"], "p1")
            ap = _to_vec2(ent["apex"], "apex")
            rho = float(ent.get("rho", 0.5))
            rho = min(max(rho, 1e-3), 1.0 - 1e-3)
            w = rho / (1.0 - rho)
            ctrl = [Vector(p0[0], p0[1], 0), Vector(ap[0], ap[1], 0),
                    Vector(p1[0], p1[1], 0)]
            try:
                return [Edge.make_bezier(*ctrl, weights=[1.0, w, 1.0])]
            except Exception:  # noqa: BLE001
                pts = _sample_conic(p0, p1, ap, w)
                vecs = [Vector(px, py, 0) for px, py in pts]
                return [Edge.make_spline(vecs)]
        except Exception:  # noqa: BLE001
            return []

    # Unknown or "point" entity (no geometry)
    return []


# --------------------------------------------------------------------------- arc slot


def _arc_slot_edges(ent: dict) -> list[Any]:
    """Build the closed capsule wire of an arc-spine slot (C30-B).

    Spine = circular arc (center, radius r, CCW a0->a1 deg). The capsule is the
    set of points within w/2 of the spine: outer arc (r+w/2), an end-cap
    half-circle at the a1 spine end, the inner arc (r-w/2) reversed, and a
    start-cap half-circle at the a0 spine end. Mirrors `slot` (lines + two cap
    arcs) adapted to a curved spine.
    """
    cx, cy = _to_vec2(ent["center"], "center")
    r = float(ent["radius"])
    a0 = float(ent["a0"])
    a1 = float(ent["a1"])
    w = float(ent["width"])
    hw = w / 2.0
    if r - hw <= 1e-9:
        raise FeatureError("arc slot width exceeds diameter")
    if abs(a1 - a0) < 1e-9 or w <= 0:
        return []
    pl = Plane(origin=Vector(cx, cy, 0))
    a0r = math.radians(a0)
    a1r = math.radians(a1)

    def _cap(a_rad: float, tangent_sign: float):
        # Half-circle (radius hw) at the spine endpoint S = C + r*(cos a, sin a),
        # connecting outer (r+hw) to inner (r-hw) bulging along +/- the spine tangent.
        ca, sa = math.cos(a_rad), math.sin(a_rad)
        sx, sy = cx + r * ca, cy + r * sa
        outer = Vector(cx + (r + hw) * ca, cy + (r + hw) * sa, 0)
        inner = Vector(cx + (r - hw) * ca, cy + (r - hw) * sa, 0)
        # tangent of a CCW arc at angle a is (-sin a, cos a); cap bulges along it.
        tx, ty = -sa * tangent_sign, ca * tangent_sign
        mid = Vector(sx + hw * tx, sy + hw * ty, 0)
        return Edge.make_three_point_arc(outer, mid, inner)

    try:
        outer_arc = Edge.make_circle(r + hw, pl, start_angle=a0, end_angle=a1)
        inner_arc = Edge.make_circle(r - hw, pl, start_angle=a0, end_angle=a1)
        end_cap = _cap(a1r, +1.0)    # bulge forward (CCW travel direction)
        start_cap = _cap(a0r, -1.0)  # bulge backward
        return [outer_arc, end_cap, inner_arc, start_cap]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- conic sampling


def _sample_parabola(ent: dict, n: int = 24) -> list[tuple[float, float]]:
    """Sample a parabola entity into XY points (sketch_solver frame).

    Frame: origin = vertex, axis u = unit(focus - vertex), w = perp(u),
    focal length f = |focus - vertex|; P(t) = vertex + (t^2/(4f))*u + t*w.
    """
    vx, vy = _to_vec2(ent["vertex"], "vertex")
    fx, fy = _to_vec2(ent["focus"], "focus")
    t0 = float(ent["t0"])
    t1 = float(ent["t1"])
    dx, dy = fx - vx, fy - vy
    f = math.hypot(dx, dy)
    if f < 1e-12 or abs(t1 - t0) < 1e-12:
        return []
    ux, uy = dx / f, dy / f
    wx, wy = -uy, ux
    out: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        s = (t * t) / (4.0 * f)
        out.append((vx + s * ux + t * wx, vy + s * uy + t * wy))
    return out


def _sample_conic(p0, p1, ap, w: float, n: int = 24) -> list[tuple[float, float]]:
    """Sample a rational quadratic Bezier (P0, apex@weight w, P1) into XY points."""
    out: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        b0 = (1 - t) ** 2
        b1 = 2 * (1 - t) * t * w
        b2 = t * t
        den = b0 + b1 + b2
        x = (b0 * p0[0] + b1 * ap[0] + b2 * p1[0]) / den
        y = (b0 * p0[1] + b1 * ap[1] + b2 * p1[1]) / den
        out.append((x, y))
    return out


# --------------------------------------------------------------------------- loop detection


_EPS = 1e-6  # endpoint coincidence tolerance (mm)


def _endpoints(edge) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return the (start, end) 2-D endpoints of a sketch-space edge."""
    vs = edge.vertices()
    if len(vs) >= 2:
        s = vs[0]
        e = vs[-1]
        return (s.X, s.Y), (e.X, e.Y)
    # Closed edge (circle) — return same point
    try:
        p = edge.start_point()
        return (p.X, p.Y), (p.X, p.Y)
    except Exception:  # noqa: BLE001
        return (0.0, 0.0), (0.0, 0.0)


def _detect_loops(edges: list) -> list[list]:
    """Walk edge connectivity and return lists of edges forming closed loops.

    Each loop is a list of edges whose endpoints chain back to the start within ``_EPS``.
    Open chains are discarded.
    """
    if not edges:
        return []

    # Build adjacency: for each edge endpoint, which edges connect there?
    # We work with index lists.
    n = len(edges)
    start_pts = []
    end_pts = []
    for e in edges:
        s, en = _endpoints(e)
        start_pts.append(s)
        end_pts.append(en)

    def _near(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS

    def _is_closed_edge(idx: int) -> bool:
        return _near(start_pts[idx], end_pts[idx])

    loops: list[list] = []

    # Circles (closed single edges) → immediate single-edge loops
    used = [False] * n
    for i, e in enumerate(edges):
        if _is_closed_edge(i):
            loops.append([e])
            used[i] = True

    # Walk chains for open edges
    for start_idx in range(n):
        if used[start_idx]:
            continue
        chain: list[int] = [start_idx]
        used[start_idx] = True
        chain_closed = False
        loop_head = start_pts[start_idx]

        current_tip = end_pts[start_idx]
        while True:
            # Find an unused edge whose start or end connects to current_tip
            found = -1
            for j in range(n):
                if used[j]:
                    continue
                if _near(start_pts[j], current_tip):
                    found = j
                    break
                if _near(end_pts[j], current_tip):
                    # Reverse the edge — build123d Edge.reversed()
                    found = j
                    break
            if found == -1:
                break
            chain.append(found)
            used[found] = True
            # Update tip — pick the end that is NOT the junction
            if _near(start_pts[found], current_tip):
                current_tip = end_pts[found]
            else:
                current_tip = start_pts[found]

            if _near(current_tip, loop_head):
                chain_closed = True
                break

        if chain_closed:
            loops.append([edges[i] for i in chain])

    return loops


# --------------------------------------------------------------------------- region faces


def _loop_to_face(loop_edges: list, plane: Plane) -> Face | None:
    """Build a planar Face (in 3-D sketch-plane coords) from a closed loop."""
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace

        # Transform edges from XY sketch-space to 3-D plane space
        lifted = [plane.location * e for e in loop_edges]
        w = Wire(lifted)
        if not w.is_valid:
            return None
        mf = BRepBuilderAPI_MakeFace(w.wrapped, True)
        if not mf.IsDone():
            return None
        return Face(mf.Face())
    except Exception:  # noqa: BLE001
        return None


def _region_descriptor(face: Face, loop_index: int, plane: Plane | None = None) -> dict[str, Any]:
    """Build a region dict for resolve_region consumption.

    Centroid is stored in 2-D sketch-local coordinates (u, v) so that
    resolve_region can match against selector centroids stored the same way.
    If *plane* is provided, the 3-D centroid is projected into sketch-local coords.
    """
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face.wrapped, props)
        area = props.Mass()
        cm = props.CentreOfMass()
        cx3, cy3, cz3 = cm.X(), cm.Y(), cm.Z()
    except Exception:  # noqa: BLE001
        area = 0.0
        cx3, cy3, cz3 = 0.0, 0.0, 0.0

    if plane is not None:
        # Project 3-D centroid back into sketch-local 2-D coords
        # The sketch-plane local X and Y axes
        try:
            origin = plane.origin
            x_axis = plane.x_dir
            y_axis = plane.y_dir
            diff = Vector(cx3 - origin.X, cy3 - origin.Y, cz3 - origin.Z)
            u = diff.dot(x_axis)
            v = diff.dot(y_axis)
            cx, cy = u, v
        except Exception:  # noqa: BLE001
            cx, cy = cx3, cy3
    else:
        cx, cy = cx3, cy3

    return {
        "centroid": [cx, cy],
        "area_or_len": area,
        "hint_index": loop_index,
    }


# --------------------------------------------------------------------------- region nesting (even-odd)


def _loop_polyline(loop_edges: list) -> list[tuple[float, float]]:
    """Sample a closed loop into an ordered 2-D polyline (XY sketch space).

    Builds a build123d Wire (which orders + orients the edges head-to-tail) and samples it at
    a curvature-aware point count. Used for containment tests and the viewport region overlay.
    The polyline is implicitly closed (last point connects back to the first)."""
    try:
        w = Wire(loop_edges)
    except Exception:  # noqa: BLE001
        # Endpoint fallback (degenerate / unorderable): chain raw endpoints.
        pts: list[tuple[float, float]] = []
        for e in loop_edges:
            (sx, sy), _ = _endpoints(e)
            pts.append((sx, sy))
        return pts
    npts = max(48, 16 * len(loop_edges))
    out: list[tuple[float, float]] = []
    for i in range(npts):
        try:
            p = w.position_at(i / npts)
            out.append((float(p.X), float(p.Y)))
        except Exception:  # noqa: BLE001
            continue
    return out


def _poly_area_centroid(pts: list[tuple[float, float]]) -> tuple[float, tuple[float, float]]:
    """Shoelace signed area + centroid of a closed 2-D polyline."""
    n = len(pts)
    if n < 3:
        if not pts:
            return 0.0, (0.0, 0.0)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        return 0.0, (mx, my)
    a = cx = cy = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a *= 0.5
    if abs(a) < 1e-12:
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        return 0.0, (mx, my)
    return a, (cx / (6.0 * a), cy / (6.0 * a))


def _point_in_poly(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon for a closed 2-D polyline."""
    x, y = pt
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def _orient_ccw(loop_edges: list) -> list:
    """Return *loop_edges* wound counter-clockwise (positive signed area in sketch uv).

    The sketch solver may emit a loop in either winding; OCCT's hole logic needs a KNOWN
    relative orientation between outer and inner wires, so we normalise every loop to CCW
    first (then the hole is reversed to CW when added). Without this, a clockwise outer loop
    made the hole reversal a no-op — the circle was added as material instead of subtracted
    (square+circle extruded as a filled square + disc instead of a washer)."""
    area, _ = _poly_area_centroid(_loop_polyline(loop_edges))
    if area >= 0:
        return loop_edges
    from OCP.TopoDS import TopoDS
    return [Edge(TopoDS.Edge_s(e.wrapped.Reversed())) for e in reversed(loop_edges)]


def _region_face(outer_edges: list, hole_edge_lists: list[list], plane: Plane) -> Face | None:
    """Build a planar Face from an outer loop minus inner (hole) loops, lifted to plane space.

    Outer + hole loops are normalised to CCW first, then each hole is added reversed (CW) so the
    relative orientation is always correct regardless of how the solver wound the input loops."""
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
        from OCP.TopoDS import TopoDS

        outer_w = Wire([plane.location * e for e in _orient_ccw(outer_edges)])
        if not outer_w.is_valid:
            return None
        mf = BRepBuilderAPI_MakeFace(outer_w.wrapped, True)
        for hedges in hole_edge_lists:
            hw = Wire([plane.location * e for e in _orient_ccw(hedges)])
            if not hw.is_valid:
                continue
            # Both wires are now CCW — reverse the hole to CW so OCCT subtracts it.
            mf.Add(TopoDS.Wire_s(hw.wrapped.Reversed()))
        if not mf.IsDone():
            return None
        return Face(mf.Face())
    except Exception:  # noqa: BLE001
        return None


def region_triangles_2d(face: Face, plane: Plane) -> list[list[list[float]]]:
    """Tessellate a region face into hole-aware 2-D triangles in sketch-local (u,v) coords.

    The host fills these (lifted via plane_frame) to highlight a hovered/selected region — a
    washer (square-with-hole) fills correctly because OCCT meshes the trimmed face. Returns a
    list of ``[[u,v],[u,v],[u,v]]`` triangles (empty if meshing fails)."""
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopLoc import TopLoc_Location

        BRepMesh_IncrementalMesh(face.wrapped, 0.25, False, 0.5, True).Perform()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face.wrapped, loc)
        if tri is None:
            return []
        trsf = loc.Transformation()
        ident = loc.IsIdentity()
        ox, oy, oz = plane.origin.X, plane.origin.Y, plane.origin.Z
        xd, yd = plane.x_dir, plane.y_dir

        def _uv(i: int) -> list[float]:
            nd = tri.Node(i)
            if not ident:
                nd.Transform(trsf)
            dx, dy, dz = nd.X() - ox, nd.Y() - oy, nd.Z() - oz
            return [dx * xd.X + dy * xd.Y + dz * xd.Z,
                    dx * yd.X + dy * yd.Y + dz * yd.Z]

        nodes = [_uv(i) for i in range(1, tri.NbNodes() + 1)]
        out: list[list[list[float]]] = []
        for ti in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(ti).Get()
            out.append([nodes[a - 1], nodes[b - 1], nodes[c - 1]])
        return out
    except Exception:  # noqa: BLE001
        return []


def compute_regions(all_edges: list, plane: Plane) -> tuple[list[Face], list[dict[str, Any]]]:
    """Detect closed loops, resolve even-odd nesting, and build region faces + descriptors.

    Each closed loop becomes ONE region whose face is that loop minus its DIRECT child loops
    (holes). ``depth`` = number of enclosing loops; ``is_material`` = depth is even. The
    whole-sketch profile (extrude/revolve default) unions only material regions, so a circle
    inside a square extrudes as a washer (SolidWorks even-odd). Every loop — material or void —
    is still an individually selectable region (SolidWorks "Selected Contours").

    Returns ``(faces, descs)`` paired and sorted by centroid for stable indices. Each desc:
    ``{centroid:[u,v], area_or_len, hint_index, index, depth, is_material, outer:[[u,v]...],
    holes:[[[u,v]...]...]}`` (``outer``/``holes`` in sketch-local 2-D for the viewport overlay)."""
    loops = _detect_loops(all_edges)
    if not loops:
        return [], []

    polys = [_loop_polyline(le) for le in loops]
    areas: list[float] = []
    cents: list[tuple[float, float]] = []
    for pl in polys:
        a, c = _poly_area_centroid(pl)
        areas.append(abs(a))
        cents.append(c)

    n = len(loops)
    depth = [0] * n
    parent = [-1] * n
    for i in range(n):
        best = -1
        best_area: float | None = None
        for j in range(n):
            if i == j or areas[j] <= areas[i]:
                continue
            if _point_in_poly(cents[i], polys[j]):
                depth[i] += 1
                if best_area is None or areas[j] < best_area:
                    best_area, best = areas[j], j
        parent[i] = best

    children: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        if parent[i] >= 0:
            children[parent[i]].append(i)

    faces: list[Face] = []
    descs: list[dict[str, Any]] = []
    for i in range(n):
        face = _region_face(loops[i], [loops[c] for c in children[i]], plane)
        if face is None:
            continue
        desc = _region_descriptor(face, i, plane=plane)
        desc["depth"] = depth[i]
        desc["is_material"] = (depth[i] % 2 == 0)
        desc["outer"] = [[float(x), float(y)] for (x, y) in polys[i]]
        desc["holes"] = [[[float(x), float(y)] for (x, y) in polys[c]] for c in children[i]]
        faces.append(face)
        descs.append(desc)

    if faces:
        paired = sorted(zip(descs, faces),
                        key=lambda p: (p[0]["centroid"][0], p[0]["centroid"][1]))
        descs = [d for d, _ in paired]
        faces = [f for _, f in paired]
        for i, d in enumerate(descs):
            d["hint_index"] = i
            d["index"] = i
    return faces, descs


# --------------------------------------------------------------------------- compiler


@register("sketch")
def compile_sketch(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    plane_ref = refs.get("plane")
    if plane_ref is None:
        raise FeatureError("sketch: refs.plane is required")

    plane = _resolve_plane_ref(plane_ref, ctx)

    entities = p.get("entities", [])
    if not isinstance(entities, list):
        raise FeatureError("sketch: params.entities must be a list")

    # Build edges for non-construction entities
    all_edges: list = []
    for ent in entities:
        if ent.get("construction", False):
            continue
        all_edges.extend(_entity_to_edges(ent))

    # Detect loops + build even-odd region faces (outer loops carry their contained loops as
    # holes; every loop is still an individually selectable region). compute_regions handles
    # nesting, hole subtraction, centroid sort and hint_index assignment.
    region_faces, region_descs = compute_regions(all_edges, plane)

    # Store regions persistently via ctx.datums so extrude/revolve can access them
    # across feature boundaries (ctx is re-created per feature but datums is shared).
    # Use a sentinel Datum with kind "_sketch_regions" and a sub-dict payload as obj.
    from .base import Datum
    _SKETCH_KEY = "_sketch_regions"
    if _SKETCH_KEY not in ctx.datums:
        # Install a container Datum on first sketch
        ctx.datums[_SKETCH_KEY] = Datum(kind="_sketch_regions", obj={}, feature_id="__sys__")
    ctx.datums[_SKETCH_KEY].obj[fid] = {
        "faces": region_faces,
        "descs": region_descs,
        "plane": plane,
        "all_edges": all_edges,
    }

    # Pseudo-body: compound of region faces for tessellation (C02/AICM).
    # If no regions, emit an empty compound (sketch plane info preserved).
    bodies = list(ctx.bodies)

    if region_faces:
        try:
            pseudo_body = Compound.make_compound(region_faces)
        except Exception:  # noqa: BLE001
            pseudo_body = region_faces[0] if region_faces else None
    else:
        pseudo_body = None

    # Sketches DON'T modify the running solid body list — they add a pseudo-body
    # that is separate. For simplicity in M1 we keep bodies unchanged and just
    # record the pseudo-body as tool_body so it's accessible.
    # (C11 / AICM path uses CompileResult.tool_bodies[fid] to find it.)
    return CompileOutput(
        bodies=bodies,
        tool_body=pseudo_body,
        resolved={"_region_count": float(len(region_faces))},
    )
