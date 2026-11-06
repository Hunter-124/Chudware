"""Reinforcement proposals preserve the source and are remeasured on a shadow."""
import pytest

from services.mcp import core_bridge as cb
from services.mcp.dfm_fix_tools import _candidates, _fit_geometry, reinforce, wall_target
from services.mcp import dfm_fix_tools, dfm_fix_verify
from services.mcp.dfm_common import DfmContext


def test_wall_target_uses_extrusion_width_and_perimeters():
    target, evidence = wall_target({"min_wall_mm": 1.0, "rules": {
        "line_width_mm": 0.45, "perimeters": 3}})
    assert target == pytest.approx(1.35)
    assert evidence["line_width_mm"] == pytest.approx(0.45)
    assert wall_target({"min_wall_mm": 1.0}, 2.0)[0] == pytest.approx(2.0)
    with pytest.raises(ValueError):
        wall_target({"min_wall_mm": 1.0}, -1)


def test_shell_candidate_keeps_expression_driven_parameters_untouched():
    region = {"min_thickness_mm": 0.6, "built_by": [{"id": "f2"}]}
    features = [{"id": "f2", "type": "shell", "params": {"thickness": {"value": 0.6, "expr": None}}}]
    fixes = _candidates(region, features, 1.35)
    assert len(fixes) == 1
    assert fixes[0]["ops"][0]["params"]["thickness"]["value"] > 1.35
    features[0]["params"]["thickness"]["expr"] = "Wall"
    assert _candidates(region, features, 1.35) == []


def test_fit_geometry_detects_bore_and_opposed_slot_dimensions(monkeypatch):
    def faces(radius, slot_width):
        return {"matches": [
            {"surf": "cylinder", "owner_feature": "h", "radius": radius},
            {"surf": "plane", "owner_feature": "s", "normal": [1, 0, 0], "centroid": [0, 0, 0]},
            {"surf": "plane", "owner_feature": "s", "normal": [-1, 0, 0], "centroid": [slot_width, 0, 0]},
        ]}
    current = [faces(2, 3)]
    monkeypatch.setattr(cb, "call", lambda method, params: current[0])
    features = [{"id": "s", "type": "extrude", "params": {"mode": "cut"}}]
    before = _fit_geometry("doc", features)
    current[0] = faces(1.8, 2.5)
    after = _fit_geometry("clone", features)
    assert before["cylindrical_radii"] != after["cylindrical_radii"]
    assert before["opposed_cut_face_distances"] == [("s", 3.0)]
    assert after["opposed_cut_face_distances"] == [("s", 2.5)]

def test_rib_and_root_fillet_candidates_use_measured_root(monkeypatch):
    import numpy as np
    monkeypatch.setattr(dfm_fix_tools, "_region_points", lambda *a:
                        np.array([[0, 0, 0], [0, 0, 8], [8, 0, 0], [0, 8, 8]], dtype=float))
    region = {"body": 1, "bbox": {"min": [0, 0, 0], "max": [8, 8, 8]},
              "faces": ["wall"], "min_thickness_mm": 0.6}
    edges = [{"sid": "root", "body": 1, "mid": [0, 0, 4], "dir": [0, 0, 1],
              "faces": ["wall", "base"]}]
    faces = [{"sid": "wall", "normal": [1, 0, 0]},
             {"sid": "base", "normal": [0, 1, 0]}]
    candidates = dfm_fix_tools._rib_candidates(region, [], edges, faces, 1.35, 1)
    assert [c["reinforcement"] for c in candidates] == ["rib", "fillet"]
    assert candidates[0]["ops"][2]["feature"]["type"] == "rib"
    assert candidates[0]["thickness_mm"] == pytest.approx(1.35)
    # The web is sketched in the root's normal section, but stiffness must
    # be measured across the wall leg, about the root-parallel bending axis.
    rib = candidates[0]
    assert rib["ops"][0]["feature"]["params"]["normal"] == pytest.approx([0, 0, 1])
    assert rib["section_axis"] == pytest.approx([0, 1, 0])
    assert rib["bending_axis"] == pytest.approx([0, 0, 1])
    assert rib["section_origin"] == pytest.approx([0, (3 ** .5), 4])
    endpoints = rib["ops"][1]["feature"]["params"]["entities"][0]
    assert endpoints["p1"] == pytest.approx([2 * (3 ** .5), 0])
    assert endpoints["p2"] == pytest.approx([0, 2 * (3 ** .5)])
    assert candidates[1]["ops"][0]["feature"]["type"] == "fillet"
    assert candidates[1]["requested_radius_mm"] == pytest.approx(.6)




