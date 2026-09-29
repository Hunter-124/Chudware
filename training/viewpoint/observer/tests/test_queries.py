"""Queries: answer balance, determinism, option masks, and no answer leaking into the vector."""
from __future__ import annotations

import random
from collections import Counter, defaultdict

import numpy as np

from training.viewpoint.datasets.importers import CANON
from training.viewpoint.observer import queries as Q

_CLASSES = ("hole_through", "pocket", "slot_through", "slot_blind", "step_through",
            "step_blind", "boss", "passage")


def _records(n: int, seed: int = 0) -> list:
    rng = random.Random(seed)
    recs = []
    for p in range(n):
        feats = []
        for i in range(rng.randint(1, 6)):
            cls = rng.choice(_CLASSES)
            through = (True if cls in ("hole_through", "slot_through", "step_through",
                                       "passage")
                       else False if cls in ("pocket", "slot_blind", "step_blind") else None)
            feats.append({"instance": i, "class": cls, "faces": [3 * i, 3 * i + 1],
                          "through": through,
                          "opening_side": rng.choice(Q.SIDES) if cls != "hole_through" else None,
                          "centroid": [rng.uniform(0, 100), rng.uniform(0, 60),
                                       rng.uniform(0, 20)],
                          "axis": rng.choice([[0, 0, 1], [1, 0, 0], [0, -1, 0]])})
        recs.append({"schema": "chudware.geomlabel/1", "part_id": f"synth/{p}", "split": "train",
                     "features": feats,
                     "stats": {"n_faces": 40, "bbox": {"min": [0, 0, 0], "max": [100, 60, 20]}}})
    return recs


def test_bank_is_deterministic_and_seed_changes_only_the_selection():
    recs = _records(300)
    a = Q.build_query_bank(recs, seed=0)
    b = Q.build_query_bank(list(reversed(recs)), seed=0)
    assert [(q.qid, q.answer) for q in a] == [(q.qid, q.answer) for q in b]
    c = Q.build_query_bank(recs, seed=1)
    pool = {q.qid for r in recs for q in Q.build_queries(r)}
    assert {q.qid for q in c} <= pool
    assert {q.qid for q in c} != {q.qid for q in a}
    # A part's queries never depend on the other records in the corpus.
    assert [q.qid for q in Q.build_queries(recs[7])] == \
        [q.qid for q in Q.build_queries(dict(recs[7]))]


def test_answers_are_balanced_within_every_stratum():
    recs = _records(400)
    raw, kept = defaultdict(Counter), defaultdict(Counter)
    for r in recs:
        for q in Q.build_queries(r):
            raw[(q.mode, q.kind, q.subject)][q.answer] += 1
    for q in Q.build_query_bank(recs, seed=0):
        kept[(q.mode, q.kind, q.subject)][q.answer] += 1
    assert {k for _m, k, _s in kept} == set(Q.KINDS)
    for key, counts in raw.items():
        support = max(3, int(np.ceil(0.02 * sum(counts.values()))))
        supported = [c for c in counts.values() if c >= support]
        if len(supported) < 2:
            assert key not in kept      # one answer: the query vector alone would give it
            continue
        cap = int(Q.BALANCE_RATIO * min(supported))
        # Every value keeps min(its count, cap): rare values whole, common ones capped.
        assert kept[key] == Counter({v: min(c, cap) for v, c in counts.items()}), key


def test_single_answer_strata_are_dropped():
    # Every boss count is 0 or 1 but every hole count is 0: "how many holes" answers itself.
    recs = _records(200)
    for r in recs:
        r["features"] = [f for f in r["features"] if f["class"] != "hole_through"]
    bank = Q.build_query_bank(recs, seed=0)
    assert not [q for q in bank if q.kind == "count" and q.subject == "hole"]


def test_option_masks_and_chance():
    for q in Q.build_query_bank(_records(150), seed=0):
        assert q.vec.shape == (Q.QD,) and q.vec.dtype == np.float32
        assert q.option_mask.shape == (Q.A_MAX,)
        assert q.option_mask[q.answer]
        assert q.n_options == int(q.option_mask.sum())
        assert abs(q.chance - 1.0 / q.n_options) < 1e-12
        if q.kind == "type":
            assert not q.option_mask[CANON.index("stock")]
            assert not q.option_mask[CANON.index("other")]


def test_query_vector_does_not_give_the_answer_away():
    cls = slice(Q._C0, Q._C0 + len(CANON))
    axis = slice(Q._A0, Q._A0 + 3)
    for q in Q.build_query_bank(_records(300), seed=0):
        if q.kind == "presence_side":
            assert not q.vec[axis].any()
        if q.kind == "type":
            assert not q.vec[cls].any()
        if q.kind == "through_blind":
            named = {CANON[i] for i in np.flatnonzero(q.vec[cls])}
            assert named == set(Q.FAMILY_CLASSES[q.subject])
            assert len(named) == 2      # both the through and the blind variant


def test_target_mode_carries_the_painted_faces():
    for q in Q.build_query_bank(_records(100), seed=0):
        if q.mode == "target":
            assert q.target_instance >= 0 and q.target_faces
        else:
            assert q.target_instance == -1 and not q.target_faces


def test_save_load_roundtrip_is_an_upsert(tmp_path):
    bank = Q.build_query_bank(_records(80), seed=0)
    path = Q.queries_path(tmp_path)
    assert Q.load_queries(path) == {}
    assert Q.save_queries(path, bank[:50]) == 50
    assert Q.save_queries(path, bank) == len(bank) - 50
    got = Q.load_queries(path)
    assert set(got) == {q.qid for q in bank}
    q0 = bank[3]
    assert got[q0.qid] == q0
    assert np.array_equal(got[q0.qid].vec, q0.vec)
    assert np.array_equal(got[q0.qid].option_mask, q0.option_mask)


def test_pick_targets_is_seeded_and_bounded():
    qs = Q.build_queries(_records(1, seed=5)[0])
    insts = {q.target_instance for q in qs if q.mode == "target"}
    a = Q.pick_targets(qs, 2, seed=0)
    assert a == Q.pick_targets(list(reversed(qs)), 2, seed=0)
    assert len(a) == min(2, len(insts)) and set(a) <= insts
