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

    # Detect loops
    loops = _detect_loops(all_edges)

    # Build region faces
    region_faces: list[Face] = []
    region_descs: list[dict[str, Any]] = []

    for i, loop_edges in enumerate(loops):
        face = _loop_to_face(loop_edges, plane)
        if face is None:
            continue
        # Order by (cx, cy) on the descriptor for selector stability
        desc = _region_descriptor(face, i, plane=plane)
        region_faces.append(face)
        region_descs.append(desc)

    # Sort regions by centroid for selector stability (selectors.md §4)
    paired = sorted(zip(region_descs, region_faces),
                    key=lambda x: (x[0]["centroid"][0], x[0]["centroid"][1]))
    if paired:
        region_descs, region_faces = zip(*paired)  # type: ignore[assignment]
        region_descs = list(region_descs)
        region_faces = list(region_faces)
    else:
        region_descs = []
        region_faces = []

    # Re-index hint_index after sorting
    for i, desc in enumerate(region_descs):
        desc["hint_index"] = i

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
