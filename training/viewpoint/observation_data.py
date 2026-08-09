from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from services.viewpoint.geometry import CameraPose, Scene, unit
from services.viewpoint.observation import (
    CorruptionConfig,
    DepthOnlyObservation,
    DepthPlusPriorObservation,
    InformationLevel,
    Observation,
    ObservationConfig,
    SparsePointsObservation,
    WorkspaceBounds,
    observe,
)
from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import GENERATOR_VERSION, generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .data import ViewpointBatch, encode_camera_pose, encode_task
from .model import COMPONENTS

Array = np.ndarray
LIMITED_INFORMATION_LEVELS: tuple[InformationLevel, ...] = (
    "depth_only",
    "depth_plus_prior",
    "sparse_points",
)
GEOMETRY_TOKENS = 25


@dataclass(frozen=True)
class LimitedCorpusConfig:
    part_indices: tuple[int, ...]
    level: InformationLevel = "depth_only"
    examples_per_part: int = 1
    candidates: int = 16
    seed: int = 1729
    image_size: tuple[int, int] = (96, 96)
    observation: ObservationConfig = ObservationConfig(
        depth_height=12,
        depth_width=16,
        sparse_point_count=48,
        feature_region_quantum_px=4.0,
        feature_uncertainty_px=8.0,
        prior_voxel_size=0.08,
    )
    corruption: CorruptionConfig = CorruptionConfig()

    def validate(self) -> "LimitedCorpusConfig":
        if not self.part_indices or any(int(index) < 0 for index in self.part_indices):
            raise ValueError("part_indices must contain non-negative part indices")
        if len(set(self.part_indices)) != len(self.part_indices):
            raise ValueError("part_indices must be unique")
        if self.level not in LIMITED_INFORMATION_LEVELS:
            raise ValueError(f"limited corpus does not accept information level {self.level!r}")
        if self.examples_per_part <= 0 or self.candidates < 2:
            raise ValueError("examples_per_part and candidates must be positive")
        if len(self.image_size) != 2 or min(self.image_size) <= 0:
            raise ValueError("image_size must contain two positive values")
        return self




def context_camera(scene: Scene, *, image_size: tuple[int, int]) -> CameraPose:
    """Calibrated fixed isometric sensor view used before every policy decision."""
    lower = scene.mesh.vertices.min(axis=0)
    upper = scene.mesh.vertices.max(axis=0)
    center = (lower + upper) * 0.5
    radius = max(float(np.linalg.norm(scene.mesh.vertices - center, axis=1).max()), 1.0e-6)
    direction = unit(np.asarray((1.0, -1.0, 0.85), np.float64), name="context direction")
    position = center + direction * radius * 3.2
    forward = unit(center - position, name="context forward")
    preferred = np.asarray((0.0, 0.0, 1.0), np.float64)
    up = preferred if abs(float(np.dot(preferred, forward))) < 0.95 else np.asarray((0.0, 1.0, 0.0))
    return CameraPose(
        position=position,
        target=center,
        up=up,
        vertical_fov_deg=48.0,
        image_width_px=int(image_size[0]),
        image_height_px=int(image_size[1]),
        near=radius * 1.5,
        far=radius * 5.0,
    )


def _workspace_frame(bounds: WorkspaceBounds) -> tuple[Array, float]:
    center = (bounds.minimum + bounds.maximum) * 0.5
    radius = max(float(np.linalg.norm(bounds.maximum - bounds.minimum)) * 0.5, 1.0e-6)
    return center, radius


def _point_token(
    points: Array,
    normals: Array,
    *,
    center: Array,
    radius: float,
    density: float,
    image_center: tuple[float, float] = (0.0, 0.0),
    image_span: tuple[float, float] = (0.0, 0.0),
    feature_overlap: float = 0.0,
    current: float = 0.0,
    prior: float = 0.0,
    level_code: float = 0.0,
) -> Array:
    token = np.zeros(18, np.float32)
    if len(points):
        token[0:3] = np.mean(points, axis=0) - center
        token[0:3] /= radius
        token[3:6] = np.ptp(points, axis=0) / radius
    if len(normals):
        mean_normal = np.mean(normals, axis=0)
        norm = float(np.linalg.norm(mean_normal))
        if norm > 1.0e-8:
            token[6:9] = mean_normal / norm
    token[9] = float(np.clip(density, 0.0, 1.0))
    if len(points):
        distances = np.linalg.norm(points - center, axis=1) / radius
        token[10] = float(np.mean(distances))
        token[11] = float(np.std(distances))
    token[12:14] = image_center
    token[14] = float(feature_overlap)
    token[15] = float(current)
    token[16] = float(prior)
    token[17] = float(level_code)
    return token


