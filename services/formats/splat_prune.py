"""Prune a trained Gaussian scene down to its subject before any surface extraction.

A phone-video splat is mostly not the subject: the floor, the room beyond the camera
orbit, view-ray streaks, and low-opacity haze that only looks solid after alpha
compositing.  Every Gaussian center is a position and nothing more, so meshing the raw
cloud (or a single global opacity cut) meshes that noise.  ``prune_gaussians`` removes it
in fixed stages and reports what each one dropped:

1. opacity   - adaptive, because optimizers with opacity regularisation (Brush) end with
               a median opacity of 0.05-0.2, where a fixed 0.5 cut keeps only 1-8%.
2. region    - inside the sphere the registered cameras orbit (when poses are known),
               otherwise inside robust percentile bounds.
3. shape     - drop splats far larger than their neighbours and needle-like view-ray
               streaks; neither is surface.
4. floor     - remove the dominant supporting plane (RANSAC) when the scene has one.
5. floaters  - statistical k-NN outlier removal, then drop small disconnected islands.

All stages are deterministic.  Coordinates and units are those of the input cloud.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

AUTO_MIN_OPACITY = 0.05          # Floor of the adaptive opacity cut.
AUTO_MAX_OPACITY = 0.30          # Ceiling: never cut deeper than this automatically.
MAX_FLOOR_CANDIDATES = 200_000   # RANSAC / k-NN work is bounded on huge scenes.
_RANSAC_ROUNDS = 400
_KNN = 16
_MIN_FLOOR_FRACTION = 0.25
_FLAT_COS = math.cos(math.radians(30.0))  # Thin axis within 30 degrees of the floor normal.


@dataclass
class PruneOptions:
    min_opacity: float | None = None  # None: adaptive.
    focus: np.ndarray | None = None   # Orbit center; with ``radius`` enables the region stage.
    radius: float | None = None
    max_scale_ratio: float = 6.0      # max-scale vs. the median of the surviving splats.
    max_aspect: float = 10.0          # largest/middle axis: above this a splat is a needle.
    remove_floor: bool = True
    outlier_std: float = 2.0
    island_fraction: float = 0.03     # keep islands >= this fraction of the largest one.


@dataclass
class PruneReport:
    input_count: int
    stages: list[dict] = field(default_factory=list)
    min_opacity: float = 0.0
    floor: dict | None = None

    @property
    def kept(self) -> int:
        return self.stages[-1]["kept"] if self.stages else self.input_count

    def as_dict(self) -> dict:
        return {"input": self.input_count, "kept": self.kept, "min_opacity": self.min_opacity,
                "stages": self.stages, "floor": self.floor}


def orbit_focus(images) -> tuple[np.ndarray, float]:
    """Point nearest every optical axis, and the median camera distance to it.

    ``images`` are COLMAP ``ModelImage`` records (world-to-camera ``qvec``/``tvec``).
    A capture that orbits its subject looks at it from every side, so this is the
    subject center without any segmentation.
    """
    from services.vision.splat_depth import _rotation_matrices
    registered = list(images.values()) if hasattr(images, "values") else list(images)
    if not registered:
        raise ValueError("no registered camera poses")
    rot = np.array([_rotation_matrices(i.qvec) for i in registered], dtype=np.float64)
    trans = np.array([i.tvec for i in registered], dtype=np.float64)
    centers = -np.einsum("nji,nj->ni", rot, trans)
    axes = rot[:, 2, :]
    lhs, rhs = np.zeros((3, 3)), np.zeros(3)
    for center, axis in zip(centers, axes):
        proj = np.eye(3) - np.outer(axis, axis)
        lhs += proj
        rhs += proj @ center
    if np.linalg.cond(lhs) > 1e8:  # Parallel axes: a straight-line pass has no focus.
        raise ValueError("camera optical axes do not converge on a subject")
    focus = np.linalg.solve(lhs, rhs)
    radius = float(np.median(np.linalg.norm(centers - focus, axis=1)))
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("camera orbit has no usable radius")
    return focus, radius


def _record(report, name, keep, before):
    report.stages.append({"stage": name, "removed": int(before - keep.sum()), "kept": int(keep.sum())})


def _auto_opacity(opacity: np.ndarray) -> float:
    """Cut at half the median opacity, bounded; keeps the denser half of a low-opacity cloud."""
    return float(np.clip(0.5 * np.median(opacity), AUTO_MIN_OPACITY, AUTO_MAX_OPACITY))


def _fit_floor(points: np.ndarray, extent: float, seed: int = 0):
    """RANSAC plane, refined by SVD. Returns (normal, offset, inlier mask) or None."""
    n = len(points)
    if n < 100:
        return None
    rng = np.random.default_rng(seed)
    tol = 0.01 * extent
    best, best_count = None, 0
    for _ in range(_RANSAC_ROUNDS):
        a, b, c = points[rng.choice(n, 3, replace=False)]
        normal = np.cross(b - a, c - a)
        norm = np.linalg.norm(normal)
        if norm < 1e-12:
            continue
        normal /= norm
        count = int(np.count_nonzero(np.abs((points - a) @ normal) < tol))
        if count > best_count:
            best, best_count = (normal, float(normal @ a)), count
    if best is None:
        return None
    normal, offset = best
    inliers = np.abs(points @ normal - offset) < tol
    for _ in range(2):  # Refit on inliers, then re-select inliers on the refit plane.
        sample = points[inliers]
        centroid = sample.mean(axis=0)
        normal = np.linalg.svd(sample - centroid, full_matrices=False)[2][-1]
        offset = float(normal @ centroid)
        inliers = np.abs(points @ normal - offset) < tol
    return normal, offset, inliers


def _knn_distance(points: np.ndarray, k: int) -> np.ndarray:
    from scipy.spatial import cKDTree
    k = min(k, len(points) - 1)
    distances, _ = cKDTree(points).query(points, k=k + 1, workers=-1)
    return distances[:, 1:].mean(axis=1)


def _islands(points: np.ndarray, spacing: float, fraction: float) -> np.ndarray:
    """Keep voxel-connected islands that are at least ``fraction`` of the largest."""
    import scipy.ndimage as ndi
    cells = np.floor((points - points.min(axis=0)) / spacing).astype(np.int64)
    dims = cells.max(axis=0) + 3
    if int(np.prod(dims, dtype=np.float64)) > 200_000_000:
        return np.ones(len(points), dtype=bool)
    grid = np.zeros(dims, dtype=bool)
    grid[tuple((cells + 1).T)] = True
    labels, count = ndi.label(grid, structure=np.ones((3, 3, 3), dtype=bool))
    if count <= 1:
        return np.ones(len(points), dtype=bool)
    per_point = labels[tuple((cells + 1).T)]
    sizes = np.bincount(per_point, minlength=count + 1)
    sizes[0] = 0
    return sizes[per_point] >= fraction * sizes.max()


def prune_gaussians(cloud, options: PruneOptions | None = None):
    """Return ``(keep_mask, PruneReport)`` for a validated ``GaussianCloud``."""
    opt = options or PruneOptions()
    means = np.asarray(cloud.means, dtype=np.float64)
    opacity = np.asarray(cloud.opacities, dtype=np.float64)
    scales = np.asarray(cloud.scales, dtype=np.float64)
    n = len(means)
    report = PruneReport(input_count=n)
    keep = np.ones(n, dtype=bool)

    # 1. opacity
    if opt.min_opacity is None:
        threshold = _auto_opacity(opacity)
    else:
        threshold = float(opt.min_opacity)
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("min_opacity must be between 0 and 1")
    report.min_opacity = threshold
    before = n
    keep &= opacity >= threshold
    _record(report, "opacity", keep, before)
    if not keep.any():
        raise ValueError("no Gaussian passes the opacity cut")

    # 2. region
    before = int(keep.sum())
    if opt.focus is not None and opt.radius:
        keep &= np.linalg.norm(means - np.asarray(opt.focus, dtype=np.float64), axis=1) < opt.radius
    else:
        lo, hi = np.percentile(means[keep], [1, 99], axis=0)
        pad = 0.25 * (hi - lo)
        keep &= np.all((means >= lo - pad) & (means <= hi + pad), axis=1)
    _record(report, "region", keep, before)
    if not keep.any():
        raise ValueError("no Gaussian lies inside the subject region")
    extent = float(np.max(np.ptp(means[keep], axis=0)))

    # 3. shape
    before = int(keep.sum())
    ordered = np.sort(scales, axis=1)
    biggest, middle = ordered[:, 2], ordered[:, 1]
    median = float(np.median(biggest[keep]))
    keep &= (biggest <= opt.max_scale_ratio * median) & (biggest <= opt.max_aspect * middle)
    _record(report, "shape", keep, before)
    if not keep.any():
        raise ValueError("no Gaussian passes the shape filter")

    # 4. floor
    if opt.remove_floor:
        before = int(keep.sum())
        idx = np.flatnonzero(keep)
        sample = idx if len(idx) <= MAX_FLOOR_CANDIDATES else np.random.default_rng(0).choice(
            idx, MAX_FLOOR_CANDIDATES, replace=False)
        fit = _fit_floor(means[sample], extent)
        if fit is not None and fit[2].mean() >= _MIN_FLOOR_FRACTION:
            normal, offset, inliers = fit
            height = means @ normal - offset
            spread = float(np.median(np.abs(height[sample][inliers])))
            band = max(4.0 * spread, 0.01 * extent)
            # The subject is on the side with more material off the plane.
            if np.count_nonzero(height[idx] < -band) > np.count_nonzero(height[idx] > band):
                normal, offset, height = -normal, -offset, -height
            # A subject lying on the floor dips into the floor band, so inside the band
            # only floor goes: splats lying flat on the plane (thin axis along the normal,
            # which also removes a flat printed pattern) and splats colored like the floor.
            # Upright, differently colored subject surfaces stay.
            from services.vision.splat_depth import _rotation_matrices
            in_band = np.flatnonzero(keep & (np.abs(height) <= band))
            rotation = _rotation_matrices(np.asarray(cloud.rotations, dtype=np.float64)[in_band])
            thin_axis = rotation[np.arange(len(in_band)), :, np.argmin(scales[in_band], axis=1)]
            flat = np.abs(thin_axis @ normal) >= _FLAT_COS
            color = np.asarray(cloud.sh_dc, dtype=np.float64)
            floor_color = np.median(color[sample][inliers], axis=0)
            distance = np.linalg.norm(color - floor_color, axis=1)
            limit = 1.5 * float(np.percentile(distance[sample][inliers], 90))
            keep[in_band[flat | (distance[in_band] <= limit)]] = False
            keep &= height >= -band
            report.floor = {"normal": normal.tolist(), "offset": offset, "band": band,
                            "inlier_fraction": float(inliers.mean()), "color_limit": limit}
        _record(report, "floor", keep, before)
        if not keep.any():
            raise ValueError("nothing remains above the detected floor; pass remove_floor=false")

    # 5. floaters
    before = int(keep.sum())
    idx = np.flatnonzero(keep)
    if len(idx) > _KNN + 1:
        pts = means[idx]
        mean_nn = _knn_distance(pts, _KNN)
        good = mean_nn <= mean_nn.mean() + opt.outlier_std * mean_nn.std()
        keep[idx[~good]] = False
        idx = np.flatnonzero(keep)
        if len(idx) > _KNN + 1:
            spacing = 3.0 * float(np.median(_knn_distance(means[idx], 1)))
            if spacing > 0:
                keep[idx[~_islands(means[idx], spacing, opt.island_fraction)]] = False
    _record(report, "floaters", keep, before)
    if not keep.any():
        raise ValueError("no Gaussian survives floater removal")
    return keep, report
