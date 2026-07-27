"""Canonical filesystem locations for Chudware.

Everything the app produces or installs lives under ``%APPDATA%\\Chudware`` (or
``~/.local/share/Chudware``) so the shipped (non-sandboxed) binary and this dev
environment converge on the same layout.

Legacy pre-rename roots (``Billet`` / ``AI-CAD``) are migrated forward ONCE by
:func:`active_root`: their contents are copied into the Chudware root
(copy-if-missing, skipping venvs and re-provisionable payloads — venvs do not
survive relocation). The old directory is never moved or deleted automatically.
Only when the copy cannot complete does the legacy root stay in use for that
boot. Call :func:`data_roots` for both candidate paths when scanning.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

APP_NAME = "Chudware"
# Legacy data-dir names, newest first. Used by the one-time migration in
# active_root() and the read fallback in data_roots()/find_config().
APP_NAMES_LEGACY = ["Billet", "AI-CAD"]

# Top-level payload dirs skipped by the migration: venvs bake absolute paths and
# cannot be relocated, models/llamacpp re-download, caches regenerate.
_MIGRATE_SKIP = frozenset({
    "runtime", "mcp-venv", "torch-venv", "models", "llamacpp", "cache", "prof",
    "cef_cache", "cef_cache_daemon", "cef_cache_gui",
})


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


def _copy_tree_if_missing(src: Path, dst: Path) -> bool:
    """Copy-if-missing merge of *src* into *dst*. Best-effort: False when any entry
    could not be copied (caller keeps the legacy root for this boot)."""
    if src.is_file():
        if dst.exists():
            return True
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True
        except OSError:
            return dst.exists()  # a concurrent copy winning the race is fine
    ok = True
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            ok = _copy_tree_if_missing(entry, dst / entry.name) and ok
    except OSError:
        ok = False
    return ok


def _migrate_legacy_root(old: Path, new: Path) -> bool:
    """One-time copy of a legacy install into the Chudware root (skips _MIGRATE_SKIP)."""
    ok = True
    try:
        new.mkdir(parents=True, exist_ok=True)
        for entry in old.iterdir():
            if entry.name in _MIGRATE_SKIP:
                continue
            ok = _copy_tree_if_missing(entry, new / entry.name) and ok
    except OSError:
        ok = False
    return ok


def data_roots() -> list[Path]:
    """All candidate data root directories, new first, legacy second.

    Use when scanning for existing user data (config, models, parts library) so both
    the new Chudware directory and the legacy directories are checked.
    """
    return [appdata_root(), *_legacy_roots()]


def active_root() -> Path:
    """The data root actually in use for this process.

    Always the Chudware root. A machine whose live data still sits under a legacy
    pre-rename root (marker: ``config.json``) gets it copied forward once — after
    that the legacy root is never used again. Only a FAILED copy keeps the legacy
    root in use for this boot, so a read-only disk strands nothing.
    """
    new = appdata_root()
    if (new / "config.json").exists():
        return new
    for d in _legacy_roots():
        if not (d / "config.json").exists():
            continue
        return new if _migrate_legacy_root(d, new) else d
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
    """Return the config.json path — the Chudware location, with a legacy-root read
    fallback for the rare boot where the one-time migration could not complete."""
    new_cfg = ROOT / "config.json"
    if new_cfg.exists():
        return new_cfg
    for d in _legacy_roots():
        legacy_cfg = d / "config.json"
        if legacy_cfg.exists():
            return legacy_cfg
    return new_cfg  # canonical write target even if it doesn't exist yet
