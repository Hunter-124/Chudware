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
        # An LED is not a silicon rectifier: without junction parameters it fell through to the
        # solver's default diode (IS=1e-14, N=1) and conducted at ~0.74 V, making every simulated
        # LED circuit physically wrong. Generic RED-LED Shockley model — 2.00 V forward drop
        # across the 1-20 mA indicator range. Ported in core/sim/netlist.cpp symbol_sim_table().
        sim={"model": "D", "pin_order": ["A", "K"],
             "params": {"IS": 2.3e-19, "N": 2.0, "RS": 2.0}},
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
    # Pin numbers 1/2/3 match SOT-23 pad numbers (MMBT3904-class: 1=B, 2=E, 3=C)
    # so sch->pcb pad binding attaches correctly. sim.pin_order still uses names
    # (C/B/E) which resolve via the pin name map.
    syms["Device:Q_NPN"] = _sym(
        "Device:Q_NPN", "Q_NPN", "Q", "Q",
        "NPN bipolar transistor (SOT-23 pin numbers)",
        "npn bjt transistor",
        pins=[
            _pin("1", "B", [-3.81, 0.0], 180.0, "input"),
            _pin("3", "C", [0.0, 3.81], 90.0, "output"),
            _pin("2", "E", [0.0, -3.81], 270.0, "output"),
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
        sim={"model": "NPN", "pin_order": ["C", "B", "E"]},  # solver order (devices.py BJT: nodes=[C,B,E])
        footprint_filters=["SOT-23*", "TO-92*"],
    )

    # ── PNP BJT ──────────────────────────────────────────────────────────────
    # SOT-23 pin numbers: 1=B, 2=E, 3=C (MMBT3906-class).
    syms["Device:Q_PNP"] = _sym(
        "Device:Q_PNP", "Q_PNP", "Q", "Q",
        "PNP bipolar transistor (SOT-23 pin numbers)",
        "pnp bjt transistor",
        pins=[
            _pin("1", "B", [-3.81, 0.0], 180.0, "input"),
            _pin("2", "E", [0.0, 3.81], 90.0, "output"),
            _pin("3", "C", [0.0, -3.81], 270.0, "output"),
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
        sim={"model": "PNP", "pin_order": ["C", "B", "E"]},  # solver order (devices.py BJT: nodes=[C,B,E])
        footprint_filters=["SOT-23*", "TO-92*"],
    )

    # ── N-channel MOSFET ────────────────────────────────────────────────────
    # SOT-23 pin numbers match 2N7002-class: 1=G, 2=S, 3=D.
    syms["Device:NMOS"] = _sym(
        "Device:NMOS", "NMOS", "Q", "NMOS",
        "N-channel enhancement MOSFET (SOT-23 pin numbers)",
        "nmos mosfet transistor",
        pins=[
            _pin("1", "G", [-5.08, 0.0], 180.0, "input"),
            _pin("3", "D", [0.0, 3.81], 90.0, "output"),
            _pin("2", "S", [0.0, -3.81], 270.0, "output"),
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
        sim={"model": "NMOS", "pin_order": ["D", "G", "S"]},  # solver order (devices.py Mosfet: nodes=[D,G,S])
        footprint_filters=["SOT-23*", "TO-92*"],
    )

    # ── P-channel MOSFET ────────────────────────────────────────────────────
    # SOT-23 pin numbers match BSS84-class: 1=G, 2=S, 3=D.
    syms["Device:PMOS"] = _sym(
        "Device:PMOS", "PMOS", "Q", "PMOS",
        "P-channel enhancement MOSFET (SOT-23 pin numbers)",
        "pmos mosfet transistor",
        pins=[
            _pin("1", "G", [-5.08, 0.0], 180.0, "input"),
            _pin("2", "S", [0.0, 3.81], 90.0, "output"),
            _pin("3", "D", [0.0, -3.81], 270.0, "output"),
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
        sim={"model": "PMOS", "pin_order": ["D", "G", "S"]},  # solver order (devices.py Mosfet: nodes=[D,G,S])
        footprint_filters=["SOT-23*", "TO-92*"],
    )

    # ── Dual op-amp abstract multi-unit ──────────────────────────────────────
    # Unit-based abstract dual op-amp for multi-unit schematic placement / SPICE
    # OPAMP model. Pin numbers are NOT a physical package map — use
    # Amplifier_Operational:LM358 for a datasheet-correct DIP-8/SOIC-8 pinout.
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
    # Power unit (unit 3)
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

    syms["Amplifier_Operational:OPAMP_dual"] = _sym(
        "Amplifier_Operational:OPAMP_dual", "OPAMP_dual", "U", "OPAMP_dual",
        "Dual operational amplifier (abstract multi-unit; not a package pinout)",
        "opamp operational amplifier dual abstract",
        pins=opamp_pins,
        graphics=opamp_graphics_u12,
        units_count=3,
        sim={"model": "OPAMP", "pin_order": ["IN+", "IN-", "OUT"]},
        footprint_filters=["SOIC-8*", "DIP-8*"],
    )

    # ── LM358 dual op-amp (datasheet DIP-8 / SOIC-8 pinout) ─────────────────
    # TI SLOS068 / public datasheet pin map (clean-room; not derived from KiCad):
    #   1 OUT1, 2 IN1-, 3 IN1+, 4 V-, 5 IN2+, 6 IN2-, 7 OUT2, 8 V+
    # Single-unit symbol so every pin is on unit 1 and matches SOIC-8/DIP-8 pads 1-8.
    lm358_w, lm358_h = 10.16, 15.24
    lm358_pins = [
        _pin("1", "OUT1", [-lm358_w / 2 - 2.54, lm358_h / 2 - 1.27], 180.0, "output"),
        _pin("2", "IN1-", [-lm358_w / 2 - 2.54, lm358_h / 2 - 1.27 - 2.54], 180.0, "input"),
        _pin("3", "IN1+", [-lm358_w / 2 - 2.54, lm358_h / 2 - 1.27 - 2.54 * 2], 180.0, "input"),
        _pin("4", "V-", [-lm358_w / 2 - 2.54, -lm358_h / 2 + 1.27], 180.0, "power_in"),
        _pin("5", "IN2+", [lm358_w / 2 + 2.54, -lm358_h / 2 + 1.27], 0.0, "input"),
        _pin("6", "IN2-", [lm358_w / 2 + 2.54, -lm358_h / 2 + 1.27 + 2.54], 0.0, "input"),
        _pin("7", "OUT2", [lm358_w / 2 + 2.54, lm358_h / 2 - 1.27 - 2.54], 0.0, "output"),
        _pin("8", "V+", [lm358_w / 2 + 2.54, lm358_h / 2 - 1.27], 0.0, "power_in"),
    ]
    syms["Amplifier_Operational:LM358"] = _sym(
        "Amplifier_Operational:LM358", "LM358", "U", "LM358",
        "LM358 dual operational amplifier (DIP-8/SOIC-8 datasheet pinout)",
        "opamp operational amplifier lm358 dual",
        pins=lm358_pins,
        graphics=[_rect(-lm358_w / 2, -lm358_h / 2, lm358_w / 2, lm358_h / 2)],
        sim={"model": "OPAMP", "pin_order": ["IN1+", "IN1-", "V+", "V-", "OUT1"]},
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

    # ── Ferrite bead ────────────────────────────────────────────────────────
    syms["Device:Ferrite_Bead"] = _sym(
        "Device:Ferrite_Bead", "Ferrite_Bead", "FB", "FB",
        "Ferrite bead (series impedance)",
        "ferrite bead choke emi filter inductor",
        pins=[
            _pin("1", "~", [0.0, 2.54], 90.0, "passive"),
            _pin("2", "~", [0.0, -2.54], 270.0, "passive"),
        ],
        graphics=[
            _rect(-1.0, -1.5, 1.0, 1.5),
            _line(0.0, 2.54, 0.0, 1.5),
            _line(0.0, -1.5, 0.0, -2.54),
            _line(-0.7, 0.8, 0.7, 0.8),
            _line(-0.7, 0.0, 0.7, 0.0),
            _line(-0.7, -0.8, 0.7, -0.8),
        ],
        sim={"model": "L", "pin_order": ["1", "2"]},
        footprint_filters=["L_*", "FB_*"],
    )

    # ── 2xN pin headers (2.54 mm) ───────────────────────────────────────────
    def _header_2xn(cols: int) -> dict:
        n = cols * 2
        pins = []
        for i in range(1, n + 1):
            # odd pins left column top→bottom (1,3,5…), even right (2,4,6…)
            col = 0 if (i % 2 == 1) else 1
            row = (i - 1) // 2
            x = -3.81 if col == 0 else 3.81
            y = (cols - 1) * 1.27 - row * 2.54
            pins.append(_pin(str(i), f"Pin_{i}", [x, y], 180.0 if col == 0 else 0.0, "passive"))
        h = cols * 1.27 + 1.27
        return _sym(
            f"Connector:Header_2x{cols:02d}", f"Header_2x{cols:02d}", "J", f"Header_2x{cols:02d}",
            f"2x{cols} pin header 2.54 mm",
            f"connector header 2x{cols} pinheader 2.54",
            pins=pins,
            graphics=[_rect(-2.54, -h, 2.54, h)],
            footprint_filters=["PinHeader_2x*", "Header_2x*"],
        )

    syms["Connector:Header_2x03"] = _header_2xn(3)
    syms["Connector:Header_2x05"] = _header_2xn(5)

    # ── USB-C receptacle (simplified 16-pin mid-mount class) ────────────────
    # Clean-room simplified USB-C pin subset for host/device boards. Numbers are
    # sequential 1..16 matching the footprint pads (not a full CC/SBU map).
    usb_pins = []
    usb_names = [
        "GND", "TX1+", "TX1-", "VBUS", "CC1", "D+", "D-", "SBU1",
        "VBUS", "RX2-", "RX2+", "GND", "SBU2", "VBUS", "CC2", "SHIELD",
    ]
    for i, nm in enumerate(usb_names, start=1):
        y = (8 - i) * 1.27
        ptype = "power_in" if nm in ("VBUS", "GND", "SHIELD") else "bidirectional"
        if nm in ("VBUS",):
            ptype = "power_in"
        if nm == "GND" or nm == "SHIELD":
            ptype = "power_in"
        usb_pins.append(_pin(str(i), nm, [-7.62, y], 180.0, ptype))
    syms["Connector:USB_C"] = _sym(
        "Connector:USB_C", "USB_C", "J", "USB_C",
        "USB Type-C receptacle (simplified 16-pin)",
        "usb usb-c type-c connector receptacle",
        pins=usb_pins,
        graphics=[_rect(-5.08, -12.0, 5.08, 12.0), _circle(-3.0, 10.0, 0.4)],
        footprint_filters=["USB_C*", "Connector_USB*"],
    )

    # ── USB-B receptacle (4-pin classic) ────────────────────────────────────
    # Public pinout: 1=VBUS, 2=D-, 3=D+, 4=GND
    syms["Connector:USB_B"] = _sym(
        "Connector:USB_B", "USB_B", "J", "USB_B",
        "USB Type-B receptacle (4-pin)",
        "usb usb-b type-b connector receptacle",
        pins=[
            _pin("1", "VBUS", [-5.08, 3.81], 180.0, "power_in"),
            _pin("2", "D-", [-5.08, 1.27], 180.0, "bidirectional"),
            _pin("3", "D+", [-5.08, -1.27], 180.0, "bidirectional"),
            _pin("4", "GND", [-5.08, -3.81], 180.0, "power_in"),
        ],
        graphics=[_rect(-2.54, -5.08, 2.54, 5.08)],
        footprint_filters=["USB_B*", "Connector_USB*"],
    )

    # ── Linear regulator AMS1117-3.3 (SOT-223) ──────────────────────────────
    # Public datasheet pinout SOT-223 (tab = VOUT): 1=GND, 2=VOUT, 3=VIN, 4=VOUT(tab)
    syms["Regulator_Linear:AMS1117-3.3"] = _sym(
        "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", "U", "AMS1117-3.3",
        "AMS1117 3.3 V LDO (SOT-223 pinout)",
        "regulator ldo linear ams1117 3.3v 3v3",
        pins=[
            _pin("1", "GND", [-7.62, -2.54], 180.0, "power_in"),
            _pin("2", "VOUT", [7.62, 0.0], 0.0, "power_out"),
            _pin("3", "VIN", [-7.62, 2.54], 180.0, "power_in"),
            _pin("4", "VOUT", [0.0, 5.08], 90.0, "power_out"),
        ],
        graphics=[_rect(-5.08, -3.81, 5.08, 3.81)],
        footprint_filters=["SOT-223*"],
        eng={
            "power_w": {"typ": 0.5, "max": 1.0},
            "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VIN", 4.75, 5.0, 12.0, 0.01, None),
                _rail("VOUT", 3.2, 3.3, 3.4, None, None),
            ], "voltage_domains": ["VIN", "VOUT"], "sequencing": []},
            "sources": ["research_datasheet_fetch: AMS1117-3.3 class LDO — Vout 3.3 V,"
                        " dropout ~1.1 V, Tj max 125 °C. Seed currents; load-dependent."],
        },
    )

    # ── 7805 linear regulator (TO-220) ──────────────────────────────────────
    # Public pinout TO-220 (facing front, pins down): 1=IN, 2=GND, 3=OUT
    syms["Regulator_Linear:7805"] = _sym(
        "Regulator_Linear:7805", "7805", "U", "7805",
        "7805 5 V linear regulator (TO-220 pinout)",
        "regulator linear 7805 5v lm7805",
        pins=[
            _pin("1", "VIN", [-7.62, 2.54], 180.0, "power_in"),
            _pin("2", "GND", [-7.62, 0.0], 180.0, "power_in"),
            _pin("3", "VOUT", [-7.62, -2.54], 180.0, "power_out"),
        ],
        graphics=[_rect(-5.08, -5.08, 5.08, 5.08)],
        footprint_filters=["TO-220*"],
        eng={
            "power_w": {"typ": 1.0, "max": 15.0},
            "limits_c": {"tj_max": 150.0, "ta_max": 70.0},
            "supply": {"rails": [
                _rail("VIN", 7.0, 9.0, 25.0, 0.005, None),
                _rail("VOUT", 4.8, 5.0, 5.2, None, None),
            ], "voltage_domains": ["VIN", "VOUT"], "sequencing": []},
            "sources": ["research_datasheet_fetch: LM7805/MC7805 class — Vout 5 V,"
                        " Vin 7–25 V, Tj max 150 °C. Dissipation is (Vin-Vout)*Iload."],
        },
    )

    # ── Generic MCU QFP-48 class (simplified STM32F0-like) ──────────────────
    # Clean-room simplified 48-pin MCU symbol for host-board workflows. Pins are
    # numbered 1..48 matching LQFP-48 pads. Named power/boot/reset/clock pins
    # for hostboard_rules identification; remaining pins are GPIO.
    mcu_named = {
        1: ("VBAT", "power_in"), 7: ("NRST", "input"), 8: ("VSSA", "power_in"),
        9: ("VDDA", "power_in"), 10: ("OSC_IN", "input"), 11: ("OSC_OUT", "output"),
        12: ("VSS", "power_in"), 13: ("VDD", "power_in"), 23: ("VSS", "power_in"),
        24: ("VDD", "power_in"), 28: ("BOOT0", "input"), 35: ("VSS", "power_in"),
        36: ("VDD", "power_in"), 47: ("VSS", "power_in"), 48: ("VDD", "power_in"),
    }
    mcu_pins = []
    # 12 pins per side, LQFP-48 style numbering counterclockwise from top-left.
    # Side layout for symbol (not package geometry): L 1-12, B 13-24, R 25-36, T 37-48.
    for i in range(1, 49):
        if i <= 12:  # left
            y = (6.5 - i) * 2.54
            pos, ang = [-12.7, y], 180.0
        elif i <= 24:  # bottom
            x = (i - 18.5) * 2.54
            pos, ang = [x, -12.7], 270.0
        elif i <= 36:  # right
            y = (i - 30.5) * 2.54
            pos, ang = [12.7, y], 0.0
        else:  # top
            x = (42.5 - i) * 2.54
            pos, ang = [x, 12.7], 90.0
        if i in mcu_named:
            nm, ptype = mcu_named[i]
        else:
            nm, ptype = f"P{i}", "bidirectional"
        mcu_pins.append(_pin(str(i), nm, pos, ang, ptype))
    syms["MCU:Generic_QFP48"] = _sym(
        "MCU:Generic_QFP48", "Generic_QFP48", "U", "MCU",
        "Generic 48-pin MCU (QFP-48 / STM32F0-class simplified)",
        "mcu microcontroller stm32 arm cortex qfp48 host",
        pins=mcu_pins,
        graphics=[_rect(-10.16, -10.16, 10.16, 10.16)],
        footprint_filters=["LQFP-48*", "QFP-48*"],
        eng={
            "limits_c": {"tj_max": 105.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VDD", 2.0, 3.3, 3.6, 0.02, 0.00001),
                _rail("VDDA", 2.0, 3.3, 3.6, 0.001, None),
            ], "voltage_domains": ["VDD", "VDDA"], "sequencing": []},
            "sources": ["research_datasheet_fetch: STM32F0-class MCU envelope — VDD 2.0–3.6 V,"
                        " Tj industrial. Pin names simplified for host-board checks; currents seed."],
        },
    )


    # ═══════════════════════════════════════════════════════════════════════
    # Flight-computer + battery-charger parts pack (clean-room, public pinouts)
    # ═══════════════════════════════════════════════════════════════════════

    # ── MPU-6050 class 6-axis IMU (QFN-24) ─────────────────────────────────
    # InvenSense MPU-6050 public QFN-24 pin map (clean-room from datasheet):
    # 1 CLKIN, 6 VDD, 7 GND, 8 FSYNC, 9 INT, 11 GND, 12 REGOUT, 13 CPOUT,
    # 18 VLOGIC, 20 SCL/SCLK, 21 SDA/SDI, 22 nCS, 23 AD0/SDO; EP=25 GND.
    # Reserved/unused package pins kept so pad set is a superset of symbol pins.
    imu_w, imu_h = 12.7, 17.78
    imu_named = {
        1: ("CLKIN", "input"), 6: ("VDD", "power_in"), 7: ("GND", "power_in"),
        8: ("FSYNC", "input"), 9: ("INT", "output"), 11: ("GND", "power_in"),
        12: ("REGOUT", "passive"), 13: ("CPOUT", "passive"),
        18: ("VLOGIC", "power_in"), 20: ("SCL", "bidirectional"),
        21: ("SDA", "bidirectional"), 22: ("nCS", "input"),
        23: ("AD0", "bidirectional"), 25: ("EP", "power_in"),
    }
    imu_pins = []
    for i in range(1, 25):
        if i <= 6:  # left
            y = (3.5 - i) * 2.54
            pos, ang = [-imu_w / 2 - 2.54, y], 180.0
        elif i <= 12:  # bottom
            x = (i - 9.5) * 2.54
            pos, ang = [x, -imu_h / 2 - 2.54], 270.0
        elif i <= 18:  # right
            y = (i - 15.5) * 2.54
            pos, ang = [imu_w / 2 + 2.54, y], 0.0
        else:  # top
            x = (21.5 - i) * 2.54
            pos, ang = [x, imu_h / 2 + 2.54], 90.0
        if i in imu_named:
            nm, ptype = imu_named[i]
        else:
            nm, ptype = f"NC{i}", "no_connect"
        imu_pins.append(_pin(str(i), nm, pos, ang, ptype))
    imu_pins.append(_pin("25", "EP", [0.0, 0.0], 0.0, "power_in"))
    syms["Sensor_Motion:MPU6050"] = _sym(
        "Sensor_Motion:MPU6050", "MPU6050", "U", "MPU6050",
        "MPU-6050 class 6-axis IMU (accel+gyro, I2C+SPI, QFN-24)",
        "imu gyro gyroscope accelerometer accel 6axis mpu6050 motion i2c spi",
        pins=imu_pins,
        graphics=[_rect(-imu_w / 2, -imu_h / 2, imu_w / 2, imu_h / 2)],
        footprint_filters=["QFN-24*", "Package_DFN_QFN*"],
        eng={
            "power_w": {"typ": 0.012, "max": 0.03},
            "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VDD", 2.375, 3.3, 3.46, 0.0039, 0.000005),
                _rail("VLOGIC", 1.71, 3.3, 3.46, 0.0001, None),
            ], "voltage_domains": ["VDD", "VLOGIC"], "sequencing": []},
            "sources": ["research_datasheet_fetch: InvenSense MPU-6050 PS-MPU-6000A — "
                        "VDD 2.375–3.46 V, Idd 3.9 mA typ (accel+gyro), sleep 5 uA; "
                        "QFN-24 public pin map. Seed currents."],
        },
    )

    # ── BMP280 class barometric pressure (LGA-8) ───────────────────────────
    # Bosch BMP280 public LGA-8 pin map:
    # 1 VDDIO, 2 GND1, 3 SDI, 4 SCK, 5 SDO, 6 CSB, 7 GND2, 8 VDD
    bmp_w, bmp_h = 7.62, 12.7
    bmp_pins = [
        _pin("1", "VDDIO", [-bmp_w / 2 - 2.54, bmp_h / 2 - 1.27], 180.0, "power_in"),
        _pin("2", "GND1", [-bmp_w / 2 - 2.54, bmp_h / 2 - 1.27 - 2.54], 180.0, "power_in"),
        _pin("3", "SDI", [-bmp_w / 2 - 2.54, -bmp_h / 2 + 1.27 + 2.54], 180.0, "bidirectional"),
        _pin("4", "SCK", [-bmp_w / 2 - 2.54, -bmp_h / 2 + 1.27], 180.0, "input"),
        _pin("5", "SDO", [bmp_w / 2 + 2.54, -bmp_h / 2 + 1.27], 0.0, "bidirectional"),
        _pin("6", "CSB", [bmp_w / 2 + 2.54, -bmp_h / 2 + 1.27 + 2.54], 0.0, "input"),
        _pin("7", "GND2", [bmp_w / 2 + 2.54, bmp_h / 2 - 1.27 - 2.54], 0.0, "power_in"),
        _pin("8", "VDD", [bmp_w / 2 + 2.54, bmp_h / 2 - 1.27], 0.0, "power_in"),
    ]
    syms["Sensor_Pressure:BMP280"] = _sym(
        "Sensor_Pressure:BMP280", "BMP280", "U", "BMP280",
        "BMP280 class barometric pressure sensor (I2C/SPI, LGA-8)",
        "barometer pressure bmp280 sensor i2c spi altimeter",
        pins=bmp_pins,
        graphics=[_rect(-bmp_w / 2, -bmp_h / 2, bmp_w / 2, bmp_h / 2)],
        footprint_filters=["LGA-8*", "Package_LGA*"],
        eng={
            "power_w": {"typ": 0.00001, "max": 0.00003},
            "limits_c": {"tj_max": 85.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VDD", 1.71, 3.3, 3.6, 0.0000027, 0.0000001),
                _rail("VDDIO", 1.2, 3.3, 3.6, 0.0000001, None),
            ], "voltage_domains": ["VDD", "VDDIO"], "sequencing": []},
            "sources": ["research_datasheet_fetch: Bosch BMP280 — VDD 1.71–3.6 V, "
                        "Idd ~2.7 uA typ ultra-low power, LGA-8 public pin map."],
        },
    )

    # ── LIS3MDL class 3-axis magnetometer (LGA-12) ─────────────────────────
    # ST LIS3MDL public LGA-12 pin map:
    # 1 SCL/SPC, 2 CS, 3 SDA/SDI/SDO, 4 SDO/SA1, 6 GND, 8 GND,
    # 9 VDD_IO, 10 INT, 11 DRDY, 12 VDD
    mag_w, mag_h = 10.16, 15.24
    mag_named = {
        1: ("SCL", "bidirectional"), 2: ("CS", "input"),
        3: ("SDA", "bidirectional"), 4: ("SDO", "bidirectional"),
        6: ("GND", "power_in"), 8: ("GND", "power_in"),
        9: ("VDD_IO", "power_in"), 10: ("INT", "output"),
        11: ("DRDY", "output"), 12: ("VDD", "power_in"),
    }
    mag_pins = []
    for i in range(1, 13):
        if i <= 3:
            y = (2 - i) * 2.54
            pos, ang = [-mag_w / 2 - 2.54, y], 180.0
        elif i <= 6:
            x = (i - 4.5) * 2.54
            pos, ang = [x, -mag_h / 2 - 2.54], 270.0
        elif i <= 9:
            y = (i - 8) * 2.54
            pos, ang = [mag_w / 2 + 2.54, y], 0.0
        else:
            x = (10.5 - i) * 2.54
            pos, ang = [x, mag_h / 2 + 2.54], 90.0
        if i in mag_named:
            nm, ptype = mag_named[i]
        else:
            nm, ptype = f"NC{i}", "no_connect"
        mag_pins.append(_pin(str(i), nm, pos, ang, ptype))
    syms["Sensor_Magnetic:LIS3MDL"] = _sym(
        "Sensor_Magnetic:LIS3MDL", "LIS3MDL", "U", "LIS3MDL",
        "LIS3MDL class 3-axis magnetometer (I2C/SPI, LGA-12)",
        "magnetometer magnetic compass mag lis3mdl 3axis i2c spi sensor",
        pins=mag_pins,
        graphics=[_rect(-mag_w / 2, -mag_h / 2, mag_w / 2, mag_h / 2)],
        footprint_filters=["LGA-12*", "Package_LGA*"],
        eng={
            "power_w": {"typ": 0.0009, "max": 0.002},
            "limits_c": {"tj_max": 85.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VDD", 1.9, 3.3, 3.6, 0.00027, 0.000001),
                _rail("VDD_IO", 1.71, 3.3, 3.6, 0.00001, None),
            ], "voltage_domains": ["VDD", "VDD_IO"], "sequencing": []},
            "sources": ["research_datasheet_fetch: ST LIS3MDL — VDD 1.9–3.6 V, "
                        "Idd 270 uA typ continuous, LGA-12 public pin map."],
        },
    )

    # ── GNSS/GPS UART module (castellated breakout class) ──────────────────
    # Clean-room 1x6 edge module inspired by common NEO-6M breakout pinouts:
    # 1 VCC, 2 GND, 3 TXD, 4 RXD, 5 PPS, 6 NC  (not a copy of any vendor netlist)
    gps_pins = [
        _pin("1", "VCC", [-5.08, 6.35], 180.0, "power_in"),
        _pin("2", "GND", [-5.08, 3.81], 180.0, "power_in"),
        _pin("3", "TXD", [-5.08, 1.27], 180.0, "output"),
        _pin("4", "RXD", [-5.08, -1.27], 180.0, "input"),
        _pin("5", "PPS", [-5.08, -3.81], 180.0, "output"),
        _pin("6", "NC", [-5.08, -6.35], 180.0, "no_connect"),
    ]
    syms["RF_GPS:NEO6M"] = _sym(
        "RF_GPS:NEO6M", "NEO6M", "U", "NEO-6M",
        "GNSS/GPS UART module (castellated breakout class)",
        "gps gnss neo6m neo-6m uart module satellite navigation",
        pins=gps_pins,
        graphics=[_rect(-2.54, -8.89, 5.08, 8.89)],
        footprint_filters=["GPS*", "Module*", "Castellated*"],
        eng={
            "power_w": {"typ": 0.15, "max": 0.25},
            "limits_c": {"tj_max": 85.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VCC", 2.7, 3.3, 3.6, 0.045, 0.00002),
            ], "voltage_domains": ["VCC"], "sequencing": []},
            "sources": ["research_datasheet_fetch: u-blox NEO-6 class module envelope — "
                        "VCC 2.7–3.6 V, acquisition ~45 mA typ. Castellated breakout pin "
                        "order is a clean-room 1x6 class map (VCC/GND/TX/RX/PPS/NC)."],
        },
    )

    # ── 3V3 buck regulator SOT-23-6 (TPS562201-class) ──────────────────────
    # TI TPS562201 public SOT-23-6 pin map:
    # 1 VBST, 2 SW, 3 GND, 4 FB, 5 EN, 6 VIN
    syms["Regulator_Switching:Buck_3V3"] = _sym(
        "Regulator_Switching:Buck_3V3", "Buck_3V3", "U", "Buck_3V3",
        "3.3 V synchronous buck regulator (SOT-23-6 / TPS562201-class)",
        "buck regulator switching dcdc 3v3 3.3v switcher sot-23-6 tps562201",
        pins=[
            _pin("1", "VBST", [-7.62, 5.08], 180.0, "passive"),
            _pin("2", "SW", [-7.62, 2.54], 180.0, "output"),
            _pin("3", "GND", [-7.62, 0.0], 180.0, "power_in"),
            _pin("4", "FB", [-7.62, -2.54], 180.0, "input"),
            _pin("5", "EN", [-7.62, -5.08], 180.0, "input"),
            _pin("6", "VIN", [7.62, 0.0], 0.0, "power_in"),
        ],
        graphics=[_rect(-5.08, -6.35, 5.08, 6.35)],
        footprint_filters=["SOT-23-6*", "SOT-23*"],
        eng={
            "power_w": {"typ": 0.2, "max": 1.5},
            "limits_c": {"tj_max": 150.0, "ta_max": 125.0},
            "supply": {"rails": [
                _rail("VIN", 4.5, 5.0, 17.0, 0.001, None),
                _rail("VOUT", 3.2, 3.3, 3.4, None, None),
            ], "voltage_domains": ["VIN", "VOUT"], "sequencing": []},
            "sources": ["research_datasheet_fetch: TI TPS562201-class 2 A buck — "
                        "Vin 4.5–17 V, SOT-23-6 public pin map. Quiescent seed; load power "
                        "is circuit-dependent."],
        },
    )

    # ── 32.768 kHz crystal (2-pin) ─────────────────────────────────────────
    syms["Device:Crystal_32k768"] = _sym(
        "Device:Crystal_32k768", "Crystal_32k768", "Y", "32.768kHz",
        "32.768 kHz watch crystal (2-pin)",
        "crystal xtal 32768 32.768khz rtc oscillator watch",
        pins=[
            _pin("1", "1", [-3.81, 0.0], 180.0, "passive"),
            _pin("2", "2", [3.81, 0.0], 0.0, "passive"),
        ],
        graphics=[
            _rect(-1.5, -1.5, 1.5, 1.5),
            _line(-3.81, 0.0, -1.5, 0.0),
            _line(1.5, 0.0, 3.81, 0.0),
            _line(-1.0, -1.0, -1.0, 1.0),
            _line(1.0, -1.0, 1.0, 1.0),
        ],
        footprint_filters=["Crystal*", "XTAL*"],
    )

    # ── HSE crystal (2-pin, MCU high-speed external) ───────────────────────
    syms["Device:Crystal_HSE"] = _sym(
        "Device:Crystal_HSE", "Crystal_HSE", "Y", "8MHz",
        "HSE high-speed crystal (2-pin, MCU external clock)",
        "crystal xtal hse oscillator mcu clock 8mhz 16mhz",
        pins=[
            _pin("1", "1", [-3.81, 0.0], 180.0, "passive"),
            _pin("2", "2", [3.81, 0.0], 0.0, "passive"),
        ],
        graphics=[
            _rect(-1.5, -1.5, 1.5, 1.5),
            _line(-3.81, 0.0, -1.5, 0.0),
            _line(1.5, 0.0, 3.81, 0.0),
            _line(-1.0, -1.0, -1.0, 1.0),
            _line(1.0, -1.0, 1.0, 1.0),
        ],
        footprint_filters=["Crystal*", "XTAL*"],
    )

    # ── SWD 2x5 debug header (ARM 10-pin) ──────────────────────────────────
    # Same 1..10 numbering as Connector:Header_2x05; keywords add SWD/debug.
    swd = _header_2xn(5)
    swd["libid"] = "Connector:SWD_2x5"
    swd["name"] = "SWD_2x5"
    swd["value"] = "SWD_2x5"
    swd["description"] = "ARM 10-pin SWD/JTAG 2x5 header (2.54 mm)"
    swd["keywords"] = "swd jtag debug arm cortex connector header 2x5 pinheader"
    swd["footprint_filters"] = ["Header_2x05*", "PinHeader_2x*", "SWD*"]
    syms["Connector:SWD_2x5"] = swd

    # ── MCP73831 class linear Li-ion charger (SOT-23-5) ────────────────────
    # Microchip MCP73831 public SOT-23-5 pin map:
    # 1 STAT, 2 VSS, 3 VBAT, 4 VDD, 5 PROG
    syms["Battery_Management:MCP73831"] = _sym(
        "Battery_Management:MCP73831", "MCP73831", "U", "MCP73831",
        "MCP73831 class linear Li-ion charge controller (SOT-23-5)",
        "charger li-ion lithium battery mcp73831 linear charge management",
        pins=[
            _pin("1", "STAT", [-7.62, 2.54], 180.0, "open_collector"),
            _pin("2", "VSS", [-7.62, 0.0], 180.0, "power_in"),
            _pin("3", "VBAT", [-7.62, -2.54], 180.0, "power_out"),
            _pin("4", "VDD", [7.62, 1.27], 0.0, "power_in"),
            _pin("5", "PROG", [7.62, -1.27], 0.0, "passive"),
        ],
        graphics=[_rect(-5.08, -3.81, 5.08, 3.81)],
        footprint_filters=["SOT-23-5*", "SOT-23*"],
        eng={
            "power_w": {"typ": 0.3, "max": 0.6},
            "limits_c": {"tj_max": 150.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VDD", 3.75, 5.0, 6.0, 0.00055, None),
                _rail("VBAT", 0.0, 4.2, 4.2, None, None),
            ], "voltage_domains": ["VDD", "VBAT"], "sequencing": []},
            "sources": ["research_datasheet_fetch: Microchip MCP73831 — Vin 3.75–6 V, "
                        "programmable charge current via PROG, SOT-23-5 public pin map. "
                        "Dissipation is (Vin-Vbat)*Icharge."],
        },
    )

    # ── Switching Li-ion charger SOP-8 class (CN3703/MP2607-class) ─────────
    # Clean-room SOP-8 switching charger pin class (public common topology):
    # 1 TEMP(NTC), 2 ISET, 3 GND, 4 VIN, 5 SW, 6 VCC, 7 BAT, 8 STAT
    chg_w, chg_h = 10.16, 15.24
    chg_pins = [
        _pin("1", "TEMP", [-chg_w / 2 - 2.54, chg_h / 2 - 1.27], 180.0, "input"),
        _pin("2", "ISET", [-chg_w / 2 - 2.54, chg_h / 2 - 1.27 - 2.54], 180.0, "passive"),
        _pin("3", "GND", [-chg_w / 2 - 2.54, -chg_h / 2 + 1.27 + 2.54], 180.0, "power_in"),
        _pin("4", "VIN", [-chg_w / 2 - 2.54, -chg_h / 2 + 1.27], 180.0, "power_in"),
        _pin("5", "SW", [chg_w / 2 + 2.54, -chg_h / 2 + 1.27], 0.0, "output"),
        _pin("6", "VCC", [chg_w / 2 + 2.54, -chg_h / 2 + 1.27 + 2.54], 0.0, "power_out"),
        _pin("7", "BAT", [chg_w / 2 + 2.54, chg_h / 2 - 1.27 - 2.54], 0.0, "power_out"),
        _pin("8", "STAT", [chg_w / 2 + 2.54, chg_h / 2 - 1.27], 0.0, "open_collector"),
    ]
    syms["Battery_Management:SwitchingCharger_SOP8"] = _sym(
        "Battery_Management:SwitchingCharger_SOP8", "SwitchingCharger_SOP8", "U",
        "SW_CHG",
        "Switching Li-ion charge controller (SOP-8 class)",
        "charger li-ion lithium battery switching buck charge management sop8",
        pins=chg_pins,
        graphics=[_rect(-chg_w / 2, -chg_h / 2, chg_w / 2, chg_h / 2)],
        footprint_filters=["SOIC-8*", "SOP-8*", "Package_SO*"],
        eng={
            "power_w": {"typ": 0.5, "max": 2.0},
            "limits_c": {"tj_max": 150.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VIN", 4.5, 12.0, 28.0, 0.002, None),
                _rail("BAT", 0.0, 4.2, 4.2, None, None),
            ], "voltage_domains": ["VIN", "BAT", "VCC"], "sequencing": []},
            "sources": ["research_datasheet_fetch: CN3703/MP2607-class switching Li-ion "
                        "charger envelope — Vin up to ~28 V class, SOP-8 public topology "
                        "pin class (TEMP/ISET/GND/VIN/SW/VCC/BAT/STAT). Seed only."],
        },
    )

    # ── Power N-MOSFET SO-8 (multi-source / multi-drain) ───────────────────
    # Common single N-ch SO-8 pin map: 1-3 Source, 4 Gate, 5-8 Drain
    nmos_pwr_pins = [
        _pin("1", "S", [-7.62, 3.81], 180.0, "passive"),
        _pin("2", "S", [-7.62, 1.27], 180.0, "passive"),
        _pin("3", "S", [-7.62, -1.27], 180.0, "passive"),
        _pin("4", "G", [-7.62, -3.81], 180.0, "input"),
        _pin("5", "D", [7.62, -3.81], 0.0, "passive"),
        _pin("6", "D", [7.62, -1.27], 0.0, "passive"),
        _pin("7", "D", [7.62, 1.27], 0.0, "passive"),
        _pin("8", "D", [7.62, 3.81], 0.0, "passive"),
    ]
    syms["Device:NMOS_Power"] = _sym(
        "Device:NMOS_Power", "NMOS_Power", "Q", "NMOS_PWR",
        "N-channel power MOSFET (SO-8 multi-source/drain)",
        "nmos mosfet power_mosfet power transistor so-8 so8",
        pins=nmos_pwr_pins,
        graphics=[_rect(-5.08, -5.08, 5.08, 5.08), _circle(0.0, 0.0, 2.0)],
        sim={"model": "NMOS", "pin_order": ["D", "G", "S"]},
        footprint_filters=["SOIC-8*", "SO-8*", "Package_SO*"],
        eng={
            "limits_c": {"tj_max": 150.0, "ta_max": None},
            "sources": ["research_datasheet_fetch: SO-8 power N-MOSFET class (e.g. IRF7413) — "
                        "Tj max 150 °C. Pd circuit-dependent; multi-S multi-D pin map."],
        },
    )

    # ── Power P-MOSFET SO-8 ────────────────────────────────────────────────
    # Same multi-pin SO-8 map: 1-3 Source, 4 Gate, 5-8 Drain
    pmos_pwr_pins = [
        _pin("1", "S", [-7.62, 3.81], 180.0, "passive"),
        _pin("2", "S", [-7.62, 1.27], 180.0, "passive"),
        _pin("3", "S", [-7.62, -1.27], 180.0, "passive"),
        _pin("4", "G", [-7.62, -3.81], 180.0, "input"),
        _pin("5", "D", [7.62, -3.81], 0.0, "passive"),
        _pin("6", "D", [7.62, -1.27], 0.0, "passive"),
        _pin("7", "D", [7.62, 1.27], 0.0, "passive"),
        _pin("8", "D", [7.62, 3.81], 0.0, "passive"),
    ]
    syms["Device:PMOS_Power"] = _sym(
        "Device:PMOS_Power", "PMOS_Power", "Q", "PMOS_PWR",
        "P-channel power MOSFET (SO-8 multi-source/drain)",
        "pmos mosfet power_mosfet power transistor so-8 so8 reverse protection",
        pins=pmos_pwr_pins,
        graphics=[_rect(-5.08, -5.08, 5.08, 5.08), _circle(0.0, 0.0, 2.0)],
        sim={"model": "PMOS", "pin_order": ["D", "G", "S"]},
        footprint_filters=["SOIC-8*", "SO-8*", "Package_SO*"],
        eng={
            "limits_c": {"tj_max": 150.0, "ta_max": None},
            "sources": ["research_datasheet_fetch: SO-8 power P-MOSFET class — "
                        "Tj max 150 °C. Common reverse-protection / high-side topology."],
        },
    )

    # ── Reverse-protection P-MOSFET (SOT-23) ───────────────────────────────
    # Same SOT-23 numbers as Device:PMOS (BSS84-class): 1=G, 2=S, 3=D
    syms["Device:PMOS_Reverse"] = _sym(
        "Device:PMOS_Reverse", "PMOS_Reverse", "Q", "PMOS_REV",
        "P-channel MOSFET for reverse-polarity protection (SOT-23)",
        "pmos mosfet reverse protection polarity battery ideal diode",
        pins=[
            _pin("1", "G", [-5.08, 0.0], 180.0, "input"),
            _pin("2", "S", [0.0, 3.81], 90.0, "passive"),
            _pin("3", "D", [0.0, -3.81], 270.0, "passive"),
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
        sim={"model": "PMOS", "pin_order": ["D", "G", "S"]},
        footprint_filters=["SOT-23*"],
        eng={
            "limits_c": {"tj_max": 150.0, "ta_max": None},
            "sources": ["research_datasheet_fetch: BSS84-class P-MOS reverse-protection — "
                        "SOT-23 pin numbers 1=G 2=S 3=D."],
        },
    )

    # ── Current-sense resistor (keyword-tagged; 2512 footprint class) ──────
    syms["Device:R_Sense"] = _sym(
        "Device:R_Sense", "R_Sense", "R", "R_SENSE",
        "Current-sense shunt resistor (2512 class)",
        "current sense shunt resistor rsense current_sense 2512",
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
        footprint_filters=["R_2512*", "R_*"],
        eng={
            "sources": ["research_datasheet_fetch: thick-film current-sense 2512 class — "
                        "value and Pd are circuit-defined; footprint carries rated max."],
        },
    )

    # ── Load switch SOT-23-5 (TPS22918-class) ──────────────────────────────
    # TI TPS22918 public SOT-23-5 pin map: 1 VIN, 2 GND, 3 ON, 4 NC, 5 VOUT
    syms["Power_Management:Load_Switch"] = _sym(
        "Power_Management:Load_Switch", "Load_Switch", "U", "LOAD_SW",
        "Load switch IC (SOT-23-5 / TPS22918-class)",
        "load switch load_switch power switch tps22918 enable",
        pins=[
            _pin("1", "VIN", [-7.62, 2.54], 180.0, "power_in"),
            _pin("2", "GND", [-7.62, 0.0], 180.0, "power_in"),
            _pin("3", "ON", [-7.62, -2.54], 180.0, "input"),
            _pin("4", "NC", [7.62, -1.27], 0.0, "no_connect"),
            _pin("5", "VOUT", [7.62, 1.27], 0.0, "power_out"),
        ],
        graphics=[_rect(-5.08, -3.81, 5.08, 3.81)],
        footprint_filters=["SOT-23-5*", "SOT-23*"],
        eng={
            "power_w": {"typ": 0.05, "max": 0.5},
            "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VIN", 1.0, 3.3, 5.5, 0.00001, None),
            ], "voltage_domains": ["VIN", "VOUT"], "sequencing": []},
            "sources": ["research_datasheet_fetch: TI TPS22918-class load switch — "
                        "Vin 1–5.5 V, SOT-23-5 public pin map. Ron loss is I^2*Ron."],
        },
    )

    # ── LVD / undervoltage comparator (SOT-23-5 class) ─────────────────────
    # Clean-room LVD comparator class: 1 OUT, 2 VSS, 3 IN-, 4 IN+, 5 VDD
    # (similar topology to open-drain UVLO / window-comparator packages)
    syms["Power_Management:LVD_Comparator"] = _sym(
        "Power_Management:LVD_Comparator", "LVD_Comparator", "U", "LVD",
        "Low-voltage detect / undervoltage comparator (SOT-23-5)",
        "lvd comparator undervoltage uvlo brownout load switch monitor",
        pins=[
            _pin("1", "OUT", [-7.62, 2.54], 180.0, "open_collector"),
            _pin("2", "VSS", [-7.62, 0.0], 180.0, "power_in"),
            _pin("3", "IN-", [-7.62, -2.54], 180.0, "input"),
            _pin("4", "IN+", [7.62, -1.27], 0.0, "input"),
            _pin("5", "VDD", [7.62, 1.27], 0.0, "power_in"),
        ],
        graphics=[_rect(-5.08, -3.81, 5.08, 3.81)],
        footprint_filters=["SOT-23-5*", "SOT-23*"],
        eng={
            "power_w": {"typ": 0.00005, "max": 0.0002},
            "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
            "supply": {"rails": [
                _rail("VDD", 1.6, 3.3, 5.5, 0.00001, None),
            ], "voltage_domains": ["VDD"], "sequencing": []},
            "sources": ["research_datasheet_fetch: SOT-23-5 UVLO/LVD comparator class — "
                        "VDD 1.6–5.5 V envelope. Pin class OUT/VSS/IN-/IN+/VDD."],
        },
    )

    # ── NTC thermistor (2-pin) ─────────────────────────────────────────────
    syms["Device:NTC"] = _sym(
        "Device:NTC", "NTC", "RT", "NTC",
        "NTC thermistor (2-pin)",
        "ntc thermistor temperature sensor thermal battery",
        pins=[
            _pin("1", "~", [-2.54, 0.0], 180.0, "passive"),
            _pin("2", "~", [2.54, 0.0], 0.0, "passive"),
        ],
        graphics=[
            _rect(-1.016, -0.635, 1.016, 0.635),
            _line(-2.54, 0.0, -1.016, 0.0),
            _line(1.016, 0.0, 2.54, 0.0),
            # temperature tick marks
            _line(-0.5, 0.9, 0.5, 1.4),
            _line(-0.3, 1.2, 0.3, 1.7),
        ],
        sim={"model": "R", "pin_order": ["1", "2"]},
        footprint_filters=["R_*", "Thermistor*"],
        eng={
            "limits_c": {"tj_max": 125.0, "ta_max": 125.0},
            "sources": ["research_datasheet_fetch: NTC thermistor class (e.g. 10k 0402/0603) — "
                        "resistance is temperature-dependent; seed as passive R for netlist."],
        },
    )

    # ── Power inductor (larger SMD class) ──────────────────────────────────
    syms["Device:L_Power"] = _sym(
        "Device:L_Power", "L_Power", "L", "L_PWR",
        "Power inductor (SMD power-class footprint)",
        "inductor power coil choke buck boost smd",
        pins=[
            _pin("1", "~", [0.0, 2.54], 90.0, "passive"),
            _pin("2", "~", [0.0, -2.54], 270.0, "passive"),
        ],
        graphics=[
            _polyline([0.0, 2.54], [0.0, 1.8], [0.7, 1.2], [0.0, 0.6],
                      [-0.7, 0.0], [0.0, -0.6], [0.7, -1.2], [0.0, -1.8],
                      [0.0, -2.54]),
            _rect(-1.5, -2.0, 1.5, 2.0),
        ],
        sim={"model": "L", "pin_order": ["1", "2"]},
        footprint_filters=["L_Power*", "Inductor*", "L_*"],
        eng={
            "sources": ["research_datasheet_fetch: power inductor class (e.g. 4.7 uH 7x7 mm) — "
                        "Isat/Irms circuit-defined; footprint carries body dims."],
        },
    )

    return syms


