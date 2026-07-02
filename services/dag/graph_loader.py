"""graph_loader.py — Validate and build an aicad.dag/1 graph as a LangGraph StateGraph.

Contract: contracts/dag_schema.md
Scaffold: T-A1. Node handler dispatch wired in T-A2.
"""
from __future__ import annotations

import warnings
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph

from . import context_pack as cp
from . import node_runner as nr
from . import autonomy as av
from . import gate as gt

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
# Back-edge (retry cycle) helpers
# ---------------------------------------------------------------------------

def _classify_edges(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict], list[dict]]:
    """Separate edges into forward edges and back-edges (retry cycles).

    A back-edge is one that carries ``retry_budget`` in the edge dict.
    The schema allows extra keys on edges (validate_graph does not reject them),
    and ``retry_budget`` is the signal that a cycle is intentional and bounded.

    Returns:
        (forward_edges, back_edges)
    """
    forward: list[dict] = []
    back: list[dict] = []
    for edge in edges:
        if "retry_budget" in edge:
            back.append(edge)
        else:
            forward.append(edge)
    return forward, back


# ---------------------------------------------------------------------------
# Node handler — real dispatch via node_runner (T-A2)
# ---------------------------------------------------------------------------

def _make_node_handler(
    node: dict[str, Any],
    emit: EmitFn,
    *,
    agent_fn: Any = None,
    initial_retry_budget: int = cp.DEFAULT_RETRY_BUDGET,
    autonomy_default: str = "auto",
) -> Callable[[DagState], DagState]:
    """Return a LangGraph node function with real capability dispatch.

    T-A2 replaces the T-A1 stub: instead of a hardcoded "done" output,
    each node is executed via node_runner.run(), which wraps the agent loop
    with an isolated context pack (inputs, allowed tools, retry budget).

    T-A3 wires real interrupt() for approve/manual autonomy via gate.run_gate().
    The verdict returned by node_runner ("done" | "retry" | "escalate") is
    written into the node state so the conditional router built by
    build_state_graph() can route accordingly.

    Args:
        initial_retry_budget: The retry budget to use on the node's FIRST visit
            (when node_state has no retry_remaining yet). Comes from the back-edge's
            retry_budget field. Subsequent visits read from accumulated node_state.
        autonomy_default: Run-level autonomy floor (from executor.run/resume).
    """
    node_id = node["id"]
    node_type = node["type"]
    node_name = node["name"]
    # Effective autonomy = max(node's declared level, run-level default).
    eff_autonomy = av.effective_autonomy(node, autonomy_default=autonomy_default)

    def _handle(state: DagState) -> DagState:
        # emit is captured via closure — NOT read from state (callables can't be
        # serialised by the LangGraph checkpoint serde).
        _emit: EmitFn = emit

        # --- autonomy gate (T-A3: real interrupt()) --------------------------
        # Events are emitted HERE (before run_gate) so they fire exactly once,
        # on the initial suspending call.  run_gate() calls interrupt() which
        # does NOT return on first call (LangGraph suspends the graph); on resume
        # interrupt() returns the decision without re-triggering the events.
        _run_id = state.get("run_id", "unknown")

        if eff_autonomy == "approve":
            if _emit:
                _emit("dag.node.paused", {
                    "run_id":  _run_id,
                    "node_id": node_id,
                    "reason":  "gate",
                })
                _emit("dag.node.approval_required", {
                    "run_id":  _run_id,
                    "node_id": node_id,
                })
            decision, extra_patch = gt.run_gate(node, state, emit=_emit)
            if decision == gt.DECISION_REJECT:
                # Rejected: mark paused without running the node.
                return {
                    "node_states": {**state.get("node_states", {}),
                                    node_id: {"status": "paused", "output": {},
                                              "verdict": "escalate",
                                              "gate_decision": decision}},
                    "run_status": "paused",
                }
            # "approve" or "modify" → fall through to execution below.
            # For "modify", the user_output override is in extra_patch["user_output"].

        elif eff_autonomy == "manual":
            if _emit:
                _emit("dag.node.manual_required", {
                    "run_id":  _run_id,
                    "node_id": node_id,
                })
                _emit("dag.node.paused", {
                    "run_id":  _run_id,
                    "node_id": node_id,
                    "reason":  "manual",
                })
            decision, extra_patch = gt.run_gate(node, state, emit=_emit)
            if decision == gt.DECISION_REJECT:
                return {
                    "node_states": {**state.get("node_states", {}),
                                    node_id: {"status": "paused", "output": {},
                                              "verdict": "escalate",
                                              "gate_decision": decision}},
                    "run_status": "paused",
                }
            # Approved or modified — continue to agent execution below.

        # --- verify node stub (T-A4 owns real logic) -------------------------
        if node_type == "verify":
            warnings.warn(
                f"Verify node {node_id!r}: T-A4 verifier logic not implemented; "
                "stub returns status='done' with empty output.",
                stacklevel=2,
            )

        # --- gate node (requires approve/manual — already handled above) -----
        if node_type == "gate" and eff_autonomy == "auto":
            warnings.warn(
                f"Gate node {node_id!r} has effective autonomy='auto'; gate nodes should use "
                "'approve' or 'manual'. Running as pass-through.",
                stacklevel=2,
            )

        # --- Assemble context pack ------------------------------------------
        current_node_state: dict[str, Any] = state.get("node_states", {}).get(
            node_id, {}
        )
        # On first visit, node_state has no retry_remaining; seed from the
        # back-edge's retry_budget (passed at graph-build time).  On subsequent
        # visits, the accumulated value in node_state takes over.
        if "retry_remaining" not in current_node_state:
            current_node_state = {**current_node_state, "retry_remaining": initial_retry_budget}
        pack = cp.assemble(node, current_node_state)

        # --- Dispatch via node_runner ----------------------------------------
        verdict, output_state = nr.run(
            node,
            pack,
            emit=_emit,
            agent_fn=agent_fn,
        )

        # Write the verdict into node state so the router can branch.
        node_entry: dict[str, Any] = {
            "status": output_state.get("status", "done"),
            "output": output_state,
            "verdict": verdict,
            "retry_remaining": output_state.get("retry_remaining", 0),
        }

        new_node_states = {**state.get("node_states", {}), node_id: node_entry}

        # Derive run_status update for escalations.
        extra: dict[str, Any] = {}
        if verdict == nr.VERDICT_ESCALATE:
            on_fail = node.get("on_fail", "pause")
            if on_fail == "abort":
                extra["run_status"] = "failed"
            elif on_fail in ("pause", "retry"):
                extra["run_status"] = "paused"
            # on_fail == "skip": leave run_status as-is (execution continues)

        # §14 narration: populate open_questions on escalation
        if verdict == nr.VERDICT_ESCALATE:
            oq = list(state.get("open_questions") or [])
            oq.append({
                "node": node_id,
                "question": (
                    f"Node '{node_name}' escalated after retry budget exhausted. "
                    f"Stop reason: {output_state.get('data', {}).get('stop_reason', 'unknown')}."
                ),
                "blocked_reason": "retry_budget_exhausted",
            })
            extra["open_questions"] = oq

        return {"node_states": new_node_states, **extra}

    _handle.__name__ = node_id
    return _handle


