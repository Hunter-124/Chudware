"""Tests for the engineering calculator registry.

Each calculator test includes a hand-checked reference case with arithmetic
shown in comments. All numbers verified against cited references.

References used:
  Shigley = Shigley's Mechanical Engineering Design, 10th ed. (Budynas & Nisbett)
  Roark   = Roark's Formulas for Stress and Strain, 8th ed. (Young & Budynas)
  Bayer   = Bayer Material Science, "Snap-Fit Joints for Plastics" (1998)
"""
from __future__ import annotations

import math
import pytest

# Importing the package registers all calculators
from ...research import calcs as calcs
from ...research.calcs.registry import run_calc, list_calcs, calc_fail
from ...research.calcs.beam import beam_calc_raw, section_props
from ...research.calcs.bolted_joint import tensile_stress_area
from ...research.calcs.press_fit import press_fit_raw


# =========================================================================
# Registry smoke tests
# =========================================================================

class TestRegistry:
    def test_all_six_calcs_registered(self):
        registered = set(list_calcs().keys())
        # The six original calcs, plus the design-pipeline geometry helpers
        # (bend_allowance, draft_check). The registry is extensible — assert the
        # required calcs are a subset rather than an exact match.
        expected = {"beam", "bolted_joint", "thread_engagement",
                    "press_fit", "wall_thickness", "snap_fit",
                    "bend_allowance", "draft_check"}
        assert expected <= registered, f"Missing: {expected - registered}"

    def test_run_calc_unknown_returns_fail(self):
        r = run_calc("nonexistent", {})
        assert r["ok"] is False
        assert r["reason"] == "not_found"

    def test_list_calcs_has_input_schema_and_prefill_hints(self):
        listing = list_calcs()
        for cid, info in listing.items():
            assert "input_schema" in info, f"{cid} missing input_schema"
            assert "prefill_hints" in info, f"{cid} missing prefill_hints"

    def test_all_registered_calcs_have_aicad_schema(self):
        """Spot-check each calc returns aicad.calc/1 on a valid minimal call."""
        results = {
            "beam": run_calc("beam", {
                "support": "cantilever", "load": "point", "section": "rect",
                "L_mm": 100, "E_mpa": 69000, "F_n": 100, "b_mm": 10, "h_mm": 20,
            }),
            "bolted_joint": run_calc("bolted_joint", {"d_mm": 6, "grade": "8.8"}),
            "thread_engagement": run_calc("thread_engagement", {
                "d_mm": 6, "grade": "8.8", "internal_material_id": "alu_6061",
            }),
            "press_fit": run_calc("press_fit", {
                "d_mm": 50, "d_o_mm": 80, "interference_mm": 0.05,
            }),
            "wall_thickness": run_calc("wall_thickness", {"process": "fdm_3dp"}),
            "snap_fit": run_calc("snap_fit", {
                "L_mm": 20, "h_mm": 2, "b_mm": 5, "delta_mm": 1.5,
                "material_id": "abs",
            }),
        }
        for cid, r in results.items():
            assert r.get("schema") == "aicad.calc/1", (
                f"{cid}: schema not aicad.calc/1 — got {r}"
            )
            assert r.get("ok") is True, f"{cid}: ok=False — {r}"


# =========================================================================
# Beam calculator
# =========================================================================

class TestBeamSectionProps:
    """Reference: Shigley §A-18 Table A-18 (Area moments of inertia)."""

    def test_rect_I_and_c(self):
        # b=10, h=20: I = 10×20³/12 = 6666.67, c = 10
        I, c = section_props("rect", b_mm=10, h_mm=20)
        assert abs(I - 10 * 20**3 / 12) < 1e-6
        assert abs(c - 10.0) < 1e-9

    def test_round_I_and_c(self):
        # d=20: I = π×20⁴/64 = π×160000/64 = 7853.98, c = 10
        I, c = section_props("round", d_mm=20)
        assert abs(I - math.pi * 20**4 / 64) < 1e-6
        assert abs(c - 10.0) < 1e-9

    def test_tube_I_and_c(self):
        # D=30, d=20: I = π(30⁴-20⁴)/64 = π(810000-160000)/64 = π×650000/64 = 31906.8
        I, c = section_props("tube", D_mm=30, d_mm=20)
        expected_I = math.pi * (30**4 - 20**4) / 64
        assert abs(I - expected_I) < 1e-3
        assert abs(c - 15.0) < 1e-9

    def test_invalid_section_raises(self):
        with pytest.raises(ValueError, match="unknown section"):
            section_props("hex", b_mm=10, h_mm=10)


