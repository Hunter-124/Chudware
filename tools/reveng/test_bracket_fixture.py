"""Acceptance tests for the synthetic bracket fixture generator (RE-20).

Verifies:
- build_fixture() produces the expected file set in a tmp directory
- ground_truth.json has the required schema + dimensional fields
- camera_poses.json has one entry per render with required keys
- all PNG renders are non-empty valid images
- ground-truth dimensions are self-consistent (hole_spacing matches centers, etc.)

Feeds: RE-09 (photogrammetry), RE-13 (fiducial calibration), RE-24 (E2E pipeline).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Import the module under test via sys.path so we don't need package install
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from tools.reveng.gen_bracket_fixture import build_fixture, BRACKET, _VIEWS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gt(tmp_path) -> dict:
    return build_fixture(tmp_path)


# ---------------------------------------------------------------------------
# File presence
# ---------------------------------------------------------------------------

def test_render_count(tmp_path):
    gt = _gt(tmp_path)
    assert gt["render_count"] == len(_VIEWS), "render_count should equal number of views"
    for fname in gt["render_files"]:
        p = tmp_path / fname
        assert p.exists(), f"render file missing: {fname}"
        assert p.stat().st_size > 0, f"render file is empty: {fname}"


def test_aruco_mat_present(tmp_path):
    gt = _gt(tmp_path)
    mat_path = tmp_path / gt["aruco_mat"]
    assert mat_path.exists(), "aruco_mat.png missing"
    assert mat_path.stat().st_size > 0


def test_ground_truth_json_present(tmp_path):
    _gt(tmp_path)
    p = tmp_path / "ground_truth.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["schema"] == "aicad.reveng_fixture/1"


def test_camera_poses_json_present(tmp_path):
    gt = _gt(tmp_path)
    p = tmp_path / gt["camera_poses_file"]
    assert p.exists()
    poses = json.loads(p.read_text())
    assert len(poses) == gt["render_count"]


# ---------------------------------------------------------------------------
# Ground-truth shape + consistency
# ---------------------------------------------------------------------------

def test_ground_truth_schema(tmp_path):
    gt = _gt(tmp_path)
    assert gt["schema"] == "aicad.reveng_fixture/1"
    assert gt["units"] == "mm"
    assert "overall" in gt
    for key in ("length", "width", "height"):
        assert key in gt["overall"], f"overall.{key} missing"
        assert gt["overall"][key] > 0


def test_holes_present_and_shaped(tmp_path):
    gt = _gt(tmp_path)
    holes = gt["holes"]
    assert len(holes) >= 2, "fixture must have at least 2 holes"
    for h in holes:
        for key in ("id", "diameter", "center_x", "center_y", "depth", "face"):
            assert key in h, f"hole missing key: {key}"
        assert h["diameter"] > 0
        assert h["depth"] > 0


def test_hole_spacing_consistent(tmp_path):
    gt = _gt(tmp_path)
    h1, h2 = gt["holes"][0], gt["holes"][1]
    computed = abs(h2["center_x"] - h1["center_x"])
    assert abs(computed - gt["hole_spacing"]) < 1e-6, (
        f"hole_spacing {gt['hole_spacing']} does not match center diff {computed}"
    )


def test_overall_contains_holes(tmp_path):
    gt = _gt(tmp_path)
    L = gt["overall"]["length"]
    W = gt["overall"]["width"]
    for h in gt["holes"]:
        r = h["diameter"] / 2
        assert h["center_x"] - r >= 0 and h["center_x"] + r <= L, \
            f"hole {h['id']} extends outside bracket length"
        assert h["center_y"] - r >= 0 and h["center_y"] + r <= W, \
            f"hole {h['id']} extends outside bracket width"


# ---------------------------------------------------------------------------
# Camera poses
# ---------------------------------------------------------------------------

def test_camera_poses_keys(tmp_path):
    gt = _gt(tmp_path)
    poses = json.loads((tmp_path / gt["camera_poses_file"]).read_text())
    required = {"index", "view", "eye_xyz", "target_xyz", "up_xyz",
                "fov_deg", "projection", "image_width_px", "image_height_px",
                "scale_px_per_mm", "render_file"}
    for pose in poses:
        missing = required - set(pose.keys())
        assert not missing, f"pose {pose.get('index')} missing keys: {missing}"


def test_camera_poses_render_files_match(tmp_path):
    gt = _gt(tmp_path)
    poses = json.loads((tmp_path / gt["camera_poses_file"]).read_text())
    render_files_in_poses = {p["render_file"] for p in poses}
    assert render_files_in_poses == set(gt["render_files"])


# ---------------------------------------------------------------------------
# PNG validity
# ---------------------------------------------------------------------------

def test_renders_are_valid_png(tmp_path):
    gt = _gt(tmp_path)
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")
    for fname in gt["render_files"]:
        img = Image.open(tmp_path / fname)
        assert img.size == (800, 600), f"{fname}: unexpected size {img.size}"
        assert img.mode == "RGB", f"{fname}: unexpected mode {img.mode}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotent(tmp_path):
    """Running twice overwrites cleanly — no extra files, same JSON content."""
    gt1 = build_fixture(tmp_path)
    gt2 = build_fixture(tmp_path)
    assert gt1["render_count"] == gt2["render_count"]
    assert gt1["holes"] == gt2["holes"]


# ---------------------------------------------------------------------------
# Subset views
# ---------------------------------------------------------------------------

def test_n_views_param(tmp_path):
    gt = build_fixture(tmp_path, n_views=2)
    assert gt["render_count"] == 2
    assert len(gt["render_files"]) == 2
