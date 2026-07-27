"""flywheel/dvc_init.py — DVC repository initialisation + round-trip helpers (T-H5).

Initialises a DVC-tracked training-data store under `training/` (adjacent to this
repo or at an explicit path), wires a file:// remote for local testing, and provides
push/pull/status helpers for the CI round-trip test.

DVC remote hierarchy:
  local testing : file:///abs/path/to/dvc-remote   (no credentials needed)
  production    : s3://bucket/chudware-training-data   (HUMAN-GATED — see work/questions/T-H5.md)

DVC license: Apache-2.0. In-process use permitted (see contracts/licenses.md row added
by T-H5 commit).

Usage (programmatic):
    from tools.flywheel.dvc_init import init_training_repo, dvc_push, dvc_pull

    repo_path = init_training_repo(Path("training"), remote_url="file:///tmp/dvc-remote")
    dvc_push(repo_path)
    dvc_pull(repo_path)  # from a "second machine" clone

Usage (CLI):
    python -m tools.flywheel.dvc_init --repo training --remote file:///tmp/dvc-remote init
    python -m tools.flywheel.dvc_init --repo training push corpus/IP-13/v1
    python -m tools.flywheel.dvc_init --repo training pull corpus/IP-13/v1
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command. Raises CalledProcessError if check=True and rc != 0."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _dvc_cmd() -> list[str]:
    """Return the DVC invocation prefix.

    Prefers the ``dvc`` binary on PATH (works even when DVC is not installed in
    the active venv, e.g. the mcp-venv).  Falls back to ``sys.executable -m dvc``
    so the function degrades gracefully when only the Python package is present.
    """
    if shutil.which("dvc"):
        return ["dvc"]
    return [sys.executable, "-m", "dvc"]


def _dvc(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Invoke DVC CLI via subprocess (GPL-clean subprocess isolation)."""
    return _run([*_dvc_cmd(), *args], cwd=cwd, check=check)


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, check=check)


# ---------------------------------------------------------------------------
# init_training_repo
# ---------------------------------------------------------------------------

def init_training_repo(
    repo_path: Path,
    remote_url: str | None = None,
    remote_name: str = "origin",
) -> Path:
    """Initialise (or re-use) a DVC-tracked training-data repository.

    Creates the directory if it doesn't exist, runs `git init` + `dvc init`,
    and wires the supplied remote.

    Args:
        repo_path: Path to the training-data repo root (e.g. Path("training")).
                   Created if absent.
        remote_url: DVC remote URL.  Use a plain absolute path (e.g.
                    "/data/dvc-remote") for local/NAS storage — this is the
                    shipped default.  For cloud swap, supply an s3:// URL.
                    NOTE: do NOT use file:// URIs; DVC on Linux resolves them
                    as relative paths.  Plain absolute paths are correct.
                    If None, no remote is configured (useful for pure-local tests).
        remote_name: DVC remote name (default: "origin").

    Returns:
        Absolute resolved path to the repo root.
    """
    repo_path = repo_path.resolve()
    repo_path.mkdir(parents=True, exist_ok=True)

    # Git init (idempotent)
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        _git("init", cwd=repo_path)
        # Set a baseline identity so DVC commits work in CI
        _git("config", "user.email", "flywheel@chudware.local", cwd=repo_path)
        _git("config", "user.name", "Flywheel", cwd=repo_path)

    # DVC init (idempotent)
    dvc_dir = repo_path / ".dvc"
    if not dvc_dir.exists():
        _dvc("init", cwd=repo_path)
        _git("add", ".dvc", cwd=repo_path)
        _git("commit", "-m", "chore: dvc init", cwd=repo_path)

    # Wire remote
    if remote_url:
        # Check if remote already configured
        result = _dvc("remote", "list", cwd=repo_path, check=False)
        if remote_name not in (result.stdout or ""):
            _dvc("remote", "add", "-d", remote_name, remote_url, cwd=repo_path)
        else:
            _dvc("remote", "modify", remote_name, "url", remote_url, cwd=repo_path)
        _git("add", ".dvc/config", cwd=repo_path, check=False)

    return repo_path


# ---------------------------------------------------------------------------
# add_to_dvc
# ---------------------------------------------------------------------------

def add_to_dvc(repo_path: Path, data_path: Path) -> str:
    """Add a file or directory to DVC tracking and return its .dvc file content.

    Args:
        repo_path: DVC repo root.
        data_path: Path to add (must be inside repo_path or a recognised relative path).

    Returns:
        Content of the generated .dvc file (for round-trip verification).
    """
    # data_path relative to repo
    rel = data_path.relative_to(repo_path) if data_path.is_absolute() else data_path
    _dvc("add", str(rel), cwd=repo_path)
    dvc_file = repo_path / (str(rel) + ".dvc")
    if dvc_file.exists():
        return dvc_file.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# dvc_push / dvc_pull / dvc_status
# ---------------------------------------------------------------------------

def dvc_push(repo_path: Path, target: str | None = None) -> str:
    """Push DVC-tracked files to the configured remote.

    Args:
        repo_path: DVC repo root.
        target: Specific .dvc file or directory to push; None = push all.

    Returns:
        DVC stdout.
    """
    args = ["push"]
    if target:
        args.append(target)
    result = _dvc(*args, cwd=repo_path)
    return result.stdout


def dvc_pull(repo_path: Path, target: str | None = None) -> str:
    """Pull DVC-tracked files from the configured remote.

    Args:
        repo_path: DVC repo root.
        target: Specific .dvc file or directory to pull; None = pull all.

    Returns:
        DVC stdout.
    """
    args = ["pull"]
    if target:
        args.append(target)
    result = _dvc(*args, cwd=repo_path)
    return result.stdout


def dvc_status(repo_path: Path) -> str:
    """Return DVC status output."""
    result = _dvc("status", cwd=repo_path, check=False)
    return result.stdout


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Flywheel DVC repo init + round-trip helper (T-H5)"
    )
    parser.add_argument("--repo", type=Path, default=Path("training"),
                        help="Training-data repo root (default: training/)")
    parser.add_argument("--remote", type=str, default=None,
                        help="DVC remote URL (file:// or s3://)")
    parser.add_argument("--remote-name", default="origin",
                        help="DVC remote name (default: origin)")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    subparsers.add_parser("init", help="Initialise the DVC repo")

    push_p = subparsers.add_parser("push", help="Push to DVC remote")
    push_p.add_argument("target", nargs="?", default=None)

    pull_p = subparsers.add_parser("pull", help="Pull from DVC remote")
    pull_p.add_argument("target", nargs="?", default=None)

    subparsers.add_parser("status", help="Show DVC status")

    args = parser.parse_args()

    if args.cmd == "init":
        repo = init_training_repo(args.repo, args.remote, args.remote_name)
        print(f"[dvc_init] Repo ready at {repo}")
    elif args.cmd == "push":
        out = dvc_push(args.repo, args.target)
        print(out)
    elif args.cmd == "pull":
        out = dvc_pull(args.repo, args.target)
        print(out)
    elif args.cmd == "status":
        out = dvc_status(args.repo)
        print(out)


if __name__ == "__main__":
    main()
