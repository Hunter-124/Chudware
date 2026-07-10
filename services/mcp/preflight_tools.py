"""MCP tools for preflight dry-run validation (aicad.preflight/1, T-W12).

Registered onto the FastMCP instance by ``register_preflight_tools(mcp)``.

External tool:
    ``preflight_ops(doc_id, tool, ops)`` — read-risk class; never mutates.
    Returns an aicad.preflight/1 JSON string.

Internal seam:
    ``run_auto_preflight(doc_id, tool, ops)`` — called by add_features / mutating
    tools when ``AI_CAD_PREFLIGHT=1`` (default on).  Returns the preflight result
    dict; caller attaches ``advisory_items`` to its own response and, if the
    verdict is ``fail``, calls ``log_preflight_override`` after applying.

Freedom rule: a failed preflight NEVER blocks the caller.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP


# ------------------------------------------------------------------ public seam

def run_auto_preflight(doc_id: str, tool: str, ops: list) -> dict:
    """Run preflight automatically inside mutating tools.

    Returns an aicad.preflight/1 dict, or ``{"verdict": "skipped", "skipped": "disabled"}``
    when AI_CAD_PREFLIGHT=0.  Never raises.
    """
    enabled = os.environ.get("AI_CAD_PREFLIGHT", "1").strip().lower()
    if enabled in ("0", "false", "off", "no"):
        return {
            "schema": "aicad.preflight/1",
            "id": "pf_disabled",
            "target": {"doc_id": doc_id, "tool": tool, "ops_hash": ""},
            "verdict": "skipped",
            "skipped": "disabled",
            "skipped_detail": "AI_CAD_PREFLIGHT=0",
            "checks": [],
            "predicted": {},
            "downstream": [],
            "advisory_items": [],
        }
    try:
        from services.agent.advisor.preflight import run_preflight
        return run_preflight(doc_id, tool, ops)
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": "aicad.preflight/1",
            "id": "pf_error",
            "target": {"doc_id": doc_id, "tool": tool, "ops_hash": ""},
            "verdict": "skipped",
            "skipped": "error",
            "skipped_detail": str(exc),
            "checks": [],
            "predicted": {},
            "downstream": [],
            "advisory_items": [],
        }


def log_preflight_override(doc_id: str, tool: str, pf_result: dict) -> None:
    """Log a preflight_overridden event when a failed preflight is applied.

    Call this AFTER a mutating tool applies ops when ``pf_result["verdict"] == "fail"``.
    """
    try:
        from services.agent.advisor.preflight import log_override
        log_override(
            doc_id=doc_id,
            tool=tool,
            pf_id=pf_result.get("id", "pf_unknown"),
            verdict=pf_result.get("verdict", "unknown"),
        )
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ registration

def register_preflight_tools(mcp: FastMCP) -> None:
    """Register the ``preflight_ops`` MCP tool onto *mcp*."""

    @mcp.tool()
    def preflight_ops(
        ops: list,
        doc_id: Optional[str] = None,
        tool: str = "add_features",
    ) -> str:
        """Dry-run validate planned ops on a shadow clone of the document.

        Checks planned ops BEFORE they touch the document:
        - ``refs_resolve``: stable ids / names in ops exist in the current tree.
        - ``params_sane``: positive dimensions; fillet radius vs adjacent wall;
          hole depth vs body extent.
        - ``rebuild_predict``: predicted feature_status after apply (no rollback
          needed — uses a throwaway clone).
        - ``downstream_impact``: features referencing entities you are editing.

        Returns an ``aicad.preflight/1`` JSON object.  ``verdict`` is one of:
        ``pass``, ``warn``, ``fail``, or ``skipped`` (budget exceeded or disabled).
        A ``fail`` verdict is advisory — it does NOT block; the caller decides
        whether to proceed (freedom rule, §2.10).

        **Read-only**: this tool NEVER mutates the live document.  It is safe to
        call speculatively before any destructive edit.

        Typical use:
            1. Before ``add_features``: call ``preflight_ops(ops)`` to catch
               dangling refs, impossible fillets, or downstream breaks.
            2. Read ``advisory_items`` — severity ``action`` items need attention,
               ``hint`` items are warnings.
            3. Call ``add_features(ops)`` as normal.  If preflight said fail and
               you proceed anyway, a ``preflight_overridden`` audit event is logged.
        """
        # Relative imports: resolve to the SAME module identity this package was
        # loaded under (mcp_server or services.mcp). An absolute services.mcp
        # import here creates a second cad_tools/core_bridge module object with
        # its own _CUR/_PERIODIC_TIMER/fallback state — the 12x-autosave bug class.
        from . import core_bridge as cb
        from .cad_tools import _CUR, _DOC_ALIASES

        def _resolve(did: Optional[str]) -> str:
            if did:
                return _DOC_ALIASES.get(did, did)
            cur = _CUR.get("doc_id")
            if not cur:
                raise ValueError("No current document — pass doc_id or call new_part() first")
            return cur

        try:
            d = _resolve(doc_id)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        try:
            from services.agent.advisor.preflight import run_preflight
            result = run_preflight(d, tool, ops)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)})
