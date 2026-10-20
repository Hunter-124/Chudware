"""In-app entry points for the overseer: ``audit_now`` and ``change_request``.

Task T-W13, in-app pivot (2026-07-04): the sole integration target is the in-app
copilot harness (services/agent + GUI).  These are plain callables the loop / dag
runner / pipeline invoke by name — no MCP dependency.  Registering them on the MCP
server is OPTIONAL (parked to T-W20); :func:`register_overseer_tools` is provided as
a free mirror of ``buildgraph.mcp_tools.register_buildgraph_tools`` but is NOT wired
into ``services/mcp/server.py`` here (MCP freshness is out of scope per the pivot).

Both functions load the run ledger and active build graph, then execute the
``overseer`` role through Chudcode.  A caller may request the deterministic local
fallback only with ``offline=True`` and an offline-safe provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from services.agent.ledger import Ledger, ledger_render
from .overseer import AuditScope, EvidenceBudget, Overseer, resolve_overseer_provider
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


# Retained as an injected test/offline seam. Online execution never resolves or
# invokes these provider objects.
ProviderMap = dict[str, Any]


def _offline_provider(providers: ProviderMap | None) -> Any | None:
    return resolve_overseer_provider(dict(providers or {}))


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
    session_id: str = "",
    on_event: Callable[..., None] | None = None,
    options: dict[str, Any] | None = None,
    offline: bool = False,
) -> dict:
    """Run an on-demand overseer audit over *run_id*'s current evidence.

    ``scope`` (optional) narrows the audit: ``{"stage","roles","buildgraph_nodes"}``.
    Returns ``{ok, skipped?, error?, audit, advisory, escalated, escalation_text}``.
    """
    ledger = Ledger.load(run_id, ledger_path)
    graph = _load_buildgraph(run_id, runs_dir)
    budget = budget or EvidenceBudget()
    sc = AuditScope(
        run_id=run_id,
        stage=(scope or {}).get("stage", ""),
        roles=list((scope or {}).get("roles", [])),
        buildgraph_nodes=list((scope or {}).get("buildgraph_nodes", [])),
    )
    ledger_text = ledger_render(ledger, budget.ledger)

    o = Overseer(
        provider=_offline_provider(providers) if offline else None,
        ledger=ledger,
        budget=budget,
        session_id=session_id,
        on_event=on_event,
        options=options,
        offline=offline,
    )
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
    session_id: str = "",
    on_event: Callable[..., None] | None = None,
    options: dict[str, Any] | None = None,
    offline: bool = False,
) -> dict:
    """Map a user change-request onto the run's ledger, plan it, audit it, recommend.

    Returns ``{ok, empty_scope, plan, audit, advisory, dispatch, needs_user_confirm,
    user_summary}``.  Nothing is mutated/dispatched — the overseer recommends; the
    orchestrator dispatches (freedom rule).
    """
    ledger = Ledger.load(run_id, ledger_path)
    graph = _load_buildgraph(run_id, runs_dir)
    overseer = Overseer(
        provider=_offline_provider(providers) if offline else None,
        ledger=ledger,
        session_id=session_id,
        on_event=on_event,
        options=options,
        offline=offline,
    )

    result = _cr.route_change_request(
        text,
        ledger.state,
        overseer=overseer,
        ledger=ledger,
        ledger_text=ledger_render(ledger, _CR_LEDGER_BUDGET),
        buildgraph=graph,
        run_id=run_id,
        stage=stage,
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
        "audit_status": "executed" if result.audit else "skipped",
        "audit_error": "",
        "audit_policy_refusal": False,
    }


# ---------------------------------------------------------------------------
# Optional MCP registration (free mirror — NOT wired into server.py; T-W20 parks
# external MCP surface polish).  Present so the tools are one call from exposure.
# ---------------------------------------------------------------------------


def register_overseer_tools(
    mcp, *, providers: ProviderMap | None = None, runs_dir: Optional[Path] = None
) -> None:
    """Register ``audit_now`` and ``change_request`` MCP tools onto *mcp*.

    Not called from server.py in T-W13 (in-app pivot).  A future MCP-freshness task
    (T-W20) can call this and add both tools to the server instructions.
    """
    import json as _json

    @mcp.tool()
    def audit_now_tool(
        run_id: str, stage: str = "", roles: str = "", buildgraph_nodes: str = ""
    ) -> str:
        scope = {
            "stage": stage,
            "roles": [r for r in roles.split(",") if r],
            "buildgraph_nodes": [n for n in buildgraph_nodes.split(",") if n],
        }
        return _json.dumps(
            audit_now(run_id, scope, providers=providers, runs_dir=runs_dir), indent=2
        )

    @mcp.tool()
    def change_request_tool(text: str, run_id: str, stage: str = "fix") -> str:
        return _json.dumps(
            change_request(
                text, run_id, stage=stage, providers=providers, runs_dir=runs_dir
            ),
            indent=2,
        )
