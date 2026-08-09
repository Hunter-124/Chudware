from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from services.viewpoint.geometry import CameraPose, Scene, project_points, unit

from .model import ModelSpec

Array = np.ndarray
FEATURE_KINDS = ("face", "edge", "hole", "boss", "pocket", "region")
QUESTION_KINDS = ("presence", "dimension", "surface", "clearance", "orientation", "identity")


def encode_task(feature_kind: str, question_kind: str, *, min_scale: float = 0.25, detail: float = 0.5) -> Array:
    """Encode task semantics without hashes or part-specific identifiers."""
    if feature_kind not in FEATURE_KINDS:
        raise ValueError(f"unknown feature kind {feature_kind!r}")
    if question_kind not in QUESTION_KINDS:
        raise ValueError(f"unknown question kind {question_kind!r}")
    result = np.zeros(14, np.float32)
    result[FEATURE_KINDS.index(feature_kind)] = 1.0
    result[6 + QUESTION_KINDS.index(question_kind)] = 1.0
    result[12] = np.clip(min_scale, 0.0, 1.0)
    result[13] = np.clip(detail, 0.0, 1.0)
    return result


def _unit(value: Array, fallback: Sequence[float]) -> Array:
    vector = np.asarray(value, np.float32)
    norm = float(np.linalg.norm(vector))
    return np.asarray(fallback, np.float32) if norm < 1.0e-8 else vector / norm


def encode_camera_pose(pose: object, *, part_center: Array, part_radius: float) -> Array:
    """Continuous pose encoding: orthonormal forward/up, never Euler angles.

    ``pose`` follows ``services.viewpoint.CameraPose`` by attribute rather than an
    import, keeping dataset inspection independent of the service implementation.
    """
    position = np.asarray(getattr(pose, "position"), np.float32)
    target = np.asarray(getattr(pose, "target"), np.float32)
    forward = _unit(target - position, (0.0, 0.0, -1.0))
    up_raw = np.asarray(getattr(pose, "up"), np.float32)
    up = _unit(up_raw - forward * float(np.dot(up_raw, forward)), (0.0, 1.0, 0.0))
    radius = max(float(part_radius), 1.0e-6)
    offset = (target - np.asarray(part_center, np.float32)) / radius
    distance = max(float(np.linalg.norm(target - position)) / radius, 1.0e-6)
    fov = float(getattr(pose, "vertical_fov_deg")) / 180.0
    width = max(float(getattr(pose, "image_width_px")), 1.0)
    height = max(float(getattr(pose, "image_height_px")), 1.0)
    return np.concatenate((forward, up, offset, [np.log(distance), fov, np.log(width / height)])).astype(np.float32)


def _canonical_feature_frame(scene: Scene, target_id: str) -> tuple[Array, Array, Array, Array, Array]:
    feature = scene.feature(target_id)
    points = scene.feature_points(feature)
    center = scene.feature_center(feature)
    if feature.orientation is not None:
        axis = np.asarray(feature.orientation, np.float64)
    else:
        centered = points - center
        covariance = centered.T @ centered
        axis = np.linalg.eigh(covariance)[1][:, -1] if np.any(covariance) else np.asarray((0.0, 0.0, 1.0))
    axis = unit(axis, name="feature axis")
    dominant = int(np.argmax(np.abs(axis)))
    if axis[dominant] < 0.0:
        axis = -axis
    anchors = np.eye(3, dtype=np.float64)
    reference = anchors[int(np.argmin(np.abs(anchors @ axis)))]
    tangent = unit(np.cross(axis, reference), name="feature tangent")
    bitangent = np.cross(axis, tangent)
    return center, axis, tangent, bitangent, points


