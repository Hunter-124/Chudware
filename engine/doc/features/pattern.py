"""Pattern + mirror feature compilers — linear_pattern / circular_pattern / mirror.

feature_params.md §Patterns. These features REPLICATE one or more upstream "seed" features
(``refs.features``): each seed contributed a ``tool_body`` (the solid it fused or cut) and an
``op`` ("add"/"cut"), both threaded into the :class:`CompileContext` by the compiler core. A
pattern lays down copies of those tool bodies at the instance transforms, re-applying the SAME
boolean — so a patterned boss fuses N times and a patterned cut subtracts N times, exactly like
SolidWorks.

Instance 0 is the seed already baked into the running body, so we only place the *additional*
instances (k = 1..count-1, the full grid minus the origin cell).

Direction / axis / plane resolution reuses the datum + selector machinery (datum axis/plane,
linear model edge, planar model face). When ``refs.features`` is empty, mirror falls back to a
whole-body mirror (SolidWorks "Mirror bodies").
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Axis, Plane, Vector

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_edges, describe_faces, resolve
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized
from .revolve import _resolve_axis_ref


# --------------------------------------------------------------------------- ref resolution


def _unit(v: Vector) -> Vector:
    ln = v.length
    if ln < 1e-12:
        raise FeatureError("pattern: direction reference has zero length")
    return v * (1.0 / ln)


def _resolve_direction(ref: Any, ctx: CompileContext) -> Vector:
    """Resolve a direction ref to a unit Vector.

    Accepts a reserved/datum axis ("axis.x"), a datum plane ("plane.front" → its normal), a
    datum feature, a linear model edge, or a planar model face (→ its normal).
    """
    if ref is None:
        raise FeatureError("pattern: missing direction reference")

    # Datum (reserved string or {"kind":"datum",...}).
    if isinstance(ref, str) or (isinstance(ref, dict) and ref.get("kind") == "datum"):
        d = ctx.datum(ref)
        if d.kind == "axis":
            return _unit(Vector(d.obj.direction))
        if d.kind in ("plane", "csys"):
            return _unit(Vector(d.obj.z_dir))
        raise FeatureError(f"pattern: datum '{ref}' (kind {d.kind}) is not a direction")

    bodies = ctx.bodies
    if not bodies:
        raise FeatureError("pattern: geometry direction ref but no bodies exist")
    body = bodies[-1]
    diag = _body_bbox_diag(body)

    if isinstance(ref, dict) and ref.get("kind") == "edge":
        rr = resolve(ref, describe_edges(body.wrapped), diag)
        if rr.index is None:
            raise FeatureError(f"pattern: edge direction dangling — {rr.dangling_reason}",
                               dangling_refs=[f"dir: {rr.dangling_reason}"])
        edge = body.edges()[rr.index]
        verts = edge.vertices()
        if len(verts) < 2:
            raise FeatureError("pattern: edge has no direction (closed curve?)")
        p1 = Vector(verts[0].X, verts[0].Y, verts[0].Z)
        p2 = Vector(verts[1].X, verts[1].Y, verts[1].Z)
        return _unit(p2 - p1)

    if isinstance(ref, dict) and ref.get("kind") == "face":
        rr = resolve(ref, describe_faces(body.wrapped), diag)
        if rr.index is None:
            raise FeatureError(f"pattern: face direction dangling — {rr.dangling_reason}",
                               dangling_refs=[f"dir: {rr.dangling_reason}"])
        face = body.faces()[rr.index]
        try:
            return _unit(Vector(face.normal_at()))
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"pattern: could not take face normal: {exc}")

    raise FeatureError(f"pattern: unsupported direction reference {ref!r}")


def _resolve_plane(ref: Any, ctx: CompileContext) -> Plane:
    """Resolve a mirror plane ref to a build123d Plane (datum plane/csys or planar model face)."""
    if ref is None:
        raise FeatureError("mirror: refs.plane is required")
    if isinstance(ref, str) or (isinstance(ref, dict) and ref.get("kind") == "datum"):
        d = ctx.datum(ref)
        if d.kind in ("plane", "csys"):
            return d.obj
        raise FeatureError(f"mirror: datum '{ref}' (kind {d.kind}) is not a plane")

    bodies = ctx.bodies
    if not bodies:
        raise FeatureError("mirror: face plane ref but no bodies exist")
    body = bodies[-1]
    if isinstance(ref, dict) and ref.get("kind") == "face":
        rr = resolve(ref, describe_faces(body.wrapped), _body_bbox_diag(body))
        if rr.index is None:
            raise FeatureError(f"mirror: face plane dangling — {rr.dangling_reason}",
                               dangling_refs=[f"plane: {rr.dangling_reason}"])
        face = body.faces()[rr.index]
        try:
            return Plane(origin=Vector(face.center()), z_dir=Vector(face.normal_at()))
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"mirror: face is not planar enough to mirror about: {exc}")

    raise FeatureError(f"mirror: unsupported plane reference {ref!r}")


def _feature_id(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("feature") or item.get("id")
    return None


def _gather_seeds(refs: dict, ctx: CompileContext, what: str) -> list[tuple[Any, str]]:
    """Resolve refs.features → list of (tool_body, op). Raises on dangling feature ids."""
    items = refs.get("features") or []
    seeds: list[tuple[Any, str]] = []
    dangling: list[str] = []
    for it in items:
        fid = _feature_id(it)
        if fid is None:
            dangling.append(f"{what}: bad feature ref {it!r}")
            continue
        tb = ctx.tool_bodies.get(fid)
        if tb is None:
            dangling.append(f"{what}: feature '{fid}' has no patternable geometry "
                            "(must be an earlier boss/cut/hole, and listed before this feature)")
            continue
        seeds.append((tb, ctx.feature_ops.get(fid, "add")))
    if dangling:
        raise FeatureError(f"{what}: {len(dangling)} feature reference(s) failed",
                           dangling_refs=dangling)
    return seeds


def _apply(target, tool, op):
    return (target - tool) if op == "cut" else (target + tool)


# --------------------------------------------------------------------------- linear pattern


@register("linear_pattern")
def compile_linear_pattern(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    count1 = max(1, int(round(ctx.dim(p, "count1", 2))))
    spacing1 = ctx.dim(p, "spacing1", 10.0)
    has_dir2 = refs.get("dir2") is not None
    count2 = max(1, int(round(ctx.dim(p, "count2", 1)))) if p.get("count2") is not None else 1
    spacing2 = ctx.dim(p, "spacing2", 10.0) if p.get("spacing2") is not None else 0.0
    if not has_dir2:
        count2 = 1

    if count1 < 2 and count2 < 2:
        raise FeatureError("linear pattern: need at least 2 instances")

    dir1 = _resolve_direction(refs.get("dir1"), ctx)
    dir2 = _resolve_direction(refs.get("dir2"), ctx) if has_dir2 else None
    seeds = _gather_seeds(refs, ctx, "linear pattern")
    if not seeds:
        raise FeatureError("linear pattern: refs.features is required")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("linear pattern: no body to pattern")
    target = bodies[-1]
    body = target
    placed = 0
    for i in range(count1):
        for j in range(count2):
            if i == 0 and j == 0:
                continue  # seed instance is already in the body
            offset = dir1 * (i * spacing1)
            if dir2 is not None:
                offset = offset + dir2 * (j * spacing2)
            for tool, op in seeds:
                try:
                    moved = tool.translate(offset)
                    body = _apply(body, moved, op)
                except Exception as exc:  # noqa: BLE001
                    raise FeatureError(f"linear pattern: instance ({i},{j}) failed: {exc}")
                placed += 1

    body = _to_solid_normalized(body)
    bodies[-1] = body
    _store_topo_owners(ctx, fid, body, target)
    return CompileOutput(bodies=bodies, tool_body=body, op="add",
                         resolved={"count1": float(count1), "spacing1": spacing1,
                                   "count2": float(count2)})


# --------------------------------------------------------------------------- circular pattern


@register("circular_pattern")
def compile_circular_pattern(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    count = max(1, int(round(ctx.dim(p, "count", 4))))
    angle_total = ctx.dim(p, "angle", 360.0)
    equal_spacing = bool(p.get("equal_spacing", True))
    if count < 2:
        raise FeatureError("circular pattern: need at least 2 instances")

    axis = _resolve_axis_ref(refs.get("axis"), ctx, None)
    seeds = _gather_seeds(refs, ctx, "circular pattern")
    if not seeds:
        raise FeatureError("circular pattern: refs.features is required")

    # equal_spacing: instances fill the total angle (step = angle/count → a full 360 wraps
    # cleanly). Otherwise `angle` is the increment BETWEEN instances.
    step = (angle_total / count) if equal_spacing else angle_total

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("circular pattern: no body to pattern")
    target = bodies[-1]
    body = target
    for k in range(1, count):
        ang = step * k
        for tool, op in seeds:
            try:
                rotated = tool.rotate(axis, ang)
                body = _apply(body, rotated, op)
            except Exception as exc:  # noqa: BLE001
                raise FeatureError(f"circular pattern: instance {k} failed: {exc}")

    body = _to_solid_normalized(body)
    bodies[-1] = body
    _store_topo_owners(ctx, fid, body, target)
    return CompileOutput(bodies=bodies, tool_body=body, op="add",
                         resolved={"count": float(count), "angle": angle_total})


# --------------------------------------------------------------------------- mirror


@register("mirror")
def compile_mirror(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    plane = _resolve_plane(refs.get("plane"), ctx)

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("mirror: no body to mirror")
    target = bodies[-1]

    # Feature mirror (preferred): replicate each seed feature's tool body reflected across the
    # plane, re-applying its boolean. Falls back to a whole-body mirror when no features are named.
    feat_items = refs.get("features") or []
    if feat_items:
        seeds = _gather_seeds(refs, ctx, "mirror")
        body = target
        for tool, op in seeds:
            try:
                reflected = tool.mirror(plane)
                body = _apply(body, reflected, op)
            except Exception as exc:  # noqa: BLE001
                raise FeatureError(f"mirror: reflecting a feature failed: {exc}")
        body = _to_solid_normalized(body)
        bodies[-1] = body
        _store_topo_owners(ctx, fid, body, target)
        return CompileOutput(bodies=bodies, tool_body=body, op="add", resolved={})

    # Whole-body mirror: reflect the running body and union (SolidWorks "Mirror bodies").
    try:
        reflected = target.mirror(plane)
        body = _to_solid_normalized(target + reflected)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"mirror: whole-body mirror failed: {exc}")
    bodies[-1] = body
    _store_topo_owners(ctx, fid, body, target)
    return CompileOutput(bodies=bodies, tool_body=body, op="add", resolved={})
