import json
import numpy as np


from services.perception.reference import NumpyMLDP
from training.perception.evaluate import evaluate_model
from training.perception.synthetic import (
    development_windows_for_total,
    generate_synthetic_corpus,
    total_windows_for_development,
)
from training.perception.train import train_job


def test_sample_size_ladder_preserves_exact_development_counts():
    for development_windows in (307, 1000, 3000, 10000):
        total_windows = total_windows_for_development(development_windows)
        assert development_windows_for_total(total_windows) == development_windows
        assert total_windows_for_development(development_windows) == total_windows


def test_cpu_train_entrypoint_is_seeded_and_writes_real_checkpoint(tmp_path):
    first = train_job({"seed": 101, "synthetic_windows": 32}, {}, tmp_path / "first")
    second = train_job({"seed": 101, "synthetic_windows": 32}, {}, tmp_path / "second")
    assert first == second
    checkpoint = tmp_path / "first" / "mldp_reference.npz"
    metrics_file = tmp_path / "first" / "metrics.json"
    assert checkpoint.stat().st_size > 0
    assert json.loads(metrics_file.read_text(encoding="utf-8"))["seed"] == 101
    model = NumpyMLDP.load(checkpoint)
    initial = NumpyMLDP(model.spec, seed=101)
    assert not np.array_equal(
        model.parameters["cam_w"], initial.parameters["cam_w"]
    )
    assert not np.array_equal(
        model.parameters["logvar_w"], initial.parameters["logvar_w"]
    )
    assert np.linalg.norm(model.parameters["logvar_w"]) > 0.0
    assert (
        first["calibration"]["parity_max_abs_error"]
        <= first["calibration"]["parity_atol"]
    )
    calibration = first["calibration"]
    comparison = calibration["variance_method_comparison"]
    assert (
        calibration["selection_split"]
        == "five_fold_cross_validation_over_development_train"
    )
    assert calibration["selection_folds"] == 5
    assert calibration["selection_examples"] == 25
    assert calibration["selection_raw_nll_winner"] in comparison
    assert calibration["selection_raw_nll_runner_up"] in comparison
    assert (
        calibration["selection_top_pair_nll_difference"]
        >= 0.0
    )
    assert (
        calibration["selection_top_pair_difference_standard_error"]
        >= 0.0
    )
    assert calibration["selection_top_pair_statistically_tied"] == (
        calibration["selection_top_pair_nll_difference"]
        <= calibration["selection_top_pair_difference_standard_error"]
    )
    assert calibration["selection_decisive_and_stable"] == (
        calibration["selection_decision"] == "decisive"
        and calibration["selection_stable_across_folds"]
    )
    assert calibration["test_used_for_selection"] is False
    assert {
        "beta_nll",
        "two_stage",
        "scalar_temperature",
        "per_horizon_temperature",
        "affine_conformal_temperature",
    } == set(comparison)
    assert sum(bool(report["selected"]) for report in comparison.values()) == 1
    assert comparison[calibration["selected_variance_method"]]["selected"]
    assert sum(
        int(report["fold_choice_count"]) for report in comparison.values()
    ) == calibration["selection_folds"]
    for report in comparison.values():
        assert len(report["fold_nll"]) == calibration["selection_folds"]
        assert report["standard_error"] >= 0.0
        assert np.isclose(
            report["mean_oof_nll"], np.mean(report["fold_nll"])
        )
        assert np.isclose(
            report["fold_choice_frequency"],
            report["fold_choice_count"] / calibration["selection_folds"],
        )
    if calibration["selection_decision"] == "parsimony_tie_break":
        selected_complexity = comparison[
            calibration["selected_variance_method"]
        ]["calibration_parameter_count"]
        assert len(calibration["selection_tied_methods"]) > 1
        assert selected_complexity == min(
            comparison[name]["calibration_parameter_count"]
            for name in calibration["selection_tied_methods"]
        )
    corpus = generate_synthetic_corpus(spec=model.spec, windows=16, seed=101)
    metrics = evaluate_model(model, corpus.windows, calibration=first["calibration"])
    assert "event_precision" in metrics
    assert "variance_calibration_error" in metrics
    assert all(f"horizon_{h}_rmse" in metrics for h in model.spec.horizons)
