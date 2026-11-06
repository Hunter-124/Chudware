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
    assert result["authored_dimensions"] == result["auto_dimensions"] == 0
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
    assert result["anchored"] and result["authored_dimensions"] == 1
    assert result["auto_dimensions"] == 0


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


def test_repeated_soft_driving_dimensions_are_editability_advice():
    entities = [{"id": "e1", "type": "circle", "center": [-10, 0], "radius": 4},
                {"id": "e2", "type": "circle", "center": [10, 0], "radius": 4}]
    constraints = [
        {"type": "radius", "refs": ["e1"], "value": {"value": 4}, "auto": True},
        {"type": "radius", "refs": ["e2"], "value": {"value": 4}, "auto": True}]
    result = A.sketch_design_intent(entities, constraints, health={"dof": 0})
    finding = next(row for row in result["findings"] if row["code"] == "repeated_dimension_values")
    assert finding["severity"] == "warning"
    assert finding["entities"] == ["e1", "e2"]
    assert "Advisory" in finding["message"]
    assert result["authored_dimensions"] == 0 and result["auto_dimensions"] == 2
    assert result["pinned_only"] is False
    constraints.append({"type": "equal", "refs": ["e1", "e2"]})
    assert "repeated_dimension_values" not in {
        row["code"] for row in A.sketch_design_intent(entities, constraints)["findings"]}


def test_equal_relation_chains_and_reference_measurements_do_not_repeat_intent():
    entities = [{"id": f"e{i}", "type": "circle"} for i in range(1, 4)]
    constraints = [
        {"type": "radius", "refs": ["e1"], "value": 4},
        {"type": "radius", "refs": ["e3"], "value": 4},
        {"type": "equal", "refs": ["e1", "e2"]},
        {"type": "equal", "refs": ["e2", "e3"]},
        {"type": "radius", "refs": ["e2"], "value": 4, "driven": True}]
    assert "repeated_dimension_values" not in {
        row["code"] for row in A.sketch_design_intent(entities, constraints)["findings"]}
    constraints[1]["value"] = 5
    constraints[2:4] = []
    assert "repeated_dimension_values" not in {
        row["code"] for row in A.sketch_design_intent(entities, constraints)["findings"]}


@pytest.mark.parametrize("dimension_type", ["distance", "signed_distance"])
@pytest.mark.parametrize("axis_kind,axis,centers", [
    ("h", "axis.v", [[-10, 3], [10, 3]]),
    ("v", "axis.h", [[3, -10], [3, 10]])])
def test_mirrored_origin_offsets_suggest_symmetry(axis_kind, axis, centers, dimension_type):
    # Horizontal/vertical offsets use axis "h"|"v" on the actual engine type.
    entities = [{"id": f"e{i+1}", "type": "circle", "center": list(p), "radius": 4}
                for i, p in enumerate(centers)]
    constraints = [{"type": dimension_type, "axis": axis_kind,
                    "refs": [f"e{i+1}.center", "origin"],
                    "value": {"value": value}, "auto": True}
                   for i, value in enumerate([-10, 10])]
    findings = A.sketch_design_intent(entities, constraints)["findings"]
    finding = next(row for row in findings if row["code"] == "mirrored_origin_offsets")
    assert axis in finding["message"] and "spacing" in finding["message"]
    entities[1]["center"][1 if axis == "axis.v" else 0] += 2
    assert "mirrored_origin_offsets" not in {
        row["code"] for row in A.sketch_design_intent(entities, constraints)["findings"]}


def test_mirrored_offsets_need_the_same_axis_and_ignore_axisless_distances():
    entities = [{"id": "e1", "type": "circle", "center": [-10, 3], "radius": 4},
                {"id": "e2", "type": "circle", "center": [10, 3], "radius": 4}]
    codes = lambda constraints: {  # noqa: E731
        row["code"] for row in A.sketch_design_intent(entities, constraints)["findings"]}
    mixed = [{"type": "distance", "axis": axis, "refs": [f"e{i+1}.center", "origin"],
              "value": {"value": value}} for i, (axis, value) in enumerate([("h", -10), ("v", 10)])]
    assert "mirrored_origin_offsets" not in codes(mixed)
    axisless = [{"type": "distance", "refs": [f"e{i+1}.center", "origin"],
                 "value": {"value": value}} for i, value in enumerate([-10, 10])]
    assert "mirrored_origin_offsets" not in codes(axisless)


