"""Test suite for node_runner.py and context_pack.py.

All tests use injected mock agent functions (no live LLM, no engine build).

Run with:
    ~/.local/share/Chudware/mcp-venv/bin/pytest services/dag/test_node_runner.py -q

Or with real engine available:
    CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so \\
        ~/.local/share/Chudware/mcp-venv/bin/pytest services/dag/test_node_runner.py -q
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# Canonical imports keep the repo root on ``sys.path`` and never shadow the
# installed MCP SDK with ``services/mcp`` as a top-level ``mcp`` package.
from services.dag import context_pack as cp
from services.dag import node_runner as nr
from services.dag import executor, graph_loader
from services.dag.graph_loader import (
    GraphValidationError,
    build_state_graph,
    validate_graph,
)


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

    def test_phase_promotes_from_node_inputs_for_skill_and_budget_policy(self):
        node = _minimal_node(
            inputs={
                "request": "verify the bracket",
                "role": "reviewer",
                "phase": "deliver",
            }
        )
        pack = cp.assemble(node, {})
        assert pack["phase"] == "deliver"
        assert pack["role_id"] == "reviewer"

    def test_blocked_and_escalated_stop_reasons_never_map_done(self):
        assert nr._map_verdict("blocked", False) == nr.VERDICT_RETRY
        assert nr._map_verdict("escalated", False) == nr.VERDICT_ESCALATE

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

    def test_retryable_context_overflow_retries_within_budget(self):
        result = _fake_run("context_overflow", False)
        result.result = {"retryable": True}
        assert nr._map_verdict(result.stop_reason, result.success) == nr.VERDICT_RETRY

        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 2})
        verdict, out = nr.run(node, pack, agent_fn=lambda req, ctx: result)
        assert verdict == nr.VERDICT_RETRY
        assert out["retry_remaining"] == 1

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

    def test_non_retry_escalation_event_keeps_real_cause(self):
        events = []
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 3})
        nr.run(
            node,
            pack,
            emit=lambda kind, payload: events.append((kind, payload)),
            agent_fn=_agent_always("no_code", False),
        )

        failure = next(p for kind, p in events if kind == "dag.node.failed")
        assert failure["error"] == "escalated: no_code"
        assert "budget exhausted" not in failure["error"]

    def test_retry_exhaustion_event_says_retry_budget_exhausted(self):
        events = []
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 1})
        nr.run(
            node,
            pack,
            emit=lambda kind, payload: events.append((kind, payload)),
            agent_fn=_agent_always("max_iters", False),
        )

        failure = next(p for kind, p in events if kind == "dag.node.failed")
        assert failure["error"] == ("escalated: max_iters (retry budget exhausted)")


# ---------------------------------------------------------------------------
# dag_planner.md validation helpers
# ---------------------------------------------------------------------------


class TestDagPlannerPrompt:
    def test_dag_planner_md_exists(self):
        prompt_path = (
            Path(__file__).parent.parent / "agent" / "prompts" / "dag_planner.md"
        )
        assert prompt_path.exists(), f"dag_planner.md not found at {prompt_path}"

    def test_dag_planner_md_contains_schema(self):
        prompt_path = (
            Path(__file__).parent.parent / "agent" / "prompts" / "dag_planner.md"
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
            Path(__file__).parent.parent / "agent" / "prompts" / "dag_planner.md"
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
                    return _fake_run("max_iters", False)  # first call: retry
                return _fake_run("confirmed", True)  # second call: done
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
                {
                    "id": "n1",
                    "type": "plan",
                    "name": "Plan",
                    "inputs": {"request": "plan it"},
                    "on_fail": "abort",
                    "autonomy": "auto",
                },
                {
                    "id": "n2",
                    "type": "report",
                    "name": "Report",
                    "inputs": {},
                    "on_fail": "skip",
                    "autonomy": "auto",
                },
            ],
            "edges": [{"from": "n1", "to": "n2"}],
        }
        result = executor.run(
            graph,
            runs_dir=tmp_path,
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
                {
                    "id": "n1",
                    "type": "design",
                    "name": "Design",
                    "inputs": {"request": "make something"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                },
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
                {
                    "id": "n1",
                    "type": "design",
                    "name": "D",
                    "inputs": {"request": "x"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                },
            ],
            "edges": [],
        }
        events = []

        def emit(etype, payload):
            events.append(etype)

        executor.run(
            graph,
            runs_dir=tmp_path,
            emit=emit,
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
                {
                    "id": "n1",
                    "type": "report",
                    "name": "Report",
                    "inputs": {},
                    "on_fail": "skip",
                    "autonomy": "auto",
                },
            ],
            "edges": [],
        }
        result = executor.run(
            graph, runs_dir=tmp_path, agent_fn=_agent_always("confirmed", True)
        )
        assert result["name"] == "soil-moisture-sensor"

    def test_open_questions_empty_on_clean_run(self, tmp_path: Path) -> None:
        """A run with no escalations has an empty open_questions list."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "narration-clean",
            "name": "clean",
            "nodes": [
                {
                    "id": "n1",
                    "type": "design",
                    "name": "D",
                    "inputs": {"request": "x"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                },
            ],
            "edges": [],
        }
        result = executor.run(
            graph, runs_dir=tmp_path, agent_fn=_agent_always("confirmed", True)
        )
        assert "open_questions" in result
        assert result["open_questions"] == []

    def test_open_questions_populated_on_escalation(self, tmp_path: Path) -> None:
        """An escalation (budget exhausted) appends an entry to open_questions."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "narration-escalation",
            "name": "escalation-test",
            "nodes": [
                {
                    "id": "n1",
                    "type": "design",
                    "name": "Design",
                    "inputs": {"request": "x"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                },
                {
                    "id": "n_gate",
                    "type": "gate",
                    "name": "Gate",
                    "inputs": {},
                    "on_fail": "pause",
                    "autonomy": "approve",
                },
            ],
            "edges": [{"from": "n1", "to": "n_gate"}],
        }
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = executor.run(
                graph,
                runs_dir=tmp_path,
                # n1 always escalates (failed verdict, budget exhausted)
                agent_fn=_agent_always("failed", False),
            )
        assert "open_questions" in result
        # n1 should have escalated and added an entry
        assert any(q.get("node") == "n1" for q in result["open_questions"]), (
            f"Expected n1 escalation in open_questions, got: {result['open_questions']}"
        )


# ---------------------------------------------------------------------------
# Role card wiring: a node's ``role:`` field applies the card
# ---------------------------------------------------------------------------


class TestRoleCardWiring:
    """A DAG node may carry a ``role:`` field naming a predefined role card
    (services/agent/roles/*.yaml). node_runner loads it, applies its tool
    allowlist to the MCP registry, and injects its system prompt + tools into
    the context pack. Missing/unknown roles degrade to default behaviour."""

    def _captured_pack(self):
        captured = {}

        def _fn(request: str, pack: dict) -> Any:
            captured.update(pack)
            return _fake_run("confirmed", True)

        return _fn, captured

    def test_role_node_injects_prompt_and_tools(self):
        from services.mcp import tool_registry as reg

        reg.reset_policy()
        agent_fn, captured = self._captured_pack()
        node = {
            "id": "n1",
            "type": "design",
            "name": "Sketch",
            "role": "sketcher",
            "inputs": {"request": "draw a plate"},
        }
        verdict, out = nr.run(
            node,
            {"node_inputs": node["inputs"], "retry_remaining": 1},
            agent_fn=agent_fn,
        )
        assert verdict == "done"
        assert captured.get("role_id") == "sketcher"
        assert "SKETCHER" in captured.get("role_system_prompt", "")
        # The role scope is visible to the turn but restored afterwards; no
        # process-global registry mutation may leak out of node execution.
        assert "sketch_health" in captured.get("allowed_tools", [])
        assert "cam_post" not in captured.get("allowed_tools", [])
        assert reg.is_allowed("cam_post")
        reg.reset_policy()

    def test_unknown_role_degrades(self):
        agent_fn, captured = self._captured_pack()
        node = {
            "id": "n1",
            "type": "design",
            "name": "X",
            "role": "does_not_exist",
            "inputs": {"request": "x"},
        }
        verdict, out = nr.run(
            node,
            {"node_inputs": node["inputs"], "retry_remaining": 1},
            agent_fn=agent_fn,
        )
        assert verdict == "done"
        assert "role_id" not in captured  # no card applied, ran with defaults

    def test_no_role_field_unchanged(self):
        agent_fn, captured = self._captured_pack()
        node = {"id": "n1", "type": "design", "name": "X", "inputs": {"request": "x"}}
        verdict, out = nr.run(
            node,
            {"node_inputs": node["inputs"], "retry_remaining": 1},
            agent_fn=agent_fn,
        )
        assert verdict == "done"
        assert "role_id" not in captured


# ---------------------------------------------------------------------------
# Default agent_fn: ledger threading + tier budget seam
# ---------------------------------------------------------------------------


class TestDefaultAgentFnSeams:
    @staticmethod
    def _capture_role_turn(monkeypatch, scripted=None):
        from services.agent import chudcode_roles

        monkeypatch.setattr(nr, "_ADVISOR_ENABLED", False)

        calls = []
        replies = iter(scripted or [])

        def fake_run_role_turn(request, role, **kwargs):
            calls.append({"request": request, "role": role, **kwargs})
            try:
                reply = next(replies)
            except StopIteration:
                refusal = {
                    "reason": "single coordinated lane",
                    "domains_considered": ["geometry", "planning"],
                    "why_flat": "the fixture has no independent executable slice",
                }
                reply = {
                    "success": True,
                    "stop_reason": "completed",
                    "result": {
                        "handoff": {
                            "status": "done",
                            "summary": "node complete",
                            "outputs": {"typed_refusal": refusal},
                        },
                        "typed_refusal": refusal,
                    },
                    "run_id": kwargs.get("run_id"),
                    "session_id": f"session-{kwargs.get('parent_id')}",
                    "parent_id": kwargs.get("parent_id"),
                    "cancelled": False,
                    "offline": kwargs.get("options", {}).get("offline", False),
                }
            return reply

        monkeypatch.setattr(chudcode_roles, "run_role_turn", fake_run_role_turn)
        return calls

    def test_build_and_swarm_nodes_use_chudcode_with_distinct_stable_lineage(
        self,
        monkeypatch,
    ):
        calls = self._capture_role_turn(monkeypatch)
        base = {
            "run_id": "run-build",
            "session_id": "chat-root",
            "retry_remaining": 2,
            "tier": "frontier",
        }
        build = _minimal_node(
            "build-a",
            "design",
            tools=["new_part"],
            inputs={"request": "build", "role": "feature_modeler", "tier": "frontier"},
        )
        swarm = _minimal_node(
            "swarm-b",
            "plan",
            tools=[],
            inputs={
                "request": "coordinate",
                "role": "orchestrator",
                "tier": "frontier",
            },
        )

        for node in (build, swarm):
            pack = {**cp.assemble(node, {"retry_remaining": 2}), **base}
            verdict, out = nr.run(node, pack)
            assert verdict == nr.VERDICT_DONE
            assert out["status"] == "done"

        assert [call["role"] for call in calls] == ["feature_modeler", "orchestrator"]
        assert calls[0]["session_id"] == calls[1]["session_id"] == "chat-root"
        assert calls[0]["run_id"] != calls[1]["run_id"]
        assert all(call["run_id"].startswith("dag-node-") for call in calls)
        assert calls[0]["parent_id"] != calls[1]["parent_id"]
        assert calls[0]["options"]["allowed_tools"] == ["new_part"]
        assert calls[1]["options"]["allowed_tools"] == []
        assert all(call["tier"] == "frontier" for call in calls)
        assert all(call["capability"] is not None for call in calls)
        assert all(call["close_session"] is False for call in calls)

    def test_explicit_offline_node_uses_only_role_helper_offline_path(
        self,
        monkeypatch,
    ):
        calls = self._capture_role_turn(monkeypatch)
        offline_provider = object()
        result = nr._default_agent_fn(
            "offline build",
            {
                "run_id": "offline-run",
                "node_id": "offline-node",
                "role_id": "feature_modeler",
                "node_inputs": {"offline": True},
                "allowed_tools": [],
            },
            provider=offline_provider,
        )

        assert result.success is True
        assert calls[0]["options"]["offline"] is True
        assert calls[0]["options"]["offline_provider"] is offline_provider

    def test_online_node_never_passes_injected_legacy_provider(
        self,
        monkeypatch,
    ):
        calls = self._capture_role_turn(monkeypatch)
        nr._default_agent_fn(
            "online build",
            {
                "run_id": "online-run",
                "node_id": "online-node",
                "role_id": "feature_modeler",
                "node_inputs": {},
                "allowed_tools": [],
            },
            provider=object(),
        )
        assert calls[0]["options"]["offline"] is False
        assert "offline_provider" not in calls[0]["options"]

    def test_chudcode_cancel_and_retry_keep_dag_event_and_budget_contract(
        self,
        monkeypatch,
    ):
        calls = self._capture_role_turn(
            monkeypatch,
            scripted=[
                {
                    "success": False,
                    "stop_reason": "provider_error",
                    "result": {"retryable": True, "error": "HTTP 429"},
                    "session_id": "retry-session",
                    "cancelled": False,
                },
                {
                    "success": False,
                    "stop_reason": "cancelled",
                    "result": {"error": "cancelled"},
                    "session_id": "cancel-session",
                    "cancelled": True,
                },
            ],
        )
        node = _minimal_node(inputs={"request": "build", "role": "feature_modeler"})
        events = []
        pack = {**cp.assemble(node, {"retry_remaining": 2}), "run_id": "run-retry"}
        verdict, out = nr.run(
            node, pack, emit=lambda kind, payload: events.append(kind)
        )
        assert verdict == nr.VERDICT_RETRY
        assert out["retry_remaining"] == 1
        assert events == [
            "dag.node.started",
            "agent.model_assignment",
            "dag.node.paused",
        ]

        events.clear()
        verdict, out = nr.run(
            node, pack, emit=lambda kind, payload: events.append(kind)
        )
        assert verdict == nr.VERDICT_ESCALATE
        assert out["data"]["stop_reason"] == "cancelled"
        assert events == [
            "dag.node.started",
            "agent.model_assignment",
            "dag.node.failed",
        ]
        assert len(calls) == 2

    def test_chudcode_calls_preserve_doc_id_and_checkpoint_result_shape(
        self,
        monkeypatch,
    ):
        self._capture_role_turn(
            monkeypatch,
            scripted=[
                {
                    "success": True,
                    "stop_reason": "completed",
                    "result": "built bracket",
                    "calls": [
                        {
                            "tool": "new_part",
                            "args": {"name": "bracket"},
                            "ok": True,
                            "result": {"ok": True, "doc_id": "doc-created"},
                        }
                    ],
                    "tool_calls": 1,
                    "session_id": "build-session",
                    "parent_id": "run-build:build",
                    "cancelled": False,
                    "offline": False,
                }
            ],
        )
        node = _minimal_node(
            "build",
            "design",
            tools=["new_part"],
            inputs={"request": "build", "role": "feature_modeler"},
        )
        pack = {**cp.assemble(node, {"retry_remaining": 1}), "run_id": "run-build"}
        verdict, out = nr.run(node, pack)

        assert verdict == nr.VERDICT_DONE
        assert out["doc_id"] == "doc-created"
        assert out["data"]["agent_result"]["calls"] == [
            {"tool": "new_part", "ok": True}
        ]
        assert out["data"]["agent_result"]["tool_calls"] == 1

    def test_chudcode_session_lineage_survives_executor_checkpoint(
        self,
        monkeypatch,
        tmp_path,
    ):
        self._capture_role_turn(monkeypatch)
        monkeypatch.setattr(nr, "_ADVISOR_ENABLED", False)
        graph = {
            "schema": "aicad.dag/1",
            "id": "chudcode-checkpoint",
            "name": "checkpoint",
            "nodes": [
                {
                    "id": "build",
                    "type": "design",
                    "name": "Build",
                    "role": "feature_modeler",
                    "inputs": {"request": "build"},
                    "tools": [],
                    "on_fail": "pause",
                    "autonomy": "auto",
                }
            ],
            "edges": [],
        }
        result = executor.run(
            graph,
            run_id="checkpoint-run",
            runs_dir=tmp_path,
        )
        agent_result = result["nodes"]["build"]["output"]["data"]["agent_result"]
        assert agent_result["session_id"] == "session-checkpoint-run:build"
        assert agent_result["parent_id"] == "checkpoint-run:build"
        assert (tmp_path / "checkpoint-run.db").exists()


class TestDispatchLaneParity:
    """Compiled DAGs keep the shared retry predicate used by delegated roles."""

    def test_retryable_verdict_routes_through_the_shared_predicate(
        self,
        monkeypatch,
    ):
        """Neutering ``mission_dispatch.retryable_child_failure`` must strip this
        lane's retry upgrade too — proof the two lanes agree on WHICH provider
        faults are transient instead of each keeping a private rule."""
        from services.agent import mission_dispatch as md

        result = _fake_run("provider_error", False)
        result.result = {"retryable": True, "error": "HTTP 429"}
        node = _minimal_node()

        pack = cp.assemble(node, {"retry_remaining": 2})
        verdict, _out = nr.run(node, pack, agent_fn=lambda req, ctx: result)
        assert verdict == nr.VERDICT_RETRY

        monkeypatch.setattr(md, "retryable_child_failure", lambda _r: False)
        pack = cp.assemble(node, {"retry_remaining": 2})
        verdict, _out = nr.run(node, pack, agent_fn=lambda req, ctx: result)
        assert verdict == nr.VERDICT_ESCALATE

    def test_non_retryable_provider_error_still_escalates(self):
        """A 404 dead route / real auth failure is stamped retryable=False and
        must NOT consume a retry: re-running fails identically."""
        result = _fake_run("provider_error", False)
        result.result = {"retryable": False, "error": "HTTP 404 no such model"}
        node = _minimal_node()
        pack = cp.assemble(node, {"retry_remaining": 2})
        verdict, out = nr.run(node, pack, agent_fn=lambda req, ctx: result)
        assert verdict == nr.VERDICT_ESCALATE
        assert out["retry_remaining"] == 2


class TestMissionRoleCardWiring:
    """Real-run regressions: mission-graph nodes carry role in inputs.role
    (mission_template._node); the runner must resolve the card from there and must
    apply the card allowlist when the context pack's allowed_tools is OPEN (None).
    Both were silently broken (node['role'] only + setdefault on a present key) —
    the real C1 run showed model tool-name hallucination as the direct symptom."""

    def _capture_pack(self):
        seen = {}

        def _agent_fn(request, context_pack):
            seen["request"] = request
            seen["pack"] = context_pack
            return _fake_run("confirmed", True)

        return seen, _agent_fn

    def test_role_resolves_from_inputs_role(self):
        node = {
            "id": "n1",
            "type": "design",
            "name": "BUILD DL1",
            "inputs": {"role": "feature_modeler"},
        }
        pack = cp.assemble(node, {})
        seen, agent_fn = self._capture_pack()
        verdict, _ = nr.run(node, pack, agent_fn=agent_fn)
        assert verdict == "done"
        assert seen["pack"].get("role_id") == "feature_modeler"
        assert seen["pack"].get("role_system_prompt"), "card prompt must be applied"

    def test_card_allowlist_fills_open_allowed_tools(self):
        from services.agent.roles import load_role

        node = {
            "id": "n1",
            "type": "design",
            "name": "BUILD DL1",
            "inputs": {"role": "feature_modeler"},
        }
        pack = cp.assemble(node, {})
        assert pack["allowed_tools"] is None  # open before the card applies
        seen, agent_fn = self._capture_pack()
        nr.run(node, pack, agent_fn=agent_fn)
        assert seen["pack"]["allowed_tools"] == list(
            load_role("feature_modeler").concrete_tools
        )

    def test_explicit_node_tools_still_win_over_card(self):
        node = {
            "id": "n1",
            "type": "design",
            "name": "BUILD DL1",
            "tools": ["measure_part"],
            "inputs": {"role": "feature_modeler"},
        }
        pack = cp.assemble(node, {})
        seen, agent_fn = self._capture_pack()
        nr.run(node, pack, agent_fn=agent_fn)
        assert seen["pack"]["allowed_tools"] == ["measure_part"]


class TestMissionNodeRequests:
    """Mission node requests must EMBED the manifest content (verbatim
    request + deliverable + acceptance criteria), not reference an absent brief."""

    def _manifest(self):
        return {
            "schema": "aicad.deliverables/1",
            "request_verbatim": "make a flanged bushing with a d20 sliding-fit bore",
            "deliverables": [
                {
                    "id": "DL1",
                    "kind": "part",
                    "name": "flanged bushing",
                    "description": "bushing with flange",
                    "acceptance_criteria": [
                        {
                            "id": "AC1",
                            "text": "bore d20 H7",
                            "check": {
                                "tool": "fit_report",
                                "pass_rule": "clearance in [0.007,0.041]",
                            },
                        },
                    ],
                    "depends_on": [],
                    "status": "pending",
                }
            ],
            "ambiguities": [],
            "assumptions": [],
        }

    def test_build_node_request_carries_manifest_content(self):
        from services.dag.mission_template import build_mission_graph

        graph = build_mission_graph(self._manifest(), graph_id="t")
        build = next(n for n in graph["nodes"] if n["id"] == "n_build_DL1")
        req = build["inputs"]["request"]
        assert "make a flanged bushing" in req  # verbatim mission request
        assert "AC1" in req and "fit_report" in req  # the criterion + named tool
        assert "clearance in [0.007,0.041]" in req  # the pass rule

    def test_every_mission_node_has_a_request(self):
        from services.dag.mission_template import build_mission_graph

        graph = build_mission_graph(self._manifest(), graph_id="t", org_layer=True)
        for n in graph["nodes"]:
            assert n["inputs"].get("request"), f"{n['id']} has no request"


def test_role_policy_is_scoped_to_the_turn():
    """Gate-sweep regression: applying a role card's allowlist must NOT
    leak into the process-global tool_registry after the node turn returns
    (it broke ~78 unrelated cad/gsl tool tests in the combined sweep)."""
    from services.mcp import tool_registry as reg

    reg.reset_policy()
    assert reg.is_allowed("export_part")  # default: everything allowed
    node = {
        "id": "n1",
        "type": "design",
        "name": "BUILD DL1",
        "inputs": {"role": "feature_modeler"},
    }
    nr.run(
        node,
        cp.assemble(node, {}),
        agent_fn=lambda req, pack: _fake_run("confirmed", True),
    )
    # export_part is NOT in feature_modeler's allowlist — if the policy leaked,
    # this dispatch-time check now refuses it.
    assert reg.is_allowed("export_part"), "role tool policy leaked past the node turn"


# ---------------------------------------------------------------------------
# Tool-schema injection + run-scoped dispatcher wiring
# ---------------------------------------------------------------------------


class TestCompiledDagPolicy:
    @pytest.mark.parametrize(
        ("role", "mandated_calls"),
        [
            # ECAD base chain is 32; antenna design/analyse/provision note add 3.
            ("ecad_engineer", 1 + 1 + 7 + 7 + 6 + 2 + 1 + 1 + 1 + 1 + 1 + 1 + 2 + 3),
            # Four sketch-backed nodes have five mandatory calls apiece.
            ("model3d", 5 * 4 + 1 + 1 + 1 + 1),
            ("feature_modeler", 18),
            # Nine researched ledger/spec entries require compute + note.
            ("spec_writer", 9 * 2),
        ],
    )
    def test_role_soft_floor_covers_card_mandated_chain(
        self,
        role,
        mandated_calls,
    ):
        from services.agent import tool_budget
        from services.agent.roles import load_role

        card = load_role(role)
        actual = tool_budget.floor_for(
            role,
            card.concrete_tools,
            phase=None,
            base=1,
        )
        assert actual >= mandated_calls
