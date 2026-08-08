#!/usr/bin/env python3
"""Executable audit of the current simulation surface.

Each family runs a deterministic public-module or raw-RPC probe and prints an
explicit verdict:

REAL      = a capability or physical invariant was exercised successfully
VALIDATED = numerical output matches an independent analytic/conservation anchor
STUB      = a reachable backend returns placeholder output
BROKEN    = an available backend errors, fails to converge, or returns wrong numbers
SKIP      = a required optional dependency/backend is unavailable (with a reason)

Independent reference values are derived from first principles inside this
script. Optional backends are isolated per family: an absent package or solver
must produce SKIP, never PASS, and a BROKEN result always makes the process exit
nonzero.

Usage:
    CHUDWARE_LIBCHUDWARE=/path/to/libchudware.so \
        ~/.local/share/Chudware/mcp-venv/bin/python scripts/sim_audit.py

Exit code: 0 if all executed checks pass; 1 if any check is BROKEN.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import time
import traceback

# Ensure the project root (containing ``services``) is importable regardless of
# which directory invokes this script.
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
# Current C++ RPCs; libchudware is optional for the rest of the harness.
# ===========================================================================

def audit_hydraulic() -> None:
    print("\n--- FAMILY: hydraulic (core hydraulic_size_* RPCs) ---")
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("hydraulic_size_cylinder", reason)
        _skip("hydraulic_size_pump", reason)
        return

    bore_mm, rod_mm, pressure_bar = 50.0, 20.0, 200.0
    stroke_mm, velocity_mm_s, efficiency = 100.0, 50.0, 0.95
    area_m2 = math.pi / 4.0 * (bore_mm * 1e-3) ** 2
    force_expected = area_m2 * pressure_bar * 1e5 * efficiency * 1e-3
    flow_expected = area_m2 * velocity_mm_s * 1e-3 * 60.0 * 1000.0
    try:
        result = cb.call("hydraulic_size_cylinder", {
            "bore_mm": bore_mm, "rod_mm": rod_mm, "pressure_bar": pressure_bar,
            "stroke_mm": stroke_mm, "velocity_mm_s": velocity_mm_s,
            "efficiency": efficiency,
        })
        if not result.get("ok"):
            _record("hydraulic_size_cylinder", "BROKEN",
                    f"reasonable sizing input was refused: {result}")
        else:
            force_got = float(result["extend_force_kn"])
            flow_got = float(result["flow_extend_lpm"])
            f_err = _pct_err(force_got, force_expected)
            q_err = _pct_err(flow_got, flow_expected)
            note = (f"A×P gives {force_got:.4f} kN vs {force_expected:.4f} "
                    f"({f_err:.3f}%); A×v gives {flow_got:.4f} L/min vs "
                    f"{flow_expected:.4f} ({q_err:.3f}%)")
            _record("hydraulic_size_cylinder",
                    "VALIDATED" if max(f_err, q_err) < 0.1 else "BROKEN", note)
    except cb.CoreError as exc:
        if "unknown method" in str(exc):
            _skip("hydraulic_size_cylinder",
                  "loaded libchudware does not expose hydraulic_size_cylinder")
        else:
            _record("hydraulic_size_cylinder", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("hydraulic_size_cylinder", "BROKEN", f"runtime error: {exc}")

    flow_lpm, speed_rpm, eta = 20.0, 1450.0, 0.85
    displacement_expected = flow_lpm * 1000.0 / speed_rpm
    shaft_expected = (flow_lpm / 60000.0) * (pressure_bar * 1e5) / 1000.0 / eta
    try:
        result = cb.call("hydraulic_size_pump", {
            "flow_lpm": flow_lpm, "pressure_bar": pressure_bar,
            "speed_rpm": speed_rpm, "efficiency": eta,
        })
        if not result.get("ok"):
            _record("hydraulic_size_pump", "BROKEN",
                    f"reasonable sizing input was refused: {result}")
        else:
            disp_got = float(result["displacement_cc_rev"])
            power_got = float(result["shaft_power_kw"])
            d_err = _pct_err(disp_got, displacement_expected)
            p_err = _pct_err(power_got, shaft_expected)
            note = (f"Q/N gives {disp_got:.4f} cc/rev vs "
                    f"{displacement_expected:.4f} ({d_err:.3f}%); QΔP/η gives "
                    f"{power_got:.4f} kW vs {shaft_expected:.4f} ({p_err:.3f}%)")
            _record("hydraulic_size_pump",
                    "VALIDATED" if max(d_err, p_err) < 0.1 else "BROKEN", note)
    except cb.CoreError as exc:
        if "unknown method" in str(exc):
            _skip("hydraulic_size_pump",
                  "loaded libchudware does not expose hydraulic_size_pump")
        else:
            _record("hydraulic_size_pump", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("hydraulic_size_pump", "BROKEN", f"runtime error: {exc}")


# ===========================================================================
# FAMILY 2: Piping network solver
# Current C++ Darcy-Weisbach network RPC; no legacy ``fluids`` dependency.
# ===========================================================================

def audit_piping() -> None:
    print("\n--- FAMILY: piping (core piping_* RPCs) ---")
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("piping_solve", reason)
        _skip("piping_fluid", reason)
        _skip("piping_solve [input guard]", reason)
        return

    doc = {
        "schema": "aicad.piping/1", "id": "sim-audit-pipe",
        "name": "AuditNet", "units": {"system": "MMGS"}, "material": "A106-B",
        "runs": [{
            "id": "r1", "name": "main", "suppressed": False,
            "path": [[0, 0, 0], [100000, 0, 0]], "nps": 2.0,
            "schedule": "40", "material": None, "bend_radius": None,
            "end_type": "BW", "bore_mm": None,
        }],
        "fittings": [],
    }
    boundary = [
        {"run": "r1", "end": "start", "head": 10.0},
        {"run": "r1", "end": "end", "head": 0.0},
    ]
    try:
        result = cb.call("piping_solve", {"doc": doc, "boundary": boundary})
        links = result.get("links") or []
        if not result.get("ok"):
            _record("piping_solve", "BROKEN",
                    f"valid single-pipe network failed: {result.get('reason') or result}")
        elif not result.get("converged"):
            _record("piping_solve", "BROKEN",
                    f"network did not converge: iterations={result.get('iterations')}, "
                    f"imbalance={result.get('max_imbalance_m3h')} m³/h")
        elif len(links) != 1:
            _record("piping_solve", "BROKEN",
                    f"expected one solved link, received {len(links)}")
        else:
            link = links[0]
            flow = float(link.get("flow_m3h", 0.0))
            headloss = float(link.get("headloss_m", float("nan")))
            energy_err = _pct_err(headloss, 10.0)
            note = (f"100 m NPS2 Sch40: Q={flow:.4f} m³/h, Re="
                    f"{link.get('reynolds')}, f={link.get('friction_factor')}; "
                    f"headloss={headloss:.4f} m vs imposed 10 m "
                    f"(energy-balance err={energy_err:.3f}%)")
            _record("piping_solve",
                    "VALIDATED" if flow > 0.0 and energy_err < 0.1 else "BROKEN",
                    note)
    except cb.CoreError as exc:
        if "unknown method" in str(exc):
            _skip("piping_solve", "loaded libchudware does not expose piping_solve")
        else:
            _record("piping_solve", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("piping_solve", "BROKEN", f"runtime error: {exc}")

    try:
        fluid = cb.call("piping_fluid", {"name": "water", "T": 293.15, "P": 101325.0})
        rho, mu = float(fluid.get("rho", 0.0)), float(fluid.get("mu", 0.0))
        note = f"water at 293.15 K: rho={rho:.3f} kg/m³, mu={mu:.6g} Pa·s"
        _record("piping_fluid", "VALIDATED"
                if fluid.get("ok") and abs(rho - 998.2) < 0.5
                and abs(mu - 1.002e-3) < 1e-5 else "BROKEN", note)
    except cb.CoreError as exc:
        if "unknown method" in str(exc):
            _skip("piping_fluid", "loaded libchudware does not expose piping_fluid")
        else:
            _record("piping_fluid", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("piping_fluid", "BROKEN", f"runtime error: {exc}")

    try:
        bad = cb.call("piping_solve", {"doc": doc})
        reason_text = str(bad.get("reason") or bad.get("error") or "")
        guarded = not bad.get("ok") and "boundary" in reason_text.lower()
        _record("piping_solve [input guard]",
                "VALIDATED" if guarded else "BROKEN",
                f"missing boundary conditions {'refused' if guarded else 'not refused'}: "
                f"{reason_text or bad}")
    except cb.CoreError as exc:
        if "unknown method" in str(exc):
            _skip("piping_solve [input guard]",
                  "loaded libchudware does not expose piping_solve")
        else:
            _record("piping_solve [input guard]", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("piping_solve [input guard]", "BROKEN", f"runtime error: {exc}")


# ===========================================================================
# FAMILY 3: ECAD impedance (services.ecad.impedance)
# Pure Python + scipy. No .so or ngspice needed.
# ===========================================================================

def audit_ecad_impedance() -> None:
    print("\n--- FAMILY: ECAD impedance (services.ecad.impedance) ---")
    try:
        from services.ecad import impedance as ecad_imp

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

    except ModuleNotFoundError as exc:
        _skip("ecad_check_impedance",
              f"scipy dependency unavailable: {exc}",
              install="pip install scipy")
    except Exception as exc:
        _record("ecad_check_impedance", "BROKEN",
                f"runtime error: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 4: ECAD circuit simulation (native C++ MNA + ngspice capability)
# ===========================================================================

def audit_ecad_sim() -> None:
    print("\n--- FAMILY: ECAD circuit simulation (core sim_spice RPC) ---")
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("sim_spice [ac]", reason)
        _skip("sim_spice [op]", reason)
        _skip("sim_spice [tf]", reason)
    else:
        try:
            resistance, capacitance = 1000.0, 159.155e-9
            fc_expected = 1.0 / (2.0 * math.pi * resistance * capacitance)
            h_expected = 1.0 / math.sqrt(2.0)
            circuit = {
                "nodes": ["0", "IN", "OUT"],
                "devices": [
                    {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                     "value": 0.0, "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
                    {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
                     "value": resistance, "params": {}},
                    {"kind": "C", "name": "C1", "nodes": ["OUT", "0"],
                     "value": capacitance, "params": {}},
                ],
                "models": {}, "params": {"TEMP": 27},
            }
            study = {"analyses": [{"type": "ac", "sweep": "lin", "points": 2,
                                    "fstart": fc_expected, "fstop": 2.0 * fc_expected}],
                     "probes": ["V(OUT)"]}
            result = cb.call("sim_spice", {"circuit": circuit, "study": study})
            if not result.get("convergence", {}).get("ac"):
                _record("sim_spice [ac]", "BROKEN",
                        f"AC solve did not converge: {result.get('log')}")
            else:
                got = float(result["ac"]["mag"]["V(OUT)"][0])
                err = _pct_err(got, h_expected)
                _record("sim_spice [ac]", "VALIDATED" if err < 0.1 else "BROKEN",
                        f"RC corner |H|={got:.7f} vs 1/sqrt(2)={h_expected:.7f} "
                        f"(err={err:.4f}%)")
        except cb.CoreError as exc:
            if "unknown method" in str(exc):
                _skip("sim_spice [ac]", "loaded libchudware does not expose sim_spice")
            else:
                _record("sim_spice [ac]", "BROKEN", f"CoreError: {exc}")
        except Exception as exc:  # noqa: BLE001
            _record("sim_spice [ac]", "BROKEN", f"runtime error: {exc}")

        divider = {
            "nodes": ["0", "IN", "OUT"],
            "devices": [
                {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
                 "value": 10.0, "params": {}},
                {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
                 "value": 1000.0, "params": {}},
                {"kind": "R", "name": "R2", "nodes": ["OUT", "0"],
                 "value": 2000.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        try:
            result = cb.call("sim_spice", {
                "circuit": divider,
                "study": {"analyses": [
                    {"type": "op"},
                    {"type": "tf", "output": "V(OUT)", "source": "V1"},
                ], "probes": ["V(OUT)"]},
            })
            op_ok = result.get("convergence", {}).get("op")
            if not op_ok:
                _record("sim_spice [op]", "BROKEN",
                        f"DC operating point did not converge: {result.get('log')}")
            else:
                got = float(result["op"]["V(OUT)"])
                expected = 10.0 * 2000.0 / 3000.0
                err = _pct_err(got, expected)
                _record("sim_spice [op]", "VALIDATED" if err < 0.01 else "BROKEN",
                        f"divider Vout={got:.7f} V vs {expected:.7f} V "
                        f"(err={err:.5f}%)")
            tf_ok = result.get("convergence", {}).get("tf")
            gain = (result.get("tf") or {}).get("gain")
            gain_err = _pct_err(float(gain), 2.0 / 3.0) if gain is not None else math.inf
            _record("sim_spice [tf]",
                    "VALIDATED" if tf_ok and gain_err < 0.01 else "BROKEN",
                    f"small-signal gain={gain} vs 2/3 (err={gain_err:.5f}%), "
                    f"converged={bool(tf_ok)}")
        except cb.CoreError as exc:
            if "unknown method" in str(exc):
                _skip("sim_spice [op]", "loaded libchudware does not expose sim_spice")
                _skip("sim_spice [tf]", "loaded libchudware does not expose sim_spice")
            else:
                _record("sim_spice [op]", "BROKEN", f"CoreError: {exc}")
                _record("sim_spice [tf]", "BROKEN", f"CoreError: {exc}")
        except Exception as exc:  # noqa: BLE001
            _record("sim_spice [op]", "BROKEN", f"runtime error: {exc}")
            _record("sim_spice [tf]", "BROKEN", f"runtime error: {exc}")

    ngspice = shutil.which("ngspice")
    if ngspice:
        _record("ngspice [capability]", "REAL",
                f"subprocess backend found at {ngspice}; native MNA probes above are "
                "independent of it")
    else:
        _skip("ngspice [capability]",
              "ngspice not found on PATH; external deck execution unavailable",
              install="sudo dnf install ngspice (GPL-2.0 subprocess isolation)")


# ===========================================================================
# FAMILY 5: FEA static + modal (C++ core via core_bridge)
# The full core/build-occt-linux library carries the Netgen-backed solve RPCs;
# a smaller library may legitimately SKIP them but must never receive PASS.
# ===========================================================================

def _load_core_bridge():
    """Import the package-qualified core bridge, or return ``(None, reason)``."""
    lib_path = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
    if not lib_path or not os.path.exists(lib_path):
        return None, ("CHUDWARE_LIBCHUDWARE not set or .so not found — export "
                      "CHUDWARE_LIBCHUDWARE=core/build-occt-linux/bin/libchudware.so "
                      "(use build-linux when no Netgen FEA solve is needed)")
    # A standalone audit should exercise the explicitly selected library, not an
    # unrelated already-running daemon. An explicit caller override still wins.
    os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        return cb, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"services.mcp.core_bridge import failed: {exc}"


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
    CoreError = cb.CoreError

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
                _skip("run_fea_static",
                      "loaded libchudware lacks sim_run_static; use the "
                      "CHUDWARE_WITH_NETGEN build at "
                      "core/build-occt-linux/bin/libchudware.so")
                _skip("run_fea_modal",
                      "loaded libchudware lacks sim_run_modal; use the "
                      "CHUDWARE_WITH_NETGEN build at "
                      "core/build-occt-linux/bin/libchudware.so")
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

        requested_modes = 4
        modal_study = {"step_path": ctx["step"], "material": _AUDIT_STEEL,
                       "fixtures": [ctx["fix_fp"]], "mesh_order": 2, "fineness": 0.5}
        try:
            mres = cb.call(
                "sim_run_modal",
                {"study": modal_study, "n_modes": requested_modes},
            )
        except CoreError as exc:
            if "unknown method" in str(exc):
                _skip("run_fea_modal",
                      "loaded libchudware lacks sim_run_modal; use the "
                      "CHUDWARE_WITH_NETGEN build at "
                      "core/build-occt-linux/bin/libchudware.so")
            else:
                _record("run_fea_modal", "BROKEN", f"CoreError: {exc}")
        except Exception as exc:  # noqa: BLE001
            _record("run_fea_modal", "BROKEN",
                    f"unexpected live modal error: {exc}")
        else:
            if not mres.get("ok"):
                _record("run_fea_modal", "BROKEN",
                        f"live modal failed: {mres.get('error')}")
            else:
                def finite_number(value) -> bool:
                    return (isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(float(value)))

                issues = []
                n_modes = mres.get("n_modes")
                n_nodes = mres.get("n_nodes")
                n_dof = mres.get("n_dof")
                counts_ok = (
                    isinstance(n_modes, int) and not isinstance(n_modes, bool)
                    and n_modes == requested_modes
                    and isinstance(n_nodes, int) and not isinstance(n_nodes, bool)
                    and n_nodes > 0
                )
                if not counts_ok:
                    issues.append(
                        f"invalid mode/node counts n_modes={n_modes}, n_nodes={n_nodes}"
                    )
                if (not isinstance(n_dof, int) or isinstance(n_dof, bool)
                        or not isinstance(n_nodes, int) or isinstance(n_nodes, bool)
                        or n_dof != n_nodes * 3):
                    issues.append(f"n_dof={n_dof} is inconsistent with n_nodes={n_nodes}")

                freqs = mres.get("freqs_hz")
                freqs_ok = (
                    counts_ok and isinstance(freqs, list) and len(freqs) >= n_modes
                    and all(finite_number(value) for value in freqs[:n_modes])
                )
                if not freqs_ok:
                    issues.append("freqs_hz is incomplete or non-finite")
                f1 = float(freqs[0]) if freqs_ok else None
                err = _pct_err(f1, f1_expected_hz) if f1 is not None else math.inf

                mode_shapes = mres.get("mode_shapes")
                shape_required = n_modes * n_nodes * 3 if counts_ok else 0
                shapes_ok = (
                    counts_ok and isinstance(mode_shapes, list)
                    and len(mode_shapes) >= shape_required
                    and all(finite_number(value) for value in mode_shapes)
                )
                if not shapes_ok:
                    actual = len(mode_shapes) if isinstance(mode_shapes, list) else None
                    issues.append(
                        f"mode_shapes must be a finite flat array with at least "
                        f"{shape_required} values (got {actual})"
                    )

                mesh_type = mres.get("mesh_type_used")
                if not isinstance(mesh_type, str) or not mesh_type.startswith("tet10"):
                    issues.append(f"mesh_type_used={mesh_type!r}, expected tet10")
                overlay = mres.get("overlay_aicm")
                overlay_ok = isinstance(overlay, str) and bool(overlay)
                if not overlay_ok:
                    issues.append("STEP modal solve returned no overlay_aicm")

                note = (
                    f"LIVE modal solve: f1={f1} Hz "
                    f"(beam theory {f1_expected_hz:.2f} Hz, err={err:.2f}%); "
                    f"modes={n_modes}, nodes={n_nodes}, "
                    f"shape_values="
                    f"{len(mode_shapes) if isinstance(mode_shapes, list) else None}/"
                    f"{shape_required}, mesh={mesh_type!r}, "
                    f"overlay_bytes={len(overlay) if overlay_ok else 0}"
                )
                if err < 10.0 and not issues:
                    _record("run_fea_modal [live]", "VALIDATED", note)
                else:
                    _record("run_fea_modal [live]", "BROKEN",
                            f"incomplete or inaccurate modal response: {note}; "
                            f"issues={'; '.join(issues) or 'frequency mismatch'}")

    except CoreError as exc:
        _record("run_fea_static", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("run_fea_static", "BROKEN", f"Unexpected FEA audit error: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 6: FEA fatigue — pure S-N anchors plus optional live core cross-check
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
    CoreError = cb.CoreError
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
                  "this .so lacks sim_run_fatigue (needs CHUDWARE_WITH_NETGEN build)")
        else:
            _record("run_fea_fatigue [live]", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("run_fea_fatigue [live]", "BROKEN", f"unexpected: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 7: Steady FEA thermal — plane-wall anchor plus optional live core solve
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
    CoreError = cb.CoreError
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
                  "this .so lacks sim_run_thermal (needs CHUDWARE_WITH_NETGEN build)")
        else:
            _record("sim_run_thermal [live]", "BROKEN", f"CoreError: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("sim_run_thermal [live]", "BROKEN", f"unexpected: {exc}")
        traceback.print_exc()


# ===========================================================================
# FAMILY 8: Transient thermal (live C++ RPC plus Python parity oracle)
# ===========================================================================

def audit_thermal_transient() -> None:
    print("\n--- FAMILY: transient thermal (live sim_run_thermal RPC + Python oracle) ---")

    # --- Production RPC: insulated tetra heated only by a surface flux --------
    # With no fixed-temperature or convection boundary, conservation gives an
    # implementation-independent volume-average temperature rise:
    #
    #   delta_T = q A t / (rho cp V)
    #
    # For one linear tetra with lumped capacitance, all four nodal masses are
    # equal, so the arithmetic mean of the returned nodal field is exactly the
    # volume average.  This specifically exercises the flux-only transient path
    # that a steady thermal setup must reject.
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("sim_run_thermal [transient flux-only live]", reason)
    else:
        CoreError = cb.CoreError
        try:
            edge_mm = 10.0
            edge_m = edge_mm * 1.0e-3
            flux_w_m2 = 1000.0
            rho_kg_m3 = 1000.0
            cp_j_kgk = 1000.0
            t_init_c = 20.0
            t_end_s = 10.0
            area_m2 = math.sqrt(3.0) * edge_m * edge_m / 2.0
            volume_m3 = edge_m ** 3 / 6.0
            rise_ref_c = (flux_w_m2 * area_m2 * t_end_s
                          / (rho_kg_m3 * cp_j_kgk * volume_m3))

            live_study = {
                "points": [
                    0.0, 0.0, 0.0,
                    edge_mm, 0.0, 0.0,
                    0.0, edge_mm, 0.0,
                    0.0, 0.0, edge_mm,
                ],
                "tets": [0, 1, 2, 3],
                "nodes_per_elem": 4,
                "material": {
                    "k_w_mk": 1.0,
                    "rho_kg_m3": rho_kg_m3,
                    "cp_j_kgk": cp_j_kgk,
                },
                "fixtures": [],
                "loads": [{
                    "type": "flux",
                    "magnitude": flux_w_m2,
                    "faces": {
                        "points": [
                            [edge_mm, 0.0, 0.0],
                            [0.0, edge_mm, 0.0],
                            [0.0, 0.0, edge_mm],
                        ],
                        "tol_mm": 1.0e-6,
                    },
                }],
                "transient": {
                    "t_end_s": t_end_s,
                    "dt_s": 1.0,
                    "t_init_c": t_init_c,
                    "capacitance": "lumped",
                    "frame_times_s": [0.0, 5.0, t_end_s],
                },
            }
            response = cb.call("sim_run_thermal", {"study": live_study})
            if not response.get("ok"):
                _record("sim_run_thermal [transient flux-only live]", "BROKEN",
                        f"production RPC rejected a well-posed flux-only transient: "
                        f"{response.get('error') or response}")
            else:
                issues = []

                def finite_number(value) -> bool:
                    return (isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and math.isfinite(float(value)))

                if response.get("transient") is not True:
                    issues.append("transient flag is not true")
                for field, expected in {
                    "n_nodes": 4, "n_dof": 4, "n_elems": 1, "n_steps": 10,
                }.items():
                    value = response.get(field)
                    if not finite_number(value):
                        issues.append(f"{field} is missing/non-finite")
                    elif int(value) != expected:
                        issues.append(f"{field}={value}, expected {expected}")

                frames = response.get("frames")
                frame_values = []
                frame_times = []
                if not isinstance(frames, list) or len(frames) != 3:
                    issues.append("frames must contain the requested 0/5/10 s fields")
                else:
                    for index, frame in enumerate(frames):
                        if not isinstance(frame, dict):
                            issues.append(f"frame {index} is not an object")
                            continue
                        time_s = frame.get("t_s")
                        values = frame.get("temperature")
                        if not finite_number(time_s):
                            issues.append(f"frame {index} time is missing/non-finite")
                        else:
                            frame_times.append(float(time_s))
                        if (not isinstance(values, list) or len(values) != 4
                                or not all(finite_number(v) for v in values)):
                            issues.append(f"frame {index} temperature is incomplete/non-finite")
                        else:
                            frame_values.append([float(v) for v in values])
                    if (len(frame_times) == 3
                            and any(abs(got - expected) > 1.0e-9
                                    for got, expected in zip(
                                        frame_times, (0.0, 5.0, t_end_s)))):
                        issues.append(f"frame times are {frame_times}, expected [0, 5, 10]")

                final_field = response.get("temperature")
                final_values = []
                if (not isinstance(final_field, list) or len(final_field) != 4
                        or not all(finite_number(v) for v in final_field)):
                    issues.append("final temperature field is incomplete/non-finite")
                else:
                    final_values = [float(v) for v in final_field]
                    if (len(frame_values) == 3
                            and max(abs(a - b) for a, b in
                                    zip(final_values, frame_values[-1])) > 1.0e-9):
                        issues.append("final temperature does not match the last frame")

                envelope = response.get("envelope_max")
                if (not isinstance(envelope, list) or len(envelope) != 4
                        or not all(finite_number(v) for v in envelope)):
                    issues.append("envelope_max is incomplete/non-finite")

                summary = response.get("summary")
                if not isinstance(summary, dict):
                    issues.append("summary is missing")
                else:
                    for field in ("max_temp_c", "max_temp_time_s", "min_temp_c",
                                  "final_max_temp_c"):
                        if not finite_number(summary.get(field)):
                            issues.append(f"summary.{field} is missing/non-finite")

                flux_summary = response.get("flux_summary")
                if not isinstance(flux_summary, list) or len(flux_summary) != 1:
                    issues.append("flux_summary must describe exactly one flux load")
                else:
                    flux_row = flux_summary[0]
                    if (not isinstance(flux_row, dict)
                            or flux_row.get("type") != "flux"
                            or not finite_number(flux_row.get("q_w_m2"))
                            or abs(float(flux_row.get("q_w_m2", 0.0)) - flux_w_m2)
                            > 1.0e-9
                            or flux_row.get("n_nodes") != 3):
                        issues.append("flux_summary is incomplete or inconsistent")
                if not isinstance(response.get("solver_notes"), list):
                    issues.append("solver_notes is missing")
                if not isinstance(response.get("mesh_type_used"), str):
                    issues.append("mesh_type_used is missing")

                rise_got_c = (sum(final_values) / len(final_values) - t_init_c
                              if final_values else None)
                rise_err = (_pct_err(rise_got_c, rise_ref_c)
                            if rise_got_c is not None else math.inf)
                complete = not issues
                note = (
                    f"LIVE flux-only insulated tetra: mean rise={rise_got_c} C vs "
                    f"qAt/(rho*cp*V)={rise_ref_c:.9f} C "
                    f"(err={rise_err:.6f}%); response "
                    f"{'complete' if complete else 'invalid: ' + '; '.join(issues)}"
                )
                _record("sim_run_thermal [transient flux-only live]",
                        "VALIDATED" if complete and rise_err < 0.1 else "BROKEN",
                        note)
        except CoreError as exc:
            if "unknown method" in str(exc):
                _skip("sim_run_thermal [transient flux-only live]",
                      "loaded libchudware does not expose the production "
                      "sim_run_thermal RPC")
            else:
                _record("sim_run_thermal [transient flux-only live]", "BROKEN",
                        f"CoreError: {exc}")
        except Exception as exc:  # noqa: BLE001
            _record("sim_run_thermal [transient flux-only live]", "BROKEN",
                    f"unexpected production-RPC error: {exc}")
            traceback.print_exc()

    # --- Python parity oracle -------------------------------------------------
    # These checks remain useful implementation coverage, but only the live row
    # above may claim VALIDATED for the family.
    try:
        import numpy as np
        from services.sim.fea_thermal_transient import box_mesh, solve_transient
    except ModuleNotFoundError as exc:
        _skip("thermal_transient [Python slab quench]",
              f"dependency import failed: {exc}", install="pip install numpy")
        _skip("thermal_transient [Python input guard]",
              f"dependency import failed: {exc}", install="pip install numpy")
    except Exception as exc:  # noqa: BLE001
        _record("thermal_transient [Python slab quench]", "BROKEN",
                f"module import failed: {exc}")
        _record("thermal_transient [Python input guard]", "BROKEN",
                f"module import failed: {exc}")
    else:
        # Both slab faces are quenched to 0 C from a uniform 100 C initial state.
        # The mid-plane series solution is independent of the implementation.
        length, conductivity, rho_cp, initial = 20.0, 0.05, 0.004, 100.0
        alpha = conductivity / rho_cp
        t_star = 0.3 * length * length / alpha
        try:
            points, tets, x0, xL = box_mesh(length, 8.0, 8.0, 16, 2, 2)
        except Exception as exc:  # noqa: BLE001
            _record("thermal_transient [Python slab quench]", "BROKEN",
                    f"oracle mesh setup failed: {exc}")
            _record("thermal_transient [Python input guard]", "BROKEN",
                    f"oracle mesh setup failed: {exc}")
        else:
            fixed = x0 + xL
            try:
                result = solve_transient(
                    points, tets, conductivity, fixed, [0.0] * len(fixed), [], [],
                    rho_cp, t_end=t_star, dt=t_star / 240.0, t_init=initial)
                if not result.ok:
                    _record("thermal_transient [Python slab quench]", "BROKEN",
                            f"well-posed solve failed: {result.error}")
                elif not result.frames:
                    _record("thermal_transient [Python slab quench]", "BROKEN",
                            "solver returned no temperature frames")
                else:
                    _, temperatures = result.frames[-1]
                    mids = [i for i, point in enumerate(points)
                            if abs(float(point[0]) - length / 2.0) < 1e-9]
                    got = float(np.mean(temperatures[mids]))
                    reference = 0.0
                    for mode in range(1, 200, 2):
                        reference += (
                            4.0 * initial / (mode * math.pi)
                            * math.sin(mode * math.pi * 0.5)
                            * math.exp(-alpha * mode * mode * math.pi * math.pi
                                       * t_star / (length * length))
                        )
                    err = _pct_err(got, reference)
                    envelope_ok = (abs(float(result.max_temp) - initial) < 1e-9
                                   and abs(float(result.max_temp_time)) < 1e-12)
                    note = (
                        f"mid-plane T={got:.5f} C vs Fourier series "
                        f"{reference:.5f} C (err={err:.3f}%); "
                        f"initial-temperature envelope "
                        f"{'preserved' if envelope_ok else 'wrong'}; "
                        f"steps={result.n_steps}; Python oracle only"
                    )
                    _record("thermal_transient [Python slab quench]",
                            "REAL" if err < 3.0 and envelope_ok else "BROKEN", note)
            except Exception as exc:  # noqa: BLE001
                _record("thermal_transient [Python slab quench]", "BROKEN",
                        f"runtime error: {exc}")

            try:
                bad = solve_transient(
                    points, tets, conductivity, [], [], [], [], rho_cp,
                    t_end=1.0, dt=0.1, t_init=20.0)
                guarded = (
                    not bad.ok
                    and ("boundary" in bad.error.lower()
                         or "heat source" in bad.error.lower())
                )
                _record("thermal_transient [Python input guard]",
                        "REAL" if guarded else "BROKEN",
                        f"unforced/unconstrained Python setup "
                        f"{'refused' if guarded else 'not refused'}: {bad.error!r}")
            except Exception as exc:  # noqa: BLE001
                _record("thermal_transient [Python input guard]", "BROKEN",
                        f"guard raised instead of returning a result: {exc}")


# ===========================================================================
# FAMILY 9: Vibration / frequency response
# ===========================================================================

def audit_vibration() -> None:
    print("\n--- FAMILY: vibration / frequency response (services.sim.vibration) ---")
    try:
        from services.sim.vibration import harmonic_response, miles_response
        from services.sim.multiphysics_rpc import WORKER_METHODS
    except ModuleNotFoundError as exc:
        _skip("frequency_response [harmonic]", f"dependency import failed: {exc}",
              install="pip install numpy")
        _skip("frequency_response [Miles]", f"dependency import failed: {exc}",
              install="pip install numpy")
        _skip("frequency_response [input guard]", f"dependency import failed: {exc}",
              install="pip install numpy")
        return
    except Exception as exc:  # noqa: BLE001
        for name in ("frequency_response [harmonic]",
                     "frequency_response [Miles]",
                     "frequency_response [input guard]"):
            _record(name, "BROKEN", f"module import failed: {exc}")
        return

    # Two equal-mass nodes, unit X mode shape. The 0 Hz mode must be skipped;
    # the 20 Hz elastic mode has Gamma/m=1 exactly.
    shapes = [
        1.0, 0.0, 0.0, 1.0, 0.0, 0.0,
        1.0, 0.0, 0.0, 1.0, 0.0, 0.0,
    ]
    zeta = 0.05
    try:
        harmonic = harmonic_response(
            freqs_hz=[0.0, 20.0], mode_shapes_flat=shapes, n_nodes=2,
            total_mass_kg=2.0, excitation_dir=[1.0, 0.0, 0.0],
            sweep_hz=[10.0, 20.0, 30.0], zeta=zeta, base_accel_g=1.0)
        expected = math.sqrt(1.0 + 4.0 * zeta * zeta) / (2.0 * zeta)
        got = float(harmonic["peak_accel_g"])
        err = _pct_err(got, expected)
        rigid_skipped = len(harmonic.get("per_mode") or []) == 1
        at_resonance = abs(float(harmonic.get("peak_freq_hz")) - 20.0) < 1e-12
        note = (f"20 Hz SDOF resonance={got:.6f} g vs {expected:.6f} g "
                f"(err={err:.5f}%); rigid mode skipped={rigid_skipped}")
        _record("frequency_response [harmonic]",
                "VALIDATED" if err < 0.01 and rigid_skipped and at_resonance
                else "BROKEN", note)
    except Exception as exc:  # noqa: BLE001
        _record("frequency_response [harmonic]", "BROKEN", f"runtime error: {exc}")

    try:
        psd = 0.01
        random = miles_response(
            freqs_hz=[0.0, 20.0], mode_shapes_flat=shapes, n_nodes=2,
            total_mass_kg=2.0, excitation_dir=[1.0, 0.0, 0.0],
            psd_breakpoints=[[10.0, psd], [100.0, psd]], zeta=zeta)
        expected = math.sqrt(math.pi / 2.0 * 20.0
                             * (1.0 / (2.0 * zeta)) * psd)
        got = float(random["total_grms"])
        err = _pct_err(got, expected)
        _record("frequency_response [Miles]",
                "VALIDATED" if err < 0.01 else "BROKEN",
                f"flat-PSD GRMS={got:.7f} vs Miles {expected:.7f} "
                f"(err={err:.5f}%)")
    except Exception as exc:  # noqa: BLE001
        _record("frequency_response [Miles]", "BROKEN", f"runtime error: {exc}")

    try:
        bad = WORKER_METHODS["sim_freq_response"]({"freqs_hz": [20.0]}, lambda _: None)
        guarded = not bad.get("ok") and "mode_shapes" in str(bad.get("error", ""))
        _record("frequency_response [input guard]",
                "VALIDATED" if guarded else "BROKEN",
                f"incomplete modal input {'refused' if guarded else 'not refused'}: {bad}")
    except Exception as exc:  # noqa: BLE001
        _record("frequency_response [input guard]", "BROKEN",
                f"worker RPC guard failed: {exc}")


# ===========================================================================
# FAMILY 10: Advanced FEA — CalculiX buckling/contact + input generators
# ===========================================================================

def _cload_sum(inp_text: str) -> float:
    """Sum the third field of the first generated CalculiX ``*CLOAD`` block."""
    lines = inp_text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip().upper() == "*CLOAD"), None)
    if start is None:
        raise ValueError("generated deck has no *CLOAD block")
    total = 0.0
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("*"):
            break
        if not stripped:
            continue
        fields = [field.strip() for field in stripped.split(",")]
        if len(fields) < 3:
            raise ValueError(f"malformed *CLOAD row: {line!r}")
        total += float(fields[2])
    return total


def audit_advanced_fea() -> None:
    print("\n--- FAMILY: advanced FEA (buckling/contact, CalculiX capability) ---")
    try:
        from services.mcp import fea_advanced as fa
        from services.sim.multiphysics_rpc import WORKER_METHODS
    except Exception as exc:  # noqa: BLE001
        for name in ("fea_buckling [input]", "fea_contact [input]",
                     "fea_buckling [CalculiX]", "fea_contact [CalculiX]"):
            _record(name, "BROKEN", f"advanced FEA import failed: {exc}")
        return

    length, width, height = 1000.0, 20.0, 10.0
    elastic_modulus, poisson, applied = 200000.0, 0.3, 100.0
    try:
        buckling = fa.build_buckling_column_inp(
            length, width, height, elastic_modulus, poisson, applied,
            nx=16, ny=2, nz=2)
        inertia = min(width * height ** 3, height * width ** 3) / 12.0
        euler = math.pi ** 2 * elastic_modulus * inertia / (2.0 * length) ** 2
        pcr_err = _pct_err(float(buckling["expected_Pcr"]), euler)
        load_err = _pct_err(abs(_cload_sum(buckling["inp"])), applied)
        keywords_ok = "*BUCKLE" in buckling["inp"] and "TYPE=C3D8I" in buckling["inp"]
        note = (f"generated fixed-free column: Pcr={buckling['expected_Pcr']:.6f} N "
                f"vs Euler {euler:.6f} N (err={pcr_err:.6f}%); "
                f"summed load err={load_err:.6f}%")
        _record("fea_buckling [input]",
                "VALIDATED" if pcr_err < 1e-8 and load_err < 1e-4 and keywords_ok
                else "BROKEN", note)
    except Exception as exc:  # noqa: BLE001
        buckling = None
        _record("fea_buckling [input]", "BROKEN", f"generator failed: {exc}")

    contact_length, contact_width, pressure = 40.0, 30.0, 1.25
    try:
        contact = fa.build_contact_block_inp(
            contact_length, contact_width, 10.0, elastic_modulus, poisson,
            pressure, nx=3, ny=3, nz=2)
        expected_load = pressure * contact_length * contact_width
        load_err = _pct_err(abs(_cload_sum(contact["inp"])), expected_load)
        keywords_ok = ("*CONTACT PAIR" in contact["inp"]
                       and "*STEP, NLGEOM" in contact["inp"])
        note = (f"generated two-block contact load={abs(_cload_sum(contact['inp'])):.6f} N "
                f"vs pA={expected_load:.6f} N (err={load_err:.6f}%)")
        _record("fea_contact [input]",
                "VALIDATED" if load_err < 1e-6 and keywords_ok else "BROKEN", note)
    except Exception as exc:  # noqa: BLE001
        contact = None
        _record("fea_contact [input]", "BROKEN", f"generator failed: {exc}")

    try:
        caps = WORKER_METHODS["sim_caps"]({}, lambda _: None)
        ccx_cap = caps.get("ccx") or {}
        ccx = fa.ccx_path()
        if bool(ccx) != bool(ccx_cap.get("found")):
            _record("CalculiX [capability]", "BROKEN",
                    f"sim_caps and fea_advanced disagree: {ccx_cap}, path={ccx}")
        elif ccx:
            _record("CalculiX [capability]", "REAL",
                    f"PATH probe found {ccx}")
        else:
            _skip("CalculiX [capability]",
                  "CalculiX not found on PATH and CHUDWARE_CCX does not resolve",
                  install="install ccx_2.20 or newer and put it on PATH")
    except Exception as exc:  # noqa: BLE001
        _record("CalculiX [capability]", "BROKEN", f"capability probe failed: {exc}")
        ccx = None

    if not ccx:
        hint = ("install CalculiX (ccx_2.20 or newer), put it on PATH, or set "
                "CHUDWARE_CCX=/full/path/to/ccx")
        _skip("fea_buckling [CalculiX]",
              "CalculiX not found; generated input was audited but no eigenvalue solve ran",
              install=hint)
        _skip("fea_contact [CalculiX]",
              "CalculiX not found; generated input was audited but no nonlinear solve ran",
              install=hint)
        return

    workdirs: list[str] = []
    try:
        if buckling is None:
            _record("fea_buckling [CalculiX]", "BROKEN",
                    "input generation failed; solver was not launched")
        else:
            run = fa.run_ccx(buckling["inp"], job="sim_audit_buckle", timeout=60.0)
            if run.get("workdir"):
                workdirs.append(run["workdir"])
            if not run.get("ok") or not run.get("dat_path"):
                _record("fea_buckling [CalculiX]", "BROKEN",
                        f"ccx failed or emitted no .dat result: "
                        f"{run.get('error') or run.get('stderr') or run.get('stdout')}")
            else:
                factors = fa.parse_buckle_factors(run["dat_path"])
                got = factors[0] * applied if factors else None
                err = (_pct_err(got, float(buckling["expected_Pcr"]))
                       if got is not None else math.inf)
                _record("fea_buckling [CalculiX]",
                        "VALIDATED" if got is not None and err < 10.0 else "BROKEN",
                        f"first critical load={got} N vs Euler "
                        f"{buckling['expected_Pcr']:.6f} N (err={err:.3f}%)")
    except Exception as exc:  # noqa: BLE001
        _record("fea_buckling [CalculiX]", "BROKEN", f"solver error: {exc}")

    try:
        if contact is None:
            _record("fea_contact [CalculiX]", "BROKEN",
                    "input generation failed; solver was not launched")
        else:
            run = fa.run_ccx(contact["inp"], job="sim_audit_contact", timeout=60.0)
            if run.get("workdir"):
                workdirs.append(run["workdir"])
            if not run.get("ok") or not run.get("dat_path"):
                _record("fea_contact [CalculiX]", "BROKEN",
                        f"ccx failed or emitted no .dat result: "
                        f"{run.get('error') or run.get('stderr') or run.get('stdout')}")
            else:
                average = fa.parse_dat_nodal(
                    run["dat_path"], "RF", contact["base_nodes"], component=3)
                reaction = (abs(float(average) * len(contact["base_nodes"]))
                            if average is not None else None)
                expected = float(contact["applied_load_n"])
                err = _pct_err(reaction, expected) if reaction is not None else math.inf
                _record("fea_contact [CalculiX]",
                        "VALIDATED" if reaction is not None and err < 5.0 else "BROKEN",
                        f"base reaction={reaction} N vs applied {expected:.6f} N "
                        f"(equilibrium err={err:.3f}%)")
    except Exception as exc:  # noqa: BLE001
        _record("fea_contact [CalculiX]", "BROKEN", f"solver error: {exc}")
    finally:
        for workdir in workdirs:
            shutil.rmtree(workdir, ignore_errors=True)


# ===========================================================================
# FAMILY 11: Assembly motion / rigid-body inverse dynamics
# ===========================================================================

def audit_motion() -> None:
    print("\n--- FAMILY: motion / dynamics (core simulate_motion RPC) ---")
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("simulate_motion [kinematics]", reason)
        _skip("simulate_motion [dynamics]", reason)
        _skip("simulate_motion [input guard]", reason)
        return

    import tempfile
    sidecar = tempfile.mkdtemp(prefix="sim_audit_motion_")
    docs: list[str] = []
    try:
        part = cb.call("doc_new", {"kind": "part", "name": "audit_motion_bar"})["doc_id"]
        docs.append(part)
        built = cb.call("doc_apply_ops", {"doc_id": part, "ops": [{
            "op": "add_feature", "feature": {
                "type": "box", "name": "Bar",
                "params": {
                    "length": {"value": 40.0, "expr": None},
                    "width": {"value": 4.0, "expr": None},
                    "height": {"value": 4.0, "expr": None},
                },
            },
        }]})
        statuses = (built.get("rebuild") or {}).get("feature_status") or []
        if not statuses or not all(row.get("ok") for row in statuses):
            raise RuntimeError(f"motion bar rebuild failed: {statuses}")
        faces = cb.call("doc_query", {
            "doc_id": part, "query": {"of": "face"}}).get("matches") or []
        x_face = max(faces, key=lambda row: abs(row.get("centroid", [0, 0, 0])[0]))
        z_face = max(faces, key=lambda row: abs(row.get("centroid", [0, 0, 0])[2]))
        path = os.path.join(sidecar, "bar.step+")
        saved = cb.call("doc_save", {"doc_id": part, "path": path})
        if not saved.get("ok", True):
            raise RuntimeError(f"component save failed: {saved}")

        assembly = cb.call(
            "doc_new", {"kind": "assembly", "name": "audit_swing_arm"})["doc_id"]
        docs.append(assembly)

        def revision() -> int:
            return int(cb.call("doc_tree", {"doc_id": assembly}).get("revision", 0))

        ground_xf = [1, 0, 0, 0, 0, 1, 0, 200,
                     0, 0, 1, 0, 0, 0, 0, 1]
        cb.call("doc_apply_ops", {
            "doc_id": assembly, "base_revision": revision(), "with_tess": False,
            "ops": [
                {"op": "asm_insert_component", "source": {"path": path},
                 "name": "Ground", "fixed": True, "transform": ground_xf},
                {"op": "asm_insert_component", "source": {"path": path},
                 "name": "Arm", "fixed": False},
            ],
        })
        mates = cb.call("doc_apply_ops", {
            "doc_id": assembly, "base_revision": revision(), "with_tess": False,
            "ops": [
                {"op": "asm_add_mate", "mate": {
                    "type": "angle", "align": "aligned", "suppressed": False,
                    "refs": [{"component": "k1", "selector": x_face},
                             {"component": "k2", "selector": x_face}],
                    "value": {"value": 0.0, "expr": None},
                }},
                {"op": "asm_add_mate", "mate": {
                    "type": "parallel", "align": "aligned", "suppressed": False,
                    "refs": [{"component": "k1", "selector": z_face},
                             {"component": "k2", "selector": z_face}],
                }},
            ],
        })
        mate_id = (mates.get("id_map") or {}).get("$0")
        if not mate_id:
            raise RuntimeError(f"driven mate id missing: {mates}")

        result = cb.call("simulate_motion", {
            "doc_id": assembly, "mate_id": mate_id, "start": 0.0, "stop": 90.0,
            "steps": 8, "check_interference": False,
            "dynamics": {"duration_s": 0.8, "default_density_g_cm3": 2.7,
                         "gravity": [0.0, 0.0, 0.0],
                         "damping": {"angular_nms_per_rad": 0.1}},
        })
        trajectory = result.get("trajectory") or []
        transforms = []
        for frame in trajectory:
            arm = next((body for body in frame.get("bodies", [])
                        if body.get("component") == "k2"), None)
            if arm:
                transforms.append(arm.get("transform") or [])
        moved = (len(transforms) == 8 and
                 max(abs(float(a) - float(b))
                     for a, b in zip(transforms[0], transforms[-1])) > 0.5)
        converged = len(trajectory) == 8 and all(
            bool(frame.get("converged")) for frame in trajectory)
        _record("simulate_motion [kinematics]",
                "VALIDATED" if result.get("ok") and moved and converged else "BROKEN",
                f"8-frame 0→90 deg sweep: moved={moved}, converged={converged}, "
                f"driver={result.get('driver')}")

        dynamics = result.get("dynamics") or {}
        arm_body = next((body for body in dynamics.get("bodies", [])
                         if body.get("component") == "k2"), None)
        mass = float(arm_body["mass_kg"]) if arm_body else None
        expected_mass = 40.0 * 4.0 * 4.0 * 2.7e-6
        frames = dynamics.get("frames") or []
        duration_ok = (len(frames) == 8
                       and abs(float(frames[-1].get("t_s", -1.0)) - 0.8) < 1e-9)
        reactions_ok = bool(dynamics.get("per_joint_worst")) and bool(
            dynamics.get("worst_case"))
        mass_err = _pct_err(mass, expected_mass) if mass is not None else math.inf
        _record("simulate_motion [dynamics]",
                "VALIDATED" if mass_err < 0.1 and duration_ok and reactions_ok
                else "BROKEN",
                f"geometry×density mass={mass} kg vs {expected_mass:.9f} kg "
                f"(err={mass_err:.4f}%); duration_ok={duration_ok}, "
                f"joint_reactions={reactions_ok}")

        try:
            normalized_result = cb.call("simulate_motion", {
                "doc_id": assembly, "mate_id": mate_id, "start": 0.0, "stop": 1.0,
                "steps": 1, "check_interference": False,
            })
            normalized_driver = normalized_result.get("driver") or {}
            normalized_frames = normalized_result.get("trajectory") or []
            normalized = (normalized_result.get("ok")
                          and int(normalized_driver.get("steps", 0)) == 2
                          and len(normalized_frames) == 2)
            _record("simulate_motion [input normalization]",
                    "VALIDATED" if normalized else "BROKEN",
                    f"public RPC steps=1 normalized to "
                    f"{normalized_driver.get('steps')} steps and "
                    f"{len(normalized_frames)} frames")
        except cb.CoreError as exc:
            _record("simulate_motion [input normalization]", "BROKEN",
                    f"public RPC rejected steps=1 instead of normalizing to 2: {exc}")
    except cb.CoreError as exc:
        if "unknown method" in str(exc):
            _skip("simulate_motion [kinematics]",
                  "loaded libchudware does not expose simulate_motion")
            _skip("simulate_motion [dynamics]",
                  "loaded libchudware does not expose simulate_motion dynamics")
            _skip("simulate_motion [input guard]",
                  "loaded libchudware does not expose simulate_motion")
        else:
            _record("simulate_motion [kinematics]", "BROKEN", f"CoreError: {exc}")
            _record("simulate_motion [dynamics]", "BROKEN",
                    "motion fixture or solve failed before dynamics completed")
            _record("simulate_motion [input guard]", "BROKEN",
                    "motion fixture failed before input guard could execute")
    except Exception as exc:  # noqa: BLE001
        _record("simulate_motion [kinematics]", "BROKEN", f"runtime error: {exc}")
        _record("simulate_motion [dynamics]", "BROKEN",
                "motion fixture or solve failed before dynamics completed")
        _record("simulate_motion [input guard]", "BROKEN",
                "motion fixture failed before input guard could execute")
    finally:
        for doc_id in reversed(docs):
            try:
                cb.call("doc_close", {"doc_id": doc_id})
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(sidecar, ignore_errors=True)


# ===========================================================================
# FAMILY 12: Electromagnetics — analytic + FDFD, optional openEMS
# ===========================================================================

def audit_em() -> None:
    print("\n--- FAMILY: electromagnetics (analytic, FDFD, openEMS capability) ---")
    try:
        from services.sim.em.adapters import (
            AnalyticAdapter, BuiltinFdfdAdapter, EmSolverError)
        from services.sim.em.analytic import C0
        from services.sim.em.openems_adapter import OpenEmsAdapter
    except ModuleNotFoundError as exc:
        for name in ("em_analytic [WR90]", "em_fdfd [WR90]",
                     "em_adapter [input guard]", "em_openems [full-wave]"):
            _skip(name, f"EM dependency import failed: {exc}",
                  install="pip install numpy scipy")
        return
    except Exception as exc:  # noqa: BLE001
        for name in ("em_analytic [WR90]", "em_fdfd [WR90]",
                     "em_adapter [input guard]", "em_openems [full-wave]"):
            _record(name, "BROKEN", f"EM module import failed: {exc}")
        return

    spec = {"standard": "WR90", "n_modes": 3}
    expected_fc = C0 / (2.0 * 22.86e-3)
    try:
        result = AnalyticAdapter().eigenmode(spec)
        got = float(result["modes"][0]["fc_hz"])
        err = _pct_err(got, expected_fc)
        _record("em_analytic [WR90]", "VALIDATED" if err < 1e-9 else "BROKEN",
                f"TE10 cutoff={got / 1e9:.9f} GHz vs c/(2a)="
                f"{expected_fc / 1e9:.9f} GHz (err={err:.9f}%)")
    except Exception as exc:  # noqa: BLE001
        _record("em_analytic [WR90]", "BROKEN", f"analytic solve failed: {exc}")

    try:
        result = BuiltinFdfdAdapter().eigenmode({
            **spec, "resolution": 40, "fields": False})
        modes = result.get("modes") or []
        got = float(modes[0]["fc_hz"]) if modes else None
        err = _pct_err(got, expected_fc) if got is not None else math.inf
        _record("em_fdfd [WR90]",
                "VALIDATED" if got is not None and err < 3.0 else "BROKEN",
                f"40-cell scalar Helmholtz cutoff={got / 1e9 if got else None} GHz "
                f"vs analytic {expected_fc / 1e9:.9f} GHz (err={err:.3f}%)")
    except ModuleNotFoundError as exc:
        _skip("em_fdfd [WR90]", f"FDFD dependency unavailable: {exc}",
              install="pip install numpy scipy")
    except Exception as exc:  # noqa: BLE001
        _record("em_fdfd [WR90]", "BROKEN", f"FDFD solve failed: {exc}")

    try:
        AnalyticAdapter().eigenmode({
            "waveguide": {"shape": "rectangular", "a_mm": 10.0, "b_mm": 20.0}})
        _record("em_adapter [input guard]", "BROKEN",
                "rectangular guide with b>a was accepted")
    except EmSolverError as exc:
        guarded = "broad-wall" in str(exc)
        _record("em_adapter [input guard]",
                "VALIDATED" if guarded else "BROKEN",
                f"invalid geometry refusal: {exc}")
    except Exception as exc:  # noqa: BLE001
        _record("em_adapter [input guard]", "BROKEN",
                f"invalid geometry raised wrong error: {exc}")

    openems = OpenEmsAdapter()
    try:
        plan = openems.build_plan({
            "standard": "WR90", "length_mm": 50.0,
            "f_start_ghz": 8.0, "f_stop_ghz": 12.0})
        plan_ok = (plan.get("ports") is not None and plan.get("mesh") is not None)
        _record("em_openems [input plan]",
                "REAL" if plan_ok else "BROKEN",
                "rectangular-guide mesh/port plan generated without launching the "
                f"heavy solver; plan keys={sorted(plan)}")
    except Exception as exc:  # noqa: BLE001
        _record("em_openems [input plan]", "BROKEN", f"plan generation failed: {exc}")

    availability = openems.available()
    if not availability.get("installed"):
        _skip("em_openems [full-wave]",
              f"openEMS/CSXCAD unavailable: {availability.get('detail')}",
              install=openems.install_hint)
    else:
        _skip("em_openems [full-wave]",
              "openEMS is available, but a full 3-D FDTD solve is intentionally outside "
              "the deterministic cheap normal audit; run the EM worker validation suite "
              "for full-wave S-parameters")


# ===========================================================================
# FAMILY 13: CFD (low-level D3Q19 LBM + optional OpenFOAM)
# ===========================================================================

def audit_cfd() -> None:
    print("\n--- FAMILY: CFD (core D3Q19 LBM + OpenFOAM capability) ---")
    cb, reason = _load_core_bridge()
    if cb is None:
        _skip("sim_lbm [deterministic kernel]", reason)
    else:
        nx, ny, nz = 20, 10, 10
        solid = [
            1 if 8 <= x <= 10 and 3 <= y <= 6 and 3 <= z <= 6 else 0
            for x in range(nx) for y in range(ny) for z in range(nz)
        ]
        payload = {
            "mask": {"nx": nx, "ny": ny, "nz": nz, "solid": solid},
            "params": {"u_lat": 0.04, "tau0": 0.8, "voxel_mm": 1.0,
                       "dx_m": 1.0e-3, "dt_s": 1.0e-4,
                       "rho_air": 1.225, "backend": "cpu"},
            "n_steps": 120,
        }
        try:
            first = cb.call("sim_lbm", payload)
            second = cb.call("sim_lbm", payload)
            scalars = [
                first.get("drag_n"), first.get("lift_n"), first.get("cd"),
                first.get("frontal_area_mm2"),
            ]
            fields = ((first.get("rho") or []) + (first.get("ux") or [])
                      + (first.get("uy") or []) + (first.get("uz") or []))
            finite = (all(value is not None and math.isfinite(float(value))
                          for value in scalars)
                      and all(math.isfinite(float(value)) for value in fields))
            deterministic = (first.get("drag_n") == second.get("drag_n")
                             and first.get("cd") == second.get("cd"))
            physical = (float(first.get("drag_n") or 0.0) > 0.0
                        and float(first.get("frontal_area_mm2") or 0.0) > 0.0)
            ok = (first.get("ok") and second.get("ok")
                  and not first.get("diverged") and finite
                  and deterministic and physical)
            note = (f"20x10x10 lattice, 120 steps twice: drag="
                    f"{first.get('drag_n')} N, Cd={first.get('cd')}, "
                    f"backend={first.get('backend')}; finite={finite}, "
                    f"bit-deterministic={deterministic}. This validates kernel "
                    "stability/determinism, not absolute Cd or convergence.")
            _record("sim_lbm [deterministic kernel]",
                    "VALIDATED" if ok else "BROKEN", note)
        except cb.CoreError as exc:
            if "unknown method" in str(exc):
                _skip("sim_lbm [deterministic kernel]",
                      "loaded libchudware does not expose sim_lbm")
            else:
                _record("sim_lbm [deterministic kernel]", "BROKEN",
                        f"CoreError: {exc}")
        except Exception as exc:  # noqa: BLE001
            _record("sim_lbm [deterministic kernel]", "BROKEN",
                    f"runtime error: {exc}")

    try:
        from services.sim.foam_runner import capabilities
        caps = capabilities()
    except Exception as exc:  # noqa: BLE001
        _record("OpenFOAM [capability]", "BROKEN",
                f"services.sim.foam_runner import/probe failed: {exc}")
        return

    if not caps.get("openfoam"):
        _skip("OpenFOAM [RANS]",
              "no working OpenFOAM runner detected by services.sim.foam_runner",
              install="python -m tools.provision_openfoam --install (Linux user-space "
                      "environment; Windows native blueCFD or WSL; subprocess only)")
        return

    _record("OpenFOAM [capability]", "REAL",
            f"runner kind={caps.get('openfoam_kind')}, "
            f"version={caps.get('openfoam_version') or 'unknown'}")
    if not os.environ.get("CHUDWARE_SIM_AUDIT_LIVE_FOAM"):
        _skip("OpenFOAM [live sphere]",
              "backend is available, but the 10–30 s RANS validation is opt-in; "
              "set CHUDWARE_SIM_AUDIT_LIVE_FOAM=1")
        return
    try:
        from tools.foam_validation import run_case
        result = run_case("sphere", "coarse")
        passed = bool(result.get("pass"))
        _record("OpenFOAM [live sphere]",
                "VALIDATED" if passed else "BROKEN",
                f"Cd={result.get('cd')} vs reference {result.get('cd_ref')}, "
                f"band={result.get('band')}, Re={result.get('re')}, "
                f"elapsed={result.get('secs')} s")
    except Exception as exc:  # noqa: BLE001
        _record("OpenFOAM [live sphere]", "BROKEN",
                f"live validation failed: {exc}")


# ===========================================================================
# FAMILY 14: Structural frame analysis + D/C checks
# ===========================================================================

def audit_structural() -> None:
    print("\n--- FAMILY: structural (PyNite + services.structural.checks) ---")
    try:
        from Pynite import FEModel3D
    except ModuleNotFoundError as exc:
        _skip("struct_analyze", f"PyNiteFEA unavailable: {exc}",
              install="pip install PyNiteFEA (MIT, in-process)")
    except Exception as exc:  # noqa: BLE001
        _record("struct_analyze", "BROKEN", f"PyNite import failed: {exc}")
    else:
        try:
            model = FEModel3D()
            model.add_material("steel", E=200000.0, G=77000.0, nu=0.3, rho=7.85e-3)
            width, height, length, load = 50.0, 200.0, 3000.0, 5000.0
            area = width * height
            iy = width * height ** 3 / 12.0
            iz = height * width ** 3 / 12.0
            torsion = 0.141 * width * height ** 3 / (width / height)
            model.add_node("A", 0, 0, 0)
            model.add_node("B", length, 0, 0)
            model.add_section("rect50x200", area, iy, iz, torsion)
            model.add_member("m1", "A", "B", "steel", "rect50x200")
            model.def_support("A", True, True, True, True, True, True)
            model.add_node_load("B", "FZ", -load)
            model.analyze(check_statics=False)
            got = abs(model.nodes["B"].DZ["Combo 1"])
            expected = load * length ** 3 / (3.0 * 200000.0 * iy)
            err = _pct_err(got, expected)
            _record("struct_analyze", "VALIDATED" if err < 1.0 else "BROKEN",
                    f"cantilever deflection={got:.6f} mm vs Euler-Bernoulli "
                    f"{expected:.6f} mm (err={err:.4f}%)")
        except Exception as exc:  # noqa: BLE001
            _record("struct_analyze", "BROKEN",
                    f"PyNite analysis failed: {exc}")

    try:
        from services.structural.checks import dc_ratio_axial
        force, area, fy, phi = 100000.0, 1000.0, 250.0, 0.9
        expected = force / (phi * area * fy)
        got = dc_ratio_axial(force, area, fy, phi)
        err = _pct_err(got, expected)
        _record("struct_check [axial LRFD]",
                "VALIDATED" if err < 1e-9 else "BROKEN",
                f"D/C={got:.9f} vs P/(phi*A*Fy)={expected:.9f} "
                f"(err={err:.9f}%)")
    except Exception as exc:  # noqa: BLE001
        _record("struct_check [axial LRFD]", "BROKEN",
                f"services.structural.checks failed: {exc}")


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> int:
    print("=" * 70)
    print("sim_audit.py — Current Simulation Surface Audit")
    print(f"Python: {sys.executable}")
    print(f"CHUDWARE_LIBCHUDWARE: "
          f"{os.environ.get('CHUDWARE_LIBCHUDWARE', '(not set)')}")
    print("=" * 70)

    families = (
        audit_hydraulic,
        audit_piping,
        audit_ecad_impedance,
        audit_ecad_sim,
        audit_fea,
        audit_fatigue,
        audit_thermal,
        audit_thermal_transient,
        audit_vibration,
        audit_advanced_fea,
        audit_motion,
        audit_em,
        audit_cfd,
        audit_structural,
    )
    t0 = time.perf_counter()
    for audit in families:
        try:
            audit()
        except Exception as exc:  # noqa: BLE001 — keep later families reachable
            _record(audit.__name__, "BROKEN",
                    f"unhandled family-level error: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    elapsed = time.perf_counter() - t0

    print()
    print("=" * 70)
    print(f"SUMMARY  families={len(families)}  checks={len(_RESULTS)}  "
          f"executed={_EXECUTED}  skipped={_SKIPPED}  broken={_BROKEN}")
    print(f"Elapsed: {elapsed:.2f}s")
    print("=" * 70)
    print()
    print(f"{'Tool':<45} {'Verdict':<12} {'Note'}")
    print("-" * 120)
    for result in _RESULTS:
        print(f"  {result['tool']:<43} {result['verdict']:<12} "
              f"{result['note'][:70]}")
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
