"""Multi-physics worker endpoints (T-FIN24) — the GUI-facing mirror of the advanced
simulation + cross-sim-handoff MCP tools.

Registered into the JSON-RPC worker (``engine_oracle/worker.py``) so the desktop
Simulate workspace drives the SAME implementations as the MCP tools (the fw_* /
firmware worker_rpc pattern): thin ``(params, emit)`` adapters over framework-free
module functions.

Endpoints:
  sim_caps            capability doctor for the Simulate tab's honest gating: CalculiX
                      presence (buckling/contact), vibration post-processing (numpy),
                      and which cross-sim handoff endpoints are actually servable —
                      including a forward probe for the T-FIN17 heatsink picker so the
                      GUI button lights up the moment that tool lands.
  sim_run_buckling    CalculiX linear (eigenvalue) buckling — box-proxy column, the
                      exact pipeline of the run_fea_buckling MCP tool.
  sim_run_contact     CalculiX nonlinear contact two-block proxy (run_fea_contact).
  sim_freq_response   modal-superposition harmonic + Miles random post-processing over
                      a C++ core sim_run_modal result (run_fea_frequency_response's
                      math, engine_oracle/sim/vibration.py).
  ecad_thermal_handoff  board JSON -> solved thermal FEA study + aicad.simhandoff/1
                      envelope (services/mcp/ecad_thermal.thermal_handoff_impl — the
                      same impl the MCP tool wraps).
  piping_verify_cfd   1D-vs-3D pressure-drop verification envelope
                      (engine.piping.cfd_handoff.verify_pressure_drop).

Graceful posture (firmware contract): expected failures come back as result payloads
(``{ok:false, error|skipped, hint}``), never raised exceptions.
"""

from __future__ import annotations

from typing import Callable, Optional


def _wrap(fn: Callable[[dict], dict]) -> Callable[[dict, Callable], dict]:
    def handler(params: dict, emit) -> dict:  # noqa: ANN001
        del emit
        try:
            return fn(params or {})
        except Exception as e:  # noqa: BLE001 — worker contract: payloads, not raises
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return handler


# --------------------------------------------------------------------------- capability doctor

def _heatsink_endpoint() -> Optional[Callable]:
    """Forward probe for the T-FIN17 heatsink selection loop. Returns the callable
    when the tool has landed as an importable impl, else None — the GUI's 'Pick
    heatsink' button is wired behind this check and lights up when T-FIN17 merges."""
    try:
        from services.mcp import ecad_thermal as et
    except Exception:  # noqa: BLE001
        return None
    for name in ("heatsink_select_impl", "heatsink_select", "select_heatsink"):
        fn = getattr(et, name, None)
        if callable(fn):
            return fn
    return None


def _sim_caps(params: dict) -> dict:
    del params
    caps: dict = {"ok": True}
    try:
        from services.mcp import fea_advanced as fa
        ccx = fa.ccx_path()
        caps["ccx"] = {"found": bool(ccx), "path": ccx or "",
                       "hint": "" if ccx else
                       "CalculiX (ccx) not on PATH — buckling/contact studies need it. "
                       "Install ccx or set AI_CAD_CCX."}
    except Exception as e:  # noqa: BLE001
        caps["ccx"] = {"found": False, "path": "", "hint": f"fea_advanced unavailable: {e}"}
    try:
        import numpy  # noqa: F401
        from engine_oracle.sim import vibration  # noqa: F401
        caps["freq_response"] = {"available": True, "hint": ""}
    except Exception as e:  # noqa: BLE001
        caps["freq_response"] = {"available": False,
                                 "hint": f"vibration post-processing unavailable: {e}"}
    # Handoff seam availability (aicad.simhandoff/1 producers/consumers).
    ecad_hint = ""
    try:
        from services.mcp.ecad_thermal import thermal_handoff_impl  # noqa: F401
        ecad_ok = True
    except Exception as e:  # noqa: BLE001
        ecad_ok, ecad_hint = False, str(e)
    piping_hint = ""
    try:
        from engine.piping.cfd_handoff import verify_pressure_drop  # noqa: F401
        piping_ok = True
    except Exception as e:  # noqa: BLE001
        piping_ok, piping_hint = False, str(e)
    caps["handoffs"] = {
        "ecad_thermal": {"available": ecad_ok, "hint": ecad_hint},
        "heatsink_select": {"available": _heatsink_endpoint() is not None,
                            "hint": "" if _heatsink_endpoint() is not None else
                            "heatsink selection loop (T-FIN17) not installed yet"},
        # motion_fea_handoff needs the MCP session's motion+static chaining; no worker
        # mirror yet — the GUI shows honest guidance instead of a dead button.
        "motion_fea": {"available": False,
                       "hint": "run via the MCP tool motion_fea_handoff (agent copilot) — "
                               "worker mirror pending"},
        "piping_verify_cfd": {"available": piping_ok, "hint": piping_hint},
    }
    return caps


