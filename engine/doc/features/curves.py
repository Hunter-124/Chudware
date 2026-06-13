"""Curve feature compilers — helix (feature_params.md §Sketched features > helix).

``helix`` is a CURVE / reference feature: it produces a wire (a build123d ``Helix``) and is
consumed downstream by a ``sweep`` as its path. It does NOT add a solid body.

Consumption contract (so sweep can use a helix feature as ``refs.path``)
------------------------------------------------------------------------
``sweep`` resolves a path of the form ``{"kind":"region","sketch":<fid>}`` by looking up
``ctx.datums["_sketch_regions"][fid]`` and lifting that entry's ``all_edges`` through its
``plane`` (``plane.location * edge``). The sketch compiler populates that store; we mirror it
exactly: helix registers its own feature id into ``_sketch_regions`` with

    {"all_edges": [<helix edges in the base local frame>],
     "plane": <base plane>, "faces": [], "descs": []}

so a downstream ``sweep`` whose ``refs.path`` is ``{"kind":"region","sketch":<helix_fid>}``
consumes the helix as its spine with no change to sweep.py.

Parameters (feature_params.md §helix)
-------------------------------------
``pitch`` (5) · ``height`` (20) · ``turns`` (null → height/pitch) · ``taper`` (0,
degrees → cone angle) · ``start_angle`` (0, degrees, about the helix axis) · ``ccw`` (true).
When ``turns`` is given it drives the height (``height = turns * pitch``) so the wire carries
exactly that many revolutions.

``refs.base`` (default ``plane.top``) positions/orients the helix axis: a datum plane / csys
gives the origin + axis (its +Z) and, when it is a circle-edge / cylinder-face model selector,
its radius. Standalone helices (no pickable circle) fall back to a ``radius`` param (default
10) — see the report's contract-deviation note.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Edge, Helix, Plane, Vector

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from engine.doc.selectors import describe_edges, describe_faces, resolve


_DEFAULT_RADIUS = 10.0


def _resolve_base(ref: Any, ctx: CompileContext) -> tuple[Plane, float | None]:
    """Resolve ``refs.base`` to a (plane, radius_or_None).

    - datum plane / csys → (its plane, None) : origin + axis come from the datum, radius unknown.
    - model circle-edge selector → (plane on the circle, circle radius).
    - model cylinder-face selector → (plane at the face centre along its axis, cylinder radius).
    """
    if ref is None:
        ref = "plane.top"

    # Datum plane / csys (reserved string id or {"kind":"datum",...}).
    if isinstance(ref, str) or (isinstance(ref, dict) and ref.get("kind") == "datum"):
        d = ctx.datum(ref)
        if d.kind in ("plane", "csys"):
            return d.obj, None
        if d.kind == "point":
            return Plane(origin=Vector(d.obj)), None
        raise FeatureError(f"helix: datum base '{ref}' (kind {d.kind}) is not a plane")

    bodies = ctx.bodies
    if not bodies:
        raise FeatureError("helix: geometry base ref but no bodies exist")
    body = bodies[-1]
    try:
        bb = body.bounding_box()
        diag = max(((bb.max.X - bb.min.X) ** 2 + (bb.max.Y - bb.min.Y) ** 2
                    + (bb.max.Z - bb.min.Z) ** 2) ** 0.5, 1e-3)
    except Exception:  # noqa: BLE001
        diag = 100.0

    if isinstance(ref, dict) and ref.get("kind") == "edge":
        rr = resolve(ref, describe_edges(body.wrapped), diag)
        if rr.index is None:
            raise FeatureError(f"helix: base edge dangling — {rr.dangling_reason}",
                               dangling_refs=[f"base: {rr.dangling_reason}"])
        edge = body.edges()[rr.index]
        try:
            radius = float(edge.radius)
            center = Vector(edge.arc_center)
            axis = Vector(edge.normal())
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"helix: base edge is not a circle: {exc}")
        return Plane(origin=center, z_dir=axis), radius

    if isinstance(ref, dict) and ref.get("kind") == "face":
        rr = resolve(ref, describe_faces(body.wrapped), diag)
        if rr.index is None:
            raise FeatureError(f"helix: base face dangling — {rr.dangling_reason}",
                               dangling_refs=[f"base: {rr.dangling_reason}"])
        face = body.faces()[rr.index]
        try:
            surf = face.geom_adaptor()
            radius = float(surf.Cylinder().Radius())
            ax = surf.Cylinder().Axis()
            loc = ax.Location()
            center = Vector(loc.X(), loc.Y(), loc.Z())
            axdir = ax.Direction()
            axis = Vector(axdir.X(), axdir.Y(), axdir.Z())
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"helix: base face is not a cylinder: {exc}")
        return Plane(origin=center, z_dir=axis), radius

    raise FeatureError(f"helix: unsupported base reference {ref!r}")


@register("helix")
def compile_helix(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    pitch = ctx.dim(p, "pitch", 5.0)
    if pitch <= 0.0:
        raise FeatureError("helix: pitch must be > 0")
    height = ctx.dim(p, "height", 20.0)
    taper = ctx.dim(p, "taper", 0.0)            # degrees → cone_angle
    start_angle = ctx.dim(p, "start_angle", 0.0)  # degrees, about the helix axis
    ccw = bool(p.get("ccw", True))

    # turns: null → height/pitch; given → drives the height so the wire has that many turns.
    turns_param = p.get("turns")
    if turns_param is not None:
        turns = ctx.dim(p, "turns", 0.0)
        if turns <= 0.0:
            raise FeatureError("helix: turns must be > 0")
        height = turns * pitch
    else:
        if height <= 0.0:
            raise FeatureError("helix: height must be > 0")
        turns = height / pitch

    base_plane, base_radius = _resolve_base(refs.get("base"), ctx)
    radius = base_radius if base_radius is not None else ctx.dim(p, "radius", _DEFAULT_RADIUS)
    if radius <= 0.0:
        raise FeatureError("helix: radius must be > 0")

    # Build the helix in the base LOCAL frame (axis +Z at the local origin); sweep lifts it
    # through base_plane.location, so storing local edges keeps placement consistent with the
    # sketch-region path contract. ccw (right-handed default) → lefthand=False.
    try:
        wire = Helix(
            pitch=pitch,
            height=height,
            radius=radius,
            center=(0.0, 0.0, 0.0),
            direction=(0.0, 0.0, 1.0),
            cone_angle=taper,
            lefthand=(not ccw),
        )
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"helix: could not build helix: {exc}",
                           hint="check pitch/height/turns/radius are positive and finite")

    # start_angle: rotate the local helix about its own axis (Z).
    if abs(start_angle) > 1e-9:
        try:
            from build123d import Axis
            wire = wire.rotate(Axis.Z, start_angle)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"helix: applying start_angle failed: {exc}")

    edges: list[Edge] = list(wire.edges())
    if not edges:
        raise FeatureError("helix: produced no curve geometry")

    # Register into the _sketch_regions store so sweep can consume this helix as a path via
    # {"kind":"region","sketch":<this fid>}. Mirrors the sketch compiler's payload shape.
    _SKETCH_KEY = "_sketch_regions"
    if _SKETCH_KEY not in ctx.datums:
        ctx.datums[_SKETCH_KEY] = Datum(kind="_sketch_regions", obj={}, feature_id="__sys__")
    ctx.datums[_SKETCH_KEY].obj[fid] = {
        "faces": [],
        "descs": [],
        "plane": base_plane,
        "all_edges": edges,
    }

    # A curve feature adds NO solid body. Store the wire as tool_body so the AICM path can find
    # it (same convention sketches use), but leave the rolling body list untouched.
    bodies = list(ctx.bodies)
    return CompileOutput(
        bodies=bodies,
        tool_body=wire,
        op=None,
        resolved={"pitch": pitch, "height": height, "turns": turns,
                  "taper": taper, "radius": radius},
    )
