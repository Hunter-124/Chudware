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
    def workspace_status(run_id: str = "") -> dict:
        """Return the full workspace snapshot for *run_id*.

        One call gives the GUI everything it needs:
        - Open docs with owners, status, and latest render artifact path.
        - Buildgraph node statuses (if a session is active for this run).
        - Timestamp.

        This is the polling target until a push channel exists (T-W17).
        Risk: read (named in _READ_EXACT in tool_registry — no lease required).

        run_id is optional: a general/orientation turn that doesn't name a run
        gets the ambient session when exactly one is live, otherwise an
        empty-but-OK snapshot. A fresh single-doc turn has no registered
        multi-doc session — that's not an error, so it never paints a failure
        card. A NAMED-but-unknown run_id stays an error (orchestrator poll
        contract).
        """
        from services.agent import workspace as ws

        reg = ws._session_registry
        rid = (run_id or "").strip()
        if not rid:
            # No run named: resolve the ambient session, else report empty-OK.
            if len(reg) == 1:
                session = next(iter(reg.values()))
            else:
                snap = {
                    "ok": True,
                    "schema": "aicad.workspace/1",
                    "run_id": "",
                    "ts": ws._now(),
                    "docs": [],
                }
                snap["note"] = ("no active multi-doc workspace session" if not reg
                                else f"{len(reg)} active sessions; pass run_id to select one")
                if reg:
                    snap["run_ids"] = sorted(reg.keys())
                return snap
        else:
            session = reg.get(rid)
            if session is None:
                return {
                    "ok": False,
                    "error": f"no active workspace session for run_id={rid!r}",
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
        from services.agent.workspace import read_doc_events

        events = read_doc_events(run_id, doc_id, since_seq=since_seq)
        return {
            "ok": True,
            "run_id": run_id,
            "doc_id": doc_id,
            "since_seq": since_seq,
            "events": events,
            "count": len(events),
        }
