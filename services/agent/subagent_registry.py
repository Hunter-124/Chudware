"""subagent_registry.py — live sub-agent session registry + steering queue (Phase 4, M11/M15–M17).

The GUI's subagent chat viewer/steerer needs an enumerable, cross-process view of
sub-agent sessions: who was spawned, by which run, what they've said, and a way to
inject a human steering message into a RUNNING sub-agent's loop.

Why file-backed
---------------
Sub-agents run in whatever process hosts the mission (the MCP server or the
engine worker's mission path), while the GUI's ``subagent_list`` /
``subagent_steer`` RPCs are served by the engine worker process
(``engine_oracle/worker.py``).  An in-memory registry would be invisible across
that boundary, so every record lives on disk:

    <subagents_dir>/<subagent_id>.json              — session metadata (atomic rewrite)
    <subagents_dir>/<subagent_id>.transcript.jsonl  — append-only conversation transcript
    <subagents_dir>/<subagent_id>.steer.jsonl       — append-only steering queue

``subagents_dir`` defaults to ``~/.local/share/Chudware/cache/subagents`` (the same
cache subtree as the agent-events journal); ``CHUDWARE_SUBAGENTS_DIR`` overrides it
(tests point it at a tmpdir).

Steering (injection) semantics — HONEST
---------------------------------------
``queue_steer`` only appends to the queue file; nothing is interrupted.  The
consumption point is the sub-agent's turn boundary: ``agent_turn.run_turn``
(when called with ``subagent_id=``) drains ``consume_steers()`` before EACH
model completion, so a queued steer lands as a user-role message at the
sub-agent's next model call.  A steer queued after the sub-agent finished stays
pending forever (the GUI shows the session status so the user can tell).
``request_terminate`` is the same shape: a flag honored at the next turn
boundary — never a hard kill.  Pause, approval holds, and runtime overrides
are also polled only at turn boundaries: work already in flight finishes first,
and a model/provider or effort change starts with the next model completion.

Concurrency model: one writer per sub-agent for meta/transcript (the runner);
the steer queue is append-only from any process and consumed only by the runner.
POSIX O_APPEND line writes keep the queue safe under concurrent steers.
"""
from __future__ import annotations

import json
import os
import platform
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = "aicad.subagent_session/1"

#: Session statuses mirror delegation node statuses plus "terminated".
STATUSES = (
    "pending", "approved", "running", "paused", "done", "failed", "revoked",
    "terminated",
)

_CONTROL_DEFAULTS = {
    "paused": False,
    "awaiting_approval": False,
    "spawn_decision": "",
    "model_override": "",
    "provider_override": "",
    "effort_override": "",
}


def _default_control_state(meta: dict) -> dict:
    """Return *meta* with control-plane keys present for legacy records."""
    for key, default in _CONTROL_DEFAULTS.items():
        meta.setdefault(key, default)
    return meta


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

def subagents_dir() -> Path:
    """The registry directory (created on demand).  Env-overridable for tests."""
    env = os.environ.get("CHUDWARE_SUBAGENTS_DIR")
    if env:
        p = Path(env)
    elif platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        p = Path(appdata) / "Chudware" / "cache" / "subagents"
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        p = Path(xdg) / "Chudware" / "cache" / "subagents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _meta_path(subagent_id: str) -> Path:
    return subagents_dir() / f"{subagent_id}.json"


def _transcript_path(subagent_id: str) -> Path:
    return subagents_dir() / f"{subagent_id}.transcript.jsonl"


def _steer_path(subagent_id: str) -> Path:
    return subagents_dir() / f"{subagent_id}.steer.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_id(subagent_id: str) -> str:
    """Reject path-traversal-shaped ids (registry files are keyed by id)."""
    sid = str(subagent_id or "").strip()
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        raise ValueError(f"invalid subagent_id: {subagent_id!r}")
    return sid


