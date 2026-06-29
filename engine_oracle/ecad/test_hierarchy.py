"""Hierarchical multi-sheet schematic tests — sheet symbols / sheet pins / hier-label bridging.

House style (mirrors engine.ecad.test_netlist): ``from __future__ import annotations``, plain
``main()`` + asserts, run as a module. Fixtures are self-contained (a tiny inline ``lib_cache``).
Coordinates are mm on the 2.54 grid.

Run:  & "<venv>/python.exe" -m engine.ecad.test_hierarchy

Covers (per the task spec):
  1. A 2-sheet design: a parent sheet symbol (child=sh2) with sheet pin "CLK" wired to a component
     pin on the parent, and a child hier label "CLK" wired to a component pin on sh2, put BOTH pins
     on ONE net named "CLK" that SPANS the two sheets.
  2. A `global` label still merges across sheets (regression); a `local` label does NOT merge across
     sheets (regression).
  3. Determinism: byte-stable netlist across two runs.
  4. ERC: a sheet pin with no matching child hier label fires `hier_pin_no_label`; a child hier
     label with no exposing sheet pin fires `hier_label_no_pin`.
  5. Backward-compat: a doc with no sheet_symbols/hier content netlists BYTE-IDENTICALLY to a doc
     built without the hierarchy machinery (sanity that the bridge is purely additive).
  6. ops: sch_add_sheet / sch_add_sheet_symbol / sch_add_sheet_pin build a valid hierarchy and the
     resulting netlist bridges the two sheets.
"""

from __future__ import annotations

import json

from engine.ecad import schema
from engine.ecad.netlist import build_netlist
from engine.ecad.erc import run_erc
from engine.ecad.ops import apply_ops


# ── tiny symbol library (R), pins length 0 so world tip == pos for easy hand-placement ──

def _pin(number: str, name: str, pos, angle: float, ptype: str) -> dict:
    return {"number": number, "name": name, "pos": list(pos), "angle": angle,
            "length": 0.0, "type": ptype, "unit": 1}


def _lib_cache() -> dict:
    return {
        "Device:R": {
            "libid": "Device:R", "name": "R", "description": "Resistor",
            "reference_prefix": "R", "value": "R", "power": False,
            "pins": [_pin("1", "~", [0.0, 2.54], 90.0, "passive"),
                     _pin("2", "~", [0.0, -2.54], -90.0, "passive")],
        },
    }


def _sym(sid: str, ref: str, pos, sheet: str = "sh1", libid: str = "Device:R") -> dict:
    return {"id": sid, "libid": libid, "sheet": sheet, "ref": ref, "value": "1k",
            "footprint": "FP", "pos": list(pos), "angle": 0.0, "mirror": "none",
            "unit": 1, "fields": {}, "in_bom": True, "on_board": True, "dnp": False}


def _wire(wid: str, pts, sheet: str = "sh1") -> dict:
    return {"id": wid, "sheet": sheet, "pts": [list(p) for p in pts], "bus": False}


def _label(lid: str, pos, text: str, scope: str, sheet: str = "sh1") -> dict:
    return {"id": lid, "sheet": sheet, "pos": list(pos), "text": text, "scope": scope, "angle": 0.0}


def _two_sheet_doc() -> dict:
    """Two sheets: sh1 (parent, root) and sh2 (child)."""
    d = schema.new_schematic("hier-test")
    d["lib_cache"] = _lib_cache()
    d["sheets"] = [
        {"id": "sh1", "name": "root", "parent": None, "size": [297, 210], "ports": []},
        {"id": "sh2", "name": "child", "parent": "sh1", "size": [297, 210], "ports": []},
    ]
    return d


def _net_of(nl: dict, pinref: str) -> str:
    return nl["by_pin"].get(pinref, "<missing>")


def _rules(viols) -> set[str]:
    return {v["rule"] for v in viols}


