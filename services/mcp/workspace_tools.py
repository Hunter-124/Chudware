"""workspace_tools.py — MCP tools for the multi-doc workspace (T-W16).

Family: ``workspace``.
Risk: ``workspace_status`` → read (added to _READ_EXACT in tool_registry);
      all other workspace tools → write (default, lease-gated upstream).

Contract: contracts/advisor.md §12 (leases), §13 (buildgraph), §2.14.
"""
from __future__ import annotations

from typing import Any


def register_workspace_tools(mcp) -> None:
    """Register workspace tools under the ``workspace`` family."""

    @mcp.tool()
    def workspace_status(run_id: str) -> dict:
        """Return the full workspace snapshot for *run_id*.

        One call gives the GUI everything it needs:
        - Open docs with owners, status, and latest render artifact path.
        - Buildgraph node statuses (if a session is active for this run).
        - Timestamp.

        This is the polling target until a push channel exists (T-W17).
        Risk: read (named in _READ_EXACT in tool_registry — no lease required).
        """
        try:
            from services.agent import workspace as ws
        except ImportError:
            from agent import workspace as ws  # type: ignore

        # Look up the active session for this run_id
        session = ws._session_registry.get(run_id)
        if session is None:
            return {
                "ok": False,
                "error": f"no active workspace session for run_id={run_id!r}",
                "hint": "Session is created when WorkspaceSession is registered via register_session().",
            }
        return {"ok": True, **session.workspace_status()}

    @mcp.tool()
    def workspace_doc_events(
        run_id: str,
        doc_id: str,
        since_seq: int = 0,
    ) -> dict:
        """Return per-doc event stream entries starting at *since_seq*.

        Events are 0-indexed lines in the JSONL stream.  Pass the count of
        previously-received events as *since_seq* to get only new events.
        Suitable for incremental polling by the GUI.

        Risk: read.
        """
        try:
            from services.agent.workspace import read_doc_events
        except ImportError:
            from agent.workspace import read_doc_events  # type: ignore

        events = read_doc_events(run_id, doc_id, since_seq=since_seq)
        return {
            "ok": True,
            "run_id": run_id,
            "doc_id": doc_id,
            "since_seq": since_seq,
            "events": events,
            "count": len(events),
        }
