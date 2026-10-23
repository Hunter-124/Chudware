"""Multi-physics worker endpoints — the GUI-facing mirror of the advanced
simulation + cross-sim-handoff MCP tools.

Registered into the JSON-RPC worker (``engine_oracle/worker.py``) so the desktop
Simulate workspace drives the SAME implementations as the MCP tools (the fw_* /
firmware worker_rpc pattern): thin ``(params, emit)`` adapters over framework-free
module functions.

Endpoints:
  sim_caps            capability doctor for the Simulate tab's honest gating: CalculiX
                      presence (contact proxy), vibration post-processing (numpy),
                      and which cross-sim handoff endpoints are actually servable —
                      including a forward probe for the heatsink picker so the
                      GUI button lights up the moment that tool lands.
  sim_run_contact     CalculiX nonlinear contact two-block proxy (run_fea_contact).
  sim_freq_response   modal-superposition harmonic + Miles random post-processing over
                      a C++ core sim_run_modal result (run_fea_frequency_response's
                      math, engine_oracle/sim/vibration.py).
  ecad_thermal_handoff  board JSON -> solved thermal FEA study + aicad.simhandoff/1
                      envelope (services/mcp/ecad_thermal.thermal_handoff_impl — the
                      same impl the MCP tool wraps).
  piping_verify_cfd   1D-vs-3D pressure-drop verification envelope
                      (services.piping.cfd_handoff.verify_pressure_drop).

Graceful posture (firmware contract): expected failures come back as result payloads
(``{ok:false, error|skipped, hint}``), never raised exceptions.
"""

from __future__ import annotations

import json
import math
from typing import Callable, Optional


def _wrap(fn: Callable[[dict], dict]) -> Callable[[dict, Callable], dict]:
    def handler(params: dict, emit) -> dict:  # noqa: ANN001
        del emit
        try:
            result = fn(params or {})
            if not isinstance(result, dict):
                return {"ok": False,
                        "error": f"{fn.__name__} returned a non-object result"}
            json.dumps(result, allow_nan=False)
            return result
        except Exception as exc:  # noqa: BLE001 — worker contract: payloads, not raises
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return handler


def _finite_param(params: dict, key: str, default: float, *,
                  positive: bool = False) -> float:
    raw = default if params.get(key) is None else params[key]
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    if positive and value <= 0.0:
        raise ValueError(f"{key} must be > 0")
    return value


def _positive_int_param(params: dict, key: str, default: int) -> int:
    raw = default if params.get(key) is None else params[key]
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw)
    if value != float(raw) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


# --------------------------------------------------------------------------- capability doctor

