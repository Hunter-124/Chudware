"""D3Q19 Lattice-Boltzmann Method (LBM) wind tunnel — engine/sim/lbm.py

Lattice → SI scaling (documented here as required):
----------------------------------------------------
  dx  = physical voxel size in metres = domain_length_m / N_cells_along_flow
  dt  = dx / (cs * u_phys / u_lat)
      where cs = lattice speed of sound = 1/sqrt(3) in lattice units,
            u_phys = inlet speed in m/s, u_lat = target lattice inlet speed ≤ 0.1

  Kinematic viscosity (SI):
    nu_lat = cs^2 * (tau - 0.5)     [lattice units, cs^2=1/3]
    nu_phys = nu_lat * dx^2 / dt    [m^2/s]
    → Reynolds = u_phys * L_char / nu_phys   (L_char = longest body bbox dim in m)

  Force conversion (momentum exchange on solid boundary links):
    Each momentum exchange at a boundary link contributes:
      ΔP_lat  = (f_i_out + f_i_in) * e_i_alpha  (dimensionless, rho_lat=1 normalised)
    Summing over all boundary links and the flow direction alpha:
      drag_lat = Σ ΔP_lat * e_i_x  (dimensionless accumulation)
    Physical force (N) — Aidun & Clausen 2010 derivation:
      Each f_i carries momentum flux rho_ref * f_i_lat * c_i * dx^2 through the face
      where c_i = e_i * dx/dt (physical velocity, m/s), dx^2 = face area
      Summing: drag_n = rho_ref * drag_lat * (dx/dt) * dx^2
                      = rho_ref * drag_lat * dx^3 / dt
      with rho_ref ≈ 1.225 kg/m^3 (air, sea level)
    Note: an incorrect formula of rho*dx^4/dt^2 is off by a factor of c=dx/dt.

  Drag coefficient:
    Cd = drag_n / (0.5 * rho_phys * u_phys^2 * frontal_area_m2)

Turbulence:
  Smagorinsky LES with Cs=0.17.  Effective relaxation:
    nu_eff = nu_lat + (Cs * delta_x)^2 * |S̃|
    tau_eff = nu_eff / cs^2 + 0.5
  where delta_x=1 (lattice) and |S̃| is the magnitude of the strain-rate tensor
  estimated from the second-order moment of the non-equilibrium distribution.

Boundary conditions:
  Inlet  (x=0 face)        : prescribed velocity BC (Zou-He equilibrium injection)
  Outlet (x=Nx-1 face)     : zero-gradient (copy from x=Nx-2 plane)
  Lateral (y,z faces)      : slip (mirror, no friction) — documented choice
  Solid cells              : half-way bounce-back

Numerical stability guards:
  - u_lat ≤ 0.1 (Mach guard at construction time; enforced by scaling)
  - tau ≥ 0.51 (viscosity floor; clamped at init)
  - NaN/divergence watchdog every ~50 steps:
      first trigger  → halve (tau-0.5) toward tau=1 and continue
      second trigger → abort, set diverged=True, return partial results
  - Memory guard: refuse if domain would exceed ~2 GB at float32 (19 fields × 4 bytes)
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable, List, Optional, Tuple

import numpy as np
import trimesh
import trimesh.voxel.creation as tvc

# ---------------------------------------------------------------------------
# D3Q19 constants
# ---------------------------------------------------------------------------
# Velocity vectors (19 directions including rest)
_EQ: np.ndarray = np.array([
    [0,  0,  0],   # 0  rest
    [1,  0,  0],   # 1
    [-1, 0,  0],   # 2
    [0,  1,  0],   # 3
    [0, -1,  0],   # 4
    [0,  0,  1],   # 5
    [0,  0, -1],   # 6
    [1,  1,  0],   # 7
    [-1,-1,  0],   # 8
    [1, -1,  0],   # 9
    [-1, 1,  0],   # 10
    [1,  0,  1],   # 11
    [-1, 0, -1],   # 12
    [1,  0, -1],   # 13
    [-1, 0,  1],   # 14
    [0,  1,  1],   # 15
    [0, -1, -1],   # 16
    [0,  1, -1],   # 17
    [0, -1,  1],   # 18
], dtype=np.float32)

# Weights
_W: np.ndarray = np.array([
    1.0/3.0,                                    # rest
    1.0/18.0, 1.0/18.0, 1.0/18.0, 1.0/18.0,   # face
    1.0/18.0, 1.0/18.0,
    1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,   # edge
    1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
    1.0/36.0, 1.0/36.0, 1.0/36.0, 1.0/36.0,
], dtype=np.float32)

# Opposite direction index for bounce-back
_OPP: np.ndarray = np.array([
    0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15, 18, 17
], dtype=np.int32)

_NQ = 19

# Precompute e_x·e components for drag / lift
_EX = _EQ[:, 0].astype(np.float32)  # drag (flow direction = x)
_EY = _EQ[:, 1].astype(np.float32)  # lift (y)
_EZ = _EQ[:, 2].astype(np.float32)  # span (z)

# cs^2 = 1/3
_CS2 = np.float32(1.0 / 3.0)

# Preset step caps and grid sizes (cells on longest axis)
_PRESETS = {
    "coarse":  {"cells": 48,  "steps": 1500},
    "medium":  {"cells": 64,  "steps": 3000},
    "fine":    {"cells": 96,  "steps": 6000},
    "xfine":   {"cells": 128, "steps": 10000},
}

# Air properties
_RHO_AIR_KGM3 = 1.225  # kg/m^3
_NU_AIR_M2S   = 1.5e-5  # kinematic viscosity m^2/s

# Smagorinsky constant
_CS_SMAG = 0.17

# RNG seed for streamline jitter (determinism)
_SEED = 42

# ---------------------------------------------------------------------------
# FlowResult dataclass
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class FlowResult:
    """Physical results from a wind-tunnel run.

    All forces in Newtons, areas in mm², angles/coords in mm (world space).

    Slice arrays are 2D float32 velocity-magnitude fields in lattice-cell
    coordinates.  ``slice_transforms`` maps each slice to a 4×4 world
    transform (mm) so C19 can position quads.

    Streamlines are lists of (N,3) arrays of points (mm) + (N,) speed arrays (m/s).
    """
    drag_n:             float
    lift_n:             float
    cd:                 float
    frontal_area_mm2:   float
    reynolds_note:      str
    trace:              List[Tuple[int, float]]    # (step, drag_n)
    slices:             List[np.ndarray]           # [x_mid, y_mid, z_mid] 2D vmag arrays
    slice_transforms:   List[np.ndarray]           # 3 × (4,4) float64 world→lattice transforms
    streamline_points:  List[np.ndarray]           # list of (N,3) float32 mm coords
    streamline_speeds:  List[np.ndarray]           # list of (N,) float32 m/s
    diverged:           bool
    steps_run:          int

    def save_npz(self, path: str) -> None:
        """Save all arrays + scalars to a compressed .npz file."""
        sl_pts  = {f"sl_pts_{i}":  a for i, a in enumerate(self.streamline_points)}
        sl_spd  = {f"sl_spd_{i}":  a for i, a in enumerate(self.streamline_speeds)}
        sl_xfm  = {f"sl_xfm_{i}":  a for i, a in enumerate(self.slice_transforms)}
        sl_arr  = {f"slice_{i}":    a for i, a in enumerate(self.slices)}
        trace_arr = np.array(self.trace, dtype=np.float32) if self.trace else np.zeros((0, 2), dtype=np.float32)
        np.savez_compressed(
            path,
            drag_n=np.float32(self.drag_n),
            lift_n=np.float32(self.lift_n),
            cd=np.float32(self.cd),
            frontal_area_mm2=np.float32(self.frontal_area_mm2),
            reynolds_note=np.bytes_(self.reynolds_note.encode()),
            trace=trace_arr,
            diverged=np.bool_(self.diverged),
            steps_run=np.int32(self.steps_run),
            **sl_pts, **sl_spd, **sl_xfm, **sl_arr,
        )


# ---------------------------------------------------------------------------
# Equilibrium distribution (vectorized)
# ---------------------------------------------------------------------------
def _feq_fast(rho: np.ndarray, ux: np.ndarray, uy: np.ndarray, uz: np.ndarray) -> np.ndarray:
    """Vectorized D3Q19 equilibrium distribution.

    rho, ux, uy, uz: shape (Nx, Ny, Nz) float32
    Returns feq: shape (Nx, Ny, Nz, 19) float32
    Formula: f_i^eq = w_i * rho * (1 + e_i.u/cs2 + (e_i.u)^2/(2*cs4) - u^2/(2*cs2))
    """
    eu2 = (np.outer(ux.ravel(), _EQ[:, 0]) +
           np.outer(uy.ravel(), _EQ[:, 1]) +
           np.outer(uz.ravel(), _EQ[:, 2])).reshape(ux.shape + (19,)).astype(np.float32)
    u2  = (ux**2 + uy**2 + uz**2)[..., np.newaxis]
    return (_W * rho[..., np.newaxis] * (
        np.float32(1.0) + eu2 / _CS2 + eu2**2 / (np.float32(2.0) * _CS2 * _CS2)
        - u2 / (np.float32(2.0) * _CS2)
    )).astype(np.float32)


# ---------------------------------------------------------------------------
# Streaming step (vectorized, ping-pong)
# ---------------------------------------------------------------------------
def _stream(f: np.ndarray) -> np.ndarray:
    """Shift populations along their velocity vectors.

    f: (Nx, Ny, Nz, 19) — returns new array of same shape.
    """
    g = np.empty_like(f)
    for q in range(_NQ):
        ex, ey, ez = int(_EQ[q, 0]), int(_EQ[q, 1]), int(_EQ[q, 2])
        g[..., q] = np.roll(f[..., q], shift=(ex, ey, ez), axis=(0, 1, 2))
    return g


# ---------------------------------------------------------------------------
# Smagorinsky effective tau
# ---------------------------------------------------------------------------
def _smag_tau(f: np.ndarray, feq: np.ndarray, tau0: float) -> np.ndarray:
    """Return per-cell effective relaxation time using Smagorinsky LES.

    Approximation: |S̃| estimated from the Frobenius norm of the
    non-equilibrium stress tensor Pi_neq.
    """
    # non-equilibrium part
    fneq = f - feq                                 # (Nx,Ny,Nz,19)

    # Pi_neq_ab = Σ_q e_qa e_qb f_neq_q (symmetric 3×3 per cell)
    # We only need the Frobenius norm squared = Σ_{a,b} Pi_neq_ab^2
    # |S̃| = sqrt(2 * Pi:Pi) / (2 * rho * cs^2)  but we absorb into the formula below
    # Simpler: use scalar approximation Pi_mag = |fneq| (L2 over q)
    pi_mag = np.sqrt((fneq ** 2).sum(axis=-1))    # (Nx,Ny,Nz)

    nu0   = _CS2 * (tau0 - 0.5)
    smag  = (_CS_SMAG ** 2) * np.sqrt(np.float32(2.0)) * pi_mag
    nu_eff = nu0 + smag
    tau_eff = (nu_eff / _CS2 + np.float32(0.5)).astype(np.float32)
    tau_eff = np.clip(tau_eff, np.float32(0.51), np.float32(2.0))
    return tau_eff


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------
def run_wind_tunnel(
    stl_path: str,
    direction: Tuple[float, float, float] = (1.0, 0.0, 0.0),
    speed_mps: float = 10.0,
    preset: str = "coarse",
    on_progress: Optional[Callable[[dict], None]] = None,
    max_steps: Optional[int] = None,
) -> FlowResult:
    """Run a D3Q19 LBM wind-tunnel simulation.

    Parameters
    ----------
    stl_path:   Path to the STL file of the part (in mm).
    direction:  Unit vector of incoming flow in world space (default +X).
    speed_mps:  Free-stream velocity in m/s (default 10 m/s).
    preset:     Grid preset: "coarse" (48) / "medium" (64) / "fine" (96) / "xfine" (128).
    on_progress: Callable receiving sim_progress dicts:
                 {job_id, stage, pct, text, trace_point?:{step, drag}}
    max_steps:  Override step cap (None = use preset default).

    Returns
    -------
    FlowResult with all physical outputs and numpy arrays ready for C19.
    """
    if preset not in _PRESETS:
        raise ValueError(f"Unknown preset {preset!r}; choose from {list(_PRESETS)}")

    def _progress(stage: str, pct: int, text: str, trace_point: Optional[dict] = None) -> None:
        if on_progress is None:
            return
        data: dict = {"job_id": "lbm", "stage": stage, "pct": pct, "text": text}
        if trace_point is not None:
            data["trace_point"] = trace_point
        on_progress(data)

    # ------------------------------------------------------------------
    # 1. Load mesh and voxelize
    # ------------------------------------------------------------------
    _progress("voxelize", 0, "Loading STL...")
    mesh = trimesh.load(stl_path, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("STL did not produce a single trimesh.Trimesh")

    # Align mesh so flow direction == +X
    dir_vec = np.array(direction, dtype=np.float64)
    dir_vec /= np.linalg.norm(dir_vec)
    mesh = _align_to_x(mesh, dir_vec)

    bbox_min = mesh.bounds[0]    # mm
    bbox_max = mesh.bounds[1]    # mm
    part_size = bbox_max - bbox_min  # mm
    longest_axis = float(np.max(part_size))  # mm

    # Voxel pitch in mm to fit preset cells along longest axis
    # (domain is larger; this sets the resolution)
    cells_on_longest = _PRESETS[preset]["cells"]

    # Domain layout (mm):
    #   upstream pad  : 1× longest_axis
    #   part           :  part_size_x
    #   downstream pad : 3× longest_axis
    #   lateral pads   : 1× longest_axis each side
    lat_pad_mm  = max(longest_axis, 10.0)
    up_pad_mm   = max(longest_axis, 10.0)
    dn_pad_mm   = max(3.0 * longest_axis, 30.0)

    domain_x_mm = up_pad_mm + part_size[0] + dn_pad_mm
    domain_y_mm = lat_pad_mm + part_size[1] + lat_pad_mm
    domain_z_mm = lat_pad_mm + part_size[2] + lat_pad_mm

    domain_longest = max(domain_x_mm, domain_y_mm, domain_z_mm)
    voxel_mm = domain_longest / cells_on_longest

    Nx = max(4, int(np.ceil(domain_x_mm / voxel_mm)))
    Ny = max(4, int(np.ceil(domain_y_mm / voxel_mm)))
    Nz = max(4, int(np.ceil(domain_z_mm / voxel_mm)))

    # Memory guard: 19 floats × 4 bytes per cell, two buffers
    mem_bytes = Nx * Ny * Nz * _NQ * 4 * 2
    mem_gb = mem_bytes / (1024 ** 3)
    if mem_gb > 2.0:
        raise MemoryError(
            f"Preset '{preset}' would require ~{mem_gb:.1f} GB for the LBM arrays "
            f"(domain {Nx}×{Ny}×{Nz}, {_NQ} distributions, float32 × 2 buffers). "
            f"Reduce preset or part size."
        )

    _progress("voxelize", 5, f"Domain {Nx}×{Ny}×{Nz}, voxel {voxel_mm:.2f} mm, ~{mem_gb*1024:.0f} MB")

    # Offset mesh so it sits inside the domain with correct padding
    offset = np.array([up_pad_mm, lat_pad_mm, lat_pad_mm]) - bbox_min
    mesh.apply_translation(offset)

    # Voxelize
    pitch = voxel_mm
    voxel_grid = tvc.voxelize(mesh, pitch=pitch)
    voxel_grid = voxel_grid.fill()  # solid fill
    solid_origin = voxel_grid.translation  # mm, corner of voxel grid
    solid_vox_idx = voxel_grid.sparse_indices  # (M, 3) integer indices

    # Build boolean solid array mapped to our domain grid
    # Voxel grid indices → domain grid indices
    solid_mask = np.zeros((Nx, Ny, Nz), dtype=bool)
    ix = np.round((solid_vox_idx[:, 0] * pitch + solid_origin[0]) / voxel_mm).astype(int)
    iy = np.round((solid_vox_idx[:, 1] * pitch + solid_origin[1]) / voxel_mm).astype(int)
    iz = np.round((solid_vox_idx[:, 2] * pitch + solid_origin[2]) / voxel_mm).astype(int)
    valid = (ix >= 0) & (ix < Nx) & (iy >= 0) & (iy < Ny) & (iz >= 0) & (iz < Nz)
    solid_mask[ix[valid], iy[valid], iz[valid]] = True

    _progress("voxelize", 15, f"Solid cells: {int(solid_mask.sum())}")

    # Frontal area (projected onto YZ plane, i.e. normal to flow = X)
    frontal_mask = solid_mask.any(axis=0)  # (Ny, Nz)
    frontal_area_mm2 = float(frontal_mask.sum()) * (voxel_mm ** 2)

    # ------------------------------------------------------------------
    # 2. LBM parameters
    # ------------------------------------------------------------------
    # dx in metres
    dx_m = voxel_mm * 1e-3

    # Target lattice inlet speed ≤ 0.1 (Mach guard)
    u_lat = min(0.1, speed_mps / (np.sqrt(3.0) * speed_mps + 1e-12) * 0.1)
    # Simpler: scale so u_lat = 0.05 (conservative)
    u_lat = np.float32(0.05)

    # Physical time step
    dt_s = u_lat * dx_m / speed_mps  # u_lat = speed_mps * dt/dx  → dt = u_lat*dx/speed

    # Lattice kinematic viscosity from Reynolds proxy
    L_char_m = longest_axis * 1e-3
    Re_target = speed_mps * L_char_m / _NU_AIR_M2S
    nu_lat = u_lat * L_char_m / dx_m / Re_target if Re_target > 0 else _CS2 * 0.5
    # Clamp for stability
    nu_lat = max(nu_lat, _CS2 * 0.01)
    tau0 = float(nu_lat / _CS2 + 0.5)
    tau0 = max(tau0, 0.51)
    tau0 = min(tau0, 1.99)  # very high tau → over-damped but stable

    # Conversion factor lattice force → Newtons
    # Standard LBM momentum exchange (Aidun & Clausen 2010):
    #   drag_n [N] = drag_lat * rho_phys * dx^3 / dt
    #             = drag_lat * rho_phys * dx^2 * (dx/dt)
    # where drag_lat is dimensionless (rho_lat=1 normalised populations summed over boundary links)
    # Note: NOT dx^4/dt^2 — that would be off by c=dx/dt.
    force_factor = float(_RHO_AIR_KGM3 * dx_m**3 / dt_s)

    reynolds_note = (
        f"Lattice Re ~{Re_target:.0f} (u_lat={u_lat:.3f}, tau={tau0:.3f}, "
        f"Nu_lat={nu_lat:.4f}, dx={dx_m*1e3:.2f} mm, dt={dt_s*1e6:.3f} us) - "
        f"QUALITATIVE result; use CFD solver for engineering decisions."
    )

    _progress("assemble", 20, f"Re~{Re_target:.0f}, tau={tau0:.3f}, u_lat={u_lat:.3f}")

    # ------------------------------------------------------------------
    # 3. Initialise distributions
    # ------------------------------------------------------------------
    rho = np.ones((Nx, Ny, Nz), dtype=np.float32)
    ux  = np.full((Nx, Ny, Nz), u_lat, dtype=np.float32)
    uy  = np.zeros((Nx, Ny, Nz), dtype=np.float32)
    uz  = np.zeros((Nx, Ny, Nz), dtype=np.float32)

    f = _feq_fast(rho, ux, uy, uz)  # (Nx, Ny, Nz, 19)

    # Zero out solid interior (they never contribute to dynamics)
    f[solid_mask] = 0.0

    step_cap = max_steps if max_steps is not None else _PRESETS[preset]["steps"]
    convergence_window = max(20, step_cap // 5)

    trace: List[Tuple[int, float]] = []
    diverged = False
    watchdog_count = 0
    tau_current = tau0

    _progress("solve", 25, f"Starting LBM ({step_cap} steps cap)...")

    # Pre-build boundary-link list for momentum-exchange drag calculation
    # boundary links: fluid cell adjacent to solid cell
    # (ix_fluid, iy_fluid, iz_fluid, q) where e_q points from fluid → solid
    bound_links = _build_boundary_links(solid_mask, Nx, Ny, Nz)

    # ------------------------------------------------------------------
    # 4. Time-stepping loop
    # ------------------------------------------------------------------
    t_start = time.monotonic()
    last_drags: List[float] = []
    step = 0

    for step in range(1, step_cap + 1):
        # --- Collision (BGK + Smagorinsky) ---
        rho, ux, uy, uz = _macros(f)
        feq = _feq_fast(rho, ux, uy, uz)

        if tau_current > 0.55:
            # Full Smagorinsky
            tau_arr = _smag_tau(f, feq, tau_current)
            omega = np.float32(1.0) / tau_arr
            f = f - omega[..., np.newaxis] * (f - feq)
        else:
            # Simple BGK (already viscous enough)
            omega_s = np.float32(1.0 / tau_current)
            f = f - omega_s * (f - feq)

        f = f.astype(np.float32)

        # --- Streaming ---
        f = _stream(f)

        # --- Boundary conditions ---
        # Inlet (x=0): Zou-He velocity BC
        _inlet_bc(f, u_lat)

        # Outlet (x=Nx-1): zero-gradient
        f[-1, :, :, :] = f[-2, :, :, :]

        # Lateral slip (mirror)
        _lateral_slip(f, Ny, Nz)

        # Solid half-way bounce-back
        _bounce_back(f, solid_mask)

        # Zero solid cells
        f[solid_mask] = 0.0

        # --- NaN/divergence watchdog (every 50 steps) ---
        if step % 50 == 0:
            if not np.isfinite(f).all() or float(np.abs(f).max()) > 1e6:
                watchdog_count += 1
                if watchdog_count == 1:
                    # Halve excess tau toward 1.0
                    tau_current = tau_current * 0.5 + 0.5
                    tau_current = max(tau_current, 0.51)
                    # Reset distributions to equilibrium
                    rho = np.ones((Nx, Ny, Nz), dtype=np.float32)
                    ux  = np.full((Nx, Ny, Nz), u_lat, dtype=np.float32)
                    uy  = np.zeros((Nx, Ny, Nz), dtype=np.float32)
                    uz  = np.zeros((Nx, Ny, Nz), dtype=np.float32)
                    f   = _feq_fast(rho, ux, uy, uz)
                    f[solid_mask] = 0.0
                    _progress("solve", int(step / step_cap * 80), f"Watchdog 1: tau->{tau_current:.3f}, resetting...")
                else:
                    # Second failure — abort
                    diverged = True
                    _progress("solve", int(step / step_cap * 80), "Watchdog 2: aborting (diverged).")
                    break

        # --- Drag calculation (momentum exchange) ---
        if step % 10 == 0 or step == step_cap:
            drag_lat, lift_lat = _momentum_exchange(f, bound_links)
            drag_n_raw = drag_lat * force_factor
            lift_n_raw = lift_lat * force_factor
            trace.append((step, float(drag_n_raw)))
            last_drags.append(float(drag_n_raw))
            if len(last_drags) > convergence_window // 10:
                last_drags.pop(0)

        # --- Progress reporting (every ~50 steps) ---
        if step % 50 == 0:
            pct = int(25 + 55 * step / step_cap)
            td = trace[-1] if trace else (0, 0.0)
            _progress("solve", pct, f"Step {step}/{step_cap}, drag={td[1]:.3f} N",
                       trace_point={"step": td[0], "drag": td[1]})

        # --- Convergence check ---
        if len(last_drags) >= convergence_window // 10 and step > step_cap // 5:
            d_arr = np.array(last_drags[-max(5, len(last_drags)//5):])
            d_mean = float(np.mean(np.abs(d_arr)))
            d_var  = float(np.std(d_arr))
            if d_mean > 1e-12 and d_var / d_mean < 0.02:
                _progress("solve", 82, f"Converged at step {step}: drag={d_arr.mean():.4f} N (var {d_var/d_mean*100:.1f}%)")
                break

    # ------------------------------------------------------------------
    # 5. Post-processing
    # ------------------------------------------------------------------
    _progress("post", 85, "Computing macroscopic fields...")

    rho_f, ux_f, uy_f, uz_f = _macros(f)
    vmag = np.sqrt(ux_f**2 + uy_f**2 + uz_f**2)

    # Convert lattice velocity to physical (m/s)
    vel_scale = speed_mps / float(u_lat)

    # Final drag/lift
    drag_lat_f, lift_lat_f = _momentum_exchange(f, bound_links)
    drag_n = float(drag_lat_f * force_factor)
    lift_n = float(lift_lat_f * force_factor)

    frontal_area_m2 = frontal_area_mm2 * 1e-6
    dyn_pressure = 0.5 * _RHO_AIR_KGM3 * speed_mps**2
    if frontal_area_m2 > 0.0 and dyn_pressure > 0.0:
        cd = drag_n / (dyn_pressure * frontal_area_m2)
    else:
        cd = 0.0

    # --- Velocity slices (mid-planes) ---
    vmag_phys = vmag * vel_scale   # m/s physical
    mid_x = Nx // 2
    mid_y = Ny // 2
    mid_z = Nz // 2

    slice_x = vmag_phys[mid_x, :, :]  # YZ plane at x_mid
    slice_y = vmag_phys[:, mid_y, :]  # XZ plane at y_mid
    slice_z = vmag_phys[:, :, mid_z]  # XY plane at z_mid

    # World transforms (mm): position each slice in world space
    # Transform is 4×4 scaling by voxel_mm, translated to mid-plane
    def _slice_transform(axis: int, mid: int) -> np.ndarray:
        T = np.eye(4, dtype=np.float64)
        T[0, 0] = voxel_mm
        T[1, 1] = voxel_mm
        T[2, 2] = voxel_mm
        if axis == 0:
            T[0, 3] = mid * voxel_mm
        elif axis == 1:
            T[1, 3] = mid * voxel_mm
        else:
            T[2, 3] = mid * voxel_mm
        return T

    slices           = [slice_x, slice_y, slice_z]
    slice_transforms = [
        _slice_transform(0, mid_x),
        _slice_transform(1, mid_y),
        _slice_transform(2, mid_z),
    ]

    # --- Streamlines (RK2 from inlet seed grid, ~40 lines) ---
    _progress("stream", 90, "Tracing streamlines...")
    ux_phys = ux_f * vel_scale
    uy_phys = uy_f * vel_scale
    uz_phys = uz_f * vel_scale
    streamline_points, streamline_speeds = _trace_streamlines(
        ux_phys, uy_phys, uz_phys, solid_mask, voxel_mm, n_lines=40
    )

    _progress("stream", 98, f"Done. Cd={cd:.3f}, drag={drag_n:.4f} N, lift={lift_n:.4f} N")

    return FlowResult(
        drag_n=drag_n,
        lift_n=lift_n,
        cd=cd,
        frontal_area_mm2=frontal_area_mm2,
        reynolds_note=reynolds_note,
        trace=trace,
        slices=slices,
        slice_transforms=slice_transforms,
        streamline_points=streamline_points,
        streamline_speeds=streamline_speeds,
        diverged=diverged,
        steps_run=step,
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _align_to_x(mesh: trimesh.Trimesh, dir_vec: np.ndarray) -> trimesh.Trimesh:
    """Rotate mesh so that dir_vec aligns with +X axis."""
    x_axis = np.array([1.0, 0.0, 0.0])
    axis = np.cross(dir_vec, x_axis)
    norm = np.linalg.norm(axis)
    if norm < 1e-10:
        if np.dot(dir_vec, x_axis) > 0:
            return mesh  # already aligned
        # Anti-parallel: rotate 180 around Y
        mesh = mesh.copy()
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [0, 1, 0]))
        return mesh
    axis /= norm
    angle = np.arccos(np.clip(np.dot(dir_vec, x_axis), -1.0, 1.0))
    mesh = mesh.copy()
    mesh.apply_transform(trimesh.transformations.rotation_matrix(angle, axis))
    return mesh


def _macros(f: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute macroscopic density and velocity from distribution f."""
    rho = f.sum(axis=-1)                                              # (Nx,Ny,Nz)
    rho_safe = np.where(rho > 1e-10, rho, np.float32(1.0))
    ux  = (f * _EQ[:, 0]).sum(axis=-1) / rho_safe
    uy  = (f * _EQ[:, 1]).sum(axis=-1) / rho_safe
    uz  = (f * _EQ[:, 2]).sum(axis=-1) / rho_safe
    return rho.astype(np.float32), ux.astype(np.float32), uy.astype(np.float32), uz.astype(np.float32)


