"""Label (view set, query) pairs with a cross-fitted Observer's P(correct answer).

`label` answers every pair with the fold model that EXCLUDED the pair's part (fold_of with the
judge's salt) and refuses outright if that model's checkpoint lists the part among its training
parts. judge="crossfit" produces the utility model's training targets; judge="independent"
(another architecture, seed and fold assignment) is for the final A/B and the noise floor only.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Callable

from .queries import QUERY_SCHEMA, load_queries, queries_path
from .render import IMAGE_KEY, load_observer_sets
from .train import (JUDGES, OBSERVER_SCHEMA, TrainConfig, build_corpus, default_device,
                    fold_of, folds_meta_path, judge_dir, load_checkpoint, predict)


def _set_lookup(store) -> dict:
    sets = dict(store.load_sets()) if hasattr(store, "load_sets") else {}
    for k, r in load_observer_sets(store.root).items():
        sets.setdefault(k, r)
    return sets


def judge_meta(root, judge: str) -> dict:
    """folds.json of a trained judge; FileNotFoundError naming the path when absent."""
    if judge not in JUDGES:
        raise ValueError(f"unknown judge {judge!r}; expected one of {sorted(JUDGES)}")
    path = folds_meta_path(root, judge)
    if not path.exists():
        raise FileNotFoundError(f"Observer judge {judge!r} is not trained: {path} missing "
                                f"(run `python3 -m training.viewpoint.observer train "
                                f"--judge {judge}`)")
    meta = json.loads(path.read_text())
    if meta.get("schema") != OBSERVER_SCHEMA or meta.get("query_schema") != QUERY_SCHEMA:
        raise ValueError(f"{path}: schemas {meta.get('schema')!r}/"
                         f"{meta.get('query_schema')!r} != "
                         f"{OBSERVER_SCHEMA}/{QUERY_SCHEMA}")
    return meta


def fold_checkpoint(root, judge: str, meta: dict, fold: int):
    """Path of a fold's checkpoint; FileNotFoundError naming it when that fold is untrained."""
    entry = meta["folds"].get(str(fold))
    ck = judge_dir(root, judge) / (entry["path"] if entry else f"fold{fold}.pt")
    if entry is None or not ck.exists():
        raise FileNotFoundError(f"Observer judge {judge!r} fold {fold} checkpoint missing: {ck}")
    return ck


