"""Netlist + ERC tests — connectivity core of AI-ECAD.

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
Fixtures are self-contained — a tiny ``lib_cache`` (R / V / GND symbol defs) is built inline so the
tests are hermetic. Coordinates are mm; the standard grid is 2.54 mm (100 mil).

Run:  & "<venv>/python.exe" -m engine.ecad.test_netlist

Covers (per the task spec):
  1. Resistive voltage divider — correct nets + pin membership + BYTE-STABLE across two runs.
  2. Crossing-without-junction does NOT merge; adding a junction DOES merge.
  3. A global label on two separate wires merges their nets; net takes the label name.
  4. A power port forces/keeps its net name even when also labeled.
  5. ERC seeded faults: pin_not_connected, conflicting_drivers, duplicate_ref, power_not_driven.
"""

from __future__ import annotations

import json

from engine.ecad import schema
from engine.ecad.netlist import build_netlist, expand_bus_members
from engine.ecad.erc import run_erc, pair_severity


# ── tiny symbol library (R / V / GND), pins laid out on the 2.54 grid ──────────────
#
# Pin convention (sch_schema.md §3.1): pin.pos is the root on the body; angle points AWAY from the
# body; world tip = pos + length*dir(angle). We keep pins length 0 so the world endpoint == pos for
# easy hand-placement of test geometry.

def _pin(number: str, name: str, pos, angle: float, ptype: str) -> dict:
    return {"number": number, "name": name, "pos": list(pos), "angle": angle,
            "length": 0.0, "type": ptype, "unit": 1}


def _lib_cache() -> dict:
    return {
        # Resistor: two passive pins, pin 1 at top (+y), pin 2 at bottom (-y), body-local.
        "Device:R": {
            "libid": "Device:R", "name": "R", "description": "Resistor",
            "reference_prefix": "R", "value": "R", "power": False,
            "pins": [_pin("1", "~", [0.0, 2.54], 90.0, "passive"),
                     _pin("2", "~", [0.0, -2.54], -90.0, "passive")],
            "sim": {"model": "R", "pin_order": ["1", "2"]},
        },
        # Voltage source: pin 1 (+) power_out at top, pin 2 (-) power_out at bottom.
        "Sim:V": {
            "libid": "Sim:V", "name": "V", "description": "Voltage source",
            "reference_prefix": "V", "value": "V", "power": False,
            "pins": [_pin("1", "+", [0.0, 2.54], 90.0, "power_out"),
                     _pin("2", "-", [0.0, -2.54], -90.0, "power_out")],
            "sim": {"model": "V", "pin_order": ["1", "2"]},
        },
        # GND power symbol: a single power_in-ish pin; marked power=True so missing_footprint skips.
        "power:GND": {
            "libid": "power:GND", "name": "GND", "description": "Ground",
            "reference_prefix": "#PWR", "value": "GND", "power": True,
            "pins": [_pin("1", "GND", [0.0, 0.0], -90.0, "power_in")],
        },
        # A logic gate with an output pin, for ERC driver-conflict tests.
        "Logic:OUT": {
            "libid": "Logic:OUT", "name": "U", "description": "Output buffer",
            "reference_prefix": "U", "value": "U", "power": False,
            "pins": [_pin("1", "Y", [0.0, 0.0], 0.0, "output")],
        },
    }


def _sym(sid: str, libid: str, ref: str, pos, value: str = "",
         footprint: str = "FP", angle: float = 0.0) -> dict:
    return {"id": sid, "libid": libid, "sheet": "sh1", "ref": ref, "value": value,
            "footprint": footprint, "pos": list(pos), "angle": angle, "mirror": "none",
            "unit": 1, "fields": {}, "in_bom": True, "on_board": True, "dnp": False}


def _wire(wid: str, pts) -> dict:
    return {"id": wid, "sheet": "sh1", "pts": [list(p) for p in pts], "bus": False}


