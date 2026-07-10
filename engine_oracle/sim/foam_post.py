"""Post-process a finished OpenFOAM run into a FlowResult + viewport overlay.

Reads the converged case with pyvista's native OpenFOAM reader (no foamToVTK hop) and:

* parses ``postProcessing/forceCoeffs/.../coefficient.dat`` for Cd / Cl, turning them into
  drag / lift forces via the same reference area the UI passed;
* maps the part-patch kinematic pressure to a per-vertex Cp on the world-space surface and
  packs it as the ``pressure_cp`` SCLR chunk;
* integrates a few forward streamlines through the volume and packs them as a ``streamlines``
  LINS chunk coloured by speed.

The overlay AICM is written with the *same* tessellate helpers the LBM overlay uses
(engine/sim/overlay.py + engine/doc/tessellate.py), so the viewport renders it unchanged.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from engine.sim.foam_case import CaseInfo


# --------------------------------------------------------------------------- forces

def parse_coefficients(case_dir: str) -> Tuple[float, float]:
    """Return (Cd, Cl) from the last row of the forceCoeffs coefficient.dat.

    coefficient.dat columns: time  Cd  Cd(f)  Cd(r)  Cl  Cl(f)  Cl(r)  Cm...  (ESI v2412).
    Falls back to (0, 0) if the file is missing/empty.
    """
    pp = Path(case_dir) / "postProcessing"
    dats = list(pp.glob("forceCoeffs*/**/coefficient.dat")) or list(pp.glob("**/coefficient.dat"))
    if not dats:
        return 0.0, 0.0
    last = None
    for line in dats[0].read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        last = line
    if not last:
        return 0.0, 0.0
    cols = last.split()
    try:
        cd = float(cols[1])
        cl = float(cols[4])
        return cd, cl
    except (IndexError, ValueError):
        return 0.0, 0.0


# --------------------------------------------------------------------------- pyvista read

def _read_case(case_dir: str):
    """Open the case at its last time step.  Returns (internalMesh, {patch: poly})."""
    import pyvista as pv

    foam = Path(case_dir) / "case.foam"
    if not foam.exists():
        foam.write_text("")
    reader = pv.OpenFOAMReader(str(foam))
    if reader.time_values:
        reader.set_active_time_value(reader.time_values[-1])
    reader.cell_to_point_creation = True
    ds = reader.read()
    internal = ds["internalMesh"]
    patches = {}
    if "boundary" in ds.keys():
        b = ds["boundary"]
        for name in b.keys():
            patches[name] = b[name]
    return internal, patches


def _sample_on_surface(world_verts_mm: np.ndarray, info: CaseInfo, part_poly,
                       values: np.ndarray) -> np.ndarray:
    """Map a part-patch point field onto world-space surface vertices (nearest neighbour)."""
    from scipy.spatial import cKDTree

    pts_m = np.asarray(part_poly.points, dtype=np.float64)
    tree = cKDTree(pts_m)
    q = info.world_mm_to_case_m(world_verts_mm)
    _, idx = tree.query(q, k=1)
    return np.asarray(values, dtype=np.float64)[idx]


def _cp_on_surface(world_verts_mm: np.ndarray, info: CaseInfo, part_poly, q_kin: float) -> np.ndarray:
    """Per-world-vertex Cp by nearest-neighbour from the part-patch pressure (case-m space)."""
    p_kin = np.asarray(part_poly.point_data["p"], dtype=np.float64)   # kinematic p (m^2/s^2)
    return (_sample_on_surface(world_verts_mm, info, part_poly, p_kin) / max(q_kin, 1e-9)).astype(np.float32)


def parse_wall_heat_flux(case_dir: str):
    """Total wall heat flow (W) from the wallHeatFlux function object, or None."""
    pp = Path(case_dir) / "postProcessing"
    dats = list(pp.glob("wallHeatFlux*/**/wallHeatFlux.dat"))
    if not dats:
        return None
    last = None
    for line in dats[0].read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        last = s
    if not last:
        return None
    # columns: Time patch min max integral  (integral = total heat flow in W)
    cols = last.split()
    try:
        return float(cols[-1])
    except (IndexError, ValueError):
        return None


def _streamlines(internal, info: CaseInfo, n_seeds: int = 60
                 ) -> Tuple[List[np.ndarray], List[np.ndarray], float]:
    """Integrate forward streamlines through the volume; return (polys_mm, speeds_mps, vmax)."""
    import pyvista as pv

    U = np.asarray(internal.point_data["U"], dtype=np.float64)
    vmax = float(np.linalg.norm(U, axis=1).max()) if U.size else info.speed_mps

    # Seed a grid in the YZ plane just downstream of the inlet, spanning the domain cross-section.
    b = internal.bounds  # (xmin,xmax,ymin,ymax,zmin,zmax) in metres
    x_seed = b[0] + 0.15 * (b[1] - b[0])
    ys = np.linspace(b[2] + 0.1 * (b[3] - b[2]), b[3] - 0.1 * (b[3] - b[2]), int(math.sqrt(n_seeds)))
    zs = np.linspace(b[4] + 0.1 * (b[5] - b[4]), b[5] - 0.1 * (b[5] - b[4]), int(math.sqrt(n_seeds)))
    seeds = np.array([[x_seed, y, z] for y in ys for z in zs], dtype=np.float64)
    seed_poly = pv.PolyData(seeds)

    try:
        strm = internal.streamlines_from_source(
            seed_poly, vectors="U", integration_direction="forward",
            max_length=2.0 * (b[1] - b[0]),
        )
    except Exception:
        return [], [], vmax
    if strm is None or strm.n_points == 0:
        return [], [], vmax

    polys_mm: List[np.ndarray] = []
    speeds: List[np.ndarray] = []
    lines = strm.lines
    spd_pt = np.linalg.norm(np.asarray(strm.point_data["U"], dtype=np.float64), axis=1)
    pts = np.asarray(strm.points, dtype=np.float64)
    i = 0
    while i < len(lines):
        n = lines[i]
        ids = lines[i + 1:i + 1 + n]
        i += n + 1
        if n < 2:
            continue
        seg_m = pts[ids]
        polys_mm.append(info.case_m_to_world_mm(seg_m).astype(np.float32))
        speeds.append(spd_pt[ids].astype(np.float32))
    return polys_mm, speeds, vmax


# --------------------------------------------------------------------------- overlay

def build_overlay(stl_path: str, info: CaseInfo, internal, patches,
                  cp_min: float, cp_max: float, temp_range=None
                  ) -> Tuple[str, List[str], List[np.ndarray], List[np.ndarray]]:
    """Write the flow overlay AICM; return (path, scalar_fields, streamline_pts, streamline_speeds).

    Aero case: a ``pressure_cp`` SCLR. Thermal case: a ``temperature`` SCLR (degC). Both add a
    ``velocity`` LINS of streamlines.
    """
    from engine.sim import overlay as ov
    from engine.doc import tessellate as tess

    bt = ov._tessellate_stl_world(stl_path)
    surf = ov._surface_verts(bt)
    chunks: list = []
    scalar_fields: List[str] = []
    part = patches.get(info.part_patch)

    if info.thermal:
        if part is not None and "T" in part.point_data:
            try:
                t_c = _sample_on_surface(surf, info, part,
                                         np.asarray(part.point_data["T"], dtype=np.float64) - 273.15)
                lo, hi = (temp_range or (float(t_c.min()), float(t_c.max())))
                chunks.append(tess.scalar_chunk(1, "temperature", t_c.astype(np.float32),
                                                vmin=lo, vmax=hi))
                scalar_fields.append("temperature")
            except Exception:
                pass
    else:
        q_kin = 0.5 * info.speed_mps * info.speed_mps
        if part is not None and "p" in part.point_data:
            try:
                cp = np.clip(_cp_on_surface(surf, info, part, q_kin), cp_min, cp_max)
                chunks.append(tess.scalar_chunk(1, "pressure_cp", cp, vmin=cp_min, vmax=cp_max))
                scalar_fields.append("pressure_cp")
            except Exception:
                pass

    sl_pts, sl_spd, _ = _streamlines(internal, info)
    if sl_pts:
        ch = ov.streamlines_chunk(sl_pts, sl_spd)
        if ch is not None:
            chunks.append(ch)
            scalar_fields.append("velocity")

    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
        path = tf.name
    tess.write_aicm_file(path, [bt], extra_chunks=chunks)
    return path, scalar_fields, sl_pts, sl_spd


# --------------------------------------------------------------------------- top level

def _read_vtm_combined(pattern_dir: Path, exclude=("boundary",)):
    """Read the first non-excluded .vtm under a foamToVTK region dir; return a merged dataset."""
    import pyvista as pv
    vtms = [p for p in pattern_dir.glob("*.vtm")
            if not any(x in p.stem for x in exclude)]
    if not vtms:
        return None
    mb = pv.read(str(sorted(vtms)[-1]))
    try:
        return mb.combine() if hasattr(mb, "combine") else mb
    except Exception:
        return mb


def _xyz_and_T(ds):
    """(points, T) from a dataset, handling foamToVTK cell-data OR point-data layouts."""
    if ds is None or ds.n_points == 0:
        return None, None
    if "T" in ds.point_data:
        return np.asarray(ds.points, dtype=np.float64), np.asarray(ds.point_data["T"], dtype=np.float64)
    if "T" in ds.cell_data:
        try:
            cc = ds.cell_centers()
            return np.asarray(cc.points, dtype=np.float64), np.asarray(ds.cell_data["T"], dtype=np.float64)
        except Exception:
            return None, None
    return None, None


def postprocess_cht(case_dir: str, stl_path: str, info):
    """Assemble a FlowResult from a finished chtMultiRegionFoam (CHT) run.

    Reads the solid region temperature (foamToVTK export), reports the part's hot/mean
    temperature + dissipated power, and colours the part surface by temperature.
    """
    from engine.sim.lbm import FlowResult
    from engine.sim import overlay as ov
    from engine.doc import tessellate as tess

    cd = Path(case_dir)
    Tin_c = info.inlet_temp_k - 273.15
    t_min = t_max = t_mean = Tin_c
    regions = list(getattr(info, "solid_regions", None) or [info.solid_region])

    # Aggregate solid temperatures over EVERY solid region (multi-solid CHT, T-FIN19),
    # keeping a per-region summary for the notes.
    per_region: List[str] = []
    all_xyz: list = []
    all_T: list = []
    got_any = False
    for reg in regions:
        solid = _read_vtm_combined(cd / "VTK" / reg)
        xyz, Tsolid = _xyz_and_T(solid)
        if Tsolid is None or not Tsolid.size:
            per_region.append(f"{reg}: no data")
            continue
        Tc = Tsolid - 273.15
        rmin, rmax, rmean = float(Tc.min()), float(Tc.max()), float(Tc.mean())
        per_region.append(f"{reg}: {rmean:.0f} C mean, {rmax:.0f} C peak")
        if not got_any:
            t_min, t_max, t_mean = rmin, rmax, rmean
            got_any = True
        else:
            t_min = min(t_min, rmin)
            t_max = max(t_max, rmax)
        if xyz is not None:
            all_xyz.append(xyz)
            all_T.append(Tsolid)

    # Surface temperature overlay: map the solids' outer-surface (boundary patch) T onto the
    # world STL vertices.  Prefer the boundary patches; fall back to the solid cell fields.
    bt = ov._tessellate_stl_world(stl_path)
    surf = ov._surface_verts(bt)
    chunks: list = []
    scalar_fields: List[str] = []
    b_xyz: list = []
    b_T: list = []
    import pyvista as pv
    for reg in regions:
        for bvtm in list((cd / "VTK" / reg).rglob("*boundary*.vtm"))[-1:]:
            try:
                bmb = pv.read(str(bvtm))
                bnd = bmb.combine() if hasattr(bmb, "combine") else bmb
                xyz, T = _xyz_and_T(bnd)
                if xyz is not None and T is not None and T.size:
                    b_xyz.append(xyz)
                    b_T.append(T)
            except Exception:
                pass
    if b_xyz:
        src_xyz = np.concatenate(b_xyz)
        src_T = np.concatenate(b_T)
    elif all_xyz:                            # fall back to the solid volume fields
        src_xyz = np.concatenate(all_xyz)
        src_T = np.concatenate(all_T)
    else:
        src_xyz, src_T = None, None
    try:
        if src_xyz is not None and src_T.size:
            from scipy.spatial import cKDTree
            Tc_b = src_T - 273.15
            tree = cKDTree(src_xyz)
            _, idx = tree.query(info.world_mm_to_case_m(surf), k=1)
            t_surf = Tc_b[idx].astype(np.float32)
            lo, hi = float(min(Tin_c, t_surf.min())), float(max(t_surf.max(), Tin_c + 1.0))
            chunks.append(tess.scalar_chunk(1, "temperature", t_surf, vmin=lo, vmax=hi))
            scalar_fields.append("temperature")
    except Exception:
        pass

    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
        overlay_path = tf.name
    tess.write_aicm_file(overlay_path, [bt], extra_chunks=chunks)

    what = "solid" if len(regions) == 1 else f"{len(regions)} solids"
    re_note = (f"OpenFOAM CHT (conjugate) - Re {info.reynolds:,.0f}; {info.power_w:.1f} W in {what} "
               f"(k={info.solid_kappa:.0f} W/mK), inlet {Tin_c:.0f} C -> part {t_mean:.0f} C "
               f"(peak {t_max:.0f} C).")
    notes = [f"chtMultiRegionFoam coupled solid+fluid; solid temperature {t_min:.0f}-{t_max:.0f} C."]
    if len(regions) > 1:
        notes.append("per-region: " + "; ".join(per_region) + ".")
    return FlowResult(
        drag_n=0.0, lift_n=0.0, cd=0.0, frontal_area_mm2=info.frontal_area_mm2,
        reynolds_note=re_note, trace=[], slices=[], slice_transforms=[],
        streamline_points=[], streamline_speeds=[],
        diverged=False, steps_run=0, side_n=0.0, cl=0.0, ld=0.0, reynolds=info.reynolds,
        ref_area_mm2=info.frontal_area_mm2, cp_min=t_min, cp_max=t_max, vmax_mps=info.speed_mps,
        solver_notes=notes, overlay_path=overlay_path, scalar_fields=scalar_fields,
    )


def postprocess(case_dir: str, stl_path: str, info: CaseInfo, converged: bool,
                solver_notes: Optional[List[str]] = None):
    """Assemble a FlowResult from a finished OpenFOAM run."""
    from engine.sim.lbm import FlowResult

    internal, patches = _read_case(case_dir)
    part = patches.get(info.part_patch)
    U = np.asarray(internal.point_data.get("U", np.zeros((1, 3))), dtype=np.float64)
    vmax = float(np.linalg.norm(U, axis=1).max()) if U.size else info.speed_mps
    notes = list(solver_notes or [])
    conv = "converged" if converged else "did not fully converge"

    if info.thermal:
        # Temperature legend (degC) from the part wall.
        t_lo, t_hi = info.inlet_temp_k - 273.15, info.inlet_temp_k - 273.15 + 1.0
        if part is not None and "T" in part.point_data:
            t_c = np.asarray(part.point_data["T"], dtype=np.float64) - 273.15
            if t_c.size:
                t_lo, t_hi = float(t_c.min()), float(t_c.max())
        heat_w = parse_wall_heat_flux(case_dir)
        overlay_path, scalar_fields, sl_pts, sl_spd = build_overlay(
            stl_path, info, internal, patches, -1.0, 1.0, temp_range=(t_lo, t_hi))
        heat_txt = f", wall heat flow {heat_w:.1f} W" if heat_w is not None else ""
        re_note = (f"OpenFOAM buoyant RANS - Re {info.reynolds:,.0f}; inlet "
                   f"{info.inlet_temp_k - 273.15:.0f} C; surface T {t_lo:.0f}-{t_hi:.0f} C{heat_txt}.")
        notes.append(f"OpenFOAM {info.iters}-iter buoyant RANS (k-omega SST, heat transfer); {conv}.")
        return FlowResult(
            drag_n=0.0, lift_n=0.0, cd=0.0, frontal_area_mm2=info.frontal_area_mm2,
            reynolds_note=re_note, trace=[], slices=[], slice_transforms=[],
            streamline_points=sl_pts, streamline_speeds=sl_spd,
            diverged=not converged, steps_run=info.iters,
            side_n=0.0, cl=0.0, ld=0.0, reynolds=info.reynolds, ref_area_mm2=info.ref_area_mm2,
            cp_min=t_lo, cp_max=t_hi, vmax_mps=vmax, solver_notes=notes,
            overlay_path=overlay_path, scalar_fields=scalar_fields,
        )

    # ---- aero ----
    cd, cl = parse_coefficients(case_dir)
    q = 0.5 * info.rho_kgm3 * info.speed_mps ** 2
    aref_m2 = max(info.ref_area_mm2 * 1e-6, 1e-9)
    drag_n = cd * q * aref_m2
    lift_n = cl * q * aref_m2
    ld = (cl / cd) if abs(cd) > 1e-9 else 0.0

    cp_min, cp_max = -1.0, 1.0
    if part is not None and "p" in part.point_data:
        q_kin = max(0.5 * info.speed_mps ** 2, 1e-9)
        cp_all = np.asarray(part.point_data["p"], dtype=np.float64) / q_kin
        if cp_all.size:
            cp_min = float(min(-0.5, np.percentile(cp_all, 1)))
            cp_max = float(max(1.0, np.percentile(cp_all, 99)))

    overlay_path, scalar_fields, sl_pts, sl_spd = build_overlay(
        stl_path, info, internal, patches, cp_min, cp_max)

    notes.append(f"OpenFOAM {info.iters}-iter RANS (k-omega SST); {conv}.")
    re_note = (f"OpenFOAM RANS - Re {info.reynolds:,.0f}; "
               f"Cd {cd:.3f}, Cl {cl:.3f} on Aref {info.ref_area_mm2:.0f} mm2.")
    return FlowResult(
        drag_n=drag_n, lift_n=lift_n, cd=cd, frontal_area_mm2=info.frontal_area_mm2,
        reynolds_note=re_note, trace=[], slices=[], slice_transforms=[],
        streamline_points=sl_pts, streamline_speeds=sl_spd,
        diverged=not converged, steps_run=info.iters,
        side_n=0.0, cl=cl, ld=ld, reynolds=info.reynolds, ref_area_mm2=info.ref_area_mm2,
        cp_min=cp_min, cp_max=cp_max, vmax_mps=vmax, solver_notes=notes,
        overlay_path=overlay_path, scalar_fields=scalar_fields,
    )
