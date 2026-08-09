"""Seeded synthetic camera model with an explicit information boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from services.viewpoint.geometry import CameraPose, Scene, project_points, unit

from .model import (
    CorruptionConfig,
    DepthOnlyObservation,
    DepthPlusPriorObservation,
    FeatureRegion2D,
    FullObservation,
    INFORMATION_LEVELS,
    InformationLevel,
    Observation,
    ObservationConfig,
    SparsePointsObservation,
    VisitRecord,
    WorkspaceBounds,
)


@dataclass(frozen=True, slots=True)
class _Frame:
    depth: np.ndarray
    normals: np.ndarray
    points: np.ndarray
    point_normals: np.ndarray
    sparse_indices: np.ndarray
    effective_camera: CameraPose


def _rotate(vector: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    return vector * np.cos(angle) + np.cross(axis, vector) * np.sin(angle) + axis * float(np.dot(axis, vector)) * (1.0 - np.cos(angle))


def _effective_camera(camera: CameraPose, corruption: CorruptionConfig, rng: np.random.Generator) -> CameraPose:
    translation = rng.normal(0.0, corruption.calibration_position_std, size=3)
    forward = camera.forward
    up = camera.true_up
    angle = np.deg2rad(rng.normal(0.0, corruption.calibration_angle_std_deg))
    if angle != 0.0:
        axis = rng.normal(size=3)
        axis_length = float(np.linalg.norm(axis))
        if axis_length <= 1e-12:
            axis = camera.right
        else:
            axis /= axis_length
        forward = unit(_rotate(forward, axis, angle), name="calibrated forward")
        up = unit(_rotate(up, axis, angle), name="calibrated up")
    position = camera.position + translation
    distance = float(np.linalg.norm(camera.target - camera.position))
    return CameraPose(
        position=position,
        target=position + distance * forward,
        up=up,
        vertical_fov_deg=camera.vertical_fov_deg,
        image_width_px=camera.image_width_px,
        image_height_px=camera.image_height_px,
        near=camera.near,
        far=camera.far,
    )


def _pixel_rays(camera: CameraPose, config: ObservationConfig) -> np.ndarray:
    rows, columns = np.indices((config.depth_height, config.depth_width), dtype=np.float64)
    ndc_x = 2.0 * (columns + 0.5) / config.depth_width - 1.0
    ndc_y = 1.0 - 2.0 * (rows + 0.5) / config.depth_height
    tan_y = np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5)
    tan_x = tan_y * camera.image_width_px / camera.image_height_px
    rays = (
        camera.forward[None, None, :]
        + ndc_x[:, :, None] * tan_x * camera.right[None, None, :]
        + ndc_y[:, :, None] * tan_y * camera.true_up[None, None, :]
    )
    rays /= np.linalg.norm(rays, axis=2, keepdims=True)
    return rays


def _raycast(scene: Scene, camera: CameraPose, config: ObservationConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rays = _pixel_rays(camera, config)
    flat_rays = rays.reshape((-1, 3))
    triangle_vertices = scene.mesh.vertices[scene.mesh.triangles]
    origins = triangle_vertices[:, 0]
    edge1 = triangle_vertices[:, 1] - origins
    edge2 = triangle_vertices[:, 2] - origins
    raw_normals = np.cross(edge1, edge2)
    lengths = np.linalg.norm(raw_normals, axis=1)
    valid_triangles = lengths > config.ray_epsilon
    triangle_normals = np.zeros_like(raw_normals)
    triangle_normals[valid_triangles] = raw_normals[valid_triangles] / lengths[valid_triangles, None]
    origin_to_triangle = camera.position[None, :] - origins
    qvec = np.cross(origin_to_triangle, edge1)

    depth = np.full(len(flat_rays), np.nan, dtype=np.float64)
    normals = np.full((len(flat_rays), 3), np.nan, dtype=np.float64)
    for start in range(0, len(flat_rays), 64):
        directions = flat_rays[start : start + 64]
        pvec = np.cross(directions[:, None, :], edge2[None, :, :])
        determinant = np.einsum("tj,rtj->rt", edge1, pvec)
        usable = np.abs(determinant) > config.ray_epsilon
        inverse = np.divide(1.0, determinant, out=np.zeros_like(determinant), where=usable)
        u = np.einsum("tj,rtj->rt", origin_to_triangle, pvec) * inverse
        v = np.einsum("rj,tj->rt", directions, qvec) * inverse
        distance = np.einsum("tj,tj->t", edge2, qvec)[None, :] * inverse
        hit = (
            usable
            & valid_triangles[None, :]
            & (u >= -config.ray_epsilon)
            & (v >= -config.ray_epsilon)
            & (u + v <= 1.0 + config.ray_epsilon)
            & (distance >= camera.near)
            & (distance <= camera.far)
        )
        candidates = np.where(hit, distance, np.inf)
        indices = np.argmin(candidates, axis=1)
        nearest = candidates[np.arange(len(directions)), indices]
        found = np.isfinite(nearest)
        target = slice(start, start + len(directions))
        depth[target][found] = nearest[found]
        selected = triangle_normals[indices]
        facing_away = np.einsum("ij,ij->i", selected, directions) > 0.0
        selected = np.where(facing_away[:, None], -selected, selected)
        selected = np.round(selected * 32.0) / 32.0
        selected_lengths = np.linalg.norm(selected, axis=1)
        selected = np.divide(selected, selected_lengths[:, None], out=np.zeros_like(selected), where=selected_lengths[:, None] > 0.0)
        normals[target][found] = selected[found]

    world_points = camera.position[None, :] + depth[:, None] * flat_rays
    return depth.reshape((config.depth_height, config.depth_width)), normals.reshape((config.depth_height, config.depth_width, 3)), world_points.reshape((config.depth_height, config.depth_width, 3))


def _capture(
    scene: Scene,
    camera: CameraPose,
    *,
    seed: int,
    corruption: CorruptionConfig,
    config: ObservationConfig,
) -> _Frame:
    calibration_seed, dropout_seed, noise_seed, sample_seed = np.random.SeedSequence(int(seed)).spawn(4)
    effective = _effective_camera(camera, corruption, np.random.default_rng(calibration_seed))
    depth, normals, _ = _raycast(scene, effective, config)
    effective_rays = _pixel_rays(effective, config)
    nominal_rays = _pixel_rays(camera, config)

    finite = np.isfinite(depth)
    if corruption.grazing_dropout_probability > 0.0 and np.any(finite):
        cosine = np.abs(np.einsum("ijk,ijk->ij", normals, -effective_rays))
        grazing_strength = np.clip((corruption.grazing_cosine_threshold - cosine) / corruption.grazing_cosine_threshold, 0.0, 1.0)
        probability = corruption.grazing_dropout_probability * grazing_strength
        dropped = np.random.default_rng(dropout_seed).random(depth.shape) < probability
        depth[dropped & finite] = np.nan
        normals[dropped & finite] = np.nan
        finite = np.isfinite(depth)

    if corruption.depth_noise_std > 0.0 and np.any(finite):
        noise = np.random.default_rng(noise_seed).normal(0.0, corruption.depth_noise_std, size=depth.shape)
        depth[finite] = np.clip(depth[finite] + noise[finite], camera.near, camera.far)

    points = camera.position[None, None, :] + depth[:, :, None] * nominal_rays
    sample_count = min(config.sparse_point_count, depth.size)
    sparse_indices = np.sort(
        np.random.default_rng(sample_seed).choice(depth.size, size=sample_count, replace=False)
    )
    return _Frame(depth, normals, points, normals.copy(), sparse_indices, effective)


def _workspace_bounds(scene: Scene) -> WorkspaceBounds:
    return WorkspaceBounds(scene.mesh.vertices.min(axis=0), scene.mesh.vertices.max(axis=0))


def _feature_region(scene: Scene, feature_id: str, camera: CameraPose, config: ObservationConfig) -> FeatureRegion2D:
    feature_points = scene.feature_points(scene.feature(feature_id))
    projection = project_points(feature_points, camera)
    in_front = projection.depth > camera.near
    uncertainty = config.feature_uncertainty_px
    if not np.any(in_front):
        return FeatureRegion2D(np.asarray((0.0, 0.0, 1.0, 1.0)), np.asarray((camera.near, camera.far)), uncertainty, 0.0)

    pixels = projection.pixels[in_front]
    depths = np.linalg.norm(feature_points[in_front] - camera.position, axis=1)
    quantum_x = config.feature_region_quantum_px / camera.image_width_px
    quantum_y = config.feature_region_quantum_px / camera.image_height_px
    normalized = np.column_stack((pixels[:, 0] / camera.image_width_px, pixels[:, 1] / camera.image_height_px))
    lower = normalized.min(axis=0) - np.asarray((uncertainty / camera.image_width_px, uncertainty / camera.image_height_px))
    upper = normalized.max(axis=0) + np.asarray((uncertainty / camera.image_width_px, uncertainty / camera.image_height_px))
    lower = np.floor(lower / np.asarray((quantum_x, quantum_y))) * np.asarray((quantum_x, quantum_y))
    upper = np.ceil(upper / np.asarray((quantum_x, quantum_y))) * np.asarray((quantum_x, quantum_y))
    bounds = np.clip(np.asarray((lower[0], lower[1], upper[0], upper[1])), 0.0, 1.0)

    diagonal = float(np.linalg.norm(scene.mesh.vertices.max(axis=0) - scene.mesh.vertices.min(axis=0)))
    depth_quantum = max(diagonal * 0.04, 1e-6)
    depth_lower = max(camera.near, np.floor((depths.min() - diagonal * 0.05) / depth_quantum) * depth_quantum)
    depth_upper = min(camera.far, np.ceil((depths.max() + diagonal * 0.05) / depth_quantum) * depth_quantum)
    inside = (normalized[:, 0] >= 0.0) & (normalized[:, 0] <= 1.0) & (normalized[:, 1] >= 0.0) & (normalized[:, 1] <= 1.0)
    confidence = float(np.mean(inside))
    return FeatureRegion2D(bounds, np.asarray((depth_lower, max(depth_lower, depth_upper))), uncertainty, confidence)


def _feature_samples(scene: Scene, feature_id: str) -> tuple[np.ndarray, np.ndarray]:
    feature = scene.feature(feature_id)
    if not feature.triangle_indices:
        points = scene.feature_points(feature)
        return points, np.full(len(points), 1.0 / len(points), dtype=np.float64)
    triangle_ids = np.asarray(feature.triangle_indices, dtype=np.int64)
    triangles = scene.mesh.vertices[scene.mesh.triangles[triangle_ids]]
    barycentric = np.asarray(
        (
            (0.6, 0.2, 0.2),
            (0.2, 0.6, 0.2),
            (0.2, 0.2, 0.6),
            (0.45, 0.45, 0.10),
            (0.45, 0.10, 0.45),
            (0.10, 0.45, 0.45),
        ),
        dtype=np.float64,
    )
    samples = np.einsum("sk,tkd->tsd", barycentric, triangles).reshape((-1, 3))
    areas = np.linalg.norm(
        np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]),
        axis=1,
    ) * 0.5
    if float(areas.sum()) <= 1e-15:
        weights = np.full(len(samples), 1.0 / len(samples), dtype=np.float64)
    else:
        weights = np.repeat(areas / areas.sum() / len(barycentric), len(barycentric))
    return samples, weights


def _nearest_ray_distances(
    scene: Scene,
    origin: np.ndarray,
    directions: np.ndarray,
    *,
    epsilon: float,
) -> np.ndarray:
    triangle_vertices = scene.mesh.vertices[scene.mesh.triangles]
    triangle_origins = triangle_vertices[:, 0]
    edge1 = triangle_vertices[:, 1] - triangle_origins
    edge2 = triangle_vertices[:, 2] - triangle_origins
    origin_to_triangle = origin[None, :] - triangle_origins
    qvec = np.cross(origin_to_triangle, edge1)
    nearest = np.full(len(directions), np.inf, dtype=np.float64)
    for start in range(0, len(directions), 64):
        rays = directions[start : start + 64]
        pvec = np.cross(rays[:, None, :], edge2[None, :, :])
        determinant = np.einsum("tj,rtj->rt", edge1, pvec)
        usable = np.abs(determinant) > epsilon
        inverse = np.divide(1.0, determinant, out=np.zeros_like(determinant), where=usable)
        u = np.einsum("tj,rtj->rt", origin_to_triangle, pvec) * inverse
        v = np.einsum("rj,tj->rt", rays, qvec) * inverse
        distance = np.einsum("tj,tj->t", edge2, qvec)[None, :] * inverse
        hit = usable & (u >= -epsilon) & (v >= -epsilon) & (u + v <= 1.0 + epsilon) & (distance >= epsilon)
        nearest[start : start + len(rays)] = np.min(np.where(hit, distance, np.inf), axis=1)
    return nearest


def _full_mesh_visibility(scene: Scene, feature_id: str, camera: CameraPose, config: ObservationConfig) -> float:
    hypotheses, weights = _feature_samples(scene, feature_id)
    offsets = hypotheses - camera.position
    distances = np.linalg.norm(offsets, axis=1)
    directions = offsets / distances[:, None]
    epsilon = max(config.ray_epsilon, 1e-7)
    nearest = _nearest_ray_distances(scene, camera.position, directions, epsilon=epsilon)
    visible = nearest >= distances - epsilon
    return float(np.clip(np.sum(weights[visible]), 0.0, 1.0))


def _depth_visibility(
    scene: Scene,
    feature_id: str,
    sensor_camera: CameraPose,
    depth: np.ndarray,
    config: ObservationConfig,
) -> float:
    """Conservative feature-ray occlusion test using only samples present in ``depth``."""
    hypotheses, weights = _feature_samples(scene, feature_id)
    projection = project_points(hypotheses, sensor_camera)
    expected_range = np.linalg.norm(hypotheses - sensor_camera.position, axis=1)
    rows = np.floor(projection.pixels[:, 1] / sensor_camera.image_height_px * config.depth_height).astype(np.int64)
    columns = np.floor(projection.pixels[:, 0] / sensor_camera.image_width_px * config.depth_width).astype(np.int64)
    inside = (
        (projection.depth > sensor_camera.near)
        & (rows >= 0)
        & (rows < config.depth_height)
        & (columns >= 0)
        & (columns < config.depth_width)
    )
    returned = np.full(len(hypotheses), np.nan, dtype=np.float64)
    returned[inside] = depth[rows[inside], columns[inside]]
    visible = np.isfinite(returned) & (returned >= expected_range - config.prior_voxel_size)
    return float(np.clip(np.sum(weights[visible]), 0.0, 1.0))

def _sparse_visibility(
    scene: Scene,
    feature_id: str,
    sensor_camera: CameraPose,
    frame: _Frame,
    config: ObservationConfig,
) -> float:
    """Nearest-ray estimate with support scaled to the sparse sampler's lattice spacing."""
    hypotheses, weights = _feature_samples(scene, feature_id)
    projection = project_points(hypotheses, sensor_camera)
    expected_range = np.linalg.norm(hypotheses - sensor_camera.position, axis=1)
    hypothesis_rows = projection.pixels[:, 1] / sensor_camera.image_height_px * config.depth_height - 0.5
    hypothesis_columns = projection.pixels[:, 0] / sensor_camera.image_width_px * config.depth_width - 0.5
    sampled_depth = frame.depth.ravel()[frame.sparse_indices]
    valid_samples = np.isfinite(sampled_depth)
    if not np.any(valid_samples):
        return 0.0
    valid_indices = frame.sparse_indices[valid_samples]
    sample_rows = valid_indices // config.depth_width
    sample_columns = valid_indices % config.depth_width
    sample_ranges = sampled_depth[valid_samples]
    support_radius = 1.5 * np.sqrt(frame.depth.size / len(frame.sparse_indices))
    visible = np.zeros(len(hypotheses), dtype=bool)
    in_front = projection.depth > sensor_camera.near
    for index in np.flatnonzero(in_front):
        distance_squared = (
            (sample_rows - hypothesis_rows[index]) ** 2
            + (sample_columns - hypothesis_columns[index]) ** 2
        )
        nearest = int(np.argmin(distance_squared))
        if distance_squared[nearest] <= support_radius * support_radius:
            visible[index] = sample_ranges[nearest] >= expected_range[index] - config.prior_voxel_size
    return float(np.clip(np.sum(weights[visible]), 0.0, 1.0))




