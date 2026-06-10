"""Datum feature compilers: plane / axis / point / csys (feature_params.md §Datums).

Datums produce reference geometry stored in the compile context (build123d ``Plane`` / ``Axis``
/ ``Vector`` / ``Location``), never bodies. The reserved built-in datums (doc_schema.md §5) are
seeded by the compiler core via :func:`reserved_datums`; these compilers handle authored datum
features.

v1 scope note: the methods that depend on resolving geometry selectors into faces/edges/vertices
(``offset`` off a model face, ``two_points`` axis, etc.) require the selector resolver that lands
in C03/C09. Here we implement the datum methods that resolve against OTHER datums / reserved
datums and plain numeric params; selector-dependent methods raise a contained FeatureError so the
feature errors honestly rather than guessing.
"""

from __future__ import annotations

from build123d import Axis, Plane, Vector

from .base import CompileContext, CompileOutput, Datum, FeatureError, register


def reserved_datums() -> dict[str, Datum]:
    """The implicit per-document datums (doc_schema.md §5), in canonical Z-up geometry.

    plane.top  = Z=0, +Z   (build123d Plane.XY)
    plane.front= Y=0, -Y   (Plane.XZ — its z_dir is (0,-1,0))
    plane.right= X=0, +X   (Plane.YZ — its z_dir is (1,0,0))
    """

    return {
        "origin": Datum("point", Vector(0, 0, 0), "origin"),
        "plane.top": Datum("plane", Plane.XY, "plane.top"),
        "plane.front": Datum("plane", Plane.XZ, "plane.front"),
        "plane.right": Datum("plane", Plane.YZ, "plane.right"),
        "axis.x": Datum("axis", Axis.X, "axis.x"),
        "axis.y": Datum("axis", Axis.Y, "axis.y"),
        "axis.z": Datum("axis", Axis.Z, "axis.z"),
    }


@register("plane")
def compile_plane(feat: dict, ctx: CompileContext) -> CompileOutput:
    params = feat.get("params", {})
    refs = feat.get("refs", {})
    method = params.get("method", "offset")
    fid = feat["id"]

    if method == "offset":
        base = ctx.datum(refs.get("base", "plane.top"))
        if base.kind != "plane":
            raise FeatureError(f"plane offset needs a planar base, got {base.kind}")
        offset = ctx.dim(params, "offset", 10.0)
        if params.get("flip"):
            offset = -offset
        plane = base.obj.offset(offset)
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("plane", plane, fid),
                             resolved={"offset": offset})

    if method == "midplane":
        a = ctx.datum(refs.get("base", "plane.top"))
        b = ctx.datum(refs.get("second", "plane.top"))
        if a.kind != "plane" or b.kind != "plane":
            raise FeatureError("midplane needs two planar references")
        mid_o = (Vector(a.obj.origin) + Vector(b.obj.origin)) * 0.5
        plane = Plane(origin=mid_o, z_dir=a.obj.z_dir)
        return CompileOutput(bodies=list(ctx.bodies), datum=Datum("plane", plane, fid))

    # angle / three_point need an edge/vertex selector resolver (C03/C09).
    raise FeatureError(
        f"plane method '{method}' needs geometry selectors (resolver lands in C03/C09)")


@register("axis")
def compile_axis(feat: dict, ctx: CompileContext) -> CompileOutput:
    params = feat.get("params", {})
    refs = feat.get("refs", {})
    method = params.get("method", "cylinder")
    fid = feat["id"]

    if method == "two_points":
        a = ctx.datum(refs.get("a"))
        b = ctx.datum(refs.get("b"))
        if a.kind != "point" or b.kind != "point":
            raise FeatureError("axis two_points needs two point datums")
        pa, pb = Vector(a.obj), Vector(b.obj)
        d = pb - pa
        if d.length < 1e-9:
            raise FeatureError("axis two_points: coincident points")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("axis", Axis(pa, d), fid))

    raise FeatureError(
        f"axis method '{method}' needs geometry selectors (resolver lands in C03/C09)")


@register("point")
def compile_point(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat["id"]
    target = refs.get("target")
    # Only datum/vertex-datum targets are resolvable without the geometry selector resolver.
    if isinstance(target, str) or (isinstance(target, dict) and target.get("kind") == "datum"):
        d = ctx.datum(target)
        if d.kind == "point":
            return CompileOutput(bodies=list(ctx.bodies), datum=Datum("point", Vector(d.obj), fid))
    raise FeatureError(
        "point feature needs a vertex/face geometry selector (resolver lands in C03/C09)")


@register("csys")
def compile_csys(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat["id"]
    origin = refs.get("origin", "origin")
    d = ctx.datum(origin) if (isinstance(origin, str) or
                              (isinstance(origin, dict) and origin.get("kind") == "datum")) else None
    if d is None or d.kind != "point":
        raise FeatureError(
            "csys needs a vertex/datum origin (full dir selectors resolve in C03/C09)")
    plane = Plane(origin=Vector(d.obj))
    return CompileOutput(bodies=list(ctx.bodies), datum=Datum("csys", plane, fid))
