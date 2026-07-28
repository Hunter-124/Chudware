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
import tempfile
import time
from pathlib import Path

_CAD_EXTS = {
    ".step+", ".step", ".stp", ".stl", ".3mf", ".iges", ".igs",
    ".brep", ".dxf", ".dwg", ".sldprt", ".sldasm",
}
_SKIP_DIRS = {".plm", ".chudware"}
_MAX_CHARS = 1500
_MAX_DEPTH = 2

# Directories that mark a folder as a Chudware/PLM project root. A folder the user
# opened as a PLM project once carries these forever, so NESTED marker dirs are
# normal: /Documents/489-final and /Documents/489-final/CAD & Design Geometry both
# hold a .plm. Picking the NEAREST one is what jailed a live run inside the CAD
# subfolder and refused a run_shell in the sibling CDR/ folder
# ("outside_workspace_root"). The user's project is the OUTERMOST marked folder, so
# resolve_project_root climbs to the topmost one instead of stopping at the first.
_PROJECT_MARKERS = (".plm", ".aicadvault", ".chudware")

# Never treat these as a project root even if something dropped a marker there —
# jailing an agent to $HOME or ~/Documents is the same as no jail at all.
_ROOT_STOP_NAMES = {"Documents", "Desktop", "Downloads", "Music", "Pictures", "Videos"}


def _never_project_root(node: Path, home: Path | None) -> bool:
    """True for directories that must never become the agent's jail root.

    The climb keeps the OUTERMOST marked ancestor, which is right for real nested
    project folders but catastrophic if a marker ever lands somewhere system-wide:
    one stray ``.aicadvault`` in ``/tmp`` resolved EVERY temp path to ``/tmp``, i.e.
    a jail covering every other process's scratch space. The same would apply to
    ``$HOME`` or ``/``. Stop the climb before those rather than trusting the marker.
    """
    if node.parent == node:          # filesystem root
        return True
    if home is not None and node == home:
        return True
    if node.name in _ROOT_STOP_NAMES:
        return True
    try:
        # The system temp dir is shared scratch space, never one user's project.
        # Compare through realpath so /tmp vs a symlinked /private/tmp both match.
        if Path(os.path.realpath(str(node))) == Path(os.path.realpath(tempfile.gettempdir())):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _is_project_dir(d: Path) -> bool:
    return any((d / m).is_dir() for m in _PROJECT_MARKERS)


def resolve_project_root(start: str | Path) -> Path:
    """The user's project root for *start* — the OUTERMOST ancestor carrying a project
    marker, else *start* itself.

    Climbs from *start* to (but never past) $HOME, remembering the highest marked
    folder seen. Returns an absolute path; never raises. Callers pass the host's
    save_dir, which is the open DOCUMENT's folder — often a subfolder of the real
    project — so this is what turns "the folder this part happens to sit in" into
    "the project the user is working on".
    """
    try:
        cur = Path(os.path.abspath(os.path.expanduser(str(start))))
    except Exception:  # noqa: BLE001
        return Path(str(start))
    if not cur.is_dir():
        cur = cur.parent
    try:
        home = Path.home().resolve()
    except Exception:  # noqa: BLE001
        home = None
    best = cur
    node = cur
    # Bounded climb: filesystem depth is finite, but guard anyway.
    for _ in range(64):
        if _never_project_root(node, home):
            break
        try:
            if _is_project_dir(node):
                best = node  # keep climbing — an outer marker outranks this one
        except OSError:
            pass
        node = node.parent
    return best


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
