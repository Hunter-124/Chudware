"""Vibration frequency-response solver — modal superposition (harmonic) + Miles' equation (random PSD).

T-BL17. Builds on the C++ modal solver's output (``sim_run_modal`` → natural frequencies +
unit-max-normalized mode shapes) — NO new solver dependency. Pure Python + numpy.

Method
------
Harmonic (sine sweep, base excitation):
    Each retained mode n is treated as an independent SDOF oscillator excited through its
    modal participation factor. The absolute-acceleration amplification of a base-excited
    SDOF is the classical transmissibility

        |T(r)| = sqrt( (1 + (2*zeta*r)^2) / ((1 - r^2)^2 + (2*zeta*r)^2) ),   r = f / fn

    (Harris & Piersol [3], Ch. 2, Eq. 2.41). The per-mode response acceleration at the
    point of maximum mode-shape amplitude is

        a_n(f) = (Gamma_n / m_n) * |T_n(f)| * a_base

    where Gamma_n = phi_n^T M r_dir is the participation factor and m_n = phi_n^T M phi_n
    the generalized (modal) mass (Craig & Kurdila [2], Sec. 12.3). Modes are combined by
    SRSS — appropriate for well-separated modes.

Random (PSD base input):
    Miles' equation for the RMS acceleration response of a lightly damped SDOF under a
    flat-near-resonance base PSD (Steinberg [1], Eq. 6.6):

        GRMS_n = sqrt( (pi / 2) * fn * Q * W(fn) ),   Q = 1 / (2 * zeta)

    with W(fn) the input PSD (g^2/Hz) log-log interpolated at the natural frequency.
    Per-mode responses are scaled by (Gamma_n / m_n) and combined by SRSS. This is an
    APPROXIMATION — Miles assumes the input PSD is flat near each resonance and modes
    respond independently. Do not read more precision into the result than that.

References
----------
[1] Steinberg, D. S. (2000). "Vibration Analysis for Electronic Equipment", 3rd ed.,
    Wiley. Miles' equation: Eq. 6.6.
[2] Craig, R. R. & Kurdila, A. J. (2006). "Fundamentals of Structural Dynamics",
    2nd ed., Wiley. Modal participation / generalized mass: Sec. 12.3; cantilever
    eigenvalue coefficients (beta_1*L = 1.8751): Table 8.1.
[3] Harris, C. M. & Piersol, A. G. (2002). "Harris' Shock and Vibration Handbook",
    5th ed., McGraw-Hill. SDOF transmissibility: Ch. 2, Eq. 2.41.

Units: mm / kg / N internally; frequencies Hz; accelerations g; PSD g^2/Hz.
Density arrives as g/cm^3 (material dict convention) → 1 g/cm^3 = 1e-6 kg/mm^3.

Known limitations (stated honestly in every result payload):
  * Lumped mass = total mass / n_nodes (equal distribution). Exact total mass; for
    uniform-density single bodies the participation estimate is reasonable. The seam
    for consistent-mass recovery is documented in work/questions/T-BL17.md.
  * Mode shapes are unit-max-normalized, NOT mass-normalized — physical displacement /
    stress amplitudes are NOT recovered (that is the CalculiX *STEADY STATE DYNAMICS
    seam, T-BL04). Response is reported as acceleration amplification only.
  * SRSS modal combination assumes well-separated modes.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

import numpy as np

G_CM3_TO_KG_MM3 = 1e-6

_CITATION_HARMONIC = ("Craig & Kurdila 2006, Fundamentals of Structural Dynamics 2e, "
                      "Sec. 12.3 (modal participation); Harris & Piersol 2002, Harris' "
                      "Shock and Vibration Handbook 5e, Ch. 2 Eq. 2.41 (transmissibility)")
_CITATION_MILES = ("Steinberg 2000, Vibration Analysis for Electronic Equipment 3e, "
                   "Eq. 6.6 (Miles' equation)")
_LIMITATIONS = ("Lumped (equal-distribution) nodal mass; unit-max-normalized mode shapes "
                "(no physical displacement/stress recovery); SRSS combination assumes "
                "well-separated modes; Miles assumes flat input PSD near each resonance.")


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def lumped_mass_vector(n_nodes: int, total_mass_kg: float) -> np.ndarray:
    """Equal-distribution lumped nodal mass: total mass / n_nodes at every node.

    Correct in total mass; participation weighting ignores local density variation
    (uniform-density parts — the only current material model — are unaffected in
    aggregate). See module docstring, limitation 1.
    """
    if n_nodes <= 0:
        raise ValueError("n_nodes must be positive")
    if total_mass_kg <= 0.0:
        raise ValueError("total_mass_kg must be positive")
    return np.full(n_nodes, total_mass_kg / n_nodes)


def modal_participation(
    mode_shapes: np.ndarray,   # (n_modes, n_nodes, 3)
    mass_vec: np.ndarray,      # (n_nodes,)
    excitation_dir: Sequence[float],  # (3,) unit vector
) -> tuple[np.ndarray, np.ndarray]:
    """Participation factors Gamma_n = phi_n^T M r and generalized masses m_n = phi_n^T M phi_n.

    r is the rigid-body influence vector: excitation_dir replicated at every node.
    Reference: Craig & Kurdila [2], Sec. 12.3.

    Returns (gamma, gen_mass), each shape (n_modes,).
    """
    d = np.asarray(excitation_dir, dtype=float)
    nrm = np.linalg.norm(d)
    if nrm == 0.0:
        raise ValueError("excitation_dir must be a non-zero vector")
    d = d / nrm
    # phi . (M r): M diagonal per node (same mass on x/y/z DOFs of a node).
    #   Gamma_n = sum_i m_i * (phi_n[i] . d)
    proj = mode_shapes @ d                       # (n_modes, n_nodes)
    gamma = proj @ mass_vec                      # (n_modes,)
    # m_n = sum_i m_i * |phi_n[i]|^2
    gen_mass = np.einsum("mnc,mnc,n->m", mode_shapes, mode_shapes, mass_vec)
    return gamma, gen_mass


def sdof_transmissibility(freq_hz: float, sweep_hz: Sequence[float], zeta: float) -> np.ndarray:
    """|T(r)| = sqrt((1+(2z r)^2) / ((1-r^2)^2 + (2z r)^2)), r = f/fn.

    Harris & Piersol [3], Ch. 2, Eq. 2.41. At r=1 this equals sqrt(1+4z^2)/(2z) ~= Q.
    """
    if freq_hz <= 0.0:
        raise ValueError("freq_hz must be positive")
    if zeta <= 0.0:
        raise ValueError("zeta must be positive")
    r = np.asarray(sweep_hz, dtype=float) / freq_hz
    num = 1.0 + (2.0 * zeta * r) ** 2
    den = (1.0 - r * r) ** 2 + (2.0 * zeta * r) ** 2
    return np.sqrt(num / den)


def _interp_psd(psd_breakpoints: Sequence[Sequence[float]], f_hz: float) -> float:
    """Log-log interpolate a PSD breakpoint table [(f_hz, g2_per_hz), ...] at f_hz.

    Standard vibration-spec convention (test PSDs are straight lines on log-log axes,
    i.e. constant dB/octave slopes). Flat extrapolation outside the table range.
    """
    pts = sorted((float(f), float(w)) for f, w in psd_breakpoints)
    if not pts:
        raise ValueError("psd_breakpoints is empty")
    for f, w in pts:
        if f <= 0.0 or w <= 0.0:
            raise ValueError("PSD breakpoints must have positive frequency and PSD")
    if f_hz <= pts[0][0]:
        return pts[0][1]
    if f_hz >= pts[-1][0]:
        return pts[-1][1]
    for (f0, w0), (f1, w1) in zip(pts, pts[1:]):
        if f0 <= f_hz <= f1:
            if f0 == f1:
                return w0
            t = (math.log(f_hz) - math.log(f0)) / (math.log(f1) - math.log(f0))
            return math.exp(math.log(w0) + t * (math.log(w1) - math.log(w0)))
    return pts[-1][1]  # unreachable


def _reshape_modes(mode_shapes_flat: Sequence[float], n_modes: int, n_nodes: int) -> np.ndarray:
    """Reshape the C++ solver's flat mode-shape array (n_modes*n_nodes*3, row-major)."""
    arr = np.asarray(mode_shapes_flat, dtype=float)
    expect = n_modes * n_nodes * 3
    if arr.size != expect:
        raise ValueError(f"mode_shapes length {arr.size} != n_modes*n_nodes*3 = {expect}")
    return arr.reshape(n_modes, n_nodes, 3)


