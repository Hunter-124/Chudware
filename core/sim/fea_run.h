// Combined FEA run entry — C++ port of the GLUE in engine/sim/worker.py's
// _sim_run_static / _sim_run_modal on top of engine/sim/fea_static.py and
// engine/sim/fea_modal.py.
//
// The mesher (sim/mesher.h, P50) and the solvers (sim/fea_static.h,
// sim/fea_modal.h) are already parity-proven. This layer stitches them
// together for the host sim worker: it takes the study dict the host sends
// (a STEP path or an inline mesh + material + fixtures + force/pressure
// loads), resolves the point-cloud facesets to mesh nodes EXACTLY like the
// oracle's _nodes_in_faceset, builds the nodal force vector EXACTLY like
// run_static (force equal-split + the pressure area-weighted face
// enumeration with its fallback), calls the existing solver, and returns the
// result JSON the host expects.
//
// This header is intentionally gmsh-FREE (the mesher.cpp it calls confines
// gmsh.h to its own TU), so it stays includable in any link config.
#pragma once

#include <vector>

#include <nlohmann/json.hpp>

namespace aicad::sim {

// Resolve mesh nodes within tol_mm of ANY faceset sample point — a direct port
// of engine/sim/fea_static.py:_nodes_in_faceset. points is n*3 flat, sample is
// k*3 flat. Returns SORTED UNIQUE node indices (ascending; node i = the i-th
// (x,y,z) triple). An empty sample yields an empty result.
// When face_normal is non-null, uses split-tolerance mode: the perpendicular component
// of (node - sample) must be <= perp_tol_mm (tight: rejects off-plane nodes), and the
// in-plane component must be <= tol_mm (large: covers entire face). This prevents
// bleed to opposite faces of thin plates. Set face_normal=nullptr for legacy behavior.
std::vector<int> nodes_in_faceset(const std::vector<double>& points,
                                  const std::vector<double>& sample, double tol_mm,
                                  const double* face_normal = nullptr,
                                  double perp_tol_mm = 0.0);

// Combined static run. `study` carries:
//   step_path (string)  -> mesh via mesher::mesh_step(step_path, density)
//   OR  points[n*3] + tets[m*4] inline (the deterministic parity-gate path)
//   density (float, default 0.5)
//   material: {E_mpa, nu, yield_mpa, density_g_cm3}
//   fixtures: [{points:[[x,y,z]...], tol_mm}]
//   loads:    [{faces:{points,tol_mm}, type:"force"|"pressure",
//               vector:[x,y,z]|null, magnitude:<float>|null}]
// Returns {ok, displacement, disp_magnitude, von_mises, n_dof,
//          summary:{max_von_mises_mpa,max_disp_mm,min_fos}, solver_notes}.
// Summary scalars are rounded to match the oracle (vm .4, disp .6, fos .4).
// Never throws: a bad mesh / unsolvable model returns {ok:false, error}.
nlohmann::json fea_run_static(const nlohmann::json& study);

// Combined modal run. `study` carries the same mesh + material + fixtures
// fields (loads are ignored — modal is fixtures-only). Returns
// {ok, freqs_hz, mode_shapes, n_modes, n_nodes, n_dof}. Never throws.
nlohmann::json fea_run_modal(const nlohmann::json& study, int n_modes);

}  // namespace aicad::sim