@pytest.mark.parametrize(("builds", "after_status", "expected_count"), [
    (False, "pass", 0), (True, "not_evaluated", 0), (True, "pass", 1),
])
def test_row_fixes_require_build_and_same_check_remeasurement(monkeypatch, builds, after_status, expected_count):
    ctx = DfmContext("original", "fdm", {"params": {}}, [0, 0, 1], "caller")
    row = {"check": "wall", "status": "fail", "fix_ops": [{"op": "edit_feature", "id": "f1"}]}
    def producer(fresh):
        assert fresh.options["verify_fixes"] is False
        assert fresh._cache == {}
        return {"check": "wall", "status": "fail" if fresh.doc_id == "original" else after_status}
    def verify(doc, ops, measure):
        assert doc == "original"
        assert ops == [{"op": "edit_feature", "id": "f1"}]
        return {"builds": builds, "before": measure("original"), "after": measure("clone"),
                "side_effects": {}, "error": None}
    monkeypatch.setattr(dfm_fix_verify, "verify_fix_ops", verify)
    result = dfm_fix_verify.verify_row_fixes(ctx, row, producer)
    assert len(result["fix_ops"]) == expected_count
    if expected_count:
        assert result["fix_ops"][0]["verified"] is True
        assert result["fix_ops"][0]["verification"]["after"] == "pass"
    else:
        assert result["fix_verification_rejected"]

def test_row_verification_disabled_drops_raw_operations(monkeypatch):
    ctx = DfmContext("original", "fdm", {"params": {}}, [0, 0, 1], "caller",
                     options={"verify_fixes": False})
    row = {"check": "wall", "status": "fail", "fix_ops": [{"op": "edit_feature", "id": "f1"}]}
    def forbidden(*args):
        raise AssertionError("must not verify")
    monkeypatch.setattr(dfm_fix_verify, "verify_fix_ops", forbidden)
    result = dfm_fix_verify.verify_row_fixes(ctx, row, forbidden)
    assert result["fix_ops"] == []
    assert result["status"] == "fail"


@pytest.mark.skipif(not cb.available(), reason="native engine unavailable")
def test_open_box_wall_fix_is_verified_without_source_mutation():
    doc = cb.call("doc_new", {"kind": "part", "name": "reinforcement_box", "internal": True})["doc_id"]
    try:
        result = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Block", "params": {"length": 40, "width": 30, "height": 20}}},
            {"op": "add_feature", "feature": {"type": "shell", "name": "Shell", "params": {"thickness": 0.6}, "refs": {"remove": {"of": "face", "side": "top"}}}},
        ]})
        assert all(s.get("ok") for s in result["rebuild"]["feature_status"])
        before = cb.call("doc_tree", {"doc_id": doc})
        report = reinforce(doc, process="fdm", target_wall_mm=1.35)
        fixes = [fix for region in report["regions"] for fix in region["verified_fixes"]]
        assert fixes, report
        assert all(fix["after_mm"] >= report["target_wall_mm"] - 1e-4 for fix in fixes)
        assert cb.call("doc_tree", {"doc_id": doc}) == before
    finally:
        cb.call("doc_close", {"doc_id": doc})


