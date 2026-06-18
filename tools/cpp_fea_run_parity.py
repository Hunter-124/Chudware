"""cpp_fea_run_parity.py — Python-vs-C++ parity for the COMBINED FEA run glue (P50b).

Where cpp_sim_parity.py isolates the FEA *solver* by handing both backends an
already-resolved {fixed_nodes, nodal_forces} problem, this gate exercises the NEW
surface that P50b adds: the host-study glue (sim_run_static / sim_run_modal) that
resolves point-cloud facesets to mesh nodes (_nodes_in_faceset) and builds the
nodal force vector from force/pressure loads (incl. the pressure area-weighted
face enumeration + fallback) BEFORE the solve.

To prove the glue (not gmsh), most cases feed a DETERMINISTIC tet mesh (make_beam,
copied from cpp_sim_parity — no gmsh) to BOTH sides:
  - Python oracle : build MeshResult + FaceSet/Load -> engine.sim.fea_static.run_static
                    / engine.sim.fea_modal.run_modal.
  - C++ core      : core.call("sim_run_static"/"sim_run_modal", {"study": {points, tets
                    inline, material, fixtures:[{points,tol_mm}], loads:[...]}}).
Compare the displacement field + von_mises field + summary at rel 1e-5 (SAME as
cpp_sim_parity); modal freqs at rel 1e-3. The PRESSURE case is the key new
assertion: the C++ area-weighted face force build must match the oracle bit-for-bit.

One END-TO-END STEP case (a build123d box) drives sim_run_static with step_path+
density on the C++ side and (mesh_step + run_static) on the Python side, confirming
the summary matches (mesh is bit-identical per P50). Skipped gracefully if build123d
is unavailable.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_fea_run_parity.py

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# ----------------------------------------------------------- structured tet beam
def make_beam(L: float, W: float, H: float, nx: int, ny: int, nz: int):
    """A L x W x H box meshed as nx*ny*nz hex cells, each split into 6 tet4s.

    Deterministic (no gmsh) so the Python oracle and the C++ core mesh the SAME
    geometry. Returns (points (N,3) float64, tets (M,4) int32)."""
    xs = np.linspace(0.0, L, nx + 1)
    ys = np.linspace(0.0, W, ny + 1)
    zs = np.linspace(0.0, H, nz + 1)

    def nid(i, j, k):
        return (i * (ny + 1) + j) * (nz + 1) + k

    pts = np.zeros(((nx + 1) * (ny + 1) * (nz + 1), 3), dtype=np.float64)
    for i in range(nx + 1):
        for j in range(ny + 1):
            for k in range(nz + 1):
                pts[nid(i, j, k)] = (xs[i], ys[j], zs[k])

    tets = []
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                c = [nid(i, j, k), nid(i + 1, j, k), nid(i + 1, j + 1, k), nid(i, j + 1, k),
                     nid(i, j, k + 1), nid(i + 1, j, k + 1), nid(i + 1, j + 1, k + 1),
                     nid(i, j + 1, k + 1)]
                for a, b, cc, d in [(0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6),
                                    (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6)]:
                    tets.append([c[a], c[b], c[cc], c[d]])
    return pts, np.asarray(tets, dtype=np.int32)


def _compare_static(label, ro, rcpp):
    """Compare an oracle StaticResult vs a C++ sim_run_static result dict.

    Returns a list of diff strings (empty == match). Mirrors cpp_sim_parity's
    field comparison, but compares summary against the oracle's ROUNDED summary
    dict (C++ fea_run_static rounds to the same precision)."""
    diffs = []
    # Summary scalars: C++ rounds to the oracle's precision, so compare against
    # ro.summary directly at rel 1e-5 (the rounding step is far finer than 1e-5
    # for these magnitudes, so this is not a weakened check).
    for key in ("max_disp_mm", "max_von_mises_mpa", "min_fos"):
        ov = float(ro.summary[key])
        cv = float(rcpp["summary"][key])
        rel = abs(ov - cv) / max(abs(ov), 1e-9)
        if rel > 1e-5:
            diffs.append(f"{key} {ov:.6g}!={cv:.6g} (rel {rel:.1e})")
    # Displacement field.
    ud = np.asarray(rcpp["displacement"], dtype=np.float64).reshape(-1, 3)
    if ud.shape == ro.displacement.shape:
        dmax = float(np.max(np.abs(ud - ro.displacement)))
        scale = max(float(np.max(np.abs(ro.displacement))), 1e-9)
        if dmax / scale > 1e-5:
            diffs.append(f"disp field max|d|={dmax:.3e} (rel {dmax/scale:.1e})")
    else:
        diffs.append(f"disp shape {ud.shape}!={ro.displacement.shape}")
    # von Mises field.
    vm = np.asarray(rcpp["von_mises"], dtype=np.float64)
    if vm.shape == ro.von_mises.shape:
        vmax = float(np.max(np.abs(vm - ro.von_mises)))
        vscale = max(float(np.max(np.abs(ro.von_mises))), 1e-9)
        if vmax / vscale > 1e-5:
            diffs.append(f"vm field max|d|={vmax:.3e} (rel {vmax/vscale:.1e})")
    else:
        diffs.append(f"vm shape {vm.shape}!={ro.von_mises.shape}")
    return diffs


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[fea-run-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[fea-run-parity] libaicad ABI={core.abi()}")

    from engine.sim.fea_static import run_static, FaceSet, Load
    from engine.sim.fea_modal import run_modal
    from engine.sim.mesher import MeshResult

    material = {"E_mpa": 200000.0, "nu": 0.29, "density_g_cm3": 7.87, "yield_mpa": 370.0}
    mism = 0
    checks = 0

    # ── STATIC: force + pressure loads, faceset-resolved (the NEW surface) ──────
    cases = [
        # (L,W,H,nx,ny,nz, load_kind, payload, label)
        (100.0, 10.0, 10.0, 10, 2, 2, "force", [0.0, 0.0, -100.0], "force -z cantilever"),
        (60.0, 12.0, 8.0, 8, 3, 2, "force", [50.0, 0.0, 0.0], "force +x beam"),
        (100.0, 10.0, 10.0, 10, 2, 2, "pressure", 0.5, "pressure 0.5MPa end"),
        (80.0, 10.0, 10.0, 8, 2, 2, "pressure", 1.25, "pressure 1.25MPa end"),
    ]
    for (L, W, H, nx, ny, nz, kind, payload, label) in cases:
        pts, tets = make_beam(L, W, H, nx, ny, nz)
        mesh = MeshResult(points=pts, tets=tets, mesh_path="", char_len_mm=0.0,
                          n_nodes=len(pts), n_elems=len(tets))
        fix_pts = pts[pts[:, 0] < 1e-6]
        load_pts = pts[pts[:, 0] > L - 1e-6]
        fixtures = [FaceSet(points=fix_pts, tol_mm=1e-4)]
        if kind == "force":
            fvec = np.array(payload, dtype=np.float64)
            loads = [Load(faceset=FaceSet(points=load_pts, tol_mm=1e-4), type="force",
                          vector=fvec)]
            load_json = {"faces": {"points": load_pts.tolist(), "tol_mm": 1e-4},
                         "type": "force", "vector": list(payload)}
        else:
            loads = [Load(faceset=FaceSet(points=load_pts, tol_mm=1e-4), type="pressure",
                          magnitude=float(payload))]
            load_json = {"faces": {"points": load_pts.tolist(), "tol_mm": 1e-4},
                         "type": "pressure", "magnitude": float(payload)}

        ro = run_static(mesh, material, fixtures, loads)

        rc, rcpp = core.call("sim_run_static", {"study": {
            "points": pts.ravel().tolist(), "tets": tets.ravel().tolist(),
            "material": material,
            "fixtures": [{"points": fix_pts.tolist(), "tol_mm": 1e-4}],
            "loads": [load_json]}})
        checks += 1
        if rc != 0 or not rcpp.get("ok"):
            print(f"  {label:<24} DIFF: cpp run failed rc={rc} {str(rcpp)[:70]}")
            mism += 1
            continue
        diffs = _compare_static(label, ro, rcpp)
        if diffs:
            print(f"  {label:<24} DIFF: {'; '.join(diffs)[:120]}")
            mism += 1
        else:
            print(f"  {label:<24} MATCH  (max_disp={ro.summary['max_disp_mm']:.4f}mm, "
                  f"max_vm={ro.summary['max_von_mises_mpa']:.1f}MPa)")

    # ── MODAL: fixtures-only, compare lowest natural frequencies ────────────────
    for (L, W, H, nx, ny, nz, nmodes, label) in [
        (100.0, 10.0, 10.0, 10, 2, 2, 6, "modal cantilever 6"),
        (50.0, 10.0, 10.0, 6, 2, 2, 4, "modal stub 4"),
    ]:
        pts, tets = make_beam(L, W, H, nx, ny, nz)
        mesh = MeshResult(points=pts, tets=tets, mesh_path="", char_len_mm=0.0,
                          n_nodes=len(pts), n_elems=len(tets))
        fix_pts = pts[pts[:, 0] < 1e-6]
        fixtures = [FaceSet(points=fix_pts, tol_mm=1e-4)]
        rmo = run_modal(mesh, material, fixtures, n_modes=nmodes)
        rc, rcm = core.call("sim_run_modal", {"study": {
            "points": pts.ravel().tolist(), "tets": tets.ravel().tolist(),
            "material": material,
            "fixtures": [{"points": fix_pts.tolist(), "tol_mm": 1e-4}]},
            "n_modes": nmodes})
        checks += 1
        if rc != 0 or not rcm.get("ok"):
            print(f"  {label:<24} DIFF: cpp run failed rc={rc} {str(rcm)[:70]}")
            mism += 1
            continue
        pf = np.asarray(rmo.freqs_hz, dtype=np.float64)
        cf = np.asarray(rcm["freqs_hz"], dtype=np.float64)
        n = min(len(pf), len(cf))
        rel = max((abs(pf[i] - cf[i]) / max(abs(pf[i]), 1.0)) for i in range(n)) if n else 1.0
        if n >= min(nmodes, 4) and rel <= 1e-3:
            print(f"  {label:<24} MATCH  (f1..f3 = "
                  f"{', '.join(f'{x:.1f}' for x in pf[:3])} Hz)")
        else:
            print(f"  {label:<24} DIFF: n={n} relerr={rel:.2e} py={pf[:n]} cpp={cf[:n]}")
            mism += 1

    # ── END-TO-END STEP: mesh a build123d box on BOTH sides, compare summary ────
    try:
        from build123d import Box, export_step
        from engine.sim.mesher import mesh_step
        checks += 1
        with tempfile.TemporaryDirectory() as td:
            step = Path(td) / "box.step"
            box = Box(40.0, 20.0, 20.0)
            export_step(box, str(step))
            density = 0.5
            # Python: mesh + run_static. Fix one face (x≈min), force on the other.
            pmesh = mesh_step(str(step), density)
            pp = pmesh.points
            xmin, xmax = float(pp[:, 0].min()), float(pp[:, 0].max())
            fix_pts = pp[pp[:, 0] < xmin + 1e-4]
            load_pts = pp[pp[:, 0] > xmax - 1e-4]
            fixtures = [FaceSet(points=fix_pts, tol_mm=1e-4)]
            loads = [Load(faceset=FaceSet(points=load_pts, tol_mm=1e-4), type="force",
                          vector=np.array([0.0, 0.0, -200.0]))]
            ro = run_static(pmesh, material, fixtures, loads)
            rc, rcpp = core.call("sim_run_static", {"study": {
                "step_path": str(step), "density": density,
                "material": material,
                "fixtures": [{"points": fix_pts.tolist(), "tol_mm": 1e-4}],
                "loads": [{"faces": {"points": load_pts.tolist(), "tol_mm": 1e-4},
                           "type": "force", "vector": [0.0, 0.0, -200.0]}]}})
            label = "STEP box e2e"
            if rc != 0 or not rcpp.get("ok"):
                print(f"  {label:<24} DIFF: cpp run failed rc={rc} {str(rcpp)[:70]}")
                mism += 1
            else:
                diffs = _compare_static(label, ro, rcpp)
                if diffs:
                    print(f"  {label:<24} DIFF: {'; '.join(diffs)[:120]}")
                    mism += 1
                else:
                    print(f"  {label:<24} MATCH  (n_dof={rcpp['n_dof']}, "
                          f"max_disp={ro.summary['max_disp_mm']:.4f}mm, "
                          f"max_vm={ro.summary['max_von_mises_mpa']:.1f}MPa)")
    except ImportError as e:
        print(f"  STEP box e2e             SKIP  (build123d unavailable: {e})")

    print(f"\n[fea-run-parity] {checks - mism}/{checks} cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
