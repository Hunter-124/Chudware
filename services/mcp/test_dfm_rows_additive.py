from types import SimpleNamespace

import numpy as np
import pytest

from services.mcp import core_bridge as cb
from services.mcp import cad_eval_tools, dfm_fix_tools, dfm_fix_verify
from services.mcp import dfm_rows_additive as additive
from services.mcp.dfm_common import DfmContext


def test_concave_corner_reports_located_fillet_proposal(monkeypatch):
    ctx = DfmContext("d", "fdm", {"params": {"min_wall_mm": 1,
        "rules": {"internal_corner_radius_wall_ratio": 1},
        "rule_sources": {"internal_corner_radius_wall_ratio": "process guidance"}}},
        [0, 0, 1], "caller")
    monkeypatch.setattr(ctx, "edge_analysis", lambda **kw: {"edges": [
        {"sid": "E1", "kind": "concave", "mid": [3, 4, 5],
         "faces": ["F1", "F2"], "owner_feature": "f1", "dihedral_deg": 270}],
        "concave_blends": []})
    monkeypatch.setattr(cad_eval_tools, "_eval_thickness_all_bodies",
                        lambda *a: ({"measurement_basis": {"spacing_mm": 0.1}}, []))
    monkeypatch.setattr(dfm_fix_tools, "_local_field_min", lambda *a: (2.0, 8))
    monkeypatch.setattr(dfm_fix_verify, "verify_fix_ops", lambda *a: {
        "builds": True, "side_effects": {"feature_status": [
            {"data": {"fillet": {"effective": {"radius": 2},
             "selected_edges": 1, "covered_edges": 1}}}]}})
    result = additive.rows(ctx, ["sharp_internal_corners"])[0]
    assert result["status"] == "fail"
    assert result["regions"][0]["faces"] == ["F1", "F2"]
    assert result["fix_ops"][0]["ops"][0]["feature"]["type"] == "fillet"
    assert result["fix_ops"][0]["verified"] is True
    assert result["fix_ops"][0]["effective_radius_mm"] == 2


def test_corner_verification_can_be_disabled_without_proposing_ops(monkeypatch):
    ctx = DfmContext("d", "fdm", {"params": {"min_wall_mm": 1,
        "rules": {"internal_corner_radius_wall_ratio": 1}}},
        [0, 0, 1], "caller", options={"verify_fixes": False})
    monkeypatch.setattr(ctx, "edge_analysis", lambda **kw: {"edges": [
        {"sid": "E1", "mid": [0, 0, 0], "faces": ["F1", "F2"]}]})
    monkeypatch.setattr(cad_eval_tools, "_eval_thickness_all_bodies",
                        lambda *a: ({"measurement_basis": {"spacing_mm": .1}}, []))
    monkeypatch.setattr(dfm_fix_tools, "_local_field_min", lambda *a: (1, 4))
    def should_not_verify(*args):
        raise AssertionError("verification must not run")
    monkeypatch.setattr(dfm_fix_verify, "verify_fix_ops", should_not_verify)
    result = additive.rows(ctx, ["sharp_internal_corners"])[0]
    assert result["status"] == "fail"
    assert result["fix_ops"] == []
    assert result["regions"][0]["fix_unavailable_reason"] == "Fix verification disabled."


def test_build_aligned_boss_is_flagged_as_possible_layer_risk(monkeypatch):
    ctx = DfmContext("d", "fdm", {"params": {"rules": {"min_pin_mm": 3}}},
                     [0, 0, 1], "caller")
    monkeypatch.setattr(ctx, "faces", lambda: [{"sid": "F1", "surf": "cylinder",
        "concavity": "boss", "radius": 1, "axis": [0, 0, 1],
        "centroid": [0, 0, 2], "owner_feature": "f1"}])
    mesh = SimpleNamespace(faces=[SimpleNamespace(sid="F1", owner_feature="f1")],
                           vertices=np.array([[1, 0, 0], [0, 1, 0], [1, 0, 10]]),
                           triangles=np.array([[0, 1, 2]]), tri_face=np.array([0]))
    monkeypatch.setattr(ctx, "mesh", lambda: mesh)
    pin, layers = additive.rows(ctx, ["min_pin", "layer_anisotropy"])
    assert pin["status"] == "fail"
    assert pin["regions"][0]["diameter_mm"] == 2
    assert layers["status"] == "warn"
    assert layers["regions"][0]["faces"] == ["F1"]
    assert layers["regions"][0]["slenderness"] == 5
    assert layers["regions"][0]["suggested_rotation"]["angle_deg"] == pytest.approx(90)


@pytest.mark.skipif(not cb.available(), reason="native engine unavailable")
def test_engine_build_aligned_cylinder_reports_measured_slenderness():
    doc = cb.call("doc_new", {"kind": "part", "name": "layer_pin", "internal": True})["doc_id"]
    try:
        result = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "cylinder", "name": "Pin",
             "params": {"radius": 1, "height": 12}}}]})
        assert all(s.get("ok") for s in result["rebuild"]["feature_status"]), result
        ctx = DfmContext(doc, "fdm", {"params": {}}, [0, 0, 1], "caller")
        row = additive.rows(ctx, ["layer_anisotropy"])[0]
        assert row["status"] == "warn", row
        assert row["regions"][0]["slenderness"] == pytest.approx(6, rel=0.05)
    finally:
        cb.call("doc_close", {"doc_id": doc})
