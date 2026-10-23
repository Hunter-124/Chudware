// core/piping/piping.cpp — the piping dispatch: port of the piping_tools.py
// tool bodies (minus MCP session bookkeeping and the kernel/CFD orchestration
// legs of piping_cfd_handoff, which stay MCP-side).
#include "piping/piping.h"

#include "piping/bom.h"
#include "piping/cfd.h"
#include "piping/duct.h"
#include "piping/edit.h"
#include "piping/fluid_properties.h"
#include "piping/route.h"
#include "piping/schema.h"
#include "piping/simhandoff.h"
#include "piping/solve.h"
#include "piping/spec.h"
#include "piping/vessel.h"

namespace chudware::piping {
namespace {

using pyutil::jerr;
using pyutil::pyround;
namespace sh = simhandoff;

ojson get(const ojson& params, const char* key) {
    return params.is_object() ? params.value(key, ojson(nullptr)) : ojson(nullptr);
}

// Extract (doc, boundary, fluid, errors) from an aicad.simhandoff/1 handoff —
// the consumer side of the doc-to-doc coupling shape (hydraulic→piping).
std::tuple<ojson, ojson, ojson, std::vector<std::string>> network_from_handoff(ojson env) {
    std::vector<std::string> errs;
    if (!env.is_object()) {
        return {nullptr, nullptr, nullptr, {"source_handoff must be an envelope object"}};
    }
    if (env.value("schema", ojson(nullptr)) != sh::SCHEMA &&
        env.value("simhandoff", ojson(nullptr)).is_object()) {
        env = env["simhandoff"];  // accept a whole producer result too
    }
    const auto verrs = sh::validate_envelope(env);
    if (!verrs.empty()) {
        for (const auto& e : verrs) errs.push_back("source_handoff invalid: " + e);
        return {nullptr, nullptr, nullptr, errs};
    }
    if (env.value("kind", ojson(nullptr)) != "handoff") {
        return {nullptr, nullptr, nullptr, {"source_handoff must be a handoff envelope"}};
    }
    if (env.value("coupling", ojson(nullptr)) != "hydraulic_to_piping") {
        errs.push_back("source_handoff coupling must be 'hydraulic_to_piping'");
    }
    const ojson source = env.value("source", ojson::object());
    const ojson target = env.value("target", ojson::object());
    if (!source.is_object() || source.value("domain", ojson(nullptr)) != "hydraulic") {
        errs.push_back("source_handoff source.domain must be 'hydraulic'");
    }
    if (!target.is_object() || target.value("domain", ojson(nullptr)) != "piping" ||
        target.value("tool", ojson(nullptr)) != "piping_solve") {
        errs.push_back(
            "source_handoff target must identify piping/piping_solve");
    }
    ojson doc = nullptr;
    for (const auto& g : env.value("geometry", ojson::array())) {
        if (g.value("kind", ojson(nullptr)) == "doc" &&
            g.value("inline", ojson(nullptr)).is_object()) {
            doc = g["inline"];
            break;
        }
    }
    if (doc.is_null()) {
        errs.push_back("source_handoff carries no inline piping doc "
                       "(geometry ref kind 'doc' with an inline payload)");
    }
    const ojson meta = env.value("meta", ojson::object());
    const ojson boundary = meta.is_object() ? meta.value("boundary", ojson(nullptr)) : ojson(nullptr);
    const ojson fluid = meta.is_object() ? meta.value("fluid", ojson(nullptr)) : ojson(nullptr);
    if (boundary.is_null()) errs.push_back("source_handoff.meta.boundary missing");
    return {doc, boundary, fluid, errs};
}

ojson node_menu(const ojson& nodes) {
    ojson out = ojson::array();
    for (const auto& n : nodes) {
        ojson row = ojson::object();
        row["node"] = n.value("id", ojson(nullptr));
        row["xyz"] = n.value("xyz", ojson(nullptr));
        out.push_back(std::move(row));
    }
    return out;
}

// The simhandoff verify envelope for the piping→CFD coupling (piping_tools
// _verify_envelope).
ojson verify_envelope(const ojson& result) {
    ojson chk = sh::check("pressure_drop", "kPa", result["dp_1d_kpa"], result["dp_3d_kpa"],
                          result["deviation_pct"], result["tolerance_pct"],
                          result["pass"].get<bool>());
    ojson checks = ojson::array({std::move(chk)});
    ojson advisories = nullptr;
    const ojson vlim = result.value("velocity_limits", ojson(nullptr));
    if (vlim.is_object() && !vlim.value("violations", ojson::array()).empty()) {
        advisories = ojson::array(
            {ojson::object({{"name", "velocity_limit"}, {"detail", vlim}})});
    }
    return sh::build_verify("piping_to_cfd", sh::study_ref("piping", "piping_solve"),
                            sh::study_ref("cfd", "run_cfd_flow"), checks,
                            result.value("verdict", std::string()), nullptr, advisories,
                            pyutil::py_truthy(result.value("warnings", ojson::array()))
                                ? result["warnings"]
                                : ojson(nullptr));
}

ojson m_piping_solve(const ojson& params) {
    ojson d = get(params, "doc");
    ojson bc = get(params, "boundary");
    ojson fl = get(params, "fluid");
    ojson pp = get(params, "pumps");
    ojson env = get(params, "source_handoff");
    const std::string network_type = get(params, "network_type").is_string()
                                         ? get(params, "network_type").get<std::string>()
                                         : "liquid";

    ojson handoff_id = nullptr;
    if (!env.is_null()) {
        auto [hdoc, hbc, hfl, herrs] = network_from_handoff(env);
        if (!herrs.empty()) {
            ojson out = jerr("could not consume source_handoff",
                             "pass a hydraulic_piping_handoff result or its simhandoff "
                             "envelope");
            ojson errs = ojson::array();
            for (const auto& e : herrs) errs.push_back(e);
            out["errors"] = std::move(errs);
            return out;
        }
        if (d.is_null()) d = hdoc;
        if (bc.is_null()) bc = hbc;
        if (fl.is_null()) fl = hfl;
        const ojson inner = env.value("schema", ojson(nullptr)) != sh::SCHEMA
                                ? env.value("simhandoff", ojson(nullptr))
                                : env;
        handoff_id = inner.is_object() ? inner.value("id", ojson(nullptr)) : ojson(nullptr);
    }
    if (d.is_null()) {
        return jerr("doc is required (directly or via source_handoff)",
                    "pass doc=<aicad.piping/1> or source_handoff=<handoff envelope>");
    }

    const std::vector<std::string> verrs =
        d.is_object() ? validate_piping(d) : std::vector<std::string>{"doc is not an object"};
    if (!verrs.empty()) {
        ojson out = jerr("piping document failed schema validation",
                         "fix the listed errors (see contracts/piping_schema.md)");
        ojson errs = ojson::array();
        for (const auto& e : verrs) errs.push_back(e);
        out["errors"] = std::move(errs);
        return out;
    }
    if (bc.is_null()) {
        return jerr("boundary conditions are required",
                    "e.g. [{\"node\":\"n0\",\"pressure\":300},{\"node\":\"n1\",\"demand\":50}]");
    }

    ojson res = solve(d, bc, fl, pp, network_type);
    if (!res.value("ok", false)) {
        if (!res.contains("next_actions")) {
            res["next_actions"] = ojson::array(
                {"check every run endpoint meets another run or a boundary node",
                 "ensure at least one boundary fixes pressure/head"});
        }
        return res;
    }
    res["node_menu"] = node_menu(res["nodes"]);
    if (!handoff_id.is_null()) res["handoff_id"] = handoff_id;
    return res;
}

ojson m_piping_report(const ojson& params) {
    const ojson d = get(params, "doc");
    const ojson result = get(params, "result");
    const ojson bc = get(params, "boundary");
    const ojson fl = get(params, "fluid");
    const ojson pp = get(params, "pumps");
    if (d.is_null()) {
        // The session fallback (report on the last piping_solve) is MCP-side
        // state; the core needs the doc (+ result or boundary) explicitly.
        return jerr("no prior piping_solve to report on",
                    "call piping_solve first, or pass doc + boundary here");
    }
    const std::vector<std::string> verrs =
        d.is_object() ? validate_piping(d) : std::vector<std::string>{"doc is not an object"};
    if (!verrs.empty()) {
        ojson out = jerr("piping document failed schema validation");
        ojson errs = ojson::array();
        for (const auto& e : verrs) errs.push_back(e);
        out["errors"] = std::move(errs);
        return out;
    }
    if (result.is_null() && bc.is_null()) {
        return jerr("boundary conditions are required to solve before reporting",
                    "pass boundary, e.g. [{\"node\":\"n0\",\"pressure\":300}, ...]");
    }
    return pressure_drop_report(d, result, bc, fl, pp);
}

ojson m_piping_operating_point(const ojson& params) {
    return operating_point(get(params, "pump_curve"), get(params, "system"),
                           get(params, "doc"), get(params, "boundary"), get(params, "fluid"),
                           get(params, "q_ref_m3h"), get(params, "npshr_curve"),
                           get(params, "suction"),
                           get(params, "speed_ratio").is_number()
                               ? get(params, "speed_ratio").get<double>()
                               : 1.0);
}

ojson m_piping_fluid(const ojson& params) {
    const double T = get(params, "T").is_number() ? get(params, "T").get<double>() : 293.15;
    const double P =
        get(params, "P").is_number() ? get(params, "P").get<double>() : P_ATM;
    return fluid_properties(get(params, "name"), T, P);
}

ojson m_piping_cfd_boundary(const ojson& params) {
    return cfd_boundary_from_solve(get(params, "solve_result"), get(params, "inlet_run_id"),
                                   get(params, "outlet_node_id"));
}

ojson m_piping_verify_cfd(const ojson& params) {
    const ojson sr = get(params, "solve_result");
    const ojson cr = get(params, "cfd_result");
    const double tol = get(params, "tolerance_pct").is_number()
                           ? get(params, "tolerance_pct").get<double>()
                           : 20.0;
    if (sr.is_null()) {
        return jerr("no prior piping_solve result in session",
                    "call piping_solve or piping_cfd_handoff first");
    }
    if (cr.is_null()) {
        return jerr("cfd_result is required",
                    "pass the result from run_cfd_flow or piping_cfd_handoff['cfd']");
    }
    ojson result = verify_pressure_drop(sr, cr, tol, get(params, "run_id"));
    if (result.value("ok", false)) {
        result = sh::attach(std::move(result), verify_envelope(result));
    }
    return result;
}

}  // namespace

bool call(const std::string& method, const ojson& params, ojson& result) {
    if (method == "piping_edit") {
        result = piping_edit(params);
        return true;
    }
    if (method == "piping_fitting_k") {
        result = piping_fitting_k(params);
        return true;
    }
    if (method == "piping_solve") {
        result = m_piping_solve(params);
        return true;
    }
    if (method == "piping_report") {
        result = m_piping_report(params);
        return true;
    }
    if (method == "piping_operating_point") {
        result = m_piping_operating_point(params);
        return true;
    }
    if (method == "piping_fluid") {
        result = m_piping_fluid(params);
        return true;
    }
    if (method == "piping_cfd_boundary") {
        result = m_piping_cfd_boundary(params);
        return true;
    }
    if (method == "piping_verify_cfd") {
        result = m_piping_verify_cfd(params);
        return true;
    }
    if (method == "piping_route") {
        result = piping_route(params);
        return true;
    }
    if (method == "piping_spec_check") {
        result = piping_spec_check(params);
        return true;
    }
    if (method == "piping_bom") {
        result = piping_bom(params);
        return true;
    }
    if (method == "vessel_size") {
        result = vessel_size(params);
        return true;
    }
    if (method == "duct_size") {
        result = duct_size(params);
        return true;
    }
    return false;
}

}  // namespace chudware::piping
