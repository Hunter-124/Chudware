"""Smoke tests for services/dag.

Tests run against the stub handlers.
All checkpoint files are written to a pytest tmp_path dir — never to the real runs dir.

Run with:
    ~/.local/share/Chudware/mcp-venv/bin/pytest services/dag/test_executor.py -q
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from services.dag import executor
from services.dag.graph_loader import GraphValidationError, validate_graph


# ---------------------------------------------------------------------------
# Deterministic agent_fn for executor-mechanics tests.
#
# The node_runner default agent_fn is now a REAL tool-executing turn
# (config provider + agent_turn) — with no model server it honestly escalates
# rather than fabricating success. These smoke tests exercise executor mechanics
# (checkpoint DB, event emission, graph traversal), NOT the live agent, so they
# inject a deterministic done-result agent_fn (the intended test pattern).
# ---------------------------------------------------------------------------

class _DoneAgentResult:
    stop_reason = "confirmed"
    success = True
    final_code = None
    result = None


def _done_agent_fn(request, context_pack):
    return _DoneAgentResult()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _two_node_graph() -> dict[str, Any]:
    """Minimal valid aicad.dag/1 graph: plan → report."""
    return {
        "schema": "aicad.dag/1",
        "id": "test-graph-001",
        "name": "smoke-test graph",
        "nodes": [
            {
                "id": "n_plan",
                "type": "plan",
                "name": "Plan",
                "inputs": {"request": "make a bracket"},
                "on_fail": "abort",
                "autonomy": "auto",
            },
            {
                "id": "n_report",
                "type": "report",
                "name": "Report",
                "inputs": {},
                "on_fail": "abort",
                "autonomy": "auto",
            },
        ],
        "edges": [
            {"from": "n_plan", "to": "n_report"},
        ],
    }


# ---------------------------------------------------------------------------
# Core smoke test (DoD requirement)
# ---------------------------------------------------------------------------

class TestExecutorSmoke:
    def test_two_node_graph_runs_and_creates_checkpoint(self, tmp_path: Path) -> None:
        """DoD: executor.run() executes a two-node graph and writes a checkpoint .db."""
        graph = _two_node_graph()
        result = executor.run(graph, runs_dir=tmp_path, agent_fn=_done_agent_fn)

        # Run result shape
        assert "run_id" in result
        assert result["status"] == "done", f"Expected 'done', got {result['status']!r}"

        # Both nodes completed
        assert "n_plan" in result["nodes"]
        assert "n_report" in result["nodes"]
        assert result["nodes"]["n_plan"]["status"] == "done"
        assert result["nodes"]["n_report"]["status"] == "done"

        # Checkpoint SQLite file created and non-empty
        db_path = Path(result["checkpoint_db"])
        assert db_path.exists(), f"Checkpoint DB not found at {db_path}"
        assert db_path.stat().st_size > 0, "Checkpoint DB is empty"

        # Verify it's a valid SQLite file (not corrupted)
        with sqlite3.connect(str(db_path)) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        assert len(tables) > 0, f"Checkpoint DB has no tables; got {tables!r}"

    def test_checkpoint_db_path_uses_run_id(self, tmp_path: Path) -> None:
        """Checkpoint file is named <run_id>.db."""
        graph = _two_node_graph()
        fixed_id = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
        result = executor.run(graph, run_id=fixed_id, runs_dir=tmp_path, agent_fn=_done_agent_fn)
        db_path = Path(result["checkpoint_db"])
        assert db_path.name == f"{fixed_id}.db"

    def test_events_emitted(self, tmp_path: Path) -> None:
        """executor.run() emits dag.run.started, dag.node.*, dag.run.done events."""
        events: list[tuple[str, dict]] = []

        def _emit(event_type: str, payload: dict) -> None:
            events.append((event_type, payload))

        graph = _two_node_graph()
        executor.run(graph, runs_dir=tmp_path, emit=_emit, agent_fn=_done_agent_fn)

        event_types = [e[0] for e in events]
        assert "dag.run.started" in event_types
        assert "dag.run.done" in event_types
        # Each node should emit started + done
        assert event_types.count("dag.node.started") == 2
        assert event_types.count("dag.node.done") == 2

    def test_importable_from_canonical_package(self) -> None:
        """The executor is importable through its canonical package."""
        from services.dag import executor as _exec  # noqa: F401
        assert callable(_exec.run)


# ---------------------------------------------------------------------------
# Run ID path containment
# ---------------------------------------------------------------------------

class TestRunIdValidation:
    @pytest.mark.parametrize(
        "run_id",
        [
            "aaaabbbb-cccc-dddd-eeee-ffff00001111",
            "mission",
            "run_walk01",
            "r_deadbeef",
            "run.id",
        ],
    )
    def test_existing_run_id_shapes_are_accepted_unchanged(self, run_id: str) -> None:
        assert executor.validate_run_id(run_id) == run_id

    @pytest.mark.parametrize(
        "run_id",
        [
            "",
            ".",
            "..",
            "../escape",
            r"..\escape",
            "/tmp/escape",
            r"C:\tmp\escape",
            "run/id",
            r"run\id",
            "run id",
            "run:1",
            "rún",
            "x" * 129,
        ],
    )
    def test_unsafe_run_ids_are_rejected(self, run_id: str) -> None:
        with pytest.raises(ValueError, match="run_id"):
            executor.validate_run_id(run_id)

    @pytest.mark.parametrize(
        "run_id",
        [
            "",
            ".",
            "..",
            "../escaped",
            "/tmp/escaped",
            r"C:\tmp\escaped",
            "run id",
            "x" * 129,
        ],
    )
    def test_run_rejects_invalid_id_before_any_write(
        self,
        run_id: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runs_dir = tmp_path / "runs"
        xdg_root = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_root))

        with pytest.raises(ValueError, match="run_id"):
            executor.run(
                _two_node_graph(),
                run_id=run_id,
                runs_dir=runs_dir,
                agent_fn=_done_agent_fn,
            )

        assert not runs_dir.exists()
        assert not (tmp_path / "escaped.db").exists()
        assert not (tmp_path / "escaped.graph.json").exists()
        assert not xdg_root.exists()

    def test_resume_rejects_traversal_before_any_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs_dir = tmp_path / "runs"
        xdg_root = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_root))

        with pytest.raises(ValueError, match="path separators"):
            executor.resume("../escaped", runs_dir=runs_dir)

        assert not runs_dir.exists()
        assert not (tmp_path / "escaped.db").exists()
        assert not (tmp_path / "escaped.graph.json").exists()
        assert not xdg_root.exists()


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestGraphValidation:
    def test_wrong_schema_string_raises(self) -> None:
        g = _two_node_graph()
        g["schema"] = "aicad.dag/2"
        with pytest.raises(GraphValidationError, match="schema string"):
            validate_graph(g)

    def test_missing_schema_raises(self) -> None:
        g = _two_node_graph()
        del g["schema"]
        with pytest.raises(GraphValidationError):
            validate_graph(g)

    def test_empty_nodes_raises(self) -> None:
        g = _two_node_graph()
        g["nodes"] = []
        with pytest.raises(GraphValidationError, match="non-empty"):
            validate_graph(g)

    def test_invalid_node_type_raises(self) -> None:
        g = _two_node_graph()
        g["nodes"][0]["type"] = "unknown_type"
        with pytest.raises(GraphValidationError, match="invalid type"):
            validate_graph(g)

    def test_invalid_autonomy_raises(self) -> None:
        g = _two_node_graph()
        g["nodes"][0]["autonomy"] = "robot"
        with pytest.raises(GraphValidationError, match="invalid autonomy"):
            validate_graph(g)

    def test_invalid_on_fail_raises(self) -> None:
        g = _two_node_graph()
        g["nodes"][0]["on_fail"] = "explode"
        with pytest.raises(GraphValidationError, match="invalid on_fail"):
            validate_graph(g)

    def test_edge_unknown_node_raises(self) -> None:
        g = _two_node_graph()
        g["edges"].append({"from": "n_plan", "to": "n_ghost"})
        with pytest.raises(GraphValidationError, match="unknown 'to' node"):
            validate_graph(g)

    def test_duplicate_node_id_raises(self) -> None:
        g = _two_node_graph()
        g["nodes"].append({**g["nodes"][0]})  # duplicate id
        with pytest.raises(GraphValidationError, match="Duplicate node"):
            validate_graph(g)

    def test_all_node_types_accepted(self) -> None:
        """Every declared node type in §3 must pass validation."""
        from services.dag.graph_loader import VALID_NODE_TYPES
        for ntype in VALID_NODE_TYPES:
            g = _two_node_graph()
            g["nodes"][0]["type"] = ntype
            validate_graph(g)  # must not raise

    def test_all_autonomy_levels_accepted(self) -> None:
        from services.dag.graph_loader import VALID_AUTONOMY
        for level in VALID_AUTONOMY:
            g = _two_node_graph()
            g["nodes"][0]["autonomy"] = level
            validate_graph(g)  # must not raise

    def test_all_on_fail_values_accepted(self) -> None:
        from services.dag.graph_loader import VALID_ON_FAIL
        for val in VALID_ON_FAIL:
            g = _two_node_graph()
            g["nodes"][0]["on_fail"] = val
            validate_graph(g)  # must not raise


# ---------------------------------------------------------------------------
# Single-node edge case
# ---------------------------------------------------------------------------

class TestSingleNode:
    def test_single_node_no_edges(self, tmp_path: Path) -> None:
        """A graph with one node and no edges should run to done."""
        graph = {
            "schema": "aicad.dag/1",
            "id": "single-node-graph",
            "name": "single node",
            "nodes": [
                {
                    "id": "n1",
                    "type": "design",
                    "name": "Design only",
                    "inputs": {"request": "make a box"},
                    "on_fail": "pause",
                    "autonomy": "auto",
                }
            ],
            "edges": [],
        }
        result = executor.run(graph, runs_dir=tmp_path, agent_fn=_done_agent_fn)
        assert result["status"] == "done"
        assert result["nodes"]["n1"]["status"] == "done"
        assert Path(result["checkpoint_db"]).exists()


# ---------------------------------------------------------------------------
# default_runs_dir (non-destructive)
# ---------------------------------------------------------------------------

class TestDefaultRunsDir:
    def test_returns_path_object(self) -> None:
        d = executor.default_runs_dir()
        assert isinstance(d, Path)
        assert "Chudware" in str(d)
        assert "runs" in str(d)
