"""Extrude feature compiler — boss/cut, blind/through_all/mid_plane.

feature_params.md §Sketched features > extrude.

Profile source:
- ``{"kind":"region","sketch":fid,...}`` → resolved via ``ctx._sketch_regions[fid]``
  using ``resolve_region``.
- ``{"kind":"face",...}`` → resolved via describe_faces + resolve (face selector).

``draft`` is DEFERRED — BRepOffsetAPI_DraftAngle not implemented in this card.
``through_all`` = 1.05× combined bounding-box extent in the extrude direction.
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


def _resolve_profile_selector(
    sel: dict[str, Any], ctx: CompileContext
) -> Face:
    """Resolve a single profile selector to a build123d Face.

    Handles:
    - ``{"kind":"region","sketch":fid,...}`` → ctx._sketch_regions
    - ``{"kind":"face",...}`` → resolved via describe_faces
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
        # Compute sketch bbox diag from the face bounding boxes
        try:
            coords = []
            for f in faces:
                bb = f.bounding_box()
                coords.extend([bb.min.X, bb.min.Y, bb.max.X, bb.max.Y])
            if coords:
                span = max(coords) - min(coords)
                sketch_diag = max(span * math.sqrt(2), 1e-3)
            else:
                sketch_diag = 100.0
        except Exception:  # noqa: BLE001
            sketch_diag = 100.0

        rr = resolve_region(sel, descs, sketch_diag)
        if rr.index is None:
            raise FeatureError(
                f"extrude profile: region selector dangling — {rr.dangling_reason}",
                dangling_refs=[f"profile[?]: {rr.dangling_reason}"],
            )
        return faces[rr.index]

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
        return body.faces()[rr.index]

    raise FeatureError(
        f"extrude profile: unsupported selector kind '{kind}' (expected 'region' or 'face')"
    )


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


@register("extrude")
def compile_extrude(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    end = p.get("end", "blind")
    flip = p.get("flip", False)
    merge = p.get("merge", True)

    depth = ctx.dim(p, "depth", 10.0)  # used for blind and mid_plane

    # Draft is deferred
    draft_val = p.get("draft")
    if draft_val is not None:
        draft_deg = ctx.dim(p, "draft", 0.0)
        if abs(draft_deg) > 1e-9:
            pass  # DEFERRED: BRepOffsetAPI_DraftAngle not implemented

    # Resolve profile faces
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("extrude: refs.profile is required")

    profile_faces: list[Face] = []
    dangling: list[str] = []
    for i, sel in enumerate(profile_sels):
        try:
            f = _resolve_profile_selector(sel, ctx)
            profile_faces.append(f)
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

    # Compute amount
    if end == "blind":
        amount = depth
    elif end == "through_all":
        # 1.05× combined bounding-box extent in the extrude direction
        if bodies:
            extent = _body_extent_along(bodies[-1], normal)
        else:
            # No body yet; estimate from face bounding box
            try:
                bb = face0.bounding_box()
                dx = bb.max.X - bb.min.X
                dy = bb.max.Y - bb.min.Y
                dz = bb.max.Z - bb.min.Z
                extent = math.sqrt(dx * dx + dy * dy + dz * dz) * 2
            except Exception:  # noqa: BLE001
                extent = 100.0
        amount = 1.05 * extent
    elif end == "mid_plane":
        amount = depth / 2.0
    else:
        raise FeatureError(f"extrude: unknown end '{end}'")

    if amount <= 0.0:
        raise FeatureError(f"extrude: effective depth {amount:.3g} must be > 0")

    # Build extruded tools for each profile face
    tool_bodies: list[Any] = []
    for face in profile_faces:
        try:
            if end == "mid_plane":
                # Extrude both ways by amount (half-depth each side)
                try:
                    tool_fwd = b3d_extrude(face, amount=amount, mode=Mode.PRIVATE)
                    face_normal = face.normal_at()
                    tool_rev = b3d_extrude(face, amount=amount, dir=face_normal * -1.0,
                                           mode=Mode.PRIVATE)
                    combined = tool_fwd + tool_rev
                    tool_bodies.append(combined)
                except Exception:  # noqa: BLE001
                    try:
                        tool_both = b3d_extrude(face, amount=amount, both=True, mode=Mode.PRIVATE)
                        tool_bodies.append(tool_both)
                    except Exception as e2:  # noqa: BLE001
                        raise FeatureError(
                            f"extrude: mid_plane build failed: {e2}",
                            hint="check that the profile is a closed region in the extrude plane",
                        )
            else:
                tool = b3d_extrude(face, amount=amount, dir=normal, mode=Mode.PRIVATE)
                tool_bodies.append(tool)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"extrude: build failed: {exc}",
                hint="check that the profile is a closed region in the extrude plane",
            )

    # Combine all tool solids
    if len(tool_bodies) == 1:
        tool_solid = tool_bodies[0]
    else:
        tool_solid = tool_bodies[0]
        for tb in tool_bodies[1:]:
            tool_solid = tool_solid + tb

    tool_solid = _to_solid_normalized(tool_solid)

    fid = feat.get("id", "?")

    # Apply to the running body
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("extrude: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        _store_topo_owners(ctx, fid, tool_solid, None)
        return CompileOutput(bodies=bodies, tool_body=tool_solid,
                             resolved={"depth": amount})

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
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(bodies=bodies, tool_body=tool_solid,
                         resolved={"depth": amount})
