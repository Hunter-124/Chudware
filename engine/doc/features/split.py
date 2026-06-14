"""Split feature compiler — divide the running body(ies) by a tool surface into pieces.

feature_params.md §Stretch > split; docs/SW_PARITY_SPEC.md §2 "Split".

SolidWorks "Split" cuts a body with a tool surface and keeps the resulting pieces. Two tool kinds
are supported:

PLANAR tool — a planar model face or a datum plane. The SolidWorks "resulting bodies" choice is
``keep``:

- ``keep="both"``  → the body becomes TWO bodies (the piece above + the piece below the plane).
- ``keep="above"`` → keep only the piece on the +normal side of the plane.
- ``keep="below"`` → keep only the −normal side.

Geometry: the plane's two open half-spaces are realised as a large box on the +normal side (the
"above" solid). ``above = body ∩ half_space``; ``below = body − half_space``. Each EXISTING body is
split this way; non-intersected bodies pass through unchanged (only ``keep`` filters the split
ones). Reuses the half-space approach proven in the weldment mitre trim and the split prototype.

NON-PLANAR / SURFACE tool — a curved model face (a cylindrical/conical/spherical/B-spline face on
the running body or on ANOTHER body, e.g. a revolved or extruded surface body's face). The target
body(ies) are split by that surface with OCCT ``BRepAlgoAPI_Splitter`` (the general BOP splitter
that divides a solid along an arbitrary face), and every resulting solid becomes a separate body
(SolidWorks "keep all"). ``keep`` filters which pieces survive, ordered along the tool face's local
normal so it lines up with the planar above/below convention:

- ``keep="both"``  → keep ALL resulting pieces (≥2 bodies).
- ``keep="above"`` → keep the pieces on the +normal side of the tool face (centroid side).
- ``keep="below"`` → keep the −normal side.

The body that OWNS the tool face is never itself consumed by its own surface (splitting a body by
one of its own faces is a no-op); it passes through unchanged. Faces from another body split every
OTHER running body. A planar model face still takes the exact planar above/below path above (so
behaviour for planar tools is unchanged); only genuinely curved tool faces take the BOP path.

Errors (contained :class:`FeatureError`): no body; a dangling tool selector; the tool surface does
not intersect / divide any body (so nothing is split).
"""

from __future__ import annotations

from typing import Any

from build123d import Plane, Solid, Vector

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized
from .pattern import _resolve_plane
from ..selectors import describe_faces

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


# --------------------------------------------------------------------------- surface (non-planar)


def _face_is_planar(info: Any) -> bool:
    return getattr(info, "surf", None) == "plane"


def _resolve_tool_face(tool: dict, ctx: CompileContext) -> tuple[Any, Any, int]:
    """Resolve a FACE selector to ``(owning_body, build123d_Face, body_index)``.

    Searches every running body for the best-scoring face match (so a tool face on ANOTHER body
    resolves too, not just the last body). Raises a contained :class:`FeatureError` with
    ``dangling_refs`` when no body yields an accepted match.
    """
    from ..selectors import resolve

    best: tuple[float, int, int] | None = None  # (score, body_index, face_index)
    best_reason = "no candidates"
    for bi, body in enumerate(ctx.bodies):
        wrapped = getattr(body, "wrapped", None)
        if wrapped is None:
            continue
        rr = resolve(tool, describe_faces(wrapped), _body_bbox_diag(body))
        if rr.index is not None and (best is None or rr.score < best[0]):
            best = (rr.score, bi, rr.index)
        elif rr.dangling_reason:
            best_reason = rr.dangling_reason
    if best is None:
        raise FeatureError(
            f"split: tool face is dangling — {best_reason}",
            dangling_refs=[f"tool: {best_reason}"],
        )
    _, bi, fi = best
    body = ctx.bodies[bi]
    face = body.faces()[fi]
    return body, face, bi


