"""Dependency-free information measurements for observation/oracle comparisons."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class InformationReport:
    samples: int
    pearson_correlation: float
    mutual_information_bits: float
    estimate_mean: float
    oracle_mean: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "samples": self.samples,
            "pearson_correlation": self.pearson_correlation,
            "mutual_information_bits": self.mutual_information_bits,
            "estimate_mean": self.estimate_mean,
            "oracle_mean": self.oracle_mean,
        }


def quantify_information(
    visibility_estimates: object,
    oracle_visibility: object,
    *,
    bins: int = 8,
) -> InformationReport:
    """Measure dependence without treating the analytic label as policy input.

    Mutual information is the empirical discrete MI after both values are assigned to fixed,
    equal-width bins on [0, 1].  Fixed edges make reports comparable between information levels.
    """
    estimates = np.asarray(visibility_estimates, dtype=np.float64).reshape(-1)
    truth = np.asarray(oracle_visibility, dtype=np.float64).reshape(-1)
    if estimates.shape != truth.shape or len(estimates) < 2:
        raise ValueError("visibility estimates and oracle visibility need the same length >= 2")
    if not np.all(np.isfinite(estimates)) or not np.all(np.isfinite(truth)):
        raise ValueError("information inputs must be finite")
    if np.any(estimates < 0.0) or np.any(estimates > 1.0) or np.any(truth < 0.0) or np.any(truth > 1.0):
        raise ValueError("information inputs must be in [0, 1]")
    bins = int(bins)
    if bins < 2:
        raise ValueError("bins must be at least two")

    estimate_std = float(np.std(estimates))
    truth_std = float(np.std(truth))
    if estimate_std <= 1e-15 or truth_std <= 1e-15:
        correlation = 0.0
    else:
        correlation = float(np.corrcoef(estimates, truth)[0, 1])

    edges = np.linspace(0.0, 1.0, bins + 1)
    joint, _, _ = np.histogram2d(estimates, truth, bins=(edges, edges))
    joint /= joint.sum()
    estimate_probability = joint.sum(axis=1)
    truth_probability = joint.sum(axis=0)
    independent = estimate_probability[:, None] * truth_probability[None, :]
    occupied = joint > 0.0
    mutual_information = float(np.sum(joint[occupied] * np.log2(joint[occupied] / independent[occupied])))
    return InformationReport(
        samples=len(estimates),
        pearson_correlation=correlation,
        mutual_information_bits=mutual_information,
        estimate_mean=float(np.mean(estimates)),
        oracle_mean=float(np.mean(truth)),
    )