# ---------------------------------------------------------------------------
# Conditional router for retry back-edges
# ---------------------------------------------------------------------------

def _make_retry_router(
    node_id: str,
    forward_target: str,
    back_target: str,
) -> Callable[[DagState], str]:
    """Return a conditional-edge router for a node with a retry back-edge.

    Routes:
      "done"     → forward_target (success path, next node or END sentinel)
      "retry"    → back_target    (the node to re-run — usually the same node
                                   or an upstream fix node)
      "escalate" → forward_target (escalation is handled by the gate node in
                                   the forward path; we advance, not loop)

    The forward_target uses LangGraph's special END sentinel (imported from
    langgraph.graph) when the node has no outgoing forward edges.
    """
    def _route(state: DagState) -> str:
        node_entry = state.get("node_states", {}).get(node_id, {})
        verdict = node_entry.get("verdict", nr.VERDICT_DONE)
        retry_remaining = node_entry.get("retry_remaining", 0)
        if verdict == nr.VERDICT_RETRY and retry_remaining > 0:
            return back_target
        return forward_target

    _route.__name__ = f"_route_{node_id}"
    return _route


# ---------------------------------------------------------------------------
# Graph builder — returns uncompiled StateGraph so executor can attach checkpointer
# ---------------------------------------------------------------------------

