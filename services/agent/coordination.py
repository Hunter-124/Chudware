"""coordination.py — per-run shared work board for sub-agents.

Sub-agents (and the root chat) publish non-exclusive work intents for
doc ids / feature ids / file paths. Multiple agents may work on the same
resource; the board is peer awareness, never custody or permission to edit.
The engine serializes document mutation batches, not entire agent tasks.

Persistence: in-memory dict keyed by run_id, with an optional JSON
sidecar under the platform runs directory (same pattern as
buildgraph sidecars).
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

COORDINATION_SCHEMA = "aicad.coordination/2"
DEFAULT_PROGRESS_LIMIT = 50


def _runs_dir() -> Path:
    """Platform default runs directory (mirrors buildgraph.mcp_tools._runs_dir)."""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "Chudware" / "runs"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sidecar_path(run_id: str, runs_dir: Path) -> Path:
    return runs_dir / f"{run_id}.coordination.json"


def _claim_lock_path(run_id: str, runs_dir: Path) -> Path:
    return runs_dir / f"{run_id}.coordination.lock"



@dataclass
class Claim:
    """One agent's advisory work intent on a resource."""
    claim_id: str
    agent_id: str
    resource: str
    intent: str
    ts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "agent_id": self.agent_id,
            "resource": self.resource,
            "intent": self.intent,
            "ts": self.ts,
        }


@dataclass
class ProgressNote:
    """A short progress note posted by an agent."""
    agent_id: str
    note: str
    ts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "note": self.note,
            "ts": self.ts,
        }


