"""HERC audit (docs/dev/herc-rover-mcp-audit.md) edit/document semantics at the MCP layer.

Unit tests use fakes; the `live` ones drive the suite's private hostd.
Run:  python -m pytest services/mcp/test_mcp_edit_semantics.py -q
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.mcp import cad_common as C
from services.mcp import cad_doc_tools as D
from services.mcp import core_bridge as cb


# --------------------------------------------------------------- doc-id re-issue guard

class _FakeEngine:
    def __init__(self):
        self.rows = []
        self.calls = []

    def dispatch(self, method, params=None):
        self.calls.append(method)
        if method == "doc_list":
            return {"docs": [dict(r) for r in self.rows]}
        return {"ok": True, "revision": 1}


@pytest.fixture
def guarded(monkeypatch):
    fake = _FakeEngine()
    start = {"id": "run-A"}
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    monkeypatch.setattr(cb, "_dispatch_call", fake.dispatch)
    monkeypatch.setattr(cb, "_engine_start_id", lambda: start["id"])
    monkeypatch.setattr(C, "note_touched_doc", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_DOC_IDENTITY", {})
    return fake, start


def test_mutation_on_reissued_doc_id_is_refused(guarded):
    fake, start = guarded
    fake.rows = [{"doc_id": "doc-cpp00000002", "name": "FRAME-100", "revision": 3}]
    cb.note_doc_identities(fake.rows)
    start["id"] = "run-B"  # hostd restarted and re-issued the id
    fake.rows = [{"doc_id": "doc-cpp00000002", "name": "chassis_asm", "revision": 0},
                 {"doc_id": "doc-cpp00000005", "name": "FRAME-100", "revision": 0}]
    with pytest.raises(cb.CoreError) as exc:
        cb.call("doc_apply_ops", {"doc_id": "doc-cpp00000002", "ops": []})
    assert exc.value.code == "doc_id_reissued"
    assert "FRAME-100" in str(exc.value) and "chassis_asm" in str(exc.value)
    assert exc.value.detail["expected_doc_id"] == "doc-cpp00000005"
    assert "doc_apply_ops" not in fake.calls
    # A read still proceeds, and a close is refused like a write.
    cb.call("doc_tree", {"doc_id": "doc-cpp00000002"})
    cb.remember_resolution_rows(fake.rows)
    with pytest.raises(cb.CoreError):
        cb.call("doc_close", {"doc_id": "doc-cpp00000002"})
    assert "doc_close" not in fake.calls
    # list_parts (note_doc_identities) is how the caller learns the new mapping.
    cb.note_doc_identities(fake.rows)
    cb.call("doc_apply_ops", {"doc_id": "doc-cpp00000002", "ops": []})
    assert "doc_apply_ops" in fake.calls


def test_same_part_after_restart_and_same_run_rename_pass(guarded):
    fake, start = guarded
    fake.rows = [{"doc_id": "doc-cpp00000001", "name": "bracket", "revision": 2}]
    cb.note_doc_identities(fake.rows)
    start["id"] = "run-B"
    cb.call("doc_apply_ops", {"doc_id": "doc-cpp00000001", "ops": []})  # same name: fine
    fake.rows = [{"doc_id": "doc-cpp00000001", "name": "bracket_renamed", "revision": 3}]
    cb.call("doc_apply_ops", {"doc_id": "doc-cpp00000001", "ops": []})  # same run: fine
    assert fake.calls.count("doc_apply_ops") == 2


# --------------------------------------------------------------- empty-tree autosave guard

def test_autosave_refuses_to_overwrite_file_with_emptied_part(monkeypatch, tmp_path):
    target = tmp_path / "frame.step+"
    target.write_text("previous version")
    row = {"doc_id": "d1", "name": "frame", "path": str(target), "feature_count": 0,
           "component_count": 0, "revision": 9}
    saves = []

    def call(method, params=None):
        if method == "doc_list":
            return {"docs": [row]}
        saves.append(params)
        return {"ok": True, "path": str(target)}

    monkeypatch.setattr(C.cb, "call", call)
    monkeypatch.setattr(C, "_native_deliverable_path", lambda entry: entry["path"])
    monkeypatch.setattr(C.cb, "doc_content_peak", lambda doc_id: 8)
    out: dict = {}
    C.persist_result("d1", out)
    assert out["persisted"] is False
    assert out["persist_error"]["code"] == "empty_tree_not_persisted"
    assert "save_part(path=" in out["persist_error"]["message"]
    assert saves == [] and target.read_text() == "previous version"
    # A part never seen with content (a fresh empty part) still persists.
    monkeypatch.setattr(C.cb, "doc_content_peak", lambda doc_id: 0)
    out = {}
    C.persist_result("d1", out)
    assert out["persisted"] is True and len(saves) == 1


# --------------------------------------------------------------- op normalizers

def test_set_material_op_accepts_the_tool_spelling():
    notes: list = []
    ops = D._normalize_op_elements(
        [{"op": "set_material", "material": "A36", "body": 2}], notes)
    assert ops == [{"op": "set_material", "key": "A36", "id": "b2"}]
    assert any("interpreted material='A36' as key" in n for n in notes)


def test_component_rename_routes_to_asm_edit_component():
    notes: list = []
    ops = D._normalize_op_elements([{"op": "rename", "id": "k56", "name": "LeftWheel"},
                                    {"op": "rename", "id": "f2", "name": "Base"}], notes)
    assert ops[0] == {"op": "asm_edit_component", "id": "k56", "name": "LeftWheel"}
    assert ops[1] == {"op": "rename", "id": "f2", "name": "Base"}


@pytest.mark.parametrize("feature", [
    {"type": "transform", "refs": {"body": "$3"}, "params": {"translate": [5, 0, 0]}},
    {"type": "transform", "refs": {"bodies": ["$3"]}, "params": {"translate": [5, 0, 0]}},
    {"type": "transform", "params": {"body": "$3", "translate": [5, 0, 0]}},
])
def test_transform_body_lands_in_params(feature):
    notes: list = []
    ops = D._normalize_transform_body([{"op": "add_feature", "feature": feature}], notes)
    feat = ops[0]["feature"]
    assert feat["params"]["body"] == "$3" and "refs" not in feat


def test_transform_display_body_id_is_refused_with_the_accepted_forms():
    with pytest.raises(ValueError, match="OWNING FEATURE id"):
        D._normalize_transform_body([{"op": "add_feature", "feature": {
            "type": "transform", "params": {"body": "b1", "translate": [1, 0, 0]}}}], [])


# --------------------------------------------------------------- reply size

def test_feature_rows_keep_small_data_and_the_fillet_report():
    rows = [{"id": "f1", "ok": True, "data": {"fillet": {"requested": 2}, "blob": "x" * 5000}},
            {"id": "f2", "ok": True, "data": {"note": "small"}},
            {"id": "f3", "ok": True, "data": None}]
    D._compact_feature_rows(rows)
    assert rows[0]["data"] == {"fillet": {"requested": 2}} and rows[0]["data_elided_chars"] > 5000
    assert rows[1]["data"] == {"note": "small"}
    assert "data" not in rows[2]


def test_cut_safety_net():
    ops = [{"op": "add_feature", "feature": {"type": "sketch", "params": {}}},
           {"op": "add_feature", "feature": {"type": "cylinder", "params": {"mode": "cut"}}}]
    ids = D._cut_batch_feature_ids(ops, {"$1": "f9"})
    assert ids == ["f9"]
    rows = [{"id": "f9", "ok": True, "warning": None}]
    miss = D._cut_warnings(ids, rows, 1000.0, 1000.0, 1, 1)
    assert miss and "no material" in miss["summary"]
    split = D._cut_warnings(ids, rows, 1000.0, 900.0, 5, 10)
    assert split and "solid count went UP" in split["summary"]
    assert D._cut_warnings(ids, rows, 1000.0, 900.0, 1, 1) is None
    # The engine's own per-feature warning wins.
    assert D._cut_warnings(ids, [{"id": "f9", "warning": "cut missed"}], 1.0, 1.0, 1, 1) is None
    # A batch that also adds material is not judged.
    mixed = ops + [{"op": "add_feature", "feature": {"type": "box", "params": {}}}]
    assert D._cut_batch_feature_ids(mixed, {}) is None


def test_get_part_summary_is_size_capped(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    big = {"entities": [{"id": f"e{i}", "pts": [[i * 1.123456, i * 2.654321]] * 8}
                        for i in range(40)], "depth": 5}
    features = [{"id": f"f{i}", "name": f"F{i}", "type": "sketch", "params": big}
                for i in range(1, 60)]
    native = {"revision": 7, "tree": {"features": features, "rollback_after": "f50"}}
    monkeypatch.setattr(D, "_doc", lambda doc_id: "doc-1")
    monkeypatch.setattr(D.cb, "call", lambda method, args: native)
    monkeypatch.setattr(D.cb, "lease_document", lambda doc_id: None)
    mcp = FastMCP("test-get-part-cap")
    D.register_cad_doc_tools(mcp)
    get_part = mcp._tool_manager._tools["get_part"].fn
    text = get_part(doc_id="doc-1", summary=True)
    out = json.loads(text)
    assert len(text) < 40_000, len(text)
    rows = out["tree"]["features"]
    assert len(rows) == 59 and out["type_counts"] == {"sketch": 59}
    assert rows[0]["params_elided"] == ["entities"] and rows[0]["params"] == {"depth": 5}
    assert rows[50]["status"] == "rolled_back" and rows[0]["status"] == "active"
    assert all({"id", "name", "type", "status"} <= set(r) for r in rows)


# --------------------------------------------------------------- live (private hostd)

def _call(name: str, args: dict):
    from services.mcp import server

    result = asyncio.run(server.mcp.call_tool(name, args))
    if isinstance(result, tuple):
        _content, structured = result
        text = structured.get("result") if isinstance(structured, dict) else None
    else:
        text = next((c.text for c in result if getattr(c, "type", None) == "text"), None)
    return json.loads(text)


live = pytest.mark.skipif(not cb.available(), reason="libchudware not staged")

_BOX = {"op": "add_feature", "feature": {"type": "box", "name": "Block",
                                         "params": {"length": 40, "width": 30, "height": 20}}}


@live
def test_expect_revision_makes_a_retry_conditional():
    part = _call("new_part", {"name": "herc_expect_revision", "fresh": True})
    doc, rev = part["doc_id"], part["revision"]
    first = _call("add_features", {"doc_id": doc, "ops": [_BOX], "expect_revision": rev})
    assert first["ok"], first
    retry = _call("add_features", {"doc_id": doc, "ops": [_BOX], "expect_revision": rev})
    assert retry["ok"] is False and retry["code"] == "revision_mismatch", retry
    assert retry["applied"] is False and retry["current_revision"] == first["revision"]
    tree = _call("get_part", {"doc_id": doc, "summary": True})
    assert tree["feature_count"] == 1, tree
    _call("close_part", {"doc_id": doc, "discard": True})


@live
def test_set_material_op_inside_add_features_reads_material():
    part = _call("new_part", {"name": "herc_material_op", "fresh": True})
    doc = part["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        _BOX, {"op": "set_material", "material": "6061-T6"}]})
    assert r["ok"], r
    assert any("as key" in n for n in r.get("interpreted") or []), r
    _call("close_part", {"doc_id": doc, "discard": True})


@live
def test_cut_that_splits_the_body_warns():
    part = _call("new_part", {"name": "herc_cut_split", "fresh": True})
    doc = part["doc_id"]
    assert _call("add_features", {"doc_id": doc, "ops": [_BOX]})["ok"]
    r = _call("add_features", {"doc_id": doc, "ops": [{"op": "add_feature", "feature": {
        "type": "box", "name": "Slit",
        "params": {"length": 2, "width": 100, "height": 100, "mode": "cut"}}}]})
    assert r["ok"], r
    assert r["body_count"] == 2, r
    assert ("solid count went UP" in (r.get("cut_warnings") or {}).get("summary", "")
            or any(f.get("warning") for f in r["features"])), r
    _call("close_part", {"doc_id": doc, "discard": True})
