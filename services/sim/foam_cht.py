"""Conjugate heat transfer (CHT) case generation — solid part(s) + surrounding fluid.

True CHT couples heat *conduction inside the solid part* to *convection in the fluid* — the
right model for electronics cooling, heat sinks, and any "how hot does this part get?" question
where the part's own conductivity matters.

Workflow:

  1. blockMesh           background wind-tunnel box (metres), as in the aero case.
  2. surfaceFeatureExtract / snappyHexMesh   mesh the box AND tag the cells *inside* each solid
     surface as its own cellZone (faceZone on the surface) via ``locationsInMesh`` seeds.
  3. splitMeshRegions -cellZones   split into a ``fluid`` region and one region per solid,
     with auto-generated coupled interface patches ``fluid_to_<name>`` / ``<name>_to_fluid``.
  4. chtMultiRegionFoam   solve the coupled energy equation with a volumetric heat source in
     each powered solid.

MULTI-SOLID SEAM (board/enclosure handoff): ``build_cht_case`` accepts a ``solids``
list — one entry per solid region::

    solids=[{"name": "board", "stl_path": ".../board.stl", "kappa_w_mk": 8.0,  "power_w": 0.0},
            {"name": "u1",    "stl_path": ".../u1.stl",    "kappa_w_mk": 60.0, "power_w": 3.5}]

Each entry is a watertight solid STL in world millimetres (same frame for all entries),
``kappa_w_mk`` its thermal conductivity, ``power_w`` an absolute volumetric heat source in
watts (0 = passive conductor).  Names are sanitized to the OpenFOAM word charset and must be
unique.  When ``solids`` is omitted the legacy single-solid form is synthesized from
``stl_path`` / ``solid_kappa`` / ``power_w`` (region name ``part``), so existing callers are
unchanged.

Steady trick: the steady temperature field is independent of the solid's thermal *mass*
(rho*Cp drops out at d/dt = 0), so we run the transient solver with an artificially small solid
rho*Cp.  All regions then reach the *same* steady answer in a few fluid flow-through times
(minutes) instead of the solid's real thermal time constant (hours).

``build_cht_case`` writes the whole case and returns a :class:`CHTInfo`; ``services.sim.openfoam``
drives the utilities and ``services.sim.foam_post`` reads the solid temperature back.
"""

from __future__ import annotations

import dataclasses
import math
import re
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import trimesh

from services.sim.foam_case import (_PRESETS, _align_to_x, _compose_flow_dir, _header,
                                  _projected_area_mm2, _w)

_CMU = 0.09
_TI = 0.05

_WORD_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_region(name: str) -> str:
    w = re.sub(r"_+", "_", _WORD_RE.sub("_", str(name))).strip("_") or "solid"
    if w[0].isdigit():
        w = "s_" + w
    if w == "fluid":
        w = "solid_fluid"          # 'fluid' is reserved for the fluid region
    return w


@dataclasses.dataclass
class CHTInfo:
    case_dir: str
    R: np.ndarray
    scale_m: float
    speed_mps: float
    reynolds: float
    char_len_m: float
    frontal_area_mm2: float
    power_w: float                      # TOTAL dissipated power across all solids
    solid_kappa: float                  # first solid's kappa (legacy display)
    inlet_temp_k: float
    end_time: float
    fluid_region: str = "fluid"
    solid_region: str = "part"          # first/primary solid region (legacy)
    interface_fluid: str = "fluid_to_part"
    interface_solid: str = "part_to_fluid"
    app: str = "chtMultiRegionFoam"
    thermal: bool = True
    conjugate: bool = True
    # Multi-solid seam: every solid region, in build order.
    solid_regions: Tuple[str, ...] = ("part",)
    solids: Tuple[dict, ...] = ()       # normalized {name, kappa_w_mk, power_w} per region

    def world_mm_to_case_m(self, pts_mm: np.ndarray) -> np.ndarray:
        p = np.asarray(pts_mm, dtype=np.float64).reshape(-1, 3)
        return (self.R @ p.T).T * self.scale_m

    def case_m_to_world_mm(self, pts_m: np.ndarray) -> np.ndarray:
        p = np.asarray(pts_m, dtype=np.float64).reshape(-1, 3)
        return (self.R.T @ (p / self.scale_m).T).T


