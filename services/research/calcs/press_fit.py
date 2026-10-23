"""Press fit (interference fit) calculator — Lamé thick-wall cylinder equations.

Reference: Shigley's Mechanical Engineering Design, 10th ed., §3-16
(Thick-Wall Cylinders / Press and Shrink Fits) and Eq. 3-56 through 3-59.

Coordinate convention (EXPLICIT to avoid the factor-of-2 bug)
--------------------------------------------------------------
- ``interference_mm`` is the **diametral** interference δ_d (as returned by
  fits.py fit_calc, where clearance < 0 means interference):
      δ_d = shaft_OD − bore_ID  [mm, positive for a press fit]
- Lamé's formula needs the **radial** interference δ_r = δ_d / 2.
- This conversion is done explicitly inside the function.

Nomenclature follows Shigley §3-16:
  p   = interface pressure [MPa]
  d   = nominal interface diameter [mm]
  d_o = hub (outer component) outer diameter [mm]
  d_i = shaft (inner component) inner diameter [mm] (0 for solid shaft)
  E_o = hub Young's modulus [MPa]
  E_i = shaft Young's modulus [MPa]
  nu_o = hub Poisson's ratio
  nu_i = shaft Poisson's ratio
  δ_r  = radial interference [mm] = diametral_interference / 2
"""
from __future__ import annotations

import math
from typing import Optional

from .registry import calc_envelope, calc_fail, register

import json
import pathlib

_MAT_PATH = (pathlib.Path(__file__).parents[3] / "services" / "sim" / "materials.json")
_MATERIALS: Optional[dict] = None


def _materials() -> dict:
    global _MATERIALS
    if _MATERIALS is None:
        _MATERIALS = json.loads(_MAT_PATH.read_text(encoding="utf-8"))
    return _MATERIALS


def _mat_props(material_id: str) -> tuple[float, float, float]:
    """Return (E_mpa, nu, yield_mpa)."""
    mats = _materials()
    e = mats.get(material_id)
    if e is None:
        raise ValueError(
            f"material_id {material_id!r} not found; known: {sorted(mats.keys())}."
        )
    return float(e["E_mpa"]), float(e["nu"]), float(e["yield_mpa"])


# ---------------------------------------------------------------------------
# Pure compute (FEA cross-check callable)
# ---------------------------------------------------------------------------

