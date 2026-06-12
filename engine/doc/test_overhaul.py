"""C35-overhaul — engine-workstream tests (house-style plain runner, NOT pytest).

Run:  & $py -m engine.doc.test_overhaul   (from repo root)

Covers the SolidWorks-class engine overhaul wave:
  1. owner_map exposure — box+fillet: the fillet feature owns the new cylindrical fillet face, the
     box owns the un-regenerated faces; bodies attributed by face ownership.
  2. constraints in sketch responses — sketch_enter carries every constraint with
     {id,type,refs,value,expr,driven,placement,measured}; measured null for relations.
  3. dimensional constraint upgrades — distance point-line, distance axis h/v, angle between two
     lines (measured in [0,180)), diameter; driven leaves geometry alone + still measures.
  4. dimension placement persistence — placement stored, validated, set via sketch_set_dim,
     survives a JSON round-trip; set_dim updates driven/placement subsets.
  5. doc_checkpoint / doc_revert — checkpoint -> 3 edits -> revert restores snapshot; one undo
     after revert returns to the 3-edits state.
  6. rollback bar — box+fillet+shell: set_rollback after box rolls back fillet+shell (volume ==
     box volume, statuses rolled_back), null restores full volume; add_feature mid-bar inserts at
     the bar and moves the bar; deleting the bar feature keeps the field valid.
"""

from __future__ import annotations

import json
import math

from engine.doc import session as docsess
from engine.doc import sketch_solver
from engine.doc.ops import OpError, apply_ops as _apply_ops_to_doc
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- helpers


def _dim(value, expr=None):
    return {"value": value, "expr": expr}


def _box_op(l=20, w=20, h=20, name="Box1"):
    return {"op": "add_feature", "feature": {
        "type": "box", "name": name,
        "params": {"length": _dim(l), "width": _dim(w), "height": _dim(h)}}}


def _new_part(name="t"):
    return docsess.new_session("part", name=name)


def _add_rect_sketch(s):
    """Add a sketch on plane.top with a closed rectangle (4 lines) + h/v relations + a fix. Returns
    (sketch_fid, [L0,L1,L2,L3]). Mirrors test_worker_sketch's rectangle construction in-process."""
    r = docsess.apply_ops(s, s.revision, [
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.top"},
            "params": {"entities": [], "constraints": []}}},
        {"op": "sketch_add_entity", "sketch": "$0",
         "entity": {"type": "line", "p1": [0, 0], "p2": [80, 0]}}])
    sk, L0 = r["id_map"]["$0"], r["id_map"]["$1"]
    r = docsess.apply_ops(s, s.revision, [
        {"op": "sketch_add_entity", "sketch": sk,
         "entity": {"type": "line", "p1": [80, 0], "p2": [80, 40]},
         "constraints": [{"type": "coincident", "refs": [f"{L0}.p2", "$self.p1"]}]}])
    L1 = r["id_map"]["$0"]
    r = docsess.apply_ops(s, s.revision, [
        {"op": "sketch_add_entity", "sketch": sk,
         "entity": {"type": "line", "p1": [80, 40], "p2": [0, 40]},
         "constraints": [{"type": "coincident", "refs": [f"{L1}.p2", "$self.p1"]}]}])
    L2 = r["id_map"]["$0"]
    r = docsess.apply_ops(s, s.revision, [
        {"op": "sketch_add_entity", "sketch": sk,
         "entity": {"type": "line", "p1": [0, 40], "p2": [0, 0]},
         "constraints": [
             {"type": "coincident", "refs": [f"{L2}.p2", "$self.p1"]},
             {"type": "coincident", "refs": [f"{L0}.p1", "$self.p2"]}]}])
    L3 = r["id_map"]["$0"]
    docsess.apply_ops(s, s.revision, [
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "horizontal", "refs": [L0]}},
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "horizontal", "refs": [L2]}},
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "vertical", "refs": [L1]}},
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "vertical", "refs": [L3]}},
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "fix", "refs": [f"{L0}.p1"]}}])
    return sk, [L0, L1, L2, L3]


