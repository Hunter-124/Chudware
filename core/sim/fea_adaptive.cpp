// Adaptive static FEA driver. See sim/fea_adaptive.h for why this exists.
//
// The whole design is "reuse, do not reimplement": each pass hands fea_run_static an
// INLINE mesh (a shape its study schema already accepts) and takes back the standard
// result. Nothing about boundary-condition resolution, bonded contact, materials,
// cancellation or the result payload is duplicated here, so an adaptive result cannot
// drift away from an ordinary one.
#include "sim/fea_adaptive.h"

#include <algorithm>
#include <cmath>
#include <memory>
#include <string>
#include <vector>

#include "sim/fea_run.h"
#include "sim/mesher.h"
#include "sim/overlay.h"

namespace chudware::sim {

using nlohmann::json;

namespace detail {
// Defined in sim/fea_run.cpp, which owns the one advisor->JSON shape every study route
// reports. Declared here rather than duplicated: an adaptive result's advisor envelope
// must be field-for-field the same object an ordinary study returns.
json mesh_advisor_json(const MeshAdvisor& advisor);
}  // namespace detail

namespace {

constexpr int kDefaultMaxPasses = 3;
constexpr double kDefaultEtaTarget = 0.05;

MeshOptions mesh_options_from_study(const json& study) {
    MeshOptions options;
    options.fineness = study.value("fineness", 0.5);
    options.order = study.value("mesh_order", study.value("order", 1));
    options.target_size_mm = study.value("target_size_mm", 0.0);
    options.min_size_mm = study.value("min_size_mm", 0.0);
    options.max_size_mm = study.value("max_size_mm", 0.0);
    options.learned_advisor = study.value("learned_advisor", false);
    options.advisor_max_dof = study.value("advisor_max_dof", uint32_t{0});
    return options;
}

}  // namespace

json fea_run_adaptive(const json& study) {
    try {
        const std::string step_path = study.value("step_path", std::string());
        if (step_path.empty())
            return json{{"ok", false},
                        {"error", "fea_adaptive: a step_path is required — an inline mesh "
                                  "cannot be refined"}};

        const json adapt = study.value("adapt", json::object());
        int max_passes = adapt.value("max_passes", kDefaultMaxPasses);
        max_passes = std::max(1, std::min(6, max_passes));
        const double eta_target = adapt.value("eta_target", kDefaultEtaTarget);
        if (!std::isfinite(eta_target) || eta_target <= 0.0)
            return json{{"ok", false},
                        {"error", "fea_adaptive: adapt.eta_target must be finite and > 0"}};

        const json material = study.value("material", json::object());
        const double e_mpa = material.value("E_mpa", 200000.0);
        const double nu = material.value("nu", 0.3);

        const MeshOptions options = mesh_options_from_study(study);
        std::string error;
        std::unique_ptr<AdaptiveMesher> mesher =
            AdaptiveMesher::open(step_path, options, error);
        if (!mesher)
            return json{{"ok", false}, {"error", "fea_adaptive: " + error}};

        // Each pass uses the ordinary inline-mesh solve route so boundary conditions,
        // materials, cancellation, and result formatting remain shared with static FEA.
        json pass_study = study;
        pass_study.erase("step_path");
        pass_study.erase("adapt");
        // The advisor already ran, on the geometry, in AdaptiveMesher::open. Left in
        // place these two keys make fea_run_static's inline route report the envelope it
        // owes an inline study — "advisor unavailable: learned advising requires STEP
        // geometry" — which for THIS study would be a false statement about a decision that
        // was made. The real envelope is attached to the result below.
        pass_study.erase("learned_advisor");
        pass_study.erase("advisor_max_dof");
        // The evidence file describes the FINAL mesh, so intermediate passes must not
        // each overwrite it with a coarser picture (and pay the write for nothing).
        pass_study.erase("vtu_path");

        json result;
        json passes = json::array();
        double global_eta = 0.0;
        bool target_met = false;
        std::string stop_reason;
        int passes_run = 0;

        MeshOut mesh;  // survives the loop: the overlay must be built on the FINAL mesh
        int prev_elems = 0;
        for (int pass = 0; pass < max_passes; ++pass) {
            if (!mesher->mesh(mesh, error))
                return json{{"ok", false},
                            {"error", "fea_adaptive: mesh pass " + std::to_string(pass + 1) +
                                          " failed: " + error}};
            // A refinement request that the mesher cannot honor (seed balls smaller than
            // its size floor, say) yields the same mesh again. Solving it a second time
            // would burn the pass budget to reproduce a number we already have, and would
            // report "budget exhausted" as if the run had been trying.
            if (pass > 0 && mesh.n_elems <= prev_elems) {
                stop_reason = "refinement_exhausted: the mesher produced no additional "
                              "elements at the requested refinement";
                break;
            }
            prev_elems = mesh.n_elems;

            pass_study["points"] = mesh.points;
            pass_study["tets"] = mesh.tets;
            pass_study["nodes_per_elem"] = mesh.nodes_per_elem;

            result = fea_run_static(pass_study);
            if (!result.value("ok", false)) {
                result["adapt"] = json{{"passes", passes},
                                       {"failed_pass", pass + 1},
                                       {"element_size_mm", mesher->element_size_mm()}};
                if (mesh.advisor) result["advisor"] = detail::mesh_advisor_json(*mesh.advisor);
                return result;
            }
            ++passes_run;

            std::vector<double> element_eta;
            const std::vector<double> disp =
                result.value("displacement", std::vector<double>());
            if (!mesher->estimate_error(disp, e_mpa, nu, element_eta, global_eta, error))
                return json{{"ok", false},
                            {"error", "fea_adaptive: error estimation failed after pass " +
                                          std::to_string(pass + 1) + ": " + error}};

            const json summary = result.value("summary", json::object());
            passes.push_back(json{{"pass", pass + 1},
                                  {"n_nodes", mesh.n_nodes},
                                  {"n_elems", mesh.n_elems},
                                  {"n_dof", result.value("n_dof", 0)},
                                  {"element_size_mm", mesher->element_size_mm()},
                                  {"global_eta", global_eta},
                                  {"max_von_mises_mpa", summary.value("max_von_mises_mpa", 0.0)},
                                  {"max_disp_mm", summary.value("max_disp_mm", 0.0)}});

            if (global_eta <= eta_target) {
                target_met = true;
                stop_reason = "eta_target_met";
                break;
            }
            if (pass + 1 == max_passes) {
                stop_reason = "pass_budget_exhausted";
                break;
            }
            std::string note;
            if (!mesher->refine(element_eta, note, error)) {
                if (!error.empty())
                    return json{{"ok", false},
                                {"error", "fea_adaptive: refinement failed after pass " +
                                              std::to_string(pass + 1) + ": " + error}};
                // Not an error: the mesh cannot usefully refine further. Report the last
                // solve with the reason, rather than pretending a budget was spent.
                stop_reason = "refinement_exhausted: " + note;
                break;
            }
        }

        result["adapt"] = json{{"passes", passes},
                               {"passes_run", passes_run},
                               {"global_eta", global_eta},
                               {"eta_target", eta_target},
                               {"eta_target_met", target_met},
                               {"stop_reason", stop_reason}};
        // The envelope the advisor actually produced for THIS geometry, in the same shape
        // every other study route reports it. mesh.advisor is the value
        // AdaptiveMesher::mesh attached on the final pass; it is absent exactly
        // when the study never opted in.
        if (mesh.advisor) result["advisor"] = detail::mesh_advisor_json(*mesh.advisor);
        // Provenance: the same object every route reports, corrected to name THIS
        // route — the passes ran through fea_run_static, which stamps its own name.
        if (result.value("ok", false)) {
            if (result.contains("provenance") && result["provenance"].is_object())
                result["provenance"]["solver"] = "chudware/fea_adaptive";
            // Each pass solved an INLINE mesh, whose quality probe has no CAD
            // tessellation to raycast, so the thin-wall witness has to come from the
            // mesher that produced the final mesh. Same correction shape as the solver
            // name above: an adaptive result must not under-report evidence an ordinary
            // STEP study carries.
            if (mesh.thin_wall_ratio > 0.0 && result.contains("mesh_quality") &&
                result["mesh_quality"].is_object())
                result["mesh_quality"]["thin_wall_ratio"] = mesh.thin_wall_ratio;
            std::string vtu_path, vtu_error;
            if (!detail::write_result_vtu(study, mesh.points, mesh.tets,
                                          mesh.nodes_per_elem, result, vtu_path,
                                          vtu_error))
                return json{{"ok", false}, {"error", "fea_adaptive: " + vtu_error}};
            if (!vtu_path.empty()) result["vtu_path"] = vtu_path;
        }
        // A result that missed its accuracy target must SAY so where a reader will see it,
        // not only in a nested block: an unconverged stress number that looks converged is
        // the exact failure this feature exists to prevent.
        if (!target_met) {
            json notes = result.value("solver_notes", json::array());
            notes.push_back("adaptive refinement stopped at global_eta=" +
                            std::to_string(global_eta) + " without reaching the target " +
                            std::to_string(eta_target) + " (" + stop_reason +
                            "): peak stress may still be under-resolved");
            result["solver_notes"] = notes;
        }

        // Display overlay, built here rather than by the RPC layer: the caller's usual
        // trick of re-fetching the cached mesh for `step_path` would hand it the FIRST
        // pass's mesh, and painting the final pass's stress field onto a coarser mesh is
        // a silently wrong picture. Best-effort — an overlay failure never fails a solve.
        {
            const std::vector<double> vm = result.value("von_mises", std::vector<double>());
            if (!vm.empty() && vm.size() == mesh.points.size() / 3) {
                const json summary = result.value("summary", json::object());
                const std::string overlay = static_overlay(
                    step_path, mesh.points, mesh.tets, mesh.nodes_per_elem, vm,
                    result.value("disp_magnitude", std::vector<double>()),
                    result.value("displacement", std::vector<double>()),
                    result.value("principal1", std::vector<double>()),
                    result.value("principal3", std::vector<double>()),
                    result.value("max_shear", std::vector<double>()),
                    study.value("material", json::object()).value("yield_mpa", 250.0), summary);
                if (!overlay.empty()) result["overlay_aicm"] = overlay;
            }
        }
        return result;
    } catch (const json::exception& e) {
        return json{{"ok", false},
                    {"error", std::string("fea_adaptive: invalid study JSON: ") + e.what()}};
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("fea_adaptive: ") + e.what()}};
    } catch (...) {
        return json{{"ok", false}, {"error", "fea_adaptive: unknown failure"}};
    }
}

}  // namespace chudware::sim
