"""Worker-RPC bridge for sub-agent viewing, steering, and control.

The file-backed :mod:`services.agent.subagent_registry` is exposed over the
same newline-delimited JSON worker seam as other services.  Every method
degrades to an ``ok: false`` envelope rather than raising.

Methods:

  subagent_list       {limit?, session_id?, project_dir?} -> {ok, sessions}
  subagent_transcript {subagent_id, limit?, raw?}        -> {ok, session,
                                                             messages,
                                                             pending_steers}
  subagent_steer      {subagent_id, text}                -> {ok, queued, note}
  subagent_terminate  {subagent_id, confirm}             -> {ok,
                                                             terminate_requested}
  subagent_control    {subagent_id, op, ...}             -> {ok, note,
                                                             session_status,
                                                             paused,
                                                             *_override}

Steering queues a message for the sub-agent's next turn boundary; it does not
interrupt an in-flight completion.  A steer sent to a finished session remains
pending and the response includes the session status so the GUI can say so.
"""
from __future__ import annotations

from typing import Callable


def _subagent_list(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        reg.note_controller("gui")
        limit = int(request.get("limit", 50))
        session_id = str(request.get("session_id", "")).strip()
        sessions = reg.list_sessions(
            limit=2_147_483_647 if session_id else limit,
            project_dir=request.get("project_dir"),
        )
        if session_id:
            sessions = [
                session for session in sessions
                if str(session.get("session_id", "")) == session_id
            ][:max(1, limit)]
        return {"ok": True, "sessions": sessions}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_list failed: {exc}"}


def _subagent_transcript(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        sid = request.get("subagent_id", "")
        if not sid:
            return {"ok": False, "reason": "subagent_id is required"}
        session = reg.get_session(sid)
        if session is None:
            return {"ok": False, "reason": f"unknown subagent {sid!r}"}
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
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_transcript failed: {exc}"}


def _subagent_steer(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        sid = (params or {}).get("subagent_id", "")
        text = (params or {}).get("text", "")
        if not sid:
            return {"ok": False, "reason": "subagent_id is required"}
        if not str(text).strip():
            return {"ok": False, "reason": "steer text must be non-empty"}
        session = reg.get_session(sid)
        if session is None:
            return {"ok": False, "reason": f"unknown subagent {sid!r}"}
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
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_steer failed: {exc}"}


def _subagent_terminate(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        sid = (params or {}).get("subagent_id", "")
        if not sid:
            return {"ok": False, "reason": "subagent_id is required"}
        if not bool((params or {}).get("confirm", False)):
            # M16: ending a sub-agent chat must never be an easy accident.
            return {"ok": False, "reason": "terminate requires confirm=true",
                    "requires_confirm": True}
        session = reg.get_session(sid)
        if session is None:
            return {"ok": False, "reason": f"unknown subagent {sid!r}"}
        if not reg.request_terminate(sid):
            return {"ok": False, "reason": "terminate flag could not be set"}
        reg.append_transcript(sid, "system",
                              "Termination requested by the user — the sub-agent "
                              "stops at its next turn boundary.")
        return {"ok": True, "terminate_requested": True,
                "session_status": session.get("status", ""),
                "note": "honored at the sub-agent's next turn boundary (not a hard kill)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_terminate failed: {exc}"}


def _subagent_control(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        request = params or {}
        sid = request.get("subagent_id", "")
        if not sid:
            return {"ok": False, "reason": "subagent_id is required"}

        op = str(request.get("op", "")).strip().lower()
        valid_ops = ("pause", "resume", "set_runtime")
        if op not in valid_ops:
            return {
                "ok": False,
                "reason": "op must be one of: " + ", ".join(valid_ops),
            }
        session = reg.get_session(sid)
        if session is None:
            return {"ok": False, "reason": f"unknown subagent {sid!r}"}

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
