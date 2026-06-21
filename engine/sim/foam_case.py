"""Generate an OpenFOAM case directory for an external-aero RANS run.

Given a part STL (mm, world space), a flow direction, and a speed, this writes a complete
OpenFOAM case that meshes the part inside a wind-tunnel domain (blockMesh background +
snappyHexMesh body-fitted refinement *with prism boundary layers*) and solves steady
incompressible RANS (``simpleFoam``, k-ω SST).

Conventions kept consistent with the LBM solver (engine/sim/lbm.py) so the two engines are
interchangeable:

* Flow direction is composed from base-axis + AoA + sideslip via ``lbm._compose_flow_dir``.
* The part is rotated so the flow runs along +X (``lbm._align_to_x``); the background box is
  then axis-aligned with inlet on -X, outlet on +X, slip on the four lateral faces.
* Domain padding mirrors the LBM wind tunnel: ≈1× longest-axis upstream, 3× downstream,
  1× lateral each side.

Units: the OpenFOAM case works entirely in **metres** (STL pre-scaled mm→m), so ``speed_mps``
and ``nu`` (m²/s) give the correct Reynolds number directly.

``build_case`` returns a :class:`CaseInfo` carrying the forward transform (world-mm → case-m)
and reference quantities the post-processor needs to turn coefficients into forces and to map
OpenFOAM surface pressure back onto the world-space overlay.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import trimesh

from engine.sim.lbm import _align_to_x, _compose_flow_dir

# Per-preset mesh + solver budget.  Bigger preset = finer surface refinement, more prism
# layers, more SIMPLE iterations.  `bg` = target background cells along the longest box edge.
_PRESETS = {
    "coarse": {"bg": 18, "refine": (1, 2), "layers": 1, "iters": 600},
    "medium": {"bg": 26, "refine": (2, 3), "layers": 2, "iters": 1000},
    "fine":   {"bg": 36, "refine": (3, 4), "layers": 3, "iters": 1600},
    "xfine":  {"bg": 48, "refine": (4, 5), "layers": 4, "iters": 2500},
}

_CMU = 0.09           # k-ω/k-ε model constant
_TURB_INTENSITY = 0.05  # 5% free-stream turbulence intensity


@dataclasses.dataclass
class CaseInfo:
    """Everything the runner + post-processor need that isn't on disk."""
    case_dir: str
    R: np.ndarray               # (3,3) world-mm -> aligned rotation (flow -> +X)
    scale_m: float              # mm -> m (always 1e-3); case-m = (R @ world_mm) * scale_m
    speed_mps: float
    rho_kgm3: float
    nu_m2s: float
    reynolds: float
    char_len_m: float
    frontal_area_mm2: float
    ref_area_mm2: float
    drag_dir: Tuple[float, float, float]   # +X in case space
    lift_dir: Tuple[float, float, float]   # +Z in case space (world up-ish after align)
    side_dir: Tuple[float, float, float]   # +Y in case space
    iters: int
    n_proc: int
    part_patch: str = "part"

    def world_mm_to_case_m(self, pts_mm: np.ndarray) -> np.ndarray:
        """Map world-space mm points into the OpenFOAM case (aligned, metres)."""
        p = np.asarray(pts_mm, dtype=np.float64).reshape(-1, 3)
        return (self.R @ p.T).T * self.scale_m

    def case_m_to_world_mm(self, pts_m: np.ndarray) -> np.ndarray:
        """Inverse: OpenFOAM case (aligned, metres) -> world mm (for streamline overlays)."""
        p = np.asarray(pts_m, dtype=np.float64).reshape(-1, 3)
        return (self.R.T @ (p / self.scale_m).T).T


# --------------------------------------------------------------------------- dict header

def _header(cls: str, obj: str, loc: str = "") -> str:
    locline = f'    location    "{loc}";\n' if loc else ""
    return (
        "/*--------------------------------*- C++ -*----------------------------------*\\\n"
        "| AI-CAD OpenFOAM bridge - generated case                                     |\n"
        "\\*---------------------------------------------------------------------------*/\n"
        "FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
        f"    class       {cls};\n{locline}    object      {obj};\n}}\n"
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
    )


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii", newline="\n")


