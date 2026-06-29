"""Circuit-simulator accuracy gates (sim.md §5) — plain main()+asserts (house style).

Each test builds a Circuit dict inline (the sim.md §1 shape — the integration boundary with the
SPICE parser) and asserts the result against the closed-form answer (≤1% unless noted).

Gates:
  1. resistive divider              — exact
  2. RC step                        — V(out) = 63.2% of final at t = RC
  3. RL step                        — i(L)   = 63.2% of final at t = L/R
  4. series-RLC ring                — ring frequency & damped envelope (zero-cross / FFT)
  5. diode clamp                    — clamp output voltage
  6. inverting op-amp               — gain = -Rf/Rin  (op + ac)
  7. common-emitter DC bias         — Vce in the active region
  8. RC low-pass AC                 — -3 dB at f = 1/(2*pi*RC), ~-20 dB/dec slope

Run:  & ".../venv/Scripts/python.exe" -m engine.sim.test_circuit
"""
from __future__ import annotations

import math
import time

import numpy as np

from engine.sim.circuit import simulate


# --------------------------------------------------------------------------- helpers

def _wf(kind: str, **kw) -> dict:
    d = {"type": kind}
    d.update(kw)
    return d


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


# =============================================================================
# Gate 1 — resistive divider (exact)
# =============================================================================

def test_resistive_divider() -> None:
    # 10V across R1=1k (top) and R2=2k (bottom). V(out) = 10 * 2k/3k = 6.6667 V.
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)"]}
    res = simulate(circuit, study)
    vout = res["op"]["V(OUT)"]
    iv1 = res["op"]["I(V1)"]
    exp = 10.0 * 2000.0 / 3000.0
    err = _pct_err(vout, exp)
    print(f"  divider: V(OUT)={vout:.6f} V  expected={exp:.6f} V  err={err:.4f}%")
    print(f"           I(V1) ={iv1:.6e} A   expected={-10.0/3000.0:.6e} A")
    assert res["convergence"]["op"], "op must converge"
    assert err < 0.01, f"divider not exact: {err:.4f}%"
    # Source current = -(V/Rtotal) by passive sign convention through the V branch.
    assert _pct_err(iv1, -10.0 / 3000.0) < 0.1, f"I(V1) wrong: {iv1:.6e}"
    print("  [gate 1 resistive divider] PASS")


# =============================================================================
# Gate 2 — RC step (tau = RC, 63.2% at t=RC)
# =============================================================================

def test_rc_step() -> None:
    R = 1000.0; C = 1e-6
    tau = R * C  # 1 ms
    vstep = 5.0
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=vstep, td=0.0, tr=1e-9,
                                         tf=1e-9, pw=1.0, per=0.0)}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": R, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": C,
             "params": {"ic": 0.0}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "tran", "tstep": tau / 200, "tstop": 5 * tau, "uic": True}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["tran"], "tran must converge"
    t = np.array(res["tran"]["x_t"]); v = np.array(res["tran"]["traces"]["V(OUT)"])
    # Value at t = tau (interpolate).
    v_at_tau = float(np.interp(tau, t, v))
    exp = vstep * (1 - math.exp(-1.0))  # 63.21%
    err = _pct_err(v_at_tau, exp)
    print(f"  RC step: V(out) @ t=RC = {v_at_tau:.5f} V  expected={exp:.5f} V (63.2%)  err={err:.3f}%")
    print(f"           final = {v[-1]:.5f} V (expected {vstep})")
    assert err < 1.0, f"RC step @tau off by {err:.3f}%"
    assert _pct_err(v[-1], vstep) < 1.0, "RC final value off"
    print("  [gate 2 RC step] PASS")


# =============================================================================
# Gate 3 — RL step (tau = L/R, 63.2% of i_final at t=L/R)
# =============================================================================

