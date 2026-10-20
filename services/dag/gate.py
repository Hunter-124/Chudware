"""gate.py — Human-in-the-loop gate node for the aicad.dag/1 orchestrator.

Contract: contracts/dag_schema.md §6 (autonomy levels), §11 (events).
Interaction shape: contracts/interaction.md §1 (kind=approval), §3 (batch envelope).
Wired by: T-A3. Migration seam for T-F4 (thin adapter, not a rewrite).

The gate node calls LangGraph interrupt() to suspend execution; the user resumes via
executor.resume(run_id, decision).  The decision is one of:
  "approve"  — proceed with the node as planned
  "reject"   — mark the node paused; the run stays paused (node is not executed)
  "modify"   — complete the node with a user-supplied output override (carried in
               the decision dict; the agent is not dispatched)
Any other value FAILS CLOSED: it is treated as "reject" (with a warning) — an
unrecognised decision must never be treated as consent on a safety gate.

LangGraph 1.x behaviour (verified empirically, 2026-07-02):
  - interrupt() RETURNS the Command(resume=...) value when resumed; it does NOT raise.
  - invoke() returns a state dict with an '__interrupt__' key when suspended.
  - Resume: compiled.invoke(Command(resume=<decision>), config=config)
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from langgraph.types import interrupt

# Interaction contract (contracts/interaction.md §5 replay, §7 events).
# Lazy import at call time to keep gate.py importable without the full agent stack;
# tests that mock run_gate() can inject a ledger directly.
try:
    from services.agent.interaction import FileLedger, MemoryLedger
except ImportError:
    from services.agent.interaction import FileLedger, MemoryLedger  # type: ignore

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
    autonomy: Optional[str] = None,
) -> dict[str, Any]:
    """Build the interrupt payload shaped as an aicad.interaction/1 approval batch.

    The shape follows contracts/interaction.md §1 (kind=approval) and §3 (batch
    envelope) so T-F4's migration to the full ask_user transport is a thin adapter.

    Args:
        node:      The DAG node dict (id, name, type, inputs, autonomy, …).
        run_id:    Current run UUID (for the batch scope).
        batch_id:  Stable batch id (generated if not supplied).
        autonomy:  EFFECTIVE autonomy level (node level raised by the run-level
                   default). Falls back to the node's declared level if omitted.

    Returns:
        A dict conforming to aicad.interaction/1 with a single approval question,
        plus top-level ``node_id`` and ``autonomy`` keys (extra keys are allowed
        by the contract) so executor.run()/resume() can emit the §11 pause events
        exactly once per suspend from the surfaced interrupt payload.
    """
    batch_id = batch_id or f"b_{uuid.uuid4().hex[:12]}"
    node_id   = node["id"]
    node_name = node.get("name", node_id)
    node_type = node.get("type", "unknown")
    autonomy  = autonomy or node.get("autonomy", "approve")

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
        "node_id":   node_id,     # for executor-level §11 event emission (dedupe)
        "autonomy":  autonomy,    # effective level; picks paused-reason/manual events
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


def _record_gate_answer(ledger, question_id: str, raw_value: Any, decision_str: str) -> None:
    """Persist the gate decision to the interaction ledger (contract §5).

    Stores a boolean approval answer (kind=approval value shape) so replay logic
    in both run_gate() and ask_user() can skip re-asking if the same gate question
    id appears again (e.g. via intake tool or a second agent pass).
    Best-effort: a ledger write failure must never block the gate.
    """
    import time as _time
    try:
        ledger.record_answer(question_id, {
            "value": raw_value,
            "source": "user",
            "at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        })
    except Exception:  # noqa: BLE001
        pass


_UNKNOWN_RUN_ID = "unknown"


def _gate_ledger(run_id: str, ledger=None):
    """Return the interaction ledger for this run (injectable for tests).

    Uses FileLedger (run-scoped JSONL) when run_id is a real id so gate answers
    written by intake tools (via ask_user with scope='run') replay here without
    re-asking.  Falls back to MemoryLedger when run_id is the 'unknown' sentinel
    (state not yet populated) or when FileLedger init fails — so a transient
    storage failure never blocks the gate, and cross-run contamination is avoided.
    Pass a ledger directly in tests for hermeticity.
    """
    from services.agent.request_context import in_cell
    if in_cell():
        from .storage import resolve_run
        record = resolve_run(run_id)
        return FileLedger(record.run_id, path=record.root / "interaction.jsonl")
    if ledger is not None:
        return ledger
    if not run_id or run_id == _UNKNOWN_RUN_ID:
        return MemoryLedger()
    try:
        return FileLedger(run_id)
    except Exception:  # noqa: BLE001 — never block the gate on a ledger init failure
        return MemoryLedger()


def run_gate(
    node: dict[str, Any],
    state: DagState,
    emit: EmitFn = None,
    autonomy: Optional[str] = None,
    ledger=None,
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
        node:     The node dict.
        state:    Current DagState (used for run_id and emit).
        emit:     Optional event callback (currently unused here — see the
                  emit-dedupe note below; kept for signature stability).
        autonomy: EFFECTIVE autonomy level from graph_loader (node level raised
                  by the run-level default). Carried in the interrupt payload so
                  the executor can emit the right pause events.

    Returns:
        (decision_str, extra_state_patch) — decision_str is DECISION_APPROVE,
        DECISION_REJECT, or "modify"; extra_state_patch carries any user-supplied
        output override (empty dict for approve/reject).
    """
    run_id  = state.get("run_id", "unknown")
    node_id = node["id"]

    # Build the interaction payload (aicad.interaction/1 shape for T-F4 compat).
    payload = make_gate_payload(node, run_id, autonomy=autonomy)
    question_id = payload["questions"][0]["id"]  # "gate.<node_id>"

    # --- Ledger replay (contracts/interaction.md §5) ----------------------
    # Before calling interrupt(), check whether this gate question was already
    # answered in the run's interaction ledger (e.g. an intake tool recorded
    # an approval for this gate id via ask_user(scope="run")).  If yes, skip
    # the interrupt() entirely so the run never re-asks.
    #
    # This is approach (a) from work/questions/T-F4.md: an explicit replay-check
    # here rather than wrapping interrupt() as an ask_user transport, which would
    # risk re-entering _emit() and breaking the event-dedup contract (T-A3).
    led = _gate_ledger(run_id, ledger)
    prior = led.answered(question_id)
    if prior is not None:
        # Already answered — return the stored decision without suspending.
        raw = prior.get("value")
        if raw is None or raw is True or raw == DECISION_APPROVE:
            return DECISION_APPROVE, {}
        if raw is False or raw == DECISION_REJECT:
            return DECISION_REJECT, {}
        if isinstance(raw, dict) and raw.get("action") == "modify":
            return "modify", {"user_output": raw.get("output", {})}
        # Unknown stored value: fail closed.
        return DECISION_REJECT, {}

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
    # the initial run (before suspending) and on resume — LangGraph re-executes
    # the node handler from the top on resume, so ANY emit placed above the
    # interrupt() call double-fires.  Events emitted AFTER interrupt() only
    # fire on resume.
    #
    # Dedupe strategy (empirically forced by the above): neither run_gate()
    # nor graph_loader._make_node_handler() emits dag.node.paused /
    # dag.node.approval_required / dag.node.manual_required.  Instead,
    # executor.run()/resume() emit them exactly once per suspend, from the
    # surfaced '__interrupt__' payloads — which carry top-level node_id and
    # autonomy keys for that purpose (see make_gate_payload()).
    decision = interrupt(payload)

    # Normalise the decision.
    import time as _time
    if decision is None or decision is True or decision == DECISION_APPROVE:
        _record_gate_answer(led, question_id, True, DECISION_APPROVE)
        return DECISION_APPROVE, {}
    if decision is False or decision == DECISION_REJECT:
        _record_gate_answer(led, question_id, False, DECISION_REJECT)
        return DECISION_REJECT, {}
    if isinstance(decision, dict) and decision.get("action") == "modify":
        _record_gate_answer(led, question_id, decision, "modify")
        return "modify", {"user_output": decision.get("output", {})}

    # Unknown value — FAIL CLOSED.  This is a human-approval safety gate:
    # an unrecognised decision must never be treated as consent.  Reject
    # (the caller re-pauses the node) and keep the warning for diagnosis.
    import warnings
    _record_gate_answer(led, question_id, decision, DECISION_REJECT)
    warnings.warn(
        f"Gate node {node_id!r}: unrecognised decision {decision!r}; "
        "treating as reject (fail-closed).",
        stacklevel=2,
    )
    return DECISION_REJECT, {}
