"""Planar surface feature compiler — the Surfacing-foundation canary.

``surf_planar`` is the simplest open-surface feature: it resolves a closed planar sketch profile
and returns that bounding FACE as a single-face SURFACE body (no thickness, no enclosed volume).
It exists to exercise the body-``kind`` path end-to-end — the compiler tags its output
``kind="surface"``, which the core stamps onto the BodyOut so tessellation emits a two-sided mesh
and mass_props reports area instead of volume.

It reuses ``extrude._resolve_profile_faces`` for profile resolution (so a region selector resolves
exactly as it does for a solid extrude). The resolved face IS the surface body — appended to the
body list as a new body (surfaces never fuse into the running solid).

This is the only surfacing compiler in the foundation batch; later batches add the remaining 15
``surf_*`` feature types (their schema entries already exist).
"""

from __future__ import annotations

from typing import Any

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _resolve_profile_faces, _store_topo_owners


@register("surf_planar")
def compile_surf_planar(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("surf_planar: refs.profile is required")

    faces: list[Any] = []
    dangling: list[str] = []
    for i, sel in enumerate(profile_sels):
        try:
            faces.extend(_resolve_profile_faces(sel, ctx))
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"profile[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"profile[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"surf_planar: {len(dangling)} profile selector(s) failed",
            dangling_refs=dangling,
        )
    if not faces:
        raise FeatureError("surf_planar: no profiles resolved")

    # The bounding planar face IS the surface body. A whole-sketch profile may yield several
    # material regions; the foundation canary keeps the first (single-face surface body).
    surface = faces[0]

    # Append as a NEW surface body (surfaces never merge into the running solid).
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)
    return CompileOutput(bodies=bodies, kind="surface")