def build_state_graph(
    graph_json: dict[str, Any],
    emit: EmitFn = None,
    *,
    agent_fn: Any = None,
    autonomy_default: str = "auto",
) -> StateGraph:
    """Validate and assemble an aicad.dag/1 graph JSON into a LangGraph StateGraph.

    Returns an **uncompiled** StateGraph.  The caller (executor.run) compiles it
    with an attached SqliteSaver checkpointer so intermediate states are persisted.

    Args:
        graph_json:        The graph document (must conform to aicad.dag/1).
        emit:              Optional event callback passed into node handlers.
        agent_fn:          Injectable agent callable for testing (None = live loop).
        autonomy_default:  Run-level autonomy floor — one of "auto", "approve", "manual".
                           Can only RAISE a node's declared level, never lower it.
                           Passed into each node handler via autonomy.effective_autonomy().

    Returns:
        Uncompiled LangGraph StateGraph ready for `.compile(checkpointer=...)`.
    """
    validate_graph(graph_json)

    nodes = graph_json["nodes"]
    edges = graph_json["edges"]

    forward_edges, back_edges = _classify_edges(nodes, edges)

    # Build lookup: node_id → set of back-edge targets (the "from" side loops back
    # to "to").  We index by the SOURCE of the back-edge (the node that may retry).
    # back_edge: {"from": "n_fix", "to": "n_verify", "retry_budget": 2}
    # means n_fix can route back to n_verify on retry.
    back_edge_map: dict[str, list[dict]] = {}
    for be in back_edges:
        src = be["from"]
        if src not in back_edge_map:
            back_edge_map[src] = []
        back_edge_map[src].append(be)

    # Map each node_id to the retry_budget it should receive on first visit.
    # Nodes that are the SOURCE of a back-edge have an explicit budget (from the edge).
    # All other nodes use the DEFAULT_RETRY_BUDGET (1 retry).
    node_retry_budgets: dict[str, int] = {}
    for be in back_edges:
        src = be["from"]
        budget = int(be.get("retry_budget", cp.DEFAULT_RETRY_BUDGET))
        # Take the max if multiple back-edges from the same node (unusual).
        node_retry_budgets[src] = max(node_retry_budgets.get(src, 0), budget)

    sg: StateGraph = StateGraph(DagState)

    # Determine entry nodes (no incoming FORWARD edges — back-edges do not block
    # entry detection so cyclic graphs are handled correctly).
    has_incoming_forward: set[str] = {edge["to"] for edge in forward_edges}
    entry_nodes = [n["id"] for n in nodes if n["id"] not in has_incoming_forward]
    if not entry_nodes:
        raise GraphValidationError(
            "Graph has no entry node (all nodes have incoming forward edges; "
            "check that at least one node has no non-retry incoming edge)."
        )

    # Register node handlers
    for node in nodes:
        budget = node_retry_budgets.get(node["id"], cp.DEFAULT_RETRY_BUDGET)
        sg.add_node(
            node["id"],
            _make_node_handler(
                node, emit,
                agent_fn=agent_fn,
                initial_retry_budget=budget,
                autonomy_default=autonomy_default,
            ),
        )

    # Compute forward successors for each node (for routing).
    forward_successors: dict[str, list[str]] = {}
    for edge in forward_edges:
        src = edge["from"]
        if src not in forward_successors:
            forward_successors[src] = []
        forward_successors[src].append(edge["to"])

    # Register edges — forward edges as static edges; back-edges as conditional
    # edges that check the verdict before deciding whether to loop back.
    registered_conditional: set[str] = set()

    for node in nodes:
        nid = node["id"]
        fwd_targets = forward_successors.get(nid, [])
        be_list = back_edge_map.get(nid, [])

        if not be_list:
            # No back-edges from this node — use static forward edges.
            if fwd_targets:
                for tgt in fwd_targets:
                    sg.add_edge(nid, tgt)
            else:
                sg.add_edge(nid, END)
        else:
            # This node has at least one retry back-edge.
            # Build a conditional router: retry → back_target; else → forward.
            # Only the first back-edge is supported per node in this version
            # (multiple retry loops from one node is an unusual pattern; if
            # needed, file a contract change via T-A2.md questions).
            be = be_list[0]
            back_target = be["to"]
            # The forward target: first forward successor, or END.
            forward_target = fwd_targets[0] if fwd_targets else END

            router = _make_retry_router(nid, forward_target, back_target)

            # add_conditional_edges needs a mapping from router return values
            # to actual node names (or END).
            path_map: dict[str, str] = {
                forward_target if forward_target is not END else "__end__": (
                    forward_target if forward_target is not END else END
                ),
                back_target: back_target,
            }
            # LangGraph expects the values to be real node names or END.
            # Rebuild cleanly:
            if forward_target is END:
                path_map = {"__end__": END, back_target: back_target}

                def _router_end(state: DagState, _nid=nid, _bt=back_target) -> str:
                    entry = state.get("node_states", {}).get(_nid, {})
                    verdict = entry.get("verdict", nr.VERDICT_DONE)
                    rem = entry.get("retry_remaining", 0)
                    if verdict == nr.VERDICT_RETRY and rem > 0:
                        return _bt
                    return "__end__"
                _router_end.__name__ = f"_route_{nid}_end"
                sg.add_conditional_edges(nid, _router_end, path_map)
            else:
                _fwd = forward_target

                def _router_fwd(
                    state: DagState,
                    _nid=nid,
                    _bt=back_target,
                    _fwd=_fwd,
                ) -> str:
                    entry = state.get("node_states", {}).get(_nid, {})
                    verdict = entry.get("verdict", nr.VERDICT_DONE)
                    rem = entry.get("retry_remaining", 0)
                    if verdict == nr.VERDICT_RETRY and rem > 0:
                        return _bt
                    return _fwd

                _router_fwd.__name__ = f"_route_{nid}_fwd"
                sg.add_conditional_edges(nid, _router_fwd, {_fwd: _fwd, back_target: back_target})

            registered_conditional.add(nid)

            # Wire remaining forward successors (if there were multiple) as static.
            for tgt in fwd_targets[1:]:
                sg.add_edge(nid, tgt)

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

    # Nodes with no outgoing forward edges AND no back-edges connect to END.
    # (Already handled above per-node in the loop; this is the safety net for
    # any nodes not processed — e.g. if they only appear as back-edge targets.)
    has_outgoing_forward: set[str] = set(forward_successors.keys())
    for node in nodes:
        nid = node["id"]
        if nid not in has_outgoing_forward and nid not in registered_conditional:
            sg.add_edge(nid, END)

    return sg
