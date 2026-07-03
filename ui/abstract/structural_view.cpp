// T-U7 structural doc → net canvas adapter.
#include "structural_view.h"
#include "net_canvas_view.h"
#include <nlohmann/json.hpp>
#include <cstdio>
#include <cmath>
#include <string>
#include <unordered_map>

namespace aicad::ui {

// Extract the translation from a 4×4 row-major transform (elements 12,13).
static std::pair<float,float> transformOrigin(const nlohmann::json& tx) {
    if (!tx.is_array() || tx.size() < 16) return {0.f, 0.f};
    // Row-major: [0..3]=row0, [4..7]=row1, [8..11]=row2, [12..15]=row3(translation)
    float x = (float)tx[12].get<double>() * 0.001f;  // mm→canvas (÷1000)
    float y = (float)tx[13].get<double>() * 0.001f;
    return {x, y};
}

void structuralDocToCanvas(const std::string& doc_json,
                           const std::string& results_json,
                           NetCanvasModel& out) {
    out = NetCanvasModel{};
    if (doc_json.empty()) { out.state = NetCanvasPanelState::Idle; return; }

    nlohmann::json doc, res;
    try { doc = nlohmann::json::parse(doc_json); } catch (...) {
        out.state = NetCanvasPanelState::Error;
        out.errorMsg = "Failed to parse structural doc JSON";
        return;
    }
    bool hasResults = !results_json.empty();
    if (hasResults) {
        try { res = nlohmann::json::parse(results_json); } catch (...) { hasResults = false; }
    }

    std::unordered_map<std::string, std::string> resLabel;
    std::unordered_map<std::string, bool> resOk;
    if (hasResults && res.is_object()) {
        for (auto& [eid, er] : res.items()) {
            char buf[128];
            float dc   = (float)er.value("dc_ratio", 0.0);
            float defl = (float)er.value("deflection_max_mm", 0.0);
            bool ok    = er.value("ok", dc <= 1.0);
            std::snprintf(buf, sizeof(buf), "D/C=%.2f δ=%.1f mm", dc, defl);
            resLabel[eid] = buf;
            resOk[eid] = ok;
        }
        out.solved = !resLabel.empty();
        out.converged = true;
        out.solveStatus = "Structural analysis available";
    }

    const auto& elements = doc.value("elements", nlohmann::json::array());
    float xOff = 0.f;
    for (const auto& el : elements) {
        std::string eid = el.value("id", "");
        auto [cx, cy] = transformOrigin(el.value("transform", nlohmann::json::array()));
        if (cx == 0.f && cy == 0.f) { cx = xOff; }
        xOff += 120.f;

        NetCanvasNode nd;
        nd.id       = eid;
        nd.label    = el.value("section_name", eid);
        nd.sublabel = el.value("element_type", std::string()) + " " + el.value("material", std::string());
        nd.x = cx; nd.y = cy;
        if (resLabel.count(eid)) {
            nd.resultStr = resLabel[eid];
            nd.resultOk  = resOk[eid];
        }
        out.nodes.push_back(nd);

        NetCanvasResultItem ri;
        ri.elementId = eid;
        ri.label     = nd.label + " [" + el.value("element_type", std::string()) + "]";
        ri.value     = nd.resultStr.empty() ? "(no analysis result)" : nd.resultStr;
        ri.ok        = nd.resultStr.empty() ? true : resOk.count(eid) ? resOk[eid] : true;
        out.results.push_back(ri);
    }

    // Sequential connectivity edges (frame members share endpoints)
    for (int i = 0; i + 1 < (int)elements.size(); ++i) {
        NetCanvasEdge e;
        e.fromId = elements[i].value("id", "");
        e.toId   = elements[i+1].value("id", "");
        e.dashed = false;
        out.edges.push_back(e);
    }

    normalizeNetCanvasLayout(out);
    out.state = NetCanvasPanelState::Ready;
}

}  // namespace aicad::ui
