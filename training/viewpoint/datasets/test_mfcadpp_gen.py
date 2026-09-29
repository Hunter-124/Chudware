"""mfcadpp_gen: our OCP port of the MFCAD++ generator, and its labels through the engine import.

Generator tests need OCP (skipped without it); the engine tests import the generated STEP with
libchudware (skipped when the engine is unavailable).
"""
from __future__ import annotations

import json
import random
from collections import Counter
from math import comb

import pytest

from training.viewpoint.datasets import importers as im
from training.viewpoint.datasets import mfcadpp_gen

try:
    import OCP  # noqa: F401
    HAVE_OCP = True
except ImportError:
    HAVE_OCP = False

try:
    from services.mcp import core_bridge as cb
    HAVE_ENGINE = cb.available()
except Exception:  # noqa: BLE001 — any import/load failure means no engine
    HAVE_ENGINE = False

needs_ocp = pytest.mark.skipif(not HAVE_OCP, reason="OCP (OpenCASCADE bindings) not installed")
needs_engine = pytest.mark.skipif(not (HAVE_OCP and HAVE_ENGINE),
                                  reason="libchudware or OCP unavailable")

# Every curved MFCAD++ class in one sequence; seeds 1 and 3 apply all seven, and their round
# produces a toroidal fillet face (a surface of revolution after the STEP round trip).
CURVED_COMBO = ["circular_through_slot", "h_circular_end_blind_slot", "through_hole",
                "blind_hole", "circular_end_pocket", "Oring", "round"]
CURVED_SEEDS = (1, 3)


@needs_ocp
def test_combo_draw_is_uniform_over_main_py_multisets():
    from training.viewpoint.datasets.mfcadpp_gen.generator import sample_combo
    rng = random.Random(7)
    draws = [sample_combo(rng) for _ in range(4000)]
    for c in draws:
        assert c == sorted(c) and 3 <= len(c) <= 9 and all(0 <= v < 24 for v in c)
    # main.py lists every multiset of each size once, so a size's share is its multiset count
    total = sum(comb(23 + k, k) for k in range(3, 10))
    sizes = Counter(len(c) for c in draws)
    for k in (7, 8, 9):
        assert sizes[k] / len(draws) == pytest.approx(comb(23 + k, k) / total, abs=0.03)
    assert any(len(set(c)) < len(c) for c in draws)          # with replacement
    assert sample_combo(random.Random(11)) == sample_combo(random.Random(11))


@needs_ocp
def test_generation_is_deterministic_and_labels_every_face(tmp_path):
    from training.viewpoint.datasets.mfcadpp_gen.generator import write_part
    a, b = tmp_path / "a", tmp_path / "b"
    for seed in (0, 5):
        ra, rb = write_part(seed, a), write_part(seed, b)
        assert ra["ok"] and rb["ok"], (ra, rb)
        sa = json.loads(mfcadpp_gen.sidecar_path(mfcadpp_gen.step_path(a, seed)).read_text())
        sb = json.loads(mfcadpp_gen.sidecar_path(mfcadpp_gen.step_path(b, seed)).read_text())
        assert sa == sb
        assert [f["id"] for f in sa["faces"]] == list(range(len(sa["faces"])))
        by_inst = {}
        for f in sa["faces"]:
            if f["class"] == "stock":
                assert f["instance"] == -1
            else:
                assert f["instance"] >= 0
                by_inst.setdefault(f["instance"], set()).add(f["class"])
        applied = {x["instance"]: x["class"] for x in sa["applied"]}
        for k, classes in by_inst.items():
            assert classes == {applied[k]}          # one feature -> one class


@needs_ocp
def test_reader_refuses_a_sidecar_that_does_not_fit_the_step(tmp_path):
    from training.viewpoint.datasets.mfcadpp_gen.generator import write_part
    assert write_part(2, tmp_path)["ok"]
    step = mfcadpp_gen.step_path(tmp_path, 2)
    part = im.read_mfcadpp_gen(step)
    assert part.dataset == "mfcadpp_gen" and part.instances
    side_file = mfcadpp_gen.sidecar_path(step)
    side = json.loads(side_file.read_text())
    big = max(side["faces"], key=lambda f: f["area"])
    small = min(side["faces"], key=lambda f: f["area"])
    big["id"], small["id"] = small["id"], big["id"]            # names now point at wrong faces
    side_file.write_text(json.dumps(side))
    with pytest.raises(im.LabelError, match="read back"):
        im.read_mfcadpp_gen(step)


@needs_engine
@pytest.mark.parametrize("seed", CURVED_SEEDS)
def test_curved_features_survive_engine_import(tmp_path, seed):
    from training.viewpoint.datasets.mfcadpp_gen.generator import write_part
    assert write_part(seed, tmp_path, combo=CURVED_COMBO)["ok"]
    step = mfcadpp_gen.step_path(tmp_path, seed)
    side = json.loads(mfcadpp_gen.sidecar_path(step).read_text())
    assert [a["class"] for a in side["applied"]] == CURVED_COMBO
    part = im.read_mfcadpp_gen(step)
    out = im.label_part(part)
    assert out.record is not None, out.report
    assert out.report["match_rate"] >= im.MIN_MATCH_RATE
    rec = out.record
    feats = Counter(f["class"] for f in rec["features"])
    for cls in ("slot_through", "slot_blind", "hole_through", "hole_blind", "pocket", "o_ring",
                "fillet"):
        assert feats[cls] >= 1, (cls, feats)
    # instances are the generator's: one record feature per applied feature that kept a face
    alive = {f["instance"] for f in side["faces"] if f["instance"] >= 0}
    assert len(rec["features"]) == len(alive)
    by_src = {f["source_class"]: f for f in rec["faces"]}
    assert {"through_hole", "blind_hole", "Oring", "round", "circular_through_slot",
            "circular_end_pocket", "h_circular_end_blind_slot"} <= set(by_src)
    holes = [f for f in rec["features"] if f["class"] in ("hole_through", "hole_blind", "o_ring")]
    for f in holes:
        assert f["axis"] is not None
    thr = next(f for f in rec["features"] if f["class"] == "hole_through")
    assert thr["through"] is True
    assert next(f for f in rec["features"] if f["class"] == "hole_blind")["through"] is False
    assert by_src["stock"]["instance"] == -1
