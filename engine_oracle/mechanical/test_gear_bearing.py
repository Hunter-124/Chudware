"""Tests for engine_oracle.mechanical — AGMA gear check + ISO 281 bearing life (T-BL18).

Style mirrors engine_oracle/structural/test_checks.py.
"""

from __future__ import annotations

import pytest

from ..mechanical.gear_check import (
    DISCLAIMER,
    GearCheckResult,
    gear_check,
)
from ..mechanical.bearing_life import (
    BearingLifeResult,
    bearing_life,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_gear(**kwargs):
    """Run gear_check with the hand-calculated validation case (Shigley's Ch. 14 method).

    Not a verbatim textbook example: the inputs are a hand-worked case computed
    step-by-step from the cited published equations (Shigley's 10th ed.
    Eqs. 14-15/14-16); full arithmetic is shown in each test docstring so the
    expected literals are independently checkable in one minute (T-D3/T-D5
    byte-exact-literal discipline).
    """
    defaults = dict(
        Wt=1000.0,
        module=4.0,
        face_width=50.0,
        pitch_diameter=100.0,
        J=0.36,
        I=0.107,
        Ko=1.25,
        Kv=1.52,
        Ks=1.0,
        Km=1.8,
        Cf=1.0,
    )
    defaults.update(kwargs)
    return gear_check(**defaults)


def _make_bearing(**kwargs):
    """Run bearing_life with the hand-calculated validation case (ISO 281 / Shigley's Ch. 11).

    Hand-worked case from the cited published life equation (ISO 281:2007;
    Shigley's 10th ed. Eq. 11-1); C/P = 3.75 is chosen so the expected values
    are exact decimals (3.75^3 = 52.734375), checkable by hand.
    """
    defaults = dict(
        C=30000.0,
        Fr=8000.0,
        Fa=0.0,
        n_rpm=1200.0,
        bearing_type="ball",
    )
    defaults.update(kwargs)
    return bearing_life(**defaults)


# ===========================================================================
# TestGearCheckDisclaimer
# ===========================================================================

class TestGearCheckDisclaimer:
    """DISCLAIMER constant is byte-exact and appears in every GearCheckResult."""

    EXPECTED = (
        "Preliminary engineering estimate — not a certified analysis. "
        "Verify with a qualified mechanical engineer before use in safety-critical applications."
    )

    def test_disclaimer_constant_byte_exact(self):
        assert DISCLAIMER == self.EXPECTED

    def test_gear_result_carries_disclaimer(self):
        r = _make_gear()
        assert r.disclaimer == self.EXPECTED

    def test_gear_result_disclaimer_is_module_constant(self):
        r = _make_gear()
        assert r.disclaimer is DISCLAIMER

    def test_gear_to_dict_includes_disclaimer(self):
        d = _make_gear().to_dict()
        assert "disclaimer" in d
        assert d["disclaimer"] == self.EXPECTED


# ===========================================================================
# TestGearCheckCalc
# ===========================================================================

class TestGearCheckCalc:
    """Numeric validation — hand calc from Shigley's 10th ed. Eqs. 14-15/14-16."""

    def test_sigma_bending(self):
        """Shigley's Eq. 14-15 (SI): sigma_b = Wt*Ko*Kv*Ks * (1/(b*mt)) * (Km*KB/J).

        sigma_b = 1000 * 1.25 * 1.52 * 1.0 * (1.8 * 1.0 / 0.36) / (50 * 4)
                = 1000 * 1.25 * 1.52 * 5.0 / 200      (KB = 1.0, solid gear)
                = 9500 / 200
                = 47.5 MPa                            (exact)
        """
        r = _make_gear()
        assert r.sigma_bending_MPa == pytest.approx(47.5, rel=1e-9)

    def test_sigma_contact(self):
        """Shigley's Eq. 14-16: sigma_c = Cp * sqrt(Wt*Ko*Kv*Ks * Km/(dp*b) * Cf/I).

        sigma_c = 191 * sqrt(1000 * 1.25 * 1.52 * 1.0 * 1.8 * 1.0 / (100 * 50 * 0.107))
                = 191 * sqrt(3420 / 535)
                = 191 * sqrt(6.392523364485981...)
                = 482.9137033257734 MPa   (Cp = 191 sqrt(MPa), steel/steel, Table 14-8)
        """
        r = _make_gear()
        assert r.sigma_contact_MPa == pytest.approx(482.9137033257734, rel=1e-9)

    def test_sf_none_when_no_allowables(self):
        r = _make_gear()
        assert r.SF_bending is None
        assert r.SF_contact is None

    def test_sf_bending_computed(self):
        # With allowable bending stress of 200 MPa -> SF_b = 200/47.5 ≈ 4.21
        r = _make_gear(allowable_bending_stress=200.0)
        assert r.SF_bending == pytest.approx(200.0 / 47.5, rel=0.01)
        assert r.SF_contact is None

    def test_sf_contact_computed(self):
        # With allowable contact stress of 1000 MPa -> SF_c = 1000/482.9137... ≈ 2.0708
        r = _make_gear(allowable_contact_stress=1000.0)
        assert r.SF_contact == pytest.approx(1000.0 / 482.9137033257734, rel=1e-9)
        assert r.SF_bending is None

    def test_both_sf_computed(self):
        r = _make_gear(allowable_bending_stress=200.0, allowable_contact_stress=1000.0)
        assert r.SF_bending is not None
        assert r.SF_contact is not None

    def test_to_dict_keys(self):
        r = _make_gear(allowable_bending_stress=200.0, allowable_contact_stress=1000.0)
        d = r.to_dict()
        assert set(d.keys()) == {
            "sigma_bending_MPa", "sigma_contact_MPa",
            "SF_bending", "SF_contact", "disclaimer",
        }

    def test_result_type(self):
        r = _make_gear()
        assert isinstance(r, GearCheckResult)

    def test_KB_affects_bending_only(self):
        """KB (rim-thickness) is a bending-equation factor (Eq. 14-15): doubling KB
        doubles sigma_b and leaves sigma_c untouched."""
        r1 = _make_gear()
        r2 = _make_gear(KB=2.0)
        assert r2.sigma_bending_MPa == pytest.approx(2.0 * r1.sigma_bending_MPa, rel=1e-9)
        assert r2.sigma_contact_MPa == pytest.approx(r1.sigma_contact_MPa, rel=1e-9)

    def test_Cf_affects_contact_only(self):
        """Cf (surface condition) is a contact-equation factor (Eq. 14-16): raising Cf
        scales sigma_c by sqrt(Cf) and leaves sigma_b untouched."""
        r1 = _make_gear()
        r2 = _make_gear(Cf=4.0)
        assert r2.sigma_bending_MPa == pytest.approx(r1.sigma_bending_MPa, rel=1e-9)
        assert r2.sigma_contact_MPa == pytest.approx(2.0 * r1.sigma_contact_MPa, rel=1e-9)

    def test_default_cp_is_steel_steel(self):
        """Default Cp=191 MPa^0.5 (Shigley's Table 14-8, steel/steel)."""
        r1 = _make_gear()
        r2 = _make_gear(Cp=191.0)
        assert r1.sigma_contact_MPa == pytest.approx(r2.sigma_contact_MPa, rel=1e-9)

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            gear_check(Wt=0, module=4, face_width=50, pitch_diameter=100, J=0.36, I=0.107)
        with pytest.raises(ValueError):
            gear_check(Wt=1000, module=0, face_width=50, pitch_diameter=100, J=0.36, I=0.107)
        with pytest.raises(ValueError):
            gear_check(Wt=1000, module=4, face_width=0, pitch_diameter=100, J=0.36, I=0.107)
        with pytest.raises(ValueError):
            gear_check(Wt=1000, module=4, face_width=50, pitch_diameter=0, J=0.36, I=0.107)
        with pytest.raises(ValueError):
            gear_check(Wt=1000, module=4, face_width=50, pitch_diameter=100, J=0, I=0.107)
        with pytest.raises(ValueError):
            gear_check(Wt=1000, module=4, face_width=50, pitch_diameter=100, J=0.36, I=0)


# ===========================================================================
# TestBearingLifeDisclaimer
# ===========================================================================

class TestBearingLifeDisclaimer:
    """DISCLAIMER constant is byte-exact and appears in every BearingLifeResult."""

    EXPECTED = (
        "Preliminary engineering estimate — not a certified analysis. "
        "Verify with a qualified mechanical engineer before use in safety-critical applications."
    )

    def test_disclaimer_constant_matches_gear_module(self):
        """bearing_life imports DISCLAIMER from gear_check — must be identical."""
        from ..mechanical.bearing_life import DISCLAIMER as BL_DISC
        assert BL_DISC == self.EXPECTED

    def test_bearing_result_carries_disclaimer(self):
        r = _make_bearing()
        assert r.disclaimer == self.EXPECTED

    def test_bearing_to_dict_includes_disclaimer(self):
        d = _make_bearing().to_dict()
        assert "disclaimer" in d
        assert d["disclaimer"] == self.EXPECTED


# ===========================================================================
# TestBearingLifeCalc
# ===========================================================================

class TestBearingLifeCalc:
    """Numeric validation — hand calc from ISO 281:2007 / Shigley's 10th ed. Eq. 11-1."""

    def test_L10_million_rev(self):
        """L10 = (C/P)^p (ISO 281 basic rating life, millions of revolutions).

        P   = 1.0*8000 + 0.0*0 = 8000 N
        L10 = (30000/8000)^3 = 3.75^3 = 52.734375 Mrev   (exact decimal)
        """
        r = _make_bearing()
        assert r.L10_million_rev == pytest.approx(52.734375, rel=1e-9)

    def test_L10h_hours(self):
        """L10h = 10^6 / (60 n) * (C/P)^p.

        L10h = 10^6 / (60 * 1200) * 52.734375
             = 52734375 / 72000
             = 732.421875 h                              (exact decimal)
        """
        r = _make_bearing()
        assert r.L10h_hours == pytest.approx(732.421875, rel=1e-9)

    def test_p_equiv(self):
        r = _make_bearing()
        assert r.P_equiv_N == pytest.approx(8000.0, rel=1e-9)

    def test_p_exponent_ball(self):
        r = _make_bearing(bearing_type="ball")
        assert r.p_exponent == pytest.approx(3.0)

    def test_p_exponent_roller(self):
        r = _make_bearing(bearing_type="roller")
        assert r.p_exponent == pytest.approx(10.0 / 3.0)

    def test_roller_life_longer_than_ball(self):
        """Roller exponent 10/3 > 3 gives longer life when C/P > 1."""
        r_ball = bearing_life(C=30000, Fr=8000, bearing_type="ball")
        r_roller = bearing_life(C=30000, Fr=8000, bearing_type="roller")
        assert r_roller.L10_million_rev > r_ball.L10_million_rev

    def test_result_type(self):
        r = _make_bearing()
        assert isinstance(r, BearingLifeResult)

    def test_to_dict_keys(self):
        r = _make_bearing(bearing_designation="6205")
        d = r.to_dict()
        assert set(d.keys()) == {
            "L10_million_rev", "L10h_hours", "P_equiv_N",
            "p_exponent", "bearing_designation", "disclaimer",
        }

    def test_bearing_designation_passthrough(self):
        r = _make_bearing(bearing_designation="6205")
        assert r.bearing_designation == "6205"
        assert r.to_dict()["bearing_designation"] == "6205"

    def test_bearing_designation_empty_by_default(self):
        r = _make_bearing()
        assert r.bearing_designation == ""

    def test_combined_loading(self):
        """With axial load and non-zero Y factor, P increases."""
        r_pure = bearing_life(C=30000, Fr=8000, Fa=0, X=1.0, Y=0.0)
        r_combined = bearing_life(C=30000, Fr=8000, Fa=2000, X=1.0, Y=0.56)
        # P_combined = 8000 + 0.56*2000 = 9120 > 8000, so L10 should be shorter
        assert r_combined.P_equiv_N == pytest.approx(9120.0, rel=1e-9)
        assert r_combined.L10_million_rev < r_pure.L10_million_rev


# ===========================================================================
# TestBearingLifeNoCRating
# ===========================================================================

class TestBearingLifeNoCRating:
    """Catalog geometry (bore/od/width) alone does not provide C; C is always required."""

    def test_zero_C_raises(self):
        with pytest.raises(ValueError, match="Dynamic load rating C must be positive"):
            bearing_life(C=0, Fr=8000)

    def test_negative_C_raises(self):
        with pytest.raises(ValueError, match="Dynamic load rating C must be positive"):
            bearing_life(C=-5000, Fr=8000)

    def test_zero_load_raises(self):
        with pytest.raises(ValueError):
            bearing_life(C=30000, Fr=0, Fa=0)

    def test_bearing_designation_does_not_provide_C(self):
        """Passing a bearing designation but no C must still raise if C<=0."""
        with pytest.raises(ValueError):
            bearing_life(C=0, Fr=8000, bearing_designation="6205")

    def test_bearing_designation_accepted_with_valid_C(self):
        """bearing_designation is accepted when C is supplied explicitly."""
        r = bearing_life(C=30000, Fr=8000, bearing_designation="6205")
        assert r.bearing_designation == "6205"
        assert r.L10_million_rev > 0

    def test_invalid_bearing_type_raises(self):
        with pytest.raises(ValueError, match="bearing_type"):
            bearing_life(C=30000, Fr=8000, bearing_type="tapered")
