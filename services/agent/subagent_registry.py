"""File-backed sub-agent registry, display transcript, and lossless raw log.

Sub-agents can run in a different process from the worker serving the GUI's
``subagent_*`` RPCs, so session state and control requests must be visible
across process boundaries.  The registry keeps these records on disk:

    <subagents_dir>/<subagent_id>.json              — session metadata
    <subagents_dir>/<subagent_id>.transcript.jsonl  — user-facing display log
    <subagents_dir>/<subagent_id>.steer.jsonl       — steering queue
    <transcript_dir>/<subagent_id>.jsonl            — lossless raw log

The display transcript contains concise lifecycle and UI notes.  It is not the
conversation record.  The separate raw JSONL stores uncapped provider messages,
compactions, and lifecycle events append-only beside the owning chat session so
it travels with that conversation.  If no owning session directory is bound,
the raw file falls back to the registry cache and still always exists.

``subagents_dir`` defaults to ``~/.local/share/Chudware/cache/subagents``;
``CHUDWARE_SUBAGENTS_DIR`` overrides it.

Spawning is automatic; there is no human spawn gate.  Steering and termination
are observed at turn boundaries, so work already in flight finishes first.
A pause is likewise a turn-boundary hold with no wall-clock deadline: it lasts
until resume or termination, except that a paused runner releases the hold when
no controller remains alive that could ever resume it.

``queue_steer`` only appends to the queue file.  ``agent_turn.run_turn`` drains
``consume_steers`` before each model completion, placing queued text into the
next provider call.  A steer queued after completion remains pending.

Metadata is atomically replaced, and every read-modify-write of it runs under a
per-session cross-process lock (``_update_meta``): the runners live inside the
agent worker while the GUI's ``subagent_*`` RPCs are served by a different
worker process, so an unlocked update could silently drop the other side's
write.  Display, raw, and steer records use append writes; POSIX ``O_APPEND``
keeps complete raw records and concurrent steering writes from overwriting one
another.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from services.agent import raw_log, request_context
from services import paths

SCHEMA = "aicad.subagent_session/1"
RAW_SCHEMA = "chudware.subagent.raw/1"

#: Live statuses can outlive a worker crash; reconciliation marks them abandoned.
STATUSES = (
    "pending", "approved", "running", "in_progress", "waiting", "paused",
    "done", "failed", "revoked", "terminated", "abandoned",
)
LIVE_STATUSES = frozenset(("pending", "approved", "running", "in_progress", "waiting", "paused"))
UNKNOWN_OWNER_GRACE_S = 24 * 60 * 60


def _process_start_time(pid: int) -> str | None:
    """Linux /proc stat field 22 (clock ticks since boot), stable across PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def _runner_gone(meta: dict) -> str:
    """Return a reason only when a live status has lost its owning runner."""
    if meta.get("status") not in LIVE_STATUSES:
        return ""
    pid = meta.get("owner_pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return f"owning runner pid {pid} is gone"
        except PermissionError:
            return ""  # inaccessible, but still running
        except OSError:
            pass  # PID lookup unavailable on this platform
        else:
            recorded_start = str(meta.get("owner_start_time") or "")
            actual_start = _process_start_time(pid)
            if recorded_start and actual_start and recorded_start != actual_start:
                return f"owning runner pid {pid} was reused"
            return ""  # a live process is never abandoned on age alone
    timestamp = (
        meta.get("runner_heartbeat_ts")
        or meta.get("created_ts")
        or meta.get("updated_ts")
    )
    try:
        age = time.time() - datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return ""
    if age > UNKNOWN_OWNER_GRACE_S:
        return "owning runner unavailable; no heartbeat for over 24 hours"
    return ""


def resolve_project_dir(start: str | Path | None = None) -> str:
    """Resolve the run's project root, preferring explicit run-scoped roots.

    An explicit root (``CHUDWARE_PROJECT_DIR``, then ``CHUDWARE_FS_ROOT``,
    then *start*) is the selected workspace and is kept as given, only
    canonicalized: climbing to a marked parent would merge distinct nested
    projects.  Only the ambient configured workspace fallback is widened to
    its outermost marked project root.
    """
    for candidate in (
        request_context.root_value("CHUDWARE_PROJECT_DIR"),
        request_context.root_value("CHUDWARE_FS_ROOT"),
        None if request_context.in_cell() else start,
    ):
        explicit = str(candidate or "").strip()
        if explicit:
            return os.path.realpath(os.path.expanduser(explicit))
    if request_context.in_cell():
        return "/work"
    from services.agent.config import workspace_dir
    from services.agent.project_context import resolve_project_root
    return os.path.realpath(
        os.path.expanduser(str(resolve_project_root(workspace_dir())))
    )


def heartbeat(subagent_id: str) -> None:
    """Runner-only liveness write at turn boundaries, including pause holds."""
    def touch(meta: dict) -> bool:
        if meta.get("status") not in LIVE_STATUSES:
            return False
        meta["runner_heartbeat_ts"] = _now()
        return True
    _update_meta(subagent_id, touch)

_CONTROL_DEFAULTS = {
    "paused": False,
    "model_override": "",
    "provider_override": "",
    "effort_override": "",
}

_BOUND_SESSION_ID = ""
_BOUND_TRANSCRIPT_DIR = ""
_CELL_BINDING: ContextVar[tuple[object, str, str] | None] = ContextVar(
    "subagent_session_binding", default=None
)


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
    if request_context.in_cell():
        p = paths.actor_root() / "subagents"
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p
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


def bind_session(session_id: str, transcript_dir: str) -> None:
    """Bind new sub-agents to an owning chat session in this process.

    Passing empty strings clears the binding.
    """
    if request_context.in_cell():
        _CELL_BINDING.set((request_context.owner_key(), str(session_id or "").strip(), ""))
        return
    global _BOUND_SESSION_ID, _BOUND_TRANSCRIPT_DIR
    _BOUND_SESSION_ID = str(session_id or "").strip()
    directory = str(transcript_dir or "").strip()
    _BOUND_TRANSCRIPT_DIR = (
        str(Path(directory).expanduser().resolve()) if directory else ""
    )


def bound_session() -> tuple[str, Path | None]:
    """Return the process-bound owning session and sub-agent transcript dir."""
    if request_context.in_cell():
        binding = _CELL_BINDING.get()
        if binding is None or not request_context.owns(binding[0]):
            return "", None
        return binding[1], None
    directory = Path(_BOUND_TRANSCRIPT_DIR) if _BOUND_TRANSCRIPT_DIR else None
    return _BOUND_SESSION_ID, directory


def _meta_path(subagent_id: str) -> Path:
    if request_context.in_cell():
        return paths.private_path("subagents", subagent_id).with_suffix(".json")
    return subagents_dir() / f"{subagent_id}.json"


def _transcript_path(subagent_id: str) -> Path:
    if request_context.in_cell():
        return _meta_path(subagent_id).with_suffix(".transcript.jsonl")
    return subagents_dir() / f"{subagent_id}.transcript.jsonl"


def _steer_path(subagent_id: str) -> Path:
    if request_context.in_cell():
        return _meta_path(subagent_id).with_suffix(".steer.jsonl")
    return subagents_dir() / f"{subagent_id}.steer.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_id(subagent_id: str) -> str:
    """Reject path-traversal-shaped ids (registry files are keyed by id)."""
    if request_context.in_cell() and (
            not isinstance(subagent_id, str) or "\x00" in subagent_id
            or subagent_id != subagent_id.strip()):
        raise KeyError("not_found")
    sid = str(subagent_id or "").strip()
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        if request_context.in_cell():
            raise KeyError("not_found")
        raise ValueError(f"invalid subagent_id: {subagent_id!r}")
    return sid


def _write_meta(subagent_id: str, meta: dict) -> None:
    """Atomic rewrite (tmp + rename) so readers never see a torn file.

    Callers that derive *meta* from the stored record must hold ``_meta_lock``
    (use ``_update_meta``); a bare call is only for a record nobody else writes.
    """
    if request_context.in_cell() and meta.get("tenant_owner") != list(request_context.owner_key()):
        raise KeyError("not_found")
    path = _meta_path(subagent_id)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


_META_THREAD_LOCK = threading.Lock()


@contextmanager
def _meta_lock(subagent_id: str) -> Iterator[None]:
    """Hold one session's metadata exclusively across processes and threads.

    Same lock shape as services/plm/vault.py ``_local_config_lock``: an
    ``fcntl.flock`` (``msvcrt.locking`` on Windows) on a ``<id>.lock`` sidecar,
    plus a process-local lock so this process's threads queue before the file.
    Not re-entrant — never acquire it while already holding it.
    """
    path = _meta_path(subagent_id).with_suffix(".lock")
    with _META_THREAD_LOCK:
        with open(path, "a+b") as lock:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415 — Windows only
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415 — POSIX only
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _update_meta(subagent_id: str, mutate: Callable[[dict], Any]) -> Optional[dict]:
    """Read, mutate in place, and rewrite one session record under ``_meta_lock``.

    Returns the written record; ``None`` for an unknown or invalid id, or when
    *mutate* returns ``False`` (nothing to write).  Exceptions from *mutate*
    propagate with the record untouched.
    """
    try:
        sid = _safe_id(subagent_id)
    except ValueError:
        return None
    with _meta_lock(sid):
        meta = get_session(sid)
        if meta is None and request_context.in_cell():
            raise KeyError("not_found")
        if meta is None or mutate(meta) is False:
            return None
        _write_meta(sid, meta)
        return meta


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
    subagent_id: str | None = None,
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
    session_id: str = "",
    transcript_dir: str = "",
    project_dir: str | Path | None = None,
) -> dict:
    """Mint a new cell session; desktop retains caller-keyed create/refresh."""
    sid = f"dg_{uuid.uuid4().hex}" if request_context.in_cell() else _safe_id(subagent_id)
    with _meta_lock(sid):
        return _register_locked(
            sid, role=role, brief=brief, spawn_kind=spawn_kind,
            parent_run_id=parent_run_id, parent_node_id=parent_node_id,
            requested_by=requested_by, model=model, budget_tokens=budget_tokens,
            status=status, session_id=session_id, transcript_dir=transcript_dir,
            project_dir=project_dir,
        )