def _normalize_solids(stl_path: Optional[str], solids: Optional[List[dict]],
                      solid_kappa: float, power_w: float) -> List[dict]:
    if not solids:
        if not stl_path:
            raise ValueError("build_cht_case needs stl_path or a non-empty solids list")
        solids = [{"name": "part", "stl_path": stl_path,
                   "kappa_w_mk": solid_kappa, "power_w": power_w}]
    if not isinstance(solids, list) or not solids:
        raise ValueError("solids must be a non-empty list")
    out: List[dict] = []
    seen: set = set()
    for i, s in enumerate(solids):
        if not isinstance(s, dict):
            raise ValueError("each CHT solid must be a mapping")
        name = _sanitize_region(s.get("name") or f"solid{i}")
        if name in seen:
            raise ValueError(f"duplicate CHT solid region name {name!r}")
        seen.add(name)
        sp = s.get("stl_path")
        if not sp:
            raise ValueError(f"CHT solid {name!r} has no stl_path")
        kappa = float(s.get("kappa_w_mk", s.get("kappa", solid_kappa)))
        heat = float(s.get("power_w", power_w if len(solids) == 1 else 0.0))
        density = float(s.get("density_kgm3", 2700.0))
        cp = float(s.get("specific_heat_j_kgk", s.get("cp_j_kgk", 900.0)))
        if (not all(math.isfinite(v) for v in (kappa, heat, density, cp)) or
                kappa <= 0.0 or density <= 0.0 or cp <= 0.0 or heat < 0.0):
            raise ValueError(
                f"CHT solid {name!r} needs positive finite conductivity, density and heat "
                "capacity, and non-negative power")
        out.append({"name": name, "stl_path": str(sp), "kappa_w_mk": kappa,
                    "power_w": heat, "density_kgm3": density,
                    "specific_heat_j_kgk": cp})
    return out


