"""CLI: python3 -m training.viewpoint.observer {render,train,label,report}

render  plan + render the Observer's own random-ish training view sets (engine workers)
train   fit a judge's K fold models (part cross-fitting)                          (torch)
label   label every (view set, query) pair of a store namespace with a judge      (torch)
report  learning curves per kind vs train fraction, seed spread, controls         (torch)
See README.md in this directory.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path


def _hw(text: str) -> tuple:
    a, _, b = text.lower().partition("x")
    return int(a), int(b)


def _csv(text: str) -> tuple:
    return tuple(s.strip() for s in text.split(",") if s.strip())


def _store(args, name: str = "trials"):
    from training.viewpoint.utility.store import TrialStore  # noqa: PLC0415
    return TrialStore(args.store, name=name)


def _add_store(p: argparse.ArgumentParser) -> None:
    from training.viewpoint.utility.store import default_store_root  # noqa: PLC0415
    p.add_argument("--store", type=Path, default=default_store_root(),
                   help="trial store root (default $CHUDWARE_VIEWNET_DATA/utility/store)")


def _add_train(p: argparse.ArgumentParser) -> None:
    p.add_argument("--device", default="auto", help="auto | cuda | cpu")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--tile-hw", type=_hw, default=None, help="model tile size HxW (192x256)")
    p.add_argument("--tiles-per-batch", type=int, default=None)
    p.add_argument("--lr", type=float, default=None)
    p.add_argument("--workers", type=int, default=None, help="data-loader processes")
    p.add_argument("--k", type=int, default=5, help="cross-fitting folds")


def _device(args):
    import torch  # noqa: PLC0415
    from .train import default_device  # noqa: PLC0415
    return default_device() if args.device == "auto" else torch.device(args.device)


def _cfg(args, judge: str):
    from .train import JUDGES, TrainConfig  # noqa: PLC0415
    spec = JUDGES[judge]
    over = {"epochs": args.epochs, "tile_hw": args.tile_hw, "tiles_per_batch":
            args.tiles_per_batch, "lr": args.lr, "workers": args.workers}
    return TrainConfig(variant=spec.variant, seed=spec.seed,
                       **{k: v for k, v in over.items() if v is not None})


def cmd_render(args) -> int:
    from training.viewpoint.utility.trials import load_labels  # noqa: PLC0415
    from . import queries as Q  # noqa: PLC0415
    from .render import ObserverSetConfig, render_observer_sets  # noqa: PLC0415

    if args.labels is None:
        from training.viewpoint.datasets.build_manifest import default_manifest_path  # noqa: PLC0415
        args.labels = default_manifest_path()
    records = load_labels(args.labels, args.splits)
    if args.limit_parts:
        records = records[:args.limit_parts]
    store = _store(args)
    t0 = time.time()
    bank = Q.build_query_bank(records, seed=args.seed)
    added = Q.save_queries(Q.queries_path(store.root), bank)
    by_part = defaultdict(list)
    for q in bank:
        by_part[q.part_id].append(q)
    targets = {p: Q.pick_targets(qs, args.targets_per_part, seed=args.seed)
               for p, qs in by_part.items()}
    print(f"[observer] {len(records)} parts, {len(bank)} balanced queries ({added} new) in "
          f"{time.time() - t0:.1f}s; chance {Q.chance_by_kind(bank)}", file=sys.stderr)
    cfg = ObserverSetConfig(random_k1=args.random_k1, random_k4=args.random_k4,
                            target_random_k1=args.target_random_k1,
                            target_random_k4=args.target_random_k4, greedy=not args.no_greedy,
                            targets_per_part=args.targets_per_part, tile=args.tile,
                            seed=args.seed)
    summary = render_observer_sets([r for r in records if r["part_id"] in by_part], targets,
                                   store=store, cfg=cfg, workers=args.workers)
    print(json.dumps(summary, indent=1))
    return 0 if summary["errors"] == 0 else 1


def cmd_train(args) -> int:
    from .train import fit_folds  # noqa: PLC0415

    folds = None if args.folds == "all" else [int(f) for f in _csv(args.folds)]
    meta = fit_folds(_store(args), k=args.k, judge=args.judge, folds=folds,
                     cfg=_cfg(args, args.judge), device=_device(args))
    print(json.dumps({"judge": meta["judge"], "k": meta["k"], "seconds": meta["seconds"],
                      "folds": {f: {"oof": v["oof"], "train_seconds": v["info"]["train_seconds"],
                                    "best_epoch": v["info"]["best_epoch"]}
                                for f, v in meta["folds"].items()}}, indent=1))
    return 0


def cmd_label(args) -> int:
    from .judge import all_pairs, label  # noqa: PLC0415

    store = _store(args, "trials" if args.namespace == "observer" else args.namespace)
    sks, qids = all_pairs(store, observer_sets=args.namespace == "observer",
                          trained_folds_of=args.judge if args.trained_folds_only else "")
    if not sks:
        print(f"[observer] no (set, query) pairs to label in namespace {args.namespace!r}",
              file=sys.stderr)
        return 1
    if args.limit_pairs:
        sks, qids = sks[:args.limit_pairs], qids[:args.limit_pairs]
    t0 = time.time()
    rows = label(store, sks, qids, judge=args.judge, device=_device(args))
    dt = time.time() - t0
    if args.namespace == "observer":
        from .train import observer_dir  # noqa: PLC0415
        out = observer_dir(store.root) / f"labels_{args.judge}.jsonl"
        with open(out, "a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    else:
        store.append_labels(args.judge, rows)
        out = store.labels_path(args.judge)
    queries = store.load_queries()
    by = defaultdict(list)
    for r in rows:
        by[queries[r["qid"]].kind].append(r)
    print(json.dumps({"pairs": len(rows), "seconds": round(dt, 2),
                      "pairs_per_s": round(len(rows) / max(dt, 1e-9), 1),
                      "labels": str(out),
                      "per_kind": {k: {"n": len(v),
                                       "acc": sum(r["correct"] for r in v) / len(v),
                                       "mean_p": sum(r["p_correct"] for r in v) / len(v)}
                                   for k, v in sorted(by.items())}}, indent=1))
    return 0


def cmd_report(args) -> int:
    from .train import format_report, learning_report  # noqa: PLC0415

    rep = learning_report(_store(args), fractions=tuple(float(f) for f in _csv(args.fractions)),
                          seeds=tuple(int(s) for s in _csv(args.seeds)), k=args.k,
                          fold=args.fold, cfg=_cfg(args, "crossfit"),
                          controls=not args.no_controls, device=_device(args))
    print(format_report(rep))
    print(f"report: {rep['path']} ({rep['seconds']:.0f}s)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m training.viewpoint.observer",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("render", help="render the Observer's own training view sets")
    _add_store(p)
    p.add_argument("--labels", type=Path, default=None,
                   help="chudware.geomlabel/1 JSONL (default: the DatasetsLabels manifest)")
    p.add_argument("--splits", type=_csv, default=("train", "val", "test"))
    p.add_argument("--limit-parts", type=int, default=None)
    p.add_argument("--workers", type=int, default=None,
                   help="render processes (default $CHUDWARE_VIEWNET_RENDER_WORKERS or cpus-2)")
    p.add_argument("--targets-per-part", type=int, default=2)
    p.add_argument("--random-k1", type=int, default=3)
    p.add_argument("--random-k4", type=int, default=3)
    p.add_argument("--target-random-k1", type=int, default=2)
    p.add_argument("--target-random-k4", type=int, default=2)
    p.add_argument("--no-greedy", action="store_true", help="skip the hand-greedy k=1/k=4 sets")
    p.add_argument("--tile", type=_hw, default=(512, 384), help="render tile WxH")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("train", help="fit a judge's cross-fitted fold models")
    _add_store(p)
    _add_train(p)
    p.add_argument("--judge", choices=("crossfit", "independent"), default="crossfit")
    p.add_argument("--folds", default="all", help="'all' or comma-separated fold ids")
    p.set_defaults(fn=cmd_train)

    p = sub.add_parser("label", help="label every (set, query) pair of a store namespace")
    _add_store(p)
    p.add_argument("--device", default="auto")
    p.add_argument("--judge", choices=("crossfit", "independent"), default="crossfit")
    p.add_argument("--namespace", default="trials",
                   help="trials | ab (trial-store namespaces) | observer (the Observer's own "
                        "sets; their labels measure the Observer, the utility ignores them)")
    p.add_argument("--trained-folds-only", action="store_true",
                   help="label only parts whose fold model exists (partial cross-fit)")
    p.add_argument("--limit-pairs", type=int, default=None)
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("report", help="learning curves, seed spread, controls")
    _add_store(p)
    _add_train(p)
    p.add_argument("--fractions", default="0.125,0.25,0.5,1")
    p.add_argument("--seeds", default="0,1,2")
    p.add_argument("--fold", type=int, default=0, help="held-out fold of the crossfit split")
    p.add_argument("--no-controls", action="store_true")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
