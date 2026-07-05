#!/usr/bin/env python3
"""Simulation backend audit — T-V1, extended T-SIM4 (live FEA + fatigue + thermal).

Runs independent analytic checks against each simulation/analysis backend and
prints per-tool verdicts: REAL, VALIDATED, STUB, or BROKEN.

REAL      = code implements real physics (evidence: source lines cited)
VALIDATED = REAL + matches an independent hand-calc within tolerance
STUB      = returns canned / constant / placeholder output
BROKEN    = errors on reasonable input or returns physically wrong numbers

Independent reference values are derived from first principles INSIDE this script
(never read from the engine under test), so a wrong docstring would be caught.

Usage:
    AI_CAD_LIBAICAD=/path/to/libaicad.so \\
        ~/.local/share/AI-CAD/mcp-venv/bin/python scripts/sim_audit.py

Exit code: 0 if all checks pass or skip-with-reason; 1 if any BROKEN finding.
"""
from __future__ import annotations

import math
import os
import sys
import time
import traceback

# Ensure the project root (containing engine/) is on sys.path regardless of
# which directory the script is invoked from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
_RESULTS: list[dict] = []
_EXECUTED = 0
_SKIPPED = 0
_BROKEN = 0


def _record(tool: str, verdict: str, note: str, executed: bool = True) -> None:
    global _EXECUTED, _SKIPPED, _BROKEN
    _RESULTS.append({"tool": tool, "verdict": verdict, "note": note})
    if executed:
        _EXECUTED += 1
    else:
        _SKIPPED += 1
    if verdict == "BROKEN":
        _BROKEN += 1
    sym = {"REAL": "REAL", "VALIDATED": "VALD", "STUB": "STUB",
           "BROKEN": "BROK", "SKIP": "SKIP"}.get(verdict, verdict)
    tag = "(executed)" if executed else "(skipped)"
    print(f"  [{sym}] {tool}: {note} {tag}")


def _skip(tool: str, reason: str, install: str = "") -> None:
    hint = f" — install: {install}" if install else ""
    _record(tool, "SKIP", reason + hint, executed=False)


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got) * 100.0
    return abs(got - exp) / abs(exp) * 100.0


# ===========================================================================
# FAMILY 1: Hydraulic cylinder / pump sizing
# Pure-Python, no .so needed. Pure math, low risk.
# ===========================================================================

def audit_hydraulic() -> None:
    print("\n--- FAMILY: hydraulic (engine.hydraulic.solve) ---")
    try:
        from engine.hydraulic import solve as hyd

        # --- cylinder sizing ---
        # Independent check (first principles):
        #   A_bore = pi/4 * (bore_mm * 1e-3)^2 (m^2)
        #   F_ext  = A_bore * pressure_bar * 1e5 * efficiency (N) / 1000 (kN)
        bore_mm = 50.0
        pressure_bar = 200.0
        stroke_mm = 100.0
        efficiency = 0.95
        A_bore_m2 = math.pi / 4.0 * (bore_mm * 1e-3) ** 2
        F_ext_kn_expected = A_bore_m2 * pressure_bar * 1e5 * efficiency * 1e-3
        # Flow: Q_ext = A_bore_m2 * velocity_m_s * 60 * 1000 (L/min)
        velocity_mm_s = 50.0
        velocity_m_s = velocity_mm_s * 1e-3
        Q_ext_lpm_expected = A_bore_m2 * velocity_m_s * 60.0 * 1000.0

        result = hyd.size_cylinder(bore_mm=bore_mm, pressure_bar=pressure_bar,
                                   stroke_mm=stroke_mm, velocity_mm_s=velocity_mm_s,
                                   efficiency=efficiency)
        assert result.get("ok"), f"size_cylinder failed: {result}"
        F_got = result["extend_force_kn"]
        Q_got = result["flow_extend_lpm"]
        f_err = _pct_err(F_got, F_ext_kn_expected)
        q_err = _pct_err(Q_got, Q_ext_lpm_expected)
        note = (f"bore={bore_mm}mm, P={pressure_bar}bar → "
                f"F={F_got:.3f} kN (expect {F_ext_kn_expected:.3f}, err={f_err:.2f}%), "
                f"Q={Q_got:.4f} L/min (expect {Q_ext_lpm_expected:.4f}, err={q_err:.2f}%)")
        if f_err < 1.0 and q_err < 1.0:
            _record("hydraulic_size_cylinder", "VALIDATED", note)
        else:
            _record("hydraulic_size_cylinder", "BROKEN",
                    f"mismatch > 1%: {note}")

        # --- pump sizing ---
        # Independent check:
        #   displacement_cc = Q_lpm * 1000 / rpm  (cc/rev)
        #   hyd_power_kw    = Q_m3s * dP_pa / 1000  (kW)
        #   Q_m3s = Q_lpm / 60000
        #   shaft_power_kw  = hyd_power_kw / efficiency
        Q_lpm = 20.0
        dP_bar = 200.0
        rpm = 1450.0
        eta = 0.85
        disp_expected = Q_lpm * 1000.0 / rpm
        Q_m3s = Q_lpm / 60000.0
        dP_pa = dP_bar * 1e5
        hyd_pwr = Q_m3s * dP_pa / 1000.0    # kW
        shaft_pwr = hyd_pwr / eta

        result2 = hyd.size_pump(flow_lpm=Q_lpm, pressure_bar=dP_bar,
                                speed_rpm=rpm, efficiency=eta)
        assert result2.get("ok"), f"size_pump failed: {result2}"
        d_got = result2["displacement_cc_rev"]
        p_got = result2["shaft_power_kw"]
        d_err = _pct_err(d_got, disp_expected)
        p_err = _pct_err(p_got, shaft_pwr)
        note2 = (f"Q={Q_lpm} L/min, P={dP_bar} bar, N={rpm} rpm → "
                 f"disp={d_got:.4f} cc/rev (expect {disp_expected:.4f}, err={d_err:.2f}%), "
                 f"shaft_pwr={p_got:.4f} kW (expect {shaft_pwr:.4f}, err={p_err:.2f}%)")
        if d_err < 1.0 and p_err < 1.0:
            _record("hydraulic_size_pump", "VALIDATED", note2)
        else:
            _record("hydraulic_size_pump", "BROKEN", f"mismatch >1%: {note2}")

        # Schema/management tools — not physics solvers, mark REAL
        _record("hydraulic_new", "REAL",
                "schema factory only, no physics (engine.hydraulic.schema)", executed=False)
        _record("hydraulic_add_component", "REAL",
                "schema mutation only", executed=False)
        _record("hydraulic_add_hose", "REAL",
                "schema mutation only", executed=False)
        _record("hydraulic_bom", "REAL",
                "BOM aggregation from schema, no physics", executed=False)

    except Exception as exc:
        _record("hydraulic_size_cylinder", "BROKEN",
                f"import or runtime error: {exc}")
        _record("hydraulic_size_pump", "BROKEN",
                f"import or runtime error: {exc}")


