"""Hierarchical run-event emission + persistence (docs/contracts/events.md §3).

Every orchestrator event is (1) handed to the caller's ``emit`` callback (the worker forwards it as
a ``{id, event, data}`` line) AND (2) appended to
``%APPDATA%\\AI-CAD\\cache\\runs\\<run_id>\\events.jsonl`` (one JSON object per line). Raw model
outputs are capped at 16 KB in the event/jsonl but written in full to sidecar
``attempt_<stage>_<n>.txt`` files. The Activity panel (C22b) replays from events.jsonl after a
restart and tests assert against it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from engine import paths

EmitFn = Callable[[dict], None]

RAW_CAP = 16 * 1024  # 16 KB cap on raw text carried in events (full text lives on disk).


def run_dir(run_id: str) -> Path:
    d = paths.CACHE / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def cap_raw(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= RAW_CAP:
        return text
    return text[:RAW_CAP] + "\n...[raw truncated; full output in sidecar]"


class RunEvents:
    """Tags every event with ``run_id``, fans it out to the live sink + events.jsonl."""

    def __init__(self, run_id: str, emit: Optional[EmitFn] = None) -> None:
        self.run_id = run_id
        self._emit = emit
        self._dir = run_dir(run_id)
        self._jsonl = self._dir / "events.jsonl"
        # Truncate any stale file for this (uuid) run id.
        try:
            self._jsonl.write_text("", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def emit(self, kind: str, **data) -> dict:
        ev = {"kind": kind, "run_id": self.run_id, **data}
        # Persist first (the source of truth for replay), then surface live (best-effort).
        try:
            with self._jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
        except Exception:  # noqa: BLE001
            pass
        if self._emit is not None:
            try:
                self._emit(ev)
            except Exception:  # noqa: BLE001
                pass
        return ev

    def write_sidecar(self, stage: str, attempt: int, raw: str) -> str:
        """Write the uncapped raw model output to ``attempt_<stage>_<n>.txt``; return its path."""
        p = self._dir / f"attempt_{stage}_{attempt}.txt"
        try:
            p.write_text(raw or "", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return str(p)