def _doc(symbols=None, wires=None, junctions=None, labels=None, power=None,
         no_connect=None) -> dict:
    d = schema.new_schematic("test")
    d["lib_cache"] = _lib_cache()
    d["symbols"] = symbols or []
    d["wires"] = wires or []
    d["junctions"] = junctions or []
    d["labels"] = labels or []
    d["power"] = power or []
    d["no_connect"] = no_connect or []
    return d


def _net_of(netlist: dict, pinref: str) -> str:
    return netlist["by_pin"].get(pinref, "<missing>")


def _rules(viols) -> set[str]:
    return {v["rule"] for v in viols}


# ── Test 1: voltage divider, correct nets + pin membership + byte stability ────────

def test_voltage_divider():
    # Layout (x=0 column). V1 at y=0 (pin1 tip y=+2.54 = VTOP, pin2 tip y=-2.54 = GND rail).
    # R1 from VTOP down to MID; R2 from MID down to GND. GND symbol at the bottom rail.
    #
    #   V1.1 (+2.54) --wire-- R1.1 (+2.54 from R1@... )  -> we place symbols so tips coincide.
    #
    # Place explicitly by symbol origin so pin world tips line up on the grid.
    # R pin1 tip = pos + (0, +2.54); pin2 tip = pos + (0, -2.54).
    # V pin1 tip = pos + (0, +2.54); pin2 tip = pos + (0, -2.54).
    #
    # Nodes:  VTOP=(0, 10),  MID=(0, 0),  GND=(0,-10)
    # V1: pos (0, 7.46) -> pin1 tip (0,10)=VTOP, pin2 tip (0,4.92) ... we instead route via wires.
    # Simplest: put coincident pin tips and connect with short wires where needed.
    #
    # Use direct coincidence (no wires) by stacking pin tips:
    #   V1 pos (0, 12.54): pin1 tip (0,15.08), pin2 tip (0,10.0)=VTOP
    #   R1 pos (0, 7.46):  pin1 tip (0,10.0)=VTOP, pin2 tip (0,4.92)=MID
    #   R2 pos (0, 2.38):  pin1 tip (0,4.92)=MID,  pin2 tip (0,-0.16)=GND
    #   GND pos (0,-0.16): pin1 tip (0,-0.16)=GND
    #   V1.1 (top +) is the supply top; close the loop V1.1 -> wire -> VTOP so it isn't floating.
    v1 = _sym("s1", "Sim:V", "V1", (0.0, 12.54), value="5")
    r1 = _sym("s2", "Device:R", "R1", (0.0, 7.46), value="10k")
    r2 = _sym("s3", "Device:R", "R2", (0.0, 2.38), value="10k")
    gnd = _sym("s4", "power:GND", "#PWR01", (0.0, -0.16), footprint="")
    # connect V1.1 (0,15.08) down to GND (0,-0.16) forms supply loop top; and a wire from V1.1 to
    # the GND symbol would short — instead wire V1.1 to VTOP so the source spans VTOP..GND.
    # We want: V across [VTOP=(0,10)] and [GND=(0,-0.16)]. V1.2 tip is already (0,10)=VTOP.
    # V1.1 (0,15.08) is the other terminal -> wire it to GND node so the source drives the divider.
    w1 = _wire("w1", [(0.0, 15.08), (0.0, -0.16)])  # V1.1 -> GND rail (the return)
    doc = _doc(symbols=[v1, r1, r2, gnd], wires=[w1])

    errs = schema.validate_schematic(doc)
    assert errs == [], f"fixture invalid: {errs}"

    nl = build_netlist(doc)

    # Expected nodes by pin:
    #   VTOP: V1.2, R1.1
    #   MID:  R1.2, R2.1
    #   GND:  R2.2, GND.1, V1.1 (via wire w1)
    assert _net_of(nl, "s1.2") == _net_of(nl, "s2.1"), "V1.2 and R1.1 should share VTOP"
    assert _net_of(nl, "s2.2") == _net_of(nl, "s3.1"), "R1.2 and R2.1 should share MID"
    assert _net_of(nl, "s3.2") == _net_of(nl, "s4.1"), "R2.2 and GND should share"
    assert _net_of(nl, "s1.1") == _net_of(nl, "s4.1"), "V1.1 wired to GND rail"

    # VTOP and MID and GND must be three DISTINCT nets.
    distinct = {_net_of(nl, "s1.2"), _net_of(nl, "s2.2"), _net_of(nl, "s4.1")}
    assert len(distinct) == 3, f"expected 3 distinct nets, got {distinct}"

    # net membership counts
    vtop = _net_of(nl, "s1.2")
    mid = _net_of(nl, "s2.2")
    gnet = _net_of(nl, "s4.1")
    assert {(m["sym"], m["pin"]) for m in nl["nets"][vtop]} == {("s1", "2"), ("s2", "1")}
    assert {(m["sym"], m["pin"]) for m in nl["nets"][mid]} == {("s2", "2"), ("s3", "1")}
    assert {(m["sym"], m["pin"]) for m in nl["nets"][gnet]} == {
        ("s1", "1"), ("s3", "2"), ("s4", "1")}, "GND net membership wrong"

    # auto names are N$<n>
    for n in (vtop, mid, gnet):
        assert n.startswith("N$"), f"unlabeled net should be auto-named, got {n}"

    # components present and pins mapped
    refs = {c["ref"] for c in nl["components"]}
    assert refs == {"V1", "R1", "R2", "#PWR01"}, refs

    # BYTE STABILITY: build twice, json.dumps must be identical.
    a = json.dumps(build_netlist(doc), sort_keys=True)
    b = json.dumps(build_netlist(doc), sort_keys=True)
    assert a == b, "netlist is not byte-stable across runs"
    # also stable without sort_keys (our builder emits sorted dicts already)
    a2 = json.dumps(build_netlist(doc))
    b2 = json.dumps(build_netlist(doc))
    assert a2 == b2, "netlist insertion order is not deterministic"

    # purity: build did not mutate the doc
    assert "warnings" not in doc, "build_netlist mutated the input doc"
    print("  test_voltage_divider OK  (VTOP=%s MID=%s GND=%s)" % (vtop, mid, gnet))


