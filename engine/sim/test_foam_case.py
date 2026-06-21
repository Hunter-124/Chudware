"""Offline tests for the OpenFOAM case generator + post-parser.

These do NOT require an OpenFOAM install — they cover geometry math (frontal area, the
world<->case transform, Reynolds, domain files) and force-coefficient parsing.  A live
end-to-end solve is exercised by tools/foam_validation.py (Phase 3, needs OpenFOAM).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import trimesh

from engine.sim import foam_case
from engine.sim import foam_post


def _sphere_stl(r=50.0):
    m = trimesh.creation.icosphere(subdivisions=3, radius=r)
    p = Path(tempfile.mkdtemp()) / "sphere.stl"
    m.export(str(p))
    return str(p), m


def test_frontal_area_sphere():
    _, m = _sphere_stl(50.0)
    a = foam_case._projected_area_mm2(m)
    true = math.pi * 50.0 ** 2
    assert abs(a - true) / true < 0.03   # within 3% of pi r^2


def test_build_case_files_and_physics():
    stl, _ = _sphere_stl(50.0)
    case = tempfile.mkdtemp()
    info = foam_case.build_case(case, stl, direction=(1, 0, 0), speed_mps=10.0,
                                preset="coarse",
                                fluid={"density_kgm3": 1.225, "kinematic_visc_m2s": 1.5e-5})
    # Reynolds = U * L / nu ; L = 100mm = 0.1m -> 10*0.1/1.5e-5
    assert abs(info.reynolds - 10 * 0.1 / 1.5e-5) / info.reynolds < 1e-6
    assert abs(info.char_len_m - 0.1) < 1e-6
    for f in ["system/blockMeshDict", "system/snappyHexMeshDict", "system/controlDict",
              "system/fvSchemes", "system/fvSolution", "constant/transportProperties",
              "constant/turbulenceProperties", "constant/triSurface/part.stl",
              "0/U", "0/p", "0/k", "0/omega", "0/nut"]:
        assert (Path(case) / f).exists(), f"missing {f}"


def test_world_case_transform_roundtrip():
    stl, _ = _sphere_stl(30.0)
    info = foam_case.build_case(tempfile.mkdtemp(), stl, direction=(0, 1, 0), speed_mps=5.0)
    pts = np.array([[10.0, -20.0, 5.0], [0.0, 0.0, 0.0], [-15.0, 7.0, 22.0]])
    back = info.case_m_to_world_mm(info.world_mm_to_case_m(pts))
    assert np.allclose(back, pts, atol=1e-6)


def test_parse_coefficients(tmp_path=None):
    d = Path(tempfile.mkdtemp())
    dat = d / "postProcessing" / "forceCoeffs" / "0" / "coefficient.dat"
    dat.parent.mkdir(parents=True)
    dat.write_text(
        "# Force coefficients\n"
        "# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)\n"
        "1\t0.5\t0.25\t0.25\t0.1\t0.05\t0.05\n"
        "2\t0.42\t0.21\t0.21\t0.08\t0.04\t0.04\n"
    )
    cd, cl = foam_post.parse_coefficients(str(d))
    assert abs(cd - 0.42) < 1e-9
    assert abs(cl - 0.08) < 1e-9


def test_parse_coefficients_missing():
    cd, cl = foam_post.parse_coefficients(tempfile.mkdtemp())
    assert (cd, cl) == (0.0, 0.0)


def test_build_thermal_case():
    stl, _ = _sphere_stl(50.0)
    case = tempfile.mkdtemp()
    info = foam_case.build_case(case, stl, speed_mps=5.0, preset="coarse",
                                thermal={"enabled": True, "inlet_temp_c": 20.0,
                                         "wall_mode": "power", "wall_power_w": 50.0})
    assert info.thermal and info.app == "buoyantSimpleFoam"
    for f in ["constant/thermophysicalProperties", "constant/g", "0/T", "0/p_rgh", "0/alphat"]:
        assert (Path(case) / f).exists(), f"missing {f}"


def test_build_cht_case():
    from engine.sim import foam_cht
    stl, _ = _sphere_stl(50.0)
    case = tempfile.mkdtemp()
    info = foam_cht.build_cht_case(case, stl, speed_mps=5.0, preset="coarse",
                                   solid_kappa=200.0, power_w=20.0)
    assert info.conjugate and info.app == "chtMultiRegionFoam"
    assert info.end_time > 0
    for f in ["constant/regionProperties", "constant/fluid/thermophysicalProperties",
              "constant/part/thermophysicalProperties", "system/fvSchemes",
              "system/fluid/fvSolution", "system/part/fvOptions",
              "0/fluid/T", "0/part/T", "system/snappyHexMeshDict"]:
        assert (Path(case) / f).exists(), f"missing {f}"
    # heat source power present in the solid fvOptions
    assert "20" in (Path(case) / "system" / "part" / "fvOptions").read_text()
