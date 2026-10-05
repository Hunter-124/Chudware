"""Independent hand-calculation validation of services.sim.vibration.

Reference tiers:
  PRECISE  (<1%)  — validated analytic reference, formula cited inline
  EXACT    (<0.01%) — same closed form, machine-exact expected

Every reference value below is derived INDEPENDENTLY in this file (literal
arithmetic shown in comments) — never read back from the module under test.

References:
  [1] Steinberg 2000, "Vibration Analysis for Electronic Equipment" 3e, Eq. 6.6.
  [2] Craig & Kurdila 2006, "Fundamentals of Structural Dynamics" 2e,
      Table 8.1 (cantilever beta_1*L = 1.8751), Sec. 12.3.
  [3] Harris & Piersol 2002, "Harris' Shock and Vibration Handbook" 5e, Ch.2 Eq. 2.41.

No C++ engine required — this module is pure Python/numpy.
"""
from __future__ import annotations

import math

import pytest

from ..sim.vibration import (
    _interp_psd,
    evaluate_vibration_spec,
    harmonic_response,
    lumped_mass_vector,
    miles_response,
    modal_participation,
    sdof_transmissibility,
)

PRECISE = 1.0    # % tolerance tier
EXACT = 0.01     # %


def _pct_err(actual: float, expected: float) -> float:
    return abs(actual - expected) / abs(expected) * 100.0


# ---------------------------------------------------------------------------
# SDOF building blocks
# ---------------------------------------------------------------------------

def _sdof_mode(fn_hz: float, mass_kg: float = 1.0):
    """A synthetic single-node, single-mode system with unit mode shape along Z.

    gamma = m * 1, gen_mass = m * 1^2  =>  gamma/m = 1 exactly — the classical
    SDOF base-excitation problem, so Miles/transmissibility references apply directly.
    """
    return {
        "freqs_hz": [fn_hz],
        "mode_shapes_flat": [0.0, 0.0, 1.0],  # 1 mode * 1 node * 3
        "n_nodes": 1,
        "total_mass_kg": mass_kg,
        "excitation_dir": [0.0, 0.0, 1.0],
    }


def test_transmissibility_at_resonance_equals_q():
    """[3] Eq. 2.41 at r=1: |T| = sqrt(1 + 4 zeta^2) / (2 zeta).

    zeta = 0.02: |T| = sqrt(1.0016) / 0.04 = 1.00079968.../0.04 = 25.01999...
    Literal expected: 25.0200 (and ~= Q = 25 for light damping).
    """
    t = sdof_transmissibility(100.0, [100.0], zeta=0.02)
    assert _pct_err(float(t[0]), 25.0200) < PRECISE


def test_transmissibility_static_limit():
    """[3] Eq. 2.41 at r->0: |T| -> 1 (base motion passes straight through)."""
    t = sdof_transmissibility(100.0, [0.1], zeta=0.02)
    assert _pct_err(float(t[0]), 1.0) < PRECISE


def test_transmissibility_isolation_region():
    """[3] Eq. 2.41 at r = 10, zeta = 0.02:
    num = 1 + (0.4)^2 = 1.16;  den = (1-100)^2 + 0.16 = 9801.16
    |T| = sqrt(1.16 / 9801.16) = sqrt(1.18353e-4) = 0.0108790  (literal)
    """
    t = sdof_transmissibility(10.0, [100.0], zeta=0.02)
    assert _pct_err(float(t[0]), 0.0108790) < PRECISE


# ---------------------------------------------------------------------------
# Miles' equation — SDOF textbook case
# ---------------------------------------------------------------------------

def test_miles_sdof_flat_psd():
    """[1] Eq. 6.6: GRMS = sqrt(pi/2 * fn * Q * W).

    fn = 100 Hz, zeta = 0.02 => Q = 25, W = 0.04 g^2/Hz (flat):
      pi/2 * 100 = 157.0796...; * 25 = 3926.990...; * 0.04 = 157.0796...
      GRMS = sqrt(157.0796) = 12.5332 g  (literal)
    """
    sys_ = _sdof_mode(100.0)
    out = miles_response(psd_breakpoints=[(10.0, 0.04), (2000.0, 0.04)],
                         zeta=0.02, **sys_)
    assert len(out["per_mode"]) == 1
    assert _pct_err(out["per_mode"][0]["grms"], 12.5332) < PRECISE
    assert _pct_err(out["total_grms"], 12.5332) < PRECISE
    assert out["per_mode"][0]["Q"] == pytest.approx(25.0)
    # Citation must be carried in the payload (honesty requirement).
    assert "Steinberg" in out["citation"]
    assert "limitations" in out


# ---------------------------------------------------------------------------
# Cantilever beam first mode — textbook natural frequency + Miles response
# ---------------------------------------------------------------------------

