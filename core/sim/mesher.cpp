// Polyhedral-FEA mesher and adaptive-refinement bridge for Chudware.
#include "sim/mesher.h"
#include "sim/progress.h"

#include "prof/phases.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <unordered_map>
#include <vector>

// Module-path lookup for the staged advisor model directory — the one reason
// this TU ever needs a platform header, so it is admitted only in an
// advisor-linked build. BEFORE the OCCT and polymesh headers: OCCT defines
// WIN32_LEAN_AND_MEAN, which would poison a later <windows.h>, and NOMINMAX has
// to land first or windows.h's min()/max() macros break the std::max /
// std::clamp calls in this file and in polymesh's headers.
#ifdef CHUDWARE_WITH_POLYMESH_ADVISOR
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <dlfcn.h> // dladdr: locate the staged advisor model dir beside this module
#endif
#endif // CHUDWARE_WITH_POLYMESH_ADVISOR

#include <TopoDS_Shape.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepBndLib.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepGProp.hxx>
#include <BRepTools.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_ListOfShape.hxx>

#include "sim/mesh_size_band.h"
#include "sim/solver_threads.h"
#include "util/resource_governor.h"

#include <adapt/graded_sizing.hpp>
#include <adapt/loop.hpp>
#include <fea/backend.hpp>
#include <fea/material.hpp>
#include <fea/nodal_mesh.hpp>
#include <fea/p_elevate.hpp>
#include <fea/zz.hpp>
#include <geom/indicators.hpp>
#include <mesh/cell_validity.hpp>
#include <mesh/fill_progress.hpp>
#include <mesh/grid_classify.hpp>
#include <mesh/local_refine.hpp>
#include <mesh/surface_project.hpp>
#include <pipeline/scene.hpp>

// CHUDWARE_WITH_POLYMESH_ADVISOR is a SEPARATE axis from
// CHUDWARE_WITH_POLYMESH: upstream pins a prebuilt ONNX Runtime for Windows x64
// and non-Apple 64-bit Unix only, so polymesh::advisor — and therefore
// <advisor/advisor.hpp> — simply does not exist on macOS or any other pointer
// width. The graded-tet fill and the adaptive loop above have no advisor
// dependency, so everything the advisor owns (its header, the ONNX session, and
// the module-relative model lookup that needs util/platform_paths.h) lives
// behind this macro and an opted-in study on such a platform gets an honest
// unavailable report.
#ifdef CHUDWARE_WITH_POLYMESH_ADVISOR
#include "util/platform_paths.h"

#include <advisor/advisor.hpp>
#endif

namespace chudware::sim {

bool validate_mesh_options(const MeshOptions &options, std::string &error) {
  if (!std::isfinite(options.fineness) || options.fineness < 0.0 ||
      options.fineness > 1.0) {
    error = "mesh options: fineness must be finite and in [0,1]";
    return false;
  }
  if (options.order != 1 && options.order != 2) {
    error = "mesh options: order must be 1 or 2";
    return false;
  }
  for (double size :
       {options.target_size_mm, options.min_size_mm, options.max_size_mm}) {
    if (!std::isfinite(size) || size < 0.0) {
      error =
          "mesh options: target/min/max sizes must be finite and non-negative";
      return false;
    }
  }
  if (options.min_size_mm > 0.0 && options.max_size_mm > 0.0 &&
      options.min_size_mm > options.max_size_mm) {
    error = "mesh options: min_size_mm cannot exceed max_size_mm";
    return false;
  }
  return true;
}
namespace {

namespace pm = polymesh;

void report_fill_work(std::size_t elements, std::size_t done, std::size_t total) {
  if (auto *progress = pm::mesh::active_fill_progress) {
    progress->set_elements(elements);
    progress->set_cells(done, total);
    progress->poll();
  }
}

void report_fill_phase(const char *phase, int pass = 0, int total = 0) {
  if (auto *progress = pm::mesh::active_fill_progress)
    progress->set_phase(phase, pass, total);
}

// Only optional sizing probes spend this budget. The first measurable mesh
// and the single delivery of a local contract must still run to completion.
struct MeshProbeBudget {
  const std::chrono::steady_clock::time_point start =
      std::chrono::steady_clock::now();
  double seconds = 60.0;
  const char *origin = "default";

  MeshProbeBudget() {
    if (const char *value = std::getenv("CHUDWARE_MESH_PROBE_BUDGET_S")) {
      char *end = nullptr;
      const double configured = std::strtod(value, &end);
      if (end != value && *end == '\0' && std::isfinite(configured) &&
          configured >= 0.0) {
        seconds = configured;
        origin = "CHUDWARE_MESH_PROBE_BUDGET_S";
      }
    }
  }

  double elapsed() const {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - start)
        .count();
  }

  bool admit(double h, double predicted_s, MeshOut &out) const {
    const double remaining = std::max(0.0, seconds - elapsed());
    if (std::isfinite(predicted_s) && predicted_s <= remaining && remaining > 0.0)
      return true;
    out.governor_skipped_probes.push_back(
        {h, std::isfinite(predicted_s) ? predicted_s : 0.0, remaining,
         std::isfinite(predicted_s) ? "predicted_probe_exceeds_remaining_budget"
                                   : "probe_time_forecast_unavailable"});
    report_progress("mesh_probe_skipped", static_cast<std::size_t>(out.n_elems));
    return false;
  }

  void stamp(MeshOut &out) const {
    out.governor_probe_budget_s = seconds;
    out.governor_elapsed_s = elapsed();
    out.governor_budget_origin = origin;
  }
};
// polymesh reads OpenMP thread count once from the environment at process
// start. Chudware governs solver/mesher parallelism centrally so a background
// study cannot starve the GUI render thread, so re-point polymesh at the same
// cap. Runs once per process.
void init_polymesh_runtime() {
  static std::once_flag once;
  std::call_once(once, [] {
    pm::fea::init_runtime_performance();
    const int threads = static_cast<int>(governed_solver_threads());
    if (threads > 0)
      pm::fea::set_openmp_threads(threads);
  });
}

// Chudware's tet10 slots follow the gmsh MSH_TET_10 convention (mesher.h):
// mid-edge nodes on {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}. polymesh orders its
// mids on (0,1),(1,2),(0,2),(0,3),(1,3),(2,3) (fea/nodal_mesh.hpp). The first
// four agree; the last two are swapped. Getting this wrong does not crash — it
// silently produces a warped quadratic element and a wrong stress field — so it
// is a named table, not an inline index.
constexpr int kTet10PolymeshToChudware[6] = {4, 5, 6, 7, 9, 8};

void append_note(std::string &note, const std::string &text) {
  if (text.empty())
    return;
  note = note.empty() ? text : note + "; " + text;
}

// A loaded model plus the resolved element-size band. Shared by the one-shot
// mesh and the adaptive driver, which must not re-parse the STEP on every pass.
struct LoadedModel {
  pm::pipeline::Model model;
  SizeBand band;
};

bool load_model(const std::string &step_path, const MeshOptions &options,
                LoadedModel &loaded, std::string &error) {
  loaded.model = pm::pipeline::Model::load(step_path);
  if (loaded.model.surface.vertices.empty() ||
      loaded.model.surface.triangles.empty()) {
    error = "polymesh: STEP produced no tessellated surface to fill";
    return false;
  }
  // Chudware owns the user-facing fineness control and host-memory governor, so
  // resolve the size band once here and feed the resulting millimetre scale
  // into Polyhedral-FEA.
  if (loaded.model.cad && loaded.model.cad->shape_handle() != nullptr) {
    const TopoDS_Shape &shape =
        *static_cast<const TopoDS_Shape *>(loaded.model.cad->shape_handle());
    loaded.band = resolve_size_band(shape, options);
  }
  if (!loaded.band.ok) {
    error = loaded.band.error.empty()
                ? "polymesh: could not resolve element sizing from the STEP"
                : loaded.band.error;
    return false;
  }
  return true;
}

#ifdef CHUDWARE_WITH_POLYMESH_ADVISOR

// The four files pm::advisor::Advisor actually opens: the graph plus the three
// required sidecars (normalization, clamp/candidate box, OOD veto). Everything
// else in the upstream bench/advisor directory is campaign evidence — tens of
// megabytes of cross-validation dumps — and is not a runtime input, so a
// package stages exactly this list and a resolved directory is only accepted
// when it carries all of it.
constexpr const char *kAdvisorModelFiles[] = {
    "model.onnx", "normalization.json", "clamps.json", "ood.json"};

bool advisor_model_dir_complete(const std::filesystem::path &dir) {
  std::error_code ec;
  if (!std::filesystem::is_directory(dir, ec))
    return false;
  for (const char *name : kAdvisorModelFiles)
    if (!std::filesystem::is_regular_file(dir / name, ec))
      return false;
  return true;
}

// Directory of the module this code was linked into: libchudware.{so,dll}, or a
// test executable that links the mesher archive directly. NOT simply the
// executable's directory — the engine is dlopen'd, and a packaged layout keeps
// the daemon in bin/ and the engine plus its staged siblings in lib/. Same
// module-relative discovery doc/formats.cpp uses to find the lib3mf staged
// beside the engine.
std::filesystem::path this_module_dir() {
  std::filesystem::path module;
#ifdef _WIN32
  HMODULE self = nullptr;
  if (::GetModuleHandleExW(
          GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
              GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
          reinterpret_cast<LPCWSTR>(&advisor_model_dir_complete), &self) &&
      self != nullptr) {
    std::wstring buffer(MAX_PATH, L'\0');
    for (;;) {
      const DWORD n = ::GetModuleFileNameW(self, buffer.data(),
                                           static_cast<DWORD>(buffer.size()));
      if (n == 0) {
        buffer.clear();
        break;
      }
      // GetModuleFileNameW truncates instead of failing when the buffer is
      // short, so grow until the returned length is strictly inside it.
      if (n < buffer.size()) {
        buffer.resize(n);
        break;
      }
      buffer.resize(buffer.size() * 2);
    }
    if (!buffer.empty())
      module = std::filesystem::path(buffer);
  }
#else
  Dl_info info;
  if (::dladdr(reinterpret_cast<void *>(&advisor_model_dir_complete), &info) !=
          0 &&
      info.dli_fname != nullptr)
    module = std::filesystem::path(info.dli_fname);
#endif
  std::error_code ec;
  if (!module.empty()) {
    const std::filesystem::path resolved =
        std::filesystem::weakly_canonical(module, ec);
    if (!ec && std::filesystem::exists(resolved, ec))
      return resolved.parent_path();
  }
  // dladdr names the MAIN executable by the string it was exec'd with, which
  // can be relative ("./test_x") and is then meaningless once the process
  // changes directory. executablePath() is this repo's one canonical answer for
  // that case (/proc/self/exe and its per-OS equivalents), so a test binary
  // still finds the package beside it.
  const std::string exe = chudware::platform::executablePath();
  if (exe.empty())
    return {};
  return std::filesystem::path(exe).parent_path();
}

// Where the advisor's model artifacts live AT RUNTIME.
//
// A packaged install has neither a polymesh source checkout nor the build
// tree's `_deps`, so the configure-time source path compiled into this TU is
// not an answer on its own — baking it in as the only lookup made the advisor
// reachable on exactly one machine: the one that built the binary. Resolution
// order, first hit wins:
//
//   1. CHUDWARE_POLYMESH_ADVISOR_DIR — explicit operator/test override. Set but
//      incomplete is an ERROR, never a silent fall-through to some other
//      directory: a run told which model to use must not quietly recommend from
//      a different one.
//   2. <this module's directory>/polymesh-advisor — the package the build
//   stages beside
//      libchudware, the daemon and the test binaries, and that a release ships
//      beside the engine. This is the production path.
//   3. CHUDWARE_POLYMESH_ADVISOR_SOURCE_DIR — the provisioned source checkout.
//   Present
//      only on a machine that has one, which is what makes an unstaged
//      developer build work; a packaged install simply never finds it.
struct ResolvedModelDir {
  std::filesystem::path dir;
  std::string revision; // reported verbatim as MeshAdvisor::model_revision
  std::string error;    // non-empty => nothing usable was found
};

ResolvedModelDir resolve_advisor_model_dir() {
  ResolvedModelDir out;
  if (const char *override_dir = std::getenv("CHUDWARE_POLYMESH_ADVISOR_DIR");
      override_dir != nullptr && *override_dir != '\0') {
    const std::filesystem::path dir(override_dir);
    if (!advisor_model_dir_complete(dir)) {
      out.error =
          "CHUDWARE_POLYMESH_ADVISOR_DIR=" + dir.string() +
          " does not contain model.onnx + normalization.json + clamps.json + "
          "ood.json";
      return out;
    }
    out.dir = dir;
    // The override may point at a re-exported or experimental model, so
    // claiming the pinned upstream revision here would be a false provenance
    // claim.
    out.revision = "override:" + dir.string();
    return out;
  }

  const std::filesystem::path module_dir = this_module_dir();
  if (!module_dir.empty()) {
    const std::filesystem::path staged =
        module_dir / CHUDWARE_POLYMESH_ADVISOR_PACKAGE_DIR;
    if (advisor_model_dir_complete(staged)) {
      out.dir = staged;
      out.revision = CHUDWARE_POLYMESH_MODEL_REVISION;
      return out;
    }
  }

  const std::filesystem::path source_dir(CHUDWARE_POLYMESH_ADVISOR_SOURCE_DIR);
  if (advisor_model_dir_complete(source_dir)) {
    out.dir = source_dir;
    out.revision = CHUDWARE_POLYMESH_MODEL_REVISION;
    return out;
  }

  out.error =
      "no advisor model directory found (looked for " +
      (module_dir.empty()
           ? std::string("the package beside this module")
           : (module_dir / CHUDWARE_POLYMESH_ADVISOR_PACKAGE_DIR).string()) +
      ", then " + source_dir.string() +
      "); set CHUDWARE_POLYMESH_ADVISOR_DIR to a directory holding model.onnx "
      "and its normalization/clamps/ood sidecars";
  return out;
}

struct AdvisorRuntime {
  std::unique_ptr<pm::advisor::Advisor> model;
  std::string revision = "not-loaded";
  std::string error;

  AdvisorRuntime() {
    ResolvedModelDir resolved = resolve_advisor_model_dir();
    if (!resolved.error.empty()) {
      error = std::move(resolved.error);
      return;
    }
    try {
      model = std::make_unique<pm::advisor::Advisor>(resolved.dir);
      revision = std::move(resolved.revision);
    } catch (const std::exception &e) {
      error = e.what();
    } catch (...) {
      error = "unknown advisor model-load failure";
    }
  }
};

// ONNX session construction validates and loads all resolved advisor artifacts.
// It is deliberately process-wide and lazy: optional-off studies never touch
// the model, while every opted-in study shares the same deterministic
// single-threaded session.
const AdvisorRuntime &advisor_runtime() {
  static const AdvisorRuntime runtime;
  return runtime;
}

#endif // CHUDWARE_WITH_POLYMESH_ADVISOR

// The mesh controls this build will actually use, with nothing predicted: the
// report a study gets whenever the advisor cannot speak. The revision is passed
// IN rather than read here because the honest answer differs per build — the
// revision loaded in THIS process where an advisor exists ("not-loaded" when
// resolution or the ONNX session failed), and "not-built" where the platform
// has no advisor at all. A configure-time constant would name a revision that
// never ran.
MeshAdvisor configured_advisor_fallback(const LoadedModel &loaded,
                                        const MeshOptions &options,
                                        std::string model_revision) {
  MeshAdvisor out;
  out.mesher = "graded_tet";
  out.h_rel =
      loaded.band.diag > 0.0 ? loaded.band.base / loaded.band.diag : 0.0;
  out.order = options.order;
  out.model_revision = std::move(model_revision);
  return out;
}

struct AdvisorPlan {
  MeshOptions options;
  double h = 0.0;
  std::optional<MeshAdvisor> result;
};

#ifdef CHUDWARE_WITH_POLYMESH_ADVISOR

AdvisorPlan make_advisor_plan(const LoadedModel &loaded,
                              const MeshOptions &configured) {
  AdvisorPlan plan{configured, loaded.band.base, std::nullopt};
  if (!configured.learned_advisor)
    return plan;

  const AdvisorRuntime &runtime = advisor_runtime();
  MeshAdvisor report =
      configured_advisor_fallback(loaded, configured, runtime.revision);
  if (!runtime.model) {
    report.note = "advisor unavailable; configured deterministic mesh controls "
                  "retained: " +
                  runtime.error;
    plan.result = std::move(report);
    return plan;
  }

  try {
    // This backend seam does not receive resolved load/fixture regions. The
    // upstream extractor still supplies exact/tessellated geometry features
    // deterministically; empty BC spans and the conventional nu=0.3 sentinel
    // are explicit rather than invented from post-mesh node selections.
    const pm::pipeline::CaseFeatures features =
        pm::pipeline::extract_case_features(loaded.model, {}, {},
                                            Eigen::Vector3d::Zero(), 0.3);
    const pm::advisor::AdvisorDecision decision =
        runtime.model->recommend(features, configured.advisor_max_dof);

    report.available = true;
    report.vetoed = decision.vetoed;
    report.budget_refusal = decision.budget_refusal;
    report.ood_distance = decision.ood_distance;
    report.model_revision = runtime.revision;

    if (decision.vetoed) {
      // The upstream chooser suppresses predictions on refusals. Reassert that
      // boundary here rather than relying on a model revision to keep zero
      // defaults.
      report.note =
          decision.note.empty()
              ? "advisor refused the case; configured deterministic mesh "
                "controls retained and predictions suppressed"
              : decision.note +
                    "; configured deterministic mesh controls retained "
                    "and predictions suppressed";
      plan.result = std::move(report);
      return plan;
    }

    if (!std::isfinite(decision.h_rel) || !(decision.h_rel > 0.0) ||
        (decision.order != 1 && decision.order != 2) ||
        decision.adapt_passes < 0 || !std::isfinite(decision.eta_target) ||
        decision.eta_target < 0.0) {
      report.available = false;
      report.vetoed = true;
      report.note =
          "advisor returned unusable sizing/order/adaptation controls; "
          "configured "
          "deterministic mesh controls retained and predictions suppressed";
      plan.result = std::move(report);
      return plan;
    }

    report.applied = true;
    report.mesher = decision.mesher;
    report.h_rel = decision.h_rel;
    report.order = decision.order;
    report.adapt_passes = decision.adapt_passes;
    report.eta_target = decision.eta_target;
    report.predicted_dof = decision.predicted_dof;
    report.predicted_mesh_ms = decision.predicted_mesh_ms;
    report.predicted_solve_ms = decision.predicted_solve_ms;
    report.failure_prob = decision.failure_prob;
    report.ood_distance = decision.ood_distance;
    report.note = decision.note;

    const double requested_h = decision.h_rel * loaded.band.diag;
    plan.h = std::clamp(requested_h, loaded.band.min_len, loaded.band.max_len);
    plan.options.order = decision.order;
    if (plan.h != requested_h)
      append_note(report.note,
                  "advisor h_rel was clamped to Chudware's configured "
                  "element-size band");

    // The current Chudware solve contract is fixed-stride tet4/tet10. Apply the
    // advisor's sizing, order, and adaptation controls, but report any
    // incompatible element-family recommendation rather than mislabelling mixed
    // cells as tets.
    if (decision.mesher != "graded_tet")
      append_note(
          report.note,
          "advisor mesher '" + decision.mesher +
              "' is incompatible with Chudware's tet-only solve contract; "
              "retained graded_tet and applied only h/order controls");
    if (decision.adapt_passes > 0)
      append_note(
          report.note,
          "adaptation controls apply only when the adaptive study route "
          "requests error-driven refinement");
    plan.result = std::move(report);
    return plan;
  } catch (const std::exception &e) {
    report.note =
        std::string("advisor unavailable during recommendation; configured "
                    "deterministic mesh controls retained: ") +
        e.what();
  } catch (...) {
    report.note = "advisor unavailable during recommendation; configured "
                  "deterministic mesh "
                  "controls retained: unknown inference failure";
  }
  plan.result = std::move(report);
  return plan;
}

#else // !CHUDWARE_WITH_POLYMESH_ADVISOR

// Advisor-less build. The mesher, the shared element-size band and the adaptive
// loop are all present and fully deterministic here — the ONLY missing
// capability is learned advice. So an opted-in study gets a PRESENT report
// carrying available=false and the reason: silence would mean "you never
// asked", and refusing to compile would take the whole mesher down over an
// optional recommendation. plan.options and plan.h keep the configured values,
// so this study meshes exactly as it would have without opting in.
AdvisorPlan make_advisor_plan(const LoadedModel &loaded,
                              const MeshOptions &configured) {
  AdvisorPlan plan{configured, loaded.band.base, std::nullopt};
  if (!configured.learned_advisor)
    return plan;

  MeshAdvisor report =
      configured_advisor_fallback(loaded, configured, "not-built");
  report.note = "advisor unavailable; configured deterministic mesh controls "
                "retained: this build "
                "has no learned mesh advisor. PolyMesh's advisor needs the "
                "prebuilt ONNX Runtime "
                "upstream pins for Windows x64 and non-Apple 64-bit Unix only, "
                "so this platform/"
                "architecture builds the mesher without it "
                "(CHUDWARE_WITH_POLYMESH_ADVISOR off)";
  plan.result = std::move(report);
  return plan;
}

#endif // CHUDWARE_WITH_POLYMESH_ADVISOR

// Whether a study's fineness asks for polymesh's extra sharp-edge/thickness
// feature-refinement pass by default. Coarse/draft must stay interactive, so
// the pass only starts at the product's medium setting — but polymesh REFUSES
// to fill at all when a CAD feature is smaller than h and the pass is off, so
// this is a default, not a policy: see mesh_loaded_model's recovery retry.
bool default_feature_refine(const MeshOptions &options) {
  return options.fineness >= 0.5;
}

// The thinnest wall of the part, measured in element sizes: the minimum over
// boundary elements of (local wall thickness / local element size).
//
// Why the number exists: a wall meshed ONE element thick has no interior node,
// so the element cannot bend — the assembled system is a mechanism, and the
// solver returns a "converged" displacement of nonsense magnitude rather than
// an error (live, a tube-frame spine: an O60 x 5.16 mm wall tube at h = 16 mm
// reported max displacement 9.6e6 mm at an equilibrium error of 0.0004%). A
// ratio below 1 says exactly that, before anyone reads a stress off it.
//
// The wall estimate is polymesh's inward-raycast per-vertex thickness over the
// CAD tessellation (geom::estimate_local_thickness — uniform-grid accelerated
// and OpenMP-parallel), and it runs once per mesh GENERATION because MeshOut is
// cached, so a repeated solve pays nothing. Every surface vertex lies on the
// boundary by construction, so pairing each one with the element size beside it
// IS a boundary-element ratio; the pairing goes through an h-sized spatial hash
// of element centroids instead of a nearest-node scan, so the cost is O(V + E).
// The neighbourhood MINIMUM element size is used, which biases the ratio UP: a
// graded mesh that already refined the wall is never accused of spanning it.
//
// Sampling limit, stated because the number is release evidence: the estimate
// lives at tessellation VERTICES. A planar slab tessellated as two triangles per
// face is therefore measured only at its corners, where the vertex normal runs
// diagonally and the ray overshoots the wall (a 20 mm cube at h = 5 mm reads
// ~12.7, not 4). Curved geometry — the thin-wall regime this metric exists for —
// is densely tessellated, so its walls are sampled where they are actually thin
// (an O60 x 5.16 mm tube whose wall carries 4 elements reads 4.1).
//
// Returns 0 when no usable pair exists (no tessellation, or an open shell whose
// inward rays never reached an opposite wall).
double thin_wall_ratio(const pm::pipeline::Model &model, const MeshOut &out,
                       double h, double &min_wall_mm) {
  const int stride = out.nodes_per_elem == 10 ? 10 : 4;
  if (model.surface.vertices.empty() || out.points.empty() ||
      out.tets.empty() ||
      out.tets.size() % static_cast<size_t>(stride) != 0 || !(h > 0.0))
    return 0.0;
  std::vector<double> thickness;
  try {
    thickness = pm::geom::estimate_local_thickness(model.surface).thickness;
  } catch (const std::exception &) {
    // A surface the indicator rejects (non-manifold fan, degenerate triangle)
    // yields NO estimate. Reporting one anyway would be the lie this metric
    // exists to catch.
    return 0.0;
  }
  if (thickness.size() != model.surface.vertices.size())
    return 0.0;

  auto cell_of = [h](double x, double y, double z) {
    return std::array<long long, 3>{
        static_cast<long long>(std::floor(x / h)),
        static_cast<long long>(std::floor(y / h)),
        static_cast<long long>(std::floor(z / h))};
  };
  auto key_of = [](const std::array<long long, 3> &cell) {
    return (cell[0] * 73856093LL) ^ (cell[1] * 19349663LL) ^
           (cell[2] * 83492791LL);
  };
  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  std::unordered_map<long long, double> smallest_near;
  smallest_near.reserve(element_count);
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    double centroid[3] = {0.0, 0.0, 0.0};
    double size = 0.0;
    for (int a = 0; a < 4; ++a) {
      const double *pa = &out.points[static_cast<size_t>(element[a]) * 3];
      for (int c = 0; c < 3; ++c)
        centroid[c] += pa[c] / 4.0;
      for (int b = a + 1; b < 4; ++b) {
        const double *pb = &out.points[static_cast<size_t>(element[b]) * 3];
        const double dx = pa[0] - pb[0];
        const double dy = pa[1] - pb[1];
        const double dz = pa[2] - pb[2];
        size = std::max(size, std::sqrt(dx * dx + dy * dy + dz * dz));
      }
    }
    if (!std::isfinite(size) || !(size > 0.0))
      continue;
    const long long key = key_of(cell_of(centroid[0], centroid[1], centroid[2]));
    const auto at = smallest_near.find(key);
    if (at == smallest_near.end())
      smallest_near.emplace(key, size);
    else
      at->second = std::min(at->second, size);
  }

  double ratio = std::numeric_limits<double>::infinity();
  for (size_t v = 0; v < thickness.size(); ++v) {
    if (!pm::geom::has_finite_thickness(thickness[v]))
      continue;
    if (min_wall_mm <= 0.0 || thickness[v] < min_wall_mm)
      min_wall_mm = thickness[v];
    const Eigen::Vector3d &p = model.surface.vertices[v];
    const std::array<long long, 3> home = cell_of(p.x(), p.y(), p.z());
    double local = std::numeric_limits<double>::infinity();
    for (long long dx = -1; dx <= 1; ++dx)
      for (long long dy = -1; dy <= 1; ++dy)
        for (long long dz = -1; dz <= 1; ++dz) {
          const auto at = smallest_near.find(
              key_of({home[0] + dx, home[1] + dy, home[2] + dz}));
          if (at != smallest_near.end())
            local = std::min(local, at->second);
        }
    if (std::isfinite(local) && local > 0.0)
      ratio = std::min(ratio, thickness[v] / local);
  }
  return std::isfinite(ratio) ? ratio : 0.0;
}

// Polyhedral-FEA labels every length in its diagnostics " m", because ITS
// contract is metres. Chudware never scales on this path: load_model hands
// polymesh the STEP in the document's own units (millimetres), and the element
// size resolve_size_band computes is millimetres too — there is no `--scale`
// anywhere in the in-process mesher (only the out-of-process polymesh_cli
// release path passes `--scale 0.001` for a mm STEP). So a refusal on a ~100 mm
// board printed "feature unresolved at h=4.45857 m": the NUMBER is a
// millimetre element size, the LABEL is wrong. Relabel where the upstream text
// is captured, so every error and note the product surfaces reads in the units
// the study is actually in.
std::string in_model_units(std::string diagnostic) {
  for (size_t at = diagnostic.find(" m"); at != std::string::npos;
       at = diagnostic.find(" m", at + 2)) {
    // Only unit tokens: a space-m that follows a number and is not already "mm"
    // (and never a word like " mesh" or " max").
    if (at == 0 ||
        !std::isdigit(static_cast<unsigned char>(diagnostic[at - 1])))
      continue;
    const char next = at + 2 < diagnostic.size() ? diagnostic[at + 2] : '\0';
    if (next == 'm' || (std::isalpha(static_cast<unsigned char>(next)) != 0))
      continue;
    diagnostic.insert(at + 2, 1, 'm');
  }
  return diagnostic;
}

