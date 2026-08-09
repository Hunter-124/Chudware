from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np

from services.perception.config import ModelSpec
from services.perception.reference import surprise_score
from .data import WindowBatch
from .metrics import (
    calibration_metrics,
    event_metrics,
    fit_event_threshold,
    horizon_errors,
    regime_breakdown,
)

Array = np.ndarray


class Baseline(Protocol):
    name: str
    spec: ModelSpec

    def predict(self, windows: WindowBatch) -> tuple[Array, Array]: ...


def _latest_latent(windows: WindowBatch, spec: ModelSpec) -> Array:
    return np.asarray(
        windows.cam_feat[:, :, -1, : spec.latent_features], dtype=np.float32
    )


def _constant_log_variance(observed: Array, predicted: Array) -> Array:
    residual = np.asarray(observed, np.float64) - np.asarray(predicted, np.float64)
    variance = np.mean(residual**2, axis=(0, 2)) + 1.0e-4
    shape = (1, variance.shape[0], 1, variance.shape[1])
    return np.log(variance).reshape(shape).astype(np.float32)


@dataclass(frozen=True)
class PersistencePredictor:
    spec: ModelSpec
    log_variance: Array
    name: str = "persistence"

    @classmethod
    def fit(cls, train: WindowBatch, spec: ModelSpec) -> "PersistencePredictor":
        current = _latest_latent(train, spec)
        prediction = np.repeat(current[:, None, :, :], len(spec.horizons), axis=1)
        return cls(spec, _constant_log_variance(train.target_latent, prediction))

    def predict(self, windows: WindowBatch) -> tuple[Array, Array]:
        windows.validate(self.spec)
        current = _latest_latent(windows, self.spec)
        mean = np.repeat(current[:, None, :, :], len(self.spec.horizons), axis=1)
        return mean, np.broadcast_to(self.log_variance, mean.shape)


@dataclass(frozen=True)
class MeanPredictor:
    spec: ModelSpec
    mean: Array
    log_variance: Array
    name: str = "mean"

    @classmethod
    def fit(cls, train: WindowBatch, spec: ModelSpec) -> "MeanPredictor":
        mean = np.mean(train.target_latent, axis=(0, 2), dtype=np.float64).astype(np.float32)
        prediction = np.broadcast_to(mean[None, :, None, :], train.target_latent.shape)
        return cls(spec, mean, _constant_log_variance(train.target_latent, prediction))

    def predict(self, windows: WindowBatch) -> tuple[Array, Array]:
        windows.validate(self.spec)
        shape = (
            len(windows.window_ids),
            len(self.spec.horizons),
            self.spec.cameras,
            self.spec.latent_features,
        )
        mean = np.broadcast_to(self.mean[None, :, None, :], shape)
        return mean, np.broadcast_to(self.log_variance, shape)


@dataclass(frozen=True)
class LinearAutoregressor:
    spec: ModelSpec
    coefficients: Array
    log_variance: Array
    ridge: float
    name: str = "linear_autoregressor"

    @classmethod
    def fit(
        cls,
        train: WindowBatch,
        spec: ModelSpec,
        *,
        ridge: float = 1.0e-3,
    ) -> "LinearAutoregressor":
        if ridge <= 0:
            raise ValueError("ridge must be positive")
        current = _latest_latent(train, spec)
        feature_count = spec.latent_features + spec.command_features + 1
        coefficients = np.empty(
            (len(spec.horizons), feature_count, spec.latent_features), np.float32
        )
        penalty = np.eye(feature_count, dtype=np.float64)
        penalty[-1, -1] = 0.0
        for index, horizon in enumerate(spec.horizons):
            command = np.repeat(
                train.cmd_future[:, horizon - 1, None, :], spec.cameras, axis=1
            )
            design = np.concatenate(
                (
                    current.astype(np.float64),
                    command.astype(np.float64),
                    np.ones((*current.shape[:2], 1), np.float64),
                ),
                axis=-1,
            ).reshape(-1, feature_count)
            target = train.target_latent[:, index].reshape(
                -1, spec.latent_features
            ).astype(np.float64)
            coefficients[index] = np.linalg.solve(
                design.T @ design + ridge * penalty, design.T @ target
            ).astype(np.float32)
        provisional = cls(
            spec,
            coefficients,
            np.zeros((1, len(spec.horizons), 1, spec.latent_features), np.float32),
            ridge,
        )
        prediction, _ = provisional.predict(train)
        return cls(
            spec,
            coefficients,
            _constant_log_variance(train.target_latent, prediction),
            ridge,
        )

    def predict(self, windows: WindowBatch) -> tuple[Array, Array]:
        windows.validate(self.spec)
        current = _latest_latent(windows, self.spec)
        feature_count = self.coefficients.shape[1]
        means = []
        for index, horizon in enumerate(self.spec.horizons):
            command = np.repeat(
                windows.cmd_future[:, horizon - 1, None, :], self.spec.cameras, axis=1
            )
            design = np.concatenate(
                (
                    current,
                    command,
                    np.ones((*current.shape[:2], 1), np.float32),
                ),
                axis=-1,
            ).reshape(-1, feature_count)
            means.append(
                (design @ self.coefficients[index]).reshape(
                    len(windows.window_ids), self.spec.cameras, self.spec.latent_features
                )
            )
        mean = np.stack(means, axis=1).astype(np.float32)
        return mean, np.broadcast_to(self.log_variance, mean.shape)


def evaluate_baseline(
    predictor: Baseline,
    calibration: WindowBatch,
    test: WindowBatch,
    *,
    regimes: Sequence[str] | None = None,
) -> dict[str, object]:
    calibration_mean, calibration_logvar = predictor.predict(calibration)
    calibration_scores = surprise_score(
        calibration.target_latent, calibration_mean, calibration_logvar
    )
    normal_scores = calibration_scores[~calibration.event_label]
    if normal_scores.size == 0:
        normal_scores = calibration_scores
    threshold = fit_event_threshold(normal_scores)

    predicted, log_variance = predictor.predict(test)
    scores = surprise_score(test.target_latent, predicted, log_variance)
    metrics: dict[str, float | int] = {}
    metrics.update(horizon_errors(test.target_latent, predicted, predictor.spec.horizons))
    metrics.update(calibration_metrics(test.target_latent, predicted, log_variance))
    metrics.update(event_metrics(test.event_label, scores, threshold=threshold))
    metrics["mean_surprise_nll"] = float(np.mean(scores))
    report: dict[str, object] = {
        "calibration": {
            "event_threshold": threshold,
            "normal_examples": int(normal_scores.size),
            "quantile": 0.99,
        },
        "metrics": metrics,
    }
    if regimes is not None:
        report["per_regime"] = regime_breakdown(
            test.target_latent,
            predicted,
            log_variance,
            test.event_label,
            scores,
            regimes,
            threshold=threshold,
            horizons=predictor.spec.horizons,
        )
    return report


def fit_and_evaluate_baselines(
    train: WindowBatch,
    calibration: WindowBatch,
    test: WindowBatch,
    spec: ModelSpec,
    *,
    test_regimes: Sequence[str] | None = None,
) -> dict[str, dict[str, object]]:
    predictors: tuple[Baseline, ...] = (
        PersistencePredictor.fit(train, spec),
        LinearAutoregressor.fit(train, spec),
        MeanPredictor.fit(train, spec),
    )
    return {
        predictor.name: evaluate_baseline(
            predictor, calibration, test, regimes=test_regimes
        )
        for predictor in predictors
    }
