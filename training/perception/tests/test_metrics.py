import numpy as np

from services.perception.reference import surprise_score
from training.perception.metrics import calibration_metrics, event_metrics, horizon_errors


def test_variance_calibration_is_exact_on_constructed_case():
    predicted = np.zeros((2, 1, 1, 2), np.float32)
    observed = np.array([[[[1.0, -1.0]]], [[[-1.0, 1.0]]]], np.float32)
    logvar = np.zeros_like(predicted)
    metrics = calibration_metrics(observed, predicted, logvar)
    assert metrics["standardized_mse"] == 1.0
    assert metrics["variance_calibration_error"] == 0.0


def test_event_precision_recall_detect_injected_anomalies():
    labels = np.array([False, True, False, True, False])
    observed = np.zeros((5, 1, 1, 2), np.float32)
    observed[labels] = 6.0
    predicted = np.zeros_like(observed)
    logvar = np.zeros_like(observed)
    scores = surprise_score(observed, predicted, logvar)
    metrics = event_metrics(labels, scores, threshold=5.0)
    assert metrics["event_precision"] == 1.0
    assert metrics["event_recall"] == 1.0
    assert metrics["false_positive"] == 0


def test_horizon_error_reports_each_horizon():
    observed = np.ones((2, 2, 1, 1), np.float32)
    predicted = np.zeros_like(observed)
    metrics = horizon_errors(observed, predicted, (1, 4))
    assert metrics == {
        "horizon_1_mae": 1.0, "horizon_1_rmse": 1.0,
        "horizon_4_mae": 1.0, "horizon_4_rmse": 1.0,
    }
