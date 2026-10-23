// core/hydraulic/hydraulic.cpp — the hydraulic dispatch: port of the
// hydraulic_tools.py tool bodies (minus MCP session bookkeeping). The piping
// solve inside hydraulic_piping_handoff always runs live here (the C++ core
// IS the solver — there is no fluids-absent "solve_pending" stage).
#include <cmath>
#include "hydraulic/hydraulic.h"

#include "hydraulic/bom.h"
#include "hydraulic/handoff.h"
#include "hydraulic/motor.h"
#include "hydraulic/schema.h"
#include "hydraulic/sim.h"
#include "hydraulic/solve.h"
#include "hydraulic/templates.h"
#include "piping/simhandoff.h"
#include "piping/solve.h"

namespace chudware::hydraulic {
namespace {

using pyutil::fmt_f;
using pyutil::jerr;
using pyutil::py_float;
using pyutil::py_repr;
using pyutil::py_str;
using pyutil::py_truthy;
using pyutil::pyround;
namespace sh = piping::simhandoff;

ojson get(const ojson& params, const char* key) {
    return params.is_object() ? params.value(key, ojson(nullptr)) : ojson(nullptr);
}

double num(const ojson& v, double def) {
    return v.is_number() ? v.get<double>() : def;
}

std::vector<std::string> validate_doc_errs(const ojson& d) {
    return validate_hydraulic(d);
}

ojson errs_to_json(const std::vector<std::string>& errs) {
    ojson out = ojson::array();
    for (const auto& e : errs) out.push_back(e);
    return out;
}
ojson estimate_dp_by_run(const ojson& estimates) {
    ojson out = ojson::object();
    if (!estimates.is_object()) return out;
    for (auto it = estimates.begin(); it != estimates.end(); ++it) {
        const ojson dp = it->value("dp_kpa", ojson(nullptr));
        if (it->value("ok", false) && dp.is_number() &&
            std::isfinite(dp.get<double>())) {
            out[it.key()] = std::abs(dp.get<double>());
        }
    }
    return out;
}

ojson expectation_dp_by_run(const ojson& expectations) {
    ojson out = ojson::object();
    if (!expectations.is_array()) return out;
    for (const auto& exp : expectations) {
        const std::string quantity = exp.value("quantity", std::string());
        const size_t colon = quantity.find(':');
        const std::string run_id =
            colon == std::string::npos ? quantity : quantity.substr(colon + 1);
        const ojson value = exp.value("value", ojson(nullptr));
        if (!run_id.empty() && value.is_number() &&
            std::isfinite(value.get<double>())) {
            out[run_id] = std::abs(value.get<double>());
        }
    }
    return out;
}

ojson independent_line_margins(double supply_pressure_bar,
                               const ojson& load_pressure_bar,
                               const ojson& estimated_dp_kpa_by_run,
                               const ojson& solve_result) {
    ojson per_line = ojson::object();
    for (const auto& link : solve_result.value("links", ojson::array())) {
        const std::string run_id = link.value("id", std::string());
        if (run_id.empty()) continue;
        ojson estimated_bar = nullptr;
        const ojson estimated_kpa =
            estimated_dp_kpa_by_run.value(run_id, ojson(nullptr));
        if (estimated_kpa.is_number() &&
            std::isfinite(estimated_kpa.get<double>())) {
            estimated_bar = pyround(std::abs(estimated_kpa.get<double>()) / 100.0, 4);
        }
        ojson solved_bar = nullptr;
        const ojson solved_kpa = link.value("dp_kpa", ojson(nullptr));
        if (solved_kpa.is_number() && std::isfinite(solved_kpa.get<double>())) {
            solved_bar = pyround(std::abs(solved_kpa.get<double>()) / 100.0, 4);
        }
        ojson row =
            margin_update(supply_pressure_bar, load_pressure_bar, estimated_bar, solved_bar);
        row["run_id"] = run_id;
        row["topology_role"] = "independent_hose_line_case";
        per_line[run_id] = std::move(row);
    }

    ojson out = ojson::object();
    out["topology_model"] = "independent_hose_line_cases";
    out["per_line"] = std::move(per_line);
    // Preserve the old scalar field names as explicit unavailable values so a
    // caller cannot accidentally interpret independent cases as one series path.
    out["line_loss_estimate_bar"] = nullptr;
    out["line_loss_solved_bar"] = nullptr;
    out["pump_margin_estimate_bar"] = nullptr;
    out["pump_margin_solved_bar"] = nullptr;
    out["pump_margin_solved_pct"] = nullptr;
    out["diagnostic"] =
        "aggregate line loss and pump margin are not defined: each generated hose "
        "is an independent fixed-pressure/prescribed-flow case; inspect per_line";
    return out;
}

ojson m_new(const ojson& params) {
    const ojson name = get(params, "name");
    const ojson template_id = get(params, "template");
    return new_from_template(name.is_string() ? name.get<std::string>() : "Hydraulic Circuit",
                             template_id.is_null() ? "blank" : py_str(template_id));
}

ojson m_add_component(const ojson& params) {
    const ojson d = get(params, "doc");
    const ojson p = get(params, "params");
    const ojson s = get(params, "sourcing");

    if (d.is_null()) {
        return jerr("no active hydraulic circuit",
                    "call hydraulic_new first, or pass doc=<circuit>");
    }
    const auto base_errs = validate_doc_errs(d);
    if (!base_errs.empty()) {
        ojson out = jerr("hydraulic document failed schema validation");
        out["errors"] = errs_to_json(base_errs);
        return out;
    }
    if (!p.is_null() && !p.is_object()) return jerr("params must be an object");
    if (!s.is_null() && !s.is_object()) return jerr("sourcing must be an object");
    const double x = num(get(params, "x"), 0.0);
    const double y = num(get(params, "y"), 0.0);
    if (!std::isfinite(x) || !std::isfinite(y)) {
        return jerr("component x and y must be finite");
    }

    const std::string type = get(params, "type").is_string()
                                 ? get(params, "type").get<std::string>()
                                 : py_str(get(params, "type"));
    if (!is_component_type(type)) {
        ojson sorted = ojson::array();
        for (const auto& t : hyd_component_types_sorted()) sorted.push_back(t);
        return jerr("unknown component type " + py_repr(get(params, "type")),
                    "valid types: " + py_repr(sorted));
    }

    ojson doc = d;
    const std::string comp_id = next_id(doc, "c");
    const ojson name = get(params, "name");
    ojson comp = new_component(comp_id, type,
                               name.is_string() ? name.get<std::string>() : "",
                               x, y, p.is_object() ? p : ojson::object());
    if (py_truthy(s)) pyutil::update(comp["sourcing"], s);

    doc["components"].push_back(comp);

    const auto errs = validate_doc_errs(doc);
    if (!errs.empty()) {
        ojson invalid = jerr("component would make hydraulic document invalid");
        invalid["errors"] = errs_to_json(errs);
        return invalid;
    }
    ojson out = ojson::object();
    out["ok"] = true;
    out["component_id"] = comp_id;
    out["component"] = comp;
    out["doc_component_count"] = static_cast<int>(doc["components"].size());
    out["doc"] = doc;  // additive: the MCP layer stores it as the active doc
    out["warnings"] = errs_to_json(errs);
    out["hint"] = errs.empty() ? ojson(nullptr) : ojson("fix validation errors before proceeding");
    out["next_actions"] = ojson::array(
        {"call hydraulic_add_hose to connect component ports",
         "call hydraulic_size_cylinder / hydraulic_size_pump to size this component"});
    return out;
}

ojson m_add_hose(const ojson& params) {
    const ojson d = get(params, "doc");
    const ojson conns = get(params, "connections");

    if (d.is_null()) {
        return jerr("no active hydraulic circuit", "call hydraulic_new first");
    }
    if (!conns.is_array() || conns.size() < 2) {
        return jerr("connections must be a list of >= 2 port references",
                    "e.g. [{\"component_id\":\"c1\",\"port\":\"p1\"},"
                    "{\"component_id\":\"c2\",\"port\":\"p1\"}]");
    }
    const auto base_errs = validate_doc_errs(d);
    if (!base_errs.empty()) {
        ojson out = jerr("hydraulic document failed schema validation");
        out["errors"] = errs_to_json(base_errs);
        return out;
    }

    ojson doc = d;
    const std::string hose_id = next_id(doc, "h");
    const ojson net_name = get(params, "net_name");
    ojson hose = new_hose(hose_id, net_name.is_string() ? net_name.get<std::string>() : "",
                          conns);
    doc["hoses"].push_back(hose);

    const auto errs = validate_doc_errs(doc);
    if (!errs.empty()) {
        ojson invalid = jerr("hose would make hydraulic document invalid");
        invalid["errors"] = errs_to_json(errs);
        return invalid;
    }
    ojson out = ojson::object();
    out["ok"] = true;
    out["hose_id"] = hose_id;
    out["hose"] = hose;
    out["doc_hose_count"] = static_cast<int>(doc["hoses"].size());
    out["doc"] = doc;  // additive: the MCP layer stores it as the active doc
    out["warnings"] = errs_to_json(errs);
    out["hint"] = errs.empty() ? ojson(nullptr) : ojson("fix validation errors before proceeding");
    out["next_actions"] =
        ojson::array({"call hydraulic_bom to get the sourcing bill of materials"});
    return out;
}

ojson m_size_cylinder(const ojson& params) {
    const double pressure_bar = num(get(params, "pressure_bar"), 0.0);
    ojson result = size_cylinder(num(get(params, "bore_mm"), 0.0),
                                 num(get(params, "rod_mm"), 0.0), pressure_bar,
                                 num(get(params, "stroke_mm"), 0.0),
                                 num(get(params, "velocity_mm_s"), 50.0),
                                 num(get(params, "efficiency"), 0.95));
    if (!result.value("ok", false)) return result;
    // The tool wrapper adds warnings + a pump-sizing hint (setdefault order:
    // warnings then hint, appended after the solver's own keys).
    result["warnings"] = ojson::array();
    result["hint"] = "Pump must supply >= " +
                     fmt_f(result["flow_extend_lpm"].get<double>(), 2) + " L/min at " +
                     py_float(pressure_bar) +
                     " bar. Call hydraulic_size_pump with these values.";
    return result;
}

ojson m_size_pump(const ojson& params) {
    ojson result = size_pump(num(get(params, "flow_lpm"), 0.0),
                             num(get(params, "pressure_bar"), 0.0),
                             num(get(params, "efficiency"), 0.85),
                             num(get(params, "speed_rpm"), 1500.0));
    if (!result.value("ok", false)) return result;
    result["warnings"] = ojson::array();
    result["hint"] = "Select a pump with >= " +
                     fmt_f(result["displacement_cc_rev"].get<double>(), 2) +
                     " cc/rev displacement and motor >= " +
                     fmt_f(result["shaft_power_kw"].get<double>(), 2) + " kW shaft power.";
    return result;
}

ojson m_bom(const ojson& params) {
    const ojson d = get(params, "doc");
    if (d.is_null()) {
        return jerr("no active hydraulic circuit",
                    "call hydraulic_new first, or pass doc=<circuit>");
    }
    const auto errs = validate_doc_errs(d);
    if (!errs.empty()) {
        ojson out = jerr("hydraulic document failed schema validation",
                         "fix errors before extracting BOM");
        out["errors"] = errs_to_json(errs);
        return out;
    }

    const ojson lines = hydraulic_bom_lines(d);
    const ojson summary = sourcing_summary(d);
    const ojson missing = summary["missing_mpn"];

    ojson warnings = ojson::array();
    for (const auto& cid : missing)
        warnings.push_back("component " + py_str(cid) + " has no MPN");

    ojson out = ojson::object();
    out["ok"] = true;
    out["bom_lines"] = lines;
    out["summary"] = summary;
    out["warnings"] = std::move(warnings);
    out["hint"] = missing.empty()
                      ? ojson("All components sourced.")
                      : ojson(std::to_string(missing.size()) +
                              " component(s) missing MPN — add to sourcing fields.");
    out["next_actions"] = ojson::array(
        {"add mpn/manufacturer to unsourced components via hydraulic_add_component sourcing=",
         "pass bom_lines to bom_rollup at B-chain merge time"});
    return out;
}

ojson m_piping_handoff(const ojson& params) {
    const ojson d = get(params, "doc");
    const ojson ln = get(params, "lines");
    const ojson fl = get(params, "fluid");
    if (d.is_null()) {
        return jerr("no active hydraulic circuit",
                    "call hydraulic_new first, or pass doc=<circuit>");
    }
    const auto errs = validate_doc_errs(d);
    if (!errs.empty()) {
        ojson out = jerr("hydraulic document failed schema validation");
        out["errors"] = errs_to_json(errs);
        return out;
    }
    const double supply_pressure_bar = num(get(params, "supply_pressure_bar"), 0.0);
    const double flow_lpm = num(get(params, "flow_lpm"), 0.0);
    if (supply_pressure_bar <= 0 || flow_lpm <= 0) {
        return jerr("supply_pressure_bar and flow_lpm must be > 0");
    }
    if (!std::isfinite(supply_pressure_bar) || !std::isfinite(flow_lpm)) {
        return jerr("supply_pressure_bar and flow_lpm must be finite");
    }
    if (!std::isfinite(supply_pressure_bar * 100.0) ||
        !std::isfinite(flow_lpm * 0.06)) {
        return jerr("hydraulic-to-piping unit conversion overflowed");
    }
    const double default_id_mm = num(get(params, "default_id_mm"), 12.7);
    const double default_length_mm = num(get(params, "default_length_mm"), 1000.0);
    if (!std::isfinite(default_id_mm) || default_id_mm <= 0 ||
        !std::isfinite(default_length_mm) || default_length_mm <= 0) {
        return jerr("default_id_mm and default_length_mm must be finite and > 0");
    }
    const ojson load_pressure = get(params, "load_pressure_bar");
    if (!load_pressure.is_null() &&
        (!load_pressure.is_number() ||
         !std::isfinite(load_pressure.get<double>()) ||
         load_pressure.get<double>() < 0)) {
        return jerr("load_pressure_bar must be a finite number >= 0 or null");
    }
    const double tolerance_pct = num(get(params, "tolerance_pct"), 30.0);
    if (!std::isfinite(tolerance_pct) || tolerance_pct < 0) {
        return jerr("tolerance_pct must be finite and >= 0");
    }


    ojson net = hoses_to_piping(d, flow_lpm, supply_pressure_bar, ln,
                                default_id_mm, default_length_mm, fl);
    if (!net.value("ok", false)) return net;

    const ojson env = piping_handoff_envelope(d, net, supply_pressure_bar);

    ojson base = ojson::object();
    base["ok"] = true;
    base["piping_doc"] = net["piping_doc"];
    base["boundary"] = net["boundary"];
    base["fluid"] = net["fluid"];
    base["lines"] = net["lines"];
    base["estimates"] = net["estimates"];
    base["warnings"] = net["warnings"];
    if (net["lines"].size() > 1) {
        base["warnings"].push_back(
            "hose runs are independent line cases; no aggregate series pressure loss "
            "or pump margin is reported");
    }

    // The C++ core IS the solver: the live solve always runs here.
    ojson solve_res = piping::solve(net["piping_doc"], net["boundary"], net["fluid"]);
    if (!solve_res.value("ok", false)) {
        base["ok"] = false;
        base["reason"] =
            solve_res.value("reason", std::string("piping solve failed"));
        base["stage"] = "solve_failed";
        base["solve"] = solve_res;
        return sh::attach(std::move(base), env);
    }

    auto [verify_env, checks] = verify_piping_envelope(env, solve_res, tolerance_pct);

    ojson per_line = ojson::object();
    for (const auto& lk : solve_res["links"])
        per_line[lk["id"].get<std::string>()] = lk.value("dp_kpa", ojson(nullptr));

    ojson verify = ojson::object();
    verify["pass"] = verify_env["pass"];
    verify["verdict"] = verify_env["verdict"];
    verify["checks"] = checks;

    base["stage"] = "solved";
    base["solve"] = solve_res;
    base["per_line_dp_kpa"] = std::move(per_line);
    base["margin"] = independent_line_margins(
        supply_pressure_bar, load_pressure, estimate_dp_by_run(net["estimates"]),
        solve_res);
    base["verify"] = std::move(verify);
    base["next_actions"] = ojson::array(
        {"hydraulic_verify_piping() to get the full verify envelope",
         "inspect margin.per_line and resize the pump if any actual circuit-line "
         "margin is negative"});
    return sh::attach(std::move(base), env);
}

ojson m_verify_piping(const ojson& params) {
    ojson h = get(params, "handoff");
    const ojson srs = get(params, "solve_result");
    if (!h.is_null() && h.is_object() && h.value("schema", ojson(nullptr)) != sh::SCHEMA) {
        h = h.value("simhandoff", ojson(nullptr));
    }
    if (!h.is_object() || h.value("kind", ojson(nullptr)) != "handoff") {
        return jerr("no hydraulic→piping handoff envelope",
                    "call hydraulic_piping_handoff first, or pass "
                    "handoff=<result or its simhandoff envelope>");
    }
    const auto envelope_errs = sh::validate_envelope(h);
    if (!envelope_errs.empty() ||
        h.value("coupling", ojson(nullptr)) != "hydraulic_to_piping") {
        ojson out = jerr("invalid hydraulic→piping handoff envelope");
        out["errors"] = errs_to_json(envelope_errs);
        return out;
    }
    if (!srs.is_object() || !srs.value("ok", false) ||
        !srs.value("converged", false) ||
        !py_truthy(srs.value("links", ojson(nullptr)))) {
        return jerr("no converged piping solve result with links to verify against",
                    "pass a successful piping_solve output with converged=true");
    }

    const double tolerance_pct = num(get(params, "tolerance_pct"), 30.0);
    if (!std::isfinite(tolerance_pct) || tolerance_pct < 0) {
        return jerr("tolerance_pct must be finite and >= 0");
    }
    auto [env, checks] = verify_piping_envelope(h, srs, tolerance_pct);
    if (checks.empty()) {
        return jerr("no comparable lines (expectations vs solve links)",
                    "the handoff had no dP expectations or the solve "
                    "links don't match its run ids");
    }
    const ojson supply = get(params, "supply_pressure_bar");
    if (!supply.is_null() &&
        (!supply.is_number() || !std::isfinite(supply.get<double>()) ||
         supply.get<double>() <= 0)) {
        return jerr("supply_pressure_bar must be a finite number > 0 or null");
    }
    const ojson load = get(params, "load_pressure_bar");
    if (!load.is_null() &&
        (!load.is_number() || !std::isfinite(load.get<double>()) ||
         load.get<double>() < 0)) {
        return jerr("load_pressure_bar must be a finite number >= 0 or null");
    }
    ojson result = ojson::object();
    result["ok"] = true;
    result["pass"] = env["pass"];
    result["verdict"] = env["verdict"];
    result["checks"] = checks;
    result["tolerance_pct"] = tolerance_pct;
    result["margin"] =
        !supply.is_null()
            ? independent_line_margins(
                  supply.get<double>(), load,
                  expectation_dp_by_run(h.value("expectations", ojson::array())), srs)
            : ojson(nullptr);
    result["warnings"] = ojson::array();
    return sh::attach(std::move(result), env);
}

ojson m_fea_handoff(const ojson& params) {
    std::vector<double> ax;
    try {
        ax = unit3(get(params, "axis"));
    } catch (const std::exception& e) {
        return jerr(std::string("could not parse inputs: ") + e.what(),
                    "axis must be a non-zero [x,y,z] vector");
    }
    const double bore_mm = num(get(params, "bore_mm"), 0.0);
    const double max_pressure_bar = num(get(params, "max_pressure_bar"), 0.0);
    const double rod_mm = num(get(params, "rod_mm"), 0.0);
    const double efficiency = num(get(params, "efficiency"), 1.0);
    if (!std::isfinite(bore_mm) || !std::isfinite(max_pressure_bar) ||
        !std::isfinite(rod_mm) || !std::isfinite(efficiency)) {
        return jerr("cylinder geometry, pressure, and efficiency must be finite");
    }
    if (bore_mm <= 0 || max_pressure_bar <= 0) {
        return jerr("bore_mm and max_pressure_bar must be > 0");
    }
    if (rod_mm < 0 || rod_mm >= bore_mm) return jerr("rod_mm must be in [0, bore_mm)");
    if (!(0.0 < efficiency && efficiency <= 1.0)) return jerr("efficiency must be in (0, 1]");
    const ojson d = get(params, "doc");
    if (!d.is_null()) {
        const auto doc_errs = validate_doc_errs(d);
        if (!doc_errs.empty()) {
            ojson out = jerr("hydraulic document failed schema validation");
            out["errors"] = errs_to_json(doc_errs);
            return out;
        }
    }
    const std::string load_face = get(params, "load_face").is_string()
                                      ? get(params, "load_face").get<std::string>()
                                      : "the cylinder mount face";
    const std::string fixture_face = get(params, "fixture_face").is_string()
                                         ? get(params, "fixture_face").get<std::string>()
                                         : "the base";
    if (load_face.empty() || fixture_face.empty() || load_face == fixture_face) {
        return jerr("load_face and fixture_face must be non-empty distinct face queries");
    }
    const ojson forces = cylinder_mount_forces(bore_mm, rod_mm, max_pressure_bar, efficiency);
    if (!forces.value("extend_n", ojson(nullptr)).is_number() ||
        !forces.value("retract_n", ojson(nullptr)).is_number() ||
        !std::isfinite(forces["extend_n"].get<double>()) ||
        !std::isfinite(forces["retract_n"].get<double>())) {
        return jerr("cylinder mount-force calculation overflowed");
    }


    const ojson doc_id = d.is_object() ? d.value("id", ojson(nullptr)) : ojson(nullptr);
    const ojson target_doc_id = get(params, "target_doc_id");
    if (!target_doc_id.is_null() &&
        (!target_doc_id.is_string() || target_doc_id.get<std::string>().empty())) {
        return jerr("target_doc_id must be a non-empty string when provided");
    }
    const ojson env = fea_handoff_envelope(bore_mm, rod_mm, max_pressure_bar, ax,
                                           efficiency, load_face, fixture_face, doc_id,
                                           target_doc_id);

    ojson fea_args = ojson::object();
    for (const auto& cond : env["conditions"]) {
        if (cond.value("kind", ojson(nullptr)) != "force") continue;
        ojson args = ojson::object();
        args["fixtures"] = ojson::array({fixture_face});
        args["loads"] = ojson::array(
            {ojson::object({{"face", load_face}, {"force", cond["value"]}})});
        if (!target_doc_id.is_null()) args["doc_id"] = target_doc_id;
        fea_args[cond["case"].get<std::string>()] = std::move(args);
    }
    ojson cases = ojson::array();
    for (auto it = fea_args.begin(); it != fea_args.end(); ++it) cases.push_back(it.key());
    std::sort(cases.begin(), cases.end(),
              [](const ojson& a, const ojson& b) { return a.get<std::string>() < b.get<std::string>(); });

    const ojson force_digest = env["source"]["result_digest"];
    ojson result = ojson::object();
    result["ok"] = true;
    result["extend_force_n"] = force_digest["extend_force_n"];
    result["retract_force_n"] = force_digest["retract_force_n"];
    result["axis"] = ojson::array({ax[0], ax[1], ax[2]});
    result["cases"] = std::move(cases);
    result["fea_args"] = std::move(fea_args);
    result["warnings"] = ojson::array();
    result["hint"] = "run each case: run_fea_static(**fea_args['extend_max_pressure']) "
                     "etc., then hydraulic_verify_fea(fea_result=<summary>)";
    result["next_actions"] = ojson::array(
        {"run_fea_static(**fea_args[case]) for each load case",
         "hydraulic_verify_fea() to cross-check force magnitude/direction"});
    return sh::attach(std::move(result), env);
}

ojson m_verify_fea(const ojson& params) {
    ojson h = get(params, "handoff");
    const ojson fr = get(params, "fea_result");
    if (!h.is_null() && h.is_object() && h.value("schema", ojson(nullptr)) != sh::SCHEMA) {
        h = h.value("simhandoff", ojson(nullptr));
    }
    if (!h.is_object() || h.value("kind", ojson(nullptr)) != "handoff" ||
        !py_truthy(h.value("meta", ojson(nullptr)).is_object()
                       ? h["meta"].value("bore_mm", ojson(nullptr))
                       : ojson(nullptr))) {
        return jerr("no hydraulic→FEA handoff envelope",
                    "call hydraulic_fea_handoff first, or pass "
                    "handoff=<result or its simhandoff envelope>");
    }
    const auto envelope_errs = sh::validate_envelope(h);
    if (!envelope_errs.empty() ||
        h.value("coupling", ojson(nullptr)) != "hydraulic_to_fea") {
        ojson out = jerr("invalid hydraulic→FEA handoff envelope");
        out["errors"] = errs_to_json(envelope_errs);
        return out;
    }
    const double tolerance_pct = num(get(params, "tolerance_pct"), 0.5);
    if (!std::isfinite(tolerance_pct) || tolerance_pct < 0) {
        return jerr("tolerance_pct must be finite and >= 0");
    }
    const ojson env = verify_fea_envelope(h, tolerance_pct, fr);
    ojson result = ojson::object();
    result["ok"] = true;
    result["pass"] = env["pass"];
    result["verdict"] = env["verdict"];
    result["checks"] = env["checks"];
    result["warnings"] =
        py_truthy(env.value("warnings", ojson(nullptr))) ? env["warnings"] : ojson::array();
    return sh::attach(std::move(result), env);
}

}  // namespace

bool call(const std::string& method, const ojson& params, ojson& result) {
    if (method == "hydraulic_new") {
        result = m_new(params);
        return true;
    }
    if (method == "hydraulic_templates") {
        result = hydraulic_templates();
        return true;
    }
    if (method == "hydraulic_add_component") {
        result = m_add_component(params);
        return true;
    }
    if (method == "hydraulic_add_hose") {
        result = m_add_hose(params);
        return true;
    }
    if (method == "hydraulic_size_cylinder") {
        result = m_size_cylinder(params);
        return true;
    }
    if (method == "hydraulic_size_pump") {
        result = m_size_pump(params);
        return true;
    }
    if (method == "hydraulic_size_accumulator") {
        result = size_accumulator(num(get(params, "pre_charge_bar"), 0.0),
                                  num(get(params, "max_pressure_bar"), 0.0),
                                  num(get(params, "min_pressure_bar"), 0.0),
                                  num(get(params, "volume_stored_l"), 0.0));
        return true;
    }
    if (method == "hydraulic_valve_flow") {
        result = valve_flow(num(get(params, "Cv"), 0.0),
                            num(get(params, "pressure_drop_bar"), 0.0),
                            num(get(params, "fluid_sg"), 0.87));
        return true;
    }
    if (method == "hydraulic_simulate") {
        result = simulate(params);
        return true;
    }
    if (method == "hydraulic_size_motor") {
        result = size_motor(params);
        return true;
    }
    if (method == "pump_drive_size") {
        result = pump_drive_size(params);
        return true;
    }
    if (method == "hydraulic_bom") {
        result = m_bom(params);
        return true;
    }
    if (method == "hydraulic_piping_handoff") {
        result = m_piping_handoff(params);
        return true;
    }
    if (method == "hydraulic_verify_piping") {
        result = m_verify_piping(params);
        return true;
    }
    if (method == "hydraulic_fea_handoff") {
        result = m_fea_handoff(params);
        return true;
    }
    if (method == "hydraulic_verify_fea") {
        result = m_verify_fea(params);
        return true;
    }
    return false;
}

}  // namespace chudware::hydraulic