def build_cht_case(case_dir: str, stl_path: Optional[str] = None,
                   direction=(1.0, 0.0, 0.0), speed_mps: float = 5.0,
                   preset: str = "coarse", aoa_deg: float = 0.0, sideslip_deg: float = 0.0,
                   fluid: Optional[dict] = None, inlet_temp_c: float = 20.0,
                   solid_kappa: float = 200.0, power_w: float = 20.0,
                   solids: Optional[List[dict]] = None) -> CHTInfo:
    """Write a complete chtMultiRegionFoam case; return its :class:`CHTInfo`.

    Single solid: pass ``stl_path`` (+ ``solid_kappa`` / ``power_w``) as before.
    Multi solid: pass ``solids=[{name, stl_path, kappa_w_mk, power_w}, ...]``.
    """
    if preset not in _PRESETS:
        raise ValueError(f"unknown OpenFOAM preset {preset!r}")
    pre = _PRESETS[preset]
    case = Path(case_dir)
    vals = [speed_mps, aoa_deg, sideslip_deg, inlet_temp_c]
    if not all(math.isfinite(float(v)) for v in vals) or float(speed_mps) <= 0.0:
        raise ValueError("CHT speed must be positive; angles and inlet temperature must be finite")
    speed_mps = float(speed_mps)
    aoa_deg = float(aoa_deg)
    sideslip_deg = float(sideslip_deg)
    inlet_temp_c = float(inlet_temp_c)
    d = np.asarray(direction, dtype=np.float64)
    if d.shape != (3,) or not np.isfinite(d).all() or np.linalg.norm(d) <= 1e-12:
        raise ValueError("direction must be a finite non-zero 3-vector")
    f = {"density_kgm3": 1.225, "kinematic_visc_m2s": 1.5e-5}
    if fluid is not None:
        if not isinstance(fluid, dict):
            raise ValueError("fluid must be a mapping")
        f.update({k: v for k, v in fluid.items() if v is not None})
    rho = float(f["density_kgm3"])
    nu = float(f["kinematic_visc_m2s"])
    if not math.isfinite(rho) or rho <= 0.0 or not math.isfinite(nu) or nu <= 0.0:
        raise ValueError("fluid density and kinematic viscosity must be positive and finite")
    mu = nu * rho
    Tin = float(inlet_temp_c) + 273.15
    if not math.isfinite(Tin) or Tin <= 0.0:
        raise ValueError("CHT inlet temperature must be above absolute zero")
    sol = _normalize_solids(stl_path, solids, solid_kappa, power_w)
    names = [s["name"] for s in sol]
    total_w = sum(s["power_w"] for s in sol)
    if not math.isfinite(total_w) or total_w <= 0.0:
        raise ValueError("CHT case needs a finite positive total heat source")

    flow_dir = _compose_flow_dir(d, float(aoa_deg), float(sideslip_deg))

    # Load every solid, force watertight, rotate ALL with the same alignment R (from the
    # combined geometry) so the shared frame transform in CHTInfo holds for all regions.
    meshes: List[trimesh.Trimesh] = []
    for s in sol:
        m = trimesh.load(s["stl_path"], force="mesh")
        if not isinstance(m, trimesh.Trimesh):
            raise ValueError(f"STL {s['stl_path']} did not load as a single mesh")
        if m.vertices.size == 0 or m.faces.size == 0 or not np.isfinite(m.vertices).all():
            raise ValueError(f"STL {s['stl_path']} is empty or contains non-finite vertices")
        if not m.is_watertight:
            raise ValueError(f"STL {s['stl_path']} must be watertight for a CHT solid region")
        meshes.append(m)
    combined = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0]
    _, R = _align_to_x(combined, flow_dir)
    T4 = np.eye(4)
    T4[:3, :3] = R
    aligned_all = combined.copy()
    aligned_all.apply_transform(T4)
    aligned: List[trimesh.Trimesh] = []
    for m in meshes:
        a = m.copy()
        a.apply_transform(T4)
        aligned.append(a)

    bb_min, bb_max = aligned_all.bounds
    size = bb_max - bb_min
    longest = float(np.max(size))
    if not math.isfinite(longest) or longest <= 1e-9:
        raise ValueError("CHT geometry has a degenerate bounding box")
    char_len_m = longest * 1e-3
    reynolds = speed_mps * char_len_m / nu

    up = max(longest, 10.0); dn = max(3.0 * longest, 30.0); lat = max(longest, 10.0)
    box_min = np.array([bb_min[0] - up, bb_min[1] - lat, bb_min[2] - lat]) * 1e-3
    box_max = np.array([bb_max[0] + dn, bb_max[1] + lat, bb_max[2] + lat]) * 1e-3
    frontal_mm2 = _projected_area_mm2(aligned_all)

    # Per-solid inside seed = its centroid (in metres); fluid seed = upstream of everything.
    seeds_m: List[Tuple[str, Tuple[float, float, float]]] = []
    tri = case / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    for s, a in zip(sol, aligned):
        c = a.center_mass if a.is_watertight else a.bounds.mean(axis=0)
        seeds_m.append((s["name"], tuple(float(x) for x in np.asarray(c, float) * 1e-3)))
        am = a.copy()
        am.apply_scale(1e-3)
        am.export(str(tri / f"{s['name']}.stl"))
    fluid_seed = (box_min[0] + 0.4 * up * 1e-3, 0.5 * (box_min[1] + box_max[1]),
                  0.5 * (box_min[2] + box_max[2]))

    box_size = box_max - box_min
    base = float(np.max(box_size)) / pre["bg"]
    nx = max(4, int(round(box_size[0] / base)))
    ny = max(4, int(round(box_size[1] / base)))
    nz = max(4, int(round(box_size[2] / base)))

    U = float(speed_mps)
    k0 = 1.5 * (_TI * U) ** 2
    om0 = math.sqrt(k0) / (_CMU ** 0.25 * max(0.07 * char_len_m, 1e-6))

    # Run for at least five flow-through and solid thermal-diffusion time scales. Three
    # time constants left the coupled residual above the production 1e-3 acceptance gate
    # on small electronics cases; five reaches the same physical steady-state criterion.
    flow_through = box_size[0] / U
    thermal_times = [
        char_len_m * char_len_m * s["density_kgm3"] *
        s["specific_heat_j_kgk"] / s["kappa_w_mk"] for s in sol
    ]
    physics_horizon = 5.0 * max([flow_through, *thermal_times])
    end_time = math.nextafter(max(0.05, physics_horizon), math.inf)

    _write_blockmesh(case, box_min, box_max, nx, ny, nz)
    _write_cht_snappy(case, pre["refine"], fluid_seed, names, seeds_m)
    _write_cht_surface_features(case, names)
    _write_mesh_quality(case)
    _write_top_mesh_dicts(case)
    _write_region_properties(case, names)
    _write_fluid_constant(case, mu, rho, names)
    _write_g(case)
    _write_control_dict(case, end_time)
    _write_top_fvsolution(case)
    _write_fluid_system(case, names)
    _write_fluid_fields(case, U, k0, om0, Tin, names)
    for s in sol:
        _write_solid_constant(case, s["name"], s["kappa_w_mk"],
                              s["density_kgm3"], s["specific_heat_j_kgk"])
        _write_solid_system(case, s["name"], s["power_w"])
        _write_solid_fields(case, s["name"], Tin)

    first = names[0]
    return CHTInfo(
        case_dir=str(case), R=R, scale_m=1e-3, speed_mps=U, reynolds=reynolds,
        char_len_m=char_len_m, frontal_area_mm2=frontal_mm2, power_w=total_w,
        solid_kappa=sol[0]["kappa_w_mk"], inlet_temp_k=Tin, end_time=end_time,
        solid_region=first, interface_fluid=f"fluid_to_{first}",
        interface_solid=f"{first}_to_fluid",
        solid_regions=tuple(names),
        solids=tuple({"name": s["name"], "kappa_w_mk": s["kappa_w_mk"],
                      "power_w": s["power_w"], "density_kgm3": s["density_kgm3"],
                      "specific_heat_j_kgk": s["specific_heat_j_kgk"]} for s in sol),
    )


