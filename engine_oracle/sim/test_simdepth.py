"""Depth tests for the native simulator: default-all-nodes output, .subckt expansion (built-in
op-amp + NE555), and .step parameter sweeps.

These are additive to the accuracy gates in test_circuit.py / test_spice.py (which must stay green).
Each test builds inputs inline and asserts against closed-form / qualitative expectations.

Run:  & ".../venv/Scripts/python.exe" -m engine.sim.test_simdepth
"""
from __future__ import annotations

import math
import time

import numpy as np

from engine.sim.circuit import simulate
from engine.sim.spice import parse_spice_deck
from engine.sim.subckt import SUBCKT_FIDELITY


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


# =============================================================================
# 1 — Default-all-nodes output (no probes specified)
# =============================================================================

def test_default_probes() -> None:
    # 10V divider, R1=1k / R2=3k -> V(OUT) = 10 * 3/4 = 7.5 V.  NO probes in the study.
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 3000.0, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "op"}]}  # <-- no "probes" key
    res = simulate(circuit, study)
    assert res["convergence"]["op"], "op must converge"
    op = res["op"]
    # Every non-ground node voltage present, plus the V-source current.
    assert "V(IN)" in op and "V(OUT)" in op, f"default node probes missing: {list(op)}"
    assert "I(V1)" in op, f"default source-current probe missing: {list(op)}"
    assert "V(0)" not in op, "ground must not be emitted as a default probe"
    err_in = _pct_err(op["V(IN)"], 10.0)
    err_out = _pct_err(op["V(OUT)"], 7.5)
    print(f"  default probes: V(IN)={op['V(IN)']:.4f} (exp 10), V(OUT)={op['V(OUT)']:.4f} (exp 7.5), "
          f"I(V1)={op['I(V1)']:.6e}")
    assert err_in < 0.01 and err_out < 0.01, f"divider default-probe values off ({err_in}, {err_out})"
    print("  [1 default-all-nodes output] PASS")


def test_explicit_probes_unchanged() -> None:
    # Same divider, but WITH an explicit probe -> result must contain ONLY that probe (no defaults).
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 3000.0, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    res = simulate(circuit, {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]})
    assert set(res["op"].keys()) == {"V(OUT)"}, \
        f"explicit-probe behaviour changed: {list(res['op'])}"
    print(f"  explicit probes: keys={list(res['op'])} (only V(OUT)) PASS")


# =============================================================================
# 2 — .subckt expansion: built-in op-amp as an inverting amp
# =============================================================================

def test_opamp_subckt_inverting() -> None:
    # X1 instantiates the built-in OPAMP subckt (in+ in- out). Inverting amp gain = -Rf/Rin.
    Rin, Rf, vin = 1000.0, 10000.0, 0.2
    gain_exp = -Rf / Rin
    deck = f"""\
* inverting amp using the built-in OPAMP subckt
V1 IN 0 DC {vin}
RIN IN INV {Rin}
RF INV OUT {Rf}
X1 0 INV OUT OPAMP
.op
.end
"""
    circ, study = parse_spice_deck(deck)
    study["probes"] = ["V(OUT)"]
    res = simulate(circ, study)
    assert res["convergence"]["op"], f"opamp-subckt op must converge; log={res['log']}"
    vout = res["op"]["V(OUT)"]
    gain = vout / vin
    err = _pct_err(gain, gain_exp)
    print(f"  opamp subckt: V(OUT)={vout:.5f} V  gain={gain:.4f}  expected={gain_exp:.4f}  "
          f"err={err:.3f}%")
    # The op-amp subckt expands to internal devices (R/E/C) with uniquified names.
    inst_names = [d["name"] for d in circ["devices"] if ":X1" in d["name"]]
    assert inst_names, "X1 did not expand into named internal devices"
    print(f"  opamp subckt expanded into: {inst_names}")
    assert err < 1.0, f"inverting-amp gain off by {err:.3f}% (subckt fidelity)"
    print(f"  fidelity: {SUBCKT_FIDELITY['OPAMP'][:60]}...")
    print("  [2 op-amp subckt inverting amp] PASS")


