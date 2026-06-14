"""Fill-surface feature compiler — a patch surface spanning a bounded region.

``surf_fill`` is the surfacing "Fill Surface" command: it resolves a set of BOUNDING edges
(``refs.edges``) that trace a closed gap — model edges of a body (e.g. the opening left when a
face is removed) and/or the curve chain of a sketch region — and builds a single filled patch
FACE that spans that boundary. The patch is returned as an open SURFACE body
(``CompileOutput(..., kind="surface")``), so the core stamps ``BodyOut.kind == "surface"`` and
``mass_props`` reports AREA (``is_surface`` True, ``volume_mm3`` ≈ 0) instead of volume.

Refs
----
- ``refs.edges`` = a list of edge/region selectors describing the boundary loop. Each selector
  is resolved to one or more bounding edges and ALL of them are fed to the filler:
    * a ``region`` selector (``{"kind":"region","sketch":fid,...}``) → the sketch's raw curve
      chain lifted into world space (reuses ``loft._resolve_curve_wire``); its edges become
      boundary constraints.
    * an ``edge`` selector (``{"kind":"edge",...}``) → a model edge on the running body
      (reuses ``dress._resolve_edges``).
  Mixing the two kinds is allowed (e.g. three model edges of an opening plus one sketch curve
  bridging the gap).

Params
------
- ``continuity`` (optional) — ``g0`` (default) | ``g1``. ``g0`` constrains the patch to merely
  touch each boundary edge (``GeomAbs_C0``); ``g1`` asks the patch to leave each boundary
  tangentially (``GeomAbs_C1``) where the boundary edge is shared with an adjacent face. ``g1``
  is best-effort — when no owning face backs a boundary edge OCCT simply enforces position
  there, so ``g1`` degrades gracefully to ``g0`` on free (sketch-curve) boundaries.

Geometry strategy
-----------------
Built with OCCT ``BRepFill_Filling``: every boundary edge is ``Add``-ed with the requested
continuity, ``Build()`` solves the constrained patch, and ``Face()`` returns the result FACE.
There is no public way to force the patch through interior points here, so the patch is the
minimal-energy surface OCCT fits to the boundary — exactly SolidWorks' default Fill behaviour.

Surfaces never fuse into the running solid — the patch is appended as a NEW body, mirroring
``surf_planar`` / ``surf_loft``.
"""

from __future__ import annotations

from typing import Any

from build123d import Face
from OCP.BRepFill import BRepFill_Filling
from OCP.GeomAbs import GeomAbs_Shape

from .base import CompileContext, CompileOutput, FeatureError, register
from .dress import _bbox_diag, _describe_with_owners, _resolve_with_hint_fallback
from .extrude import _store_topo_owners
from .loft import _resolve_curve_wire


def _continuity_for(name: str) -> GeomAbs_Shape:
    """Map the ``continuity`` param to the OCCT boundary continuity order."""
    n = str(name or "g0").lower()
    if n in ("g1", "c1", "tangent", "tangency"):
        return GeomAbs_Shape.GeomAbs_C1
    if n in ("g2", "c2", "curvature"):
        return GeomAbs_Shape.GeomAbs_C2
    return GeomAbs_Shape.GeomAbs_C0


def _is_region_sel(sel: Any) -> bool:
    return isinstance(sel, dict) and sel.get("kind") == "region"


def _is_edge_sel(sel: Any) -> bool:
    return isinstance(sel, dict) and sel.get("kind") == "edge"


def _resolve_model_edges(
    sel_list: list[dict[str, Any]], body, ctx: CompileContext
) -> tuple[list, list[str]]:
    """Resolve a list of model-edge selectors against *body* → unique build123d edges.

    Mirrors ``dress._resolve_edges`` but de-duplicates like ``dress._resolve_vertices``:
    ``describe_edges`` over a solid enumerates one descriptor per incident face (24 for a box,
    vs. 12 unique ``body.edges()``), so the resolved describe-index space is wider than the
    unique edge list. We therefore map each resolved descriptor back to the NEAREST unique
    ``body.edges()`` edge by centroid — robust to the index-space mismatch (the plain
    ``edge_objs[idx]`` indexing in ``dress._resolve_edges`` IndexErrors on high-index edges
    such as a box's top rim). Returns ``(edges, dangling_locators)``.
    """
    infos = _describe_with_owners(body.wrapped, "edge", ctx)
    diag = _bbox_diag(body)
    edge_objs = body.edges()

    resolved: list = []
    dangling: list[str] = []
    for i, sel in enumerate(sel_list):
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            from engine.doc.selectors import resolve
            rr = resolve(sel, infos, diag)
            dangling.append(f"edges[{i}]: {rr.dangling_reason}")
            continue
        target = infos[idx].centroid
        nearest = min(
            edge_objs,
            key=lambda e: sum((c - t) ** 2 for c, t in zip(
                (e.center().X, e.center().Y, e.center().Z), target)),
        )
        resolved.append(nearest)
    return resolved, dangling


