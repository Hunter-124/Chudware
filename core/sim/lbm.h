// D3Q19 BGK Lattice-Boltzmann wind-tunnel core — C++ port of engine/sim/lbm.py.
//
// This is the eventual CUDA payload: the lattice numerics (equilibrium, BGK +
// Smagorinsky collision, np.roll streaming, inlet / lateral-slip / bounce-back
// boundary conditions, momentum-exchange drag) isolated from the trimesh
// voxelization. The gate (tools/cpp_lbm_parity.py) feeds BOTH this core and the
// Python oracle the SAME precomputed solid mask + lattice params + step count, so
// only the D3Q19 BGK lattice is under test here.
//
// FLOAT32 CONTRACT. The Python oracle stores every field as np.float32 and the
// per-step arithmetic is float32 (collision result is .astype(np.float32), macros
// sum in float32, feq is float32). To match the oracle to ~1e-6 relative the C++
// core MUST use 32-bit float for the per-cell state + arithmetic and replicate the
// SAME reduction order numpy uses (pairwise over the contiguous q axis). The drag
// momentum-exchange accumulator is float64 (the oracle uses np.float64 there).
//
// INDEX LAYOUT. Fields are flat row-major (Nx,Ny,Nz,19) exactly like numpy C-order:
//   f[((x*Ny + y)*Nz + z)*19 + q]
// Macroscopic fields are flat (Nx,Ny,Nz): u[(x*Ny + y)*Nz + z].
//
// D3Q19 direction order, weights, and the opposite-pair table are LOAD-BEARING and
// reproduce engine/sim/lbm.py's _EQ / _W / _OPP exactly (see lbm.cpp).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace aicad::sim {

// Lattice parameters — the SAME scalars run_wind_tunnel computes from the mesh +
// preset, passed in already-resolved so this core is the pure kernel.
struct LbmParams {
    double u_lat = 0.05;     // lattice inlet speed (oracle pins 0.05)
    double tau0 = 1.0;       // base relaxation time (already clamped by caller)
    double voxel_mm = 1.0;   // voxel pitch (mm) — for frontal area + force factor
    double dx_m = 1.0e-3;    // voxel pitch (m)
    double dt_s = 1.0e-6;    // physical timestep (s)
    double rho_air = 1.225;  // reference air density (kg/m^3)
};

struct LbmResult {
    int nx = 0, ny = 0, nz = 0;
    // Macroscopic fields, flat (Nx,Ny,Nz) row-major, lattice units.
    std::vector<float> ux, uy, uz, rho;
    double cd = 0.0;
    double drag_n = 0.0;
    double lift_n = 0.0;
    double frontal_area_mm2 = 0.0;
    int steps_run = 0;
    bool diverged = false;
    // Convergence indicator (filled by lbm_run): residual = coefficient of variation of the drag
    // over the last fifth of the run. converged = residual < 1% (the force has settled). Lets a
    // caller tell a physically-zero result from an under-converged one (no early exit — the full
    // n_steps still run, so this is parity-safe with the oracle).
    double residual = -1.0;
    bool converged = false;
    // Resolution diagnostics (filled by run_wind_tunnel) — the #1 reason lift reads ~0 and frontal
    // area collapses is that a thin feature falls below the voxel pitch. Surfaced so the caller can
    // warn instead of silently trusting an unresolved solve.
    double voxel_mm = 0.0;          // grid pitch (mm)
    double ref_length_mm = 0.0;     // characteristic length used for Reynolds (longest part span)
    double min_feature_mm = 0.0;    // smallest part bbox dimension (mm)
    int min_feature_voxels = 0;     // how many voxels span that smallest dimension
    std::string warning;            // non-empty when the grid likely under-resolves the geometry
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

// Run the D3Q19 BGK lattice for exactly n_steps (no convergence / watchdog early
// exit — the gate runs a fixed small N so both engines walk the identical loop).
//   solid : flat (Nx,Ny,Nz) row-major bool, solid[(x*Ny+y)*Nz+z] != 0 => solid cell.
//   nx,ny,nz : grid dims.
//   p : lattice params (see LbmParams).
//   n_steps : number of LBM time steps to run.
// Mirrors run_wind_tunnel's loop body exactly: macros -> feq -> collide (BGK or
// Smagorinsky depending on tau0>0.55) -> stream -> inlet -> outlet -> lateral slip
// -> bounce-back -> zero-solid, then momentum-exchange drag/Cd at the end. Never
// throws; a bad mask/params returns ok=false with an error string.
LbmResult lbm_run(const std::vector<uint8_t>& solid, int nx, int ny, int nz,
                  const LbmParams& p, int n_steps);

}  // namespace aicad::sim
