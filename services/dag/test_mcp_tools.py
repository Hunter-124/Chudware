"""test_mcp_tools.py — Tests for services/dag/mcp_tools.py.

Uses a MagicMock-based FastMCP stub (same pattern as test_req_tools.py) to avoid
the services/mcp/ directory shadowing the real `mcp` package when conftest.py
inserts services/ onto sys.path.

Tests the five DAG MCP tools (dag_plan, dag_run, dag_status, dag_approve, dag_abort).
executor.run()/resume() are monkeypatched to avoid real LangGraph DB writes.

Import via `dag.mcp_tools` (matching the conftest-provided `dag` package alias) to
avoid the services/mcp shadow of the `mcp` PyPI package.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# MagicMock FastMCP stub (mirrors test_req_tools.py pattern)
# ---------------------------------------------------------------------------

def _make_mcp():
    """Return a mock FastMCP that records registered tool callables by name."""
    tools: dict = {}
    mcp = MagicMock()

    def _tool_decorator():
        def decorator(fn):
            tools[fn.__name__] = fn
            return fn
        return decorator

    mcp.tool = _tool_decorator
    mcp._tools = tools
    return mcp, tools


# ---------------------------------------------------------------------------
# Import register_dag_tools with the stub mcp
# conftest.py adds services/ to sys.path so `dag` resolves to services/dag.
# ---------------------------------------------------------------------------

def _build_clients():
    mcp, tools = _make_mcp()
    from services.dag.mcp_tools import register_dag_tools
    register_dag_tools(mcp)
    return tools


# ---------------------------------------------------------------------------
# Minimal executor result shapes
# ---------------------------------------------------------------------------

def _done_result(run_id: str = None, nodes: dict = None) -> dict:
    rid = run_id or str(uuid.uuid4())
    return {
        "run_id": rid,
        "name": "test-graph",
        "status": "done",
        "nodes": nodes or {"n1": {"status": "done", "output": {}}},
        "checkpoint_db": f"/tmp/{rid}.db",
        "open_questions": [],
    }


def _paused_result(run_id: str = None) -> dict:
    rid = run_id or str(uuid.uuid4())
    return {
        "run_id": rid,
        "name": "test-graph",
        "status": "paused",
        "nodes": {"gate1": {"status": "paused", "output": {}}},
        "checkpoint_db": f"/tmp/{rid}.db",
        "open_questions": [],
        "interrupted": [{"node_id": "gate1", "batch_id": "b_x",
                          "schema": "aicad.interaction/1"}],
    }


_MINIMAL_GRAPH: dict = {
    "schema": "aicad.dag/1",
    "id": "g1",
    "name": "test",
    "nodes": [{"id": "n1", "type": "design", "name": "Node 1",
               "inputs": {}, "on_fail": "pause", "autonomy": "auto"}],
    "edges": [],
}


# ---------------------------------------------------------------------------
# All five tools registered
# ---------------------------------------------------------------------------

def test_all_five_tools_registered():
    tools = _build_clients()
    expected = {"dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort"}
    registered = set(tools.keys())
    missing = expected - registered
    assert not missing, f"Missing DAG MCP tools: {missing}"


# ---------------------------------------------------------------------------
# dag_run
# ---------------------------------------------------------------------------

def test_dag_run_starts_new_run():
    tools = _build_clients()
    run_result = _done_result()
    with patch("services.dag.executor.run", return_value=run_result) as mock_run:
        raw = tools["dag_run"](graph_json=_MINIMAL_GRAPH)
        result = json.loads(raw)
    assert result["ok"] is True
    assert result["status"] == "done"
    assert "run_id" in result
    mock_run.assert_called_once()


def test_dag_run_resumes_with_run_id(tmp_path, monkeypatch):
    """A run_id whose checkpoint EXISTS is a resume."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    tools = _build_clients()
    rid = str(uuid.uuid4())
    runs = tmp_path / "Chudware" / "runs"
    runs.mkdir(parents=True)
    (runs / f"{rid}.db").write_bytes(b"")     # the checkpoint dag_run looks for
    resume_result = _done_result(run_id=rid)
    with patch("services.dag.executor.resume", return_value=resume_result) as mock_resume:
        raw = tools["dag_run"](graph_json=_MINIMAL_GRAPH, run_id=rid)
        result = json.loads(raw)
    assert result["ok"] is True
    assert result["run_id"] == rid
    mock_resume.assert_called_once()


