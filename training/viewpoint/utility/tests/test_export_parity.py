import numpy as np
import pytest

torch = pytest.importorskip("torch")

from services.viewpoint.utility.features import FEATURE_SCHEMA, QUESTION_KINDS  # noqa: E402
from services.viewpoint.utility.model import UtilityModel  # noqa: E402

from training.viewpoint.utility.export import (  # noqa: E402
    ParityError, export_npz, stamp_report, verify_parity, weights_digest)
from training.viewpoint.utility.tests.synth import planted_dataset  # noqa: E402
from training.viewpoint.utility.train import (  # noqa: E402
    NORM_KEYS, WEIGHT_KEYS, TrainConfig, fit_utility, split_indices)


@pytest.fixture(scope="module")
def trained():
    ds = planted_dataset(n_sets=120, n_trials=600, seed=3, empty_sets=3)
    tr, va, _ = split_indices(ds)
    fit = fit_utility(ds, tr, va, seed=0, cfg=TrainConfig(lr=3e-3, max_epochs=8, patience=8,
                                                          device="cpu"))
    return ds, fit.net


def test_numpy_model_reproduces_torch_logits(trained, tmp_path):
    ds, net = trained
    npz = export_npz(net, tmp_path / "utility_v1.npz", trained_on="synthetic", report_sha="ab" * 32)
    all_sets = list(range(len(ds.face_tokens)))  # includes empty-mask and padded sets
    res = verify_parity(npz, net, ds, all_sets)
    assert res["max_abs_diff_float64"] <= 1e-5
    assert res["n_sets"] == len(all_sets)


def test_npz_layout_matches_contract(trained, tmp_path):
    ds, net = trained
    npz = export_npz(net, tmp_path / "u.npz", trained_on="synthetic run", report_sha="f" * 64)
    with np.load(npz, allow_pickle=False) as z:
        files = set(z.files)
        assert set(WEIGHT_KEYS + NORM_KEYS) <= files
        assert {"kind_bias", "feature_schema", "trained_on", "report_sha"} <= files
        assert z["kind_bias"].shape == (len(QUESTION_KINDS),)
        for k in ("feature_schema", "trained_on", "report_sha"):
            assert z[k].shape == () and z[k].dtype.kind == "U"
        assert str(z["feature_schema"][()]) == FEATURE_SCHEMA
    model = UtilityModel.load(str(npz))
    assert model.trained_on == "synthetic run"


def test_parity_catches_a_file_that_is_not_the_trained_net(trained, tmp_path):
    ds, net = trained
    npz = export_npz(net, tmp_path / "u.npz", trained_on="synthetic", report_sha="0" * 64)
    with np.load(npz, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    arrays["out_b1"] = arrays["out_b1"] + np.float32(1e-3)
    np.savez(npz, **arrays)
    with pytest.raises(ParityError):
        verify_parity(npz, net, ds, range(10))


def test_stamp_report_changes_only_the_report_sha(trained, tmp_path):
    ds, net = trained
    npz = export_npz(net, tmp_path / "u.npz", trained_on="synthetic", report_sha="1" * 64)
    before = weights_digest(npz)
    stamp_report(npz, "2" * 64)
    assert weights_digest(npz) == before
    assert UtilityModel.load(str(npz)).report_sha == "2" * 64
    verify_parity(npz, net, ds, range(10))
