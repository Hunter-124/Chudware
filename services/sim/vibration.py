"""Vibration frequency-response solver — modal superposition (harmonic) + Miles' equation (random PSD).

Builds on the C++ modal solver's output (``sim_run_modal`` → natural frequencies +
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
    uniform-density single bodies the participation estimate is reasonable. Consistent-mass
    recovery is a future seam.
  * Mode shapes are unit-max-normalized, NOT mass-normalized — physical displacement /
    stress amplitudes are NOT recovered (that is the CalculiX *STEADY STATE DYNAMICS
    seam). Response is reported as acceleration amplification only.
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

def _damping_ratio(value: float) -> float:
    zeta = float(value)
    if not math.isfinite(zeta) or not 0.0 < zeta < 1.0:
        raise ValueError("zeta must be finite and between 0 and 1")
    return zeta


def _validated_psd_points(
    psd_breakpoints: Sequence[Sequence[float]],
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index, point in enumerate(psd_breakpoints):
        if len(point) != 2:
            raise ValueError(f"PSD breakpoint {index} must be [frequency_hz, g2_per_hz]")
        frequency, density = float(point[0]), float(point[1])
        if not math.isfinite(frequency) or not math.isfinite(density):
            raise ValueError("PSD breakpoints must be finite")
        if frequency <= 0.0 or density <= 0.0:
            raise ValueError("PSD breakpoints must have positive frequency and PSD")
        points.append((frequency, density))
    if not points:
        raise ValueError("psd_breakpoints is empty")
    points.sort()
    if any(right[0] <= left[0] for left, right in zip(points, points[1:])):
        raise ValueError("PSD breakpoint frequencies must be unique")
    return points


def lumped_mass_vector(n_nodes: int, total_mass_kg: float) -> np.ndarray:
    """Equal-distribution lumped nodal mass: total mass / n_nodes at every node.

    Correct in total mass; participation weighting ignores local density variation
    (uniform-density parts — the only current material model — are unaffected in
    aggregate). See module docstring, limitation 1.
    """
    if isinstance(n_nodes, bool) or int(n_nodes) != n_nodes or n_nodes <= 0:
        raise ValueError("n_nodes must be a positive integer")
    total_mass_kg = float(total_mass_kg)
    if not math.isfinite(total_mass_kg) or total_mass_kg <= 0.0:
        raise ValueError("total_mass_kg must be finite and positive")
    return np.full(int(n_nodes), total_mass_kg / int(n_nodes))


def modal_participation(
    mode_shapes: np.ndarray,   # (n_modes, n_nodes, 3)
    mass_vec: np.ndarray,      # (n_nodes,)
    excitation_dir: Sequence[float],  # (3,) unit vector
) -> tuple[np.ndarray, np.ndarray]:
    """Participation factors Gamma_n and generalized masses for each mode."""
    shapes = np.asarray(mode_shapes, dtype=float)
    mass = np.asarray(mass_vec, dtype=float)
    if shapes.ndim != 3 or shapes.shape[2] != 3:
        raise ValueError("mode_shapes must have shape (n_modes, n_nodes, 3)")
    if mass.ndim != 1 or mass.shape[0] != shapes.shape[1]:
        raise ValueError("mass_vec length must equal the mode-shape node count")
    if not np.all(np.isfinite(shapes)):
        raise ValueError("mode_shapes must be finite")
    if not np.all(np.isfinite(mass)) or np.any(mass <= 0.0):
        raise ValueError("mass_vec entries must be finite and positive")
    direction = np.asarray(excitation_dir, dtype=float)
    if direction.shape != (3,) or not np.all(np.isfinite(direction)):
        raise ValueError("excitation_dir must contain three finite components")
    norm = float(np.linalg.norm(direction))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("excitation_dir must be a non-zero vector")
    direction = direction / norm
    projection = shapes @ direction
    gamma = projection @ mass
    gen_mass = np.einsum("mnc,mnc,n->m", shapes, shapes, mass)
    return gamma, gen_mass


def sdof_transmissibility(freq_hz: float, sweep_hz: Sequence[float], zeta: float) -> np.ndarray:
    """|T(r)| for absolute acceleration under harmonic base excitation."""
    freq_hz = float(freq_hz)
    if not math.isfinite(freq_hz) or freq_hz <= 0.0:
        raise ValueError("freq_hz must be finite and positive")
    zeta = _damping_ratio(zeta)
    sweep = np.asarray(sweep_hz, dtype=float)
    if (sweep.ndim != 1 or sweep.size == 0 or not np.all(np.isfinite(sweep))
            or np.any(sweep <= 0.0)):
        raise ValueError("sweep_hz must contain finite positive frequencies")
    ratio = sweep / freq_hz
    numerator = 1.0 + (2.0 * zeta * ratio) ** 2
    denominator = (1.0 - ratio * ratio) ** 2 + (2.0 * zeta * ratio) ** 2
    result = np.sqrt(numerator / denominator)
    if not np.all(np.isfinite(result)):
        raise ValueError("transmissibility calculation produced non-finite values")
    return result


def _interp_psd(psd_breakpoints: Sequence[Sequence[float]], f_hz: float) -> float:
    """Log-log interpolate a PSD table, with flat endpoint extrapolation."""
    points = _validated_psd_points(psd_breakpoints)
    f_hz = float(f_hz)
    if not math.isfinite(f_hz) or f_hz <= 0.0:
        raise ValueError("PSD interpolation frequency must be finite and positive")
    if f_hz <= points[0][0]:
        return points[0][1]
    if f_hz >= points[-1][0]:
        return points[-1][1]
    for (f0, w0), (f1, w1) in zip(points, points[1:]):
        if f0 <= f_hz <= f1:
            fraction = (math.log(f_hz) - math.log(f0)) / (math.log(f1) - math.log(f0))
            return math.exp(math.log(w0) + fraction * (math.log(w1) - math.log(w0)))
    raise RuntimeError("validated PSD interpolation interval was not found")


def _reshape_modes(mode_shapes_flat: Sequence[float], n_modes: int, n_nodes: int) -> np.ndarray:
    """Reshape and validate the C++ solver's row-major mode-shape array."""
    if isinstance(n_nodes, bool) or int(n_nodes) != n_nodes or n_nodes <= 0:
        raise ValueError("n_nodes must be a positive integer")
    if n_modes <= 0:
        raise ValueError("at least one natural frequency is required")
    arr = np.asarray(mode_shapes_flat, dtype=float)
    expect = n_modes * int(n_nodes) * 3
    if arr.ndim != 1 or arr.size != expect:
        raise ValueError(f"mode_shapes length {arr.size} != n_modes*n_nodes*3 = {expect}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("mode_shapes must be finite")
    return arr.reshape(n_modes, int(n_nodes), 3)


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
    """Harmonic base-excitation response spectrum by modal superposition."""
    freqs = np.asarray(freqs_hz, dtype=float)
    if freqs.ndim != 1 or freqs.size == 0 or not np.all(np.isfinite(freqs)):
        raise ValueError("freqs_hz must contain finite natural frequencies")
    if np.any(freqs < 0.0):
        raise ValueError("freqs_hz cannot contain negative frequencies")
    zeta = _damping_ratio(zeta)
    base_accel_g = float(base_accel_g)
    if not math.isfinite(base_accel_g) or base_accel_g <= 0.0:
        raise ValueError("base_accel_g must be finite and > 0")
    sweep = np.asarray(sweep_hz, dtype=float)
    if (sweep.ndim != 1 or sweep.size == 0 or not np.all(np.isfinite(sweep))
            or np.any(sweep <= 0.0)):
        raise ValueError("sweep_hz must contain finite positive frequencies")

    shapes = _reshape_modes(mode_shapes_flat, len(freqs), n_nodes)
    mass = lumped_mass_vector(n_nodes, total_mass_kg)
    gamma, gen_mass = modal_participation(shapes, mass, excitation_dir)

    total_sq = np.zeros_like(sweep)
    per_mode: list[dict[str, Any]] = []
    has_participating_mode = False
    for n, fn in enumerate(freqs):
        if fn < 0.5:
            continue
        if not math.isfinite(float(gen_mass[n])) or gen_mass[n] <= 0.0:
            continue
        amp = abs(float(gamma[n])) / float(gen_mass[n])
        has_participating_mode = has_participating_mode or amp > 0.0
        transmissibility = sdof_transmissibility(float(fn), sweep, zeta)
        response = amp * transmissibility * base_accel_g
        total_sq += response * response
        per_mode.append({
            "mode": n,
            "freq_hz": float(fn),
            "gamma_over_m": amp,
            "peak_accel_g": float(response.max()),
        })
    if not per_mode:
        raise ValueError("modal result contains no elastic modes at or above 0.5 Hz")
    if not has_participating_mode:
        raise ValueError("no elastic mode participates in the requested excitation direction")
    total = np.sqrt(total_sq)
    if not np.all(np.isfinite(total)):
        raise ValueError("harmonic response produced non-finite acceleration")
    peak_idx = int(total.argmax())
    return {
        "sweep_hz": sweep.tolist(),
        "accel_g": total.tolist(),
        "peak_accel_g": float(total[peak_idx]),
        "peak_freq_hz": float(sweep[peak_idx]),
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
    """Random-vibration GRMS by Miles' equation and modal SRSS."""
    freqs = np.asarray(freqs_hz, dtype=float)
    if freqs.ndim != 1 or freqs.size == 0 or not np.all(np.isfinite(freqs)):
        raise ValueError("freqs_hz must contain finite natural frequencies")
    if np.any(freqs < 0.0):
        raise ValueError("freqs_hz cannot contain negative frequencies")
    zeta = _damping_ratio(zeta)
    points = _validated_psd_points(psd_breakpoints)
    shapes = _reshape_modes(mode_shapes_flat, len(freqs), n_nodes)
    mass = lumped_mass_vector(n_nodes, total_mass_kg)
    gamma, gen_mass = modal_participation(shapes, mass, excitation_dir)

    quality = 1.0 / (2.0 * zeta)
    per_mode: list[dict[str, Any]] = []
    total_sq = 0.0
    has_participating_mode = False
    for n, fn in enumerate(freqs):
        if fn < 0.5 or gen_mass[n] <= 0.0:
            continue
        density = _interp_psd(points, float(fn))
        amp = abs(float(gamma[n])) / float(gen_mass[n])
        has_participating_mode = has_participating_mode or amp > 0.0
        grms_sdof = math.sqrt(math.pi / 2.0 * float(fn) * quality * density)
        grms = amp * grms_sdof
        total_sq += grms * grms
        per_mode.append({
            "mode": n,
            "freq_hz": float(fn),
            "psd_at_fn_g2_per_hz": density,
            "Q": quality,
            "gamma_over_m": amp,
            "grms_sdof": grms_sdof,
            "grms": grms,
        })
    if not per_mode:
        raise ValueError("modal result contains no elastic modes at or above 0.5 Hz")
    if not has_participating_mode:
        raise ValueError("no elastic mode participates in the requested excitation direction")
    total_grms = math.sqrt(total_sq)
    if not math.isfinite(total_grms):
        raise ValueError("Miles response produced a non-finite GRMS value")
    return {
        "per_mode": per_mode,
        "total_grms": total_grms,
        "Q": quality,
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
    """Evaluate finite, non-negative acceleration limits and fail closed."""
    if not spec:
        return None
    checks: list[dict[str, Any]] = []
    passed = True

    def limit_value(key: str) -> Optional[float]:
        nonlocal passed
        try:
            value = float(spec[key])
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value < 0.0:
            checks.append({"check": key, "limit": None, "actual": None,
                           "passed": False,
                           "reason": f"{key} must be a finite non-negative limit"})
            passed = False
            return None
        return value

    if "max_peak_accel_g" in spec:
        limit = limit_value("max_peak_accel_g")
        if limit is not None:
            if harmonic is None or not harmonic.get("sweep_hz"):
                checks.append({"check": "max_peak_accel_g", "limit": limit, "actual": None,
                               "passed": False, "reason": "harmonic response not computed"})
                passed = False
            else:
                actual = float(harmonic.get("peak_accel_g", math.nan))
                ok = math.isfinite(actual) and actual <= limit
                check = {"check": "max_peak_accel_g", "limit": limit,
                         "actual": actual if math.isfinite(actual) else None, "passed": ok}
                if not math.isfinite(actual):
                    check["reason"] = "harmonic response is non-finite"
                checks.append(check)
                passed = passed and ok
    if "max_grms" in spec:
        limit = limit_value("max_grms")
        if limit is not None:
            if random is None or not random.get("ok", True) or "total_grms" not in random:
                checks.append({"check": "max_grms", "limit": limit, "actual": None,
                               "passed": False,
                               "reason": "random (PSD) response not computed — "
                                         "provide psd_breakpoints"})
                passed = False
            else:
                actual = float(random.get("total_grms", math.nan))
                ok = math.isfinite(actual) and actual <= limit
                check = {"check": "max_grms", "limit": limit,
                         "actual": actual if math.isfinite(actual) else None, "passed": ok}
                if not math.isfinite(actual):
                    check["reason"] = "random response is non-finite"
                checks.append(check)
                passed = passed and ok
    if not checks:
        return {"passed": False, "checks": [],
                "reason": "vibration_spec had no recognized keys "
                          "(use max_peak_accel_g and/or max_grms)"}
    return {"passed": passed, "checks": checks}
