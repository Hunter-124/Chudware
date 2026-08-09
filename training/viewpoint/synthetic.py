from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from services.viewpoint.geometry import Scene
from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import GENERATOR_VERSION, PartExample, generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .data import FEATURE_KINDS, ViewpointBatch, encode_candidate, encode_task
from .model import COMPONENTS

Array = np.ndarray


@dataclass(frozen=True)
class CorpusConfig:
    parts: int = 72
    examples_per_part: int = 6
    candidates: int = 32
    seed: int = 1729
    image_size: tuple[int, int] = (320, 240)

    def validate(self) -> "CorpusConfig":
        if self.parts < 3 or self.examples_per_part <= 0 or self.candidates < 3:
            raise ValueError("corpus requires >=3 parts, positive examples, and >=3 candidates")
        if min(self.image_size) <= 0:
            raise ValueError("image dimensions must be positive")
        return self


def _geometry_tokens(scene: Scene, target_id: str) -> tuple[Array, Array]:
    vertices = scene.mesh.vertices
    center = vertices.mean(axis=0)
    radius = max(float(np.linalg.norm(vertices - center, axis=1).max()), 1.0e-6)
    tokens = np.zeros((len(scene.features), 18), np.float32)
    feature_mask = np.zeros(len(scene.features), bool)
    for row, feature in enumerate(scene.features):
        points = scene.feature_points(feature)
        local_center = points.mean(axis=0)
        tokens[row, :3] = (local_center - center) / radius
        tokens[row, 3:6] = (points.max(axis=0) - points.min(axis=0)) / (2.0 * radius)
        if feature.orientation is not None:
            tokens[row, 6:9] = feature.orientation
        kind = feature.kind if feature.kind in FEATURE_KINDS else "face"
        tokens[row, 9 + FEATURE_KINDS.index(kind)] = 1.0
        if feature.triangle_indices:
            triangles = scene.mesh.vertices[scene.mesh.triangles[np.asarray(feature.triangle_indices)]]
            area = 0.5 * np.linalg.norm(
                np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]),
                axis=1,
            ).sum()
        else:
            area = 0.0
        tokens[row, 15] = np.log1p(area / (radius * radius))
        tokens[row, 16] = np.linalg.norm(local_center - center) / radius
        tokens[row, 17] = len(feature.triangle_indices) / 2.0
        feature_mask[row] = feature.feature_id == target_id
    return tokens, feature_mask


def _candidate_seed(config: CorpusConfig, part_index: int, example_index: int) -> int:
    sequence = np.random.SeedSequence((config.seed, part_index, example_index, 0xCA4E2A))
    return int(sequence.generate_state(1, dtype=np.uint32)[0])


def _pad_geometry(rows: list[Array], masks: list[Array]) -> tuple[Array, Array, Array]:
    max_features = max(len(row) for row in rows)
    geometry = np.zeros((len(rows), max_features, rows[0].shape[1]), np.float32)
    geometry_mask = np.zeros((len(rows), max_features), bool)
    feature_mask = np.zeros((len(rows), max_features), bool)
    for index, (row, target_mask) in enumerate(zip(rows, masks)):
        geometry[index, : len(row)] = row
        geometry_mask[index, : len(row)] = True
        feature_mask[index, : len(row)] = target_mask
    return geometry, geometry_mask, feature_mask


def _example_rows(config: CorpusConfig) -> list[tuple[int, int, PartExample]]:
    rows: list[tuple[int, int, PartExample]] = []
    for part_index in range(config.parts):
        examples = generate_part_examples(
            part_index,
            config.examples_per_part,
            seed=config.seed,
        )
        rows.extend((part_index, example_index, example) for example_index, example in enumerate(examples))
    return rows


def generate_oracle_corpus(config: CorpusConfig = CorpusConfig()) -> ViewpointBatch:
    """Generate labels from occluding mechanical parts and the geometric oracle.

    All examples for one ``part_id`` share one generated scene. Candidate pools are
    generated once at their maximum render budget; best-of-N baselines consume nested
    prefixes without regenerating poses.
    """
    config.validate()
    part_ids: list[str] = []
    difficulties: list[str] = []
    tasks: list[Array] = []
    geometry_rows: list[Array] = []
    target_masks: list[Array] = []
    candidate_rows: list[Array] = []
    component_rows: list[Array] = []

    for part_index, example_index, example in _example_rows(config):
        scene = example.scene
        vertices = scene.mesh.vertices
        scene_center = vertices.mean(axis=0)
        radius = max(float(np.linalg.norm(vertices - scene_center, axis=1).max()), 1.0e-6)
        poses = generate_benchmark_candidate_set(
            scene,
            example.target_id,
            config.candidates,
            seed=_candidate_seed(config, part_index, example_index),
            image_size=config.image_size,
        )
        geometry, target_mask = _geometry_tokens(scene, example.target_id)
        labels = [evaluate(scene, example.target_id, pose).quality_components() for pose in poses]
        part_ids.append(example.part_id)
        difficulties.append(example.difficulty)
        tasks.append(encode_task(example.feature_kind, example.question_kind))
        geometry_rows.append(geometry)
        target_masks.append(target_mask)
        candidate_rows.append(np.stack([
            encode_candidate(
                scene,
                example.target_id,
                pose,
                part_center=scene_center,
                part_radius=radius,
            )
            for pose in poses
        ]))
        component_rows.append(np.asarray([
            [label[name] for name in COMPONENTS]
            for label in labels
        ], np.float32))

    geometry, geometry_mask, feature_mask = _pad_geometry(geometry_rows, target_masks)
    batch = ViewpointBatch(
        part_ids=tuple(part_ids),
        task=np.stack(tasks),
        geometry=geometry,
        geometry_mask=geometry_mask,
        feature_mask=feature_mask,
        candidates=np.stack(candidate_rows),
        candidate_mask=np.ones((len(part_ids), config.candidates), bool),
        components=np.stack(component_rows),
        difficulties=tuple(difficulties),
    )
    return batch.validate()


__all__ = ["CorpusConfig", "GENERATOR_VERSION", "generate_oracle_corpus"]
