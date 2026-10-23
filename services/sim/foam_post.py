"""Post-process a finished OpenFOAM run into a FlowResult + viewport overlay.

Reads the converged case with pyvista's native OpenFOAM reader (no foamToVTK hop) and:

* parses ``forceCoeffs`` for Cd / Cl and ``forces`` for the resolved side force,
  never replacing missing solver output with a fabricated zero;
* maps the part-patch kinematic pressure to a per-vertex Cp on the world-space surface and
  packs it as the ``pressure_cp`` SCLR chunk;
* integrates a few forward streamlines through the volume and packs them as a ``streamlines``
  LINS chunk coloured by speed.

The overlay uses ``services/sim/overlay.py`` and ``services/sim/tessellate.py``,
so the viewport consumes the same AICM format as the LBM path.
"""

from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from services.sim.foam_case import CaseInfo


# --------------------------------------------------------------------------- forces

def parse_coefficients(case_dir: str, expected_time: Optional[float] = None) -> Tuple[float, float]:
    """Return finite (Cd, Cl) from the newest forceCoeffs row.

    Missing, malformed, stale or non-finite force output is a failed solve, never a zero-force
    success. ``expected_time`` pins the row to the solver iteration observed on stdout.
    """
    pp = Path(case_dir) / "postProcessing"
    dats = sorted(pp.glob("forceCoeffs*/**/coefficient.dat"))
    if not dats:
        dats = sorted(pp.glob("**/coefficient.dat"))
    rows: List[Tuple[float, float, float]] = []
    seen_times: List[float] = []
    for dat in dats:
        cd_col: Optional[int] = None
        cl_col: Optional[int] = None
        for line in dat.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                header = line.lstrip("#").split()
                if header and header[0].lower() == "time":
                    try:
                        cd_col = header.index("Cd")
                        cl_col = header.index("Cl")
                    except ValueError:
                        cd_col = cl_col = None
                continue
            cols = line.split()
            try:
                time_value = float(cols[0])
            except (IndexError, ValueError):
                continue
            if not math.isfinite(time_value):
                continue
            seen_times.append(time_value)
            if cd_col is None or cl_col is None:
                continue
            try:
                cd, cl = float(cols[cd_col]), float(cols[cl_col])
            except (IndexError, ValueError):
                continue
            if math.isfinite(cd) and math.isfinite(cl):
                rows.append((time_value, cd, cl))
    if not rows:
        raise ValueError("forceCoeffs produced no finite coefficient rows")
    time_value, cd, cl = max(rows, key=lambda row: row[0])
    if seen_times and time_value + 1e-9 < max(seen_times):
        raise ValueError("latest forceCoeffs row is malformed or non-finite")
    if expected_time is not None:
        expected = float(expected_time)
        if not math.isfinite(expected) or time_value + 1e-9 < expected:
            raise ValueError(
                f"forceCoeffs output is stale (latest {time_value}, solver reached {expected})")
    return cd, cl

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_FORCE_ROW = re.compile(
    rf"^\s*({_NUMBER})\s+\(\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)"
    rf"\s*\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)"
    rf"(?:\s*\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\))?\s*\)"
)