# --------------------------------------------------------------------------- builder

def build_case(case_dir: str, stl_path: str,
               direction=(1.0, 0.0, 0.0), speed_mps: float = 10.0,
               preset: str = "coarse", aoa_deg: float = 0.0, sideslip_deg: float = 0.0,
               fluid: Optional[dict] = None, ref_area_mm2: float = 0.0,
               n_proc: int = 1) -> CaseInfo:
    """Write a full OpenFOAM case under ``case_dir``; return its :class:`CaseInfo`."""
    pre = _PRESETS.get(preset, _PRESETS["coarse"])
    case = Path(case_dir)

    # Fluid props (mirror lbm defaults).
    f = {"density_kgm3": 1.225, "kinematic_visc_m2s": 1.5e-5}
    if fluid:
        f.update({k: v for k, v in fluid.items() if v is not None})
    rho = float(f["density_kgm3"]) or 1.225
    nu = float(f["kinematic_visc_m2s"]) or 1.5e-5

    # Flow direction (base + AoA + sideslip), then align part so flow -> +X.
    flow_dir = _compose_flow_dir(np.asarray(direction, float), aoa_deg, sideslip_deg)
    world_mesh = trimesh.load(stl_path, force="mesh")
    if not isinstance(world_mesh, trimesh.Trimesh):
        raise ValueError("STL did not load as a single mesh")
    aligned, R = _align_to_x(world_mesh, flow_dir)     # rotation about origin (mm)

    bb_min, bb_max = aligned.bounds                    # mm
    size = bb_max - bb_min                             # mm
    longest = float(np.max(size))
    char_len_m = longest * 1e-3
    reynolds = speed_mps * char_len_m / nu

    # Domain padding (mm) — same convention as the LBM wind tunnel.
    up = max(longest, 10.0)
    dn = max(3.0 * longest, 30.0)
    lat = max(longest, 10.0)
    box_min = np.array([bb_min[0] - up,  bb_min[1] - lat, bb_min[2] - lat])  # mm
    box_max = np.array([bb_max[0] + dn,  bb_max[1] + lat, bb_max[2] + lat])  # mm
    box_min_m, box_max_m = box_min * 1e-3, box_max * 1e-3

    # Frontal (projected) area for coefficients — projection onto the YZ plane of the
    # aligned mesh (flow is +X), in mm².  Robust union area via trimesh's 2D projection.
    frontal_mm2 = _projected_area_mm2(aligned)
    ref_mm2 = ref_area_mm2 if ref_area_mm2 > 0 else frontal_mm2
    aref_m2 = max(ref_mm2 * 1e-6, 1e-9)

    # --- constant/triSurface/part.stl : aligned + scaled to metres ---
    part_m = aligned.copy()
    part_m.apply_scale(1e-3)
    tri_dir = case / "constant" / "triSurface"
    tri_dir.mkdir(parents=True, exist_ok=True)
    part_m.export(str(tri_dir / "part.stl"))

    # --- background mesh sizing (roughly cubic cells) ---
    box_size_m = box_max_m - box_min_m
    longest_box_m = float(np.max(box_size_m))
    base = longest_box_m / pre["bg"]
    nx = max(4, int(round(box_size_m[0] / base)))
    ny = max(4, int(round(box_size_m[1] / base)))
    nz = max(4, int(round(box_size_m[2] / base)))

    # Turbulence inlet values.
    u = float(speed_mps)
    k0 = 1.5 * (_TURB_INTENSITY * u) ** 2
    turb_len = 0.07 * char_len_m
    omega0 = math.sqrt(k0) / (_CMU ** 0.25 * max(turb_len, 1e-6))
    nut0 = k0 / max(omega0, 1e-9)

    # location-in-mesh: upstream of the part, safely inside the fluid.
    loc = (box_min_m[0] + 0.4 * up * 1e-3,
           0.5 * (box_min_m[1] + box_max_m[1]),
           0.5 * (box_min_m[2] + box_max_m[2]))

    iters = pre["iters"]

    # ---- write everything ----
    _write_blockmesh(case, box_min_m, box_max_m, nx, ny, nz)
    _write_snappy(case, pre["refine"], pre["layers"], loc)
    _write_surface_features(case)
    _write_mesh_quality(case)
    _write_transport(case, nu)
    _write_turbulence(case)
    _write_control_dict(case, iters, rho, u, aref_m2, char_len_m)
    _write_fv_schemes(case)
    _write_fv_solution(case)
    _write_decompose(case, n_proc)
    _write_fields(case, u, k0, omega0, nut0)

    return CaseInfo(
        case_dir=str(case), R=R, scale_m=1e-3, speed_mps=u, rho_kgm3=rho, nu_m2s=nu,
        reynolds=reynolds, char_len_m=char_len_m, frontal_area_mm2=frontal_mm2,
        ref_area_mm2=ref_mm2, drag_dir=(1.0, 0.0, 0.0), lift_dir=(0.0, 0.0, 1.0),
        side_dir=(0.0, 1.0, 0.0), iters=iters, n_proc=n_proc,
    )


