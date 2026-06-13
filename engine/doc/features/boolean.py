"""Multi-body boolean feature compiler — Combine / Subtract / Intersect.

feature_params.md §Multi-body > boolean.

A ``boolean`` feature operates on the rolling body list directly (NOT on tool solids like
patterns do): it resolves ``refs.targets`` and ``refs.tools`` to bodies already in
``ctx.bodies`` and fuses / subtracts / intersects them, leaving ONE result body where the
targets were.

- ``op="combine"``   → fuse every target + every tool into one body.
- ``op="subtract"``  → (union of targets) minus (union of tools).
- ``op="intersect"`` → common volume of (union of targets) and (union of tools).

``keep_tools`` (default false): when false the consumed tool bodies are removed from the
rolling body list; when true they stay (SolidWorks "Keep tool bodies").

Body-selector resolution
-------------------------
A body selector is ``{"kind":"body","owner_feature":<fid>}`` (selectors.md / schema §body — it
carries NO centroid, only the owning feature id). We map it to one of ``ctx.bodies`` by the
feature that owns the body: each feature stashed its ``tool_body`` (the solid it contributed)
in ``ctx.tool_bodies``, so we match the owner feature's tool solid to the rolling body with the
nearest geometric signature (volume + bbox centre). For real multi-body docs (separate
``merge:false`` primitives) this is exact; mirrors how pattern.py keys off owner features.

Error containment: every selector miss raises :class:`FeatureError` with ``dangling_refs``; the
OCCT boolean is wrapped so a kernel failure becomes a contained FeatureError, never a crash
(same discipline as hole.py / dress.py).
"""

from __future__ import annotations

import math
from typing import Any

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _store_topo_owners, _to_solid_normalized

_VALID_OPS = ("combine", "subtract", "intersect")


# --------------------------------------------------------------------------- body matching


def _body_signature(body) -> tuple[float, tuple[float, float, float]]:
    """A cheap geometric signature for a body: (volume, bbox-centre)."""
    vol = float(getattr(body, "volume", 0.0) or 0.0)
    try:
        bb = body.bounding_box()
        c = ((bb.min.X + bb.max.X) * 0.5,
             (bb.min.Y + bb.max.Y) * 0.5,
             (bb.min.Z + bb.max.Z) * 0.5)
    except Exception:  # noqa: BLE001
        c = (0.0, 0.0, 0.0)
    return vol, c


def _sig_distance(a: tuple[float, tuple[float, float, float]],
                  b: tuple[float, tuple[float, float, float]]) -> float:
    """Normalised distance between two body signatures (lower = better match)."""
    va, ca = a
    vb, cb = b
    denom = max(abs(va), abs(vb), 1e-9)
    dv = abs(va - vb) / denom
    dc = math.sqrt(sum((ca[i] - cb[i]) ** 2 for i in range(3)))
    scale = max(abs(va) ** (1.0 / 3.0), abs(vb) ** (1.0 / 3.0), 1.0)
    return dv + dc / scale


def _resolve_body(sel: Any, bodies: list[Any], ctx: CompileContext,
                  slot: str) -> int:
    """Resolve one body selector to an INDEX into *bodies*.

    Raises a dangling :class:`FeatureError` when the selector cannot be matched.
    """
    if not isinstance(sel, dict) or sel.get("kind") != "body":
        raise FeatureError(
            f"boolean: {slot} entry must be a body selector",
            dangling_refs=[f"{slot}: not a body selector ({sel!r})"],
        )
    owner = sel.get("owner_feature")
    if not isinstance(owner, str) or not owner:
        raise FeatureError(
            f"boolean: {slot} body selector missing owner_feature",
            dangling_refs=[f"{slot}: body selector has no owner_feature"],
        )

    # The owning feature's tool solid is the geometric anchor for the body it produced.
    anchor = ctx.tool_bodies.get(owner)
    if anchor is None:
        raise FeatureError(
            f"boolean: {slot} references feature '{owner}' which produced no body",
            dangling_refs=[f"{slot}: feature '{owner}' has no body geometry"],
        )
    anchor_sig = _body_signature(anchor)

    best_idx: int | None = None
    best_d = math.inf
    second_d = math.inf
    for i, b in enumerate(bodies):
        d = _sig_distance(anchor_sig, _body_signature(b))
        if d < best_d:
            second_d = best_d
            best_d = d
            best_idx = i
        elif d < second_d:
            second_d = d

    if best_idx is None:
        raise FeatureError(
            f"boolean: {slot} body '{owner}' not found among current bodies",
            dangling_refs=[f"{slot}: no body matches feature '{owner}'"],
        )
    return best_idx


