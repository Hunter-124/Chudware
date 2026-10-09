"""Provenance for trial manifests: repo revision + dirty fingerprint, binary identities,
and task/scorer content hashes (hashes of content, never hand-maintained labels)."""
from __future__ import annotations

import hashlib
import inspect
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmarks.artifact_validity.tasks.schema import Task, check_ids

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

#: Shared grading code every task's verdict depends on (beyond its own check modules).
SHARED_GRADER_MODULES = ("tasks/schema.py", "evidence.py", "scoring.py",
                         "geometry_checks.py", "oracles.py")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path | str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def repo_state() -> dict:
    """HEAD plus a fingerprint of every uncommitted change (tracked diff + status list)."""
    head = (_git("rev-parse", "HEAD") or "").strip() or None
    status = _git("status", "--porcelain=v1") or ""
    diff = _git("diff", "HEAD", "--binary") or ""
    changed = [ln for ln in status.splitlines() if ln.strip()]
    return {
        "head": head,
        "dirty": bool(changed),
        "dirty_files": len(changed),
        "dirty_fingerprint": sha256_bytes((status + "\0" + diff).encode()) if changed else None,
    }


def binary_identity(path: Path | str | None) -> dict:
    if not path:
        return {"path": None, "sha256": None, "mtime": None}
    p = Path(path)
    return {"path": str(p), "sha256": sha256_path(p),
            "mtime": p.stat().st_mtime if p.is_file() else None}


def stale_core_sources(binary: Path | str | None, limit: int = 20) -> list[str] | None:
    """Core sources (excluding core/tests) newer than ``binary``; None when unverifiable."""
    if not binary or not Path(binary).is_file():
        return None
    ref = Path(binary).stat().st_mtime
    newer: list[str] = []
    for s in (REPO / "core").rglob("*"):
        if s.suffix not in (".cpp", ".h") or not s.is_file():
            continue
        rel = s.relative_to(REPO).as_posix()
        if rel.startswith("core/tests/") or "/build" in rel:
            continue
        if s.stat().st_mtime > ref:
            newer.append(rel)
            if len(newer) >= limit:
                break
    return sorted(newer)


def runtime_identity() -> dict:
    return {"python": sys.version.split()[0], "platform": platform.platform()}


def _fixture_hashes(task: Task) -> dict[str, str | None]:
    return {name: sha256_path(FIXTURES / name) for name in task.fixtures}


def task_hash(task: Task) -> str:
    """Hash of everything the MODEL sees or is budgeted with: prompt, fixtures, deliverable."""
    body = {"id": task.id, "version": task.version, "domain": task.domain,
            "prompt": task.prompt, "final_artifact": task.final_artifact,
            "fixtures": _fixture_hashes(task), "budget_s": task.budget_s}
    return sha256_bytes(json.dumps(body, sort_keys=True).encode())


def _check_source_files(task: Task) -> set[Path]:
    files: set[Path] = set()
    for fn in task.checks:
        target = inspect.unwrap(fn)
        try:
            src = inspect.getsourcefile(target)
        except TypeError:
            src = None
        if src:
            files.add(Path(src).resolve())
        mod = sys.modules.get(getattr(target, "__module__", "") or "")
        mod_file = getattr(mod, "__file__", None)
        if mod_file:
            files.add(Path(mod_file).resolve())
    return files


def scorer_hash(task: Task) -> dict:
    """Hash of the grading code that decides this task: the modules defining its checks plus
    the shared grader modules, and the declared mandatory check ids."""
    files = _check_source_files(task) | {
        (HERE / rel).resolve() for rel in SHARED_GRADER_MODULES if (HERE / rel).is_file()}
    per_file = {p.relative_to(REPO).as_posix(): sha256_path(p) for p in sorted(files)}
    digest = sha256_bytes(json.dumps({"files": per_file, "checks": check_ids(task)},
                                     sort_keys=True).encode())
    return {"sha256": digest, "files": per_file, "check_ids": check_ids(task)}


def env_snapshot(env: dict[str, str]) -> dict[str, Any]:
    """Engine/runtime-relevant env actually handed to the executor (no secrets)."""
    keep = ("CHUDWARE_INSTANCE_KEY", "CHUDWARE_HOSTD_BIN", "CHUDWARE_HOSTD_LINGER_SECS",
            "CHUDWARE_LIBCHUDWARE", "CHUDWARE_DIST", "CHUDWARE_AUTOSAVE_DIR")
    return {k: env.get(k) for k in keep}
