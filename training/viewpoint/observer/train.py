"""Observer training: tile cache, corpus, K-fold part cross-fitting, learning-curve report.

Cross-fitting: every part gets a fold ``fold_of(part_id, K, salt)``; fold model f trains on
the parts of every OTHER fold (minus an internal early-stopping slice of them), so labelling a
part with its own fold's model is always out-of-sample. Each checkpoint records the exact
part ids it trained on and judge.label refuses a part found there.

Two cross-fits exist (``JUDGES``): "crossfit" (variant a) produces the utility model's
training labels; "independent" (variant b, other seed, other fold salt) is ONLY for the final
A/B and the noise floor — different architecture, initialisation and fold boundaries, so the
utility model cannot win the A/B by fitting one observer's quirks.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset

from .model import ModelConfig, ObserverNet, augment, p_answer, param_count, query_loss
from .queries import A_MAX, QD, QUERY_SCHEMA, load_queries, queries_path
from .render import IMAGE_KEY, load_observer_sets

OBSERVER_SCHEMA = "viewnet.observer/1"
TILE_GAP = 4                      # shot_planner._compose's gutter between tiles


@dataclass(frozen=True)
class JudgeSpec:
    variant: str
    seed: int
    salt: str


JUDGES = {"crossfit": JudgeSpec("a", 0, "crossfit-v1"),
          "independent": JudgeSpec("b", 1, "independent-v1")}


def _h(*parts) -> int:
    return int(hashlib.sha1(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:12], 16)


def fold_of(part_id: str, k: int, salt: str) -> int:
    return _h(salt, "fold", part_id) % int(k)


def observer_dir(root) -> Path:
    return Path(root).expanduser() / "observer"


def judge_dir(root, judge: str) -> Path:
    return observer_dir(root) / judge


# ---------------------------------------------------------------------------- tiles

def grid_layout(k: int) -> tuple:
    cols = 1 if k == 1 else (2 if k <= 4 else 3)
    return cols, (k + cols - 1) // cols


def split_grid(img, k: int) -> list:
    """The k tiles of a render_shot_grid sheet (cols x rows, TILE_GAP gutters)."""
    cols, rows = grid_layout(k)
    W, H = img.size
    tw, th = (W - TILE_GAP * (cols - 1)) // cols, (H - TILE_GAP * (rows - 1)) // rows
    if tw * cols + TILE_GAP * (cols - 1) != W or th * rows + TILE_GAP * (rows - 1) != H:
        raise ValueError(f"grid {W}x{H} is not a {cols}x{rows} sheet with {TILE_GAP}px gutters")
    out = []
    for i in range(k):
        r, c = divmod(i, cols)
        x, y = c * (tw + TILE_GAP), r * (th + TILE_GAP)
        out.append(img.crop((x, y, x + tw, y + th)))
    return out


def tile_array(png_path, k: int, tile_hw: tuple) -> np.ndarray:
    """uint8 [k, H, W, 3]: each tile box-filtered (area average) to tile_hw."""
    from PIL import Image  # noqa: PLC0415
    with Image.open(png_path) as im0:
        im = im0.convert("RGB")
    H, W = int(tile_hw[0]), int(tile_hw[1])
    return np.stack([np.asarray(t.resize((W, H), Image.BOX), np.uint8)
                     for t in split_grid(im, k)])


def tile_cache_path(root, image_rel: str, tile_hw: tuple) -> Path:
    return (observer_dir(root) / "tiles" / f"{int(tile_hw[0])}x{int(tile_hw[1])}"
            / f"{Path(image_rel).stem}.npy")


def _build_tile(args) -> Optional[str]:
    root, image_rel, k, tile_hw = args
    dest = tile_cache_path(root, image_rel, tile_hw)
    if dest.exists():
        return None
    try:
        arr = tile_array(Path(root).expanduser() / image_rel, k, tile_hw)
    except Exception as e:  # noqa: BLE001
        return f"{image_rel}: {type(e).__name__}: {e}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.npy")
    np.save(tmp, arr)
    os.replace(tmp, dest)
    return None


def prepare_tiles(root, items: Iterable, tile_hw: tuple, *, workers: Optional[int] = None,
                  log: Callable = print) -> dict:
    """Decode + cut + downsample every (image_rel, k) not cached yet, in parallel."""
    todo = sorted({(rel, int(k)) for rel, k in items
                   if not tile_cache_path(root, rel, tile_hw).exists()})
    t0 = time.time()
    errors: list = []
    if todo:
        args = [(str(root), rel, k, tuple(tile_hw)) for rel, k in todo]
        n = max(1, min(workers or (os.cpu_count() or 4), len(args)))
        if n == 1:
            errors = [e for e in map(_build_tile, args) if e]
        else:
            import multiprocessing as mp  # noqa: PLC0415
            with ProcessPoolExecutor(n, mp_context=mp.get_context("spawn")) as ex:
                errors = [e for e in ex.map(_build_tile, args, chunksize=16) if e]
        log(f"[observer] tiles: built {len(todo) - len(errors)} in {time.time() - t0:.1f}s"
            + (f", {len(errors)} failed: {errors[:3]}" if errors else ""))
    return {"built": len(todo) - len(errors), "errors": errors}


# ---------------------------------------------------------------------------- corpus

@dataclass
class Corpus:
    """View sets with images + the queries each can be asked. ``set_qids[i]`` lists qids of
    set i's part whose mode / target instance match the set."""
    root: Path
    sets: list
    queries: dict
    set_qids: list
    tile_hw: tuple

    def parts(self, idx: Optional[Iterable] = None) -> list:
        rng = range(len(self.sets)) if idx is None else idx
        return sorted({self.sets[i]["part_id"] for i in rng})

    def tiles(self, i: int) -> np.ndarray:
        s = self.sets[i]
        return np.load(tile_cache_path(self.root, s[IMAGE_KEY], self.tile_hw))


