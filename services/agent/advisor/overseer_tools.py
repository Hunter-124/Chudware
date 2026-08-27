"""In-app entry points for the overseer: ``audit_now`` and ``change_request``.

Task T-W13, in-app pivot (2026-07-04): the sole integration target is the in-app
copilot harness (services/agent + GUI).  These are plain callables the loop / dag
runner / pipeline invoke by name — no MCP dependency.  Registering them on the MCP
server is OPTIONAL (parked to T-W20); :func:`register_overseer_tools` is provided as
a free mirror of ``buildgraph.mcp_tools.register_buildgraph_tools`` but is NOT wired
into ``services/mcp/server.py`` here (MCP freshness is out of scope per the pivot).

Both functions:
  * load the run's ledger (state = the change-request scope surface) and its active
    build-graph sidecar (the plan slice);
  * build an :class:`~services.agent.advisor.overseer.Overseer` with a provider
    resolved via the routing seam (§14; ``strongest`` until T-W14);
  * return JSON-serializable dicts.

Offline: when no provider is configured the audit degrades to ``skipped`` (for
``audit_now``) or an un-audited deterministic change plan (for ``change_request``) —
the deterministic mapping is useful on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from services.agent.ledger import Ledger, ledger_render
from .overseer import (
    AuditScope, EvidenceBudget, Overseer, resolve_overseer_provider,
)
from . import change_request as _cr

_CR_LEDGER_BUDGET = 2000  # tokens rendered into the change-request self-audit


# ---------------------------------------------------------------------------
# Run-state loading (ledger + buildgraph sidecar)
# ---------------------------------------------------------------------------

def _load_buildgraph(run_id: str, runs_dir: Optional[Path]) -> dict | None:
    """Load the active build graph for *run_id* from its sidecar (T-W15).

    Reuses the buildgraph sidecar reader so we stay consistent with how the graph
    was persisted.  Returns the ``aicad.buildgraph/1`` dict or ``None``.
    """
    try:
        from services.agent.buildgraph.mcp_tools import _read_sidecar, _runs_dir
    except Exception:  # noqa: BLE001
        return None
    rd = runs_dir or _runs_dir()
    data = _read_sidecar(run_id, rd)
    if not data:
        return None
    return data.get("graph")


# ProviderResolver: called with nothing, returns {name: provider} available now.
# Injected so this module never reaches into GUI settings directly (testable).
ProviderMap = dict[str, Any]
@dataclass(frozen=True)
class _ProviderResolution:
    providers: ProviderMap
    status: str
    error: str = ""
    policy_refusal: bool = False


def _is_credential_fault(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "credential unavailable",
        "credential missing",
        "requires an api key",
        "missing api key",
        "missing access_token",
        "authentication credential",
    ))


def _default_providers() -> _ProviderResolution:
    """Resolve and type provider construction instead of collapsing every fault."""
    try:
        from services.agent import config as agent_config
    except Exception as exc:  # noqa: BLE001
        return _ProviderResolution({}, "provider_error", str(exc))

    try:
        provider = agent_config.get_provider(role="overseer")
    except agent_config.OfflineModeError as exc:
        return _ProviderResolution(
            {}, "offline_policy", str(exc), policy_refusal=True,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_credential_fault(exc):
            return _ProviderResolution({}, "credential_unavailable", str(exc))
        return _ProviderResolution({}, "provider_error", str(exc))

    if provider is None:
        return _ProviderResolution({}, "unprovisioned")

    from services.agent.role_models import clamp_provider_effort
    provider = clamp_provider_effort(provider)
    name = getattr(provider, "name", "") or "resolved"
    return _ProviderResolution({name: provider}, "ready")


def _resolve_provider_input(providers: ProviderMap | None) -> _ProviderResolution:
    if providers is None:
        return _default_providers()
    if not providers or not any(provider is not None for provider in providers.values()):
        return _ProviderResolution({}, "unprovisioned")
    return _ProviderResolution(dict(providers), "ready")


def _nonexecuted_audit(scope: AuditScope, resolution: _ProviderResolution) -> dict:
    skipped = (
        "no provider configured for overseer (routing seam)"
        if resolution.status == "unprovisioned"
        else ""
    )
    outcome_kind = (
        "policy_refusal" if resolution.policy_refusal
        else "guard_block" if resolution.status == "unprovisioned"
        else "internal_error"
    )
    return {
        "ok": False,
        "executed": False,
        "policy_refusal": resolution.policy_refusal,
        "outcome_kind": outcome_kind,
        "status": resolution.status,
        "reason": resolution.error,
        "skipped": skipped,
        "error": "" if resolution.policy_refusal else resolution.error,
        "audit": {},
        "advisory": {
            "schema": "aicad.advisory/1",
            "seq": 0,
            "stage": scope.stage,
            "items": [],
            "user_override": False,
        },
        "escalated": False,
        "escalation_text": "",
    }


# ---------------------------------------------------------------------------
# audit_now(run_id, scope?)
# ---------------------------------------------------------------------------

def audit_now(
    run_id: str,
    scope: dict | None = None,
    *,
    trigger: str = "manual",
    providers: ProviderMap | None = None,
    runs_dir: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    budget: EvidenceBudget | None = None,
) -> dict:
    """Run an on-demand overseer audit over *run_id*'s current evidence.

    ``scope`` (optional) narrows the audit: ``{"stage","roles","buildgraph_nodes"}``.
    Returns ``{ok, skipped?, error?, audit, advisory, escalated, escalation_text}``.
    """
    ledger = Ledger.load(run_id, ledger_path)
    graph = _load_buildgraph(run_id, runs_dir)
    resolution = _resolve_provider_input(providers)
    provider = resolve_overseer_provider(resolution.providers)
    budget = budget or EvidenceBudget()
    sc = AuditScope(
        run_id=run_id,
        stage=(scope or {}).get("stage", ""),
        roles=list((scope or {}).get("roles", [])),
        buildgraph_nodes=list((scope or {}).get("buildgraph_nodes", [])),
    )
    if resolution.status != "ready" or provider is None:
        if provider is None and resolution.status == "ready":
            resolution = _ProviderResolution({}, "unprovisioned")
        return _nonexecuted_audit(sc, resolution)
    ledger_text = ledger_render(ledger, budget.ledger)

    o = Overseer(provider=provider, ledger=ledger, budget=budget)
    res = o.audit(trigger, sc, ledger_text=ledger_text, buildgraph=graph)
    executed = bool(res.audit) and not res.error and not res.skipped
    return {
        "ok": executed,
        "executed": executed,
        "policy_refusal": False,
        "outcome_kind": (
            "ok" if executed else "internal_error" if res.error else "guard_block"
        ),
        "status": (
            "executed" if executed else "provider_error" if res.error else "skipped"
        ),
        "skipped": res.skipped,
        "error": res.error,
        "audit": res.audit,
        "advisory": res.advisory,
        "escalated": res.escalated,
        "escalation_text": res.escalation_text,
    }


# ---------------------------------------------------------------------------
# change_request(text)
# ---------------------------------------------------------------------------

def change_request(
    text: str,
    run_id: str,
    *,
    stage: str = "fix",
    providers: ProviderMap | None = None,
    runs_dir: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
) -> dict:
    """Map a user change-request onto the run's ledger, plan it, audit it, recommend.

    Returns ``{ok, empty_scope, plan, audit, advisory, dispatch, needs_user_confirm,
    user_summary}``.  Nothing is mutated/dispatched — the overseer recommends; the
    orchestrator dispatches (freedom rule).
    """
    ledger = Ledger.load(run_id, ledger_path)
    graph = _load_buildgraph(run_id, runs_dir)
    resolution = _resolve_provider_input(providers)
    provider = resolve_overseer_provider(resolution.providers)
    overseer = Overseer(provider=provider, ledger=ledger) if provider is not None else None

    result = _cr.route_change_request(
        text, ledger.state,
        overseer=overseer, ledger=ledger,
        ledger_text=ledger_render(ledger, _CR_LEDGER_BUDGET),
        buildgraph=graph, run_id=run_id, stage=stage,
    )
    return {
        "ok": True,
        "empty_scope": result.empty_scope,
        "plan": result.plan,
        "audit": result.audit,
        "advisory": result.advisory,
        "dispatch": result.dispatch,
        "needs_user_confirm": result.needs_user_confirm,
        "user_summary": result.user_summary,
        "audit_status": (
            "executed" if result.audit else resolution.status
        ),
        "audit_error": resolution.error,
        "audit_policy_refusal": resolution.policy_refusal,
    }


# ---------------------------------------------------------------------------
# Optional MCP registration (free mirror — NOT wired into server.py; T-W20 parks
# external MCP surface polish).  Present so the tools are one call from exposure.
# ---------------------------------------------------------------------------

def register_overseer_tools(mcp, *, providers: ProviderMap | None = None,
                            runs_dir: Optional[Path] = None) -> None:
    """Register ``audit_now`` and ``change_request`` MCP tools onto *mcp*.

    Not called from server.py in T-W13 (in-app pivot).  A future MCP-freshness task
    (T-W20) can call this and add both tools to the server instructions.
    """
    import json as _json

    @mcp.tool()
    def audit_now_tool(run_id: str, stage: str = "", roles: str = "",
                       buildgraph_nodes: str = "") -> str:
        scope = {
            "stage": stage,
            "roles": [r for r in roles.split(",") if r],
            "buildgraph_nodes": [n for n in buildgraph_nodes.split(",") if n],
        }
        return _json.dumps(audit_now(run_id, scope, providers=providers,
                                     runs_dir=runs_dir), indent=2)

    @mcp.tool()
    def change_request_tool(text: str, run_id: str, stage: str = "fix") -> str:
        return _json.dumps(change_request(text, run_id, stage=stage,
                                          providers=providers, runs_dir=runs_dir),
                           indent=2)
