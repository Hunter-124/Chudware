"""Depth tests (wave 2) for the native simulator: the "better-than-ngspice" extended analyses
added in ``analysis_ext.py`` and wired into ``circuit.simulate``:

  1. Fourier / THD  (study type "four")    - pure sine ~0% THD; half-wave-rectified sine ~ analytic.
  2. Monte-Carlo    (study type "mc")       - 5% divider, 500 runs: mean ~ nominal, std ~ tol-propagated,
                                              deterministic across two equal-seed runs.
  3. Temperature    (study type "temp")     - diode Vf falls ~2 mV/degC with temperature.
  4. Basic noise    (study type "noise")    - RC low-pass: density ~ sqrt(4kTR) at low freq, rolls off
                                              above fc; integrated noise -> sqrt(kT/C), positive+finite.

These are additive to the accuracy gates in test_circuit.py / test_spice.py / test_simdepth.py
(which must stay green). Each test builds inputs inline and asserts against closed form / qualitative
trends.

Run:  & ".../venv/Scripts/python.exe" -m engine.sim.test_simdepth2
"""
from __future__ import annotations

import math
import time

import numpy as np

from engine.sim.analysis_ext import run_fourier
from engine.sim.circuit import simulate

_K = 1.380649e-23  # Boltzmann (J/K)


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


# =============================================================================
# 1 - Fourier / THD
# =============================================================================

def test_thd_pure_sine() -> None:
    # Synthesize a pure 1 kHz sine trace and feed it directly to run_fourier -> THD ~ 0.
    f0 = 1000.0
    n = 4000
    t = np.linspace(0.0, 8.0 / f0, n)  # 8 periods
    y = 1.5 * np.sin(2 * math.pi * f0 * t + 0.3)
    tran = {"x_t": list(t), "traces": {"V(OUT)": list(y)}}
    log: list = []
    four = run_fourier(tran, {"f0": f0, "probe": "V(OUT)", "nharm": 9}, log)
    assert four is not None, f"four returned None; log={log}"
    h1 = four["harmonics"][0]
    print(f"  pure sine: H1 mag={h1[1]:.5f} (exp ~1.5)  THD={four['thd_pct']:.5f}%")
    assert _pct_err(h1[1], 1.5) < 1.0, f"fundamental amplitude off: {h1[1]}"
    assert four["thd_pct"] < 1.0, f"pure sine THD should be ~0, got {four['thd_pct']:.4f}%"
    print("  [1a pure-sine THD ~0] PASS")


def test_thd_half_wave_rectified() -> None:
    # Half-wave-rectified unit sine: f(t)=max(sin,0). Analytic single-sided harmonics:
    #   H1=1/2; even Hn = 2/(pi*(n^2-1)) (n=2,4,6,8); odd Hn (n>=3) = 0.
    # THD (up to H9) ~ 43.5%.
    f0 = 1000.0
    n = 8000
    t = np.linspace(0.0, 10.0 / f0, n)  # 10 periods, exclude endpoint handled by run_fourier window
    y = np.maximum(np.sin(2 * math.pi * f0 * t), 0.0)
    tran = {"x_t": list(t), "traces": {"V(OUT)": list(y)}}
    log: list = []
    four = run_fourier(tran, {"f0": f0, "probe": "V(OUT)", "nharm": 9}, log)
    assert four is not None, f"four returned None; log={log}"

    harm = {n_: (m, p) for (n_, m, p) in four["harmonics"]}
    h1 = harm[1][0]
    h2 = harm.get(2, (0.0,))[0]
    h3 = harm.get(3, (0.0,))[0]
    # Analytic expectations (single-sided amplitudes).
    h1_exp = 0.5
    h2_exp = 2.0 / (math.pi * (4 - 1))   # = 2/(pi*3) ~ 0.2122
    thd_exp = 43.5
    print(f"  half-wave rect: H1={h1:.4f} (exp {h1_exp:.4f})  H2={h2:.4f} (exp {h2_exp:.4f})  "
          f"H3={h3:.5f} (exp ~0)")
    print(f"                  THD={four['thd_pct']:.3f}% (analytic ~{thd_exp:.1f}%)  DC={four['dc']:.4f} (exp {1/math.pi:.4f})")
    assert _pct_err(h1, h1_exp) < 2.0, f"H1 off: {h1}"
    assert _pct_err(h2, h2_exp) < 3.0, f"H2 off: {h2}"
    assert h3 < 0.01, f"odd harmonic H3 should be ~0, got {h3}"
    assert _pct_err(four["thd_pct"], thd_exp) < 5.0, f"THD off: {four['thd_pct']:.3f}% vs ~{thd_exp}%"
    print("  [1b half-wave-rectified THD ~ analytic] PASS")


