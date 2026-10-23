#!/usr/bin/env python3
"""V&V Benchmark Harness.

Systematic validation of Chudware simulation and analysis backends against
INDEPENDENT analytic reference solutions (textbook cases, NAFEMS-style
benchmarks where freely reproducible).

Extends scripts/sim_audit.py with:
  - More cases per solver family (plates, modal, multi-span frames, more circuits)
  - Tolerance tiers: EXACT (<0.01%), PRECISE (<1%), ENGINEERING (<10%), TREND (labeled)
  - Machine-readable JSON results table
  - Plain-text Markdown V&V report (auto-generated, cites formula source in-code)
  - pytest entry points for CI integration (smoke subset needs no .so; its native-RPC
    cases skip until CHUDWARE_LIBCHUDWARE names a staged core library)

Reference values are derived INDEPENDENTLY in this script (never read from the engine
under test). Every formula is cited in the inline comment above it.

Usage (standalone):
    CHUDWARE_LIBCHUDWARE=/path/to/libchudware.so \\
        ~/.local/share/Chudware/mcp-venv/bin/python scripts/vv_benchmark.py

Usage (pytest smoke — no .so needed; native hydraulic/piping/ECAD RPC cases skip
without one):
    pytest scripts/vv_benchmark.py::TestSmoke -q

Usage (full suite via pytest):
    CHUDWARE_RUN_VV=full CHUDWARE_LIBCHUDWARE=... pytest scripts/vv_benchmark.py -q

Exit code: 0 if all executed checks within tolerance (or skip-with-reason); 1 on failure.
Zero executed checks = failure (all-skip = failure).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Tolerance tiers
# ---------------------------------------------------------------------------
EXACT = 0.01       # < 0.01%  — same closed-form formula, should be machine-exact
PRECISE = 1.0      # < 1.0%   — validated analytic reference
ENGINEERING = 10.0  # < 10.0%  — engineering-grade estimate (rough friction factor etc.)
# TREND cases use direction/monotonicity checks, not a numeric tolerance


# ---------------------------------------------------------------------------
# Result table
# ---------------------------------------------------------------------------

class VVResult:
    """One benchmark case result."""
    __slots__ = ("family", "case", "tier", "verdict", "got", "ref", "pct_err",
                 "note", "citation", "executed", "solver_source", "solver_revision")

    def __init__(self, family: str, case: str, tier: str, verdict: str,
                 got: float | None, ref: float | None, pct_err: float | None,
                 note: str, citation: str, executed: bool = True,
                 solver_source: str | None = None,
                 solver_revision: str | None = None):
        self.family = family
        self.case = case
        self.tier = tier
        self.verdict = verdict
        self.got = got
        self.ref = ref
        self.pct_err = pct_err
        self.note = note
        self.citation = citation
        self.executed = executed
        self.solver_source = solver_source
        self.solver_revision = solver_revision

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "case": self.case,
            "tier": self.tier,
            "verdict": self.verdict,
            "got": self.got,
            "ref": self.ref,
            "pct_err": self.pct_err,
            "note": self.note,
            "citation": self.citation,
            "executed": self.executed,
            "solver_source": self.solver_source,
            "solver_revision": self.solver_revision,
        }


_RESULTS: list[VVResult] = []
_EXECUTED = 0
_BROKEN = 0


def _record(family: str, case: str, tier: str, verdict: str,
            got: float | None, ref: float | None, note: str, citation: str,
            executed: bool = True, *, solver_source: str | None = None,
            solver_revision: str | None = None) -> None:
    global _EXECUTED, _BROKEN
    pct = None
    if got is not None and ref is not None and ref != 0:
        pct = abs(got - ref) / abs(ref) * 100.0
    r = VVResult(family, case, tier, verdict, got, ref, pct, note, citation, executed,
                 solver_source, solver_revision)
    _RESULTS.append(r)
    if executed:
        _EXECUTED += 1
    if verdict == "FAIL":
        _BROKEN += 1
    sym = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP",
           "TREND-PASS": "TRND", "TREND-FAIL": "TFAL"}.get(verdict, verdict[:4])
    tag = "" if executed else " (skipped)"
    err_str = f" err={pct:.3f}%" if pct is not None else ""
    print(f"  [{sym}] {family}/{case}{err_str}: {note[:80]}{tag}")


def _skip(family: str, case: str, reason: str, citation: str = "") -> None:
    _record(family, case, "N/A", "SKIP", None, None, reason, citation, executed=False)


def _pct_err(got: float, ref: float) -> float:
    if ref == 0:
        return abs(got) * 100.0
    return abs(got - ref) / abs(ref) * 100.0


def _check(family: str, case: str, tier: str,
           got: float, ref: float, citation: str, note: str = "",
           tol: float | None = None, *, solver_source: str | None = None,
           solver_revision: str | None = None) -> None:
    """Record a numeric comparison against a named tolerance tier."""
    tol_map = {"EXACT": EXACT, "PRECISE": PRECISE, "ENGINEERING": ENGINEERING}
    tol_val = tol if tol is not None else tol_map[tier]
    err = _pct_err(got, ref)
    passed = err <= tol_val
    msg = (f"got={got:.6g}, ref={ref:.6g}, err={err:.3f}% "
           f"(tol={tol_val}%) {note}").strip()
    _record(family, case, tier, "PASS" if passed else "FAIL",
            got, ref, msg, citation, solver_source=solver_source,
            solver_revision=solver_revision)


# ---------------------------------------------------------------------------
# Native core access for the hydraulic / piping / ECAD families
# ---------------------------------------------------------------------------

def _native_core():
    """The C++ core bridge, or ``(None, reason)`` — scripts/sim_audit.py's policy: the
    explicitly selected CHUDWARE_LIBCHUDWARE, loaded in-process."""
    from scripts.sim_audit import _load_core_bridge
    return _load_core_bridge()


def _piping_run_doc(length_m: float, nps: float, schedule: str) -> dict:
    """An ``aicad.piping/1`` document holding one straight run along +X (path in mm)."""
    return {
        "schema": "aicad.piping/1", "id": "vv-pipe", "name": "VV",
        "units": {"system": "MMGS"}, "material": "A106-B",
        "runs": [{
            "id": "r0", "name": "main", "suppressed": False,
            "path": [[0, 0, 0], [int(length_m * 1000), 0, 0]],
            "nps": nps, "schedule": schedule, "material": None,
            "bend_radius": None, "end_type": "BW", "bore_mm": None,
        }],
        "fittings": [],
    }


def _piping_single_run(cb, length_m: float, nps: float, schedule: str,
                       head_m: float) -> dict:
    """``piping_solve`` on one run driven by ``head_m`` from its start to its end."""
    result = cb.call("piping_solve", {
        "doc": _piping_run_doc(length_m, nps, schedule),
        "boundary": [{"run": "r0", "end": "start", "head": head_m},
                     {"run": "r0", "end": "end", "head": 0.0}],
    })
    if not result.get("ok") or not result.get("converged"):
        raise RuntimeError(f"piping_solve failed to converge: {result}")
    return result


# ===========================================================================
# FAMILY 1: Hydraulic sizing (core hydraulic_size_* RPCs)
# Reference: first-principles hydraulics
# ===========================================================================

def bench_hydraulic() -> None:
    """Extended hydraulic benchmarks beyond sim_audit.py."""
    print("\n--- [hydraulic] Extended cylinder/pump sizing ---")
    cb, reason = _native_core()
    if cb is None:
        _skip("hydraulic", "H1-retract-force", reason, "Merritt 1967 §2.2")
        _skip("hydraulic", "H2-pump-displacement", reason, "Vickers Ind. Hyd. Manual 1992 ch.3")
        return
    try:
        # -- Case H1: Annular-area retract force (rod side) --
        # Citation: Merritt, H.E. "Hydraulic Control Systems" (1967) §2.2
        #   F_retract = (A_bore - A_rod) * P * η
        # bore=80mm, rod=50mm, P=160bar, η=0.95
        bore = 80.0; rod = 50.0; P = 160.0; eta = 0.95
        A_bore = math.pi / 4 * (bore * 1e-3) ** 2
        A_rod = math.pi / 4 * (rod * 1e-3) ** 2
        F_ret_ref = (A_bore - A_rod) * P * 1e5 * eta * 1e-3  # kN
        res = cb.call("hydraulic_size_cylinder", {
            "bore_mm": bore, "pressure_bar": P, "stroke_mm": 200,
            "velocity_mm_s": 30, "efficiency": eta, "rod_mm": rod})
        if res.get("ok") and "retract_force_kn" in res:
            _check("hydraulic", "H1-retract-force", "EXACT",
                   res["retract_force_kn"], F_ret_ref,
                   "Merritt 1967 §2.2: F_ret = (A_bore-A_rod)*P*η",
                   note=f"bore={bore}mm rod={rod}mm P={P}bar")
        else:
            _record("hydraulic", "H1-retract-force", "EXACT", "FAIL",
                    None, F_ret_ref, f"hydraulic_size_cylinder returned: {res}",
                    "Merritt 1967 §2.2")

        # -- Case H2: Pump volumetric efficiency & delivered flow --
        # Citation: Vickers Industrial Hydraulics Manual (1992) ch.3
        #   Q_actual = disp_cc_rev * rpm / 1000 * η_vol  (L/min)
        # With perfect η_vol=1.0, Q_actual = theoretical Q from size_pump
        Q_lpm = 30.0; dP = 180.0; rpm = 1000.0; eta2 = 0.88
        Q_m3s = Q_lpm / 60000.0
        disp_ref = Q_lpm * 1000.0 / rpm          # cc/rev
        shaft_ref = Q_m3s * dP * 1e5 / (eta2 * 1000.0)  # kW
        res2 = cb.call("hydraulic_size_pump", {
            "flow_lpm": Q_lpm, "pressure_bar": dP, "speed_rpm": rpm,
            "efficiency": eta2})
        if res2.get("ok"):
            _check("hydraulic", "H2-pump-displacement", "EXACT",
                   res2["displacement_cc_rev"], disp_ref,
                   "Vickers Ind. Hyd. Manual 1992 ch.3: disp=Q*1000/N",
                   note=f"Q={Q_lpm} L/min N={rpm} rpm")
            _check("hydraulic", "H2-pump-shaft-power", "EXACT",
                   res2["shaft_power_kw"], shaft_ref,
                   "Vickers Ind. Hyd. Manual 1992 ch.3: P_shaft=Q*dP/η",
                   note=f"dP={dP}bar η={eta2}")
        else:
            _record("hydraulic", "H2-pump-displacement", "EXACT", "FAIL",
                    None, disp_ref, f"hydraulic_size_pump returned not-ok: {res2}",
                    "Vickers Ind. Hyd. Manual 1992 ch.3")

    except Exception as exc:
        _record("hydraulic", "H1-retract-force", "N/A", "FAIL",
                None, None, f"runtime error: {exc}", "")
        traceback.print_exc()


# ===========================================================================
# FAMILY 2: Piping / Darcy-Weisbach (core piping_solve RPC)
# Reference: Moody chart, Colebrook-White equation
# ===========================================================================

def bench_piping() -> None:
    """Piping network solver benchmarks."""
    print("\n--- [piping] Darcy-Weisbach network ---")
    cb, reason = _native_core()
    if cb is None:
        _skip("piping", "P1-straight-pipe", reason, "Darcy-Weisbach: h_f = f*L/D*v²/2g")
        _skip("piping", "P2-length-scaling", reason)
        return
    try:
        # -- Case P1: Single straight pipe, turbulent regime --
        # Citation: White, F.M. "Fluid Mechanics" 8th ed §8.4 (public-domain formula)
        #   Moody friction factor f_T ≈ 0.316 * Re^-0.25  (Blasius, smooth pipe)
        #   h_f = f * (L/D) * v²/(2g)   →   v = sqrt(h_f * 2g * D / (f * L))
        #   Q = A * v
        # Params: dH=5m, L=50m, DN25 (D≈0.027m), ρ=1000, g=9.81
        D_p1 = 0.027; L_p1 = 50.0; dH_p1 = 5.0; g = 9.81
        # Rough turbulent estimate with f ≈ 0.03 (from Moody for Re~50k, smooth)
        f_approx = 0.03
        v_approx = math.sqrt(dH_p1 * 2 * g * D_p1 / (f_approx * L_p1))
        Q_ref_p1 = math.pi / 4 * D_p1 ** 2 * v_approx * 3600.0  # m³/h

        r_p1 = _piping_single_run(cb, L_p1, 1.0, "40", dH_p1)  # 1" NPS ≈ DN25
        if r_p1.get("ok") and r_p1.get("links"):
            Q_got_p1 = r_p1["links"][0]["flow_m3h"]
            _check("piping", "P1-straight-pipe", "ENGINEERING",
                   Q_got_p1, Q_ref_p1,
                   "White 2011 §8.4: h_f=f(L/D)(v²/2g), Blasius f",
                   note=f"dH={dH_p1}m L={L_p1}m DN25")
        else:
            _record("piping", "P1-straight-pipe", "ENGINEERING", "FAIL",
                    None, Q_ref_p1,
                    f"solve returned not-ok or no links: {r_p1.get('reason')}",
                    "White 2011 §8.4")

        # -- Case P2: Longer run — head loss scales linearly with L at same Q --
        # Citation: Darcy-Weisbach proportionality: for fixed f,D,v: h_f ∝ L
        #   Doubling L at fixed dH reduces Q by roughly 1/√2 (constant-f estimate).
        #   Ratio: Q(2L) / Q(L) ≈ 1/sqrt(2)
        r_p2 = _piping_single_run(cb, L_p1 * 2, 1.0, "40", dH_p1)  # 100m instead of 50m
        if (r_p1.get("ok") and r_p1.get("links") and
                r_p2.get("ok") and r_p2.get("links")):
            Q_p2 = r_p2["links"][0]["flow_m3h"]
            Q_p1 = r_p1["links"][0]["flow_m3h"]
            ratio_got = Q_p2 / Q_p1
            ratio_ref = 1.0 / math.sqrt(2.0)  # ≈ 0.7071
            err = _pct_err(ratio_got, ratio_ref)
            passed = err < ENGINEERING  # 10% tolerance on the ratio
            _record("piping", "P2-length-scaling", "ENGINEERING",
                    "PASS" if passed else "FAIL",
                    ratio_got, ratio_ref,
                    f"Q ratio (2L/L) = {ratio_got:.4f} (ref 1/√2={ratio_ref:.4f}, err={err:.1f}%)",
                    "Darcy-Weisbach: Q∝√(dH/L) → Q(2L)/Q(L)=1/√2")
        else:
            _record("piping", "P2-length-scaling", "ENGINEERING", "FAIL",
                    None, 1.0 / math.sqrt(2.0),
                    f"P1 or P2 solve failed: P1={r_p1}, P2={r_p2}",
                    "Darcy-Weisbach: Q∝√(dH/L)")

    except Exception as exc:
        _record("piping", "P1-straight-pipe", "N/A", "FAIL",
                None, None, f"runtime error: {exc}", "")
        traceback.print_exc()


# ===========================================================================
# FAMILY 3: ECAD impedance (IPC-2141A closed-form, core ecad_impedance RPC)
# These are the same formula the engine implements — tolerance is EXACT
# ===========================================================================

def _impedance(cb, mode: str, **geometry: Any) -> dict:
    """One ``ecad_impedance`` call — the RPC behind ecad_check_impedance."""
    return cb.call("ecad_impedance", {"mode": mode, **geometry})


def bench_ecad_impedance() -> None:
    """ECAD PCB trace impedance benchmarks."""
    print("\n--- [ecad-impedance] IPC-2141A cases ---")
    cb, reason = _native_core()
    if cb is None:
        for case in ("I1-microstrip-narrow", "I2-microstrip-wide", "I3-stripline",
                     "I4-diff-pair"):
            _skip("ecad-impedance", case, reason, "IPC-2141A §6")
        return
    try:
        # -- Case I1: Microstrip (narrow trace) --
        # Citation: IPC-2141A §6.2 (public-domain closed-form)
        #   Z0 = 87/√(εr+1.41) × ln(5.98h/(0.8w+t))
        w, h, t, er = 0.5, 1.6, 0.035, 4.5
        ref_i1 = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * w + t))
        res_i1 = _impedance(cb, "microstrip", w=w, h=h, t=t, er=er)
        _check("ecad-impedance", "I1-microstrip-narrow", "EXACT",
               res_i1["z0"], ref_i1,
               "IPC-2141A §6.2: Z0=87/√(εr+1.41)×ln(5.98h/(0.8w+t))",
               note=f"w={w}mm h={h}mm t={t}mm er={er}")

        # -- Case I2: Microstrip (wide trace, 50Ω target near 3mm) --
        # Citation: IPC-2141A §6.2 same formula, different geometry
        w2, h2, t2, er2 = 3.0, 1.6, 0.035, 4.5
        ref_i2 = (87.0 / math.sqrt(er2 + 1.41)) * math.log(5.98 * h2 / (0.8 * w2 + t2))
        res_i2 = _impedance(cb, "microstrip", w=w2, h=h2, t=t2, er=er2)
        _check("ecad-impedance", "I2-microstrip-wide", "EXACT",
               res_i2["z0"], ref_i2,
               "IPC-2141A §6.2",
               note=f"w={w2}mm h={h2}mm → ~50Ω standard trace")

        # -- Case I3: Stripline (symmetric) --
        # Citation: IPC-2141A §6.3: Z0 = 60/√εr × ln(4h/(0.67π(0.8w+t)))
        w3, h3, t3, er3 = 2.0, 3.2, 0.035, 4.5
        ref_i3 = (60.0 / math.sqrt(er3)) * math.log(
            4.0 * h3 / (0.67 * math.pi * (0.8 * w3 + t3)))
        res_i3 = _impedance(cb, "stripline", w=w3, h=h3, t=t3, er=er3)
        _check("ecad-impedance", "I3-stripline", "EXACT",
               res_i3["z0"], ref_i3,
               "IPC-2141A §6.3: Z0=60/√εr×ln(4h/(0.67π(0.8w+t)))",
               note=f"w={w3}mm h={h3}mm er={er3}")

        # -- Case I4: Differential pair coupling (de-rating factor) --
        # Citation: IPC-2141A §6.4 empirical:
        #   Z_diff = 2·Z0_se·(1 - 0.48·exp(-0.96·s/h))
        s, h4 = 0.2, 1.6
        Z0_se = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h4 / (0.8 * w + t))
        ref_i4 = 2.0 * Z0_se * (1.0 - 0.48 * math.exp(-0.96 * s / h4))
        res_i4 = _impedance(cb, "diff_pair", w=w, s=s, h=h4, t=t, er=er, kind="microstrip")
        _check("ecad-impedance", "I4-diff-pair", "EXACT",
               res_i4["z0_diff"], ref_i4,
               "IPC-2141A §6.4: Z_diff=2·Z0_se·(1-0.48·exp(-0.96·s/h))",
               note=f"s={s}mm h={h4}mm")

    except Exception as exc:
        _record("ecad-impedance", "I1-microstrip-narrow", "N/A", "FAIL",
                None, None, f"runtime error: {exc}", "")
        traceback.print_exc()


# ===========================================================================
# FAMILY 4: ECAD circuit simulation (core sim_spice native MNA RPC)
# Reference: classical circuit theory (Hayt & Kemmerly, Sedra & Smith)
# ===========================================================================

def _spice(cb, circuit: dict, study: dict) -> dict:
    """One native MNA run through ``sim_spice`` (sim.md result shape)."""
    return cb.call("sim_spice", {"circuit": circuit, "study": study})


def bench_ecad_circuit() -> None:
    """Circuit simulation accuracy benchmarks."""
    print("\n--- [ecad-circuit] MNA solver accuracy ---")
    cb, reason = _native_core()
    if cb is None:
        for case in ("C1-RC-lowpass-corner", "C2-RLC-resonance-frequency",
                     "C3-thevenin-load"):
            _skip("ecad-circuit", case, reason, "Hayt&Kemmerly 8th")
        return
    try:
        import numpy as np

        def _make_rlc_circuit(R, L, C, V=1.0):
            """Series RLC driven by an AC source."""
            return {
                "nodes": ["0", "IN", "N1", "OUT"],
                "devices": [
                    {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                     "value": 0.0, "params": {"ac": {"mag": V, "phase": 0.0}}},
                    {"kind": "R", "name": "R1", "nodes": ["IN", "N1"],
                     "value": R, "params": {}},
                    {"kind": "L", "name": "L1", "nodes": ["N1", "OUT"],
                     "value": L, "params": {}},
                    {"kind": "C", "name": "C1", "nodes": ["OUT", "0"],
                     "value": C, "params": {}},
                ],
                "models": {}, "params": {"TEMP": 27},
            }

        # -- Case C1: RC low-pass at -3dB corner (|H|=1/√2) --
        # Citation: Hayt & Kemmerly "Engineering Circuit Analysis" 8th ed §14.5
        #   fc = 1/(2πRC); at f=fc: |H(jωc)| = 1/√2
        R_c1, C_c1 = 1000.0, 159.155e-9
        fc_c1 = 1.0 / (2 * math.pi * R_c1 * C_c1)
        circ_c1 = {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": 0.0, "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
                 "value": R_c1, "params": {}},
                {"kind": "C", "name": "C1", "nodes": ["OUT", "0"],
                 "value": C_c1, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study_c1 = {
            "analyses": [{"type": "ac", "sweep": "dec", "points": 30,
                          "fstart": 10, "fstop": 1e6}],
            "probes": ["V(OUT)"],
        }
        res_c1 = _spice(cb, circ_c1, study_c1)
        if res_c1.get("convergence", {}).get("ac"):
            f = np.array(res_c1["ac"]["x_f"])
            mag = np.array(res_c1["ac"]["mag"]["V(OUT)"])
            H_got = float(np.interp(fc_c1, f, mag))
            _check("ecad-circuit", "C1-RC-lowpass-corner", "PRECISE",
                   H_got, 1.0 / math.sqrt(2.0),
                   "Hayt&Kemmerly 8th §14.5: |H(jωc)|=1/√2 at f=1/(2πRC)",
                   note=f"R={R_c1}Ω C={C_c1*1e9:.3f}nF fc={fc_c1:.0f}Hz")
        else:
            _record("ecad-circuit", "C1-RC-lowpass-corner", "PRECISE", "FAIL",
                    None, 1.0 / math.sqrt(2.0),
                    f"AC analysis did not converge: {res_c1.get('log', [])}",
                    "Hayt&Kemmerly 8th §14.5")

        # -- Case C2: Series RLC resonance — peak at ω0 = 1/√(LC) --
        # Citation: Sedra & Smith "Microelectronic Circuits" 6th ed §1.5
        #   ω0 = 1/√(LC), at ω=ω0 the impedance is purely resistive (min)
        #   |V(OUT)/V(IN)| ≈ 1.0 at resonance (voltage across C in series RLC
        #   can exceed 1.0 for high Q, but |V_in| is the source magnitude)
        # We check that the series impedance minimum is at ω0 (frequency of peak
        # current = peak voltage across R1).
        R_c2 = 10.0; L_c2 = 1e-3; C_c2 = 1e-6
        f0_c2 = 1.0 / (2 * math.pi * math.sqrt(L_c2 * C_c2))
        circ_c2 = _make_rlc_circuit(R_c2, L_c2, C_c2, V=1.0)
        study_c2 = {
            "analyses": [{"type": "ac", "sweep": "dec", "points": 50,
                          "fstart": 100, "fstop": 1e5}],
            "probes": ["V(N1)", "V(OUT)"],
        }
        res_c2 = _spice(cb, circ_c2, study_c2)
        if res_c2.get("convergence", {}).get("ac"):
            f2 = np.array(res_c2["ac"]["x_f"])
            # V(N1)-V(OUT) is the voltage across L1; V(IN)-V(N1) is across R1.
            # At resonance, current is maximum → V(R1)=V(IN)-V(N1) is maximum.
            # Series voltage across R: V_R = |V_in| at resonance (all voltage on R).
            # Simpler: magnitude of V(OUT) at resonance is Q * V_source (bandpass).
            # Check: peak of V(OUT) magnitude occurs at f0.
            mag_out2 = np.array(res_c2["ac"]["mag"]["V(OUT)"])
            f_peak = float(f2[np.argmax(mag_out2)])
            err_freq = _pct_err(f_peak, f0_c2)
            passed = err_freq < ENGINEERING  # peak should be within 10% of f0
            _record("ecad-circuit", "C2-RLC-resonance-frequency", "ENGINEERING",
                    "PASS" if passed else "FAIL",
                    f_peak, f0_c2,
                    f"peak freq={f_peak:.1f}Hz ref f0={f0_c2:.1f}Hz err={err_freq:.1f}%",
                    "Sedra&Smith 6th §1.5: ω0=1/√(LC), series RLC peak at ω0")
        else:
            _record("ecad-circuit", "C2-RLC-resonance-frequency", "ENGINEERING", "FAIL",
                    None, f0_c2, f"AC analysis did not converge: {res_c2}",
                    "Sedra&Smith 6th §1.5")

        # -- Case C3: Thevenin equivalent — superposition DC --
        # Citation: Hayt & Kemmerly 8th §5.3 (Thevenin theorem)
        #   V_TH = V_s * R2/(R1+R2); R_TH = R1||R2
        #   With load R_L: V(LOAD) = V_TH * R_L / (R_TH + R_L)
        R1_c3, R2_c3, R_L_c3, V_s = 1000.0, 2000.0, 3000.0, 12.0
        V_th = V_s * R2_c3 / (R1_c3 + R2_c3)
        R_th = R1_c3 * R2_c3 / (R1_c3 + R2_c3)
        V_load_ref = V_th * R_L_c3 / (R_th + R_L_c3)
        # Circuit: V_s into R1 → MID, then R2 ∥ R_L from MID to GND.
        # This directly computes the Thevenin-loaded voltage at MID.
        # Topology: R2 and R_L both connect MID to 0 (parallel from MID to GND).
        circ_c3 = {
            "nodes": ["0", "IN", "MID"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": V_s, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "MID"],
                 "value": R1_c3, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["MID", "0"],
                 "value": R2_c3, "params": {}},
                {"kind": "R", "name": "RL", "nodes": ["MID", "0"],
                 "value": R_L_c3, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study_c3 = {"analyses": [{"type": "op"}], "probes": ["V(MID)"]}
        res_c3 = _spice(cb, circ_c3, study_c3)
        if res_c3.get("convergence", {}).get("op"):
            _check("ecad-circuit", "C3-thevenin-load", "EXACT",
                   res_c3["op"]["V(MID)"], V_load_ref,
                   "Hayt&Kemmerly 8th §5.3: V_load=V_th*R_L/(R_th+R_L)",
                   note=f"V_th={V_th:.3f}V R_th={R_th:.1f}Ω")
        else:
            _record("ecad-circuit", "C3-thevenin-load", "EXACT", "FAIL",
                    None, V_load_ref,
                    f"DC OP did not converge: {res_c3.get('log', [])}",
                    "Hayt&Kemmerly 8th §5.3")

    except ImportError as exc:
        _skip("ecad-circuit", "C1-RC-lowpass-corner",
              f"numpy not available: {exc}",
              "Hayt&Kemmerly 8th")
    except Exception as exc:
        _record("ecad-circuit", "C1-RC-lowpass-corner", "N/A", "FAIL",
                None, None, f"runtime error: {exc}", "")
        traceback.print_exc()


# ===========================================================================
# FAMILY 5: Structural frame analysis (PyNite)
# Reference: Euler-Bernoulli beam theory (Hibbeler, Roark)
# ===========================================================================

def bench_structural() -> None:
    """Structural frame analysis benchmarks using PyNite."""
    print("\n--- [structural] PyNite frame benchmarks ---")
    try:
        import Pynite as _  # noqa: F401
    except ImportError:
        _skip("structural", "S1-cantilever-deflection",
              "PyNite not installed — pip install PyNiteFEA (MIT)",
              "Euler-Bernoulli beam theory")
        _skip("structural", "S2-simply-supported-UDL",
              "PyNite not installed")
        _skip("structural", "S3-fixed-fixed-point-load",
              "PyNite not installed")
        _skip("structural", "S4-two-span-continuous",
              "PyNite not installed")
        return

    try:
        from Pynite import FEModel3D

        E = 200000.0; G = 77000.0  # MPa (steel)
        rho = 7.85e-3  # g/mm³

        def _steel_rect_model(b, h, L_mm, loads_fn):
            """Build a FEModel3D with a rect section beam along +X."""
            A = b * h
            # FZ load along global Z bends about local y.
            # For member along X: local z (weak) = global Y, local y (strong) = global Z.
            # FZ (global) bends about global Y = local z.
            # CORRECTION: PyNite convention confirmed in sim_audit.py local-axis note:
            # member along +X has local y = global Z, local z = global Y.
            # FZ load → bending about local z → uses Iz (= h*b³/12, weak for b<h).
            # Wait — per axis audit in sim_audit.py: FZ bends about LOCAL y → uses Iy.
            # Confirmed: add_node_load("B","FZ",-P) → uses Iy = b*h³/12 (stiff about Y).
            Iy = b * h ** 3 / 12.0  # bending about local y (FZ load direction)
            Iz = h * b ** 3 / 12.0  # bending about local z (FY load direction)
            J = 0.141 * b * h ** 3 / (b / h) if b < h else 0.141 * h * b ** 3 / (h / b)
            m = FEModel3D()
            m.add_material("steel", E=E, G=G, nu=0.3, rho=rho)
            m.add_section("sec", A, Iy, Iz, J)
            m.add_node("A", 0, 0, 0)
            m.add_node("B", L_mm, 0, 0)
            m.add_member("m1", "A", "B", "steel", "sec")
            loads_fn(m)
            return m, Iy, Iz

        # -- Case S1: Cantilever tip deflection under point load --
        # Citation: Roark's "Formulas for Stress and Strain" 8th ed Table 8.1 Case 1a
        #   δ_tip = P·L³ / (3·E·I)
        b1, h1, L1, P1 = 50.0, 200.0, 3000.0, 5000.0
        I1 = b1 * h1 ** 3 / 12.0  # Iy = b*h³/12 for FZ load
        delta_ref_s1 = P1 * L1 ** 3 / (3.0 * E * I1)

        def _loads_s1(m):
            m.def_support("A", True, True, True, True, True, True)
            m.add_node_load("B", "FZ", -P1)

        m1, Iy1, _ = _steel_rect_model(b1, h1, L1, _loads_s1)
        m1.analyze(check_statics=False)
        delta_got_s1 = abs(m1.nodes["B"].DZ["Combo 1"])
        _check("structural", "S1-cantilever-tip-deflection", "PRECISE",
               delta_got_s1, delta_ref_s1,
               "Roark 8th Table 8.1 Case 1a: δ=PL³/(3EI)",
               note=f"{b1}×{h1}mm L={L1}mm P={P1}N")

        # -- Case S2: Simply supported beam, UDL (uniform distributed load) --
        # Citation: Roark 8th Table 8.1 Case 2a
        #   δ_max = 5·w·L⁴ / (384·E·I)  at midspan
        b2, h2, L2 = 100.0, 200.0, 6000.0
        w2 = 2.0  # N/mm uniform load
        I2 = b2 * h2 ** 3 / 12.0
        delta_ref_s2 = 5.0 * w2 * L2 ** 4 / (384.0 * E * I2)

        def _loads_s2(m):
            m.def_support("A", True, True, True, False, False, False)
            m.def_support("B", True, True, True, False, False, False)  # pin-pin
            m.add_member_dist_load("m1", "FZ", -w2, -w2)  # uniform

        m2, _, _ = _steel_rect_model(b2, h2, L2, _loads_s2)
        m2.analyze(check_statics=False)
        # Midspan deflection via midpoint node
        m2.add_node("C", L2 / 2, 0, 0)
        # Re-run with midspan node
        m2b = FEModel3D()
        m2b.add_material("steel", E=E, G=G, nu=0.3, rho=rho)
        A2 = b2 * h2; Iy2 = I2; Iz2 = h2 * b2 ** 3 / 12.0
        J2 = 0.141 * b2 * h2 ** 3 / (b2 / h2)
        m2b.add_section("sec", A2, Iy2, Iz2, J2)
        m2b.add_node("A", 0, 0, 0)
        m2b.add_node("MID", L2 / 2, 0, 0)
        m2b.add_node("B", L2, 0, 0)
        m2b.add_member("mL", "A", "MID", "steel", "sec")
        m2b.add_member("mR", "MID", "B", "steel", "sec")
        m2b.def_support("A", True, True, True, False, False, False)
        m2b.def_support("B", True, True, True, False, False, False)
        for mem in ("mL", "mR"):
            m2b.add_member_dist_load(mem, "FZ", -w2, -w2)
        m2b.analyze(check_statics=False)
        delta_got_s2 = abs(m2b.nodes["MID"].DZ["Combo 1"])
        _check("structural", "S2-simply-supported-UDL", "PRECISE",
               delta_got_s2, delta_ref_s2,
               "Roark 8th Table 8.1 Case 2a: δ=5wL⁴/(384EI)",
               note=f"{b2}×{h2}mm L={L2}mm w={w2}N/mm")

        # -- Case S3: Fixed-fixed beam, point load at midspan --
        # Citation: Roark 8th Table 8.1 Case 3b
        #   δ_max = P·L³ / (192·E·I)  at midspan (fixed-fixed symmetry)
        b3, h3, L3, P3 = 80.0, 160.0, 4000.0, 10000.0
        I3 = b3 * h3 ** 3 / 12.0
        delta_ref_s3 = P3 * L3 ** 3 / (192.0 * E * I3)

        m3 = FEModel3D()
        m3.add_material("steel", E=E, G=G, nu=0.3, rho=rho)
        A3 = b3 * h3; Iy3 = I3; Iz3 = h3 * b3 ** 3 / 12.0
        J3 = 0.141 * b3 * h3 ** 3 / (b3 / h3)
        m3.add_section("sec", A3, Iy3, Iz3, J3)
        m3.add_node("A", 0, 0, 0)
        m3.add_node("MID", L3 / 2, 0, 0)
        m3.add_node("B", L3, 0, 0)
        m3.add_member("mL", "A", "MID", "steel", "sec")
        m3.add_member("mR", "MID", "B", "steel", "sec")
        # Fixed-fixed: all 6 DOF pinned at both ends
        m3.def_support("A", True, True, True, True, True, True)
        m3.def_support("B", True, True, True, True, True, True)
        m3.add_node_load("MID", "FZ", -P3)
        m3.analyze(check_statics=False)
        delta_got_s3 = abs(m3.nodes["MID"].DZ["Combo 1"])
        _check("structural", "S3-fixed-fixed-midspan", "PRECISE",
               delta_got_s3, delta_ref_s3,
               "Roark 8th Table 8.1 Case 3b: δ=PL³/(192EI)",
               note=f"{b3}×{h3}mm L={L3}mm P={P3}N")

        # -- Case S4: Two-span continuous beam (method of three moments) --
        # Citation: Hibbeler "Structural Analysis" 9th ed §12.1 (three-moment equation)
        #   Equal spans L, symmetric UDL w.
        #   Three-moment eq → M_B = -wL²/8 at interior support.
        #   R_A = R_C = 3wL/8,  R_B = 5wL/4.
        #
        # Analytic deflection in span 1 (0≤x≤L), derived by double integration
        # of M(x) = R_A·x − wx²/2 (moment from left equilibrium):
        #   EI·y(x) = R_A·x³/6 − wx⁴/24 + C1·x   (C2=0 from y(0)=0)
        #   C1 = −wL³/48  (from y(L)=0)
        #
        # Maximum at root of dy/dx=0: solves 4u³ − 9u² + 1 = 0 (u=x/L)
        # Positive real root in (0,1): u ≈ 0.3641, x_max ≈ 0.3641·L
        # δ_max ≈ 0.005301·wL⁴/(EI)  (derived; matches Roark 8th Table 8.14 row 8a
        #         listed as 0.0054wL⁴/EI — slight diff due to rounding in tables)
        # Tolerance: ENGINEERING (10%) because max location between mesh nodes
        # means FEM slightly overestimates; expect <3% with fine mesh.
        b4, h4, L4 = 100.0, 200.0, 3000.0
        w4 = 1.0  # N/mm
        I4 = b4 * h4 ** 3 / 12.0
        # Exact analytic max: 0.005301·wL⁴/(EI)
        delta_ref_s4 = 0.005301 * w4 * L4 ** 4 / (E * I4)

        m4 = FEModel3D()
        m4.add_material("steel", E=E, G=G, nu=0.3, rho=rho)
        A4 = b4 * h4; Iy4 = I4; Iz4 = h4 * b4 ** 3 / 12.0
        J4 = 0.141 * b4 * h4 ** 3 / (b4 / h4)
        m4.add_section("sec", A4, Iy4, Iz4, J4)
        # Three supports: A (x=0), B (x=L), C (x=2L).
        # Include the max-deflection point at x=3L/4 (=2250mm for L=3000mm).
        # Note on node placement: PyNite's deflection curve for this two-span model
        # has its span-1 maximum at x=L/4 from A in PyNite's internal ordering,
        # which corresponds to the analytic maximum at x=3L/4 from A (x=2250mm).
        # Use a fine mesh (8 segments per span) and take the absolute maximum.
        n_seg = 8
        node_names = []
        for i in range(2 * n_seg + 1):
            x = i * L4 / n_seg
            nm = f"N{i}"
            m4.add_node(nm, x, 0, 0)
            node_names.append(nm)
        for i in range(2 * n_seg):
            m4.add_member(f"m{i}", node_names[i], node_names[i + 1], "steel", "sec")
            m4.add_member_dist_load(f"m{i}", "FZ", -w4, -w4)
        # Supports at A (i=0), B (i=n_seg), C (i=2*n_seg)
        m4.def_support("N0", True, True, True, False, False, False)
        m4.def_support(f"N{n_seg}", True, True, True, False, False, False)
        m4.def_support(f"N{2 * n_seg}", True, True, True, False, False, False)
        m4.analyze(check_statics=False)
        # Take the maximum absolute deflection across all non-support nodes
        delta_got_s4 = max(
            abs(m4.nodes[nm].DZ["Combo 1"])
            for i, nm in enumerate(node_names)
            if i not in (0, n_seg, 2 * n_seg)
        )
        _check("structural", "S4-two-span-continuous-UDL", "ENGINEERING",
               delta_got_s4, delta_ref_s4,
               "Hibbeler 9th §12.1 + double integration: δ_max≈0.005301wL⁴/(EI) at x≈0.364L",
               note=f"{b4}×{h4}mm L={L4}mm w={w4}N/mm max~x=0.364L")

    except Exception as exc:
        _record("structural", "S1-cantilever-tip-deflection", "N/A", "FAIL",
                None, None, f"PyNite analysis error: {exc}", "")
        traceback.print_exc()


# ===========================================================================
# Shared in-process full-MCP-server dispatch for the C++ FEA cases below.
#
# sim_run_static / sim_run_modal are C++ plugins that expect a pre-resolved "study"
# dict (mesh, fixed-node ids, nodal loads). Building that dict requires the face-query
# resolution (_resolve_face_points, doc_export_step, ...) that lives in the registered
# MCP tools (run_fea_static / run_fea_modal) — raw core_bridge.call("sim_run_static", ...)
# cannot do this itself. So we call the FULL FastMCP server in-process, exactly like
# services/mcp/test_roundtrip.py and services/mcp/test_cad_tools.py do (services.mcp.server.mcp
# .call_tool), never a bare core_bridge FFI call and never a real subprocess (the "full MCP
# server" distinction is tool-registration-vs-raw-FFI, not process boundary).
# ===========================================================================

def _mcp_call(name: str, args: dict) -> dict:
    """Call a registered MCP tool in-process through the full FastMCP server."""
    import asyncio
    from services.mcp import server
    _content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    if isinstance(text, str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": "non-JSON MCP result"}
    return {"ok": False, "error": "no MCP result payload"}


def _fea_ready() -> tuple[bool, str]:
    """Whether the C++ engine is staged and reachable through services.mcp.core_bridge."""
    lib_path = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
    if not lib_path or not os.path.exists(lib_path):
        return False, ("CHUDWARE_LIBCHUDWARE not set — C++ FEA is only reachable through the "
                        "full MCP server (run_fea_static/run_fea_modal)")
    try:
        from services.mcp import core_bridge as cb
    except ImportError as exc:
        return False, f"services.mcp import failed: {exc}"
    if not cb.available():
        return False, "core_bridge reports libchudware not loadable"
    return True, ""


def _missing_fea_capability(error) -> bool:
    """True when the staged .so genuinely lacks the FEA plugin (plain build-linux has
    no OCCT geometry kernel — see scripts/gate.sh stage_modeling's note on this same
    build-linux-vs-build-occt-linux split), as opposed to a real solve-time failure."""
    return bool(error) and "unknown method" in str(error)

def _polymesh_provenance() -> tuple[str, str]:
    """Identify the exact linked solver binary and pinned mesher source."""
    lib_value = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
    binary_revision = "not-present"
    if lib_value and Path(lib_value).is_file():
        digest = hashlib.sha256()
        with Path(lib_value).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        binary_revision = digest.hexdigest()

    pin = os.environ.get("CHUDWARE_POLYMESH_REVISION", "").strip()
    if not pin:
        provision = _PROJECT_ROOT / "core" / "cmake" / "ProvisionPolymesh.cmake"
        if provision.exists():
            for line in provision.read_text(encoding="utf-8").splitlines():
                if "set(POLYMESH_COMMIT" in line and '"' in line:
                    pin = line.split('"', 2)[1]
                    break
    source = "Chudware core tet FEA + pinned Polyhedral-FEA polymesh mesher"
    revision = f"polymesh-git:{pin or 'unresolved'};libchudware-sha256:{binary_revision}"
    return source, revision


def _missing_polymesh_capability(error: Any) -> bool:
    text = str(error or "").lower()
    return ("polymesh" in text
            and any(marker in text for marker in
                    ("not available", "unavailable", "configured without", "unknown method")))


# Explicit dict (not a named preset) so the analytic reference and the solver always
# agree on the exact mechanical properties. NOTE density_g_cm3 is g/cm³ (steel ~7.85),
# not g/mm³ — this bit us during calibration (a 1000x density error silently manifests
# as a sqrt(1000)~31.6x modal-frequency error, since f∝1/√ρ).
_VV_STEEL = {"name": "vv-steel", "E_mpa": 200000.0, "nu": 0.3,
             "density_g_cm3": 7.85, "yield_mpa": 250.0}


def _build_box_part(name: str, length: float, width: float, height: float) -> str:
    """Build a single-box part via the full MCP server (X=length, Y=width, Z=height —
    core/doc/document.cpp:6717); returns doc_id."""
    from services.mcp import core_bridge as cb
    r = _mcp_call("new_part", {"name": name})
    if "doc_id" not in r:
        raise RuntimeError(f"new_part failed: {r}")
    doc = r["doc_id"]
    ops = [cb.feat("box", "Beam", {"length": cb.dim(length), "width": cb.dim(width),
                                    "height": cb.dim(height)})]
    r2 = _mcp_call("add_features", {"ops": ops, "doc_id": doc})
    if not r2.get("ok"):
        raise RuntimeError(f"add_features failed: {r2}")
    return doc


def _fea1_point_load_case() -> dict:
    """FEA1: cantilever tip deflection, square section, tip point load."""
    L, b, h, P = 1000.0, 50.0, 50.0, 1000.0
    E = _VV_STEEL["E_mpa"]
    I = b * h ** 3 / 12.0
    ref = P * L ** 3 / (3.0 * E * I)
    doc = _build_box_part("vv_fea1", L, b, h)
    r = _mcp_call("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -P]}],
        "material": _VV_STEEL, "doc_id": doc,
    })
    got = r.get("summary", {}).get("max_disp_mm") if r.get("ok") else None
    return {"ok": r.get("ok", False), "got": got, "ref": ref, "error": r.get("error"),
            "note": f"{b:.0f}×{h:.0f}mm L={L:.0f}mm P={P:.0f}N tip force, tet10 mesh"}


def _fea2_rect_section_case() -> dict:
    """FEA2: cantilever tip deflection, rectangular (b≠h) section, strong-axis point
    load. A distinct cross-section from FEA1 catches bending-axis / moment-of-inertia
    mix-ups that a square section can't (I is orientation-independent when b=h)."""
    L, b, h, P = 1000.0, 20.0, 80.0, 1000.0
    E = _VV_STEEL["E_mpa"]
    I = b * h ** 3 / 12.0  # bending about the FZ load axis (h = depth in load direction)
    ref = P * L ** 3 / (3.0 * E * I)
    doc = _build_box_part("vv_fea2", L, b, h)
    r = _mcp_call("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -P]}],
        "material": _VV_STEEL, "doc_id": doc,
    })
    got = r.get("summary", {}).get("max_disp_mm") if r.get("ok") else None
    return {"ok": r.get("ok", False), "got": got, "ref": ref, "error": r.get("error"),
            "note": f"{b:.0f}×{h:.0f}mm L={L:.0f}mm P={P:.0f}N tip force "
                    f"(deep section, strong-axis bend)"}