def _index_queries(queries: dict) -> dict:
    by = defaultdict(list)
    for q in queries.values():
        by[(q.part_id, q.mode, q.target_instance)].append(q.qid)
    return {k: sorted(v) for k, v in by.items()}


def build_corpus(root, set_records: Iterable, queries: dict, tile_hw: tuple, *,
                 prepare: bool = True, workers: Optional[int] = None,
                 log: Callable = print) -> Corpus:
    root = Path(root).expanduser()
    index = _index_queries(queries)
    sets, set_qids = [], []
    for r in set_records:
        if not r.get(IMAGE_KEY):
            continue
        ti = int(r.get("target_instance", -1)) if r["mode"] == "target" else -1
        qids = index.get((r["part_id"], r["mode"], ti), [])
        if qids:
            sets.append(r)
            set_qids.append(qids)
    if prepare:
        res = prepare_tiles(root, [(s[IMAGE_KEY], s["k"]) for s in sets], tile_hw,
                            workers=workers, log=log)
        if res["errors"]:
            bad = {e.split(":", 1)[0] for e in res["errors"]}
            keep = [i for i, s in enumerate(sets) if s[IMAGE_KEY] not in bad]
            sets, set_qids = [sets[i] for i in keep], [set_qids[i] for i in keep]
    return Corpus(root=root, sets=sets, queries=queries, set_qids=set_qids,
                  tile_hw=tuple(tile_hw))


def training_sets(store) -> list:
    """The trial store's training-namespace sets plus the Observer's own random sets."""
    recs = dict(store.load_sets()) if hasattr(store, "load_sets") else {}
    for k, r in load_observer_sets(store.root).items():
        recs.setdefault(k, r)
    return [recs[k] for k in sorted(recs)]


def load_corpus(store, tile_hw: tuple, *, workers: Optional[int] = None,
                log: Callable = print) -> Corpus:
    queries = load_queries(queries_path(store.root))
    if not queries:
        raise FileNotFoundError(f"no Observer queries at {queries_path(store.root)} "
                                f"(run `python3 -m training.viewpoint.observer render`)")
    return build_corpus(store.root, training_sets(store), queries, tile_hw, workers=workers,
                        log=log)


# ---------------------------------------------------------------------------- data

class _SetData(Dataset):
    """Item = (set index, qid tuple) -> tiles + those queries."""

    def __init__(self, corpus: Corpus, answer_override: Optional[dict] = None):
        self.c = corpus
        self.override = answer_override or {}

    def __len__(self) -> int:
        return len(self.c.sets)

    def __getitem__(self, item):
        i, qids = item
        qs = [self.c.queries[q] for q in qids]
        return {"tiles": self.c.tiles(i),                 # uint8 [k, H, W, 3] (NHWC)
                "qvec": np.stack([q.vec for q in qs]).astype(np.float32),
                "mask": np.stack([q.option_mask for q in qs]),
                "ans": np.asarray([self.override.get(q.qid, q.answer) for q in qs], np.int64),
                "set": i, "qids": list(qids)}


