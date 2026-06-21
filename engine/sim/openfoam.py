"""OpenFOAM external-aero RANS bridge — a drop-in alternative to the LBM wind tunnel.

``run_openfoam_tunnel`` has the *same signature and return type* as
``engine.sim.lbm.run_wind_tunnel`` so the sim worker can dispatch to either engine behind one
``sim_run_flow`` RPC.  It:

  1. builds an OpenFOAM case from the part STL (engine.sim.foam_case),
  2. meshes it (blockMesh -> surfaceFeatureExtract -> snappyHexMesh) and solves steady
     incompressible RANS (simpleFoam, k-omega SST) via the active runner
     (engine.sim.foam_runner: native Windows OpenFOAM preferred, WSL fallback),
  3. streams solver residuals/forces to ``on_progress`` for the live UI, and
  4. post-processes the result into a FlowResult with a ready-made viewport overlay
     (engine.sim.foam_post).

Requires a provisioned OpenFOAM (see tools/provision_openfoam.py).  Raises RuntimeError with a
clear message if none is reachable, which the worker surfaces as a structured error.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from engine.sim.foam_case import build_case
from engine.sim.foam_runner import FoamError, get_runner

_TIME_RE = re.compile(r"^Time = ([0-9.eE+-]+)")
_CD_RE = re.compile(r"Cd:\s+([0-9.eE+-]+)")
_CONVERGED_RE = re.compile(r"SIMPLE solution converged")


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
    wall thermal BC — see engine.sim.foam_case.build_case.
    """
    runner = get_runner()
    if runner is None or not runner.available():
        raise RuntimeError(
            "OpenFOAM is not available. Provision it with "
            "`python -m tools.provision_openfoam --install` (WSL) or install blueCFD-Core "
            "natively and set %AICAD_OPENFOAM%."
        )

    def prog(stage: str, pct: int, text: str, trace_point: Optional[dict] = None) -> None:
        if on_progress is None:
            return
        data = {"job_id": "openfoam", "stage": stage, "pct": int(pct), "text": text}
        if trace_point is not None:
            data["trace_point"] = trace_point
        on_progress(data)

    case_dir = tempfile.mkdtemp(prefix="aicad_foam_")
    try:
        kind = "thermal" if (thermal and thermal.get("enabled")) else "aero"
        prog("mesh", 2, f"Building OpenFOAM {kind} case ({preset})...")
        info = build_case(case_dir, stl_path, direction=direction, speed_mps=speed_mps,
                          preset=preset, aoa_deg=aoa_deg, sideslip_deg=sideslip_deg,
                          fluid=fluid, ref_area_mm2=ref_area_mm2, n_proc=1, thermal=thermal)

        # ---- mesh ----
        prog("mesh", 6, "blockMesh (background domain)...")
        runner.run(case_dir, "blockMesh", on_line=None)
        prog("mesh", 10, "surfaceFeatureExtract (edges)...")
        try:
            runner.run(case_dir, "surfaceFeatureExtract", on_line=None)
        except FoamError:
            pass  # smooth bodies have no sharp features; snappy still meshes
        prog("mesh", 14, "snappyHexMesh (body fit + boundary layers)...")
        runner.run(case_dir, "snappyHexMesh", ["-overwrite"], on_line=_mesh_line(prog))

        # ---- solve ----
        q = 0.5 * info.rho_kgm3 * info.speed_mps ** 2
        aref_m2 = max(info.ref_area_mm2 * 1e-6, 1e-9)
        state = {"converged": False}
        prog("solve", 18, f"{info.app} ({'buoyant RANS' if info.thermal else 'RANS'}, {info.iters} iters)...")
        runner.run(case_dir, info.app,
                   on_line=_solve_line(prog, info.iters, q * aref_m2, state),
                   timeout=3300)

        prog("post", 92, "Post-processing (forces, Cp, streamlines)...")
        from engine.sim.foam_post import postprocess
        result = postprocess(case_dir, stl_path, info, converged=state["converged"])
        prog("post", 100, f"Done: Cd {result.cd:.3f}, Cl {result.cl:.3f}")
        return result
    finally:
        # Keep the case on disk only if explicitly requested for debugging.
        import os
        if not os.environ.get("AICAD_FOAM_KEEP"):
            shutil.rmtree(case_dir, ignore_errors=True)


def _mesh_line(prog) -> Callable[[str], None]:
    def on_line(line: str) -> None:
        if "Layer mesh" in line or "Doing final balancing" in line:
            prog("mesh", 16, "snappyHexMesh: adding boundary layers...")
    return on_line


def _solve_line(prog, iters: int, drag_factor: float, state: dict) -> Callable[[str], None]:
    """Parse simpleFoam stdout for iteration count (-> pct) and Cd (-> live drag trace)."""
    def on_line(line: str) -> None:
        line = line.strip()
        m = _TIME_RE.match(line)
        if m:
            try:
                t = float(m.group(1))
                pct = 18 + int(min(72, 72 * t / max(iters, 1)))
                prog("solve", pct, f"simpleFoam iteration {int(t)}/{iters}")
            except ValueError:
                pass
            return
        c = _CD_RE.search(line)
        if c:
            try:
                cd = float(c.group(1))
                prog("solve", -1, "", trace_point={"step": 0, "drag": cd * drag_factor})
            except ValueError:
                pass
            return
        if _CONVERGED_RE.search(line):
            state["converged"] = True
    return on_line
