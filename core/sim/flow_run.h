// core/sim/flow_run.h — geometry -> wind-tunnel LBM forces in one call.
//
// Bridges triangle geometry to the D3Q19 LBM kernel (lbm.h): voxelize the triangle soup into a
// Cartesian solid mask sized for a wind-tunnel domain, derive lattice params from a PHYSICAL inlet
// speed (mirrors engine/sim/lbm.py::run_wind_tunnel), run the lattice, and return forces. Pure
// numerics — no OCCT, no doc layer (the caller supplies the tessellated triangles).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/lbm.h"

namespace chudware {
namespace sim {

struct FlowParams {
    double velocity_ms = 10.0;  // physical inlet speed (m/s)
    double aoa_deg = 0.0;       // angle of attack (deg, pitch about Y; flow is +X, lift +Z)
    double nu_fluid = 1.5e-5;   // kinematic viscosity (air ~1.5e-5, water ~1.0e-6) m^2/s
    double rho_fluid = 1.225;   // density (air 1.225, water ~1000) kg/m^3
    int cells = 96;             // cells along the DOMAIN's longest axis (resolution)
    int n_steps = 2000;         // LBM time steps
    // Optional progress side-channel: writes {"pct","step","n_steps","stage"}
    // at start, approximately every second (at completed steps), and completion.
    // sim.progress events use the same pct independently of this file.
    std::string progress_path;
    // Optional cancel identity: if set, the run registers in the sim-job registry
    // (sim/sim_cancel.h) for its duration so sim_cancel(job_id) stops it at the next LBM
    // checkpoint. Empty = anonymous run (not registered, not cancellable).
    std::string job_id;
};

// X-scanline parity fill of one closed triangle shell into `mask` (OR), grid (Nx,Ny,Nz) of
// `voxel` mm cells with its origin at 0 (callers shift positions into the grid first).
// pos = flat 3*nverts, idx = flat 3*ntris. Y/Z sample cell centers; along X the default fill
// is conservative (every cell the interior span touches — the wind tunnel's solid mask),
// while center_sample=true keeps only cells whose center lies inside (an unbiased fluid
// volume for internal flow, sim/pipe_flow.h).
void voxelize_body(const std::vector<float>& pos, const std::vector<uint32_t>& idx,
                   std::vector<uint8_t>& mask, int Nx, int Ny, int Nz, double voxel,
                   bool center_sample = false);

// Load a binary or ASCII STL (mm) into a triangle soup: pos = flat 3*nverts, idx = flat
// 3*ntris sequential (no vertex dedup). False with `err` on any malformed input.
bool load_stl_soup(const std::string& path, std::vector<float>& pos,
                   std::vector<uint32_t>& idx, std::string& err);

// Run a wind-tunnel LBM over the given bodies. Each body is a triangle soup: positions = flat
// 3*nverts (mm), indices = flat 3*ntris into that body's positions. Returns all three
// force components plus fields/diagnostics and writes Reynolds through the optional pointer.
// Bad geometry, unstable setup, non-convergence, and divergence return ok=false; never throws.
LbmResult run_wind_tunnel(const std::vector<std::vector<float>>& body_pos,
                          const std::vector<std::vector<uint32_t>>& body_idx,
                          const FlowParams& fp, double* reynolds_out = nullptr);

// ── sim_run_flow_stl (host sim-panel flow path; replaces the Python sim worker) ──
//
// JSON-in/JSON-out front-end mirroring the retired Python sim worker's sim_run_flow envelope
// (engine_oracle/sim/worker.py). Params (all optional except stl_path):
//   stl_path (str, mm), direction [x,y,z] (base flow axis, default [1,0,0]),
//   speed_mps (float, 10), preset ("coarse"|"medium"|"fine"|"xfine" → cells/steps per the
//   oracle's _PRESETS), cells?/n_steps? (explicit overrides), aoa_deg, sideslip_deg,
//   fluid {name, density_kgm3, kinematic_visc_m2s} (default air), roughness_um,
//   ref_area_mm2 (0 = auto frontal), solver ("lbm" only — "openfoam" returns a structured
//   unsupported_solver error), job_id?, progress_path? (same side-channels as FlowParams),
//   overlay (bool, true; false skips spatial overlay packaging).
// The composed flow direction (base + AoA + sideslip, oracle _compose_flow_dir) is aligned to
// the tunnel's +X by rotating the STL triangles, then the SAME X-scanline voxelization + LBM
// as run_wind_tunnel runs (aoa is baked into the rotation — FlowParams.aoa_deg stays 0).
//
// Result mirrors the worker envelope the sim panel consumes: drag_n, lift_n, side_n, cd, cl,
// ld, reynolds, frontal_area_mm2, ref_area_mm2, reynolds_note, diverged, trace (empty —
// no drag history), cp_min, cp_max, vmax_mps, solver_notes, scalar_fields
// ["pressure_cp","velocity"], and overlay_aicm (model-frame surface SCLR fields plus LINS
// "streamlines"/"slices"). Streamlines use RK2 with trilinear lattice velocities;
// Cp samples the nearest fluid cell beside the wall. These remain qualitative LBM
// fields, not evidence-grade CFD. Overlay failure is disclosed in solver_notes.
// Failures return {"ok": false, "error", "code", "message"} (never throws).
nlohmann::json run_flow_stl(const nlohmann::json& params);

}  // namespace sim
}  // namespace chudware
