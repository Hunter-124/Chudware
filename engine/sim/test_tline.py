"""Lossless transmission-line device ``T`` gates (signal-integrity simulation).

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
A source (Rs) -> T-line (Z0, TD) -> load. Asserts the textbook reflection physics against the
closed-form launch/reflection amplitudes and the 2*TD round-trip timing.

Model under test (see engine/sim/devices.py ``TLine``): Branin method of characteristics for the
transient (ideal lossless, linear history interpolation) + the exact lossless two-port for AC.

Run:  & "<venv>/python.exe" -m engine.sim.test_tline

Gates:
  1. Matched load (RL=Z0): clean half-amplitude launch (Rs=Z0), settled, NO reflection at 2*TD.
  2. Open load: positive reflection arriving at ~2*TD (input doubles toward the full source step).
  3. Short load: negative reflection arriving at ~2*TD (input dips back toward 0).
  4. AC: a line terminated in Z0 presents a flat input impedance == Z0 (no resonance).
  5. AC: a quarter-wave open stub transforms an open to a short at f = 1/(4*TD).
"""
from __future__ import annotations

import math
import time

import numpy as np

from engine.sim.circuit import simulate


def _wf(kind: str, **kw) -> dict:
    d = {"type": kind}
    d.update(kw)
    return d


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


