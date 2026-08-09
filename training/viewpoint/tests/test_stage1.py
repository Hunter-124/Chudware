import numpy as np

from services.viewpoint.oracle import DEFAULT_WEIGHTS
from training.viewpoint.baselines import all_baselines, random_search_baselines
from training.viewpoint.metrics import compute_equivalence_report, evaluation_report
from training.viewpoint.model import COMPONENTS, weighted_geometric_mean
from training.viewpoint.synthetic import CorpusConfig, generate_oracle_corpus


def test_oracle_corpus_is_seeded_normalized_and_baselines_are_measured():
    config = CorpusConfig(parts=3, examples_per_part=1, candidates=5, seed=41, image_size=(96, 96))
    first = generate_oracle_corpus(config)
    second = generate_oracle_corpus(config)
    np.testing.assert_array_equal(first.candidates, second.candidates)
    np.testing.assert_array_equal(first.components, second.components)
    assert np.all((first.components >= 0.0) & (first.components <= 1.0))
    baselines = all_baselines(first, random_n=3)
    report = evaluation_report(first, baselines["greedy_geometric"], baselines)
    assert set(report["policy"]["components"]) == set(COMPONENTS)
    assert set(report["baselines"]) == {"fixed_isometric", "best_of_3_random", "greedy_geometric"}


def test_aggregate_weights_match_oracle_contract():
    values = np.asarray([[0.91, 0.82, 0.73, 0.64, 0.55, 0.96, 0.87]], np.float32)
    weights = np.asarray([weight for _, weight in DEFAULT_WEIGHTS], np.float32)
    expected = np.exp(np.sum(np.log(values) * weights, axis=-1) / weights.sum())
    np.testing.assert_allclose(weighted_geometric_mean(values), expected, rtol=1e-6)


def test_compute_equivalence_is_part_bootstrapped_and_reports_distributions():
    batch = generate_oracle_corpus(
        CorpusConfig(parts=6, examples_per_part=1, candidates=32, seed=53, image_size=(72, 72)),
    )
    random = random_search_baselines(batch)
    policy = all_baselines(batch)["greedy_geometric"]
    report = compute_equivalence_report(
        batch, policy, random, bootstrap_samples=64, seed=9,
    )
    assert report["bootstrap_unit"] == "part_id"
    assert report["test_examples"] == 6
    assert report["render_budgets"] == [1, 2, 4, 8, 16, 32]
    assert report["crossover_ci"]["lower"] in report["render_budgets"]
    previous = -np.inf
    for comparison in report["comparisons"].values():
        assert comparison["baseline_aggregate"] >= previous
        previous = comparison["baseline_aggregate"]
        assert 0.0 <= comparison["strict_win_rate"] <= 1.0
        assert set(comparison["delta_quantiles"]) == {
            "min", "p10", "p25", "median", "p75", "p90", "max",
        }
