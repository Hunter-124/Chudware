"""bugreport_context.py — captured failure context + file writer for agent bug reports.

Settings → AI → Tools "Send bug reports" (``agent.bug_reports``, default on)
gives the agent the ``submit_bug_report`` MCP tool. When a tool call fails
during a turn, :func:`record_failure` captures the offence into a small ring
buffer; the next report the agent files embeds those failures automatically,
so the report says WHICH tool broke, with WHAT arguments, and the exact
error — the model only has to add a title and summary.

Reports are plain markdown under ``~/Documents/Chudware/Bug-Reports/``
(override: ``CHUDWARE_BUG_REPORTS_DIR``) — local files for the user to
triage; nothing leaves the machine.

This module lives under ``services.agent`` ONLY and is imported by both
``agent_turn`` (recorder) and ``services.mcp.bugreport_tools`` (tool wrapper)
under this exact dotted path, so there is exactly ONE ring buffer per
process — the dual-identity trap (``mcp_server.*`` vs ``services.mcp.*``)
that bit ``cad_tools._CUR`` cannot split the state.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MAX_FAILURES = 8        # ring depth — one turn's error burst, no more
_MAX_FIELD_CHARS = 4000  # per-field cap inside the report

_FAILURES: "deque[dict]" = deque(maxlen=_MAX_FAILURES)
_FILED: dict[str, str] = {}  # normalized title -> report path (per-process dedupe)
_LOCK = threading.Lock()


def bug_reports_dir() -> Path:
    """Directory reports land in (created on demand by :func:`file_report`)."""
    override = os.environ.get("CHUDWARE_BUG_REPORTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / "Chudware" / "Bug-Reports"


def _clip(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(
            value, indent=2, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"\n… [truncated {len(text) - limit} chars]"
    return text


def record_failure(tool: str, args: Any, result: Any, *,
                   run_id: str | None = None, actor: str | None = None) -> None:
    """Capture one failed tool call for the next report. Never raises."""
    try:
        error = ""
        if isinstance(result, dict):
            error = str(result.get("error") or result.get("reason") or "")
        if not error:
            error = _clip(result, 800)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": str(tool),
            "args": _clip(args),
            "error": _clip(error),
        }
        if run_id:
            entry["run_id"] = str(run_id)
        if actor:
            entry["actor"] = str(actor)
        with _LOCK:
            _FAILURES.append(entry)
    except Exception:  # noqa: BLE001 — the recorder never breaks a turn
        pass


def recent_failures() -> list[dict]:
    """Snapshot of the captured failures, oldest first."""
    with _LOCK:
        return list(_FAILURES)


def clear() -> None:
    """Drop ring + dedupe state (tests)."""
    with _LOCK:
        _FAILURES.clear()
        _FILED.clear()


def _slug(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "bug").lower()).strip("-")
    return (slug or "bug")[:limit].strip("-")


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())[:120]


def file_report(title: str, summary: str, *, context: str = "",
                severity: str = "bug") -> dict:
    """Write one markdown report; returns the tool envelope dict."""
    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title:
        return {"ok": False, "reason": "missing_title",
                "hint": "submit_bug_report needs a short title naming the "
                        "broken thing."}
    if not summary:
        return {"ok": False, "reason": "missing_summary",
                "hint": "submit_bug_report needs a summary: what you tried, "
                        "what happened (exact error), what you expected."}

    key = _norm_title(title)
    with _LOCK:
        dup = _FILED.get(key)
    if dup:
        return {"ok": True, "path": dup, "duplicate": True,
                "note": "An identical report was already filed this session; "
                        "no new file written. Keep working around the issue."}

    now = datetime.now(timezone.utc)
    out_dir = bug_reports_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "reason": f"cannot_create_dir: {exc}"}

    base = f"{now:%Y%m%d-%H%M%S}-{_slug(title)}"
    path = out_dir / f"{base}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{base}-{n}.md"
        n += 1

    failures = recent_failures()
    lines = [
        f"# Bug report: {title}",
        "",
        f"- **Filed:** {now.isoformat(timespec='seconds')}",
        f"- **Severity:** {severity or 'bug'}",
        f"- **Platform:** {platform.platform()}",
        f"- **Python:** {sys.version.split()[0]}",
        "",
        "## Summary",
        "",
        summary,
        "",
    ]
    if context.strip():
        lines += ["## Agent notes", "", context.strip(), ""]
    if failures:
        lines += ["## Recent tool failures (auto-captured)", ""]
        for i, f in enumerate(failures, 1):
            head = f"### {i}. `{f['tool']}` — {f['ts']}"
            meta = []
            if f.get("actor"):
                meta.append(f"actor={f['actor']}")
            if f.get("run_id"):
                meta.append(f"run={f['run_id']}")
            if meta:
                head += f" ({', '.join(meta)})"
            lines += [head, "",
                      "**Error**", "", "```", f["error"], "```", "",
                      "**Arguments**", "", "```json", f["args"], "```", ""]
    else:
        lines += ["## Recent tool failures", "",
                  "(none captured this session)", ""]
    lines += ["---",
              "Filed by the Chudware agent (submit_bug_report). "
              "Reports stay on this machine.",
              ""]

    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": f"write_failed: {exc}"}
    with _LOCK:
        _FILED[key] = str(path)
    return {"ok": True, "path": str(path),
            "failures_included": len(failures),
            "note": "Bug report filed. The user reviews these reports; keep "
                    "working around the issue where possible."}
