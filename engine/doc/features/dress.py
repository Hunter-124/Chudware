"""Dress-up feature compilers: fillet / chamfer / shell.

feature_params.md §Dress-up.

Fillet and chamfer resolve edge selectors from the *current top body* using C03's
describe_edges / resolve API, then apply build123d's fillet/chamfer with auto-clamp
retry (halve ×3, floor 0.2 mm) mirroring the pattern in kernel._wrap_edge_op.

Shell resolves face selectors from the current top body and uses
``build123d.offset(solid, -thickness, openings=faces)``; OCCT failures surface the
"shell before fillet" guidance.

Dangling selector → FeatureError with dangling_refs list.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import fillet as b3d_fillet, chamfer as b3d_chamfer, offset

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import (
    describe_edges,
    describe_faces,
    resolve,
    TopoInfo,
)

# --------------------------------------------------------------------------- helpers


def _bbox_diag(shape) -> float:
    """Return the bounding-box diagonal of *shape* (used for selector normalisation)."""
    try:
        bb = shape.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        return d if d > 1e-12 else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _describe_with_owners(shape, topo_kind: str, ctx=None) -> list[TopoInfo]:
    """Describe faces or edges, then patch owner fields from ctx's stored topo owners.

    topo_kind: "face" or "edge"
    """
    from engine.doc.features.extrude import _get_topo_owners
    if topo_kind == "face":
        infos = describe_faces(shape)
    else:
        infos = describe_edges(shape)
    if ctx is not None:
        topo_owners = _get_topo_owners(ctx, topo_kind)
        for info in infos:
            owner = topo_owners.get(info.index + 1)  # convert 0-based to 1-based
            if owner is not None:
                info.owner = owner
    return infos


def _resolve_with_hint_fallback(
    sel: dict[str, Any],
    infos: list[TopoInfo],
    diag: float,
) -> int | None:
    """Resolve a selector, applying hint_index tiebreak fallback for margin failures.

    Per selectors.md §3, ties (margin <= 0.05) are broken by nearest hint_index when
    the best score is very low (< 0.01) — the reference is clearly correct, just
    geometrically ambiguous due to identical shapes (e.g. 4 identical box edges).
    """
    from engine.doc.selectors import _score_candidate
    rr = resolve(sel, infos, diag)
    if rr.index is not None:
        return rr.index

    # Apply hint_index fallback: only when margin failure and best score < 0.01
    if rr.dangling_reason and "margin" in rr.dangling_reason and rr.score < 0.01:
        kind = sel.get("kind")
        s_owner = sel.get("owner_feature")
        s_hint = sel.get("hint_index")
        candidates = [i for i in infos if i.kind == kind]
        if s_owner:
            owner_set = [i for i in candidates if i.owner == s_owner]
            if owner_set:
                candidates = owner_set
        scored = sorted(
            [(_score_candidate(sel, c, diag), c) for c in candidates],
            key=lambda t: (t[0], t[1].hint_index)
        )
        if scored and scored[0][0] < 0.01:
            # If hint_index is specified and matches the best after sort, use it
            best_cand = scored[0][1]
            if s_hint is None or best_cand.hint_index == s_hint:
                return best_cand.index
            # Try to find the candidate with the matching hint_index
            for _, c in scored:
                if c.hint_index == s_hint:
                    return c.index
            # Fall back to the overall best
            return best_cand.index
    return None


def _resolve_edges(
    sel_list: list[dict[str, Any]],
    body,
    slot_name: str,
    ctx=None,
) -> tuple[list, list[str]]:
    """Resolve a list of edge selectors against *body*.

    Returns ``(edge_objects, dangling_locators)``.
    """
    infos: list[TopoInfo] = _describe_with_owners(body.wrapped, "edge", ctx)
    diag = _bbox_diag(body)

    edge_objs = body.edges()  # build123d edge list (index-aligned with infos)
    resolved_edges: list = []
    dangling: list[str] = []

    for i, sel in enumerate(sel_list):
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            rr = resolve(sel, infos, diag)
            dangling.append(f"{slot_name}[{i}]: {rr.dangling_reason}")
        else:
            resolved_edges.append(edge_objs[idx])

    return resolved_edges, dangling


def _resolve_faces(
    sel_list: list[dict[str, Any]],
    body,
    slot_name: str,
    ctx=None,
) -> tuple[list, list[str]]:
    """Resolve a list of face selectors against *body*."""
    infos = _describe_with_owners(body.wrapped, "face", ctx)
    diag = _bbox_diag(body)

    face_objs = body.faces()
    resolved_faces: list = []
    dangling: list[str] = []

    for i, sel in enumerate(sel_list):
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            rr = resolve(sel, infos, diag)
            dangling.append(f"{slot_name}[{i}]: {rr.dangling_reason}")
        else:
            resolved_faces.append(face_objs[idx])

    return resolved_faces, dangling


# --------------------------------------------------------------------------- fillet


@register("fillet")
def compile_fillet(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    radius = ctx.dim(p, "radius", 2.0)
    if radius <= 0.0:
        raise FeatureError("fillet radius must be > 0")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("fillet: no body to apply to")
    body = bodies[-1]

    edge_sel_list = refs.get("edges", [])
    if not edge_sel_list:
        raise FeatureError("fillet: refs.edges is required and must be non-empty")

    edges, dangling = _resolve_edges(edge_sel_list, body, "edges", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"fillet: {len(dangling)} edge selector(s) dangling",
            dangling_refs=dangling,
        )
    if not edges:
        raise FeatureError("fillet: no edges resolved")

    warnings: list[str] = []
    result_body = None
    r = radius
    last_err: Exception | None = None

    # Auto-clamp retry: halve ×3, floor 0.2 mm
    for attempt in range(4):  # 0 = original, 1-3 = clamped
        if attempt > 0:
            r = r / 2.0
            if r < 0.2:
                break
        try:
            result_body = b3d_fillet(edges, radius=r)
            if attempt > 0:
                warnings.append(
                    f"fillet radius {radius:g} mm too large; auto-clamped to {r:g} mm"
                )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    if result_body is None:
        raise FeatureError(
            f"fillet failed (radius={radius:g} mm): {last_err}",
            hint="try a smaller radius; avoid overlapping fillets",
        )

    resolved: dict[str, float] = {"radius": r}
    if warnings:
        resolved["_warning"] = 1.0  # token so the warning text can ride in resolved

    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)


# --------------------------------------------------------------------------- chamfer


@register("chamfer")
def compile_chamfer(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    length = ctx.dim(p, "length", 1.0)
    if length <= 0.0:
        raise FeatureError("chamfer length must be > 0")

    # Optional asymmetric second distance. 0 / missing / equal to length → symmetric.
    length2_raw = ctx.dim(p, "length2", 0.0) if "length2" in p else 0.0
    asymmetric = length2_raw > 0.0 and abs(length2_raw - length) > 1e-9

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("chamfer: no body to apply to")
    body = bodies[-1]

    edge_sel_list = refs.get("edges", [])
    if not edge_sel_list:
        raise FeatureError("chamfer: refs.edges is required and must be non-empty")

    edges, dangling = _resolve_edges(edge_sel_list, body, "edges", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"chamfer: {len(dangling)} edge selector(s) dangling",
            dangling_refs=dangling,
        )
    if not edges:
        raise FeatureError("chamfer: no edges resolved")

    warnings: list[str] = []
    result_body = None
    d  = length
    d2 = length2_raw if asymmetric else None
    last_err: Exception | None = None

    for attempt in range(4):
        if attempt > 0:
            d = d / 2.0
            if d2 is not None:
                d2 = d2 / 2.0
            if d < 0.2:
                break
        try:
            if d2 is not None:
                result_body = b3d_chamfer(edges, length=d, length2=d2)
            else:
                result_body = b3d_chamfer(edges, length=d)
            if attempt > 0:
                warnings.append(
                    f"chamfer length {length:g} mm too large; auto-clamped to {d:g} mm"
                )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    if result_body is None:
        raise FeatureError(
            f"chamfer failed (length={length:g} mm): {last_err}",
            hint="try a smaller length",
        )

    resolved: dict[str, float] = {"length": d}
    if d2 is not None:
        resolved["length2"] = d2
    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved=resolved)


# --------------------------------------------------------------------------- shell


@register("shell")
def compile_shell(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    thickness = ctx.dim(p, "thickness", 2.0)
    outward = p.get("outward", False)
    if thickness <= 0.0:
        raise FeatureError("shell thickness must be > 0")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("shell: no body to apply to")
    body = bodies[-1]

    remove_sel_list = refs.get("remove") or []
    remove_faces: list = []
    dangling: list[str] = []

    if remove_sel_list:
        remove_faces, dangling = _resolve_faces(remove_sel_list, body, "remove", ctx=ctx)
        if dangling:
            raise FeatureError(
                f"shell: {len(dangling)} face selector(s) dangling",
                dangling_refs=dangling,
            )

    # build123d offset: positive = outward, negative = inward.
    # Contract says outward:false → hollow inward (default shell behavior).
    signed_t = thickness if outward else -thickness

    try:
        result_body = offset(body, amount=signed_t, openings=remove_faces if remove_faces else None)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"shell failed: {exc}",
            hint="shell before fillet often succeeds where after fails",
        )

    bodies[-1] = result_body
    return CompileOutput(bodies=bodies, tool_body=result_body, resolved={"thickness": thickness})
