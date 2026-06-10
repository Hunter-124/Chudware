"""Primitive feature compilers: box / cylinder / sphere / cone / torus (feature_params.md
§Primitives).

Each primitive builds a build123d solid, optionally places it at an ``at`` datum (default
origin), and then either starts a new body or fuses/cuts into the running body:

- The FIRST solid-producing feature creates Body1.
- Later primitives default ``merge:true`` → boolean with the last body (``mode:"boss"`` fuses,
  ``mode:"cut"`` subtracts). ``merge:false`` starts a new, separate body (real multi-body).

The compiler core stores each feature's ``tool_body`` (the solid it fused/cut) so patterns/mirror
can re-apply the same boolean per instance later (feature_params.md §Patterns).

``centered`` (default true) follows the SolidWorks "centered on origin" convention. When false the
primitive sits with its natural base/corner at the placement point (box corner at origin; cylinder
/ cone base on the placement plane; sphere/torus stay centered — they have no meaningful corner).
"""

from __future__ import annotations

from build123d import Align, Box, Cone, Cylinder, Pos, Sphere, Torus, Vector

from .base import CompileContext, CompileOutput, FeatureError, register

_MIN = Align.MIN
_CTR = Align.CENTER


def _place(solid, ctx: CompileContext, refs: dict):
    """Translate ``solid`` to the ``at`` datum origin (point/plane), if any."""
    at = refs.get("at")
    if at is None:
        return solid
    d = ctx.datum(at)
    if d.kind == "point":
        p = Vector(d.obj)
    elif d.kind in ("plane", "csys"):
        p = Vector(d.obj.origin)
    else:
        raise FeatureError(f"primitive 'at' must be a point/plane datum, got {d.kind}")
    return Pos(p.X, p.Y, p.Z) * solid


def _to_solid(shape):
    """Booleans can return a Compound wrapping a single solid; normalize to one carrier.

    A genuine multi-solid result (a fuse that left disjoint lumps) is kept as-is; callers treat
    the returned carrier as one logical body.
    """
    try:
        solids = shape.solids()
    except Exception:  # noqa: BLE001
        return shape
    if len(solids) == 1:
        return solids[0]
    return shape


def _combine(feat: dict, ctx: CompileContext, solid, resolved: dict) -> CompileOutput:
    """Apply boss/cut/merge semantics against the running body list."""
    params = feat.get("params", {})
    mode = params.get("mode", "boss")
    merge = params.get("merge", True)
    bodies = list(ctx.bodies)

    if not bodies or not merge:
        # First body, or an explicit new separate body.
        bodies.append(solid)
        return CompileOutput(bodies=bodies, tool_body=solid, resolved=resolved)

    target = bodies[-1]
    try:
        if mode == "cut":
            out = target - solid
        else:  # boss
            out = target + solid
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"boolean {mode} failed: {exc}",
            hint="overlapping geometry or an empty result; check placement/dimensions")
    out = _to_solid(out)
    if mode == "cut" and abs(getattr(out, "volume", 0.0) - getattr(target, "volume", 0.0)) < 1e-9:
        # Cut removed nothing — warn-shaped, but not fatal: keep the body, surface a hint.
        raise FeatureError("cut removed no material",
                           hint="the tool solid does not intersect the target body")
    bodies[-1] = out
    return CompileOutput(bodies=bodies, tool_body=solid, resolved=resolved)


@register("box")
def compile_box(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    length = ctx.dim(p, "length", 20.0)
    width = ctx.dim(p, "width", 20.0)
    height = ctx.dim(p, "height", 20.0)
    centered = p.get("centered", True)
    align = (_CTR, _CTR, _CTR) if centered else (_MIN, _MIN, _MIN)
    solid = Box(length, width, height, align=align)
    solid = _place(solid, ctx, feat.get("refs", {}))
    return _combine(feat, ctx, solid,
                    {"length": length, "width": width, "height": height})


@register("cylinder")
def compile_cylinder(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    radius = ctx.dim(p, "radius", 10.0)
    height = ctx.dim(p, "height", 20.0)
    centered = p.get("centered", True)
    align = (_CTR, _CTR, _CTR) if centered else (_CTR, _CTR, _MIN)
    solid = Cylinder(radius, height, align=align)
    solid = _place(solid, ctx, feat.get("refs", {}))
    return _combine(feat, ctx, solid, {"radius": radius, "height": height})


@register("sphere")
def compile_sphere(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    radius = ctx.dim(p, "radius", 10.0)
    solid = Sphere(radius)
    solid = _place(solid, ctx, feat.get("refs", {}))
    return _combine(feat, ctx, solid, {"radius": radius})


@register("cone")
def compile_cone(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    r1 = ctx.dim(p, "r1", 10.0)
    r2 = ctx.dim(p, "r2", 0.0)
    height = ctx.dim(p, "height", 20.0)
    centered = p.get("centered", True)
    align = (_CTR, _CTR, _CTR) if centered else (_CTR, _CTR, _MIN)
    solid = Cone(r1, r2, height, align=align)
    solid = _place(solid, ctx, feat.get("refs", {}))
    return _combine(feat, ctx, solid, {"r1": r1, "r2": r2, "height": height})


@register("torus")
def compile_torus(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    major_r = ctx.dim(p, "major_r", 15.0)
    minor_r = ctx.dim(p, "minor_r", 3.0)
    solid = Torus(major_r, minor_r)
    solid = _place(solid, ctx, feat.get("refs", {}))
    return _combine(feat, ctx, solid, {"major_r": major_r, "minor_r": minor_r})
