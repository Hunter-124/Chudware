"""Beam deflection and bending stress calculator.

Supports cantilever and simply-supported beams with point load or UDL.
Section types: rect (rectangular), round (solid circle), tube (hollow circle).

This module is also the **cross-check primitive for FEA verification**.
The pure compute functions (``section_props``, ``beam_calc_raw``) return plain
numbers; call them directly from FEA verification without parsing an envelope.

Reference: Shigley's Mechanical Engineering Design, 10th ed., §3-4 (Beam
deflection formulas) and §4-1 (area moment of inertia). Roark's Formulas for
Stress and Strain, 8th ed., Table 3-1 (beam cases).

Load-position assumptions
--------------------------
- cantilever + point:   load at the **free end** (x=L).
- cantilever + udl:     uniform distributed load along full length.
- simply_supported + point:  load at **mid-span** (x=L/2).
- simply_supported + udl:    uniform distributed load along full length.

SI units throughout: lengths mm, forces N, stress MPa (N/mm²), E MPa, I mm⁴.
"""
from __future__ import annotations

import math
from typing import Optional

from .registry import calc_envelope, calc_fail, register

# ---------------------------------------------------------------------------
# Section moment of inertia and neutral-axis distance
# ---------------------------------------------------------------------------

def section_props(section: str, **dims) -> tuple[float, float]:
    """Return (I_mm4, c_mm) for the given section type.

    Section types
    -------------
    rect :  requires b_mm (width), h_mm (height); bending about the x-axis.
            I = b·h³/12,  c = h/2.
            Ref: Shigley §A-18 Table A-18.
    round : requires d_mm (diameter); solid circular section.
            I = π·d⁴/64,  c = d/2.
            Ref: Shigley §A-18.
    tube :  requires D_mm (outer dia), d_mm (inner dia).
            I = π(D⁴−d⁴)/64,  c = D/2.
            Ref: Shigley §A-18.

    Raises ValueError for unknown section or bad dimensions.
    """
    s = section.strip().lower()
    if s == "rect":
        b = float(dims["b_mm"])
        h = float(dims["h_mm"])
        if b <= 0 or h <= 0:
            raise ValueError("rect section requires b_mm > 0 and h_mm > 0.")
        I = b * h ** 3 / 12.0
        c = h / 2.0
    elif s == "round":
        d = float(dims["d_mm"])
        if d <= 0:
            raise ValueError("round section requires d_mm > 0.")
        I = math.pi * d ** 4 / 64.0
        c = d / 2.0
    elif s == "tube":
        D = float(dims["D_mm"])
        d = float(dims["d_mm"])
        if D <= 0 or d < 0 or d >= D:
            raise ValueError("tube section requires D_mm > d_mm >= 0.")
        I = math.pi * (D ** 4 - d ** 4) / 64.0
        c = D / 2.0
    else:
        raise ValueError(f"unknown section type {section!r}; use 'rect', 'round', or 'tube'.")
    return I, c


# ---------------------------------------------------------------------------
# Pure beam compute function (FEA cross-check callable interface)
# ---------------------------------------------------------------------------

def beam_calc_raw(
    support: str,
    load: str,
    section: str,
    L_mm: float,
    E_mpa: float,
    F_n: float = 0.0,
    w_n_mm: float = 0.0,
    **section_dims,
) -> dict:
    """Compute beam deflection and bending stress; returns plain-number dict.

    This is the FEA cross-check primitive — call it without parsing an envelope.

    Parameters
    ----------
    support : ``"cantilever"`` | ``"simply_supported"``
    load :    ``"point"`` | ``"udl"``
    section : ``"rect"`` | ``"round"`` | ``"tube"``
    L_mm :    Beam span or length [mm]
    E_mpa :   Young's modulus [MPa]
    F_n :     Point load [N]  (used when load="point")
    w_n_mm :  Distributed load intensity [N/mm]  (used when load="udl")
    **section_dims :  Passed to ``section_props``.

    Returns
    -------
    dict with keys:
        I_mm4, c_mm, max_deflection_mm, max_moment_nmm, max_stress_mpa,
        support, load, section, L_mm, E_mpa

    Raises ValueError for bad inputs.

    References
    ----------
    Shigley §3-4; Roark Table 3-1 cases 1, 2, 6, 7.
    """
    sup = support.strip().lower()
    ld  = load.strip().lower()

    if sup not in ("cantilever", "simply_supported"):
        raise ValueError(f"support must be 'cantilever' or 'simply_supported', got {support!r}.")
    if ld not in ("point", "udl"):
        raise ValueError(f"load must be 'point' or 'udl', got {load!r}.")
    if L_mm <= 0:
        raise ValueError("L_mm must be > 0.")
    if E_mpa <= 0:
        raise ValueError("E_mpa must be > 0.")

    I, c = section_props(section, **section_dims)

    EI = E_mpa * I  # [N·mm²]

    if sup == "cantilever" and ld == "point":
        # Shigley §3-4, case: cantilever, end point load F at free end.
        # δ_max = F·L³ / (3·E·I)   [at free end]
        # M_max = F·L              [at fixed support]
        delta = F_n * L_mm ** 3 / (3.0 * EI)
        M_max = F_n * L_mm

    elif sup == "cantilever" and ld == "udl":
        # Shigley §3-4; Roark Table 3-1 case 2.
        # δ_max = w·L⁴ / (8·E·I)   [at free end]
        # M_max = w·L² / 2         [at fixed support]
        delta = w_n_mm * L_mm ** 4 / (8.0 * EI)
        M_max = w_n_mm * L_mm ** 2 / 2.0

    elif sup == "simply_supported" and ld == "point":
        # Shigley §3-4; load at mid-span (x = L/2).
        # δ_max = F·L³ / (48·E·I)
        # M_max = F·L / 4
        delta = F_n * L_mm ** 3 / (48.0 * EI)
        M_max = F_n * L_mm / 4.0

    else:  # simply_supported + udl
        # Shigley §3-4; Roark Table 3-1 case 6.
        # δ_max = 5·w·L⁴ / (384·E·I)
        # M_max = w·L² / 8
        delta = 5.0 * w_n_mm * L_mm ** 4 / (384.0 * EI)
        M_max = w_n_mm * L_mm ** 2 / 8.0

    sigma = M_max * c / I  # [MPa]

    return {
        "I_mm4": I,
        "c_mm": c,
        "max_deflection_mm": delta,
        "max_moment_nmm": M_max,
        "max_stress_mpa": sigma,
        "support": sup,
        "load": ld,
        "section": section.strip().lower(),
        "L_mm": L_mm,
        "E_mpa": E_mpa,
    }