class TestBeamDeflection:
    """Reference: Shigley §3-4 deflection formulas; Roark Table 3-1."""

    def test_cantilever_point_deflection(self):
        # Reference: Shigley §3-4, Eq. for cantilever end-load.
        # Steel bar: b=10, h=20, L=200, E=200000, F=500
        # I = 10×20³/12 = 6666.67 mm⁴
        # δ = F·L³/(3·E·I) = 500×8e6/(3×200000×6666.67)
        #   = 4e9 / 4e9 = 1.0 mm
        I = 10 * 20**3 / 12          # 6666.67
        delta_expected = 500 * 200**3 / (3 * 200000 * I)  # 1.0 mm
        r = beam_calc_raw("cantilever", "point", "rect", 200, 200000,
                          F_n=500, b_mm=10, h_mm=20)
        assert abs(r["max_deflection_mm"] - delta_expected) < 1e-6
        # M_max = F·L = 500×200 = 100000 N·mm
        assert abs(r["max_moment_nmm"] - 100000) < 1e-6

    def test_cantilever_udl_deflection(self):
        # Reference: Roark Table 3-1, case 2 (cantilever, UDL).
        # w=2 N/mm, L=100, rect 20×10 (b=20, h=10), E=69000 (aluminium)
        # I = 20×10³/12 = 1666.67 mm⁴
        # δ = w·L⁴/(8·E·I) = 2×1e8/(8×69000×1666.67)
        #   = 2e8 / 9.2e8 = 0.21739 mm
        I = 20 * 10**3 / 12
        delta_expected = 2 * 100**4 / (8 * 69000 * I)
        r = beam_calc_raw("cantilever", "udl", "rect", 100, 69000,
                          w_n_mm=2, b_mm=20, h_mm=10)
        assert abs(r["max_deflection_mm"] - delta_expected) < 1e-8

    def test_simply_supported_point_deflection(self):
        # Reference: Shigley §3-4, mid-span point load.
        # Round bar d=25, L=500, E=200000, F=1000
        # I = π×25⁴/64 = 19174.76 mm⁴
        # δ = F·L³/(48·E·I) = 1000×1.25e8/(48×200000×19174.76)
        I = math.pi * 25**4 / 64
        delta_expected = 1000 * 500**3 / (48 * 200000 * I)
        r = beam_calc_raw("simply_supported", "point", "round", 500, 200000,
                          F_n=1000, d_mm=25)
        assert abs(r["max_deflection_mm"] - delta_expected) < 1e-8

    def test_simply_supported_udl_deflection(self):
        # Reference: Shigley §3-4; Roark Table 3-1, case 6.
        # Tube D=40 d=30, L=400, E=200000, w=5 N/mm
        # I = π(40⁴-30⁴)/64 = π(2560000-810000)/64 = 85667.5 mm⁴
        I = math.pi * (40**4 - 30**4) / 64
        delta_expected = 5 * 5 * 400**4 / (384 * 200000 * I)
        r = beam_calc_raw("simply_supported", "udl", "tube", 400, 200000,
                          w_n_mm=5, D_mm=40, d_mm=30)
        assert abs(r["max_deflection_mm"] - delta_expected) < 1e-6

    def test_beam_stress_cantilever_point(self):
        # σ = M·c/I = (F·L)·(h/2)/I  for rect section
        # F=500, L=200, h=20: M=100000, c=10
        # I=6666.67: σ = 100000×10/6666.67 = 150 MPa
        r = beam_calc_raw("cantilever", "point", "rect", 200, 200000,
                          F_n=500, b_mm=10, h_mm=20)
        I = 10 * 20**3 / 12
        sigma_expected = 100000 * 10 / I   # 150 MPa
        assert abs(r["max_stress_mpa"] - sigma_expected) < 1e-6

    def test_beam_envelope_returns_aicad_schema(self):
        from ...research.calcs.beam import beam
        r = beam("cantilever", "point", "rect", 200, 200000,
                 F_n=500, b_mm=10, h_mm=20)
        assert r["schema"] == "aicad.calc/1"
        assert r["ok"] is True
        assert "max_deflection_mm" in r["answer"]

    def test_beam_envelope_records_prefilled(self):
        from ...research.calcs.beam import beam
        pf = {"L_mm": "doc_bbox z body=shaft"}
        r = beam("simply_supported", "udl", "round", 300, 69000,
                 w_n_mm=1, d_mm=15, prefilled=pf)
        assert r["inputs"]["prefilled"] == pf

    def test_beam_bad_section_returns_fail(self):
        from ...research.calcs.beam import beam
        r = beam("cantilever", "point", "hex", 100, 200000, F_n=100, side_mm=10)
        assert r["ok"] is False

    def test_beam_bad_support_returns_fail(self):
        from ...research.calcs.beam import beam
        r = beam("fixed_fixed", "point", "rect", 100, 200000,
                 F_n=100, b_mm=10, h_mm=10)
        assert r["ok"] is False