def _collate(items: list) -> dict:
    """numpy assembly, tiles kept NHWC and permuted on the GPU: torch's CPU permute/copy of the
    uint8 tiles was measured at 5.1 s for 347 grids, the whole numpy path predicts them in 0.35 s."""
    B = len(items)
    T = max(it["tiles"].shape[0] for it in items)
    Q = max(len(it["qids"]) for it in items)
    t0 = items[0]["tiles"]
    tiles = np.zeros((B, T, *t0.shape[1:]), np.uint8)
    tmask = np.zeros((B, T), bool)
    qvec = np.zeros((B, Q, QD), np.float32)
    mask = np.zeros((B, Q, A_MAX), bool)
    mask[..., 0] = True                   # padded queries keep one finite logit
    ans = np.zeros((B, Q), np.int64)
    qvalid = np.zeros((B, Q), bool)
    for b, it in enumerate(items):
        k, n = it["tiles"].shape[0], len(it["qids"])
        tiles[b, :k] = it["tiles"]
        tmask[b, :k] = True
        qvec[b, :n] = it["qvec"]
        mask[b, :n] = it["mask"]
        ans[b, :n] = it["ans"]
        qvalid[b, :n] = True
    return {"tiles": torch.from_numpy(tiles), "tmask": torch.from_numpy(tmask),
            "qvec": torch.from_numpy(qvec), "mask": torch.from_numpy(mask),
            "ans": torch.from_numpy(ans), "qvalid": torch.from_numpy(qvalid),
            "sets": [it["set"] for it in items], "qids": [it["qids"] for it in items]}


