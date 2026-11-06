"""Engine acceptance for agent-authored sketch intent (run after the integrated core build)."""
from __future__ import annotations

import copy
import os
import json
import uuid

import pytest

from services.mcp import core_bridge as cb

enginetest = pytest.mark.skipif(not cb.available(), reason="libchudware not built/staged")


@pytest.fixture
def sketch_doc(monkeypatch):
    from services.agent.agent_turn import _bind_tool
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")

    doc = cb.call("doc_new", {"name": "sketch_intent_" + uuid.uuid4().hex, "kind": "part"})["doc_id"]
    yield doc, _bind_tool("add_features"), _bind_tool("check_sketch")
    cb.call("doc_close", {"doc_id": doc})


def sketch_op(entity, *, auto_define=None):
    params = {"entities": [entity], "constraints": []}
    if auto_define is not None:
        params["auto_define"] = auto_define
    return {"op": "add_feature", "feature": {"type": "sketch", "name": "Profile",
            "refs": {"plane": "plane.top"}, "params": params}}


@enginetest
@pytest.mark.skipif(os.name != "posix", reason="bounded native preflight requires POSIX")
@pytest.mark.parametrize("auto_define", [True, False], ids=["default", "opt-out"])
def test_explicit_preflight_predicts_the_same_prepared_sketch_batch(
        sketch_doc, monkeypatch, auto_define):
    from services.agent.advisor import preflight
    from services.agent.agent_turn import _bind_tool

    doc, add, check = sketch_doc
    monkeypatch.setenv("CHUDWARE_PREFLIGHT", "always")
    monkeypatch.setattr(preflight, "_ledger_append", lambda event: None)
    seed = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [{
        "op": "add_feature", "feature": {"type": "box", "name": "Seed",
            "params": {"length": 5, "width": 5, "height": 5}}}]})
    assert all(s["ok"] for s in seed["rebuild"]["feature_status"]), seed
    before = cb.call("doc_tree", {"doc_id": doc})
    ops = [
        sketch_op({"type": "rectangle", "center": [0, 0], "width": 20, "height": 12}),
        {"op": "add_feature", "feature": {"type": "extrude", "name": "Body",
            "refs": {"profile": [{"kind": "region", "sketch": "Profile"}]},
            "params": {"depth": "3mm", "mode": "boss"}}},
    ]
    ops[0]["feature"]["refs"]["plane"] = "XY"
    original = copy.deepcopy(ops)
    policy = {} if auto_define else {"auto_define": False}
    explicit = json.loads(_bind_tool("preflight_ops")(doc_id=doc, ops=ops, **policy))
    assert explicit["verdict"] in ("pass", "warn"), explicit
    assert next(c for c in explicit["checks"] if c["check"] == "rebuild_predict")["result"] in ("pass", "warn")
    assert all(s["ok"] for s in explicit["predicted"]["feature_status"])
    assert ops == original
    assert cb.call("doc_tree", {"doc_id": doc}) == before
    unchanged = cb.call("doc_dry_run_ops", {"doc_id": doc, "ops": []})
    assert unchanged["ok"]
    status = unchanged["rebuild"]["doc_status"]
    assert status["features_reused"] == 1 and status["features_executed"] == 0

    # Capture the real automatic prediction, not a simulated advisor response.
    automatic = []
    real_preflight = preflight.run_preflight

    def capture(*args, **kwargs):
        result = real_preflight(*args, **kwargs)
        automatic.append(result)
        return result

    monkeypatch.setattr(preflight, "run_preflight", capture)
    applied = json.loads(add(doc_id=doc, ops=ops, **policy))
    assert applied["ok"], applied
    assert len(automatic) == 1
    assert automatic[0]["target"]["ops_hash"] == explicit["target"]["ops_hash"]
    assert automatic[0]["predicted"]["feature_status"] == explicit["predicted"]["feature_status"]
    sketch_id, extrude_id = applied["id_map"]["$0"], applied["id_map"]["$1"]
    tree = cb.call("doc_tree", {"doc_id": doc})["tree"]
    extrude = next(f for f in tree["features"] if f["id"] == extrude_id)
    assert extrude["refs"]["profile"][0]["sketch"] == sketch_id
    assert extrude["params"]["depth"] == 3
    health = json.loads(check(doc_id=doc, sketch=sketch_id))
    assert (health["dof"] == 0) is auto_define, health
    assert bool(applied.get("sketch_definitions")) is auto_define


@enginetest
def test_explicit_preflight_preserves_placeholder_validation(sketch_doc):
    from services.agent.agent_turn import _bind_tool

    doc, _, _ = sketch_doc
    before = cb.call("doc_tree", {"doc_id": doc})
    result = json.loads(_bind_tool("preflight_ops")(doc_id=doc, ops=[
        {"op": "add_feature", "feature": {"type": "extrude",
            "refs": {"profile": [{"kind": "region", "sketch": "$9"}]},
            "params": {"depth": 2, "mode": "boss"}}}]))
    assert "error" in result and "$9" in result["error"], result
    assert cb.call("doc_tree", {"doc_id": doc}) == before


