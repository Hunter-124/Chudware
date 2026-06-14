"""Split feature compiler — divide the running body(ies) by a planar tool into above/below pieces.

feature_params.md §Stretch > split.

SolidWorks "Split" cuts a body with a tool surface (here a PLANE — a planar model face or a datum
plane) and keeps the resulting pieces. v1 supports a single planar tool and the SolidWorks
"resulting bodies" choice via ``keep``:

- ``keep="both"``  → the body becomes TWO bodies (the piece above + the piece below the plane).
- ``keep="above"`` → keep only the piece on the +normal side of the plane.
- ``keep="below"`` → keep only the −normal side.

Geometry: the plane's two open half-spaces are realised as a large box on the +normal side (the
"above" solid). ``above = body ∩ half_space``; ``below = body − half_space``. Each EXISTING body is
split this way; non-intersected bodies pass through unchanged (only ``keep`` filters the split
ones). Reuses the half-space approach proven in the weldment mitre trim and the split prototype.

Errors (contained :class:`FeatureError`): no body; non-planar tool; the tool plane does not
intersect any body (so nothing is split).
"""

from __future__ import annotations

from typing import Any

from build123d import Plane, Solid, Vector

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized
from .pattern import _resolve_plane

_VALID_KEEP = ("both", "above", "below")


def _half_space(point: Vector, normal: Vector, reach: float) -> Solid:
    """A large box whose near face lies on the plane (``point``, ``normal``) and whose body extends
    to the +normal side. ``body ∩ half_space`` = the piece above; ``body − half_space`` = below."""
    n = normal.normalized()
    box = Solid.make_box(2 * reach, 2 * reach, 2 * reach).translate(Vector(-reach, -reach, 0.0))
    cut_plane = Plane(origin=point, z_dir=n)
    return cut_plane.location * box


def _split_one(body, point: Vector, normal: Vector, reach: float) -> tuple[Any, Any]:
    """Split ``body`` by the plane (point, normal) → (above, below); either may be None (no material
    on that side, i.e. the plane misses the body / is tangent)."""
    hs = _half_space(point, normal, reach)
    try:
        above = _to_solid_normalized(body.intersect(hs))
    except Exception:  # noqa: BLE001
        above = None
    try:
        below = _to_solid_normalized(body - hs)
    except Exception:  # noqa: BLE001
        below = None
    if above is not None and getattr(above, "volume", 0.0) <= 1e-9:
        above = None
    if below is not None and getattr(below, "volume", 0.0) <= 1e-9:
        below = None
    return above, below


@register("split")
def compile_split(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    keep = p.get("keep", "both")
    if keep not in _VALID_KEEP:
        raise FeatureError(f"split: unknown keep '{keep}' (expected both|above|below)")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("split: no body to split")

    tool = refs.get("tool")
    if tool is None:
        raise FeatureError("split: refs.tool is required (a planar face or a datum plane)",
                           dangling_refs=["tool: missing"])
    # `_resolve_plane` accepts a datum plane/csys ref or a planar model-face selector and raises a
    # contained FeatureError (with dangling_refs) on a non-planar / dangling tool.
    plane = _resolve_plane(tool, ctx)
    point = Vector(plane.origin)
    normal = Vector(plane.z_dir).normalized()

    # Reach must cover every body, not just the top one (split acts on all running bodies).
    reach = max((_body_bbox_diag(b) for b in bodies), default=100.0) * 2.0 + 10.0

    new_bodies: list[Any] = []
    split_count = 0       # bodies the plane actually divided into two
    produced = 0          # pieces kept from split bodies
    for body in bodies:
        above, below = _split_one(body, point, normal, reach)
        crosses = above is not None and below is not None
        if not crosses:
            # The plane misses this body (or is tangent): it survives unchanged regardless of keep.
            new_bodies.append(body)
            continue
        split_count += 1
        if keep == "above":
            new_bodies.append(above)
            produced += 1
        elif keep == "below":
            new_bodies.append(below)
            produced += 1
        else:  # both
            new_bodies.append(above)
            new_bodies.append(below)
            produced += 2

    if split_count == 0:
        raise FeatureError(
            "split: the tool plane does not intersect any body",
            hint="position the plane so it passes through the part",
        )

    # Tag topology on the LAST kept piece (the compiler core only owner-tags the top body; the rest
    # keep their prior owners, which is fine for a clean planar split).
    top = new_bodies[-1]
    _store_topo_owners(ctx, fid, top, None)

    return CompileOutput(
        bodies=new_bodies, tool_body=top, op="add",
        resolved={"_split_bodies": float(split_count),
                  "_pieces_kept": float(produced),
                  "keep_above": 1.0 if keep in ("both", "above") else 0.0,
                  "keep_below": 1.0 if keep in ("both", "below") else 0.0},
    )