def test_dag_run_starts_under_a_run_id_with_no_checkpoint(tmp_path, monkeypatch):
    """A run_id with NO checkpoint is a first start under that id, not a resume.

    mission_start hands back run_id="mission" for a graph that has never
    executed; routing that to executor.resume() answered the tool's own
    documented sequence with "Run not found" (live 2026-08-06).
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    tools = _build_clients()
    with patch("services.dag.executor.run", return_value=_done_result(run_id="mission")) as mock_run, \
         patch("services.dag.executor.resume") as mock_resume:
        result = json.loads(tools["dag_run"](graph_json=_MINIMAL_GRAPH, run_id="mission"))
    assert result["ok"] is True
    assert result["run_id"] == "mission"
    mock_resume.assert_not_called()
    assert mock_run.call_args.kwargs["run_id"] == "mission"
    assert any("started a NEW run under that id" in n for n in result["interpreted"])


def test_dag_run_paused_includes_interrupted():
    tools = _build_clients()
    paused = _paused_result()
    with patch("services.dag.executor.run", return_value=paused):
        raw = tools["dag_run"](graph_json=_MINIMAL_GRAPH)
        result = json.loads(raw)
    assert result["ok"] is True
    assert result["status"] == "paused"
    assert "interrupted" in result
    assert len(result["interrupted"]) > 0


def test_dag_run_executor_error_returns_ok_false():
    tools = _build_clients()
    with patch("services.dag.executor.run", side_effect=ValueError("bad graph")):
        raw = tools["dag_run"](graph_json=_MINIMAL_GRAPH)
        result = json.loads(raw)
    assert result["ok"] is False
    assert "reason" in result


def test_dag_run_not_found_returns_ok_false(tmp_path, monkeypatch):
    """resume() losing its DB mid-flight is still ok=False, not a crash."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    tools = _build_clients()
    rid = str(uuid.uuid4())
    runs = tmp_path / "Chudware" / "runs"
    runs.mkdir(parents=True)
    (runs / f"{rid}.db").write_bytes(b"")   # exists at dispatch, gone by resume
    with patch("services.dag.executor.resume", side_effect=FileNotFoundError("no db")):
        result = json.loads(tools["dag_run"](graph_json=_MINIMAL_GRAPH, run_id=rid))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# dag_status — DoD: returns current node ID, run state, gate-pending flag
# ---------------------------------------------------------------------------

def test_dag_status_unknown_run():
    tools = _build_clients()
    with tempfile.TemporaryDirectory() as tmpdir:
        raw = tools["dag_status"](run_id="nonexistent-id", runs_dir=tmpdir)
        result = json.loads(raw)
    assert result["ok"] is False
    assert "not found" in result["reason"]


def test_dag_status_known_run():
    tools = _build_clients()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        rid = str(uuid.uuid4())
        (tmp / f"{rid}.db").write_bytes(b"")
        (tmp / f"{rid}.graph.json").write_text(
            json.dumps(_MINIMAL_GRAPH), encoding="utf-8"
        )
        raw = tools["dag_status"](run_id=rid, runs_dir=tmpdir)
        result = json.loads(raw)
    assert result["ok"] is True
    assert result["run_id"] == rid
    assert result["db_exists"] is True
    assert result["node_count"] == 1
    assert result["node_ids"] == ["n1"]


def test_dag_status_returns_gate_pending_and_status_keys():
    """DoD: dag_status returns current node ID, run state, and gate-pending flag."""
    tools = _build_clients()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        rid = str(uuid.uuid4())
        (tmp / f"{rid}.db").write_bytes(b"")
        (tmp / f"{rid}.graph.json").write_text(
            json.dumps(_MINIMAL_GRAPH), encoding="utf-8"
        )
        raw = tools["dag_status"](run_id=rid, runs_dir=tmpdir)
        result = json.loads(raw)
    assert "gate_pending" in result, "dag_status must return gate_pending flag (DoD)"
    assert "status" in result, "dag_status must return run state (DoD)"
    assert "node_ids" in result, "dag_status must return current node IDs (DoD)"


def test_dag_status_gate_pending_is_null_when_unobserved_and_true_when_paused():
    """gate_pending must never be a made-up boolean.

    It was hardcoded False with a "conservative" comment, so a run sitting at a
    gate reported `gate_pending: false` — indistinguishable from an observation
    that no gate is pending, on the one field that decides whether the caller
    calls dag_approve.
    """
    from services.dag import session
    tools = _build_clients()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        rid = str(uuid.uuid4())
        (tmp / f"{rid}.db").write_bytes(b"")
        (tmp / f"{rid}.graph.json").write_text(json.dumps(_MINIMAL_GRAPH), encoding="utf-8")

        session.RUNS.pop(rid, None)          # nothing in this process ran it
        unobserved = json.loads(tools["dag_status"](run_id=rid, runs_dir=tmpdir))
        assert unobserved["gate_pending"] is None
        assert unobserved["gate_pending_known"] is False
        assert unobserved["status"] == "unknown_after_restart"

        session.remember_result(_paused_result(run_id=rid))
        try:
            observed = json.loads(tools["dag_status"](run_id=rid, runs_dir=tmpdir))
        finally:
            session.RUNS.pop(rid, None)
    assert observed["gate_pending"] is True
    assert observed["gate_pending_known"] is True
    assert observed["status"] == "paused"
    assert observed["gate_nodes"] == ["gate1"]
    assert any("dag_approve" in a for a in observed["next_actions"])