def _projected_area_mm2(aligned_mesh: trimesh.Trimesh, n: int = 256) -> float:
    """Frontal (silhouette) area in mm² of the aligned mesh projected onto the YZ plane.

    Rasterises the triangles into an n×n coverage mask (z-buffer style) and multiplies the
    covered-pixel fraction by the YZ bbox area.  Dependency-free (pure numpy) and correct for
    concave/holed silhouettes, mirroring the LBM's voxel-projection frontal area.
    """
    lo, hi = aligned_mesh.bounds
    y0, z0 = lo[1], lo[2]
    dy, dz = (hi[1] - y0), (hi[2] - z0)
    if dy <= 0 or dz <= 0:
        return 0.0
    tris = aligned_mesh.triangles[:, :, 1:3]           # (m,3,2) -> (y,z) only
    # to pixel coords [0,n)
    px = (tris[:, :, 0] - y0) / dy * (n - 1)
    py = (tris[:, :, 1] - z0) / dz * (n - 1)
    mask = np.zeros((n, n), dtype=bool)
    for i in range(px.shape[0]):
        ax, ay = px[i, 0], py[i, 0]
        bx, by = px[i, 1], py[i, 1]
        cx, cy = px[i, 2], py[i, 2]
        xmin = max(0, int(np.floor(min(ax, bx, cx))))
        xmax = min(n - 1, int(np.ceil(max(ax, bx, cx))))
        ymin = max(0, int(np.floor(min(ay, by, cy))))
        ymax = min(n - 1, int(np.ceil(max(ay, by, cy))))
        if xmax < xmin or ymax < ymin:
            continue
        gx, gy = np.meshgrid(np.arange(xmin, xmax + 1), np.arange(ymin, ymax + 1))
        # barycentric sign test
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            continue
        a = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / d
        b = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / d
        c = 1.0 - a - b
        inside = (a >= 0) & (b >= 0) & (c >= 0)
        mask[gy[inside], gx[inside]] = True
    frac = float(np.count_nonzero(mask)) / (n * n)
    return frac * dy * dz


# --------------------------------------------------------------------------- system/ files