def observer_digest(meta: dict) -> str:
    """Identity of the judge's trained fold set (changes whenever any fold is retrained)."""
    blob = json.dumps([meta["judge"], meta["k"], meta["salt"],
                       [[f, meta["folds"][f]["digest"]] for f in sorted(meta["folds"])]])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def label(store, set_keys: list, qids: list, *, judge: str = "crossfit", device=None,
          tiles_per_batch: int = 128, log: Callable = print) -> list:
    """Rows {set_key, qid, part_id, fold, p_correct, correct, observer_digest, judge} for the
    zipped (set_key, qid) pairs, in input order."""
    set_keys, qids = list(set_keys), list(qids)
    if len(set_keys) != len(qids):
        raise ValueError(f"label: {len(set_keys)} set keys vs {len(qids)} qids (pairs expected)")
    meta = judge_meta(store.root, judge)
    digest = observer_digest(meta)
    k, salt = int(meta["k"]), meta["salt"]
    sets = _set_lookup(store)
    queries = load_queries(queries_path(store.root))
    for sk in set_keys:
        if sk not in sets:
            raise KeyError(f"label: view set {sk!r} is not in the store")
        if not sets[sk].get(IMAGE_KEY):
            raise KeyError(f"label: view set {sk!r} has no rendered image")
    for q in qids:
        if q not in queries:
            raise KeyError(f"label: query {q!r} not in {queries_path(store.root)}")
    device = device or default_device()
    order = sorted(set(set_keys))
    # Group the pairs by fold (one model in memory at a time), then by set.
    by_fold: dict = defaultdict(lambda: defaultdict(list))
    for sk, q in zip(set_keys, qids):
        rec = sets[sk]
        if queries[q].part_id != rec["part_id"]:
            raise ValueError(f"label: query {q} is about part {queries[q].part_id!r}, set {sk} "
                             f"shows {rec['part_id']!r}")
        by_fold[fold_of(rec["part_id"], k, salt)][sk].append(q)
    ckpts = {f: fold_checkpoint(store.root, judge, meta, f) for f in sorted(by_fold)}
    results: dict = {}
    t0 = time.time()
    for f, path in ckpts.items():
        model, ck = load_checkpoint(path, device)
        if int(ck["fold"]) != f or ck["salt"] != salt:
            raise ValueError(f"{path}: checkpoint is fold {ck['fold']}/{ck['salt']}, "
                             f"folds.json says fold {f}/{salt}")
        trained = set(ck["trained_parts"])
        pairs = by_fold[f]
        seen = sorted({sets[sk]["part_id"] for sk in pairs} & trained)
        if seen:
            raise RuntimeError(f"cross-fit violation: {judge} fold {f} trained on {seen[:5]} — "
                               f"it cannot label them")
        cfg = TrainConfig(**{kk: v for kk, v in ck["train_config"].items()
                             if kk in TrainConfig.__dataclass_fields__})
        cfg.tile_hw = tuple(cfg.tile_hw)
        cfg.tiles_per_batch = tiles_per_batch
        recs = [dict(sets[sk], set_key=sk) for sk in sorted(pairs)]
        corpus = build_corpus(store.root, recs, queries, cfg.tile_hw, log=log)
        index = {s["set_key"]: i for i, s in enumerate(corpus.sets)}
        missing = [sk for sk in pairs if sk not in index]
        if missing:
            raise ValueError(f"label: view sets {missing[:4]} could not be prepared "
                             f"(no image tiles or no matching queries)")
        items = [(index[sk], sorted(set(q))) for sk, q in pairs.items()]
        for r in predict(model, corpus, items, device=device, cfg=cfg):
            sk = corpus.sets[r["set"]]["set_key"]
            results[(sk, r["qid"])] = {
                "set_key": sk, "qid": r["qid"], "part_id": corpus.sets[r["set"]]["part_id"],
                "fold": f, "p_correct": r["p"], "correct": bool(r["correct"]),
                "observer_digest": digest, "judge": judge}
        del model
    log(f"[observer] {judge}: labelled {len(results)} pairs over {len(order)} sets in "
        f"{time.time() - t0:.1f}s")
    return [results[(sk, q)] for sk, q in zip(set_keys, qids)]


def all_pairs(store, *, observer_sets: bool = False, trained_folds_of: str = "") -> tuple:
    """(set_keys, qids) of every set x its applicable queries: the store namespace's sets, or
    with `observer_sets` the Observer's own sets. `trained_folds_of=<judge>` keeps only parts
    whose fold model of that judge exists (a partial cross-fit labels what it can)."""
    queries = load_queries(queries_path(store.root))
    by = defaultdict(list)
    for q in queries.values():
        by[(q.part_id, q.mode, q.target_instance)].append(q.qid)
    sets = load_observer_sets(store.root) if observer_sets else store.load_sets()
    keep_fold = None
    if trained_folds_of:
        meta = judge_meta(store.root, trained_folds_of)
        k, salt = int(meta["k"]), meta["salt"]
        trained = {int(f) for f in meta["folds"]}
        keep_fold = lambda pid: fold_of(pid, k, salt) in trained  # noqa: E731
    sks, qs = [], []
    for sk, rec in sorted(sets.items()):
        if not rec.get(IMAGE_KEY) or (keep_fold is not None and not keep_fold(rec["part_id"])):
            continue
        ti = int(rec.get("target_instance", -1)) if rec["mode"] == "target" else -1
        for q in sorted(by.get((rec["part_id"], rec["mode"], ti), [])):
            sks.append(sk)
            qs.append(q)
    return sks, qs


__all__ = ["all_pairs", "judge_meta", "label", "observer_digest"]
