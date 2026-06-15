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

SolidWorks-parity options (SW_PARITY_SPEC §2 Rib)
-------------------------------------------------
* **Explicit drop / extrusion direction** (``thicken="parallel"`` only) — by default the in-plane
  drop direction is *inferred* (the perpendicular pointing at the body centre). SolidWorks lets you
  flip / pick it. We honour an explicit pick when given:
    - ``params.drop_dir`` — a literal 3-vector ``[x,y,z]`` (or ``{"x","y","z"}``) the contour drops
      along. It is projected onto the sketch plane (the rib silhouette must stay planar) and used
      verbatim; ``params.drop_flip`` reverses it.
    - ``params.drop_ref`` — a *direction reference* (datum axis/plane, linear model edge, or planar
      face normal), resolved through the pattern direction machinery and likewise in-plane-projected.
  When neither is given the legacy inference is used unchanged.
* **Extrusion-direction mode** ``params.thicken`` — SolidWorks' Rib has two ways the material grows
  relative to the sketch plane:
    - ``"parallel"`` (default, legacy) grows the rib PARALLEL TO THE SKETCH: the contour drops along
      an IN-PLANE direction and is thickened ACROSS the sketch-plane normal (the legacy rib; the
      explicit drop pick above applies here).
    - ``"normal"`` grows the rib NORMAL TO THE SKETCH: the contour drops along the SKETCH-PLANE
      NORMAL (perpendicular to the sketch) and is thickened IN-PLANE (perpendicular to the contour) —
      SolidWorks' "Normal to Sketch" rib, for a contour drawn on/near a face whose rib grows straight
      off the sketch plane. ``side`` (both/one) + ``draft`` + ``flip`` apply in both modes.
