"""coordination.py — per-run shared work board for sub-agents.

Sub-agents (and the root chat) claim exclusive ownership of resources
(doc ids / feature ids / file paths) so parallel workers do not clobber
each other's edits.  Conflicting claims return CONFLICT with the holder
and its intent — never a hard deadlock; the second agent must wait or
narrow scope.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

COORDINATION_SCHEMA = "aicad.coordination/1"
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


class _FileFlock:
    """Exclusive interprocess flock (POSIX) held for one ``with`` block.

    Same fallback as services/agent/providers/sub_auth.py's credential-store
    lock: a no-op when ``fcntl`` is unavailable (non-POSIX).  Closes the lock
    file handle on exit — callers open a fresh handle per call.
    """

    def __init__(self, lf) -> None:
        self._lf = lf
        try:
            import fcntl  # noqa: PLC0415 — optional on non-POSIX
            self._fcntl = fcntl
        except ImportError:  # pragma: no cover
            self._fcntl = None

    def __enter__(self) -> None:
        if self._fcntl is not None:
            self._fcntl.flock(self._lf.fileno(), self._fcntl.LOCK_EX)
        return None

    def __exit__(self, *exc: object) -> bool:
        try:
            if self._fcntl is not None:
                self._fcntl.flock(self._lf.fileno(), self._fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


@dataclass
class Claim:
    """One exclusive claim on a single resource."""
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
    """Shared, per-run work board.

    *register_claim* is atomic per resource: the first agent wins; a second
    claim on the same resource returns CONFLICT listing the holder + intent.
    """

    def __init__(
        self,
        run_id: str,
        *,
        runs_dir: Path | None = None,
        persist: bool = True,
        progress_limit: int = DEFAULT_PROGRESS_LIMIT,
    ) -> None:
        self.run_id = run_id or f"r_{uuid.uuid4().hex[:8]}"
        self._runs_dir = runs_dir
        self._persist = persist
        self._progress_limit = max(1, int(progress_limit))
        self._lock = threading.RLock()
        # resource -> Claim
        self._claims: dict[str, Claim] = {}
        self._progress: list[ProgressNote] = []
        if self._persist:
            self._load_sidecar()

    # -- claims ---------------------------------------------------------------

    def register_claim(
        self,
        agent_id: str,
        resources: list[str],
        intent: str,
    ) -> dict[str, Any]:
        """Claim exclusive ownership of *resources* for *agent_id*.

        Returns ``{ok: True, claims: [...]}`` on success, or
        ``{ok: False, status: "CONFLICT", conflicts: [...], message: ...}``
        when any resource is already held by a different agent.
        Idempotent re-claim by the same agent refreshes intent and succeeds.

        Cross-process exclusive: when persisted, the whole read-modify-write
        (re-read the sidecar, check conflicts, grant, flush) runs under an
        interprocess file lock, so two CoordinationBoard instances in different
        processes can never both win the same resource — the second re-reads
        the sidecar INSIDE the lock and sees the first's claim even if both
        started the call before either had flushed.
        """
        agent = str(agent_id or "").strip()
        if not agent:
            return {"ok": False, "error": "agent_id must be non-empty"}
        intent_s = str(intent or "").strip()
        if not intent_s:
            return {"ok": False, "error": "intent must be non-empty"}
        res_list = [str(r).strip() for r in (resources or []) if str(r).strip()]
        if not res_list:
            return {"ok": False, "error": "resources must be a non-empty list"}

        with self._lock, self._claims_file_lock():
            if self._persist:
                self._reload_claims_locked()
            conflicts: list[dict[str, Any]] = []
            for res in res_list:
                held = self._claims.get(res)
                if held is not None and held.agent_id != agent:
                    conflicts.append({
                        "resource": res,
                        "holder": held.agent_id,
                        "intent": held.intent,
                        "claim_id": held.claim_id,
                        "ts": held.ts,
                    })
            if conflicts:
                return {
                    "ok": False,
                    "status": "CONFLICT",
                    "conflicts": conflicts,
                    "message": (
                        "claim conflict: resource(s) already held — wait or narrow "
                        "scope; do not hard-deadlock"
                    ),
                    "hint": (
                        # The model-facing names, not this class's method names:
                        # release_claim/post_progress are CoordinationBoard methods it
                        # cannot call — the tools are the MissionDispatch verbs
                        # (mission_dispatch._handlers). Named wrong until
                        # advertised_tool_audit caught it, 2026-07-30.
                        "Release is not automatic. Narrow resources, wait for the "
                        "holder to coordinate_release, or coordinate via "
                        "coordinate_progress."
                    ),
                }

            granted: list[dict[str, Any]] = []
            ts = _now()
            for res in res_list:
                existing = self._claims.get(res)
                if existing is not None and existing.agent_id == agent:
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
                    self._claims[res] = claim
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
        """Release claims held by *agent_id*.

        If *resources* is given, only those resources are released (must be
        held by this agent).  If *claim_ids* is given, those claim ids are
        released.  If both are omitted, ALL claims for the agent are released.
        """
        agent = str(agent_id or "").strip()
        if not agent:
            return {"ok": False, "error": "agent_id must be non-empty"}

        with self._lock:
            released: list[str] = []
            if claim_ids:
                id_set = {str(c).strip() for c in claim_ids if str(c).strip()}
                for res, claim in list(self._claims.items()):
                    if claim.agent_id == agent and claim.claim_id in id_set:
                        del self._claims[res]
                        released.append(res)
            elif resources:
                for res in resources:
                    r = str(res).strip()
                    claim = self._claims.get(r)
                    if claim is not None and claim.agent_id == agent:
                        del self._claims[r]
                        released.append(r)
            else:
                for res, claim in list(self._claims.items()):
                    if claim.agent_id == agent:
                        del self._claims[res]
                        released.append(res)
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

        with self._lock:
            entry = ProgressNote(agent_id=agent, note=text, ts=_now())
            self._progress.append(entry)
            if len(self._progress) > self._progress_limit:
                self._progress = self._progress[-self._progress_limit:]
            self._flush_sidecar()
            return {
                "ok": True,
                "status": "posted",
                "agent_id": agent,
                "note": text,
                "ts": entry.ts,
            }

    def board_snapshot(self) -> dict[str, Any]:
        """Return active claims + recent progress notes."""
        with self._lock:
            claims = [c.to_dict() for c in self._claims.values()]
            # Stable order by resource for deterministic tests / prompts.
            claims.sort(key=lambda c: (c.get("resource") or "", c.get("ts") or ""))
            progress = [p.to_dict() for p in self._progress]
            return {
                "schema": COORDINATION_SCHEMA,
                "run_id": self.run_id,
                "claims": claims,
                "progress": progress,
                "claim_count": len(claims),
                "progress_count": len(progress),
            }

    def preamble_text(self, *, max_chars: int = 1200) -> str:
        """Compact board snapshot for injection into a sub-agent system prompt."""
        snap = self.board_snapshot()
        claims = snap.get("claims") or []
        progress = snap.get("progress") or []
        lines = [
            "[Coordination board — claim resources before editing to avoid clobbering peers]",
            f"run_id={self.run_id} active_claims={len(claims)}",
        ]
        if claims:
            lines.append("Active claims:")
            for c in claims[:20]:
                lines.append(
                    f"  - {c.get('resource')}: held by {c.get('agent_id')} "
                    f"({c.get('intent')})"
                )
            if len(claims) > 20:
                lines.append(f"  … +{len(claims) - 20} more")
        else:
            lines.append("Active claims: (none)")
        if progress:
            lines.append("Recent progress:")
            for p in progress[-8:]:
                lines.append(f"  - [{p.get('agent_id')}] {p.get('note')}")
        lines.append(
            "Tools: coordinate_claim(resources, intent), coordinate_release(resources?), "
            "coordinate_progress(note), coordinate_board()."
        )
        text = "\n".join(lines)
        if len(text) > max_chars:
            return text[: max_chars - 1].rstrip() + "…"
        return text

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

    def _parse_claims(self, data: dict[str, Any]) -> dict[str, Claim]:
        claims: dict[str, Claim] = {}
        claims_raw = data.get("claims") or []
        if isinstance(claims_raw, list):
            for item in claims_raw:
                if not isinstance(item, dict):
                    continue
                res = str(item.get("resource") or "").strip()
                agent = str(item.get("agent_id") or "").strip()
                if not res or not agent:
                    continue
                claims[res] = Claim(
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

    def _reload_claims_locked(self) -> None:
        """Replace in-memory claims with on-disk truth.

        Call ONLY while holding both the thread lock and the interprocess
        file lock (``_claims_file_lock``) — this closes the cross-process
        double-claim race: another process may have granted and flushed a
        claim since this instance last read the sidecar, and the conflict
        check that follows in ``register_claim`` must see it.
        """
        data = self._read_sidecar_data()
        if data is None:
            return
        self._claims = self._parse_claims(data)

    def _claims_file_lock(self):
        """Interprocess exclusive lock for the ``register_claim`` critical section.

        No sidecar file exists to race on when persistence is off, so a pure
        in-memory board (most tests) gets a no-op — single-process behavior
        and perf are unchanged.
        """
        if not self._persist:
            return contextlib.nullcontext()
        rd = self._runs_dir or _runs_dir()
        rd.mkdir(parents=True, exist_ok=True)
        lf = open(_claim_lock_path(self.run_id, rd), "a+", encoding="utf-8")
        return _FileFlock(lf)

    def _flush_sidecar(self) -> None:
        if not self._persist:
            return
        try:
            rd = self._runs_dir or _runs_dir()
            rd.mkdir(parents=True, exist_ok=True)
            path = _sidecar_path(self.run_id, rd)
            payload = self.board_snapshot()
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
) -> CoordinationBoard | None:
    """Return the shared CoordinationBoard for *run_id* (create on demand)."""
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _BOARDS_LOCK:
        board = _BOARDS.get(rid)
        if board is not None:
            return board
        if not create:
            return None
        board = CoordinationBoard(rid, runs_dir=runs_dir, persist=persist)
        _BOARDS[rid] = board
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