@pytest.mark.parametrize("axis_kind", [None, "h", "v"])
def test_zero_location_dimensions_suggest_relations_except_reference_dims(axis_kind):
    constraints = [{"id": "c1", "type": "distance", "refs": ["e1.center", "origin"],
                    "value": {"value": 0}, "auto": True,
                    **({"axis": axis_kind} if axis_kind else {})}]
    assert "zero_location_dimension" in {row["code"] for row in intent(constraints)["findings"]}
    constraints[0]["driven"] = True
    assert "zero_location_dimension" not in {
        row["code"] for row in intent(constraints)["findings"]}
    constraints[0]["driven"] = False
    assert "zero_location_dimension" not in {row["code"] for row in
        intent(constraints, health={"dof": 0, "skipped_constraints": [{"id": "c1"}]})["findings"]}


def _rectangle(*, auto, authored=0, relations=8):
    """A 4-line profile: `relations` relations plus rectangle width/height dimensions."""
    entities = [{"id": f"e{i}", "type": "line", "p1": [0, 0], "p2": [1, 1]} for i in range(1, 5)]
    constraints = [{"id": f"r{i}", "type": "horizontal", "refs": [f"e{1 + i % 4}"]}
                   for i in range(relations)]
    constraints += [{"id": f"a{i}", "type": "length", "refs": [f"e{i + 1}"],
                     "value": {"value": 40.0 + i}, "auto": True} for i in range(auto)]
    constraints += [{"id": f"u{i}", "type": "length", "refs": [f"e{i + 1}"],
                     "value": {"value": 10.0 + i}} for i in range(authored)]
    return entities, constraints


def test_all_auto_dimensions_on_a_feature_profile_say_so_instead_of_no_dimensions():
    entities, constraints = _rectangle(auto=2)
    result = A.sketch_design_intent(entities, constraints, health={"dof": 0, "status": "full"},
                                    feeds_feature=True)
    codes = {row["code"] for row in result["findings"]}
    assert result["authored_dimensions"] == 0 and result["auto_dimensions"] == 2
    assert "all_dimensions_auto" in codes and "no_driving_dimensions" not in codes
    row = next(r for r in result["findings"] if r["code"] == "all_dimensions_auto")
    assert "sketch_set_dim" in row["message"] and "critical dimensions" in row["message"]
    # One authored dimension anywhere ends the finding; no consumer means no finding.
    entities, constraints = _rectangle(auto=2, authored=1)
    assert "all_dimensions_auto" not in {r["code"] for r in A.sketch_design_intent(
        entities, constraints, health={"dof": 0}, feeds_feature=True)["findings"]}
    entities, constraints = _rectangle(auto=2)
    assert "all_dimensions_auto" not in {r["code"] for r in A.sketch_design_intent(
        entities, constraints, health={"dof": 0}, feeds_feature=False)["findings"]}


def test_no_dimensions_at_all_is_still_no_driving_dimensions():
    entities, constraints = _rectangle(auto=0)
    codes = {r["code"] for r in A.sketch_design_intent(
        entities, constraints, health={"dof": 0}, feeds_feature=True)["findings"]}
    assert "no_driving_dimensions" in codes and "all_dimensions_auto" not in codes


def test_dimension_heavy_flags_dimension_dominated_sketches_with_a_ratio():
    entities, constraints = _rectangle(auto=0, authored=4, relations=1)
    result = A.sketch_design_intent(entities, constraints, health={"dof": 0}, feeds_feature=True)
    row = next(r for r in result["findings"] if r["code"] == "dimension_heavy")
    assert result["relation_count"] == 1 and result["relation_dimension_ratio"] == 0.25
    assert "relation:dimension 0.25" in row["message"]
    # Relation-dominated sketches are not dimension-heavy.
    entities, constraints = _rectangle(auto=0, authored=2, relations=8)
    result = A.sketch_design_intent(entities, constraints, health={"dof": 0}, feeds_feature=True)
    assert "dimension_heavy" not in {r["code"] for r in result["findings"]}
    assert result["relation_dimension_ratio"] == 4.0


def test_dimension_heavy_flags_repeated_and_zero_dimensions_even_with_many_relations():
    entities = [{"id": f"e{i}", "type": "circle", "center": [i, 0], "radius": 4}
                for i in range(1, 4)]
    constraints = [{"id": f"c{i}", "type": "radius", "refs": [f"e{i}"], "value": {"value": 4}}
                   for i in range(1, 4)]
    constraints += [{"id": f"h{i}", "type": "horizontal", "refs": [f"e{i}.center", "origin"]}
                    for i in range(1, 4)] * 2
    codes = {r["code"] for r in A.sketch_design_intent(
        entities, constraints, health={"dof": 0}, feeds_feature=True)["findings"]}
    assert {"repeated_dimension_values", "dimension_heavy"} <= codes