class CoordinationBoard:
    """Shared, per-run work intents and progress; overlapping work is allowed."""

    def __init__(
        self,
        run_id: str,
        *,
        runs_dir: Path | None = None,
        persist: bool = True,
        progress_limit: int = DEFAULT_PROGRESS_LIMIT,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self.run_id = run_id or f"r_{uuid.uuid4().hex[:8]}"
        self._runs_dir = runs_dir
        self._persist = persist
        self._progress_limit = max(1, int(progress_limit))
        self._lock = threading.RLock()
        # Live GUI event sink for board posts (agent_events ``live`` shape). When
        # unset, posts follow the ambient per-turn sink instead (_live_sink).
        self.on_event = on_event
        # (resource, agent_id) -> Claim; peers never replace each other's intent.
        self._claims: dict[tuple[str, str], Claim] = {}
        self._progress: list[ProgressNote] = []
        if self._persist:
            with self._lock, self._claims_file_lock():
                self._load_sidecar()

    # -- claims ---------------------------------------------------------------

    def register_claim(
        self,
        agent_id: str,
        resources: list[str],
        intent: str,
    ) -> dict[str, Any]:
        """Record non-exclusive work on *resources* for *agent_id*.

        Re-registering the same agent/resource refreshes its intent and keeps
        its claim id. Peers may register the same resource without waiting.
        Persisted read-modify-write operations serialize only the sidecar update.
        """
        agent = str(agent_id or "").strip()
        if not agent:
            return {"ok": False, "error": "agent_id must be non-empty"}
        intent_s = str(intent or "").strip()
        if not intent_s:
            return {"ok": False, "error": "intent must be non-empty"}
        res_list = list(dict.fromkeys(
            str(r).strip() for r in (resources or []) if str(r).strip()
        ))
        if not res_list:
            return {"ok": False, "error": "resources must be a non-empty list"}

        with self._lock, self._claims_file_lock():
            if self._persist:
                self._load_sidecar()

            granted: list[dict[str, Any]] = []
            ts = _now()
            for res in res_list:
                existing = self._claims.get((res, agent))
                if existing is not None:
                    # Refresh intent on re-claim by same agent.
                    existing.intent = intent_s
                    existing.ts = ts
                    granted.append(existing.to_dict())
                else:
                    claim = Claim(
                        claim_id=f"cl_{uuid.uuid4().hex[:12]}",
                        agent_id=agent,
                        resource=res,
                        intent=intent_s,
                        ts=ts,
                    )
                    self._claims[(res, agent)] = claim
                    granted.append(claim.to_dict())
            self._flush_sidecar()
            return {
                "ok": True,
                "status": "claimed",
                "claims": granted,
                "agent_id": agent,
                "intent": intent_s,
            }

    def release_claim(
        self,
        agent_id: str,
        resources: list[str] | None = None,
        *,
        claim_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Remove this agent's work intents, never another agent's entries.

        Select by resources or claim_ids; omitting both removes all entries
        for this agent. An empty explicit selection removes nothing.
        """
        agent = str(agent_id or "").strip()
        if not agent:
            return {"ok": False, "error": "agent_id must be non-empty"}

        with self._lock, self._claims_file_lock():
            if self._persist:
                self._load_sidecar()
            released: list[str] = []
            ids = set(map(str, claim_ids)) if claim_ids is not None else None
            selected = {str(r).strip() for r in resources} if resources is not None else None
            for key, claim in list(self._claims.items()):
                if claim.agent_id != agent:
                    continue
                if ids is not None:
                    if claim.claim_id not in ids:
                        continue
                elif selected is not None and claim.resource not in selected:
                    continue
                del self._claims[key]
                released.append(claim.resource)
            self._flush_sidecar()
            return {
                "ok": True,
                "status": "released",
                "agent_id": agent,
                "released": released,
                "count": len(released),
            }

    def post_progress(self, agent_id: str, note: str) -> dict[str, Any]:
        """Append a progress note from *agent_id* (ring-buffered)."""
        agent = str(agent_id or "").strip()
        if not agent:
            return {"ok": False, "error": "agent_id must be non-empty"}
        text = str(note or "").strip()
        if not text:
            return {"ok": False, "error": "note must be non-empty"}
        # Cap note length so the board stays compact.
        if len(text) > 500:
            text = text[:499].rstrip() + "…"

        with self._lock, self._claims_file_lock():
            if self._persist:
                self._load_sidecar()
            entry = ProgressNote(agent_id=agent, note=text, ts=_now())
            self._progress.append(entry)
            if len(self._progress) > self._progress_limit:
                self._progress = self._progress[-self._progress_limit:]
            self._flush_sidecar()
        self._forward_post(agent, text)
        return {
            "ok": True,
            "status": "posted",
            "agent_id": agent,
            "note": text,
            "ts": entry.ts,
        }

    # -- live forwarding --------------------------------------------------------

    def _live_sink(self) -> Callable[..., None] | None:
        """The board's own sink, else the turn's ambient live sink.

        ``agent_turn.run_turn`` installs its ordered event sink on
        ``task_list``'s contextvar for every turn; a board post made by a tool
        dispatched inside that turn reaches the same stream through it.
        """
        if self.on_event is not None:
            return self.on_event
        try:
            from services.agent import task_list  # noqa: PLC0415 — optional
            return task_list._SINK.get()
        except Exception:  # noqa: BLE001 — forwarding is display-only
            return None

    def _forward_post(self, agent: str, text: str) -> None:
        """Mirror one board post to the GUI as an ``agent.message`` (channel=board).

        Display-only: a missing or raising sink never affects the post.
        """
        sink = self._live_sink()
        if sink is None:
            return
        try:
            from services.agent import agent_events  # noqa: PLC0415
            agent_events.emit_event(
                "agent.message",
                {
                    "node_id": agent,
                    "id": agent_events.new_span_id(),
                    "run_id": self.run_id,
                    "actor": agent,
                    "channel": "board",
                    "from": agent,
                    "to": "board",
                    "text": text,
                    "timestamp": int(time.time() * 1000),
                },
                live=sink,
            )
        except Exception:  # noqa: BLE001
            pass

    def board_snapshot(self) -> dict[str, Any]:
        """Return fresh work intents and recent progress, including peer updates."""
        with self._lock, self._claims_file_lock():
            if self._persist:
                self._load_sidecar()
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        claims = [c.to_dict() for c in self._claims.values()]
        claims.sort(key=lambda c: (c["resource"], c["agent_id"]))
        progress = [p.to_dict() for p in self._progress]
        return {
            "schema": COORDINATION_SCHEMA,
            "run_id": self.run_id,
            "claims": claims,
            "progress": progress,
            "claim_count": len(claims),
            "progress_count": len(progress),
        }

    # -- persistence ----------------------------------------------------------

    def _read_sidecar_data(self) -> dict[str, Any] | None:
        """Fresh parse of the on-disk sidecar, or ``None`` when absent/unreadable."""
        rd = self._runs_dir or _runs_dir()
        path = _sidecar_path(self.run_id, rd)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _parse_claims(self, data: dict[str, Any]) -> dict[tuple[str, str], Claim]:
        claims: dict[tuple[str, str], Claim] = {}
        claims_raw = data.get("claims") or []
        if isinstance(claims_raw, list):
            for item in claims_raw:
                if not isinstance(item, dict):
                    continue
                res = str(item.get("resource") or "").strip()
                agent = str(item.get("agent_id") or "").strip()
                if not res or not agent:
                    continue
                claims[(res, agent)] = Claim(
                    claim_id=str(item.get("claim_id") or f"cl_{uuid.uuid4().hex[:12]}"),
                    agent_id=agent,
                    resource=res,
                    intent=str(item.get("intent") or ""),
                    ts=str(item.get("ts") or ""),
                )
        return claims

    def _load_sidecar(self) -> None:
        data = self._read_sidecar_data()
        if data is None:
            return
        self._claims = self._parse_claims(data)
        self._progress = []
        prog_raw = data.get("progress") or []
        if isinstance(prog_raw, list):
            for item in prog_raw:
                if not isinstance(item, dict):
                    continue
                agent = str(item.get("agent_id") or "").strip()
                note = str(item.get("note") or "").strip()
                if not agent or not note:
                    continue
                self._progress.append(ProgressNote(
                    agent_id=agent,
                    note=note,
                    ts=str(item.get("ts") or ""),
                ))
            if len(self._progress) > self._progress_limit:
                self._progress = self._progress[-self._progress_limit:]

    @contextlib.contextmanager
    def _claims_file_lock(self):
        """Lock only a board read/update, never the modeled resource or task."""
        if not self._persist:
            yield
            return
        rd = self._runs_dir or _runs_dir()
        rd.mkdir(parents=True, exist_ok=True)
        with open(_claim_lock_path(self.run_id, rd), "a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _flush_sidecar(self) -> None:
        if not self._persist:
            return
        try:
            rd = self._runs_dir or _runs_dir()
            rd.mkdir(parents=True, exist_ok=True)
            path = _sidecar_path(self.run_id, rd)
            payload = self._snapshot_locked()
            tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass


# Process-local board registry (one board per run_id).
_BOARDS: dict[str, CoordinationBoard] = {}
_BOARDS_LOCK = threading.RLock()


def get_board(
    run_id: str,
    *,
    runs_dir: Path | None = None,
    persist: bool = True,
    create: bool = True,
    on_event: Callable[..., None] | None = None,
) -> CoordinationBoard | None:
    """Return the shared CoordinationBoard for *run_id* (create on demand).

    *on_event* binds the board's live GUI sink when it has none yet.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _BOARDS_LOCK:
        board = _BOARDS.get(rid)
        if board is None:
            if not create:
                return None
            board = CoordinationBoard(rid, runs_dir=runs_dir, persist=persist)
            _BOARDS[rid] = board
        if on_event is not None and board.on_event is None:
            board.on_event = on_event
        return board


def drop_board(run_id: str) -> None:
    """Remove a board from the process registry (tests / run teardown)."""
    rid = str(run_id or "").strip()
    with _BOARDS_LOCK:
        _BOARDS.pop(rid, None)


def clear_boards() -> None:
    """Test helper — wipe the process-local registry."""
    with _BOARDS_LOCK:
        _BOARDS.clear()