# ===========================================================================
# FAMILY 2: Piping network solver
# Depends on 'fluids' (MIT). Degrades gracefully without it.
# ===========================================================================

def audit_piping() -> None:
    print("\n--- FAMILY: piping (engine.piping.solve) ---")
    try:
        from engine.piping import solve as pip_solve

        if not pip_solve.have_fluids():
            _skip("piping_solve", "fluids library not installed (soft-fail degradation)",
                  install="pip install fluids  # MIT license, in-process OK")
            _skip("piping_operating_point",
                  "fluids library not installed — same degradation path")
            _skip("piping_report",
                  "fluids library not installed — same degradation path")
            # Mark internal solver as REAL (implements Darcy-Weisbach, verified by source read)
            _record("piping_solve [source audit]", "REAL",
                    "engine/piping/solve.py: Darcy-Weisbach + Colebrook friction, "
                    "nodal head network (MNA analogue), K-factor fitting losses. "
                    "Cannot execute without fluids dep; verdict from source read.",
                    executed=False)
        else:
            # fluids available — run a live check
            from engine.piping import schema as pip_schema
            doc = pip_schema.new_piping("AuditNet")
            run = pip_schema.new_pipe_run(
                id="r0",
                path=[[0, 0, 0], [100000, 0, 0]],  # 100 m straight, in mm
                nps=2.0,  # 2-inch NPS ~ DN50
                schedule="40",
                name="main",
            )
            doc["runs"].append(run)
            # solve() takes a LIST of BC dicts ({'run','end','head'} — see
            # _apply_boundary), not a node-id->head mapping.
            boundary = [
                {"run": "r0", "end": "start", "head": 10.0},
                {"run": "r0", "end": "end", "head": 0.0},
            ]
            result = pip_solve.solve(doc, boundary=boundary)

            if not result.get("ok"):
                _record("piping_solve", "BROKEN",
                        f"solve returned not-ok: {result.get('reason')}")
            else:
                # Independent Darcy-Weisbach check (fully turbulent, Moody f ~ 0.02):
                #   v = sqrt(dH*2g*D / (f*L))
                #   Q = A*v
                # (order-of-magnitude; exact f depends on Reynolds iteration)
                links = result.get("links", [])
                if links:
                    Q_m3h = links[0].get("flow_m3h", 0.0)  # solver reports m3/h directly
                    # Rough expected: dH=10m, f=0.02, L=100m, D=0.0525m (2" sched40)
                    D = 0.0525
                    f_approx = 0.02
                    v_approx = math.sqrt(10.0 * 2 * 9.81 * D / (f_approx * 100.0))
                    Q_expected = math.pi / 4 * D ** 2 * v_approx * 3600.0  # m3/h
                    pct = _pct_err(Q_m3h, Q_expected)
                    note = (f"dH=10m, L=100m, DN50 → Q={Q_m3h:.3f} m³/h "
                            f"(Darcy-Weisbach rough estimate {Q_expected:.3f} m³/h, "
                            f"~{pct:.0f}% from approx f)")
                    # Wide tolerance because f depends on exact Re iteration
                    if pct < 40.0:
                        _record("piping_solve", "VALIDATED", note)
                    else:
                        _record("piping_solve", "BROKEN",
                                f"flow too far from Darcy-Weisbach estimate: {note}")
                else:
                    _record("piping_solve", "REAL",
                            "solve returned ok but no link flows in result")

        # operating_point / cfd tools
        _record("piping_operating_point", "REAL",
                "engine/piping/solve.py:operating_point — pump curve intersection + NPSH margin; "
                "code verified real physics; runtime depends on fluids dep",
                executed=False)
        _record("piping_report", "REAL",
                "engine/piping/solve.py:pressure_drop_report — K-factor breakdown per segment",
                executed=False)
        # Probe (not hardcode) the build123d/OCP kernel these three tools need.
        try:
            import build123d  # noqa: F401
            from engine.piping import cfd_handoff as _cfd  # noqa: F401
            _b3d_note = "build123d/OCP import OK; engine.piping.cfd_handoff imports clean"
            _b3d_verdict = "REAL"
        except Exception as _e:  # noqa: BLE001
            _b3d_note = f"requires build123d/OCP kernel: {type(_e).__name__}: {_e}"
            _b3d_verdict = "BROKEN"
        _record("piping_internal_volume", _b3d_verdict, _b3d_note, executed=False)
        _record("piping_cfd_handoff", _b3d_verdict,
                _b3d_note + " (bore extraction path)", executed=False)
        _record("piping_verify_cfd", _b3d_verdict, _b3d_note, executed=False)
        _record("piping_fluid", "REAL",
                "engine/piping/properties.py: CoolProp/thermo if available, else built-in table; "
                "graceful degradation confirmed",
                executed=False)

    except Exception as exc:
        _record("piping_solve", "BROKEN",
                f"import or runtime error: {exc}")