def test_cantilever_first_mode_miles():
    """Steel cantilever 50x50 mm, L = 1000 mm ([2] Table 8.1, beta_1*L = 1.8751):

      fn1 = (1.8751^2 / (2 pi L^2)) * sqrt(E I / (rho A))
      E = 200e9 Pa, I = 0.05^4/12 = 5.208333e-7 m^4, rho = 7850 kg/m^3,
      A = 2.5e-3 m^2, L = 1 m
      E I = 1.0416667e5;  rho A = 19.625;  E I/(rho A) = 5307.856
      sqrt(...) = 72.8551;  1.8751^2/(2 pi) = 3.51600/6.28319 = 0.559590
      fn1 = 40.769 Hz  (literal; matches SIM_AUDIT.md's ~40.8 Hz for the same part)

    Miles at that mode, Q = 25 (zeta = 0.02), flat W = 0.1 g^2/Hz:
      GRMS = sqrt(pi/2 * 40.769 * 25 * 0.1) = sqrt(160.113) = 12.6536 g  (literal)
    """
    # Independent hand computation of fn1 (kept in-test, never from the module):
    E, rho, L, b = 200e9, 7850.0, 1.0, 0.05
    I = b * b**3 / 12.0
    A = b * b
    fn1 = (1.8751**2 / (2.0 * math.pi * L**2)) * math.sqrt(E * I / (rho * A))
    assert _pct_err(fn1, 40.769) < PRECISE  # guard the hand-calc itself

    sys_ = _sdof_mode(fn1)
    out = miles_response(psd_breakpoints=[(10.0, 0.1), (2000.0, 0.1)],
                         zeta=0.02, **sys_)
    assert _pct_err(out["total_grms"], 12.6536) < PRECISE


# ---------------------------------------------------------------------------
# Harmonic sweep — resonance capture
# ---------------------------------------------------------------------------

def test_harmonic_sdof_peak_at_resonance():
    """SDOF harmonic sweep peaks at fn with amplification ~= Q ([3] Eq. 2.41).

    1 g base input, zeta = 0.02: peak response = 25.02 g at f = fn (literal;
    sweep includes the exact resonance point so no grid-resolution slack needed).
    """
    fn = 100.0
    sweep = [50.0, 90.0, 99.0, 100.0, 101.0, 110.0, 200.0]
    out = harmonic_response(sweep_hz=sweep, zeta=0.02, base_accel_g=1.0,
                            **_sdof_mode(fn))
    assert out["peak_freq_hz"] == pytest.approx(100.0)
    assert _pct_err(out["peak_accel_g"], 25.0200) < PRECISE
    assert out["combination"] == "SRSS"
    assert "Harris" in out["citation"]


def test_harmonic_skips_rigid_body_modes():
    """Free-free solves emit ~0 Hz rigid-body modes — they must not amplify."""
    out = harmonic_response(
        freqs_hz=[1e-6, 100.0],
        mode_shapes_flat=[0.0, 0.0, 1.0, 0.0, 0.0, 1.0],  # 2 modes * 1 node * 3
        n_nodes=1, total_mass_kg=1.0, excitation_dir=[0, 0, 1],
        sweep_hz=[100.0], zeta=0.02)
    assert len(out["per_mode"]) == 1
    assert out["per_mode"][0]["freq_hz"] == pytest.approx(100.0)


def test_harmonic_resonance_decreases_with_damping():
    system = _sdof_mode(100.0)
    lightly_damped = harmonic_response(
        sweep_hz=[100.0], zeta=0.02, base_accel_g=1.0, **system)
    more_damped = harmonic_response(
        sweep_hz=[100.0], zeta=0.04, base_accel_g=1.0, **system)
    ratio = lightly_damped["peak_accel_g"] / more_damped["peak_accel_g"]
    # Absolute base-excitation transmissibility includes sqrt(1 + (2*zeta*r)^2)
    # in the numerator, so the exact r=1 ratio is 1.995225..., not exactly 2.
    expected_ratio = (
        math.sqrt(1.0 + (2.0 * 0.02) ** 2) / (2.0 * 0.02)
        / (math.sqrt(1.0 + (2.0 * 0.04) ** 2) / (2.0 * 0.04))
    )
    assert ratio == pytest.approx(expected_ratio, rel=1e-12)


def test_default_worker_sweep_captures_exact_modal_resonance():
    from ..sim.multiphysics_rpc import _sim_freq_response

    frequency = 123.456
    result = _sim_freq_response({
        "freqs_hz": [frequency],
        "mode_shapes": [0.0, 0.0, 1.0],
        "n_nodes": 1,
        "total_mass_kg": 1.0,
        "excitation_dir": [0.0, 0.0, 1.0],
        "zeta": 0.02,
        "base_accel_g": 1.0,
    })
    assert result["ok"], result
    assert result["harmonic"]["peak_freq_hz"] == pytest.approx(frequency)
    assert result["harmonic"]["peak_accel_g"] == pytest.approx(25.019992, rel=1e-6)


