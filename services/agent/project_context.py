"""project_context.py — user project-folder awareness block (2026-07-27).

The general lane runs tools with no idea what already lives in the user's
project folder: it re-created parts that were already on disk and its saves
never surfaced next to the user's files. This block lists the CAD-relevant
files in the project root (newest first) and states the ground rules, so the
model opens existing parts instead of duplicating them.

``project_context_block`` NEVER raises and returns "" when no root is known —
headless tests and lanes without a workspace are unaffected.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_CAD_EXTS = {
    ".step+", ".step", ".stp", ".stl", ".3mf", ".iges", ".igs",
    ".brep", ".dxf", ".dwg", ".sldprt", ".sldasm",
}
_SKIP_DIRS = {".plm", ".chudware"}
_MAX_CHARS = 1500
_MAX_DEPTH = 2


def _age(mtime: float, now: float) -> str:
    secs = max(0.0, now - mtime)
    if secs < 3600:
        mins = int(secs // 60)
        return f"{mins}m ago" if mins else "just now"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _collect(root: Path, max_files: int) -> tuple[list[tuple[str, int, float]], int]:
    """(relpath, size_bytes, mtime) rows newest-first + drop count."""
    found: list[tuple[str, int, float]] = []
    # (dir, depth); dirs at depth 0 are the root's immediate children.
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            if depth == 0:
                raise  # unreadable root: caller returns ""
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < _MAX_DEPTH and not e.name.startswith(".") \
                            and e.name not in _SKIP_DIRS:
                        stack.append((Path(e.path), depth + 1))
                    continue
                name = e.name.lower()
                if any(name.endswith(ext) for ext in _CAD_EXTS):
                    st = e.stat(follow_symlinks=False)
                    found.append((os.path.relpath(e.path, root), int(st.st_size), st.st_mtime))
            except OSError:
                continue
    found.sort(key=lambda r: r[2], reverse=True)
    dropped = max(0, len(found) - max_files)
    return found[:max_files], dropped


def project_context_block(root: str | Path | None, *, max_files: int = 40) -> str:
    """System-prompt block listing the project folder's CAD files; "" when unknown."""
    try:
        if not root:
            return ""
        rp = Path(root)
        if not rp.is_dir():
            return ""
        now = time.time()
        rows, dropped = _collect(rp, max(1, int(max_files)))
        by_dir: dict[str, list[tuple[str, int, float]]] = {}
        for rel, size, mtime in rows:
            parent, _, name = rel.rpartition("/")
            by_dir.setdefault(parent, []).append((name, size, mtime))
        # Wording matters more than the list. A bare prohibition ("do NOT create
        # a second part") deadlocked kimi k3 live on 2026-07-27: a matching file
        # existed, so it spent completions deliberating "import or build?" and
        # never acted. State BOTH branches and demand a decision in the same
        # reply — the model needs an action for whichever branch it picks.
        head = [
            "[PROJECT FILES]",
            f"Project folder (absolute): {rp}",
            "The files below already exist on disk. Decide from this list and act",
            "in the SAME reply - never stop to deliberate or ask which to do:",
            "- request clearly refers to one of these -> import_part(path=...) and",
            "  work on THAT file; do not model it again from scratch.",
            "- request does not match any of them -> create the new part as usual.",
            "  That is expected; it is saved into this folder automatically.",
        ]
        body: list[str] = []
        for parent, entries in by_dir.items():
            body.append(f"{parent or '.'}/  ({len(entries)})")
            for name, size, mtime in entries:
                kb = max(1, size // 1024) if size else 0
                body.append(f"  {name} ({kb} KB, {_age(mtime, now)})")

        # Char cap trims from the tail — but the count must stay TRUTHFUL, or the
        # model reasons about a project it cannot see (31 real parts rendered as
        # 13 with no note, 2026-07-27). Drop rows, then state what was dropped.
        def _assemble(rows_body: list[str], hidden: int) -> str:
            tail = ([f"... {hidden} more file(s) not listed — call fs_list(path) "
                     f"to see every file before creating anything."] if hidden else [])
            return "\n".join(head + rows_body + tail)

        hidden = dropped
        block = _assemble(body, hidden)
        while len(block) > _MAX_CHARS and body:
            last = body.pop()
            if not last.startswith("  "):
                continue  # a folder header carries no file count of its own
            hidden += 1
            block = _assemble(body, hidden)
        if not rows and not dropped:
            block = "\n".join(head + ["(no CAD files found yet)"])
        return block
    except Exception:  # noqa: BLE001 — context is best-effort, never breaks a turn
        return ""
