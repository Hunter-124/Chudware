"""Offline tests for the CHT case generator (services.sim.foam_cht).

No OpenFOAM required — these verify the written case dictionaries, including the
multi-solid seam (board + component volumetric heat sources).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import trimesh

from services.sim import foam_cht


def _box_stl(extents, center, name):
    m = trimesh.creation.box(extents=extents)
    m.apply_translation(center)
    p = Path(tempfile.mkdtemp()) / f"{name}.stl"
    m.export(str(p))
    return str(p)


def test_single_solid_legacy_layout():
    stl = _box_stl((40, 40, 10), (0, 0, 0), "part")
    case = tempfile.mkdtemp()
    info = foam_cht.build_cht_case(case, stl, speed_mps=3.0, preset="coarse",
                                   solid_kappa=150.0, power_w=12.0)
    assert info.solid_regions == ("part",)
    assert info.power_w == 12.0
    assert info.interface_fluid == "fluid_to_part"
    c = Path(case)
    for f in ("constant/regionProperties", "constant/part/thermophysicalProperties",
              "system/part/fvOptions", "0/part/T", "0/fluid/T",
              "constant/triSurface/part.stl", "system/snappyHexMeshDict"):
        assert (c / f).exists(), f"missing {f}"
    assert "solid (part)" in (c / "constant/regionProperties").read_text()
    assert "12" in (c / "system/part/fvOptions").read_text()  # heat source present


def test_multi_solid_seam():
    board = _box_stl((80, 60, 2), (0, 0, 0), "board")
    u1 = _box_stl((10, 10, 3), (10, 5, 2.5), "u1")
    u2 = _box_stl((8, 8, 2), (-20, -10, 2.0), "u2")
    case = tempfile.mkdtemp()
    info = foam_cht.build_cht_case(
        case, speed_mps=2.0, preset="coarse", inlet_temp_c=25.0,
        solids=[
            {"name": "board", "stl_path": board, "kappa_w_mk": 8.0, "power_w": 0.0},
            {"name": "u1", "stl_path": u1, "kappa_w_mk": 60.0, "power_w": 3.5},
            {"name": "u2", "stl_path": u2, "kappa_w_mk": 60.0, "power_w": 1.5},
        ])
    assert info.solid_regions == ("board", "u1", "u2")
    assert abs(info.power_w - 5.0) < 1e-9      # total dissipated power
    c = Path(case)
    # region properties list every solid
    assert "solid (board u1 u2)" in (c / "constant/regionProperties").read_text()
    # every solid gets its own tri-surface + constant/system/0 dirs
    for n in ("board", "u1", "u2"):
        assert (c / "constant/triSurface" / f"{n}.stl").exists()
        assert (c / "constant" / n / "thermophysicalProperties").exists()
        assert (c / "system" / n / "fvOptions").exists()
        assert (c / "0" / n / "T").exists()
    # heat sources only in powered solids
    assert "heatSource" not in (c / "system/board/fvOptions").read_text()
    assert "3.5" in (c / "system/u1/fvOptions").read_text()
    assert "1.5" in (c / "system/u2/fvOptions").read_text()
    # snappy declares one cellZone + insidePoint per solid, single fluid seed
    snappy = (c / "system/snappyHexMeshDict").read_text()
    for n in ("board", "u1", "u2"):
        assert f"cellZone {n};" in snappy
    assert snappy.count("insidePoint (") == 3
    assert "multiRegionFeatureSnap true;" in snappy
    # fluid fields carry a coupled/wall entry per interface patch
    for f in ("U", "T", "k", "omega", "nut", "alphat", "p", "p_rgh"):
        txt = (c / "0/fluid" / f).read_text()
        for n in ("board", "u1", "u2"):
            assert f"fluid_to_{n}" in txt, f"0/fluid/{f} missing fluid_to_{n}"
    # per-solid kappa landed
    assert "kappa 8" in (c / "constant/board/thermophysicalProperties").read_text()
    assert "kappa 60" in (c / "constant/u1/thermophysicalProperties").read_text()
    # Real thermal mass is written; the old rho=10/Cp=10 shortcut produced false steady fields.
    thermo = (c / "constant/u1/thermophysicalProperties").read_text()
    assert "rho 2700" in thermo
    assert "Cp 900" in thermo


def test_solid_validation_errors():
    with pytest.raises(ValueError):
        foam_cht.build_cht_case(tempfile.mkdtemp())          # neither stl nor solids
    stl = _box_stl((5, 5, 5), (0, 0, 0), "a")
    with pytest.raises(ValueError, match="duplicate"):
        foam_cht.build_cht_case(tempfile.mkdtemp(), solids=[
            {"name": "x", "stl_path": stl}, {"name": "x", "stl_path": stl}])
    with pytest.raises(ValueError, match="no stl_path"):
        foam_cht.build_cht_case(tempfile.mkdtemp(), solids=[{"name": "y"}])
    with pytest.raises(ValueError, match="positive total heat"):
        foam_cht.build_cht_case(tempfile.mkdtemp(), solids=[
            {"name": "passive", "stl_path": stl, "power_w": 0.0}])
    with pytest.raises(ValueError, match="conductivity"):
        foam_cht.build_cht_case(tempfile.mkdtemp(), solids=[
            {"name": "bad", "stl_path": stl, "power_w": 1.0, "kappa_w_mk": 0.0}])
    with pytest.raises(ValueError, match="absolute zero"):
        foam_cht.build_cht_case(
            tempfile.mkdtemp(), stl, inlet_temp_c=-300.0, power_w=1.0)


def test_region_name_sanitization():
    assert foam_cht._sanitize_region("U-1 (top)") == "U_1_top"
    assert foam_cht._sanitize_region("fluid") == "solid_fluid"
    assert foam_cht._sanitize_region("9volt") == "s_9volt"
    assert foam_cht._sanitize_region("") == "solid"
