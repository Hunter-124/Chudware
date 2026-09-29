"""Torch trainer for the ViewNet utility model (contract §4 architecture, exactly).

The network is written with raw ``[in, out]`` parameter matrices named like the NPZ keys, so
export is a straight copy and the numpy ``UtilityModel`` computes the same function. Pair
logit = set utility logit + ``kind_bias[query kind]``: the intercepts absorb how hard a query
kind is, leaving the set logit to explain what the PICTURES contribute. Inference ignores
``kind_bias``.

Targets are soft: the cross-fitted observer's P(correct) for (view set, query), fitted with
BCE against that probability. Every set carries a unique high-dimensional token fingerprint,
which the set encoder memorises long before it finds the signal; regularisation is dropout
on the face-token embeddings (training only, never at inference) plus AdamW weight decay,
chosen together on val log-loss (``select_regularisation``).

Baselines trained on the same pairs: a logistic regression on mean/max pooled tokens and a
calibrated hand_utility (1-D logistic on the hand score); both also get per-kind intercepts
so every comparison isolates the view-set term.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import torch
from torch import nn

from services.viewpoint.utility.features import DC, DF, DG, FEATURE_SCHEMA, QUESTION_KINDS

from .store import TrialDataset, TrialStore, load_dataset

HIDDEN = 64
DCTX = DG + DC
WEIGHT_KEYS = ("face_w0", "face_b0", "face_w1", "face_b1", "ctx_w0", "ctx_b0", "ctx_w1",
               "ctx_b1", "att_wq", "out_w0", "out_b0", "out_w1", "out_b1")
NORM_KEYS = ("norm_face_mean", "norm_face_std", "norm_ctx_mean", "norm_ctx_std")
_EPS = 1e-7


def resolve_device(device: str = "auto") -> torch.device:
    if device == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(device)


# ---------------------------------------------------------------------------- model

class UtilityNet(nn.Module):
    def __init__(self, n_kinds: int = len(QUESTION_KINDS)):
        super().__init__()
        shapes = {
            "face_w0": (DF, HIDDEN), "face_b0": (HIDDEN,), "face_w1": (HIDDEN, HIDDEN),
            "face_b1": (HIDDEN,), "ctx_w0": (DCTX, HIDDEN), "ctx_b0": (HIDDEN,),
            "ctx_w1": (HIDDEN, HIDDEN), "ctx_b1": (HIDDEN,), "att_wq": (HIDDEN, HIDDEN),
            "out_w0": (3 * HIDDEN, HIDDEN), "out_b0": (HIDDEN,), "out_w1": (HIDDEN, 1),
            "out_b1": (1,),
        }
        # nn.Linear's default: U(-1/sqrt(fan_in), 1/sqrt(fan_in)) for weights and biases.
        fan_in = {"face_w0": DF, "face_b0": DF, "ctx_w0": DCTX, "ctx_b0": DCTX,
                  "out_w0": 3 * HIDDEN, "out_b0": 3 * HIDDEN}
        for name, shape in shapes.items():
            bound = 1.0 / math.sqrt(fan_in.get(name, HIDDEN))
            self.register_parameter(name, nn.Parameter(torch.empty(shape).uniform_(-bound, bound)))
        self.kind_bias = nn.Parameter(torch.zeros(n_kinds))
        self.register_buffer("norm_face_mean", torch.zeros(DF))
        self.register_buffer("norm_face_std", torch.ones(DF))
        self.register_buffer("norm_ctx_mean", torch.zeros(DCTX))
        self.register_buffer("norm_ctx_std", torch.ones(DCTX))

    def set_norm(self, face_mean, face_std, ctx_mean, ctx_std) -> None:
        for name, val in (("norm_face_mean", face_mean), ("norm_face_std", face_std),
                          ("norm_ctx_mean", ctx_mean), ("norm_ctx_std", ctx_std)):
            getattr(self, name).copy_(torch.as_tensor(np.asarray(val), dtype=getattr(self, name).dtype))

    def base_logit(self, ft: torch.Tensor, fm: torch.Tensor, ctx: torch.Tensor, *,
                   face_dropout: float = 0.0) -> torch.Tensor:
        """Set utility logit. ft [B,F,DF], fm bool [B,F], ctx [B,DG+DC] -> [B].
        `face_dropout` (inverted dropout on each face token's first-layer embedding) acts
        only in train mode; in eval mode this is exactly the numpy inference function."""
        relu = torch.relu
        xf = (ft - self.norm_face_mean) / self.norm_face_std
        xc = (ctx - self.norm_ctx_mean) / self.norm_ctx_std
        h = relu(xf @ self.face_w0 + self.face_b0)
        if face_dropout > 0.0:
            h = nn.functional.dropout(h, face_dropout, training=self.training)
        h = relu(h @ self.face_w1 + self.face_b1)
        c = relu(relu(xc @ self.ctx_w0 + self.ctx_b0) @ self.ctx_w1 + self.ctx_b1)
        a = torch.einsum("bfh,bh->bf", h, c @ self.att_wq) / 8.0
        maskf = fm.to(h.dtype)
        a = a.masked_fill(~fm, float("-inf"))
        amax = a.max(dim=1, keepdim=True).values
        amax = torch.where(torch.isfinite(amax), amax, torch.zeros_like(amax)).detach()
        e = torch.exp(a - amax) * maskf
        w = e / e.sum(dim=1, keepdim=True).clamp_min(1e-30)
        p = torch.einsum("bf,bfh->bh", w, h)
        # h >= 0 after relu, so zeroing masked rows leaves the masked max intact and gives
        # m = 0 for an empty mask — the numpy rule.
        m = (h * maskf.unsqueeze(-1)).max(dim=1).values
        z = relu(torch.cat([p, m, c], dim=1) @ self.out_w0 + self.out_b0)
        return (z @ self.out_w1 + self.out_b1).squeeze(-1)


# ---------------------------------------------------------------------------- data

def norm_stats(ds: TrialDataset, set_ids) -> tuple:
    """Normalisation from the TRAIN sets only; degenerate std entries divide by 1."""
    set_ids = sorted(set(int(s) for s in set_ids))
    faces = [ds.face_tokens[s][ds.face_mask[s]] for s in set_ids]
    faces = [f for f in faces if f.shape[0]]
    allf = (np.concatenate(faces).astype(np.float64) if faces
            else np.zeros((1, DF), np.float64))
    ctx = np.stack([ds.ctx[s] for s in set_ids]).astype(np.float64)
    fstd, cstd = allf.std(0), ctx.std(0)
    return (allf.mean(0), np.where(fstd > 1e-6, fstd, 1.0),
            ctx.mean(0), np.where(cstd > 1e-6, cstd, 1.0))


def collate(ds: TrialDataset, set_ids, device, dtype=torch.float32) -> tuple:
    set_ids = [int(s) for s in set_ids]
    fmax = max(1, max(ds.face_tokens[s].shape[0] for s in set_ids))
    B = len(set_ids)
    ft = np.zeros((B, fmax, DF), np.float32)
    fm = np.zeros((B, fmax), bool)
    ctx = np.zeros((B, DCTX), np.float32)
    for b, s in enumerate(set_ids):
        n = ds.face_tokens[s].shape[0]
        ft[b, :n] = ds.face_tokens[s]
        fm[b, :n] = ds.face_mask[s]
        ctx[b] = ds.ctx[s]
    return (torch.as_tensor(ft, dtype=dtype, device=device),
            torch.as_tensor(fm, device=device),
            torch.as_tensor(ctx, dtype=dtype, device=device))


@torch.no_grad()
def set_logits(net: UtilityNet, ds: TrialDataset, set_ids, *, device=None,
               chunk: int = 256, dtype=torch.float32) -> np.ndarray:
    device = device or next(net.parameters()).device
    set_ids = [int(s) for s in set_ids]
    out = np.zeros(len(set_ids), np.float64)
    order = sorted(range(len(set_ids)), key=lambda i: ds.face_tokens[set_ids[i]].shape[0])
    for start in range(0, len(order), chunk):
        sel = order[start:start + chunk]
        ft, fm, ctx = collate(ds, [set_ids[i] for i in sel], device, dtype)
        out[sel] = net.base_logit(ft, fm, ctx).double().cpu().numpy()
    return out


def trial_logits(net: UtilityNet, ds: TrialDataset, idx) -> np.ndarray:
    idx = np.asarray(idx, np.int64)
    uniq, inv = np.unique(ds.set_index[idx], return_inverse=True)
    base = set_logits(net, ds, uniq)
    kb = net.kind_bias.detach().double().cpu().numpy()
    return base[inv] + kb[ds.kind[idx]]


# ---------------------------------------------------------------------------- metrics

def sigmoid(x) -> np.ndarray:
    x = np.asarray(x, np.float64)
    return 0.5 * (1.0 + np.tanh(0.5 * x))


def logloss(y, p) -> float:
    """Mean binary cross-entropy; `y` may be soft (a probability target)."""
    y = np.asarray(y, np.float64)
    p = np.clip(np.asarray(p, np.float64), _EPS, 1 - _EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def entropy_floor(y) -> float:
    """The least log-loss any predictor can reach on soft targets `y`: mean H(y) (the
    cross-entropy of predicting each target exactly)."""
    y = np.clip(np.asarray(y, np.float64), _EPS, 1 - _EPS)
    return float(-np.mean(y * np.log(y) + (1 - y) * np.log(1 - y)))


def brier(y, p) -> float:
    return float(np.mean((np.asarray(p, np.float64) - np.asarray(y, np.float64)) ** 2))


def auc(y, s) -> float:
    """Rank AUC with average ranks for ties; nan when only one class is present."""
    y = np.asarray(y, bool)
    s = np.asarray(s, np.float64)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), np.float64)
    ss = s[order]
    i = 0
    while i < len(ss):
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        ranks[order[i:j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def reliability(y, p, bins: int = 10) -> list:
    """[{bin_lo, bin_hi, n, mean_p, mean_y}] over equal-width predicted-probability bins."""
    y = np.asarray(y, np.float64)
    p = np.asarray(p, np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        sel = which == b
        out.append({"bin_lo": float(edges[b]), "bin_hi": float(edges[b + 1]),
                    "n": int(sel.sum()),
                    "mean_p": float(p[sel].mean()) if sel.any() else None,
                    "mean_y": float(y[sel].mean()) if sel.any() else None})
    return out


def ece(y, p, bins: int = 10) -> float:
    n = max(len(np.asarray(y)), 1)
    return float(sum(r["n"] / n * abs(r["mean_p"] - r["mean_y"])
                     for r in reliability(y, p, bins) if r["n"]))


def binary_metrics(y, p, hard=None) -> dict:
    """Log-loss/Brier/ECE against the (soft) targets `y`; AUC against the observer's hard
    correctness `hard` (y >= 0.5 when not given)."""
    y = np.asarray(y, np.float64)
    hard = (y >= 0.5) if hard is None else np.asarray(hard, np.float64) > 0.5
    return {"n": int(len(y)), "logloss": logloss(y, p), "auc": auc(hard, p),
            "brier": brier(y, p), "ece": ece(y, p),
            "mean_target": float(np.mean(y)) if len(y) else float("nan"),
            "accuracy_rate": float(np.mean(hard)) if len(y) else float("nan")}


# ---------------------------------------------------------------------------- fitting

@dataclass
class TrainConfig:
    lr: float = 1e-3
    weight_decay: float = 1.0
    face_dropout: float = 0.2      # dropout on face-token embeddings (training only)
    batch_trials: int = 256
    max_epochs: int = 200
    patience: int = 12
    min_delta: float = 1e-4
    device: str = "auto"


# Regularisation candidates (AdamW weight_decay, face_dropout), chosen on val log-loss.
# Dropout sits on the face-token EMBEDDING, not the raw token: zeroing a raw column (or
# dropping a whole face from the pooling) can erase exactly what a label hinges on — the one
# target face no view shows — which turns labels unexplainable and trains memorisation.
REG_GRID = ((1e-1, 0.0), (1.0, 0.0), (3.0, 0.0), (1e-1, 0.2), (1.0, 0.2), (3.0, 0.2))


@dataclass
class FitResult:
    net: UtilityNet
    seed: int
    best_epoch: int
    val_logloss: float
    train_logloss: float
    epochs_run: int
    shuffled: bool
    history: list = field(default_factory=list)

    def summary(self) -> dict:
        return {"seed": self.seed, "best_epoch": self.best_epoch,
                "val_logloss": self.val_logloss, "train_logloss": self.train_logloss,
                "epochs_run": self.epochs_run, "shuffled": self.shuffled}


def _kind_rates(kind: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-kind success rate with a +1/+2 prior (kinds absent from train sit at 0.5)."""
    K = len(QUESTION_KINDS)
    pos = np.bincount(kind, weights=y, minlength=K)
    cnt = np.bincount(kind, minlength=K)
    return (pos + 1.0) / (cnt + 2.0)


def _logit(p):
    p = np.clip(np.asarray(p, np.float64), _EPS, 1 - _EPS)
    return np.log(p / (1 - p))


def fit_utility(ds: TrialDataset, train_idx, val_idx, *, seed: int, cfg: TrainConfig,
                shuffle_labels: bool = False, verbose: bool = False) -> FitResult:
    """AdamW + soft-target BCE with early stopping on val log-loss; best-epoch weights are
    returned. The shuffled control permutes the val targets it stops on too (its
    val_logloss is on those)."""
    train_idx = np.asarray(train_idx, np.int64)
    val_idx = np.asarray(val_idx, np.int64)
    if len(train_idx) == 0 or len(val_idx) == 0:
        raise ValueError("fit_utility needs non-empty train and val pairs")
    device = resolve_device(cfg.device)
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    y_all = ds.y.astype(np.float32).copy()
    if shuffle_labels:
        y_all[train_idx] = y_all[rng.permutation(train_idx)]
    net = UtilityNet()
    net.set_norm(*norm_stats(ds, ds.set_index[train_idx]))
    with torch.no_grad():
        net.kind_bias.copy_(torch.as_tensor(
            _logit(_kind_rates(ds.kind[train_idx], y_all[train_idx])), dtype=torch.float32))
    net.to(device)
    decay = [p for n, p in net.named_parameters() if n.endswith(("_w0", "_w1", "_wq"))]
    no_decay = [p for n, p in net.named_parameters() if not n.endswith(("_w0", "_w1", "_wq"))]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": cfg.weight_decay},
                             {"params": no_decay, "weight_decay": 0.0}], lr=cfg.lr)
    loss_fn = nn.BCEWithLogitsLoss()
    y_val = ds.y[val_idx].astype(np.float64)
    if shuffle_labels:
        # Early stopping must be as blind as the gradients: picking the epoch on TRUE val
        # labels keeps whichever random function happens to line up with the truth, so the
        # control would beat the prior by selection alone. Callers judge the net on truth.
        y_val = y_val[rng.permutation(len(y_val))]
    best = (float("inf"), -1, None, float("nan"))
    history: list = []
    bad = 0
    epoch = 0
    for epoch in range(1, cfg.max_epochs + 1):
        net.train()
        perm = train_idx[rng.permutation(len(train_idx))]
        total, count = 0.0, 0
        for start in range(0, len(perm), cfg.batch_trials):
            b = perm[start:start + cfg.batch_trials]
            uniq, inv = np.unique(ds.set_index[b], return_inverse=True)
            ft, fm, ctx = collate(ds, uniq, device)
            base = net.base_logit(ft, fm, ctx, face_dropout=cfg.face_dropout)
            logits = base[torch.as_tensor(inv, device=device)] + \
                net.kind_bias[torch.as_tensor(ds.kind[b], device=device)]
            target = torch.as_tensor(y_all[b], device=device)
            loss = loss_fn(logits, target)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            total += float(loss.detach()) * len(b)
            count += len(b)
        net.eval()
        val_ll = logloss(y_val, sigmoid(trial_logits(net, ds, val_idx)))
        history.append({"epoch": epoch, "train_logloss": total / max(count, 1),
                        "val_logloss": val_ll})
        if verbose:
            print(f"  seed {seed} epoch {epoch}: train {total / max(count, 1):.4f} "
                  f"val {val_ll:.4f}")
        if val_ll < best[0] - cfg.min_delta:
            best = (val_ll, epoch, copy.deepcopy(net.state_dict()), total / max(count, 1))
            bad = 0
        else:
            bad += 1
            if bad >= cfg.patience:
                break
    net.load_state_dict(best[2])
    net.eval()
    return FitResult(net=net.cpu(), seed=seed, best_epoch=best[1], val_logloss=best[0],
                     train_logloss=best[3], epochs_run=epoch, shuffled=shuffle_labels,
                     history=history)


