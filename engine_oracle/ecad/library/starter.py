"""Clean-room starter library: programmatic symbol + footprint definitions.

All geometry is original (simple rectangles/lines for symbol bodies; IPC-7351
nominal pad dimensions which are a public standard). No KiCad library content
is included or derived.

Coordinate convention: mm, +y-up (math convention).
Pin angle = direction the pin sticks OUT from the body (connection-point side).
  • 180° = left side of body, pin sticks left  (connection point is to the LEFT)
  • 0°   = right side of body, pin sticks right
  • 90°  = top  (sticks up)
  • 270° = bottom (sticks down)

IPC-7351 nominal pad sizes used (public dimensional standard, not copyrightable):
  0402: pad 0.5×0.5 mm, pitch 1.0 mm
  0603: pad 0.9×0.9 mm (courtyard ~3.4×1.8)  — simplified nominal
  0805: pad 1.15×1.45 mm, pitch 2.0 mm
  1206: pad 1.60×1.80 mm, pitch 3.2 mm
  SOT-23: 3 pads 0.55×0.85 mm, pitch 0.95 mm
  SOIC-8: 8 pads 0.6×1.55 mm, pitch 1.27 mm, span 5.4 mm
  DIP-8:  8 pads (THT) 1.6×1.6 mm, pitch 2.54 mm, row-span 7.62 mm
  TO-92:  3 pads THT 1.5×1.5 mm, pitch 2.54 mm
  D_SMA/SMB: pads vary; simplified

Public API:
    SYMBOLS: dict[libid, SymbolDef]
    FOOTPRINTS: dict[fpid, footprint-def]
    get_symbol(libid) -> SymbolDef | None
    get_footprint(fpid) -> dict | None
"""

from __future__ import annotations

import math

# ─── small helpers ────────────────────────────────────────────────────────────

def _pin(number: str, name: str, pos: list, angle: float, ptype: str,
         length: float = 2.54, unit: int = 1) -> dict:
    return {
        "number": number,
        "name": name,
        "pos": pos,
        "angle": angle,
        "length": length,
        "type": ptype,
        "shape": "line",
        "unit": unit,
    }


# Standard silkscreen stroke width (mm). Fab minimum is ~0.15; generated footprint
# graphics MUST carry it, or DFM's silk_min_width flags every line as a 0mm stroke.
_SILK_W = 0.15


def _rect(x1: float, y1: float, x2: float, y2: float) -> dict:
    return {"type": "rect", "p1": [x1, y1], "p2": [x2, y2],
            "layer": "F.SilkS", "width": _SILK_W}


def _line(x1: float, y1: float, x2: float, y2: float) -> dict:
    return {"type": "line", "p1": [x1, y1], "p2": [x2, y2],
            "layer": "F.SilkS", "width": _SILK_W}


def _circle(cx: float, cy: float, r: float) -> dict:
    return {"type": "circle", "center": [cx, cy], "radius": r,
            "layer": "F.SilkS", "width": _SILK_W}


def _polyline(*pts) -> dict:
    return {"type": "polyline", "pts": list(pts), "layer": "F.SilkS", "width": _SILK_W}


def _smd_pad(pid: str, number: str, x: float, y: float,
             w: float, h: float, rr: float = 0.0) -> dict:
    shape = "roundrect" if rr > 0 else "rect"
    return {
        "id": pid,
        "number": number,
        "pos": [x, y],
        "angle": 0.0,
        "shape": shape,
        "size": [w, h],
        "roundrect_ratio": rr,
        "type": "smd",
        "layers": ["F.Cu", "F.Mask", "F.Paste"],
        "drill": None,
        "net": None,
        "pin": number,
    }


def _tht_pad(pid: str, number: str, x: float, y: float,
             drill: float, pad_w: float, pad_h: float,
             shape: str = "circle") -> dict:
    return {
        "id": pid,
        "number": number,
        "pos": [x, y],
        "angle": 0.0,
        "shape": shape,
        "size": [pad_w, pad_h],
        "roundrect_ratio": 0.0,
        "type": "tht",
        "layers": ["F.Cu", "B.Cu", "F.Mask", "B.Mask"],
        "drill": {"diameter": drill, "shape": "round", "offset": [0.0, 0.0]},
        "net": None,
        "pin": number,
    }


def _fp(fpid: str, pads: list, graphics: list, attr: str = "smd",
        description: str = "", eng: dict | None = None) -> dict:
    d = {
        "fpid": fpid,
        "pads": pads,
        "graphics": graphics,
        "model_3d": None,
        "model_3d_transform": None,
        "attr": attr,
        "description": description,
    }
    if eng:
        d["eng"] = eng
    return d


def _pkg_eng(shape: str, *, l_mm: float | None = None, w_mm: float | None = None,
             h_mm: float | None = None, d_mm: float | None = None,
             theta_ja: float | None = None, source: str = "") -> dict:
    """Footprint-level engineering metadata: package body dims + package theta_ja.

    Dims are the nominal PLASTIC BODY (not lead span). theta_ja is the JEDEC-style
    single-part-on-standard-board value — a layout-dependent APPROXIMATION carried with a
    source note; downstream thermal handoff (boardphys consumers) must treat it as a seed.
    Unknown values stay None (contracts/ecad/boardphys.md honesty rule).
    """
    eng: dict = {"package": {"shape": shape, "l_mm": l_mm, "w_mm": w_mm,
                             "h_mm": h_mm, "d_mm": d_mm}}
    if theta_ja is not None:
        eng["theta_c_per_w"] = {"ja": theta_ja}
    if source:
        eng["sources"] = [source]
    return eng


def _sym(libid: str, name: str, ref_prefix: str, value: str,
         description: str, keywords: str,
         pins: list, graphics: list,
         units_count: int = 1, power: bool = False,
         sim: dict | None = None,
         footprint_filters: list | None = None,
         eng: dict | None = None) -> dict:
    d: dict = {
        "libid": libid,
        "name": name,
        "description": description,
        "keywords": keywords,
        "reference_prefix": ref_prefix,
        "value": value,
        "footprint_filters": footprint_filters or [],
        "pins": pins,
        "graphics": graphics,
        "units_count": units_count,
        "power": power,
    }
    if sim:
        d["sim"] = sim
    if eng:
        d["eng"] = eng
    return d


# ─── SYMBOLS ─────────────────────────────────────────────────────────────────
# Body box half-widths: most passives use ±1.27 mm in x, ±2.54 in y.
# ICs use taller boxes scaled to pin count.

