"""Sim worker: stdio JSON-RPC process for simulation methods.

Protocol (same as engine/worker.py — newline-delimited JSON):
  request : {"id": int, "method": str, "params": {...}}
  event   : {"id": int, "event": kind, "data": {...}}   (zero or more before response)
  response: {"id": int, "ok": true,  "result": {...}}
          | {"id": int, "ok": false, "error": {"code": str, "message": str, "detail": str}}

Supported methods:
  ping           -> {pong: true, protocol: 2, sim: true}
  sim_mesh       {step_path, density}  -> {mesh_path, nodes, elems, char_len_mm}
  sim_run_static {study}              -> {results_npz, summary}
  sim_run_modal  {study, n_modes}     -> {freqs_hz, results_npz}
  sim_run_flow   {stl_path, direction, speed_mps, preset, aoa_deg?, sideslip_deg?,
                  fluid?{name,density_kgm3,kinematic_visc_m2s}, roughness_um?, ref_area_mm2?}
                                      -> {drag_n, lift_n, side_n, cd, cl, ld, reynolds,
                                          frontal_area_mm2, ref_area_mm2, reynolds_note, diverged,
                                          trace, overlay_aicm, scalar_fields, cp_min, cp_max,
                                          vmax_mps, solver_notes, lines_aicm?, slice_pngs?}
                                         (engine.sim.lbm.run_wind_tunnel; skipped if module absent)
  sim_cancel     {job_id}             -> {cancelled: bool}
  shutdown

Crash isolation: this process never touches live document state.
The host restarts it on demand if it crashes.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Callable

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
log = logging.getLogger("ai-cad.sim-worker")

# Cancel flag — checked by on_progress callbacks; set by sim_cancel
_cancel_flag: bool = False
_current_job_id: str | None = None

EmitFn = Callable[[dict], None]


# ── helpers ───────────────────────────────────────────────────────────────────

def _emit_line(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _error(code: str, message: str, detail: str = "") -> dict:
    return {"code": code, "message": message, "detail": detail}


# ── method implementations ────────────────────────────────────────────────────

def _ping(_params: dict, _emit: EmitFn) -> dict:
    return {"pong": True, "protocol": 2, "sim": True}


def _sim_caps(_params: dict, _emit: EmitFn) -> dict:
    """Report available simulation engines so the host can gate the solver selector.

    -> {lbm: true, openfoam: bool, openfoam_kind: "native"|"wsl"|null, openfoam_version: str}
    LBM is always present (pure Python).  OpenFOAM availability is probed via
    engine.sim.foam_runner (native Windows build preferred, WSL fallback).
    """
    caps = {"lbm": True, "openfoam": False, "openfoam_kind": None, "openfoam_version": ""}
    try:
        from engine.sim.foam_runner import capabilities
        caps.update(capabilities())
    except Exception as e:  # noqa: BLE001 — never let probing crash the worker
        log.warning("openfoam capability probe failed: %s", e)
    return caps


# FEA (sim_mesh / sim_run_static / sim_run_modal) MIGRATED TO THE C++ CORE (libaicad), which
# the host routes to by default (AI_CAD_CPP_SIM on). The Python gmsh mesher + FEA solvers
# (engine/sim/{mesher,fea_static,fea_modal}.py) and their result-overlay packaging were deleted
# after a final differential-parity pass (mesh 8/8, fea-run 7/7 — MATCH). This sim worker now
# serves only the LBM wind-tunnel (sim_run_flow), which stays Python (trimesh voxelization).


def _sim_run_flow(params: dict, emit: EmitFn) -> dict:
    """Delegate to engine.sim.lbm.run_wind_tunnel.

    INPUT params:
      stl_path (str), direction [bx,by,bz] (base flow axis), speed_mps (float),
      preset ("coarse"|"medium"|"fine"|"xfine"),
      aoa_deg (float, default 0), sideslip_deg (float, default 0),
      fluid {name, density_kgm3, kinematic_visc_m2s} (default air 1.225 / 1.5e-5),
      roughness_um (float Ks, default 0 = smooth),
      ref_area_mm2 (float, 0 = auto frontal area).

    OUTPUT (worker -> host):
      drag_n, lift_n, side_n, cd, cl, ld, reynolds,
      frontal_area_mm2, ref_area_mm2, reynolds_note, diverged,
      trace [{step,drag}], overlay_aicm (model-bearing AICM path),
      scalar_fields [str], cp_min, cp_max, vmax_mps, solver_notes [str],
      lines_aicm (back-compat), slice_pngs (back-compat).
    """
    solver = str(params.get("solver", "lbm") or "lbm").lower()
    if solver == "openfoam":
        try:
            from engine.sim.openfoam import run_openfoam_tunnel as run_tunnel  # type: ignore[import]
        except ImportError as e:
            raise RuntimeError(f"OpenFOAM bridge unavailable: {e}")
    else:
        try:
            from engine.sim.lbm import run_wind_tunnel as run_tunnel  # type: ignore[import]
        except ImportError:
            raise RuntimeError(
                "engine.sim.lbm is not available. sim_run_flow is unavailable in this build."
            )

    stl_path = params["stl_path"]
    direction = tuple(params.get("direction", [1, 0, 0]))
    speed_mps = float(params.get("speed_mps", 10.0))
    preset = params.get("preset", "coarse")
    job_id = params.get("job_id", "flow")
    aoa_deg = float(params.get("aoa_deg", 0.0) or 0.0)
    sideslip_deg = float(params.get("sideslip_deg", 0.0) or 0.0)
    fluid = params.get("fluid") or None
    roughness_um = float(params.get("roughness_um", 0.0) or 0.0)
    ref_area_mm2 = float(params.get("ref_area_mm2", 0.0) or 0.0)
    thermal = params.get("thermal") or None  # OpenFOAM-only: heat-transfer config (ignored by LBM)

    def on_progress(info: dict) -> None:
        if _cancel_flag:
            raise RuntimeError("Cancelled by sim_cancel")
        data: dict = {
            "kind": "sim_progress",
            "job_id": job_id,
            "stage": "stream",
            "pct": info.get("pct", 0),
            "text": info.get("text", ""),
        }
        if "trace_point" in info:
            data["trace_point"] = info["trace_point"]
        emit(data)

    tunnel_kwargs = dict(
        stl_path=stl_path,
        direction=direction,
        speed_mps=speed_mps,
        preset=preset,
        on_progress=on_progress,
        aoa_deg=aoa_deg,
        sideslip_deg=sideslip_deg,
        fluid=fluid,
        roughness_um=roughness_um,
        ref_area_mm2=ref_area_mm2,
    )
    if solver == "openfoam":
        tunnel_kwargs["thermal"] = thermal   # LBM has no thermal param; only pass to OpenFOAM
    result = run_tunnel(**tunnel_kwargs)

    out = {
        "drag_n": float(result.drag_n),
        "lift_n": float(result.lift_n),
        "side_n": float(result.side_n),
        "cd": float(result.cd),
        "cl": float(result.cl),
        "ld": float(result.ld),
        "reynolds": float(result.reynolds),
        "frontal_area_mm2": float(result.frontal_area_mm2),
        "ref_area_mm2": float(result.ref_area_mm2),
        "reynolds_note": str(result.reynolds_note),
        "diverged": bool(result.diverged),
        "trace": [{"step": s, "drag": d} for s, d in result.trace],
        "cp_min": float(result.cp_min),
        "cp_max": float(result.cp_max),
        "vmax_mps": float(result.vmax_mps),
        "solver_notes": [str(n) for n in result.solver_notes],
        "scalar_fields": [],
    }

    # ── Result overlays: model-bearing AICM + back-compat lines/PNGs ──
    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "post", "pct": 95, "text": "Packing flow overlays"})
    try:
        from engine.sim import overlay
        from engine.doc import tessellate as tess

        # The OpenFOAM bridge pre-packages its own overlay (no lattice carriers to rebuild from);
        # the LBM solver leaves overlay_path None so we build the overlay here from its lattice.
        prebuilt = getattr(result, "overlay_path", None)
        if prebuilt:
            out["overlay_aicm"] = prebuilt
            # The OpenFOAM bridge reports exactly which fields its overlay carries
            # (pressure_cp+velocity for aero, temperature+velocity for thermal).
            out["scalar_fields"] = list(getattr(result, "scalar_fields", None) or ["pressure_cp"])
        else:
            out["overlay_aicm"] = overlay.flow_overlay(stl_path, result)
            sf = ["pressure_cp"]
            if result.streamline_points:
                sf.append("velocity")
            out["scalar_fields"] = sf

        # Back-compat: lines-only AICM + slice PNGs.
        sl_chunk = overlay.streamlines_chunk(result.streamline_points, result.streamline_speeds)
        if sl_chunk is not None:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
                lines_path = tf.name
            tess.write_aicm_file(lines_path, [], extra_chunks=[sl_chunk])
            out["lines_aicm"] = lines_path
        out["slice_pngs"] = overlay.slice_pngs(
            result.slices, result.slice_transforms, speed_mps)
    except Exception as e:  # noqa: BLE001
        log.warning("flow overlay packaging failed: %s", e)

    return out


def _sim_cancel(params: dict, _emit: EmitFn) -> dict:
    global _cancel_flag
    _cancel_flag = True
    return {"cancelled": True}


# ── dispatch table ────────────────────────────────────────────────────────────

_METHODS: dict[str, Callable] = {
    "ping": _ping,
    "sim_caps": _sim_caps,  # which engines are available (LBM always; OpenFOAM if provisioned)
    # sim_mesh / sim_run_static / sim_run_modal migrated to the C++ core (libaicad).
    "sim_run_flow": _sim_run_flow,  # LBM wind-tunnel — stays Python (trimesh voxelization)
    "sim_cancel": _sim_cancel,
}


# ── serve loop ────────────────────────────────────────────────────────────────

def serve() -> None:
    global _cancel_flag
    log.info("sim worker ready")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _emit_line({"id": None, "ok": False,
                        "error": {"code": "parse_error", "message": str(e), "detail": ""}})
            continue

        rid = req.get("id")
        method = req.get("method")

        if method == "shutdown":
            _emit_line({"id": rid, "ok": True, "result": {"bye": True}})
            break

        fn = _METHODS.get(method)
        if fn is None:
            _emit_line({"id": rid, "ok": False,
                        "error": {"code": "unknown_method",
                                  "message": f"unknown method: {method}",
                                  "detail": ""}})
            continue

        # Reset cancel flag for each new job request (except sim_cancel itself)
        if method not in ("sim_cancel", "ping", "shutdown"):
            _cancel_flag = False

        def emit_event(data: dict, _rid: int = rid) -> None:
            _emit_line({"id": _rid, "event": data.get("kind", "event"), "data": data})

        try:
            result = fn(req.get("params") or {}, emit_event)
            _emit_line({"id": rid, "ok": True, "result": result})
        except Exception as e:  # noqa: BLE001
            _emit_line({
                "id": rid,
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": f"{type(e).__name__}: {e}",
                    "detail": traceback.format_exc(),
                },
            })


if __name__ == "__main__":
    serve()