# =============================================================================
# 3 — .subckt expansion: built-in NE555 astable oscillator
# =============================================================================

def test_ne555_astable() -> None:
    # Classic astable: RA from VCC to DIS, RB from DIS to the timing cap; THR=TRIG=cap top.
    # Output should oscillate, swinging between ~0 and ~Vcc as the cap ramps 1/3..2/3 Vcc.
    vcc = 5.0
    deck = f"""\
* NE555 astable: RA=10k RB=10k C=10n, Vcc=5
VCC VCC 0 DC {vcc}
RA VCC DIS 10k
RB DIS CAP 10k
C1 CAP 0 10n
X1 0 CAP OUT VCC CV CAP DIS VCC NE555
.tran 2u 4m 0
.end
"""
    circ, study = parse_spice_deck(deck)
    study["probes"] = ["V(OUT)", "V(CAP)"]
    res = simulate(circ, study)
    assert res["convergence"]["tran"], f"NE555 tran must converge; log={res['log'][-2:]}"

    t = np.array(res["tran"]["x_t"])
    vout = np.array(res["tran"]["traces"]["V(OUT)"])
    vcap = np.array(res["tran"]["traces"]["V(CAP)"])

    out_hi = float(vout.max())
    out_lo = float(vout.min())
    cap_hi = float(vcap.max())
    cap_lo = float(vcap.min())

    # Oscillation: OUT must cross BOTH a high threshold (>0.8*Vcc) and a low threshold (<0.2*Vcc).
    hi_thresh = 0.8 * vcc
    lo_thresh = 0.2 * vcc
    crosses_high = bool((vout > hi_thresh).any())
    crosses_low = bool((vout < lo_thresh).any())

    # Count toggles (sign changes of OUT about Vcc/2) to confirm repeated oscillation.
    sg = np.sign(vout - vcc / 2.0)
    n_toggles = int(np.sum(np.diff(sg) != 0))

    print(f"  NE555 astable: OUT swing [{out_lo:.3f}, {out_hi:.3f}] V  "
          f"CAP swing [{cap_lo:.3f}, {cap_hi:.3f}] V")
    print(f"                 crosses >{hi_thresh:.1f}V: {crosses_high}  "
          f"crosses <{lo_thresh:.1f}V: {crosses_low}  toggles about Vcc/2: {n_toggles}")
    # Cap should ramp roughly within the 1/3..2/3 Vcc band (behavioural — allow generous slack).
    print(f"                 ideal cap band ~[{vcc/3:.2f}, {2*vcc/3:.2f}] V")

    assert crosses_high, f"OUT never reached high (>{hi_thresh} V): max={out_hi:.3f}"
    assert crosses_low, f"OUT never reached low (<{lo_thresh} V): min={out_lo:.3f}"
    assert n_toggles >= 4, f"OUT did not oscillate (only {n_toggles} toggles)"
    # Cap stays bounded in a sane region (no numerical runaway).
    assert 0.5 < cap_lo < vcc and 0.5 < cap_hi < vcc + 0.5, \
        f"cap swing out of physical band: [{cap_lo}, {cap_hi}]"
    print(f"  fidelity: {SUBCKT_FIDELITY['NE555'][:60]}...")
    print("  [3 NE555 astable oscillation] PASS")


