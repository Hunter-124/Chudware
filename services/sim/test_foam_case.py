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
import pytest

from services.sim import foam_case
from services.sim import foam_post


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
    p_txt = (Path(case) / "0" / "p").read_text()
    k_txt = (Path(case) / "0" / "k").read_text()
    omega_txt = (Path(case) / "0" / "omega").read_text()
    assert "sides   { type zeroGradient; }" in p_txt
    assert "sides   { type zeroGradient; }" in k_txt
    assert "sides   { type zeroGradient; }" in omega_txt
    control = (Path(case) / "system" / "controlDict").read_text()
    assert "type            forces;" in control


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
        "# Time Cd Cs Cl CmRoll CmPitch CmYaw\n"
        "1\t0.5\t9.0\t0.1\t0\t0\t0\n"
        "2\t0.42\t8.0\t0.08\t0\t0\t0\n"
    )
    cd, cl = foam_post.parse_coefficients(str(d))
    assert abs(cd - 0.42) < 1e-9
    assert abs(cl - 0.08) < 1e-9


def test_parse_resolved_forces_and_reject_stale_output():
    d = Path(tempfile.mkdtemp())
    dat = d / "postProcessing" / "forces" / "0" / "force.dat"
    dat.parent.mkdir(parents=True)
    dat.write_text(
        "# Time forces(pressure viscous porous) moment\n"
        "1 ((1 2 3) (0.1 0.2 0.3) (0.01 0.02 0.03)) "
        "((0 0 0) (0 0 0) (0 0 0))\n"
        "2 ((4 5 6) (0.4 0.5 0.6) (0.04 0.05 0.06)) "
        "((0 0 0) (0 0 0) (0 0 0))\n"
    )
    drag, side, lift = foam_post.parse_forces(str(d), expected_time=2.0)
    assert drag == pytest.approx(4.44)
    assert side == pytest.approx(5.55)
    assert lift == pytest.approx(6.66)
    with pytest.raises(ValueError, match="stale"):
        foam_post.parse_forces(str(d), expected_time=3.0)
    with dat.open("a") as stream:
        stream.write("3 ((nan 0 0) (0 0 0) (0 0 0)) "
                     "((0 0 0) (0 0 0) (0 0 0))\n")
    with pytest.raises(ValueError, match="latest"):
        foam_post.parse_forces(str(d))

def test_parse_resolved_forces_flat_v2412():
    d = Path(tempfile.mkdtemp())
    dat = d / "postProcessing" / "forces" / "0" / "force.dat"
    dat.parent.mkdir(parents=True)
    dat.write_text(
        "# Time total_x total_y total_z pressure_x pressure_y pressure_z "
        "viscous_x viscous_y viscous_z\n"
        "1 1.1 2.2 3.3 1 2 3 0.1 0.2 0.3\n"
        "2 4.4 5.5 6.6 4 5 6 0.4 0.5 0.6\n"
    )
    drag, side, lift = foam_post.parse_forces(str(d), expected_time=2.0)
    assert drag == pytest.approx(4.4)
    assert side == pytest.approx(5.5)
    assert lift == pytest.approx(6.6)


def test_parse_coefficients_missing_and_stale_are_errors():
    with pytest.raises(ValueError, match="no finite"):
        foam_post.parse_coefficients(tempfile.mkdtemp())
    d = Path(tempfile.mkdtemp())
    dat = d / "postProcessing" / "forceCoeffs" / "0" / "coefficient.dat"
    dat.parent.mkdir(parents=True)
    dat.write_text("# Time Cd Cs Cl\n10 0.4 0 0.1\n11 nan 0 0.2\n")
    with pytest.raises(ValueError, match="latest"):
        foam_post.parse_coefficients(str(d))
    dat.write_text("# Time Cd Cs Cl\n10 0.4 0 0.1\n")
    with pytest.raises(ValueError, match="stale"):
        foam_post.parse_coefficients(str(d), expected_time=12.0)
    dat.write_text("# Time Cd Cs Cl\n11 nan 0 inf\n")
    with pytest.raises(ValueError, match="no finite"):
        foam_post.parse_coefficients(str(d))


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
    from services.sim import foam_cht
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
    fluid_options = (Path(case) / "system/fluid/fvOptions").read_text()
    assert "limitTemperature" not in fluid_options
    # heat source power present in the solid fvOptions
    assert "20" in (Path(case) / "system" / "part" / "fvOptions").read_text()

def test_case_rejects_invalid_setup_before_writing_solver_files():
    stl, _ = _sphere_stl(20.0)
    for kwargs in (
        {"speed_mps": 0.0},
        {"direction": (0.0, 0.0, 0.0)},
        {"fluid": {"density_kgm3": -1.0, "kinematic_visc_m2s": 1.5e-5}},
        {"preset": "unknown"},
        {"thermal": {"enabled": True, "inlet_temp_c": -300.0,
                     "wall_mode": "temp", "wall_temp_c": 20.0}},
        {"max_iters": 0},
    ):
        with pytest.raises(ValueError):
            foam_case.build_case(tempfile.mkdtemp(), stl, **kwargs)


def test_cht_uses_physical_thermal_mass_and_diffusion_horizon():
    from services.sim import foam_cht
    stl, _ = _sphere_stl(20.0)
    case = tempfile.mkdtemp()
    info = foam_cht.build_cht_case(
        case, stl, speed_mps=2.0, power_w=10.0,
        solids=[{"name": "part", "stl_path": stl, "kappa_w_mk": 200.0,
                 "power_w": 10.0, "density_kgm3": 2700.0,
                 "specific_heat_j_kgk": 900.0}])
    thermo = (Path(case) / "constant/part/thermophysicalProperties").read_text()
    assert "rho 2700" in thermo
    assert "Cp 900" in thermo
    assert info.end_time >= 3.0 * (0.04 ** 2 * 2700.0 * 900.0 / 200.0)