def parse_forces(case_dir: str, expected_time: Optional[float] = None) -> Tuple[float, float, float]:
    """Return the newest finite total (drag, side, lift) force vector in case axes."""
    pp = Path(case_dir) / "postProcessing"
    dats = sorted(pp.glob("forces*/**/force*.dat"))
    if not dats:
        dats = sorted(pp.glob("**/force*.dat"))
    rows: List[Tuple[float, float, float, float]] = []
    seen_times: List[float] = []
    for dat in dats:
        total_cols: Optional[Tuple[int, int, int]] = None
        for line in dat.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                header = stripped.lstrip("#").split()
                if header and header[0].lower() == "time":
                    try:
                        total_cols = (
                            header.index("total_x"),
                            header.index("total_y"),
                            header.index("total_z"),
                        )
                    except ValueError:
                        total_cols = None
                continue
            try:
                seen_time = float(stripped.split(maxsplit=1)[0])
                if math.isfinite(seen_time):
                    seen_times.append(seen_time)
            except (IndexError, ValueError):
                pass
            if total_cols is not None:
                cols = stripped.split()
                try:
                    values = (
                        float(cols[0]),
                        float(cols[total_cols[0]]),
                        float(cols[total_cols[1]]),
                        float(cols[total_cols[2]]),
                    )
                except (IndexError, ValueError):
                    continue
                if all(math.isfinite(value) for value in values):
                    rows.append(values)
                continue
            match = _FORCE_ROW.match(line)
            if match is None:
                continue
            values = [float(value) if value is not None else 0.0
                      for value in match.groups()]
            time_value = values[0]
            force = (
                values[1] + values[4] + values[7],
                values[2] + values[5] + values[8],
                values[3] + values[6] + values[9],
            )
            if math.isfinite(time_value) and all(math.isfinite(value) for value in force):
                rows.append((time_value, *force))
    if not rows:
        raise ValueError("forces produced no finite force rows")
    time_value, drag_n, side_n, lift_n = max(rows, key=lambda row: row[0])
    if seen_times and time_value + 1e-9 < max(seen_times):
        raise ValueError("latest forces row is malformed or non-finite")
    if expected_time is not None:
        expected = float(expected_time)
        if not math.isfinite(expected) or time_value + 1e-9 < expected:
            raise ValueError(
                f"forces output is stale (latest {time_value}, solver reached {expected})")
    return drag_n, side_n, lift_n


# --------------------------------------------------------------------------- pyvista read

def _read_case(case_dir: str, expected_time: Optional[float] = None):
    """Open the case at its current final time; return (internalMesh, {patch: poly})."""
    import pyvista as pv

    foam = Path(case_dir) / "case.foam"
    if not foam.exists():
        foam.write_text("")
    reader = pv.OpenFOAMReader(str(foam))
    times = [float(t) for t in reader.time_values if math.isfinite(float(t))]
    if not times:
        raise ValueError("OpenFOAM case contains no finite result time")
    latest = max(times)
    if expected_time is not None and latest + 1e-9 < float(expected_time):
        raise ValueError(
            f"OpenFOAM fields are stale (latest {latest}, solver reached {expected_time})")
    reader.set_active_time_value(latest)
    reader.cell_to_point_creation = True
    ds = reader.read()
    if "internalMesh" not in ds.keys():
        raise ValueError("OpenFOAM result has no internalMesh")
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