# ---------------------------------------------------------------------------
# Harmonic response (modal superposition)
# ---------------------------------------------------------------------------

def harmonic_response(
    freqs_hz: Sequence[float],
    mode_shapes_flat: Sequence[float],
    n_nodes: int,
    total_mass_kg: float,
    excitation_dir: Sequence[float],
    sweep_hz: Sequence[float],
    zeta: float = 0.02,
    base_accel_g: float = 1.0,
) -> dict[str, Any]:
    """Harmonic base-excitation response spectrum by SDOF-per-mode modal superposition.

    Skips rigid-body modes (< 0.5 Hz — free-free solves emit ~0 Hz modes).
    Returns peak response acceleration (g) vs sweep frequency, SRSS across modes.
    """
    freqs = [float(f) for f in freqs_hz]
    n_modes = len(freqs)
    shapes = _reshape_modes(mode_shapes_flat, n_modes, n_nodes)
    mass = lumped_mass_vector(n_nodes, total_mass_kg)
    gamma, gen_mass = modal_participation(shapes, mass, excitation_dir)

    sweep = np.asarray(sweep_hz, dtype=float)
    total_sq = np.zeros_like(sweep)
    per_mode: list[dict[str, Any]] = []
    for n, fn in enumerate(freqs):
        if fn < 0.5:  # rigid-body mode — no elastic amplification
            continue
        if gen_mass[n] <= 0.0:
            continue
        amp = abs(gamma[n]) / gen_mass[n]          # participation amplification
        t = sdof_transmissibility(fn, sweep, zeta)
        a_n = amp * t * base_accel_g               # (n_sweep,) response accel, g
        total_sq += a_n * a_n
        per_mode.append({
            "mode": n,
            "freq_hz": fn,
            "gamma_over_m": amp,
            "peak_accel_g": float(a_n.max()),
        })
    total = np.sqrt(total_sq)
    peak_idx = int(total.argmax()) if total.size else 0
    return {
        "sweep_hz": sweep.tolist(),
        "accel_g": total.tolist(),
        "peak_accel_g": float(total.max()) if total.size else 0.0,
        "peak_freq_hz": float(sweep[peak_idx]) if total.size else None,
        "per_mode": per_mode,
        "zeta": zeta,
        "base_accel_g": base_accel_g,
        "method": "modal_superposition_sdof",
        "combination": "SRSS",
        "citation": _CITATION_HARMONIC,
        "limitations": _LIMITATIONS,
    }