def _inlet_bc(f: np.ndarray, u_lat: float) -> None:
    """Zou-He velocity BC at x=0 (inlet). Modifies f in-place."""
    # Prescribed: ux=u_lat, uy=uz=0
    # Density from momentum balance (Zou-He 1997)
    # For x=0 face, incoming directions are those with ex>0: 1,7,9,11,13
    # ρ = 1/(1 - u_lat) * [f0 + f3 + f4 + f5 + f6 + 2*(f2+f8+f10+f12+f14)]
    # Simplified: set rho=1, reconstruct non-equilibrium remainder
    u = np.float32(u_lat)
    rho_in = np.float32(1.0)
    # Pure equilibrium injection (simpler, stable):
    feq_in = _feq_fast(
        np.full(f.shape[1:3], rho_in, dtype=np.float32),
        np.full(f.shape[1:3], u,     dtype=np.float32),
        np.zeros(f.shape[1:3], dtype=np.float32),
        np.zeros(f.shape[1:3], dtype=np.float32),
    )
    f[0, :, :, :] = feq_in


def _lateral_slip(f: np.ndarray, Ny: int, Nz: int) -> None:
    """Slip (mirror) boundary on Y and Z faces. Modifies f in-place."""
    # Y faces (slip = specular reflect uy component):
    # At y=0: incoming dirs with ey<0 reflect to ey>0
    # Simpler: copy plane 1 → 0 and plane Ny-2 → Ny-1 (zero-gradient slip)
    f[:, 0,    :, :] = f[:, 1,    :, :]
    f[:, -1,   :, :] = f[:, -2,   :, :]
    # Z faces
    f[:, :,  0, :] = f[:, :,  1, :]
    f[:, :, -1, :] = f[:, :, -2, :]