"""

from __future__ import annotations

from typing import Any

from build123d import Edge, Face, Mode, Solid, Vector
from build123d import extrude as b3d_extrude

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized
from .pattern import _resolve_direction

_VALID_SIDES = ("both", "one")
_VALID_THICKEN = ("parallel", "normal")


def _vec_from_param(v: Any) -> Vector | None:
    """Coerce a literal direction param (``[x,y,z]`` list/tuple or ``{"x","y","z"}`` dict) to a
    Vector, or return None if it is not a literal vector (so a ref can be tried instead)."""
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(
            isinstance(c, (int, float)) and not isinstance(c, bool) for c in v):
        return Vector(float(v[0]), float(v[1]), float(v[2]))
    if isinstance(v, dict) and all(k in v for k in ("x", "y", "z")):
        try:
            return Vector(float(v["x"]), float(v["y"]), float(v["z"]))
        except Exception:  # noqa: BLE001
            return None
    return None


def _project_into_plane(vec: Vector, normal: Vector) -> Vector:
    """Project ``vec`` onto the plane with the given unit ``normal`` (drop the out-of-plane part)."""
    n = normal.normalized()
    return vec - n * vec.dot(n)


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
    thicken = p.get("thicken", "parallel")
    if thicken not in _VALID_THICKEN:
        raise FeatureError(
            f"rib: unknown thicken '{thicken}' (expected 'parallel' or 'normal')")
    drop_flip = bool(p.get("drop_flip", False))

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

    # Drop / extrusion direction + thickening axis. SolidWorks' Rib has two *extrusion-direction*
    # modes (``params.thicken``) that decide how the material grows relative to the sketch plane:
    #
    #   - "parallel" (default, legacy): the rib material grows PARALLEL TO THE SKETCH — the contour
    #     is dropped along an IN-PLANE direction (perpendicular to the contour, toward the body) and
    #     thickened ACROSS the sketch-plane normal.
    #   - "normal": the rib material grows NORMAL TO THE SKETCH — the contour is dropped along the
    #     SKETCH-PLANE NORMAL (perpendicular to the sketch) and thickened IN-PLANE (perpendicular to
    #     the contour). This is SW's "Normal to Sketch" rib, used when the contour is drawn on/near a
    #     face and the rib grows straight off the sketch plane.
    #
    # ``drop_dir`` / ``drop_ref`` give an EXPLICIT drop pick (parallel mode only — it is projected
    # into the sketch plane); ``drop_flip`` reverses whichever drop direction is used.
    explicit: Vector | None = None
    if p.get("drop_dir") is not None:
        explicit = _vec_from_param(p.get("drop_dir"))
        if explicit is None:
            raise FeatureError(
                "rib: params.drop_dir must be a 3-vector [x,y,z] (or {x,y,z})")
    elif p.get("drop_ref") is not None:
        try:
            explicit = _resolve_direction(p.get("drop_ref"), ctx)
        except FeatureError as exc:
            raise FeatureError(f"rib: drop direction reference did not resolve: {exc}",
                               dangling_refs=["drop_ref: " + str(exc)])

    bc = body.bounding_box().center()
    mid = (a + b) * 0.5
    toward = Vector(bc.X, bc.Y, bc.Z) - mid

    if thicken == "normal":
        # Drop straight off the sketch plane (along its normal); orient toward the body, thicken
        # in-plane perpendicular to the contour. An explicit drop pick is not meaningful here (the
        # drop is fixed to the sketch normal); honour only the flip.
        if explicit is not None:
            raise FeatureError(
                "rib: params.drop_dir/drop_ref only apply to thicken='parallel' (in 'normal' mode "
                "the rib drops along the sketch normal)")
        perp = Vector(normal)
        if perp.dot(toward) < 0.0:
            perp = perp * -1.0
        if drop_flip:
            perp = perp * -1.0
        t_axis = normal.cross(cdir)            # in-plane, perpendicular to the contour
        if t_axis.length < 1e-9:
            t_axis = normal.cross(perp)
        if t_axis.length < 1e-9:
            t_axis = Vector(normal)
        t_axis = t_axis.normalized()
    elif explicit is not None:
        # Explicit in-plane drop pick (parallel mode).
        perp = _project_into_plane(explicit, normal)
        if perp.length < 1e-9:
            raise FeatureError(
                "rib: the picked drop direction is parallel to the sketch normal "
                "(it has no in-plane component to drop the rib along)")
        perp = perp.normalized()
        if drop_flip:
            perp = perp * -1.0
        t_axis = normal
    else:
        # Legacy inference: in-plane, perpendicular to the contour, pointing toward the body interior.
        perp = normal.cross(cdir)
        if perp.length < 1e-9:
            raise FeatureError(
                "rib: could not derive a drop direction (contour parallel to normal?)")
        perp = perp.normalized()
        if perp.dot(toward) < 0.0:
            perp = perp * -1.0
        if drop_flip:
            perp = perp * -1.0
        t_axis = normal

    diag = _body_bbox_diag(body)
    reach = diag * 1.5

    # Build the rib slab. For each contour edge: extrude it along the drop direction into a
    # SILHOUETTE face (the rib's side profile, in the sketch plane), then THICKEN that face along
    # the thickening axis to `thickness`. Per-edge handling (vs one Wire) is what makes an open
    # line OR an open line/arc chain work — `Face.extrude` accepts an Edge, not a multi-edge Wire.
    taper = draft if abs(draft) > 1e-9 else 0.0

    def _thicken(sil_face) -> Any:
        if side == "one":
            tdir = t_axis * (-1.0 if flip else 1.0)
            return b3d_extrude(sil_face, amount=thickness, dir=tdir, taper=taper, mode=Mode.PRIVATE)
        half = thickness / 2.0
        s_fwd = b3d_extrude(sil_face, amount=half, dir=t_axis, taper=taper, mode=Mode.PRIVATE)
        s_rev = b3d_extrude(sil_face, amount=half, dir=t_axis * -1.0, taper=taper, mode=Mode.PRIVATE)
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
                  "_rib_volume": float(new_vol - old_vol),
                  # Echo resolved drop direction + thicken mode so callers / tests can verify the
                  # explicit pick was honoured (1.0 = an explicit drop dir was used).
                  "_drop_x": float(perp.X), "_drop_y": float(perp.Y), "_drop_z": float(perp.Z),
                  "_thicken_normal": 1.0 if thicken == "normal" else 0.0,
                  "_drop_explicit": 1.0 if explicit is not None else 0.0},
    )