# =========================================================================
# Bolted joint calculator
# =========================================================================

class TestBoltedJoint:
    """Reference: Shigley §8-1..§8-5."""

    def test_tensile_stress_area_M6(self):
        # Shigley Eq. 8-2: A_t = (π/4)·(d − 0.9382·p)²
        # M6×1.0: A_t = (π/4)·(6 − 0.9382)² = (π/4)·(5.0618)² = 20.12 mm²
        # Tabulated (Shigley Table 8-1): 20.1 mm²  ✓
        A_t = tensile_stress_area(6, 1.0)
        assert abs(A_t - 20.1) < 0.1, f"A_t={A_t:.2f}, expected ~20.1"

    def test_tensile_stress_area_M8(self):
        # M8×1.25: A_t = (π/4)·(8 − 0.9382×1.25)² = (π/4)·(8 − 1.1728)²
        #   = (π/4)·(6.8272)² = 36.6 mm²
        # Tabulated (Shigley Table 8-1): 36.6 mm²  ✓
        A_t = tensile_stress_area(8, 1.25)
        assert abs(A_t - 36.6) < 0.1, f"A_t={A_t:.2f}, expected ~36.6"

    def test_tensile_stress_area_M10(self):
        # M10×1.5: A_t = (π/4)·(10 − 0.9382×1.5)² = (π/4)·(10 − 1.4073)²
        #   = (π/4)·(8.5927)² = 58.0 mm²
        # Tabulated (Shigley Table 8-1): 58.0 mm²  ✓
        A_t = tensile_stress_area(10, 1.5)
        assert abs(A_t - 58.0) < 0.1, f"A_t={A_t:.2f}, expected ~58.0"

    def test_recommended_torque_M6_88(self):
        # Shigley §8-5:
        # A_t = 20.12 mm², S_p(8.8) = 600 MPa
        # F_p = 20.12 × 600 = 12072 N
        # F_i = 0.75 × 12072 = 9054 N
        # T = K·F_i·d = 0.2×9054×6 = 10865 N·mm = 10.865 N·m
        from ...research.calcs.bolted_joint import bolted_joint
        r = bolted_joint(d_mm=6, grade="8.8")
        assert r["ok"] is True
        assert r["schema"] == "aicad.calc/1"
        # Check torque: expected ~10.8–11.0 N·m
        T = r["answer"]["recommended_torque_nm"]
        assert 10.0 < T < 12.0, f"T={T:.2f} N·m, expected ~10.8"
        # Check utilisation at 75%
        assert abs(r["answer"]["utilisation_pct"] - 75.0) < 0.1

    def test_applied_torque_M6_88(self):
        # T=12 N·m applied to M6×1 grade 8.8, K=0.2
        # F_i = T / (K·d) = 12000 / (0.2×6) = 10000 N
        # sigma_i = F_i / A_t = 10000 / 20.12 = 497 MPa
        # S_p = 600 MPa → no yielding
        # utilisation = 10000/12072 = 82.8%
        from ...research.calcs.bolted_joint import bolted_joint
        r = bolted_joint(d_mm=6, grade="8.8", T_nm=12.0)
        assert r["ok"] is True
        ans = r["answer"]
        assert abs(ans["F_i_n"] - 10000) < 10, f"F_i={ans['F_i_n']:.1f}, expected 10000"
        assert ans["utilisation_pct"] < 100.0  # not overstressed

    def test_unknown_grade_fails(self):
        from ...research.calcs.bolted_joint import bolted_joint
        r = bolted_joint(d_mm=6, grade="3.6")
        assert r["ok"] is False
        assert r["reason"] == "bad_query"


# =========================================================================
# Thread engagement calculator
# =========================================================================

