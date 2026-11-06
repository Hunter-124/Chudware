"""Median-depth rendering, subject classification and TSDF fusion from synthetic splats."""
from __future__ import annotations

import math
import types

import numpy as np
import pytest

from services.formats.splat_io import GaussianCloud, splat_to_mesh
from services.vision import splat_depth
from services.vision.colmap_pipeline import ModelCamera, ModelImage


def _box_splats(center, size, samples=31, bottom=True):
    """Face splats whose rendered silhouette is the box: a dense face grid's alpha-1/2
    edge lies 1.43 sigma beyond its last row, so the rows are inset by that much."""
    positions, all_scales = [], []
    lo = center - size/2
    sigma = .7 * size/(samples - 1)
    inset = 1.43 * sigma
    for axis in range(3):
        other = [i for i in range(3) if i != axis]
        a, b = np.meshgrid(np.linspace(lo[other[0]] + inset[other[0]], lo[other[0]] + size[other[0]] - inset[other[0]], samples),
                           np.linspace(lo[other[1]] + inset[other[1]], lo[other[1]] + size[other[1]] - inset[other[1]], samples))
        for side in (0, 1):
            if axis == 2 and side == 0 and not bottom:
                continue
            points = np.zeros((a.size, 3))
            points[:, axis] = lo[axis] + side*size[axis]
            points[:, other[0]], points[:, other[1]] = a.ravel(), b.ravel()
            scales = np.tile(sigma, (len(points), 1))
            scales[:, axis] = .001
            positions.append(points)
            all_scales.append(scales)
    return np.concatenate(positions), np.concatenate(all_scales)


def _cloud(means, scales):
    n = len(means)
    return GaussianCloud(means.astype(np.float32), scales.astype(np.float32),
                         np.tile([1, 0, 0, 0], (n, 1)).astype(np.float32),
                         np.full(n, .98, dtype=np.float32), np.zeros((n, 3), np.float32),
                         None, "synthetic", [])


def _floor_splats(half, spacing=.05):
    a, b = np.meshgrid(np.arange(-half, half + 1e-9, spacing), np.arange(-half, half + 1e-9, spacing))
    means = np.column_stack((a.ravel(), b.ravel(), np.zeros(a.size)))
    return means, np.tile([spacing*.7, spacing*.7, .001], (len(means), 1))


def _write_model(path, eyes, center):
    rotation_module = pytest.importorskip("scipy.spatial.transform")
    (path / "cameras.txt").write_text("1 PINHOLE 192 192 205 205 96 96\n")
    rows = []
    for i, eye in enumerate(eyes, 1):
        forward = center - eye
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0., 0., 1.])
        right /= np.linalg.norm(right)
        down = np.cross(forward, right)
        world_to_camera = np.stack([right, down, forward])
        q = rotation_module.Rotation.from_matrix(world_to_camera).as_quat()[[3, 0, 1, 2]]
        t = -world_to_camera @ eye
        rows.append(f"{i} {' '.join(map(str, q))} {' '.join(map(str, t))} 1 view_{i}.png\n\n")
    (path / "images.txt").write_text("".join(rows))


def _tilted_ring(center, n=18):
    # A tilted circular orbit sees all six faces, unlike a horizontal orbit.
    normal = np.array([1., 2., 3.])
    normal /= np.linalg.norm(normal)
    a = np.cross(normal, [0., 0., 1.])
    a /= np.linalg.norm(a)
    b = np.cross(normal, a)
    return [center + 2.8*(a*math.cos(t) + b*math.sin(t)) for t in np.linspace(0, 2*math.pi, n, endpoint=False)]


def _box_distance(vertices, center, size):
    q = np.abs(vertices - center) - size/2
    return np.where(np.any(q > 0, axis=1), np.linalg.norm(np.maximum(q, 0), axis=1), np.min(-q, axis=1))


def test_tsdf_box_accuracy_keeps_every_face(tmp_path):
    o3d = pytest.importorskip("open3d")
    center = np.array([.23, -.17, .31])
    size = np.array([1.2, 1., .8])
    _write_model(tmp_path, _tilted_ring(center), center)
    target = tmp_path / "box.ply"
    result = splat_to_mesh(_cloud(*_box_splats(center, size)), target, sparse_dir=tmp_path)
    assert result["ok"] and result["approximate"] and result["method"] == "tsdf"
    assert result["registered_views"] == result["integrated_views"] == 18
    assert result["units"] == "reconstruction"
    # No box face is mistaken for a supporting floor: all six survive.
    assert result["support_plane"] is None
    vertices = np.asarray(o3d.io.read_triangle_mesh(str(target)).vertices)
    voxel = result["voxel_size"]
    assert np.all(np.abs(np.ptp(vertices, axis=0) - size) <= 2.5*voxel)
    assert np.mean(_box_distance(vertices, center, size) <= 2*voxel) > .95
    for axis in range(3):
        for sign in (-1, 1):
            face = center[axis] + sign*size[axis]/2
            assert np.count_nonzero(np.abs(vertices[:, axis] - face) < voxel) > 100