# --------------------------------------------------------------------------- 1. owner_map


def test_owner_map_box_fillet():
    s = _new_part("om")
    r = docsess.apply_ops(s, 0, [_box_op()])
    om = r["owner_map"]
    # The lone box owns all 6 faces + 12 edges of body 1.
    assert set(om.keys()) == {"f1"}, om
    assert len(om["f1"]["faces"]) == 6 and len(om["f1"]["edges"]) == 12, om["f1"]
    assert om["f1"]["bodies"] == [1], om["f1"]
    # Every face/edge id is [body_id, topo_id] with integer ids (the pick ids).
    for bid, tid in om["f1"]["faces"]:
        assert isinstance(bid, int) and isinstance(tid, int), (bid, tid)

    # Fillet one edge: the new cylindrical fillet face must be owned by the fillet feature; the box
    # keeps the faces the fillet did not regenerate.
    pick = docsess.resolve_pick(s, 1, 1, "edge", 1)
    fillet = {"op": "add_feature", "feature": {
        "type": "fillet", "name": "F1", "params": {"radius": _dim(2)},
        "refs": {"edges": [pick["selector"]]}}}
    r2 = docsess.apply_ops(s, 1, [fillet])
    om2 = r2["owner_map"]
    assert "f2" in om2 and "f1" in om2, om2
    # Locate the cylindrical (fillet) face and assert f2 owns it.
    from engine.doc import selectors as sel
    bo = s.last_result.bodies[0]
    infos = sel.describe_faces(bo.shape.wrapped)
    cyl_topo = [i for i, fi in enumerate(infos, start=1) if fi.surf == "cylinder"]
    assert cyl_topo, "fillet must add a cylindrical face"
    f2_faces = {tuple(x) for x in om2["f2"]["faces"]}
    assert (1, cyl_topo[0]) in f2_faces, (cyl_topo, om2["f2"]["faces"])
    # The box still owns at least one (un-regenerated) face.
    assert om2["f1"]["faces"], "box must still own some faces"
    # Total tagged faces == real face count (6 box + 1 fillet = 7).
    total = len(om2["f1"]["faces"]) + len(om2["f2"]["faces"])
    assert total == len(infos) == 7, (total, len(infos))
    print(f"[owner_map] box=f1 ({len(om2['f1']['faces'])} faces) fillet=f2 owns cyl face "
          f"topo={cyl_topo[0]}; total faces={total}")


def test_owner_map_in_tree_and_undo():
    """doc_tree, doc_undo responses all carry owner_map (single-body lineage box+fillet)."""
    s = _new_part("om2")
    docsess.apply_ops(s, 0, [_box_op()])
    pick = docsess.resolve_pick(s, 1, 1, "edge", 1)
    docsess.apply_ops(s, 1, [{"op": "add_feature", "feature": {
        "type": "fillet", "name": "F1", "params": {"radius": _dim(2)},
        "refs": {"edges": [pick["selector"]]}}}])
    t = docsess.tree(s)
    assert "owner_map" in t and "f1" in t["owner_map"], t.get("owner_map")
    u = docsess.undo(s)  # back to just the box
    assert "owner_map" in u and "f1" in u["owner_map"], list(u.keys())
    print("[owner_map] present in doc_tree + doc_undo responses")


# --------------------------------------------------------------------------- 2. constraints in response