def test_thd_via_simulate_clipped() -> None:
    # End-to-end through simulate(): a sine driven into a diode clamp produces harmonic distortion.
    # SIN source 1 kHz, 2 V amplitude through 1k into a diode-to-ground clamp -> clipped on top.
    f0 = 1000.0
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": {"type": "SIN", "vo": 0.0, "va": 2.0, "f": f0, "td": 0.0}}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "D", "name": "D1", "nodes": ["OUT", "0"], "value": None,
             "model": "D1N4148", "params": {}},
        ],
        "models": {"D1N4148": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}},
        "params": {"TEMP": 27},
    }
    study = {
        "analyses": [
            {"type": "tran", "tstep": 1.0 / (f0 * 400), "tstop": 10.0 / f0, "uic": False},
            {"type": "four", "f0": f0, "probe": "V(OUT)", "nharm": 9},
        ],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"].get("four"), f"four must converge; log={res['log'][-3:]}"
    four = res["four"]
    print(f"  clipped sine via simulate: THD={four['thd_pct']:.3f}%  H1={four['harmonics'][0][1]:.4f}")
    # The diode clamps the positive half -> substantial distortion (well above a clean sine).
    assert four["thd_pct"] > 5.0, f"clipped sine should show distortion, THD={four['thd_pct']:.3f}%"
    print("  [1c clipped-sine THD via simulate] PASS")


# =============================================================================
# 2 - Monte-Carlo tolerance
# =============================================================================

def _divider_circuit(r1: float = 1000.0, r2: float = 1000.0, vin: float = 10.0) -> dict:
    return {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": vin, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": r1, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": r2, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }


def test_monte_carlo_divider() -> None:
    # 10V divider, R1=R2=1k (nominal V(OUT)=5V), 5% resistors, 500 runs.
    nominal = 5.0
    study = {
        "analyses": [{
            "type": "mc", "runs": 500, "tol": {"R": 0.05},
            "analysis": {"type": "op"}, "probe": "V(OUT)",
        }],
        "probes": ["V(OUT)"],
        "seed": 7,
    }
    res = simulate(_divider_circuit(), study)
    assert res["convergence"]["mc"], f"mc must run; log={res['log'][-3:]}"
    mc = res["mc"]
    print(f"  MC divider (5% R, {mc['runs']} runs): mean={mc['mean']:.5f} V (nominal {nominal})  "
          f"std={mc['std']:.5f} V  min={mc['min']:.4f} max={mc['max']:.4f}")
    # Mean within ~0.5% of nominal (gaussian, symmetric divider).
    assert _pct_err(mc["mean"], nominal) < 0.6, f"MC mean off nominal: {mc['mean']:.5f}"
    # First-order tolerance propagation for Vout = Vin*R2/(R1+R2) with R1=R2=R:
    #   dVout/dR1 = -Vin/(4R), dVout/dR2 = +Vin/(4R); each resistor sigma_R = 0.05*R, so each term
    #   contributes Vin*0.05/4 = 0.125 V and (independent) sigma_Vout = (Vin*0.05/4)*sqrt(2) ~ 0.177 V.
    vin = 10.0
    sigma_pred = (vin * 0.05 / 4.0) * math.sqrt(2.0)
    print(f"                 predicted 1st-order sigma ~ {sigma_pred:.5f} V")
    assert 0.7 * sigma_pred < mc["std"] < 1.3 * sigma_pred, \
        f"MC std {mc['std']:.5f} inconsistent with ~5% propagation ({sigma_pred:.5f})"
    print("  [2a MC mean/std consistent with 5% tolerance] PASS")


def test_monte_carlo_deterministic() -> None:
    # Same seed -> bit-identical value lists across two independent simulate() calls.
    def run():
        study = {
            "analyses": [{
                "type": "mc", "runs": 64, "tol": {"R": 0.05},
                "analysis": {"type": "op"}, "probe": "V(OUT)", "seed": 99,
            }],
            "probes": ["V(OUT)"],
        }
        return simulate(_divider_circuit(), study)["mc"]["values"]

    a = run()
    b = run()
    assert a == b, "MC with the same seed must be deterministic (value lists differ)"
    print(f"  MC determinism: {len(a)} values identical across two seeded runs "
          f"(first={a[0]:.6f})")
    print("  [2b MC deterministic across equal seed] PASS")


# =============================================================================
# 3 - Temperature sweep (diode Vf tempco)
# =============================================================================

def test_temp_diode_vf() -> None:
    # Diode biased by a stiff supply through a large series R (10V / 10k -> ~1 mA, nearly constant
    # vs T). Read the diode forward drop V(A); sweep -40 / 25 / 85 C. For silicon at roughly fixed
    # current, Vf DECREASES with temperature (~ -2 mV/degC). (A current source straight into a diode
    # is a classic DC-path-less convergence trap, so we provide a real DC path via R.)
    circuit = {
        "nodes": ["0", "VS", "A"],
        "devices": [
            {"kind": "V", "name": "VS", "nodes": ["VS", "0"], "value": 10.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["VS", "A"], "value": 10000.0, "params": {}},
            {"kind": "D", "name": "D1", "nodes": ["A", "0"], "value": None,
             "model": "DSI", "params": {}},
        ],
        "models": {"DSI": {"IS": 1e-14, "N": 1.0}},
        "params": {"TEMP": 27},
    }
    temps = [-40.0, 25.0, 85.0]
    study = {
        "analyses": [{"type": "temp", "values": temps, "analysis": {"type": "op"}}],
        "probes": ["V(A)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["temp"], f"temp sweep must run; log={res['log'][-3:]}"
    pts = res["temp"]["points"]
    vf = [p["op"]["V(A)"] for p in pts]
    print("  temp diode Vf @ ~1 mA (10V/10k bias):")
    for tc, v in zip(temps, vf):
        print(f"    T={tc:+6.1f} C  Vf={v:.5f} V  (I~={(10.0 - v) / 10000.0 * 1e3:.4f} mA)")
    # Monotonic decrease with temperature.
    assert vf[0] > vf[1] > vf[2], f"Vf must fall with temperature: {vf}"
    # Slope between 25 C and 85 C, mV/degC.
    slope = (vf[2] - vf[1]) / (temps[2] - temps[1]) * 1000.0
    print(f"    dVf/dT (25->85 C) = {slope:.3f} mV/degC  (expected ~ -2 mV/degC)")
    assert -3.0 < slope < -1.0, f"Vf tempco out of band: {slope:.3f} mV/degC"
    print("  [3 temperature diode Vf trend] PASS")


def test_temp_resistor_tempco() -> None:
    # Divider where R2 has a +1000 ppm/degC tempco -> V(OUT) rises with temperature.
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 1000.0,
             "params": {"tc1": 1e-3}},  # +0.1%/degC
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    temps = [0.0, 27.0, 100.0]
    study = {
        "analyses": [{"type": "temp", "values": temps, "analysis": {"type": "op"}}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["temp"], f"temp sweep must run; log={res['log'][-3:]}"
    vouts = [p["op"]["V(OUT)"] for p in res["temp"]["points"]]
    print(f"  temp resistor tc1: V(OUT) at {temps} C -> {[round(v,5) for v in vouts]}")
    # At 27 C (Tnom) V(OUT) is exactly 5.0; rising R2 with T raises V(OUT).
    assert _pct_err(vouts[1], 5.0) < 0.01, f"V(OUT) at Tnom should be 5.0, got {vouts[1]}"
    assert vouts[0] < vouts[1] < vouts[2], f"V(OUT) should rise with T (tc1>0): {vouts}"
    print("  [3b resistor tc1 tempco] PASS")


# =============================================================================
# 4 - Basic noise (resistor thermal)
# =============================================================================

def test_noise_rc_lowpass() -> None:
    # RC low-pass: R=1k to OUT, C=159.155 nF from OUT to ground -> fc ~ 1 kHz.
    # Output thermal-noise density at low freq -> sqrt(4kTR); rolls off above fc; integrated -> sqrt(kT/C).
    R = 1000.0
    C = 159.155e-9
    fc = 1.0 / (2 * math.pi * R * C)
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            # The independent V source is an AC short -> R noise refers to OUT through R||C.
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": R, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": C, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "noise", "output": "V(OUT)", "source": "V1",
                      "sweep": "dec", "points": 40, "fstart": 1.0, "fstop": 1e7}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["noise"], f"noise must converge; log={res['log'][-3:]}"
    noise = res["noise"]
    f = np.array(noise["x_f"])
    dens = np.array(noise["onoise_spectrum"])  # V/sqrt(Hz)

    T = 27.0 + 273.15
    dens_lf_exp = math.sqrt(4 * _K * T * R)        # sqrt(4kTR) at DC
    dens_lf = float(dens[0])                       # first (lowest) freq point
    print(f"  noise RC LP: fc={fc:.1f} Hz")
    print(f"    low-f density = {dens_lf:.4e} V/sqrtHz  expected sqrt(4kTR) = {dens_lf_exp:.4e}  "
          f"err={_pct_err(dens_lf, dens_lf_exp):.3f}%")
    assert _pct_err(dens_lf, dens_lf_exp) < 2.0, \
        f"low-f noise density off: {dens_lf:.4e} vs {dens_lf_exp:.4e}"

    # Roll-off: density at 10*fc should be well below the low-freq floor (single-pole -> ~1/10).
    d_hi = float(np.interp(10 * fc, f, dens))
    print(f"    density @10*fc = {d_hi:.4e}  (ratio to LF = {d_hi / dens_lf:.3f}, expect <~0.2)")
    assert d_hi < 0.3 * dens_lf, f"noise did not roll off above fc: {d_hi:.4e} vs LF {dens_lf:.4e}"

    # Integrated RMS noise: the classic kT/C result, sqrt(kT/C), independent of R.
    ktc = math.sqrt(_K * T / C)
    rms = noise["total_noise_rms"]
    print(f"    integrated RMS = {rms:.4e} Vrms  sqrt(kT/C) = {ktc:.4e}  err={_pct_err(rms, ktc):.2f}%")
    assert rms > 0.0 and math.isfinite(rms), "integrated noise must be positive + finite"
    # Band is finite (1 Hz..10 MHz) so we capture most but not 100%; allow a generous band.
    assert _pct_err(rms, ktc) < 10.0, f"integrated noise off kT/C: {rms:.4e} vs {ktc:.4e}"
    # Per-resistor breakdown present.
    assert "R1" in noise["per_resistor"], f"per-resistor noise missing: {noise['per_resistor']}"
    per = {k: round(v, 12) for k, v in noise["per_resistor"].items()}
    print(f"    per-resistor RMS: {per}")
    print("  [4 RC low-pass thermal noise] PASS")


def test_noise_divider_floor() -> None:
    # Pure resistive divider (no reactive roll-off): output noise is flat (white). Two resistors in
    # parallel from OUT to ground (R1 to the shorted source, R2 to ground) -> Req = R1||R2.
    # Output density = sqrt(4kT*(R1||R2)) at all freqs.
    R1 = 1000.0
    R2 = 3000.0
    circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": R1, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": R2, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    study = {
        "analyses": [{"type": "noise", "output": "V(OUT)", "sweep": "dec",
                      "points": 10, "fstart": 1.0, "fstop": 1e5}],
        "probes": ["V(OUT)"],
    }
    res = simulate(circuit, study)
    assert res["convergence"]["noise"], f"noise must converge; log={res['log'][-3:]}"
    dens = np.array(res["noise"]["onoise_spectrum"])
    T = 27.0 + 273.15
    req = R1 * R2 / (R1 + R2)
    exp = math.sqrt(4 * _K * T * req)
    print(f"  noise divider floor: R1||R2={req:.1f} ohm  density={dens[0]:.4e} V/sqrtHz  "
          f"expected sqrt(4kT*Req)={exp:.4e}  err={_pct_err(float(dens[0]), exp):.3f}%")
    # White: flat across the band.
    assert dens.std() / dens.mean() < 0.01, f"resistive divider noise should be white: {dens}"
    assert _pct_err(float(dens[0]), exp) < 2.0, f"divider noise floor off: {dens[0]:.4e} vs {exp:.4e}"
    print("  [4b resistive-divider white-noise floor] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("ECAD SIM DEPTH 2 - Fourier/THD / Monte-Carlo / Temp / Noise")
    print("=" * 64)

    tests = [
        ("1a: pure-sine THD ~0", test_thd_pure_sine),
        ("1b: half-wave-rect THD ~ analytic", test_thd_half_wave_rectified),
        ("1c: clipped-sine THD via simulate", test_thd_via_simulate_clipped),
        ("2a: Monte-Carlo mean/std", test_monte_carlo_divider),
        ("2b: Monte-Carlo deterministic", test_monte_carlo_deterministic),
        ("3: temperature diode Vf trend", test_temp_diode_vf),
        ("3b: resistor tc1 tempco", test_temp_resistor_tempco),
        ("4: RC low-pass thermal noise", test_noise_rc_lowpass),
        ("4b: resistive-divider noise floor", test_noise_divider_floor),
    ]
    for title, fn in tests:
        print(f"\n--- {title} ---")
        fn()

    print(f"\nALL SIM-DEPTH-2 TESTS PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