# --------------------------------------------------------------------------- mesh dicts
# blockMesh / meshQuality reuse the aero writers (same domain box).
from services.sim.foam_case import _write_blockmesh, _write_mesh_quality  # noqa: E402


def _write_cht_surface_features(case: Path, names: List[str]) -> None:
    body = _header("dictionary", "surfaceFeatureExtractDict", "system")
    for n in names:
        body += (f"{n}.stl\n{{\n"
                 "    extractionMethod    extractFromSurface;\n"
                 "    extractFromSurfaceCoeffs { includedAngle 150; }\n"
                 "    writeObj            no;\n}\n")
    _w(case / "system" / "surfaceFeatureExtractDict", body)


def _write_cht_snappy(case: Path, refine, fluid_seed, names: List[str],
                      seeds_m: List[Tuple[str, Tuple[float, float, float]]]) -> None:
    lvl_min, lvl_max = refine
    fx, fy, fz = fluid_seed
    geom = "".join(f'    {n} {{ type triSurfaceMesh; file "{n}.stl"; }}\n' for n in names)
    feats = " ".join(f'{{ file "{n}.eMesh"; level {lvl_max}; }}' for n in names)
    surfs = ""
    for n, (sx, sy, sz) in seeds_m:
        surfs += (f"        {n}\n        {{\n"
                  f"            level ({lvl_min} {lvl_max});\n"
                  f"            faceZone {n};\n            cellZone {n};\n"
                  "            cellZoneInside insidePoint;\n"
                  f"            insidePoint ({sx:.6g} {sy:.6g} {sz:.6g});\n        }}\n")
    multi = "true" if len(names) > 1 else "false"
    body = _header("dictionary", "snappyHexMeshDict", "system") + (
        "castellatedMesh true;\nsnap true;\naddLayers false;\n\n"
        "geometry\n{\n" + geom + "}\n\n"
        "castellatedMeshControls\n{\n    maxLocalCells 1000000;\n    maxGlobalCells 4000000;\n"
        "    minRefinementCells 10;\n    maxLoadUnbalance 0.10;\n    nCellsBetweenLevels 3;\n"
        f"    features ( {feats} );\n"
        "    refinementSurfaces\n    {\n" + surfs + "    }\n"
        "    resolveFeatureAngle 30;\n    refinementRegions {}\n"
        f"    locationsInMesh ( (( {fx:.6g} {fy:.6g} {fz:.6g} ) fluid) );\n"
        "    allowFreeStandingZoneFaces false;\n}\n\n"
        "snapControls\n{\n    nSmoothPatch 3;\n    tolerance 2.0;\n    nSolveIter 50;\n"
        "    nRelaxIter 5;\n    nFeatureSnapIter 10;\n    implicitFeatureSnap false;\n"
        f"    explicitFeatureSnap true;\n    multiRegionFeatureSnap {multi};\n}}\n\n"
        "addLayersControls\n{\n    relativeSizes true;\n    layers {}\n    expansionRatio 1.2;\n"
        "    finalLayerThickness 0.5;\n    minThickness 0.05;\n    nGrow 0;\n    featureAngle 130;\n"
        "    nRelaxIter 5;\n    nSmoothSurfaceNormals 1;\n    nSmoothNormals 3;\n    nSmoothThickness 10;\n"
        "    maxFaceThicknessRatio 0.5;\n    maxThicknessToMedialRatio 0.3;\n    minMedialAxisAngle 90;\n"
        "    nBufferCellsNoExtrude 0;\n    nLayerIter 50;\n}\n\n"
        "meshQualityControls\n{\n    #include \"meshQualityDict\"\n}\n\n"
        "writeFlags ( scalarLevels );\nmergeTolerance 1e-6;\n"
    )
    _w(case / "system" / "snappyHexMeshDict", body)


