// Polyhedral-FEA volume meshing for Chudware simulation studies.
//
// The implementation loads STEP/BREP geometry through the same OpenCASCADE
// build as Chudware, generates a graded tetrahedral mesh, and normalizes it to
// the flat tet4/tet10 representation consumed by the existing static, modal,
// thermal, fatigue, overlay, and viewport paths. Coordinates and element sizes
// are millimetres.
//
// Polyhedral-FEA is BSD-3-Clause and links statically.
#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace chudware::sim {

// Optional learned-advisor telemetry. Absence means the study did not request
// advice; available=false means it requested advice but the model declined or
// was unavailable.
struct MeshAdvisor {
  bool available = false;
  bool applied = false;
  bool vetoed = false;
  bool budget_refusal = false;
  std::string mesher;
  double h_rel = 0.0;
  int order = 1;
  int adapt_passes = 0;
  double eta_target = 0.0;
  double predicted_dof = 0.0;
  double predicted_mesh_ms = 0.0;
  double predicted_solve_ms = 0.0;
  double failure_prob = 0.0;
  double ood_distance = 0.0;
  std::string model_revision;
  std::string note;
};

struct MeshOut {
  std::vector<double> points; // n*3 flat, row-major, millimetres
  std::vector<int> tets;      // m*K flat, 0-based; K is nodes_per_elem
  double char_len_mm = 0.0;
  int n_nodes = 0;
  int n_elems = 0;
  int nodes_per_elem = 4; // 4 (tet4) or 10 (tet10)
  double min_tet_volume_mm3 = 0.0;
  double max_tet_edge_ratio = 0.0;
  // Elements whose projected tet10 mid-edge nodes folded the quadratic map and
  // were reverted to their straight-edge midpoints (see
  // detail::repair_folded_quadratic). 0 for tet4 and for a clean tet10 mesh.
  int quadratic_fallback_elems = 0;

  double aspect_min = 0.0;
  double aspect_avg = 0.0;
  double aspect_max = 0.0;
  double jacobian_min = 0.0;
  double pct_poor = 0.0;
  std::vector<int> aspect_hist;

  std::string mesh_type_used; // "tet4" or "tet10"
  std::string note;
  std::optional<MeshAdvisor> advisor;

  bool ok = false;
  std::string error;
};

struct MeshOptions {
  // 0 = coarse, 1 = fine. Base size derives from the shape bounding-box
  // diagonal.
  double fineness = 0.5;
  int order = 1;               // 1=tet4, 2=tet10
  double target_size_mm = 0.0; // >0 overrides derived base size
  double min_size_mm = 0.0;    // >0 overrides the automatic size-band floor
  double max_size_mm = 0.0;    // >0 overrides the automatic size-band ceiling
  bool learned_advisor = false;
  uint32_t advisor_max_dof = 0; // 0 disables the advisor's DOF budget
};

bool validate_mesh_options(const MeshOptions &options, std::string &error);

inline constexpr double kPoorAspect = 10.0;

// Mesh a STEP/BREP file. Never throws; failures return ok=false with a
// diagnostic. Tet10 connectivity uses Chudware's canonical mid-edge order:
// {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}.
MeshOut mesh_step(const std::string &step_path, const MeshOptions &options);

// Content-and-options keyed immutable mesh cache. A hit returns the resident
// mesh without copying its points/connectivity arrays.
std::shared_ptr<const MeshOut> mesh_step_cached(const std::string &step_path,
                                                const MeshOptions &options);

struct MeshCacheStats {
  uint64_t hits = 0;
  uint64_t misses = 0;
  uint64_t generations = 0;
  uint64_t resident_bytes = 0;
  uint64_t budget_bytes = 0;
};
MeshCacheStats mesh_cache_stats();
void mesh_cache_reset_stats();
void mesh_cache_note_generation();

namespace detail {
bool fill_quality(MeshOut &out);

// Element-local straight-edge fallback for a promoted+projected tet10 mesh.
//
// Projecting free-surface mid-edge nodes onto the exact BRep is what lets a
// quadratic element follow a bore or a fillet, but when the element size is
// comparable to the wall thickness or the curvature radius the projection can
// push a mid node far enough to FOLD the element: the quadratic Jacobian
// changes sign inside it, or one of its tri6 faces reverses. Such a mesh is
// refused downstream ("invalid or folded quadratic boundary face"), which used
// to force the whole study back to tet4.
//
// This reverts the mid-edge nodes of ONLY the folded elements to the midpoint
// of their straight chord — those elements lose the curved geometry, the rest
// of the mesh keeps it. Reverting a shared mid node can in principle unfold one
// neighbour and fold another, so the sweep repeats until it converges (bounded).
// Returns the number of elements that took the fallback, and records it in
// out.quadratic_fallback_elems. A no-op for tet4.
int repair_folded_quadratic(MeshOut &out);
} // namespace detail

// Error-driven refinement wrapper. Chudware keeps its solver and uses
// Polyhedral-FEA for ZZ recovery, marking, and spatial sizing so every adaptive
// pass remains comparable with an ordinary study result.
class AdaptiveMesher {
public:
  static std::unique_ptr<AdaptiveMesher> open(const std::string &step_path,
                                              const MeshOptions &options,
                                              std::string &error);
  ~AdaptiveMesher();

  AdaptiveMesher(const AdaptiveMesher &) = delete;
  AdaptiveMesher &operator=(const AdaptiveMesher &) = delete;

  bool mesh(MeshOut &out, std::string &error);
  bool estimate_error(const std::vector<double> &disp_mm, double e_mpa,
                      double nu, std::vector<double> &element_eta,
                      double &global_eta, std::string &error);
  bool refine(const std::vector<double> &element_eta, std::string &note,
              std::string &error);
  double element_size_mm() const;

private:
  struct Impl;
  explicit AdaptiveMesher(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};

} // namespace chudware::sim