def _level_evidence(
    scene: Scene,
    feature_id: str,
    camera: CameraPose,
    level: InformationLevel,
    frame: _Frame,
    config: ObservationConfig,
    prior_points: np.ndarray,
    prior_normals: np.ndarray,
) -> tuple[float, np.ndarray, np.ndarray]:
    flat_points = frame.points.reshape((-1, 3))
    flat_normals = frame.point_normals.reshape((-1, 3))
    current_valid = np.isfinite(flat_points).all(axis=1) & np.isfinite(flat_normals).all(axis=1)
    accumulated_points, accumulated_normals = _voxel_merge(
        np.concatenate((prior_points, flat_points[current_valid]), axis=0),
        np.concatenate((prior_normals, flat_normals[current_valid]), axis=0),
        config.prior_voxel_size,
    )
    if level == "full":
        visibility = _full_mesh_visibility(scene, feature_id, camera, config)
    elif level == "depth_only":
        visibility = _depth_visibility(scene, feature_id, frame.effective_camera, frame.depth, config)
    elif level == "sparse_points":
        visibility = _sparse_visibility(scene, feature_id, frame.effective_camera, frame, config)
    else:
        visibility = _depth_visibility(scene, feature_id, frame.effective_camera, frame.depth, config)
    return visibility, accumulated_points, accumulated_normals