def test_tsdf_removes_support_plane_and_keeps_subject_on_it(tmp_path):
    o3d = pytest.importorskip("open3d")
    center = np.array([0., 0., .4])
    size = np.array([1.2, 1., .8])
    box, box_scales = _box_splats(center, size, bottom=False)
    floor, floor_scales = _floor_splats(2.)
    eyes = [np.array([3.2*math.cos(t), 3.2*math.sin(t), 2.6]) for t in np.linspace(0, 2*math.pi, 18, endpoint=False)]
    _write_model(tmp_path, eyes, center)
    target = tmp_path / "standing.ply"
    result = splat_to_mesh(_cloud(np.vstack((box, floor)), np.vstack((box_scales, floor_scales))),
                           target, sparse_dir=tmp_path)
    assert result["method"] == "tsdf"
    plane = result["support_plane"]
    assert plane is not None and np.dot(plane["normal"], [0, 0, 1]) > .999
    assert abs(plane["offset"]) < .01
    vertices = np.asarray(o3d.io.read_triangle_mesh(str(target)).vertices)
    # The floor is gone; the box rises from just above it to its full height.
    assert vertices[:, 2].min() > 0
    voxel = result["voxel_size"]
    assert vertices[:, 2].max() == pytest.approx(size[2], abs=2*voxel)
    assert np.all(np.abs(np.ptp(vertices[:, :2], axis=0) - size[:2]) <= 2.5*voxel)


def _camera_record(width=32, focal=80.):
    return {"id": 1, "name": "view", "width": width, "height": width, "fx": focal, "fy": focal,
            "cx": width/2, "cy": width/2, "R": np.eye(3), "t": np.zeros(3)}


def _render(means, covariance, opacity, camera=None):
    full = np.asarray(covariance, dtype=np.float64)
    cov6 = np.stack((full[:, 0, 0], full[:, 0, 1], full[:, 0, 2], full[:, 1, 1], full[:, 1, 2], full[:, 2, 2]), 1)
    depth, alpha, _ = splat_depth.render_median_depth(np.asarray(means, float), cov6, np.asarray(opacity, float),
                                                      camera or _camera_record())
    return depth, alpha


def test_median_depth_is_front_to_back_and_independent_of_splat_order():
    means = np.array([[0, 0, 2.], [0, 0, 4.]])
    covariance = np.array([np.diag([.08**2, .02**2, .001**2])] * 2)
    depth, alpha = _render(means, covariance, [.98, .98])
    assert depth[16, 16] == pytest.approx(2, abs=2e-3)
    assert alpha[16, 16] > .9
    reversed_depth, _ = _render(means[::-1], covariance, [.98, .98])
    np.testing.assert_allclose(reversed_depth, depth, atol=1e-9)


def test_median_depth_passes_layers_that_leave_more_than_half_the_light():
    means = np.array([[0, 0, 2.], [0, 0, 4.]])
    covariance = np.array([np.diag([.08**2, .08**2, .001**2])] * 2)
    depth, _ = _render(means, covariance, [.3, .9])
    assert depth[16, 16] == pytest.approx(4, abs=4e-3)
    lone, alpha = _render(means[:1], covariance[:1], [.3])
    assert not lone.any() and alpha[16, 16] == pytest.approx(.3, abs=.01)


def test_footprint_is_anisotropic_and_tilted_splat_follows_its_plane():
    covariance = np.diag([.08**2, .02**2, .001**2])
    depth, _ = _render([[0, 0, 2.]], [covariance], [.98])
    assert depth[16, 18] > 0 and depth[18, 16] == 0
    turn = splat_depth._rotation_matrices(np.array([math.sqrt(.5), 0, 0, math.sqrt(.5)]))
    turned, _ = _render([[0, 0, 2.]], [turn @ covariance @ turn.T], [.98])
    assert turned[18, 16] > 0 and turned[16, 18] == 0
    # A thin disk tilted 30 degrees about y: depth follows the ray-plane intersection.
    tilt = splat_depth._rotation_matrices(np.array([math.cos(math.radians(15)), 0, math.sin(math.radians(15)), 0]))
    disk = tilt @ np.diag([.1**2, .1**2, .0005**2]) @ tilt.T
    depth, _ = _render([[0, 0, 2.]], [disk], [.98])
    normal = tilt[:, 2]
    for u in range(12, 20):  # Where the disk is opaque (alpha >= 1/2) along this row.
        ray = np.array([(u + .5 - 16) / 80., (16 + .5 - 16) / 80., 1.])
        assert depth[16, u] == pytest.approx(2 * normal[2] / (normal @ ray), rel=.003)


