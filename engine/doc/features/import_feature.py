"""``import`` feature compiler (feature_params.md §Escape hatches / import).

Params
------
* ``source_path: str``   — absolute or repo-relative path to the source geometry file.
* ``format: "step"|"iges"|"brep"|"stl"|...``   — explicit format (auto-detected from extension
  when absent from the params dict).
* ``original_name: str?``   — display name for the feature; overrides the file stem.
* ``as_solid: bool?``   — for mesh sources (STL/OBJ/PLY/3MF/glTF/3dm), sew the triangle soup into
  an OCCT solid on import (SolidWorks "Convert to Solid Body"). Falls back to a mesh body per-body
  if it can't be closed into a valid solid.

Behaviour
---------
Bodies are **frozen** (no upstream params).  The compiler calls :func:`formats._cached_import`
(mtime-keyed) so repeated recompiles do NOT re-read the file.  All resulting shapes are returned
as separate bodies whose ``merge`` is always ``False`` — imported geometry is never auto-fused.

The feature is intentionally kept simple for M8.  Later cards can add transform/placement params.
"""

from __future__ import annotations

from .base import CompileContext, CompileOutput, FeatureError, register


@register("import")
def compile_import(feat: dict, ctx: CompileContext) -> CompileOutput:  # noqa: ARG001
    """Compile an ``import`` feature by loading (or cache-hitting) the source file."""
    params = feat.get("params") or {}

    source_path: str | None = params.get("source_path")
    if not source_path:
        raise FeatureError("import feature missing 'source_path'")

    fmt: str | None = params.get("format") or None  # may be empty string

    try:
        from engine.doc.formats import _cached_import
        body_pairs = _cached_import(source_path, fmt)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"import failed for '{source_path}': {exc}",
            hint="Check that the file exists and is a valid geometry file.",
        ) from exc

    if not body_pairs:
        raise FeatureError(f"import: no bodies found in '{source_path}'")

    # Frozen bodies: carry prior bodies forward, append each imported shape as an extra body.
    # Two kinds of body can come back (FORMATS_PLAN.md §2):
    #   * an OCCT B-rep shape (STEP / IGES / BREP) — has a non-None ``.wrapped``;
    #   * a MeshBody triangle soup (STL / OBJ / PLY / 3MF / glTF) — ``.wrapped`` is None but it
    #     tessellates directly via tessellate_mesh_body, so it IS a valid renderable body now.
    # Only a genuinely-empty (None) result is skipped.
    #
    # ``as_solid`` (import-time option / SolidWorks "Convert to Solid Body"): when true, each mesh
    # body is sewn into an OCCT solid (mesh_solidify.solidify) so it can be filleted/cut/booleaned.
    # If a particular mesh can't be closed into a valid solid, it falls back to a mesh body (the
    # import still succeeds, just stays a graphics body).
    from engine.doc.mesh_body import MeshBody

    as_solid = bool(params.get("as_solid"))

    new_bodies = list(ctx.bodies)
    skipped = 0
    has_mesh = False
    for _name, shape in body_pairs:
        if shape is None:
            skipped += 1
            continue
        if isinstance(shape, MeshBody):
            if as_solid:
                from engine.doc.mesh_solidify import solidify
                solid = solidify(shape)
                if solid is not None:
                    new_bodies.append(solid)   # now a real B-rep solid (kind="solid")
                    continue
                # conversion failed → keep the mesh body so the import still succeeds
            new_bodies.append(shape)
            has_mesh = True
            continue
        if getattr(shape, "wrapped", "sentinel") is None:
            # Unknown proxy with no OCCT topology and not a MeshBody — cannot tessellate.
            skipped += 1
            continue
        new_bodies.append(shape)

    if len(new_bodies) == len(ctx.bodies):
        raise FeatureError(
            f"import: no usable bodies in '{source_path}' ({skipped} skipped).",
            hint="The file produced no OCCT solid or mesh body.",
        )

    # A mesh import has no parametric history; flag the produced body as kind="mesh". (The compiler
    # core also force-stamps mesh bodies by type, so multi-body mesh imports are covered regardless.)
    return CompileOutput(bodies=new_bodies, tool_body=None, datum=None, resolved={},
                         kind="mesh" if has_mesh else "solid")
