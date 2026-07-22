"""Structural BOM bridge — convert structural cost takeoff into BomLine format (T-D5).

Translates :func:`engine_oracle.structural.estimate.takeoff` output into
:class:`engine_oracle.bom.BomLine` records consumable by :func:`services.deliverables.joins.bom_rollup`.

All structural elements are ``kind="made"`` (produced from geometry + rate-table estimates, not
sourced from vendors).  Cost data lives in ``cost_model``; ``unit_cost`` / ``ext_cost`` are filled
from the rate table.  This is categorically different from bought-part prices — never merged with
or substituted for vendor provider prices.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from ..structural.estimate import TakeoffLine, estimate, fastener_count


@dataclass
class BomLine:
    """One line of a priced bill of materials (``aicad.sourcing/1`` §3).

    Restored locally 2026-07-22: ``engine_oracle/bom.py`` was deleted in T-RF18 (its
    pricing logic is C++ now) — the structural takeoff only needs the record shape,
    which services/deliverables/joins.bom_rollup consumes via ``asdict``.
    """

    line_id: str
    kind: str                                  # "made" | "bought"
    quantity: int = 1
    unit: str = "ea"
    part_ref: Optional[str] = None             # bought: "<supplier>::<part_number>" (VendorPart.key)
    vendor_part: Optional[dict] = None         # bought: resolved §1 record; else None
    cost_model: Optional[dict] = None          # made: cost-model result (T-B3); else None
    unit_cost: Optional[float] = None          # cost of one unit at `quantity`; None if unpriced
    ext_cost: Optional[float] = None           # unit_cost * quantity; None if unpriced
    currency: str = "USD"                      # ISO-4217 for unit_cost/ext_cost
    mpn: Optional[str] = None                  # manufacturer part number driving provider lookup
    description: str = ""                      # human-readable line description (order summary)


def _takeoff_to_bom_line(line: TakeoffLine, *, currency: str = "USD") -> BomLine:
    """Convert a single :class:`TakeoffLine` to a :class:`BomLine`."""
    cost_model = {
        "source": "struct_estimate",
        "quantity": line.quantity,
        "unit": line.unit,
        "unit_rate_usd": line.unit_rate,
        "material_cost_usd": line.material_cost,
        "labor_hours": line.labor_hours,
        "labor_cost_usd": line.labor_cost,
    }
    total_line_cost = round(line.material_cost + line.labor_cost, 2)
    return BomLine(
        line_id=line.element_id,
        kind="made",
        quantity=1,          # one structural element = one BOM line
        unit="ea",
        cost_model=cost_model,
        unit_cost=total_line_cost if total_line_cost > 0 else None,
        ext_cost=total_line_cost if total_line_cost > 0 else None,
        currency=currency,
        description=line.description,
    )


def structural_bom_lines(structure_doc) -> list[BomLine]:
    """Convert structural cost takeoff to a list of :class:`~engine_oracle.bom.BomLine` records.

    Each structural element becomes one ``kind="made"`` BomLine.  Fasteners are aggregated
    into a single additional line.  All costs come from the ENR rate table — these are
    preliminary budget figures, not vendor prices.

    Parameters
    ----------
    structure_doc:
        A :class:`engine_oracle.structural.elements.StructureDoc`.

    Returns
    -------
    list[BomLine]  One per element + one fastener aggregate line.  Suitable for
    passing to :func:`services.deliverables.joins.bom_rollup` (each line's
    ``{item, qty, unit_cost}`` dict is extracted in that join layer).
    """
    result = estimate(structure_doc)
    lines: list[BomLine] = [
        _takeoff_to_bom_line(ln, currency=result.currency)
        for ln in result.line_items
    ]
    # Fastener aggregate line
    fastener_bl = _takeoff_to_bom_line(result.fastener_line, currency=result.currency)
    lines.append(fastener_bl)
    return lines


def structural_bom_dicts(structure_doc) -> list[dict]:
    """Like :func:`structural_bom_lines` but returns plain dicts (for bom_rollup join layer).

    The join layer (:func:`services.deliverables.joins.bom_rollup`) expects each item as
    ``{item, qty, unit_cost?, source?}``.  This helper translates accordingly.
    """
    bom_lines = structural_bom_lines(structure_doc)
    out = []
    for bl in bom_lines:
        out.append({
            "item": bl.description or bl.line_id,
            "qty": bl.quantity,
            "unit_cost": bl.unit_cost,
            "source": "struct_estimate (ENR rate table)",
        })
    return out
