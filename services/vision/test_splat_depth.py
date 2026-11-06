"""Depth rendering and bounded TSDF reconstruction from synthetic splats."""
from __future__ import annotations

import math
import time
import types

import numpy as np
import pytest

from services.formats.splat_io import GaussianCloud, splat_to_mesh
from services.vision import splat_depth
from services.vision.colmap_pipeline import ModelCamera, ModelImage


def _box_splats(center, size, samples=31):
    positions, all_scales = [], []
    lo = center - size/2
    for axis in range(3):
        other = [i for i in range(3) if i != axis]
        a, b = np.meshgrid(np.linspace(lo[other[0]], lo[other[0]] + size[other[0]], samples),
                           np.linspace(lo[other[1]], lo[other[1]] + size[other[1]], samples))
        for side in (0, 1):
            points = np.zeros((a.size, 3))
            points[:, axis] = lo[axis] + side*size[axis]
            points[:, other[0]], points[:, other[1]] = a.ravel(), b.ravel()
            scales = np.tile(size/(samples-1)*.7, (len(points), 1))
            scales[:, axis] = .001
            positions.append(points)
            all_scales.append(scales)
    means, scales = np.concatenate(positions), np.concatenate(all_scales)
    n = len(means)
    return GaussianCloud(means.astype(np.float32), scales.astype(np.float32),
                         np.tile([1, 0, 0, 0], (n, 1)).astype(np.float32),
                         np.full(n, .98, dtype=np.float32), np.zeros((n, 3), np.float32),
                         None, "synthetic", [])


def _ring_model(path, center, n=18):
    rotation_module = pytest.importorskip("scipy.spatial.transform")
    # A tilted circular orbit sees all six faces, unlike a horizontal orbit.
    normal = np.array([1., 2., 3.])
    normal /= np.linalg.norm(normal)
    a = np.cross(normal, [0., 0., 1.])
    a /= np.linalg.norm(a)
    b = np.cross(normal, a)
    (path / "cameras.txt").write_text("1 PINHOLE 192 192 205 205 96 96\n")
    rows = []
    for i, angle in enumerate(np.linspace(0, 2*math.pi, n, endpoint=False), 1):
        eye = center + 2.8*(a*math.cos(angle) + b*math.sin(angle))
        forward = center-eye
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0., 0., 1.])
        right /= np.linalg.norm(right)
        down = np.cross(forward, right)
        world_to_camera = np.stack([right, down, forward])
        xyzw = rotation_module.Rotation.from_matrix(world_to_camera).as_quat()
        q = xyzw[[3, 0, 1, 2]]
        t = -world_to_camera @ eye
        rows.append(f"{i} {' '.join(map(str, q))} {' '.join(map(str, t))} 1 view_{i}.png\n\n")
    (path / "images.txt").write_text("".join(rows))


def test_tsdf_box_accuracy_and_no_spurious_sheets(tmp_path):
    o3d = pytest.importorskip("open3d")
    center = np.array([.23, -.17, .31])
    size = np.array([1.2, 1., .8])
    cloud = _box_splats(center, size)
    _ring_model(tmp_path, center)
    target = tmp_path / "box.ply"
    started = time.monotonic()
    result = splat_to_mesh(cloud, target, sparse_dir=tmp_path)
    elapsed = time.monotonic() - started
    assert result["ok"] and result["approximate"] and result["method"] == "tsdf"
    assert result["registered_views"] == result["integrated_views"] == 18
    assert result["triangles"] > 100 and result["units"] == "reconstruction"
    assert result["gaussians_used"] <= splat_depth.MAX_GAUSSIANS
    assert result["max_resolution"] <= splat_depth.MAX_RESOLUTION
    assert elapsed < splat_depth.TIME_BUDGET_SECONDS + 15
    vertices = np.asarray(o3d.io.read_triangle_mesh(str(target)).vertices)
    np.testing.assert_allclose(np.ptp(vertices, axis=0), size, rtol=.03, atol=0)
    q = np.abs(vertices-center) - size/2
    distance = np.where(np.any(q > 0, axis=1), np.linalg.norm(np.maximum(q, 0), axis=1),
                        np.min(-q, axis=1))
    assert np.mean(distance <= 2*result["voxel_size"]) > .95


def _test_camera():
    return {"id": 1, "width": 32, "height": 32, "fx": 80., "fy": 80.,
            "cx": 16., "cy": 16., "R": np.eye(3), "t": np.zeros(3)}


