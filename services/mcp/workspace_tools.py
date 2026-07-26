"""workspace_tools.py — MCP tools for the multi-doc workspace (T-W16).

Family: ``workspace``.
Risk: ``workspace_status`` → read (added to _READ_EXACT in tool_registry);
      all other workspace tools → write (default, lease-gated upstream).

Contract: contracts/advisor.md §12 (leases), §13 (buildgraph), §2.14.
"""
from __future__ import annotations

import difflib
import sys
from typing import Any


def _current_doc_id() -> str:
    """The document this process is pointed at, or "" — never raises.

    Read out of the ALREADY-LOADED cad_tools module (whichever identity this
    package was imported under). Importing it fresh here would mint a SECOND
    cad_tools object with its own _CUR — the 12x-autosave bug class — and a
    cad_tools that was never loaded has no current document anyway.
    """
    for name in (f"{__package__}.cad_tools", "services.mcp.cad_tools",
                 "mcp_server.cad_tools"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            return str(getattr(mod, "_CUR", {}).get("doc_id") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


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
        contract) but the error is actionable: it lists the run ids that DO
        exist, suggests the closest spelling (suggestion only — never resolved
        for you), and when NO multi-doc session exists anywhere it says so and
        names the tools that answer the same question in a single-doc run.
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
                # ADVERTISED BUT IMPOSSIBLE (10 dead calls in the gym ledgers):
                # every one named the mission run_id, and nothing on the live
                # single-doc path ever calls workspace.register_session(), so the
                # old one-line error was unactionable — the model could only
                # retry. Say precisely what to do INSTEAD in this run.
                known = sorted(reg.keys())
                close = difflib.get_close_matches(rid, known, n=1, cutoff=0.5)
                out: dict[str, Any] = {
                    "ok": False,
                    "schema": "aicad.workspace/1",
                    "run_id": rid,
                    "error": f"no active workspace session for run_id={rid!r}",
                    "known_run_ids": known,
                    "did_you_mean": (close[0] if close else ""),
                    "docs": [],
                }
                if known:
                    out["hint"] = (
                        "Registered workspace sessions: "
                        + ", ".join(repr(k) for k in known)
                        + (f". Closest to {rid!r} is {close[0]!r} — call "
                           f"workspace_status(run_id='{close[0]}') if that is the one "
                           "you meant (not resolved for you)." if close else "")
                        + " workspace_status() with no run_id picks the ambient session."
                    )
                    out["do_instead"] = [
                        f"workspace_status(run_id='{known[0]}')",
                        "workspace_status()  # ambient session",
                    ]
                else:
                    out["hint"] = (
                        "There is no multi-doc workspace session anywhere in this "
                        "process — this is a single-doc run, so no run_id will ever "
                        "resolve here. Do NOT retry workspace_status with another id: "
                        "use describe_project() for the project view, list_parts() for "
                        "what is open, and describe_part(doc_id=...) for one document."
                    )
                    out["do_instead"] = [
                        "describe_project()",
                        "list_parts()",
                        "describe_part(doc_id=...)",
                    ]
                    cur = _current_doc_id()
                    if cur:
                        # Real state, not a fabricated session: the document this
                        # turn is actually pointed at.
                        out["current_doc_id"] = cur
                        out["do_instead"].insert(0, f"describe_part(doc_id='{cur}')")
                return out
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
