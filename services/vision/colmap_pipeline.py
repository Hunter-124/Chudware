"""In-process pycolmap sparse Structure-from-Motion pipeline.

Uses pycolmap (and GLOMAP global mapper when available) to reconstruct a
sparse COLMAP model from an image directory, emitting a standard COLMAP model
(cameras / images / points3D) into ``<out_dir>/sparse/``.

Backend contract
----------------
- pycolmap is imported lazily; if absent the function returns the degradation
  envelope ``{"ok": False, "reason": "backend_not_provisioned", "hint": "..."}``
  (same shape as photogrammetry.py).
- On success returns ``{"ok": True, "sparse_dir": str, "registered": int,
  "backend": "pycolmap"|"pycolmap+glomap"}``.
- On partial failure (reconstruction ran but zero images registered) returns
  ok=False with reason "no_images_registered".

GLOMAP
------
The optional ``glomap`` Python binding is attempted first (global mapper,
more robust on unordered captures).  Falls back to pycolmap's incremental
mapper automatically.

Synthetic-fixture note
----------------------
The bracket_fixture renders are orthographic, which real SfM doesn't handle.
For test purposes each render is treated as a pinhole camera using the image
width as focal-length proxy (FOV ~53° diagonal).  Registration counts may be
lower than real-world captures; the acceptance threshold is ≥6/8.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public availability check
# ---------------------------------------------------------------------------

def _import_pycolmap():
    """Try to import pycolmap; return the module or None."""
    try:
        import pycolmap  # type: ignore
        return pycolmap
    except ImportError:
        return None


def available() -> bool:
    """Return True if pycolmap is importable."""
    return _import_pycolmap() is not None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_sparse(
    image_dir: str,
    out_dir: str,
    *,
    camera_model: str = "SIMPLE_RADIAL",
    single_camera: bool = True,
) -> dict:
    """Run in-process pycolmap sparse SfM on *image_dir*.

    Parameters
    ----------
    image_dir:
        Directory containing JPEG/PNG input images.
    out_dir:
        Session root; sparse model written to ``<out_dir>/sparse/``.
    camera_model:
        COLMAP camera model string (default ``SIMPLE_RADIAL``).
    single_camera:
        Assume all images share a single camera (suitable for bracket
        fixture and most turntable captures).

    Returns
    -------
    dict with keys: ok, sparse_dir, registered, backend  (on success)
         or:       ok, reason, hint                       (on failure)
    """
    pycolmap = _import_pycolmap()
    if pycolmap is None:
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": "pip install pycolmap",
        }

    image_dir = str(Path(image_dir).resolve())
    sparse_dir = str(Path(out_dir) / "sparse")
    os.makedirs(sparse_dir, exist_ok=True)

    # Attempt GLOMAP global mapper first (optional)
    try:
        import glomap  # type: ignore  # noqa: F401
        _has_glomap = True
    except ImportError:
        _has_glomap = False

    try:
        backend = "pycolmap"
        registered = 0
        if _has_glomap:
            try:
                registered = _run_glomap(pycolmap, image_dir, sparse_dir,
                                         camera_model, single_camera)
                backend = "pycolmap+glomap"
            except Exception:
                # An installed binding is not necessarily executable with this
                # pycolmap version. Use the documented incremental fallback.
                registered = 0
        if registered == 0:
            registered = _run_incremental(pycolmap, image_dir, sparse_dir,
                                          camera_model, single_camera)
            backend = "pycolmap"
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"reconstruction error: {exc}",
            "hint": "check image quality and pycolmap installation",
        }

    if registered == 0:
        return {
            "ok": False,
            "reason": "no_images_registered",
            "hint": "ensure images overlap sufficiently and are not all orthographic",
            "backend": backend,
            "sparse_dir": sparse_dir,
        }

    return {
        "ok": True,
        "sparse_dir": sparse_dir,
        "registered": registered,
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_incremental(
    pycolmap,
    image_dir: str,
    sparse_dir: str,
    camera_model: str,
    single_camera: bool,
) -> int:
    """Run pycolmap incremental mapper; return number of registered images."""
    with tempfile.TemporaryDirectory() as db_tmp:
        database_path = os.path.join(db_tmp, "colmap.db")

        # Feature extraction
        pycolmap.extract_features(
            database_path=database_path,
            image_path=image_dir,
            camera_mode=pycolmap.CameraMode.SINGLE if single_camera else pycolmap.CameraMode.AUTO,
            camera_model=camera_model,
        )

        # Feature matching (exhaustive for small sets)
        pycolmap.match_exhaustive(database_path=database_path)

        # Incremental reconstruction
        maps = pycolmap.incremental_mapping(
            database_path=database_path,
            image_path=image_dir,
            output_path=sparse_dir,
        )

        # maps is a dict {reconstruction_id: Reconstruction}
        if not maps:
            return 0

        # Pick the largest reconstruction and write it
        best = max(maps.values(), key=lambda r: r.num_reg_images())
        best.write(sparse_dir)
        return best.num_reg_images()


def _run_glomap(
    pycolmap,
    image_dir: str,
    sparse_dir: str,
    camera_model: str,
    single_camera: bool,
) -> int:
    """Run GLOMAP global mapper; return number of registered images."""
    import glomap  # type: ignore

    with tempfile.TemporaryDirectory() as db_tmp:
        database_path = os.path.join(db_tmp, "colmap.db")

        pycolmap.extract_features(
            database_path=database_path,
            image_path=image_dir,
            camera_mode=pycolmap.CameraMode.SINGLE if single_camera else pycolmap.CameraMode.AUTO,
            camera_model=camera_model,
        )
        pycolmap.match_exhaustive(database_path=database_path)

        reconstruction = glomap.mapper(
            database_path=database_path,
            image_path=image_dir,
            output_path=sparse_dir,
        )

        if reconstruction is None:
            return 0

        reconstruction.write(sparse_dir)
        return reconstruction.num_reg_images()
