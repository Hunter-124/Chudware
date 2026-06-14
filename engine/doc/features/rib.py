"""Rib feature compiler — a thin wall added between existing walls from an OPEN sketch contour.

feature_params.md §Stretch > rib.

A rib in SolidWorks is a thin support wall grown from an OPEN sketch contour (a line, or a chain
of lines/arcs) drawn near the gap it bridges. The contour is NOT a closed region; it is the
*silhouette trace* of the rib's top. Material is grown DOWN from that trace (parallel to the sketch
plane, toward the existing body) and THICKENED across the sketch plane to ``thickness``, then fused
to the body and trimmed to the body's envelope so the rib fills cleanly between the walls.

Geometry (robust v1 — gets a real connected rib on the common case: a line bridging two wall tops)
--------------------------------------------------------------------------------------------------
1. Resolve the OPEN contour from the referenced sketch (``refs.profile`` is a region selector that
   names a sketch; we read that sketch's raw ``all_edges`` + ``plane`` and chain them into one open
   wire — closed contours are rejected, a rib needs an open one).
2. **Drop direction** = the in-plane direction perpendicular to the contour that points TOWARD the
   existing body (its bounding-box centre). The contour is extruded along it by a generous reach to
   build the rib SILHOUETTE face (the rib's side profile, lying in the sketch plane).
3. **Thicken** the silhouette by ``thickness`` along the sketch-plane NORMAL — centred for
   ``side="both"`` (½ each way), one-sided for ``side="one"``. ``draft`` (deg) tapers the rib sides
   via build123d ``extrude(..., taper=)``. ``flip`` flips the one-sided direction.
4. **Trim** the thickened slab to the body's bounding box (so the down-extrude doesn't dangle past
   the part) and FUSE it to the running body.

Result: one connected solid; the rib touches the walls it bridges (fuse-verified by a volume
increase). Limitation: the contour must be planar + open and its drop direction must actually reach
the body (a contour drawn away from the part adds a dangling rib → caught as "added no material").
Closed profile / no body / non-intersecting are contained :class:`FeatureError`.
"""

from __future__ import annotations

from typing import Any

from build123d import Edge, Face, Mode, Solid, Vector
from build123d import extrude as b3d_extrude

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized

_VALID_SIDES = ("both", "one")


def _resolve_open_contour(sel: Any, ctx: CompileContext) -> tuple[list[Edge], Any]:
    """Resolve ``refs.profile`` to (lifted-3D contour edges, sketch Plane).

    A rib's profile is a region selector that names a sketch (``{"kind":"region","sketch":fid}``);
    unlike extrude we IGNORE closed-region detection and read the sketch's raw open contour
    (``all_edges`` in XY sketch space) so an OPEN line/chain is usable. Edges are lifted into the
    sketch plane's 3-D frame and returned with the plane.
    """
    if not isinstance(sel, dict) or sel.get("kind") != "region":
        raise FeatureError(
            "rib: refs.profile must be a sketch region selector naming the rib's open contour",
            dangling_refs=["profile: not a region selector"],
        )
    sketch_id = sel.get("sketch")
    if not sketch_id:
        raise FeatureError("rib: profile region selector missing 'sketch' field",
                           dangling_refs=["profile: region selector has no sketch"])
    store = ctx.datums.get("_sketch_regions")
    sketch_data = (store.obj.get(sketch_id) if store is not None else None)
    if sketch_data is None:
        raise FeatureError(
            f"rib: sketch '{sketch_id}' not compiled (is the rib AFTER its sketch?)",
            dangling_refs=[f"profile: sketch '{sketch_id}' not found"],
        )
    raw_edges = sketch_data.get("all_edges") or []
    plane = sketch_data.get("plane")
    if not raw_edges or plane is None:
        raise FeatureError("rib: the referenced sketch has no contour edges",
                           dangling_refs=["profile: sketch has no edges"])
    lifted = [plane.location * e for e in raw_edges]
    return lifted, plane


def _is_contour_closed(edges: list[Edge]) -> bool:
    """A contour is closed if it is a single closed curve (circle) or its endpoints chain into a
    loop (start of the chain coincides with its end)."""
    if len(edges) == 1:
        try:
            return edges[0].is_closed
        except Exception:  # noqa: BLE001
            pass
    pts: list[Vector] = []
    for e in edges:
        vs = e.vertices()
        if len(vs) >= 2:
            pts.append(Vector(vs[0].X, vs[0].Y, vs[0].Z))
            pts.append(Vector(vs[-1].X, vs[-1].Y, vs[-1].Z))
    if len(pts) < 4:
        return False
    # Count free endpoints (appear once). An open chain has exactly two; a loop has none.
    free = 0
    for i, p in enumerate(pts):
        if not any(j != i and (p - q).length < 1e-6 for j, q in enumerate(pts)):
            free += 1
    return free == 0


def _contour_endpoints(edges: list[Edge]) -> tuple[Vector, Vector]:
    """The two free endpoints of an open contour chain (the ends of the bridge line)."""
    pts: list[Vector] = []
    for e in edges:
        vs = e.vertices()
        if len(vs) >= 2:
            pts.append(Vector(vs[0].X, vs[0].Y, vs[0].Z))
            pts.append(Vector(vs[-1].X, vs[-1].Y, vs[-1].Z))
    free: list[Vector] = []
    for i, p in enumerate(pts):
        if not any(j != i and (p - q).length < 1e-6 for j, q in enumerate(pts)):
            free.append(p)
    if len(free) >= 2:
        return free[0], free[-1]
    # Fallback: first/last edge endpoints.
    return pts[0], pts[-1]