# ── Test 2: crossing without junction does NOT merge; junction DOES merge ─────────

def test_crossing_and_junction():
    # Two wires crossing at the origin. Horizontal w1 (-5,0)->(5,0); vertical w2 (0,-5)->(0,5).
    # They share the *interior* point (0,0) but neither has a vertex there -> NO connection.
    w1 = _wire("w1", [(-5.0, 0.0), (5.0, 0.0)])
    w2 = _wire("w2", [(0.0, -5.0), (0.0, 5.0)])
    # Put a passive pin on each far end so each wire becomes its own net.
    rA = _sym("s1", "Device:R", "R1", (-5.0, -2.54))   # pin1 tip (-5, 0) on w1 left end
    rB = _sym("s2", "Device:R", "R2", (0.0, -7.54))    # pin1 tip (0, -5) on w2 bottom end
    doc = _doc(symbols=[rA, rB], wires=[w1, w2])
    nl = build_netlist(doc)
    # R1.1 sits on w1, R2.1 sits on w2; crossing must NOT merge them.
    assert _net_of(nl, "s1.1") != _net_of(nl, "s2.1"), \
        "crossing without a junction must NOT connect the two wires"

    # Now add a junction at the crossing -> the two wires (which both pass through (0,0)) merge.
    j = {"id": "j1", "sheet": "sh1", "pos": [0.0, 0.0]}
    doc2 = _doc(symbols=[rA, rB], wires=[w1, w2], junctions=[j])
    nl2 = build_netlist(doc2)
    assert _net_of(nl2, "s1.1") == _net_of(nl2, "s2.1"), \
        "a junction at the crossing MUST connect the two wires"
    print("  test_crossing_and_junction OK")


