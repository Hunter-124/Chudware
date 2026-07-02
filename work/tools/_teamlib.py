#!/usr/bin/env python3
"""_teamlib.py — small shared helpers for team_mcp.py and dashboard.py.

Per-machine "whoami" identity storage and a cheap graphify staleness check.
Stdlib only. Not a CLI entry point — imported by team_mcp.py / dashboard.py,
both of which live in this same directory (work/tools/), so Python's default
"script dir on sys.path[0]" behavior makes `import _teamlib` work with no
extra path setup.
"""
import json
import subprocess
from pathlib import Path

WORK = Path(__file__).resolve().parent.parent  # work/
IDENTITY_PATH = WORK / "sync" / ".identity"     # gitignored (work/sync/)


def get_identity():
    if IDENTITY_PATH.exists():
        name = IDENTITY_PATH.read_text(encoding="utf-8").strip()
        return name or None
    return None


def set_identity(name):
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_PATH.write_text(name.strip() + "\n", encoding="utf-8")


def graphify_freshness(repo):
    """Return a one-line staleness note if any graphify-indexed file changed since
    the last build, else "". Cheap: mtime comparison against graphify-out/manifest.json
    (no hashing, no rebuild) — files graphify doesn't index are silently skipped."""
    manifest_path = repo / "graphify-out" / "manifest.json"
    if not manifest_path.exists():
        return ""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    try:
        r = subprocess.run(["git", "ls-files"], cwd=str(repo),
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    if r.returncode != 0:
        return ""
    changed = 0
    for rel in r.stdout.splitlines():
        entry = manifest.get(rel)
        if not isinstance(entry, dict):
            continue
        try:
            mtime = (repo / rel).stat().st_mtime
        except OSError:
            continue
        if mtime > entry.get("mtime", 0) + 1:
            changed += 1
    if changed:
        return (f"[note: {changed} graphify-indexed file(s) changed since the last "
                f"build — this answer may be stale; run `graphify update .`]\n")
    return ""
