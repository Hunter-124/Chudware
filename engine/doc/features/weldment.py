"""Weldment feature compilers — structural members + dress features (gusset, end cap).

feature_params.md §Weldments > weld_member / weld_gusset / weld_end_cap.

This module hosts three weldment features:
  * ``weld_member`` — sweep a structural profile along sketch segments (the base members).
  * ``weld_gusset`` — a triangular reinforcing plate welded into the corner between two members
    (resolved from two picked planar faces). A gusset is a SEPARATE weldment body.
  * ``weld_end_cap`` — a plate that closes the open end of a hollow member (resolved from one
    picked planar END face). Also a separate body.

Gusset / end-cap ref form
-------------------------
Both dress features resolve from **picked planar faces on the existing member bodies** (the simplest
ref form that stays robust for the common cases). Faces are resolved against ALL current bodies, not
just the last one, because ``weld_member`` keeps every member as a separate body.

A ``weld_member`` feature takes a sketch and a structural PROFILE from the small profile library
(``engine/doc/weld_profiles.py``) and produces one member SOLID per selected sketch segment: the
profile cross-section is oriented perpendicular to the segment at its start and swept along that
single segment (OCCT ``BRepOffsetAPI_MakePipe``). The members of one feature are kept as SEPARATE
bodies (an assembly of members) so each is individually addressable for the future cut list.

Params
------
- ``profile``: a library profile name (``square_tube`` | ``rect_tube`` | ``round_tube`` |
  ``angle_L`` | ``flat_bar`` | ``c_channel``).
- per-profile dimension keys (mm), e.g. ``size``/``thickness`` for ``square_tube`` — any missing
  key falls back to the library default.
- ``angle``: rotation of the profile about the path (deg, default 0).
- ``justify``: ``"center"`` (default) | ``"corner"`` — SolidWorks "Locate Profile" extremes.
- ``trim_corners``: bool (default true) — mitre-trim members that share a sketch vertex.

Refs
----
- ``sketch``: the source sketch feature id (its raw edges become the member paths).
- ``segments``: optional list of entity ids (``e1``…) — default ALL segments of the sketch.

Corner trim (joint type)
------------------------
Where two members meet at a shared sketch vertex we cut BOTH with the **mitre plane** through the
vertex: the plane that bisects the angle between the two segment directions (its normal is the
bisector of the two outgoing tangents). Each member is subtracted by the half-space on the OTHER
member's side, so neither crosses the mitre plane — a clean, mutual mitre with no interference.
This is robust for coplanar straight segments meeting at a vertex (the common weldment-frame case).
Limitations: only straight (line) segments are mitred (arcs are swept but butt at their ends);
a vertex shared by 3+ members trims each adjacent pair independently (the result is well-defined
but not a single fitted gusset); near-collinear members (≈180°) are left untrimmed.

Cut-list data
-------------
The per-member records are stashed in ``ctx.datums["_weld_members"]`` (keyed by feature id) as
``{segment_id, profile, length, start, end, area, volume}`` and the summary (member count, total
length, total volume) is echoed in ``resolved`` for the PM. A later wave reads these for the table.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Edge, Face, GeomType, Plane, Solid, Vector, Wire
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from .dress import _resolve_faces
from .extrude import _to_solid_normalized
from engine.doc.weld_profiles import build_profile_face, profile_names


_MEMBERS_KEY = "_weld_members"


# --------------------------------------------------------------------------- sketch segments


def _sketch_data(ctx: CompileContext, sid: str):
    store = ctx.datums.get("_sketch_regions")
    data = store.obj.get(sid) if store is not None else None
    if data is None:
        raise FeatureError(
            f"weld_member: sketch '{sid}' not compiled (wrong feature order?)",
            dangling_refs=[f"sketch: '{sid}' not found"],
        )
    return data


def _segment_edges(ctx: CompileContext, sid: str, wanted: list[str] | None) -> list[tuple[str, Edge]]:
    """Resolve the (entity_id, world-space Edge) pairs to sweep along.

    The sketch stores ``all_edges`` in sketch-XY space plus ``entities``; we lift each edge into
    world space via the sketch plane location. ``wanted`` (entity ids) filters to a subset; None =
    all segments. Closed entities (circles, full ellipses) are NOT valid member paths and are
    skipped (a member runs along an OPEN segment, not around a closed loop).
    """
    data = _sketch_data(ctx, sid)
    plane: Plane = data["plane"]
    entities = data.get("entities") or []

    # Map each non-construction entity to its edge(s). _entity_to_edges yields edges in the SAME
    # order as all_edges was built, but we re-derive per-entity so we can tag the segment id.
    from .sketch import _entity_to_edges

    out: list[tuple[str, Edge]] = []
    for ent in entities:
        if ent.get("construction", False):
            continue
        eid = ent.get("id", "?")
        if wanted is not None and eid not in wanted:
            continue
        for e in _entity_to_edges(ent):
            # Skip closed (periodic) edges — a circle is not a member path.
            try:
                if e.is_closed:
                    continue
            except Exception:  # noqa: BLE001
                pass
            out.append((eid, plane.location * e))
    return out


def _edge_geom(edge: Edge) -> tuple[Vector, Vector, Vector, Vector, float]:
    """Return (start, end, start_tangent, end_tangent, length) of a world-space edge."""
    p0 = edge.position_at(0.0)
    p1 = edge.position_at(1.0)
    t0 = edge.tangent_at(0.0)
    t1 = edge.tangent_at(1.0)
    length = edge.length
    return (Vector(p0), Vector(p1), Vector(t0), Vector(t1), float(length))


# --------------------------------------------------------------------------- member solid


def _sweep_member(face_local: Face, edge: Edge, start: Vector, tangent: Vector) -> Solid:
    """Orient ``face_local`` (drawn in its own XY plane) perpendicular to ``edge`` at ``start``
    and sweep it along the edge → one member solid."""
    n = tangent
    if n.length < 1e-9:
        raise FeatureError("weld_member: degenerate segment tangent")
    n = n.normalized()
    # Profile plane: origin at the segment start, normal along the path tangent. Placing the local
    # XY section into this plane orients the section perpendicular to the path.
    prof_plane = Plane(origin=start, z_dir=n)
    face = prof_plane.location * face_local
    path = Wire([edge])
    try:
        mk = BRepOffsetAPI_MakePipe(path.wrapped, face.wrapped)
        mk.Build()
        if not mk.IsDone():
            raise FeatureError("weld_member: OCCT MakePipe failed (IsDone=False)")
        return _to_solid_normalized(Solid(mk.Shape()).clean())
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"weld_member: sweep failed: {exc}",
            hint="the profile may be too large for the segment, or the segment is degenerate",
        )


# --------------------------------------------------------------------------- corner trim (mitre)


def _half_space(point: Vector, normal: Vector, reach: float) -> Solid:
    """A large box whose near face passes through ``point`` with outward ``normal`` — the SOLID on
    the +normal side. Subtracting it from a member removes everything past the mitre plane."""
    n = normal.normalized()
    # Build a box of side 2*reach, then position it so one face lies on the plane (point, n) and
    # the body extends to the +n side.
    box = Solid.make_box(2 * reach, 2 * reach, 2 * reach)
    # Centre the box on origin, then shift so its -Z face is at z=0 looking up +Z, finally map
    # +Z -> n and origin -> point.
    box = box.translate(Vector(-reach, -reach, 0.0))  # -Z face on z=0, body in +Z
    cut_plane = Plane(origin=point, z_dir=n)
    return cut_plane.location * box


def _mitre_trim(members: list[dict[str, Any]], reach: float) -> None:
    """Mitre-trim members that share a vertex, mutating each member dict's ``solid`` in place.

    For every pair of straight members sharing an endpoint we compute the mitre plane (bisector of
    the two outgoing tangents at the shared vertex) and subtract from each the half-space on the
    other's side, so neither crosses the plane. Coplanar straight-segment case only.
    """
    _EPS = 1e-6

    def _near(a: Vector, b: Vector) -> bool:
        return (a - b).length < 1e-4

    n = len(members)
    for i in range(n):
        mi = members[i]
        if not mi.get("straight"):
            continue
        for j in range(i + 1, n):
            mj = members[j]
            if not mj.get("straight"):
                continue
            # Find a shared vertex and the OUTGOING tangent of each member from that vertex.
            shared, ti, tj = _shared_vertex_tangents(mi, mj, _near)
            if shared is None:
                continue
            ti = ti.normalized()
            tj = tj.normalized()
            cosang = max(-1.0, min(1.0, ti.dot(tj)))
            ang = math.degrees(math.acos(cosang))
            # Near-collinear (straight-through) or fully overlapping: nothing to mitre.
            if ang > 179.0 or ang < 1.0:
                continue
            # Mitre plane normal = bisector of the two OUTGOING directions. Each member keeps the
            # side toward its own tangent; we remove the side toward the OTHER tangent.
            bis = (ti + tj)
            if bis.length < 1e-9:
                continue
            bis = bis.normalized()
            # For member i, remove material on the +bis side that belongs to j (and vice-versa):
            # the half-space whose outward normal points toward member j's tangent.
            ni = (tj - ti)  # points from i's side toward j's side
            nj = (ti - tj)
            if ni.length > _EPS:
                mi["solid"] = _subtract_halfspace(mi["solid"], shared, ni, reach)
            if nj.length > _EPS:
                mj["solid"] = _subtract_halfspace(mj["solid"], shared, nj, reach)


def _shared_vertex_tangents(mi: dict, mj: dict, near) -> tuple[Vector | None, Vector, Vector]:
    """If members i and j share an endpoint, return (vertex, outgoing_tangent_i, outgoing_tangent_j).

    The outgoing tangent points from the shared vertex INTO the member. Returns (None, ..) when the
    members do not share an endpoint."""
    si, ei = mi["start"], mi["end"]
    sj, ej = mj["start"], mj["end"]
    # i's tangent at start points into i (+t0); at end it points out of i (so -t1 points in).
    candidates = [
        (si, mi["t_start"], sj, mj["t_start"]),
        (si, mi["t_start"], ej, -mj["t_end"]),
        (ei, -mi["t_end"], sj, mj["t_start"]),
        (ei, -mi["t_end"], ej, -mj["t_end"]),
    ]
    for vi, tin_i, vj, tin_j in candidates:
        if near(vi, vj):
            return vi, Vector(tin_i), Vector(tin_j)
    return None, Vector(0, 0, 0), Vector(0, 0, 0)


def _subtract_halfspace(solid: Solid, point: Vector, outward: Vector, reach: float) -> Solid:
    """Subtract from ``solid`` the half-space (point, outward-normal). On any kernel hiccup the
    member is returned untouched (trim is best-effort — never fail the whole feature on a trim)."""
    try:
        hs = _half_space(point, outward, reach)
        result = solid - hs
        result = _to_solid_normalized(result)
        if getattr(result, "volume", 0.0) <= 1e-9:
            return solid  # trim would have erased the member — leave it
        return result
    except Exception:  # noqa: BLE001
        return solid


# --------------------------------------------------------------------------- compiler


@register("weld_member")
def compile_weld_member(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    profile = p.get("profile", "square_tube")
    if profile not in profile_names():
        raise FeatureError(
            f"weld_member: unknown profile '{profile}' (have {profile_names()})",
        )
    justify = p.get("justify", "center")
    if justify not in ("center", "corner"):
        raise FeatureError(f"weld_member: unknown justify '{justify}' (center|corner)")
    angle = ctx.dim(p, "angle", 0.0) if p.get("angle") is not None else 0.0
    trim_corners = bool(p.get("trim_corners", True))

    sid = refs.get("sketch")
    if not isinstance(sid, str) or not sid:
        raise FeatureError("weld_member: refs.sketch is required")
    wanted = refs.get("segments")
    if wanted is not None and not isinstance(wanted, list):
        raise FeatureError("weld_member: refs.segments must be a list of entity ids")

    # Profile dimension keys are read straight from params (defaults fill the rest).
    seg_pairs = _segment_edges(ctx, sid, wanted)
    if not seg_pairs:
        raise FeatureError(
            "weld_member: sketch has no usable segments to sweep along",
            hint="the sketch needs at least one open line/arc segment (closed loops are skipped)",
        )

    # Build the profile face once (local frame); reused per segment.
    try:
        face_local, dims, area = build_profile_face(
            profile, p, justify=justify, angle=angle)
    except KeyError as exc:  # noqa: BLE001
        raise FeatureError(f"weld_member: unknown profile {exc}")
    except ValueError as exc:  # noqa: BLE001
        raise FeatureError(f"weld_member: degenerate profile dimensions — {exc}")

    members: list[dict[str, Any]] = []
    max_len = 1.0
    for eid, edge in seg_pairs:
        start, end, t_start, t_end, length = _edge_geom(edge)
        if length < 1e-6:
            raise FeatureError(
                f"weld_member: segment '{eid}' is degenerate (zero length)")
        max_len = max(max_len, length)
        solid = _sweep_member(face_local, edge, start, t_start)
        # A straight member's start and end tangents are parallel (line); used by the mitre trim.
        straight = (t_start - t_end).length < 1e-6
        members.append({
            "segment_id": eid, "solid": solid, "length": length,
            "start": start, "end": end, "t_start": t_start, "t_end": t_end,
            "straight": straight, "area": area,
        })

    # Mitre-trim shared corners (best-effort; never fails the feature).
    if trim_corners and len(members) > 1:
        try:
            reach = max_len * 4.0
            _mitre_trim(members, reach)
        except Exception:  # noqa: BLE001
            pass  # leave members untrimmed on any trim error

    # Members are SEPARATE bodies (assembly of members) → append each to the rolling body list.
    bodies = list(ctx.bodies)
    member_records: list[dict[str, Any]] = []
    total_len = 0.0
    total_vol = 0.0
    for m in members:
        bodies.append(m["solid"])
        vol = float(getattr(m["solid"], "volume", 0.0) or 0.0)
        total_len += m["length"]
        total_vol += vol
        member_records.append({
            "segment_id": m["segment_id"],
            "profile": profile,
            "length": m["length"],
            "start": [m["start"].X, m["start"].Y, m["start"].Z],
            "end": [m["end"].X, m["end"].Y, m["end"].Z],
            "area": m["area"],
            "volume": vol,
        })

    # Stash the cut-list records on a shared datum keyed by feature id.
    if _MEMBERS_KEY not in ctx.datums:
        ctx.datums[_MEMBERS_KEY] = Datum(kind=_MEMBERS_KEY, obj={}, feature_id="__sys__")
    ctx.datums[_MEMBERS_KEY].obj[fid] = {
        "profile": profile, "dims": dims, "members": member_records,
    }

    resolved: dict[str, float] = {
        "_member_count": float(len(member_records)),
        "_total_length": total_len,
        "_total_volume": total_vol,
        "_section_area": area,
    }
    # tool_body = the first member (so the feature still exposes a representative solid).
    return CompileOutput(bodies=bodies, tool_body=members[0]["solid"], op="add",
                         resolved=resolved)


# =========================================================================== dress: shared helpers


def _resolve_face_any_body(sel: dict, bodies: list, ctx: CompileContext, slot: str) -> Face:
    """Resolve ONE face selector against every current body and return the best match.

    ``weld_member`` keeps each member as a separate body, so a face pick may land on any of them.
    We resolve the selector against each body independently (reusing the dress-up face resolver so
    owner-tagging + hint fallback apply) and keep the candidate with the smallest centroid distance
    to the selector. Raises a contained FeatureError (dangling) when no body yields a match.
    """
    if not isinstance(sel, dict) or sel.get("kind") != "face":
        raise FeatureError(
            f"{slot}: a planar face selector is required",
            dangling_refs=[f"{slot}: not a face selector"],
        )
    want = sel.get("centroid")
    best: Face | None = None
    best_d = float("inf")
    reasons: list[str] = []
    for body in bodies:
        try:
            faces, dangling = _resolve_faces([sel], body, slot, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            reasons.append(str(exc))
            continue
        if dangling:
            reasons.extend(dangling)
        for f in faces:
            try:
                c = f.center()
                if isinstance(want, (list, tuple)) and len(want) == 3:
                    d = ((c.X - want[0]) ** 2 + (c.Y - want[1]) ** 2 + (c.Z - want[2]) ** 2) ** 0.5
                else:
                    d = 0.0
            except Exception:  # noqa: BLE001
                d = 0.0
            if d < best_d:
                best_d = d
                best = f
    if best is None:
        raise FeatureError(
            f"{slot}: face selector did not resolve on any member body",
            dangling_refs=[f"{slot}: {r}" for r in reasons] or [f"{slot}: no candidate"],
        )
    return best


def _require_planar(face: Face, what: str) -> Vector:
    """Return the OUTWARD-ish unit normal of a planar face, or raise on a non-planar pick."""
    try:
        if face.geom_type != GeomType.PLANE:
            raise FeatureError(f"{what}: the picked face is not planar")
    except FeatureError:
        raise
    except Exception:  # noqa: BLE001
        raise FeatureError(f"{what}: could not determine the face's surface type")
    try:
        n = face.normal_at()
    except Exception:  # noqa: BLE001
        raise FeatureError(f"{what}: could not evaluate the face normal")
    n = Vector(n)
    if n.length < 1e-9:
        raise FeatureError(f"{what}: degenerate face normal")
    return n.normalized()


def _longest_edge_dir(face: Face) -> tuple[Vector, float]:
    """Return ``(unit_dir, length)`` of the longest straight chord among the face's edges.

    For a swept-tube side/top face the longest edge runs ALONG the member axis, so this recovers the
    member's direction from the picked face without needing the member record."""
    best = Vector(1, 0, 0)
    best_len = 0.0
    for e in face.edges():
        try:
            vs = e.vertices()
            if len(vs) < 2:
                continue
            d = Vector(vs[-1].X - vs[0].X, vs[-1].Y - vs[0].Y, vs[-1].Z - vs[0].Z)
            ln = d.length
            if ln > best_len:
                best_len = ln
                best = d.normalized()
        except Exception:  # noqa: BLE001
            continue
    return best, best_len