def test_depth_render_is_front_to_back_anisotropic_and_masks_transparency():
    camera = _test_camera()
    means = np.array([[0, 0, 2.], [0, 0, 4.]])
    covariance = np.array([np.diag([.08**2, .02**2, .001**2]),
                           np.diag([.08**2, .02**2, .001**2])])
    colors = np.array([[255, 0, 0], [0, 0, 255]], dtype=np.uint8)
    deadline = time.monotonic() + 10
    depth, color, stats = splat_depth._render_view(
        means, covariance, np.array([.98, .98]), colors, camera,
        (np.full(3, -10.), np.full(3, 10.)), deadline)
    assert depth[16, 16] == pytest.approx(2, abs=1e-4)
    assert color[16, 16].tolist() == [255, 0, 0]
    assert depth[16, 18] > 0 and depth[18, 16] == 0
    assert stats["opaque_pixels"] > 0
    rotation = splat_depth._rotation_matrices(np.array([math.sqrt(.5), 0, 0, math.sqrt(.5)]))
    rotated = np.array([rotation @ covariance[0] @ rotation.T])
    turned, _, _ = splat_depth._render_view(
        means[:1], rotated, np.array([.98]), colors[:1], camera,
        (np.full(3, -10.), np.full(3, 10.)), deadline)
    assert turned[18, 16] > 0 and turned[16, 18] == 0
    depth, _, _ = splat_depth._render_view(
        means[:1], covariance[:1], np.array([.3]), colors[:1], camera,
        (np.full(3, -10.), np.full(3, 10.)), deadline)
    assert not depth.any()


def test_planar_support_rejects_extrapolation_without_axis_alignment():
    pytest.importorskip("scipy")
    x, y = np.meshgrid(np.linspace(-.12, .12, 7), np.linspace(-.12, .12, 7))
    centers = np.column_stack([x.ravel(), y.ravel(), np.zeros(x.size)])
    scales = np.tile([.028, .028, .001], (len(centers), 1))
    rotation = splat_depth._rotation_matrices([math.cos(.4), math.sin(.4), 0., 0.])
    translation = np.array([.3, -.4, .7])
    vertices = np.array([[.013, .012, .003], [.17, .005, .001],
                         [.01, .01, .025], [.12, 0., .001]])
    rotations = np.tile(rotation, (len(centers), 1, 1))
    world_centers = centers @ rotation.T + translation
    world_vertices = vertices @ rotation.T + translation
    deadline = time.monotonic() + 10
    supported, checked = splat_depth._surface_support(
        world_vertices, world_centers, scales, rotations, .01, deadline)
    assert checked == len(vertices)
    assert supported.tolist() == [True, False, False, True]
    # Without enough consistent neighbors, do not invent a support plane.
    supported, checked = splat_depth._surface_support(
        world_vertices, world_centers[:5], scales[:5], rotations[:5], .01, deadline)
    assert checked == 0 and supported.all()


def test_colmap_world_to_camera_and_scaled_intrinsics():
    image = ModelImage(1, 1, "view", (math.sqrt(.5), 0., 0., math.sqrt(.5)), (1., -.5, 2.))
    camera = splat_depth._camera(image, ModelCamera(1, "PINHOLE", 200, 100, (120., 100., 90., 40.)), 100)
    assert (camera["width"], camera["height"], camera["fx"], camera["fy"], camera["cx"], camera["cy"]) == (100, 50, 60, 50, 45, 20)
    projected, _ = splat_depth._project(np.array([[.5, 1., 1.]]), np.array([np.eye(3)*.0001]),
                                         np.array([.9]), np.array([[255, 255, 255]], np.uint8), camera)
    assert projected["u"][0] == pytest.approx(45)
    assert projected["v"][0] == pytest.approx(20)
    assert projected["z"][0] == pytest.approx(3)


def test_distorted_calibration_rejected_and_bounds_enforced(monkeypatch):
    image = ModelImage(1, 1, "view", (1, 0, 0, 0), (0, 0, 0))
    distorted = ModelCamera(1, "SIMPLE_RADIAL", 100, 100, (100, 50, 50, .1))
    with pytest.raises(ValueError, match="undistorted"):
        splat_depth._camera(image, distorted, 100)
    args = (np.zeros((30, 3)), np.ones((30, 3)), np.tile([1, 0, 0, 0], (30, 1)),
            np.ones(30), np.zeros((30, 3), np.uint8), {}, {})
    with pytest.raises(ValueError, match="max_resolution"):
        splat_depth.fuse_splat_depth(*args, max_resolution=513)
    with pytest.raises(ValueError, match="time_budget"):
        splat_depth.fuse_splat_depth(*args, time_budget=121)
    camera = ModelCamera(1, "PINHOLE", 100, 100, (100, 100, 50, 50))
    times = iter([0., 1.])
    monkeypatch.setattr(splat_depth, "time", types.SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(TimeoutError, match="time budget"):
        splat_depth.fuse_splat_depth(*args[:5], {1: camera}, {1: image}, time_budget=.01)
