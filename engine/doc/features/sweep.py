"""Sweep feature compiler — boss/cut, profile region(s) swept along a path.

feature_params.md §Sketched features > sweep.

Profile source: ``refs.profile`` = region selector(s), resolved exactly like extrude/revolve
(``_resolve_profile_faces``). Whole-sketch profiles sweep their material regions.

Path source: ``refs.path`` — one of
- ``{"kind":"region","sketch":fid}`` → a wire built from that sketch's edge chain (open or
  closed). The path sketch's own regions are irrelevant; we use its raw geometry as the spine.
- ``{"kind":"edge",...}`` → a model edge on the running body.

The solid is built with OCCT ``BRepOffsetAPI_MakePipe`` (profile face swept along the spine
wire), mirroring revolve's direct-OCCT approach for robustness with raw faces. Per-profile
pipes are unioned, then applied to the running body (boss = union, cut = subtract).
"""

from __future__ import annotations

from typing import Any

from build123d import Face, Solid, Wire
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_edges, resolve
from .extrude import (
    _body_bbox_diag,
    _resolve_profile_faces,
    _store_topo_owners,
    _to_solid_normalized,
)


def _resolve_path_wire(path_ref: Any, ctx: CompileContext) -> Wire:
    """Resolve refs.path to a build123d Wire (the sweep spine)."""
    if isinstance(path_ref, dict) and path_ref.get("kind") == "region":
        sid = path_ref.get("sketch")
        if not sid:
            raise FeatureError("sweep path: region selector missing 'sketch' field")
        store = ctx.datums.get("_sketch_regions")
        data = store.obj.get(sid) if store is not None else None
        if data is None:
            raise FeatureError(f"sweep path: sketch '{sid}' not compiled (wrong order?)")
        edges = data.get("all_edges") or []
        if not edges:
            raise FeatureError(f"sweep path: sketch '{sid}' has no path geometry")
        plane = data["plane"]
        lifted = [plane.location * e for e in edges]
        try:
            return Wire(lifted)
        except Exception:  # noqa: BLE001
            combined = Wire.combine(lifted)
            if not combined:
                raise FeatureError("sweep path: could not assemble a continuous wire")
            return combined[0]

    if isinstance(path_ref, dict) and path_ref.get("kind") == "edge":
        bodies = ctx.bodies
        if not bodies:
            raise FeatureError("sweep path: edge selector but no bodies exist")
        body = bodies[-1]
        infos = describe_edges(body.wrapped)
        rr = resolve(path_ref, infos, _body_bbox_diag(body))
        if rr.index is None:
            raise FeatureError(
                f"sweep path: edge selector dangling — {rr.dangling_reason}",
                dangling_refs=[f"path: {rr.dangling_reason}"],
            )
        return Wire([body.edges()[rr.index]])

    raise FeatureError(
        "sweep path: unsupported ref (expected a sketch region or a model edge)"
    )


@register("sweep")
def compile_sweep(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    merge = p.get("merge", True)
    fid = feat.get("id", "?")

    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("sweep: refs.profile is required")

    profile_faces: list[Face] = []
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
        raise FeatureError(f"sweep: {len(dangling)} profile selector(s) failed",
                           dangling_refs=dangling)
    if not profile_faces:
        raise FeatureError("sweep: no profiles resolved")

    path_ref = refs.get("path")
    if path_ref is None:
        raise FeatureError("sweep: refs.path is required")
    try:
        path_wire = _resolve_path_wire(path_ref, ctx)
    except FeatureError as exc:
        raise FeatureError(f"sweep: path resolution failed: {exc}",
                           dangling_refs=exc.dangling_refs)

    tool_bodies: list[Any] = []
    for face in profile_faces:
        try:
            mk = BRepOffsetAPI_MakePipe(path_wire.wrapped, face.wrapped)
            mk.Build()
            if not mk.IsDone():
                raise FeatureError("sweep: OCCT MakePipe failed (IsDone=False)")
            tool_bodies.append(Solid(mk.Shape()).clean())
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"sweep: build failed: {exc}",
                hint="the profile must be a closed region positioned at the path start",
            )

    tool_solid = tool_bodies[0]
    for tb in tool_bodies[1:]:
        tool_solid = tool_solid + tb
    tool_solid = _to_solid_normalized(tool_solid)

    bodies = list(ctx.bodies)
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("sweep: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        _store_topo_owners(ctx, fid, tool_solid, None)
        return CompileOutput(bodies=bodies, tool_body=tool_solid, op="add", resolved={})

    target = bodies[-1]
    try:
        result = (target - tool_solid) if mode == "cut" else (target + tool_solid)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sweep: boolean {mode} failed: {exc}",
                           hint="the swept tool may not intersect the body correctly")
    result = _to_solid_normalized(result)

    if mode == "cut":
        if abs(getattr(result, "volume", 0.0) - getattr(target, "volume", 0.0)) < 1e-9:
            raise FeatureError("sweep cut removed no material",
                               hint="the swept tool does not intersect the target body")

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(bodies=bodies, tool_body=tool_solid,
                         op=("cut" if mode == "cut" else "add"), resolved={})