def _make_symbols() -> dict:
    syms: dict[str, dict] = {}

    # ── Resistor ────────────────────────────────────────────────────────────
    # Body: zigzag represented as a rectangle 2.54×1.27 centred at origin
    # Pin 1: left (angle 180°), Pin 2: right (angle 0°)
    syms["Device:R"] = _sym(
        "Device:R", "R", "R", "R",
        "Resistor",
        "res resistor",
        pins=[
            _pin("1", "~", [-2.54, 0.0], 180.0, "passive"),
            _pin("2", "~", [2.54, 0.0], 0.0, "passive"),
        ],
        graphics=[
            _rect(-1.016, -0.635, 1.016, 0.635),
            _line(-2.54, 0.0, -1.016, 0.0),
            _line(1.016, 0.0, 2.54, 0.0),
        ],
        sim={"model": "R", "pin_order": ["1", "2"]},
        footprint_filters=["R_*"],
    )

    # ── Capacitor ───────────────────────────────────────────────────────────
    syms["Device:C"] = _sym(
        "Device:C", "C", "C", "C",
        "Capacitor, non-polarised",
        "cap capacitor",
        pins=[
            _pin("1", "~", [0.0, 2.54], 90.0, "passive"),
            _pin("2", "~", [0.0, -2.54], 270.0, "passive"),
        ],
        graphics=[
            _line(-2.0, 0.5, 2.0, 0.5),
            _line(-2.0, -0.5, 2.0, -0.5),
            _line(0.0, 2.54, 0.0, 0.5),
            _line(0.0, -0.5, 0.0, -2.54),
        ],
        sim={"model": "C", "pin_order": ["1", "2"]},
        footprint_filters=["C_*"],
    )

    # ── Capacitor polarised ─────────────────────────────────────────────────
    syms["Device:C_pol"] = _sym(
        "Device:C_pol", "C_pol", "C", "C",
        "Polarised capacitor",
        "cap capacitor electrolytic polarised",
        pins=[
            _pin("1", "+", [0.0, 2.54], 90.0, "passive"),
            _pin("2", "-", [0.0, -2.54], 270.0, "passive"),
        ],
        graphics=[
            _line(-2.0, 0.5, 2.0, 0.5),
            _line(-2.0, -0.5, 2.0, -0.5),
            _line(0.0, 2.54, 0.0, 0.5),
            _line(0.0, -0.5, 0.0, -2.54),
            # + marker near pin 1
            _line(-0.5, 1.2, 0.5, 1.2),
            _line(0.0, 0.7, 0.0, 1.7),
        ],
        sim={"model": "C", "pin_order": ["1", "2"]},
        footprint_filters=["CP_*", "C_*"],
    )

    # ── Inductor ─────────────────────────────────────────────────────────────
    syms["Device:L"] = _sym(
        "Device:L", "L", "L", "L",
        "Inductor",
        "ind inductor coil choke",
        pins=[
            _pin("1", "~", [0.0, 2.54], 90.0, "passive"),
            _pin("2", "~", [0.0, -2.54], 270.0, "passive"),
        ],
        graphics=[
            # three arcs as a polyline approximation
            _polyline([0.0, 2.54], [0.0, 1.8], [0.7, 1.2], [0.0, 0.6],
                      [-0.7, 0.0], [0.0, -0.6], [0.7, -1.2], [0.0, -1.8],
                      [0.0, -2.54]),
        ],
        sim={"model": "L", "pin_order": ["1", "2"]},
        footprint_filters=["L_*"],
    )

    # ── Diode ────────────────────────────────────────────────────────────────
    syms["Device:D"] = _sym(
        "Device:D", "D", "D", "D",
        "Diode",
        "diode rectifier",
        pins=[
            _pin("K", "K", [2.54, 0.0], 0.0, "passive"),
            _pin("A", "A", [-2.54, 0.0], 180.0, "passive"),
        ],
        graphics=[
            # triangle pointing right (anode left → cathode right)
            _polyline([-1.27, -1.27], [-1.27, 1.27], [1.27, 0.0], [-1.27, -1.27]),
            # cathode bar
            _line(1.27, -1.27, 1.27, 1.27),
            _line(-2.54, 0.0, -1.27, 0.0),
            _line(1.27, 0.0, 2.54, 0.0),
        ],
        sim={"model": "D", "pin_order": ["A", "K"]},
        footprint_filters=["D_*"],
    )

    # ── LED ──────────────────────────────────────────────────────────────────
    syms["Device:LED"] = _sym(
        "Device:LED", "LED", "D", "LED",
        "Light-emitting diode",
        "led diode light",
        pins=[
            _pin("K", "K", [2.54, 0.0], 0.0, "passive"),
            _pin("A", "A", [-2.54, 0.0], 180.0, "passive"),
        ],
        graphics=[
            _polyline([-1.27, -1.27], [-1.27, 1.27], [1.27, 0.0], [-1.27, -1.27]),
            _line(1.27, -1.27, 1.27, 1.27),
            _line(-2.54, 0.0, -1.27, 0.0),
            _line(1.27, 0.0, 2.54, 0.0),
            # emission arrows
            _line(0.5, 1.5, 1.5, 2.5),
            _line(1.5, 2.5, 1.1, 2.5),
            _line(1.5, 2.5, 1.5, 2.1),
            _line(0.0, 1.8, 1.0, 2.8),
            _line(1.0, 2.8, 0.6, 2.8),
            _line(1.0, 2.8, 1.0, 2.4),
        ],
        sim={"model": "D", "pin_order": ["A", "K"]},
        footprint_filters=["LED_*"],
    )

    # ── Zener diode ─────────────────────────────────────────────────────────
    syms["Device:Zener"] = _sym(
        "Device:Zener", "Zener", "D", "D",
        "Zener diode",
        "zener diode",
        pins=[
            _pin("K", "K", [2.54, 0.0], 0.0, "passive"),
            _pin("A", "A", [-2.54, 0.0], 180.0, "passive"),
        ],
        graphics=[
            _polyline([-1.27, -1.27], [-1.27, 1.27], [1.27, 0.0], [-1.27, -1.27]),
            # Zener bar with bends
            _polyline([1.27, -1.27], [1.0, -1.6], [1.27, -1.27],
                      [1.27, 1.27], [1.5, 1.6]),
            _line(-2.54, 0.0, -1.27, 0.0),
            _line(1.27, 0.0, 2.54, 0.0),
        ],
        sim={"model": "D", "pin_order": ["A", "K"]},
        footprint_filters=["D_*"],
    )

    # ── NPN BJT ──────────────────────────────────────────────────────────────
    syms["Device:Q_NPN"] = _sym(
        "Device:Q_NPN", "Q_NPN", "Q", "Q",
        "NPN bipolar transistor",
        "npn bjt transistor",
        pins=[
            _pin("B", "B", [-3.81, 0.0], 180.0, "input"),
            _pin("C", "C", [0.0, 3.81], 90.0, "output"),
            _pin("E", "E", [0.0, -3.81], 270.0, "output"),
        ],
        graphics=[
            # base line
            _line(-1.27, -2.54, -1.27, 2.54),
            # collector & emitter lines
            _line(-1.27, 1.27, 0.0, 2.54),
            _line(-1.27, -1.27, 0.0, -2.54),
            # base connection
            _line(-3.81, 0.0, -1.27, 0.0),
            # collector to pin
            _line(0.0, 2.54, 0.0, 3.81),
            # emitter to pin (with arrow)
            _line(0.0, -2.54, 0.0, -3.81),
            _circle(0.0, 0.0, 2.2),
        ],
        sim={"model": "NPN", "pin_order": ["B", "C", "E"]},
        footprint_filters=["TO-92*", "SOT-23*"],
    )

    # ── PNP BJT ──────────────────────────────────────────────────────────────
    syms["Device:Q_PNP"] = _sym(
        "Device:Q_PNP", "Q_PNP", "Q", "Q",
        "PNP bipolar transistor",
        "pnp bjt transistor",
        pins=[
            _pin("B", "B", [-3.81, 0.0], 180.0, "input"),
            _pin("C", "C", [0.0, -3.81], 270.0, "output"),
            _pin("E", "E", [0.0, 3.81], 90.0, "output"),
        ],
        graphics=[
            _line(-1.27, -2.54, -1.27, 2.54),
            _line(-1.27, 1.27, 0.0, 2.54),
            _line(-1.27, -1.27, 0.0, -2.54),
            _line(-3.81, 0.0, -1.27, 0.0),
            _line(0.0, 2.54, 0.0, 3.81),
            _line(0.0, -2.54, 0.0, -3.81),
            _circle(0.0, 0.0, 2.2),
        ],
        sim={"model": "PNP", "pin_order": ["B", "C", "E"]},
        footprint_filters=["TO-92*", "SOT-23*"],
    )

    # ── N-channel MOSFET ────────────────────────────────────────────────────
    syms["Device:NMOS"] = _sym(
        "Device:NMOS", "NMOS", "Q", "NMOS",
        "N-channel enhancement MOSFET",
        "nmos mosfet transistor",
        pins=[
            _pin("G", "G", [-5.08, 0.0], 180.0, "input"),
            _pin("D", "D", [0.0, 3.81], 90.0, "output"),
            _pin("S", "S", [0.0, -3.81], 270.0, "output"),
        ],
        graphics=[
            # gate line
            _line(-1.27, -2.0, -1.27, 2.0),
            _line(-5.08, 0.0, -1.27, 0.0),
            # channel line
            _line(0.0, -2.54, 0.0, 2.54),
            # drain/source connections
            _line(0.0, 2.54, 0.0, 3.81),
            _line(0.0, -2.54, 0.0, -3.81),
            # body
            _rect(-1.27, -2.8, 0.0, 2.8),
            _circle(0.0, 0.0, 2.8),
        ],
        sim={"model": "NMOS", "pin_order": ["G", "D", "S"]},
        footprint_filters=["SOT-23*", "TO-92*"],
    )

    # ── P-channel MOSFET ────────────────────────────────────────────────────
    syms["Device:PMOS"] = _sym(
        "Device:PMOS", "PMOS", "Q", "PMOS",
        "P-channel enhancement MOSFET",
        "pmos mosfet transistor",
        pins=[
            _pin("G", "G", [-5.08, 0.0], 180.0, "input"),
            _pin("D", "D", [0.0, -3.81], 270.0, "output"),
            _pin("S", "S", [0.0, 3.81], 90.0, "output"),
        ],
        graphics=[
            _line(-1.27, -2.0, -1.27, 2.0),
            _line(-5.08, 0.0, -1.27, 0.0),
            _line(0.0, -2.54, 0.0, 2.54),
            _line(0.0, 2.54, 0.0, 3.81),
            _line(0.0, -2.54, 0.0, -3.81),
            _rect(-1.27, -2.8, 0.0, 2.8),
            _circle(0.0, 0.0, 2.8),
        ],
        sim={"model": "PMOS", "pin_order": ["G", "D", "S"]},
        footprint_filters=["SOT-23*", "TO-92*"],
    )

    # ── Dual op-amp (e.g. LM358) ────────────────────────────────────────────
    # 2 units; unit 3 = power
    opamp_pins = []
    # Unit 1: IN-, IN+, OUT
    opamp_pins += [
        _pin("1", "IN-", [-6.35, 1.27], 180.0, "input", unit=1),
        _pin("2", "IN+", [-6.35, -1.27], 180.0, "input", unit=1),
        _pin("3", "OUT", [6.35, 0.0], 0.0, "output", unit=1),
    ]
    # Unit 2: IN-, IN+, OUT
    opamp_pins += [
        _pin("5", "IN-", [-6.35, 1.27], 180.0, "input", unit=2),
        _pin("6", "IN+", [-6.35, -1.27], 180.0, "input", unit=2),
        _pin("7", "OUT", [6.35, 0.0], 0.0, "output", unit=2),
    ]
    # Power unit (unit 3, unit=0 means shared in KiCad, we use 3)
    opamp_pins += [
        _pin("8", "V+", [0.0, 6.35], 90.0, "power_in", unit=3),
        _pin("4", "V-", [0.0, -6.35], 270.0, "power_in", unit=3),
    ]

    opamp_graphics_u12 = [
        # triangle body
        _polyline([-6.35, 5.08], [-6.35, -5.08], [6.35, 0.0], [-6.35, 5.08]),
        # +/- markers
        _line(-5.08, -1.27, -3.81, -1.27),
        _line(-4.445, -1.905, -4.445, -0.635),
        _line(-5.08, 1.27, -3.81, 1.27),
    ]
    opamp_graphics_pwr = [_rect(-2.54, -2.54, 2.54, 2.54)]

    syms["Amplifier_Operational:OPAMP_dual"] = _sym(
        "Amplifier_Operational:OPAMP_dual", "OPAMP_dual", "U", "OPAMP_dual",
        "Dual operational amplifier",
        "opamp operational amplifier",
        pins=opamp_pins,
        graphics=opamp_graphics_u12,
        units_count=3,
        sim={"model": "OPAMP", "pin_order": ["IN+", "IN-", "V+", "V-", "OUT"]},
        footprint_filters=["SOIC-8*", "DIP-8*"],
    )

    # ── NE555 timer ──────────────────────────────────────────────────────────
    # 8-pin DIP; pins labelled per datasheet
    ne555_w = 10.16
    ne555_h = 15.24
    ne555_pins = [
        _pin("1", "GND", [-ne555_w / 2 - 2.54, -ne555_h / 2 + 1.27], 180.0, "power_in"),
        _pin("2", "TRIG", [-ne555_w / 2 - 2.54, -ne555_h / 2 + 1.27 + 2.54], 180.0, "input"),
        _pin("3", "OUT", [ne555_w / 2 + 2.54, -ne555_h / 2 + 1.27 + 2.54 * 2], 0.0, "output"),
        _pin("4", "RST", [ne555_w / 2 + 2.54, -ne555_h / 2 + 1.27 + 2.54 * 3], 0.0, "input"),
        # CV sat at the SAME (x,y) as RST (both y=1.27) -> the netlist union-find silently
        # shorted RST and CV onto one net. Drop it one grid row to its own connection point.
        _pin("5", "CV", [ne555_w / 2 + 2.54, -ne555_h / 2 + 1.27 + 2.54], 0.0, "input"),
        _pin("6", "THR", [-ne555_w / 2 - 2.54, ne555_h / 2 - 1.27 - 2.54], 180.0, "input"),
        _pin("7", "DIS", [-ne555_w / 2 - 2.54, ne555_h / 2 - 1.27], 180.0, "open_collector"),
        _pin("8", "VCC", [ne555_w / 2 + 2.54, ne555_h / 2 - 1.27], 0.0, "power_in"),
    ]
    ne555_graphics = [_rect(-ne555_w / 2, -ne555_h / 2, ne555_w / 2, ne555_h / 2)]

    syms["Timer:NE555"] = _sym(
        "Timer:NE555", "NE555", "U", "NE555",
        "NE555 timer, single",
        "timer 555 oscillator",
        pins=ne555_pins,
        graphics=ne555_graphics,
        sim={
            "model": "SUBCKT",
            "subckt": "NE555",
            "pin_order": ["GND", "TRIG", "OUT", "RST", "CV", "THR", "DIS", "VCC"],
        },
        footprint_filters=["DIP-8*", "SOIC-8*"],
    )

    # ── 2-pin connector ─────────────────────────────────────────────────────
    syms["Connector:Conn_2"] = _sym(
        "Connector:Conn_2", "Conn_2", "J", "Conn_2",
        "2-pin connector",
        "connector jack plug 2pin",
        pins=[
            _pin("1", "Pin_1", [-3.81, 1.27], 180.0, "passive"),
            _pin("2", "Pin_2", [-3.81, -1.27], 180.0, "passive"),
        ],
        graphics=[
            _rect(-2.54, -2.54, 2.54, 2.54),
            _circle(-1.27, 1.27, 0.5),
            _circle(-1.27, -1.27, 0.5),
        ],
        footprint_filters=["Conn_*", "PinHeader_*"],
    )

    # ── 3-pin connector ─────────────────────────────────────────────────────
    syms["Connector:Conn_3"] = _sym(
        "Connector:Conn_3", "Conn_3", "J", "Conn_3",
        "3-pin connector",
        "connector 3pin",
        pins=[
            _pin("1", "Pin_1", [-3.81, 2.54], 180.0, "passive"),
            _pin("2", "Pin_2", [-3.81, 0.0], 180.0, "passive"),
            _pin("3", "Pin_3", [-3.81, -2.54], 180.0, "passive"),
        ],
        graphics=[
            _rect(-2.54, -3.81, 2.54, 3.81),
            _circle(-1.27, 2.54, 0.5),
            _circle(-1.27, 0.0, 0.5),
            _circle(-1.27, -2.54, 0.5),
        ],
        footprint_filters=["Conn_*", "PinHeader_*"],
    )

    # ── 4-pin connector ─────────────────────────────────────────────────────
    def _conn_n(n: int) -> dict:
        libid = f"Connector:Conn_{n}"
        pins = []
        for i in range(1, n + 1):
            y = (n - 1) * 1.27 - (i - 1) * 2.54
            pins.append(_pin(str(i), f"Pin_{i}", [-3.81, y], 180.0, "passive"))
        h = (n + 1) * 2.54 / 2
        circles = [_circle(-1.27, (n - 1) * 1.27 - (i - 1) * 2.54, 0.5)
                   for i in range(1, n + 1)]
        return _sym(
            libid, f"Conn_{n}", "J", f"Conn_{n}",
            f"{n}-pin connector",
            f"connector {n}pin",
            pins=pins,
            graphics=[_rect(-2.54, -h, 2.54, h)] + circles,
            footprint_filters=["Conn_*", "PinHeader_*"],
        )

    syms["Connector:Conn_4"] = _conn_n(4)

    # ── Header_1x4 ─────────────────────────────────────────────────────────
    syms["Connector:Header_1x4"] = _conn_n(4)
    syms["Connector:Header_1x4"]["libid"] = "Connector:Header_1x4"
    syms["Connector:Header_1x4"]["name"] = "Header_1x4"
    syms["Connector:Header_1x4"]["value"] = "Header_1x4"
    syms["Connector:Header_1x4"]["description"] = "1x4 pin header"

    # ── GND power symbol ────────────────────────────────────────────────────
    syms["power:GND"] = _sym(
        "power:GND", "GND", "#PWR", "GND",
        "Ground power symbol",
        "power gnd ground",
        pins=[_pin("1", "GND", [0.0, 0.0], 90.0, "power_out")],
        graphics=[
            _line(0.0, 0.0, 0.0, -1.27),
            _line(-1.27, -1.27, 1.27, -1.27),
            _line(-0.762, -1.905, 0.762, -1.905),
            _line(-0.254, -2.54, 0.254, -2.54),
        ],
        power=True,
        sim={"model": "GND"},
    )

    # ── VCC power symbol ────────────────────────────────────────────────────
    syms["power:VCC"] = _sym(
        "power:VCC", "VCC", "#PWR", "VCC",
        "VCC power symbol",
        "power vcc supply",
        pins=[_pin("1", "VCC", [0.0, 0.0], 270.0, "power_out")],
        graphics=[
            _line(0.0, 0.0, 0.0, 1.27),
            _line(-1.27, 1.27, 1.27, 1.27),
        ],
        power=True,
        sim={"model": "VCC"},
    )

    # ── +5V power symbol ───────────────────────────────────────────────────
    syms["power:+5V"] = _sym(
        "power:+5V", "+5V", "#PWR", "+5V",
        "+5V power symbol",
        "power 5v supply",
        pins=[_pin("1", "+5V", [0.0, 0.0], 270.0, "power_out")],
        graphics=[
            _line(0.0, 0.0, 0.0, 1.27),
            _line(-1.27, 1.27, 1.27, 1.27),
        ],
        power=True,
        sim={"model": "+5V"},
    )

    # ── +3V3 power symbol ──────────────────────────────────────────────────
    syms["power:+3V3"] = _sym(
        "power:+3V3", "+3V3", "#PWR", "+3V3",
        "+3.3V power symbol",
        "power 3v3 supply",
        pins=[_pin("1", "+3V3", [0.0, 0.0], 270.0, "power_out")],
        graphics=[
            _line(0.0, 0.0, 0.0, 1.27),
            _line(-1.27, 1.27, 1.27, 1.27),
        ],
        power=True,
        sim={"model": "+3V3"},
    )

    # ── -5V power symbol ───────────────────────────────────────────────────
    syms["power:-5V"] = _sym(
        "power:-5V", "-5V", "#PWR", "-5V",
        "-5V power symbol",
        "power -5v negative supply",
        pins=[_pin("1", "-5V", [0.0, 0.0], 90.0, "power_out")],
        graphics=[
            _line(0.0, 0.0, 0.0, -1.27),
            _line(-1.27, -1.27, 1.27, -1.27),
        ],
        power=True,
        sim={"model": "-5V"},
    )

    # ── Voltage source ─────────────────────────────────────────────────────
    syms["Simulation_SPICE:V_source"] = _sym(
        "Simulation_SPICE:V_source", "V_source", "V", "V",
        "Ideal voltage source (SPICE)",
        "voltage source spice simulation",
        pins=[
            _pin("+", "+", [0.0, 2.54], 90.0, "power_out"),
            _pin("-", "-", [0.0, -2.54], 270.0, "power_out"),
        ],
        graphics=[
            _circle(0.0, 0.0, 1.6),
            _line(0.0, 1.6, 0.0, 2.54),
            _line(0.0, -1.6, 0.0, -2.54),
            # + marker
            _line(-0.5, 0.8, 0.5, 0.8),
            _line(0.0, 0.3, 0.0, 1.3),
            # - marker
            _line(-0.5, -0.8, 0.5, -0.8),
        ],
        sim={"model": "V", "pin_order": ["+", "-"]},
    )

    # ── Current source ─────────────────────────────────────────────────────
    syms["Simulation_SPICE:I_source"] = _sym(
        "Simulation_SPICE:I_source", "I_source", "I", "I",
        "Ideal current source (SPICE)",
        "current source spice simulation",
        pins=[
            _pin("+", "+", [0.0, 2.54], 90.0, "power_out"),
            _pin("-", "-", [0.0, -2.54], 270.0, "power_out"),
        ],
        graphics=[
            _circle(0.0, 0.0, 1.6),
            # arrow up
            _line(0.0, -1.2, 0.0, 1.2),
            _line(-0.5, 0.5, 0.0, 1.2),
            _line(0.5, 0.5, 0.0, 1.2),
            _line(0.0, 1.6, 0.0, 2.54),
            _line(0.0, -1.6, 0.0, -2.54),
        ],
        sim={"model": "I", "pin_order": ["+", "-"]},
    )

    # ── Switch ─────────────────────────────────────────────────────────────
    syms["Device:Switch"] = _sym(
        "Device:Switch", "Switch", "SW", "SW",
        "SPST mechanical switch",
        "switch button spst",
        pins=[
            _pin("1", "A", [-3.81, 0.0], 180.0, "passive"),
            _pin("2", "B", [3.81, 0.0], 0.0, "passive"),
        ],
        graphics=[
            # switch body: two circles (contacts)
            _circle(-1.27, 0.0, 0.5),
            _circle(1.27, 0.0, 0.5),
            # open blade
            _line(-0.8, 0.0, 1.27, 1.27),
            _line(-3.81, 0.0, -1.27, 0.0),
            _line(1.27, 0.0, 3.81, 0.0),
        ],
        sim={"model": "S", "pin_order": ["1", "2"]},
    )

    return syms


