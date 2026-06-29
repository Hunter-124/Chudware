"""Tests for engine.ecad.formats — .ecad+ container round-trip + KiCad importers.

House style: main() + asserts, same as test_rpc.py.
Run:
  & "C:\\Users\\nigga\\AppData\\Roaming\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" -m engine.ecad.test_formats
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Minimal inline KiCad schematics
# ---------------------------------------------------------------------------

_MINIMAL_SCH = """\
(kicad_sch (version 20221018) (generator AI-ECAD-test)
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_0_1"
        (polyline (pts (xy -1.016 0) (xy 1.016 0)) (stroke (width 0)) (fill (type none)))
      )
      (symbol "R_1_1"
        (pin passive line (at -2.54 0 0) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 2.54 0 180) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
      (property "Reference" "R" (at 1.524 1.778 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 1.524 -1.778 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    )
    (symbol "Device:C"
      (symbol "C_1_1"
        (pin passive line (at 0 2.54 270) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
      (property "Reference" "C" (at 1.524 1.778 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 1.524 -1.778 0) (effects (font (size 1.27 1.27))))
    )
  )
  (symbol (lib_id "Device:R") (at 50.8 50.8 0) (unit 1)
    (property "Reference" "R1" (at 52.324 52.578 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 52.324 50.038 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 50.8 50.8 0) (effects (font (size 1.27 1.27)) hide))
  )
  (symbol (lib_id "Device:C") (at 60.96 50.8 0) (unit 1)
    (property "Reference" "C1" (at 62.484 52.578 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 62.484 50.038 0) (effects (font (size 1.27 1.27))))
  )
  (wire (pts (xy 45.72 50.8) (xy 48.26 50.8)))
  (label "CLK" (at 45.72 50.8 0) (effects (font (size 1.27 1.27))))
  (global_label "VCC" (at 60.96 45.72 90) (shape input) (effects (font (size 1.27 1.27))))
  (junction (at 50.8 50.8))
  (no_connect (at 60.96 55.88))
)
"""

_MINIMAL_PCB = """\
(kicad_pcb (version 20221018) (generator AI-ECAD-test)
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "B.CrtYd" user)
    (45 "F.CrtYd" user)
    (46 "B.Fab" user)
    (47 "F.Fab" user)
    (48 "B.SilkS" user)
    (49 "F.SilkS" user)
    (50 "Edge.Cuts" user)
  )
  (net 1 "GND")
  (net 2 "VCC")
  (net 3 "NET1")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10 0)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 3 "NET1"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10 0)
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS"))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 3 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 2 "VCC"))
  )
  (segment (start 9.5 10) (end 10 10) (width 0.25) (layer "F.Cu") (net 1))
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.05))
)
"""


# ---------------------------------------------------------------------------
# test 1: .ecad+ round-trip
# ---------------------------------------------------------------------------

def test_ecad_plus_roundtrip() -> None:
    """Build a schematic, save to .ecad+, open, verify doc equality + validation."""
    from engine.ecad import formats, schema

    with tempfile.TemporaryDirectory() as td:
        ecad_path = os.path.join(td, "test_sch.ecad+")
        unknown_entry = b"hello from the future"

        # Build a minimal schematic doc directly
        doc = schema.new_schematic(name="RoundTripSch")
        # Add a stub lib_cache entry + a symbol
        libid = "Device:R"
        doc["lib_cache"][libid] = {
            "libid": libid,
            "name": "R",
            "description": "Resistor",
            "keywords": "res",
            "reference_prefix": "R",
            "value": "R",
            "footprint_filters": [],
            "pins": [
                {"number": "1", "name": "", "pos": [-2.54, 0.0], "angle": 0.0,
                 "length": 2.54, "type": "passive", "shape": "line", "unit": 1},
                {"number": "2", "name": "", "pos": [2.54, 0.0], "angle": 180.0,
                 "length": 2.54, "type": "passive", "shape": "line", "unit": 1},
            ],
            "graphics": [],
            "units_count": 1,
            "power": False,
            "fields": {},
        }
        doc["symbols"].append({
            "id": "s1",
            "libid": libid,
            "sheet": "sh1",
            "ref": "R1",
            "value": "10k",
            "footprint": "",
            "pos": [10.0, 10.0],
            "angle": 0.0,
            "mirror": "none",
            "unit": 1,
            "fields": {},
            "in_bom": True,
            "on_board": True,
            "dnp": False,
        })
        doc["wires"].append({
            "id": "w1",
            "sheet": "sh1",
            "pts": [[0.0, 0.0], [10.0, 0.0]],
            "bus": False,
        })

        # Inject an unknown entry (simulates forward-compat data from a future app version)
        doc["_extra_entries"] = {"future_data.bin": unknown_entry}

        # Validate original
        errs = schema.validate_schematic(doc)
        assert not errs, f"Original doc invalid: {errs}"

        # Save
        out_path = formats.save(doc, ecad_path)
        assert out_path == ecad_path, f"save() returned wrong path: {out_path}"
        assert os.path.isfile(ecad_path), ".ecad+ file not created"

        # Open
        doc2, warnings = formats.open(ecad_path)
        assert isinstance(doc2, dict), "open() did not return a dict"
        assert isinstance(warnings, list), "open() did not return warnings list"

        # REGRESSION (Wave Y): the opened doc must be JSON-serialisable — the worker emits the
        # ecad_open response as JSON, and _extra_entries used to carry raw bytes (thumbnail / unknown
        # entries) which raised "Object of type bytes is not JSON serializable" over the RPC boundary.
        # Entries are now base64 strings; json.dumps must succeed.
        json.dumps(doc2)  # raises TypeError if any bytes leaked into the doc

        # Validate reopened
        errs2 = schema.validate_schematic(doc2)
        assert not errs2, f"Reopened doc invalid: {errs2}"

        # Structure check
        assert doc2.get("schema") == "aicad.sch/1"
        assert doc2.get("name") == "RoundTripSch"
        syms2 = doc2.get("symbols", [])
        assert len(syms2) == 1, f"Expected 1 symbol, got {len(syms2)}"
        assert syms2[0]["ref"] == "R1"
        wires2 = doc2.get("wires", [])
        assert len(wires2) == 1, f"Expected 1 wire, got {len(wires2)}"

        # Unknown entry preserved on resave (now base64-encoded str for JSON-safety; decode to compare)
        extra = doc2.get("_extra_entries", {})
        assert "future_data.bin" in extra, f"Unknown entry not preserved; extras: {list(extra)}"
        assert isinstance(extra["future_data.bin"], str), "extra entry should be a base64 str now"
        assert base64.b64decode(extra["future_data.bin"]) == unknown_entry, "Unknown entry content changed"

        # Resave with unknown entry still present (base64 str round-trips back through save's decode)
        ecad_path2 = os.path.join(td, "test_sch_resave.ecad+")
        formats.save(doc2, ecad_path2)
        doc3, _ = formats.open(ecad_path2)
        extra3 = doc3.get("_extra_entries", {})
        assert "future_data.bin" in extra3, "Unknown entry lost after resave"
        assert base64.b64decode(extra3["future_data.bin"]) == unknown_entry, "Unknown entry corrupted on resave"

    print("  test_ecad_plus_roundtrip PASS")


# ---------------------------------------------------------------------------
# test 2: .ecad+ board round-trip
# ---------------------------------------------------------------------------

def test_ecad_plus_board_roundtrip() -> None:
    from engine.ecad import formats, schema

    with tempfile.TemporaryDirectory() as td:
        ecad_path = os.path.join(td, "test_board.ecad+")

        doc = schema.new_board(name="RoundTripBoard")
        doc["footprints"].append({
            "id": "fp1",
            "fpid": "Resistor_SMD:R_0402",
            "ref": "R1",
            "value": "10k",
            "pos": [10.0, 10.0],
            "angle": 0.0,
            "side": "top",
            "pads": [
                {"id": "p1", "number": "1", "pos": [-0.5, 0.0], "angle": 0.0,
                 "shape": "rect", "size": [0.5, 0.6], "roundrect_ratio": 0.0,
                 "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"],
                 "drill": None, "net": "GND", "pin": "1"},
            ],
            "graphics": [],
            "model_3d": None,
            "attr": "smd",
            "linked_symbol": None,
        })

        errs = schema.validate_board(doc)
        assert not errs, f"Original board doc invalid: {errs}"

        out_path = formats.save(doc, ecad_path)
        doc2, warnings = formats.open(ecad_path)

        errs2 = schema.validate_board(doc2)
        assert not errs2, f"Reopened board invalid: {errs2}"

        fps2 = doc2.get("footprints", [])
        assert len(fps2) == 1, f"Expected 1 footprint, got {len(fps2)}"
        assert fps2[0]["ref"] == "R1"

    print("  test_ecad_plus_board_roundtrip PASS")


# ---------------------------------------------------------------------------
# test 3: import_kicad_sch
# ---------------------------------------------------------------------------

def test_import_kicad_sch() -> None:
    from engine.ecad import formats, schema

    doc = formats.import_kicad_sch(_MINIMAL_SCH)

    # Basic schema check
    assert doc.get("schema") == "aicad.sch/1", f"Wrong schema: {doc.get('schema')}"
    errs = schema.validate_schematic(doc)
    assert not errs, f"Imported sch invalid: {errs}"

    # Symbol count: 2 placed (R1, C1) — power symbols go to doc["power"] so excluded
    syms = doc.get("symbols", [])
    assert len(syms) == 2, f"Expected 2 symbols, got {len(syms)}: {[s.get('ref') for s in syms]}"
    refs = {s.get("ref") for s in syms}
    assert "R1" in refs, f"R1 not found in symbols: {refs}"
    assert "C1" in refs, f"C1 not found in symbols: {refs}"

    # lib_cache populated
    cache = doc.get("lib_cache", {})
    assert len(cache) >= 2, f"Expected >=2 lib_cache entries, got {len(cache)}: {list(cache)}"
    assert "Device:R" in cache, f"Device:R not in lib_cache: {list(cache)}"
    assert "Device:C" in cache, f"Device:C not in lib_cache: {list(cache)}"

    # Pins present in lib_cache
    r_def = cache["Device:R"]
    assert len(r_def.get("pins", [])) >= 2, f"Expected >=2 pins on Device:R, got {r_def.get('pins')}"

    # Wire present
    wires = doc.get("wires", [])
    assert len(wires) >= 1, f"Expected >=1 wire, got {len(wires)}"

    # Local label "CLK"
    labels = doc.get("labels", [])
    clk_labels = [l for l in labels if l.get("text") == "CLK"]
    assert clk_labels, f"Label 'CLK' not found: {[l.get('text') for l in labels]}"

    # Global label "VCC"
    vcc_labels = [l for l in labels if l.get("text") == "VCC" and l.get("scope") == "global"]
    assert vcc_labels, f"Global label 'VCC' not found: {[(l.get('text'), l.get('scope')) for l in labels]}"

    # Junction present
    junctions = doc.get("junctions", [])
    assert len(junctions) >= 1, f"Expected >=1 junction, got {len(junctions)}"

    # no_connect present
    ncs = doc.get("no_connect", [])
    assert len(ncs) >= 1, f"Expected >=1 no_connect, got {len(ncs)}"

    # (Bonus) build_netlist runs on it
    try:
        from engine.ecad import netlist as nl_mod
        nl = nl_mod.build_netlist(doc)
        assert isinstance(nl, dict), "build_netlist returned non-dict"
        print(f"    (netlist built: {len(nl.get('nets', {}))} nets)")
    except ImportError:
        print("    (netlist module not available — skipping netlist bonus check)")

    print("  test_import_kicad_sch PASS")


# ---------------------------------------------------------------------------
# test 4: import_kicad_pcb
# ---------------------------------------------------------------------------

def test_import_kicad_pcb() -> None:
    from engine.ecad import formats, schema

    doc = formats.import_kicad_pcb(_MINIMAL_PCB)

    # Basic schema check
    assert doc.get("schema") == "aicad.pcb/1", f"Wrong schema: {doc.get('schema')}"
    errs = schema.validate_board(doc)
    assert not errs, f"Imported pcb invalid: {errs}"

    # Footprints: 2
    fps = doc.get("footprints", [])
    assert len(fps) == 2, f"Expected 2 footprints, got {len(fps)}"
    refs = {fp.get("ref") for fp in fps}
    assert "R1" in refs, f"R1 not found: {refs}"
    assert "R2" in refs, f"R2 not found: {refs}"

    # Each footprint has 2 pads
    for fp in fps:
        pads = fp.get("pads", [])
        assert len(pads) == 2, f"{fp['ref']} has {len(pads)} pads, expected 2"

    # Nets: at least GND, VCC, NET1
    net_names = {n.get("name") for n in doc.get("nets", [])}
    assert "GND" in net_names, f"GND not in nets: {net_names}"
    assert "VCC" in net_names, f"VCC not in nets: {net_names}"
    assert "NET1" in net_names, f"NET1 not in nets: {net_names}"

    # Track: at least 1
    tracks = doc.get("tracks", [])
    assert len(tracks) >= 1, f"Expected >=1 track, got {len(tracks)}"

    # Outline from gr_line Edge.Cuts: 4 lines
    outline = doc.get("outline", {})
    shapes = outline.get("shapes", [])
    assert len(shapes) >= 4, f"Expected >=4 outline shapes (4 Edge.Cuts gr_lines), got {len(shapes)}"

    print(f"  test_import_kicad_pcb PASS  "
          f"(fps={len(fps)}, tracks={len(tracks)}, nets={len(net_names)}, "
          f"outline_shapes={len(shapes)})")


# ---------------------------------------------------------------------------
# test 5: import_kicad_sch from temp file
# ---------------------------------------------------------------------------

def test_import_kicad_sch_from_file() -> None:
    from engine.ecad import formats, schema

    with tempfile.TemporaryDirectory() as td:
        sch_path = os.path.join(td, "test.kicad_sch")
        pathlib.Path(sch_path).write_text(_MINIMAL_SCH, encoding="utf-8")

        doc = formats.import_kicad_sch(sch_path)
        errs = schema.validate_schematic(doc)
        assert not errs, f"File-based sch import invalid: {errs}"
        assert doc.get("name") == "test", f"Expected name='test', got {doc.get('name')!r}"

    print("  test_import_kicad_sch_from_file PASS")


# ---------------------------------------------------------------------------
# test 6: formats.open dispatch by extension
# ---------------------------------------------------------------------------

def test_open_dispatch() -> None:
    from engine.ecad import formats, schema

    with tempfile.TemporaryDirectory() as td:
        # .kicad_sch dispatch
        sch_path = os.path.join(td, "t.kicad_sch")
        pathlib.Path(sch_path).write_text(_MINIMAL_SCH, encoding="utf-8")
        doc, warnings = formats.open(sch_path)
        assert doc.get("schema") == "aicad.sch/1"
        assert isinstance(warnings, list)

        # .kicad_pcb dispatch
        pcb_path = os.path.join(td, "t.kicad_pcb")
        pathlib.Path(pcb_path).write_text(_MINIMAL_PCB, encoding="utf-8")
        doc2, warnings2 = formats.open(pcb_path)
        assert doc2.get("schema") == "aicad.pcb/1"
        assert isinstance(warnings2, list)

        # .ecad+ dispatch
        ecad_path = os.path.join(td, "t.ecad+")
        sch_doc = schema.new_schematic(name="DispatchTest")
        sch_doc["lib_cache"]["Device:R"] = {
            "libid": "Device:R", "name": "R", "description": "", "keywords": "",
            "reference_prefix": "R", "value": "R", "footprint_filters": [],
            "pins": [], "graphics": [], "units_count": 1, "power": False, "fields": {},
        }
        formats.save(sch_doc, ecad_path)
        doc3, warnings3 = formats.open(ecad_path)
        assert doc3.get("schema") == "aicad.sch/1"

    print("  test_open_dispatch PASS")


# ---------------------------------------------------------------------------
# test 7: rpc auto-wire (formats.save / formats.open names match rpc.py expectation)
# ---------------------------------------------------------------------------

def test_rpc_autowire() -> None:
    """Verify that formats exposes the exact names rpc.py imports: formats.save and formats.open."""
    from engine.ecad import formats

    assert callable(formats.save), "formats.save must be callable"
    assert callable(formats.open), "formats.open must be callable"  # noqa: A001

    # Also check the KiCad importers are present
    assert callable(formats.import_kicad_sch), "formats.import_kicad_sch must be callable"
    assert callable(formats.import_kicad_pcb), "formats.import_kicad_pcb must be callable"

    print("  test_rpc_autowire PASS (formats.save + formats.open present)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print("engine.ecad.test_formats")
    tests = [
        test_rpc_autowire,
        test_ecad_plus_roundtrip,
        test_ecad_plus_board_roundtrip,
        test_import_kicad_sch,
        test_import_kicad_sch_from_file,
        test_import_kicad_pcb,
        test_open_dispatch,
    ]
    failed: list[str] = []
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  FAIL {t.__name__}: {exc}")
            traceback.print_exc()
            failed.append(t.__name__)

    print()
    if failed:
        print(f"FAILED: {failed}")
        sys.exit(1)
    else:
        print(f"All {len(tests)} tests PASSED")


if __name__ == "__main__":
    main()
