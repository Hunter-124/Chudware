"""DAG tenant boundary regressions: actual checkpoints, RPCs and event sinks."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from services.agent.request_context import bind_context, bind_roots, current_context, owner_key, root_value
from services.dag import executor, session, storage, worker_rpc
from services.dag.event_journal import EventJournal, run_events_path
from services.dag.graph_loader import _make_node_handler
from services.dag.mcp_tools import register_dag_tools, register_dag_replan_tools
from services.dag.replan import dag_replan


def _actor(name: str) -> dict:
    return {"cell": True, "workspace_key": "team:org/team", "actor_key": name,
            "actor_hex": hashlib.sha256(name.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1}


def _graph() -> dict:
    return {"schema": "aicad.dag/1", "id": "fixture", "name": "private fixture",
            "nodes": [{"id": "gate", "type": "gate", "name": "Approve fixture",
                       "inputs": {}, "autonomy": "approve", "on_fail": "abort"}],
            "edges": []}


@pytest.fixture
def tools():
    mcp = FastMCP("dag-tenancy-fixture")
    register_dag_tools(mcp)
    register_dag_replan_tools(mcp)
    return {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}


def test_same_handle_actor_owned_checkpoints_and_controls(tmp_path, monkeypatch, tools):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private"))
    caller_root = tmp_path / "caller-controlled"
    from services.dag import node_runner
    monkeypatch.setattr(node_runner, "run", lambda *args, **kwargs: (
        "done", {"status": "done", "summary": "fixture", "data": {}}))
    with bind_context(_actor("alice")):
        alice = executor.run(_graph(), run_id="mission", runs_dir=caller_root)
        assert alice["status"] == "paused"
        alice_record = storage.resolve_run("mission")
        journal = EventJournal(alice["run_id"])
        journal_path = journal.path
        session.remember_graph(_graph(), "alice-plan")
        session.remember_run(alice["run_id"])
        session.remember_result(alice)
        assert tools["dag_status"](run_id="mission", runs_dir=str(caller_root))
        # The mapping is persisted and the public ID never names a file/directory.
        assert alice["run_id"] not in Path(alice["checkpoint_db"]).parts
        assert "mission" not in Path(alice["checkpoint_db"]).parts
        assert alice_record.owner == owner_key()
        assert alice_record.root.is_dir()

    with bind_context(_actor("bob")):
        assert session.planned_graph() is None
        assert session.last_run() == ""
        assert session.run_result(alice["run_id"]) is None
        assert storage.list_run_ids() == []
        bob = executor.run(_graph(), run_id="mission", runs_dir=caller_root)
        assert bob["run_id"] != alice["run_id"]
        assert bob["checkpoint_db"] != alice["checkpoint_db"]
        assert storage.list_run_ids() == [bob["run_id"]]
        for method in ("dag_status", "dag_approve", "dag_abort"):
            def call(handle):
                return worker_rpc.WORKER_METHODS[method](
                    {"run_id": handle, "runs_dir": str(alice_record.root),
                     "graph_json": _graph(), "autonomy_default": "auto"}, lambda *_: None)
            assert call(alice["run_id"]) == call("missing") == {
                "ok": False, "reason": "not_found", "code": "not_found"}
            assert json.loads(tools[method](run_id=alice["run_id"])) == json.loads(tools[method](run_id="missing"))
        with pytest.raises(KeyError, match="not_found"):
            executor.resume(alice["run_id"], runs_dir=alice_record.root)
        with pytest.raises(KeyError, match="not_found"):
            dag_replan(alice["run_id"], [], [], "gate", runs_dir=alice_record.root)
        with pytest.raises(KeyError, match="not_found"):
            run_events_path(alice["run_id"], runs_dir=alice_record.root)
        with pytest.raises(KeyError, match="not_found"):
            journal.emit("dag.run.done", {})
        with pytest.raises(KeyError, match="not_found"):
            _ = journal.path
        assert journal_path.read_text()  # original persisted events remain untouched
        rejected = json.loads(tools["dag_abort"](run_id=bob["run_id"]))
        assert rejected["ok"] and rejected["status"] == "paused"

    with bind_context(_actor("alice")):
        session.reset()  # restart-style read: disk ownership does not rely on session state
        assert worker_rpc.WORKER_METHODS["dag_status"]({"run_id": alice["run_id"]}, lambda *_: None)["ok"]
        replacement = _graph()
        replacement["name"] = "unapproved replacement"
        approved = worker_rpc.WORKER_METHODS["dag_approve"](
            {"run_id": alice["run_id"], "graph_json": replacement, "autonomy_default": "auto"}, lambda *_: None)
        assert approved["ok"]
        assert json.loads(alice_record.graph.read_text())["name"] == "private fixture"
    assert not caller_root.exists()


def test_resume_preserves_stored_autonomy_floor(tmp_path, monkeypatch):
    from services.dag import node_runner
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(node_runner, "run", lambda *args, **kwargs: (
        "done", {"status": "done", "summary": "fixture", "data": {}}))
    graph = _graph()
    graph["nodes"] = [
        {"id": name, "type": "design", "name": name, "inputs": {},
         "autonomy": "auto", "on_fail": "abort"}
        for name in ("first", "second")
    ]
    graph["edges"] = [{"from": "first", "to": "second"}]
    with bind_context(_actor("alice")):
        result = executor.run(graph, run_id="mission", autonomy_default="approve")
        assert result["status"] == "paused"
        resumed = executor.resume(result["run_id"], graph_json={"invalid": True},
                                  autonomy_default="auto")
        assert resumed["status"] == "paused"
        assert resumed["nodes"]["first"]["status"] == "done"
        assert resumed["interrupted"][0]["node_id"] == "second"


def test_node_threads_explicitly_inherit_identity_and_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    observed = []

    class Done:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = None

    def agent(request, pack):
        observed.append((current_context(), root_value("CHUDWARE_PROJECT_DIR"), root_value("CHUDWARE_PROJECT_SAVE_DIR")))
        return Done()

    node = {"id": "design", "type": "design", "name": "Fixture", "inputs": {"request": "fixture"},
            "autonomy": "auto", "on_fail": "abort"}
    with bind_context(_actor("alice")), bind_roots("/work/project", "/work/saved"):
        handler = _make_node_handler(node, None, agent_fn=agent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(handler, {"run_id": "fixture", "node_states": {}}).result()
    assert result["node_states"]["design"]["status"] == "done"
    assert observed == [(_actor("alice"), "/work/project", "/work/saved")]


def test_cell_without_trusted_envelope_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="cell_refused"):
        executor.run(_graph(), runs_dir=tmp_path / "caller")
    assert not (tmp_path / "caller").exists()


@pytest.mark.parametrize("handle", ["../escape", "/absolute", r"a\b", ".", "..", "",
                                   " ", "bad\x00id", "a" * 129, None, False, 7, [], {}])
def test_malformed_dag_handles_match_absent_and_foreign_before_control(
    handle, tmp_path, monkeypatch, tools,
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private"))
    with bind_context(_actor("alice")):
        record = storage.create_run("alice-only")
        record.db.touch()
        record.graph.write_text(json.dumps(_graph()))
    expected = {"ok": False, "reason": "not_found", "code": "not_found"}
    with bind_context(_actor("bob")):
        session.reset()
        for identifier in (handle, "missing", record.run_id):
            with pytest.raises(KeyError, match="not_found"):
                storage.resolve_run(identifier)
            for method in ("dag_status", "dag_approve", "dag_abort"):
                assert worker_rpc.WORKER_METHODS[method](
                    {"run_id": identifier}, lambda *_: None,
                ) == expected
                assert json.loads(tools[method](run_id=identifier)) == expected
            assert json.loads(tools["dag_replan"](run_id=identifier)) == expected
    assert record.db.exists()
    assert json.loads(record.graph.read_text()) == _graph()


def test_desktop_dag_keeps_validation_message():
    with bind_context(None):
        result = worker_rpc.WORKER_METHODS["dag_status"](
            {"run_id": "../escape"}, lambda *_: None,
        )
    assert result == {
        "ok": False, "reason": "dag_status failed: run_id must not contain path separators",
    }
