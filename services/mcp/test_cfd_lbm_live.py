"""CFD fast-path (D3Q19 LBM) physics regression tests — live in-process engine.

Drives core/sim/lbm.cpp + core/sim/flow_run.cpp through the raw core bridge
(``sim_lbm`` = pure kernel on a caller-supplied solid mask, ``sim_run_flow`` =
doc geometry -> voxelize -> wind tunnel), the same RPCs MCP run_cfd_flow uses.

Physics anchors (each cites its formula — no constants from memory):

* Plane-Poiseuille profile: for fully developed laminar flow between parallel
  plates the exact Navier-Stokes solution is u(z) = u_max*(1 - (z/h)^2), so
  u_max / u_mean = 3/2 (White, Viscous Fluid Flow, ch. 3 exact solutions).
  A finite parallel-plate duct is placed inside the open wind-tunnel domain
  (a sealed duct cannot reach steady state with this kernel's BC set — the
  fixed-equilibrium inlet plus copy outlet over-pressurizes a fully blocked
  section; see the sealed-channel note below).
* Low-Re sphere drag: Oseen's correction to Stokes drag gives
  Cd = 24/Re * (1 + 3*Re/16) (White, Viscous Fluid Flow, low-Re solutions).
  The measured Cd sits ABOVE that (finite ~10% blockage raises drag at low Re,
  and the staircase voxel sphere is not smooth) but must stay within a small
  multiple — this pins the momentum-exchange force scaling.
* Bluff body: nonzero drag, near-zero lift (symmetric), and mass conservation
  (rho*ux flux in == flux out within a few %, steady incompressible continuity).

KNOWN LIMITATION (documented, not asserted away): flow_run.cpp clamps the
lattice viscosity (nu_lat >= cs^2/100), so high-Reynolds cases run at an
effective lattice Re of O(100) and Cd comes out an order of magnitude above
textbook values. These tests therefore anchor the kernel in the low-Re regime
it can actually resolve and treat high-Re Cd as regression-only.

Run: CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest \
         services/mcp/test_cfd_lbm_live.py -v
"""
from __future__ import annotations

import math
import os
import sys

import pytest

os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp.core_bridge import CoreError  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _idx(ny: int, nz: int):
    def f(x: int, y: int, z: int) -> int:
        return (x * ny + y) * nz + z
    return f


def _sim_lbm(solid, nx, ny, nz, tau0, n_steps, u_lat=0.05):
    """Raw kernel call. dx/dt chosen so the physical speed is 10 m/s."""
    dx_m = 1e-3
    dt_s = u_lat * dx_m / 10.0
    return cb.call("sim_lbm", {
        "mask": {"nx": nx, "ny": ny, "nz": nz, "solid": solid},
        "params": {"u_lat": u_lat, "tau0": tau0, "voxel_mm": 1.0,
                   "dx_m": dx_m, "dt_s": dt_s, "rho_air": 1.225},
        "n_steps": n_steps})


def _skip_unless(res: dict):
    if not res.get("ok"):
        pytest.fail(f"sim_lbm failed: {res.get('error')}")


def _mass_flux(res: dict, x: int) -> float:
    """Integral of rho*ux over the (y,z) plane at station x (lattice units)."""
    ny, nz = res["ny"], res["nz"]
    ux, rho = res["ux"], res["rho"]
    idx = _idx(ny, nz)
    return sum(rho[idx(x, y, z)] * ux[idx(x, y, z)]
               for y in range(ny) for z in range(nz))


# ---------------------------------------------------------------------------
# 1. Plane-Poiseuille profile in a finite open-domain duct
# ---------------------------------------------------------------------------