# ---------------------------------------------------------------------------
# Envelope-wrapped calculator (MCP / registry callable)
# ---------------------------------------------------------------------------

def beam(
    support: str,
    load: str,
    section: str,
    L_mm: float,
    E_mpa: float,
    F_n: float = 0.0,
    w_n_mm: float = 0.0,
    prefilled: Optional[dict] = None,
    **section_dims,
) -> dict:
    """Beam deflection and bending stress — aicad.calc/1 envelope.

    Parameters mirror :func:`beam_calc_raw`.  ``prefilled`` records which
    inputs came from live geometry (§7 provenance).
    """
    try:
        r = beam_calc_raw(
            support=support,
            load=load,
            section=section,
            L_mm=float(L_mm),
            E_mpa=float(E_mpa),
            F_n=float(F_n),
            w_n_mm=float(w_n_mm),
            **section_dims,
        )
    except ValueError as exc:
        return calc_fail("bad_query", str(exc))
    except ZeroDivisionError:
        return calc_fail("bad_value", "beam: division by zero (I=0 or E=0).")

    if not math.isfinite(r["max_deflection_mm"]) or not math.isfinite(r["max_stress_mpa"]):
        return calc_fail("bad_value", "beam: non-finite result — check inputs.")

    # Build equation label
    load_sym = "F" if load.strip().lower() == "point" else "w"
    eqn_map = {
        ("cantilever", "point"): f"δ={load_sym}·L³/(3EI); M={load_sym}·L",
        ("cantilever", "udl"):   f"δ={load_sym}·L⁴/(8EI); M={load_sym}·L²/2",
        ("simply_supported", "point"): f"δ={load_sym}·L³/(48EI); M={load_sym}·L/4",
        ("simply_supported", "udl"):   f"δ=5{load_sym}·L⁴/(384EI); M={load_sym}·L²/8",
    }
    eqn = eqn_map.get((r["support"], r["load"]), "beam formula")

    inputs = {
        "support": support,
        "load": load,
        "section": section,
        "L_mm": L_mm,
        "E_mpa": E_mpa,
    }
    if load.strip().lower() == "point":
        inputs["F_n"] = F_n
    else:
        inputs["w_n_mm"] = w_n_mm
    inputs.update(section_dims)

    return calc_envelope(
        calc="beam",
        answer={
            "max_deflection_mm": round(r["max_deflection_mm"], 6),
            "max_stress_mpa":    round(r["max_stress_mpa"], 4),
            "max_moment_nmm":    round(r["max_moment_nmm"], 4),
            "I_mm4":             round(r["I_mm4"], 6),
            "c_mm":              round(r["c_mm"], 4),
        },
        units="mm, MPa",
        equation=eqn,
        inputs=inputs,
        source_kind="closed_form",
        citation=(
            "Shigley's Mechanical Engineering Design, 10th ed., §3-4 "
            "(deflection formulas); §A-18 (section properties). "
            "Roark's Formulas for Stress and Strain, 8th ed., Table 3-1."
        ),
        prefilled=prefilled,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    "beam",
    beam,
    input_schema={
        "support": {"type": "string", "unit": None,
                    "description": "cantilever | simply_supported",
                    "family": "string"},
        "load":    {"type": "string", "unit": None,
                    "description": "point | udl",
                    "family": "string"},
        "section": {"type": "string", "unit": None,
                    "description": "rect | round | tube",
                    "family": "string"},
        "L_mm":    {"type": "float", "unit": "mm",
                    "description": "Beam span [mm]",
                    "family": "length"},
        "E_mpa":   {"type": "float", "unit": "MPa",
                    "description": "Young's modulus [MPa]",
                    "family": "material_ref"},
        "F_n":     {"type": "float", "unit": "N",
                    "description": "Point load [N] (point load only)",
                    "family": "force"},
        "w_n_mm":  {"type": "float", "unit": "N/mm",
                    "description": "UDL intensity [N/mm] (udl only)",
                    "family": "force"},
        "b_mm":    {"type": "float", "unit": "mm",
                    "description": "Rect section width [mm]",
                    "family": "length"},
        "h_mm":    {"type": "float", "unit": "mm",
                    "description": "Rect section height [mm]",
                    "family": "length"},
        "d_mm":    {"type": "float", "unit": "mm",
                    "description": "Solid-round diameter OR tube inner dia [mm]",
                    "family": "length"},
        "D_mm":    {"type": "float", "unit": "mm",
                    "description": "Tube outer diameter [mm]",
                    "family": "length"},
    },
    prefill_hints={
        "L_mm": "bbox_z",          # span inferred from bounding-box longest dim
        "E_mpa": "material_id",    # from doc material assignment
    },
)
