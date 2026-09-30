"""Trial store: view sets, their tokens and images, the observer's queries and its labels.

Layout under a store root::

    sets.jsonl            one record per view set of the training namespace ("trials")
    sets_<name>.jsonl     the same for another namespace ("ab" = held-out A/B sets)
    parts_<name>.jsonl    parts whose view sets are complete (resume marker)
    labels_<judge>.jsonl  observer answers per (set_key, qid): p_correct, correct, fold
    tokens/<set_key>.npz  services.viewpoint.utility.features.build_set_tokens output
    images/<sha>.png      content-addressed shot grids exactly as the agent would see them
    (queries)             training.viewpoint.observer.queries.queries_path(root)

Rows are appended under a lock and deduplicated on load (last row wins), so an interrupted
run resumes by simply running again.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from training.viewpoint.durable import append_jsonl, durable_replace, nonempty, read_jsonl

JUDGES = ("crossfit", "independent")


def viewnet_root() -> Path:
    return Path(os.environ.get("CHUDWARE_VIEWNET_DATA", "~/.cache/chudware/viewnet")).expanduser()


def default_store_root() -> Path:
    return viewnet_root() / "utility" / "store"


def default_run_dir() -> Path:
    return viewnet_root() / "utility" / "run"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TrialStore:
    def __init__(self, root, name: str = "trials"):
        self.root = Path(root).expanduser()
        self.name = name
        self._lock = threading.Lock()
        for sub in ("tokens", "images"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    @property
    def sets_path(self) -> Path:
        return self.root / ("sets.jsonl" if self.name == "trials" else f"sets_{self.name}.jsonl")

    @property
    def parts_path(self) -> Path:
        return self.root / f"parts_{self.name}.jsonl"

    def labels_path(self, judge: str) -> Path:
        if judge not in JUDGES:
            raise ValueError(f"unknown judge {judge!r}; expected one of {JUDGES}")
        return self.root / f"labels_{judge}.jsonl"

    def _append(self, path: Path, rows: Iterable[dict]) -> None:
        with self._lock:
            append_jsonl(path, rows)

    # ---- images / tokens ----
    def save_image(self, png_path) -> tuple:
        """Copy a rendered grid into the content-addressed image dir -> (sha, stored path)."""
        sha = sha256_file(png_path)
        dest = self.root / "images" / f"{sha}.png"
        if not nonempty(dest):
            tmp = dest.with_suffix(".tmp")
            shutil.copyfile(png_path, tmp)
            durable_replace(tmp, dest)
        return sha, dest

    def save_tokens(self, set_key: str, tokens: dict) -> str:
        """Write a set's build_set_tokens arrays; returns the path relative to the root."""
        path = self.root / "tokens" / f"{set_key}.npz"
        if not nonempty(path):
            tmp = path.with_name(path.stem + ".tmp.npz")
            np.savez_compressed(tmp, **{k: np.asarray(v) for k, v in tokens.items()})
            durable_replace(tmp, path)
        return str(path.relative_to(self.root))

    def load_tokens(self, rel_path: str) -> dict:
        with np.load(self.root / rel_path) as z:
            return {k: z[k] for k in z.files}

    # ---- sets ----
    def append_sets(self, records: Iterable[dict]) -> None:
        self._append(self.sets_path, records)

    def load_sets(self) -> dict:
        return {r["set_key"]: r for r in read_jsonl(self.sets_path)}

    def mark_part_done(self, part_id: str, config_sha: str) -> None:
        self._append(self.parts_path, [{"part_id": part_id, "config": config_sha}])

    def done_parts(self, config_sha: str) -> set:
        return {r["part_id"] for r in read_jsonl(self.parts_path) if r.get("config") == config_sha}

    # ---- queries (owned by the observer package; the store only locates them) ----
    @property
    def queries_path(self) -> Path:
        from training.viewpoint.observer.queries import queries_path  # noqa: PLC0415
        return Path(queries_path(self.root))

    def save_queries(self, queries: Iterable) -> None:
        from training.viewpoint.observer.queries import save_queries  # noqa: PLC0415
        path = self.queries_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            save_queries(path, list(queries))

    def load_queries(self) -> dict:
        """{qid: Query}"""
        from training.viewpoint.observer.queries import load_queries  # noqa: PLC0415
        return load_queries(self.queries_path)

    # ---- labels ----
    def append_labels(self, judge: str, rows: Iterable[dict]) -> None:
        self._append(self.labels_path(judge), rows)

    def load_labels(self, judge: str) -> dict:
        """{(set_key, qid): row}; a later row for the same pair replaces an earlier one."""
        return {(r["set_key"], r["qid"]): r for r in read_jsonl(self.labels_path(judge))}


