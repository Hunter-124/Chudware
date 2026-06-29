"""Coupled-inductor (K) + behavioral-source (B) accuracy gates -- plain main()+asserts (house style).

These exercise the two ngspice-class primitives added to the native simulator:

  * **K mutual inductance / transformers** -- ``K Lx Ly k`` couples two inductors with ``M=k*sqrt(Lx*Ly)``.
    A transformer with turns ratio ``n = sqrt(L2/L1)`` steps an AC (and transient) primary voltage to
    ``n*Vp`` on the open secondary (scaled by ``k`` for loose coupling).
  * **B arbitrary source** -- ``B n+ n- V=<expr>`` / ``I=<expr>`` evaluated by the SAFE-AST evaluator and
    linearized each Newton iteration by a finite-difference Jacobian (exact VCVS/VCCS for a linear expr,
    nonlinear for a product / square law).
  * **Safety** -- a hostile expression (``__import__('os')`` etc.) is REJECTED at build, never executed.

Run:  & ".../venv/Scripts/python.exe" -m engine.sim.test_behavioral
"""
from __future__ import annotations

import math
import time

import numpy as np

from engine.sim.circuit import simulate
from engine.sim.devices import DeviceError, SafeExprError, _SafeExpr, make_device
from engine.sim.spice import parse_spice_deck


# --------------------------------------------------------------------------- helpers

def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


