"""Tests for T-D3: section loaders and D/C ratio checks.

Hand-calc validation (W8x31, A992 steel — the required DoD case)
-----------------------------------------------------------------
W8x31 properties (from ``aisc_shapes_curated.json``, cited to public-domain structural textbooks):
  A  = 5884 mm²
  Sx = 4.506e5 mm³   (27.5 in³ × 16387.1 mm³/in³)

A992 steel: Fy = 344.7 MPa (50 ksi), φ = 0.90 (AISC LRFD)

Bending capacity at first yield (conservative — elastic, not plastic Mp):
  φMy = φ · Sx · Fy = 0.90 × 4.506e5 mm³ × 344.7 N/mm² = 139 783 920 N·mm ≈ 139.8 kN·m

80% demand:
  M_demand = 0.80 × 139.8 kN·m = 111.9 kN·m = 111 900 000 N·mm

Expected D/C ratio:
  D/C = 111 900 000 / 139 783 920 ≈ 0.800  → passed=True

This anchors the test against real section data (not a synthetic capacity) and is independent of
the production code path — the numbers come from the published Sx and the AISC LRFD formula.

Run::

    /home/hunter/Repositories/CAD/.venv-mcp/bin/python \\
        -m pytest engine_oracle/structural/test_checks.py -q
"""

from __future__ import annotations

import math

import pytest

from engine.structural.checks import (
    DISCLAIMER,
    CheckResult,
    check_member,
    dc_ratio_axial,
    dc_ratio_bending,
    dc_ratio_axial_wood,
    dc_ratio_bending_wood,
)
from engine.structural.sections import get_aisc_section, get_lumber_section


# ============================================================================ section loaders

class TestGetAiscSection:
    def test_w8x31_keys(self):
        """get_aisc_section('W8X31') returns a dict with all expected keys."""
        s = get_aisc_section("W8X31")
        for key in ("A", "d", "bf", "tf", "tw", "Ix", "Sx", "Zx", "Iy", "Sy", "Zy", "J", "Cw"):
            assert key in s, f"missing key {key!r}"

    def test_w8x31_case_insensitive(self):
        """Lookup is case-insensitive."""
        a = get_aisc_section("W8X31")
        b = get_aisc_section("w8x31")
        c = get_aisc_section("W8x31")
        assert a == b == c

    def test_w8x31_A(self):
        """W8x31 area within ±2% of published 5884 mm² (9.12 in²)."""
        s = get_aisc_section("W8X31")
        assert s["A"] == pytest.approx(5884, rel=0.02)

    def test_w8x31_Sx(self):
        """W8x31 Sx within ±2% of published 4.506e5 mm³ (27.5 in³)."""
        s = get_aisc_section("W8X31")
        assert s["Sx"] == pytest.approx(4.506e5, rel=0.02)

    def test_w8x31_Ix(self):
        """W8x31 Ix within ±2% of published 4.574e7 mm⁴ (110 in⁴)."""
        s = get_aisc_section("W8X31")
        assert s["Ix"] == pytest.approx(4.574e7, rel=0.02)

    def test_unknown_section_raises_key_error(self):
        with pytest.raises(KeyError, match="W99X999"):
            get_aisc_section("W99X999")


