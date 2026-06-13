"""Pick-and-place (CPL) writer for aicad.pcb/1 boards.

Entry point:
    write_placement(board, exclude_tht=True, exclude_virtual=True) -> str

CSV columns: Designator, MidX(mm), MidY(mm), Layer, Rotation
- Position = footprint pos minus board origin.
- Bottom side: rotation is mirrored (negated) per assembly convention.
- THT-only and virtual footprints excluded by default.
"""
from __future__ import annotations

import csv
import io
import re


# ── Natural sort for stable ref ordering ─────────────────────────────────────

def _natural_key(ref: str) -> tuple:
    parts = re.split(r"(\d+)", ref)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_tht_only(fp: dict) -> bool:
    """Return True if ALL copper pads are through-hole (tht or np_tht), no SMD."""
    pads = fp.get("pads", [])
    if not pads:
        return False
    return all(p.get("type", "smd") in ("tht", "np_tht") for p in pads)


def _is_virtual(fp: dict) -> bool:
    return fp.get("attr", "smd") == "virtual"


# ── Entry point ───────────────────────────────────────────────────────────────

def write_placement(
    board: dict,
    exclude_tht: bool = True,
    exclude_virtual: bool = True,
) -> str:
    """Generate pick-and-place CSV.

    Args:
        board: aicad.pcb/1 board document.
        exclude_tht: If True (default), skip THT-only footprints.
        exclude_virtual: If True (default), skip virtual footprints.

    Returns:
        CSV string with columns: Designator, MidX(mm), MidY(mm), Layer, Rotation.
    """
    origin = board.get("origin", [0.0, 0.0])
    ox, oy = float(origin[0]), float(origin[1])

    rows: list[dict] = []

    for fp in board.get("footprints", []):
        if exclude_virtual and _is_virtual(fp):
            continue
        if exclude_tht and _is_tht_only(fp):
            continue

        ref = fp.get("ref", "?")
        pos = fp.get("pos", [0.0, 0.0])
        angle = float(fp.get("angle", 0.0))
        side = fp.get("side", "top")

        mid_x = float(pos[0]) - ox
        mid_y = float(pos[1]) - oy

        # Bottom-side rotation convention: negate the angle so assembly pick
        # angles account for the board being flipped (mirror across X axis).
        if side == "bottom":
            rotation = -angle % 360
        else:
            rotation = angle % 360

        layer = "Top" if side == "top" else "Bottom"

        rows.append({
            "Designator": ref,
            "MidX(mm)": f"{mid_x:.4f}",
            "MidY(mm)": f"{mid_y:.4f}",
            "Layer": layer,
            "Rotation": f"{rotation:.4f}",
        })

    # Stable sort by designator (natural order)
    rows.sort(key=lambda r: _natural_key(r["Designator"]))

    buf = io.StringIO()
    fieldnames = ["Designator", "MidX(mm)", "MidY(mm)", "Layer", "Rotation"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
