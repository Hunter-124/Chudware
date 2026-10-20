"""Pure-Python sketch intent grading, atomic auto-definition, and MCP contracts."""
from __future__ import annotations

import copy
import json

import pytest

from services.mcp import ascii2d as A
from services.mcp import cad_doc_tools as D
from services.mcp import cad_eval_tools as E
from services.mcp import gsl_tools as G
from services.mcp import recon_tools as R


class Tools:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def circle_feature(*, constraints=None, **params):
    return {"op": "add_feature", "feature": {
        "type": "sketch", "name": "Profile", "refs": {"plane": "plane.top"},
        "params": {"entities": [{"id": "e1", "type": "circle", "center": [0, 0],
                                  "radius": 4, "construction": False}],
                   "constraints": constraints or [], **params}}}


def intent(constraints, *, health=None, feeds_feature=True):
    return A.sketch_design_intent(circle_feature()["feature"]["params"]["entities"], constraints,
                                  health=health or {"dof": 0, "status": "full"},
                                  feeds_feature=feeds_feature)


def test_pins_do_not_count_as_parametric_design_intent():
    result = intent([{"id": "c1", "type": "fix", "refs": ["e1"]}])
    assert result["pinned_only"] is True
    assert result["driving_dimensions"] == 0
    assert {row["code"] for row in result["findings"]} == {
        "pinned_instead_of_constrained", "no_driving_dimensions", "unreferenced_profile"}
    assert all("sketch_fully_define" in row["message"] for row in result["findings"])


def test_external_pins_are_legitimate_references_not_profile_pins():
    entities = circle_feature()["feature"]["params"]["entities"] + [
        {"id": "e2", "type": "point", "p": [0, 0], "construction": True}]
    result = A.sketch_design_intent(entities, [
        {"id": "c1", "type": "fix", "refs": ["e2"]},
        {"id": "c2", "type": "coincident", "refs": ["e1.center", "e2"]},
        {"id": "c3", "type": "radius", "refs": ["e1"], "value": {"value": 4}}],
        health={"dof": 0, "external_refs": [{"entity_ids": ["e2"]}]}, feeds_feature=True)
    assert result["anchored"] is True
    assert result["pinned_only"] is False
    assert result["findings"] == []


def test_fixed_external_profile_itself_does_not_trigger_quality_findings():
    result = intent([{"type": "fix", "refs": ["e1"]}],
                    health={"dof": 0, "external_refs": [{"entity_ids": ["e1"]}]})
    assert result["findings"] == []
    assert result["pinned_only"] is False


def test_reference_dimensions_and_unrelated_origin_relation_do_not_hide_weak_profile():
    constraints = [{"type": "radius", "refs": ["e1"], "driven": True},
                   {"type": "coincident", "refs": ["e2.p1", "origin"]}]
    result = intent(constraints, health={"dof": 3})
    assert not result["anchored"]
    assert {row["code"] for row in result["findings"]} == {
        "no_driving_dimensions", "unreferenced_profile", "under_dimensioned"}


def test_driving_dimensions_and_origin_reference_grade_cleanly():
    result = intent([{"type": "coincident", "refs": ["e1.center", "origin"]},
                     {"type": "radius", "refs": ["e1"], "value": {"value": 4}}])
    assert result["findings"] == []
    assert result["anchored"] and result["driving_dimensions"] == 1


def test_disconnected_profile_requires_its_own_reference():
    entities = [{"id": "e1", "type": "circle"}, {"id": "e2", "type": "circle"}]
    result = A.sketch_design_intent(entities, [
        {"type": "coincident", "refs": ["e1.center", "origin"]}], health={"dof": 3})
    assert result["anchored"] is False


def test_missing_dimensions_finding_only_for_feature_consumers():
    assert "no_driving_dimensions" not in {
        row["code"] for row in intent([], feeds_feature=False)["findings"]}
    features = [{"id": "s1", "type": "sketch"},
                {"id": "f2", "type": "extrude", "refs": {"profile": [{"sketch": "s1"}]}}]
    assert A.sketch_feeds_feature(features, "s1")
    features[1]["suppressed"] = True
    assert not A.sketch_feeds_feature(features, "s1")