class TestGetLumberSection:
    def test_2x4_actual_dims(self):
        """2x4 has actual dims 38 mm × 89 mm (1.5 in × 3.5 in — dressed sizes per PS 20)."""
        s = get_lumber_section("2x4")
        assert s["b_actual_mm"] == pytest.approx(38, abs=1)
        assert s["d_actual_mm"] == pytest.approx(89, abs=1)

    def test_2x4_area(self):
        """2x4 area = 38 × 89 = 3382 mm²."""
        s = get_lumber_section("2x4")
        expected_A = s["b_actual_mm"] * s["d_actual_mm"]
        assert s["A"] == pytest.approx(expected_A, rel=0.01)

    def test_2x4_Sx(self):
        """2x4 Sx = b*d²/6 (strong axis)."""
        s = get_lumber_section("2x4")
        b, d = s["b_actual_mm"], s["d_actual_mm"]
        expected_Sx = b * d ** 2 / 6.0
        assert s["Sx"] == pytest.approx(expected_Sx, rel=0.01)

    def test_4x4_square(self):
        """4x4 is square: Sx == Sy (both axes identical)."""
        s = get_lumber_section("4x4")
        assert s["Sx"] == pytest.approx(s["Sy"], rel=0.01)
        assert s["b_actual_mm"] == pytest.approx(s["d_actual_mm"], abs=1)

    def test_6x6_keys(self):
        s = get_lumber_section("6x6")
        for key in ("b_actual_mm", "d_actual_mm", "A", "Ix", "Sx", "Iy", "Sy"):
            assert key in s

    def test_case_insensitive(self):
        assert get_lumber_section("2x4") == get_lumber_section("2X4")

    def test_unknown_size_raises_key_error(self):
        with pytest.raises(KeyError):
            get_lumber_section("99x99")


# ============================================================================ ratio helpers

class TestDcRatioAxial:
    def test_exact(self):
        """Half-capacity axial → D/C = 0.5."""
        # capacity = 0.9 × 1000 × 250 = 225 000 N; force = 112 500 N
        dc = dc_ratio_axial(112_500.0, 1000.0, 250.0, phi=0.9)
        assert dc == pytest.approx(0.5, rel=1e-6)

    def test_zero_area_guard(self):
        assert dc_ratio_axial(1000.0, 0.0, 250.0) == 0.0

    def test_tension_and_compression_equal(self):
        """Sign of force doesn't matter — absolute value used."""
        assert dc_ratio_axial(50000.0, 1000.0, 250.0) == pytest.approx(
            dc_ratio_axial(-50000.0, 1000.0, 250.0)
        )


class TestDcRatioBending:
    def test_exact(self):
        """M = φ·Sx·Fy → D/C = 1.0 exactly."""
        phi, Sx, Fy = 0.9, 4.506e5, 344.7
        M_cap = phi * Sx * Fy  # N·mm
        dc = dc_ratio_bending(M_cap, Sx, Fy, phi=phi)
        assert dc == pytest.approx(1.0, rel=1e-6)

    def test_zero_Sx_guard(self):
        assert dc_ratio_bending(1e6, 0.0, 250.0) == 0.0


# ============================================================================ check_member — DoD validation

