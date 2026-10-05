"""Python section-table loaders for structural helpers.

Provides two lookups:
- :func:`get_aisc_section` — AISC W/S/C steel section properties from a curated JSON table.
- :func:`get_lumber_section` — Dressed lumber section properties (nominal → actual).

Data sources
------------
``services/structural/data/aisc_shapes/aisc_shapes_curated.json``
    Curated table of common AISC shapes.  Property values are dimensional facts (re-stated from
    widely published engineering references — Salmon & Johnson *Steel Structures*, NSAI, CISC
    Handbook) rather than a bulk copy of the AISC database compilation.  See
    ``contracts/licenses.md`` for the licensing posture.

``services/structural/data/lumber_table.json``
    Nominal-to-actual dressed (S4S) lumber dimensions per ANSI/AWC National Design Specification
    (NDS) and American Softwood Lumber Standard PS 20 — a public standard; values are physical facts.

Unit system: all outputs are in **mm / mm² / mm³ / mm⁴** to match the native solver's
N·mm·MPa convention. The C++ solver and its section tables live in ``core/structural/``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------- data paths

_HERE = Path(__file__).parent  # services/structural/
_AISC_PATH = _HERE / "data" / "aisc_shapes" / "aisc_shapes_curated.json"
_LUMBER_PATH = _HERE / "data" / "lumber_table.json"

# --------------------------------------------------------------------------- loaders


@lru_cache(maxsize=1)
def _load_aisc_db() -> dict:
    """Return the ``shapes`` dict from the curated AISC JSON (cached — file read once)."""
    with open(_AISC_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["shapes"]


@lru_cache(maxsize=1)
def _load_lumber_db() -> dict:
    """Return the ``lumber`` dict from the lumber JSON (cached — file read once)."""
    with open(_LUMBER_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["lumber"]


# --------------------------------------------------------------------------- public API


def get_aisc_section(name: str) -> dict:
    """Return section properties for an AISC shape by designation (case-insensitive).

    Parameters
    ----------
    name:
        AISC designation, e.g. ``"W8X31"``, ``"W8x31"``, ``"w8x31"``.
        The look-up is case-insensitive and ignores internal spaces.

    Returns
    -------
    dict with keys: ``A`` (mm²), ``d``, ``bf``, ``tf``, ``tw`` (mm),
    ``Ix``, ``Iy`` (mm⁴), ``Sx``, ``Sy`` (mm³), ``Zx``, ``Zy`` (mm³),
    ``J`` (mm⁴), ``Cw`` (mm⁶).

    Raises
    ------
    KeyError
        If the designation is not in the curated table.
    """
    db = _load_aisc_db()
    key = name.strip().upper().replace(" ", "")
    if key not in db:
        available = ", ".join(sorted(db.keys()))
        raise KeyError(
            f"AISC section {name!r} not in curated table. "
            f"Available: {available}. "
            "For unlisted shapes, supply section properties directly or extend the table."
        )
    row = dict(db[key])
    row.pop("_note", None)  # strip metadata keys
    return row


def get_lumber_section(nominal: str) -> dict:
    """Return section properties for a nominal dressed-lumber size.

    Parameters
    ----------
    nominal:
        Nominal size string, e.g. ``"2x4"``, ``"2X4"``, ``"4x6"``.
        Case-insensitive.  Spaces are stripped.

    Returns
    -------
    dict with keys:
    ``b_actual_mm`` (breadth, mm), ``d_actual_mm`` (depth, mm),
    ``A`` (mm²), ``Ix``, ``Iy`` (mm⁴), ``Sx``, ``Sy`` (mm³).

    Note: ``Sx`` / ``Sy`` use the strong/weak-axis convention where the depth ``d`` is the
    *strong* dimension (member bends about the axis through the breadth ``b``).

    Raises
    ------
    KeyError
        If the nominal size is not in the table.
    """
    db = _load_lumber_db()
    key = nominal.strip().upper().replace(" ", "")
    # Accept both "2X4" and "2x4"
    key_lower = nominal.strip().lower().replace(" ", "")
    if key_lower in db:
        row = db[key_lower]
    elif key in db:
        row = db[key]
    else:
        available = ", ".join(sorted(db.keys()))
        raise KeyError(
            f"Lumber size {nominal!r} not in table. Available: {available}."
        )
    # Normalise: expose A, Sx, Sy at top level (aliases from the JSON field names)
    out = {
        "b_actual_mm": row["b_actual_mm"],
        "d_actual_mm": row["d_actual_mm"],
        "A": row["A_mm2"],
        "Ix": row["Ix_mm4"],
        "Sx": row["Sx_mm3"],
        "Iy": row["Iy_mm4"],
        "Sy": row["Sy_mm3"],
    }
    return out