# ─── engineering-metadata seeds (T-FIN8; schema: contracts/ecad/boardphys.md) ──
# Values are DATASHEET-CLASS SEEDS, each with a source note (research_datasheet_fetch
# style provenance). They are nominal/typical figures for the generic part class the
# starter symbol represents — good enough for power budgets and thermal screening, and
# every consumer sees the source note. Unknown values are intentionally ABSENT so the
# resolver reports honest nulls (never invented zeros).
#   sequencing: [] = known "no sequencing requirement" (single-rail device);
#               absent/None = unknown.

def _rail(name: str, vmin: float | None, vnom: float | None, vmax: float | None,
          i_active: float | None, i_sleep: float | None = None) -> dict:
    return {"name": name, "voltage_v_min": vmin, "voltage_v_nom": vnom,
            "voltage_v_max": vmax, "current_a_active": i_active,
            "current_a_sleep": i_sleep}


_SYMBOL_ENG: dict[str, dict] = {
    # Bipolar 555: Icc 3 mA typ / 6 mA max @ Vcc=5 V; 10/15 mA @ 15 V; Vcc 4.5–16 V.
    "Timer:NE555": {
        "power_w": {"typ": 0.015, "max": 0.225},   # 5 V·3 mA typ; 15 V·15 mA worst-case quiescent
        "limits_c": {"tj_max": 150.0, "ta_max": 70.0},
        "supply": {"rails": [_rail("VCC", 4.5, 5.0, 16.0, 0.003, None)],
                   "voltage_domains": ["VCC"], "sequencing": []},
        "sources": ["research_datasheet_fetch: TI NE555 datasheet (SLFS022) — Icc 3 mA typ /"
                    " 6 mA max @5 V, 10/15 mA @15 V; Vcc 4.5–16 V; Ta 0–70 °C. Seed values"
                    " nominal; verify against the exact device grade."],
    },
    # LM358-class dual op-amp: Icc 0.7 mA typ / 1.2 mA max (both amps); V+ 3–32 V.
    "Amplifier_Operational:OPAMP_dual": {
        "power_w": {"typ": 0.0035, "max": 0.0384},  # 5 V·0.7 mA typ; 32 V·1.2 mA max quiescent
        "limits_c": {"tj_max": 150.0, "ta_max": 85.0},
        "supply": {"rails": [_rail("V+", 3.0, 5.0, 32.0, 0.0007, None)],
                   "voltage_domains": ["V+"], "sequencing": []},
        "sources": ["research_datasheet_fetch: TI LM358 datasheet (SLOS068) — Icc 0.7 mA typ /"
                    " 1.2 mA max, single-supply 3–32 V. Quiescent-only seed; load power is"
                    " circuit-dependent (null until sim provides it)."],
    },
    # Small-signal transistor classes: dissipation is circuit-dependent (left null);
    # junction limit is the class-standard 150 °C.
    "Device:Q_NPN": {
        "limits_c": {"tj_max": 150.0, "ta_max": None},
        "sources": ["research_datasheet_fetch: 2N3904-class small-signal NPN — Tj max 150 °C"
                    " (onsemi 2N3904 datasheet). Pd depends on package + circuit; package"
                    " theta_ja seeds come from the footprint."],
    },
    "Device:Q_PNP": {
        "limits_c": {"tj_max": 150.0, "ta_max": None},
        "sources": ["research_datasheet_fetch: 2N3906-class small-signal PNP — Tj max 150 °C"
                    " (onsemi 2N3906 datasheet)."],
    },
    "Device:NMOS": {
        "limits_c": {"tj_max": 150.0, "ta_max": None},
        "sources": ["research_datasheet_fetch: 2N7002-class small-signal NMOS — Tj max 150 °C"
                    " (Nexperia 2N7002 datasheet)."],
    },
    "Device:PMOS": {
        "limits_c": {"tj_max": 150.0, "ta_max": None},
        "sources": ["research_datasheet_fetch: BSS84-class small-signal PMOS — Tj max 150 °C"
                    " (Nexperia BSS84 datasheet)."],
    },
    "Device:LED": {
        "power_w": {"typ": 0.04, "max": 0.12},      # 2 V·20 mA typ drive; 0805 LED Pd max class
        "limits_c": {"tj_max": 115.0, "ta_max": 85.0},
        "sources": ["research_datasheet_fetch: 0805 chip LED class (e.g. Kingbright APT2012) —"
                    " If 20 mA typ, Pd ~120 mW max, Tj ~115 °C. Actual color/bin changes Vf;"
                    " treat as a budget seed."],
    },
    "Device:D": {
        "limits_c": {"tj_max": 150.0, "ta_max": None},
        "sources": ["research_datasheet_fetch: S1A/SS1x-class rectifier — Tj max 150 °C"
                    " (Vishay S1A datasheet). Conduction loss is circuit-dependent (null)."],
    },
    "Device:Zener": {
        "limits_c": {"tj_max": 150.0, "ta_max": None},
        "sources": ["research_datasheet_fetch: BZT52-class zener — Tj max 150 °C"
                    " (Nexperia BZT52 datasheet)."],
    },
}

