"""Cantilever snap-fit deflection and strain calculator.

Reference: Shigley's Mechanical Engineering Design, 10th ed., §4-1 (cantilever
deflection) combined with the snap-fit strain formula from:
  Bayer Material Science / DuPont Engineering Polymers Design Guide:
  "Snap-Fit Joints for Plastics — A Design Guide" (1998).

Also: Tres, "Designing Plastic Parts for Assembly," 7th ed., §6.

Geometry
--------
  Cantilever snap-fit beam:
  - Length  L [mm]
  - Thickness h [mm] (at root; tapered version not implemented)
  - Width  b [mm]

Key formulas (rectangular cross-section cantilever beam)
---------------------------------------------------------
  I = b·h³/12
  End deflection (Shigley §4-1):
      δ = F·L³ / (3·E·I)  =>  F = 3·E·I·δ / L³
  Bending strain at root (max fibre):
      ε = 1.5·h·δ / L²   (from M·c/I and δ=F·L³/(3EI))
  Snap-fit design criterion: ε ≤ ε_allow = ε_y × safety_factor
  where ε_y = S_y / E (yield strain) for the material.

The snap-fit is viable if the required deflection δ is ≤ δ_max such that
ε ≤ ε_allow.

Source kind: heuristic (material-dependent rule-of-thumb for ε_allow safety factor).
"""
from __future__ import annotations

import math
from typing import Optional

from .registry import calc_envelope, calc_fail, register

import json
import pathlib

_MAT_PATH = pathlib.Path(__file__).parents[2] / "sim" / "materials.json"
_MATERIALS: Optional[dict] = None


def _materials() -> dict:
    global _MATERIALS
    if _MATERIALS is None:
        _MATERIALS = json.loads(_MAT_PATH.read_text(encoding="utf-8"))
    return _MATERIALS


def _mat_props(material_id: str) -> tuple[float, float]:
    """Return (E_mpa, yield_mpa)."""
    mats = _materials()
    e = mats.get(material_id)
    if e is None:
        raise ValueError(
            f"material_id {material_id!r} not found; known: {sorted(mats.keys())}."
        )
    return float(e["E_mpa"]), float(e["yield_mpa"])


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

def snap_fit(
    L_mm: float,
    h_mm: float,
    b_mm: float,
    delta_mm: float,
    material_id: str = "abs",
    safety_factor: float = 0.6,
    prefilled: Optional[dict] = None,
) -> dict:
    """Cantilever snap-fit — deflection force and root strain check.

    Parameters
    ----------
    L_mm :          Snap-fit arm length [mm].
    h_mm :          Arm thickness at root [mm].
    b_mm :          Arm width [mm].
    delta_mm :      Required tip deflection (the "undercut") [mm].
    material_id :   Material from materials.json; needs E_mpa and yield_mpa.
    safety_factor : Allowable strain fraction (ε_allow = safety_factor × ε_y).
                    Default 0.6 (Bayer Design Guide: use 50–70% of yield strain).
    prefilled :     §7 provenance dict.

    Returns
    -------
    aicad.calc/1 envelope.  ``answer`` has:
        epsilon_root (root strain), epsilon_allowable, F_snap_n, viable (bool),
        margin_pct, I_mm4.
    """
    try:
        L_mm = float(L_mm)
        h_mm = float(h_mm)
        b_mm = float(b_mm)
        delta_mm = float(delta_mm)
        safety_factor = float(safety_factor)
    except (TypeError, ValueError) as exc:
        return calc_fail("bad_query", f"snap_fit: non-numeric param — {exc}.")

    for name, val in [("L_mm", L_mm), ("h_mm", h_mm), ("b_mm", b_mm), ("delta_mm", delta_mm)]:
        if val <= 0:
            return calc_fail("bad_query", f"snap_fit: {name} must be > 0.")
    if not (0 < safety_factor <= 1.0):
        return calc_fail("bad_query", "safety_factor must be in (0, 1].")

    try:
        E_mpa, Sy_mpa = _mat_props(material_id)
    except ValueError as exc:
        return calc_fail("bad_query", str(exc))

    I = b_mm * h_mm ** 3 / 12.0          # mm⁴
    epsilon_y = Sy_mpa / E_mpa           # yield strain (dimensionless)
    epsilon_allow = safety_factor * epsilon_y

    # Root strain for required deflection:
    # ε = 1.5·h·δ / L²  (Shigley §4-1 + snap-fit strain formula)
    epsilon_root = 1.5 * h_mm * delta_mm / (L_mm ** 2)

    # Snap force (F = 3·E·I·δ / L³)
    F_snap = 3.0 * E_mpa * I * delta_mm / (L_mm ** 3)

    viable = epsilon_root <= epsilon_allow
    margin = (epsilon_allow - epsilon_root) / epsilon_allow * 100.0

    notes = []
    if not viable:
        notes.append(
            f"Root strain {epsilon_root:.4f} exceeds allowable {epsilon_allow:.4f} "
            f"({safety_factor*100:.0f}% of yield strain {epsilon_y:.4f}). "
            "Increase L or reduce h/δ."
        )

    return calc_envelope(
        calc="snap_fit",
        answer={
            "epsilon_root":       round(epsilon_root, 5),
            "epsilon_allowable":  round(epsilon_allow, 5),
            "epsilon_yield":      round(epsilon_y, 5),
            "F_snap_n":           round(F_snap, 3),
            "I_mm4":              round(I, 4),
            "viable":             viable,
            "margin_pct":         round(margin, 1),
        },
        units="dimensionless (strain), N",
        equation="ε=1.5·h·δ/L²; F=3·E·I·δ/L³; ε_allow=sf×(Sy/E)",
        inputs={
            "L_mm": L_mm,
            "h_mm": h_mm,
            "b_mm": b_mm,
            "delta_mm": delta_mm,
            "material_id": material_id,
            "safety_factor": safety_factor,
        },
        source_kind="heuristic",
        citation=(
            "Shigley's Mechanical Engineering Design, 10th ed., §4-1 "
            "(cantilever deflection). "
            "Bayer Material Science, 'Snap-Fit Joints for Plastics — A Design Guide' (1998); "
            "Tres, Designing Plastic Parts for Assembly, 7th ed., §6."
        ),
        notes=notes,
        prefilled=prefilled,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    "snap_fit",
    snap_fit,
    input_schema={
        "L_mm":          {"type": "float", "unit": "mm",
                          "description": "Snap-fit arm length [mm]", "family": "length"},
        "h_mm":          {"type": "float", "unit": "mm",
                          "description": "Arm thickness at root [mm]", "family": "length"},
        "b_mm":          {"type": "float", "unit": "mm",
                          "description": "Arm width [mm]", "family": "length"},
        "delta_mm":      {"type": "float", "unit": "mm",
                          "description": "Required tip deflection (undercut) [mm]",
                          "family": "length"},
        "material_id":   {"type": "string", "unit": None,
                          "description": "Material id from materials.json",
                          "family": "material_ref"},
        "safety_factor": {"type": "float", "unit": None,
                          "description": "Allowable strain fraction (0–1, default 0.6)",
                          "family": "dimensionless"},
    },
    prefill_hints={
        "material_id": "material_id",
        "L_mm":        "bbox_z",
    },
)
