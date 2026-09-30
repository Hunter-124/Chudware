"""Queries: answer balance, determinism, option masks, and no answer leaking into the vector."""
from __future__ import annotations

import json
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


def test_query_vector_names_only_the_subject_and_never_locates_it():
    cls = slice(Q._C0, Q._C0 + len(CANON))
    for q in Q.build_query_bank(_records(300), seed=0):
        if q.mode == "part":
            assert q.kind == "count" and q.target_instance == -1
        else:
            assert q.target_instance >= 0
            if q.kind == "type":
                assert not q.vec[cls].any()
            else:
                named = {CANON[i] for i in np.flatnonzero(q.vec[cls])}
                assert named == set(Q.FAMILY_CLASSES[q.subject])
        assert q.vec.shape == (len(Q.KINDS) + len(Q.MODES) + len(CANON),)


def test_qvec_probe_catches_a_hidden_answer_field():
    from dataclasses import replace

    bank = Q.build_query_bank(_records(400, seed=7))
    split = lambda p: int(p.split("/")[-1]) % 5 == 0
    clean = Q.qvec_probe(bank, split)
    for kind, row in clean.items():
        assert row["acc"] <= row["majority"] + 0.03, (kind, row)
    # A field omitted from the balancing key may appear harmless in marginal counts,
    # but a trained query-only classifier must discover that it encodes the answer.
    leaky = [replace(q, vec=np.r_[q.vec, float(q.answer == 0)].astype(np.float32))
             for q in bank]
    probed = Q.qvec_probe(leaky, split)
    assert probed["through_blind"]["acc"] > probed["through_blind"]["majority"] + 0.2

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


def test_requery_remaps_all_namespaces_and_archives_stale_labels(tmp_path):
    from training.viewpoint.observer.requery import requery_store

    records = _records(100)
    sample = Q.build_query_bank(records)
    target = next(q for q in sample if q.mode == "target")
    part = next(q for q in sample if q.mode == "part")
    (tmp_path / "observer").mkdir()
    (tmp_path / "images").mkdir()
    img = tmp_path / "images" / "unchanged.png"
    img.write_bytes(b"original render")
    old = {"schema": "viewnet.query/1", "qid": "old"}
    Q.queries_path(tmp_path).write_text(json.dumps(old) + "\n")
    (tmp_path / "labels_crossfit.jsonl").write_text('{"stale":true}\n')
    (tmp_path / "observer" / "labels_crossfit.jsonl").write_text('{"stale":true}\n')
    (tmp_path / "observer" / "crossfit").mkdir()
    (tmp_path / "observer" / "crossfit" / "fold0.pt").write_bytes(b"stale checkpoint")
    for path, q in ((tmp_path / "observer" / "sets.jsonl", target),
                    (tmp_path / "sets.jsonl", part),
                    (tmp_path / "sets_ab.jsonl", target)):
        path.write_text(json.dumps({"set_key": path.stem, "part_id": q.part_id,
                                    "mode": q.mode, "target_instance": q.target_instance,
                                    "image": "images/unchanged.png", "qids": ["old"]}) + "\n")
    result = requery_store(tmp_path, records)
    assert result["changed"] and result["queries"] == len(sample)
    assert result["sets"] == {"observer/sets.jsonl": 1, "sets.jsonl": 1,
                               "sets_ab.jsonl": 1}
    for name, q in (("observer/sets.jsonl", target), ("sets.jsonl", part),
                    ("sets_ab.jsonl", target)):
        row = json.loads((tmp_path / name).read_text())
        assert row["image"] == "images/unchanged.png"
        assert set(row["qids"]) == {x.qid for x in sample if
                                   (x.part_id, x.mode, x.target_instance) ==
                                   (q.part_id, q.mode, q.target_instance)}
    assert img.read_bytes() == b"original render"
    assert not (tmp_path / "labels_crossfit.jsonl").exists()
    assert not (tmp_path / "observer" / "crossfit" / "fold0.pt").exists()
    archive = next((tmp_path / "query_archive").iterdir())
    assert (archive / "observer" / "queries.jsonl").exists()
    assert (archive / "labels_crossfit.jsonl").exists()
    assert (archive / "observer" / "labels_crossfit.jsonl").exists()
    assert (archive / "observer" / "crossfit" / "fold0.pt").exists()
    second = requery_store(tmp_path, records)
    assert not second["changed"] and second["archived"] == []
