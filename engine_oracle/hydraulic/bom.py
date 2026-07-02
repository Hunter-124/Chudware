"""BOM sourcing helpers for engine.hydraulic.

Emits sourcing-field dicts in the VendorPart-compatible shape so components
can be joined into bom_rollup at merge time (B-chain seam, T-E5).

VendorPart-compatible fields (engine/vendor_parts.py shape):
    id, type, name, mpn, manufacturer, description, quantity, unit, category
"""

from __future__ import annotations


def hydraulic_bom_lines(doc: dict) -> list[dict]:
    """Extract sourcing lines from a hydraulic circuit doc.

    Returns a list of dicts with VendorPart-compatible fields:
    ``{id, type, name, mpn, manufacturer, description, quantity, unit, category}``

    Components with no sourcing block are still emitted (with null fields) so the
    BOM roll-up can flag them as un-sourced. Components that set ``"suppressed": True``
    are excluded.
    """
    lines: list[dict] = []
    for comp in doc.get("components", []):
        if not isinstance(comp, dict):
            continue
        if comp.get("suppressed"):
            continue
        sourcing = comp.get("sourcing") or {}
        lines.append({
            "id": comp.get("id"),
            "type": comp.get("type"),
            "name": comp.get("name") or comp.get("type"),
            "mpn": sourcing.get("mpn"),
            "manufacturer": sourcing.get("manufacturer"),
            "description": sourcing.get("description"),
            "quantity": sourcing.get("quantity", 1),
            "unit": sourcing.get("unit", "ea"),
            "category": sourcing.get("category"),
        })
    return lines


def sourcing_summary(doc: dict) -> dict:
    """Count components by type and flag those missing MPNs.

    Returns::

        {
            "total": int,
            "by_type": {"pump": 1, "cylinder_linear": 2, ...},
            "missing_mpn": ["c1", "c3", ...],
            "sourced": int,
        }
    """
    lines = hydraulic_bom_lines(doc)
    by_type: dict[str, int] = {}
    missing_mpn: list[str] = []
    sourced = 0

    for line in lines:
        ctype = line.get("type") or "unknown"
        by_type[ctype] = by_type.get(ctype, 0) + 1
        if not line.get("mpn"):
            missing_mpn.append(line["id"])
        else:
            sourced += 1

    return {
        "total": len(lines),
        "by_type": by_type,
        "missing_mpn": missing_mpn,
        "sourced": sourced,
    }