def _fea3_uniform_pressure_case() -> dict:
    """FEA3: cantilever tip deflection under a uniform top-face pressure (Roark UDL case).

    FINDING, now CLOSED: this case surfaced a real defect in the C++
    pressure-load path (core/sim/fea_run.cpp, the `type == "pressure"` branch of
    fea_run_static), which lumped the whole face load onto corner nodes and read ~10x
    high, non-convergent with mesh density. The deposit is now the work-consistent
    f_i = -p*integral(N_i n_outward)dA (tri6 quadrature on tet10), so the resultant is
    p*A along the INWARD face normal and this row converges to Roark. The load is a
    downward UDL because +p pushes INTO the top face: `pressure` is "MPa, inward"
    (pressure-sign fix, 2026-09-05). Do not loosen the tolerance here.
    """
    L, b, h, p = 1000.0, 50.0, 50.0, 0.1  # MPa uniform top-face pressure
    E = _VV_STEEL["E_mpa"]
    I = b * h ** 3 / 12.0
    w = p * b  # N/mm equivalent line load
    ref = w * L ** 4 / (8.0 * E * I)
    doc = _build_box_part("vv_fea3", L, b, h)
    r = _mcp_call("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "top"}, "pressure": p}],
        "material": _VV_STEEL, "doc_id": doc,
    })
    got = r.get("summary", {}).get("max_disp_mm") if r.get("ok") else None
    return {"ok": r.get("ok", False), "got": got, "ref": ref, "error": r.get("error"),
            "note": f"{b:.0f}×{h:.0f}mm L={L:.0f}mm p={p}MPa uniform top pressure "
                    f"(w={w:.1f}N/mm, downward: +p acts INWARD on the top face)"}


