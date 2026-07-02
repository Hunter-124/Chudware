"""Canonical filesystem locations for AI-CAD.

Everything the app produces or installs lives under ``%APPDATA%\\AI-CAD`` so the
shipped (non-sandboxed) binary and this dev environment converge on the same layout.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def appdata_root() -> Path:
    """Per-user data root, matching the C++ convention (core/agent/paths.cpp):
    APPDATA on Windows, $XDG_DATA_HOME (default ~/.local/share) elsewhere.

    An explicit APPDATA env var always wins (tests use it to redirect). The old
    unconditional ``expanduser(r"~\\AppData\\Roaming")`` fallback silently created a
    literal ``~\\AppData\\Roaming/`` directory in CWD on Linux (backslashes are not
    path separators there, so expanduser is a no-op).
    """
    base = os.environ.get("APPDATA")
    if not base:
        if os.name == "nt":
            base = os.path.expanduser(r"~\AppData\Roaming")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "AI-CAD"


ROOT = appdata_root()
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