def _write_blockmesh(case: Path, lo, hi, nx, ny, nz) -> None:
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    verts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    vtxt = "\n".join(f"    ({v[0]:.6g} {v[1]:.6g} {v[2]:.6g})" for v in verts)
    body = _header("dictionary", "blockMeshDict", "system")
    body += "convertToMeters 1;\n\n"
    body += f"vertices\n(\n{vtxt}\n);\n\n"
    body += (f"blocks\n(\n    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) "
             "simpleGrading (1 1 1)\n);\n\n")
    body += "edges\n(\n);\n\n"
    # inlet=-X, outlet=+X, sides = the 4 lateral faces grouped.
    body += (
        "boundary\n(\n"
        "    inlet\n    {\n        type patch;\n        faces ( (0 4 7 3) );\n    }\n"
        "    outlet\n    {\n        type patch;\n        faces ( (1 2 6 5) );\n    }\n"
        "    sides\n    {\n        type patch;\n        faces\n        (\n"
        "            (0 1 5 4)\n            (3 7 6 2)\n"
        "            (0 3 2 1)\n            (4 5 6 7)\n        );\n    }\n"
        ");\n\n"
        "mergePatchPairs\n(\n);\n"
    )
    _w(case / "system" / "blockMeshDict", body)


def _write_snappy(case: Path, refine, layers, loc) -> None:
    lvl_min, lvl_max = refine
    lx, ly, lz = loc
    add_layers = "true" if layers > 0 else "false"
    body = _header("dictionary", "snappyHexMeshDict", "system")
    body += (
        f"castellatedMesh true;\nsnap            true;\naddLayers       {add_layers};\n\n"
        "geometry\n{\n    part\n    {\n        type triSurfaceMesh;\n"
        "        file \"part.stl\";\n    }\n}\n\n"
        "castellatedMeshControls\n{\n"
        "    maxLocalCells 1000000;\n    maxGlobalCells 4000000;\n"
        "    minRefinementCells 10;\n    maxLoadUnbalance 0.10;\n    nCellsBetweenLevels 3;\n\n"
        "    features\n    (\n        { file \"part.eMesh\"; level " f"{lvl_max}" "; }\n    );\n\n"
        "    refinementSurfaces\n    {\n        part\n        {\n"
        f"            level ({lvl_min} {lvl_max});\n"
        "            patchInfo { type wall; }\n        }\n    }\n\n"
        "    resolveFeatureAngle 30;\n\n"
        "    refinementRegions\n    {\n    }\n\n"
        f"    locationInMesh ({lx:.6g} {ly:.6g} {lz:.6g});\n"
        "    allowFreeStandingZoneFaces true;\n}\n\n"
        "snapControls\n{\n    nSmoothPatch 3;\n    tolerance 2.0;\n    nSolveIter 50;\n"
        "    nRelaxIter 5;\n    nFeatureSnapIter 10;\n    implicitFeatureSnap false;\n"
        "    explicitFeatureSnap true;\n    multiRegionFeatureSnap false;\n}\n\n"
        "addLayersControls\n{\n    relativeSizes true;\n    layers\n    {\n        part\n"
        f"        {{ nSurfaceLayers {max(layers,1)}; }}\n    }}\n"
        "    expansionRatio 1.2;\n    finalLayerThickness 0.5;\n    minThickness 0.05;\n"
        "    nGrow 0;\n    featureAngle 130;\n    slipFeatureAngle 30;\n    nRelaxIter 5;\n"
        "    nSmoothSurfaceNormals 1;\n    nSmoothNormals 3;\n    nSmoothThickness 10;\n"
        "    maxFaceThicknessRatio 0.5;\n    maxThicknessToMedialRatio 0.3;\n"
        "    minMedialAxisAngle 90;\n    nBufferCellsNoExtrude 0;\n    nLayerIter 50;\n}\n\n"
        "meshQualityControls\n{\n    #include \"meshQualityDict\"\n}\n\n"
        "writeFlags ( scalarLevels layerSets layerFields );\nmergeTolerance 1e-6;\n"
    )
    _w(case / "system" / "snappyHexMeshDict", body)


def _write_surface_features(case: Path) -> None:
    # v2412 ships `surfaceFeatureExtract` (reads surfaceFeatureExtractDict); it writes
    # constant/triSurface/part.eMesh which snappy's `features` block references.
    body = _header("dictionary", "surfaceFeatureExtractDict", "system")
    body += (
        "part.stl\n{\n"
        "    extractionMethod    extractFromSurface;\n"
        "    extractFromSurfaceCoeffs { includedAngle 150; }\n"
        "    writeObj            no;\n}\n"
    )
    _w(case / "system" / "surfaceFeatureExtractDict", body)


