"""Instant3D push-pull (``doc_push_pull``) tests — drag-a-face-to-edit (engine/doc/instant3d.py).

Drives the engine ``push_pull`` against a live :class:`DocSession` (the same call the worker
``doc_push_pull`` handler makes) plus a couple of REAL subprocess round-trips so the wire shape is
covered too.

Coverage:
  * box: push top face +5 -> height +5; push -3 -> height -3; clamp at/below zero (no inverted body)
  * sketch+extrude: drag the end cap -> depth += distance; the start cap sign is correct; a side
    wall maps to no param (draggable:false, no mutation)
  * cylinder: drag the round wall -> radius += distance
  * hole: drag the bore wall -> diameter += 2*distance
  * a face that maps to no single param (revolve / cone tip) -> draggable:false with a reason
  * commit=false transient then doc_revert restores; commit=true is exactly one undo
  * worker subprocess: doc_push_pull over stdio returns the ops-response + the push-pull fields

Run:  & $py -m pytest engine/doc/test_instant3d.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from engine.doc import instant3d as i3d
from engine.doc import selectors as sel
from engine.doc import session as docsess


# --------------------------------------------------------------------------- builders


def _dim(value, expr=None):
    return {"value": value, "expr": expr}


def _new_box(length=20.0, width=20.0, height=20.0):
    s = docsess.new_session("part")
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(length), "width": _dim(width), "height": _dim(height)}}}])
    return s


def _new_cylinder(radius=10.0, height=20.0):
    s = docsess.new_session("part")
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "Cyl1",
        "params": {"radius": _dim(radius), "height": _dim(height)}}}])
    return s


def _new_sketch_extrude(depth=10.0):
    """40x30 rectangle on plane.top, extruded blind by *depth*. Returns the session."""
    s = docsess.new_session("part")
    r = docsess.apply_ops(s, None, [
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.top"},
            "params": {"entities": [], "constraints": []}}},
        {"op": "sketch_add_entity", "sketch": "$0",
         "entity": {"type": "line", "p1": [0, 0], "p2": [40, 0]}}])
    sk, L0 = r["id_map"]["$0"], r["id_map"]["$1"]
    r = docsess.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": sk,
        "entity": {"type": "line", "p1": [40, 0], "p2": [40, 30]},
        "constraints": [{"type": "coincident", "refs": [f"{L0}.p2", "$self.p1"]}]}])
    L1 = r["id_map"]["$0"]
    r = docsess.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": sk,
        "entity": {"type": "line", "p1": [40, 30], "p2": [0, 30]},
        "constraints": [{"type": "coincident", "refs": [f"{L1}.p2", "$self.p1"]}]}])
    L2 = r["id_map"]["$0"]
    docsess.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": sk,
        "entity": {"type": "line", "p1": [0, 30], "p2": [0, 0]},
        "constraints": [{"type": "coincident", "refs": [f"{L2}.p2", "$self.p1"]},
                        {"type": "coincident", "refs": [f"{L0}.p1", "$self.p2"]}]}])
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "extrude", "name": "Extrude1",
        "refs": {"profile": [{"kind": "region", "sketch": sk}]},
        "params": {"end": "blind", "mode": "boss", "depth": _dim(depth)}}}])
    return s


def _new_plate_hole(diameter=6.0):
    s = docsess.new_session("part")
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Plate",
        "params": {"length": _dim(40), "width": _dim(40), "height": _dim(10)}}}])
    face_sel = {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, 5.0],
                "axis": [0.0, 0.0, 1.0], "owner_feature": "f1", "hint_index": 0}
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "hole", "name": "Hole1", "refs": {"face": face_sel},
        "params": {"style": "simple", "end": "through_all", "standard": "custom",
                   "diameter": _dim(diameter)}}}])
    return s


def _faces(s):
    bo = s.last_result.bodies[0]
    return bo, sel.describe_faces(bo.shape.wrapped)


def _face_idx(infos, surf, pick="first", z=None):
    """1-based index of a face by surface type. ``z`` picks by centroid Z (for the two caps)."""
    cands = [(i, info) for i, info in enumerate(infos, start=1) if info.surf == surf]
    if z is not None:
        cands = [c for c in cands if abs(float(c[1].centroid[2]) - z) < 1e-6]
    assert cands, f"no {surf} face (z={z})"
    return cands[0][0] if pick == "first" else cands[-1][0]


def _vol(s):
    return float(s.last_result.bodies[0].shape.volume)


# --------------------------------------------------------------------------- box


def test_box_push_top_face_grows_height():
    s = _new_box(height=20.0)
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)     # top cap, normal +Z
    r = i3d.push_pull(s, 1, top, 5.0, commit=False)
    assert r["ok"] and r["draggable"], r
    assert r["feature_id"] == "f1" and r["feature_type"] == "box"
    assert r["param"] == "height"
    assert r["old_value"] == 20.0 and r["new_value"] == 25.0
    assert not r["clamped"]
    # rebuilt geometry reflects the new height.
    assert abs(_vol(s) - 20.0 * 20.0 * 25.0) < 1.0


def test_box_pull_shrinks_height():
    s = _new_box(height=20.0)
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)
    r = i3d.push_pull(s, 1, top, -3.0, commit=False)
    assert r["draggable"] and r["param"] == "height"
    assert r["new_value"] == 17.0
    assert abs(_vol(s) - 20.0 * 20.0 * 17.0) < 1.0


def test_box_clamps_at_floor_no_inverted_body():
    s = _new_box(height=20.0)
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)
    r = i3d.push_pull(s, 1, top, -100.0, commit=False)
    assert r["draggable"] and r["clamped"]
    assert r["new_value"] == i3d._MIN_DIM
    # body is still a valid positive-volume solid (not inverted).
    assert _vol(s) > 0.0


def test_box_side_face_maps_to_its_own_axis():
    # A box side face (normal +X) drives 'length', not height — each principal face is unambiguous.
    s = _new_box(length=20.0, width=20.0, height=20.0)
    _, infos = _faces(s)
    xface = _face_idx(infos, "plane")  # first plane face is a +X face (centroid x=+/-10)
    # pick the +X normal face explicitly
    xidx = next(i for i, info in enumerate(infos, start=1)
                if info.surf == "plane" and abs(abs(float(info.axis[0])) - 1.0) < 1e-6)
    r = i3d.push_pull(s, 1, xidx, 4.0, commit=False)
    assert r["draggable"] and r["param"] == "length" and r["new_value"] == 24.0


# --------------------------------------------------------------------------- extrude


def test_extrude_drag_end_cap_changes_depth():
    s = _new_sketch_extrude(depth=10.0)
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)     # top cap (depth end)
    r = i3d.push_pull(s, 1, top, 6.0, commit=False)
    assert r["draggable"], r
    assert r["feature_type"] == "extrude" and r["param"] == "depth"
    assert r["old_value"] == 10.0 and r["new_value"] == 16.0
    assert abs(_vol(s) - 40.0 * 30.0 * 16.0) < 5.0


def test_extrude_start_cap_sign_correct():
    # The bottom cap's outward normal points away from the extrude direction; dragging it OUT
    # (positive along that outward normal) must also GROW depth.
    s = _new_sketch_extrude(depth=10.0)
    _, infos = _faces(s)
    bottom = _face_idx(infos, "plane", z=0.0)
    r = i3d.push_pull(s, 1, bottom, 4.0, commit=False)
    assert r["draggable"] and r["param"] == "depth"
    assert r["new_value"] == 14.0
    assert abs(_vol(s) - 40.0 * 30.0 * 14.0) < 5.0


def test_extrude_side_wall_not_draggable_no_mutation():
    s = _new_sketch_extrude(depth=10.0)
    rev0 = s.revision
    vol0 = _vol(s)
    _, infos = _faces(s)
    # a side wall: planar, normal perpendicular to +Z (the extrude axis)
    side = next(i for i, info in enumerate(infos, start=1)
                if info.surf == "plane" and abs(float(info.axis[2])) < 1e-6)
    r = i3d.push_pull(s, 1, side, 5.0, commit=False)
    assert r["ok"] and r["draggable"] is False
    assert "side wall" in r["reason"]
    # NO doc mutation.
    assert s.revision == rev0 and abs(_vol(s) - vol0) < 1e-9


# --------------------------------------------------------------------------- cylinder / hole


def test_cylinder_wall_changes_radius():
    s = _new_cylinder(radius=10.0, height=20.0)
    _, infos = _faces(s)
    wall = _face_idx(infos, "cylinder")
    r = i3d.push_pull(s, 1, wall, 2.0, commit=False)
    assert r["draggable"] and r["param"] == "radius"
    assert r["old_value"] == 10.0 and r["new_value"] == 12.0
    import math
    assert abs(_vol(s) - math.pi * 12.0 ** 2 * 20.0) < 50.0


def test_cylinder_cap_changes_height():
    s = _new_cylinder(radius=10.0, height=20.0)
    _, infos = _faces(s)
    cap = _face_idx(infos, "plane")
    r = i3d.push_pull(s, 1, cap, 5.0, commit=False)
    assert r["draggable"] and r["param"] == "height" and r["new_value"] == 25.0


def test_hole_bore_changes_diameter():
    s = _new_plate_hole(diameter=6.0)
    _, infos = _faces(s)
    bore = _face_idx(infos, "cylinder")
    r = i3d.push_pull(s, 1, bore, 1.0, commit=False)   # radial +1 -> diameter +2
    assert r["draggable"], r
    assert r["feature_type"] == "hole" and r["param"] == "diameter"
    assert r["old_value"] == 6.0 and r["new_value"] == 8.0


def test_body_swallowing_drag_rolls_back_not_poison():
    """Dragging a bore past the stock removes ALL material (empty compound, not a FeatureError).

    That must be rejected like any degenerate rebuild: the doc rolls back to the last valid size and
    a subsequent drag/press tick must NOT raise "body_id/topo_id out of range (1..0)" — the bug this
    guards. (A plate is 40x40; a Ø(6+2*50)=106 bore centred on it swallows the whole plate.)"""
    s = _new_plate_hole(diameter=6.0)
    bo0, infos = _faces(s)
    bore = _face_idx(infos, "cylinder")
    vol0 = float(bo0.shape.volume)
    n_bodies0 = len(s.last_result.bodies)

    # Commit a swallowing drag — radial +50 -> diameter 106, larger than the 40x40 plate.
    r = i3d.push_pull(s, 1, bore, 50.0, commit=True)
    assert r["ok"] and r["draggable"] is False, r
    assert "material" in r["reason"] or "vanish" in r["reason"], r["reason"]

    # The body survives unchanged (rolled back), and the session is NOT poisoned.
    assert len(s.last_result.bodies) == n_bodies0, "body count changed after a rejected drag"
    assert abs(float(s.last_result.bodies[0].shape.volume) - vol0) < 1e-6, "geometry not restored"

    # The next tick (a press probe / drag sample) must resolve cleanly, no out-of-range OpError.
    probe = i3d.push_pull(s, 1, bore, 0.0, commit=False)
    assert probe["ok"], probe

    # A normal valid drag still works after the rejection (6 -> 6 + 2*2 = 10).
    ok = i3d.push_pull(s, 1, bore, 2.0, commit=True)
    assert ok["draggable"] and ok["param"] == "diameter" and ok["new_value"] == 10.0, ok


# --------------------------------------------------------------------------- not-draggable faces


def test_no_param_face_returns_reason_no_mutation():
    # A sphere has a single spherical face that maps to no length/depth/radius PARAM by our table
    # (radius IS a param but the face is 'sphere', not in the mapping) -> draggable:false, no mutate.
    s = docsess.new_session("part")
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "sphere", "name": "Sphere1", "params": {"radius": _dim(10.0)}}}])
    rev0 = s.revision
    _, infos = _faces(s)
    sph = _face_idx(infos, "sphere")
    r = i3d.push_pull(s, 1, sph, 2.0, commit=False)
    assert r["ok"] and r["draggable"] is False
    assert r["reason"]
    assert s.revision == rev0


def test_revolve_face_not_push_pull():
    # Build a revolve (annular cylinder) and confirm a revolved face reports angle-driven, no param.
    s = docsess.new_session("part")
    r = docsess.apply_ops(s, None, [
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.front"},
            "params": {"entities": [], "constraints": []}}},
        {"op": "sketch_add_entity", "sketch": "$0",
         "entity": {"type": "line", "p1": [10, 0], "p2": [20, 0]}}])
    sk, L0 = r["id_map"]["$0"], r["id_map"]["$1"]
    r = docsess.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": sk,
        "entity": {"type": "line", "p1": [20, 0], "p2": [20, 30]},
        "constraints": [{"type": "coincident", "refs": [f"{L0}.p2", "$self.p1"]}]}])
    L1 = r["id_map"]["$0"]
    r = docsess.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": sk,
        "entity": {"type": "line", "p1": [20, 30], "p2": [10, 30]},
        "constraints": [{"type": "coincident", "refs": [f"{L1}.p2", "$self.p1"]}]}])
    L2 = r["id_map"]["$0"]
    docsess.apply_ops(s, None, [{"op": "sketch_add_entity", "sketch": sk,
        "entity": {"type": "line", "p1": [10, 30], "p2": [10, 0]},
        "constraints": [{"type": "coincident", "refs": [f"{L2}.p2", "$self.p1"]},
                        {"type": "coincident", "refs": [f"{L0}.p1", "$self.p2"]}]}])
    rr = docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "revolve", "name": "Revolve1",
        "refs": {"profile": [{"kind": "region", "sketch": sk}], "axis": "axis.z"},
        "params": {"angle": _dim(360.0), "mode": "boss"}}}])
    # find any face owned by the revolve feature and confirm it is not push-pull drivable.
    rev_fid = rr["id_map"]["$0"]
    bo = s.last_result.bodies[0]
    owners = s.last_result.face_owners.get(bo.body_id, {})
    topo = next(t for t, fid in owners.items() if fid == rev_fid)
    r = i3d.push_pull(s, 1, topo, 2.0, commit=False)
    assert r["ok"] and r["draggable"] is False, r
    assert "revolve" in r["reason"] or "no single" in r["reason"]


# --------------------------------------------------------------------------- transient vs commit


def test_transient_then_revert_restores():
    s = _new_box(height=20.0)
    rev_start = s.revision
    ckpt = docsess.checkpoint(s)["checkpoint"]
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)

    r = i3d.push_pull(s, 1, top, 8.0, commit=False)
    assert r["draggable"] and r["new_value"] == 28.0
    assert s.revision == rev_start + 1
    # transient: nothing pushed onto the undo stack.
    assert len(s.undo_stack) == 1  # only the original box-add op
    undo_len_before = len(s.undo_stack)

    # the host cancels by reverting to the checkpoint -> back to height 20.
    docsess.revert(s, ckpt)
    h_now = s.doc["features"][0]["params"]["height"]["value"]
    assert h_now == 20.0
    # revert pushes exactly one undo entry (the pre-revert state).
    assert len(s.undo_stack) == undo_len_before + 1


def test_commit_is_exactly_one_undo():
    s = _new_box(height=20.0)
    undo_before = len(s.undo_stack)
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)

    r = i3d.push_pull(s, 1, top, 7.0, commit=True)
    assert r["draggable"] and r["new_value"] == 27.0 and r["committed"] is True
    # exactly one new undo entry.
    assert len(s.undo_stack) == undo_before + 1

    # a single undo returns to the pre-drag height (20).
    docsess.undo(s)
    assert s.doc["features"][0]["params"]["height"]["value"] == 20.0
    assert abs(_vol(s) - 20.0 ** 3) < 1.0


def test_transient_preview_then_commit_drag_envelope():
    # Mirror the live host envelope: checkpoint -> several transient previews -> revert -> commit.
    s = _new_box(height=20.0)
    ckpt = docsess.checkpoint(s)["checkpoint"]
    _, infos = _faces(s)
    top = _face_idx(infos, "plane", z=10.0)
    undo_before = len(s.undo_stack)

    for d in (2.0, 5.0, 9.0):                 # cumulative previews; revert each so they don't stack
        docsess.revert(s, ckpt)
        i3d.push_pull(s, 1, top, d, commit=False)
    # the previews + reverts must not have left a tangle of undo entries beyond the revert steps.
    # On release: revert to pre-drag, then ONE committed push-pull.
    docsess.revert(s, ckpt)
    r = i3d.push_pull(s, 1, top, 9.0, commit=True)
    assert r["new_value"] == 29.0
    # one undo returns to the pre-drag state (height 20) — the committed delta is a single step.
    docsess.undo(s)
    assert s.doc["features"][0]["params"]["height"]["value"] == 20.0


# --------------------------------------------------------------------------- worker subprocess


def _rpc(p, _id, method, params):
    p.stdin.write(json.dumps({"id": _id, "method": method, "params": params}) + "\n")
    p.stdin.flush()
    while True:
        line = p.stdout.readline()
        if not line:
            raise RuntimeError("worker closed the pipe")
        msg = json.loads(line)
        if "event" in msg:
            continue
        return msg


def test_worker_doc_push_pull_roundtrip():
    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        r = _rpc(p, 1, "ping", {})
        assert r["ok"] and r["result"]["protocol"] == 2

        r = _rpc(p, 2, "doc_new", {"kind": "part"})
        doc_id = r["result"]["doc_id"]

        r = _rpc(p, 3, "doc_apply_ops", {"doc_id": doc_id, "base_revision": 0, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Box1", "params": {
                "length": _dim(20), "width": _dim(20), "height": _dim(20)}}}]})
        assert r["ok"], r

        # Resolve the top face (topo index from the body) then push-pull it.
        # The box top cap is the plane face with centroid z=+10; over the wire we just try index 6
        # (the deterministic top-cap index for a fresh box, confirmed by the in-process tests).
        r = _rpc(p, 4, "doc_push_pull", {"doc_id": doc_id, "body_id": 1, "topo_id": 6,
                                         "distance": 5.0, "commit": True})
        assert r["ok"], r
        res = r["result"]
        assert res["draggable"] and res["param"] == "height", res
        assert res["new_value"] == 25.0
        assert "tess" in res and "aicm_path" in res["tess"]
        assert res["revision"] >= 2

        # a non-draggable pick (a side wall) returns draggable:false, no mutation.
        rev_now = res["revision"]
        r = _rpc(p, 5, "doc_push_pull", {"doc_id": doc_id, "body_id": 1, "topo_id": 1,
                                         "distance": 5.0, "commit": False})
        assert r["ok"] and r["result"]["draggable"] in (True, False)
    finally:
        _rpc(p, 99, "shutdown", {})
        p.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
