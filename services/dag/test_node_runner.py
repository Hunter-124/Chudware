"""Test suite for node_runner.py and context_pack.py — T-A2.

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
from services.dag.graph_loader import GraphValidationError, build_state_graph, validate_graph


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
        node = _minimal_node(inputs={
            "request": "verify the bracket",
            "role": "reviewer",
            "phase": "deliver",
        })
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
        assert failure["error"] == (
            "escalated: max_iters (retry budget exhausted)"
        )


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


# ---------------------------------------------------------------------------
# Role card wiring (T-W7): a node's ``role:`` field applies the card
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
        node = {"id": "n1", "type": "design", "name": "Sketch",
                "role": "sketcher", "inputs": {"request": "draw a plate"}}
        verdict, out = nr.run(node, {"node_inputs": node["inputs"],
                                     "retry_remaining": 1}, agent_fn=agent_fn)
        assert verdict == "done"
        assert captured.get("role_id") == "sketcher"
        assert "SKETCHER" in captured.get("role_system_prompt", "")
        # the sketcher card's allowlist is now bound in the registry
        assert reg.is_allowed("sketch_health")
        assert not reg.is_allowed("cam_post")
        assert "sketch_health" in captured.get("allowed_tools", [])
        reg.reset_policy()

    def test_unknown_role_degrades(self):
        agent_fn, captured = self._captured_pack()
        node = {"id": "n1", "type": "design", "name": "X",
                "role": "does_not_exist", "inputs": {"request": "x"}}
        verdict, out = nr.run(node, {"node_inputs": node["inputs"],
                                     "retry_remaining": 1}, agent_fn=agent_fn)
        assert verdict == "done"
        assert "role_id" not in captured  # no card applied, ran with defaults

    def test_no_role_field_unchanged(self):
        agent_fn, captured = self._captured_pack()
        node = {"id": "n1", "type": "design", "name": "X",
                "inputs": {"request": "x"}}
        verdict, out = nr.run(node, {"node_inputs": node["inputs"],
                                     "retry_remaining": 1}, agent_fn=agent_fn)
        assert verdict == "done"
        assert "role_id" not in captured


# ---------------------------------------------------------------------------
# T-W30 finisher — default agent_fn: ledger threading + tier budget seam
# ---------------------------------------------------------------------------

class _TurnScriptedProvider:
    """Do one real tool call, then finish (agent_turn protocol).

    A BUILD brief that finishes with zero real tool calls is an unearned finish
    and the harness refuses it (browsing packs does not count either). These
    tests are about provider/tier resolution, so the scripted turn performs one
    harmless ledger write to earn its handoff.
    """
    name = "scripted"

    def __init__(self):
        self._turns = 0

    def complete(self, system, messages, max_tokens=None):
        self._turns += 1
        if self._turns == 1:
            return ('```json\n{"tool": "ledger_note", "args": {"kind": "note", '
                    '"payload": {"text": "scripted"}}}\n```')
        return '```json\n{"done": true, "handoff": {"summary": "done"}}\n```'


class TestDefaultAgentFnSeams:
    def test_make_default_agent_fn_threads_ledger_into_turn(self, monkeypatch):
        """loop.run_mission binds its ledger via make_default_agent_fn; the turn's
        events must land on that ledger (DoD: ledger events for every turn)."""
        from services.agent import config as agent_config

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: _TurnScriptedProvider())

        events = []

        class _ListLedger:
            def append(self, actor, kind, payload):
                events.append({"actor": actor, "kind": kind, "payload": payload})

        agent_fn = nr.make_default_agent_fn(ledger=_ListLedger())
        res = agent_fn("do the node", {"node_name": "n_build", "allowed_tools": []})
        assert res.success is True
        assert any(e["kind"] == "agent_turn" for e in events), events
        assert all(e["actor"] == "n_build" for e in events)

    def test_default_agent_fn_passes_tier_generation_budget(self, monkeypatch):
        """CHUDWARE_TIER set → run_turn gets that tier's generation_headroom as
        max_tokens (bounded by tier budgets, aicad.effort/1 seam)."""
        from services.agent import config as agent_config
        from services.agent import agent_turn as at
        from services.agent.tiers import load_tier

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: _TurnScriptedProvider())
        monkeypatch.setenv("CHUDWARE_TIER", "local-24k")

        seen = {}
        real_run_turn = at.run_turn

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real_run_turn(*args, **kwargs)

        monkeypatch.setattr(at, "run_turn", _spy)
        nr._default_agent_fn("go", {"node_name": "n1"})
        assert seen.get("max_tokens") == load_tier("local-24k").generation_headroom

    def test_default_agent_fn_no_tier_uses_shared_child_headroom(self, monkeypatch):
        """Untiered DAG children get the delegate lane's completion headroom."""
        from services.agent import config as agent_config
        from services.agent import agent_turn as at
        from services.agent.mission_dispatch import LINEAR_ROLE_MAX_OUTPUT_TOKENS

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: _TurnScriptedProvider())
        monkeypatch.delenv("CHUDWARE_TIER", raising=False)

        seen = {}
        real_run_turn = at.run_turn

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real_run_turn(*args, **kwargs)

        monkeypatch.setattr(at, "run_turn", _spy)
        nr._default_agent_fn("go", {"node_name": "n1"})
        assert seen["max_tokens"] == LINEAR_ROLE_MAX_OUTPUT_TOKENS

    def test_node_context_tier_beats_environment_for_all_turn_policy(
        self, monkeypatch,
    ):
        from services.agent import agent_turn as at
        from services.agent.tiers import load_tier

        monkeypatch.setenv("CHUDWARE_TIER", "local-24k")
        seen = {}

        def _spy(*args, **kwargs):
            seen.update(kwargs)

            class _Result:
                success = True
                stop_reason = "confirmed"
                final_code = None
                result = {"handoff": {"status": "done"}, "calls": []}

            return _Result()

        monkeypatch.setattr(at, "run_turn", _spy)
        nr._default_agent_fn(
            "go",
            {
                "node_id": "n-frontier",
                "node_name": "n-frontier",
                "node_inputs": {"tier": "xai:grok-4.5"},
                "allowed_tools": [],
            },
            provider=_TurnScriptedProvider(),
        )

        frontier = load_tier("frontier")
        assert seen["tier"] == "frontier"
        assert seen["max_tokens"] == frontier.generation_headroom
        assert seen["capability"] is not None


