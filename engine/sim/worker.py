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


# ── C19: result overlay packaging (best-effort; never fails the solve) ─────────

def _static_overlay_path(study, mesh, result, material):
    """Build the surface SCLR/DISP overlay AICM for a static result. Returns path or None.

    Needs a STEP surface to tessellate (study['step_path']); skips silently if absent or if
    packaging fails so a missing overlay never aborts a successful solve.
    """
    step_path = study.get("step_path")
    if not step_path or not Path(step_path).exists():
        return None
    try:
        from engine.sim import overlay
        yield_mpa = float(material.get("yield_mpa", 250.0))
        return overlay.static_overlay(
            step_path, mesh.points, result.von_mises, result.disp_magnitude,
            result.displacement, yield_mpa, result.summary)
    except Exception as e:  # noqa: BLE001
        log.warning("static overlay packaging failed: %s", e)
        return None


def _modal_overlay_path(study, mesh, result, mode_index):
    step_path = study.get("step_path")
    if not step_path or not Path(step_path).exists():
        return None
    try:
        import numpy as np
        from engine.sim import overlay
        shapes = getattr(result, "mode_shapes", None)
        if shapes is None:
            return None
        shp = np.asarray(shapes)[mode_index]
        freq = float(result.freqs_hz[mode_index])
        return overlay.modal_overlay(step_path, mesh.points, shp, freq)
    except Exception as e:  # noqa: BLE001
        log.warning("modal overlay packaging failed: %s", e)
        return None


def _sim_mesh(params: dict, emit: EmitFn) -> dict:
    from engine.sim.mesher import mesh_step

    step_path = params["step_path"]
    density = float(params.get("density", 0.5))

    job_id = params.get("job_id", "mesh")

    def on_progress(info: dict) -> None:
        emit({"kind": "sim_progress", "job_id": job_id,
              "stage": "mesh",
              "pct": info.get("pct", 0),
              "text": info.get("text", "meshing")})

    # Mesher doesn't take on_progress, so we emit start/done
    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "mesh", "pct": 0, "text": "Starting gmsh mesh"})

    result = mesh_step(step_path, density)

    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "mesh", "pct": 100,
          "text": f"Mesh done: {result.n_nodes} nodes, {result.n_elems} elems"})

    return {
        "mesh_path": result.mesh_path,
        "nodes": result.n_nodes,
        "elems": result.n_elems,
        "char_len_mm": result.char_len_mm,
    }


def _sim_run_static(params: dict, emit: EmitFn) -> dict:
    import numpy as np
    from engine.sim.mesher import mesh_step, MeshResult
    from engine.sim.fea_static import run_static, FaceSet, Load

    study = params["study"]
    job_id = study.get("job_id", "static")

    def on_progress(info: dict) -> None:
        if _cancel_flag:
            raise RuntimeError("Cancelled by sim_cancel")
        emit({"kind": "sim_progress", "job_id": job_id,
              "stage": info.get("stage", "solve"),
              "pct": info.get("pct", 0),
              "text": info.get("text", "")})

    # Load or re-mesh
    mesh_path = study.get("mesh_path")
    step_path = study.get("step_path")
    density = float(study.get("density", 0.5))

    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "mesh", "pct": 0, "text": "Loading mesh"})

    if mesh_path and Path(mesh_path).exists():
        # Load from pre-computed mesh
        import meshio
        m = meshio.read(mesh_path)
        points = m.points.astype(np.float64)
        tets = None
        for cells in m.cells:
            if cells.type in ("tetra", "tetra4"):
                tets = cells.data.astype(np.int32)
                break
        if tets is None:
            raise ValueError(f"No tet4 elements found in mesh file {mesh_path}")
        mesh = MeshResult(
            points=points, tets=tets, mesh_path=mesh_path,
            char_len_mm=0.0, n_nodes=len(points), n_elems=len(tets)
        )
    elif step_path:
        mesh = mesh_step(step_path, density)
    else:
        raise ValueError("study must have mesh_path or step_path")

    # Material
    material = study.get("material", {})

    # Fixtures: list of {points: [[x,y,z],...], tol_mm}
    fixtures = []
    for fx in study.get("fixtures", []):
        pts = np.array(fx["points"], dtype=np.float64)
        tol = float(fx.get("tol_mm", 1.0))
        fixtures.append(FaceSet(points=pts, tol_mm=tol))

    # Loads: list of {faces:{points,tol_mm}, type, vector|magnitude}
    loads = []
    for ld in study.get("loads", []):
        fs_data = ld["faces"]
        fs = FaceSet(
            points=np.array(fs_data["points"], dtype=np.float64),
            tol_mm=float(fs_data.get("tol_mm", 1.0)),
        )
        vec = None
        mag = None
        if ld.get("vector") is not None:
            vec = np.array(ld["vector"], dtype=np.float64)
        if ld.get("magnitude") is not None:
            mag = float(ld["magnitude"])
        loads.append(Load(faceset=fs, type=ld["type"], vector=vec, magnitude=mag))

    result = run_static(mesh, material, fixtures, loads, on_progress=on_progress)

    # Save results
    out_npz = Path(mesh.mesh_path).with_suffix(".static.npz")
    result.save_npz(str(out_npz))

    out = {
        "results_npz": str(out_npz),
        "summary": result.summary,
        "solver_notes": result.solver_notes,
        "n_dof": result.n_dof,
        "solve_time_s": result.solve_time_s,
    }

    # ── C19: result overlay (surface SCLR von_mises/displacement_mag/fos + DISP) ──
    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "post", "pct": 90, "text": "Packing result overlay"})
    overlay = _static_overlay_path(study, mesh, result, material)
    if overlay:
        out["overlay_aicm"] = overlay
    return out


