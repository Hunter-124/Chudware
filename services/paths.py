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
import sqlite3
import uuid

from services.agent import request_context

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
    if request_context.in_cell():
        return actor_root()
    return _base_dir() / APP_NAME


def actor_root() -> Path:
    """Private actor storage in the nested worker sandbox, never desktop state."""
    if not request_context.in_cell():
        return globals().get("ROOT") or __getattr__("ROOT")
    return request_context.actor_state_root()


def private_path(kind: str, handle: str, *, create: bool = True) -> Path | None:
    """Persistently map a public handle to a server-minted storage name."""
    if not re.fullmatch(r"[a-z_]+", kind):
        raise ValueError("invalid storage kind")
    root = actor_root()
    directory = root / kind
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(root / "handles.sqlite3", timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS handles "
                   "(kind TEXT, handle TEXT, token TEXT, owner TEXT, PRIMARY KEY(kind, handle))")
        owner = repr(request_context.owner_key())
        if create:
            db.execute("INSERT OR IGNORE INTO handles VALUES (?, ?, ?, ?)",
                       (kind, str(handle), uuid.uuid4().hex, owner))
        row = db.execute("SELECT token, owner FROM handles WHERE kind=? AND handle=?",
                         (kind, str(handle))).fetchone()
    if row is None:
        return None
    if row[1] != owner:
        raise KeyError("not_found")
    token = row[0]
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise KeyError("not_found")
    return directory / token


def artifact_dir(handle: str, *, create: bool = False) -> Path:
    """Resolve only this actor's artifact handle; foreign and absent are identical."""
    if not request_context.in_cell():
        return model_dir(handle)
    p = private_path("artifacts", handle, create=create)
    if p is None or (not create and not p.is_dir()):
        raise KeyError("not_found")
    if create:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


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
    if request_context.in_cell():
        return [actor_root()]
    return [appdata_root(), *_legacy_roots()]


def active_root() -> Path:
    """The data root actually in use for this process.

    Always the Chudware root. A machine whose live data still sits under a legacy
    pre-rename root (marker: ``config.json``) gets it copied forward once — after
    that the legacy root is never used again. Only a FAILED copy keeps the legacy
    root in use for this boot, so a read-only disk strands nothing.
    """
    new = appdata_root()
    if request_context.in_cell():
        return actor_root()
    if (new / "config.json").exists():
        return new
    for d in _legacy_roots():
        if not (d / "config.json").exists():
            continue
        return new if _migrate_legacy_root(d, new) else d
    return new


_DESKTOP_ROOT = active_root() if not request_context.in_cell() else None
_PATH_SUFFIXES = {
    "ROOT": "", "RUNTIME": "runtime", "CACHE": "cache", "CONFIG": "config.json",
    "LLAMACPP": "llamacpp", "MODELS": "models", "VENDOR_PARTS": "vendor_parts",
    "PARTS_LIBRARY": "parts_library", "BROWSER_PROFILES": "browser_profiles",
    "BROWSER_DOWNLOADS": "browser_downloads",
}

# Reloading replaces any materialized legacy constants or test overrides with
# request-time lookup, so none can shadow actor-private paths after reload.
for _path_name in _PATH_SUFFIXES:
    globals().pop(_path_name, None)


def __getattr__(name: str) -> Path:
    if name not in _PATH_SUFFIXES:
        raise AttributeError(name)
    if name == "RUNTIME" and os.environ.get("CHUDWARE_CELL") == "1":
        return request_context.process_runtime_root()
    root = actor_root() if request_context.in_cell() else _DESKTOP_ROOT
    if root is None:
        root = active_root()
    return root / _PATH_SUFFIXES[name]


def parts_library_dir() -> Path:
    """Return the watched parts-library folder, creating it on first access."""
    directory = (actor_root() / "parts_library" if request_context.in_cell()
                 else globals().get("PARTS_LIBRARY") or __getattr__("PARTS_LIBRARY"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def model_dir(name: str) -> Path:
    """Return (and create) the per-model output directory under :data:`CACHE`.

    Sanitizes ``name`` so a hostile ``run_model`` / worker caller cannot escape the cache
    root via ``..`` path segments.
    """
    if request_context.in_cell():
        return artifact_dir(name, create=True)
    safe = re.sub(r"[^\w.-]+", "_", str(name)).strip("._") or "model"
    root = (globals().get("CACHE") or __getattr__("CACHE")).resolve()
    d = (root / safe).resolve()
    if d != root and root not in d.parents:
        safe = "model"
        d = root / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_config() -> Path:
    """Return the config.json path — the Chudware location, with a legacy-root read
    fallback for the rare boot where the one-time migration could not complete."""
    if request_context.in_cell():
        return actor_root() / "config.json"
    new_cfg = (globals().get("ROOT") or __getattr__("ROOT")) / "config.json"
    if new_cfg.exists():
        return new_cfg
    for d in _legacy_roots():
        legacy_cfg = d / "config.json"
        if legacy_cfg.exists():
            return legacy_cfg
    return new_cfg  # canonical write target even if it doesn't exist yet