@enginetest
@pytest.mark.parametrize("entity", [
    {"type": "rectangle", "center": [0, 0], "width": 20, "height": 12},
    {"type": "polyline", "closed": True, "points": [[0, 0], [20, 0], [20, 12], [0, 12]]},
    {"type": "circle", "center": [0, 0], "radius": 6},
], ids=["rectangle", "polyline", "circle"])
def test_agent_profiles_are_fully_defined_in_the_same_revision(sketch_doc, entity):
    doc, add, check = sketch_doc
    before = cb.call("doc_checkpoint", {"doc_id": doc})["revision"]
    result = json.loads(add(doc_id=doc, ops=[sketch_op(entity), {
        "op": "add_feature", "feature": {"type": "extrude", "name": "Body",
        "refs": {"profile": [{"kind": "region", "sketch": "$0"}]},
        "params": {"depth": 3, "mode": "boss"}}}]))
    assert result["ok"], result
    assert result["revision"] == before + 1, result
    assert result["volume_mm3"] > 0
    sid = result["id_map"]["$0"]
    assert result["id_map"]["$1"] != sid  # original caller op indices stay stable
    health = json.loads(check(doc_id=doc, sketch=sid))
    assert health["ok"] and health["dof"] == 0 and health["fully_defined"], health
    assert health["pinned_only"] is False and health["authored_dimensions"] == 0, health
    assert health["auto_dimensions"] > 0, health
    assert "all_dimensions_auto" in {finding["code"] for finding in health["findings"]}
    feature = next(f for f in cb.call("doc_tree", {"doc_id": doc})["tree"]["features"] if f["id"] == sid)
    types = {constraint["type"] for constraint in feature["params"]["constraints"]}
    assert "fix" not in types, feature
    if entity["type"] != "circle":
        assert {"coincident", "horizontal", "vertical"} <= types, types
    else:
        assert types & {"radius", "diameter"}, types
    definition = result["sketch_definitions"][0]
    assert definition["sketch"] == sid and definition["dimensions_added"] > 0, definition
    assert definition["dof"] == 0, definition
    assert result["sketch_readbacks"][0]["dof"] == 0


@enginetest
def test_call_opt_out_leaves_geometry_free_then_entity_batch_defines_it(sketch_doc):
    doc, add, check = sketch_doc
    created = json.loads(add(doc_id=doc, auto_define=False,
        ops=[sketch_op({"type": "circle", "center": [0, 0], "radius": 4})]))
    assert created["ok"], created
    sid = created["id_map"]["$0"]
    assert json.loads(check(doc_id=doc, sketch=sid))["dof"] > 0
    added = json.loads(add(doc_id=doc, ops=[
        {"op": "sketch_add_entity", "sketch": sid,
         "entity": {"type": "circle", "center": [12, 0], "radius": 2}},
        {"op": "sketch_add_entity", "sketch": sid,
         "entity": {"type": "circle", "center": [0, 12], "radius": 2}}]))
    assert added["ok"], added
    health = json.loads(check(doc_id=doc, sketch=sid))
    assert health["dof"] == 0 and not health["pinned_only"], health
    assert len(added["sketch_definitions"]) == 1


@enginetest
def test_feature_opt_out_persists_across_entity_batches(sketch_doc):
    doc, add, check = sketch_doc
    created = json.loads(add(doc_id=doc, ops=[sketch_op(
        {"type": "circle", "center": [0, 0], "radius": 4}, auto_define=False)]))
    assert created["ok"], created
    sid = created["id_map"]["$0"]
    added = json.loads(add(doc_id=doc, ops=[{"op": "sketch_add_entity", "sketch": sid,
        "entity": {"type": "circle", "center": [12, 0], "radius": 2}}]))
    assert added["ok"], added
    assert not added.get("sketch_definitions")
    assert json.loads(check(doc_id=doc, sketch=sid))["dof"] > 0


@enginetest
def test_blanket_fix_reports_pinned_only_not_fully_defined(sketch_doc):
    doc, add, check = sketch_doc
    op = sketch_op({"id": "e1", "type": "circle", "center": [0, 0], "radius": 4})
    op["feature"]["params"]["constraints"] = [{"id": "c1", "type": "fix", "refs": ["e1"]}]
    created = json.loads(add(doc_id=doc, auto_define=False, ops=[op]))
    assert created["ok"], created
    sid = created["id_map"]["$0"]
    health = json.loads(check(doc_id=doc, sketch=sid))
    assert health["ok"] and health["dof"] == 0, health
    assert health["pinned_only"] is True and health["fully_defined"] is False, health
    assert "pinned_instead_of_constrained" in {finding["code"] for finding in health["findings"]}


@enginetest
def test_later_agent_dimension_supersedes_soft_auto_size(sketch_doc):
    doc, add, check = sketch_doc
    created = json.loads(add(doc_id=doc, ops=[sketch_op(
        {"type": "rectangle", "corner1": [0, 0], "corner2": [40, 30]})]))
    assert created["ok"], created
    sid = created["id_map"]["$0"]
    feature = next(f for f in cb.call("doc_tree", {"doc_id": doc})["tree"]["features"]
                   if f["id"] == sid)
    bottom = feature["params"]["entities"][0]["id"]
    old = next(c for c in feature["params"]["constraints"]
               if c.get("auto") and c.get("refs") == [bottom] and "value" in c)
    changed = json.loads(add(doc_id=doc, ops=[{
        "op": "sketch_add_constraint", "sketch": sid,
        "constraint": {"type": "length", "refs": [bottom], "value": 50}}]))
    assert changed["ok"], changed
    health = json.loads(check(doc_id=doc, sketch=sid))
    assert health["dof"] == 0 and health["fully_defined"], health
    assert health["status"] != "over" and not health.get("conflicting"), health
    feature = next(f for f in cb.call("doc_tree", {"doc_id": doc})["tree"]["features"]
                   if f["id"] == sid)
    constraints = feature["params"]["constraints"]
    assert old["id"] not in {c["id"] for c in constraints}
    explicit = next(c for c in constraints if c["id"] == changed["id_map"]["$0"])
    assert "auto" not in explicit
    assert explicit["value"]["value"] == 50
