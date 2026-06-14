"""Sheet-metal feature compilers — base flange + edge flange.

feature_params.md §Sheet Metal.

This module establishes a part as sheet metal (a uniform-thickness sheet) and adds walls
("flanges") off the sheet's edges with real bend geometry. Bend records (radius / angle /
k-factor / neutral-axis / bend-allowance) are stored on each flange output so a LATER wave can
unfold the part into a flat pattern without re-deriving the bend math.

Bend math (canonical units: mm for lengths, deg for angles)
-----------------------------------------------------------
For one bend of interior angle ``angle`` (the included sweep of the bend arc, = 90° for a
right-angle flange), inner radius ``R`` (mm), material thickness ``T`` (mm) and k-factor ``K``
(0..1, fraction of the thickness at which the neutral fibre sits):

    neutral_radius   Rn = R + K·T
    bend_allowance   BA = angle_rad · Rn = angle_rad · (R + K·T)

BA is the developed (flat) length of material consumed by the bend along the neutral fibre — the
arc length of the neutral axis through the bend region. The SOLID we build is true geometry: a
cylindrical bend region of inner radius ``R`` swept through ``angle``, plus a flat wall of the
requested ``length`` tangent to the bend, both at the sheet ``thickness``. Unfolding later just
replaces each bend region's projected length with its stored ``BA``; for completeness we also
store the *bend deduction* ``BD = 2·(R+T)·tan(angle/2) − BA`` (the amount subtracted from the sum
of the two outside-setback lengths) so either flat-pattern convention is computable.

``length`` is measured the SolidWorks way — the flat wall length is the straight tangent portion
AFTER the bend (it does not include the curved bend region). The total outside dimension of the
finished flange = length + (R+T)·tan(angle/2) contribution from the bend, but we model the wall
and bend as separate true solids, so the geometry is exact regardless of convention.

``position`` (material/bend placement, mirrors SolidWorks "Flange Position"):
- ``material_inside``  — the OUTSIDE of the bent wall is flush with the picked thickness face;
  the bend eats inward so the flange does not grow the outside envelope. (default)
- ``material_outside`` — the INSIDE of the wall is flush with the picked thickness face; the
  flange sits entirely outside the original envelope (the wall starts at the outer edge).
- ``bend_outside``     — the bend's inner radius starts at the thickness face plane; the bend
  bulges outward beyond the original envelope (SolidWorks "Bend Outside").

``flip`` reverses the bend direction (which way the wall folds — up vs down relative to the
parent face normal).
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Edge, Face, Solid, Vector, Wire
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from engine.doc.selectors import describe_edges, describe_faces, resolve
from .dress import _resolve_edges
from .extrude import (
    _body_bbox_diag,
    _resolve_profile_faces,
    _store_topo_owners,
    _to_solid_normalized,
)

# Defaults (feature_params.md §Sheet Metal).
_DEFAULT_THICKNESS = 2.0
_DEFAULT_K = 0.45
_VALID_POSITIONS = ("material_inside", "material_outside", "bend_outside")

# Datum container key on ctx.datums for the part-level sheet-metal context. One entry per body
# (keyed by the body's owning base-flange feature id) so later flanges inherit thickness / k.
_SM_KEY = "_sheet_metal"


# --------------------------------------------------------------------------- bend math


def bend_allowance(angle_deg: float, inner_radius: float, thickness: float,
                   k_factor: float) -> float:
    """Bend Allowance BA = angle_rad · (R + K·T)  — neutral-fibre arc length (mm)."""
    return math.radians(angle_deg) * (inner_radius + k_factor * thickness)


def bend_deduction(angle_deg: float, inner_radius: float, thickness: float,
                   k_factor: float) -> float:
    """Bend Deduction BD = 2·OSSB − BA, OSSB = (R+T)·tan(angle/2) (outside setback)."""
    ba = bend_allowance(angle_deg, inner_radius, thickness, k_factor)
    ossb = (inner_radius + thickness) * math.tan(math.radians(angle_deg) / 2.0)
    return 2.0 * ossb - ba


# --------------------------------------------------------------------------- sheet-metal context


def _set_sheet_context(ctx: CompileContext, fid: str, thickness: float,
                       k_factor: float, default_bend_radius: float) -> None:
    """Record part-level sheet-metal params so later flanges inherit them."""
    if _SM_KEY not in ctx.datums:
        ctx.datums[_SM_KEY] = Datum(kind=_SM_KEY, obj={}, feature_id="__sys__")
    ctx.datums[_SM_KEY].obj[fid] = {
        "thickness": thickness,
        "k_factor": k_factor,
        "default_bend_radius": default_bend_radius,
    }


def _get_sheet_context(ctx: CompileContext) -> dict[str, float] | None:
    """Return the most-recently-stored sheet-metal context (the active sheet's params)."""
    store = ctx.datums.get(_SM_KEY)
    if store is None or not store.obj:
        return None
    last_fid = list(store.obj.keys())[-1]
    return store.obj[last_fid]


# --------------------------------------------------------------------------- base flange


@register("sm_base_flange")
def compile_sm_base_flange(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Base flange — extrude a sketch region by ``thickness`` to make the base sheet.

    This is the FIRST sheet-metal feature; it establishes the part as sheet metal and stores the
    thickness / k-factor so later edge flanges inherit them.
    """
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    thickness = ctx.dim(p, "thickness", _DEFAULT_THICKNESS)
    k_factor = ctx.dim(p, "k_factor", _DEFAULT_K)
    default_bend_radius = ctx.dim(p, "default_bend_radius", thickness)

    if thickness <= 0.0:
        raise FeatureError("sm_base_flange: thickness must be > 0")
    if not (0.0 <= k_factor <= 1.0):
        raise FeatureError("sm_base_flange: k_factor must be in 0..1")

    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("sm_base_flange: refs.profile is required (a closed sketch region)")

    profile_faces: list[Face] = []
    dangling: list[str] = []
    for i, sel in enumerate(profile_sels):
        try:
            profile_faces.extend(_resolve_profile_faces(sel, ctx))
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"profile[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"profile[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"sm_base_flange: {len(dangling)} profile selector(s) failed",
            dangling_refs=dangling,
        )
    if not profile_faces:
        raise FeatureError(
            "sm_base_flange: no profile regions resolved (open or empty sketch?)"
        )

    # Extrude each region face by thickness along its normal, then union.
    tool_bodies: list[Any] = []
    for face in profile_faces:
        try:
            n = face.normal_at()
        except Exception:  # noqa: BLE001
            n = Vector(0, 0, 1)
        try:
            piece = Solid.extrude(face, Vector(n) * thickness)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"sm_base_flange: extrude failed: {exc}",
                hint="the profile must be a closed region in the sketch plane",
            )
        tool_bodies.append(piece)

    sheet = tool_bodies[0]
    for tb in tool_bodies[1:]:
        sheet = sheet + tb
    sheet = _to_solid_normalized(sheet)

    bodies = list(ctx.bodies)
    bodies.append(sheet)
    _store_topo_owners(ctx, fid, sheet, None)
    _set_sheet_context(ctx, fid, thickness, k_factor, default_bend_radius)

    return CompileOutput(
        bodies=bodies, tool_body=sheet, op="add",
        resolved={
            "thickness": thickness,
            "k_factor": k_factor,
            "default_bend_radius": default_bend_radius,
            "volume": float(getattr(sheet, "volume", 0.0) or 0.0),
        },
    )


# --------------------------------------------------------------------------- edge flange geometry


def _edge_dir_and_endpoints(edge: Edge) -> tuple[Vector, Vector, Vector, float]:
    """Return ``(unit_dir, p_start, p_end, length)`` for a STRAIGHT edge.

    Raises FeatureError if the edge is not (effectively) straight."""
    vs = edge.vertices()
    if len(vs) < 2:
        raise FeatureError("sm_edge_flange: bend edge is closed/degenerate, not a straight edge")
    p0 = Vector(vs[0].X, vs[0].Y, vs[0].Z)
    p1 = Vector(vs[-1].X, vs[-1].Y, vs[-1].Z)
    chord = p1 - p0
    length = chord.length
    if length < 1e-9:
        raise FeatureError("sm_edge_flange: bend edge has zero length")
    # Straightness: the edge's arc length must equal the chord length (a line).
    try:
        arclen = edge.length
    except Exception:  # noqa: BLE001
        arclen = length
    if abs(arclen - length) > 1e-4 * max(length, 1.0):
        raise FeatureError(
            "sm_edge_flange: bend edge must be a straight line (a curved edge is not a bend line)"
        )
    return chord * (1.0 / length), p0, p1, length


def _faces_adjacent_to_edge(body, edge: Edge) -> list[Face]:
    """Return the model faces that share *edge* (within tolerance)."""
    target_mid = edge.center()
    target_dir, _, _, _ = _edge_dir_and_endpoints(edge)
    out: list[Face] = []
    for f in body.faces():
        for fe in f.edges():
            try:
                if fe.length <= 1e-9:
                    continue
                # Same midpoint + parallel direction => same edge.
                if (fe.center() - target_mid).length > 1e-5:
                    continue
                fvs = fe.vertices()
                if len(fvs) < 2:
                    continue
                d = (Vector(fvs[-1].X, fvs[-1].Y, fvs[-1].Z)
                     - Vector(fvs[0].X, fvs[0].Y, fvs[0].Z))
                if d.length < 1e-9:
                    continue
                d = d * (1.0 / d.length)
                if abs(abs(d.dot(target_dir)) - 1.0) < 1e-6:
                    out.append(f)
                    break
            except Exception:  # noqa: BLE001
                continue
    return out


def _classify_bend_edge(body, edge: Edge, thickness: float,
                        e_dir: Vector) -> tuple[Face, Face, Vector, Vector]:
    """Identify the bend-edge frame from the two faces meeting at the edge.

    The bend edge of a sheet sits on a "thickness face" (a narrow planar strip whose extent
    perpendicular to the edge ≈ thickness) and a "broad face" (one of the large top/bottom faces).
    Returns ``(broad_face, thickness_face, n_broad_out, n_thick_out)`` where the normals point
    OUTWARD from the solid. Raises FeatureError when the edge is not a valid sheet-metal bend edge.
    """
    faces = _faces_adjacent_to_edge(body, edge)
    planar = [f for f in faces if _is_planar(f)]
    if len(planar) < 2:
        raise FeatureError(
            "sm_edge_flange: bend edge is not shared by two planar faces "
            "(pick a straight edge on a flat sheet face)"
        )

    # The thickness face: a planar face whose dimension perpendicular to the edge ≈ thickness.
    thick_face = None
    broad_face = None
    for f in planar:
        perp = _face_extent_perp_to_edge(f, e_dir)
        if perp is not None and abs(perp - thickness) < 0.05 * thickness + 1e-4:
            thick_face = f
        else:
            broad_face = f
    if thick_face is None:
        raise FeatureError(
            "sm_edge_flange: the picked edge is not on a thickness face of the sheet "
            f"(no adjacent face is ~{thickness:g} mm wide). Pick a bend line on the sheet's "
            "thickness (side) face."
        )
    if broad_face is None:
        # Both adjacent faces were ~thickness wide (a corner sliver); reject cleanly.
        raise FeatureError(
            "sm_edge_flange: could not identify the parent sheet face for this edge"
        )

    n_broad = _outward_normal(broad_face, body)
    n_thick = _outward_normal(thick_face, body)
    return broad_face, thick_face, n_broad, n_thick


def _is_planar(face: Face) -> bool:
    from build123d import GeomType
    try:
        return face.geom_type == GeomType.PLANE
    except Exception:  # noqa: BLE001
        return False


def _face_extent_perp_to_edge(face: Face, e_dir: Vector) -> float | None:
    """Extent of *face* measured perpendicular to *e_dir*, within the face plane.

    For a thickness (side) face of a sheet this returns ≈ thickness."""
    try:
        n = face.normal_at()
    except Exception:  # noqa: BLE001
        return None
    # In-plane direction perpendicular to the edge.
    perp = e_dir.cross(n)
    if perp.length < 1e-9:
        return None
    perp = perp * (1.0 / perp.length)
    vs = face.vertices()
    if not vs:
        return None
    projs = [perp.dot(Vector(v.X, v.Y, v.Z)) for v in vs]
    return max(projs) - min(projs)


def _outward_normal(face: Face, body) -> Vector:
    """Return *face*'s normal flipped to point OUT of *body* (away from the solid centre)."""
    try:
        n = face.normal_at()
    except Exception:  # noqa: BLE001
        n = Vector(0, 0, 1)
    n = n * (1.0 / max(n.length, 1e-12))
    try:
        fc = face.center()
        bb = body.bounding_box()
        bc = Vector((bb.min.X + bb.max.X) / 2.0,
                    (bb.min.Y + bb.max.Y) / 2.0,
                    (bb.min.Z + bb.max.Z) / 2.0)
        # If the normal points back toward the body centre, flip it.
        if n.dot(fc - bc) < 0:
            n = n * -1.0
    except Exception:  # noqa: BLE001
        pass
    return n


def _make_face_from_pts(pts: list[Vector]) -> Face:
    """Build a planar Face from an ordered list of 3-D corner points (a closed polygon)."""
    edges = []
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        edges.append(Edge.make_line(a, b))
    w = Wire(edges)
    mf = BRepBuilderAPI_MakeFace(w.wrapped, True)
    if not mf.IsDone():
        raise FeatureError("sm_edge_flange: failed to build the flange cross-section face")
    return Face(mf.Face())


def _prism(face: Face, vec: Vector) -> Solid:
    """Linear extrude (prism) of *face* along *vec*, via OCP for raw-face robustness."""
    mk = BRepPrimAPI_MakePrism(face.wrapped, gp_Vec(vec.X, vec.Y, vec.Z), False, True)
    if not mk.IsDone():
        raise FeatureError("sm_edge_flange: prism build failed")
    return Solid(mk.Shape()).clean()


def _build_flange_solid(
    p_start: Vector, e_dir: Vector, length_dir: Vector, fold_dir: Vector,
    edge_length: float, thickness: float, inner_radius: float, angle_deg: float,
    wall_length: float, position: str,
) -> Solid:
    """Build the bend region + flat wall solid in true geometry.

    Frame (all unit vectors, mutually orthogonal):
    - ``e_dir``      : along the bend line (prism axis).
    - ``length_dir`` : in the plane of the parent broad face, pointing AWAY from the body across
      the thickness face (the direction the flat sheet would continue). Origin of the bend.
    - ``fold_dir``   : perpendicular to the broad face, the side the wall folds toward (outward
      sheet normal on the fold side). ``+fold_dir`` is "up".

    The cross-section is built in the 2-D frame ``(length_dir, fold_dir)`` at ``p_start`` (a point
    on the bend edge), then prismed along ``e_dir`` by ``edge_length``.

    Geometry (for the canonical 90° case, generalised to ``angle``):
    - The bend arc has inner radius R and outer radius R+T. Its centre C sits at distance R+? from
      the broad face on the fold side. We place the bend so the inner arc is tangent to the parent
      face and the wall is tangent to the arc at the end of the sweep.
    - The flat wall is a rectangle of length ``length`` × thickness ``T`` starting tangent to the
      outer/inner arc and continuing in the rotated direction.
    """
    T = thickness
    R = inner_radius
    ang = math.radians(angle_deg)

    # 2-D helpers in the (length_dir 'x', fold_dir 'y') plane, mapped back to 3-D.
    def xy(x: float, y: float) -> Vector:
        return p_start + length_dir * x + fold_dir * y

    # Position offset along length_dir: where the bend's NEUTRAL start sits relative to the
    # thickness face plane (x = 0 is the picked thickness-face plane / bend edge).
    #   material_inside : the wall's OUTER surface is flush with x=0 -> shift bend inward by (R+T).
    #   material_outside: the wall's INNER surface is flush with x=0 -> shift inward by R.
    #   bend_outside    : the bend's inner arc starts at x=0 -> no inward shift (bulges out).
    if position == "material_inside":
        x0 = -(R + T)
    elif position == "material_outside":
        x0 = -R
    else:  # bend_outside
        x0 = 0.0

    # Bend arc centre: on the fold side, at height (R+? ) — the inner arc has radius R, the outer
    # arc radius R+T. The arc sweeps the material from the flat (broad) plane up by ``angle``.
    # Centre is at x = x0, y = (R+T) so the OUTER arc bottom (y=T) is in the broad-face plane at
    # the flat region and the INNER arc bottom is at y=T as well... we instead place the centre so
    # the bend's inner surface is tangent to the parent face plane (y=0 inner / y=T outer side).
    #
    # Concretely: the sheet's broad faces are at y=0 (top/attach side, where fold_dir points) and
    # the material occupies y in [-T, 0] just inside the body. The bend rotates that T-thick strip
    # about the centre C = (x0, 0) wait — to keep the wall tangent and thickness uniform we set:
    #   inner radius surface passes through (x0, 0); centre C = (x0, R) along +fold? No.
    #
    # Cleanest robust construction: sample the cross-section polygon directly.
    cx = x0
    cy = R  # centre is R above the inner tangent point on the broad-face plane

    # Inner arc: radius R about C, from the start angle (pointing -fold, i.e. down to the broad
    # plane) sweeping by ``ang``. Outer arc: radius R+T about the same C.
    # Start angle: the inner tangent point (x0, 0) is at C + R*(0,-1) -> angle = -90° (pointing
    # down). We sweep CCW by ``ang`` toward +length_dir side.
    a_start = -math.pi / 2.0
    nseg = max(8, int(math.degrees(ang) / 6.0))

    inner_pts: list[tuple[float, float]] = []
    outer_pts: list[tuple[float, float]] = []
    for i in range(nseg + 1):
        a = a_start + ang * (i / nseg)
        inner_pts.append((cx + R * math.cos(a), cy + R * math.sin(a)))
        outer_pts.append((cx + (R + T) * math.cos(a), cy + (R + T) * math.sin(a)))

    # Tangent (wall) direction at the end of the sweep.
    a_end = a_start + ang
    # Radial outward direction at a_end:
    rad = (math.cos(a_end), math.sin(a_end))
    # The flat wall extends from the arc end, tangent: tangent = rotate radial by +90°.
    tang = (-rad[1], rad[0])
    # Wall inner edge end + outer edge end (extend by ``length`` along tangent).
    inner_end = (cx + R * rad[0], cy + R * rad[1])
    outer_end = (cx + (R + T) * rad[0], cy + (R + T) * rad[1])
    wall_inner_far = (inner_end[0] + tang[0] * wall_length, inner_end[1] + tang[1] * wall_length)
    wall_outer_far = (outer_end[0] + tang[0] * wall_length, outer_end[1] + tang[1] * wall_length)

    # Assemble the closed cross-section polygon:
    #   inner arc (start->end) -> wall inner far -> wall outer far -> outer arc (end->start)
    poly2d: list[tuple[float, float]] = []
    poly2d.extend(inner_pts)                      # inner arc, start..end
    poly2d.append(wall_inner_far)                 # up the wall (inner)
    poly2d.append(wall_outer_far)                 # across the wall tip
    poly2d.extend(reversed(outer_pts))            # outer arc, end..start
    # Closing back to outer_pts[0] (the outer inner-tangent point) then up to inner_pts[0].

    pts3d = [xy(x, y) for (x, y) in poly2d]
    # Drop consecutive duplicates that can occur where arc end meets wall.
    cleaned: list[Vector] = []
    for v in pts3d:
        if not cleaned or (v - cleaned[-1]).length > 1e-7:
            cleaned.append(v)
    if (cleaned[0] - cleaned[-1]).length <= 1e-7:
        cleaned.pop()

    section = _make_face_from_pts(cleaned)
    solid = _prism(section, e_dir * edge_length)
    return solid


# --------------------------------------------------------------------------- multi-bend strip
#
# Hem and jog are not single bend+wall walls — a hem folds the material 180° back on itself, a jog
# makes two opposing bends with a short connector. Both are most cleanly built as a CONSTANT-
# THICKNESS strip that walks a centerline of straight runs and circular bends in the 2-D
# (x = length_dir, y = fold_dir) cross-section plane, then offsets ±T/2 to inner/outer boundaries
# and prisms along the bend edge. The walker also yields the per-bend swept angle so the caller can
# emit a flat-pattern bend record (BA = θ·(R+K·T)) for EACH bend, exactly like the edge flange.
#
# Centerline convention (matches _build_flange_solid so records stay consistent): the strip starts
# at the picked thickness-face plane and the FIRST point of the centerline is the mid-thickness
# point of the base material at the bend origin. We track a moving frame (position + heading unit
# vector) and a half-thickness offset, sampling the OUTER (+normal) and INNER (−normal) edges.


def _strip_from_centerline(
    p_start: Vector, e_dir: Vector, length_dir: Vector, fold_dir: Vector,
    edge_length: float, thickness: float, ops: list[dict], start_x: float, start_y: float,
) -> Solid:
    """Build a constant-thickness strip in the (length_dir, fold_dir) plane and prism it along e_dir.

    ``ops`` is a list of moves applied to a moving frame that starts at 2-D point ``(start_x,
    start_y)`` heading +length_dir:
      * ``{"run": L}``                      — straight segment of centerline length ``L``.
      * ``{"bend": angle_deg, "r_center": Rc, "ccw": bool}`` — a circular arc of the centerline of
        centre-radius ``Rc`` sweeping ``angle_deg`` (left turn if ccw).

    The strip is the locus of points within ±T/2 of the centerline along the in-plane normal of the
    heading. Returns the prismed Solid.
    """
    T = thickness
    half = T / 2.0

    def xy(x: float, y: float) -> Vector:
        return p_start + length_dir * x + fold_dir * y

    # Moving frame in 2-D: position (px, py) and heading angle (radians, 0 = +x = +length_dir).
    px, py = start_x, start_y
    heading = 0.0
    # Left-hand normal of the heading (rotate heading by +90°) — used to offset to the OUTER edge.
    # We collect centerline points; inner/outer are derived by offsetting each sample ±half along
    # the local normal. To keep corners clean we sample arcs finely and emit straight-run endpoints.
    inner: list[tuple[float, float]] = []
    outer: list[tuple[float, float]] = []

    def emit(x: float, y: float, h: float) -> None:
        nx, ny = -math.sin(h), math.cos(h)  # left normal of heading
        outer.append((x + nx * half, y + ny * half))
        inner.append((x - nx * half, y - ny * half))

    emit(px, py, heading)
    for op in ops:
        if "run" in op:
            L = float(op["run"])
            px += math.cos(heading) * L
            py += math.sin(heading) * L
            emit(px, py, heading)
        else:
            ang = math.radians(float(op["bend"]))
            Rc = float(op["r_center"])
            ccw = bool(op.get("ccw", True))
            sign = 1.0 if ccw else -1.0
            # Centre of the arc: offset from the current position by Rc along the (left/right) normal.
            cnx, cny = -math.sin(heading), math.cos(heading)  # left normal
            cx = px + sign * cnx * Rc
            cy = py + sign * cny * Rc
            # Current position's polar angle about the centre.
            a0 = math.atan2(py - cy, px - cx)
            nseg = max(8, int(math.degrees(ang) / 6.0))
            for i in range(1, nseg + 1):
                a = a0 + sign * ang * (i / nseg)
                cpx = cx + Rc * math.cos(a)
                cpy = cy + Rc * math.sin(a)
                h = heading + sign * ang * (i / nseg)
                emit(cpx, cpy, h)
            px = cx + Rc * math.cos(a0 + sign * ang)
            py = cy + Rc * math.sin(a0 + sign * ang)
            heading += sign * ang

    # Closed cross-section polygon: outer forward, inner backward.
    poly2d: list[tuple[float, float]] = list(outer) + list(reversed(inner))
    pts3d = [xy(x, y) for (x, y) in poly2d]
    cleaned: list[Vector] = []
    for v in pts3d:
        if not cleaned or (v - cleaned[-1]).length > 1e-7:
            cleaned.append(v)
    if len(cleaned) >= 2 and (cleaned[0] - cleaned[-1]).length <= 1e-7:
        cleaned.pop()
    if len(cleaned) < 3:
        raise FeatureError("sheet metal: degenerate strip cross-section")
    section = _make_face_from_pts(cleaned)
    return _prism(section, e_dir * edge_length)


def _bend_frame(body, edge: Edge, thickness: float, flip: bool
                ) -> tuple[Vector, Vector, float, Vector, Vector, Vector]:
    """Resolve the common bend-edge frame shared by hem / jog (mirrors the edge-flange setup).

    Returns ``(p_start, p_end, edge_length, e_dir, length_dir, fold_dir)``."""
    e_dir, p_start, p_end, edge_length = _edge_dir_and_endpoints(edge)
    broad_face, thick_face, n_broad, n_thick = _classify_bend_edge(
        body, edge, thickness, e_dir
    )
    length_dir = n_thick - n_broad * n_thick.dot(n_broad)
    if length_dir.length < 1e-9:
        length_dir = e_dir.cross(n_broad)
    length_dir = length_dir * (1.0 / length_dir.length)
    fold_dir = n_broad * (-1.0 if flip else 1.0)
    fold_dir = fold_dir * (1.0 / max(fold_dir.length, 1e-12))
    return p_start, p_end, edge_length, e_dir, length_dir, fold_dir


def _resolve_bend_edge(refs: dict, body, ctx: CompileContext, what: str) -> Edge:
    """Resolve the single straight bend edge for a hem / jog (shared with edge flange)."""
    edge_sel = refs.get("edge")
    if edge_sel is None:
        raise FeatureError(f"{what}: refs.edge is required (the bend line)")
    sel_list = edge_sel if isinstance(edge_sel, list) else [edge_sel]
    edges, dangling = _resolve_edges(sel_list, body, "edge", ctx=ctx)
    if dangling:
        raise FeatureError(f"{what}: {len(dangling)} edge selector(s) dangling",
                           dangling_refs=dangling)
    if not edges:
        raise FeatureError(f"{what}: bend edge not resolved")
    return edges[0]


def _append_bend_record(ctx: CompileContext, rec: dict) -> None:
    if _BEND_KEY not in ctx.datums:
        ctx.datums[_BEND_KEY] = Datum(kind=_BEND_KEY, obj=[], feature_id="__sys__")
    ctx.datums[_BEND_KEY].obj.append(rec)


def _make_bend_record(fid: str, angle_deg: float, inner_radius: float, thickness: float,
                      k_factor: float, wall_length: float, position: str, flip: bool,
                      p_start: Vector, p_end: Vector, e_dir: Vector,
                      length_dir: Vector, fold_dir: Vector) -> dict:
    """One per-bend flat-pattern record (same shape the edge flange stores) with BA = θ·(R+K·T)."""
    ba = bend_allowance(angle_deg, inner_radius, thickness, k_factor)
    bd = bend_deduction(angle_deg, inner_radius, thickness, k_factor)
    return {
        "feature": fid,
        "angle": angle_deg,
        "radius": inner_radius,
        "thickness": thickness,
        "k_factor": k_factor,
        "neutral_radius": inner_radius + k_factor * thickness,
        "bend_allowance": ba,
        "bend_deduction": bd,
        "wall_length": wall_length,
        "position": position,
        "flip": flip,
        "bend_line": [[p_start.X, p_start.Y, p_start.Z], [p_end.X, p_end.Y, p_end.Z]],
        "e_dir": [e_dir.X, e_dir.Y, e_dir.Z],
        "length_dir": [length_dir.X, length_dir.Y, length_dir.Z],
        "fold_dir": [fold_dir.X, fold_dir.Y, fold_dir.Z],
    }


# Bend-record container key (shared with the edge flange + read by sheetmetal_flat).
_BEND_KEY = "_sheet_metal_bends"


# --------------------------------------------------------------------------- edge flange


@register("sm_edge_flange")
def compile_sm_edge_flange(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Edge flange — a bent wall off a straight edge of the sheet's thickness face."""
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("sm_edge_flange: no sheet body — add a base flange first")
    body = bodies[-1]

    sheet_ctx = _get_sheet_context(ctx)
    if sheet_ctx is None:
        raise FeatureError(
            "sm_edge_flange: part is not sheet metal — add a base flange first"
        )
    thickness = sheet_ctx["thickness"]
    k_default = sheet_ctx["k_factor"]
    radius_default = sheet_ctx.get("default_bend_radius", thickness)

    angle_deg = ctx.dim(p, "angle", 90.0)
    length = ctx.dim(p, "length", 20.0)
    inner_radius = ctx.dim(p, "radius", radius_default)
    k_factor = ctx.dim(p, "k_factor", k_default)
    position = p.get("position", "material_inside")
    flip = bool(p.get("flip", False))

    if inner_radius <= 0.0:
        raise FeatureError("sm_edge_flange: bend radius must be > 0")
    if length <= 0.0:
        raise FeatureError("sm_edge_flange: flange length must be > 0")
    if abs(angle_deg) < 1e-6 or abs(angle_deg) >= 180.0:
        raise FeatureError("sm_edge_flange: angle must be in (0, 180) degrees")
    if position not in _VALID_POSITIONS:
        raise FeatureError(
            f"sm_edge_flange: position must be one of {_VALID_POSITIONS}"
        )

    # Resolve the bend edge against the running body (reuse the dress-up edge resolver so
    # topology-owner tagging + hint fallback apply identically to fillet/chamfer edge picks).
    edge_sel = refs.get("edge")
    if edge_sel is None:
        raise FeatureError("sm_edge_flange: refs.edge is required (the bend line)")
    sel_list = edge_sel if isinstance(edge_sel, list) else [edge_sel]
    edges, dangling = _resolve_edges(sel_list, body, "edge", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"sm_edge_flange: {len(dangling)} edge selector(s) dangling",
            dangling_refs=dangling,
        )
    if not edges:
        raise FeatureError("sm_edge_flange: bend edge not resolved")
    edge = edges[0]

    # --- bend-edge frame ---
    e_dir, p_start, p_end, edge_length = _edge_dir_and_endpoints(edge)
    broad_face, thick_face, n_broad, n_thick = _classify_bend_edge(
        body, edge, thickness, e_dir
    )

    # length_dir: in the broad-face plane, pointing away from the body across the thickness face.
    # That is the thickness face's outward normal projected into the broad plane (≈ n_thick, which
    # is already perpendicular to the edge and tangent to the broad face for a box sheet).
    length_dir = n_thick - n_broad * n_thick.dot(n_broad)
    if length_dir.length < 1e-9:
        # Fallback: perpendicular to edge within the broad plane.
        length_dir = e_dir.cross(n_broad)
    length_dir = length_dir * (1.0 / length_dir.length)

    # fold_dir: perpendicular to the broad face — the side the wall folds toward. By default the
    # flange folds to the SAME side as the broad face outward normal (wall rises above the sheet);
    # flip sends it the other way.
    fold_dir = n_broad * (-1.0 if flip else 1.0)
    fold_dir = fold_dir * (1.0 / max(fold_dir.length, 1e-12))

    # Build the bend+wall solid anchored at the bend edge start.
    try:
        flange = _build_flange_solid(
            p_start, e_dir, length_dir, fold_dir, edge_length,
            thickness, inner_radius, angle_deg, length, position,
        )
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"sm_edge_flange: flange geometry build failed: {exc}",
            hint="check the bend edge, radius, and that the flange does not self-intersect",
        )

    if getattr(flange, "volume", 0.0) <= 1e-9:
        raise FeatureError("sm_edge_flange: flange produced an empty solid")

    # Fuse to the running sheet body.
    target = bodies[-1]
    try:
        fused = target + flange
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"sm_edge_flange: fuse to sheet failed: {exc}",
            hint="the flange may not be tangent to the parent face (self-intersection?)",
        )

    # A valid flange shares a face with the sheet, so the union is ONE connected solid. If it only
    # touches along the bend line (e.g. bend_outside folded away from the sheet), the union stays
    # disjoint — reject that cleanly. _to_solid_normalized only collapses a genuine single-solid.
    try:
        n_solids = len(fused.solids())
    except Exception:  # noqa: BLE001
        n_solids = 1
    if n_solids > 1:
        raise FeatureError(
            "sm_edge_flange: flange is not connected to the sheet (degenerate placement)",
            hint="this position/flip folds the bend away from the sheet; try a different "
                 "position or clear 'flip'",
        )
    result = _to_solid_normalized(fused)

    # Sanity: a valid flange increases volume (it adds the bend + wall material).
    if getattr(result, "volume", 0.0) <= getattr(target, "volume", 0.0) + 1e-9:
        raise FeatureError(
            "sm_edge_flange: flange added no material (degenerate placement?)",
        )

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)

    # Carry the sheet context forward so subsequent flanges still inherit it.
    _set_sheet_context(ctx, fid, thickness, k_factor, radius_default)

    # --- bend record for the future flat pattern ---
    ba = bend_allowance(angle_deg, inner_radius, thickness, k_factor)
    bd = bend_deduction(angle_deg, inner_radius, thickness, k_factor)
    neutral_radius = inner_radius + k_factor * thickness
    bend_record = {
        "feature": fid,
        "angle": angle_deg,
        "radius": inner_radius,
        "thickness": thickness,
        "k_factor": k_factor,
        "neutral_radius": neutral_radius,
        "bend_allowance": ba,
        "bend_deduction": bd,
        "wall_length": length,
        "position": position,
        "flip": flip,
        # Geometric anchor for unfolding: the bend line endpoints + frame.
        "bend_line": [
            [p_start.X, p_start.Y, p_start.Z],
            [p_end.X, p_end.Y, p_end.Z],
        ],
        "e_dir": [e_dir.X, e_dir.Y, e_dir.Z],
        "length_dir": [length_dir.X, length_dir.Y, length_dir.Z],
        "fold_dir": [fold_dir.X, fold_dir.Y, fold_dir.Z],
    }
    # Persist bend records on a ctx datum so the (future) flat-pattern feature can read them.
    _BEND_KEY = "_sheet_metal_bends"
    if _BEND_KEY not in ctx.datums:
        ctx.datums[_BEND_KEY] = Datum(kind=_BEND_KEY, obj=[], feature_id="__sys__")
    ctx.datums[_BEND_KEY].obj.append(bend_record)

    return CompileOutput(
        bodies=bodies, tool_body=flange, op="add",
        resolved={
            "angle": angle_deg,
            "length": length,
            "radius": inner_radius,
            "k_factor": k_factor,
            "bend_allowance": ba,
            "bend_deduction": bd,
            "neutral_radius": neutral_radius,
            "volume": float(getattr(result, "volume", 0.0) or 0.0),
        },
    )


