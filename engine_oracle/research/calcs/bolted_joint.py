"""Bolted joint calculator: preload from torque, stress area check vs proof load.

Reference: Shigley's Mechanical Engineering Design, 10th ed., §8-1 through §8-5.

Formulas used
-------------
Tensile stress area (Shigley Eq. 8-2):
    A_t = (π/4) · (d - 0.9382·p)²

Torque–preload relationship (Shigley Eq. 8-31):
    T = K · F_i · d
    K ≈ 0.2 (common nut factor; Shigley Table 8-15 notes K=0.20 for as-received)

Preload ratio (Shigley §8-5):
    F_i = 0.75 · F_p  (recommended preload = 75 % of proof load for reused bolts)
    F_p = A_t · S_p   (proof load)
"""
from __future__ import annotations

import math
from typing import Optional

from .registry import calc_envelope, calc_fail, register

# ---------------------------------------------------------------------------
# M-series thread data: pitch [mm] and proof strength [MPa] by grade
# Shigley Table 8-9 (metric bolt grades) and Table 8-1 (thread geometry)
# ---------------------------------------------------------------------------

# Nominal diameter [mm] -> standard coarse pitch [mm]
# Source: ISO 261 / Shigley Table 8-1 (coarse series)
_COARSE_PITCH: dict[int, float] = {
    3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5,
    12: 1.75, 16: 2.0, 20: 2.5, 24: 3.0, 30: 3.5, 36: 4.0,
}

# Grade -> proof strength [MPa]  (Shigley Table 8-9)
_PROOF_STRENGTH: dict[str, float] = {
    "4.6": 225.0,
    "4.8": 310.0,
    "5.8": 380.0,
    "8.8": 600.0,
    "9.8": 650.0,
    "10.9": 830.0,
    "12.9": 970.0,
}


def tensile_stress_area(d_mm: float, pitch_mm: float) -> float:
    """Tensile stress area A_t [mm²].

    Shigley Eq. 8-2:  A_t = (π/4)·(d − 0.9382·p)²
    """
    return (math.pi / 4.0) * (d_mm - 0.9382 * pitch_mm) ** 2


def recommended_torque(d_mm: float, pitch_mm: float, grade: str,
                       K: float = 0.2) -> tuple[float, float, float]:
    """Return (T_nm, F_i_n, A_t_mm2) for recommended preload torque.

    Shigley §8-5:
      A_t = tensile stress area
      S_p = proof strength for grade
      F_p = A_t · S_p  (proof load)
      F_i = 0.75 · F_p  (75 % preload for reused fasteners)
      T   = K · F_i · d  [N·mm]  → converted to N·m
    """
    S_p = _PROOF_STRENGTH.get(grade)
    if S_p is None:
        raise ValueError(f"unsupported grade {grade!r}; known: {sorted(_PROOF_STRENGTH)}.")
    A_t = tensile_stress_area(d_mm, pitch_mm)
    F_p = A_t * S_p            # [N]
    F_i = 0.75 * F_p           # [N]
    T   = K * F_i * d_mm       # [N·mm]
    T_nm = T / 1000.0          # [N·m]
    return T_nm, F_i, A_t


# ---------------------------------------------------------------------------
# Envelope-wrapped calculator
# ---------------------------------------------------------------------------

