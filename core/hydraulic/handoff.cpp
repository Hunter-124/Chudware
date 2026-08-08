// core/hydraulic/handoff.cpp — port of the module-level helpers in
// services/mcp/hydraulic_tools.py.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI
#include "hydraulic/handoff.h"

#include <cmath>
#include <map>
#include <limits>
#include <stdexcept>

#include "hydraulic/schema.h"
#include "piping/fluid_properties.h"
#include "piping/schema.h"
#include "piping/simhandoff.h"

namespace chudware::hydraulic {
namespace {

using pyutil::fmt_f;
using pyutil::py_float;
using pyutil::py_str;
using pyutil::py_truthy;
using pyutil::pyround;
namespace sh = piping::simhandoff;

// Lateral spacing (mm) between generated runs so endpoint snapping never
// merges two independent hose lines into one network node.
constexpr double kLineSpacingMm = 10000.0;

double num(const ojson& v, double def = 0.0) {
    return v.is_number() ? v.get<double>() : def;
}

}  // namespace

ojson hydraulic_oil() {
    ojson out = ojson::object();
    out["name"] = "hydraulic_oil_iso_vg32";
    out["rho"] = 870.0;
    out["mu"] = 0.0278;
    return out;
}

ojson estimate_line_dp(double flow_lpm, double id_mm, double length_mm, double rho,
                       double mu, double eps_m) {
    if (!std::isfinite(flow_lpm) || !std::isfinite(id_mm) ||
        !std::isfinite(length_mm) || flow_lpm <= 0 || id_mm <= 0 ||
        length_mm <= 0) {
        ojson out = ojson::object();
        out["ok"] = false;
        out["reason"] = "flow_lpm, id_mm, length_mm must all be finite and > 0";
        return out;
    }
    if (!std::isfinite(rho) || !std::isfinite(mu) || !std::isfinite(eps_m) ||
        rho <= 0 || mu <= 0 || eps_m < 0) {
        return pyutil::jerr("rho and mu must be finite and > 0; eps_m must be finite and >= 0");
    }
    const double d_m = id_mm / 1000.0;
    const double area = M_PI / 4.0 * d_m * d_m;
    const double q_m3s = flow_lpm / 60000.0;
    const double v = q_m3s / area;
    const double re = rho * v * d_m / mu;
    if (!std::isfinite(area) || area <= 0 || !std::isfinite(q_m3s) ||
        !std::isfinite(v) || v <= 0 || !std::isfinite(re) || re <= 0) {
        return pyutil::jerr("line pressure-drop calculation overflowed");
    }
    double f;
    std::string regime;
    if (re < 2300.0) {
        f = 64.0 / std::max(re, 1e-9);
        regime = "laminar";
    } else {
        f = 0.25 / std::pow(std::log10(eps_m / (3.7 * d_m) + 5.74 / std::pow(re, 0.9)), 2);
        regime = "turbulent (Swamee-Jain)";
    }
    const double dp_pa = f * (length_mm / 1000.0) / d_m * 0.5 * rho * v * v;
    if (!std::isfinite(f) || f <= 0 || !std::isfinite(dp_pa) || dp_pa < 0) {
        return pyutil::jerr("line pressure-drop calculation overflowed");
    }
    ojson out = ojson::object();
    out["ok"] = true;
    out["velocity_ms"] = pyround(v, 4);
    out["reynolds"] = pyround(re, 1);
    out["friction"] = pyround(f, 5);
    out["regime"] = regime;
    out["dp_kpa"] = pyround(dp_pa / 1000.0, 4);
    return out;
}

namespace {

// Resolve one hose's line geometry/flow: explicit override > hose fields >
// defaults.
ojson line_spec(const ojson& hose, const ojson& override_map, double default_id_mm,
                double default_length_mm, double default_flow_lpm) {
    auto pick = [&](const char* key, double def) {
        ojson v = override_map.is_object() ? override_map.value(key, ojson(nullptr))
                                           : ojson(nullptr);
        if (v.is_null() && hose.is_object()) v = hose.value(key, ojson(nullptr));
        if (v.is_null()) return def;
        return v.is_number() ? v.get<double>()
                             : std::numeric_limits<double>::quiet_NaN();
    };
    ojson out = ojson::object();
    out["id_mm"] = pick("id_mm", default_id_mm);
    out["length_mm"] = pick("length_mm", default_length_mm);
    out["flow_lpm"] = pick("flow_lpm", default_flow_lpm);
    return out;
}

}  // namespace

ojson hoses_to_piping(const ojson& hyd_doc, double flow_lpm, double supply_pressure_bar,
                      const ojson& lines, double default_id_mm, double default_length_mm,
                      const ojson& fluid) {
    const ojson hoses = hyd_doc.value("hoses", ojson(nullptr)).is_array()
                            ? hyd_doc["hoses"]
                            : ojson::array();
    if (hoses.empty()) {
        return pyutil::jerr("hydraulic circuit has no hoses", "call hydraulic_add_hose first");
    }
    const ojson raw_fluid = py_truthy(fluid) ? fluid : hydraulic_oil();
    ojson fl = piping::resolve_solver_fluid(raw_fluid);
    if (!fl.value("ok", true) && fl.contains("reason")) return fl;
    ojson pdoc = piping::new_piping(
        py_str(hyd_doc.value("name", ojson("Hydraulic"))) + " - hose runs");
    ojson boundary = ojson::array();
    ojson line_rows = ojson::array();
    ojson estimates = ojson::object();
    ojson warnings = ojson::array();
    const double supply_kpa = supply_pressure_bar * 100.0;

    int i = 0;
    for (const auto& hose : hoses) {
        const ojson hose_id = hose.value("id", ojson(nullptr));
        const ojson ov = lines.is_object() && hose_id.is_string() &&
                                 lines.contains(hose_id.get<std::string>()) &&
                                 py_truthy(lines[hose_id.get<std::string>()])
                             ? lines[hose_id.get<std::string>()]
                             : ojson::object();
        const ojson spec =
            line_spec(hose, ov, default_id_mm, default_length_mm, flow_lpm);
        const double id_mm = spec["id_mm"].get<double>();
        const double length_mm = spec["length_mm"].get<double>();
        const double line_flow_lpm = spec["flow_lpm"].get<double>();
        if (!std::isfinite(id_mm) || id_mm <= 0 ||
            !std::isfinite(length_mm) || length_mm <= 0 ||
            !std::isfinite(line_flow_lpm) || line_flow_lpm <= 0) {
            return pyutil::jerr(
                "hose " + py_str(hose_id) +
                " has invalid line geometry/flow",
                "id_mm, length_mm, and flow_lpm must each be finite and > 0");
        }
        const std::string rid = piping::next_id(pdoc, "r");
        const double y = i * kLineSpacingMm;
        const ojson name = hose.value("net_name", ojson(nullptr));
        ojson run = piping::new_pipe_run(
            rid,
            ojson::array({ojson::array({0.0, y, 0.0}),
                          ojson::array({spec["length_mm"], y, 0.0})}),
            pyround(id_mm / 25.4, 4), "40",
            py_truthy(name) && name.is_string() ? name.get<std::string>() : rid, id_mm);
        run["connections"] = hose.value("connections", ojson::array());
        pdoc["runs"].push_back(std::move(run));
        const double q_m3h = line_flow_lpm * 60.0 / 1000.0;
        boundary.push_back({{"run", rid}, {"end", "start"}, {"pressure", supply_kpa}});
        boundary.push_back({{"run", rid}, {"end", "end"}, {"demand", q_m3h}});
        const ojson est = estimate_line_dp(
            line_flow_lpm, id_mm, length_mm,
            num(fl.value("rho", ojson(nullptr))),
            num(fl.value("mu", ojson(nullptr))));
        if (!est.value("ok", false)) {
            warnings.push_back(py_str(hose_id) +
                               ": estimate failed: " + est.value("reason", std::string()));
        }
        if (est.value("ok", false) && est["velocity_ms"].get<double>() > 5.0) {
            warnings.push_back(
                py_str(hose_id) + ": velocity " +
                fmt_f(est["velocity_ms"].get<double>(), 2) +
                " m/s exceeds the 5 m/s general hydraulic-line advisory limit");
        }
        estimates[rid] = est;
        ojson row = ojson::object();
        row["hose_id"] = hose_id;
        row["net_name"] = hose.value("net_name", ojson(nullptr));
        row["run_id"] = rid;
        row["connections"] = hose.value("connections", ojson::array());
        pyutil::update(row, spec);
        row["flow_m3h"] = pyround(q_m3h, 6);
        line_rows.push_back(std::move(row));
        ++i;
    }

    const auto errs = piping::validate_piping(pdoc);
    if (!errs.empty()) {  // producer bug — surface loudly but contract-shaped
        ojson out = pyutil::jerr("generated piping doc failed schema validation");
        ojson earr = ojson::array();
        for (const auto& e : errs) earr.push_back(e);
        out["errors"] = std::move(earr);
        return out;
    }
    ojson out = ojson::object();
    out["ok"] = true;
    out["piping_doc"] = std::move(pdoc);
    out["boundary"] = std::move(boundary);
    out["lines"] = std::move(line_rows);
    out["fluid"] = std::move(fl);
    out["estimates"] = std::move(estimates);
    out["warnings"] = std::move(warnings);
    return out;
}

ojson piping_handoff_envelope(const ojson& hyd_doc, const ojson& net,
                              double supply_pressure_bar) {
    ojson conditions = ojson::array();
    conditions.push_back(sh::condition(
        "supply_pressure", "pressure", supply_pressure_bar * 100.0, "kPa",
        "hydraulic circuit operating pressure (supply_pressure_bar x 100)",
        "all run starts"));
    ojson expectations = ojson::array();
    for (const auto& row : net["lines"]) {
        const std::string rid = row["run_id"].get<std::string>();
        conditions.push_back(sh::condition(
            rid + "_flow", "flow", row["flow_m3h"], "m3/h",
            "hydraulic hose " + py_str(row["hose_id"]) + " (" +
                py_str(row["net_name"]) + ") flow_lpm converted by x0.06 to m3/h",
            rid));
        const ojson est = net["estimates"].value(rid, ojson::object());
        if (est.value("ok", false)) {
            expectations.push_back(sh::expectation(
                "pressure_drop:" + rid, est["dp_kpa"], "kPa",
                "per-hose Darcy estimate (estimate_line_dp, " +
                    est["regime"].get<std::string>() + ") for hose " +
                    py_str(row["hose_id"])));
        }
    }
    ojson digest = ojson::object();
    digest["hoses"] = static_cast<int>(net["lines"].size());
    digest["supply_pressure_bar"] = supply_pressure_bar;
    ojson meta = ojson::object();
    meta["boundary"] = net["boundary"];
    meta["fluid"] = net["fluid"];
    meta["lines"] = net["lines"];
    meta["topology_model"] =
        "independent_hose_line_cases (each run has its own fixed-pressure start "
        "and prescribed-flow end; component-port continuity is provenance only)";
    return sh::build_handoff(
        "hydraulic_to_piping",
        sh::study_ref("hydraulic", "hydraulic_piping_handoff",
                      hyd_doc.value("id", ojson(nullptr)), nullptr, nullptr, digest),
        sh::study_ref("piping", "piping_solve"),
        ojson::array({sh::geometry_ref("doc", "doc", nullptr, nullptr, net["piping_doc"],
                                       "hoses_to_piping", "mm")}),
        conditions, expectations.empty() ? ojson(nullptr) : expectations,
        py_truthy(net.value("warnings", ojson::array())) ? net["warnings"] : ojson(nullptr),
        meta);
}

ojson margin_update(double supply_pressure_bar, const ojson& load_pressure_bar,
                    const ojson& est_loss_bar, const ojson& solved_loss_bar) {
    ojson out = ojson::object();
    out["supply_pressure_bar"] = supply_pressure_bar;
    out["load_pressure_bar"] = load_pressure_bar;
    out["line_loss_estimate_bar"] = est_loss_bar;
    out["line_loss_solved_bar"] = solved_loss_bar;
    if (!load_pressure_bar.is_null()) {
        const double load = load_pressure_bar.get<double>();
        if (!est_loss_bar.is_null()) {
            out["pump_margin_estimate_bar"] =
                pyround(supply_pressure_bar - load - est_loss_bar.get<double>(), 4);
        }
        if (!solved_loss_bar.is_null()) {
            const double margin =
                pyround(supply_pressure_bar - load - solved_loss_bar.get<double>(), 4);
            out["pump_margin_solved_bar"] = margin;
            out["pump_margin_solved_pct"] =
                pyround(margin / supply_pressure_bar * 100.0, 2);
        }
    }
    return out;
}

std::pair<ojson, ojson> verify_piping_envelope(const ojson& handoff,
                                               const ojson& solve_res,
                                               double tolerance_pct) {
    std::map<std::string, ojson> links;
    for (const auto& lk : solve_res.value("links", ojson::array()))
        links[lk.value("id", std::string())] = lk;
    ojson checks = ojson::array();
    for (const auto& exp : handoff.value("expectations", ojson::array())) {
        const std::string q = exp.value("quantity", std::string());
        const std::string rid =
            q.find(':') != std::string::npos ? q.substr(q.find(':') + 1) : q;
        auto it = links.find(rid);
        const ojson est_j = exp.value("value", ojson(nullptr));
        if (it == links.end() || !est_j.is_number() ||
            !std::isfinite(est_j.get<double>()) ||
            !it->second.value("dp_kpa", ojson(nullptr)).is_number() ||
            !std::isfinite(it->second["dp_kpa"].get<double>())) {
            checks.push_back(
                sh::check(q, "kPa", est_j, nullptr, nullptr, tolerance_pct, false));
            continue;
        }
        const double solved = std::abs(it->second["dp_kpa"].get<double>());
        const double est = est_j.get<double>();
        const double base = std::max(std::abs(solved), 1e-9);
        const double dev = std::abs(solved - est) / base * 100.0;
        checks.push_back(sh::check(q, "kPa", est, pyround(solved, 4), pyround(dev, 2),
                                   tolerance_pct, dev <= tolerance_pct));
    }
    int n_pass = 0;
    for (const auto& c : checks)
        if (c["pass"].get<bool>()) ++n_pass;
    std::string verdict;
    if (!checks.empty() && n_pass == static_cast<int>(checks.size())) {
        verdict = "PASS: " + std::to_string(n_pass) + "/" + std::to_string(checks.size()) +
                  " hose-line dP estimates within " + py_str(tolerance_pct) +
                  "% of the piping 1D solve";
    } else {
        verdict = "FAIL: " + std::to_string(checks.size() - n_pass) + "/" +
                  std::to_string(checks.size()) + " hose-line dP estimates deviate more than " +
                  py_str(tolerance_pct) + "% from the piping 1D solve";
    }
    const ojson handoff_id = handoff.value("id", ojson(nullptr));
    ojson env = sh::build_verify("hydraulic_to_piping",
                                 sh::study_ref("hydraulic", "hydraulic_piping_handoff"),
                                 sh::study_ref("piping", "piping_solve", nullptr, nullptr,
                                               handoff_id),
                                 checks, verdict, handoff_id);
    return {env, checks};
}

ojson cylinder_mount_forces(double bore_mm, double rod_mm, double max_pressure_bar,
                            double efficiency) {
    const double a_bore = M_PI / 4.0 * std::pow(bore_mm * 1e-3, 2);
    const double a_ann =
        M_PI / 4.0 * (std::pow(bore_mm * 1e-3, 2) - std::pow(rod_mm * 1e-3, 2));
    const double p_pa = max_pressure_bar * 1e5;
    ojson out = ojson::object();
    out["extend_n"] = a_bore * p_pa * efficiency;
    out["retract_n"] = a_ann * p_pa * efficiency;
    return out;
}

std::vector<double> unit3(const ojson& axis) {
    const ojson src = axis.is_null() ? ojson::array({0.0, 0.0, 1.0}) : axis;
    if (!src.is_array() || src.size() != 3)
        throw std::runtime_error("axis must be a 3-vector");
    std::vector<double> v;
    for (const auto& c : src) {
        if (!c.is_number() || !std::isfinite(c.get<double>()))
            throw std::runtime_error("axis components must be finite numbers");
        v.push_back(c.get<double>());
    }
    double n = 0.0;
    for (double c : v) n += c * c;
    n = std::sqrt(n);
    if (!std::isfinite(n) || n <= 0)
        throw std::runtime_error("axis must be non-zero");
    for (double& c : v) c /= n;
    return v;
}

ojson fea_handoff_envelope(double bore_mm, double rod_mm, double max_pressure_bar,
                           const std::vector<double>& axis, double efficiency,
                           const std::string& load_face, const std::string& fixture_face,
                           const ojson& doc_id, const ojson& target_doc_id) {
    const ojson f =
        cylinder_mount_forces(bore_mm, rod_mm, max_pressure_bar, efficiency);
    ojson ext_vec = ojson::array();
    ojson ret_vec = ojson::array();
    for (double c : axis) {
        ext_vec.push_back(pyround(-c * f["extend_n"].get<double>(), 3));
        ret_vec.push_back(pyround(c * f["retract_n"].get<double>(), 3));
    }
    const std::string prov = "hydraulic cylinder bore " + py_float(bore_mm) + " mm rod " +
                             py_float(rod_mm) + " mm at max pressure " +
                             py_float(max_pressure_bar) + " bar (area x pressure x efficiency " +
                             py_float(efficiency) + ")";
    ojson conditions = ojson::array();
    conditions.push_back(sh::condition(
        "mount_force_extend", "force", ext_vec, "N",
        prov + " - extend, full-bore area, reaction into mount (-axis)", load_face,
        "extend_max_pressure"));
    conditions.push_back(sh::condition(
        "mount_force_retract", "force", ret_vec, "N",
        prov + " - retract, annulus area, reaction toward load (+axis)", load_face,
        "retract_max_pressure"));
    conditions.push_back(sh::condition(
        "mount_fixture", "fixture", ojson::object({{"type", "fixed"}}), "1",
        "mounting structure support (held fixed in every load case)", fixture_face));
    ojson expectations = ojson::array();
    expectations.push_back(sh::expectation("mount_force:extend_max_pressure",
                                           pyround(f["extend_n"].get<double>(), 3), "N",
                                           "pi/4*bore^2 x max pressure x efficiency"));
    expectations.push_back(sh::expectation("mount_force:retract_max_pressure",
                                           pyround(f["retract_n"].get<double>(), 3), "N",
                                           "pi/4*(bore^2-rod^2) x max pressure x efficiency"));
    ojson digest = ojson::object();
    digest["bore_mm"] = bore_mm;
    digest["rod_mm"] = rod_mm;
    digest["max_pressure_bar"] = max_pressure_bar;
    digest["efficiency"] = efficiency;
    digest["extend_force_n"] = pyround(f["extend_n"].get<double>(), 3);
    digest["retract_force_n"] = pyround(f["retract_n"].get<double>(), 3);
    ojson geometry = ojson::array();
    if (!target_doc_id.is_null()) {
        geometry.push_back(sh::geometry_ref("mount", "doc_id", nullptr, target_doc_id,
                                            nullptr, "caller-supplied mounting geometry"));
    }
    ojson meta = ojson::object();
    meta["axis"] = ojson::array({axis[0], axis[1], axis[2]});
    meta["bore_mm"] = bore_mm;
    meta["rod_mm"] = rod_mm;
    meta["max_pressure_bar"] = max_pressure_bar;
    meta["efficiency"] = efficiency;
    return sh::build_handoff(
        "hydraulic_to_fea",
        sh::study_ref("hydraulic", "hydraulic_fea_handoff", doc_id, nullptr, nullptr, digest),
        sh::study_ref("fea", "run_fea_static", target_doc_id), geometry, conditions,
        expectations, nullptr, meta);
}

ojson verify_fea_envelope(const ojson& handoff, double tolerance_pct,
                          const ojson& fea_result) {
    const ojson meta = handoff.value("meta", ojson(nullptr)).is_object()
                           ? handoff["meta"]
                           : ojson::object();
    auto finite_meta = [&](const char* key, double def, bool required) {
        const ojson value = meta.value(key, required ? ojson(nullptr) : ojson(def));
        return value.is_number() && std::isfinite(value.get<double>())
                   ? value.get<double>()
                   : std::numeric_limits<double>::quiet_NaN();
    };
    const double bore = finite_meta("bore_mm", 0.0, true);
    const double rod = finite_meta("rod_mm", 0.0, false);
    const double pressure = finite_meta("max_pressure_bar", 0.0, true);
    const double efficiency = finite_meta("efficiency", 1.0, false);

    ojson checks = ojson::array();
    ojson warnings = ojson::array();
    if (!std::isfinite(bore) || !std::isfinite(rod) ||
        !std::isfinite(pressure) || !std::isfinite(efficiency) ||
        bore <= 0 || rod < 0 || rod >= bore || pressure <= 0 ||
        efficiency <= 0 || efficiency > 1) {
        checks.push_back(sh::check("handoff_metadata", "1", meta, nullptr, nullptr,
                                   tolerance_pct, false));
    } else {
        const ojson f = cylinder_mount_forces(bore, rod, pressure, efficiency);
        const std::map<std::string, double> want = {
            {"extend_max_pressure", f["extend_n"].get<double>()},
            {"retract_max_pressure", f["retract_n"].get<double>()},
        };

        std::vector<double> axis;
        bool axis_ok = true;
        try {
            if (!meta.contains("axis"))
                throw std::runtime_error("axis is missing");
            axis = unit3(meta.value("axis", ojson(nullptr)));
        } catch (const std::exception& e) {
            axis_ok = false;
            warnings.push_back(std::string("invalid cylinder axis provenance: ") + e.what());
            checks.push_back(sh::check("cylinder_axis", "1",
                                       meta.value("axis", ojson(nullptr)), nullptr,
                                       nullptr, tolerance_pct, false));
        }

        std::map<std::string, std::vector<ojson>> vectors;
        for (const auto& cond : handoff.value("conditions", ojson::array())) {
            const ojson case_j = cond.value("case", ojson(nullptr));
            if (cond.value("kind", ojson(nullptr)) == "force" &&
                case_j.is_string() && want.count(case_j.get<std::string>())) {
                vectors[case_j.get<std::string>()].push_back(
                    cond.value("value", ojson(nullptr)));
            }
        }

        for (const auto& [case_s, expect] : want) {
            const auto it = vectors.find(case_s);
            const bool one = it != vectors.end() && it->second.size() == 1;
            const ojson vec = one ? it->second.front() : ojson(nullptr);
            bool vector_ok = vec.is_array() && vec.size() == 3;
            double mag2 = 0.0;
            if (vector_ok) {
                for (const auto& c : vec) {
                    if (!c.is_number() || !std::isfinite(c.get<double>())) {
                        vector_ok = false;
                        break;
                    }
                    mag2 += c.get<double>() * c.get<double>();
                }
            }
            if (!one || !vector_ok || !std::isfinite(mag2)) {
                checks.push_back(sh::check("mount_force:" + case_s, "N",
                                           pyround(expect, 3), nullptr, nullptr,
                                           tolerance_pct, false));
                continue;
            }
            const double mag = std::sqrt(mag2);
            const double dev =
                std::abs(mag - expect) / std::max(expect, 1e-9) * 100.0;
            checks.push_back(sh::check("mount_force:" + case_s, "N",
                                       pyround(expect, 3), pyround(mag, 3),
                                       pyround(dev, 3), tolerance_pct,
                                       dev <= tolerance_pct));
            if (axis_ok && mag > 0) {
                double dot = 0.0;
                for (size_t k = 0; k < axis.size(); ++k)
                    dot += axis[k] * vec[k].get<double>();
                const double cosang = std::abs(dot) / mag;
                if (cosang < 0.999) {
                    warnings.push_back(
                        case_s +
                        ": force vector not colinear with cylinder axis (|cos| = " +
                        fmt_f(cosang, 4) + ")");
                }
            }
        }
    }

    bool ok = !checks.empty();
    for (const auto& c : checks) ok = ok && c["pass"].get<bool>();
    const std::string verdict =
        ok ? "PASS: all " + std::to_string(checks.size()) +
                 " mount-load case force magnitudes match the independent area x pressure "
                 "recompute within " +
                 py_str(tolerance_pct) + "%"
           : "FAIL: mount-load cases are missing, ambiguous, invalid, or deviate from the "
             "independent area x pressure recompute";
    ojson advisories = nullptr;
    if (fea_result.is_object()) {
        const ojson summ_j = fea_result.value("summary", ojson(nullptr));
        const ojson summ = py_truthy(summ_j) ? summ_j : fea_result;
        ojson detail = ojson::object();
        for (const char* k : {"max_von_mises_mpa", "max_disp_mm", "min_fos"}) {
            if (summ.is_object() && summ.contains(k)) detail[k] = summ[k];
        }
        if (!detail.empty()) {
            advisories = ojson::array(
                {ojson::object({{"name", "fea_static_summary"}, {"detail", detail}})});
        }
    }
    return sh::build_verify("hydraulic_to_fea",
                            sh::study_ref("hydraulic", "hydraulic_fea_handoff"),
                            sh::study_ref("fea", "run_fea_static", nullptr, nullptr,
                                          handoff.value("id", ojson(nullptr))),
                            checks, verdict, handoff.value("id", ojson(nullptr)),
                            advisories,
                            warnings.empty() ? ojson(nullptr) : warnings);
}

}  // namespace chudware::hydraulic