def _write_meta(subagent_id: str, meta: dict) -> None:
    """Atomic rewrite (tmp + rename) so readers never see a torn file."""
    path = _meta_path(subagent_id)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _append_line(path: Path, record: dict) -> None:
    line = json.dumps(record, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
            except Exception:  # noqa: BLE001 — skip torn/corrupt lines
                continue
    except Exception:  # noqa: BLE001
        return out
    return out


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def register_session(
    subagent_id: str,
    *,
    role: str,
    brief: str = "",
    spawn_kind: str = "worker",
    parent_run_id: str = "",
    parent_node_id: str = "",
    requested_by: str = "",
    model: str = "",
    budget_tokens: int = 0,
    status: str = "pending",
    awaiting_approval: bool = False,
) -> dict:
    """Create (or refresh) a session record.  Returns the stored metadata dict."""
    sid = _safe_id(subagent_id)
    existing = get_session(sid) or {}
    meta = {
        "schema": SCHEMA,
        "id": sid,
        "role": role,
        "brief": str(brief)[:500],
        "spawn_kind": spawn_kind,
        "parent_run_id": parent_run_id,
        "parent_node_id": parent_node_id,
        "requested_by": requested_by,
        "model": model,
        "budget_tokens": int(budget_tokens),
        "status": status if status in STATUSES else "pending",
        "terminate_requested": bool(existing.get("terminate_requested", False)),
        "paused": bool(existing.get("paused", False)),
        "awaiting_approval": bool(
            existing.get("awaiting_approval", awaiting_approval)),
        "spawn_decision": str(existing.get("spawn_decision", "")),
        "model_override": str(existing.get("model_override", "")),
        "provider_override": str(existing.get("provider_override", "")),
        "effort_override": str(existing.get("effort_override", "")),
        "created_ts": existing.get("created_ts") or _now(),
        "updated_ts": _now(),
    }
    _write_meta(sid, meta)
    return meta


def get_session(subagent_id: str) -> Optional[dict]:
    try:
        sid = _safe_id(subagent_id)
    except ValueError:
        return None
    path = _meta_path(sid)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        return _default_control_state(meta) if isinstance(meta, dict) else None
    except Exception:  # noqa: BLE001
        return None


def set_status(subagent_id: str, status: str, *, note: str = "") -> bool:
    """Update a session's status (mirrors delegation node transitions)."""
    if status not in STATUSES:
        return False
    meta = get_session(subagent_id)
    if meta is None:
        return False
    meta["status"] = status
    meta["updated_ts"] = _now()
    if note:
        meta["status_note"] = str(note)[:300]
    _write_meta(meta["id"], meta)
    return True


def list_sessions(limit: int = 50) -> list[dict]:
    """Registry snapshot, newest-updated first.  Adds live queue/transcript counts."""
    sessions: list[dict] = []
    try:
        for p in subagents_dir().glob("*.json"):
            if p.suffixes[-2:-1] == [".tmp"]:
                continue
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(meta, dict) or meta.get("schema") != SCHEMA:
                continue
            sid = meta.get("id", "")
            _default_control_state(meta)
            meta["pending_steers"] = len(pending_steers(sid)) if sid else 0
            meta["transcript_len"] = len(_read_lines(_transcript_path(sid))) if sid else 0
            sessions.append(meta)
    except Exception:  # noqa: BLE001
        pass
    sessions.sort(key=lambda m: str(m.get("updated_ts", "")), reverse=True)
    return sessions[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def append_transcript(subagent_id: str, who: str, text: str, **meta: Any) -> None:
    """Append one transcript record.  *who* ∈ {"user","subagent","system"}."""
    sid = _safe_id(subagent_id)
    rec = {"ts": _now(), "who": who, "text": str(text)}
    if meta:
        rec.update(meta)
    _append_line(_transcript_path(sid), rec)
    # Touch updated_ts so list_sessions sorts recently-active sessions first.
    m = get_session(sid)
    if m is not None:
        m["updated_ts"] = _now()
        _write_meta(sid, m)


def get_transcript(subagent_id: str, limit: int = 200) -> list[dict]:
    try:
        sid = _safe_id(subagent_id)
    except ValueError:
        return []
    recs = _read_lines(_transcript_path(sid))
    return recs[-max(1, int(limit)):]


# ---------------------------------------------------------------------------
# Steering queue (injection), terminate flag, and turn-boundary controls
# ---------------------------------------------------------------------------

def queue_steer(subagent_id: str, text: str, *, source: str = "user") -> dict:
    """Queue a steering message.  Returns the queued record (with steer_id).

    The message is CONSUMED at the sub-agent's next turn boundary
    (``consume_steers`` inside ``agent_turn.run_turn``) — nothing is interrupted.
    """
    sid = _safe_id(subagent_id)
    if not str(text).strip():
        raise ValueError("steer text must be non-empty")
    rec = {
        "kind": "steer",
        "steer_id": f"st_{uuid.uuid4().hex[:10]}",
        "ts": _now(),
        "source": source,
        "text": str(text),
    }
    _append_line(_steer_path(sid), rec)
    return rec


def pending_steers(subagent_id: str) -> list[dict]:
    """Steers queued but not yet consumed, in queue order."""
    try:
        sid = _safe_id(subagent_id)
    except ValueError:
        return []
    consumed: set[str] = set()
    steers: list[dict] = []
    for rec in _read_lines(_steer_path(sid)):
        if rec.get("kind") == "steer" and rec.get("steer_id"):
            steers.append(rec)
        elif rec.get("kind") == "consumed" and rec.get("steer_id"):
            consumed.add(rec["steer_id"])
    return [s for s in steers if s["steer_id"] not in consumed]


def consume_steers(subagent_id: str) -> list[dict]:
    """Drain pending steers (marking each consumed).  Called by the RUNNER only,
    at a turn boundary.  Returns the drained records in queue order."""
    sid = _safe_id(subagent_id)
    pending = pending_steers(sid)
    for s in pending:
        _append_line(_steer_path(sid), {
            "kind": "consumed", "steer_id": s["steer_id"], "ts": _now(),
        })
    return pending


# ---------------------------------------------------------------------------
# Watcher heartbeat — "is anyone actually there to answer?"
# ---------------------------------------------------------------------------
#
# An approval hold is only honest when a human can see the request. The GUI's
# AI workspace polls ``subagent_list`` every few seconds while it is open, so
# that poll IS the liveness signal. A headless/batch run (bench, CI, a mission
# with no GUI attached) has no watcher, and must NOT block waiting for an
# approval nobody will give — it keeps the pre-approval-gate behaviour of
# returning the node still pending.

#: A watcher counts as live for this long after its last poll. Comfortably more
#: than the GUI's ~4 s `pollSubagentList` interval, short enough that a closed
#: workspace stops blocking spawns within one window.
WATCHER_TTL_S = 30.0


def _watcher_path() -> Path:
    return subagents_dir() / "watchers.json"


def note_watcher(kind: str = "gui") -> None:
    """Record that an interactive watcher just polled the registry."""
    try:
        path = _watcher_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        data[str(kind)] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a heartbeat failure must never break a list call


def watcher_active(within_s: float = WATCHER_TTL_S) -> bool:
    """True when some interactive watcher polled within *within_s* seconds."""
    try:
        path = _watcher_path()
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        cutoff = time.time() - max(0.0, float(within_s))
        return any(isinstance(ts, (int, float)) and ts >= cutoff
                   for ts in data.values())
    except (OSError, ValueError, TypeError):
        return False


def request_terminate(subagent_id: str) -> bool:
    """Flag a session for termination — honored at the next turn boundary.

    This is deliberately NOT a hard kill: a synchronous sub-agent turn cannot be
    interrupted mid-completion; the runner checks this flag before each model
    call and stops cleanly.  Returns False for an unknown session.
    """
    meta = get_session(subagent_id)
    if meta is None:
        return False
    meta["terminate_requested"] = True
    meta["updated_ts"] = _now()
    _write_meta(meta["id"], meta)
    return True


def terminate_requested(subagent_id: str) -> bool:
    meta = get_session(subagent_id)
    return bool(meta and meta.get("terminate_requested"))


def set_paused(subagent_id: str, paused: bool) -> bool:
    """Set or clear the turn-boundary pause flag."""
    meta = get_session(subagent_id)
    if meta is None:
        return False
    meta["paused"] = bool(paused)
    meta["updated_ts"] = _now()
    _write_meta(meta["id"], meta)
    return True


def is_paused(subagent_id: str) -> bool:
    meta = get_session(subagent_id)
    return bool(meta and meta.get("paused"))


def set_runtime_override(
    subagent_id: str,
    *,
    model=None,
    provider=None,
    effort=None,
) -> bool:
    """Set runtime overrides; ``None`` leaves a value and ``""`` clears it."""
    meta = get_session(subagent_id)
    if meta is None:
        return False
    if model is not None:
        meta["model_override"] = str(model).strip()
    if provider is not None:
        meta["provider_override"] = str(provider).strip()
    if effort is not None:
        raw_effort = str(effort).strip()
        if raw_effort:
            from services.agent.role_models import (  # noqa: PLC0415
                clamp_subagent_effort,
            )
            clamped = clamp_subagent_effort(raw_effort)
            if clamped not in ("low", "medium", "high"):
                raise ValueError(
                    "effort must be one of: low, medium, high, or empty")
            meta["effort_override"] = clamped
        else:
            meta["effort_override"] = ""
    meta["updated_ts"] = _now()
    _write_meta(meta["id"], meta)
    return True


def runtime_override(subagent_id: str) -> dict:
    meta = get_session(subagent_id) or {}
    return {
        "model": str(meta.get("model_override", "")),
        "provider": str(meta.get("provider_override", "")),
        "effort": str(meta.get("effort_override", "")),
    }


def decide_spawn(
    subagent_id: str,
    *,
    approve: bool,
    by: str = "user",
) -> bool:
    """Record a pending spawn decision for the runner's next boundary poll."""
    meta = get_session(subagent_id)
    if meta is None:
        return False
    meta["spawn_decision"] = "approved" if approve else "denied"
    meta["awaiting_approval"] = False
    meta["spawn_decided_by"] = str(by)
    meta["spawn_decided_ts"] = _now()
    meta["updated_ts"] = _now()
    _write_meta(meta["id"], meta)
    if approve:
        set_status(meta["id"], "approved", note=f"spawn approved by {by}")
    return True


def spawn_decision(subagent_id: str) -> str:
    meta = get_session(subagent_id)
    return str(meta.get("spawn_decision", "")) if meta else ""


def awaiting_approval(subagent_id: str) -> bool:
    meta = get_session(subagent_id)
    return bool(meta and meta.get("awaiting_approval"))
