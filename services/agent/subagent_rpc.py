"""subagent_rpc.py — worker-RPC bridge for sub-agent chat viewing + live steering.

Phase 4 (tracker M11/M15–M17).  Exposes the sub-agent session registry
(:mod:`services.agent.subagent_registry`) over the SAME newline-delimited-JSON
worker seam the GUI already uses for every other service (WorkerClient /
AsyncRpc) — no bespoke transport.  Registered append-only in
``engine_oracle/worker.py`` following the services/dag/worker_rpc.py precedent.

Methods (each degrades to an ``ok: false`` envelope, never raises):

  subagent_list       {limit?}                    -> {ok, sessions:[...]}
  subagent_transcript {subagent_id, limit?}       -> {ok, session, messages, pending_steers}
  subagent_steer      {subagent_id, text}         -> {ok, steer_id, queued, note}
  subagent_terminate  {subagent_id, confirm}      -> {ok, terminate_requested} — REFUSES
                                                     without confirm=true (the M16 guard:
                                                     ending a sub-agent is never one click)
  subagent_control    {subagent_id, op, ...}       -> {ok, note, session_status,
                                                     paused, awaiting_approval,
                                                     spawn_decision, *_override}

Injection semantics (be honest with the user): ``subagent_steer`` QUEUES the
message; the sub-agent consumes it at its next turn boundary
(``agent_turn.run_turn(subagent_id=...)`` drains the queue before each model
completion).  A steer sent to a finished session stays pending — the response
carries the session status so the GUI can say so.
"""
from __future__ import annotations

from typing import Callable


def _subagent_list(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        # This poll IS the "a human is watching" signal: only while it keeps
        # arriving will a spawn approval gate actually hold a mission (see
        # delegation.await_registry_decision). An unattended run must never
        # block on an approval nobody is there to give.
        reg.note_watcher("gui")
        limit = int((params or {}).get("limit", 50))
        return {"ok": True, "sessions": reg.list_sessions(limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"subagent_list failed: {exc}"}


def _subagent_transcript(params: dict, _emit: Callable) -> dict:
    try:
        from services.agent import subagent_registry as reg
        sid = (params or {}).get("subagent_id", "")
        if not sid:
            return {"ok": False, "reason": "subagent_id is required"}
        session = reg.get_session(sid)
        if session is None:
            return {"ok": False, "reason": f"unknown subagent {sid!r}"}
        limit = int((params or {}).get("limit", 200))
        return {
            "ok": True,
            "session": session,
            "messages": reg.get_transcript(sid, limit=limit),
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
        valid_ops = ("pause", "resume", "approve", "deny", "set_runtime")
        if op not in valid_ops:
            return {
                "ok": False,
                "reason": "op must be one of: " + ", ".join(valid_ops),
            }
        if op == "deny" and not bool(request.get("confirm", False)):
            return {
                "ok": False,
                "reason": "deny requires confirm=true",
                "requires_confirm": True,
            }
        session = reg.get_session(sid)
        if session is None:
            return {"ok": False, "reason": f"unknown subagent {sid!r}"}

        note = ""
        if op == "pause":
            if session.get("status") in (
                "done", "failed", "revoked", "terminated",
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
        else:
            decision = str(session.get("spawn_decision", ""))
            if not session.get("awaiting_approval") and not decision:
                return {
                    "ok": False,
                    "reason": "sub-agent is not awaiting spawn approval",
                }
            approve = op == "approve"
            if not reg.decide_spawn(sid, approve=approve, by="user"):
                return {"ok": False, "reason": "spawn decision could not be set"}
            decision_word = "approved" if approve else "denied"
            transcript_note = (
                f"Spawn {decision_word} by the user — honored at the "
                "sub-agent's next turn boundary."
            )
            reg.append_transcript(sid, "system", transcript_note)
            note = (
                f"spawn {decision_word}; the decision takes effect at the "
                "sub-agent's next turn-boundary poll"
            )

        current = reg.get_session(sid) or {}
        overrides = reg.runtime_override(sid)
        return {
            "ok": True,
            "note": note,
            "session_status": str(current.get("status", "")),
            "paused": bool(current.get("paused", False)),
            "awaiting_approval": bool(
                current.get("awaiting_approval", False)),
            "spawn_decision": str(current.get("spawn_decision", "")),
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