// ── Local element sizing: seeds + graded field ───────────────────────────────
// polymesh takes two independent levers for "make it finer HERE": `refine_seeds`
// + `seed_band` stamp fine CELLS around world points (mesh/cell_stamp.cpp), and
// `size_field` biases the graded fill's child placement. Neither alone is
// enough — the seed stamp is what guarantees the fine cells exist, and the field
// is what grades them back out to the background size without a sliver ring — so
// a local request drives both, exactly as the adaptive driver does for its
// marked elements.
//
// The lattice stops at h/4. Smaller local requests are delivered by LEB after
// this fill, without repeating the band at alternate background sizes.
pm::mesh::SizeFieldFn local_size_field(const MeshOptions &options, double h,
                                       const pm::mesh::SizeFieldFn &outer,
                                       std::vector<Eigen::Vector3d> &seeds,
                                       double &seed_band) {
  seeds.clear();
  seed_band = 0.0;
  if (!options.local_refine.active())
    return outer;
  const MeshLocalRefine &request = options.local_refine;
  const std::size_t count = request.points.size() / 3;
  seeds.reserve(count);
  for (std::size_t i = 0; i < count; ++i)
    seeds.emplace_back(request.points[i * 3], request.points[i * 3 + 1],
                       request.points[i * 3 + 2]);
  // The band has to be at least the background cell it is carved out of, or the
  // stamp lands inside one cell and changes nothing.
  const double band = request.band_mm > 0.0 ? request.band_mm : 2.0 * request.size_mm;
  seed_band = std::max(band, h);
  if (request.max_depth_mm > 0.0)
    seed_band = std::min(seed_band, request.max_depth_mm);
  // Keep the requested source size: raising it to the background h/4 also
  // raises the entire grading band and starves the interface of fine parents.
  // The fill clamps its own discrete levels; later LEB delivers finer sizes.
  // A GradedSizing is `clamp(min_i(h_i + beta*d), h_min, h_max)` and
  // its constructor requires 0 < h_min <= h_max. A coarser local request is
  // already met by the background, so clamp only the upper bound.
  const double h_local = std::min(request.size_mm, h);
  const std::vector<pm::adapt::SizeSource> sources =
      pm::adapt::point_size_sources(seeds, h_local);
  // beta is the gradient limit: the requested size may grow beta mm per mm away
  // from a seed. The adaptive driver uses 1.0, and that is TOO STEEP for a
  // surface request, because the fill evaluates the field at CELL CENTRES: the
  // centre of a fine cell touching the interface sits half a cell plus the
  // clearance away from the seed, where a beta of 1.0 has already pushed the
  // requested size back above the lattice's h/2 threshold — so it places an L1
  // cell where an L2 was asked for. Measured on the seated-pad assembly probe
  // (seat 8 x 8 mm, background 2.0 mm, requested 0.5 mm): beta 1.0 delivered an
  // interface element size of 2.07 mm, beta 0.5 delivers the L2 cells.
  const double fill_beta = request.max_depth_mm > 0.0
      ? std::max(0.5, (h - h_local) / request.max_depth_mm) : 0.5;
  pm::mesh::SizeFieldFn local =
      pm::adapt::size_field_from_sources(sources, h_local, h, fill_beta);
  if (!outer)
    return local;
  // Two requests, one field: whichever asks for the smaller element wins.
  return [local, outer](const Eigen::Vector3d &x) {
    return std::min(local(x), outer(x));
  };
}

// ── Delivering a local size the LATTICE cannot place: Rivara LEB ─────────────
// The seed stamp + size field above bottom out at h/4, and h itself has a hard
// floor that has nothing to do with the study: polymesh sizes its Cartesian
// classification grid at `2 * 1.05 * cbrt(bbox_volume / 512k cells)`
// (mesh::min_h_for_cell_budget, kDefaultMaxGridCells), and raises h to it. On
// the live FRAME-PEDAL-BRAKE keyway isolate (230 x 60 x 60 mm bbox) that floor
// is 2.75 mm, so a 0.35 mm interface request came back as 1.299 mm — the fill
// had lowered the background to 1.4 mm exactly as asked, and the grid budget
// put it back to 2.747 mm with a 0.687 mm finest child. NO background size can
// buy that interface: the request is below the floor's own L2.
//
// So the contract is delivered by conforming h-refinement of the elements that
// are ALREADY there: longest-edge bisection with LEPP closure
// (pm::mesh::local_refine_tets — tet4 only, no hanging nodes, no MPCs), driven
// by a gradient-limited size field over the seeds that is FLAT within one seed
// spacing of them and grades to the background outside. Each wave bisects every
// element coarser than that field asks for anywhere it reaches, so the refined
// region is a shell on the interface instead of a finer part: the alternative,
// lowering the background to 4x the request, predicted 3.4M elements for the
// same interface.
//
// Bounded three ways: at most kLebWaves waves, never past the size band's
// element ceiling (the caller refuses with the count when that binds), and a
// wave whose closure fails or grows nothing ends the loop with what it has. The
// achieved size is MEASURED and reported, never assumed.
struct LocalContract {
  double requested_mm = 0.0;
  double achieved_mm = 0.0;  // coarsest boundary face left inside the core
  double core_mm = 0.0;      // width of the flat request zone (seed spacing)
  int waves = 0;
  int elems_added = 0;
  double transition_depth_mm = 0.0;
  uint64_t transition_marks = 0;
  uint64_t outside_transition_marks = 0;
  uint64_t outside_transition_checks = 0;
  std::size_t elems = 0;         // element count after refinement
  std::size_t element_ceiling = 0;
  bool ceiling_bound = false;    // stopped because the next wave breaks the ceiling
  // Where the new mid-edge nodes ended up, and what the mesh cost for it.
  std::size_t surface_mids = 0;  // projected onto the CAD surface
  std::size_t chord_mids = 0;    // kept on the chord (curvature gate / fold)
  int reprojected_nodes = 0;     // slid back onto the face by the smoothing pass
  int smoothed_nodes = 0;        // moved by the quality (tangential/umbrella) pass
  double worst_quality = 0.0;    // worst tet shape quality after the last wave
  double max_residual_mm = 0.0;  // interface boundary node distance to the CAD
  double mean_residual_mm = 0.0; // surface, sampled (fidelity, not cosmetics)
  std::size_t below_shape_floor = 0;  // cells under polymesh's own 0.02 floor
  // ── The bounded quality pass over the refined region ─────────────────────
  // What the waves left and what the passes made of it, both measured over the
  // same region (see refined_region): the cells that own a wave-created node,
  // plus one layer of their neighbours. `*_before` is taken straight out of
  // the LAST bisection, before that wave's own pass; `*_after` after the final
  // bounded loop.
  double jacobian_min_before = 0.0;
  double jacobian_min_after = 0.0;
  double aspect_max_before = 0.0;
  double aspect_max_after = 0.0;
  std::size_t cells_below_floor_before = 0;
  std::size_t cells_below_floor_after = 0;
  int quality_passes = 0;      // per-wave passes plus the final convergent loop
  int flips_23 = 0;            // interior faces re-tiled around a new edge
  int edge_removals = 0;       // interior edges removed (3-2 and its 4-4/5-6 kin)
  int collapsed_slivers = 0;
  bool resplit = false;        // the pass coarsened the delivery and it was re-split
  std::string note;
};

// Waves are bounded by the element ceiling long before this in any real study;
// the count only has to be high enough to cover the halvings a request needs
// (log2(background / request) plus the grading), which on the live keyway
// isolate is 0.35 mm out of a 3.45 mm background, i.e. 3.3 halvings of a mesh
// whose interface starts at ~3 mm.
constexpr int kLebWaves = 18;

// A wave is DISCARDED when it leaves a cell this bad — the pipeline's own
// degeneracy bound, not an aesthetic one. Downstream, mesh_quality refuses a
// cell whose |6V| is under 64 eps max(1, span^3) and the quadratic promotion
// needs a positive Jacobian; a shape quality of 1e-5 is ~9 orders above the
// first and strictly positive for the second, and Tet4Conn's
// repair_degenerate_tets picks up anything that still lands under 1e-6.
//
// It is deliberately NOT pm::mesh::validity::kCellShapeFloor (0.02). That floor
// is a SHIP-QUALITY target, and judging a whole wave by it is what stalled the
// live keyway at 2.434 mm: bisection legitimately produces cells under 0.02 on
// an anisotropic wedge, and the fill itself ships some (33 left-handed and 2
// flat cells on the rover wheel sector), so a single such cell — pre-existing or
// new — vetoed every remaining wave and the study was refused on a mesh the
// solver would have taken. Cells between this floor and 0.02 are COUNTED and
// reported instead of being fatal.
constexpr double kWaveDegenerateFloor = 1e-5;

// Curvature gate on each new mid-edge node's projection onto the CAD surface,
// as a fraction of the child edge being created (pm::mesh::local_refine_tets'
// `max_sag_fraction`). Chudware's contract path refines an ALREADY-fine mesh
// toward a sub-millimetre target and owns a smoothing pass over the declined
// nodes, so it wants the gate; polymesh's own Cartesian fills pass 0 and keep
// unconditional projection, because their parent edges are a whole lattice cell
// and their snap passes own the residual (turning the gate on there cost
// test_curved_mesh_quality's cylinder_prism 0.0350 mm of boundary residual
// against a 0.0128 mm bound).
constexpr double kMaxProjectionSagFraction = 0.2;

// The longest edge of a tet4, mm — the element size LEB actually halves.
double longest_edge_mm(const pm::fea::NodalMesh &mesh,
                       const pm::fea::NodalElement &element) {
  double worst = 0.0;
  for (int a = 0; a < 4; ++a)
    for (int b = a + 1; b < 4; ++b) {
      const double len = (mesh.nodes[element.nodes[static_cast<std::size_t>(a)]] -
                          mesh.nodes[element.nodes[static_cast<std::size_t>(b)]])
                             .norm();
      worst = std::max(worst, len);
    }
  return worst;
}

// How densely the seed cloud samples the interface: the 90th percentile of the
// nearest-neighbour distance over a bounded sample of seeds. It is the scale
// below which the cloud says nothing about the surface it came from, so it is
// the width of the flat core in deliver_local_contract, and the tolerance the
// achieved size is measured with. Sampled (not exhaustive) because the seed set
// runs to tens of thousands of points and an exact all-pairs pass would cost
// more than the refinement it is sizing.
double seed_spacing_mm(const std::vector<Eigen::Vector3d> &seeds) {
  if (seeds.size() < 2) return 0.0;
  constexpr std::size_t kSamples = 128;
  const std::size_t stride = std::max<std::size_t>(1, seeds.size() / kSamples);
  std::vector<double> nearest;
  nearest.reserve(seeds.size() / stride + 1);
  for (std::size_t i = 0; i < seeds.size(); i += stride) {
    double best = std::numeric_limits<double>::infinity();
    for (std::size_t j = 0; j < seeds.size(); ++j) {
      if (j == i) continue;
      best = std::min(best, (seeds[i] - seeds[j]).squaredNorm());
    }
    if (std::isfinite(best)) nearest.push_back(std::sqrt(best));
  }
  if (nearest.empty()) return 0.0;
  const std::size_t at = (nearest.size() * 9) / 10;
  std::nth_element(nearest.begin(),
                   nearest.begin() + static_cast<std::ptrdiff_t>(
                                         std::min(at, nearest.size() - 1)),
                   nearest.end());
  return nearest[std::min(at, nearest.size() - 1)];
}

// ── Recovering what a bisection wave leaves behind: the quality pass ─────────
// A bisection wave that declines a surface projection (local_refine.cpp's sag
// gate) leaves the new mid-edge node on the chord — INSIDE the void of a bore,
// so the wall bulges by that sag until something puts the node back. And a wave
// that keeps a projection can still leave the star around it skewed: LEPP
// closure has to bisect a whole chain of neighbours to stay conforming, and it
// pays for that with stretched cells one size class out from the interface. The
// rover hinge saddle came back with a delivered interface exactly as asked and
// an `aspect_max` of 263, a `jacobian_min` of 0.0029 and 50 cells under the
// ship floor away from it. Both are per-CELL and per-NODE facts, so they get
// per-node and per-cell decisions here rather than the per-wave,
// all-or-nothing verdict that stalled the live keyway.
//
// Three node classes, three different moves — this is a boundary-aware
// smoother, not a Laplacian:
//
//   * a FEATURE node — a CAD edge or corner, detected as incident surface
//     normals that disagree by more than kFeatureAngleCos — does not move at
//     all. Sliding one tangentially and re-projecting is how a sharp edge gets
//     rounded, and on the hinge saddle that edge IS the load path;
//   * a SURFACE node slides along its own face: project onto the CAD surface
//     for fidelity, then relax toward the umbrella centroid of its BOUNDARY
//     neighbours and re-project, so every step of the travel stays on the
//     face;
//   * an INTERIOR node carries no geometry constraint, so it is placed by
//     OPTIMISATION: a bounded pattern search that maximises the MINIMUM shape
//     quality of its own star. Max-min, not centroid — the umbrella centroid
//     of a LEPP closure fan is roughly where the sliver already is, which is
//     why a Laplacian pass leaves `aspect_max` in the hundreds.
//
// FIDELITY is its own move and comes first (boundary nodes only). Travel is
// capped at kSnapMoveFrac of the node's own shortest incident edge — a bound in
// the mesh's local scale, not the background h, because by wave 8 the two
// differ by an order of magnitude. It is accepted when every incident cell
// keeps a positive volume AND the star ends either healthy in absolute terms
// (>= the ship floor) or no worse than it started: a geometry move is NOT
// required to improve quality, only to not trade a healthy star for a sick
// one. Every QUALITY move is accepted only on a strictly improving star
// minimum.
//
// Bounded: kSmoothPasses sweeps over an explicit movable set — the nodes ONE
// wave created, or the nodes of the cells a pass is working on — never the
// whole mesh, and every boundary candidate costs one closest-point query.
constexpr int kSmoothPasses = 2;
constexpr double kSnapMoveFrac = 1.0;
constexpr double kSmoothRelax = 0.6;
// Pattern-search budget for one interior node: halve the step this many times.
// Three is enough to place a node inside a star whose own edges differ by 8x,
// and the search costs one star evaluation per direction per step.
constexpr int kOptimiseSteps = 3;

// The angle between two incident surface normals above which the node between
// them sits on a CAD FEATURE and is pinned. polymesh's own fills call 30 deg a
// sharp edge (pipeline::resolve_mesh_size's sharp_angle_deg), and a tessellated
// bore of radius 6 mm turns its normal by 12 deg per facet at a 1.3 mm element
// size, so 40 deg separates a real edge from mere curvature at every element
// size a contact study reaches.
constexpr double kFeatureAngleCos = 0.766044443118978;  // cos(40 deg)

// Twice-area over longest-edge-squared below which a boundary facet's normal
// direction is rounding noise rather than a statement about the surface, so it
// is left out of the feature fan (an equilateral facet scores 0.866; 0.02 is a
// 40:1 needle).
constexpr double kNormalConditionFloor = 0.02;

// How far the pass may move the BOUNDARY away from the CAD surface, as a
// fraction of the local surface element size. It bounds a feature-edge node's
// slide along its own edge (smooth_nodes) and a surface-node sliver collapse
// (collapse_slivers) alike, because both trade tessellation fidelity for
// shape and both must stay inside the placement error the fill already ships:
// the fill's own boundary snap carries about 9 % of h (its note quotes
// 1.21 mm at h = 13.94 mm).
constexpr double kBoundaryDriftFrac = 0.1;

// ...and the much tighter share of it a feature-edge SLIDE may spend. The
// slide's whole justification is that travelling along the edge's tangent
// keeps the node ON the edge, which is exact for a straight edge and false by
// the chord sag for a curved one. At the face bound (0.1) the saddle's two
// straight radial edges gained everything asked of them — jacobian_min
// 0.0198 -> 0.0621, aspect_max 75.3 -> 17.2 — while the pin-in-bore's CURVED
// rim lost as much, 0.0881 -> 0.0422, because a 0.03 mm sag budget on a
// 6.35 mm radius buys a 0.6-element slide right across the rim. A thousandth
// leaves a straight edge free (its deviation is zero identically) and holds a
// curved one to sqrt(2*R*delta), about a fifth of an element.
constexpr double kEdgeSlideDriftFrac = 1e-3;

// ── What the pass optimises, and why ONE number covers both reported floors ──
// Every candidate move, flip and collapse is judged by the same quantity: the
// MINIMUM shape quality (6*sqrt(2)*V / L_max^3, polymesh's own scale-free tet
// metric) over the cells it changes. The two floors the report is stated in
// follow from it exactly:
//
//   * scaled_jacobian >= shape_quality. The scaled Jacobian's worst corner
//     divides 6*sqrt(2)*V by that corner's edge PRODUCT, and no corner's
//     product exceeds L_max^3.
//   * aspect <= 1 / shape_quality. Aspect is L_max*A_max / (3*1.2247*V), and
//     the largest face of a tet whose longest edge is L_max has area at most
//     sqrt(3)/4*L_max^2; 2.3094 * 0.4330 = 1.
//
// So a region driven to shape quality kQualityTarget carries
// jacobian_min >= 0.05 and aspect_max <= 20, and nothing sits under polymesh's
// 0.02 ship floor. That is why the moves optimise one metric while the stop
// test reads the reported ones.
constexpr double kQualityTarget = 0.05;
constexpr double kQualityJacobianFloor = 0.05;
constexpr double kQualityAspectCeiling = 40.0;
constexpr int kQualityPasses = 5;

// Element star, neighbour ring and boundary/feature classification of ONE
// bounded node set. Rebuilt whenever connectivity changes (a flip or a
// collapse), which is why it is a value and not a cache.
struct StarTopology {
  std::vector<std::uint32_t> movable;                    // the nodes in play
  std::vector<std::int32_t> slot;                        // node -> index, -1 outside
  std::vector<std::vector<std::uint32_t>> star;          // incident cells
  std::vector<std::vector<std::uint32_t>> ring;          // neighbour nodes
  std::vector<std::vector<std::uint32_t>> boundary_ring; // boundary neighbours
  std::vector<char> on_boundary;
  // A boundary node's CAD constraint, from the fan of its incident surface
  // normals: `fixed` marks a feature (edge or corner), and `corner` picks out
  // the ones with three or more distinct surface directions. A feature EDGE
  // node keeps ONE degree of freedom, along `edge_dir` — the intersection of
  // its two surface planes, which is the edge's own tangent — so it can slide
  // without taking the edge with it. A corner has none.
  std::vector<char> fixed;
  std::vector<char> corner;
  std::vector<Eigen::Vector3d> edge_dir;

  std::size_t size() const { return movable.size(); }
  std::int32_t at(std::uint32_t node) const {
    return node < slot.size() ? slot[node] : -1;
  }
};

// The face-of-a-tet corner triples, and the corner opposite each face.
constexpr int kTetFaces[4][3] = {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
constexpr int kTetFaceApex[4] = {3, 2, 1, 0};

// Bijective key for a sorted node triple — base 1000033, not a hash, so two
// different faces can never collide into one census entry.
std::uint64_t face_key(const std::array<std::uint32_t, 3> &tri) {
  return (static_cast<std::uint64_t>(tri[0]) * 1000003ull + tri[1]) * 1000033ull +
         tri[2];
}

bool is_tet4(const pm::fea::NodalElement &element) {
  return element.type == pm::fea::ElementType::kTet4 && element.nodes.size() == 4;
}

StarTopology build_star_topology(const pm::fea::NodalMesh &nodal,
                                 std::vector<std::uint32_t> movable) {
  StarTopology topo;
  std::sort(movable.begin(), movable.end());
  movable.erase(std::unique(movable.begin(), movable.end()), movable.end());
  movable.erase(std::remove_if(movable.begin(), movable.end(),
                               [&](std::uint32_t n) {
                                 return n >= nodal.nodes.size();
                               }),
                movable.end());
  topo.movable = std::move(movable);
  const std::size_t count = topo.movable.size();
  topo.star.assign(count, {});
  topo.ring.assign(count, {});
  topo.boundary_ring.assign(count, {});
  topo.on_boundary.assign(count, 0);
  topo.fixed.assign(count, 0);
  topo.corner.assign(count, 0);
  topo.edge_dir.assign(count, Eigen::Vector3d::Zero());
  if (count == 0 || nodal.elements.empty())
    return topo;
  topo.slot.assign(nodal.nodes.size(), -1);
  for (std::size_t i = 0; i < count; ++i)
    topo.slot[topo.movable[i]] = static_cast<std::int32_t>(i);

  // The star is MESH-WIDE: a movable node can be shared with a cell outside
  // the working set, and moving it changes that cell too, so it has to be in
  // the accept test. `touched` is every cell any movable node reaches — and
  // therefore both owners of every face carrying a movable node, which is what
  // makes the restricted boundary census below exact.
  std::vector<std::uint32_t> touched;
  touched.reserve(count * 8);
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    const pm::fea::NodalElement &element = nodal.elements[e];
    if (!is_tet4(element)) continue;
    bool any = false;
    for (int a = 0; a < 4; ++a) {
      const std::int32_t at = topo.slot[element.nodes[static_cast<std::size_t>(a)]];
      if (at < 0) continue;
      any = true;
      topo.star[static_cast<std::size_t>(at)].push_back(
          static_cast<std::uint32_t>(e));
      // Duplicates are deliberate: the umbrella centroid is then weighted by
      // how many cells each neighbour is shared through, which is the discrete
      // Laplacian the accept test was tuned against.
      for (int b = 0; b < 4; ++b)
        if (b != a)
          topo.ring[static_cast<std::size_t>(at)].push_back(
              element.nodes[static_cast<std::size_t>(b)]);
    }
    if (any) touched.push_back(static_cast<std::uint32_t>(e));
  }

  // Boundary = a triangular face used by exactly one cell. Each entry keeps
  // the owner's opposite corner so the face normal can be oriented OUTWARD,
  // which is what makes the feature test an angle between real surface
  // normals rather than between two arbitrary windings.
  struct FaceUse {
    std::array<std::uint32_t, 3> tri{};
    std::uint32_t apex = 0;
    int uses = 0;
  };
  std::unordered_map<std::uint64_t, FaceUse> faces;
  faces.reserve(touched.size() * 2);
  for (const std::uint32_t e : touched) {
    const pm::fea::NodalElement &element = nodal.elements[e];
    for (int f = 0; f < 4; ++f) {
      std::array<std::uint32_t, 3> tri{
          element.nodes[static_cast<std::size_t>(kTetFaces[f][0])],
          element.nodes[static_cast<std::size_t>(kTetFaces[f][1])],
          element.nodes[static_cast<std::size_t>(kTetFaces[f][2])]};
      const std::uint32_t apex =
          element.nodes[static_cast<std::size_t>(kTetFaceApex[f])];
      std::sort(tri.begin(), tri.end());
      const auto it = faces.find(face_key(tri));
      if (it == faces.end())
        faces.emplace(face_key(tri), FaceUse{tri, apex, 1});
      else
        ++it->second.uses;
    }
  }
  std::vector<std::vector<Eigen::Vector3d>> normals(count);
  // The normal fan is consumed greedily and boundary-ring sums are ordered
  // floating-point reductions. Neither may inherit a hash table's iteration
  // order (libc++ and libstdc++ enumerate this census differently).
  std::vector<const FaceUse *> boundary_faces;
  for (const auto &entry : faces)
    if (entry.second.uses == 1) boundary_faces.push_back(&entry.second);
  std::sort(boundary_faces.begin(), boundary_faces.end(),
            [](const FaceUse *a, const FaceUse *b) { return a->tri < b->tri; });
  for (const FaceUse *face : boundary_faces) {
    const std::array<std::uint32_t, 3> &tri = face->tri;
    const Eigen::Vector3d &p0 = nodal.nodes[tri[0]];
    const Eigen::Vector3d &p1 = nodal.nodes[tri[1]];
    const Eigen::Vector3d &p2 = nodal.nodes[tri[2]];
    Eigen::Vector3d normal = (p1 - p0).cross(p2 - p0);
    const double length = normal.norm();
    // A facet's normal DIRECTION is only meaningful when the facet is not
    // nearly collinear. Twice the area over the longest edge squared is the
    // facet's own conditioning (0.866 for an equilateral triangle), and a
    // refined region is full of thin boundary facets whose normals are pure
    // rounding noise. Feeding those into the fan below mislabels their nodes
    // as CAD features and then PINS them: on the hinge saddle it produced
    // cells with all four nodes "on a feature" at a 0.35 mm element size,
    // which no smoothing, flip or collapse could touch.
    const double longest = std::sqrt(std::max(
        {(p1 - p0).squaredNorm(), (p2 - p0).squaredNorm(), (p2 - p1).squaredNorm()}));
    const bool reliable = length > 0.0 && longest > 0.0 &&
                          length / (longest * longest) >= kNormalConditionFloor;
    if (length > 0.0) {
      normal /= length;
      // Away from the owner's fourth corner: that is outward, exactly.
      if (normal.dot((p0 + p1 + p2) / 3.0 - nodal.nodes[face->apex]) < 0.0)
        normal = -normal;
    }
    for (int a = 0; a < 3; ++a) {
      const std::int32_t at = topo.slot[tri[static_cast<std::size_t>(a)]];
      if (at < 0) continue;
      const std::size_t slot = static_cast<std::size_t>(at);
      topo.on_boundary[slot] = 1;
      if (reliable) normals[slot].push_back(normal);
      for (int b = 0; b < 3; ++b)
        if (b != a)
          topo.boundary_ring[slot].push_back(tri[static_cast<std::size_t>(b)]);
    }
  }
  // Cluster each node's incident surface normals into distinct SURFACE
  // DIRECTIONS: one cluster is a smooth patch, two is a feature edge, three or
  // more is a corner. The clustering is greedy against the same feature angle
  // the pairwise test used, which is enough because the alternatives on a CAD
  // solid are far apart by construction.
  for (std::size_t at = 0; at < count; ++at) {
    const std::vector<Eigen::Vector3d> &fan = normals[at];
    std::array<Eigen::Vector3d, 4> mean{};
    std::array<int, 4> members{};
    std::size_t clusters = 0;
    for (const Eigen::Vector3d &normal : fan) {
      std::size_t joined = clusters;
      for (std::size_t c = 0; c < clusters; ++c)
        if (normal.dot(mean[c].normalized()) >= kFeatureAngleCos) {
          joined = c;
          break;
        }
      if (joined == clusters) {
        if (clusters == mean.size()) {
          clusters = mean.size() + 1;  // more directions than a corner needs
          break;
        }
        mean[clusters] = normal;
        members[clusters] = 1;
        ++clusters;
      } else {
        mean[joined] += normal;
        ++members[joined];
      }
    }
    if (clusters < 2) continue;  // a smooth patch: the surface node's own case
    topo.fixed[at] = 1;
    if (clusters > 2) {
      topo.corner[at] = 1;
      continue;
    }
    // Two surface planes meet in a line, and that line is the CAD edge's
    // tangent — the one direction this node may travel in.
    const Eigen::Vector3d tangent =
        mean[0].normalized().cross(mean[1].normalized());
    const double length = tangent.norm();
    if (length > 1e-6)
      topo.edge_dir[at] = tangent / length;
    else
      topo.corner[at] = 1;  // the two planes are parallel: nothing to slide on
  }
  return topo;
}

