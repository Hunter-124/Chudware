// Polyhedral-FEA mesher and adaptive-refinement bridge for Chudware.
#include "sim/mesher.h"

#include "prof/phases.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
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
#include <BRepTools.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp_Explorer.hxx>

#include "sim/mesh_size_band.h"
#include "sim/solver_threads.h"

#include <adapt/graded_sizing.hpp>
#include <adapt/loop.hpp>
#include <fea/backend.hpp>
#include <fea/material.hpp>
#include <fea/nodal_mesh.hpp>
#include <fea/p_elevate.hpp>
#include <fea/zz.hpp>
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
  const pm::pipeline::ResolvedMeshSize resolved =
      pm::pipeline::resolve_mesh_size(loaded.model, h);

  // Graded tet is the one Polyhedral-FEA family that matches Chudware's
  // fixed-stride tet solve contract and accepts the spatial size field used by
  // adaptive refinement.
  const pm::pipeline::VolumeMeshOutput volume = [&] {
    chudware::prof::phases::PhaseScope phase("generate");
    return pm::pipeline::volume_mesh(
        loaded.model, h, pm::pipeline::VolumeMesher::kGradedTet,
        /*skin_layers=*/2,
        // The graded skin stays active regardless; the extra
        // sharp-edge/thickness pass is the caller's choice.
        feature_refine, /*refine_seeds=*/{},
        /*seed_band=*/0.0,
        /*element_tendency=*/0.0, resolved.element_ceiling, resolved.dof_ceiling,
        /*auto_retry_budget=*/0, /*cancel_check=*/{}, size_field);
  }();

  nodal = volume.mesh;
  if (options.order == 2) {
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
  nodal.compact_unused_nodes();

  chudware::prof::phases::PhaseScope extract_phase("extract");
  const int stride = options.order == 2 ? 10 : 4;
  const pm::fea::ElementType want = options.order == 2
                                        ? pm::fea::ElementType::kTet10
                                        : pm::fea::ElementType::kTet4;
  // A mixed cell here would be silently misread as a tet by every downstream
  // reader (they index a flat array by a constant stride), so refuse instead of
  // truncating.
  for (const pm::fea::NodalElement &element : nodal.elements) {
    if (element.type != want) {
      out.ok = false;
      out.error =
          std::string("polyfea: mesh contains unsupported ") +
          pm::fea::element_type_name(element.type) +
          " connectivity; Chudware studies currently require tet4 or tet10";
      return false;
    }
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
  out.n_nodes = static_cast<int>(nodal.nodes.size());
  out.n_elems = static_cast<int>(nodal.elements.size());
  out.nodes_per_elem = stride;
  out.mesh_type_used = stride == 10 ? "tet10" : "tet4";
  append_note(out.note, volume.mesher_note);
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
  if (!detail::fill_quality(out)) {
    out.ok = false; // fill_quality set the diagnostic
    return false;
  }
  mesh_cache_note_generation();
  out.ok = true;
  return true;
}

bool retryable_lattice_guard(const std::string &error) {
  return error.find("geometry fill-stage guard failed") != std::string::npos;
}

// Polymesh refuses the whole fill (GeometryVolumeLimitError) when a CAD feature
// is smaller than the element size and the feature-refinement pass is off:
// "feature unresolved at h=<metres> m: CAD face <n> ...". Match the literal
// prefix — it is the pipeline's own wording and carries no Chudware framing.
bool feature_unresolved(const std::string &error) {
  return error.find("feature unresolved at h=") != std::string::npos;
}

// The same refusal also names the element size that WOULD resolve the feature:
// "... reduce -h to <= 1.56 m or raise --max-elems/--max-dof". Polyhedral-FEA
// labels model units as metres in its diagnostics, but Chudware resolves the
// size band in MILLIMETRES and feeds that straight into pm::pipeline::volume_mesh
// (see build_mesh above and the size-band comment in load_model) with no unit
// conversion anywhere, so the printed number is already a millimetre element
// size directly comparable to `h`. Returns 0 when there is no usable hint.
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
  if (end[0] != 'm' || (end[1] != '\0' && end[1] != ' '))
    return 0.0;
  return value;
}

