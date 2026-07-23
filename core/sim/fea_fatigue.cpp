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
#include <cmath>
#include <string>
#include <vector>

#include "sim/fea_run.h"
#include "sim/mesher.h"
#include "sim/overlay.h"

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
    if (study.contains("points") && study["points"].is_array() &&
        !study["points"].empty()) {
        // Flatten [[x,y,z],...] OR an already-flat [x,y,z,...].
        for (const auto& p : study["points"]) {
            if (p.is_array()) {
                for (const auto& c : p) rm.points.push_back(c.get<double>());
            } else if (p.is_number()) {
                rm.points.push_back(p.get<double>());
            }
        }
        rm.tets = json_ints(study.value("tets", json::array()));
        rm.nodes_per_elem = study.value(
            "nodes_per_elem", study.value("mesh_order", 1) == 2 ? 10 : 4);
        rm.ok = !rm.points.empty();
        return rm;
    }
    if (study.contains("step_path") && study["step_path"].is_string()) {
        MeshOptions opts;
        opts.density = study.value("density", 0.5);
        opts.order = study.value("mesh_order", study.value("order", 1));
        opts.target_size_mm = study.value("target_size_mm", 0.0);
        opts.min_size_mm = study.value("min_size_mm", 0.0);
        opts.max_size_mm = study.value("max_size_mm", 0.0);
        opts.optimize = study.value("optimize", false);
        opts.algorithm3d = study.value("algorithm3d", 1);
        MeshOut m = mesh_step_cached(study["step_path"].get<std::string>(), opts);
        if (m.ok) {
            rm.points = m.points;
            rm.tets = m.tets;
            rm.nodes_per_elem = m.nodes_per_elem;
            rm.ok = true;
        }
    }
    return rm;
}

}  // namespace

nlohmann::json fea_run_fatigue(const nlohmann::json& study) {
    // Per-phase record (prof/phases.h): "static" (the reused static solve — which also opens
    // its own nested fea_static study with the finer split), "damage", "overlay".
    chudware::prof::phases::StudyScope _study("fea_fatigue");
    try {
        // ── 1. Static stress field (reuse the existing parity-proven path) ────
        json st;
        try {
            chudware::prof::phases::PhaseScope _p("static");
            st = fea_run_static(study);
        } catch (const std::exception& e) {
            return json{{"ok", false},
                        {"error", std::string("fatigue: static threw: ") + e.what()}};
        }
        if (!st.value("ok", false)) return st;  // propagate {ok:false,error}

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
        if (vm.empty()) return json{{"ok", false}, {"error", "fatigue: empty stress field"}};
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
            std::transform(load_type.begin(), load_type.end(), load_type.begin(),
                           ::tolower);
            if (load_type == "zero_based") {
                lmin = 0.0;
                lmax = 1.0;
            } else if (load_type == "ratio") {
                lmin = fat.value("load_ratio", 0.0);
                lmax = 1.0;
            } else {  // "reversed" + default
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

        notes.push_back("Se_corrected=" + std::to_string(Se) + " Se'=" +
                        std::to_string(Se_base) + " Sut=" + std::to_string(Sut) +
                        " Sy=" + std::to_string(Sy));
        notes.push_back("load factors lmin=" + std::to_string(lmin) + " lmax=" +
                        std::to_string(lmax));

        // ── 3. Per-node mean-stress theories ─────────────────────────────────
        // For each theory track the max equivalent-fully-reversed stress (worst node).
        // Remember the Goodman-critical node for crit_sigma_a / crit_sigma_m.
        const double BIG = 1e9;  // a sa_eq "fails" sentinel when a denom collapses.
        auto sa_eq_div = [BIG](double sa, double denom) {
            if (denom <= 1e-6) return BIG;
            return sa / denom;
        };

        double max_goodman = 0.0, max_gerber = 0.0, max_soderberg = 0.0;
        double max_asme = 0.0, max_morrow = 0.0;
        size_t crit_i = 0;
        double crit_sa = 0.0, crit_sm = 0.0;

        std::vector<double> goodman_eq(vm.size());  // for the overlay

        for (size_t i = 0; i < vm.size(); ++i) {
            const double sigma = std::max(vm[i], 0.0);  // sigma_peak
            const double sa = sigma * (lmax - lmin) / 2.0;
            const double sm = sigma * (lmax + lmin) / 2.0;
            const double sm_eff = std::max(sm, 0.0);  // compressive mean -> no penalty

            double sa_goodman, sa_gerber, sa_soderberg, sa_asme, sa_morrow;
            if (sa <= 0.0) {
                sa_goodman = sa_gerber = sa_soderberg = sa_asme = sa_morrow = 0.0;
            } else {
                sa_goodman = sa_eq_div(sa, 1.0 - sm_eff / Sut);
                sa_gerber = sa_eq_div(sa, 1.0 - (sm_eff / Sut) * (sm_eff / Sut));
                sa_soderberg = sa_eq_div(sa, 1.0 - sm_eff / Sy_eff);
                sa_asme = sa_eq_div(
                    sa, std::sqrt(std::max(1.0 - (sm_eff / Sy_eff) * (sm_eff / Sy_eff),
                                           1e-6)));
                sa_morrow = sa_eq_div(sa, 1.0 - sm_eff / Sf);
            }

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
        // Basquin S-N (Shigley, f=0.9): a = (f*Sut)^2/Se, b = -1/3*log10(f*Sut/Se).
        const double fSut = 0.9 * Sut;
        const double a = (fSut * fSut) / std::max(Se, 1e-9);
        const double b = -(1.0 / 3.0) * std::log10(fSut / std::max(Se, 1e-9));

        for (size_t i = 0; i < vm.size(); ++i) {
            const double seq = goodman_eq[i];
            double fos = Se / std::max(seq, 1e-9);
            if (fos < 0.0) fos = 0.0;
            if (fos > 15.0) fos = 15.0;
            fatigue_fos[i] = fos;

            double N;
            if (seq <= Se) {
                N = 1e6;  // run-out / (effectively) infinite life
            } else {
                N = std::pow(seq / a, 1.0 / b);  // b<0 so larger seq -> smaller N
            }
            if (!std::isfinite(N)) N = 1.0;
            if (N < 1.0) N = 1.0;
            if (N > 1e6) N = 1e6;
            life_log10[i] = std::log10(N);
        }

        chudware::prof::phases::add(
            "damage", std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t_damage0).count());

        // ── 5. Fatigue overlay (best-effort, only when a STEP path is given) ──
        std::string overlay_path;
        if (study.contains("step_path") && study["step_path"].is_string()) {
            chudware::prof::phases::PhaseScope _p("overlay");
            ResolvedMesh rm = resolve_mesh_for_overlay(study);
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
               {{"goodman", {{"sa_eq_mpa", round_nd(max_goodman, 4)}}},
                {"gerber", {{"sa_eq_mpa", round_nd(max_gerber, 4)}}},
                {"soderberg", {{"sa_eq_mpa", round_nd(max_soderberg, 4)}}},
                {"asme", {{"sa_eq_mpa", round_nd(max_asme, 4)}}},
                {"morrow", {{"sa_eq_mpa", round_nd(max_morrow, 4)}}}}}}},
            {"solver_notes", notes}};
        (void)crit_i;
        if (!overlay_path.empty()) res["overlay_aicm"] = overlay_path;
        return res;
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"error", std::string("fatigue: ") + e.what()}};
    } catch (...) {
        return json{{"ok", false}, {"error", "fatigue: unknown error"}};
    }
}

}  // namespace chudware::sim
