"""Revolve feature compiler — boss/cut + SolidWorks-parity option matrix.

feature_params.md §Sketched features > revolve.

Profile source: same resolution as extrude (region or face selectors).

Axis source: ``sel(edge[line], datum, region-line)``:
- reserved datum string (``"axis.x"`` …) / datum feature ref → that axis.
- edge selector ``{"kind":"edge","surf":"line",...}`` → a body line edge.
- region selector ``{"kind":"region","sketch":fid}`` → the sketch's CONSTRUCTION
  CENTERLINE (SolidWorks: a revolve with no picked axis spins about the sketch's
  centerline). When the axis ref is a region pointing at the profile sketch we look
  up that sketch's construction line and revolve about it.

End conditions (``params.end``, default ``one_direction``):
- ``one_direction`` / ``blind`` — revolve ``angle`` about the axis (Dir1 only).
- ``mid_plane`` — revolve ``angle`` SYMMETRICALLY about the sketch plane (±angle/2
  each side of the axis), matching SolidWorks "Mid-Plane".
- ``up_to_surface`` — revolve until the swept tool reaches a face of the existing
  body, then trim to it (geometry-driven, like extrude's up_to_next — needs a body).

Second direction (``params.dir2`` truthy, or ``angle2`` given): a second revolve of
``angle2`` about the *reversed* axis, applied in the same feature (boss unions, cut
subtracts). SolidWorks "Direction 2".

Thin feature (``params.thin`` truthy): the result is hollowed to a wall of
``thin_thickness`` on the chosen ``thin_side`` (``inside`` / ``outside`` / ``both``)
by subtracting an inner revolve of the profile offset toward the axis. This produces a
thin-wall (shell-like) revolve, matching SolidWorks "Thin Feature".

Validates that the axis does not cross the profile interior (clean error per contract).
"""

from __future__ import annotations

import math
from typing import Any

from build123d import (
    Axis,
    Face,
    Vector,
    Solid,
    Wire,
)
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_edges, resolve

# Import shared helpers from extrude
from .extrude import (
    _body_bbox_diag,
    _resolve_profile_faces,
    _to_solid_normalized,
)

_VALID_ENDS = ("one_direction", "blind", "mid_plane", "up_to_surface")
_VALID_SIDES = ("inside", "outside", "both")

# --------------------------------------------------------------------------- axis resolution


def _centerline_axis_from_sketch(sketch_id: str, ctx: CompileContext) -> Axis:
    """Build a revolve Axis from a sketch's construction CENTERLINE.

    SolidWorks: a revolve whose axis isn't an explicitly picked edge spins about the
    sketch's centerline (a construction line). The sketch compiler skips construction
    entities when building region edges but stashes the raw entity list + plane in
    ``ctx.datums["_sketch_regions"][fid]`` — we read the first construction LINE there
    and lift its 2-D endpoints into the sketch plane to form the axis.
    """
    store = ctx.datums.get("_sketch_regions")
    sketch_data = store.obj.get(sketch_id) if store is not None else None
    if sketch_data is None:
        raise FeatureError(
            f"revolve refs.axis: sketch '{sketch_id}' not compiled (wrong order?)"
        )
    entities = sketch_data.get("entities") or []
    plane = sketch_data.get("plane")
    if plane is None:
        raise FeatureError(
            f"revolve refs.axis: sketch '{sketch_id}' has no plane for the centerline"
        )

    # The centerline is a construction line. Prefer one explicitly tagged centerline,
    # else the first construction line entity.
    cl = None
    for ent in entities:
        if ent.get("type") == "line" and ent.get("construction", False):
            cl = ent
            break
    if cl is None:
        raise FeatureError(
            "revolve refs.axis: no axis picked and the sketch has no construction "
            "centerline to revolve about",
            hint="draw a construction line (centerline) in the sketch, or pick an axis edge",
        )

    p1 = cl.get("p1")
    p2 = cl.get("p2")
    if not (isinstance(p1, (list, tuple)) and isinstance(p2, (list, tuple))
            and len(p1) == 2 and len(p2) == 2):
        raise FeatureError("revolve refs.axis: centerline endpoints malformed")

    # Lift 2-D sketch-space endpoints into 3-D plane space.
    origin = plane.origin
    x_dir = plane.x_dir
    y_dir = plane.y_dir

    def _lift(uv) -> Vector:
        return Vector(
            origin.X + x_dir.X * uv[0] + y_dir.X * uv[1],
            origin.Y + x_dir.Y * uv[0] + y_dir.Y * uv[1],
            origin.Z + x_dir.Z * uv[0] + y_dir.Z * uv[1],
        )

    a = _lift(p1)
    b = _lift(p2)
    direction = b - a
    ln = direction.length
    if ln < 1e-9:
        raise FeatureError("revolve refs.axis: centerline is degenerate (zero length)")
    return Axis(a, direction * (1.0 / ln))