void smooth_nodes(const pm::geom::TriSurface *surface, pm::fea::NodalMesh &nodal,
                  const StarTopology &topo, int &reprojected, int &improved) {
  const std::size_t count = topo.size();
  if (count == 0 || nodal.elements.empty())
    return;
  const std::vector<std::uint32_t> &movable = topo.movable;
  const auto quality_of = [&](std::uint32_t e) {
    const pm::fea::NodalElement &cell = nodal.elements[e];
    return pm::mesh::validity::tet_shape_quality(
        nodal.nodes[cell.nodes[0]], nodal.nodes[cell.nodes[1]],
        nodal.nodes[cell.nodes[2]], nodal.nodes[cell.nodes[3]]);
  };
  const auto star_worst = [&](std::size_t at, bool &all_positive) {
    double worst = std::numeric_limits<double>::infinity();
    all_positive = true;
    for (const std::uint32_t e : topo.star[at]) {
      const double quality = quality_of(e);
      if (!std::isfinite(quality) || !(quality > 0.0)) all_positive = false;
      worst = std::min(worst, quality);
    }
    return worst;
  };
  // Shortest incident edge: the scale this node's own neighbourhood works at.
  const auto local_edge = [&](std::size_t at) {
    double shortest = std::numeric_limits<double>::infinity();
    for (const std::uint32_t other : topo.ring[at])
      shortest =
          std::min(shortest, (nodal.nodes[movable[at]] - nodal.nodes[other]).norm());
    return shortest;
  };
  const auto try_move = [&](std::size_t at, const Eigen::Vector3d &target,
                            bool require_improvement) {
    bool before_positive = true;
    const double before = star_worst(at, before_positive);
    const Eigen::Vector3d original = nodal.nodes[movable[at]];
    nodal.nodes[movable[at]] = target;
    bool after_positive = true;
    const double after = star_worst(at, after_positive);
    // A quality move must strictly improve the star. A FIDELITY move need not
    // — its job is the boundary residual — but it may not manufacture a cell
    // the pass would then have to repair and might not be able to: the bar is
    // the pass's own target, not polymesh's 0.02 ship floor. Measured on the
    // pin-in-bore probe, the 0.02 bar is exactly how a region the waves left
    // at jacobian_min 0.206 came out at 0.032 — the projections were spending
    // healthy stars down to just above the ship floor, and the boundary
    // slivers they made had no interior node left for a collapse and no
    // interior face for a flip.
    const bool keep =
        after_positive &&
        (require_improvement
             ? after > before
             : (after >= kQualityTarget ||
                (before_positive ? after >= before : true)));
    if (!keep) nodal.nodes[movable[at]] = original;
    return keep;
  };
  const bool have_surface = surface != nullptr && !surface->triangles.empty();
  for (int pass = 0; pass < kSmoothPasses; ++pass) {
    report_fill_phase("projection", pass + 1, kSmoothPasses);
    for (std::size_t at = 0; at < count; ++at) {
      report_fill_work(nodal.elements.size(), at, count);
      if (topo.star[at].empty()) continue;  // orphan midpoint; compacted later
      if (topo.corner[at] != 0) continue;  // a CAD corner has nowhere to go
      const double scale = local_edge(at);
      if (!(scale > 0.0) || !std::isfinite(scale)) continue;
      // 0. A FEATURE EDGE node keeps one degree of freedom: along the edge's
      // own tangent. Sliding there does not take the edge with it — on a
      // straight edge the travel is exactly along the CAD edge, and on a
      // curved one it leaves by the chord sag. Pinning these nodes outright is
      // what left the hinge saddle's arc-end corner at jacobian_min 0.0373:
      // its bad cells have three or four nodes on the two straight radial
      // edges, so every other operation in the pass was reaching for a node it
      // was not allowed to touch.
      //
      // The fidelity rule is MONOTONE, not absolute: the slide may leave the
      // node no further from the CAD surface than it already is, plus a thin
      // budget. Both halves are load-bearing and both were measured. Spending
      // the full face budget (kBoundaryDriftFrac) bought the saddle everything
      // asked of it — jacobian_min 0.0198 -> 0.0621, aspect_max 75.3 -> 17.2 —
      // and cost the pin-in-bore exactly as much, 0.0881 -> 0.0422, because a
      // 0.03 mm sag budget on the bore's 6.35 mm rim buys a slide right across
      // it. Tightening to a thousandth restored the bore and lost the saddle
      // again (0.0345), because the nodes that need to move are the ones
      // already 0.016-0.040 mm OFF their face — an absolute bar refuses them
      // before they start. Monotone plus the thin budget keeps both.
      if (topo.fixed[at] != 0) {
        const Eigen::Vector3d direction = topo.edge_dir[at];
        if (!(direction.squaredNorm() > 0.0)) continue;
        double surface_scale = 0.0;
        for (const std::uint32_t other : topo.boundary_ring[at])
          surface_scale +=
              (nodal.nodes[movable[at]] - nodal.nodes[other]).norm();
        if (!topo.boundary_ring[at].empty())
          surface_scale /= static_cast<double>(topo.boundary_ring[at].size());
        const double budget = kEdgeSlideDriftFrac * surface_scale;
        const double already =
            have_surface
                ? pm::mesh::closest_on_surface(*surface, nodal.nodes[movable[at]])
                      .distance
                : 0.0;
        const double tolerance =
            std::max(budget, std::isfinite(already) ? already : 0.0);
        double step = 0.5 * scale;
        bool moved = false;
        for (int iteration = 0; iteration < kOptimiseSteps; ++iteration) {
          const Eigen::Vector3d here = nodal.nodes[movable[at]];
          bool improved_here = false;
          for (const double sign : {1.0, -1.0}) {
            const Eigen::Vector3d target = here + direction * (sign * step);
            if (have_surface && tolerance > 0.0 &&
                pm::mesh::closest_on_surface(*surface, target).distance >
                    tolerance)
              continue;
            if (try_move(at, target, true)) {
              improved_here = true;
              break;
            }
          }
          if (improved_here)
            moved = true;
          else
            step *= 0.5;
        }
        if (moved && pass == 0) ++improved;
        continue;
      }
      // 1. Fidelity: put a boundary node back on the face it belongs to. The
      // FULL projection is tried first and the step is halved twice on
      // rejection — the residual is what the contact solve pays for, so give
      // back as much of it as the star will take rather than a fixed fraction.
      // The travel is still capped at the node's own shortest incident edge:
      // beyond that a "closest point" is describing some other part of the
      // surface, which is the fold the curvature gate exists to refuse.
      if (topo.on_boundary[at] != 0 && have_surface) {
        const Eigen::Vector3d here = nodal.nodes[movable[at]];
        const pm::mesh::ClosestPoint hit =
            pm::mesh::closest_on_surface(*surface, here);
        const Eigen::Vector3d step = hit.point - here;
        const double travel = step.norm();
        if (travel > 1e-12 * scale) {
          const double capped = std::min(travel, kSnapMoveFrac * scale);
          for (const double fraction : {1.0, 0.5, 0.25}) {
            if (try_move(at, here + step * (capped * fraction / travel), false)) {
              if (pass == 0) ++reprojected;
              break;
            }
          }
        }
      }
      // 2. Quality. A surface node relaxes toward the umbrella centroid of its
      // BOUNDARY neighbours and is re-projected, so it slides along its own
      // face; an interior node is placed by max-min pattern search, which is
      // the move a stretched LEPP-closure star needs and a centroid pull
      // cannot make.
      if (topo.on_boundary[at] != 0) {
        const std::vector<std::uint32_t> &neighbours = topo.boundary_ring[at];
        if (neighbours.empty()) continue;
        Eigen::Vector3d centroid = Eigen::Vector3d::Zero();
        for (const std::uint32_t other : neighbours) centroid += nodal.nodes[other];
        centroid /= static_cast<double>(neighbours.size());
        Eigen::Vector3d target =
            nodal.nodes[movable[at]] +
            kSmoothRelax * (centroid - nodal.nodes[movable[at]]);
        if (have_surface)
          target = pm::mesh::closest_on_surface(*surface, target).point;
        if (try_move(at, target, true) && pass == 0) ++improved;
        continue;
      }
      const std::vector<std::uint32_t> &neighbours = topo.ring[at];
      if (neighbours.empty()) continue;
      Eigen::Vector3d centroid = Eigen::Vector3d::Zero();
      for (const std::uint32_t other : neighbours) centroid += nodal.nodes[other];
      centroid /= static_cast<double>(neighbours.size());
      // Seven search directions: the umbrella pull (the direction a Laplacian
      // would take, which is right when the star is merely lopsided) and the
      // six coordinate axes (which reach the off-centroid placements a sliver
      // needs). The step starts at half the node's shortest edge and halves on
      // a pass that improves nothing, so the search is bounded by
      // kOptimiseSteps * 7 star evaluations.
      std::array<Eigen::Vector3d, 7> directions{
          Eigen::Vector3d::UnitX(),  -Eigen::Vector3d::UnitX(),
          Eigen::Vector3d::UnitY(),  -Eigen::Vector3d::UnitY(),
          Eigen::Vector3d::UnitZ(),  -Eigen::Vector3d::UnitZ(),
          Eigen::Vector3d::Zero()};
      const Eigen::Vector3d pull = centroid - nodal.nodes[movable[at]];
      const double pull_length = pull.norm();
      directions[6] = pull_length > 0.0 ? Eigen::Vector3d(pull / pull_length)
                                        : Eigen::Vector3d::UnitX();
      double step = 0.5 * scale;
      bool moved = false;
      for (int iteration = 0; iteration < kOptimiseSteps; ++iteration) {
        const Eigen::Vector3d here = nodal.nodes[movable[at]];
        bool improved_here = false;
        for (const Eigen::Vector3d &direction : directions)
          if (try_move(at, here + direction * step, true)) {
            improved_here = true;
            break;
          }
        if (improved_here)
          moved = true;
        else
          step *= 0.5;
      }
      if (moved && pass == 0) ++improved;
    }
  }
}

// The nodes of every tet4 still under polymesh's ship-quality floor. This is
// the set the LAST smoothing round works on: a sliver's own corners are what
// have to move to give it shape, and by then they are as likely to be original
// fill nodes as nodes a wave created.
std::vector<std::uint32_t> nodes_of_poor_cells(const pm::fea::NodalMesh &nodal) {
  std::vector<std::uint32_t> nodes;
  for (const pm::fea::NodalElement &element : nodal.elements) {
    if (element.type != pm::fea::ElementType::kTet4) continue;
    if (pm::mesh::validity::tet_shape_quality(
            nodal.nodes[element.nodes[0]], nodal.nodes[element.nodes[1]],
            nodal.nodes[element.nodes[2]], nodal.nodes[element.nodes[3]]) >=
        pm::mesh::validity::kCellShapeFloor)
      continue;
    nodes.insert(nodes.end(), element.nodes.begin(), element.nodes.begin() + 4);
  }
  std::sort(nodes.begin(), nodes.end());
  nodes.erase(std::unique(nodes.begin(), nodes.end()), nodes.end());
  return nodes;
}

// ── Connectivity edits: the two moves smoothing cannot make ──────────────────
// A node move cannot fix every star. Two of them are TOPOLOGICAL:
//
//   * a flat cell whose four nodes are all where they should be is wrong in
//     its CONNECTIVITY, not its geometry — the fix is to re-tile the same
//     region with a different diagonal (2-3 / 3-2 flips);
//   * a sliver so thin that no single node move can inflate it has to be
//     REMOVED, by collapsing its short edge.
//
// Both are bounded by the same three rules, which is what makes them safe to
// run inside a size contract:
//   1. the union of the cells they touch must be RETILED, not reshaped: the
//      absolute volumes of the replacements must sum to the volumes of the
//      originals (kRetileVolumeSlack). This is the exact validity test — a
//      2-3 flip whose new edge does not pierce the shared face, or a 3-2 flip
//      on an open fan, fails it by construction — and it is also the
//      volume-preservation guarantee the study needs;
//   2. the minimum shape quality over the cells they touch must strictly
//      improve;
//   3. the BOUNDARY may not move. A flip never touches it (the outer hull of
//      the retiled union is unchanged) and a collapse is only ever performed
//      on an INTERIOR node, so the domain boundary — and therefore the
//      interface the contract is measured on — comes out bit-identical.
constexpr double kRetileVolumeSlack = 1e-9;
// A collapse is the same retile: the star of an INTERIOR node is a ball whose
// outer faces are its link, and moving that node onto a neighbour re-cones the
// SAME ball from the neighbour — the cells that held both ends collapse to
// zero and the rest take up their volume exactly. So the identity above is the
// collapse's validity test as well as its volume guarantee, and there is no
// "how much volume may a sliver lose" tuning knob: the answer is none. A first
// cut bounded the discarded volume at a thousandth of the star instead, which
// refused every collapse on the pin-in-bore probe — a cap cell at shape
// quality 0.025 holds three thousandths of its own star, so the one operation
// that could have removed it was gated out by an arbitrary constant.

// Node -> incident cells, mesh-wide, in CSR: 8 bytes per (node, cell) pair and
// one allocation, against ~24 bytes of vector header per node for the obvious
// shape. A refined contact mesh runs to hundreds of thousands of nodes and
// this is rebuilt after every connectivity change.
struct NodeCells {
  std::vector<std::uint32_t> offset;  // nodes + 1
  std::vector<std::uint32_t> cells;

  std::pair<const std::uint32_t *, const std::uint32_t *> of(
      std::uint32_t node) const {
    if (node + 1 >= offset.size())
      return {nullptr, nullptr};
    return {cells.data() + offset[node], cells.data() + offset[node + 1]};
  }
};

NodeCells build_node_cells(const pm::fea::NodalMesh &nodal,
                           const std::vector<char> &dead) {
  NodeCells out;
  out.offset.assign(nodal.nodes.size() + 1, 0);
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    if (!dead.empty() && dead[e] != 0) continue;
    if (!is_tet4(nodal.elements[e])) continue;
    for (const std::uint32_t node : nodal.elements[e].nodes)
      ++out.offset[node + 1];
  }
  for (std::size_t i = 1; i < out.offset.size(); ++i)
    out.offset[i] += out.offset[i - 1];
  out.cells.resize(out.offset.back());
  std::vector<std::uint32_t> cursor(out.offset.begin(), out.offset.end() - 1);
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    if (!dead.empty() && dead[e] != 0) continue;
    if (!is_tet4(nodal.elements[e])) continue;
    for (const std::uint32_t node : nodal.elements[e].nodes)
      out.cells[cursor[node]++] = static_cast<std::uint32_t>(e);
  }
  return out;
}

using Tet = std::array<std::uint32_t, 4>;

Tet cell_nodes(const pm::fea::NodalElement &element) {
  return Tet{element.nodes[0], element.nodes[1], element.nodes[2],
             element.nodes[3]};
}

double tet_quality_of(const pm::fea::NodalMesh &nodal, const Tet &tet) {
  return pm::mesh::validity::tet_shape_quality(
      nodal.nodes[tet[0]], nodal.nodes[tet[1]], nodal.nodes[tet[2]],
      nodal.nodes[tet[3]]);
}

double tet_abs_volume(const pm::fea::NodalMesh &nodal, const Tet &tet) {
  return std::abs(pm::mesh::validity::tet_signed_volume(
      nodal.nodes[tet[0]], nodal.nodes[tet[1]], nodal.nodes[tet[2]],
      nodal.nodes[tet[3]]));
}

// A candidate laid out right-handed. The pass builds cells from scratch and
// every metric it reads is signed, so a mirrored candidate would score
// negative and be rejected as inverted; orient_tets_right_handed does the same
// job for the fill's own cells later.
Tet oriented(const pm::fea::NodalMesh &nodal, Tet tet) {
  if (pm::mesh::validity::tet_signed_volume(
          nodal.nodes[tet[0]], nodal.nodes[tet[1]], nodal.nodes[tet[2]],
          nodal.nodes[tet[3]]) < 0.0)
    std::swap(tet[1], tet[2]);
  return tet;
}

bool cell_has(const pm::fea::NodalElement &element, std::uint32_t node) {
  return element.nodes[0] == node || element.nodes[1] == node ||
         element.nodes[2] == node || element.nodes[3] == node;
}

bool retiles(double replacement_volume, double original_volume) {
  return original_volume > 0.0 &&
         std::abs(replacement_volume - original_volume) <=
             kRetileVolumeSlack * original_volume;
}

// ── When is a connectivity edit an improvement? ──────────────────────────────
// Sort both quality multisets ascending and compare lexicographically: the edit
// wins if it improves the WORST cell it touches, or leaves the worst alone and
// improves the next, and so on; a tie on every cell is broken toward the
// SMALLER mesh. Judging only the minimum stalls whenever the affected set holds
// a second bad cell the edit cannot help, and on the hinge saddle that was
// every remaining case: the operation that fixed a 0.022 needle was refused
// because a neighbour in the same star stayed at 0.022. This is the standard
// aggressive-improvement criterion, and since each accepted edit strictly
// raises a bounded, lexicographically-ordered vector it cannot cycle.
bool sorted_qualities_better(const std::vector<double> &before,
                            const std::vector<double> &after) {
  const std::size_t common = std::min(before.size(), after.size());
  for (std::size_t i = 0; i < common; ++i) {
    // Relative, because these are quality ratios spanning several decades and
    // an edit that changes the worst cell by a part per billion is noise.
    const double slack = std::abs(before[i]) * 1e-9;
    if (after[i] > before[i] + slack) return true;
    if (after[i] < before[i] - slack) return false;
  }
  return after.size() < before.size();
}

bool lexicographically_better(std::vector<double> before,
                              std::vector<double> after) {
  std::sort(before.begin(), before.end());
  std::sort(after.begin(), after.end());
  return sorted_qualities_better(before, after);
}

// Cells whose connectivity a pass has already rewritten. Every lookup below
// re-validates against the live element array, so a stale incidence entry is
// harmless — but a cell may only be rewritten once per pass, or two edits
// would each be judged against a mesh the other has changed.
struct MeshEdits {
  std::vector<char> dead;
  std::vector<char> dirty;
  std::size_t removed = 0;

  explicit MeshEdits(std::size_t cells) : dead(cells, 0), dirty(cells, 0) {}
  bool usable(std::uint32_t e) const {
    return e < dead.size() && dead[e] == 0 && dirty[e] == 0;
  }
  void claim(std::uint32_t e) {
    if (e < dirty.size()) dirty[e] = 1;
  }
  void remove(std::uint32_t e) {
    if (dead[e] == 0) {
      dead[e] = 1;
      ++removed;
    }
  }
  void grew(std::size_t cells) {
    dead.resize(cells, 0);
    dirty.resize(cells, 1);  // a cell this pass created is already settled
  }
};

// 2-3: the two cells sharing an interior face become three around the new edge
// joining their apexes. 3-2: the three cells around an interior edge become
// two, and that edge is gone. Candidates are the cells under the target; each
// is tried both ways and the first accepted edit wins.
void flip_region(pm::fea::NodalMesh &nodal,
                 const std::vector<std::size_t> &candidates, MeshEdits &edits,
                 int &flips_23, int &edge_removals) {
  if (candidates.empty()) return;
  const NodeCells incidence = build_node_cells(nodal, edits.dead);
  // Face census over the cells the candidates can reach. Both owners of any
  // face of a candidate are incident to that face's nodes, so restricting the
  // census to those cells still sees every use of every face in play.
  std::unordered_map<std::uint64_t, std::array<std::uint32_t, 2>> owners;
  std::unordered_map<std::uint64_t, int> uses;
  owners.reserve(candidates.size() * 32);
  uses.reserve(candidates.size() * 32);
  std::vector<std::uint32_t> local;
  local.reserve(candidates.size() * 16);
  for (const std::size_t candidate : candidates) {
    if (!is_tet4(nodal.elements[candidate])) continue;
    for (const std::uint32_t node : nodal.elements[candidate].nodes) {
      const auto range = incidence.of(node);
      local.insert(local.end(), range.first, range.second);
    }
  }
  std::sort(local.begin(), local.end());
  local.erase(std::unique(local.begin(), local.end()), local.end());
  for (const std::uint32_t e : local) {
    const pm::fea::NodalElement &element = nodal.elements[e];
    for (int f = 0; f < 4; ++f) {
      std::array<std::uint32_t, 3> tri{
          element.nodes[static_cast<std::size_t>(kTetFaces[f][0])],
          element.nodes[static_cast<std::size_t>(kTetFaces[f][1])],
          element.nodes[static_cast<std::size_t>(kTetFaces[f][2])]};
      std::sort(tri.begin(), tri.end());
      const std::uint64_t key = face_key(tri);
      const int seen = uses[key]++;
      if (seen < 2) owners[key][static_cast<std::size_t>(seen)] = e;
    }
  }

  report_fill_phase("quality flips");
  std::size_t completed = 0;
  for (const std::size_t candidate : candidates) {
    report_fill_work(nodal.elements.size() - edits.removed, completed++, candidates.size());
    const std::uint32_t here = static_cast<std::uint32_t>(candidate);
    if (!edits.usable(here) || !is_tet4(nodal.elements[here])) continue;
    const Tet cell = cell_nodes(nodal.elements[here]);
    const double here_quality = tet_quality_of(nodal, cell);
    bool done = false;

    // ── 2-3 ──────────────────────────────────────────────────────────────────
    for (int f = 0; f < 4 && !done; ++f) {
      std::array<std::uint32_t, 3> tri{
          cell[static_cast<std::size_t>(kTetFaces[f][0])],
          cell[static_cast<std::size_t>(kTetFaces[f][1])],
          cell[static_cast<std::size_t>(kTetFaces[f][2])]};
      const std::uint32_t apex = cell[static_cast<std::size_t>(kTetFaceApex[f])];
      std::array<std::uint32_t, 3> sorted = tri;
      std::sort(sorted.begin(), sorted.end());
      const std::uint64_t key = face_key(sorted);
      const auto used = uses.find(key);
      if (used == uses.end() || used->second != 2) continue;  // not interior
      const std::array<std::uint32_t, 2> &pair = owners[key];
      const std::uint32_t other = pair[0] == here ? pair[1] : pair[0];
      if (other == here || !edits.usable(other) ||
          !is_tet4(nodal.elements[other]))
        continue;
      const pm::fea::NodalElement &neighbour = nodal.elements[other];
      if (!cell_has(neighbour, tri[0]) || !cell_has(neighbour, tri[1]) ||
          !cell_has(neighbour, tri[2]))
        continue;  // stale census entry
      // Not `far`: <windows.h> defines near/far as empty macros, which breaks MSVC.
      std::uint32_t opposite = neighbour.nodes[0];
      for (const std::uint32_t node : neighbour.nodes)
        if (node != tri[0] && node != tri[1] && node != tri[2]) opposite = node;
      if (opposite == apex) continue;
      const Tet neighbour_cell = cell_nodes(neighbour);
      const std::vector<double> before{here_quality,
                                       tet_quality_of(nodal, neighbour_cell)};
      const double volume = tet_abs_volume(nodal, cell) +
                            tet_abs_volume(nodal, neighbour_cell);
      const std::array<Tet, 3> made{
          oriented(nodal, Tet{apex, opposite, tri[0], tri[1]}),
          oriented(nodal, Tet{apex, opposite, tri[1], tri[2]}),
          oriented(nodal, Tet{apex, opposite, tri[2], tri[0]})};
      std::vector<double> after;
      double made_volume = 0.0;
      for (const Tet &tet : made) {
        after.push_back(tet_quality_of(nodal, tet));
        made_volume += tet_abs_volume(nodal, tet);
      }
      if (!lexicographically_better(before, after) ||
          !retiles(made_volume, volume))
        continue;
      nodal.elements[here].nodes.assign(made[0].begin(), made[0].end());
      nodal.elements[other].nodes.assign(made[1].begin(), made[1].end());
      nodal.elements.push_back(pm::fea::NodalElement{
          pm::fea::ElementType::kTet4,
          {made[2][0], made[2][1], made[2][2], made[2][3]}});
      edits.grew(nodal.elements.size());
      edits.claim(here);
      edits.claim(other);
      ++flips_23;
      done = true;
    }
    if (done) continue;

    // ── EDGE REMOVAL: 3-2, and its 4-4 / 5-6 generalizations ────────────────
    // The 3-2 flip is the n = 3 case of one operation: an interior edge whose
    // incident cells form a closed ring of n is removed by re-triangulating
    // that ring's n-gon and coning each triangle to both edge ends —
    // 2*(n-2) cells for n. Stopping at n = 3 is what left the pin-in-bore
    // probe with 18 cells at shape quality 0.0276 (scaled Jacobian 0.035,
    // against a 0.05 floor): those cells sit against the bore rim, so two of
    // their four nodes are pinned CAD-edge nodes and neither smoothing nor a
    // collapse can reach them, but the edge between the two FREE nodes has a
    // ring of 4, 5 or 6. The ring's OPTIMAL triangulation is found by dynamic
    // program below, so the operation is complete for every ring it accepts
    // rather than a sample of them.
    static constexpr int kEdgeEnds[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                            {1, 2}, {1, 3}, {2, 3}};
    constexpr std::size_t kMaxRing = 7;
    for (const auto &ends : kEdgeEnds) {
      const std::uint32_t u = cell[static_cast<std::size_t>(ends[0])];
      const std::uint32_t v = cell[static_cast<std::size_t>(ends[1])];
      std::array<std::uint32_t, kMaxRing> around{};
      std::array<std::array<std::uint32_t, 2>, kMaxRing> opposite{};
      std::size_t found = 0;
      bool ok = true;
      const auto range = incidence.of(u);
      for (const std::uint32_t *it = range.first; it != range.second && ok; ++it) {
        if (!is_tet4(nodal.elements[*it]) || !cell_has(nodal.elements[*it], v))
          continue;
        if (found == kMaxRing || !edits.usable(*it)) {
          ok = false;
          break;
        }
        std::array<std::uint32_t, 2> pair{};
        std::size_t at = 0;
        for (const std::uint32_t node : nodal.elements[*it].nodes)
          if (node != u && node != v) {
            if (at == 2) { ok = false; break; }
            pair[at++] = node;
          }
        if (!ok || at != 2) { ok = false; break; }
        opposite[found] = pair;
        around[found] = *it;
        ++found;
      }
      if (!ok || found < 3) continue;
      // Walk the link: the cells' opposite PAIRS are the n-gon's edges, so a
      // closed cycle through all of them is the ring, in order. An OPEN fan —
      // which is what a boundary edge has — never closes, and that is what
      // keeps edge removal off the boundary.
      std::array<std::uint32_t, kMaxRing> ring{};
      std::array<char, kMaxRing> used{};
      used.fill(0);
      ring[0] = opposite[0][0];
      ring[1] = opposite[0][1];
      used[0] = 1;
      std::size_t placed = 2;
      for (std::size_t step = 1; step < found && ok; ++step) {
        bool advanced = false;
        for (std::size_t i = 0; i < found && !advanced; ++i) {
          if (used[i] != 0) continue;
          for (int side = 0; side < 2; ++side)
            if (opposite[i][static_cast<std::size_t>(side)] == ring[placed - 1]) {
              const std::uint32_t next = opposite[i][static_cast<std::size_t>(1 - side)];
              used[i] = 1;
              advanced = true;
              if (step + 1 == found) {
                if (next != ring[0]) ok = false;  // does not close: open fan
              } else if (placed < kMaxRing) {
                ring[placed++] = next;
              } else {
                ok = false;
              }
              break;
            }
        }
        if (!advanced) ok = false;
      }
      if (!ok || placed != found) continue;
      std::array<std::uint32_t, kMaxRing> distinct = ring;
      std::sort(distinct.begin(), distinct.begin() + static_cast<std::ptrdiff_t>(found));
      if (std::adjacent_find(distinct.begin(),
                             distinct.begin() + static_cast<std::ptrdiff_t>(found)) !=
          distinct.begin() + static_cast<std::ptrdiff_t>(found))
        continue;  // a repeated ring node is not a simple polygon
      double volume = 0.0;
      std::vector<double> before;
      before.reserve(found);
      for (std::size_t i = 0; i < found; ++i) {
        const Tet tet = cell_nodes(nodal.elements[around[i]]);
        volume += tet_abs_volume(nodal, tet);
        before.push_back(tet_quality_of(nodal, tet));
      }
      // ── The OPTIMAL ring triangulation, by dynamic program ───────────────
      // Klincsek's recursion: the best triangulation of the sub-polygon
      // r[i..j] closed by the chord (i, j) is the best over its third vertex
      // k of min(that triangle's own worst coned cell, the two sub-polygons).
      // Enumerating FANS instead was tried and is not enough: a hexagonal ring
      // has 14 triangulations and only 6 of them are fans, and the hinge
      // saddle's last four bad cells sat on a ring of 6 whose winning
      // triangulation is not one (all four of their nodes are on CAD feature
      // edges, so nothing else in the pass can reach them). O(n^3) over n <= 7
      // is at most a few hundred cell evaluations per edge.
      std::array<std::array<double, kMaxRing>, kMaxRing> best_q{};
      std::array<std::array<std::size_t, kMaxRing>, kMaxRing> cut{};
      const auto coned_quality = [&](std::size_t i, std::size_t k,
                                     std::size_t j) {
        double worst = std::numeric_limits<double>::infinity();
        for (const std::uint32_t tip : {u, v})
          worst = std::min(worst,
                           tet_quality_of(nodal, oriented(nodal, Tet{tip, ring[i],
                                                                     ring[k],
                                                                     ring[j]})));
        return worst;
      };
      for (std::size_t span = 2; span < found; ++span)
        for (std::size_t i = 0; i + span < found; ++i) {
          const std::size_t j = i + span;
          best_q[i][j] = -std::numeric_limits<double>::infinity();
          for (std::size_t k = i + 1; k < j; ++k) {
            double quality = coned_quality(i, k, j);
            if (k > i + 1) quality = std::min(quality, best_q[i][k]);
            if (j > k + 1) quality = std::min(quality, best_q[k][j]);
            if (quality > best_q[i][j]) {
              best_q[i][j] = quality;
              cut[i][j] = k;
            }
          }
        }
      // Walk the DP back out into the triangles it chose, then cone each to
      // both ends of the edge being removed. The DP maximises the ring's WORST
      // new cell; the accept test below is the lexicographic one, so a
      // triangulation that ties on the worst still wins on the rest.
      std::vector<Tet> best;
      best.reserve(2 * (found - 2));
      std::vector<std::pair<std::size_t, std::size_t>> todo{{0, found - 1}};
      double made_volume = 0.0;
      while (!todo.empty()) {
        const auto [i, j] = todo.back();
        todo.pop_back();
        if (j <= i + 1) continue;
        const std::size_t k = cut[i][j];
        for (const std::uint32_t tip : {u, v}) {
          const Tet tet =
              oriented(nodal, Tet{tip, ring[i], ring[k], ring[j]});
          made_volume += tet_abs_volume(nodal, tet);
          best.push_back(tet);
        }
        todo.push_back({i, k});
        todo.push_back({k, j});
      }
      std::vector<double> after;
      after.reserve(best.size());
      for (const Tet &tet : best) after.push_back(tet_quality_of(nodal, tet));
      if (best.size() != 2 * (found - 2) || !retiles(made_volume, volume) ||
          !lexicographically_better(before, after))
        continue;
      for (std::size_t i = 0; i < found; ++i) {
        if (i < best.size())
          nodal.elements[around[i]].nodes.assign(best[i].begin(), best[i].end());
        else
          edits.remove(around[i]);
        edits.claim(around[i]);
      }
      for (std::size_t i = found; i < best.size(); ++i)
        nodal.elements.push_back(pm::fea::NodalElement{
            pm::fea::ElementType::kTet4,
            {best[i][0], best[i][1], best[i][2], best[i][3]}});
      if (best.size() > found) edits.grew(nodal.elements.size());
      ++edge_removals;
      break;
    }
  }
}

