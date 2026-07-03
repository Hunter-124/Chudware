"""Test suite for node_runner.py and context_pack.py — T-A2.

All tests use injected mock agent functions (no live LLM, no engine build).

Run with:
    ~/.local/share/AI-CAD/mcp-venv/bin/pytest services/dag/test_node_runner.py -q

Or with real engine available:
    AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so \\
        ~/.local/share/AI-CAD/mcp-venv/bin/pytest services/dag/test_node_runner.py -q
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# `dag` resolves via the repo-root symlink (dag -> services/dag, matching
# agent/deliverables/engine/installer/mcp_server) — see services/dag/conftest.py for why
# this no longer does sys.path.insert(0, services_dir) (T-V3: that shadowed the real mcp SDK).
from dag import context_pack as cp
from dag import node_runner as nr
from dag import executor, graph_loader
from dag.graph_loader import GraphValidationError, build_state_graph, validate_graph


# ---------------------------------------------------------------------------
# Helpers — fake AgentRun and agent_fn builders
# ---------------------------------------------------------------------------

def _fake_run(stop_reason: str = "confirmed", success: bool = True) -> Any:
    """Return a mock AgentRun-like object."""
    m = MagicMock()
    m.stop_reason = stop_reason
    m.success = success
    m.final_code = None
    m.result = None
    return m


def _agent_always(stop_reason: str = "confirmed", success: bool = True):
    """Return an agent_fn that always returns the given result."""
    def _fn(request: str, pack: dict) -> Any:
        return _fake_run(stop_reason=stop_reason, success=success)
    return _fn


def _agent_sequence(*results):
    """Return an agent_fn that returns each result in turn (cycling on exhaustion)."""
    calls = [0]
    result_list = list(results)

    def _fn(request: str, pack: dict) -> Any:
        idx = min(calls[0], len(result_list) - 1)
        calls[0] += 1
        return result_list[idx]

    return _fn


def _minimal_node(
    node_id: str = "n1",
    node_type: str = "design",
    on_fail: str = "pause",
    autonomy: str = "auto",
    tools: Any = None,
    inputs: dict | None = None,
) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "name": f"Node {node_id}",
        "inputs": inputs or {"request": "make a bracket"},
        "tools": tools,
        "end_condition": None,
        "verify": None,
        "on_fail": on_fail,
        "autonomy": autonomy,
    }


def _minimal_graph(*nodes, edges=None) -> dict:
    return {
        "schema": "aicad.dag/1",
        "id": "test-graph-node-runner",
        "name": "test",
        "nodes": list(nodes),
        "edges": edges or [],
    }


# ---------------------------------------------------------------------------
# context_pack tests
# ---------------------------------------------------------------------------

class TestContextPack:
    def test_assembles_basic_fields(self):
        node = _minimal_node()
        pack = cp.assemble(node, {})
        assert pack["node_id"] == "n1"
        assert pack["node_type"] == "design"
        assert pack["node_name"] == "Node n1"
        assert pack["node_inputs"] == {"request": "make a bracket"}
        assert pack["allowed_tools"] is None  # no tools restriction
        assert pack["retry_remaining"] == cp.DEFAULT_RETRY_BUDGET
        assert pack["end_condition"] is None
        assert pack["part_description"] is None

    def test_allowed_tools_from_node(self):
        node = _minimal_node(tools=["describe_part", "add_features"])
        pack = cp.assemble(node, {})
        assert pack["allowed_tools"] == ["describe_part", "add_features"]

    def test_retry_remaining_from_node_state(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 2})
        assert pack["retry_remaining"] == 2

    def test_no_history_in_pack(self):
        """Context pack must NOT contain any conversation history."""
        node = _minimal_node()
        pack = cp.assemble(node, {})
        assert "messages" not in pack
        assert "history" not in pack
        assert "conversation" not in pack

    def test_describe_fn_called_for_doc_id(self):
        node = _minimal_node(inputs={"request": "analyse", "doc_id": "doc-abc"})
        called_with = []

        def fake_describe(doc_id: str) -> str:
            called_with.append(doc_id)
            return "outline: box 10x10x10mm"

        pack = cp.assemble(node, {}, describe_fn=fake_describe)
        assert called_with == ["doc-abc"]
        assert pack["part_description"] == "outline: box 10x10x10mm"

    def test_describe_fn_not_called_without_doc_id(self):
        node = _minimal_node()
        called = []

        def fake_describe(doc_id: str) -> str:
            called.append(doc_id)
            return "outline"

        pack = cp.assemble(node, {}, describe_fn=fake_describe)
        assert called == []
        assert pack["part_description"] is None

    def test_describe_fn_failure_is_graceful(self):
        """A describe_part failure must not raise — part_description becomes None."""
        node = _minimal_node(inputs={"request": "x", "doc_id": "bad-doc"})

        def broken_describe(doc_id: str) -> str:
            raise RuntimeError("engine not available")

        pack = cp.assemble(node, {}, describe_fn=broken_describe)
        assert pack["part_description"] is None


# ---------------------------------------------------------------------------
# node_runner verdict mapping tests
# ---------------------------------------------------------------------------

class TestNodeRunnerVerdicts:
    def test_confirmed_success_is_done(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 1})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("confirmed", True))
        assert verdict == nr.VERDICT_DONE
        assert out["status"] == "done"

    def test_stabilized_success_is_done(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 1})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("stabilized", True))
        assert verdict == nr.VERDICT_DONE

    def test_max_iters_is_retry(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 2})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("max_iters", False))
        assert verdict == nr.VERDICT_RETRY
        assert out["retry_remaining"] == 1

    def test_max_iters_exhausted_budget_is_escalate(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 1})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("max_iters", False))
        # budget was 1 → decrement to 0 → escalate
        assert verdict == nr.VERDICT_ESCALATE
        assert out["retry_remaining"] == 0

    def test_failed_is_escalate(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 3})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("failed", False))
        assert verdict == nr.VERDICT_ESCALATE

    def test_no_code_is_escalate(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 3})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("no_code", False))
        assert verdict == nr.VERDICT_ESCALATE

    def test_agent_exception_is_escalate(self):
        node = _minimal_node()
        pack = cp.assemble(node, {})

        def boom(req, pack):
            raise RuntimeError("engine crash")

        verdict, out = nr.run(node, pack, agent_fn=boom)
        assert verdict == nr.VERDICT_ESCALATE
        assert "engine crash" in out["data"]["error"]

    def test_retry_decrement_tracked_in_output(self):
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 3})
        verdict, out = nr.run(node, pack, agent_fn=_agent_always("max_iters", False))
        assert verdict == nr.VERDICT_RETRY
        assert out["retry_remaining"] == 2

    def test_events_emitted(self):
        events = []

        def emit(etype, payload):
            events.append((etype, payload))

        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 1})
        nr.run(node, pack, emit=emit, agent_fn=_agent_always("confirmed", True))

        etypes = [e[0] for e in events]
        assert "dag.node.started" in etypes
        assert "dag.node.done" in etypes

    def test_escalate_emits_failed_event(self):
        events = []

        def emit(etype, payload):
            events.append((etype, payload))

        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 1})
        nr.run(node, pack, emit=emit, agent_fn=_agent_always("max_iters", False))

        etypes = [e[0] for e in events]
        assert "dag.node.failed" in etypes


# ---------------------------------------------------------------------------
# dag_planner.md validation helpers
# ---------------------------------------------------------------------------

class TestDagPlannerPrompt:
    def test_dag_planner_md_exists(self):
        prompt_path = (
            Path(__file__).parent.parent
            / "agent" / "prompts" / "dag_planner.md"
        )
        assert prompt_path.exists(), f"dag_planner.md not found at {prompt_path}"

    def test_dag_planner_md_contains_schema(self):
        prompt_path = (
            Path(__file__).parent.parent
            / "agent" / "prompts" / "dag_planner.md"
        )
        text = prompt_path.read_text()
        assert "aicad.dag/1" in text
        assert "retry_budget" in text
        assert "≤ 20 nodes" in text or "<= 20" in text or "20 nodes" in text

    def test_worked_example_validates(self):
        """The soil-moisture-sensor example in dag_planner.md must pass schema validation."""
        import json
        import re

        prompt_path = (
            Path(__file__).parent.parent
            / "agent" / "prompts" / "dag_planner.md"
        )
        text = prompt_path.read_text()
        # Extract JSON blocks from the worked example section.
        # The largest JSON block containing "aicad.dag/1" is the worked example.
        json_re = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
        candidates = [m.group(1).strip() for m in json_re.finditer(text)]
        dag_jsons = []
        for c in candidates:
            try:
                obj = json.loads(c)
                if obj.get("schema") == "aicad.dag/1":
                    dag_jsons.append(obj)
            except Exception:
                pass
        assert dag_jsons, "No aicad.dag/1 graph JSON found in dag_planner.md"
        # Validate the first one (the soil-moisture example).
        validate_graph(dag_jsons[0])


# ---------------------------------------------------------------------------
# Retry cycle (DoD requirement): graph with a cycle terminates via budget
# ---------------------------------------------------------------------------

class TestRetryCycleTermination:
    """Core DoD: a graph with a retry cycle terminates when the budget is exhausted."""

    def _retry_graph(self) -> dict:
        """Two-node graph: n_design → n_verify → n_design (retry back-edge, budget=2).
        A third node n_report receives the forward exit from n_verify.

        n_design: always "done" (succeeds)
        n_verify: returns "max_iters" on first N calls, then "confirmed"
        """
        return {
            "schema": "aicad.dag/1",
            "id": "retry-cycle-test-graph",
            "name": "retry-cycle-test",
            "nodes": [
                {
                    "id": "n_design",
                    "type": "design",
                    "name": "Design",
                    "inputs": {"request": "make a part"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                },
                {
                    "id": "n_verify",
                    "type": "verify",
                    "name": "Verify",
                    "inputs": {"request": "check the part"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                },
                {
                    "id": "n_report",
                    "type": "report",
                    "name": "Report",
                    "inputs": {},
                    "on_fail": "skip",
                    "autonomy": "auto",
                },
            ],
            "edges": [
                {"from": "n_design", "to": "n_verify"},
                # Back-edge: verify can retry by looping back to design (budget=2)
                {"from": "n_verify", "to": "n_design", "retry_budget": 2},
                # Forward exit: when verify is done, proceed to report
                {"from": "n_verify", "to": "n_report"},
            ],
        }

    def test_retry_cycle_terminates_by_budget(self, tmp_path: Path) -> None:
        """A graph with a retry back-edge must terminate when budget is exhausted.

        The verify node always returns "max_iters" (retry verdict).  After
        decrementing from 2 → 1 → 0, the executor should escalate (not loop
        forever) and the run should not raise GraphRecursionError.
        """
        graph = self._retry_graph()
        call_counts: dict[str, int] = {}

        def counting_agent(request: str, pack: dict) -> Any:
            nid = pack.get("node_id", "?")
            call_counts[nid] = call_counts.get(nid, 0) + 1
            # n_verify always retries (to exercise budget exhaustion)
            if nid == "n_verify":
                return _fake_run("max_iters", False)
            return _fake_run("confirmed", True)

        result = executor.run(graph, runs_dir=tmp_path, agent_fn=counting_agent)

        # Must not loop forever — verify budget is 2
        assert call_counts.get("n_verify", 0) <= 3, (
            f"n_verify called {call_counts.get('n_verify', 0)} times — budget not honoured"
        )
        # Run must finish (not hang)
        assert result["status"] in ("done", "failed", "paused")

    def test_retry_cycle_succeeds_when_fixed(self, tmp_path: Path) -> None:
        """If verify eventually returns 'confirmed', the cycle exits cleanly."""
        graph = self._retry_graph()

        # n_verify: retries once, then succeeds.
        verify_calls = [0]

        def agent_fn(request: str, pack: dict) -> Any:
            nid = pack.get("node_id", "?")
            if nid == "n_verify":
                verify_calls[0] += 1
                if verify_calls[0] == 1:
                    return _fake_run("max_iters", False)   # first call: retry
                return _fake_run("confirmed", True)         # second call: done
            return _fake_run("confirmed", True)

        result = executor.run(graph, runs_dir=tmp_path, agent_fn=agent_fn)
        # Verify was called twice: once retry, once success
        assert verify_calls[0] == 2
        # n_report should have run (forward exit taken after verify succeeds)
        assert result["status"] in ("done", "paused")

    def test_no_cycle_graph_still_works(self, tmp_path: Path) -> None:
        """A plain linear graph with no back-edges continues to run correctly."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "linear-graph",
            "name": "linear",
            "nodes": [
                {"id": "n1", "type": "plan", "name": "Plan",
                 "inputs": {"request": "plan it"}, "on_fail": "abort", "autonomy": "auto"},
                {"id": "n2", "type": "report", "name": "Report",
                 "inputs": {}, "on_fail": "skip", "autonomy": "auto"},
            ],
            "edges": [{"from": "n1", "to": "n2"}],
        }
        result = executor.run(
            graph, runs_dir=tmp_path,
            agent_fn=_agent_always("confirmed", True),
        )
        assert result["status"] == "done"
        assert result["nodes"]["n1"]["status"] == "done"
        assert result["nodes"]["n2"]["status"] == "done"


