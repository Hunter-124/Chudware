// Topology optimization: manufacturing-aware measures on the tet4 design
// domain (OCCT-free, Eigen + JSON).
//
// * OverhangModel — a differentiable build-direction support HEURISTIC for
//   unstructured tets. It is not Langelaar's layer-wise AM filter (that filter
//   needs a structured layer grid) and it does NOT guarantee a support-free
//   part: it penalizes solid material whose cone below holds no material, with
//   a smooth power-mean support measure. Reported as heuristic everywhere.
// * EnclosedVoidFinder — exact exterior flood fill over tet face adjacency of
//   a thresholded design. Used for candidate acceptance (BESO, level set) and
//   feasibility (all methods). It never fills geometry.
// * ManufacturingReport — what the run actually enforced, and what it only
//   intended (min member via filter radius is intent, not a measured minimum).
//
// All quantities SI (m, m^3); element densities rho in [0,1], rho = 1 solid.
#pragma once

#include "sim/topopt_model.h"

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

// ── Build-direction overhang heuristic
// ──────────────────────────────────────
// Geometry. Heights are measured along the unit build direction d (the
// direction the part grows). The build plate is the plane through the lowest
// referenced node; an element whose lowest node lies on it (1e-6 of the bbox
// diagonal) is plate-supported. Element f supports element e when
//   t = (c_e - c_f) . d > 0.05 * size_e                (strictly below),
//   |(c_e - c_f) - t d| <= t tan(overhang_angle) + lateral_allowance * size_e,
//   |c_e - c_f| <= support_radius_factor * size_e,
// i.e. f lies inside the self-supporting cone of half-angle overhang_angle
// around -d, widened by a mesh-resolution allowance. overhang_angle_deg is the
// largest lean from the build direction a wall may have without support.
//
// Relaxed measure. With w_ef = (R_e - |c_e - c_f|) V_f normalized per row,
// eps = 1e-6 and power P,
//   s_e = (sum_f w_ef (rho_f + eps)^P)^(1/P) - eps         (in [0,1])
//   s_e = 1 for plate elements, 0 when the cone holds no element,
//   O(rho) = sum_e V_e rho_e (1 - s_e) / V_total            (in [0,1]).
// The power mean is a smooth under-estimate of the max over the cone. The
// gradient is exact for this definition.
struct OverhangSettings {
  Eigen::Vector3d build_direction = Eigen::Vector3d::UnitZ(); // normalized
  double overhang_angle_deg = 45.0;                           // (0,90)
  double support_radius_factor = 1.5; // R_e = factor * element_size_e, > 0
  double lateral_allowance = 0.5;     // fraction of element size, >= 0
  double power = 8.0;                 // power-mean exponent P >= 1
};

class OverhangModel {
public:
  OverhangModel() = default;
  // Builds the support sets once (CSR). Refuses (ModelError):
  // "invalid_overhang" for bad settings, "memory_budget_exceeded" when the
  // support-set storage does not fit the host memory governor.
  OverhangModel(const TetMesh &mesh, const OverhangSettings &settings);

  bool empty() const { return element_count_ == 0; }
  int element_count() const { return element_count_; }
  const OverhangSettings &settings() const { return settings_; }
  // Elements resting on the build plate.
  int plate_elements() const { return plate_count_; }
  // Elements above the plate whose support cone contains no element at all
  // (always unsupported, e.g. the underside of an overhanging design domain).
  int coneless_elements() const { return coneless_count_; }
  std::size_t support_entries() const { return indices_.size(); }

  // Relaxed unsupported-solid fraction O(rho); writes dO/drho_e (E entries)
  // when `gradient` is non-null. Reuses internal scratch (not thread-safe).
  double measure(const Eigen::VectorXd &rho, Eigen::VectorXd *gradient);
  // Thresholded check: volume fraction of elements rho >= threshold that are
  // neither plate-supported nor have any element rho >= threshold in their
  // cone. Still a heuristic for real support requirements.
  double unsupported_fraction(const Eigen::VectorXd &rho,
                              double threshold) const;
  // Exact label of the formulation (for reports).
  std::string formulation() const;

private:
  OverhangSettings settings_;
  int element_count_ = 0;
  int plate_count_ = 0;
  int coneless_count_ = 0;
  double total_volume_m3_ = 0;
  Eigen::VectorXd volume_m3_;
  std::vector<std::uint8_t> plate_;
  std::vector<int> offsets_; // E+1
  std::vector<int> indices_;
  std::vector<double> weights_; // row-normalized
  Eigen::VectorXd mean_;        // per-element power-mean^P scratch
};

// ── Enclosed voids
// ──────────────────────────────────────────────────────────
// A void element is OPEN when a face-connected path of void elements reaches
// an exterior face of the design domain (TetMesh::face_neighbor == -1).
// Remaining void elements are ENCLOSED; `regions` counts their face-connected
// components.
struct EnclosedVoids {
  int void_elements = 0;
  int open_void_elements = 0;
  int enclosed_elements = 0;
  int regions = 0;
  double enclosed_volume_m3 = 0;
  std::vector<int> enclosed; // ascending element ids
};

class EnclosedVoidFinder {
public:
  EnclosedVoidFinder() = default;
  explicit EnclosedVoidFinder(std::shared_ptr<const TetMesh> mesh);
  // is_void: E entries, nonzero = void. Buffers are reused between calls; the
  // returned reference stays valid until the next call.
  const EnclosedVoids &find(const std::vector<std::uint8_t> &is_void);
  // Convenience: void = rho < threshold.
  const EnclosedVoids &find(const Eigen::VectorXd &rho, double threshold);

private:
  std::shared_ptr<const TetMesh> mesh_;
  std::vector<std::uint8_t> state_; // 0 solid, 1 void unvisited, 2 open, 3 enc
  std::vector<std::uint8_t> void_scratch_;
  std::vector<int> queue_;
  EnclosedVoids result_;
};

// ── Report
// ──────────────────────────────────────────────────────────────────
struct ManufacturingReport {
  // Overhang (present only with a build direction).
  bool overhang_evaluated = false;
  bool overhang_in_objective = false; // overhang_penalty > 0
  Eigen::Vector3d build_direction = Eigen::Vector3d::Zero();
  std::string build_direction_source;
  std::optional<BuildPlate> build_plate;
  double overhang_angle_deg = 0;
  double overhang_penalty = 0;
  double overhang_measure = 0;            // relaxed O(rho) at the final design
  double unsupported_volume_fraction = 0; // thresholded heuristic
  std::string overhang_formulation;
  int plate_elements = 0, coneless_elements = 0;
  // Enclosed voids.
  bool no_enclosed_voids = false;        // requested
  std::string enclosed_void_enforcement; // candidate_acceptance |
                                         // feasibility_check | report_only
  double threshold = 0.5;
  EnclosedVoids enclosed;   // at the final design and threshold
  int rejected_updates = 0; // candidate changes refused over the run
  // Minimum member (intent only).
  std::optional<double> min_member_m;
  double filter_radius_m = 0;
  std::string filter_radius_source;
  std::optional<double> min_member_filter_radius_mm, mesh_floor_radius_mm;
  std::string min_member_regularization; // how the method used the radius
  // Variable ties.
  std::array<bool, 3> symmetry_axes{false, false, false};
  int extrusion_axis = -1;
  int tied_groups = 0;
  double max_tie_distance_m = 0;
};
nlohmann::json to_json(const ManufacturingReport &);

} // namespace topology
} // namespace sim
} // namespace chudware