class TestCheckMemberSteelW8x31:
    """Hand-calc validation case (required by the workstream review bar).

    W8x31 A992: φMy = 0.90 × 4.506e5 mm³ × 344.7 MPa = 139 783 920 N·mm ≈ 139.8 kN·m
    Demand at 80%: M = 0.80 × 139 783 920 = 111 827 136 N·mm

    Expected: dc_ratio ≈ 0.80, passed=True, governing_limit='bending_yield_LRFD'
    """
    _PHI = 0.90
    _FY = 344.7   # A992 Fy in MPa

    @pytest.fixture
    def section(self):
        return get_aisc_section("W8X31")

    @pytest.fixture
    def capacity_nmm(self, section):
        return self._PHI * section["Sx"] * self._FY

    @pytest.fixture
    def member_at_80pct(self, capacity_nmm):
        return {
            "element_id": "b1",
            "combo": "DEAD_LIVE",
            "axial_n": 0.0,                          # pure bending case
            "moment_nmm": 0.80 * capacity_nmm,
            "shear_n": 0.0,
            "deflection_mm": 0.0,
        }

    def test_dc_ratio_approx_0_8(self, section, member_at_80pct):
        cr = check_member(member_at_80pct, section, "steel_a992")
        assert cr.dc_ratio == pytest.approx(0.80, rel=0.005), (
            f"D/C = {cr.dc_ratio:.4f}, expected ≈ 0.80. "
            f"Hand-calc: φMy = {self._PHI} × {section['Sx']:.0f} mm³ × {self._FY} MPa "
            f"= {self._PHI * section['Sx'] * self._FY:.0f} N·mm"
        )

    def test_passed_true(self, section, member_at_80pct):
        cr = check_member(member_at_80pct, section, "steel_a992")
        assert cr.passed is True

    def test_governing_limit_bending(self, section, member_at_80pct):
        cr = check_member(member_at_80pct, section, "steel_a992")
        assert cr.governing_limit == "bending_yield_LRFD"

    def test_disclaimer_exact(self, section, member_at_80pct):
        cr = check_member(member_at_80pct, section, "steel_a992")
        assert cr.disclaimer == DISCLAIMER

    def test_over_capacity_fails(self, section, capacity_nmm):
        member_over = {
            "element_id": "b1", "combo": "DEAD_LIVE",
            "axial_n": 0.0,
            "moment_nmm": 1.05 * capacity_nmm,  # 5% over capacity
            "shear_n": 0.0, "deflection_mm": 0.0,
        }
        cr = check_member(member_over, section, "steel_a992")
        assert cr.dc_ratio > 1.0
        assert cr.passed is False

    def test_capacity_absolute_value(self, section):
        """Pin absolute φMy against the published hand-calc value (independent of production code).

        Published W8x31 props: Sx = 27.5 in³ = 27.5 × 16387.1 = 450 645 mm³ ≈ 4.506e5 mm³
        A992 Fy = 50 ksi = 344.7 MPa; φ = 0.90 (AISC LRFD §F2)
        φMy = 0.90 × 4.506e5 × 344.7 = 139 801 620 N·mm ≈ 139.8 kN·m

        This test feeds the literal 111 900 000 N·mm demand (= 80% × 139.875 kN·m, rounded from
        the docstring) and asserts D/C ≈ 0.80 independently — so a units bug (e.g. Sx in in³)
        would produce a wildly wrong ratio, not a tautological 0.80.
        """
        # Literal demand from the docstring hand-calc, not derived from section["Sx"] at runtime
        _LITERAL_DEMAND_NMM = 111_900_000.0   # N·mm (≈ 80% × 139.875 kN·m)
        _EXPECTED_CAPACITY_NMM = 139_800_000.0  # N·mm (published φMy rounded to 0.1 kN·m)

        # Capacity pin: production code must give ≈ 139.8 kN·m for this section
        computed_capacity = self._PHI * section["Sx"] * self._FY
        assert computed_capacity == pytest.approx(_EXPECTED_CAPACITY_NMM, rel=0.01), (
            f"φMy = {computed_capacity/1e6:.3f} kN·m, expected ≈ 139.8 kN·m. "
            "Possible units bug in Sx (should be mm³, not in³)."
        )

        # D/C from literal demand
        member_literal = {
            "element_id": "b1", "combo": "DEAD_LIVE",
            "axial_n": 0.0,
            "moment_nmm": _LITERAL_DEMAND_NMM,
            "shear_n": 0.0, "deflection_mm": 0.0,
        }
        cr = check_member(member_literal, section, "steel_a992")
        assert cr.dc_ratio == pytest.approx(0.80, rel=0.01), (
            f"D/C = {cr.dc_ratio:.4f} from literal demand {_LITERAL_DEMAND_NMM/1e6:.2f} kN·m; "
            f"expected ≈ 0.80 (hand-calc φMy = {_EXPECTED_CAPACITY_NMM/1e6:.1f} kN·m)"
        )

    def test_a36_lower_capacity_than_a992(self, section):
        """A36 (248 MPa) → smaller φMy → higher D/C for the same demand."""
        same_moment = {
            "element_id": "b1", "combo": "DEAD_LIVE",
            "axial_n": 0.0, "moment_nmm": 1e8,
            "shear_n": 0.0, "deflection_mm": 0.0,
        }
        cr_a36 = check_member(same_moment, section, "steel_a36")
        cr_a992 = check_member(same_moment, section, "steel_a992")
        assert cr_a36.dc_ratio > cr_a992.dc_ratio


