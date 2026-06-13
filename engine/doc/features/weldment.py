"""Weldment member feature compiler — sweep a structural profile along sketch segments.

feature_params.md §Weldments > weld_member.

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

from build123d import Edge, Face, Plane, Solid, Vector, Wire
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
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