def press_fit_raw(
    d_mm: float,
    d_o_mm: float,
    d_i_mm: float,
    interference_mm: float,
    E_o_mpa: float,
    nu_o: float,
    E_i_mpa: float,
    nu_i: float,
) -> dict:
    """Compute interface pressure, assembly force, and max stress — plain numbers.

    ``interference_mm`` is **diametral** (shaft_OD − bore_ID, positive = press).
    Returns:
        p_mpa, F_assembly_n, sigma_t_hub_mpa, sigma_t_shaft_mpa,
        delta_r_mm (radial interference used)
    """
    if d_mm <= 0:
        raise ValueError("d_mm (interface diameter) must be > 0.")
    if d_o_mm <= d_mm:
        raise ValueError(f"hub OD d_o_mm={d_o_mm} must be > interface d_mm={d_mm}.")
    if d_i_mm < 0 or d_i_mm >= d_mm:
        raise ValueError(f"shaft bore d_i_mm={d_i_mm} must be in [0, d_mm).")
    if interference_mm <= 0:
        raise ValueError("interference_mm must be > 0 (positive diametral interference).")

    r = d_mm / 2.0        # interface radius
    c = d_o_mm / 2.0      # hub outer radius
    a = d_i_mm / 2.0      # shaft bore radius (0 for solid shaft)

    delta_r = interference_mm / 2.0  # radial interference (Lamé input)

    # Lamé compliance factors:
    # Hub (outer):  B_o = (c²+r²)/(c²-r²) + nu_o
    # Shaft (inner, solid a=0): B_i = (r²+a²)/(r²-a²) - nu_i
    #   for solid shaft (a=0): B_i = 1 - nu_i
    denom_o = (c ** 2 - r ** 2)
    if denom_o <= 0:
        raise ValueError("Hub outer diameter too close to interface diameter.")

    B_o = (c ** 2 + r ** 2) / denom_o + nu_o  # Shigley Eq. 3-57 numerator terms

    if a == 0.0:
        B_i = 1.0 - nu_i                       # solid shaft case
    else:
        denom_i = (r ** 2 - a ** 2)
        if denom_i <= 0:
            raise ValueError("Shaft bore diameter equals interface diameter.")
        B_i = (r ** 2 + a ** 2) / denom_i - nu_i  # Shigley Eq. 3-58

    # Interface pressure (Shigley Eq. 3-56):
    # p = δ_r / (r · (B_o/E_o + B_i/E_i))
    p_mpa = delta_r / (r * (B_o / E_o_mpa + B_i / E_i_mpa))

    # Assembly force (friction-driven): approximate μ ≈ 0.12 (light lubrication)
    # F = π · d · L · μ · p  — caller can override L; we return force per unit length
    # (N/mm) since we don't know engagement length here.
    F_per_mm = math.pi * d_mm * 0.12 * p_mpa  # [N/mm] of engagement

    # Hoop (tangential) stress at bore of hub — maximum tensile stress in hub:
    # σ_t_hub = p · (c²+r²)/(c²-r²)   [Shigley Eq. 3-50, inner surface]
    sigma_t_hub = p_mpa * (c ** 2 + r ** 2) / (c ** 2 - r ** 2)

    # Hoop stress at OD of solid shaft (compressive):
    # σ_t_shaft = -2·p·r² / (r²-a²)  at outer surface of shaft
    if a == 0.0:
        sigma_t_shaft = -p_mpa  # uniform for solid shaft
    else:
        sigma_t_shaft = -2.0 * p_mpa * r ** 2 / (r ** 2 - a ** 2)

    return {
        "p_mpa": p_mpa,
        "F_assembly_per_mm_n": F_per_mm,
        "sigma_t_hub_mpa": sigma_t_hub,
        "sigma_t_shaft_mpa": sigma_t_shaft,
        "delta_r_mm": delta_r,
    }


# ---------------------------------------------------------------------------
# Envelope-wrapped calculator
# ---------------------------------------------------------------------------

