"""gate.py — Human-in-the-loop gate node for the aicad.dag/1 orchestrator.

Contract: contracts/dag_schema.md §6 (autonomy levels), §11 (events).
Interaction shape: contracts/interaction.md §1 (kind=approval), §3 (batch envelope).
Wired by: T-A3. Migration seam for T-F4 (thin adapter, not a rewrite).

The gate node calls LangGraph interrupt() to suspend execution; the user resumes via
executor.resume(run_id, decision).  The decision is one of:
  "approve"  — proceed with the node as planned
  "reject"   — mark the node failed (on_fail disposition applies)
  "modify"   — proceed with a user-supplied output override (carried in decision dict)

LangGraph 1.x behaviour (verified empirically, 2026-07-02):
  - interrupt() RETURNS the Command(resume=...) value when resumed; it does NOT raise.
  - invoke() returns a state dict with an '__interrupt__' key when suspended.
  - Resume: compiled.invoke(Command(resume=<decision>), config=config)
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from langgraph.types import interrupt

# Local type aliases (avoid circular import with graph_loader).
# DagState and EmitFn are also defined in graph_loader; use the same shapes.
DagState = dict  # dict[str, Any]
EmitFn = object  # Optional[Callable[[str, dict], None]]

# Decision constants (public API consumed by executor.resume and tests)
DECISION_APPROVE = "approve"
DECISION_REJECT  = "reject"
# "modify" is carried as a dict: {"action": "modify", "output": {...}}


def make_gate_payload(
    node: dict[str, Any],
    run_id: str,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build the interrupt payload shaped as an aicad.interaction/1 approval batch.

    The shape follows contracts/interaction.md §1 (kind=approval) and §3 (batch
    envelope) so T-F4's migration to the full ask_user transport is a thin adapter.

    Args:
        node:      The DAG node dict (id, name, type, inputs, autonomy, …).
        run_id:    Current run UUID (for the batch scope).
        batch_id:  Stable batch id (generated if not supplied).

    Returns:
        A dict conforming to aicad.interaction/1 with a single approval question.
    """
    batch_id = batch_id or f"b_{uuid.uuid4().hex[:12]}"
    node_id   = node["id"]
    node_name = node.get("name", node_id)
    node_type = node.get("type", "unknown")
    autonomy  = node.get("autonomy", "approve")

    # Build the proposal text.  For "manual" nodes include a note that the user
    # can supply output directly.
    if autonomy == "manual":
        proposal = (
            f"Node '{node_name}' ({node_type}) requires manual input. "
            "Approve to proceed with agent execution, or supply output directly."
        )
    else:
        proposal = (
            f"Node '{node_name}' ({node_type}) is about to execute. "
            "Approve to proceed, or reject to pause the run."
        )

    return {
        "schema":    "aicad.interaction/1",
        "batch_id":  batch_id,
        "scope":     "run",
        "run_id":    run_id,
        "blocking":  True,
        "questions": [
            {
                "id":        f"gate.{node_id}",      # stable ledger key
                "kind":      "approval",
                "prompt":    f"Approve execution of '{node_name}'?",
                "proposal":  proposal,
                "suggested": True,                    # required non-null (contract §2)
                "why": (
                    f"This node has autonomy='{autonomy}' and requires human sign-off "
                    "before the agent proceeds."
                ),
            }
        ],
    }


def run_gate(
    node: dict[str, Any],
    state: DagState,
    emit: EmitFn = None,
) -> tuple[str, dict[str, Any]]:
    """Execute a gate check-point inline inside a node handler.

    This function is called from graph_loader._make_node_handler() when the node
    has autonomy='approve' or autonomy='manual' (or type='gate').  It calls
    interrupt() and returns the human decision to the caller.

    Behaviour:
      - First call (fresh run): interrupt() suspends; executor.run() returns with
        '__interrupt__' in the state.  The node handler never returns to LangGraph.
      - On resume: interrupt() returns the decision value passed via
        Command(resume=decision).  The handler continues normally.

    Args:
        node:  The node dict.
        state: Current DagState (used for run_id and emit).
        emit:  Optional event callback.

    Returns:
        (decision_str, extra_state_patch) — decision_str is DECISION_APPROVE,
        DECISION_REJECT, or "modify"; extra_state_patch carries any user-supplied
        output override (empty dict for approve/reject).
    """
    run_id  = state.get("run_id", "unknown")
    node_id = node["id"]

    # Build the interaction payload (aicad.interaction/1 shape for T-F4 compat).
    payload = make_gate_payload(node, run_id)

    # --- LangGraph interrupt() -------------------------------------------
    # Empirically verified (LangGraph 1.2.7, 2026-07-02):
    #   - interrupt() does NOT raise on first call; invoke() RETURNS with
    #     {"__interrupt__": [Interrupt(value=payload)]} in the state.
    #   - The node function body BELOW interrupt() is NOT reached on the first
    #     (suspending) call — LangGraph snapshots state at the interrupt() call
    #     site and returns immediately to the caller.
    #   - On resume (Command(resume=decision)), interrupt() returns `decision`
    #     and execution continues normally from the line after interrupt().
    #
    # Consequence for emit: events emitted BEFORE interrupt() fire on BOTH
    # the initial run (before suspending) and on resume (before the decision
    # is returned).  Events emitted AFTER interrupt() only fire on resume.
    #
    # The caller (graph_loader._make_node_handler) emits dag.node.paused and
    # dag.node.approval_required BEFORE calling run_gate() so they fire exactly
    # once, on the initial suspending call.  run_gate() itself does not re-emit
    # them to avoid double-emission on resume.
    decision = interrupt(payload)

    # Normalise the decision.
    if decision is None or decision is True or decision == DECISION_APPROVE:
        return DECISION_APPROVE, {}
    if decision is False or decision == DECISION_REJECT:
        return DECISION_REJECT, {}
    if isinstance(decision, dict) and decision.get("action") == "modify":
        return "modify", {"user_output": decision.get("output", {})}

    # Unknown value — treat as approve (safe default, logged).
    import warnings
    warnings.warn(
        f"Gate node {node_id!r}: unrecognised decision {decision!r}; treating as approve.",
        stacklevel=2,
    )
    return DECISION_APPROVE, {}