def _make_solid_plate(corners: list[Vector], normal: Vector, thickness: float,
                      offset: float, what: str) -> Solid:
    """Extrude the closed polygon ``corners`` by ``thickness`` along ``normal``.

    The plate is centred on the corners' plane (so half the thickness sits each side), then shifted
    by ``offset`` along ``normal``. Raises a contained FeatureError on a degenerate polygon/extrude.
    """
    if len(corners) < 3:
        raise FeatureError(f"{what}: plate needs at least 3 corners")
    # Drop consecutive duplicates.
    cleaned: list[Vector] = []
    for v in corners:
        if not cleaned or (v - cleaned[-1]).length > 1e-7:
            cleaned.append(v)
    if len(cleaned) >= 2 and (cleaned[0] - cleaned[-1]).length <= 1e-7:
        cleaned.pop()
    if len(cleaned) < 3:
        raise FeatureError(f"{what}: plate outline degenerated to < 3 distinct corners")
    n = normal.normalized()
    base = -thickness / 2.0 + offset
    moved = [v + n * base for v in cleaned]
    try:
        edges = [Edge.make_line(moved[i], moved[(i + 1) % len(moved)]) for i in range(len(moved))]
        face = Face(Wire(edges))
        solid = Solid.extrude(face, n * thickness)
        solid = _to_solid_normalized(solid)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"{what}: plate build failed: {exc}",
            hint="check the picked face(s), thickness and leg sizes",
        )
    if getattr(solid, "volume", 0.0) <= 1e-9:
        raise FeatureError(f"{what}: plate produced an empty/degenerate solid")
    return solid