def press_fit(
    d_mm: float,
    d_o_mm: float,
    interference_mm: float,
    hub_material_id: str = "steel_1018",
    shaft_material_id: str = "steel_1018",
    d_i_mm: float = 0.0,
    L_mm: Optional[float] = None,
    prefilled: Optional[dict] = None,
) -> dict:
    """Press fit interface pressure and assembly force — Lamé thick-wall.

    Parameters
    ----------
    d_mm :             Nominal interface (mating) diameter [mm].
    d_o_mm :           Hub (outer component) outer diameter [mm].
    interference_mm :  **Diametral** interference [mm] — shaft_OD minus bore_ID.
                       This is the |clearance| from fits.py when fit_character="press".
    hub_material_id :  Material id of hub (outer part) from materials.json.
    shaft_material_id: Material id of shaft (inner part).
    d_i_mm :           Shaft bore diameter [mm] (0 for solid shaft).
    L_mm :             Engagement length [mm] — used to compute total assembly force.
                       Optional; if omitted, only force-per-mm is returned.
    prefilled :        §7 provenance dict.

    Returns
    -------
    aicad.calc/1 envelope.
    """
    try:
        d_mm = float(d_mm)
        d_o_mm = float(d_o_mm)
        interference_mm = float(interference_mm)
        d_i_mm = float(d_i_mm)
    except (TypeError, ValueError) as exc:
        return calc_fail("bad_query", f"press_fit: non-numeric param — {exc}.")

    try:
        E_o, nu_o, Sy_o = _mat_props(hub_material_id)
        E_i, nu_i, Sy_i = _mat_props(shaft_material_id)
    except ValueError as exc:
        return calc_fail("bad_query", str(exc))

    try:
        r = press_fit_raw(d_mm, d_o_mm, d_i_mm, interference_mm, E_o, nu_o, E_i, nu_i)
    except ValueError as exc:
        return calc_fail("bad_query", str(exc))

    p = r["p_mpa"]
    notes: list[str] = []

    # Yield check for hub (von Mises at bore of hub)
    if r["sigma_t_hub_mpa"] > Sy_o:
        notes.append(
            f"Hub hoop stress {r['sigma_t_hub_mpa']:.1f} MPa exceeds "
            f"{hub_material_id} yield {Sy_o:.1f} MPa — hub may yield."
        )

    answer: dict = {
        "interface_pressure_mpa":     round(p, 3),
        "sigma_t_hub_mpa":            round(r["sigma_t_hub_mpa"], 2),
        "sigma_t_shaft_mpa":          round(r["sigma_t_shaft_mpa"], 2),
        "delta_r_mm":                 round(r["delta_r_mm"], 4),
        "F_assembly_per_mm_n":        round(r["F_assembly_per_mm_n"], 3),
        "hub_yield_mpa":              Sy_o,
        "shaft_yield_mpa":            Sy_i,
    }
    if L_mm is not None:
        L_mm = float(L_mm)
        answer["L_mm"] = L_mm
        answer["F_assembly_total_n"] = round(r["F_assembly_per_mm_n"] * L_mm, 1)

    notes.append(
        f"Diametral interference {interference_mm:.4f} mm → radial δ_r = "
        f"{r['delta_r_mm']:.4f} mm (Lamé input)."
    )
    notes.append("Assembly force uses μ=0.12 (light lubrication, Shigley §3-16 guidance).")

    return calc_envelope(
        calc="press_fit",
        answer=answer,
        units="MPa, N",
        equation="p=δr/(r·(Bo/Eo+Bi/Ei)); σt_hub=p·(c²+r²)/(c²-r²)",
        inputs={
            "d_mm": d_mm,
            "d_o_mm": d_o_mm,
            "d_i_mm": d_i_mm,
            "interference_mm": interference_mm,
            "hub_material_id": hub_material_id,
            "shaft_material_id": shaft_material_id,
            **({"L_mm": L_mm} if L_mm is not None else {}),
        },
        source_kind="closed_form",
        citation=(
            "Shigley's Mechanical Engineering Design, 10th ed., §3-16 "
            "(Eq. 3-56..3-59: Lamé thick-wall press/shrink fit)."
        ),
        notes=notes,
        prefilled=prefilled,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    "press_fit",
    press_fit,
    input_schema={
        "d_mm":                {"type": "float", "unit": "mm",
                                "description": "Nominal interface diameter [mm]",
                                "family": "length"},
        "d_o_mm":              {"type": "float", "unit": "mm",
                                "description": "Hub outer diameter [mm]",
                                "family": "length"},
        "interference_mm":     {"type": "float", "unit": "mm",
                                "description": "Diametral interference [mm]",
                                "family": "length"},
        "hub_material_id":     {"type": "string", "unit": None,
                                "description": "Hub material id (materials.json)",
                                "family": "material_ref"},
        "shaft_material_id":   {"type": "string", "unit": None,
                                "description": "Shaft material id (materials.json)",
                                "family": "material_ref"},
        "d_i_mm":              {"type": "float", "unit": "mm",
                                "description": "Shaft bore diameter [mm] (0=solid)",
                                "family": "length"},
        "L_mm":                {"type": "float", "unit": "mm",
                                "description": "Engagement length [mm] (optional)",
                                "family": "length"},
    },
    prefill_hints={
        "d_mm":   "cylinder_diameter",
        "d_o_mm": "cylinder_diameter",
        "L_mm":   "bbox_z",
    },
)