@register("surf_fill")
def compile_surf_fill(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    continuity = _continuity_for(p.get("continuity", "g0"))

    edge_sels = refs.get("edges") or []
    if not edge_sels:
        raise FeatureError("surf_fill: refs.edges is required and must be non-empty")

    # Resolve every boundary selector to concrete bounding edges. Edge selectors resolve against
    # the running body (the opening's model edges); region selectors resolve to a sketch curve
    # chain lifted into world space. Both kinds may appear in one boundary loop.
    boundary_edges: list = []
    dangling: list[str] = []

    # Pull the model-edge selectors out and resolve them as a batch against the running body
    # (so dress._resolve_edges' hint-fallback de-duplication works across identical box edges).
    edge_indices = [i for i, s in enumerate(edge_sels) if _is_edge_sel(s)]
    if edge_indices:
        if not ctx.bodies:
            raise FeatureError(
                "surf_fill: an edge selector needs an existing body to resolve against",
                hint="build the body whose opening you are filling before surf_fill",
            )
        body = ctx.bodies[-1]
        model_sels = [edge_sels[i] for i in edge_indices]
        resolved, edge_dangling = _resolve_model_edges(model_sels, body, ctx)
        boundary_edges.extend(resolved)
        dangling.extend(edge_dangling)

    for i, sel in enumerate(edge_sels):
        if _is_region_sel(sel):
            try:
                wire = _resolve_curve_wire(sel, ctx, f"edges[{i}]")
                boundary_edges.extend(wire.edges())
            except FeatureError as exc:
                if exc.dangling_refs:
                    dangling.extend([f"edges[{i}]: " + dr for dr in exc.dangling_refs])
                else:
                    dangling.append(f"edges[{i}]: {exc}")
        elif not _is_edge_sel(sel):
            dangling.append(
                f"edges[{i}]: unsupported selector kind "
                f"{sel.get('kind') if isinstance(sel, dict) else type(sel).__name__!r} "
                "(expected a model edge or a sketch region)"
            )

    if dangling:
        raise FeatureError(
            f"surf_fill: {len(dangling)} boundary selector(s) failed",
            dangling_refs=dangling,
        )

    # De-duplicate boundary edges by geometry (center + length): a model edge can be picked twice
    # when its describe-descriptor appears once per incident face, and feeding the same edge to
    # the filler twice over-constrains the patch.
    unique_edges: list = []
    seen_keys: set = set()
    for e in boundary_edges:
        try:
            c = e.center()
            key = (round(c.X, 5), round(c.Y, 5), round(c.Z, 5), round(float(e.length), 5))
        except Exception:  # noqa: BLE001
            key = (id(e),)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_edges.append(e)
    boundary_edges = unique_edges

    if len(boundary_edges) < 3:
        raise FeatureError(
            f"surf_fill: need at least 3 bounding edges to fill a region "
            f"(resolved {len(boundary_edges)})",
            hint="the boundary edges must trace a single closed gap",
        )

    # Build the filled patch over the bounded region.
    try:
        mk = BRepFill_Filling()
        for e in boundary_edges:
            mk.Add(e.wrapped, continuity)
        mk.Build()
        if not mk.IsDone():
            raise FeatureError(
                "surf_fill: OCCT BRepFill_Filling failed (IsDone=False)",
                hint="the bounding edges must form a single closed loop with no gaps",
            )
        patch = Face(mk.Face())
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_fill: fill build failed: {exc}",
            hint="the bounding edges must trace one closed boundary; "
            "drop g1 to g0 if a free boundary cannot be made tangent",
        )

    if getattr(patch, "wrapped", None) is None or patch.area <= 1e-9:
        raise FeatureError("surf_fill: filler produced an empty patch")

    resolved_echo: dict[str, float | str] = {
        "_continuity": str(p.get("continuity", "g0") or "g0").lower(),
        "boundary_edges": float(len(boundary_edges)),
    }

    # Surfaces never fuse into the running solid — append the patch as a NEW surface body.
    bodies = list(ctx.bodies)
    bodies.append(patch)
    _store_topo_owners(ctx, fid, patch, None)
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved_echo)