class TestThreadEngagement:
    """Reference: Shigley §8-6 + §6-6.

    Work-order anchor: M6 into 6061-T6 → ~9 mm.
    Hand-derivation:
      A_t = (π/4)·(6 − 0.9382×1.0)² = 20.12 mm²
      S_p(8.8) = 600 MPa
      S_y(6061) = 276 MPa  (materials.json)
      τ_y = 0.577 × 276 = 159.25 MPa
      L_e = (20.12 × 600) / (0.5 × π × 6 × 159.25)
           = 12072 / (1500.8) = 8.04 mm
      L_e_rec = 8.04 × 1.1 = 8.85 mm  ≈ 9 mm  ✓
    """

    def test_M6_into_alu_6061_approx_9mm(self):
        from ...research.calcs.thread_engagement import thread_engagement
        r = thread_engagement(d_mm=6, grade="8.8",
                              internal_material_id="alu_6061")
        assert r["ok"] is True
        Le_rec = r["answer"]["recommended_engagement_mm"]
        # Work-order says ~9 mm; our derivation gives 8.85 mm ✓
        assert 8.0 < Le_rec < 10.0, (
            f"recommended engagement {Le_rec:.2f} mm, expected ~9 mm"
        )

    def test_M6_into_steel_shorter_than_alu(self):
        """Steel internal thread needs less engagement than aluminium."""
        from ...research.calcs.thread_engagement import thread_engagement
        r_alu = thread_engagement(6, "8.8", "alu_6061")
        r_steel = thread_engagement(6, "8.8", "steel_1018")
        assert r_alu["answer"]["recommended_engagement_mm"] > \
               r_steel["answer"]["recommended_engagement_mm"]

    def test_returns_aicad_schema(self):
        from ...research.calcs.thread_engagement import thread_engagement
        r = thread_engagement(8, "10.9", "steel_1018")
        assert r["schema"] == "aicad.calc/1"
        assert r["ok"] is True

    def test_unknown_material_fails(self):
        from ...research.calcs.thread_engagement import thread_engagement
        r = thread_engagement(6, "8.8", "unobtanium")
        assert r["ok"] is False


# =========================================================================
# Press fit calculator
# =========================================================================

class TestPressFit:
    """Reference: Shigley §3-16 (Lamé thick-wall equations, Eq. 3-56..3-59).

    Hand-check: steel hub (E=200000, nu=0.29) and solid steel shaft (E=200000, nu=0.29)
    d=50 mm, d_o=80 mm, diametral interference=0.05 mm.

    r = 25, c = 40, a = 0  (solid shaft)
    δ_r = 0.025 mm   (radial = diametral/2)
    B_o = (c²+r²)/(c²-r²) + nu_o = (1600+625)/(1600-625) + 0.29
         = 2225/975 + 0.29 = 2.282 + 0.29 = 2.572
    B_i = 1 - nu_i = 1 - 0.29 = 0.71  (solid shaft)
    p = δ_r / (r × (B_o/E_o + B_i/E_i))
      = 0.025 / (25 × (2.572/200000 + 0.71/200000))
      = 0.025 / (25 × 1.641e-5)
      = 0.025 / 4.1025e-4
      = 60.94 MPa
    """

    def test_radial_diametral_conversion_explicit(self):
        # delta_r must be interference_mm/2
        r = press_fit_raw(50, 80, 0, 0.05, 200000, 0.29, 200000, 0.29)
        assert abs(r["delta_r_mm"] - 0.025) < 1e-9

    def test_interface_pressure_steel_steel(self):
        # Hand-computed p ≈ 60.9 MPa (see class docstring)
        r = press_fit_raw(50, 80, 0, 0.05, 200000, 0.29, 200000, 0.29)
        assert abs(r["p_mpa"] - 60.94) < 1.0, (
            f"p={r['p_mpa']:.2f} MPa, expected ~60.9"
        )

    def test_interface_pressure_scales_with_interference(self):
        r1 = press_fit_raw(50, 80, 0, 0.05, 200000, 0.29, 200000, 0.29)
        r2 = press_fit_raw(50, 80, 0, 0.10, 200000, 0.29, 200000, 0.29)
        # Pressure should double for double interference (linear Lamé)
        assert abs(r2["p_mpa"] / r1["p_mpa"] - 2.0) < 0.01

    def test_press_fit_envelope(self):
        from ...research.calcs.press_fit import press_fit
        r = press_fit(50, 80, 0.05)
        assert r["schema"] == "aicad.calc/1"
        assert r["ok"] is True
        assert "interface_pressure_mpa" in r["answer"]

    def test_press_fit_with_engagement_length(self):
        from ...research.calcs.press_fit import press_fit
        r = press_fit(50, 80, 0.05, L_mm=30)
        assert "F_assembly_total_n" in r["answer"]
        # Total = per_mm × 30
        assert abs(r["answer"]["F_assembly_total_n"] -
                   r["answer"]["F_assembly_per_mm_n"] * 30) < 0.1

    def test_press_fit_notes_diametral_convention(self):
        from ...research.calcs.press_fit import press_fit
        r = press_fit(50, 80, 0.05)
        assert any("diametral" in n.lower() for n in r["notes"])

    def test_invalid_hub_diameter_fails(self):
        from ...research.calcs.press_fit import press_fit
        r = press_fit(50, 40, 0.05)  # d_o < d
        assert r["ok"] is False


