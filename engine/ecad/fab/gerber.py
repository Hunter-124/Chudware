"""RS-274X Gerber writer for aicad.pcb/1 boards.

Entry points:
    write_gerber_layer(board, layer_id) -> str
    write_gerbers(board, out_dir) -> {layer_id: path}

Format: %FSLAX46Y46*% (4.6, absolute), %MOMM*%.
Coordinates are in units of 1e-6 mm (6 decimal places stored as integers).
"""
from __future__ import annotations

import math
import os
from typing import Any

from engine.ecad.schema import copper_layer_ids, pad_world

# ── coordinate encoding ────────────────────────────────────────────────────────

_SCALE = 1_000_000  # 1 mm → 1_000_000 units (4.6 format → 10^6 units per mm)


def _fmt(mm: float) -> str:
    """Format a coordinate value as a 4.6 integer string (no decimal point)."""
    return str(round(mm * _SCALE))


# ── layer → file extension mapping ───────────────────────────────────────────

_LAYER_EXT: dict[str, str] = {
    # Standard Protel/KiCad copper + mask/silk/paste extensions (unique, fab-house standard)
    "F.Cu": ".GTL",    # front copper (Gerber Top Layer)
    "B.Cu": ".GBL",    # back copper  (Gerber Bottom Layer)
    "F.SilkS": ".GTO", # front silkscreen
    "B.SilkS": ".GBO", # back silkscreen
    "F.Mask": ".GTS",  # front solder mask
    "B.Mask": ".GBS",  # back solder mask
    "F.Paste": ".GTP", # front paste
    "B.Paste": ".GBP", # back paste
    # Board outline
    "Edge.Cuts": ".GM1",
    # Documentation / fab / courtyard layers — use distinct mechanical layer numbers
    "F.Fab": ".GM2",   # front fab annotation
    "B.Fab": ".GM3",   # back fab annotation
    "F.CrtYd": ".GM4", # front courtyard
    "B.CrtYd": ".GM5", # back courtyard
}

# Inner copper: In1.Cu → .G2, In2.Cu → .G3, ...
def _layer_ext(layer_id: str) -> str:
    if layer_id in _LAYER_EXT:
        return _LAYER_EXT[layer_id]
    import re
    m = re.match(r"^In(\d+)\.Cu$", layer_id)
    if m:
        n = int(m.group(1))
        return f".G{n + 1}"
    return f".G_{layer_id.replace('.', '_')}"


# ── aperture definitions ───────────────────────────────────────────────────────

class _ApertureTable:
    """Tracks apertures and assigns D-codes starting at D10."""

    def __init__(self) -> None:
        self._table: dict[tuple, int] = {}
        self._next_d = 10
        self._macros: list[str] = []  # %AM...% lines that must be emitted before %ADD%

    def _key_for_pad(self, pad: dict) -> tuple:
        shape = pad.get("shape", "rect")
        w, h = float(pad["size"][0]), float(pad["size"][1])
        rr = float(pad.get("roundrect_ratio", 0.0))
        return (shape, round(w, 6), round(h, 6), round(rr, 6))

    def _key_for_track(self, width: float) -> tuple:
        return ("circle", round(width, 6), round(width, 6), 0.0)

    def _key_for_via(self, diameter: float) -> tuple:
        return ("circle", round(diameter, 6), round(diameter, 6), 0.0)

    def get_or_add_pad(self, pad: dict) -> int:
        key = self._key_for_pad(pad)
        if key not in self._table:
            self._table[key] = self._next_d
            self._next_d += 1
        return self._table[key]

    def get_or_add_track(self, width: float) -> int:
        key = self._key_for_track(width)
        if key not in self._table:
            self._table[key] = self._next_d
            self._next_d += 1
        return self._table[key]

    def get_or_add_via(self, diameter: float) -> int:
        key = self._key_for_via(diameter)
        if key not in self._table:
            self._table[key] = self._next_d
            self._next_d += 1
        return self._table[key]

    def _aperture_def_line(self, key: tuple, d: int) -> tuple[list[str], str]:
        """Return (macro_lines, ADD_line) for this aperture key."""
        shape, w, h, rr = key
        macros: list[str] = []

        if shape == "circle":
            add = f"%ADD{d}C,{w:.6f}*%"
        elif shape == "oval":
            add = f"%ADD{d}O,{w:.6f}X{h:.6f}*%"
        elif shape in ("rect", "trapezoid"):
            add = f"%ADD{d}R,{w:.6f}X{h:.6f}*%"
        elif shape == "roundrect":
            # Use aperture macro for roundrect: 4 rounded corners via Moire is complex;
            # simplest compliant approach is MORC macro with outline polygon.
            # We approximate as an obround if square, else use outline polygon AM.
            name = f"RNDRCT{d}"
            # Build an outline AM: 4 line segments + 4 quarter-circle arc approximations
            # For simplicity we generate a polygon with 8 pts per corner (32 total) as AM primitive 4
            corner_r = min(w, h) * rr
            pts = _roundrect_polygon(w, h, corner_r, n_arc=8)
            # Primitive 4 = outline, rotation 0
            pts_str = "X".join(f"{x:.6f}X{y:.6f}" for x, y in pts)
            # close polygon (repeat first point)
            x0, y0 = pts[0]
            macro_body = f"4,1,{len(pts)},{pts_str}X{x0:.6f}X{y0:.6f}X0*"
            macros.append(f"%AM{name}*\n{macro_body}\n%")
            add = f"%ADD{d}{name},*%"
        else:
            # custom / unknown → fallback to bounding rect
            add = f"%ADD{d}R,{w:.6f}X{h:.6f}*%"

        return macros, add

    def header_lines(self) -> list[str]:
        """Return all %AM...% macros then %ADD...% lines in D-code order."""
        # Sort by d-code for deterministic output
        sorted_items = sorted(self._table.items(), key=lambda kv: kv[1])
        all_macros: list[str] = []
        add_lines: list[str] = []
        for key, d in sorted_items:
            macros, add = self._aperture_def_line(key, d)
            all_macros.extend(macros)
            add_lines.append(add)
        return all_macros + add_lines


