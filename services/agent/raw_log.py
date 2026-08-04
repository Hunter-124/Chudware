"""Best-effort append-only JSONL storage for lossless agent transcripts."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_SEQUENCE_CACHE: dict[str, tuple[int, int]] = {}


def append(
    path: Path | str,
    record: dict,
    header: dict | None = None,
) -> None:
    """Append one uncapped record, optionally creating a first-line header.

    Persistence is deliberately best-effort: serialization and filesystem
    failures never escape into the agent loop. Sequence numbers are recovered
    from complete lines on disk whenever the file changed outside this process,
    so they remain monotonic across restarts and torn trailing writes.
    """
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        file_size = target.stat().st_size if target.exists() else 0
        cache_key = str(target.resolve())
        cached = _SEQUENCE_CACHE.get(cache_key)
        if cached is not None and cached[0] == file_size:
            next_seq = cached[1]
        else:
            complete_lines = 0
            first_is_header = False
            if file_size:
                with open(target, "rb") as stream:
                    first = stream.readline()
                    if first.endswith(b"\n"):
                        complete_lines = 1
                        try:
                            first_item = json.loads(first.decode("utf-8"))
                            first_is_header = (
                                isinstance(first_item, dict)
                                and first_item.get("kind") == "header"
                            )
                        except Exception:  # noqa: BLE001 — corrupt first line
                            pass
                    while chunk := stream.read(1024 * 1024):
                        complete_lines += chunk.count(b"\n")
            # Header-backed logs use the historical line-count sequence.
            # Headerless logs need one extra because every complete line is a
            # data record rather than line zero's schema marker.
            next_seq = max(
                1, complete_lines + (0 if first_is_header else 1))

        needs_separator = False
        if file_size:
            with open(target, "rb") as stream:
                stream.seek(-1, os.SEEK_END)
                needs_separator = stream.read(1) != b"\n"

        lines: list[dict[str, Any]] = []
        if file_size == 0 and header is not None:
            lines.append(dict(header))
        payload = dict(record)
        try:
            payload["ts"] = float(payload.get("ts", time.time()))
        except (TypeError, ValueError):
            payload["ts"] = time.time()
        payload["seq"] = next_seq
        lines.append(payload)

        encoded = (
            (b"\n" if needs_separator else b"")
            + "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                for item in lines
            ).encode("utf-8")
        )
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("raw transcript append made no progress")
                remaining = remaining[written:]
        finally:
            os.close(fd)
        _SEQUENCE_CACHE[cache_key] = (file_size + len(encoded), next_seq + 1)
    except Exception:  # noqa: BLE001 — raw persistence must not break a turn
        return


def read(path: Path | str, limit: int = 0, offset: int = 0) -> list[dict]:
    """Read records in append order; tolerate a torn trailing JSON line."""
    try:
        target = Path(path).expanduser()
        if not target.exists():
            return []
        records: list[dict] = []
        with open(target, "rb") as stream:
            for raw_line in stream:
                try:
                    item = json.loads(raw_line.decode("utf-8").strip())
                except Exception:  # noqa: BLE001 — skip torn/corrupt lines
                    continue
                if isinstance(item, dict):
                    records.append(item)
        start = max(0, int(offset))
        count = int(limit)
        return records[start:] if count <= 0 else records[start:start + count]
    except Exception:  # noqa: BLE001 — archive reads are best-effort
        return []
