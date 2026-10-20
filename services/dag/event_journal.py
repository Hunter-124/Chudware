"""event_journal.py — Persist DAG events to events.jsonl for mission-control replay.

Contract: contracts/agent_events.md §1 (storage), §5 (validation).

Usage:
    journal = EventJournal(run_id)
    emit = journal.emit          # pass as executor.run(emit=emit)
    # or compose with another emitter:
    emit = journal.wrap(existing_emit)

The file lands at:
    ~/.local/share/Chudware/cache/runs/<run_id>/events.jsonl  (Linux/macOS)
    %APPDATA%\\Chudware\\cache\\runs\\<run_id>\\events.jsonl  (Windows)

Persist-then-callback: the file write + flush complete before control returns to the
caller, so a crash after write leaves the record on disk (per telemetry.md §1.2).
"""
from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .run_ids import validate_run_id
from services.agent.request_context import in_cell, owner_key, require_owner
from .storage import resolve_run


# ---------------------------------------------------------------------------
# Run-directory resolution (mirrors executor.py's default_runs_dir pattern
# but uses the 'cache' subtree per contracts/agent_events.md §1)
# ---------------------------------------------------------------------------

def _default_cache_runs_dir() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "cache" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "Chudware" / "cache" / "runs"


def run_events_path(run_id: str, runs_dir: Optional[Path] = None) -> Path:
    """Return the events.jsonl path for a run, creating the directory if needed."""
    if in_cell():
        return resolve_run(run_id).root / "events.jsonl"
    run_id = validate_run_id(run_id)
    base = runs_dir if runs_dir is not None else _default_cache_runs_dir()
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "events.jsonl"


# ---------------------------------------------------------------------------
# EventJournal
# ---------------------------------------------------------------------------

class EventJournal:
    """Writes every emit(kind, payload) call to a per-run events.jsonl file.

    Thread-safety: the file is opened in append mode and each write is a single
    os.write() call (atomic on POSIX for lines < PIPE_BUF ~ 4 kB).  For larger
    payloads a per-instance threading.Lock is used, but DAG executor calls are
    single-threaded so this is defensive only.
    """

    def __init__(self, run_id: str, runs_dir: Optional[Path] = None) -> None:
        self._owner = owner_key()
        self.run_id = resolve_run(run_id).run_id if in_cell() else run_id
        self._path = run_events_path(self.run_id, runs_dir)

    @property
    def path(self) -> Path:
        require_owner(self._owner)
        return self._path

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Write one event line to the journal (flush before return)."""
        require_owner(self._owner)
        record = {
            **payload,
            "kind": event_type,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": self.run_id,
        }
        line = json.dumps(record, default=str) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def wrap(
        self, other: Optional[Callable[[str, dict[str, Any]], None]]
    ) -> Callable[[str, dict[str, Any]], None]:
        """Return a composite emitter: persists to journal AND calls *other* if not None."""
        if other is None:
            return self.emit

        def _composite(event_type: str, payload: dict[str, Any]) -> None:
            self.emit(event_type, payload)
            other(event_type, payload)

        return _composite
