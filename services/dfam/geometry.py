"""Bounded, deterministic triangle geometry in millimetres, without spatial extras.

Build coordinates use a proper rotation and a bed translation only. Triangle
indices and input winding are preserved, including normals facing cavity air.
Peels are centre-sampled Z crossings, sorted by XY cell then height; a half-open
projected-edge rule avoids double counting tessellation seams. Coincident sheets
share one public hit/provenance, while private signed multiplicity preserves
union winding for overlapping solids. Voxels are centre occupancy (not surface
coverage), with at least one full air cell around every side. Raster/ray pair
workspaces are capped; neither routine constructs a triangle-by-ray matrix.
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


class RayCaster:
    """Uniform hashed grid + batched DDA and Moller--Trumbore intersections.

    Grid references are limited to max(three million, triangle count) by increasing
    the requested pitch when necessary. Cast returns the nearest positive-distance hit, with
    lowest triangle id resolving coincident edges/sheets within 1e-6 mm. Directions
    are normalized so max_distance and returned distances are always mm.
    """
    def __init__(self, geom: BuildGeometry, cell: float) -> None:
        if not math.isfinite(cell) or cell <= 0:
            raise ValueError("cell must be positive and finite")
        self.geom = geom
        self.origin = geom.bounds[0].copy()
        extent = geom.bounds[1] - self.origin
        minimum = geom.triangles.min(axis=1)
        maximum = geom.triangles.max(axis=1)
        # Keep virtual flattened grid ids within int64 even for tiny pitches.
        self.cell = max(float(cell), float(np.max(extent)) / (2 ** 20 - 1))
        while True:
            self.shape = np.maximum(1, np.floor(extent / self.cell).astype(np.int64) + 1)
            lower = np.floor((minimum - self.origin) / self.cell).astype(np.int64)
            upper = np.minimum(np.floor((maximum - self.origin) / self.cell).astype(np.int64), self.shape - 1)
            references = sum(map(int, np.prod(upper - lower + 1, axis=1, dtype=np.int64)))
            if references <= max(3000000, len(geom.triangles)):
                break
            self.cell *= max(1.25, (references / 3000000) ** (1 / 3))
        items, cells = [], []
        for item, flat in _box_pairs(lower, upper, self.shape):
            items.append(item)
            cells.append(flat)
        cells, items = np.concatenate(cells), np.concatenate(items)
        order = np.argsort(cells, kind="stable")
        cells = cells[order]
        self.items = items[order]
        self.keys, self.starts, self.counts = np.unique(cells, return_index=True, return_counts=True)
        self.v0 = geom.triangles[:, 0]
        self.e1 = geom.triangles[:, 1] - self.v0
        self.e2 = geom.triangles[:, 2] - self.v0
        self.det_scale = np.linalg.norm(self.e1, axis=1) * np.linalg.norm(self.e2, axis=1)

    def _intersections(self, ray, tri, origins, directions, limits, opposing):
        d = directions[ray]
        p = np.cross(d, self.e2[tri])
        det = np.einsum("ij,ij->i", self.e1[tri], p)
        good = np.abs(det) > 1e-12 * self.det_scale[tri]
        inverse = np.divide(1., det, out=np.zeros_like(det), where=good)
        delta = origins[ray] - self.v0[tri]
        u = np.einsum("ij,ij->i", delta, p) * inverse
        q = np.cross(delta, self.e1[tri])
        v = np.einsum("ij,ij->i", d, q) * inverse
        distance = np.einsum("ij,ij->i", self.e2[tri], q) * inverse
        good &= (u >= -1e-10) & (v >= -1e-10) & (u + v <= 1 + 1e-10)
        good &= (distance > 1e-9) & (distance <= limits[ray] + 1e-9)
        if opposing is not None:
            good &= np.einsum("ij,ij->i", self.geom.normals[tri], opposing[ray]) < 0
        return ray[good], tri[good], distance[good]

    def cast(self, origins: ArrayLike, directions: ArrayLike, max_distance: float | np.ndarray, *,
             opposing_normals: ArrayLike | None = None) -> tuple[np.ndarray, np.ndarray]:
        origins, directions = np.asarray(origins, dtype=float), np.asarray(directions, dtype=float)
        if origins.ndim != 2 or origins.shape[1:] != (3,) or directions.shape != origins.shape:
            raise ValueError("origins and directions must be matching (N,3) arrays")
        lengths = np.linalg.norm(directions, axis=1)
        if not np.isfinite(origins).all() or not np.isfinite(directions).all() or np.any(lengths < 1e-15):
            raise ValueError("rays must be finite with nonzero directions")
        directions = directions / lengths[:, None]
        limits = np.broadcast_to(np.asarray(max_distance, dtype=float), (len(origins),))
        if np.any(np.isnan(limits)) or np.any(limits < 0):
            raise ValueError("max_distance must be nonnegative")
        opposing = None if opposing_normals is None else np.asarray(opposing_normals, dtype=float)
        if opposing is not None and (opposing.shape != origins.shape or not np.isfinite(opposing).all()):
            raise ValueError("opposing_normals must be a finite (N,3) array")
        distances = np.full(len(origins), np.inf)
        triangles = np.full(len(origins), -1, dtype=np.int64)
        inverse = np.divide(1., directions, out=np.full_like(directions, np.inf), where=directions != 0)
        lower, upper = self.geom.bounds
        moving = directions != 0
        near = np.minimum(np.where(moving, (lower - origins) * np.where(moving, inverse, 0), -np.inf),
                          np.where(moving, (upper - origins) * np.where(moving, inverse, 0), np.inf))
        far = np.maximum(np.where(moving, (lower - origins) * np.where(moving, inverse, 0), -np.inf),
                         np.where(moving, (upper - origins) * np.where(moving, inverse, 0), np.inf))
        outside = np.any(~moving & ((origins < lower) | (origins > upper)), axis=1)
        entry = np.maximum(0., np.max(near, axis=1))
        exit_ = np.minimum(limits, np.min(far, axis=1))
        active = np.flatnonzero(~outside & (entry <= exit_))
        if not len(active):
            return distances, triangles
        position = origins + directions * entry[:, None]
        coordinate = np.clip(np.floor((position - self.origin) / self.cell).astype(np.int64), 0, self.shape - 1)
        step = np.sign(directions).astype(np.int64)
        boundary = self.origin + (coordinate + (step > 0)) * self.cell
        next_t = np.divide(boundary - origins, directions, out=np.full_like(origins, np.inf), where=moving)
        delta_t = self.cell * np.abs(inverse)
        while len(active):
            c = coordinate[active]
            flat = (c[:, 0] * self.shape[1] + c[:, 1]) * self.shape[2] + c[:, 2]
            key = np.searchsorted(self.keys, flat)
            found = key < len(self.keys)
            found[found] &= self.keys[key[found]] == flat[found]
            rays = active[found]
            key = key[found]
            counts = self.counts[key]
            ends = np.cumsum(counts)
            starts = ends - counts
            total = int(ends[-1]) if len(ends) else 0
            for begin in range(0, total, _PAIR_LIMIT):
                offsets = np.arange(begin, min(total, begin + _PAIR_LIMIT))
                item = np.searchsorted(ends, offsets, side="right")
                ray = rays[item]
                tri = self.items[self.starts[key[item]] + offsets - starts[item]]
                ray, tri, distance = self._intersections(ray, tri, origins, directions, limits, opposing)
                if not len(ray):
                    continue
                order = np.lexsort((tri, distance, ray))
                ray, tri, distance = ray[order], tri[order], distance[order]
                first = np.r_[True, ray[1:] != ray[:-1]]
                group_starts = np.flatnonzero(first)
                group_lengths = np.diff(np.r_[group_starts, len(ray)])
                minimum = np.repeat(distance[first], group_lengths)
                eligible = distance <= minimum + _HIT_EPS
                chosen = np.minimum.reduceat(np.where(eligible, tri, len(self.v0)), group_starts)
                chosen_by_pair = np.repeat(chosen, group_lengths)
                positions = np.minimum.reduceat(
                    np.where(eligible & (tri == chosen_by_pair), np.arange(len(ray)), len(ray)),
                    group_starts)
                ray, tri, distance = ray[first], chosen, distance[positions]
                better = (distance < distances[ray] - _HIT_EPS) | ((np.abs(distance - distances[ray]) <= _HIT_EPS) & ((triangles[ray] < 0) | (tri < triangles[ray])))
                distances[ray[better]], triangles[ray[better]] = distance[better], tri[better]
            next_entry = np.min(next_t[active], axis=1)
            continuing = next_entry <= np.minimum(exit_[active], distances[active] + _HIT_EPS)
            active = active[continuing]
            next_entry = next_entry[continuing]
            if not len(active):
                break
            axes = next_t[active] <= next_entry[:, None] + 1e-12
            coordinate[active] += axes * step[active]
            next_t[active] += np.where(axes, delta_t[active], 0)
            valid = np.all((coordinate[active] >= 0) & (coordinate[active] < self.shape), axis=1)
            active = active[valid]
        return distances, triangles
