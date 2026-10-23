"""Numpy inference for the ViewNet utility model v1 (any trainer must match it exactly).

Architecture — a set encoder over face tokens conditioned on the
set-global and context vectors:

    x_f = (face_tokens - norm_face_mean) / norm_face_std
    x_c = (concat(set_global, context) - norm_ctx_mean) / norm_ctx_std
    h_i = relu(relu(x_f_i @ face_w0 + face_b0) @ face_w1 + face_b1)      # 32 -> 64 -> 64
    c   = relu(relu(x_c @ ctx_w0 + ctx_b0) @ ctx_w1 + ctx_b1)            # 40 -> 64 -> 64
    a_i = (c @ att_wq) . h_i / 8 ; masked softmax over faces
    p   = sum_i softmax_i h_i ; m = max_i h_i over masked faces
    logit = relu(concat(p, m, c) @ out_w0 + out_b0) @ out_w1 + out_b1  # 192 -> 64 -> 1

Optional paired scalar arrays `hand_slope` and `hand_intercept` make this a residual model:
    score = logit + hand_slope * hand + hand_intercept
Legacy artifacts omit both scalars and retain the network-only logit. A nonzero slope
requires the caller's measured hand utility; it is never inferred from feature tokens.

Two edge rules the trainer mirrors: an EMPTY face mask gives p = m = 0, and a normalisation
std entry <= 1e-12 divides by 1. Compute is float64.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Optional

import numpy as np

from .features import DC, DF, DG, FEATURE_SCHEMA

HIDDEN = 64
DEFAULT_WEIGHTS = Path(__file__).resolve().parent / "weights" / "utility_v1.npz"

_SHAPES = {
    "face_w0": (DF, HIDDEN), "face_b0": (HIDDEN,),
    "face_w1": (HIDDEN, HIDDEN), "face_b1": (HIDDEN,),
    "ctx_w0": (DG + DC, HIDDEN), "ctx_b0": (HIDDEN,),
    "ctx_w1": (HIDDEN, HIDDEN), "ctx_b1": (HIDDEN,),
    "att_wq": (HIDDEN, HIDDEN),
    "out_w0": (3 * HIDDEN, HIDDEN), "out_b0": (HIDDEN,),
    "out_w1": (HIDDEN, 1), "out_b1": (1,),
    "norm_face_mean": (DF,), "norm_face_std": (DF,),
    "norm_ctx_mean": (DG + DC,), "norm_ctx_std": (DG + DC,),
}


class ModelSchemaMismatch(ValueError):
    """The weights were trained on a different feature schema than features.py produces."""


def _relu(x):
    return np.maximum(x, 0.0)


def _safe_std(s):
    return np.where(s > 1e-12, s, 1.0)


class UtilityModel:
    """A loaded utility_v1 model. `name` is "utility_v1@<sha8 of the npz bytes>"."""

    def __init__(self, arrays: dict, *, name: str, trained_on: str = "",
                 report_sha: str = "") -> None:
        residual_keys = ("hand_slope", "hand_intercept")
        present = [k in arrays for k in residual_keys]
        if any(present) and not all(present):
            raise ValueError("hand_slope and hand_intercept must both be present or absent")
        for k in residual_keys:
            value = np.asarray(arrays[k] if all(present) else 0.0, dtype=np.float64)
            if value.shape != () or not np.isfinite(value):
                raise ValueError(f"{k} must be a finite scalar")
            setattr(self, k, float(value))
        self.w = {k: np.asarray(arrays[k], dtype=np.float64) for k in _SHAPES}
        self.w["norm_face_std"] = _safe_std(self.w["norm_face_std"])
        self.w["norm_ctx_std"] = _safe_std(self.w["norm_ctx_std"])
        self.name = name
        self.trained_on = trained_on
        self.report_sha = report_sha

    @classmethod
    def load(cls, path) -> "UtilityModel":
        """Load and validate an npz. Raises ModelSchemaMismatch when its `feature_schema` is
        not FEATURE_SCHEMA, ValueError for a missing key or a wrong shape — a model that
        would read the features differently is never used."""
        raw = Path(path).read_bytes()
        sha8 = hashlib.sha256(raw).hexdigest()[:8]
        with np.load(io.BytesIO(raw), allow_pickle=False) as z:
            files = set(z.files)
            if "feature_schema" not in files:
                raise ModelSchemaMismatch(
                    f"{path}: no feature_schema in the weights; this build needs "
                    f"{FEATURE_SCHEMA!r}")
            schema = str(z["feature_schema"][()])
            if schema != FEATURE_SCHEMA:
                raise ModelSchemaMismatch(
                    f"{path}: trained on feature schema {schema!r}, this build produces "
                    f"{FEATURE_SCHEMA!r}")
            missing = sorted(k for k in _SHAPES if k not in files)
            if missing:
                raise ValueError(f"{path}: missing weight arrays {', '.join(missing)}")
            arrays = {k: np.asarray(z[k]) for k in _SHAPES}
            arrays.update({k: np.asarray(z[k]) for k in ("hand_slope", "hand_intercept")
                           if k in files})
            meta = {k: (str(z[k][()]) if k in files else "")
                    for k in ("trained_on", "report_sha")}
        for k, shape in _SHAPES.items():
            if tuple(arrays[k].shape) != shape:
                raise ValueError(f"{path}: {k} has shape {tuple(arrays[k].shape)}, "
                                 f"expected {shape}")
        return cls(arrays, name=f"utility_v1@{sha8}", **meta)

    def score(self, tokens: dict, *, hand=None) -> float:
        """One view-set logit plus the optional calibrated hand-utility residual."""
        residual = 0.0
        if self.hand_slope != 0.0:
            if hand is None:
                raise ValueError("nonzero hand_slope requires hand utility")
            value = np.asarray(hand, dtype=np.float64)
            if value.shape != () or not np.isfinite(value):
                raise ValueError("hand utility must be a finite scalar")
            residual = self.hand_slope * float(value)
        w = self.w
        ft = np.asarray(tokens["face_tokens"], dtype=np.float64).reshape(-1, DF)
        mask = np.asarray(tokens["face_mask"], dtype=bool).reshape(-1)
        xc = np.concatenate([np.asarray(tokens["set_global"], dtype=np.float64).reshape(DG),
                             np.asarray(tokens["context"], dtype=np.float64).reshape(DC)])
        xc = (xc - w["norm_ctx_mean"]) / w["norm_ctx_std"]
        c = _relu(_relu(xc @ w["ctx_w0"] + w["ctx_b0"]) @ w["ctx_w1"] + w["ctx_b1"])
        xf = (ft[mask] - w["norm_face_mean"]) / w["norm_face_std"]
        if xf.shape[0]:
            h = _relu(_relu(xf @ w["face_w0"] + w["face_b0"]) @ w["face_w1"] + w["face_b1"])
            a = (h @ (c @ w["att_wq"])) / 8.0
            e = np.exp(a - a.max())
            p = (e / e.sum()) @ h
            m = h.max(axis=0)
        else:
            p = np.zeros(HIDDEN)
            m = np.zeros(HIDDEN)
        z = _relu(np.concatenate([p, m, c]) @ w["out_w0"] + w["out_b0"])
        return float((z @ w["out_w1"] + w["out_b1"])[0]) + residual + self.hand_intercept

    def score_batch(self, tokens_list, *, hand=None) -> np.ndarray:
        """Score view sets with one aligned hand utility per set when supplied."""
        if hand is None:
            if self.hand_slope != 0.0:
                raise ValueError("nonzero hand_slope requires hand utility")
            return np.asarray([self.score(t) for t in tokens_list], dtype=np.float64)
        tokens_list = list(tokens_list)
        values = np.asarray(hand, dtype=np.float64)
        if values.shape != (len(tokens_list),):
            raise ValueError("hand batch must contain one scalar per token set")
        return np.asarray([self.score(t, hand=h) for t, h in zip(tokens_list, values)],
                          dtype=np.float64)


_LOADED: dict = {}


def load_default(path: Optional[Path] = None) -> tuple:
    """(model or None, note). Absent weights -> (None, "no trained weights at <path>"); a
    refused model -> (None, the refusal). Cached per (path, mtime, size)."""
    p = Path(path) if path is not None else DEFAULT_WEIGHTS
    try:
        st = p.stat()
    except OSError:
        return None, f"no trained utility weights at {p}"
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _LOADED:
        try:
            _LOADED.clear()
            _LOADED[key] = (UtilityModel.load(p), "")
        except Exception as e:  # noqa: BLE001 — a corrupt npz is a refusal, not a crash
            _LOADED[key] = (None, f"utility model refused: {e}")
    return _LOADED[key]