@register("rib")
def compile_rib(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    thickness = ctx.dim(p, "thickness", 3.0)
    if thickness <= 0.0:
        raise FeatureError("rib: thickness must be > 0")
    side = p.get("side", "both")
    if side not in _VALID_SIDES:
        raise FeatureError(f"rib: unknown side '{side}' (expected 'both' or 'one')")
    draft = ctx.dim(p, "draft", 0.0) if p.get("draft") is not None else 0.0
    flip = bool(p.get("flip", False))

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("rib: needs an existing body to grow the rib against",
                           hint="add a base/walls before the rib")
    body = bodies[-1]

    prof = refs.get("profile")
    # The PM emits a single region selector; accept a 1-list too (parity with extrude wells).
    if isinstance(prof, list):
        if not prof:
            raise FeatureError("rib: refs.profile is required",
                               dangling_refs=["profile: empty"])
        prof = prof[0]
    if prof is None:
        raise FeatureError("rib: refs.profile is required",
                           dangling_refs=["profile: missing"])

    contour, plane = _resolve_open_contour(prof, ctx)

    if _is_contour_closed(contour):
        raise FeatureError(
            "rib: the sketch contour is CLOSED — a rib needs an OPEN contour (a line or chain)",
            hint="use an open line bridging the walls; a closed region is an extrude/boss, not a rib",
        )

    # Contour tangent (chord direction) + sketch-plane normal.
    a, b = _contour_endpoints(contour)
    chord = b - a
    if chord.length < 1e-9:
        raise FeatureError("rib: contour is degenerate (zero length)")
    cdir = chord.normalized()
    normal = Vector(plane.z_dir).normalized()

    # Drop direction: in-plane, perpendicular to the contour, pointing toward the body interior.
    perp = normal.cross(cdir)
    if perp.length < 1e-9:
        raise FeatureError("rib: could not derive a drop direction (contour parallel to normal?)")
    perp = perp.normalized()
    bc = body.bounding_box().center()
    mid = (a + b) * 0.5
    toward = Vector(bc.X, bc.Y, bc.Z) - mid
    if perp.dot(toward) < 0.0:
        perp = perp * -1.0

    diag = _body_bbox_diag(body)
    reach = diag * 1.5

    # Build the rib slab. For each contour edge: extrude it along the drop direction into a
    # SILHOUETTE face (the rib's side profile, in the sketch plane), then THICKEN that face across
    # the sketch-plane normal to `thickness`. Per-edge handling (vs one Wire) is what makes an open
    # line OR an open line/arc chain work — `Face.extrude` accepts an Edge, not a multi-edge Wire.
    taper = draft if abs(draft) > 1e-9 else 0.0

    def _thicken(sil_face) -> Any:
        if side == "one":
            tdir = normal * (-1.0 if flip else 1.0)
            return b3d_extrude(sil_face, amount=thickness, dir=tdir, taper=taper, mode=Mode.PRIVATE)
        half = thickness / 2.0
        s_fwd = b3d_extrude(sil_face, amount=half, dir=normal, taper=taper, mode=Mode.PRIVATE)
        s_rev = b3d_extrude(sil_face, amount=half, dir=normal * -1.0, taper=taper, mode=Mode.PRIVATE)
        return s_fwd + s_rev

    slab: Any = None
    for edge in contour:
        try:
            sil = Face.extrude(edge, perp * reach)
        except Exception:  # noqa: BLE001
            continue  # degenerate edge — skip it
        try:
            piece = _thicken(sil)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"rib: could not thicken the rib wall: {exc}",
                               hint="try a smaller thickness or draft")
        slab = piece if slab is None else (slab + piece)
    if slab is None:
        raise FeatureError("rib: could not build the rib silhouette from the contour",
                           hint="the contour must be a planar open curve")
    slab = _to_solid_normalized(slab)

    # Trim the slab to the body's bounding box so the down-extrude doesn't dangle past the part.
    bb = body.bounding_box()
    clip = Solid.make_box(
        (bb.max.X - bb.min.X) + 10.0,
        (bb.max.Y - bb.min.Y) + 10.0,
        (bb.max.Z - bb.min.Z) + 10.0,
    ).translate(Vector(bb.min.X - 5.0, bb.min.Y - 5.0, bb.min.Z - 5.0))
    try:
        slab = _to_solid_normalized(slab.intersect(clip))
    except Exception:  # noqa: BLE001
        pass  # keep the untrimmed slab; the fuse below still works

    if getattr(slab, "volume", 0.0) <= 1e-9:
        raise FeatureError(
            "rib: the rib has no material inside the part envelope",
            hint="draw the rib contour so it bridges/overlaps the existing walls",
        )

    old_vol = getattr(body, "volume", 0.0)
    try:
        result = _to_solid_normalized(body + slab)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"rib: fusing the rib to the body failed: {exc}",
                           hint="the rib contour may not reach the body")

    new_vol = getattr(result, "volume", 0.0)
    if new_vol - old_vol <= 1e-6:
        raise FeatureError(
            "rib added no material — the contour does not bridge the body",
            hint="draw the rib so it spans between the walls and reaches down to the base",
        )

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, body)
    return CompileOutput(
        bodies=bodies, tool_body=slab, op="add",
        resolved={"thickness": thickness, "draft": taper,
                  "_rib_volume": float(new_vol - old_vol)},
    )
