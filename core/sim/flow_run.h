// core/sim/flow_run.h — geometry -> wind-tunnel LBM (drag/lift) in one call.
//
// Bridges triangle geometry to the D3Q19 LBM kernel (lbm.h): voxelize the triangle soup into a
// Cartesian solid mask sized for a wind-tunnel domain, derive lattice params from a PHYSICAL inlet
// speed (mirrors engine/sim/lbm.py::run_wind_tunnel), run the lattice, and return forces. Pure
// numerics — no OCCT, no doc layer (the caller supplies the tessellated triangles).
#pragma once

#include <cstdint>
#include <vector>

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
};

// Run a wind-tunnel LBM over the given bodies. Each body is a triangle soup: positions = flat
// 3*nverts (mm), indices = flat 3*ntris into that body's positions. Returns the LbmResult
// (drag_n, lift_n, cd, frontal_area_mm2, ...) plus reynolds in .error="" on success. ok=false with
// an error string on a bad mesh / oversized domain / diverged solve. Never throws.
LbmResult run_wind_tunnel(const std::vector<std::vector<float>>& body_pos,
                          const std::vector<std::vector<uint32_t>>& body_idx,
                          const FlowParams& fp, double* reynolds_out = nullptr);

}  // namespace sim
}  // namespace aicad