def test_support_plane_is_the_floor_not_a_face_of_the_subject():
    center, size = np.array([0., 0., .4]), np.array([1.2, 1., .8])
    box, _ = _box_splats(center, size, bottom=False)
    floor, _ = _floor_splats(2., spacing=.02)
    hidden = (np.abs(floor[:, 0]) < .6) & (np.abs(floor[:, 1]) < .5)
    eyes = [np.array([3.2*math.cos(t), 3.2*math.sin(t), 2.6]) for t in np.linspace(0, 2*math.pi, 12, endpoint=False)]
    plane = splat_depth._fit_support_plane(np.vstack((box, floor[~hidden])), eyes)
    assert plane is not None
    normal, offset, noise = plane
    assert normal @ [0, 0, 1] > .999 and abs(offset) < 1e-6 and noise < 1e-6
    # The box alone: its top has the box behind it, its sides are seen from both sides.
    assert splat_depth._fit_support_plane(box, eyes) is None


def test_consistency_drops_surfaces_only_one_view_renders():
    views, depths = [], []
    for x in (0., .15, -.15, .3):
        camera = _camera_record(width=48, focal=60.)
        camera["R"] = np.diag([1., -1., -1.])  # Looking down -z from z = 2.
        camera["t"] = -camera["R"] @ np.array([x, 0., 2.])
        views.append(camera)
        depths.append(np.full((48, 48), 2., dtype=np.float32))
    depths[0][20:28, 20:28] = 1.2  # A floater in front of the plane, in one view only.
    subjects = [d.copy() for d in depths]
    rejected = splat_depth._keep_consistent(views, depths, subjects, voxel=.01)
    assert not subjects[0][20:28, 20:28].any()
    assert subjects[0][10, 24] == 2 and subjects[1][24, 24] == 2
    assert rejected >= 64


def test_colmap_world_to_camera_and_scaled_intrinsics():
    image = ModelImage(1, 1, "view", (math.sqrt(.5), 0., 0., math.sqrt(.5)), (1., -.5, 2.))
    camera = splat_depth._camera(image, ModelCamera(1, "PINHOLE", 200, 100, (120., 100., 90., 40.)), 100)
    assert (camera["width"], camera["height"], camera["fx"], camera["fy"], camera["cx"], camera["cy"]) == (100, 50, 60, 50, 45, 20)
    projected = splat_depth._project(np.array([[.5, 1., 1.]]), np.array([[1e-4, 0, 0, 1e-4, 0, 1e-4]]),
                                     np.array([.9]), camera)
    assert projected["u"][0] == pytest.approx(45)
    assert projected["v"][0] == pytest.approx(20)
    assert projected["z"][0] == pytest.approx(3)


def test_distorted_calibration_rejected_and_limits_enforced(monkeypatch):
    image = ModelImage(1, 1, "view", (1, 0, 0, 0), (0, 0, 0))
    distorted = ModelCamera(1, "SIMPLE_RADIAL", 100, 100, (100, 50, 50, .1))
    with pytest.raises(ValueError, match="undistorted"):
        splat_depth._camera(image, distorted, 100)
    camera = ModelCamera(1, "PINHOLE", 100, 100, (100, 100, 50, 50))
    args = (np.zeros((30, 3)) + [0, 0, 3], np.full((30, 3), .1), np.tile([1, 0, 0, 0], (30, 1)),
            np.ones(30), {1: camera}, {1: image})
    bounds = (np.full(3, -5.), np.full(3, 5.))
    with pytest.raises(ValueError, match="render_size"):
        splat_depth.fuse_splat_depth(*args, bounds=bounds, render_size=splat_depth.MAX_RENDER_SIZE + 1)
    with pytest.raises(ValueError, match="time_budget"):
        splat_depth.fuse_splat_depth(*args, bounds=bounds, time_budget=splat_depth.TIME_BUDGET_SECONDS + 1)
    with pytest.raises(ValueError, match="bounds"):
        splat_depth.fuse_splat_depth(*args, bounds=(bounds[1], bounds[0]))
    clock = iter([0.] + [1e6] * 1000)
    monkeypatch.setattr(splat_depth, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    with pytest.raises(TimeoutError, match="time budget"):
        splat_depth.fuse_splat_depth(*args, bounds=bounds, time_budget=1.)