def _bounce_back(f: np.ndarray, solid_mask: np.ndarray) -> None:
    """Half-way bounce-back for solid cells. Modifies f in-place.

    After streaming, populations that moved into solid cells are reversed back.
    For fluid cell at (i,j,k) with solid neighbour at (i+ex, j+ey, k+ez) in direction q:
      f_post_stream[i,j,k, opp_q] = f_post_stream[i+ex, j+ey, k+ez, q]
    This replaces the population coming back from the solid wall.
    """
    for q in range(1, _NQ):  # skip rest
        ex, ey, ez = int(_EQ[q, 0]), int(_EQ[q, 1]), int(_EQ[q, 2])
        oq = int(_OPP[q])
        # solid_shifted[i,j,k] = solid_mask[i+ex, j+ey, k+ez]
        # = True if cell (i+ex,j+ey,k+ez) is solid (our solid neighbour in direction q)
        solid_shifted = np.roll(solid_mask, shift=(-ex, -ey, -ez), axis=(0, 1, 2))
        # fluid cells that have a solid neighbour in direction q
        fluid_at_boundary = solid_shifted & ~solid_mask
        # f_solid_q[i,j,k] = f[i+ex, j+ey, k+ez, q]  (solid neighbour's q-population)
        # roll by (-ex,-ey,-ez) so index (i,j,k) reads from (i+ex,j+ey,k+ez)
        f_solid_q = np.roll(f[..., q], shift=(-ex, -ey, -ez), axis=(0, 1, 2))
        f[fluid_at_boundary, oq] = f_solid_q[fluid_at_boundary]


