// core/piping/cfd.cpp — port of engine_oracle/piping/cfd_handoff.py (the
// kernel-free functions). Contract-shaped soft failures throughout.
#include "piping/cfd.h"

#include <cmath>

namespace aicad::piping {
namespace {

using pyutil::fmt_f;
using pyutil::jerr;
using pyutil::py_repr;
using pyutil::pyround;

double num(const ojson& v, double def = 0.0) {
    return v.is_number() ? v.get<double>() : def;
}

}  // namespace

ojson cfd_boundary_from_solve(const ojson& solve_result, const ojson& inlet_run_id,
                              const ojson& outlet_node_id) {
    if (!solve_result.value("ok", false)) {
        return jerr("solve_result is not ok — cannot extract CFD boundary conditions",
                    "run piping_solve first and pass a successful result");
    }

    const ojson links = solve_result.value("links", ojson::array());
    const ojson nodes = solve_result.value("nodes", ojson::array());
    if (links.empty()) {
        return jerr("solve_result has no links", "check the piping document has runs");
    }
    if (nodes.empty()) return jerr("solve_result has no nodes");

    // pick inlet link
    ojson inlet_link = nullptr;
    if (!inlet_run_id.is_null()) {
        for (const auto& lk : links)
            if (lk.value("id", ojson(nullptr)) == inlet_run_id) inlet_link = lk;
        if (inlet_link.is_null()) {
            ojson ids = ojson::array();
            for (const auto& lk : links) ids.push_back(lk.value("id", ojson(nullptr)));
            return jerr("inlet_run_id " + py_repr(inlet_run_id) + " not found in solve links",
                        "available link ids: " + py_repr(ids));
        }
    } else {
        double best = -1.0;
        for (const auto& lk : links) {
            const double f = std::abs(num(lk.value("flow_m3h", ojson(0.0))));
            if (inlet_link.is_null() || f > best) {
                best = f;
                inlet_link = lk;
            }
        }
    }

    // pick outlet node: lowest pressure when not named
    ojson outlet_node = nullptr;
    if (!outlet_node_id.is_null()) {
        for (const auto& nd : nodes)
            if (nd.value("id", ojson(nullptr)) == outlet_node_id) outlet_node = nd;
        if (outlet_node.is_null()) {
            ojson ids = ojson::array();
            for (const auto& nd : nodes) ids.push_back(nd.value("id", ojson(nullptr)));
            return jerr("outlet_node_id " + py_repr(outlet_node_id) +
                            " not found in solve nodes",
                        "available node ids: " + py_repr(ids));
        }
    } else {
        double best = 0.0;
        for (const auto& nd : nodes) {
            const double p = num(nd.value("pressure_kpa", ojson(0.0)));
            if (outlet_node.is_null() || p < best) {
                best = p;
                outlet_node = nd;
            }
        }
    }

    // inlet node: the "from" end of the inlet link (highest pressure)
    const ojson inlet_node_id = inlet_link.value("from", ojson(nullptr));
    ojson inlet_node = nullptr;
    for (const auto& nd : nodes)
        if (nd.value("id", ojson(nullptr)) == inlet_node_id) inlet_node = nd;
    if (inlet_node.is_null()) {
        return jerr("inlet node " + py_repr(inlet_node_id) + " not found in solve result nodes");
    }

    const double inlet_v_ms = std::abs(num(inlet_link.value("velocity_ms", ojson(0.0))));
    const double inlet_p_kpa = num(inlet_node.value("pressure_kpa", ojson(0.0)));
    const double outlet_p_kpa = num(outlet_node.value("pressure_kpa", ojson(0.0)));
    const double dp_1d = inlet_p_kpa - outlet_p_kpa;
    const double ref_dia_mm = num(inlet_link.value("id_mm", ojson(0.0)));

    ojson out = ojson::object();
    out["ok"] = true;
    out["inlet_velocity_ms"] = pyround(inlet_v_ms, 4);
    out["inlet_pressure_kpa"] = pyround(inlet_p_kpa, 3);
    out["outlet_pressure_kpa"] = pyround(outlet_p_kpa, 3);
    out["pressure_drop_1d_kpa"] = pyround(dp_1d, 3);
    out["fluid"] = solve_result.value("fluid", ojson::object());
    out["ref_diameter_mm"] = pyround(ref_dia_mm, 3);
    out["inlet_link"] = inlet_link.value("id", ojson(nullptr));
    out["outlet_node"] = outlet_node.value("id", ojson(nullptr));
    out["hint"] =
        "Use inlet_velocity_ms as the CFD inlet BC; outlet_pressure_kpa as the "
        "CFD outlet pressure (relative); compare CFD pressure drop to "
        "pressure_drop_1d_kpa via piping_verify_cfd.";
    return out;
}

ojson verify_pressure_drop(const ojson& solve_result, const ojson& cfd_result,
                           double tolerance_pct, const ojson& run_id) {
    if (!solve_result.value("ok", false)) {
        return jerr("solve_result is not ok", "run piping_solve first");
    }
    if (!cfd_result.value("ok", false)) {
        return jerr("cfd_result is not ok", "run piping_cfd_handoff or run_cfd_flow first");
    }

    ojson warnings = ojson::array();

    // ── 1D pressure drop ─────────────────────────────────────────────────────
    const ojson links = solve_result.value("links", ojson::array());
    double dp_1d_kpa;
    if (!run_id.is_null()) {
        ojson target = nullptr;
        for (const auto& lk : links)
            if (lk.value("id", ojson(nullptr)) == run_id) target = lk;
        if (target.is_null()) {
            ojson ids = ojson::array();
            for (const auto& lk : links) ids.push_back(lk.value("id", ojson(nullptr)));
            return jerr("run_id " + py_repr(run_id) + " not found in solve links",
                        "available: " + py_repr(ids));
        }
        dp_1d_kpa = num(target.value("dp_kpa", ojson(0.0)));
    } else {
        // Total network drop: sum all link dp_kpa (conservative: worst-case path)
        dp_1d_kpa = 0.0;
        for (const auto& lk : links) dp_1d_kpa += std::abs(num(lk.value("dp_kpa", ojson(0.0))));
    }
    if (dp_1d_kpa <= 0.0) {
        warnings.push_back("1D pressure drop is zero or negative — result may be trivial");
    }

    // ── 3D CFD pressure drop (dP = F_drag / A_bore, or Cd x ½ρV² fallback) ───
    const double drag_n = num(cfd_result.value("drag_n", ojson(0.0)));
    double area_mm2 = num(cfd_result.value("frontal_area_mm2", ojson(nullptr)));
    if (area_mm2 == 0.0) area_mm2 = num(cfd_result.value("ref_area_mm2", ojson(nullptr)));

    double dp_3d_kpa;
    if (drag_n == 0.0 || area_mm2 == 0.0) {
        const double cd = num(cfd_result.value("cd", ojson(0.0)));
        const double v_ms = num(cfd_result.value("velocity_ms", ojson(0.0)));
        const ojson fluid = solve_result.value("fluid", ojson::object());
        const double rho = num(fluid.value("rho", ojson(998.2)), 998.2);
        if (cd > 0.0 && v_ms > 0.0) {
            dp_3d_kpa = 0.5 * rho * v_ms * v_ms * cd / 1000.0;
            warnings.push_back(
                "frontal_area_mm2 not in CFD result — pressure drop estimated from "
                "Cd × ½ρV² (approximate; prefer internal-flow CFD mode)");
        } else {
            return jerr(
                "CFD result has no drag_n/frontal_area_mm2 or cd/velocity_ms — "
                "cannot derive pressure drop",
                "run run_cfd_flow with the fluid-domain STEP imported as a CAD part");
        }
    } else {
        dp_3d_kpa = drag_n / (area_mm2 * 1e-6) / 1000.0;  // Pa → kPa
    }

    // ── comparison ────────────────────────────────────────────────────────────
    double deviation_pct;
    if (dp_1d_kpa > 0.0) {
        deviation_pct = std::abs(dp_3d_kpa - dp_1d_kpa) / dp_1d_kpa * 100.0;
    } else {
        deviation_pct = dp_3d_kpa == 0.0 ? 0.0 : HUGE_VAL;
    }
    const bool passed = deviation_pct <= tolerance_pct;

    // ── velocity limits (advisory: 3 m/s for liquid service) ─────────────────
    constexpr double kVelocityLimitMs = 3.0;
    ojson violations = ojson::array();
    for (const auto& lk : links) {
        const double v = std::abs(num(lk.value("velocity_ms", ojson(0.0))));
        if (v > kVelocityLimitMs) {
            ojson row = ojson::object();
            row["link"] = lk.value("id", ojson(nullptr));
            row["velocity_ms"] = pyround(v, 3);
            row["limit_ms"] = kVelocityLimitMs;
            violations.push_back(std::move(row));
        }
    }
    if (!violations.empty()) {
        warnings.push_back(
            std::to_string(violations.size()) + " link(s) exceed " +
            pyutil::py_float(kVelocityLimitMs) +
            " m/s (erosion risk) — consider larger pipe or reduced flow");
    }

    std::string verdict;
    if (passed) {
        verdict = "PASS: 3D CFD dp (" + fmt_f(dp_3d_kpa, 2) + " kPa) within " +
                  fmt_f(tolerance_pct, 0) + "% of 1D solve dp (" + fmt_f(dp_1d_kpa, 2) +
                  " kPa) — Δ " + fmt_f(deviation_pct, 1) + "%";
    } else {
        verdict = "FAIL: 3D CFD dp (" + fmt_f(dp_3d_kpa, 2) + " kPa) deviates " +
                  fmt_f(deviation_pct, 1) + "% from 1D solve dp (" + fmt_f(dp_1d_kpa, 2) +
                  " kPa) — tolerance " + fmt_f(tolerance_pct, 0) + "%";
    }

    ojson vlim = ojson::object();
    vlim["checked"] = true;
    vlim["limit_ms"] = kVelocityLimitMs;
    vlim["violations"] = std::move(violations);

    ojson out = ojson::object();
    out["ok"] = true;
    out["pass"] = passed;
    out["dp_1d_kpa"] = pyround(dp_1d_kpa, 3);
    out["dp_3d_kpa"] = pyround(dp_3d_kpa, 3);
    out["deviation_pct"] = pyround(deviation_pct, 2);
    out["tolerance_pct"] = tolerance_pct;
    out["verdict"] = verdict;
    out["velocity_limits"] = std::move(vlim);
    out["warnings"] = std::move(warnings);
    out["hint"] =
        passed ? ojson(nullptr)
               : ojson("Large 1D vs 3D deviation is expected for elbows/tees (secondary "
                       "flows) — check that the CFD domain is the fluid bore, not the pipe "
                       "wall solid. If using run_cfd_flow on the bore STEP, ensure inlet "
                       "velocity matches the 1D solve velocity (use piping_cfd_handoff to "
                       "set BCs automatically).");
    return out;
}

}  // namespace aicad::piping