def _mid(seq: list[float]) -> float:
    return seq[len(seq) // 2]


# =============================================================================
# Gate K1 -- transformer turns ratio (AC), tight coupling
# =============================================================================

def test_transformer_ac_turns_ratio() -> None:
    # L1=1mH primary, L2=4mH secondary -> turns ratio n = sqrt(L2/L1) = 2.
    # Tight coupling k=0.999, open (high-impedance) secondary: |V(SEC)| = k*n*|V(IN)|.
    L1, L2 = 1e-3, 4e-3
    k = 0.999
    n = math.sqrt(L2 / L1)
    circuit = {
        "nodes": ["0", "IN", "SEC"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "L", "name": "L1", "nodes": ["IN", "0"], "value": L1, "params": {}},
            {"kind": "L", "name": "L2", "nodes": ["SEC", "0"], "value": L2, "params": {}},
            {"kind": "R", "name": "RL", "nodes": ["SEC", "0"], "value": 1e7, "params": {}},
            {"kind": "K", "name": "K1", "nodes": [], "value": k,
             "params": {"inductors": ["L1", "L2"], "k": k}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 5, "fstart": 1e4, "fstop": 1e6}],
             "probes": ["V(IN)", "V(SEC)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["ac"], f"transformer AC must converge; log={res['log']}"
    vin = res["ac"]["mag"]["V(IN)"]; vsec = res["ac"]["mag"]["V(SEC)"]
    ratio = _mid(vsec) / _mid(vin)
    exp = k * n  # open-secondary transfer
    err = _pct_err(ratio, exp)
    print(f"  transformer AC: V(SEC)/V(IN) = {ratio:.5f}  expected k*n = {exp:.5f}  (n={n:.3f})  err={err:.3f}%")
    assert err < 0.5, f"turns ratio off by {err:.3f}%"
    print("  [gate K1 transformer turns ratio (AC)] PASS")


# =============================================================================
# Gate K2 -- loose coupling shows reduced transfer
# =============================================================================

def test_transformer_loose_coupling() -> None:
    L1, L2 = 1e-3, 4e-3
    n = math.sqrt(L2 / L1)

    def transfer(k: float) -> float:
        circuit = {
            "nodes": ["0", "IN", "SEC"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
                 "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "L", "name": "L1", "nodes": ["IN", "0"], "value": L1, "params": {}},
                {"kind": "L", "name": "L2", "nodes": ["SEC", "0"], "value": L2, "params": {}},
                {"kind": "R", "name": "RL", "nodes": ["SEC", "0"], "value": 1e7, "params": {}},
                {"kind": "K", "name": "K1", "nodes": [], "value": k,
                 "params": {"inductors": ["L1", "L2"], "k": k}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        res = simulate(circuit, {"analyses": [
            {"type": "ac", "sweep": "dec", "points": 3, "fstart": 1e4, "fstop": 1e6}],
            "probes": ["V(SEC)"]})
        assert res["convergence"]["ac"], "loose transformer must converge"
        return _mid(res["ac"]["mag"]["V(SEC)"])

    tight = transfer(0.999)
    loose = transfer(0.5)
    print(f"  loose coupling: |V(SEC)| k=0.999 -> {tight:.5f}, k=0.5 -> {loose:.5f}  (open-sec = k*n)")
    # Open-secondary transfer is exactly k*n: k=0.5 should give 0.5*2 = 1.0, vs 0.999*2 ~ 2.0.
    assert _pct_err(loose, 0.5 * math.sqrt(L2 / L1)) < 0.5, "k=0.5 transfer wrong"
    assert loose < 0.6 * tight, "loose coupling did not reduce transfer"
    print("  [gate K2 loose coupling reduced transfer] PASS")


# =============================================================================
# Gate K3 -- transient transformer (sine primary -> scaled secondary)
# =============================================================================

def test_transformer_transient() -> None:
    L1, L2 = 1e-3, 4e-3
    k = 0.999
    n = math.sqrt(L2 / L1)
    f = 1e4
    circuit = {
        "nodes": ["0", "IN", "P", "SEC"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": {"type": "SIN", "vo": 0.0, "va": 1.0, "f": f}}},
            {"kind": "R", "name": "RS", "nodes": ["IN", "P"], "value": 1.0, "params": {}},
            {"kind": "L", "name": "L1", "nodes": ["P", "0"], "value": L1, "params": {}},
            {"kind": "L", "name": "L2", "nodes": ["SEC", "0"], "value": L2, "params": {}},
            {"kind": "R", "name": "RL", "nodes": ["SEC", "0"], "value": 1e6, "params": {}},
            {"kind": "K", "name": "K1", "nodes": [], "value": k,
             "params": {"inductors": ["L1", "L2"], "k": k}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "tran", "tstep": 1.0 / f / 100, "tstop": 3.0 / f, "uic": True}],
             "probes": ["V(P)", "V(SEC)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["tran"], f"transformer tran must converge; log={res['log']}"
    vp = np.array(res["tran"]["traces"]["V(P)"])
    vs = np.array(res["tran"]["traces"]["V(SEC)"])
    half = len(vp) // 2  # ignore the start-up transient
    ratio = np.max(np.abs(vs[half:])) / np.max(np.abs(vp[half:]))
    err = _pct_err(ratio, k * n)
    print(f"  transformer tran: |V(SEC)|/|V(P)| = {ratio:.5f}  expected k*n = {k*n:.5f}  err={err:.3f}%")
    assert err < 2.0, f"transient turns ratio off by {err:.3f}%"
    print("  [gate K3 transient transformer] PASS")


# =============================================================================
# Gate B1 -- linear behavioral V=2*V(in) (op + DC sweep, exact 2x)
# =============================================================================

def test_behavioral_linear_v() -> None:
    # B1 OUT 0 V=2*V(IN) loading a 1k resistor. Output must be exactly 2*V(IN).
    def build(vin: float) -> dict:
        return {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": vin, "params": {}},
                {"kind": "B", "name": "B1", "nodes": ["OUT", "0"], "value": None,
                 "params": {"btype": "V", "expr": "2*V(IN)"}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
    res = simulate(build(1.5), {"analyses": [{"type": "op"}], "probes": ["V(IN)", "V(OUT)"]})
    assert res["convergence"]["op"], f"B linear op must converge; log={res['log']}"
    vout = res["op"]["V(OUT)"]
    print(f"  B V=2*V(in) (op): V(IN)=1.5 -> V(OUT)={vout:.6f}  expected 3.0  err={_pct_err(vout,3.0):.4f}%")
    assert _pct_err(vout, 3.0) < 0.01, "linear behavioral not exactly 2x"

    # DC sweep V1 0..5: V(OUT) must track 2*Vin exactly.
    res2 = simulate(build(0.0),
                    {"analyses": [{"type": "dc", "source": "V1", "start": 0.0, "stop": 5.0, "step": 1.0}],
                     "probes": ["V(OUT)"]})
    assert res2["convergence"]["dc"], "B linear DC sweep must converge"
    xs = res2["dc"]["x"]; vo = res2["dc"]["traces"]["V(OUT)"]
    for xi, voi in zip(xs, vo):
        assert _pct_err(voi, 2.0 * xi) < 0.01 or xi == 0.0, f"sweep wrong at Vin={xi}: {voi}"
    print(f"  B V=2*V(in) (dc): V(OUT) at Vin=5 -> {vo[-1]:.6f}  expected 10.0")
    print("  [gate B1 linear behavioral V] PASS")


# =============================================================================
# Gate B2 -- nonlinear behavioral V=V(a)*V(b) (multiplier, two op points)
# =============================================================================

def test_behavioral_multiplier() -> None:
    def vout_for(va: float, vb: float) -> float:
        circuit = {
            "nodes": ["0", "A", "B", "OUT"],
            "devices": [
                {"kind": "V", "name": "VA", "nodes": ["A", "0"], "value": va, "params": {}},
                {"kind": "V", "name": "VB", "nodes": ["B", "0"], "value": vb, "params": {}},
                {"kind": "B", "name": "B1", "nodes": ["OUT", "0"], "value": None,
                 "params": {"btype": "V", "expr": "V(A)*V(B)"}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        res = simulate(circuit, {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]})
        assert res["convergence"]["op"], f"multiplier op must converge; log={res['log']}"
        return res["op"]["V(OUT)"]

    for va, vb in ((3.0, 4.0), (-2.0, 5.0), (1.5, 1.5)):
        got = vout_for(va, vb)
        exp = va * vb
        err = _pct_err(got, exp)
        print(f"  B V=V(A)*V(B): {va} * {vb} -> V(OUT)={got:.6f}  expected {exp:.6f}  err={err:.4f}%")
        assert err < 0.1, f"multiplier off at ({va},{vb}): {err:.4f}%"
    print("  [gate B2 nonlinear multiplier] PASS")


# =============================================================================
# Gate B3 -- behavioral current source I = k*V(g)^2 (square law)
# =============================================================================

def test_behavioral_current_square_law() -> None:
    # B from 0 -> OUT injects I = 1e-3*V(G)^2 into a 1k load: V(OUT) = 1e3 * 1e-3 * Vg^2 = Vg^2.
    kk = 1e-3
    R = 1000.0
    for vg in (2.0, 3.0):
        circuit = {
            "nodes": ["0", "G", "OUT"],
            "devices": [
                {"kind": "V", "name": "VG", "nodes": ["G", "0"], "value": vg, "params": {}},
                {"kind": "B", "name": "B1", "nodes": ["0", "OUT"], "value": None,
                 "params": {"btype": "I", "expr": "1e-3*V(G)**2"}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": R, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        res = simulate(circuit, {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]})
        assert res["convergence"]["op"], f"square-law op must converge; log={res['log']}"
        vout = res["op"]["V(OUT)"]
        exp = R * kk * vg * vg  # current * load
        err = _pct_err(vout, exp)
        print(f"  B I=1e-3*V(G)^2: V(G)={vg} -> V(OUT)={vout:.6f}  expected {exp:.6f}  err={err:.4f}%")
        assert err < 0.1, f"square-law off at Vg={vg}: {err:.4f}%"
    print("  [gate B3 behavioral square-law current] PASS")


# =============================================================================
# Gate B4 -- expression safety: hostile input rejected, not executed
# =============================================================================

def test_behavioral_safety() -> None:
    hostile = [
        "__import__('os')",
        "os.system('echo pwned')",
        "open('secret.txt')",
        "(lambda: 1)()",
        "V(in).__class__",
    ]
    for expr in hostile:
        rejected = False
        try:
            _SafeExpr(expr)
        except (SafeExprError, SyntaxError):
            rejected = True
        assert rejected, f"hostile expr NOT rejected by _SafeExpr: {expr!r}"
        # Also rejected at device-build time (clear DeviceError, never executed).
        try:
            make_device({"kind": "B", "name": "B1", "nodes": ["OUT", "0"], "value": None,
                         "params": {"btype": "V", "expr": expr}}, {}, {})
            assert False, f"hostile expr built a device: {expr!r}"
        except DeviceError:
            pass
        print(f"  rejected (not executed): {expr!r}")
    print("  [gate B4 expression safety] PASS")


# =============================================================================
# Gate P -- SPICE parsing of K and B cards into the frozen Circuit shape
# =============================================================================

def test_spice_parse_k_b() -> None:
    deck = (
        "* transformer + behavioral source\n"
        "V1 IN 0 AC 1\n"
        "L1 IN 0 1m\n"
        "L2 SEC 0 4m\n"
        "RL SEC 0 1meg\n"
        "K1 L1 L2 0.5\n"
        "B1 OUT 0 V=2*V(IN)\n"
        "B2 0 OUT2 I=1e-3*V(SEC)**2\n"
        "RB OUT 0 1k\n"
        "RB2 OUT2 0 1k\n"
        ".ac dec 5 1k 1meg\n"
        ".end\n"
    )
    circuit, _study = parse_spice_deck(deck)
    by_name = {d["name"]: d for d in circuit["devices"]}
    k1 = by_name["K1"]
    assert k1["kind"] == "K", "K1 kind"
    assert k1["params"]["inductors"] == ["L1", "L2"], f"K1 inductors {k1['params']}"
    assert _pct_err(k1["params"]["k"], 0.5) < 0.01, "K1 coupling value"
    b1 = by_name["B1"]
    assert b1["kind"] == "B" and b1["params"]["btype"] == "V", "B1 V-type"
    assert b1["params"]["expr"] == "2*V(IN)", f"B1 expr {b1['params']}"
    b2 = by_name["B2"]
    assert b2["kind"] == "B" and b2["params"]["btype"] == "I", "B2 I-type"
    assert "V(SEC)" in b2["params"]["expr"], f"B2 expr {b2['params']}"
    print("  K/B cards parsed:", {n: by_name[n]["kind"] for n in ("K1", "B1", "B2")})

    # End-to-end: the parsed deck simulates and the transformer transfers (k=0.5 -> 0.5*2 = 1.0).
    res = simulate(circuit, {"analyses": [
        {"type": "ac", "sweep": "dec", "points": 3, "fstart": 1e4, "fstop": 1e6}],
        "probes": ["V(SEC)", "V(IN)"]})
    assert res["convergence"]["ac"], f"parsed-deck AC must converge; log={res['log']}"
    ratio = _mid(res["ac"]["mag"]["V(SEC)"]) / _mid(res["ac"]["mag"]["V(IN)"])
    print(f"  parsed deck: V(SEC)/V(IN)={ratio:.4f} (k=0.5, n=2 -> exp 1.0)")
    assert _pct_err(ratio, 1.0) < 1.0, "parsed transformer transfer wrong"
    print("  [gate P SPICE parse K/B] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("ECAD COUPLED-INDUCTOR (K) + BEHAVIORAL-SOURCE (B) GATES")
    print("=" * 64)

    gates = [
        ("Gate K1: transformer turns ratio (AC)", test_transformer_ac_turns_ratio),
        ("Gate K2: loose coupling reduced transfer", test_transformer_loose_coupling),
        ("Gate K3: transient transformer", test_transformer_transient),
        ("Gate B1: linear behavioral V=2*V(in)", test_behavioral_linear_v),
        ("Gate B2: nonlinear multiplier V(a)*V(b)", test_behavioral_multiplier),
        ("Gate B3: behavioral square-law current", test_behavioral_current_square_law),
        ("Gate B4: expression safety (reject hostile)", test_behavioral_safety),
        ("Gate P: SPICE parse K/B cards", test_spice_parse_k_b),
    ]
    for title, fn in gates:
        print(f"\n--- {title} ---")
        fn()

    print(f"\nALL BEHAVIORAL/COUPLED GATES PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
