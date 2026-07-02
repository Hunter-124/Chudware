"""graph_loader.py — Validate and build an aicad.dag/1 graph as a LangGraph StateGraph.

Contract: contracts/dag_schema.md
Scaffold: T-A1. Node handler dispatch wired in T-A2.
"""
from __future__ import annotations

import warnings
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_STRING = "aicad.dag/1"

VALID_NODE_TYPES = frozenset(
    ["plan", "design", "ecad", "firmware", "structural",
     "source", "simulate", "verify", "gate", "report"]
)
VALID_AUTONOMY = frozenset(["auto", "approve", "manual"])
VALID_ON_FAIL = frozenset(["pause", "abort", "skip", "retry"])


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

EmitFn = Optional[Callable[[str, dict[str, Any]], None]]

# DAG state is a plain dict; LangGraph merges updates returned by node handlers.
DagState = dict[str, Any]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class GraphValidationError(ValueError):
    """Raised when the graph JSON does not conform to aicad.dag/1."""


def validate_graph(graph_json: dict[str, Any]) -> None:
    """Validate a graph document against the aicad.dag/1 schema.

    Raises GraphValidationError on any violation.  A conforming consumer MUST
    reject documents where 'schema' is absent or not 'aicad.dag/1'.
    """
    if graph_json.get("schema") != SCHEMA_STRING:
        raise GraphValidationError(
            f"Missing or wrong schema string; expected {SCHEMA_STRING!r}, "
            f"got {graph_json.get('schema')!r}"
        )
    if not graph_json.get("id"):
        raise GraphValidationError("Graph 'id' is required.")
    nodes = graph_json.get("nodes")
    if not nodes or not isinstance(nodes, list):
        raise GraphValidationError("'nodes' must be a non-empty list.")
    edges = graph_json.get("edges")
    if edges is None or not isinstance(edges, list):
        raise GraphValidationError("'edges' must be a list (may be empty).")

    node_ids: set[str] = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            raise GraphValidationError(f"Node missing 'id': {n!r}")
        if nid in node_ids:
            raise GraphValidationError(f"Duplicate node id: {nid!r}")
        node_ids.add(nid)
        if n.get("type") not in VALID_NODE_TYPES:
            raise GraphValidationError(
                f"Node {nid!r}: invalid type {n.get('type')!r}; "
                f"expected one of {sorted(VALID_NODE_TYPES)}"
            )
        if not n.get("name"):
            raise GraphValidationError(f"Node {nid!r}: 'name' is required.")
        if not isinstance(n.get("inputs"), dict):
            raise GraphValidationError(f"Node {nid!r}: 'inputs' must be an object.")
        if n.get("on_fail") not in VALID_ON_FAIL:
            raise GraphValidationError(
                f"Node {nid!r}: invalid on_fail {n.get('on_fail')!r}; "
                f"expected one of {sorted(VALID_ON_FAIL)}"
            )
        if n.get("autonomy") not in VALID_AUTONOMY:
            raise GraphValidationError(
                f"Node {nid!r}: invalid autonomy {n.get('autonomy')!r}; "
                f"expected one of {sorted(VALID_AUTONOMY)}"
            )

    for edge in edges:
        if edge.get("from") not in node_ids:
            raise GraphValidationError(
                f"Edge references unknown 'from' node: {edge.get('from')!r}"
            )
        if edge.get("to") not in node_ids:
            raise GraphValidationError(
                f"Edge references unknown 'to' node: {edge.get('to')!r}"
            )


# ---------------------------------------------------------------------------
# Node handler stubs (T-A2 replaces with real capability dispatch)
# ---------------------------------------------------------------------------

