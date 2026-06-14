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

SolidWorks-parity options (docs/SW_PARITY_SPEC.md §2 / docs/SW_PARITY_GAPS.md), all additive —
output is byte-identical to the original behaviour when none are set:

- **Instance skip** (``params.skip``): omit chosen instances. Linear pattern accepts grid cells
  as ``[i, j]`` pairs (``i`` along dir1, ``j`` along dir2) or, for a 1-D row, a bare linear index
  ``i`` (treated as ``[i, 0]``). Circular pattern accepts bare ring indices ``k`` (0-based,
  0 = seed). Skipping the seed cell ``[0, 0]`` / ``0`` is ignored (it is already in the body).

- **Vary instance** (``params.vary``, linear only): incrementally change spacing and/or instance
  size per step, like SolidWorks' "Vary sketch / vary instances". Keys (each a dim ``{value}`` or
  a bare number):
    * ``spacing1`` / ``spacing2`` — ADD this delta to the step along dir1/dir2 once PER STEP, so
      column ``i`` sits at ``i*spacing1 + (i*(i-1)/2)*delta`` (accumulating gaps).
    * ``scale`` — multiply instance size by ``(1 + (i+j)*scale)`` about the instance's own centre,
      so later instances grow (or shrink, for a negative delta) without moving.

- **Bidirectional** (``params.bidirectional``, circular only): also lay instances on the OTHER
  side of the seed (clockwise). ``params.count_b`` / ``params.angle_b`` give the second-direction
  instance count / total angle (default: mirror direction 1). Direction-1 instances are unchanged.

- **Mirror bodies + merge** (mirror): ``refs.bodies`` mirrors whole picked bodies (or, when given
  without ``refs.features``, the running body) across the plane. ``params.merge`` (default True)
  unions each reflected body into the running solid; ``merge:false`` appends each reflection as a
  SEPARATE body (SolidWorks "Merge solids" unticked), growing the body list.

- **Geometry vs feature pattern** (``params.geometry_pattern``): the flag is accepted and echoed
  in ``resolved``. The implemented strategy is the GEOMETRY pattern (rigid transformed copies of
  the seed tool body) — fast and robust; the feature (re-solve-per-instance) strategy is reserved
  for a later wave. ``geometry_pattern:false`` currently falls back to the geometry pattern with
  the flag recorded, so a model authored either way still rebuilds.
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


# --------------------------------------------------------------------------- option helpers


def _num(v: Any, default: float = 0.0) -> float:
    """Read a plain number or a dim object ``{"value": float}`` to a float.

    ``params.vary`` sub-values are NOT walked by the compiler core's dim evaluator (it only
    resolves top-level params), so a vary delta may arrive as a raw number or an unevaluated dim
    dict — accept both. Non-numeric → ``default``.
    """
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v)


def _parse_linear_skip(raw: Any) -> set[tuple[int, int]]:
    """Parse ``params.skip`` into a set of (i, j) grid cells to omit.

    Accepts a list whose items are either ``[i, j]`` pairs or bare linear indices ``i`` (→
    ``(i, 0)``). Non-list / malformed input → empty set (no skips), so a bad value degrades to the
    original full grid rather than raising.
    """
    cells: set[tuple[int, int]] = set()
    if not isinstance(raw, (list, tuple)):
        return cells
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                cells.add((int(round(float(item[0]))), int(round(float(item[1])))))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            cells.add((int(round(float(item))), 0))
    return cells


def _parse_circular_skip(raw: Any) -> set[int]:
    """Parse ``params.skip`` into a set of ring instance indices (0-based, 0 = seed) to omit."""
    out: set[int] = set()
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, (list, tuple)) and item:
            item = item[0]
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            out.add(int(round(float(item))))
    return out