# =========================================================================== weld_gusset


@register("weld_gusset")
def compile_weld_gusset(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Gusset — a triangular/polygonal reinforcing plate welded into an L-corner of two members.

    Ref form: ``refs.faces`` = exactly two planar face selectors, one on each member at the corner
    (the two coplanar faces facing into the corner — e.g. the two members' top faces). The gusset is
    built IN the plane shared by those faces:

      * the member axes are recovered as each picked face's longest in-plane edge direction;
      * the corner apex = the (near-)intersection of the two member axis lines;
      * each leg direction is oriented from the apex TOWARD its face centre (into the member);
      * the plate is a right triangle (apex, apex+d1·leg1, apex+d2·leg2) — or, for ``polygonal``, a
        cut-corner pentagon (the hypotenuse tip is chamfered back) — extruded by ``thickness`` along
        the shared-plane normal, centred on the plane (shifted by ``location``/``offset``).

    The gusset is a SEPARATE weldment body (it is welded in, not fused into a member).
    """
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    thickness = ctx.dim(p, "thickness", 5.0)
    d1 = ctx.dim(p, "d1", 30.0)
    d2 = ctx.dim(p, "d2", 30.0)
    gtype = p.get("type", "triangular")
    location = p.get("location", "centered")
    offset = ctx.dim(p, "offset", 0.0) if p.get("offset") is not None else 0.0

    if thickness <= 0.0:
        raise FeatureError("weld_gusset: thickness must be > 0")
    if d1 <= 0.0 or d2 <= 0.0:
        raise FeatureError("weld_gusset: legs d1 and d2 must be > 0")
    if gtype not in ("triangular", "polygonal"):
        raise FeatureError("weld_gusset: type must be 'triangular' or 'polygonal'")
    if location not in ("centered", "inner", "outer"):
        raise FeatureError("weld_gusset: location must be 'centered', 'inner' or 'outer'")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("weld_gusset: no member bodies — add weld_member first")

    face_sels = refs.get("faces")
    if not isinstance(face_sels, list) or len(face_sels) != 2:
        raise FeatureError(
            "weld_gusset: refs.faces must be exactly two planar face selectors "
            "(one on each member at the corner)"
        )

    f1 = _resolve_face_any_body(face_sels[0], bodies, ctx, "faces[0]")
    f2 = _resolve_face_any_body(face_sels[1], bodies, ctx, "faces[1]")
    n1 = _require_planar(f1, "weld_gusset")
    n2 = _require_planar(f2, "weld_gusset")

    c1 = Vector(f1.center())
    c2 = Vector(f2.center())
    axis1, len1 = _longest_edge_dir(f1)
    axis2, len2 = _longest_edge_dir(f2)
    if len1 < 1e-6 or len2 < 1e-6:
        raise FeatureError("weld_gusset: could not recover a member axis from a picked face")
    if abs(axis1.dot(axis2)) > 0.999:
        raise FeatureError(
            "weld_gusset: the two picked faces are parallel/collinear members — "
            "pick faces on two members that meet at a corner"
        )

    # Corner apex = closest approach of the two axis lines L1: c1 + s·axis1, L2: c2 + t·axis2.
    apex = _closest_point_between_lines(c1, axis1, c2, axis2)

    # Gusset plane normal: the two faces share a plane in the clean L-corner case (both normals
    # parallel). Use the average of the two face normals; fall back to axis1×axis2.
    if abs(n1.dot(n2)) > 0.5:
        gn = (n1 + (n2 if n1.dot(n2) > 0 else -n2))
        if gn.length < 1e-9:
            gn = axis1.cross(axis2)
    else:
        gn = axis1.cross(axis2)
    if gn.length < 1e-9:
        raise FeatureError("weld_gusset: could not determine the gusset plane normal")
    gn = gn.normalized()

    # Orient each leg from the apex toward its face centre (into the member), projected into the
    # gusset plane so the triangle is planar.
    leg1 = _project_into_plane(_toward(apex, c1, axis1), gn)
    leg2 = _project_into_plane(_toward(apex, c2, axis2), gn)
    if leg1.length < 1e-9 or leg2.length < 1e-9:
        raise FeatureError("weld_gusset: degenerate leg direction at the corner")
    leg1 = leg1.normalized()
    leg2 = leg2.normalized()

    p_a = apex
    p_b = apex + leg1 * d1
    p_c = apex + leg2 * d2
    if gtype == "triangular":
        corners = [p_a, p_b, p_c]
    else:
        # polygonal: clip the outer hypotenuse tip — pull each leg's far point back to 70% so the
        # plate is a cut-corner pentagon (a SolidWorks "polygonal" gusset).
        pb2 = apex + leg1 * (d1 * 0.7)
        pc2 = apex + leg2 * (d2 * 0.7)
        corners = [p_a, p_b, pb2, pc2, p_c]

    # location offset: 'centered' (default) keeps the plate straddling the shared plane; 'inner'
    # sinks it by half thickness to the body side; 'outer' raises it by half thickness.
    loc_off = offset
    if location == "inner":
        loc_off -= thickness / 2.0
    elif location == "outer":
        loc_off += thickness / 2.0

    plate = _make_solid_plate(corners, gn, thickness, loc_off, "weld_gusset")

    out_bodies = list(ctx.bodies)
    out_bodies.append(plate)

    tri_area = 0.5 * abs((p_b - p_a).cross(p_c - p_a).length)
    return CompileOutput(
        bodies=out_bodies, tool_body=plate, op="add",
        resolved={
            "thickness": thickness,
            "d1": d1,
            "d2": d2,
            "_plate_area": _polygon_area(corners, gn),
            "_triangle_area": tri_area,
            "volume": float(getattr(plate, "volume", 0.0) or 0.0),
        },
    )


def _closest_point_between_lines(p1: Vector, d1: Vector, p2: Vector, d2: Vector) -> Vector:
    """Midpoint of the shortest segment between lines (p1+s·d1) and (p2+t·d2).

    For two member axes that genuinely meet at a corner this is the corner vertex; for skew axes it
    is the best-fit corner. Falls back to p1 on a parallel-line singularity."""
    a = d1.dot(d1)
    b = d1.dot(d2)
    c = d2.dot(d2)
    w0 = p1 - p2
    d = d1.dot(w0)
    e = d2.dot(w0)
    denom = a * c - b * b
    if abs(denom) < 1e-12:
        return p1
    s = (b * e - c * d) / denom
    t = (a * e - b * d) / denom
    q1 = p1 + d1 * s
    q2 = p2 + d2 * t
    return (q1 + q2) * 0.5


def _toward(apex: Vector, target: Vector, axis: Vector) -> Vector:
    """Axis flipped so it points from ``apex`` toward ``target`` (into the member)."""
    v = target - apex
    return axis if axis.dot(v) >= 0 else axis * -1.0


def _project_into_plane(v: Vector, normal: Vector) -> Vector:
    """Component of ``v`` lying in the plane with the given unit ``normal``."""
    return v - normal * v.dot(normal)


def _polygon_area(corners: list[Vector], normal: Vector) -> float:
    """Planar polygon area (shoelace in an in-plane basis); best-effort, 0.0 on failure."""
    try:
        n = normal.normalized()
        ref = Vector(0, 1, 0) if abs(n.dot(Vector(1, 0, 0))) > 0.9 else Vector(1, 0, 0)
        u = (ref - n * ref.dot(n)).normalized()
        w = n.cross(u)
        pts = [(c.dot(u), c.dot(w)) for c in corners]
        s = 0.0
        m = len(pts)
        for i in range(m):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % m]
            s += x0 * y1 - x1 * y0
        return abs(s) / 2.0
    except Exception:  # noqa: BLE001
        return 0.0


# =========================================================================== weld_end_cap


@register("weld_end_cap")
def compile_weld_end_cap(feat: dict, ctx: CompileContext) -> CompileOutput:
    """End cap — a plate closing the open END of a hollow member (a tube).

    Ref form: ``refs.face`` = one planar END face of a member (the picked face whose outline is the
    member's cross-section, e.g. the open end of a square/round tube). Two modes:

      * ``outer`` (default) — a plate covering the WHOLE tube end (the face's OUTER boundary), grown
        by ``overhang`` on every side, mounted ON the end face (extruded outward by ``thickness``).
      * ``inner`` — a plate matching the tube's INNER void (the face's inner boundary) that insets
        INTO the tube by ``thickness`` (it plugs the bore, sitting inside the wall).

    ``offset`` shifts the cap along the member axis (clearance / proud mount). The cap is a SEPARATE
    weldment body.
    """
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    thickness = ctx.dim(p, "thickness", 5.0)
    cap_type = p.get("type", "outer")
    offset = ctx.dim(p, "offset", 0.0) if p.get("offset") is not None else 0.0
    overhang = ctx.dim(p, "overhang", 0.0) if p.get("overhang") is not None else 0.0

    if thickness <= 0.0:
        raise FeatureError("weld_end_cap: thickness must be > 0")
    if cap_type not in ("outer", "inner"):
        raise FeatureError("weld_end_cap: type must be 'outer' or 'inner'")
    if overhang < 0.0:
        raise FeatureError("weld_end_cap: overhang must be >= 0")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("weld_end_cap: no member bodies — add weld_member first")

    face_sel = refs.get("face")
    if face_sel is None:
        raise FeatureError("weld_end_cap: refs.face is required (a planar member END face)")
    if isinstance(face_sel, list):
        if len(face_sel) != 1:
            raise FeatureError("weld_end_cap: refs.face takes exactly one face selector")
        face_sel = face_sel[0]

    end_face = _resolve_face_any_body(face_sel, bodies, ctx, "face")
    n = _require_planar(end_face, "weld_end_cap")

    # Cap extrudes ALONG the member axis = the end-face normal. For 'outer' the plate sits on the
    # end and grows outward (away from the body); 'inner' plugs into the bore (toward the body).
    # The face normal points outward from its member body, so:
    #   outer: extrude along +n (away), starting at the face plane + offset.
    #   inner: extrude along -n (into the tube), starting just inside.
    try:
        outer_wire = end_face.outer_wire()
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"weld_end_cap: could not read the end-face outline: {exc}")

    inner_wires = []
    try:
        for w in end_face.wires():
            if not w.is_equal(outer_wire):
                inner_wires.append(w)
    except Exception:  # noqa: BLE001
        inner_wires = []

    if cap_type == "outer":
        # Plate from the OUTER boundary (covers the whole end including the bore), grown by overhang.
        try:
            plate_face = Face(outer_wire)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"weld_end_cap: outer cap face build failed: {exc}")
        if overhang > 1e-9:
            plate_face = _grow_face(plate_face, overhang, n, "weld_end_cap")
        # Mount on the end: start at face plane + offset·n, extrude outward by thickness.
        base_shift = offset
        plate = _extrude_face_plate(plate_face, n, thickness, base_shift, "weld_end_cap")
        cap_area = float(getattr(plate_face, "area", 0.0) or 0.0)
    else:
        # inner: plate matching the bore (inner void). The end face area already EXCLUDES the bore,
        # so we rebuild the plate from the inner ring(s). A solid bar (no inner ring) cannot take an
        # inner cap.
        if not inner_wires:
            raise FeatureError(
                "weld_end_cap: type 'inner' needs a hollow member (no inner boundary on this end "
                "face — it is a solid section). Use type 'outer'."
            )
        try:
            plate_face = Face(inner_wires[0]) if len(inner_wires) == 1 else Face(
                Wire(inner_wires[0].edges()))
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"weld_end_cap: inner cap face build failed: {exc}")
        # Inset into the tube: start a hair inside (−offset along n is "outward"), extrude inward.
        base_shift = -thickness - offset
        plate = _extrude_face_plate(plate_face, n, thickness, base_shift, "weld_end_cap")
        cap_area = float(getattr(plate_face, "area", 0.0) or 0.0)

    out_bodies = list(ctx.bodies)
    out_bodies.append(plate)

    return CompileOutput(
        bodies=out_bodies, tool_body=plate, op="add",
        resolved={
            "thickness": thickness,
            "overhang": overhang,
            "_cap_area": cap_area,
            "volume": float(getattr(plate, "volume", 0.0) or 0.0),
        },
    )


def _extrude_face_plate(face: Face, normal: Vector, thickness: float, base_shift: float,
                        what: str) -> Solid:
    """Translate ``face`` by ``base_shift`` along ``normal`` then extrude it by ``thickness``."""
    n = normal.normalized()
    try:
        moved = face.translate(n * base_shift) if abs(base_shift) > 1e-12 else face
        solid = Solid.extrude(moved, n * thickness)
        solid = _to_solid_normalized(solid)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"{what}: cap extrude failed: {exc}")
    if getattr(solid, "volume", 0.0) <= 1e-9:
        raise FeatureError(f"{what}: cap produced an empty solid")
    return solid


def _grow_face(face: Face, margin: float, normal: Vector, what: str) -> Face:
    """Offset a planar face's outline OUTWARD by ``margin`` (the end-cap overhang).

    Best-effort: a kernel offset failure falls back to the un-grown face rather than failing."""
    try:
        from build123d import offset as b3d_offset
        grown = b3d_offset(face, amount=margin)
        # offset can return a compound; keep the largest face.
        faces = grown.faces() if hasattr(grown, "faces") else [grown]
        faces = [f for f in faces if getattr(f, "area", 0.0) > 0]
        if faces:
            return max(faces, key=lambda f: f.area)
    except Exception:  # noqa: BLE001
        pass
    return face
