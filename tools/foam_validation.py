"""Validate the OpenFOAM CFD bridge against canonical cases with known drag.

This is what turns "the solver runs" into "the numbers can be trusted": each case has a
literature drag coefficient and an accepted band; the run PASSES if the computed Cd lands in
the band.  Bands are intentionally generous — RANS on an automatically-generated mesh is a
sanity/regression gate, not a certification, and bluff-body separation (sphere) is genuinely
hard for steady RANS.

Run (needs a provisioned OpenFOAM — see tools/provision_openfoam.py):

    python -m tools.foam_validation                 # sphere + cube at the default preset
    python -m tools.foam_validation --preset fine   # finer mesh (slower, more accurate)
    python -m tools.foam_validation --case sphere

References:
  * Sphere, subcritical 1e3 < Re < 2e5:  Cd ~ 0.4-0.5  (Schlichting; Munson).
  * Cube, face-on, Re ~ 1e4:             Cd ~ 1.05      (Hoerner, Fluid-Dynamic Drag).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time

import trimesh


def _sphere_stl(d_mm=100.0):
    p = os.path.join(tempfile.mkdtemp(), "sphere.stl")
    trimesh.creation.icosphere(subdivisions=3, radius=d_mm / 2).export(p)
    return p, math.pi * (d_mm / 2) ** 2  # auto ref area (frontal)


def _cube_stl(side_mm=100.0):
    p = os.path.join(tempfile.mkdtemp(), "cube.stl")
    trimesh.creation.box(extents=(side_mm, side_mm, side_mm)).export(p)
    return p, side_mm * side_mm  # face area


# name -> (stl factory, speed m/s, expected Cd, accepted band, ref-area override or 0=auto, note)
#
# IMPORTANT — read the bands as *steady-RANS* expectations, not experiment:
#   * Sphere (mildly separated): steady k-omega SST lands a touch low (~0.35-0.45) vs the
#     experimental subcritical ~0.47 — acceptable.
#   * Cube (sharp-edged bluff body): the real wake sheds vortices and is strongly unsteady;
#     steady RANS time-averages to an over-low base pressure and so OVER-predicts drag
#     (commonly Cd ~1.4-2.0 vs the experimental ~1.05). That is a known model limitation, not
#     a solver bug — for sharp bluff bodies prefer an unsteady/LES run. The band reflects the
#     steady-RANS reality so the gate is honest about what this engine does and doesn't nail.
_CASES = {
    # d=100mm @1.5 m/s air -> Re ~ 1e4
    "sphere": (lambda: _sphere_stl(100.0), 1.5, 0.47, (0.25, 0.55), 0.0,
               "experimental ~0.47; steady RANS lands slightly low"),
    # side=100mm @1.5 m/s, ref area = face. Experimental ~1.05, but steady RANS over-predicts.
    "cube":   (lambda: _cube_stl(100.0), 1.5, 1.05, (1.20, 2.20), 100.0 * 100.0,
               "experimental ~1.05; steady RANS over-predicts sharp bluff-body drag (use LES)"),
}


def run_case(name: str, preset: str) -> dict:
    from services.sim.openfoam import run_openfoam_tunnel

    factory, speed, cd_ref, band, ref_area, note = _CASES[name]
    stl, auto_area = factory()
    ref_mm2 = ref_area or auto_area
    t0 = time.time()
    r = run_openfoam_tunnel(stl, direction=(1, 0, 0), speed_mps=speed, preset=preset,
                            ref_area_mm2=ref_mm2)
    dt = time.time() - t0
    ok = band[0] <= r.cd <= band[1]
    return {"name": name, "cd": r.cd, "cd_ref": cd_ref, "band": band, "re": r.reynolds,
            "drag_n": r.drag_n, "pass": ok, "secs": dt, "note": note}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate the OpenFOAM CFD bridge.")
    ap.add_argument("--preset", default="medium", choices=["coarse", "medium", "fine", "xfine"])
    ap.add_argument("--case", default="all", choices=["all", *_CASES])
    args = ap.parse_args(argv)

    try:
        from services.sim.foam_runner import get_runner
        if get_runner() is None:
            print("[validation] No OpenFOAM available. Provision it with "
                  "`python -m tools.provision_openfoam --install`.")
            return 2
    except Exception as e:  # noqa: BLE001
        print(f"[validation] could not probe OpenFOAM: {e}")
        return 2

    cases = list(_CASES) if args.case == "all" else [args.case]
    print(f"[validation] preset={args.preset}  cases={cases}\n")
    print(f"{'case':8} {'Re':>9} {'Cd':>8} {'ref':>6} {'band':>14} {'drag(N)':>9} {'s':>5}  result")
    print("-" * 72)
    results = []
    for name in cases:
        try:
            res = run_case(name, args.preset)
        except Exception as e:  # noqa: BLE001
            print(f"{name:8} {'ERROR':>9}  {e}")
            results.append({"name": name, "pass": False})
            continue
        results.append(res)
        print(f"{res['name']:8} {res['re']:9.0f} {res['cd']:8.3f} {res['cd_ref']:6.2f} "
              f"[{res['band'][0]:.2f},{res['band'][1]:.2f}]{'':3} {res['drag_n']:9.4f} "
              f"{res['secs']:5.0f}  {'PASS' if res['pass'] else 'FAIL'}")
        print(f"{'':8} note: {res['note']}")

    n_pass = sum(1 for r in results if r.get("pass"))
    print("-" * 72)
    print(f"[validation] {n_pass}/{len(results)} passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