# ---------------------------------------------------------------------------
# dag_approve — DoD: resumes a paused gate from outside (e.g. CLI or UI)
# ---------------------------------------------------------------------------

def test_dag_approve_resumes_with_approve_decision():
    tools = _build_clients()
    rid = str(uuid.uuid4())
    done = _done_result(run_id=rid)
    with patch("services.dag.executor.resume", return_value=done) as mock_resume:
        raw = tools["dag_approve"](run_id=rid, graph_json=_MINIMAL_GRAPH)
        result = json.loads(raw)
    assert result["ok"] is True
    assert result["status"] == "done"
    # Must call executor.resume with decision="approve"
    call_kwargs = mock_resume.call_args.kwargs
    assert call_kwargs.get("decision") == "approve", (
        "dag_approve must call executor.resume(decision='approve')"
    )


def test_dag_approve_not_found_returns_ok_false():
    tools = _build_clients()
    with patch("services.dag.executor.resume", side_effect=FileNotFoundError("no db")):
        raw = tools["dag_approve"](run_id="bad-id")
        result = json.loads(raw)
    assert result["ok"] is False


def test_dag_approve_chains_on_second_gate():
    """If another gate fires after approve, interrupted is present."""
    tools = _build_clients()
    rid = str(uuid.uuid4())
    paused2 = _paused_result(run_id=rid)
    with patch("services.dag.executor.resume", return_value=paused2):
        raw = tools["dag_approve"](run_id=rid)
        result = json.loads(raw)
    assert result["ok"] is True
    assert result["status"] == "paused"
    assert "interrupted" in result


# ---------------------------------------------------------------------------
# dag_abort
# ---------------------------------------------------------------------------

def test_dag_abort_injects_reject_decision():
    tools = _build_clients()
    rid = str(uuid.uuid4())
    paused = {
        "run_id": rid,
        "status": "paused",
        "nodes": {"gate1": {"status": "paused", "output": {}}},
        "checkpoint_db": f"/tmp/{rid}.db",
    }
    with patch("services.dag.executor.resume", return_value=paused) as mock_resume:
        raw = tools["dag_abort"](run_id=rid)
        result = json.loads(raw)
    assert result["ok"] is True
    # Must call executor.resume with decision="reject"
    call_kwargs = mock_resume.call_args.kwargs
    assert call_kwargs.get("decision") == "reject", (
        "dag_abort must call executor.resume(decision='reject')"
    )


def test_dag_abort_not_found_returns_ok_false():
    tools = _build_clients()
    with patch("services.dag.executor.resume", side_effect=FileNotFoundError("no db")):
        raw = tools["dag_abort"](run_id="bad-id")
        result = json.loads(raw)
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# dag_plan
# ---------------------------------------------------------------------------

def test_dag_plan_calls_executor_run():
    tools = _build_clients()
    run_result = _done_result()
    run_result["nodes"]["plan"] = {
        "status": "done",
        "output": {"data": {"agent_result": {"schema": "aicad.dag/1", "nodes": []}}},
    }
    with patch("services.dag.executor.run", return_value=run_result) as mock_run:
        raw = tools["dag_plan"](brief="Design a bracket and check interference")
        result = json.loads(raw)
    assert result["ok"] is True
    assert "run_id" in result
    mock_run.assert_called_once()


def test_dag_plan_executor_error_returns_ok_false():
    tools = _build_clients()
    with patch("services.dag.executor.run", side_effect=RuntimeError("agent down")):
        raw = tools["dag_plan"](brief="anything")
        result = json.loads(raw)
    assert result["ok"] is False


