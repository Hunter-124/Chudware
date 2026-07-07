"""mission_tools.py — MCP surface for the copilot mission pipeline (T-W19).

Three thin tools over the deterministic pipeline glue:

  * ``mission_start(request)``   — validate the request into a manifest (via an
    injected or role-driven intake) and compile the mission DAG; returns the
    run_id + manifest + planned graph (or the block reason).
  * ``mission_status(run_id)``   — render the mission's phase state from the ledger
    (what mission control + the GUI consume — a pure ledger read).
  * ``replan_request(payload)``  — route an ``aicad.replan/1`` request per §17 and
    report who must approve.

These mirror the ``register_<family>_tools(mcp)`` + nested ``@mcp.tool()`` pattern
used by ``services/dag/mcp_tools.py`` and ``services/mcp/ledger_tools.py``.  The
in-app path (``services.agent.loop.run_mission``) is the primary integration
target; these tools expose the same capability to external MCP clients.
"""
from __future__ import annotations

from typing import Any, Optional


def register_mission_tools(mcp) -> None:
    """Register mission_start / mission_status / replan_request on *mcp*."""

    @mcp.tool()
    def mission_start(
        request: str,
        manifest: Optional[dict] = None,
        run_id: Optional[str] = None,
        autonomy_default: str = "approve",
        tier: Optional[str] = None,
    ) -> dict:
        """Plan a mission: validate the deliverable manifest and compile the
        role-assigned mission DAG (does NOT execute — call dag_run to run it).

        INTAKE (turning the request into a manifest) is a role call; pass the
        ``manifest`` the intake_analyst produced (or run the in-app
        ``run_mission(request, provider, intake_fn=...)`` which does INTAKE for you).
        Without a manifest this returns the intake instruction rather than guessing.

        A blocking ambiguity or an invalid manifest returns ``ok=False`` with the
        validator findings and the block reason — the mission never fabricates a graph.

        ``tier`` (optional): a model tier (``local-24k`` / ``local-48k`` / ``mid`` /
        ``frontier``) to pin EVERY node of the compiled graph to. This MCP path has no
        in-hand Provider object (unlike the in-app ``run_mission(request, provider,
        ...)``, whose own provider is now threaded into every internal DAG node), so a
        tier recorded here is how ``dag_run`` keeps this mission on the requested
        tier's provider (resolved via ``services.agent.tiers.provider_for_tier`` —
        e.g. a ``local-*`` tier stays on the bundled local model instead of silently
        leaking to the ambient config.json / ``AI_CAD_PROVIDER`` cloud default).
        Omitting it (the default) produces exactly the graph this tool has always
        returned — no behaviour change.
        """
        from services.agent.pipeline import (
            validate_manifest, manifest_valid, blocking_ambiguities)
        from services.dag.mission_template import build_mission_graph

        if manifest is None:
            return {
                "ok": False,
                "reason": "INTAKE not run: pass the intake_analyst's manifest, or "
                          "use the in-app run_mission(request, provider, intake_fn=...) "
                          "which performs INTAKE and drives all 8 phases",
                "request": request,
            }

        findings = validate_manifest(manifest)
        finding_strs = [str(f) for f in findings]
        if not manifest_valid(findings):
            return {"ok": False, "reason": "manifest invalid (§16)",
                    "findings": finding_strs}
        blocking = blocking_ambiguities(findings)
        if blocking:
            return {"ok": False, "reason": "blocking ambiguity — route to CLARIFY",
                    "findings": [str(f) for f in blocking]}

        gid = run_id or "mission"
        graph = build_mission_graph(manifest, graph_id=gid,
                                    default_autonomy=autonomy_default,
                                    tier=tier)
        return {
            "ok": True,
            "run_id": gid,
            "graph": graph,
            "deliverables": [d.get("id") for d in manifest.get("deliverables", [])],
            "findings": finding_strs,
            "hint": "execute with dag_run(graph_json=<graph>), then track via "
                    "mission_status(run_id)",
        }

    @mcp.tool()
    def mission_status(run_id: str, budget_tokens: int = 1200) -> dict:
        """Render a mission's phase state from the ledger (pure read).

        Returns the stage_log (phase entries/exits with their exit gates), the
        deliverable count, open questions, and a budgeted ledger render — the exact
        surface mission control + the GUI display.
        """
        try:
            from services.agent.ledger import Ledger, ledger_render
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"ledger unavailable: {exc}"}
        try:
            led = Ledger.load(run_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"no ledger for run {run_id!r}: {exc}"}
        state = led.state
        stage_log = state.get("stage_log", [])
        current = None
        for s in reversed(stage_log):
            if not s.get("exited"):
                current = s.get("stage")
                break
        return {
            "ok": True,
            "run_id": run_id,
            "current_phase": current,
            "stage_log": stage_log,
            "phases_complete": [s["stage"] for s in stage_log if s.get("exited")],
            "open_questions": [q for q in state.get("questions", [])
                               if q.get("status") == "open"],
            "artifacts": state.get("artifacts", []),
            "render": ledger_render(led, budget_tokens),
        }

    @mcp.tool()
    def replan_request(payload: dict, run_id: Optional[str] = None) -> dict:
        """Route an ``aicad.replan/1`` request per §17 and report the approvers.

        This performs the DETERMINISTIC routing only (who must approve, whether an
        overseer audit fires, whether the user is required).  It does NOT auto-apply
        the change — approval is an orchestrator/overseer/user judgement; call the
        in-app ``apply_replan`` (or dag_replan for the splice) once ruled on.
        """
        from services.agent.pipeline import route_replan, validate_replan_request
        errs = validate_replan_request(payload)
        if errs:
            return {"ok": False, "reason": "invalid replan request", "errors": errs}
        decision = route_replan(payload)
        return {
            "ok": True,
            "scope": decision.scope,
            "approvers": list(decision.approvers),
            "overseer_audit": decision.overseer_audit,
            "user_required": decision.user_required,
            "routing_reason": decision.reason,
        }
