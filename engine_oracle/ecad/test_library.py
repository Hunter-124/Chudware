"""Test suite for engine.ecad.library.

Run from repo root:
    python -m engine.ecad.test_library

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
    from engine.ecad.library.sexpr import parse, dumps

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
    from engine.ecad.library.kicad_sym import load_kicad_sym

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
    from engine.ecad.library.kicad_mod import load_kicad_mod

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
    from engine.ecad.schema import new_schematic, validate_schematic
    from engine.ecad.library.starter import SYMBOLS

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
    from engine.ecad.schema import new_board, validate_board
    from engine.ecad.library.starter import FOOTPRINTS

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
    from engine.ecad import library

    # Starter symbols
    for libid in ["Device:R", "Device:C", "Device:L", "Device:D",
                  "Device:LED", "Device:Q_NPN", "Device:NMOS",
                  "Amplifier_Operational:OPAMP_dual", "Timer:NE555",
                  "Connector:Conn_2", "Connector:Header_1x4",
                  "power:GND", "power:VCC", "power:+5V",
                  "Simulation_SPICE:V_source", "Simulation_SPICE:I_source"]:
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
    from engine.ecad.library.starter import SYMBOLS

    required_sim = {
        "Device:R": "R",
        "Device:C": "C",
        "Device:L": "L",
        "Device:D": "D",
        "Simulation_SPICE:V_source": "V",
        "Simulation_SPICE:I_source": "I",
        "Amplifier_Operational:OPAMP_dual": "OPAMP",
        "Timer:NE555": "SUBCKT",
    }
    for libid, expected_model in required_sim.items():
        sym = SYMBOLS.get(libid)
        assert sym is not None, f"{libid} not in SYMBOLS"
        sim = sym.get("sim")
        assert sim is not None, f"{libid} missing sim mapping"
        assert sim.get("model") == expected_model, \
            f"{libid}: sim model {sim.get('model')!r} != {expected_model!r}"
    _ok(f"{len(required_sim)} sim mappings verified")


# ─── 7. Electrical pin-type correctness ──────────────────────────────────────

def test_pin_types() -> None:
    print("\n[7] pin type correctness")
    from engine.ecad.library.starter import SYMBOLS

    def _types(libid: str) -> set[str]:
        sym = SYMBOLS[libid]
        return {p["type"] for p in sym.get("pins", [])}

    # Passives
    for libid in ["Device:R", "Device:C", "Device:L"]:
        t = _types(libid)
        assert t == {"passive"}, f"{libid} pins should all be passive, got {t}"
    _ok("R/C/L pins all passive")

    # Op-amp
    opamp_types = _types("Amplifier_Operational:OPAMP_dual")
    assert "input" in opamp_types
    assert "output" in opamp_types
    assert "power_in" in opamp_types
    _ok("OPAMP_dual has input/output/power_in pins")

    # Power symbols
    for libid in ["power:GND", "power:VCC", "power:+5V"]:
        t = _types(libid)
        assert "power_out" in t, f"{libid} should have power_out pin"
    _ok("power symbols have power_out pins")

    # V_source / I_source
    for libid in ["Simulation_SPICE:V_source", "Simulation_SPICE:I_source"]:
        t = _types(libid)
        assert "power_out" in t, f"{libid} should have power_out pins"
    _ok("V_source/I_source have power_out pins")

    # NE555 has power_in
    ne_types = _types("Timer:NE555")
    assert "power_in" in ne_types, "NE555 should have power_in pin"
    assert "output" in ne_types, "NE555 should have output pin"
    _ok("NE555 has power_in and output pins")


# ─── 8. multi-unit OPAMP_dual unit validation ────────────────────────────────

def test_multiunit_symbol() -> None:
    print("\n[8] multi-unit symbol (OPAMP_dual)")
    from engine.ecad.schema import new_schematic, validate_schematic
    from engine.ecad.library.starter import SYMBOLS

    libid = "Amplifier_Operational:OPAMP_dual"
    sdef = SYMBOLS[libid]
    assert sdef["units_count"] == 3, f"units_count: {sdef['units_count']}"

    # Each unit 1 and 2 should validate individually
    for unit in (1, 2):
        sch = new_schematic("opamp_test")
        sch["lib_cache"][libid] = sdef
        sch["symbols"].append({
            "id": "s1",
            "libid": libid,
            "sheet": "sh1",
            "ref": "U?",
            "value": "OPAMP",
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
    _ok("OPAMP_dual unit 1 and 2 each validate")

# ─── 9. pin numbers ⊆ default footprint pads ─────────────────────────────────

def test_default_footprints_pin_subset_of_pads() -> None:
    print("\n[9] _DEFAULT_FOOTPRINTS pin numbers ⊆ footprint pads")
    from engine.ecad.library.starter import SYMBOLS, FOOTPRINTS
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
    _ok(f"{len(_DEFAULT_FOOTPRINTS)} default footprint mappings: pins ⊆ pads")


# ─── 10. LM358 datasheet pinout golden ───────────────────────────────────────

def test_lm358_datasheet_pinout() -> None:
    print("\n[10] LM358 DIP-8/SOIC-8 datasheet pinout")
    from engine.ecad.library.starter import SYMBOLS

    # TI SLOS068 public pin map (clean-room).
    golden = {
        "1": "OUT1",
        "2": "IN1-",
        "3": "IN1+",
        "4": "V-",
        "5": "IN2+",
        "6": "IN2-",
        "7": "OUT2",
        "8": "V+",
    }
    sdef = SYMBOLS["Amplifier_Operational:LM358"]
    got = {str(p["number"]): str(p["name"]) for p in sdef["pins"]}
    assert got == golden, f"LM358 pinout {got} != golden {golden}"
    # OPAMP_dual remains abstract multi-unit (not a package claim).
    dual = SYMBOLS["Amplifier_Operational:OPAMP_dual"]
    assert dual["units_count"] == 3
    assert "not a package pinout" in dual["description"].lower() or \
           "abstract" in dual["description"].lower()
    _ok("LM358 pinout golden; OPAMP_dual stays abstract multi-unit")


# ─── 11. BJT/MOSFET SOT-23 numeric pin numbers ───────────────────────────────

def test_bjt_mosfet_sot23_pin_numbers() -> None:
    print("\n[11] BJT/MOSFET pin numbers map to SOT-23 pads 1/2/3")
    from engine.ecad.library.starter import SYMBOLS, FOOTPRINTS

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
    from engine.ecad import library

    for q in ("mcu", "regulator", "usb", "inductor"):
        hits = library.search(q)
        assert len(hits) >= 1, f"search({q!r}) returned 0 hits: {hits}"
        _ok(f"search({q!r}) → {len(hits)} hit(s)")



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