def test_definition_follows_explicit_intent_and_precedes_solid_consumption():
    ops = [circle_feature(), {"op": "sketch_add_constraint", "sketch": "$0",
                             "constraint": {"type": "radius", "refs": ["e1"],
                                            "value": {"value": 4}}},
           {"op": "add_feature", "feature": {"type": "extrude",
            "refs": {"profile": [{"sketch": "$0"}]}, "params": {"depth": 2}}},
           {"op": "rename", "id": "$2", "name": "Body"}]
    original = copy.deepcopy(ops)
    mapping = {}
    result = D._append_sketch_auto_define(ops, index_map=mapping)
    assert ops == original
    assert result[2] == {"op": "sketch_fully_define", "sketch": "$0"}
    assert result[3]["feature"]["refs"]["profile"][0]["sketch"] == "$0"
    assert result[4]["id"] == "$3"
    assert D._authored_batch_id_map({"$0": "f1", "$2": "c1", "$2#1": "c2", "$3": "f2"},
                                    mapping) == {"$0": "f1", "$2": "f2"}


def test_all_new_sketches_are_defined_and_future_placeholders_renumbered():
    result = D._append_sketch_auto_define([circle_feature(), circle_feature(),
                                          {"op": "sketch_add_entity", "sketch": "$1",
                                           "entity": {"type": "point", "p": [1, 2]}}])
    assert [op["op"] for op in result] == ["add_feature", "sketch_fully_define",
                                           "add_feature", "sketch_add_entity", "sketch_fully_define"]
    assert result[-1]["sketch"] == result[-2]["sketch"] == "$2"


@pytest.mark.parametrize("level", ["call", "feature", "params", "op"])
def test_auto_define_opt_out_at_each_supported_level(level):
    op = circle_feature()
    if level == "feature":
        op["feature"]["auto_define"] = False
    elif level == "params":
        op["feature"]["params"]["auto_define"] = False
    elif level == "op":
        op["auto_define"] = False
    result = D._append_sketch_auto_define([op], auto_define=level != "call")
    assert len(result) == 1
    assert "auto_define" not in result[0]


def test_entity_batch_defines_once_after_all_intent_ops(monkeypatch):
    monkeypatch.setattr(D.cb, "call", lambda *a, **k: {"tree": {"features": []}})
    result = D._append_sketch_auto_define([
        {"op": "sketch_add_entity", "sketch": "s1", "entity": {}},
        {"op": "sketch_add_entity", "sketch": "s1", "entity": {}},
        {"op": "sketch_add_constraint", "sketch": "s1", "constraint": {}}], doc_id="d")
    assert len(result) == 4
    assert result[-1] == {"op": "sketch_fully_define", "sketch": "s1"}


def test_entity_batches_respect_persisted_and_per_op_opt_out(monkeypatch):
    monkeypatch.setattr(D.cb, "call", lambda *a, **k: {"tree": {"features": [
        {"id": "s1", "type": "sketch", "params": {"auto_define": False}}]}})
    assert len(D._append_sketch_auto_define([
        {"op": "sketch_add_entity", "sketch": "s1", "entity": {}}], doc_id="d")) == 1
    assert len(D._append_sketch_auto_define([
        {"op": "sketch_add_entity", "sketch": "s2", "entity": {}},
        {"op": "sketch_add_entity", "sketch": "s2", "entity": {}, "auto_define": False}],
        doc_id="d")) == 2


def test_explicit_fully_define_does_not_get_duplicate_automatic_pass():
    ops = [circle_feature(), {"op": "sketch_fully_define", "sketch": "$0", "anchor": "none"}]
    assert D._append_sketch_auto_define(ops) == ops


def test_readback_reports_actual_authored_constraints_and_intent(monkeypatch):
    feature = circle_feature(constraints=[{"id": "c1", "type": "fix", "refs": ["e1"]}])["feature"]
    feature["id"] = "s1"
    def call(method, params):
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        if method == "doc_sketch_overview":
            return {"entities": feature["params"]["entities"]}  # overview has no constraints
        if method == "sketch_health":
            return {"ok": True, "dof": 0, "status": "full", "closure": {"open_endpoints": []}}
        raise AssertionError(method)
    monkeypatch.setattr(D.cb, "call", call)
    result = A.live_sketch_readback("d", "s1")
    assert result["pinned_only"] is True
    assert result["dof"] == 0
    assert "pinned_instead_of_constrained" in {row["code"] for row in result["findings"]}