def test_poiseuille_duct_profile():
    nx, ny, nz = 120, 5, 36
    x0, x1 = 20, 100                # duct plates span x=[20,100)
    zw_lo, zw_hi = (10, 11), (24, 25)   # plate cells; fluid gap z=12..23
    solid = [0] * (nx * ny * nz)
    idx = _idx(ny, nz)
    for x in range(x0, x1):
        for y in range(ny):
            for z in list(zw_lo) + list(zw_hi):
                solid[idx(x, y, z)] = 1

    # tau=1.0 -> nu = cs^2*(tau-1/2) = 1/6 (Chapman-Enskog), gap H=12,
    # Re = u*H/nu ~ 3.6: deep-laminar, entry length << duct length.
    res = _sim_lbm(solid, nx, ny, nz, tau0=1.0, n_steps=1500)
    _skip_unless(res)
    assert not res["diverged"]

    ymid = ny // 2
    xs = x1 - 10                    # sample near the duct exit (fully developed)
    gap = range(zw_lo[1] + 1, zw_hi[0])
    prof = [res["ux"][idx(xs, ymid, z)] for z in gap]
    assert all(math.isfinite(v) for v in prof)
    assert min(prof) > 0.0, "duct flow should be everywhere downstream"

    umax = max(prof)
    umean = sum(prof) / len(prof)
    ratio = umax / umean
    # Plane Poiseuille (Navier-Stokes exact): u_max/u_mean = 3/2.
    # Discrete cell-center sampling of the parabola over 12 cells biases the
    # ratio slightly low; 10% covers that plus residual entry effects.
    assert abs(ratio - 1.5) / 1.5 < 0.10, f"u_max/u_mean {ratio:.3f} != 1.5"

    # Profile-shape L2 error vs the parabola u_max*(1-((z-zc)/h)^2) with the
    # half-way bounce-back wall (wall plane sits half a cell into the plates).
    zc = (zw_lo[1] + zw_hi[0]) / 2.0
    halfw = (zw_hi[0] - zw_lo[1]) / 2.0
    l2n = l2d = 0.0
    for i, z in enumerate(gap):
        ua = umax * (1.0 - ((z - zc) / halfw) ** 2)
        l2n += (prof[i] - ua) ** 2
        l2d += ua * ua
    l2 = math.sqrt(l2n / l2d)
    assert l2 < 0.15, f"profile L2 error vs parabola {l2:.3f} >= 0.15"

    # Symmetry: the profile about the duct centerline (pairs off z and its mirror).
    n = len(prof)
    for i in range(n // 2):
        a, b = prof[i], prof[n - 1 - i]
        assert abs(a - b) <= 0.15 * max(abs(a), abs(b), 1e-12)


# ---------------------------------------------------------------------------
# 2. Bluff cube: drag sign, lift symmetry, mass conservation
# ---------------------------------------------------------------------------

def test_bluff_cube_drag_lift_mass():
    nx, ny, nz = 60, 28, 28
    solid = [0] * (nx * ny * nz)
    idx = _idx(ny, nz)
    for x in range(16, 23):
        for y in range(11, 18):
            for z in range(11, 18):
                solid[idx(x, y, z)] = 1

    res = _sim_lbm(solid, nx, ny, nz, tau0=0.8, n_steps=1500)
    _skip_unless(res)
    assert not res["diverged"]
    assert res["drag_n"] > 0.0, "bluff body must produce positive drag"
    # Symmetric body/domain -> lift ~ 0 relative to drag.
    assert abs(res["lift_n"]) < 0.05 * res["drag_n"]
    # Frontal area = 7x7 voxels * (1 mm)^2.
    assert res["frontal_area_mm2"] == pytest.approx(49.0)

    # Steady incompressible continuity: mass flux in == out within a few %.
    f_in = _mass_flux(res, 3)
    f_out = _mass_flux(res, nx - 4)
    assert f_in > 0.0
    assert abs(f_in - f_out) / f_in < 0.05, \
        f"mass conservation violated: in {f_in:.4f} out {f_out:.4f}"


# ---------------------------------------------------------------------------
# 3. Low-Re sphere vs Oseen drag (force-scaling anchor)
# ---------------------------------------------------------------------------

def test_sphere_low_re_oseen_anchor():
    nx, ny, nz = 64, 32, 32
    cx, cy, cz, R = 19.5, 15.5, 15.5, 4.5
    solid = [0] * (nx * ny * nz)
    idx = _idx(ny, nz)
    for x in range(nx):
        for y in range(ny):
            for z in range(nz):
                if (x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2 <= R * R:
                    solid[idx(x, y, z)] = 1

    tau0 = 1.2
    nu = (tau0 - 0.5) / 3.0         # Chapman-Enskog: nu = cs^2*(tau-1/2), cs^2=1/3
    re = 0.05 * (2 * R) / nu
    res = _sim_lbm(solid, nx, ny, nz, tau0=tau0, n_steps=1800)
    _skip_unless(res)
    assert not res["diverged"]

    # Oseen: Cd = 24/Re * (1 + 3*Re/16) (White, Viscous Fluid Flow).
    cd_oseen = 24.0 / re * (1.0 + 3.0 / 16.0 * re)
    ratio = res["cd"] / cd_oseen
    # Unbounded-flow analytic; ~10% frontal blockage and the voxel staircase
    # raise the measured drag, so the ratio sits above 1 but must stay small.
    assert 0.8 < ratio < 3.0, \
        f"Cd {res['cd']:.2f} vs Oseen {cd_oseen:.2f} (Re {re:.2f}): ratio {ratio:.2f}"


# ---------------------------------------------------------------------------
# 4. Determinism + input validation
# ---------------------------------------------------------------------------

def test_lbm_determinism_bit_exact():
    nx, ny, nz = 24, 10, 10
    solid = [0] * (nx * ny * nz)
    idx = _idx(ny, nz)
    for x in range(8, 12):
        for y in range(3, 7):
            for z in range(3, 7):
                solid[idx(x, y, z)] = 1
    a = _sim_lbm(solid, nx, ny, nz, tau0=0.9, n_steps=120)
    b = _sim_lbm(solid, nx, ny, nz, tau0=0.9, n_steps=120)
    _skip_unless(a)
    _skip_unless(b)
    assert a["drag_n"] == b["drag_n"]       # bit-exact (float32 kernel, fixed order)
    assert a["ux"] == b["ux"]


def test_lbm_bad_mask_clean_error():
    try:
        res = cb.call("sim_lbm", {"mask": {"nx": 8, "ny": 8, "nz": 8, "solid": [0, 0, 0]},
                                  "params": {}, "n_steps": 5})
    except CoreError as exc:
        assert "mismatch" in str(exc) or "solid" in str(exc)
        return
    assert res.get("ok") is False
    assert "mismatch" in (res.get("error") or "")


# ---------------------------------------------------------------------------
# 5. End-to-end doc -> voxelize -> wind tunnel (sim_run_flow, the MCP path)
# ---------------------------------------------------------------------------

def test_flow_doc_cube_end_to_end():
    doc = cb.call("doc_new", {"kind": "part", "name": "cfd_suite_box"})["doc_id"]
    try:
        r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "B", "params": {
                "length": {"value": 40.0, "expr": None},
                "width": {"value": 40.0, "expr": None},
                "height": {"value": 40.0, "expr": None}}}}]})
        fs = (r.get("rebuild") or {}).get("feature_status", [])
        assert fs and all(f.get("ok") for f in fs), "box rebuild failed"

        try:
            res = cb.call("sim_run_flow", {"doc_id": doc, "velocity": 10.0,
                                           "cells": 48, "n_steps": 800})
        except CoreError as exc:
            if "unknown method" in str(exc):
                pytest.skip(f"build lacks sim_run_flow: {exc}")
            raise
        assert res.get("ok"), res.get("error")
        assert not res["diverged"]
        assert res["drag_n"] > 0.0
        assert abs(res["lift_n"]) < 0.05 * res["drag_n"]   # symmetric cube
        assert abs(res["side_n"]) < 0.05 * res["drag_n"]
        # Voxelized frontal area vs the true 40x40 mm face: staircase quantization
        # at ~4.2 mm pitch allows ~15%.
        assert abs(res["frontal_area_mm2"] - 1600.0) / 1600.0 < 0.15
        # Reynolds = U*L/nu (definition): 10 m/s * 0.04 m / 1.5e-5 m^2/s.
        assert res["reynolds"] == pytest.approx(10.0 * 0.040 / 1.5e-5, rel=1e-6)
        assert res["ref_length_mm"] == pytest.approx(40.0, rel=1e-6)
        assert math.isfinite(res["cd"]) and res["cd"] > 0.0
        assert 0 < res["steps_run"] <= 800
        assert res["converged"]
        assert "effective lattice Re" in res["warning"]
        assert res["mass_imbalance"] < 0.02
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. Sealed duct: documented BC limitation stays documented
# ---------------------------------------------------------------------------