# --------------------------------------------------------------------------- ccx proxies

def _sim_run_buckling(params: dict) -> dict:
    """{L_mm,b_mm,h_mm, E_mpa, nu, load_n, n_modes} -> buckling load factors.
    Box-proxy fixed-free Euler column, identical to the run_fea_buckling MCP tool."""
    from services.mcp import fea_advanced as fa
    if fa.ccx_path() is None:
        return fa._ccx_absent_result("linear buckling")  # noqa: SLF001 — shared helper
    L = float(params.get("L_mm") or 100.0)
    b = float(params.get("b_mm") or 20.0)
    h = float(params.get("h_mm") or 20.0)
    E = float(params.get("E_mpa") or 200000.0)
    nu = float(params.get("nu") or 0.3)
    P = max(1.0, abs(float(params.get("load_n") or 1000.0)))
    n_modes = int(params.get("n_modes") or 4)
    gen = fa.build_buckling_column_inp(L, b, h, E, nu, P, nz=4)
    run = fa.run_ccx(gen["inp"], job="buckle")
    if run.get("skipped"):
        return run
    if not run.get("ok") or not run.get("dat_path"):
        return {"ok": False, "error": run.get("stderr", "")[-400:]
                or run.get("error", "ccx buckling run failed"),
                "backend": run.get("backend")}
    factors = fa.parse_buckle_factors(run["dat_path"])[:n_modes]
    return {"ok": True, "backend": run["backend"],
            "solver": "CalculiX *BUCKLE (box proxy, fixed-free column)",
            "applied_load_n": P,
            "load_factors": [round(f, 4) for f in factors],
            "critical_loads_n": [round(f * P, 3) for f in factors],
            "euler_reference_pcr_n": round(gen["expected_Pcr"], 3),
            "note": "box-proxy Euler buckling estimate sized to the part bounding box; "
                    "ccx GPL-2.0 subprocess. load_factor<1 = buckles below the applied "
                    "load."}


def _sim_run_contact(params: dict) -> dict:
    """{L_mm,b_mm,h_mm, E_mpa, nu, pressure_mpa} -> contact conservation check.
    Two-block proxy, identical to the run_fea_contact MCP tool."""
    from services.mcp import fea_advanced as fa
    if fa.ccx_path() is None:
        return fa._ccx_absent_result("nonlinear contact")  # noqa: SLF001
    L = float(params.get("L_mm") or 50.0)
    b = float(params.get("b_mm") or 50.0)
    h = float(params.get("h_mm") or 20.0)
    E = float(params.get("E_mpa") or 200000.0)
    nu = float(params.get("nu") or 0.3)
    pressure = float(params.get("pressure_mpa") or 1.0)
    gen = fa.build_contact_block_inp(L, b, h, E, nu, pressure)
    run = fa.run_ccx(gen["inp"], job="contact")
    if run.get("skipped"):
        return run
    if not run.get("ok") or not run.get("dat_path"):
        return {"ok": False, "error": run.get("stderr", "")[-400:]
                or run.get("error", "ccx contact run failed"),
                "backend": run.get("backend")}
    rf = fa.parse_dat_nodal(run["dat_path"], "RF", gen["base_nodes"], component=3)
    reaction = abs(rf * len(gen["base_nodes"])) if rf is not None else None
    return {"ok": True, "backend": run["backend"],
            "solver": "CalculiX *STATIC,NLGEOM + *CONTACT PAIR (two-block proxy)",
            "applied_load_n": round(gen["applied_load_n"], 3),
            "base_reaction_n": (round(reaction, 3) if reaction is not None else None),
            "note": "box-proxy contact conservation check sized to the part bounding "
                    "box; base reaction should equal the applied load. ccx GPL-2.0 "
                    "subprocess."}


# --------------------------------------------------------------------------- frequency response

