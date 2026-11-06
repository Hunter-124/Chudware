"""Bounded, deterministic triangle geometry in millimetres, without spatial extras.

Build coordinates use a proper rotation and a bed translation only. Triangle
indices and input winding are preserved, including normals facing cavity air.
Peels are centre-sampled Z crossings, sorted by XY cell then height; a half-open
projected-edge rule avoids double counting tessellation seams. Coincident sheets
share one public hit/provenance, while private signed multiplicity preserves
union winding for overlapping solids. Voxels are centre occupancy (not surface
coverage), with at least one full air cell around every side. Raster workspaces
are capped without constructing a triangle-by-sample matrix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import numpy as np
from numpy.typing import ArrayLike
from .mesh import PrintMesh

from .params import frame_for_build_dir

_PAIR_LIMIT = 131072
_HIT_EPS = 1e-6


class BuildGeometry:
    def __init__(self, mesh: PrintMesh, build_dir: ArrayLike = (0, 0, 1), yaw_deg: float = 0.0) -> None:
        self.mesh = mesh
        self.rotation = frame_for_build_dir(build_dir, yaw_deg)
        self.vertices = mesh.vertices @ self.rotation.T
        self.z_min = float(self.vertices[:, 2].min())
        self.vertices[:, 2] -= self.z_min
        self.triangles = self.vertices[mesh.triangles]
        cross = np.cross(self.triangles[:, 1] - self.triangles[:, 0],
                         self.triangles[:, 2] - self.triangles[:, 0])
        lengths = np.linalg.norm(cross, axis=1)
        self.areas = lengths * 0.5
        self.normals = np.divide(cross, lengths[:, None], out=np.zeros_like(cross),
                                 where=lengths[:, None] > 0)
        self.centroids = self.triangles.mean(axis=1)
        self.bounds = np.stack((self.vertices.min(axis=0), self.vertices.max(axis=0)))

    def model_points(self, points: ArrayLike) -> np.ndarray:
        return (np.asarray(points, dtype=float) + [0., 0., self.z_min]) @ self.rotation


def _pitch_shape(extent, cell, max_cells, padding=0):
    if not math.isfinite(cell) or cell <= 0:
        raise ValueError("cell must be positive and finite")
    if int(max_cells) != max_cells or max_cells < (1 + padding) ** len(extent):
        raise ValueError("max_cells is too small for the padded grid")
    # A bounded grid cannot need more than max_cells cells along one axis.
    # Clamp before float-to-int conversion, including subnormal requested pitches.
    cell = max(float(cell), float(np.max(extent)) / max_cells)

    def shape_at(pitch):
        return np.maximum(1, np.ceil(extent / pitch).astype(np.int64)) + padding

    shape = shape_at(cell)
    if math.prod(map(int, shape)) <= max_cells:
        return float(cell), shape
    lower, upper = cell, max(cell, float(np.max(extent)))
    while math.prod(map(int, shape_at(upper))) > max_cells:
        upper *= 2
    for _ in range(50):
        middle = (lower + upper) * .5
        if math.prod(map(int, shape_at(middle))) > max_cells:
            lower = middle
        else:
            upper = middle
    return upper, shape_at(upper)


def _box_pairs(lower, upper, shape):
    """Yield bounded (item, flattened cell) batches for inclusive integer boxes."""
    widths = np.maximum(upper - lower + 1, 0)
    counts = np.prod(widths, axis=1, dtype=np.int64)
    ends = np.cumsum(counts)
    starts = ends - counts
    total = int(ends[-1]) if len(ends) else 0
    for start in range(0, total, _PAIR_LIMIT):
        offsets = np.arange(start, min(total, start + _PAIR_LIMIT), dtype=np.int64)
        item = np.searchsorted(ends, offsets, side="right")
        local = offsets - starts[item]
        flat = np.zeros(len(item), dtype=np.int64)
        stride = 1
        for axis in range(len(shape) - 1, -1, -1):
            coordinate = local % widths[item, axis] + lower[item, axis]
            local //= widths[item, axis]
            flat += coordinate * stride
            stride *= int(shape[axis])
        yield item, flat


@dataclass
class DepthPeel:
    """Public outward signs; private multiplicity is entering-positive.

    ``_active_surface`` marks zero/positive union-winding transitions, allowing
    support consumers to ignore surfaces buried inside overlapping solid bodies.
    """
    cell: float
    shape: tuple[int, int]
    origin: np.ndarray
    cells: np.ndarray
    z: np.ndarray
    tri: np.ndarray
    sign: np.ndarray
    _crossing_winding: np.ndarray = field(repr=False)
    _active_surface: np.ndarray = field(repr=False)

    def xy_for_cells(self, ids: ArrayLike) -> np.ndarray:
        ids = np.asarray(ids, dtype=np.int64)
        return self.origin + (np.stack((ids // self.shape[1], ids % self.shape[1]), axis=-1) + .5) * self.cell


def _peel(geom, cell, shape, origin):
    triangles = geom.triangles
    # Make the projected triangles CCW, independently of the outward Z sign.
    active = np.flatnonzero(np.abs(geom.normals[:, 2]) > 1e-12)
    projected = triangles[active].copy()
    flip = geom.normals[active, 2] < 0
    projected[flip] = projected[flip][:, [0, 2, 1]]
    lower = np.ceil((projected[:, :, :2].min(axis=1) - origin) / cell - .5).astype(np.int64)
    upper = np.floor((projected[:, :, :2].max(axis=1) - origin) / cell - .5).astype(np.int64)
    lower = np.maximum(lower, 0)
    upper = np.minimum(upper, np.asarray(shape) - 1)
    all_cells, all_z, all_tri = [], [], []
    for item, cells in _box_pairs(lower, upper, shape):
        xy = origin + (np.stack((cells // shape[1], cells % shape[1]), axis=1) + .5) * cell
        t = projected[item]
        inside = np.ones(len(item), dtype=bool)
        edge_values = []
        for a, b in ((1, 2), (2, 0), (0, 1)):
            edge = t[:, b, :2] - t[:, a, :2]
            delta = xy - t[:, a, :2]
            value = edge[:, 0] * delta[:, 1] - edge[:, 1] * delta[:, 0]
            tolerance = 1e-12 * np.maximum(1., np.linalg.norm(edge, axis=1) * cell)
            top_left = (edge[:, 1] > 0) | ((edge[:, 1] == 0) & (edge[:, 0] < 0))
            inside &= (value > tolerance) | ((np.abs(value) <= tolerance) & top_left)
            edge_values.append(value)
        if not inside.any():
            continue
        area = sum(edge_values)
        z = sum(edge_values[a][inside] * t[inside, a, 2] for a in range(3)) / area[inside]
        all_cells.append(cells[inside])
        all_z.append(z)
        all_tri.append(active[item[inside]])
    if not all_cells:
        empty = np.empty(0, dtype=np.int64)
        return DepthPeel(cell, tuple(map(int, shape)), np.asarray(origin), empty,
                         np.empty(0), empty.copy(), np.empty(0, dtype=np.int8), empty.copy(),
                         np.empty(0, dtype=bool))
    cells, z, tri = np.concatenate(all_cells), np.concatenate(all_z), np.concatenate(all_tri)
    order = np.lexsort((tri, z, cells))
    cells, z, tri = cells[order], z[order], tri[order]
    signs = np.sign(geom.normals[tri, 2]).astype(np.int32)
    new = np.r_[True, (cells[1:] != cells[:-1]) | (np.diff(z) > _HIT_EPS)]
    starts = np.flatnonzero(new)
    winding = np.add.reduceat(signs, starts)
    # Pick the lowest triangle id among sheets with the surviving outward sign.
    group = np.cumsum(new) - 1
    eligible = signs == np.sign(winding[group])
    representatives = np.minimum.reduceat(np.where(eligible, tri, len(triangles)), starts)
    keep = winding != 0
    crossing_cells = cells[starts][keep]
    entering = -winding[keep]
    column_starts = np.flatnonzero(np.r_[True, np.diff(crossing_cells) != 0]) if len(entering) else np.empty(0, dtype=np.int64)
    cumulative = np.cumsum(entering)
    lengths = np.diff(np.r_[column_starts, len(entering)])
    baseline = np.repeat(cumulative[column_starts] - entering[column_starts], lengths)
    after = cumulative - baseline
    active_surface = (after > 0) != (after - entering > 0)
    return DepthPeel(cell, tuple(map(int, shape)), np.asarray(origin), crossing_cells,
                     z[starts][keep], representatives[keep], np.sign(winding[keep]).astype(np.int8),
                     entering, active_surface)


def depth_peel(geom: BuildGeometry, cell: float = .5, max_cells: int = 1500000) -> DepthPeel:
    cell, shape = _pitch_shape(geom.bounds[1, :2] - geom.bounds[0, :2], cell, max_cells)
    return _peel(geom, cell, shape, geom.bounds[0, :2])


@dataclass
class Voxels:
    solid: np.ndarray
    cell: float
    origin: np.ndarray

    def centers(self, indices: ArrayLike) -> np.ndarray:
        return self.origin + (np.asarray(indices, dtype=float) + .5) * self.cell


def voxelize(geom: BuildGeometry, cell: float | None = None, max_cells: int = 3000000) -> Voxels:
    extent = geom.bounds[1] - geom.bounds[0]
    if cell is None:
        cell = max(.5, (float(np.prod(extent)) / 3000000) ** (1 / 3))
    cell, shape = _pitch_shape(extent, cell, max_cells, padding=2)
    origin = geom.bounds[0] - cell
    peel = _peel(geom, cell, shape[:2], origin[:2])
    nz = int(shape[2])
    changes = np.zeros((int(shape[0]) * int(shape[1]), nz + 1), dtype=np.int32)
    # An event affects the first centre at or above its height.
    iz = np.clip(np.ceil((peel.z - origin[2]) / cell - .5).astype(np.int64), 0, nz)
    np.add.at(changes.ravel(), peel.cells * (nz + 1) + iz, peel._crossing_winding)
    solid = (np.cumsum(changes[:, :nz], axis=1, dtype=np.int32) > 0).reshape(tuple(shape))
    # Occupancy cannot extend beyond the known mesh bounding box, even if an
    # open/inconsistently wound column fails to return to zero winding.
    solid[0, :, :] = solid[-1, :, :] = False
    solid[:, 0, :] = solid[:, -1, :] = False
    solid[:, :, 0] = solid[:, :, -1] = False
    return Voxels(solid, cell, origin)


def fit_cylinder(points: ArrayLike, normals: ArrayLike) -> dict | None:
    """Fit a cylindrical side patch; reject planes, cones and ill-conditioned arcs.

    Residual is RMS radial distance in mm. Cylinder axis sign is canonical and
    centre is the fitted axis point at the samples' mean axial coordinate.
    """
    points, normals = np.asarray(points, dtype=float), np.asarray(normals, dtype=float)
    if points.ndim != 2 or points.shape[1:] != (3,) or normals.shape != points.shape or len(points) < 6:
        return None
    if not np.isfinite(points).all() or not np.isfinite(normals).all():
        return None
    lengths = np.linalg.norm(normals, axis=1)
    if np.any(lengths < 1e-12):
        return None
    normals = normals / lengths[:, None]
    values, vectors = np.linalg.eigh(normals.T @ normals / len(normals))
    if values[1] < .005 or values[0] > .0025:
        return None
    axis = vectors[:, 0]
    if axis[np.argmax(np.abs(axis))] < 0:
        axis = -axis
    reference = np.eye(3)[np.argmin(np.abs(axis))]
    u = np.cross(axis, reference)
    u /= np.linalg.norm(u)
    v = np.cross(axis, u)
    mean = points.mean(axis=0)
    xy = (points - mean) @ np.stack((u, v), axis=1)
    matrix = np.column_stack((2 * xy, np.ones(len(xy))))
    solution, _, rank, singular = np.linalg.lstsq(matrix, np.sum(xy * xy, axis=1), rcond=None)
    if rank < 3 or singular[-1] < singular[0] * 1e-6:
        return None
    radial = xy - solution[:2]
    radii = np.linalg.norm(radial, axis=1)
    radius = float(radii.mean())
    residual = float(np.sqrt(np.mean((radii - radius) ** 2)))
    if radius < 1e-8 or residual > max(1e-5, radius * .02):
        return None
    radial_normals = radial @ np.stack((u, v)) / radii[:, None]
    alignment = np.einsum("ij,ij->i", radial_normals, normals)
    if np.mean(np.abs(alignment)) < .98 or np.min(np.abs(alignment)) < .9:
        return None
    axial = (points - mean) @ axis
    if np.ptp(axial) > radius * .05:
        slope = np.dot(axial, radii - radius) / max(np.dot(axial, axial), 1e-30)
        if abs(slope) * np.ptp(axial) > max(1e-5, radius * .01):
            return None
    return {"axis": axis, "center": mean + solution[0] * u + solution[1] * v,
            "radius": radius, "residual": residual, "concave": bool(alignment.mean() < 0)}


def surface_samples(geom: BuildGeometry, max_samples: int = 20000) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Area-stratified deterministic barycentric samples with area quadrature weights.

    When the budget permits, every nondegenerate triangle receives its centroid;
    remaining samples follow largest-remainder area allocation. Below that budget
    systematic area bins carry their exact bin area, without mesh-size bias.
    Points and outward normals are in the build frame; these query samples are
    independent of the native inscribed-sphere blocker's refinement spacing.
    """
    if int(max_samples) != max_samples or max_samples < 1:
        raise ValueError("max_samples must be a positive integer")
    ids = np.flatnonzero(geom.areas > 0)
    total_area = float(geom.areas[ids].sum())
    if len(ids) > max_samples:
        ends = np.cumsum(geom.areas[ids])
        tri = ids[np.searchsorted(ends, (np.arange(max_samples) + .5) * total_area / max_samples)]
        counts = np.bincount(tri, minlength=len(geom.triangles))
        weights = np.full(max_samples, total_area / max_samples)
    else:
        quota = geom.areas[ids] / total_area * (max_samples - len(ids))
        counts = np.zeros(len(geom.triangles), dtype=np.int64)
        allocation = np.floor(quota).astype(np.int64) + 1
        remainder = max_samples - int(allocation.sum())
        order = np.lexsort((ids, -(quota - np.floor(quota))))
        allocation[order[:remainder]] += 1
        counts[ids] = allocation
        tri = np.repeat(ids, allocation)
        weights = geom.areas[tri] / counts[tri]
    starts = np.r_[0, np.flatnonzero(np.diff(tri)) + 1]
    local = np.arange(len(tri)) - np.repeat(starts, np.diff(np.r_[starts, len(tri)]))
    # Centroid first, then a low-discrepancy interior sequence (no random state).
    s = np.sqrt(np.mod((local + 1) * .7548776662466927, 1.))
    t = np.mod((local + 1) * .5698402909980532, 1.)
    bary = np.column_stack((1 - s, s * (1 - t), s * t))
    bary[local == 0] = 1 / 3
    points = np.einsum("ij,ijk->ik", bary, geom.triangles[tri])
    return points, geom.normals[tri], tri, weights