# ===========================================================================
# FAMILY 6: Modal frequency — Euler-Bernoulli cantilever
# Reference: vibration theory (Rao "Mechanical Vibrations" 5th ed §8.2 Table 8.1)
# ===========================================================================

_BETA1 = 1.87510  # first root of cosh(β₁L)cos(β₁L) = -1, i.e. the cantilever B.C.


def _modal_case(name: str, L: float, b: float, h: float, weak_axis: bool) -> dict:
    """First bending-mode frequency, executed via run_fea_modal + an independent
    Euler-Bernoulli analytic reference. weak_axis=True uses I=h·b³/12 (the SMALLER
    moment of inertia governs the lowest/first mode when b≠h — a b=h square section
    doesn't distinguish this, hence a second, asymmetric case)."""
    E, nu, rho = _VV_STEEL["E_mpa"], _VV_STEEL["nu"], _VV_STEEL["density_g_cm3"]
    I_mm4 = (h * b ** 3 if weak_axis else b * h ** 3) / 12.0
    I = I_mm4 * 1e-12  # mm^4 -> m^4
    A = b * h * 1e-6  # mm^2 -> m^2
    Lm = L * 1e-3  # mm -> m
    rho_si = rho * 1000.0  # g/cm^3 -> kg/m^3
    E_si = E * 1e6  # MPa -> Pa
    ref = (_BETA1 ** 2 / (2 * math.pi * Lm ** 2)) * math.sqrt(E_si * I / (rho_si * A))
    doc = _build_box_part(name, L, b, h)
    r = _mcp_call("run_fea_modal", {
        "fixtures": [{"of": "face", "side": "left"}],
        "material": _VV_STEEL, "doc_id": doc, "n_modes": 4,
    })
    freqs = r.get("freqs_hz") if r.get("ok") else None
    got = freqs[0] if freqs else None
    return {"ok": r.get("ok", False), "got": got, "ref": ref, "error": r.get("error"),
            "freqs": freqs}