def test_constraints_in_sketch_response():
    s = _new_part("cs")
    sk, (L0, L1, L2, L3) = _add_rect_sketch(s)
    # Two driving dims: width 80 (point-point distance) + a driven reference dim.
    r = docsess.apply_ops(s, s.revision, [
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "distance", "refs": [f"{L0}.p1", f"{L0}.p2"],
                        "value": _dim(80.0), "placement": [40.0, -5.0]}},
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "distance", "refs": [f"{L1}.p1", f"{L1}.p2"],
                        "value": _dim(40.0), "driven": True}}])
    width_cid = r["id_map"]["$0"]
    ref_cid = r["id_map"]["$1"]
    resp = docsess.sketch_enter(s, sk)
    cons = {c["id"]: c for c in resp["constraints"]}
    # Every stored constraint appears, with the full record shape.
    for c in resp["constraints"]:
        assert set(c.keys()) == {"id", "type", "refs", "value", "expr", "driven",
                                 "placement", "measured"}, c
    # The width driving dim: value 80, not driven, placement preserved, measured ~80.
    w = cons[width_cid]
    assert w["value"] == 80.0 and w["driven"] is False, w
    assert w["placement"] == [40.0, -5.0], w
    assert abs(w["measured"] - 80.0) < 1e-3, w
    # The driven reference dim: driven True, measured reflects the solved geometry (~40).
    d = cons[ref_cid]
    assert d["driven"] is True and abs(d["measured"] - 40.0) < 1e-3, d
    # A geometric relation (horizontal) reports measured == None.
    rel = [c for c in resp["constraints"] if c["type"] == "horizontal"][0]
    assert rel["measured"] is None, rel
    docsess.sketch_exit(s, sk)
    print(f"[constraints] {len(resp['constraints'])} constraints in sketch_enter; "
          f"width measured={w['measured']:.1f} driven-ref measured={d['measured']:.1f}")


# --------------------------------------------------------------------------- 3. dim upgrades (solver-level)


def _solve(entities, constraints, env=None):
    return sketch_solver.solve_sketch(
        {"entities": entities, "constraints": constraints}, env or {})


def test_distance_point_line():
    # A point above a fully-fixed horizontal line; constrain the perpendicular distance to 7.
    # The line is pinned at both ends (so its infinite line is the x-axis) and the point is fixed
    # in x (vertical-aligned with e1.p1), leaving only the perpendicular offset for the dim to set.
    ents = [
        {"id": "e1", "type": "line", "p1": [0, 0], "p2": [10, 0], "construction": False},
        {"id": "e2", "type": "point", "p": [0, 3], "construction": False},
    ]
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
        {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
        {"id": "c3", "type": "vertical", "refs": ["e1.p1", "e2"]},   # point shares x with e1.p1
        {"id": "c4", "type": "distance", "refs": ["e2", "e1"], "value": _dim(7.0)},
    ]
    sol = _solve(ents, cons)
    assert sol.converged, "point-line distance must converge"
    # The line is the x-axis, the point sits at x=0, so its y must be +-7 (perpendicular offset).
    assert abs(abs(sol.entities["e2"]["p"][1]) - 7.0) < 1e-4, sol.entities["e2"]
    m = sketch_solver.measure_constraint(cons[3], sol.entities)
    assert abs(m - 7.0) < 1e-4, f"point-line distance should solve to 7, measured {m}"
    print(f"[distance:point-line] solved perpendicular distance measured={m:.4f}")


def test_distance_axis_hv():
    # Two points; axis 'h' constrains |Δx|=6, axis 'v' on a second pair constrains |Δy|=9.
    ents = [
        {"id": "e1", "type": "point", "p": [0, 0], "construction": False},
        {"id": "e2", "type": "point", "p": [2, 2], "construction": False},
    ]
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1"]},
        {"id": "c2", "type": "distance", "refs": ["e1", "e2"], "value": _dim(6.0), "axis": "h"},
        {"id": "c3", "type": "distance", "refs": ["e1", "e2"], "value": _dim(9.0), "axis": "v"},
    ]
    sol = _solve(ents, cons)
    assert sol.converged, "axis distance must converge"
    dx = abs(sol.entities["e1"]["p"][0] - sol.entities["e2"]["p"][0])
    dy = abs(sol.entities["e1"]["p"][1] - sol.entities["e2"]["p"][1])
    assert abs(dx - 6.0) < 1e-4 and abs(dy - 9.0) < 1e-4, (dx, dy)
    mh = sketch_solver.measure_constraint(cons[1], sol.entities)
    mv = sketch_solver.measure_constraint(cons[2], sol.entities)
    assert abs(mh - 6.0) < 1e-4 and abs(mv - 9.0) < 1e-4, (mh, mv)
    print(f"[distance:axis] |dx|={dx:.4f} (h=6), |dy|={dy:.4f} (v=9)")