@pytest.mark.parametrize("override", [
    {"zeta": 0.0},
    {"base_accel_g": 0.0},
    {"freqs_hz": [float("nan")]},
    {"excitation_dir": [0.0, 0.0, 0.0]},
])
def test_worker_frequency_response_rejects_nonfinite_or_zero_excitation(override):
    from ..sim.multiphysics_rpc import _sim_freq_response

    params = {
        "freqs_hz": [100.0],
        "mode_shapes": [0.0, 0.0, 1.0],
        "n_nodes": 1,
        "total_mass_kg": 1.0,
        "excitation_dir": [0.0, 0.0, 1.0],
        "sweep_hz": [100.0],
    }
    params.update(override)
    result = _sim_freq_response(params)
    assert result["ok"] is False
    assert "invalid frequency-response setup" in result["error"]


# ---------------------------------------------------------------------------
# Participation factors
# ---------------------------------------------------------------------------

def test_participation_orthogonal_direction_is_zero():
    """A mode purely in Z has zero participation for X base excitation ([2] Sec. 12.3)."""
    import numpy as np
    shapes = np.array([[[0.0, 0.0, 1.0]]])  # 1 mode, 1 node, along Z
    mass = np.array([2.0])
    gamma, gen_m = modal_participation(shapes, mass, [1.0, 0.0, 0.0])
    assert gamma[0] == pytest.approx(0.0)
    assert gen_m[0] == pytest.approx(2.0)


def test_lumped_mass_totals():
    m = lumped_mass_vector(10, 5.0)
    assert m.sum() == pytest.approx(5.0)
    assert m[0] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# PSD log-log interpolation
# ---------------------------------------------------------------------------

def test_psd_loglog_interpolation():
    """NAVMAT P-9492-style ramp: (20 Hz, 0.01) -> (80 Hz, 0.04) g^2/Hz.

    Log-log slope = ln(0.04/0.01)/ln(80/20) = ln4/ln4 = 1 (i.e. +3 dB/octave x2).
    W(40) = 0.01 * (40/20)^1 = 0.02 g^2/Hz  (literal, EXACT tier — same closed form).
    """
    bp = [(20.0, 0.01), (80.0, 0.04), (350.0, 0.04), (2000.0, 0.007)]
    assert _pct_err(_interp_psd(bp, 40.0), 0.02) < EXACT
    # Flat extrapolation outside the table (conservative):
    assert _interp_psd(bp, 5.0) == pytest.approx(0.01)
    assert _interp_psd(bp, 3000.0) == pytest.approx(0.007)
    # Flat mid-band:
    assert _interp_psd(bp, 200.0) == pytest.approx(0.04)


def test_psd_rejects_nonpositive():
    with pytest.raises(ValueError):
        _interp_psd([(0.0, 0.01)], 10.0)
    with pytest.raises(ValueError):
        _interp_psd([], 10.0)


def test_psd_rejects_duplicate_or_nonfinite_breakpoints():
    with pytest.raises(ValueError, match="unique"):
        _interp_psd([(20.0, 0.01), (20.0, 0.02)], 20.0)
    with pytest.raises(ValueError, match="finite"):
        _interp_psd([(20.0, float("inf"))], 20.0)


@pytest.mark.parametrize("zeta", [0.0, -0.01, 1.0, float("nan")])
def test_miles_rejects_invalid_damping(zeta):
    with pytest.raises(ValueError, match="zeta"):
        miles_response(
            psd_breakpoints=[(10.0, 0.04), (2000.0, 0.04)],
            zeta=zeta, **_sdof_mode(100.0))


# ---------------------------------------------------------------------------
# Spec verdict (DAG verify-node end condition)
# ---------------------------------------------------------------------------

def test_spec_verdict_pass_and_fail():
    harmonic = {"sweep_hz": [1.0], "peak_accel_g": 10.0}
    random = {"total_grms": 8.0}
    v = evaluate_vibration_spec({"max_peak_accel_g": 12.0, "max_grms": 9.0},
                                harmonic, random)
    assert v["passed"] is True
    v = evaluate_vibration_spec({"max_peak_accel_g": 5.0}, harmonic, random)
    assert v["passed"] is False


def test_spec_verdict_none_when_no_spec():
    assert evaluate_vibration_spec(None, {"peak_accel_g": 1.0}, None) is None
    assert evaluate_vibration_spec({}, {"peak_accel_g": 1.0}, None) is None


def test_spec_verdict_fails_closed_without_random_results():
    """A GRMS limit with no PSD run must FAIL (never silently pass)."""
    v = evaluate_vibration_spec({"max_grms": 9.0},
                                {"sweep_hz": [1.0], "peak_accel_g": 1.0}, None)
    assert v["passed"] is False
    assert "psd" in v["checks"][0]["reason"].lower()


def test_spec_verdict_unknown_keys_fail_closed():
    v = evaluate_vibration_spec({"max_wiggle": 3.0},
                                {"sweep_hz": [1.0], "peak_accel_g": 1.0}, None)
    assert v["passed"] is False


def test_spec_verdict_fails_closed_on_nonfinite_limit():
    verdict = evaluate_vibration_spec(
        {"max_peak_accel_g": float("nan")},
        {"sweep_hz": [100.0], "peak_accel_g": 1.0}, None)
    assert verdict["passed"] is False
    assert verdict["checks"][0]["limit"] is None
    assert "finite" in verdict["checks"][0]["reason"]
