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
boundary — never a hard kill.

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
STATUSES = ("pending", "approved", "running", "done", "failed", "revoked", "terminated")


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
        return meta if isinstance(meta, dict) else None
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
# Steering queue (injection) + terminate flag
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
