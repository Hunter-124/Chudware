from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

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
