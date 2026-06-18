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
  sim_run_flow   {stl_path, direction, speed_mps, preset}
                                      -> delegated to engine.sim.lbm.run_wind_tunnel
                                         (C07 card; skipped if module absent)
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


# FEA (sim_mesh / sim_run_static / sim_run_modal) MIGRATED TO THE C++ CORE (libaicad), which
# the host routes to by default (AI_CAD_CPP_SIM on). The Python gmsh mesher + FEA solvers
# (engine/sim/{mesher,fea_static,fea_modal}.py) and their result-overlay packaging were deleted
# after a final differential-parity pass (mesh 8/8, fea-run 7/7 — MATCH). This sim worker now
# serves only the LBM wind-tunnel (sim_run_flow), which stays Python (trimesh voxelization).


def _sim_run_flow(params: dict, emit: EmitFn) -> dict:
    """Delegate to engine.sim.lbm.run_wind_tunnel (C07 card).

    Signature per C07 card:
      run_wind_tunnel(stl_path, direction=(1,0,0), speed_mps=10.0,
                      preset="coarse", on_progress=None, max_steps=None)
      -> FlowResult with .drag_n, .lift_n, .cd, .frontal_area_mm2,
                        .reynolds_note, .trace, .diverged
    """
    try:
        from engine.sim.lbm import run_wind_tunnel  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "engine.sim.lbm is not available (C07 card not yet built). "
            "sim_run_flow is unavailable in this build."
        )

    stl_path = params["stl_path"]
    direction = tuple(params.get("direction", [1, 0, 0]))
    speed_mps = float(params.get("speed_mps", 10.0))
    preset = params.get("preset", "coarse")
    job_id = params.get("job_id", "flow")

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

    result = run_wind_tunnel(
        stl_path=stl_path,
        direction=direction,
        speed_mps=speed_mps,
        preset=preset,
        on_progress=on_progress,
    )

    # Build serialisable return dict (FlowResult may have numpy arrays in slices/streamlines)
    import numpy as np

    def _maybe_tolist(v):
        if isinstance(v, np.ndarray):
            return v.tolist()
        if isinstance(v, list):
            return [_maybe_tolist(i) for i in v]
        return v

    out = {
        "drag_n": float(result.drag_n),
        "lift_n": float(result.lift_n),
        "cd": float(result.cd),
        "frontal_area_mm2": float(result.frontal_area_mm2),
        "reynolds_note": str(result.reynolds_note),
        "diverged": bool(result.diverged),
        "trace": [{"step": s, "drag": d} for s, d in result.trace],
    }

    # ── C19: result overlays — streamline LINS + colormap slice PNGs ──
    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "post", "pct": 95, "text": "Packing flow overlays"})
    try:
        from engine.sim import overlay
        from engine.doc import tessellate as tess
        sl_chunk = overlay.streamlines_chunk(result.streamline_points, result.streamline_speeds)
        if sl_chunk is not None:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
                lines_path = tf.name
            # LINS-only AICM (no body): write_aicm tolerates a body-less file via extra_chunks.
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