# --------------------------------------------------------------------------- constant/

def _write_region_properties(case: Path, names: List[str]) -> None:
    _w(case / "constant" / "regionProperties", _header("dictionary", "regionProperties", "constant") +
       "regions\n(\n    fluid (fluid)\n    solid (" + " ".join(names) + ")\n);\n")


def _write_fluid_constant(case: Path, mu: float, rho: float, names: List[str]) -> None:
    _w(case / "constant" / "fluid" / "thermophysicalProperties",
       _header("dictionary", "thermophysicalProperties", "constant/fluid") +
       "thermoType\n{\n    type heRhoThermo; mixture pureMixture; transport const; thermo hConst;\n"
       "    equationOfState perfectGas; specie specie; energy sensibleEnthalpy;\n}\n"
       "mixture\n{\n    specie { molWeight 28.96; }\n    thermodynamics { Cp 1004.4; Hf 0; }\n"
       f"    transport {{ mu {mu:.6g}; Pr 0.705; }}\n}}\n")
    _w(case / "constant" / "fluid" / "turbulenceProperties",
       _header("dictionary", "turbulenceProperties", "constant/fluid") +
       "simulationType RAS;\nRAS { RASModel kOmegaSST; turbulence on; printCoeffs on; }\n")
    for reg in ("fluid", *names):
        _w(case / "constant" / reg / "radiationProperties",
           _header("dictionary", "radiationProperties", "constant/" + reg) +
           "radiation off;\nradiationModel none;\n")


def _write_solid_constant(case: Path, name: str, kappa: float,
                          density: float, cp: float) -> None:
    _w(case / "constant" / name / "thermophysicalProperties",
       _header("dictionary", "thermophysicalProperties", f"constant/{name}") +
       "thermoType\n{\n    type heSolidThermo;\n    mixture pureMixture;\n"
       "    transport constIso;\n    thermo hConst;\n    equationOfState rhoConst;\n"
       "    specie specie;\n    energy sensibleEnthalpy;\n}\n"
       "mixture\n{\n"
       f"    transport {{ kappa {kappa:.8g}; }}\n"
       "    specie { molWeight 1; }\n"
       f"    thermodynamics {{ Hf 0; Cp {cp:.8g}; }}\n"
       f"    equationOfState {{ rho {density:.8g}; }}\n"
       "}\n")


def _write_g(case: Path) -> None:
    for loc in ("constant", "constant/fluid"):
        _w(case / loc / "g", _header("uniformDimensionedVectorField", "g", loc) +
           "dimensions [0 1 -2 0 0 0 0];\nvalue (0 0 -9.81);\n")


# --------------------------------------------------------------------------- system/

def _write_control_dict(case: Path, end_time: float) -> None:
    _w(case / "system" / "controlDict", _header("dictionary", "controlDict", "system") +
       "application chtMultiRegionFoam;\nstartFrom startTime; startTime 0; stopAt endTime;\n"
       f"endTime {end_time!r}; deltaT 1e-4; writeControl adjustableRunTime; "
       f"writeInterval {end_time!r}; purgeWrite 2;\n"
       "writeFormat ascii; writePrecision 7; runTimeModifiable true;\n"
       "adjustTimeStep yes; maxCo 5; maxDi 50; maxDeltaT 0.01;\n")


def _write_top_fvsolution(case: Path) -> None:
    _w(case / "system" / "fvSolution", _header("dictionary", "fvSolution", "system") +
       "solvers { }\nPIMPLE { nOuterCorrectors 1; }\n")


def _write_top_mesh_dicts(case: Path) -> None:
    """Minimal top-level system/fvSchemes (+fvSolution) so blockMesh/snappy run on the
    combined mesh before splitMeshRegions creates the per-region dirs."""
    _w(case / "system" / "fvSchemes", _header("dictionary", "fvSchemes", "system") +
       "ddtSchemes { default steadyState; }\ngradSchemes { default Gauss linear; }\n"
       "divSchemes { default none; }\nlaplacianSchemes { default Gauss linear corrected; }\n"
       "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n")
    _write_top_fvsolution(case)


