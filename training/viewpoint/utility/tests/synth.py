"""Synthetic labelled-pair datasets with a planted signal (no engine, no observer).

Tokens follow the features.py layout (see build_set_tokens) instead of iid noise: a part has
fixed face geometry (area, surface type, normal, roles) shared by all its view sets, and each
set only changes the per-face visibility columns. With iid N(0,1) in every column a set
carries a unique high-dimensional fingerprint, which a 64-wide max-pooled set encoder
memorises from a few hundred sets long before it finds a one-column signal — unlike real
tokens, which are mostly one-hots, flags and bounded fractions.
"""
from __future__ import annotations

import numpy as np

from services.viewpoint.utility.features import DC, DF, DG, N_SURF, TARGET_KINDS, legibility

from training.viewpoint.utility.store import TrialDataset

THRESHOLD = 0.3           # worst target coverage at which a trial is a coin flip (before kind)


def _part(rng) -> dict:
    n = int(rng.integers(4, 16))
    area = rng.lognormal(-1.2, 0.7, size=n)
    normal = rng.normal(size=(n, 3))
    normal /= np.linalg.norm(normal, axis=1, keepdims=True)
    target = np.zeros(n, bool)
    target[rng.choice(n, size=int(rng.integers(1, 4)), replace=False)] = True
    context = ~target & (rng.random(n) < 0.3)
    bc = rng.random(n) < 0.1
    size = np.sort(rng.uniform(0.2, 1.0, size=3))[::-1]
    ctx = np.zeros(DC, np.float32)
    ctx[0] = np.log1p(n)
    ctx[1] = np.log1p(1.0)
    ctx[2:5] = size / size[0]
    ctx[5 + TARGET_KINDS.index("face")] = 1.0
    ctx[11] = float(rng.random() < 0.2)
    ctx[12] = float(bc.any())
    ctx[13] = np.log1p(np.linalg.norm(size))
    ctx[14] = 1.0
    return {"n": n, "area": area, "surf": rng.integers(0, N_SURF, size=n), "normal": normal,
            "target": target, "context": context, "bc": bc,
            "tri": rng.integers(2, 400, size=n), "dist": rng.uniform(0.0, 1.0, size=n),
            "ctx": ctx}


def _set_tokens(rng, part: dict) -> tuple:
    """(face tokens [n, DF], set_global [DG], worst legible coverage over the target faces)."""
    n = part["n"]
    tok = np.zeros((n, DF), np.float64)
    tok[:, 0] = np.log1p(part["area"])
    tok[np.arange(n), 1 + part["surf"]] = 1.0
    tok[:, 8], tok[:, 9], tok[:, 10] = part["target"], part["context"], part["bc"]
    tok[:, 11:14] = part["normal"]
    tok[:, 24] = np.log1p(part["tri"])
    tok[:, 25] = part["area"] / part["area"].sum()
    tok[:, 26] = part["dist"]
    n_views = int(rng.integers(1, 5))
    seen = rng.random(n) < 0.9
    frac = np.where(seen, rng.beta(2.0, 1.0, size=n), 0.0)
    cos = np.where(seen, rng.uniform(0.15, 1.0, size=n), 0.0)
    cov = legibility(cos) * frac
    tok[:, 14] = frac
    tok[:, 15] = cos
    tok[:, 16] = np.where(seen, rng.integers(1, n_views + 1, size=n), 0)
    tok[:, 17] = 1.0 - frac
    tok[:, 18] = cov
    tok[:, 19] = np.where(seen, np.log1p(frac * part["area"] * 2e4), 0.0)
    tok[:, 20] = np.where(seen, rng.uniform(0.0, 0.5, size=n), 0.0)
    tok[:, 21] = np.clip(frac + rng.normal(0.0, 0.1, size=n), 0.0, 1.0) * seen
    tok[:, 22] = np.where(seen, rng.random(n) < 0.3, 0.0)
    tok[:, 23] = np.where(seen, rng.uniform(2.0, 6.0, size=n), 0.0)
    tok[:, 29] = cos * frac
    sg = np.zeros(DG, np.float64)
    sg[0] = n_views / 4.0
    sg[1] = float((part["area"] * cov).sum() / part["area"].sum())
    sg[5:7] = np.sort(rng.uniform(0.2, 0.9, size=2))
    sg[7] = rng.uniform(0.3, 1.0)
    sg[11:13] = rng.uniform(6.0, 10.0, size=2)
    sg[13:17] = rng.uniform(0.0, 1.0, size=4)
    sg[17] = rng.uniform(0.0, 0.5) if n_views > 1 else 0.0
    sg[22] = rng.uniform(0.2, 0.8)
    sg[23] = rng.uniform(-0.5, 0.8)
    return tok, sg, float(cov[part["target"]].min())


