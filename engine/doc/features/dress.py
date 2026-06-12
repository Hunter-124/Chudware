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

from build123d import fillet as b3d_fillet, chamfer as b3d_chamfer, offset, Solid, GeomType
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepAdaptor import BRepAdaptor_Curve

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


def _line_dir(edge) -> tuple[float, float, float]:
    """Unit direction of a straight build123d edge (raw OCP line adaptor)."""
    ad = BRepAdaptor_Curve(edge.wrapped)
    d = ad.Line().Direction()
    return (d.X(), d.Y(), d.Z())


def _analyze_middle_face(face):
    """Validate a full-round middle face and compute its constant width.

    v1 precondition (the rib-top case users want): the middle face is planar and
    has a pair of parallel, equal-length straight "long" edges. The full round is
    the tangent cylinder that replaces the strip between those two edges; its
    radius is width/2 where width = the perpendicular distance between the pair.

    Returns ``(width, [edge0, edge1])`` or raises FeatureError with a clear v1
    constraint message (see feature_params.md §full_round).
    """
    if face.geom_type != GeomType.PLANE:
        raise FeatureError(
            "full round requires a planar constant-width middle face (v1)"
        )
    lines = [e for e in face.edges() if e.geom_type == GeomType.LINE]
    if len(lines) < 2:
        raise FeatureError(
            "full round middle face needs two straight side edges (v1)"
        )
    lines.sort(key=lambda e: -e.length)
    e0 = lines[0]
    d0 = _line_dir(e0)
    mate = None
    for e in lines[1:]:
        d = _line_dir(e)
        if abs(abs(d[0] * d0[0] + d[1] * d0[1] + d[2] * d0[2]) - 1.0) < 1e-4:
            mate = e
            break
    if mate is None:
        raise FeatureError(
            "full round requires a planar constant-width middle face (v1): "
            "no parallel long-edge pair found"
        )
    if abs(e0.length - mate.length) > 1e-4 * max(e0.length, 1.0):
        raise FeatureError(
            "full round requires a planar constant-width middle face (v1): "
            "the two long edges are not equal length"
        )
    c0 = e0.center()
    c1 = mate.center()
    sep = (c1.X - c0.X, c1.Y - c0.Y, c1.Z - c0.Z)
    proj = sep[0] * d0[0] + sep[1] * d0[1] + sep[2] * d0[2]
    perp = (sep[0] - proj * d0[0], sep[1] - proj * d0[1], sep[2] - proj * d0[2])
    width = math.sqrt(perp[0] ** 2 + perp[1] ** 2 + perp[2] ** 2)
    if width <= 1e-6:
        raise FeatureError("full round middle face has zero width (v1)")
    return width, [e0, mate]


def _compile_full_round(feat: dict, ctx: CompileContext) -> CompileOutput:
    """Full-round fillet: replace a planar constant-width middle face with a
    tangent half-cylinder (rib-top case). feature_params.md §full_round.

    Realized as the "poor man's full round": both long edges of the middle face
    filleted at r = width/2 * 0.999 in a single build123d fillet call. At exactly
    width/2 OCCT fails (the strip degenerates to a tangent seam); 0.999 leaves a
    ~width*1e-3 mm residual planar sliver that is below CAD tolerance, and the
    volume matches a true half-cylinder full round to <0.01%.
    """
    refs = feat.get("refs", {})
    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("fillet: no body to apply to")
    body = bodies[-1]

    mid_sel = refs.get("middle_face")
    if not mid_sel:
        raise FeatureError(
            "fillet full_round: refs.middle_face is required",
        )
    sel_list = mid_sel if isinstance(mid_sel, list) else [mid_sel]
    faces, dangling = _resolve_faces(sel_list, body, "middle_face", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"fillet full_round: {len(dangling)} face selector(s) dangling",
            dangling_refs=dangling,
        )
    if not faces:
        raise FeatureError("fillet full_round: middle face not resolved")

    width, long_edges = _analyze_middle_face(faces[0])
    r = width / 2.0 * 0.999

    try:
        result_body = b3d_fillet(long_edges, radius=r)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"full round failed (width={width:g} mm): {exc}",
            hint="middle face may be too narrow or adjacent to other features",
        )

    bodies[-1] = result_body
    return CompileOutput(
        bodies=bodies,
        tool_body=result_body,
        resolved={"full_round_width": width},
    )


def _variable_fillet(body, edges, r1: float, r2: float):
    """Apply a linear-law variable-radius fillet (r1 at edge start, r2 at end).

    build123d has no variable-radius wrapper, so drop to raw OCP:
    BRepFilletAPI_MakeFillet.Add(r1, r2, edge) sets a linear radius law per edge.
    Result is re-wrapped as a build123d Solid (matches revolve.py's OCP→Solid path).
    Raises on IsDone()==False so the caller's clamp loop can retry.
    """
    mk = BRepFilletAPI_MakeFillet(body.wrapped)
    for e in edges:
        mk.Add(r1, r2, e.wrapped)
    mk.Build()
    if not mk.IsDone():
        raise RuntimeError("BRepFilletAPI_MakeFillet IsDone=False")
    return Solid(mk.Shape()).clean()


@register("fillet")
def compile_fillet(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    # Full-round (C33-B): replaces a middle face with a tangent half-cylinder.
    # Distinct ref slot (middle_face) + own validation; bypasses the edge path.
    # Triggered by params.full_round (canonical) or the PM "Fillet Type" combo
    # value type == "full_round" (the form gates on that single combo).
    if p.get("full_round") or p.get("type") == "full_round":
        return _compile_full_round(feat, ctx)

    radius = ctx.dim(p, "radius", 2.0)
    if radius <= 0.0:
        raise FeatureError("fillet radius must be > 0")

    # Optional variable-radius end value. 0 / missing / equal to radius → constant.
    radius2_raw = ctx.dim(p, "radius2", 0.0) if "radius2" in p else 0.0
    variable = radius2_raw > 0.0 and abs(radius2_raw - radius) > 1e-9

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
    r2 = radius2_raw if variable else None
    last_err: Exception | None = None

    # Auto-clamp retry: halve ×3, floor 0.2 mm (both radii in lockstep when variable)
    for attempt in range(4):  # 0 = original, 1-3 = clamped
        if attempt > 0:
            r = r / 2.0
            if r2 is not None:
                r2 = r2 / 2.0
            if r < 0.2 or (r2 is not None and r2 < 0.2):
                break
        try:
            if r2 is not None:
                result_body = _variable_fillet(body, edges, r, r2)
            else:
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
    if r2 is not None:
        resolved["radius2"] = r2
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