def _metadata_token(observation: Observation, *, center: Array, radius: float) -> Array:
    region = observation.feature_region
    token = np.zeros(18, np.float32)
    token[0:3] = (observation.camera.target - center) / radius
    token[3:6] = (observation.workspace_bounds.maximum - observation.workspace_bounds.minimum) / radius
    token[6:9] = observation.camera.forward
    token[9] = observation.visibility_estimate
    token[10] = observation.valid_return_fraction
    token[11] = region.confidence
    token[12:16] = region.bounds_normalized
    token[16] = LIMITED_INFORMATION_LEVELS.index(observation.level) / max(len(LIMITED_INFORMATION_LEVELS) - 1, 1)
    token[17] = min(len(observation.visits), 8) / 8.0
    return token


def _depth_tokens(
    observation: DepthOnlyObservation,
    *,
    center: Array,
    radius: float,
) -> list[Array]:
    depth = np.asarray(observation.depth_map)
    normals = np.asarray(observation.normal_map)
    height, width = depth.shape
    rows, columns = np.indices(depth.shape, dtype=np.float64)
    ndc_x = 2.0 * (columns + 0.5) / width - 1.0
    ndc_y = 1.0 - 2.0 * (rows + 0.5) / height
    tan_y = np.tan(np.deg2rad(observation.camera.vertical_fov_deg) * 0.5)
    tan_x = tan_y * observation.camera.image_width_px / observation.camera.image_height_px
    rays = (
        observation.camera.forward[None, None, :]
        + ndc_x[:, :, None] * tan_x * observation.camera.right[None, None, :]
        + ndc_y[:, :, None] * tan_y * observation.camera.true_up[None, None, :]
    )
    rays /= np.linalg.norm(rays, axis=2, keepdims=True)
    points = observation.camera.position + rays * depth[:, :, None]
    region = observation.feature_region.bounds_normalized
    tokens: list[Array] = []
    for gy in range(4):
        y0, y1 = height * gy // 4, height * (gy + 1) // 4
        for gx in range(4):
            x0, x1 = width * gx // 4, width * (gx + 1) // 4
            block_depth = depth[y0:y1, x0:x1]
            valid = np.isfinite(block_depth)
            block_points = points[y0:y1, x0:x1][valid]
            block_normals = normals[y0:y1, x0:x1][valid]
            block_normals = block_normals[np.isfinite(block_normals).all(axis=1)]
            bx0, bx1 = x0 / width, x1 / width
            by0, by1 = y0 / height, y1 / height
            overlap_x = max(0.0, min(bx1, region[2]) - max(bx0, region[0]))
            overlap_y = max(0.0, min(by1, region[3]) - max(by0, region[1]))
            overlap = overlap_x * overlap_y / max((bx1 - bx0) * (by1 - by0), 1.0e-9)
            tokens.append(_point_token(
                block_points,
                block_normals,
                center=center,
                radius=radius,
                density=float(np.mean(valid)),
                image_center=((bx0 + bx1) - 1.0, 1.0 - (by0 + by1)),
                image_span=(bx1 - bx0, by1 - by0),
                feature_overlap=overlap,
                current=1.0,
                level_code=LIMITED_INFORMATION_LEVELS.index(observation.level) / 2.0,
            ))
    return tokens