def _heatsink_endpoint() -> Optional[Callable]:
    """Forward probe for the heatsink selection loop. Returns the callable
    when the tool has landed as an importable impl, else None — the GUI's 'Pick
    heatsink' button is wired behind this check and lights up when the tool is present.

    Prefers services.mcp.heatsink (where the tool actually lives) over a historical
    probe on ecad_thermal. Worker adapters accept a params-dict and return a dict.
    """
    try:
        from services.mcp import heatsink as hs
        fn = getattr(hs, "heatsink_select_from_params", None)
        if callable(fn):
            return fn
        # Fallback: kwargs-style impl wrapped as params-dict
        impl = getattr(hs, "heatsink_select_impl", None)
        if callable(impl):
            def _wrap_impl(params: dict) -> dict:
                p = params or {}
                return impl(
                    component_ref=str(p.get("component_ref") or ""),
                    tj_target_c=p.get("tj_target_c"),
                    t_case_target_c=p.get("t_case_target_c"),
                    tim_t_mm=float(p.get("tim_t_mm") if p.get("tim_t_mm") is not None
                                   else hs.TIM_T_MM_DEFAULT),
                    tim_k_w_mk=float(p.get("tim_k_w_mk") if p.get("tim_k_w_mk") is not None
                                     else hs.TIM_K_W_MK_DEFAULT),
                    include_library=bool(p.get("include_library", True)),
                    include_parametric=bool(p.get("include_parametric", True)),
                    max_candidates=int(p.get("max_candidates")
                                       if p.get("max_candidates") is not None else 8),
                )
            return _wrap_impl
    except Exception:  # noqa: BLE001
        pass
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
                       "CalculiX (ccx) not on PATH — the legacy contact proxy needs it. "
                       "Install ccx or set CHUDWARE_CCX."}
    except Exception as e:  # noqa: BLE001
        caps["ccx"] = {"found": False, "path": "", "hint": f"fea_advanced unavailable: {e}"}
    try:
        import numpy  # noqa: F401
        from ..sim import vibration  # noqa: F401
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
        from services.piping.cfd_handoff import verify_pressure_drop  # noqa: F401
        piping_ok = True
    except Exception as e:  # noqa: BLE001
        piping_ok, piping_hint = False, str(e)
    caps["handoffs"] = {
        "ecad_thermal": {"available": ecad_ok, "hint": ecad_hint},
        "heatsink_select": {"available": _heatsink_endpoint() is not None,
                            "hint": "" if _heatsink_endpoint() is not None else
                            "heatsink selection loop not installed yet"},
        # motion_fea_handoff needs the MCP session's motion+static chaining; no worker
        # mirror yet — the GUI shows honest guidance instead of a dead button.
        "motion_fea": {"available": False,
                       "hint": "run via the MCP tool motion_fea_handoff (agent copilot) — "
                               "worker mirror pending"},
        "piping_verify_cfd": {"available": piping_ok, "hint": piping_hint},
    }
    return caps


# --------------------------------------------------------------------------- ccx proxies

def _sim_run_contact(params: dict) -> dict:
    """Run the CalculiX two-block contact proxy and require force balance."""
    try:
        length = _finite_param(params, "L_mm", 50.0, positive=True)
        width = _finite_param(params, "b_mm", 50.0, positive=True)
        height = _finite_param(params, "h_mm", 20.0, positive=True)
        modulus = _finite_param(params, "E_mpa", 200000.0, positive=True)
        poisson = _finite_param(params, "nu", 0.3)
        pressure = _finite_param(params, "pressure_mpa", 1.0, positive=True)
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"invalid contact setup: {exc}"}
    if not -1.0 < poisson < 0.5:
        return {"ok": False, "error": "invalid contact setup: nu must be between -1 and 0.5"}

    from services.mcp import fea_advanced as fa
    if fa.ccx_path() is None:
        return fa._ccx_absent_result("nonlinear contact")  # noqa: SLF001
    generated = fa.build_contact_block_inp(
        length, width, height, modulus, poisson, pressure)
    run = fa.run_ccx(generated["inp"], job="contact")
    if run.get("skipped"):
        return run
    if not run.get("ok") or not run.get("dat_path"):
        return {"ok": False, "error": run.get("stderr", "")[-400:]
                or run.get("error", "ccx contact run failed"),
                "backend": run.get("backend")}
    reaction_per_node = fa.parse_dat_nodal(
        run["dat_path"], "RF", generated["base_nodes"], component=3)
    if reaction_per_node is None or not math.isfinite(float(reaction_per_node)):
        return {"ok": False, "error": "CalculiX contact result has no finite base reaction",
                "backend": run.get("backend")}
    reaction = abs(float(reaction_per_node) * len(generated["base_nodes"]))
    applied = float(generated["applied_load_n"])
    if not math.isfinite(applied) or applied <= 0.0:
        return {"ok": False, "error": "contact applied load is non-finite",
                "backend": run.get("backend")}
    balance_pct = 100.0 * abs(reaction - applied) / applied
    if balance_pct > 5.0:
        return {"ok": False, "error": "contact solve failed force balance",
                "backend": run.get("backend"),
                "applied_load_n": round(applied, 3),
                "base_reaction_n": round(reaction, 3),
                "reaction_balance_error_pct": round(balance_pct, 3)}
    return {"ok": True, "backend": run["backend"],
            "solver": "CalculiX *STATIC,NLGEOM + *CONTACT PAIR (two-block proxy)",
            "applied_load_n": round(applied, 3),
            "base_reaction_n": round(reaction, 3),
            "reaction_balance_error_pct": round(balance_pct, 3),
            "note": "box-proxy contact conservation check sized to the part bounding "
                    "box; base reaction should equal the applied load. ccx GPL-2.0 subprocess."}