def _tline_step_circuit(rl: float, z0: float, td: float, rs: float, vstep: float) -> dict:
    """Vs --Rs-- IN --[T-line A|B]-- OUT --RL-- gnd. Port returns to ground."""
    return {
        "nodes": ["0", "SRC", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["SRC", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=vstep, td=0.0,
                                         tr=td / 50.0, tf=td / 50.0, pw=100 * td, per=0.0)}},
            {"kind": "R", "name": "RS", "nodes": ["SRC", "IN"], "value": rs, "params": {}},
            # T-line: ports (IN,0) and (OUT,0).
            {"kind": "T", "name": "T1", "nodes": ["IN", "0", "OUT", "0"], "value": None,
             "params": {"z0": z0, "td": td}},
            {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": rl, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }


# =============================================================================
# Gate 1 — matched load: clean half-amplitude launch, no reflection
# =============================================================================

def test_matched_no_reflection() -> None:
    z0 = 50.0; td = 1e-9; rs = 50.0; vstep = 2.0
    c = _tline_step_circuit(rl=z0, z0=z0, td=td, rs=rs, vstep=vstep)
    study = {"analyses": [{"type": "tran", "tstep": td / 40.0, "tstop": 6 * td, "uic": True}],
             "probes": ["V(IN)", "V(OUT)"]}
    res = simulate(c, study)
    assert res["convergence"]["tran"], f"tran must converge; log={res['log']}"
    t = np.array(res["tran"]["x_t"])
    vin = np.array(res["tran"]["traces"]["V(IN)"])
    vout = np.array(res["tran"]["traces"]["V(OUT)"])

    # Launch into the line: divider Rs/(Rs+Z0) -> Vstep*Z0/(Rs+Z0) = half for Rs=Z0.
    launch = vstep * z0 / (rs + z0)
    vin_mid = float(np.interp(0.5 * td, t, vin))  # well after the edge, before any round trip
    print(f"  matched: launch V(IN) @0.5TD = {vin_mid:.4f} V (exp {launch:.4f}, half-amplitude)")
    assert _pct_err(vin_mid, launch) < 5.0, "launch must be half-amplitude into a matched source"

    # Matched load -> NO reflection: V(IN) stays at the launch level past 2*TD (no step change).
    vin_before = float(np.interp(1.7 * td, t, vin))
    vin_after = float(np.interp(2.5 * td, t, vin))
    print(f"  matched: V(IN) @1.7TD={vin_before:.4f}  @2.5TD={vin_after:.4f}  (no reflection step)")
    assert abs(vin_after - vin_before) < 0.05 * vstep, "matched load must produce no reflection"

    # OUT settles to the launch level (full wave delivered, no reflection) after one TD.
    vout_final = float(np.interp(2.5 * td, t, vout))
    print(f"  matched: V(OUT) settled = {vout_final:.4f} V (exp {launch:.4f})")
    assert _pct_err(vout_final, launch) < 5.0, "matched OUT must equal the launched wave"
    print("  [gate 1 matched -> no reflection] PASS")


# =============================================================================
# Gate 2 — open load: positive reflection at ~2*TD
# =============================================================================

def test_open_positive_reflection() -> None:
    z0 = 50.0; td = 1e-9; rs = 50.0; vstep = 2.0
    rl = 1e9  # ~open
    c = _tline_step_circuit(rl=rl, z0=z0, td=td, rs=rs, vstep=vstep)
    study = {"analyses": [{"type": "tran", "tstep": td / 60.0, "tstop": 6 * td, "uic": True}],
             "probes": ["V(IN)", "V(OUT)"]}
    res = simulate(c, study)
    assert res["convergence"]["tran"], f"tran must converge; log={res['log']}"
    t = np.array(res["tran"]["x_t"])
    vin = np.array(res["tran"]["traces"]["V(IN)"])

    launch = vstep * z0 / (rs + z0)        # 1.0 V launched
    # Open reflection coefficient at load = +1: the reflected wave doubles V at the open end,
    # and V(IN) steps UP by the launch amount when the reflection returns at 2*TD.
    vin_before = float(np.interp(1.5 * td, t, vin))
    vin_after = float(np.interp(2.5 * td, t, vin))
    print(f"  open: launch={launch:.4f} V  V(IN) @1.5TD={vin_before:.4f}  @2.5TD={vin_after:.4f}")
    # Before the round trip V(IN)=launch; after, V(IN) rises toward the full source step (Rs match
    # absorbs the returning wave -> final = Vstep since no DC current into an open).
    assert _pct_err(vin_before, launch) < 6.0, "pre-reflection level must be the launch voltage"
    assert vin_after > vin_before + 0.4 * launch, "open must give a POSITIVE reflection step"
    assert _pct_err(vin_after, vstep) < 6.0, "after the +reflection V(IN) -> full step (open DC)"

    # Reflection arrival time ~ 2*TD: find where V(IN) crosses the midpoint of the up-step.
    target = 0.5 * (vin_before + vin_after)
    t_arr = _crossing_time(t, vin, target, after=1.2 * td)
    err_t = _pct_err(t_arr, 2.0 * td)
    print(f"  open: reflection arrival t={t_arr*1e9:.3f} ns (exp {2*td*1e9:.3f} ns)  err={err_t:.2f}%")
    assert err_t < 10.0, f"open reflection must arrive at ~2*TD ({err_t:.2f}%)"
    print("  [gate 2 open -> +reflection @2TD] PASS")


# =============================================================================
# Gate 3 — short load: negative reflection at ~2*TD
# =============================================================================

def test_short_negative_reflection() -> None:
    z0 = 50.0; td = 1e-9; rs = 50.0; vstep = 2.0
    rl = 1e-6  # ~short
    c = _tline_step_circuit(rl=rl, z0=z0, td=td, rs=rs, vstep=vstep)
    study = {"analyses": [{"type": "tran", "tstep": td / 60.0, "tstop": 6 * td, "uic": True}],
             "probes": ["V(IN)", "V(OUT)"]}
    res = simulate(c, study)
    assert res["convergence"]["tran"], f"tran must converge; log={res['log']}"
    t = np.array(res["tran"]["x_t"])
    vin = np.array(res["tran"]["traces"]["V(IN)"])

    launch = vstep * z0 / (rs + z0)
    vin_before = float(np.interp(1.5 * td, t, vin))
    vin_after = float(np.interp(2.5 * td, t, vin))
    print(f"  short: launch={launch:.4f} V  V(IN) @1.5TD={vin_before:.4f}  @2.5TD={vin_after:.4f}")
    assert _pct_err(vin_before, launch) < 6.0, "pre-reflection level must be the launch voltage"
    # Short reflection coefficient = -1: the returning wave pulls V(IN) DOWN; with Rs=Z0 the
    # absorbed reflection drives V(IN) to ~0 (a shorted line draws Vstep/Rs, V(IN)=0).
    assert vin_after < vin_before - 0.4 * launch, "short must give a NEGATIVE reflection step"
    assert abs(vin_after) < 0.15 * vstep, "after the -reflection V(IN) -> ~0 (shorted line)"

    target = 0.5 * (vin_before + vin_after)
    t_arr = _crossing_time(t, vin, target, after=1.2 * td)
    err_t = _pct_err(t_arr, 2.0 * td)
    print(f"  short: reflection arrival t={t_arr*1e9:.3f} ns (exp {2*td*1e9:.3f} ns)  err={err_t:.2f}%")
    assert err_t < 10.0, f"short reflection must arrive at ~2*TD ({err_t:.2f}%)"
    print("  [gate 3 short -> -reflection @2TD] PASS")


def _crossing_time(t: np.ndarray, y: np.ndarray, level: float, after: float) -> float:
    """First time after ``after`` that ``y`` crosses ``level`` (linear interpolation)."""
    for k in range(len(t) - 1):
        if t[k] < after:
            continue
        y0, y1 = y[k], y[k + 1]
        if (y0 - level) * (y1 - level) <= 0 and y1 != y0:
            return t[k] + (level - y0) * (t[k + 1] - t[k]) / (y1 - y0)
    return float(t[-1])


# =============================================================================
# Gate 4 — AC: matched line presents a flat input impedance == Z0
# =============================================================================

def test_ac_matched_flat_zin() -> None:
    z0 = 50.0; td = 1e-9
    # Drive node IN with a 1 A AC current source; Zin(IN) = V(IN)/1A. A line terminated in Z0
    # has input impedance Z0 at every frequency (no resonance).
    c = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "I", "name": "I1", "nodes": ["0", "IN"], "value": 0.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "T", "name": "T1", "nodes": ["IN", "0", "OUT", "0"], "value": None,
             "params": {"z0": z0, "td": td}},
            {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": z0, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    # Sweep across several quarter-wave multiples (f from below to above 1/(4TD)=250 MHz).
    study = {"analyses": [{"type": "ac", "sweep": "lin", "points": 40,
                           "fstart": 10e6, "fstop": 1e9}],
             "probes": ["V(IN)"]}
    res = simulate(c, study)
    assert res["convergence"]["ac"], f"ac must converge; log={res['log']}"
    f = np.array(res["ac"]["x_f"])
    zin = np.array(res["ac"]["mag"]["V(IN)"])  # |V/I| = |Zin|
    zmin, zmax = float(zin.min()), float(zin.max())
    print(f"  AC matched: |Zin| over 10MHz-1GHz: min={zmin:.3f} max={zmax:.3f} ohm (exp flat {z0})")
    assert _pct_err(zmin, z0) < 2.0 and _pct_err(zmax, z0) < 2.0, "matched line Zin must be flat=Z0"
    print("  [gate 4 AC matched -> flat Zin=Z0] PASS")


# =============================================================================
# Gate 5 — AC: quarter-wave open stub transforms to a short at f=1/(4*TD)
# =============================================================================

def test_ac_quarter_wave_transform() -> None:
    z0 = 50.0; td = 1e-9
    f_qw = 1.0 / (4.0 * td)  # 250 MHz: line is a quarter wavelength long
    # Open-terminated stub: far end open. Zin = -j*Z0*cot(beta*l). At a quarter wave Zin -> 0
    # (open transforms to a short). Drive with 1 A AC, read |V(IN)| = |Zin|.
    c = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "I", "name": "I1", "nodes": ["0", "IN"], "value": 0.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "T", "name": "T1", "nodes": ["IN", "0", "OUT", "0"], "value": None,
             "params": {"z0": z0, "td": td}},
            {"kind": "R", "name": "ROPEN", "nodes": ["OUT", "0"], "value": 1e9, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }
    # Fine linear sweep straddling f_qw so we can find the impedance minimum (the short).
    study = {"analyses": [{"type": "ac", "sweep": "lin", "points": 201,
                           "fstart": 50e6, "fstop": 450e6}],
             "probes": ["V(IN)"]}
    res = simulate(c, study)
    assert res["convergence"]["ac"], f"ac must converge; log={res['log']}"
    f = np.array(res["ac"]["x_f"])
    zin = np.array(res["ac"]["mag"]["V(IN)"])

    # Quarter-wave: |Zin| should dip to a deep minimum (open -> short) at f_qw.
    k_min = int(np.argmin(zin))
    f_at_min = float(f[k_min])
    print(f"  AC qw stub: |Zin| min={zin[k_min]:.3f} ohm at f={f_at_min/1e6:.2f} MHz "
          f"(exp short at {f_qw/1e6:.2f} MHz)")
    # Compare to a low-frequency point where the open stub still looks ~open (high |Zin|).
    zin_low = float(zin[0])
    print(f"  AC qw stub: |Zin| @ {f[0]/1e6:.0f}MHz = {zin_low:.2f} ohm (open, should be >> Z0)")
    assert _pct_err(f_at_min, f_qw) < 5.0, "quarter-wave short must land at f=1/(4*TD)"
    assert zin[k_min] < 0.05 * zin_low, "open stub must transform to a near-short at the qw freq"
    assert zin[k_min] < z0, "qw-transformed impedance must be well below Z0 (a short)"
    print("  [gate 5 AC quarter-wave transform] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("SIM TRANSMISSION-LINE (T) - REFLECTION + QUARTER-WAVE GATES")
    print("=" * 64)
    gates = [
        ("Gate 1: matched -> no reflection", test_matched_no_reflection),
        ("Gate 2: open -> +reflection @2TD", test_open_positive_reflection),
        ("Gate 3: short -> -reflection @2TD", test_short_negative_reflection),
        ("Gate 4: AC matched -> flat Zin", test_ac_matched_flat_zin),
        ("Gate 5: AC quarter-wave transform", test_ac_quarter_wave_transform),
    ]
    for title, fn in gates:
        print(f"\n--- {title} ---")
        fn()
    print(f"\nALL TLINE GATES PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