# =========================================================================
# Wall thickness calculator
# =========================================================================

class TestWallThickness:
    """Reference: Kalpakjian §21; Gibson et al. AM Technologies §6; Bryce §4."""

    def test_machining_steel_aspect_3(self):
        # h=30, steel → aspect=3.0, min_wall=30/3=10 mm
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("machining", feature_height_mm=30,
                           material_family="steel")
        assert r["ok"] is True
        assert abs(r["answer"]["min_wall_mm"] - 10.0) < 0.01

    def test_machining_aluminium_aspect_5(self):
        # h=50, aluminium → aspect=5.0, min_wall=50/5=10 mm
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("machining", feature_height_mm=50,
                           material_family="aluminium")
        assert abs(r["answer"]["min_wall_mm"] - 10.0) < 0.01

    def test_fdm_default_nozzle(self):
        # nozzle=0.4 → 2×0.4=0.8 < 1.2 structural min → min_wall=1.2
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("fdm_3dp")
        assert abs(r["answer"]["min_wall_mm"] - 1.2) < 0.01

    def test_fdm_large_nozzle(self):
        # nozzle=0.8 → 2×0.8=1.6 > 1.2 → min_wall=1.6
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("fdm_3dp", nozzle_dia_mm=0.8)
        assert abs(r["answer"]["min_wall_mm"] - 1.6) < 0.01

    def test_injection_molding_abs(self):
        # ABS: min=1.2, max=3.5, nominal=2.0
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("injection_molding", material_family="abs")
        assert r["ok"] is True
        assert r["answer"]["min_wall_mm"] == 1.2
        assert r["answer"]["nominal_wall_mm"] == 2.0

    def test_source_kind_heuristic(self):
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("machining", feature_height_mm=20)
        assert r["source"]["kind"] == "heuristic"

    def test_unknown_process_fails(self):
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("casting")
        assert r["ok"] is False

    def test_machining_requires_height(self):
        from ...research.calcs.wall_thickness import wall_thickness
        r = wall_thickness("machining")
        assert r["ok"] is False


# =========================================================================
# Snap fit calculator
# =========================================================================