# --------------------------------------------------------------------------- frequency response

def _sim_freq_response(params: dict) -> dict:
    """Validate and post-process a core modal result into harmonic and random response."""
    try:
        from ..sim.vibration import (
            evaluate_vibration_spec, harmonic_response, miles_response)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"vibration post-processing unavailable: {exc}",
                "hint": "numpy is required for the frequency-response leg"}

    try:
        raw_freqs = params.get("freqs_hz")
        raw_shapes = params.get("mode_shapes")
        if not isinstance(raw_freqs, (list, tuple)) or not raw_freqs:
            raise ValueError("need freqs_hz from a modal solve")
        if not isinstance(raw_shapes, (list, tuple)) or not raw_shapes:
            raise ValueError("need mode_shapes from a modal solve")
        freqs = [float(f) for f in raw_freqs]
        if any(not math.isfinite(f) or f < 0.0 for f in freqs):
            raise ValueError("freqs_hz must contain finite non-negative frequencies")
        if not any(f >= 0.5 for f in freqs):
            raise ValueError("modal result has no elastic mode at or above 0.5 Hz")
        n_nodes = _positive_int_param(params, "n_nodes", 0)
        mass = _finite_param(params, "total_mass_kg", 0.0, positive=True)
        direction_raw = params.get("excitation_dir")
        direction_raw = [0.0, 0.0, 1.0] if direction_raw is None else direction_raw
        if not isinstance(direction_raw, (list, tuple)) or len(direction_raw) != 3:
            raise ValueError("excitation_dir must contain three components")
        excitation_dir = [float(value) for value in direction_raw]
        if (any(not math.isfinite(value) for value in excitation_dir)
                or not any(value != 0.0 for value in excitation_dir)):
            raise ValueError("excitation_dir must be finite and non-zero")
        zeta = _finite_param(params, "zeta", 0.02, positive=True)
        if zeta >= 1.0:
            raise ValueError("zeta must be less than 1 for modal resonance response")
        base_accel = _finite_param(params, "base_accel_g", 1.0, positive=True)

        raw_sweep = params.get("sweep_hz")
        if raw_sweep:
            sweep = [float(f) for f in raw_sweep]
        else:
            sweep = [10.0 * (2000.0 / 10.0) ** (i / 199.0) for i in range(200)]
            sweep = sorted(set(sweep + [f for f in freqs if 10.0 <= f <= 2000.0]))
        if not sweep or any(not math.isfinite(f) or f <= 0.0 for f in sweep):
            raise ValueError("sweep_hz must contain finite positive frequencies")

        harmonic = harmonic_response(
            freqs_hz=freqs, mode_shapes_flat=raw_shapes, n_nodes=n_nodes,
            total_mass_kg=mass, excitation_dir=excitation_dir, sweep_hz=sweep,
            zeta=zeta, base_accel_g=base_accel)
        psd_input = params.get("psd_breakpoints")
        if psd_input:
            random: dict = miles_response(
                freqs_hz=freqs, mode_shapes_flat=raw_shapes, n_nodes=n_nodes,
                total_mass_kg=mass, excitation_dir=excitation_dir,
                psd_breakpoints=psd_input, zeta=zeta)
            random["ok"] = True
        else:
            random = {"ok": False, "reason": "no_psd_provided",
                      "hint": "Provide psd_breakpoints=[[f_hz, g2_per_hz], ...] for "
                              "random-vibration GRMS via Miles' equation."}
        spec = params.get("vibration_spec")
        if spec is not None and not isinstance(spec, dict):
            raise ValueError("vibration_spec must be an object")
        verdict = evaluate_vibration_spec(
            spec, harmonic, random if random.get("ok") else None)
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": f"invalid frequency-response setup: {exc}"}

    return {"ok": True, "freqs_hz": freqs, "harmonic": harmonic, "random": random,
            "verdict": verdict,
            "damping_assumption": f"zeta={zeta} (Q={1.0 / (2.0 * zeta):g}) — assumed "
                                  "modal damping, not measured"}