# ---------------------------------------------------------------------------
# executor.run() passes agent_fn through to graph_loader (wiring test)
# ---------------------------------------------------------------------------

class TestExecutorWiring:
    def test_executor_run_with_agent_fn(self, tmp_path: Path) -> None:
        """executor.run() accepts and threads agent_fn into the node handlers."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "wiring-test",
            "name": "wiring",
            "nodes": [
                {"id": "n1", "type": "design", "name": "Design",
                 "inputs": {"request": "make something"}, "on_fail": "pause",
                 "autonomy": "auto"},
            ],
            "edges": [],
        }
        called = [False]

        def my_agent(request: str, pack: dict) -> Any:
            called[0] = True
            return _fake_run("confirmed", True)

        result = executor.run(graph, runs_dir=tmp_path, agent_fn=my_agent)
        assert called[0], "agent_fn was not called"
        assert result["status"] == "done"

    def test_events_from_executor_include_node_events(self, tmp_path: Path) -> None:
        """node_runner events are forwarded through executor's emit callback."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "event-test",
            "name": "events",
            "nodes": [
                {"id": "n1", "type": "design", "name": "D",
                 "inputs": {"request": "x"}, "on_fail": "pause", "autonomy": "auto"},
            ],
            "edges": [],
        }
        events = []

        def emit(etype, payload):
            events.append(etype)

        executor.run(
            graph, runs_dir=tmp_path, emit=emit,
            agent_fn=_agent_always("confirmed", True),
        )
        assert "dag.run.started" in events
        assert "dag.node.started" in events
        assert "dag.node.done" in events
        assert "dag.run.done" in events