def _ray_pattern_points(center: Array, tangent: Array, bitangent: Array, points: Array) -> Array:
    """Choose a fixed center-plus-eight-extrema pattern on the target feature."""
    centered = points - center
    rows = [center]
    for angle in np.arange(8, dtype=np.float64) * (np.pi / 4.0):
        direction = np.cos(angle) * tangent + np.sin(angle) * bitangent
        rows.append(points[int(np.argmax(centered @ direction))])
    return np.asarray(rows, np.float64)


def _ray_visibility_pattern(
    scene: Scene,
    target_id: str,
    camera_position: Array,
    targets: Array,
    *,
    part_radius: float,
) -> tuple[Array, Array]:
    """Return coarse visibility and first-hit depth for rays toward target landmarks."""
    target_feature = scene.feature(target_id)
    triangle_mask = np.ones(len(scene.mesh.triangles), dtype=bool)
    if target_feature.triangle_indices:
        triangle_mask[np.asarray(target_feature.triangle_indices, np.int64)] = False
    triangles = scene.mesh.vertices[scene.mesh.triangles[triangle_mask]]
    visibility = np.ones(len(targets), np.float32)
    hit_depth = np.ones(len(targets), np.float32)
    if not len(triangles):
        return visibility, hit_depth

    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    origin = np.asarray(camera_position, np.float64)
    epsilon = max(float(part_radius) * 1.0e-7, 1.0e-9)
    for row, target in enumerate(targets):
        segment = target - origin
        target_distance = float(np.linalg.norm(segment))
        if target_distance <= epsilon:
            continue
        direction = segment / target_distance
        pvec = np.cross(np.broadcast_to(direction, edge2.shape), edge2)
        determinant = np.einsum("ij,ij->i", edge1, pvec)
        valid = np.abs(determinant) > 1.0e-12
        inverse = np.zeros_like(determinant)
        inverse[valid] = 1.0 / determinant[valid]
        tvec = origin - triangles[:, 0]
        bary_u = np.einsum("ij,ij->i", tvec, pvec) * inverse
        qvec = np.cross(tvec, edge1)
        bary_v = np.einsum("j,ij->i", direction, qvec) * inverse
        distance = np.einsum("ij,ij->i", edge2, qvec) * inverse
        hits = (
            valid
            & (bary_u >= 0.0)
            & (bary_v >= 0.0)
            & (bary_u + bary_v <= 1.0)
            & (distance > epsilon)
            & (distance < target_distance - epsilon)
        )
        if np.any(hits):
            nearest = float(np.min(distance[hits]))
            visibility[row] = 0.0
            hit_depth[row] = np.clip(nearest / target_distance, 0.0, 1.0)
    return visibility, hit_depth


