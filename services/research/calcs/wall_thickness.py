"""Minimum wall thickness calculator by manufacturing process.

Three process families (Shigley + DFM references):

1. **machining** — chatter rule of thumb: wall ≥ (cutter_diameter / min_aspect_ratio).
   Practical minimum also varies by material. Ref: Kalpakjian & Schmid,
   "Manufacturing Engineering & Technology," §21 (milling chatter avoidance).
   Guideline: wall-to-height aspect ratio typically ≥ 3:1 (steel), ≥ 5:1 (aluminium)
   to avoid chatter.  Reformulated as min_wall = height / aspect_ratio.

2. **fdm_3dp** — FDM process minimum wall = 2 × nozzle_diameter, typically 0.8 mm
   for 0.4 mm nozzle. "Design for Additive Manufacturing" guideline;
   rule-of-thumb: t_min = 1.2 mm for structural walls at standard layer heights.
   Ref: Gibson, Rosen, Stucker, "Additive Manufacturing Technologies," §6.

3. **injection_molding** — nominal wall tied to material family and part size.
   Rule: uniform wall t in range given by material; abrupt changes cause sink marks.
   Ref: Bryce, "Plastic Injection Molding," Vol. 2, §4 (wall thickness guidelines).
   Typical ranges (mm): ABS 1.2–3.5, PP 0.8–3.8, PA(Nylon) 0.8–3.0.

Source kind: heuristic (these are process guidelines, not closed-form derivations).
"""
from __future__ import annotations

from typing import Optional

from .registry import calc_envelope, calc_fail, register

# ---------------------------------------------------------------------------
# Process data tables
# ---------------------------------------------------------------------------

# machining: material -> recommended minimum aspect ratio (wall_height / min_wall)
# Lower ratio = thicker wall required.
_MACHINING_ASPECT: dict[str, float] = {
    "steel":     3.0,   # conservative; Kalpakjian §21 chatter rule
    "aluminium": 5.0,   # Al is more compliant; thinner walls ok relative to depth
    "titanium":  2.5,   # Ti is gummy — requires stiffer walls
    "plastic":   4.0,
    "default":   3.0,
}

# injection_molding: material_family -> (min_mm, max_mm, nominal_mm)
# Ref: Bryce §4; DuPont Design Guide for injection moulding.
_IM_WALL: dict[str, tuple[float, float, float]] = {
    "abs":     (1.2, 3.5, 2.0),
    "pp":      (0.8, 3.8, 2.0),
    "pe":      (0.8, 3.0, 2.0),
    "pa":      (0.8, 3.0, 1.5),  # Nylon
    "nylon":   (0.8, 3.0, 1.5),
    "pc":      (1.0, 4.0, 2.5),  # Polycarbonate
    "pom":     (0.8, 3.0, 1.5),  # Acetal / Delrin
    "ps":      (1.0, 4.0, 2.5),
    "tpe":     (0.8, 3.0, 1.5),
    "default": (1.2, 4.0, 2.5),
}

# FDM: nozzle size -> min wall [mm]
# Rule: t_min = 2 × nozzle_dia (one perimeter each side)
_FDM_DEFAULT_NOZZLE_MM = 0.4
_FDM_STRUCTURAL_MIN_MM = 1.2  # guideline minimum for structural walls


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