def test_editability_warnings_surface_in_existing_readback_without_changing_completion():
    sketch = {"entities": [
        {"id": "e1", "type": "circle", "center": [-10, 0], "radius": 4},
        {"id": "e2", "type": "circle", "center": [10, 0], "radius": 4}],
        "constraints": [
            {"type": "radius", "refs": ["e1"], "value": 4, "auto": True},
            {"type": "radius", "refs": ["e2"], "value": 4, "auto": True}]}
    readback = A.sketch_mutation_readback(sketch, health={"status": "full", "dof": 0})
    assert "repeated_dimension_values" in {row["code"] for row in readback["findings"]}
    assert "Advisory: repeated" in readback["summary"]
    assert readback["dof"] == 0 and not readback["pinned_only"]


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


def test_live_readback_reports_a_failure_instead_of_returning_none(monkeypatch):
    def call(method, params):
        raise RuntimeError("overview exploded")
    monkeypatch.setattr(D.cb, "call", call)
    result = A.live_sketch_readback("d", "s1")
    assert result["ok"] is False and result["sketch_id"] == "s1"
    assert "overview exploded" in result["error"]


def test_live_readback_still_propagates_engine_connection_loss(monkeypatch):
    class Lost(Exception):
        code = "engine_connection_lost"

    def call(method, params):
        raise Lost("gone")
    monkeypatch.setattr(D.cb, "call", call)
    with pytest.raises(Lost):
        A.live_sketch_readback("d", "s1")


def test_a_failing_sketch_health_is_carried_into_the_readback_not_dropped(monkeypatch):
    feature = {"id": "s1", **circle_feature()["feature"]}
    def call(method, params):
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        if method == "doc_sketch_overview":
            return {"entities": feature["params"]["entities"]}
        raise RuntimeError("health solver blew up")
    monkeypatch.setattr(D.cb, "call", call)
    data, health = A._load_sketch("s1", "d")
    assert health is None and "health solver blew up" in data["health_error"]
    result = A.live_sketch_readback("d", "s1")
    assert result["ok"] and "health solver blew up" in result["health_error"]
    assert "health_unavailable" in {row["code"] for row in result["findings"]}


def test_rejected_solve_is_loud_and_overview_measured_values_reach_the_summary(monkeypatch):
    monkeypatch.setattr("services.mcp.cad_tools._doc", lambda doc_id: doc_id)
    feature = {"id": "s1", **circle_feature(constraints=[
        {"id": "c1", "type": "radius", "refs": ["e1"], "value": {"value": 4}, "auto": True},
        {"id": "c2", "type": "diameter", "refs": ["e1"], "driven": True}])["feature"]}
    def call(method, params):
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        if method == "doc_sketch_overview":
            return {"entities": feature["params"]["entities"], "solve_rejected": True,
                    "constraints": [{"id": "c1", "type": "radius", "measured": 4.0},
                                    {"id": "c2", "type": "diameter", "measured": 8.0}]}
        return {"ok": True, "dof": 0, "status": "full", "converged": True,
                "closure": {"open_endpoints": []}}
    monkeypatch.setattr(D.cb, "call", call)
    result = A.live_sketch_readback("d", "s1")
    assert result["solve_rejected"] is True
    assert result["findings"][0]["code"] == "solve_rejected"
    assert result["summary"].startswith("SOLVE REJECTED")
    assert "authored_seed=1" in result["ascii_ruled"]
    assert "c2:diameter" in result["summary"] and "measured=8" in result["summary"]
    assert "c1:radius" in result["summary"] and "[auto]" in result["summary"]
    assert "measured=unavailable" not in result["summary"]


@pytest.mark.parametrize("usage,informational", [
    ("open_path", True), ("unknown", True), ("closed_profile", False), ("mixed", False)])
def test_open_endpoints_are_a_defect_only_for_closed_profile_consumers(usage, informational):
    health = {"dof": 0, "status": "full", "converged": True,
              "closure": {"open_endpoints": ["e1.end", "e2.start"], "closed_chains_ok": False,
                          "usage": usage}}
    entities = [{"id": "e1", "type": "line", "p1": [0, 0], "p2": [5, 0]},
                {"id": "e2", "type": "line", "p1": [9, 0], "p2": [9, 5]}]
    findings = A.check_sketch_geometry(entities, health=health, constraints=[])
    opened = next(row for row in findings if row["code"] == "open_profile")
    assert opened["severity"] == ("info" if informational else "error")
    summary = A.summarize_sketch(entities, [], findings, health=health)
    assert ("Profile: OPEN" in summary) is (not informational)
    assert ("informational" in summary) is informational
    text = G.sketch_health_text(health)
    assert ("Closure: OPEN" in text) is (not informational)
    assert ("(informational)" in text) is informational


@pytest.mark.parametrize("status,dof,converged,constraints_kind,expected", [
    ("full", 0, True, "pinned", False),                # blanket pins only
    ("full", 0, True, "pinned_plus_dimension", False),  # one dimension beside pins: still pinned
    ("full", 0, True, "parametric", True),
    ("full", 0, False, "parametric", False),            # solver did not converge
    ("full", 0, None, "parametric", False),             # convergence not reported: not claimable
    ("full", 3, True, "parametric", False),
    ("over", 0, True, "parametric", False),
    ("unknown", None, True, "parametric", False)])