# ---------------------------------------------------------------------------- baselines

def pooled_features(ds: TrialDataset, idx) -> np.ndarray:
    """[mean over visible faces, max over faces, set_global, context] per trial."""
    idx = np.asarray(idx, np.int64)
    uniq, inv = np.unique(ds.set_index[idx], return_inverse=True)
    rows = np.zeros((len(uniq), 2 * DF + DCTX), np.float64)
    for r, s in enumerate(uniq):
        f = ds.face_tokens[s][ds.face_mask[s]].astype(np.float64)
        if f.shape[0]:
            rows[r, :DF] = f.mean(0)
            rows[r, DF:2 * DF] = f.max(0)
        rows[r, 2 * DF:] = ds.ctx[s]
    return rows[inv]


class LogisticBaseline:
    """logit = standardised(X) @ w + b[kind]; L2 on w, fitted full-batch with L-BFGS."""

    def __init__(self, l2: float):
        self.l2 = float(l2)
        self.mean = self.std = self.w = self.b = None

    def fit(self, X, kind, y) -> "LogisticBaseline":
        X = np.asarray(X, np.float64)
        self.mean = X.mean(0)
        sd = X.std(0)
        self.std = np.where(sd > 1e-9, sd, 1.0)
        Xt = torch.as_tensor((X - self.mean) / self.std, dtype=torch.float64)
        kt = torch.as_tensor(np.asarray(kind, np.int64))
        yt = torch.as_tensor(np.asarray(y, np.float64))
        w = torch.zeros(X.shape[1], dtype=torch.float64, requires_grad=True)
        b = torch.as_tensor(_logit(_kind_rates(np.asarray(kind, np.int64),
                                               np.asarray(y, np.float64))),
                            dtype=torch.float64).clone().requires_grad_(True)
        opt = torch.optim.LBFGS([w, b], lr=1.0, max_iter=500, tolerance_grad=1e-9,
                                tolerance_change=1e-12, line_search_fn="strong_wolfe")
        bce = nn.BCEWithLogitsLoss()

        def closure():
            opt.zero_grad()
            loss = bce(Xt @ w + b[kt], yt) + 0.5 * self.l2 * (w * w).sum()
            loss.backward()
            return loss

        opt.step(closure)
        self.w = w.detach().numpy()
        self.b = b.detach().numpy()
        return self

    def logits(self, X, kind) -> np.ndarray:
        X = (np.asarray(X, np.float64) - self.mean) / self.std
        return X @ self.w + self.b[np.asarray(kind, np.int64)]


