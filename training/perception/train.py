from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from services.perception.config import ModelSpec
from services.perception.reference import NumpyMLDP, surprise_score
from .evaluate import evaluate_model
from .synthetic import SyntheticCorpus, generate_synthetic_corpus

DEFAULT_SEED = 1729


def _split_indices(count: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    order = rng.permutation(count)
    train_end = max(1, int(count * 0.6))
    calib_end = max(train_end + 1, int(count * 0.8))
    return order[:train_end], order[train_end:calib_end], order[calib_end:]


def fit_reference_model(
    corpus: SyntheticCorpus,
    *,
    seed: int = DEFAULT_SEED,
    ridge: float = 1.0e-3,
) -> tuple[NumpyMLDP, dict[str, float]]:
    """Fit the forecast and variance heads by deterministic ridge regression."""
    if ridge <= 0:
        raise ValueError("ridge must be positive")
    model = NumpyMLDP(corpus.spec, seed=seed)
    train_idx, calib_idx, _ = _split_indices(len(corpus.windows.window_ids), seed)
    train = corpus.windows.subset(train_idx)
    _, contexts, _ = model.prediction_contexts(train.model_inputs())
    h = corpus.spec.hidden
    identity = np.eye(h + 1, dtype=np.float64)
    identity[-1, -1] = 0.0
    for k_index in range(len(corpus.spec.horizons)):
        x = np.concatenate(
            [
                contexts[:, k_index].reshape(-1, h).astype(np.float64),
                np.ones((len(train_idx) * corpus.spec.cameras, 1)),
            ],
            axis=1,
        )
        y = train.target_latent[:, k_index].reshape(
            -1, corpus.spec.latent_features
        ).astype(np.float64)
        coefficient = np.linalg.solve(x.T @ x + ridge * identity, x.T @ y)
        model.parameters["forecast_w"][k_index] = coefficient[:-1].astype(np.float32)
        model.parameters["forecast_b"][k_index] = coefficient[-1].astype(np.float32)
    prediction = model.forward(train.model_inputs())
    residual = train.target_latent - prediction["z_hat"]
    variance = np.mean(residual**2, axis=(0, 2)) + 1.0e-4
    model.parameters["logvar_w"].fill(0.0)
    model.parameters["logvar_b"][:] = np.log(variance).astype(np.float32)

    calibration = corpus.windows.subset(calib_idx)
    calibration_output = model.forward(calibration.model_inputs())
    scores = surprise_score(
        calibration.target_latent,
        calibration_output["z_hat"],
        calibration_output["z_logvar"],
    )
    normal = scores[~calibration.event_label]
    if normal.size == 0:
        normal = scores
    center = float(np.quantile(normal, 0.99, method="higher"))
    mad = float(np.median(np.abs(normal - np.median(normal))))
    scale = max(mad * 1.4826, 1.0e-3)
    return model, {"surprise_center": center, "surprise_scale": scale}


def train_job(
    job: Mapping[str, Any] | None = None,
    recipe: Mapping[str, Any] | None = None,
    out_dir: str | Path = "scratch/perception",
) -> dict[str, Any]:
    """Train/evaluate a tiny CPU model; compatible with the flywheel call shape."""
    job, recipe = dict(job or {}), dict(recipe or {})
    seed = int(job.get("seed", recipe.get("seed", DEFAULT_SEED)))
    count = int(job.get("synthetic_windows", recipe.get("synthetic_windows", 96)))
    corpus = generate_synthetic_corpus(windows=count, seed=seed)
    model, calibration = fit_reference_model(corpus, seed=seed)
    _, _, test_idx = _split_indices(count, seed)
    metrics = evaluate_model(model, corpus.windows.subset(test_idx), calibration=calibration)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    model.save(destination / "mldp_reference.npz")
    result = {
        "seed": seed,
        "feature_spec_id": model.spec.feature_spec_id,
        "calibration": calibration,
        "metrics": metrics,
    }
    (destination / "metrics.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the tiny deterministic MLDP reference model")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--windows", type=int, default=96)
    args = parser.parse_args(argv)
    result = train_job({"seed": args.seed, "synthetic_windows": args.windows}, {}, args.out)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
