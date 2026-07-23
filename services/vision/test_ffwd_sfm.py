"""Tests for services/vision/ffwd_sfm.py.

Acceptance:
  (a) graceful backend_not_provisioned when weights absent
  (b) with mocked model output, COLMAP-format writer produces valid cameras.txt/images.txt
"""

from __future__ import annotations

import math
import pathlib
import tempfile
import types
import sys

import pytest

from ..vision.ffwd_sfm import run_ffwd_sfm, write_colmap_sparse


# ---------------------------------------------------------------------------
# (a) graceful fallback when no weights present
# ---------------------------------------------------------------------------

def test_no_backend_returns_not_provisioned(tmp_path, monkeypatch):
    """When no weights are present, run_ffwd_sfm must return ok=False with
    reason='backend_not_provisioned' — never a traceback/exception."""
    # Ensure env vars don't accidentally point at real weights
    monkeypatch.delenv("VGGT_WEIGHTS_DIR", raising=False)
    monkeypatch.delenv("MAPANYTHING_WEIGHTS_DIR", raising=False)

    # Patch _vggt_available and _mapanything_available to False
    from ..vision import ffwd_sfm as mod
    monkeypatch.setattr(mod, "_vggt_available", lambda: False)
    monkeypatch.setattr(mod, "_mapanything_available", lambda: False)

    result = run_ffwd_sfm(["img1.jpg", "img2.jpg"], str(tmp_path / "session"))

    assert result["ok"] is False
    assert result["reason"] == "backend_not_provisioned"
    assert "hint" in result


def test_no_images_returns_error(tmp_path, monkeypatch):
    """Empty image list should return ok=False without crashing."""
    from ..vision import ffwd_sfm as mod
    monkeypatch.setattr(mod, "_vggt_available", lambda: False)
    monkeypatch.setattr(mod, "_mapanything_available", lambda: False)

    result = run_ffwd_sfm([], str(tmp_path / "session"))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# (b) COLMAP writer produces valid cameras.txt / images.txt
# ---------------------------------------------------------------------------

def _identity_rot():
    """3×3 identity as nested lists."""
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _make_rots_and_ts(n: int):
    """Return n identity rotations and zero translations."""
    Rs = [_identity_rot() for _ in range(n)]
    ts = [[0.0, 0.0, float(i)] for i in range(n)]
    return Rs, ts


def test_write_colmap_cameras_txt(tmp_path):
    """write_colmap_sparse must emit a parseable cameras.txt."""
    Rs, ts = _make_rots_and_ts(2)
    write_colmap_sparse(
        sparse_dir=tmp_path,
        image_names=["a.jpg", "b.jpg"],
        rotations=Rs,
        translations=ts,
        fx=800.0, fy=800.0, cx=320.0, cy=240.0,
        width=640, height=480,
    )

    cameras_txt = tmp_path / "cameras.txt"
    assert cameras_txt.exists()

    data_lines = [
        l for l in cameras_txt.read_text().splitlines()
        if l and not l.startswith("#")
    ]
    assert len(data_lines) == 1, "one shared camera"
    parts = data_lines[0].split()
    assert parts[0] == "1"          # camera_id
    assert parts[1] == "PINHOLE"    # model
    assert int(parts[2]) == 640     # width
    assert int(parts[3]) == 480     # height
    assert abs(float(parts[4]) - 800.0) < 1e-3  # fx


def test_write_colmap_images_txt(tmp_path):
    """write_colmap_sparse must emit a parseable images.txt with correct pose lines."""
    Rs, ts = _make_rots_and_ts(3)
    names = ["frame_00.jpg", "frame_01.jpg", "frame_02.jpg"]
    write_colmap_sparse(
        sparse_dir=tmp_path,
        image_names=names,
        rotations=Rs,
        translations=ts,
        fx=600.0, fy=600.0, cx=256.0, cy=256.0,
        width=512, height=512,
    )

    images_txt = tmp_path / "images.txt"
    assert images_txt.exists()

    lines = [l for l in images_txt.read_text().splitlines() if not l.startswith("#")]
    # Alternating: pose line, empty POINTS2D line → 6 non-comment lines for 3 images
    pose_lines = [l for l in lines if l.strip()]
    assert len(pose_lines) == 3

    for idx, line in enumerate(pose_lines):
        parts = line.split()
        assert int(parts[0]) == idx + 1   # IMAGE_ID
        # Quaternion components
        qw, qx, qy, qz = map(float, parts[1:5])
        norm = math.sqrt(qw**2 + qx**2 + qy**2 + qz**2)
        assert abs(norm - 1.0) < 1e-6, f"quaternion not unit for image {idx}"
        # Translation z should match our ts[idx][2]
        tz = float(parts[7])
        assert abs(tz - float(idx)) < 1e-6
        # Camera ID
        assert int(parts[8]) == 1
        # Name
        assert parts[9] == names[idx]


def test_write_colmap_points3d_exists(tmp_path):
    """points3D.txt must exist (may be empty body)."""
    Rs, ts = _make_rots_and_ts(1)
    write_colmap_sparse(
        sparse_dir=tmp_path,
        image_names=["x.jpg"],
        rotations=Rs,
        translations=ts,
        fx=500.0, fy=500.0, cx=256.0, cy=256.0,
        width=512, height=512,
    )
    assert (tmp_path / "points3D.txt").exists()


def test_run_ffwd_sfm_with_mocked_vggt(tmp_path, monkeypatch):
    """run_ffwd_sfm with _vggt_available=True + mocked _run_vggt must succeed."""
    from ..vision import ffwd_sfm as mod

    monkeypatch.setattr(mod, "_vggt_available", lambda: True)

    captured: dict = {}

    def fake_run_vggt(image_paths, sparse_dir):
        # Actually write real COLMAP files so the caller can verify
        Rs, ts = _make_rots_and_ts(len(image_paths))
        write_colmap_sparse(
            sparse_dir=sparse_dir,
            image_names=[pathlib.Path(p).name for p in image_paths],
            rotations=Rs,
            translations=ts,
            fx=700.0, fy=700.0, cx=300.0, cy=200.0,
            width=600, height=400,
        )
        captured["called"] = True
        return {"ok": True, "backend": "vggt", "n_images": len(image_paths)}

    monkeypatch.setattr(mod, "_run_vggt", fake_run_vggt)

    session_dir = tmp_path / "session"
    result = run_ffwd_sfm(["img1.jpg", "img2.jpg"], str(session_dir))

    assert result["ok"] is True
    assert result["backend"] == "vggt"
    assert captured.get("called")
    # Verify files were written
    assert (session_dir / "sparse" / "cameras.txt").exists()
    assert (session_dir / "sparse" / "images.txt").exists()