def _spatial_bin_tokens(
    points: Array,
    normals: Array,
    *,
    center: Array,
    radius: float,
    prior: bool,
    level_code: float,
) -> list[Array]:
    points = np.asarray(points)
    normals = np.asarray(normals)
    valid = np.isfinite(points).all(axis=1) & np.isfinite(normals).all(axis=1)
    points, normals = points[valid], normals[valid]
    if not len(points):
        return [np.zeros(18, np.float32) for _ in range(8)]
    high = points >= center
    bins = high[:, 0].astype(np.int8) * 4 + high[:, 1].astype(np.int8) * 2 + high[:, 2].astype(np.int8)
    return [
        _point_token(
            points[bins == index], normals[bins == index],
            center=center, radius=radius,
            density=float(np.mean(bins == index)),
            current=0.0 if prior else 1.0,
            prior=1.0 if prior else 0.0,
            level_code=level_code,
        )
        for index in range(8)
    ]


def encode_observation(observation: Observation) -> tuple[Array, Array, Array]:
    """Encode only the public observation object into the existing token contract."""
    if observation.level not in LIMITED_INFORMATION_LEVELS:
        raise ValueError("the limited encoder intentionally refuses full observations")
    center, radius = _workspace_frame(observation.workspace_bounds)
    rows = [_metadata_token(observation, center=center, radius=radius)]
    valid_count = 1
    if isinstance(observation, DepthOnlyObservation):
        rows.extend(_depth_tokens(observation, center=center, radius=radius))
        valid_count += 16
    if isinstance(observation, DepthPlusPriorObservation):
        rows.extend(_spatial_bin_tokens(
            observation.accumulated_points,
            observation.accumulated_normals,
            center=center,
            radius=radius,
            prior=True,
            level_code=0.5,
        ))
        valid_count += 8
    elif isinstance(observation, SparsePointsObservation):
        rows.extend(_spatial_bin_tokens(
            observation.points,
            observation.normals,
            center=center,
            radius=radius,
            prior=False,
            level_code=1.0,
        ))
        valid_count += 8
    geometry = np.zeros((GEOMETRY_TOKENS, 18), np.float32)
    count = min(len(rows), GEOMETRY_TOKENS)
    geometry[:count] = np.asarray(rows[:count], np.float32)
    geometry_mask = np.zeros(GEOMETRY_TOKENS, bool)
    geometry_mask[: min(valid_count, GEOMETRY_TOKENS)] = True
    feature_mask = np.zeros(GEOMETRY_TOKENS, bool)
    feature_mask[0] = True
    return geometry, geometry_mask, feature_mask


def encode_observation_candidates(observation: Observation, candidates: Sequence[CameraPose]) -> Array:
    """Candidate descriptors using observation metadata, never scene or target geometry."""
    center, radius = _workspace_frame(observation.workspace_bounds)
    current = encode_camera_pose(observation.camera, part_center=center, part_radius=radius)
    region = observation.feature_region.as_vector().astype(np.float32)
    rows: list[Array] = []
    for candidate in candidates:
        base = encode_camera_pose(candidate, part_center=center, part_radius=radius)
        relative = np.asarray((
            np.dot(candidate.forward, observation.camera.right),
            np.dot(candidate.forward, observation.camera.true_up),
            np.dot(candidate.forward, observation.camera.forward),
            *((candidate.position - observation.camera.position) / radius),
            *((candidate.target - observation.camera.target) / radius),
            np.log(max(np.linalg.norm(candidate.position - candidate.target), 1.0e-6) / max(np.linalg.norm(observation.camera.position - observation.camera.target), 1.0e-6)),
            (candidate.vertical_fov_deg - observation.camera.vertical_fov_deg) / 180.0,
            np.dot(candidate.true_up, observation.camera.true_up),
        ), np.float32)
        metadata = np.asarray((
            observation.visibility_estimate,
            observation.valid_return_fraction,
            LIMITED_INFORMATION_LEVELS.index(observation.level) / 2.0,
            min(len(observation.visits), 8) / 8.0,
        ), np.float32)
        encoded = np.concatenate((base, current, relative, region, metadata))
        if encoded.shape != (48,) or not np.isfinite(encoded).all():
            raise RuntimeError("limited candidate encoding produced an invalid vector")
        rows.append(encoded.astype(np.float32))
    return np.stack(rows)


def _candidate_seed(seed: int, part_index: int, example_index: int) -> int:
    return int(np.random.SeedSequence((seed, part_index, example_index, 0x1A11)).generate_state(1, dtype=np.uint32)[0])


