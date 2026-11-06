"""Behavior tests for sheet-metal DfM evidence and rule handling."""
import math

import pytest

from services.mcp.dfm_common import DfmContext
from services.mcp import dfm_rows_sheetmetal as sheet


@pytest.fixture
def evidence(monkeypatch):
    record = {"feature_id": "f2", "thickness_mm": 2.0, "radius_mm": 1.0,
              "wall_length_mm": 6.0, "angle_deg": 90.0,
              "bend_allowance_mm": math.pi / 2 * 1.8}
    mass = {"material": {"source": "part", "key": "alloy"}}
    def call(method, _params):
        if method == "eval_sheet_metal_bends":
            return {"records": [record]}
        if method == "eval_mass":
            return mass
        raise AssertionError(method)
    monkeypatch.setattr(sheet.cb, "call", call)
    profile = {"params": {"rules": {"min_flange_length_thickness_factor": 4,
                                     "min_bend_radius_per_thickness": {"alloy": 1},
                                     "k_factor_min": 0.3, "k_factor_max": 0.5},
                           "rule_sources": {k: "test rule" for k in (
                               "min_flange_length_thickness_factor", "min_bend_radius_per_thickness",
                               "k_factor_min", "k_factor_max")}}}
    ctx = DfmContext("d", "sheet_metal", profile, [0, 0, 1], "default")
    ctx._cache["features"] = [{"id": "f1", "type": "sm_base_flange", "params": {}},
                              {"id": "f2", "type": "sm_edge_flange", "params": {}}]
    ctx._cache["faces"] = [{"sid": "face-1", "owner_feature": "f2", "centroid": [2, 3, 4],
                            "surf": "cylinder", "radius": 1.0, "body": 1}]
    return ctx, record, mass


def test_flange_failure_and_fix_use_compiled_length(evidence):
    ctx, record, _ = evidence
    row = sheet.rows(ctx, ["flange_length"])[0]
    assert row["status"] == "fail"
    assert row["metrics"]["flange_length_mm"] == 6
    assert row["limits"]["minimum_mm"] == 8
    assert row["regions"][0]["built_by"] == ["f2"]
    assert row["regions"][0]["faces"] == ["face-1"]
    assert row["fix_ops"] == []
    record["wall_length_mm"] = 9
    assert sheet.rows(ctx, ["flange_length"])[0]["status"] == "pass"


def test_material_radius_and_k_factor(evidence):
    ctx, record, mass = evidence
    radius, k = sheet.rows(ctx, ["bend_radius", "k_factor"])
    assert radius["status"] == "fail" and radius["limits"]["minimum_mm"] == 2
    assert k["status"] == "pass" and k["metrics"]["k_factor"] == pytest.approx(0.4)
    record["bend_allowance_mm"] = math.pi / 2 * 2.4
    assert sheet.rows(ctx, ["k_factor"])[0]["status"] == "fail"
    mass["material"]["source"] = "default"
    assert sheet.rows(ctx, ["bend_radius"])[0]["status"] == "not_evaluated"


def test_screened_through_hole_and_ambiguous_face(evidence):
    ctx, bend, _ = evidence
    bend.update({"start": [0, 0, 0], "end": [0, 20, 0], "length_dir": [1, 0, 0],
                 "fold_dir": [0, 0, 1], "position": "material_inside"})
    ctx.profile["params"]["rules"]["hole_to_bend_thickness_factor"] = 2.5
    ctx.profile["params"]["rule_sources"]["hole_to_bend_thickness_factor"] = "test rule"
    ctx._cache["features"].insert(1, {"id": "hole", "type": "hole", "params": {
        "end": "through_all", "world_points": [[-5, 10, 0]]}})
    face = {"sid": "bore", "owner_feature": "hole", "surf": "cylinder", "concavity": "bore",
            "radius": 1, "area_or_len": 4 * math.pi, "axis": [0, 0, 1], "body": 1,
            "axis_point": [-5, 10, 0], "centroid": [-5, 10, 1]}
    ctx._cache["faces"].append(face)
    row = sheet.rows(ctx, ["hole_to_bend"])[0]
    assert row["status"] == "warn"
    assert row["metrics"]["minimum_screened_distance_mm"] == pytest.approx(1)
    assert row["limits"]["minimum_mm"] == 6
    assert row["fix_ops"] == []
    face["area_or_len"] /= 2
    assert sheet.rows(ctx, ["hole_to_bend"])[0]["status"] == "not_evaluated"


def test_later_geometry_and_missing_sources_never_pass(evidence):
    ctx, _, _ = evidence
    ctx._cache["features"].append({"id": "f3", "type": "hole", "params": {}})
    assert sheet.rows(ctx, ["flange_length"])[0]["status"] == "not_evaluated"
    assert all(row["status"] == "not_evaluated" for row in
               sheet.rows(ctx, ["hole_to_bend", "hole_to_edge", "bend_relief"]))
    del ctx.profile["params"]["rules"]["k_factor_min"]
    assert sheet.rows(ctx, ["k_factor"])[0]["status"] == "not_evaluated"


def test_hole_to_edge_uses_outer_wire_not_inner_hole(evidence, monkeypatch):
    ctx, _, _ = evidence
    ctx.profile["params"]["rules"]["hole_to_edge_thickness_factor"] = 2
    ctx.profile["params"]["rule_sources"]["hole_to_edge_thickness_factor"] = "test rule"
    ctx._cache["faces"].extend([
        {"sid": "bore", "surf": "cylinder", "concavity": "bore", "body": 1,
         "axis": [0, 0, 1], "axis_point": [2, 5, 0], "radius": 1,
         "centroid": [2, 5, 1], "owner_feature": "hole"},
        {"sid": "top", "surf": "plane", "normal": [0, 0, 1], "body": 1,
         "centroid": [5, 5, 0], "owner_feature": "sheet"}])
    call = sheet.cb.call
    def geometry(method, params):
        if method == "doc_adjacency":
            return {"edges": [{"between_sids": ["bore", "top"]}]}
        if method == "doc_face_wires":
            return {"plane_frame": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                    "wires": [{"outer": True, "entity_ids": ["a", "b", "c", "d"]}],
                    "entities": [
                        {"id": "a", "type": "line", "p1": [0, 0], "p2": [10, 0]},
                        {"id": "b", "type": "line", "p1": [10, 0], "p2": [10, 10]},
                        {"id": "c", "type": "line", "p1": [10, 10], "p2": [0, 10]},
                        {"id": "d", "type": "line", "p1": [0, 10], "p2": [0, 0]}]}
        return call(method, params)
    monkeypatch.setattr(sheet.cb, "call", geometry)
    result = sheet.rows(ctx, ["hole_to_edge"])[0]
    assert result["status"] == "warn"
    assert result["regions"][0]["hole_edge_distance_mm"] == pytest.approx(1)
    assert result["limits"]["minimum_mm"] == 4
    ctx.process = "cnc_mill"
    ctx.profile["params"]["rules"]["hole_to_edge_diameter_factor"] = 1.5
    ctx.profile["params"]["rule_sources"]["hole_to_edge_diameter_factor"] = "test rule"
    result = sheet._hole_to_edge(ctx)
    assert result["check"] == "hole_edge_distance"
    assert result["status"] == "warn"
    assert result["regions"][0]["minimum_mm"] == pytest.approx(3)
