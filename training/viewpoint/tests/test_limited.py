import numpy as np
import pytest

from services.viewpoint.observation import INFORMATION_LEVELS, observe
from services.viewpoint.parts import generate_part_examples
from training.viewpoint.data import locked_expansion_part_indices
from training.viewpoint.metrics import information_asymmetry_report, sequential_evaluation_report
from training.viewpoint.model import NumpyViewpointPolicy, weighted_geometric_mean
from training.viewpoint.observation_data import (
    LIMITED_INFORMATION_LEVELS,
    LimitedCorpusConfig,
    context_camera,
    encode_observation,
    generate_limited_corpus,
)
from training.viewpoint.sequential import SequentialConfig, evaluate_sequential_policy


def test_limited_encoder_refuses_full_and_is_deterministic_for_each_camera_level():
    example = generate_part_examples(0, 1, seed=17)[0]
    camera = context_camera(example.scene, image_size=(64, 64))
    full = observe(example.scene, example.target_id, camera, level="full", seed=9)
    with pytest.raises(ValueError, match="refuses full"):
        encode_observation(full)
    assert set(LIMITED_INFORMATION_LEVELS) < set(INFORMATION_LEVELS)
    for level in LIMITED_INFORMATION_LEVELS:
        config = LimitedCorpusConfig((0, 1, 2), level=level, candidates=3, seed=17, image_size=(64, 64))
        first = generate_limited_corpus(config)
        second = generate_limited_corpus(config)
        np.testing.assert_array_equal(first.geometry, second.geometry)
        np.testing.assert_array_equal(first.candidates, second.candidates)
        assert first.geometry.shape == (3, 25, 18)
        assert first.candidates.shape == (3, 3, 48)
        assert np.all(first.feature_mask[:, 0])


def test_locked_expansion_keeps_holdouts_identical_as_training_grows():
    train_50, validation_50, test_50 = locked_expansion_part_indices(50, seed=1729)
    train_5000, validation_5000, test_5000 = locked_expansion_part_indices(5000, seed=1729)
    assert train_5000[:50] == train_50
    assert validation_5000 == validation_50
    assert test_5000 == test_50
    assert len(train_5000) == 5000
    assert set(train_5000).isdisjoint(validation_5000)
    assert set(train_5000).isdisjoint(test_5000)


def test_information_and_sequential_reports_bootstrap_whole_parts():
    batch = generate_limited_corpus(
        LimitedCorpusConfig((0, 1, 2), candidates=4, seed=23, image_size=(64, 64)),
    )
    oracle = np.argmax(weighted_geometric_mean(batch.components), axis=1)
    report = information_asymmetry_report(
        batch,
        policy=oracle,
        random=np.zeros(3, np.int64),
        fixed_isometric=np.ones(3, np.int64),
        bootstrap_samples=16,
        bootstrap_seed=5,
    )
    assert report["bootstrap_unit"] == "part_id"
    assert report["full_information_oracle_regret"]["mean"] == 0.0
    direct = sequential_evaluation_report(
        batch,
        policy_best_by_budget={1: np.asarray((0.1, 0.2, 0.3)), 2: np.asarray((0.2, 0.3, 0.4))},
        one_shot_random_best_by_budget={1: np.asarray((0.0, 0.1, 0.2)), 2: np.asarray((0.1, 0.2, 0.3))},
        bootstrap_samples=16,
    )
    assert direct["bootstrap_unit"] == "part_id"
    assert direct["by_budget"]["2"]["policy_vs_one_shot_random"]["mean_delta"] > 0.0


def test_sequential_harness_uses_matched_additional_render_budgets():
    report = evaluate_sequential_policy(
        NumpyViewpointPolicy.random(seed=29),
        SequentialConfig(
            (0, 1, 2),
            level="depth_plus_prior",
            examples_per_part=1,
            candidates=4,
            budgets=(1, 2, 4),
            seed=29,
            image_size=(64, 64),
            bootstrap_samples=16,
        ),
    )
    assert report["look_budgets"] == [1, 2, 4]
    assert report["budget_unit"].startswith("additional camera render")
    assert report["policy_schedule"].startswith("adaptive")
    assert report["one_shot_schedule"].startswith("seeded random")
    for row in report["policy_selection_indices"]:
        assert len(row) == len(set(row)) == 4