def _apply_symbol_eng(syms: dict) -> None:
    for libid, eng in _SYMBOL_ENG.items():
        if libid in syms:
            syms[libid]["eng"] = eng


# ─── FPGA parts (T-FIN11) ─────────────────────────────────────────────────────
# Symbol pins are GENERATED from the aicad.fpga_device/1 JSON seed (engine/ecad/
# fpga_devices/) so the schematic part can never drift from the bank/pin model the
# FPGA checks and constraint exporters use. Only the full-coverage seed (the
# iCE40UP5K-SG48) gets a placeable symbol+footprint; partial seeds (ECP5/XC7A35T
# classes) are check/export data — tag any symbol with fields {"fpga_device": ...}
# to bind it to one of those.

def _make_fpga_symbols() -> dict:
    try:
        from engine.ecad import fpga as _fpga
    except ImportError:  # pragma: no cover - starter must import standalone
        return {}
    syms: dict[str, dict] = {}
    for dev in _fpga.load_devices().values():
        if dev.get("coverage") != "full":
            continue
        devid = dev["device"]
        libid = f"FPGA:{devid}"
        pin_items = sorted(dev.get("pins", {}).items(),
                           key=lambda kv: (int(kv[0]) if kv[0].isdigit() else 10_000, kv[0]))
        n = len(pin_items)
        half = (n + 1) // 2
        rows = max(half, n - half)
        h = rows * 2.54 + 5.08
        w = 30.48
        pins = []
        for i, (num, pd) in enumerate(pin_items):
            nm = pd.get("name") or num
            ptype = {"power": "power_in", "config": "input", "jtag": "input"}.get(
                pd.get("kind"), "bidirectional")
            if pd.get("role") == "GND":
                ptype = "power_in"
            if i < half:   # left column, top to bottom
                y = h / 2 - 2.54 - i * 2.54
                pins.append(_pin(num, nm, [-w / 2 - 2.54, y], 180.0, ptype))
            else:          # right column, top to bottom
                y = h / 2 - 2.54 - (i - half) * 2.54
                pins.append(_pin(num, nm, [w / 2 + 2.54, y], 0.0, ptype))
        syms[libid] = _sym(
            libid, devid, "U", devid,
            f"{dev.get('vendor', '').title()} {dev.get('family')} FPGA, {dev.get('package')} "
            f"package (aicad.fpga_device/1 seed)",
            f"fpga {dev.get('family')} {dev.get('vendor')} programmable logic",
            pins=pins,
            graphics=[_rect(-w / 2, -h / 2, w / 2, h / 2)],
            footprint_filters=["QFN-48*"] if dev.get("package") == "SG48" else [],
            eng={
                "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
                "supply": {"rails": [
                    _rail("VCC", 1.14, 1.2, 1.26, None, None),
                    _rail("VCCIO", 1.71, 3.3, 3.465, None, None),
                ], "voltage_domains": ["VCC", "VCCIO_0", "VCCIO_1", "VCCIO_2"],
                    "sequencing": None},
                "sources": ["research_datasheet_fetch: Lattice iCE40 UltraPlus Family Data"
                            " Sheet FPGA-DS-02008 — VCC 1.2 V core, VCCIO 1.8-3.3 V,"
                            " industrial Tj. Currents are design-dependent (null)."],
            },
        )
        # let the device model be found by libid/value AND record it explicitly
        syms[libid]["fields"] = {"fpga_device": devid}
    return syms