def encode_candidate(
    scene: Scene,
    target_id: str,
    pose: CameraPose,
    *,
    part_center: Array | None = None,
    part_radius: float | None = None,
) -> Array:
    """Encode a pose together with cheap candidate-conditioned visibility evidence.

    The first twelve values preserve the continuous forward/up pose contract.
    The remaining values are view-dependent: target-relative directions, apparent
    extent, and a fixed nine-ray depth/visibility pattern. No oracle score enters
    this encoding.
    """
    vertices = scene.mesh.vertices
    center = vertices.mean(axis=0) if part_center is None else np.asarray(part_center, np.float64)
    radius = (
        max(float(np.linalg.norm(vertices - center, axis=1).max()), 1.0e-6)
        if part_radius is None
        else max(float(part_radius), 1.0e-6)
    )
    feature_center, axis, tangent, bitangent, points = _canonical_feature_frame(scene, target_id)
    base = encode_camera_pose(pose, part_center=center, part_radius=radius)
    camera_from_feature = unit(pose.position - feature_center, name="feature-to-camera direction")
    local_direction = np.asarray(
        (
            np.dot(camera_from_feature, tangent),
            np.dot(camera_from_feature, bitangent),
            np.dot(camera_from_feature, axis),
        ),
        np.float32,
    )
    axis_in_camera = np.asarray(
        (np.dot(axis, pose.right), np.dot(axis, pose.true_up), np.dot(axis, pose.forward)),
        np.float32,
    )
    distance = max(float(np.linalg.norm(pose.position - feature_center)), 1.0e-6)
    feature_extent = max(float(np.linalg.norm(points - feature_center, axis=1).max()), radius * 1.0e-6)
    apparent_scale = feature_extent / (distance * np.tan(np.deg2rad(pose.vertical_fov_deg) * 0.5))
    distance_scale = np.asarray((np.log(distance / radius), np.log1p(apparent_scale)), np.float32)

    projection = project_points(points, pose)
    front = projection.depth > 0.0
    if np.any(front):
        ndc = projection.ndc[front]
        ndc_center = np.clip(ndc.mean(axis=0), -4.0, 4.0)
        ndc_span = np.clip(np.ptp(ndc, axis=0), 0.0, 8.0) / 4.0
        in_frame = np.mean(np.all(np.abs(ndc) <= 1.0, axis=1))
    else:
        ndc_center = np.zeros(2, np.float64)
        ndc_span = np.zeros(2, np.float64)
        in_frame = 0.0
    projection_features = np.asarray(
        (*ndc_center, *ndc_span, in_frame, np.mean(front)),
        np.float32,
    )

    ray_targets = _ray_pattern_points(feature_center, tangent, bitangent, points)
    visibility, hit_depth = _ray_visibility_pattern(
        scene,
        target_id,
        pose.position,
        ray_targets,
        part_radius=radius,
    )
    ray_summary = np.asarray(
        (
            visibility.mean(),
            hit_depth.mean(),
            hit_depth.min(),
            hit_depth.std(),
        ),
        np.float32,
    )
    encoded = np.concatenate(
        (
            base,
            local_direction,
            axis_in_camera,
            distance_scale,
            projection_features,
            visibility,
            hit_depth,
            ray_summary,
        )
    ).astype(np.float32)
    if encoded.shape != (48,) or not np.isfinite(encoded).all():
        raise RuntimeError("candidate encoding contract produced an invalid vector")
    return encoded


@dataclass(frozen=True)
class ViewpointBatch:
    part_ids: tuple[str, ...]
    task: Array
    geometry: Array
    geometry_mask: Array
    feature_mask: Array
    candidates: Array
    candidate_mask: Array
    components: Array
    difficulties: tuple[str, ...] = ()

    def validate(self, spec: ModelSpec | None = None) -> "ViewpointBatch":
        s = (spec or ModelSpec()).validate()
        count = len(self.part_ids)
        if any(not item for item in self.part_ids):
            raise ValueError("part ids must be non-empty")
        if self.difficulties and len(self.difficulties) != count:
            raise ValueError("one difficulty label is required per example")
        if any(not item for item in self.difficulties):
            raise ValueError("difficulty labels must be non-empty")
        expected_prefix = {
            "task": (count, s.task_dim),
            "geometry": (count, None, s.geometry_dim),
            "geometry_mask": (count, None),
            "feature_mask": (count, None),
            "candidates": (count, None, s.candidate_dim),
            "candidate_mask": (count, None),
            "components": (count, None, s.component_count),
        }
        for name, expected in expected_prefix.items():
            value = np.asarray(getattr(self, name))
            if value.ndim != len(expected) or any(size is not None and value.shape[i] != size for i, size in enumerate(expected)):
                raise ValueError(f"{name} has shape {value.shape}, expected {expected}")
            if name not in {"geometry_mask", "feature_mask", "candidate_mask"} and not np.isfinite(value).all():
                raise ValueError(f"{name} contains non-finite values")
        if self.geometry.shape[:2] != self.geometry_mask.shape or self.feature_mask.shape != self.geometry_mask.shape:
            raise ValueError("geometry masks must match geometry")
        if self.candidates.shape[:2] != self.candidate_mask.shape or self.components.shape[:2] != self.candidate_mask.shape:
            raise ValueError("candidate tensors and masks must match")
        if np.any(self.feature_mask & ~self.geometry_mask) or np.any(~self.feature_mask.any(axis=1)):
            raise ValueError("each item needs a valid feature token")
        if np.any(~self.candidate_mask.any(axis=1)):
            raise ValueError("each item needs a candidate")
        if np.any((self.components < 0.0) | (self.components > 1.0)):
            raise ValueError("component labels must be in [0, 1]")
        return self

    def subset(self, indices: Iterable[int]) -> "ViewpointBatch":
        idx = np.asarray(tuple(indices), np.int64)
        return ViewpointBatch(
            part_ids=tuple(self.part_ids[int(i)] for i in idx),
            task=self.task[idx], geometry=self.geometry[idx], geometry_mask=self.geometry_mask[idx],
            feature_mask=self.feature_mask[idx], candidates=self.candidates[idx],
            candidate_mask=self.candidate_mask[idx], components=self.components[idx],
            difficulties=tuple(self.difficulties[int(i)] for i in idx) if self.difficulties else (),
        )

    def model_inputs(self) -> dict[str, Array]:
        return {
            "task": self.task,
            "geometry": self.geometry,
            "geometry_mask": self.geometry_mask,
            "feature_mask": self.feature_mask,
            "candidates": self.candidates,
            "candidate_mask": self.candidate_mask,
        }


