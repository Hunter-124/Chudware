"""C19 sim-result overlay packaging.

The sim solvers (engine.sim.fea_static / fea_modal / lbm) produce results on the VOLUME mesh
(tet nodes) or on lattice slices.  The viewport renders the AICM v1 SURFACE tessellation
(docs/contracts/mesh_format.md).  This module bridges the two:

* :func:`static_overlay` — tessellate the analysed STEP surface, map each volume-node scalar
  (von Mises, |u|, FoS) + displacement vector to the nearest surface vertex, and write an AICM
  with SCLR + DISP chunks.
* :func:`flow_overlay` — render the LBM mid-plane velocity slices to transparent colormap PNGs
  (fixed range) and pack the streamlines as a LINS chunk coloured by speed.

Kept separate from worker.py so the worker stays a thin dispatcher (surgical C08 extension).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import numpy as np


# --------------------------------------------------------------------------- STEP surface tess


def tessellate_step_surface(step_path: str):
    """Load a STEP file and tessellate its surface to an AICM BodyTess (body_id 1)."""
    from build123d import import_step
    from services.sim import tessellate as tess

    shape = import_step(step_path)
    raw = shape.wrapped if hasattr(shape, "wrapped") else shape
    bt = tess.tessellate_body(raw, body_id=1, name="Body1", color=(0.71, 0.76, 0.82))
    return bt


def _surface_verts(bt) -> np.ndarray:
    """(n_verts, 3) surface vertex positions from a BodyTess."""
    return np.asarray(bt.pos, dtype=np.float64).reshape(-1, 3)


def _map_to_surface(surf_xyz: np.ndarray, vol_xyz: np.ndarray,
                    vol_values: np.ndarray) -> np.ndarray:
    """For each surface vertex, take the value of the nearest volume node (brute-force kNN=1).

    surf_xyz: (m, 3); vol_xyz: (n, 3); vol_values: (n,) or (n, k).
    Returns (m,) or (m, k).
    """
    m = surf_xyz.shape[0]
    out_shape = (m,) if vol_values.ndim == 1 else (m, vol_values.shape[1])
    out = np.zeros(out_shape, dtype=np.float64)
    chunk = 2048
    for i in range(0, m, chunk):
        s = surf_xyz[i:i + chunk]                       # (c, 3)
        d = np.linalg.norm(s[:, None, :] - vol_xyz[None, :, :], axis=-1)  # (c, n)
        nn = np.argmin(d, axis=1)                        # (c,)
        out[i:i + chunk] = vol_values[nn]
    return out


# --------------------------------------------------------------------------- static / modal


def static_overlay(step_path: str, vol_points: np.ndarray, von_mises: np.ndarray,
                   disp_mag: np.ndarray, displacement: np.ndarray,
                   yield_mpa: float, summary: dict) -> str:
    """Write an AICM overlay for a static result. Returns the temp .aicm path.

    SCLR chunks: ``von_mises`` (MPa), ``displacement_mag`` (mm), ``fos`` (clamped 0..15).
    DISP chunk: per-surface-vertex displacement vectors with a scale_hint chosen so peak
    deformation reads as ~10% of the part bbox diagonal at scale 1.
    """
    from services.sim import tessellate as tess

    bt = tessellate_step_surface(step_path)
    surf = _surface_verts(bt)
    vol = np.asarray(vol_points, dtype=np.float64)

    vm_s = _map_to_surface(surf, vol, np.asarray(von_mises, dtype=np.float64))
    dm_s = _map_to_surface(surf, vol, np.asarray(disp_mag, dtype=np.float64))
    dvec_s = _map_to_surface(surf, vol, np.asarray(displacement, dtype=np.float64))

    with np.errstate(divide="ignore", invalid="ignore"):
        fos_s = np.where(vm_s > 1e-9, yield_mpa / vm_s, 15.0)
    fos_s = np.clip(fos_s, 0.0, 15.0)

    # scale_hint: make peak displacement read as ~10% of the bbox diagonal at scale 1.
    bb_diag = float(np.linalg.norm(surf.max(axis=0) - surf.min(axis=0))) if len(surf) else 1.0
    peak = float(np.max(dm_s)) if len(dm_s) and np.max(dm_s) > 1e-12 else 1.0
    scale_hint = (0.1 * bb_diag / peak) if peak > 1e-12 else 1.0

    chunks = [
        tess.scalar_chunk(1, "von_mises", vm_s,
                          vmin=0.0, vmax=float(summary.get("max_von_mises_mpa", np.max(vm_s)))),
        tess.scalar_chunk(1, "displacement_mag", dm_s,
                          vmin=0.0, vmax=float(summary.get("max_disp_mm", np.max(dm_s)))),
        tess.scalar_chunk(1, "fos", fos_s, vmin=0.0, vmax=5.0),
        tess.disp_chunk(1, float(scale_hint), dvec_s),
    ]
    return _write_temp(bt, chunks)


def modal_overlay(step_path: str, vol_points: np.ndarray,
                  mode_shape: np.ndarray, freq_hz: float) -> str:
    """Write an AICM overlay for one modal mode: DISP of the mode shape + SCLR of its magnitude."""
    from services.sim import tessellate as tess

    bt = tessellate_step_surface(step_path)
    surf = _surface_verts(bt)
    vol = np.asarray(vol_points, dtype=np.float64)

    shp = np.asarray(mode_shape, dtype=np.float64).reshape(-1, 3)
    dvec_s = _map_to_surface(surf, vol, shp)
    mag = np.linalg.norm(dvec_s, axis=1)
    norm = float(np.max(mag)) if len(mag) and np.max(mag) > 1e-12 else 1.0

    bb_diag = float(np.linalg.norm(surf.max(axis=0) - surf.min(axis=0))) if len(surf) else 1.0
    scale_hint = (0.15 * bb_diag / norm) if norm > 1e-12 else 1.0

    chunks = [
        tess.scalar_chunk(1, "mode_amplitude", mag, vmin=0.0, vmax=norm),
        tess.disp_chunk(1, float(scale_hint), dvec_s),
    ]
    return _write_temp(bt, chunks)


def _write_temp(bt, chunks) -> str:
    from services.sim import tessellate as tess
    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
        tmp = tf.name
    tess.write_aicm_file(tmp, [bt], extra_chunks=chunks)
    return tmp


# --------------------------------------------------------------------------- flow


def streamlines_chunk(streamline_points: list, streamline_speeds: list) -> Optional[bytes]:
    """Pack LBM streamlines as a LINS chunk coloured by speed (m/s). None if empty."""
    from services.sim import tessellate as tess
    polys = [np.asarray(p, dtype=np.float32).reshape(-1, 3) for p in streamline_points
             if len(p) >= 2]
    if not polys:
        return None
    speeds = []
    for p, s in zip(streamline_points, streamline_speeds):
        if len(p) >= 2:
            speeds.append(np.asarray(s, dtype=np.float32).reshape(-1))
    return tess.lines_chunk("streamlines", polys, scalars=speeds)


def _tessellate_stl_world(stl_path: str, color=(0.78, 0.80, 0.82)):
    """Tessellate an STL (already in world coords) into an AICM BodyTess (body_id 1).

    Uses MeshBody-style direct triangle tessellation (no BRepMesh) so the model surface
    renders even when no STEP topology is available.
    """
    import trimesh
    from services.sim import tessellate as tess
    from services.formats.mesh_body import MeshBody

    mesh = trimesh.load(stl_path, force="mesh")
    verts = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3)
    tris = np.asarray(mesh.faces, dtype=np.uint32).reshape(-1, 3)
    mb = MeshBody(verts=verts, tris=tris)
    return tess.tessellate_mesh_body(mb, body_id=1, name="FlowModel", color=color)


def _cp_at_surface(surf_xyz: np.ndarray, result) -> np.ndarray:
    """Per-surface-vertex pressure coefficient Cp from the near-wall lattice density.

    For each surface vertex: map world->lattice index (p_domain = R @ p_world + offset,
    then /voxel_mm), step one cell along the outward direction to the nearest FLUID cell,
    read rho there, and Cp = cs2*(rho-1)/(0.5*u_lat^2).  Clamped to [cp_min, cp_max].
    """
    R = np.asarray(result.transform_R, dtype=np.float64)
    offset = np.asarray(result.transform_offset, dtype=np.float64)
    voxel_mm = float(result.voxel_mm)
    rho_grid = np.asarray(result.rho_grid)
    solid = np.asarray(result.solid_mask, dtype=bool)
    u_lat = float(result.u_lat)
    Nx, Ny, Nz = rho_grid.shape
    cs2 = 1.0 / 3.0

    # world -> domain mm -> lattice index
    p_dom = (R @ surf_xyz.T).T + offset          # (m,3) mm
    idx_f = p_dom / max(voxel_mm, 1e-9)          # fractional index
    m = surf_xyz.shape[0]

    # outward direction estimate: from the surface centroid (in domain space).
    centroid = idx_f.mean(axis=0)
    outward = idx_f - centroid
    nrm = np.linalg.norm(outward, axis=1, keepdims=True)
    outward = np.where(nrm > 1e-9, outward / np.maximum(nrm, 1e-9), 0.0)

    cp = np.full(m, np.nan, dtype=np.float64)
    denom = 0.5 * u_lat * u_lat + 1e-12

    base = np.round(idx_f).astype(int)
    for vi in range(m):
        found_rho = None
        # search a small neighbourhood, preferring the outward step, for the nearest fluid cell.
        for step in (1, 2):
            cand = base[vi] + np.round(outward[vi] * step).astype(int)
            ci, cj, ck = cand
            if 0 <= ci < Nx and 0 <= cj < Ny and 0 <= ck < Nz and not solid[ci, cj, ck]:
                found_rho = float(rho_grid[ci, cj, ck])
                break
        if found_rho is None:
            # fall back to a 3x3x3 scan around the vertex for any fluid cell
            ci0, cj0, ck0 = base[vi]
            best = None
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for dk in (-1, 0, 1):
                        ci, cj, ck = ci0 + di, cj0 + dj, ck0 + dk
                        if 0 <= ci < Nx and 0 <= cj < Ny and 0 <= ck < Nz and not solid[ci, cj, ck]:
                            best = float(rho_grid[ci, cj, ck])
                            break
                    if best is not None:
                        break
                if best is not None:
                    break
            found_rho = best
        if found_rho is not None:
            cp[vi] = cs2 * (found_rho - 1.0) / denom

    cp = np.nan_to_num(cp, nan=0.0)
    return np.clip(cp, result.cp_min, result.cp_max)


def flow_overlay(stl_path: str, result) -> str:
    """Write a MODEL-BEARING flow overlay AICM and return its path.

    Chunks:
      body 1   : the analysed model surface (STL, world coords), light grey.
      SCLR "pressure_cp" : per-surface-vertex Cp (dimensionless), vmin/vmax = cp_min/cp_max.
      LINS "streamlines" : world-coord streamlines, scalar = speed (m/s).
      LINS "slices"      : world-coord velocity grid polylines, scalar = speed (m/s).
    """
    from services.sim import tessellate as tess

    bt = _tessellate_stl_world(stl_path)
    surf = _surface_verts(bt)

    chunks: list = []

    # Cp scalar field
    try:
        cp = _cp_at_surface(surf, result)
        chunks.append(tess.scalar_chunk(1, "pressure_cp", cp,
                                        vmin=float(result.cp_min), vmax=float(result.cp_max)))
    except Exception:  # noqa: BLE001
        pass

    # streamlines (already world coords from run_wind_tunnel)
    sl = streamlines_chunk(result.streamline_points, result.streamline_speeds)
    if sl is not None:
        chunks.append(sl)

    # slices as world-coord grid polylines
    polys = [np.asarray(p, dtype=np.float32).reshape(-1, 3)
             for p in getattr(result, "slice_world_polys", []) if len(p) >= 2]
    if polys:
        spds = [np.asarray(s, dtype=np.float32).reshape(-1)
                for p, s in zip(result.slice_world_polys, result.slice_world_speeds)
                if len(p) >= 2]
        chunks.append(tess.lines_chunk("slices", polys, scalars=spds))

    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
        path = tf.name
    tess.write_aicm_file(path, [bt], extra_chunks=chunks)
    return path


def slice_pngs(slices: list, slice_transforms: list, speed_mps: float) -> list:
    """Render each velocity-magnitude slice to a transparent colormap PNG (fixed 0..1.5*u range).

    Returns ``[{path, transform:[16]}, ...]``.  matplotlib is optional — returns [] if absent.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        # matplotlib >=3.9 removed matplotlib.cm.get_cmap; prefer the colormaps registry.
        try:
            from matplotlib import colormaps as _mpl_colormaps

            def _get_cmap(name):
                return _mpl_colormaps[name]
        except Exception:  # noqa: BLE001 -- older matplotlib (<3.5)
            from matplotlib import cm as _cm

            def _get_cmap(name):
                return _cm.get_cmap(name)
    except Exception:  # noqa: BLE001
        return []

    try:
        from PIL import Image
        have_pil = True
    except Exception:  # noqa: BLE001
        have_pil = False

    vmax = max(1e-6, 1.5 * float(speed_mps))
    out = []
    cmap = _get_cmap("turbo")
    for i, (sl, xf) in enumerate(zip(slices, slice_transforms)):
        arr = np.asarray(sl, dtype=np.float32)
        if arr.ndim != 2 or arr.size == 0:
            continue
        norm = np.clip(arr / vmax, 0.0, 1.0)
        rgba = (cmap(norm) * 255.0).astype(np.uint8)   # (h, w, 4)
        # Fully transparent where the flow is essentially still (so the part shows through).
        alpha = (np.clip(norm * 4.0, 0.0, 1.0) * 255.0).astype(np.uint8)
        rgba[..., 3] = alpha
        with tempfile.NamedTemporaryFile(suffix=f"_slice{i}.png", delete=False) as tf:
            png_path = tf.name
        if have_pil:
            Image.fromarray(rgba, "RGBA").save(png_path)
        else:
            import matplotlib.pyplot as plt
            plt.imsave(png_path, rgba)
        out.append({"path": png_path, "transform": list(np.asarray(xf, dtype=float).flatten())})
    return out
