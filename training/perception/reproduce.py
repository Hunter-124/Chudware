from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import tempfile
from typing import Any, Mapping

import numpy as np

from services.perception.reference import NumpyMLDP
from .baselines import fit_and_evaluate_baselines
from .evaluate import evaluate_model
from .metrics import (
    calibration_metrics,
    precursor_event_scores,
    regime_breakdown,
    structure_diagnostics,
)
from .synthetic import (
    SyntheticCorpus,
    generate_synthetic_corpus,
    total_windows_for_development,
)
from .train import DEFAULT_SEED, _split_indices, fit_reference_model

DEFAULT_WINDOWS = 384
DEFAULT_SWEEP_DEVELOPMENT_SIZES = (307, 1000, 3000)
ARTIFACT_FILES = (
    "mldp_reference.npz",
    "synthetic_corpus.npz",
    "metrics.json",
    "publication_manifest.json",
)
TRAINING_CORE_FILES = (
    "services/perception/config.py",
    "services/perception/reference.py",
    "training/perception/__init__.py",
    "training/perception/baselines.py",
    "training/perception/data.py",
    "training/perception/evaluate.py",
    "training/perception/export_onnx.py",
    "training/perception/manifest.py",
    "training/perception/metrics.py",
    "training/perception/model.py",
    "training/perception/reproduce.py",
    "training/perception/synthetic.py",
    "training/perception/train.py",
)
DOCUMENTATION_FILES = ("docs/manufacturing/22_perception_model.md",)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_corpus(path: Path, corpus: SyntheticCorpus) -> None:
    windows = corpus.windows
    metadata = json.dumps(
        {
            "anomaly_ticks": corpus.anomaly_ticks,
            "regimes": corpus.regimes,
            "seed": corpus.seed,
            "spec": asdict(corpus.spec),
            "split_labels": corpus.split_labels,
            "window_ids": windows.window_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    np.savez_compressed(
        path,
        __metadata__=np.array(metadata),
        cam_feat=windows.cam_feat,
        cam_mask=windows.cam_mask,
        cam_age=windows.cam_age,
        proc_feat=windows.proc_feat,
        proc_mask=windows.proc_mask,
        proc_age=windows.proc_age,
        cmd_future=windows.cmd_future,
        cmd_mask=windows.cmd_mask,
        target_latent=windows.target_latent,
        event_label=windows.event_label,
    )


def _source_entries(repo_root: Path, paths: tuple[str, ...]) -> list[dict[str, str]]:
    entries = []
    for relative in paths:
        source = repo_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"publication source is missing: {relative}")
        entries.append({"path": relative, "sha256": _sha256(source)})
    return entries


def _write_publication_manifest(out_dir: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    release_checksums = {
        name: _sha256(out_dir / name)
        for name in ARTIFACT_FILES
        if name != "publication_manifest.json"
    }
    manifest = {
        "schema_version": 1,
        "release_files": list(ARTIFACT_FILES),
        "release_checksums_sha256": release_checksums,
        "training_core": _source_entries(repo_root, TRAINING_CORE_FILES),
        "documentation": _source_entries(repo_root, DOCUMENTATION_FILES),
        "dataset_publication_predicate": {
            "all_of": [
                "record passes training.perception.manifest.ManifestRecord.from_dict",
                "export_restricted is false",
                "redaction_applied is true",
                "license is one of owned, licensed, synthetic",
                "license_ref is present in the release review's approved license-reference set and covers public redistribution",
                "consent_ref is present in the release review's approved consent-reference set and covers training and public redistribution",
            ],
            "third_party_default": "excluded unless reclassified by a new manifest schema carrying an explicit redistribution grant",
            "enforcement": "publication_eligible() requires both approved-reference sets; non-empty strings alone are insufficient",
        },
        "license": "Repository license applies to code; dataset and model redistribution remains subject to every source record's approved rights references.",
    }
    (out_dir / "publication_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def reproduce(out_dir: str | Path, *, seed: int = DEFAULT_SEED, windows: int = DEFAULT_WINDOWS) -> dict[str, Any]:
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    corpus = generate_synthetic_corpus(windows=windows, seed=seed)
    train_idx, calibration_idx, test_idx = _split_indices(windows, seed)
    train = corpus.windows.subset(train_idx)
    calibration = corpus.windows.subset(calibration_idx)
    test = corpus.windows.subset(test_idx)
    test_regimes = tuple(corpus.regimes[int(index)] for index in test_idx)

    candidate_models: dict[str, NumpyMLDP] = {}
    model, model_calibration = fit_reference_model(
        corpus,
        seed=seed,
        _candidate_models=candidate_models,
    )
    model_metrics = evaluate_model(
        model, test, calibration=model_calibration
    )
    model_output = model.forward(test.model_inputs())
    for method, candidate in candidate_models.items():
        candidate_output = candidate.forward(test.model_inputs())
        model_calibration["variance_method_comparison"][method][
            "test_variance_calibration_error"
        ] = calibration_metrics(
            test.target_latent,
            candidate_output["z_hat"],
            candidate_output["z_logvar"],
        )["variance_calibration_error"]
    model_scores = precursor_event_scores(model_output["event_bnd"])
    models: dict[str, dict[str, object]] = {
        "mldp": {
            "calibration": model_calibration,
            "metrics": model_metrics,
            "per_regime": regime_breakdown(
                test.target_latent,
                model_output["z_hat"],
                model_output["z_logvar"],
                test.event_label,
                model_scores,
                test_regimes,
                threshold=float(model_calibration["event_threshold"]),
                horizons=corpus.spec.horizons,
            ),
        }
    }
    models.update(
        fit_and_evaluate_baselines(
            train,
            calibration,
            test,
            corpus.spec,
            test_regimes=test_regimes,
        )
    )

    diagnostics = structure_diagnostics(train, test, corpus.spec.horizons)
    baseline_names = ("persistence", "linear_autoregressor", "mean")
    horizon_keys = tuple(
        f"horizon_{horizon}_{error}"
        for horizon in corpus.spec.horizons
        for error in ("mae", "rmse")
    )
    model_values = models["mldp"]["metrics"]
    linear_values = models["linear_autoregressor"]["metrics"]
    linear_horizon_win = all(
        float(model_values[key]) < float(linear_values[key])
        for key in horizon_keys
    )
    linear_calibration_win = (
        float(model_values["variance_calibration_error"])
        < float(linear_values["variance_calibration_error"])
    )
    linear_event_win = (
        float(model_values["event_f1"])
        > float(linear_values["event_f1"])
    )
    acceptance_bar = (
        linear_horizon_win and linear_calibration_win and linear_event_win
    )
    all_horizon_win = all(
        float(model_values[key]) < float(models[name]["metrics"][key])
        for name in baseline_names
        for key in horizon_keys
    )
    all_calibration_win = all(
        float(model_values["variance_calibration_error"])
        < float(models[name]["metrics"]["variance_calibration_error"])
        for name in baseline_names
    )
    all_event_win = all(
        float(model_values["event_f1"])
        > float(models[name]["metrics"]["event_f1"])
        for name in baseline_names
    )
    complete_all_win = (
        all_horizon_win and all_calibration_win and all_event_win
    )
    failed = [
        name
        for name, won in (
            ("horizon_error", linear_horizon_win),
            ("variance_calibration", linear_calibration_win),
            ("event_f1", linear_event_win),
        )
        if not won
    ]
    calibration_method = str(
        model_calibration["selected_variance_method"]
    )
    selection_stable = bool(
        model_calibration["selection_stable_across_folds"]
    )
    selection_tied = (
        model_calibration["selection_decision"]
        == "parsimony_tie_break"
    )
    mean_event_unchanged = bool(
        model_calibration["mean_event_parameters_unchanged_by_calibration"]
    )
    if acceptance_bar:
        statement = (
            "MLDP beats the linear autoregressor on every horizon MAE/RMSE, "
            "variance calibration error, and event F1."
        )
        next_experiment = None
        diagnosis = (
            f"{calibration_method} won calibration-split NLL without "
            "consulting the locked test split. "
            + (
                "The selected calibration changes only variance parameters, "
                "so mean forecasts and precursor event scores are unchanged."
                if mean_event_unchanged
                else "The selected method also changes mean/event parameters."
            )
        )
    else:
        statement = (
            "MLDP does not meet the acceptance bar against the linear "
            f"autoregressor; losing criteria: {', '.join(failed)}."
        )
        if failed == ["variance_calibration"]:
            diagnosis = (
                "The learned conditional variance remains overconfident on "
                "the locked split even though mean forecasts and precursor "
                "events generalize."
            )
            next_experiment = (
                "Fit a single variance temperature on the unchanged "
                "calibration split, then compare it with log-variance "
                "shrinkage selected on calibration NLL."
            )
        else:
            diagnosis = (
                "End-to-end optimization did not generalize every required "
                "prediction contract to the locked split."
            )
            next_experiment = (
                "Repeat the fixed objective with calibration-NLL early "
                "stopping and report the unchanged locked split once."
            )
    if selection_tied:
        diagnosis += (
            " The development corpus cannot distinguish the tied variance "
            "methods at one paired standard error; more windows spanning "
            "more regimes, or a larger calibration budget, are required."
        )
        next_experiment = (
            "Increase the development/calibration budget with additional "
            "windows across every regime, then repeat the locked five-fold "
            "selection protocol."
        )
    if not selection_stable:
        fold_counts = model_calibration["selection_fold_winner_counts"]
        stability_text = ", ".join(
            f"{name} {count}/{model_calibration['selection_folds']}"
            for name, count in sorted(fold_counts.items())
            if count
        )
        statement += (
            " Variance-method selection was unstable across folds "
            f"({stability_text})."
        )
        diagnosis += (
            " The selected method was not the fold-wise winner in every "
            "held-out fold, so the aggregate winner is a limitation rather "
            "than a robust method ranking."
        )
    verdict = {
        "mldp_meets_acceptance_bar": acceptance_bar,
        "mldp_beats_all_baselines": complete_all_win,
        "horizon_error_win_against_linear_autoregressor": linear_horizon_win,
        "variance_calibration_win_against_linear_autoregressor": linear_calibration_win,
        "event_f1_win_against_linear_autoregressor": linear_event_win,
        "horizon_error_win": all_horizon_win,
        "variance_calibration_win": all_calibration_win,
        "event_f1_win": all_event_win,
        "failed_against_linear_autoregressor": failed,
        "statement": statement,
        "diagnosis": diagnosis,
        "next_experiment": next_experiment,
        "selected_variance_method": calibration_method,
        "selection_decision": model_calibration["selection_decision"],
        "selection_stable_across_folds": selection_stable,
        "selection_fold_winner_counts": model_calibration[
            "selection_fold_winner_counts"
        ],
        "selection_calibration_nll_standard_error": model_calibration[
            "selection_calibration_nll_standard_error"
        ],
        "selection_nll_difference_standard_error": model_calibration[
            "selection_nll_difference_standard_error"
        ],
        "selection_split": model_calibration["selection_split"],
        "selection_calibration_nll": model_calibration[
            "selection_calibration_nll"
        ],
        "selection_runner_up": model_calibration["selection_runner_up"],
        "selection_nll_margin": model_calibration["selection_nll_margin"],
        "test_used_for_selection": False,
        "mean_accuracy_regressed_due_to_calibration": (
            False if mean_event_unchanged else None
        ),
        "event_f1_regressed_due_to_calibration": (
            False if mean_event_unchanged else None
        ),
    }

    model.save(destination / "mldp_reference.npz")
    _write_corpus(destination / "synthetic_corpus.npz", corpus)
    result = {
        "schema_version": 5,
        "seed": seed,
        "synthetic_windows": windows,
        "feature_spec_id": corpus.spec.feature_spec_id,
        "split_examples": {
            "train": len(train_idx),
            "calibration": len(calibration_idx),
            "test_locked": len(test_idx),
        },
        "regime_examples": {
            name: int(sum(regime == name for regime in test_regimes))
            for name in sorted(set(test_regimes))
        },
        "structure_diagnostics": diagnostics,
        "models": models,
        "verdict": verdict,
        "reproduction": {
            "device": "cpu",
            "python": platform.python_version(),
            "numpy": np.__version__,
            "machine": platform.machine(),
            "exact_policy": "metrics.json UTF-8 bytes and every checkpoint/corpus array must match",
            "portable_tolerance": "--atol compares numeric metric leaves and arrays at the requested absolute tolerance with rtol=0",
        },
    }
    (destination / "metrics.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_publication_manifest(destination)
    return result

def sample_size_sweep(
    out_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    development_sizes: tuple[int, ...] = DEFAULT_SWEEP_DEVELOPMENT_SIZES,
) -> dict[str, Any]:
    """Run the locked five-fold protocol over a deterministic sample-size ladder."""
    if not development_sizes:
        raise ValueError("sample-size sweep requires at least one development size")
    sizes = tuple(int(size) for size in development_sizes)
    if any(size <= 0 for size in sizes):
        raise ValueError("development sizes must be positive")
    if tuple(sorted(set(sizes))) != sizes:
        raise ValueError("development sizes must be strictly increasing and unique")

    rows: list[dict[str, Any]] = []
    for development_windows in sizes:
        total_windows = total_windows_for_development(development_windows)
        with tempfile.TemporaryDirectory(
            prefix=f"mldp-sweep-{development_windows}-"
        ) as temporary:
            result = reproduce(
                temporary,
                seed=seed,
                windows=total_windows,
            )
        calibration = result["models"]["mldp"]["calibration"]
        comparison = calibration["variance_method_comparison"]
        top_pair_tied = bool(
            calibration["selection_top_pair_statistically_tied"]
        )
        stable = bool(calibration["selection_stable_across_folds"])
        decisive = (
            float(calibration["selection_top_pair_nll_difference"])
            > float(
                calibration[
                    "selection_top_pair_difference_standard_error"
                ]
            )
        )
        rows.append(
            {
                "development_windows": development_windows,
                "total_windows": total_windows,
                "locked_test_windows": result["split_examples"][
                    "test_locked"
                ],
                "selection": {
                    "selected_method": calibration[
                        "selected_variance_method"
                    ],
                    "raw_nll_winner": calibration[
                        "selection_raw_nll_winner"
                    ],
                    "raw_nll_runner_up": calibration[
                        "selection_raw_nll_runner_up"
                    ],
                    "top_pair_nll_difference": calibration[
                        "selection_top_pair_nll_difference"
                    ],
                    "top_pair_difference_standard_error": calibration[
                        "selection_top_pair_difference_standard_error"
                    ],
                    "top_pair_statistically_tied": top_pair_tied,
                    "stable_across_folds": stable,
                    "decisive_and_stable": decisive and stable,
                    "fold_winners": calibration[
                        "selection_fold_winners"
                    ],
                    "fold_winner_counts": calibration[
                        "selection_fold_winner_counts"
                    ],
                    "test_used_for_selection": False,
                },
                "candidates": {
                    name: {
                        "mean_oof_nll": report["mean_oof_nll"],
                        "standard_error": report["standard_error"],
                        "fold_wins": report["fold_choice_count"],
                        "statistically_tied_with_raw_best": report[
                            "statistically_tied_with_raw_best"
                        ],
                        "test_variance_calibration_error": report[
                            "test_variance_calibration_error"
                        ],
                        "selected": report["selected"],
                    }
                    for name, report in comparison.items()
                },
                "models": {
                    name: {
                        "metrics": report["metrics"],
                        "per_regime": report.get("per_regime", {}),
                    }
                    for name, report in result["models"].items()
                },
                "verdict": result["verdict"],
            }
        )

    decisive_rows = [
        row
        for row in rows
        if row["selection"]["decisive_and_stable"]
    ]
    smallest_decisive = (
        decisive_rows[0]["development_windows"]
        if decisive_rows
        else None
    )
    sweep = {
        "schema_version": 1,
        "seed": seed,
        "selection_protocol": (
            "five_fold_cross_validation_over_development_train"
        ),
        "test_used_for_selection": False,
        "development_sizes": list(sizes),
        "default_total_windows": DEFAULT_WINDOWS,
        "default_development_windows": int(DEFAULT_WINDOWS * 0.8),
        "smallest_decisive_stable_development_windows": smallest_decisive,
        "sizes": rows,
    }
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "sample_size_sweep.json").write_text(
        json.dumps(sweep, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sweep


def _compare_npz(reference: Path, candidate: Path, *, atol: float) -> None:
    with np.load(reference, allow_pickle=False) as expected, np.load(candidate, allow_pickle=False) as actual:
        if set(expected.files) != set(actual.files):
            raise AssertionError(f"archive members differ for {reference.name}")
        for name in sorted(expected.files):
            left, right = expected[name], actual[name]
            if left.dtype.kind in "fc":
                equal = (
                    np.allclose(left, right, atol=atol, rtol=0.0, equal_nan=True)
                    if atol > 0
                    else np.array_equal(left, right, equal_nan=True)
                )
            else:
                equal = np.array_equal(left, right)
            if not equal:
                raise AssertionError(f"{reference.name}:{name} did not reproduce")


def _compare_json_values(expected: Any, actual: Any, *, path: str, atol: float) -> None:
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        if set(expected) != set(actual):
            raise AssertionError(f"JSON keys differ at {path}")
        for key in sorted(expected):
            _compare_json_values(expected[key], actual[key], path=f"{path}.{key}", atol=atol)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            raise AssertionError(f"JSON list lengths differ at {path}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            _compare_json_values(left, right, path=f"{path}[{index}]", atol=atol)
        return
    if (
        atol > 0
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
    ):
        if not np.isclose(expected, actual, atol=atol, rtol=0.0, equal_nan=True):
            raise AssertionError(f"numeric metric differs at {path}: {expected} != {actual}")
        return
    if expected != actual:
        raise AssertionError(f"value differs at {path}: {expected!r} != {actual!r}")


def verify(reference_dir: str | Path, *, atol: float = 0.0) -> None:
    reference = Path(reference_dir)
    metrics_path = reference / "metrics.json"
    if not metrics_path.is_file():
        raise FileNotFoundError(f"reference metrics not found: {metrics_path}")
    expected = json.loads(metrics_path.read_text(encoding="utf-8"))
    seed = int(expected["seed"])
    windows = int(expected["synthetic_windows"])
    with tempfile.TemporaryDirectory(prefix="mldp-verify-") as temporary:
        candidate = Path(temporary)
        reproduce(candidate, seed=seed, windows=windows)
        candidate_metrics = candidate / "metrics.json"
        if atol == 0:
            if metrics_path.read_bytes() != candidate_metrics.read_bytes():
                raise AssertionError("metrics.json is not bit-identical")
        else:
            _compare_json_values(
                expected,
                json.loads(candidate_metrics.read_text(encoding="utf-8")),
                path="$",
                atol=atol,
            )
        _compare_npz(
            reference / "mldp_reference.npz",
            candidate / "mldp_reference.npz",
            atol=atol,
        )
        _compare_npz(
            reference / "synthetic_corpus.npz",
            candidate / "synthetic_corpus.npz",
            atol=atol,
        )


def _print_summary(result: Mapping[str, Any]) -> None:
    horizons = tuple(
        int(key.removeprefix("horizon_").removesuffix("_rmse"))
        for key in result["models"]["mldp"]["metrics"]
        if key.startswith("horizon_") and key.endswith("_rmse")
    )
    horizons = tuple(sorted(horizons))
    diagnostics = result.get("structure_diagnostics")
    if diagnostics:
        autocorrelation = diagnostics["lag1_autocorrelation"]
        print(
            "structure lag1_autocorrelation "
            + " ".join(
                f"{channel}={float(value):.6f}"
                for channel, value in autocorrelation.items()
            )
        )
        for horizon, report in diagnostics["future_target_correlation_proxy"].items():
            print(
                f"structure {horizon} "
                f"command_max_abs_corr={float(report['command_max_abs_correlation']):.6f} "
                f"context_max_abs_corr={float(report['context_max_abs_correlation']):.6f}"
            )
        for horizon, report in diagnostics["held_out_variance_explained"].items():
            print(
                f"structure {horizon} variance_explained "
                f"constant={float(report['constant_r2']):.6f} "
                f"lag1={float(report['lag1_r2']):.6f} "
                f"command_conditioned={float(report['command_conditioned_r2']):.6f}"
            )
    for name, report in result["models"].items():
        metrics = report["metrics"]
        horizon_text = " ".join(
            f"h{h}:mae={metrics[f'horizon_{h}_mae']:.9f},rmse={metrics[f'horizon_{h}_rmse']:.9f}"
            for h in horizons
        )
        print(
            f"{name} {horizon_text} "
            f"variance_calibration_error={metrics['variance_calibration_error']:.9f} "
            f"coverage1={metrics['coverage_1sigma']:.9f} "
            f"coverage2={metrics['coverage_2sigma']:.9f} "
            f"event_precision={metrics['event_precision']:.9f} "
            f"event_recall={metrics['event_recall']:.9f} "
            f"event_f1={metrics['event_f1']:.9f}"
        )
        for regime, regime_metrics in report.get("per_regime", {}).items():
            regime_horizons = " ".join(
                f"h{h}:mae={regime_metrics[f'horizon_{h}_mae']:.9f},rmse={regime_metrics[f'horizon_{h}_rmse']:.9f}"
                for h in horizons
            )
            print(
                f"{name} regime={regime} n={regime_metrics['examples']} "
                f"{regime_horizons} "
                f"variance_calibration_error={regime_metrics['variance_calibration_error']:.9f} "
                f"coverage1={regime_metrics['coverage_1sigma']:.9f} "
                f"coverage2={regime_metrics['coverage_2sigma']:.9f} "
                f"event_f1={regime_metrics['event_f1']:.9f}"
            )
    calibration = result["models"]["mldp"]["calibration"]
    comparison = calibration.get("variance_method_comparison")
    if comparison:
        for method, report in sorted(
            comparison.items(),
            key=lambda item: float(item[1]["mean_oof_nll"]),
        ):
            fold_text = ",".join(
                f"{float(value):.9f}" for value in report["fold_nll"]
            )
            print(
                f"calibration_method {method} "
                f"fold_nll=[{fold_text}] "
                f"mean_nll={float(report['mean_oof_nll']):.9f} "
                f"se={float(report['standard_error']):.9f} "
                f"fold_choices={int(report['fold_choice_count'])}/"
                f"{calibration['selection_folds']} "
                f"tied_with_raw_best="
                f"{str(bool(report['statistically_tied_with_raw_best'])).lower()} "
                f"test_calibration_error="
                f"{float(report['test_variance_calibration_error']):.9f} "
                f"selected={str(bool(report['selected'])).lower()}"
            )
        print(
            "calibration_selection "
            f"winner={calibration['selected_variance_method']} "
            f"raw_top_pair={calibration['selection_raw_nll_winner']},"
            f"{calibration['selection_raw_nll_runner_up']} "
            f"decision={calibration['selection_decision']} "
            f"top_pair_difference="
            f"{float(calibration['selection_top_pair_nll_difference']):.9f} "
            f"top_pair_difference_se="
            f"{float(calibration['selection_top_pair_difference_standard_error']):.9f} "
            f"top_pair_tied="
            f"{str(bool(calibration['selection_top_pair_statistically_tied'])).lower()} "
            f"stable={str(bool(calibration['selection_stable_across_folds'])).lower()} "
            f"split={calibration['selection_split']} "
            f"test_used=false"
        )
    if "verdict" in result:
        print(f"verdict {result['verdict']['statement']}")
        if result["verdict"].get("next_experiment"):
            print(f"next_experiment {result['verdict']['next_experiment']}")

def _print_sweep_summary(sweep: Mapping[str, Any]) -> None:
    for row in sweep["sizes"]:
        selection = row["selection"]
        for method, report in sorted(
            row["candidates"].items(),
            key=lambda item: float(item[1]["mean_oof_nll"]),
        ):
            print(
                f"sweep development={row['development_windows']} "
                f"total={row['total_windows']} "
                f"test={row['locked_test_windows']} "
                f"method={method} "
                f"mean_nll={float(report['mean_oof_nll']):.9f} "
                f"se={float(report['standard_error']):.9f} "
                f"fold_wins={int(report['fold_wins'])}/5 "
                f"tied_with_raw_best="
                f"{str(bool(report['statistically_tied_with_raw_best'])).lower()} "
                f"test_calibration_error="
                f"{float(report['test_variance_calibration_error']):.9f}"
            )
        print(
            f"sweep_selection development={row['development_windows']} "
            f"selected={selection['selected_method']} "
            f"top_pair={selection['raw_nll_winner']},"
            f"{selection['raw_nll_runner_up']} "
            f"difference={float(selection['top_pair_nll_difference']):.9f} "
            f"difference_se="
            f"{float(selection['top_pair_difference_standard_error']):.9f} "
            f"tied={str(bool(selection['top_pair_statistically_tied'])).lower()} "
            f"stable={str(bool(selection['stable_across_folds'])).lower()} "
            f"test_used=false"
        )
    decisive = sweep["smallest_decisive_stable_development_windows"]
    print(
        "sweep_result smallest_decisive_stable_development="
        f"{decisive if decisive is not None else 'none'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduce or verify the CPU MLDP publication bundle")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="generate corpus, train, evaluate, and publish")
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run_parser.add_argument("--windows", type=int, default=DEFAULT_WINDOWS)
    sweep_parser = subparsers.add_parser(
        "sweep",
        help="opt in to the larger deterministic sample-size sweep",
    )
    sweep_parser.add_argument("--out", type=Path, required=True)
    sweep_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sweep_parser.add_argument(
        "--development-sizes",
        type=int,
        nargs="+",
        default=DEFAULT_SWEEP_DEVELOPMENT_SIZES,
    )
    verify_parser = subparsers.add_parser("verify", help="rerun and compare with a publication bundle")
    verify_parser.add_argument("--reference", type=Path, required=True)
    verify_parser.add_argument("--atol", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.command == "run":
        result = reproduce(args.out, seed=args.seed, windows=args.windows)
        _print_summary(result)
        print(f"REPRODUCE_OK out={args.out} seed={args.seed} windows={args.windows}")
        return 0
    if args.command == "sweep":
        sweep = sample_size_sweep(
            args.out,
            seed=args.seed,
            development_sizes=tuple(args.development_sizes),
        )
        _print_sweep_summary(sweep)
        print(
            f"SWEEP_OK out={args.out} seed={args.seed} "
            f"development_sizes={','.join(str(size) for size in args.development_sizes)}"
        )
        return 0
    if args.atol < 0:
        parser.error("--atol must be non-negative")
    verify(args.reference, atol=args.atol)
    result = json.loads((args.reference / "metrics.json").read_text(encoding="utf-8"))
    _print_summary(result)
    policy = "bit-identical" if args.atol == 0 else f"atol={args.atol:g},rtol=0"
    print(f"VERIFY_OK reference={args.reference} policy={policy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
