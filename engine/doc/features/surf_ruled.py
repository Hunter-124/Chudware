"""Ruled SURFACE feature compiler — the linear-blend strip between two rail curves.

``surf_ruled`` is the surfacing feature that builds the simplest of all blended surfaces: the
straight-line ruled surface spanning two rails (SolidWorks "Ruled Surface" → "Tapered to vector"
analogue, here in its two-edge "between two curves" form). Each ruling is a straight segment
joining a point on rail A to the matching point on rail B; the swept family of rulings is the
surface. Where ``surf_loft`` blends a *family* of section loops, a ruled surface blends just two
open rails into a single un-capped strip.

Refs
----
- ``refs.edge_a`` / ``refs.edge_b`` — each a single rail curve, resolved exactly like a loft
  guide/centerline via ``loft._resolve_curve_wire`` (a sketch-region selector → the sketch's raw
  edge chain lifted into world space, or a model-edge selector → an edge on the running body).
  Both are required.

Geometry strategy
-----------------
- **Both rails are a single edge** → ``BRepFill.Face_s(edge_a, edge_b)`` — OCCT's direct
  "ruled surface between two edges" primitive, yielding one ruled ``Face``. This is the exact,
  un-approximated linear blend and the common case (a line/arc rail pair).
- **Either rail is a multi-edge wire** → ``BRepOffsetAPI_ThruSections(isSolid=False,
  ruled=True)`` over the two wires, giving the open ruled ``Shell`` that spans the whole rails.
  ThruSections with the ruled flag is precisely the multi-edge generalization of ``Face_s``.

The result is an OPEN sheet body (a ``Face`` or ``Shell`` with free edges along both rails).
It is tagged ``kind="surface"`` so the core stamps ``BodyOut.kind == "surface"``: tessellation
emits a two-sided mesh and ``mass_props`` reports AREA (``is_surface`` True, ``volume_mm3``≈0)
instead of volume.

Surfaces never fuse into the running solid — the strip is appended as a NEW body, mirroring
``surf_planar`` / ``surf_loft``.
"""

from __future__ import annotations

from typing import Any

from build123d import Face, Shell, Wire
from OCP.BRepFill import BRepFill
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _store_topo_owners, _to_solid_normalized
from .loft import _resolve_curve_wire


def _single_edge(wire: Wire) -> Any | None:
    """Return the lone TopoDS_Edge of a single-edge wire, else None (multi-edge rail)."""
    try:
        edges = wire.edges()
    except Exception:  # noqa: BLE001
        return None
    if len(edges) == 1:
        return edges[0].wrapped
    return None


def _build_ruled_face(edge_a, edge_b) -> Face:
    """The exact ruled surface between two single edges (OCCT ``BRepFill.Face_s``)."""
    face = BRepFill.Face_s(edge_a, edge_b)
    if face is None:
        raise FeatureError("surf_ruled: BRepFill produced no ruled face")
    return Face(face)


def _build_ruled_shell(wire_a: Wire, wire_b: Wire) -> Shell:
    """The ruled surface between two multi-edge rails via a ruled ThruSections (open shell)."""
    builder = BRepOffsetAPI_ThruSections(False, True, 1e-6)  # isSolid=False, ruled=True
    builder.AddWire(wire_a.wrapped)
    builder.AddWire(wire_b.wrapped)
    builder.Build()
    if not builder.IsDone():
        raise FeatureError("surf_ruled: OCCT ruled ThruSections failed (IsDone=False)")
    return Shell(builder.Shape())


@register("surf_ruled")
def compile_surf_ruled(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    ref_a = refs.get("edge_a")
    ref_b = refs.get("edge_b")
    if ref_a is None or ref_b is None:
        raise FeatureError("surf_ruled: refs.edge_a and refs.edge_b are both required")

    # Resolve each rail to a world-space wire (same machinery as a loft guide/centerline).
    dangling: list[str] = []
    wire_a: Wire | None = None
    wire_b: Wire | None = None
    try:
        wire_a = _resolve_curve_wire(ref_a, ctx, "edge_a")
    except FeatureError as exc:
        dangling.extend(exc.dangling_refs or [f"edge_a: {exc}"])
    try:
        wire_b = _resolve_curve_wire(ref_b, ctx, "edge_b")
    except FeatureError as exc:
        dangling.extend(exc.dangling_refs or [f"edge_b: {exc}"])
    if dangling:
        raise FeatureError(
            f"surf_ruled: {len(dangling)} rail selector(s) failed", dangling_refs=dangling
        )
    assert wire_a is not None and wire_b is not None  # for type-checkers

    edge_a = _single_edge(wire_a)
    edge_b = _single_edge(wire_b)

    try:
        if edge_a is not None and edge_b is not None:
            # Common case: two single-edge rails → the exact OCCT ruled face.
            surface: Any = _build_ruled_face(edge_a, edge_b)
            mode = "face"
        else:
            # At least one multi-edge rail → ruled ThruSections over the two wires (open shell).
            surface = _build_ruled_shell(wire_a, wire_b)
            mode = "shell"
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_ruled: build failed: {exc}",
            hint="the two rails must be open curves that can be spanned by straight rulings; "
            "keep their parameterization running the same direction",
        )

    surface = _to_solid_normalized(surface)
    if getattr(surface, "wrapped", None) is None:
        raise FeatureError("surf_ruled: builder produced no surface geometry")

    # Surfaces never fuse into the running solid — append as a NEW surface body.
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)
    return CompileOutput(bodies=bodies, kind="surface", resolved={"_ruled_kind": mode})