def planted_dataset(*, n_sets: int = 320, n_trials: int = 1600, sets_per_part: int = 4,
                    seed: int = 0, strength: float = 8.0, empty_sets: int = 0,
                    label_noise: float = 0.3) -> TrialDataset:
    """True P(correct) = sigmoid(strength * (worst legible coverage (column 18) among the
    VISIBLE target faces (column 8) - THRESHOLD) + kind offset): the reader fails when any
    target face is poorly seen by the whole set. The soft target ``y`` is an observer's
    estimate of it (N(0, label_noise) on the logit) and ``correct`` a draw from that
    estimate, as the observer's argmax would be. set_global deliberately omits the target
    coverage, so the signal is reachable only through the masked face pooling. Padded rows
    are target faces that no view sees (and carry 6.0 in column 0), so a model that ignores
    the mask learns the wrong thing. Sets of one part share its face geometry; splits are by
    part (70 % train / 30 % val). The first `empty_sets` sets have no visible face."""
    rng = np.random.default_rng(seed)
    ft, fm, cx = [], [], []
    signal = np.zeros(n_sets)
    parts: dict = {}
    for s in range(n_sets):
        if s // sets_per_part not in parts:
            parts[s // sets_per_part] = _part(rng)
        part = parts[s // sets_per_part]
        tok, sg, worst = _set_tokens(rng, part)
        pad = int(rng.integers(0, 4))
        padded = np.zeros((pad, DF), np.float64)
        padded[:, 0] = 6.0
        padded[:, 8] = 1.0
        padded[:, 17] = 1.0
        mask = np.zeros(part["n"] + pad, bool)
        mask[:part["n"]] = s >= empty_sets
        ft.append(np.concatenate([tok, padded]).astype(np.float32))
        fm.append(mask)
        cx.append(np.concatenate([sg, part["ctx"]]).astype(np.float32))
        signal[s] = worst if mask.any() else THRESHOLD
    set_index = rng.integers(0, n_sets, size=n_trials)
    kind = rng.integers(0, 4, size=n_trials)
    kind_offset = np.array([0.3, -0.2, 0.0, 0.5])
    logit = strength * (signal[set_index] - THRESHOLD) + kind_offset[kind]
    y = 1.0 / (1.0 + np.exp(-(logit + rng.normal(0.0, label_noise, size=n_trials))))
    correct = (rng.random(n_trials) < y).astype(np.float32)
    part = set_index // sets_per_part
    n_parts = int(part.max()) + 1
    val_parts = set(rng.permutation(n_parts)[: int(0.3 * n_parts)].tolist())
    splits = np.array(["val" if int(pp) in val_parts else "train" for pp in part], object)
    return TrialDataset(
        set_keys=[f"s{s}" for s in range(n_sets)], face_tokens=ft, face_mask=fm, ctx=cx,
        set_index=set_index.astype(np.int64), kind=kind.astype(np.int64),
        y=y.astype(np.float32), correct=correct,
        hand=rng.normal(size=n_trials), chance=np.full(n_trials, 0.25),
        part_ids=np.array([f"p{int(pp)}" for pp in part], object), splits=splits,
        strategies=np.array(["random"] * n_trials, object),
        ks=np.full(n_trials, 4, np.int64), modes=np.array(["part"] * n_trials, object),
        folds=(part % 5).astype(np.int64),
        rows=[{"pair_id": f"t{i}", "set_key": f"s{int(set_index[i])}", "qid": f"q{i}",
               "strategies": ["random"], "k": 4} for i in range(n_trials)])