def bench_modal() -> None:
    """Modal frequency validation via analytical beam theory, executed against the C++
    core through the full MCP server (see the shared-helpers docstring above for why
    raw core_bridge can't reach sim_run_modal's face-query resolution itself)."""
    print("\n--- [modal] Euler-Bernoulli modal frequencies (Rao 5th §8.2) ---")
    ready, reason = _fea_ready()
    cases = [
        ("M1-cantilever-square-first-mode",
         lambda: _modal_case("vv_m1", 1000.0, 50.0, 50.0, False),
         "Rao 5th §8.2 Table 8.1: β₁L=1.87510, f₁=(β₁L)²/(2πL²)√(EI/ρA)"),
        ("M2-cantilever-rect-weak-axis-first-mode",
         lambda: _modal_case("vv_m2", 1000.0, 20.0, 80.0, True),
         "Rao 5th §8.2: weak-axis I=h·b³/12 governs the first (lowest) mode when b≠h"),
    ]
    if not ready:
        print(f"  (no solver: {reason})")
        for case_name, _fn, citation in cases:
            _skip("modal", case_name, reason, citation)
        return
    for case_name, fn, citation in cases:
        try:
            res = fn()
        except Exception as exc:
            _record("modal", case_name, "N/A", "FAIL", None, None, f"error: {exc}", citation)
            traceback.print_exc()
            continue
        if not res["ok"]:
            err = res.get("error")
            if _missing_fea_capability(err):
                _skip("modal", case_name,
                      f"engine build lacks sim_run_modal (needs core/build-occt-linux, "
                      f"not the plain build-linux .so): {err}", citation)
            else:
                _record("modal", case_name, "N/A", "FAIL", None, res["ref"],
                         f"run_fea_modal failed: {err}", citation)
            continue
        _check("modal", case_name, "PRECISE", res["got"], res["ref"], citation,
               note=f"f1={res['got']:.2f}Hz (all modes: {res.get('freqs')})")


# ===========================================================================
# FAMILY 6b: TRANSIENT thermal — 1D slab quench
# Reference: Incropera & DeWitt "Fundamentals of Heat and Mass Transfer" 7th ed
# §5.5: series solution for a plane wall with sudden symmetric surface temperature.
# ===========================================================================

def _thermal_transient_slab_case() -> dict:
    """TT1: slab at uniform T0 suddenly held at T_s on both x faces (insulated sides —
    natural BCs on the unlisted faces). Analytic 1D mid-plane temperature:
        theta(x,t)/theta0 = sum_{odd n} 4/(n*pi) sin(n*pi*x/L) exp(-alpha n^2 pi^2 t/L^2)
    The run's final-frame max temperature IS the mid-plane temperature (the hottest
    point of a symmetric quench), so the last frame's max is the observable."""
    L, b, h = 100.0, 20.0, 20.0                     # mm slab, through-x conduction
    T0, T_s = 100.0, 25.0
    k_wmk, rho, cp = 50.0, 7.85, 490.0              # steel; rho in g/cm^3
    alpha = k_wmk / (rho * 1000.0 * cp) * 1e6       # mm^2/s (= 13.0 for these numbers)
    t_star = 0.3 * L * L / alpha                    # Fo = 0.3
    theta = 0.0
    for n in range(1, 200, 2):
        theta += (4.0 / (n * math.pi) * math.sin(n * math.pi / 2.0)
                  * math.exp(-alpha * n * n * math.pi ** 2 * t_star / (L * L)))
    ref = T_s + (T0 - T_s) * theta

    doc = _build_box_part("vv_tt1", L, b, h)
    r = _mcp_call("run_fea_thermal", {
        "hot_faces": [{"face": {"of": "face", "side": "left"}, "temperature": T_s}],
        "cold_faces": [{"face": {"of": "face", "side": "right"}, "temperature": T_s}],
        "material": {**_VV_STEEL, "conductivity_w_mk": k_wmk, "cp_j_kgk": cp},
        "transient": {"t_end_s": t_star, "dt_s": t_star / 300.0, "t_init_c": T0},
        "doc_id": doc,
    })
    got = None
    if r.get("ok") and r.get("frames"):
        got = r["frames"][-1].get("max_temp_c")
    return {"ok": r.get("ok", False), "got": got, "ref": ref, "error": r.get("error"),
            "note": f"L={L:.0f}mm slab, Fo=0.3 (t*={t_star:.1f}s), alpha={alpha:.2f}mm^2/s, "
                    f"backward Euler dt=t*/300, mid-plane T"}