# ── Test 3: global label merges two separate wires; net takes the label name ───────

def test_global_label_merge():
    # Two geometrically disjoint wires, each with a pin. A global label "CLK" placed on each end.
    w1 = _wire("w1", [(0.0, 0.0), (10.0, 0.0)])
    w2 = _wire("w2", [(50.0, 0.0), (60.0, 0.0)])
    rA = _sym("s1", "Device:R", "R1", (0.0, -2.54))    # pin1 tip (0,0) on w1
    rB = _sym("s2", "Device:R", "R2", (60.0, -2.54))   # pin1 tip (60,0) on w2
    lblA = {"id": "l1", "sheet": "sh1", "pos": [10.0, 0.0], "text": "CLK", "scope": "global"}
    lblB = {"id": "l2", "sheet": "sh1", "pos": [50.0, 0.0], "text": "CLK", "scope": "global"}
    doc = _doc(symbols=[rA, rB], wires=[w1, w2], labels=[lblA, lblB])
    nl = build_netlist(doc)
    assert _net_of(nl, "s1.1") == _net_of(nl, "s2.1"), "global label CLK must merge both wires"
    assert _net_of(nl, "s1.1") == "CLK", f"merged net must take label name CLK, got {_net_of(nl, 's1.1')}"
    assert "CLK" in nl["nets"] and len(nl["nets"]["CLK"]) == 2
    print("  test_global_label_merge OK")


# ── Test 4: power port forces/keeps its net name even when also labeled ────────────

def test_power_port_priority():
    # One wire with a pin, a power port net="VCC" on it, AND a local label "MY_RAIL" on it.
    # Power port net name must win over the label.
    w1 = _wire("w1", [(0.0, 0.0), (10.0, 0.0)])
    rA = _sym("s1", "Device:R", "R1", (0.0, -2.54))    # pin1 tip (0,0)
    pw = {"id": "pw1", "sheet": "sh1", "pos": [5.0, 0.0], "net": "VCC", "symbol": "VCC"}
    lbl = {"id": "l1", "sheet": "sh1", "pos": [10.0, 0.0], "text": "MY_RAIL", "scope": "local"}
    doc = _doc(symbols=[rA], wires=[w1], power=[pw], labels=[lbl])
    nl = build_netlist(doc)
    assert _net_of(nl, "s1.1") == "VCC", \
        f"power port must override label, expected VCC got {_net_of(nl, 's1.1')}"
    assert "MY_RAIL" not in nl["nets"], "label name must not appear; power port wins"
    print("  test_power_port_priority OK")


# ── Test 5: ERC seeded faults ──────────────────────────────────────────────────────

