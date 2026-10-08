// core/sim/pipe_flow.h — native internal-flow LBM over a closed fluid volume (sim_run_pipe_flow).
//
// The input mesh IS the fluid: a watertight shell such as a pipe bore from
// piping_internal_volume, a duct, a manifold or a valve cavity. It is center-sampled onto a
// Cartesian grid (flow_run.h voxelize_body) in a frame whose X axis is the inlet axis, the
// inlet/outlet planes pick out one voxel layer each, and a D3Q19 lattice runs to steady state
// at the PHYSICAL Reynolds number:
//   * incompressible equilibrium (He–Luo, so the pressure field does not feed back into the
//     velocity through density);
//   * laminar (Re < 2300): TRT collision at the "magic" parameter 3/16, which keeps half-way
//     bounce-back walls viscosity-independent (regularized BGK when tau nears 1/2), developed
//     inlet profile from a cross-section Poisson solve; validated against Hagen–Poiseuille;
//   * turbulent (Re >= 2300, model "mixing_length+wall_function"): steady RANS — regularized
//     BGK with an algebraic eddy viscosity (mixing length kappa y with van Driest damping,
//     capped at 0.09 D_h/2, over Reichardt's pipe distribution) and a force-based wall
//     function: the bounce-back's tangential momentum exchange at each wall cell is replaced
//     by the Spalding law-of-the-wall stress (u_tau from the first fluid cell off the wall,
//     calibrated lattice coefficient). The inflow is recycled from a few diameters down the
//     straight inlet leg (fully developed) and the inlet-plane pressure follows that leg's
//     developed gradient. Validated against Colebrook/Clamond (straight pipe, Re 1e4..1e6) and
//     Rennels (90-degree long-radius bend) in core/tests/test_sim.cpp;
//   * velocity inlet + constant-pressure outlet by non-equilibrium extrapolation;
//   * convergence on the inlet-to-outlet pressure-drop change plus a flux-balance check
//     (turbulent: statistical stationarity of window-averaged dp).
// Lattice units convert to physical units by density and the velocity scale taken from the
// measured lattice flow rate (dp = dp_lat · rho · (U/U_lat)^2); darcy_estimate_pa reports the
// 1-D Darcy–Weisbach (Colebrook/Clamond) straight friction over the same path, and with
// piping + run_id one_d_estimate_pa adds that run's fitting/bend K-factors (piping_solve's
// Rennels/Crane set, core/piping/solve.h run_loss_1d) for a like-for-like comparison.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::sim {

// 1-D loss model of one piping run at a flow (chudware::piping::run_loss_1d — the sim library
// does not link the piping library, so the caller injects it): (piping doc, run_id, rho kg/m3,
// mu Pa·s, flow m3/s, roughness_mm (< 0 = run material)) -> {ok, total_pa, friction_pa,
// fittings:[{id,type,K,method,dp_pa}], bends, ...} | {ok:false, reason, hint}.
using PipeRunLossFn = nlohmann::json (*)(const nlohmann::json& piping, const std::string& run_id,
                                         double rho, double mu, double flow_m3s,
                                         double roughness_mm);

// One closed triangle shell (mm): pos = flat 3*nverts, idx = flat 3*ntris.
struct PipeFlowShell {
    std::vector<float> pos;
    std::vector<uint32_t> idx;
};

