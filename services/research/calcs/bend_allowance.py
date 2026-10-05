"""Sheet-metal bend allowance / bend deduction calculator.

Registered ``aicad.calc/1`` calculator for the sheet-metal bend geometry kind
in ``contracts/design_pipeline.md`` §8. Computes bend allowance and deduction
from dimensions and a cited K-factor table rather than a recalled flat length.

Closed-form (the industry-standard neutral-axis model):

    BA = θ · (π/180) · (r + K · t)          bend allowance (arc of the neutral axis)
    OSSB = tan(θ/2) · (r + t)               outside setback
    BD = 2 · OSSB − BA                       bend deduction

where θ = bend angle (deg, the complement of the included angle — a 90° bend has
θ=90), r = inside bend radius, t = material thickness, K = K-factor (neutral-axis
position as a fraction of t). A folded flange's flat pattern length is
``leg_a + leg_b − BD`` per bend.

K-factor table (the load-bearing numbers), from the standard sheet-metal DFM
references — Machinery's Handbook (29th ed., "Bending Sheet Metal") and the SME
"Die Design Handbook": K rises with the r/t ratio because a tighter bend pushes the
neutral axis further toward the inside face.

Source kind: heuristic (K-factor is a tabulated DFM guideline, not a closed-form
derivation); the BA/OSSB/BD relations themselves are exact geometry.
"""
from __future__ import annotations

from typing import Optional

from .registry import calc_envelope, calc_fail, register

import math

# r/t ratio -> K-factor.  Ref: Machinery's Handbook 29th ed. (Bending Sheet Metal);
# SME Die Design Handbook.  Air-bending / soft materials trend lower, coining higher;
# these are the widely-used mid-range values.
_KFACTOR_BY_RT: list[tuple[float, float]] = [
    # (r/t <= threshold, K)
    (0.5, 0.33),   # very tight bend: neutral axis near inside face
    (1.0, 0.35),
    (2.0, 0.40),
    (3.0, 0.43),
    (float("inf"), 0.45),  # generous radius: neutral axis approaches mid-thickness
]

_DEFAULT_KFACTOR = 0.44  # common default when r/t unknown (Machinery's Handbook)


def _k_for_rt(r: float, t: float) -> tuple[float, str]:
    if t <= 0:
        return _DEFAULT_KFACTOR, "r/t undefined (t=0) → default K"
    rt = r / t
    for thresh, k in _KFACTOR_BY_RT:
        if rt <= thresh:
            return k, f"r/t={rt:.2f} → K={k} (Machinery's Handbook bend table)"
    return _DEFAULT_KFACTOR, f"r/t={rt:.2f} → default K"


def bend_allowance(
    thickness_mm: float,
    inside_radius_mm: float,
    angle_deg: float = 90.0,
    k_factor: Optional[float] = None,
    prefilled: Optional[dict] = None,
) -> dict:
    """Bend allowance, outside setback, and bend deduction for one sheet-metal bend.

    Parameters
    ----------
    thickness_mm :     material (sheet) thickness ``t`` [mm].
    inside_radius_mm : inside bend radius ``r`` [mm] (often ≈ t for air bending).
    angle_deg :        bend angle ``θ`` [deg] — a 90° corner is θ=90 (default).
    k_factor :         override K; when ``None`` it is picked from the r/t table.
    prefilled :        §7 provenance dict.

    Returns
    -------
    ``aicad.calc/1`` envelope with ``answer.bend_allowance_mm``,
    ``answer.bend_deduction_mm``, ``answer.k_factor``.
    """
    t = float(thickness_mm)
    r = float(inside_radius_mm)
    theta = float(angle_deg)
    if t <= 0:
        return calc_fail("bad_query", "thickness_mm must be > 0.")
    if r < 0:
        return calc_fail("bad_query", "inside_radius_mm must be ≥ 0.")
    if not (0 < theta < 180):
        return calc_fail("bad_query", "angle_deg must be in (0, 180).")

    if k_factor is None:
        k, k_note = _k_for_rt(r, t)
    else:
        k = float(k_factor)
        if not (0.0 < k < 0.5):
            return calc_fail("bad_query", "k_factor must be in (0, 0.5).")
        k_note = f"K={k} (user-supplied)"

    theta_rad = math.radians(theta)
    ba = theta_rad * (r + k * t)
    ossb = math.tan(theta_rad / 2.0) * (r + t)
    bd = 2.0 * ossb - ba

    answer = {
        "bend_allowance_mm": round(ba, 3),
        "outside_setback_mm": round(ossb, 3),
        "bend_deduction_mm": round(bd, 3),
        "k_factor": round(k, 3),
        "note": (
            f"{k_note}; flat length per bend = leg_a + leg_b − bend_deduction. "
            f"BA = θ·(π/180)·(r + K·t)."
        ),
    }
    return calc_envelope(
        calc="bend_allowance",
        answer=answer,
        units="mm",
        equation="BA = θ·(π/180)·(r + K·t); BD = 2·tan(θ/2)·(r+t) − BA",
        inputs={
            "thickness_mm": t,
            "inside_radius_mm": r,
            "angle_deg": theta,
            **({"k_factor": k_factor} if k_factor is not None else {}),
        },
        source_kind="heuristic",
        citation=(
            "Machinery's Handbook 29th ed. (Bending Sheet Metal); "
            "SME Die Design Handbook — K-factor by r/t ratio."
        ),
        prefilled=prefilled,
    )


register(
    "bend_allowance",
    bend_allowance,
    input_schema={
        "thickness_mm":     {"type": "float", "unit": "mm",
                             "description": "Sheet thickness t [mm]",
                             "family": "length"},
        "inside_radius_mm": {"type": "float", "unit": "mm",
                             "description": "Inside bend radius r [mm]",
                             "family": "length"},
        "angle_deg":        {"type": "float", "unit": "deg",
                             "description": "Bend angle θ [deg] (90 for a right-angle bend)",
                             "family": "dimensionless"},
        "k_factor":         {"type": "float", "unit": None,
                             "description": "Neutral-axis K-factor (auto from r/t if omitted)",
                             "family": "dimensionless"},
    },
    prefill_hints={
        "thickness_mm": "wall_thickness",
    },
)
