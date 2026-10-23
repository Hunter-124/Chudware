"""Worker-RPC bridge for the deliverables preview seam.

Exposes ``render_document`` + ``list_artifacts`` as **engine-worker methods** so the C++/ImGui GUI
reaches them through the same newline-delimited-JSON worker seam (``WorkerClient`` / ``AsyncRpc``,
``services/worker/``) it already uses for every other service — no bespoke transport, no format
code in C++. These are thin wrappers over :mod:`deliverables.api`; the heavy format→PNG work
(LibreOffice→PDF→raster for office; native pdf/svg/png/csv/md previews, all capped at ≤1024 px /
<1 MB) stays entirely inside the service per contract ``deliverables/1`` §5.4. The GUI displays
what the service produced and never re-renders office formats itself.

Each method returns the ``api`` envelope **unchanged** (contract §5.4 / §5.5 shape), plus one
additive convenience field — an absolute ``project_dir`` — so the GUI can resolve the record's
project-relative preview PNG paths to on-disk files without having to know the project layout.
Registered append-only in ``engine/worker.py`` (mirrors the ECAD ``WORKER_METHODS`` block).
"""
from __future__ import annotations

from typing import Callable

from . import api, project


def _project_dir() -> str:
    return str(project.project_dir().resolve())


def _render_document(params: dict, _emit: Callable) -> dict:
    """Worker method ``render_document`` → contract §5.4 preview envelope + absolute ``project_dir``."""
    env = api.render_document((params or {}).get("path"))
    env.setdefault("project_dir", _project_dir())
    return env


def _list_artifacts(params: dict, _emit: Callable) -> dict:
    """Worker method ``list_artifacts`` → contract §5.5 registry envelope + absolute ``project_dir``."""
    env = api.list_artifacts((params or {}).get("run_id"))
    env.setdefault("project_dir", _project_dir())
    return env


# Registered by engine/worker.py via ``_METHODS.update(worker_rpc.WORKER_METHODS)``.
WORKER_METHODS: dict[str, Callable] = {
    "render_document": _render_document,
    "list_artifacts": _list_artifacts,
}