def _register_locked(sid: str, *, role, brief, spawn_kind, parent_run_id,
                     parent_node_id, requested_by, model, budget_tokens, status,
                     session_id, transcript_dir, project_dir) -> dict:
    existing = get_session(sid) or {}
    bound_id, bound_dir = bound_session()
    owner_session_id = str(session_id or bound_id).strip()
    owner_dir = str(transcript_dir or bound_dir or "").strip()
    if request_context.in_cell():
        raw_transcript_path = _meta_path(sid).with_suffix(".raw.jsonl")
    elif owner_dir:
        raw_transcript_path = (
            Path(owner_dir).expanduser().resolve() / f"{sid}.jsonl"
        )
    else:
        raw_transcript_path = (subagents_dir() / f"{sid}.raw.jsonl").resolve()
    meta = {
        "schema": SCHEMA,
        **({"tenant_owner": list(request_context.owner_key())} if request_context.in_cell() else {}),
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
        "model_override": str(existing.get("model_override", "")),
        "provider_override": str(existing.get("provider_override", "")),
        "effort_override": str(existing.get("effort_override", "")),
        "session_id": owner_session_id,
        "raw_transcript_path": str(raw_transcript_path),
        "project_dir": existing.get("project_dir") or resolve_project_dir(project_dir),
        "owner_pid": existing.get("owner_pid") or os.getpid(),
        "owner_start_time": existing.get("owner_start_time") or _process_start_time(os.getpid()),
        "runner_heartbeat_ts": existing.get("runner_heartbeat_ts") or _now(),
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
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(meta, dict):
        return None
    if request_context.in_cell() and meta.get("tenant_owner") != list(request_context.owner_key()):
        return None
    return _default_control_state(meta)


def set_status(subagent_id: str, status: str, *, note: str = "") -> bool:
    """Update a session's status (mirrors delegation node transitions)."""
    if status not in STATUSES:
        return False

    def apply(meta: dict) -> None:
        meta["status"] = status
        meta["updated_ts"] = _now()
        if note:
            meta["status_note"] = str(note)[:300]
    return _update_meta(subagent_id, apply) is not None


def list_sessions(limit: int = 50, *, project_dir: str | None = None) -> list[dict]:
    """Registry snapshot, newest-updated first, optionally scoped to a project."""
    project_filter = (
        os.path.realpath(os.path.expanduser(project_dir))
        if project_dir is not None else None
    )
    sessions: list[dict] = []
    try:
        directory = paths.actor_root() / "subagents" if request_context.in_cell() else subagents_dir()
        for p in directory.glob("*.json"):
            if p.suffixes[-2:-1] == [".tmp"]:
                continue
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(meta, dict) or meta.get("schema") != SCHEMA:
                continue
            if request_context.in_cell() and meta.get("tenant_owner") != list(request_context.owner_key()):
                continue
            sid = meta.get("id", "")
            _default_control_state(meta)
            reason = _runner_gone(meta)
            if reason:
                # Re-read under the lock before replacing: another process may
                # have completed it while this list request was reading.
                def abandon(current: dict) -> bool:
                    if current.get("status") not in LIVE_STATUSES:
                        return False
                    why = _runner_gone(current)
                    if not why:
                        return False
                    current["status"] = "abandoned"
                    current["status_note"] = why
                    current["updated_ts"] = _now()
                    return True
                meta = _update_meta(sid, abandon) or meta
            if project_filter is not None and (
                not meta.get("project_dir")
                or meta["project_dir"] != project_filter
            ):
                continue
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
    if request_context.in_cell() and get_session(sid) is None:
        raise KeyError("not_found")
    rec = {"ts": _now(), "who": who, "text": str(text)}
    if meta:
        rec.update(meta)
    _append_line(_transcript_path(sid), rec)
    # Touch updated_ts so list_sessions sorts recently-active sessions first.
    _update_meta(sid, lambda m: m.__setitem__("updated_ts", _now()))


def get_transcript(subagent_id: str, limit: int = 200) -> list[dict]:
    try:
        sid = _safe_id(subagent_id)
    except ValueError:
        return []
    if request_context.in_cell() and get_session(sid) is None:
        raise KeyError("not_found")
    recs = _read_lines(_transcript_path(sid))
    return recs[-max(1, int(limit)):]


def raw_path(subagent_id: str) -> Path | None:
    """Return the absolute lossless-log path for a registered sub-agent."""
    meta = get_session(subagent_id)
    if meta is None:
        if request_context.in_cell():
            raise KeyError("not_found")
        return None
    value = str(meta.get("raw_transcript_path", "")).strip()
    if request_context.in_cell():
        return _meta_path(subagent_id).with_suffix(".raw.jsonl")
    return Path(value) if value else None


def append_raw(subagent_id: str, record: dict) -> None:
    """Append an uncapped raw record for a registered sub-agent."""
    try:
        sid = _safe_id(subagent_id)
        path = raw_path(sid)
        if path is None:
            return
        meta = get_session(sid) or {}
        raw_log.append(path, record, {
            "kind": "header",
            "schema": RAW_SCHEMA,
            "subagent_id": sid,
            "session_id": str(meta.get("session_id", "")),
            "role": str(meta.get("role", "")),
            "parent_id": str(meta.get("parent_node_id", "")),
            "run_id": str(meta.get("parent_run_id", "")),
            "created_ts": time.time(),
        })
    except KeyError:
        raise
    except Exception:  # noqa: BLE001 — raw persistence must not break the turn
        return


def read_raw(
    subagent_id: str,
    limit: int = 0,
    offset: int = 0,
) -> list[dict]:
    """Read raw records in append order; zero *limit* means no limit."""
    try:
        path = raw_path(subagent_id)
        return raw_log.read(path, limit=limit, offset=offset) if path else []
    except KeyError:
        raise
    except Exception:  # noqa: BLE001 — registry reads are best-effort
        return []


# ---------------------------------------------------------------------------
# Steering queue (injection), terminate flag, and turn-boundary controls
# ---------------------------------------------------------------------------

def queue_steer(subagent_id: str, text: str, *, source: str = "user") -> dict:
    """Queue a steering message.  Returns the queued record (with steer_id).

    The message is CONSUMED at the sub-agent's next turn boundary
    (``consume_steers`` inside ``agent_turn.run_turn``) — nothing is interrupted.
    """
    sid = _safe_id(subagent_id)
    if request_context.in_cell() and get_session(sid) is None:
        raise KeyError("not_found")
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
    if request_context.in_cell() and get_session(sid) is None:
        raise KeyError("not_found")
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
# Controller heartbeat — "could anyone ever release a pause?"
# ---------------------------------------------------------------------------
#
# The GUI polls ``subagent_list`` every few seconds while it can control the
# runner.  That poll is only a controller-liveness signal.  It is not a human
# attention or approval signal.  A paused runner uses it to avoid parking
# forever after the GUI process that could resume or terminate it has died.

#: A controller remains live for 120 seconds after its last poll.  At the GUI's
#: roughly four-second polling interval this tolerates about 30 missed polls,
#: making expiry evidence of unambiguous controller death rather than a hiccup.
CONTROLLER_GRACE_S = 120.0


def _controller_path() -> Path:
    return subagents_dir() / "controllers.json"


def note_controller(kind: str = "gui") -> None:
    """Record that a controller capable of releasing a pause just polled."""
    try:
        path = _controller_path()
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


def controller_alive(within_s: float = CONTROLLER_GRACE_S) -> bool:
    """True when some pause-capable controller polled within *within_s* seconds."""
    try:
        path = _controller_path()
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
    def flag(meta: dict) -> None:
        meta["terminate_requested"] = True
        meta["updated_ts"] = _now()
    return _update_meta(subagent_id, flag) is not None


def terminate_requested(subagent_id: str) -> bool:
    meta = get_session(subagent_id)
    if meta is None and request_context.in_cell():
        raise KeyError("not_found")
    return bool(meta and meta.get("terminate_requested"))


def set_paused(subagent_id: str, paused: bool) -> bool:
    """Set or clear the turn-boundary pause flag."""
    def apply(meta: dict) -> None:
        meta["paused"] = bool(paused)
        meta["updated_ts"] = _now()
    return _update_meta(subagent_id, apply) is not None


def is_paused(subagent_id: str) -> bool:
    meta = get_session(subagent_id)
    if meta is None and request_context.in_cell():
        raise KeyError("not_found")
    return bool(meta and meta.get("paused"))


def set_runtime_override(
    subagent_id: str,
    *,
    model=None,
    provider=None,
    effort=None,
) -> bool:
    """Set runtime overrides; ``None`` leaves a value and ``""`` clears it."""
    def apply(meta: dict) -> None:
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
    return _update_meta(subagent_id, apply) is not None


def runtime_override(subagent_id: str) -> dict:
    meta = get_session(subagent_id) or {}
    if not meta and request_context.in_cell():
        raise KeyError("not_found")
    return {
        "model": str(meta.get("model_override", "")),
        "provider": str(meta.get("provider_override", "")),
        "effort": str(meta.get("effort_override", "")),
    }


