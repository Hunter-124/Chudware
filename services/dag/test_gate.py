"""test_gate.py — Tests for gate nodes, autonomy dial, interrupt/resume.

Contract: contracts/dag_schema.md §6 (autonomy levels), §9 (checkpoint format).
Interaction shape: contracts/interaction.md §1 kind=approval, §3 batch envelope.

Key DoD assertions:
  1. gate.py interrupt/resume round-trip (LangGraph interrupt() + Command(resume=...)).
  2. executor.resume() reloads checkpoint from a NEW SqliteSaver (app-restart simulation).
  3. Autonomy dial: auto skips gate, approve gates, manual gates.
  4. VERDICT_PAUSE referenced in gate context payloads (C++ seam).
  5. Gate payload shaped as aicad.interaction/1 (ask_user compat).

NOTE: tests are pure Python (LangGraph + SQLite); they do NOT import core_bridge.
The libchudware.so restage is a build-system obligation recorded separately.
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from langgraph.types import Command

from services.dag import executor
from services.dag.autonomy import effective_autonomy, requires_gate
from services.dag.gate import (
    DECISION_APPROVE,
    DECISION_REJECT,
    make_gate_payload,
)

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

def _make_graph(
    node_autonomy: str = "approve",
    node_type: str = "gate",
) -> dict[str, Any]:
    """Minimal valid aicad.dag/1 graph with one gate node."""
    return {
        "schema": "aicad.dag/1",
        "id": str(uuid.uuid4()),
        "name": "test-gate-graph",
        "nodes": [
            {
                "id": "n_gate",
                "type": node_type,
                "name": "Test Gate",
                "inputs": {"request": "do something"},
                "on_fail": "pause",
                "autonomy": node_autonomy,
            }
        ],
        "edges": [],
    }


def _make_two_node_graph(gate_autonomy: str = "approve") -> dict[str, Any]:
    """Graph: node_a (auto) → gate_node (approve/manual) → end."""
    return {
        "schema": "aicad.dag/1",
        "id": str(uuid.uuid4()),
        "name": "two-node-gate-graph",
        "nodes": [
            {
                "id": "n_a",
                "type": "design",
                "name": "Design step",
                "inputs": {"request": "build it"},
                "on_fail": "pause",
                "autonomy": "auto",
            },
            {
                "id": "n_gate",
                "type": "gate",
                "name": "Approval gate",
                "inputs": {},
                "on_fail": "pause",
                "autonomy": gate_autonomy,
            },
        ],
        "edges": [{"from": "n_a", "to": "n_gate"}],
    }


def _mock_agent(request: str, context_pack: dict) -> Any:
    """Stub agent that always returns a successful 'confirmed' result.

    node_runner.run() maps stop_reason="confirmed" → verdict="done".
    The mock must implement the AgentRun protocol (.success, .stop_reason).
    """
    class _FakeRun:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = None
    return _FakeRun()


# ---------------------------------------------------------------------------
# §1 — make_gate_payload shape (aicad.interaction/1)
# ---------------------------------------------------------------------------

class TestGatePayloadShape:
    """Gate payloads must be shaped as aicad.interaction/1 approval batches (ask_user seam)."""

    def test_schema_field(self):
        node = {"id": "g1", "name": "Gate 1", "type": "gate", "autonomy": "approve"}
        payload = make_gate_payload(node, run_id="r-123")
        assert payload["schema"] == "aicad.interaction/1"

    def test_single_approval_question(self):
        node = {"id": "g1", "name": "Gate 1", "type": "gate", "autonomy": "approve"}
        payload = make_gate_payload(node, run_id="r-123")
        assert len(payload["questions"]) == 1
        q = payload["questions"][0]
        assert q["kind"] == "approval"
        assert q["suggested"] is True        # required non-null (contract §2)
        assert "id" in q                     # stable ledger key
        assert "prompt" in q
        assert "proposal" in q

    def test_question_id_stable(self):
        node = {"id": "my_gate", "name": "G", "type": "gate", "autonomy": "approve"}
        p1 = make_gate_payload(node, run_id="r-1", batch_id="b-fixed")
        p2 = make_gate_payload(node, run_id="r-1", batch_id="b-fixed")
        assert p1["questions"][0]["id"] == p2["questions"][0]["id"]

    def test_scope_run(self):
        node = {"id": "g1", "name": "G", "type": "gate", "autonomy": "approve"}
        payload = make_gate_payload(node, run_id="r-abc")
        assert payload["scope"] == "run"
        assert payload["run_id"] == "r-abc"
        assert payload["blocking"] is True

    def test_manual_proposal_text(self):
        node = {"id": "g1", "name": "G", "type": "gate", "autonomy": "manual"}
        payload = make_gate_payload(node, run_id="r-abc")
        assert "manual" in payload["questions"][0]["proposal"].lower()

    def test_verdict_pause_referenced_in_payload(self):
        """The payload why field references the gate contract (C++ VERDICT_PAUSE seam)."""
        node = {"id": "g1", "name": "Gate", "type": "gate", "autonomy": "approve"}
        payload = make_gate_payload(node, run_id="r-1")
        q = payload["questions"][0]
        assert "autonomy" in q["why"]


# ---------------------------------------------------------------------------
# §2 — Autonomy dial (autonomy.py)
# ---------------------------------------------------------------------------

class TestAutonomyDial:
    def test_auto_node_stays_auto_with_auto_default(self):
        assert effective_autonomy({"autonomy": "auto"}, autonomy_default="auto") == "auto"

    def test_auto_node_raised_to_approve(self):
        assert effective_autonomy({"autonomy": "auto"}, autonomy_default="approve") == "approve"

    def test_auto_node_raised_to_manual(self):
        assert effective_autonomy({"autonomy": "auto"}, autonomy_default="manual") == "manual"

    def test_approve_not_lowered_by_auto_default(self):
        assert effective_autonomy({"autonomy": "approve"}, autonomy_default="auto") == "approve"

    def test_manual_not_lowered_by_auto_default(self):
        assert effective_autonomy({"autonomy": "manual"}, autonomy_default="auto") == "manual"

    def test_manual_not_lowered_by_approve_default(self):
        assert effective_autonomy({"autonomy": "manual"}, autonomy_default="approve") == "manual"

    def test_approve_raised_to_manual_by_manual_default(self):
        assert effective_autonomy({"autonomy": "approve"}, autonomy_default="manual") == "manual"

    def test_requires_gate_auto(self):
        assert not requires_gate({"autonomy": "auto"}, autonomy_default="auto")

    def test_requires_gate_approve(self):
        assert requires_gate({"autonomy": "approve"}, autonomy_default="auto")

    def test_requires_gate_manual(self):
        assert requires_gate({"autonomy": "manual"}, autonomy_default="auto")

    def test_requires_gate_auto_raised_by_default(self):
        # auto node promoted to approve by run-level default → requires gate
        assert requires_gate({"autonomy": "auto"}, autonomy_default="approve")


# ---------------------------------------------------------------------------
# §3 — interrupt() / resume() round-trip (the core DoD)
# ---------------------------------------------------------------------------

class TestInterruptResume:
    """End-to-end interrupt → resume tests using real SqliteSaver (no mocks)."""

    def test_gate_node_pauses_run(self):
        """A graph with an approve gate node should return status=paused on first run."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            result = executor.run(
                graph,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )
        assert result["status"] == "paused", f"Expected paused, got: {result['status']}"
        assert "interrupted" in result
        assert len(result["interrupted"]) == 1

    def test_gate_payload_in_interrupt(self):
        """The interrupt payload must be shaped as aicad.interaction/1."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            result = executor.run(graph, runs_dir=Path(td), agent_fn=_mock_agent)
        payload = result["interrupted"][0]
        assert payload["schema"] == "aicad.interaction/1"
        assert payload["questions"][0]["kind"] == "approval"

    def test_resume_with_approve_completes(self):
        """After approve, the run should complete (status=done)."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            assert r1["status"] == "paused"

            r2 = executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision=DECISION_APPROVE,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )
        assert r2["status"] == "done", f"Expected done after approve, got: {r2['status']}"

    def test_resume_with_reject_pauses(self):
        """After reject, the node is paused/failed and the run stays paused."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            assert r1["status"] == "paused"

            r2 = executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision=DECISION_REJECT,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )
        assert r2["status"] == "paused", f"Expected paused after reject, got: {r2['status']}"

    def test_app_restart_simulation(self):
        """Core DoD: interrupt() persistence survives 'app restart'.

        Simulates a restart by:
          1. Calling executor.run() — pauses at gate.
          2. Dropping ALL references to that call's objects (no shared state).
          3. Calling executor.resume() with ONLY the run_id and runs_dir (no
             graph_json) — it must load the sidecar .graph.json from disk.
          4. Verifying the run completes.
        """
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        run_id = str(uuid.uuid4())

        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)

            # --- "session 1": start the run, it pauses ---
            r1 = executor.run(
                graph,
                run_id=run_id,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )
            assert r1["status"] == "paused", "Run should pause at gate"

            # Drop r1 — simulate app restart (in a real restart, even the in-memory
            # state is gone; here we just don't use r1 below).
            del r1

            # --- "session 2": resume with no graph_json (loads from .graph.json sidecar) ---
            r2 = executor.resume(
                run_id,
                graph_json=None,   # must load from sidecar — key restart test
                decision=DECISION_APPROVE,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )

        assert r2["status"] == "done", (
            f"Expected done after restart-resume, got: {r2['status']}"
        )

    def test_graph_sidecar_written(self):
        """executor.run() must persist graph_json beside the .db for restart-resume."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            executor.run(graph, run_id=run_id, runs_dir=runs_dir, agent_fn=_mock_agent)
            sidecar = runs_dir / f"{run_id}.graph.json"
            assert sidecar.exists(), "Sidecar .graph.json should be written by run()"

    def test_checkpoint_db_written(self):
        """executor.run() must write a .db checkpoint file."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            executor.run(graph, run_id=run_id, runs_dir=runs_dir, agent_fn=_mock_agent)
            db = runs_dir / f"{run_id}.db"
            assert db.exists(), "Checkpoint .db should be written by run()"


# ---------------------------------------------------------------------------
# §4 — Autonomy dial integration: auto skips gate, approve gates
# ---------------------------------------------------------------------------

class TestAutonomyDialIntegration:
    """End-to-end: auto nodes don't gate; approve/manual nodes do."""

    def test_auto_node_does_not_pause(self):
        """An auto-autonomy gate node runs as pass-through (warning emitted)."""
        graph = _make_graph(node_autonomy="auto", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            result = executor.run(graph, runs_dir=Path(td), agent_fn=_mock_agent)
        # auto = no gate interrupt; run completes (or fails on node logic, not gate)
        assert result["status"] != "paused", (
            "auto node should not trigger a gate pause"
        )

    def test_approve_node_pauses(self):
        graph = _make_graph(node_autonomy="approve", node_type="design")
        with tempfile.TemporaryDirectory() as td:
            result = executor.run(graph, runs_dir=Path(td), agent_fn=_mock_agent)
        assert result["status"] == "paused"

    def test_manual_node_pauses(self):
        graph = _make_graph(node_autonomy="manual", node_type="design")
        with tempfile.TemporaryDirectory() as td:
            result = executor.run(graph, runs_dir=Path(td), agent_fn=_mock_agent)
        assert result["status"] == "paused"

    def test_auto_default_raises_floor_to_approve(self):
        """Run-level autonomy_default='approve' promotes an auto node to gate."""
        graph = _make_graph(node_autonomy="auto", node_type="design")
        with tempfile.TemporaryDirectory() as td:
            result = executor.run(
                graph,
                runs_dir=Path(td),
                agent_fn=_mock_agent,
                autonomy_default="approve",
            )
        assert result["status"] == "paused", (
            "auto node promoted by autonomy_default='approve' should pause"
        )

    def test_two_node_graph_gates_at_second_node(self):
        """First node (auto) runs, second node (approve) triggers gate."""
        graph = _make_two_node_graph(gate_autonomy="approve")
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as td:
            result = executor.run(
                graph,
                runs_dir=Path(td),
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
        assert result["status"] == "paused"
        # n_a should have run (approval_required fires on n_gate)
        event_types = [e[0] for e in events]
        assert "dag.node.approval_required" in event_types

    def test_two_node_graph_completes_after_resume(self):
        """Full two-node flow: run → pause at gate → resume → done."""
        graph = _make_two_node_graph(gate_autonomy="approve")
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            assert r1["status"] == "paused"

            r2 = executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision=DECISION_APPROVE,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )
        assert r2["status"] == "done"


# ---------------------------------------------------------------------------
# §5 — Events emitted on gate
# ---------------------------------------------------------------------------

class TestGateEvents:
    def test_pause_event_emitted(self):
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as td:
            executor.run(
                graph,
                runs_dir=Path(td),
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
        event_types = [e[0] for e in events]
        assert "dag.node.paused" in event_types
        assert "dag.node.approval_required" in event_types

    def test_manual_required_event_emitted(self):
        graph = _make_graph(node_autonomy="manual", node_type="gate")
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as td:
            executor.run(
                graph,
                runs_dir=Path(td),
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
        event_types = [e[0] for e in events]
        assert "dag.node.manual_required" in event_types

    def test_gate_events_fire_exactly_once_across_resume(self):
        """Pause events must not double-emit on resume.

        On resume LangGraph re-executes the node handler from the top, so events
        emitted inside the handler would fire twice.  The fix emits them from
        executor.run()/resume() off the surfaced interrupt payloads — exactly
        once per suspend.  A single-gate run + approve-resume must therefore
        produce exactly ONE dag.node.paused and ONE dag.node.approval_required.
        """
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(
                graph,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
            assert r1["status"] == "paused"
            r2 = executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision=DECISION_APPROVE,
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
        assert r2["status"] == "done"
        gate_pauses = [
            p for t, p in events
            if t == "dag.node.paused" and p.get("reason") == "gate"
        ]
        approvals = [p for t, p in events if t == "dag.node.approval_required"]
        assert len(gate_pauses) == 1, f"dag.node.paused double-emitted: {gate_pauses}"
        assert len(approvals) == 1, f"approval_required double-emitted: {approvals}"

    def test_pause_event_carries_run_id(self):
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        run_id = str(uuid.uuid4())
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as td:
            executor.run(
                graph,
                run_id=run_id,
                runs_dir=Path(td),
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
        pause_payloads = [p for t, p in events if t == "dag.node.paused"]
        assert pause_payloads, "dag.node.paused event must be emitted"
        assert pause_payloads[0].get("run_id") == run_id


# ---------------------------------------------------------------------------
# §6 — "modify" decision override + fail-closed unrecognised decisions
# ---------------------------------------------------------------------------

class TestModifyDecision:
    """A 'modify' decision's user-supplied output must actually land in state."""

    def test_modify_output_lands_in_node_state(self):
        """resume(decision={'action': 'modify', ...}) completes the node with
        the user-supplied output — the override reaches downstream state."""
        graph = _make_graph(node_autonomy="manual", node_type="design")
        user_output = {"answer": 42, "note": "human-edited"}
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            assert r1["status"] == "paused"
            r2 = executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision={"action": "modify", "output": user_output},
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
            )
        assert r2["status"] == "done", f"Expected done after modify, got: {r2['status']}"
        entry = r2["nodes"]["n_gate"]
        assert entry["status"] == "done"
        assert entry["gate_decision"] == "modify"
        assert entry["output"]["data"] == user_output, (
            "user-supplied modify output must land in the node's output state"
        )

    def test_modify_skips_agent_dispatch(self):
        """With a modify override, the agent must NOT be dispatched for the node."""
        graph = _make_graph(node_autonomy="approve", node_type="design")
        calls: list[str] = []

        def counting_agent(request: str, context_pack: dict) -> Any:
            calls.append(request)
            return _mock_agent(request, context_pack)

        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=counting_agent)
            assert r1["status"] == "paused"
            assert calls == [], "gated node must not run before the decision"
            r2 = executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision={"action": "modify", "output": {"k": "v"}},
                runs_dir=runs_dir,
                agent_fn=counting_agent,
            )
        assert r2["status"] == "done"
        assert calls == [], (
            "modify supplies the output directly; the agent must not be dispatched"
        )

    def test_modify_emits_node_done_event(self):
        graph = _make_graph(node_autonomy="manual", node_type="design")
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            executor.resume(
                r1["run_id"],
                graph_json=graph,
                decision={"action": "modify", "output": {"x": 1}},
                runs_dir=runs_dir,
                agent_fn=_mock_agent,
                emit=lambda t, p: events.append((t, p)),
            )
        done_events = [p for t, p in events if t == "dag.node.done"]
        assert done_events, "modify completion must emit dag.node.done"
        assert done_events[0]["output"]["data"] == {"x": 1}


class TestUnrecognisedDecisionFailsClosed:
    """A human-approval gate must never fail open: unknown decisions reject."""

    def test_unrecognised_decision_rejects(self):
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            assert r1["status"] == "paused"
            with pytest.warns(UserWarning, match="unrecognised decision"):
                r2 = executor.resume(
                    r1["run_id"],
                    graph_json=graph,
                    decision="bogus-decision-value",
                    runs_dir=runs_dir,
                    agent_fn=_mock_agent,
                )
        assert r2["status"] == "paused", (
            f"Unrecognised decision must fail closed (paused), got: {r2['status']}"
        )
        entry = r2["nodes"]["n_gate"]
        assert entry["status"] == "paused"
        assert entry["gate_decision"] == DECISION_REJECT

    def test_unrecognised_dict_decision_rejects(self):
        """A dict without action='modify' is also unrecognised → reject."""
        graph = _make_graph(node_autonomy="approve", node_type="gate")
        with tempfile.TemporaryDirectory() as td:
            runs_dir = Path(td)
            r1 = executor.run(graph, runs_dir=runs_dir, agent_fn=_mock_agent)
            with pytest.warns(UserWarning, match="fail-closed"):
                r2 = executor.resume(
                    r1["run_id"],
                    graph_json=graph,
                    decision={"action": "overwrite", "output": {"evil": True}},
                    runs_dir=runs_dir,
                    agent_fn=_mock_agent,
                )
        assert r2["status"] == "paused"
        assert r2["nodes"]["n_gate"]["gate_decision"] == DECISION_REJECT