def test_check_sketch_truthful_fully_defined(monkeypatch, status, dof, converged,
                                              constraints_kind, expected):
    pins = [{"id": "c1", "type": "fix", "refs": ["e1"]}]
    dimension = {"id": "c2", "type": "radius", "refs": ["e1"], "value": {"value": 4}}
    constraints = {"pinned": pins, "pinned_plus_dimension": pins + [dimension],
                   "parametric": [dimension,
                                  {"id": "c3", "type": "coincident",
                                   "refs": ["e1.center", "origin"]}]}[constraints_kind]
    feature = {"id": "s1", **circle_feature(constraints=constraints)["feature"]}
    def call(method, params):
        if method == "sketch_health":
            assert params == {"doc_id": "d", "sketch": "s1"}
            health = {"ok": True, "status": status, "dof": dof, "converged": converged}
            return {k: v for k, v in health.items() if v is not None or k == "dof"}
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
    assert result["pinned_only"] is (constraints_kind == "pinned")
    assert result["pinned_entities"] == (["e1"] if constraints_kind.startswith("pinned") else [])
    assert result["converged"] is converged
    if expected:
        assert "nothing fix-pinned" in result["summary"]
    if constraints_kind == "pinned_plus_dimension" and status == "full":
        assert "fix-pinned" in result["summary"]
        assert "constrained parametrically, not fix-pinned" not in result["summary"]
    if converged is not True and status == "full" and dof == 0:
        assert result["summary"].startswith("Not converged")


def test_check_sketch_names_the_skip_reason_and_reports_hints_and_closure_usage(monkeypatch):
    feature = {"id": "s1", **circle_feature()["feature"]}
    health = {"ok": True, "status": "full", "dof": 0, "converged": False,
              "skipped_constraints": [{"id": "c9", "reason": "expr failed: width"}],
              "hints": [{"order": 1, "kind": "relation", "action": "coincident", "why": "x"}],
              "closure": {"open_endpoints": ["e1.end"], "usage": "open_path"}}
    def call(method, params):
        return health if method == "sketch_health" else {"tree": {"features": [feature]}}
    monkeypatch.setattr(E.cb, "call", call)
    monkeypatch.setattr(E, "_doc", lambda d: d)
    tools = Tools()
    E.register_cad_eval_tools(tools)
    result = json.loads(tools.tools["check_sketch"](sketch="s1", doc_id="d"))
    assert result["fully_defined"] is False and result["converged"] is False
    assert "c9 (expr failed: width)" in result["summary"]
    assert "under-defined" not in result["summary"].lower()
    assert result["hints"] == health["hints"] and "schedule" not in result
    assert result["closure_usage"] == "open_path" and result["open_endpoints"] == ["e1.end"]


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


def test_fully_define_tool_surfaces_engine_report_and_round_false(monkeypatch):
    feature = {"id": "s1", **circle_feature()["feature"]}
    calls = []
    def call(method, params):
        calls.append((method, params))
        if method == "doc_apply_ops":
            return {"revision": 8, "id_map": {}, "tree": {"features": [feature]},
                    "op_reports": [{"index": 0, "op": "sketch_fully_define", "sketch": "s1",
                                    "relations_added": 0, "dimensions_added": 0,
                                    "dof_before": 3, "dof_after": 3,
                                    "notes": ["left e1 radius free: no measurable value"],
                                    "rounded": {"count": 2, "max_delta": 0.02}}]}
        if method == "sketch_health":
            return {"ok": True, "status": "under", "dof": 3}
        return {"tree": {"features": [feature]}}
    monkeypatch.setattr(G.cb, "call", call)
    monkeypatch.setattr(G, "_doc", lambda d: d)
    tools = Tools()
    G.register_gsl_tools(tools)
    result = json.loads(tools.tools["sketch_fully_define"](
        sketch="s1", round=False, doc_id="d"))
    assert calls[0][1]["ops"][0]["round"] is False
    assert result["notes"] == ["left e1 radius free: no measurable value"]
    assert (result["dof_before"], result["dof_after"]) == (3, 3)
    assert result["rounded"] == {"count": 2, "max_delta": 0.02}


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


def _add_features_tool(monkeypatch, call):
    from services.mcp import cad_tools, preflight_tools

    monkeypatch.setattr(D.cb, "call", call)
    monkeypatch.setattr(D, "_doc", lambda d: d)
    monkeypatch.setattr(cad_tools, "_doc", lambda d: d)
    monkeypatch.setattr(D, "_face_hist", lambda d: {})
    monkeypatch.setattr(D, "persist_result", lambda *a: None)
    monkeypatch.setattr(D, "_solve_sync_or_handoff", lambda name, fn, **kwargs: fn())
    monkeypatch.setattr(preflight_tools, "run_auto_preflight", lambda *a: {})
    tools = Tools()
    D.register_cad_doc_tools(tools)
    return tools.tools["add_features"]


