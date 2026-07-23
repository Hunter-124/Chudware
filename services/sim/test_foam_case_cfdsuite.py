"""CFD accurate-path (OpenFOAM) offline tests — deterministic case generation.

Extends test_foam_case.py's patterns: everything here runs WITHOUT OpenFOAM
binaries (pure file generation + physics-derived values written into the case),
so the suite is green on boxes with no foam install. Runner-dependent behavior
is covered by clean availability checks, never by launching a solver.

References used for the value asserts:
  * Reynolds number Re = U*L/nu (definition).
  * Turbulence inlet k = 1.5*(I*U)^2 and omega = sqrt(k)/(Cmu^0.25 * l)
    (standard k-omega SST inlet estimates; the constants live in foam_case.py
    and the test recomputes from the SAME published formulas, not copies of
    the file's output).
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path

import pytest
import trimesh

from services.sim import foam_case
from services.sim.foam_runner import get_runner


def _sphere_stl(r=50.0, subdivisions=2):
    m = trimesh.creation.icosphere(subdivisions=subdivisions, radius=r)
    p = Path(tempfile.mkdtemp()) / "sphere.stl"
    m.export(str(p))
    return str(p)


def _case_hashes(case_dir: str) -> dict:
    out = {}
    root = Path(case_dir)
    for f in sorted(root.rglob("*")):
        if f.is_file():
            out[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# determinism: same inputs -> byte-identical case
# ---------------------------------------------------------------------------

def test_build_case_deterministic():
    stl = _sphere_stl(50.0)
    d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    foam_case.build_case(d1, stl, direction=(1, 0, 0), speed_mps=10.0, preset="coarse")
    foam_case.build_case(d2, stl, direction=(1, 0, 0), speed_mps=10.0, preset="coarse")
    h1, h2 = _case_hashes(d1), _case_hashes(d2)
    assert h1.keys() == h2.keys()
    diff = [k for k in h1 if h1[k] != h2[k]]
    assert not diff, f"non-deterministic case files: {diff}"


# ---------------------------------------------------------------------------
# physics values written into the case files
# ---------------------------------------------------------------------------

def test_case_file_value_anchors():
    stl = _sphere_stl(50.0)
    case = tempfile.mkdtemp()
    u, nu = 12.5, 1.5e-5
    info = foam_case.build_case(case, stl, direction=(1, 0, 0), speed_mps=u,
                                preset="coarse",
                                fluid={"density_kgm3": 1.225,
                                       "kinematic_visc_m2s": nu})
    # Re = U*L/nu; L = sphere diameter 100 mm = 0.1 m.
    assert info.reynolds == pytest.approx(u * 0.1 / nu, rel=1e-6)

    # 0/U inlet: fixedValue with the requested speed on +X (flow axis).
    u_txt = (Path(case) / "0" / "U").read_text()
    assert f"internalField   uniform ({u} 0 0);" in u_txt
    assert f"inlet   {{ type fixedValue; value uniform ({u} 0 0); }}" in u_txt

    # controlDict: endTime = preset iteration budget (coarse = 600).
    cd_txt = (Path(case) / "system" / "controlDict").read_text()
    assert f"endTime         {info.iters};" in cd_txt
    assert info.iters == 600            # coarse preset budget

    # transportProperties: the requested kinematic viscosity.
    tp_txt = (Path(case) / "constant" / "transportProperties").read_text()
    assert f"{nu}" in tp_txt or f"{nu:g}" in tp_txt

    # forceCoeffs config: dynamic-pressure reference values (magUInf, Aref).
    # foam_case wires the coefficients function into controlDict/its own dict;
    # find whichever file carries magUInf and check the numbers.
    hits = [p for p in Path(case).rglob("*")
            if p.is_file() and "magUInf" in p.read_text(errors="ignore")]
    assert hits, "no file carries the forceCoeffs magUInf reference"
    blob = "\n".join(p.read_text() for p in hits)
    assert f"magUInf         {u};" in blob or f"magUInf {u}" in blob.replace("  ", " ")

    # 0/k inlet: k = 1.5*(I*U)^2 (k-omega SST inlet estimate) with the module's
    # published turbulence intensity.
    k0 = 1.5 * (foam_case._TURB_INTENSITY * u) ** 2
    k_txt = (Path(case) / "0" / "k").read_text()
    assert f"{k0:.6g}" in k_txt


def test_domain_padding_matches_lbm_convention():
    """blockMesh domain = part bbox + (1L up, 3L down, 1L lateral) — the same
    wind-tunnel padding the LBM path uses (flow_run.cpp / lbm.py)."""
    stl = _sphere_stl(50.0)
    case = tempfile.mkdtemp()
    foam_case.build_case(case, stl, direction=(1, 0, 0), speed_mps=10.0, preset="coarse")
    bm = (Path(case) / "system" / "blockMeshDict").read_text()
    # part bbox is [-50, 50] mm on each axis; L = 100 mm.
    # x: [-50-100, 50+300] mm = [-0.15, 0.35] m; y,z: [-0.15, 0.15] m.
    import re as _re
    vtx_block = bm.split("vertices", 1)[1].split(");", 1)[0]
    triples = _re.findall(
        r"\(\s*(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)", vtx_block)
    assert len(triples) == 8, f"expected 8 blockMesh vertices, got {len(triples)}"
    xs = [float(t[0]) for t in triples]
    assert min(xs) == pytest.approx(-0.15, abs=5e-3)
    assert max(xs) == pytest.approx(0.35, abs=5e-3)


# ---------------------------------------------------------------------------
# max_steps / max_iters plumb (mirrors lbm.run_wind_tunnel's cap)
# ---------------------------------------------------------------------------

def test_build_case_max_iters_clamp():
    stl = _sphere_stl(30.0)
    case = tempfile.mkdtemp()
    info = foam_case.build_case(case, stl, speed_mps=10.0, preset="coarse",
                                max_iters=120)
    assert info.iters == 120
    cd_txt = (Path(case) / "system" / "controlDict").read_text()
    assert "endTime         120;" in cd_txt
    assert "writeInterval   120;" in cd_txt

    # cap larger than the preset budget must NOT raise it
    case2 = tempfile.mkdtemp()
    info2 = foam_case.build_case(case2, stl, speed_mps=10.0, preset="coarse",
                                 max_iters=10_000)
    assert info2.iters == 600


def test_build_case_max_iters_clamp_thermal():
    stl = _sphere_stl(30.0)
    case = tempfile.mkdtemp()
    info = foam_case.build_case(case, stl, speed_mps=5.0, preset="coarse",
                                thermal={"enabled": True, "inlet_temp_c": 20.0,
                                         "wall_mode": "temp", "wall_temp_c": 80.0},
                                max_iters=90)
    assert info.iters == 90 and info.app == "buoyantSimpleFoam"
    cd_txt = (Path(case) / "system" / "controlDict").read_text()
    assert "endTime         90;" in cd_txt


# ---------------------------------------------------------------------------
# runner availability: clean detection, clean refusal
# ---------------------------------------------------------------------------

def test_get_runner_never_raises():
    r = get_runner()
    if r is not None:
        assert isinstance(r.available(), bool)
        assert isinstance(r.version(), str)


def test_run_openfoam_tunnel_clean_refusal_when_absent():
    r = get_runner()
    if r is not None and r.available():
        pytest.skip("OpenFOAM is installed on this box; refusal path not reachable")
    from services.sim.openfoam import run_openfoam_tunnel
    stl = _sphere_stl(30.0, subdivisions=1)
    with pytest.raises(RuntimeError, match="OpenFOAM is not available"):
        run_openfoam_tunnel(stl, speed_mps=5.0, preset="coarse")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