# ===========================================================================
# FAMILY 3: ECAD impedance (engine.ecad.impedance)
# Pure Python + scipy. No .so, no ngspice needed.
# ===========================================================================

def audit_ecad_impedance() -> None:
    print("\n--- FAMILY: ECAD impedance (engine.ecad.impedance) ---")
    try:
        from engine.ecad import impedance as ecad_imp

        # --- microstrip Z0 ---
        # Independent IPC-2141A surface-microstrip formula:
        #   Z0 = 87/sqrt(er+1.41) * ln(5.98*h / (0.8*w + t))
        # Geometry: w=3mm, h=1.6mm, t=0.035mm (1oz Cu), er=4.5 (FR4)
        w = 3.0; h = 1.6; t = 0.035; er = 4.5
        Z0_ind = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * w + t))
        result = ecad_imp.microstrip_z0(w, h, t, er)
        Z0_got = result["z0"]
        err = _pct_err(Z0_got, Z0_ind)
        note = (f"microstrip w={w},h={h},t={t},er={er} → "
                f"Z0={Z0_got:.3f} Ω (IPC-2141A: {Z0_ind:.3f} Ω, err={err:.3f}%)")
        if err < 0.1:  # same formula — should be exact
            _record("ecad_check_impedance [microstrip]", "VALIDATED", note)
        else:
            _record("ecad_check_impedance [microstrip]", "BROKEN",
                    f"Z0 mismatch: {note}")

        # --- stripline Z0 ---
        # IPC-2141A symmetric stripline:
        #   Z0 = 60/sqrt(er) * ln(4*h / (0.67*pi*(0.8*w + t)))
        # w=3mm, h=3.2mm (total plane-to-plane), t=0.035mm, er=4.5
        w2 = 3.0; h2 = 3.2; t2 = 0.035; er2 = 4.5
        Z0_strip_ind = (60.0 / math.sqrt(er2)) * math.log(
            4.0 * h2 / (0.67 * math.pi * (0.8 * w2 + t2)))
        result2 = ecad_imp.stripline_z0(w2, h2, t2, er2)
        Z0_strip_got = result2["z0"]
        err2 = _pct_err(Z0_strip_got, Z0_strip_ind)
        note2 = (f"stripline w={w2},h={h2},t={t2},er={er2} → "
                 f"Z0={Z0_strip_got:.3f} Ω (IPC-2141A: {Z0_strip_ind:.3f} Ω, err={err2:.3f}%)")
        if err2 < 0.1:
            _record("ecad_check_impedance [stripline]", "VALIDATED", note2)
        else:
            _record("ecad_check_impedance [stripline]", "BROKEN",
                    f"stripline Z0 mismatch: {note2}")

        # --- diff pair ---
        # Empirical coupling de-rating: Z_diff = 2*Z0_se*(1 - 0.48*exp(-0.96*s/h))
        # s = gap between traces
        s = 0.2; h3 = 1.6
        Z0_se = Z0_ind  # same single-ended microstrip as above
        Z_diff_ind = 2.0 * Z0_se * (1.0 - 0.48 * math.exp(-0.96 * s / h3))
        # diff_pair_z0 signature: (w, s, h, t, er, kind='microstrip')
        result3 = ecad_imp.diff_pair_z0(w, s, h, t, er, kind='microstrip')
        Z_diff_got = result3["z0_diff"]
        err3 = _pct_err(Z_diff_got, Z_diff_ind)
        note3 = (f"diff pair s={s}mm → Z_diff={Z_diff_got:.3f} Ω "
                 f"(formula: {Z_diff_ind:.3f} Ω, err={err3:.3f}%)")
        if err3 < 1.0:
            _record("ecad_check_diff_impedance", "VALIDATED", note3)
        else:
            _record("ecad_check_diff_impedance", "BROKEN",
                    f"diff Z mismatch: {note3}")

    except Exception as exc:
        _record("ecad_check_impedance", "BROKEN",
                f"import or runtime error: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 4: ECAD circuit simulation (engine.sim.circuit MNA solver)
# Checks ecad_simulate and ecad_tf
# ===========================================================================

def audit_ecad_sim() -> None:
    print("\n--- FAMILY: ECAD circuit sim (engine.sim.circuit) ---")
    try:
        import numpy as np
        from engine.sim.circuit import simulate

        # --- RC low-pass AC (gate 8 format from test_circuit.py) ---
        # R=1k, C=159.155nF → fc = 1/(2*pi*R*C) = 1000 Hz
        # At f=fc: |H| = 1/sqrt(2) = -3.01 dB
        R = 1000.0; C = 159.155e-9
        fc_expected = 1.0 / (2.0 * math.pi * R * C)  # = 1000 Hz
        H_fc_expected = 1.0 / math.sqrt(2.0)          # = 0.7071

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
            "analyses": [{"type": "ac", "sweep": "dec", "points": 20,
                          "fstart": 10, "fstop": 1e6}],
            "probes": ["V(OUT)"],
        }
        result = simulate(circuit, study)

        if not result.get("convergence", {}).get("ac"):
            _record("ecad_simulate [ac]", "BROKEN",
                    f"AC analysis did not converge: log={result.get('log', [])}")
        else:
            f = np.array(result["ac"]["x_f"])
            mag = np.array(result["ac"]["mag"]["V(OUT)"])
            H_fc_got = float(np.interp(fc_expected, f, mag))
            err = _pct_err(H_fc_got, H_fc_expected)
            note = (f"RC LP fc={fc_expected:.0f} Hz: "
                    f"|H(fc)|={H_fc_got:.5f} (expect {H_fc_expected:.5f}, err={err:.2f}%)")
            if err < 1.0:
                _record("ecad_simulate [ac]", "VALIDATED", note)
            else:
                _record("ecad_simulate [ac]", "BROKEN",
                        f"AC magnitude wrong at corner: {note}")

        # --- DC op-point: resistor divider (gate 1 format) ---
        # 10V across R1=1k, R2=2k → V(OUT) = 10 * 2k/3k = 6.667 V
        V_supply = 10.0
        R1v = 1000.0; R2v = 2000.0
        V_out_expected = V_supply * R2v / (R1v + R2v)

        circuit2 = {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": V_supply, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
                 "value": R1v, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"],
                 "value": R2v, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study2 = {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)"]}
        result2 = simulate(circuit2, study2)

        if not result2.get("convergence", {}).get("op"):
            _record("ecad_simulate [op]", "BROKEN",
                    f"DC OP did not converge: log={result2.get('log', [])}")
        else:
            V_got = result2["op"]["V(OUT)"]
            err2 = _pct_err(V_got, V_out_expected)
            note2 = (f"R-divider: V(OUT)={V_got:.6f} V "
                     f"(expect {V_out_expected:.6f} V, err={err2:.4f}%)")
            if err2 < 0.01:
                _record("ecad_simulate [op]", "VALIDATED", note2)
            else:
                _record("ecad_simulate [op]", "BROKEN",
                        f"DC OP wrong: {note2}")

        # ecad_tf wraps run_tf on the same MNA solver
        _record("ecad_tf", "REAL",
                "wraps engine.sim.circuit.run_tf — same MNA solver as ecad_simulate; "
                "no independent tf check executed (solver validated via ac/op gates above)",
                executed=False)

    except ImportError as exc:
        _skip("ecad_simulate", f"numpy/scipy not available: {exc}")
        _skip("ecad_tf", f"numpy/scipy not available: {exc}")
    except Exception as exc:
        _record("ecad_simulate", "BROKEN",
                f"unexpected error: {exc}")
        traceback.print_exc()

    # ngspice
    import subprocess
    r = subprocess.run(["which", "ngspice"], capture_output=True, text=True)
    if not r.stdout.strip():
        _record("ecad_simulate [ngspice path]", "SKIP",
                "ngspice not found on PATH — SPICE netlist pass-through unavailable; "
                "install: sudo dnf install ngspice  (GPL2; subprocess-only OK)",
                executed=False)
    else:
        _record("ecad_simulate [ngspice]", "REAL",
                f"ngspice found at {r.stdout.strip()} — subprocess isolation path present",
                executed=False)


# ===========================================================================
# FAMILY 5: FEA static + modal + fatigue + thermal (core C++ via core_bridge)
# Requires libaicad.so. T-SIM4 UPDATE: the AICAD_WITH_NETGEN build
# (core/build-occt-linux) registers sim_run_static/modal/fatigue/thermal
# DIRECTLY on the raw cb.call() bridge (core/api/libaicad.cpp), so the audit
# now solves LIVE when that build is staged. The T-V1-era "not exposed on the
# raw bridge" finding only holds for the plain build-linux .so.
# ===========================================================================

def _load_core_bridge():
    """Import core_bridge (or return (None, reason))."""
    lib_path = os.environ.get("AI_CAD_LIBAICAD", "")
    if not lib_path or not os.path.exists(lib_path):
        return None, ("AI_CAD_LIBAICAD not set or .so not found — "
                      "export AI_CAD_LIBAICAD=core/build-occt-linux/bin/libaicad.so "
                      "(the netgen build carries the FEA RPCs)")
    mcp_path = os.path.join(_PROJECT_ROOT, "services", "mcp")
    if mcp_path not in sys.path:
        sys.path.insert(0, mcp_path)
    try:
        import core_bridge as cb  # noqa: PLC0415
        return cb, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"core_bridge import failed: {exc}"


_CANTILEVER_CTX: dict = {}


def _cantilever_setup(cb) -> dict:
    """Build the audit cantilever (box 1000x50x50) once; return doc/step/facesets.

    Returns {doc_id, step, fix_fp, load_fp} or {"error": ...}. Cached so the
    static/modal/fatigue families share one geometry build.
    """
    if _CANTILEVER_CTX:
        return _CANTILEVER_CTX
    import tempfile
    L_mm, b_mm, h_mm = 1000.0, 50.0, 50.0
    doc_id = cb.call("doc_new", {"kind": "part", "name": "audit_cantilever"})["doc_id"]
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id,
        "ops": [{"op": "add_feature", "feature": {
            "type": "box", "name": "Beam",
            "params": {
                "length": {"value": L_mm, "expr": None},
                "width":  {"value": b_mm, "expr": None},
                "height": {"value": h_mm, "expr": None},
            },
        }}]})
    fs = (r.get("rebuild") or {}).get("feature_status", [])
    if not (fs and all(f.get("ok") for f in fs)):
        _CANTILEVER_CTX["error"] = f"box rebuild failed: {fs}"
        return _CANTILEVER_CTX
    step = tempfile.mktemp(suffix=".step", prefix="sim_audit_")
    cb.call("doc_export_step", {"doc_id": doc_id, "path": step})

    def faceset(side):
        q = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face", "side": side}})
        m = [x for x in q.get("matches", []) if x.get("kind") == "face"]
        return cb.call("doc_face_points", {"doc_id": doc_id, "selector": m[0]})

    _CANTILEVER_CTX.update({
        "doc_id": doc_id, "step": step,
        "fix_fp": faceset("left"), "load_fp": faceset("right"),
        "L_mm": L_mm, "b_mm": b_mm, "h_mm": h_mm,
    })
    return _CANTILEVER_CTX


