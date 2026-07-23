// Structural RPC dispatch — see structural_rpc.h.

#include "structural/structural_rpc.h"

#include <cmath>
#include <fstream>
#include <sstream>
#include <stdexcept>

#include "structural/checks.h"
#include "structural/estimate.h"
#include "structural/model.h"
#include "structural/sections.h"
#include "structural/solver.h"

namespace aicad::structural {
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
    const nlohmann::json lc = params.contains("load_combo") ? params["load_combo"]
                                                            : nlohmann::json("DEAD_LIVE");
    if (lc.is_array()) {
        for (const nlohmann::json& name : lc) combos.push_back(get_combo(name.get<std::string>()));
    } else {
        combos.push_back(get_combo(lc.get<std::string>()));
    }
    return combos;
}

nlohmann::json rpc_analyze(const nlohmann::json& params) {
    const StructureDoc doc = structure_doc_from_dict(doc_json_from_params(params));
    const std::vector<LoadCombo> combos = combos_from_params(params);
    const std::vector<AnalysisRow> rows = analyze_doc(doc, combos);
    nlohmann::json results = nlohmann::json::array();
    for (const AnalysisRow& r : rows) results.push_back(r.to_dict());
    return {{"load_combo", params.value("load_combo", nlohmann::json("DEAD_LIVE"))},
            {"results", std::move(results)}};
}

// Section resolution per element — mirrors the _section_for closure in
// structural_tools.struct_check: AISC table → lumber table → brep-dims
// fallback from section_properties (Sx approximated from Iz / sqrt(A)).
nlohmann::json section_for(const StructuralElement& el) {
    std::string sname = el.section_name;
    // Python: (el.section_name or "").strip().upper().replace(" ", "") — the
    // lookups redo their own normalization, so passing the raw name is fine.
    if (auto s = get_aisc_section(sname)) return *s;
    if (auto s = get_lumber_section(sname)) return *s;
    const SectionProps props = section_properties(el.section_name, el.material, el.element_type);
    const double sx_approx =
        props.A > 0.0 ? 2.0 * props.Iz / std::max(std::sqrt(props.A), 1.0) : 0.0;
    return {{"A", props.A}, {"Sx", sx_approx}, {"Sy", sx_approx}, {"Ix", props.Iy}};
}

nlohmann::json rpc_check(const nlohmann::json& params) {
    const StructureDoc doc = structure_doc_from_dict(doc_json_from_params(params));
    const std::vector<LoadCombo> combos = combos_from_params(params);
    const std::vector<AnalysisRow> rows = analyze_doc(doc, combos);

    nlohmann::json results = nlohmann::json::array();
    for (const AnalysisRow& row : rows) {
        const nlohmann::json row_dict = row.to_dict();
        const StructuralElement* el = nullptr;
        for (const StructuralElement& e : doc.elements) {
            if (e.id == row.element_id) {
                el = &e;
                break;
            }
        }
        const nlohmann::json section = el ? section_for(*el) : nlohmann::json::object();
        const std::string material = el ? el->material : "steel";
        results.push_back(check_member(row_dict, section, material).to_dict());
    }
    return {{"load_combo", params.value("load_combo", nlohmann::json("DEAD_LIVE"))},
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

}  // namespace aicad::structural
