// fea_fatigue.cpp — see fea_fatigue.h. High-cycle fatigue on top of the static FEA.
//
// Strategy: reuse fea_run_static(study) for the per-node von Mises field (the peak
// operating stress) and the n_nodes/n_elems/n_dof counts. Re-resolve the mesh points
// (inline or re-mesh the STEP, exactly as the libchudware overlay path does) so the
// fatigue overlay can map per-node results to the surface. The static numerics are
// untouched — this layer only post-processes the stress field.
//
// The host owns Marin correction + reliability + life. The engine evaluates EVERY
// standard mean-stress theory (Goodman / Gerber / Soderberg / ASME-elliptic /
// Morrow) and returns each theory's max equivalent-fully-reversed stress (sa_eq) at
// the worst node, plus the strength echoes, so the host can finish the calculation.
#include "sim/fea_fatigue.h"

#include "prof/phases.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <string>
#include <vector>

#include "sim/fea_run.h"
#include "sim/mesher.h"
#include "sim/overlay.h"
#include "sim/sim_cancel.h"

using nlohmann::json;

namespace chudware::sim {

namespace {

double round_nd(double v, int nd) {
    double scale = std::pow(10.0, nd);
    return std::round(v * scale) / scale;
}

std::vector<double> json_doubles(const json& j) {
    std::vector<double> v;
    if (j.is_array()) {
        v.reserve(j.size());
        for (const auto& x : j) {
            if (x.is_number()) v.push_back(x.get<double>());
        }
    }
    return v;
}

// Flatten a JSON int array (flat or nested) to ints — the tet-connectivity shape the
// boundary-surface overlay needs.
std::vector<int> json_ints(const json& j) {
    std::vector<int> v;
    if (j.is_array()) {
        for (const auto& x : j) {
            if (x.is_array()) {
                for (const auto& c : x) v.push_back(c.get<int>());
            } else if (x.is_number()) {
                v.push_back(x.get<int>());
            }
        }
    }
    return v;
}

// Recover the volume mesh for the overlay: node coordinates (n*3) PLUS the tet
// connectivity + element order (the boundary-surface overlay needs all three).
// Inline if the study carries them, else re-mesh the STEP deterministically (same
// density/order the solve used) so the mesh matches the solved result. .ok=false on
// failure.
struct ResolvedMesh {
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    bool ok = false;
};

ResolvedMesh resolve_mesh_for_overlay(const json& study) {
    ResolvedMesh rm;
    const bool has_step =
        study.contains("step_path") && study["step_path"].is_string() &&
        !study["step_path"].get<std::string>().empty();
    if (!has_step && study.contains("points") && study["points"].is_array() &&
        !study["points"].empty()) {
        for (const auto& p : study["points"]) {
            if (p.is_array()) {
                for (const auto& c : p) rm.points.push_back(c.get<double>());
            } else if (p.is_number()) {
                rm.points.push_back(p.get<double>());
            }
        }
        rm.tets = json_ints(study.value("tets", json::array()));
        rm.nodes_per_elem = study.value("nodes_per_elem", 4);
        rm.ok = !rm.points.empty() && !rm.tets.empty();
        return rm;
    }
    if (has_step) {
        MeshOptions opts;
        opts.fineness = study.value("fineness", 0.5);
        opts.density = study.value("density", -1.0);
        opts.order = study.value("mesh_order", study.value("order", 1));
        opts.curvature = study.value("curvature", true);
        opts.curv_elems_per_circle =
            study.value("curv_elems_per_circle", 12.0);
        opts.target_size_mm = study.value("target_size_mm", 0.0);
        opts.min_size_mm = study.value("min_size_mm", 0.0);
        opts.max_size_mm = study.value("max_size_mm", 0.0);
        opts.optimize = study.value("optimize", true);
        opts.algorithm3d = study.value("algorithm3d", 1);
        opts.structured =
            study.value("structured",
                        study.value("mesh_type", std::string()) == "hex");
        opts.auto_type = study.value("auto_type", false);
        opts.quad_surface = study.value("quad_surface", false);
        opts.fallback_to_tet = study.value("fallback_to_tet", true);
        opts.thin_wall_layers = study.value("thin_wall_layers", 4.0);
        MeshOut m =
            mesh_step_cached(study["step_path"].get<std::string>(), opts);
        if (m.ok) {
            rm.points = m.points;
            rm.tets = m.tets;
            rm.nodes_per_elem = m.nodes_per_elem;
            rm.ok = true;
        }
    }
    return rm;
}

class FatigueJobScope {
public:
    explicit FatigueJobScope(const json& study)
        : id_(study.is_object() && study.contains("job_id") &&
                      study["job_id"].is_string()
                  ? study["job_id"].get<std::string>()
                  : std::string()),
          token_(sim_job_register(id_)) {}
    ~FatigueJobScope() { sim_job_unregister(id_, token_); }
    const SimCancelToken& token() const { return token_; }
    bool cancelled() const {
        return token_ && token_->load(std::memory_order_relaxed);
    }

private:
    std::string id_;
    SimCancelToken token_;
};

}  // namespace

nlohmann::json fea_run_fatigue(const nlohmann::json& study) {
    // Per-phase record (prof/phases.h): "static" (the reused static solve — which also opens
    // its own nested fea_static study with the finer split), "damage", "overlay".
    chudware::prof::phases::StudyScope _study("fea_fatigue");
    FatigueJobScope post_job(study);
    try {
        if (!study.is_object())
            return json{{"ok", false},
                        {"error", "fatigue: study must be an object"}};
        if (study.contains("fatigue") && !study["fatigue"].is_object())
            return json{{"ok", false},
                        {"error", "fatigue: fatigue settings must be an object"}};
        if (study.contains("material") && !study["material"].is_object())
            return json{{"ok", false},
                        {"error", "fatigue: material must be an object"}};
        const json pre_fat = study.value("fatigue", json::object());
        const json pre_material = study.value("material", json::object());
        auto finite_positive_if_present =
            [](const json& object, const char* key) {
                if (!object.contains(key)) return true;
                return object[key].is_number() &&
                       std::isfinite(object[key].get<double>()) &&
                       object[key].get<double>() > 0.0;
            };
        for (const char* key :
             {"ultimate_mpa", "se_corrected_mpa", "se_base_mpa"})
            if (!finite_positive_if_present(pre_fat, key))
                return json{{"ok", false},
                            {"error", std::string("fatigue: ") + key +
                                          " must be finite and positive"}};
        if (pre_fat.contains("yield_mpa") &&
            (!pre_fat["yield_mpa"].is_number() ||
             !std::isfinite(pre_fat["yield_mpa"].get<double>())))
            return json{{"ok", false},
                        {"error", "fatigue: yield_mpa must be finite"}};
        for (const char* key :
             {"ultimate_mpa", "endurance_mpa", "yield_mpa"})
            if (!finite_positive_if_present(pre_material, key))
                return json{{"ok", false},
                            {"error", std::string("fatigue material: ") + key +
                                          " must be finite and positive"}};
        const double pre_sut =
            pre_fat.value("ultimate_mpa",
                          pre_material.value("ultimate_mpa", 0.0));
        if (!std::isfinite(pre_sut) || !(pre_sut > 0.0))
            return json{{"ok", false},
                        {"error", "fatigue: ultimate strength (Sut) required"}};
        double pre_se = pre_fat.value(
            "se_corrected_mpa",
            pre_material.value("endurance_mpa", 0.5 * pre_sut));
        if (!std::isfinite(pre_se) || !(pre_se > 0.0) ||
            !(pre_se < 0.9 * pre_sut))
            return json{{"ok", false},
                        {"error", "fatigue: corrected endurance limit must be finite, "
                                  "positive, and below 0.9*Sut"}};
        if (pre_fat.contains("lmin") || pre_fat.contains("lmax")) {
            if (!pre_fat.contains("lmin") || !pre_fat["lmin"].is_number() ||
                !pre_fat.contains("lmax") || !pre_fat["lmax"].is_number()) {
                return json{{"ok", false},
                            {"error", "fatigue: lmin and lmax must both be numeric"}};
            }
            const double lo = pre_fat["lmin"].get<double>();
            const double hi = pre_fat["lmax"].get<double>();
            if (!std::isfinite(lo) || !std::isfinite(hi) || !(hi > lo))
                return json{{"ok", false},
                            {"error", "fatigue: load factors must be finite with "
                                      "lmax > lmin"}};
            if (!std::isfinite((hi - lo) / 2.0) ||
                !std::isfinite((hi + lo) / 2.0))
                return json{{"ok", false},
                            {"error", "fatigue: load-factor range is not numerically "
                                      "representable"}};
        } else {
            if (pre_fat.contains("load_type") &&
                !pre_fat["load_type"].is_string())
                return json{{"ok", false},
                            {"error", "fatigue: load_type must be a string"}};
            std::string load_type =
                pre_fat.value("load_type", std::string("reversed"));
            std::transform(load_type.begin(), load_type.end(),
                           load_type.begin(), [](unsigned char c) {
                               return static_cast<char>(std::tolower(c));
                           });
            if (load_type != "reversed" && load_type != "zero_based" &&
                load_type != "ratio")
                return json{{"ok", false},
                            {"error", "fatigue: unsupported load_type '" +
                                          load_type + "'"}};
            if (load_type == "ratio") {
                if (!pre_fat.contains("load_ratio") ||
                    !pre_fat["load_ratio"].is_number() ||
                    !std::isfinite(pre_fat["load_ratio"].get<double>()) ||
                    !(pre_fat["load_ratio"].get<double>() < 1.0))
                    return json{{"ok", false},
                                {"error", "fatigue: ratio load needs finite "
                                          "load_ratio < 1"}};
            }
        }
        // ── 1. Static stress field (reuse the existing parity-proven path) ────
        json st;
        try {
            chudware::prof::phases::PhaseScope _p("static");
            st = detail::fea_run_static_with_cancel_token(study, post_job.token());
        } catch (const std::exception& e) {
            return json{{"ok", false},
                        {"error", std::string("fatigue: static threw: ") + e.what()}};
        }
        if (!st.value("ok", false)) return st;  // propagate {ok:false,error}
        if (post_job.cancelled())
            return json{{"ok", false}, {"error", "fatigue: cancelled"}};

        std::vector<double> vm = json_doubles(st.value("von_mises", json::array()));
        const int n_nodes = st.contains("n_nodes") && st["n_nodes"].is_number()
                                ? st["n_nodes"].get<int>()
                                : 0;
        const int n_elems = st.contains("n_elems") && st["n_elems"].is_number()
                                ? st["n_elems"].get<int>()
                                : 0;
        const int n_dof = st.contains("n_dof") && st["n_dof"].is_number()
                              ? st["n_dof"].get<int>()
                              : 0;
        if (n_nodes <= 0 || vm.size() != static_cast<size_t>(n_nodes))
            return json{{"ok", false},
                        {"error", "fatigue: stress field size does not match n_nodes"}};
        for (double stress : vm)
            if (!std::isfinite(stress) || stress < 0.0)
                return json{{"ok", false},
                            {"error", "fatigue: stress field contains a non-finite "
                                      "or negative value"}};
        const auto t_damage0 = std::chrono::steady_clock::now();  // phases: damage evaluation

        // ── 2. Fatigue params (study["fatigue"] + material fallbacks) ─────────
        // The host now owns Marin correction + reliability + method selection; the
        // engine just needs Sut, Sy and a corrected Se to evaluate every mean-stress
        // theory and report each theory's worst equivalent-fully-reversed stress.
        const json fat = study.value("fatigue", json::object());
        const json material = study.value("material", json::object());

        std::vector<std::string> notes;

        // Load factors lmin..lmax. Prefer explicit factors; otherwise derive from the
        // legacy load_type/load_ratio keys; default fully reversed {-1, 1}.
        double lmin = -1.0, lmax = 1.0;
        if (fat.contains("lmin") && fat["lmin"].is_number() && fat.contains("lmax") &&
            fat["lmax"].is_number()) {
            lmin = fat["lmin"].get<double>();
            lmax = fat["lmax"].get<double>();
        } else {
            std::string load_type = fat.value("load_type", std::string("reversed"));
            std::transform(load_type.begin(), load_type.end(),
                           load_type.begin(), [](unsigned char c) {
                               return static_cast<char>(std::tolower(c));
                           });
            if (load_type == "zero_based") {
                lmin = 0.0;
                lmax = 1.0;
            } else if (load_type == "ratio") {
                lmin = fat.value("load_ratio", 0.0);
                lmax = 1.0;
            } else {
                lmin = -1.0;
                lmax = 1.0;
            }
            notes.push_back("load factors derived from legacy load_type=" + load_type);
        }

        // Sut (required).
        double Sut = fat.value("ultimate_mpa", 0.0);
        if (Sut <= 0.0) Sut = material.value("ultimate_mpa", 0.0);
        if (Sut <= 0.0)
            return json{{"ok", false},
                        {"error", "fatigue: ultimate strength (Sut) required"}};

        // Corrected endurance limit Se (host-applied Marin), with fallbacks.
        double Se = fat.value("se_corrected_mpa", 0.0);
        if (Se <= 0.0) Se = material.value("endurance_mpa", 0.0);
        if (Se <= 0.0) {
            Se = 0.5 * Sut;
            notes.push_back("Se_corrected fell back to 0.5*Sut");
        }

        // Uncorrected Se' (echo only).
        double Se_base = fat.value("se_base_mpa", 0.0);
        if (Se_base <= 0.0) Se_base = Se;

        // Sy; theories that need it fall back to Sut when Sy<=0.
        double Sy = fat.value("yield_mpa", 0.0);
        if (Sy <= 0.0) Sy = material.value("yield_mpa", 0.0);
        const double Sy_eff = (Sy > 0.0) ? Sy : Sut;
        if (Sy <= 0.0)
            notes.push_back("Sy<=0: soderberg/asme fall back to Sut");

        // Morrow true-fracture-strength approximation Sf = Sut + 345 MPa.
        const double Sf = Sut + 345.0;
        if (!std::isfinite(Sf))
            return json{{"ok", false},
                        {"error", "fatigue: material strengths are not numerically "
                                  "representable"}};

        notes.push_back("Se_corrected=" + std::to_string(Se) + " Se'=" +
                        std::to_string(Se_base) + " Sut=" + std::to_string(Sut) +
                        " Sy=" + std::to_string(Sy));
        notes.push_back("load factors lmin=" + std::to_string(lmin) + " lmax=" +
                        std::to_string(lmax));

        const double amplitude_factor = (lmax - lmin) / 2.0;
        const double mean_factor = (lmax + lmin) / 2.0;
        if (!std::isfinite(amplitude_factor) || !std::isfinite(mean_factor))
            return json{{"ok", false},
                        {"error", "fatigue: load-factor range is not numerically "
                                  "representable"}};
        // ── 3. Per-node mean-stress theories ─────────────────────────────────
        // For each theory track the max equivalent-fully-reversed stress (worst node).
        // Remember the Goodman-critical node for crit_sigma_a / crit_sigma_m.
        // Preserve the historical finite failure sentinel for JSON compatibility,
        // but accompany it with an explicit per-theory capability status below.
        const double BIG = 1e9;
        auto sa_eq_div = [BIG](double sa, double denom, bool& limit) {
            if (denom <= 1e-6) {
                limit = true;
                return BIG;
            }
            return sa / denom;
        };

        double max_goodman = 0.0, max_gerber = 0.0, max_soderberg = 0.0;
        double max_asme = 0.0, max_morrow = 0.0;
        bool limit_goodman = false, limit_gerber = false;
        bool limit_soderberg = false, limit_asme = false, limit_morrow = false;
        size_t crit_i = 0;
        double crit_sa = 0.0, crit_sm = 0.0;

        std::vector<double> goodman_eq(vm.size());  // for the overlay

        for (size_t i = 0; i < vm.size(); ++i) {
            if ((i & 1023U) == 0U && post_job.cancelled())
                return json{{"ok", false}, {"error", "fatigue: cancelled"}};
            const double sigma = std::max(vm[i], 0.0);  // sigma_peak
            const double sa = sigma * amplitude_factor;
            const double sm = sigma * mean_factor;
            if (!std::isfinite(sa) || !std::isfinite(sm))
                return json{{"ok", false},
                            {"error", "fatigue: stress cycle overflowed"}};
            const double sm_eff = std::max(sm, 0.0);  // compressive mean -> no penalty

            double sa_goodman, sa_gerber, sa_soderberg, sa_asme, sa_morrow;
            if (sa <= 0.0) {
                sa_goodman = sa_gerber = sa_soderberg = sa_asme = sa_morrow = 0.0;
            } else {
                sa_goodman =
                    sa_eq_div(sa, 1.0 - sm_eff / Sut, limit_goodman);
                sa_gerber = sa_eq_div(
                    sa, 1.0 - (sm_eff / Sut) * (sm_eff / Sut),
                    limit_gerber);
                sa_soderberg =
                    sa_eq_div(sa, 1.0 - sm_eff / Sy_eff, limit_soderberg);
                const double asme_rad =
                    1.0 - (sm_eff / Sy_eff) * (sm_eff / Sy_eff);
                if (asme_rad <= 1e-6) {
                    limit_asme = true;
                    sa_asme = BIG;
                } else {
                    sa_asme = sa / std::sqrt(asme_rad);
                }
                sa_morrow =
                    sa_eq_div(sa, 1.0 - sm_eff / Sf, limit_morrow);
            }
            if (!std::isfinite(sa_goodman) || !std::isfinite(sa_gerber) ||
                !std::isfinite(sa_soderberg) || !std::isfinite(sa_asme) ||
                !std::isfinite(sa_morrow))
                return json{{"ok", false},
                            {"error", "fatigue: mean-stress correction overflowed"}};

            goodman_eq[i] = sa_goodman;

            if (sa_goodman > max_goodman) {
                max_goodman = sa_goodman;
                crit_i = i;
                crit_sa = sa;
                crit_sm = sm;
            }
            if (sa_gerber > max_gerber) max_gerber = sa_gerber;
            if (sa_soderberg > max_soderberg) max_soderberg = sa_soderberg;
            if (sa_asme > max_asme) max_asme = sa_asme;
            if (sa_morrow > max_morrow) max_morrow = sa_morrow;
        }

        // ── 4. Per-node overlay fields (Goodman sa_eq vs corrected Se) ────────
        std::vector<double> fatigue_fos(vm.size());
        std::vector<double> life_log10(vm.size());
        // Basquin S-N (Shigley, f=0.9), evaluated in log space so large but finite
        // strengths cannot overflow the historical a=(f*Sut)^2/Se coefficient.
        const double fSut = 0.9 * Sut;
        const double log_a = 2.0 * std::log10(fSut) - std::log10(Se);
        const double b = -(1.0 / 3.0) * std::log10(fSut / Se);

        for (size_t i = 0; i < vm.size(); ++i) {
            if ((i & 1023U) == 0U && post_job.cancelled())
                return json{{"ok", false}, {"error", "fatigue: cancelled"}};
            const double seq = goodman_eq[i];
            double fos = Se / std::max(seq, 1e-9);
            if (fos < 0.0) fos = 0.0;
            if (fos > 15.0) fos = 15.0;
            fatigue_fos[i] = fos;

            if (seq <= Se) {
                life_log10[i] = 6.0;  // run-out / (effectively) infinite life
            } else {
                const double log_n = (std::log10(seq) - log_a) / b;
                if (!std::isfinite(log_n))
                    return json{{"ok", false},
                                {"error", "fatigue: Basquin life evaluation became "
                                          "non-finite"}};
                life_log10[i] = std::clamp(log_n, 0.0, 6.0);
            }
        }

        chudware::prof::phases::add(
            "damage", std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t_damage0).count());

        // ── 5. Fatigue overlay (best-effort, only when a STEP path is given) ──
        std::string overlay_path;
        if (post_job.cancelled())
            return json{{"ok", false}, {"error", "fatigue: cancelled"}};
        if (study.contains("step_path") && study["step_path"].is_string()) {
            chudware::prof::phases::PhaseScope _p("overlay");
            ResolvedMesh rm = resolve_mesh_for_overlay(study);
            if (post_job.cancelled())
                return json{{"ok", false}, {"error", "fatigue: cancelled"}};
            if (rm.ok && rm.points.size() / 3 == vm.size()) {
                overlay_path = fatigue_overlay(study["step_path"].get<std::string>(),
                                               rm.points, rm.tets, rm.nodes_per_elem,
                                               fatigue_fos, life_log10, vm);
            } else if (rm.ok) {
                notes.push_back("overlay skipped: node count mismatch (" +
                                std::to_string(rm.points.size() / 3) + " pts vs " +
                                std::to_string(vm.size()) + " nodes)");
            }
        }

        if (post_job.cancelled())
            return json{{"ok", false}, {"error", "fatigue: cancelled"}};
        // ── 6. Return ────────────────────────────────────────────────────────
        json res = json{
            {"ok", true},
            {"n_nodes", n_nodes},
            {"n_elems", n_elems},
            {"n_dof", n_dof},
            {"summary",
             {{"se_corrected_mpa", Se},
              {"se_base_mpa", Se_base},
              {"sut_mpa", Sut},
              {"sy_mpa", Sy},
              {"lmin", lmin},
              {"lmax", lmax},
              {"crit_sigma_a", round_nd(crit_sa, 4)},
              {"crit_sigma_m", round_nd(crit_sm, 4)},
              {"theories",
               {{"goodman",
                 {{"sa_eq_mpa", round_nd(max_goodman, 4)},
                  {"status", limit_goodman ? "mean_stress_limit_exceeded" : "ok"}}},
                {"gerber",
                 {{"sa_eq_mpa", round_nd(max_gerber, 4)},
                  {"status", limit_gerber ? "mean_stress_limit_exceeded" : "ok"}}},
                {"soderberg",
                 {{"sa_eq_mpa", round_nd(max_soderberg, 4)},
                  {"status", limit_soderberg ? "mean_stress_limit_exceeded" : "ok"}}},
                {"asme",
                 {{"sa_eq_mpa", round_nd(max_asme, 4)},
                  {"status", limit_asme ? "mean_stress_limit_exceeded" : "ok"}}},
                {"morrow",
                 {{"sa_eq_mpa", round_nd(max_morrow, 4)},
                  {"status", limit_morrow ? "mean_stress_limit_exceeded" : "ok"}}}}}}},
            {"solver_notes", notes}};
        (void)crit_i;
        if (!overlay_path.empty()) res["overlay_aicm"] = overlay_path;
        if (post_job.cancelled())
            return json{{"ok", false}, {"error", "fatigue: cancelled"}};
        return res;
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("fatigue: ") + e.what()}};
    } catch (...) {
        return json{{"ok", false}, {"error", "fatigue: unknown error"}};
    }
}

}  // namespace chudware::sim