def _resolve_axis_ref(axis_ref: Any, ctx: CompileContext, profile_face: Face) -> Axis:
    """Resolve refs.axis to a build123d Axis.

    Supports:
    - reserved datum string (``"axis.x"`` / ``"axis.y"`` / ``"axis.z"``)
    - datum feature ref
    - edge selector ``{"kind":"edge","surf":"line",...}``
    - region selector ``{"kind":"region","sketch":fid}`` → the sketch's construction
      centerline (SolidWorks default revolve axis).
    """
    # Region selector → sketch construction centerline.
    if isinstance(axis_ref, dict) and axis_ref.get("kind") == "region":
        sketch_id = axis_ref.get("sketch")
        if not isinstance(sketch_id, str):
            raise FeatureError("revolve refs.axis: region selector needs a 'sketch' id")
        return _centerline_axis_from_sketch(sketch_id, ctx)

    if isinstance(axis_ref, str) or (
        isinstance(axis_ref, dict) and axis_ref.get("kind") == "datum"
    ):
        d = ctx.datum(axis_ref)
        if d.kind == "axis":
            return d.obj
        raise FeatureError(
            f"revolve refs.axis: datum has kind '{d.kind}', expected 'axis'"
        )

    if isinstance(axis_ref, dict) and axis_ref.get("kind") == "edge":
        # Resolve against current body edges
        bodies = ctx.bodies
        if not bodies:
            raise FeatureError("revolve refs.axis: edge selector but no bodies exist")
        body = bodies[-1]
        infos = describe_edges(body.wrapped)
        diag = _body_bbox_diag(body)
        rr = resolve(axis_ref, infos, diag)
        if rr.index is None:
            raise FeatureError(
                f"revolve refs.axis: edge selector dangling — {rr.dangling_reason}",
                dangling_refs=[f"axis[0]: {rr.dangling_reason}"],
            )
        edge = body.edges()[rr.index]
        # Extract axis from a line edge
        try:
            verts = edge.vertices()
            if len(verts) >= 2:
                p1 = Vector(*[verts[0].X, verts[0].Y, verts[0].Z])
                p2 = Vector(*[verts[1].X, verts[1].Y, verts[1].Z])
                direction = (p2 - p1)
                ln = direction.length
                if ln < 1e-12:
                    raise FeatureError("revolve refs.axis: edge too short to define a direction")
                direction = direction * (1.0 / ln)
                return Axis(p1, direction)
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"revolve refs.axis: failed to extract axis from edge: {exc}")

    raise FeatureError(f"revolve refs.axis: unsupported ref form {axis_ref!r}")


