"""LibreDWG tool discovery (``dwg2dxf`` reads DWG 2D entities) and the pinned provisioning descriptor.

LibreDWG is GPL-3.0, so Chudware never links or bundles it (contracts/licenses.md). It runs
only as a subprocess, found on ``PATH`` first, then in the per-user managed install that
``scripts/provision_libredwg.py`` builds from the pinned upstream release on the user's own
machine. ``core/doc/dwg.cpp`` (``libredwg_tool``) resolves the same two locations in the
same order; keep them in lockstep.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

LIBREDWG_VERSION = "0.14"
LIBREDWG_REPOSITORY = "https://github.com/LibreDWG/libredwg"
LIBREDWG_SOURCE_URL = (
    "https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14.tar.xz")
LIBREDWG_SOURCE_SHA256 = "62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275"
LIBREDWG_WIN64_URL = (
    "https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip")
LIBREDWG_WIN64_SHA256 = "1ad7e15344d20b3426c3435b078d82fb84b35062815946b2cca9c5fc9810fea8"


def install_dir() -> Path:
    """Managed install root: ``<appdata>/Chudware/tools/libredwg`` (executables in ``bin/``)."""
    from services.paths import appdata_root
    return appdata_root() / "tools" / "libredwg"


def tool(name: str) -> str | None:
    """Absolute path of LibreDWG tool *name* (e.g. ``dwg2dxf``): PATH, then managed install."""
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    candidate = install_dir() / "bin" / exe
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None
