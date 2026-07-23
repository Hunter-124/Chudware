// core/sim/flow_run.h — geometry -> wind-tunnel LBM (drag/lift) in one call.
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

namespace aicad {
namespace sim {

struct FlowParams {
    double velocity_ms = 10.0;  // physical inlet speed (m/s)
    double aoa_deg = 0.0;       // angle of attack (deg, pitch about Y; flow is +X, lift +Z)
    double nu_fluid = 1.5e-5;   // kinematic viscosity (air ~1.5e-5, water ~1.0e-6) m^2/s
    double rho_fluid = 1.225;   // density (air 1.225, water ~1000) kg/m^3
    int cells = 96;             // cells along the DOMAIN's longest axis (resolution)
    int n_steps = 2000;         // LBM time steps
    // Optional progress side-channel: if set, the solver writes a small JSON file
    // {"pct","step","n_steps","stage"} here every ~2% so a long background LBM run can be
    // polled live (get_job) the way the OpenFOAM path reports progress. Empty = no progress.
    std::string progress_path;
    // Optional cancel identity: if set, the run registers in the sim-job registry
    // (sim/sim_cancel.h) for its duration so sim_cancel(job_id) stops it at the next LBM
    // checkpoint. Empty = anonymous run (not registered, not cancellable).
    std::string job_id;
};

// Run a wind-tunnel LBM over the given bodies. Each body is a triangle soup: positions = flat
// 3*nverts (mm), indices = flat 3*ntris into that body's positions. Returns the LbmResult
// (drag_n, lift_n, cd, frontal_area_mm2, ...) plus reynolds in .error="" on success. ok=false with
// an error string on a bad mesh / oversized domain / diverged solve. Never throws.
LbmResult run_wind_tunnel(const std::vector<std::vector<float>>& body_pos,
                          const std::vector<std::vector<uint32_t>>& body_idx,
                          const FlowParams& fp, double* reynolds_out = nullptr);

// ── sim_run_flow_stl (host sim-panel flow path; T-RF wave: kills the Python sim worker) ──
//
// JSON-in/JSON-out front-end mirroring the retired Python sim worker's sim_run_flow envelope
// (engine_oracle/sim/worker.py). Params (all optional except stl_path):
//   stl_path (str, mm), direction [x,y,z] (base flow axis, default [1,0,0]),
//   speed_mps (float, 10), preset ("coarse"|"medium"|"fine"|"xfine" → cells/steps per the
//   oracle's _PRESETS), cells?/n_steps? (explicit overrides), aoa_deg, sideslip_deg,
//   fluid {name, density_kgm3, kinematic_visc_m2s} (default air), roughness_um,
//   ref_area_mm2 (0 = auto frontal), solver ("lbm" only — "openfoam" returns a structured
//   unsupported_solver error), job_id?, progress_path? (same side-channels as FlowParams).
// The composed flow direction (base + AoA + sideslip, oracle _compose_flow_dir) is aligned to
// the tunnel's +X by rotating the STL triangles, then the SAME X-scanline voxelization + LBM
// as run_wind_tunnel runs (aoa is baked into the rotation — FlowParams.aoa_deg stays 0).
//
// Result mirrors the worker envelope the sim panel consumes: drag_n, lift_n, side_n, cd, cl,
// ld, reynolds, frontal_area_mm2, ref_area_mm2, reynolds_note, diverged, trace (empty — the
// C++ kernel keeps no drag history; the panel's live sparkline is progress-file driven),
// cp_min, cp_max, vmax_mps, solver_notes, scalar_fields (empty). NOT PORTED: the overlay
// package (flow_overlay AICM / streamlines / slice PNGs) — the result carries
// "overlay_pending": true and no overlay_aicm; the panel degrades to forces-only.
// side_n is 0.0: the C++ kernel's momentum exchange reports drag/lift only.
// Failures return {"ok": false, "error", "code", "message"} (never throws).
nlohmann::json run_flow_stl(const nlohmann::json& params);

}  // namespace sim
}  // namespace aicad
