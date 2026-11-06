"""Test suite for services.ecad.library.

Run from repo root:
    python -m services.ecad.test_library

House style: main() + asserts, zero external dependencies.
"""

from __future__ import annotations

import sys


# ─── test helpers ─────────────────────────────────────────────────────────────

def _fail(msg: str) -> None:
    print(f"  FAIL: {msg}", file=sys.stderr)
    raise AssertionError(msg)


def _ok(label: str) -> None:
    print(f"  ok  {label}")


# ─── representative KiCad snippets (inline) ───────────────────────────────────

_KICAD_SYM_SNIPPET = r"""
(kicad_symbol_lib (version 20220914) (generator kicad_symbol_editor)
  (symbol "TestRes"
    (property "Reference" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (property "Value" "TestRes" (at 0 -2.54 0))
    (property "Footprint" "Resistor_SMD:R_0603" (at 0 0 0))
    (property "Description" "Test resistor" (at 0 0 0))
    (symbol "TestRes_0_1"
      (rectangle (start -1.016 -0.635) (end 1.016 0.635)
        (stroke (width 0.2) (type default)) (fill (type none)))
    )
    (symbol "TestRes_1_1"
      (pin passive line (at -3.81 0 0) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 3.81 0 180) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""

_KICAD_MOD_SNIPPET = r"""
(footprint "TestPad" (layer "F.Cu")
  (attr smd)
  (fp_line (start -1.5 -1.0) (end 1.5 -1.0) (layer "F.SilkS") (width 0.12))
  (fp_line (start 1.5 -1.0) (end 1.5 1.0) (layer "F.SilkS") (width 0.12))
  (fp_line (start 1.5 1.0) (end -1.5 1.0) (layer "F.SilkS") (width 0.12))
  (fp_line (start -1.5 1.0) (end -1.5 -1.0) (layer "F.SilkS") (width 0.12))
  (pad "1" smd roundrect (at -0.8 0) (size 0.9 0.9)
    (layers "F.Cu" "F.Mask" "F.Paste")
    (roundrect_rratio 0.25))
  (pad "2" smd roundrect (at 0.8 0) (size 0.9 0.9)
    (layers "F.Cu" "F.Mask" "F.Paste")
    (roundrect_rratio 0.25))
)
"""

_KICAD_MOD_THT_SNIPPET = r"""
(footprint "TestTHT" (layer "F.Cu")
  (attr through_hole)
  (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6)
    (layers "F.Cu" "B.Cu" "F.Mask" "B.Mask")
    (drill 0.8))
  (pad "2" thru_hole circle (at 2.54 0) (size 1.6 1.6)
    (layers "F.Cu" "B.Cu" "F.Mask" "B.Mask")
    (drill 0.8))
)
"""


# ─── 1. sexpr round-trip ──────────────────────────────────────────────────────

def test_sexpr_roundtrip() -> None:
    print("\n[1] sexpr round-trip")
    from services.ecad.library.sexpr import parse, dumps

    # kicad_sym snippet
    tree1 = parse(_KICAD_SYM_SNIPPET)
    assert isinstance(tree1, list), "sym: root must be list"
    assert tree1[0] == "kicad_symbol_lib"
    text1 = dumps(tree1)
    tree1b = parse(text1)
    assert tree1 == tree1b, "sym: round-trip not structurally stable"
    _ok("kicad_sym snippet round-trips")

    # kicad_mod snippet
    tree2 = parse(_KICAD_MOD_SNIPPET)
    assert isinstance(tree2, list)
    assert tree2[0] == "footprint"
    text2 = dumps(tree2)
    tree2b = parse(text2)
    assert tree2 == tree2b, "mod: round-trip not structurally stable"
    _ok("kicad_mod snippet round-trips")

    # numbers preserved
    tree3 = parse("(at -1.016 0.635 90)")
    assert tree3 == ["at", -1.016, 0.635, 90]
    _ok("numbers parsed correctly (int vs float)")

    # quoted strings with escapes
    tree4 = parse('(prop "hello \\"world\\"" 42)')
    assert tree4 == ["prop", 'hello "world"', 42]
    _ok("quoted string escape handling")


# ─── 2. kicad_sym and kicad_mod parsing ──────────────────────────────────────

def test_kicad_sym_parsing() -> None:
    print("\n[2] kicad_sym parsing")
    from services.ecad.library.kicad_sym import load_kicad_sym

    result = load_kicad_sym(_KICAD_SYM_SNIPPET)
    assert "Device:TestRes" in result, f"expected Device:TestRes in {list(result)}"
    sdef = result["Device:TestRes"]
    assert sdef["reference_prefix"] == "R", f"ref prefix: {sdef['reference_prefix']}"
    pins = sdef["pins"]
    assert len(pins) == 2, f"expected 2 pins, got {len(pins)}"
    pnums = {p["number"] for p in pins}
    assert pnums == {"1", "2"}, f"pin numbers: {pnums}"
    for p in pins:
        assert p["type"] == "passive", f"pin type: {p['type']}"
        assert isinstance(p["pos"], list) and len(p["pos"]) == 2
        assert isinstance(p["angle"], (int, float))
    # y-flip: KiCad pin at y=0, angle=0 → our y=0 (no flip on zero; test non-zero below)
    graphics = sdef["graphics"]
    assert len(graphics) >= 1, "expected at least one graphic"
    rect = next((g for g in graphics if g["type"] == "rect"), None)
    assert rect is not None, "expected a rect graphic"
    _ok("TestRes symbol parsed: 2 passive pins, rect body")


def test_kicad_mod_parsing() -> None:
    print("\n[2b] kicad_mod parsing")
    from services.ecad.library.kicad_mod import load_kicad_mod

    # SMD footprint
    fp = load_kicad_mod(_KICAD_MOD_SNIPPET)
    assert fp["attr"] == "smd", f"attr: {fp['attr']}"
    pads = fp["pads"]
    assert len(pads) == 2, f"expected 2 pads, got {len(pads)}"
    pnums = {p["number"] for p in pads}
    assert pnums == {"1", "2"}, f"pad numbers: {pnums}"
    for p in pads:
        assert p["type"] == "smd"
        assert p["drill"] is None, "smd pad should have no drill"
        assert p["shape"] == "roundrect"
        assert p["roundrect_ratio"] == 0.25
    assert len(fp["graphics"]) >= 4, "expected silk lines"
    _ok("SMD footprint parsed: 2 roundrect pads, silk graphics")

    # THT footprint
    fp2 = load_kicad_mod(_KICAD_MOD_THT_SNIPPET)
    assert fp2["attr"] == "tht", f"attr: {fp2['attr']}"
    for p in fp2["pads"]:
        assert p["type"] == "tht"
        assert p["drill"] is not None, "tht pad must have drill"
        assert p["drill"]["diameter"] == 0.8
    _ok("THT footprint parsed: 2 tht pads with drill")


# ─── 3. All starter symbols validate against validate_schematic ───────────────

def test_starter_symbols_validate() -> None:
    print("\n[3] starter symbols validate_schematic")
    from services.ecad.schema import new_schematic, validate_schematic
    from services.ecad.library.starter import SYMBOLS

    failures: list[str] = []
    for libid, sdef in SYMBOLS.items():
        sch = new_schematic(f"test_{libid}")
        sch["lib_cache"][libid] = sdef
        sch["symbols"].append({
            "id": "s1",
            "libid": libid,
            "sheet": "sh1",
            "ref": "X?",
            "value": sdef.get("value", "?"),
            "footprint": "",
            "pos": [0.0, 0.0],
            "angle": 0.0,
            "mirror": "none",
            "unit": 1,
            "in_bom": True,
            "on_board": True,
            "dnp": False,
        })
        errs = validate_schematic(sch)
        if errs:
            failures.append(f"{libid}: {errs}")

    if failures:
        for f in failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        _fail(f"{len(failures)} starter symbol(s) failed validation")
    _ok(f"all {len(SYMBOLS)} starter symbols validate with zero errors")


# ─── 4. All starter footprints validate against validate_board ────────────────

def test_starter_footprints_validate() -> None:
    print("\n[4] starter footprints validate_board")
    from services.ecad.schema import new_board, validate_board
    from services.ecad.library.starter import FOOTPRINTS

    failures: list[str] = []
    for fpid, fpdef in FOOTPRINTS.items():
        board = new_board(f"test_{fpid}")
        # Build placed footprint from the def
        placed: dict = {
            "id": "fp1",
            "fpid": fpid,
            "ref": "X1",
            "value": fpid.split(":")[-1],
            "pos": [50.0, 50.0],
            "angle": 0.0,
            "side": "top",
            "pads": [],
            "graphics": fpdef.get("graphics", []),
            "model_3d": None,
            "attr": fpdef.get("attr", "smd"),
            "linked_symbol": None,
        }
        # Place pads; net must be None or in board nets ("GND" is always there)
        for k, pad in enumerate(fpdef.get("pads", [])):
            p = dict(pad)
            p["id"] = f"p{k+1}"
            p["net"] = None   # unassigned
            placed["pads"].append(p)
        board["footprints"].append(placed)
        errs = validate_board(board)
        if errs:
            failures.append(f"{fpid}: {errs}")

    if failures:
        for f in failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        _fail(f"{len(failures)} starter footprint(s) failed validation")
    _ok(f"all {len(FOOTPRINTS)} starter footprints validate with zero errors")


# ─── 5. library.resolve lookups ───────────────────────────────────────────────

def test_resolve_lookups() -> None:
    print("\n[5] library.resolve / resolve_footprint")
    from services.ecad import library

    # Starter symbols
    for libid in ["Device:R", "Device:C", "Device:L", "Device:D",
                  "Device:LED", "Device:Q_NPN", "Device:NMOS",
                  "Amplifier_Operational:LM358", "Simulation_SPICE:OPAMP",
                  "Timer:NE555P", "Timer:NE555D",
                  "Connector:Conn_2", "Connector:Header_1x4",
                  "power:GND", "power:VCC", "power:+5V",
                  "Simulation_SPICE:VDC", "Simulation_SPICE:VSIN",
                  "Simulation_SPICE:VPULSE", "Simulation_SPICE:I_source"]:
        sym = library.resolve(libid)
        if sym is None:
            _fail(f"resolve({libid!r}) returned None")
        assert sym["libid"] == libid, f"libid mismatch: {sym['libid']!r} != {libid!r}"
    _ok("all required starter symbols resolve correctly")

    # Starter footprints
    for fpid in ["Resistor_SMD:R_0603", "Capacitor_SMD:C_0603",
                 "Package_SO:SOIC-8", "Package_TO_SOT_SMD:SOT-23",
                 "Package_DIP:DIP-8",
                 "Connector_PinHeader_2.54mm:Header_1x4",
                 "LED_SMD:LED_0805", "Diode_SMD:D_SMA"]:
        fp = library.resolve_footprint(fpid)
        if fp is None:
            _fail(f"resolve_footprint({fpid!r}) returned None")
        assert fp["fpid"] == fpid, f"fpid mismatch: {fp['fpid']!r} != {fpid!r}"
    _ok("all required starter footprints resolve correctly")

    # Missing symbols return None
    assert library.resolve("Nonexistent:Part") is None
    _ok("resolve of nonexistent libid returns None")

    # Search works
    hits = library.search("resistor")
    assert any(h.get("libid") == "Device:R" for h in hits), \
        f"search('resistor') didn't find Device:R: {hits}"
    _ok("search('resistor') finds Device:R")

    hits2 = library.search("soic")
    assert any("SOIC" in h.get("fpid", "") or "SOIC" in h.get("name", "")
               for h in hits2), f"search('soic') found: {hits2}"
    _ok("search('soic') finds SOIC footprints")


# ─── 6. Sim mappings present on key symbols ───────────────────────────────────

def test_sim_mappings() -> None:
    print("\n[6] sim mappings")
    from services.ecad.library.starter import SYMBOLS

    required_sim = {
        "Device:R": "R",
        "Device:C": "C",
        "Device:L": "L",
        "Device:D": "D",
        "Simulation_SPICE:VDC": "V",
        "Simulation_SPICE:VSIN": "V",
        "Simulation_SPICE:VPULSE": "V",
        "Simulation_SPICE:I_source": "I",
        "Timer:NE555P": "SUBCKT",
        "Timer:NE555D": "SUBCKT",
    }
    for libid, expected_model in required_sim.items():
        sym = SYMBOLS.get(libid)
        assert sym is not None, f"{libid} not in SYMBOLS"
        sim = sym.get("sim")
        assert sim is not None, f"{libid} missing sim mapping"
        assert sim.get("model") == expected_model, \
            f"{libid}: sim model {sim.get('model')!r} != {expected_model!r}"
    for libid in ("Timer:NE555P", "Timer:NE555D"):
        sim = SYMBOLS[libid]["sim"]
        assert sim["subckt"] == "NE555", f"{libid}: {sim}"
        assert sim["pin_order"] == [str(n) for n in range(1, 9)], f"{libid}: {sim}"

    # The physical dual op-amp cannot be represented by a single ideal model:
    # that would silently omit its second amplifier. Use KiCad's simulation
    # symbol and its explicit five-terminal subcircuit mapping instead.
    assert "sim" not in SYMBOLS["Amplifier_Operational:LM358"]
    ideal = SYMBOLS["Simulation_SPICE:OPAMP"]
    assert "sim" not in ideal
    fields = ideal["fields"]
    assert fields["Sim.Device"] == "SUBCKT"
    assert fields["Sim.Library"].endswith("/Simulation_SPICE.sp")
    assert fields["Sim.Name"] == "kicad_builtin_opamp"
    assert fields["Sim.Pins"] == "1=in+ 2=in- 3=vcc 4=vee 5=out"
    _ok(f"{len(required_sim)} sim mappings verified")


# ─── 7. Electrical pin-type correctness ──────────────────────────────────────

def test_pin_types() -> None:
    print("\n[7] pin type correctness")
    from services.ecad.library.starter import SYMBOLS

    def _types(libid: str) -> set[str]:
        sym = SYMBOLS[libid]
        return {p["type"] for p in sym.get("pins", [])}

    # Passives
    for libid in ["Device:R", "Device:C", "Device:L"]:
        t = _types(libid)
        assert t == {"passive"}, f"{libid} pins should all be passive, got {t}"
    _ok("R/C/L pins all passive")

    # Physical and simulated op-amps both expose input, output and supply pins.
    for libid in ("Amplifier_Operational:LM358", "Simulation_SPICE:OPAMP"):
        opamp_types = _types(libid)
        assert opamp_types == {"input", "output", "power_in"}, f"{libid}: {opamp_types}"
    _ok("physical and simulated op-amps have input/output/power_in pins")

    # Power symbols
    for libid in ["power:GND", "power:VCC", "power:+5V"]:
        t = _types(libid)
        assert "power_out" in t, f"{libid} should have power_out pin"
    _ok("power symbols have power_out pins")

    # KiCad voltage-source terminals are passive, numbered pins; current source
    # remains the separate starter-only source model.
    for libid in ("Simulation_SPICE:VDC", "Simulation_SPICE:VSIN",
                  "Simulation_SPICE:VPULSE"):
        assert _types(libid) == {"passive"}, f"{libid}: {_types(libid)}"
        sym = SYMBOLS[libid]
        assert [p["number"] for p in sym["pins"]] == ["1", "2"]
        assert sym["sim"]["pin_order"] == ["1", "2"]
        assert sym["fields"]["Sim.Device"] == "V"
        assert sym["fields"]["Sim.Pins"] == "1=+ 2=-"
    assert "power_out" in _types("Simulation_SPICE:I_source")
    _ok("KiCad voltage sources have passive numbered pins and Sim.Pins mapping")

    # Both physical 555 packages retain the KiCad electrical pin types.
    for libid in ("Timer:NE555P", "Timer:NE555D"):
        ne_types = _types(libid)
        assert {"power_in", "output"} <= ne_types, f"{libid}: {ne_types}"
    _ok("NE555P and NE555D have power_in and output pins")


# ─── 8. multi-unit LM358 unit validation ────────────────────────────────────

def test_multiunit_symbol() -> None:
    print("\n[8] multi-unit symbol (LM358)")
    from services.ecad.schema import iter_placed_pins, new_schematic, validate_schematic
    from services.ecad.library.starter import SYMBOLS

    libid = "Amplifier_Operational:LM358"
    sdef = SYMBOLS[libid]
    expected_units = {1: {"1", "2", "3"}, 2: {"5", "6", "7"}, 3: {"4", "8"}}
    assert sdef["units_count"] == len(expected_units), f"units_count: {sdef['units_count']}"

    for unit, expected_pins in expected_units.items():
        unit_pins = [p["number"] for p in sdef["pins"] if p.get("unit", 1) == unit]
        assert len(unit_pins) == len(expected_pins) and set(unit_pins) == expected_pins, \
            f"unit {unit}: {unit_pins}"
        sch = new_schematic("opamp_test")
        sch["lib_cache"][libid] = sdef
        sch["symbols"].append({
            "id": "s1",
            "libid": libid,
            "sheet": "sh1",
            "ref": "U?",
            "value": "LM358",
            "footprint": "",
            "pos": [0.0, 0.0],
            "angle": 0.0,
            "mirror": "none",
            "unit": unit,
            "in_bom": True,
            "on_board": True,
            "dnp": False,
        })
        errs = validate_schematic(sch)
        assert not errs, f"unit {unit} validation failed: {errs}"
        placed = list(iter_placed_pins(sch))
        assert len(placed) == len(expected_pins)
        assert {pin["number"] for _, pin, _ in placed} == expected_pins
        assert len({pos for _, _, pos in placed}) == len(expected_pins), \
            f"unit {unit}: overlapping pin endpoints: {placed}"
    _ok("LM358 units 1, 2 and 3 validate and expose only their physical pins")

# ─── 9. pin numbers ⊆ default footprint pads ─────────────────────────────────

def test_default_footprints_pin_subset_of_pads() -> None:
    print("\n[9] _DEFAULT_FOOTPRINTS pin numbers ⊆ footprint pads")
    from services.ecad.library.starter import SYMBOLS, FOOTPRINTS
    from services.ecad_design.orchestrator import _DEFAULT_FOOTPRINTS

    assert _DEFAULT_FOOTPRINTS, "_DEFAULT_FOOTPRINTS must be non-empty"
    for libid, fpid in _DEFAULT_FOOTPRINTS.items():
        assert libid in SYMBOLS, f"default map libid missing from SYMBOLS: {libid}"
        assert fpid in FOOTPRINTS, f"default map fpid missing from FOOTPRINTS: {fpid}"
        pin_nums = {str(p.get("number")) for p in SYMBOLS[libid].get("pins", [])}
        pad_nums = {str(p.get("number")) for p in FOOTPRINTS[fpid].get("pads", [])}
        missing = pin_nums - pad_nums
        assert not missing, (
            f"{libid} → {fpid}: pin numbers {sorted(missing)} not in pads {sorted(pad_nums)}"
        )
    assert _DEFAULT_FOOTPRINTS["Timer:NE555P"].startswith("Package_DIP:DIP-8")
    assert _DEFAULT_FOOTPRINTS["Timer:NE555D"].startswith("Package_SO:SOIC-8")
    _ok(f"{len(_DEFAULT_FOOTPRINTS)} default footprint mappings: pins ⊆ pads")


# ─── 10. LM358 datasheet pinout golden ───────────────────────────────────────

def test_lm358_datasheet_pinout() -> None:
    print("\n[10] LM358 DIP-8/SOIC-8 datasheet pinout")
    from services.ecad.library.starter import SYMBOLS

    # TI SLOS068 DIP-8/SOIC-8 map, with KiCad LM358 pin names and units.
    # Outputs are unnamed on the KiCad symbol; their type identifies them.
    golden = {
        "1": ("", "output", 1),     # OUT1
        "2": ("-", "input", 1),     # IN1-
        "3": ("+", "input", 1),     # IN1+
        "4": ("V-", "power_in", 3),
        "5": ("+", "input", 2),     # IN2+
        "6": ("-", "input", 2),     # IN2-
        "7": ("", "output", 2),     # OUT2
        "8": ("V+", "power_in", 3),
    }
    sdef = SYMBOLS["Amplifier_Operational:LM358"]
    pins = sdef["pins"]
    got = {p["number"]: (p["name"], p["type"], p["unit"]) for p in pins}
    assert len(pins) == len(golden), f"LM358 has duplicate or extra pins: {pins}"
    assert got == golden, f"LM358 pinout {got} != golden {golden}"
    _ok("LM358 physical pinout, electrical types and unit memberships match KiCad")


def test_ne555_datasheet_pinout() -> None:
    print("\n[10b] NE555 DIP-8/SOIC-8 datasheet pinout")
    from services.ecad.library.starter import SYMBOLS
    from services.ecad.schema import iter_placed_pins, new_schematic

    # KiCad's supply pins belong to common unit 0 and appear on unit 1.
    golden = {
        "1": ("GND", "power_in", 0),
        "2": ("TRIG", "input", 1),
        "3": ("OUT", "output", 1),
        "4": ("~{RST}", "input", 1),
        "5": ("CONT", "passive", 1),
        "6": ("THRES", "input", 1),
        "7": ("DISCH", "input", 1),
        "8": ("VCC", "power_in", 0),
    }
    for libid in ("Timer:NE555P", "Timer:NE555D"):
        sdef = SYMBOLS[libid]
        assert sdef["units_count"] == 1, f"{libid}: {sdef['units_count']}"
        pins = sdef["pins"]
        got = {p["number"]: (p["name"], p["type"], p["unit"]) for p in pins}
        assert len(pins) == len(golden), f"{libid} has duplicate or extra pins: {pins}"
        assert got == golden, f"{libid} pinout {got} != golden {golden}"
        sch = new_schematic("timer_test")
        sch["lib_cache"][libid] = sdef
        sch["symbols"].append({"libid": libid, "unit": 1, "pos": [0.0, 0.0]})
        placed = list(iter_placed_pins(sch))
        assert len(placed) == len(golden)
        assert {pin["number"] for _, pin, _ in placed} == set(golden)
        assert len({pos for _, _, pos in placed}) == len(golden), \
            f"{libid}: overlapping pin endpoints: {placed}"
    _ok("NE555P and NE555D physical pins, types and common supplies match KiCad")


def test_sim_opamp_pinout() -> None:
    print("\n[10c] KiCad simulation op-amp pinout")
    from services.ecad.library.starter import SYMBOLS

    golden = {
        "1": ("+", "input", 1),
        "2": ("-", "input", 1),
        "3": ("V+", "power_in", 1),
        "4": ("V-", "power_in", 1),
        "5": ("", "output", 1),  # OUT
    }
    sdef = SYMBOLS["Simulation_SPICE:OPAMP"]
    assert sdef["units_count"] == 1
    pins = sdef["pins"]
    got = {p["number"]: (p["name"], p["type"], p["unit"]) for p in pins}
    assert len(pins) == len(golden), f"simulation op-amp has duplicate or extra pins: {pins}"
    assert got == golden, f"simulation op-amp pinout {got} != golden {golden}"
    _ok("simulation op-amp five-terminal pinout and electrical types match KiCad")


def test_physical_part_engineering_metadata() -> None:
    print("\n[10d] physical timer and op-amp engineering metadata")
    from services.ecad.library.starter import SYMBOLS

    timer_rail = {
        "name": "VCC", "voltage_v_min": 4.5, "voltage_v_nom": 5.0,
        "voltage_v_max": 16.0, "current_a_active": 0.003, "current_a_sleep": None,
    }
    opamp_rail = {
        "name": "V+", "voltage_v_min": 3.0, "voltage_v_nom": 5.0,
        "voltage_v_max": 32.0, "current_a_active": 0.0007, "current_a_sleep": None,
    }
    expectations = (
        ("Timer:NE555P", {"typ": 0.015, "max": 0.225},
         {"tj_max": 150.0, "ta_max": 70.0}, timer_rail, "SLFS022"),
        ("Timer:NE555D", {"typ": 0.015, "max": 0.225},
         {"tj_max": 150.0, "ta_max": 70.0}, timer_rail, "SLFS022"),
        ("Amplifier_Operational:LM358", {"typ": 0.0035, "max": 0.0384},
         {"tj_max": 150.0, "ta_max": 85.0}, opamp_rail, "SLOS068"),
    )
    for libid, power, limits, rail, datasheet in expectations:
        eng = SYMBOLS[libid]["eng"]
        assert eng["power_w"] == power, f"{libid}: {eng}"
        assert eng["limits_c"] == limits, f"{libid}: {eng}"
        assert eng["supply"] == {
            "rails": [rail], "voltage_domains": [rail["name"]], "sequencing": [],
        }, f"{libid}: {eng}"
        assert any(datasheet in source for source in eng["sources"]), f"{libid}: {eng}"
    _ok("physical part power, limits, rails and datasheet provenance preserved")


# ─── 11. BJT/MOSFET SOT-23 numeric pin numbers ───────────────────────────────

def test_bjt_mosfet_sot23_pin_numbers() -> None:
    print("\n[11] BJT/MOSFET pin numbers map to SOT-23 pads 1/2/3")
    from services.ecad.library.starter import SYMBOLS, FOOTPRINTS

    pads = {str(p["number"]) for p in FOOTPRINTS["Package_TO_SOT_SMD:SOT-23"]["pads"]}
    assert pads == {"1", "2", "3"}
    for libid in ("Device:Q_NPN", "Device:Q_PNP", "Device:NMOS", "Device:PMOS"):
        pins = {str(p["number"]) for p in SYMBOLS[libid]["pins"]}
        assert pins == {"1", "2", "3"}, f"{libid} pins {pins} not {{1,2,3}}"
        assert pins <= pads
    # Name map sanity (MMBT3904 / 2N7002 class).
    npn = {str(p["number"]): p["name"] for p in SYMBOLS["Device:Q_NPN"]["pins"]}
    assert npn == {"1": "B", "2": "E", "3": "C"}
    nmos = {str(p["number"]): p["name"] for p in SYMBOLS["Device:NMOS"]["pins"]}
    assert nmos == {"1": "G", "2": "S", "3": "D"}
    _ok("BJT/MOSFET SOT-23 pin numbers + names")


# ─── 12. real-board essentials search hits ───────────────────────────────────

def test_board_essentials_search() -> None:
    print("\n[12] search mcu/regulator/usb/inductor each ≥1 hit")
    from services.ecad import library

    for q in ("mcu", "regulator", "usb", "inductor"):
        hits = library.search(q)
        assert len(hits) >= 1, f"search({q!r}) returned 0 hits: {hits}"
        _ok(f"search({q!r}) → {len(hits)} hit(s)")


def test_python_recipe_model_fields_export_to_kicad_spice(tmp_path) -> None:
    """Fallback recipes retain editable KiCad models and yield real SPICE netlists."""
    import shutil
    import pytest
    from services.ecad_design import orchestrator, recipes
    from services.mcp.ecad_common import _rpc, ecad_session, netlist_mod

    if not shutil.which("kicad-cli"):
        pytest.skip("KiCad CLI is required for SPICE export")
    for name, design in (("filter", recipes.rc_lowpass()),
                         ("timer", recipes.rc_555_astable())):
        target = tmp_path / f"{name}.kicad_sch"
        session = ecad_session.new_session("sch", name, str(target))
        result = orchestrator.build_design(session, design)
        assert not result["missing_pins"], result
        components = {c["ref"]: c for c in netlist_mod.build_netlist(session.doc)["components"]}
        if name == "filter":
            assert components["V1"]["fields"]["Sim.Type"] == "SIN"
            assert "sim" not in components["V1"]
        else:
            assert components["U1"]["fields"]["Sim.Device"] == "SPICE"
            assert any(".subckt NE555" in text.get("text", "")
                       for text in session.doc.get("texts", []))
        assert target.is_file() and ecad_session.close_session(session.doc_id)
        reopened = ecad_session.open_path(str(target))
        exported = _rpc("ecad_export", {"doc_id": reopened.doc_id, "kind": "spice"})
        spice = exported["files"][0]["content"]
        if name == "filter":
            assert "sin(" in spice.lower() and "ac 1" in spice.lower(), spice
        else:
            assert ".subckt NE555" in spice and ".model Dclamp" in spice, spice
            assert any(line.startswith("XU1 ") and line.rstrip().endswith(" NE555")
                       for line in spice.splitlines()), spice



# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("engine.ecad.library test suite")
    print("=" * 60)

    tests = [
        test_sexpr_roundtrip,
        test_kicad_sym_parsing,
        test_kicad_mod_parsing,
        test_starter_symbols_validate,
        test_starter_footprints_validate,
        test_resolve_lookups,
        test_sim_mappings,
        test_pin_types,
        test_multiunit_symbol,
        test_default_footprints_pin_subset_of_pads,
        test_lm358_datasheet_pinout,
        test_ne555_datasheet_pinout,
        test_sim_opamp_pinout,
        test_physical_part_engineering_metadata,
        test_bjt_mosfet_sot23_pin_numbers,
        test_board_essentials_search,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"\n  ASSERTION FAILED in {t.__name__}: {e}", file=sys.stderr)
            failed += 1
        except Exception as e:
            import traceback
            print(f"\n  ERROR in {t.__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    if failed == 0:
        print(f"PASS — {passed}/{passed} tests passed")
    else:
        print(f"FAIL — {passed} passed, {failed} FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