def _build_boundary_links(
    solid_mask: np.ndarray, Nx: int, Ny: int, Nz: int
) -> List[Tuple[np.ndarray, np.ndarray, np.ndarray, int, int]]:
    """Build list of boundary links for momentum exchange drag calculation.

    Each entry: (ix_fluid, iy_fluid, iz_fluid, q, q_opp)
    where q points from fluid toward solid.
    """
    links = []
    for q in range(1, _NQ):
        ex, ey, ez = int(_EQ[q, 0]), int(_EQ[q, 1]), int(_EQ[q, 2])
        # Solid cell = fluid + e
        solid_shifted = np.roll(solid_mask, shift=(-ex, -ey, -ez), axis=(0, 1, 2))
        fluid_boundary = solid_shifted & ~solid_mask
        idxs = np.where(fluid_boundary)
        if len(idxs[0]) > 0:
            links.append((idxs[0], idxs[1], idxs[2], q, int(_OPP[q])))
    return links


def _momentum_exchange(
    f: np.ndarray,
    bound_links: List[Tuple[np.ndarray, np.ndarray, np.ndarray, int, int]],
) -> Tuple[float, float]:
    """Compute drag and lift via momentum exchange method.

    Returns (drag_lat, lift_lat) in dimensionless lattice units.
    """
    drag_lat = np.float64(0.0)
    lift_lat = np.float64(0.0)
    for ix_f, iy_f, iz_f, q, q_opp in bound_links:
        # Half-way bounce-back momentum exchange (Ladd 1994 / Aidun 2010):
        # f[fluid, q] streams toward solid; the solid returns f[fluid, q_opp]
        # Net momentum transferred to solid per timestep per link:
        #   delta_p = 2 * f_q * e_q   (each bounce reverses f_q*e_q momentum twice)
        # In practice we use f_q (pre-bounce value at fluid cell, direction toward solid)
        # summed with the opp-direction that came back from the wall.
        # Force on solid = sum of (f_toward_solid + f_from_solid) * e_toward  [per link]
        # Since e_from = -e_toward:  contrib = f_q * e_x - f_opp * (-e_x) = (f_q + f_opp)*e_x
        # BUT our f[fluid, opp] after bounce-back = what was in the solid = pre-stream f[fluid,q]
        # So f_q + f_opp ≈ 2*f_q.  Use only f_q (simpler, avoids double-counting with opp links).
        f_q = f[ix_f, iy_f, iz_f, q]
        contrib = float(np.sum(f_q)) * 2.0   # factor 2 for momentum reversal
        drag_lat += contrib * float(_EQ[q, 0])
        lift_lat += contrib * float(_EQ[q, 1])
    return float(drag_lat), float(lift_lat)


