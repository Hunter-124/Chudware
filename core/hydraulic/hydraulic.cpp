// core/hydraulic/hydraulic.cpp — the hydraulic dispatch: port of the
// hydraulic_tools.py tool bodies (minus MCP session bookkeeping). The piping
// solve inside hydraulic_piping_handoff always runs live here (the C++ core
// IS the solver — there is no fluids-absent "solve_pending" stage).
#include "hydraulic/hydraulic.h"

#include "hydraulic/bom.h"
#include "hydraulic/handoff.h"
#include "hydraulic/schema.h"
#include "hydraulic/solve.h"
#include "piping/simhandoff.h"
#include "piping/solve.h"

namespace aicad::hydraulic {
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

ojson m_new(const ojson& params) {
    const ojson name = get(params, "name");
    const ojson doc = new_hydraulic(name.is_string() ? name.get<std::string>()
                                                     : "Hydraulic Circuit");
    ojson out = ojson::object();
    out["ok"] = true;
    out["doc"] = doc;
    out["warnings"] = ojson::array();
    out["hint"] = "Use hydraulic_add_component to add ISO 1219 components.";
    out["next_actions"] = ojson::array(
        {"call hydraulic_add_component to add pumps, cylinders, valves, etc.",
         "call hydraulic_size_cylinder / hydraulic_size_pump to size components"});
    return out;
}

ojson m_add_component(const ojson& params) {
    const ojson d = get(params, "doc");
    const ojson p = get(params, "params");
    const ojson s = get(params, "sourcing");

    if (d.is_null()) {
        return jerr("no active hydraulic circuit",
                    "call hydraulic_new first, or pass doc=<circuit>");
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
                               num(get(params, "x"), 0.0), num(get(params, "y"), 0.0),
                               p.is_object() ? p : ojson::object());
    if (py_truthy(s)) pyutil::update(comp["sourcing"], s);

    doc["components"].push_back(comp);

    const auto errs = validate_doc_errs(doc);
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

    ojson doc = d;
    const std::string hose_id = next_id(doc, "h");
    const ojson net_name = get(params, "net_name");
    ojson hose = new_hose(hose_id, net_name.is_string() ? net_name.get<std::string>() : "",
                          conns);
    doc["hoses"].push_back(hose);

    const auto errs = validate_doc_errs(doc);
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
         "pass bom_lines to bom_rollup at B-chain merge time (T-E5 seam)"});
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

    ojson net = hoses_to_piping(d, flow_lpm, supply_pressure_bar, ln,
                                num(get(params, "default_id_mm"), 12.7),
                                num(get(params, "default_length_mm"), 1000.0), fl);
    if (!net.value("ok", false)) return net;

    const ojson env = piping_handoff_envelope(d, net, supply_pressure_bar);
    ojson est_loss_bar = nullptr;
    {
        double sum = 0.0;
        int n = 0;
        for (auto it = net["estimates"].begin(); it != net["estimates"].end(); ++it) {
            if (it->value("ok", false)) {
                sum += (*it)["dp_kpa"].get<double>();
                ++n;
            }
        }
        if (n > 0) est_loss_bar = pyround(sum / 100.0, 4);
    }

    ojson base = ojson::object();
    base["ok"] = true;
    base["piping_doc"] = net["piping_doc"];
    base["boundary"] = net["boundary"];
    base["fluid"] = net["fluid"];
    base["lines"] = net["lines"];
    base["estimates"] = net["estimates"];
    base["warnings"] = net["warnings"];

    // The C++ core IS the solver: the live solve always runs here.
    ojson solve_res = piping::solve(net["piping_doc"], net["boundary"], net["fluid"]);
    if (!solve_res.value("ok", false)) {
        base["stage"] = "solve_failed";
        base["solve"] = solve_res;
        return sh::attach(std::move(base), env);
    }

    const double tolerance_pct = num(get(params, "tolerance_pct"), 30.0);
    auto [verify_env, checks] = verify_piping_envelope(env, solve_res, tolerance_pct);
    double solved_sum = 0.0;
    for (const auto& lk : solve_res["links"])
        solved_sum += std::abs(num(lk.value("dp_kpa", ojson(0.0)), 0.0));
    const ojson solved_loss_bar = pyround(solved_sum / 100.0, 4);

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
    base["margin"] = margin_update(supply_pressure_bar, get(params, "load_pressure_bar"),
                                   est_loss_bar, solved_loss_bar);
    base["verify"] = std::move(verify);
    base["next_actions"] = ojson::array(
        {"hydraulic_verify_piping() to get the full verify envelope",
         "hydraulic_size_pump(flow_lpm, supply_pressure_bar) if the margin "
         "went negative — hose losses are now real, not estimated"});
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
    if (!srs.is_object() || !py_truthy(srs.value("links", ojson(nullptr)))) {
        return jerr("no piping solve result with links to verify against",
                    "pass solve_result=<piping_solve output>");
    }

    const double tolerance_pct = num(get(params, "tolerance_pct"), 30.0);
    auto [env, checks] = verify_piping_envelope(h, srs, tolerance_pct);
    if (checks.empty()) {
        return jerr("no comparable lines (expectations vs solve links)",
                    "the handoff had no dP expectations or the solve "
                    "links don't match its run ids");
    }
    double solved_sum = 0.0;
    for (const auto& lk : srs["links"])
        solved_sum += std::abs(num(lk.value("dp_kpa", ojson(0.0)), 0.0));
    const ojson solved_loss_bar = pyround(solved_sum / 100.0, 4);
    ojson est_loss_bar = nullptr;
    const ojson exps = h.value("expectations", ojson(nullptr));
    if (py_truthy(exps)) {
        double sum = 0.0;
        for (const auto& e : exps) sum += pyutil::to_double(e["value"]);
        est_loss_bar = pyround(sum / 100.0, 4);
    }
    const ojson supply = get(params, "supply_pressure_bar");
    ojson result = ojson::object();
    result["ok"] = true;
    result["pass"] = env["pass"];
    result["verdict"] = env["verdict"];
    result["checks"] = checks;
    result["tolerance_pct"] = tolerance_pct;
    result["margin"] = !supply.is_null()
                           ? margin_update(supply.get<double>(), get(params, "load_pressure_bar"),
                                           est_loss_bar, solved_loss_bar)
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
    if (bore_mm <= 0 || max_pressure_bar <= 0) {
        return jerr("bore_mm and max_pressure_bar must be > 0");
    }
    if (rod_mm < 0 || rod_mm >= bore_mm) return jerr("rod_mm must be in [0, bore_mm)");
    if (!(0.0 < efficiency && efficiency <= 1.0)) return jerr("efficiency must be in (0, 1]");

    const ojson d = get(params, "doc");
    const ojson doc_id = d.is_object() ? d.value("id", ojson(nullptr)) : ojson(nullptr);
    const ojson target_doc_id = get(params, "target_doc_id");
    const std::string load_face = get(params, "load_face").is_string()
                                      ? get(params, "load_face").get<std::string>()
                                      : "the cylinder mount face";
    const std::string fixture_face = get(params, "fixture_face").is_string()
                                         ? get(params, "fixture_face").get<std::string>()
                                         : "the base";
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

    const ojson forces = env["source"]["result_digest"];
    ojson result = ojson::object();
    result["ok"] = true;
    result["extend_force_n"] = forces["extend_force_n"];
    result["retract_force_n"] = forces["retract_force_n"];
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
    const double tolerance_pct = num(get(params, "tolerance_pct"), 0.5);
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
    if (method == "hydraulic_fmu_simulate") {
        result = fmu_simulate(get(params, "doc"), get(params, "fmu_path"),
                              num(get(params, "stop_time"), 10.0));
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

}  // namespace aicad::hydraulic
