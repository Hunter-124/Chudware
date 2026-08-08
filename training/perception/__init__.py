"""Forecast-to-Compress training and evaluation core."""

from .data import WindowBatch, WINDOW_SCHEMA
from .manifest import ManifestRecord, validate_manifest
from .metrics import calibration_metrics, event_metrics, horizon_errors
from .synthetic import SyntheticCorpus, generate_synthetic_corpus

__all__ = [
    "ManifestRecord",
    "SyntheticCorpus",
    "WINDOW_SCHEMA",
    "WindowBatch",
    "calibration_metrics",
    "event_metrics",
    "generate_synthetic_corpus",
    "horizon_errors",
    "validate_manifest",
]
