"""OpenFOAM external-aero RANS bridge — a drop-in alternative to the LBM wind tunnel.

``run_openfoam_tunnel`` has the *same signature and return type* as
``services.sim.lbm.run_wind_tunnel`` so the sim worker can dispatch to either engine behind one
``sim_run_flow`` RPC.  It:

  1. builds an OpenFOAM case from the part STL (services.sim.foam_case),
  2. meshes it (blockMesh -> surfaceFeatureExtract -> snappyHexMesh) and solves steady
     incompressible RANS (simpleFoam, k-omega SST) via the active runner
     (services.sim.foam_runner: native Windows OpenFOAM preferred, WSL fallback),
  3. streams solver residuals/forces to ``on_progress`` for the live UI, and
  4. post-processes the result into a FlowResult with a ready-made viewport overlay
     (services.sim.foam_post).

Requires a provisioned OpenFOAM (see tools/provision_openfoam.py).  Raises RuntimeError with a
clear message if none is reachable, which the worker surfaces as a structured error.
"""

from __future__ import annotations

import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from services.sim.foam_case import build_case
from services.sim.foam_runner import FoamError, get_runner

_TIME_RE = re.compile(r"^Time = ([0-9.eE+-]+)")
_CD_RE = re.compile(r"\bCd:\s+(\S+)", re.IGNORECASE)
_CONVERGED_RE = re.compile(r"SIMPLE solution converged")

def _require_runner():
    runner = get_runner()
    if runner is None or not runner.available():
        raise RuntimeError(
            "OpenFOAM is not available. Provision it with "
            "`python -m tools.provision_openfoam --install` (WSL) or install blueCFD-Core "
            "natively and set %CHUDWARE_OPENFOAM%."
        )
    return runner


def run_openfoam_tunnel(
    stl_path: str,
    direction: Tuple[float, float, float] = (1.0, 0.0, 0.0),
    speed_mps: float = 10.0,
    preset: str = "coarse",
    on_progress: Optional[Callable[[dict], None]] = None,
    max_steps: Optional[int] = None,
    aoa_deg: float = 0.0,
    sideslip_deg: float = 0.0,
    fluid: Optional[dict] = None,
    roughness_um: float = 0.0,
    ref_area_mm2: float = 0.0,
    thermal: Optional[dict] = None,
):
    """Run an OpenFOAM RANS wind tunnel.  Signature mirrors lbm.run_wind_tunnel.

    ``thermal`` (optional) switches to steady buoyant heat transfer (buoyantSimpleFoam) with a
    wall thermal BC — see services.sim.foam_case.build_case.
    """
    if thermal is not None and not isinstance(thermal, dict):
        raise ValueError("thermal must be a mapping")
    is_cht = bool(thermal and thermal.get("enabled") and thermal.get("mode") == "conjugate")
    if is_cht and max_steps is not None:
        raise ValueError(
            "max_steps is an iteration cap and is not supported by transient CHT; "
            "the CHT solve uses a physics-derived time horizon")

    # Conjugate heat transfer (solid + fluid) is a different solver/mesh path.
    if is_cht:
        return run_openfoam_cht(stl_path, direction, speed_mps, preset, on_progress,
                                aoa_deg, sideslip_deg, fluid, thermal)

    def prog(stage: str, pct: int, text: str, trace_point: Optional[dict] = None) -> None:
        if on_progress is None:
            return
        data = {"job_id": "openfoam", "stage": stage, "pct": int(pct), "text": text}
        if trace_point is not None:
            data["trace_point"] = trace_point
        on_progress(data)

    case_dir = tempfile.mkdtemp(prefix="chudware_foam_")
    try:
        kind = "thermal" if (thermal and thermal.get("enabled")) else "aero"
        prog("mesh", 2, f"Building OpenFOAM {kind} case ({preset})...")
        # max_steps mirrors lbm.run_wind_tunnel: cap the solver iteration count (endTime).
        # Before this plumb the parameter was accepted but silently ignored, so callers
        # could not bound an OpenFOAM solve the way they bound an LBM one.
        info = build_case(case_dir, stl_path, direction=direction, speed_mps=speed_mps,
                          preset=preset, aoa_deg=aoa_deg, sideslip_deg=sideslip_deg,
                          fluid=fluid, ref_area_mm2=ref_area_mm2, n_proc=1, thermal=thermal,
                          max_iters=max_steps)
        runner = _require_runner()

        # ---- mesh ----
        prog("mesh", 6, "blockMesh (background domain)...")
        runner.run(case_dir, "blockMesh", on_line=None)
        prog("mesh", 10, "surfaceFeatureExtract (edges)...")
        runner.run(case_dir, "surfaceFeatureExtract", on_line=None)
        runner.run(case_dir, "snappyHexMesh", ["-overwrite"], on_line=_mesh_line(prog))

        # ---- solve ----
        q = 0.5 * info.rho_kgm3 * info.speed_mps ** 2
        aref_m2 = max(info.ref_area_mm2 * 1e-6, 1e-9)
        state = {"converged": False, "last_time": None, "invalid_output": None}
        prog("solve", 18, f"{info.app} ({'buoyant RANS' if info.thermal else 'RANS'}, {info.iters} iters)...")
        runner.run(case_dir, info.app,
                   on_line=_solve_line(prog, info.iters, q * aref_m2, state),
                   timeout=3300)
        if state["invalid_output"]:
            raise FoamError(info.app, -1, state["invalid_output"])
        if not state["converged"]:
            raise FoamError(
                info.app, -1,
                f"solver exited without satisfying residualControl "
                f"(last iteration {state['last_time']!r} of {info.iters})")

        prog("post", 92, "Post-processing (forces, Cp, streamlines)...")
        from services.sim.foam_post import postprocess
        result = postprocess(case_dir, stl_path, info, converged=True,
                             expected_time=state["last_time"])
        prog("post", 100, f"Done: Cd {result.cd:.3f}, Cl {result.cl:.3f}")
        return result
    finally:
        # Keep the case on disk only if explicitly requested for debugging.
        import os
        if not os.environ.get("CHUDWARE_FOAM_KEEP"):
            shutil.rmtree(case_dir, ignore_errors=True)


