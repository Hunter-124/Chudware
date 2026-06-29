"""Excellon drill file writer for aicad.pcb/1 boards.

Entry point:
    write_drill(board, out_dir) -> {"pth": path, "npth": path}

Format: METRIC,TZ; M48 header; tool table T1C0.600; X/Y hits; M30 end.
Plated (tht pads + vias) go to PTH file; non-plated (np_tht) go to NPTH file.
Round holes = hits; oval holes = G85 routed slots.
"""
from __future__ import annotations

import math
import os

from engine.ecad.schema import pad_world

# ── coordinate encoding ────────────────────────────────────────────────────────
# Excellon METRIC,TZ (trailing zeros) with 3.3 format by default; but KiCad uses
# METRIC,TZ with 3 integer digits and 3 fractional = 1 unit = 0.001 mm
_SCALE = 1000  # 1 mm → 1000 units (3.3 format)


def _fmt(mm: float) -> str:
    """Format coordinate in Excellon 3.3 METRIC TZ format (no decimal point)."""
    return str(round(mm * _SCALE))


# ── drill data collection ─────────────────────────────────────────────────────

def _collect_drills(board: dict) -> tuple[list[dict], list[dict]]:
    """Return (pth_drills, npth_drills) each as list of drill-hit dicts.

    Each dict has:
        diameter: float  (mm)
        x, y: float      (board world coords)
        shape: "round"|"oval"
        slot_end: (x, y) | None  (for oval/slot, the other end)
    """
    origin = board.get("origin", [0.0, 0.0])
    ox, oy = float(origin[0]), float(origin[1])

    pth: list[dict] = []
    npth: list[dict] = []

    def board_xy(x: float, y: float) -> tuple[float, float]:
        return (x - ox, y - oy)

    # Footprint pads
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            ptype = pad.get("type", "smd")
            if ptype not in ("tht", "np_tht"):
                continue
            drill = pad.get("drill")
            if not drill:
                continue

            wx, wy = pad_world(fp, pad)
            wx, wy = board_xy(wx, wy)

            dia = float(drill.get("diameter", 0.0))
            dshape = drill.get("shape", "round")

            # Oval slot: the slot runs along the longer axis
            slot_end = None
            if dshape == "oval":
                # size of the pad gives the slot extent; the oval drill offset field
                # may specify slot endpoint shift. For simplicity use pad size.
                pad_w, pad_h = float(pad["size"][0]), float(pad["size"][1])
                # Oval slot length = max(w,h) - min(w,h) + dia
                slot_len = max(pad_w, pad_h) - min(pad_w, pad_h)
                fp_angle = float(fp.get("angle", 0.0))
                if pad_w >= pad_h:
                    dx = slot_len / 2 * math.cos(math.radians(fp_angle))
                    dy = slot_len / 2 * math.sin(math.radians(fp_angle))
                else:
                    dx = slot_len / 2 * math.cos(math.radians(fp_angle + 90))
                    dy = slot_len / 2 * math.sin(math.radians(fp_angle + 90))
                slot_end = (wx + dx, wy + dy)
                # start is the other end
                wx_start = wx - dx
                wy_start = wy - dy
                hit = {
                    "diameter": dia,
                    "x": wx_start,
                    "y": wy_start,
                    "shape": "oval",
                    "slot_end": slot_end,
                }
            else:
                hit = {
                    "diameter": dia,
                    "x": wx,
                    "y": wy,
                    "shape": "round",
                    "slot_end": None,
                }

            if ptype == "np_tht":
                npth.append(hit)
            else:
                pth.append(hit)

    # Vias (always plated)
    for via in board.get("vias", []):
        vx, vy = board_xy(float(via["at"][0]), float(via["at"][1]))
        pth.append({
            "diameter": float(via["drill"]),
            "x": vx,
            "y": vy,
            "shape": "round",
            "slot_end": None,
        })

    return pth, npth


# ── Excellon file builder ──────────────────────────────────────────────────────

def _build_excellon(drills: list[dict], plated: bool) -> str:
    if not drills:
        # Return a valid but empty file
        lines = [
            "M48",
            "METRIC,TZ",
            "M95",
            "M30",
        ]
        return "\n".join(lines) + "\n"

    # Build tool table: group by diameter
    diameters = sorted({round(d["diameter"], 6) for d in drills})
    tool_map: dict[float, int] = {dia: i + 1 for i, dia in enumerate(diameters)}

    lines: list[str] = []
    lines.append("M48")
    lines.append("METRIC,TZ")
    if plated:
        lines.append("; Plated Through-Hole Drill File")
    else:
        lines.append("; Non-Plated Drill File")

    for dia, tnum in tool_map.items():
        lines.append(f"T{tnum:02d}C{dia:.3f}")

    lines.append("M95")  # end of header

    # Group drills by tool (diameter)
    from collections import defaultdict
    by_tool: dict[int, list[dict]] = defaultdict(list)
    for d in drills:
        t = tool_map[round(d["diameter"], 6)]
        by_tool[t].append(d)

    for tnum in sorted(by_tool.keys()):
        lines.append(f"T{tnum:02d}")
        for hit in by_tool[tnum]:
            if hit["shape"] == "round":
                lines.append(f"X{_fmt(hit['x'])}Y{_fmt(hit['y'])}")
            else:
                # Oval slot: G85 canned cycle (slot from X1Y1 to X2Y2)
                ex, ey = hit["slot_end"]
                x1, y1 = hit["x"], hit["y"]
                lines.append(f"G85X{_fmt(x1)}Y{_fmt(y1)}G85X{_fmt(ex)}Y{_fmt(ey)}")

    lines.append("M30")
    return "\n".join(lines) + "\n"


# ── Entry point ───────────────────────────────────────────────────────────────

def write_drill(board: dict, out_dir: str) -> dict[str, str]:
    """Write Excellon drill files for PTH and NPTH drills.

    Returns {"pth": path, "npth": path}.
    """
    os.makedirs(out_dir, exist_ok=True)

    pth_drills, npth_drills = _collect_drills(board)

    board_name = board.get("name", "board").replace(" ", "_")

    pth_path = os.path.join(out_dir, f"{board_name}-PTH.drl")
    npth_path = os.path.join(out_dir, f"{board_name}-NPTH.drl")

    with open(pth_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_build_excellon(pth_drills, plated=True))

    with open(npth_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_build_excellon(npth_drills, plated=False))

    return {"pth": pth_path, "npth": npth_path}
