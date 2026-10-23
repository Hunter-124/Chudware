"""Tests for ISO 286 fit calculator.

Run: python -m pytest services/research/test_fits.py -v

Spot-check requirements:
- 20H7 = +0.021/0
- 20g6 = -0.007/-0.020
- 50H7/p6 is a press fit (interference)
"""
from __future__ import annotations

import math
import pytest

from ..research.fits import (
    FitError,
    fit_calc,
    tolerance_band,
    recommend_fit,
    _find_range,
    _fit_recs,
)


# ──────────────────────────────────────────────────── range lookup tests ──────

class TestFindRange:
    def test_20mm_in_18_30_range(self):
        row = _find_range(20.0)
        assert row["over_mm"] == 18 and row["up_to_mm"] == 30

    def test_50mm_in_30_50_range(self):
        """ISO convention: 50 is 'up to and including 50', so it falls in 30-50."""
        row = _find_range(50.0)
        assert row["over_mm"] == 30 and row["up_to_mm"] == 50

    def test_1mm_in_1_3_range(self):
        row = _find_range(1.0)
        assert row["over_mm"] == 1 and row["up_to_mm"] == 3

    def test_500mm_in_400_500_range(self):
        row = _find_range(500.0)
        assert row["over_mm"] == 400 and row["up_to_mm"] == 500

    def test_out_of_range_low(self):
        with pytest.raises(FitError) as exc_info:
            _find_range(0.5)
        assert exc_info.value.reason == "out_of_range"

    def test_out_of_range_high(self):
        with pytest.raises(FitError) as exc_info:
            _find_range(501.0)
        assert exc_info.value.reason == "out_of_range"

    def test_3mm_in_3_6_range(self):
        """3mm: over 1 up to 3? No — it's over 3 up to 6? Check convention.
        ISO: 'over 1 up to and including 3' — so 3mm IS in the 1-3 range.
        But 'over 3 up to 6' starts at >3. So exactly 3.0 is in 1-3."""
        row = _find_range(3.0)
        assert row["over_mm"] == 1 and row["up_to_mm"] == 3

    def test_3_001mm_in_3_6_range(self):
        row = _find_range(3.001)
        assert row["over_mm"] == 3 and row["up_to_mm"] == 6


# ──────────────────────────────────────────────────── tolerance_band tests ────

class TestToleranceBand:
    """Core spot-check: 20H7 = +0.021/0, 20g6 = -0.007/-0.020."""

    def test_20H7_upper(self):
        """20H7: ES = +0.021 mm, EI = 0."""
        result = tolerance_band(20.0, "7", "H", basis="hole")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["lower_mm"] == pytest.approx(0.0, abs=1e-9)
        assert ans["upper_mm"] == pytest.approx(0.021, abs=1e-6), \
            f"20H7 ES should be +0.021, got {ans['upper_mm']}"

    def test_20g6_deviations(self):
        """20g6: es = -0.007 mm, ei = -0.020 mm."""
        result = tolerance_band(20.0, "6", "g", basis="shaft")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["upper_mm"] == pytest.approx(-0.007, abs=1e-6), \
            f"20g6 es should be -0.007, got {ans['upper_mm']}"
        assert ans["lower_mm"] == pytest.approx(-0.020, abs=1e-6), \
            f"20g6 ei should be -0.020, got {ans['lower_mm']}"

    def test_H7_source_is_closed_form(self):
        result = tolerance_band(20.0, "7", "H")
        assert result["source"]["kind"] == "closed_form"

    def test_g6_source_is_closed_form(self):
        result = tolerance_band(20.0, "6", "g")
        assert result["source"]["kind"] == "closed_form"

    def test_p6_source_is_table(self):
        """p shaft deviation is tabulated."""
        result = tolerance_band(50.0, "6", "p", basis="shaft")
        assert result["ok"] is True
        assert result["source"]["kind"] == "table"

    def test_50H7_band(self):
        """50H7: IT7 in 30-50 range = 25 µm. EI=0, ES=+0.025."""
        result = tolerance_band(50.0, "7", "H")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["lower_mm"] == pytest.approx(0.0, abs=1e-9)
        assert ans["upper_mm"] == pytest.approx(0.025, abs=1e-6), \
            f"50H7 ES should be +0.025, got {ans['upper_mm']}"

    def test_50p6_band(self):
        """50p6: es=+42µm, IT6=16µm, ei=+26µm. Abs: 50.026-50.042."""
        result = tolerance_band(50.0, "6", "p")
        assert result["ok"] is True
        ans = result["answer"]
        # es = +42 µm = +0.042 mm
        assert ans["upper_mm"] == pytest.approx(0.042, abs=1e-6), \
            f"50p6 es should be +0.042, got {ans['upper_mm']}"
        # ei = +26 µm = +0.026 mm
        assert ans["lower_mm"] == pytest.approx(0.026, abs=1e-6), \
            f"50p6 ei should be +0.026, got {ans['lower_mm']}"

    def test_h6_es_zero(self):
        """h shaft: fundamental deviation es=0."""
        result = tolerance_band(20.0, "6", "h")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["upper_mm"] == pytest.approx(0.0, abs=1e-9)
        # ei = -IT6 = -0.013
        assert ans["lower_mm"] == pytest.approx(-0.013, abs=1e-6)

    def test_js_symmetric(self):
        """js7 at 20mm: IT7=21µm, so floor(21/2)=10µm upper, ceil(21/2)=11µm lower."""
        result = tolerance_band(20.0, "7", "js")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["upper_mm"] == pytest.approx(0.010, abs=1e-6)
        assert ans["lower_mm"] == pytest.approx(-0.011, abs=1e-6)

    def test_unsupported_hole_degrades(self):
        """Unsupported holes (K, N, P...) should degrade (return ok=False), not raise."""
        result = tolerance_band(20.0, "7", "K")
        assert result["ok"] is False
        assert result["reason"] == "unsupported_hole"

    def test_schema_citation(self):
        result = tolerance_band(20.0, "7", "H")
        assert result["schema"] == "aicad.calc/1"
        assert "ISO 286" in result["source"]["citation"]


