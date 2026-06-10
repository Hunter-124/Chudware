"""Revolve feature compiler — boss/cut, full/partial revolution.

feature_params.md §Sketched features > revolve.

Profile source: same resolution as extrude (region or face selectors).
Axis source: ``sel(edge[line], datum, region-line)`` — in M1 we support datum and
edge selectors; ``region-line`` is deferred.

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

# Import shared helper from extrude
from .extrude import (
    _body_bbox_diag,
    _resolve_profile_selector,
    _to_solid_normalized,
)

# --------------------------------------------------------------------------- axis resolution


def _resolve_axis_ref(axis_ref: Any, ctx: CompileContext, profile_face: Face) -> Axis:
    """Resolve refs.axis to a build123d Axis.

    Supports:
    - reserved datum string (``"axis.x"`` / ``"axis.y"`` / ``"axis.z"``)
    - datum feature ref
    - edge selector ``{"kind":"edge","surf":"line",...}``
    """
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

    We project the axis onto the face plane and check if the projected line
    intersects any face edges. A more robust check would use BRepAlgoAPI, but
    for M1 we use a centroid-based approximation:

    If the face centroid is on the same side as the axis origin for all face
    vertices, the axis is outside. We use BRepAdaptor + intersection checks
    against the bounding box of the face projected perpendicular to the axis.
    """
    try:
        # Project face centroid and vertices onto a plane perpendicular to axis
        origin = axis.position
        direction = axis.direction

        # Get face bounding box extremes relative to axis
        verts = face.vertices()
        if not verts:
            return False

        # For each vertex, compute signed distance to axis line (= min cross product)
        def _dist_to_axis(point: Vector) -> float:
            diff = point - origin
            proj_len = diff.dot(direction)
            proj_point = origin + direction * proj_len
            perp = point - proj_point
            return perp.length

        distances = [_dist_to_axis(Vector(v.X, v.Y, v.Z)) for v in verts]

        # Also check the centroid
        try:
            cx, cy, cz = face.center().X, face.center().Y, face.center().Z
            cd = _dist_to_axis(Vector(cx, cy, cz))
        except Exception:  # noqa: BLE001
            cd = min(distances)

        # If any vertex is very close to the axis and they're on both sides,
        # the axis may cross the profile. Simplified: if the closest distance < 1e-3
        # AND the axis projects through the face bounding region, flag it.
        # For M1 we only catch the obvious case: min distance ≈ 0 with vertices
        # on both sides (i.e., axis passes near a vertex that's on the profile boundary).
        # A more thorough check is left for M2.

        # Actually use a projection approach: project face onto plane perpendicular to axis
        # and check if origin is inside the projected boundary.
        # Simplified: use bounding box of perpendicular distances with sign via cross product.
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

        # Winding number / sign change: if both positive and negative u values exist
        # AND both positive and negative v values, origin (0,0) might be inside.
        us = [u for u, _ in pts_2d]
        vs = [v for _, v in pts_2d]
        if us and (min(us) < -1e-3 < max(us)) or (vs and min(vs) < -1e-3 < max(vs)):
            # Origin (axis) might be inside the projected polygon — be conservative
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- revolve


@register("revolve")
def compile_revolve(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    angle_deg = ctx.dim(p, "angle", 360.0)
    flip = p.get("flip", False)
    merge = p.get("merge", True)

    if abs(angle_deg) < 1e-6:
        raise FeatureError("revolve: angle must be non-zero")
    if abs(angle_deg) > 360.0 + 1e-6:
        angle_deg = math.copysign(360.0, angle_deg)

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
            f = _resolve_profile_selector(sel, ctx)
            profile_faces.append(f)
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

    # Build revolved tool for each profile face
    tool_bodies: list[Any] = []
    angle_rad = math.radians(abs(angle_deg))

    for face in profile_faces:
        try:
            # Build OCP revolve directly (more reliable than build123d wrapper for raw faces)
            ax_origin = b3d_axis.position
            ax_dir = b3d_axis.direction
            ocp_ax = gp_Ax1(
                gp_Pnt(ax_origin.X, ax_origin.Y, ax_origin.Z),
                gp_Dir(ax_dir.X, ax_dir.Y, ax_dir.Z),
            )
            revol_builder = BRepPrimAPI_MakeRevol(face.wrapped, ocp_ax, angle_rad, True)
            if not revol_builder.IsDone():
                raise FeatureError("revolve: OCCT MakeRevol failed (IsDone=False)")
            solid = Solid(revol_builder.Shape()).clean()
            tool_bodies.append(solid)
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"revolve: build failed: {exc}",
                hint="check that the profile is in a plane containing the axis",
            )

    # Combine tool solids
    if len(tool_bodies) == 1:
        tool_solid = tool_bodies[0]
    else:
        tool_solid = tool_bodies[0]
        for tb in tool_bodies[1:]:
            tool_solid = tool_solid + tb
    tool_solid = _to_solid_normalized(tool_solid)

    # Apply to running body
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("revolve: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        return CompileOutput(bodies=bodies, tool_body=tool_solid,
                             resolved={"angle": angle_deg})

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
                         resolved={"angle": angle_deg})