SYMBOLS: dict[str, dict] = _make_symbols()
SYMBOLS.update(_make_fpga_symbols())
_apply_symbol_eng(SYMBOLS)


# ─── FOOTPRINTS ───────────────────────────────────────────────────────────────

def _silk_box(x1: float, y1: float, x2: float, y2: float, layer: str = "F.SilkS") -> list:
    """Four-line rectangle on silkscreen, with a fab-legal stroke width (DFM silk_min_width)."""
    return [
        {"type": "line", "p1": [x1, y1], "p2": [x2, y1], "layer": layer, "width": _SILK_W},
        {"type": "line", "p1": [x2, y1], "p2": [x2, y2], "layer": layer, "width": _SILK_W},
        {"type": "line", "p1": [x2, y2], "p2": [x1, y2], "layer": layer, "width": _SILK_W},
        {"type": "line", "p1": [x1, y2], "p2": [x1, y1], "layer": layer, "width": _SILK_W},
    ]


def _crtyd_box(x1: float, y1: float, x2: float, y2: float) -> list:
    return _silk_box(x1, y1, x2, y2, "F.CrtYd")


# Silk-to-pad clearance (mm) kept when clipping silk lines off the pads. The real fab
# wants silk to clear copper; this also keeps DFM's silk_over_pad (0 mm overlap) quiet.
_SILK_PAD_CLEAR = 0.12