def _resolve_body_set(sels: Any, bodies: list[Any], ctx: CompileContext,
                      slot: str) -> list[int]:
    """Resolve a list of body selectors to a de-duplicated list of body indices."""
    if not isinstance(sels, list) or not sels:
        raise FeatureError(f"boolean: refs.{slot} must be a non-empty list of body selectors")
    out: list[int] = []
    dangling: list[str] = []
    for i, sel in enumerate(sels):
        try:
            idx = _resolve_body(sel, bodies, ctx, f"{slot}[{i}]")
        except FeatureError as exc:
            dangling.extend(exc.dangling_refs or [f"{slot}[{i}]: {exc}"])
            continue
        if idx not in out:
            out.append(idx)
    if dangling:
        raise FeatureError(
            f"boolean: {len(dangling)} {slot} selector(s) failed",
            dangling_refs=dangling,
        )
    return out


# --------------------------------------------------------------------------- compiler


@register("boolean")
def compile_boolean(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    op = p.get("op", "combine")
    if op not in _VALID_OPS:
        raise FeatureError(f"boolean: unknown op '{op}' (combine|subtract|intersect)")
    keep_tools = bool(p.get("keep_tools", False))

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("boolean: no bodies to operate on")

    target_idx = _resolve_body_set(refs.get("targets"), bodies, ctx, "targets")
    if not target_idx:
        raise FeatureError("boolean: refs.targets is required (>=1 body)")

    if op in ("subtract", "intersect"):
        tool_idx = _resolve_body_set(refs.get("tools"), bodies, ctx, "tools")
        if not tool_idx:
            raise FeatureError(f"boolean: op '{op}' requires refs.tools (>=1 body)")
    else:
        # combine: tools are optional — combining targets alone is valid.
        raw_tools = refs.get("tools")
        tool_idx = (_resolve_body_set(raw_tools, bodies, ctx, "tools")
                    if isinstance(raw_tools, list) and raw_tools else [])

    overlap = set(target_idx) & set(tool_idx)
    if overlap:
        raise FeatureError(
            "boolean: a body is named as both a target and a tool",
            dangling_refs=[f"tools: body index {i} is also a target" for i in sorted(overlap)],
        )

    # Fold each side into one solid.
    try:
        target_solid = bodies[target_idx[0]]
        for i in target_idx[1:]:
            target_solid = target_solid + bodies[i]
        target_solid = _to_solid_normalized(target_solid)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"boolean: fusing targets failed: {exc}",
                           hint="the target bodies may be invalid or disjoint")

    tool_solid = None
    if tool_idx:
        try:
            tool_solid = bodies[tool_idx[0]]
            for i in tool_idx[1:]:
                tool_solid = tool_solid + bodies[i]
            tool_solid = _to_solid_normalized(tool_solid)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"boolean: fusing tools failed: {exc}",
                               hint="the tool bodies may be invalid or disjoint")

    # Apply the operation.
    try:
        if op == "combine":
            result = target_solid if tool_solid is None else (target_solid + tool_solid)
        elif op == "subtract":
            result = target_solid - tool_solid
        else:  # intersect
            result = target_solid & tool_solid
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"boolean: {op} failed: {exc}",
                           hint="overlapping geometry or an empty result; check the bodies")
    result = _to_solid_normalized(result)

    if op in ("subtract", "intersect"):
        if getattr(result, "volume", 0.0) <= 1e-9:
            raise FeatureError(
                f"boolean: {op} produced an empty body",
                hint=("the tools do not overlap the targets"
                      if op == "intersect" else "the tools removed the entire target"),
            )

    # Rebuild the rolling body list. All consumed targets are removed; the single fused result is
    # appended LAST so it becomes the top body — the compiler core only owner-tags the top body,
    # so this keeps the boolean result resolvable by downstream face/edge/body selectors. Tools
    # are removed too, UNLESS keep_tools and the op left them as standalone bodies (subtract);
    # combine/intersect always fuse the tools into the result, so there is nothing left to keep.
    consumed = set(target_idx)
    drop_tools = (not keep_tools) or (op != "subtract")
    if drop_tools:
        consumed |= set(tool_idx)

    new_bodies: list[Any] = [b for i, b in enumerate(bodies) if i not in consumed]
    new_bodies.append(result)

    # Tag topology on the result so downstream selectors resolve against this feature.
    _store_topo_owners(ctx, fid, result, target_solid)

    resolved = {
        "_target_count": float(len(target_idx)),
        "_tool_count": float(len(tool_idx)),
        "volume": float(getattr(result, "volume", 0.0) or 0.0),
    }
    return CompileOutput(bodies=new_bodies, tool_body=result, op="add", resolved=resolved)
