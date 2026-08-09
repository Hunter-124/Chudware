from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

import numpy as np

from services.perception.reference import NumpyMLDP, surprise_score
from .data import WindowBatch
from .metrics import (
    calibration_metrics,
    event_metrics,
    horizon_errors,
    precursor_event_scores,
)
from .synthetic import generate_synthetic_corpus


def evaluate_model(
    model: NumpyMLDP,
    windows: WindowBatch,
    *,
    calibration: Mapping[str, float],
) -> dict[str, float | int]:
    windows.validate(model.spec)
    output = model.forward(windows.model_inputs())
    surprise = surprise_score(
        windows.target_latent, output["z_hat"], output["z_logvar"]
    )
    if "event_threshold" in calibration:
        event_score = precursor_event_scores(output["event_bnd"])
        event_threshold = float(calibration["event_threshold"])
    else:
        event_score = surprise
        event_threshold = float(calibration["surprise_center"])
    result: dict[str, float | int] = {}
    result.update(
        horizon_errors(
            windows.target_latent, output["z_hat"], model.spec.horizons
        )
    )
    result.update(
        calibration_metrics(
            windows.target_latent, output["z_hat"], output["z_logvar"]
        )
    )
    result.update(
        event_metrics(
            windows.event_label,
            event_score,
            threshold=event_threshold,
        )
    )
    result["mean_surprise_nll"] = float(np.mean(surprise))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate an MLDP checkpoint on deterministic synthetic data")
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--windows", type=int, default=96)
    parser.add_argument("--surprise-center", type=float, required=True)
    parser.add_argument("--surprise-scale", type=float, default=1.0)
    args = parser.parse_args(argv)
    model = NumpyMLDP.load(args.checkpoint)
    corpus = generate_synthetic_corpus(spec=model.spec, windows=args.windows, seed=args.seed)
    metrics = evaluate_model(
        model,
        corpus.windows,
        calibration={"surprise_center": args.surprise_center, "surprise_scale": args.surprise_scale},
    )
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
