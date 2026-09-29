"""Structured geometry queries derived from `chudware.geomlabel/1` records (no text).

A query is what the Observer is asked about a rendered view set: a kind (count /
presence_side / through_blind / type), the subject (a multi-hot over CANON classes), where the
subject sits (bbox-normalised centroid + opening-axis one-hot) and, in target mode, the target
is ALSO painted by the render's emphasis overlay. The answer is an index into the kind's answer
space; ``option_mask`` marks the valid options inside the shared ``A_MAX`` logit vector.

Nothing in ``vec`` may give the answer away, otherwise the Observer answers from the query and
the pixels stop mattering:
- presence_side zeroes the axis one-hot (knowing the axis halves the six sides to two);
- through_blind names the subject by FAMILY (hole_through and hole_blind both set) because the
  CANON class alone would say which;
- type leaves the subject empty (the class IS the answer).
Answers are then balanced within each (mode, kind, subject) stratum, and a stratum with a
single answer value is dropped: its answer would be a constant of the query vector.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from training.viewpoint.datasets.importers import CANON

QUERY_SCHEMA = "viewnet.query/1"
KINDS = ("count", "presence_side", "through_blind", "type")
MODES = ("part", "target")
SIDES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")
COUNT_BINS = 6                       # 0, 1, 2, 3, 4, 5+
THROUGH_BLIND = ("through", "blind")
# stock / other describe no feature, so they are never a type answer.
TYPE_CLASSES = tuple(c for c in CANON if c not in ("stock", "other"))
FAMILY = {
    "hole_through": "hole", "hole_blind": "hole", "passage": "passage", "pocket": "pocket",
    "slot_through": "slot", "slot_blind": "slot", "step_through": "step",
    "step_blind": "step", "o_ring": "o_ring", "boss": "boss", "rib": "rib",
    "fillet": "fillet", "chamfer": "chamfer", "extrude_side": "extrude",
    "extrude_end": "extrude", "cut_side": "cut", "cut_end": "cut", "revolve_side": "revolve",
    "revolve_end": "revolve",
}
FAMILY_CLASSES: dict = defaultdict(tuple)
for _c, _f in FAMILY.items():
    FAMILY_CLASSES[_f] = FAMILY_CLASSES[_f] + (_c,)
FAMILY_CLASSES = dict(FAMILY_CLASSES)
# What "how many X" counts: each family and each through/blind variant of it. extrude/cut/
# revolve describe how faces were made, not features a reader counts.
COUNT_GROUPS = tuple(
    [(f, FAMILY_CLASSES[f]) for f in ("hole", "passage", "pocket", "slot", "step", "o_ring",
                                      "boss", "rib", "fillet", "chamfer")]
    + [(c, (c,)) for c in ("hole_through", "hole_blind", "slot_through", "slot_blind",
                           "step_through", "step_blind")])

# Query vector layout.
_K0 = 0
_M0 = _K0 + len(KINDS)
_C0 = _M0 + len(MODES)
_L0 = _C0 + len(CANON)
_H0 = _L0 + 3
_A0 = _H0 + 1
QD = _A0 + 3
A_MAX = max(COUNT_BINS, len(SIDES), len(THROUGH_BLIND), len(CANON))

# A located part-mode query names its subject by position only, so no other candidate may sit
# within this bbox-normalised distance of it.
LOCATE_RADIUS = 0.2
BALANCE_RATIO = 1.5                  # cap per answer value = ratio x rarest supported value


def _mask(kind: str) -> np.ndarray:
    m = np.zeros(A_MAX, bool)
    if kind == "count":
        m[:COUNT_BINS] = True
    elif kind == "presence_side":
        m[:len(SIDES)] = True
    elif kind == "through_blind":
        m[:len(THROUGH_BLIND)] = True
    elif kind == "type":
        for c in TYPE_CLASSES:
            m[CANON.index(c)] = True
    else:
        raise ValueError(f"unknown query kind {kind!r}")
    return m


OPTION_MASKS = {k: _mask(k) for k in KINDS}


@dataclass(frozen=True)
class Query:
    qid: str
    part_id: str
    kind: str
    mode: str                           # "part" | "target"
    target_instance: int                # -1 in part mode
    vec: np.ndarray = field(compare=False, repr=False)   # float32[QD]
    answer: int
    n_options: int
    option_mask: np.ndarray = field(compare=False, repr=False)  # bool[A_MAX]
    chance: float
    answer_key: str                     # human-readable answer value (balancing, reports)
    subject: str                        # balancing stratum: counted group / family / ""
    target_faces: tuple = ()            # face ordinals the emphasis overlay paints (target mode)
    target_class: Optional[str] = None

    def to_dict(self) -> dict:
        return {"schema": QUERY_SCHEMA, "qid": self.qid, "part_id": self.part_id,
                "kind": self.kind, "mode": self.mode, "target_instance": self.target_instance,
                "vec": [float(x) for x in self.vec], "answer": self.answer,
                "n_options": self.n_options, "option_mask": [bool(x) for x in self.option_mask],
                "chance": self.chance, "answer_key": self.answer_key, "subject": self.subject,
                "target_faces": list(self.target_faces), "target_class": self.target_class}

    @classmethod
    def from_dict(cls, d: dict) -> "Query":
        if d.get("schema") != QUERY_SCHEMA:
            raise ValueError(f"query {d.get('qid')!r} has schema {d.get('schema')!r}, "
                             f"expected {QUERY_SCHEMA}")
        return cls(qid=d["qid"], part_id=d["part_id"], kind=d["kind"], mode=d["mode"],
                   target_instance=int(d["target_instance"]),
                   vec=np.asarray(d["vec"], np.float32), answer=int(d["answer"]),
                   n_options=int(d["n_options"]),
                   option_mask=np.asarray(d["option_mask"], bool), chance=float(d["chance"]),
                   answer_key=d["answer_key"], subject=d["subject"],
                   target_faces=tuple(int(o) for o in d.get("target_faces") or ()),
                   target_class=d.get("target_class"))


def _h(*parts) -> str:
    return hashlib.sha1(json.dumps([str(p) for p in parts]).encode()).hexdigest()


def _bbox(record: dict) -> tuple:
    bb = (record.get("stats") or {}).get("bbox") or {}
    return [float(v) for v in bb["min"][:3]], [float(v) for v in bb["max"][:3]]


def _norm_loc(centroid, lo, hi) -> Optional[np.ndarray]:
    if centroid is None:
        return None
    out = np.zeros(3, np.float32)
    for k in range(3):
        ext = hi[k] - lo[k]
        t = 0.5 if ext <= 1e-9 else (float(centroid[k]) - lo[k]) / ext
        out[k] = min(1.0, max(0.0, t)) - 0.5
    return out


def _axis_onehot(axis) -> np.ndarray:
    out = np.zeros(3, np.float32)
    if axis is not None and len(axis) >= 3:
        a = np.abs(np.asarray(axis[:3], np.float64))
        if a.max() > 1e-9:
            out[int(np.argmax(a))] = 1.0
    return out


def _vec(kind: str, mode: str, classes: Iterable[str], loc: Optional[np.ndarray],
         axis: Optional[np.ndarray]) -> np.ndarray:
    v = np.zeros(QD, np.float32)
    v[_K0 + KINDS.index(kind)] = 1.0
    v[_M0 + MODES.index(mode)] = 1.0
    for c in classes:
        v[_C0 + CANON.index(c)] = 1.0
    if loc is not None:
        v[_L0:_L0 + 3] = loc
        v[_H0] = 1.0
    if axis is not None:
        v[_A0:_A0 + 3] = axis
    return v


def _make(part_id: str, kind: str, mode: str, target_instance: int, vec: np.ndarray,
          answer: int, answer_key: str, subject: str, target_faces: tuple = (),
          target_class: Optional[str] = None) -> Query:
    mask = OPTION_MASKS[kind].copy()
    if not mask[answer]:
        raise ValueError(f"{part_id}: {kind} answer {answer} is outside the option mask")
    n = int(mask.sum())
    qid = _h(QUERY_SCHEMA, part_id, kind, mode, target_instance, subject,
             [round(float(x), 5) for x in vec], answer)[:16]
    return Query(qid=qid, part_id=part_id, kind=kind, mode=mode,
                 target_instance=int(target_instance), vec=vec, answer=int(answer),
                 n_options=n, option_mask=mask, chance=1.0 / n, answer_key=answer_key,
                 subject=subject, target_faces=tuple(target_faces), target_class=target_class)


def _isolated(f: dict, pool: list, locs: dict) -> bool:
    """No other pool member lies within LOCATE_RADIUS of f (so a location names it)."""
    me = locs.get(f["instance"])
    if me is None:
        return False
    for g in pool:
        if g["instance"] == f["instance"]:
            continue
        other = locs.get(g["instance"])
        if other is not None and float(np.linalg.norm(other - me)) < LOCATE_RADIUS:
            return False
    return True


def build_queries(record: dict, *, seed: int = 0) -> list:
    """Every query one part's labels support (unbalanced), sorted by qid.

    `seed` is accepted for interface symmetry: per-part queries are a pure function of the
    labels; the seed only selects which ones balancing keeps (build_query_bank)."""
    del seed
    part_id = str(record["part_id"])
    lo, hi = _bbox(record)
    feats = [f for f in (record.get("features") or [])
             if f.get("class") in FAMILY and f.get("faces")]
    locs = {f["instance"]: _norm_loc(f.get("centroid"), lo, hi) for f in feats}
    locs = {k: v for k, v in locs.items() if v is not None}
    out: list = []

    # count: whole part, one query per counted group.
    for group, classes in COUNT_GROUPS:
        n = sum(1 for f in feats if f["class"] in classes)
        b = min(n, COUNT_BINS - 1)
        out.append(_make(part_id, "count", "part", -1,
                         _vec("count", "part", classes, None, None), b,
                         str(b) if b < COUNT_BINS - 1 else f"{COUNT_BINS - 1}+", group))

    by_family = defaultdict(list)
    for f in feats:
        by_family[FAMILY[f["class"]]].append(f)

    for f in feats:
        cls, inst = f["class"], int(f["instance"])
        fam = FAMILY[cls]
        fam_classes = FAMILY_CLASSES[fam]
        side = f.get("opening_side")
        through = f.get("through")
        loc = locs.get(inst)
        axis = _axis_onehot(f.get("axis"))
        faces = tuple(int(o) for o in f["faces"])
        fam_ok = _isolated(f, by_family[fam], locs)
        any_ok = _isolated(f, feats, locs)
        for mode in MODES:
            located = mode == "target" or fam_ok
            tkw = ({"target_faces": faces, "target_class": cls} if mode == "target" else {})
            ti = inst if mode == "target" else -1
            if side in SIDES and located:
                out.append(_make(part_id, "presence_side", mode, ti,
                                 _vec("presence_side", mode, fam_classes, loc, None),
                                 SIDES.index(side), side, fam, **tkw))
            if through is not None and located:
                a = 0 if through else 1
                out.append(_make(part_id, "through_blind", mode, ti,
                                 _vec("through_blind", mode, fam_classes, loc, axis),
                                 a, THROUGH_BLIND[a], fam, **tkw))
            if mode == "target" or any_ok:
                out.append(_make(part_id, "type", mode, ti, _vec("type", mode, (), loc, None),
                                 CANON.index(cls), cls, "", **tkw))
    uniq = {q.qid: q for q in out}
    return [uniq[k] for k in sorted(uniq)]


def balance(queries: list, *, seed: int = 0, ratio: float = BALANCE_RATIO) -> list:
    """Cap each answer value at `ratio` x the rarest well-supported value of its (mode, kind,
    subject) stratum; drop strata with fewer than two supported values. A value is supported
    when it holds >= max(3, 2 %) of its stratum. Which members survive is a seeded hash order,
    so the choice is deterministic and a query's survival never depends on list order."""
    strata = defaultdict(list)
    for q in queries:
        strata[(q.mode, q.kind, q.subject)].append(q)
    kept: list = []
    for key in sorted(strata):
        group = strata[key]
        by_value = defaultdict(list)
        for q in group:
            by_value[q.answer].append(q)
        support = max(3, int(math.ceil(0.02 * len(group))))
        supported = sorted(len(v) for v in by_value.values() if len(v) >= support)
        if len(supported) < 2:
            continue
        cap = max(1, int(ratio * supported[0]))
        for value in sorted(by_value):
            members = sorted(by_value[value], key=lambda q: _h(seed, "bal", q.qid))
            kept.extend(members[:cap])
    kept.sort(key=lambda q: (q.part_id, q.qid))
    return kept