@pytest.mark.skipif(not cb.available(), reason="native engine unavailable")
def test_sketch_driven_thin_tab_returns_verified_dimension_edit():
    doc = cb.call("doc_new", {"kind": "part", "name": "thin_tab", "internal": True})["doc_id"]
    try:
        points = [[0, 0], [20, 0], [20, 0.6], [0, 0.6]]
        entities = [{"id": f"e{i+1}", "type": "line", "p1": points[i],
                     "p2": points[(i+1) % 4]} for i in range(4)]
        constraints = [{"id": f"c{i+1}", "type": "coincident",
                        "refs": [f"e{i+1}.p2", f"e{(i+1)%4+1}.p1"]} for i in range(4)]
        constraints += [{"id": "c5", "type": "horizontal", "refs": ["e1"]},
                        {"id": "c6", "type": "vertical", "refs": ["e2"]},
                        {"id": "c7", "type": "horizontal", "refs": ["e3"]},
                        {"id": "c8", "type": "vertical", "refs": ["e4"]},
                        {"id": "c9", "type": "length", "refs": ["e2"], "value": {"value": 0.6}}]
        built = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "sketch", "name": "Tab",
                "refs": {"plane": "plane.top"}, "params": {"entities": entities,
                "constraints": constraints}}},
            {"op": "add_feature", "feature": {"type": "extrude", "name": "Tab body",
                "params": {"depth": 8, "mode": "add"}, "refs": {"profile": "$0"}}}]})
        assert all(s.get("ok") for s in built["rebuild"]["feature_status"]), built
        before = cb.call("doc_tree", {"doc_id": doc})
        result = reinforce(doc, process="fdm", target_wall_mm=1.35)
        fixes = [fix for item in result["regions"] for fix in item["verified_fixes"]
                 if fix.get("control") == "sketch_dimension"]
        assert fixes, result
        assert all(fix["after_mm"] >= 1.35 - 1e-4 for fix in fixes)
        assert cb.call("doc_tree", {"doc_id": doc}) == before
    finally:
        cb.call("doc_close", {"doc_id": doc})


@pytest.mark.skipif(not cb.available(), reason="native engine unavailable")
def test_fit_limited_l_bracket_offers_verified_rib():
    doc = cb.call("doc_new", {"kind": "part", "name": "fit_limited_bracket", "internal": True})["doc_id"]
    try:
        points = [[0, 0], [20, 0], [20, .6], [.6, .6], [.6, 20], [0, 20]]
        entities = [{"id": f"e{i+1}", "type": "line", "p1": points[i],
                     "p2": points[(i+1) % len(points)]} for i in range(len(points))]
        built = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "sketch", "name": "L section",
                "refs": {"plane": "plane.top"}, "params": {"entities": entities}}},
            {"op": "add_feature", "feature": {"type": "extrude", "name": "Bracket",
                "params": {"depth": 8, "mode": "add"}, "refs": {"profile": "$0"}}},
            {"op": "add_feature", "feature": {"type": "cylinder", "name": "Fit notch",
                "params": {"radius": .2, "height": 8, "mode": "cut"},
                "at": [10, .6, 4]}}]})
        assert all(s.get("ok") for s in built["rebuild"]["feature_status"]), built
        before = cb.call("doc_tree", {"doc_id": doc})
        report = reinforce(doc, process="fdm", target_wall_mm=1.35)
        ribs = [fix for item in report["regions"] for fix in item["verified_fixes"]
                if fix["kind"] == "reinforce" and fix["reinforcement"] == "rib"]
        assert ribs, report
        assert all(fix["stiffness_comparison"]["ratio_to_target"] >= 1 for fix in ribs)
        assert all(fix["thickness_mm"] >= report["target_wall_mm"] for fix in ribs)
        assert all(fix["side_effects"]["fit_and_clearance"]["status"] == "measured_cards_unchanged"
                   for fix in ribs)
        assert all(fix["side_effects"]["section_before"]["Ixx"] > 0
                   and fix["side_effects"]["section_after"]["Ixx"]
                   > fix["side_effects"]["section_before"]["Ixx"] for fix in ribs)
        assert cb.call("doc_tree", {"doc_id": doc}) == before
    finally:
        cb.call("doc_close", {"doc_id": doc})
