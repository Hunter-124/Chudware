from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import numpy as np

if TYPE_CHECKING:
    from .data import WindowBatch

Array = np.ndarray


def horizon_errors(observed: Array, predicted: Array, horizons: Sequence[int]) -> dict[str, float]:
    observed = np.asarray(observed, np.float64)
    predicted = np.asarray(predicted, np.float64)
    if observed.shape != predicted.shape or observed.ndim < 2:
        raise ValueError("observed and predicted tensors must have equal [B,K,...] shapes")
    if observed.shape[1] != len(horizons):
        raise ValueError("horizon count does not match tensor")
    result: dict[str, float] = {}
    for index, horizon in enumerate(horizons):
        residual = observed[:, index] - predicted[:, index]
        result[f"horizon_{horizon}_mae"] = float(np.mean(np.abs(residual)))
        result[f"horizon_{horizon}_rmse"] = float(np.sqrt(np.mean(residual**2)))
    return result


def calibration_metrics(observed: Array, predicted: Array, log_variance: Array) -> dict[str, float]:
    """Gaussian variance calibration and empirical one/two-sigma coverage."""
    observed = np.asarray(observed, np.float64)
    predicted = np.asarray(predicted, np.float64)
    log_variance = np.asarray(log_variance, np.float64)
    if observed.shape != predicted.shape or observed.shape != log_variance.shape:
        raise ValueError("calibration tensors must have equal shapes")
    variance = np.exp(log_variance)
    if not np.isfinite(variance).all() or np.any(variance <= 0):
        raise ValueError("predicted variances must be finite and positive")
    squared = (observed - predicted) ** 2
    standardized_mse = float(np.mean(squared / variance))
    sigma = np.sqrt(variance)
    absolute = np.abs(observed - predicted)
    coverage_1 = float(np.mean(absolute <= sigma))
    coverage_2 = float(np.mean(absolute <= 2.0 * sigma))
    return {
        "variance_calibration_error": abs(standardized_mse - 1.0),
        "standardized_mse": standardized_mse,
        "coverage_1sigma": coverage_1,
        "coverage_2sigma": coverage_2,
        "coverage_1sigma_error": abs(coverage_1 - 0.682689492),
        "coverage_2sigma_error": abs(coverage_2 - 0.954499736),
    }


def event_metrics(labels: Array, scores: Array, *, threshold: float) -> dict[str, float | int]:
    labels = np.asarray(labels, bool)
    scores = np.asarray(scores, np.float64)
    if labels.shape != scores.shape or labels.ndim != 1:
        raise ValueError("event labels and scores must be equal one-dimensional arrays")
    if not np.isfinite(scores).all():
        raise ValueError("event scores must be finite")
    detected = scores >= threshold
    tp = int(np.sum(detected & labels))
    fp = int(np.sum(detected & ~labels))
    fn = int(np.sum(~detected & labels))
    tn = int(np.sum(~detected & ~labels))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "event_precision": float(precision),
        "event_recall": float(recall),
        "event_f1": float(2.0 * precision * recall / (precision + recall)) if precision + recall else 0.0,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
    }


def precursor_event_scores(event_logits: Array) -> Array:
    """Return one window-level precursor probability from the event boundary head."""
    logits = np.asarray(event_logits, np.float64)
    if logits.ndim != 3 or logits.shape[2] < 2:
        raise ValueError("event_logits must have shape [B,T,at least 2]")
    precursor_logit = np.max(logits[:, :, 1], axis=1)
    precursor_logit = np.clip(precursor_logit, -40.0, 40.0)
    return (1.0 / (1.0 + np.exp(-precursor_logit))).astype(np.float32)


