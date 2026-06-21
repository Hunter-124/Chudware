"""Conjugate heat transfer (CHT) case generation — solid part + surrounding fluid.

True CHT couples heat *conduction inside the solid part* to *convection in the fluid* — the
right model for electronics cooling, heat sinks, and any "how hot does this part get?" question
where the part's own conductivity matters.

Workflow (all from a single watertight part STL):

  1. blockMesh           background wind-tunnel box (metres), as in the aero case.
  2. surfaceFeatureExtract / snappyHexMesh   mesh the box AND tag the cells *inside* the part
     surface as a ``part`` cellZone (faceZone on the surface) via two ``locationsInMesh`` seeds.
  3. splitMeshRegions -cellZones   split into a ``fluid`` region and a ``part`` (solid) region,
     with auto-generated coupled interface patches ``fluid_to_part`` / ``part_to_fluid``.
  4. chtMultiRegionFoam   solve the coupled energy equation with a volumetric heat source in
     the solid.

Steady trick: the steady temperature field is independent of the solid's thermal *mass*
(rho*Cp drops out at d/dt = 0), so we run the transient solver with an artificially small solid
rho*Cp.  Both regions then reach the *same* steady answer in a few fluid flow-through times
(minutes) instead of the solid's real thermal time constant (hours).

``build_cht_case`` writes the whole case and returns a :class:`CHTInfo`; ``engine.sim.openfoam``
drives the utilities and ``engine.sim.foam_post`` reads the solid temperature back.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import trimesh

from engine.sim.foam_case import (_PRESETS, _align_to_x, _compose_flow_dir, _header,
                                  _projected_area_mm2, _w)

_CMU = 0.09
_TI = 0.05


@dataclasses.dataclass
class CHTInfo:
    case_dir: str
    R: np.ndarray
    scale_m: float
    speed_mps: float
    reynolds: float
    char_len_m: float
    frontal_area_mm2: float
    power_w: float
    solid_kappa: float
    inlet_temp_k: float
    end_time: float
    fluid_region: str = "fluid"
    solid_region: str = "part"
    interface_fluid: str = "fluid_to_part"
    interface_solid: str = "part_to_fluid"
    app: str = "chtMultiRegionFoam"
    thermal: bool = True
    conjugate: bool = True

    def world_mm_to_case_m(self, pts_mm: np.ndarray) -> np.ndarray:
        p = np.asarray(pts_mm, dtype=np.float64).reshape(-1, 3)
        return (self.R @ p.T).T * self.scale_m

    def case_m_to_world_mm(self, pts_m: np.ndarray) -> np.ndarray:
        p = np.asarray(pts_m, dtype=np.float64).reshape(-1, 3)
        return (self.R.T @ (p / self.scale_m).T).T


def build_cht_case(case_dir: str, stl_path: str,
                   direction=(1.0, 0.0, 0.0), speed_mps: float = 5.0,
                   preset: str = "coarse", aoa_deg: float = 0.0, sideslip_deg: float = 0.0,
                   fluid: Optional[dict] = None, inlet_temp_c: float = 20.0,
                   solid_kappa: float = 200.0, power_w: float = 20.0) -> CHTInfo:
    """Write a complete chtMultiRegionFoam case; return its :class:`CHTInfo`."""
    pre = _PRESETS.get(preset, _PRESETS["coarse"])
    case = Path(case_dir)
    f = {"density_kgm3": 1.225, "kinematic_visc_m2s": 1.5e-5}
    if fluid:
        f.update({k: v for k, v in fluid.items() if v is not None})
    rho = float(f["density_kgm3"]) or 1.225
    nu = float(f["kinematic_visc_m2s"]) or 1.5e-5
    mu = nu * rho
    Tin = inlet_temp_c + 273.15

    flow_dir = _compose_flow_dir(np.asarray(direction, float), aoa_deg, sideslip_deg)
    world_mesh = trimesh.load(stl_path, force="mesh")
    if not isinstance(world_mesh, trimesh.Trimesh):
        raise ValueError("STL did not load as a single mesh")
    if not world_mesh.is_watertight:
        # CHT needs a closed solid to define the inside cellZone; fill small holes.
        world_mesh.fill_holes()
    aligned, R = _align_to_x(world_mesh, flow_dir)
    bb_min, bb_max = aligned.bounds
    size = bb_max - bb_min
    longest = float(np.max(size))
    char_len_m = longest * 1e-3
    reynolds = speed_mps * char_len_m / nu

    up = max(longest, 10.0); dn = max(3.0 * longest, 30.0); lat = max(longest, 10.0)
    box_min = np.array([bb_min[0] - up, bb_min[1] - lat, bb_min[2] - lat]) * 1e-3
    box_max = np.array([bb_max[0] + dn, bb_max[1] + lat, bb_max[2] + lat]) * 1e-3
    frontal_mm2 = _projected_area_mm2(aligned)

    # solid seed = part centroid (inside the closed surface); fluid seed = upstream.
    centroid_m = aligned.center_mass * 1e-3 if aligned.is_watertight else (np.array(
        [(bb_min[0] + bb_max[0]) / 2, (bb_min[1] + bb_max[1]) / 2, (bb_min[2] + bb_max[2]) / 2]) * 1e-3)
    fluid_seed = (box_min[0] + 0.4 * up * 1e-3, 0.5 * (box_min[1] + box_max[1]),
                  0.5 * (box_min[2] + box_max[2]))

    # STL -> metres into both region triSurface dirs (snappy reads constant/triSurface).
    part_m = aligned.copy(); part_m.apply_scale(1e-3)
    tri = case / "constant" / "triSurface"; tri.mkdir(parents=True, exist_ok=True)
    part_m.export(str(tri / "part.stl"))

    box_size = box_max - box_min
    base = float(np.max(box_size)) / pre["bg"]
    nx = max(4, int(round(box_size[0] / base)))
    ny = max(4, int(round(box_size[1] / base)))
    nz = max(4, int(round(box_size[2] / base)))

    U = float(speed_mps)
    k0 = 1.5 * (_TI * U) ** 2
    om0 = math.sqrt(k0) / (_CMU ** 0.25 * max(0.07 * char_len_m, 1e-6))

    # ~3 fluid flow-through times is enough for the near-body flow (hence solid T) to converge.
    flow_through = (box_size[0]) / max(U, 1e-3)
    end_time = max(0.05, round(3.0 * flow_through, 4))

    _write_blockmesh(case, box_min, box_max, nx, ny, nz)
    _write_cht_snappy(case, pre["refine"], fluid_seed)
    _write_surface_features(case)
    _write_mesh_quality(case)
    _write_top_mesh_dicts(case)
    _write_region_properties(case)
    _write_fluid_constant(case, mu, rho)
    _write_solid_constant(case, solid_kappa)
    _write_g(case)
    _write_control_dict(case, end_time)
    _write_top_fvsolution(case)
    _write_fluid_system(case)
    _write_solid_system(case, power_w)
    _write_fluid_fields(case, U, k0, om0, Tin)
    _write_solid_fields(case, Tin)

    return CHTInfo(
        case_dir=str(case), R=R, scale_m=1e-3, speed_mps=U, reynolds=reynolds,
        char_len_m=char_len_m, frontal_area_mm2=frontal_mm2, power_w=power_w,
        solid_kappa=solid_kappa, inlet_temp_k=Tin, end_time=end_time,
    )


# --------------------------------------------------------------------------- mesh dicts
# blockMesh / surfaceFeatures / meshQuality reuse the aero writers (same domain box).
from engine.sim.foam_case import (_write_blockmesh, _write_mesh_quality,  # noqa: E402
                                  _write_surface_features)


def _write_cht_snappy(case: Path, refine, fluid_seed) -> None:
    lvl_min, lvl_max = refine
    fx, fy, fz = fluid_seed
    body = _header("dictionary", "snappyHexMeshDict", "system") + (
        "castellatedMesh true;\nsnap true;\naddLayers false;\n\n"
        "geometry\n{\n    part { type triSurfaceMesh; file \"part.stl\"; }\n}\n\n"
        "castellatedMeshControls\n{\n    maxLocalCells 1000000;\n    maxGlobalCells 4000000;\n"
        "    minRefinementCells 10;\n    maxLoadUnbalance 0.10;\n    nCellsBetweenLevels 3;\n"
        "    features ( { file \"part.eMesh\"; level " f"{lvl_max}" "; } );\n"
        "    refinementSurfaces\n    {\n        part\n        {\n"
        f"            level ({lvl_min} {lvl_max});\n"
        "            faceZone part;\n            cellZone part;\n            cellZoneInside insidePoint;\n"
        "            insidePoint (0 0 0);\n        }\n    }\n"
        "    resolveFeatureAngle 30;\n    refinementRegions {}\n"
        f"    locationsInMesh ( (( {fx:.6g} {fy:.6g} {fz:.6g} ) fluid) );\n"
        "    allowFreeStandingZoneFaces false;\n}\n\n"
        "snapControls\n{\n    nSmoothPatch 3;\n    tolerance 2.0;\n    nSolveIter 50;\n"
        "    nRelaxIter 5;\n    nFeatureSnapIter 10;\n    implicitFeatureSnap false;\n"
        "    explicitFeatureSnap true;\n    multiRegionFeatureSnap false;\n}\n\n"
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

def _write_region_properties(case: Path) -> None:
    _w(case / "constant" / "regionProperties", _header("dictionary", "regionProperties", "constant") +
       "regions\n(\n    fluid (fluid)\n    solid (part)\n);\n")


def _write_fluid_constant(case: Path, mu: float, rho: float) -> None:
    _w(case / "constant" / "fluid" / "thermophysicalProperties",
       _header("dictionary", "thermophysicalProperties", "constant/fluid") +
       "thermoType\n{\n    type heRhoThermo; mixture pureMixture; transport const; thermo hConst;\n"
       "    equationOfState perfectGas; specie specie; energy sensibleEnthalpy;\n}\n"
       "mixture\n{\n    specie { molWeight 28.96; }\n    thermodynamics { Cp 1004.4; Hf 0; }\n"
       f"    transport {{ mu {mu:.6g}; Pr 0.705; }}\n}}\n")
    _w(case / "constant" / "fluid" / "turbulenceProperties",
       _header("dictionary", "turbulenceProperties", "constant/fluid") +
       "simulationType RAS;\nRAS { RASModel kOmegaSST; turbulence on; printCoeffs on; }\n")
    for reg in ("fluid", "part"):
        _w(case / "constant" / reg / "radiationProperties",
           _header("dictionary", "radiationProperties", "constant/" + reg) +
           "radiation off;\nradiationModel none;\n")


def _write_solid_constant(case: Path, kappa: float) -> None:
    # Real conductivity; artificially low rho*Cp so the transient reaches steady fast (the
    # steady temperature is independent of thermal mass).
    _w(case / "constant" / "part" / "thermophysicalProperties",
       _header("dictionary", "thermophysicalProperties", "constant/part") +
       "thermoType\n{\n    type heSolidThermo; mixture pureMixture; transport constIso; thermo hConst;\n"
       "    equationOfState rhoConst; specie specie; energy sensibleEnthalpy;\n}\n"
       "mixture\n{\n    specie { molWeight 27; }\n"
       f"    transport {{ kappa {kappa:.6g}; }}\n"
       "    thermodynamics { Hf 0; Cp 10; }\n    equationOfState { rho 10; }\n}\n")


def _write_g(case: Path) -> None:
    for loc in ("constant", "constant/fluid"):
        _w(case / loc / "g", _header("uniformDimensionedVectorField", "g", loc) +
           "dimensions [0 1 -2 0 0 0 0];\nvalue (0 0 -9.81);\n")


# --------------------------------------------------------------------------- system/

def _write_control_dict(case: Path, end_time: float) -> None:
    _w(case / "system" / "controlDict", _header("dictionary", "controlDict", "system") +
       "application chtMultiRegionFoam;\nstartFrom startTime; startTime 0; stopAt endTime;\n"
       f"endTime {end_time:.6g}; deltaT 1e-4; writeControl adjustableRunTime; "
       f"writeInterval {end_time:.6g}; purgeWrite 2;\n"
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


def _write_fluid_system(case: Path) -> None:
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
    _w(case / "system" / "fluid" / "fvOptions", _header("dictionary", "fvOptions", "system/fluid") +
       "limitT\n{\n    type limitTemperature;\n    selectionMode all;\n    min 250;\n    max 1500;\n}\n")


def _write_solid_system(case: Path, power_w: float) -> None:
    _w(case / "system" / "part" / "fvSchemes", _header("dictionary", "fvSchemes", "system/part") +
       "ddtSchemes { default Euler; }\ngradSchemes { default Gauss linear; }\n"
       "divSchemes { default none; }\nlaplacianSchemes { default Gauss linear corrected; }\n"
       "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n")
    _w(case / "system" / "part" / "fvSolution", _header("dictionary", "fvSolution", "system/part") +
       'solvers\n{\n    h { solver PCG; preconditioner DIC; tolerance 1e-7; relTol 0.1; }\n'
       '    hFinal { $h; relTol 0; }\n}\n'
       'PIMPLE { nNonOrthogonalCorrectors 0; }\nrelaxationFactors { equations { h 1; } }\n')
    # Volumetric heat source dissipating `power_w` watts (absolute) across the solid.
    _w(case / "system" / "part" / "fvOptions", _header("dictionary", "fvOptions", "system/part") +
       "heatSource\n{\n    type scalarSemiImplicitSource;\n    selectionMode all;\n"
       f"    volumeMode absolute;\n    injectionRateSuSp {{ h ({power_w:.6g} 0); }}\n}}\n")


# --------------------------------------------------------------------------- 0/

def _write_fluid_fields(case: Path, U: float, k0: float, om0: float, Tin: float) -> None:
    z = case / "0" / "fluid"
    coupled = ("compressible::turbulentTemperatureRadCoupledMixed; "
               "Tnbr T; kappaMethod fluidThermo; qrNbr none; qr none; value uniform %g;" % Tin)
    _w(z / "U", _header("volVectorField", "U", "0/fluid") +
       f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform ({U} 0 0);\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform ({U} 0 0); }}\n"
       "    outlet { type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }\n"
       "    sides { type slip; }\n    fluid_to_part { type noSlip; }\n}\n")
    _w(z / "p_rgh", _header("volScalarField", "p_rgh", "0/fluid") +
       "dimensions [1 -1 -2 0 0 0 0];\ninternalField uniform 1e5;\nboundaryField\n{\n"
       "    inlet { type fixedFluxPressure; value uniform 1e5; }\n"
       "    outlet { type prghPressure; p uniform 1e5; value uniform 1e5; }\n"
       "    sides { type fixedFluxPressure; value uniform 1e5; }\n"
       "    fluid_to_part { type fixedFluxPressure; value uniform 1e5; }\n}\n")
    _w(z / "p", _header("volScalarField", "p", "0/fluid") +
       "dimensions [1 -1 -2 0 0 0 0];\ninternalField uniform 1e5;\nboundaryField\n{\n"
       "    inlet { type calculated; value uniform 1e5; }\n    outlet { type calculated; value uniform 1e5; }\n"
       "    sides { type calculated; value uniform 1e5; }\n    fluid_to_part { type calculated; value uniform 1e5; }\n}\n")
    _w(z / "T", _header("volScalarField", "T", "0/fluid") +
       f"dimensions [0 0 0 1 0 0 0];\ninternalField uniform {Tin};\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform {Tin}; }}\n"
       f"    outlet {{ type inletOutlet; inletValue uniform {Tin}; value uniform {Tin}; }}\n"
       "    sides { type zeroGradient; }\n"
       f"    fluid_to_part {{ type {coupled} }}\n}}\n")
    _w(z / "k", _header("volScalarField", "k", "0/fluid") +
       f"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform {k0:.6g};\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform {k0:.6g}; }}\n"
       f"    outlet {{ type inletOutlet; inletValue uniform {k0:.6g}; value uniform {k0:.6g}; }}\n"
       "    sides { type slip; }\n    fluid_to_part { type kqRWallFunction; value uniform %g; }\n}\n" % k0)
    _w(z / "omega", _header("volScalarField", "omega", "0/fluid") +
       f"dimensions [0 0 -1 0 0 0 0];\ninternalField uniform {om0:.6g};\nboundaryField\n{{\n"
       f"    inlet {{ type fixedValue; value uniform {om0:.6g}; }}\n"
       f"    outlet {{ type inletOutlet; inletValue uniform {om0:.6g}; value uniform {om0:.6g}; }}\n"
       "    sides { type slip; }\n    fluid_to_part { type omegaWallFunction; value uniform %g; }\n}\n" % om0)
    _w(z / "nut", _header("volScalarField", "nut", "0/fluid") +
       "dimensions [0 2 -1 0 0 0 0];\ninternalField uniform 0;\nboundaryField\n{\n"
       "    inlet { type calculated; value uniform 0; }\n    outlet { type calculated; value uniform 0; }\n"
       "    sides { type calculated; value uniform 0; }\n    fluid_to_part { type nutkWallFunction; value uniform 0; }\n}\n")
    _w(z / "alphat", _header("volScalarField", "alphat", "0/fluid") +
       "dimensions [1 -1 -1 0 0 0 0];\ninternalField uniform 0;\nboundaryField\n{\n"
       "    inlet { type calculated; value uniform 0; }\n    outlet { type calculated; value uniform 0; }\n"
       "    sides { type calculated; value uniform 0; }\n"
       "    fluid_to_part { type compressible::alphatWallFunction; Prt 0.85; value uniform 0; }\n}\n")


def _write_solid_fields(case: Path, Tin: float) -> None:
    z = case / "0" / "part"
    _w(z / "T", _header("volScalarField", "T", "0/part") +
       f"dimensions [0 0 0 1 0 0 0];\ninternalField uniform {Tin};\nboundaryField\n{{\n"
       "    part_to_fluid { type compressible::turbulentTemperatureRadCoupledMixed; "
       f"Tnbr T; kappaMethod solidThermo; qrNbr none; qr none; value uniform {Tin}; }}\n}}\n")
    _w(z / "p", _header("volScalarField", "p", "0/part") +
       "dimensions [1 -1 -2 0 0 0 0];\ninternalField uniform 1e5;\nboundaryField\n{\n"
       "    part_to_fluid { type calculated; value uniform 1e5; }\n}\n")
