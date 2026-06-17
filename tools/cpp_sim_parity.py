"""cpp_sim_parity.py — Python-vs-C++ SIMULATION parity (FEA static first).

Drives the SAME linear-static FEA problem through the Python oracle
(engine.sim.fea_static.run_static) and the C++ core (libaicad.dll: sim_fea_static)
on a structured cantilever-beam tet mesh, and compares the displacement field +
von Mises stress + summary. Boundary conditions are resolved to nodes with the SAME
Python helper (_nodes_in_faceset) and the identical force split run_static uses, so
both backends solve the byte-identical linear system K u = f — any divergence is a
genuine numerical-port bug, not a BC-matching artifact.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_sim_parity.py

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# ----------------------------------------------------------- structured tet beam
def make_beam(L: float, W: float, H: float, nx: int, ny: int, nz: int):
    """A L x W x H box meshed as nx*ny*nz hex cells, each split into 6 tet4s.

    Returns (points (N,3) float64, tets (M,4) int32). Deterministic — no gmsh — so the
    Python oracle and the C++ core mesh the SAME geometry."""
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

    # 6-tet split of each hex (corners 0..7 in (x,y,z) lexicographic via the local map).
    tets = []
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                c = [nid(i, j, k), nid(i + 1, j, k), nid(i + 1, j + 1, k), nid(i, j + 1, k),
                     nid(i, j, k + 1), nid(i + 1, j, k + 1), nid(i + 1, j + 1, k + 1),
                     nid(i, j + 1, k + 1)]
                # local 0..7 -> Kuhn 6-tet decomposition through the 0-6 diagonal
                for a, b, cc, d in [(0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6),
                                    (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6)]:
                    tets.append([c[a], c[b], c[cc], c[d]])
    return pts, np.asarray(tets, dtype=np.int32)


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[sim-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[sim-parity] libaicad ABI={core.abi()}")

    from engine.sim.fea_static import run_static, FaceSet, Load, _nodes_in_faceset
    from engine.sim.mesher import MeshResult

    mism = 0
    checks = 0

    # Cantilever: 100 x 10 x 10 mm steel, fixed at x=0, 100 N down at x=L.
    for (L, W, H, nx, ny, nz, label) in [
        (100.0, 10.0, 10.0, 10, 2, 2, "cantilever 10x2x2"),
        (60.0, 12.0, 8.0, 8, 3, 2, "beam 8x3x2 force+x"),
    ]:
        pts, tets = make_beam(L, W, H, nx, ny, nz)
        mesh = MeshResult(points=pts, tets=tets, mesh_path="", char_len_mm=0.0,
                          n_nodes=len(pts), n_elems=len(tets))
        material = {"E_mpa": 200000.0, "nu": 0.29, "density_g_cm3": 7.87, "yield_mpa": 370.0}
        fvec = np.array([0.0, 0.0, -100.0]) if "down" not in label else np.array([0.0, 0.0, -100.0])
        if "force+x" in label:
            fvec = np.array([50.0, 0.0, 0.0])
        fix_pts = pts[pts[:, 0] < 1e-6]
        load_pts = pts[pts[:, 0] > L - 1e-6]
        fixtures = [FaceSet(points=fix_pts, tol_mm=1e-4)]
        loads = [Load(faceset=FaceSet(points=load_pts, tol_mm=1e-4), type="force", vector=fvec)]

        # Oracle.
        ro = run_static(mesh, material, fixtures, loads)

        # C++ inputs: resolve BCs identically (same helper + same equal force split).
        fixed_nodes = _nodes_in_faceset(pts, fixtures[0]).tolist()
        load_nodes = _nodes_in_faceset(pts, loads[0].faceset)
        f = np.zeros(len(pts) * 3, dtype=np.float64)
        per = fvec / len(load_nodes)
        for nid in load_nodes:
            f[nid * 3: nid * 3 + 3] += per

        rc, rcpp = core.call("sim_fea_static", {
            "points": pts.ravel().tolist(), "tets": tets.ravel().tolist(),
            "E_mpa": 200000.0, "nu": 0.29, "yield_mpa": 370.0,
            "fixed_nodes": fixed_nodes, "nodal_forces": f.tolist()})
        if rc != 0 or not rcpp.get("ok"):
            print(f"  {label:<26} DIFF: cpp solve failed rc={rc} {str(rcpp)[:60]}")
            mism += 1
            checks += 1
            continue

        diffs = []
        # Summary scalars compared against the UNROUNDED oracle arrays (ro.summary is
        # display-rounded via round(x, 6)/round(x, 4), which would falsely flag a
        # solver-precision match — the C++ reports the true unrounded value).
        vmpos = ro.von_mises[ro.von_mises > 0]
        o_min_fos = min(15.0, max(0.0, float((370.0 / vmpos).min()))) if len(vmpos) else 15.0
        for key, ov, otol in [("max_disp_mm", float(np.max(ro.disp_magnitude)), 1e-5),
                              ("max_von_mises_mpa", float(np.max(ro.von_mises)), 1e-5),
                              ("min_fos", o_min_fos, 1e-5)]:
            cv = float(rcpp["summary"][key])
            rel = abs(ov - cv) / max(abs(ov), 1e-9)
            if rel > otol:
                diffs.append(f"{key} {ov:.6g}!={cv:.6g} (rel {rel:.1e})")
        # Full displacement field: max absolute node-displacement difference.
        ud = np.asarray(rcpp["displacement"], dtype=np.float64).reshape(-1, 3)
        if ud.shape == ro.displacement.shape:
            dmax = float(np.max(np.abs(ud - ro.displacement)))
            scale = max(float(np.max(np.abs(ro.displacement))), 1e-9)
            if dmax / scale > 1e-5:
                diffs.append(f"disp field max|d|={dmax:.3e} (rel {dmax/scale:.1e})")
        else:
            diffs.append(f"disp shape {ud.shape}!={ro.displacement.shape}")
        # von Mises field max absolute difference.
        vm = np.asarray(rcpp["von_mises"], dtype=np.float64)
        if vm.shape == ro.von_mises.shape:
            vmax = float(np.max(np.abs(vm - ro.von_mises)))
            vscale = max(float(np.max(np.abs(ro.von_mises))), 1e-9)
            if vmax / vscale > 1e-5:
                diffs.append(f"vm field max|d|={vmax:.3e} (rel {vmax/vscale:.1e})")
        else:
            diffs.append(f"vm shape {vm.shape}!={ro.von_mises.shape}")

        checks += 1
        if diffs:
            print(f"  {label:<26} DIFF: {'; '.join(diffs)[:120]}")
            mism += 1
        else:
            print(f"  {label:<26} MATCH  (max_disp={ro.summary['max_disp_mm']:.4f}mm, "
                  f"max_vm={ro.summary['max_von_mises_mpa']:.1f}MPa)")

    # --- Modal FEA: compare the lowest natural frequencies (the robust eigen-invariant;
    # mode shapes carry sign/normalization + degenerate-subspace ambiguity). Same
    # cantilever, fixed at x=0. ARPACK (scipy eigsh) vs Spectra shift-invert should agree
    # to a few 1e-4 on well-separated modes.
    from engine.sim.fea_modal import run_modal
    for (L, W, H, nx, ny, nz, nmodes, label) in [
        (100.0, 10.0, 10.0, 10, 2, 2, 6, "modal cantilever 6"),
        (50.0, 10.0, 10.0, 6, 2, 2, 4, "modal stub 4"),
    ]:
        pts, tets = make_beam(L, W, H, nx, ny, nz)
        mesh = MeshResult(points=pts, tets=tets, mesh_path="", char_len_mm=0.0,
                          n_nodes=len(pts), n_elems=len(tets))
        material = {"E_mpa": 200000.0, "nu": 0.29, "density_g_cm3": 7.87, "yield_mpa": 370.0}
        fix_pts = pts[pts[:, 0] < 1e-6]
        fixtures = [FaceSet(points=fix_pts, tol_mm=1e-4)]
        rmo = run_modal(mesh, material, fixtures, n_modes=nmodes)
        fixed_nodes = _nodes_in_faceset(pts, fixtures[0]).tolist()
        rc, rcm = core.call("sim_fea_modal", {
            "points": pts.ravel().tolist(), "tets": tets.ravel().tolist(),
            "E_mpa": 200000.0, "nu": 0.29, "density_g_cm3": 7.87,
            "fixed_nodes": fixed_nodes, "n_modes": nmodes})
        checks += 1
        if rc != 0 or not rcm.get("ok"):
            print(f"  {label:<26} DIFF: cpp solve failed rc={rc} {str(rcm)[:60]}")
            mism += 1
            continue
        pf = np.asarray(rmo.freqs_hz, dtype=np.float64)
        cf = np.asarray(rcm["freqs_hz"], dtype=np.float64)
        n = min(len(pf), len(cf))
        rel = max((abs(pf[i] - cf[i]) / max(abs(pf[i]), 1.0)) for i in range(n)) if n else 1.0
        if n >= min(nmodes, 4) and rel <= 1e-3:
            print(f"  {label:<26} MATCH  (f1..f3 = "
                  f"{', '.join(f'{x:.1f}' for x in pf[:3])} Hz)")
        else:
            print(f"  {label:<26} DIFF: n={n} relerr={rel:.2e} py={pf[:n]} cpp={cf[:n]}")
            mism += 1

    print(f"\n[sim-parity] {checks - mism}/{checks} sim cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
