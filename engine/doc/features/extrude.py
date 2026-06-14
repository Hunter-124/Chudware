"""Extrude feature compiler — boss/cut, blind/through_all/mid_plane/up_to_next + the
SolidWorks geometry-driven end conditions, full Dir2, thin feature, and merge/new-body.

feature_params.md §Sketched features > extrude.

Profile source:
- ``{"kind":"region","sketch":fid,...}`` → resolved via ``ctx._sketch_regions[fid]``
  using ``resolve_region``.
- ``{"kind":"face",...}`` → resolved via describe_faces + resolve (face selector).

End conditions (direction 1 = first profile face normal, flipped by ``flip``):
- ``blind`` = ``depth`` along the direction.
- ``through_all`` = 1.05× combined bounding-box extent in the extrude direction.
- ``mid_plane`` = ±depth/2 each side (SolidWorks: ``depth`` is the TOTAL height).
- ``up_to_next`` = library ``extrude(..., until=Until.NEXT, target=body)`` — extrudes
  until the first surface of the existing body along the direction. Requires a body.
- ``up_to_surface`` = extrude until the profile reaches a referenced FACE. The target is
  read from ``params.up_to`` (a face selector) — see below. For a planar target the stop
  distance is computed analytically (ray/plane intersection) and realized as a blind
  extrude; for a non-planar target we fall back to a long column trimmed by the target's
  owning body. Requires a body to resolve the face selector against.
- ``offset_from_surface`` = ``up_to_surface`` plus a signed ``offset`` (``params.offset``)
  along the extrude direction (positive = past the surface, negative = short of it).
- ``up_to_body`` = extrude until the profile passes through a referenced BODY — the column
  is trimmed against the body and the slab running from the sketch plane to the body's FAR
  surface along the direction is kept (distinct from ``up_to_next``, which stops at the
  NEAR surface). The body is the running body, or the body owning ``params.up_to``'s face.
- ``up_to_vertex`` = extrude until the plane through a referenced VERTEX, perpendicular to
  the direction. Stop distance = projection of (vertex − profile_centre) onto the direction.
  The vertex is read from ``params.up_to`` (a vertex selector).

``params.up_to`` carries the target SELECTOR for the geometry-driven ends. It lives in
``params`` (not ``refs``) because the doc schema's ``extrude`` ref table is closed to the
single ``profile`` slot; a face/vertex selector is just a dict and resolves against the
running body with the same ``describe_* + resolve`` machinery the datum/hole compilers use.
(A dedicated ``refs.up_to`` slot would be cleaner — noted as a deferred schema change.)

Second direction (``end2`` != "none") builds a second tool from the same profiles along
the *negated* direction with its own end-condition logic (the FULL matrix: blind /
through_all / up_to_next / up_to_surface / offset_from_surface / up_to_body / up_to_vertex,
each reading ``params.up_to2`` / ``depth2`` / ``offset2``); both tools are applied in one
feature (boss: union both; cut: subtract both).

THIN feature (``params.thin`` truthy): the profile is extruded as a thin WALL of thickness
``params.thin_thickness`` instead of a filled solid. ``params.thin_side`` selects which way
the wall offsets from the profile loop: ``"one"`` (inside, default), ``"two"`` /
``"both"`` (½ thickness each side), or ``"mid"`` (alias of two-side). Implemented by
differencing an inset/outset offset of the profile region BEFORE extrusion, so it composes
with every end condition and with Dir2.

``merge`` (default True): when False the extrusion becomes a NEW BODY (appended to the body
list) instead of fusing with the running body — SolidWorks "Merge result" unticked. Cut
mode still requires an existing body and always edits it.

``draft`` is applied via build123d ``extrude(..., taper=)`` (``Solid.extrude_taper``).
Sign convention: SolidWorks default "draft inward" (profile narrows toward the extrude
direction) maps to POSITIVE taper; ``draft_outward=True`` flips to NEGATIVE taper (profile
grows). Draft applies to blind/mid_plane only — ignored (with a resolved note) for the
through_all / up_to_* geometry-driven ends, matching SolidWorks greying it out there.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import (
    Axis,
    Edge,
    Face,
    Kind,
    Mode,
    Plane,
    Solid,
    Until,
    Vector,
    Wire,
)
from build123d import extrude as b3d_extrude

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from engine.doc.selectors import (
    describe_faces,
    describe_vertices,
    resolve,
    resolve_region,
)

# --------------------------------------------------------------------------- topo owner store


def _store_topo_owners(ctx: CompileContext, fid: str, body, prev_body) -> None:
    """Compute face/edge topology diff and store owners in ctx.datums.

    Stores ``ctx.datums["_face_owners"]`` and ``ctx.datums["_edge_owners"]`` as
    Datum(kind="_topo_owners", obj=dict) where obj maps:
      ``{"<fid>": {"face": {1_based_idx: owner_fid}, "edge": {1_based_idx: owner_fid}}}``

    Entities absent from prev_body (or present with a new TShape) are tagged with fid.
    This mirrors the logic in compiler._diff_owners.
    """
    _KEY = "_topo_owners"
    if _KEY not in ctx.datums:
        ctx.datums[_KEY] = Datum(kind=_KEY, obj={}, feature_id="__sys__")
    store = ctx.datums[_KEY].obj

    from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def _tag_shape(topo_type, prev_shape):
        cur_map = TopTools_IndexedMapOfShape()
        if getattr(body, "wrapped", None) is not None:
            TopExp.MapShapes_s(body.wrapped, topo_type, cur_map)
        prev_map = TopTools_IndexedMapOfShape()
        if prev_shape is not None and getattr(prev_shape, "wrapped", None) is not None:
            TopExp.MapShapes_s(prev_shape.wrapped, topo_type, prev_map)

        owners: dict[int, str] = {}
        # Inherit previous owners if available
        prev_key = "face" if topo_type == TopAbs_FACE else "edge"
        prev_fid = _find_last_fid(store, prev_key)
        prev_owners_map: dict[int, str] = {}
        if prev_fid and prev_fid in store:
            prev_owners_map = store[prev_fid].get(prev_key, {})

        for i in range(1, cur_map.Extent() + 1):
            ent = cur_map.FindKey(i)
            if prev_map.Contains(ent):
                prev_idx = prev_map.FindIndex(ent)
                owners[i] = prev_owners_map.get(prev_idx, fid)
            else:
                owners[i] = fid
        return owners

    def _find_last_fid(store: dict, kind: str) -> str | None:
        # Find the most recently stored fid that has this kind
        for k in reversed(list(store.keys())):
            if kind in store[k]:
                return k
        return None

    face_owners = _tag_shape(TopAbs_FACE, prev_body)
    edge_owners = _tag_shape(TopAbs_EDGE, prev_body)
    store[fid] = {"face": face_owners, "edge": edge_owners}


def _get_topo_owners(ctx: CompileContext, kind: str = "face") -> dict[int, str]:
    """Get the most recently stored topo owners from ctx.datums."""
    _KEY = "_topo_owners"
    store_datum = ctx.datums.get(_KEY)
    if store_datum is None:
        return {}
    store = store_datum.obj
    if not store:
        return {}
    last_fid = list(store.keys())[-1]
    return store[last_fid].get(kind, {})


# --------------------------------------------------------------------------- helpers


def _body_bbox_diag(body) -> float:
    try:
        bb = body.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def _body_extent_along(body, direction: Vector) -> float:
    """Return total bbox extent along *direction* (unit vector)."""
    try:
        bb = body.bounding_box()
        corners = [
            Vector(bb.min.X, bb.min.Y, bb.min.Z),
            Vector(bb.max.X, bb.max.Y, bb.max.Z),
        ]
        projs = [direction.dot(c) for c in corners]
        return abs(projs[1] - projs[0])
    except Exception:  # noqa: BLE001
        return 100.0


def _resolve_profile_faces(
    sel: dict[str, Any], ctx: CompileContext
) -> list[Face]:
    """Resolve a single profile selector to one or more build123d Faces.

    Region selectors (``{"kind":"region","sketch":fid,...}``) resolve three ways:
    - **whole sketch** (no ``index``/``centroid``) → ALL *material* (even-depth) regions of the
      sketch, so a circle-in-a-square extrudes as a washer. This is the UI default and avoids the
      fragile centroid match that produced spurious "dangling reference" errors.
    - **indexed** (``index`` present) → that one region (the host emits a stable index per pick).
    - **centroid** (legacy) → nearest region via ``resolve_region``.

    Face selectors (``{"kind":"face",...}``) resolve via describe_faces against the running body.
    """
    kind = sel.get("kind")

    if kind == "region":
        sketch_id = sel.get("sketch")
        if not sketch_id:
            raise FeatureError("extrude profile: region selector missing 'sketch' field")
        _sketch_store = ctx.datums.get("_sketch_regions")
        sketch_data = (_sketch_store.obj.get(sketch_id) if _sketch_store is not None else None)
        if sketch_data is None:
            raise FeatureError(
                f"extrude profile: sketch '{sketch_id}' not compiled (wrong order?)"
            )
        descs = sketch_data["descs"]
        faces = sketch_data["faces"]
        if not faces:
            raise FeatureError(
                f"extrude profile: sketch '{sketch_id}' produced no closed-loop regions"
            )

        # Explicit, stable index from a UI pick.
        idx_val = sel.get("index")
        if isinstance(idx_val, (int, float)) and not isinstance(idx_val, bool):
            idx = int(idx_val)
            if idx < 0 or idx >= len(faces):
                raise FeatureError(
                    f"extrude profile: region index {idx} out of range (0..{len(faces) - 1})",
                    dangling_refs=[f"region index {idx} out of range (sketch has {len(faces)})"],
                )
            return [faces[idx]]

        # Legacy centroid resolution.
        if sel.get("centroid") is not None:
            try:
                coords = []
                for f in faces:
                    bb = f.bounding_box()
                    coords.extend([bb.min.X, bb.min.Y, bb.max.X, bb.max.Y])
                span = (max(coords) - min(coords)) if coords else 100.0
                sketch_diag = max(span * math.sqrt(2), 1e-3)
            except Exception:  # noqa: BLE001
                sketch_diag = 100.0
            rr = resolve_region(sel, descs, sketch_diag)
            if rr.index is None:
                raise FeatureError(
                    f"extrude profile: region selector dangling — {rr.dangling_reason}",
                    dangling_refs=[f"profile[?]: {rr.dangling_reason}"],
                )
            return [faces[rr.index]]

        # Whole sketch → all material (even-depth) regions; fall back to every region if the
        # nesting metadata is absent (older stored sketches).
        material = [f for f, d in zip(faces, descs) if d.get("is_material", True)]
        return material if material else list(faces)

    if kind == "face":
        bodies = ctx.bodies
        if not bodies:
            raise FeatureError("extrude profile: face selector given but no bodies")
        body = bodies[-1]
        infos = describe_faces(body.wrapped)
        diag = _body_bbox_diag(body)
        rr = resolve(sel, infos, diag)
        if rr.index is None:
            raise FeatureError(
                f"extrude profile: face selector dangling — {rr.dangling_reason}",
                dangling_refs=[f"profile[?]: {rr.dangling_reason}"],
            )
        return [body.faces()[rr.index]]

    raise FeatureError(
        f"extrude profile: unsupported selector kind '{kind}' (expected 'region' or 'face')"
    )


def _resolve_profile_selector(sel: dict[str, Any], ctx: CompileContext) -> Face:
    """Back-compat single-face shim (returns the first resolved face)."""
    return _resolve_profile_faces(sel, ctx)[0]


def _to_solid_normalized(shape) -> Any:
    """Normalize an extrude/revolve result to a single solid-like object."""
    try:
        solids = shape.solids()
    except Exception:  # noqa: BLE001
        return shape
    if len(solids) == 1:
        return solids[0]
    return shape


# --------------------------------------------------------------------------- extrude


_VALID_ENDS = (
    "blind", "through_all", "mid_plane", "up_to_next",
    "up_to_surface", "offset_from_surface", "up_to_body", "up_to_vertex",
)
_VALID_ENDS2 = (
    "none", "blind", "through_all", "up_to_next",
    "up_to_surface", "offset_from_surface", "up_to_body", "up_to_vertex",
)

# End conditions whose stop is GEOMETRY-driven (depend on a referenced target rather than a
# linear depth). Draft greys out for these (SolidWorks behavior), matching through_all.
_GEOMETRY_ENDS = ("up_to_next", "up_to_surface", "offset_from_surface",
                  "up_to_body", "up_to_vertex")


def _up_to_next_cut_tool(face: Face, direction: Vector, body) -> Any:
    """Cut "up to next": the SOLID material from the sketch plane to the next face.

    build123d ``Until.NEXT`` fills the next VOID (boss semantics) and isn't subtractable,
    so for a cut we extrude a full-length linear column, intersect it with the body, and
    keep the solid slab nearest the sketch plane along the cut direction.
    """
    reach = max(_body_extent_along(body, direction), _body_bbox_diag(body)) * 4.0
    column = Solid.extrude(face, Vector(direction) * reach)
    inside = body.intersect(column)
    try:
        slabs = inside.solids()
    except Exception:  # noqa: BLE001
        slabs = []
    if not slabs:
        raise FeatureError(
            "extrude: up_to_next cut removed no material",
            hint="the profile does not intersect the body along the cut direction",
        )
    # Nearest slab to the sketch plane along the cut direction.
    axis = Axis(face.center(), Vector(direction))
    nearest = slabs.sort_by(axis)[0]
    return nearest


# --------------------------------------------------------------------------- target resolution
#
# Geometry-driven ends (up_to_surface / offset_from_surface / up_to_body / up_to_vertex)
# need a target face/vertex. The doc schema's extrude ref table is CLOSED to the single
# ``profile`` slot, so the target selector is carried in ``params.up_to`` (Dir1) /
# ``params.up_to2`` (Dir2) — a plain selector dict resolved against the running body with
# the same describe_* + resolve machinery the datum/hole compilers use.


def _describe_pool(kind: str, pool: list) -> list:
    """One ``TopoInfo`` per deduplicated topology entity, index-aligned to *pool*.

    Mirrors ``datum._describe_pool``: describing each ``body.faces()`` / ``body.vertices()``
    entity individually yields one info per pickable entity (no shared-edge duplicates), so
    ``resolve`` returns a usable index into *pool*.
    """
    describer = {"face": describe_faces, "vertex": describe_vertices}[kind]
    infos: list = []
    for i, ent in enumerate(pool):
        sub = describer(ent.wrapped)
        if not sub:
            continue
        info = sub[0]
        info.index = i
        info.hint_index = i
        infos.append(info)
    return infos


def _resolve_target_entity(sel: Any, body, kind: str, slot: str):
    """Resolve a face/vertex selector to its build123d entity on *body*.

    Raises a contained :class:`FeatureError` (with ``dangling_refs``) when it does not
    resolve — same contract as the profile/datum resolvers.
    """
    if not isinstance(sel, dict):
        raise FeatureError(
            f"extrude: {slot} must be a {kind} selector object, got {sel!r}"
        )
    pool = {"face": body.faces, "vertex": body.vertices}[kind]()
    if not pool:
        raise FeatureError(f"extrude: {slot}: body has no {kind}s to resolve against")
    infos = _describe_pool(kind, pool)
    rr = resolve(sel, infos, _body_bbox_diag(body))
    if rr.index is None:
        raise FeatureError(
            f"extrude: {slot}: {kind} selector dangling — {rr.dangling_reason}",
            dangling_refs=[f"{slot}: {rr.dangling_reason}"],
        )
    return pool[rr.index]


def _face_plane(face: Face) -> tuple[Vector, Vector]:
    """(point on face, unit normal) for a planar face, robust to orientation."""
    try:
        c = face.center()
    except Exception:  # noqa: BLE001
        raise FeatureError("extrude: up_to_surface target face has no centre")
    try:
        n = face.normal_at()
    except Exception:  # noqa: BLE001
        raise FeatureError("extrude: up_to_surface target face has no normal")
    return Vector(c.X, c.Y, c.Z), Vector(n).normalized()


def _is_planar_face(face: Face) -> bool:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Plane
        return BRepAdaptor_Surface(face.wrapped).GetType() == GeomAbs_Plane
    except Exception:  # noqa: BLE001
        return False


def _up_to_surface_amount(profile: Face, target_face: Face, direction: Vector,
                          offset: float, *, label: str) -> float:
    """Signed distance to extrude so the profile reaches a PLANAR *target_face*.

    Solves the ray/plane intersection: from the profile centroid along *direction*, the
    distance t at which the ray hits the target plane is
    ``t = ((pf - pc) · n) / (direction · n)``. ``offset`` shifts the stop further along
    the direction (offset_from_surface). Raises if the surface is unreachable (parallel or
    behind the profile).
    """
    try:
        pc = profile.center()
        pc = Vector(pc.X, pc.Y, pc.Z)
    except Exception:  # noqa: BLE001
        pc = Vector(0, 0, 0)
    pf, n = _face_plane(target_face)
    denom = direction.dot(n)
    if abs(denom) < 1e-9:
        raise FeatureError(
            f"extrude: {label} surface is parallel to the extrude direction (unreachable)",
            hint="pick a face that the profile can actually reach along the direction",
        )
    t = (pf - pc).dot(n) / denom
    t += offset
    if t <= 1e-9:
        raise FeatureError(
            f"extrude: {label} surface is behind the profile along the direction "
            f"(distance {t:.3g} <= 0)",
            hint="flip the direction or pick a face ahead of the profile",
        )
    return t


def _up_to_vertex_amount(profile: Face, vertex_pt: Vector, direction: Vector,
                         *, label: str) -> float:
    """Distance so the profile reaches the plane through *vertex_pt* ⟂ to *direction*."""
    try:
        pc = profile.center()
        pc = Vector(pc.X, pc.Y, pc.Z)
    except Exception:  # noqa: BLE001
        pc = Vector(0, 0, 0)
    t = (vertex_pt - pc).dot(direction)
    if t <= 1e-9:
        raise FeatureError(
            f"extrude: {label} vertex is behind the profile along the direction "
            f"(distance {t:.3g} <= 0)",
            hint="flip the direction or pick a vertex ahead of the profile",
        )
    return t


def _up_to_body_tool(face: Face, direction: Vector, body) -> Any:
    """Extrude *face* until it passes THROUGH *body* — keep the slab from the sketch plane
    to the body's FAR surface along *direction* (distinct from up_to_next's near surface).

    A long column is built, intersected with the body to find where the body ends, and the
    near-plane→far-surface slab of the column is returned.
    """
    reach = max(_body_extent_along(body, direction), _body_bbox_diag(body)) * 4.0
    column = Solid.extrude(face, Vector(direction) * reach)
    inside = body.intersect(column)
    try:
        slabs = inside.solids()
    except Exception:  # noqa: BLE001
        slabs = []
    if not slabs:
        raise FeatureError(
            "extrude: up_to_body — profile does not intersect the target body",
            hint="the profile must project onto the body along the direction",
        )
    # The body's EXIT plane along the direction = the furthest intersection vertex projected
    # onto the direction, measured from the profile centre. A blind extrude of that length
    # fills the profile from the sketch plane through to the body's far surface.
    try:
        pc = Vector(face.center())
    except Exception:  # noqa: BLE001
        pc = Vector(0, 0, 0)
    far_dist = 0.0
    for slab in slabs:
        for v in slab.vertices():
            d = (Vector(v.X, v.Y, v.Z) - pc).dot(direction)
            far_dist = max(far_dist, d)
    if far_dist <= 1e-9:
        raise FeatureError(
            "extrude: up_to_body — target body is behind the profile along the direction",
            hint="flip the direction or pick a body ahead of the profile",
        )
    return Solid.extrude(face, Vector(direction) * far_dist)


# --------------------------------------------------------------------------- thin feature


def _thin_profile_face(face: Face, thickness: float, side: str, *, label: str) -> Face:
    """Convert a filled profile *face* into a thin-WALL ring face of *thickness*.

    ``side`` ∈ ``one`` (inset, wall inside the loop, the SolidWorks default) |
    ``two``/``both``/``mid`` (½ thickness each side, straddling the loop) | ``outside``
    (wall grows outward). The wall is the annulus between an OUTER and an INNER offset of
    the profile's perimeter wire, built as a single ``Face(outer_wire, inner_wires=[inner])``
    so the inner offset becomes a hole — composes with every end condition and Dir2.
    """
    if thickness <= 1e-9:
        raise FeatureError(
            f"extrude: thin feature needs a positive thickness (got {thickness:.3g})"
        )
    side = (side or "one").lower()
    if side in ("two", "both", "mid"):
        outer_off, inner_off = thickness / 2.0, -thickness / 2.0
    elif side in ("one", "inside", "inner"):
        outer_off, inner_off = 0.0, -thickness
    elif side in ("outside", "outer"):
        outer_off, inner_off = thickness, 0.0
    else:
        raise FeatureError(
            f"extrude: thin_side '{side}' unknown (one | two | mid | outside)"
        )

    try:
        perimeter = face.outer_wire()
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"extrude: thin feature ({label}) could not read the profile perimeter: {exc}"
        )

    def _offset_wire(off: float):
        if abs(off) < 1e-12:
            return perimeter
        # A CLOSED wire offsets concentrically (outward for +, inward for −). Kind.INTERSECTION
        # keeps sharp corners (true offset of a polygon) instead of rounding them.
        w = perimeter.offset_2d(off, kind=Kind.INTERSECTION)
        return w.wires()[0] if hasattr(w, "wires") and w.wires() else w

    try:
        outer_w = _offset_wire(outer_off)
        inner_w = _offset_wire(inner_off)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"extrude: thin feature ({label}) offset failed: {exc}",
            hint="the wall thickness may be too large for the profile",
        )

    try:
        ring = Face(outer_w, inner_wires=[inner_w])
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"extrude: thin feature ({label}) ring build failed: {exc}",
            hint="reduce the wall thickness",
        )
    if getattr(ring, "area", 0.0) <= 1e-9:
        raise FeatureError(
            f"extrude: thin feature ({label}) produced an empty wall",
            hint="reduce the wall thickness",
        )
    return ring


def _build_tool_for_dir(
    face: Face, end_cond: str, amount: float, direction: Vector, taper: float,
    bodies: list, fid: str, *, label: str, mode: str = "boss",
    up_to_body_target=None,
) -> Any:
    """Build a single-direction extrude tool for one profile face.

    ``end_cond`` ∈ blind | through_all | up_to_next | up_to_body. The geometry-driven
    ``up_to_surface`` / ``offset_from_surface`` / ``up_to_vertex`` ends are resolved to a
    linear ``amount`` by the caller and arrive here as ``blind`` (so they share the blind
    path). ``amount`` is the linear distance for blind/through_all (ignored for up_to_next /
    up_to_body). ``taper`` (deg) applies to blind only. ``mode`` selects the up_to_next
    realization (boss = library Until.NEXT void-fill; cut = nearest solid slab to subtract).
    ``up_to_body_target`` is the body the ``up_to_body`` column is trimmed against.
    """
    if end_cond == "up_to_next":
        if not bodies:
            raise FeatureError(
                "extrude: up_to_next requires an existing body",
                hint="add a base feature before extruding up to next",
            )
        if mode == "cut":
            return _up_to_next_cut_tool(face, direction, bodies[-1])
        try:
            tool = b3d_extrude(
                face, until=Until.NEXT, target=bodies[-1], dir=direction,
                mode=Mode.PRIVATE,
            )
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"extrude: up_to_next ({label}) build failed: {exc}",
                hint="the profile must project onto a face of the existing body along the direction",
            )
        return tool

    if end_cond == "up_to_body":
        target = up_to_body_target if up_to_body_target is not None else (
            bodies[-1] if bodies else None)
        if target is None:
            raise FeatureError(
                "extrude: up_to_body requires an existing body",
                hint="add a base feature before extruding up to a body",
            )
        return _up_to_body_tool(face, direction, target)

    try:
        if end_cond == "blind" and abs(taper) > 1e-9:
            tool = b3d_extrude(face, amount=amount, dir=direction, taper=taper,
                               mode=Mode.PRIVATE)
        else:
            tool = b3d_extrude(face, amount=amount, dir=direction, mode=Mode.PRIVATE)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"extrude: build failed ({label}): {exc}",
            hint="check that the profile is a closed region in the extrude plane",
        )
    return tool


@register("extrude")
def compile_extrude(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    end = p.get("end", "blind")
    end2 = p.get("end2", "none")
    flip = p.get("flip", False)
    merge = p.get("merge", True)

    if end not in _VALID_ENDS:
        raise FeatureError(f"extrude: unknown end '{end}'")
    if end2 not in _VALID_ENDS2:
        raise FeatureError(f"extrude: unknown end2 '{end2}'")

    depth = ctx.dim(p, "depth", 10.0)    # blind & mid_plane (dir 1)
    depth2 = ctx.dim(p, "depth2", 10.0)  # blind (dir 2)

    # Draft → taper (degrees). SW "draft inward" (default) = positive taper (narrows);
    # draft_outward flips the sign. Applies to blind/mid_plane only.
    draft_deg = ctx.dim(p, "draft", 0.0) if p.get("draft") is not None else 0.0
    draft_outward = bool(p.get("draft_outward", False))
    taper = -draft_deg if draft_outward else draft_deg
    draft_ignored = False  # set true when end condition greys out draft

    # Resolve profile faces
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("extrude: refs.profile is required")

    profile_faces: list[Face] = []
    dangling: list[str] = []
    for i, sel in enumerate(profile_sels):
        try:
            profile_faces.extend(_resolve_profile_faces(sel, ctx))
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"profile[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"profile[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"extrude: {len(dangling)} profile selector(s) failed",
            dangling_refs=dangling,
        )
    if not profile_faces:
        raise FeatureError("extrude: no profiles resolved")

    bodies = list(ctx.bodies)

    # --- thin feature: replace each filled profile with its thin-wall ring face ----------
    # ``thin`` makes the extrude a wall of ``thin_thickness`` rather than a solid; ``thin_side``
    # = one (inside, default) | two/mid (½ each side) | outside. Done at the FACE level so it
    # composes with every end condition and Dir2 below.
    thin = bool(p.get("thin", False))
    thin_thk = ctx.dim(p, "thin_thickness", 1.0) if p.get("thin_thickness") is not None else 1.0
    thin_side = p.get("thin_side", "one")
    if thin:
        profile_faces = [
            _thin_profile_face(f, thin_thk, thin_side, label="thin")
            for f in profile_faces
        ]

    # Compute extrude direction from the first profile face's normal
    face0 = profile_faces[0]
    try:
        normal = face0.normal_at()
    except Exception:  # noqa: BLE001
        normal = Vector(0, 0, 1)

    if flip:
        normal = normal * -1.0

    fid = feat.get("id", "?")

    def _linear_amount(end_cond: str, blind_depth: float, dir_vec: Vector) -> float:
        """Linear distance for blind/through_all. up_to_next/up_to_body ignore amount."""
        if end_cond == "blind":
            return blind_depth
        if end_cond == "through_all":
            if bodies:
                extent = _body_extent_along(bodies[-1], dir_vec)
            else:
                try:
                    bb = face0.bounding_box()
                    dx = bb.max.X - bb.min.X
                    dy = bb.max.Y - bb.min.Y
                    dz = bb.max.Z - bb.min.Z
                    extent = math.sqrt(dx * dx + dy * dy + dz * dz) * 2
                except Exception:  # noqa: BLE001
                    extent = 100.0
            return 1.05 * extent
        return 0.0

    # --- geometry-driven up-to ends: resolve the target + reduce to a linear amount ------
    # up_to_surface / offset_from_surface need a target FACE; up_to_vertex needs a target
    # VERTEX; both come from params.up_to (Dir1) / params.up_to2 (Dir2) — a selector resolved
    # against the running body. up_to_body trims a column against the target body and is
    # handled inside _build_tool_for_dir, so it produces no linear amount here.
    offset1 = ctx.dim(p, "offset", 0.0) if p.get("offset") is not None else 0.0
    offset2_v = ctx.dim(p, "offset2", 0.0) if p.get("offset2") is not None else 0.0
    up_to_body_target = None  # body for up_to_body (Dir1 or Dir2); default = running body

    def _geometry_amount(end_cond: str, dir_vec: Vector, up_sel: Any, offset: float,
                         label: str) -> float:
        """Linear amount for up_to_surface / offset_from_surface / up_to_vertex."""
        if not bodies:
            raise FeatureError(
                f"extrude: {end_cond} ({label}) requires an existing body to reference",
                hint="add a base feature before extruding up to a surface/vertex",
            )
        if up_sel is None:
            raise FeatureError(
                f"extrude: {end_cond} ({label}) needs a target — set params."
                f"{'up_to2' if label == 'dir2' else 'up_to'} to a face/vertex selector",
            )
        body = bodies[-1]
        if end_cond in ("up_to_surface", "offset_from_surface"):
            tface = _resolve_target_entity(up_sel, body, "face",
                                           "up_to2" if label == "dir2" else "up_to")
            if not _is_planar_face(tface):
                raise FeatureError(
                    f"extrude: {end_cond} ({label}) target is non-planar — only planar "
                    "up-to-surface targets are supported analytically",
                    hint="pick a planar face, or use up_to_body for a curved boundary",
                )
            off = offset if end_cond == "offset_from_surface" else 0.0
            return _up_to_surface_amount(face0, tface, dir_vec, off, label=end_cond)
        # up_to_vertex
        v = _resolve_target_entity(up_sel, body, "vertex",
                                   "up_to2" if label == "dir2" else "up_to")
        vp = Vector(v.X, v.Y, v.Z)
        return _up_to_vertex_amount(face0, vp, dir_vec, label=end_cond)

    _SURFACE_ENDS = ("up_to_surface", "offset_from_surface", "up_to_vertex")

    # --- direction-1 amount (or mid_plane half-depth) ---
    if end == "mid_plane":
        amount = depth / 2.0  # SW: depth = TOTAL height, half each side
    elif end in _SURFACE_ENDS:
        amount = _geometry_amount(end, normal, p.get("up_to"), offset1, "dir1")
        if abs(taper) > 1e-9:
            draft_ignored = True
    elif end == "up_to_body":
        amount = 0.0  # the tool is built by trimming a column, not by a linear depth
        if abs(taper) > 1e-9:
            draft_ignored = True
        if not bodies:
            raise FeatureError(
                "extrude: up_to_body requires an existing body",
                hint="add a base feature before extruding up to a body",
            )
        # The target is the running body (this codebase keeps a single running solid; there
        # is no body registry exposed to compilers). An optional params.up_to face selector
        # is resolved only to VALIDATE the pick names a real face of that body.
        if p.get("up_to") is not None:
            _resolve_target_entity(p.get("up_to"), bodies[-1], "face", "up_to")
        up_to_body_target = bodies[-1]
    else:
        amount = _linear_amount(end, depth, normal)
        if end in ("through_all", "up_to_next") and abs(taper) > 1e-9:
            draft_ignored = True  # SW greys draft here
    if end not in ("up_to_next", "up_to_body") and amount <= 0.0:
        raise FeatureError(f"extrude: effective depth {amount:.3g} must be > 0")

    # Surface/vertex/body ends arrive at _build_tool_for_dir as a blind extrude of the
    # resolved amount (surface/vertex) or as their own column tool (up_to_body).
    end1_eff = "blind" if end in _SURFACE_ENDS else end

    # Taper applies to blind/mid_plane only.
    taper1 = taper if (end in ("blind", "mid_plane") and not draft_ignored) else 0.0

    # --- direction-2 amount ---
    amount2 = 0.0
    end2_eff = end2
    if end2 != "none":
        if end2 in _SURFACE_ENDS:
            amount2 = _geometry_amount(end2, normal * -1.0, p.get("up_to2"),
                                       offset2_v, "dir2")
            end2_eff = "blind"
            if abs(taper) > 1e-9:
                draft_ignored = True
        elif end2 == "up_to_body":
            amount2 = 0.0
            if abs(taper) > 1e-9:
                draft_ignored = True
        else:
            amount2 = _linear_amount(end2, depth2, normal * -1.0)
            if end2 in ("through_all", "up_to_next") and abs(taper) > 1e-9:
                draft_ignored = True
        if end2 not in ("up_to_next", "up_to_body") and amount2 <= 0.0:
            raise FeatureError(f"extrude: effective depth2 {amount2:.3g} must be > 0")

    # Build per-profile tools. Each profile contributes a dir-1 tool (and a dir-2 tool
    # when end2 != none); we union the per-profile pieces into one tool_solid.
    tool_bodies: list[Any] = []
    for face in profile_faces:
        if end == "mid_plane":
            try:
                if abs(taper1) > 1e-9:
                    fn = face.normal_at()
                    if flip:
                        fn = fn * -1.0
                    t_fwd = b3d_extrude(face, amount=amount, dir=fn, taper=taper1,
                                        mode=Mode.PRIVATE)
                    t_rev = b3d_extrude(face, amount=amount, dir=fn * -1.0, taper=taper1,
                                        mode=Mode.PRIVATE)
                    piece = t_fwd + t_rev
                else:
                    piece = b3d_extrude(face, amount=amount, both=True, mode=Mode.PRIVATE)
            except Exception as exc:  # noqa: BLE001
                raise FeatureError(
                    f"extrude: mid_plane build failed: {exc}",
                    hint="check that the profile is a closed region in the extrude plane",
                )
            tool_bodies.append(piece)
            continue

        piece = _build_tool_for_dir(face, end1_eff, amount, normal, taper1, bodies, fid,
                                    label="dir1", mode=mode,
                                    up_to_body_target=up_to_body_target)
        if end2 != "none":
            piece2 = _build_tool_for_dir(face, end2_eff, amount2, normal * -1.0, taper1,
                                         bodies, fid, label="dir2", mode=mode,
                                         up_to_body_target=up_to_body_target)
            piece = piece + piece2
        tool_bodies.append(piece)

    # Union all per-profile tool pieces.
    tool_solid = tool_bodies[0]
    for tb in tool_bodies[1:]:
        tool_solid = tool_solid + tb
    tool_solid = _to_solid_normalized(tool_solid)

    resolved: dict[str, float | str] = {"depth": amount, "taper": taper1}
    if end2 != "none":
        resolved["depth2"] = amount2
    if thin:
        resolved["thin_thickness"] = thin_thk
    if end == "offset_from_surface":
        resolved["offset"] = offset1
    if end2 == "offset_from_surface":
        resolved["offset2"] = offset2_v
    if draft_ignored:
        resolved["_draft_note"] = (
            "draft ignored (not supported for through_all / up_to_* ends)")

    # Apply to the running body. merge=False (or no existing body) makes a NEW BODY: the
    # extrusion is appended to the body list instead of fusing — SolidWorks "Merge result"
    # unticked. Cut always edits the existing body.
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("extrude: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        _store_topo_owners(ctx, fid, tool_solid, None)
        return CompileOutput(bodies=bodies, tool_body=tool_solid, op="add", resolved=resolved)

    target = bodies[-1]
    try:
        if mode == "cut":
            result = target - tool_solid
        else:
            result = target + tool_solid
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"extrude: boolean {mode} failed: {exc}",
            hint="the profile may not intersect the body correctly",
        )

    result = _to_solid_normalized(result)

    if mode == "cut":
        old_vol = getattr(target, "volume", 0.0)
        new_vol = getattr(result, "volume", 0.0)
        if abs(new_vol - old_vol) < 1e-9:
            raise FeatureError(
                "extrude cut removed no material",
                hint="the profile does not intersect the target body",
            )

    bodies[-1] = result
    # Both-direction / single-direction share this one owner-store on the final result.
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(bodies=bodies, tool_body=tool_solid,
                         op=("cut" if mode == "cut" else "add"), resolved=resolved)