@pytest.mark.parametrize("status,dof,pinned,expected", [
    ("full", 0, True, False), ("full", 0, False, True),
    ("full", 3, False, False), ("over", 0, False, False), ("unknown", None, False, False)])
def test_check_sketch_truthful_fully_defined(monkeypatch, status, dof, pinned, expected):
    constraints = ([{"id": "c1", "type": "fix", "refs": ["e1"]}] if pinned else [
        {"id": "c1", "type": "radius", "refs": ["e1"], "value": {"value": 4}},
        {"id": "c2", "type": "coincident", "refs": ["e1.center", "origin"]}])
    feature = {"id": "s1", **circle_feature(constraints=constraints)["feature"]}
    def call(method, params):
        if method == "sketch_health":
            assert params == {"doc_id": "d", "sketch": "s1"}
            return {"ok": True, "status": status, "dof": dof}
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        raise AssertionError(method)
    monkeypatch.setattr(E.cb, "call", call)
    monkeypatch.setattr(E, "_doc", lambda d: d)
    tools = Tools()
    E.register_cad_eval_tools(tools)
    result = json.loads(tools.tools["check_sketch"](sketch="s1", doc_id="d"))
    assert result["ok"], result
    assert result["fully_defined"] is expected
    assert result["pinned_only"] is pinned


def test_fully_define_tool_calls_op_and_reports_exact_additions(monkeypatch):
    constraints = [{"id": "c1", "type": "coincident", "refs": ["e1.center", "origin"]},
                   {"id": "c2", "type": "radius", "refs": ["e1"], "value": {"value": 4}}]
    feature = {"id": "s1", **circle_feature(constraints=constraints)["feature"]}
    calls = []
    def call(method, params):
        calls.append((method, params))
        if method == "doc_apply_ops":
            return {"revision": 7, "id_map": {"$0": "c1", "$0#1": "c2"},
                    "tree": {"features": [feature]}}
        if method == "sketch_health":
            return {"ok": True, "status": "full", "dof": 0}
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        raise AssertionError(method)
    monkeypatch.setattr(G.cb, "call", call)
    monkeypatch.setattr(G, "_doc", lambda d: d)
    tools = Tools()
    G.register_gsl_tools(tools)
    result = json.loads(tools.tools["sketch_fully_define"](
        sketch="s1", relations=False, anchor="none", entities=["e1"], doc_id="d"))
    assert calls[0] == ("doc_apply_ops", {"doc_id": "d", "ops": [{
        "op": "sketch_fully_define", "sketch": "s1", "relations": False,
        "dimensions": True, "anchor": "none", "entities": ["e1"]}]})
    assert result["ok"] and result["dof"] == 0 and not result["pinned_only"]
    assert (result["relations_added"], result["dimensions_added"]) == (1, 1)


def test_project_into_sketch_supports_contract_reference_kinds(monkeypatch):
    refs = [{"kind": "vertex", "selector": {"sid": "V123"}},
            {"kind": "edge", "selector": {"sid": "E123"}, "mode": "pierce", "near": [1, 2]},
            {"kind": "sketch_entity", "sketch": "s0", "entity": "e3"}, "axis.z", "origin"]
    calls = []
    def call(method, params):
        assert method == "doc_apply_ops"
        calls.append(params)
        return {"id_map": {"$0": "e1"}, "tree": {"features": []}}
    monkeypatch.setattr(R.cb, "call", call)
    monkeypatch.setattr(R, "_doc", lambda d: d)
    tools = Tools()
    R.register_recon_tools(tools)
    result = json.loads(tools.tools["project_into_sketch"](
        sketch="s1", refs=refs, construction=True, doc_id="d"))
    assert result["ok"] and result["construction"]
    assert calls[0]["ops"] == [{"op": "sketch_convert", "sketch": "s1",
        "refs": refs[:3] + [{"datum": "axis.z"}, {"datum": "origin"}],
        "construction": True, "associative": True}]