def _observation_seed(seed: int, part_index: int, example_index: int) -> int:
    return int(np.random.SeedSequence((seed, part_index, example_index, 0x0B5E)).generate_state(1, dtype=np.uint32)[0])




def _limited_part_rows(payload: tuple[LimitedCorpusConfig, int]) -> list[tuple[object, ...]]:
    config, part_index = payload
    rows: list[tuple[object, ...]] = []
    examples = generate_part_examples(part_index, config.examples_per_part, seed=config.seed)
    for example_index, example in enumerate(examples):
        camera = context_camera(example.scene, image_size=config.image_size)
        observation = observe(
            example.scene,
            example.target_id,
            camera,
            level=config.level,
            seed=_observation_seed(config.seed, part_index, example_index),
            corruption=config.corruption,
            config=config.observation,
        )
        poses = generate_benchmark_candidate_set(
            example.scene,
            example.target_id,
            config.candidates,
            seed=_candidate_seed(config.seed, part_index, example_index),
            image_size=config.image_size,
        )
        geometry, geometry_mask, feature_mask = encode_observation(observation)
        labels = [evaluate(example.scene, example.target_id, pose).quality_components() for pose in poses]
        rows.append((
            example.part_id,
            example.difficulty,
            encode_task(example.feature_kind, example.question_kind),
            geometry,
            geometry_mask,
            feature_mask,
            encode_observation_candidates(observation, poses),
            np.asarray([[label[name] for name in COMPONENTS] for label in labels], np.float32),
        ))
    return rows


def generate_limited_corpus(config: LimitedCorpusConfig, *, workers: int = 1) -> ViewpointBatch:
    """Generate oracle labels while exposing only camera observations to the policy."""
    config.validate()
    if workers <= 0:
        raise ValueError("workers must be positive")
    payloads = [(config, int(index)) for index in config.part_indices]
    if workers == 1:
        grouped = map(_limited_part_rows, payloads)
        rows = [row for group in grouped for row in group]
    else:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as executor:
            grouped = executor.map(
                _limited_part_rows,
                payloads,
                chunksize=max(1, len(payloads) // (workers * 16)),
            )
            rows = [row for group in grouped for row in group]
    return ViewpointBatch(
        part_ids=tuple(str(row[0]) for row in rows),
        task=np.stack([row[2] for row in rows]),
        geometry=np.stack([row[3] for row in rows]),
        geometry_mask=np.stack([row[4] for row in rows]),
        feature_mask=np.stack([row[5] for row in rows]),
        candidates=np.stack([row[6] for row in rows]),
        candidate_mask=np.ones((len(rows), config.candidates), bool),
        components=np.stack([row[7] for row in rows]),
        difficulties=tuple(str(row[1]) for row in rows),
    ).validate()


def concatenate_batches(batches: Sequence[ViewpointBatch]) -> ViewpointBatch:
    if not batches:
        raise ValueError("at least one batch is required")
    for batch in batches:
        batch.validate()
    return ViewpointBatch(
        part_ids=tuple(part for batch in batches for part in batch.part_ids),
        task=np.concatenate([batch.task for batch in batches], axis=0),
        geometry=np.concatenate([batch.geometry for batch in batches], axis=0),
        geometry_mask=np.concatenate([batch.geometry_mask for batch in batches], axis=0),
        feature_mask=np.concatenate([batch.feature_mask for batch in batches], axis=0),
        candidates=np.concatenate([batch.candidates for batch in batches], axis=0),
        candidate_mask=np.concatenate([batch.candidate_mask for batch in batches], axis=0),
        components=np.concatenate([batch.components for batch in batches], axis=0),
        difficulties=tuple(label for batch in batches for label in batch.difficulties),
    ).validate()


__all__ = [
    "GENERATOR_VERSION",
    "GEOMETRY_TOKENS",
    "LIMITED_INFORMATION_LEVELS",
    "LimitedCorpusConfig",
    "concatenate_batches",
    "context_camera",
    "encode_observation",
    "encode_observation_candidates",
    "generate_limited_corpus",
]