def bench_thermal_transient() -> None:
    """Transient conduction validation: analytic 1D slab quench vs the C++ backward-Euler
    transient solver, executed through the full MCP server (same access pattern
    and graceful-skip rules as bench_fea_static)."""
    print("\n--- [thermal-transient] 1D slab quench (Incropera 7th §5.5) ---")
    case_name = "TT1-slab-quench-mid-T"
    citation = ("Incropera 7th §5.5: plane-wall series "
                "theta/theta0=sum 4/(n*pi) sin(n*pi*x/L) exp(-alpha n^2 pi^2 t/L^2)")
    ready, reason = _fea_ready()
    if not ready:
        _skip("thermal-transient", case_name, reason, citation)
        return
    try:
        res = _thermal_transient_slab_case()
    except Exception as exc:
        _record("thermal-transient", case_name, "N/A", "FAIL", None, None,
                f"error: {exc}", citation)
        traceback.print_exc()
        return
    if not res["ok"]:
        err = res.get("error")
        if _missing_fea_capability(err):
            _skip("thermal-transient", case_name,
                  f"engine build lacks sim_run_thermal (needs core/build-occt-linux, "
                  f"not the plain build-linux .so): {err}", citation)
        else:
            _record("thermal-transient", case_name, "N/A", "FAIL", None, res["ref"],
                    f"run_fea_thermal(transient) failed: {err}", citation)
        return
    # ENGINEERING tier, tightened to 3%: real tet mesh + O(dt) backward Euler vs the
    # exact 1D series — spatial + temporal discretisation both present.
    _check("thermal-transient", case_name, "ENGINEERING", res["got"], res["ref"],
           citation, note=res["note"], tol=3.0)


# ===========================================================================
# FAMILY 7: CFD — LBM drag trend (not absolute Cd)
# The engine explicitly documents Cd as uncalibrated (staircased voxel LBM).
# We test the TREND: Cd must be velocity-independent (Re-independent for same
# body geometry, which is the correct physical expectation for high-Re LBM).
# ===========================================================================

def bench_cfd_lbm_trend() -> None:
    """LBM wind tunnel drag trend validation.

    The engine docstring states: 'ABSOLUTE Cd IS UNCALIBRATED — coarse voxel LBM
    staircased surface over-counts force'. We do NOT validate absolute Cd.
    We validate the physical TREND: for fixed geometry, drag force ∝ V² (i.e.
    Cd is velocity-independent in the drag equation F = ½ρCdAV²).
    This requires a real part document and C++ core — skip gracefully without it.
    """
    print("\n--- [cfd-lbm] LBM drag trend (Cd velocity-independence) ---")

    lib_path = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
    if not lib_path or not os.path.exists(lib_path):
        _skip("cfd-lbm", "LBM-T1-drag-velocity-squared",
              "CHUDWARE_LIBCHUDWARE not set — C++ LBM only accessible through full MCP server; "
              "TREND test requires two sim_run_flow calls with different velocities",
              "Drag equation: F=½ρCdAV² → Cd=const for fixed Re regime")
        return

    mcp_path = str(_PROJECT_ROOT / "services" / "mcp")
    if mcp_path not in sys.path:
        sys.path.insert(0, mcp_path)
    try:
        import core_bridge as cb
    except ImportError as exc:
        _skip("cfd-lbm", "LBM-T1-drag-velocity-squared",
              f"core_bridge import failed: {exc}",
              "Drag equation: F=½ρCdAV²")
        return

    # sim_run_flow requires a full MCP server doc context — not directly callable
    # on the raw bridge (same as sim_run_static). Record source-audit verdict.
    # Citation: drag equation F = ½·ρ·Cd·A·V² (Anderson "Introduction to Flight" 8th ed §5.4)
    # TREND: if F1 at V1 and F2 at V2, then Cd1 == Cd2 iff F2/F1 == (V2/V1)²
    _record("cfd-lbm", "LBM-T1-drag-velocity-squared", "N/A", "SKIP",
            None, None,
            "sim_run_flow only callable through full MCP server (C++ plugin registration). "
            "TREND test (Cd velocity-independence) deferred to MCP integration test. "
            "Physical expectation: F∝V² → Cd=const for fixed geometry. "
            "Engine docstring: 'ABSOLUTE Cd UNCALIBRATED — coarse voxel over-counts force'.",
            "Anderson 2016 §5.4: F_drag=½ρCdAV²; Cd=const(V) for incompressible regime",
            executed=False)


# ===========================================================================
# FAMILY 8: FEA static (C++ core via the full MCP server)
# Reference: Euler-Bernoulli cantilever deflection (Roark 8th)
# ===========================================================================

def bench_fea_static() -> None:
    """FEA static analysis via the C++ core, wired through the full MCP server (see the
    shared-helpers docstring above bench_modal for why raw core_bridge can't reach
    sim_run_static's face-query resolution itself)."""
    print("\n--- [fea-static] C++ FEA static (via full MCP server) ---")
    ready, reason = _fea_ready()
    cases = [
        ("FEA1-cantilever-point-load", _fea1_point_load_case, "PRECISE",
         "Roark 8th Table 8.1 Case 1a: δ=PL³/(3EI)"),
        ("FEA2-cantilever-rect-section", _fea2_rect_section_case, "PRECISE",
         "Roark 8th Table 8.1 Case 1a: δ=PL³/(3EI), b≠h section"),
        ("FEA3-cantilever-uniform-pressure", _fea3_uniform_pressure_case, "ENGINEERING",
         "Roark 8th Table 3.1 Case 2a: δ=wL⁴/(8EI)"),
    ]
    if not ready:
        for case_name, _fn, _tier, citation in cases:
            _skip("fea-static", case_name, reason, citation)
        return
    for case_name, fn, tier, citation in cases:
        try:
            res = fn()
        except Exception as exc:
            _record("fea-static", case_name, "N/A", "FAIL", None, None,
                     f"error: {exc}", citation)
            traceback.print_exc()
            continue
        if not res["ok"]:
            err = res.get("error")
            if _missing_fea_capability(err):
                _skip("fea-static", case_name,
                      f"engine build lacks sim_run_static (needs core/build-occt-linux, "
                      f"not the plain build-linux .so): {err}", citation)
            else:
                _record("fea-static", case_name, "N/A", "FAIL", None, res["ref"],
                         f"run_fea_static failed: {err}", citation)
            continue
        _check("fea-static", case_name, tier, res["got"], res["ref"], citation,
               note=res.get("note", ""))


# ===========================================================================
# FAMILY 8b: pinned Polyhedral-FEA / PolyMesh mesher + Chudware tet FEA
#
# This is deliberately distinct from FAMILY 8.  It exercises the independently
# implemented graded Cartesian PolyMesh discretization while retaining Chudware's
# tet10 solver boundary.  It does NOT claim a VEM or general polyhedral solve.
# References are clean-room closed-form derivations in this file; no standards
# documents or copied benchmark decks are bundled.
# ===========================================================================

def _polymesh_static_case(case: str) -> dict:
    E = _VV_STEEL["E_mpa"]
    if case == "PM1-cantilever-bending":
        L, b, h, P = 1000.0, 50.0, 50.0, 1000.0
        I = b * h ** 3 / 12.0
        ref = P * L ** 3 / (3.0 * E * I)
        load = [0.0, 0.0, -P]
        note = "clean-room Euler-Bernoulli δ=PL³/(3EI)"
    elif case == "PM2-axial-extension":
        L, b, h, P = 800.0, 40.0, 30.0, 12000.0
        ref = P * L / (E * b * h)
        load = [P, 0.0, 0.0]
        note = "clean-room uniform-bar δ=PL/(EA)"
    else:
        raise ValueError(f"unknown PolyMesh benchmark case: {case}")
    doc = _build_box_part(f"vv_{case.lower().replace('-', '_')}", L, b, h)
    result = _mcp_call("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": load}],
        "material": _VV_STEEL,
        "mesh_fineness": 0.6,
        "doc_id": doc,
    })
    got = result.get("summary", {}).get("max_disp_mm") if result.get("ok") else None
    return {
        "ok": result.get("ok", False),
        "got": got,
        "ref": ref,
        "error": result.get("error"),
        "note": (f"{note}; mesher=Polyhedral-FEA, element=tet10, "
                 "solver=Chudware core tet FEA"),
    }


def bench_polymesh_fea() -> None:
    """Independent PolyMesh discretization checked against two clean-room references."""
    print("\n--- [polymesh-fea] pinned Polyhedral-FEA mesher + core tet FEA ---")
    cases = [
        ("PM1-cantilever-bending", "ENGINEERING",
         "Clean-room Euler-Bernoulli derivation: I=bh³/12; δ=PL³/(3EI)"),
        ("PM2-axial-extension", "ENGINEERING",
         "Clean-room Hooke-law derivation: σ=P/A; ε=σ/E; δ=PL/(EA)"),
    ]
    source, revision = _polymesh_provenance()
    ready, reason = _fea_ready()
    if not ready:
        for case, _tier, citation in cases:
            _record("polymesh-fea", case, "N/A", "SKIP", None, None, reason, citation,
                    executed=False, solver_source=source, solver_revision=revision)
        return
    for case, tier, citation in cases:
        try:
            result = _polymesh_static_case(case)
        except Exception as exc:
            _record("polymesh-fea", case, "N/A", "FAIL", None, None,
                    f"error: {exc}", citation, solver_source=source,
                    solver_revision=revision)
            traceback.print_exc()
            continue
        if not result["ok"]:
            error = result.get("error")
            if _missing_polymesh_capability(error):
                _record("polymesh-fea", case, "N/A", "SKIP", None, None,
                        f"PolyMesh backend unavailable: {error}", citation,
                        executed=False, solver_source=source, solver_revision=revision)
            else:
                _record("polymesh-fea", case, "N/A", "FAIL", None, result["ref"],
                        f"run_fea_static(polymesh) failed: {error}", citation,
                        solver_source=source, solver_revision=revision)
            continue
        _check("polymesh-fea", case, tier, result["got"], result["ref"], citation,
               note=result["note"], solver_source=source, solver_revision=revision)


# ===========================================================================
# FAMILY 9: CalculiX (ccx) — second structural opinion
# GPL-2 -> subprocess isolation per contracts/licenses.md (same posture as the
# ngspice/Renode/QEMU rows: PATH-discovered, never bundled, degrades gracefully).
# Reference: same cantilever as FEA1 — Roark AND our own C++ FEA both get an
# independent third opinion from ccx.
# ===========================================================================

def _write_calculix_cantilever_inp(path: str, L: float, b: float, h: float, P: float,
                                   E: float, nu: float,
                                   nx: int = 40, ny: int = 4, nz: int = 4) -> dict:
    """Generate a minimal Abaqus-format .inp for a C3D8 hex-mesh cantilever: fixed at
    x=0, a total force P (N, -Z) spread evenly over the x=L end nodes. Clean-room:
    synthesized directly from our own geometry/loads — no NAFEMS/ASME content is
    reproduced (same fence as the rest of this harness)."""
    nnx, nny, nnz = nx + 1, ny + 1, nz + 1

    def nid(i: int, j: int, k: int) -> int:  # 1-based CalculiX node id
        return 1 + i + j * nnx + k * nnx * nny

    lines = ["*NODE"]
    for k in range(nnz):
        for j in range(nny):
            for i in range(nnx):
                x, y, z = L * i / nx, b * j / ny, h * k / nz
                lines.append(f"{nid(i, j, k)}, {x:.6f}, {y:.6f}, {z:.6f}")

    lines.append("*ELEMENT, TYPE=C3D8, ELSET=EBEAM")
    eid = 1
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                # Standard C3D8 winding: bottom face CCW, top face directly above.
                n1, n2 = nid(i, j, k), nid(i + 1, j, k)
                n3, n4 = nid(i + 1, j + 1, k), nid(i, j + 1, k)
                n5, n6 = nid(i, j, k + 1), nid(i + 1, j, k + 1)
                n7, n8 = nid(i + 1, j + 1, k + 1), nid(i, j + 1, k + 1)
                lines.append(f"{eid}, {n1}, {n2}, {n3}, {n4}, {n5}, {n6}, {n7}, {n8}")
                eid += 1

    fixed = [nid(0, j, k) for k in range(nnz) for j in range(nny)]
    lines.append("*NSET, NSET=NFIXED")
    lines.append(", ".join(str(n) for n in fixed))
    tip = [nid(nx, j, k) for k in range(nnz) for j in range(nny)]
    lines.append("*NSET, NSET=NTIP")
    lines.append(", ".join(str(n) for n in tip))

    per_node = P / len(tip)
    lines += [
        "*MATERIAL, NAME=STEEL",
        "*ELASTIC",
        f"{E:.1f}, {nu:.3f}",
        "*SOLID SECTION, ELSET=EBEAM, MATERIAL=STEEL",
        "*BOUNDARY",
        "NFIXED, 1, 3, 0.0",
        "*STEP",
        "*STATIC",
        "*CLOAD",
    ]
    lines += [f"{n}, 3, {-per_node:.6f}" for n in tip]
    lines += ["*NODE PRINT, NSET=NTIP", "U", "*END STEP"]
    Path(path).write_text("\n".join(lines) + "\n")
    return {"n_nodes": nnx * nny * nnz, "n_elements": nx * ny * nz, "tip_nodes": tip}


def _parse_ccx_tip_uz(dat_path: str, tip_nodes: list) -> float | None:
    """Parse a ccx *NODE PRINT U .dat block for the average |U3| over the tip node set."""
    tip_set = set(tip_nodes)
    vals: list = []
    lines = Path(dat_path).read_text().splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("NODE") and "U1" in lines[i]:
            j = i + 1
            while j < len(lines) and lines[j].strip():
                parts = lines[j].split()
                if len(parts) >= 4:
                    try:
                        if int(parts[0]) in tip_set:
                            vals.append(float(parts[3]))
                    except ValueError:
                        pass
                j += 1
        i += 1
    if not vals:
        return None
    return abs(sum(vals) / len(vals))