def _pad_keepout(pad: dict) -> tuple[float, float, float] | None:
    """A conservative circular keepout (cx, cy, r) around a pad's copper, expanded by the
    silk-to-pad clearance. Round/oval -> max(w,h)/2; rect-ish -> half-diagonal (over-covers
    the corners slightly, which only shortens silk a hair). Returns None for a sizeless pad."""
    pos = pad.get("pos") or [0.0, 0.0]
    size = pad.get("size") or [0.0, 0.0]
    w = float(size[0]) if len(size) > 0 else 0.0
    h = float(size[1]) if len(size) > 1 else w
    if w <= 0.0 and h <= 0.0:
        return None
    shape = pad.get("shape", "rect")
    if shape in ("circle", "oval"):
        r = max(w, h) / 2.0
    else:
        r = 0.5 * math.hypot(w, h)
    return (float(pos[0]), float(pos[1]), r + _SILK_PAD_CLEAR)


def _clip_silk_line(p1: list, p2: list, keepouts: list) -> list[tuple]:
    """Clip a silk line segment p1->p2 to the parts NOT inside any pad keepout circle.

    Returns a list of (a, b) sub-segments (possibly empty). Parametric sampling along the
    line keeps maximal runs whose midpoint clears every keepout, so it works for any
    orientation (the chip end-lines vanish; the long edges keep their exposed middle)."""
    ax, ay = float(p1[0]), float(p1[1])
    bx, by = float(p2[0]), float(p2[1])
    length = math.hypot(bx - ax, by - ay)
    if length < 1e-9 or not keepouts:
        return [((ax, ay), (bx, by))]

    def _blocked(t: float) -> bool:
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        for (cx, cy, r) in keepouts:
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return True
        return False

    n = max(48, int(length / 0.02))          # ~0.02 mm sampling, fine enough for chip silk
    segs: list[tuple] = []
    run_start: float | None = None
    prev_blocked = True
    for i in range(n + 1):
        t = i / n
        blk = _blocked(t)
        if not blk and prev_blocked:
            run_start = t
        elif blk and not prev_blocked and run_start is not None:
            segs.append((run_start, (i - 1) / n))
            run_start = None
        prev_blocked = blk
    if run_start is not None:
        segs.append((run_start, 1.0))

    out: list[tuple] = []
    for (t0, t1) in segs:
        if (t1 - t0) * length < 0.15:          # drop slivers shorter than a fab-legal tick
            continue
        a = (ax + (bx - ax) * t0, ay + (by - ay) * t0)
        b = (ax + (bx - ax) * t1, ay + (by - ay) * t1)
        out.append((a, b))
    return out


def _clip_footprint_silk(fp: dict) -> None:
    """In place: clip every F/B.SilkS LINE graphic off this footprint's pads.

    Generic + future-proof: any footprint whose silk crosses a pad gets clean silk
    automatically (no per-footprint hand-tuning), killing DFM silk_over_pad false
    positives AND producing manufacturable silk. Non-line silk and non-silk graphics
    are left untouched (text/courtyard/etc.)."""
    keepouts = [k for k in (_pad_keepout(p) for p in fp.get("pads", [])) if k is not None]
    if not keepouts:
        return
    new_graphics: list[dict] = []
    for g in fp.get("graphics", []):
        if g.get("layer") not in ("F.SilkS", "B.SilkS") or g.get("type") != "line":
            new_graphics.append(g)
            continue
        for (a, b) in _clip_silk_line(g.get("p1", [0, 0]), g.get("p2", [0, 0]), keepouts):
            ng = dict(g)
            ng["p1"] = [a[0], a[1]]
            ng["p2"] = [b[0], b[1]]
            new_graphics.append(ng)
    fp["graphics"] = new_graphics


