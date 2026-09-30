"""NPZ export of a trained UtilityNet (contract §4) + numpy/torch parity gate.

Weights are stored float32 exactly as trained; the numpy runtime computes in float64. The
parity gate evaluates the SAME stored weights through torch in float64 and through
``services.viewpoint.utility.model.UtilityModel`` and refuses the export when any set logit
differs by more than 1e-5 — a mismatch means inference is a different function from the one
that was trained, which no amount of accuracy upstream can fix.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import numpy as np
import torch

from services.viewpoint.utility.features import DG, FEATURE_SCHEMA
from training.viewpoint.durable import durable_replace

from .train import NORM_KEYS, WEIGHT_KEYS, UtilityNet, set_logits

PARITY_ATOL = 1e-5


class ParityError(AssertionError):
    """numpy UtilityModel does not reproduce the torch logits."""


def _save_atomic(path: Path, arrays: dict) -> None:
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, **arrays)
    durable_replace(tmp, path)


def npz_arrays(net: UtilityNet, *, trained_on: str, report_sha: str) -> dict:
    arrays = {k: getattr(net, k).detach().cpu().numpy().astype(np.float32)
              for k in WEIGHT_KEYS + NORM_KEYS}
    arrays["kind_bias"] = net.kind_bias.detach().cpu().numpy().astype(np.float32)
    arrays["feature_schema"] = np.array(FEATURE_SCHEMA)
    arrays["trained_on"] = np.array(str(trained_on))
    arrays["report_sha"] = np.array(str(report_sha))
    return arrays


def export_npz(net: UtilityNet, path, *, trained_on: str, report_sha: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_atomic(path, npz_arrays(net, trained_on=trained_on, report_sha=report_sha))
    return path


def stamp_report(path, report_sha: str) -> None:
    """Replace the NPZ's report_sha, leaving every weight byte untouched."""
    path = Path(path)
    with np.load(path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    arrays["report_sha"] = np.array(str(report_sha))
    _save_atomic(path, arrays)


def weights_digest(path) -> str:
    """sha256 of the weight/norm/kind_bias arrays + schema — stable across stamp_report."""
    h = hashlib.sha256()
    with np.load(Path(path), allow_pickle=False) as z:
        for k in WEIGHT_KEYS + NORM_KEYS + ("kind_bias",):
            a = np.ascontiguousarray(z[k])
            h.update(k.encode())
            h.update(str(a.dtype).encode())
            h.update(str(a.shape).encode())
            h.update(a.tobytes())
        h.update(str(z["feature_schema"][()]).encode())
    return h.hexdigest()


def verify_parity(npz_path, net: UtilityNet, ds, set_ids, *, atol: float = PARITY_ATOL) -> dict:
    """Score `set_ids` of dataset `ds` with numpy UtilityModel (reading `npz_path`) and with
    the trained torch `net` evaluated in float64 (its float32 parameters upcast exactly);
    raise ParityError when any logit differs by more than `atol`. This catches both a file
    that does not hold the trained weights and an inference that computes a different
    function. The float32 (as-trained) difference is reported alongside."""
    from services.viewpoint.utility.model import UtilityModel  # noqa: PLC0415

    set_ids = [int(s) for s in set_ids]
    if not set_ids:
        raise ParityError("parity check needs at least one view set")
    model = UtilityModel.load(str(npz_path))
    np_logits = model.score_batch([ds.token_dict(s, DG) for s in set_ids])
    cpu = torch.device("cpu")
    t64 = set_logits(copy.deepcopy(net).double().cpu(), ds, set_ids, device=cpu,
                     dtype=torch.float64)
    t32 = set_logits(copy.deepcopy(net).float().cpu(), ds, set_ids, device=cpu,
                     dtype=torch.float32)
    d64 = float(np.max(np.abs(np_logits - t64)))
    d32 = float(np.max(np.abs(np_logits - t32)))
    result = {"n_sets": len(set_ids), "max_abs_diff_float64": d64,
              "max_abs_diff_float32_trained": d32, "atol": atol,
              "npz_sha256": hashlib.sha256(Path(npz_path).read_bytes()).hexdigest()}
    if not np.isfinite(d64) or d64 > atol:
        raise ParityError(f"numpy UtilityModel vs torch float64 logits differ by {d64:.3g} "
                          f"(> {atol}) on {len(set_ids)} sets: {npz_path}")
    return result
