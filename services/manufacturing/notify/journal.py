from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol


class Journal(Protocol):
    def append(self, kind: str, subject: str, body: dict[str, Any]) -> int: ...

    def replay(self, from_seq: int = 1) -> list[dict[str, Any]]: ...


class JsonlJournal:
    """Small durable adapter with the same record envelope as the gateway journal.

    Production can supply the hostd journal RPC adapter instead. This implementation is
    useful for an isolated worker and deliberately fsyncs each state transition.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        rows = self.replay()
        self._head = max((int(row["seq"]) for row in rows), default=0)

    def append(self, kind: str, subject: str, body: dict[str, Any]) -> int:
        if not kind or not subject:
            raise ValueError("journal kind and subject must be non-empty")
        with self._lock:
            self._head += 1
            row = {
                "seq": self._head,
                "kind": kind,
                "subject": subject,
                "body": deepcopy(body),
                "t_mono_ns": time.monotonic_ns(),
            }
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            return self._head

    def replay(self, from_seq: int = 1) -> list[dict[str, Any]]:
        if from_seq < 1:
            raise ValueError("from_seq must be positive")
        with self._lock:
            if not self.path.exists():
                return []
            rows: list[dict[str, Any]] = []
            with self.path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # A process can die between write and newline. Only a torn final row
                        # is recoverable; corruption in the middle is not silently skipped.
                        if not any(rest.strip() for rest in stream):
                            break
                        raise ValueError(f"corrupt journal row {line_number}") from None
                    if not isinstance(row, dict) or not isinstance(row.get("seq"), int):
                        raise ValueError(f"invalid journal row {line_number}")
                    if row["seq"] >= from_seq:
                        rows.append(row)
            rows.sort(key=lambda row: row["seq"])
            return rows


class MemoryJournal:
    """In-process journal with the production envelope, intended for embedding/tests."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._lock = threading.RLock()
        self._rows = deepcopy(rows or [])
        self._head = max((int(row["seq"]) for row in self._rows), default=0)

    def append(self, kind: str, subject: str, body: dict[str, Any]) -> int:
        with self._lock:
            self._head += 1
            self._rows.append(
                {
                    "seq": self._head,
                    "kind": kind,
                    "subject": subject,
                    "body": deepcopy(body),
                    "t_mono_ns": time.monotonic_ns(),
                }
            )
            return self._head

    def replay(self, from_seq: int = 1) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([row for row in self._rows if row["seq"] >= from_seq])
