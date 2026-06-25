"""Fabrication output writer tests.

Tests per contract §1-4 (fab_outputs.md). Run with:
    python -m engine.ecad.test_fab
"""
from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import os

from engine.ecad.schema import new_board
from engine.ecad.fab import write_gerbers, write_gerber_layer, write_drill, write_bom, write_placement


# ── Board fixture ─────────────────────────────────────────────────────────────

def _make_test_board() -> dict:
    """Build a small board with 3 SMD footprints, a THT, tracks, a via, and an outline."""
    board = new_board("TestBoard")

    # Nets
    board["nets"] = [
        {"name": "GND", "class": "default"},
        {"name": "VCC", "class": "default"},
        {"name": "SIG", "class": "default"},
    ]

    # 3 SMD footprints (R1, R2, C1) + 1 THT (J1) + 1 virtual (TP1, excluded from CPL/BOM)
    board["footprints"] = [
        {
            "id": "fp1",
            "fpid": "Resistor_SMD:R_0603_1608Metric",
            "ref": "R1",
            "value": "10k",
            "pos": [10.0, 10.0],
            "angle": 0.0,
            "side": "top",
            "attr": "smd",
            "pads": [
                {
                    "id": "p1",
                    "number": "1",
                    "pos": [-0.8, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [1.0, 1.2],
                    "type": "smd",
                    "layers": ["F.Cu", "F.Mask", "F.Paste"],
                    "drill": None,
                    "net": "GND",
                    "pin": "1",
                },
                {
                    "id": "p2",
                    "number": "2",
                    "pos": [0.8, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [1.0, 1.2],
                    "type": "smd",
                    "layers": ["F.Cu", "F.Mask", "F.Paste"],
                    "drill": None,
                    "net": "VCC",
                    "pin": "2",
                },
            ],
            "graphics": [],
            "model_3d": None,
            "linked_symbol": None,
        },
        {
            "id": "fp2",
            "fpid": "Resistor_SMD:R_0603_1608Metric",
            "ref": "R2",
            "value": "10k",
            "pos": [15.0, 10.0],
            "angle": 45.0,
            "side": "top",
            "attr": "smd",
            "pads": [
                {
                    "id": "p3",
                    "number": "1",
                    "pos": [-0.8, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [1.0, 1.2],
                    "type": "smd",
                    "layers": ["F.Cu", "F.Mask", "F.Paste"],
                    "drill": None,
                    "net": "SIG",
                    "pin": "1",
                },
                {
                    "id": "p4",
                    "number": "2",
                    "pos": [0.8, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [1.0, 1.2],
                    "type": "smd",
                    "layers": ["F.Cu", "F.Mask", "F.Paste"],
                    "drill": None,
                    "net": "GND",
                    "pin": "2",
                },
            ],
            "graphics": [],
            "model_3d": None,
            "linked_symbol": None,
        },
        {
            "id": "fp3",
            "fpid": "Capacitor_SMD:C_0402_1005Metric",
            "ref": "C1",
            "value": "100nF",
            "pos": [20.0, 10.0],
            "angle": 0.0,
            "side": "top",
            "attr": "smd",
            "pads": [
                {
                    "id": "p5",
                    "number": "1",
                    "pos": [-0.5, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [0.8, 0.9],
                    "type": "smd",
                    "layers": ["F.Cu", "F.Mask", "F.Paste"],
                    "drill": None,
                    "net": "GND",
                    "pin": "1",
                },
                {
                    "id": "p6",
                    "number": "2",
                    "pos": [0.5, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [0.8, 0.9],
                    "type": "smd",
                    "layers": ["F.Cu", "F.Mask", "F.Paste"],
                    "drill": None,
                    "net": "VCC",
                    "pin": "2",
                },
            ],
            "graphics": [],
            "model_3d": None,
            "linked_symbol": None,
        },
        {
            # THT connector — should appear in drill but NOT in CPL (excluded_tht=True)
            "id": "fp4",
            "fpid": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            "ref": "J1",
            "value": "Conn_01x02",
            "pos": [25.0, 10.0],
            "angle": 0.0,
            "side": "top",
            "attr": "tht",
            "pads": [
                {
                    "id": "p7",
                    "number": "1",
                    "pos": [0.0, 0.0],
                    "angle": 0.0,
                    "shape": "circle",
                    "size": [1.7, 1.7],
                    "type": "tht",
                    "layers": ["F.Cu", "B.Cu", "F.Mask", "B.Mask"],
                    "drill": {"diameter": 1.0, "shape": "round", "offset": [0, 0]},
                    "net": "VCC",
                    "pin": "1",
                },
                {
                    "id": "p8",
                    "number": "2",
                    "pos": [0.0, 2.54],
                    "angle": 0.0,
                    "shape": "circle",
                    "size": [1.7, 1.7],
                    "type": "tht",
                    "layers": ["F.Cu", "B.Cu", "F.Mask", "B.Mask"],
                    "drill": {"diameter": 1.0, "shape": "round", "offset": [0, 0]},
                    "net": "GND",
                    "pin": "2",
                },
            ],
            "graphics": [],
            "model_3d": None,
            "linked_symbol": None,
        },
        {
            # Virtual testpoint — excluded from BOM (dnp) and CPL
            "id": "fp5",
            "fpid": "TestPoint:TestPoint_THTPad_D1.5mm_Drill0.7mm",
            "ref": "TP1",
            "value": "TestPoint",
            "pos": [30.0, 10.0],
            "angle": 0.0,
            "side": "top",
            "attr": "virtual",
            "dnp": True,
            "pads": [
                {
                    "id": "p9",
                    "number": "1",
                    "pos": [0.0, 0.0],
                    "angle": 0.0,
                    "shape": "circle",
                    "size": [1.5, 1.5],
                    "type": "tht",
                    "layers": ["F.Cu", "B.Cu"],
                    "drill": {"diameter": 0.7, "shape": "round", "offset": [0, 0]},
                    "net": "GND",
                    "pin": "1",
                },
            ],
            "graphics": [],
            "model_3d": None,
            "linked_symbol": None,
        },
        {
            # Bottom-side SMD (R10, larger number to test natural sort)
            "id": "fp6",
            "fpid": "Resistor_SMD:R_0402_1005Metric",
            "ref": "R10",
            "value": "4.7k",
            "pos": [10.0, 20.0],
            "angle": 90.0,
            "side": "bottom",
            "attr": "smd",
            "pads": [
                {
                    "id": "p10",
                    "number": "1",
                    "pos": [-0.5, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [0.8, 0.9],
                    "type": "smd",
                    "layers": ["B.Cu", "B.Mask", "B.Paste"],
                    "drill": None,
                    "net": "SIG",
                    "pin": "1",
                },
                {
                    "id": "p11",
                    "number": "2",
                    "pos": [0.5, 0.0],
                    "angle": 0.0,
                    "shape": "rect",
                    "size": [0.8, 0.9],
                    "type": "smd",
                    "layers": ["B.Cu", "B.Mask", "B.Paste"],
                    "drill": None,
                    "net": "GND",
                    "pin": "2",
                },
            ],
            "graphics": [],
            "model_3d": None,
            "linked_symbol": None,
        },
    ]

    # Tracks
    board["tracks"] = [
        {
            "id": "t1",
            "net": "GND",
            "layer": "F.Cu",
            "width": 0.25,
            "kind": "segment",
            "p1": [9.2, 10.0],
            "p2": [11.0, 10.0],
        },
        {
            "id": "t2",
            "net": "VCC",
            "layer": "F.Cu",
            "width": 0.25,
            "kind": "segment",
            "p1": [10.8, 10.0],
            "p2": [14.2, 10.0],
        },
    ]

    # Via
    board["vias"] = [
        {
            "id": "v1",
            "net": "GND",
            "at": [12.0, 10.0],
            "drill": 0.4,
            "diameter": 0.8,
            "layers": ["F.Cu", "B.Cu"],
            "type": "through",
        }
    ]

    # Zones (with filled_polys for Gerber region test)
    board["zones"] = [
        {
            "id": "z1",
            "net": "GND",
            "layer": "F.Cu",
            "polygon": [[5.0, 5.0], [35.0, 5.0], [35.0, 25.0], [5.0, 25.0]],
            "clearance": 0.3,
            "min_width": 0.25,
            "fill": "solid",
            "thermal": {"gap": 0.3, "spoke_width": 0.3},
            "filled_polys": [
                [[5.5, 5.5], [34.5, 5.5], [34.5, 24.5], [5.5, 24.5]],
            ],
        }
    ]

    # Outline (simple rectangle 40x30 mm)
    board["outline"] = {
        "shapes": [
            {"type": "line", "p1": [0.0, 0.0], "p2": [40.0, 0.0]},
            {"type": "line", "p1": [40.0, 0.0], "p2": [40.0, 30.0]},
            {"type": "line", "p1": [40.0, 30.0], "p2": [0.0, 30.0]},
            {"type": "line", "p1": [0.0, 30.0], "p2": [0.0, 0.0]},
        ],
        "closed": True,
    }

    return board


# ── Gerber lexer helpers ──────────────────────────────────────────────────────

def _parse_gerber(content: str) -> dict:
    """Lex Gerber content and return analysis dict."""
    lines = content.splitlines()

    aperture_defs: dict[int, int] = {}  # d_code -> line_number of %ADD
    macro_defs: set[str] = set()        # AM names defined
    first_use: dict[int, int] = {}      # d_code -> line_number of first D01/D02/D03
    flash_count = 0
    region_open = 0
    region_close = 0
    has_fs = False
    has_mo = False
    has_m02 = False

    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue

        # Format statement
        if line.startswith("%FSLAX"):
            has_fs = True
        if line.startswith("%MOMM"):
            has_mo = True

        # Aperture macro definition
        am_m = re.match(r"%AM(\w+)\*", line)
        if am_m:
            macro_defs.add(am_m.group(1))

        # Aperture add
        add_m = re.match(r"%ADD(\d+)", line)
        if add_m:
            d = int(add_m.group(1))
            aperture_defs[d] = lineno

        # Tool select
        select_m = re.match(r"^D(\d+)\*$", line)
        if select_m:
            d = int(select_m.group(1))
            if d >= 10 and d not in first_use:
                first_use[d] = lineno

        # D03 flash
        if re.search(r"D03\*", line):
            flash_count += 1
            # record tool use
            # current_d is tracked by the D<n>* select statements above
            # For the purposes of "aperture def precedes first use" we need to
            # map each D03 line to a current aperture — we'll do a stateful scan.

        # G36/G37
        if re.search(r"G36\*", line):
            region_open += 1
        if re.search(r"G37\*", line):
            region_close += 1

        # M02
        if line == "M02*":
            has_m02 = True

    return {
        "aperture_defs": aperture_defs,
        "macro_defs": macro_defs,
        "first_use": first_use,
        "flash_count": flash_count,
        "region_open": region_open,
        "region_close": region_close,
        "has_fs": has_fs,
        "has_mo": has_mo,
        "has_m02": has_m02,
        "lines": lines,
    }


def _count_flashes_stateful(content: str) -> int:
    """Count D03 flashes using stateful D-code tracking."""
    count = 0
    for line in content.splitlines():
        line = line.strip()
        if re.search(r"D03\*", line):
            count += 1
    return count


def _apertures_precede_use(content: str) -> tuple[bool, str]:
    """Check that every aperture is defined before its first use in the file."""
    # Track: map D-code -> line where first used (D01/D02/D03 with that tool active)
    defined_at: dict[int, int] = {}
    first_used_at: dict[int, int] = {}
    current_d: int | None = None

    for lineno, line in enumerate(content.splitlines(), 1):
        line = line.strip()

        # Aperture definition
        add_m = re.match(r"%ADD(\d+)", line)
        if add_m:
            d = int(add_m.group(1))
            defined_at[d] = lineno

        # Tool select
        sel_m = re.match(r"^D(\d+)\*$", line)
        if sel_m:
            d = int(sel_m.group(1))
            if d >= 10:
                current_d = d
                if current_d not in first_used_at:
                    first_used_at[current_d] = lineno

        # D01/D02/D03 operations use the current aperture
        if current_d is not None and re.search(r"D0[123]\*", line):
            if current_d not in first_used_at:
                first_used_at[current_d] = lineno

    for d, used_line in first_used_at.items():
        def_line = defined_at.get(d)
        if def_line is None:
            return False, f"D{d} used at line {used_line} but never defined"
        if def_line > used_line:
            return False, f"D{d} defined at line {def_line} but first used at line {used_line}"
    return True, "ok"


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_gerber(board: dict) -> None:
    print("--- Gerber tests ---")

    with tempfile.TemporaryDirectory() as tmp:
        paths = write_gerbers(board, tmp)

    # Must have produced files for the copper layers at minimum
    assert "F.Cu" in paths, "No F.Cu layer produced"
    assert "B.Cu" in paths, "No B.Cu layer produced"

    # Test F.Cu layer in detail
    fcu_content = write_gerber_layer(board, "F.Cu")

    lex = _parse_gerber(fcu_content)

    # FS and MO headers present
    assert lex["has_fs"], "Missing %FSLAX46Y46*% header"
    assert lex["has_mo"], "Missing %MOMM*% header"

    # Ends with M02*
    assert lex["has_m02"], "Missing M02* terminator"

    # G36/G37 balanced
    assert lex["region_open"] == lex["region_close"], (
        f"Unbalanced G36/G37: {lex['region_open']} opens vs {lex['region_close']} closes"
    )

    # At least one G36 region (we have a zone with filled_polys)
    assert lex["region_open"] >= 1, "Expected at least 1 G36 region for zone fill"

    # Aperture defs precede first use
    ok, msg = _apertures_precede_use(fcu_content)
    assert ok, f"Aperture ordering violation: {msg}"

    # Flash count == pad count on F.Cu + via count (via is on both F.Cu and B.Cu)
    fcu_pads = sum(
        1 for fp in board["footprints"]
        for pad in fp.get("pads", [])
        if "F.Cu" in pad.get("layers", [])
    )
    fcu_vias = sum(
        1 for via in board.get("vias", [])
        if "F.Cu" in via.get("layers", [])
    )
    expected_flashes = fcu_pads + fcu_vias
    actual_flashes = _count_flashes_stateful(fcu_content)
    assert actual_flashes == expected_flashes, (
        f"F.Cu flash count: expected {expected_flashes} (pads={fcu_pads} + vias={fcu_vias}), "
        f"got {actual_flashes}"
    )

    # B.Cu should have the via flash and bottom pads
    bcu_content = write_gerber_layer(board, "B.Cu")
    bcu_pads = sum(
        1 for fp in board["footprints"]
        for pad in fp.get("pads", [])
        if "B.Cu" in pad.get("layers", [])
    )
    bcu_vias = sum(
        1 for via in board.get("vias", [])
        if "B.Cu" in via.get("layers", [])
    )
    bcu_expected = bcu_pads + bcu_vias
    bcu_actual = _count_flashes_stateful(bcu_content)
    assert bcu_actual == bcu_expected, (
        f"B.Cu flash count: expected {bcu_expected}, got {bcu_actual}"
    )

    # Edge.Cuts should have no flashes but should have strokes
    edge_content = write_gerber_layer(board, "Edge.Cuts")
    edge_lex = _parse_gerber(edge_content)
    assert edge_lex["has_m02"], "Edge.Cuts missing M02*"
    assert _count_flashes_stateful(edge_content) == 0, "Edge.Cuts should have no flashes"

    print("  PASS: FS/MO headers present")
    print("  PASS: M02* terminator")
    print("  PASS: G36/G37 balanced")
    print("  PASS: aperture defs precede first use")
    print(f"  PASS: F.Cu flash count == {expected_flashes} (pads + vias)")
    print(f"  PASS: B.Cu flash count == {bcu_expected}")
    print("  PASS: Edge.Cuts has no flashes")


def test_drill(board: dict) -> None:
    print("--- Drill tests ---")

    # Collect expected values from board (outside tempdir)
    expected_pth_sizes: set[float] = set()
    expected_pth_count = 0

    for fp in board["footprints"]:
        for pad in fp.get("pads", []):
            ptype = pad.get("type", "smd")
            if ptype == "tht":
                d = round(float(pad["drill"]["diameter"]), 6)
                expected_pth_sizes.add(d)
                expected_pth_count += 1
            # np_tht goes to npth file

    for via in board.get("vias", []):
        d = round(float(via["drill"]), 6)
        expected_pth_sizes.add(d)
        expected_pth_count += 1

    with tempfile.TemporaryDirectory() as tmp:
        paths = write_drill(board, tmp)

        assert "pth" in paths, "Missing PTH drill file"
        assert "npth" in paths, "Missing NPTH drill file"

        with open(paths["pth"], encoding="utf-8") as f:
            pth_content = f.read()

        # Check tool diameter lines appear for each distinct size
        for dia in expected_pth_sizes:
            pattern = f"C{dia:.3f}"
            assert pattern in pth_content, (
                f"PTH drill missing tool definition for diameter {dia:.3f} (expected '{pattern}')"
            )

        # Check M48 header and M30 footer
        assert "M48" in pth_content, "PTH drill missing M48 header"
        assert "M30" in pth_content, "PTH drill missing M30 footer"
        assert "METRIC,TZ" in pth_content, "PTH drill missing METRIC,TZ"

        # Count coordinate hits (lines starting with X...Y... pattern, not G85 slots)
        hit_lines = [
            ln.strip() for ln in pth_content.splitlines()
            if re.match(r"^X-?\d+Y-?\d+$", ln.strip())
        ]
        # Count G85 slots
        slot_lines = [
            ln.strip() for ln in pth_content.splitlines()
            if ln.strip().startswith("G85")
        ]
        actual_pth_hits = len(hit_lines) + len(slot_lines)
        assert actual_pth_hits == expected_pth_count, (
            f"PTH hit count: expected {expected_pth_count}, got {actual_pth_hits} "
            f"(hits={len(hit_lines)}, slots={len(slot_lines)})\nContent:\n{pth_content}"
        )

    print(f"  PASS: PTH distinct diameters: {sorted(expected_pth_sizes)}")
    print(f"  PASS: PTH hit count == {expected_pth_count}")
    print("  PASS: M48 header, METRIC,TZ, M30 present")


def test_bom(board: dict) -> None:
    print("--- BOM tests ---")

    csv_out = write_bom(board, fmt="csv")
    json_out = write_bom(board, fmt="json")

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_out))
    rows = list(reader)

    # Expected non-DNP footprints:
    # R1 (10k, 0603), R2 (10k, 0603) -> grouped together
    # C1 (100nF, 0402) -> own row
    # J1 (Conn_01x02, PinHeader) -> own row
    # R10 (4.7k, 0402) -> own row
    # TP1 is virtual/dnp -> EXCLUDED
    # Total rows = 4

    assert len(rows) == 4, f"Expected 4 BOM rows, got {len(rows)}: {[r['Refs'] for r in rows]}"

    # R1+R2 should be grouped
    r_row = next((r for r in rows if "R1" in r["Refs"] and "R2" in r["Refs"]), None)
    assert r_row is not None, "R1 and R2 should be grouped in same row"
    assert int(r_row["Qty"]) == 2, f"R1+R2 row qty should be 2, got {r_row['Qty']}"
    assert r_row["Value"] == "10k", f"R1+R2 value should be '10k', got {r_row['Value']}"

    # Refs within row should be naturally sorted (R1 before R2)
    refs_in_row = r_row["Refs"].split(",")
    assert refs_in_row == sorted(refs_in_row, key=lambda r: tuple(
        int(p) if p.isdigit() else p for p in re.split(r"(\d+)", r)
    )), f"Refs not naturally sorted: {refs_in_row}"

    # TP1 should NOT appear anywhere
    all_refs = ",".join(r["Refs"] for r in rows)
    assert "TP1" not in all_refs, f"TP1 (virtual/dnp) should be excluded from BOM"

    # C1 row
    c1_row = next((r for r in rows if "C1" in r["Refs"]), None)
    assert c1_row is not None, "C1 missing from BOM"
    assert int(c1_row["Qty"]) == 1

    # JSON format produces same data
    json_rows = json.loads(json_out)
    assert len(json_rows) == 4, f"JSON BOM should have 4 rows, got {len(json_rows)}"

    # Natural ref sort across groups: C1 < J1 < R1 < R10 (alphabetic+numeric)
    first_refs = [r["Refs"].split(",")[0] for r in rows]
    print(f"  BOM row order (first ref): {first_refs}")

    print(f"  PASS: {len(rows)} BOM rows (TP1/virtual excluded)")
    print("  PASS: R1+R2 grouped, qty=2")
    print("  PASS: natural ref sort within groups")
    print("  PASS: JSON format produces same row count")


def test_placement(board: dict) -> None:
    print("--- Placement tests ---")

    cpl = write_placement(board)
    reader = csv.DictReader(io.StringIO(cpl))
    rows = list(reader)

    # Expected: R1, R2, C1 (top SMD), R10 (bottom SMD)
    # J1 (THT) excluded, TP1 (virtual) excluded
    designators = {r["Designator"] for r in rows}
    assert "R1" in designators, "R1 missing from placement"
    assert "R2" in designators, "R2 missing from placement"
    assert "C1" in designators, "C1 missing from placement"
    assert "R10" in designators, "R10 missing from placement"
    assert "J1" not in designators, "J1 (THT) should be excluded from placement"
    assert "TP1" not in designators, "TP1 (virtual) should be excluded from placement"
    assert len(rows) == 4, f"Expected 4 placement rows, got {len(rows)}: {sorted(designators)}"

    # R1: pos [10,10], angle 0, top
    r1 = next(r for r in rows if r["Designator"] == "R1")
    assert r1["Layer"] == "Top", f"R1 layer should be Top, got {r1['Layer']}"
    assert abs(float(r1["MidX(mm)"]) - 10.0) < 0.001, f"R1 MidX wrong: {r1['MidX(mm)']}"
    assert abs(float(r1["MidY(mm)"]) - 10.0) < 0.001, f"R1 MidY wrong: {r1['MidY(mm)']}"
    assert abs(float(r1["Rotation"]) - 0.0) < 0.001, f"R1 rotation should be 0"

    # R2: angle 45, top
    r2 = next(r for r in rows if r["Designator"] == "R2")
    assert r2["Layer"] == "Top"
    assert abs(float(r2["Rotation"]) - 45.0) < 0.001, f"R2 rotation should be 45, got {r2['Rotation']}"

    # R10: bottom, angle 90 → mirrored rotation = -90 % 360 = 270
    r10 = next(r for r in rows if r["Designator"] == "R10")
    assert r10["Layer"] == "Bottom", f"R10 should be Bottom"
    expected_r10_rot = (-90.0) % 360  # = 270
    assert abs(float(r10["Rotation"]) - expected_r10_rot) < 0.001, (
        f"R10 rotation should be {expected_r10_rot}, got {r10['Rotation']}"
    )

    # Natural sort order: C1, R1, R2, R10
    expected_order = ["C1", "R1", "R2", "R10"]
    actual_order = [r["Designator"] for r in rows]
    assert actual_order == expected_order, (
        f"Placement natural sort order wrong: {actual_order} != {expected_order}"
    )

    print("  PASS: R1/R2/C1/R10 present; J1/TP1 excluded")
    print("  PASS: R1 pos/layer/rotation correct")
    print("  PASS: R2 rotation=45")
    print(f"  PASS: R10 bottom, rotation={expected_r10_rot}")
    print("  PASS: natural sort order C1, R1, R2, R10")


# ── Test M5: rows count excludes CSV header ──────────────────────────────────

def test_m5_bom_rows_excludes_header() -> None:
    """M5: write_bom CSV text.count('\\n') includes the header line; the MCP tool subtracts 1.
    Verify the raw CSV has exactly (data_rows + 1) newlines so the fix is correct."""
    board = _make_test_board()
    csv_text = write_bom(board, fmt="csv")
    reader = csv.DictReader(io.StringIO(csv_text))
    data_rows = list(reader)
    n_data = len(data_rows)  # 4 for the test board (TP1/virtual excluded)
    n_newlines = csv_text.count("\n")
    # The CSV must have exactly one more newline than data rows (the header line).
    assert n_newlines == n_data + 1, (
        f"BOM CSV: {n_data} data rows but {n_newlines} newlines — "
        f"expected {n_data + 1} (header + data); "
        f"MCP rows fix (subtract 1) would report {n_newlines - 1} = {n_data} ✓")
    # Verify the MCP subtraction yields the correct data-only count
    mcp_rows = max(0, n_newlines - 1)
    assert mcp_rows == n_data, f"MCP rows={mcp_rows} should equal data rows={n_data}"
    print(f"  PASS: BOM CSV {n_newlines} newlines, {n_data} data rows; MCP rows={mcp_rows}")


def test_m5_placement_rows_excludes_header() -> None:
    """M5: same header-line fix applies to placement CSV."""
    board = _make_test_board()
    from engine.ecad.fab.placement import write_placement as _wp
    cpl_text = _wp(board)
    reader = csv.DictReader(io.StringIO(cpl_text))
    data_rows = list(reader)
    n_data = len(data_rows)
    n_newlines = cpl_text.count("\n")
    assert n_newlines == n_data + 1, (
        f"Placement CSV: {n_data} data rows but {n_newlines} newlines; expected {n_data + 1}")
    mcp_rows = max(0, n_newlines - 1)
    assert mcp_rows == n_data
    print(f"  PASS: Placement CSV {n_newlines} newlines, {n_data} data rows; MCP rows={mcp_rows}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== Fabrication output tests ===\n")
    board = _make_test_board()
    test_gerber(board)
    print()
    test_drill(board)
    print()
    test_bom(board)
    print()
    test_placement(board)
    print()
    test_m5_bom_rows_excludes_header()
    print()
    test_m5_placement_rows_excludes_header()
    print("\n=== ALL PASS ===")


if __name__ == "__main__":
    main()
