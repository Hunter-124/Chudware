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
    for label, circuit, study in cases:
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

    print(f"\n[spice-parity] {len(cases) - mism}/{len(cases)} DC cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
