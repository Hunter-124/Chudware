"""Canonical filesystem locations for ShopCraft (formerly AI-CAD).

Everything the app produces or installs lives under ``%APPDATA%\\ShopCraft`` so the
shipped (non-sandboxed) binary and this dev environment converge on the same layout.

Backward compatibility (T-G10, alias/read-both — no destructive migration): machines
with an existing ``AI-CAD`` directory keep using it wholesale (see :func:`active_root`);
fresh installs get ``ShopCraft``. The old directory is never moved or deleted
automatically — it may hold the active mcp-venv, config and chat history, and venvs do
not survive relocation. Call :func:`data_roots` for both candidate paths when scanning.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

APP_NAME = "ShopCraft"
APP_NAME_LEGACY = "AI-CAD"


def _base_dir() -> Path:
    """XDG base data dir (or Windows APPDATA). Explicit env var wins for test isolation."""
    base = os.environ.get("APPDATA")
    if not base:
        if os.name == "nt":
            base = os.path.expanduser(r"~\AppData\Roaming")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base)


def appdata_root() -> Path:
    """Per-user data root for ShopCraft: ``%APPDATA%\\ShopCraft`` / ``~/.local/share/ShopCraft``.

    Matching the C++ convention (core/agent/paths.cpp). An explicit APPDATA env var
    always wins (tests use it to redirect).

    New data is written here. For reading existing data, use :func:`data_roots` so the
    legacy AI-CAD directory is also checked.
    """
    return _base_dir() / APP_NAME


def appdata_root_legacy() -> Path:
    """The old per-user data root (``AI-CAD`` directory). Read-only fallback.

    The legacy directory is never moved or deleted automatically — it holds the active
    mcp-venv, existing config, and chat history. Installers that want to migrate data
    should do so explicitly and recreate the venv at the new location.
    """
    return _base_dir() / APP_NAME_LEGACY


def data_roots() -> list[Path]:
    """All candidate data root directories, new first, legacy second.

    Use when scanning for existing user data (config, models, parts library) so both
    the new ShopCraft directory and the old AI-CAD directory are checked.
    """
    return [appdata_root(), appdata_root_legacy()]


def active_root() -> Path:
    """The data root actually in use for this process.

    Machines with an existing legacy ``AI-CAD`` directory (and no ``ShopCraft``
    directory yet) keep using the legacy directory for BOTH reads and writes — this
    avoids a split-brain where reads come from the old dir but new writes land in the
    new one, and keeps the live mcp-venv, config and caches working untouched.
    Fresh installs (no legacy dir) get the new ``ShopCraft`` directory.
    """
    new = appdata_root()
    legacy = appdata_root_legacy()
    if not new.exists() and legacy.exists():
        return legacy
    return new


ROOT = active_root()
RUNTIME = ROOT / "runtime"          # isolated python + wheels
CACHE = ROOT / "cache"              # per-model outputs (stl/step/png)
CONFIG = ROOT / "config.json"       # provider keys + settings
LLAMACPP = ROOT / "llamacpp"        # optional managed llama.cpp server payload
MODELS = ROOT / "models"            # user / bundled GGUF models
VENDOR_PARTS = ROOT / "vendor_parts"  # approved/manual supplier CAD + metadata cache
PARTS_LIBRARY = ROOT / "parts_library"  # watched folder of user-dropped CAD files (auto-registered)


def parts_library_dir() -> Path:
    """Return the watched parts-library folder, creating it on first access."""
    PARTS_LIBRARY.mkdir(parents=True, exist_ok=True)
    return PARTS_LIBRARY


def model_dir(name: str) -> Path:
    """Return (and create) the per-model output directory under :data:`CACHE`.

    Sanitizes ``name`` so a hostile ``run_model`` / worker caller cannot escape the cache
    root via ``..`` path segments.
    """
    safe = re.sub(r"[^\w.-]+", "_", str(name)).strip("._") or "model"
    d = (CACHE / safe).resolve()
    root = CACHE.resolve()
    if d != root and root not in d.parents:
        safe = "model"
        d = root / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_config() -> Path:
    """Return the config.json path, preferring the new ShopCraft location but
    falling back to the legacy AI-CAD location if it exists there and not here."""
    new_cfg = ROOT / "config.json"
    if new_cfg.exists():
        return new_cfg
    legacy_cfg = appdata_root_legacy() / "config.json"
    if legacy_cfg.exists():
        return legacy_cfg
    return new_cfg  # canonical write target even if it doesn't exist yet
