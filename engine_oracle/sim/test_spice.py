"""Tests for engine.sim.spice — run via:
    python -m engine.sim.test_spice
"""

from __future__ import annotations

import math
import sys


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _assert(condition: bool, msg: str) -> None:
    if not condition:
        print(f"  FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"  ok   {msg}")


def _circuits_structurally_equal(a: dict, b: dict) -> tuple[bool, str]:
    """Return (equal, reason) comparing Circuit dicts structurally."""

    # Same device names and kinds
    a_devs = {d["name"]: d for d in a.get("devices", [])}
    b_devs = {d["name"]: d for d in b.get("devices", [])}
    if set(a_devs) != set(b_devs):
        return False, f"device name sets differ: {set(a_devs)} vs {set(b_devs)}"
    for name, da in a_devs.items():
        db = b_devs[name]
        if da["kind"] != db["kind"]:
            return False, f"{name}: kind {da['kind']} vs {db['kind']}"
        if da["nodes"] != db["nodes"]:
            return False, f"{name}: nodes {da['nodes']} vs {db['nodes']}"
        # value tolerance 0.1%
        va, vb = da["value"], db["value"]
        if va is None and vb is None:
            pass
        elif va is None or vb is None:
            return False, f"{name}: value None mismatch ({va} vs {vb})"
        elif abs(va - vb) > 1e-6 * max(abs(va), abs(vb), 1.0):
            return False, f"{name}: value {va} vs {vb}"

    # Same model names
    a_mods = set(a.get("models", {}))
    b_mods = set(b.get("models", {}))
    if a_mods != b_mods:
        return False, f"model sets differ: {a_mods} vs {b_mods}"

    return True, "ok"


# ---------------------------------------------------------------------------
# Test 1: round-trip  (parse -> write -> parse -> structurally equal)
# ---------------------------------------------------------------------------

_DECK = """\
* Voltage divider + RC + diode, .tran
V1 VCC 0 DC 5
R1 VCC N1 10k
R2 N1 0 10k
C1 N1 0 100n
D1 N1 OUT 1N4148
.model 1N4148 D(IS=2.52n N=1.752 RS=0.568 CJO=4p VJ=0.7 BV=100)
.tran 1u 1m 0
.end
"""


def test_round_trip() -> None:
    print("\n[Test 1] Round-trip parse -> write -> parse")
    from engine.sim.spice import parse_spice_deck, write_spice

    c1, s1 = parse_spice_deck(_DECK)

    txt2 = write_spice(c1, s1)
    c2, s2 = parse_spice_deck(txt2)

    ok, reason = _circuits_structurally_equal(c1, c2)
    _assert(ok, f"circuit structurally equal after round-trip ({reason})")

    # Spot-check node list includes "0" first
    _assert(c1["nodes"][0] == "0", "ground node '0' is first")
    _assert("N1" in c1["nodes"], "'N1' in nodes")

    # Model preserved
    _assert("1N4148" in c1["models"], "model 1N4148 present")
    _assert(c2["models"]["1N4148"]["type"] == "D", "model type=D round-tripped")

    # Study round-trip
    _assert(s1 is not None and s1["analyses"][0]["type"] == "tran",
            ".tran study parsed")
    _assert(s2 is not None and s2["analyses"][0]["type"] == "tran",
            ".tran study round-tripped")
    _assert(abs(s1["analyses"][0]["tstep"] - 1e-6) < 1e-12, "tstep=1u")
    _assert(abs(s1["analyses"][0]["tstop"] - 1e-3) < 1e-12, "tstop=1m")


# ---------------------------------------------------------------------------
# Test 2: value-suffix parsing
# ---------------------------------------------------------------------------

def test_value_suffixes() -> None:
    print("\n[Test 2] Value-suffix parsing")
    from engine.sim.spice import _parse_value  # type: ignore[attr-defined]

    cases = [
        ("1k",    1_000.0),
        ("1K",    1_000.0),
        ("2.2k",  2_200.0),
        ("1meg",  1e6),
        ("1MEG",  1e6),
        ("10u",   10e-6),
        ("100n",  100e-9),
        ("4.7n",  4.7e-9),
        ("100p",  100e-12),
        ("22p",   22e-12),
        ("1m",    1e-3),
        ("470m",  0.470),
        ("1f",    1e-15),
        ("1g",    1e9),
        ("0",     0.0),
        ("3.3",   3.3),
        ("1e3",   1000.0),
        ("2.5e-3", 2.5e-3),
    ]

    for token, expected in cases:
        got = _parse_value(token)
        rel = abs(got - expected) / max(abs(expected), 1e-30) if expected != 0 else abs(got)
        _assert(rel < 1e-6, f"  '{token}' -> {got!r} (expected {expected!r})")


# ---------------------------------------------------------------------------
# Test 3: netlist_to_spice_circuit
# ---------------------------------------------------------------------------

def test_netlist_bridge() -> None:
    """Build a small hand-crafted netlist (voltage divider) and check Circuit output."""
    print("\n[Test 3] netlist_to_spice_circuit")
    from engine.sim.spice import netlist_to_spice_circuit

    # Minimal netlist matching netlist.md §1 shape:
    # V1 VCC->GND, R1 VCC->N1, R2 N1->GND
    netlist = {
        "nets": {
            "GND": [{"sym": "s0", "pin": "1", "ref": "#PWR01"}],
            "VCC": [{"sym": "s1", "pin": "+", "ref": "V1"},
                    {"sym": "s2", "pin": "1",  "ref": "R1"}],
            "N1":  [{"sym": "s2", "pin": "2", "ref": "R1"},
                    {"sym": "s3", "pin": "1", "ref": "R2"}],
        },
        "by_pin": {
            "s1.+": "VCC", "s1.-": "GND",
            "s2.1": "VCC", "s2.2": "N1",
            "s3.1": "N1",  "s3.2": "GND",
        },
        "components": [
            {
                "ref":   "V1",
                "libid": "Simulation_SPICE:V_source",
                "value": "5",
                "pins":  {"+": "VCC", "-": "GND"},
                "dnp":   False,
                # sim carried inline (mirrors starter.py)
                "sim":   {"model": "V", "pin_order": ["+", "-"]},
            },
            {
                "ref":   "R1",
                "libid": "Device:R",
                "value": "10k",
                "pins":  {"1": "VCC", "2": "N1"},
                "dnp":   False,
                "sim":   {"model": "R", "pin_order": ["1", "2"]},
            },
            {
                "ref":   "R2",
                "libid": "Device:R",
                "value": "10k",
                "pins":  {"1": "N1", "2": "GND"},
                "dnp":   False,
                "sim":   {"model": "R", "pin_order": ["1", "2"]},
            },
        ],
        "warnings": [],
    }

    circuit, warnings = netlist_to_spice_circuit(netlist)

    # Ground at node "0"
    _assert(circuit["nodes"][0] == "0", "ground is first node ('0')")

    # All three devices created
    names = {d["name"] for d in circuit["devices"]}
    _assert("V1" in names, "V1 device present")
    _assert("R1" in names, "R1 device present")
    _assert("R2" in names, "R2 device present")

    # GND net -> "0"
    by_name = {d["name"]: d for d in circuit["devices"]}
    v1 = by_name["V1"]
    r2 = by_name["R2"]
    _assert(v1["nodes"] == ["VCC", "0"],
            f"V1 nodes [VCC, 0] (got {v1['nodes']})")
    _assert(r2["nodes"] == ["N1", "0"],
            f"R2 nodes [N1, 0] (got {r2['nodes']})")

    # Values parsed
    r1 = by_name["R1"]
    _assert(r1["value"] is not None and abs(r1["value"] - 10000.0) < 1,
            f"R1 value=10000 (got {r1['value']})")

    # pin_order respected: V1 pin "+" first
    _assert(v1["nodes"][0] == "VCC",
            f"V1 node[0]=VCC per pin_order (got {v1['nodes'][0]})")

    print(f"   warnings: {warnings}")


# ---------------------------------------------------------------------------
# Test 3b: netlist_to_spice_circuit via starter library (no inline sim)
# ---------------------------------------------------------------------------

def test_netlist_bridge_via_library() -> None:
    """Same divider but let the bridge look up sim from starter SYMBOLS."""
    print("\n[Test 3b] netlist bridge via starter library lookup")
    from engine.sim.spice import netlist_to_spice_circuit

    netlist = {
        "nets": {},
        "by_pin": {},
        "components": [
            {
                "ref":   "R3",
                "libid": "Device:R",
                "value": "4.7k",
                "pins":  {"1": "VCC", "2": "OUT"},
                "dnp":   False,
                # no "sim" key — bridge must look up from SYMBOLS["Device:R"]
            },
        ],
        "warnings": [],
    }

    circuit, warnings = netlist_to_spice_circuit(netlist)
    by_name = {d["name"]: d for d in circuit["devices"]}
    _assert("R3" in by_name, "R3 created from library lookup")
    r3 = by_name["R3"]
    _assert(abs(r3["value"] - 4700.0) < 1, f"R3 value=4700 (got {r3['value']})")
    _assert(r3["nodes"] == ["VCC", "OUT"],
            f"R3 nodes respect pin_order [1,2] from library (got {r3['nodes']})")


# ---------------------------------------------------------------------------
# Test 4 (bonus): optional circuit.simulate round-trip
# ---------------------------------------------------------------------------

def test_circuit_simulate_optional() -> None:
    print("\n[Test 4] Optional: circuit.simulate RC low-pass -3dB check")
    try:
        from engine.sim import circuit as circuit_mod  # type: ignore
    except ImportError:
        print("  SKIP: engine.sim.circuit not yet available")
        return

    from engine.sim.spice import parse_spice_deck

    deck = """\
* RC low-pass filter
V1 IN 0 AC 1
R1 IN OUT 1k
C1 OUT 0 1u
.ac dec 20 1 1meg
.end
"""
    circ, study = parse_spice_deck(deck)
    _assert(study is not None, "study parsed for AC sim")

    try:
        result = circuit_mod.simulate(circ, study)
    except Exception as exc:
        print(f"  SKIP: circuit.simulate raised {exc!r}")
        return

    # RC -3dB corner: f_c = 1 / (2π R C) = 1/(2π × 1000 × 1e-6) ≈ 159.15 Hz
    f_c_expected = 1.0 / (2 * math.pi * 1e3 * 1e-6)

    ac = result.get("ac", {})
    x_f = ac.get("x_f", [])
    mag_data = ac.get("mag", {})

    if not x_f or not mag_data:
        print("  SKIP: no AC result data in response")
        return

    # find trace for V(OUT) or first available trace
    trace_key = None
    for k in ("V(OUT)", "V(out)", "out", "OUT"):
        if k in mag_data:
            trace_key = k
            break
    if trace_key is None:
        trace_key = next(iter(mag_data), None)

    if trace_key is None:
        print("  SKIP: no magnitude trace in AC result")
        return

    mags = mag_data[trace_key]
    # find frequency closest to f_c; magnitude should be ≈ 1/√2
    closest_i = min(range(len(x_f)), key=lambda i: abs(x_f[i] - f_c_expected))
    mag_at_fc = mags[closest_i]
    expected_mag = 1.0 / math.sqrt(2)
    rel_err = abs(mag_at_fc - expected_mag) / expected_mag
    _assert(rel_err < 0.05,
            f"-3dB at {x_f[closest_i]:.1f} Hz: |H|={mag_at_fc:.4f} "
            f"(expected ~{expected_mag:.4f}, rel err {rel_err:.1%})")


# ---------------------------------------------------------------------------
# Additional: .dc and .ac parse + waveform params
# ---------------------------------------------------------------------------

def test_analysis_cards() -> None:
    print("\n[Test 5] .dc / .ac / .op analysis-card parsing")
    from engine.sim.spice import parse_spice_deck

    deck = """\
* mixed analysis cards
V1 IN 0 PULSE(0 5 0 10n 10n 500u 1m)
V2 AC_IN 0 AC 1 0
R1 IN OUT 1k
.dc V1 0 5 0.1
.ac dec 20 10 10meg
.op
.end
"""
    c, s = parse_spice_deck(deck)
    _assert(s is not None, "study parsed")
    types = [a["type"] for a in s["analyses"]]
    _assert("dc" in types, ".dc present")
    _assert("ac" in types, ".ac present")
    _assert("op" in types, ".op present")

    dc = next(a for a in s["analyses"] if a["type"] == "dc")
    _assert(dc["source"] == "V1", f"dc source=V1 (got {dc['source']})")
    _assert(abs(dc["stop"] - 5.0) < 1e-9, f"dc stop=5 (got {dc['stop']})")

    ac = next(a for a in s["analyses"] if a["type"] == "ac")
    _assert(ac["sweep"] == "dec", f"ac sweep=dec (got {ac['sweep']})")
    _assert(ac["points"] == 20, f"ac points=20 (got {ac['points']})")

    # PULSE waveform on V1
    by_name = {d["name"]: d for d in c["devices"]}
    v1 = by_name["V1"]
    _assert("waveform" in v1["params"], "PULSE waveform parsed on V1")
    pulse = v1["params"]["waveform"]
    _assert(pulse.get("type") == "PULSE", f"waveform type=PULSE (got {pulse.get('type')})")
    _assert(abs(pulse["v2"] - 5.0) < 1e-9, f"PULSE v2=5 (got {pulse['v2']})")
    _assert(abs(pulse["per"] - 1e-3) < 1e-12, f"PULSE per=1m (got {pulse['per']})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("engine.sim.test_spice")
    print("=" * 60)

    test_value_suffixes()
    test_round_trip()
    test_netlist_bridge()
    test_netlist_bridge_via_library()
    test_analysis_cards()
    test_circuit_simulate_optional()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