def _sim_freq_response(params: dict) -> dict:
    """Post-process a C++ core sim_run_modal result into harmonic + random vibration
    response (run_fea_frequency_response's math). Params:
      freqs_hz[], mode_shapes (flat n_modes*n_nodes*3), n_nodes, total_mass_kg,
      excitation_dir?[3], sweep_hz?[], zeta?, base_accel_g?, psd_breakpoints?[[f,psd]],
      vibration_spec?{max_peak_accel_g, max_grms}."""
    try:
        from engine_oracle.sim.vibration import (
            evaluate_vibration_spec, harmonic_response, miles_response)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"vibration post-processing unavailable: {e}",
                "hint": "numpy is required for the frequency-response leg"}
    freqs = [float(f) for f in params.get("freqs_hz") or []]
    shapes = params.get("mode_shapes") or []
    n_nodes = int(params.get("n_nodes") or 0)
    mass = float(params.get("total_mass_kg") or 0.0)
    if not freqs or not shapes or n_nodes <= 0:
        return {"ok": False, "error": "need freqs_hz + mode_shapes + n_nodes from a "
                                      "modal solve"}
    if mass <= 0.0:
        return {"ok": False, "error": "total_mass_kg must be > 0 — set a material with "
                                      "density so the part mass is known"}
    ex_dir = [float(x) for x in (params.get("excitation_dir") or [0.0, 0.0, 1.0])]
    sweep = [float(f) for f in (params.get("sweep_hz") or [])]
    if not sweep:  # default: 200 log-spaced points across the common vibe-test band
        sweep = [10.0 * (2000.0 / 10.0) ** (i / 199.0) for i in range(200)]
    zeta = float(params.get("zeta") or 0.02)
    base_g = float(params.get("base_accel_g") or 1.0)
    psd_in = params.get("psd_breakpoints")
    harm = harmonic_response(freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                             total_mass_kg=mass, excitation_dir=ex_dir, sweep_hz=sweep,
                             zeta=zeta, base_accel_g=base_g)
    if psd_in:
        rand: dict = miles_response(freqs_hz=freqs, mode_shapes_flat=shapes,
                                    n_nodes=n_nodes, total_mass_kg=mass,
                                    excitation_dir=ex_dir,
                                    psd_breakpoints=[(float(p[0]), float(p[1]))
                                                     for p in psd_in],
                                    zeta=zeta)
        rand["ok"] = True
    else:  # honest degrade — NEVER assume a default vibration profile
        rand = {"ok": False, "reason": "no_psd_provided",
                "hint": "Provide psd_breakpoints=[[f_hz, g2_per_hz], ...] for random-"
                        "vibration GRMS via Miles' equation."}
    verdict = evaluate_vibration_spec(params.get("vibration_spec"),
                                      harm, rand if rand.get("ok") else None)
    return {"ok": True, "freqs_hz": freqs, "harmonic": harm, "random": rand,
            "verdict": verdict,
            "damping_assumption": f"zeta={zeta} (Q={1.0 / (2.0 * zeta):g}) — assumed "
                                  "modal damping, not measured"}


# --------------------------------------------------------------------------- handoffs

def _ecad_thermal_handoff(params: dict) -> dict:
    """{board: <ECAD board doc JSON>, environment?, h_w_m2k?, t_amb_c?,
    include_trace_heating?, transient?, mesh_fineness?, dry_run?, doc_id?} ->
    thermal_handoff_impl result (same payload as the MCP tool, dict-shaped)."""
    from services.mcp.ecad_thermal import thermal_handoff_impl
    return thermal_handoff_impl(
        params.get("board") or {},
        doc_id=str(params.get("doc_id") or ""),
        environment=str(params.get("environment") or "still_air"),
        h_w_m2k=params.get("h_w_m2k"),
        t_amb_c=params.get("t_amb_c"),
        include_trace_heating=bool(params.get("include_trace_heating", False)),
        transient=params.get("transient"),
        mesh_fineness=float(params.get("mesh_fineness") or 0.35),
        dry_run=bool(params.get("dry_run", False)))


def _heatsink_select(params: dict) -> dict:
    """T-FIN17 forward seam: delegates when the heatsink selection impl exists."""
    fn = _heatsink_endpoint()
    if fn is None:
        return {"ok": False, "skipped": True,
                "error": "heatsink selection loop (T-FIN17) not installed yet",
                "hint": "the 'Pick heatsink' action lights up when T-FIN17 lands"}
    out = fn(params or {})
    if isinstance(out, str):
        import json as _json
        try:
            out = _json.loads(out)
        except Exception:  # noqa: BLE001
            out = {"ok": False, "error": "heatsink_select returned non-JSON output"}
    return out


def _piping_verify_cfd(params: dict) -> dict:
    """{solve_result: <piping_solve dict>, cfd_result: <run_cfd dict>,
    tolerance_pct?, run_id?} -> verify envelope (engine.piping.cfd_handoff)."""
    from engine.piping.cfd_handoff import verify_pressure_drop
    solve = params.get("solve_result")
    cfd = params.get("cfd_result")
    if not isinstance(solve, dict) or not isinstance(cfd, dict):
        return {"ok": False, "error": "need solve_result (piping_solve output) and "
                                      "cfd_result (run_cfd_* output) dicts"}
    return verify_pressure_drop(solve, cfd,
                                tolerance_pct=float(params.get("tolerance_pct") or 20.0),
                                run_id=params.get("run_id"))


WORKER_METHODS: dict[str, Callable] = {
    "sim_caps": _wrap(_sim_caps),
    "sim_run_buckling": _wrap(_sim_run_buckling),
    "sim_run_contact": _wrap(_sim_run_contact),
    "sim_freq_response": _wrap(_sim_freq_response),
    "ecad_thermal_handoff": _wrap(_ecad_thermal_handoff),
    "heatsink_select": _wrap(_heatsink_select),
    "piping_verify_cfd": _wrap(_piping_verify_cfd),
}