def _engine(features, *, id_map, applied=None, **reply):
    """A fake core_bridge.call: apply replies `id_map`/`reply`, health is clean and full."""
    def call(method, params):
        if method == "doc_apply_ops":
            if applied is not None:
                applied.append(copy.deepcopy(params["ops"]))
            return {"revision": 1, "id_map": id_map, "tree": {"features": features},
                    "rebuild": {"feature_status": [{"id": f["id"], "ok": True}
                                                   for f in features]}, **reply}
        if method == "doc_tree":
            return {"tree": {"features": features}}
        if method == "doc_sketch_overview":
            return {"entities": features[0]["params"]["entities"]}
        if method == "sketch_health":
            return {"ok": True, "dof": 0, "status": "full", "converged": True,
                    "closure": {"open_endpoints": []}}
        return {}
    return call


def test_add_features_applies_auto_definition_once_and_reports_counts(monkeypatch):
    constraints = [{"id": "c1", "type": "coincident", "refs": ["e1.center", "origin"]},
                   {"id": "c2", "type": "diameter", "refs": ["e1"], "value": {"value": 8}}]
    feature = {"id": "s1", **circle_feature(constraints=constraints)["feature"]}
    applied = []
    add = _add_features_tool(monkeypatch, _engine(
        [feature], id_map={"$0": "s1", "$1": "c1", "$1#1": "c2"}, applied=applied))
    result = json.loads(add(ops=[circle_feature()], doc_id="d"))
    assert result["ok"], result
    assert len(applied) == 1
    assert applied[0][-1] == {"op": "sketch_fully_define", "sketch": "$0"}
    assert result["revision"] == 1 and result["id_map"] == {"$0": "s1"}
    definition = result["sketch_readbacks"][0]["definition"][0]
    assert definition["relations_added"] == definition["dimensions_added"] == 1
    assert definition["dof"] == 0 and result["sketch_readbacks"][0]["pinned_only"] is False
    assert definition["auto_appended"] is True
    assert "sketch_readbacks_omitted" not in result and "sketch_readbacks_error" not in result
    note = " ".join(result.get("assumptions") or [])
    assert "rounded to clean increments" in note and "auto:true" in note
    assert "round:false" in note and "auto_define:false" in note


def test_add_features_surfaces_engine_op_reports_on_definitions_and_other_sketch_ops(monkeypatch):
    feature = {"id": "s1", **circle_feature(constraints=[
        {"id": "c1", "type": "coincident", "refs": ["e1.center", "origin"]},
        {"id": "c2", "type": "diameter", "refs": ["e1"], "value": {"value": 8}}])["feature"]}
    # Authored: [sketch, sketch, delete]. Engine batch: [sketch, define, sketch, delete, define],
    # so the delete report (engine index 3) must come back as the caller's op 2.
    id_map = {"$0": "s1", "$1": "c1", "$1#1": "c2", "$2": "s1", "$4": "c1"}
    add = _add_features_tool(monkeypatch, _engine([feature], id_map=id_map, op_reports=[
        {"index": 1, "op": "sketch_fully_define", "sketch": "$0", "relations_added": 1,
         "dimensions_added": 1, "dof_before": 3, "dof_after": 0,
         "notes": ["kept e1 radius free"], "rounded": {"count": 1, "max_delta": 0.02}},
        {"index": 3, "op": "sketch_delete_constraint", "sketch": "$0", "id": "c9",
         "dropped_constraints": ["c9"]}]))
    result = json.loads(add(ops=[
        circle_feature(), circle_feature(),
        {"op": "sketch_delete_constraint", "sketch": "$0", "id": "c9"}], doc_id="d"))
    assert result["ok"], result
    first = result["sketch_definitions"][0]
    assert first["notes"] == ["kept e1 radius free"] and first["rounded"]["count"] == 1
    assert (first["dof_before"], first["dof_after"], first["dof"]) == (3, 0, 0)
    assert result["sketch_op_reports"] == [{
        "index": 2, "op": "sketch_delete_constraint", "sketch": "$0", "id": "c9",
        "dropped_constraints": ["c9"]}]


def test_add_features_names_sketches_that_got_no_readback(monkeypatch):
    features = [{"id": f"s{i}", **circle_feature()["feature"]} for i in range(1, 6)]
    id_map = {f"${2 * i}": f"s{i + 1}" for i in range(5)}
    add = _add_features_tool(monkeypatch, _engine(features, id_map=id_map))
    result = json.loads(add(ops=[circle_feature() for _ in range(5)], doc_id="d"))
    assert result["ok"], result
    assert len(result["sketch_readbacks"]) == 4
    assert result["sketch_readbacks_omitted"] == 1
    assert result["sketch_readbacks_omitted_ids"] == ["s5"]
    assert "s5" in result["readback_hint"]


