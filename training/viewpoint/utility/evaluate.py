"""Convergence report and held-out A/B for the ViewNet utility model.

Labels are the cross-fitted observer's P(correct) (``training.viewpoint.observer``); no
language model is involved anywhere. Convergence is measured, not assumed:

* learning curve over train-PART fractions (1/8, 1/4, 1/2, 1) x seeds, val log-loss/AUC/Brier;
* seed spread at full data;
* shuffled-label control — trained on permuted train targets, judged on true val targets; it
  must sit at the per-kind prior, anything clearly better means information leaks through
  something other than the pictures;
* label-noise floor from observer disagreement: the same val pairs labelled by the
  cross-fitted observer and by the independent cross-fit replicate (both out-of-sample on
  every part). With independent errors of equal variance, a predictor of the underlying
  usefulness scores Brier = E[(p_a - p_b)^2] / 2 against either observer — the floor;
  the soft log-loss can never go below the targets' mean entropy;
* calibration (ECE + reliability table) and whole-part bootstrap CIs against the baselines.

Verdict rule (stated in every report): CONVERGED iff the last doubling of train parts
improves mean val log-loss by < 1 % relative, the seed std at full data is smaller than the
total improvement over the curve, and the shuffled control shows no leakage.

Ship rule: the exported model must beat calibrated hand_utility on held-out log-loss (test
split; val when no test pairs exist) AND on held-out TEST parts the INDEPENDENT observer's
expected accuracy on the model-greedy@4 grid minus the hand-greedy@4 grid must have a 95 %
whole-part bootstrap CI excluding 0.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from services.viewpoint.utility.features import DG, QUESTION_KINDS

from .store import TrialDataset, TrialStore, load_dataset
from .train import (TrainConfig, binary_metrics, brier, entropy_floor, fit_hand_calibration,
                    fit_logistic, fit_utility, logloss, pooled_features, prior_probs,
                    reliability, sigmoid, split_indices, trial_logits)

FRACTIONS = (0.125, 0.25, 0.5, 1.0)
PLATEAU_REL = 0.01
LEAK_REL = 0.01
REPORT_SCHEMA = "viewnet.utility_report/2"
AB_SCHEMA = "viewnet.utility_ab/2"
AB_REF = "model_greedy@4"


# ---------------------------------------------------------------------------- statistics

def per_trial_logloss(y, p) -> np.ndarray:
    y = np.asarray(y, np.float64)
    p = np.clip(np.asarray(p, np.float64), 1e-7, 1 - 1e-7)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def part_bootstrap(part_ids, values, *, n_boot: int = 2000, seed: int = 0) -> dict:
    """Mean of per-pair `values` with a 95 % CI from resampling whole PARTS (pairs of one
    part are correlated — resampling pairs would overstate certainty)."""
    values = np.asarray(values, np.float64)
    if values.size == 0:
        return {"estimate": float("nan"), "ci95": [float("nan"), float("nan")],
                "n": 0, "n_parts": 0}
    uniq, inv = np.unique(np.asarray(part_ids, object).astype(str), return_inverse=True)
    P = len(uniq)
    sums = np.bincount(inv, weights=values, minlength=P)
    cnt = np.bincount(inv, minlength=P).astype(np.float64)
    rng = np.random.default_rng(seed)
    draws = rng.multinomial(P, np.full(P, 1.0 / P), size=int(n_boot)).astype(np.float64)
    stats = (draws @ sums) / np.maximum(draws @ cnt, 1e-12)
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return {"estimate": float(values.mean()), "ci95": [float(lo), float(hi)],
            "n": int(values.size), "n_parts": int(P)}


def _excludes_zero_positive(b: dict) -> bool:
    return bool(np.isfinite(b["ci95"][0]) and b["ci95"][0] > 0.0)


# ---------------------------------------------------------------------------- predictions

def artifact_probs(npz_path, ds: TrialDataset, idx) -> np.ndarray:
    """P(correct) from the SHIPPED artifact: numpy UtilityModel set logit + NPZ kind_bias."""
    from services.viewpoint.utility.model import UtilityModel  # noqa: PLC0415

    idx = np.asarray(idx, np.int64)
    if idx.size == 0:
        return np.zeros(0)
    model = UtilityModel.load(str(npz_path))
    with np.load(Path(npz_path), allow_pickle=False) as z:
        kind_bias = np.asarray(z["kind_bias"], np.float64)
    uniq, inv = np.unique(ds.set_index[idx], return_inverse=True)
    base = model.score_batch([ds.token_dict(int(s), DG) for s in uniq])
    return sigmoid(base[inv] + kind_bias[ds.kind[idx]])


def _subset_parts(ds: TrialDataset, tr: np.ndarray, frac: float, seed: int = 0) -> np.ndarray:
    """Nested whole-part subsets: the 1/8 parts are inside the 1/4 parts, etc."""
    parts = np.array(sorted(set(ds.part_ids[tr].astype(str))))
    order = parts[np.random.default_rng(seed).permutation(len(parts))]
    keep = set(order[:max(1, int(math.ceil(frac * len(parts))))])
    return tr[np.isin(ds.part_ids[tr].astype(str), list(keep))]


def _strategy_table(ds: TrialDataset, idx, model_p, *, n_boot: int) -> list:
    """Observer expected accuracy per (strategy, k) — the view-set baselines as measured —
    next to what the model predicts for the same pairs."""
    idx = np.asarray(idx, np.int64)
    pos = {int(i): j for j, i in enumerate(idx)}
    groups: dict = {}
    for i in idx:
        row = ds.rows[i]
        for s in row["strategies"]:
            groups.setdefault((s, int(row["k"])), []).append(int(i))
    out = []
    for (s, k), members in sorted(groups.items()):
        m = np.asarray(members, np.int64)
        out.append({"strategy": s, "k": k,
                    "observer_p_correct": part_bootstrap(ds.part_ids[m], ds.y[m], n_boot=n_boot),
                    "observer_accuracy": float(ds.correct[m].mean()),
                    "model_mean_p": float(np.mean([model_p[pos[int(i)]] for i in m])),
                    "mean_chance": float(ds.chance[m].mean())})
    return out


def observer_noise(store: TrialStore, ds: TrialDataset, idx, *, n_boot: int) -> dict:
    """Disagreement between the cross-fitted observer (ds labels) and the independent
    cross-fit replicate on the same pairs — the label-noise floor."""
    idx = np.asarray(idx, np.int64)
    other = store.load_labels("independent")
    hit = [(int(i), other[(ds.rows[i]["set_key"], ds.rows[i]["qid"])]) for i in idx
           if (ds.rows[i]["set_key"], ds.rows[i]["qid"]) in other]
    if not hit:
        return {"pairs": 0, "brier_floor": None,
                "note": "no val pairs labelled by the independent observer — run "
                        "`observe --judge independent --splits val`"}
    ii = np.asarray([i for i, _r in hit], np.int64)
    pa = ds.y[ii].astype(np.float64)
    pb = np.asarray([float(r["p_correct"]) for _i, r in hit], np.float64)
    ca = ds.correct[ii] > 0.5
    cb = np.asarray([bool(r["correct"]) for _i, r in hit])
    sq = (pa - pb) ** 2
    ci = part_bootstrap(ds.part_ids[ii], sq, n_boot=n_boot)["ci95"]
    return {"pairs": int(len(ii)), "parts": int(len(set(ds.part_ids[ii].astype(str)))),
            "brier_floor": float(sq.mean() / 2.0), "brier_floor_ci95": [x / 2.0 for x in ci],
            "mean_abs_diff": float(np.abs(pa - pb).mean()),
            "p_correlation": float(np.corrcoef(pa, pb)[0, 1]) if len(ii) > 1 else None,
            "hard_agreement": float((ca == cb).mean()),
            "entropy_floor": entropy_floor(pa)}


# ---------------------------------------------------------------------------- report

def convergence_report(store: TrialStore, run_dir, *, seeds: int = 5,
                       fractions: Sequence[float] = FRACTIONS, cfg: Optional[TrainConfig] = None,
                       n_boot: int = 2000, control_seeds: int = 3,
                       verbose: bool = False) -> dict:
    from .export import stamp_report, weights_digest  # noqa: PLC0415

    cfg = cfg or TrainConfig()
    run_dir = Path(run_dir)
    npz = run_dir / "utility_v1.npz"
    if not npz.exists():
        raise FileNotFoundError(f"{npz} not found — run `train` first")
    ds = load_dataset(store, judge="crossfit", splits=("train", "val", "test"))
    tr, va, te = split_indices(ds)
    if len(tr) == 0 or len(va) == 0:
        raise ValueError(f"need train and val labelled pairs; have {len(tr)} / {len(va)}")
    y_va = ds.y[va].astype(np.float64)
    prior_va = prior_probs(ds, tr, va)
    prior_ll = logloss(y_va, prior_va)

    # ---- learning curve ----
    curve = []
    for frac in sorted(fractions):
        sub = _subset_parts(ds, tr, frac)
        runs = []
        for s in range(seeds):
            fit = fit_utility(ds, sub, va, seed=s, cfg=cfg)
            m = binary_metrics(y_va, sigmoid(trial_logits(fit.net, ds, va)), ds.correct[va])
            runs.append({"seed": s, "best_epoch": fit.best_epoch,
                         "train_logloss": fit.train_logloss, **m})
            if verbose:
                print(f"[evaluate] frac {frac:.3f} seed {s}: val logloss {m['logloss']:.4f} "
                      f"auc {m['auc']:.3f}")
        lls = np.array([r["logloss"] for r in runs])
        curve.append({
            "fraction": float(frac), "train_parts": int(len(set(ds.part_ids[sub].astype(str)))),
            "train_pairs": int(len(sub)),
            "val_logloss_mean": float(lls.mean()), "val_logloss_std": float(lls.std(ddof=0)),
            "train_logloss_mean": float(np.mean([r["train_logloss"] for r in runs])),
            "val_auc_mean": float(np.nanmean([r["auc"] for r in runs])),
            "val_brier_mean": float(np.mean([r["brier"] for r in runs])),
            "runs": runs})

    # ---- shuffled-label control ----
    control = []
    for s in range(control_seeds):
        fit = fit_utility(ds, tr, va, seed=1000 + s, cfg=cfg, shuffle_labels=True)
        control.append(logloss(y_va, sigmoid(trial_logits(fit.net, ds, va))))
    control_ll = float(np.mean(control))
    leakage = control_ll < prior_ll * (1.0 - LEAK_REL)

    # ---- label-noise floor ----
    noise = observer_noise(store, ds, va, n_boot=n_boot)

    # ---- shipped artifact vs baselines ----
    hand = fit_hand_calibration(ds, tr)
    Xtr, Xva = pooled_features(ds, tr), pooled_features(ds, va)
    logreg, l2, _ = fit_logistic(Xtr, ds.kind[tr], ds.y[tr], Xva, ds.kind[va], ds.y[va])
    splits = {"val": va} | ({"test": te} if len(te) else {})
    evals: dict = {}
    for name, idx in splits.items():
        y = ds.y[idx].astype(np.float64)
        hard = ds.correct[idx]
        preds = {
            "model": artifact_probs(npz, ds, idx),
            "hand_utility_calibrated": sigmoid(hand.logits(ds.hand[idx, None], ds.kind[idx])),
            "logreg_pooled": sigmoid(logreg.logits(pooled_features(ds, idx), ds.kind[idx])),
            "prior": prior_probs(ds, tr, idx),
        }
        model_ll = per_trial_logloss(y, preds["model"])
        evals[name] = {
            "entropy_floor": entropy_floor(y),
            "metrics": {k: binary_metrics(y, p, hard) for k, p in preds.items()},
            "delta_logloss_vs_model": {
                k: part_bootstrap(ds.part_ids[idx], per_trial_logloss(y, p) - model_ll,
                                  n_boot=n_boot)
                for k, p in preds.items() if k != "model"},
            "calibration": reliability(y, preds["model"]),
            "strategies": _strategy_table(ds, idx, preds["model"], n_boot=n_boot),
            "by_fold": {int(f): binary_metrics(y[ds.folds[idx] == f], preds["model"][ds.folds[idx] == f],
                                               hard[ds.folds[idx] == f])
                        for f in sorted(set(ds.folds[idx].tolist()))},
            "by_kind": {QUESTION_KINDS[k]: binary_metrics(y[ds.kind[idx] == k],
                                                          preds["model"][ds.kind[idx] == k],
                                                          hard[ds.kind[idx] == k])
                        for k in sorted(set(ds.kind[idx].tolist()))},
        }
    if noise.get("brier_floor") is not None:
        noise["model_val_brier"] = brier(y_va, artifact_probs(npz, ds, va))

    # ---- verdict ----
    ll = {c["fraction"]: c["val_logloss_mean"] for c in curve}
    fr = sorted(ll)
    last_gain = ((ll[fr[-2]] - ll[fr[-1]]) / ll[fr[-2]]) if len(fr) >= 2 else float("nan")
    total_gain = ll[fr[0]] - ll[fr[-1]]
    seed_std = curve[-1]["val_logloss_std"]
    plateaued = bool(np.isfinite(last_gain) and last_gain < PLATEAU_REL)
    resolved = bool(seed_std < total_gain)
    converged = plateaued and resolved and not leakage
    held_name = "test" if "test" in evals else "val"
    held = evals[held_name]["delta_logloss_vs_model"]["hand_utility_calibrated"]
    offline_pass = _excludes_zero_positive(held)
    report = {
        "schema": REPORT_SCHEMA,
        "weights_digest": weights_digest(npz),
        "train_report_sha": hashlib.sha256((run_dir / "train_report.json").read_bytes()).hexdigest()
        if (run_dir / "train_report.json").exists() else None,
        "config": asdict(cfg), "seeds": seeds, "n_boot": n_boot,
        "labels": {"judge": "crossfit",
                   "observer_digests": sorted({str(r.get("observer_digest")) for r in ds.rows
                                               if r.get("observer_digest")})},
        "data": {s: {"pairs": int(len(i)), "parts": int(len(set(ds.part_ids[i].astype(str))))}
                 for s, i in (("train", tr), ("val", va), ("test", te))},
        "prior_val_logloss": prior_ll,
        "learning_curve": curve,
        "seed_spread": {"val_logloss_std_full": seed_std,
                        "val_logloss_full": [r["logloss"] for r in curve[-1]["runs"]]},
        "shuffled_control": {"val_logloss_runs": control, "val_logloss_mean": control_ll,
                             "prior_val_logloss": prior_ll, "leakage": bool(leakage),
                             "rule": f"leakage iff control < prior * (1 - {LEAK_REL})"},
        "noise_floor": noise,
        "baselines": {"logreg_l2": l2},
        "evaluation": evals,
        "verdict": {
            "converged": bool(converged),
            "rule": (f"converged iff last doubling of train parts improves mean val log-loss by "
                     f"< {PLATEAU_REL:.0%} relative AND seed std at full data < total "
                     f"improvement over the curve AND the shuffled control shows no leakage"),
            "last_doubling_rel_gain": float(last_gain), "total_gain": float(total_gain),
            "seed_std_full": float(seed_std), "plateaued": plateaued, "resolved": resolved,
            "leakage": bool(leakage),
            "heldout_logloss": {"split": held_name, "delta_hand_minus_model": held,
                                "pass": bool(offline_pass),
                                "rule": "95% part-bootstrap CI of (hand - model) log-loss > 0"},
        },
    }
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=_json_default))
    (run_dir / "report.md").write_text(render_markdown(report))
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    stamp_report(npz, report_sha)
    report["report_sha"] = report_sha
    return report


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def _f(x, nd: int = 4) -> str:
    return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{nd}f}"


def _ci(b: dict, nd: int = 4) -> str:
    return f"{_f(b['estimate'], nd)} [{_f(b['ci95'][0], nd)}, {_f(b['ci95'][1], nd)}]"


def render_markdown(r: dict) -> str:
    v = r["verdict"]
    L = ["# ViewNet utility model — convergence report", "",
         f"**Verdict: {'CONVERGED' if v['converged'] else 'NOT CONVERGED'}** — {v['rule']}.",
         f"Last doubling gain {_f(v['last_doubling_rel_gain'] * 100, 2)} %, total gain "
         f"{_f(v['total_gain'])}, seed std {_f(v['seed_std_full'])}, leakage {v['leakage']}.", "",
         f"**Held-out log-loss vs calibrated hand_utility ({v['heldout_logloss']['split']}):** "
         f"hand − model = {_ci(v['heldout_logloss']['delta_hand_minus_model'])} → "
         f"{'PASS' if v['heldout_logloss']['pass'] else 'FAIL'}.", "",
         f"Labels: cross-fitted observer "
         f"{', '.join(o[:12] for o in r['labels']['observer_digests']) or '(no digest)'}. Data: "
         + ", ".join(f"{s} {d['parts']} parts / {d['pairs']} pairs" for s, d in r["data"].items()),
         "",
         f"Config: weight_decay {r['config']['weight_decay']:g}, face_dropout "
         f"{r['config']['face_dropout']:g}, lr {r['config']['lr']:g}.", "",
         "## Learning curve (val)", "",
         "| train fraction | parts | pairs | val log-loss mean ± std | train log-loss | AUC | Brier |",
         "|---|---|---|---|---|---|---|"]
    for c in r["learning_curve"]:
        L.append(f"| {c['fraction']:.3f} | {c['train_parts']} | {c['train_pairs']} | "
                 f"{_f(c['val_logloss_mean'])} ± {_f(c['val_logloss_std'])} | "
                 f"{_f(c['train_logloss_mean'])} | {_f(c['val_auc_mean'], 3)} | "
                 f"{_f(c['val_brier_mean'])} |")
    sc = r["shuffled_control"]
    nf = r["noise_floor"]
    L += ["", "## Controls", "",
          f"- Prior (per-kind rate) val log-loss: {_f(r['prior_val_logloss'])}",
          f"- Shuffled-label control val log-loss: {_f(sc['val_logloss_mean'])} "
          f"({'LEAKAGE' if sc['leakage'] else 'at prior — ok'}; {sc['rule']})",
          (f"- Label-noise floor (cross-fit vs independent observer, {nf['pairs']} val pairs): "
           f"Brier floor {_f(nf['brier_floor'])} (model {_f(nf.get('model_val_brier'))}), "
           f"hard agreement {_f(nf['hard_agreement'], 3)}, p correlation "
           f"{_f(nf['p_correlation'], 3)}, entropy floor {_f(nf['entropy_floor'])}"
           if nf.get("brier_floor") is not None else f"- Label-noise floor: {nf['note']}"), ""]
    for split, ev in r["evaluation"].items():
        L += [f"## {split}", "", f"Soft log-loss floor (mean target entropy): "
              f"{_f(ev['entropy_floor'])}", "",
              "| predictor | log-loss | AUC | Brier | ECE |", "|---|---|---|---|---|"]
        for name, m in ev["metrics"].items():
            L.append(f"| {name} | {_f(m['logloss'])} | {_f(m['auc'], 3)} | {_f(m['brier'])} | "
                     f"{_f(m['ece'])} |")
        L += ["", "Δ log-loss (predictor − model), part-bootstrap 95 % CI:", ""]
        for name, b in ev["delta_logloss_vs_model"].items():
            L.append(f"- {name}: {_ci(b)}")
        L += ["", "Calibration (model):", "", "| predicted | n | mean predicted | mean target |",
              "|---|---|---|---|"]
        for b in ev["calibration"]:
            if b["n"]:
                L.append(f"| {b['bin_lo']:.1f}-{b['bin_hi']:.1f} | {b['n']} | "
                         f"{_f(b['mean_p'], 3)} | {_f(b['mean_y'], 3)} |")
        L += ["", "Observer expected accuracy by view-set strategy:", "",
              "| strategy | k | observer P(correct) [95 % CI] | observer acc | model mean p | pairs | parts |",
              "|---|---|---|---|---|---|---|"]
        for s in ev["strategies"]:
            a = s["observer_p_correct"]
            L.append(f"| {s['strategy']} | {s['k']} | {_ci(a, 3)} | "
                     f"{_f(s['observer_accuracy'], 3)} | {_f(s['model_mean_p'], 3)} | "
                     f"{a['n']} | {a['n_parts']} |")
        L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------- A/B

def ab_arm_table(store: TrialStore, *, judge: str = "independent", n_boot: int = 2000,
                 arms: Sequence[tuple] = None) -> dict:
    """Per-arm expected accuracy of `judge` on the store's sets, and paired deltas of the
    reference arm against every other arm over the queries both answered."""
    from .trials import AB_ARMS  # noqa: PLC0415

    arms = {f"{s}@{k}": (s, int(k)) for s, k in (arms or AB_ARMS)}
    sets = store.load_sets()
    labels = store.load_labels(judge)
    by_arm: dict = {a: {} for a in arms}
    for (sk, qid), row in labels.items():
        rec = sets.get(sk)
        if rec is None or rec["split"] != "test":
            continue
        for a, (s, k) in arms.items():
            if s in rec["strategies"] and int(rec["k"]) == k:
                by_arm[a][qid] = {"part_id": str(rec["part_id"]),
                                  "p": float(row["p_correct"]),
                                  "correct": 1.0 if row["correct"] else 0.0}
    accuracy = {a: {"p_correct": part_bootstrap([r["part_id"] for r in d.values()],
                                                [r["p"] for r in d.values()], n_boot=n_boot),
                    "hard_accuracy": part_bootstrap([r["part_id"] for r in d.values()],
                                                    [r["correct"] for r in d.values()],
                                                    n_boot=n_boot)}
                for a, d in by_arm.items()}
    deltas = {}
    for a in arms:
        if a == AB_REF:
            continue
        common = sorted(set(by_arm[AB_REF]) & set(by_arm[a]))
        deltas[f"{AB_REF} - {a}"] = part_bootstrap(
            [by_arm[AB_REF][q]["part_id"] for q in common],
            [by_arm[AB_REF][q]["p"] - by_arm[a][q]["p"] for q in common], n_boot=n_boot)
    return {"arms": {a: {"strategy": s, "k": k} for a, (s, k) in arms.items()},
            "accuracy": accuracy, "paired_deltas": deltas}


def ab_report(labels_path, store_root, run_dir, trial_cfg, *, n_boot: int = 2000,
              ship: bool = False, weights_dest: Optional[Path] = None) -> dict:
    """View sets for every arm on held-out TEST parts, scored by the INDEPENDENT observer,
    then the ship verdict. `ship` copies the NPZ only when the verdict passes."""
    from services.viewpoint.utility.model import DEFAULT_WEIGHTS  # noqa: PLC0415

    from .trials import AB_ARMS, observe, run_trials  # noqa: PLC0415

    run_dir = Path(run_dir)
    npz = run_dir / "utility_v1.npz"
    rep_path = run_dir / "report.json"
    if not npz.exists() or not rep_path.exists():
        raise FileNotFoundError(f"need {npz} and {rep_path} — run `train` and `evaluate` first")
    offline = json.loads(rep_path.read_text())
    cfg = replace(trial_cfg, namespace="ab", ab_arms=AB_ARMS, splits=("test",),
                  weights=str(npz))
    store = TrialStore(store_root, name="ab")
    run = run_trials(labels_path, store, cfg)
    labelled = observe(store, judge="independent", splits=("test",))
    table = ab_arm_table(store, judge="independent", n_boot=n_boot)
    online = table["paired_deltas"].get(f"{AB_REF} - hand_greedy@4")
    online_pass = bool(online is not None and _excludes_zero_positive(online))
    offline_pass = bool(offline["verdict"]["heldout_logloss"]["pass"])
    ship_ok = online_pass and offline_pass
    reasons = []
    if not offline_pass:
        reasons.append("held-out log-loss does not beat calibrated hand_utility with CI > 0")
    if not online_pass:
        reasons.append("independent-observer P(correct) delta model_greedy@4 − hand_greedy@4: "
                       "CI does not exclude 0")
    result = {
        "schema": AB_SCHEMA, "judge": "independent", "weights_digest": offline["weights_digest"],
        "run": run, "observe": labelled, **table,
        "verdict": {"ship": ship_ok, "offline_pass": offline_pass, "online_pass": online_pass,
                    "reasons": reasons,
                    "rule": ("ship iff held-out log-loss beats hand_utility (CI > 0) AND the "
                             "independent observer's P(correct) on model_greedy@4 − "
                             "hand_greedy@4 has a 95 % part-bootstrap CI excluding 0")},
    }
    dest = Path(weights_dest) if weights_dest else DEFAULT_WEIGHTS
    if ship:
        if ship_ok:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(npz, dest)
            result["shipped_to"] = str(dest)
        else:
            result["shipped_to"] = None
    (run_dir / "ab_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=_json_default))
    (run_dir / "ab_report.md").write_text(render_ab_markdown(result))
    return result


def render_ab_markdown(r: dict) -> str:
    v = r["verdict"]
    L = ["# ViewNet utility model — A/B on held-out test parts (independent observer)", "",
         f"**Ship verdict: {'SHIP' if v['ship'] else 'DO NOT SHIP — planner stays on hand_utility'}**"
         f" — {v['rule']}.", ""]
    L += [f"- {x}" for x in v["reasons"]]
    L += ["", "| arm | P(correct) [95 % CI] | hard accuracy [95 % CI] | queries | parts |",
          "|---|---|---|---|---|"]
    for a, acc in r["accuracy"].items():
        b = acc["p_correct"]
        L.append(f"| {a} | {_ci(b, 3)} | {_ci(acc['hard_accuracy'], 3)} | {b['n']} | "
                 f"{b['n_parts']} |")
    L += ["", "Paired P(correct) deltas (same queries, part-bootstrap 95 % CI):", ""]
    for name, b in r["paired_deltas"].items():
        L.append(f"- {name}: {_ci(b, 3)} over {b['n']} queries")
    if "shipped_to" in r:
        L += ["", f"Shipped to: {r['shipped_to'] or 'not shipped (verdict failed)'}"]
    return "\n".join(L) + "\n"
