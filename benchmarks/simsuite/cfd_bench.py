"""simsuite bench — CFD: LBM fast path (D3Q19 kernel + doc wind tunnel) and the
OpenFOAM accurate path (case generation always; a capped live solve when foam
binaries exist and the run is not --quick).

Cases (domain "cfd"):
  lbm_poiseuille_duct     u_max/u_mean vs plane-Poiseuille 3/2 (Navier-Stokes
                          exact: u(z) = u_max*(1-(z/h)^2) => u_max/u_mean = 3/2,
                          White, Viscous Fluid Flow).
  lbm_sphere_oseen        sphere Cd vs Oseen Cd = 24/Re*(1+3Re/16) (White). The
                          LBM value sits above the unbounded-flow analytic
                          (~10% blockage + voxel staircase), recorded as-is.
  lbm_cube_mass_flux      mass-conservation defect |in-out|/in (steady
                          incompressible continuity => 0).
  lbm_convergence_N       drag vs step count on a fixed cube (timing/convergence
                          curve; regression-only).
  flow_doc_cube           end-to-end doc -> voxelize -> wind tunnel
                          (sim_run_flow, the MCP run_cfd_flow path).
  foam_case_gen           OpenFOAM aero case generation (deterministic; hashes
                          of two builds must match).
  foam_cht_case_gen       conjugate-heat-transfer case generation.
  foam_solve_sphere       live coarse simpleFoam solve capped at 150 iterations
                          (full mode + foam binaries only; skips otherwise).

Known limitation (recorded in detail fields, not asserted away): flow_run.cpp
clamps nu_lat >= cs^2/100, so high-Re cases run at an effective lattice Re of
O(100) and Cd is far above textbook — high-Re Cd cases are regression-only.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import resguard  # noqa: E402  (benchmarks/simsuite/resguard.py)

DOMAIN = "cfd"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _case(case: str, *, setup_s=0.0, mesh_s=0.0, solve_s=0.0, total_s=0.0,
          metric=None, ref=None, status="ok", detail="") -> dict:
    rel = None
    if ref not in (None, 0) and metric is not None:
        rel = abs(metric - ref) / abs(ref)
    return {"domain": DOMAIN, "case": case, "setup_s": round(setup_s, 3),
            "mesh_s": round(mesh_s, 3), "solve_s": round(solve_s, 3),
            "total_s": round(total_s, 3), "metric": metric, "ref": ref,
            "rel_err": rel, "status": status, "detail": detail}


def _fail(case: str, exc: Exception, t0: float) -> dict:
    return _case(case, total_s=time.time() - t0, status="fail",
                 detail=f"{type(exc).__name__}: {exc}")


def _idx(ny, nz):
    return lambda x, y, z: (x * ny + y) * nz + z


def _sim_lbm(cb, solid, nx, ny, nz, tau0, n_steps, u_lat=0.05, backend=None):
    dx_m = 1e-3
    dt_s = u_lat * dx_m / 10.0      # physical speed 10 m/s
    params = {"u_lat": u_lat, "tau0": tau0, "voxel_mm": 1.0,
              "dx_m": dx_m, "dt_s": dt_s, "rho_air": 1.225}
    # Additive backend select (auto|cpu|cuda); default leaves CPU unless env set.
    be = backend if backend is not None else os.environ.get("AI_CAD_LBM_BACKEND")
    if be:
        params["backend"] = be
    return cb.call("sim_lbm", {
        "mask": {"nx": nx, "ny": ny, "nz": nz, "solid": solid},
        "params": params,
        "n_steps": n_steps})


# ---------------------------------------------------------------------------
# LBM cases
# ---------------------------------------------------------------------------

def _bench_poiseuille(cb, quick: bool) -> dict:
    t0 = time.time()
    try:
        nx, ny, nz = (100, 5, 36) if quick else (120, 5, 36)
        x0, x1 = 20, nx - 20
        zw_lo, zw_hi = (10, 11), (24, 25)
        idx = _idx(ny, nz)
        solid = [0] * (nx * ny * nz)
        for x in range(x0, x1):
            for y in range(ny):
                for z in list(zw_lo) + list(zw_hi):
                    solid[idx(x, y, z)] = 1
        setup = time.time() - t0
        steps = 1200 if quick else 2000
        t1 = time.time()
        res = _sim_lbm(cb, solid, nx, ny, nz, tau0=1.0, n_steps=steps)
        solve = time.time() - t1
        if not res.get("ok") or res.get("diverged"):
            return _case("lbm_poiseuille_duct", setup_s=setup, solve_s=solve,
                         total_s=time.time() - t0, status="fail",
                         detail=res.get("error") or "diverged")
        ymid = ny // 2
        gap = range(zw_lo[1] + 1, zw_hi[0])
        prof = [res["ux"][idx(x1 - 10, ymid, z)] for z in gap]
        ratio = max(prof) / (sum(prof) / len(prof))
        ok = abs(ratio - 1.5) / 1.5 < 0.10
        # ref: plane Poiseuille u_max/u_mean = 3/2 (Navier-Stokes exact, White).
        return _case("lbm_poiseuille_duct", setup_s=setup, solve_s=solve,
                     total_s=time.time() - t0, metric=ratio, ref=1.5,
                     status="ok" if ok else "fail",
                     detail=f"u_max/u_mean, {steps} steps, gap 12 voxels")
    except Exception as exc:
        return _fail("lbm_poiseuille_duct", exc, t0)


def _bench_sphere_oseen(cb, quick: bool) -> dict:
    t0 = time.time()
    try:
        if quick:
            nx, ny, nz, R, steps = 64, 32, 32, 4.5, 1500
        else:
            nx, ny, nz, R, steps = 80, 40, 40, 5.5, 3000
        cx, cy, cz = nx * 0.3, (ny - 1) / 2.0, (nz - 1) / 2.0
        idx = _idx(ny, nz)
        solid = [0] * (nx * ny * nz)
        for x in range(nx):
            for y in range(ny):
                for z in range(nz):
                    if (x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2 <= R * R:
                        solid[idx(x, y, z)] = 1
        setup = time.time() - t0
        tau0 = 1.2
        nu = (tau0 - 0.5) / 3.0           # Chapman-Enskog nu = cs^2*(tau-1/2)
        re = 0.05 * (2 * R) / nu
        cd_oseen = 24.0 / re * (1.0 + 3.0 / 16.0 * re)  # Oseen (White, Viscous Fluid Flow)
        t1 = time.time()
        res = _sim_lbm(cb, solid, nx, ny, nz, tau0=tau0, n_steps=steps)
        solve = time.time() - t1
        if not res.get("ok") or res.get("diverged"):
            return _case("lbm_sphere_oseen", setup_s=setup, solve_s=solve,
                         total_s=time.time() - t0, status="fail",
                         detail=res.get("error") or "diverged")
        ratio = res["cd"] / cd_oseen
        ok = 0.8 < ratio < 3.0
        return _case("lbm_sphere_oseen", setup_s=setup, solve_s=solve,
                     total_s=time.time() - t0, metric=res["cd"], ref=cd_oseen,
                     status="ok" if ok else "fail",
                     detail=f"Re {re:.2f}; blockage+staircase raise Cd above the "
                            f"unbounded analytic (ratio {ratio:.2f})")
    except Exception as exc:
        return _fail("lbm_sphere_oseen", exc, t0)


def _cube_mask(nx, ny, nz, x0, side):
    idx = _idx(ny, nz)
    solid = [0] * (nx * ny * nz)
    yc, zc = (ny - side) // 2, (nz - side) // 2
    for x in range(x0, x0 + side):
        for y in range(yc, yc + side):
            for z in range(zc, zc + side):
                solid[idx(x, y, z)] = 1
    return solid


def _bench_cube_mass(cb, quick: bool) -> dict:
    t0 = time.time()
    try:
        nx, ny, nz = 60, 28, 28
        solid = _cube_mask(nx, ny, nz, 16, 7)
        setup = time.time() - t0
        steps = 1200 if quick else 2500
        t1 = time.time()
        res = _sim_lbm(cb, solid, nx, ny, nz, tau0=0.8, n_steps=steps)
        solve = time.time() - t1
        if not res.get("ok") or res.get("diverged") or res["drag_n"] <= 0.0:
            return _case("lbm_cube_mass_flux", setup_s=setup, solve_s=solve,
                         total_s=time.time() - t0, status="fail",
                         detail=res.get("error") or "diverged or non-positive drag")
        idx = _idx(ny, nz)

        def flux(x):
            return sum(res["rho"][idx(x, y, z)] * res["ux"][idx(x, y, z)]
                       for y in range(ny) for z in range(nz))
        f_in, f_out = flux(3), flux(nx - 4)
        defect = abs(f_in - f_out) / abs(f_in)
        # steady incompressible continuity: influx == outflux (defect -> 0)
        return _case("lbm_cube_mass_flux", setup_s=setup, solve_s=solve,
                     total_s=time.time() - t0, metric=defect, ref=None,
                     status="ok" if defect < 0.05 else "fail",
                     detail=f"|in-out|/in mass-flux defect; drag {res['drag_n']:.3g} N")
    except Exception as exc:
        return _fail("lbm_cube_mass_flux", exc, t0)


def _bench_convergence(cb, quick: bool) -> list[dict]:
    out = []
    step_list = (150, 300, 600) if quick else (300, 600, 1200, 2400)
    nx, ny, nz = 50, 24, 24
    solid = _cube_mask(nx, ny, nz, 14, 6)
    for n in step_list:
        t0 = time.time()
        try:
            res = _sim_lbm(cb, solid, nx, ny, nz, tau0=0.8, n_steps=n)
            ok = bool(res.get("ok")) and not res.get("diverged")
            out.append(_case(f"lbm_convergence_{n}", solve_s=time.time() - t0,
                             total_s=time.time() - t0,
                             metric=res.get("drag_n"), ref=None,
                             status="ok" if ok else "fail",
                             detail=f"cube drag after {n} steps (note: the sim_lbm "
                                    f"RPC does not emit residual/converged)"))
        except Exception as exc:
            out.append(_fail(f"lbm_convergence_{n}", exc, t0))
    return out


def _bench_flow_doc(cb, quick: bool) -> dict:
    t0 = time.time()
    doc = None
    try:
        doc = cb.call("doc_new", {"kind": "part", "name": "cfd_bench_box"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "B", "params": {
                "length": {"value": 40.0, "expr": None},
                "width": {"value": 40.0, "expr": None},
                "height": {"value": 40.0, "expr": None}}}}]})
        setup = time.time() - t0
        cells, steps = (48, 800) if quick else (64, 2000)
        t1 = time.time()
        res = cb.call("sim_run_flow", {"doc_id": doc, "velocity": 10.0,
                                       "cells": cells, "n_steps": steps})
        solve = time.time() - t1
        if not res.get("ok") or res.get("diverged") or res["drag_n"] <= 0.0:
            return _case("flow_doc_cube", setup_s=setup, solve_s=solve,
                         total_s=time.time() - t0, status="fail",
                         detail=res.get("error") or "diverged or non-positive drag")
        # Cd is regression-only here: the nu_lat clamp (flow_run.cpp) caps the
        # effective lattice Re at O(100), so the value is far above the
        # Re=2.7e4 textbook cube Cd (~1.05) by construction.
        return _case("flow_doc_cube", setup_s=setup, solve_s=solve,
                     total_s=time.time() - t0, metric=res["cd"], ref=None,
                     status="ok",
                     detail=f"cells {cells}, steps {steps}; drag {res['drag_n']:.3g} N; "
                            f"Cd regression-only (nu_lat clamp)")
    except Exception as exc:
        return _fail("flow_doc_cube", exc, t0)
    finally:
        if doc is not None:
            try:
                cb.call("doc_close", {"doc_id": doc})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# OpenFOAM cases
# ---------------------------------------------------------------------------

def _sphere_stl(r_mm: float, subdivisions: int = 2) -> str:
    import trimesh
    m = trimesh.creation.icosphere(subdivisions=subdivisions, radius=r_mm)
    p = Path(tempfile.mkdtemp(prefix="cfd_bench_")) / "sphere.stl"
    m.export(str(p))
    return str(p)


def _bench_foam_case_gen() -> dict:
    t0 = time.time()
    try:
        import hashlib
        from engine.sim import foam_case
        stl = _sphere_stl(50.0)
        setup = time.time() - t0

        def build():
            d = tempfile.mkdtemp(prefix="cfd_bench_case_")
            foam_case.build_case(d, stl, direction=(1, 0, 0), speed_mps=10.0,
                                 preset="coarse")
            return d

        t1 = time.time()
        d1 = build()
        gen = time.time() - t1
        d2 = build()
        h = [
            {str(f.relative_to(d)): hashlib.sha256(f.read_bytes()).hexdigest()
             for f in sorted(Path(d).rglob("*")) if f.is_file()}
            for d in (d1, d2)]
        deterministic = h[0] == h[1]
        return _case("foam_case_gen", setup_s=setup, solve_s=gen,
                     total_s=time.time() - t0, metric=float(len(h[0])), ref=None,
                     status="ok" if deterministic else "fail",
                     detail="case files written (metric=count); "
                            + ("deterministic" if deterministic
                               else "NON-DETERMINISTIC rebuild"))
    except Exception as exc:
        return _fail("foam_case_gen", exc, t0)


def _bench_foam_cht_case_gen() -> dict:
    t0 = time.time()
    try:
        from engine.sim import foam_cht
        stl = _sphere_stl(50.0)
        setup = time.time() - t0
        d = tempfile.mkdtemp(prefix="cfd_bench_cht_")
        t1 = time.time()
        info = foam_cht.build_cht_case(d, stl, speed_mps=5.0, preset="coarse",
                                       solid_kappa=200.0, power_w=20.0)
        gen = time.time() - t1
        nfiles = sum(1 for f in Path(d).rglob("*") if f.is_file())
        ok = info.conjugate and info.app == "chtMultiRegionFoam" and nfiles > 10
        return _case("foam_cht_case_gen", setup_s=setup, solve_s=gen,
                     total_s=time.time() - t0, metric=float(nfiles), ref=None,
                     status="ok" if ok else "fail",
                     detail=f"app {info.app}; case files (metric=count)")
    except Exception as exc:
        return _fail("foam_cht_case_gen", exc, t0)


def _bench_foam_solve(quick: bool) -> dict:
    t0 = time.time()
    try:
        from engine.sim.foam_runner import get_runner
        runner = get_runner()
        if runner is None or not runner.available():
            return _case("foam_solve_sphere", total_s=time.time() - t0,
                         status="skip", detail="OpenFOAM binaries not installed")
        if quick:
            return _case("foam_solve_sphere", total_s=time.time() - t0,
                         status="skip",
                         detail="quick mode: live foam solve exceeds the smoke budget")
        if not resguard.fits(est_peak_gb=2.0):
            return _case("foam_solve_sphere", total_s=time.time() - t0,
                         status="skip", detail="resguard: needs ~2 GB")
        from engine.sim.openfoam import run_openfoam_tunnel
        stl = _sphere_stl(50.0)
        setup = time.time() - t0
        t1 = time.time()
        # max_steps caps simpleFoam's endTime (see foam_case.build_case max_iters)
        # so this is a bounded smoke solve, not a converged Cd study.
        res = run_openfoam_tunnel(stl, speed_mps=10.0, preset="coarse",
                                  max_steps=150)
        solve = time.time() - t1
        # A 150-iter capped run is not converged by design, so the result's
        # converged/diverged flag is informational; ok = finite positive forces.
        ok = math.isfinite(res.cd) and res.cd > 0.0 and res.drag_n > 0.0
        return _case("foam_solve_sphere", setup_s=setup, solve_s=solve,
                     total_s=time.time() - t0, metric=res.cd, ref=None,
                     status="ok" if ok else "fail",
                     detail=f"{getattr(runner, 'version_str', '')}; 150-iter capped "
                            f"coarse RANS (unconverged by design); "
                            f"drag {res.drag_n:.3g} N (regression-only)")
    except Exception as exc:
        return _fail("foam_solve_sphere", exc, t0)


# ---------------------------------------------------------------------------
# contract entry point
# ---------------------------------------------------------------------------

def run(quick: bool = False) -> list[dict]:
    resguard.install()
    os.environ.setdefault("AI_CAD_ENGINE_DAEMON", "0")
    from services.mcp import core_bridge as cb

    cases: list[dict] = []
    cases.append(_bench_poiseuille(cb, quick))
    resguard.check()
    cases.append(_bench_sphere_oseen(cb, quick))
    resguard.check()
    cases.append(_bench_cube_mass(cb, quick))
    cases.extend(_bench_convergence(cb, quick))
    resguard.check()
    cases.append(_bench_flow_doc(cb, quick))
    cases.append(_bench_foam_case_gen())
    cases.append(_bench_foam_cht_case_gen())
    resguard.check()
    cases.append(_bench_foam_solve(quick))
    return cases


if __name__ == "__main__":
    import json
    print(json.dumps(run(quick="--quick" in sys.argv), indent=2))
