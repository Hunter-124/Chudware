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
// This header is intentionally mesher-backend-FREE (the mesher.cpp it calls
// confines nglib.h to its own TU), so it stays includable in any link config.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include "sim/sim_cancel.h"
#include "sim/fea_contact.h"

namespace chudware::sim {

struct MeshOut;
struct MeshOptions;
// One study-option parser shared by the solve and its cached display mesh.
MeshOptions mesh_options_from_study(const nlohmann::json& study);
// Shared mesh-only and solved-study governor evidence (RSS is pre-assembly).
nlohmann::json mesh_governor_json(const MeshOut& mesh, int dof_per_node = 3);

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

// Analytic surface descriptor for CURVED facesets (the C2 fix — the curved analogue of
// face_normal/perp_tol above). doc_face_points emits it for quadric faces as
//   surface: {type:"cylinder"|"cone"|"sphere"|"torus", origin:[x,y,z], axis:[x,y,z],
//             radius | semi_angle_rad | major_radius+minor_radius}
// so node membership uses an EXACT distance-to-surface test (tight, perp_tol_mm) plus
// proximity to the sample cloud (large tol_mm, delimits the trimmed extent) instead of
// relying on sparse CAD-triangulation samples with a small Euclidean tol — which missed
// most mesh nodes on e.g. a long cylinder barrel (triangulation only puts vertices where
// curvature demands; along a cylinder axis that is the two end rings).
struct FacesetSurface {
    enum Kind { kNone, kCylinder, kCone, kSphere, kTorus };
    Kind kind = kNone;
    double origin[3] = {0, 0, 0};  // axis point (cyl/torus), center (sphere), apex (cone)
    double axis[3] = {0, 0, 1};    // unit axis direction (cyl/cone/torus; unused for sphere)
    double r1 = 0.0;               // radius (cyl/sphere), major radius (torus)
    double r2 = 0.0;               // minor radius (torus)
    double semi_angle = 0.0;       // half-angle, radians (cone)
};

// Parse faceset["surface"] into `out`. Returns true when a valid descriptor is present.
bool faceset_surface(const nlohmann::json& fs, FacesetSurface& out);

// Unsigned distance from point p to the (infinite/untrimmed) analytic surface.
double faceset_surface_distance(const FacesetSurface& s, const double* p);

// nodes_in_faceset with an analytic surface test: a node is kept iff
//   faceset_surface_distance(surface, node) <= perp_tol_mm   (ON the surface)
//   AND 3D distance to the nearest sample <= tol_mm          (within the trimmed extent)
std::vector<int> nodes_in_faceset_surface(const std::vector<double>& points,
                                          const std::vector<double>& sample, double tol_mm,
                                          const FacesetSurface& surface, double perp_tol_mm);

// Combined static run. `study` carries:
//   step_path (string) -> Polyhedral-FEA mesh
//   OR points[n*3] + tets[m*K] inline
//   fineness (0..1), mesh_order (1=tet4, 2=tet10)
//   material: {E_mpa, nu, yield_mpa, density_g_cm3}
//   fixtures: [faceset]
//   loads:    [{faces:faceset, type:"force"|"pressure",
//               vector:[x,y,z]|null, magnitude:<float>|null}]
// A faceset is a terminal {points:[[x,y,z]...], tol_mm, normal?, surface?}
// or {facesets:[terminal,...]} with at least one child, no nesting and no parent
// points/tol_mm/normal/surface. Nodes and facets are deduplicated; one force is
// distributed over the union once. A facet must belong wholly to one child.
// Returns {ok, displacement, disp_magnitude, von_mises, summary_node, n_dof,
//          summary:{max_von_mises_mpa,max_disp_mm,min_fos,...}, solver_notes}.
// Summary scalars are rounded to match the oracle (vm .4, disp .6, fos .4).
//
// summary.max_von_mises_mpa is a MASKED reduction: stress at an ideal fixed
// constraint is a discretization singularity, so nodes within two element rings
// of a fixed node do not feed it (nor min_fos / the principal+shear extrema).
// The mask is data, not a footnote — `summary_node` is the per-node 1/0 array
// that reduction used, `summary.max_von_mises_reduction` names the reduction
// ("node_averaged_excluding_fixture_ring2", or "node_averaged" when nothing was
// excluded), `summary.max_von_mises_raw_mpa` / `min_fos_raw` are the same
// reduction with no mask, and `summary.reduction_masked_keys` /
// `reduction_unmasked_keys` say which scalars in that object share the mask
// (Tresca/Rankine are unmasked). So
//   max{von_mises[i] : summary_node[i]} == summary.max_von_mises_mpa
//   max{von_mises[i]}                   == summary.max_von_mises_raw_mpa
// hold for the reply and for the .vtu, which carries both arrays.
// Never throws: a bad mesh / unsolvable model returns {ok:false, error}.
nlohmann::json fea_run_static(const nlohmann::json& study);

namespace detail {
// Reuse an enclosing study's cancellation token without replacing its job-id
// registration. This keeps composite studies (fatigue -> static) continuously
// cancellable while the static wrapper runs.
nlohmann::json fea_run_static_with_cancel_token(
    const nlohmann::json& study, const SimCancelToken& token);

// Write the optional `vtu_path` evidence file for a finished study. `result` is the
// study result JSON: point data is taken from whichever of displacement (3-vec, mm),
// von_mises (MPa), summary_node (the 1/0 summary-reduction mask), temperature (°C)
// and mode_shapes (flat n_modes*n*3) it carries, so static, adaptive, modal and
// thermal runs all produce the same kind of file. Shipping von_mises WITH its mask
// is what makes the file agree with the packet by construction rather than by
// coincidence — masked max == summary.max_von_mises_mpa, unmasked max ==
// summary.max_von_mises_raw_mpa.
// Returns true when the study asked for no file OR the file was written (path_out set);
// false with `error` when the request is malformed or the write failed. Never throws.
bool write_result_vtu(const nlohmann::json& study, const std::vector<double>& points,
                      const std::vector<int>& tets, int nodes_per_elem,
                      const nlohmann::json& result, std::string& path_out,
                      std::string& error);

// True when the study requests a `vtu_path` whose file is absent. A cached result may
// echo a path the filesystem no longer keeps; the evidence file IS the request, so the
// solve routes treat that as a cache miss instead of promising a missing file.
bool vtu_evidence_missing(const nlohmann::json& study);

// ── The CAD-face gap oracle a contact study measures its clearance with ────────
// Loads `geometry_path` (STEP or BREP — the same file the mesher meshed), explodes
// it into solids, and answers ContactGapProbe::pair_gap_mm with the SIGNED distance
// from each candidate node to the other body's own analytic faces. That is the
// clearance a fit actually has; the node-to-node facet gap is the clearance the two
// meshes happened to sample, and on a curved interface the two differ by most of
// the fit (fea_contact.h ContactGapProbe has the rover hinge numbers).
//
// `reach_mm` bounds the search: a face further than this from a node is not
// consulted, and a node with no face inside it is reported as unmeasurable so the
// caller can fall back to the facet value and count it.
//
// Returns nullptr when the file cannot be read or carries fewer than two solids —
// there is no "other body" then — with `note` saying which. Never throws.
std::unique_ptr<ContactGapProbe> make_cad_gap_probe(const std::string& geometry_path,
                                                    double reach_mm, std::string& note);
}  // namespace detail

// Combined modal run. `study` carries the same mesh + material + fixtures
// fields (loads are ignored — modal is fixtures-only). Returns
// {ok, freqs_hz, mode_shapes, n_modes, n_nodes, n_dof}. Never throws.
nlohmann::json fea_run_modal(const nlohmann::json& study, int n_modes);

// Combined steady-state THERMAL (heat conduction) run. `study` carries the same
// mesh (step_path | inline points/tets) + material fields, plus thermal BCs:
//   material.k_w_mk (float) -> conductivity W/(m·K); default from the study or a fallback
//   fixtures: [{points,tol_mm, temp_c:<float>}]        Dirichlet fixed-temperature faces
//   loads:    [{faces:{points,tol_mm}, type:"flux",       magnitude:<W/m^2>}   heat flux
//              {faces:{points,tol_mm}, type:"convection",  h:<W/m^2K>, t_amb_c:<°C>}]
// The mesh length unit is mm, so the glue converts W/(m·K)→W/(mm·K), W/m^2→W/mm^2,
// W/(m^2·K)→W/(mm^2·K) before calling the kernel (fea_thermal_solve). Returns
//   {ok, temperature[n], n_nodes, n_dof, n_elems, mesh_type_used,
//    summary:{max_temp_c, min_temp_c}, flux_summary:[...per convection/flux face...],
//    solver_notes}.
//
// TRANSIENT: when the study carries a "transient" object the run integrates
// C dT/dt + K T = f(t) (backward Euler; see fea_thermal.h) instead of the steady solve:
//   material: adds cp_j_kgk (specific heat, J/(kg·K)) + density_g_cm3 | rho_kg_m3
//   transient: {t_end_s, dt_s, t_init_c (default 20), frame_times_s:[...],
//               capacitance:"lumped"|"consistent" (default lumped)}
//   loads: each "flux" load may add a time schedule (dimensionless multiplier on q):
//       schedule: {duty:<0..1>, period_s:<s>}                    duty-cycle shorthand
//       schedule: {points:[[t_s,scale],...], period_s:<s,opt>}   piecewise sample-and-hold
//     This is the consumption shape for aicad.powerprofile/1 per-rail duty profiles:
//     rail duty d over period p -> {duty:d, period_s:p}; a residency timeline
//     maps to points verbatim. Multiple flux loads keep INDEPENDENT schedules.
// Returns {ok, transient:true, frames:[{t_s, temperature[n]}], envelope_max[n],
//          n_steps, summary:{max_temp_c (peak over the run), max_temp_time_s,
//          min_temp_c, final_max_temp_c}, flux_summary, solver_notes}. The steady path
// (no "transient" key) is unchanged bit-for-bit.
// Never throws: a bad mesh / singular model returns {ok:false, error}.
nlohmann::json fea_run_thermal(const nlohmann::json& study);

// Combined linear buckling on the static study's real mesh, elastic material,
// fixtures and reference force/pressure loads. STEP defaults to tet10.
// Returns modes[{load_factor,participation:{x,y,z}}], critical_load_factor,
// critical_load_n (= factor * magnitude of the reference applied resultant),
// full max-component-normalized mode_shapes, mesh_quality and engine provenance.
// Participation is the axis share of squared nodal shape, not effective mass.
// study.vtu_path writes mode_1, mode_2, ... 3-vector point fields.
// No compressive reference stress/positive buckling factor -> {ok:false,error,reason}.
nlohmann::json fea_run_buckling(const nlohmann::json& study, int n_modes = 4,
                               double max_load_factor = 1e4);

}  // namespace chudware::sim
