"""graph_loader.py — Validate and build an aicad.dag/1 graph as a LangGraph StateGraph.

Contract: contracts/dag_schema.md
"""
from __future__ import annotations

import operator
import warnings
from typing import Annotated, Any, Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import context_pack as cp
from . import node_runner as nr
from . import autonomy as av
from . import gate as gt
from . import verify as vr
from services.agent.request_context import (
    bind_context, bind_roots, current_context, in_cell, root_value,
)

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
# Type aliases + concurrent-safe state (LangGraph Annotated reducers)
# ---------------------------------------------------------------------------

EmitFn = Optional[Callable[[str, dict[str, Any]], None]]


def _merge_dicts(left: Any, right: Any) -> dict[str, Any]:
    """Reducer for maps written by sibling nodes in one superstep (multi-DL fan-in).

    LangGraph rejects concurrent writes to a bare key with
    INVALID_CONCURRENT_GRAPH_UPDATE. Sibling SPECIFY/BUILD nodes each return a
    partial ``node_states`` patch; this reducer merges them so fan-in to
    ``n_spec_done`` (etc.) does not crash the mission graph.
    """
    if not left:
        return dict(right or {})
    if not right:
        return dict(left or {})
    out = dict(left)
    out.update(right)
    return out


def _last_value(left: Any, right: Any) -> Any:
    """Last-write-wins reducer for scalar status fields under parallel updates."""
    return right if right is not None else left


class DagState(TypedDict, total=False):
    """Mission DAG state with reducers on every key that parallel nodes may write.

    Without Annotated reducers, two parallel ``n_spec_DL*`` completions raise
    ``INVALID_CONCURRENT_GRAPH_UPDATE`` on the shared channel.
    """
    run_id: Annotated[str, _last_value]
    session_id: Annotated[str, _last_value]
    run_status: Annotated[str, _last_value]
    node_states: Annotated[dict[str, Any], _merge_dicts]
    open_questions: Annotated[list[Any], operator.add]


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
# Node handler — real dispatch via node_runner
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

    Each node is executed via node_runner.run(), which wraps the agent loop
    with an isolated context pack (inputs, allowed tools, retry budget).

    Real interrupt() is wired for approve/manual autonomy via gate.run_gate().
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

        # --- autonomy gate (real interrupt()) ---------------------------------
        # NOTE on events: dag.node.paused / dag.node.approval_required /
        # dag.node.manual_required are NOT emitted here.  On resume, LangGraph
        # re-executes this handler from the top before interrupt() returns the
        # decision, so any emit placed before run_gate() double-fires (once on
        # the initial suspend, again on resume).  Instead executor.run()/resume()
        # emit those events exactly once per suspend, from the surfaced
        # '__interrupt__' payloads (which carry node_id + autonomy for this).
        gate_decision: Optional[str] = None
        gate_user_output: Optional[dict[str, Any]] = None

        if eff_autonomy in ("approve", "manual"):
            decision, extra_patch = gt.run_gate(
                node, state, emit=_emit, autonomy=eff_autonomy
            )
            if decision == gt.DECISION_REJECT:
                # Rejected (or unrecognised decision, which fails closed to
                # reject in run_gate): mark paused without running the node.
                return {
                    "node_states": {**state.get("node_states", {}),
                                    node_id: {"status": "paused", "output": {},
                                              "verdict": "escalate",
                                              "gate_decision": decision}},
                    "run_status": "paused",
                }
            gate_decision = decision
            if decision == "modify":
                # User-supplied output override; applied below instead of
                # dispatching the agent.
                gate_user_output = extra_patch.get("user_output", {})
            # "approve" → fall through to agent execution below.

        # --- "modify" decision: user-supplied output override -----------------
        # The user supplied the node's output directly (manual nodes advertise
        # this in the gate proposal; approve nodes may also modify).  Record the
        # node as done with that output — the agent is NOT dispatched, so the
        # override actually takes effect in downstream state.
        if gate_user_output is not None:
            prev_ns = state.get("node_states", {}).get(node_id, {})
            retry_rem = prev_ns.get("retry_remaining", initial_retry_budget)
            output_state = {
                "status": "done",
                "summary": (
                    f"Node '{node_name}': user-supplied output override "
                    "(gate decision 'modify'); agent execution skipped."
                ),
                "data": gate_user_output,
                "retry_remaining": retry_rem,
                "doc_id": node.get("inputs", {}).get("doc_id"),
            }
            node_entry = {
                "status": "done",
                "output": output_state,
                "verdict": nr.VERDICT_DONE,
                "retry_remaining": retry_rem,
                "gate_decision": "modify",
            }
            if _emit:
                _emit("dag.node.done", {
                    "node_id": node_id,
                    "output": output_state,
                })
            return {
                "node_states": {**state.get("node_states", {}),
                                node_id: node_entry},
            }

        # --- verify node (deterministic dispatch via verify.run) --------------
        # Only intercept verify nodes that declare a check_type in inputs.
        # Without check_type the node falls through to node_runner (agent path)
        # for backward compatibility with agent-driven verify nodes.
        if node_type == "verify" and node.get("inputs", {}).get("check_type"):
            current_node_state_v = state.get("node_states", {}).get(node_id, {})
            if "retry_remaining" not in current_node_state_v:
                current_node_state_v = {**current_node_state_v,
                                         "retry_remaining": initial_retry_budget}
            pack_v = cp.assemble(node, current_node_state_v)
            rid_v = state.get("run_id")
            if rid_v and not pack_v.get("run_id"):
                pack_v = dict(pack_v)
                pack_v["run_id"] = rid_v
            if state.get("session_id"):
                pack_v["session_id"] = state["session_id"]
            verdict_v, output_v = vr.run(node, pack_v, emit=_emit)
            node_entry_v: dict[str, Any] = {
                "status": output_v.get("status", "done"),
                "output": output_v,
                "verdict": verdict_v,
            }
            return {
                "node_states": {**state.get("node_states", {}),
                                node_id: node_entry_v},
            }

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
        # Seed run_id from LangGraph state so agent_turn op-vocab
        # cheatsheet keys once-per-mission, overseer/leaf telemetry share the
        # same id, and context_pack.get("run_id") is never silently None.
        rid = state.get("run_id")
        if rid and not pack.get("run_id"):
            pack = dict(pack)
            pack["run_id"] = rid
        if state.get("session_id"):
            pack["session_id"] = state["session_id"]

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
        if gate_decision is not None:
            node_entry["gate_decision"] = gate_decision

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

    if in_cell():
        context = current_context()
        project = root_value("CHUDWARE_PROJECT_DIR", "/work")
        save = root_value("CHUDWARE_PROJECT_SAVE_DIR", project)

        def _owned_handle(state: DagState) -> DagState:
            with bind_context(context), bind_roots(project, save):
                return _handle(state)

        _owned_handle.__name__ = node_id
        return _owned_handle
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
            # needed, propose a contract change).
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
        # Multiple entry nodes: create a synthetic START fan-out
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