class _Batches:
    """Batch sampler: sets bucketed by tile count (no padded tiles), ~tiles_per_batch tiles per
    batch; in training each set carries a fresh random draw of up to q_per_set of its queries
    every epoch, in evaluation every requested query (chunked to q_per_set)."""

    def __init__(self, corpus: Corpus, items: list, tiles_per_batch: int, *, train: bool,
                 q_per_set: int, seed: int):
        self.c = corpus
        self.items = items            # [(set index, [qid...])]
        self.tpb = int(tiles_per_batch)
        self.train = train
        self.qps = int(q_per_set)
        self.seed = seed
        self.epoch = 0

    def _plan(self) -> list:
        rng = random.Random(_h(self.seed, "epoch", self.epoch))
        entries = []
        for i, qids in self.items:
            if self.train:
                qs = list(qids)
                rng.shuffle(qs)
                entries.append((i, tuple(sorted(qs[:self.qps]))))
            else:
                for j in range(0, len(qids), max(self.qps, 16)):
                    entries.append((i, tuple(qids[j:j + max(self.qps, 16)])))
        by_k = defaultdict(list)
        for e in entries:
            by_k[int(self.c.sets[e[0]]["k"])].append(e)
        batches = []
        for k in sorted(by_k):
            group = by_k[k]
            if self.train:
                rng.shuffle(group)
            bs = max(1, self.tpb // k)
            batches += [group[j:j + bs] for j in range(0, len(group), bs)]
        if self.train:
            rng.shuffle(batches)
        return batches

    def __iter__(self):
        yield from self._plan()
        self.epoch += 1

    def __len__(self) -> int:
        return len(self._plan())


# ---------------------------------------------------------------------------- training

@dataclass
class TrainConfig:
    variant: str = "a"
    d: int = 192
    width: int = 0
    tile_hw: tuple = (192, 256)
    epochs: int = 24
    min_epochs: int = 4
    patience: int = 5
    lr: float = 1e-3
    weight_decay: float = 0.05
    warmup_frac: float = 0.05
    tiles_per_batch: int = 64
    q_per_set: int = 4
    label_smoothing: float = 0.05
    dropout: float = 0.1
    augment: bool = True
    amp: bool = True
    workers: int = 6
    seed: int = 0

    def model_config(self) -> ModelConfig:
        return ModelConfig(variant=self.variant, d=self.d, tile_hw=tuple(self.tile_hw),
                           width=self.width, dropout=self.dropout)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tile_hw"] = list(self.tile_hw)
        return d


def default_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _one_thread(_worker_id: int) -> None:
    # Loader processes only np.load + memcpy; torch's default intra-op pool per process would
    # oversubscribe the CPUs the main process and the render workers need.
    torch.set_num_threads(1)


def _loader(corpus: Corpus, sampler: _Batches, workers: int, override=None) -> DataLoader:
    kw = {}
    if workers > 0:
        import multiprocessing as mp  # noqa: PLC0415
        kw = {"num_workers": workers, "persistent_workers": True, "prefetch_factor": 4,
              "multiprocessing_context": mp.get_context("spawn"), "worker_init_fn": _one_thread}
    return DataLoader(_SetData(corpus, override), batch_sampler=sampler, collate_fn=_collate,
                      pin_memory=torch.cuda.is_available(), **kw)


def _inputs(batch: dict, device, *, train: bool, gen, cfg: TrainConfig, blind: bool):
    tiles = batch["tiles"].to(device, non_blocking=True)          # uint8 [B, T, H, W, 3]
    B, T = tiles.shape[:2]
    x = tiles.reshape(B * T, *tiles.shape[2:]).permute(0, 3, 1, 2).float().div_(255.0)
    if train and cfg.augment:
        x = augment(x, gen)
    if blind:
        x = torch.zeros_like(x)
    return (x.reshape(B, T, *x.shape[1:]), batch["tmask"].to(device),
            batch["qvec"].to(device), batch["mask"].to(device))


def _autocast(device, amp: bool):
    if amp and device.type == "cuda":
        return torch.autocast("cuda", dtype=torch.bfloat16)
    return torch.autocast("cpu", enabled=False)


@torch.no_grad()
def predict(model: ObserverNet, corpus: Corpus, items: list, *, device, cfg: TrainConfig,
            blind: bool = False, override: Optional[dict] = None) -> list:
    """Rows {set, qid, kind, p, pred, answer, correct} for [(set index, [qid...])], every
    requested query answered. Loads in-process: npy tiles + numpy collate run ~1000 sets/s,
    faster than the GPU needs, while spawning loader workers costs ~10 s per call."""
    model.eval()
    rows = []
    sampler = _Batches(corpus, items, cfg.tiles_per_batch * 2, train=False,
                       q_per_set=cfg.q_per_set, seed=0)
    for batch in _loader(corpus, sampler, 0, override):
        tiles, tmask, qvec, mask = _inputs(batch, device, train=False, gen=None, cfg=cfg,
                                           blind=blind)
        with _autocast(device, cfg.amp):
            logits = model(tiles, tmask, qvec, mask)
        ans = batch["ans"].to(device)
        p = p_answer(logits, ans).cpu().numpy()
        pred = logits.argmax(-1).cpu().numpy()
        a = batch["ans"].numpy()
        for b, (i, qids) in enumerate(zip(batch["sets"], batch["qids"])):
            for j, qid in enumerate(qids):
                rows.append({"set": i, "qid": qid, "kind": corpus.queries[qid].kind,
                             "p": float(p[b, j]), "pred": int(pred[b, j]),
                             "answer": int(a[b, j]), "correct": bool(pred[b, j] == a[b, j])})
    return rows


def kind_metrics(rows: list, corpus: Corpus) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["kind"]].append(r)
        by["all"].append(r)
    out = {}
    for k, rs in sorted(by.items()):
        p = np.clip(np.asarray([r["p"] for r in rs]), 1e-7, 1.0)
        out[k] = {"n": len(rs), "acc": float(np.mean([r["correct"] for r in rs])),
                  "mean_p": float(p.mean()), "logloss": float(-np.log(p).mean()),
                  "chance": float(np.mean([corpus.queries[r["qid"]].chance for r in rs]))}
    return out


def items_for(corpus: Corpus, set_idx: Iterable) -> list:
    return [(i, list(corpus.set_qids[i])) for i in set_idx]