def test_ne555_via_netlist_bridge() -> None:
    # The starter NE555 part (sim.model='SUBCKT', subckt='NE555') must map to an X instance and
    # expand through netlist_to_spice_circuit (the path the GUI uses).
    from engine.sim.spice import netlist_to_spice_circuit

    netlist = {
        "components": [
            {
                "ref": "U1", "libid": "Timer:NE555", "value": "",
                "pins": {"GND": "0", "TRIG": "CAP", "OUT": "OUT", "RST": "VCC",
                         "CV": "CV", "THR": "CAP", "DIS": "DIS", "VCC": "VCC"},
                "dnp": False,
            },
        ],
    }
    circuit, warns = netlist_to_spice_circuit(netlist)
    # The bridge emits the instance as "XU1" (X + ref); expanded devices carry the ":XU1" suffix.
    inst = [d["name"] for d in circuit["devices"] if ":XU1" in d["name"]]
    assert inst, f"NE555 starter part did not expand to devices; warns={warns}"
    assert "Dclamp" in circuit["models"], "built-in Dclamp model not merged from bridge"
    print(f"  netlist bridge NE555 -> {len(inst)} internal devices (e.g. {inst[:3]})")
    print("  [3b NE555 via netlist bridge] PASS")


# =============================================================================
# 4 — .step parameter sweep
# =============================================================================

def test_step_divider_resistor() -> None:
    # Sweep R2 of a 10V / R1=1k / R2 divider over three values; V(OUT)=10*R2/(R1+R2) increases
    # monotonically with R2.
    def circ():
        return {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }

    study = {
        "analyses": [{"type": "op"}],
        "probes": ["V(OUT)"],
        "step": {"param": "R2", "values": [1000.0, 2000.0, 4000.0]},
    }
    res = simulate(circ(), study)
    assert res["convergence"]["step"], f"step sweep must converge; log={res['log']}"
    step = res["step"]
    assert step["param"] == "R2"
    assert step["values"] == [1000.0, 2000.0, 4000.0]

    vouts = [run["op"]["V(OUT)"] for run in step["runs"]]
    expected = [10.0 * r2 / (1000.0 + r2) for r2 in step["values"]]
    print("  step R2 sweep:")
    for r2, vo, ex in zip(step["values"], vouts, expected):
        print(f"    R2={r2:.0f}  V(OUT)={vo:.4f} V  expected={ex:.4f} V  err={_pct_err(vo, ex):.3f}%")
        assert _pct_err(vo, ex) < 0.01, f"step value wrong at R2={r2}: {vo} vs {ex}"

    # Monotonic increase in midpoint voltage.
    assert vouts[0] < vouts[1] < vouts[2], f"V(OUT) not monotonic in R2: {vouts}"
    print("  [4 .step divider resistor] PASS")


def test_step_via_analysis_entry() -> None:
    # The step spec may also live as an analysis entry {"type":"step", ...} wrapping the rest.
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 12.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [
            {"type": "step", "param": "R1", "start": 1000.0, "stop": 3000.0, "step": 1000.0},
            {"type": "op"},
        ],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert "step" in res, "step analysis entry not recognised"
    vals = res["step"]["values"]
    assert vals == [1000.0, 2000.0, 3000.0], f"start/stop/step expansion wrong: {vals}"
    vouts = [r["op"]["V(OUT)"] for r in res["step"]["runs"]]
    # Increasing R1 lowers V(OUT) = 12*R2/(R1+R2).
    assert vouts[0] > vouts[1] > vouts[2], f"V(OUT) should fall as R1 rises: {vouts}"
    print(f"  step-as-analysis: R1 {vals} -> V(OUT) {[round(v,3) for v in vouts]} (monotonic down)")
    print("  [4b .step via analysis entry] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("ECAD SIM DEPTH — default probes / .subckt / .step")
    print("=" * 64)

    tests = [
        ("1: default-all-nodes output", test_default_probes),
        ("1b: explicit probes unchanged", test_explicit_probes_unchanged),
        ("2: op-amp subckt inverting amp", test_opamp_subckt_inverting),
        ("3: NE555 astable oscillation", test_ne555_astable),
        ("3b: NE555 via netlist bridge", test_ne555_via_netlist_bridge),
        ("4: .step divider resistor", test_step_divider_resistor),
        ("4b: .step via analysis entry", test_step_via_analysis_entry),
    ]
    for title, fn in tests:
        print(f"\n--- {title} ---")
        fn()

    print(f"\nALL SIM-DEPTH TESTS PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
