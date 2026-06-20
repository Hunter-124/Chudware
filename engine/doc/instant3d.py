"""Instant3D push-pull (SolidWorks "drag a face to change the model").

``doc_push_pull`` takes a dragged planar/cylindrical face and a signed drag distance, finds the
OWNING feature (via the compiler owner map), determines the SINGLE drivable parameter that moving
that face corresponds to, updates it, and rebuilds — so the host can implement live "drag a face"
editing. The host calls this transiently during a drag (``commit=false`` — no undo entry) and
commits on release (``commit=true`` — exactly ONE undo entry).

Request (worker method ``doc_push_pull``)::

    {doc_id, body_id, topo_id, distance, commit?}

- ``body_id``  : 1-based body ordinal (the PickHit / AICM body id).
- ``topo_id``  : 1-based TopExp face index (the picked FACE; same index resolve_pick uses).
- ``distance`` : mm, SIGNED along the face's OUTWARD normal. Positive grows the body
                 (push out / inflate); negative shrinks it (pull in).
- ``commit``   : false (default) = transient preview, no undo entry; true = one undo entry.

Response (success — draggable face)::

    {ok: true, draggable: true, feature_id, feature_type, param, axis,
     old_value, new_value, distance, clamped,
     revision, tree, rebuild, tess, owner_map, id_map}

(The ``revision``/``tree``/``rebuild``/``tess``/``owner_map``/``id_map`` block is exactly the
ops-response :func:`session._ops_response` returns, so the host viewport updates identically to a
normal edit.)

Response (non-draggable face — NO doc mutation)::

    {ok: true, draggable: false, reason: "...", feature_id?, feature_type?}

Transient-preview vs commit mechanism
-------------------------------------
The drag delta is applied as an in-place ``edit_feature``-style patch of the owning feature's
param, then the doc is rebuilt and the revision bumped — identical to ``apply_ops`` EXCEPT for the
undo stack:

- ``commit=false`` pushes NOTHING onto the undo stack (transient). The host wraps a live drag in
  ``doc_checkpoint`` (drag start) and, per sample, ``doc_revert`` (back to the pre-drag doc) then a
  ``doc_push_pull(commit=false)`` for the CURRENT cumulative distance — so previews never compound
  and a cancel is a single ``doc_revert``.
- ``commit=true`` pushes EXACTLY ONE undo entry of the doc state immediately before the delta, so a
  single ``doc_undo`` after the release returns to the pre-delta geometry.

Robustness: every failure path leaves the document UNCHANGED. A face that maps to no single
unambiguous drivable param returns ``draggable:false`` with a reason (never a guessed edit); a
degenerate / failed rebuild rolls the doc back to its pre-edit snapshot and likewise returns
``draggable:false``. Canonical units mm / deg.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

import numpy as np

from engine.doc import selectors as sel
from engine.doc import session as docsess
from engine.doc.ops import OpError

# A param can never be driven to or below zero (that inverts/collapses the body). Push-pull clamps
# the new value to this floor and flags ``clamped`` so the host can show the limit (SolidWorks
# refuses the drag past a degenerate dimension rather than building an inverted solid).
_MIN_DIM = 0.1            # mm — smallest length/depth/radius/thickness a drag may leave
_PARALLEL_TOL = 0.966     # |cos| >= this (~15 deg) counts a face normal as "along" an axis


# --------------------------------------------------------------------------- geometry helpers


def _unit(v) -> np.ndarray:
    a = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(a))
    return a / n if n > 1e-12 else a


def _axis_label(axis: np.ndarray) -> Optional[str]:
    """If *axis* is ~parallel to a world axis, return 'x'|'y'|'z'; else None."""
    if axis is None:
        return None
    a = _unit(axis)
    for lbl, base in (("x", (1.0, 0.0, 0.0)), ("y", (0.0, 1.0, 0.0)), ("z", (0.0, 0.0, 1.0))):
        if abs(float(np.dot(a, base))) >= _PARALLEL_TOL:
            return lbl
    return None


def _feature_by_id(doc: dict, fid: str) -> Optional[dict]:
    for f in doc.get("features", []) or []:
        if isinstance(f, dict) and f.get("id") == fid:
            return f
    return None


def _dim_value(feat: dict, key: str) -> Optional[float]:
    """Current stored value of a dim-or-number param ``key`` (resolved expr value or bare num)."""
    p = feat.get("params", {}) or {}
    if key not in p:
        return None
    v = p[key]
    if isinstance(v, dict):
        val = v.get("value")
    else:
        val = v
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def _has_expr(feat: dict, key: str) -> bool:
    """True if the param is expression-driven (an equation) — push-pull must not stomp it."""
    p = feat.get("params", {}) or {}
    v = p.get(key)
    return isinstance(v, dict) and isinstance(v.get("expr"), str) and bool(v.get("expr"))


def _consumes_sketch_region(feat: dict) -> bool:
    """True if *feat* profiles a sketch region (extrude/revolve/sweep/loft) — its upstream sketch
    must re-register its regions on rebuild, which a cache-hit sketch skips."""
    refs = feat.get("refs", {}) or {}

    def _has_region(obj) -> bool:
        if isinstance(obj, dict):
            return obj.get("kind") == "region" or any(_has_region(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_has_region(v) for v in obj)
        return False

    return _has_region(refs)


# --------------------------------------------------------------------------- face -> param mapping


class _NotDraggable(Exception):
    """A picked face that maps to no single unambiguous drivable param (carries the reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _plan_for_planar(session, feat: dict, ftype: str, info: sel.TopoInfo,
                     distance: float) -> dict:
    """Map a PLANAR dragged face to its drivable length param + new value.

    box     : normal along x/y/z -> length/width/height.
    extrude : cap face (normal ∥ extrude axis) -> depth (blind only). Side walls -> not draggable.
    """
    axis_lbl = _axis_label(info.axis)

    if ftype == "box":
        if axis_lbl is None:
            raise _NotDraggable("box face normal is not aligned to a principal axis")
        key = {"x": "length", "y": "width", "z": "height"}[axis_lbl]
        old = _dim_value(feat, key)
        if old is None:
            old = {"length": 20.0, "width": 20.0, "height": 20.0}[key]
        return _length_plan(feat, key, old, distance, axis_lbl)

    if ftype in ("cylinder", "cone"):
        # The flat end caps drive the axial length (height); the round wall is handled separately.
        key = "height"
        old = _dim_value(feat, key)
        if old is None:
            old = 20.0
        return _length_plan(feat, key, old, distance, axis_lbl or "axial")

    if ftype == "extrude":
        end = (feat.get("params", {}) or {}).get("end", "blind")
        if end != "blind":
            raise _NotDraggable(
                f"extrude end condition '{end}' has no single blind-depth param to drag")
        # The drivable face is a CAP — its normal is parallel to the extrude (sketch-plane) axis.
        # A side wall's normal is perpendicular to that axis; dragging it must NOT change depth, so
        # we reject it. Resolve the extrude axis from the owning sketch's plane normal.
        ext_axis = _extrude_axis(session, feat)
        if ext_axis is not None and info.axis is not None:
            if abs(float(np.dot(_unit(info.axis), ext_axis))) < _PARALLEL_TOL:
                raise _NotDraggable(
                    "dragged face is a side wall of the extrude (normal ⊥ extrude axis), "
                    "not a depth cap")
        old = _dim_value(feat, "depth")
        if old is None:
            old = 10.0
        return _length_plan(feat, "depth", old, distance, axis_lbl or "n")

    raise _NotDraggable(f"planar face of feature type '{ftype}' is not push-pull drivable")


def _extrude_axis(session, feat: dict):
    """Unit extrude direction (= owning sketch plane normal) for a blind extrude, or None.

    Resolved from ``refs.profile[*].sketch`` via the session's sketch-plane resolver. Returns None
    (and the caller skips the cap/side-wall test) when the sketch can't be resolved cheaply."""
    refs = feat.get("refs", {}) or {}
    profiles = refs.get("profile") or []
    sketch_fid = None
    for p in profiles:
        if isinstance(p, dict) and p.get("kind") == "region" and p.get("sketch"):
            sketch_fid = p["sketch"]
            break
    if not sketch_fid:
        return None
    try:
        sfeat = docsess._find_sketch_feature(session, sketch_fid)
        result = session.last_result if session.last_result is not None \
            else docsess._ensure_built(session)
        plane = docsess._resolve_sketch_plane(session, sfeat, result)
        return _unit(tuple(plane.z_dir))
    except Exception:  # noqa: BLE001
        return None


def _length_plan(feat: dict, key: str, old: float, distance: float, axis_lbl: str) -> dict:
    if _has_expr(feat, key):
        raise _NotDraggable(f"param '{key}' is equation-driven (expr) — edit the equation instead")
    new = old + distance
    clamped = False
    if new < _MIN_DIM:
        new = _MIN_DIM
        clamped = True
    return {"param": key, "old_value": old, "new_value": new, "clamped": clamped, "axis": axis_lbl}


def _plan_for_cylindrical(feat: dict, ftype: str, info: sel.TopoInfo, distance: float) -> dict:
    """Map a CYLINDRICAL dragged face to its radius/diameter param + new value.

    cylinder : radius (distance = radial change, outward normal points away from the axis).
    hole     : diameter (radial change doubles the diameter).
    """
    if ftype == "cylinder":
        key = "radius"
        old = _dim_value(feat, key)
        if old is None:
            old = 10.0
        if _has_expr(feat, key):
            raise _NotDraggable("param 'radius' is equation-driven (expr)")
        new = old + distance
        clamped = False
        if new < _MIN_DIM:
            new = _MIN_DIM
            clamped = True
        return {"param": key, "old_value": old, "new_value": new, "clamped": clamped,
                "axis": "radial"}

    if ftype == "hole":
        # Custom-diameter holes only — a standard-table hole's bore is driven by the standard,
        # not a free diameter, so dragging it would silently diverge from the table.
        p = feat.get("params", {}) or {}
        if p.get("standard") not in (None, "custom"):
            raise _NotDraggable(
                f"hole bore is driven by standard '{p.get('standard')}', not a free diameter")
        key = "diameter"
        old = _dim_value(feat, key)
        if old is None:
            old = 3.4
        if _has_expr(feat, key):
            raise _NotDraggable("param 'diameter' is equation-driven (expr)")
        new = old + 2.0 * distance     # radial change doubles the diameter
        clamped = False
        if new < _MIN_DIM:
            new = _MIN_DIM
            clamped = True
        return {"param": key, "old_value": old, "new_value": new, "clamped": clamped,
                "axis": "radial"}

    raise _NotDraggable(f"cylindrical face of feature type '{ftype}' is not push-pull drivable")


def _plan_for_offset_plane(feat: dict, ftype: str, distance: float) -> dict:
    """Datum offset plane: the plane itself is the moved 'face' — drive its offset."""
    key = "offset"
    old = _dim_value(feat, key)
    if old is None:
        raise _NotDraggable("offset datum has no 'offset' param to drive")
    if _has_expr(feat, key):
        raise _NotDraggable("param 'offset' is equation-driven (expr)")
    new = old + distance
    return {"param": key, "old_value": old, "new_value": new, "clamped": False, "axis": "normal"}


def plan_edit(session, feat: dict, info: sel.TopoInfo, distance: float) -> dict:
    """Decide the drivable param + new value for dragging *info* (a face TopoInfo) of *feat*.

    Raises :class:`_NotDraggable` (with a reason) when the face maps to no single param.
    """
    ftype = feat.get("type", "")
    surf = info.surf

    if surf == "plane":
        # An offset/midplane datum's planar face moves with its offset param.
        if ftype in ("plane",) and (feat.get("params", {}) or {}).get("offset") is not None:
            return _plan_for_offset_plane(feat, ftype, distance)
        return _plan_for_planar(session, feat, ftype, info, distance)
    if surf == "cylinder":
        return _plan_for_cylindrical(feat, ftype, info, distance)

    # Revolve angle / cones / spheres / bsplines / sweeps / lofts: no single linear push-pull param.
    if ftype == "revolve":
        raise _NotDraggable("revolve is angle-driven; dragging a revolved face is not a push-pull")
    raise _NotDraggable(
        f"face surface '{surf}' of feature '{ftype}' maps to no single push-pull param")


# --------------------------------------------------------------------------- the RPC


def _resolve_face_owner(session: "docsess.DocSession", body_id: int, topo_id: int):
    """(BodyOut, face TopoInfo, owner_feature_id) for the picked face — or raise OpError."""
    if session.is_assembly:
        raise OpError("invalid_op", "doc_push_pull: not supported on assemblies",
                      {"reason": "assembly"})
    result = docsess._ensure_built(session)
    bodies = list(result.bodies)
    if body_id < 1 or body_id > len(bodies):
        raise OpError("invalid_op", f"body_id {body_id} out of range (1..{len(bodies)})",
                      {"reason": "bad body_id"})
    bo = bodies[body_id - 1]
    infos = sel.describe_faces(bo.shape.wrapped)
    if topo_id < 1 or topo_id > len(infos):
        raise OpError("invalid_op",
                      f"topo_id {topo_id} out of range (1..{len(infos)}) for faces",
                      {"reason": "bad topo_id"})
    info = infos[topo_id - 1]
    owner = result.face_owners.get(bo.body_id, {}).get(topo_id)
    return bo, info, owner


def _result_is_degenerate(result) -> bool:
    """True if a rebuilt *result* carries no real geometry to keep.

    A push-pull value can drive the geometry to nothing without raising — e.g. a hole/cut whose
    bore grows past the stock so the boolean removes ALL material. The cut "succeeds" but yields an
    empty COMPOUND (zero faces, zero solids) rather than a FeatureError, and committing that poisons
    the session: the next face/body resolve sees ``(1..0)`` and the user gets "body_id out of range".
    We treat "no body has any face" as degenerate so the caller can roll back instead of committing
    it. (A multi-body edit that collapses only one body still has faces elsewhere, so it is kept.)
    """
    bodies = list(getattr(result, "bodies", []) or [])
    if not bodies:
        return True
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape
    for b in bodies:
        try:
            shp = b.shape.wrapped if hasattr(b.shape, "wrapped") else b.shape
            if shp is None or shp.IsNull():
                continue
            fmap = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(shp, TopAbs_FACE, fmap)
            if fmap.Size() > 0:
                return False
        except Exception:  # noqa: BLE001 — a body we can't inspect counts as no geometry
            continue
    return True


def _rollback_edit(session: "docsess.DocSession", pre_doc_snapshot: str, commit: bool,
                   emit, owner_fid: str, ftype: str, reason: str) -> dict:
    """Restore the pre-edit doc + rebuild, returning the ``draggable:false`` response.

    Shared by both failure paths (a rebuild that raises, and one that silently collapses the body):
    every push-pull failure must leave the document EXACTLY as it was before the drag tick."""
    session.doc = json.loads(pre_doc_snapshot)
    session.revision += 1
    if commit and session.undo_stack and session.undo_stack[-1] == pre_doc_snapshot:
        session.undo_stack.pop()
    session.last_result = None
    rb = docsess.rebuild(session, emit=emit)
    out = {"ok": True, "draggable": False, "reason": reason,
           "feature_id": owner_fid, "feature_type": ftype}
    out.update(rb)
    return out


def push_pull(session: "docsess.DocSession", body_id: int, topo_id: int,
              distance: float, commit: bool = False,
              emit=None) -> dict:
    """Core push-pull (worker ``doc_push_pull``). See the module docstring for the schema."""
    bo, info, owner_fid = _resolve_face_owner(session, body_id, int(topo_id))

    if not owner_fid:
        return {"ok": True, "draggable": False,
                "reason": "picked face has no owning feature in the owner map"}
    feat = _feature_by_id(session.doc, owner_fid)
    if feat is None:
        return {"ok": True, "draggable": False,
                "reason": f"owner feature '{owner_fid}' not found in the document",
                "feature_id": owner_fid}
    ftype = feat.get("type", "")

    distance = float(distance)
    try:
        plan = plan_edit(session, feat, info, distance)
    except _NotDraggable as nd:
        return {"ok": True, "draggable": False, "reason": nd.reason,
                "feature_id": owner_fid, "feature_type": ftype}

    # A zero-distance drag is a no-op preview: report draggable + the param, mutate nothing.
    if abs(plan["new_value"] - plan["old_value"]) < 1e-12:
        return {"ok": True, "draggable": True, "feature_id": owner_fid, "feature_type": ftype,
                "param": plan["param"], "axis": plan.get("axis"),
                "old_value": plan["old_value"], "new_value": plan["new_value"],
                "distance": distance, "clamped": plan["clamped"],
                **docsess.rebuild(session, emit=emit)}

    # --- apply the param delta on a working copy, rebuild, contain failures ------------------
    pre_doc_snapshot = session._snapshot()       # exact pre-edit JSON for rollback / one-undo
    new_doc = copy.deepcopy(session.doc)
    new_feat = _feature_by_id(new_doc, owner_fid)
    p = new_feat.setdefault("params", {})
    cur = p.get(plan["param"])
    if isinstance(cur, dict):
        # Keep the dim-object shape; clear any (now-stale) expr since we set a literal value.
        new_dim = dict(cur)
        new_dim["value"] = plan["new_value"]
        new_dim["expr"] = None
        p[plan["param"]] = new_dim
    else:
        p[plan["param"]] = {"value": plan["new_value"], "expr": None}

    # Commit pushes ONE undo entry of the pre-edit doc; transient pushes none.
    if commit:
        session.undo_stack.append(pre_doc_snapshot)
        if len(session.undo_stack) > docsess._UNDO_LIMIT:
            session.undo_stack.pop(0)
        session.redo_stack.clear()

    session.doc = new_doc
    session.revision += 1
    session.dirty = True

    # A feature that profiles a sketch region reads its regions from the sketch compiler's
    # side-channel (ctx.datums['_sketch_regions']); an unchanged upstream sketch is a cache HIT
    # that doesn't repopulate it, so the edited extrude/revolve/sweep/loft would see "sketch not
    # compiled". Force a clean recompile in that case (same guard apply_ops uses).
    if _consumes_sketch_region(new_feat):
        session.compile_cache.clear()

    try:
        result = docsess._rebuild(session, emit)
    except Exception as exc:  # noqa: BLE001 — never corrupt the doc on a degenerate rebuild
        # Roll the document fully back (and undo the snapshot/revision side effects).
        return _rollback_edit(session, pre_doc_snapshot, commit, emit, owner_fid, ftype,
                              f"rebuild failed for the new value: {exc}")

    # A rebuild can SUCCEED yet leave nothing to keep — e.g. dragging a bore past the stock so the
    # cut removes all material, yielding an empty compound (zero faces) the compiler reports as ok.
    # Committing that poisons the session (the next resolve sees "body_id 1 out of range (1..0)"),
    # so roll back exactly like a raising rebuild: the body stays at its last valid size.
    if _result_is_degenerate(result):
        return _rollback_edit(session, pre_doc_snapshot, commit, emit, owner_fid, ftype,
                              "the new value removes all material (the body would vanish) — "
                              "drag limited to the last valid size")

    # A feature that errors out (e.g. cut removes no material at the new value) leaves the body
    # unchanged but is still a *contained* error — surface it but treat as a successful edit so the
    # host shows the feature-error state exactly like a normal edit would.
    resp = docsess._ops_response(session, result, {})
    out = {"ok": True, "draggable": True, "feature_id": owner_fid, "feature_type": ftype,
           "param": plan["param"], "axis": plan.get("axis"),
           "old_value": plan["old_value"], "new_value": plan["new_value"],
           "distance": distance, "clamped": plan["clamped"], "committed": bool(commit)}
    out.update(resp)
    return out


# --------------------------------------------------------------------------- worker glue


def _doc_push_pull(params: dict, emit) -> dict:
    s = docsess.get_session(params["doc_id"])

    def _e(data: dict) -> None:
        emit(data)

    return push_pull(s, int(params["body_id"]), int(params["topo_id"]),
                     float(params["distance"]), bool(params.get("commit", False)),
                     emit=_e)


WORKER_METHODS = {"doc_push_pull": _doc_push_pull}