def test_add_features_reports_failed_readbacks_as_entries(monkeypatch):
    feature = {"id": "s1", **circle_feature()["feature"]}
    add = _add_features_tool(monkeypatch, _engine([feature], id_map={"$0": "s1"}))
    monkeypatch.setattr(A, "live_sketch_readback", lambda doc, sid, **kw: {
        "ok": False, "kind": "sketch_readback", "sketch_id": sid, "error": "overview exploded"})
    result = json.loads(add(ops=[circle_feature()], doc_id="d"))
    assert result["ok"], result
    assert result["sketch_readbacks"] == [{
        "ok": False, "kind": "sketch_readback", "sketch_id": "s1", "error": "overview exploded"}]
    assert "READBACK FAILED" in result["readback_hint"] and "s1" in result["readback_hint"]
    assert result["sketch_definitions"][0]["dof"] == 0  # health fetched directly


def test_add_features_reports_a_readback_block_failure_instead_of_swallowing(monkeypatch):
    feature = {"id": "s1", **circle_feature()["feature"]}
    add = _add_features_tool(monkeypatch, _engine([feature], id_map={"$0": "s1"}))
    def boom(*args, **kwargs):
        raise ValueError("kaput")
    monkeypatch.setattr(A, "sketch_define_additions", boom)
    result = json.loads(add(ops=[circle_feature()], doc_id="d"))
    assert result["ok"], result
    assert "kaput" in result["sketch_readbacks_error"]


def test_fix_pinned_authored_construction_is_advisory_not_ignored():
    result = A.sketch_design_intent(
        [{"id": "e1", "type": "line", "construction": True}],
        [{"type": "fix", "refs": ["e1"]}], health={"dof": 0})
    assert result["pinned_only"] is False
    assert result["pinned_entities"] == ["e1"]
    assert [row["code"] for row in result["findings"]] == ["pinned_instead_of_constrained"]


def test_readback_reports_rpc_error_envelopes(monkeypatch):
    monkeypatch.setattr("services.mcp.cad_tools._doc", lambda doc_id: doc_id)
    feature = {"id": "s1", **circle_feature()["feature"]}
    def call(method, params):
        if method == "doc_sketch_overview":
            return {"ok": False, "error": "overview unavailable"}
        return {"tree": {"features": [feature]}}
    monkeypatch.setattr(D.cb, "call", call)
    assert "overview unavailable" in A.live_sketch_readback("d", "s1")["error"]

    def health_failure(method, params):
        if method == "doc_sketch_overview":
            return {"entities": feature["params"]["entities"]}
        if method == "sketch_health":
            return {"ok": False, "error": "health unavailable"}
        return {"tree": {"features": [feature]}}
    monkeypatch.setattr(D.cb, "call", health_failure)
    readback = A.live_sketch_readback("d", "s1")
    assert readback["ok"] and readback["health_error"] == "health unavailable"


def test_sketch_probe_validates_actions_and_reports_engine_result(monkeypatch):
    calls = []
    def call(method, params):
        calls.append((method, params))
        return {"ok": True, "status": "over", "dof_before": 1, "dof_after": 0,
                "dof_delta": -1, "converged": True, "conflicting": ["__probe__", "c1"],
                "redundant": [], "skipped": [], "conflicts_with": {"__probe__": ["c1"]},
                "would_over_define": True, "probe_id": "__probe__"}
    monkeypatch.setattr(G.cb, "call", call)
    monkeypatch.setattr(G, "_doc", lambda d: d)
    tools = Tools()
    G.register_gsl_tools(tools)
    probe = tools.tools["sketch_probe"]
    for kwargs in ({}, {"set_dim": {"id": "c1", "value": 3}, "delete_constraint": "c2"},
                   {"delete_constraint": {"id": "c1", "ignored": 1}}):
        assert json.loads(probe(sketch="s1", doc_id="d", **kwargs))["ok"] is False
    assert not calls
    constraint = {"type": "parallel", "refs": ["e1", "e2"]}
    result = json.loads(probe(sketch="s1", doc_id="d", add_constraint=constraint))
    assert calls == [("sketch_probe", {"doc_id": "d", "sketch": "s1",
                                      "action": {"add_constraint": constraint}})]
    assert result["would_over_define"] and result["dof_delta"] == -1
    assert result["conflicts_with"] == {"__probe__": ["c1"]}
    assert result["conflicting"] == ["__probe__", "c1"] and result["skipped"] == []
    assert "c1" in result["verdict"]
    json.loads(probe(sketch="s1", doc_id="d", set_dim={"id": "c1", "value": 12}))
    assert calls[-1][1]["action"] == {"set_dim": {"id": "c1", "value": 12}}
    json.loads(probe(sketch="s1", doc_id="d", delete_constraint={"id": "c2"}))
    assert calls[-1][1]["action"] == {"delete_constraint": {"id": "c2"}}


