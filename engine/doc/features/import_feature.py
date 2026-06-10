"""``import`` feature compiler (feature_params.md §Escape hatches / import).

Params
------
* ``source_path: str``   — absolute or repo-relative path to the source geometry file.
* ``format: "step"|"iges"|"brep"|"stl"``   — explicit format (auto-detected from extension
  when absent from the params dict).
* ``original_name: str?``   — display name for the feature; overrides the file stem.

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
    # Skip mesh proxies (3MF / raw-face-mesh shapes that have no OCCT topology) — the
    # tessellation layer (session._write_aicm) cannot handle them.  Feature_params.md lists
    # "step|iges|brep|stl" as supported; STL comes back as a build123d Face (has .wrapped);
    # 3MF is export-only for the import feature type.
    new_bodies = list(ctx.bodies)
    skipped = 0
    for _name, shape in body_pairs:
        if shape is None:
            skipped += 1
            continue
        wrapped = getattr(shape, "wrapped", "sentinel")
        if wrapped is None:
            # Mesh proxy — skip with a count so we can warn.
            skipped += 1
            continue
        new_bodies.append(shape)

    if not new_bodies and skipped:
        raise FeatureError(
            f"import: all {skipped} imported bodies are mesh-only (no OCCT solid); "
            "use a STEP, BREP, or IGES source for solid import.",
            hint="3MF and raw-STL mesh sources cannot be used as import feature bodies.",
        )

    return CompileOutput(bodies=new_bodies, tool_body=None, datum=None, resolved={})
