"""Internal-document integration coverage for resolved bend records and checkpoints."""
import math

import pytest

from services.mcp import core_bridge as cb

pytestmark = pytest.mark.skipif(not cb.available(), reason="native engine unavailable")


def test_bend_records_follow_incremental_rebuild(monkeypatch):
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    doc = cb.call("doc_new", {"kind": "part", "name": "sheet_record", "internal": True})["doc_id"]
    try:
        base = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("sketch", "profile", {"entities": [{"type": "rectangle", "center": [0, 0],
                                                           "width": 60, "height": 40}]},
                    {"plane": "plane.top"}),
            cb.feat("sm_base_flange", "base", {"thickness": 1.5, "default_bend_radius": 2,
                                                  "k_factor": 0.4}, {"profile": "$0"}),
        ]})
        assert all(s.get("ok") for s in base["rebuild"]["feature_status"]), base
        assert cb.call("eval_sheet_metal_bends", {"doc_id": doc})["records"] == []
        edge = cb.call("doc_query", {"doc_id": doc, "query": {
            "of": "edge", "surf": "line", "near": [30, 0, 1.5], "count": 1}})["matches"][0]
        added = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("sm_edge_flange", "flange", {"length": 12, "angle": 90}, {"edge": edge})]})
        assert all(s.get("ok") for s in added["rebuild"]["feature_status"]), added
        fid = added["id_map"]["$0"]
        first = cb.call("eval_sheet_metal_bends", {"doc_id": doc})["records"]
        assert len(first) == 1
        assert first[0]["feature_id"] == fid
        assert first[0]["radius_mm"] == pytest.approx(2)
        assert first[0]["thickness_mm"] == pytest.approx(1.5)
        assert first[0]["wall_length_mm"] == pytest.approx(12)
        assert first[0]["bend_allowance_mm"] == pytest.approx(math.pi / 2 * 2.6)
        changed = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "edit_feature", "id": fid, "params": {"length": 15}}]})
        assert all(s.get("ok") for s in changed["rebuild"]["feature_status"]), changed
        second = cb.call("eval_sheet_metal_bends", {"doc_id": doc})["records"]
        assert len(second) == 1 and second[0]["feature_id"] == fid
        assert second[0]["wall_length_mm"] == pytest.approx(15)
        suffix = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("sketch", "reference", {"entities": [{"type": "rectangle", "center": [0, 0],
                                                            "width": 10, "height": 8}]},
                    {"plane": "plane.top"})]})
        assert all(s.get("ok") for s in suffix["rebuild"]["feature_status"]), suffix
        restored = cb.call("eval_sheet_metal_bends", {"doc_id": doc})["records"]
        assert len(restored) == 1 and restored[0]["feature_id"] == fid
        assert restored[0]["wall_length_mm"] == pytest.approx(15)
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_hole_near_resolved_bend_is_reported():
    from services.mcp.dfm_common import DfmContext
    from services.mcp.dfm_rows_sheetmetal import rows

    doc = cb.call("doc_new", {"kind": "part", "name": "sheet_hole_bend", "internal": True})["doc_id"]
    try:
        base = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("sketch", "profile", {"entities": [{"type": "rectangle", "center": [0, 0],
                                                            "width": 60, "height": 40}]},
                    {"plane": "plane.top"}),
            cb.feat("sm_base_flange", "base", {"thickness": 1.5, "default_bend_radius": 2,
                                                   "k_factor": 0.4}, {"profile": "$0"})]})
        assert all(s.get("ok") for s in base["rebuild"]["feature_status"]), base
        edge = cb.call("doc_query", {"doc_id": doc, "query": {
            "of": "edge", "surf": "line", "near": [30, 0, 1.5], "count": 1}})["matches"][0]
        flange = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("sm_edge_flange", "flange", {"length": 12, "angle": 90}, {"edge": edge})]})
        assert all(s.get("ok") for s in flange["rebuild"]["feature_status"]), flange
        bend = cb.call("eval_sheet_metal_bends", {"doc_id": doc})["records"][0]
        tangent = {"material_inside": -bend["radius_mm"] - bend["thickness_mm"],
                   "material_outside": -bend["radius_mm"], "bend_outside": 0}[bend["position"]]
        point = [(a + b) / 2 + d * (tangent - 3)
                 for a, b, d in zip(bend["start"], bend["end"], bend["length_dir"])]
        face = cb.call("doc_query", {"doc_id": doc, "query": {
            "of": "face", "surf": "plane", "near": point, "count": 1}})["matches"][0]
        hole = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("hole", "near bend", {"style": "simple", "end": "through_all",
                                             "diameter": 2, "world_points": [point]}, {"face": face})]})
        assert all(s.get("ok") for s in hole["rebuild"]["feature_status"]), hole
        faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]
        bend_faces = [f for f in faces if f["owner_feature"] == flange["id_map"]["$0"]
                      and f["surf"] == "cylinder" and
                      f["radius"] == pytest.approx(bend["radius_mm"])]
        hole_faces = [f for f in faces if f["owner_feature"] == hole["id_map"]["$0"]
                      and f["surf"] == "cylinder"]
        assert bend_faces and hole_faces, faces
        body = bend_faces[0]["body"]
        body_faces = cb.call("doc_query", {"doc_id": doc, "query": {
            "of": "face", "body_id": body}})["matches"]
        assert {(f["topo"], f["owner_feature"]) for f in body_faces} == {
            (f["topo"], f["owner_feature"]) for f in faces if f["body"] == body}
        edges = cb.call("doc_query", {"doc_id": doc, "query": {"of": "edge"}})["matches"]
        body_edges = cb.call("doc_query", {"doc_id": doc, "query": {
            "of": "edge", "body_id": body}})["matches"]
        assert any(e["owner_feature"] == flange["id_map"]["$0"] for e in edges)
        assert {(e["topo"], e["owner_feature"]) for e in body_edges} == {
            (e["topo"], e["owner_feature"]) for e in edges if e["body"] == body}
        profile = {"params": {"rules": {"hole_to_bend_thickness_factor": 2.5},
                              "rule_sources": {"hole_to_bend_thickness_factor": "MJM Sheet Metal DFM Guide, section 04"}}}
        ctx = DfmContext(doc, "sheet_metal", profile, [0, 0, 1], "caller",
                         options={"verify_fixes": False})
        result = rows(ctx, ["hole_to_bend"])[0]
        assert result["status"] == "warn", result
        assert result["metrics"]["minimum_screened_distance_mm"] == pytest.approx(2)
        assert result["limits"]["minimum_mm"] == pytest.approx(5.75)
        assert result["regions"][0]["faces"]
    finally:
        cb.call("doc_close", {"doc_id": doc})