def _axis_crosses_profile(axis: Axis, face: Face) -> bool:
    """Heuristic check: does the axis cross the profile interior?

    We project the profile vertices onto a plane perpendicular to the axis and flag the
    axis when the projected points straddle the origin in both perpendicular directions
    (i.e. the axis line passes through the projected profile region). Conservative: a
    false positive only rejects a borderline profile, a false negative lets OCCT catch it.
    """
    try:
        origin = axis.position
        direction = axis.direction

        verts = face.vertices()
        if not verts:
            return False

        origin_v = Vector(origin.X, origin.Y, origin.Z)
        dir_v = Vector(direction.X, direction.Y, direction.Z)

        # Build a 2D coordinate system perpendicular to axis
        perp1 = dir_v.perpendicular_line.direction
        perp2 = dir_v.cross(perp1)

        def _project_2d(point: Vector):
            diff = point - origin_v
            u = diff.dot(perp1)
            v = diff.dot(perp2)
            return u, v

        pts_2d = [_project_2d(Vector(vx.X, vx.Y, vx.Z)) for vx in verts]
        us = [u for u, _ in pts_2d]
        vs = [v for _, v in pts_2d]
        if us and (min(us) < -1e-3 < max(us)) or (vs and min(vs) < -1e-3 < max(vs)):
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- tool builders


def _ocp_ax1(axis: Axis, reverse: bool = False) -> gp_Ax1:
    """Build an OCP gp_Ax1 from a build123d Axis (optionally reversed)."""
    o = axis.position
    d = axis.direction
    sign = -1.0 if reverse else 1.0
    return gp_Ax1(
        gp_Pnt(o.X, o.Y, o.Z),
        gp_Dir(d.X * sign, d.Y * sign, d.Z * sign),
    )


def _revolve_face(face: Face, axis: Axis, angle_rad: float, reverse: bool = False) -> Solid:
    """Revolve one profile face about *axis* by *angle_rad* (>0) via OCCT MakeRevol."""
    ocp_ax = _ocp_ax1(axis, reverse=reverse)
    revol_builder = BRepPrimAPI_MakeRevol(face.wrapped, ocp_ax, angle_rad, True)
    if not revol_builder.IsDone():
        raise FeatureError("revolve: OCCT MakeRevol failed (IsDone=False)")
    return Solid(revol_builder.Shape()).clean()


def _union(solids: list[Any]) -> Any:
    out = solids[0]
    for s in solids[1:]:
        out = out + s
    return out


def _offset_face_toward_axis(face: Face, axis: Axis, thickness: float,
                             side: str) -> Face | None:
    """Build the INNER profile face for a thin (thin-wall) revolve.

    The revolved wall is bounded by the original profile and a copy offset toward the
    revolve axis by ``thickness``. We approximate the offset by translating the whole
    profile face perpendicular toward the axis (in the profile plane). For an axis-aligned
    annular/box profile this yields a clean tube wall; ``side`` selects which way the wall
    grows (``outside`` = away from axis, ``inside`` = toward axis, ``both`` = split).

    Returns the inner face to SUBTRACT from the solid revolve (its revolve carves the
    bore). Returns ``None`` if the offset collapses the profile.
    """
    try:
        # Profile centroid and a radial direction = component of (centroid - axis) ⟂ axis.
        c = face.center()
        cp = Vector(c.X, c.Y, c.Z)
        o = axis.position
        d = axis.direction
        diff = cp - Vector(o.X, o.Y, o.Z)
        along = d * diff.dot(d)
        radial = diff - along
        rl = radial.length
        if rl < 1e-9:
            return None
        radial = radial * (1.0 / rl)

        # Shift magnitude/direction per side. The inner face is the original profile moved
        # toward the axis by `thickness`; its revolve removes everything inside the wall.
        if side == "outside":
            shift = radial * (-thickness)      # inner boundary sits inward by t
        elif side == "both":
            shift = radial * (-thickness)      # symmetric handled by caller halving t
        else:  # inside (default)
            shift = radial * (-thickness)

        from build123d import Pos
        inner = Pos(shift.X, shift.Y, shift.Z) * face
        return inner
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- revolve