def fit_logistic(Xtr, ktr, ytr, Xva, kva, yva,
                 l2_grid: Sequence[float] = (1e-4, 1e-3, 1e-2, 1e-1, 1.0)) -> tuple:
    """(best model, chosen l2, val log-loss) with L2 picked on the val split."""
    best = None
    for l2 in l2_grid:
        m = LogisticBaseline(l2).fit(Xtr, ktr, ytr)
        ll = logloss(yva, sigmoid(m.logits(Xva, kva)))
        if best is None or ll < best[2]:
            best = (m, l2, ll)
    return best


def fit_hand_calibration(ds: TrialDataset, train_idx) -> LogisticBaseline:
    """1-D logistic on hand_utility (+ per-kind intercepts): the baseline to beat."""
    train_idx = np.asarray(train_idx, np.int64)
    return LogisticBaseline(1e-6).fit(ds.hand[train_idx, None], ds.kind[train_idx],
                                      ds.y[train_idx])


def prior_probs(ds: TrialDataset, train_idx, idx) -> np.ndarray:
    """Per-kind train success rate: what a model that ignores the pictures predicts."""
    rates = _kind_rates(ds.kind[np.asarray(train_idx, np.int64)],
                        ds.y[np.asarray(train_idx, np.int64)].astype(np.float64))
    return rates[ds.kind[np.asarray(idx, np.int64)]]


