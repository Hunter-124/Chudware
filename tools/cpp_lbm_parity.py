"""cpp_lbm_parity.py — Python-vs-C++ D3Q19 LBM wind-tunnel kernel parity.

Drives the SAME precomputed solid mask + lattice params + fixed step count through
the Python ORACLE (engine.sim.lbm kernel helpers, run in run_wind_tunnel's exact
loop order) and the C++ core (libaicad.dll: sim_lbm), and compares the macroscopic
fields (ux/uy/uz/rho) + the drag coefficient Cd + frontal area.

The trimesh voxelization is deliberately OUT of scope: both engines get the IDENTICAL
mask, so this isolates the lattice numerics (the eventual CUDA payload). The Python
reference IS the oracle — it calls the actual lbm.py helpers (_feq_fast, _stream,
_smag_tau, _macros, _inlet_bc, _lateral_slip, _bounce_back, _build_boundary_links,
_momentum_exchange) in the SAME order run_wind_tunnel's time loop uses, for the SAME
N steps, with NO watchdog / convergence early-exit (the masks + N are chosen to stay
below divergence so both engines walk an identical, deterministic loop).

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_lbm_parity.py

Exit code = number of mismatched checks (0 = full parity).

TOLERANCE. The C++ core mirrors numpy's float32 state AND numpy's exact float32
pairwise_sum reduction order in every place the oracle reduces (_macros' rho +
momentum sums, _smag_tau's pi_mag, _momentum_exchange's np.sum), so parity is
BIT-EXACT: fields and Cd both agree to better than 1e-9 relative across all cases
(measured residual is ~1e-12, i.e. the float64 JSON round-trip floor, NOT structured
algorithmic error). We gate fields + Cd at 1e-9 relative — a tight bound that a real
algorithmic mismatch (wrong roll sign, wrong direction pairing, float64-vs-float32
collision) would blow past by many orders of magnitude.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- masks
def box_mask(nx, ny, nz, x0, x1, y0, y1, z0, z1):
    """Solid axis-aligned box [x0:x1, y0:y1, z0:z1) in an (nx,ny,nz) grid."""
    m = np.zeros((nx, ny, nz), dtype=bool)
    m[x0:x1, y0:y1, z0:z1] = True
    return m


def sphere_mask(nx, ny, nz, cx, cy, cz, r):
    """Solid sphere of radius r centred at (cx,cy,cz)."""
    xs = np.arange(nx)[:, None, None]
    ys = np.arange(ny)[None, :, None]
    zs = np.arange(nz)[None, None, :]
    return ((xs - cx) ** 2 + (ys - cy) ** 2 + (zs - cz) ** 2) <= (r * r)


# --------------------------------------------------------------------------- oracle
def python_reference(solid_mask, params, n_steps):
    """Run the lbm.py kernel helpers in run_wind_tunnel's loop order for n_steps.

    Returns (rho, ux, uy, uz, cd, drag_n, lift_n, frontal_area_mm2) — the same
    post-processing scalars/fields the C++ core reports. NO watchdog / convergence
    early-exit (fixed N below divergence).
    """
    import engine.sim.lbm as L

    Nx, Ny, Nz = solid_mask.shape
    u_lat = np.float32(params["u_lat"])
    tau0 = float(params["tau0"])

    rho = np.ones((Nx, Ny, Nz), dtype=np.float32)
    ux = np.full((Nx, Ny, Nz), u_lat, dtype=np.float32)
    uy = np.zeros((Nx, Ny, Nz), dtype=np.float32)
    uz = np.zeros((Nx, Ny, Nz), dtype=np.float32)

    f = L._feq_fast(rho, ux, uy, uz)
    f[solid_mask] = 0.0

    bound_links = L._build_boundary_links(solid_mask, Nx, Ny, Nz)
    tau_current = tau0

    for _step in range(1, n_steps + 1):
        # --- Collision (BGK + Smagorinsky) ---
        rho, ux, uy, uz = L._macros(f)
        feq = L._feq_fast(rho, ux, uy, uz)
        if tau_current > 0.55:
            tau_arr = L._smag_tau(f, feq, tau_current)
            omega = np.float32(1.0) / tau_arr
            f = f - omega[..., np.newaxis] * (f - feq)
        else:
            omega_s = np.float32(1.0 / tau_current)
            f = f - omega_s * (f - feq)
        f = f.astype(np.float32)

        # --- Streaming ---
        f = L._stream(f)

        # --- Boundary conditions ---
        L._inlet_bc(f, u_lat)
        f[-1, :, :, :] = f[-2, :, :, :]
        L._lateral_slip(f, Ny, Nz)
        L._bounce_back(f, solid_mask)
        f[solid_mask] = 0.0
        # NOTE: no watchdog / convergence — fixed N, deterministic.

    # --- Post-processing (mirrors run_wind_tunnel) ---
    rho_f, ux_f, uy_f, uz_f = L._macros(f)
    drag_lat_f, lift_lat_f = L._momentum_exchange(f, bound_links)

    dx_m = params["dx_m"]
    dt_s = params["dt_s"]
    rho_air = params["rho_air"]
    voxel_mm = params["voxel_mm"]
    force_factor = float(rho_air * dx_m ** 3 / dt_s)
    drag_n = float(drag_lat_f * force_factor)
    lift_n = float(lift_lat_f * force_factor)

    frontal_mask = solid_mask.any(axis=0)
    frontal_area_mm2 = float(frontal_mask.sum()) * (voxel_mm ** 2)
    frontal_area_m2 = frontal_area_mm2 * 1e-6
    speed_mps = float(u_lat) * dx_m / dt_s  # dt = u_lat*dx/speed
    dyn_pressure = 0.5 * rho_air * speed_mps ** 2
    if frontal_area_m2 > 0.0 and dyn_pressure > 0.0:
        cd = drag_n / (dyn_pressure * frontal_area_m2)
    else:
        cd = 0.0

    return rho_f, ux_f, uy_f, uz_f, cd, drag_n, lift_n, frontal_area_mm2


# --------------------------------------------------------------------------- main
def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[lbm-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[lbm-parity] libaicad ABI={core.abi()}")

    # Fixed deterministic param set (a plausible run_wind_tunnel scaling). tau0 above
    # 0.55 exercises the FULL Smagorinsky branch (the harder path to match).
    params = {
        "u_lat": 0.05,
        "tau0": 0.8,
        "voxel_mm": 5.0,
        "dx_m": 5.0e-3,
        "dt_s": 2.5e-5,  # = u_lat*dx/speed with speed=10 m/s
        "rho_air": 1.225,
    }
    # A second param set on the simple-BGK branch (tau0 <= 0.55) for coverage.
    params_bgk = dict(params, tau0=0.52)

    FIELD_TOL = 1e-9  # bit-exact float32 pairwise-sum parity (residual ~1e-12)
    CD_TOL = 1e-9     # bit-exact on Cd too

    cases = []
    # (label, mask, params, n_steps)
    cases.append(("box 24x16x16 N100",
                  box_mask(24, 16, 16, 8, 12, 6, 10, 6, 10), params, 100))
    cases.append(("box 24x16x16 N200",
                  box_mask(24, 16, 16, 8, 12, 6, 10, 6, 10), params, 200))
    cases.append(("sphere 24x16x16 N150",
                  sphere_mask(24, 16, 16, 10, 8, 8, 3.0), params, 150))
    cases.append(("box bgk N150",
                  box_mask(20, 14, 14, 7, 11, 5, 9, 5, 9), params_bgk, 150))
    cases.append(("sphere bgk N100",
                  sphere_mask(20, 14, 14, 8, 7, 7, 2.5), params_bgk, 100))
    cases.append(("thin plate N120",
                  box_mask(24, 16, 16, 10, 12, 3, 13, 3, 13), params, 120))

    mism = 0
    checks = 0
    for label, mask, prm, n_steps in cases:
        checks += 1
        Nx, Ny, Nz = mask.shape
        rho_p, ux_p, uy_p, uz_p, cd_p, drag_p, lift_p, fa_p = python_reference(
            mask, prm, n_steps)

        solid_flat = mask.astype(np.int32).ravel(order="C").tolist()
        rc, cpp = core.call("sim_lbm", {
            "mask": {"nx": Nx, "ny": Ny, "nz": Nz, "solid": solid_flat},
            "params": prm,
            "n_steps": n_steps,
        })

        diffs = []
        if not cpp.get("ok"):
            diffs.append(f"cpp error: {cpp.get('error', '?')}")
        else:
            def cmp_field(name, py_arr):
                vp = py_arr.astype(np.float64).ravel(order="C")
                vc = np.asarray(cpp.get(name, []), dtype=np.float64)
                if vp.shape != vc.shape:
                    return f"{name} shape {vp.shape}!={vc.shape}"
                scale = max(float(np.max(np.abs(vp))), 1e-9)
                dmax = float(np.max(np.abs(vp - vc)))
                if dmax / scale > FIELD_TOL:
                    return f"{name} max|d|={dmax:.3e} (rel {dmax/scale:.2e})"
                return None

            for nm, arr in (("ux", ux_p), ("uy", uy_p), ("uz", uz_p), ("rho", rho_p)):
                d = cmp_field(nm, arr)
                if d:
                    diffs.append(d)

            cd_c = float(cpp.get("cd", 0.0))
            cd_rel = abs(cd_p - cd_c) / max(abs(cd_p), 1e-9)
            if cd_rel > CD_TOL:
                diffs.append(f"cd {cd_p:.6g}!={cd_c:.6g} (rel {cd_rel:.2e})")

            fa_c = float(cpp.get("frontal_area_mm2", 0.0))
            if abs(fa_p - fa_c) / max(abs(fa_p), 1e-9) > 1e-9:
                diffs.append(f"frontal_area {fa_p:.6g}!={fa_c:.6g}")

        if diffs:
            print(f"  {label:<24} DIFF: {'; '.join(diffs)[:120]}")
            mism += 1
        else:
            print(f"  {label:<24} MATCH  (cd={cd_p:.5g}, drag={drag_p:.4g} N, "
                  f"Afront={fa_p:.0f} mm2)")

    print(f"\n[lbm-parity] {checks - mism}/{checks} LBM cases at full parity "
          f"(field tol {FIELD_TOL:.0e} rel, Cd tol {CD_TOL:.0e} rel)")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