# --------------------------------------------------------------------------- handoffs

def _ecad_thermal_handoff(params: dict) -> dict:
    """Validate worker-only scalars before dispatching the shared ECAD thermal handoff."""
    mesh_fineness = _finite_param(params, "mesh_fineness", 0.35, positive=True)
    if mesh_fineness > 1.0:
        return {"ok": False, "error": "mesh_fineness must be in (0, 1]"}
    h_value = params.get("h_w_m2k")
    if h_value is not None:
        h_value = _finite_param(params, "h_w_m2k", 0.0, positive=True)
    ambient = params.get("t_amb_c")
    if ambient is not None:
        ambient = _finite_param(params, "t_amb_c", 0.0)
    from services.mcp.ecad_thermal import thermal_handoff_impl
    return thermal_handoff_impl(
        params.get("board") or {},
        doc_id=str(params.get("doc_id") or ""),
        environment=str(params.get("environment") or "still_air"),
        h_w_m2k=h_value,
        t_amb_c=ambient,
        include_trace_heating=bool(params.get("include_trace_heating", False)),
        transient=params.get("transient"),
        mesh_fineness=mesh_fineness,
        dry_run=bool(params.get("dry_run", False)))


def _heatsink_select(params: dict) -> dict:
    """Delegate only when the capability exists and require an object result."""
    fn = _heatsink_endpoint()
    if fn is None:
        return {"ok": False, "skipped": True,
                "error": "heatsink selection loop not installed yet",
                "hint": "the 'Pick heatsink' action lights up when the heatsink tool is installed"}
    out = fn(params or {})
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "heatsink_select returned non-JSON output"}
    if not isinstance(out, dict):
        return {"ok": False, "error": "heatsink_select returned a non-object result"}
    return out


def _piping_verify_cfd(params: dict) -> dict:
    """{solve_result: <piping_solve dict>, cfd_result: <run_cfd dict>,
    tolerance_pct?, run_id?} -> verify envelope (services.piping.cfd_handoff)."""
    from services.piping.cfd_handoff import verify_pressure_drop
    solve = params.get("solve_result")
    cfd = params.get("cfd_result")
    if not isinstance(solve, dict) or not isinstance(cfd, dict):
        return {"ok": False, "error": "need solve_result (piping_solve output) and "
                                      "cfd_result (run_cfd_* output) dicts"}
    tolerance = _finite_param(params, "tolerance_pct", 20.0)
    if tolerance < 0.0:
        return {"ok": False, "error": "tolerance_pct must be finite and >= 0"}
    return verify_pressure_drop(solve, cfd, tolerance_pct=tolerance,
                                run_id=params.get("run_id"))


WORKER_METHODS: dict[str, Callable] = {
    "sim_caps": _wrap(_sim_caps),
    "sim_run_contact": _wrap(_sim_run_contact),
    "sim_freq_response": _wrap(_sim_freq_response),
    "ecad_thermal_handoff": _wrap(_ecad_thermal_handoff),
    "heatsink_select": _wrap(_heatsink_select),
    "piping_verify_cfd": _wrap(_piping_verify_cfd),
}
