"""workspace_tools.py — MCP tools for the multi-doc workspace.

Family: ``workspace``.
Risk: ``workspace_status`` → read (added to _READ_EXACT in tool_registry);
      all other workspace tools → write (default, lease-gated upstream).

Contract: contracts/advisor.md §12 (leases), §13 (buildgraph), §2.14.
"""
from __future__ import annotations

import difflib
import os
import sys
from typing import Any

from services.mcp import core_bridge as cb
from services.mcp.tool_registry import model_selectors
from services.agent import request_context


def _current_doc_id() -> str:
    """The document this process is pointed at, or "" — never raises.

    Read out of the ALREADY-LOADED cad_tools module (whichever identity this
    package was imported under). Importing it fresh here would mint a SECOND
    cad_tools object with its own _CUR — the 12x-autosave bug class — and a
    cad_tools that was never loaded has no current document anyway.
    """
    for name in (f"{__package__}.cad_tools", "services.mcp.cad_tools",
                 "services.mcp.cad_tools"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            return str(getattr(mod, "_CUR", {}).get("doc_id") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _turn_project_dir() -> str:
    """The project directory of the current turn, or "" when none is set.

    The online turn binds request-local roots (services/agent/online_turn.py).
    The project dir has priority over the filesystem root.
    """
    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        raw = request_context.root_value(key).strip()
        if raw:
            return os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
    return ""


def _doc_location(path: Any, project: str) -> str:
    """Classify an open document: "project", "outside_project" or "unsaved"."""
    if not path:
        return "unsaved"
    if not project:
        return "outside_project"
    p = os.path.normpath(os.path.abspath(str(path)))
    try:
        inside = os.path.commonpath([p, project]) == project
    except ValueError:  # different drives on Windows
        inside = False
    return "project" if inside else "outside_project"


def _turn_snapshot(ts: str) -> dict:
    """The real workspace of a turn that has no multi-doc session.

    GUI chat turns and plain MCP clients never register a WorkspaceSession: only
    a multi-doc orchestration does. Their workspace is the project directory of
    the turn plus the documents open in the engine. Report that state, or say
    exactly which part is unavailable. Never report an empty list as success.
    """
    project = _turn_project_dir()
    out: dict[str, Any] = {
        "schema": "aicad.workspace/1",
        "scope": "turn",
        "run_id": "",
        "ts": ts,
        "project_dir": project,
        "current_doc_id": _current_doc_id(),
    }
    try:
        listed = cb.call("doc_list", {}) or {}
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not hidden
        out.update(
            ok=False,
            reason="engine_unavailable",
            error=f"The engine did not return the open documents: {exc}",
            hint=("Use fs_list() to see the project files. Call workspace_status() "
                  "again when the engine is available."),
        )
        return out
    docs = []
    for row in listed.get("docs") or []:
        if isinstance(row, dict) and row.get("doc_id"):
            docs.append({**row, "location": _doc_location(row.get("path"), project)})
    out.update(ok=True, docs=docs)
    out["note"] = (
        "No multi-doc workspace session is registered. docs lists every document "
        "that is open in the engine."
    )
    if not project:
        out["note"] += " This turn has no project directory."
    return out


def register_workspace_tools(mcp) -> None:
    """Register workspace tools under the ``workspace`` family."""

    @mcp.tool()
    @model_selectors("run_id")
    def workspace_status(run_id: str = "") -> dict:
        """Return the workspace snapshot: project directory, open documents, owners.

        Omit run_id in a chat turn. Then the snapshot shows the project directory
        of the turn and every document open in the engine (scope "turn"). Each
        document has a location: "project", "outside_project" or "unsaved".
        If exactly one multi-doc session is registered, the snapshot shows that
        session (scope "session"): document owners, status and latest render.

        Give run_id only to select a registered multi-doc session. A chat turn
        run id does not name a session. An unknown run_id returns ok=false with
        the registered run ids and the call to use instead.
        Risk: read (named in _READ_EXACT in tool_registry; no lease required).
        """
        from services.agent import workspace as ws

        reg = ws._session_registry
        rid = (run_id or "").strip()
        if not rid:
            if not reg:
                return _turn_snapshot(ws._now())
            if len(reg) > 1:
                known = sorted(reg.keys())
                return {
                    "ok": False,
                    "schema": "aicad.workspace/1",
                    "reason": "run_id_required",
                    "run_id": "",
                    "error": f"{len(reg)} multi-doc workspace sessions are registered.",
                    "known_run_ids": known,
                    "hint": "Give run_id to select one session.",
                    "do_instead": [f"workspace_status(run_id='{k}')" for k in known],
                }
            session = next(iter(reg.values()))
        else:
            session = reg.get(rid)
            if session is None:
                if request_context.in_cell():
                    return {"ok": False, "error": "not_found", "reason": "not_found"}
                # Production code registers a WorkspaceSession only for a multi-doc
                # orchestration. A GUI chat turn ("gui-...") or a mission run id
                # never resolves here, so the error must teach the call that works.
                known = sorted(reg.keys())
                close = difflib.get_close_matches(rid, known, n=1, cutoff=0.5)
                out: dict[str, Any] = {
                    "ok": False,
                    "schema": "aicad.workspace/1",
                    "reason": "unknown_run_id",
                    "run_id": rid,
                    "error": f"no multi-doc workspace session has run_id={rid!r}",
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
                        "No multi-doc workspace session is registered. A chat turn run "
                        "id does not name a session. Call workspace_status() with no "
                        "run_id to get the project directory and the open documents."
                    )
                    out["do_instead"] = ["workspace_status()", "list_parts()"]
                    cur = _current_doc_id()
                    if cur:
                        out["current_doc_id"] = cur
                return out
        return {"ok": True, "scope": "session", **session.workspace_status()}

    @mcp.tool()
    @model_selectors("run_id")
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

        try:
            events = read_doc_events(run_id, doc_id, since_seq=since_seq)
        except KeyError:
            if request_context.in_cell():
                return {"ok": False, "error": "not_found", "reason": "not_found"}
            raise
        return {
            "ok": True,
            "run_id": run_id,
            "doc_id": doc_id,
            "since_seq": since_seq,
            "events": events,
            "count": len(events),
        }