def test_angle_between_lines():
    # Two lines sharing a vertex; constrain the angle to 30 degrees. Measured in [0,180).
    ents = [
        {"id": "e1", "type": "line", "p1": [0, 0], "p2": [10, 0], "construction": False},
        {"id": "e2", "type": "line", "p1": [0, 0], "p2": [8, 5], "construction": False},
    ]
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
        {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
        {"id": "c3", "type": "coincident", "refs": ["e1.p1", "e2.p1"]},
        {"id": "c4", "type": "angle", "refs": ["e1", "e2"], "value": _dim(30.0)},
    ]
    sol = _solve(ents, cons)
    assert sol.converged, "angle must converge"
    m = sketch_solver.measure_constraint(cons[3], sol.entities)
    assert 0.0 <= m < 180.0, f"angle measured must be in [0,180), got {m}"
    assert abs(m - 30.0) < 1e-2, f"angle should solve to 30, measured {m}"
    print(f"[angle] measured={m:.4f} deg (target 30, in [0,180))")


def test_diameter_reachable():
    ents = [{"id": "e1", "type": "circle", "center": [0, 0], "radius": 5,
             "construction": False}]
    cons = [{"id": "c1", "type": "diameter", "refs": ["e1"], "value": _dim(12.0)}]
    sol = _solve(ents, cons)
    assert sol.converged, "diameter must converge"
    assert abs(sol.entities["e1"]["radius"] - 6.0) < 1e-6, sol.entities["e1"]
    m = sketch_solver.measure_constraint(cons[0], sol.entities)
    assert abs(m - 12.0) < 1e-6, f"diameter measured should be 12, got {m}"
    print(f"[diameter] radius solved to {sol.entities['e1']['radius']}, measured diameter={m}")


def test_driven_leaves_geometry_and_measures():
    # A circle starts at r=5. A DRIVEN diameter=20 must NOT change the geometry, but measured
    # reports the actual diameter (10).
    ents = [{"id": "e1", "type": "circle", "center": [0, 0], "radius": 5,
             "construction": False}]
    cons = [{"id": "c1", "type": "fix", "refs": ["e1.center"]},
            {"id": "c2", "type": "diameter", "refs": ["e1"], "value": _dim(20.0),
             "driven": True}]
    sol = _solve(ents, cons)
    assert abs(sol.entities["e1"]["radius"] - 5.0) < 1e-9, "driven dim must not move geometry"
    m = sketch_solver.measure_constraint(cons[1], sol.entities)
    assert abs(m - 10.0) < 1e-6, f"driven measured should be actual diameter 10, got {m}"
    print(f"[driven] geometry unchanged (r=5); driven diameter measured={m} (actual)")


# --------------------------------------------------------------------------- 4. placement persistence


def test_placement_persists_and_set_dim():
    s = _new_part("pl")
    sk, (L0, L1, L2, L3) = _add_rect_sketch(s)
    r = docsess.apply_ops(s, s.revision, [
        {"op": "sketch_add_constraint", "sketch": sk,
         "constraint": {"type": "distance", "refs": [f"{L0}.p1", f"{L0}.p2"],
                        "value": _dim(80.0), "placement": [40.0, -5.0]}}])
    cid = r["id_map"]["$0"]
    # Stored in the doc JSON.
    con = _find_constraint(s.doc, sk, cid)
    assert con["placement"] == [40.0, -5.0], con
    # JSON round-trip survives (same dump/load the stepplus writer uses).
    assert validate_part(s.doc) == [], validate_part(s.doc)
    roundtrip = json.loads(json.dumps(s.doc))
    assert _find_constraint(roundtrip, sk, cid)["placement"] == [40.0, -5.0]

    # sketch_set_dim updates a subset: change placement + mark driven, leave value alone.
    docsess.apply_ops(s, s.revision, [
        {"op": "sketch_set_dim", "sketch": sk, "id": cid,
         "placement": [12.0, 34.0], "driven": True}])
    con2 = _find_constraint(s.doc, sk, cid)
    assert con2["placement"] == [12.0, 34.0] and con2["driven"] is True, con2
    assert con2["value"]["value"] == 80.0, "value must be untouched when only placement/driven set"

    # sketch_set_dim with value+placement together.
    docsess.apply_ops(s, s.revision, [
        {"op": "sketch_set_dim", "sketch": sk, "id": cid, "value": 90.0,
         "placement": None}])
    con3 = _find_constraint(s.doc, sk, cid)
    assert con3["value"]["value"] == 90.0 and "placement" not in con3, con3
    print("[placement] stored + JSON round-trip + sketch_set_dim subset updates OK")


