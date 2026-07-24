"""Canonical filesystem locations for Chudware (formerly Billet / AI-CAD).

Everything the app produces or installs lives under ``%APPDATA%\\Chudware`` so the
shipped (non-sandboxed) binary and this dev environment converge on the same layout.

Backward compatibility (T-G10, alias/read-both — no destructive migration): machines
with an existing ``Chudware`` directory keep using it wholesale (see :func:`active_root`);
fresh installs get ``Chudware``. The old directory is never moved or deleted
automatically — it may hold the active mcp-venv, config and chat history, and venvs do
not survive relocation. Call :func:`data_roots` for both candidate paths when scanning.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

APP_NAME = "Chudware"
# Legacy data-dir names, newest first. Kept for read-both back-compat (no migration).
APP_NAMES_LEGACY = ["Billet", "AI-CAD"]


def _apply_legacy_env_compat() -> None:
    """Map legacy AI_CAD_/AICAD_/BILLET_/SHOPCRAFT_ env vars onto their CHUDWARE_
    equivalents when the new name is unset — keeps pre-rename scripts/environments
    working after the Chudware rename. Runs once at import."""
    prefixes = ("AI_CAD_", "AICAD_", "BILLET_", "SHOPCRAFT_")
    for key in list(os.environ.keys()):
        for pfx in prefixes:
            if key.startswith(pfx) and len(key) > len(pfx):
                os.environ.setdefault("CHUDWARE_" + key[len(pfx):], os.environ[key])
                break


_apply_legacy_env_compat()


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
    """Per-user data root for Chudware: ``%APPDATA%\\Chudware`` / ``~/.local/share/Chudware``.

    Matching the C++ convention (core/agent/paths.cpp). An explicit APPDATA env var
    always wins (tests use it to redirect).

    New data is written here. For reading existing data, use :func:`data_roots` so the
    legacy Chudware directory is also checked.
    """
    return _base_dir() / APP_NAME


def _legacy_roots() -> list[Path]:
    """Candidate legacy data roots (Billet, then AI-CAD), newest name first."""
    base = _base_dir()
    return [base / name for name in APP_NAMES_LEGACY]


def appdata_root_legacy() -> Path:
    """The old per-user data root. Read-only fallback: the first legacy dir that
    exists (Billet preferred, then AI-CAD), else the newest legacy candidate.

    Legacy dirs are never moved or deleted automatically — they hold the active
    mcp-venv, config, and chat history, and venvs do not survive relocation.
    """
    for d in _legacy_roots():
        if d.exists():
            return d
    return _legacy_roots()[0]


def data_roots() -> list[Path]:
    """All candidate data root directories, new first, legacy second.

    Use when scanning for existing user data (config, models, parts library) so both
    the new Chudware directory and the legacy directories are checked.
    """
    return [appdata_root(), *_legacy_roots()]


def active_root() -> Path:
    """The data root actually in use for this process.

    Machines with an existing legacy ``Chudware`` directory (and no ``Chudware``
    directory yet) keep using the legacy directory for BOTH reads and writes — this
    avoids a split-brain where reads come from the old dir but new writes land in the
    new one, and keeps the live mcp-venv, config and caches working untouched.
    Fresh installs (no legacy dir) get the new ``Chudware`` directory.
    """
    new = appdata_root()
    if (new / "config.json").exists():
        return new
    # Prefer a legacy dir with a REAL install marker (config.json), newest name first —
    # a stray empty/cache-only dir must not flip the root away from live data.
    for d in _legacy_roots():
        if (d / "config.json").exists():
            return d
    # No marker anywhere: keep an existing legacy dir wholesale if present.
    for d in _legacy_roots():
        if d.exists():
            return d
    return new


ROOT = active_root()
RUNTIME = ROOT / "runtime"          # isolated python + wheels
CACHE = ROOT / "cache"              # per-model outputs (stl/step/png)
CONFIG = ROOT / "config.json"       # provider keys + settings
LLAMACPP = ROOT / "llamacpp"        # optional managed llama.cpp server payload
MODELS = ROOT / "models"            # user / bundled GGUF models
VENDOR_PARTS = ROOT / "vendor_parts"  # approved/manual supplier CAD + metadata cache
PARTS_LIBRARY = ROOT / "parts_library"  # watched folder of user-dropped CAD files (auto-registered)
BROWSER_PROFILES = ROOT / "browser_profiles"  # T-X1: local-only per-site browser login profiles
BROWSER_DOWNLOADS = ROOT / "browser_downloads"  # T-X2: artifacts landed by browser source adapters


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
    """Return the config.json path, preferring the new Chudware location but
    falling back to the legacy Chudware location if it exists there and not here."""
    new_cfg = ROOT / "config.json"
    if new_cfg.exists():
        return new_cfg
    for d in _legacy_roots():
        legacy_cfg = d / "config.json"
        if legacy_cfg.exists():
            return legacy_cfg
    return new_cfg  # canonical write target even if it doesn't exist yet
