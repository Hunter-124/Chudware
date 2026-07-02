"""Structural cost takeoff — board-feet, steel weight, fastener count, and line-item estimate.

Takes a :class:`engine_oracle.structural.elements.StructureDoc` (``aicad.structure/1``) and
produces a line-item cost estimate using the rate table in
``engine_oracle/data/enr_rates.json``.

Public API
----------
board_feet(element)          -> float
steel_weight_lbs(element)    -> float
fastener_count(structure_doc) -> int
takeoff(structure_doc)        -> list[TakeoffLine]
estimate(structure_doc)       -> EstimateResult

Rate table
----------
Seeded with ENR Q4-2025 national averages (see ``enr_rates.json``).  The user can override
any rate by editing that file — it is not shipped read-only.

Pricing honesty
---------------
All costs come from the user-editable rate table, NOT from the vendor sourcing provider chain
(``bom_price`` / ``parts_providers``).  They are *preliminary budget figures*, clearly labeled
as such in every output.  Never substitute fabricated numbers for provider prices.

Labor model
-----------
Labor hours are estimated from conservative RS Means Division 05/06 install-hour seed values
stored in ``enr_rates.json`` under ``_install_hours``.  These are user-overridable estimates —
not engineer-stamped quantities.  The disclaimer below is attached to every estimate output.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

DISCLAIMER = (
    "Preliminary cost estimate — not a contractor bid.  "
    "Rates are ENR national averages; verify with local suppliers before procurement."
)

# --------------------------------------------------------------------------- rate table

_RATES_PATH = Path(__file__).parent.parent / "data" / "enr_rates.json"

# Steel weight: lbs per linear foot for common AISC sections.
# Values derived from AISC Manual Table 1-1 (published lbs/ft is a dimensional fact from a
# public engineering reference).  Keyed by designation uppercase with no spaces.
_AISC_LBS_PER_FT: dict[str, float] = {
    "W4X13": 13.0, "W5X16": 16.0, "W6X9": 9.0, "W6X12": 12.0, "W6X15": 15.0,
    "W6X20": 20.0, "W8X10": 10.0, "W8X15": 15.0, "W8X18": 18.0, "W8X21": 21.0,
    "W8X24": 24.0, "W8X28": 28.0, "W8X31": 31.0, "W8X35": 35.0, "W8X40": 40.0,
    "W8X48": 48.0, "W10X12": 12.0, "W10X17": 17.0, "W10X22": 22.0, "W10X26": 26.0,
    "W10X30": 30.0, "W10X33": 33.0, "W10X39": 39.0, "W10X45": 45.0, "W10X49": 49.0,
    "W12X16": 16.0, "W12X19": 19.0, "W12X22": 22.0, "W12X26": 26.0, "W12X30": 30.0,
    "W12X35": 35.0, "W12X40": 40.0, "W14X22": 22.0, "W14X26": 26.0, "W14X30": 30.0,
    "W14X38": 38.0, "W14X48": 48.0, "W14X53": 53.0,
    # HSS columns (square/rectangular)
    "HSS4X4X1/4": 12.21, "HSS5X5X1/4": 15.62, "HSS6X6X1/4": 19.02,
}

# Connection fastener counts per joint type (conservative standard field practice).
# Post base = 4 anchor bolts; joist hanger = 10 nails/screws; knee brace = 4; splice = 6.
_FASTENERS_PER_JOINT: dict[str, int] = {
    "post_base": 4,
    "post_cap":  4,
    "joist_hanger": 10,
    "stud_sheathing": 6,  # per stud (top/bottom plate + 2 corners)
    "column_base": 4,
    "beam_splice": 6,
}


@lru_cache(maxsize=1)
def _load_rates() -> dict:
    with open(_RATES_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _rates() -> dict:
    return _load_rates()


# --------------------------------------------------------------------------- helpers


def _length_ft(element) -> float:
    """Convert element length_mm → decimal feet."""
    return element.length_mm / 304.8


def _is_lumber(material: str) -> bool:
    """Return True if the material is a wood/lumber species (not steel/concrete)."""
    mat = material.lower()
    return any(kw in mat for kw in (
        "lumber", "cedar", "pine", "fir", "spruce", "hemlock", "wood", "timber",
        "douglas", "larch", "redwood", "oak", "maple",
    ))


def _parse_nominal_lumber(section_name: str) -> Optional[tuple[int, int]]:
    """Parse ``"4x4"``-style nominal to (thickness_nom_in, width_nom_in), or None."""
    name = section_name.strip().upper().replace(" ", "")
    # Accept "4X4", "2X12", "6X6" etc.
    if "X" in name:
        parts = name.split("X", 1)
        try:
            t = int(parts[0])
            w = int(parts[1])
            return t, w
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- public API


def board_feet(element) -> float:
    """Board-feet for a lumber element.

    Uses **nominal** dimensions from the section_name string (e.g. ``"4x4"``), NOT the actual
    dressed dimensions.  Standard board-foot formula:

        BF = (thickness_nominal_in × width_nominal_in × length_ft) / 12

    Parameters
    ----------
    element:
        Any :class:`engine_oracle.structural.elements.StructuralElement` with
        ``section_name`` (e.g. ``"4x4"``) and ``length_mm``.

    Returns
    -------
    float  Board-feet (rounded to 4 decimal places).

    Raises
    ------
    ValueError
        If ``section_name`` cannot be parsed as a nominal lumber size.
    """
    dims = _parse_nominal_lumber(element.section_name)
    if dims is None:
        raise ValueError(
            f"Cannot parse nominal lumber size from section_name={element.section_name!r}. "
            "Expected format like '4x4', '2x10'."
        )
    t_in, w_in = dims
    length_ft = _length_ft(element)
    return round((t_in * w_in * length_ft) / 12.0, 4)


def steel_weight_lbs(element) -> float:
    """Weight of a steel structural element in pounds.

    Looks up lbs/ft for the section designation from the internal AISC table, then multiplies
    by element length in feet.

    Parameters
    ----------
    element:
        A :class:`~engine_oracle.structural.elements.StructuralElement` with ``section_name``
        matching an AISC designation (case-insensitive, spaces ignored, e.g. ``"W8X31"``).

    Returns
    -------
    float  Weight in pounds.

    Raises
    ------
    KeyError
        If the designation is not in the built-in lbs/ft table.
    """
    key = element.section_name.strip().upper().replace(" ", "")
    if key not in _AISC_LBS_PER_FT:
        raise KeyError(
            f"Steel section {key!r} not in lbs/ft table. "
            f"Available: {', '.join(sorted(_AISC_LBS_PER_FT.keys()))}. "
            "Add the section to _AISC_LBS_PER_FT or supply weight manually."
        )
    return _AISC_LBS_PER_FT[key] * _length_ft(element)


def fastener_count(structure_doc) -> int:
    """Estimate total fastener count for a structure document.

    Rules (per element type, conservative standard practice):
    - post/column:  4 anchor bolts at base + 4 at cap = 8
    - beam:         6 per splice (1 splice assumed per beam = 6)
    - joist:        10 per joist hanger (2 hangers per joist = 20)
    - stud_wall:    6 per stud (top plate + bottom plate nails)
    - footing:      4 anchor bolts per footing

    Parameters
    ----------
    structure_doc:
        A :class:`engine_oracle.structural.elements.StructureDoc`.

    Returns
    -------
    int  Estimated total fastener count.
    """
    total = 0
    for el in structure_doc.elements:
        et = el.element_type
        if et in ("post", "column"):
            total += _FASTENERS_PER_JOINT["post_base"] + _FASTENERS_PER_JOINT["post_cap"]
        elif et == "beam":
            total += _FASTENERS_PER_JOINT["beam_splice"]
        elif et == "joist":
            # Two joist hangers per joist
            total += 2 * _FASTENERS_PER_JOINT["joist_hanger"]
        elif et == "stud_wall":
            total += _FASTENERS_PER_JOINT["stud_sheathing"]
        elif et == "footing":
            total += _FASTENERS_PER_JOINT["column_base"]
    return total


@dataclass
class TakeoffLine:
    """One cost-takeoff line for a structural element.

    Fields
    ------
    element_id:      matches :attr:`StructuralElement.id`
    element_type:    "post" | "beam" | "column" | etc.
    section_name:    section designation (e.g. "4x4", "W8X31")
    material:        material description (e.g. "cedar", "steel")
    quantity:        physical quantity (BF for lumber, lbs for steel, ea for fasteners)
    unit:            "BF" | "lbs" | "ea" | "cy"
    unit_rate:       rate from enr_rates.json (USD per unit)
    material_cost:   quantity × unit_rate (USD)
    labor_hours:     estimated install-hours from _install_hours table
    labor_cost:      labor_hours × labor_per_hr rate (USD)
    description:     human-readable line label
    """
    element_id: str
    element_type: str
    section_name: str
    material: str
    quantity: float
    unit: str
    unit_rate: float
    material_cost: float
    labor_hours: float
    labor_cost: float
    description: str


@dataclass
class EstimateResult:
    """Full takeoff result for a structure doc."""
    line_items: list[TakeoffLine]
    fastener_line: TakeoffLine
    total_material_cost: float
    total_labor_cost: float
    total_cost: float
    currency: str
    rate_table_date: str
    disclaimer: str


def takeoff(structure_doc) -> list[TakeoffLine]:
    """Run all quantity takeoff for a structure document; multiply by rate table.

    Produces one :class:`TakeoffLine` per structural element.  Fasteners are handled
    separately via :func:`fastener_count` (returned as a single line in :func:`estimate`).

    Parameters
    ----------
    structure_doc:
        A :class:`engine_oracle.structural.elements.StructureDoc`.

    Returns
    -------
    list[TakeoffLine]  One line per element (not including fasteners).
    """
    rates = _rates()
    lumber_rate = float(rates["cedar_lumber_per_bf"])
    steel_rate = float(rates["steel_per_lb"])
    labor_rate = float(rates["labor_per_hr"])
    install_hours: dict[str, float] = {
        k: float(v)
        for k, v in rates.get("_install_hours", {}).items()
        if not k.startswith("_")
    }

    lines: list[TakeoffLine] = []
    for el in structure_doc.elements:
        et = el.element_type
        mat = el.material
        hrs = install_hours.get(et, 0.5)  # fallback 0.5 hr if type not in table

        if _is_lumber(mat):
            try:
                qty = board_feet(el)
            except ValueError:
                # Can't parse size — skip material cost but keep labor
                qty = 0.0
            unit = "BF"
            unit_rate = lumber_rate
            mat_cost = round(qty * unit_rate, 2)
        elif et in ("beam", "column") and not _is_lumber(mat):
            # Try AISC weight lookup; fall back to 0 (unestimated)
            try:
                qty = steel_weight_lbs(el)
            except KeyError:
                qty = 0.0
            unit = "lbs"
            unit_rate = steel_rate
            mat_cost = round(qty * unit_rate, 2)
        elif et == "footing":
            # Concrete: very rough estimate — footing volume not available from length alone
            # Return 0 material (can't size without plan dimensions), keep labor
            qty = 0.0
            unit = "cy"
            unit_rate = float(rates.get("concrete_per_cy", 145.0))
            mat_cost = 0.0
        else:
            qty = 0.0
            unit = "ea"
            unit_rate = 0.0
            mat_cost = 0.0

        labor_cost = round(hrs * labor_rate, 2)
        desc = f"{et.replace('_', ' ').title()} — {el.section_name} × {_length_ft(el):.2f} ft {mat}"

        lines.append(TakeoffLine(
            element_id=el.id,
            element_type=et,
            section_name=el.section_name,
            material=mat,
            quantity=round(qty, 4),
            unit=unit,
            unit_rate=unit_rate,
            material_cost=mat_cost,
            labor_hours=hrs,
            labor_cost=labor_cost,
            description=desc,
        ))
    return lines


def estimate(structure_doc) -> EstimateResult:
    """Full cost estimate: member takeoff + fasteners.

    Parameters
    ----------
    structure_doc:
        A :class:`engine_oracle.structural.elements.StructureDoc`.

    Returns
    -------
    :class:`EstimateResult`  with ``line_items``, ``fastener_line``, totals,
    ``rate_table_date``, and ``disclaimer``.
    """
    rates = _rates()
    labor_rate = float(rates["labor_per_hr"])
    fastener_rate = float(rates["fastener_per_ea"])

    lines = takeoff(structure_doc)

    # Fastener aggregate line
    n_fasteners = fastener_count(structure_doc)
    fastener_mat = round(n_fasteners * fastener_rate, 2)
    fastener_line = TakeoffLine(
        element_id="__fasteners__",
        element_type="fasteners",
        section_name="",
        material="hardware",
        quantity=float(n_fasteners),
        unit="ea",
        unit_rate=fastener_rate,
        material_cost=fastener_mat,
        labor_hours=0.0,   # labor already counted per-element above
        labor_cost=0.0,
        description=f"Structural fasteners — {n_fasteners} ea (anchor bolts, hangers, screws)",
    )

    total_mat = round(sum(ln.material_cost for ln in lines) + fastener_mat, 2)
    total_labor = round(sum(ln.labor_cost for ln in lines), 2)
    total = round(total_mat + total_labor, 2)

    return EstimateResult(
        line_items=lines,
        fastener_line=fastener_line,
        total_material_cost=total_mat,
        total_labor_cost=total_labor,
        total_cost=total,
        currency="USD",
        rate_table_date="ENR Q4-2025",
        disclaimer=DISCLAIMER,
    )