def run_openfoam_cht(stl_path, direction, speed_mps, preset, on_progress,
                     aoa_deg, sideslip_deg, fluid, thermal):
    """Conjugate heat transfer: mesh solid+fluid regions and run chtMultiRegionFoam."""
    from services.sim.foam_cht import build_cht_case

    # Multi-solid runs (thermal["solids"]) may omit stl_path; the overlay/display surface
    # then defaults to the first solid's STL.
    if not stl_path and thermal.get("solids"):
        stl_path = thermal["solids"][0].get("stl_path")

    def prog(stage, pct, text, trace_point=None):
        if on_progress is None:
            return
        d = {"job_id": "openfoam-cht", "stage": stage, "pct": int(pct), "text": text}
        if trace_point is not None:
            d["trace_point"] = trace_point
        on_progress(d)

    case_dir = tempfile.mkdtemp(prefix="chudware_cht_")
    try:
        prog("mesh", 2, f"Building CHT case ({preset})...")
        info = build_cht_case(
            case_dir, stl_path, direction=direction, speed_mps=speed_mps, preset=preset,
            aoa_deg=aoa_deg, sideslip_deg=sideslip_deg, fluid=fluid,
            inlet_temp_c=float(thermal.get("inlet_temp_c", 20.0)),
            solid_kappa=float(thermal.get("solid_kappa", 200.0)),
            power_w=float(thermal.get("power_w", 20.0)),
            # Multi-solid seam: optional list of solid regions with per-solid heat sources
            # [{name, stl_path, kappa_w_mk, power_w}, ...]; overrides the single-solid form.
            solids=thermal.get("solids"))
        runner = _require_runner()

        prog("mesh", 6, "blockMesh...")
        runner.run(case_dir, "blockMesh")
        runner.run(case_dir, "surfaceFeatureExtract")
        prog("mesh", 12, "snappyHexMesh (solid + fluid zones)...")
        runner.run(case_dir, "snappyHexMesh", ["-overwrite"])
        prog("mesh", 16, "splitMeshRegions (solid/fluid)...")
        runner.run(case_dir, "splitMeshRegions", ["-cellZones", "-overwrite"])

        state = {"last_time": None, "last_residual": None, "current_residual": None}
        prog("solve", 20, f"chtMultiRegionFoam (coupled transient, {info.end_time:.2g}s horizon)...")
        runner.run(case_dir, "chtMultiRegionFoam",
                   on_line=_cht_line(prog, info.end_time, state), timeout=3300)
        if state["last_time"] is None or state["last_time"] < info.end_time * (1.0 - 1e-6):
            raise FoamError(
                "chtMultiRegionFoam", -1,
                f"solver stopped before requested horizon ({state['last_time']!r} < {info.end_time})")
        final_residual = state["current_residual"]
        if final_residual is None or not math.isfinite(final_residual):
            raise FoamError("chtMultiRegionFoam", -1,
                            "no finite final-step residuals were reported")
        if final_residual > 1e-3:
            raise FoamError(
                "chtMultiRegionFoam", -1,
                f"final coupled residual {final_residual:.6g} exceeds 1e-3")

        prog("post", 90, "Exporting solid temperature...")
        for reg in getattr(info, "solid_regions", ("part",)):
            runner.run(case_dir, "foamToVTK", ["-region", reg, "-latestTime"])

        prog("post", 95, "Post-processing (solid temperature overlay)...")
        from services.sim.foam_post import postprocess_cht
        result = postprocess_cht(case_dir, stl_path, info, residual=final_residual)
        prog("post", 100, f"Done: {result.reynolds_note}")
        return result
    finally:
        import os
        if not os.environ.get("CHUDWARE_FOAM_KEEP"):
            shutil.rmtree(case_dir, ignore_errors=True)


