from pathlib import Path

import numpy as np
import pytest

from services.viewpoint.observation import INFORMATION_LEVELS, SequentialObserver, observe
from services.viewpoint.parts import generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set
from training.viewpoint.data import encode_task, locked_expansion_part_indices
from training.viewpoint.metrics import information_asymmetry_report, sequential_evaluation_report
from training.viewpoint.model import NumpyViewpointPolicy, PolicyWeights, weighted_geometric_mean
from training.viewpoint.observation_data import (
    LIMITED_INFORMATION_LEVELS,
    LimitedCorpusConfig,
    context_camera,
    encode_observation,
    generate_limited_corpus,
)
from training.viewpoint.sequential import SequentialConfig, _one_row_inputs, evaluate_sequential_policy


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


def test_policy_scores_are_sensitive_to_accumulated_observation_history():
    example = generate_part_examples(0, 1, seed=17)[0]
    candidates = generate_benchmark_candidate_set(
        example.scene,
        example.target_id,
        8,
        seed=123,
        image_size=(64, 64),
    )
    common = context_camera(example.scene, image_size=(64, 64))
    first = SequentialObserver(example.scene, example.target_id, level="depth_plus_prior", seed=999)
    second = SequentialObserver(example.scene, example.target_id, level="depth_plus_prior", seed=999)
    first.look(common)
    second.look(common)
    first.look(candidates[0])
    second.look(candidates[7])
    history_a = first.look(candidates[4])
    history_b = second.look(candidates[4])
    task = encode_task(example.feature_kind, example.question_kind)
    available = np.ones(len(candidates), bool)
    inputs_a = _one_row_inputs(task, history_a, candidates, available)
    inputs_b = _one_row_inputs(task, history_b, candidates, available)
    assert not np.array_equal(inputs_a["geometry"], inputs_b["geometry"])
    # Freeze even the encoded candidate tensor so only the observation-history
    # tokens can cause a score change.
    inputs_b["candidates"] = inputs_a["candidates"].copy()
    weights, spec = PolicyWeights.load(Path(__file__).parents[1] / "limited_depth_plus_prior_weights.npz")
    policy = NumpyViewpointPolicy(weights, spec)
    scores_a = policy.forward(**inputs_a)
    scores_b = policy.forward(**inputs_b)
    assert np.max(np.abs(scores_a - scores_b)) > 1.0e-6


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
    checkpoints = []
    report = evaluate_sequential_policy(
        NumpyViewpointPolicy.random(seed=29),
        SequentialConfig(
            (0, 1, 2),
            level="depth_plus_prior",
            examples_per_part=1,
            candidates=4,
            budgets=(1, 2, 4),
            workers=2,
            seed=29,
            image_size=(64, 64),
            bootstrap_samples=16,
            device="cpu",
        ),
        progress=checkpoints.append,
    )
    assert report["look_budgets"] == [1, 2, 4]
    assert report["budget_unit"].startswith("additional camera render")
    assert report["policy_schedule"].startswith("adaptive")
    assert report["one_shot_schedule"].startswith("seeded random")
    assert report["workers"] == 2
    assert report["device"] == "cpu"
    assert report["oracle_backend"] == "numpy"
    assert checkpoints[-1]["completed_examples"] == 3
    assert len(checkpoints[-1]["policy_best_by_budget"]["4"]) == 3
    assert report["common_initial_render_count"] == 1
    assert report["additional_render_budgets"] == [1, 2, 4]
    assert report["total_render_budgets"] == [2, 3, 5]
    for additional in (1, 2, 4):
        accounting = report["render_accounting"][str(additional)]
        assert accounting["policy_additional"] == accounting["one_shot_random_additional"] == additional
        assert accounting["policy_total"] == accounting["one_shot_random_total"] == additional + 1
    for row in report["policy_selection_indices"]:
        assert len(row) == len(set(row)) == 4