# ──────────────────────────────────────────────────── fit_calc tests ─────────

class TestFitCalc:
    """Combined fit analysis tests including the three mandatory spot checks."""

    def test_20H7_g6_clearance_range(self):
        """Key spot check: 20H7/g6 is a sliding clearance fit.
        Hole [0, +0.021], shaft [-0.020, -0.007].
        Min clearance = 0 - (-0.007) = 0.007 mm.
        Max clearance = 0.021 - (-0.020) = 0.041 mm.
        """
        result = fit_calc(20.0, "H7/g6")
        assert result["ok"] is True
        ans = result["answer"]

        # Hole band
        assert ans["hole"][0] == pytest.approx(0.0, abs=1e-9)
        assert ans["hole"][1] == pytest.approx(0.021, abs=1e-6)

        # Shaft band
        assert ans["shaft"][0] == pytest.approx(-0.020, abs=1e-6)
        assert ans["shaft"][1] == pytest.approx(-0.007, abs=1e-6)

        # Clearance range
        assert ans["clearance"][0] == pytest.approx(0.007, abs=1e-6), \
            f"Min clearance should be 0.007, got {ans['clearance'][0]}"
        assert ans["clearance"][1] == pytest.approx(0.041, abs=1e-6), \
            f"Max clearance should be 0.041, got {ans['clearance'][1]}"

        # Fit character
        assert ans["fit_character"] == "clearance"

    def test_20H7_g6_absolute_sizes(self):
        result = fit_calc(20.0, "H7/g6")
        ans = result["answer"]
        assert ans["hole_absolute"][0] == pytest.approx(20.000, abs=1e-6)
        assert ans["hole_absolute"][1] == pytest.approx(20.021, abs=1e-6)
        assert ans["shaft_absolute"][0] == pytest.approx(19.980, abs=1e-6)
        assert ans["shaft_absolute"][1] == pytest.approx(19.993, abs=1e-6)

    def test_50H7_p6_is_press_fit(self):
        """50H7/p6: hole [0, +0.025], shaft [+0.026, +0.042].
        Min interference = 0.026 - 0.025 = 0.001 mm.
        Max interference = 0.042 - 0.000 = 0.042 mm.
        fit_character = 'press'.
        """
        result = fit_calc(50.0, "H7/p6")
        assert result["ok"] is True
        ans = result["answer"]

        # Hole band: H7 on 30-50 range = IT7=25µm
        assert ans["hole"][0] == pytest.approx(0.0, abs=1e-9)
        assert ans["hole"][1] == pytest.approx(0.025, abs=1e-6)

        # Shaft band: p6 on 30-50 range, es=+42µm, IT6=16µm, ei=+26µm
        assert ans["shaft"][0] == pytest.approx(0.026, abs=1e-6)
        assert ans["shaft"][1] == pytest.approx(0.042, abs=1e-6)

        # Clearance (negative = interference)
        # min_clr = hole_min - shaft_max = 0.000 - 0.042 = -0.042
        # max_clr = hole_max - shaft_min = 0.025 - 0.026 = -0.001
        assert ans["clearance"][0] == pytest.approx(-0.042, abs=1e-6)
        assert ans["clearance"][1] == pytest.approx(-0.001, abs=1e-6)

        # Press fit
        assert ans["fit_character"] == "press", \
            f"50H7/p6 should be press fit, got {ans['fit_character']!r}"

    def test_H7_k6_transition_fit(self):
        """H7/k6 at 20mm should be transition (some clearance, some interference possible)."""
        result = fit_calc(20.0, "H7/k6")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["fit_character"] == "transition"

    def test_H7_m6_recommended_transition_fit(self):
        result = fit_calc(20.0, "H7/m6")
        assert result["ok"] is True
        assert result["answer"]["shaft"] == pytest.approx([0.008, 0.021])
        assert result["answer"]["fit_character"] == "transition"

    def test_shaft_only_input(self):
        result = fit_calc(20.0, "g6")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["hole"] is None
        assert ans["shaft"] is not None
        assert ans["shaft"][1] == pytest.approx(-0.007, abs=1e-6)
        assert ans["clearance"] is None

    def test_hole_only_input(self):
        result = fit_calc(20.0, "H7")
        assert result["ok"] is True
        ans = result["answer"]
        assert ans["shaft"] is None
        assert ans["hole"][1] == pytest.approx(0.021, abs=1e-6)
        assert ans["clearance"] is None

    def test_envelope_schema(self):
        result = fit_calc(20.0, "H7/g6")
        assert result["schema"] == "aicad.calc/1"
        assert result["calc"] == "fit_calc"
        assert "ISO 286" in result["source"]["citation"]
        assert result["units"] == "mm"

    def test_prefilled_recorded(self):
        pf = {"nominal": "doc_eval_measure F7"}
        result = fit_calc(20.0, "H7/g6", prefilled=pf)
        assert result["ok"] is True
        assert result["inputs"]["prefilled"] == pf

    def test_bad_fit_string(self):
        result = fit_calc(20.0, "JUNK")
        assert result["ok"] is False
        assert result["reason"] == "bad_fit"

    def test_out_of_range_nominal(self):
        result = fit_calc(600.0, "H7/g6")
        assert result["ok"] is False
        assert "out_of_range" in result["reason"]

    def test_backslash_separator(self):
        """Accept backslash as fit separator."""
        result = fit_calc(20.0, r"H7\g6")
        assert result["ok"] is True

    def test_source_kind_table_for_p_shaft(self):
        """p shaft uses tabulated data -> source.kind=table."""
        result = fit_calc(50.0, "H7/p6")
        assert result["source"]["kind"] == "table"

    def test_source_kind_closed_form_for_g_shaft(self):
        """g shaft uses formula -> source.kind=closed_form."""
        result = fit_calc(20.0, "H7/g6")
        assert result["source"]["kind"] == "closed_form"


