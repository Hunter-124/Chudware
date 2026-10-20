"""Worker-RPC bridge for sub-agent viewing, steering, and control.

The file-backed :mod:`services.agent.subagent_registry` is exposed over the
same newline-delimited JSON worker seam as other services.  Every method
degrades to an ``ok: false`` envelope rather than raising.

Methods:

  subagent_list       {limit?, session_id?, project_dir?} -> {ok, sessions}
  subagent_transcript {subagent_id, limit?, raw?, scope?} -> {ok, session,
                                                             messages,
                                                             pending_steers}
  subagent_steer      {subagent_id, text, scope?}        -> {ok, queued, note}
  subagent_terminate  {subagent_id, confirm, scope?}     -> {ok,
                                                             terminate_requested}
  subagent_control    {subagent_id, op, ..., scope?}     -> {ok, note,
                                                             session_status,
                                                             paused,
                                                             *_override}

``scope`` is the optional ``project_dir`` and ``session_id`` pair.  Each one
the caller sends narrows the request: ``project_dir`` must equal the record's
owning project after ``expanduser``/``realpath``/``normcase``, and
``session_id`` must equal the record's owning chat session.  A record without
the requested ownership never matches, and a blank requested value matches
nothing (it never widens to "any").  ``subagent_list`` returns the
intersection of both filters, newest first, with ``limit`` applied after
filtering.  The detail and control methods check scope before any transcript
read or registry write and refuse a mismatch with ``_OUT_OF_SCOPE_REASON`` and
no record metadata.  Callers that send neither key keep the unscoped behavior.

Steering queues a message for the sub-agent's next turn boundary; it does not
interrupt an in-flight completion.  A steer sent to a finished session remains
pending and the response includes the session status so the GUI can say so.
"""
from __future__ import annotations

import os
from typing import Callable
from services.agent import request_context

_OUT_OF_SCOPE_REASON = "Sub-agent is outside the requested scope."
_UNLIMITED = 2_147_483_647


def _canonical_project(value: object) -> str:
    """Comparable project key (registry stores ``realpath(expanduser(dir))``)."""
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.realpath(os.path.expanduser(text)))


def _requested_scope(request: dict) -> tuple[str | None, str | None]:
    """Return ``(project, session)`` filters.

    ``None`` means the caller did not scope on that key; ``""`` means it did
    with a blank value, which matches no record.
    """
    project = request.get("project_dir")
    session = request.get("session_id")
    return (
        None if project is None else _canonical_project(project),
        None if session is None else str(session).strip(),
    )


def _in_scope(meta: dict, project: str | None, session: str | None) -> bool:
    if project is not None:
        owner_project = _canonical_project(meta.get("project_dir"))
        if not project or not owner_project or owner_project != project:
            return False
    if session is not None:
        owner_session = str(meta.get("session_id") or "").strip()
        if not session or not owner_session or owner_session != session:
            return False
    return True


def _not_found() -> dict:
    result = {"ok": False, "error": "not_found", "reason": "not_found"}
    if request_context.in_cell():
        result["code"] = "not_found"
    return result


def _scoped_session(reg, request: dict, sid: str) -> tuple[dict | None, dict | None]:
    """Look up *sid* and enforce the request scope before any other access.

    Returns ``(session, None)`` when the caller may act on it, otherwise
    ``(None, refusal)``.
    """
    session = reg.get_session(sid)
    if session is None:
        return None, (
            _not_found()
            if request_context.in_cell()
            else {"ok": False, "reason": f"unknown subagent {sid!r}"}
        )
    if not _in_scope(session, *_requested_scope(request)):
        return None, (
            _not_found()
            if request_context.in_cell()
            else {"ok": False, "reason": _OUT_OF_SCOPE_REASON}
        )
    return session, None