def test_sketch_health_text_names_conflict_values_and_unordered_hints():
    text = G.sketch_health_text({
        "dof": 0, "status": "over", "converged": False,
        "conflicting": [{"id": "c3", "type": "distance",
                         "conflict_set": [{"id": "c3", "type": "distance", "value": 40},
                                          {"id": "c1", "type": "distance", "value": 30},
                                          {"id": "c2", "type": "horizontal"}]}],
        "redundant": [{"id": "c4", "type": "horizontal", "redundant_with": ["c2"]}],
        "hints": [{"order": 1, "kind": "relation", "action": "equal", "why": "same size"}]})
    assert "CONFLICT c3 (distance 40) conflicts with c1 (distance 30), c2 (horizontal)" in text
    assert "REDUNDANT c4 (horizontal) is implied by c2" in text
    assert "Hints (unordered suggestions, not a plan)" in text


def test_describe_sketch_uses_measured_values_and_flags_rejected_seeds():
    feature = {"id": "s1", **circle_feature(constraints=[
        {"id": "c1", "type": "radius", "refs": ["e1"], "auto": True,
         "value": {"value": 4, "expr": "width/2"}},
        {"id": "c2", "type": "diameter", "refs": ["e1"], "driven": True}])["feature"]}
    digest = G._sketch_digest("s1", {"entities": feature["params"]["entities"],
        "constraints": [{"id": "c1", "measured": 4.5}, {"id": "c2", "measured": 9}],
        "solve_rejected": True, "status": "over", "dof": 0}, {}, feature)
    text = G._sketch_text(digest)
    assert "SOLVE REJECTED" in text and "AUTHORED seed" in text
    assert "width/2 = 4.5" in text and "measured 9" in text and "[auto]" in text
    assert not digest["fully_defined"]


@pytest.mark.parametrize("tool_name", ["check_sketch", "sketch_health"])
@pytest.mark.parametrize("before,after,expected", [(7, 7, 7), (7, 8, None), (None, None, None)])
def test_sketch_check_brackets_the_solver_and_intent_with_revisions(
        monkeypatch, tool_name, before, after, expected):
    feature = {"id": "s1", **circle_feature()["feature"]}
    calls = []
    def call(method, params):
        calls.append(method)
        if method == "doc_tree":
            revision = before if calls.count("doc_tree") == 1 else after
            return {"revision": revision, "tree": {"features": [feature]}}
        assert method == "sketch_health"
        return {"ok": True, "dof": 0, "status": "full", "converged": True}
    monkeypatch.setattr(E.cb, "call", call)
    monkeypatch.setattr(E, "_doc", lambda d: d)
    monkeypatch.setattr(G, "_doc", lambda d: d)
    tools = Tools()
    E.register_cad_eval_tools(tools)
    G.register_gsl_tools(tools)
    result = json.loads(tools.tools[tool_name](sketch="s1", doc_id="d"))
    assert result["ok"] and calls == ["doc_tree", "sketch_health", "doc_tree"]
    if expected is None:
        assert result["revision_stale"] is True and "revision" not in result
    else:
        assert result["revision"] == expected and not result.get("revision_stale", False)


def test_rotate_point_is_a_signed_angular_dimension_in_normalization_and_reports():
    refs = ["e1.center", "e2.center", "origin"]
    normalized = D._normalize_constraint_op({
        "op": "sketch_add_constraint", "sketch": "s1",
        "constraint": {"id": "c1", "type": "rotate_point", "refs": refs,
                       "value": "-45 degrees"}}, [])["constraint"]
    assert normalized["refs"] == refs
    assert normalized["value"] == {"value": -45, "expr": None}
    assert normalized["driven"] is False and D._CONSTRAINT_ARITY["rotate_point"] == 3
    feature = {"id": "s1", **circle_feature(constraints=[normalized])["feature"]}
    feature["params"]["entities"].append({"id": "e2", "type": "circle",
                                           "center": [3, 3], "radius": 4})
    intent = A.sketch_design_intent(feature["params"]["entities"], [normalized],
                                    health={"dof": 0}, feeds_feature=True)
    assert intent["authored_dimensions"] == 1 and intent["relation_count"] == 0
    automatic = {**normalized, "auto": True}
    intent_auto = A.sketch_design_intent(feature["params"]["entities"], [automatic],
                                         health={"dof": 0}, feeds_feature=True)
    assert intent_auto["auto_dimensions"] == 1 and intent_auto["authored_dimensions"] == 0
    reference = {**normalized, "driven": True}
    intent_reference = A.sketch_design_intent(feature["params"]["entities"], [reference],
                                              health={"dof": 0}, feeds_feature=True)
    assert intent_reference["authored_dimensions"] == intent_reference["auto_dimensions"] == 0
    assert A.sketch_define_additions({"$0": "c1"}, 0, [normalized])["dimensions_added"] == 1
    digest = G._sketch_digest("s1", {"entities": feature["params"]["entities"],
        "constraints": [{"id": "c1", "measured": -45}], "status": "full", "dof": 0},
        {}, feature)
    assert digest["dimensions"][0]["type"] == "rotate_point"
    assert "c1 rotate_point=-45" in G._sketch_text(digest)