def _find_constraint(doc, sk_fid, cid):
    for f in doc["features"]:
        if f["id"] == sk_fid:
            for c in f["params"]["constraints"]:
                if c["id"] == cid:
                    return c
    raise AssertionError(f"constraint {cid} not found")


# --------------------------------------------------------------------------- 5. checkpoint / revert


def test_checkpoint_revert():
    s = _new_part("ck")
    docsess.apply_ops(s, 0, [_box_op(20, 20, 20)])
    snap_doc = json.loads(json.dumps(s.doc))  # the state we will checkpoint
    ck = docsess.checkpoint(s)["checkpoint"]
    rev_at_ckpt = s.revision

    # 3 edits after the checkpoint.
    docsess.apply_ops(s, s.revision, [_box_op(5, 5, 5, name="Box2")])
    docsess.apply_ops(s, s.revision, [{"op": "rename", "id": "f1", "name": "Renamed"}])
    docsess.apply_ops(s, s.revision, [_box_op(3, 3, 3, name="Box3")])
    three_edits_doc = json.loads(json.dumps(s.doc))
    assert len(s.doc["features"]) == 3, s.doc["features"]

    # Revert -> doc equals the checkpoint snapshot.
    resp = docsess.revert(s, ck)
    assert "owner_map" in resp and "tree" in resp and "rebuild" in resp, list(resp.keys())
    assert s.doc == snap_doc, "revert must restore the exact snapshot doc"
    assert s.revision > rev_at_ckpt, "revert bumps the revision"

    # ONE undo after revert returns to the 3-edits state (the pre-revert state).
    docsess.undo(s)
    assert s.doc == three_edits_doc, "single undo after revert must restore the pre-revert state"

    # Reverting to the same checkpoint when the doc already equals it is a cheap no-op but still
    # returns the full ops-response.
    docsess.revert(s, ck)  # back to checkpoint
    again = docsess.revert(s, ck)  # already equal -> still a full response
    assert again["tree"]["features"] == snap_doc["features"], again["tree"]["features"]

    # Unknown checkpoint id is a structured error.
    bad_code = None
    try:
        docsess.revert(s, "ckpt-nope")
        raise AssertionError("revert with bad id must raise")
    except OpError as e:
        bad_code = e.code
    assert bad_code == "invalid_op", bad_code
    print(f"[checkpoint] revert restored snapshot; one undo -> 3-edits; bad id -> {bad_code}")


# --------------------------------------------------------------------------- 6. rollback bar


def _volume(s):
    return float(sum(b.shape.volume for b in s.last_result.bodies))