// Remove a sliver by collapsing its shortest edge onto one end.
//
// `boundary_edge_limit` is the longest BOUNDARY edge the collapse may leave at
// a point, and it exists because the pass and the wave loop otherwise fight: a
// collapse coarsens an interface face, the next wave's `scan` marks it, the
// wave bisects it, the next pass collapses it again. Measured on the
// pin-in-bore probe, unrestricted surface collapses took the run from 6 waves
// / 150 484 elements / 20 s to 10 waves / 3 901 081 elements / 366 s and left
// the region WORSE than it started (aspect_max 724), because every wave
// arrived on a mesh the previous pass had just chewed.
//
// The limit is the marking threshold itself, so the invariant is exact rather
// than prophylactic: `scan` marks a node whose MEAN incident boundary-edge
// length exceeds the request, so a collapse that leaves no edge longer than
// that cannot raise any node's mean past it and cannot re-trigger a wave. A
// blanket ban on collapsing inside the measured zone was tried first and is
// too crude — it left the hinge saddle with four cells at shape quality 0.018
// whose every node sits ON the interface, which is exactly where a sliver
// hurts the solve most. Interior-node collapses need no limit at all: they
// cannot touch the boundary.
void collapse_slivers(const pm::geom::TriSurface *surface,
                      pm::fea::NodalMesh &nodal,
                      const std::vector<std::size_t> &candidates,
                      const StarTopology &topo, MeshEdits &edits,
                      const std::function<double(const Eigen::Vector3d &)>
                          &boundary_edge_limit,
                      int &collapsed) {
  if (candidates.empty()) return;
  const NodeCells incidence = build_node_cells(nodal, edits.dead);
  static constexpr int kEdgeEnds[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                          {1, 2}, {1, 3}, {2, 3}};
  std::vector<std::uint32_t> doomed;
  std::vector<std::uint32_t> moved;
  report_fill_phase("quality collapses");
  std::size_t completed = 0;
  for (const std::size_t candidate : candidates) {
    report_fill_work(nodal.elements.size() - edits.removed, completed++, candidates.size());
    const std::uint32_t here = static_cast<std::uint32_t>(candidate);
    if (!edits.usable(here) || !is_tet4(nodal.elements[here])) continue;
    const Tet cell = cell_nodes(nodal.elements[here]);
    const double here_quality = tet_quality_of(nodal, cell);
    if (here_quality >= kQualityTarget) continue;
    // The longest boundary edge that ALREADY exists at a node. A collapse
    // replaces `from`'s boundary edges with edges of comparable length from
    // `into`, so allowing up to this length is a monotonicity guarantee: the
    // population of long boundary edges here cannot grow, and therefore no
    // node's mean can cross `scan`'s marking threshold that was not already
    // over it. A flat request threshold alone is too strict to be useful —
    // the hinge saddle's own interface carries 0.81 mm boundary edges beside a
    // 0.4 mm request, so the four cells that mattered had every legal collapse
    // refused for creating an edge exactly as long as the one it replaced.
    const auto longest_boundary_edge = [&](std::uint32_t node) {
      const std::int32_t node_slot = topo.at(node);
      double longest = 0.0;
      if (node_slot < 0) return longest;
      for (const std::uint32_t other :
           topo.boundary_ring[static_cast<std::size_t>(node_slot)])
        longest =
            std::max(longest, (nodal.nodes[node] - nodal.nodes[other]).norm());
      return longest;
    };
    // Shortest edge first: that is the one a sliver has no use for.
    std::array<std::pair<double, int>, 6> edges{};
    for (int i = 0; i < 6; ++i) {
      const std::uint32_t a = cell[static_cast<std::size_t>(kEdgeEnds[i][0])];
      const std::uint32_t b = cell[static_cast<std::size_t>(kEdgeEnds[i][1])];
      edges[static_cast<std::size_t>(i)] = {
          (nodal.nodes[a] - nodal.nodes[b]).norm(), i};
    }
    std::sort(edges.begin(), edges.end());
    bool done = false;
    for (const auto &edge : edges) {
      if (done) break;
      const std::uint32_t ends[2] = {
          cell[static_cast<std::size_t>(kEdgeEnds[edge.second][0])],
          cell[static_cast<std::size_t>(kEdgeEnds[edge.second][1])]};
      for (int order = 0; order < 2 && !done; ++order) {
        const std::uint32_t from = ends[order];
        const std::uint32_t into = ends[1 - order];
        const std::int32_t slot = topo.at(from);
        // A node the working set never classified is not a candidate. A CAD
        // edge or corner is NOT vetoed here, unlike in the smoother: a
        // smoothing move relocates the node and would round the feature, while
        // a collapse REMOVES it, and removing one node from a straight feature
        // edge leaves that edge exactly where it was. The drift bound below is
        // what tells the two cases apart, without a special case: collapsing
        // ALONG a straight edge sweeps no volume at all, while collapsing an
        // edge node sideways onto a face sweeps a wedge whose mean
        // displacement is a third of the local element size, an order over the
        // bound. Vetoing features outright instead left the hinge saddle with
        // 4 cells under the ship floor and a jacobian_min of 0.029: they were
        // wedged into the saddle's own radial edges, where every node of the
        // cell is a feature node.
        if (slot < 0) continue;
        const std::size_t at = static_cast<std::size_t>(slot);
        const bool interior = topo.on_boundary[at] == 0;
        // Every boundary edge the collapse leaves at `into` is one from a node
        // that shared a boundary face with `from`, so this is the whole set.
        // The bar is the contract's own marking threshold OR the longest
        // boundary edge already there, whichever is more permissive.
        if (!interior && boundary_edge_limit) {
          const double limit =
              std::max({std::min(boundary_edge_limit(nodal.nodes[from]),
                                 boundary_edge_limit(nodal.nodes[into])),
                        longest_boundary_edge(from),
                        longest_boundary_edge(into)});
          bool within = true;
          for (const std::uint32_t other : topo.boundary_ring[at])
            if (other != into &&
                (nodal.nodes[into] - nodal.nodes[other]).norm() > limit) {
              within = false;
              break;
            }
          if (!within) continue;
        }
        const auto range = incidence.of(from);
        doomed.clear();
        moved.clear();
        double star_volume = 0.0;
        double shortest_edge = std::numeric_limits<double>::infinity();
        bool ok = true;
        // Face census over `from`'s own star. Every face carrying `from` has
        // both its owners in that star, so a face used once here is a
        // boundary face of the mesh, exactly.
        struct StarFace {
          std::array<std::uint32_t, 3> tri{};
          int uses = 0;
        };
        std::unordered_map<std::uint64_t, StarFace> star_faces;
        for (const std::uint32_t *it = range.first; it != range.second; ++it) {
          if (!is_tet4(nodal.elements[*it])) { ok = false; break; }
          if (!edits.usable(*it)) { ok = false; break; }
          const pm::fea::NodalElement &element = nodal.elements[*it];
          const Tet tet = cell_nodes(element);
          star_volume += tet_abs_volume(nodal, tet);
          for (const std::uint32_t node : tet)
            if (node != from)
              shortest_edge = std::min(
                  shortest_edge, (nodal.nodes[from] - nodal.nodes[node]).norm());
          if (!interior)
            for (int f = 0; f < 4; ++f) {
              std::array<std::uint32_t, 3> tri{
                  tet[static_cast<std::size_t>(kTetFaces[f][0])],
                  tet[static_cast<std::size_t>(kTetFaces[f][1])],
                  tet[static_cast<std::size_t>(kTetFaces[f][2])]};
              if (tri[0] != from && tri[1] != from && tri[2] != from) continue;
              std::array<std::uint32_t, 3> sorted_tri = tri;
              std::sort(sorted_tri.begin(), sorted_tri.end());
              auto slot_it = star_faces.find(face_key(sorted_tri));
              if (slot_it == star_faces.end())
                star_faces.emplace(face_key(sorted_tri), StarFace{tri, 1});
              else
                ++slot_it->second.uses;
            }
          if (cell_has(element, into))
            doomed.push_back(*it);
          else
            moved.push_back(*it);
        }
        if (!ok || doomed.empty() || !(star_volume > 0.0) ||
            !(shortest_edge > 0.0) || !std::isfinite(shortest_edge))
          continue;
        // The retile identity is checked below, once the replacements exist.
        // A rewritten cell may not duplicate one that already exists — that
        // would be two cells over the same region, which no volume test
        // catches because both are positive.
        std::vector<Tet> existing;
        const auto into_range = incidence.of(into);
        for (const std::uint32_t *it = into_range.first; it != into_range.second;
             ++it) {
          if (!is_tet4(nodal.elements[*it]) || edits.dead[*it] != 0) continue;
          if (std::find(doomed.begin(), doomed.end(), *it) != doomed.end())
            continue;
          Tet sorted = cell_nodes(nodal.elements[*it]);
          std::sort(sorted.begin(), sorted.end());
          existing.push_back(sorted);
        }
        std::vector<double> before;
        std::vector<double> after;
        before.reserve(moved.size() + doomed.size());
        after.reserve(moved.size());
        double made_volume = 0.0;
        std::vector<Tet> replacement;
        replacement.reserve(moved.size());
        for (const std::uint32_t e : moved) {
          Tet tet = cell_nodes(nodal.elements[e]);
          before.push_back(tet_quality_of(nodal, tet));
          for (std::uint32_t &node : tet)
            if (node == from) node = into;
          Tet sorted = tet;
          std::sort(sorted.begin(), sorted.end());
          if (sorted[0] == sorted[1] || sorted[1] == sorted[2] ||
              sorted[2] == sorted[3] ||
              std::find(existing.begin(), existing.end(), sorted) !=
                  existing.end()) {
            ok = false;
            break;
          }
          tet = oriented(nodal, tet);
          after.push_back(tet_quality_of(nodal, tet));
          made_volume += tet_abs_volume(nodal, tet);
          replacement.push_back(tet);
        }
        if (!ok || replacement.empty()) continue;
        for (const std::uint32_t e : doomed)
          before.push_back(tet_quality_of(nodal, cell_nodes(nodal.elements[e])));
        if (!lexicographically_better(before, after)) continue;
        // ── How much geometry may a collapse move? ──────────────────────────
        // The star of an INTERIOR node is a ball whose outer faces are its
        // link, and re-coning it from a neighbour reproduces the same ball, so
        // the volumes must agree exactly and the boundary is untouched.
        //
        // A SURFACE node is the case that actually matters: the cell that
        // stalled the pin-in-bore probe at aspect 39.5 was a flat cap on a
        // planar face whose apex sat 0.0153 mm inside it, and the ones that
        // stalled the hinge saddle were the same shape 0.016-0.040 mm off a
        // radial face. Projecting such an apex back onto its face makes the
        // cell degenerate, so the only fix is to remove the node, and there is
        // no interior node in the cell to remove instead.
        //
        // What must be preserved is the BOUNDARY's agreement with the CAD
        // surface, so that is what is measured: every boundary facet the
        // collapse creates is sampled at its own centroid against the
        // tessellation, and the deviation may not exceed kBoundaryDriftFrac of
        // the local surface element size (the fill's own snap already carries
        // about 9 % of h — its note quotes 1.21 mm at h = 13.94 mm — so a
        // collapse inside that is inside the placement error the mesh already
        // has). A swept-volume proxy was tried first and is the wrong
        // quantity: it charges the collapse for the off-surface node it is
        // REMOVING, which is exactly backwards, and on the saddle it worked out
        // to 0.013 mm against a 0.014 mm bound — the fix landed or missed on
        // rounding. Without a tessellation to answer against there is nothing
        // to measure, so the volume identity is required instead.
        if (interior) {
          if (!retiles(made_volume, star_volume)) continue;
        } else {
          double surface_scale = 0.0;
          int surface_edges = 0;
          for (const std::uint32_t other : topo.boundary_ring[at]) {
            surface_scale += (nodal.nodes[from] - nodal.nodes[other]).norm();
            ++surface_edges;
          }
          if (surface_edges > 0)
            surface_scale /= static_cast<double>(surface_edges);
          else
            surface_scale = shortest_edge;
          if (!(surface_scale > 0.0)) continue;
          if (surface == nullptr || surface->triangles.empty()) {
            if (!retiles(made_volume, star_volume)) continue;
          } else {
            const double tolerance = kBoundaryDriftFrac * surface_scale;
            bool faithful = true;
            for (const auto &entry : star_faces) {
              if (entry.second.uses != 1) continue;
              std::array<std::uint32_t, 3> tri = entry.second.tri;
              bool degenerate = false;
              for (std::uint32_t &node : tri) {
                if (node == into) degenerate = true;  // this facet collapses away
                if (node == from) node = into;
              }
              if (degenerate) continue;
              const Eigen::Vector3d centroid =
                  (nodal.nodes[tri[0]] + nodal.nodes[tri[1]] +
                   nodal.nodes[tri[2]]) /
                  3.0;
              const double deviation =
                  pm::mesh::closest_on_surface(*surface, centroid).distance;
              if (!std::isfinite(deviation) || deviation > tolerance) {
                faithful = false;
                break;
              }
            }
            if (!faithful) continue;
          }
        }
        for (std::size_t i = 0; i < moved.size(); ++i) {
          nodal.elements[moved[i]].nodes.assign(replacement[i].begin(),
                                                replacement[i].end());
          edits.claim(moved[i]);
        }
        for (const std::uint32_t e : doomed) {
          edits.remove(e);
          edits.claim(e);
        }
        ++collapsed;
        done = true;
      }
    }
  }
}

// Drop the cells a pass killed. Element numbering shifts, node numbering does
// not: build_mesh's own compact_unused_nodes retires the nodes a collapse
// orphaned, after the last consumer of a node index has run.
void compact_dead_cells(pm::fea::NodalMesh &nodal, const std::vector<char> &dead,
                        std::vector<char> &in_region) {
  if (std::find(dead.begin(), dead.end(), 1) == dead.end()) return;
  std::vector<pm::fea::NodalElement> kept;
  std::vector<char> kept_region;
  kept.reserve(nodal.elements.size());
  kept_region.reserve(in_region.size());
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    if (e < dead.size() && dead[e] != 0) continue;
    kept.push_back(std::move(nodal.elements[e]));
    if (e < in_region.size()) kept_region.push_back(in_region[e]);
  }
  nodal.elements = std::move(kept);
  in_region = std::move(kept_region);
}

// ── The region the pass owns, and what it measures over it ───────────────────
// Every cell that owns a node a wave CREATED, plus one layer of neighbours.
// That is exactly the locally-refined zone: LEPP closure only bisects cells,
// so a refined cell always carries a new mid-edge node, and the extra layer
// picks up the un-bisected neighbours whose shape the smoothing pass can still
// change through a shared node. It costs two element sweeps and — unlike a
// distance-to-seed test — not one size-field evaluation.
std::vector<char> refined_region(const pm::fea::NodalMesh &nodal,
                                 std::size_t nodes_before_waves) {
  std::vector<char> in_region(nodal.elements.size(), 0);
  std::vector<char> in_layer(nodal.nodes.size(), 0);
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    const pm::fea::NodalElement &element = nodal.elements[e];
    if (!is_tet4(element)) continue;
    bool fresh = false;
    for (const std::uint32_t node : element.nodes)
      if (node >= nodes_before_waves) fresh = true;
    if (!fresh) continue;
    in_region[e] = 1;
    for (const std::uint32_t node : element.nodes) in_layer[node] = 1;
  }
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    if (in_region[e] != 0 || !is_tet4(nodal.elements[e])) continue;
    for (const std::uint32_t node : nodal.elements[e].nodes)
      if (in_layer[node] != 0) {
        in_region[e] = 1;
        break;
      }
  }
  return in_region;
}

struct RegionQuality {
  double jacobian_min = 0.0;
  double aspect_max = 0.0;
  std::size_t below_floor = 0;
  std::size_t cells = 0;

  // The stop test, stated in the currencies the packet reports. `below_floor`
  // rides with them because it is the STRICTEST of the three (shape quality
  // bounds both — see kQualityTarget), and a region that still ships cells
  // under polymesh's own ship floor is not finished however good its extremes
  // read.
  bool met() const {
    return cells == 0 || (jacobian_min >= kQualityJacobianFloor &&
                          aspect_max <= kQualityAspectCeiling &&
                          below_floor == 0);
  }
};

RegionQuality region_quality(const pm::fea::NodalMesh &nodal,
                             const std::vector<char> &in_region) {
  RegionQuality out;
  out.jacobian_min = std::numeric_limits<double>::infinity();
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    if (e >= in_region.size() || in_region[e] == 0) continue;
    const pm::fea::NodalElement &element = nodal.elements[e];
    if (!is_tet4(element)) continue;
    const detail::TetShape shape = detail::tet_shape(
        nodal.nodes[element.nodes[0]].data(), nodal.nodes[element.nodes[1]].data(),
        nodal.nodes[element.nodes[2]].data(), nodal.nodes[element.nodes[3]].data());
    ++out.cells;
    out.jacobian_min = std::min(out.jacobian_min, shape.scaled_jacobian);
    if (std::isfinite(shape.aspect))
      out.aspect_max = std::max(out.aspect_max, shape.aspect);
    else
      out.aspect_max = std::numeric_limits<double>::infinity();
    if (tet_quality_of(nodal, cell_nodes(element)) <
        pm::mesh::validity::kCellShapeFloor)
      ++out.below_floor;
  }
  if (out.cells == 0 || !std::isfinite(out.jacobian_min))
    out.jacobian_min = 0.0;
  return out;
}

// ONE bounded pass: boundary-aware smoothing, then flips, then collapses, over
// the region cells that are still under the quality target. The order matters —
// a node move is the cheapest fix and often the only one needed, a flip retiles
// what no move can, and a collapse removes what neither can save.
//
// Returns connectivity progress, which can unlock a repair on the next pass
// even when the deficient quality multiset tied. The caller measures smoothing
// progress directly: its counters cover only the first internal sweep.
bool quality_pass(const pm::geom::TriSurface *surface, pm::fea::NodalMesh &nodal,
                  const std::function<double(const Eigen::Vector3d &)>
                      &boundary_edge_limit,
                  std::vector<char> &in_region, LocalContract &contract) {
  report_progress("mesh_interface_band_quality", nodal.elements.size());
  std::vector<std::size_t> poor;
  for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
    report_fill_work(nodal.elements.size(), e, nodal.elements.size());
    if (e >= in_region.size() || in_region[e] == 0) continue;
    if (!is_tet4(nodal.elements[e])) continue;
    if (tet_quality_of(nodal, cell_nodes(nodal.elements[e])) < kQualityTarget)
      poor.push_back(e);
  }
  if (poor.empty()) return false;
  const int flips_before = contract.flips_23;
  const int removals_before = contract.edge_removals;
  const int collapses_before = contract.collapsed_slivers;
  ++contract.quality_passes;
  std::vector<std::uint32_t> movable;
  movable.reserve(poor.size() * 4);
  for (const std::size_t e : poor)
    movable.insert(movable.end(), nodal.elements[e].nodes.begin(),
                   nodal.elements[e].nodes.begin() + 4);
  const StarTopology topo = build_star_topology(nodal, std::move(movable));
  smooth_nodes(surface, nodal, topo, contract.reprojected_nodes,
               contract.smoothed_nodes);
  MeshEdits edits(nodal.elements.size());
  flip_region(nodal, poor, edits, contract.flips_23, contract.edge_removals);
  in_region.resize(nodal.elements.size(), 1);
  // The flips' `dirty` marks do not carry into the collapse sweep. `dirty`
  // exists so two edits of ONE sweep are not each judged against a mesh the
  // other changed; a collapse re-reads every node and cell it touches from the
  // live arrays, so a cell a flip rewrote is perfectly good input. Carrying the
  // marks over is what pinned the hinge saddle at jacobian_min 0.0373 through
  // every acceptance rule tried: the flip sweep is deterministic, so it dirtied
  // the same neighbour every pass and the one collapse that could have removed
  // the cell never got a clean look at it. `dead` DOES carry over — a cell that
  // is gone is gone.
  std::fill(edits.dirty.begin(), edits.dirty.end(), 0);
  collapse_slivers(surface, nodal, poor, topo, edits, boundary_edge_limit,
                   contract.collapsed_slivers);
  compact_dead_cells(nodal, edits.dead, in_region);
  in_region.resize(nodal.elements.size(), 1);
  return contract.flips_23 != flips_before ||
         contract.edge_removals != removals_before ||
         contract.collapsed_slivers != collapses_before;
}

// Projection can create a short-edge sliver without any requested local band.
// Repair that topology globally, without running the local LEB/smoothing loop.
int collapse_global_slivers(const pm::geom::TriSurface *surface,
                            pm::fea::NodalMesh &nodal) {
  constexpr double kGlobalJacobianFloor = 0.034;
  int collapsed = 0;
  for (int pass = 0; pass < kQualityPasses; ++pass) {
    report_fill_phase("quality pass", pass + 1, kQualityPasses);
    std::vector<std::size_t> poor;
    std::vector<std::uint32_t> movable;
    for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
      report_fill_work(nodal.elements.size(), e, nodal.elements.size());
      const auto &element = nodal.elements[e];
      if (!is_tet4(element)) continue;
      const auto shape = detail::tet_shape(
          nodal.nodes[element.nodes[0]].data(), nodal.nodes[element.nodes[1]].data(),
          nodal.nodes[element.nodes[2]].data(), nodal.nodes[element.nodes[3]].data());
      if (shape.scaled_jacobian >= kGlobalJacobianFloor) continue;
      poor.push_back(e);
      movable.insert(movable.end(), element.nodes.begin(), element.nodes.begin() + 4);
    }
    if (poor.empty()) break;
    const StarTopology topo = build_star_topology(nodal, std::move(movable));
    MeshEdits edits(nodal.elements.size());
    const int before = collapsed;
    collapse_slivers(surface, nodal, poor, topo, edits, {}, collapsed);
    if (collapsed == before) break;
    std::vector<char> region;
    compact_dead_cells(nodal, edits.dead, region);
  }
  return collapsed;
}