def test_rl_step() -> None:
    R = 100.0; L = 0.1
    tau = L / R  # 1 ms
    vstep = 5.0
    i_final = vstep / R
    circuit = {
        "nodes": ["0", "IN", "MID"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=vstep, td=0.0, tr=1e-12,
                                         tf=1e-12, pw=1.0, per=0.0)}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "MID"], "value": R, "params": {}},
            {"kind": "L", "name": "L1", "nodes": ["MID", "0"], "value": L, "params": {"ic": 0.0}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "tran", "tstep": tau / 200, "tstop": 5 * tau, "uic": True}],
        "probes": ["I(L1)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["tran"], "tran must converge"
    t = np.array(res["tran"]["x_t"]); i = np.array(res["tran"]["traces"]["I(L1)"])
    i_at_tau = float(np.interp(tau, t, i))
    exp = i_final * (1 - math.exp(-1.0))
    err = _pct_err(i_at_tau, exp)
    print(f"  RL step: i(L) @ t=L/R = {i_at_tau:.6f} A  expected={exp:.6f} A (63.2%)  err={err:.3f}%")
    print(f"           final = {i[-1]:.6f} A (expected {i_final})")
    assert err < 1.5, f"RL step @tau off by {err:.3f}%"
    assert _pct_err(i[-1], i_final) < 1.0, "RL final current off"
    print("  [gate 3 RL step] PASS")


# =============================================================================
# Gate 4 — series RLC ring (frequency + damping)
# =============================================================================

def test_rlc_ring() -> None:
    # Underdamped series RLC driven by a step; observe ringing on the cap.
    R = 20.0; L = 1e-3; C = 1e-6
    w0 = 1.0 / math.sqrt(L * C)          # rad/s
    f0 = w0 / (2 * math.pi)
    zeta = (R / 2.0) * math.sqrt(C / L)  # damping ratio
    wd = w0 * math.sqrt(1 - zeta * zeta)
    fd = wd / (2 * math.pi)
    period = 1.0 / fd
    vstep = 1.0
    circuit = {
        "nodes": ["0", "IN", "MID", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=vstep, td=0.0, tr=1e-9,
                                         tf=1e-9, pw=1.0, per=0.0)}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "MID"], "value": R, "params": {}},
            {"kind": "L", "name": "L1", "nodes": ["MID", "OUT"], "value": L, "params": {"ic": 0.0}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": C, "params": {"ic": 0.0}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    tstop = 10 * period
    study = {
        "analyses": [{"type": "tran", "tstep": period / 200, "tstop": tstop, "uic": True}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["tran"], "tran must converge"
    t = np.array(res["tran"]["x_t"]); v = np.array(res["tran"]["traces"]["V(OUT)"])

    # Ring frequency from zero-crossings of (v - final).
    vsig = v - vstep
    sign = np.sign(vsig)
    crossings = np.where(np.diff(sign) != 0)[0]
    # interpolate crossing times
    tc = []
    for k in crossings:
        t0, t1 = t[k], t[k + 1]
        y0, y1 = vsig[k], vsig[k + 1]
        if y1 != y0:
            tc.append(t0 - y0 * (t1 - t0) / (y1 - y0))
    tc = np.array(tc)
    assert len(tc) >= 4, f"too few zero crossings ({len(tc)}) to estimate frequency"
    # half-period between consecutive crossings
    half_periods = np.diff(tc)
    fd_meas = 1.0 / (2.0 * np.median(half_periods))
    err_f = _pct_err(fd_meas, fd)
    print(f"  RLC ring: f_d measured={fd_meas:.2f} Hz  expected={fd:.2f} Hz  err={err_f:.3f}%  (f0={f0:.1f}, zeta={zeta:.4f})")

    # Damping from successive peak ratio (logarithmic decrement).
    # Find local maxima of vsig.
    peaks = []
    for k in range(1, len(vsig) - 1):
        if vsig[k] > vsig[k - 1] and vsig[k] >= vsig[k + 1] and vsig[k] > 0:
            peaks.append((t[k], vsig[k]))
    assert len(peaks) >= 2, f"need >=2 positive peaks, got {len(peaks)}"
    (t1, a1), (t2, a2) = peaks[0], peaks[1]
    delta = math.log(a1 / a2)                       # log decrement over one period
    zeta_meas = delta / math.sqrt(4 * math.pi ** 2 + delta ** 2)
    err_z = _pct_err(zeta_meas, zeta)
    print(f"  RLC ring: zeta measured={zeta_meas:.4f}  expected={zeta:.4f}  err={err_z:.2f}%")

    assert err_f < 2.0, f"ring frequency off by {err_f:.3f}%"
    assert err_z < 10.0, f"damping off by {err_z:.2f}% (note: <=10% tol for log-dec estimate)"
    print("  [gate 4 series-RLC ring] PASS")


# =============================================================================
# Gate 5 — diode clamp
# =============================================================================

def test_diode_clamp() -> None:
    # 5V through 1k into a diode to ground. Output clamps near the diode drop (~0.6-0.75V).
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "D", "name": "D1", "nodes": ["OUT", "0"], "value": None,
             "model": "D1N4148", "params": {}},
        ],
        "models": {"D1N4148": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}},
        "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["op"], f"diode op must converge; log={res['log']}"
    vout = res["op"]["V(OUT)"]
    # Closed form: solve 5 = I*1k + Vd with I = IS*(exp(Vd/(N*Vt))-1) + I*RS folded.
    IS, N, RS = 2.52e-9, 1.752, 0.568
    Vt = 1.380649e-23 * 300.15 / 1.602176634e-19
    # Newton for the reference diode voltage at the output node.
    vd = 0.6
    for _ in range(100):
        i = IS * (math.exp(vd / (N * Vt)) - 1.0)
        # node OUT = vd + i*RS (internal), and 5 = i*1000 + (vd + i*RS)
        f = i * (1000.0 + RS) + vd - 5.0
        gi = IS / (N * Vt) * math.exp(vd / (N * Vt))
        df = gi * (1000.0 + RS) + 1.0
        vd -= f / df
    i_final = IS * (math.exp(vd / (N * Vt)) - 1.0)
    vout_exp = vd + i_final * RS
    err = _pct_err(vout, vout_exp)
    print(f"  diode clamp: V(OUT)={vout:.5f} V  closed-form={vout_exp:.5f} V  err={err:.3f}%")
    assert 0.4 < vout < 0.95, f"diode clamp out of expected band: {vout:.4f} V"
    assert err < 1.0, f"diode clamp off by {err:.3f}%"
    print("  [gate 5 diode clamp] PASS")