def test_rollback_bar():
    s = _new_part("rb")
    # box (20^3) + fillet + shell.
    docsess.apply_ops(s, 0, [_box_op(20, 20, 20)])
    pick = docsess.resolve_pick(s, 1, 1, "edge", 1)
    docsess.apply_ops(s, 1, [{"op": "add_feature", "feature": {
        "type": "fillet", "name": "F1", "params": {"radius": _dim(2)},
        "refs": {"edges": [pick["selector"]]}}}])
    docsess.apply_ops(s, 2, [{"op": "add_feature", "feature": {
        "type": "shell", "name": "Shell1", "params": {"thickness": _dim(1.5)},
        "refs": {}}}])
    full_vol = _volume(s)
    box_vol = 20.0 * 20.0 * 20.0

    # Set the rollback bar after the box: fillet + shell are rolled back.
    r = docsess.apply_ops(s, s.revision, [{"op": "set_rollback", "after": "f1"}])
    assert r["tree"]["rollback_after"] == "f1", r["tree"].get("rollback_after")
    fs = {x["id"]: x for x in r["rebuild"]["feature_status"]}
    assert fs["f2"].get("rolled_back") is True and fs["f3"].get("rolled_back") is True, fs
    assert fs["f2"]["error"] is None and fs["f3"]["error"] is None, "rolled_back is not an error"
    assert fs["f1"].get("rolled_back") is False, fs["f1"]
    vol_rolled = _volume(s)
    assert abs(vol_rolled - box_vol) < 1e-6, f"rolled-back volume {vol_rolled} != box {box_vol}"

    # Bar back to end (null) -> full geometry again.
    r2 = docsess.apply_ops(s, s.revision, [{"op": "set_rollback", "after": None}])
    assert "rollback_after" not in r2["tree"], r2["tree"].get("rollback_after")
    assert abs(_volume(s) - full_vol) < 1e-6, "null bar must restore full volume"
    print(f"[rollback] box={box_vol:.0f} rolled={vol_rolled:.0f} full={full_vol:.0f} "
          f"(fillet/shell -> rolled_back, then restored)")


def test_rollback_add_feature_mid_bar():
    s = _new_part("rb2")
    docsess.apply_ops(s, 0, [_box_op(20, 20, 20)])           # f1
    docsess.apply_ops(s, 1, [_box_op(10, 10, 10, name="B2")])  # f2
    docsess.apply_ops(s, 2, [_box_op(5, 5, 5, name="B3")])     # f3
    # Bar after f1.
    docsess.apply_ops(s, s.revision, [{"op": "set_rollback", "after": "f1"}])
    # add_feature while bar mid-history: inserts right after f1, bar moves to the new feature.
    r = docsess.apply_ops(s, s.revision, [_box_op(7, 7, 7, name="Inserted")])
    new_fid = r["id_map"]["$0"]
    feat_ids = [f["id"] for f in r["tree"]["features"]]
    # Order: f1, <new>, f2, f3.
    assert feat_ids.index(new_fid) == feat_ids.index("f1") + 1, feat_ids
    assert r["tree"]["rollback_after"] == new_fid, r["tree"].get("rollback_after")
    print(f"[rollback] add_feature mid-bar inserted {new_fid} after f1; bar now after it; "
          f"order={feat_ids}")


def test_rollback_delete_bar_feature():
    s = _new_part("rb3")
    docsess.apply_ops(s, 0, [_box_op(20, 20, 20)])             # f1
    docsess.apply_ops(s, 1, [_box_op(10, 10, 10, name="B2")])  # f2
    docsess.apply_ops(s, 2, [_box_op(5, 5, 5, name="B3")])     # f3
    docsess.apply_ops(s, s.revision, [{"op": "set_rollback", "after": "f2"}])
    # Delete the bar feature f2 -> bar moves to the previous surviving feature (f1).
    docsess.apply_ops(s, s.revision, [{"op": "delete_feature", "id": "f2"}])
    assert s.doc.get("rollback_after") == "f1", s.doc.get("rollback_after")
    assert validate_part(s.doc) == [], validate_part(s.doc)
    # Delete f1 too (now the bar feature) with only f3 after -> no preceding survivor -> null.
    docsess.apply_ops(s, s.revision, [{"op": "delete_feature", "id": "f1"}])
    assert s.doc.get("rollback_after") is None, s.doc.get("rollback_after")
    print("[rollback] deleting the bar feature moves the bar to the prior feature, then to null")


