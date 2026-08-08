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
#include <functional>
#include <string>
#include <vector>

namespace chudware::sim {

// Lattice parameters — the SAME scalars run_wind_tunnel computes from the mesh +
// preset, passed in already-resolved so this core is the pure kernel.
struct LbmParams {
    double u_lat = 0.05;     // lattice inlet speed (oracle pins 0.05)
    double tau0 = 1.0;       // molecular relaxation time; caller must supply stable >0.5
    double voxel_mm = 1.0;   // voxel pitch (mm) — for frontal area + force factor
    double dx_m = 1.0e-3;    // voxel pitch (m)
    double dt_s = 1.0e-6;    // physical timestep (s)
    double rho_air = 1.225;  // reference air density (kg/m^3)
    // Runtime backend select: "auto" (CUDA if built+available else CPU), "cpu", "cuda".
    // Empty string = honour CHUDWARE_LBM_BACKEND env, else "auto". Additive — default keeps
    // every existing caller on the portable OpenMP CPU path unless they opt in.
    std::string backend;
};

struct LbmResult {
    int nx = 0, ny = 0, nz = 0;
    // Macroscopic fields, flat (Nx,Ny,Nz) row-major, lattice units.
    std::vector<float> ux, uy, uz, rho;
    double cd = 0.0;
    double drag_n = 0.0;
    double lift_n = 0.0;
    double side_n = 0.0;
    double frontal_area_mm2 = 0.0;
    int steps_run = 0;
    bool diverged = false;
    // Convergence diagnostics: residual is the relative drift between the two half-window
    // mean drag forces. converged additionally requires <2% inlet/outlet mass-flux mismatch.
    // A settled run may stop before n_steps; steps_run records the actual solver work.
    double residual = -1.0;
    bool converged = false;
    // Final normalized inlet/outlet mass-flux mismatch. Open boundaries do not conserve the
    // domain's total mass exactly, so flux balance is the meaningful continuity diagnostic.
    // -1 means unavailable; finite values near zero are well balanced.
    double mass_imbalance = -1.0;
    // Resolution diagnostics (filled by run_wind_tunnel) — the #1 reason lift reads ~0 and frontal
    // area collapses is that a thin feature falls below the voxel pitch. Surfaced so the caller can
    // warn instead of silently trusting an unresolved solve.
    double voxel_mm = 0.0;          // grid pitch (mm)
    double ref_length_mm = 0.0;     // characteristic length used for Reynolds (longest part span)
    double min_feature_mm = 0.0;    // smallest part bbox dimension (mm)
    int min_feature_voxels = 0;     // how many voxels span that smallest dimension
    std::string warning;            // non-empty when the grid likely under-resolves the geometry
    // Backend actually used + why a requested CUDA path fell back (empty on clean CPU).
    std::string backend;            // "cpu" | "cuda"
    std::string backend_fallback;   // non-empty reason when CUDA was requested but CPU ran
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

// Run the D3Q19 BGK lattice for at most n_steps. A run may stop early once its
// force-window means settle and continuity is within tolerance.
//   solid : flat (Nx,Ny,Nz) row-major bool, solid[(x*Ny+y)*Nz+z] != 0 => solid cell.
//   nx,ny,nz : grid dims.
//   p : lattice params (see LbmParams).
//   n_steps : maximum number of LBM time steps.
// Runs macros -> feq -> Smagorinsky-BGK collision -> stream -> inlet -> outlet ->
// specular lateral slip -> bounce-back -> zero-solid, then momentum-exchange
// drag/Cd. Never throws; a bad mask/params returns ok=false with an error string.
// `on_progress(step, n_steps)` (optional) is invoked at the convergence checkpoints and
// once with (n_steps,n_steps) after a successful early stop so job progress reaches 100%.
// `should_cancel()` (optional) is polled at the same checkpoints; when it returns true the
// run stops early with ok=false, error "cancelled by sim_cancel at step ...", and steps_run =
// the steps completed so far.
LbmResult lbm_run(const std::vector<uint8_t>& solid, int nx, int ny, int nz,
                  const LbmParams& p, int n_steps,
                  const std::function<void(int, int)>& on_progress = {},
                  const std::function<bool()>& should_cancel = {});

// True when this build was compiled with CHUDWARE_WITH_CUDA and a CUDA device is present.
bool lbm_cuda_available();

}  // namespace chudware::sim
