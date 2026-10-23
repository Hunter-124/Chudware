"""replan.py — ``aicad.replan/1`` approval routing (contract §17).

The flexibility valve: any role, at any phase, may propose changing the process
instead of grinding a failed path.  This module owns the DETERMINISTIC part of the
valve — *who approves* a replan (by scope) and *what happens* once a verdict is
in.  The actual approval decision (an orchestrator/overseer/user judgement) is the
caller's; this module routes the request to the right approver and applies the
outcome to durable state.

Routing table (contract §17, harness-enforced):

  ┌───────────────┬──────────────────────────────────────────────────────────┐
  │ scope         │ approver(s)                                                │
  ├───────────────┼──────────────────────────────────────────────────────────┤
  │ approach      │ orchestrator (how a node is built; spec untouched)         │
  │ buildgraph    │ orchestrator + overseer audit (order/structure)            │
  │ spec          │ overseer, AND user if any acceptance criterion changes     │
  │ requirement   │ user (always)                                              │
  │ org  (1.3)    │ orchestrator + overseer audit (re-charter the org)         │
  └───────────────┴──────────────────────────────────────────────────────────┘

On APPROVED: the splice/edit is applied (buildgraph/approach → ``dag_replan`` +
buildgraph edit; spec → mark_faulty change plan) and the outcome is a ledger
event.  On DENIED: the request is returned with a reason the role can act on.  A
``blocked``-urgency request freezes ONLY the requesting node's branch until ruled
on — it never continues down the failed path (the anti-grind rule).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA = "aicad.replan/1"

VALID_SCOPES = frozenset({"approach", "buildgraph", "spec", "requirement", "org"})
VALID_URGENCY = frozenset({"opportunistic", "blocked"})


class ReplanError(ValueError):
    """Raised when a replan request is structurally invalid (cannot be routed)."""


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    """Who must approve a replan, derived from its scope (contract §17).

    ``approvers``       — ordered approver roles (``orchestrator`` / ``overseer`` /
                          ``user``); ALL listed must approve.
    ``overseer_audit``  — True when the scope triggers an overseer audit
                          (buildgraph, spec, org).
    ``user_required``   — True when the user must approve (requirement always;
                          spec only if an acceptance criterion changes).
    ``reason``          — a short explanation of the routing (for the ledger event).
    """
    scope: str
    approvers: tuple[str, ...]
    overseer_audit: bool
    user_required: bool
    reason: str


def route_replan(request: dict[str, Any]) -> RoutingDecision:
    """Compute the :class:`RoutingDecision` for a replan request per §17.

    Raises :class:`ReplanError` if the request is not routable (bad scope).
    """
    errs = validate_replan_request(request)
    if errs:
        raise ReplanError(f"cannot route invalid replan request: {errs}")

    scope = request["scope"]
    proposal = request.get("proposal") or {}

    if scope == "approach":
        return RoutingDecision(
            scope, ("orchestrator",), overseer_audit=False, user_required=False,
            reason="approach change (how a node is built; spec untouched) — "
                   "orchestrator approves")

    if scope == "buildgraph":
        return RoutingDecision(
            scope, ("orchestrator", "overseer"), overseer_audit=True,
            user_required=False,
            reason="buildgraph change (order/structure) — orchestrator approves "
                   "with an overseer audit")

    if scope == "spec":
        criteria_change = bool(_criteria_changed(proposal))
        approvers = ("overseer", "user") if criteria_change else ("overseer",)
        reason = ("spec change moving an acceptance criterion — overseer + user "
                  "(a criterion change is a promise to the user)") if criteria_change \
            else "spec change (criteria unchanged) — overseer approves"
        return RoutingDecision(
            scope, approvers, overseer_audit=True, user_required=criteria_change,
            reason=reason)

    if scope == "org":  # 1.3: org re-charter
        return RoutingDecision(
            scope, ("orchestrator", "overseer"), overseer_audit=True,
            user_required=False,
            reason="org re-charter (scope=org) — orchestrator approves with an "
                   "overseer audit")

    # requirement
    return RoutingDecision(
        scope, ("user",), overseer_audit=False, user_required=True,
        reason="requirement change — always the user (it changes what gets built)")


def _criteria_changed(proposal: dict[str, Any]) -> bool:
    """True when a spec-scope proposal moves an acceptance criterion.

    A spec change touches criteria if it carries ``spec_changes`` that reference an
    AC, or an explicit ``requirement_changes`` entry.  The §17 example: a spec_writer
    proposing "PETG lid or cantilever redesign" changes the snap-engagement criterion.
    """
    for sc in proposal.get("spec_changes") or []:
        if isinstance(sc, dict) and (sc.get("changes_criteria") or sc.get("ac_ref")):
            return True
    return bool(proposal.get("requirement_changes"))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_replan_request(request: dict[str, Any]) -> list[str]:
    """Structural validation of an ``aicad.replan/1`` request.  ``[]`` = valid."""
    errs: list[str] = []
    if not isinstance(request, dict):
        return ["replan request must be a JSON object"]
    if request.get("schema") != SCHEMA:
        errs.append(f"schema is {request.get('schema')!r}, expected {SCHEMA!r}")
    if not request.get("id"):
        errs.append("missing 'id'")
    if not request.get("from_role"):
        errs.append("missing 'from_role'")
    scope = request.get("scope")
    if scope not in VALID_SCOPES:
        errs.append(f"scope {scope!r} not in {sorted(VALID_SCOPES)}")
    if not str(request.get("reason") or "").strip():
        errs.append("missing 'reason'")
    urgency = request.get("urgency")
    if urgency not in VALID_URGENCY:
        errs.append(f"urgency {urgency!r} not in {sorted(VALID_URGENCY)}")
    return errs


# ---------------------------------------------------------------------------
# Outcome application
# ---------------------------------------------------------------------------

@dataclass
class ReplanOutcome:
    """Result of ruling on and, when approved, applying a replan request."""
    approved: bool
    scope: str
    applied: Optional[dict[str, Any]] = None
    ledger_event: dict[str, Any] = field(default_factory=dict)
    returned_reason: str = ""
    frozen_branch: Optional[str] = None
    outcome_kind: str = "ok"
    status: str = "completed"
    executed: bool = True


def apply_replan(
    request: dict[str, Any],
    *,
    approved: bool,
    ledger: Any = None,
    run_id: Optional[str] = None,
    after_node_id: Optional[str] = None,
    runs_dir: Any = None,
    denial_reason: str = "",
) -> ReplanOutcome:
    """Apply the verdict on a replan request.

    On APPROVED:
      * ``approach`` / ``buildgraph`` / ``org`` → splice the proposal's
        ``buildgraph_changes`` into the running DAG via ``services.dag.replan.
        dag_replan`` (structural edit).  Requires ``run_id`` + ``after_node_id``.
      * ``spec`` → compute the impact of the spec change via
        ``services.agent.derivation.mark_faulty`` (the derivation-tree change plan)
        so downstream dims/nodes/checks are re-planned, not silently poisoned.
      * ``requirement`` → recorded; the manifest update is the caller's (it changes
        the definition of done).

    On DENIED: returns the request with ``denial_reason`` for the role.

    Every outcome is a ledger event (actor = ``from_role``, kind = ``replan``).
    ``blocked`` urgency freezes the requesting node's branch (``frozen_branch``).
    """
    errs = validate_replan_request(request)
    if errs:
        raise ReplanError(f"invalid replan request: {errs}")

    scope = request["scope"]
    from_role = request["from_role"]
    urgency = request.get("urgency", "opportunistic")
    proposal = request.get("proposal") or {}
    frozen = request.get("node_id") if urgency == "blocked" else None

    if not approved:
        outcome = ReplanOutcome(
            approved=False, scope=scope,
            returned_reason=denial_reason or "denied (no reason given)",
            frozen_branch=frozen,
            ledger_event={
                "schema": SCHEMA, "id": request.get("id"), "scope": scope,
                "from_role": from_role, "verdict": "denied",
                "reason": denial_reason,
            },
        )
        _log(ledger, from_role, outcome.ledger_event)
        return outcome

    applied: Optional[dict[str, Any]] = None

    if scope in ("approach", "buildgraph", "org"):
        applied = _apply_buildgraph_splice(
            proposal, run_id=run_id, after_node_id=after_node_id, runs_dir=runs_dir)
    elif scope == "spec":
        applied = _apply_spec_change(proposal, ledger=ledger)
    elif scope == "requirement":
        applied = {
            "kind": "requirement_change",
            "requirement_changes": proposal.get("requirement_changes", []),
        }

    apply_failed = bool(
        isinstance(applied, dict)
        and (
            (scope in ("approach", "buildgraph", "org")
             and applied.get("spliced") is not True)
            or (scope == "spec" and applied.get("applied") is False)
        )
    )
    if apply_failed:
        reason = str(applied.get("error") or applied.get("note")
                     or "approved replan could not be applied")
        event = {
            "schema": SCHEMA,
            "id": request.get("id"),
            "scope": scope,
            "from_role": from_role,
            "verdict": "apply_failed",
            "status": "replan_apply_failed",
            "outcome_kind": "internal_error",
            "executed": False,
            "applied": applied,
        }
        outcome = ReplanOutcome(
            approved=False,
            scope=scope,
            applied=applied,
            ledger_event=event,
            returned_reason=reason,
            frozen_branch=frozen,
            outcome_kind="internal_error",
            status="replan_apply_failed",
            executed=False,
        )
        _log(ledger, from_role, event)
        return outcome

    outcome = ReplanOutcome(
        approved=True,
        scope=scope,
        applied=applied,
        frozen_branch=None,
        ledger_event={
            "schema": SCHEMA,
            "id": request.get("id"),
            "scope": scope,
            "from_role": from_role,
            "verdict": "approved",
            "outcome_kind": "ok",
            "executed": True,
            "applied": applied,
        },
    )
    _log(ledger, from_role, outcome.ledger_event)
    return outcome


def _apply_buildgraph_splice(
    proposal: dict[str, Any],
    *,
    run_id: Optional[str],
    after_node_id: Optional[str],
    runs_dir: Any,
) -> dict[str, Any]:
    """Translate a proposal's ``buildgraph_changes`` into a ``dag_replan`` splice.

    Only ``add`` ops become spliced nodes (remove/reorder/reassign are recorded as
    intent for the orchestrator — dag_replan is additive by design, see
    ``services/dag/replan.py``).  Requires a live run to splice into; without a
    ``run_id`` the change is recorded but not physically spliced (dry-run / test).
    """
    changes = proposal.get("buildgraph_changes") or []
    add_nodes = [c.get("node") for c in changes
                 if isinstance(c, dict) and c.get("op") == "add" and c.get("node")]
    other_ops = [c for c in changes
                 if isinstance(c, dict) and c.get("op") in ("remove", "reorder",
                                                            "reassign")]

    if not run_id or not after_node_id or not add_nodes:
        return {"kind": "buildgraph_change", "spliced": False,
                "add_nodes": add_nodes, "other_ops": other_ops,
                "note": "recorded; no live splice (missing run_id/after_node/adds)"}

    # Build the splice edges: each added node needs a bounded retry back-edge
    # (dag_replan validation enforces this).  The proposal may supply them; otherwise we
    # synthesise a self-retry with a conservative budget so the splice validates.
    splice_edges = list(proposal.get("splice_edges") or [])
    have_back = {e.get("from") for e in splice_edges if "retry_budget" in e}
    for node in add_nodes:
        nid = node.get("id")
        if nid and nid not in have_back:
            splice_edges.append({"from": nid, "to": nid, "retry_budget": 1})

    try:
        from services.dag.replan import dag_replan
        merged = dag_replan(
            run_id=run_id, splice_nodes=add_nodes, splice_edges=splice_edges,
            after_node_id=after_node_id, runs_dir=runs_dir)
        return {"kind": "buildgraph_change", "spliced": True,
                "add_nodes": [n.get("id") for n in add_nodes],
                "other_ops": other_ops, "merged_node_count": len(merged.get("nodes", []))}
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the ruling
        return {"kind": "buildgraph_change", "spliced": False,
                "add_nodes": [n.get("id") for n in add_nodes],
                "other_ops": other_ops, "error": f"{type(exc).__name__}: {exc}"}


def _apply_spec_change(proposal: dict[str, Any], *, ledger: Any) -> dict[str, Any]:
    """Compute the impact of a spec change via the derivation tree (mark_faulty).

    A spec change usually moves one or more dimensions; ``mark_faulty`` walks the
    §21 derivation tree and reports exactly what needs relink/recompute/rebuild/
    reverify so an early spec edit never silently poisons later features.
    """
    spec_changes = proposal.get("spec_changes") or []
    impacts: list[dict[str, Any]] = []
    if ledger is not None:
        try:
            from services.agent.derivation import mark_faulty
            for sc in spec_changes:
                if not isinstance(sc, dict):
                    continue
                ref = sc.get("dim_ref") or sc.get("ac_ref")
                if not ref:
                    continue
                impacts.append(mark_faulty(
                    ref, why=sc.get("why", "spec replan"), ledger=ledger))
        except Exception as exc:  # noqa: BLE001 — application failure is explicit
            detail = " ".join(str(exc).split())
            rendered = (
                f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            )
            return {
                "kind": "spec_change",
                "applied": False,
                "spec_changes": spec_changes,
                "impacts": impacts,
                "error": rendered[:240],
            }
    return {
        "kind": "spec_change",
        "applied": True,
        "spec_changes": spec_changes,
        "impacts": impacts,
    }


def _log(ledger: Any, actor: str, event: dict[str, Any]) -> None:
    if ledger is None:
        return
    try:
        ledger.append(actor, "replan", event)
    except Exception:  # noqa: BLE001
        pass
