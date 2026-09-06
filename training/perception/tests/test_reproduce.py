import json

import numpy as np
import pytest

from training.perception.baselines import fit_and_evaluate_baselines
from training.perception.reproduce import ARTIFACT_FILES, reproduce, verify
from training.perception.synthetic import generate_synthetic_corpus
from training.perception.train import _split_indices


def test_every_baseline_reports_horizons_calibration_and_events():
    corpus = generate_synthetic_corpus(windows=48, seed=23)
    train_idx, calibration_idx, test_idx = _split_indices(48, 23)
    reports = fit_and_evaluate_baselines(
        corpus.windows.subset(train_idx),
        corpus.windows.subset(calibration_idx),
        corpus.windows.subset(test_idx),
        corpus.spec,
    )
    assert set(reports) == {"persistence", "linear_autoregressor", "mean"}
    for report in reports.values():
        metrics = report["metrics"]
        assert all(f"horizon_{h}_mae" in metrics for h in corpus.spec.horizons)
        assert all(f"horizon_{h}_rmse" in metrics for h in corpus.spec.horizons)
        assert 0.0 <= metrics["coverage_1sigma"] <= 1.0
        assert 0.0 <= metrics["coverage_2sigma"] <= 1.0
        assert 0.0 <= metrics["event_precision"] <= 1.0
        assert 0.0 <= metrics["event_recall"] <= 1.0
        assert 0.0 <= metrics["event_f1"] <= 1.0


def test_reproduction_bundle_verifies_exactly_and_names_complete_release(tmp_path):
    # reproduce() fits the reference MLDP through torch autograd (see the same guard
    # in test_train_eval.py): torch is a training-only optional dependency, absent
    # from requirements.txt on purpose, so a service-venv-only box skips instead of
    # reporting a missing optional dependency as a product failure.
    pytest.importorskip(
        "torch",
        reason="the reproduction bundle trains the MLDP and needs the optional torch "
        "dependency (pip install torch — training-only, not in requirements.txt)",
    )
    destination = tmp_path / "release"
    result = reproduce(destination, seed=31, windows=48)
    verify(destination)
    assert set(result["models"]) == {
        "mldp",
        "persistence",
        "linear_autoregressor",
        "mean",
    }
    assert result["verdict"]["mldp_beats_all_baselines"] is False
    assert "held_out_variance_explained" in result["structure_diagnostics"]
    for report in result["models"].values():
        assert set(report["per_regime"]) == {
            "steady_cut",
            "material_change",
            "wear_ramp",
        }
    manifest = json.loads(
        (destination / "publication_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["release_files"] == list(ARTIFACT_FILES)
    assert set(manifest["release_checksums_sha256"]) == set(ARTIFACT_FILES[:-1])
    assert all((destination / name).is_file() for name in ARTIFACT_FILES)
    with np.load(destination / "synthetic_corpus.npz", allow_pickle=False) as corpus:
        assert corpus["cam_feat"].shape[0] == 48
