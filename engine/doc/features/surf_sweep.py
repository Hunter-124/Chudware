"""Swept-surface feature compiler — an open SHELL swept from a profile wire along a path.

``surf_sweep`` is the surfacing sibling of the solid :mod:`sweep`. It resolves the SAME
profile / path / guide references as a solid sweep, then sweeps the profile WIRE along the path
WITHOUT capping the ends — yielding an open ``Shell`` surface body (``kind="surface"``) instead of
a closed solid. SolidWorks "Swept Surface": a profile sketch dragged along a path leaves a sheet,
not a solid; a closed profile leaves an open tube WALL (no end caps).

Reuse: profile resolution is ``extrude._resolve_profile_faces`` (identical to the solid sweep, so
a region selector resolves exactly as it does for a solid extrude/sweep); path and guide
resolution are ``sweep._resolve_path_wire`` / ``sweep._resolve_curve_ref``; the actual swept-shell
construction is ``sweep._pipe_shell(..., make_solid=False)`` — the shared pipe builder that, when
told not to ``MakeSolid()``, returns the bare open ``Shell`` (its ``BRepOffsetAPI_MakePipeShell``
result). Orientation, guide-curve, and twist options thread through that same builder.

The resolved profile is a Face (a region of the sketch); ``_pipe_shell`` sweeps its outer wire.
For a closed profile region this gives the open side wall of a tube; the open ends are exactly
the ``surf_sweep`` surface (the solid sweep would cap them). Multiple profile regions each produce
a shell; they are returned as one shell-compound surface body.

Surfaces never fuse into the running solid — each ``surf_sweep`` appends a NEW surface body to the
body list (mirroring :mod:`surf_planar`).
"""

from __future__ import annotations

from typing import Any

from build123d import Compound, Shell

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _resolve_profile_faces, _store_topo_owners
from .sweep import (
    _VALID_ORIENT,
    _pipe_shell,
    _resolve_curve_ref,
    _resolve_path_wire,
)


@register("surf_sweep")
def compile_surf_sweep(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    # --- orientation / twist (same vocabulary as the solid sweep) --------------------------
    orientation = p.get("orientation", "follow_path")
    if orientation not in _VALID_ORIENT:
        raise FeatureError(
            f"surf_sweep: unknown orientation '{orientation}' "
            f"(expected one of {', '.join(_VALID_ORIENT)})"
        )

    twist_turns = ctx.dim(p, "twist_turns", 0.0) if p.get("twist_turns") is not None else 0.0
    twist_extra_deg = (
        ctx.dim(p, "twist_degrees", 0.0) if p.get("twist_degrees") is not None else 0.0
    )
    twist_deg = twist_turns * 360.0 + twist_extra_deg

    # --- profiles (resolved exactly like a solid sweep/extrude) ----------------------------
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("surf_sweep: refs.profile is required")

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
            f"surf_sweep: {len(dangling)} profile selector(s) failed", dangling_refs=dangling
        )
    if not profile_faces:
        raise FeatureError("surf_sweep: no profiles resolved")

    # --- path (the sweep spine) ------------------------------------------------------------
    path_ref = refs.get("path")
    if path_ref is None:
        raise FeatureError("surf_sweep: refs.path is required")
    try:
        path_wire = _resolve_path_wire(path_ref, ctx)
    except FeatureError as exc:
        raise FeatureError(
            f"surf_sweep: path resolution failed: {exc}", dangling_refs=exc.dangling_refs
        )

    # --- guide curves (optional; OCCT drives one auxiliary spine per shell) -----------------
    guide_sels = refs.get("guides") or []
    guide_wire = None
    guide_note: str | None = None
    if guide_sels:
        try:
            guide_wire = _resolve_curve_ref(guide_sels[0], ctx, "guide[0]")
        except FeatureError as exc:
            raise FeatureError(
                f"surf_sweep: guide resolution failed: {exc}", dangling_refs=exc.dangling_refs
            )
        if len(guide_sels) > 1:
            guide_note = (
                f"{len(guide_sels)} guides given; OCCT uses 1 auxiliary spine per sweep — "
                "the first guide was applied"
            )

    # --- sweep each profile wire into an OPEN shell (no end caps) ---------------------------
    shells: list[Shell] = []
    for face in profile_faces:
        try:
            shell = _pipe_shell(
                path_wire, face, orientation=orientation,
                guide_wire=guide_wire, twist_deg=twist_deg, make_solid=False,
            )
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"surf_sweep: build failed: {exc}",
                hint="the profile must be a valid wire positioned at the path start",
            )
        shells.append(shell)

    surface: Any = shells[0] if len(shells) == 1 else Compound(shells)

    resolved: dict[str, Any] = {}
    if abs(twist_deg) > 1e-9:
        resolved["twist_degrees"] = twist_deg
    if orientation != "follow_path":
        resolved["orientation"] = orientation
    if guide_wire is not None:
        resolved["guides"] = len(guide_sels)
    if guide_note:
        resolved["_guide_note"] = guide_note

    # Append as a NEW surface body (surfaces never merge into the running solid).
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved)
