"""CLI: python3 -m training.viewpoint.utility {trials,observe,train,evaluate,ab}

trials/ab need the engine (libchudware, in-process) and the observer's render workers;
observe/train/evaluate/ab need torch (system python3). See README.md in this directory for
the full recipe and wall-clock estimates.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path


def _labels_default() -> Path:
    from training.viewpoint.datasets.build_manifest import default_manifest_path  # noqa: PLC0415
    return Path(default_manifest_path())


def _tile(text: str) -> tuple:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def _csv(text: str) -> tuple:
    return tuple(s.strip() for s in text.split(",") if s.strip())


def _add_trial_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--labels", type=Path, default=None,
                   help="chudware.geomlabel/1 JSONL (default: the DatasetsLabels manifest under "
                        "$CHUDWARE_VIEWNET_DATA/manifests/geomlabel.jsonl)")
    p.add_argument("--budget-minutes", type=float, default=None,
                   help="wall-clock cap; a stopped run resumes where it left off")
    p.add_argument("--limit-parts", type=int, default=None)
    p.add_argument("--targets-per-part", type=int, default=1,
                   help="target features per part (each gets its own universe of view sets)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tile", type=_tile, default=(512, 384), help="grid tile WxH")
    p.add_argument("--render-workers", type=int, default=8,
                   help="observer render worker processes (each loads its own engine)")


def _add_store_args(p: argparse.ArgumentParser) -> None:
    from .store import default_run_dir, default_store_root  # noqa: PLC0415
    p.add_argument("--store", type=Path, default=default_store_root(),
                   help="trial store root (default $CHUDWARE_VIEWNET_DATA/utility/store)")
    p.add_argument("--run-dir", type=Path, default=default_run_dir(),
                   help="training outputs (default $CHUDWARE_VIEWNET_DATA/utility/run)")


def _add_train_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--device", default="auto", help="auto | cuda | cpu")
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--weight-decay", type=float, default=None)
    p.add_argument("--face-dropout", type=float, default=None)
    p.add_argument("--batch", type=int, default=None, help="pairs per batch")
    p.add_argument("--max-epochs", type=int, default=None)
    p.add_argument("--patience", type=int, default=None)
    p.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m training.viewpoint.utility",
        description="ViewNet utility model: view sets, observer labels, training, "
                    "convergence report, held-out A/B.")
    sub = ap.add_subparsers(dest="cmd", required=True,
                            metavar="{trials,observe,train,evaluate,ab}")

    p = sub.add_parser("trials", help="build view sets per strategy, store tokens, render grids")
    _add_store_args(p)
    _add_trial_args(p)
    p.add_argument("--splits", default="train,val,test")
    p.add_argument("--random-sets", type=int, default=2, help="random view sets per k")
    p.add_argument("--k", default="1,4", help="view-set sizes")
    p.add_argument("--strategies", default=None,
                   help="comma list (default: random,iso,fixed4,hand_greedy,adversarial_low,"
                        "model_greedy)")
    p.add_argument("--weights", default=None,
                   help="UtilityModel NPZ for model_greedy sets (default: shipped weights if any)")

    p = sub.add_parser("observe", help="label every (set, query) pair with the observer")
    _add_store_args(p)
    p.add_argument("--judge", default="crossfit", choices=("crossfit", "independent"),
                   help="crossfit = training labels; independent = noise floor / A/B only")
    p.add_argument("--splits", default=None, help="comma list (default: all)")
    p.add_argument("--chunk", type=int, default=20000,
                   help="pairs per judge.label call (labels are appended after each chunk)")
    p.add_argument("--relabel", action="store_true",
                   help="label pairs again (after retraining the observer); newest row wins")

    p = sub.add_parser("train", help="regularisation sweep, fit 5 seeds, export + parity-check NPZ")
    _add_store_args(p)
    _add_train_args(p)
    p.add_argument("--no-sweep", action="store_true",
                   help="skip the (weight_decay, face_dropout) sweep; use the given values")

    p = sub.add_parser("evaluate", help="convergence report (learning curve, controls, baselines)")
    _add_store_args(p)
    _add_train_args(p)
    p.add_argument("--fractions", default="0.125,0.25,0.5,1")
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--control-seeds", type=int, default=3)

    p = sub.add_parser("ab", help="held-out A/B scored by the independent observer; --ship "
                                  "installs on pass")
    _add_store_args(p)
    _add_trial_args(p)
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--ship", action="store_true",
                   help="copy the NPZ to services/viewpoint/utility/weights/utility_v1.npz "
                        "only if the ship verdict passes")
    return ap


_CFG_FLAGS = {"lr": "lr", "weight_decay": "weight_decay", "face_dropout": "face_dropout",
              "batch": "batch_trials", "max_epochs": "max_epochs", "patience": "patience"}


def _train_cfg(a, base: dict = None):
    """TrainConfig from `base` (evaluate: the trained config) overridden by explicit flags."""
    from .train import TrainConfig  # noqa: PLC0415
    known = {f.name for f in fields(TrainConfig)}
    kw = {k: v for k, v in (base or {}).items() if k in known}
    kw.update({dest: getattr(a, flag) for flag, dest in _CFG_FLAGS.items()
               if getattr(a, flag) is not None})
    kw["device"] = a.device
    return TrainConfig(**kw)


def _trial_cfg(a, **extra):
    from .trials import TrialConfig  # noqa: PLC0415
    return TrialConfig(budget_minutes=a.budget_minutes, limit_parts=a.limit_parts,
                       targets_per_part=a.targets_per_part, seed=a.seed, tile=tuple(a.tile),
                       render_workers=a.render_workers, **extra)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "trials":
        from .store import TrialStore  # noqa: PLC0415
        from .trials import TRAINING_STRATEGIES, run_trials  # noqa: PLC0415
        strategies = _csv(a.strategies) if a.strategies else TRAINING_STRATEGIES
        unknown = set(strategies) - set(TRAINING_STRATEGIES)
        if unknown:
            raise SystemExit(f"unknown strategies: {sorted(unknown)}")
        cfg = _trial_cfg(a, splits=_csv(a.splits), random_sets=a.random_sets,
                         k_values=tuple(int(k) for k in _csv(a.k)),
                         strategies=strategies, weights=a.weights)
        out = run_trials(a.labels or _labels_default(), TrialStore(a.store), cfg)
    elif a.cmd == "observe":
        from .store import TrialStore  # noqa: PLC0415
        from .trials import observe  # noqa: PLC0415
        out = observe(TrialStore(a.store), judge=a.judge,
                      splits=_csv(a.splits) if a.splits else None, chunk=a.chunk,
                      relabel=a.relabel)
    elif a.cmd == "train":
        from .store import TrialStore  # noqa: PLC0415
        from .train import train_final  # noqa: PLC0415
        out = train_final(TrialStore(a.store), a.run_dir, seeds=a.seeds, cfg=_train_cfg(a),
                          sweep=not a.no_sweep, verbose=a.verbose)
    elif a.cmd == "evaluate":
        from .evaluate import convergence_report  # noqa: PLC0415
        from .store import TrialStore  # noqa: PLC0415
        trained = Path(a.run_dir) / "train_report.json"
        base = json.loads(trained.read_text())["config"] if trained.exists() else None
        rep = convergence_report(
            TrialStore(a.store), a.run_dir, seeds=a.seeds, fractions=tuple(
                float(f) for f in _csv(a.fractions)),
            cfg=_train_cfg(a, base), n_boot=a.n_boot, control_seeds=a.control_seeds,
            verbose=a.verbose)
        out = {"report": str(Path(a.run_dir) / "report.json"),
               "markdown": str(Path(a.run_dir) / "report.md"), "verdict": rep["verdict"]}
    else:
        from .evaluate import ab_report  # noqa: PLC0415
        res = ab_report(a.labels or _labels_default(), a.store, a.run_dir, _trial_cfg(a),
                        n_boot=a.n_boot, ship=a.ship)
        out = {"report": str(Path(a.run_dir) / "ab_report.json"), "verdict": res["verdict"],
               "shipped_to": res.get("shipped_to")}
        print(json.dumps(out, indent=2, default=str))
        return 0 if (res["verdict"]["ship"] or not a.ship) else 2
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
