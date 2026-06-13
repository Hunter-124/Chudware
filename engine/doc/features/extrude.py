"""Extrude feature compiler — boss/cut, blind/through_all/mid_plane/up_to_next.

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

Second direction (``end2`` != "none") builds a second tool from the same profiles along
the *negated* direction with its own end-condition logic; both tools are applied in one
feature (boss: union both; cut: subtract both).

``draft`` is applied via build123d ``extrude(..., taper=)`` (``Solid.extrude_taper``).
Sign convention: SolidWorks default "draft inward" (profile narrows toward the extrude
direction) maps to POSITIVE taper; ``draft_outward=True`` flips to NEGATIVE taper (profile
grows). Draft applies to blind/mid_plane only — ignored (with a resolved note) for
through_all/up_to_next, matching SolidWorks greying it out there.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import (
    Axis,
    Edge,
    Face,
    Mode,
    Plane,
    Solid,
    Until,
    Vector,
    Wire,
)
from build123d import extrude as b3d_extrude

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from engine.doc.selectors import describe_faces, resolve, resolve_region

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


_VALID_ENDS = ("blind", "through_all", "mid_plane", "up_to_next")
_VALID_ENDS2 = ("none", "blind", "through_all", "up_to_next")


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


def _build_tool_for_dir(
    face: Face, end_cond: str, amount: float, direction: Vector, taper: float,
    bodies: list, fid: str, *, label: str, mode: str = "boss",
) -> Any:
    """Build a single-direction extrude tool for one profile face.

    ``end_cond`` ∈ blind|through_all|up_to_next. ``amount`` is the linear distance for
    blind/through_all (ignored for up_to_next). ``taper`` (deg) applies to blind only.
    ``mode`` selects the up_to_next realization (boss = library Until.NEXT void-fill;
    cut = nearest solid slab to subtract).
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
        """Linear distance for blind/through_all. up_to_next ignores amount."""
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

    # --- direction-1 amount (or mid_plane half-depth) ---
    if end == "mid_plane":
        amount = depth / 2.0  # SW: depth = TOTAL height, half each side
    else:
        amount = _linear_amount(end, depth, normal)
        if end in ("through_all", "up_to_next") and abs(taper) > 1e-9:
            draft_ignored = True  # SW greys draft here
    if end != "up_to_next" and amount <= 0.0:
        raise FeatureError(f"extrude: effective depth {amount:.3g} must be > 0")

    # Taper applies to blind/mid_plane only.
    taper1 = taper if (end in ("blind", "mid_plane") and not draft_ignored) else 0.0

    # --- direction-2 amount ---
    amount2 = 0.0
    if end2 != "none":
        amount2 = _linear_amount(end2, depth2, normal * -1.0)
        if end2 in ("through_all", "up_to_next") and abs(taper) > 1e-9:
            draft_ignored = True
        if end2 != "up_to_next" and amount2 <= 0.0:
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

        piece = _build_tool_for_dir(face, end, amount, normal, taper1, bodies, fid,
                                    label="dir1", mode=mode)
        if end2 != "none":
            piece2 = _build_tool_for_dir(face, end2, amount2, normal * -1.0, taper1,
                                         bodies, fid, label="dir2", mode=mode)
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
    if draft_ignored:
        resolved["_draft_note"] = "draft ignored (not supported for through_all/up_to_next)"

    # Apply to the running body
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
