// Structural RPC dispatch — see structural_rpc.h.

#include "structural/structural_rpc.h"

#include <cmath>
#include <fstream>
#include <sstream>
#include <set>
#include <stdexcept>
#include <utility>

#include "structural/checks.h"
#include "structural/estimate.h"
#include "structural/model.h"
#include "structural/solver.h"

namespace chudware::structural {
namespace {

nlohmann::json err_env(const std::string& code, const std::string& message,
                       const std::string& detail = "") {
    return {{"code", code}, {"message", message}, {"detail", detail}};
}

// Resolve the structure document from params: inline "doc" object wins, else
// "doc_path" is read from disk (mirrors structural_tools._load_structure).
nlohmann::json doc_json_from_params(const nlohmann::json& params) {
    if (params.contains("doc") && params["doc"].is_object()) return params["doc"];
    const std::string path = params.value("doc_path", "");
    if (path.empty()) {
        throw std::invalid_argument("struct_* requires a 'doc' object or a 'doc_path'");
    }
    std::ifstream fh(path);
    if (!fh) {
        throw std::runtime_error("document not found: " + path);
    }
    std::stringstream ss;
    ss << fh.rdbuf();
    return nlohmann::json::parse(ss.str());
}

// load_combo param: a combo name (default DEAD_LIVE) or a list of names —
// mirrors analysis.analyze_doc's str|list acceptance.
std::vector<LoadCombo> combos_from_params(const nlohmann::json& params) {
    std::vector<LoadCombo> combos;
    std::set<std::string> seen;
    const nlohmann::json lc = params.contains("load_combo") ? params["load_combo"]
                                                            : nlohmann::json("DEAD_LIVE");
    auto add_combo = [&](const nlohmann::json& name) {
        if (!name.is_string()) throw std::invalid_argument("load_combo names must be strings");
        const LoadCombo combo = get_combo(name.get<std::string>());
        if (!seen.insert(combo.name).second) {
            throw std::invalid_argument("duplicate load_combo '" + combo.name + "'");
        }
        combos.push_back(combo);
    };
    if (lc.is_array()) {
        if (lc.empty()) throw std::invalid_argument("load_combo list must not be empty");
        for (const nlohmann::json& name : lc) add_combo(name);
    } else {
        add_combo(lc);
    }
    return combos;
}

nlohmann::json rpc_analyze(const nlohmann::json& params) {
    const StructureDoc doc = structure_doc_from_dict(doc_json_from_params(params));
    const std::vector<LoadCombo> combos = combos_from_params(params);
    std::vector<ReactionRow> reaction_rows;
    const std::vector<AnalysisRow> rows = analyze_doc(doc, combos, &reaction_rows);
    nlohmann::json results = nlohmann::json::array();
    for (const AnalysisRow& row : rows) results.push_back(row.to_dict());
    nlohmann::json reactions = nlohmann::json::array();
    for (const ReactionRow& reaction : reaction_rows) reactions.push_back(reaction.to_dict());
    return {{"load_combo", params.value("load_combo", nlohmann::json("DEAD_LIVE"))},
            {"units", {{"force", "N"}, {"moment", "N*mm"}, {"displacement", "mm"}}},
            {"results", std::move(results)},
            {"reactions", std::move(reactions)}};
}

// Resolve strength properties in the same local major/minor axes used by the
// frame solver. Sx pairs with moment_z; Sy pairs with moment_y.
nlohmann::json section_for(const StructuralElement& el) {
    const SectionProps props =
        section_properties(el.section_name, el.material, el.element_type);
    return {{"A", props.A},
            {"Sx", props.S_major},
            {"Sy", props.S_minor},
            {"Ix", props.Iz},
            {"Iy", props.Iy}};
}

nlohmann::json rpc_check(const nlohmann::json& params) {
    const StructureDoc doc = structure_doc_from_dict(doc_json_from_params(params));
    const std::vector<LoadCombo> combos = combos_from_params(params);
    const std::vector<AnalysisRow> rows = analyze_doc(doc, combos);
    std::vector<LoadCombo> service_combos;
    service_combos.reserve(combos.size());
    for (const LoadCombo& combo : combos) {
        service_combos.push_back({combo.name,
                                  combo.dead != 0.0 ? 1.0 : 0.0,
                                  combo.live != 0.0 ? 1.0 : 0.0,
                                  combo.wind != 0.0 ? 1.0 : 0.0,
                                  combo.snow != 0.0 ? 1.0 : 0.0});
    }
    const std::vector<AnalysisRow> service_rows = analyze_doc(doc, service_combos);

    nlohmann::json results = nlohmann::json::array();
    bool complete = true;
    for (const AnalysisRow& row : rows) {
        const StructuralElement* el = nullptr;
        for (const StructuralElement& e : doc.elements) {
            if (e.id == row.element_id) {
                el = &e;
                break;
            }
        }
        if (!el) throw std::runtime_error("analysis returned an unknown structural element");

        if (el->element_type == "footing") {
            complete = false;
            results.push_back({
                {"element_id", row.element_id},
                {"combo", row.combo},
                {"dc_ratio", nullptr},
                {"passed", false},
                {"governing_limit", "check_unavailable"},
                {"dc_axial", nullptr},
                {"dc_bending", nullptr},
                {"dc_deflection", nullptr},
                {"status", "unavailable"},
                {"reason", "footing capacity requires soil bearing and reinforced-concrete design inputs"},
                {"disclaimer", kCheckDisclaimer},
            });
            continue;
        }

        nlohmann::json checked;
        try {
            checked = check_member(row.to_dict(), section_for(*el), el->material).to_dict();
        } catch (const std::invalid_argument& e) {
            complete = false;
            results.push_back({
                {"element_id", row.element_id},
                {"combo", row.combo},
                {"dc_ratio", nullptr},
                {"passed", false},
                {"governing_limit", "check_unavailable"},
                {"dc_axial", nullptr},
                {"dc_bending", nullptr},
                {"dc_deflection", nullptr},
                {"status", "unavailable"},
                {"reason", e.what()},
                {"disclaimer", kCheckDisclaimer},
            });
            continue;
        }
        const AnalysisRow* service_row = nullptr;
        for (const AnalysisRow& candidate : service_rows) {
            if (candidate.element_id == row.element_id && candidate.combo == row.combo) {
                service_row = &candidate;
                break;
            }
        }
        if (!service_row) throw std::runtime_error("serviceability result is missing");
        const double deflection_limit_mm = el->length_mm / 240.0;
        const double service_deflection_mm = std::abs(service_row->deflection_mm);
        const double dc_deflection = service_deflection_mm / deflection_limit_mm;
        checked["dc_deflection"] = dc_deflection;
        checked["service_deflection_mm"] = service_deflection_mm;
        checked["deflection_limit_mm"] = deflection_limit_mm;
        checked["deflection_limit_basis"] = "L/240 total-load preliminary";
        checked["deflection_load_basis"] =
            "unfactored service components corresponding to selected strength combo";
        checked["status"] = "evaluated";
        if (dc_deflection > checked["dc_ratio"].get<double>()) {
            checked["dc_ratio"] = dc_deflection;
            checked["passed"] = dc_deflection <= 1.0;
            checked["governing_limit"] = "deflection_L_over_240";
        }
        results.push_back(std::move(checked));
    }
    return {{"load_combo", params.value("load_combo", nlohmann::json("DEAD_LIVE"))},
            {"units", {{"force", "N"}, {"moment", "N*mm"}, {"displacement", "mm"}}},
            {"complete", complete},
            {"disclaimer", kCheckDisclaimer},
            {"results", std::move(results)}};
}

nlohmann::json rpc_estimate(const nlohmann::json& params) {
    const StructureDoc doc = structure_doc_from_dict(doc_json_from_params(params));
    const RateTable rates = load_rates(params.value("rates_path", ""));
    return estimate(doc, rates);
}

}  // namespace

bool call(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
          std::string& err) {
    (void)err;  // reserved — expected failures ride inside `result` (see header)
    try {
        if (method == "struct_analyze") {
            result = rpc_analyze(params);
        } else if (method == "struct_check") {
            result = rpc_check(params);
        } else if (method == "struct_estimate") {
            result = rpc_estimate(params);
        } else {
            return false;
        }
        return true;
    } catch (const nlohmann::json::exception& e) {
        result = err_env("invalid_json", e.what());
        return true;
    } catch (const std::invalid_argument& e) {
        result = err_env("invalid_schema", e.what());
        return true;
    } catch (const std::out_of_range& e) {
        result = err_env("unknown_key", e.what());
        return true;
    } catch (const std::exception& e) {
        result = err_env("solver_error", e.what());
        return true;
    }
}

}  // namespace chudware::structural