def fit_event_threshold(normal_scores: Array, *, quantile: float = 0.99) -> float:
    values = np.asarray(normal_scores, np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("normal_scores must be a non-empty finite vector")
    if not 0.0 < quantile < 1.0:
        raise ValueError("quantile must be between zero and one")
    return float(np.quantile(values, quantile, method="higher"))


def _pearson(left: Array, right: Array) -> float:
    x = np.asarray(left, np.float64).reshape(-1)
    y = np.asarray(right, np.float64).reshape(-1)
    usable = np.isfinite(x) & np.isfinite(y)
    if int(usable.sum()) < 3:
        return 0.0
    x = x[usable] - np.mean(x[usable])
    y = y[usable] - np.mean(y[usable])
    denominator = float(np.sqrt(np.sum(x * x) * np.sum(y * y)))
    return float(np.sum(x * y) / denominator) if denominator > 0.0 else 0.0


def _linear_fit(design: Array, target: Array, *, ridge: float = 1.0e-6) -> Array:
    x = np.asarray(design, np.float64)
    y = np.asarray(target, np.float64)
    penalty = np.eye(x.shape[1], dtype=np.float64) * ridge
    penalty[-1, -1] = 0.0
    return np.linalg.solve(x.T @ x + penalty, x.T @ y)


def _variance_explained(observed: Array, predicted: Array, reference_mean: Array) -> float:
    target = np.asarray(observed, np.float64)
    residual = target - np.asarray(predicted, np.float64)
    centered = target - np.asarray(reference_mean, np.float64)
    total = float(np.sum(centered * centered))
    return float(1.0 - np.sum(residual * residual) / total) if total > 0.0 else 0.0


def structure_diagnostics(
    train: "WindowBatch",
    test: "WindowBatch",
    horizons: Sequence[int],
) -> dict[str, object]:
    """Measure temporal and command structure without fitting the benchmark models.

    Linear probes are fitted only on ``train`` and scored on ``test``.  The
    command-conditioned probe adds the command at the forecast horizon to the
    lag-1 state; it therefore measures incremental command information rather
    than leaking the future target.
    """
    if not horizons or train.target_latent.shape[1] != len(horizons):
        raise ValueError("horizons must match the target tensor")

    autocorrelation: dict[str, float] = {}
    camera = np.asarray(train.cam_feat, np.float64)
    camera_mask = np.asarray(train.cam_mask, bool)
    for channel in range(camera.shape[-1]):
        usable = camera_mask[:, :, 1:] & camera_mask[:, :, :-1]
        autocorrelation[f"camera_{channel}"] = _pearson(
            camera[:, :, :-1, channel][usable],
            camera[:, :, 1:, channel][usable],
        )
    process = np.asarray(train.proc_feat, np.float64)
    process_mask = np.asarray(train.proc_mask, bool)
    for channel in range(process.shape[-1]):
        usable = process_mask[:, 1:, channel] & process_mask[:, :-1, channel]
        autocorrelation[f"process_{channel}"] = _pearson(
            process[:, :-1, channel][usable],
            process[:, 1:, channel][usable],
        )
    command = np.asarray(train.cmd_future, np.float64)
    command_mask = np.asarray(train.cmd_mask, bool)
    for channel in range(command.shape[-1]):
        usable = command_mask[:, 1:] & command_mask[:, :-1]
        autocorrelation[f"command_{channel}"] = _pearson(
            command[:, :-1, channel][usable],
            command[:, 1:, channel][usable],
        )

    context = np.concatenate(
        (
            np.mean(camera[:, :, -1, :], axis=1),
            np.mean(process, axis=1),
            process[:, -min(3, process.shape[1]) :, :].reshape(process.shape[0], -1),
        ),
        axis=1,
    )
    correlation_proxy: dict[str, dict[str, float]] = {}
    for index, horizon in enumerate(horizons):
        target = np.mean(train.target_latent[:, index], axis=1)
        command_at_horizon = command[:, horizon - 1]
        command_correlations = [
            abs(_pearson(command_at_horizon[:, source], target[:, destination]))
            for source in range(command_at_horizon.shape[1])
            for destination in range(target.shape[1])
        ]
        context_correlations = [
            abs(_pearson(context[:, source], target[:, destination]))
            for source in range(context.shape[1])
            for destination in range(target.shape[1])
        ]
        correlation_proxy[f"horizon_{horizon}"] = {
            "command_max_abs_correlation": float(max(command_correlations, default=0.0)),
            "context_max_abs_correlation": float(max(context_correlations, default=0.0)),
        }

    train_current = np.asarray(
        train.cam_feat[:, :, -1, : train.target_latent.shape[-1]], np.float64
    )
    test_current = np.asarray(
        test.cam_feat[:, :, -1, : test.target_latent.shape[-1]], np.float64
    )
    examples_per_train = train_current.shape[0] * train_current.shape[1]
    examples_per_test = test_current.shape[0] * test_current.shape[1]
    variance_probes: dict[str, dict[str, float]] = {}
    aggregate_target: list[Array] = []
    aggregate_constant: list[Array] = []
    aggregate_lag1: list[Array] = []
    aggregate_conditioned: list[Array] = []
    for index, horizon in enumerate(horizons):
        train_target = np.asarray(train.target_latent[:, index], np.float64).reshape(
            examples_per_train, -1
        )
        test_target = np.asarray(test.target_latent[:, index], np.float64).reshape(
            examples_per_test, -1
        )
        constant = np.mean(train_target, axis=0, keepdims=True)
        train_lag = np.concatenate(
            (train_current.reshape(examples_per_train, -1), np.ones((examples_per_train, 1))),
            axis=1,
        )
        test_lag = np.concatenate(
            (test_current.reshape(examples_per_test, -1), np.ones((examples_per_test, 1))),
            axis=1,
        )
        lag_coefficient = _linear_fit(train_lag, train_target)
        lag_prediction = test_lag @ lag_coefficient
        train_command = np.repeat(
            train.cmd_future[:, horizon - 1, None, :], train_current.shape[1], axis=1
        ).reshape(examples_per_train, -1)
        test_command = np.repeat(
            test.cmd_future[:, horizon - 1, None, :], test_current.shape[1], axis=1
        ).reshape(examples_per_test, -1)
        train_conditioned = np.concatenate((train_lag[:, :-1], train_command, train_lag[:, -1:]), axis=1)
        test_conditioned = np.concatenate((test_lag[:, :-1], test_command, test_lag[:, -1:]), axis=1)
        conditioned_coefficient = _linear_fit(train_conditioned, train_target)
        conditioned_prediction = test_conditioned @ conditioned_coefficient
        constant_prediction = np.broadcast_to(constant, test_target.shape)
        report = {
            "constant_r2": _variance_explained(test_target, constant_prediction, constant),
            "lag1_r2": _variance_explained(test_target, lag_prediction, constant),
            "command_conditioned_r2": _variance_explained(
                test_target, conditioned_prediction, constant
            ),
        }
        report["lag1_gain_over_constant"] = report["lag1_r2"] - report["constant_r2"]
        report["command_gain_over_lag1"] = (
            report["command_conditioned_r2"] - report["lag1_r2"]
        )
        variance_probes[f"horizon_{horizon}"] = report
        aggregate_target.append(test_target)
        aggregate_constant.append(constant_prediction)
        aggregate_lag1.append(lag_prediction)
        aggregate_conditioned.append(conditioned_prediction)

    all_target = np.concatenate(aggregate_target)
    all_constant = np.concatenate(aggregate_constant)
    all_lag1 = np.concatenate(aggregate_lag1)
    all_conditioned = np.concatenate(aggregate_conditioned)
    variance_probes["overall"] = {
        "constant_r2": _variance_explained(all_target, all_constant, all_constant),
        "lag1_r2": _variance_explained(all_target, all_lag1, all_constant),
        "command_conditioned_r2": _variance_explained(
            all_target, all_conditioned, all_constant
        ),
    }
    return {
        "lag1_autocorrelation": autocorrelation,
        "future_target_correlation_proxy": correlation_proxy,
        "held_out_variance_explained": variance_probes,
    }


def regime_breakdown(
    observed: Array,
    predicted: Array,
    log_variance: Array,
    labels: Array,
    scores: Array,
    regimes: Sequence[str],
    *,
    threshold: float,
    horizons: Sequence[int],
) -> dict[str, dict[str, float | int]]:
    """Report the complete metric set for each non-empty held-out regime."""
    names = np.asarray(tuple(regimes), dtype=str)
    if names.shape != np.asarray(labels).shape:
        raise ValueError("regime names must align with event labels")
    result: dict[str, dict[str, float | int]] = {}
    for name in sorted(set(names.tolist())):
        selected = names == name
        report: dict[str, float | int] = {"examples": int(np.sum(selected))}
        report.update(horizon_errors(observed[selected], predicted[selected], horizons))
        report.update(
            calibration_metrics(
                observed[selected], predicted[selected], log_variance[selected]
            )
        )
        report.update(
            event_metrics(labels[selected], scores[selected], threshold=threshold)
        )
        report["mean_surprise_nll"] = float(np.mean(np.asarray(scores)[selected]))
        result[name] = report
    return result
