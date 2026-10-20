"""Disposable stdlib regex evaluator; receives admitted bytes only, never opens paths."""
from __future__ import annotations

import io
import json
import re
import sys

HEADER_BYTES = 32 * 1024


def _header(stream) -> dict:
    line = stream.readline(HEADER_BYTES + 1)
    if not line or len(line) > HEADER_BYTES or not line.endswith(b"\n"):
        raise ValueError("missing or oversized search frame")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("invalid search frame")
    return value


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(min(64 * 1024, size - len(data)))
        if not chunk:
            raise ValueError("incomplete scoped file data")
        data.extend(chunk)
    return bytes(data)


def search(stream) -> dict:
    config = _header(stream)
    pattern = config["pattern"]
    try:
        rx = re.compile(pattern, re.I if config["ignore_case"] else 0)
    except (re.error, OverflowError, RecursionError) as exc:
        return {"ok": False, "reason": "bad_pattern", "hint": str(exc)[:2000]}
    cap = min(max(int(config["cap"]), 1), 500)
    output_cap = min(int(config["output_cap"]), HEADER_BYTES)
    file_cap = min(int(config["file_cap"]), 8 * 1024 * 1024)
    columns = min(int(config["line_columns"]), 2000)
    matches = []
    scanned = 0
    used = len(json.dumps(pattern, ensure_ascii=False).encode("utf-8")) + 256

    def result(truncated=False):
        return {"ok": True, "pattern": pattern, "matches": matches, "count": len(matches),
                "files_scanned": scanned, "truncated": truncated}

    while True:
        frame = _header(stream)
        if "error" in frame:
            return frame["error"]
        if frame.get("end"):
            return result()
        scanned += 1
        if frame.get("skip"):
            continue
        size = frame["bytes"]
        if not isinstance(size, int) or not 0 <= size <= file_cap:
            raise ValueError("scoped file data exceeds its budget")
        data = _read_exact(stream, size)
        # Iteration is lazy even for millions of short lines. No split/list of lines.
        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace", newline=None) as text:
            for number, line in enumerate(text, 1):
                if rx.search(line):
                    body = line.rstrip("\n")
                    if len(body) > columns:
                        body = body[:columns] + f"… [+{len(body) - columns} chars]"
                    match = {"file": frame["file"], "line": number, "text": body}
                    cost = len(json.dumps(match, ensure_ascii=False).encode("utf-8")) + 2
                    if used + cost > output_cap:
                        return {"ok": False, "reason": "search_output_budget_exceeded",
                                "hint": "Matches exceed 32 KiB; lower max_matches or narrow path/pattern."}
                    matches.append(match)
                    used += cost
                    if len(matches) >= cap:
                        return result(truncated=True)


def main() -> int:
    try:
        result = search(sys.stdin.buffer)
    except (KeyError, TypeError, ValueError, OSError):
        result = {"ok": False, "reason": "search_worker_failed", "hint": "Invalid scoped search input."}
    data = json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > HEADER_BYTES:
        data = b'{"ok":false,"reason":"search_output_budget_exceeded","hint":"Search response budget exceeded."}\n'
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
