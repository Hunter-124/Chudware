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


def model_dir(name: str) -> Path:
    d = CACHE / name
    d.mkdir(parents=True, exist_ok=True)
    return d
