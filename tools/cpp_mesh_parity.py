"""cpp_mesh_parity.py — Python-vs-C++ MESH parity (gmsh tet4 mesher).

Drives the SAME STEP-meshing problem through the Python oracle
(engine.sim.mesher.mesh_step) and the C++ core (libaicad.dll: sim_mesh). BOTH
wrap the identical libgmsh 4.15.2 (the Python `gmsh` wheel and the C++ SDK), so
the tet mesh must be BIT-IDENTICAL: same node/element counts, identical
characteristic length, elementwise-identical node coordinates + tet connectivity.

Run under the RUNTIME venv (it has gmsh + build123d):
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_mesh_parity.py

Exit code = number of mismatched cases (0 = full parity), mirroring
tools/cpp_sim_parity.py.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- fixtures
def make_fixtures(tmp: Path) -> list[tuple[str, str]]:
    """Build a handful of STEP solids with build123d. Returns [(label, step_path)]."""
    from build123d import (  # noqa: WPS433
        Axis,
        Box,
        Cylinder,
        Location,
        Pos,
        export_step,
    )

    out: list[tuple[str, str]] = []

    # 1) plain box 30x20x10
    box = Box(30, 20, 10)
    p = str(tmp / "box.step")
    export_step(box, p)
    out.append(("box 30x20x10", p))

    # 2) cylinder r=8, h=25
    cyl = Cylinder(radius=8, height=25)
    p = str(tmp / "cyl.step")
    export_step(cyl, p)
    out.append(("cylinder r8 h25", p))

    # 3) box with a through hole (a "washer"-ish plate): 40x40x10 minus a r=8 bore
    plate = Box(40, 40, 10) - Cylinder(radius=8, height=10)
    p = str(tmp / "plate_hole.step")
    export_step(plate, p)
    out.append(("plate-with-hole", p))

    # 4) L-bracket: two boxes fused into an L
    lbr = Box(40, 10, 30) + Pos(15, 0, -10) * Box(10, 10, 10)
    p = str(tmp / "lbracket.step")
    export_step(lbr, p)
    out.append(("L-bracket", p))

    return out


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[mesh-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[mesh-parity] libaicad ABI={core.abi()}")

    # Both sides MUST wrap gmsh 4.15.2, else parity is meaningless.
    import gmsh  # noqa: WPS433

    gver = gmsh.GMSH_API_VERSION
    print(f"[mesh-parity] venv gmsh version = {gver}")
    if gver != "4.15.2":
        print(f"[mesh-parity] ABORT: venv gmsh is {gver}, expected 4.15.2 — "
              f"pip install gmsh==4.15.2 into the runtime venv first.")
        return 3

    from engine.sim.mesher import mesh_step  # noqa: WPS433

    mism = 0
    checks = 0

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            fixtures = make_fixtures(tmp)
        except Exception as exc:  # pragma: no cover
            print(f"[mesh-parity] could not build STEP fixtures: {exc}")
            return 4

        for label, step_path in fixtures:
            for density in (0.3, 0.6):
                checks += 1
                case = f"{label} d={density}"

                # The C++ side writes the .msh next to the STEP (.step -> .msh),
                # SAME as Python — give each side its own STEP copy so the two
                # .msh writes don't race on the same path.
                py_step = str(tmp / f"py_{Path(step_path).stem}_{density}.step")
                cpp_step = str(tmp / f"cpp_{Path(step_path).stem}_{density}.step")
                import shutil  # noqa: WPS433
                shutil.copyfile(step_path, py_step)
                shutil.copyfile(step_path, cpp_step)

                # Oracle.
                try:
                    ro = mesh_step(py_step, density)
                except Exception as exc:
                    print(f"  {case:<28} DIFF: python mesh_step raised {exc}")
                    mism += 1
                    continue

                # Candidate.
                rc, rcpp = core.call("sim_mesh", {"step_path": cpp_step,
                                                  "density": density})
                if rc != 0 or not rcpp.get("ok"):
                    print(f"  {case:<28} DIFF: cpp sim_mesh failed rc={rc} "
                          f"{str(rcpp)[:70]}")
                    mism += 1
                    continue

                diffs = []
                cpp_nn = int(rcpp["n_nodes"])
                cpp_ne = int(rcpp["n_elems"])
                if ro.n_nodes != cpp_nn:
                    diffs.append(f"n_nodes {ro.n_nodes}!={cpp_nn}")
                if ro.n_elems != cpp_ne:
                    diffs.append(f"n_elems {ro.n_elems}!={cpp_ne}")

                # Characteristic length identical to ~1e-12.
                clen_d = abs(float(ro.char_len_mm) - float(rcpp["char_len_mm"]))
                if clen_d > 1e-12:
                    diffs.append(f"char_len d={clen_d:.2e}")

                # Node coordinates elementwise identical.
                cpp_pts = np.asarray(rcpp["points"], dtype=np.float64).reshape(-1, 3)
                if cpp_pts.shape == ro.points.shape:
                    pmax = float(np.max(np.abs(cpp_pts - ro.points))) if cpp_pts.size else 0.0
                    if pmax >= 1e-9:
                        diffs.append(f"points max|d|={pmax:.3e}")
                else:
                    diffs.append(f"points shape {cpp_pts.shape}!={ro.points.shape}")

                # Tet connectivity elementwise identical.
                cpp_tets = np.asarray(rcpp["tets"], dtype=np.int64).reshape(-1, 4)
                py_tets = ro.tets.astype(np.int64)
                if cpp_tets.shape == py_tets.shape:
                    if not np.array_equal(cpp_tets, py_tets):
                        nd = int(np.count_nonzero(np.any(cpp_tets != py_tets, axis=1)))
                        diffs.append(f"tets differ ({nd} rows)")
                else:
                    diffs.append(f"tets shape {cpp_tets.shape}!={py_tets.shape}")

                if diffs:
                    print(f"  {case:<28} DIFF: {'; '.join(diffs)[:110]}")
                    mism += 1
                else:
                    print(f"  {case:<28} MATCH  (nodes={ro.n_nodes}, elems={ro.n_elems}, "
                          f"char_len={ro.char_len_mm:.4f}mm)")

    print(f"\n[mesh-parity] {checks - mism}/{checks} mesh cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
