"""Unit-sanity range table — catches the ksi/MPa 6.9x class of unit confusion.

Also used by the requirements quantity validation. Table lives here, not in the battery,
so it can be imported independently.
"""
from __future__ import annotations

from typing import Any

RANGES: dict[str, tuple[str, float, float]] = {
    # key: (si_unit, min_val, max_val)
    "length_mm":     ("mm",    1e-3,   1e5),     # 1 micron .. 100 m
    "area_mm2":      ("mm²",   1e-6,   1e10),
    "volume_mm3":    ("mm³",   1e-9,   1e15),
    "mass_g":        ("g",     1e-6,   1e9),     # 1 microgram .. 1000 tonnes
    "stress_mpa":    ("MPa",   0.001,  5000.0),  # 1 kPa .. 5 GPa
    "force_n":       ("N",     1e-6,   1e9),
    "density_g_cm3": ("g/cm³", 0.1,    25.0),   # lightest aerogel .. osmium
    "angle_deg":     ("deg",   -360.0, 360.0),
    "temperature_c": ("°C",    -273.15, 5000.0),
}

# Map field-name patterns to range key (substring match, first match wins)
_FIELD_PATTERNS: list[tuple[str, str]] = [
    ("volume",   "volume_mm3"),
    ("area",     "area_mm2"),
    ("length",   "length_mm"),
    ("width",    "length_mm"),
    ("height",   "length_mm"),
    ("depth",    "length_mm"),
    ("radius",   "length_mm"),
    ("diameter", "length_mm"),
    ("mass",     "mass_g"),
    ("weight",   "mass_g"),
    ("density",  "density_g_cm3"),
    ("stress",   "stress_mpa"),
    ("mpa",      "stress_mpa"),
    ("psi",      "stress_mpa"),
    ("force",    "force_n"),
    ("angle",    "angle_deg"),
    ("temp",     "temperature_c"),
]


def _classify_field(field_name: str) -> str | None:
    fl = field_name.lower()
    for pat, kind in _FIELD_PATTERNS:
        if pat in fl:
            return kind
    return None


def _flatten(obj: Any, prefix: str = "") -> list[tuple[str, float]]:
    """Recursively flatten a nested dict/list to (dotted.key, float) pairs."""
    out: list[tuple[str, float]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_flatten(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, float(obj)))
    return out


def check_unit_ranges(state: dict) -> list[dict]:
    """Check all numeric fields in state against expected ranges.

    Returns list of violations: {field, value, kind, unit, min, max, hint}
    """
    violations: list[dict] = []
    for field, val in _flatten(state):
        kind = _classify_field(field)
        if kind is None:
            continue
        unit, lo, hi = RANGES[kind]
        if not (lo <= val <= hi):
            # Hint: detect the classic ksi/MPa confusion (factor ~6.9)
            hint = ""
            if kind == "stress_mpa":
                converted_up = val * 6.894757
                converted_dn = val / 6.894757
                if lo <= converted_up <= hi:
                    hint = "value looks like ksi — multiply by 6.895 to convert to MPa"
                elif lo <= converted_dn <= hi:
                    hint = "value looks like MPa expressed as ksi — divide by 6.895"
            violations.append({
                "field": field,
                "value": val,
                "kind": kind,
                "unit": unit,
                "min": lo,
                "max": hi,
                "hint": hint,
            })
    return violations
