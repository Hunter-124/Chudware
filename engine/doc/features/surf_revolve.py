"""Revolved-surface feature compiler — an OPEN surface of revolution (no end caps).

``surf_revolve`` is the surfacing analogue of the solid ``revolve``: instead of revolving a
capped profile FACE (which sweeps a solid), it revolves the profile's outer WIRE about an axis
to sweep an open SHELL — the surface of revolution of that wire. A rectangle profile spun a full
turn yields the four walls the rectangle's edges trace (two cylinders + two annular discs), with
NO solid interior and NO virtual caps closing the ends of a partial sweep.

It reuses the solid revolve's resolution machinery verbatim:
- profile faces via ``extrude._resolve_profile_faces`` (a region/face selector resolves exactly
  as it does for a solid extrude/revolve), then ``face.outer_wire()`` is the wire we revolve;
- the revolve axis via ``revolve._resolve_axis_ref`` (reserved datum string ``"axis.z"`` …, a
  datum feature ref, a body line-edge selector, or a sketch construction-centerline region).

The wire is swept with OCCT ``BRepPrimAPI_MakeRevol(wire, gp_Ax1, angle_rad)`` — given a WIRE (not
a face) the primitive builds a SHELL, the open surface of revolution. The result is appended as a
NEW surface body (surfaces never fuse into the running solid) and the compiler tags its output
``kind="surface"`` so the core stamps the BodyOut, mass_props reports AREA, and tessellation emits
a two-sided mesh.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Shell, Wire
from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _resolve_profile_faces, _store_topo_owners
from .revolve import _ocp_ax1, _resolve_axis_ref


@register("surf_revolve")
def compile_surf_revolve(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    p = feat.get("params", {})
    fid = feat.get("id", "?")

    # --- angle (degrees → radians), clamped to a full turn like the solid revolve -------------
    angle_deg = ctx.dim(p, "angle", 360.0)
    if abs(angle_deg) < 1e-6:
        raise FeatureError("surf_revolve: angle must be non-zero")
    if abs(angle_deg) > 360.0 + 1e-6:
        angle_deg = math.copysign(360.0, angle_deg)
    angle_rad = math.radians(abs(angle_deg))

    # --- profile resolution (reuse extrude/revolve's face resolver) ---------------------------
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("surf_revolve: refs.profile is required")

    profile_faces: list[Any] = []
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
            f"surf_revolve: {len(dangling)} profile selector(s) failed",
            dangling_refs=dangling,
        )
    if not profile_faces:
        raise FeatureError("surf_revolve: no profiles resolved")

    # --- axis resolution (reuse the solid revolve's axis resolver) ----------------------------
    axis_ref = refs.get("axis")
    if axis_ref is None:
        raise FeatureError("surf_revolve: refs.axis is required")
    try:
        b3d_axis = _resolve_axis_ref(axis_ref, ctx, profile_faces[0])
    except FeatureError as exc:
        raise FeatureError(
            f"surf_revolve: axis resolution failed: {exc}",
            dangling_refs=exc.dangling_refs,
        )

    ocp_ax = _ocp_ax1(b3d_axis)

    # --- revolve each profile's OUTER WIRE → an open shell (surface of revolution) -------------
    # A WIRE (not a capped face) makes BRepPrimAPI_MakeRevol build a SHELL: only the surface the
    # wire's edges sweep, with NO end caps. Multiple profile regions each contribute their shell.
    shells: list[Shell] = []
    for face in profile_faces:
        try:
            wire = face.outer_wire()
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"surf_revolve: could not read the profile outer wire: {exc}"
            )
        builder = BRepPrimAPI_MakeRevol(wire.wrapped, ocp_ax, angle_rad, True)
        if not builder.IsDone():
            raise FeatureError("surf_revolve: OCCT MakeRevol failed (IsDone=False)")
        try:
            shell = Shell(builder.Shape())
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"surf_revolve: revolved wire did not form a shell: {exc}"
            )
        shells.append(shell)

    # The foundation surfacing convention keeps the first region as the single surface body
    # (matching surf_planar); additional regions are merged into one shell when present.
    surface: Shell = shells[0]
    for extra in shells[1:]:
        try:
            surface = surface.fuse(extra)
        except Exception:  # noqa: BLE001
            # A failed fuse leaves the first shell as the surface body (clean degrade).
            break

    # Append as a NEW surface body (surfaces never merge into the running solid).
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)
    return CompileOutput(bodies=bodies, kind="surface",
                         resolved={"angle": float(math.copysign(angle_deg, angle_deg))})