def test_sealed_channel_stagnates_not_crashes():
    """A channel whose walls seal the lateral faces cannot vent mass with the
    equilibrium-inlet + copy-outlet BC set: the domain over-pressurizes and the
    flow decays toward stagnation instead of reaching Poiseuille. This test
    pins the DOCUMENTED behavior: the solve must stay finite and conservative
    per-plane, never NaN/diverge. If a future BC fix makes the sealed channel
    actually flow, the (dead) stagnation assertion below flags it so the
    open-duct test above can be tightened."""
    nx, ny, nz, wall = 60, 5, 20, 2
    solid = [0] * (nx * ny * nz)
    idx = _idx(ny, nz)
    for x in range(nx):
        for y in range(ny):
            for z in list(range(wall)) + list(range(nz - wall, nz)):
                solid[idx(x, y, z)] = 1
    res = _sim_lbm(solid, nx, ny, nz, tau0=1.0, n_steps=2000)
    _skip_unless(res)
    assert not res["diverged"]
    assert all(math.isfinite(v) for v in res["ux"])
    # Mid-channel speed has decayed far below the 0.05 inlet (stagnation).
    mid = res["ux"][idx(nx // 2, ny // 2, nz // 2)]
    assert abs(mid) < 0.02, (
        "sealed channel now sustains flow — BC behavior changed; revisit the "
        "open-duct Poiseuille tolerances and this documented limitation")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