# =============================================================================
# Gate 6 — inverting op-amp (op + ac)
# =============================================================================

def test_inverting_opamp() -> None:
    # Vin -> Rin -> (-) node ; Rf from (-) to OUT ; (+) to ground ; ideal opamp.
    Rin = 1000.0; Rf = 10000.0
    vin = 0.2
    gain_exp = -Rf / Rin  # -10
    circuit = {
        "nodes": ["0", "IN", "INV", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": vin,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RIN", "nodes": ["IN", "INV"], "value": Rin, "params": {}},
            {"kind": "R", "name": "RF", "nodes": ["INV", "OUT"], "value": Rf, "params": {}},
            # OPAMP nodes: in+, in-, out  (in+ = ground)
            {"kind": "OPAMP", "name": "X1", "nodes": ["0", "INV", "OUT"], "value": None,
             "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "op"},
                     {"type": "ac", "sweep": "dec", "points": 5, "fstart": 1, "fstop": 1000}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["op"], f"opamp op must converge; log={res['log']}"
    vout = res["op"]["V(OUT)"]
    gain_op = vout / vin
    err_op = _pct_err(gain_op, gain_exp)
    print(f"  inv opamp (op): V(OUT)={vout:.5f} V  gain={gain_op:.4f}  expected={gain_exp:.4f}  err={err_op:.3f}%")
    assert err_op < 0.5, f"op-amp DC gain off by {err_op:.3f}%"

    # AC: with AC mag=1 on V1, |V(OUT)| should equal |gain| = 10 across the band.
    mag = res["ac"]["mag"]["V(OUT)"]
    mid = mag[len(mag) // 2]
    gain_ac = mid
    err_ac = _pct_err(gain_ac, abs(gain_exp))
    print(f"  inv opamp (ac): |V(OUT)|={gain_ac:.4f}  expected={abs(gain_exp):.4f}  err={err_ac:.3f}%")
    assert err_ac < 0.5, f"op-amp AC gain off by {err_ac:.3f}%"
    print("  [gate 6 inverting op-amp] PASS")


# =============================================================================
# Gate 7 — common-emitter DC bias (Vce in active region)
# =============================================================================

def test_common_emitter_bias() -> None:
    # Classic CE: Vcc=12, Rc=2.2k, Rb=470k base resistor from Vcc, NPN.
    # Expect transistor in active region: 0.2 < Vce < Vcc.
    circuit = {
        "nodes": ["0", "VCC", "B", "C"],
        "devices": [
            {"kind": "V", "name": "VCC", "nodes": ["VCC", "0"], "value": 12.0, "params": {}},
            {"kind": "R", "name": "RB", "nodes": ["VCC", "B"], "value": 470000.0, "params": {}},
            {"kind": "R", "name": "RC", "nodes": ["VCC", "C"], "value": 2200.0, "params": {}},
            # Q nodes: C, B, E ; emitter to ground
            {"kind": "Q", "name": "Q1", "nodes": ["C", "B", "0"], "value": None,
             "model": "QN", "params": {}},
        ],
        "models": {"QN": {"type": "npn", "IS": 1e-15, "BF": 200.0, "BR": 1.0, "VAF": 100.0}},
        "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "op"}], "probes": ["V(C)", "V(B)", "I(VCC)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["op"], f"CE op must converge; log={res['log']}"
    vc = res["op"]["V(C)"]; vb = res["op"]["V(B)"]
    vce = vc  # emitter at ground
    # Closed-form estimate: Ib = (Vcc - Vbe)/Rb, Ic = BF*Ib, Vce = Vcc - Ic*Rc.
    Vbe = 0.65  # typical
    Ib = (12.0 - Vbe) / 470000.0
    Ic = 200.0 * Ib
    vce_exp = 12.0 - Ic * 2200.0
    print(f"  CE bias: V(B)={vb:.4f} V  V(C)=Vce={vce:.4f} V  closed-form Vce~={vce_exp:.4f} V")
    print(f"           Ib~={Ib*1e6:.2f} uA  Ic~={Ic*1e3:.3f} mA")
    assert 0.2 < vce < 11.8, f"transistor not in active region: Vce={vce:.4f} V"
    assert 0.5 < vb < 0.8, f"Vbe out of range: {vb:.4f} V"
    # Loose closed-form check (±25%: Vbe and BF dominate, model-dependent).
    err = _pct_err(vce, vce_exp)
    print(f"           Vce err vs first-order estimate = {err:.1f}% (<=25% tol)")
    assert err < 25.0, f"Vce far from first-order estimate: {err:.1f}%"
    print("  [gate 7 common-emitter bias] PASS")


# =============================================================================
# Gate 8 — RC low-pass AC: -3 dB corner + -20 dB/dec slope
# =============================================================================

def test_rc_lowpass_ac() -> None:
    R = 1000.0; C = 159.155e-9  # fc = 1/(2*pi*R*C) ~ 1000 Hz
    fc = 1.0 / (2 * math.pi * R * C)
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": R, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": C, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "ac", "sweep": "dec", "points": 20, "fstart": 10, "fstop": 1e6}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["ac"], "ac must converge"
    f = np.array(res["ac"]["x_f"]); mag = np.array(res["ac"]["mag"]["V(OUT)"])
    db = 20 * np.log10(mag)

    # -3 dB corner: magnitude at fc should be 1/sqrt(2) = -3.01 dB.
    mag_fc = float(np.interp(fc, f, mag))
    db_fc = 20 * math.log10(mag_fc)
    print(f"  RC LP: fc={fc:.2f} Hz  |H(fc)|={mag_fc:.5f} (exp {1/math.sqrt(2):.5f})  {db_fc:.3f} dB (exp -3.01)")
    assert abs(db_fc - (-3.0103)) < 0.2, f"-3 dB corner off: {db_fc:.3f} dB"
    assert _pct_err(mag_fc, 1 / math.sqrt(2)) < 1.0, "corner magnitude off >1%"

    # -20 dB/dec slope well above fc: compare 10*fc and 100*fc.
    f1, f2 = 10 * fc, 100 * fc
    if f2 <= f[-1]:
        d1 = float(np.interp(f1, f, db)); d2 = float(np.interp(f2, f, db))
        slope = (d2 - d1)  # over one decade
        print(f"  RC LP: slope {f1:.0f}->{f2:.0f} Hz = {slope:.3f} dB/dec (exp -20)")
        assert abs(slope - (-20.0)) < 1.0, f"high-freq slope off: {slope:.3f} dB/dec"
    print("  [gate 8 RC low-pass AC] PASS")


# =============================================================================
# bonus — DC sweep smoke (divider swept) — exercises run_dc_sweep
# =============================================================================

def test_dc_sweep() -> None:
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {"analyses": [{"type": "dc", "source": "V1", "start": 0, "stop": 10, "step": 1.0}],
             "probes": ["V(OUT)"]}
    res = simulate(circuit, study)
    assert res["convergence"]["dc"], "dc sweep must converge"
    x = res["dc"]["x"]; vout = res["dc"]["traces"]["V(OUT)"]
    # Half divider: V(OUT) = Vin/2.
    for xi, vo in zip(x, vout):
        assert _pct_err(vo, xi / 2.0) < 0.01 or xi == 0, f"dc sweep wrong at Vin={xi}: {vo}"
    print(f"  dc sweep: V(OUT) at Vin=10 -> {vout[-1]:.4f} (exp 5.0)")
    print("  [bonus dc sweep] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("ECAD CIRCUIT SIMULATOR - ACCURACY GATES (sim.md sec.5)")
    print("=" * 64)

    gates = [
        ("Gate 1: resistive divider", test_resistive_divider),
        ("Gate 2: RC step", test_rc_step),
        ("Gate 3: RL step", test_rl_step),
        ("Gate 4: series-RLC ring", test_rlc_ring),
        ("Gate 5: diode clamp", test_diode_clamp),
        ("Gate 6: inverting op-amp", test_inverting_opamp),
        ("Gate 7: common-emitter bias", test_common_emitter_bias),
        ("Gate 8: RC low-pass AC", test_rc_lowpass_ac),
        ("Bonus: DC sweep", test_dc_sweep),
    ]
    for title, fn in gates:
        print(f"\n--- {title} ---")
        fn()

    print(f"\nALL CIRCUIT GATES PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