def wall_thickness(
    process: str,
    feature_height_mm: Optional[float] = None,
    material_family: str = "default",
    nozzle_dia_mm: float = _FDM_DEFAULT_NOZZLE_MM,
    prefilled: Optional[dict] = None,
) -> dict:
    """Minimum wall thickness by manufacturing process.

    Parameters
    ----------
    process :            ``"machining"`` | ``"fdm_3dp"`` | ``"injection_molding"``
    feature_height_mm :  Required for machining (wall height being machined) [mm].
    material_family :    For machining: ``"steel"`` | ``"aluminium"`` | ``"titanium"``
                         | ``"plastic"`` | ``"default"``.
                         For injection_molding: ``"abs"`` | ``"pp"`` | ``"pc"``
                         | ``"pa"`` | ``"pom"`` | ... | ``"default"``.
                         Ignored for fdm_3dp.
    nozzle_dia_mm :      FDM nozzle diameter [mm] (default 0.4).
    prefilled :          §7 provenance dict.

    Returns
    -------
    aicad.calc/1 envelope with ``answer.min_wall_mm`` and process-specific fields.
    """
    proc = (process or "").strip().lower()
    mat  = (material_family or "default").strip().lower()

    if proc == "machining":
        if feature_height_mm is None:
            return calc_fail(
                "bad_query",
                "machining process requires feature_height_mm (the wall height being machined).",
            )
        h = float(feature_height_mm)
        if h <= 0:
            return calc_fail("bad_query", "feature_height_mm must be > 0.")
        aspect = _MACHINING_ASPECT.get(mat, _MACHINING_ASPECT["default"])
        min_wall = h / aspect
        # Absolute minimum regardless of aspect ratio
        abs_min = 0.5
        min_wall = max(min_wall, abs_min)
        answer = {
            "min_wall_mm": round(min_wall, 2),
            "aspect_ratio": aspect,
            "feature_height_mm": h,
            "material_family": mat,
            "note": (
                f"wall_height / min_wall ≥ {aspect}:1 chatter rule; "
                f"absolute min 0.5 mm."
            ),
        }
        eqn = "t_min = h / aspect_ratio (chatter avoidance heuristic)"
        citation = (
            "Kalpakjian & Schmid, Manufacturing Engineering & Technology, §21 "
            "(milling chatter avoidance); aspect ratio guidelines for thin-wall milling."
        )

    elif proc == "fdm_3dp":
        nozzle = float(nozzle_dia_mm)
        if nozzle <= 0:
            return calc_fail("bad_query", "nozzle_dia_mm must be > 0.")
        min_wall = max(2.0 * nozzle, _FDM_STRUCTURAL_MIN_MM)
        answer = {
            "min_wall_mm": round(min_wall, 2),
            "nozzle_dia_mm": nozzle,
            "structural_min_mm": _FDM_STRUCTURAL_MIN_MM,
            "note": "t_min = max(2×nozzle_dia, 1.2 mm) for structural integrity.",
        }
        eqn = "t_min = max(2 × nozzle_dia, 1.2 mm)"
        citation = (
            "Gibson, Rosen, Stucker, Additive Manufacturing Technologies, §6 "
            "(FDM process wall thickness guidelines)."
        )

    elif proc == "injection_molding":
        row = _IM_WALL.get(mat, _IM_WALL["default"])
        t_min, t_max, t_nom = row
        answer = {
            "min_wall_mm": t_min,
            "max_wall_mm": t_max,
            "nominal_wall_mm": t_nom,
            "material_family": mat,
            "note": (
                "Uniform wall recommended; abrupt changes cause sink marks. "
                "Ribs typically 60% of nominal wall thickness."
            ),
        }
        eqn = "range from injection moulding design table (material-family)"
        citation = (
            "Bryce, Plastic Injection Molding Vol. 2, §4 "
            "(wall thickness guidelines by material family)."
        )

    else:
        return calc_fail(
            "bad_query",
            f"unsupported process {process!r}; "
            "use 'machining', 'fdm_3dp', or 'injection_molding'.",
        )

    return calc_envelope(
        calc="wall_thickness",
        answer=answer,
        units="mm",
        equation=eqn,
        inputs={
            "process": process,
            "material_family": material_family,
            **({"feature_height_mm": feature_height_mm}
               if feature_height_mm is not None else {}),
            **({"nozzle_dia_mm": nozzle_dia_mm} if proc == "fdm_3dp" else {}),
        },
        source_kind="heuristic",
        citation=citation,
        prefilled=prefilled,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    "wall_thickness",
    wall_thickness,
    input_schema={
        "process":            {"type": "string", "unit": None,
                               "description": "machining | fdm_3dp | injection_molding",
                               "family": "string"},
        "feature_height_mm":  {"type": "float", "unit": "mm",
                               "description": "Wall height for machining [mm]",
                               "family": "length"},
        "material_family":    {"type": "string", "unit": None,
                               "description": "Material family (steel/abs/pp/...)",
                               "family": "string"},
        "nozzle_dia_mm":      {"type": "float", "unit": "mm",
                               "description": "FDM nozzle diameter [mm] (default 0.4)",
                               "family": "length"},
    },
    prefill_hints={
        "feature_height_mm": "bbox_z",
    },
)