def _write_fluid_system(case: Path, names: List[str]) -> None:
    _w(case / "system" / "fluid" / "fvSchemes", _header("dictionary", "fvSchemes", "system/fluid") +
       "ddtSchemes { default Euler; }\ngradSchemes { default Gauss linear; }\n"
       "divSchemes { default none; div(phi,U) bounded Gauss linearUpwindV grad(U);\n"
       "  div(phi,K) bounded Gauss linear; div(phi,h) bounded Gauss linearUpwind grad(h);\n"
       "  div(phi,k) bounded Gauss upwind; div(phi,omega) bounded Gauss upwind;\n"
       "  div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear; }\n"
       "laplacianSchemes { default Gauss linear corrected; }\n"
       "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n"
       "wallDist { method meshWave; }\n")
    _w(case / "system" / "fluid" / "fvSolution", _header("dictionary", "fvSolution", "system/fluid") +
       'solvers\n{\n    "rho.*" { solver PCG; preconditioner DIC; tolerance 1e-7; relTol 0; }\n'
       '    p_rgh { solver GAMG; smoother GaussSeidel; tolerance 1e-7; relTol 0.01; }\n'
       '    p_rghFinal { $p_rgh; relTol 0; }\n'
       '    "(U|h|k|omega).*" { solver PBiCGStab; preconditioner DILU; tolerance 1e-7; relTol 0.1; }\n}\n'
       'PIMPLE { momentumPredictor on; nNonOrthogonalCorrectors 0; nCorrectors 2; transonic no; }\n'
       'relaxationFactors { equations { ".*" 1; } }\n')
    # Do not clamp temperature with limitTemperature: clipping a divergent or
    # genuinely out-of-range field would turn it into a finite-looking success.
    _w(case / "system" / "fluid" / "fvOptions",
       _header("dictionary", "fvOptions", "system/fluid"))


def _write_solid_system(case: Path, name: str, power_w: float) -> None:
    _w(case / "system" / name / "fvSchemes", _header("dictionary", "fvSchemes", "system/" + name) +
       "ddtSchemes { default Euler; }\ngradSchemes { default Gauss linear; }\n"
       "divSchemes { default none; }\nlaplacianSchemes { default Gauss linear corrected; }\n"
       "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n")
    _w(case / "system" / name / "fvSolution", _header("dictionary", "fvSolution", "system/" + name) +
       'solvers\n{\n    h { solver PCG; preconditioner DIC; tolerance 1e-7; relTol 0.1; }\n'
       '    hFinal { $h; relTol 0; }\n}\n'
       'PIMPLE { nNonOrthogonalCorrectors 0; }\nrelaxationFactors { equations { h 1; } }\n')
    # Volumetric heat source dissipating `power_w` watts (absolute) across this solid.
    if power_w > 0.0:
        _w(case / "system" / name / "fvOptions", _header("dictionary", "fvOptions", "system/" + name) +
           "heatSource\n{\n    type scalarSemiImplicitSource;\n    selectionMode all;\n"
           f"    volumeMode absolute;\n    injectionRateSuSp {{ h ({power_w:.6g} 0); }}\n}}\n")
    else:
        _w(case / "system" / name / "fvOptions",
           _header("dictionary", "fvOptions", "system/" + name) + "// passive solid - no heat source\n")


# --------------------------------------------------------------------------- 0/