bool mesh_loaded_model(const LoadedModel &loaded, const MeshOptions &options,
                       MeshOut &out) {
  const AdvisorPlan plan = make_advisor_plan(loaded, options);
  auto attempt = [&](double h, bool feature_refine) {
    out = MeshOut();
    out.advisor = plan.result;
    append_note(out.note, loaded.band.note);
    if (out.advisor)
      append_note(out.note, out.advisor->note);
    pm::fea::NodalMesh nodal;
    try {
      return build_mesh(loaded, plan.options, h, feature_refine, {}, nodal, out);
    } catch (const std::exception &e) {
      out.ok = false;
      out.error = e.what();
      return false;
    }
  };
  // First attempt keeps the interactive default: draft/coarse skips the extra
  // feature pass.
  const bool feature_refine = default_feature_refine(plan.options);
  if (attempt(plan.h, feature_refine))
    return true;
  const std::string first_error = out.error;

  // A detail-dense model (thin ECAD boards, small bosses) can have features far
  // below h, and polymesh then refuses outright rather than meshing over them.
  // Coarsening cannot help (the governor re-coarsens anyway and the features
  // stay unresolved), so the one lever is the pass we skipped. Retry ONCE at
  // the SAME h with it on: bounded, deterministic, and it keeps a coarse study
  // running instead of failing where a finer setting would have passed.
  if (!feature_refine && feature_unresolved(first_error)) {
    if (attempt(plan.h, /*feature_refine=*/true)) {
      char text[192];
      std::snprintf(text, sizeof(text),
                    "feature refinement enabled after polymesh refused "
                    "h=%.4g mm (fineness %.2f)",
                    plan.h, plan.options.fineness);
      append_note(out.note, text);
      return true;
    }
    std::string chain = first_error +
                        "; feature-refinement retry at the same element size "
                        "also failed: " +
                        out.error;

    // Last bounded lever: polymesh stated the element size that resolves the
    // feature, so honour it once instead of discarding it and failing a study
    // that a finer setting would have meshed. Only ever DOWNWARD, and only
    // once — the hint is a hard bound, not a search.
    double suggested = feature_hint_size_mm(out.error);
    if (suggested <= 0.0)
      suggested = feature_hint_size_mm(first_error);
    if (suggested > 0.0 && suggested < plan.h) {
      // Land just under the stated bound: it is inclusive upstream but derived
      // from the same rounded value it prints, so sitting exactly on it can
      // still be refused.
      const double hinted = std::min(suggested, 0.98 * suggested);
      if (attempt(hinted, /*feature_refine=*/true)) {
        char text[192];
        std::snprintf(text, sizeof(text),
                      "element size reduced %.4g->%.4g mm on polymesh's "
                      "feature-resolution hint (fineness %.2f)",
                      plan.h, hinted, plan.options.fineness);
        append_note(out.note, text);
        return true;
      }
      char tail[128];
      std::snprintf(tail, sizeof(tail),
                    "; feature-resolution hint retry at h=%.4g mm also failed: ",
                    hinted);
      out.error = chain + tail + out.error;
      return false;
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
  if (attempt(plan.h * 1.0625, feature_refine)) {
    append_note(out.note,
                "graded-tet lattice guard recovered on deterministic 17/16 "
                "element-size retry");
    return true;
  }
  out.error = first_error + "; deterministic 17/16 lattice retry also failed: " +
              out.error;
  return false;
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

bool mesh_separate_solids(const std::vector<TopoDS_Shape> &solids,
                          const MeshOptions &options, MeshOut &out) {
  static std::atomic<uint64_t> sequence{0};
  MeshOut combined;
  combined.nodes_per_elem = options.order == 2 ? 10 : 4;
  combined.mesh_type_used = options.order == 2 ? "tet10" : "tet4";
  combined.char_len_mm = std::numeric_limits<double>::infinity();
  for (size_t i = 0; i < solids.size(); ++i) {
    const uint64_t ticket = sequence.fetch_add(1, std::memory_order_relaxed);
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    const std::filesystem::path path =
        std::filesystem::temp_directory_path() /
        ("chudware-mesh-solid-" + std::to_string(stamp) + "-" +
         std::to_string(ticket) + "-" + std::to_string(i) + ".brep");
    if (!BRepTools::Write(solids[i], path.string().c_str())) {
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
    if (!mesh_loaded_model(loaded, options, part)) {
      out.error = "solid " + std::to_string(i + 1) + ": " + part.error;
      return false;
    }
    const int node_offset = static_cast<int>(combined.points.size() / 3);
    combined.points.insert(combined.points.end(), part.points.begin(),
                           part.points.end());
    combined.tets.reserve(combined.tets.size() + part.tets.size());
    for (int node : part.tets)
      combined.tets.push_back(node + node_offset);
    combined.n_nodes += part.n_nodes;
    combined.n_elems += part.n_elems;
    combined.quadratic_fallback_elems += part.quadratic_fallback_elems;
    combined.char_len_mm = std::min(combined.char_len_mm, part.char_len_mm);
    append_note(combined.note, part.note);
    if (!combined.advisor && part.advisor)
      combined.advisor = part.advisor;
  }
  append_note(combined.note, std::to_string(solids.size()) +
                                 " separate solid bodies meshed independently");
  if (!detail::fill_quality(combined)) {
    out = std::move(combined);
    out.ok = false;
    return false;
  }
  combined.ok = true;
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
    if (solids.size() > 1)
      mesh_separate_solids(solids, options, out);
    else
      mesh_loaded_model(loaded, options, out);
    if (!out.ok && !out.error.starts_with("polymesh mesher failed: "))
      out.error = "polymesh mesher failed: " + out.error;
    return out;
  } catch (const std::exception &e) {
    out.ok = false;
    out.error = std::string("polymesh mesher failed: ") + e.what();
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
    error = std::string("polymesh adaptive open failed: ") + e.what();
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
    error = std::string("polymesh adaptive mesh failed: ") + e.what();
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
