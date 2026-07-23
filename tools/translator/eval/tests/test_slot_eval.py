#!/usr/bin/env python3
"""Deterministic pytest suite for the T-F10 translator eval stack.

Covers the invariants of `gsl_ser` (round-trip, version tag), `eval.slot_accuracy`
(template baseline, intrinsic/extrinsic/missing detection), the `groundedness` seam
(stub behaviour + checker swap), and `eval.harness` (gold loading, fold evaluation,
ROC-AUC). Engine-free: everything operates on plain state dicts, no libchudware.

Run from the repo root:

    python3 -m pytest tools/translator/eval/tests/ -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.translator import gsl_ser, groundedness
from tools.translator.eval import harness, slot_accuracy

GOLD_DIR = Path(__file__).resolve().parents[1] / "gold"
GOLD_FILES = sorted(GOLD_DIR.glob("*.json"))

# A representative brep state (mirrors the shape of gold/brep_plate_bolt_circle.json)
# used by tests that want one known-clean fixture rather than the whole gold set.
BREP_STATE = {
    "domain": "brep",
    "part": "bolt_plate",
    "bbox_mm": [120.0, 80.0, 10.0],
    "faces": 18,
    "edges": 40,
    "symmetry": {"order": 6, "type": "rotational", "axis": "z"},
    "feature_groups": [
        {"id": "F1", "name": "Plate", "type": "box", "faces": 6},
        {"id": "F2", "name": "Bolts", "type": "bolt_circle", "faces": 6,
         "params": {"count": 6, "diameter": 8.0, "pcd": 60.0}},
    ],
    "bores": [
        {"sid": "f11", "radius": 4.0}, {"sid": "f12", "radius": 4.0},
        {"sid": "f13", "radius": 4.0},
    ],
}


def _load_gold_states() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for p in GOLD_FILES:
        data = json.loads(p.read_text())
        for ex in (data if isinstance(data, list) else [data]):
            out.append((ex.get("id", p.stem), ex["state"]))
    return out


GOLD_STATES = _load_gold_states()
GOLD_IDS = [gid for gid, _ in GOLD_STATES]

assert GOLD_STATES, f"no gold examples found under {GOLD_DIR}"


def _one_numeral_sentence(state: dict, slot) -> tuple[str, str]:
    """(sentence, token) for `slot` as `render_caption` would render it.
    render_caption uses the dotted path as the label (keeps index digits dot-guarded)."""
    token = gsl_ser.prose_token(slot.value, slot.unit)
    return f"{slot.path} is {token}.", token


# ------------------------------------------------------------------ 1. round-trip exactness


def test_roundtrip_exact_representative_state():
    assert gsl_ser.roundtrip_diff(BREP_STATE) == []
    assert gsl_ser.slots_equal(
        gsl_ser.extract_slots(gsl_ser.serialize(BREP_STATE)),
        gsl_ser.enumerate_slots(BREP_STATE),
    )


@pytest.mark.parametrize("state_id,state", GOLD_STATES, ids=GOLD_IDS)
def test_roundtrip_exact_gold(state_id, state):
    assert gsl_ser.roundtrip_diff(state) == []
    assert gsl_ser.slots_equal(
        gsl_ser.extract_slots(gsl_ser.serialize(state)),
        gsl_ser.enumerate_slots(state),
    )


# ------------------------------------------------------------------ 2. version tag present


def test_version_tag_present():
    assert gsl_ser.SER_VERSION == "gsl-ser/v1"
    text = gsl_ser.serialize(BREP_STATE)
    assert text.splitlines()[0].startswith("#gsl-ser/v1")


# ------------------------------------------------------------------ 3. template baseline == 1.0


@pytest.mark.parametrize("state_id,state", GOLD_STATES, ids=GOLD_IDS)
def test_template_caption_scores_perfect(state_id, state):
    """render_caption is built FROM enumerate_slots, so by construction it should be
    fully grounded: slot_accuracy == 1.0, zero wrong, zero extrinsic (per the
    render_caption/harness docstrings: 'contains all and only grounded numerals')."""
    caption = gsl_ser.render_caption(state)
    report = slot_accuracy.score(caption, state)
    assert report.wrong == 0
    assert report.extrinsic == 0
    assert report.slot_accuracy == 1.0


# ------------------------------------------------------------------ 4. intrinsic detection


@pytest.mark.parametrize("state_id,state", GOLD_STATES, ids=GOLD_IDS)
def test_intrinsic_corruption_detected(state_id, state):
    slots = [s for s in gsl_ser.enumerate_slots(state) if s.numeric]
    assert slots, f"{state_id} has no numeric slots to corrupt"
    target = slots[len(slots) // 2]
    caption = gsl_ser.render_caption(state)
    sentence, good_token = _one_numeral_sentence(state, target)
    assert sentence in caption, f"expected sentence {sentence!r} not found in caption"
    bad_token = gsl_ser.prose_token(float(target.value) * 1.7 + 5.0, target.unit)
    corrupted = caption.replace(sentence, f"{target.path} is {bad_token}.")
    assert corrupted != caption

    report = slot_accuracy.score(corrupted, state)
    assert report.wrong >= 1
    assert report.slot_accuracy < 1.0


# ------------------------------------------------------------------ 5. extrinsic detection


@pytest.mark.parametrize("state_id,state", GOLD_STATES, ids=GOLD_IDS)
def test_extrinsic_corruption_detected(state_id, state):
    caption = gsl_ser.render_caption(state) + " ghost bore radius is 999.0mm."
    report = slot_accuracy.score(caption, state)
    assert report.extrinsic >= 1


# ------------------------------------------------------------------ 6. missing detection


@pytest.mark.parametrize("state_id,state", GOLD_STATES, ids=GOLD_IDS)
def test_missing_slot_detected(state_id, state):
    slots = [s for s in gsl_ser.enumerate_slots(state) if s.numeric]
    assert slots, f"{state_id} has no numeric slots to remove"
    target = slots[0]
    caption = gsl_ser.render_caption(state)
    sentence, _ = _one_numeral_sentence(state, target)
    assert sentence in caption, f"expected sentence {sentence!r} not found in caption"
    stripped = caption.replace(sentence, "").strip()
    assert stripped != caption

    report = slot_accuracy.score(stripped, state)
    assert report.missing >= 1


# ------------------------------------------------------------------ 7. groundedness seam


def test_groundedness_is_stub_by_default():
    assert groundedness.is_stub() is True


def test_groundedness_clean_caption_all_grounded():
    caption = gsl_ser.render_caption(BREP_STATE)
    verdicts = groundedness.check(caption, BREP_STATE)
    assert verdicts, "expected at least one numeral to check"
    assert all(v.grounded for v in verdicts)


def test_groundedness_invented_numeral_ungrounded():
    caption = gsl_ser.render_caption(BREP_STATE) + " ghost bore radius is 999.0mm."
    verdicts = groundedness.check(caption, BREP_STATE)
    assert any(not v.grounded for v in verdicts)


def test_groundedness_set_checker_swap_and_restore():
    original = groundedness.get_checker()

    class _DummyChecker:
        def check(self, text: str, state: dict):
            return []

    dummy = _DummyChecker()
    try:
        groundedness.set_checker(dummy)
        assert groundedness.get_checker() is dummy
    finally:
        groundedness.set_checker(original)

    assert groundedness.get_checker() is original
    assert groundedness.is_stub() is True


# ------------------------------------------------------------------ 8. harness end-to-end


def test_harness_load_gold_has_at_least_five_examples():
    examples = harness.load_gold()
    assert len(examples) >= 5


def test_harness_evaluate_folds_report_perfect_template_baseline():
    examples = harness.load_gold()
    results = harness.evaluate(examples)
    assert set(results["folds"]) == {"held_out", "seen"}
    for fold_name, fold in results["folds"].items():
        tmin = fold["template_slot_accuracy_min"]
        assert tmin == 1.0 or tmin is None, f"fold {fold_name!r}: template min = {tmin!r}"


def test_harness_roc_auc():
    assert harness.roc_auc([0.0, 0.0], [0.5, 0.7]) == 1.0
    assert harness.roc_auc([], [1.0]) is None