def train_observer(corpus: Corpus, train_idx: list, val_idx: list, cfg: TrainConfig, *,
                   device=None, override: Optional[dict] = None, blind: bool = False,
                   log: Callable = print) -> tuple:
    """Fit one ObserverNet; early-stops on val log-loss (fixed epochs when val_idx is empty).
    Returns (model with the best weights, info)."""
    device = device or default_device()
    torch.manual_seed(cfg.seed)
    gen = torch.Generator(device=device)
    gen.manual_seed(cfg.seed)
    if device.type == "cuda":
        torch.backends.cudnn.benchmark = True
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    model = ObserverNet(cfg.model_config()).to(device).to(memory_format=torch.channels_last)
    sampler = _Batches(corpus, items_for(corpus, train_idx), cfg.tiles_per_batch, train=True,
                       q_per_set=cfg.q_per_set, seed=cfg.seed)
    steps_per_epoch = max(1, len(sampler))
    total = steps_per_epoch * cfg.epochs
    warm = max(1, int(cfg.warmup_frac * total))
    decay, no_decay = [], []
    for n, p in model.named_parameters():
        (no_decay if p.ndim <= 1 or n in ("pos", "slot") else decay).append(p)
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": cfg.weight_decay},
                             {"params": no_decay, "weight_decay": 0.0}], lr=cfg.lr,
                            betas=(0.9, 0.99))
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warm) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / total))))
    loader = _loader(corpus, sampler, cfg.workers, override)
    val_items = items_for(corpus, val_idx)
    best = (float("inf"), None, -1)
    history = []
    t_start = time.time()
    bad = 0
    for epoch in range(cfg.epochs):
        model.train()
        t0 = time.time()
        tot, n_q, n_sets, n_tiles = torch.zeros((), device=device), 0, 0, 0
        for step, batch in enumerate(loader):
            tiles, tmask, qvec, mask = _inputs(batch, device, train=True, gen=gen, cfg=cfg,
                                               blind=blind)
            ans = batch["ans"].to(device)
            qvalid = batch["qvalid"].to(device)
            with _autocast(device, cfg.amp):
                logits = model(tiles, tmask, qvec, mask)
            loss = query_loss(logits, ans, qvalid, cfg.label_smoothing)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            opt.step()
            sched.step()
            nq = int(batch["qvalid"].sum())
            tot += loss.detach() * nq
            n_q += nq
            n_sets += tiles.shape[0]
            n_tiles += int(batch["tmask"].sum())
            # In-epoch progress: a full-corpus epoch is long enough that silence is
            # indistinguishable from a stall (the first campaign ran 1.5 h with no line).
            if step and step % 200 == 0:
                el = time.time() - t0
                log(f"[observer] epoch {epoch} step {step}/{steps_per_epoch} "
                    f"loss={float(tot) / max(n_q, 1):.4g} tiles/s={n_tiles / max(el, 1e-9):.0f} "
                    f"eta={el / step * (steps_per_epoch - step) / 60:.1f}min")
        dt = time.time() - t0
        rec = {"epoch": epoch, "train_loss": float(tot) / max(n_q, 1), "seconds": round(dt, 2),
               "sets_per_s": round(n_sets / max(dt, 1e-9), 1),
               "tiles_per_s": round(n_tiles / max(dt, 1e-9), 1)}
        if val_items:
            rows = predict(model, corpus, val_items, device=device, cfg=cfg, blind=blind)
            m = kind_metrics(rows, corpus)["all"]
            rec.update(val_logloss=m["logloss"], val_acc=m["acc"])
            if m["logloss"] < best[0] - 1e-4:
                best = (m["logloss"], {k: v.detach().clone() for k, v in
                                       model.state_dict().items()}, epoch)
                bad = 0
            else:
                bad += 1
        history.append(rec)
        log(f"[observer] epoch {epoch}: " + " ".join(
            f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in rec.items()
            if k != "epoch"))
        if val_items and epoch + 1 >= cfg.min_epochs and bad >= cfg.patience:
            break
    if best[1] is not None:
        model.load_state_dict(best[1])
    info = {"history": history, "best_epoch": best[2] if best[1] is not None else len(history) - 1,
            "train_seconds": round(time.time() - t_start, 2), "params": param_count(model),
            "n_train_sets": len(train_idx), "n_val_sets": len(val_idx)}
    return model, info


# ---------------------------------------------------------------------------- cross-fitting

