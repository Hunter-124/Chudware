"""Minimum thread engagement length calculator.

Reference: Shigley's Mechanical Engineering Design, 10th ed., §8-6 (length of
thread engagement for equal strength of bolt and nut material); and the
standard engineering rule for dissimilar materials (steel into aluminium).

Method
------
The minimum engagement length is derived from the requirement that the
stripping strength of the internal thread (nut material) equals the bolt's
proof load capacity:

    L_e = (A_t · S_p_bolt) / (0.5 · π · d · tau_yield_internal)

where tau_yield_internal ≈ 0.577 · S_y_internal  (von Mises, Shigley §6-6).

For two components of the same material this simplifies to the rule of thumb
L_e ≈ 1.0 · d (for steel–steel) or L_e ≈ 1.5 · d (for aluminium–aluminium).
This calculator implements the full formula so it works for any material pairing.

Reference case:
    M6 × 1.0, class 8.8 bolt into 6061-T6 aluminium:
    A_t = 20.1 mm²  (Shigley Eq. 8-2)
    S_p = 600 MPa
    S_y_6061 = 276 MPa → τ_y = 0.577 × 276 = 159.3 MPa
    L_e = (20.1 × 600) / (0.5 × π × 6 × 159.3) ≈ 8.07 mm → rounds to ~9 mm
"""
from __future__ import annotations

import math
from typing import Optional

from .registry import calc_envelope, calc_fail, register
from .bolted_joint import tensile_stress_area, _COARSE_PITCH, _PROOF_STRENGTH

# ---------------------------------------------------------------------------
# Materials loader (mirrors machining.py pattern)
# ---------------------------------------------------------------------------

import json
import pathlib

_MAT_PATH = (pathlib.Path(__file__).parents[3] / "services" / "sim" / "materials.json")
_MATERIALS: Optional[dict] = None


def _materials() -> dict:
    global _MATERIALS
    if _MATERIALS is None:
        _MATERIALS = json.loads(_MAT_PATH.read_text(encoding="utf-8"))
    return _MATERIALS


def _yield_mpa(material_id: str) -> float:
    """Return yield_mpa for a material id from materials.json."""
    mats = _materials()
    entry = mats.get(material_id)
    if entry is None:
        known = sorted(mats.keys())
        raise ValueError(
            f"material_id {material_id!r} not found; known: {known}."
        )
    return float(entry["yield_mpa"])


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

def thread_engagement(
    d_mm: float,
    grade: str = "8.8",
    internal_material_id: str = "alu_6061",
    pitch_mm: Optional[float] = None,
    prefilled: Optional[dict] = None,
) -> dict:
    """Minimum thread engagement length for bolt–tapped-hole pairing.

    Parameters
    ----------
    d_mm :                  Nominal bolt diameter [mm].
    grade :                 ISO metric bolt grade (e.g. ``"8.8"``).
    internal_material_id :  Material key from materials.json for the tapped
                            component (the weaker, internal thread material).
    pitch_mm :              Thread pitch [mm]; defaults to ISO coarse series.
    prefilled :             §7 provenance dict.

    Returns
    -------
    aicad.calc/1 envelope with ``answer.min_engagement_mm``.

    Reference
    ---------
    Shigley §8-6; von Mises shear yield approximation §6-6.
    Work-order anchor: M6 into 6061 → ~9 mm.
    """
    try:
        d_mm = float(d_mm)
    except (TypeError, ValueError) as exc:
        return calc_fail("bad_query", f"thread_engagement: non-numeric d_mm — {exc}.")

    if d_mm <= 0:
        return calc_fail("bad_query", "d_mm must be > 0.")
    grade = str(grade).strip()
    if grade not in _PROOF_STRENGTH:
        return calc_fail("bad_query",
                         f"unsupported grade {grade!r}; known: {sorted(_PROOF_STRENGTH)}.")

    d_key = round(d_mm)
    if pitch_mm is None:
        pitch_mm = _COARSE_PITCH.get(d_key)
        if pitch_mm is None:
            return calc_fail(
                "bad_query",
                f"no default coarse pitch for d={d_mm} mm; provide pitch_mm explicitly.",
            )
    pitch_mm = float(pitch_mm)

    try:
        S_y_int = _yield_mpa(internal_material_id)
    except ValueError as exc:
        return calc_fail("bad_query", str(exc))

    S_p_bolt = _PROOF_STRENGTH[grade]
    A_t = tensile_stress_area(d_mm, pitch_mm)

    # Shear yield by von Mises criterion: τ_y = 0.577 · S_y
    tau_y_int = 0.577 * S_y_int

    # Minimum engagement from thread-strip vs bolt-proof equality:
    # L_e = (A_t · S_p) / (0.5 · π · d · τ_y)
    denom = 0.5 * math.pi * d_mm * tau_y_int
    if denom <= 0:
        return calc_fail("bad_value", "thread_engagement: zero denominator.")
    L_e = (A_t * S_p_bolt) / denom

    # Recommended: add 10% margin (common practice)
    L_e_rec = L_e * 1.1

    notes = [
        f"τ_y_internal = 0.577 × {S_y_int} MPa = {tau_y_int:.1f} MPa "
        f"(von Mises, Shigley §6-6).",
        f"Minimum (exact) {L_e:.2f} mm; recommended (+10% margin) {L_e_rec:.2f} mm.",
    ]

    return calc_envelope(
        calc="thread_engagement",
        answer={
            "min_engagement_mm":     round(L_e, 2),
            "recommended_engagement_mm": round(L_e_rec, 2),
            "A_t_mm2":               round(A_t, 2),
            "S_p_bolt_mpa":          S_p_bolt,
            "S_y_internal_mpa":      S_y_int,
            "tau_y_internal_mpa":    round(tau_y_int, 1),
            "internal_material":     internal_material_id,
        },
        units="mm",
        equation="Le=(At·Sp)/(0.5·π·d·τy); τy=0.577·Sy [von Mises]",
        inputs={
            "d_mm": d_mm,
            "grade": grade,
            "pitch_mm": pitch_mm,
            "internal_material_id": internal_material_id,
        },
        source_kind="closed_form",
        citation=(
            "Shigley's Mechanical Engineering Design, 10th ed., §8-6 "
            "(thread engagement length); §6-6 (von Mises shear yield criterion)."
        ),
        notes=notes,
        prefilled=prefilled,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    "thread_engagement",
    thread_engagement,
    input_schema={
        "d_mm": {"type": "float", "unit": "mm",
                 "description": "Nominal bolt diameter [mm]", "family": "length"},
        "grade": {"type": "string", "unit": None,
                  "description": "ISO metric bolt grade (e.g. '8.8')",
                  "family": "string"},
        "internal_material_id": {
            "type": "string", "unit": None,
            "description": "Material id of tapped component (from materials.json)",
            "family": "material_ref",
        },
        "pitch_mm": {"type": "float", "unit": "mm",
                     "description": "Thread pitch [mm]; defaults to ISO coarse",
                     "family": "length"},
    },
    prefill_hints={
        "internal_material_id": "material_id",
    },
)