# ---------------------------------------------------------------------------- final model

def data_digest(ds: TrialDataset, idx) -> str:
    ids = sorted(ds.rows[i]["pair_id"] for i in np.asarray(idx, np.int64))
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def split_indices(ds: TrialDataset) -> tuple:
    return (np.flatnonzero(ds.splits == "train"), np.flatnonzero(ds.splits == "val"),
            np.flatnonzero(ds.splits == "test"))


def observer_digests(ds: TrialDataset, idx) -> list:
    return sorted({str(ds.rows[i].get("observer_digest")) for i in np.asarray(idx, np.int64)
                   if ds.rows[i].get("observer_digest")})


def select_regularisation(ds: TrialDataset, train_idx, val_idx, cfg: TrainConfig, *,
                          grid: Sequence[tuple] = REG_GRID, seed: int = 0,
                          verbose: bool = False) -> tuple:
    """(cfg with the (weight_decay, face_dropout) of lowest val log-loss, sweep table)."""
    table = []
    for wd, drop in grid:
        trial = replace(cfg, weight_decay=float(wd), face_dropout=float(drop))
        fit = fit_utility(ds, train_idx, val_idx, seed=seed, cfg=trial)
        table.append({"weight_decay": float(wd), "face_dropout": float(drop),
                      "val_logloss": fit.val_logloss, "train_logloss": fit.train_logloss,
                      "best_epoch": fit.best_epoch})
        if verbose:
            print(f"[train] wd {wd:g} face_dropout {drop:g}: val {fit.val_logloss:.4f} "
                  f"train {fit.train_logloss:.4f} (epoch {fit.best_epoch})")
    best = min(table, key=lambda r: r["val_logloss"])
    return replace(cfg, weight_decay=best["weight_decay"],
                   face_dropout=best["face_dropout"]), table


