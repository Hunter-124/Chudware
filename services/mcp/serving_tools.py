"""Adapter-serving MCP tools (T-H9) — routing status, canary, rollback, gated promotion.

Surfaces the ``services.serving`` routing table over MCP so an operator (or the in-app
Settings surface) can see *which brain answers at each insertion point* and drive the
rollout ladder:

  serving_status()                         -> per-IP {provider, model, adapter_version, canary%, fallback}
  serving_set_canary(ip, pct[, opt_in])    -> dial canary traffic (ladder middle rung)
  serving_rollback(ip[, to_version])       -> routing flip to a previous adapter (always safe)
  serving_evaluate(ip, shadow_log[, ...])  -> promotion EVIDENCE only (writes report; promotes nothing)
  serving_promote(ip, adapter_version, report_ref, approver[, ...])  -> GATED promote

Promotion is Erik-ratified (2026-07-02): the challenger must beat-or-match the incumbent on
BOTH verifier-score AND accept-rate over N shadow jobs AND carry a human sign-off. This tool
requires ``report_ref`` + ``approver`` as mandatory arguments — there is no auto-promote path;
supplying them IS the human trigger.

Pure Python (routing/promotion + shadow_report are stdlib-only) — no engine/core dependency,
so these tools work in any MCP runtime venv.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from services.mcp.cad_tools import _dumps, _err


def register_serving_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def serving_status() -> str:
        """Report the active adapter-routing table — WHICH BRAIN answers at each insertion point.

        Returns JSON ``{ok, routes:[{insertion_point, provider, model, adapter_version,
        canary_pct, opt_in_count, fallback:{mode,provider,model}, history}]}``. This is the
        read-only "which brain answered" data the Settings surface renders. No adapter_version
        (null) means the base model / fallback answers there.
        """
        try:
            from services.serving.routing import load_routing_table
            table = load_routing_table()
            routes = []
            for ip, e in sorted(table.entries().items()):
                routes.append({
                    "insertion_point": ip,
                    "provider": e.provider,
                    "model": e.model,
                    "adapter_version": e.adapter_version,
                    "canary_pct": e.canary_pct,
                    "opt_in_count": len(e.canary_opt_in),
                    "fallback": e.fallback.to_dict(),
                    "history": list(e.history),
                })
            return _dumps({"ok": True, "routes": routes})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def serving_set_canary(insertion_point: str, pct: float,
                           opt_in: list[str] | None = None) -> str:
        """Dial the canary traffic percentage (0-100) for an insertion point; persists live.

        Optional ``opt_in`` replaces the early-adopter list. This is the ladder's middle rung —
        NOT a promotion (promotion to 100% production is ``serving_promote`` and is gated).
        Returns the updated route.
        """
        try:
            from services.serving.promotion import set_canary
            from services.serving.routing import load_routing_table, save_routing_table
            table = load_routing_table()
            entry = set_canary(table, insertion_point, float(pct), opt_in=opt_in)
            save_routing_table(table)
            return _dumps({"ok": True, "insertion_point": insertion_point,
                           "canary_pct": entry.canary_pct,
                           "adapter_version": entry.adapter_version})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def serving_rollback(insertion_point: str, to_version: str | None = None) -> str:
        """Roll back an insertion point to a previous DVC-versioned adapter — always safe.

        ``to_version`` given -> flip straight to it; omitted -> restore the most recent prior
        adapter from history (or drop to the base/fallback if none). Nothing is overwritten;
        this is a routing flip only (documented <1 min rollback). Returns the updated route.
        """
        try:
            from services.serving.promotion import rollback
            from services.serving.routing import load_routing_table, save_routing_table
            table = load_routing_table()
            entry = rollback(table, insertion_point, to_version=to_version)
            save_routing_table(table)
            return _dumps({"ok": True, "insertion_point": insertion_point,
                           "adapter_version": entry.adapter_version,
                           "history": list(entry.history)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def serving_evaluate(insertion_point: str, shadow_log: str,
                         min_calls: int = 100, report_out: str | None = None) -> str:
        """Compute promotion EVIDENCE for an insertion point from a T-H7 shadow log.

        Reads the JSONL ``shadow_log``, aggregates incumbent vs challenger
        verifier-score + accept-rate, and returns the verdict. If ``report_out`` is
        given, also writes the human-readable evidence report there (the artifact Erik signs
        off). THIS PROMOTES NOTHING — ``eligible=true`` is necessary, not sufficient.
        """
        try:
            from pathlib import Path

            from services.serving.promotion import evaluate_promotion, write_evidence_report
            from tools.flywheel.shadow_report import load_shadow_log
            rows = load_shadow_log(Path(shadow_log))
            if report_out:
                ev = write_evidence_report(rows, insertion_point, Path(report_out),
                                           min_calls=min_calls)
            else:
                ev = evaluate_promotion(rows, insertion_point, min_calls=min_calls)
            out = ev.to_dict()
            out["report_written"] = report_out or None
            return _dumps({"ok": True, "evidence": out})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def serving_promote(insertion_point: str, adapter_version: str,
                        report_ref: str, approver: str,
                        shadow_log: str, min_calls: int = 100) -> str:
        """GATED promote of an adapter to full production (100%). Human trigger + evidence.

        REQUIRES (all mandatory — no auto-promote path, Erik-ratified 2026-07-02):
          - ``approver``    : the human signing off (e.g. "erik");
          - ``report_ref``  : path to the signed-off evidence report (must exist on disk);
          - ``shadow_log``  : the T-H7 shadow log the eligibility is recomputed from;
        and the recomputed evidence must be ``eligible`` (challenger beats-or-matches incumbent
        on BOTH verifier-score AND accept-rate over >= ``min_calls`` jobs). On success the
        routing table flips to the new adapter and the outgoing version is retained for rollback.
        Refusals return ``{ok:False, code:"promotion_refused", detail}`` — never a silent no-op.
        """
        try:
            from pathlib import Path

            from services.serving.promotion import (SignOff, evaluate_promotion,
                                                    promote, PromotionRefused)
            from services.serving.routing import load_routing_table, save_routing_table
            from tools.flywheel.shadow_report import load_shadow_log

            rows = load_shadow_log(Path(shadow_log))
            evidence = evaluate_promotion(rows, insertion_point, min_calls=min_calls)
            signoff = SignOff(approver=approver, report_ref=report_ref)
            table = load_routing_table()
            try:
                entry = promote(table, insertion_point, adapter_version,
                                evidence=evidence, signoff=signoff)
            except PromotionRefused as pr:
                return _dumps({"ok": False, "code": "promotion_refused", "detail": str(pr),
                               "evidence": evidence.to_dict()})
            save_routing_table(table)
            return _dumps({"ok": True, "insertion_point": insertion_point,
                           "adapter_version": entry.adapter_version,
                           "canary_pct": entry.canary_pct,
                           "history": list(entry.history),
                           "approver": approver, "report_ref": report_ref})
        except Exception as e:  # noqa: BLE001
            return _err(e)