def _subagent_list(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        reg.note_controller("gui")
        limit = int(request.get("limit", 50))
        project, session_id = _requested_scope(request)
        if project == "" or session_id == "":
            return {"ok": True, "sessions": []}
        # The registry prefilter skips foreign records' transcript counts; the
        # shared predicate then applies the same rule the detail methods use.
        sessions = reg.list_sessions(
            limit=_UNLIMITED,
            project_dir=(
                None if project is None
                else str(request.get("project_dir")).strip()
            ),
        )
        sessions = [
            session for session in sessions
            if _in_scope(session, project, session_id)
        ][:max(1, limit)]
        return {"ok": True, "sessions": sessions}
    except KeyError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_list failed: {exc}"}


def _subagent_transcript(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        sid = request.get("subagent_id", "")
        if not sid and not request_context.in_cell():
            return {"ok": False, "reason": "subagent_id is required"}
        session, refusal = _scoped_session(reg, request, sid)
        if refusal is not None:
            return refusal
        limit = int(request.get("limit", 200))
        raw = bool(request.get("raw", False))
        messages = (
            reg.read_raw(sid, limit=limit)
            if raw
            else reg.get_transcript(sid, limit=limit)
        )
        return {
            "ok": True,
            "session": session,
            "messages": messages,
            "pending_steers": reg.pending_steers(sid),
        }
    except KeyError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_transcript failed: {exc}"}


def _subagent_steer(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        sid = request.get("subagent_id", "")
        text = request.get("text", "")
        if not sid and not request_context.in_cell():
            return {"ok": False, "reason": "subagent_id is required"}
        if request_context.in_cell():
            session, refusal = _scoped_session(reg, request, sid)
            if refusal is not None:
                return refusal
        if not str(text).strip():
            return {"ok": False, "reason": "steer text must be non-empty"}
        if not request_context.in_cell():
            session, refusal = _scoped_session(reg, request, sid)
        if refusal is not None:
            return refusal
        rec = reg.queue_steer(sid, text, source="user")
        # Mirror the steer into the transcript immediately so the viewer shows it
        # (tagged queued; the runner logs consumption when it lands).
        reg.append_transcript(sid, "user", str(text),
                              steer_id=rec["steer_id"], queued=True)
        status = session.get("status", "")
        note = ("queued — applied at the sub-agent's next turn boundary"
                if status in ("pending", "approved", "running")
                else f"queued, but the sub-agent is {status or 'inactive'} — "
                     "it will only be read if the session resumes")
        return {"ok": True, "queued": True, "steer_id": rec["steer_id"],
                "session_status": status, "note": note}
    except KeyError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_steer failed: {exc}"}


def _subagent_terminate(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        sid = request.get("subagent_id", "")
        if not sid and not request_context.in_cell():
            return {"ok": False, "reason": "subagent_id is required"}
        if request_context.in_cell():
            session, refusal = _scoped_session(reg, request, sid)
            if refusal is not None:
                return refusal
        if not bool(request.get("confirm", False)):
            # M16: ending a sub-agent chat must never be an easy accident.
            return {"ok": False, "reason": "terminate requires confirm=true",
                    "requires_confirm": True}
        if not request_context.in_cell():
            session, refusal = _scoped_session(reg, request, sid)
        if refusal is not None:
            return refusal
        if not reg.request_terminate(sid):
            return {"ok": False, "reason": "terminate flag could not be set"}
        reg.append_transcript(sid, "system",
                              "Termination requested by the user — the sub-agent "
                              "stops at its next turn boundary.")
        return {"ok": True, "terminate_requested": True,
                "session_status": session.get("status", ""),
                "note": "honored at the sub-agent's next turn boundary (not a hard kill)"}
    except KeyError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_terminate failed: {exc}"}


def _subagent_control(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        sid = request.get("subagent_id", "")
        if not sid and not request_context.in_cell():
            return {"ok": False, "reason": "subagent_id is required"}
        if request_context.in_cell():
            session, refusal = _scoped_session(reg, request, sid)
            if refusal is not None:
                return refusal

        op = str(request.get("op", "")).strip().lower()
        valid_ops = ("pause", "resume", "set_runtime")
        if op not in valid_ops:
            return {
                "ok": False,
                "reason": "op must be one of: " + ", ".join(valid_ops),
            }
        if not request_context.in_cell():
            session, refusal = _scoped_session(reg, request, sid)
        if refusal is not None:
            return refusal

        note = ""
        if op == "pause":
            if session.get("status") in (
                "done", "failed", "revoked", "terminated", "abandoned",
            ):
                return {
                    "ok": False,
                    "reason": (
                        "cannot pause terminal sub-agent in status "
                        f"{session.get('status')!r}"
                    ),
                }
            if not reg.set_paused(sid, True):
                return {"ok": False, "reason": "pause flag could not be set"}
            note = (
                "pause takes effect at the sub-agent's next turn boundary; "
                "work already in flight finishes first"
            )
        elif op == "resume":
            if not reg.set_paused(sid, False):
                return {"ok": False, "reason": "pause flag could not be cleared"}
            note = "resumed; the sub-agent continues at its next turn-boundary poll"
        elif op == "set_runtime":
            if not reg.set_runtime_override(
                sid,
                model=request.get("model") if "model" in request else None,
                provider=(
                    request.get("provider") if "provider" in request else None
                ),
                effort=request.get("effort") if "effort" in request else None,
            ):
                return {"ok": False, "reason": "runtime override could not be set"}
            note = (
                "the model/provider swap and effort cap change apply from the "
                "sub-agent's next model completion, not the in-flight one"
            )

        current = reg.get_session(sid) or {}
        overrides = reg.runtime_override(sid)
        return {
            "ok": True,
            "note": note,
            "session_status": str(current.get("status", "")),
            "paused": bool(current.get("paused", False)),
            "model_override": overrides["model"],
            "provider_override": overrides["provider"],
            "effort_override": overrides["effort"],
        }
    except KeyError:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_control failed: {exc}"}


# Registered by engine_oracle/worker.py via ``_METHODS.update(WORKER_METHODS)``.
WORKER_METHODS: dict[str, Callable] = {
    "subagent_list": _subagent_list,
    "subagent_transcript": _subagent_transcript,
    "subagent_steer": _subagent_steer,
    "subagent_terminate": _subagent_terminate,
    "subagent_control": _subagent_control,
}