def bolted_joint(
    d_mm: float,
    grade: str = "8.8",
    pitch_mm: Optional[float] = None,
    T_nm: Optional[float] = None,
    K: float = 0.2,
    prefilled: Optional[dict] = None,
) -> dict:
    """Bolted joint preload and stress check.

    Parameters
    ----------
    d_mm :     Nominal bolt diameter [mm].
    grade :    ISO metric grade string, e.g. ``"8.8"``, ``"10.9"``.
    pitch_mm : Thread pitch [mm]; defaults to ISO coarse series.
    T_nm :     Applied torque [N·m]; if omitted, recommended torque is computed.
    K :        Nut factor (default 0.2, Shigley Table 8-15 as-received).
    prefilled: Provenance dict for inputs from live geometry.

    Returns
    -------
    aicad.calc/1 envelope.  ``answer`` has:
        A_t_mm2, S_p_mpa, F_p_n, F_i_n (preload), sigma_i_mpa (preload stress),
        recommended_torque_nm, applied_torque_nm (if given), utilisation_pct.
    """
    try:
        d_mm = float(d_mm)
        K = float(K)
    except (TypeError, ValueError) as exc:
        return calc_fail("bad_query", f"bolted_joint: non-numeric param — {exc}.")

    if d_mm <= 0:
        return calc_fail("bad_query", "d_mm must be > 0.")
    grade = str(grade).strip()
    if grade not in _PROOF_STRENGTH:
        return calc_fail("bad_query",
                         f"unsupported grade {grade!r}; known: {sorted(_PROOF_STRENGTH)}.")

    # Resolve pitch
    d_key = round(d_mm)
    if pitch_mm is None:
        pitch_mm = _COARSE_PITCH.get(d_key)
        if pitch_mm is None:
            return calc_fail(
                "bad_query",
                f"no default coarse pitch for d={d_mm} mm; "
                f"provide pitch_mm explicitly. Known sizes: {sorted(_COARSE_PITCH)}.",
            )
    pitch_mm = float(pitch_mm)

    try:
        T_rec_nm, F_i_rec, A_t = recommended_torque(d_mm, pitch_mm, grade, K)
    except ValueError as exc:
        return calc_fail("bad_query", str(exc))

    S_p = _PROOF_STRENGTH[grade]
    F_p = A_t * S_p

    if T_nm is not None:
        # Compute actual preload from applied torque
        T_nm_f = float(T_nm)
        T_nmm = T_nm_f * 1000.0
        F_i_applied = T_nmm / (K * d_mm)
        sigma_i = F_i_applied / A_t
        utilisation = (F_i_applied / F_p) * 100.0
        notes = []
        if sigma_i > S_p:
            notes.append(
                f"WARNING: preload stress {sigma_i:.1f} MPa exceeds proof strength "
                f"{S_p:.1f} MPa — bolt may yield."
            )
        answer = {
            "A_t_mm2":              round(A_t, 2),
            "S_p_mpa":              S_p,
            "F_p_n":                round(F_p, 1),
            "F_i_n":                round(F_i_applied, 1),
            "sigma_i_mpa":          round(sigma_i, 2),
            "recommended_torque_nm": round(T_rec_nm, 2),
            "applied_torque_nm":    round(T_nm_f, 2),
            "utilisation_pct":      round(utilisation, 1),
        }
    else:
        sigma_i_rec = F_i_rec / A_t
        answer = {
            "A_t_mm2":              round(A_t, 2),
            "S_p_mpa":              S_p,
            "F_p_n":                round(F_p, 1),
            "F_i_n":                round(F_i_rec, 1),
            "sigma_i_mpa":          round(sigma_i_rec, 2),
            "recommended_torque_nm": round(T_rec_nm, 2),
            "applied_torque_nm":    None,
            "utilisation_pct":      75.0,
        }
        notes = []

    return calc_envelope(
        calc="bolted_joint",
        answer=answer,
        units="N, MPa, N·m",
        equation="T=K·Fi·d; At=(π/4)·(d−0.9382p)²; Fi=0.75·Fp; Fp=At·Sp",
        inputs={
            "d_mm": d_mm,
            "grade": grade,
            "pitch_mm": pitch_mm,
            "K": K,
            **({"T_nm": T_nm} if T_nm is not None else {}),
        },
        source_kind="closed_form",
        citation=(
            "Shigley's Mechanical Engineering Design, 10th ed., §8-1..§8-5 "
            "(Eq. 8-2 tensile stress area; Eq. 8-31 torque-preload; Table 8-9 "
            "metric proof strengths; Table 8-15 nut factor K=0.20)."
        ),
        notes=notes,
        prefilled=prefilled,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    "bolted_joint",
    bolted_joint,
    input_schema={
        "d_mm":     {"type": "float", "unit": "mm",
                     "description": "Nominal bolt diameter [mm]",
                     "family": "length"},
        "grade":    {"type": "string", "unit": None,
                     "description": "ISO metric grade (e.g. '8.8', '10.9')",
                     "family": "string"},
        "pitch_mm": {"type": "float", "unit": "mm",
                     "description": "Thread pitch [mm]; defaults to ISO coarse",
                     "family": "length"},
        "T_nm":     {"type": "float", "unit": "N·m",
                     "description": "Applied torque [N·m] (optional)",
                     "family": "torque"},
        "K":        {"type": "float", "unit": None,
                     "description": "Nut factor (default 0.2)",
                     "family": "dimensionless"},
    },
    prefill_hints={},
)