# ---------------------------------------------------------------------------
# §14 narration fields in executor.run() return dict
# ---------------------------------------------------------------------------

class TestSection14NarrationFields:
    """executor.run() must surface §14 fields so describe_project(run=...) works."""

    def test_name_in_result(self, tmp_path: Path) -> None:
        """executor.run() return dict includes 'name' from the graph document."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "narration-test",
            "name": "soil-moisture-sensor",
            "nodes": [
                {"id": "n1", "type": "report", "name": "Report",
                 "inputs": {}, "on_fail": "skip", "autonomy": "auto"},
            ],
            "edges": [],
        }
        result = executor.run(graph, runs_dir=tmp_path,
                              agent_fn=_agent_always("confirmed", True))
        assert result["name"] == "soil-moisture-sensor"

    def test_open_questions_empty_on_clean_run(self, tmp_path: Path) -> None:
        """A run with no escalations has an empty open_questions list."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "narration-clean",
            "name": "clean",
            "nodes": [
                {"id": "n1", "type": "design", "name": "D",
                 "inputs": {"request": "x"}, "on_fail": "pause", "autonomy": "auto"},
            ],
            "edges": [],
        }
        result = executor.run(graph, runs_dir=tmp_path,
                              agent_fn=_agent_always("confirmed", True))
        assert "open_questions" in result
        assert result["open_questions"] == []

    def test_open_questions_populated_on_escalation(self, tmp_path: Path) -> None:
        """An escalation (budget exhausted) appends an entry to open_questions."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "narration-escalation",
            "name": "escalation-test",
            "nodes": [
                {"id": "n1", "type": "design", "name": "Design",
                 "inputs": {"request": "x"}, "on_fail": "pause", "autonomy": "auto"},
                {"id": "n_gate", "type": "gate", "name": "Gate",
                 "inputs": {}, "on_fail": "pause", "autonomy": "approve"},
            ],
            "edges": [{"from": "n1", "to": "n_gate"}],
        }
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = executor.run(
                graph, runs_dir=tmp_path,
                # n1 always escalates (failed verdict, budget exhausted)
                agent_fn=_agent_always("failed", False),
            )
        assert "open_questions" in result
        # n1 should have escalated and added an entry
        assert any(q.get("node") == "n1" for q in result["open_questions"]), (
            f"Expected n1 escalation in open_questions, got: {result['open_questions']}"
        )
