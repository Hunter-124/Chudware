// T-U7 hydraulic doc → net canvas adapter.
#include "hydraulic_view.h"
#include "net_canvas_view.h"
#include <nlohmann/json.hpp>
#include <cstdio>
#include <string>
#include <unordered_map>

namespace aicad::ui {

void hydraulicDocToCanvas(const std::string& doc_json,
                          const std::string& sizing_json,
                          NetCanvasModel& out) {
    out = NetCanvasModel{};
    if (doc_json.empty()) { out.state = NetCanvasPanelState::Idle; return; }

    nlohmann::json doc, sizing;
    try { doc = nlohmann::json::parse(doc_json); } catch (...) {
        out.state = NetCanvasPanelState::Error;
        out.errorMsg = "Failed to parse hydraulic doc JSON";
        return;
    }
    bool hasSizing = !sizing_json.empty();
    if (hasSizing) {
        try { sizing = nlohmann::json::parse(sizing_json); } catch (...) { hasSizing = false; }
    }

    // Build sizing lookup by component id
    std::unordered_map<std::string, std::string> sizeLabel;
    if (hasSizing && sizing.is_object()) {
        for (auto& [cid, sr] : sizing.items()) {
            char buf[128];
            if (sr.contains("extend_force_kn")) {
                std::snprintf(buf, sizeof(buf), "F=%.1f kN Q=%.1f L/min",
                    sr.value("extend_force_kn", 0.0),
                    sr.value("extend_flow_lpm", 0.0));
            } else if (sr.contains("displacement_cc_rev")) {
                std::snprintf(buf, sizeof(buf), "D=%.1f cc/rev P=%.1f kW",
                    sr.value("displacement_cc_rev", 0.0),
                    sr.value("shaft_power_kw", 0.0));
            } else {
                std::snprintf(buf, sizeof(buf), "sized");
            }
            sizeLabel[cid] = buf;
        }
        out.solved = !sizeLabel.empty();
        out.converged = true;
        out.solveStatus = "Hydraulic sizing available";
    }

    // Build id→index map for edge wiring
    std::unordered_map<std::string, int> idxMap;
    const auto& comps = doc.value("components", nlohmann::json::array());
    for (const auto& c : comps) {
        std::string cid = c.value("id", "");
        float cx = (float)c.value("x", 0.0) * 0.01f;
        float cy = (float)c.value("y", 0.0) * 0.01f;

        NetCanvasNode nd;
        nd.id       = cid;
        nd.label    = c.value("name", c.value("type", std::string()));
        nd.sublabel = c.value("type", std::string());  // used for ISO symbol dispatch
        nd.x = cx; nd.y = cy;
        if (sizeLabel.count(cid)) {
            nd.resultStr = sizeLabel[cid];
            nd.resultOk  = true;
        }
        idxMap[cid] = (int)out.nodes.size();
        out.nodes.push_back(nd);

        NetCanvasResultItem ri;
        ri.elementId = cid;
        ri.label     = nd.label + " (" + nd.sublabel + ")";
        ri.value     = nd.resultStr.empty() ? "(no sizing result)" : nd.resultStr;
        ri.ok        = true;
        out.results.push_back(ri);
    }

    // Edges from hoses: from_port.comp_id → to_port.comp_id
    const auto& hoses = doc.value("hoses", nlohmann::json::array());
    for (const auto& h : hoses) {
        std::string fromId = h.value("from_port", nlohmann::json::object()).value("comp_id", "");
        std::string toId   = h.value("to_port",   nlohmann::json::object()).value("comp_id", "");
        if (fromId.empty() || toId.empty()) continue;
        NetCanvasEdge e;
        e.fromId = fromId;
        e.toId   = toId;
        e.dashed = true;  // hoses rendered dashed per ISO 1219
        out.edges.push_back(e);
    }

    normalizeNetCanvasLayout(out);
    out.state = NetCanvasPanelState::Ready;
}

}  // namespace aicad::ui
