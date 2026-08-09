from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from services.viewpoint.dataset import geometry_hash
from services.viewpoint.geometry import CameraPose, DIFFICULTY_LABELS
from services.viewpoint.oracle import COMPONENT_POLICY, evaluate
from services.viewpoint.parts import GENERATOR_VERSION, generate_part, generate_part_corpus, generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set


def _axis_pose(part, feature_id: str, direction: np.ndarray) -> CameraPose:
    center = part.scene.feature_center(feature_id)
    vertices = part.scene.mesh.vertices
    part_center = (vertices.min(axis=0) + vertices.max(axis=0)) * 0.5
    radius = float(np.linalg.norm(vertices - part_center, axis=1).max())
    direction = np.asarray(direction, dtype=np.float64)
    direction /= np.linalg.norm(direction)
    candidates = (np.asarray((0.0, 0.0, 1.0)), np.asarray((0.0, 1.0, 0.0)), np.asarray((1.0, 0.0, 0.0)))
    up = max(candidates, key=lambda value: float(np.linalg.norm(np.cross(-direction, value))))
    return CameraPose(
        position=center + 2.5 * radius * direction,
        target=center,
        up=up,
        vertical_fov_deg=45.0,
        image_width_px=320,
        image_height_px=240,
        near=radius * 0.001,
        far=radius * 20.0,
    )


def test_part_generator_is_seeded_novel_and_labels_every_target() -> None:
    first = generate_part(3, seed=41)
    repeated = generate_part(3, seed=41)
    held_out = generate_part(4, seed=41)

    assert geometry_hash(first.scene) == geometry_hash(repeated.scene)
    assert geometry_hash(first.scene) != geometry_hash(held_out.scene)
    assert len(first.target_feature_ids) == 12
    assert {first.scene.feature(feature_id).difficulty for feature_id in first.target_feature_ids} == set(DIFFICULTY_LABELS)
    assert {first.scene.feature(feature_id).kind for feature_id in first.target_feature_ids} >= {"face", "edge", "hole", "boss", "pocket"}


def test_examples_share_one_part_and_cycle_all_difficulties() -> None:
    examples = generate_part_examples(2, 12, seed=53)
    assert len({example.part_id for example in examples}) == 1
    assert all(example.scene is examples[0].scene for example in examples)
    assert len({example.target_id for example in examples}) == 12
    assert {example.difficulty for example in examples} == set(DIFFICULTY_LABELS)
    assert all(example.scene.feature(example.target_id).kind == example.feature_kind for example in examples)


def test_deep_counterbore_has_real_axis_dependent_self_occlusion() -> None:
    part = generate_part(0)
    feature = part.scene.feature("deep_counterbore")
    assert feature.orientation is not None
    helper = np.asarray((1.0, 0.0, 0.0)) if abs(float(feature.orientation[0])) < 0.8 else np.asarray((0.0, 1.0, 0.0))
    tangent = np.cross(feature.orientation, helper)
    axial = evaluate(part.scene, feature.feature_id, _axis_pose(part, feature.feature_id, feature.orientation))
    side = evaluate(part.scene, feature.feature_id, _axis_pose(part, feature.feature_id, tangent))
    back = evaluate(part.scene, feature.feature_id, _axis_pose(part, feature.feature_id, -feature.orientation))

    assert axial.quality_components()["visibility"] > 0.8
    assert side.quality_components()["visibility"] == pytest.approx(0.0)
    assert back.quality_components()["visibility"] == pytest.approx(0.0)


def test_benchmark_pose_pool_is_deterministic_and_prefix_stable() -> None:
    part = generate_part(1)
    short = generate_benchmark_candidate_set(part.scene, "angled_face_hole", 4, seed=101)
    long = generate_benchmark_candidate_set(part.scene, "angled_face_hole", 8, seed=101)
    repeated = generate_benchmark_candidate_set(part.scene, "angled_face_hole", 8, seed=101)

    assert [pose.as_dict() for pose in short] == [pose.as_dict() for pose in long[:4]]
    assert [pose.as_dict() for pose in long] == [pose.as_dict() for pose in repeated]
    assert len({round(pose.vertical_fov_deg, 8) for pose in long}) == len(long)
    assert len({round(float(np.linalg.norm(pose.position - pose.target)), 8) for pose in long}) == len(long)


def test_formerly_saturated_components_vary_on_hard_scene() -> None:
    part = generate_part(0)
    poses = generate_benchmark_candidate_set(part.scene, part.target_feature_ids[0], 8, seed=99)
    labels = [evaluate(part.scene, feature_id, pose) for feature_id in part.target_feature_ids for pose in poses]

    policy = {name: action for name, action, _ in COMPONENT_POLICY}
    for component in ("coverage", "scale", "depth", "uniqueness"):
        values = np.asarray([label.quality_components()[component] for label in labels])
        assert policy[component] == "included"
        assert float(values.min()) < 1.0
        assert np.unique(values).size > 1


def test_locked_mechanical_fixture_reproduces_part_hashes_and_counts() -> None:
    fixture_path = Path(__file__).parents[2] / "fixtures" / "viewpoint" / "mechanical_parts_v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    parts = generate_part_corpus(fixture["part_count"], seed=fixture["seed"])

    assert fixture["generator_version"] == GENERATOR_VERSION
    assert fixture["feature_count"] == sum(len(part.target_feature_ids) for part in parts)
    assert [geometry_hash(part.scene) for part in parts] == [item["geometry_sha256"] for item in fixture["parts"]]
    assert fixture["difficulty_counts"] == {
        difficulty: sum(feature.difficulty == difficulty for part in parts for feature in part.scene.features)
        for difficulty in DIFFICULTY_LABELS
    }
    assert [
        [
            {
                "feature_id": feature_id,
                "kind": part.scene.feature(feature_id).kind,
                "difficulty": part.scene.feature(feature_id).difficulty,
            }
            for feature_id in part.target_feature_ids
        ]
        for part in parts
    ] == [item["features"] for item in fixture["parts"]]
