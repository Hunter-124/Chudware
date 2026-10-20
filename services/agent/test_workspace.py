"""test_workspace.py — Tests for T-W16 multi-document workspace.

Covers:
- Lease contention: two agents, one doc → second rejected with owner named.
- Steal on simulated death.
- Fan-out on a 3-part fixture graph with join.
- Per-doc event stream append and replay.
- Guard perf smoke (lease check overhead < 0.5 ms per call).
- MCP tool integration smoke.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.agent.workspace import (
    LEASE_READ,
    LEASE_WRITE,
    EVENT_LEASE_CHANGE,
    EVENT_OPENED,
    EVENT_RENDERED,
    LeaseRegistry,
    WorkspaceError,
    WorkspaceSession,
    append_doc_event,
    bind_mutation_guard,
    check_write_lease,
    fanout_ready_nodes,
    read_doc_events,
    register_session,
    unbind_mutation_guard,
    unregister_session,
    _session_registry,
)
from services.agent.buildgraph.executor import BuildGraphExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_id() -> str:
    return f"test_{uuid.uuid4().hex[:12]}"


def _registry(run_id: str | None = None) -> LeaseRegistry:
    return LeaseRegistry(run_id=run_id or _run_id())


def _three_part_graph() -> dict:
    """A 3-part graph: bracket, shaft, collar → verified by a mate node."""
    return {
        "schema": "aicad.buildgraph/1",
        "mission": "test-assembly",
        "nodes": [
            {"id": "bg_bracket", "kind": "part", "label": "bracket",
             "doc_id": "doc_bracket", "requires": []},
            {"id": "bg_shaft",   "kind": "part", "label": "shaft",
             "doc_id": "doc_shaft",   "requires": []},
            {"id": "bg_collar",  "kind": "part", "label": "collar",
             "doc_id": "doc_collar",  "requires": []},
            {"id": "bg_mate",    "kind": "mate", "label": "mate parts",
             "doc_id": "doc_assembly", "requires": ["bg_bracket", "bg_shaft", "bg_collar"]},
        ],
    }


# ---------------------------------------------------------------------------
# 1. Lease contention — second agent rejected with owner named
# ---------------------------------------------------------------------------

class TestLeaseContention:
    def test_second_write_rejected_names_owner(self):
        reg = _registry()
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)

        with pytest.raises(WorkspaceError) as exc_info:
            reg.acquire_write("doc1", "agent_B")

        msg = str(exc_info.value)
        assert "agent_A" in msg, "refusal must name the current write owner"
        assert "agent_B" in msg, "refusal must name the rejected agent"
        assert "doc1" in msg

    def test_same_agent_reacquire_write_is_idempotent(self):
        reg = _registry()
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        # Re-acquiring is a no-op (same owner)
        reg.acquire_write("doc1", "agent_A")
        assert reg.write_owner("doc1") == "agent_A"

    def test_read_leases_unlimited(self):
        reg = _registry()
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        # Many readers allowed alongside the writer
        for i in range(5):
            reg.acquire_read("doc1", f"reader_{i}")
        assert reg.is_writer("doc1", "agent_A")
        assert reg.is_reader("doc1", "reader_3")

    def test_release_write_allows_new_owner(self):
        reg = _registry()
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        reg.release("doc1", "agent_A")
        # Now agent_B can acquire write
        reg.acquire_write("doc1", "agent_B")
        assert reg.write_owner("doc1") == "agent_B"

    def test_unknown_doc_raises(self):
        reg = _registry()
        with pytest.raises(WorkspaceError, match="not registered"):
            reg.acquire_write("no_such_doc", "agent_A")


# ---------------------------------------------------------------------------
# 2. Steal on simulated owner death
# ---------------------------------------------------------------------------

class TestStealOnDeath:
    def test_steal_transfers_write_lease(self):
        reg = _registry()
        reg.open_doc("doc1", "dead_agent", lease=LEASE_WRITE)
        assert reg.write_owner("doc1") == "dead_agent"

        reg.steal("doc1", "harness_recovery", reason="owner_death")
        assert reg.write_owner("doc1") == "harness_recovery"

    def test_steal_emits_event_with_previous_owner(self):
        events: list[dict] = []

        def emit(event_type, payload):
            events.append({"event_type": event_type, **payload})

        run_id = _run_id()
        reg = LeaseRegistry(run_id=run_id, emit=emit)
        reg.open_doc("doc1", "dead_agent", lease=LEASE_WRITE)
        events.clear()  # ignore open events

        reg.steal("doc1", "harness_recovery", reason="owner_death")

        lease_events = [e for e in events if e["event_type"] == EVENT_LEASE_CHANGE]
        assert len(lease_events) == 1
        ev = lease_events[0]
        assert ev["transition"] == "write_stolen"
        assert ev["previous_owner"] == "dead_agent"
        assert ev["agent_id"] == "harness_recovery"
        assert ev.get("reason") == "owner_death"

    def test_steal_recorded_in_jsonl_stream(self, tmp_path, monkeypatch):
        run_id = _run_id()
        monkeypatch.setattr(
            "services.agent.workspace.cache_root",
            lambda: tmp_path,
        )
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc1", "dead_agent", lease=LEASE_WRITE)
        reg.steal("doc1", "harness_recovery", reason="owner_death")

        events = read_doc_events(run_id, "doc1")
        stolen = [e for e in events if e.get("transition") == "write_stolen"]
        assert len(stolen) == 1
        assert stolen[0]["previous_owner"] == "dead_agent"


# ---------------------------------------------------------------------------
# 3. Fan-out on 3-part fixture graph with join
# ---------------------------------------------------------------------------

class TestFanOut:
    def test_three_parts_dispatched_then_joined(self):
        run_id = _run_id()
        graph = _three_part_graph()
        executor = BuildGraphExecutor(graph)
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc_assembly", "orchestrator", lease=LEASE_WRITE)

        spawned: list[tuple] = []

        def capture_spawn(node, doc_id, _run_id):
            spawned.append((node["id"], doc_id))

        # Cycle 1: three part nodes ready simultaneously
        dispatched = fanout_ready_nodes(
            executor, reg, run_id, "orchestrator", spawn_modeler=capture_spawn
        )
        assert set(dispatched) == {"bg_bracket", "bg_shaft", "bg_collar"}
        assert len(spawned) == 3

        # Mate node not yet ready (parts still building)
        assert "bg_mate" not in executor.ready_nodes()

        # Finish the three part nodes
        for nid in ["bg_bracket", "bg_shaft", "bg_collar"]:
            executor.finish_node(nid, success=True)

        # Cycle 2: mate node now ready (join)
        dispatched2 = fanout_ready_nodes(
            executor, reg, run_id, "orchestrator", spawn_modeler=capture_spawn
        )
        assert "bg_mate" in dispatched2
        # Mate join is handled by orchestrator — finish it
        executor.finish_node("bg_mate", success=True)

        assert executor.is_done()
        assert executor.is_succeeded()

    def test_failed_part_blocks_mate_join(self):
        run_id = _run_id()
        graph = _three_part_graph()
        executor = BuildGraphExecutor(graph)
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc_assembly", "orchestrator", lease=LEASE_WRITE)

        dispatched = fanout_ready_nodes(executor, reg, run_id, "orchestrator")
        assert set(dispatched) == {"bg_bracket", "bg_shaft", "bg_collar"}

        # One part fails
        executor.finish_node("bg_bracket", success=False, detail="modeler crash")
        executor.finish_node("bg_shaft", success=True)
        executor.finish_node("bg_collar", success=True)

        # Mate should be skipped (not ready — prerequisite failed)
        assert "bg_mate" not in executor.ready_nodes()
        assert executor.node_status("bg_mate") == "skipped"
        assert executor.is_done()

    def test_part_docs_get_write_leases(self):
        run_id = _run_id()
        graph = _three_part_graph()
        executor = BuildGraphExecutor(graph)
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc_assembly", "orchestrator", lease=LEASE_WRITE)

        fanout_ready_nodes(executor, reg, run_id, "orchestrator")

        # Each part node's doc_id should be registered with orchestrator as writer
        for doc_id in ("doc_bracket", "doc_shaft", "doc_collar"):
            assert reg.write_owner(doc_id) == "orchestrator", (
                f"{doc_id} should be owned by orchestrator after fan-out"
            )


# ---------------------------------------------------------------------------
# 4. Per-doc event stream append and replay
# ---------------------------------------------------------------------------

class TestEventStream:
    def test_append_and_read(self, tmp_path, monkeypatch):
        run_id = _run_id()
        monkeypatch.setattr("services.agent.workspace.cache_root", lambda: tmp_path)

        append_doc_event(run_id, "doc1", "doc.op_applied", {"op": "box", "w": 10})
        append_doc_event(run_id, "doc1", "doc.rebuilt", {"ok": True})
        append_doc_event(run_id, "doc1", "doc.rendered", {}, render_path="/tmp/r.png")

        events = read_doc_events(run_id, "doc1")
        assert len(events) == 3
        assert events[0]["event"] == "doc.op_applied"
        assert events[1]["event"] == "doc.rebuilt"
        assert events[2]["event"] == "doc.rendered"
        assert events[2]["render_path"] == "/tmp/r.png"

    def test_since_seq_slices_correctly(self, tmp_path, monkeypatch):
        run_id = _run_id()
        monkeypatch.setattr("services.agent.workspace.cache_root", lambda: tmp_path)

        for i in range(5):
            append_doc_event(run_id, "doc2", "doc.op_applied", {"seq": i})

        tail = read_doc_events(run_id, "doc2", since_seq=3)
        assert len(tail) == 2
        assert tail[0]["seq"] == 3
        assert tail[1]["seq"] == 4

    def test_nonexistent_stream_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("services.agent.workspace.cache_root", lambda: tmp_path)
        events = read_doc_events("no_run", "no_doc")
        assert events == []

    def test_lease_transitions_appear_in_stream(self, tmp_path, monkeypatch):
        run_id = _run_id()
        monkeypatch.setattr("services.agent.workspace.cache_root", lambda: tmp_path)
        # Also patch the cache_root used by the registry's _emit_doc_event path
        import services.agent.workspace as ws_mod
        monkeypatch.setattr(ws_mod, "cache_root", lambda: tmp_path)

        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        reg.release("doc1", "agent_A")

        events = read_doc_events(run_id, "doc1")
        event_types = [e["event"] for e in events]
        assert EVENT_OPENED in event_types
        assert EVENT_LEASE_CHANGE in event_types

    def test_registry_records_latest_render(self):
        reg = _registry()
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        reg.set_latest_render("doc1", "/cache/runs/r1/docs/doc1/render.png")

        docs = reg.list_docs()
        assert docs[0]["latest_render"] == "/cache/runs/r1/docs/doc1/render.png"


# ---------------------------------------------------------------------------
# 5. Mutation guard — dispatch-layer lease check
# ---------------------------------------------------------------------------

class TestMutationGuard:
    def test_check_write_lease_allows_when_no_registry(self):
        unbind_mutation_guard()
        result = check_write_lease("doc1", "add_features")
        assert result is None

    def test_check_write_lease_allows_own_doc(self):
        run_id = _run_id()
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        bind_mutation_guard(reg, "agent_A")
        try:
            result = check_write_lease("doc1", "add_features")
            assert result is None
        finally:
            unbind_mutation_guard()

    def test_check_write_lease_rejects_non_owner(self):
        run_id = _run_id()
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        bind_mutation_guard(reg, "agent_B")
        try:
            result = check_write_lease("doc1", "add_features")
            assert result is not None
            assert result["ok"] is False
            assert "agent_A" in result["reason"]
            assert "agent_B" in result["reason"]
            assert "owner" in result
            assert result["owner"] == "agent_A"
        finally:
            unbind_mutation_guard()

    def test_guard_in_tool_registry_rejects_non_owner(self):
        """End-to-end: _guard wraps a tool; non-owner gets a refusal."""
        from services.mcp import tool_registry as reg_mod

        run_id = _run_id()
        ws_reg = LeaseRegistry(run_id=run_id)
        ws_reg.open_doc("doc_x", "agent_A", lease=LEASE_WRITE)
        bind_mutation_guard(ws_reg, "agent_B")

        # Craft a simple mutating tool and wrap it with _guard
        def fake_add_features(doc_id: str, ops: list) -> dict:
            return {"ok": True, "mutated": True}

        guarded = reg_mod._guard(fake_add_features, "add_features")
        try:
            result = guarded(doc_id="doc_x", ops=[])
            assert result["ok"] is False
            assert "agent_A" in result["reason"]
        finally:
            unbind_mutation_guard()

    def test_guard_allows_read_tools_without_lease_check(self):
        """Read tools never trigger a lease check even for non-owners."""
        from services.mcp import tool_registry as reg_mod

        run_id = _run_id()
        ws_reg = LeaseRegistry(run_id=run_id)
        ws_reg.open_doc("doc_x", "agent_A", lease=LEASE_WRITE)
        bind_mutation_guard(ws_reg, "agent_B")

        called = []

        def fake_describe_part(doc_id: str) -> dict:
            called.append(doc_id)
            return {"ok": True, "description": "a box"}

        guarded = reg_mod._guard(fake_describe_part, "describe_part")
        try:
            # describe_part is RISK_READ — must pass through even for non-owner
            result = guarded(doc_id="doc_x")
            assert result["ok"] is True
            assert called == ["doc_x"]
        finally:
            unbind_mutation_guard()


# ---------------------------------------------------------------------------
# 6. Guard perf smoke — lease check must be fast
# ---------------------------------------------------------------------------

class TestGuardPerf:
    def test_lease_check_overhead_under_half_ms(self):
        run_id = _run_id()
        reg = LeaseRegistry(run_id=run_id)
        reg.open_doc("doc1", "agent_A", lease=LEASE_WRITE)
        bind_mutation_guard(reg, "agent_A")

        N = 1000
        try:
            t0 = time.perf_counter()
            for _ in range(N):
                check_write_lease("doc1", "add_features")
            elapsed = time.perf_counter() - t0
        finally:
            unbind_mutation_guard()

        per_call_ms = (elapsed / N) * 1000
        assert per_call_ms < 0.5, (
            f"Lease check too slow: {per_call_ms:.3f} ms/call (budget: 0.5 ms)"
        )


# ---------------------------------------------------------------------------
# 7. WorkspaceSession integration
# ---------------------------------------------------------------------------

class TestWorkspaceSession:
    def test_workspace_status_includes_docs_and_buildgraph(self):
        run_id = _run_id()
        session = WorkspaceSession(run_id=run_id)
        session.open_assembly_doc("doc_asm", "orch")

        graph = _three_part_graph()
        executor = BuildGraphExecutor(graph)

        status = session.workspace_status(executor=executor)
        assert status["schema"] == "aicad.workspace/1"
        assert status["run_id"] == run_id
        assert any(d["doc_id"] == "doc_asm" for d in status["docs"])
        assert "buildgraph" in status
        assert status["buildgraph"]["is_done"] is False

    def test_session_registry_lookup(self):
        run_id = _run_id()
        session = WorkspaceSession(run_id=run_id)
        register_session(session)
        try:
            assert _session_registry[run_id] is session
        finally:
            unregister_session(run_id)
        assert run_id not in _session_registry

    def test_record_render_updates_status(self):
        run_id = _run_id()
        session = WorkspaceSession(run_id=run_id)
        session.open_assembly_doc("doc_asm", "orch")
        session.record_render("doc_asm", "/cache/render.png")

        status = session.workspace_status()
        asm_doc = next(d for d in status["docs"] if d["doc_id"] == "doc_asm")
        assert asm_doc["latest_render"] == "/cache/render.png"

    def test_dispatch_fanout_via_session(self):
        run_id = _run_id()
        spawned = []

        def mock_spawn(node, doc_id, _run_id):
            spawned.append(node["id"])

        session = WorkspaceSession(run_id=run_id, spawn_modeler=mock_spawn)
        session.open_assembly_doc("doc_asm", "orch")

        graph = _three_part_graph()
        executor = BuildGraphExecutor(graph)

        dispatched = session.dispatch_fanout(executor, "orch")
        assert set(dispatched) == {"bg_bracket", "bg_shaft", "bg_collar"}
        assert len(spawned) == 3


# ---------------------------------------------------------------------------
# 8. MCP tool smoke tests
# ---------------------------------------------------------------------------

class TestMCPTools:
    def _make_mcp_stub(self):
        """Return a minimal MCP stub that calls through to the registered function."""
        tools = {}

        class StubMCP:
            def tool(self, *args, **kwargs):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn
                return deco

        return StubMCP(), tools

    def test_workspace_status_tool_missing_session(self):
        from services.mcp.workspace_tools import register_workspace_tools

        mcp_stub, tools = self._make_mcp_stub()
        register_workspace_tools(mcp_stub)

        result = tools["workspace_status"](run_id="nonexistent_run")
        assert result["ok"] is False
        assert "nonexistent_run" in result["error"]

    def test_workspace_status_tool_with_registered_session(self):
        from services.mcp.workspace_tools import register_workspace_tools

        run_id = _run_id()
        session = WorkspaceSession(run_id=run_id)
        session.open_assembly_doc("doc_asm", "orch")
        register_session(session)

        mcp_stub, tools = self._make_mcp_stub()
        register_workspace_tools(mcp_stub)

        try:
            result = tools["workspace_status"](run_id=run_id)
            assert result["ok"] is True
            assert result["run_id"] == run_id
            assert any(d["doc_id"] == "doc_asm" for d in result["docs"])
        finally:
            unregister_session(run_id)

    def test_workspace_doc_events_tool(self, tmp_path, monkeypatch):
        import services.agent.workspace as ws_mod
        monkeypatch.setattr(ws_mod, "cache_root", lambda: tmp_path)

        from services.mcp.workspace_tools import register_workspace_tools

        run_id = _run_id()
        append_doc_event(run_id, "doc1", "doc.op_applied", {"op": "box"})
        append_doc_event(run_id, "doc1", "doc.rebuilt", {"ok": True})

        mcp_stub, tools = self._make_mcp_stub()
        register_workspace_tools(mcp_stub)

        result = tools["workspace_doc_events"](run_id=run_id, doc_id="doc1")
        assert result["ok"] is True
        assert result["count"] == 2

        # Test since_seq slicing
        result2 = tools["workspace_doc_events"](run_id=run_id, doc_id="doc1", since_seq=1)
        assert result2["count"] == 1
        assert result2["events"][0]["event"] == "doc.rebuilt"


def test_cell_leases_sessions_and_events_are_actor_private(tmp_path, monkeypatch):
    import hashlib
    from services.agent import request_context as ctx
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    from services.mcp.workspace_tools import register_workspace_tools
    stub, tools = TestMCPTools()._make_mcp_stub()
    register_workspace_tools(stub)

    def principal(actor):
        return {"cell": True, "workspace_key": "team:shared", "actor_key": actor,
                "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
                "right": "editor", "epoch": 1}

    run_id, doc_id = "../same-run", "/same-doc"
    with ctx.bind_context(principal("alice")):
        alice = WorkspaceSession(run_id)
        register_session(alice)
        alice.open_assembly_doc(doc_id, "alice-agent")
        bind_mutation_guard(alice.registry, "alice-agent")
        assert check_write_lease(doc_id, "edit") is None
        assert _session_registry[run_id] is alice
        assert read_doc_events(run_id, doc_id)

    with ctx.bind_context(principal("bob")):
        assert list(_session_registry) == []
        refusal = {"ok": False, "error": "not_found", "reason": "not_found"}
        assert tools["workspace_status"](run_id=run_id) == refusal
        assert tools["workspace_status"](run_id="absent") == refusal
        assert tools["workspace_doc_events"](run_id=run_id, doc_id=doc_id) == refusal
        assert tools["workspace_doc_events"](run_id="absent", doc_id=doc_id) == refusal
        with pytest.raises(KeyError, match="not_found"):
            _session_registry[run_id]
        with pytest.raises(KeyError, match="not_found"):
            read_doc_events(run_id, doc_id)
        for operation in (
            lambda: alice.workspace_status(),
            lambda: alice.registry.open_doc("new-doc", "bob-agent"),
            lambda: alice.registry.write_owner(doc_id),
            lambda: alice.registry.is_reader(doc_id, "alice-agent"),
            lambda: alice.registry.acquire_write(doc_id, "bob-agent"),
            lambda: alice.registry.steal(doc_id, "bob-agent"),
            lambda: alice.registry.release(doc_id, "alice-agent"),
            lambda: alice.registry.close_doc(doc_id, "alice-agent"),
            lambda: alice.record_render(doc_id, "/foreign-image"),
            lambda: bind_mutation_guard(alice.registry, "bob-agent"),
        ):
            with pytest.raises(KeyError, match="not_found"):
                operation()
        bob = WorkspaceSession(run_id)
        register_session(bob)
        bob.open_assembly_doc(doc_id, "bob-agent")
        assert _session_registry[run_id] is bob
        assert [d["write_owner"] for d in bob.workspace_status()["docs"]] == ["bob-agent"]
        assert tools["workspace_status"](run_id=run_id)["docs"][0]["write_owner"] == "bob-agent"
        assert tools["workspace_doc_events"](run_id=run_id, doc_id=doc_id)["ok"]
        assert all(e["tenant_owner"][1] == "bob" for e in read_doc_events(run_id, doc_id))
        unregister_session(run_id)

    with ctx.bind_context(principal("alice")):
        assert _session_registry[run_id] is alice
        assert alice.registry.write_owner(doc_id) == "alice-agent"
        assert all(e["tenant_owner"][1] == "alice" for e in read_doc_events(run_id, doc_id))
        unbind_mutation_guard()
        unregister_session(run_id)
    assert not (tmp_path / "same-run").exists()
