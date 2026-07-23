"""Analytic waveguide electromagnetics — closed-form references for the EM sim family.

Everything in this module is textbook closed-form math (Pozar, *Microwave Engineering*):
rectangular/circular hollow-waveguide cutoffs, the TE/TM propagation constant, ideal
uniform-section S-parameters, and a pulse-through-dispersive-guide time-domain response
built by inverse FFT.  It serves three roles:

1. the ``analytic`` fallback adapter (real physics for uniform guides, clearly labeled);
2. the validation oracle for the numerical solvers (FDFD eigenmode, openEMS FDTD);
3. the standard-waveguide (WR-xx) dimension table shared by every adapter.

No external solver, no GPL code — pure numpy.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

C0 = 299_792_458.0  # speed of light, m/s
MU0 = 4.0e-7 * math.pi          # vacuum permeability, H/m (classical SI definition)
EPS0 = 1.0 / (MU0 * C0**2)      # vacuum permittivity from c = 1/sqrt(mu0*eps0)
ETA0 = MU0 * C0                 # free-space wave impedance eta0 = sqrt(mu0/eps0) ~ 376.73 ohm

# ---------------------------------------------------------------- standard guides
# EIA rectangular waveguide standards: name -> (a_mm, b_mm). a is the broad wall.
STANDARD_WAVEGUIDES: Dict[str, Tuple[float, float]] = {
    "WR-2300": (584.20, 292.10),
    "WR-650": (165.10, 82.55),
    "WR-430": (109.22, 54.61),
    "WR-284": (72.136, 34.036),
    "WR-187": (47.5488, 22.1488),
    "WR-137": (34.8488, 15.7988),
    "WR-112": (28.4988, 12.6238),
    "WR-90": (22.86, 10.16),     # X band 8.2-12.4 GHz; TE10 fc = 6.557 GHz
    "WR-75": (19.05, 9.525),
    "WR-62": (15.7988, 7.8994),
    "WR-42": (10.668, 4.318),
    "WR-28": (7.112, 3.556),
    "WR-15": (3.7592, 1.8796),
    "WR-10": (2.54, 1.27),
}


def lookup_standard(name: str) -> Tuple[float, float]:
    """Return (a_mm, b_mm) for an EIA name.  Accepts 'WR90', 'wr-90', 'WR 90'."""
    key = name.strip().upper().replace(" ", "").replace("_", "-")
    if not key.startswith("WR"):
        raise ValueError(f"unknown waveguide standard {name!r} (expected WR-xx)")
    digits = key[2:].lstrip("-")
    canon = f"WR-{digits}"
    if canon not in STANDARD_WAVEGUIDES:
        raise ValueError(
            f"unknown waveguide standard {name!r}; known: {', '.join(STANDARD_WAVEGUIDES)}")
    return STANDARD_WAVEGUIDES[canon]


# ---------------------------------------------------------------- cutoff frequencies

# First bessel-derivative roots (TE modes, circular guide) p'_nm and bessel roots
# (TM modes) p_nm, indexed [n][m-1]. Enough for the first ~10 circular modes.
_BESSEL_DERIV_ROOTS = {
    0: [3.8317, 7.0156, 10.1735],
    1: [1.8412, 5.3314, 8.5363],
    2: [3.0542, 6.7061, 9.9695],
    3: [4.2012, 8.0152, 11.3459],
}
_BESSEL_ROOTS = {
    0: [2.4048, 5.5201, 8.6537],
    1: [3.8317, 7.0156, 10.1735],
    2: [5.1356, 8.4172, 11.6198],
    3: [6.3802, 9.7610, 13.0152],
}


def rect_cutoff_hz(a_m: float, b_m: float, m: int, n: int, eps_r: float = 1.0) -> float:
    """Cutoff of TE/TM mn in a hollow rectangular guide (a = broad wall, metres)."""
    if m == 0 and n == 0:
        raise ValueError("TE00/TM00 does not exist")
    kc = math.pi * math.hypot(m / a_m, n / b_m)
    return C0 / math.sqrt(eps_r) * kc / (2.0 * math.pi)


def rect_mode_table(a_m: float, b_m: float, n_modes: int = 6,
                    eps_r: float = 1.0) -> List[dict]:
    """First ``n_modes`` TE+TM modes of a rectangular guide, sorted by cutoff."""
    modes: List[dict] = []
    for m in range(0, 7):
        for n in range(0, 7):
            if m == 0 and n == 0:
                continue
            fc = rect_cutoff_hz(a_m, b_m, m, n, eps_r)
            kc = 2.0 * math.pi * fc * math.sqrt(eps_r) / C0
            modes.append({"type": "TE", "m": m, "n": n, "name": f"TE{m}{n}",
                          "fc_hz": fc, "kc_per_m": kc})
            if m >= 1 and n >= 1:
                modes.append({"type": "TM", "m": m, "n": n, "name": f"TM{m}{n}",
                              "fc_hz": fc, "kc_per_m": kc})
    modes.sort(key=lambda md: (md["fc_hz"], md["type"]))
    return modes[:n_modes]


def circ_mode_table(radius_m: float, n_modes: int = 6, eps_r: float = 1.0) -> List[dict]:
    """First ``n_modes`` TE+TM modes of a circular guide of radius ``radius_m``."""
    modes: List[dict] = []
    for n, roots in _BESSEL_DERIV_ROOTS.items():
        for m, p in enumerate(roots, start=1):
            kc = p / radius_m
            modes.append({"type": "TE", "m": n, "n": m, "name": f"TE{n}{m}",
                          "fc_hz": C0 / math.sqrt(eps_r) * kc / (2 * math.pi),
                          "kc_per_m": kc})
    for n, roots in _BESSEL_ROOTS.items():
        for m, p in enumerate(roots, start=1):
            kc = p / radius_m
            modes.append({"type": "TM", "m": n, "n": m, "name": f"TM{n}{m}",
                          "fc_hz": C0 / math.sqrt(eps_r) * kc / (2 * math.pi),
                          "kc_per_m": kc})
    modes.sort(key=lambda md: (md["fc_hz"], md["type"]))
    return modes[:n_modes]


# ---------------------------------------------------------------- line impedances

def te_wave_impedance(f_hz: float, fc_hz: float, eps_r: float = 1.0) -> float:
    """TE-mode wave impedance of a hollow guide above cutoff (Pozar 4e, eq. 3.22).

    Z_TE = k*eta/beta = eta / sqrt(1 - (fc/f)^2), eta = eta0/sqrt(eps_r).
    Below (or at) cutoff there is no propagating wave -> ValueError.
    """
    if f_hz <= fc_hz:
        raise ValueError(f"TE wave impedance undefined at/below cutoff "
                         f"({f_hz:.4g} Hz <= fc {fc_hz:.4g} Hz)")
    eta = ETA0 / math.sqrt(eps_r)
    return eta / math.sqrt(1.0 - (fc_hz / f_hz) ** 2)


def parallel_plate_z0(w_m: float, d_m: float, eps_r: float = 1.0) -> float:
    """Characteristic impedance of an ideal parallel-plate line (Pozar 4e, §3.2).

    Z0 = eta*d/w with eta = eta0/sqrt(eps_r) — plate width w, separation d,
    fringing fields neglected (valid for w >> d).
    """
    if w_m <= 0 or d_m <= 0 or eps_r < 1.0:
        raise ValueError("parallel_plate_z0 needs w > 0, d > 0, eps_r >= 1")
    return ETA0 / math.sqrt(eps_r) * d_m / w_m


def microstrip_z0(w_m: float, h_m: float, eps_r: float) -> Dict[str, float]:
    """Quasi-static microstrip characteristic impedance — Hammerstad closed form
    (Pozar 4e, eqs. 3.195-3.196; zero-thickness trace).

    eps_e = (er+1)/2 + (er-1)/2 / sqrt(1 + 12 h/W)
    W/h <= 1:  Z0 = 60/sqrt(eps_e) * ln(8h/W + W/(4h))
    W/h >= 1:  Z0 = 120*pi / (sqrt(eps_e) * (W/h + 1.393 + 0.667*ln(W/h + 1.444)))

    Returns {"z0": ohm, "eps_eff": ..., "model": "microstrip-hammerstad"}.
    """
    if w_m <= 0 or h_m <= 0 or eps_r < 1.0:
        raise ValueError("microstrip_z0 needs w > 0, h > 0, eps_r >= 1")
    u = w_m / h_m
    eps_e = (eps_r + 1.0) / 2.0 + (eps_r - 1.0) / 2.0 / math.sqrt(1.0 + 12.0 / u)
    if u <= 1.0:
        z0 = 60.0 / math.sqrt(eps_e) * math.log(8.0 / u + u / 4.0)
    else:
        z0 = 120.0 * math.pi / (math.sqrt(eps_e)
                                * (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return {"z0": z0, "eps_eff": eps_e, "model": "microstrip-hammerstad"}


# ---------------------------------------------------------------- propagation

def gamma(f_hz: np.ndarray, fc_hz: float, eps_r: float = 1.0) -> np.ndarray:
    """Complex propagation constant of a single hollow-guide mode (lossless walls).

    gamma = alpha + j*beta;  evanescent (real) below cutoff, propagating above.
    """
    f = np.asarray(f_hz, dtype=float)
    k = 2.0 * np.pi * f * math.sqrt(eps_r) / C0
    kc = 2.0 * math.pi * fc_hz * math.sqrt(eps_r) / C0
    arg = k**2 - kc**2
    g = np.where(arg >= 0.0,
                 1j * np.sqrt(np.maximum(arg, 0.0)),
                 np.sqrt(np.maximum(-arg, 0.0)) + 0j)
    return g


def uniform_section_sparams(f_hz: np.ndarray, fc_hz: float, length_m: float,
                            eps_r: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """Ideal S11/S21 of a uniform guide section between two matched modal ports.

    With matched (mode-normalised) ports there is no reflection: S11 = 0 and
    S21 = exp(-gamma*L) — below cutoff |S21| decays exponentially, above cutoff
    |S21| = 1 with linear-in-beta phase.  This is the trusted reference the FDTD
    validation compares against.
    """
    g = gamma(f_hz, fc_hz, eps_r)
    s21 = np.exp(-g * length_m)
    s11 = np.zeros_like(s21)
    return s11, s21


def gaussian_pulse(t_s: np.ndarray, f0_hz: float, fc_bw_hz: float) -> np.ndarray:
    """openEMS-style Gaussian-modulated sine: centre f0, 20 dB bandwidth ~ fc_bw."""
    t0 = 3.0 / (2.0 * math.pi * fc_bw_hz) * 3.0
    tau = 3.0 / (2.0 * math.pi * fc_bw_hz)
    return np.sin(2.0 * np.pi * f0_hz * (t_s - t0)) * np.exp(-((t_s - t0) / tau) ** 2)


def timedomain_response(fc_hz: float, length_m: float, f0_hz: float, bw_hz: float,
                        n_samples: int = 4096, eps_r: float = 1.0,
                        ) -> Dict[str, np.ndarray]:
    """Broadband pulse through a dispersive uniform guide, via FFT/IFFT.

    Returns dict with t_s, v_in (excitation), v_out (transmitted waveform).
    """
    f_max = f0_hz + 4.0 * bw_hz
    dt = 1.0 / (8.0 * f_max)
    t = np.arange(n_samples) * dt
    v_in = gaussian_pulse(t, f0_hz, bw_hz)
    spec = np.fft.rfft(v_in)
    freqs = np.fft.rfftfreq(n_samples, dt)
    _, s21 = uniform_section_sparams(freqs, fc_hz, length_m, eps_r)
    v_out = np.fft.irfft(spec * s21, n=n_samples)
    return {"t_s": t, "v_in": v_in, "v_out": v_out}


def rect_te_field(a_m: float, b_m: float, m: int, n: int,
                  nx: int = 32, ny: int = 16) -> np.ndarray:
    """|Hz| of TEmn on an nx x ny preview grid (unit peak). For mode-preview payloads."""
    x = np.linspace(0.0, a_m, nx)
    y = np.linspace(0.0, b_m, ny)
    X, Y = np.meshgrid(x, y, indexing="ij")
    return np.cos(m * np.pi * X / a_m) * np.cos(n * np.pi * Y / b_m)