def _val_slice(parts: list, salt: str, frac: float = 0.1) -> set:
    """Early-stopping parts carved out of a fold's training parts (never its held-out fold)."""
    n = max(1, int(round(frac * len(parts)))) if len(parts) >= 10 else 0
    return set(sorted(parts, key=lambda p: _h(salt, "val", p))[:n])


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def save_checkpoint(path: Path, model: ObserverNet, meta: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    torch.save({**meta, "schema": OBSERVER_SCHEMA, "query_schema": QUERY_SCHEMA, "QD": QD,
                "A_MAX": A_MAX, "model_config": model.cfg.to_dict(),
                "state_dict": {k: v.detach().cpu() for k, v in model.state_dict().items()}}, tmp)
    os.replace(tmp, path)
    return _file_digest(path)


def load_checkpoint(path: Path, device=None) -> tuple:
    """(model in eval mode, checkpoint meta without the weights)."""
    if not Path(path).exists():
        raise FileNotFoundError(f"Observer checkpoint not found: {path}")
    ck = torch.load(path, map_location="cpu", weights_only=False)
    if ck.get("schema") != OBSERVER_SCHEMA or ck.get("query_schema") != QUERY_SCHEMA \
            or ck.get("QD") != QD or ck.get("A_MAX") != A_MAX:
        raise ValueError(f"{path}: checkpoint schema {ck.get('schema')}/{ck.get('query_schema')}"
                         f" does not match {OBSERVER_SCHEMA}/{QUERY_SCHEMA} (QD {QD})")
    model = ObserverNet(ModelConfig.from_dict(ck["model_config"]))
    model.load_state_dict(ck.pop("state_dict"))
    model.to(device or default_device()).eval()
    return model, ck


def folds_meta_path(root, judge: str) -> Path:
    return judge_dir(root, judge) / "folds.json"


def fit_folds(store, *, k: int = 5, judge: str = "crossfit", folds: Optional[Iterable] = None,
              cfg: Optional[TrainConfig] = None, device=None, corpus: Optional[Corpus] = None,
              log: Callable = print) -> dict:
    """Train the `judge` cross-fit's fold models (all K unless `folds` names a subset); write
    observer/<judge>/fold<f>.pt + folds.json. Returns the summary incl. per-kind out-of-fold
    accuracy of every trained fold."""
    spec = JUDGES[judge]
    cfg = cfg or TrainConfig(variant=spec.variant, seed=spec.seed)
    if cfg.variant != spec.variant:
        raise ValueError(f"judge {judge!r} uses variant {spec.variant!r}, got {cfg.variant!r}")
    device = device or default_device()
    corpus = corpus or load_corpus(store, cfg.tile_hw, log=log)
    if not corpus.sets:
        raise ValueError(f"no renderable view sets with queries in {store.root}")
    part_fold = {p: fold_of(p, k, spec.salt) for p in corpus.parts()}
    meta_path = folds_meta_path(store.root, judge)
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta and (meta.get("k") != k or meta.get("salt") != spec.salt or
                 meta.get("query_schema") != QUERY_SCHEMA):
        meta = {}
    meta.update(schema=OBSERVER_SCHEMA, query_schema=QUERY_SCHEMA, judge=judge, k=k,
                salt=spec.salt, variant=spec.variant)
    meta.setdefault("folds", {})
    wanted = sorted(set(range(k)) if folds is None else {int(f) for f in folds})
    t_all = time.time()
    for f in wanted:
        held = {p for p, g in part_fold.items() if g == f}
        train_parts = sorted(p for p, g in part_fold.items() if g != f)
        val_parts = _val_slice(train_parts, f"{spec.salt}/{f}")
        tr = [i for i, s in enumerate(corpus.sets) if s["part_id"] in train_parts
              and s["part_id"] not in val_parts]
        va = [i for i, s in enumerate(corpus.sets) if s["part_id"] in val_parts]
        oof = [i for i, s in enumerate(corpus.sets) if s["part_id"] in held]
        log(f"[observer] {judge} fold {f}/{k}: train {len(tr)} sets "
            f"({len(train_parts) - len(val_parts)} parts), early-stop {len(va)} sets, "
            f"held-out {len(oof)} sets ({len(held)} parts)")
        model, info = train_observer(corpus, tr, va, cfg, device=device, log=log)
        t0 = time.time()
        rows = predict(model, corpus, items_for(corpus, oof), device=device, cfg=cfg)
        oof_m = kind_metrics(rows, corpus) if rows else {}
        info["oof_predict_seconds"] = round(time.time() - t0, 2)
        path = judge_dir(store.root, judge) / f"fold{f}.pt"
        digest = save_checkpoint(path, model, {
            "judge": judge, "fold": f, "k": k, "salt": spec.salt,
            "trained_parts": train_parts, "train_config": cfg.to_dict(), "info": info,
            "oof": oof_m, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        meta["folds"][str(f)] = {"path": path.name, "digest": digest, "oof": oof_m,
                                 "n_train_parts": len(train_parts),
                                 "n_heldout_parts": len(held), "info": info}
        tmp = meta_path.with_name("folds.json.tmp")
        tmp.write_text(json.dumps(meta, indent=1, sort_keys=True))
        os.replace(tmp, meta_path)
        log(f"[observer] {judge} fold {f} out-of-fold: " + ", ".join(
            f"{kk} {v['acc']:.3f} (chance {v['chance']:.3f}, n {v['n']})"
            for kk, v in oof_m.items()))
    meta["seconds"] = round(time.time() - t_all, 2)
    return meta


# ---------------------------------------------------------------------------- report

def _majority_baseline(corpus: Corpus, train_items: list, eval_items: list) -> dict:
    """Accuracy of always answering the training majority of each (mode, kind, subject)
    stratum — what a query-only reader can reach without looking."""
    counts = defaultdict(lambda: defaultdict(int))
    for _i, qids in train_items:
        for qid in qids:
            q = corpus.queries[qid]
            counts[(q.mode, q.kind, q.subject)][q.answer] += 1
    by = defaultdict(list)
    for _i, qids in eval_items:
        for qid in qids:
            q = corpus.queries[qid]
            c = counts.get((q.mode, q.kind, q.subject))
            guess = max(sorted(c), key=lambda a: c[a]) if c else -1
            by[q.kind].append(guess == q.answer)
            by["all"].append(guess == q.answer)
    return {k: float(np.mean(v)) for k, v in sorted(by.items())}


def shuffled_answers(corpus: Corpus, items: list, seed: int) -> dict:
    """qid -> an answer permuted among the training queries of the same (mode, kind,
    subject) stratum: the label marginals survive, the pixel-answer link does not."""
    groups = defaultdict(list)
    for qid in sorted({q for _i, qids in items for q in qids}):
        q = corpus.queries[qid]
        groups[(q.mode, q.kind, q.subject)].append(qid)
    rng = random.Random(_h(seed, "shuffle"))
    out = {}
    for key in sorted(groups):
        qids = groups[key]
        answers = [corpus.queries[q].answer for q in qids]
        rng.shuffle(answers)
        out.update(zip(qids, answers))
    return out


def learning_report(store, *, fractions=(0.125, 0.25, 0.5, 1.0), seeds=(0, 1, 2),
                    k: int = 5, fold: int = 0, cfg: Optional[TrainConfig] = None,
                    controls: bool = True, device=None, corpus: Optional[Corpus] = None,
                    log: Callable = print) -> dict:
    """Held-out (fold `fold` of the crossfit assignment) per-kind accuracy vs train-part
    fraction, seed spread at the full fraction, shuffled-label and blind (no pixels)
    controls, chance, majority and a held-out-part query-vector-only leak probe."""
    spec = JUDGES["crossfit"]
    cfg = cfg or TrainConfig(variant=spec.variant, seed=spec.seed)
    device = device or default_device()
    corpus = corpus or load_corpus(store, cfg.tile_hw, log=log)
    parts = corpus.parts()
    held = {p for p in parts if fold_of(p, k, spec.salt) == fold}
    pool = [p for p in parts if p not in held]
    val_parts = _val_slice(pool, f"{spec.salt}/report")
    order = sorted((p for p in pool if p not in val_parts), key=lambda p: _h("order", p))
    va = [i for i, s in enumerate(corpus.sets) if s["part_id"] in val_parts]
    ev = [i for i, s in enumerate(corpus.sets) if s["part_id"] in held]
    ev_items = items_for(corpus, ev)

    def run(frac: float, seed: int, **kw) -> dict:
        n = max(1, int(round(frac * len(order))))
        chosen = set(order[:n])
        tr = [i for i, s in enumerate(corpus.sets) if s["part_id"] in chosen]
        c = TrainConfig(**{**asdict(cfg), "seed": seed})
        tag = " shuffled-label" if kw.get("override") else " blind" if kw.get("blind") else ""
        log(f"[observer] report run frac={frac} seed={seed}{tag}: {n} parts, {len(tr)} sets")
        model, info = train_observer(corpus, tr, va, c, device=device, log=log, **kw)
        rows = predict(model, corpus, ev_items, device=device, cfg=c, blind=kw.get("blind", False))
        return {"fraction": frac, "seed": seed, "train_parts": n, "train_sets": len(tr),
                "metrics": kind_metrics(rows, corpus), "best_epoch": info["best_epoch"],
                "train_seconds": info["train_seconds"],
                "tiles_per_s": float(np.mean([h["tiles_per_s"] for h in info["history"]]))}

    t0 = time.time()
    curve = [run(f, seeds[0]) for f in fractions]
    spread = [r for r in curve if r["fraction"] == 1.0] + [run(1.0, s) for s in seeds[1:]]
    out = {"schema": OBSERVER_SCHEMA + "/report", "k": k, "fold": fold,
           "eval_parts": len(held), "eval_sets": len(ev), "early_stop_parts": len(val_parts),
           "pool_parts": len(order), "train_config": cfg.to_dict(),
           "chance": {kk: v["chance"] for kk, v in
                      kind_metrics([{"qid": q, "kind": corpus.queries[q].kind, "p": 1.0,
                                     "correct": False} for _i, qs in ev_items for q in qs],
                                   corpus).items()},
           "majority": _majority_baseline(corpus, items_for(
               corpus, [i for i, s in enumerate(corpus.sets) if s["part_id"] in set(order)]),
               ev_items),
           "learning_curve": curve, "seeds": spread}
    from .queries import qvec_probe  # noqa: PLC0415
    out["qvec_probe"] = qvec_probe(corpus.queries.values(),
                                    lambda p: fold_of(p, k, spec.salt) == fold)
    kinds = sorted(curve[-1]["metrics"])
    out["seed_spread"] = {kk: {"mean": float(np.mean([r["metrics"][kk]["acc"] for r in spread])),
                               "std": float(np.std([r["metrics"][kk]["acc"] for r in spread]))}
                          for kk in kinds}
    if controls:
        full_tr = [i for i, s in enumerate(corpus.sets) if s["part_id"] in set(order)]
        perm = shuffled_answers(corpus, items_for(corpus, full_tr), seeds[0])
        out["shuffled_label"] = run(1.0, seeds[0], override=perm)
        out["blind"] = run(1.0, seeds[0], blind=True)
    out["seconds"] = round(time.time() - t0, 2)
    path = observer_dir(store.root) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, sort_keys=True))
    out["path"] = str(path)
    return out


def format_report(rep: dict) -> str:
    kinds = sorted(rep["learning_curve"][-1]["metrics"])
    lines = [f"Observer held-out accuracy (fold {rep['fold']}: {rep['eval_parts']} parts, "
             f"{rep['eval_sets']} sets)", "run".ljust(24) + "".join(k[:13].rjust(14) for k in kinds)]

    def row(name, vals):
        lines.append(name.ljust(24) + "".join(
            (f"{vals[k]:.3f}" if k in vals else "-").rjust(14) for k in kinds))

    row("chance (uniform)", rep["chance"])
    row("majority (set pairs)", rep["majority"])
    row("majority (unique qids)", {k: v["majority"] for k, v in rep["qvec_probe"].items()})
    row("qvec_probe", {k: v["acc"] for k, v in rep["qvec_probe"].items()})
    for r in rep["learning_curve"]:
        row(f"frac {r['fraction']:.3g} ({r['train_parts']}p)",
            {k: v["acc"] for k, v in r["metrics"].items()})
    for r in rep["seeds"]:
        row(f"seed {r['seed']} (full)", {k: v["acc"] for k, v in r["metrics"].items()})
    row("seed mean", {k: v["mean"] for k, v in rep["seed_spread"].items()})
    row("seed std", {k: v["std"] for k, v in rep["seed_spread"].items()})
    for name in ("shuffled_label", "blind"):
        if name in rep:
            row(name, {k: v["acc"] for k, v in rep[name]["metrics"].items()})
    return "\n".join(lines)


__all__ = ["Corpus", "JUDGES", "JudgeSpec", "OBSERVER_SCHEMA", "TrainConfig", "build_corpus",
           "fit_folds", "fold_of", "format_report", "kind_metrics", "learning_report",
           "load_checkpoint", "load_corpus", "predict", "prepare_tiles", "split_grid",
           "tile_array", "train_observer"]
