// Topology optimization: the native CAD adapter behind sim_run_topology and
// topology_capabilities.
//
// fea_run_topology(study) takes the run envelope the GUI (sim_topology_bc.h
// buildRunEnvelope) and the model tools build:
//   { step_path | points[flat xyz mm] + tets[flat tet4] (+ nodes_per_elem 4),
//     job_id?, id?, name?, type?:"topology", mesh_order?:1, fineness?,
//     target_size_mm?, min_size_mm?, max_size_mm?,
//     material:{E_mpa?, nu?, yield_mpa?, density_g_cm3?, k_w_mk?,
//               alpha_per_k?, cp_j_kgk?, name?},
//     topology:{... topopt_model.h Settings keys ...},
//     fixtures:[{faces:<faceset>, dofs?:[0,1,2], label?} | <faceset>],
//     loads:[{faces, type:"force", vector:[N]} | {faces, type:"pressure",
//             magnitude:MPa (+ inward)}],
//     load_cases:[{name?, weight?, fixtures?, loads}],
//     thermal:{reference_temperature_c?, fixtures:[{faces, temperature_c}],
//              loads:[{faces,type:"flux",magnitude_w_m2} |
//                     {faces,type:"power",power_w} |
//                     {faces,type:"convection",h_w_m2k,ambient_c}]},
//     flow:{viscosity_pa_s, density_kg_m3, brinkman_max?, reference_length_mm?,
//           fluid_k_w_mk?, fluid_cp_j_kgk?, inlets:[{faces, velocity_m_s,
//           temperature_c?}], outlets:[{faces, pressure_pa}], walls:[{faces}]},
//     preserve / exclude:[{faces, thickness_mm?} | {box_min_mm, box_max_mm}],
//     build_plate?:{faces:<faceset>, label?} }
// where <faceset> is a resolved doc_face_points faceset ({points, tol_mm,
// face_normal?, perp_tol_mm?, surface?, selector?}); a one-element array of
// facesets is accepted, an unresolved selector or several facesets are refused.
// Every entry may carry `label`, which names it in evidence and errors.
// A planar, lowest build_plate derives the build direction opposite its
// area-weighted outward boundary normal; explicit directions must agree.
//
// Order of work: the envelope, settings, method/physics compatibility, mesh
// order and every section are validated (including a dry parse_problem_spec
// of the normalized shape) BEFORE meshing or creating any directory. Then a
// unique run directory is created, the mesh is prepared ONCE (tet4; any other
// explicit order is refused), every selector is resolved ONCE through the
// shared fea_prepare oracle, an immutable Problem + physics evaluator is built,
// and the real optimizer runs with progress (report_iteration_progress) and the
// SimJobScope cancel token. The final design is reconstructed and its actual
// domain independently reanalysed (topopt_reanalysis.h). The source STEP is
// only read (hashed before and after).
//
// Result: the RESULT JSON documented in host/src/ui/sim_topology.h, plus
// reconstruction{}, evidence{}, result_dir and fields/density arrays. Report
// units: mm, N, MPa, C, W, Pa, Hz, g; objective units in summary.
//
// Durable artifacts: <topology.result_dir or the owned default
// <data>/Chudware/sim/topology_runs>/native-<utc>-<physics>-<method>-<hex>/
// holding run.json, study.json, normalized_spec.json, history.jsonl,
// checkpoint.json (atomic replace), history.json, density.vtu, surface.stl
// (geometry-valid only; reanalysed raw surface), surface_print.stl (validated
// Taubin-smoothed isosurface; domain caps pinned), overlay.aicm (print surface
// when valid, otherwise reconstruction), reanalysis.vtu, result.json — every one
// created exclusively (never overwriting), hashed (SHA-256) and listed in
// `artifacts`. A write failure fails the run (ok:false, artifact_io); a file
// that was not written is never listed. reanalysis.vtu carries, for every
// stress-bearing case, a point von_mises[@case] (MPa: volume-weighted average
// of the retained incident elements, 0 where no retained element touches the
// node) beside the cell element_von_mises[@case], so FEA viewers and
// render_animation read it like a run_fea_static .vtu.
//
// Never throws.
#pragma once

#include "sim/fea_prepare.h"
#include "sim/topopt_model.h"

#include <nlohmann/json.hpp>

#include <memory>
#include <string>

namespace chudware {
namespace sim {

nlohmann::json fea_run_topology(const nlohmann::json &study);

// Machine-readable method x physics matrix (CAPABILITIES JSON of
// host/src/ui/sim_topology.h), derived from parse_settings applicability and
// the optimizer's check_compatibility; every combination listed, unsupported
// ones with the optimizer's reason. Never throws.
nlohmann::json topology_capabilities();

namespace topology {

// Pre-mesh validation of the whole envelope (keys, settings, compatibility,
// mesh source/order, section applicability, entry shapes via a dry
// parse_problem_spec). Throws ModelError; returns the parsed settings.
Settings validate_topology_envelope(const nlohmann::json &study);

// Resolve the envelope's boundary sections on `mesh` through the shared
// fea_prepare helpers and return normalized parse_problem_spec JSON with the
// given `topology` object and the envelope's material. `include_regions`
// adds the design-domain-only entries: preserve/exclude and explicit flow wall
// labels (a reconstructed domain omits them). Throws ModelError naming the
// failing label. `evidence` (optional) receives per-selection resolution facts.
nlohmann::json resolve_topology_boundaries(const nlohmann::json &study,
                                           const nlohmann::json &topology,
                                           const PreparedMesh &mesh,
                                           bool include_regions,
                                           nlohmann::json *evidence);

struct PreparedTopology {
  Settings settings;
  std::shared_ptr<const PreparedMesh> source_mesh;
  std::shared_ptr<const TetMesh> mesh;
  nlohmann::json normalized;        // parse_problem_spec input
  nlohmann::json boundary_evidence; // resolution facts per selection
  std::shared_ptr<const Problem> problem;
};

// validate_topology_envelope + prepare_fea_mesh (tet4) + resolution +
// prepare_problem. Throws ModelError.
PreparedTopology prepare_topology_study(const nlohmann::json &study);

} // namespace topology
} // namespace sim
} // namespace chudware
