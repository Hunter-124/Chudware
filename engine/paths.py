"""Canonical filesystem locations for AI-CAD.

Everything the app produces or installs lives under ``%APPDATA%\\AI-CAD`` so the
shipped (non-sandboxed) binary and this dev environment converge on the same layout.
"""
from __future__ import annotations

import os
from pathlib import Path


def appdata_root() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
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
    d = CACHE / name
    d.mkdir(parents=True, exist_ok=True)
    return d
