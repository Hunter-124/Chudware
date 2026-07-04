"""Validation tests for engine.tolerance.stackup (T-BL08).

Hand-calc reference
-------------------
The numerical values below are derived from the standard 1D chain formula
as presented in:

    Fischer, Bruce R., "Mechanical Tolerance Stackup and Analysis,"
    2nd ed., CRC Press, 2011, Chapter 3 ("The Tolerance Stackup").
    ISBN 978-1-4398-1572-3.

    Also consistent with:
    Meadows, James D., "Geometric Dimensioning and Tolerancing: Applications,
    Analysis & Measurement," ASME Press, 2009, Chapter 9.

Example: three-dimension linear gap (assembly clearance)
---------------------------------------------------------
A slot of nominal width W is occupied by two blocks of nominal widths B1 and B2.
The assembly gap G = W − B1 − B2 is the closure dimension.

Chain (all dims equal-bilateral ±):
  Dim A  (slot width)       : nominal = 40.000 mm, tol = ±0.050 mm, direction = +1
  Dim B  (block 1 width)    : nominal = 17.000 mm, tol = ±0.025 mm, direction = −1
  Dim C  (block 2 width)    : nominal = 20.000 mm, tol = ±0.040 mm, direction = −1

Nominal gap:
  G_nom = (+1)(40.000) + (−1)(17.000) + (−1)(20.000) = 3.000 mm

Worst-case total tolerance (Σ |a_i| * tol_i):
  T_wc = 0.050 + 0.025 + 0.040 = 0.115 mm
  gap_max_wc = 3.000 + 0.115 = 3.115 mm
  gap_min_wc = 3.000 − 0.115 = 2.885 mm

RSS total tolerance (√Σ (a_i * tol_i)²):
  T_rss = √(0.050² + 0.025² + 0.040²)
        = √(0.002500 + 0.000625 + 0.001600)
        = √0.004725
        ≈ 0.068738... mm
  gap_max_rss = 3.000 + 0.068738 = 3.068738 mm
  gap_min_rss = 3.000 − 0.068738 = 2.931262 mm

Sensitivity ranking (worst-case contribution):
  1. Dim A: 0.050 mm  (43.48% of WC budget)
  2. Dim C: 0.040 mm  (34.78%)
  3. Dim B: 0.025 mm  (21.74%)

Sanity checks (pure algebra, always true for any valid stackup):
  - RSS ≤ WC (equality only if n=1 or all tolerances equal but n=1)
  - nominal_gap is independent of tolerances
  - gap_min_wc ≤ gap_min_rss ≤ gap_max_rss ≤ gap_max_wc
"""
from __future__ import annotations

import math
import pytest
from engine.tolerance.stackup import stackup_analyze, StackupResult


# ---------------------------------------------------------------------------
# Textbook example constants (independently derived — do NOT edit to match code)
# ---------------------------------------------------------------------------

DIMS_3 = [
    {"name": "A_slot_width",   "nominal": 40.000, "tol": 0.050, "direction":  1.0},
    {"name": "B_block1_width", "nominal": 17.000, "tol": 0.025, "direction": -1.0},
    {"name": "C_block2_width", "nominal": 20.000, "tol": 0.040, "direction": -1.0},
]

EXPECTED_NOMINAL_GAP    = 3.000        # mm
EXPECTED_TOL_WC         = 0.115        # mm  = 0.050 + 0.025 + 0.040
EXPECTED_GAP_MAX_WC     = 3.115        # mm
EXPECTED_GAP_MIN_WC     = 2.885        # mm
EXPECTED_TOL_RSS        = math.sqrt(0.050**2 + 0.025**2 + 0.040**2)  # ≈ 0.068738
EXPECTED_GAP_MAX_RSS    = EXPECTED_NOMINAL_GAP + EXPECTED_TOL_RSS
EXPECTED_GAP_MIN_RSS    = EXPECTED_NOMINAL_GAP - EXPECTED_TOL_RSS

EPS = 1e-9   # floating-point tolerance for exact algebraic comparisons