# --------------------------------------------------------------------------- fuse helper


def _fuse_to_sheet(target, addition, what: str):
    """Fuse a hem/jog addition to the running sheet and validate it stayed one connected solid."""
    if getattr(addition, "volume", 0.0) <= 1e-9:
        raise FeatureError(f"{what}: produced an empty solid")
    try:
        fused = target + addition
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"{what}: fuse to sheet failed: {exc}",
                           hint="the addition may not be tangent to the parent face")
    try:
        n_solids = len(fused.solids())
    except Exception:  # noqa: BLE001
        n_solids = 1
    if n_solids > 1:
        raise FeatureError(
            f"{what}: result is not connected to the sheet (degenerate placement)",
            hint="try clearing 'flip' or a different size")
    result = _to_solid_normalized(fused)
    if getattr(result, "volume", 0.0) <= getattr(target, "volume", 0.0) + 1e-9:
        raise FeatureError(f"{what}: added no material (degenerate placement?)")
    return result


# --------------------------------------------------------------------------- hem

_VALID_HEM_TYPES = ("closed", "open", "teardrop")


@register("sm_hem")
def compile_sm_hem(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Hem — fold a sheet edge back on itself (closed / open / teardrop).

    A hem stiffens an edge and removes the sharp burr. Three forms (mirrors SolidWorks):
      * ``closed``   — the return wall folds flat back AGAINST the sheet; the bend is 180° at a tiny
        inner radius (≈ T/2) so the two skins almost touch (gap ≈ the material thickness only).
      * ``open``     — a 180° bend at the requested ``radius``, so the return wall is parallel to the
        sheet but stood off by a visible gap (≈ 2·radius + T).
      * ``teardrop`` — the bend curls PAST 180° (≈ 235°) at ``radius`` to nearly close a round
        teardrop opening; the return wall tucks back toward the sheet.

    Geometry is a constant-thickness strip walking one (or, teardrop, one >180°) bend then a flat
    return wall, fused to the sheet. A bend record (BA = θ·(R+K·T)) is stored so the flat pattern
    develops it.
    """
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("sm_hem: no sheet body — add a base flange first")
    body = bodies[-1]

    sheet_ctx = _get_sheet_context(ctx)
    if sheet_ctx is None:
        raise FeatureError("sm_hem: part is not sheet metal — add a base flange first")
    thickness = sheet_ctx["thickness"]
    k_default = sheet_ctx["k_factor"]

    htype = p.get("type", "closed")
    if htype not in _VALID_HEM_TYPES:
        raise FeatureError(f"sm_hem: type must be one of {_VALID_HEM_TYPES}")
    length = ctx.dim(p, "length", 5.0 * thickness)
    # Closed hem: inner radius ~T/2 (skins ~T apart). Open/teardrop: a real radius (default = T).
    radius_default = (thickness / 2.0) if htype == "closed" else thickness
    inner_radius = ctx.dim(p, "radius", radius_default)
    k_factor = ctx.dim(p, "k_factor", k_default)
    flip = bool(p.get("flip", False))

    if inner_radius <= 0.0:
        raise FeatureError("sm_hem: bend radius must be > 0")
    # Clamp a degenerate return length: a hem return must be long enough to clear the bend.
    length = max(length, 1.5 * thickness)

    edge = _resolve_bend_edge(refs, body, ctx, "sm_hem")
    try:
        p_start, p_end, edge_length, e_dir, length_dir, fold_dir = _bend_frame(
            body, edge, thickness, flip)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sm_hem: could not frame the bend edge: {exc}")

    # Bend sweep angle: 180° for closed/open, >180° (curl) for teardrop.
    bend_angle = 235.0 if htype == "teardrop" else 180.0
    Rc = inner_radius + thickness / 2.0           # centerline (mid-thickness) bend radius

    # Centerline walk in the (length_dir, fold_dir) plane. Start at the mid-thickness of the base
    # material at the picked edge: x = -(R+T) setback (material_inside-like), y = -T/2 (mid-skin).
    # Run inward a touch so the bend sits on the base, then fold back by ``bend_angle`` (turning
    # toward +fold_dir = "up" then back over the sheet), then the flat return wall of ``length``.
    start_x = -(inner_radius + thickness)
    start_y = -thickness / 2.0
    pre_run = inner_radius + thickness          # bring the centerline to the bend origin
    ops = [
        {"run": pre_run},
        {"bend": bend_angle, "r_center": Rc, "ccw": True},
        {"run": length},
    ]
    try:
        hem = _strip_from_centerline(
            p_start, e_dir, length_dir, fold_dir, edge_length, thickness, ops,
            start_x, start_y)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sm_hem: geometry build failed: {exc}",
                           hint="check the bend edge, radius and that the hem does not self-intersect")

    result = _fuse_to_sheet(body, hem, "sm_hem")
    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, body)
    _set_sheet_context(ctx, fid, thickness, k_factor,
                       sheet_ctx.get("default_bend_radius", thickness))

    rec = _make_bend_record(fid, bend_angle, inner_radius, thickness, k_factor, length,
                            "material_inside", flip, p_start, p_end, e_dir, length_dir, fold_dir)
    _append_bend_record(ctx, rec)

    ba = rec["bend_allowance"]
    return CompileOutput(
        bodies=bodies, tool_body=hem, op="add",
        resolved={
            "_type": htype,
            "angle": bend_angle,
            "length": length,
            "radius": inner_radius,
            "k_factor": k_factor,
            "bend_allowance": ba,
            "neutral_radius": rec["neutral_radius"],
            "volume": float(getattr(result, "volume", 0.0) or 0.0),
        },
    )


# --------------------------------------------------------------------------- jog


@register("sm_jog")
def compile_sm_jog(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Jog — two opposing bends that step the flange to a parallel, OFFSET plane.

    A jog bends the material UP by ``angle`` (default 90°), runs a short connector to climb the
    ``offset`` (the Z-step height), then bends BACK by ``angle`` so the end flange is PARALLEL to the
    original face but offset from it by ``offset``. The flange then continues flat for ``length``.

    Two bends => two bend records (each BA = θ·(R+K·T)), so the flat pattern develops both.
    """
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("sm_jog: no sheet body — add a base flange first")
    body = bodies[-1]

    sheet_ctx = _get_sheet_context(ctx)
    if sheet_ctx is None:
        raise FeatureError("sm_jog: part is not sheet metal — add a base flange first")
    thickness = sheet_ctx["thickness"]
    k_default = sheet_ctx["k_factor"]
    radius_default = sheet_ctx.get("default_bend_radius", thickness)

    angle_deg = ctx.dim(p, "angle", 90.0)
    offset = ctx.dim(p, "offset", 10.0)
    length = ctx.dim(p, "length", 20.0)
    inner_radius = ctx.dim(p, "radius", radius_default)
    k_factor = ctx.dim(p, "k_factor", k_default)
    position = p.get("position", "material_inside")
    flip = bool(p.get("flip", False))

    if inner_radius <= 0.0:
        raise FeatureError("sm_jog: bend radius must be > 0")
    if length <= 0.0:
        raise FeatureError("sm_jog: flange length must be > 0")
    if abs(angle_deg) < 1e-6 or abs(angle_deg) > 90.0 + 1e-6:
        raise FeatureError("sm_jog: angle must be in (0, 90] degrees")
    if position not in _VALID_POSITIONS:
        raise FeatureError(f"sm_jog: position must be one of {_VALID_POSITIONS}")

    edge = _resolve_bend_edge(refs, body, ctx, "sm_jog")
    try:
        p_start, p_end, edge_length, e_dir, length_dir, fold_dir = _bend_frame(
            body, edge, thickness, flip)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sm_jog: could not frame the bend edge: {exc}")

    Rc = inner_radius + thickness / 2.0
    ang = math.radians(angle_deg)
    # The connector run climbs the vertical ``offset`` over the inclined segment between the two
    # opposing bends. For angle θ the connector's centerline length so the NET perpendicular rise of
    # the END flange's mid-plane equals ``offset`` is offset/sin(θ) minus the rise the two bend arcs
    # already contribute. Each Rc bend contributes Rc·(1−cos θ) of perpendicular rise.
    bend_rise = 2.0 * Rc * (1.0 - math.cos(ang))
    needed = offset - bend_rise
    connector = needed / math.sin(ang) if math.sin(ang) > 1e-6 else 0.0
    if connector < 0.0:
        # offset is smaller than what the two bend radii already produce — clamp to a tiny connector
        # (the achievable step is then ~bend_rise; reported in resolved).
        connector = 0.05 * thickness

    # Position setback so the first bend lands like an edge flange.
    if position == "material_inside":
        x0 = -(inner_radius + thickness)
    elif position == "material_outside":
        x0 = -inner_radius
    else:
        x0 = 0.0
    start_x = x0
    start_y = -thickness / 2.0
    pre_run = -x0  # run from the picked edge plane to the bend origin (>=0)
    if pre_run < 0:
        pre_run = 0.0
    ops = [
        {"run": pre_run + thickness},      # base lead-in to the first bend
        {"bend": angle_deg, "r_center": Rc, "ccw": True},   # bend UP
        {"run": connector},                                  # climb the offset
        {"bend": angle_deg, "r_center": Rc, "ccw": False},  # bend BACK parallel
        {"run": length},                                     # the offset end flange
    ]
    try:
        jog = _strip_from_centerline(
            p_start, e_dir, length_dir, fold_dir, edge_length, thickness, ops,
            start_x, start_y)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sm_jog: geometry build failed: {exc}",
                           hint="check the bend edge, radius, offset and that the jog does not self-intersect")

    result = _fuse_to_sheet(body, jog, "sm_jog")
    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, body)
    _set_sheet_context(ctx, fid, thickness, k_factor, radius_default)

    # TWO bend records (the up bend and the back bend). Both develop in the flat pattern.
    rec_up = _make_bend_record(fid, angle_deg, inner_radius, thickness, k_factor, connector,
                               position, flip, p_start, p_end, e_dir, length_dir, fold_dir)
    rec_back = _make_bend_record(fid, angle_deg, inner_radius, thickness, k_factor, length,
                                 position, flip, p_start, p_end, e_dir, length_dir, fold_dir)
    _append_bend_record(ctx, rec_up)
    _append_bend_record(ctx, rec_back)

    achieved_offset = bend_rise + connector * math.sin(ang)
    return CompileOutput(
        bodies=bodies, tool_body=jog, op="add",
        resolved={
            "angle": angle_deg,
            "offset": achieved_offset,
            "length": length,
            "radius": inner_radius,
            "k_factor": k_factor,
            "bend_allowance": rec_up["bend_allowance"],
            "neutral_radius": rec_up["neutral_radius"],
            "volume": float(getattr(result, "volume", 0.0) or 0.0),
        },
    )
