import json

from services.perception.reference import NumpyMLDP
from training.perception.evaluate import evaluate_model
from training.perception.synthetic import generate_synthetic_corpus
from training.perception.train import train_job


def test_cpu_train_entrypoint_is_seeded_and_writes_real_checkpoint(tmp_path):
    first = train_job({"seed": 101, "synthetic_windows": 32}, {}, tmp_path / "first")
    second = train_job({"seed": 101, "synthetic_windows": 32}, {}, tmp_path / "second")
    assert first == second
    checkpoint = tmp_path / "first" / "mldp_reference.npz"
    metrics_file = tmp_path / "first" / "metrics.json"
    assert checkpoint.stat().st_size > 0
    assert json.loads(metrics_file.read_text(encoding="utf-8"))["seed"] == 101
    model = NumpyMLDP.load(checkpoint)
    corpus = generate_synthetic_corpus(spec=model.spec, windows=16, seed=101)
    metrics = evaluate_model(model, corpus.windows, calibration=first["calibration"])
    assert "event_precision" in metrics
    assert "variance_calibration_error" in metrics
    assert all(f"horizon_{h}_rmse" in metrics for h in model.spec.horizons)