def _scale_about_center(tool, factor: float):
    """Uniformly scale a tool body by ``factor`` about ITS OWN centroid (position preserved).

    ``Shape.scale`` scales about the global origin, so we translate the centroid to the origin,
    scale, then translate back. ``factor`` ~1.0 (or non-positive) → return the tool unchanged.
    """
    if factor <= 0.0 or abs(factor - 1.0) < 1e-12:
        return tool
    c = tool.bounding_box().center()
    back = Vector(c.X, c.Y, c.Z)
    return tool.translate(Vector(-c.X, -c.Y, -c.Z)).scale(factor).translate(back)


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

    # SW-parity options. ``skip`` omits grid cells; ``vary`` accumulates a per-step spacing delta
    # and/or a per-step size scale; ``geometry_pattern`` selects the (currently only) strategy.
    skip = _parse_linear_skip(p.get("skip"))
    vary = p.get("vary") if isinstance(p.get("vary"), dict) else {}
    vary_s1 = _num(vary.get("spacing1"), 0.0)
    vary_s2 = _num(vary.get("spacing2"), 0.0)
    vary_scale = _num(vary.get("scale"), 0.0)
    geometry_pattern = bool(p.get("geometry_pattern", True))

    # Accumulated position along each direction for step n:
    #   pos(n) = n*spacing + (n*(n-1)/2)*delta   (delta added once per step → growing gaps).
    def _pos(n: int, spacing: float, delta: float) -> float:
        return n * spacing + (n * (n - 1) / 2.0) * delta

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("linear pattern: no body to pattern")
    target = bodies[-1]
    body = target
    placed = 0
    skipped = 0
    for i in range(count1):
        for j in range(count2):
            if i == 0 and j == 0:
                continue  # seed instance is already in the body
            if (i, j) in skip:
                skipped += 1
                continue
            offset = dir1 * _pos(i, spacing1, vary_s1)
            if dir2 is not None:
                offset = offset + dir2 * _pos(j, spacing2, vary_s2)
            # Per-instance size factor (1 + (i+j)*scale): later instances grow/shrink in place.
            factor = 1.0 + (i + j) * vary_scale if vary_scale != 0.0 else 1.0
            for tool, op in seeds:
                try:
                    moved = _scale_about_center(tool, factor)
                    moved = moved.translate(offset)
                    body = _apply(body, moved, op)
                except Exception as exc:  # noqa: BLE001
                    raise FeatureError(f"linear pattern: instance ({i},{j}) failed: {exc}")
                placed += 1

    body = _to_solid_normalized(body)
    bodies[-1] = body
    _store_topo_owners(ctx, fid, body, target)
    resolved = {"count1": float(count1), "spacing1": spacing1,
                "count2": float(count2), "_instances_placed": float(placed),
                "_instances_skipped": float(skipped),
                "geometry_pattern": 1.0 if geometry_pattern else 0.0}
    if vary_s1 or vary_s2 or vary_scale:
        resolved["_vary"] = 1.0
    return CompileOutput(bodies=bodies, tool_body=body, op="add", resolved=resolved)


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

    # SW-parity options.
    skip = _parse_circular_skip(p.get("skip"))
    bidirectional = bool(p.get("bidirectional", False))
    geometry_pattern = bool(p.get("geometry_pattern", True))
    # Second direction (clockwise) instance count / total angle; default mirrors direction 1.
    count_b = (max(1, int(round(ctx.dim(p, "count_b", count))))
               if p.get("count_b") is not None else count)
    angle_b = ctx.dim(p, "angle_b", angle_total) if p.get("angle_b") is not None else angle_total

    # equal_spacing: instances fill the total angle (step = angle/count → a full 360 wraps
    # cleanly). Otherwise `angle` is the increment BETWEEN instances.
    step = (angle_total / count) if equal_spacing else angle_total
    step_b = (angle_b / count_b) if equal_spacing else angle_b

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("circular pattern: no body to pattern")
    target = bodies[-1]
    body = target
    placed = 0
    skipped = 0

    def _place(ang: float, sign: int, k: int):
        nonlocal body, placed
        for tool, op in seeds:
            try:
                rotated = tool.rotate(axis, sign * ang)
                body = _apply(body, rotated, op)
            except Exception as exc:  # noqa: BLE001
                raise FeatureError(
                    f"circular pattern: instance {'-' if sign < 0 else ''}{k} failed: {exc}")
            placed += 1

    # Direction 1 (CCW): k = 1..count-1. k=0 is the seed (already in the body).
    for k in range(1, count):
        if k in skip:
            skipped += 1
            continue
        _place(step * k, +1, k)

    # Direction 2 (CW), bidirectional only: a fresh ring of count_b-1 instances on the other side.
    # Skip indices apply per-direction by NEGATING (i.e. skip -1 omits the first CW instance).
    if bidirectional:
        for k in range(1, count_b):
            if -k in skip:
                skipped += 1
                continue
            _place(step_b * k, -1, k)

    body = _to_solid_normalized(body)
    bodies[-1] = body
    _store_topo_owners(ctx, fid, body, target)
    resolved = {"count": float(count), "angle": angle_total,
                "_instances_placed": float(placed), "_instances_skipped": float(skipped),
                "geometry_pattern": 1.0 if geometry_pattern else 0.0}
    if bidirectional:
        resolved["count_b"] = float(count_b)
        resolved["angle_b"] = angle_b
    return CompileOutput(bodies=bodies, tool_body=body, op="add", resolved=resolved)