def _write_mesh_quality(case: Path) -> None:
    body = _header("dictionary", "meshQualityDict", "system")
    body += (
        "maxNonOrtho 65;\nmaxBoundarySkewness 20;\nmaxInternalSkewness 4;\n"
        "maxConcave 80;\nminVol 1e-13;\nminTetQuality 1e-15;\nminArea -1;\n"
        "minTwist 0.02;\nminDeterminant 0.001;\nminFaceWeight 0.05;\nminVolRatio 0.01;\n"
        "minTriangleTwist -1;\nnSmoothScale 4;\nerrorReduction 0.75;\n"
        "relaxed { maxNonOrtho 75; }\n"
    )
    _w(case / "system" / "meshQualityDict", body)


def _write_control_dict(case: Path, iters, rho, u, aref_m2, lref_m) -> None:
    body = _header("dictionary", "controlDict", "system")
    body += (
        "application     simpleFoam;\nstartFrom       startTime;\nstartTime       0;\n"
        f"stopAt          endTime;\nendTime         {iters};\ndeltaT          1;\n"
        f"writeControl    timeStep;\nwriteInterval   {iters};\npurgeWrite      2;\n"
        "writeFormat     ascii;\nwritePrecision  7;\nwriteCompression off;\n"
        "timeFormat      general;\ntimePrecision   6;\nrunTimeModifiable true;\n\n"
        "functions\n{\n"
        "    forceCoeffs\n    {\n        type            forceCoeffs;\n"
        "        libs            (\"libforces.so\");\n        writeControl    timeStep;\n"
        "        writeInterval   1;\n        patches         (part);\n"
        "        rho             rhoInf;\n"
        f"        rhoInf          {rho:.6g};\n"
        "        liftDir         (0 0 1);\n        dragDir         (1 0 0);\n"
        "        CofR            (0 0 0);\n        pitchAxis       (0 1 0);\n"
        f"        magUInf         {u:.6g};\n        lRef            {lref_m:.6g};\n"
        f"        Aref            {aref_m2:.6g};\n    }}\n"
        "}\n"
    )
    _w(case / "system" / "controlDict", body)


def _write_fv_schemes(case: Path) -> None:
    body = _header("dictionary", "fvSchemes", "system")
    body += (
        "ddtSchemes      { default steadyState; }\n"
        "gradSchemes     { default Gauss linear; grad(U) cellLimited Gauss linear 1; }\n"
        "divSchemes\n{\n    default none;\n"
        "    div(phi,U)      bounded Gauss linearUpwindV grad(U);\n"
        "    div(phi,k)      bounded Gauss upwind;\n"
        "    div(phi,omega)  bounded Gauss upwind;\n"
        "    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n}\n"
        "laplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes   { default corrected; }\n"
        "wallDist        { method meshWave; }\n"
    )
    _w(case / "system" / "fvSchemes", body)


def _write_fv_solution(case: Path) -> None:
    body = _header("dictionary", "fvSolution", "system")
    body += (
        "solvers\n{\n"
        "    p\n    {\n        solver GAMG;\n        smoother GaussSeidel;\n"
        "        tolerance 1e-7;\n        relTol 0.01;\n    }\n"
        "    \"(U|k|omega)\"\n    {\n        solver smoothSolver;\n"
        "        smoother symGaussSeidel;\n        tolerance 1e-8;\n        relTol 0.1;\n    }\n}\n\n"
        "SIMPLE\n{\n    nNonOrthogonalCorrectors 1;\n    consistent yes;\n"
        "    residualControl\n    {\n        p 1e-4;\n        U 1e-4;\n"
        "        \"(k|omega)\" 1e-4;\n    }\n}\n\n"
        "relaxationFactors\n{\n    equations\n    {\n        U 0.9;\n"
        "        \"(k|omega)\" 0.7;\n    }\n}\n"
    )
    _w(case / "system" / "fvSolution", body)