def _make_footprints() -> dict:
    fps: dict[str, dict] = {}

    # ── R_0402 (IPC-7351 nominal) ────────────────────────────────────────
    # pad 0.5×0.5, pitch 1.0 mm
    fps["Resistor_SMD:R_0402"] = _fp(
        "Resistor_SMD:R_0402",
        pads=[
            _smd_pad("p1", "1", -0.5, 0.0, 0.5, 0.5, 0.25),
            _smd_pad("p2", "2", 0.5, 0.0, 0.5, 0.5, 0.25),
        ],
        graphics=_silk_box(-0.6, -0.3, 0.6, 0.3) + _crtyd_box(-0.9, -0.45, 0.9, 0.45),
        description="Resistor 0402",
    )

    # ── R_0603 (IPC-7351 nominal) ────────────────────────────────────────
    # pad 0.9×0.9, pitch 1.6 mm
    fps["Resistor_SMD:R_0603"] = _fp(
        "Resistor_SMD:R_0603",
        pads=[
            _smd_pad("p1", "1", -0.8, 0.0, 0.9, 0.9, 0.25),
            _smd_pad("p2", "2", 0.8, 0.0, 0.9, 0.9, 0.25),
        ],
        graphics=_silk_box(-1.0, -0.5, 1.0, 0.5) + _crtyd_box(-1.3, -0.7, 1.3, 0.7),
        description="Resistor 0603",
    )

    # ── R_0805 ───────────────────────────────────────────────────────────
    fps["Resistor_SMD:R_0805"] = _fp(
        "Resistor_SMD:R_0805",
        pads=[
            _smd_pad("p1", "1", -1.0, 0.0, 1.15, 1.45, 0.25),
            _smd_pad("p2", "2", 1.0, 0.0, 1.15, 1.45, 0.25),
        ],
        graphics=_silk_box(-1.2, -0.7, 1.2, 0.7) + _crtyd_box(-1.7, -1.0, 1.7, 1.0),
        description="Resistor 0805",
    )

    # ── R_1206 ───────────────────────────────────────────────────────────
    fps["Resistor_SMD:R_1206"] = _fp(
        "Resistor_SMD:R_1206",
        pads=[
            _smd_pad("p1", "1", -1.6, 0.0, 1.60, 1.80, 0.25),
            _smd_pad("p2", "2", 1.6, 0.0, 1.60, 1.80, 0.25),
        ],
        graphics=_silk_box(-1.8, -0.9, 1.8, 0.9) + _crtyd_box(-2.3, -1.2, 2.3, 1.2),
        description="Resistor 1206",
    )

    # ── C_0402 ───────────────────────────────────────────────────────────
    fps["Capacitor_SMD:C_0402"] = _fp(
        "Capacitor_SMD:C_0402",
        pads=[
            _smd_pad("p1", "1", -0.5, 0.0, 0.5, 0.5, 0.25),
            _smd_pad("p2", "2", 0.5, 0.0, 0.5, 0.5, 0.25),
        ],
        graphics=_silk_box(-0.6, -0.3, 0.6, 0.3) + _crtyd_box(-0.9, -0.45, 0.9, 0.45),
        description="Capacitor 0402",
    )

    # ── C_0603 ───────────────────────────────────────────────────────────
    fps["Capacitor_SMD:C_0603"] = _fp(
        "Capacitor_SMD:C_0603",
        pads=[
            _smd_pad("p1", "1", -0.8, 0.0, 0.9, 0.9, 0.25),
            _smd_pad("p2", "2", 0.8, 0.0, 0.9, 0.9, 0.25),
        ],
        graphics=_silk_box(-1.0, -0.5, 1.0, 0.5) + _crtyd_box(-1.3, -0.7, 1.3, 0.7),
        description="Capacitor 0603",
    )

    # ── C_0805 ───────────────────────────────────────────────────────────
    fps["Capacitor_SMD:C_0805"] = _fp(
        "Capacitor_SMD:C_0805",
        pads=[
            _smd_pad("p1", "1", -1.0, 0.0, 1.15, 1.45, 0.25),
            _smd_pad("p2", "2", 1.0, 0.0, 1.15, 1.45, 0.25),
        ],
        graphics=_silk_box(-1.2, -0.7, 1.2, 0.7) + _crtyd_box(-1.7, -1.0, 1.7, 1.0),
        description="Capacitor 0805",
    )

    # ── C_1206 ───────────────────────────────────────────────────────────
    fps["Capacitor_SMD:C_1206"] = _fp(
        "Capacitor_SMD:C_1206",
        pads=[
            _smd_pad("p1", "1", -1.6, 0.0, 1.60, 1.80, 0.25),
            _smd_pad("p2", "2", 1.6, 0.0, 1.60, 1.80, 0.25),
        ],
        graphics=_silk_box(-1.8, -0.9, 1.8, 0.9) + _crtyd_box(-2.3, -1.2, 2.3, 1.2),
        description="Capacitor 1206",
    )

    # ── SOT-23 (3 pads) ─────────────────────────────────────────────────
    # Pad 0.55×0.85, pins 1/2 on bottom row at -0.95/+0.95, pin 3 top centre
    fps["Package_TO_SOT_SMD:SOT-23"] = _fp(
        "Package_TO_SOT_SMD:SOT-23",
        pads=[
            _smd_pad("p1", "1", -0.95, -1.3, 0.55, 0.85, 0.25),
            _smd_pad("p2", "2", 0.95, -1.3, 0.55, 0.85, 0.25),
            _smd_pad("p3", "3", 0.0, 1.3, 0.55, 0.85, 0.25),
        ],
        graphics=_silk_box(-1.5, -1.9, 1.5, 1.9) + _crtyd_box(-1.8, -2.2, 1.8, 2.2),
        description="SOT-23 3-lead",
    )

    # ── SOIC-8 ───────────────────────────────────────────────────────────
    # 8 pads; pitch 1.27 mm; row separation 5.4 mm
    # Pads 1-4 on left (x=-2.7), pads 5-8 on right (x=+2.7)
    soic8_pads = []
    for i in range(4):
        y = (1.5 - i) * 1.27   # 1..4: y decreasing top-to-bottom
        soic8_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -2.7, y, 0.6, 1.55, 0.25))
    for i in range(4):
        y = (i - 1.5) * 1.27   # 5..8: y increasing bottom-to-top
        soic8_pads.append(_smd_pad(f"p{i+5}", str(i + 5), 2.7, y, 0.6, 1.55, 0.25))
    fps["Package_SO:SOIC-8"] = _fp(
        "Package_SO:SOIC-8",
        pads=soic8_pads,
        graphics=_silk_box(-2.0, -2.7, 2.0, 2.7) + _crtyd_box(-3.4, -3.1, 3.4, 3.1),
        description="SOIC-8",
    )

    # ── SOIC-14 ──────────────────────────────────────────────────────────
    soic14_pads = []
    for i in range(7):
        y = (3.0 - i) * 1.27
        soic14_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -2.7, y, 0.6, 1.55, 0.25))
    for i in range(7):
        y = (i - 3.0) * 1.27
        soic14_pads.append(_smd_pad(f"p{i+8}", str(i + 8), 2.7, y, 0.6, 1.55, 0.25))
    fps["Package_SO:SOIC-14"] = _fp(
        "Package_SO:SOIC-14",
        pads=soic14_pads,
        graphics=_silk_box(-2.0, -4.5, 2.0, 4.5) + _crtyd_box(-3.4, -4.9, 3.4, 4.9),
        description="SOIC-14",
    )

    # ── TO-92 (3-lead THT) ───────────────────────────────────────────────
    fps["Package_TO_SOT_THT:TO-92"] = _fp(
        "Package_TO_SOT_THT:TO-92",
        pads=[
            _tht_pad("p1", "1", -2.54, 0.0, 0.72, 1.5, 1.5),
            _tht_pad("p2", "2", 0.0, 0.0, 0.72, 1.5, 1.5),
            _tht_pad("p3", "3", 2.54, 0.0, 0.72, 1.5, 1.5),
        ],
        graphics=_silk_box(-3.2, -1.5, 3.2, 1.5) + _crtyd_box(-3.7, -2.0, 3.7, 2.0),
        attr="tht",
        description="TO-92 3-lead",
    )

    # ── DIP-8 (THT) ──────────────────────────────────────────────────────
    # 2 rows × 4 pins, pitch 2.54 mm, row-sep 7.62 mm
    dip8_pads = []
    for i in range(4):
        y = (1.5 - i) * 2.54
        dip8_pads.append(_tht_pad(f"p{i+1}", str(i + 1), -3.81, y, 0.8, 1.6, 1.6))
    for i in range(4):
        y = (i - 1.5) * 2.54
        dip8_pads.append(_tht_pad(f"p{i+5}", str(i + 5), 3.81, y, 0.8, 1.6, 1.6))
    fps["Package_DIP:DIP-8"] = _fp(
        "Package_DIP:DIP-8",
        pads=dip8_pads,
        graphics=_silk_box(-4.8, -5.1, 4.8, 5.1) + _crtyd_box(-5.4, -5.7, 5.4, 5.7),
        attr="tht",
        description="DIP-8 through-hole",
    )

    # ── DIP-14 (THT) ─────────────────────────────────────────────────────
    dip14_pads = []
    for i in range(7):
        y = (3.0 - i) * 2.54
        dip14_pads.append(_tht_pad(f"p{i+1}", str(i + 1), -3.81, y, 0.8, 1.6, 1.6))
    for i in range(7):
        y = (i - 3.0) * 2.54
        dip14_pads.append(_tht_pad(f"p{i+8}", str(i + 8), 3.81, y, 0.8, 1.6, 1.6))
    fps["Package_DIP:DIP-14"] = _fp(
        "Package_DIP:DIP-14",
        pads=dip14_pads,
        graphics=_silk_box(-4.8, -8.9, 4.8, 8.9) + _crtyd_box(-5.4, -9.5, 5.4, 9.5),
        attr="tht",
        description="DIP-14 through-hole",
    )

    # ── Header_1x4_P2.54 ─────────────────────────────────────────────────
    h1x4_pads = []
    for i in range(4):
        y = (1.5 - i) * 2.54
        h1x4_pads.append(_tht_pad(f"p{i+1}", str(i + 1), 0.0, y, 1.0, 1.7, 1.7))
    fps["Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"] = _fp(
        "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        pads=h1x4_pads,
        graphics=_silk_box(-1.0, -5.1, 1.0, 5.1) + _crtyd_box(-1.6, -5.7, 1.6, 5.7),
        attr="tht",
        description="1x4 pin header 2.54mm pitch",
    )
    # alias shorter name
    fps["Connector_PinHeader_2.54mm:Header_1x4"] = dict(
        fps["Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"]
    )
    fps["Connector_PinHeader_2.54mm:Header_1x4"]["fpid"] = (
        "Connector_PinHeader_2.54mm:Header_1x4"
    )

    # ── LED_0805 SMD ─────────────────────────────────────────────────────
    fps["LED_SMD:LED_0805"] = _fp(
        "LED_SMD:LED_0805",
        pads=[
            _smd_pad("p1", "K", -1.0, 0.0, 1.15, 1.45, 0.25),
            _smd_pad("p2", "A", 1.0, 0.0, 1.15, 1.45, 0.25),
        ],
        graphics=_silk_box(-1.2, -0.7, 1.2, 0.7) + _crtyd_box(-1.7, -1.0, 1.7, 1.0),
        description="LED 0805",
    )

    # ── D_SMA SMD ────────────────────────────────────────────────────────
    # SMA pad ~1.7×2.4, pitch 5.2 mm
    fps["Diode_SMD:D_SMA"] = _fp(
        "Diode_SMD:D_SMA",
        pads=[
            _smd_pad("p1", "K", -2.6, 0.0, 1.7, 2.4, 0.1),
            _smd_pad("p2", "A", 2.6, 0.0, 1.7, 2.4, 0.1),
        ],
        graphics=_silk_box(-3.0, -1.5, 3.0, 1.5) + _crtyd_box(-3.6, -2.0, 3.6, 2.0),
        description="Diode SMA",
    )

    # ── D_SMB SMD ────────────────────────────────────────────────────────
    # SMB pad ~2.2×2.8, pitch 6.6 mm
    fps["Diode_SMD:D_SMB"] = _fp(
        "Diode_SMD:D_SMB",
        pads=[
            _smd_pad("p1", "K", -3.3, 0.0, 2.2, 2.8, 0.1),
            _smd_pad("p2", "A", 3.3, 0.0, 2.2, 2.8, 0.1),
        ],
        graphics=_silk_box(-4.0, -1.8, 4.0, 1.8) + _crtyd_box(-4.6, -2.3, 4.6, 2.3),
        description="Diode SMB",
    )

    # ── MountingHole_3.2mm_M3 (clean-room; IPC-7251 annular ring 0.5 mm) ─────
    # M3 screw clears a 3.2 mm hole; NPTH (no copper — mechanical only).
    # Courtyard 4.0 mm radius; silkscreen circle 3.4 mm radius.
    fps["MountingHole:MountingHole_3.2mm_M3"] = _fp(
        "MountingHole:MountingHole_3.2mm_M3",
        pads=[
            _tht_pad("p1", "1", 0.0, 0.0, 3.2, 3.2, 3.2),
        ],
        graphics=[
            {"type": "circle", "layer": "F.SilkS", "center": [0.0, 0.0], "radius": 3.4,
             "width": 0.12},
            {"type": "circle", "layer": "F.CrtYd", "center": [0.0, 0.0], "radius": 4.0,
             "width": 0.05},
        ],
        description="Mounting hole 3.2 mm for M3 screw (NPTH, no copper)",
    )

    # ── QFN-48-1EP 7x7 0.5mm pitch (iCE40UP5K-SG48 et al; IPC-7351 nominal) ──
    # 12 pads/side, pad 0.28x0.85 centred at +/-3.45; exposed pad 5.6x5.6 = pin 49 (GND).
    qfn48_pads = []
    for i in range(12):   # pins 1-12: left side, top to bottom
        y = (5.5 - i) * 0.5
        qfn48_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -3.45, y, 0.85, 0.28, 0.25))
    for i in range(12):   # pins 13-24: bottom, left to right
        x = (i - 5.5) * 0.5
        qfn48_pads.append(_smd_pad(f"p{i+13}", str(i + 13), x, -3.45, 0.28, 0.85, 0.25))
    for i in range(12):   # pins 25-36: right side, bottom to top
        y = (i - 5.5) * 0.5
        qfn48_pads.append(_smd_pad(f"p{i+25}", str(i + 25), 3.45, y, 0.85, 0.28, 0.25))
    for i in range(12):   # pins 37-48: top, right to left
        x = (5.5 - i) * 0.5
        qfn48_pads.append(_smd_pad(f"p{i+37}", str(i + 37), x, 3.45, 0.28, 0.85, 0.25))
    qfn48_pads.append(_smd_pad("p49", "49", 0.0, 0.0, 5.6, 5.6, 0.05))  # EP = GND paddle
    fps["Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm"] = _fp(
        "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm",
        pads=qfn48_pads,
        graphics=_silk_box(-3.6, -3.6, 3.6, 3.6) + _crtyd_box(-4.15, -4.15, 4.15, 4.15)
        + [_circle(-4.0, 3.2, 0.15)],   # pin-1 dot
        description="QFN-48 7x7 mm 0.5 mm pitch with exposed pad (pin 49)",
    )

    # Final pass: clip every footprint's silk off its pads so no generated footprint
    # ships silk over copper (DFM silk_over_pad) — generic, covers all sizes + future parts.
    for fp in fps.values():
        _clip_footprint_silk(fp)

    return fps