# --------------------------------------------------------------------------- mirror


@register("mirror")
def compile_mirror(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    plane = _resolve_plane(refs.get("plane"), ctx)

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("mirror: no body to mirror")
    target = bodies[-1]

    # Feature mirror (preferred): replicate each seed feature's tool body reflected across the
    # plane, re-applying its boolean. Highest precedence: a feature mirror is what SolidWorks does
    # when you pick "Features to Mirror".
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

    # Bodies mirror (SolidWorks "Bodies to Mirror"): reflect each PICKED body across the plane.
    # ``params.merge`` (default True) unions every reflection into the running body; merge:false
    # appends each reflection as a SEPARATE new body.
    body_sels = refs.get("bodies")
    merge = bool(p.get("merge", True))
    if isinstance(body_sels, list) and body_sels:
        # Resolve the picked bodies to indices using the shared body-selector matcher.
        from .boolean import _resolve_body_set
        idxs = _resolve_body_set(body_sels, bodies, ctx, "bodies")
        reflections = []
        for i in idxs:
            try:
                reflections.append(bodies[i].mirror(plane))
            except Exception as exc:  # noqa: BLE001
                raise FeatureError(f"mirror: reflecting body index {i} failed: {exc}")
        if merge:
            body = target
            for reflected in reflections:
                body = _to_solid_normalized(body + reflected)
            bodies[-1] = body
            _store_topo_owners(ctx, fid, body, target)
            return CompileOutput(bodies=bodies, tool_body=body, op="add",
                                 resolved={"merge": 1.0, "_bodies_mirrored": float(len(idxs))})
        # merge=False: each reflection becomes its own new body.
        out_tool = None
        for reflected in reflections:
            solid = _to_solid_normalized(reflected)
            bodies.append(solid)
            out_tool = solid
        _store_topo_owners(ctx, fid, target, target)
        return CompileOutput(bodies=bodies, tool_body=out_tool, op="add",
                             resolved={"merge": 0.0, "_bodies_mirrored": float(len(idxs))})

    # Whole-body mirror fallback: reflect the running body. Default unions (SolidWorks
    # "Mirror bodies" with Merge on); ``merge:false`` keeps the reflection as a separate body.
    try:
        reflected = target.mirror(plane)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"mirror: whole-body mirror failed: {exc}")
    if merge:
        try:
            body = _to_solid_normalized(target + reflected)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"mirror: whole-body mirror union failed: {exc}")
        bodies[-1] = body
        _store_topo_owners(ctx, fid, body, target)
        return CompileOutput(bodies=bodies, tool_body=body, op="add", resolved={})
    solid = _to_solid_normalized(reflected)
    bodies.append(solid)
    _store_topo_owners(ctx, fid, target, target)
    return CompileOutput(bodies=bodies, tool_body=solid, op="add",
                         resolved={"merge": 0.0})