# ---------------------------------------------------------------------------
# Random response (Miles' equation)
# ---------------------------------------------------------------------------

def miles_response(
    freqs_hz: Sequence[float],
    mode_shapes_flat: Sequence[float],
    n_nodes: int,
    total_mass_kg: float,
    excitation_dir: Sequence[float],
    psd_breakpoints: Sequence[Sequence[float]],
    zeta: float = 0.02,
) -> dict[str, Any]:
    """Random-vibration GRMS by Miles' equation per mode, participation-scaled, SRSS total.

    GRMS_n = (|Gamma_n|/m_n) * sqrt(pi/2 * fn * Q * W(fn)),  Q = 1/(2 zeta).
    Steinberg [1], Eq. 6.6. APPROXIMATION — see module docstring.
    """
    freqs = [float(f) for f in freqs_hz]
    n_modes = len(freqs)
    shapes = _reshape_modes(mode_shapes_flat, n_modes, n_nodes)
    mass = lumped_mass_vector(n_nodes, total_mass_kg)
    gamma, gen_mass = modal_participation(shapes, mass, excitation_dir)

    q = 1.0 / (2.0 * zeta)
    per_mode: list[dict[str, Any]] = []
    total_sq = 0.0
    for n, fn in enumerate(freqs):
        if fn < 0.5 or gen_mass[n] <= 0.0:
            continue
        w = _interp_psd(psd_breakpoints, fn)
        amp = abs(gamma[n]) / gen_mass[n]
        grms_sdof = math.sqrt(math.pi / 2.0 * fn * q * w)
        grms = amp * grms_sdof
        total_sq += grms * grms
        per_mode.append({
            "mode": n,
            "freq_hz": fn,
            "psd_at_fn_g2_per_hz": w,
            "Q": q,
            "gamma_over_m": amp,
            "grms_sdof": grms_sdof,
            "grms": grms,
        })
    return {
        "per_mode": per_mode,
        "total_grms": math.sqrt(total_sq),
        "Q": q,
        "zeta": zeta,
        "method": "miles_equation",
        "combination": "SRSS",
        "citation": _CITATION_MILES,
        "limitations": _LIMITATIONS,
    }


# ---------------------------------------------------------------------------
# Spec verdict
# ---------------------------------------------------------------------------

def evaluate_vibration_spec(
    spec: Optional[dict[str, Any]],
    harmonic: Optional[dict[str, Any]],
    random: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Pass/fail verdict against a vibration spec requirement (DAG verify-node end condition).

    spec keys (both optional, at least one required for a verdict):
      max_peak_accel_g : harmonic peak response acceleration limit (g)
      max_grms         : random total GRMS limit (g)

    Returns None when spec is None/empty (no gate requested). A spec limit that
    references a result mode that wasn't run FAILS closed with a reason.
    """
    if not spec:
        return None
    checks: list[dict[str, Any]] = []
    passed = True
    if "max_peak_accel_g" in spec:
        limit = float(spec["max_peak_accel_g"])
        if harmonic is None or not harmonic.get("sweep_hz"):
            checks.append({"check": "max_peak_accel_g", "limit": limit, "actual": None,
                           "passed": False, "reason": "harmonic response not computed"})
            passed = False
        else:
            actual = float(harmonic["peak_accel_g"])
            ok = actual <= limit
            checks.append({"check": "max_peak_accel_g", "limit": limit,
                           "actual": actual, "passed": ok})
            passed = passed and ok
    if "max_grms" in spec:
        limit = float(spec["max_grms"])
        if random is None or not random.get("ok", True) or "total_grms" not in random:
            checks.append({"check": "max_grms", "limit": limit, "actual": None,
                           "passed": False,
                           "reason": "random (PSD) response not computed — provide psd_breakpoints"})
            passed = False
        else:
            actual = float(random["total_grms"])
            ok = actual <= limit
            checks.append({"check": "max_grms", "limit": limit,
                           "actual": actual, "passed": ok})
            passed = passed and ok
    if not checks:
        return {"passed": False, "checks": [],
                "reason": "vibration_spec had no recognized keys "
                          "(use max_peak_accel_g and/or max_grms)"}
    return {"passed": passed, "checks": checks}
