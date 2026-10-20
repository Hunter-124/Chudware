"""Project directory + path conventions for deliverables.

Two roots, deliberately separate (2026-07-27):

* ``project_dir()`` — the USER's resolved project folder
  (``$CHUDWARE_PROJECT_DIR``). This is their Documents tree. Only things the
  user asked for and would recognise as their own work belong here.
* ``artifacts_dir()`` — a HIDDEN per-project store under the app's data root.
  Everything a ``make_*`` generates — rendered pages, previews, the artifact
  registry, extraction by-products — lands here instead.

They used to be the same directory, so ``<project>/artifacts/`` accumulated every
intermediate the model produced right beside the user's files. Nothing is dropped:
the artifact registry still records all of it and reads back through
``resolve_project_path``; it is simply out of sight. ``CHUDWARE_ARTIFACTS_DIR``
overrides the hidden location outright (tests, and anyone who genuinely wants the
tree inside the project).

Resolving both lazily (not at import) lets a test set the env vars per-case.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from services.agent.request_context import actor_state_root, in_cell, root_value

from .errors import DeliverableError

try:  # services.paths gives the app's %APPDATA%/Chudware root; degrade for a bare checkout.
    from services import paths as _paths

    _APP_ROOT = _paths.ROOT
except Exception:  # noqa: BLE001
    _APP_ROOT = Path(os.path.expanduser("~/.chudware"))

_ARTIFACTS = "artifacts"
_PREVIEWS = ".previews"
_REGISTRY = "registry.json"


def project_dir() -> Path:
    env = root_value("CHUDWARE_PROJECT_DIR")
    if not env or not env.strip():
        raise DeliverableError(
            "project_root_unavailable",
            "CHUDWARE_PROJECT_DIR is not set; attach or open a project first",
        )
    root = Path(env).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_slug(root: Path | None = None) -> str:
    """Stable, collision-free folder name for the active project.

    Basename keeps the directory browsable by a human; the path digest keeps two
    same-named projects apart.
    """
    root = root or project_dir()
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8", "replace")).hexdigest()[:10]
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in root.name) or "project"
    return f"{safe}-{digest}"


def artifacts_dir() -> Path:
    """Hidden per-project artifact store — NOT inside the user's project folder."""
    root = project_dir()
    if in_cell():
        digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
        d = actor_state_root() / "deliverables" / digest
        d.mkdir(parents=True, exist_ok=True)
        return d
    env = os.environ.get("CHUDWARE_ARTIFACTS_DIR")
    d = Path(env) if env else (_APP_ROOT / "projects" / _project_slug(root) / _ARTIFACTS)
    d.mkdir(parents=True, exist_ok=True)
    return d


def previews_dir() -> Path:
    d = artifacts_dir() / _PREVIEWS
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path() -> Path:
    return artifacts_dir() / _REGISTRY


def to_project_rel(p: Path) -> str:
    """Portable form of *p* for an artifact record.

    Relative to whichever root contains it — the hidden artifact store or the
    user's project — else absolute. ``resolve_project_path`` inverts this.
    """
    ap = Path(p).resolve()
    for base in (artifacts_dir().resolve(), project_dir().resolve()):
        try:
            return ap.relative_to(base).as_posix()
        except ValueError:
            continue
    return ap.as_posix()



def resolve_project_path(path: str) -> Path:
    """Inverse of :func:`to_project_rel`: absolute stays absolute; a relative path
    is looked for in the hidden artifact store first (that is where records point),
    then under the user's project."""
    p = Path(path)
    if in_cell():
        base = artifacts_dir().resolve()
        if not p.is_absolute():
            artifact = (base / p).resolve()
            if artifact != base and base not in artifact.parents:
                raise DeliverableError("not_found", "not_found")
            if artifact.exists():
                return artifact
            p = project_dir() / p
        resolved = p.resolve()
        if not (resolved == base or base in resolved.parents
                or resolved == Path("/work") or Path("/work") in resolved.parents):
            raise DeliverableError("not_found", "not_found")
        return resolved
    if p.is_absolute():
        return p
    candidate = (artifacts_dir() / p).resolve()
    if candidate.exists():
        return candidate
    return (project_dir() / p).resolve()