def _calculix_cantilever_case() -> dict:
    """FEA1's geometry/load re-run through ccx (GPL-2 subprocess, PATH-discovered)."""
    import shutil
    ccx = shutil.which("ccx") or shutil.which("ccx_2.20") or shutil.which("calculix")
    L, b, h, P = 1000.0, 50.0, 50.0, 1000.0
    E, nu = _VV_STEEL["E_mpa"], _VV_STEEL["nu"]
    I = b * h ** 3 / 12.0
    ref = P * L ** 3 / (3.0 * E * I)
    if not ccx:
        return {"ok": False, "skipped": True, "ref": ref,
                "reason": "ccx not found on PATH — subprocess-only, PATH-discovered "
                          "(contracts/licenses.md, GPL-2 isolation); the .inp generator "
                          "itself is covered by TestSmoke without the binary"}
    import subprocess
    import tempfile
    workdir = tempfile.mkdtemp(prefix="vv_ccx_")
    job = "vv_cantilever"
    inp_path = os.path.join(workdir, f"{job}.inp")
    meta = _write_calculix_cantilever_inp(inp_path, L, b, h, P, E, nu)
    try:
        proc = subprocess.run([ccx, "-i", job], cwd=workdir, capture_output=True,
                              text=True, timeout=120)
    except Exception as exc:
        return {"ok": False, "skipped": False, "ref": ref,
                "error": f"ccx subprocess error: {exc}"}
    dat_path = os.path.join(workdir, f"{job}.dat")
    if proc.returncode != 0 or not os.path.exists(dat_path):
        return {"ok": False, "skipped": False, "ref": ref,
                "error": f"ccx exit {proc.returncode}: {proc.stderr[-400:]}"}
    got = _parse_ccx_tip_uz(dat_path, meta["tip_nodes"])
    if got is None:
        return {"ok": False, "skipped": False, "ref": ref,
                "error": "could not parse tip UZ from ccx .dat output"}
    return {"ok": True, "got": got, "ref": ref,
            "note": f"{b:.0f}×{h:.0f}mm L={L:.0f}mm P={P:.0f}N, "
                    f"{meta['n_elements']} C3D8 hex elements"}


def bench_calculix() -> None:
    """CalculiX second structural opinion on the FEA1 cantilever."""
    print("\n--- [calculix] ccx subprocess second opinion ---")
    citation = ("Roark 8th Table 8.1 Case 1a: δ=PL³/(3EI); cross-checked via CalculiX "
                "(ccx, GPL-2 subprocess)")
    res = _calculix_cantilever_case()
    if res.get("skipped"):
        _skip("calculix", "CCX1-cantilever-point-load", res["reason"], citation)
        return
    if not res["ok"]:
        _record("calculix", "CCX1-cantilever-point-load", "N/A", "FAIL", None, res["ref"],
                res.get("error", "ccx run failed"), citation)
        return
    _check("calculix", "CCX1-cantilever-point-load", "ENGINEERING", res["got"], res["ref"],
          citation, note=res.get("note", ""))


# ===========================================================================
# FAMILY 10: Advanced FEA — fatigue / thermal / buckling / contact
# Fatigue is pure-Python (S-N life layer). Thermal/buckling use the engine;
# only the legacy contact proxy uses the optional ccx subprocess from FAMILY 9.
# Each case cites a closed-form reference with literal expected values + a tier.
# ===========================================================================

def bench_fatigue_life() -> None:
    """Fatigue: the host-mirrored S-N life layer vs a Shigley hand-calc."""
    print("\n--- [fatigue] S-N (Basquin) life post-processing ---")
    try:
        from services.mcp import fea_advanced as fa
    except Exception as exc:  # noqa: BLE001
        _skip("fatigue", "FAT1-basquin-life", f"import failed: {exc}", "")
        return
    # Case FAT1: fully-reversed sa_eq=250 MPa on steel (Se=200, Sut=400).
    # Shigley high-cycle S-N: a=(0.9*Sut)^2/Se=(360)^2/200=648.0;
    #   b=-(1/3)*log10(0.9*Sut/Se)=-(1/3)*log10(1.8)=-0.085085;
    #   N=(sa/a)^(1/b)=(250/648)^(1/-0.085085) = 7.263e4 cycles.
    # Reference: Shigley's Mechanical Engineering Design 10th ed, Eq 6-14..6-16
    #   (fully-reversed S-N line, f=0.9). Hand-typed; no text reproduced.
    cite = "Shigley 10th Eq 6-14..6-16 (high-cycle S-N line, f=0.9)"
    N = fa.sn_life(250.0, 200.0, 400.0)
    _check("fatigue", "FAT1-basquin-life", "PRECISE", N, 7.2627e4, cite,
           note="fully-reversed sa_eq=250, Se=200, Sut=400 MPa")
    # Case FAT2: runout — sa_eq=150 < Se=200 ⇒ infinite life sentinel 1e6.
    N2 = fa.sn_life(150.0, 200.0, 400.0)
    _check("fatigue", "FAT2-endurance-runout", "EXACT", N2, 1.0e6, cite,
           note="sa_eq below endurance limit ⇒ runout (>=1e6)")
    # Case FAT3: Marin surface factor ka, machined steel Sut=400 MPa.
    #   ka = 4.51 * 400^-0.265 = 0.9218. Reference: Shigley Eq 6-19, Table 6-2.
    ka = fa.marin_ka(400.0, 1)
    _check("fatigue", "FAT3-marin-ka", "PRECISE", ka, 0.9218,
           "Shigley 10th Eq 6-19 Table 6-2 (surface factor, machined)")


def bench_thermal_engine() -> None:
    """Steady conduction on a tetrahedral bar against Fourier's law."""
    import numpy as np
    from services.mcp import core_bridge as cb
    from services.sim.fea_thermal_transient import box_mesh
    print("\n--- [thermal] engine steady-state conduction ---")
    cite = "Incropera Heat & Mass Transfer — 1-D plane-wall conduction (linear T)"
    points, tets, hot, cold = box_mesh(20.0, 8.0, 8.0, 8, 2, 2)
    result = cb.call("sim_run_thermal", {"study": {
        "points": points.flatten().tolist(), "tets": tets.flatten().tolist(),
        "nodes_per_elem": 4, "material": {"k_w_mk": 167.0},
        "fixtures": [{"points": points[ids].tolist(), "tol_mm": 1e-6, "temp_c": temp}
                     for ids, temp in ((hot, 100.0), (cold, 0.0))],
        "loads": [],
    }})
    if not result.get("ok"):
        _record("thermal", "TH1-1d-conduction-mid-T", "ENGINEERING", "FAIL", None,
                50.0, result.get("error", "engine solve failed"), cite)
        return
    field = np.asarray(result["temperature"])
    mid = float(np.mean(field[np.abs(points[:, 0] - 10.0) < 1e-9]))
    _check("thermal", "TH1-1d-conduction-mid-T", "ENGINEERING", mid, 50.0, cite)
    _check("thermal", "TH2-fourier-flux", "ENGINEERING",
           result["flux_max_w_m2"], 167.0 * 100.0 / 0.020, cite)


def bench_buckling_engine() -> None:
    """Euler fixed-free critical load on a real STEP-backed engine tet10 mesh."""
    print("\n--- [buckling] Engine linear buckling ---")
    from scripts.sim_audit import _load_core_bridge, _cantilever_setup, _AUDIT_STEEL

    cite = "Timoshenko & Gere, Theory of Elastic Stability — Euler fixed-free Pcr=π²EI/(2L)²"
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("buckling", "BK1-euler-fixed-free", reason, cite)
        return
    expected = None
    try:
        ctx = _cantilever_setup(cb)
        if ctx.get("error"):
            raise ValueError(ctx["error"])
        length, width, height = ctx["L_mm"], ctx["b_mm"], ctx["h_mm"]
        inertia = min(width * height**3, height * width**3) / 12
        expected = math.pi**2 * _AUDIT_STEEL["E_mpa"] * inertia / (2 * length)**2
        study = {
            "step_path": ctx["step"], "material": _AUDIT_STEEL,
            "fixtures": [ctx["fix_fp"]],
            "loads": [{"faces": ctx["load_fp"], "type": "force",
                       "vector": [-1000.0, 0.0, 0.0]}],
            "mesh_order": 2, "target_size_mm": 20.0,
        }
        out = cb.call("sim_run_buckling", {"study": study, "n_modes": 4})
        if not out.get("ok"):
            raise ValueError(out.get("error", "engine buckling failed"))
        got = float(out["critical_load_n"])
        valid = math.isfinite(got) and abs(got - expected) / expected < 0.08
        _record("buckling", "BK1-euler-fixed-free", "ENGINEERING",
                "PASS" if valid else "FAIL", got, expected,
                "Real CAD tet10, h=20 mm; Euler tolerance 8%", cite)
    except Exception as exc:  # noqa: BLE001
        _record("buckling", "BK1-euler-fixed-free", "ENGINEERING", "FAIL",
                None, expected, str(exc), cite)


def bench_contact_ccx() -> None:
    """Contact: NLGEOM two-block contact load-transfer conservation via ccx.

    This validates the box-proxy deck the frozen `sim_run_contact` worker RPC still
    ships. The MCP tool run_fea_contact no longer runs ccx at all: it solves real
    unilateral contact on the meshed document, and that claim is validated by the
    engine's own `fea_contact` ctest (case (a): base reaction == applied load < 1%).
    """
    print("\n--- [contact] CalculiX nonlinear contact ---")
    from services.mcp import fea_advanced as fa
    # Reference: static equilibrium — the vertical reaction at the fixed base must
    # equal the applied load transmitted across the contact interface (Newton's
    # third law / global force balance). A correct contact solve conserves it.
    cite = "Static equilibrium (Newton III): base reaction == applied load across contact"
    L, b, h, E, nu = 20.0, 20.0, 10.0, 200000.0, 0.29
    pressure = 5.0
    gen = fa.build_contact_block_inp(L, b, h, E, nu, pressure)
    applied = gen["applied_load_n"]
    if fa.ccx_path() is None:
        _skip("contact", "CT1-load-transfer-conservation",
              "ccx not on PATH — subprocess-only (GPL-2.0 isolation); the two-block "
              ".inp generator is covered by TestSmoke without ccx", cite)
        return
    run = fa.run_ccx(gen["inp"], job="ct", timeout=180.0)
    if not run.get("ok") or not run.get("dat_path"):
        _record("contact", "CT1-load-transfer-conservation", "ENGINEERING", "FAIL",
                None, applied, run.get("stderr", "")[-200:] or "ccx failed", cite)
        return
    rf = fa.parse_dat_nodal(run["dat_path"], "RF", gen["base_nodes"], component=3)
    if rf is None:
        _record("contact", "CT1-load-transfer-conservation", "ENGINEERING", "FAIL",
                None, applied, "could not parse RF from .dat", cite)
        return
    reaction = abs(rf * len(gen["base_nodes"]))
    _check("contact", "CT1-load-transfer-conservation", "ENGINEERING", reaction,
           applied, cite, note=f"pressure={pressure} MPa on {L}x{b} mm face")


def bench_advanced_fea() -> None:
    """FAMILY 10 driver — fatigue + thermal + buckling + contact."""
    bench_fatigue_life()
    bench_thermal_engine()
    bench_buckling_engine()
    bench_contact_ccx()


# ===========================================================================
# REPORT GENERATION
# ===========================================================================

