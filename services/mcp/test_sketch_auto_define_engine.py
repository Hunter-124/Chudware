"""Engine acceptance for agent-authored sketch intent (run after the integrated core build)."""
from __future__ import annotations

import json
import uuid

import pytest

from services.mcp import core_bridge as cb

enginetest = pytest.mark.skipif(not cb.available(), reason="libchudware not built/staged")


@pytest.fixture
def sketch_doc():
    from services.agent.agent_turn import _bind_tool

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
    assert health["pinned_only"] is False and health["driving_dimensions"] > 0, health
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