def _roundrect_polygon(w: float, h: float, r: float, n_arc: int = 8) -> list[tuple[float, float]]:
    """Generate polygon vertices for a rounded rectangle centered at origin."""
    pts: list[tuple[float, float]] = []
    hw, hh = w / 2, h / 2
    r = min(r, hw, hh)
    corners = [
        (hw - r, hh - r, 0),      # top-right
        (-hw + r, hh - r, 90),    # top-left
        (-hw + r, -hh + r, 180),  # bottom-left
        (hw - r, -hh + r, 270),   # bottom-right
    ]
    for cx, cy, start_deg in corners:
        for i in range(n_arc + 1):
            a = math.radians(start_deg + i * 90 / n_arc)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


# ── Gerber layer writer ────────────────────────────────────────────────────────

def _is_copper(layer_id: str) -> bool:
    import re
    return bool(re.match(r"^(F|B|In\d+)\.Cu$", layer_id))


def write_gerber_layer(board: dict, layer_id: str) -> str:
    """Generate RS-274X Gerber content for one board layer.

    Returns the complete file as a string. All coordinates in mm, 4.6 format.
    """
    lines: list[str] = []
    ap = _ApertureTable()

    # ── Collect all operations so apertures are pre-registered before we emit anything ──
    # We do two passes: first collect to build aperture table, then emit.

    ops: list[Any] = []  # list of ('flash'|'stroke'|'region'|'select', ...)

    origin = board.get("origin", [0.0, 0.0])
    ox, oy = float(origin[0]), float(origin[1])

    def board_xy(x: float, y: float) -> tuple[float, float]:
        return (x - ox, y - oy)

    # ── Tracks on this layer ──────────────────────────────────────────────────
    for track in board.get("tracks", []):
        if track.get("layer") != layer_id:
            continue
        if track.get("kind", "segment") == "segment":
            d = ap.get_or_add_track(float(track["width"]))
            x1, y1 = board_xy(*track["p1"])
            x2, y2 = board_xy(*track["p2"])
            ops.append(("stroke", d, x1, y1, x2, y2))
        elif track.get("kind") == "arc":
            # Arc track: approximate with short chord strokes
            d = ap.get_or_add_track(float(track["width"]))
            cx, cy = float(track["center"][0]), float(track["center"][1])
            a0 = math.radians(float(track.get("a0", 0)))
            a1 = math.radians(float(track.get("a1", 90)))
            da = a1 - a0
            n = max(4, int(abs(da) / math.radians(5)))
            pts = []
            for i in range(n + 1):
                a = a0 + da * i / n
                r = math.dist([cx], [cy]) if False else math.sqrt(
                    (float(track["p1"][0]) - cx) ** 2 + (float(track["p1"][1]) - cy) ** 2
                )
                pts.append(board_xy(cx + r * math.cos(a), cy + r * math.sin(a)))
            for i in range(len(pts) - 1):
                ops.append(("stroke", d, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))

    # ── Vias on copper layers ─────────────────────────────────────────────────
    if _is_copper(layer_id):
        for via in board.get("vias", []):
            if layer_id not in via.get("layers", []):
                continue
            d = ap.get_or_add_via(float(via["diameter"]))
            x, y = board_xy(*via["at"])
            ops.append(("flash", d, x, y))

    # ── Pads on this layer ────────────────────────────────────────────────────
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            if layer_id not in pad.get("layers", []):
                continue
            d = ap.get_or_add_pad(pad)
            wx, wy = pad_world(fp, pad)
            wx, wy = board_xy(wx, wy)
            ops.append(("flash", d, wx, wy))

    # ── Zone fills (copper pours) as G36/G37 regions ──────────────────────────
    for zone in board.get("zones", []):
        if zone.get("layer") != layer_id:
            continue
        for poly in zone.get("filled_polys", []):
            if len(poly) < 3:
                continue
            pts = [board_xy(float(p[0]), float(p[1])) for p in poly]
            ops.append(("region", pts))

    # ── Board-level graphics on this layer ────────────────────────────────────
    for gfx in board.get("graphics", []):
        if gfx.get("layer") != layer_id:
            continue
        _collect_graphic_ops(gfx, ops, ap, board_xy)

    # ── Edge.Cuts outline ─────────────────────────────────────────────────────
    if layer_id == "Edge.Cuts":
        for shape in board.get("outline", {}).get("shapes", []):
            _collect_outline_op(shape, ops, ap, board_xy)

    # ── Footprint graphics on this layer ──────────────────────────────────────
    for fp in board.get("footprints", []):
        fp_pos = fp.get("pos", [0.0, 0.0])
        fp_angle = float(fp.get("angle", 0.0))
        fp_side = fp.get("side", "top")
        for gfx in fp.get("graphics", []):
            if gfx.get("layer") != layer_id:
                continue
            # Transform graphic to world coords
            _collect_fp_graphic_ops(gfx, fp_pos, fp_angle, fp_side, ops, ap, board_xy)

    # ── Now emit the file ─────────────────────────────────────────────────────
    # Header
    lines.append(f"%FSLAX46Y46*%")
    lines.append(f"%MOMM*%")

    # Aperture macros + definitions (must precede first use)
    lines.extend(ap.header_lines())

    # Operations
    current_d: int | None = None
    for op in ops:
        if op[0] == "stroke":
            _, d, x1, y1, x2, y2 = op
            if d != current_d:
                lines.append(f"D{d}*")
                current_d = d
            lines.append(f"X{_fmt(x1)}Y{_fmt(y1)}D02*")
            lines.append(f"X{_fmt(x2)}Y{_fmt(y2)}D01*")
        elif op[0] == "flash":
            _, d, x, y = op
            if d != current_d:
                lines.append(f"D{d}*")
                current_d = d
            lines.append(f"X{_fmt(x)}Y{_fmt(y)}D03*")
        elif op[0] == "region":
            _, pts = op
            lines.append("G36*")
            # Move to first point
            lines.append(f"X{_fmt(pts[0][0])}Y{_fmt(pts[0][1])}D02*")
            # Draw to each subsequent point
            for px, py in pts[1:]:
                lines.append(f"X{_fmt(px)}Y{_fmt(py)}D01*")
            # Close back to first
            lines.append(f"X{_fmt(pts[0][0])}Y{_fmt(pts[0][1])}D01*")
            lines.append("G37*")

    lines.append("M02*")
    return "\n".join(lines) + "\n"