# ─── engineering-metadata seeds (schema: contracts/ecad/boardphys.md) ─────────
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


# ─── FPGA parts ──────────────────────────────────────────────────────────────
# Symbol pins are GENERATED from the aicad.fpga_device/1 JSON seed in
# services/ecad/fpga_devices/ so the schematic part can never drift from the bank/pin model the
# FPGA checks and constraint exporters use. Only the full-coverage seed (the
# iCE40UP5K-SG48) gets a placeable symbol+footprint; partial seeds (ECP5/XC7A35T
# classes) are check/export data — tag any symbol with fields {"fpga_device": ...}
# to bind it to one of those.

def _make_fpga_symbols() -> dict:
    try:
        from services.ecad import fpga as _fpga
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

    # ── Header_1x2 / Header_1x3 P2.54 ────────────────────────────────────
    # Live 2026-07-30: Connector:Conn_2 / Conn_3 are offered by ecad_find_part('connector')
    # but had no 1xN footprint to default to, so a two-pin power inlet was dropped off the
    # board by sync with no diagnostic. Same geometry family as the 1x4 above.
    for _n in (2, 3):
        _pads = [_tht_pad(f"p{i+1}", str(i + 1), 0.0, ((_n - 1) / 2.0 - i) * 2.54,
                          1.0, 1.7, 1.7) for i in range(_n)]
        _half = (_n - 1) * 2.54 / 2.0 + 1.27
        fps[f"Connector_PinHeader_2.54mm:Header_1x{_n}"] = _fp(
            f"Connector_PinHeader_2.54mm:Header_1x{_n}",
            pads=_pads,
            graphics=(_silk_box(-1.0, -_half, 1.0, _half)
                      + _crtyd_box(-1.6, -_half - 0.6, 1.6, _half + 0.6)),
            attr="tht",
            description=f"1x{_n} pin header 2.54mm pitch",
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

    # ── Inductor SMD L_0603 / L_0805 (same pad geometry as R of same size) ──
    fps["Inductor_SMD:L_0603"] = _fp(
        "Inductor_SMD:L_0603",
        pads=[
            _smd_pad("p1", "1", -0.8, 0.0, 0.9, 0.9, 0.25),
            _smd_pad("p2", "2", 0.8, 0.0, 0.9, 0.9, 0.25),
        ],
        graphics=_silk_box(-1.0, -0.5, 1.0, 0.5) + _crtyd_box(-1.3, -0.7, 1.3, 0.7),
        description="Inductor 0603",
    )
    fps["Inductor_SMD:L_0805"] = _fp(
        "Inductor_SMD:L_0805",
        pads=[
            _smd_pad("p1", "1", -1.0, 0.0, 1.15, 1.45, 0.25),
            _smd_pad("p2", "2", 1.0, 0.0, 1.15, 1.45, 0.25),
        ],
        graphics=_silk_box(-1.2, -0.7, 1.2, 0.7) + _crtyd_box(-1.7, -1.0, 1.7, 1.0),
        description="Inductor 0805",
    )
    # Ferrite bead reuses 0603 geometry with FB-friendly fpid.
    fps["Inductor_SMD:FB_0603"] = _fp(
        "Inductor_SMD:FB_0603",
        pads=[
            _smd_pad("p1", "1", -0.8, 0.0, 0.9, 0.9, 0.25),
            _smd_pad("p2", "2", 0.8, 0.0, 0.9, 0.9, 0.25),
        ],
        graphics=_silk_box(-1.0, -0.5, 1.0, 0.5) + _crtyd_box(-1.3, -0.7, 1.3, 0.7),
        description="Ferrite bead 0603",
    )

    # ── Polarised electrolytic radial (THT, 2 pins) ────────────────────────
    # Generic 5 mm can, 2.0 mm pitch: pad 1 = +, pad 2 = −.
    fps["Capacitor_THT:CP_Radial_D5.0mm_P2.00mm"] = _fp(
        "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm",
        pads=[
            _tht_pad("p1", "1", -1.0, 0.0, 0.7, 1.6, 1.6),
            _tht_pad("p2", "2", 1.0, 0.0, 0.7, 1.6, 1.6),
        ],
        graphics=[
            {"type": "circle", "center": [0.0, 0.0], "radius": 2.5,
             "layer": "F.SilkS", "width": _SILK_W},
            _line(-0.5, 1.5, 0.5, 1.5),
            _line(0.0, 1.0, 0.0, 2.0),
        ] + _crtyd_box(-3.0, -3.0, 3.0, 3.0),
        attr="tht",
        description="Radial electrolytic capacitor D5.0 mm pitch 2.0 mm",
    )

    # ── SOT-223 (4 pads: 3 leads + tab) ────────────────────────────────────
    # JEDEC TO-261 nominal: pins 1/2/3 bottom, tab pin 4 top.
    fps["Package_TO_SOT_SMD:SOT-223"] = _fp(
        "Package_TO_SOT_SMD:SOT-223",
        pads=[
            _smd_pad("p1", "1", -2.3, -2.15, 1.2, 1.8, 0.2),
            _smd_pad("p2", "2", 0.0, -2.15, 1.2, 1.8, 0.2),
            _smd_pad("p3", "3", 2.3, -2.15, 1.2, 1.8, 0.2),
            _smd_pad("p4", "4", 0.0, 2.35, 3.6, 2.15, 0.1),
        ],
        graphics=_silk_box(-3.3, -1.8, 3.3, 1.8) + _crtyd_box(-3.8, -3.5, 3.8, 3.8),
        description="SOT-223 3-lead + tab",
    )

    # ── TO-220-3 (THT) ─────────────────────────────────────────────────────
    fps["Package_TO_SOT_THT:TO-220-3"] = _fp(
        "Package_TO_SOT_THT:TO-220-3",
        pads=[
            _tht_pad("p1", "1", -2.54, 0.0, 1.0, 2.0, 2.0),
            _tht_pad("p2", "2", 0.0, 0.0, 1.0, 2.0, 2.0),
            _tht_pad("p3", "3", 2.54, 0.0, 1.0, 2.0, 2.0),
        ],
        graphics=_silk_box(-5.0, -2.0, 5.0, 8.0) + _crtyd_box(-5.5, -2.5, 5.5, 8.5),
        attr="tht",
        description="TO-220 3-lead through-hole",
    )

    # ── 2xN pin headers 2.54 mm ────────────────────────────────────────────
    def _header_2xn_fp(cols: int) -> dict:
        pads = []
        for row in range(cols):
            y = (cols - 1) * 1.27 - row * 2.54
            n_left = row * 2 + 1
            n_right = row * 2 + 2
            pads.append(_tht_pad(f"p{n_left}", str(n_left), -1.27, y, 1.0, 1.7, 1.7))
            pads.append(_tht_pad(f"p{n_right}", str(n_right), 1.27, y, 1.0, 1.7, 1.7))
        h = cols * 1.27 + 1.5
        fpid = f"Connector_PinHeader_2.54mm:Header_2x{cols:02d}"
        return _fp(
            fpid, pads=pads,
            graphics=_silk_box(-2.0, -h, 2.0, h) + _crtyd_box(-2.6, -h - 0.5, 2.6, h + 0.5),
            attr="tht",
            description=f"2x{cols} pin header 2.54 mm pitch",
        )

    fps["Connector_PinHeader_2.54mm:Header_2x03"] = _header_2xn_fp(3)
    fps["Connector_PinHeader_2.54mm:Header_2x05"] = _header_2xn_fp(5)

    # ── USB-B receptacle (4 pins, simplified THT) ──────────────────────────
    fps["Connector_USB:USB_B"] = _fp(
        "Connector_USB:USB_B",
        pads=[
            _tht_pad("p1", "1", -3.5, 2.5, 1.0, 1.8, 1.8),
            _tht_pad("p2", "2", -1.2, 2.5, 1.0, 1.8, 1.8),
            _tht_pad("p3", "3", 1.2, 2.5, 1.0, 1.8, 1.8),
            _tht_pad("p4", "4", 3.5, 2.5, 1.0, 1.8, 1.8),
        ],
        graphics=_silk_box(-6.0, -4.0, 6.0, 6.0) + _crtyd_box(-6.5, -4.5, 6.5, 6.5),
        attr="tht",
        description="USB Type-B receptacle 4-pin",
    )

    # ── USB-C receptacle (simplified 16 SMD pads) ──────────────────────────
    usbc_pads = []
    for i in range(16):
        # two rows of 8, pitch 0.5 mm
        row = 0 if i < 8 else 1
        col = i if i < 8 else i - 8
        x = (col - 3.5) * 0.5
        y = -1.6 if row == 0 else 1.6
        usbc_pads.append(_smd_pad(f"p{i+1}", str(i + 1), x, y, 0.3, 1.0, 0.25))
    fps["Connector_USB:USB_C"] = _fp(
        "Connector_USB:USB_C",
        pads=usbc_pads,
        graphics=_silk_box(-4.5, -3.5, 4.5, 3.5) + _crtyd_box(-5.0, -4.0, 5.0, 4.0),
        description="USB Type-C receptacle simplified 16-pad",
    )

    # ── LQFP-48 7x7 mm 0.5 mm pitch (generic MCU) ──────────────────────────
    lqfp48_pads = []
    for i in range(12):  # 1-12 left, top to bottom
        y = (5.5 - i) * 0.5
        lqfp48_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -4.0, y, 1.0, 0.28, 0.25))
    for i in range(12):  # 13-24 bottom, left to right
        x = (i - 5.5) * 0.5
        lqfp48_pads.append(_smd_pad(f"p{i+13}", str(i + 13), x, -4.0, 0.28, 1.0, 0.25))
    for i in range(12):  # 25-36 right, bottom to top
        y = (i - 5.5) * 0.5
        lqfp48_pads.append(_smd_pad(f"p{i+25}", str(i + 25), 4.0, y, 1.0, 0.28, 0.25))
    for i in range(12):  # 37-48 top, right to left
        x = (5.5 - i) * 0.5
        lqfp48_pads.append(_smd_pad(f"p{i+37}", str(i + 37), x, 4.0, 0.28, 1.0, 0.25))
    fps["Package_QFP:LQFP-48_7x7mm_P0.5mm"] = _fp(
        "Package_QFP:LQFP-48_7x7mm_P0.5mm",
        pads=lqfp48_pads,
        graphics=_silk_box(-3.6, -3.6, 3.6, 3.6) + _crtyd_box(-5.0, -5.0, 5.0, 5.0)
        + [_circle(-4.5, 3.5, 0.15)],
        description="LQFP-48 7x7 mm 0.5 mm pitch",
    )


    # ═══════════════════════════════════════════════════════════════════════
    # Flight-computer + battery-charger footprints (clean-room / IPC-style)
    # ═══════════════════════════════════════════════════════════════════════

    # ── QFN-24 4x4 mm 0.5 mm pitch + EP (MPU-6050 class) ───────────────────
    # 6 pads/side; pad 0.25x0.6 at body edge; EP pin 25.
    qfn24_pads = []
    for i in range(6):  # 1-6 left, top to bottom
        y = (2.5 - i) * 0.5
        qfn24_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -2.05, y, 0.6, 0.25, 0.25))
    for i in range(6):  # 7-12 bottom, left to right
        x = (i - 2.5) * 0.5
        qfn24_pads.append(_smd_pad(f"p{i+7}", str(i + 7), x, -2.05, 0.25, 0.6, 0.25))
    for i in range(6):  # 13-18 right, bottom to top
        y = (i - 2.5) * 0.5
        qfn24_pads.append(_smd_pad(f"p{i+13}", str(i + 13), 2.05, y, 0.6, 0.25, 0.25))
    for i in range(6):  # 19-24 top, right to left
        x = (2.5 - i) * 0.5
        qfn24_pads.append(_smd_pad(f"p{i+19}", str(i + 19), x, 2.05, 0.25, 0.6, 0.25))
    qfn24_pads.append(_smd_pad("p25", "25", 0.0, 0.0, 2.4, 2.4, 0.05))
    fps["Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"] = _fp(
        "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm",
        pads=qfn24_pads,
        graphics=_silk_box(-2.1, -2.1, 2.1, 2.1) + _crtyd_box(-2.7, -2.7, 2.7, 2.7)
        + [_circle(-2.4, 1.8, 0.12)],
        description="QFN-24 4x4 mm 0.5 mm pitch with exposed pad (pin 25)",
    )

    # ── LGA-8 2.0x2.5 mm (BMP280 class) ────────────────────────────────────
    # 4 pads/side along length; pads 1-4 left top→bottom, 5-8 right bottom→top.
    lga8_pads = []
    for i in range(4):
        y = (1.5 - i) * 0.65
        lga8_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -0.85, y, 0.4, 0.35, 0.25))
    for i in range(4):
        y = (i - 1.5) * 0.65
        lga8_pads.append(_smd_pad(f"p{i+5}", str(i + 5), 0.85, y, 0.4, 0.35, 0.25))
    fps["Package_LGA:LGA-8_2x2.5mm"] = _fp(
        "Package_LGA:LGA-8_2x2.5mm",
        pads=lga8_pads,
        graphics=_silk_box(-1.1, -1.35, 1.1, 1.35) + _crtyd_box(-1.5, -1.7, 1.5, 1.7)
        + [_circle(-1.2, 1.1, 0.1)],
        description="LGA-8 2.0x2.5 mm (BMP280-class)",
    )

    # ── LGA-12 2.0x2.0 mm (LIS3MDL class) ──────────────────────────────────
    # 3 pads/side; pads 1-3 left, 4-6 bottom, 7-9 right, 10-12 top.
    lga12_pads = []
    for i in range(3):
        y = (1 - i) * 0.5
        lga12_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -0.9, y, 0.35, 0.3, 0.25))
    for i in range(3):
        x = (i - 1) * 0.5
        lga12_pads.append(_smd_pad(f"p{i+4}", str(i + 4), x, -0.9, 0.3, 0.35, 0.25))
    for i in range(3):
        y = (i - 1) * 0.5
        lga12_pads.append(_smd_pad(f"p{i+7}", str(i + 7), 0.9, y, 0.35, 0.3, 0.25))
    for i in range(3):
        x = (1 - i) * 0.5
        lga12_pads.append(_smd_pad(f"p{i+10}", str(i + 10), x, 0.9, 0.3, 0.35, 0.25))
    fps["Package_LGA:LGA-12_2x2mm"] = _fp(
        "Package_LGA:LGA-12_2x2mm",
        pads=lga12_pads,
        graphics=_silk_box(-1.05, -1.05, 1.05, 1.05) + _crtyd_box(-1.4, -1.4, 1.4, 1.4)
        + [_circle(-1.15, 0.9, 0.1)],
        description="LGA-12 2x2 mm (LIS3MDL-class magnetometer)",
    )

    # ── Castellated GPS module 1x6 edge (NEO-6M breakout class) ────────────
    gps_pads = []
    for i in range(6):
        y = (2.5 - i) * 2.54
        gps_pads.append(_smd_pad(f"p{i+1}", str(i + 1), -8.0, y, 1.5, 1.2, 0.2))
    fps["RF_Module:GPS_Castellated_1x6"] = _fp(
        "RF_Module:GPS_Castellated_1x6",
        pads=gps_pads,
        graphics=_silk_box(-7.5, -8.5, 8.5, 8.5) + _crtyd_box(-9.0, -9.5, 9.5, 9.5),
        description="Castellated GNSS/GPS module 1x6 edge pads",
    )

    # ── SOT-23-5 ───────────────────────────────────────────────────────────
    # JEDEC MO-178: pins 1/2/3 bottom L→R, pins 4/5 top R→L (pin 5 above 1-side).
    fps["Package_TO_SOT_SMD:SOT-23-5"] = _fp(
        "Package_TO_SOT_SMD:SOT-23-5",
        pads=[
            _smd_pad("p1", "1", -0.95, -1.2, 0.55, 0.8, 0.25),
            _smd_pad("p2", "2", 0.0, -1.2, 0.55, 0.8, 0.25),
            _smd_pad("p3", "3", 0.95, -1.2, 0.55, 0.8, 0.25),
            _smd_pad("p4", "4", 0.95, 1.2, 0.55, 0.8, 0.25),
            _smd_pad("p5", "5", -0.95, 1.2, 0.55, 0.8, 0.25),
        ],
        graphics=_silk_box(-1.5, -0.8, 1.5, 0.8) + _crtyd_box(-1.9, -1.9, 1.9, 1.9),
        description="SOT-23-5 5-lead",
    )

    # ── SOT-23-6 ───────────────────────────────────────────────────────────
    # Pins 1/2/3 bottom L→R, pins 4/5/6 top R→L.
    fps["Package_TO_SOT_SMD:SOT-23-6"] = _fp(
        "Package_TO_SOT_SMD:SOT-23-6",
        pads=[
            _smd_pad("p1", "1", -0.95, -1.2, 0.55, 0.8, 0.25),
            _smd_pad("p2", "2", 0.0, -1.2, 0.55, 0.8, 0.25),
            _smd_pad("p3", "3", 0.95, -1.2, 0.55, 0.8, 0.25),
            _smd_pad("p4", "4", 0.95, 1.2, 0.55, 0.8, 0.25),
            _smd_pad("p5", "5", 0.0, 1.2, 0.55, 0.8, 0.25),
            _smd_pad("p6", "6", -0.95, 1.2, 0.55, 0.8, 0.25),
        ],
        graphics=_silk_box(-1.5, -0.8, 1.5, 0.8) + _crtyd_box(-1.9, -1.9, 1.9, 1.9),
        description="SOT-23-6 6-lead",
    )

    # ── Crystal 2-pin HC-49 / 3215 class ───────────────────────────────────
    fps["Crystal:Crystal_HC49-4H_Vertical"] = _fp(
        "Crystal:Crystal_HC49-4H_Vertical",
        pads=[
            _tht_pad("p1", "1", -2.45, 0.0, 0.8, 1.6, 1.6),
            _tht_pad("p2", "2", 2.45, 0.0, 0.8, 1.6, 1.6),
        ],
        graphics=_silk_box(-5.5, -2.0, 5.5, 2.0) + _crtyd_box(-6.0, -2.5, 6.0, 2.5),
        attr="tht",
        description="Crystal HC-49 2-pin through-hole",
    )
    fps["Crystal:Crystal_SMD_3215-2Pin"] = _fp(
        "Crystal:Crystal_SMD_3215-2Pin",
        pads=[
            _smd_pad("p1", "1", -1.0, 0.0, 1.2, 1.4, 0.25),
            _smd_pad("p2", "2", 1.0, 0.0, 1.2, 1.4, 0.25),
        ],
        graphics=_silk_box(-1.7, -0.85, 1.7, 0.85) + _crtyd_box(-2.2, -1.2, 2.2, 1.2),
        description="Crystal SMD 3215 2-pin (32.768 kHz class)",
    )

    # ── Current-sense resistor 2512 ────────────────────────────────────────
    fps["Resistor_SMD:R_2512"] = _fp(
        "Resistor_SMD:R_2512",
        pads=[
            _smd_pad("p1", "1", -2.65, 0.0, 1.8, 3.2, 0.2),
            _smd_pad("p2", "2", 2.65, 0.0, 1.8, 3.2, 0.2),
        ],
        graphics=_silk_box(-3.2, -1.7, 3.2, 1.7) + _crtyd_box(-3.9, -2.2, 3.9, 2.2),
        description="Resistor 2512 (current-sense class)",
    )

    # ── Power inductor 7x7 mm 2-pad ────────────────────────────────────────
    fps["Inductor_SMD:L_Power_7x7"] = _fp(
        "Inductor_SMD:L_Power_7x7",
        pads=[
            _smd_pad("p1", "1", -2.8, 0.0, 2.2, 3.5, 0.15),
            _smd_pad("p2", "2", 2.8, 0.0, 2.2, 3.5, 0.15),
        ],
        graphics=_silk_box(-3.6, -3.6, 3.6, 3.6) + _crtyd_box(-4.2, -4.2, 4.2, 4.2),
        description="Power inductor 7x7 mm 2-pad",
    )

    # ── Bulk electrolytic radial D8.0 mm pitch 3.5 mm ──────────────────────
    fps["Capacitor_THT:CP_Radial_D8.0mm_P3.50mm"] = _fp(
        "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm",
        pads=[
            _tht_pad("p1", "1", -1.75, 0.0, 0.8, 1.8, 1.8),
            _tht_pad("p2", "2", 1.75, 0.0, 0.8, 1.8, 1.8),
        ],
        graphics=[
            {"type": "circle", "center": [0.0, 0.0], "radius": 4.0,
             "layer": "F.SilkS", "width": _SILK_W},
            _line(-0.6, 2.2, 0.6, 2.2),
            _line(0.0, 1.6, 0.0, 2.8),
        ] + _crtyd_box(-4.5, -4.5, 4.5, 4.5),
        attr="tht",
        description="Radial electrolytic capacitor D8.0 mm pitch 3.5 mm",
    )


    # Final pass: clip every footprint's silk off its pads so no generated footprint
    # ships silk over copper (DFM silk_over_pad) — generic, covers all sizes + future parts.
    for fp in fps.values():
        _clip_footprint_silk(fp)

    return fps