class TestDispatchLaneParity:
    """The compiled-DAG lane and ``MissionDispatch._run_subagent_turn`` both fan
    into ``agent_turn.run_turn``, and every budget-, retry- or policy-level rule
    has to hold on BOTH. Copy-paste drift between the two is the recurring bug
    in this subsystem, so these pin the SHARED helpers rather than the behavior.
    """

    def test_retryable_verdict_routes_through_the_shared_predicate(
        self, monkeypatch,
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

    def test_subagent_tool_call_budget_slider_reaches_dag_nodes(self, monkeypatch):
        """``agent.subagent_tool_call_budget`` is one GUI slider. This lane used
        to not read the key at all, so moving it governed only the ``delegate``
        lane and had ZERO effect on compiled-DAG (mission_start) roles."""
        from services.agent import agent_turn as at
        from services.agent import config as agent_config

        monkeypatch.delenv("CHUDWARE_AGENT_MAX_TOOL_CALLS", raising=False)
        monkeypatch.setattr(agent_config, "get_subagent_tool_call_budget",
                            lambda *a, **k: 9)
        seen: dict = {}

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return _fake_run("confirmed", True)

        monkeypatch.setattr(at, "run_turn", _spy)
        nr._default_agent_fn(
            "go",
            {"node_name": "sourcing_specialist",
             "role_id": "sourcing_specialist",
             "allowed_tools": ["part_search", "parts_taxonomy"]},
            provider=_TurnScriptedProvider(),
        )
        assert seen["max_tool_calls"] == 9

    def test_zero_slider_leaves_the_producer_floor_intact(self, monkeypatch):
        """0 is the default and means "derive automatically" — it must not reset
        the role floor this lane computes from tool_budget.floor_for."""
        from services.agent import agent_turn as at
        from services.agent import config as agent_config

        monkeypatch.delenv("CHUDWARE_AGENT_MAX_TOOL_CALLS", raising=False)
        monkeypatch.setattr(agent_config, "get_subagent_tool_call_budget",
                            lambda *a, **k: 0)
        seen: dict = {}

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return _fake_run("confirmed", True)

        monkeypatch.setattr(at, "run_turn", _spy)
        nr._default_agent_fn(
            "go",
            {"node_name": "feature_modeler", "role_id": "feature_modeler",
             "allowed_tools": ["new_part", "add_features", "save_part"]},
            provider=_TurnScriptedProvider(),
        )
        from services.agent import tool_budget as tb
        assert seen["max_tool_calls"] == tb.floor_for(
            "feature_modeler", ["new_part", "add_features", "save_part"], None,
            base=None)


class TestProviderTierResolution:
    """Regression coverage for the mission-tier provider leak: an INTERNAL DAG
    node (not just the mission's entry/INTAKE call) must stay on the tier/
    provider the mission was started with, instead of independently
    re-resolving the bare ambient config.get_provider() default.

    Two fix paths, tested separately:
      1. An explicit provider injected via make_default_agent_fn(provider=...)
         — what loop.run_mission now threads through from its own already-
         resolved provider argument.
      2. A tier recorded on the node's inputs (mission_template's tier=
         plumbing) — resolved via services.agent.tiers.provider_for_tier, for
         callers with no in-hand Provider object (mission_start/dag_run).

    In both cases the ambient config.get_provider() must NOT be called — if it
    is, the node has leaked to the ambient/cloud default.
    """

    def test_injected_provider_used_for_internal_node_not_ambient(self, monkeypatch):
        from services.agent import config as agent_config
        from services.agent import agent_turn as at

        local_provider = _TurnScriptedProvider()

        def _ambient_should_not_be_called(name=None, cfg=None):
            raise AssertionError(
                "ambient config.get_provider() was called — the mission's own "
                "provider did not propagate to this internal node")

        monkeypatch.setattr(agent_config, "get_provider", _ambient_should_not_be_called)

        seen = {}
        real_run_turn = at.run_turn

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real_run_turn(*args, **kwargs)

        monkeypatch.setattr(at, "run_turn", _spy)

        # An "internal" node — e.g. a BUILD node, not the mission's entry node.
        agent_fn = nr.make_default_agent_fn(provider=local_provider)
        res = agent_fn("explain the bracket",
                       {"node_name": "n_build_d1", "allowed_tools": ["ledger_note"]})

        assert res.success is True
        assert seen.get("provider") is local_provider

    def test_node_tier_resolves_via_shared_tiers_seam_not_ambient(self, monkeypatch):
        """No injected provider, but the node carries inputs.tier (the
        mission_start/dag_run MCP path with no Provider object in hand) —
        node_runner must resolve it through services.agent.tiers.provider_for_tier,
        and must NOT fall through to the ambient config.get_provider()."""
        from services.agent import config as agent_config
        from services.agent import tiers as agent_tiers
        from services.agent import agent_turn as at

        local_provider = _TurnScriptedProvider()
        seen_tier_calls = []

        def _ambient_should_not_be_called(name=None, cfg=None):
            raise AssertionError(
                "ambient config.get_provider() was called — the node's tier did "
                "not resolve through the shared tiers.provider_for_tier seam")

        monkeypatch.setattr(agent_config, "get_provider", _ambient_should_not_be_called)

        def _fake_provider_for_tier(tier, cfg=None):
            seen_tier_calls.append(tier)
            return local_provider

        monkeypatch.setattr(agent_tiers, "provider_for_tier", _fake_provider_for_tier)

        seen = {}
        real_run_turn = at.run_turn

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real_run_turn(*args, **kwargs)

        monkeypatch.setattr(at, "run_turn", _spy)

        context_pack = {"node_name": "n_build_d1", "allowed_tools": ["ledger_note"],
                        "node_inputs": {"tier": "local-24k"}}
        res = nr._default_agent_fn("explain the bracket", context_pack)

        assert res.success is True
        assert seen_tier_calls == ["local-24k"]
        assert seen.get("provider") is local_provider

    def test_no_provider_no_tier_falls_back_to_ambient_default(self, monkeypatch):
        """A node with neither an injected provider nor a recorded tier must keep
        resolving the ambient config.get_provider() default — the targeted-fix
        requirement: no behaviour change for callers that specify neither."""
        from services.agent import config as agent_config

        calls = []

        def _tracked_get_provider(name=None, cfg=None):
            calls.append((name, cfg))
            return _TurnScriptedProvider()

        monkeypatch.setattr(agent_config, "get_provider", _tracked_get_provider)

        res = nr._default_agent_fn("go", {"node_name": "n1"})
        assert res.success is True
        assert len(calls) == 1


class TestRoleCardWiring:
    """T-W30 real-run regressions: mission-graph nodes carry role in inputs.role
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
        node = {"id": "n1", "type": "design", "name": "BUILD DL1",
                "inputs": {"role": "feature_modeler"}}
        pack = cp.assemble(node, {})
        seen, agent_fn = self._capture_pack()
        verdict, _ = nr.run(node, pack, agent_fn=agent_fn)
        assert verdict == "done"
        assert seen["pack"].get("role_id") == "feature_modeler"
        assert seen["pack"].get("role_system_prompt"), "card prompt must be applied"

    def test_card_allowlist_fills_open_allowed_tools(self):
        from services.agent.roles import load_role
        node = {"id": "n1", "type": "design", "name": "BUILD DL1",
                "inputs": {"role": "feature_modeler"}}
        pack = cp.assemble(node, {})
        assert pack["allowed_tools"] is None  # open before the card applies
        seen, agent_fn = self._capture_pack()
        nr.run(node, pack, agent_fn=agent_fn)
        assert seen["pack"]["allowed_tools"] == \
            list(load_role("feature_modeler").concrete_tools)

    def test_explicit_node_tools_still_win_over_card(self):
        node = {"id": "n1", "type": "design", "name": "BUILD DL1",
                "tools": ["measure_part"],
                "inputs": {"role": "feature_modeler"}}
        pack = cp.assemble(node, {})
        seen, agent_fn = self._capture_pack()
        nr.run(node, pack, agent_fn=agent_fn)
        assert seen["pack"]["allowed_tools"] == ["measure_part"]


class TestMissionNodeRequests:
    """T-W30: mission node requests must EMBED the manifest content (verbatim
    request + deliverable + acceptance criteria), not reference an absent brief."""

    def _manifest(self):
        return {
            "schema": "aicad.deliverables/1",
            "request_verbatim": "make a flanged bushing with a d20 sliding-fit bore",
            "deliverables": [{
                "id": "DL1", "kind": "part", "name": "flanged bushing",
                "description": "bushing with flange",
                "acceptance_criteria": [
                    {"id": "AC1", "text": "bore d20 H7",
                     "check": {"tool": "fit_report", "pass_rule": "clearance in [0.007,0.041]"}},
                ],
                "depends_on": [], "status": "pending",
            }],
            "ambiguities": [], "assumptions": [],
        }

    def test_build_node_request_carries_manifest_content(self):
        from services.dag.mission_template import build_mission_graph
        graph = build_mission_graph(self._manifest(), graph_id="t")
        build = next(n for n in graph["nodes"] if n["id"] == "n_build_DL1")
        req = build["inputs"]["request"]
        assert "make a flanged bushing" in req          # verbatim mission request
        assert "AC1" in req and "fit_report" in req     # the criterion + named tool
        assert "clearance in [0.007,0.041]" in req      # the pass rule

    def test_every_mission_node_has_a_request(self):
        from services.dag.mission_template import build_mission_graph
        graph = build_mission_graph(self._manifest(), graph_id="t", org_layer=True)
        for n in graph["nodes"]:
            assert n["inputs"].get("request"), f"{n['id']} has no request"

def test_role_policy_is_scoped_to_the_turn():
    """T-W30 gate-sweep regression: applying a role card's allowlist must NOT
    leak into the process-global tool_registry after the node turn returns
    (it broke ~78 unrelated cad/gsl tool tests in the combined sweep)."""
    from services.mcp import tool_registry as reg
    reg.reset_policy()
    assert reg.is_allowed("export_part")  # default: everything allowed
    node = {"id": "n1", "type": "design", "name": "BUILD DL1",
            "inputs": {"role": "feature_modeler"}}
    nr.run(node, cp.assemble(node, {}),
           agent_fn=lambda req, pack: _fake_run("confirmed", True))
    # export_part is NOT in feature_modeler's allowlist — if the policy leaked,
    # this dispatch-time check now refuses it.
    assert reg.is_allowed("export_part"), "role tool policy leaked past the node turn"


def test_role_policy_restore_failure_is_logged_and_surfaced(monkeypatch, caplog):
    """A contaminated process cannot hide a failed global-policy restoration."""
    import logging

    def _broken_restore():
        raise RuntimeError("registry restore exploded")

    monkeypatch.setattr(nr, "_snapshot_tool_policy", lambda: _broken_restore)
    monkeypatch.setattr(nr, "_apply_role_tool_policy", lambda role_card: None)
    events = []
    node = {
        "id": "n1",
        "type": "design",
        "name": "BUILD DL1",
        "inputs": {"role": "feature_modeler"},
    }
    with caplog.at_level(logging.ERROR, logger=nr.__name__):
        verdict, out = nr.run(
            node,
            cp.assemble(node, {}),
            emit=lambda kind, payload: events.append((kind, payload)),
            agent_fn=lambda req, pack: _fake_run("confirmed", True),
        )

    assert verdict == nr.VERDICT_ESCALATE
    assert out["data"]["stop_reason"] == "tool_policy_restore_failed"
    assert "registry restore exploded" in out["data"]["error"]
    assert "Failed to restore process-global tool policy" in caplog.text
    failure = next(p for kind, p in events if kind == "dag.node.failed")
    assert "registry restore exploded" in failure["error"]


# ---------------------------------------------------------------------------
# T-W32 — tool-schema injection + run-scoped dispatcher wiring
# ---------------------------------------------------------------------------

class TestToolSchemaAndMissionDispatchWiring:
    def test_make_default_agent_fn_builds_mission_dispatch_from_ledger(self, monkeypatch, tmp_path):
        """A bound ledger yields a run-scoped dispatcher; ledger_note dispatches
        against THIS run (mission hit) instead of 'could not be resolved'."""
        from services.agent import config as agent_config
        from services.agent.ledger import Ledger

        class _P:
            def __init__(self):
                self._replies = [
                    '```json\n{"tool": "ledger_note", "args": {"kind": "note", '
                    '"payload": {"text": "hello"}}}\n```',
                    '```json\n{"done": true, "handoff": {}}\n```',
                ]

            def complete(self, system, messages, max_tokens=None):
                return self._replies.pop(0)

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: _P())

        led = Ledger("t_w32_node", path=tmp_path / "ledger.json")

        # keep the ledger off-disk: capture appends in-memory
        events = []
        monkeypatch.setattr(led, "_save", lambda: None)
        agent_fn = nr.make_default_agent_fn(ledger=led)
        res = agent_fn("note it", {"node_name": "n1",
                                   "allowed_tools": ["ledger_note"]})
        assert res.success is True
        call = res.result["calls"][0]
        assert call["tool"] == "ledger_note" and call["ok"] is True, call

    def test_tools_slot_rendered_measured_and_logged(self, monkeypatch, tmp_path):
        """allowed_tools → run_turn gets a rendered tools_text and the ledger
        carries ONE tools_slot event with the MEASURED token cost + tier."""
        from services.agent import config as agent_config
        from services.agent import agent_turn as at
        from services.agent.ledger import Ledger

        class _P:
            def complete(self, system, messages, max_tokens=None):
                return '```json\n{"done": true, "handoff": {}}\n```'

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: _P())
        monkeypatch.setenv("CHUDWARE_TIER", "local-24k")

        led = Ledger("t_w32_slot", path=tmp_path / "ledger.json")
        monkeypatch.setattr(led, "_save", lambda: None)

        seen = {}
        real_run_turn = at.run_turn

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real_run_turn(*args, **kwargs)

        monkeypatch.setattr(at, "run_turn", _spy)
        agent_fn = nr.make_default_agent_fn(ledger=led)
        agent_fn("go", {
            "node_id": "n1",
            "node_name": "n1",
            "phase": "deliver",
            "allowed_tools": ["ledger_note"],
        })

        assert seen.get("tools_text"), "tools slot was not rendered"
        assert "ledger_note(" in seen["tools_text"]
        allowed = set(seen.get("allowed_tools") or [])
        from services.agent.copilot_mode_tools import SUBAGENT_DEFAULT_TOOLS
        assert SUBAGENT_DEFAULT_TOOLS <= allowed
        assert "run_shell" not in allowed
        for name in ("fs_read", "fs_glob", "fs_grep"):
            assert f"{name}(" in seen["tools_text"]
        assert seen["subagent_id"] == "n1"
        assert seen["phase"] == "deliver"
        assert seen.get("mission_dispatch") is not None
        slots = [e for e in led.events
                 if (e.get("payload") or {}).get("event") == "tools_slot"]
        assert len(slots) == 1
        p = slots[0]["payload"]
        assert p["tier"] == "local-24k" and p["tokens"] > 0
        assert p["budget_tokens"] == 2500  # tiers.yaml local-24k budgets.tools
        assert p["trimmed"] is True

    def test_no_allowed_tools_no_slot(self, monkeypatch):
        """An open-tools node (allowed_tools None) renders no schema block."""
        from services.agent import config as agent_config
        from services.agent import agent_turn as at

        class _P:
            def complete(self, system, messages, max_tokens=None):
                return '```json\n{"done": true, "handoff": {}}\n```'

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: _P())
        seen = {}
        real_run_turn = at.run_turn

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return real_run_turn(*args, **kwargs)

        monkeypatch.setattr(at, "run_turn", _spy)
        nr._default_agent_fn("go", {"node_name": "n1"})
        assert seen.get("tools_text") is None


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
        self, role, mandated_calls,
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

    def test_session_default_child_provider_is_clamped_without_parent_mutation(
        self, monkeypatch,
    ):
        from services.agent import config as agent_config
        from services.agent import role_models

        assignment = SimpleNamespace(
            mode="session_default",
            provider="",
            model="",
            rank=5,
            reason="follow composer",
            is_session_default=True,
            to_dict=lambda: {
                "mode": "session_default",
                "provider": "",
                "model": "",
                "rank": 5,
                "reason": "follow composer",
            },
        )
        monkeypatch.setattr(agent_config, "load_config",
                            lambda: {"reasoning_effort": "max"})
        monkeypatch.setattr(role_models, "resolve_for_node",
                            lambda *args, **kwargs: assignment)
        monkeypatch.setattr(role_models, "resolve_role_thinking",
                            lambda *args, **kwargs: "max")

        parent = SimpleNamespace(reasoning_effort="max", name="injected")
        child = nr._resolve_provider(
            {"role_id": "reviewer", "node_inputs": {}},
            provider=parent,
        )

        assert child is not parent
        assert child.reasoning_effort == "high"
        assert parent.reasoning_effort == "max"

    def test_provider_exception_fallback_is_also_child_clamped(
        self, monkeypatch,
    ):
        from services.agent import role_models

        monkeypatch.setattr(
            role_models,
            "resolve_for_node",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("assignment unavailable")
            ),
        )
        parent = SimpleNamespace(reasoning_effort="max", name="injected")
        child = nr._resolve_provider(
            {"role_id": "reviewer", "node_inputs": {}},
            provider=parent,
        )

        assert child is not parent
        assert child.reasoning_effort == "high"
        assert parent.reasoning_effort == "max"