void deliver_local_contract(const LoadedModel &loaded, const MeshOptions &options,
                            double h, pm::fea::NodalMesh &nodal,
                            LocalContract &contract) {
  const MeshLocalRefine &request = options.local_refine;
  contract.requested_mm = request.size_mm;
  contract.element_ceiling = static_cast<std::size_t>(loaded.band.element_ceiling);
  contract.elems = nodal.elements.size();
  if (!request.active() || nodal.elements.empty())
    return;
  for (const pm::fea::NodalElement &element : nodal.elements)
    if (element.type != pm::fea::ElementType::kTet4 || element.nodes.size() != 4)
      return;  // LEB is tet4-only; promotion has not happened yet, so this holds
  // The tessellation the mid-edge projections and the smoothing pass answer
  // against. Empty on an inline-mesh study, in which case both fall back to
  // pure chord geometry.
  const pm::geom::TriSurface *const cad_surface =
      loaded.model.surface.triangles.empty() ? nullptr : &loaded.model.surface;
  std::vector<Eigen::Vector3d> seeds;
  seeds.reserve(request.points.size() / 3);
  for (std::size_t i = 0; i + 2 < request.points.size(); i += 3)
    seeds.emplace_back(request.points[i], request.points[i + 1],
                       request.points[i + 2]);
  // ONE distance oracle, TWO derived quantities. A GradedSizing evaluates to
  // `h_min + beta * dist(x, nearest seed)` inside its clamp, so a field with a
  // high enough ceiling hands back the distance to the seed cloud itself — no
  // second spatial index to build or keep in step.
  //
  // kBeta is 1.0, the standard gradient limit, and NOT local_size_field's 0.5:
  // that 0.5 exists because the FILL evaluates its field at cell CENTRES, half a
  // cell away from the interface, where a steeper field has already given up on
  // the fine cell. Bisection judges whole elements against their own vertices,
  // so it has no such offset, and 1.0 halves the depth of the graded shell.
  constexpr double kBeta = 1.0;
  // ── The CORE: why the fine zone is a shell and not a distance-to-seed cone ──
  // The seeds are the PREVIOUS mesh's interface nodes, so they sample the
  // interface surface at that mesh's node spacing — 1.3 mm on the live keyway.
  // A pure `size + beta*dist` field therefore only asks for the requested size
  // exactly AT a seed: a point on the interface halfway between two seeds is
  // allowed to be size + beta*(spacing/2) coarse, and the delivered interface
  // settles a whole spacing above the request no matter how many waves run
  // (measured: 11 waves, still 7 mm on a 0.5 mm request). So the request is FLAT
  // out to the widest LATERAL gap in the cloud — spacing/sqrt(2) for a surface
  // sampled on a grid of that spacing — and grades outward from there. The same
  // zone is what the contract is measured over, so the two agree by
  // construction, and its DEPTH is what the refinement costs: 0.7 x spacing, not
  // a whole spacing, is the difference between a 38k-element shell and a 100k
  // one on the same seat.
  const double uncapped_core =
      std::max(0.71 * seed_spacing_mm(seeds), 2.0 * request.size_mm);
  const double core_mm = request.max_depth_mm > 0.0
      ? std::min(uncapped_core, request.max_depth_mm * (2.0 / 3.0))
      : uncapped_core;
  contract.core_mm = core_mm;
  const double reach_span = core_mm + h + 2.0 * request.size_mm;
  const std::vector<pm::adapt::SizeSource> sources =
      pm::adapt::point_size_sources(seeds, request.size_mm);
  const pm::mesh::SizeFieldFn reach = pm::adapt::size_field_from_sources(
      sources, request.size_mm, request.size_mm + kBeta * reach_span, kBeta);
  auto seed_distance = [&](const Eigen::Vector3d &p) {
    return (reach(p) - request.size_mm) / kBeta;  // saturates at reach_span
  };
  const double background = std::max(h, request.size_mm);
  // Bound the fine shell independently of the background cell size. The last
  // third of the requested depth grades back to the bulk; LEPP may still add
  // closure neighbours outside this direct-marking zone.
  const double grade = request.max_depth_mm > core_mm
      ? std::max(kBeta, (background - request.size_mm) /
                           (request.max_depth_mm - core_mm))
      : kBeta;
  contract.transition_depth_mm =
      core_mm + (background - request.size_mm) / grade;
  auto want = [&](double distance) {
    const double outside = std::max(0.0, distance - core_mm);
    return std::min(background, request.size_mm + grade * outside);
  };
  // Materialize the vector: a deduced Eigen expression would retain this local.
  auto centroid_of = [&](const pm::fea::NodalElement &element) -> Eigen::Vector3d {
    Eigen::Vector3d centroid = Eigen::Vector3d::Zero();
    for (std::uint32_t n : element.nodes)
      centroid += nodal.nodes[n];
    return centroid / 4.0;
  };
  // What the contract is judged on, and what drives the marking: the BOUNDARY
  // FACES inside the core. Faces, not elements — the contact guard's own
  // interface size is the mean boundary-face edge length incident on a candidate
  // node (fea_contact.cpp), so measuring anything else here would refuse studies
  // the guard accepts and vice versa. Two element-based rules were tried first
  // and both fail on a patch SMALLER than the background element, which is the
  // normal case for a keyway or a pad:
  //   * by centroid: a top-face triangle spanning a 12 mm seat on an 8 mm mesh
  //     has one vertex in the core and its element's mass 10 mm away, so nothing
  //     is ever marked and the interface stays at 8 mm (measured: 7.2 mm left
  //     after the waves gave up);
  //   * by closest vertex: the whole bulk tet is then asked to become 0.5 mm
  //     because one corner touches the seat — measured, a 21.5 GB solve.
  // Marking the OWNERS of coarse interface faces refines exactly the layer that
  // carries the interface and lets LEPP closure grade the rest, and it makes the
  // loop's own termination test identical to the contract's.
  static constexpr int kFaceCorners[4][3] = {
      {0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
  // Three jobs, one boundary pass:
  //   * the DELIVERED size, in the same currency fea_contact's guard measures —
  //     the guard's `interface_h_mm` is the MEDIAN over candidate pairs of the
  //     mean boundary-face edge length incident on the pair's nodes, so this
  //     returns the 90th percentile of that per-node mean over the core's
  //     boundary nodes: strictly more conservative than the guard's median, and
  //     immune to a single face the smoothing pass stretched. A raw MAX was
  //     tried first and is not usable: pulling one fill node back onto the CAD
  //     surface lengthens its incident faces, so the max reported 0.601 mm for
  //     a keyway interface the guard measured at 0.251 mm, and the contract
  //     "missed" by 70% on a mesh that was fine — which then bought a
  //     2.7M-element whole-solid re-fill for nothing;
  //   * the elements owning core faces the DELIVERED size is still failing on
  //     (the marks) — in that same per-node currency, never a per-face maximum.
  //     A max was tried and costs whole waves: the fidelity round stretches
  //     individual faces by pulling one node back onto the CAD surface, and
  //     LEB's own closure leaves long diagonals, so on the pin-in-bore ctest
  //     the last three waves were chasing a handful of edges the measure had
  //     already passed — 11 waves and a delivered 0.264 mm for a 0.35 mm
  //     request, i.e. one whole extra halving of a mesh that was already
  //     inside the contract (measured: 225 186 elements and 57 s of fill).
  //     Marking every face incident on a node whose OWN mean is still coarse
  //     is strictly conservative against the guard — the guard reads a MEDIAN
  //     over pairs, this chases every node — and it makes the loop's
  //     termination test identical to the contract's, which is the invariant
  //     the two-statistic version silently broke.
  //   * the core's boundary nodes, which the fidelity round slides onto the CAD
  //     surface and the residual is sampled over.
  // Boundary = a triangular face owned by exactly one element. The key packs
  // the sorted node triple in base 1000033, so it is a bijection, not a hash.
  struct BoundaryFace {
    std::array<std::uint32_t, 3> tri{};
    std::size_t owner = 0;
    int uses = 0;
  };
  struct NodeEdges {
    double sum = 0.0;
    int count = 0;
  };
  auto scan = [&](std::vector<std::size_t> *owners,
                  std::vector<std::uint32_t> *core_nodes = nullptr) {
    std::unordered_map<std::uint64_t, BoundaryFace> faces;
    faces.reserve(nodal.elements.size() * 2);
    for (std::size_t e = 0; e < nodal.elements.size(); ++e)
      for (const auto &corners : kFaceCorners) {
        std::array<std::uint32_t, 3> tri{
            nodal.elements[e].nodes[static_cast<std::size_t>(corners[0])],
            nodal.elements[e].nodes[static_cast<std::size_t>(corners[1])],
            nodal.elements[e].nodes[static_cast<std::size_t>(corners[2])]};
        std::sort(tri.begin(), tri.end());
        const std::uint64_t key = (static_cast<std::uint64_t>(tri[0]) * 1000003ull +
                                   tri[1]) * 1000033ull + tri[2];
        auto it = faces.find(key);
        if (it == faces.end())
          faces.emplace(key, BoundaryFace{tri, e, 1});
        else
          ++it->second.uses;
      }
    // The core's own boundary faces, kept for the marking pass below: the
    // per-node means that pass needs are only complete once every face has
    // contributed its two incident edges.
    std::vector<BoundaryFace> core_faces;
    core_faces.reserve(faces.size() / 8 + 8);
    std::unordered_map<std::uint32_t, NodeEdges> per_node;
    per_node.reserve(faces.size() / 2 + 8);
    // Canonical face order also fixes the accumulation order of each node's
    // mean. Sorting owners later cannot undo rounding in those means.
    std::vector<const BoundaryFace *> boundary_faces;
    for (const auto &entry : faces)
      if (entry.second.uses == 1) boundary_faces.push_back(&entry.second);
    std::sort(boundary_faces.begin(), boundary_faces.end(),
              [](const BoundaryFace *a, const BoundaryFace *b) {
                return a->tri < b->tri;
              });
    for (const BoundaryFace *boundary_face : boundary_faces) {
      const BoundaryFace &face = *boundary_face;
      std::array<double, 3> length{};
      std::array<double, 3> distance{};
      double nearest = std::numeric_limits<double>::infinity();
      for (int a = 0; a < 3; ++a) {
        const std::size_t at = static_cast<std::size_t>(a);
        const Eigen::Vector3d &p = nodal.nodes[face.tri[at]];
        length[at] =
            (p - nodal.nodes[face.tri[static_cast<std::size_t>((a + 1) % 3)]]).norm();
        distance[at] = seed_distance(p);
        nearest = std::min(nearest, distance[at]);
      }
      // Inside the core — the same zone the request is flat over, so what is
      // measured is exactly what was asked for.
      if (nearest > core_mm) continue;
      core_faces.push_back(face);
      if (core_nodes != nullptr)
        core_nodes->insert(core_nodes->end(), face.tri.begin(), face.tri.end());
      for (int a = 0; a < 3; ++a) {
        const std::size_t at = static_cast<std::size_t>(a);
        if (distance[at] > core_mm) continue;
        // The two face edges incident on this vertex.
        NodeEdges &node = per_node[face.tri[at]];
        node.sum += length[at] + length[static_cast<std::size_t>((a + 2) % 3)];
        node.count += 2;
      }
    }
    // ── The marks, in the currency the contract is measured in ───────────────
    // Every core face incident on a node whose own mean boundary-edge length is
    // still coarser than the request. When no node is, `owners` comes back
    // empty and the wave loop stops: the delivered size IS the request, and a
    // further wave would only halve a mesh that already satisfies the contract.
    const double landed = request.size_mm * 1.02;
    if (owners != nullptr)
      for (const BoundaryFace &face : core_faces)
        for (const std::uint32_t node : face.tri) {
          const auto it = per_node.find(node);
          if (it == per_node.end() || it->second.count <= 0) continue;
          if (it->second.sum / static_cast<double>(it->second.count) <= landed)
            continue;
          owners->push_back(face.owner);
          break;
        }
    if (per_node.empty()) return 0.0;
    std::vector<double> node_h;
    node_h.reserve(per_node.size());
    for (const auto &entry : per_node)
      if (entry.second.count > 0)
        node_h.push_back(entry.second.sum / static_cast<double>(entry.second.count));
    if (node_h.empty()) return 0.0;
    const std::size_t at = std::min(node_h.size() - 1, (node_h.size() * 9) / 10);
    std::nth_element(node_h.begin(),
                     node_h.begin() + static_cast<std::ptrdiff_t>(at),
                     node_h.end());
    return node_h[at];
  };
  // ── The fidelity round: put interface nodes back on the CAD surface ─────────
  // The lattice fill snaps its boundary nodes toward the surface but leaves a
  // residual — 1.21 mm max at the 13.94 mm background the governor picks for the
  // live keyway isolate, per the fill's own note. Bisection inherits it exactly:
  // each new mid-edge node is projected onto the surface while its parents stay
  // where the lattice left them, so the refined bore alternates between
  // on-surface nodes and off-surface ones. On a CLEARANCE FIT that is not
  // cosmetic — it IS the clearance: on the keyway isolate the coarse mesh's
  // closest approach across the 0.35 mm fit measured -0.2686 mm, a quarter
  // millimetre of PHANTOM interference, all of it discretization, and the
  // contact solve saw it as the real seating.
  //
  // It runs ONCE, after the last wave, and deliberately not before: on the
  // coarse mesh a node's shortest incident edge is a whole background cell, so
  // the same accept test that is conservative on a 0.3 mm star lets a coarse one
  // move millimetres and distort the very faces the waves are about to bisect.
  // Measured on the keyway isolate — a pre-wave round left the refined interface
  // residual at 0.484 mm max / 3.5e-3 mm mean and the closest approach at
  // -0.248 mm; this round alone leaves 0.034 mm max / 8.0e-5 mm mean and
  // -0.0198 mm.
  // Returns the delivered size it measured BEFORE moving anything — the
  // baseline the re-split test below compares against, at no extra scan.
  const auto fidelity_round = [&](std::vector<std::uint32_t> movable) {
    std::vector<std::uint32_t> interface_nodes;
    const double achieved = scan(nullptr, &interface_nodes);
    movable.insert(movable.end(), interface_nodes.begin(), interface_nodes.end());
    smooth_nodes(cad_surface, nodal,
                 build_star_topology(nodal, std::move(movable)),
                 contract.reprojected_nodes, contract.smoothed_nodes);
    return achieved;
  };
  // The node index above which every node was created by a wave — the whole
  // refined region's definition (refined_region), captured before the first
  // one runs.
  const std::size_t nodes_before_waves = nodal.nodes.size();
  // The longest boundary edge a collapse may leave, by position: `scan`'s own
  // marking threshold inside the zone the delivered size is measured over, and
  // unbounded outside it. The zone is the core the request is flat across plus
  // two interface elements, because a per-node mean also counts the faces that
  // REACH into the core from just outside it. Keeping every new boundary edge
  // under the threshold is what makes a collapse unable to re-trigger a wave
  // (see collapse_slivers).
  const std::function<double(const Eigen::Vector3d &)> boundary_edge_limit =
      [&](const Eigen::Vector3d &p) {
        return seed_distance(p) <= core_mm + 2.0 * request.size_mm
                   ? request.size_mm * 1.02
                   : std::numeric_limits<double>::infinity();
      };
  // ── One bisection wave: guards, cleanup and census ──────────────────────────
  // Shared by the wave loop and by the single re-split the quality pass may owe
  // the contract, so the two cannot disagree about what a wave is or about what
  // has to happen to the mesh afterwards.
  //
  // "Damage" is measured against what the pipeline can actually solve
  // (kWaveDegenerateFloor), not against the ship-quality floor. The projection
  // of a mid-edge node onto the CAD surface can still fold a child — that is
  // what killed the live keyway ("element 113043 is degenerate, inverted, or
  // has a sign-changing Jacobian") — but the fold is now gated per EDGE inside
  // local_refine_tets by the chord-sag test, so this is a backstop rather than
  // the main defence, and the plain-chord retry stays.
  const auto run_wave = [&](const std::vector<std::size_t> &marks,
                            std::string &wave_error) -> bool {
    const std::size_t before = nodal.elements.size();
    const std::size_t nodes_before = nodal.nodes.size();
    std::vector<std::array<std::uint32_t, 4>> tets;
    tets.reserve(nodal.elements.size());
    for (const pm::fea::NodalElement &element : nodal.elements)
      tets.push_back({element.nodes[0], element.nodes[1], element.nodes[2],
                      element.nodes[3]});
    pm::mesh::TetFillOutput refined;
    pm::mesh::LocalRefineStats stats;
    bool ok = false;
    for (int attempt = 0; attempt < 2 && !ok; ++attempt) {
      std::vector<std::array<std::uint32_t, 4>> wave_tets = tets;
      pm::mesh::LocalRefineStats attempt_stats;
      try {
        refined = pm::mesh::local_refine_tets(
            nodal.nodes, std::move(wave_tets), marks, &attempt_stats,
            attempt == 0 ? cad_surface : nullptr, /*mirror=*/nullptr,
            kMaxProjectionSagFraction);
      } catch (const std::exception &e) {
        wave_error = in_model_units(e.what());
        continue;
      }
      ok = true;
      double worst = std::numeric_limits<double>::infinity();
      for (const auto &tet : refined.tets)
        worst = std::min(worst, pm::mesh::validity::tet_shape_quality(
                                    refined.nodes[tet[0]], refined.nodes[tet[1]],
                                    refined.nodes[tet[2]], refined.nodes[tet[3]]));
      if (!std::isfinite(worst) || worst < kWaveDegenerateFloor) {
        ok = false;
        char text[192];
        std::snprintf(text, sizeof(text),
                      "a bisected element came out degenerate (shape quality "
                      "%.3g against a floor of %.3g)",
                      worst, kWaveDegenerateFloor);
        wave_error = text;
        continue;
      }
      stats = attempt_stats;
    }
    if (!ok)
      return false;
    if (refined.tets.size() <= before) {
      wave_error = "a bisection wave over " + std::to_string(marks.size()) +
                   " marked element(s) grew the mesh by nothing";
      return false;
    }
    nodal.nodes = std::move(refined.nodes);
    nodal.elements.clear();
    nodal.elements.reserve(refined.tets.size());
    for (const auto &tet : refined.tets)
      nodal.elements.push_back(pm::fea::NodalElement{
          pm::fea::ElementType::kTet4, {tet[0], tet[1], tet[2], tet[3]}});
    contract.elems_added += static_cast<int>(refined.tets.size() - before);
    contract.surface_mids += stats.n_surface_mids;
    contract.chord_mids += stats.n_chord_mids;
    ++contract.waves;
    // The census, and the region this wave's pass works on: the WHOLE refined
    // region as it now stands, not just the cells this wave cut. The two
    // differ exactly where the defect lives — the transition layer between one
    // wave's shell and the next's belongs to neither wave alone — and a
    // `*_before` measured over a different region from `*_after` is not a
    // before/after at all (measured on the pin-in-bore probe: 0.206 against
    // 0.032, two regions, read as a regression).
    //
    // It is taken here, straight out of the bisection and before any node has
    // moved, and it keeps the WORST any wave left rather than the last one's:
    // the last wave of a converged run exits a region its predecessors' passes
    // already cleaned, so overwriting reported `before == after` on a run
    // whose first wave had left 40 cells under the ship floor. What the packet
    // is being asked is what conforming bisection COSTS, and that is the
    // worst it ever cost.
    std::vector<char> in_region = refined_region(nodal, nodes_before_waves);
    const RegionQuality raw = region_quality(nodal, in_region);
    contract.jacobian_min_before =
        contract.waves == 1
            ? raw.jacobian_min
            : std::min(contract.jacobian_min_before, raw.jacobian_min);
    contract.aspect_max_before =
        std::max(contract.aspect_max_before, raw.aspect_max);
    contract.cells_below_floor_before =
        std::max(contract.cells_below_floor_before, raw.below_floor);
    // The wave's own nodes, straight away: a chord midpoint left in the void
    // of a bore is a wall bulge, and the NEXT wave measures its faces against
    // the request, so the fidelity move has to happen before that measurement,
    // not once at the end.
    std::vector<std::uint32_t> fresh;
    fresh.reserve(nodal.nodes.size() - nodes_before);
    for (std::size_t n = nodes_before; n < nodal.nodes.size(); ++n)
      fresh.push_back(static_cast<std::uint32_t>(n));
    smooth_nodes(cad_surface, nodal, build_star_topology(nodal, fresh),
                 contract.reprojected_nodes, contract.smoothed_nodes);
    quality_pass(cad_surface, nodal, boundary_edge_limit, in_region, contract);
    return true;
  };
  contract.achieved_mm = scan(nullptr);
  for (int wave = 0; wave < kLebWaves; ++wave) {
    report_fill_phase("interface band LEB waves", wave + 1, kLebWaves);
    // Two mark sources, unioned: the owners of the interface faces the
    // DELIVERED size is still failing on (the layer that carries the contract,
    // marked in the contract's own per-node currency) and any element coarser
    // than the graded field asks for at its centroid (the transition out to the
    // background, so the fine layer is not a cliff).
    std::vector<std::size_t> marks;
    scan(&marks);
    for (std::size_t e = 0; e < nodal.elements.size(); ++e) {
      const double distance = seed_distance(centroid_of(nodal.elements[e]));
      const bool outside = distance >= contract.transition_depth_mm;
      if (outside) {
        ++contract.outside_transition_checks;
        continue;
      }
      // 1.02: land just under the target rather than oscillating on it.
      if (longest_edge_mm(nodal, nodal.elements[e]) > want(distance) * 1.02) {
        marks.push_back(e);
        ++contract.transition_marks;
        if (outside) ++contract.outside_transition_marks;
      }
    }
    std::sort(marks.begin(), marks.end());
    marks.erase(std::unique(marks.begin(), marks.end()), marks.end());
    if (marks.empty())
      break;
    // A bisection wave at most DOUBLES the marked elements, so the next mesh is
    // bounded before it is built. Refuse the wave rather than the machine.
    if (contract.element_ceiling > 0 &&
        nodal.elements.size() + marks.size() > contract.element_ceiling) {
      contract.ceiling_bound = true;
      break;
    }
    std::string wave_error;
    if (!run_wave(marks, wave_error)) {
      contract.note = "local refinement stopped after " +
                      std::to_string(contract.waves) + " LEB wave(s): " +
                      wave_error;
      break;
    }
  }
  // The per-wave rounds only move what THAT wave created. Two things are left:
  // the interface nodes the FILL placed, which still carry its snap residual,
  // and a sliver between a bisected cell and an original fill cell, which needs
  // the ORIGINAL node to move and has had no chance to. One round covers both —
  // the core's boundary nodes unioned with the corners of every cell still under
  // the ship floor — and every move is accepted only on a strictly improving
  // star (or, for the boundary projection, on not trading a healthy star for a
  // sick one).
  if (contract.waves > 0) {
    const double achieved_before_pass =
        fidelity_round(nodes_of_poor_cells(nodal));
    // ── Convergence over the WHOLE refined region ───────────────────────────
    // A fixed five-pass cap can stop before the floors are met: the macOS
    // pin-in-bore run reported two sub-floor cells after 14 total quality passes.
    // Continue while the deficient quality multiset improves, using the same
    // relative resolution as the connectivity edits. Qualities at/above the
    // target are irrelevant: fidelity may spend that surplus without hurting
    // the contract. Measure mesh-wide because a movable star reaches outside
    // the region, and progress in a tied neighbour can unlock the next repair.
    // A connectivity edit also earns another pass: rebuilding the stars can
    // unlock a previously blocked move even when the deficient qualities tie.
    const auto deficient_qualities = [&](std::vector<double> &qualities) {
      qualities.clear();
      for (const auto &element : nodal.elements) {
        if (!is_tet4(element)) continue;
        const double quality = tet_quality_of(nodal, cell_nodes(element));
        if (quality < kQualityTarget) qualities.push_back(quality);
      }
      std::sort(qualities.begin(), qualities.end());
    };
    std::vector<char> in_region = refined_region(nodal, nodes_before_waves);
    std::vector<double> qualities_before, qualities_after;
    const auto converge_quality = [&](std::vector<char> &region) {
      if (region_quality(nodal, region).met()) return;
      deficient_qualities(qualities_before);
      for (int pass = 0; !region_quality(nodal, region).met(); ++pass) {
        report_fill_phase("interface band quality pass", pass + 1);
        const bool retiled =
            quality_pass(cad_surface, nodal, boundary_edge_limit, region, contract);
        deficient_qualities(qualities_after);
        if (!retiled && !sorted_qualities_better(qualities_before, qualities_after))
          break;  // numerical stationarity, not an arbitrary pass budget
        qualities_before.swap(qualities_after);
      }
    };
    converge_quality(in_region);
    // ── The delivery is RE-MEASURED, and re-split ONCE if the pass lost it ──
    // Smoothing moves nodes, and a node pulled back onto the CAD surface
    // lengthens the interface faces the contract is measured on. A delivered
    // size nobody re-measured after the pass is not a measurement — so the
    // scan that produced the marks is the same one that judges the delivery,
    // and if the pass coarsened it past the request one more wave is run over
    // exactly those marks. Only once: a second round would be the wave loop
    // again, and the loop already stopped for a reason.
    std::vector<std::size_t> marks;
    const double achieved_after_pass = scan(&marks);
    if (achieved_after_pass > request.size_mm * 1.02 &&
        achieved_after_pass > achieved_before_pass && !marks.empty() &&
        (contract.element_ceiling == 0 ||
         nodal.elements.size() + marks.size() <= contract.element_ceiling)) {
      std::sort(marks.begin(), marks.end());
      marks.erase(std::unique(marks.begin(), marks.end()), marks.end());
      std::string wave_error;
      if (run_wave(marks, wave_error)) {
        contract.resplit = true;
        in_region = refined_region(nodal, nodes_before_waves);
        converge_quality(in_region);
      }
    }
    const RegionQuality after = region_quality(nodal, in_region);
    contract.jacobian_min_after = after.jacobian_min;
    contract.aspect_max_after = after.aspect_max;
    contract.cells_below_floor_after = after.below_floor;
  }
  contract.elems = nodal.elements.size();
  std::vector<std::uint32_t> core_nodes;
  contract.achieved_mm = scan(nullptr, &core_nodes);
  // ── Measured SURFACE FIDELITY at the interface ──────────────────────────────
  // On a clearance fit the boundary residual is not cosmetic: a chord midpoint
  // the curvature gate declined sits a chord sag INSIDE the void of the bore,
  // and if the smoothing pass could not put it back, that bulge is subtracted
  // from the clearance the contact solve is trying to resolve — a 0.1 mm
  // residual on a 0.35 mm fit turns a seating patch into a handful of touching
  // asperities. So it is measured and reported next to the delivered size.
  // Sampled: closest_on_surface is brute force over the tessellation.
  if (cad_surface != nullptr && !core_nodes.empty()) {
    std::sort(core_nodes.begin(), core_nodes.end());
    core_nodes.erase(std::unique(core_nodes.begin(), core_nodes.end()),
                     core_nodes.end());
    constexpr std::size_t kResidualSamples = 2048;
    const std::size_t stride =
        std::max<std::size_t>(1, core_nodes.size() / kResidualSamples);
    double sum = 0.0;
    std::size_t sampled = 0;
    for (std::size_t i = 0; i < core_nodes.size(); i += stride) {
      const double distance =
          pm::mesh::closest_on_surface(*cad_surface, nodal.nodes[core_nodes[i]])
              .distance;
      if (!std::isfinite(distance)) continue;
      contract.max_residual_mm = std::max(contract.max_residual_mm, distance);
      sum += distance;
      ++sampled;
    }
    if (sampled > 0) contract.mean_residual_mm = sum / static_cast<double>(sampled);
  }
  // What the refinement left behind, in the pipeline's own currency: the worst
  // cell and how many sit under polymesh's ship floor. Reported, not fatal —
  // the alternative (refusing the study) is what stalled the keyway at 2.434 mm.
  double worst = std::numeric_limits<double>::infinity();
  for (const pm::fea::NodalElement &element : nodal.elements) {
    if (element.type != pm::fea::ElementType::kTet4) continue;
    const double quality = pm::mesh::validity::tet_shape_quality(
        nodal.nodes[element.nodes[0]], nodal.nodes[element.nodes[1]],
        nodal.nodes[element.nodes[2]], nodal.nodes[element.nodes[3]]);
    worst = std::min(worst, quality);
    if (quality < pm::mesh::validity::kCellShapeFloor) ++contract.below_shape_floor;
  }
  contract.worst_quality = std::isfinite(worst) ? worst : 0.0;
}

// Relabel every left-handed tet4 cell so its node order is right-handed, and
// report how many needed it.
//
// Chudware's tet4 contract carries no orientation: fea_static's tet4_B,
// fea_thermal's tet4_grad and fill_quality all take |det|, so a cell whose
// four nodes are listed left-handed (negative signed volume) solves and
// measures exactly like its mirror. The quadratic PROMOTION is not
// orientation-blind: pm::fea::promote_to_quadratic admits an element only if
// the tet10 Jacobian determinant is finite and POSITIVE at every Gauss point,
// and with mid-edge nodes at the straight chord midpoints that determinant IS
// 6*V_signed. So it silently declined exactly the left-handed cells, left them
// kTet4 inside an otherwise kTet10 mesh, and the fixed-stride extract below
// then refused the entire study with "unsupported tet4 connectivity" — on a
// perfectly valid solid. Measured on a rover wheel-disc 90 deg cyclic
// sector at fineness 0.5: 33 left-handed cells out
// of 467162, all against the wedge's sharp radial faces, where the graded
// fill's boundary snapping flips a lattice tet. Order 1 never noticed; order 2
// lost the study.
//
// Swapping two corners is a pure relabelling — same four nodes, same region,
// same |volume|, and `faces` is empty for every FEM element type (it exists
// only for kPolyVem), so nothing else indexes into the node list. A cell that
// is FLAT rather than mirrored keeps a non-positive Jacobian either way; that
// one is repair_degenerate_tets' job.
int orient_tets_right_handed(pm::fea::NodalMesh &nodal) {
  int flipped = 0;
  for (pm::fea::NodalElement &element : nodal.elements) {
    if (element.type != pm::fea::ElementType::kTet4 ||
        element.nodes.size() != 4)
      continue;
    const double volume = pm::mesh::validity::tet_signed_volume(
        nodal.nodes[element.nodes[0]], nodal.nodes[element.nodes[1]],
        nodal.nodes[element.nodes[2]], nodal.nodes[element.nodes[3]]);
    if (volume < 0.0) {
      std::swap(element.nodes[1], element.nodes[2]);
      ++flipped;
    }
  }
  return flipped;
}

// Cells this sweep touches: flat to within a millionth of a regular tet of the
// same edge span. polymesh's own ship floor is 0.02 (mesh::validity::
// kCellShapeFloor), so nothing an ordinary "poor" mesh contains is in range —
// only cells whose volume is numerical noise.
inline constexpr double kDegenerateQuality = 1e-6;
// What a repaired cell must reach: still four orders below the ship floor, and
// nine orders above the rounding noise of the promotion's own determinant, so
// its sign is no longer a coin flip.
inline constexpr double kRepairedQuality = 1e-4;
// Hard bound on how far one node may move, as a fraction of the background
// element size. The same fill's boundary snap moves nodes by up to 0.18*h on
// this geometry, so this is an order of magnitude inside the placement error
// the mesh already carries.
inline constexpr double kMaxRepairMoveFrac = 0.05;

// Inflate the degenerate fill cells polymesh's ship gate could not fix.
//
// The ship gate (pipeline/scene.cpp, relax_cells_below_shape_floor) relaxes a
// sub-floor cell by moving its INTERIOR nodes only — a boundary node carries
// the exact-BRep placement. In a thin web every node of a boundary sliver IS a
// boundary node, so the gate has nothing to move, ships the cell, and says so
// in its note ("ship_gate N non-integrable cells (det J <= 0 at a quadrature
// point)"). Measured on a rover wheel-disc 90 deg sector at h_bulk
// 8.898 mm: 29 of 152591 cells at shape quality 7e-17 and signed volume
// 2e-15 mm3 — flat to machine precision, on the wedge's sharp radial faces,
// in a fill whose own boundary snap already moved nodes by up to 1.59 mm.
//
// A zero-volume tet is not a mesh element: tet4_B inverts its coordinate
// matrix, so at order 1 it contributes noise stiffness, and at order 2 it has
// no positive Jacobian at all, which is what made promote_to_quadratic decline
// it and cost the whole study its order (see build_mesh's refusal below).
//
// The repair is the ship gate's move, with its boundary restriction traded for
// a hard displacement bound: give the cell volume by moving ONE of its nodes
// along the gradient of its own signed volume (exact — the volume is linear in
// each node coordinate), by the smallest distance that reaches
// kRepairedQuality, never further than kMaxRepairMoveFrac * h; accept only if
// every cell in that node's star keeps a positive volume and the star's worst
// shape quality strictly improves. Returns the number of cells repaired and
// reports what is left in `remaining` — a cell no single-node move can save is
// a refusal, never a silent pass.
int repair_degenerate_tets(pm::fea::NodalMesh &nodal, double h,
                           int &remaining) {
  remaining = 0;
  const auto quality_of = [&nodal](const pm::fea::NodalElement &element) {
    return pm::mesh::validity::tet_shape_quality(
        nodal.nodes[element.nodes[0]], nodal.nodes[element.nodes[1]],
        nodal.nodes[element.nodes[2]], nodal.nodes[element.nodes[3]]);
  };
  const auto is_tet4 = [](const pm::fea::NodalElement &element) {
    return element.type == pm::fea::ElementType::kTet4 &&
           element.nodes.size() == 4;
  };

  std::vector<size_t> bad;
  for (size_t e = 0; e < nodal.elements.size(); ++e)
    if (is_tet4(nodal.elements[e]) &&
        quality_of(nodal.elements[e]) < kDegenerateQuality)
      bad.push_back(e);
  if (bad.empty())
    return 0;

  // Element star of every node a bad cell touches — the only nodes that can
  // move, so the incidence lists are built for those alone.
  std::unordered_map<uint32_t, std::vector<uint32_t>> star;
  star.reserve(bad.size() * 8);
  for (const size_t e : bad)
    for (const uint32_t node : nodal.elements[e].nodes)
      star.try_emplace(node);
  for (size_t e = 0; e < nodal.elements.size(); ++e)
    for (const uint32_t node : nodal.elements[e].nodes) {
      const auto it = star.find(node);
      if (it != star.end())
        it->second.push_back(static_cast<uint32_t>(e));
    }

  const double max_move = kMaxRepairMoveFrac * h;
  int repaired = 0;
  for (const size_t e : bad) {
    const pm::fea::NodalElement &cell = nodal.elements[e];
    if (quality_of(cell) >= kDegenerateQuality) {
      ++repaired; // an earlier node move already inflated this one
      continue;
    }
    const double span = pm::mesh::validity::max_edge(
        nodal.nodes[cell.nodes[0]], nodal.nodes[cell.nodes[1]],
        nodal.nodes[cell.nodes[2]], nodal.nodes[cell.nodes[3]]);
    if (!(span > 0.0))
      continue; // four coincident nodes: no move can give this cell volume
    // Volume the cell needs for kRepairedQuality at its own edge span.
    const double want_volume =
        kRepairedQuality * span * span * span / 8.485281374238570;
    bool fixed = false;
    for (int corner = 0; corner < 4 && !fixed; ++corner) {
      const uint32_t node = cell.nodes[static_cast<size_t>(corner)];
      const std::vector<uint32_t> &incident = star.at(node);
      const Eigen::Vector3d original = nodal.nodes[node];
      const auto cell_volume = [&] {
        return pm::mesh::validity::tet_signed_volume(
            nodal.nodes[cell.nodes[0]], nodal.nodes[cell.nodes[1]],
            nodal.nodes[cell.nodes[2]], nodal.nodes[cell.nodes[3]]);
      };
      // The signed volume is linear in each node coordinate, so a unit step
      // per axis IS the exact gradient.
      const double base = cell_volume();
      Eigen::Vector3d gradient;
      for (int axis = 0; axis < 3; ++axis) {
        nodal.nodes[node] = original;
        nodal.nodes[node][axis] += 1.0;
        gradient[axis] = cell_volume() - base;
      }
      nodal.nodes[node] = original;
      const double slope = gradient.norm();
      if (!(slope > 0.0))
        continue;
      const double distance = (want_volume - base) / slope;
      if (!(distance > 0.0) || distance > max_move)
        continue;
      double worst_before = std::numeric_limits<double>::infinity();
      for (const uint32_t other : incident)
        if (is_tet4(nodal.elements[other]))
          worst_before =
              std::min(worst_before, quality_of(nodal.elements[other]));
      nodal.nodes[node] = original + gradient.normalized() * distance;
      double worst_after = std::numeric_limits<double>::infinity();
      bool star_valid = true;
      for (const uint32_t other : incident) {
        if (!is_tet4(nodal.elements[other]))
          continue;
        const double quality = quality_of(nodal.elements[other]);
        if (!std::isfinite(quality) || !(quality > 0.0))
          star_valid = false;
        worst_after = std::min(worst_after, quality);
      }
      if (star_valid && worst_after > worst_before) {
        fixed = true;
        ++repaired;
      } else {
        nodal.nodes[node] = original;
      }
    }
    if (!fixed)
      ++remaining;
  }
  return repaired;
}


// Fill at coarse element size `h`, optionally under a spatial size field h(x),
// promote to tet10 when asked, and normalize into MeshOut. `feature_refine` is
// an explicit caller decision rather than a re-derivation of the fineness gate,
// because the very same `h` can fill with the pass on and be refused with it
// off. `nodal` keeps the polymesh-side mesh, which the adaptive driver needs
// for error recovery (the MeshOut node ORDER is identical, so a displacement
// vector indexed against one is valid against the other).
bool build_mesh(const LoadedModel &loaded, const MeshOptions &options, double h,
                bool feature_refine, const pm::mesh::SizeFieldFn &size_field,
                pm::fea::NodalMesh &nodal, MeshOut &out) {
  // The element/DOF ceilings are the size band's governor-derived numbers, not
  // polymesh's 0.59M default: see mesh_size_band.cpp. 0 would select the
  // default, so a band that failed to compute them still behaves as before.
  const pm::pipeline::ResolvedMeshSize resolved = pm::pipeline::resolve_mesh_size(
      loaded.model, h, /*sharp_angle_deg=*/30.0,
      static_cast<std::size_t>(loaded.band.element_ceiling),
      static_cast<std::size_t>(loaded.band.dof_ceiling));

  std::vector<Eigen::Vector3d> refine_seeds;
  double seed_band = 0.0;
  const pm::mesh::SizeFieldFn effective_field =
      local_size_field(options, h, size_field, refine_seeds, seed_band);

  // Graded tet is the one Polyhedral-FEA family that matches Chudware's
  // fixed-stride tet solve contract and accepts the spatial size field used by
  // adaptive refinement.
  pm::mesh::FillOptions fill_options;
  if (progress_detail::sink) {
    fill_options.on_progress = [&](const pm::mesh::FillProgress &progress) {
      report_mesh_progress(options.local_refine.active() ? "mesh_interface_band_fill"
                                                        : "mesh_background_probe",
                           h, progress.elements_so_far, progress.cells_done,
                           progress.cells_total, progress.sub_phase);
    };
  }
  const pm::pipeline::VolumeMeshOutput volume = [&] {
    chudware::prof::phases::PhaseScope phase("generate");
    if (options.local_refine.active())
      report_progress("mesh_interface_band_fill");
    return pm::pipeline::volume_mesh(
        loaded.model, h, pm::pipeline::VolumeMesher::kGradedTet,
        /*skin_layers=*/2,
        // The graded skin stays active regardless; the extra
        // sharp-edge/thickness pass is the caller's choice.
        feature_refine, refine_seeds, seed_band,
        /*element_tendency=*/0.0, resolved.element_ceiling, resolved.dof_ceiling,
        /*auto_retry_budget=*/0, /*cancel_check=*/{}, effective_field,
        /*on_stage=*/{}, fill_options);
  }();

  nodal = volume.mesh;
  pm::mesh::FillProgressScope delivery_progress(fill_options);
  delivery_progress.set_elements(nodal.elements.size());
  delivery_progress.set_cells(0, nodal.elements.size());
  delivery_progress.set_phase(options.local_refine.active() ? "interface band" : "quality");
  // The local size contract, before any promotion: LEB is tet4-only, and a
  // tet10 mesh promoted from bisected tet4 is the same mesh one order up.
  LocalContract contract;
  if (options.local_refine.active())
    report_progress("mesh_interface_band_refine", nodal.elements.size());
  deliver_local_contract(loaded, options, h, nodal, contract);
  if (options.local_refine.active())
    report_progress("mesh_interface_band_extract", nodal.elements.size());
  // Right-handed before anything reads the sign: the quadratic promotion
  // refuses a left-handed cell, and the fill emits a few on sharp edges.
  out.reoriented_elems = orient_tets_right_handed(nodal);
  const int global_collapses =
      collapse_global_slivers(&loaded.model.surface, nodal);
  if (global_collapses > 0)
    append_note(out.note, "global sub-0.034 Jacobian repair: " +
                             std::to_string(global_collapses) + " sliver collapses");
  // ...and non-degenerate before the promotion reads the determinant's sign:
  // a cell the fill shipped flat has no positive Jacobian to promote.
  out.degenerate_elems_repaired =
      repair_degenerate_tets(nodal, h, out.degenerate_elems_remaining);
  delivery_progress.set_elements(nodal.elements.size());
  delivery_progress.set_cells(0, nodal.elements.size());
  if (options.order == 2) {
    delivery_progress.set_phase("quadratic_promotion");
    nodal = pm::fea::promote_to_quadratic(nodal);
    // Project free-surface mids onto the exact BRep so quadratic elements
    // follow fillets, bores, and other curved faces instead of retaining
    // straight chords.
    if (loaded.model.cad) {
      std::vector<pm::mesh::BoundarySupport> provenance;
      pm::mesh::BoundaryProjectionContext projection;
      if (pm::pipeline::make_boundary_projection(*loaded.model.cad, h,
                                                 &projection, &provenance)) {
        pm::pipeline::project_quadratic_boundary_mids(nodal, *loaded.model.cad,
                                                      &projection, h);
      }
    }
  }
  delivery_progress.set_phase("mesh_extract");
  nodal.compact_unused_nodes();
  // The fill's own note goes in BEFORE the refusal gates below, so a refused
  // mesh still carries polymesh's ship-gate census (cells below the shape
  // floor, non-integrable cells) instead of only Chudware's verdict. Same
  // metre/millimetre relabel the success path uses (see in_model_units).
  append_note(out.note, in_model_units(volume.mesher_note));

  chudware::prof::phases::PhaseScope extract_phase("extract");
  const int stride = options.order == 2 ? 10 : 4;
  const pm::fea::ElementType want = options.order == 2
                                        ? pm::fea::ElementType::kTet10
                                        : pm::fea::ElementType::kTet4;
  // A mixed cell here would be silently misread as a tet by every downstream
  // reader (they index a flat array by a constant stride), so refuse instead
  // of truncating. Two unrelated failures wear that shape and get two
  // different diagnostics: an element FAMILY Chudware cannot solve (hex,
  // prism, pyramid, polyhedral), versus a LINEAR tet the quadratic promotion
  // declined. The second used to be reported as the first, which is how a
  // valid 90 deg wheel sector came back as "unsupported tet4 connectivity"
  // (see orient_tets_right_handed): after the orientation relabel only a
  // sliver whose signed volume is zero to tolerance can still be declined, and
  // that is a degenerate-mesh refusal, not an unsupported one.
  size_t declined = 0;
  const pm::fea::ElementType *foreign = nullptr;
  for (const pm::fea::NodalElement &element : nodal.elements) {
    if (element.type == want)
      continue;
    if (options.order == 2 && element.type == pm::fea::ElementType::kTet4)
      ++declined;
    else if (foreign == nullptr)
      foreign = &element.type;
  }
  if (foreign != nullptr) {
    out.ok = false;
    out.error = std::string("polyfea: mesh contains unsupported ") +
                pm::fea::element_type_name(*foreign) +
                " connectivity; Chudware studies currently require tet4 or "
                "tet10";
    return false;
  }
  if (declined > 0) {
    // Diagnose, don't guess: report the worst declined cell's signed volume,
    // shape quality and edge span so the refusal says which defect it is.
    double worst_quality = std::numeric_limits<double>::infinity();
    double worst_volume = 0.0;
    double worst_short = 0.0, worst_long = 0.0;
    for (const pm::fea::NodalElement &element : nodal.elements) {
      if (element.type != pm::fea::ElementType::kTet4 ||
          element.nodes.size() != 4)
        continue;
      const Eigen::Vector3d &a = nodal.nodes[element.nodes[0]];
      const Eigen::Vector3d &b = nodal.nodes[element.nodes[1]];
      const Eigen::Vector3d &c = nodal.nodes[element.nodes[2]];
      const Eigen::Vector3d &d = nodal.nodes[element.nodes[3]];
      const double quality = pm::mesh::validity::tet_shape_quality(a, b, c, d);
      if (quality >= worst_quality)
        continue;
      worst_quality = quality;
      worst_volume = pm::mesh::validity::tet_signed_volume(a, b, c, d);
      worst_long = pm::mesh::validity::max_edge(a, b, c, d);
      worst_short = std::min({(a - b).norm(), (a - c).norm(), (a - d).norm(),
                              (b - c).norm(), (b - d).norm(), (c - d).norm()});
    }
    out.ok = false;
    char text[576];
    std::snprintf(text, sizeof(text),
                  "polyfea: the quadratic promotion declined %zu of %zu "
                  "element(s); worst declined cell: shape quality %.3g, signed "
                  "volume %.3g mm3, edges %.4g..%.4g mm (the fill shipped %d "
                  "flat cell(s); %d were inflated inside the %.3g mm repair "
                  "bound, %d could not be)",
                  declined, nodal.elements.size(), worst_quality, worst_volume,
                  worst_short, worst_long,
                  out.degenerate_elems_repaired +
                      out.degenerate_elems_remaining,
                  out.degenerate_elems_repaired,
                  kMaxRepairMoveFrac * h, out.degenerate_elems_remaining);
    out.error = text;
    return false;
  }
  if (nodal.nodes.empty() || nodal.elements.empty()) {
    out.ok = false;
    out.error =
        "polymesh: fill produced an empty mesh (geometry not watertight?)";
    return false;
  }

  out.points.resize(nodal.nodes.size() * 3);
  for (size_t i = 0; i < nodal.nodes.size(); ++i) {
    out.points[3 * i + 0] = nodal.nodes[i].x();
    out.points[3 * i + 1] = nodal.nodes[i].y();
    out.points[3 * i + 2] = nodal.nodes[i].z();
  }
  out.tets.resize(nodal.elements.size() * static_cast<size_t>(stride));
  for (size_t e = 0; e < nodal.elements.size(); ++e) {
    const std::vector<uint32_t> &nodes = nodal.elements[e].nodes;
    if (nodes.size() != static_cast<size_t>(stride)) {
      out.ok = false;
      out.error =
          "polymesh: element node count does not match its element type";
      return false;
    }
    int *dst = out.tets.data() + e * static_cast<size_t>(stride);
    for (int c = 0; c < 4; ++c)
      dst[c] = static_cast<int>(nodes[static_cast<size_t>(c)]);
    for (int m = 0; m < stride - 4; ++m)
      dst[kTet10PolymeshToChudware[m]] =
          static_cast<int>(nodes[static_cast<size_t>(4 + m)]);
  }

  out.char_len_mm = h;
  out.size_band_applied_floor_mm = volume.size_floor;
  if (volume.size_floor > 0.0)
    out.size_band_reason = "cartesian_allocation_budget";
  if (h > loaded.band.geometry_base * (1.0 + 1e-9)) {
    out.size_band_applied_floor_mm = std::max(h, out.size_band_applied_floor_mm);
    out.size_band_reason = loaded.band.memory_coarsened ? "memory_admission" : "size_band_or_lattice_recovery";
  }
  out.n_nodes = static_cast<int>(nodal.nodes.size());
  out.n_elems = static_cast<int>(nodal.elements.size());
  out.nodes_per_elem = stride;
  out.mesh_type_used = stride == 10 ? "tet10" : "tet4";
  if (out.reoriented_elems > 0)
    append_note(out.note,
                std::to_string(out.reoriented_elems) +
                    " left-handed fill cell(s) relabelled right-handed (node "
                    "order only; same nodes, same volume)");
  if (out.degenerate_elems_repaired > 0) {
    char text[288];
    std::snprintf(text, sizeof(text),
                  "%d flat fill cell(s) inflated by one node each (within "
                  "%.3g mm; the fill's ship gate cannot move boundary nodes)",
                  out.degenerate_elems_repaired, kMaxRepairMoveFrac * h);
    append_note(out.note, text);
  }
  if (options.local_refine.active()) {
    // The MEASURED delivery, never the request: the seed stamp alone is capped
    // at the lattice's h/4, so a note quoting max(request, h/4) as "applied"
    // was how a 0.35 mm interface request came back as 1.299 mm without saying
    // so (see deliver_local_contract).
    out.local_refine_size_mm = options.local_refine.size_mm;
    out.local_refine_achieved_mm = contract.achieved_mm;
    out.local_refine_waves = contract.waves;
    out.local_refine_elems_added = contract.elems_added;
    out.local_refine_transition_depth_mm = contract.transition_depth_mm;
    out.local_refine_transition_marks = contract.transition_marks;
    out.local_refine_outside_transition_marks = contract.outside_transition_marks;
    out.local_refine_outside_transition_checks = contract.outside_transition_checks;
    out.local_refine_ceiling_bound = contract.ceiling_bound;
    out.local_refine_element_ceiling = contract.element_ceiling;
    out.local_refine_limit = contract.note;
    out.local_refine_smoothing_passes = contract.quality_passes;
    out.local_refine_jacobian_min_before = contract.jacobian_min_before;
    out.local_refine_jacobian_min_after = contract.jacobian_min_after;
    out.local_refine_aspect_max_before = contract.aspect_max_before;
    out.local_refine_aspect_max_after = contract.aspect_max_after;
    out.local_refine_cells_below_floor_before = contract.cells_below_floor_before;
    out.local_refine_cells_below_floor_after = contract.cells_below_floor_after;
    out.local_refine_resplit = contract.resplit;
    char text[608];
    std::snprintf(text, sizeof(text),
                  "local refinement for %s: %zu seed(s) requested %.4g mm, flat "
                  "within %.4g mm of them (seed stamp band %.4g mm, background "
                  "element size %.4g mm, the fill's own graded floor %.4g mm); %d "
                  "LEB wave(s) added %d element(s) and the element size delivered "
                  "there is %.4g mm (90th percentile of the mean boundary-face "
                  "edge length per interface node — the contact guard's own "
                  "statistic, one percentile stricter)",
                  options.local_refine.label.empty()
                      ? "a study request"
                      : options.local_refine.label.c_str(),
                  options.local_refine.points.size() / 3,
                  options.local_refine.size_mm, contract.core_mm, seed_band, h,
                  h * 0.25, contract.waves, contract.elems_added,
                  contract.achieved_mm);
    append_note(out.note, text);
    if (options.local_refine.max_depth_mm > 0.0) {
      char depth[192];
      std::snprintf(depth, sizeof(depth),
          "direct centroid grading depth %.4g mm (requested cap %.4g mm); "
          "conforming closure neighbours may extend beyond",
          contract.transition_depth_mm, options.local_refine.max_depth_mm);
      append_note(out.note, depth);
    }
    if (contract.waves > 0) {
      char placed[576];
      std::snprintf(placed, sizeof(placed),
                    "mid-edge nodes: %zu projected onto the CAD surface, %zu "
                    "kept on the chord by the curvature gate; the post-wave "
                    "smoothing pass slid %d back onto the face and improved %d "
                    "more for shape (worst cell quality %.3g, %zu under the "
                    "0.02 ship floor); interface boundary residual max %.4g mm, "
                    "mean %.4g mm",
                    contract.surface_mids, contract.chord_mids,
                    contract.reprojected_nodes, contract.smoothed_nodes,
                    contract.worst_quality, contract.below_shape_floor,
                    contract.max_residual_mm, contract.mean_residual_mm);
      append_note(out.note, placed);
      char pass[608];
      std::snprintf(pass, sizeof(pass),
                    "refined-region quality pass: %d pass(es) with %d 2-3 "
                    "flip(s), %d edge removal(s) and %d sliver collapse(s) took "
                    "jacobian_min %.4g -> %.4g, aspect_max %.4g -> %.4g and "
                    "cells under the 0.02 ship floor %zu -> %zu%s",
                    contract.quality_passes, contract.flips_23,
                    contract.edge_removals, contract.collapsed_slivers,
                    contract.jacobian_min_before, contract.jacobian_min_after,
                    contract.aspect_max_before, contract.aspect_max_after,
                    contract.cells_below_floor_before,
                    contract.cells_below_floor_after,
                    contract.resplit
                        ? "; the pass coarsened the interface and it was "
                          "re-marked and re-split once"
                        : "");
      append_note(out.note, pass);
    }
    if (!contract.note.empty())
      append_note(out.note, contract.note);
    if (contract.ceiling_bound) {
      char capped[384];
      std::snprintf(capped, sizeof(capped),
                    "local refinement stopped at the mesh element ceiling: "
                    "%zu element(s) against a ceiling of %zu, so the requested "
                    "%.4g mm was not reached (%.4g mm achieved)",
                    contract.elems, contract.element_ceiling,
                    options.local_refine.size_mm, contract.achieved_mm);
      append_note(out.note, capped);
    }
  }
  // Element-local straight-edge fallback for mid-edge projections that folded
  // their element. On a thin wall (element size comparable to the wall
  // thickness or the curvature radius) a handful of elements always fold, and
  // before this every one of them cost the whole study its quadratic order.
  // `nodal` is kept in sync: the adaptive driver solves against MeshOut but
  // recovers/refines through the polymesh mesh, and the two must describe the
  // same geometry.
  if (stride == 10 && detail::repair_folded_quadratic(out) > 0) {
    for (size_t i = 0; i < nodal.nodes.size(); ++i) {
      nodal.nodes[i].x() = out.points[3 * i + 0];
      nodal.nodes[i].y() = out.points[3 * i + 1];
      nodal.nodes[i].z() = out.points[3 * i + 2];
    }
    append_note(out.note,
                std::to_string(out.quadratic_fallback_elems) +
                    " tet10 element(s) kept straight mid-edge nodes (projected "
                    "mids folded them)");
  }
  // Hanging-element repair: a fragment attached to the body by one node (or one
  // edge) is a mechanism the connectivity checks cannot see, because it DOES reach
  // a fixture through that node. Runs after the fold repair so a straightened
  // element is judged on its final shape, and before fill_quality so the reported
  // metrics describe the mesh that will actually be solved.
  const int elems_before_repair = out.n_elems;
  if (detail::repair_lonely_nodes(out) > 0) {
    append_note(out.note,
                "dissolved " + std::to_string(out.hanging_elems_dropped) +
                    " hanging element(s) attached to the body by two corners or "
                    "fewer (they are rigid-body mechanisms, not stiffness), "
                    "clearing " + std::to_string(out.lonely_nodes_repaired) +
                    " single-element node(s) and " +
                    std::to_string(out.dropped_volume_mm3) + " mm^3 of sliver");
    // A dissolve is a REPAIR only while it stays a minority correction. Its
    // premise is that a fragment reaching the body through one node or edge is a
    // DEFECT — and a defect is rare: the wheel-radial case that motivated the
    // rule dropped 2 elements, and the sensor-node demo board drops 3 of 240,469 (0.001%).
    // When the same rule fires on most of the fill the premise is simply false
    // for that mesh, and "repairing" it means handing back a part that is no
    // longer the part: measured on this board at h=1.54 mm the pass dissolved
    // 52,013 of 52,045 elements and 3281.87 of 3282.13 mm^3 — 99.99% of the
    // volume — and every quality metric of the surviving 32 elements still read
    // clean. Refuse instead; the element size, not the mesh, is what is wrong,
    // and the retry ladder in mesh_loaded_model can move on.
    if (elems_before_repair > 0 &&
        out.hanging_elems_dropped * 10 > elems_before_repair) {
      char text[448];
      std::snprintf(
          text, sizeof(text),
          "mesh at h=%.4g mm is mostly disconnected fragments: the "
          "hanging-element repair would dissolve %d of %d elements (%.1f%% of "
          "the fill, %.4g mm^3), which is a bad element size rather than a "
          "repairable mesh — try a different fineness/target size",
          h, out.hanging_elems_dropped, elems_before_repair,
          100.0 * static_cast<double>(out.hanging_elems_dropped) /
              static_cast<double>(elems_before_repair),
          out.dropped_volume_mm3);
      out.ok = false;
      out.error = text;
      return false;
    }
  }
  // The same repair pass also dissolves sub-element DEBRIS islands — components
  // that share no node with the body, are under a millionth of its volume and
  // are smaller than one average element (mesh_quality.cpp
  // dissolve_debris_components). They are reported separately from the hanging
  // flaps because they are a different defect: a tangential rod junction
  // shedding cells, not a fragment hanging off an edge.
  if (out.debris_elems_dropped > 0) {
    char text[384];
    std::snprintf(text, sizeof(text),
                  "dissolved %d sub-element debris island(s) totalling %d "
                  "element(s) and %.6g mm^3 (each under a millionth of the "
                  "domain and smaller than one average element): they share no "
                  "node with the body, so each one was a rigid-body component",
                  out.debris_components_dissolved, out.debris_elems_dropped,
                  out.debris_volume_mm3);
    append_note(out.note, text);
  }
  // Element sizes read off corner nodes only, so the mid-edge fallback above
  // cannot move this number.
  out.thin_wall_ratio = thin_wall_ratio(loaded.model, out, h, out.min_wall_mm);
  if (out.thin_wall_ratio > 0.0 && out.thin_wall_ratio < 1.0)
    append_note(out.note,
                "thinnest wall spans under one element (thin_wall_ratio " +
                    std::to_string(out.thin_wall_ratio) +
                    "): a single element across a wall cannot bend");
  if (!detail::fill_quality(out)) {
    out.ok = false; // fill_quality set the diagnostic
    return false;
  }
  detail::measure_edges(out);
  mesh_cache_note_generation();
  out.ok = true;
  delivery_progress.set_elements(static_cast<std::size_t>(out.n_elems));
  delivery_progress.set_cells(static_cast<std::size_t>(out.n_elems),
                              static_cast<std::size_t>(out.n_elems));
  delivery_progress.set_phase("complete");
  return true;
}

bool retryable_lattice_guard(const std::string &error) {
  return error.find("geometry fill-stage guard failed") != std::string::npos;
}

// Polymesh refuses the whole fill (GeometryVolumeLimitError) when a CAD feature
// is smaller than the element size and the feature-refinement pass is off:
// "feature unresolved at h=<size>: CAD face <n> ...". Match the literal
// prefix — it is the pipeline's own wording and carries no Chudware framing.
bool feature_unresolved(const std::string &error) {
  return error.find("feature unresolved at h=") != std::string::npos;
}

// The same refusal also names the element size that WOULD resolve the feature:
// "... reduce -h to <= 2.68 mm or raise --max-elems/--max-dof". The number is a
// millimetre element size directly comparable to `h` (see in_model_units), and
// both the raw " m" spelling and the relabelled " mm" one are accepted so the
// hint survives whichever side of the relabel it is read on. Returns 0 when
// there is no usable hint.
double feature_hint_size_mm(const std::string &error) {
  static constexpr char kPrefix[] = "reduce -h to <= ";
  const size_t at = error.find(kPrefix);
  if (at == std::string::npos)
    return 0.0;
  const char *begin = error.c_str() + at + (sizeof(kPrefix) - 1);
  char *end = nullptr;
  const double value = std::strtod(begin, &end);
  if (end == begin || !std::isfinite(value) || value <= 0.0)
    return 0.0;
  // Require the unit token the pipeline always emits, so a reworded upstream
  // message degrades to "no hint" instead of a bogus element size.
  while (*end == ' ')
    ++end;
  if (end[0] != 'm')
    return 0.0;
  const char *tail = end[1] == 'm' ? end + 2 : end + 1;
  if (*tail != '\0' && *tail != ' ')
    return 0.0;
  return value;
}

// Defined below, beside the per-solid meshing fallback that also needs it.
std::vector<TopoDS_Shape> model_solids(const LoadedModel &loaded);

// Seed points ON the CAD faces polymesh's feature-resolution guard is about to
// refuse. The guard (pipeline/scene.cpp enforce_feature_resolution) calls a face
// a FEATURE when it is non-planar OR its area is under h^2, and refuses the
// whole fill when such a face has no aligned delivered boundary patch. The test
// below is deliberately the same one: these are the faces that need boundary
// patches, so these are the places to put fine cells.
//
// A non-planar face already carries an exact uv sample grid inside its trim
// (CadFace::samples, evaluated on the BRep, not on a tessellation); a small
// PLANAR face has none, so its bounding edges' polyline samples stand in. The
// total is decimated to kMaxSeeds so a detail-dense board cannot turn the seed
// stamp into the expensive part of the fill.
std::vector<Eigen::Vector3d> feature_face_seeds(const LoadedModel &loaded,
                                                double h) {
  std::vector<Eigen::Vector3d> seeds;
  if (!loaded.model.cad || loaded.model.cad->empty())
    return seeds;
  constexpr std::size_t kMaxSeeds = 20000;
  try {
    const pm::geom::CadTopology topology =
        pm::geom::extract_topology(*loaded.model.cad, 8);
    const double small_face_area = h * h;
    for (const pm::geom::CadFace &face : topology.faces) {
      const bool is_feature = face.kind != pm::geom::CadSurfaceKind::kPlane ||
                              face.area <= small_face_area;
      if (!is_feature)
        continue;
      if (!face.samples.empty()) {
        seeds.insert(seeds.end(), face.samples.begin(), face.samples.end());
        continue;
      }
      for (uint32_t edge_id : face.edge_ids)
        if (edge_id < topology.edges.size()) {
          const auto &samples = topology.edges[edge_id].samples;
          seeds.insert(seeds.end(), samples.begin(), samples.end());
        }
    }
  } catch (const std::exception &) {
    // No topology, no seeds: the caller falls through to the uniform ladder.
    seeds.clear();
  }
  if (seeds.size() > kMaxSeeds) {
    const std::size_t stride = seeds.size() / kMaxSeeds + 1;
    std::vector<Eigen::Vector3d> thinned;
    thinned.reserve(seeds.size() / stride + 1);
    for (std::size_t i = 0; i < seeds.size(); i += stride)
      thinned.push_back(seeds[i]);
    seeds.swap(thinned);
  }
  return seeds;
}

// The fill and its bounded recovery ladder, starting from `h_start` (0 = the
// size the advisor/size band resolved). Split out so the caller can re-run the
// WHOLE ladder at a different starting size: a recovery rung is what finally
// produces the mesh on a detail-dense part, so a check that only looked at the
// first attempt's result would not see the mesh anyone solves.
bool fill_with_recovery(const LoadedModel &loaded, const MeshOptions &options,
                        double h_start, MeshOut &out) {
  const AdvisorPlan plan = make_advisor_plan(loaded, options);
  double h_first = h_start > 0.0 ? h_start : plan.h;
  // A LOCAL refinement contract used to be delivered by LOWERING the background
  // to 4x the request (the graded lattice puts local children at h/2 and h/4 of
  // the background cell). That is gone, and had to be: polymesh raises h to its
  // own Cartesian grid-cell budget floor, so on a 230 mm part the background
  // came back at 2.75 mm however low it was asked to go, and a 0.35 mm interface
  // silently became 1.299 mm. It was also the expensive answer — a background of
  // 4x the local size is still the WHOLE part at 4x the local size (3.4M
  // predicted elements on that keyway). The contract is delivered locally
  // instead, by bisecting the elements at the seeds (deliver_local_contract),
  // and what the mesh actually achieved is measured and reported.
  auto attempt_with = [&](const MeshOptions &opts, double h,
                          bool feature_refine) {
    out = MeshOut();
    out.advisor = plan.result;
    append_note(out.note, loaded.band.note);
    if (out.advisor)
      append_note(out.note, out.advisor->note);
    pm::fea::NodalMesh nodal;
    try {
      return build_mesh(loaded, opts, h, feature_refine, {}, nodal, out);
    } catch (const std::exception &e) {
      out.ok = false;
      out.error = in_model_units(e.what());
      return false;
    }
  };
  auto attempt = [&](double h, bool feature_refine) {
    return attempt_with(plan.options, h, feature_refine);
  };
  // First attempt keeps the interactive default: draft/coarse skips the extra
  // feature pass.
  const bool feature_refine = default_feature_refine(plan.options);
  if (attempt(h_first, feature_refine)) {
    return true;
  }
  const std::string first_error = out.error;

  // A detail-dense model (thin ECAD boards, small bosses) can have features far
  // below h, and polymesh then refuses outright rather than meshing over them
  // ("feature unresolved at h=..."). Coarsening cannot help (the features stay
  // unresolved and the governor re-coarsens anyway), so the levers are the
  // feature-refinement pass and the size polymesh itself names. Run them as one
  // bounded ladder — and run it whatever the first attempt's refinement setting
  // was: a fine study starts with the pass already ON, and before this it got
  // no recovery at all even though the hint was sitting in the message.
  if (feature_unresolved(first_error)) {
    std::string chain = first_error;
    // FIRST rung: withdraw the size band's coarsening, if it made one. That
    // coarsening is a PREDICTION (mesh_size_band.cpp over-estimates the element
    // count by 6-9x on plate-like parts), and polymesh has just MEASURED that
    // the coarsened size cannot resolve this part's features. Forcing it
    // through with the refinement pass instead does "succeed", and on the
    // hole-array bracket of test_mesher_advanced that success is 27,688
    // elements in SEVEN connected components with aspect_max 4063 — a mesh
    // fea_static refuses — where the geometry's own 3.93 mm returns 21,764 in
    // ONE component. So the cheaper size is neither cheaper nor a mesh, and the
    // budget it was defending is not even breached: the honest fill lands under
    // the interactivity budget the prediction claimed it would blow by 5.6x.
    //
    // Only on the ORIGINAL call (h_start == 0): when the caller has already
    // pinned a starting size, this rung would re-do its work.
    if (h_start <= 0.0 && loaded.band.geometry_base > 0.0 &&
        loaded.band.geometry_base < h_first * (1.0 - 1e-9)) {
      if (attempt(loaded.band.geometry_base, feature_refine)) {
        char text[256];
        std::snprintf(text, sizeof(text),
                      "auto-coarsening withdrawn: polymesh refused the "
                      "budget-coarsened h=%.4g mm as unresolvable, so the mesh "
                      "was built at the geometry's own %.4g mm (%d elements)",
                      h_first, loaded.band.geometry_base, out.n_elems);
        append_note(out.note, text);
        return true;
      }
      chain += "; the geometry's own element size " +
               std::to_string(loaded.band.geometry_base) +
               " mm also failed: " + out.error;
    }
    if (!feature_refine) {
      // Retry ONCE at the SAME h with the skipped pass on: bounded,
      // deterministic, and it keeps a coarse study running instead of failing
      // where a finer setting would have passed.
      if (attempt(h_first, /*feature_refine=*/true)) {
        char text[192];
        std::snprintf(text, sizeof(text),
                      "feature refinement enabled after polymesh refused "
                      "h=%.4g mm (fineness %.2f)",
                      h_first, plan.options.fineness);
        append_note(out.note, text);
        return true;
      }
      chain += "; feature-refinement retry at the same element size also "
               "failed: " +
               out.error;
    }

    // NEXT rung, and the one that keeps a hole array from costing a million
    // elements: refine the unresolved features LOCALLY and keep the background
    // size. The guard polymesh just failed is about delivered boundary PATCHES
    // on small/curved faces (enforce_feature_resolution), not about the bulk
    // size, so fine cells stamped on exactly those faces satisfy it — while the
    // uniform hint ladder below pays for the same resolution over the whole
    // part. Measured cost of the uniform route on the live HINGE-LATCH-LOCAL
    // part: four O6.6 holes forced h <= 0.779 mm, i.e. 1.16M tet4 against the
    // element ceiling, for a plate that meshes in tens of thousands.
    //
    // Bounded: at most kLocalSteps rungs, each taking the size polymesh itself
    // named, and skipped entirely when the study already carries a local
    // refinement request of its own (the caller owns that contract) or when the
    // BRep yields no feature faces to seed.
    if (!options.local_refine.active()) {
      constexpr int kLocalSteps = 2;
      double local_hint = feature_hint_size_mm(out.error);
      if (!(local_hint > 0.0))
        local_hint = feature_hint_size_mm(first_error);
      const std::vector<Eigen::Vector3d> seeds =
          feature_face_seeds(loaded, h_first);
      for (int step = 0; step < kLocalSteps && local_hint > 0.0 && !seeds.empty();
           ++step) {
        // Land just under the stated bound, and never below what the lattice
        // can place at this background size.
        const double wanted = std::max(0.98 * local_hint, h_first * 0.25);
        MeshOptions local = plan.options;
        local.local_refine.size_mm = wanted;
        // One background cell of influence: the seeds sit ON the feature, and
        // the fine cells have to reach the wall around it.
        local.local_refine.band_mm = h_first;
        local.local_refine.label = "unresolved CAD features";
        local.local_refine.points.clear();
        local.local_refine.points.reserve(seeds.size() * 3);
        for (const Eigen::Vector3d &p : seeds) {
          local.local_refine.points.push_back(p.x());
          local.local_refine.points.push_back(p.y());
          local.local_refine.points.push_back(p.z());
        }
        if (attempt_with(local, h_first, /*feature_refine=*/true)) {
          char text[320];
          std::snprintf(
              text, sizeof(text),
              "features resolved by LOCAL refinement at %.4g mm on %zu seed "
              "point(s) of the small/curved CAD faces, background element size "
              "kept at %.4g mm (%d elements)",
              wanted, seeds.size(), h_first, out.n_elems);
          append_note(out.note, text);
          return true;
        }
        char tail[224];
        std::snprintf(tail, sizeof(tail),
                      "; local feature refinement at %.4g mm also failed: ",
                      wanted);
        chain += tail + out.error;
        if (!feature_unresolved(out.error))
          break;
        const double next_hint = feature_hint_size_mm(out.error);
        if (!(next_hint > 0.0) || !(next_hint < local_hint))
          break;
        local_hint = next_hint;
      }
    }

    // Next lever: every refusal names the element size that resolves the
    // feature it tripped on ("reduce -h to <= ..."), and resolving that one can
    // uncover a SMALLER feature with a new, smaller hint — measured on the
    // sensor-node demo board, 4.46 mm -> 2.68 mm -> 1.57 mm, one CAD face at a time. So
    // the hint is a ladder, not a single bound. It is bounded twice over: at
    // most kHintSteps rungs, and never below the study's own element-size band
    // floor, because that floor is what keeps the descent inside the memory
    // governor's approved 2:1 size band instead of trading a refusal for an
    // OOM. Falling off the bottom is reported with the size polymesh asked for,
    // so the caller can raise fineness or set min_size_mm deliberately.
    constexpr int kHintSteps = 3;
    const double floor_mm = loaded.band.min_len > 0.0 ? loaded.band.min_len : 0.0;
    double h = h_first;
    for (int step = 0; step < kHintSteps; ++step) {
      double suggested = feature_hint_size_mm(out.error);
      if (suggested <= 0.0)
        suggested = feature_hint_size_mm(first_error);
      if (!(suggested > 0.0) || !(suggested < h))
        break;
      // Land just under the stated bound: it is inclusive upstream but derived
      // from the same rounded value it prints, so sitting exactly on it can
      // still be refused.
      const double hinted = 0.98 * suggested;
      if (hinted < floor_mm) {
        char tail[224];
        std::snprintf(tail, sizeof(tail),
                      "; polymesh needs an element size of %.4g mm to resolve "
                      "this feature, below this study's element-size band floor "
                      "of %.4g mm — raise fineness or set min_size_mm to mesh "
                      "it",
                      suggested, floor_mm);
        chain += tail;
        break;
      }
      if (attempt(hinted, /*feature_refine=*/true)) {
        char text[224];
        std::snprintf(text, sizeof(text),
                      "element size reduced %.4g->%.4g mm over %d "
                      "feature-resolution hint(s) from polymesh (fineness %.2f)",
                      h_first, hinted, step + 1, plan.options.fineness);
        append_note(out.note, text);
        return true;
      }
      char tail[160];
      std::snprintf(tail, sizeof(tail),
                    "; feature-resolution hint retry at h=%.4g mm also failed: ",
                    hinted);
      chain += tail + out.error;
      h = hinted;
      // A different failure mode is not a sizing problem; stop guessing.
      if (!feature_unresolved(out.error))
        break;
    }
    out.error = std::move(chain);
    return false;
  }

  if (!retryable_lattice_guard(first_error))
    return false;

  // Graded-tet boundary fitting is lattice-phase sensitive: a valid BRep can
  // land exactly on a non-manifold/coincident skin at one bulk-cell pitch while
  // a nearby pitch is watertight. One bounded deterministic 6.25% coarser retry
  // changes the phase without weakening the geometry guard or exploding DOFs.
  if (attempt(h_first * 1.0625, feature_refine)) {
    append_note(out.note,
                "graded-tet lattice guard recovered on deterministic 17/16 "
                "element-size retry");
    return true;
  }
  out.error = first_error + "; deterministic 17/16 lattice retry also failed: " +
              out.error;
  return false;
}

// ── Was the coarsening honest? ─────────────────────────────────────────────
// resolve_size_band raises the element size when a PREDICTED element count
// blows the interactivity/memory budget, and that prediction over-estimates by
// 6-9x on plate-like parts (the measurements are beside the floor in
// mesh_size_band.cpp). A coarsening it justifies can therefore buy nothing and
// cost the mesh. Measured on the hole-array bracket of test_mesher_advanced
// (100 x 50 x 52 mm bbox over a 12 mm plate, so the bbox-side floor is inert):
// coarsened 3.93 -> 9.25 mm, the fill came back as 27,688 elements in SEVEN
// connected components with aspect_max 4063 and fea_static refused it, where
// the honest 3.93 mm gives 21,764 elements in ONE component — FEWER elements
// and a mesh instead of debris, because a fill asked for an element size its
// features cannot accept spends its budget on feature seeds instead.
//
// So the coarsening is verified on the RESULT rather than predicted from the
// geometry: which thin section a fill can still resolve is not decidable from a
// bounding box (nor from a section probe — mesh_size_band.cpp records why that
// was rejected: it withdraws the coarsening the sensor-node demo board depends on), but
// "came back in more pieces than the shape has solids" is decidable, already
// computed by fill_quality, and is exactly the defect that makes the mesh
// unsolvable. A genuinely multi-solid compound reports its own solid count and
// is untouched, and the rebuild is bounded: ONE re-run of the ladder, kept only
// if it is no more fragmented than what it replaces.
bool mesh_background(const LoadedModel &loaded, const MeshOptions &options,
                     const MeshProbeBudget &budget, MeshOut &out) {
  report_progress("mesh_background_probe");
  auto probe_start = std::chrono::steady_clock::now();
  std::vector<MeshGovernorProbe> probes = options.governor_probes;
  chudware::governor::MeshCountModel count_model;
  for (const auto& probe : probes)
    count_model.observe(probe.h, probe.elems);
  // The preceding leg already measured its anchor. Advance by a half-octave;
  // rebuild an admitted coarse mesh only if these finer actual counts do not fit.
  const double anchor = count_model.h;
  const double first_h = anchor > 0.0
      ? std::max(loaded.band.geometry_base, anchor / std::sqrt(2.0)) : 0.0;
  if (!fill_with_recovery(loaded, options, first_h, out))
    return false;
  auto record_probe = [&](const MeshOut& mesh, double prediction) {
    const double exponent = count_model.exponent;
    probes.push_back({mesh.char_len_mm, static_cast<uint64_t>(mesh.n_elems),
                     chudware::governor::process_rss_bytes() / 1073741824.0,
                     std::isfinite(prediction) ? prediction : 0.0, exponent,
                     std::chrono::duration<double>(
                         std::chrono::steady_clock::now() - probe_start).count()});
    count_model.observe(mesh.char_len_mm, mesh.n_elems);
    report_progress("mesh_background_probe_complete",
                    static_cast<std::size_t>(mesh.n_elems));
  };
  record_probe(out, count_model.h > 0.0 ? count_model.predict(out.char_len_mm) : 0.0);
  out.governor_probes = std::move(probes);
  auto retain_previous_admitted = [&]() {
    const auto limit = loaded.band.element_limit;
    if (limit == 0 || static_cast<uint64_t>(out.n_elems) <= limit)
      return;
    const MeshGovernorProbe* previous = nullptr;
    for (const auto& p : options.governor_probes)
      if (p.elems <= limit && (!previous || p.h < previous->h))
        previous = &p;
    if (!previous) return; // No measured mesh can be honestly offered instead.
    MeshOut fallback;
    report_progress("mesh_background_probe");
    const auto started = std::chrono::steady_clock::now();
    if (!fill_with_recovery(loaded, options, previous->h, fallback)) {
      append_note(out.note, "previous admitted mesh rebuild failed: " + fallback.error);
      return;
    }
    // This rebuild revisits an existing measurement; it is not an extrapolation
    // from the last rejected fine probe (which may be several octaves away).
    out.governor_probes.push_back({
        fallback.char_len_mm, static_cast<uint64_t>(fallback.n_elems),
        chudware::governor::process_rss_bytes() / 1073741824.0,
        0.0, 1.0,
        std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count(),
        "admission_fallback"});
    report_progress("mesh_background_probe_complete",
                    static_cast<std::size_t>(fallback.n_elems));
    if (static_cast<uint64_t>(fallback.n_elems) <= limit ||
        fallback.n_elems < out.n_elems) {
      fallback.governor_probes = std::move(out.governor_probes);
      fallback.governor_skipped_probes = std::move(out.governor_skipped_probes);
      append_note(fallback.note, out.note);
      fallback.governor_reason = "memory_admission";
      append_note(fallback.note,
                  "memory_admission: finer measured meshes exceed the solve budget; "
                  "delivering the finest previously admitted measurement");
      out = std::move(fallback);
    }
  };
  const double honest = loaded.band.geometry_base;
  if (!(honest > 0.0) || !(honest < out.char_len_mm * (1.0 - 1e-9))) {
    out.governor_reason = out.char_len_mm < honest * (1.0 - 1e-9)
        ? "feature_resolution" : "requested_size_admitted";
    retain_previous_admitted();
    return true;
  }

  // The bounding-box envelope is only a bootstrap, never evidence of the
  // requested mesh's cost. Measure every half-octave: feature-driven thin
  // fills need not be monotone, and one full-octave step can miss their turnover.
  // Previously a 14.72 mm probe was extrapolated directly to 2.84 mm, then that
  // forecast overruled even the ACTUAL count of the requested refill.
  if ((loaded.band.memory_coarsened || anchor > 0.0) && loaded.band.element_limit > 0) {
    probes = std::move(out.governor_probes);
    const auto limit = loaded.band.element_limit;
    while (count_model.h > honest * (1.0 + 1e-9)) {
      const double next = std::max(honest, count_model.h / std::sqrt(2.0));
      // A non-monotone count does not imply a cheaper fill: keep at least the
      // last observed wall cost, scaling upward when the measured model grows.
      const double predicted_s = probes.back().elapsed_s *
          std::max(1.0, count_model.predict(next) /
                            static_cast<double>(probes.back().elems));
      if (!budget.admit(next, predicted_s, out)) {
        out.governor_reason = "probe_time_budget";
        break;
      }
      report_progress("mesh_background_probe");
      probe_start = std::chrono::steady_clock::now();
      MeshOut candidate;
      if (!fill_with_recovery(loaded, options, next, candidate)) {
        out.governor_reason = candidate.error.find("band floor") != std::string::npos
            ? "size_band_floor" : "feature_resolution";
        append_note(out.note, "measured_octave governor: finer probe refused: " +
                                 candidate.error);
        break;
      }
      const double previous_h = count_model.h;
      // Recovery may change h; predict its delivered size from PREVIOUS counts,
      // before observing this candidate. Never label a different-size forecast.
      record_probe(candidate, count_model.predict(candidate.char_len_mm));
      // Resolution wins whenever the ACTUAL mesh fits. If neither fits, retain
      // the cheaper mesh for the solver's unchanged hard admission/refusal.
      const bool candidate_fits = static_cast<uint64_t>(candidate.n_elems) <= limit;
      const bool current_fits = static_cast<uint64_t>(out.n_elems) <= limit;
      if (!candidate_fits) out.governor_reason = "memory_admission";
      if ((candidate_fits && (!current_fits || candidate.char_len_mm < out.char_len_mm)) ||
          (!current_fits && candidate.n_elems < out.n_elems))
        out = std::move(candidate);
      // Recovery can itself change h. Do not loop on a non-progressing fill.
      if (!(count_model.h < previous_h * (1.0 - 1e-9))) {
        if (out.governor_reason.empty()) out.governor_reason = "feature_resolution";
        break;
      }
    }
    out.governor_probes = std::move(probes);
    retain_previous_admitted();
    if (out.char_len_mm <= honest * (1.0 + 1e-9))
      out.governor_reason = out.char_len_mm < honest * (1.0 - 1e-9)
          ? "feature_resolution" : "requested_size_admitted";
    char text[384];
    std::snprintf(text, sizeof(text),
                  "%smeasured_octave governor: requested %.4g mm, delivered %.4g mm; "
                  "%d ACTUAL elements versus %llu solve-budget elements; "
                  "bounding-box forecast replaced by measured probes",
                  out.char_len_mm > honest * (1.0 + 1e-9) ? "auto-coarsened by " : "",
                  honest, out.char_len_mm, out.n_elems,
                  static_cast<unsigned long long>(limit));
    append_note(out.note, text);
    return true;
  }
  if (out.connected_components <= 1)
    return true;
  const int expected =
      static_cast<int>(std::max<std::size_t>(model_solids(loaded).size(), 1));
  if (out.connected_components <= expected)
    return true;
  const double predicted_s = out.governor_probes.back().elapsed_s *
      std::max(1.0, count_model.predict(honest) /
                        static_cast<double>(out.governor_probes.back().elems));
  if (!budget.admit(honest, predicted_s, out))
    return true;
  report_progress("mesh_background_probe");
  probe_start = std::chrono::steady_clock::now();

  MeshOut coarse = std::move(out);
  out = MeshOut();
  char text[448];
  if (fill_with_recovery(loaded, options, honest, out) &&
      out.connected_components <= coarse.connected_components) {
    std::snprintf(text, sizeof(text),
                  "the auto-coarsened element size %.4g mm filled into %d "
                  "connected component(s) for a %d-solid shape, so the mesh was "
                  "rebuilt at the geometry's own element size %.4g mm: %d "
                  "component(s), %d elements against %d",
                  loaded.band.base, coarse.connected_components, expected,
                  honest, out.connected_components, out.n_elems,
                  coarse.n_elems);
    append_note(out.note, text);
    probes = std::move(coarse.governor_probes);
    record_probe(out, count_model.predict(out.char_len_mm));
    out.governor_probes = std::move(probes);
    return true;
  }
  std::snprintf(text, sizeof(text),
                "the auto-coarsened element size %.4g mm filled into %d "
                "connected component(s) for a %d-solid shape and the rebuild at "
                "%.4g mm did not improve on it — each island is its own "
                "stiffness system, so set an explicit target_size_mm here",
                loaded.band.base, coarse.connected_components, expected,
                honest);
  out = std::move(coarse);
  append_note(out.note, text);
  return true;
}

bool mesh_loaded_model(const LoadedModel &loaded, const MeshOptions &options,
                       const MeshProbeBudget &budget, MeshOut &out) {
  if (!options.local_refine.active()) {
    const bool ok = mesh_background(loaded, options, budget, out);
    budget.stamp(out);
    return ok;
  }

  // Never run the contact band through the measured-octave ladder. In
  // particular LEB closure and regional quality work belong to the delivered
  // mesh, not to discarded background-size probes.
  MeshOptions background{
      .fineness = options.fineness,
      .order = options.order,
      .target_size_mm = options.target_size_mm,
      .min_size_mm = options.min_size_mm,
      .max_size_mm = options.max_size_mm,
      .learned_advisor = options.learned_advisor,
      .advisor_max_dof = options.advisor_max_dof,
      .declared_interfaces = options.declared_interfaces,
      .local_refine = {},
      .governor_probes = options.governor_probes,
      .iterative_solver = options.iterative_solver};
  if (!mesh_background(loaded, background, budget, out)) {
    budget.stamp(out);
    return false;
  }
  const double delivered_h = out.char_len_mm;
  std::optional<MeshOptions> advised_options;
  if (out.nodes_per_elem != (options.order == 2 ? 10 : 4)) {
    advised_options = options;
    advised_options->order = out.nodes_per_elem == 10 ? 2 : 1;
  }
  auto probes = std::move(out.governor_probes);
  auto skipped = std::move(out.governor_skipped_probes);
  std::string governor_reason = std::move(out.governor_reason);
  std::string background_note = std::move(out.note);
  auto advisor = std::move(out.advisor);
  // Release the disposable background arrays before allocating the band fill.
  out = MeshOut();
  report_progress("mesh_interface_band");
  const auto started = std::chrono::steady_clock::now();
  bool ok = false;
  try {
    pm::fea::NodalMesh nodal;
    // Feature recovery has already selected a viable background size. The
    // final band is filled ONCE at that size: no alternate-size band probes
    // and no full-solid local-contract refill fallback.
    ok = build_mesh(loaded, advised_options ? *advised_options : options,
                    delivered_h, /*feature_refine=*/true, {}, nodal, out);
  } catch (const std::exception &e) {
    out.ok = false;
    out.error = in_model_units(e.what());
  }
  if (out.n_elems > 0) {
    probes.push_back(
        {out.char_len_mm, static_cast<uint64_t>(out.n_elems),
         chudware::governor::process_rss_bytes() / 1073741824.0, 0.0, 1.0,
         std::chrono::duration<double>(
             std::chrono::steady_clock::now() - started).count(),
         "local_refine"});
  }
  out.governor_probes = std::move(probes);
  out.governor_skipped_probes = std::move(skipped);
  out.governor_reason = std::move(governor_reason);
  out.advisor = std::move(advisor);
  append_note(out.note, background_note);
  if (ok)
    append_note(out.note,
                "local band filled once at the delivered background size; " +
                    std::to_string(out.n_elems) +
                    " ACTUAL total elements retained for solve admission");
  budget.stamp(out);
  report_progress(ok ? "mesh_interface_band_complete" : "mesh_interface_band_failed",
                  static_cast<std::size_t>(out.n_elems));
  // Downstream solve admission sees this ACTUAL total, including the band.
  // Never substitute the accepted background count or quietly fall back to it.
  return ok;
}

std::vector<TopoDS_Shape> model_solids(const LoadedModel &loaded) {
  std::vector<TopoDS_Shape> solids;
  if (!loaded.model.cad || loaded.model.cad->shape_handle() == nullptr)
    return solids;
  const auto &shape =
      *static_cast<const TopoDS_Shape *>(loaded.model.cad->shape_handle());
  for (TopExp_Explorer it(shape, TopAbs_SOLID); it.More(); it.Next())
    solids.push_back(it.Current());
  return solids;
}

// ── Solids that TOUCH or INTERPENETRATE are one body, so mesh them as one ────
// A multi-solid STEP is meshed solid by solid and the parts are concatenated,
// so they share no node BY CONSTRUCTION: every pair is a separate stiffness
// system whatever the CAD does. That is correct for an assembly with real
// clearances and wrong for the far more common import — a welded frame or a
// rod-and-lug upright whose members were never unioned. A rover model
// records 9299.81 mm3 of interpenetration between source bodies 200 and 201
// alone; such a study is refused for rigid-body motion at every element size,
// and until now the refusal named no remedy.
//
// So: solids whose closest approach is under kFuseTolMm are fused into one
// solid before the fill (fuzzy boolean at the same tolerance, which is what
// lets a boundary-representation touch survive the operation). Distance is the
// single test that covers both cases — interpenetrating solids are at distance
// 0, and so are ones sharing a face — and the volume the fuse REMOVED is the
// overlap, reported rather than inferred. A pair genuinely apart (the ctest's
// 1 mm gap) is left alone and still refused, by name.
//
// The tolerance is an IMPORT tolerance, not a modelling one: a micron is below
// anything a STEP round-trip preserves and four orders below any real
// clearance, so nothing that a study could reasonably call a gap is welded shut.
constexpr double kFuseTolMm = 1e-3;

// One meshable body: the shape to fill, which CAD solids it came from, and what
// fusing them cost in volume.
struct BodyGroup {
  TopoDS_Shape shape;
  std::vector<int> ordinals; // 1-based, in model_solids order
  double volume_before_mm3 = 0.0;
  double volume_after_mm3 = 0.0;
};

double solid_volume_mm3(const TopoDS_Shape &shape) {
  try {
    GProp_GProps props;
    BRepGProp::VolumeProperties(shape, props);
    return std::abs(props.Mass());
  } catch (const Standard_Failure &) {
    return 0.0;
  }
}

// Groups of solids to mesh together. Returns one group per solid when nothing
// touches; `fused` counts the groups that really merged more than one solid.
std::vector<BodyGroup> group_touching_solids(
    const std::vector<TopoDS_Shape> &solids, int &fused, std::string &note) {
  std::vector<BodyGroup> groups;
  fused = 0;
  const std::size_t n = solids.size();
  std::vector<Bnd_Box> boxes(n);
  for (std::size_t i = 0; i < n; ++i) {
    BRepBndLib::Add(solids[i], boxes[i]);
    boxes[i].Enlarge(kFuseTolMm);
  }
  std::vector<std::size_t> parent(n);
  for (std::size_t i = 0; i < n; ++i)
    parent[i] = i;
  std::function<std::size_t(std::size_t)> find = [&parent](std::size_t x) {
    while (parent[x] != x)
      x = parent[x] = parent[parent[x]];
    return x;
  };
  int probed = 0;
  for (std::size_t i = 0; i < n; ++i)
    for (std::size_t j = i + 1; j < n; ++j) {
      if (find(i) == find(j) || boxes[i].IsOut(boxes[j]))
        continue; // bbox-disjoint: no boolean can bring them closer
      ++probed;
      try {
        BRepExtrema_DistShapeShape distance(solids[i], solids[j],
                                           Extrema_ExtFlag_MIN);
        if (!distance.IsDone() || distance.Value() > kFuseTolMm)
          continue;
      } catch (const Standard_Failure &) {
        continue; // unmeasurable pair: never fuse on a guess
      }
      parent[find(i)] = find(j);
    }
  std::unordered_map<std::size_t, std::size_t> slot;
  slot.reserve(n);
  for (std::size_t i = 0; i < n; ++i) {
    const std::size_t root = find(i);
    auto it = slot.find(root);
    if (it == slot.end()) {
      it = slot.emplace(root, groups.size()).first;
      groups.emplace_back();
    }
    BodyGroup &group = groups[it->second];
    group.ordinals.push_back(static_cast<int>(i + 1));
    group.volume_before_mm3 += solid_volume_mm3(solids[i]);
    if (group.shape.IsNull()) {
      group.shape = solids[i];
      continue;
    }
    try {
      BRepAlgoAPI_Fuse fuse(group.shape, solids[i]);
      fuse.SetFuzzyValue(kFuseTolMm);
      fuse.Build();
      if (fuse.IsDone() && !fuse.Shape().IsNull())
        group.shape = fuse.Shape();
      else
        group.shape = TopoDS_Shape(); // signalled below
    } catch (const Standard_Failure &) {
      group.shape = TopoDS_Shape();
    }
    if (group.shape.IsNull()) {
      // A boolean that fails is not a reason to lose the geometry: fall back to
      // meshing these solids separately, which is exactly the old behaviour,
      // and say so instead of shipping a fused body that does not exist.
      groups.clear();
      fused = 0;
      for (std::size_t k = 0; k < n; ++k) {
        BodyGroup single;
        single.shape = solids[k];
        single.ordinals.push_back(static_cast<int>(k + 1));
        single.volume_before_mm3 = solid_volume_mm3(solids[k]);
        single.volume_after_mm3 = single.volume_before_mm3;
        groups.push_back(std::move(single));
      }
      note = "touching solids could not be fused (OCCT boolean failed); they "
             "are meshed as separate bodies and any island is refused by name";
      return groups;
    }
  }
  for (BodyGroup &group : groups) {
    group.volume_after_mm3 = group.ordinals.size() > 1
                                 ? solid_volume_mm3(group.shape)
                                 : group.volume_before_mm3;
    if (group.ordinals.size() > 1)
      ++fused;
  }
  if (fused > 0) {
    char text[384];
    double overlap = 0.0;
    int members = 0;
    for (const BodyGroup &group : groups)
      if (group.ordinals.size() > 1) {
        overlap += group.volume_before_mm3 - group.volume_after_mm3;
        members += static_cast<int>(group.ordinals.size());
      }
    std::snprintf(text, sizeof(text),
                  "%d of %zu solids touch or interpenetrate within %.4g mm and "
                  "were FUSED into %d body(ies) before meshing (%.6g mm^3 of "
                  "overlap removed, %d pair-distance probes): separately meshed "
                  "solids share no nodes, so an unfused weld is a rigid-body "
                  "mechanism at every element size",
                  members, n, kFuseTolMm, fused, overlap, probed);
    note = text;
  }
  return groups;
}

bool mesh_separate_solids(const std::vector<BodyGroup> &bodies,
                          const MeshOptions &options,
                          const MeshProbeBudget &budget, MeshOut &out) {
  static std::atomic<uint64_t> sequence{0};
  MeshOut combined;
  combined.nodes_per_elem = options.order == 2 ? 10 : 4;
  combined.mesh_type_used = options.order == 2 ? "tet10" : "tet4";
  combined.char_len_mm = std::numeric_limits<double>::infinity();
  for (size_t i = 0; i < bodies.size(); ++i) {
    const uint64_t ticket = sequence.fetch_add(1, std::memory_order_relaxed);
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    const std::filesystem::path path =
        std::filesystem::temp_directory_path() /
        ("chudware-mesh-solid-" + std::to_string(stamp) + "-" +
         std::to_string(ticket) + "-" + std::to_string(i) + ".brep");
    if (!BRepTools::Write(bodies[i].shape, path.string().c_str())) {
      out.error = "failed to serialize solid " + std::to_string(i + 1) +
                  " for separate-body meshing";
      return false;
    }
    LoadedModel loaded;
    std::string load_error;
    const bool loaded_ok = load_model(path.string(), options, loaded, load_error);
    std::error_code remove_error;
    std::filesystem::remove(path, remove_error);
    if (!loaded_ok) {
      out.error = "solid " + std::to_string(i + 1) + ": " + load_error;
      return false;
    }
    MeshOut part;
    if (!mesh_loaded_model(loaded, options, budget, part)) {
      out = std::move(part);
      out.governor_probes.insert(out.governor_probes.begin(),
                                 combined.governor_probes.begin(),
                                 combined.governor_probes.end());
      out.governor_skipped_probes.insert(
          out.governor_skipped_probes.begin(),
          combined.governor_skipped_probes.begin(),
          combined.governor_skipped_probes.end());
      out.error = "solid " + std::to_string(i + 1) + ": " + out.error;
      return false;
    }
    const int node_offset = static_cast<int>(combined.points.size() / 3);
    combined.points.insert(combined.points.end(), part.points.begin(),
                           part.points.end());
    combined.tets.reserve(combined.tets.size() + part.tets.size());
    for (int node : part.tets)
      combined.tets.push_back(node + node_offset);
    // Which CAD solids this block of nodes came from, so a rigid-body refusal
    // can name the island's body instead of its component index.
    MeshOut::Body span;
    span.source_ordinals = bodies[i].ordinals;
    span.node_begin = node_offset;
    span.node_end = static_cast<int>(combined.points.size() / 3);
    span.volume_mm3 = bodies[i].volume_after_mm3;
    span.fused_overlap_mm3 =
        bodies[i].ordinals.size() > 1
            ? bodies[i].volume_before_mm3 - bodies[i].volume_after_mm3
            : 0.0;
    if (bodies[i].ordinals.size() > 1) {
      ++combined.fused_groups;
      combined.fused_solids += static_cast<int>(bodies[i].ordinals.size());
      combined.fused_overlap_mm3 += span.fused_overlap_mm3;
    }
    combined.bodies.push_back(std::move(span));
    combined.n_nodes += part.n_nodes;
    combined.n_elems += part.n_elems;
    combined.quadratic_fallback_elems += part.quadratic_fallback_elems;
    combined.reoriented_elems += part.reoriented_elems;
    combined.degenerate_elems_repaired += part.degenerate_elems_repaired;
    combined.degenerate_elems_remaining += part.degenerate_elems_remaining;
    combined.hanging_elems_dropped += part.hanging_elems_dropped;
    combined.lonely_nodes_repaired += part.lonely_nodes_repaired;
    combined.dropped_volume_mm3 += part.dropped_volume_mm3;
    combined.debris_components_dissolved += part.debris_components_dissolved;
    combined.debris_elems_dropped += part.debris_elems_dropped;
    combined.debris_volume_mm3 += part.debris_volume_mm3;
    combined.char_len_mm = std::min(combined.char_len_mm, part.char_len_mm);
    if (part.size_band_applied_floor_mm > combined.size_band_applied_floor_mm) {
      combined.size_band_applied_floor_mm = part.size_band_applied_floor_mm;
      combined.size_band_reason = part.size_band_reason;
    }
    if (part.thin_wall_ratio > 0.0 &&
        (combined.thin_wall_ratio <= 0.0 || part.thin_wall_ratio < combined.thin_wall_ratio))
      combined.thin_wall_ratio = part.thin_wall_ratio;
    if (part.min_wall_mm > 0.0 &&
        (combined.min_wall_mm <= 0.0 || part.min_wall_mm < combined.min_wall_mm))
      combined.min_wall_mm = part.min_wall_mm;
    if (!part.governor_reason.empty() &&
        part.governor_reason != "requested_size_admitted")
      combined.governor_reason = part.governor_reason;
    combined.governor_probes.insert(combined.governor_probes.end(),
                                   part.governor_probes.begin(),
                                   part.governor_probes.end());
    combined.governor_skipped_probes.insert(
        combined.governor_skipped_probes.end(),
        part.governor_skipped_probes.begin(), part.governor_skipped_probes.end());
    // The local size contract is a property of the WHOLE mesh a study solves,
    // and every solid was meshed against the same seed set: the worst achieved
    // size decides whether the contract was met, and a ceiling that bound on
    // any one solid bound the study.
    combined.local_refine_size_mm =
        std::max(combined.local_refine_size_mm, part.local_refine_size_mm);
    combined.local_refine_achieved_mm = std::max(combined.local_refine_achieved_mm,
                                                 part.local_refine_achieved_mm);
    combined.local_refine_waves =
        std::max(combined.local_refine_waves, part.local_refine_waves);
    combined.local_refine_elems_added += part.local_refine_elems_added;
    combined.local_refine_transition_depth_mm =
        std::max(combined.local_refine_transition_depth_mm,
                 part.local_refine_transition_depth_mm);
    combined.local_refine_transition_marks += part.local_refine_transition_marks;
    combined.local_refine_outside_transition_marks +=
        part.local_refine_outside_transition_marks;
    combined.local_refine_outside_transition_checks +=
        part.local_refine_outside_transition_checks;
    combined.local_refine_ceiling_bound |= part.local_refine_ceiling_bound;
    combined.local_refine_element_ceiling =
        std::max(combined.local_refine_element_ceiling,
                 part.local_refine_element_ceiling);
    // Same rule for the quality pass: the WORST region across the solids is
    // the region the study solves, and the counts are the whole mesh's.
    combined.local_refine_smoothing_passes +=
        part.local_refine_smoothing_passes;
    combined.local_refine_resplit |= part.local_refine_resplit;
    const auto worse_min = [](double have, double add) {
      return have > 0.0 ? std::min(have, add) : add;
    };
    combined.local_refine_jacobian_min_before =
        worse_min(combined.local_refine_jacobian_min_before,
                  part.local_refine_jacobian_min_before);
    combined.local_refine_jacobian_min_after =
        worse_min(combined.local_refine_jacobian_min_after,
                  part.local_refine_jacobian_min_after);
    combined.local_refine_aspect_max_before =
        std::max(combined.local_refine_aspect_max_before,
                 part.local_refine_aspect_max_before);
    combined.local_refine_aspect_max_after =
        std::max(combined.local_refine_aspect_max_after,
                 part.local_refine_aspect_max_after);
    combined.local_refine_cells_below_floor_before +=
        part.local_refine_cells_below_floor_before;
    combined.local_refine_cells_below_floor_after +=
        part.local_refine_cells_below_floor_after;
    // The stop reason belongs to the mesh a study solves, not to one solid: the
    // refusal that quotes it is about the whole interface.
    if (!part.local_refine_limit.empty()) {
      if (!combined.local_refine_limit.empty()) combined.local_refine_limit += "; ";
      combined.local_refine_limit +=
          "solid " + std::to_string(i + 1) + ": " + part.local_refine_limit;
    }
    append_note(combined.note, part.note);
    if (!combined.advisor && part.advisor)
      combined.advisor = part.advisor;
  }
  append_note(combined.note,
              std::to_string(bodies.size()) +
                  " separate solid bodies meshed independently (they share no "
                  "nodes: each is its own stiffness system)");
  // Each part was repaired on its own; the merge cannot create a new hanging
  // element (parts share no nodes), so only the count needs refreshing.
  detail::count_lonely_nodes(combined);
  if (!detail::fill_quality(combined)) {
    out = std::move(combined);
    out.ok = false;
    return false;
  }
  combined.ok = true;
  combined.governor_probes.push_back(
      {combined.char_len_mm, static_cast<uint64_t>(combined.n_elems),
       chudware::governor::process_rss_bytes() / 1073741824.0, 0.0, 2.0,
       budget.elapsed(), "combined"});
  budget.stamp(combined);
  out = std::move(combined);
  return true;
}

} // namespace

MeshOut mesh_step(const std::string &step_path, const MeshOptions &options) {
  MeshOut out;
  if (!validate_mesh_options(options, out.error)) {
    out.ok = false;
    return out;
  }
  init_polymesh_runtime();

  chudware::prof::phases::StudyScope study_scope("mesh_tet");
  const MeshProbeBudget probe_budget;
  try {
    LoadedModel loaded;
    {
      chudware::prof::phases::PhaseScope phase("load_step");
      if (!load_model(step_path, options, loaded, out.error)) {
        out.ok = false;
        return out;
      }
    }
    const std::vector<TopoDS_Shape> solids = model_solids(loaded);
    if (solids.size() > 1) {
      // A study that DECLARED its interfaces (contact, or a bonded gap tie)
      // needs its bodies apart — fusing them would delete the interface the
      // study is about, and fea_static refuses `contact` on one component.
      if (options.declared_interfaces) {
        std::vector<BodyGroup> bodies;
        bodies.reserve(solids.size());
        for (std::size_t i = 0; i < solids.size(); ++i) {
          BodyGroup single;
          single.shape = solids[i];
          single.ordinals.push_back(static_cast<int>(i + 1));
          single.volume_before_mm3 = solid_volume_mm3(solids[i]);
          single.volume_after_mm3 = single.volume_before_mm3;
          bodies.push_back(std::move(single));
        }
        mesh_separate_solids(bodies, options, probe_budget, out);
        append_note(out.note,
                    "the study declared its interfaces (contact / bonded gap), "
                    "so touching solids were NOT fused");
      } else {
        int fused = 0;
        std::string fuse_note;
        std::vector<BodyGroup> bodies =
            group_touching_solids(solids, fused, fuse_note);
        if (bodies.size() == 1) {
          // One body after fusing: mesh it as ONE fill, which is the whole
          // point — a single fill shares nodes across the weld.
          const uint64_t ticket = static_cast<uint64_t>(
              std::chrono::steady_clock::now().time_since_epoch().count());
          const std::filesystem::path path =
              std::filesystem::temp_directory_path() /
              ("chudware-mesh-fused-" + std::to_string(ticket) + ".brep");
          LoadedModel fused_model;
          std::string load_error;
          bool ready = BRepTools::Write(bodies[0].shape, path.string().c_str()) &&
                       load_model(path.string(), options, fused_model, load_error);
          std::error_code remove_error;
          std::filesystem::remove(path, remove_error);
          if (ready) {
            mesh_loaded_model(fused_model, options, probe_budget, out);
            MeshOut::Body span;
            span.source_ordinals = bodies[0].ordinals;
            span.node_begin = 0;
            span.node_end = static_cast<int>(out.points.size() / 3);
            span.volume_mm3 = bodies[0].volume_after_mm3;
            span.fused_overlap_mm3 =
                bodies[0].volume_before_mm3 - bodies[0].volume_after_mm3;
            out.bodies.push_back(std::move(span));
            out.fused_groups = 1;
            out.fused_solids = static_cast<int>(bodies[0].ordinals.size());
            out.fused_overlap_mm3 =
                bodies[0].volume_before_mm3 - bodies[0].volume_after_mm3;
          } else {
            mesh_separate_solids(bodies, options, probe_budget, out);
            append_note(out.note,
                        "the fused body could not be re-loaded for meshing (" +
                            load_error + "), so it was meshed per solid");
          }
        } else {
          mesh_separate_solids(bodies, options, probe_budget, out);
        }
        if (!fuse_note.empty())
          append_note(out.note, fuse_note);
      }
    } else {
      mesh_loaded_model(loaded, options, probe_budget, out);
      MeshOut::Body span;
      span.source_ordinals.push_back(1);
      span.node_begin = 0;
      span.node_end = static_cast<int>(out.points.size() / 3);
      span.volume_mm3 = solids.empty() ? 0.0 : solid_volume_mm3(solids[0]);
      out.bodies.push_back(std::move(span));
    }
    if (out.ok && solids.size() > 1) detail::measure_edges(out);
    if (!out.ok && !out.error.starts_with("polymesh mesher failed: "))
      out.error = "polymesh mesher failed: " + out.error;
    return out;
  } catch (const std::exception &e) {
    out.ok = false;
    out.error = "polymesh mesher failed: " + in_model_units(e.what());
    return out;
  } catch (...) {
    out.ok = false;
    out.error = "polymesh mesher failed with an unknown exception";
    return out;
  }
}

// ── Adaptive driver
// ─────────────────────────────────────────────────────────────────

struct AdaptiveMesher::Impl {
  LoadedModel loaded;
  MeshOptions options;
  double h = 0.0; // coarse background element size, mm (never rises)
  double h_fine =
      0.0; // target size inside the marked region, mm (shrinks per pass)
  pm::mesh::SizeFieldFn size_field; // empty on the first pass = uniform h
  pm::fea::NodalMesh nodal;         // the mesh the last mesh() returned
  std::optional<MeshAdvisor> advisor;
  int advisor_adapt_passes = -1; // -1 = advisor absent/refused
  int advisor_passes_used = 0;
  double advisor_eta_target = 0.0;
  double last_global_eta = std::numeric_limits<double>::infinity();
};

AdaptiveMesher::AdaptiveMesher(std::unique_ptr<Impl> impl)
    : impl_(std::move(impl)) {}
AdaptiveMesher::~AdaptiveMesher() = default;

std::unique_ptr<AdaptiveMesher>
AdaptiveMesher::open(const std::string &step_path, const MeshOptions &options,
                     std::string &error) {
  if (!validate_mesh_options(options, error))
    return nullptr;
  init_polymesh_runtime();
  try {
    auto impl = std::make_unique<Impl>();
    if (!load_model(step_path, options, impl->loaded, error))
      return nullptr;
    AdvisorPlan plan = make_advisor_plan(impl->loaded, options);
    impl->options = plan.options;
    impl->h = plan.h;
    impl->h_fine = plan.h;
    impl->advisor = std::move(plan.result);
    if (impl->advisor && impl->advisor->applied) {
      impl->advisor_adapt_passes = impl->advisor->adapt_passes;
      impl->advisor_eta_target = impl->advisor->eta_target;
    }
    return std::unique_ptr<AdaptiveMesher>(new AdaptiveMesher(std::move(impl)));
  } catch (const std::exception &e) {
    error = "polymesh adaptive open failed: " + in_model_units(e.what());
    return nullptr;
  } catch (...) {
    error = "polymesh adaptive open failed with an unknown exception";
    return nullptr;
  }
}

// The background element size, mm — the number that actually moves as the loop
// adapts (see refine() for why the background size, and not a purely local
// field, is the lever).
double AdaptiveMesher::element_size_mm() const { return impl_->h; }

bool AdaptiveMesher::mesh(MeshOut &out, std::string &error) {
  try {
    out = MeshOut();
    append_note(out.note, impl_->loaded.band.note);
    out.advisor = impl_->advisor;
    if (out.advisor)
      append_note(out.note, out.advisor->note);
    if (!build_mesh(impl_->loaded, impl_->options, impl_->h,
                    default_feature_refine(impl_->options), impl_->size_field,
                    impl_->nodal, out)) {
      error = out.error;
      return false;
    }
    return true;
  } catch (const std::exception &e) {
    error = "polymesh adaptive mesh failed: " + in_model_units(e.what());
    return false;
  } catch (...) {
    error = "polymesh adaptive mesh failed with an unknown exception";
    return false;
  }
}

bool AdaptiveMesher::estimate_error(const std::vector<double> &disp_mm,
                                    double e_mpa, double nu,
                                    std::vector<double> &element_eta,
                                    double &global_eta, std::string &error) {
  if (impl_->nodal.nodes.empty()) {
    error = "polymesh adaptive: estimate_error called before mesh()";
    return false;
  }
  if (disp_mm.size() != impl_->nodal.nodes.size() * 3) {
    error = "polymesh adaptive: displacement length does not match the current "
            "mesh";
    return false;
  }
  try {
    // Unit consistency: coordinates mm, displacement mm, E in MPa (= N/mm^2).
    // The recovered stresses are then MPa, and global_eta is a dimensionless
    // relative indicator, so no scaling is needed anywhere in this call.
    const pm::fea::Material material{e_mpa, nu};
    Eigen::VectorXd u(static_cast<Eigen::Index>(disp_mm.size()));
    for (size_t i = 0; i < disp_mm.size(); ++i)
      u[static_cast<Eigen::Index>(i)] = disp_mm[i];
    const pm::fea::ZzRecovery zz =
        pm::fea::recover_zz(impl_->nodal, material, u);
    element_eta = zz.element_eta;
    global_eta = zz.global_eta;
    impl_->last_global_eta = global_eta;
    return true;
  } catch (const std::exception &e) {
    error = std::string("polymesh error recovery failed: ") + e.what();
    return false;
  } catch (...) {
    error = "polymesh error recovery failed with an unknown exception";
    return false;
  }
}

bool AdaptiveMesher::refine(const std::vector<double> &element_eta,
                            std::string &note, std::string &error) {
  if (element_eta.size() != impl_->nodal.elements.size()) {
    error =
        "polymesh adaptive: error field length does not match the current mesh";
    return false;
  }
  if (impl_->advisor_adapt_passes >= 0) {
    if (impl_->advisor_passes_used >= impl_->advisor_adapt_passes) {
      note = "advisor adaptation-pass limit reached (" +
             std::to_string(impl_->advisor_adapt_passes) + ")";
      return false;
    }
    if (impl_->advisor_eta_target > 0.0 &&
        impl_->last_global_eta <= impl_->advisor_eta_target) {
      note = "advisor eta target met (" +
             std::to_string(impl_->last_global_eta) +
             " <= " + std::to_string(impl_->advisor_eta_target) + ")";
      return false;
    }
  }
  try {
    std::vector<Eigen::Vector3d> centroids;
    centroids.reserve(impl_->nodal.elements.size());
    for (const pm::fea::NodalElement &element : impl_->nodal.elements) {
      Eigen::Vector3d c = Eigen::Vector3d::Zero();
      for (uint32_t n : element.nodes)
        c += impl_->nodal.nodes[n];
      centroids.push_back(c / static_cast<double>(element.nodes.size()));
    }
    // Dorfler marking at theta=0.3: the smallest set of elements carrying 30%
    // of the total error — on a stress concentration, a few percent of the
    // mesh.
    const std::vector<Eigen::Vector3d> marks =
        pm::adapt::marked_centroids(centroids, element_eta, /*theta=*/0.3);
    if (marks.empty()) {
      note = "no elements marked for refinement";
      return false;
    }

    // WHAT ACTUALLY REFINES, and why it is not pure local refinement.
    //
    // polymesh's graded tet fill is a three-level lattice: a background cell at
    // h, and local children at h/2 (L1) and h/4 (L2). A size field only decides
    // WHERE those children go — it cannot ask for anything below h/4, and on a
    // part that is mostly boundary skin (any plate, bracket or shell) the fill
    // is already at L2 everywhere, so a field alone changes nothing at all.
    // Measured on a 120x60x8 plate: identical 41,515 elements whether the field
    // asked for 4.87 mm or 1.96 mm.
    //
    // So the background size is the lever that guarantees progress, and the
    // size field rides along to bias the child cells toward the marked region.
    // Each pass is therefore "a finer mesh, graded toward the error", not "more
    // elements only at the error" — the honest description of what this mesher
    // can deliver.
    const double h_floor = impl_->loaded.band.base * 0.35;
    const double next_h = std::max(impl_->h * 0.7, h_floor);
    if (next_h >= impl_->h * 0.98) {
      note = "element-size floor reached (" + std::to_string(h_floor) + " mm)";
      return false;
    }
    impl_->h = next_h;
    impl_->h_fine = next_h / 4.0; // the finest the lattice can place (L2)
    const std::vector<pm::adapt::SizeSource> sources =
        pm::adapt::point_size_sources(marks, impl_->h_fine);
    // beta = 1.0: the requested size may grow at most 1 mm per mm away from a
    // mark, the standard gradient limit, which keeps the fine/coarse transition
    // free of slivers without a separate smoothing pass.
    impl_->size_field = pm::adapt::size_field_from_sources(
        sources, impl_->h_fine, impl_->h, /*beta=*/1.0);
    if (impl_->advisor_adapt_passes >= 0)
      ++impl_->advisor_passes_used;
    return true;
  } catch (const std::exception &e) {
    error = std::string("polymesh refinement failed: ") + e.what();
    return false;
  } catch (...) {
    error = "polymesh refinement failed with an unknown exception";
    return false;
  }
}

} // namespace chudware::sim
