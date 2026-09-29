"""Cross-fitting never labels a part with a model that saw it, and a tiny run learns a planted
pixel signal (well above chance) that the blind control cannot see."""
from __future__ import annotations

import json
import types

import numpy as np
import pytest
import torch

from training.viewpoint.observer import judge as J
from training.viewpoint.observer import queries as Q
from training.viewpoint.observer import train as T
from training.viewpoint.observer.render import IMAGE_KEY, observer_sets_path

_TILE = (96, 72)                 # rendered tile W x H in the synthetic grids
_HW = (48, 64)                   # model tile H x W


def _grid_png(path, tiles: list) -> None:
    from PIL import Image
    k = len(tiles)
    cols, rows = T.grid_layout(k)
    W = _TILE[0] * cols + T.TILE_GAP * (cols - 1)
    H = _TILE[1] * rows + T.TILE_GAP * (rows - 1)
    sheet = Image.new("RGB", (W, H), (30, 30, 32))
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet.paste(Image.fromarray(t), (c * (_TILE[0] + T.TILE_GAP), r * (_TILE[1] + T.TILE_GAP)))
    sheet.save(path)


def _tile(rng, side) -> np.ndarray:
    """Grey noise; with `side` set, a bright square in one of six slots (2 rows x 3 cols)."""
    t = rng.integers(90, 140, size=(_TILE[1], _TILE[0], 3), dtype=np.uint8)
    if side is not None:
        r, c = divmod(side, 3)
        y0, x0 = 6 + r * 34, 4 + c * 31
        t[y0:y0 + 26, x0:x0 + 24] = (240, 60, 40)
    return t


def _store(tmp_path, n_parts: int, sets_per_part: int, seed: int = 0):
    """Parts with one presence_side query each; the answer is painted in the pixels (the
    square's slot) in exactly one tile of every set."""
    rng = np.random.default_rng(seed)
    root = tmp_path / "store"
    (root / "images").mkdir(parents=True)
    queries, sets = [], []
    for p in range(n_parts):
        pid = f"synth/{p:03d}"
        side = int(rng.integers(0, 6))
        vec = Q._vec("presence_side", "part", Q.FAMILY_CLASSES["pocket"],
                     np.zeros(3, np.float32), None)
        queries.append(Q._make(pid, "presence_side", "part", -1, vec, side, Q.SIDES[side],
                               "pocket"))
        for s in range(sets_per_part):
            k = 1 if s % 2 == 0 else 4
            hot = int(rng.integers(0, k))
            img = root / "images" / f"{p:03d}_{s}.png"
            _grid_png(img, [_tile(rng, side if j == hot else None) for j in range(k)])
            sets.append({"set_key": f"s{p:03d}_{s}", "part_id": pid, "split": "train",
                         "mode": "part", "target_instance": -1, "k": k,
                         IMAGE_KEY: f"images/{img.name}", "strategies": ["random"]})
    Q.save_queries(Q.queries_path(root), queries)
    path = observer_sets_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in sets))
    return types.SimpleNamespace(root=root)


def _cfg(**kw) -> T.TrainConfig:
    base = dict(variant="a", d=32, width=8, tile_hw=_HW, epochs=2, min_epochs=1, patience=50,
                tiles_per_batch=16, workers=0, lr=3e-3, amp=False, augment=False, seed=0)
    base.update(kw)
    return T.TrainConfig(**base)


def _corpus(store):
    return T.build_corpus(store.root, T.training_sets(store), Q.load_queries(
        Q.queries_path(store.root)), _HW, workers=1, log=lambda *_: None)


def _label(store, sks, qids, judge):
    return J.label(store, sks, qids, judge=judge, device=torch.device("cpu"),
                   log=lambda *_: None)


def test_grid_split_matches_the_shot_grid_layout(tmp_path):
    rng = np.random.default_rng(1)
    tiles = [_tile(rng, j) for j in range(4)]
    _grid_png(tmp_path / "g.png", tiles)
    arr = T.tile_array(tmp_path / "g.png", 4, (_TILE[1], _TILE[0]))
    assert arr.shape == (4, _TILE[1], _TILE[0], 3)
    assert all(np.array_equal(arr[j], tiles[j]) for j in range(4))


def test_crossfit_never_labels_a_part_with_a_model_that_saw_it(tmp_path):
    store = _store(tmp_path, n_parts=15, sets_per_part=2)
    corpus = _corpus(store)
    k = 3
    meta = T.fit_folds(store, k=k, judge="crossfit", cfg=_cfg(epochs=1), corpus=corpus,
                       device=torch.device("cpu"), log=lambda *_: None)
    salt = T.JUDGES["crossfit"].salt
    for f in range(k):
        _m, ck = T.load_checkpoint(T.judge_dir(store.root, "crossfit") / f"fold{f}.pt",
                                   torch.device("cpu"))
        held = {p for p in corpus.parts() if T.fold_of(p, k, salt) == f}
        assert held and not held & set(ck["trained_parts"])
        assert set(ck["trained_parts"]) == set(corpus.parts()) - held
    sks = [s["set_key"] for s in corpus.sets]
    qids = [corpus.set_qids[i][0] for i in range(len(corpus.sets))]
    rows = _label(store, sks, qids, "crossfit")
    assert [(r["set_key"], r["qid"]) for r in rows] == list(zip(sks, qids))
    for r in rows:
        assert r["fold"] == T.fold_of(r["part_id"], k, salt)
        assert 0.0 <= r["p_correct"] <= 1.0
        assert r["observer_digest"] == J.observer_digest(meta)

    # A checkpoint that claims to have trained on a part of its own fold must be refused.
    path = T.judge_dir(store.root, "crossfit") / "fold0.pt"
    ck = torch.load(path, weights_only=False)
    victim = next(p for p in corpus.parts() if T.fold_of(p, k, salt) == 0)
    ck["trained_parts"] = sorted(ck["trained_parts"] + [victim])
    torch.save(ck, path)
    with pytest.raises(RuntimeError, match="cross-fit violation"):
        _label(store, sks, qids, "crossfit")

    path.unlink()
    with pytest.raises(FileNotFoundError, match="fold0.pt"):
        _label(store, sks, qids, "crossfit")
    with pytest.raises(FileNotFoundError, match="independent"):
        _label(store, sks, qids, "independent")


def test_tiny_run_learns_a_planted_pixel_signal(tmp_path):
    store = _store(tmp_path, n_parts=70, sets_per_part=4, seed=3)
    corpus = _corpus(store)
    parts = corpus.parts()
    held = set(parts[:14])
    tr = [i for i, s in enumerate(corpus.sets) if s["part_id"] not in held]
    ev = [i for i, s in enumerate(corpus.sets) if s["part_id"] in held]
    device = T.default_device()
    cfg = _cfg(epochs=20, amp=device.type == "cuda", augment=True)
    model, _info = T.train_observer(corpus, tr, [], cfg, device=device, log=lambda *_: None)
    acc = T.kind_metrics(T.predict(model, corpus, T.items_for(corpus, ev), device=device,
                                   cfg=cfg), corpus)["all"]["acc"]
    blind, _ = T.train_observer(corpus, tr, [], cfg, device=device, blind=True,
                                log=lambda *_: None)
    blind_acc = T.kind_metrics(T.predict(blind, corpus, T.items_for(corpus, ev), device=device,
                                         cfg=cfg, blind=True), corpus)["all"]["acc"]
    chance = 1.0 / 6.0
    assert acc > chance + 0.4, (acc, blind_acc)
    assert blind_acc < chance + 0.2, (acc, blind_acc)