def _cht_line(prog, end_time: float, state: Optional[dict] = None) -> Callable[[str], None]:
    residual_re = re.compile(r"Initial residual\s*=\s*([0-9.eE+-]+)")
    state = state if state is not None else {}

    def on_line(line: str) -> None:
        line = line.strip()
        m = _TIME_RE.match(line)
        if m:
            try:
                t = float(m.group(1))
                if not math.isfinite(t):
                    return
                state["last_time"] = t
                state["last_residual"] = state.get("current_residual")
                state["current_residual"] = None
                pct = 20 + int(min(68, 68 * t / max(end_time, 1e-9)))
                prog("solve", pct, f"chtMultiRegionFoam t={t:.3g}/{end_time:.2g}s")
            except ValueError:
                pass
            return
        r = residual_re.search(line)
        if r:
            try:
                value = float(r.group(1))
                if math.isfinite(value):
                    current = state.get("current_residual")
                    state["current_residual"] = value if current is None else max(current, value)
            except ValueError:
                pass
    return on_line


def _mesh_line(prog) -> Callable[[str], None]:
    def on_line(line: str) -> None:
        if "Layer mesh" in line or "Doing final balancing" in line:
            prog("mesh", 16, "snappyHexMesh: adding boundary layers...")
    return on_line


def _solve_line(prog, iters: int, drag_factor: float, state: dict) -> Callable[[str], None]:
    """Parse simpleFoam stdout for iteration count, force trace and convergence."""
    def on_line(line: str) -> None:
        line = line.strip()
        m = _TIME_RE.match(line)
        if m:
            try:
                t = float(m.group(1))
                if not math.isfinite(t):
                    state["invalid_output"] = "solver emitted a non-finite iteration"
                    return
                state["last_time"] = t
                pct = 18 + int(min(72, 72 * t / max(iters, 1)))
                prog("solve", pct, f"simpleFoam iteration {int(t)}/{iters}")
            except ValueError:
                pass
            return
        c = _CD_RE.search(line)
        if c:
            try:
                cd = float(c.group(1))
                if not math.isfinite(cd):
                    state["invalid_output"] = "forceCoeffs emitted a non-finite Cd"
                    return
                prog("solve", -1, "", trace_point={"step": 0, "drag": cd * drag_factor})
            except ValueError:
                state["invalid_output"] = "forceCoeffs emitted an invalid Cd"
            return
        if _CONVERGED_RE.search(line):
            state["converged"] = True
    return on_line