def _generate_report(elapsed: float) -> str:
    """Generate the V&V markdown report."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    families: dict[str, list[VVResult]] = {}
    for r in _RESULTS:
        families.setdefault(r.family, []).append(r)

    n_executed = sum(1 for r in _RESULTS if r.executed)
    n_pass = sum(1 for r in _RESULTS if r.verdict == "PASS" and r.executed)
    n_fail = sum(1 for r in _RESULTS if r.verdict == "FAIL" and r.executed)
    n_trend_pass = sum(1 for r in _RESULTS if r.verdict == "TREND-PASS")
    n_skip = sum(1 for r in _RESULTS if not r.executed)

    lines = [
        "# V&V Benchmark Report — Chudware Simulation Backends",
        "",
        f"**Generated:** {now}  ",
        f"**Script:** `scripts/vv_benchmark.py`  ",
        f"**Methodology:** Independent analytic reference solutions (textbook closed-form); "
        f"no reference values read from the engine under test.  ",
        f"**NAFEMS/ASME documents:** NOT bundled. Target values hand-typed with inline citation.  ",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Executed cases | {n_executed} |",
        f"| PASS | {n_pass} |",
        f"| FAIL | {n_fail} |",
        f"| TREND-PASS | {n_trend_pass} |",
        f"| SKIP (env/dep) | {n_skip} |",
        f"| Elapsed | {elapsed:.1f}s |",
        "",
        "## Tolerance Tiers",
        "",
        "| Tier | Threshold | Meaning |",
        "|------|-----------|---------|",
        "| EXACT | < 0.01% | Same closed-form — should be machine-exact |",
        "| PRECISE | < 1.0% | Validated analytic reference |",
        "| ENGINEERING | < 10.0% | Engineering-grade estimate |",
        "| TREND | directional | Physical trend correct (labeled explicitly) |",
        "",
    ]

    for family, results in families.items():
        lines.append(f"## Family: {family}")
        lines.append("")
        provenance = sorted({
            (r.solver_source, r.solver_revision)
            for r in results if r.solver_source and r.solver_revision
        })
        for source, revision in provenance:
            lines.append(f"**Solver source:** `{source}`  ")
            lines.append(f"**Solver revision:** `{revision}`  ")
        if provenance:
            lines.append("")
        lines.append("| Case | Tier | Verdict | Got | Ref | Err% | Citation |")
        lines.append("|------|------|---------|-----|-----|------|----------|")
        for r in results:
            got_str = f"{r.got:.4g}" if r.got is not None else "—"
            ref_str = f"{r.ref:.4g}" if r.ref is not None else "—"
            err_str = f"{r.pct_err:.3f}%" if r.pct_err is not None else "—"
            skip_marker = " *(skipped)*" if not r.executed else ""
            lines.append(
                f"| {r.case}{skip_marker} | {r.tier} | **{r.verdict}** "
                f"| {got_str} | {ref_str} | {err_str} | {r.citation[:60]} |"
            )
        lines.append("")

    lines += [
        "## Reference Sources",
        "",
        "All reference formulas are public-domain textbook physics. No NAFEMS or ASME ",
        "documents are bundled; target values are derived from first principles or ",
        "hand-typed single numbers with inline citation.",
        "",
        "| Source | Used in |",
        "|--------|---------|",
        "| Roark 8th ed Table 8.1-8.2 | S1, S2, S3, S4, FEA1, FEA2 |",
        "| Roark 8th ed Table 3.1 (UDL cantilever) | FEA3 |",
        "| Hibbeler *Structural Analysis* 9th §12.1 | S4 |",
        "| Hayt & Kemmerly *Engineering Circuit Analysis* 8th | C1, C3 |",
        "| Sedra & Smith *Microelectronic Circuits* 6th §1.5 | C2 |",
        "| IPC-2141A §6.2-6.4 (public-domain PCB standard) | I1-I4 |",
        "| White *Fluid Mechanics* 8th §8.4 | P1, P2 |",
        "| Merritt *Hydraulic Control Systems* (1967) §2.2 | H1 |",
        "| Vickers Industrial Hydraulics Manual (1992) ch.3 | H2 |",
        "| Rao *Mechanical Vibrations* 5th §8.2 | M1, M2 |",
        "| Anderson *Introduction to Flight* 8th §5.4 | LBM-T1 |",
        "| CalculiX (ccx, GPL-2, subprocess-only) 3rd-opinion cross-check | CCX1 |",
        "| Clean-room beam/bar derivations in this script | PM1, PM2 (PolyMesh family) |",
        "",
        "## CI Integration",
        "",
        "```bash",
        "# Smoke subset (no .so required; native-RPC cases skip until one is staged):",
        "pytest scripts/vv_benchmark.py::TestSmoke -q",
        "",
        "# Full suite:",
        "CHUDWARE_RUN_VV=full CHUDWARE_LIBCHUDWARE=/path/to/libchudware.so \\",
        "    pytest scripts/vv_benchmark.py -q",
        "```",
        "",
        "Gate integration: `gate.sh` runs the smoke subset when "
        "`CHUDWARE_RUN_VV` is set (skipped by default, like the other opt-in benches).",
    ]

    return "\n".join(lines)


def _generate_json() -> list[dict]:
    return [r.to_dict() for r in _RESULTS]


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> int:
    print("=" * 70)
    print("vv_benchmark.py — V&V Benchmark Harness")
    print(f"Python: {sys.executable}")
    print(f"CHUDWARE_LIBCHUDWARE: {os.environ.get('CHUDWARE_LIBCHUDWARE', '(not set)')}")
    print(f"CHUDWARE_RUN_VV: {os.environ.get('CHUDWARE_RUN_VV', '(not set = smoke)')}")
    print("=" * 70)

    t0 = time.perf_counter()
    bench_hydraulic()
    bench_piping()
    bench_ecad_impedance()
    bench_ecad_circuit()
    bench_structural()
    bench_modal()
    bench_fea_static()
    bench_polymesh_fea()
    bench_thermal_transient()   # transient conduction vs analytic slab quench
    bench_calculix()
    bench_advanced_fea()   # fatigue + thermal + buckling + contact
    bench_cfd_lbm_trend()
    elapsed = time.perf_counter() - t0

    print()
    print("=" * 70)
    print(f"SUMMARY  executed={_EXECUTED}  broken={_BROKEN}")
    print(f"Elapsed: {elapsed:.2f}s")
    print("=" * 70)
    print()

    n_pass = sum(1 for r in _RESULTS if r.verdict == "PASS" and r.executed)
    n_fail = sum(1 for r in _RESULTS if r.verdict == "FAIL" and r.executed)

    print(f"{'Case':<50} {'Tier':<12} {'Verdict':<10} {'Err%'}")
    print("-" * 100)
    for r in _RESULTS:
        err_s = f"{r.pct_err:.3f}%" if r.pct_err is not None else "—"
        mark = " *" if not r.executed else ""
        print(f"  {r.family}/{r.case:<44} {r.tier:<12} {r.verdict:<10} {err_s}{mark}")
    print()

    # Write outputs
    out_dir = _PROJECT_ROOT / "reports" / "vv"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "vv_results.json"
    md_path = out_dir / "vv_report.md"
    report = _generate_report(elapsed)
    json_path.write_text(json.dumps(_generate_json(), indent=2))
    md_path.write_text(report)
    print(f"Report: {md_path}")
    print(f"JSON:   {json_path}")

    if _EXECUTED == 0:
        print("ERROR: zero checks executed — all-skip is a FAILURE")
        return 1
    if n_fail > 0:
        print(f"FAIL: {n_fail} case(s) outside tolerance")
        return 1
    print(f"PASS: {n_pass}/{_EXECUTED} executed cases within tolerance")
    return 0


# ---------------------------------------------------------------------------
# pytest entry points
# ---------------------------------------------------------------------------
# These are the pytest-runnable wrappers. ``::TestSmoke`` needs no .so: its pure-Python
# cases always run, and its native-RPC cases skip with the loader's reason until
# CHUDWARE_LIBCHUDWARE names a staged core library.

import pytest  # noqa: E402 (pytest may not be installed in all envs)


@pytest.fixture(scope="module", autouse=True)
def _reset_results():
    """Reset global result list before pytest collection."""
    global _RESULTS, _EXECUTED, _BROKEN
    _RESULTS = []
    _EXECUTED = 0
    _BROKEN = 0
    yield


def _native_core_or_skip():
    """The core bridge for a native-RPC case, or an honest pytest skip."""
    cb, reason = _native_core()
    if cb is None:
        pytest.skip(reason)
    return cb


class TestSmoke:
    """Smoke subset: no .so required. Runs in CI gate; native-RPC cases skip honestly
    when no core library is staged."""

    def test_hydraulic_pump_sizing(self):
        """H2: Pump displacement and shaft power — hydraulic_size_pump RPC, EXACT tier."""
        cb = _native_core_or_skip()
        Q_lpm, dP_bar, rpm, eta = 30.0, 180.0, 1000.0, 0.88
        disp_ref = Q_lpm * 1000.0 / rpm
        Q_m3s = Q_lpm / 60000.0
        shaft_ref = Q_m3s * dP_bar * 1e5 / (eta * 1000.0)
        res = cb.call("hydraulic_size_pump", {
            "flow_lpm": Q_lpm, "pressure_bar": dP_bar, "speed_rpm": rpm,
            "efficiency": eta})
        assert res.get("ok"), f"size_pump failed: {res}"
        d_err = _pct_err(res["displacement_cc_rev"], disp_ref)
        p_err = _pct_err(res["shaft_power_kw"], shaft_ref)
        assert d_err < EXACT, f"displacement err={d_err:.3f}% > {EXACT}%"
        assert p_err < EXACT, f"shaft power err={p_err:.3f}% > {EXACT}%"

    def test_ecad_microstrip_z0(self):
        """I2: Microstrip ~50Ω — ecad_impedance RPC, IPC-2141A EXACT tier."""
        cb = _native_core_or_skip()
        w, h, t, er = 3.0, 1.6, 0.035, 4.5
        ref = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * w + t))
        got = _impedance(cb, "microstrip", w=w, h=h, t=t, er=er)["z0"]
        err = _pct_err(got, ref)
        assert err < EXACT, f"microstrip Z0 err={err:.4f}% > {EXACT}%"

    def test_ecad_stripline_z0(self):
        """I3: Stripline — ecad_impedance RPC, IPC-2141A EXACT tier."""
        cb = _native_core_or_skip()
        w, h, t, er = 2.0, 3.2, 0.035, 4.5
        ref = (60.0 / math.sqrt(er)) * math.log(
            4.0 * h / (0.67 * math.pi * (0.8 * w + t)))
        got = _impedance(cb, "stripline", w=w, h=h, t=t, er=er)["z0"]
        err = _pct_err(got, ref)
        assert err < EXACT, f"stripline Z0 err={err:.4f}% > {EXACT}%"

    def test_ecad_rc_corner_frequency(self):
        """C1: RC low-pass -3dB corner — sim_spice RPC, PRECISE tier."""
        pytest.importorskip("numpy")
        import numpy as np
        cb = _native_core_or_skip()
        R, C = 1000.0, 159.155e-9
        fc = 1.0 / (2 * math.pi * R * C)
        circ = {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": 0.0, "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
                 "value": R, "params": {}},
                {"kind": "C", "name": "C1", "nodes": ["OUT", "0"],
                 "value": C, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 30,
                                "fstart": 10, "fstop": 1e6}],
                 "probes": ["V(OUT)"]}
        res = _spice(cb, circ, study)
        assert res.get("convergence", {}).get("ac"), "AC did not converge"
        f = np.array(res["ac"]["x_f"])
        mag = np.array(res["ac"]["mag"]["V(OUT)"])
        H_got = float(np.interp(fc, f, mag))
        ref = 1.0 / math.sqrt(2.0)
        err = _pct_err(H_got, ref)
        assert err < PRECISE, f"|H(fc)| err={err:.3f}% > {PRECISE}%"

    def test_ecad_dc_divider(self):
        """C3: Thevenin-loaded divider — sim_spice RPC, EXACT tier.

        R2 ∥ R_L from MID to GND; probe V(MID) = Thevenin loaded voltage.
        """
        cb = _native_core_or_skip()
        R1, R2, RL, Vs = 1000.0, 2000.0, 3000.0, 12.0
        Vth = Vs * R2 / (R1 + R2)
        Rth = R1 * R2 / (R1 + R2)
        ref = Vth * RL / (Rth + RL)
        circ = {
            "nodes": ["0", "IN", "MID"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": Vs, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "MID"],
                 "value": R1, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["MID", "0"],
                 "value": R2, "params": {}},
                {"kind": "R", "name": "RL", "nodes": ["MID", "0"],
                 "value": RL, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study = {"analyses": [{"type": "op"}], "probes": ["V(MID)"]}
        res = _spice(cb, circ, study)
        assert res.get("convergence", {}).get("op"), "DC OP did not converge"
        err = _pct_err(res["op"]["V(MID)"], ref)
        assert err < EXACT, f"V(MID) err={err:.4f}% > {EXACT}%"

    def test_structural_cantilever(self):
        """S1: PyNite cantilever tip deflection — PRECISE tier."""
        pytest.importorskip("Pynite")
        from Pynite import FEModel3D
        E, G = 200000.0, 77000.0
        b, h, L, P = 50.0, 200.0, 3000.0, 5000.0
        Iy = b * h ** 3 / 12.0
        delta_ref = P * L ** 3 / (3.0 * E * Iy)
        m = FEModel3D()
        m.add_material("steel", E=E, G=G, nu=0.3, rho=7.85e-3)
        m.add_section("sec", b * h, Iy, h * b ** 3 / 12.0,
                      0.141 * b * h ** 3 / (b / h))
        m.add_node("A", 0, 0, 0)
        m.add_node("B", L, 0, 0)
        m.add_member("m1", "A", "B", "steel", "sec")
        m.def_support("A", True, True, True, True, True, True)
        m.add_node_load("B", "FZ", -P)
        m.analyze(check_statics=False)
        delta_got = abs(m.nodes["B"].DZ["Combo 1"])
        err = _pct_err(delta_got, delta_ref)
        assert err < PRECISE, f"cantilever δ err={err:.3f}% > {PRECISE}%"

    def test_structural_simply_supported(self):
        """S2: PyNite SS beam UDL midspan deflection — PRECISE tier."""
        pytest.importorskip("Pynite")
        from Pynite import FEModel3D
        E, G = 200000.0, 77000.0
        b, h, L, w = 100.0, 200.0, 6000.0, 2.0
        Iy = b * h ** 3 / 12.0
        delta_ref = 5.0 * w * L ** 4 / (384.0 * E * Iy)
        m = FEModel3D()
        m.add_material("steel", E=E, G=G, nu=0.3, rho=7.85e-3)
        m.add_section("sec", b * h, Iy, h * b ** 3 / 12.0,
                      0.141 * b * h ** 3 / (b / h))
        m.add_node("A", 0, 0, 0)
        m.add_node("MID", L / 2, 0, 0)
        m.add_node("B", L, 0, 0)
        m.add_member("mL", "A", "MID", "steel", "sec")
        m.add_member("mR", "MID", "B", "steel", "sec")
        m.def_support("A", True, True, True, False, False, False)
        m.def_support("B", True, True, True, False, False, False)
        for mem in ("mL", "mR"):
            m.add_member_dist_load(mem, "FZ", -w, -w)
        m.analyze(check_statics=False)
        delta_got = abs(m.nodes["MID"].DZ["Combo 1"])
        err = _pct_err(delta_got, delta_ref)
        assert err < PRECISE, f"SS UDL δ err={err:.3f}% > {PRECISE}%"

    def test_calculix_inp_generator(self):
        """CCX-gen: the CalculiX .inp generator emits self-consistent Abaqus
        syntax (node/element counts, boundary/load node sets) — checked without ccx
        itself, so this always runs in the gate even when the binary is absent."""
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "probe.inp")
        meta = _write_calculix_cantilever_inp(path, 1000.0, 50.0, 50.0, 1000.0,
                                              200000.0, 0.3, nx=4, ny=2, nz=2)
        text = Path(path).read_text()
        assert "*NODE" in text and "*ELEMENT, TYPE=C3D8" in text
        assert "*BOUNDARY" in text and "*CLOAD" in text
        assert meta["n_nodes"] == 5 * 3 * 3
        assert meta["n_elements"] == 4 * 2 * 2
        assert len(meta["tip_nodes"]) == 3 * 3

    def test_adv_fea_fatigue_life_handcalc(self):
        """FAT1: the S-N life layer matches the Shigley hand-calc (no binary)."""
        from services.mcp import fea_advanced as fa
        N = fa.sn_life(250.0, 200.0, 400.0)
        assert abs(N - 7.2627e4) / 7.2627e4 < 0.01, N
        assert fa.sn_life(150.0, 200.0, 400.0) == 1e6         # runout
        assert abs(fa.marin_ka(400.0, 1) - 0.9218) < 0.01     # machined ka

    def test_adv_fea_fatigue_summarize(self):
        """fatigue_summarize turns engine sa_eq into life + regime + fos."""
        from services.mcp import fea_advanced as fa
        theories = {"goodman": {"sa_eq_mpa": 250.0}, "gerber": {"sa_eq_mpa": 150.0}}
        out = fa.fatigue_summarize(theories, 200.0, 400.0, theory="goodman")
        assert out["regime"] == "finite" and out["cycles"] is not None
        assert out["theories"]["gerber"]["regime"] == "infinite"   # below Se
        assert out["fos_endurance"] == round(200.0 / 250.0, 4)


    def test_adv_fea_contact_inp_generator(self):
        """CT1: contact .inp has NLGEOM + CONTACT PAIR + two disjoint blocks."""
        from services.mcp import fea_advanced as fa
        gen = fa.build_contact_block_inp(20.0, 20.0, 10.0, 200000.0, 0.29, 5.0,
                                         nx=3, ny=3, nz=2)
        t = gen["inp"]
        assert "NLGEOM" in t and "*CONTACT PAIR" in t and "*SURFACE INTERACTION" in t
        assert "TYPE=ELEMENT" in t and "TYPE=NODE" in t   # master face + slave node
        assert gen["applied_load_n"] == 5.0 * 20.0 * 20.0
        # node ids of the two blocks must not overlap
        assert min(gen["top_nodes"]) > max(gen["base_nodes"])

    def test_adv_fea_ccx_absent_degrade(self):
        """The ccx-absent path returns an honest skipped shape, never fake data."""
        from services.mcp import fea_advanced as fa
        r = fa._ccx_absent_result("thermal")
        assert r["ok"] is False and r["skipped"] is True and "reason" in r


class TestFull:
    """Full suite: requires CHUDWARE_LIBCHUDWARE and external deps.
    Run with CHUDWARE_RUN_VV=full.
    """

    @pytest.fixture(autouse=True)
    def _require_full(self):
        if os.environ.get("CHUDWARE_RUN_VV", "") != "full":
            pytest.skip("set CHUDWARE_RUN_VV=full to run full V&V suite")

    def test_piping_darcy_weisbach(self):
        """P1: Piping Darcy-Weisbach single run — piping_solve RPC, ENGINEERING tier."""
        cb = _native_core_or_skip()
        result = _piping_single_run(cb, 50.0, 1.0, "40", 5.0)
        assert result.get("ok"), f"solve failed: {result.get('reason')}"
        links = result.get("links", [])
        assert links, "no links in result"
        D, f_approx, L_m, dH = 0.027, 0.03, 50.0, 5.0
        v_approx = math.sqrt(dH * 2 * 9.81 * D / (f_approx * L_m))
        Q_ref = math.pi / 4 * D ** 2 * v_approx * 3600.0
        Q_got = links[0]["flow_m3h"]
        err = _pct_err(Q_got, Q_ref)
        assert err < ENGINEERING, f"Q err={err:.1f}% > {ENGINEERING}%"

    def test_piping_length_scaling(self):
        """P2: Piping Q∝√(1/L) scaling — piping_solve RPC, ENGINEERING tier."""
        cb = _native_core_or_skip()

        def _run(length_m):
            result = _piping_single_run(cb, length_m, 1.0, "40", 5.0)
            assert result.get("ok"), f"solve failed: {result.get('reason')}"
            return result["links"][0]["flow_m3h"]

        Q1 = _run(50.0)
        Q2 = _run(100.0)
        ratio = Q2 / Q1
        ref = 1.0 / math.sqrt(2.0)
        err = _pct_err(ratio, ref)
        assert err < ENGINEERING, f"length-scaling ratio err={err:.1f}% > {ENGINEERING}%"

    def test_structural_fixed_fixed(self):
        """S3: Fixed-fixed midspan load — PRECISE tier."""
        pytest.importorskip("Pynite")
        from Pynite import FEModel3D
        E, G = 200000.0, 77000.0
        b, h, L, P = 80.0, 160.0, 4000.0, 10000.0
        Iy = b * h ** 3 / 12.0
        delta_ref = P * L ** 3 / (192.0 * E * Iy)
        m = FEModel3D()
        m.add_material("steel", E=E, G=G, nu=0.3, rho=7.85e-3)
        m.add_section("sec", b * h, Iy, h * b ** 3 / 12.0,
                      0.141 * b * h ** 3 / (b / h))
        m.add_node("A", 0, 0, 0)
        m.add_node("MID", L / 2, 0, 0)
        m.add_node("B", L, 0, 0)
        m.add_member("mL", "A", "MID", "steel", "sec")
        m.add_member("mR", "MID", "B", "steel", "sec")
        m.def_support("A", True, True, True, True, True, True)
        m.def_support("B", True, True, True, True, True, True)
        m.add_node_load("MID", "FZ", -P)
        m.analyze(check_statics=False)
        delta_got = abs(m.nodes["MID"].DZ["Combo 1"])
        err = _pct_err(delta_got, delta_ref)
        assert err < PRECISE, f"fixed-fixed δ err={err:.3f}% > {PRECISE}%"

    def test_structural_two_span(self):
        """S4: Two-span continuous beam UDL max at x=3L/4 — PRECISE tier.

        Analytic: three-moment eq gives M_B=-wL²/8; double integration yields
        EI·y(x)=(wx³/24)(L-x); max at x=3L/4, δ_max=27wL⁴/(6144EI).
        """
        pytest.importorskip("Pynite")
        from Pynite import FEModel3D
        E, G = 200000.0, 77000.0
        b, h, L, w = 100.0, 200.0, 3000.0, 1.0
        Iy = b * h ** 3 / 12.0
        # Analytic max: 0.005301·wL⁴/(EI), derived by double integration.
        # Max at x≈0.3641L (root of 4u³-9u²+1=0). ENGINEERING tier (10%)
        # because FEM max is between mesh nodes.
        delta_ref = 0.005301 * w * L ** 4 / (E * Iy)
        n_seg = 8
        m = FEModel3D()
        m.add_material("steel", E=E, G=G, nu=0.3, rho=7.85e-3)
        m.add_section("sec", b * h, Iy, h * b ** 3 / 12.0,
                      0.141 * b * h ** 3 / (b / h))
        nodes = [f"N{i}" for i in range(2 * n_seg + 1)]
        for i, nm in enumerate(nodes):
            m.add_node(nm, i * L / n_seg, 0, 0)
        for i in range(2 * n_seg):
            m.add_member(f"m{i}", nodes[i], nodes[i + 1], "steel", "sec")
            m.add_member_dist_load(f"m{i}", "FZ", -w, -w)
        m.def_support("N0", True, True, True, False, False, False)
        m.def_support(f"N{n_seg}", True, True, True, False, False, False)
        m.def_support(f"N{2*n_seg}", True, True, True, False, False, False)
        m.analyze(check_statics=False)
        delta_got = max(
            abs(m.nodes[nm].DZ["Combo 1"])
            for i, nm in enumerate(nodes)
            if i not in (0, n_seg, 2 * n_seg)
        )
        err = _pct_err(delta_got, delta_ref)
        assert err < ENGINEERING, f"two-span δ_max err={err:.3f}% > {ENGINEERING}%"

    def test_ecad_rlc_resonance(self):
        """C2: Series RLC resonance frequency peak — sim_spice RPC, ENGINEERING tier."""
        pytest.importorskip("numpy")
        import numpy as np
        cb = _native_core_or_skip()
        R, L_h, C_f = 10.0, 1e-3, 1e-6
        f0_ref = 1.0 / (2 * math.pi * math.sqrt(L_h * C_f))
        circ = {
            "nodes": ["0", "IN", "N1", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": 0.0, "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "N1"],
                 "value": R, "params": {}},
                {"kind": "L", "name": "L1", "nodes": ["N1", "OUT"],
                 "value": L_h, "params": {}},
                {"kind": "C", "name": "C1", "nodes": ["OUT", "0"],
                 "value": C_f, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 50,
                                "fstart": 100, "fstop": 1e5}],
                 "probes": ["V(N1)", "V(OUT)"]}
        res = _spice(cb, circ, study)
        assert res.get("convergence", {}).get("ac"), "AC did not converge"
        f = np.array(res["ac"]["x_f"])
        mag = np.array(res["ac"]["mag"]["V(OUT)"])
        f_peak = float(f[np.argmax(mag)])
        err = _pct_err(f_peak, f0_ref)
        assert err < ENGINEERING, f"RLC resonance freq err={err:.1f}% > {ENGINEERING}%"

    def test_fea_static_point_load(self):
        """FEA1: C++ static FEA cantilever tip deflection vs Roark — PRECISE."""
        ready, reason = _fea_ready()
        if not ready:
            pytest.skip(reason)
        res = _fea1_point_load_case()
        if not res["ok"] and _missing_fea_capability(res.get("error")):
            pytest.skip(f"engine build lacks sim_run_static: {res.get('error')}")
        assert res["ok"], res.get("error")
        err = _pct_err(res["got"], res["ref"])
        assert err < PRECISE, f"FEA1 δ err={err:.3f}% > {PRECISE}%"

    def test_fea_static_rect_section(self):
        """FEA2: rectangular-section strong-axis cantilever — PRECISE tier."""
        ready, reason = _fea_ready()
        if not ready:
            pytest.skip(reason)
        res = _fea2_rect_section_case()
        if not res["ok"] and _missing_fea_capability(res.get("error")):
            pytest.skip(f"engine build lacks sim_run_static: {res.get('error')}")
        assert res["ok"], res.get("error")
        err = _pct_err(res["got"], res["ref"])
        assert err < PRECISE, f"FEA2 δ err={err:.3f}% > {PRECISE}%"

    def test_fea_static_uniform_pressure(self):
        """FEA3: uniform pressure-load cantilever — ENGINEERING tier."""
        ready, reason = _fea_ready()
        if not ready:
            pytest.skip(reason)
        res = _fea3_uniform_pressure_case()
        if not res["ok"] and _missing_fea_capability(res.get("error")):
            pytest.skip(f"engine build lacks sim_run_static: {res.get('error')}")
        assert res["ok"], res.get("error")
        err = _pct_err(res["got"], res["ref"])
        assert err < ENGINEERING, f"FEA3 δ err={err:.3f}% > {ENGINEERING}%"

    def test_fea_modal_cantilever(self):
        """M1: C++ modal FEA first bending mode vs Rao — PRECISE tier."""
        ready, reason = _fea_ready()
        if not ready:
            pytest.skip(reason)
        res = _modal_case("vv_m1_pytest", 1000.0, 50.0, 50.0, False)
        if not res["ok"] and _missing_fea_capability(res.get("error")):
            pytest.skip(f"engine build lacks sim_run_modal: {res.get('error')}")
        assert res["ok"], res.get("error")
        err = _pct_err(res["got"], res["ref"])
        assert err < PRECISE, f"M1 f1 err={err:.3f}% > {PRECISE}%"

    def test_fea_modal_rect_weak_axis(self):
        """M2: rectangular-section weak-axis first mode — PRECISE tier."""
        ready, reason = _fea_ready()
        if not ready:
            pytest.skip(reason)
        res = _modal_case("vv_m2_pytest", 1000.0, 20.0, 80.0, True)
        if not res["ok"] and _missing_fea_capability(res.get("error")):
            pytest.skip(f"engine build lacks sim_run_modal: {res.get('error')}")
        assert res["ok"], res.get("error")
        err = _pct_err(res["got"], res["ref"])
        assert err < PRECISE, f"M2 f1 err={err:.3f}% > {PRECISE}%"

    def test_polymesh_fea_clean_room_references(self):
        """PM1/PM2: pinned PolyMesh meshes, core tet solver, independent references."""
        ready, reason = _fea_ready()
        if not ready:
            pytest.skip(reason)
        for case in ("PM1-cantilever-bending", "PM2-axial-extension"):
            result = _polymesh_static_case(case)
            if not result["ok"] and _missing_polymesh_capability(result.get("error")):
                pytest.skip(f"PolyMesh backend unavailable: {result.get('error')}")
            assert result["ok"], result.get("error")
            err = _pct_err(result["got"], result["ref"])
            assert err < ENGINEERING, (
                f"{case} err={err:.3f}% > {ENGINEERING}%")

    def test_calculix_cantilever(self):
        """CCX1: ccx second opinion on the FEA1 cantilever — ENGINEERING tier."""
        res = _calculix_cantilever_case()
        if res.get("skipped"):
            pytest.skip(res["reason"])
        assert res["ok"], res.get("error")
        err = _pct_err(res["got"], res["ref"])
        assert err < ENGINEERING, f"CCX1 δ err={err:.3f}% > {ENGINEERING}%"


if __name__ == "__main__":
    sys.exit(main())