// Params (lengths mm, SI otherwise):
//   inlet {point:[3], normal:[3]}, outlet {point:[3], normal:[3]} — plane points and normals.
//     Normals may point either way; both are oriented toward the fluid side automatically.
//   density_kgm3, viscosity_pas — fluid (required here; the RPC layer resolves `fluid` names).
//   fluid (string label only), mean_velocity_ms | flow_m3s (exactly one, > 0).
//   voxel_mm? (default 4V/S hydraulic-diameter estimate / cells_across), cells_across?
//   (default 20 laminar — raised to keep Re/cells <= 30 — and turbulent 12 for a straight
//   path or 16 when the path bends: outlet axis/point off the inlet axis, a wall that is not
//   a surface of revolution about it, or a piping run with corners/elbows; a 16-cell lattice
//   over the memory budget falls back to 12 with warning bend_resolution_reduced),
//   max_fluid_cells? (default 3e6, also capped by the memory governor), max_steps? (default
//   ~12 relaxation times), tolerance? (laminar: estimated remaining relative dp residual,
//   default 1e-3; turbulent: at least 1% window-mean stationarity), inlet_profile? ("auto" =
//   developed when laminar else recycled-developed | "developed" | "uniform"), roughness_mm?
//   (Darcy and 1-D estimates only, default 0 = smooth like the lattice walls), piping? +
//   run_id? (the aicad.piping/1 document and run this bore was extracted from: enables the
//   fittings-aware 1-D comparison, and extends the run's straight end legs to 5 D for the
//   solve — the planar end caps are extruded, as far as the cell budget allows (warning
//   end_legs_short otherwise) — so the inflow is developed and the bend wake clears the
//   outlet; dp_pa is then read between the run's own end planes, the outlet side from the
//   developed downstream gradient extrapolated back, as K-factors include downstream
//   recovery; needs one_d_fn), job_id? (sim_cancel registry key),
//   progress_path? ({pct,step,n_steps,stage} file). sim.progress events (phase
//   "pipe_flow_solve", iteration.metrics {dp_pa, mass_imbalance, max_density_deviation}) go to
//   the armed progress sink at every check and at least every 5 s.
// Success: {ok:true, dp_pa, mean_velocity_ms, flow_m3s, reynolds, simulated_reynolds, model,
//   regime, hydraulic_diameter_mm, inlet_area_mm2, path_length_mm (the run's, extensions
//   excluded), mass_imbalance, converged, steps, voxels, pressure_profile:[{s_mm,p_pa}]
//   (gauge to the outlet plane along the flow-path distance from the inlet plane; extended
//   legs show as s < 0 and s > path_length_mm), darcy_estimate_pa, darcy_friction_factor,
//   bends_detected, bend_loss_band? {cells_across, k_ratio_min, k_ratio_max, reference}
//   (turbulent bent paths: measured bend-K over-prediction at the resolution used),
//   dp_domain_pa? + extended_legs? {inlet_mm, outlet_mm, domain_path_length_mm} (extended
//   piping runs), warning outlet_at_bend_exit (STL/doc callers whose outlet is < 3 D_h past a
//   bend; they cannot be extended),
//   one_d_estimate_pa?, cfd_over_one_d?, one_d_breakdown? {run_id, friction_pa,
//   fittings:[{id,type,K,method,dp_pa}], fittings_pa, fittings_K, friction_factor, reynolds,
//   velocity_ms, dynamic_pressure_pa, inner_dia_mm, centreline_length_mm,
//   bend_radius_mm, roughness_mm, material_roughness_mm} (piping + run_id only), fluid,
//   caveats, warnings, hint, next_actions, provenance:{engine:"cpp-lbm-internal", voxel_mm,
//   collision, tau, y_plus_first_cell, inlet_profile, inlet_leg_gradient_pa_per_m,
//   cells_across_default ("straight" | "bend" | null when given), ...}}.
// Refusal: {ok:false, reason, hint} (never throws). Reasons: invalid_params, invalid_geometry,
//   no_geometry, mesh_not_watertight, grid_too_large, grid_too_coarse, insufficient_memory,
//   no_fluid_cells, no_inlet_cells, no_outlet_cells, inlet_outlet_overlap,
//   inlet_outlet_disconnected, pipe_too_short, solver_diverged, cancelled, invalid_stl,
//   one_d_unavailable, and the 1-D model's unknown_run / invalid_piping / degenerate_run.
nlohmann::json run_pipe_flow(const std::vector<PipeFlowShell>& shells,
                             const nlohmann::json& params, PipeRunLossFn one_d_fn = nullptr);

// stl_path front end: loads the STL (mm) as one shell, then run_pipe_flow.
nlohmann::json run_pipe_flow_stl(const nlohmann::json& params, PipeRunLossFn one_d_fn = nullptr);

// Darcy friction factor: 64/Re below Re 2100, else the exact Colebrook solution (Clamond
// 2009 — same form as core/piping/solve.cpp). eD = roughness / diameter.
double darcy_friction_factor(double reynolds, double eD);

}  // namespace chudware::sim