# ─── footprint engineering-metadata seeds (T-FIN8) ────────────────────────────
# Package BODY dims (nominal plastic body, mm — not lead span) + JEDEC-style
# single-part theta_ja seeds. ecad_to_3d uses the body dims for component solids;
# the boardphys sidecar carries theta for the thermal handoff. All approximate,
# all carrying a source note. Chip R footprints additionally carry the rated max
# dissipation of the standard thick-film part in that size.

_JEDEC_SRC = ("research_standard_section: JEDEC/IPC-7351 nominal package outlines +"
              " JEDEC JESD51 single-part theta_ja on a standard 2-layer test board."
              " Body dims nominal; theta_ja is layout-dependent — seed only.")
_R_SRC = ("research_datasheet_fetch: standard thick-film chip resistor ratings by EIA size"
          " (Vishay CRCW / Yageo RC series): 0402=1/16 W, 0603=1/10 W, 0805=1/8 W,"
          " 1206=1/4 W rated max continuous at 70 °C. Rated MAX, not operating dissipation.")


def _box(l, w, h, theta_ja=None, source=_JEDEC_SRC):
    return _pkg_eng("box", l_mm=l, w_mm=w, h_mm=h, theta_ja=theta_ja, source=source)


_FOOTPRINT_ENG: dict[str, dict] = {
    # Chip passives (EIA imperial): body L×W×H.
    "Resistor_SMD:R_0402":  _box(1.0, 0.5, 0.35),
    "Resistor_SMD:R_0603":  _box(1.6, 0.8, 0.45),
    "Resistor_SMD:R_0805":  _box(2.0, 1.25, 0.55),
    "Resistor_SMD:R_1206":  _box(3.2, 1.6, 0.60),
    "Capacitor_SMD:C_0402": _box(1.0, 0.5, 0.5),
    "Capacitor_SMD:C_0603": _box(1.6, 0.8, 0.8),
    "Capacitor_SMD:C_0805": _box(2.0, 1.25, 1.25),
    "Capacitor_SMD:C_1206": _box(3.2, 1.6, 1.6),
    "LED_SMD:LED_0805":     _box(2.0, 1.25, 0.8),
    # SMD ICs / discretes.
    "Package_TO_SOT_SMD:SOT-23": _box(2.9, 1.3, 1.1, theta_ja=350.0),
    "Package_SO:SOIC-8":         _box(4.9, 3.9, 1.75, theta_ja=120.0),
    "Package_SO:SOIC-14":        _box(8.65, 3.9, 1.75, theta_ja=95.0),
    "Diode_SMD:D_SMA":           _box(4.3, 2.6, 2.3, theta_ja=90.0),
    "Diode_SMD:D_SMB":           _box(5.4, 3.6, 2.3, theta_ja=75.0),
    # THT.
    "Package_DIP:DIP-8":  _box(9.27, 6.35, 3.3, theta_ja=100.0),
    "Package_DIP:DIP-14": _box(19.05, 6.35, 3.3, theta_ja=85.0),
    "Package_TO_SOT_THT:TO-92":
        _pkg_eng("cylinder", d_mm=4.7, h_mm=4.6, theta_ja=200.0, source=_JEDEC_SRC),
    "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical":
        _box(10.16, 2.54, 8.5, source=_JEDEC_SRC + " Header height incl. pins (nominal)."),
    "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm": _box(7.0, 7.0, 0.85, theta_ja=28.0),
}
_FOOTPRINT_ENG["Connector_PinHeader_2.54mm:Header_1x4"] = \
    _FOOTPRINT_ENG["Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"]

# Rated max dissipation for the standard thick-film resistor of each size (power_w.typ
# stays null — actual dissipation is circuit-defined and must come from sim/overrides).
for _fpid, _pmax in (("Resistor_SMD:R_0402", 0.0625), ("Resistor_SMD:R_0603", 0.1),
                     ("Resistor_SMD:R_0805", 0.125), ("Resistor_SMD:R_1206", 0.25)):
    _FOOTPRINT_ENG[_fpid]["power_w"] = {"max": _pmax}
    _FOOTPRINT_ENG[_fpid].setdefault("sources", []).append(_R_SRC)


def _apply_footprint_eng(fps: dict) -> None:
    for fpid, eng in _FOOTPRINT_ENG.items():
        if fpid in fps:
            fps[fpid]["eng"] = eng


FOOTPRINTS: dict[str, dict] = _make_footprints()
_apply_footprint_eng(FOOTPRINTS)


# ─── public helpers ──────────────────────────────────────────────────────────

def get_symbol(libid: str) -> dict | None:
    """Return a starter SymbolDef by libid, or None."""
    return SYMBOLS.get(libid)


def get_footprint(fpid: str) -> dict | None:
    """Return a starter footprint def by fpid, or None."""
    return FOOTPRINTS.get(fpid)