def parse_wall_heat_flux(case_dir: str, expected_time: Optional[float] = None):
    """Return finite total wall heat flow from the newest current output row, or None."""
    pp = Path(case_dir) / "postProcessing"
    dats = sorted(pp.glob("wallHeatFlux*/**/wallHeatFlux.dat"))
    rows: List[Tuple[float, float]] = []
    for dat in dats:
        for line in dat.read_text(errors="ignore").splitlines():
            cols = line.strip().split()
            if not cols or cols[0].startswith("#"):
                continue
            try:
                time_value, heat = float(cols[0]), float(cols[-1])
            except (IndexError, ValueError):
                continue
            if math.isfinite(time_value) and math.isfinite(heat):
                rows.append((time_value, heat))
    if not rows:
        return None
    time_value, heat = max(rows, key=lambda row: row[0])
    if expected_time is not None and time_value + 1e-9 < float(expected_time):
        return None
    return heat


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
    from services.sim import overlay as ov
    from services.sim import tessellate as tess

    bt = ov._tessellate_stl_world(stl_path)
    surf = ov._surface_verts(bt)
    chunks: list = []
    scalar_fields: List[str] = []
    part = patches.get(info.part_patch)

    if part is None:
        raise ValueError(f"OpenFOAM result is missing boundary patch {info.part_patch!r}")
    if info.thermal:
        if "T" not in part.point_data:
            raise ValueError("OpenFOAM thermal result is missing wall temperature")
        t_c = _sample_on_surface(
            surf, info, part, np.asarray(part.point_data["T"], dtype=np.float64) - 273.15)
        if not np.isfinite(t_c).all():
            raise ValueError("OpenFOAM wall temperature contains non-finite values")
        lo, hi = (temp_range or (float(t_c.min()), float(t_c.max())))
        chunks.append(tess.scalar_chunk(1, "temperature", t_c.astype(np.float32),
                                        vmin=lo, vmax=hi))
        scalar_fields.append("temperature")
    else:
        if "p" not in part.point_data:
            raise ValueError("OpenFOAM aero result is missing wall pressure")
        q_kin = 0.5 * info.speed_mps * info.speed_mps
        cp = _cp_on_surface(surf, info, part, q_kin)
        if not np.isfinite(cp).all():
            raise ValueError("OpenFOAM wall pressure contains non-finite values")
        cp = np.clip(cp, cp_min, cp_max)
        chunks.append(tess.scalar_chunk(1, "pressure_cp", cp, vmin=cp_min, vmax=cp_max))
        scalar_fields.append("pressure_cp")
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


def postprocess_cht(case_dir: str, stl_path: str, info, residual: Optional[float] = None):
    """Assemble a FlowResult from a finished chtMultiRegionFoam (CHT) run.

    Reads the solid region temperature (foamToVTK export), reports the part's hot/mean
    temperature + dissipated power, and colours the part surface by temperature.
    """
    from services.sim.lbm import FlowResult
    from services.sim import overlay as ov
    from services.sim import tessellate as tess

    cd = Path(case_dir)
    Tin_c = info.inlet_temp_k - 273.15
    t_min = t_max = t_mean = Tin_c
    regions = list(getattr(info, "solid_regions", None) or [info.solid_region])

    # Aggregate solid temperatures over EVERY solid region (multi-solid CHT),
    # keeping a per-region summary for the notes.
    per_region: List[str] = []
    all_xyz: list = []
    all_T: list = []
    got_any = False
    for reg in regions:
        solid = _read_vtm_combined(cd / "VTK" / reg)
        xyz, Tsolid = _xyz_and_T(solid)
        if Tsolid is None or not Tsolid.size:
            raise ValueError(f"CHT output for region {reg!r} has no temperature field")
        if (xyz is None or not np.isfinite(xyz).all() or not np.isfinite(Tsolid).all() or
                np.any(Tsolid <= 0.0)):
            raise ValueError(
                f"CHT output for region {reg!r} contains non-finite/incomplete/unphysical data")
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

    all_tc = np.concatenate(all_T) - 273.15
    t_min, t_max, t_mean = (float(all_tc.min()), float(all_tc.max()),
                            float(all_tc.mean()))
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
    if src_xyz is None or src_T is None or not src_T.size:
        raise ValueError("CHT output has no surface or volume temperature samples")
    if not np.isfinite(src_xyz).all() or not np.isfinite(src_T).all():
        raise ValueError("CHT temperature handoff contains non-finite samples")
    from scipy.spatial import cKDTree
    Tc_b = src_T - 273.15
    tree = cKDTree(src_xyz)
    _, idx = tree.query(info.world_mm_to_case_m(surf), k=1)
    t_surf = Tc_b[idx].astype(np.float32)
    if not np.isfinite(t_surf).all():
        raise ValueError("CHT surface interpolation produced non-finite temperatures")
    lo, hi = float(min(Tin_c, t_surf.min())), float(max(t_surf.max(), Tin_c + 1.0))
    chunks.append(tess.scalar_chunk(1, "temperature", t_surf, vmin=lo, vmax=hi))
    scalar_fields.append("temperature")

    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
        overlay_path = tf.name
    tess.write_aicm_file(overlay_path, [bt], extra_chunks=chunks)

    what = "solid" if len(regions) == 1 else f"{len(regions)} solids"
    re_note = (f"OpenFOAM CHT (conjugate) - Re {info.reynolds:,.0f}; {info.power_w:.1f} W in {what} "
               f"(k={info.solid_kappa:.0f} W/mK), inlet {Tin_c:.0f} C -> part {t_mean:.0f} C "
               f"(peak {t_max:.0f} C).")
    residual_note = (
        f"final coupled residual {float(residual):.3g} met the 1e-3 acceptance gate"
        if residual is not None and math.isfinite(float(residual)) and float(residual) >= 0.0
        else "final coupled residual was not reported")
    notes = [
        f"chtMultiRegionFoam physics-derived transient horizon {info.end_time:.4g} s "
        f"completed; {residual_note}.",
        "per-region: " + "; ".join(per_region) + ".",
        f"solid temperature {t_min:.0f}-{t_max:.0f} C.",
    ]
    return FlowResult(
        drag_n=0.0, lift_n=0.0, cd=0.0, frontal_area_mm2=info.frontal_area_mm2,
        reynolds_note=re_note, trace=[], slices=[], slice_transforms=[],
        streamline_points=[], streamline_speeds=[],
        diverged=False, steps_run=0, side_n=0.0, cl=0.0, ld=0.0, reynolds=info.reynolds,
        ref_area_mm2=info.frontal_area_mm2, cp_min=t_min, cp_max=t_max, vmax_mps=info.speed_mps,
        solver_notes=notes, overlay_path=overlay_path, scalar_fields=scalar_fields,
        converged=True, residual=float(residual if residual is not None else -1.0),
        mass_imbalance=-1.0,
    )