# ──────────────────────────────────────────────────── recommend_fit tests ─────

class TestRecommendFit:
    def test_sliding_gives_H7g6(self):
        result = recommend_fit("sliding")
        assert result["ok"] is True
        classes = result["answer"][0]["recommended_classes"]
        assert "H7/g6" in classes

    def test_press_fit_character(self):
        result = recommend_fit("press fit")
        assert result["ok"] is True
        assert result["answer"][0]["character"] == "press"

    def test_bearing_journal_returns_clearance(self):
        result = recommend_fit("bearing")
        assert result["ok"] is True
        # any bearing recommendation should include clearance or transition
        characters = [r["character"] for r in result["answer"]]
        assert any(c in ("clearance", "transition_to_press") for c in characters)

    def test_unknown_keyword_degrades(self):
        result = recommend_fit("xyzzy_nonexistent")
        assert result["ok"] is False
        assert result["reason"] == "not_found"

    def test_envelope_schema(self):
        result = recommend_fit("run")
        assert result["schema"] == "aicad.calc/1"
        assert result["calc"] == "recommend_fit"
        assert result["source"]["kind"] == "table"

    def test_empty_function_degrades(self):
        result = recommend_fit("")
        assert result["ok"] is False

    def test_dowel_recommendation(self):
        result = recommend_fit("dowel pin")
        assert result["ok"] is True
        # Should recommend p6 or n6
        all_classes = [c for r in result["answer"] for c in r["recommended_classes"]]
        assert any("p6" in c or "n6" in c for c in all_classes)

    def test_every_recommended_class_is_computable(self):
        failures = []
        for row in _fit_recs():
            for fit_class in row["recommended_classes"]:
                result = fit_calc(20.0, fit_class)
                if not result.get("ok"):
                    failures.append((row["id"], fit_class, result.get("reason")))
        assert failures == []


# ──────────────────────────────────────────────────── envelope conformance ────

class TestCalc1Conformance:
    """Verify aicad.calc/1 shape has all required keys."""

    REQUIRED_KEYS = {"schema", "ok", "calc", "answer", "units",
                     "equation", "inputs", "source", "notes"}
    SOURCE_KEYS = {"kind", "citation"}

    def _check(self, result: dict) -> None:
        missing = self.REQUIRED_KEYS - set(result)
        assert not missing, f"Missing keys: {missing}"
        assert set(result["source"]) >= self.SOURCE_KEYS
        assert result["schema"] == "aicad.calc/1"
        assert isinstance(result["notes"], list)

    def test_fit_calc_conformance(self):
        self._check(fit_calc(20.0, "H7/g6"))

    def test_tolerance_band_conformance(self):
        self._check(tolerance_band(20.0, "7", "H"))

    def test_recommend_fit_conformance(self):
        self._check(recommend_fit("sliding"))
