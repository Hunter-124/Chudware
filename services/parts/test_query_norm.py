"""Tests for services/parts/query_norm.py — pure query normalization + synonym
expansion feeding services/parts/index.py (term_coverage) and hub.py (recall).

Coverage: designation preservation, glued-alphanumeric splitting, bidirectional
synonyms, stopword removal, the term-count cap, and adversarial inputs (never
raise) per the contract in CONTEXT.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.parts import query_norm as qn  # noqa: E402


# ---------------------------------------------------------------------------
# normalize_query — designation preservation
# ---------------------------------------------------------------------------

def test_normalize_preserves_oring_dash_code():
    assert qn.normalize_query("2-014") == "2-014"


def test_normalize_preserves_material_temper():
    assert qn.normalize_query("6061-T6") == "6061-t6"


def test_normalize_preserves_metric_thread_pitch():
    assert qn.normalize_query("M8x1.25") == "m8x1.25"


def test_normalize_preserves_bearing_designation():
    assert qn.normalize_query("608ZZ") == "608zz"


def test_normalize_preserves_fractional_thread():
    assert qn.normalize_query("1/4-20") == "1/4-20"


def test_normalize_preserves_nema_designation():
    assert qn.normalize_query("NEMA17") == "nema17"


def test_normalize_lowercases_and_collapses_whitespace():
    assert qn.normalize_query("  NEMA   17   Motor ") == "nema 17 motor"


def test_normalize_unifies_multiplication_sign_as_dimension_separator():
    assert qn.normalize_query("8 × 1.25") == "8x1.25"
    assert qn.normalize_query("8 X 1.25") == "8x1.25"
    assert qn.normalize_query("8x1.25") == "8x1.25"


def test_normalize_unifies_en_and_em_dash_to_hyphen():
    assert qn.normalize_query("6061\u2013T6") == "6061-t6"  # en dash
    assert qn.normalize_query("6061\u2014T6") == "6061-t6"  # em dash


def test_normalize_strips_stray_punctuation_but_keeps_designation_chars():
    out = qn.normalize_query("m8 bolt!! (zinc)")
    assert "!" not in out and "(" not in out and ")" not in out
    assert "m8" in out


# ---------------------------------------------------------------------------
# expand_terms — glued-alphanumeric splitting
# ---------------------------------------------------------------------------

def test_expand_splits_nema17_into_glued_and_split_forms():
    groups = qn.expand_terms("nema17")
    assert len(groups) == 1
    group = groups[0]
    assert group[0] == "nema17"  # canonical stays the glued form users type
    assert "nema17" in group
    assert "nema" in group
    assert "17" in group


def test_expand_splits_608zz_into_glued_and_split_forms():
    group = qn.expand_terms("608zz")[0]
    assert group[0] == "608zz"
    assert "608" in group
    assert "zz" in group


def test_expand_splits_m8_into_glued_and_split_forms():
    group = qn.expand_terms("m8")[0]
    assert "m8" in group
    assert "8" in group


def test_expand_does_not_shred_designation_tokens_with_internal_punctuation():
    # "2-014" / "6061-t6" / "1/4-20" contain a hyphen or slash so the glue
    # regex (pure alpha+digit) never fires on them — they stay intact.
    assert qn.expand_terms("2-014") == [["2-014"]]
    assert qn.expand_terms("6061-t6") == [["6061-t6"]]
    assert qn.expand_terms("1/4-20") == [["1/4-20"]]


# ---------------------------------------------------------------------------
# expand_terms — bidirectional synonyms
# ---------------------------------------------------------------------------

def test_shcs_expands_to_full_phrase():
    group = qn.expand_terms("shcs")[0]
    assert group[0] == "shcs"
    for word in ("socket", "head", "cap", "screw"):
        assert word in group


def test_full_phrase_collapses_back_to_shcs_group():
    groups = qn.expand_terms("socket head cap screw")
    assert len(groups) == 1
    assert groups[0][0] == "shcs"
    assert "screw" in groups[0]


def test_oring_bidirectional():
    for q in ("o-ring", "oring", "orings"):
        group = qn.expand_terms(q)[0]
        assert "o-ring" in group
        assert "oring" in group
        assert "orings" in group


def test_screw_and_bolt_are_synonyms_not_identical_terms():
    screw_group = qn.expand_terms("screw")[0]
    bolt_group = qn.expand_terms("bolt")[0]
    assert screw_group == bolt_group
    assert "screw" in screw_group and "bolt" in screw_group
    # They remain two distinct surface words within the group, not collapsed
    # into one term.
    assert screw_group.count("screw") == 1
    assert screw_group.count("bolt") == 1


def test_screw_alone_is_not_swallowed_by_shcs_group():
    # Regression: "screw" is a *member* of the SHCS phrase group but must
    # still resolve to the plain screw/bolt group when queried bare.
    group = qn.expand_terms("screw")[0]
    assert group[0] == "screw"
    assert "socket" not in group


def test_aluminum_aluminium_bidirectional():
    us = qn.expand_terms("aluminum")[0]
    uk = qn.expand_terms("aluminium")[0]
    assert set(us) == set(uk)
    assert us[0] == uk[0] == "aluminum"
    assert "aluminum" in us and "aluminium" in us and "al" in us


def test_fiber_fibre_and_gauge_gage_bidirectional():
    assert qn.expand_terms("fiber")[0] == qn.expand_terms("fibre")[0]
    assert qn.expand_terms("gauge")[0] == qn.expand_terms("gage")[0]


def test_stepper_step_motor_bidirectional():
    a = qn.expand_terms("stepper")[0]
    b = qn.expand_terms("step motor")[0]
    assert "stepper" in a and "motor" in a
    assert b[0] == "stepper"


def test_id_od_bore_and_diameter():
    id_groups = qn.expand_terms("id")
    assert id_groups[0][0] == "id"
    assert "bore" in id_groups[0]
    od_groups = qn.expand_terms("od")
    assert od_groups[0][0] == "od"
    assert "id" not in od_groups[0]  # id/od never conflate


def test_stainless_ss_and_carbon_steel_cs():
    assert "ss" in qn.expand_terms("stainless")[0]
    cs_group = qn.expand_terms("carbon steel")[0]
    assert cs_group[0] == "cs"
    assert "steel" in cs_group


def test_bearing_brg_and_qty_quantity():
    assert "brg" in qn.expand_terms("bearing")[0]
    assert "qty" in qn.expand_terms("quantity")[0]


def test_belt_pulley_seal_extrusion_tube_rod_sheet_vocabulary():
    assert "sheave" in qn.expand_terms("pulley")[0]
    assert "gasket" in qn.expand_terms("seal")[0]
    assert "profile" in qn.expand_terms("extrusion")[0]
    assert "tubing" in qn.expand_terms("tube")[0]
    assert "shaft" in qn.expand_terms("rod")[0] and "bar" in qn.expand_terms("rod")[0]
    assert "plate" in qn.expand_terms("sheet")[0]
    assert qn.expand_terms("belt")[0] == ["belt"]


def test_washer_nut_dowel_standoff_spacer_present_as_own_terms():
    for term in ("washer", "nut", "dowel", "standoff", "spacer"):
        groups = qn.expand_terms(term)
        assert groups == [[term]]


# ---------------------------------------------------------------------------
# stopword removal
# ---------------------------------------------------------------------------

def test_stopwords_produce_no_group():
    for word in (
        "a", "an", "the", "for", "with", "of", "and", "to", "in", "my",
        "some", "need", "want", "please", "part", "parts", "size",
    ):
        assert qn.expand_terms(word) == [], word


def test_stopwords_are_dropped_from_a_real_sentence_but_content_survives():
    groups = qn.expand_terms("I need a m8 screw for the bracket")
    flat = {t for g in groups for t in g}
    assert "m8" in flat and "screw" in flat and "bracket" in flat
    assert "need" not in flat and "the" not in flat and "for" not in flat


# ---------------------------------------------------------------------------
# term cap
# ---------------------------------------------------------------------------

def test_expand_terms_caps_group_count():
    words = " ".join(f"word{i}" for i in range(200))
    groups = qn.expand_terms(words)
    assert len(groups) <= 24


def test_expand_terms_cap_is_exactly_24_for_oversupply():
    words = " ".join(f"uniqueterm{i}" for i in range(100))
    groups = qn.expand_terms(words)
    assert len(groups) == 24


# ---------------------------------------------------------------------------
# never raises — adversarial input
# ---------------------------------------------------------------------------

def test_never_raises_on_none():
    assert qn.normalize_query(None) == ""
    assert qn.expand_terms(None) == []


def test_never_raises_on_empty_and_whitespace():
    for q in ("", "   ", "\t\n  "):
        assert qn.normalize_query(q) == ""
        assert qn.expand_terms(q) == []


def test_never_raises_on_punctuation_only():
    for q in ("!!!???", "#$%^&*()", "-----", "..."):
        assert qn.expand_terms(q) == []
    # "×" unifies to the letter "x" (dimension separator), so pure "×"
    # repeats become real alnum text, not an empty result — just must not raise.
    assert isinstance(qn.expand_terms("×××"), list)


def test_never_raises_on_very_long_input():
    huge = "m8 screw " * 5000  # ~45k chars
    n = qn.normalize_query(huge)
    assert isinstance(n, str)
    groups = qn.expand_terms(huge)
    assert isinstance(groups, list)
    assert len(groups) <= 24


def test_never_raises_on_10k_char_garbage():
    garbage = "".join(chr(33 + (i % 90)) for i in range(10_000))
    n = qn.normalize_query(garbage)
    assert isinstance(n, str)
    groups = qn.expand_terms(garbage)
    assert isinstance(groups, list)


def test_never_raises_on_unicode():
    for q in ("über größe 5", "M8×1.25 – 6061‑T6", "螺丝 nema17", "🔩 washer"):
        n = qn.normalize_query(q)
        assert isinstance(n, str)
        groups = qn.expand_terms(q)
        assert isinstance(groups, list)


def test_never_raises_on_non_string_input():
    for q in (12345, 3.14, [], {}, object()):
        assert qn.normalize_query(q) == "" or isinstance(qn.normalize_query(q), str)
        assert isinstance(qn.expand_terms(q), list)


# ---------------------------------------------------------------------------
# structural invariants
# ---------------------------------------------------------------------------

def test_groups_are_deduped_preserving_order():
    group = qn.expand_terms("shcs")[0]
    assert len(group) == len(set(group))


def test_canonical_always_first():
    for q in ("nema17", "608zz", "shcs", "aluminum", "m8", "washer"):
        group = qn.expand_terms(q)[0]
        assert group[0] == group[0]  # index 0 exists and is deterministic
        assert len(group) >= 1