def _voxel_merge(points: np.ndarray, normals: np.ndarray, voxel_size: float) -> tuple[np.ndarray, np.ndarray]:
    if len(points) == 0:
        return points.reshape((0, 3)), normals.reshape((0, 3))
    keys = np.floor(points / voxel_size).astype(np.int64)
    _, first = np.unique(keys, axis=0, return_index=True)
    selected = np.sort(first)
    return points[selected], normals[selected]


def _build_observation(
    scene: Scene,
    camera: CameraPose,
    level: InformationLevel,
    frame: _Frame,
    region: FeatureRegion2D,
    visits: tuple[VisitRecord, ...],
    visibility: float,
    accumulated_points: np.ndarray,
    accumulated_normals: np.ndarray,
) -> Observation:
    common = dict(
        level=level,
        camera=camera,
        workspace_bounds=_workspace_bounds(scene),
        feature_region=region,
        visits=visits,
        visibility_estimate=visibility,
        valid_return_fraction=float(np.mean(np.isfinite(frame.depth))),
    )
    if level == "full":
        return FullObservation(
            **common,
            mesh_vertices=scene.mesh.vertices,
            mesh_triangles=scene.mesh.triangles,
            depth_map=frame.depth,
            normal_map=frame.normals,
        )
    if level == "depth_only":
        return DepthOnlyObservation(**common, depth_map=frame.depth, normal_map=frame.normals)
    flat_points = frame.points.reshape((-1, 3))
    flat_normals = frame.point_normals.reshape((-1, 3))
    if level == "sparse_points":
        return SparsePointsObservation(
            **common,
            points=flat_points[frame.sparse_indices],
            normals=flat_normals[frame.sparse_indices],
        )
    return DepthPlusPriorObservation(
        **common,
        depth_map=frame.depth,
        normal_map=frame.normals,
        accumulated_points=accumulated_points,
        accumulated_normals=accumulated_normals,
    )


