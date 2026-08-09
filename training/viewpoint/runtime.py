"""Runtime selection and polite resource defaults for viewpoint experiments."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

MAX_WORKERS = 6
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_ROOT = REPO_ROOT / "scratch"
DEFAULT_CACHE_DIR = SCRATCH_ROOT / "viewpoint-cache"
TMP_ROOT = SCRATCH_ROOT / "tmp"

# These must be established before NumPy/Torch initializes its native thread pools.
for _name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_name, str(MAX_WORKERS))
os.environ.setdefault("TMPDIR", str(TMP_ROOT))
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def cap_workers(workers: int) -> int:
    """Return a positive process count capped at the workstation-friendly limit."""
    if workers <= 0:
        raise ValueError("workers must be positive")
    return min(int(workers), MAX_WORKERS)

def generation_workers(device: str, requested: int) -> int:
    """Avoid one CUDA context per process; GPU generation batches within one process."""
    return 1 if device.startswith("cuda") else cap_workers(requested)


def oracle_device(device: str) -> str:
    """Map the experiment device onto the accelerator's strict execution request."""
    return "cuda" if device.startswith("cuda") else "numpy"


def _optional_torch() -> Any | None:
    try:
        import torch
    except (ImportError, ModuleNotFoundError, OSError):
        return None
    return torch


def default_device() -> str:
    """Prefer CUDA only when the optional Torch install can actually use it."""
    torch = _optional_torch()
    return "cuda" if torch is not None and bool(torch.cuda.is_available()) else "cpu"


def resolve_device(requested: str | None) -> str:
    """Resolve an actual device without silently weakening an explicit request."""
    device = default_device() if requested in (None, "auto") else str(requested)
    if device.startswith("cuda"):
        torch = _optional_torch()
        if torch is None:
            raise RuntimeError(f"requested {device!r}, but optional Torch is not importable")
        if not bool(torch.cuda.is_available()):
            raise RuntimeError(f"requested {device!r}, but Torch reports no CUDA device")
    if not (device == "cpu" or device.startswith("cuda")):
        raise ValueError("device must be 'auto', 'cpu', 'cuda', or a CUDA device such as 'cuda:0'")
    return device


def report_device(device: str, fit_backend: str) -> None:
    """Emit unambiguous execution provenance separately from JSON output."""
    print(f"DEVICE: {device}", file=sys.stderr, flush=True)
    print(f"fit_backend: {fit_backend}", file=sys.stderr, flush=True)


__all__ = [
    "DEFAULT_CACHE_DIR",
    "MAX_WORKERS",
    "REPO_ROOT",
    "SCRATCH_ROOT",
    "cap_workers",
    "generation_workers",
    "oracle_device",
    "default_device",
    "report_device",
    "resolve_device",
]