class TestTextbookExample3Dims:
    """Hand-calc validation against the three-dimension gap example."""

    def setup_method(self):
        self.result = stackup_analyze(DIMS_3)

    def test_nominal_gap(self):
        assert abs(self.result.nominal_gap - EXPECTED_NOMINAL_GAP) < EPS, (
            f"nominal_gap={self.result.nominal_gap}, expected={EXPECTED_NOMINAL_GAP}"
        )

    def test_worst_case_total_tol(self):
        assert abs(self.result.total_tol_wc - EXPECTED_TOL_WC) < EPS, (
            f"total_tol_wc={self.result.total_tol_wc}, expected={EXPECTED_TOL_WC}"
        )

    def test_worst_case_gap_max(self):
        assert abs(self.result.gap_max_wc - EXPECTED_GAP_MAX_WC) < EPS

    def test_worst_case_gap_min(self):
        assert abs(self.result.gap_min_wc - EXPECTED_GAP_MIN_WC) < EPS

    def test_rss_total_tol(self):
        assert abs(self.result.total_tol_rss - EXPECTED_TOL_RSS) < 1e-9, (
            f"total_tol_rss={self.result.total_tol_rss:.8f}, expected={EXPECTED_TOL_RSS:.8f}"
        )

    def test_rss_gap_max(self):
        assert abs(self.result.gap_max_rss - EXPECTED_GAP_MAX_RSS) < EPS

    def test_rss_gap_min(self):
        assert abs(self.result.gap_min_rss - EXPECTED_GAP_MIN_RSS) < EPS

    def test_rss_less_than_wc(self):
        """Pure algebra: RSS always ≤ worst-case for n > 1 dims."""
        assert self.result.total_tol_rss < self.result.total_tol_wc

    def test_sensitivity_ranking_first(self):
        """Dim A (0.050) has the highest WC contribution and sorts first."""
        assert self.result.contributors[0].name == "A_slot_width"
        assert abs(self.result.contributors[0].contrib_wc - 0.050) < EPS

    def test_sensitivity_ranking_second(self):
        """Dim C (0.040) sorts second."""
        assert self.result.contributors[1].name == "C_block2_width"
        assert abs(self.result.contributors[1].contrib_wc - 0.040) < EPS

    def test_sensitivity_ranking_third(self):
        """Dim B (0.025) sorts last."""
        assert self.result.contributors[2].name == "B_block1_width"
        assert abs(self.result.contributors[2].contrib_wc - 0.025) < EPS

    def test_pct_wc_sums_to_100(self):
        total_pct = sum(c.pct_wc for c in self.result.contributors)
        assert abs(total_pct - 100.0) < 1e-6

    def test_pct_rss_sums_to_100(self):
        total_pct = sum(c.pct_rss for c in self.result.contributors)
        assert abs(total_pct - 100.0) < 1e-6

    def test_gap_ordering_invariant(self):
        """gap_min_wc ≤ gap_min_rss ≤ nominal ≤ gap_max_rss ≤ gap_max_wc"""
        r = self.result
        assert r.gap_min_wc <= r.gap_min_rss + EPS
        assert r.gap_min_rss <= r.nominal_gap + EPS
        assert r.nominal_gap <= r.gap_max_rss + EPS
        assert r.gap_max_rss <= r.gap_max_wc + EPS

    def test_pct_wc_dim_a(self):
        """Dim A: 0.050/0.115 * 100 ≈ 43.478%"""
        contrib_a = next(c for c in self.result.contributors if c.name == "A_slot_width")
        assert abs(contrib_a.pct_wc - (0.050 / 0.115 * 100)) < 1e-4

    def test_to_dict_shape(self):
        d = self.result.to_dict()
        assert d["ok"] is True
        assert "worst_case" in d
        assert "rss" in d
        assert "contributors" in d
        assert len(d["contributors"]) == 3
        assert "disclaimer" in d

    def test_assumed_dims_empty(self):
        assert self.result.assumed_dims == []

    def test_disclaimer_present(self):
        assert "Preliminary" in self.result.disclaimer


class TestEdgeCases:
    """Edge cases: single dim, zero tol, assumed flag, equal tols."""

    def test_single_dim(self):
        """Single dim: RSS == WC (n=1 special case)."""
        r = stackup_analyze([{"name": "d", "nominal": 10.0, "tol": 0.1}])
        assert abs(r.total_tol_rss - r.total_tol_wc) < EPS
        assert abs(r.nominal_gap - 10.0) < EPS

    def test_zero_tol(self):
        """Zero tolerance contributes nothing."""
        r = stackup_analyze([
            {"name": "d1", "nominal": 5.0, "tol": 0.0},
            {"name": "d2", "nominal": 5.0, "tol": 0.1},
        ])
        assert abs(r.total_tol_wc - 0.1) < EPS
        assert abs(r.total_tol_rss - 0.1) < EPS

    def test_assumed_flag_propagates(self):
        """Dims with assumed=True appear in assumed_dims list."""
        r = stackup_analyze([
            {"name": "d1", "nominal": 5.0, "tol": 0.05, "assumed": True},
            {"name": "d2", "nominal": 5.0, "tol": 0.05},
        ])
        assert "d1" in r.assumed_dims
        assert "d2" not in r.assumed_dims

    def test_equal_tolerances_n_dims(self):
        """For n equal tols: WC = n*t, RSS = sqrt(n)*t."""
        t = 0.1
        n = 4
        dims = [{"name": f"d{i}", "nominal": 1.0, "tol": t} for i in range(n)]
        r = stackup_analyze(dims)
        assert abs(r.total_tol_wc - n * t) < EPS
        assert abs(r.total_tol_rss - math.sqrt(n) * t) < EPS

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            stackup_analyze([])

    def test_negative_tol_raises(self):
        with pytest.raises(ValueError, match="tol.*≥ 0"):
            stackup_analyze([{"name": "d", "nominal": 5.0, "tol": -0.1}])

    def test_missing_nominal_raises(self):
        with pytest.raises(ValueError, match="nominal"):
            stackup_analyze([{"name": "d", "tol": 0.1}])
