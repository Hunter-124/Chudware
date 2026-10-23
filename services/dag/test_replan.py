"""test_replan.py — Tests for services/dag/replan.py.

DoD coverage:
  - dag_replan rejects spliced nodes missing end_condition (ReplannError)
  - dag_replan rejects spliced nodes missing retry_budget back-edge (ReplannError)
  - dag_replan rejects unknown after_node_id (ReplannError)
  - dag_replan rejects id collisions with existing nodes (ReplannError)
  - dag_replan produces a merged graph that passes validate_graph()
  - dag_replan writes merged graph to sidecar
  - Integration smoke: run → replan → resume terminates (gated on CHUDWARE_LIBCHUDWARE)
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from services.dag.replan import ReplannError, _validate_splice, dag_replan
from services.dag.graph_loader import GraphValidationError, validate_graph


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _base_graph(extra_nodes=None, extra_edges=None) -> dict:
    """Minimal valid aicad.dag/1 graph with one design node."""
    return {
        "schema": "aicad.dag/1",
        "id": f"test-{uuid.uuid4()}",
        "name": "test graph",
        "nodes": [
            {
                "id": "n_base",
                "type": "design",
                "name": "Base node",
                "inputs": {"request": "do something"},
                "end_condition": "done when it is done",
                "on_fail": "pause",
                "autonomy": "auto",
            },
            *(extra_nodes or []),
        ],
        "edges": list(extra_edges or []),
    }


def _valid_splice_node(node_id: str = "n_splice") -> dict:
    """Minimal valid splice node with end_condition."""
    return {
        "id": node_id,
        "type": "verify",
        "name": f"Splice node {node_id}",
        "inputs": {"check_type": "interference"},
        "end_condition": "No interferences found.",
        "on_fail": "pause",
        "autonomy": "auto",
    }


def _back_edge(from_id: str, to_id: str, budget: int = 2) -> dict:
    return {"from": from_id, "to": to_id, "retry_budget": budget}


# ---------------------------------------------------------------------------
# _validate_splice unit tests
# ---------------------------------------------------------------------------

class TestValidateSplice:
    def test_rejects_unknown_after_node(self):
        with pytest.raises(ReplannError, match="after_node_id="):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[_valid_splice_node()],
                splice_edges=[_back_edge("n_splice", "n_splice")],
                after_node_id="n_nonexistent",
            )

    def test_rejects_missing_end_condition(self):
        node = _valid_splice_node()
        del node["end_condition"]
        with pytest.raises(ReplannError, match="end_condition"):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[node],
                splice_edges=[_back_edge("n_splice", "n_splice")],
                after_node_id="n_base",
            )

    def test_rejects_empty_end_condition(self):
        node = _valid_splice_node()
        node["end_condition"] = "   "
        with pytest.raises(ReplannError, match="end_condition"):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[node],
                splice_edges=[_back_edge("n_splice", "n_splice")],
                after_node_id="n_base",
            )

    def test_rejects_missing_retry_back_edge(self):
        node = _valid_splice_node()
        # Only a forward edge, no retry_budget
        forward_edge = {"from": "n_base", "to": "n_splice"}
        with pytest.raises(ReplannError, match="retry back-edge"):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[node],
                splice_edges=[forward_edge],
                after_node_id="n_base",
            )

    def test_rejects_id_collision(self):
        with pytest.raises(ReplannError, match="collides"):
            _validate_splice(
                existing_node_ids={"n_base", "n_existing"},
                splice_nodes=[_valid_splice_node("n_existing")],
                splice_edges=[_back_edge("n_existing", "n_existing")],
                after_node_id="n_base",
            )

    def test_rejects_duplicate_splice_ids(self):
        with pytest.raises(ReplannError, match="Duplicate"):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[
                    _valid_splice_node("n_dup"),
                    _valid_splice_node("n_dup"),
                ],
                splice_edges=[_back_edge("n_dup", "n_dup")],
                after_node_id="n_base",
            )

    def test_rejects_edge_referencing_unknown_node(self):
        with pytest.raises(ReplannError, match="unknown 'to' node"):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[_valid_splice_node("n_splice")],
                splice_edges=[
                    _back_edge("n_splice", "n_GHOST"),  # n_GHOST doesn't exist
                ],
                after_node_id="n_base",
            )

    def test_passes_valid_splice(self):
        # No exception = pass
        _validate_splice(
            existing_node_ids={"n_base"},
            splice_nodes=[_valid_splice_node("n_splice")],
            splice_edges=[_back_edge("n_splice", "n_base")],
            after_node_id="n_base",
        )

    def test_passes_multiple_valid_splice_nodes(self):
        nodes = [_valid_splice_node("n_a"), _valid_splice_node("n_b")]
        edges = [
            {"from": "n_a", "to": "n_b"},
            _back_edge("n_a", "n_a"),
            _back_edge("n_b", "n_a"),
        ]
        _validate_splice(
            existing_node_ids={"n_base"},
            splice_nodes=nodes,
            splice_edges=edges,
            after_node_id="n_base",
        )

    def test_missing_node_id_raises(self):
        node = _valid_splice_node()
        del node["id"]
        with pytest.raises(ReplannError, match="missing 'id'"):
            _validate_splice(
                existing_node_ids={"n_base"},
                splice_nodes=[node],
                splice_edges=[],
                after_node_id="n_base",
            )


# ---------------------------------------------------------------------------
# dag_replan integration (file-system based)
# ---------------------------------------------------------------------------

class TestDagReplan:
    def _write_sidecar(self, tmp_path: Path, graph: dict) -> str:
        run_id = str(uuid.uuid4())
        sidecar = tmp_path / f"{run_id}.graph.json"
        sidecar.write_text(json.dumps(graph), encoding="utf-8")
        return run_id

    def test_raises_when_sidecar_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Graph sidecar not found"):
            dag_replan(
                run_id="nonexistent-run",
                splice_nodes=[_valid_splice_node()],
                splice_edges=[_back_edge("n_splice", "n_base")],
                after_node_id="n_base",
                runs_dir=tmp_path,
            )

    def test_rejects_splice_missing_end_condition(self, tmp_path):
        run_id = self._write_sidecar(tmp_path, _base_graph())
        node = _valid_splice_node()
        node["end_condition"] = ""
        with pytest.raises(ReplannError, match="end_condition"):
            dag_replan(
                run_id=run_id,
                splice_nodes=[node],
                splice_edges=[_back_edge("n_splice", "n_base")],
                after_node_id="n_base",
                runs_dir=tmp_path,
            )

    def test_rejects_splice_missing_retry_budget(self, tmp_path):
        run_id = self._write_sidecar(tmp_path, _base_graph())
        with pytest.raises(ReplannError, match="retry back-edge"):
            dag_replan(
                run_id=run_id,
                splice_nodes=[_valid_splice_node()],
                splice_edges=[{"from": "n_splice", "to": "n_base"}],  # no retry_budget
                after_node_id="n_base",
                runs_dir=tmp_path,
            )

    def test_valid_splice_produces_merged_graph(self, tmp_path):
        run_id = self._write_sidecar(tmp_path, _base_graph())
        merged = dag_replan(
            run_id=run_id,
            splice_nodes=[_valid_splice_node("n_splice")],
            splice_edges=[_back_edge("n_splice", "n_base")],
            after_node_id="n_base",
            runs_dir=tmp_path,
        )
        # Merged graph passes validation
        validate_graph(merged)
        # Both nodes present
        ids = {n["id"] for n in merged["nodes"]}
        assert "n_base" in ids
        assert "n_splice" in ids

    def test_sidecar_written_after_replan(self, tmp_path):
        run_id = self._write_sidecar(tmp_path, _base_graph())
        dag_replan(
            run_id=run_id,
            splice_nodes=[_valid_splice_node("n_s")],
            splice_edges=[_back_edge("n_s", "n_base")],
            after_node_id="n_base",
            runs_dir=tmp_path,
        )
        sidecar = tmp_path / f"{run_id}.graph.json"
        written = json.loads(sidecar.read_text())
        ids = {n["id"] for n in written["nodes"]}
        assert "n_s" in ids, "Sidecar should contain spliced node id"

    def test_wires_entry_edge_from_after_node(self, tmp_path):
        run_id = self._write_sidecar(tmp_path, _base_graph())
        merged = dag_replan(
            run_id=run_id,
            splice_nodes=[_valid_splice_node("n_new")],
            splice_edges=[_back_edge("n_new", "n_base")],
            after_node_id="n_base",
            runs_dir=tmp_path,
        )
        # Injected entry edge: n_base → n_new
        edge_pairs = {(e["from"], e["to"]) for e in merged["edges"]}
        assert ("n_base", "n_new") in edge_pairs, "Entry edge should be injected"

    def test_merged_graph_invalid_type_raises(self, tmp_path):
        """If a spliced node has an invalid type the merged validate_graph catches it."""
        run_id = self._write_sidecar(tmp_path, _base_graph())
        bad_node = _valid_splice_node("n_bad")
        bad_node["type"] = "INVALID_TYPE"
        with pytest.raises((ReplannError, GraphValidationError)):
            dag_replan(
                run_id=run_id,
                splice_nodes=[bad_node],
                splice_edges=[_back_edge("n_bad", "n_base")],
                after_node_id="n_base",
                runs_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# Template validation — all three templates must pass validate_graph()
# ---------------------------------------------------------------------------

import importlib.resources
from pathlib import Path as _Path


def _template_path(name: str) -> _Path:
    here = _Path(__file__).parent
    return here / "templates" / name


@pytest.mark.parametrize("template_file", [
    "design_simulate_iterate.json",
    "ecad_firmware_emulate.json",
    "model_cam_post_cost.json",
    "reveng_scan.json",
    "reveng_datasheet.json",
    "reveng_single_photo.json",
])
def test_template_passes_validate_graph(template_file):
    path = _template_path(template_file)
    assert path.exists(), f"Template not found: {path}"
    graph = json.loads(path.read_text(encoding="utf-8"))
    # Should not raise
    validate_graph(graph)


@pytest.mark.parametrize("template_file", [
    "design_simulate_iterate.json",
    "ecad_firmware_emulate.json",
    "model_cam_post_cost.json",
    "reveng_scan.json",
    "reveng_datasheet.json",
    "reveng_single_photo.json",
])
def test_template_all_nodes_have_end_condition(template_file):
    """Style enforcement: all template nodes must declare end_condition."""
    path = _template_path(template_file)
    graph = json.loads(path.read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        ec = node.get("end_condition")
        assert ec and str(ec).strip(), (
            f"Template {template_file!r}: node {node['id']!r} missing end_condition"
        )


@pytest.mark.parametrize("template_file", [
    "design_simulate_iterate.json",
    "ecad_firmware_emulate.json",
    "model_cam_post_cost.json",
    "reveng_scan.json",
    "reveng_datasheet.json",
    "reveng_single_photo.json",
])
def test_template_has_at_least_one_back_edge(template_file):
    """Templates must have at least one retry back-edge (bounded retry semantics)."""
    path = _template_path(template_file)
    graph = json.loads(path.read_text(encoding="utf-8"))
    back_edges = [e for e in graph["edges"] if "retry_budget" in e]
    assert back_edges, f"Template {template_file!r} has no back-edges with retry_budget"


# ---------------------------------------------------------------------------
# Integration smoke: run → replan → resume terminates
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("CHUDWARE_LIBCHUDWARE"),
    reason="CHUDWARE_LIBCHUDWARE not set — skipping live engine integration test",
)
def test_replan_run_resume_terminates(tmp_path):
    """Start a 1-node run, replan to add a splice node, resume, verify termination."""
    from services.dag import executor

    base_graph = _base_graph()

    # Run with a mock agent (no LLM) that always returns done
    def _mock_agent(request: str, context_pack: dict):
        class _R:
            stop_reason = "done"
            result = {"ok": True, "summary": "mock done"}
        return _R()

    result = executor.run(
        base_graph,
        runs_dir=tmp_path,
        agent_fn=_mock_agent,
    )
    assert result["status"] in ("done", "paused"), f"Unexpected status: {result['status']}"

    run_id = result["run_id"]

    # Replan: splice a new verify node after n_base
    splice_node = _valid_splice_node("n_splice_live")
    splice_node["type"] = "design"  # use design (no check_type) so it goes through agent
    splice_node["inputs"] = {"request": "verify the design"}

    merged = dag_replan(
        run_id=run_id,
        splice_nodes=[splice_node],
        splice_edges=[_back_edge("n_splice_live", "n_base")],
        after_node_id="n_base",
        runs_dir=tmp_path,
    )
    validate_graph(merged)

    # Resume with mock agent
    result2 = executor.resume(
        run_id,
        graph_json=merged,
        decision="approve",
        runs_dir=tmp_path,
        agent_fn=_mock_agent,
    )
    assert result2["status"] in ("done", "paused", "failed"), (
        f"Unexpected resume status: {result2['status']}"
    )