class TestSnapFit:
    """Reference: Shigley §4-1; Bayer "Snap-Fit Joints for Plastics" (1998).

    Hand-check (ABS: E=2300, Sy=40 MPa):
      L=20, h=2, b=5, δ=1.5 mm
      I = 5×2³/12 = 3.333 mm⁴
      ε_root = 1.5×h×δ/L² = 1.5×2×1.5/400 = 4.5/400 = 0.01125
      F_snap = 3·E·I·δ/L³ = 3×2300×3.333×1.5/8000 = 34500/8000 = 4.3125 N
      ε_y = 40/2300 = 0.01739
      ε_allow = 0.6×0.01739 = 0.01043
      ε_root (0.01125) > ε_allow (0.01043) → NOT viable
    """

    def test_snap_fit_root_strain(self):
        # ε_root = 1.5×2×1.5/400 = 0.01125
        from ...research.calcs.snap_fit import snap_fit
        r = snap_fit(L_mm=20, h_mm=2, b_mm=5, delta_mm=1.5, material_id="abs")
        assert r["ok"] is True
        assert abs(r["answer"]["epsilon_root"] - 0.01125) < 1e-5

    def test_snap_fit_force(self):
        # F = 3×2300×3.333×1.5/8000 = 4.313 N
        from ...research.calcs.snap_fit import snap_fit
        r = snap_fit(L_mm=20, h_mm=2, b_mm=5, delta_mm=1.5, material_id="abs")
        # I = 5×8/12 = 3.333
        I = 5 * 2**3 / 12
        F_expected = 3 * 2300 * I * 1.5 / 20**3
        assert abs(r["answer"]["F_snap_n"] - F_expected) < 0.01

    def test_snap_fit_not_viable_abs_thin_long(self):
        # The reference case: ε_root > ε_allow → not viable
        from ...research.calcs.snap_fit import snap_fit
        r = snap_fit(L_mm=20, h_mm=2, b_mm=5, delta_mm=1.5, material_id="abs")
        assert r["answer"]["viable"] is False

    def test_snap_fit_viable_longer_arm(self):
        # Longer arm → smaller strain → viable
        from ...research.calcs.snap_fit import snap_fit
        r = snap_fit(L_mm=40, h_mm=2, b_mm=5, delta_mm=1.5, material_id="abs")
        # ε_root = 1.5×2×1.5/1600 = 0.00281 < ε_allow ✓
        assert r["answer"]["viable"] is True

    def test_snap_fit_returns_aicad_schema(self):
        from ...research.calcs.snap_fit import snap_fit
        r = snap_fit(20, 2, 5, 1.5, "abs")
        assert r["schema"] == "aicad.calc/1"

    def test_snap_fit_unknown_material_fails(self):
        from ...research.calcs.snap_fit import snap_fit
        r = snap_fit(20, 2, 5, 1.5, "vibranium")
        assert r["ok"] is False


# =========================================================================
# Prefill module
# =========================================================================

class TestPrefill:
    """Prefill resolution against a fixture stub DocQuerier."""

    def _make_querier(self, diameter=25.0, bbox=None, material="alu_6061"):
        """Return a stub DocQuerier with fixed responses."""
        from services.agent.advisor.prefill import DocQuerier

        class StubQuerier:
            def cylinder_diameter(self, doc_id, face_ref):
                return diameter

            def bbox(self, doc_id, body_ref):
                return bbox or {"x_mm": 100.0, "y_mm": 50.0, "z_mm": 200.0}

            def wall_thickness(self, doc_id, face_ref):
                return 3.0

            def material_id(self, doc_id, body_ref):
                return material

        return StubQuerier()

    def test_press_fit_prefills_diameter(self):
        from services.agent.advisor.prefill import prefill_inputs
        q = self._make_querier(diameter=30.0)
        vals, prov = prefill_inputs("press_fit", "doc1", querier=q)
        # press_fit prefill_hints: d_mm -> cylinder_diameter
        assert "d_mm" in vals
        assert abs(vals["d_mm"] - 30.0) < 1e-6
        assert "d_mm" in prov
        # provenance string describes the doc query (e.g. "doc_eval_measure diameter face=auto")
        assert "diameter" in prov["d_mm"].lower()

    def test_beam_prefills_span_from_bbox(self):
        from services.agent.advisor.prefill import prefill_inputs
        q = self._make_querier(bbox={"x_mm": 50.0, "y_mm": 30.0, "z_mm": 300.0})
        vals, prov = prefill_inputs("beam", "doc1", querier=q)
        # beam prefill_hints: L_mm -> bbox_z
        assert "L_mm" in vals
        assert abs(vals["L_mm"] - 300.0) < 1e-6

    def test_user_inputs_not_overridden(self):
        from services.agent.advisor.prefill import prefill_inputs
        q = self._make_querier(diameter=30.0)
        # User provides d_mm=50 — prefill must not override it
        vals, prov = prefill_inputs(
            "press_fit", "doc1",
            querier=q,
            user_inputs={"d_mm": 50.0},
        )
        assert "d_mm" not in vals  # not in prefilled (user wins)

    def test_build_prefilled_inputs_merges_correctly(self):
        from services.agent.advisor.prefill import build_prefilled_inputs
        q = self._make_querier(diameter=30.0)
        merged, prov = build_prefilled_inputs(
            "press_fit", "doc1",
            user_inputs={"interference_mm": 0.05, "d_o_mm": 60.0},
            querier=q,
        )
        assert "d_mm" in merged         # from prefill
        assert "interference_mm" in merged  # from user
        assert prov is not None
        assert "d_mm" in prov

    def test_unknown_calc_returns_empty(self):
        from services.agent.advisor.prefill import prefill_inputs
        q = self._make_querier()
        vals, prov = prefill_inputs("nonexistent", "doc1", querier=q)
        assert vals == {}
        assert prov == {}


