"""Crash-durable publish for campaign artifacts.

`os.replace` alone is atomic but not durable: after a hard reset (this box's GPU can take the
whole machine down) a freshly renamed file came back zero-length because its data never left
the page cache (3,355 dense masks on 2026-09-29). Every resumable stage treats "file exists"
as "work done", so those empty files poison the next run. Flushing the data before the rename
means a crash leaves either the complete file or no file (which the stage simply redoes).
"""
from __future__ import annotations

import json
import os


def durable_replace(tmp, dest) -> None:
    """fsync `tmp`'s data, then atomically rename it over `dest`."""
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)


def nonempty(path) -> bool:
    """True when `path` exists with content (a crash-truncated artifact counts as missing)."""
    try:
        return os.stat(path).st_size > 0
    except OSError:
        return False


def read_jsonl(path) -> list:
    """Rows of a JSONL file ([] when absent). A hard reset mid-append leaves a torn or
    NUL-filled row; it is skipped (the work it recorded is simply redone on resume)."""
    if not os.path.exists(path):
        return []
    rows, torn = [], 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip().strip("\x00")
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                torn += 1
    if torn:
        print(f"[durable] {os.path.basename(path)}: skipped {torn} torn row(s) "
              f"from an interrupted write")
    return rows


def append_jsonl(path, rows) -> int:
    """Append rows durably; never glues a row onto a torn (newline-less) tail. Returns the
    number of rows written."""
    lines = [json.dumps(r, sort_keys=True) + "\n" for r in rows]
    if not lines:
        return 0
    data = "".join(lines).encode("utf-8")
    with open(path, "a+b") as fh:
        if fh.tell() and (fh.seek(-1, os.SEEK_END), fh.read(1))[1] != b"\n":
            data = b"\n" + data
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return len(lines)
