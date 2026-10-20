// Topology optimization: reconstruction of the thresholded design and an
// independent reanalysis of the ACTUAL reconstructed domain.
//
// Reconstruction. The design's solid {d >= threshold} is cut from the design
// domain by the surface module (clip_nodal_domain): one vertex set yields the
// oriented surface (STL / overlay) and the conforming clipped tetrahedra whose
// boundary is that surface. The nodal field d is the volume-weighted nodal
// average of the element densities with ONE explicit, labelled passive policy
// (`kNodalPolicy`): nodes of fixed-solid elements are raised to at least the
// threshold, nodes of fixed-void elements are set to 0, and nodes shared by a
// fixed-solid and a fixed-void element sit exactly at the threshold. Under the
// P1 tie rule that keeps every fixed-solid element whole and leaves no volume
// inside a fixed-void element (only zero-volume pieces at shared nodes). The
// policy is not trusted: `measure_passive_geometry` measures, per element, the
// clipped volume against the passive roles of the optimized problem, and the
// reconstruction is only geometry-valid when that measurement, the surface
// evidence and the clipped-boundary/surface match all pass.
//
// Reanalysis. The reconstructed domain is meshed from the clipped tetrahedra
// (no remeshing, no binary density on the old grid), the study's ORIGINAL
// boundary selectors are resolved again on that new mesh through the same
// shared fea_prepare oracle (via the caller's resolver), a fresh immutable
// Problem is prepared and the physics evaluator is run once:
//   * solid-phase physics: the clipped solid, every element rho = 1;
//   * flow: the clipped FLUID complement {d <= threshold}, rho = 0 (pure Stokes
//     on the actual channel geometry, no Brinkman resistance anywhere);
//   * CHT: the conforming two-phase mesh (solid clip + fluid clip merged on
//     their shared node/edge-keyed vertices), rho = 1 / 0. The solid phase is
//     still a Brinkman penalization, so the reported flow leakage through it is
//     an explicitly labelled approximation.
// A boundary or domain that does not survive reconstruction is reported as a
// named failure; nothing is repaired.
//
// OCCT-free; uses fea_prepare.h types (lives beside the adapter in the mesh
// layer). All Problem/Evaluation values are SI like topopt_model.h.
#pragma once

#include "sim/fea_prepare.h"
#include "sim/topopt_model.h"
#include "sim/topopt_surface.h"

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <atomic>
#include <functional>
#include <memory>
#include <string>

namespace chudware {
namespace sim {
namespace topology {

// Factory dispatch for every implemented physics (structural, thermal,
// modal/buckling, flow/cht, thermomechanical). ModelError from the factory.
std::unique_ptr<Physics> make_topology_physics(std::shared_ptr<const Problem>);

inline constexpr const char *kNodalPolicy =
    "volume_weighted_nodal_average; fixed-solid element nodes raised to >= "
    "threshold; fixed-void element nodes set to 0; nodes shared by fixed-solid "
    "and fixed-void elements set to the threshold";

// Nodal extraction field (N; NaN on unreferenced nodes) under kNodalPolicy.
// `evidence` (optional) receives the override counts.
Eigen::VectorXd reconstruction_nodal_density(const Problem &,
                                             const Eigen::VectorXd &rho,
                                             double threshold,
                                             nlohmann::json *evidence);

// Snap tolerance used for a threshold: min(1e-4, min(t, 1 - t) / 4).
double reconstruction_snap(double threshold);

// Measured clipped volume per design-domain element against the passive roles:
// {fixed_solid:{elements, volume_mm3, missing_volume_mm3, incomplete_elements},
//  fixed_void:{elements, volume_mm3, solid_volume_mm3, invaded_elements},
//  preserve:{...}, exclude:{...}, flow_ports:{...}, satisfied:bool,
//  relative_tolerance}. For flow/CHT the fixed-void roles are the fluid ones.
nlohmann::json measure_passive_geometry(const Problem &, const ClippedDomain &);

struct Reconstruction {
  double threshold = 0.5, snap = 1e-4;
  Eigen::VectorXd nodal_density; // N, kNodalPolicy field actually extracted
  ClippedResult solid;           // surface + conforming clipped solid volume
  bool passive_satisfied = false;
  bool geometry_valid = false; // surface Valid, boundary matches, passive ok
  nlohmann::json evidence = nlohmann::json::object();
};
// Threshold = problem.settings.density_threshold. ModelError from the surface
// module (invalid_density, resource_limit, ...).
Reconstruction reconstruct_design(const Problem &, const Eigen::VectorXd &rho);

// Resolves the study's original selectors on `mesh` and returns normalized
// parse_problem_spec JSON built with the given `topology` object (no
// preserve/exclude/build_plate: design-domain-only sections are omitted; the
// reconstructed geometry already embodies passive regions).
// Throws ModelError naming the selector label that failed.
using BoundaryResolver = std::function<nlohmann::json(
    const PreparedMesh &mesh, const nlohmann::json &topology,
    nlohmann::json *evidence)>;

// The topology object a reanalysis prepares with: the design's object with
// volume_fraction 1, filter_radius_mm 0 and the design-variable-only keys
// (ties, overhang, enclosure, min member, region depths, result_dir) removed.
nlohmann::json reanalysis_topology(const nlohmann::json &topology);

struct ReanalysisResult {
  // "completed" | "failed"; failures carry a named code:
  // reconstruction_empty, reconstruction_mesh_invalid,
  // reconstruction_boundary_missing, reconstruction_problem_invalid,
  // reconstruction_analysis_failed, reconstruction_nonconforming.
  std::string status = "failed";
  std::string code, message;
  std::string domain; // clipped_solid | clipped_fluid | conforming_two_phase
  std::shared_ptr<const TetMesh> mesh;
  std::shared_ptr<const Problem> problem;
  Eigen::VectorXd density; // per reanalysis element (1 solid, 0 fluid)
  bool evaluated = false;
  Evaluation evaluation; // values only (no derivatives)
  nlohmann::json evidence = nlohmann::json::object();
};

// Throws EvaluationCancelled when *cancel is raised; every other failure is
// returned as a named status.
ReanalysisResult reanalyze_reconstruction(const Problem &design,
                                          const Reconstruction &,
                                          const nlohmann::json &topology,
                                          const BoundaryResolver &resolver,
                                          const std::atomic<bool> *cancel);

} // namespace topology
} // namespace sim
} // namespace chudware