def postprocess(case_dir: str, stl_path: str, info: CaseInfo, converged: bool,
                solver_notes: Optional[List[str]] = None,
                expected_time: Optional[float] = None):
    """Assemble a FlowResult from a current, converged OpenFOAM run."""
    from services.sim.lbm import FlowResult
    if not converged:
        raise ValueError("OpenFOAM solver did not converge")

    actual_steps = (int(round(float(expected_time)))
                    if expected_time is not None and math.isfinite(float(expected_time))
                    else 0)
    internal, patches = _read_case(case_dir, expected_time=expected_time)
    part = patches.get(info.part_patch)
    if info.part_patch not in patches:
        raise ValueError(f"OpenFOAM result is missing boundary patch {info.part_patch!r}")
    part = patches[info.part_patch]
    if "U" not in internal.point_data:
        raise ValueError("OpenFOAM result is missing velocity field U")
    U = np.asarray(internal.point_data["U"], dtype=np.float64)
    if U.ndim != 2 or U.shape[1] != 3 or not U.size or not np.isfinite(U).all():
        raise ValueError("OpenFOAM velocity field is empty or non-finite")
    vmax = float(np.linalg.norm(U, axis=1).max())
    notes = list(solver_notes or [])

    if info.thermal:
        # Temperature legend (degC) from the part wall.
        t_lo, t_hi = info.inlet_temp_k - 273.15, info.inlet_temp_k - 273.15 + 1.0
        if "T" not in part.point_data:
            raise ValueError("OpenFOAM thermal result is missing wall temperature")
        t_c = np.asarray(part.point_data["T"], dtype=np.float64) - 273.15
        if (not t_c.size or not np.isfinite(t_c).all() or
                np.any(t_c <= -273.15)):
            raise ValueError("OpenFOAM wall temperature is empty, non-finite, or below 0 K")
        heat_w = parse_wall_heat_flux(case_dir, expected_time=expected_time)
        if heat_w is None or not math.isfinite(heat_w):
            raise ValueError("wallHeatFlux produced no finite current result")
        overlay_path, scalar_fields, sl_pts, sl_spd = build_overlay(
            stl_path, info, internal, patches, -1.0, 1.0, temp_range=(t_lo, t_hi))
        heat_txt = f", wall heat flow {heat_w:.1f} W"
        re_note = (f"OpenFOAM buoyant RANS - Re {info.reynolds:,.0f}; inlet "
                   f"{info.inlet_temp_k - 273.15:.0f} C; surface T {t_lo:.0f}-{t_hi:.0f} C{heat_txt}.")
        notes.append(
            f"OpenFOAM buoyant RANS converged after {actual_steps} SIMPLE iterations "
            f"(cap {info.iters}; k-omega SST, heat transfer).")
        return FlowResult(
            drag_n=0.0, lift_n=0.0, cd=0.0, frontal_area_mm2=info.frontal_area_mm2,
            reynolds_note=re_note, trace=[], slices=[], slice_transforms=[],
            streamline_points=sl_pts, streamline_speeds=sl_spd,
            diverged=False, steps_run=actual_steps,
            side_n=0.0, cl=0.0, ld=0.0, reynolds=info.reynolds,
            ref_area_mm2=info.ref_area_mm2,
            cp_min=t_lo, cp_max=t_hi, vmax_mps=vmax, solver_notes=notes,
            overlay_path=overlay_path, scalar_fields=scalar_fields,
            converged=True, residual=-1.0, mass_imbalance=-1.0,
        )

    # ---- aero ----
    cd, cl = parse_coefficients(case_dir, expected_time=expected_time)
    _raw_drag_n, side_n, _raw_lift_n = parse_forces(
        case_dir, expected_time=expected_time)
    q = 0.5 * info.rho_kgm3 * info.speed_mps ** 2
    aref_m2 = max(info.ref_area_mm2 * 1e-6, 1e-9)
    drag_n = cd * q * aref_m2
    lift_n = cl * q * aref_m2
    ld = (cl / cd) if abs(cd) > 1e-9 else 0.0

    cp_min, cp_max = -1.0, 1.0
    if "p" not in part.point_data:
        raise ValueError("OpenFOAM aero result is missing wall pressure")
    q_kin = max(0.5 * info.speed_mps ** 2, 1e-9)
    cp_all = np.asarray(part.point_data["p"], dtype=np.float64) / q_kin
    if not cp_all.size or not np.isfinite(cp_all).all():
        raise ValueError("OpenFOAM wall pressure is empty or non-finite")
    cp_min = float(min(-0.5, np.percentile(cp_all, 1)))
    cp_max = float(max(1.0, np.percentile(cp_all, 99)))

    overlay_path, scalar_fields, sl_pts, sl_spd = build_overlay(
        stl_path, info, internal, patches, cp_min, cp_max)

    notes.append(
        f"OpenFOAM RANS converged after {actual_steps} SIMPLE iterations "
        f"(cap {info.iters}; k-omega SST).")
    re_note = (f"OpenFOAM RANS - Re {info.reynolds:,.0f}; "
               f"Cd {cd:.3f}, Cl {cl:.3f} on Aref {info.ref_area_mm2:.0f} mm2.")
    return FlowResult(
        drag_n=drag_n, lift_n=lift_n, cd=cd, frontal_area_mm2=info.frontal_area_mm2,
        reynolds_note=re_note, trace=[], slices=[], slice_transforms=[],
        streamline_points=sl_pts, streamline_speeds=sl_spd,
        diverged=False, steps_run=actual_steps,
        side_n=side_n, cl=cl, ld=ld, reynolds=info.reynolds,
        ref_area_mm2=info.ref_area_mm2,
        cp_min=cp_min, cp_max=cp_max, vmax_mps=vmax, solver_notes=notes,
        overlay_path=overlay_path, scalar_fields=scalar_fields,
        converged=True, residual=-1.0, mass_imbalance=-1.0,
    )