_AUDIT_STEEL = {"name": "audit-steel", "E_mpa": 200000.0, "nu": 0.3,
                "density_g_cm3": 7.85, "yield_mpa": 250.0, "ultimate_mpa": 400.0}


def audit_fea() -> None:
    print("\n--- FAMILY: FEA static + modal (C++ core, core_bridge, LIVE) ---")

    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("run_fea_static", reason)
        _skip("run_fea_modal", reason)
        return
    from core_bridge import CoreError  # noqa: PLC0415

    # Independent references (first principles, computed HERE):
    #   Euler-Bernoulli cantilever, square 50x50mm, L=1m, steel, P=1000N at tip:
    #   I = b*h^3/12 = 50^4/12 = 520833.33 mm^4;  E = 200000 MPa
    #   delta = P*L^3/(3*E*I) = 3.2000 mm         (Roark 8th Table 8.1 case 1a)
    #   f1 = (1.8751)^2/(2*pi*L^2)*sqrt(E*I/(rho*A)) = 40.77 Hz
    #                                             (Rao 5th §8.2 Table 8.1)
    L_mm = 1000.0; b_mm = 50.0; h_mm = 50.0
    P_N = 1000.0; E_mpa = _AUDIT_STEEL["E_mpa"]
    I_mm4 = b_mm * h_mm ** 3 / 12.0
    delta_expected_mm = P_N * L_mm ** 3 / (3.0 * E_mpa * I_mm4)
    beta_L = 1.8751
    L_m = L_mm * 1e-3
    A_m2 = (b_mm * 1e-3) * (h_mm * 1e-3)
    I_m4 = I_mm4 * 1e-12
    rho_si = _AUDIT_STEEL["density_g_cm3"] * 1000.0
    E_si = E_mpa * 1e6
    f1_expected_hz = (beta_L ** 2 / (2.0 * math.pi * L_m ** 2)) * math.sqrt(
        E_si * I_m4 / (rho_si * A_m2))
    print(f"  Cantilever refs: delta={delta_expected_mm:.4f} mm, f1={f1_expected_hz:.2f} Hz")

    try:
        ctx = _cantilever_setup(cb)
        if ctx.get("error"):
            _record("run_fea_static", "BROKEN", ctx["error"])
            _record("run_fea_modal", "BROKEN", ctx["error"])
            return

        static_study = {
            "step_path": ctx["step"], "material": _AUDIT_STEEL,
            "fixtures": [ctx["fix_fp"]],
            "loads": [{"faces": ctx["load_fp"], "type": "force",
                       "vector": [0.0, 0.0, -P_N]}],
            "mesh_order": 2, "fineness": 0.5,
        }
        try:
            res = cb.call("sim_run_static", {"study": static_study})
        except CoreError as exc:
            if "unknown method" in str(exc):
                # Plain (non-netgen) build: fall back to the T-V1 source-audit verdict.
                _record("run_fea_static", "REAL",
                        "geometry pre-step OK but this .so lacks the netgen FEA RPCs "
                        "(sim_run_static: unknown method). Stage "
                        "core/build-occt-linux/bin/libaicad.so to solve live. "
                        f"E-B reference: delta={delta_expected_mm:.4f} mm.",
                        executed=False)
                _record("run_fea_modal", "REAL",
                        "same build limitation — needs the AICAD_WITH_NETGEN .so. "
                        f"Beam-theory reference: f1={f1_expected_hz:.2f} Hz.",
                        executed=False)
                return
            raise
        if not res.get("ok"):
            _record("run_fea_static", "BROKEN", f"live solve failed: {res.get('error')}")
        else:
            got = (res.get("summary") or {}).get("max_disp_mm")
            err = _pct_err(got, delta_expected_mm) if got is not None else 1e9
            note = (f"LIVE cantilever solve: max_disp={got} mm "
                    f"(E-B delta={delta_expected_mm:.4f} mm, err={err:.2f}%), "
                    f"n_dof={res.get('n_dof')}")
            if err < 10.0:
                _record("run_fea_static [live]", "VALIDATED", note)
            else:
                _record("run_fea_static [live]", "BROKEN",
                        f"deflection off beam theory by {err:.1f}%: {note}")
            # Stash the live stress field summary for the fatigue cross-check.
            _CANTILEVER_CTX["static_summary"] = res.get("summary") or {}
            _CANTILEVER_CTX["static_study"] = static_study

        modal_study = {"step_path": ctx["step"], "material": _AUDIT_STEEL,
                       "fixtures": [ctx["fix_fp"]], "mesh_order": 2, "fineness": 0.5}
        mres = cb.call("sim_run_modal", {"study": modal_study, "n_modes": 4})
        if not mres.get("ok"):
            _record("run_fea_modal", "BROKEN", f"live modal failed: {mres.get('error')}")
        else:
            freqs = mres.get("freqs_hz") or []
            f1 = freqs[0] if freqs else None
            err = _pct_err(f1, f1_expected_hz) if f1 is not None else 1e9
            note = (f"LIVE modal solve: f1={f1:.2f} Hz "
                    f"(beam theory {f1_expected_hz:.2f} Hz, err={err:.2f}%); "
                    f"modes={['%.1f' % f for f in freqs]}")
            if err < 10.0:
                _record("run_fea_modal [live]", "VALIDATED", note)
            else:
                _record("run_fea_modal [live]", "BROKEN",
                        f"f1 off beam theory by {err:.1f}%: {note}")

    except CoreError as exc:
        _record("run_fea_static", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("run_fea_static", "BROKEN", f"Unexpected FEA audit error: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 5b: FEA fatigue (T-SIM4) — S-N math anchors (pure Python, always run)
# + live sim_run_fatigue engine cross-check when the netgen .so is staged.
# ===========================================================================

def audit_fatigue() -> None:
    print("\n--- FAMILY: FEA fatigue (S-N anchors + live engine cross-check) ---")

    # --- Pure-math anchors (run everywhere; references computed here) --------
    # Shigley 10th Eq 6-14..6-16 (f=0.9): a=(0.9*400)^2/200=648;
    #   b=-1/3*log10(1.8)=-0.085085; N=(250/648)^(1/b)=7.2627e4 cycles.
    try:
        sys.path.insert(0, _PROJECT_ROOT) if _PROJECT_ROOT not in sys.path else None
        from services.mcp.fea_advanced import sn_life  # noqa: PLC0415
        from services.agent.advisor.sim_advisor import fatigue_anchor  # noqa: PLC0415

        n_expected = (250.0 / ((0.9 * 400.0) ** 2 / 200.0)) ** (
            1.0 / (-(1.0 / 3.0) * math.log10(0.9 * 400.0 / 200.0)))
        n_got = sn_life(250.0, 200.0, 400.0)
        err = _pct_err(n_got, n_expected)
        note = (f"sa_eq=250, Se=200, Sut=400 MPa → N={n_got:.0f} cycles "
                f"(Shigley Eq 6-14..6-16 hand calc {n_expected:.0f}, err={err:.2f}%)")
        if err < 1.0:
            _record("sn_life (fea_advanced)", "VALIDATED", note)
        else:
            _record("sn_life (fea_advanced)", "BROKEN", f"S-N life mismatch: {note}")

        # fatigue_anchor Goodman zero-based hand case:
        #   smax=200, smin=0, Sut=400, Se'=0.5*Sut=200 (Shigley Eq 6-8):
        #   sa=100, sm=100 → sa_eq=100/(1-100/400)=133.333 MPa (Eq 6-46); FoS=1.5
        a = fatigue_anchor(200.0, 0.0, ultimate_mpa=400.0)
        sa_eq_expected = 100.0 / (1.0 - 100.0 / 400.0)
        if a is None:
            _record("fatigue_anchor (sim_advisor)", "BROKEN",
                    "anchor returned None for a well-posed Goodman case")
        else:
            err = _pct_err(a["sa_eq_mpa"], sa_eq_expected)
            note = (f"Goodman R=0: sa_eq={a['sa_eq_mpa']:.3f} MPa "
                    f"(hand calc {sa_eq_expected:.3f}, err={err:.3f}%), "
                    f"regime={a['regime']}, FoS={a['fos_endurance']:.2f}")
            if err < 0.1 and a["regime"] == "infinite":
                _record("fatigue_anchor (sim_advisor)", "VALIDATED", note)
            else:
                _record("fatigue_anchor (sim_advisor)", "BROKEN", note)
    except Exception as exc:  # noqa: BLE001
        _record("fatigue math anchors", "BROKEN", f"import/run error: {exc}")
        traceback.print_exc()
        return

    # --- Live engine cross-check (needs the netgen .so + a prior static solve) --
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("run_fea_fatigue [live]", reason)
        return
    from core_bridge import CoreError  # noqa: PLC0415
    study = _CANTILEVER_CTX.get("static_study")
    vm = (_CANTILEVER_CTX.get("static_summary") or {}).get("max_von_mises_mpa")
    if not study or vm is None:
        _skip("run_fea_fatigue [live]",
              "no live static solve available (see run_fea_static row) — "
              "fatigue reuses that stress field")
        return
    try:
        fat_study = dict(study)
        fat_study["fatigue"] = {"load_type": "reversed"}   # lmin=-1, lmax=1
        res = cb.call("sim_run_fatigue", {"study": fat_study})
        if not res.get("ok"):
            _record("run_fea_fatigue [live]", "BROKEN",
                    f"live fatigue failed: {res.get('error')}")
            return
        theories = ((res.get("summary") or {}).get("theories") or {})
        goodman = (theories.get("goodman") or {}).get("sa_eq_mpa")
        # Fully reversed: sm=0 → Goodman sa_eq must equal the static peak vM.
        err = _pct_err(goodman, vm) if goodman is not None else 1e9
        anchor = fatigue_anchor(vm, -vm, ultimate_mpa=_AUDIT_STEEL["ultimate_mpa"],
                                endurance_mpa=0.5 * _AUDIT_STEEL["ultimate_mpa"])
        anchor_err = (_pct_err(goodman, anchor["sa_eq_mpa"])
                      if (goodman is not None and anchor) else 1e9)
        note = (f"reversed load: engine Goodman sa_eq={goodman} MPa vs static "
                f"peak vM={vm} MPa (err={err:.3f}%) and vs fatigue_anchor "
                f"(err={anchor_err:.3f}%)")
        if err < 1.0 and anchor_err < 1.0:
            _record("run_fea_fatigue [live]", "VALIDATED", note)
        else:
            _record("run_fea_fatigue [live]", "BROKEN", f"sa_eq mismatch: {note}")
    except CoreError as exc:
        if "unknown method" in str(exc):
            _skip("run_fea_fatigue [live]",
                  "this .so lacks sim_run_fatigue (needs AICAD_WITH_NETGEN build)")
        else:
            _record("run_fea_fatigue [live]", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("run_fea_fatigue [live]", "BROKEN", f"unexpected: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 5c: FEA thermal (T-SIM1 solver, audited by T-SIM4) — plane-wall
# anchors (pure Python, always run) + live sim_run_thermal when staged.
# ===========================================================================

def audit_thermal() -> None:
    print("\n--- FAMILY: FEA thermal (plane-wall anchors + live conduction solve) ---")

    # --- Pure-math anchor checks (run everywhere) ----------------------------
    # 1D plane wall (Incropera §3.1): q = k*dT/L. Slab 10mm thick, k=50 W/mK,
    # 100→0 °C: q = 50*100/0.010 = 500000 W/m².
    try:
        if _PROJECT_ROOT not in sys.path:
            sys.path.insert(0, _PROJECT_ROOT)
        from services.agent.advisor.sim_advisor import thermal_anchor  # noqa: PLC0415
        q_expected = 50.0 * (100.0 - 0.0) / 0.010
        t = thermal_anchor({"x_mm": 10, "y_mm": 100, "z_mm": 100}, 50.0, 100.0,
                           t_cold_c=0.0)
        if t is None:
            _record("thermal_anchor (sim_advisor)", "BROKEN",
                    "anchor returned None for a well-posed conduction case")
        else:
            err = _pct_err(t["q_w_m2"], q_expected)
            note = (f"conduction: q={t['q_w_m2']:.0f} W/m² "
                    f"(Fourier hand calc {q_expected:.0f}, err={err:.3f}%)")
            if err < 0.1:
                _record("thermal_anchor (sim_advisor)", "VALIDATED", note)
            else:
                _record("thermal_anchor (sim_advisor)", "BROKEN", note)
    except Exception as exc:  # noqa: BLE001
        _record("thermal_anchor (sim_advisor)", "BROKEN", f"import/run error: {exc}")
        traceback.print_exc()
        return

    # --- Live conduction solve (Dirichlet hot + convection cold) -------------
    # Bar 100 (X) × 20 × 20 mm, k=50 W/mK; x=0 fixed 100 °C, x=100mm convection
    # h=500 W/m²K to 20 °C ambient. Series resistance (Incropera §3.1):
    #   q = (T_hot - T_amb)/(L/k + 1/h) = 80/(0.002+0.002) = 20000 W/m²
    #   T_cold_face = T_amb + q/h = 20 + 40 = 60.0 °C ← the solve-dependent anchor
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("sim_run_thermal [live]", reason)
        return
    from core_bridge import CoreError  # noqa: PLC0415
    try:
        import tempfile
        L_mm, w_mm = 100.0, 20.0
        k_w_mk, t_hot, h_conv, t_amb = 50.0, 100.0, 500.0, 20.0
        q_ref = (t_hot - t_amb) / ((L_mm / 1000.0) / k_w_mk + 1.0 / h_conv)
        t_cold_ref = t_amb + q_ref / h_conv

        doc_id = cb.call("doc_new", {"kind": "part", "name": "audit_thermal_bar"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Bar",
                "params": {"length": {"value": L_mm, "expr": None},
                           "width": {"value": w_mm, "expr": None},
                           "height": {"value": w_mm, "expr": None}}}}]})
        step = tempfile.mktemp(suffix=".step", prefix="sim_audit_th_")
        cb.call("doc_export_step", {"doc_id": doc_id, "path": step})

        def faceset(side):
            q = cb.call("doc_query", {"doc_id": doc_id,
                                      "query": {"of": "face", "side": side}})
            m = [x for x in q.get("matches", []) if x.get("kind") == "face"]
            return cb.call("doc_face_points", {"doc_id": doc_id, "selector": m[0]})

        hot_fp = faceset("left")
        cold_fp = faceset("right")
        hot_fp["temp_c"] = t_hot
        study = {
            "step_path": step,
            "material": {"k_w_mk": k_w_mk},
            "fixtures": [hot_fp],
            "loads": [{"type": "convection", "faces": cold_fp,
                       "h": h_conv, "t_amb_c": t_amb}],
            "mesh_order": 2, "fineness": 0.5,
        }
        res = cb.call("sim_run_thermal", {"study": study})
        cb.call("doc_close", {"doc_id": doc_id})
        try:
            os.unlink(step)
        except OSError:
            pass
        if not res.get("ok"):
            _record("sim_run_thermal [live]", "BROKEN",
                    f"live thermal failed: {res.get('error')}")
            return
        summ = res.get("summary") or {}
        t_max, t_min = summ.get("max_temp_c"), summ.get("min_temp_c")
        err_cold = _pct_err(t_min, t_cold_ref) if t_min is not None else 1e9
        err_hot = _pct_err(t_max, t_hot) if t_max is not None else 1e9
        note = (f"LIVE conduction+convection bar: T range [{t_min}, {t_max}] °C "
                f"(series-resistance refs: cold face {t_cold_ref:.1f} °C, "
                f"hot face {t_hot:.1f} °C; err cold={err_cold:.2f}%, hot={err_hot:.2f}%)")
        if err_cold < 2.0 and err_hot < 0.1:
            _record("sim_run_thermal [live]", "VALIDATED", note)
        else:
            _record("sim_run_thermal [live]", "BROKEN", f"temperature mismatch: {note}")
    except CoreError as exc:
        if "unknown method" in str(exc):
            _skip("sim_run_thermal [live]",
                  "this .so lacks sim_run_thermal (needs AICAD_WITH_NETGEN build)")
        else:
            _record("sim_run_thermal [live]", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("sim_run_thermal [live]", "BROKEN", f"unexpected: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 6: CFD (LBM built-in + OpenFOAM)
# ===========================================================================

def audit_cfd() -> None:
    print("\n--- FAMILY: CFD (LBM + OpenFOAM) ---")
    try:
        from engine.sim.foam_runner import capabilities
        caps = capabilities()

        lbm_ok = caps.get("lbm", False)
        foam_ok = caps.get("openfoam", False)

        if lbm_ok:
            # LBM is built into the C++ core (cb.call("sim_run_flow"))
            # Source audit: cad_tools.py calls cb.call("sim_run_flow", {...})
            # The docstring explicitly warns: "ABSOLUTE Cd IS UNCALIBRATED" —
            # the code is REAL (LBM physics implemented) but intentionally uncalibrated.
            # Independent check: drag scales as V^2, Cd velocity-independent.
            # Cannot run headless without building a part doc first; source-audit only.
            _record("run_cfd_flow [lbm]", "REAL",
                    "C++ LBM wind tunnel: cad_tools.py:run_cfd_flow calls "
                    "cb.call('sim_run_flow'). Engine docstring states: 'ABSOLUTE Cd UNCALIBRATED "
                    "— coarse voxel LBM staircased surface over-counts force'. REAL physics "
                    "(LBM lattice solve), not validated for absolute Cd. "
                    "Cannot run headless without a part doc; source-only verdict.",
                    executed=False)
        else:
            _skip("run_cfd_flow [lbm]",
                  "LBM reported not available in capabilities()")

        if not foam_ok:
            _skip("run_cfd_accurate",
                  "OpenFOAM not installed on this machine",
                  install="python -m tools.provision_openfoam --install  "
                          "(WSL openfoam2412 or blueCFD — GPL2 subprocess isolation)")
        else:
            _record("run_cfd_accurate [openfoam]", "REAL",
                    f"OpenFOAM {caps.get('openfoam_version', 'unknown')} available. "
                    "Calls engine.sim.openfoam.run_openfoam_tunnel — RANS k-omega SST "
                    "with prism layers. Docstring: 'sphere/cube within ~10-15% of experiment'.",
                    executed=False)

    except Exception as exc:
        _record("run_cfd_flow", "BROKEN",
                f"foam_runner import error: {exc}")


# ===========================================================================
# FAMILY 7: Structural (engine.structural — PyNite-based)
# ===========================================================================

def audit_structural() -> None:
    print("\n--- FAMILY: structural (engine.structural, PyNite) ---")
    try:
        import Pynite as PyNite  # PyNiteFEA installs module 'Pynite'
        pynite_ok = True
    except ImportError:
        pynite_ok = False

    if not pynite_ok:
        _skip("struct_analyze",
              "PyNite not installed in mcp-venv",
              install="pip install PyNite  # MIT license, in-process OK")
        _skip("struct_check",
              "PyNite not installed in mcp-venv (structural checks depend on analysis)")

        # Source audit: engine/structural/analysis.py uses PyNite for frame FE solve
        _record("struct_analyze [source audit]", "REAL",
                "engine/structural/analysis.py: uses PyNite FE for member forces/deflections "
                "under ASCE 7 load combos. StructureDoc (v1 schema) → PyNite FEM → "
                "AnalysisResult (N, mm, MPa). Cannot run without PyNite; verdict from source.",
                executed=False)
        _record("struct_check [source audit]", "REAL",
                "engine/structural/checks.py: LRFD D/C ratio checks — "
                "dc_ratio_axial, dc_ratio_bending, wood variants. Pure math, no PyNite needed "
                "for checks themselves, but requires analysis results as input.",
                executed=False)
        return

    # PyNite available — run a cantilever beam check
    # Cantilever: L=3000mm (3m), W200x100 I-beam, steel, P=5000N at tip
    # Expected: delta = P*L^3/(3*E*I)
    from engine.structural import analysis as sa

    try:
        import numpy as np
        from Pynite import FEModel3D  # PyNiteFEA installs module "Pynite"

        # Simple hand-built PyNite cantilever (bypasses StructureDoc to test PyNite directly)
        model = FEModel3D()
        model.add_material("steel", E=200000.0, G=77000.0, nu=0.3, rho=7.85e-3)
        # W200x100 approx: Iy=113e6 mm^4, Iz=36.9e6 mm^4
        # Use 50x200 rect for simpler independent check
        b_r = 50.0; h_r = 200.0
        A_r = b_r * h_r
        # Pynite local axes for a member along global X: a global FZ (vertical) load
        # bends the member ABOUT local y — so Iy must carry the b*h^3/12 (stiff) value
        # for a section h tall in Z. (Verified empirically: swapping these gives exactly
        # (h/b)^2 = 16x the deflection.)
        Iy_r = b_r * h_r ** 3 / 12.0
        Iz_r = h_r * b_r ** 3 / 12.0
        J_r = 0.141 * b_r * h_r ** 3 / (b_r / h_r)  # approx torsion constant
        L_r = 3000.0

        model.add_node("A", 0, 0, 0)
        model.add_node("B", L_r, 0, 0)
        # Pynite >=1.0 API: sections are named objects (same call shape as
        # engine/structural/analysis.py uses in production).
        model.add_section("rect50x200", A_r, Iy_r, Iz_r, J_r)
        model.add_member("m1", "A", "B", "steel", "rect50x200")
        model.def_support("A", True, True, True, True, True, True)
        model.add_node_load("B", "FZ", -5000.0)
        model.analyze(check_statics=False)
        delta_got = abs(model.nodes["B"].DZ["Combo 1"])  # Pynite 1.x: displacement dicts are DX/DY/DZ

        E_r = 200000.0
        I_r = Iy_r  # FZ load bends about local y (see axis note above)
        delta_expected = 5000.0 * L_r ** 3 / (3.0 * E_r * I_r)

        err = _pct_err(delta_got, delta_expected)
        note = (f"PyNite cantilever 50x200mm, L=3000mm, P=5000N: "
                f"delta={delta_got:.4f}mm (E-B: {delta_expected:.4f}mm, err={err:.2f}%)")
        if err < 1.0:
            _record("struct_analyze", "VALIDATED", note)
        else:
            _record("struct_analyze", "BROKEN",
                    f"PyNite deflection wrong: {note}")

        _record("struct_check", "REAL",
                "engine/structural/checks.py: LRFD D/C ratio checks verified by source; "
                "pure math using analysis output — no separate check needed",
                executed=False)

    except Exception as exc:
        _record("struct_analyze", "BROKEN",
                f"PyNite available but analysis failed: {exc}")
        traceback.print_exc()


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> int:
    print("=" * 70)
    print("sim_audit.py — T-V1 Simulation Backend Audit")
    print(f"Python: {sys.executable}")
    print(f"AI_CAD_LIBAICAD: {os.environ.get('AI_CAD_LIBAICAD', '(not set)')}")
    print("=" * 70)

    t0 = time.perf_counter()
    audit_hydraulic()
    audit_piping()
    audit_ecad_impedance()
    audit_ecad_sim()
    audit_fea()
    audit_fatigue()
    audit_thermal()
    audit_cfd()
    audit_structural()
    elapsed = time.perf_counter() - t0

    print()
    print("=" * 70)
    print(f"SUMMARY  executed={_EXECUTED}  skipped={_SKIPPED}  broken={_BROKEN}")
    print(f"Elapsed: {elapsed:.2f}s")
    print("=" * 70)
    print()
    print(f"{'Tool':<45} {'Verdict':<12} {'Note'}")
    print("-" * 120)
    for r in _RESULTS:
        print(f"  {r['tool']:<43} {r['verdict']:<12} {r['note'][:70]}")
    print()

    if _EXECUTED == 0:
        print("ERROR: zero checks executed — all-skip is a FAILURE")
        return 1
    if _BROKEN > 0:
        print(f"FAIL: {_BROKEN} BROKEN finding(s)")
        return 1
    print("PASS: all executed checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