# ─── footprint engineering-metadata seeds ─────────────────────────────────────
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
    "Inductor_SMD:L_0603": _box(1.6, 0.8, 0.8),
    "Inductor_SMD:L_0805": _box(2.0, 1.25, 1.25),
    "Inductor_SMD:FB_0603": _box(1.6, 0.8, 0.8),
    "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm":
        _pkg_eng("cylinder", d_mm=5.0, h_mm=11.0, source=_JEDEC_SRC),
    "Package_TO_SOT_SMD:SOT-223": _box(6.5, 3.5, 1.8, theta_ja=60.0),
    "Package_TO_SOT_THT:TO-220-3": _box(10.0, 4.5, 9.0, theta_ja=50.0),
    "Connector_PinHeader_2.54mm:Header_2x03":
        _box(7.62, 5.08, 8.5, source=_JEDEC_SRC + " Header height incl. pins (nominal)."),
    "Connector_PinHeader_2.54mm:Header_2x05":
        _box(12.7, 5.08, 8.5, source=_JEDEC_SRC + " Header height incl. pins (nominal)."),
    "Connector_USB:USB_B": _box(12.0, 11.0, 10.0),
    "Connector_USB:USB_C": _box(8.9, 7.3, 3.2),
    "Package_QFP:LQFP-48_7x7mm_P0.5mm": _box(7.0, 7.0, 1.4, theta_ja=45.0),
    # Flight-computer / charger pack packages
    "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm": _box(4.0, 4.0, 0.9, theta_ja=35.0),
    "Package_LGA:LGA-8_2x2.5mm": _box(2.0, 2.5, 0.95, theta_ja=80.0),
    "Package_LGA:LGA-12_2x2mm": _box(2.0, 2.0, 1.0, theta_ja=90.0),
    "RF_Module:GPS_Castellated_1x6": _box(16.0, 12.2, 2.4),
    "Package_TO_SOT_SMD:SOT-23-5": _box(2.9, 1.6, 1.15, theta_ja=220.0),
    "Package_TO_SOT_SMD:SOT-23-6": _box(2.9, 1.6, 1.15, theta_ja=200.0),
    "Crystal:Crystal_HC49-4H_Vertical": _box(11.0, 4.7, 3.5),
    "Crystal:Crystal_SMD_3215-2Pin": _box(3.2, 1.5, 0.9),
    "Resistor_SMD:R_2512": _box(6.3, 3.2, 0.7),
    "Inductor_SMD:L_Power_7x7": _box(7.0, 7.0, 4.0),
    "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm":
        _pkg_eng("cylinder", d_mm=8.0, h_mm=12.0, source=_JEDEC_SRC),
}
_FOOTPRINT_ENG["Connector_PinHeader_2.54mm:Header_1x4"] = \
    _FOOTPRINT_ENG["Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"]

# Rated max dissipation for the standard thick-film resistor of each size (power_w.typ
# stays null — actual dissipation is circuit-defined and must come from sim/overrides).
for _fpid, _pmax in (("Resistor_SMD:R_0402", 0.0625), ("Resistor_SMD:R_0603", 0.1),
                     ("Resistor_SMD:R_0805", 0.125), ("Resistor_SMD:R_1206", 0.25),
                     ("Resistor_SMD:R_2512", 1.0)):
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