def _make_node_handler(
    node: dict[str, Any],
    emit: EmitFn,
) -> Callable[[DagState], DagState]:
    """Return a LangGraph node function that stubs execution for the T-A1 scaffold.

    T-A2 replaces this with real capability dispatch per node type.
    """
    node_id = node["id"]
    node_type = node["type"]
    node_name = node["name"]
    autonomy = node.get("autonomy", "auto")

    def _handle(state: DagState) -> DagState:
        # emit is captured via closure — NOT read from state (callables can't be
        # serialised by the LangGraph checkpoint serde).
        _emit: EmitFn = emit

        # --- autonomy gate ---------------------------------------------------
        if autonomy == "approve":
            # T-A3 wires real interrupt(); stub = pass-through with warning
            warnings.warn(
                f"Node {node_id!r} autonomy='approve': T-A3 interrupt() not yet wired; "
                "proceeding without pause (scaffold stub).",
                stacklevel=2,
            )
            if _emit:
                _emit("dag.node.approval_required", {"node_id": node_id})

        elif autonomy == "manual":
            if _emit:
                _emit("dag.node.manual_required", {"node_id": node_id})
            return {
                "node_states": {**state.get("node_states", {}),
                                node_id: {"status": "paused", "output": {}}},
                "run_status": "paused",
            }

        # --- verify node stub (T-A4 owns real logic) -------------------------
        if node_type == "verify":
            warnings.warn(
                f"Verify node {node_id!r}: T-A4 verifier logic not implemented; "
                "stub returns status='done' with empty output.",
                stacklevel=2,
            )

        # --- gate node (requires approve/manual — already handled above) -----
        if node_type == "gate" and autonomy == "auto":
            warnings.warn(
                f"Gate node {node_id!r} has autonomy='auto'; gate nodes should use "
                "'approve' or 'manual'. Running as pass-through.",
                stacklevel=2,
            )

        # --- emit start event ------------------------------------------------
        if _emit:
            _emit("dag.node.started", {
                "node_id": node_id,
                "node_type": node_type,
                "node_name": node_name,
            })

        # --- scaffold: stub output -------------------------------------------
        output: dict[str, Any] = {
            "status": "done",
            "doc_id": node.get("inputs", {}).get("doc_id"),
            "summary": f"[stub] {node_name} completed.",
            "data": {},
        }

        if _emit:
            _emit("dag.node.done", {"node_id": node_id, "output": output})

        return {
            "node_states": {**state.get("node_states", {}),
                            node_id: {"status": "done", "output": output}},
        }

    _handle.__name__ = node_id
    return _handle


# ---------------------------------------------------------------------------
# Graph builder — returns uncompiled StateGraph so executor can attach checkpointer
# ---------------------------------------------------------------------------

def build_state_graph(
    graph_json: dict[str, Any],
    emit: EmitFn = None,
) -> StateGraph:
    """Validate and assemble an aicad.dag/1 graph JSON into a LangGraph StateGraph.

    Returns an **uncompiled** StateGraph.  The caller (executor.run) compiles it
    with an attached SqliteSaver checkpointer so intermediate states are persisted.

    Args:
        graph_json: The graph document (must conform to aicad.dag/1).
        emit:       Optional event callback passed into node stubs.

    Returns:
        Uncompiled LangGraph StateGraph ready for `.compile(checkpointer=...)`.
    """
    validate_graph(graph_json)

    nodes = graph_json["nodes"]
    edges = graph_json["edges"]

    sg: StateGraph = StateGraph(DagState)

    # Determine entry nodes (no incoming edges)
    has_incoming: set[str] = {edge["to"] for edge in edges}
    entry_nodes = [n["id"] for n in nodes if n["id"] not in has_incoming]
    if not entry_nodes:
        raise GraphValidationError(
            "Graph has no entry node (cycle detected or all nodes have incoming edges)."
        )

    # Register node handlers
    for node in nodes:
        sg.add_node(node["id"], _make_node_handler(node, emit))

    # Register edges
    for edge in edges:
        sg.add_edge(edge["from"], edge["to"])

    # Wire entry point (LangGraph supports a single entry; if multiple, chain via a fan-out)
    if len(entry_nodes) == 1:
        sg.set_entry_point(entry_nodes[0])
    else:
        # Multiple entry nodes: create a synthetic START fan-out (scaffold; T-A2 may refine)
        warnings.warn(
            f"Graph has {len(entry_nodes)} entry nodes {entry_nodes!r}; "
            "only the first is used as the LangGraph entry point in this scaffold.",
            stacklevel=2,
        )
        sg.set_entry_point(entry_nodes[0])

    # Nodes with no outgoing edges connect to END
    has_outgoing: set[str] = {edge["from"] for edge in edges}
    for node in nodes:
        nid = node["id"]
        if nid not in has_outgoing:
            sg.add_edge(nid, END)

    return sg