def build_query_bank(records: Iterable, *, seed: int = 0, balance_answers: bool = True) -> list:
    """Deterministic queries for every record, answer-balanced across the corpus."""
    qs: list = []
    for rec in records:
        qs.extend(build_queries(rec, seed=seed))
    return balance(qs, seed=seed) if balance_answers else qs


def pick_targets(queries: Iterable, n: int, *, seed: int = 0) -> list:
    """Up to n target instances of ONE part's queries, in a seeded hash order (so every
    feature class gets its turn across parts rather than the most-queried ones)."""
    insts = sorted({(q.part_id, q.target_instance) for q in queries if q.mode == "target"},
                   key=lambda pi: _h(seed, "target", pi[0], pi[1]))
    return [int(i) for _p, i in insts[:max(0, int(n))]]


def queries_for(bank: Iterable, part_id: str, mode: str, target_instance: int) -> list:
    """The queries a view set of (part, mode, target) can be asked."""
    ti = int(target_instance) if mode == "target" else -1
    return [q for q in bank if q.part_id == part_id and q.mode == mode
            and q.target_instance == ti]


def queries_path(store_root) -> Path:
    return Path(store_root).expanduser() / "observer" / "queries.jsonl"


def load_queries(path) -> dict:
    """{qid: Query}; {} when the file does not exist yet."""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict = {}
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                q = Query.from_dict(json.loads(line))
                out[q.qid] = q
    return out


def save_queries(path, queries: Iterable) -> int:
    """Upsert by qid (qids are content hashes, so an existing qid is the same query): append
    only queries the file does not hold yet. Returns how many were appended."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    have = set(load_queries(p))
    new = [q for q in queries if q.qid not in have]
    if new:
        with open(p, "a", encoding="utf-8") as fh:
            for q in new:
                fh.write(json.dumps(q.to_dict(), sort_keys=True) + "\n")
    return len(new)


def chance_by_kind(queries: Iterable) -> dict:
    """{kind: mean 1/n_options} — the uniform-guess accuracy per kind."""
    acc = defaultdict(list)
    for q in queries:
        acc[q.kind].append(q.chance)
    return {k: float(np.mean(v)) for k, v in sorted(acc.items())}