class TestBeltRoute:
    @staticmethod
    def pulley(x, y, radius, wrap="ccw"):
        return {"center_mm": [x, y], "pitch_radius_mm": radius, "wrap": wrap}

    @pytest.mark.parametrize("centers,radii,expected", [
        ([(0, 0), (100, 0)], [10, 10], 200 + 20 * math.pi),
        ([(0, 0), (100, 0)], [10, 20],
         2 * math.sqrt(100**2 - 10**2) + 30 * math.pi + 20 * math.asin(.1)),
        ([(0, 0), (100, 0), (50, 80)], [10, 10, 10],
         100 + 2 * math.hypot(50, 80) + 20 * math.pi),
    ])
    def test_open_tangents_and_convex_hull_arcs(self, centers, radii, expected):
        result = run_calc("belt_route", {"pulleys": [
            self.pulley(*center, radius) for center, radius in zip(centers, radii)]})
        assert result["ok"], result
        answer = result["answer"]
        assert answer["length_mm"] == pytest.approx(expected, rel=1e-12)
        assert answer["straight_length_mm"] + answer["arc_length_mm"] == pytest.approx(expected)

    def test_backside_wrap_and_reversed_travel_preserve_exact_tangency(self):
        pulleys = [self.pulley(0, 0, 10), self.pulley(100, 0, 10),
                   self.pulley(50, 0, 5, "cw")]
        # Two internal tangents meet a backside idler. This is not the
        # convex-hull perimeter and the idler's wrap subtracts from winding.
        expected = (100 + 2 * math.sqrt(50**2 - 15**2)
                    + 20 * math.pi + 30 * math.asin(.3))
        answer = run_calc("belt_route", {"pulleys": pulleys})["answer"]
        assert answer["length_mm"] == pytest.approx(expected, rel=1e-12)
        assert answer["wraps"][2]["angle_deg"] == pytest.approx(math.degrees(2 * math.asin(.3)))
        for span in answer["spans"]:
            a, b = span["start_mm"], span["end_mm"]
            tangent = [b[k] - a[k] for k in (0, 1)]
            assert math.hypot(*tangent) == pytest.approx(span["length_mm"])
            for endpoint, index in ((a, span["from_pulley"]), (b, span["to_pulley"])):
                circle = pulleys[index]
                radius = [endpoint[k] - circle["center_mm"][k] for k in (0, 1)]
                assert math.hypot(*radius) == pytest.approx(circle["pitch_radius_mm"])
                assert sum(radius[k] * tangent[k] for k in (0, 1)) == pytest.approx(0, abs=1e-10)
        reverse = [{**row, "wrap": "cw" if row["wrap"] == "ccw" else "ccw"}
                   for row in reversed(pulleys)]
        reversed_answer = run_calc("belt_route", {"pulleys": reverse})["answer"]
        assert reversed_answer["length_mm"] == pytest.approx(expected, rel=1e-12)
        assert reversed_answer["signed_wrap_rad"] == pytest.approx(-2 * math.pi)

    @pytest.mark.parametrize("idler_y", [-8, -5])
    def test_backside_route_cannot_cut_or_kiss_the_other_span(self, idler_y):
        result = run_calc("belt_route", {"pulleys": [
            self.pulley(0, 0, 10), self.pulley(100, 0, 10),
            self.pulley(50, idler_y, 5, "cw")]})
        assert not result["ok"]
        assert "answer" not in result

    def test_crossed_belt_is_not_reported_as_a_simple_planar_loop(self):
        result = run_calc("belt_route", {"pulleys": [
            self.pulley(0, 0, 10), self.pulley(100, 0, 10, "cw")]})
        assert not result["ok"]
        assert "answer" not in result

    def test_nested_circles_have_no_positive_tangent(self):
        result = run_calc("belt_route", {"pulleys": [
            self.pulley(0, 0, 10), self.pulley(5, 0, 20)]})
        assert not result["ok"]
        assert "answer" not in result

    @pytest.mark.parametrize("value", [True, float("nan")])
    def test_invalid_pitch_radius_cannot_become_a_length(self, value):
        result = run_calc("belt_route", {"pulleys": [
            self.pulley(0, 0, value), self.pulley(100, 0, 10)]})
        assert not result["ok"]
        assert "answer" not in result