def test_linked_rotation_followers_are_relations_not_independent_dimensions():
    entities = [{"id": f"e{i}", "type": "circle", "center": [i, 0], "radius": 1}
                for i in range(1, 4)]
    master = {"id": "c1", "type": "rotate_point",
              "refs": ["e1.center", "e2.center", "origin"],
              "value": {"value": 120, "expr": None}}
    follower = {"id": "c2", "type": "rotate_point",
                "refs": ["e2.center", "e3.center", "origin"],
                "value": {"value": 120, "expr": None}, "angle_ref": "c1"}
    constraints = [master, follower]
    intent = A.sketch_design_intent(entities, constraints, health={"dof": 0},
                                    feeds_feature=True)
    assert intent["authored_dimensions"] == 1 and intent["auto_dimensions"] == 0
    assert intent["relation_count"] == 1 and intent["relation_dimension_ratio"] == 1.0
    assert "repeated_dimension_values" not in {f["code"] for f in intent["findings"]}
    added = A.sketch_define_additions({"$0": "c1", "$0#1": "c2"}, 0, constraints)
    assert added["dimensions_added"] == 1 and added["relations_added"] == 1
    summary = A.summarize_sketch(entities, constraints, [], health={"dof": 0})
    assert "1 authored, 0 auto driving" in summary and "angle_ref=c1" in summary
    feature = {"id": "s1", "params": {"entities": entities, "constraints": constraints}}
    digest = G._sketch_digest("s1", {"entities": entities, "status": "full", "dof": 0},
                              {}, feature)
    assert len(digest["dimensions"]) == 1
    assert digest["linked_rotations"] == [{"id": "c2", "angle_ref": "c1",
                                           "refs": follower["refs"]}]
    assert digest["relation_counts"]["rotate_point (linked)"] == 1
    assert "c2 rotate_point linked to angle_ref=c1" in G._sketch_text(digest)


def test_signed_distance_and_equal_offset_keep_axis_and_design_intent():
    refs = ["e1.center", "e2.center"]
    dimension = D._normalize_constraint_op({
        "op": "sketch_add_constraint", "sketch": "s1", "constraint": {
            "id": "c1", "type": "signed_distance", "refs": refs,
            "axis": "x", "value": {"value": -5, "expr": "travel"}}}, [])["constraint"]
    relation = D._normalize_constraint_op({
        "op": "sketch_add_constraint", "sketch": "s1", "constraint": {
            "id": "c2", "type": "equal_offset", "axis": "y",
            "refs": ["e1.center", "e2.center", "e3.center", "e4.center"]}}, [])["constraint"]
    assert dimension["axis"] == "h"
    assert dimension["value"] == {"value": -5, "expr": "travel"}
    assert relation["axis"] == "v" and "value" not in relation
    assert D._CONSTRAINT_ARITY["signed_distance"] == 2 and D._CONSTRAINT_ARITY["equal_offset"] == 4
    assert D._constraint_dialect_keys({**relation, "axis": "y"})
    entities = [{"id": f"e{i}", "type": "circle", "center": [i, 0], "radius": 1}
                for i in range(1, 5)]
    constraints = [dimension, relation]
    intent = A.sketch_design_intent(entities, constraints, health={"dof": 0},
                                    feeds_feature=True)
    assert intent["authored_dimensions"] == 1 and intent["relation_count"] == 1
    summary = A.summarize_sketch(entities, constraints, [], health={"dof": 0})
    assert "c1:signed_distance" in summary and "axis=h" in summary
    assert "c2:equal_offset" in summary and "axis=v" in summary
    feature = {"id": "s1", "params": {"entities": entities, "constraints": constraints}}
    digest = G._sketch_digest("s1", {"entities": entities, "status": "full", "dof": 0,
        "constraints": [{"id": "c1", "measured": -5}]}, {}, feature)
    assert digest["dimensions"][0]["axis"] == "h"
    assert digest["relation_counts"]["equal_offset"] == 1
    assert "c1 signed_distance=travel = -5 [axis h]" in G._sketch_text(digest)