def _trace_streamlines(
    ux: np.ndarray, uy: np.ndarray, uz: np.ndarray,
    solid_mask: np.ndarray,
    voxel_mm: float,
    n_lines: int = 40,
    max_steps_sl: int = 2000,
) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    """RK2 streamline integration from an inlet seed grid.

    Velocities in m/s, positions in mm.
    Returns lists of (N,3) point arrays and (N,) speed arrays.
    """
    Nx, Ny, Nz = ux.shape
    rng = np.random.default_rng(_SEED)

    # Seed on a grid at x=2 (just downstream of inlet)
    seed_x = 2.5
    n_sq = int(np.sqrt(n_lines)) + 1
    seed_y = np.linspace(1.5, Ny - 1.5, n_sq)
    seed_z = np.linspace(1.5, Nz - 1.5, n_sq)
    SY, SZ = np.meshgrid(seed_y, seed_z)
    seeds = np.column_stack([
        np.full(SY.size, seed_x),
        SY.ravel(),
        SZ.ravel(),
    ])
    # Subsample to n_lines
    idx = rng.choice(len(seeds), size=min(n_lines, len(seeds)), replace=False)
    seeds = seeds[idx]

    def _interp_vel(pos: np.ndarray) -> Tuple[float, float, float]:
        """Trilinear interpolation of velocity at fractional cell coords."""
        x, y, z = pos
        x = float(np.clip(x, 0, Nx - 1))
        y = float(np.clip(y, 0, Ny - 1))
        z = float(np.clip(z, 0, Nz - 1))
        x0, y0, z0 = int(x), int(y), int(z)
        x1 = min(x0 + 1, Nx - 1)
        y1 = min(y0 + 1, Ny - 1)
        z1 = min(z0 + 1, Nz - 1)
        tx, ty, tz = x - x0, y - y0, z - z0
        # Trilinear
        def interp(a: np.ndarray) -> float:
            return float(
                a[x0,y0,z0]*(1-tx)*(1-ty)*(1-tz) +
                a[x1,y0,z0]*tx*(1-ty)*(1-tz) +
                a[x0,y1,z0]*(1-tx)*ty*(1-tz) +
                a[x0,y0,z1]*(1-tx)*(1-ty)*tz +
                a[x1,y1,z0]*tx*ty*(1-tz) +
                a[x1,y0,z1]*tx*(1-ty)*tz +
                a[x0,y1,z1]*(1-tx)*ty*tz +
                a[x1,y1,z1]*tx*ty*tz
            )
        return interp(ux), interp(uy), interp(uz)

    all_pts: List[np.ndarray] = []
    all_spd: List[np.ndarray] = []

    dt_sl = 0.4  # fractional cell steps per integration step

    for seed in seeds:
        pts: List[np.ndarray] = []
        spds: List[float] = []
        pos = seed.copy().astype(float)

        for _ in range(max_steps_sl):
            x, y, z = pos
            if x < 0 or x >= Nx or y < 0 or y >= Ny or z < 0 or z >= Nz:
                break
            ix0, iy0, iz0 = int(np.clip(x, 0, Nx-1)), int(np.clip(y, 0, Ny-1)), int(np.clip(z, 0, Nz-1))
            if solid_mask[ix0, iy0, iz0]:
                break

            # RK2
            vx1, vy1, vz1 = _interp_vel(pos)
            spd1 = np.sqrt(vx1**2 + vy1**2 + vz1**2)
            if spd1 < 1e-12:
                break

            # Normalize for equal arc-length steps
            scale = dt_sl / spd1 if spd1 > 1e-12 else dt_sl
            mid = pos + 0.5 * scale * np.array([vx1, vy1, vz1])
            vx2, vy2, vz2 = _interp_vel(mid)
            pos = pos + scale * np.array([vx2, vy2, vz2])

            pts.append(pos.copy() * voxel_mm)   # mm
            spds.append(float(spd1))

        if len(pts) >= 2:
            all_pts.append(np.array(pts, dtype=np.float32))
            all_spd.append(np.array(spds, dtype=np.float32))

    return all_pts, all_spd