def split_by_part(
    batch: ViewpointBatch,
    *,
    seed: int = 1729,
    train_fraction: float = 0.7,
    validation_fraction: float = 0.15,
) -> tuple[ViewpointBatch, ViewpointBatch, ViewpointBatch]:
    """Split whole part identities, never poses or feature/candidate rows."""
    batch.validate()
    if not (0.0 < train_fraction < 1.0 and 0.0 <= validation_fraction < 1.0 and train_fraction + validation_fraction < 1.0):
        raise ValueError("split fractions must leave non-empty train and test ranges")
    unique = np.asarray(sorted(set(batch.part_ids)), dtype=object)
    if len(unique) < 3:
        raise ValueError("at least three distinct parts are required")
    order = np.random.default_rng(seed).permutation(len(unique))
    n_train = max(1, min(len(unique) - 2, int(round(len(unique) * train_fraction))))
    n_val = max(1, min(len(unique) - n_train - 1, int(round(len(unique) * validation_fraction))))
    train_parts = set(unique[order[:n_train]].tolist())
    val_parts = set(unique[order[n_train:n_train + n_val]].tolist())
    test_parts = set(unique[order[n_train + n_val:]].tolist())

    def take(parts: set[str]) -> ViewpointBatch:
        return batch.subset(i for i, part in enumerate(batch.part_ids) if part in parts)

    train, validation, test = take(train_parts), take(val_parts), take(test_parts)
    assert set(train.part_ids).isdisjoint(validation.part_ids)
    assert set(train.part_ids).isdisjoint(test.part_ids)
    assert set(validation.part_ids).isdisjoint(test.part_ids)
    return train, validation, test


def locked_expansion_part_indices(
    train_parts: int,
    *,
    seed: int = 1729,
    base_parts: int = 72,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Preserve the original locked holdouts while adding procedural train parts."""
    if base_parts != 72:
        raise ValueError("the comparable locked split is defined on exactly 72 base parts")
    base_train_count = int(round(base_parts * 0.7))
    validation_count = int(round(base_parts * 0.15))
    test_count = base_parts - base_train_count - validation_count
    if train_parts < base_train_count:
        raise ValueError(f"train_parts must be at least the locked base count {base_train_count}")
    order = np.random.default_rng(seed).permutation(base_parts)
    train = tuple(int(value) for value in order[:base_train_count])
    validation = tuple(int(value) for value in order[base_train_count:base_train_count + validation_count])
    test = tuple(int(value) for value in order[-test_count:])
    extra = tuple(range(base_parts, base_parts + train_parts - base_train_count))
    return train + extra, validation, test
