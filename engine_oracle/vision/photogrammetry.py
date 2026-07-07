"""Meshroom/COLMAP subprocess driver for sparse+dense photogrammetry.

Never imports Meshroom or COLMAP — subprocess only.
Backend selection: MESHROOM_BIN env -> 'meshroom_batch' binary; COLMAP_BIN env -> colmap;
else unavailable.
"""
from __future__ import annotations

import glob
import os
import subprocess
from typing import Optional


class PhotogrammetryUnavailable(RuntimeError):
    """Raised when neither MESHROOM_BIN nor COLMAP_BIN is configured."""

    def __init__(
        self,
        reason: str = "no photogrammetry backend configured",
        hint: str = "set MESHROOM_BIN or COLMAP_BIN env var",
    ):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


def available_backend() -> Optional[str]:
    """Return 'meshroom', 'colmap', 'pycolmap', or None — read from env at call time.

    Priority: meshroom (env) > colmap (env) > pycolmap (in-process, RE-ANCHOR RE-09).
    Existing subprocess paths are unchanged.
    """
    if os.environ.get("MESHROOM_BIN"):
        return "meshroom"
    if os.environ.get("COLMAP_BIN"):
        return "colmap"
    # RE-ANCHOR RE-09: pycolmap in-process backend
    try:
        import pycolmap  # type: ignore  # noqa: F401
        return "pycolmap"
    except ImportError:
        pass
    return None


def _find_mesh(out_dir: str) -> Optional[str]:
    """Recursively find the first .obj or .ply in out_dir (prefer .obj)."""
    for pattern in ("**/*.obj", "**/*.ply"):
        hits = glob.glob(os.path.join(out_dir, pattern), recursive=True)
        if hits:
            return hits[0]
    return None


def reconstruct(
    image_dir: str,
    out_dir: str,
    *,
    scale_mm: Optional[float] = None,
) -> dict:
    """Run Meshroom or COLMAP on image_dir and write results to out_dir.

    Returns a dict with keys: ok, mesh_path (on success), backend, scale_mm.
    On unavailable backend or subprocess failure: ok=False, reason, hint.
    """
    backend = available_backend()
    if backend is None:
        return {
            "ok": False,
            "reason": "no photogrammetry backend configured",
            "hint": "set MESHROOM_BIN or COLMAP_BIN env var",
        }

    os.makedirs(out_dir, exist_ok=True)

    if backend == "meshroom":
        bin_path = os.environ["MESHROOM_BIN"]
        cmd = [
            bin_path,
            "--input", image_dir,
            "--output", out_dir,
            "--save", os.path.join(out_dir, "meshroom_project.mg"),
        ]
    else:  # colmap
        bin_path = os.environ["COLMAP_BIN"]
        cmd = [
            bin_path,
            "automatic_reconstructor",
            "--workspace_path", out_dir,
            "--image_path", image_dir,
        ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "reason": f"backend binary not found: {exc}",
            "hint": f"check that {backend.upper()}_BIN points to the correct executable",
            "backend": backend,
        }

    if result.returncode != 0:
        return {
            "ok": False,
            "reason": result.stderr[:500] if result.stderr else f"exit code {result.returncode}",
            "backend": backend,
        }

    mesh_path = _find_mesh(out_dir)
    if mesh_path is None:
        return {
            "ok": False,
            "reason": "reconstruction completed but no .obj or .ply found in output directory",
            "backend": backend,
        }

    return {
        "ok": True,
        "mesh_path": mesh_path,
        "backend": backend,
        "scale_mm": scale_mm,
    }
