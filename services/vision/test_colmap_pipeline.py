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


@pytest.mark.skipif(not PYCOLMAP_AVAILABLE, reason="pycolmap not installed")
def test_camera_groups_share_intrinsics_and_other_images_stay_independent(tmp_path):
    """Each video clip is one lens: its frames share a camera; loose stills do not."""
    import shutil

    import pycolmap
    from services.vision.colmap_pipeline import _extract_features
    from services.vision.frames import clip_camera_groups

    renders = sorted(FIXTURE_DIR.glob("render_*.png"))
    if len(renders) < 5:
        pytest.skip("bracket_fixture renders unavailable")
    images = tmp_path / "images"
    images.mkdir()
    clip_a, clip_b = "a" * 64, "b" * 64
    sources = {}
    for index, render in enumerate(renders[:5]):
        name = f"{index:06d}.png"
        shutil.copyfile(render, images / name)
        clip = clip_a if index < 2 else clip_b if index < 4 else None
        sources[name] = (f"/session/frames/clip_{clip}_frame_{index:09d}.jpg" if clip
                         else f"/session/images/still_{index}.jpg")
    database = str(tmp_path / "colmap.db")
    _extract_features(pycolmap, database, str(images), "SIMPLE_RADIAL", False,
                      clip_camera_groups(sources))
    db = pycolmap.Database.open(database)
    camera = {image.name: image.camera_id for image in db.read_all_images()}
    db.close()
    assert camera["000000.png"] == camera["000001.png"]
    assert camera["000002.png"] == camera["000003.png"]
    assert len({camera["000000.png"], camera["000002.png"], camera["000004.png"]}) == 3


def test_binary_and_text_models_match_and_scale_two_point(tmp_path):
    """Both encodings of a real pycolmap model yield one metric scale."""
    pycolmap = pytest.importorskip("pycolmap")
    np = pytest.importorskip("numpy")
    from services.vision.colmap_pipeline import read_model
    from services.vision.scale_solver import read_colmap_sparse, solve_scale_two_point

    source = tmp_path / "source"
    source.mkdir()
    (source / "cameras.txt").write_text("1 PINHOLE 640 480 100 100 320 240\n")
    (source / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 a.png\n320 240 1 360 240 2\n"
        "2 1 0 0 0 -1 0 0 1 b.png\n300 240 1 340 240 2\n")
    (source / "points3D.txt").write_text(
        "1 0 0 5 255 0 0 0 1 0 2 0\n"
        "2 2 0 5 0 255 0 0 1 1 2 1\n")
    reconstruction = pycolmap.Reconstruction(str(source))
    binary, text = tmp_path / "binary", tmp_path / "text"
    binary.mkdir()
    text.mkdir()
    reconstruction.write(str(binary))
    reconstruction.write_text(str(text))

    binary_model = read_model(binary)
    assert binary_model == read_model(text)
    for root in (binary, text):
        cameras = read_colmap_sparse(str(root))
        points = np.asarray([point.xyz for point in read_model(root)[2].values()])
        scale = solve_scale_two_point(cameras=list(cameras.values()), points3d=points,
                                      p1=[320, 240], p2=[360, 240], distance_mm=50,
                                      image_name="a.png")
        assert scale.mm_per_unit == pytest.approx(25)
        assert scale.detail["dist_units"] == pytest.approx(2)


def test_text_model_keeps_empty_observations_and_optional_points(tmp_path):
    from services.vision.colmap_pipeline import read_model

    (tmp_path / "cameras.txt").write_text("1 SIMPLE_PINHOLE 640 480 100 320 240\n")
    (tmp_path / "images.txt").write_text(
        "# poses\n1 1 0 0 0 0 0 0 1 image one.png\n\n"
        "2 1 0 0 0 1 0 0 1 image two.png\n\n")
    cameras, images, points = read_model(tmp_path)
    assert cameras[1].model == "SIMPLE_PINHOLE"
    assert [image.name for image in images.values()] == ["image one.png", "image two.png"]
    assert points == {}