def pair_id(set_key: str, qid: str) -> str:
    return f"{set_key}:{qid}"


@dataclass
class TrialDataset:
    """Training view of a store: unique set token arrays + one entry per labelled
    (view set, query) pair.

    ``face_tokens[s]``/``face_mask[s]``/``ctx[s]`` belong to set s; pair i uses set
    ``set_index[i]``. ``ctx`` = concat(set_global, context) (the model's x_c input). ``y`` is
    the cross-fitted observer's P(correct) — the soft BCE target; ``correct`` is whether its
    argmax answer was right (used for AUC)."""
    set_keys: list
    face_tokens: list
    face_mask: list
    ctx: list
    set_index: np.ndarray      # int64 [N]
    kind: np.ndarray           # int64 [N] index into features.QUESTION_KINDS
    y: np.ndarray              # float32 [N] observer p_correct
    correct: np.ndarray        # float32 [N] 1 = observer argmax correct
    hand: np.ndarray           # float64 [N] hand_utility of the pair's set
    chance: np.ndarray         # float64 [N] 1 / n_options of the query
    part_ids: np.ndarray       # object [N]
    splits: np.ndarray         # object [N]
    strategies: np.ndarray     # object [N] primary strategy of the set
    ks: np.ndarray             # int64 [N]
    modes: np.ndarray          # object [N]
    folds: np.ndarray          # int64 [N] observer fold that labelled the pair
    rows: list                 # per pair: {"pair_id", "set_key", "qid", "strategies", "k", ...}

    def __len__(self) -> int:
        return int(self.y.shape[0])

    def token_dict(self, s: int, dg: int) -> dict:
        """The build_set_tokens dict for set s (what UtilityModel.score consumes)."""
        return {"face_tokens": self.face_tokens[s], "face_mask": self.face_mask[s],
                "set_global": self.ctx[s][:dg], "context": self.ctx[s][dg:]}


def load_dataset(store: TrialStore, *, judge: str = "crossfit",
                 splits: Optional[Iterable[str]] = None) -> TrialDataset:
    """Labelled pairs of the store's namespace joined to their sets and queries."""
    from services.viewpoint.utility.features import QUESTION_KINDS  # noqa: PLC0415

    want = set(splits) if splits is not None else None
    sets = store.load_sets()
    queries = store.load_queries()
    labels = store.load_labels(judge)
    kind_index = {k: i for i, k in enumerate(QUESTION_KINDS)}
    pairs = sorted(key for key in labels
                   if key[0] in sets and key[1] in queries
                   and (want is None or sets[key[0]]["split"] in want))
    set_pos: dict = {}
    keys, ft, fm, cx = [], [], [], []
    for sk, _qid in pairs:
        if sk not in set_pos:
            tok = store.load_tokens(sets[sk]["tokens_path"])
            set_pos[sk] = len(keys)
            keys.append(sk)
            ft.append(np.asarray(tok["face_tokens"], np.float32))
            fm.append(np.asarray(tok["face_mask"], bool))
            cx.append(np.concatenate([np.asarray(tok["set_global"], np.float32),
                                      np.asarray(tok["context"], np.float32)]))
    rows = [{"pair_id": pair_id(sk, q), "set_key": sk, "qid": q,
             "strategies": list(sets[sk]["strategies"]), "k": int(sets[sk]["k"]),
             "observer_digest": labels[(sk, q)].get("observer_digest")}
            for sk, q in pairs]
    return TrialDataset(
        set_keys=keys, face_tokens=ft, face_mask=fm, ctx=cx,
        set_index=np.asarray([set_pos[sk] for sk, _q in pairs], np.int64),
        kind=np.asarray([kind_index[queries[q].kind] for _s, q in pairs], np.int64),
        y=np.asarray([float(labels[p]["p_correct"]) for p in pairs], np.float32),
        correct=np.asarray([1.0 if labels[p]["correct"] else 0.0 for p in pairs], np.float32),
        hand=np.asarray([float(sets[sk]["hand_utility"]) for sk, _q in pairs], np.float64),
        chance=np.asarray([float(queries[q].chance) for _s, q in pairs], np.float64),
        part_ids=np.asarray([str(sets[sk]["part_id"]) for sk, _q in pairs], object),
        splits=np.asarray([sets[sk]["split"] for sk, _q in pairs], object),
        strategies=np.asarray([sets[sk]["strategies"][0] for sk, _q in pairs], object),
        ks=np.asarray([int(sets[sk]["k"]) for sk, _q in pairs], np.int64),
        modes=np.asarray([sets[sk]["mode"] for sk, _q in pairs], object),
        folds=np.asarray([int(labels[p].get("fold", -1)) for p in pairs], np.int64),
        rows=rows)