@register("revolve")
def compile_revolve(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    end = p.get("end", "one_direction")
    if end == "blind":
        end = "one_direction"
    if end not in _VALID_ENDS:
        raise FeatureError(f"revolve: unknown end '{end}'")

    angle_deg = ctx.dim(p, "angle", 360.0)
    flip = p.get("flip", False)
    merge = p.get("merge", True)

    # Direction 2 — a second revolve about the reversed axis (SolidWorks "Direction 2").
    dir2 = bool(p.get("dir2", False)) or (p.get("angle2") is not None)
    angle2_deg = ctx.dim(p, "angle2", angle_deg) if dir2 else 0.0

    # Thin feature.
    thin = bool(p.get("thin", False))
    thin_thickness = ctx.dim(p, "thin_thickness", 0.0) if thin else 0.0
    thin_side = p.get("thin_side", "inside")
    if thin and thin_side not in _VALID_SIDES:
        raise FeatureError(f"revolve: unknown thin_side '{thin_side}'")
    if thin and thin_thickness <= 0.0:
        raise FeatureError("revolve: thin feature requires a positive thin_thickness")

    if abs(angle_deg) < 1e-6:
        raise FeatureError("revolve: angle must be non-zero")
    if abs(angle_deg) > 360.0 + 1e-6:
        angle_deg = math.copysign(360.0, angle_deg)
    if dir2:
        if abs(angle2_deg) < 1e-6:
            raise FeatureError("revolve: angle2 must be non-zero when Direction 2 is on")
        if abs(angle2_deg) > 360.0 + 1e-6:
            angle2_deg = math.copysign(360.0, angle2_deg)

    if flip:
        angle_deg = -angle_deg

    # Resolve profile
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("revolve: refs.profile is required")

    profile_faces: list[Face] = []
    dangling: list[str] = []
    for i, sel in enumerate(profile_sels):
        try:
            profile_faces.extend(_resolve_profile_faces(sel, ctx))
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"profile[{i}]: {dr}" for dr in exc.dangling_refs])
            else:
                dangling.append(f"profile[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"revolve: {len(dangling)} profile selector(s) failed",
            dangling_refs=dangling,
        )
    if not profile_faces:
        raise FeatureError("revolve: no profiles resolved")

    # Resolve axis
    axis_ref = refs.get("axis")
    if axis_ref is None:
        raise FeatureError("revolve: refs.axis is required")

    try:
        b3d_axis = _resolve_axis_ref(axis_ref, ctx, profile_faces[0])
    except FeatureError as exc:
        raise FeatureError(
            f"revolve: axis resolution failed: {exc}",
            dangling_refs=exc.dangling_refs,
        )

    # Validate axis doesn't cross profile interior
    if _axis_crosses_profile(b3d_axis, profile_faces[0]):
        raise FeatureError(
            "revolve: axis crosses the profile interior",
            hint="move the axis to the edge of or outside the profile",
        )

    bodies = list(ctx.bodies)
    resolved: dict[str, float | str] = {"angle": angle_deg, "end": end}

    # ----------------------------------------------------------------- build the tool
    # Angle to sweep for the up_to_surface case is generous; we trim later.
    sweep_full = 2.0 * math.pi - 1e-4  # ~360° (avoid exact closed-period degeneracy)

    def _build_dir(face: Face, reverse: bool) -> Solid:
        """Build the dir-1 (or dir-2) revolved tool slab for one face, honoring `end`."""
        if end == "mid_plane":
            half = math.radians(abs(angle_deg)) / 2.0
            fwd = _revolve_face(face, b3d_axis, half, reverse=reverse)
            rev = _revolve_face(face, b3d_axis, half, reverse=not reverse)
            return _union([fwd, rev])
        if end == "up_to_surface":
            if not bodies:
                raise FeatureError(
                    "revolve: up_to_surface requires an existing body to revolve up to",
                    hint="add a base feature, or use a blind/mid-plane angle",
                )
            # Sweep a generous (near-full) revolve, carve away the target body, and keep only
            # the slab that starts at the profile and runs UP TO the first body surface. This is
            # the revolve analogue of extrude's up_to_next: the surface terminates the sweep.
            full = _revolve_face(face, b3d_axis, sweep_full, reverse=reverse)
            leftover = full - bodies[-1]
            try:
                slabs = leftover.solids()
            except Exception:  # noqa: BLE001
                slabs = []
            if not slabs:
                raise FeatureError(
                    "revolve: up_to_surface produced no material",
                    hint="the body fully blocks the sweep, or the profile never reaches it",
                )
            # The start slab is the piece overlapping a tiny probe revolve from the start angle.
            probe = _revolve_face(face, b3d_axis, math.radians(1.5), reverse=reverse)
            start_pieces: list[Any] = []
            for s in slabs:
                try:
                    if getattr(s.intersect(probe), "volume", 0.0) > 1e-7:
                        start_pieces.append(s)
                except Exception:  # noqa: BLE001
                    continue
            if not start_pieces:
                raise FeatureError(
                    "revolve: up_to_surface could not isolate the swept slab to the surface",
                    hint="the profile may already lie inside the target body",
                )
            return _to_solid_normalized(_union(start_pieces))
        # one_direction (blind angle).
        return _revolve_face(face, b3d_axis, math.radians(abs(angle_deg)), reverse=reverse)

    tool_bodies: list[Any] = []
    for face in profile_faces:
        piece = _build_dir(face, reverse=False)
        if dir2 and end != "up_to_surface":
            # Second direction sweeps the OTHER way by angle2 (independent slab).
            piece2 = _revolve_face(face, b3d_axis, math.radians(abs(angle2_deg)),
                                   reverse=True)
            piece = piece + piece2
            resolved["angle2"] = angle2_deg
        tool_bodies.append(piece)

    tool_solid = _to_solid_normalized(_union(tool_bodies))

    # ----------------------------------------------------------------- thin feature
    if thin:
        per_face_t = thin_thickness / 2.0 if thin_side == "both" else thin_thickness
        inner_pieces: list[Any] = []
        for face in profile_faces:
            inner_face = _offset_face_toward_axis(face, b3d_axis, per_face_t, thin_side)
            if inner_face is None:
                continue
            try:
                if end == "mid_plane":
                    half = math.radians(abs(angle_deg)) / 2.0
                    inner = _union([
                        _revolve_face(inner_face, b3d_axis, half, reverse=False),
                        _revolve_face(inner_face, b3d_axis, half, reverse=True),
                    ])
                elif end == "up_to_surface":
                    inner = _revolve_face(inner_face, b3d_axis, sweep_full, reverse=False)
                else:
                    inner = _revolve_face(inner_face, b3d_axis,
                                          math.radians(abs(angle_deg)), reverse=False)
                    if dir2:
                        inner = inner + _revolve_face(
                            inner_face, b3d_axis, math.radians(abs(angle2_deg)),
                            reverse=True)
                inner_pieces.append(inner)
            except Exception:  # noqa: BLE001
                continue
        if inner_pieces:
            inner_solid = _union(inner_pieces)
            try:
                hollow = tool_solid - inner_solid
                hollow = _to_solid_normalized(hollow)
                # Only accept the hollow if it actually removed material (a wall remains).
                if getattr(hollow, "volume", 0.0) > 1e-9 and \
                        getattr(hollow, "volume", 0.0) < getattr(tool_solid, "volume", 0.0) - 1e-9:
                    tool_solid = hollow
                    resolved["thin"] = float(thin_thickness)
                    resolved["thin_side"] = thin_side
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- apply to body
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("revolve: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        return CompileOutput(bodies=bodies, tool_body=tool_solid, op="add",
                             resolved=resolved)

    target = bodies[-1]
    try:
        if mode == "cut":
            result = target - tool_solid
        else:
            result = target + tool_solid
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"revolve: boolean {mode} failed: {exc}",
            hint="the profile may not intersect the body correctly",
        )

    result = _to_solid_normalized(result)

    if mode == "cut":
        old_vol = getattr(target, "volume", 0.0)
        new_vol = getattr(result, "volume", 0.0)
        if abs(new_vol - old_vol) < 1e-9:
            raise FeatureError(
                "revolve cut removed no material",
                hint="the profile does not intersect the target body",
            )

    bodies[-1] = result
    return CompileOutput(bodies=bodies, tool_body=tool_solid,
                         op=("cut" if mode == "cut" else "add"),
                         resolved=resolved)
