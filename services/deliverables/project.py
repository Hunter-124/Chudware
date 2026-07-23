"""Project directory + path conventions for deliverables.

The artifact record (contract §3) stores ``path`` and ``spec_ref`` **relative to the project
dir** so the record is portable with the project. Everything a ``make_*`` writes lives under
``<project>/artifacts/``; previews live under ``<project>/artifacts/.previews/``; the artifact
registry is ``<project>/artifacts/registry.json``.

The project dir is ``$AICAD_PROJECT_DIR`` when set (tests and the host point it at the active
project), else ``paths.ROOT / "project"`` — the single-project fallback under the app's
``%APPDATA%/AI-CAD`` root. Resolving it lazily (not at import) lets a test set the env var
per-case.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # services.paths gives the app's %APPDATA%/AI-CAD root; degrade for a bare checkout.
    from services import paths as _paths

    _APP_ROOT = _paths.ROOT
except Exception:  # noqa: BLE001
    _APP_ROOT = Path(os.path.expanduser("~/.ai-cad"))

_ARTIFACTS = "artifacts"
_PREVIEWS = ".previews"
_REGISTRY = "registry.json"


def project_dir() -> Path:
    env = os.environ.get("AICAD_PROJECT_DIR")
    root = Path(env) if env else (_APP_ROOT / "project")
    root.mkdir(parents=True, exist_ok=True)
    return root


def artifacts_dir() -> Path:
    d = project_dir() / _ARTIFACTS
    d.mkdir(parents=True, exist_ok=True)
    return d


def previews_dir() -> Path:
    d = artifacts_dir() / _PREVIEWS
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path() -> Path:
    return artifacts_dir() / _REGISTRY


def to_project_rel(p: Path) -> str:
    """Path relative to the project dir, forward-slashed (the record's portable form)."""
    root = project_dir().resolve()
    ap = Path(p).resolve()
    try:
        return ap.relative_to(root).as_posix()
    except ValueError:
        return ap.as_posix()


def resolve_project_path(path: str) -> Path:
    """Resolve a caller-supplied path: absolute stays absolute; relative is under the project."""
    p = Path(path)
    if p.is_absolute():
        return p
    return (project_dir() / p).resolve()