def observe(
    scene: Scene,
    feature_id: str,
    camera: CameraPose,
    *,
    level: InformationLevel = "depth_only",
    seed: int = 0,
    corruption: CorruptionConfig = CorruptionConfig(),
    config: ObservationConfig = ObservationConfig(),
    visits: tuple[VisitRecord, ...] = (),
) -> Observation:
    """Take one camera-limited observation.

    ``scene`` and ``feature_id`` belong to this synthetic sensor boundary; neither is retained in
    or reachable from the returned object.
    """
    if level not in INFORMATION_LEVELS:
        raise ValueError(f"unsupported information level: {level!r}")
    frame = _capture(scene, camera, seed=seed, corruption=corruption, config=config)
    region = _feature_region(scene, feature_id, frame.effective_camera, config)
    empty = np.empty((0, 3), dtype=np.float64)
    visibility, accumulated_points, accumulated_normals = _level_evidence(
        scene, feature_id, camera, level, frame, config, empty, empty
    )
    return _build_observation(
        scene,
        camera,
        level,
        frame,
        region,
        tuple(visits),
        visibility,
        accumulated_points,
        accumulated_normals,
    )


class SequentialObserver:
    """Stateful K-look sensor with per-frame visits and a running-best visibility estimate.

    Finite surface returns are voxel-unioned for the prior level. ``VisitRecord`` values remain
    outcomes of one frame; the returned observation's scalar summarizes all visits so far.
    """

    def __init__(
        self,
        scene: Scene,
        feature_id: str,
        *,
        level: InformationLevel = "depth_plus_prior",
        seed: int = 0,
        corruption: CorruptionConfig = CorruptionConfig(),
        config: ObservationConfig = ObservationConfig(),
    ) -> None:
        if level not in INFORMATION_LEVELS:
            raise ValueError(f"unsupported information level: {level!r}")
        scene.feature(feature_id)
        self._scene = scene
        self._feature_id = feature_id
        self._level = level
        self._seed = int(seed)
        self._corruption = corruption
        self._config = config
        self._visits: tuple[VisitRecord, ...] = ()
        self._points = np.empty((0, 3), dtype=np.float64)
        self._normals = np.empty((0, 3), dtype=np.float64)

    @property
    def visit_count(self) -> int:
        return len(self._visits)

    def look(self, camera: CameraPose) -> Observation:
        look_seed = int(np.random.SeedSequence((self._seed, len(self._visits))).generate_state(1, dtype=np.uint64)[0])
        frame = _capture(self._scene, camera, seed=look_seed, corruption=self._corruption, config=self._config)
        region = _feature_region(self._scene, self._feature_id, frame.effective_camera, self._config)
        visibility, accumulated_points, accumulated_normals = _level_evidence(
            self._scene,
            self._feature_id,
            camera,
            self._level,
            frame,
            self._config,
            self._points,
            self._normals,
        )
        visit = VisitRecord(
            camera=camera,
            visibility_estimate=visibility,
            valid_return_fraction=float(np.mean(np.isfinite(frame.depth))),
            feature_confidence=region.confidence,
        )
        visits = self._visits + (visit,)
        accumulated_visibility = max(record.visibility_estimate for record in visits)
        result = _build_observation(
            self._scene,
            camera,
            self._level,
            frame,
            region,
            visits,
            accumulated_visibility,
            accumulated_points,
            accumulated_normals,
        )
        self._points = accumulated_points
        self._normals = accumulated_normals
        self._visits = visits
        return result

    def looks(self, cameras: Iterable[CameraPose]) -> tuple[Observation, ...]:
        return tuple(self.look(camera) for camera in cameras)
