"""Regression tests for the model-dialect hardening sweep over gsl_tools.py, plm_tools.py
and cad_sim_tools.py — the tail end of the guard sweep started on cad_asm_tools.py /
cam_tools.py / cad_drawing_tools.py (see services/mcp/test_dialect_guards.py).

Covers three defect classes, per the shared cad_common.coerce_json_or_repr /
require_list / require_mapping contract:
  1. CRASH        — .get()/.items() on a model-supplied value that arrived as a string.
  2. SILENT WRONG  — a fallback/default that discards the caller's real input while
                     still reporting ok:true (a bad `isinstance(x, dict) else {}`, or
                     iterating a stringified list character-by-character).
  3. CONTENTLESS   — a rejection that names neither the offending value nor the shape.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from services.mcp.cad_common import coerce_json_or_repr, require_list, require_mapping


def _dispatch():
    from services.agent.agent_turn import _bind_tool  # noqa: PLC0415
    return _bind_tool


def _skip_if_no_bridge(exc: Exception):
    msg = str(exc)
    if "engine" in msg.lower() or "bridge" in msg.lower() or "connect" in msg.lower():
        pytest.skip(f"engine bridge unavailable: {exc}")
    raise


@pytest.fixture(scope="module")
def block_doc():
    """A real doc with solid geometry, for the cad_sim_tools live-tool tests."""
    bind_tool = _dispatch()
    new_part = bind_tool("new_part")
    add_features = bind_tool("add_features")
    try:
        r = json.loads(new_part(name="dialect_guard_block"))
    except Exception as e:  # noqa: BLE001
        _skip_if_no_bridge(e)
    if not r.get("ok"):
        pytest.skip(f"new_part failed: {r}")
    doc_id = r["doc_id"]
    r2 = json.loads(add_features(doc_id=doc_id, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Blk",
                                          "params": {"length": 100, "width": 50, "height": 20}}}]))
    if not r2.get("ok"):
        pytest.skip(f"add_features failed: {r2}")
    return doc_id


# --------------------------------------------------------------------------- cad_sim_tools.py

def test_run_fea_static_accepts_stringified_fixtures_and_loads(block_doc):
    """CRASH class: `for q in fixtures` used to iterate a stringified list character by
    character. A JSON-encoded fixtures/loads pair must now solve normally."""
    bind_tool = _dispatch()
    run_fea_static = bind_tool("run_fea_static")
    out = json.loads(run_fea_static(
        fixtures='["bottom"]', loads='[{"face": "top", "pressure": 0.5}]',
        material="steel", doc_id=block_doc))
    assert out.get("ok") is True, out
    assert "summary" in out and out["summary"].get("min_fos") is not None


def test_run_fea_static_rejects_garbage_fixtures_with_teaching_error(block_doc):
    """CONTENTLESS class guard: a non-list, non-JSON fixtures value must name the arg
    and the expected shape, not silently iterate characters or crash opaquely."""
    bind_tool = _dispatch()
    run_fea_static = bind_tool("run_fea_static")
    out = json.loads(run_fea_static(
        fixtures="not a list at all", loads=[{"face": "top", "pressure": 0.5}],
        material="steel", doc_id=block_doc))
    assert out.get("ok") is False
    assert "fixtures" in out["error"] and "list" in out["error"]


def test_run_fea_buckling_rejects_malformed_load_instead_of_silent_default(block_doc):
    """SILENT WRONG class: `load[0]` not matching used to fall through to a hard-coded
    P=1000 N default and still report ok:true with fabricated numbers. It must now
    raise a teaching error naming the missing 'force' shape."""
    bind_tool = _dispatch()
    run_fea_buckling = bind_tool("run_fea_buckling")
    out = json.loads(run_fea_buckling(
        fixtures=["bottom"], load=[{"face": "top"}], material="steel", doc_id=block_doc))
    assert out.get("ok") is False
    assert not out.get("modes") and out.get("critical_load_n") is None, out


def test_run_fea_buckling_accepts_stringified_load(block_doc):
    """CRASH-adjacent class: a JSON-stringified load must decode and solve, not be
    silently discarded in favour of the removed hard-coded default."""
    bind_tool = _dispatch()
    run_fea_buckling = bind_tool("run_fea_buckling")
    out = json.loads(run_fea_buckling(
        fixtures='["bottom"]', load='[{"face": "top", "force": [0, 0, -2000]}]',
        material="steel", doc_id=block_doc, max_load_factor=1e8))
    assert out.get("ok") is True, out
    assert out["critical_load_n"] == pytest.approx(
        out["critical_load_factor"] * 2000.0), out


def test_run_fea_modal_accepts_stringified_fixtures(block_doc):
    bind_tool = _dispatch()
    run_fea_modal = bind_tool("run_fea_modal")
    out = json.loads(run_fea_modal(fixtures='["bottom"]', material="steel",
                                   n_modes=3, doc_id=block_doc))
    assert out.get("ok") is True, out
    assert "freqs_hz" in out


def test_run_fea_thermal_accepts_stringified_hot_cold_faces(block_doc):
    bind_tool = _dispatch()
    run_fea_thermal = bind_tool("run_fea_thermal")
    out = json.loads(run_fea_thermal(
        hot_faces='[{"face": "+x", "temperature": 100}]',
        cold_faces='[{"face": "-x", "temperature": 20}]',
        material="steel", doc_id=block_doc))
    assert out.get("ok") is True, out


# --------------------------------------------------------------------------- gsl_tools.py

def test_describe_scene_accepts_stringified_views(block_doc):
    """SILENT WRONG class: `for name in view_names` used to iterate a stringified
    views list character-by-character, producing garbage view entries while still
    reporting ok:true. A JSON-encoded views list must now describe the real views."""
    bind_tool = _dispatch()
    describe_scene = bind_tool("describe_scene")
    out = json.loads(describe_scene(views='["front", "top"]', doc_id=block_doc))
    assert out.get("ok") is True, out
    assert set(out["views"].keys()) == {"front", "top"}


def test_describe_scene_rejects_bare_comma_string_with_teaching_error(block_doc):
    bind_tool = _dispatch()
    describe_scene = bind_tool("describe_scene")
    out = json.loads(describe_scene(views="front,top", doc_id=block_doc))
    assert out.get("ok") is False
    assert "views" in out["error"] and "list" in out["error"]


def test_describe_project_decodes_stringified_run_dict():
    """SILENT WRONG class: a JSON-stringified run dict used to fall through to the
    run_id branch and be narrated as an unknown id string, discarding the real run."""
    bind_tool = _dispatch()
    describe_project = bind_tool("describe_project")
    run_dict = {"id": "r7", "name": "Widget Mission",
                "nodes": [{"id": "n1", "title": "design", "status": "done"}]}
    out = json.loads(describe_project(run=json.dumps(run_dict)))
    assert out.get("ok") is True, out
    assert "Widget Mission" in out["text"] and "r7" in out["text"]


def test_describe_project_repr_string_also_decodes():
    bind_tool = _dispatch()
    describe_project = bind_tool("describe_project")
    out = json.loads(describe_project(
        run="{'id': 'r9', 'name': 'Repr Mission', 'nodes': []}"))
    assert out.get("ok") is True, out
    assert "Repr Mission" in out["text"]


def test_unwrap_var_kw_decodes_stringified_kwargs_bag():
    """Unit-level proof for _unwrap_var_kw's coercion (the live FastMCP dialect path
    hands a nested `kwargs` field that a model may JSON-encode as a string)."""
    from services.mcp.gsl_tools import _unwrap_var_kw
    kwargs = {"kwargs": '{"part": "bracket"}'}
    _unwrap_var_kw(kwargs)
    assert kwargs.get("part") == "bracket"


# --------------------------------------------------------------------------- plm_tools.py

def test_plm_plan_accepts_stringified_data_dict():
    """CRASH class (LIVE shape): planning._build's bare `data.get("title", "")` blew up
    with `'str' object has no attribute 'get'` on a stringified data dict."""
    bind_tool = _dispatch()
    plm_init = bind_tool("plm_init")
    plm_plan = bind_tool("plm_plan")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "vault")
        os.makedirs(path, exist_ok=True)
        init = json.loads(plm_init(path=path))
        if not init.get("ok"):
            pytest.skip(f"plm vault unavailable: {init}")
        out = json.loads(plm_plan(
            path=path, op="create", entity="task",
            data='{"title": "Fix the widget", "priority": "p1"}'))
        assert out.get("ok") is not False, out
        assert out["entity"]["title"] == "Fix the widget"
        assert out["entity"]["priority"] == "p1"
        assert "'str' object has no attribute 'get'" not in json.dumps(out)


def test_plm_plan_rejects_garbage_data_with_teaching_error():
    bind_tool = _dispatch()
    plm_init = bind_tool("plm_init")
    plm_plan = bind_tool("plm_plan")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "vault2")
        os.makedirs(path, exist_ok=True)
        init = json.loads(plm_init(path=path))
        if not init.get("ok"):
            pytest.skip(f"plm vault unavailable: {init}")
        out = json.loads(plm_plan(
            path=path, op="create", entity="task", data="not a dict at all"))
        assert out.get("ok") is False
        assert out.get("reason") == "invalid_data"
        assert "data" in out["hint"] and "str" in out["hint"]


# --------------------------------------------------------------------------- helper unit tests

def test_require_mapping_and_require_list_reused_not_redefined():
    assert require_mapping('{"a": 1}', "x") == {"a": 1}
    assert require_list('[1, 2]', "x") == [1, 2]
    assert coerce_json_or_repr("{'a': None}") == {"a": None}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