class TestCheckMemberAxialSteel:
    """Axial yield governs when moment = 0."""

    def test_axial_governs(self):
        section = get_aisc_section("W8X31")
        # Apply only axial (compression); zero moment
        # capacity = 0.9 × A × Fy_A36 = 0.9 × 5884 × 248 ≈ 1 313 251 N
        force_n = 0.60 * (0.9 * section["A"] * 248.0)  # 60% of capacity
        member = {
            "element_id": "c1", "combo": "DEAD_ONLY",
            "axial_n": -force_n,   # compression (negative = compression per contract)
            "moment_nmm": 0.0,
            "shear_n": 0.0, "deflection_mm": 0.0,
        }
        cr = check_member(member, section, "steel_a36")
        assert cr.dc_ratio == pytest.approx(0.60, rel=0.01)
        assert cr.governing_limit == "axial_yield_LRFD"
        assert cr.passed is True


class TestCheckMemberWood:
    """Wood NDS ASD checks — 2x10 Douglas Fir-Larch No.2."""

    @pytest.fixture
    def section(self):
        return get_lumber_section("2x10")

    def test_bending_at_50pct_asd(self, section):
        """50% of allowable bending stress → D/C = 0.50."""
        Fb = 6.205   # MPa, DF-No.2
        M_50pct = 0.50 * Fb * section["Sx"]  # N·mm
        member = {
            "element_id": "j1", "combo": "DEAD_LIVE",
            "axial_n": 0.0, "moment_nmm": M_50pct,
            "shear_n": 0.0, "deflection_mm": 0.0,
        }
        cr = check_member(member, section, "df_no2")
        assert cr.dc_ratio == pytest.approx(0.50, rel=0.01)
        assert cr.governing_limit == "bending_ASD_NDS"
        assert cr.passed is True

    def test_disclaimer_present_wood(self, section):
        member = {
            "element_id": "j1", "combo": "DEAD_LIVE",
            "axial_n": 0.0, "moment_nmm": 1.0,
            "shear_n": 0.0, "deflection_mm": 0.0,
        }
        cr = check_member(member, section, "cedar")
        assert cr.disclaimer == DISCLAIMER


class TestCheckMemberDisclaimerAlways:
    """disclaimer must be byte-exact in every check_member result."""

    def test_disclaimer_byte_exact(self):
        s = get_aisc_section("W8X31")
        cr = check_member(
            {"element_id": "x", "combo": "DEAD_ONLY",
             "axial_n": 0.0, "moment_nmm": 1.0,
             "shear_n": 0.0, "deflection_mm": 0.0},
            s, "steel_a36"
        )
        assert cr.disclaimer == (
            "Preliminary check — not sealed engineering. "
            "Verify with a licensed structural engineer before construction."
        )

    def test_to_dict_contains_disclaimer(self):
        s = get_aisc_section("W8X31")
        cr = check_member(
            {"element_id": "x", "combo": "DEAD_ONLY",
             "axial_n": 0.0, "moment_nmm": 0.0,
             "shear_n": 0.0, "deflection_mm": 0.0},
            s, "steel_a36"
        )
        d = cr.to_dict()
        assert "disclaimer" in d
        assert d["disclaimer"] == DISCLAIMER


# ============================================================================ MCP tool registration

class TestStructuralToolsRegistered:
    """struct_check and struct_analyze must both be registered on a FastMCP instance.

    This does NOT call the tools (that would require PyNite + a real doc); it only verifies the
    registration contract — i.e. that ``register_structural_tools`` installs both names on the
    server object so ``struct_check`` is visible to MCP clients.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def tool_names(cls):
        from mcp.server.fastmcp import FastMCP
        from mcp_server.structural_tools import register_structural_tools

        app = FastMCP("test-structural")
        register_structural_tools(app)
        # _tool_manager.list_tools() is synchronous in this version of mcp
        return {t.name for t in app._tool_manager.list_tools()}

    def test_struct_check_registered(self, tool_names):
        assert "struct_check" in tool_names, (
            f"struct_check not registered. Found: {sorted(tool_names)}"
        )

    def test_struct_analyze_registered(self, tool_names):
        assert "struct_analyze" in tool_names, (
            f"struct_analyze not registered. Found: {sorted(tool_names)}"
        )
