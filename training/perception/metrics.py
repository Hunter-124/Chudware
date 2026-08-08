from __future__ import annotations

from typing import Sequence

import numpy as np

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


def fit_event_threshold(normal_scores: Array, *, quantile: float = 0.99) -> float:
    values = np.asarray(normal_scores, np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("normal_scores must be a non-empty finite vector")
    if not 0.0 < quantile < 1.0:
        raise ValueError("quantile must be between zero and one")
    return float(np.quantile(values, quantile, method="higher"))
