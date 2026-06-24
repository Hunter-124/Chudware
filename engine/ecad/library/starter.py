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
        description: str = "") -> dict:
    return {
        "fpid": fpid,
        "pads": pads,
        "graphics": graphics,
        "model_3d": None,
        "model_3d_transform": None,
        "attr": attr,
        "description": description,
    }


def _sym(libid: str, name: str, ref_prefix: str, value: str,
         description: str, keywords: str,
         pins: list, graphics: list,
         units_count: int = 1, power: bool = False,
         sim: dict | None = None,
         footprint_filters: list | None = None) -> dict:
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
        _pin("5", "CV", [ne555_w / 2 + 2.54, ne555_h / 2 - 1.27 - 2.54 * 2], 0.0, "input"),
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


SYMBOLS: dict[str, dict] = _make_symbols()


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

    return fps


FOOTPRINTS: dict[str, dict] = _make_footprints()


# ─── public helpers ──────────────────────────────────────────────────────────

def get_symbol(libid: str) -> dict | None:
    """Return a starter SymbolDef by libid, or None."""
    return SYMBOLS.get(libid)


def get_footprint(fpid: str) -> dict | None:
    """Return a starter footprint def by fpid, or None."""
    return FOOTPRINTS.get(fpid)
