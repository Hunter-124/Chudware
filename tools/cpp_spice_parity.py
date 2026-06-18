"""cpp_spice_parity.py — Python-vs-C++ CIRCUIT (MNA) parity, linear DC first.

Drives the SAME Circuit dict + study through the Python oracle
(engine.sim.circuit.simulate) and the C++ core (libaicad.dll: sim_spice), and compares
the DC operating-point probe values. Linear systems solve to machine precision, so the
gate is tight (1e-9 relative).

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_spice_parity.py

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[spice-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[spice-parity] libaicad ABI={core.abi()}")

    from engine.sim.circuit import simulate

    cases = [
        ("resistive divider", {
            "nodes": ["IN", "OUT", "0"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}},
            ]},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)", "I(R1)", "V(IN,OUT)"]}),
        ("current source + R", {
            "nodes": ["A", "0"],
            "devices": [
                {"kind": "I", "name": "I1", "nodes": ["0", "A"], "value": 0.002, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["A", "0"], "value": 5000.0, "params": {}},
            ]},
         {"analyses": [{"type": "op"}], "probes": ["V(A)", "I(R1)"]}),
        ("ladder 3R", {
            "nodes": ["IN", "M", "OUT", "0"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 12.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "M"], "value": 1000.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["M", "OUT"], "value": 2000.0, "params": {}},
                {"kind": "R", "name": "R3", "nodes": ["OUT", "0"], "value": 3000.0, "params": {}},
            ]},
         {"analyses": [{"type": "op"}], "probes": ["V(M)", "V(OUT)", "I(V1)"]}),
        ("VCVS x5", {
            "nodes": ["CIN", "OUT", "0"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["CIN", "0"], "value": 1.5, "params": {}},
                {"kind": "E", "name": "E1", "nodes": ["OUT", "0", "CIN", "0"], "value": 5.0, "params": {}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
            ]},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(E1)"]}),
        ("VCCS gm into R", {
            "nodes": ["CIN", "OUT", "0"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["CIN", "0"], "value": 2.0, "params": {}},
                {"kind": "G", "name": "G1", "nodes": ["0", "OUT", "CIN", "0"], "value": 0.001, "params": {}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 2200.0, "params": {}},
            ]},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]}),
        ("inductor short at DC", {
            "nodes": ["IN", "OUT", "0"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 100.0, "params": {}},
                {"kind": "L", "name": "L1", "nodes": ["OUT", "0"], "value": 1e-3, "params": {}},
            ]},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(L1)"]}),
    ]

    mism = 0
    checks = 0
    for label, circuit, study in cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro.get("convergence", {}).get("op") and rcpp.get("convergence", {}).get("op")):
            diffs.append(f"convergence op py={ro.get('convergence')} cpp={rcpp.get('convergence')}")
        po, co = ro.get("op", {}), rcpp.get("op", {})
        for pr in study["probes"]:
            pv = po.get(pr)
            cv = co.get(pr)
            if pv is None or cv is None:
                diffs.append(f"{pr} missing py={pv} cpp={cv}")
                continue
            rel = abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9)
            if rel > 1e-9:
                diffs.append(f"{pr} {pv:.9g}!={cv:.9g} (rel {rel:.1e})")
        if diffs:
            print(f"  {label:<24} DIFF: {'; '.join(diffs)[:110]}")
            mism += 1
        else:
            shown = ", ".join(f"{p}={float(co[p]):.4g}" for p in study["probes"][:2])
            print(f"  {label:<24} MATCH  ({shown})")

    # ---- Transient (trapezoidal): RC step, RL step, RLC ring. Same dt schedule + BE
    # bootstrap + breakpoints on both sides -> the time grid + traces match to FP precision.
    import numpy as np

    def _wf(kind, **kw):
        d = {"type": kind}
        d.update(kw)
        return d

    tran_cases = []
    R, C = 1000.0, 1e-6
    tau = R * C
    tran_cases.append(("RC step", {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=5.0, td=0.0, tr=1e-9, tf=1e-9, pw=1.0, per=0.0)}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": R, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": C, "params": {"ic": 0.0}}]},
        {"analyses": [{"type": "tran", "tstep": tau / 200, "tstop": 5 * tau, "uic": True}],
         "probes": ["V(OUT)"]}))
    Rl, Ll = 100.0, 0.1
    taul = Ll / Rl
    tran_cases.append(("RL step", {
        "nodes": ["0", "IN", "MID"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=5.0, td=0.0, tr=1e-12, tf=1e-12, pw=1.0, per=0.0)}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "MID"], "value": Rl, "params": {}},
            {"kind": "L", "name": "L1", "nodes": ["MID", "0"], "value": Ll, "params": {"ic": 0.0}}]},
        {"analyses": [{"type": "tran", "tstep": taul / 200, "tstop": 5 * taul, "uic": True}],
         "probes": ["I(L1)"]}))
    import math
    Rr, Lr, Cr = 20.0, 1e-3, 1e-6
    period = 2 * math.pi / (1.0 / math.sqrt(Lr * Cr))
    tran_cases.append(("RLC ring", {
        "nodes": ["0", "IN", "MID", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=1.0, td=0.0, tr=1e-9, tf=1e-9, pw=1.0, per=0.0)}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "MID"], "value": Rr, "params": {}},
            {"kind": "L", "name": "L1", "nodes": ["MID", "OUT"], "value": Lr, "params": {"ic": 0.0}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": Cr, "params": {"ic": 0.0}}]},
        {"analyses": [{"type": "tran", "tstep": period / 200, "tstop": 10 * period, "uic": True}],
         "probes": ["V(OUT)"]}))

    for label, circuit, study in tran_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro["convergence"].get("tran") and rcpp.get("convergence", {}).get("tran")):
            diffs.append("tran did not converge")
        tp = np.asarray(ro["tran"]["x_t"], dtype=np.float64)
        tc = np.asarray(rcpp.get("tran", {}).get("x_t", []), dtype=np.float64)
        if tp.shape != tc.shape:
            diffs.append(f"time grid len {tp.shape}!={tc.shape}")
        elif tp.size and float(np.max(np.abs(tp - tc))) / max(float(tp[-1]), 1e-12) > 1e-9:
            diffs.append("time grid values differ")
        else:
            for pr in study["probes"]:
                vp = np.asarray(ro["tran"]["traces"][pr], dtype=np.float64)
                vc = np.asarray(rcpp["tran"]["traces"][pr], dtype=np.float64)
                scale = max(float(np.max(np.abs(vp))), 1e-9)
                dmax = float(np.max(np.abs(vp - vc)))
                if dmax / scale > 1e-6:
                    diffs.append(f"{pr} trace max|d|={dmax:.2e} (rel {dmax/scale:.1e})")
        if diffs:
            print(f"  {('tran ' + label):<24} DIFF: {'; '.join(diffs)[:100]}")
            mism += 1
        else:
            pr0 = study["probes"][0]
            fin = float(np.asarray(rcpp["tran"]["traces"][pr0])[-1])
            print(f"  {('tran ' + label):<24} MATCH  ({len(tc)} pts, {pr0}[-1]={fin:.4g})")

    # ---- AC sweep (complex MNA): RC low-pass + RLC band response. Magnitude + phase across
    # the whole frequency grid match the Python oracle to FP precision (same complex solve).
    ac_cases = [
        ("RC low-pass", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
                 "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": 1e-6, "params": {}}]},
         {"analyses": [{"type": "ac", "sweep": "dec", "points": 10, "fstart": 1.0, "fstop": 1e5}],
          "probes": ["V(OUT)"]}),
        ("RLC bandpass", {
            "nodes": ["0", "IN", "MID", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
                 "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "MID"], "value": 50.0, "params": {}},
                {"kind": "L", "name": "L1", "nodes": ["MID", "OUT"], "value": 1e-3, "params": {}},
                {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": 1e-6, "params": {}}]},
         {"analyses": [{"type": "ac", "sweep": "dec", "points": 8, "fstart": 10.0, "fstop": 1e6}],
          "probes": ["V(OUT)"]}),
    ]
    for label, circuit, study in ac_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro["convergence"].get("ac") and rcpp.get("convergence", {}).get("ac")):
            diffs.append("ac did not converge")
        fp = np.asarray(ro["ac"]["x_f"], dtype=np.float64)
        fc = np.asarray(rcpp.get("ac", {}).get("x_f", []), dtype=np.float64)
        if fp.shape != fc.shape:
            diffs.append(f"freq grid len {fp.shape}!={fc.shape}")
        else:
            for pr in study["probes"]:
                for key in ("mag", "phase_deg"):
                    vp = np.asarray(ro["ac"][key][pr], dtype=np.float64)
                    vc = np.asarray(rcpp["ac"][key][pr], dtype=np.float64)
                    scale = max(float(np.max(np.abs(vp))), 1e-9)
                    dmax = float(np.max(np.abs(vp - vc)))
                    if dmax / scale > 1e-6:
                        diffs.append(f"{pr}.{key} max|d|={dmax:.2e}")
        if diffs:
            print(f"  {('ac ' + label):<24} DIFF: {'; '.join(diffs)[:100]}")
            mism += 1
        else:
            # report the -3dB point for the RC case
            mags = np.asarray(rcpp["ac"]["mag"][study["probes"][0]])
            i3 = int(np.argmin(np.abs(mags - 0.70710678)))
            print(f"  {('ac ' + label):<24} MATCH  ({len(fc)} pts, ~-3dB @ {fc[i3]:.0f} Hz)")

    # ---- Nonlinear DC (Newton): diode clamp, common-emitter BJT bias, NMOS bias. Both
    # engines run the same Newton loop (same junction limiting + damping) over the same dense
    # solves, so the converged operating point matches tightly (1e-6 rel).
    nl_op_cases = [
        ("diode clamp", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "D", "name": "D1", "nodes": ["OUT", "0"], "value": None,
                 "model": "D1N4148", "params": {}}],
            "models": {"D1N4148": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}},
            "params": {"TEMP": 27}},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)"]}),
        ("common-emitter bias", {
            "nodes": ["0", "VCC", "B", "C"],
            "devices": [
                {"kind": "V", "name": "VCC", "nodes": ["VCC", "0"], "value": 12.0, "params": {}},
                {"kind": "R", "name": "RB", "nodes": ["VCC", "B"], "value": 470000.0, "params": {}},
                {"kind": "R", "name": "RC", "nodes": ["VCC", "C"], "value": 2200.0, "params": {}},
                {"kind": "Q", "name": "Q1", "nodes": ["C", "B", "0"], "value": None,
                 "model": "QN", "params": {}}],
            "models": {"QN": {"type": "npn", "IS": 1e-15, "BF": 200.0, "BR": 1.0, "VAF": 100.0}},
            "params": {"TEMP": 27}},
         {"analyses": [{"type": "op"}], "probes": ["V(C)", "V(B)", "I(VCC)"]}),
        ("nmos diode-connected", {
            "nodes": ["0", "DD", "D"],
            "devices": [
                {"kind": "V", "name": "VDD", "nodes": ["DD", "0"], "value": 5.0, "params": {}},
                {"kind": "R", "name": "RD", "nodes": ["DD", "D"], "value": 1000.0, "params": {}},
                # gate tied to drain (diode-connected) -> always saturation when on
                {"kind": "M", "name": "M1", "nodes": ["D", "D", "0"], "value": None,
                 "model": "NM", "params": {}}],
            "models": {"NM": {"type": "nmos", "VTO": 1.0, "KP": 5e-4, "LAMBDA": 0.02,
                              "W": 1e-4, "L": 1e-5}},
            "params": {"TEMP": 27}},
         {"analyses": [{"type": "op"}], "probes": ["V(D)", "I(VDD)"]}),
    ]
    for label, circuit, study in nl_op_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro.get("convergence", {}).get("op") and rcpp.get("convergence", {}).get("op")):
            diffs.append(f"convergence op py={ro.get('convergence')} cpp={rcpp.get('convergence')}")
        po, co = ro.get("op", {}), rcpp.get("op", {})
        for pr in study["probes"]:
            pv, cv = po.get(pr), co.get(pr)
            if pv is None or cv is None:
                diffs.append(f"{pr} missing py={pv} cpp={cv}")
                continue
            rel = abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9)
            if rel > 1e-6:
                diffs.append(f"{pr} {pv:.9g}!={cv:.9g} (rel {rel:.1e})")
        if diffs:
            print(f"  {('nl ' + label):<24} DIFF: {'; '.join(diffs)[:110]}")
            mism += 1
        else:
            shown = ", ".join(f"{p}={float(co[p]):.4g}" for p in study["probes"][:2])
            print(f"  {('nl ' + label):<24} MATCH  ({shown})")

    # ---- Nonlinear transient (Newton per step): diode half-wave rectifier. A SIN source
    # through a series diode into an RC load; the diode rectifies the negative half-cycles.
    # Same Newton path + junction limiting on both engines -> the trace matches to FP precision.
    import math as _math
    freq = 1000.0
    period = 1.0 / freq
    rect = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("SIN", vo=0.0, va=5.0, f=freq, td=0.0, theta=0.0)}},
            {"kind": "D", "name": "D1", "nodes": ["IN", "OUT"], "value": None,
             "model": "DR", "params": {}},
            {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
            {"kind": "C", "name": "CL", "nodes": ["OUT", "0"], "value": 1e-7, "params": {"ic": 0.0}}],
        "models": {"DR": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}},
        "params": {"TEMP": 27}}
    rect_study = {"analyses": [{"type": "tran", "tstep": period / 200, "tstop": 3 * period,
                                "uic": True}], "probes": ["V(OUT)", "V(IN)"]}
    checks += 1
    ro = simulate(rect, rect_study)
    rc, rcpp = core.call("sim_spice", {"circuit": rect, "study": rect_study})
    diffs = []
    if not (ro["convergence"].get("tran") and rcpp.get("convergence", {}).get("tran")):
        diffs.append("tran did not converge")
    tp = np.asarray(ro["tran"]["x_t"], dtype=np.float64)
    tc = np.asarray(rcpp.get("tran", {}).get("x_t", []), dtype=np.float64)
    if tp.shape != tc.shape:
        diffs.append(f"time grid len {tp.shape}!={tc.shape}")
    elif tp.size and float(np.max(np.abs(tp - tc))) / max(float(tp[-1]), 1e-12) > 1e-9:
        diffs.append("time grid values differ")
    else:
        for pr in rect_study["probes"]:
            vp = np.asarray(ro["tran"]["traces"][pr], dtype=np.float64)
            vc = np.asarray(rcpp["tran"]["traces"][pr], dtype=np.float64)
            scale = max(float(np.max(np.abs(vp))), 1e-9)
            dmax = float(np.max(np.abs(vp - vc)))
            if dmax / scale > 1e-6:
                diffs.append(f"{pr} trace max|d|={dmax:.2e} (rel {dmax/scale:.1e})")
    if diffs:
        print(f"  {'nl tran rectifier':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        vmax = float(np.max(np.asarray(rcpp["tran"]["traces"]["V(OUT)"])))
        print(f"  {'nl tran rectifier':<24} MATCH  ({len(tc)} pts, V(OUT)max={vmax:.4g})")

    # ---- Mutual inductance (K): a coupled transformer, transient + AC. The inductor branch
    # equations carry the M*di_other/dt cross terms (transient) and -jwM (AC) on both engines,
    # so the coupled traces / sweeps match to FP precision.
    Lp, Ls, kc = 1e-3, 1e-3, 0.8
    xfmr = {
        "nodes": ["0", "IN", "P", "S"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=1.0, td=0.0, tr=1e-7, tf=1e-7,
                                        pw=2e-4, per=0.0), "ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RP", "nodes": ["IN", "P"], "value": 50.0, "params": {}},
            {"kind": "L", "name": "L1", "nodes": ["P", "0"], "value": Lp, "params": {"ic": 0.0}},
            {"kind": "L", "name": "L2", "nodes": ["S", "0"], "value": Ls, "params": {"ic": 0.0}},
            {"kind": "R", "name": "RL", "nodes": ["S", "0"], "value": 1000.0, "params": {}},
            {"kind": "K", "name": "K1", "nodes": [], "value": kc,
             "params": {"inductors": ["L1", "L2"]}}],
        "models": {}, "params": {}}
    # transient
    checks += 1
    xt_study = {"analyses": [{"type": "tran", "tstep": 1e-6, "tstop": 4e-4, "uic": True}],
                "probes": ["V(S)", "I(L1)", "I(L2)"]}
    ro = simulate(xfmr, xt_study)
    rc, rcpp = core.call("sim_spice", {"circuit": xfmr, "study": xt_study})
    diffs = []
    if not (ro["convergence"].get("tran") and rcpp.get("convergence", {}).get("tran")):
        diffs.append("tran did not converge")
    tp = np.asarray(ro["tran"]["x_t"], dtype=np.float64)
    tc = np.asarray(rcpp.get("tran", {}).get("x_t", []), dtype=np.float64)
    if tp.shape != tc.shape:
        diffs.append(f"time grid len {tp.shape}!={tc.shape}")
    else:
        for pr in xt_study["probes"]:
            vp = np.asarray(ro["tran"]["traces"][pr], dtype=np.float64)
            vc = np.asarray(rcpp["tran"]["traces"][pr], dtype=np.float64)
            scale = max(float(np.max(np.abs(vp))), 1e-9)
            dmax = float(np.max(np.abs(vp - vc)))
            if dmax / scale > 1e-6:
                diffs.append(f"{pr} max|d|={dmax:.2e} (rel {dmax/scale:.1e})")
    if diffs:
        print(f"  {'K tran transformer':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        vs = float(np.max(np.abs(np.asarray(rcpp["tran"]["traces"]["V(S)"]))))
        print(f"  {'K tran transformer':<24} MATCH  ({len(tc)} pts, |V(S)|max={vs:.4g})")
    # AC
    checks += 1
    xa_study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 8, "fstart": 100.0,
                              "fstop": 1e6}], "probes": ["V(S)"]}
    ro = simulate(xfmr, xa_study)
    rc, rcpp = core.call("sim_spice", {"circuit": xfmr, "study": xa_study})
    diffs = []
    if not (ro["convergence"].get("ac") and rcpp.get("convergence", {}).get("ac")):
        diffs.append("ac did not converge")
    fp = np.asarray(ro["ac"]["x_f"], dtype=np.float64)
    fc = np.asarray(rcpp.get("ac", {}).get("x_f", []), dtype=np.float64)
    if fp.shape != fc.shape:
        diffs.append(f"freq grid len {fp.shape}!={fc.shape}")
    else:
        for key in ("mag", "phase_deg"):
            vp = np.asarray(ro["ac"][key]["V(S)"], dtype=np.float64)
            vc = np.asarray(rcpp["ac"][key]["V(S)"], dtype=np.float64)
            scale = max(float(np.max(np.abs(vp))), 1e-9)
            dmax = float(np.max(np.abs(vp - vc)))
            if dmax / scale > 1e-6:
                diffs.append(f"V(S).{key} max|d|={dmax:.2e}")
    if diffs:
        print(f"  {'K ac transformer':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        print(f"  {'K ac transformer':<24} MATCH  ({len(fc)} pts)")

    # ---- Behavioral sources (B): nonlinear V/I expressions. Both engines run the same Newton
    # loop with an identical central-difference Jacobian over the controlling rows, so the
    # converged DC point matches tightly (1e-6 rel). AC linearizes about the op point.
    b_op_cases = [
        ("B-V squarer", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 2.0, "params": {}},
                {"kind": "B", "name": "B1", "nodes": ["OUT", "0"], "value": None,
                 "params": {"btype": "V", "expr": "V(IN)*V(IN)"}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}}],
            "models": {}, "params": {}},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]}),
        ("B-I behavioral diode", {
            "nodes": ["0", "IN", "A"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "A"], "value": 1000.0, "params": {}},
                {"kind": "B", "name": "B1", "nodes": ["A", "0"], "value": None,
                 "params": {"btype": "I", "expr": "1e-9*(exp(V(A)/0.025852)-1)"}}],
            "models": {}, "params": {}},
         {"analyses": [{"type": "op"}], "probes": ["V(A)"]}),
    ]
    for label, circuit, study in b_op_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro.get("convergence", {}).get("op") and rcpp.get("convergence", {}).get("op")):
            diffs.append(f"convergence op py={ro.get('convergence')} cpp={rcpp.get('convergence')}")
        po, co = ro.get("op", {}), rcpp.get("op", {})
        for pr in study["probes"]:
            pv, cv = po.get(pr), co.get(pr)
            if pv is None or cv is None:
                diffs.append(f"{pr} missing py={pv} cpp={cv}")
                continue
            rel = abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9)
            if rel > 1e-6:
                diffs.append(f"{pr} {pv:.9g}!={cv:.9g} (rel {rel:.1e})")
        if diffs:
            print(f"  {('B ' + label):<24} DIFF: {'; '.join(diffs)[:110]}")
            mism += 1
        else:
            shown = ", ".join(f"{p}={float(co[p]):.4g}" for p in study["probes"][:2])
            print(f"  {('B ' + label):<24} MATCH  ({shown})")

    # B-V AC: linear V=2*V(IN) behaves as a gain-2 VCVS; |V(OUT)| flat at 2 across the band.
    b_ac = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "B", "name": "B1", "nodes": ["OUT", "0"], "value": None,
             "params": {"btype": "V", "expr": "2*V(IN)"}},
            {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}}],
        "models": {}, "params": {}}
    b_ac_study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 5, "fstart": 1.0,
                                "fstop": 1e4}], "probes": ["V(OUT)"]}
    checks += 1
    ro = simulate(b_ac, b_ac_study)
    rc, rcpp = core.call("sim_spice", {"circuit": b_ac, "study": b_ac_study})
    diffs = []
    if not (ro["convergence"].get("ac") and rcpp.get("convergence", {}).get("ac")):
        diffs.append("ac did not converge")
    fp = np.asarray(ro["ac"]["x_f"], dtype=np.float64)
    fc = np.asarray(rcpp.get("ac", {}).get("x_f", []), dtype=np.float64)
    if fp.shape != fc.shape:
        diffs.append(f"freq grid len {fp.shape}!={fc.shape}")
    else:
        for key in ("mag", "phase_deg"):
            vp = np.asarray(ro["ac"][key]["V(OUT)"], dtype=np.float64)
            vc = np.asarray(rcpp["ac"][key]["V(OUT)"], dtype=np.float64)
            scale = max(float(np.max(np.abs(vp))), 1e-9)
            dmax = float(np.max(np.abs(vp - vc)))
            if dmax / scale > 1e-6:
                diffs.append(f"V(OUT).{key} max|d|={dmax:.2e}")
    if diffs:
        print(f"  {'B ac gain-2':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        g = float(np.asarray(rcpp["ac"]["mag"]["V(OUT)"])[0])
        print(f"  {'B ac gain-2':<24} MATCH  ({len(fc)} pts, |V(OUT)|={g:.4g})")

    # ---- Controlled sources F (CCCS) / H (CCVS) + ideal OPAMP. Linear stamps; DC matches to
    # 1e-9, AC mag+phase to FP precision.
    fh_op_cases = [
        ("CCCS x3", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 2.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "0"], "value": 1000.0, "params": {}},
                # F mirrors I(V1) * 3 into the output node load
                {"kind": "F", "name": "F1", "nodes": ["0", "OUT"], "value": 3.0,
                 "params": {"control": "V1#0"}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 100.0, "params": {}}],
            "models": {}, "params": {}},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)"]}),
        ("CCVS x50", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 1.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "0"], "value": 1000.0, "params": {}},
                {"kind": "H", "name": "H1", "nodes": ["OUT", "0"], "value": 50.0,
                 "params": {"control": "V1#0"}},
                {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}}],
            "models": {}, "params": {}},
         {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(H1)"]}),
    ]
    for label, circuit, study in fh_op_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro.get("convergence", {}).get("op") and rcpp.get("convergence", {}).get("op")):
            diffs.append("op did not converge")
        po, co = ro.get("op", {}), rcpp.get("op", {})
        for pr in study["probes"]:
            pv, cv = po.get(pr), co.get(pr)
            if pv is None or cv is None:
                diffs.append(f"{pr} missing py={pv} cpp={cv}")
                continue
            rel = abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9)
            if rel > 1e-9:
                diffs.append(f"{pr} {pv:.9g}!={cv:.9g} (rel {rel:.1e})")
        if diffs:
            print(f"  {('fh ' + label):<24} DIFF: {'; '.join(diffs)[:110]}")
            mism += 1
        else:
            shown = ", ".join(f"{p}={float(co[p]):.4g}" for p in study["probes"][:2])
            print(f"  {('fh ' + label):<24} MATCH  ({shown})")

    # Ideal inverting op-amp (op + ac): gain = -Rf/Rin = -10.
    Rin, Rf, vin = 1000.0, 10000.0, 0.2
    opamp = {
        "nodes": ["0", "IN", "INV", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": vin,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RIN", "nodes": ["IN", "INV"], "value": Rin, "params": {}},
            {"kind": "R", "name": "RF", "nodes": ["INV", "OUT"], "value": Rf, "params": {}},
            {"kind": "OPAMP", "name": "X1", "nodes": ["0", "INV", "OUT"], "value": None,
             "params": {}}],
        "models": {}, "params": {}}
    opamp_study = {"analyses": [{"type": "op"},
                                {"type": "ac", "sweep": "dec", "points": 5, "fstart": 1, "fstop": 1000}],
                   "probes": ["V(OUT)"]}
    checks += 1
    ro = simulate(opamp, opamp_study)
    rc, rcpp = core.call("sim_spice", {"circuit": opamp, "study": opamp_study})
    diffs = []
    pv, cv = ro.get("op", {}).get("V(OUT)"), rcpp.get("op", {}).get("V(OUT)")
    if pv is None or cv is None or abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9) > 1e-9:
        diffs.append(f"op V(OUT) py={pv} cpp={cv}")
    vp = np.asarray(ro["ac"]["mag"]["V(OUT)"], dtype=np.float64)
    vc = np.asarray(rcpp.get("ac", {}).get("mag", {}).get("V(OUT)", []), dtype=np.float64)
    if vp.shape != vc.shape or (vp.size and float(np.max(np.abs(vp - vc))) / max(float(np.max(np.abs(vp))), 1e-9) > 1e-6):
        diffs.append("ac mag differs")
    if diffs:
        print(f"  {'opamp inverting':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        print(f"  {'opamp inverting':<24} MATCH  (V(OUT)={float(cv):.4g}, |gain|={float(vc[0]):.4g})")

    # ---- DC sweep (.dc): step a V source, re-solving with warm-start continuation. A linear
    # divider sweep + a nonlinear diode I-V curve. Both engines walk the same swept grid through
    # the same (Newton) DC solve seeded by the previous point -> the full sweep matches.
    dc_cases = [
        ("dc divider sweep", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 3000.0, "params": {}}],
            "models": {}, "params": {}},
         {"analyses": [{"type": "dc", "source": "V1", "start": 0.0, "stop": 10.0, "step": 1.0}],
          "probes": ["V(OUT)", "I(V1)"]}),
        ("dc diode I-V", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 100.0, "params": {}},
                {"kind": "D", "name": "D1", "nodes": ["OUT", "0"], "value": None,
                 "model": "DD", "params": {}}],
            "models": {"DD": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}},
            "params": {"TEMP": 27}},
         {"analyses": [{"type": "dc", "source": "V1", "start": 0.0, "stop": 1.0, "step": 0.05}],
          "probes": ["V(OUT)", "I(V1)"]}),
    ]
    for label, circuit, study in dc_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro["convergence"].get("dc") and rcpp.get("convergence", {}).get("dc")):
            diffs.append("dc did not converge")
        xp = np.asarray(ro["dc"]["x"], dtype=np.float64)
        xc = np.asarray(rcpp.get("dc", {}).get("x", []), dtype=np.float64)
        if xp.shape != xc.shape:
            diffs.append(f"sweep grid len {xp.shape}!={xc.shape}")
        else:
            for pr in study["probes"]:
                vp = np.asarray(ro["dc"]["traces"][pr], dtype=np.float64)
                vc = np.asarray(rcpp["dc"]["traces"][pr], dtype=np.float64)
                scale = max(float(np.max(np.abs(vp))), 1e-9)
                dmax = float(np.max(np.abs(vp - vc)))
                if dmax / scale > 1e-6:
                    diffs.append(f"{pr} max|d|={dmax:.2e} (rel {dmax/scale:.1e})")
        if diffs:
            print(f"  {('dc ' + label):<24} DIFF: {'; '.join(diffs)[:100]}")
            mism += 1
        else:
            last = float(np.asarray(rcpp["dc"]["traces"][study["probes"][0]])[-1])
            print(f"  {('dc ' + label):<24} MATCH  ({len(xc)} pts, {study['probes'][0]}[-1]={last:.4g})")

    # ---- .tf (transfer function): DC gain, input resistance, output resistance. Computed on the
    # network linearized at the DC op via the complex AC stamping path at omega=0. Linear networks
    # match to 1e-9 rel; the nonlinear (op-amp) case to 1e-6.
    tf_cases = [
        ("tf divider V-src", {
            "nodes": ["IN", "OUT", "0"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}}]},
         {"analyses": [{"type": "tf", "output": "V(OUT)", "source": "V1"}], "probes": []}, 1e-9),
        ("tf I-src transimped", {
            "nodes": ["A", "0"],
            "devices": [
                {"kind": "I", "name": "I1", "nodes": ["0", "A"], "value": 0.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["A", "0"], "value": 5000.0, "params": {}}]},
         {"analyses": [{"type": "tf", "output": "V(A)", "source": "I1"}], "probes": []}, 1e-9),
        ("tf inverting opamp", {
            "nodes": ["0", "IN", "INV", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.2, "params": {}},
                {"kind": "R", "name": "RIN", "nodes": ["IN", "INV"], "value": 1000.0, "params": {}},
                {"kind": "R", "name": "RF", "nodes": ["INV", "OUT"], "value": 10000.0, "params": {}},
                {"kind": "OPAMP", "name": "X1", "nodes": ["0", "INV", "OUT"], "value": None, "params": {}}],
            "models": {}, "params": {}},
         {"analyses": [{"type": "tf", "output": "V(OUT)", "source": "V1"}], "probes": []}, 1e-9),
    ]
    for label, circuit, study, tol in tf_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro["convergence"].get("tf") and rcpp.get("convergence", {}).get("tf")):
            diffs.append("tf did not converge")
        pt, ct = ro.get("tf", {}), rcpp.get("tf", {})
        for k in ("gain", "rin", "rout"):
            pv, cv = pt.get(k), ct.get(k)
            if pv is None or cv is None:
                diffs.append(f"{k} missing py={pv} cpp={cv}")
                continue
            if not (math.isfinite(pv) and math.isfinite(cv)):
                if pv != cv:
                    diffs.append(f"{k} py={pv} cpp={cv}")
                continue
            rel = abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9)
            if rel > tol:
                diffs.append(f"{k} {pv:.9g}!={cv:.9g} (rel {rel:.1e})")
        if diffs:
            print(f"  {('tf ' + label):<24} DIFF: {'; '.join(diffs)[:100]}")
            mism += 1
        else:
            print(f"  {('tf ' + label):<24} MATCH  (gain={float(ct['gain']):.4g}, "
                  f"rin={float(ct['rin']):.4g}, rout={float(ct['rout']):.4g})")

    # ---- .noise (resistor thermal): output-noise PSD + integrated RMS via the AC transfer
    # impedance from each resistor. Same complex MNA + trapezoid integral on both engines.
    noise_cases = [
        ("noise RC low-pass", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
                 "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
                {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": 1e-6, "params": {}}],
            "params": {"TEMP": 27}},
         {"analyses": [{"type": "noise", "output": "V(OUT)", "sweep": "dec", "points": 10,
                        "fstart": 1.0, "fstop": 1e5}], "probes": []}),
        ("noise two-R divider", {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
                 "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 4700.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 10000.0, "params": {}}],
            "params": {"TEMP": 27}},
         {"analyses": [{"type": "noise", "output": "V(OUT)", "sweep": "lin", "points": 20,
                        "fstart": 1.0, "fstop": 1e4}], "probes": []}),
    ]
    for label, circuit, study in noise_cases:
        checks += 1
        ro = simulate(circuit, study)
        rc, rcpp = core.call("sim_spice", {"circuit": circuit, "study": study})
        diffs = []
        if not (ro["convergence"].get("noise") and rcpp.get("convergence", {}).get("noise")):
            diffs.append("noise did not converge")
        pn, cn = ro.get("noise", {}), rcpp.get("noise", {})
        fp = np.asarray(pn.get("x_f", []), dtype=np.float64)
        fc = np.asarray(cn.get("x_f", []), dtype=np.float64)
        if fp.shape != fc.shape:
            diffs.append(f"freq grid {fp.shape}!={fc.shape}")
        else:
            for key in ("onoise_spectrum", "onoise_psd"):
                vp = np.asarray(pn.get(key, []), dtype=np.float64)
                vc = np.asarray(cn.get(key, []), dtype=np.float64)
                scale = max(float(np.max(np.abs(vp))) if vp.size else 0.0, 1e-30)
                dmax = float(np.max(np.abs(vp - vc))) if vp.shape == vc.shape and vp.size else 1.0
                if dmax / scale > 1e-6:
                    diffs.append(f"{key} max|d|={dmax:.2e}")
            rp, rcv = float(pn.get("total_noise_rms", 0.0)), float(cn.get("total_noise_rms", 0.0))
            if abs(rp - rcv) / max(abs(rp), 1e-30) > 1e-6:
                diffs.append(f"total_rms {rp:.4g}!={rcv:.4g}")
            for rn, pv in pn.get("per_resistor", {}).items():
                cv = cn.get("per_resistor", {}).get(rn)
                if cv is None or abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-30) > 1e-6:
                    diffs.append(f"per_resistor[{rn}] py={pv} cpp={cv}")
        if diffs:
            print(f"  {('noise ' + label):<24} DIFF: {'; '.join(diffs)[:100]}")
            mism += 1
        else:
            print(f"  {('noise ' + label):<24} MATCH  ({len(fc)} pts, "
                  f"rms={float(cn['total_noise_rms']):.4g} Vrms)")

    # ---- .four (Fourier / THD): on a diode half-wave rectifier transient. Direct DFT matching
    # numpy.fft.rfft binning + single-sided amplitude scaling; THD% over the resolvable harmonics.
    _freqf = 1000.0
    _perf = 1.0 / _freqf
    four_circ = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("SIN", vo=0.0, va=5.0, f=_freqf, td=0.0, theta=0.0)}},
            {"kind": "D", "name": "D1", "nodes": ["IN", "OUT"], "value": None,
             "model": "DR", "params": {}},
            {"kind": "R", "name": "RL", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}},
            {"kind": "C", "name": "CL", "nodes": ["OUT", "0"], "value": 1e-7, "params": {"ic": 0.0}}],
        "models": {"DR": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}}, "params": {"TEMP": 27}}
    four_study = {"analyses": [{"type": "four", "f0": _freqf, "probe": "V(OUT)",
                                "tstep": _perf / 200, "tstop": 4 * _perf, "uic": True}],
                  "probes": ["V(OUT)"]}
    checks += 1
    ro = simulate(four_circ, four_study)
    rc, rcpp = core.call("sim_spice", {"circuit": four_circ, "study": four_study})
    diffs = []
    if not (ro["convergence"].get("four") and rcpp.get("convergence", {}).get("four")):
        diffs.append("four did not converge")
    pf, cf = ro.get("four", {}), rcpp.get("four", {})
    for k in ("f0", "dc", "thd_pct"):
        pv, cv = pf.get(k), cf.get(k)
        if pv is None or cv is None or abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-12) > 1e-6:
            diffs.append(f"{k} py={pv} cpp={cv}")
    ph, ch = pf.get("harmonics", []), cf.get("harmonics", [])
    if len(ph) != len(ch):
        diffs.append(f"nharm {len(ph)}!={len(ch)}")
    else:
        for (pn_, pm, pp), (cn_, cm, cp) in zip(ph, ch):
            if pn_ != cn_ or abs(pm - cm) / max(abs(pm), 1e-12) > 1e-6:
                diffs.append(f"H{pn_} mag {pm:.4g}!={cm:.4g}")
                break
    if diffs:
        print(f"  {'four rectifier THD':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        print(f"  {'four rectifier THD':<24} MATCH  (THD={float(cf['thd_pct']):.4f}%, "
              f"H1={float(cf['harmonics'][0][1]):.4g})")

    # ---- .temp (temperature sweep): diode clamp at -40/25/85 C. Diode IS + Vt are temp-aware,
    # so V(OUT) moves with temperature; deterministic, matches the oracle exactly (1e-6).
    temp_circ = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "D", "name": "D1", "nodes": ["OUT", "0"], "value": None, "model": "DD", "params": {}}],
        "models": {"DD": {"IS": 2.52e-9, "N": 1.752, "RS": 0.568}}, "params": {"TEMP": 27}}
    temp_study = {"analyses": [{"type": "temp", "values": [-40.0, 25.0, 85.0],
                               "analysis": {"type": "op"}}], "probes": ["V(OUT)"]}
    checks += 1
    ro = simulate(temp_circ, temp_study)
    rc, rcpp = core.call("sim_spice", {"circuit": temp_circ, "study": temp_study})
    diffs = []
    if not (ro["convergence"].get("temp") and rcpp.get("convergence", {}).get("temp")):
        diffs.append("temp did not converge")
    pp, cp = ro.get("temp", {}).get("points", []), rcpp.get("temp", {}).get("points", [])
    if len(pp) != len(cp):
        diffs.append(f"npoints {len(pp)}!={len(cp)}")
    else:
        for a, b in zip(pp, cp):
            if abs(float(a["temp"]) - float(b["temp"])) > 1e-9:
                diffs.append("temp value differs")
                break
            pv, cv = a["op"]["V(OUT)"], b["op"]["V(OUT)"]
            if abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9) > 1e-6:
                diffs.append(f"V(OUT)@{a['temp']} {pv:.6g}!={cv:.6g}")
    if diffs:
        print(f"  {'temp diode sweep':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        vs = ", ".join(f"{p['temp']:.0f}C={float(p['op']['V(OUT)']):.4g}" for p in cp)
        print(f"  {'temp diode sweep':<24} MATCH  ({vs})")

    # ---- .step (parameter sweep): step R2 of a divider; the inner op re-runs per value. Each run
    # is an independent build, so the swept value takes effect cleanly — runs match the oracle.
    step_circ = {
        "nodes": ["IN", "OUT", "0"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}}]}
    step_study = {"analyses": [{"type": "step", "param": "R2", "values": [1000.0, 2000.0, 4000.0]},
                               {"type": "op"}], "probes": ["V(OUT)"]}
    checks += 1
    ro = simulate(step_circ, step_study)
    rc, rcpp = core.call("sim_spice", {"circuit": step_circ, "study": step_study})
    diffs = []
    if not (ro["convergence"].get("step") and rcpp.get("convergence", {}).get("step")):
        diffs.append("step did not converge")
    ps, cs = ro.get("step", {}), rcpp.get("step", {})
    if ps.get("param") != cs.get("param"):
        diffs.append(f"param {ps.get('param')}!={cs.get('param')}")
    pv = np.asarray(ps.get("values", []), dtype=np.float64)
    cv = np.asarray(cs.get("values", []), dtype=np.float64)
    if pv.shape != cv.shape or (pv.size and float(np.max(np.abs(pv - cv))) > 1e-9):
        diffs.append("values differ")
    pr, cr = ps.get("runs", []), cs.get("runs", [])
    if len(pr) != len(cr):
        diffs.append(f"nruns {len(pr)}!={len(cr)}")
    else:
        for a, b in zip(pr, cr):
            av, bv = a["op"]["V(OUT)"], b["op"]["V(OUT)"]
            if abs(float(av) - float(bv)) / max(abs(float(av)), 1e-9) > 1e-6:
                diffs.append(f"run@{a['value']} {av:.6g}!={bv:.6g}")
                break
    if diffs:
        print(f"  {'step R2 divider':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        vs = ", ".join(f"{r['value']:.0f}={float(r['op']['V(OUT)']):.4g}" for r in cr)
        print(f"  {'step R2 divider':<24} MATCH  ({vs})")

    # ---- T (lossless transmission line). Both engines share the exact Branin transient companion
    # (Z0 + delayed history voltage source, linear interpolation) and the exact lossless two-port AC
    # stamp (coth/csch of j*omega*td), so the delayed waveform and the swept response match to FP.
    #
    # (a) AC quarter-wave impedance transformer: a TD line of electrical length 1/4 wave at f0 maps a
    #     load RL to Zin = Z0^2/RL at f0. Plus a matched-line check (RL=Z0 -> flat |V(out)|). We sweep
    #     V(out) magnitude+phase over a decade band straddling f0 and compare to the oracle.
    z0_tl, f0_tl = 75.0, 1e9
    td_tl = 0.25 / f0_tl  # quarter wave at f0
    tl_ac = {
        "nodes": ["0", "IN", "A", "B"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "R", "name": "RS", "nodes": ["IN", "A"], "value": 50.0, "params": {}},
            {"kind": "T", "name": "T1", "nodes": ["A", "0", "B", "0"], "value": None,
             "params": {"z0": z0_tl, "td": td_tl}},
            {"kind": "R", "name": "RL", "nodes": ["B", "0"], "value": 200.0, "params": {}}],
        "models": {}, "params": {}}
    tl_ac_study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 12, "fstart": 1e8,
                                 "fstop": 1e10}], "probes": ["V(A)", "V(B)"]}
    checks += 1
    ro = simulate(tl_ac, tl_ac_study)
    rc, rcpp = core.call("sim_spice", {"circuit": tl_ac, "study": tl_ac_study})
    diffs = []
    if not (ro["convergence"].get("ac") and rcpp.get("convergence", {}).get("ac")):
        diffs.append("ac did not converge")
    fp = np.asarray(ro["ac"]["x_f"], dtype=np.float64)
    fc = np.asarray(rcpp.get("ac", {}).get("x_f", []), dtype=np.float64)
    if fp.shape != fc.shape:
        diffs.append(f"freq grid {fp.shape}!={fc.shape}")
    else:
        for pr in tl_ac_study["probes"]:
            for key in ("mag", "phase_deg"):
                vp = np.asarray(ro["ac"][key][pr], dtype=np.float64)
                vc = np.asarray(rcpp["ac"][key][pr], dtype=np.float64)
                scale = max(float(np.max(np.abs(vp))), 1e-9)
                dmax = float(np.max(np.abs(vp - vc)))
                if dmax / scale > 1e-6:
                    diffs.append(f"{pr}.{key} max|d|={dmax:.2e}")
    if diffs:
        print(f"  {'T ac quarter-wave':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        print(f"  {'T ac quarter-wave':<24} MATCH  ({len(fc)} pts)")

    # (b) Transient: a voltage step launched through a source resistor into a Z0 line terminated in
    #     a matched load (RL=Z0 -> no reflection). With TD an exact integer multiple of tstep the
    #     received step at B is a clean one-delay-shifted copy. Compare the full V(A)/V(B) traces.
    z0_t, td_t = 50.0, 1e-8
    tstep_t = td_t / 10.0  # td is an exact multiple of the timestep
    tl_tr = {
        "nodes": ["0", "IN", "A", "B"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0,
             "params": {"waveform": _wf("PULSE", v1=0.0, v2=2.0, td=2e-8, tr=1e-10, tf=1e-10,
                                        pw=1e-6, per=0.0)}},
            {"kind": "R", "name": "RS", "nodes": ["IN", "A"], "value": z0_t, "params": {}},
            {"kind": "T", "name": "T1", "nodes": ["A", "0", "B", "0"], "value": None,
             "params": {"z0": z0_t, "td": td_t}},
            {"kind": "R", "name": "RL", "nodes": ["B", "0"], "value": z0_t, "params": {}}],
        "models": {}, "params": {}}
    tl_tr_study = {"analyses": [{"type": "tran", "tstep": tstep_t, "tstop": 8e-8, "uic": False}],
                   "probes": ["V(A)", "V(B)"]}
    checks += 1
    ro = simulate(tl_tr, tl_tr_study)
    rc, rcpp = core.call("sim_spice", {"circuit": tl_tr, "study": tl_tr_study})
    diffs = []
    if not (ro["convergence"].get("tran") and rcpp.get("convergence", {}).get("tran")):
        diffs.append("tran did not converge")
    tp = np.asarray(ro["tran"]["x_t"], dtype=np.float64)
    tc = np.asarray(rcpp.get("tran", {}).get("x_t", []), dtype=np.float64)
    if tp.shape != tc.shape:
        diffs.append(f"time grid len {tp.shape}!={tc.shape}")
    else:
        for pr in tl_tr_study["probes"]:
            vp = np.asarray(ro["tran"]["traces"][pr], dtype=np.float64)
            vc = np.asarray(rcpp["tran"]["traces"][pr], dtype=np.float64)
            scale = max(float(np.max(np.abs(vp))), 1e-9)
            dmax = float(np.max(np.abs(vp - vc)))
            if dmax / scale > 1e-6:
                diffs.append(f"{pr} max|d|={dmax:.2e} (rel {dmax/scale:.1e})")
    if diffs:
        print(f"  {'T tran matched line':<24} DIFF: {'; '.join(diffs)[:100]}")
        mism += 1
    else:
        vb = float(np.max(np.abs(np.asarray(rcpp["tran"]["traces"]["V(B)"]))))
        print(f"  {'T tran matched line':<24} MATCH  ({len(tc)} pts, |V(B)|max={vb:.4g})")

    print(f"\n[spice-parity] {checks - mism}/{checks} circuit cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