# ── Test 1: cross-sheet bridge via sheet pin <-> child hier label, same name "CLK" ──

def test_cross_sheet_bridge():
    doc = _two_sheet_doc()
    # PARENT (sh1): R1 with pin1 tip at (0,0); a wire (0,0)->(10,0); the sheet pin "CLK" at (10,0).
    rP = _sym("s1", "R1", (0.0, -2.54), sheet="sh1")     # pin1 tip (0,0)
    wP = _wire("w1", [(0.0, 0.0), (10.0, 0.0)], sheet="sh1")
    # CHILD (sh2): R2 with pin1 tip at (0,0); a wire (0,0)->(20,0); a hier label "CLK" at (20,0).
    rC = _sym("s2", "R2", (0.0, -2.54), sheet="sh2")     # pin1 tip (0,0) on sh2
    wC = _wire("w2", [(0.0, 0.0), (20.0, 0.0)], sheet="sh2")
    lblC = _label("l1", (20.0, 0.0), "CLK", "hier", sheet="sh2")
    # the sheet symbol on the PARENT instantiating child sh2, exposing sheet pin "CLK" at (10,0).
    hs = {"id": "hs1", "sheet": "sh1", "child": "sh2", "pos": [10.0, 0.0],
          "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]}
    doc["symbols"] = [rP, rC]
    doc["wires"] = [wP, wC]
    doc["labels"] = [lblC]
    doc["sheet_symbols"] = [hs]

    errs = schema.validate_schematic(doc)
    assert errs == [], f"fixture invalid: {errs}"

    nl = build_netlist(doc)
    nA = _net_of(nl, "s1.1")   # parent component pin
    nB = _net_of(nl, "s2.1")   # child component pin
    assert nA == nB, f"sheet pin must bridge to child hier label: {nA!r} vs {nB!r}"
    assert nA == "CLK", f"hierarchy-spanning net must be named CLK, got {nA!r}"
    assert "CLK" in nl["nets"], nl["nets"].keys()
    members = {(m["sym"], m["pin"]) for m in nl["nets"]["CLK"]}
    assert members == {("s1", "1"), ("s2", "1")}, f"CLK net membership wrong: {members}"

    # the net SPANS sheets: its two pins live on symbols on distinct sheets.
    sheet_of = {s["id"]: s["sheet"] for s in doc["symbols"]}
    sheets_touched = {sheet_of[sym] for sym, _pin in members}
    assert sheets_touched == {"sh1", "sh2"}, f"net must span both sheets, touched {sheets_touched}"
    print("  test_cross_sheet_bridge OK  (CLK spans %s)" % sorted(sheets_touched))


# ── Test 2a: a global label still merges across sheets (regression) ──────────────────

def test_global_label_spans_sheets():
    doc = _two_sheet_doc()
    rP = _sym("s1", "R1", (0.0, -2.54), sheet="sh1")     # pin1 tip (0,0) on sh1
    rC = _sym("s2", "R2", (100.0, -2.54), sheet="sh2")   # pin1 tip (100,0) on sh2
    wP = _wire("w1", [(0.0, 0.0), (5.0, 0.0)], sheet="sh1")
    wC = _wire("w2", [(100.0, 0.0), (105.0, 0.0)], sheet="sh2")
    gP = _label("l1", (5.0, 0.0), "VBUS", "global", sheet="sh1")
    gC = _label("l2", (105.0, 0.0), "VBUS", "global", sheet="sh2")
    doc["symbols"] = [rP, rC]
    doc["wires"] = [wP, wC]
    doc["labels"] = [gP, gC]
    assert schema.validate_schematic(doc) == []
    nl = build_netlist(doc)
    assert _net_of(nl, "s1.1") == _net_of(nl, "s2.1") == "VBUS", \
        f"global label must merge across sheets, got {_net_of(nl, 's1.1')} / {_net_of(nl, 's2.1')}"
    print("  test_global_label_spans_sheets OK")


# ── Test 2b: a local label does NOT merge across sheets (regression) ─────────────────

def test_local_label_does_not_span_sheets():
    # A local label is per-sheet: the same text on two sheets must form two ELECTRICALLY DISTINCT
    # union classes (unlike a global label, which merges, and unlike a hier-bridged sheet pin).
    # Caveat (pre-existing builder behavior, unchanged here): local labels are merged per-sheet but
    # NAMED by text without a sheet namespace, so two distinct same-text local nets collide on the
    # resolved name and the builder emits the multiple-nets-label warning. We therefore assert the
    # ELECTRICAL distinctness (two classes + the collision warning), not the resolved name.
    doc = _two_sheet_doc()
    rP = _sym("s1", "R1", (0.0, -2.54), sheet="sh1")
    rC = _sym("s2", "R2", (100.0, -2.54), sheet="sh2")
    wP = _wire("w1", [(0.0, 0.0), (5.0, 0.0)], sheet="sh1")
    wC = _wire("w2", [(100.0, 0.0), (105.0, 0.0)], sheet="sh2")
    lP = _label("l1", (5.0, 0.0), "DATA", "local", sheet="sh1")
    lC = _label("l2", (105.0, 0.0), "DATA", "local", sheet="sh2")
    doc["symbols"] = [rP, rC]
    doc["wires"] = [wP, wC]
    doc["labels"] = [lP, lC]
    assert schema.validate_schematic(doc) == []
    nl = build_netlist(doc)
    # The two pins are NOT in the same union class: they sit at distinct points and the builder
    # reports the same-name-but-distinct-class collision (proof they did not merge across sheets).
    data_members = {(m["sym"], m["pin"]) for m in nl["nets"].get("DATA", [])}
    pts = {tuple(m["xy"]) for m in nl["nets"].get("DATA", [])}
    assert data_members == {("s1", "1"), ("s2", "1")} and len(pts) == 2, \
        f"local label members should sit on two distinct points, got {pts}"
    assert any("resolves to 2 distinct nets" in w for w in nl["warnings"]), \
        f"per-sheet local labels must stay 2 distinct classes, got warnings {nl['warnings']}"

    # Contrast: a GLOBAL label of the same geometry WOULD merge to one class (one point set).
    gdoc = _two_sheet_doc()
    gdoc["symbols"] = [rP, rC]
    gdoc["wires"] = [wP, wC]
    gdoc["labels"] = [_label("l1", (5.0, 0.0), "DATA", "global", sheet="sh1"),
                      _label("l2", (105.0, 0.0), "DATA", "global", sheet="sh2")]
    gnl = build_netlist(gdoc)
    assert _net_of(gnl, "s1.1") == _net_of(gnl, "s2.1") == "DATA", \
        "control: a global label of the same geometry must merge across sheets"
    print("  test_local_label_does_not_span_sheets OK  (2 distinct local classes; global merges)")


# ── Test 3: determinism / byte stability of a hierarchical netlist ──────────────────

def test_hierarchy_byte_stable():
    doc = _two_sheet_doc()
    rP = _sym("s1", "R1", (0.0, -2.54), sheet="sh1")
    rC = _sym("s2", "R2", (0.0, -2.54), sheet="sh2")
    wP = _wire("w1", [(0.0, 0.0), (10.0, 0.0)], sheet="sh1")
    wC = _wire("w2", [(0.0, 0.0), (20.0, 0.0)], sheet="sh2")
    lblC = _label("l1", (20.0, 0.0), "CLK", "hier", sheet="sh2")
    hs = {"id": "hs1", "sheet": "sh1", "child": "sh2", "pos": [10.0, 0.0],
          "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]}
    doc["symbols"] = [rP, rC]
    doc["wires"] = [wP, wC]
    doc["labels"] = [lblC]
    doc["sheet_symbols"] = [hs]
    a = json.dumps(build_netlist(doc), sort_keys=True)
    b = json.dumps(build_netlist(doc), sort_keys=True)
    assert a == b, "hierarchical netlist is not byte-stable across runs"
    # also stable WITHOUT sort_keys (builder emits sorted dicts already)
    a2 = json.dumps(build_netlist(doc))
    b2 = json.dumps(build_netlist(doc))
    assert a2 == b2, "hierarchical netlist insertion order is not deterministic"
    # purity: build did not mutate the doc
    assert "warnings" not in doc, "build_netlist mutated the input doc"
    print("  test_hierarchy_byte_stable OK")


# ── Test 4: ERC sheet-pin / hier-label mismatch warnings ────────────────────────────

def test_erc_hier_mismatch():
    doc = _two_sheet_doc()
    rC = _sym("s2", "R2", (0.0, -2.54), sheet="sh2")
    wC = _wire("w2", [(0.0, 0.0), (20.0, 0.0)], sheet="sh2")
    # child has a hier label "RST" but the sheet symbol exposes a pin named "CLK" -> BOTH mismatch.
    lblC = _label("l1", (20.0, 0.0), "RST", "hier", sheet="sh2")
    hs = {"id": "hs1", "sheet": "sh1", "child": "sh2", "pos": [10.0, 0.0],
          "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]}
    doc["symbols"] = [rC]
    doc["wires"] = [wC]
    doc["labels"] = [lblC]
    doc["sheet_symbols"] = [hs]
    assert schema.validate_schematic(doc) == []
    nl = build_netlist(doc)
    viols = run_erc(doc, nl)
    rules = _rules(viols)
    assert "hier_pin_no_label" in rules, "sheet pin CLK with no matching child hier label must warn"
    assert "hier_label_no_pin" in rules, "child hier label RST with no exposing sheet pin must warn"
    by_rule = {}
    for v in viols:
        by_rule.setdefault(v["rule"], []).append(v)
    assert by_rule["hier_pin_no_label"][0]["severity"] == "warning"
    assert by_rule["hier_label_no_pin"][0]["severity"] == "warning"

    # ERC determinism + purity
    again = run_erc(doc, build_netlist(doc))
    assert json.dumps(viols, sort_keys=True) == json.dumps(again, sort_keys=True), \
        "hierarchy ERC is not deterministic"

    # And: a correctly-matched bridge fires NEITHER warning.
    doc_ok = _two_sheet_doc()
    doc_ok["symbols"] = [rC]
    doc_ok["wires"] = [wC]
    doc_ok["labels"] = [_label("l1", (20.0, 0.0), "CLK", "hier", sheet="sh2")]
    doc_ok["sheet_symbols"] = [hs]
    ok_rules = _rules(run_erc(doc_ok, build_netlist(doc_ok)))
    assert "hier_pin_no_label" not in ok_rules and "hier_label_no_pin" not in ok_rules, \
        "a matched sheet-pin/hier-label pair must not warn"
    print("  test_erc_hier_mismatch OK  (%d violations on mismatch)" % len(viols))


# ── Test 5: backward-compat — no sheet_symbols => netlist identical to flat build ───

def test_backward_compatible_no_hierarchy():
    # A plain single-sheet doc that never touches the hierarchy fields. Building it must be
    # byte-identical whether or not the sheet_symbols key is present (empty), proving the bridge is
    # purely additive.
    base = schema.new_schematic("flat")
    base["lib_cache"] = _lib_cache()
    base["symbols"] = [_sym("s1", "R1", (0.0, -2.54))]
    base["wires"] = [_wire("w1", [(0.0, 0.0), (10.0, 0.0)])]

    with_key = json.loads(json.dumps(base))            # has "sheet_symbols": [] (from factory)
    without_key = json.loads(json.dumps(base))
    without_key.pop("sheet_symbols", None)             # legacy doc shape (pre-hierarchy)
    assert "sheet_symbols" not in without_key

    a = json.dumps(build_netlist(with_key), sort_keys=True)
    b = json.dumps(build_netlist(without_key), sort_keys=True)
    assert a == b, "netlist must be byte-identical with/without the sheet_symbols key"
    # legacy doc still validates
    assert schema.validate_schematic(without_key) == []
    print("  test_backward_compatible_no_hierarchy OK")


# ── Test 6: ops build the hierarchy end-to-end and the netlist bridges ──────────────

def test_ops_build_hierarchy():
    doc = schema.new_schematic("ops-hier")
    doc["lib_cache"] = _lib_cache()
    batch = [
        # op 0: create child sheet sh2
        {"op": "sch_add_sheet", "name": "child", "parent": "sh1"},
        # op 1: parent component R on sh1, pin1 tip at (0,0)
        {"op": "sch_add_symbol", "libid": "Device:R", "sheet": "sh1", "pos": [0.0, -2.54]},
        # op 2: wire (0,0)->(10,0) on sh1
        {"op": "sch_add_wire", "sheet": "sh1", "pts": [[0.0, 0.0], [10.0, 0.0]]},
        # op 3: sheet symbol on sh1 instantiating $0 (sh2) with sheet pin CLK at (10,0)
        {"op": "sch_add_sheet_symbol", "sheet": "sh1", "child": "$0", "pos": [10.0, 0.0],
         "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]},
        # op 4: child component R on sh2, pin1 tip at (0,0)
        {"op": "sch_add_symbol", "libid": "Device:R", "sheet": "$0", "pos": [0.0, -2.54]},
        # op 5: wire (0,0)->(20,0) on sh2
        {"op": "sch_add_wire", "sheet": "$0", "pts": [[0.0, 0.0], [20.0, 0.0]]},
        # op 6: child hier label CLK at (20,0) on sh2
        {"op": "sch_add_label", "sheet": "$0", "pos": [20.0, 0.0], "text": "CLK", "scope": "hier"},
    ]
    res = apply_ops(doc, batch)
    new_doc = res["doc"]
    assert schema.validate(new_doc) == [], schema.validate(new_doc)
    # $0 resolved to a real child sheet id, used by the sheet symbol + the child symbol/label.
    child_id = res["id_map"]["$0"]
    assert child_id.startswith("sh"), child_id
    hs = new_doc["sheet_symbols"][0]
    assert hs["child"] == child_id and hs["pins"][0]["name"] == "CLK"

    # A follow-up op can add another sheet pin to the existing sheet symbol.
    res2 = apply_ops(new_doc, [
        {"op": "sch_add_sheet_pin", "id": hs["id"], "name": "RST", "pos": [10.0, 5.0],
         "shape": "output"}])
    doc2 = res2["doc"]
    names = {p["name"] for p in doc2["sheet_symbols"][0]["pins"]}
    assert names == {"CLK", "RST"}, names

    # netlist bridges parent R pin1 <-> child R pin1 onto net CLK
    nl = build_netlist(new_doc)
    syms = {s["ref"]: s["id"] for s in new_doc["symbols"]}
    a = nl["by_pin"][f"{syms['R1']}.1"]
    b = nl["by_pin"][f"{syms['R2']}.1"]
    assert a == b == "CLK", f"ops-built hierarchy must bridge to CLK, got {a} / {b}"
    print("  test_ops_build_hierarchy OK  (child=%s)" % child_id)


def main() -> None:
    print("=" * 60)
    print("AI-ECAD HIERARCHY (MULTI-SHEET) TESTS")
    print("=" * 60)
    test_cross_sheet_bridge()
    test_global_label_spans_sheets()
    test_local_label_does_not_span_sheets()
    test_hierarchy_byte_stable()
    test_erc_hier_mismatch()
    test_backward_compatible_no_hierarchy()
    test_ops_build_hierarchy()
    print("ALL HIERARCHY TESTS PASSED")


if __name__ == "__main__":
    main()