def test_rollback_validation_rejects_bad_id():
    doc = docsess._blank_part("v")
    doc["features"] = [{"id": "f1", "type": "box", "name": "B", "suppressed": False,
                        "params": {"length": _dim(1), "width": _dim(1), "height": _dim(1)},
                        "refs": {}}]
    try:
        _apply_ops_to_doc(doc, [{"op": "set_rollback", "after": "f99"}])
        raise AssertionError("set_rollback with unknown fid must raise")
    except OpError as e:
        assert e.code == "invalid_op", e.code
    # A doc carrying a dangling rollback_after fails schema validation.
    doc["rollback_after"] = "f99"
    assert any("rollback_after" in x for x in validate_part(doc)), validate_part(doc)
    print("[rollback] set_rollback bad id rejected; schema flags dangling rollback_after")


# --------------------------------------------------------------------------- worker subprocess


def test_worker_checkpoint_revert_owner_map():
    """Drive the REAL worker over stdio: owner_map on doc_apply_ops/doc_tree, and the
    doc_checkpoint/doc_revert PropertyManager-cancel round-trip."""
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def rpc(obj):
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("worker closed the pipe")
            m = json.loads(line)
            if "event" in m:
                continue
            return m

    try:
        did = rpc({"id": 1, "method": "doc_new", "params": {"kind": "part"}})["result"]["doc_id"]
        box = {"op": "add_feature", "feature": {"type": "box", "name": "B",
               "params": {"length": _dim(10), "width": _dim(10), "height": _dim(10)}}}
        r = rpc({"id": 2, "method": "doc_apply_ops",
                 "params": {"doc_id": did, "base_revision": 0, "ops": [box]}})
        assert r["ok"] and r["result"]["owner_map"]["f1"]["bodies"] == [1], r["result"]

        ck = rpc({"id": 3, "method": "doc_checkpoint",
                  "params": {"doc_id": did}})["result"]["checkpoint"]
        # Two edits, then revert.
        rpc({"id": 4, "method": "doc_apply_ops",
             "params": {"doc_id": did, "base_revision": 1, "ops": [box]}})
        rpc({"id": 5, "method": "doc_apply_ops",
             "params": {"doc_id": did, "base_revision": 2, "ops": [box]}})
        rv = rpc({"id": 6, "method": "doc_revert", "params": {"doc_id": did, "checkpoint": ck}})
        assert rv["ok"] and [f["id"] for f in rv["result"]["tree"]["features"]] == ["f1"], rv
        assert "owner_map" in rv["result"], rv["result"].keys()

        # One undo after revert -> the 3-box pre-revert state.
        u = rpc({"id": 7, "method": "doc_undo", "params": {"doc_id": did}})
        assert len(u["result"]["tree"]["features"]) == 3, u["result"]["tree"]["features"]

        # doc_tree carries owner_map; bad revert is a structured error.
        t = rpc({"id": 8, "method": "doc_tree", "params": {"doc_id": did}})
        assert "owner_map" in t["result"], t["result"].keys()
        bad = rpc({"id": 9, "method": "doc_revert", "params": {"doc_id": did, "checkpoint": "x"}})
        assert not bad["ok"] and bad["error"]["code"] == "invalid_op", bad

        rpc({"id": 10, "method": "shutdown", "params": {}})
        print("[worker] doc_checkpoint/doc_revert + owner_map RPC round-trip OK")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=30)


# --------------------------------------------------------------------------- runner


def main():
    test_owner_map_box_fillet()
    test_owner_map_in_tree_and_undo()
    test_constraints_in_sketch_response()
    test_distance_point_line()
    test_distance_axis_hv()
    test_angle_between_lines()
    test_diameter_reachable()
    test_driven_leaves_geometry_and_measures()
    test_placement_persists_and_set_dim()
    test_checkpoint_revert()
    test_rollback_bar()
    test_rollback_add_feature_mid_bar()
    test_rollback_delete_bar_feature()
    test_rollback_validation_rejects_bad_id()
    test_worker_checkpoint_revert_owner_map()
    print("\nENGINE OVERHAUL (C35) TESTS PASSED")


if __name__ == "__main__":
    main()
