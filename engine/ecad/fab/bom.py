"""Bill of Materials (BOM) writer for aicad.pcb/1 boards.

Entry point:
    write_bom(board, fmt="csv"|"json") -> str

Groups rows by (value, footprint, sorted user fields).
DNP parts excluded by default.
Columns: Refs, Qty, Value, Footprint, MPN, <user fields>.
Natural ref sorting (R1, R2, R10 order).
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict


# ── Natural sort key for ref strings ─────────────────────────────────────────

def _natural_key(ref: str) -> tuple:
    """Return sort key for natural ordering: R1 < R2 < R10."""
    parts = re.split(r"(\d+)", ref)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def _sort_refs(refs: list[str]) -> list[str]:
    return sorted(refs, key=_natural_key)


# ── BOM grouping ─────────────────────────────────────────────────────────────

def _group_footprints(board: dict, exclude_dnp: bool = True) -> list[dict]:
    """Group footprints into BOM rows.

    A group key is (value, footprint_id, frozenset of user-field items).
    Returns a list of row dicts with keys: Refs, Qty, Value, Footprint, MPN, + user fields.
    """
    # Collect all extra field names across footprints (beyond standard ones)
    _STANDARD_FIELDS = {"ref", "value", "footprint", "mpn", "dnp", "attr",
                        "model_3d", "id", "fpid", "linked_symbol", "pads",
                        "graphics", "pos", "angle", "side"}

    groups: dict[tuple, list[str]] = defaultdict(list)
    group_data: dict[tuple, dict] = {}

    for fp in board.get("footprints", []):
        ref = fp.get("ref", "?")
        value = fp.get("value", "")
        fpid = fp.get("fpid", "")
        attr = fp.get("attr", "smd")

        # fields dict: user-supplied extra fields (stored as 'fields' sub-dict or flat keys)
        fields: dict[str, str] = {}
        raw_fields = fp.get("fields", {})
        if isinstance(raw_fields, dict):
            for k, v in raw_fields.items():
                if k.lower() not in _STANDARD_FIELDS:
                    fields[k] = str(v)

        # DNP check
        dnp = bool(fp.get("dnp", False)) or attr == "virtual"
        if exclude_dnp and dnp:
            continue

        mpn = fields.pop("MPN", fields.pop("mpn", ""))
        key = (value, fpid, frozenset(fields.items()))

        groups[key].append(ref)
        if key not in group_data:
            group_data[key] = {
                "value": value,
                "footprint": fpid,
                "mpn": mpn,
                "fields": fields,
            }

    rows: list[dict] = []
    for key, refs in groups.items():
        data = group_data[key]
        sorted_refs = _sort_refs(refs)
        row: dict[str, object] = {
            "Refs": ",".join(sorted_refs),
            "Qty": len(refs),
            "Value": data["value"],
            "Footprint": data["footprint"],
            "MPN": data["mpn"],
        }
        # Add user fields as additional columns
        for fk, fv in sorted(data["fields"].items()):
            row[fk] = fv
        rows.append(row)

    # Sort rows by first ref in each group for stable output
    rows.sort(key=lambda r: _natural_key(r["Refs"].split(",")[0]))
    return rows


# ── Entry point ───────────────────────────────────────────────────────────────

def write_bom(board: dict, fmt: str = "csv", exclude_dnp: bool = True) -> str:
    """Generate BOM as CSV or JSON string.

    Args:
        board: aicad.pcb/1 board document.
        fmt: "csv" or "json".
        exclude_dnp: If True (default), omit footprints marked dnp or virtual.

    Returns:
        BOM content as a string.
    """
    rows = _group_footprints(board, exclude_dnp=exclude_dnp)

    if not rows:
        if fmt == "json":
            return json.dumps([], indent=2)
        return "Refs,Qty,Value,Footprint,MPN\n"

    # Collect all column names (standard + any user field columns)
    standard_cols = ["Refs", "Qty", "Value", "Footprint", "MPN"]
    extra_cols: list[str] = []
    seen_extra: set[str] = set()
    for row in rows:
        for k in row:
            if k not in standard_cols and k not in seen_extra:
                extra_cols.append(k)
                seen_extra.add(k)
    all_cols = standard_cols + sorted(extra_cols)

    if fmt == "json":
        # Normalize rows to have all columns
        normalized = []
        for row in rows:
            r = {col: row.get(col, "") for col in all_cols}
            normalized.append(r)
        return json.dumps(normalized, indent=2)

    # CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_cols, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        # Fill missing columns with empty string
        full_row = {col: row.get(col, "") for col in all_cols}
        writer.writerow(full_row)
    return buf.getvalue()