def _split_by_surface(target, tool_face) -> list[Any]:
    """Split solid ``target`` by ``tool_face`` (a build123d Face) using OCCT's general BOP
    splitter, returning the resulting solids (build123d ``Solid``). Empty when the face does not
    actually divide the target (BOP yields a single solid, i.e. the surface misses / is tangent)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    args = TopTools_ListOfShape()
    args.Append(target.wrapped)
    tools = TopTools_ListOfShape()
    tools.Append(tool_face.wrapped)

    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(args)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        return []

    out: list[Any] = []
    exp = TopExp_Explorer(splitter.Shape(), TopAbs_SOLID)
    while exp.More():
        sol = Solid(TopoDS.Solid_s(exp.Current()))
        if getattr(sol, "volume", 0.0) > 1e-9:
            out.append(sol)
        exp.Next()
    # A single resulting solid means the surface did not actually divide the body.
    return out if len(out) >= 2 else []


def _piece_centroid(piece) -> Vector:
    try:
        return Vector(piece.center())
    except Exception:  # noqa: BLE001
        bb = piece.bounding_box()
        return Vector((bb.min.X + bb.max.X) / 2, (bb.min.Y + bb.max.Y) / 2,
                      (bb.min.Z + bb.max.Z) / 2)


def _bucket_pieces(pieces: list[Any], normal: Vector, keep: str) -> list[Any]:
    """Split ``pieces`` into the keep-side subset along the tool face ``normal``.

    A non-planar tool face has no single global "above/below" plane (its normal varies over the
    surface, and a closed surface like a full cylinder has no outside reference point), so the side
    is defined RELATIVELY: project each piece centroid onto ``normal`` and split the pieces at the
    MEDIAN projection. ``above`` = the pieces at/above the median (the +normal half), ``below`` =
    the pieces below it. For the common two-piece split this gives exactly one piece per side. This
    is deterministic and lines up with the planar above/below convention (the +normal piece is the
    "above" one). ``keep="both"`` keeps everything (SolidWorks "keep all bodies")."""
    if keep == "both" or len(pieces) < 2:
        return pieces
    projed = sorted(pieces, key=lambda p: normal.dot(_piece_centroid(p)))
    projs = [normal.dot(_piece_centroid(p)) for p in projed]
    median = projs[len(projs) // 2] if len(projs) % 2 else (
        (projs[len(projs) // 2 - 1] + projs[len(projs) // 2]) / 2.0)
    if keep == "above":
        kept = [p for p, v in zip(projed, projs) if v >= median]
    else:  # below
        kept = [p for p, v in zip(projed, projs) if v < median]
    return kept or pieces  # degenerate (all equal) — keep all rather than drop the body


def _surface_split(tool: dict, keep: str, ctx: CompileContext, fid: str) -> CompileOutput:
    """Surface (non-planar) split path. ``tool`` is a face selector resolving to a curved face."""
    owner_body, tool_face, owner_idx = _resolve_tool_face(tool, ctx)

    # Local normal of the tool face — the direction along which keep-side is measured (median split).
    try:
        ref_normal = Vector(tool_face.normal_at()).normalized()
    except Exception:  # noqa: BLE001
        ref_normal = Vector(0, 0, 1)

    new_bodies: list[Any] = []
    split_count = 0
    produced = 0
    for bi, body in enumerate(ctx.bodies):
        # A body is never split by one of its OWN faces (degenerate: BOP returns it whole).
        if bi == owner_idx:
            new_bodies.append(body)
            continue
        pieces = _split_by_surface(body, tool_face)
        if not pieces:
            new_bodies.append(body)
            continue
        split_count += 1
        kept = _bucket_pieces(pieces, ref_normal, keep)
        new_bodies.extend(kept)
        produced += len(kept)

    if split_count == 0:
        raise FeatureError(
            "split: the tool surface does not intersect/divide any body",
            hint="position the surface so it passes fully through the part",
        )

    top = new_bodies[-1]
    _store_topo_owners(ctx, fid, top, None)
    return CompileOutput(
        bodies=new_bodies, tool_body=top, op="add",
        resolved={"_split_bodies": float(split_count),
                  "_pieces_kept": float(produced),
                  "_surface_tool": 1.0,
                  "keep_above": 1.0 if keep in ("both", "above") else 0.0,
                  "keep_below": 1.0 if keep in ("both", "below") else 0.0},
    )


def _tool_is_curved_face(tool: Any, ctx: CompileContext) -> bool:
    """True iff ``tool`` is a FACE selector whose best match across the running bodies is a
    NON-planar surface (so it needs the BOP surface-split path). A planar face / a datum / a
    reserved-plane string returns False and takes the original planar half-space path."""
    if not (isinstance(tool, dict) and tool.get("kind") == "face"):
        return False
    from ..selectors import resolve

    best_planar: bool | None = None
    best_score = float("inf")
    for body in ctx.bodies:
        wrapped = getattr(body, "wrapped", None)
        if wrapped is None:
            continue
        infos = describe_faces(wrapped)
        rr = resolve(tool, infos, _body_bbox_diag(body))
        if rr.index is None:
            continue
        match = next((i for i in infos if i.index == rr.index), None)
        if match is not None and rr.score < best_score:
            best_score = rr.score
            best_planar = _face_is_planar(match)
    # Unresolved everywhere → let the planar path raise its dangling error.
    return best_planar is False


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
        raise FeatureError("split: refs.tool is required (a planar face, a datum plane, or a "
                           "non-planar surface/face)",
                           dangling_refs=["tool: missing"])

    # Non-planar / surface split: a FACE selector whose match is a curved surface goes through the
    # general BOP splitter path. Planar faces, datum planes, and reserved planes keep the original
    # half-space planar path below (behaviour for planar tools is unchanged).
    if _tool_is_curved_face(tool, ctx):
        return _surface_split(tool, keep, ctx, fid)

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