def _write_fluid_fields(case: Path, U: float, k0: float, om0: float, Tin: float,
                        names: List[str]) -> None:
    z = case / "0" / "fluid"
    ifaces = [f"fluid_to_{n}" for n in names]

    def per_iface(entry: str) -> str:
        return "".join(f"    {p} {{ {entry} }}\n" for p in ifaces)

    coupled = ("type compressible::turbulentTemperatureRadCoupledMixed; "
               "Tnbr T; kappaMethod fluidThermo; qrNbr none; qr none; value uniform %g;" % Tin)
    _w(z / "U", _header("volVectorField", "U", "0/fluid") +
       f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform ({U} 0 0);\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform ({U} 0 0); }}\n"
       "    outlet { type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }\n"
       "    sides { type slip; }\n" + per_iface("type noSlip;") + "}\n")
    _w(z / "p_rgh", _header("volScalarField", "p_rgh", "0/fluid") +
       "dimensions [1 -1 -2 0 0 0 0];\ninternalField uniform 1e5;\nboundaryField\n{\n"
       "    inlet { type fixedFluxPressure; value uniform 1e5; }\n"
       "    outlet { type prghPressure; p uniform 1e5; value uniform 1e5; }\n"
       "    sides { type fixedFluxPressure; value uniform 1e5; }\n"
       + per_iface("type fixedFluxPressure; value uniform 1e5;") + "}\n")
    _w(z / "p", _header("volScalarField", "p", "0/fluid") +
       "dimensions [1 -1 -2 0 0 0 0];\ninternalField uniform 1e5;\nboundaryField\n{\n"
       "    inlet { type calculated; value uniform 1e5; }\n    outlet { type calculated; value uniform 1e5; }\n"
       "    sides { type calculated; value uniform 1e5; }\n"
       + per_iface("type calculated; value uniform 1e5;") + "}\n")
    _w(z / "T", _header("volScalarField", "T", "0/fluid") +
       f"dimensions [0 0 0 1 0 0 0];\ninternalField uniform {Tin};\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform {Tin}; }}\n"
       f"    outlet {{ type inletOutlet; inletValue uniform {Tin}; value uniform {Tin}; }}\n"
       "    sides { type zeroGradient; }\n" + per_iface(coupled) + "}\n")
    _w(z / "k", _header("volScalarField", "k", "0/fluid") +
       f"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform {k0:.6g};\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform {k0:.6g}; }}\n"
       f"    outlet {{ type inletOutlet; inletValue uniform {k0:.6g}; value uniform {k0:.6g}; }}\n"
       "    sides { type zeroGradient; }\n"
       + per_iface("type kqRWallFunction; value uniform %g;" % k0) + "}\n")
    _w(z / "omega", _header("volScalarField", "omega", "0/fluid") +
       f"dimensions [0 0 -1 0 0 0 0];\ninternalField uniform {om0:.6g};\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform {om0:.6g}; }}\n"
       f"    outlet {{ type inletOutlet; inletValue uniform {om0:.6g}; value uniform {om0:.6g}; }}\n"
       "    sides { type zeroGradient; }\n"
       + per_iface("type omegaWallFunction; value uniform %g;" % om0) + "}\n")
    _w(z / "nut", _header("volScalarField", "nut", "0/fluid") +
       "dimensions [0 2 -1 0 0 0 0];\ninternalField uniform 0;\nboundaryField\n{\n"
       "    inlet { type calculated; value uniform 0; }\n    outlet { type calculated; value uniform 0; }\n"
       "    sides { type calculated; value uniform 0; }\n"
       + per_iface("type nutkWallFunction; value uniform 0;") + "}\n")
    _w(z / "alphat", _header("volScalarField", "alphat", "0/fluid") +
       "dimensions [1 -1 -1 0 0 0 0];\ninternalField uniform 0;\nboundaryField\n{\n"
       "    inlet { type calculated; value uniform 0; }\n    outlet { type calculated; value uniform 0; }\n"
       "    sides { type calculated; value uniform 0; }\n"
       + per_iface("type compressible::alphatWallFunction; Prt 0.85; value uniform 0;") + "}\n")


def _write_solid_fields(case: Path, name: str, Tin: float) -> None:
    z = case / "0" / name
    # A solid can touch the fluid AND other solids (board/component stacks): the regex
    # catch-all covers the fluid interface and every auto-generated inter-solid patch
    # (`<name>_to_<other>`) with the same coupled BC.
    _w(z / "T", _header("volScalarField", "T", "0/" + name) +
       f"dimensions [0 0 0 1 0 0 0];\ninternalField uniform {Tin};\nboundaryField\n{{\n"
       f'    "{name}_to_.*" {{ type compressible::turbulentTemperatureRadCoupledMixed; '
       f"Tnbr T; kappaMethod solidThermo; qrNbr none; qr none; value uniform {Tin}; }}\n}}\n")
    _w(z / "p", _header("volScalarField", "p", "0/" + name) +
       "dimensions [1 -1 -2 0 0 0 0];\ninternalField uniform 1e5;\nboundaryField\n{\n"
       f'    "{name}_to_.*" {{ type calculated; value uniform 1e5; }}\n}}\n')