def _sim_run_modal(params: dict, emit: EmitFn) -> dict:
    import numpy as np
    from engine.sim.mesher import mesh_step, MeshResult
    from engine.sim.fea_modal import run_modal
    from engine.sim.fea_static import FaceSet

    study = params["study"]
    n_modes = int(params.get("n_modes", 6))
    job_id = study.get("job_id", "modal")

    def on_progress(info: dict) -> None:
        if _cancel_flag:
            raise RuntimeError("Cancelled by sim_cancel")
        emit({"kind": "sim_progress", "job_id": job_id,
              "stage": info.get("stage", "solve"),
              "pct": info.get("pct", 0),
              "text": info.get("text", "")})

    mesh_path = study.get("mesh_path")
    step_path = study.get("step_path")
    density = float(study.get("density", 0.5))

    emit({"kind": "sim_progress", "job_id": job_id,
          "stage": "mesh", "pct": 0, "text": "Loading mesh"})

    if mesh_path and Path(mesh_path).exists():
        import meshio
        m = meshio.read(mesh_path)
        points = m.points.astype(np.float64)
        tets = None
        for cells in m.cells:
            if cells.type in ("tetra", "tetra4"):
                tets = cells.data.astype(np.int32)
                break
        if tets is None:
            raise ValueError(f"No tet4 elements found in mesh file {mesh_path}")
        mesh = MeshResult(
            points=points, tets=tets, mesh_path=mesh_path,
            char_len_mm=0.0, n_nodes=len(points), n_elems=len(tets)
        )
    elif step_path:
        mesh = mesh_step(step_path, density)
    else:
        raise ValueError("study must have mesh_path or step_path")

    material = study.get("material", {})

    fixtures = []
    for fx in study.get("fixtures", []):
        pts = np.array(fx["points"], dtype=np.float64)
        tol = float(fx.get("tol_mm", 1.0))
        fixtures.append(FaceSet(points=pts, tol_mm=tol))

    result = run_modal(mesh, material, fixtures, n_modes=n_modes,
                       on_progress=on_progress)

    out_npz = Path(mesh.mesh_path).with_suffix(".modal.npz")
    result.save_npz(str(out_npz))

    out = {
        "freqs_hz": result.freqs_hz.tolist(),
        "results_npz": str(out_npz),
        "n_dof": result.n_dof,
        "solve_time_s": result.solve_time_s,
    }
    # C19: overlay for the first (lowest) mode shape.
    overlay = _modal_overlay_path(study, mesh, result, 0)
    if overlay:
        out["overlay_aicm"] = overlay
    return out


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
    "sim_mesh": _sim_mesh,
    "sim_run_static": _sim_run_static,
    "sim_run_modal": _sim_run_modal,
    "sim_run_flow": _sim_run_flow,
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