def _write_decompose(case: Path, n_proc: int) -> None:
    n = max(1, int(n_proc))
    body = _header("dictionary", "decomposeParDict", "system")
    body += f"numberOfSubdomains {n};\nmethod scotch;\n"
    _w(case / "system" / "decomposeParDict", body)


# --------------------------------------------------------------------------- 0/ fields

def _write_fields(case: Path, u, k0, omega0, nut0) -> None:
    z = case / "0"
    # U
    _w(z / "U", _header("volVectorField", "U", "0") +
       "dimensions      [0 1 -1 0 0 0 0];\n"
       f"internalField   uniform ({u:.6g} 0 0);\n\n"
       "boundaryField\n{\n"
       f"    inlet   {{ type fixedValue; value uniform ({u:.6g} 0 0); }}\n"
       "    outlet  { type inletOutlet; inletValue uniform (0 0 0); value uniform (0 0 0); }\n"
       "    sides   { type slip; }\n"
       "    part    { type noSlip; }\n}\n")
    # p (kinematic)
    _w(z / "p", _header("volScalarField", "p", "0") +
       "dimensions      [0 2 -2 0 0 0 0];\ninternalField   uniform 0;\n\n"
       "boundaryField\n{\n"
       "    inlet   { type zeroGradient; }\n"
       "    outlet  { type fixedValue; value uniform 0; }\n"
       "    sides   { type slip; }\n"
       "    part    { type zeroGradient; }\n}\n")
    # k
    _w(z / "k", _header("volScalarField", "k", "0") +
       "dimensions      [0 2 -2 0 0 0 0];\n"
       f"internalField   uniform {k0:.6g};\n\n"
       "boundaryField\n{\n"
       f"    inlet   {{ type fixedValue; value uniform {k0:.6g}; }}\n"
       f"    outlet  {{ type inletOutlet; inletValue uniform {k0:.6g}; value uniform {k0:.6g}; }}\n"
       "    sides   { type slip; }\n"
       f"    part    {{ type kqRWallFunction; value uniform {k0:.6g}; }}\n}}\n")
    # omega
    _w(z / "omega", _header("volScalarField", "omega", "0") +
       "dimensions      [0 0 -1 0 0 0 0];\n"
       f"internalField   uniform {omega0:.6g};\n\n"
       "boundaryField\n{\n"
       f"    inlet   {{ type fixedValue; value uniform {omega0:.6g}; }}\n"
       f"    outlet  {{ type inletOutlet; inletValue uniform {omega0:.6g}; value uniform {omega0:.6g}; }}\n"
       "    sides   { type slip; }\n"
       f"    part    {{ type omegaWallFunction; value uniform {omega0:.6g}; }}\n}}\n")
    # nut
    _w(z / "nut", _header("volScalarField", "nut", "0") +
       "dimensions      [0 2 -1 0 0 0 0];\ninternalField   uniform 0;\n\n"
       "boundaryField\n{\n"
       "    inlet   { type calculated; value uniform 0; }\n"
       "    outlet  { type calculated; value uniform 0; }\n"
       "    sides   { type calculated; value uniform 0; }\n"
       "    part    { type nutkWallFunction; value uniform 0; }\n}\n")


def _write_transport(case: Path, nu: float) -> None:
    body = _header("dictionary", "transportProperties", "constant")
    body += f"transportModel  Newtonian;\nnu              [0 2 -1 0 0 0 0] {nu:.6g};\n"
    _w(case / "constant" / "transportProperties", body)


def _write_turbulence(case: Path) -> None:
    body = _header("dictionary", "turbulenceProperties", "constant")
    body += (
        "simulationType  RAS;\n\nRAS\n{\n    RASModel        kOmegaSST;\n"
        "    turbulence      on;\n    printCoeffs     on;\n}\n"
    )
    _w(case / "constant" / "turbulenceProperties", body)