@pytest.mark.parametrize("timeout_s", [0, 10])
def test_actual_fastmcp_dag_run_and_native_schema_do_not_expose_session_authority(
    tmp_path, monkeypatch, timeout_s
):
    import asyncio
    import hashlib
    from types import SimpleNamespace
    from mcp.server.fastmcp import FastMCP
    from services.agent import request_context as tenancy
    from services.agent.chudcode_tools import build_host_tools, dispatch_host_tool
    from services.agent.test_chudcode_tools import _Policy
    from services.dag import executor, storage
    from services.dag.mcp_tools import register_dag_tools
    from services.dag.test_executor import _two_node_graph
    from services.agent import config

    monkeypatch.setattr(config, "get_tool_options", lambda *args: {"timeout_s": timeout_s})

    # Keep the real FastMCP schema/validation and adapter paths, but make any
    # attempt to execute unauthorized work fail instead of faking a done DAG.
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized cell invocation reached the DAG executor")

    monkeypatch.setattr(executor, "run", forbidden)
    monkeypatch.setattr(executor, "resume", forbidden)
    mcp = FastMCP("dag-session-scope")
    register_dag_tools(mcp)
    tool = mcp._tool_manager.get_tool("dag_run")
    assert not {"session_id", "parent_session_id", "_parent_session_id"} & tool.parameters["properties"].keys()

    def bind(parent=None):
        [binding] = [candidate for candidate in build_host_tools({
            "tool_entries": {"dag_run": {"fn": tool.fn, "input_schema": tool.parameters}},
            "policy_registry": _Policy(), "chudcode_builtin_names": (), "mode": "agent",
            "session_id": parent,
        }) if candidate.name == "dag_run"]
        assert not {"session_id", "parent_session_id", "_parent_session_id"} & binding.input_schema["properties"].keys()
        return binding

    unscoped = bind()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    ctx = {"cell": True, "workspace_key": "team:dag/scope", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    with tenancy.bind_context(ctx):
        owned = [bind(), bind("model-forged-session")]
    foreign = {**ctx, "actor_key": "bob", "actor_hex": hashlib.sha256(b"bob").hexdigest()[:32]}
    with tenancy.bind_context(foreign):
        other_actor = bind("model-forged-session")
    with tenancy.bind_context({**ctx, "right": "owner", "epoch": 0}):
        stale = bind("model-forged-session")

    def decoded(value):
        return json.loads(value) if isinstance(value, str) else value

    args = {"graph_json": _two_node_graph(), "autonomy_default": "auto",
            "initial_state": {"session_id": "model-forged-session"}}
    expected = {"ok": False, "reason": "not_found", "code": "not_found"}
    with tenancy.bind_context(ctx):
        result = asyncio.run(tool.run(args))
        if isinstance(result, tuple):
            result = result[0]
        if isinstance(result, list):
            result = result[0].text
        assert decoded(result) == expected
        for binding in [unscoped, *owned, other_actor, stale]:
            assert decoded(dispatch_host_tool(binding, args, None, None)) == expected
            sdk_tool = binding.to_host_tool(lambda **kwargs: kwargs)
            assert sdk_tool["execute"](args, SimpleNamespace(cancelled=False))["details"] == expected
            assert tenancy.current_context() == ctx
        assert storage.list_run_ids() == []


@pytest.mark.parametrize("session_owner", [None, "bob", "alice"])
def test_dag_run_root_refusal_follows_session_authorization(monkeypatch, session_owner):
    import hashlib
    from types import SimpleNamespace
    from services.agent import chudcode_host, request_context as tenancy
    from services.agent.chudcode_tools import HostToolBinding, dispatch_host_tool

    ctx = {"cell": True, "workspace_key": "team:dag/scope", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    host = chudcode_host.ChudcodeSessionHost()
    monkeypatch.setattr(chudcode_host, "get_default_chudcode_host", lambda: host)
    if session_owner is not None:
        owner_ctx = {**ctx, "actor_key": session_owner,
                     "actor_hex": hashlib.sha256(session_owner.encode()).hexdigest()[:32]}
        with tenancy.bind_context(owner_ctx):
            host._sessions[tenancy.scoped_key("parent")] = chudcode_host._Session(
                "parent", client=None, resume_file=None, workspace_dir=None,
            )

    def forbidden(**kwargs):
        pytest.fail("a rootless DAG invocation reached the tool body")

    with tenancy.bind_context(ctx):
        binding = HostToolBinding(
            name="dag_run", description="Run a DAG", input_schema={"type": "object"},
            handler=forbidden, context={"session_id": "parent"}, project_root="", save_root="",
        )
    expected = (
        {"ok": False, "reason": "workspace_root_unavailable",
         "error": "The owning turn has no project root; ambient roots are not available."}
        if session_owner == "alice"
        else {"ok": False, "reason": "not_found", "code": "not_found"}
    )
    # SDK threads have no active owner; authorization must use the captured one.
    assert dispatch_host_tool(binding, {}, None, None) == expected
    sdk_tool = binding.to_host_tool(lambda **kwargs: kwargs)
    assert sdk_tool["execute"]({}, SimpleNamespace(cancelled=False))["details"] == expected
    assert tenancy.current_context() == {}
