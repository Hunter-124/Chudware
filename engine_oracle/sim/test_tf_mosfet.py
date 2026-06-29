"""MOSFET small-signal AC + .tf transfer-function gates (house style).

Additive to the accuracy gates in test_circuit.py / test_spice.py / test_simdepth*.py (which must
stay green). Each test builds a Circuit dict inline (the sim.md sec.1 shape) and asserts against the
closed-form answer.

Gates:
  1. MOSFET AC small-signal  - common-source NMOS amp: |gain| ~ gm*Rd (gm from the DC bias).
  2. .tf resistive divider   - gain=R2/(R1+R2) exact; Rin=R1+R2; Rout=R1||R2  (all closed form).
  3. .tf inverting op-amp     - gain ~ -Rf/Rin; Rin ~ Rin resistor; Rout low.

Run:  & ".../venv/Scripts/python.exe" -m engine.sim.test_tf_mosfet
"""
from __future__ import annotations

import math
import time

import numpy as np

from engine.sim.circuit import simulate


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


# =============================================================================
# Gate 1 - MOSFET AC small-signal (common-source NMOS amplifier)
# =============================================================================

def test_mosfet_cs_ac() -> None:
    # Common-source NMOS: VDD through Rd to drain; gate held at a fixed DC bias by VG (DC) with a
    # unit AC drive; source grounded. Small-signal voltage gain ~ -gm*Rd (lambda small so gds tiny).
    VDD = 10.0
    Rd = 5000.0
    VGS_bias = 2.0     # gate bias (source at gnd) -> Vgs = 2.0, overdrive Vov = 1.0
    VTO = 1.0
    KP = 2e-4          # beta = KP*W/L = 2e-3 -> Id ~ 1 mA -> Rd drop ~5 V (stays saturated)
    Wl = 10.0          # W/L
    beta = KP * Wl
    LAMBDA = 0.0       # no channel-length modulation -> gds ~ gmin -> gain = -gm*Rd cleanly

    circuit = {
        "nodes": ["0", "VDD", "G", "D"],
        "devices": [
            {"kind": "V", "name": "VDD", "nodes": ["VDD", "0"], "value": VDD, "params": {}},
            # Gate bias source carries the AC drive (mag 1).
            {"kind": "V", "name": "VG", "nodes": ["G", "0"], "value": VGS_bias,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RD", "nodes": ["VDD", "D"], "value": Rd, "params": {}},
            # M nodes: D, G, S  (source to ground); bulk omitted.
            {"kind": "M", "name": "M1", "nodes": ["D", "G", "0"], "value": None,
             "model": "NM", "params": {}},
        ],
        "models": {"NM": {"type": "nmos", "VTO": VTO, "KP": KP, "W": Wl, "L": 1.0,
                          "LAMBDA": LAMBDA}},
        "params": {"TEMP": 27},
    }
    study = {
        "analyses": [
            {"type": "op"},
            {"type": "ac", "sweep": "dec", "points": 5, "fstart": 1, "fstop": 1000},
        ],
        "probes": ["V(D)", "V(G)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["op"], f"MOSFET op must converge; log={res['log']}"
    assert res["convergence"]["ac"], f"MOSFET ac must converge (no 'not yet modeled'); log={res['log']}"

    # DC operating point: confirm the device is in saturation and read the actual bias to size gm.
    vd_op = res["op"]["V(D)"]
    vgs = VGS_bias            # source grounded, gate at the bias
    vds = vd_op               # drain referenced to ground (= source)
    assert vds > (vgs - VTO), f"device not in saturation: Vds={vds:.3f} Vov={vgs - VTO:.3f}"
    Id = 0.5 * beta * (vgs - VTO) ** 2 * (1.0 + LAMBDA * vds)
    gm = beta * (vgs - VTO) * (1.0 + LAMBDA * vds)
    gain_exp = -gm * Rd       # textbook CS gain (gds negligible)

    mag = res["ac"]["mag"]["V(D)"]
    phase = res["ac"]["phase_deg"]["V(D)"]
    mid = len(mag) // 2
    gain_ac = mag[mid]        # |V(D)| per unit AC at the gate = |gain|
    err = _pct_err(gain_ac, abs(gain_exp))
    print(f"  CS NMOS: Id(bias)={Id*1e3:.4f} mA  gm={gm*1e3:.4f} mS  Rd={Rd:.0f} ohm")
    print(f"           |gain| measured={gain_ac:.4f}  expected gm*Rd={abs(gain_exp):.4f}  err={err:.3f}%")
    print(f"           phase @mid = {phase[mid]:.1f} deg (inverting ~180)")
    assert err < 10.0, f"CS AC gain off by {err:.3f}% (expected ~gm*Rd)"
    # Inverting stage: drain phase should be ~180 deg out of phase with the gate.
    assert abs(abs(phase[mid]) - 180.0) < 1.0, f"CS stage should invert: phase={phase[mid]:.1f}"
    print("  [gate 1 MOSFET CS AC small-signal] PASS")


def test_mosfet_pmos_ac_smoke() -> None:
    # p-channel sanity: confirm the AC stamp also works for PMOS (no 'not yet modeled' raise) and
    # produces a finite, inverting gain. Source at VDD, gate biased below VDD by |Vov|.
    VDD = 10.0
    Rd = 5000.0
    # The level-1 stamp uses a sign-flipped (positive-on) Vgs for both channel types, so the
    # threshold is given as a positive magnitude here (VTO=+1 -> |Vt|=1 for the p-channel too).
    VTO = 1.0
    KP = 2e-4           # beta = 2e-3 -> Id ~ 1 mA -> Rd drop ~5 V (stays saturated)
    Wl = 10.0
    circuit = {
        "nodes": ["0", "VDD", "G", "D"],
        "devices": [
            {"kind": "V", "name": "VDD", "nodes": ["VDD", "0"], "value": VDD, "params": {}},
            # Gate biased 2V below VDD -> Vsg = +2 (overdrive 1); AC drive on the gate.
            {"kind": "V", "name": "VG", "nodes": ["G", "0"], "value": VDD - 2.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RD", "nodes": ["D", "0"], "value": Rd, "params": {}},
            # PMOS: source=VDD, drain=D, gate=G.
            {"kind": "M", "name": "M1", "nodes": ["D", "G", "VDD"], "value": None,
             "model": "PM", "params": {}},
        ],
        "models": {"PM": {"type": "pmos", "VTO": VTO, "KP": KP, "W": Wl, "L": 1.0, "LAMBDA": 0.0}},
        "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "op"},
                     {"type": "ac", "sweep": "dec", "points": 3, "fstart": 1, "fstop": 100}],
        "probes": ["V(D)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["op"], f"PMOS op must converge; log={res['log']}"
    assert res["convergence"]["ac"], f"PMOS ac must converge; log={res['log']}"
    mag = res["ac"]["mag"]["V(D)"]
    g = mag[len(mag) // 2]
    print(f"  PMOS CS: |gain|={g:.4f} (finite, AC stamp works for p-channel)")
    assert math.isfinite(g) and g > 0.5, f"PMOS AC gain not sane: {g}"
    print("  [gate 1b PMOS AC small-signal works] PASS")


# =============================================================================
# Gate 2 - .tf on a resistive divider (gain / Rin / Rout vs closed form)
# =============================================================================

def test_tf_divider() -> None:
    # 1V source -> R1 (top) -> OUT -> R2 -> gnd.
    #   gain = R2/(R1+R2)        (V(OUT)/V1)
    #   Rin  = R1+R2             (resistance the source drives into)
    #   Rout = R1||R2            (output resistance with the source shorted)
    R1 = 1000.0
    R2 = 2000.0
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 1.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": R1, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": R2, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "tf", "output": "V(OUT)", "source": "V1"}], "probes": ["V(OUT)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["tf"], f"tf must converge; log={res['log']}"
    tf = res["tf"]

    gain_exp = R2 / (R1 + R2)
    rin_exp = R1 + R2
    rout_exp = R1 * R2 / (R1 + R2)
    eg = _pct_err(tf["gain"], gain_exp)
    ei = _pct_err(tf["rin"], rin_exp)
    eo = _pct_err(tf["rout"], rout_exp)
    print(f"  tf divider: gain={tf['gain']:.6f} (exp {gain_exp:.6f}, err {eg:.4f}%)")
    print(f"              Rin ={tf['rin']:.4f} ohm (exp {rin_exp:.1f}, err {ei:.4f}%)")
    print(f"              Rout={tf['rout']:.4f} ohm (exp {rout_exp:.4f}, err {eo:.4f}%)")
    assert eg < 0.01, f"divider tf gain off: {tf['gain']} vs {gain_exp}"
    assert ei < 0.01, f"divider tf Rin off: {tf['rin']} vs {rin_exp}"
    assert eo < 0.01, f"divider tf Rout off: {tf['rout']} vs {rout_exp}"
    print("  [gate 2 .tf resistive divider] PASS")


def test_tf_current_source_input() -> None:
    # .tf with a current-source input: I1 pushes into node A, R to ground. gain = V(A)/I1 = R
    # (transimpedance), Rin = R (the source sees R), Rout = R.
    R = 4700.0
    circuit = {
        "nodes": ["0", "A"],
        "devices": [
            {"kind": "I", "name": "I1", "nodes": ["A", "0"], "value": 1e-3, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["A", "0"], "value": R, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "tf", "output": "V(A)", "source": "I1"}], "probes": ["V(A)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["tf"], f"tf (I-source) must converge; log={res['log']}"
    tf = res["tf"]
    print(f"  tf I-source: transimpedance gain={tf['gain']:.4f} V/A (exp {R:.1f})  "
          f"Rin={tf['rin']:.4f}  Rout={tf['rout']:.4f}")
    assert _pct_err(tf["gain"], R) < 0.01, f"transimpedance off: {tf['gain']} vs {R}"
    assert _pct_err(tf["rin"], R) < 0.01, f"Rin off: {tf['rin']} vs {R}"
    assert _pct_err(tf["rout"], R) < 0.01, f"Rout off: {tf['rout']} vs {R}"
    print("  [gate 2b .tf current-source transimpedance] PASS")


# =============================================================================
# Gate 3 - .tf on the inverting op-amp (gain ~ -Rf/Rin, Rin ~ Rin resistor)
# =============================================================================

def test_tf_inverting_opamp() -> None:
    # Inverting amp: Vin -> Rin -> (-) ; Rf from (-) to OUT ; (+) to gnd ; ideal opamp.
    #   gain = -Rf/Rin ; the source drives into the virtual-ground node so Rin(seen) ~ Rin resistor;
    #   Rout is ~0 (ideal op-amp forces V(OUT), so a test current sees a stiff node).
    Rin = 1000.0
    Rf = 10000.0
    gain_exp = -Rf / Rin
    circuit = {
        "nodes": ["0", "IN", "INV", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.2,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RIN", "nodes": ["IN", "INV"], "value": Rin, "params": {}},
            {"kind": "R", "name": "RF", "nodes": ["INV", "OUT"], "value": Rf, "params": {}},
            {"kind": "OPAMP", "name": "X1", "nodes": ["0", "INV", "OUT"], "value": None,
             "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "tf", "output": "V(OUT)", "source": "V1"}], "probes": ["V(OUT)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["tf"], f"opamp tf must converge; log={res['log']}"
    tf = res["tf"]
    eg = _pct_err(tf["gain"], gain_exp)
    ei = _pct_err(tf["rin"], Rin)
    print(f"  tf inv opamp: gain={tf['gain']:.5f} (exp {gain_exp:.1f}, err {eg:.4f}%)")
    print(f"                Rin ={tf['rin']:.4f} ohm (exp ~{Rin:.0f}, err {ei:.4f}%)")
    print(f"                Rout={tf['rout']:.6g} ohm (expect ~0, stiff op-amp output)")
    assert eg < 0.5, f"inverting-amp tf gain off: {tf['gain']} vs {gain_exp}"
    # Source drives into a virtual ground, so the resistance it sees is the input resistor.
    assert ei < 1.0, f"inverting-amp tf Rin off: {tf['rin']} vs {Rin}"
    # Ideal op-amp output is stiff -> output resistance ~ 0 (well below the feedback resistor).
    assert abs(tf["rout"]) < 1.0, f"inverting-amp tf Rout should be ~0, got {tf['rout']}"
    print("  [gate 3 .tf inverting op-amp] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("ECAD SIM - MOSFET AC small-signal + .tf transfer function")
    print("=" * 64)

    tests = [
        ("1: MOSFET CS AC small-signal", test_mosfet_cs_ac),
        ("1b: PMOS AC small-signal works", test_mosfet_pmos_ac_smoke),
        ("2: .tf resistive divider", test_tf_divider),
        ("2b: .tf current-source transimpedance", test_tf_current_source_input),
        ("3: .tf inverting op-amp", test_tf_inverting_opamp),
    ]
    for title, fn in tests:
        print(f"\n--- {title} ---")
        fn()

    print(f"\nALL TF/MOSFET TESTS PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
