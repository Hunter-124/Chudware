"""Loft feature compiler — boss/cut, a solid blended through 2+ profile sections.

feature_params.md §Sketched features > loft.

Sections: ``refs.sections`` = an ordered list of region selectors (one per profile). Each
section resolves like an extrude profile (``_resolve_profile_faces``); we take that section's
first material face and loft through its outer wire. Order matters — the solid is blended in
list order.

``params.ruled`` (bool) selects straight (ruled) transitions vs a smooth blend; ``params.mode``
is boss/cut, ``params.merge`` controls union with the running body.

Built with OCCT ``BRepOffsetAPI_ThruSections`` (isSolid=True), mirroring revolve/sweep's
direct-OCCT approach for robustness with raw wires.
"""

from __future__ import annotations

from typing import Any

from build123d import Solid
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _resolve_profile_faces, _store_topo_owners, _to_solid_normalized


@register("loft")
def compile_loft(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    merge = p.get("merge", True)
    ruled = bool(p.get("ruled", False))
    fid = feat.get("id", "?")

    section_sels = refs.get("sections") or []
    if len(section_sels) < 2:
        raise FeatureError("loft: refs.sections needs at least 2 profiles")

    section_wires = []
    dangling: list[str] = []
    for i, sel in enumerate(section_sels):
        try:
            faces = _resolve_profile_faces(sel, ctx)
            if not faces:
                dangling.append(f"sections[{i}]: resolved no region")
                continue
            section_wires.append(faces[0].outer_wire())
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"sections[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"sections[{i}]: {exc}")
    if dangling:
        raise FeatureError(f"loft: {len(dangling)} section selector(s) failed",
                           dangling_refs=dangling)
    if len(section_wires) < 2:
        raise FeatureError("loft: fewer than 2 sections resolved")

    try:
        builder = BRepOffsetAPI_ThruSections(True, ruled, 1e-6)
        for w in section_wires:
            builder.AddWire(w.wrapped)
        builder.Build()
        if not builder.IsDone():
            raise FeatureError("loft: OCCT ThruSections failed (IsDone=False)")
        tool_solid = _to_solid_normalized(Solid(builder.Shape()).clean())
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"loft: build failed: {exc}",
            hint="sections must be closed profiles ordered along the loft direction",
        )

    bodies = list(ctx.bodies)
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("loft: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        _store_topo_owners(ctx, fid, tool_solid, None)
        return CompileOutput(bodies=bodies, tool_body=tool_solid, op="add", resolved={})

    target = bodies[-1]
    try:
        result = (target - tool_solid) if mode == "cut" else (target + tool_solid)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"loft: boolean {mode} failed: {exc}",
                           hint="the lofted tool may not intersect the body correctly")
    result = _to_solid_normalized(result)

    if mode == "cut":
        if abs(getattr(result, "volume", 0.0) - getattr(target, "volume", 0.0)) < 1e-9:
            raise FeatureError("loft cut removed no material",
                               hint="the lofted tool does not intersect the target body")

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(bodies=bodies, tool_body=tool_solid,
                         op=("cut" if mode == "cut" else "add"), resolved={})