def train_final(store: TrialStore, out_dir, *, seeds: int = 5, cfg: Optional[TrainConfig] = None,
                sweep: bool = True, verbose: bool = False) -> dict:
    """Choose regularisation on val (seed 0), fit `seeds` models on all train pairs with it,
    keep the lowest val log-loss, export + verify.

    Writes ``train_report.json`` (whose sha is stamped into the NPZ until evaluate replaces
    it with the convergence report's; its ``config`` is what evaluate re-fits with),
    ``utility_v1.npz`` and ``export.json`` (parity)."""
    from .export import export_npz, verify_parity  # noqa: PLC0415

    cfg = cfg or TrainConfig()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = load_dataset(store, judge="crossfit", splits=("train", "val", "test"))
    tr, va, _te = split_indices(ds)
    if len(tr) == 0 or len(va) == 0:
        raise ValueError(f"store {store.root} has {len(tr)} train / {len(va)} val labelled "
                         "pairs; run `trials` and `observe` on both splits first")
    t0 = time.time()
    sweep_table = None
    if sweep:
        cfg, sweep_table = select_regularisation(ds, tr, va, cfg, verbose=verbose)
    fits = [fit_utility(ds, tr, va, seed=s, cfg=cfg, verbose=verbose) for s in range(seeds)]
    best = min(fits, key=lambda f: f.val_logloss)
    observers = observer_digests(ds, tr)
    trained_on = (f"viewnet store {store.root}: {len(set(ds.part_ids[tr]))} train parts / "
                  f"{len(tr)} pairs, {len(set(ds.part_ids[va]))} val parts / {len(va)} pairs; "
                  f"labels=crossfit observer {','.join(o[:8] for o in observers) or '?'}; "
                  f"data_sha={data_digest(ds, tr)[:16]}")
    report = {
        "schema": "viewnet.train_report/2", "feature_schema": FEATURE_SCHEMA,
        "config": asdict(cfg), "regularisation_sweep": sweep_table,
        "seeds": [f.summary() for f in fits], "best_seed": best.seed,
        "train": {"parts": len(set(ds.part_ids[tr])), "pairs": int(len(tr)),
                  "data_sha": data_digest(ds, tr)},
        "val": {"parts": len(set(ds.part_ids[va])), "pairs": int(len(va)),
                "data_sha": data_digest(ds, va)},
        "observer_digests": observers, "trained_on": trained_on,
        "device": str(resolve_device(cfg.device)), "seconds": time.time() - t0,
    }
    report_path = out_dir / "train_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    npz = out_dir / "utility_v1.npz"
    export_npz(best.net, npz, trained_on=trained_on, report_sha=report_sha)
    parity = verify_parity(npz, best.net, ds, np.unique(ds.set_index[va]))
    (out_dir / "export.json").write_text(json.dumps(parity, indent=2, sort_keys=True))
    return {"report": str(report_path), "npz": str(npz), "best_seed": best.seed,
            "config": asdict(cfg), "val_logloss": best.val_logloss, "parity": parity,
            "seeds": [f.summary() for f in fits]}
