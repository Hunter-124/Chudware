"""Draft feature compiler — taper faces about a neutral plane (mold draft).

feature_params.md §Dress-up > draft. Tilts each selected face by ``params.angle`` degrees about
the line where the face meets the neutral plane, measured relative to the pull direction (the
mold-removal direction). Built with OCCT ``BRepOffsetAPI_DraftAngle``.

Refs:
- ``neutral`` — a datum plane / planar model face: the cross-section there is unchanged.
- ``pull``    — a direction (datum axis/plane, linear edge, or planar face normal). Defaults to
  the neutral plane normal when omitted.
- ``faces``   — the model faces to draft.

SolidWorks-parity draft types (SW_PARITY_SPEC §2 Draft) — ``params.mode``
-------------------------------------------------------------------------
* ``"neutral_plane"`` (default, legacy) — the classic neutral-plane face draft above. Output is
  byte-for-byte identical to the previous compiler when ``mode`` is absent / ``"neutral_plane"``.

* ``"parting_line"`` — **parting-line draft.** A parting line (a chain of edges) splits each drafted
  face into two regions — one on each side of the parting level along the pull direction — that are
  drafted in OPPOSITE senses, so the parting line is the maximal silhouette and the part releases
  from BOTH halves of a two-part mold. Geometry: a *parting plane* is derived from the parting-line
  edges (a plane through their centroid whose normal is the pull direction); the SOLID is split by
  that plane into an "above" piece and a "below" piece (the proven half-space split from
  ``split.py``); each piece's drafted faces are tapered INDEPENDENTLY (separate
  ``BRepOffsetAPI_DraftAngle`` builders, so there is no coplanar-twin propagation) with opposite
  angle signs about the parting plane; the two drafted pieces are fused back into one solid.

* ``"step"`` — **step draft** (stepped-parting variant). Same split-and-draft as parting-line, but
  the two regions are drafted in the SAME sense and the upper region is offset by a small step
  (``params.step``, default = thickness-scaled) along the pull's in-plane direction, leaving a
  small stepped riser face at the parting line instead of a knife edge — SolidWorks' step-draft for
  a non-planar parting line.

The parting line is supplied in ``params.parting`` (kept in *params*, not a new ref slot, so the
document schema is unchanged) as a list of EDGE selectors resolved against the target body. The
pull direction is ``refs.pull`` (or the neutral-plane normal); for parting modes the neutral ref
may name any plane perpendicular to pull — it is only used to seed the pull default.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Plane, Solid, Vector
from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_edges, describe_faces, resolve
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized
from .pattern import _resolve_direction, _resolve_plane

_VALID_MODES = ("neutral_plane", "parting_line", "step")


def _gp_pln(plane: Plane) -> gp_Pln:
    o = Vector(plane.origin)
    n = Vector(plane.z_dir)
    return gp_Pln(gp_Pnt(o.X, o.Y, o.Z), gp_Dir(n.X, n.Y, n.Z))


def _gp_pln_pn(point: Vector, normal: Vector) -> gp_Pln:
    n = normal.normalized()
    return gp_Pln(gp_Pnt(point.X, point.Y, point.Z), gp_Dir(n.X, n.Y, n.Z))


def _resolve_faces(target, face_sels: list, diag: float, what: str) -> list:
    """Resolve a list of face selectors against ``target`` → list of build123d faces.

    Raises a contained :class:`FeatureError` (with dangling_refs) on any unresolved selector.
    """
    infos = describe_faces(target.wrapped)
    faces = []
    dangling: list[str] = []
    for i, sel in enumerate(face_sels):
        rr = resolve(sel, infos, diag)
        if rr.index is None:
            dangling.append(f"{what}[{i}]: {rr.dangling_reason}")
            continue
        faces.append(target.faces()[rr.index])
    if dangling:
        raise FeatureError(f"draft: {len(dangling)} {what} selector(s) dangling",
                           dangling_refs=dangling)
    if not faces:
        raise FeatureError(f"draft: no {what} resolved")
    return faces


def _resolve_parting_edges(target, parting_sels: list, diag: float) -> list:
    """Resolve parting-line edge selectors → list of build123d edges.

    Edge selection for a parting line is special: OCCT enumerates each model edge once per adjacent
    face, so a clean pick is frequently a *symmetric tie* between an edge and its duplicate (margin
    ~0 → ``resolve`` reports dangling). That tie is harmless here — the parting line only needs to
    define a LEVEL + direction, and tied edges are geometrically identical or co-level. So we accept
    the best-scoring candidate even when the strict accept-margin is not met, and only treat a
    selector as truly dangling when there is genuinely no close candidate (best score is large).
    """
    edge_infos = describe_edges(target.wrapped)
    same_kind = [c for c in edge_infos if c.kind == "edge"]
    edges = []
    dangling: list[str] = []
    for i, sel in enumerate(parting_sels):
        rr = resolve(sel, edge_infos, diag)
        if rr.index is not None:
            edges.append(target.edges()[rr.index])
            continue
        # Strict resolve failed. If it was only an ambiguity tie (best score small), take the
        # nearest candidate by centroid distance; otherwise the selector is genuinely dangling.
        if rr.score < 0.35 and same_kind:
            import numpy as np  # local import; selectors already use numpy
            s_c = np.asarray(sel.get("centroid", [0.0, 0.0, 0.0]), dtype=float)
            best = min(same_kind, key=lambda c: float(np.linalg.norm(c.centroid - s_c)))
            edges.append(target.edges()[best.index])
        else:
            dangling.append(f"parting[{i}]: {rr.dangling_reason}")
    if dangling:
        raise FeatureError(f"draft: {len(dangling)} parting-line edge selector(s) dangling",
                           dangling_refs=dangling)
    if not edges:
        raise FeatureError("draft: no parting-line edges resolved")
    return edges


def _neutral_plane_draft(target, faces: list, neutral: Plane, pull: Vector,
                         angle_deg: float):
    """The classic neutral-plane draft: tilt every face about (neutral ∩ face) by ``angle_deg``."""
    try:
        builder = BRepOffsetAPI_DraftAngle(target.wrapped)
        pull_dir = gp_Dir(pull.X, pull.Y, pull.Z)
        neutral_pln = _gp_pln(neutral)
        ang = math.radians(angle_deg)
        for f in faces:
            builder.Add(f.wrapped, pull_dir, ang, neutral_pln, True)
        builder.Build()
        if not builder.IsDone():
            raise FeatureError(
                "draft: OCCT DraftAngle failed (IsDone=False)",
                hint="check the neutral plane intersects the drafted faces and the pull "
                     "direction is roughly normal to the neutral plane")
        return _to_solid_normalized(Solid(builder.Shape()).clean())
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"draft: build failed: {exc}",
                           hint="drafting works on planar/cylindrical side faces about a "
                                "neutral plane they all touch")


def _half_space(point: Vector, normal: Vector, reach: float) -> Solid:
    """A large box whose near face lies on the plane (point, normal); body extends +normal.
    ``body ∩ hs`` = the piece on the +normal side; ``body − hs`` = the −normal side. (Mirrors
    ``split.py._half_space``.)"""
    n = normal.normalized()
    box = Solid.make_box(2 * reach, 2 * reach, 2 * reach).translate(Vector(-reach, -reach, 0.0))
    return Plane(origin=point, z_dir=n).location * box


def _draft_piece(piece, parting_pt: Vector, pull: Vector, angle_deg: float,
                 face_pred) -> Any:
    """Draft the faces of a single solid ``piece`` (selected by ``face_pred``) about the parting
    plane (parting_pt, normal=pull) by ``angle_deg``. Returns the drafted solid, or the piece
    unchanged if no candidate face is present / the build degenerates (kept robust because a piece
    may not contain any drafted face after the split)."""
    cands = []
    for f in piece.faces():
        try:
            if face_pred(f):
                cands.append(f)
        except Exception:  # noqa: BLE001
            continue
    if not cands:
        return piece
    try:
        builder = BRepOffsetAPI_DraftAngle(piece.wrapped)
        pull_dir = gp_Dir(pull.X, pull.Y, pull.Z)
        neutral_pln = _gp_pln_pn(parting_pt, pull)
        ang = math.radians(angle_deg)
        added = 0
        for f in cands:
            builder.Add(f.wrapped, pull_dir, ang, neutral_pln, True)
            if builder.AddDone():
                added += 1
            else:
                try:
                    builder.Remove(f.wrapped)
                except Exception:  # noqa: BLE001
                    pass
        if added == 0:
            return piece
        builder.Build()
        if not builder.IsDone():
            return piece
        return _to_solid_normalized(Solid(builder.Shape()).clean())
    except Exception:  # noqa: BLE001
        return piece


def _parting_line_draft(target, faces: list, parting_edges: list, pull: Vector,
                        angle_deg: float, diag: float, *, step: bool, step_dist: float):
    """Parting-line (and step) draft. See the module docstring.

    Strategy: derive the parting plane from the parting-line edge centroids (origin = their
    midpoint, normal = pull); split the solid into above/below pieces by that plane; draft each
    piece independently about the parting plane; fuse. For ``step`` the two pieces are drafted in
    the SAME sense and the upper piece is shifted by ``step_dist`` along the in-plane pull direction
    to leave a stepped riser at the parting line.
    """
    pull = pull.normalized()

    # Parting plane origin: centroid of the parting-line edge midpoints.
    pts: list[Vector] = []
    for e in parting_edges:
        try:
            c = e.center()
            pts.append(Vector(c.X, c.Y, c.Z))
        except Exception:  # noqa: BLE001
            for v in e.vertices():
                pts.append(Vector(v.X, v.Y, v.Z))
    if not pts:
        raise FeatureError("draft: could not locate the parting line on the body")
    origin = Vector(0, 0, 0)
    for q in pts:
        origin = origin + q
    origin = origin * (1.0 / len(pts))

    # In-plane step direction (perpendicular to pull) — the step riser pushes the upper region OUT
    # along the drafted WALL's outward normal (projected into the parting plane), so the step is a
    # genuine offset of the wall. Fall back to the longest parting-edge chord, then any seed axis.
    in_plane = Vector(0, 0, 0)
    for f in faces:
        try:
            n = Vector(f.normal_at())
        except Exception:  # noqa: BLE001
            continue
        n = n - pull * n.dot(pull)
        if n.length > in_plane.length:
            in_plane = n
    if in_plane.length < 1e-9:
        for e in parting_edges:
            vs = e.vertices()
            if len(vs) >= 2:
                chord = (Vector(vs[-1].X, vs[-1].Y, vs[-1].Z)
                         - Vector(vs[0].X, vs[0].Y, vs[0].Z))
                chord = chord - pull * chord.dot(pull)
                if chord.length > in_plane.length:
                    in_plane = chord
    if in_plane.length < 1e-9:
        seed = Vector(1, 0, 0) if abs(pull.X) < 0.9 else Vector(0, 1, 0)
        in_plane = seed - pull * seed.dot(pull)
    in_plane = in_plane.normalized()

    reach = diag * 2.0 + 10.0
    hs = _half_space(origin, pull, reach)
    try:
        above = _to_solid_normalized(target.intersect(hs))
    except Exception:  # noqa: BLE001
        above = None
    try:
        below = _to_solid_normalized(target - hs)
    except Exception:  # noqa: BLE001
        below = None
    if above is None or getattr(above, "volume", 0.0) <= 1e-9 or \
       below is None or getattr(below, "volume", 0.0) <= 1e-9:
        raise FeatureError(
            "draft: the parting line does not split the body into two sides along the pull "
            "direction",
            hint="the parting line must cross the drafted faces and lie between the body's "
                 "extremes along the pull direction")

    # Which faces to draft on each split piece: the fragments of the USER-PICKED side walls. After
    # the half-space split a picked wall (e.g. the +X wall) becomes one fragment per piece. We
    # match a piece face to a pick by (a) SIGNED normal agreement — so the +X wall is drafted but
    # the −X wall is not — and (b) the same in-plane position (centroid projected perpendicular to
    # pull), so only the picked wall (not a parallel opposite wall) is drafted.
    picks: list[tuple[Vector, Vector]] = []  # (unit normal, centroid)
    for f in faces:
        try:
            n = Vector(f.normal_at()).normalized()
            c = Vector(f.center())
        except Exception:  # noqa: BLE001
            continue
        picks.append((n, c))
    if not picks:
        raise FeatureError("draft: selected faces have no usable normal for parting-line draft")

    tol = max(diag * 0.02, 0.5)

    def _is_draft_face(f) -> bool:
        try:
            n = Vector(f.normal_at()).normalized()
            c = Vector(f.center())
        except Exception:  # noqa: BLE001
            return False
        # A side wall, not the parting cap (cap normal ‖ pull) and not a +Z/-Z top/bottom.
        if abs(n.dot(pull)) > 0.9:
            return False
        for pn, pc in picks:
            if n.dot(pn) <= 0.85:        # SIGNED — must face the same way as the pick
                continue
            # In-plane offset between centroids (drop the along-pull component, which differs
            # because the fragment sits above/below the parting line).
            d = c - pc
            d = d - pull * d.dot(pull)
            if d.length <= tol:
                return True
        return False

    # Above region drafts +angle; below region drafts −angle (opposite) for parting-line, or the
    # SAME sign for step (then the upper region is stepped over).
    above_ang = angle_deg
    below_ang = angle_deg if step else -angle_deg

    da_above = _draft_piece(above, origin, pull, above_ang, _is_draft_face)
    da_below = _draft_piece(below, origin, pull, below_ang, _is_draft_face)

    if step and step_dist > 0.0:
        try:
            da_above = _to_solid_normalized(da_above.translate(in_plane * step_dist))
        except Exception:  # noqa: BLE001
            pass

    try:
        fused = _to_solid_normalized(da_above + da_below)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"draft: could not re-join the drafted parting-line regions: {exc}")
    if getattr(fused, "volume", 0.0) <= 1e-9:
        raise FeatureError("draft: parting-line draft produced an empty body")
    return fused, origin, pull, in_plane


@register("draft")
def compile_draft(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    angle_deg = ctx.dim(p, "angle", 3.0)
    if abs(angle_deg) < 1e-6:
        raise FeatureError("draft: angle must be non-zero")
    if abs(angle_deg) >= 90.0:
        raise FeatureError("draft: angle must be less than 90 degrees")

    mode = p.get("mode", "neutral_plane")
    if mode not in _VALID_MODES:
        raise FeatureError(
            f"draft: unknown mode '{mode}' (expected neutral_plane|parting_line|step)")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("draft: no body to draft")
    target = bodies[-1]
    diag = _body_bbox_diag(target)

    neutral = _resolve_plane(refs.get("neutral"), ctx)
    pull = (_resolve_direction(refs.get("pull"), ctx) if refs.get("pull") is not None
            else Vector(neutral.z_dir))

    face_sels = refs.get("faces") or []
    if not face_sels:
        raise FeatureError("draft: refs.faces is required (one or more faces to taper)")
    faces = _resolve_faces(target, face_sels, diag, "faces")

    if mode == "neutral_plane":
        result = _neutral_plane_draft(target, faces, neutral, pull, angle_deg)
        bodies[-1] = result
        _store_topo_owners(ctx, fid, result, target)
        return CompileOutput(bodies=bodies, tool_body=None,
                             resolved={"angle": angle_deg, "_mode_parting": 0.0,
                                       "_mode_step": 0.0})

    # ---- parting-line / step draft ----
    # refs.parting is the canonical home for the parting-line edge selectors (the PM form's well
    # fills it); fall back to params.parting for docs authored before the refs slot existed.
    parting_sels = refs.get("parting") or p.get("parting") or []
    if not isinstance(parting_sels, list) or not parting_sels:
        raise FeatureError(
            "draft: refs.parting (a list of edge selectors naming the parting line) is "
            "required for parting_line / step draft",
            dangling_refs=["parting: missing"])

    parting_edges = _resolve_parting_edges(target, parting_sels, diag)

    step = (mode == "step")
    # Step distance: explicit params.step, else a small fraction of the body diagonal.
    step_dist = ctx.dim(p, "step", max(diag * 0.05, 1.0)) if step else 0.0

    result, origin, pull_u, in_plane = _parting_line_draft(
        target, faces, parting_edges, pull, angle_deg, diag,
        step=step, step_dist=step_dist)

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(
        bodies=bodies, tool_body=None,
        resolved={"angle": angle_deg,
                  "_mode_parting": 1.0 if mode == "parting_line" else 0.0,
                  "_mode_step": 1.0 if step else 0.0,
                  "_part_x": float(origin.X), "_part_y": float(origin.Y),
                  "_part_z": float(origin.Z),
                  "_step": float(step_dist)},
    )