def _collect_graphic_ops(
    gfx: dict,
    ops: list,
    ap: _ApertureTable,
    board_xy,
) -> None:
    """Collect ops for a board-level graphic shape."""
    gtype = gfx.get("type", "line")
    if gtype == "line":
        width = float(gfx.get("width", 0.1))
        d = ap.get_or_add_track(width)
        p1 = gfx.get("p1", [0, 0])
        p2 = gfx.get("p2", [0, 0])
        x1, y1 = board_xy(float(p1[0]), float(p1[1]))
        x2, y2 = board_xy(float(p2[0]), float(p2[1]))
        ops.append(("stroke", d, x1, y1, x2, y2))
    elif gtype == "circle":
        width = float(gfx.get("width", 0.1))
        d = ap.get_or_add_track(width)
        cx, cy = float(gfx["center"][0]), float(gfx["center"][1])
        r = float(gfx["radius"])
        # Approximate circle with 36 segments
        n = 36
        pts = [(cx + r * math.cos(2 * math.pi * i / n),
                cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
        for i in range(len(pts) - 1):
            x1, y1 = board_xy(*pts[i])
            x2, y2 = board_xy(*pts[i + 1])
            ops.append(("stroke", d, x1, y1, x2, y2))


def _collect_outline_op(shape: dict, ops: list, ap: _ApertureTable, board_xy) -> None:
    """Collect ops for Edge.Cuts outline shapes."""
    width = float(shape.get("width", 0.05))
    d = ap.get_or_add_track(width)
    stype = shape.get("type", "line")
    if stype == "line":
        p1 = shape.get("p1", [0, 0])
        p2 = shape.get("p2", [0, 0])
        x1, y1 = board_xy(float(p1[0]), float(p1[1]))
        x2, y2 = board_xy(float(p2[0]), float(p2[1]))
        ops.append(("stroke", d, x1, y1, x2, y2))
    elif stype == "arc":
        cx, cy = float(shape["center"][0]), float(shape["center"][1])
        r = float(shape["radius"])
        a0 = math.radians(float(shape.get("a0", 0)))
        a1 = math.radians(float(shape.get("a1", 90)))
        da = a1 - a0
        n = max(4, int(abs(da) / math.radians(5)))
        pts = []
        for i in range(n + 1):
            a = a0 + da * i / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        for i in range(len(pts) - 1):
            x1, y1 = board_xy(*pts[i])
            x2, y2 = board_xy(*pts[i + 1])
            ops.append(("stroke", d, x1, y1, x2, y2))
    elif stype == "circle":
        cx, cy = float(shape["center"][0]), float(shape["center"][1])
        r = float(shape["radius"])
        n = 36
        pts = [(cx + r * math.cos(2 * math.pi * i / n),
                cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
        for i in range(len(pts) - 1):
            x1, y1 = board_xy(*pts[i])
            x2, y2 = board_xy(*pts[i + 1])
            ops.append(("stroke", d, x1, y1, x2, y2))


def _collect_fp_graphic_ops(
    gfx: dict,
    fp_pos: list,
    fp_angle: float,
    fp_side: str,
    ops: list,
    ap: _ApertureTable,
    board_xy,
) -> None:
    """Collect ops for a footprint-local graphic, transformed to world."""
    from engine.ecad.schema import local_to_world
    mirror = "x" if fp_side == "bottom" else "none"

    def to_world(p: list) -> tuple[float, float]:
        return local_to_world(p, fp_pos, fp_angle, mirror)

    gtype = gfx.get("type", "line")
    width = float(gfx.get("width", 0.1))
    d = ap.get_or_add_track(width)

    if gtype == "line":
        p1 = to_world(gfx.get("p1", [0, 0]))
        p2 = to_world(gfx.get("p2", [0, 0]))
        x1, y1 = board_xy(*p1)
        x2, y2 = board_xy(*p2)
        ops.append(("stroke", d, x1, y1, x2, y2))
    elif gtype == "circle":
        # Approximate circle
        cx, cy = to_world(gfx.get("center", [0, 0]))
        r = float(gfx.get("radius", 1.0))
        n = 36
        raw_pts = [(float(gfx["center"][0]) + r * math.cos(2 * math.pi * i / n),
                    float(gfx["center"][1]) + r * math.sin(2 * math.pi * i / n))
                   for i in range(n + 1)]
        world_pts = [board_xy(*to_world(p)) for p in raw_pts]
        for i in range(len(world_pts) - 1):
            ops.append(("stroke", d, *world_pts[i], *world_pts[i + 1]))


# ── Multi-layer writer ────────────────────────────────────────────────────────

def write_gerbers(board: dict, out_dir: str) -> dict[str, str]:
    """Write Gerber files for all populated layers to out_dir.

    Returns a dict mapping layer_id -> absolute file path.
    """
    os.makedirs(out_dir, exist_ok=True)

    # Determine which layers are populated
    n_cu = board["stackup"]["copper_layers"]
    cu_layers = copper_layer_ids(n_cu)

    candidate_layers: list[str] = list(cu_layers) + [
        "F.SilkS", "B.SilkS",
        "F.Mask", "B.Mask",
        "F.Paste", "B.Paste",
        "F.Fab", "B.Fab",
        "Edge.Cuts",
    ]

    result: dict[str, str] = {}
    board_name = board.get("name", "board").replace(" ", "_")

    for layer_id in candidate_layers:
        content = write_gerber_layer(board, layer_id)
        ext = _layer_ext(layer_id)
        filename = f"{board_name}{ext}"
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        result[layer_id] = path

    return result
