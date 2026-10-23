"""replan.py — dag_replan: splice new nodes into a running (checkpointed) DAG run.

Contract: contracts/dag_schema.md §0 (replan seam).
Called by: services/dag/mcp_tools.py dag_replan MCP tool.

Design intent
-------------
A DAG run is checkpointed as a LangGraph SQLite DB.  `dag_replan` does NOT
re-open the LangGraph state machine mid-flight (LangGraph doesn't support
structural mutations to an in-progress run's compiled graph).  Instead it:

1. Validates the splice (every spliced node must have end_condition + a
   back-edge with retry_budget — the DoD enforcement).
2. Merges the spliced nodes/edges into a NEW graph document (built from the
   persisted .graph.json sidecar).
3. Writes the merged graph back to the sidecar so executor.resume() picks it
   up on the next resume call.
4. Does NOT replay answered gate questions — the interaction ledger lives in
   SqliteSaver checkpoint state and is replayed automatically by
   executor.resume() → gate.run_gate() → interaction.answer().

Splice rules (enforced; raise ReplannError on violation):
  - Every spliced node MUST have end_condition (non-empty string).
  - Every spliced node MUST be referenced as the "from" side of at least one
    back-edge carrying retry_budget.  This forces bounded retry semantics on
    inserted nodes.
  - Spliced node ids must not collide with existing node ids.
  - Splice-point node must exist in the current graph.
  - The merged graph must pass validate_graph() from graph_loader.

Usage
-----
    from services.dag.replan import dag_replan

    new_graph = dag_replan(
        run_id="<uuid4>",
        splice_nodes=[{...}, ...],
        splice_edges=[{...}, ...],  # MUST include back-edges with retry_budget
        after_node_id="n_design",    # splice after this node in the forward path
        runs_dir=Path("..."),        # must match original executor.run() dir
    )
    # Returns the merged graph_json dict (also written to sidecar).
    # Caller should then call executor.resume(run_id, graph_json=new_graph)
    # to continue the run with the new topology.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .executor import default_runs_dir as _default_runs_dir
from .graph_loader import GraphValidationError, validate_graph
from .run_ids import validate_run_id
from services.agent.request_context import in_cell
from .storage import resolve_run

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class ReplannError(ValueError):
    """Raised when the splice is invalid and cannot be applied."""


# ---------------------------------------------------------------------------
# Core validation — splice requirements (DoD enforcement)
# ---------------------------------------------------------------------------

def _validate_splice(
    existing_node_ids: set[str],
    splice_nodes: list[dict[str, Any]],
    splice_edges: list[dict[str, Any]],
    after_node_id: str,
) -> None:
    """Enforce the splice rules.

    Raises ReplannError on any violation.  All checks must pass before
    the merged graph is written.

    Rules:
      1. after_node_id must be in existing graph.
      2. Spliced node ids must not collide with existing node ids.
      3. Every spliced node must have a non-empty end_condition.
      4. Every spliced node must appear as "from" in at least one back-edge
         (edge with "retry_budget" key) in splice_edges.
      5. All splice_edges must reference valid node ids (existing OR spliced).
    """
    if after_node_id not in existing_node_ids:
        raise ReplannError(
            f"after_node_id={after_node_id!r} not found in the current graph. "
            f"Known ids: {sorted(existing_node_ids)}"
        )

    splice_ids: set[str] = set()
    for node in splice_nodes:
        nid = node.get("id")
        if not nid:
            raise ReplannError(f"Spliced node is missing 'id': {node!r}")
        if nid in existing_node_ids:
            raise ReplannError(
                f"Spliced node id {nid!r} collides with an existing node id. "
                "Choose a unique id for each spliced node."
            )
        if nid in splice_ids:
            raise ReplannError(f"Duplicate spliced node id: {nid!r}")
        splice_ids.add(nid)

        end_condition = node.get("end_condition")
        if not end_condition or not str(end_condition).strip():
            raise ReplannError(
                f"Spliced node {nid!r} is missing 'end_condition'. "
                "Every spliced node must declare when it is done."
            )

    # Build set of spliced nodes that appear as back-edge "from"
    back_edge_sources: set[str] = set()
    for edge in splice_edges:
        if "retry_budget" in edge:
            src = edge.get("from")
            if src:
                back_edge_sources.add(src)

    # Check that every spliced node has a retry back-edge
    for nid in splice_ids:
        if nid not in back_edge_sources:
            raise ReplannError(
                f"Spliced node {nid!r} has no retry back-edge (an edge with "
                "'retry_budget' where 'from'={nid!r}). "
                "Every spliced node requires a bounded retry cycle."
            )

    # Validate edge references
    all_ids = existing_node_ids | splice_ids
    for edge in splice_edges:
        frm = edge.get("from")
        to = edge.get("to")
        if frm and frm not in all_ids:
            raise ReplannError(
                f"Splice edge references unknown 'from' node: {frm!r}. "
                f"Known: {sorted(all_ids)}"
            )
        if to and to not in all_ids:
            raise ReplannError(
                f"Splice edge references unknown 'to' node: {to!r}. "
                f"Known: {sorted(all_ids)}"
            )


# ---------------------------------------------------------------------------
# Merge graph — insert splice after after_node_id
# ---------------------------------------------------------------------------

def _merge_graph(
    base_graph: dict[str, Any],
    splice_nodes: list[dict[str, Any]],
    splice_edges: list[dict[str, Any]],
    after_node_id: str,
) -> dict[str, Any]:
    """Insert splice_nodes/edges into base_graph after after_node_id.

    The splice is a linear chain: the first splice node is wired after
    after_node_id.  Edges that originally went FROM after_node_id TO the
    next node are NOT removed (the splice is additive — it inserts an
    intermediate checkpoint, not a replacement).

    Graph topology after splice (forward path):
        ... → after_node_id → splice_nodes[0] → splice_nodes[1] → ...
                         ↘ (original forward edges preserved)

    This is additive: the original edge from after_node_id is kept, and the
    splice creates an additional branch.  The caller is responsible for
    supplying splice_edges that correctly wire the splice chain's forward path.
    """
    import copy
    merged = copy.deepcopy(base_graph)

    # Append spliced nodes
    merged["nodes"] = merged.get("nodes", []) + list(splice_nodes)

    # Find the first splice node to wire the entry point
    first_splice_id = splice_nodes[0]["id"] if splice_nodes else None

    # Wire after_node_id → first splice node (additive forward edge)
    injected_edges: list[dict[str, Any]] = []
    if first_splice_id:
        injected_edges.append({
            "from": after_node_id,
            "to": first_splice_id,
        })

    # Append all splice_edges (forward + back-edges) plus the injected entry edge
    merged["edges"] = merged.get("edges", []) + injected_edges + list(splice_edges)

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dag_replan(
    run_id: str,
    splice_nodes: list[dict[str, Any]],
    splice_edges: list[dict[str, Any]],
    after_node_id: str,
    *,
    runs_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Splice new nodes into a running (checkpointed) DAG run.

    Validates the splice, merges it into the persisted graph sidecar, and
    returns the merged graph_json dict.  The caller must then call
    executor.resume(run_id, graph_json=<returned dict>) to run with the
    new topology.

    The spliced nodes must NOT bypass gate questions already answered by the
    interaction ledger — answered questions are replayed automatically by
    gate.run_gate() on resume, because the answers are stored in the
    SqliteSaver checkpoint, not in this module.

    Args:
        run_id:         UUID4 of the run to replan (must have a .graph.json sidecar).
        splice_nodes:   List of aicad.dag/1 node dicts to insert.
                        Each MUST have: id, type, name, inputs, on_fail, autonomy,
                        AND a non-empty end_condition.
        splice_edges:   List of aicad.dag/1 edge dicts.  MUST include at least one
                        back-edge (edge with "retry_budget") for EACH spliced node.
                        May also include forward edges wiring the splice chain.
        after_node_id:  Node id in the existing graph after which the splice is
                        inserted.  The first splice node is wired to this node.
        runs_dir:       Directory where the checkpoint DB and .graph.json sidecar
                        live.  Defaults to the platform runs dir (mirrors executor).

    Returns:
        The merged aicad.dag/1 graph_json dict (already validated and written
        to the .graph.json sidecar).

    Raises:
        FileNotFoundError: if the run's .graph.json sidecar does not exist.
        ReplannError:      if the splice violates DoD rules (missing end_condition,
                           missing retry back-edge, id collision, etc.).
        GraphValidationError: if the merged graph fails aicad.dag/1 validation.
    """
    if in_cell():
        record = resolve_run(run_id)
        graph_path = record.graph
    else:
        run_id = validate_run_id(run_id)
        base_dir = Path(runs_dir) if runs_dir is not None else _default_runs_dir()
        graph_path = base_dir / f"{run_id}.graph.json"

    if not graph_path.exists():
        if in_cell():
            raise KeyError("not_found")
        raise FileNotFoundError(
            f"Graph sidecar not found: {graph_path}. "
            "Ensure executor.run() was called with this run_id first."
        )

    base_graph = json.loads(graph_path.read_text(encoding="utf-8"))

    existing_node_ids: set[str] = {n["id"] for n in base_graph.get("nodes", [])}

    # 1. Validate splice rules (DoD enforcement — raises ReplannError on failure)
    _validate_splice(existing_node_ids, splice_nodes, splice_edges, after_node_id)

    # 2. Merge the splice into the base graph
    merged_graph = _merge_graph(base_graph, splice_nodes, splice_edges, after_node_id)

    # 3. Validate the merged graph against aicad.dag/1 (raises GraphValidationError)
    validate_graph(merged_graph)

    # 4. Write the merged graph back to the sidecar so executor.resume() picks it up
    graph_path.write_text(json.dumps(merged_graph, indent=2), encoding="utf-8")

    return merged_graph