def test_erc_seeded_faults():
    # (a) pin_not_connected: a lone resistor pin with nothing on it.
    floater = _sym("s1", "Device:R", "R1", (0.0, 0.0))  # both pins float (no wire/pin coincident)
    # (b) conflicting_drivers: two outputs tied together.
    u1 = _sym("s2", "Logic:OUT", "U1", (20.0, 0.0))     # pin1 tip (20,0) output
    u2 = _sym("s3", "Logic:OUT", "U2", (20.0, 0.0))     # pin1 tip (20,0) output -> same node
    # (c) duplicate_ref: two symbols share ref "Rdup".
    dupA = _sym("s4", "Device:R", "Rdup", (40.0, 0.0))
    dupB = _sym("s5", "Device:R", "Rdup", (50.0, 0.0))
    # (d) power_not_driven: a GND-style power_in pin on a net with no power_out / power port.
    pin_only = _sym("s6", "power:GND", "#PWR02", (60.0, 0.0), footprint="")
    # tie a second power_in pin to it so the net has size 2 (so it isn't merely "not connected").
    pin_only2 = _sym("s7", "power:GND", "#PWR03", (60.0, 0.0), footprint="")

    doc = _doc(symbols=[floater, u1, u2, dupA, dupB, pin_only, pin_only2])
    nl = build_netlist(doc)
    viols = run_erc(doc, nl)
    rules = _rules(viols)

    assert "pin_not_connected" in rules, "expected a floating-pin warning"
    assert "conflicting_drivers" in rules, "expected output-output short error"
    assert "duplicate_ref" in rules, "expected duplicate-ref error"
    assert "power_not_driven" in rules, "expected power_in-with-no-source error"

    # spot-check severities + content
    by_rule = {}
    for v in viols:
        by_rule.setdefault(v["rule"], []).append(v)
    assert by_rule["conflicting_drivers"][0]["severity"] == "error"
    assert by_rule["duplicate_ref"][0]["severity"] == "error"
    assert by_rule["power_not_driven"][0]["severity"] == "error"
    assert by_rule["pin_not_connected"][0]["severity"] == "warning"
    # the conflicting net carries both outputs
    cd = by_rule["conflicting_drivers"][0]
    assert set(cd["pins"]) == {"s2.1", "s3.1"}, cd["pins"]

    # ERC is advisory + pure: did not mutate doc/netlist and is deterministic.
    again = run_erc(doc, build_netlist(doc))
    assert json.dumps(viols, sort_keys=True) == json.dumps(again, sort_keys=True), \
        "ERC output is not deterministic"

    # no_connect suppresses pin_not_connected
    nc = {"id": "nc1", "sheet": "sh1", "pos": [0.0, 2.54]}  # covers R1 pin1 tip (0,2.54)
    doc_nc = _doc(symbols=[floater], no_connect=[nc])
    nl_nc = build_netlist(doc_nc)
    viols_nc = run_erc(doc_nc, nl_nc)
    nc_pinrefs = {tuple(v["pins"]) for v in viols_nc if v["rule"] == "pin_not_connected"}
    assert ("s1.1",) not in nc_pinrefs, "no_connect should suppress pin_not_connected on that pin"
    print("  test_erc_seeded_faults OK  (%d violations)" % len(viols))


# ── Test: pin-type matrix is symmetric and unspecified/free never raise ────────────

def test_pin_matrix_symmetric():
    for a in schema.PIN_TYPES:
        for b in schema.PIN_TYPES:
            assert pair_severity(a, b) == pair_severity(b, a), f"matrix asymmetric at {a},{b}"
            if a in ("unspecified", "free") or b in ("unspecified", "free"):
                assert pair_severity(a, b) is None, f"{a}/{b} must never raise"
    assert pair_severity("output", "output") == "error"
    assert pair_severity("power_out", "power_in") is None
    print("  test_pin_matrix_symmetric OK")


# ── Test: bus expansion (v1 parse + expand; routing is a follow-up) ────────────────

def test_bus_expansion():
    assert expand_bus_members("D[0..3]") == ["D0", "D1", "D2", "D3"]
    assert expand_bus_members("A[3..0]") == ["A3", "A2", "A1", "A0"]
    assert expand_bus_members(["RX", "TX"]) == ["RX", "TX"]
    assert expand_bus_members("SINGLE") == ["SINGLE"]
    # a doc with a bus emits a parse-only follow-up warning
    bus = {"id": "bus1", "sheet": "sh1", "pts": [[0, 0], [10, 0]], "members": "D[0..1]"}
    doc = _doc()
    doc["buses"] = [bus]
    nl = build_netlist(doc)
    assert any("bus routing is a v1 follow-up" in w for w in nl["warnings"]), nl["warnings"]
    print("  test_bus_expansion OK")


def main() -> None:
    print("=" * 60)
    print("AI-ECAD NETLIST + ERC TESTS")
    print("=" * 60)
    test_voltage_divider()
    test_crossing_and_junction()
    test_global_label_merge()
    test_power_port_priority()
    test_erc_seeded_faults()
    test_pin_matrix_symmetric()
    test_bus_expansion()
    print("ALL NETLIST + ERC TESTS PASSED")


if __name__ == "__main__":
    main()