def test_virtual_axis_and_new_dimension_normalize_without_dangling_ref():
    feature = circle_feature()["feature"]
    feature["params"]["entities"] += [{"id": "e2", "type": "circle", "center": [8, 0],
                                         "radius": 4}]
    feature["params"]["constraints"] = [
        {"type": "symmetric", "refs": ["e1", "e2", "axis.v"]},
        {"type": "arc_length", "refs": ["e1"], "value": 12}]
    normalized = D._normalize_sketch_feature(feature)
    assert normalized["params"]["constraints"][0]["refs"][-1] == "axis.v"
    assert normalized["params"]["constraints"][1]["value"] == {"value": 12, "expr": None}


def test_inserted_definition_renumbers_converted_point_handles():
    result = D._append_sketch_auto_define([
        circle_feature(), circle_feature(),
        {"op": "sketch_convert", "sketch": "$1", "refs": [{"datum": "origin"}]},
        {"op": "sketch_add_constraint", "sketch": "$1", "constraint": {
            "type": "coincident", "refs": ["e1.center", "$2#1.p"]}}])
    constraint = next(op for op in result if op["op"] == "sketch_add_constraint")
    assert constraint["sketch"] == "$2"
    assert constraint["constraint"]["refs"][-1] == "$3#1.p"


def test_add_features_applies_auto_definition_once_and_reports_counts(monkeypatch):
    from services.mcp import cad_tools, preflight_tools

    constraints = [{"id": "c1", "type": "coincident", "refs": ["e1.center", "origin"]},
                   {"id": "c2", "type": "diameter", "refs": ["e1"], "value": {"value": 8}}]
    feature = {"id": "s1", **circle_feature(constraints=constraints)["feature"]}
    applied = []
    def call(method, params):
        if method == "doc_apply_ops":
            applied.append(copy.deepcopy(params["ops"]))
            return {"revision": 1, "id_map": {"$0": "s1", "$1": "c1", "$1#1": "c2"},
                    "tree": {"features": [feature]}, "rebuild": {"feature_status": [
                        {"id": "s1", "ok": True}]}}
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        if method == "doc_sketch_overview":
            return {"entities": feature["params"]["entities"]}
        if method == "sketch_health":
            return {"ok": True, "dof": 0, "status": "full", "closure": {"open_endpoints": []}}
        return {}
    monkeypatch.setattr(D.cb, "call", call)
    monkeypatch.setattr(D, "_doc", lambda d: d)
    monkeypatch.setattr(cad_tools, "_doc", lambda d: d)
    monkeypatch.setattr(D, "_face_hist", lambda d: {})
    monkeypatch.setattr(D, "_project_save", lambda *a: None)
    monkeypatch.setattr(D, "_solve_sync_or_handoff", lambda name, fn: fn())
    monkeypatch.setattr(preflight_tools, "run_auto_preflight", lambda *a: {})
    tools = Tools()
    D.register_cad_doc_tools(tools)
    result = json.loads(tools.tools["add_features"](ops=[circle_feature()], doc_id="d"))
    assert result["ok"], result
    assert len(applied) == 1
    assert applied[0][-1] == {"op": "sketch_fully_define", "sketch": "$0"}
    assert result["revision"] == 1 and result["id_map"] == {"$0": "s1"}
    definition = result["sketch_readbacks"][0]["definition"][0]
    assert definition["relations_added"] == definition["dimensions_added"] == 1
    assert definition["dof"] == 0 and result["sketch_readbacks"][0]["pinned_only"] is False


def test_fix_pinned_authored_construction_is_advisory_not_ignored():
    result = A.sketch_design_intent(
        [{"id": "e1", "type": "line", "construction": True}],
        [{"type": "fix", "refs": ["e1"]}], health={"dof": 0})
    assert result["pinned_only"] is False
    assert result["pinned_entities"] == ["e1"]
    assert [row["code"] for row in result["findings"]] == ["pinned_instead_of_constrained"]
