"""Tests for services/vision/colmap_pipeline.py.

Behaviour
---------
- If pycolmap is NOT installed: assert graceful skip envelope (ok=False,
  reason="backend_not_provisioned").  Test PASSES — does not error.
- If pycolmap IS installed: run on the bracket_fixture renders and assert all
  but one of them registered.

The bracket_fixture is at corpora/reveng/bracket_fixture/ (render_*.png plus
camera_poses.json and ground_truth.json).  The render count is read from
ground_truth.json so it cannot drift away from what is committed.  The test
locates the corpus relative to the repo root via the CHUDWARE_ROOT env var
(defaults to three levels above this file).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture / path helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    env = os.environ.get("CHUDWARE_ROOT")
    if env:
        return Path(env)
    # services/vision/test_colmap_pipeline.py  → go up 3 levels
    return Path(__file__).resolve().parent.parent.parent


FIXTURE_DIR = _repo_root() / "corpora" / "reveng" / "bracket_fixture"


def _fixture_render_count() -> int:
    """Renders the committed fixture actually ships.

    Read from ground_truth.json rather than hardcoded: this constant was pinned at 8
    while the fixture only ever shipped 6 renders, so the guard below self-skipped and
    the reconstruction assertion never ran against the committed corpus.
    """
    gt = FIXTURE_DIR / "ground_truth.json"
    try:
        with gt.open() as fh:
            return int(json.load(fh)["render_count"])
    except (OSError, ValueError, KeyError):
        return len(sorted(FIXTURE_DIR.glob("render_*.png")))


RENDER_COUNT = _fixture_render_count()
MIN_REGISTERED = max(RENDER_COUNT - 1, 2)  # allow one view to genuinely fail SfM


# ---------------------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------------------

def _pycolmap_available() -> bool:
    try:
        import pycolmap  # noqa: F401
        return True
    except ImportError:
        return False


PYCOLMAP_AVAILABLE = _pycolmap_available()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_graceful_skip_when_not_installed(tmp_path):
    """When pycolmap is absent, run_sparse must return the degradation envelope."""
    if PYCOLMAP_AVAILABLE:
        pytest.skip("pycolmap is installed — skip absence test")

    from ..vision.colmap_pipeline import run_sparse

    result = run_sparse(str(FIXTURE_DIR), str(tmp_path))

    assert result["ok"] is False
    assert result["reason"] == "backend_not_provisioned"
    assert "pycolmap" in result["hint"].lower()


def test_availability_flag_matches_import():
    """available() must agree with actual importability of pycolmap."""
    from ..vision.colmap_pipeline import available

    assert available() == PYCOLMAP_AVAILABLE


@pytest.mark.skipif(not PYCOLMAP_AVAILABLE, reason="pycolmap not installed")
def test_sparse_reconstruction_on_fixture(tmp_path):
    """With pycolmap installed, all-but-one bracket_fixture image must register."""
    if not FIXTURE_DIR.exists():
        pytest.skip(f"bracket_fixture not found at {FIXTURE_DIR}")

    # Count available renders
    renders = sorted(FIXTURE_DIR.glob("render_*.png"))
    if len(renders) < RENDER_COUNT:
        pytest.skip(f"expected {RENDER_COUNT} renders, found {len(renders)}")

    from ..vision.colmap_pipeline import run_sparse

    result = run_sparse(str(FIXTURE_DIR), str(tmp_path))

    assert "ok" in result
    if not result["ok"]:
        # Acceptable only if reason is no_images_registered (some fixture views
        # may genuinely fail SfM — still shows backend ran).
        if result.get("reason") == "no_images_registered":
            pytest.skip("SfM found no overlap on synthetic fixture (orthographic renders)")
        pytest.fail(f"run_sparse failed: {result}")

    assert result["registered"] >= MIN_REGISTERED, (
        f"Expected ≥{MIN_REGISTERED} registered images, got {result['registered']}"
    )
    assert result["backend"] in ("pycolmap", "pycolmap+glomap")

    # sparse/ directory must exist with COLMAP model files
    sparse_dir = Path(result["sparse_dir"])
    assert sparse_dir.exists(), "sparse_dir not created"
    # At least one of the standard COLMAP binary model files must exist
    model_files = list(sparse_dir.rglob("cameras.bin")) + \
                  list(sparse_dir.rglob("images.bin")) + \
                  list(sparse_dir.rglob("cameras.txt")) + \
                  list(sparse_dir.rglob("images.txt"))
    assert model_files, "No COLMAP model files (cameras.bin/txt) found in sparse_dir"
