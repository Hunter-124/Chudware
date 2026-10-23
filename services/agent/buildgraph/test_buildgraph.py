"""test_buildgraph.py — Tests for aicad.buildgraph/1 planner, validator, executor.

Contract: contracts/advisor.md §13 (FROZEN), §3 (advisory items).
Coverage:

  ✓ Rule inference on a bracket fixture (base→boss→bore-through-boss→fillet,
    4-hole pattern after base)
  ✓ Validator catches a plan missing the boss (bore has dangling requires)
  ✓ Cycle fixture
  ✓ Parallel ready-set on a 3-part assembly plan
  ✓ Executor status walk
"""
from __future__ import annotations

import pytest

from services.agent.buildgraph.planner import build_graph
from services.agent.buildgraph.schema import validate_graph, BuildGraphCycleError
from services.agent.buildgraph.executor import (
    BuildGraphExecutor,
    run_sync,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_PENDING,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Bracket plan: base solid → boss → bore through boss → fillet + 4-hole pattern
BRACKET_PLAN = {
    "name": "bracket",
    "summary": "L-bracket with boss and bore",
    "parts": [
        {
            "name": "bracket",
            "purpose": "main body",
            "dimensions": {"length": 80, "width": 60, "thickness": 6},
            "features": [
                "base extrude 80x60x6 box",
                "boss cylinder r=6 h=8 on base top face",
                "bore through boss r=3 vertical hole",
                "fillet all vertical edges r=4",
                "4x M3 clearance holes (r=1.7) linear pattern on base",
            ],
        }
    ],
    "fit": "",
}

# Bracket plan WITHOUT the boss — bore should get a dangling advisory
BRACKET_NO_BOSS_PLAN = {
    "name": "bracket_no_boss",
    "summary": "Bracket missing boss",
    "parts": [
        {
            "name": "bracket",
            "purpose": "main body",
            "dimensions": {"length": 80, "width": 60, "thickness": 6},
            "features": [
                "base extrude 80x60x6 box",
                "bore through boss r=3 vertical hole",   # boss not present
                "fillet all vertical edges r=4",
            ],
        }
    ],
    "fit": "",
}

# Three-part assembly plan — parts are independent, should show parallel ready sets
ASSEMBLY_PLAN = {
    "name": "clamp_assembly",
    "summary": "3-part assembly",
    "parts": [
        {
            "name": "base_plate",
            "purpose": "foundation",
            "dimensions": {"length": 100, "width": 80, "thickness": 8},
            "features": ["base extrude 100x80x8"],
        },
        {
            "name": "jaw_arm",
            "purpose": "clamping arm",
            "dimensions": {"length": 60, "width": 20, "thickness": 10},
            "features": ["arm extrude 60x20x10"],
        },
        {
            "name": "clamp_screw",
            "purpose": "screw body",
            "dimensions": {"diameter": 10, "length": 50},
            "features": ["revolve profile to make screw cylinder"],
        },
    ],
    "fit": "jaw_arm mates to base_plate with concentric + coincident; screw mates jaw_arm",
}


# ---------------------------------------------------------------------------
# 1. Rule inference on the bracket fixture
# ---------------------------------------------------------------------------

class TestBracketRuleInference:
    def test_graph_schema(self):
        graph, _ = build_graph(BRACKET_PLAN)
        assert graph["schema"] == "aicad.buildgraph/1"

    def test_node_kinds_present(self):
        graph, _ = build_graph(BRACKET_PLAN)
        kinds = {n["kind"] for n in graph["nodes"]}
        assert "part" in kinds
        assert "feature" in kinds
        assert "checkpoint" in kinds

    def test_every_node_has_why(self):
        graph, _ = build_graph(BRACKET_PLAN)
        for node in graph["nodes"]:
            assert node.get("why"), f"Node {node['id']!r} missing 'why'"

    def test_every_requires_is_known_id(self):
        graph, _ = build_graph(BRACKET_PLAN)
        node_ids = {n["id"] for n in graph["nodes"]}
        for node in graph["nodes"]:
            for req in node.get("requires", []):
                assert req in node_ids, (
                    f"Node {node['id']!r} requires {req!r} which is not in the graph"
                )

    def test_bore_requires_boss(self):
        """bore-through-boss must require the boss node."""
        graph, _ = build_graph(BRACKET_PLAN)
        nodes_by_label = {n["label"]: n for n in graph["nodes"]}
        bore_node = next(
            (n for n in graph["nodes"] if "bore" in n.get("label", "").lower()),
            None,
        )
        boss_node = next(
            (n for n in graph["nodes"] if "boss" in n.get("label", "").lower()),
            None,
        )
        assert bore_node is not None, "bore node not found"
        assert boss_node is not None, "boss node not found"
        assert boss_node["id"] in bore_node["requires"], (
            f"bore node {bore_node['id']!r} should require boss node {boss_node['id']!r}"
        )

    def test_pattern_comes_after_base(self):
        """Pattern node must require the base solid."""
        graph, _ = build_graph(BRACKET_PLAN)
        pattern_node = next(
            (n for n in graph["nodes"] if "pattern" in n.get("label", "").lower()),
            None,
        )
        base_node = next(
            (n for n in graph["nodes"] if "base" in n.get("label", "").lower()
             and n["kind"] == "feature"),
            None,
        )
        assert pattern_node is not None, "pattern node not found"
        assert base_node is not None, "base node not found"
        assert base_node["id"] in pattern_node["requires"], (
            f"pattern node should require base node"
        )

    def test_checkpoint_joins_all_features(self):
        """The part_done checkpoint must require all feature nodes."""
        graph, _ = build_graph(BRACKET_PLAN)
        checkpoint = next(
            (n for n in graph["nodes"]
             if n["kind"] == "checkpoint" and "done" in n.get("label", "")),
            None,
        )
        assert checkpoint is not None
        feature_ids = {n["id"] for n in graph["nodes"] if n["kind"] == "feature"}
        for fid in feature_ids:
            assert fid in checkpoint["requires"], (
                f"checkpoint should require feature {fid!r}"
            )

    def test_no_cycles_in_bracket_graph(self):
        """Bracket plan has no cycles — validator must not raise."""
        graph, _ = build_graph(BRACKET_PLAN)
        findings = validate_graph(graph)  # must not raise
        cycle_findings = [
            f for f in findings
            for item in f.get("items", [])
            if "cycle" in item.get("check", "")
        ]
        assert not cycle_findings, f"Unexpected cycle findings: {cycle_findings}"


# ---------------------------------------------------------------------------
# 2. Validator catches missing boss (dangling requires)
# ---------------------------------------------------------------------------

class TestDanglingBoss:
    def test_bore_without_boss_emits_blocker(self):
        """When boss is absent, bore-through-boss should generate a dangling advisory."""
        _graph, advisories = build_graph(BRACKET_NO_BOSS_PLAN)
        all_items = [item for adv in advisories for item in adv.get("items", [])]
        blocker_items = [i for i in all_items if i.get("severity") == "blocker"]
        # We expect at least one blocker referencing 'bore' and 'boss'
        relevant = [
            i for i in blocker_items
            if "boss" in i.get("finding", "").lower()
            or "dangling" in i.get("check", "").lower()
        ]
        assert relevant, (
            f"Expected a blocker advisory for bore-through-boss when boss absent. "
            f"Got advisories: {advisories}"
        )

    def test_advisory_items_have_correct_schema(self):
        """Advisory items must conform to aicad.advisory/1 shape."""
        _graph, advisories = build_graph(BRACKET_NO_BOSS_PLAN)
        for adv in advisories:
            assert adv.get("schema") == "aicad.advisory/1", (
                f"Advisory missing schema: {adv}"
            )
            assert "items" in adv
            for item in adv["items"]:
                assert "id" in item
                assert item.get("severity") in ("info", "hint", "action", "blocker"), (
                    f"Invalid severity: {item.get('severity')}"
                )
                assert "check" in item
                assert "finding" in item
                assert "source" in item


# ---------------------------------------------------------------------------
# 3. Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def _make_cycle_graph(self):
        """Build an aicad.buildgraph/1 graph with a 2-node cycle manually."""
        return {
            "schema": "aicad.buildgraph/1",
            "mission": "cycle_test",
            "nodes": [
                {
                    "id": "bg_a",
                    "kind": "feature",
                    "label": "feature A",
                    "doc_id": "",
                    "refs": {"features": [], "parts": []},
                    "requires": ["bg_b"],   # A requires B
                    "why": "test cycle",
                    "status": "pending",
                    "assigned": None,
                },
                {
                    "id": "bg_b",
                    "kind": "feature",
                    "label": "feature B",
                    "doc_id": "",
                    "refs": {"features": [], "parts": []},
                    "requires": ["bg_a"],   # B requires A → cycle
                    "why": "test cycle",
                    "status": "pending",
                    "assigned": None,
                },
            ],
        }

    def test_validator_raises_on_cycle(self):
        graph = self._make_cycle_graph()
        with pytest.raises(BuildGraphCycleError) as exc_info:
            validate_graph(graph)
        assert "cycle" in str(exc_info.value).lower()

    def test_planner_propagates_cycle_error(self):
        """build_graph should propagate BuildGraphCycleError for cyclic inputs."""
        # We can't trivially produce a cycle from a normal plan (the planner
        # only adds edges from rules/requires), so we test the validator directly.
        graph = {
            "schema": "aicad.buildgraph/1",
            "mission": "x",
            "nodes": [
                {"id": "bg_x", "kind": "feature", "label": "x", "doc_id": "",
                 "refs": {}, "requires": ["bg_y"], "why": "test", "status": "pending",
                 "assigned": None},
                {"id": "bg_y", "kind": "feature", "label": "y", "doc_id": "",
                 "refs": {}, "requires": ["bg_x"], "why": "test", "status": "pending",
                 "assigned": None},
            ],
        }
        with pytest.raises(BuildGraphCycleError):
            validate_graph(graph)


# ---------------------------------------------------------------------------
# 4. Parallel ready-set on a 3-part assembly plan
# ---------------------------------------------------------------------------

class TestAssemblyParallelReadySet:
    def test_three_part_roots_are_initially_ready(self):
        """In a 3-part assembly plan, all three part roots should be ready simultaneously."""
        graph, advisories = build_graph(ASSEMBLY_PLAN)
        ex = BuildGraphExecutor(graph)
        ready = ex.ready_nodes()
        # All three part nodes (kind=part) should be initially ready
        part_node_ids = {n["id"] for n in graph["nodes"] if n["kind"] == "part"}
        ready_set = set(ready)
        # Every part root should be in the ready set
        assert part_node_ids.issubset(ready_set), (
            f"Expected part roots {part_node_ids} to all be ready, got {ready_set}"
        )

    def test_mission_done_not_ready_initially(self):
        """Mission done checkpoint should not be ready until all parts are done."""
        graph, _ = build_graph(ASSEMBLY_PLAN)
        ex = BuildGraphExecutor(graph)
        ready = ex.ready_nodes()
        mission_done = next(
            (n for n in graph["nodes"]
             if n["kind"] == "checkpoint" and n.get("label") == "mission_done"),
            None,
        )
        if mission_done:
            assert mission_done["id"] not in ready, (
                "mission_done should not be ready before any parts are built"
            )

    def test_ready_set_shrinks_as_nodes_progress(self):
        """After starting a part root, it leaves the ready set."""
        graph, _ = build_graph(ASSEMBLY_PLAN)
        ex = BuildGraphExecutor(graph)
        ready_before = ex.ready_nodes()
        first_ready = ready_before[0]
        ex.start_node(first_ready)
        ready_after = ex.ready_nodes()
        assert first_ready not in ready_after, (
            f"Node {first_ready!r} should not be ready after start_node()"
        )


# ---------------------------------------------------------------------------
# 5. Executor status walk
# ---------------------------------------------------------------------------

class TestExecutorStatusWalk:
    def _simple_graph(self):
        """A→B→C chain."""
        return {
            "schema": "aicad.buildgraph/1",
            "mission": "test_walk",
            "nodes": [
                {"id": "n_a", "kind": "part",     "label": "A", "doc_id": "",
                 "refs": {}, "requires": [],       "why": "root", "status": "pending",
                 "assigned": None},
                {"id": "n_b", "kind": "feature",  "label": "B", "doc_id": "",
                 "refs": {}, "requires": ["n_a"],  "why": "needs A", "status": "pending",
                 "assigned": None},
                {"id": "n_c", "kind": "checkpoint","label": "C", "doc_id": "",
                 "refs": {}, "requires": ["n_b"],  "why": "join", "status": "pending",
                 "assigned": None},
            ],
        }

    def test_happy_path_all_done(self):
        graph = self._simple_graph()
        events = []
        def emit(evt, payload):
            events.append((evt, payload))
        ex = run_sync(graph, lambda n: (True, {}), emit=emit)
        assert ex.is_done()
        assert ex.is_succeeded()
        for nid in ["n_a", "n_b", "n_c"]:
            assert ex.node_status(nid) == STATUS_DONE

    def test_failure_propagates_skips(self):
        """Failing n_a should cause n_b and n_c to be skipped."""
        graph = self._simple_graph()
        results = {"n_a": False, "n_b": True, "n_c": True}
        ex = run_sync(graph, lambda n: (results[n["id"]], {}))
        assert ex.node_status("n_a") == STATUS_FAILED
        assert ex.node_status("n_b") == STATUS_SKIPPED
        assert ex.node_status("n_c") == STATUS_SKIPPED

    def test_partial_failure_mid_chain(self):
        """Failing n_b skips n_c but n_a is done."""
        graph = self._simple_graph()
        results = {"n_a": True, "n_b": False, "n_c": True}
        ex = run_sync(graph, lambda n: (results[n["id"]], {}))
        assert ex.node_status("n_a") == STATUS_DONE
        assert ex.node_status("n_b") == STATUS_FAILED
        assert ex.node_status("n_c") == STATUS_SKIPPED

    def test_summary_shape(self):
        graph = self._simple_graph()
        ex = run_sync(graph, lambda n: (True, {"data": 42}))
        summary = ex.summary()
        assert summary["schema"] == "aicad.buildgraph/1"
        assert summary["is_done"]
        assert summary["is_succeeded"]
        assert summary["failed"] == []
        assert summary["skipped"] == []

    def test_emit_events_fired(self):
        graph = self._simple_graph()
        events: list[tuple[str, dict]] = []
        run_sync(graph, lambda n: (True, {}), emit=lambda e, p: events.append((e, p)))
        event_types = [e[0] for e in events]
        assert "buildgraph.node.started" in event_types
        assert "buildgraph.node.done" in event_types

    def test_ready_nodes_api(self):
        """ready_nodes() returns only pending nodes with all requires done."""
        graph = self._simple_graph()
        ex = BuildGraphExecutor(graph)
        # Initially only n_a is ready
        assert ex.ready_nodes() == ["n_a"]
        ex.start_node("n_a")
        assert ex.ready_nodes() == []  # n_a building, n_b still blocked
        ex.finish_node("n_a", success=True)
        assert ex.ready_nodes() == ["n_b"]
        ex.start_node("n_b")
        ex.finish_node("n_b", success=True)
        assert ex.ready_nodes() == ["n_c"]


# ---------------------------------------------------------------------------
# 6. MCP tools smoke test (import + call with tmp runs_dir)
# ---------------------------------------------------------------------------

class TestMcpToolsSmoke:
    def test_buildgraph_plan_and_status(self, tmp_path):
        """buildgraph_plan returns ok=True; buildgraph_status reads the sidecar."""
        from services.agent.buildgraph.mcp_tools import register_buildgraph_tools

        # Minimal mock MCP object
        class MockMcp:
            def __init__(self):
                self._tools: dict = {}
            def tool(self):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

        mcp = MockMcp()
        register_buildgraph_tools(mcp, runs_dir=tmp_path)

        plan_fn = mcp._tools["buildgraph_plan"]
        status_fn = mcp._tools["buildgraph_status"]

        import json
        result = json.loads(plan_fn(BRACKET_PLAN, mission="bracket_test"))
        assert result["ok"] is True
        assert result["node_count"] > 0
        run_id = result["run_id"]

        status_result = json.loads(status_fn(run_id))
        assert status_result["ok"] is True
        assert status_result["schema"] == "aicad.buildgraph/1"
        assert status_result["node_count"] == result["node_count"]

    def test_buildgraph_status_unknown_run(self, tmp_path):
        """buildgraph_status returns ok=False for unknown run_id."""
        from services.agent.buildgraph.mcp_tools import register_buildgraph_tools
        import json

        class MockMcp:
            def __init__(self):
                self._tools: dict = {}
            def tool(self):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

        mcp = MockMcp()
        register_buildgraph_tools(mcp, runs_dir=tmp_path)
        status_fn = mcp._tools["buildgraph_status"]
        result = json.loads(status_fn("nonexistent-run-id"))
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 7. dag/1 mission input shape
# ---------------------------------------------------------------------------

class TestDagPlanInput:
    def test_dag_plan_graph_produces_buildgraph(self):
        """build_graph accepts an aicad.dag/1 graph as input."""
        dag_plan = {
            "schema": "aicad.dag/1",
            "id": "test-dag-1",
            "name": "test mission",
            "nodes": [
                {"id": "n1", "type": "design", "name": "Design bracket",
                 "inputs": {"request": "..."}, "tools": None,
                 "end_condition": "done", "verify": None, "on_fail": "pause",
                 "autonomy": "auto"},
                {"id": "n2", "type": "verify", "name": "Verify bracket",
                 "inputs": {"request": "..."}, "tools": None,
                 "end_condition": "done", "verify": None, "on_fail": "pause",
                 "autonomy": "auto"},
            ],
            "edges": [
                {"from": "n1", "to": "n2"},
            ],
        }
        graph, advisories = build_graph(dag_plan, mission="test")
        assert graph["schema"] == "aicad.buildgraph/1"
        assert len(graph["nodes"]) == 2
        # n2 should require bg_n1
        n2 = next(n for n in graph["nodes"] if n["id"] == "bg_n2")
        assert "bg_n1" in n2["requires"]

    @pytest.mark.parametrize("edges", [
        [{"from": "n1", "to": "n2"}],
        [["n1", "n2"]],
        [("n1", "n2")],
        ["n1->n2"],
        [{"source": "n1", "target": "n2"}],
        [{"parent": "n1", "child": "n2"}],
    ])
    def test_dag_plan_accepts_every_edge_spelling(self, edges):
        """Live regression: a dag/1 graph whose edges were ``[from, to]`` pairs
        raised ``'list' object has no attribute 'get'`` inside the planner."""
        dag_plan = {
            "schema": "aicad.dag/1",
            "nodes": [{"id": "n1", "kind": "part", "label": "Hub"},
                      {"id": "n2", "kind": "checkpoint", "label": "Verify"}],
            "edges": edges,
        }
        graph, _ = build_graph(dag_plan, mission="test")
        n2 = next(n for n in graph["nodes"] if n["id"] == "bg_n2")
        assert n2["requires"] == ["bg_n1"]

    def test_dag_plan_reads_kind_and_label_aliases(self):
        dag_plan = {
            "schema": "aicad.dag/1",
            "nodes": [{"id": "n1", "kind": "part", "label": "Hub cylinder"}],
            "edges": [],
        }
        graph, _ = build_graph(dag_plan, mission="test")
        assert graph["nodes"][0]["label"] == "Hub cylinder"


class TestCoercePlanDialect:
    """buildgraph_plan input shapes recorded from live runs (gym ledgers)."""

    def _coerce(self, plan_json, **extras):
        from services.agent.buildgraph.mcp_tools import coerce_plan
        return coerce_plan(plan_json, dict(extras), extras.get("mission", ""), [])

    def test_parts_as_name_to_contents_map(self):
        plan, why = self._coerce(
            '{"parts": {"flanged_bushing": ["hub", "flange", "bore"]}}')
        assert why == ""
        assert plan["parts"] == [
            {"name": "flanged_bushing", "features": ["hub", "flange", "bore"]}]

    def test_plan_json_python_literal_with_mixed_quotes(self):
        plan, why = self._coerce(
            "{\"part\": \"bushing\", 'inputs': [\"m4\"], 'outputs': [\"bore\"]}")
        assert why == "" and plan is not None

    @pytest.mark.parametrize("key", ["mission_request", "mission_spec",
                                     "mission_brief", "user_request"])
    def test_build_description_aliases(self, key):
        """These carried the whole brief and were dropped as unknown kwargs."""
        plan, why = self._coerce(None, **{key: "Design a flanged bushing"})
        assert why == "" and plan is not None
        assert plan["parts"]
