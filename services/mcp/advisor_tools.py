"""Advisory MCP tools — ``advise``, ``stage_set``, ``advisory_dispute``.

Family: ``advisor``.  Risk classes auto-detected from tool names:
  - ``advise``           → ``read``  (on-demand battery; no mutation)
  - ``stage_set``        → ``write`` (records a stage override to the ledger)
  - ``advisory_dispute`` → ``write`` (logs a dispute to the ledger)

Schema: contracts/advisor.md §1 (stages), §3 (advisory block), §9 (precedence
+ dispute rules).  Per §9.3, disputing a ``kernel``-sourced advisory requires an
``ask_user`` waiver — this tool records the dispute and flags when a waiver is
required; it does not itself grant the waiver.

The in-app hosts (loop.py, dag node_runner) run the foreman automatically; these
tools give external MCP clients (e.g. Claude Code) the same battery on demand.
"""
from __future__ import annotations

from typing import Any


def register_advisor_tools(mcp) -> None:
    """Register ``advise``, ``stage_set``, ``advisory_dispute`` under ``advisor``."""

    @mcp.tool()
    def advise(doc_id: str, stage: str = "assembly", run_id: str = "") -> dict:
        """Run the stage's read-only check battery over *doc_id* and return advisories.

        The foreman's on-demand entry point (aicad.advisory/1, contract §3).
        ``stage`` selects the battery (intake|spec|layout|sketch|feature|
        part_verify|assembly|sim|cam|deliver|fix); default ``assembly`` runs the
        interference check.  Returns the rendered [ADVISORY] block plus the raw
        items so the caller can act on candidate fixes programmatically.  When a
        ``run_id`` is given the advisory is also logged to that ledger (§3).
        """
        try:
            from services.agent.advisor import adapters, engine, render
            from services.agent.advisor import stages as _stages
        except ImportError as exc:
            return {
                "ok": False,
                "executed": False,
                "policy_refusal": False,
                "outcome_kind": "internal_error",
                "status": "advisor_unavailable",
                "error": f"advisor package unavailable: {exc}",
            }

        if not _stages.is_stage(stage):
            return {
                "ok": False,
                "executed": False,
                "policy_refusal": False,
                "outcome_kind": "validation_error",
                "status": "invalid_stage",
                "error": f"unknown stage {stage!r}",
                "stages": list(_stages.STAGES) + [_stages.FIX_STAGE],
            }

        try:
            obs = adapters.observe_doc(doc_id, stage)
            advisory = engine.build_advisory(obs)
            block = render.render_advisory(advisory)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "executed": False,
                "policy_refusal": False,
                "outcome_kind": "internal_error",
                "status": "internal_error",
                "error": f"advise failed: {exc}",
            }

        if run_id:
            try:
                from services.agent.ledger import Ledger
                Ledger.load(run_id).append("foreman", "advisory", advisory)
            except Exception:  # noqa: BLE001
                pass

        errors = [
            finding.finding
            for finding in obs.findings
            if finding.source_kind == "harness"
            and finding.extra.get("outcome") == "error"
        ]
        skipped = [
            finding.finding
            for finding in obs.findings
            if finding.source_kind == "harness"
            and finding.extra.get("outcome") == "skipped"
        ]
        no_checks = not _stages.check_battery(stage)
        executed = not errors and not skipped and not no_checks
        if errors:
            outcome_kind = "internal_error"
            status = "check_error"
        elif skipped or no_checks:
            outcome_kind = "guard_block"
            status = "checks_unavailable" if skipped else "no_checks"
        else:
            outcome_kind = "ok"
            status = "executed"

        return {
            "ok": executed,
            "executed": executed,
            "policy_refusal": False,
            "outcome_kind": outcome_kind,
            "status": status,
            "skipped": skipped,
            "error": "; ".join(errors),
            "doc_id": doc_id,
            "stage": stage,
            "block": block,
            "advisory": advisory,
            "severity": engine.max_severity(advisory),
            "has_blocker": engine.has_blocker(advisory),
        }

    @mcp.tool()
    def stage_set(run_id: str, stage: str, reason: str, actor: str = "foreman") -> dict:
        """Override the inferred process stage (contract §1: ``stage_set``).

        Records a ``stage`` event on the ledger for *run_id* so replay and the
        transparency UI show the override + its reason.
        """
        try:
            from services.agent.advisor import stages as _stages
        except ImportError as exc:
            return {"ok": False, "error": f"advisor package unavailable: {exc}"}
        if not _stages.is_stage(stage):
            return {"ok": False, "error": f"unknown stage {stage!r}",
                    "stages": list(_stages.STAGES) + [_stages.FIX_STAGE]}
        try:
            from services.agent.ledger import Ledger
            led = Ledger.load(run_id)
            ev = led.append(actor, "stage",
                            {"stage": stage, "reason": reason, "override": True})
            return {"ok": True, "run_id": run_id, "stage": stage, "seq": ev["seq"]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"stage_set failed: {exc}"}

    @mcp.tool()
    def advisory_dispute(run_id: str, advisory_id: str, reason: str,
                         source_kind: str = "kernel", actor: str = "foreman") -> dict:
        """Contest an advisory (contract §9.3).  Logs the dispute to the ledger.

        Disputing a ``kernel``-sourced advisory requires an ``ask_user`` waiver:
        this tool records the dispute and returns ``waiver_required: true`` so the
        harness prompts the human before the advisory is treated as waived.
        """
        waiver_required = source_kind == "kernel"
        try:
            from services.agent.ledger import Ledger
            led = Ledger.load(run_id)
            ev = led.append(actor, "advisory_dispute", {
                "advisory_id": advisory_id,
                "reason": reason,
                "source_kind": source_kind,
                "waiver_required": waiver_required,
                "status": "pending" if waiver_required else "accepted",
            })
            return {"ok": True, "run_id": run_id, "advisory_id": advisory_id,
                    "waiver_required": waiver_required, "seq": ev["seq"]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"advisory_dispute failed: {exc}"}
