// T-U7 piping doc → net canvas adapter.
#include "piping_view.h"
#include "net_canvas_view.h"
#include <nlohmann/json.hpp>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>
#include <unordered_map>

namespace aicad::ui {

// Project a 3-D path midpoint to 2-D plan view (x, y), scaled to canvas mm.
static std::pair<float,float> planMid(const nlohmann::json& path) {
    if (!path.is_array() || path.empty()) return {0.f, 0.f};
    double sx = 0, sy = 0;
    int n = 0;
    for (const auto& p : path) {
        if (p.is_array() && p.size() >= 2) {
            sx += p[0].get<double>(); sy += p[1].get<double>(); ++n;
        }
    }
    if (n == 0) return {0.f, 0.f};
    return {(float)(sx / n * 0.01), (float)(sy / n * 0.01)};  // mm→canvas units
}

void pipingDocToCanvas(const std::string& doc_json,
                       const std::string& solve_json,
                       NetCanvasModel& out) {
    out = NetCanvasModel{};
    if (doc_json.empty()) { out.state = NetCanvasPanelState::Idle; return; }

    nlohmann::json doc, solve;
    try { doc = nlohmann::json::parse(doc_json); } catch (...) {
        out.state = NetCanvasPanelState::Error;
        out.errorMsg = "Failed to parse piping doc JSON";
        return;
    }
    bool hasSolve = !solve_json.empty();
    if (hasSolve) {
        try { solve = nlohmann::json::parse(solve_json); } catch (...) { hasSolve = false; }
    }

    // Build link-flow lookup from solve result
    // links[]: {run_id, flow_m3h, velocity_ms, pressure_drop_kpa}
    std::unordered_map<std::string, std::string> linkLabel;
    std::unordered_map<std::string, bool> linkOk;
    if (hasSolve && solve.value("ok", false) && solve.contains("links")) {
        for (const auto& lk : solve["links"]) {
            std::string rid = lk.value("run_id", "");
            if (rid.empty()) continue;
            char buf[128];
            float q  = (float)lk.value("flow_m3h", 0.0);
            float v  = (float)lk.value("velocity_ms", 0.0);
            float dp = (float)lk.value("pressure_drop_kpa", 0.0);
            std::snprintf(buf, sizeof(buf), "Q=%.1f m³/h v=%.1f m/s dP=%.0f kPa", q, v, dp);
            linkLabel[rid] = buf;
            linkOk[rid] = true;
        }
        out.solved = true;
        out.converged = solve.value("converged", false);
        out.solveStatus = out.converged ? "Solved (converged)" : "Solved (not converged)";
    }

    // Nodes from runs — position at path centroid
    const auto& runs = doc.value("runs", nlohmann::json::array());
    float xOff = 0.f;
    for (const auto& run : runs) {
        std::string rid = run.value("id", "");
        auto [cx, cy] = planMid(run.value("path", nlohmann::json::array()));
        // Spread out if projected positions collide
        if (cx == 0.f && cy == 0.f) cx = xOff;
        xOff += 150.f;

        NetCanvasNode nd;
        nd.id       = rid;
        nd.label    = rid + " NPS " + std::to_string((int)run.value<double>("nps", 0.0));
        nd.sublabel = run.value("schedule", std::string());
        nd.x = cx; nd.y = cy;
        if (linkLabel.count(rid)) {
            nd.resultStr = linkLabel[rid];
            nd.resultOk  = linkOk[rid];
        }
        out.nodes.push_back(nd);

        NetCanvasResultItem ri;
        ri.elementId = rid;
        ri.label     = "Run " + rid;
        ri.value     = nd.resultStr.empty() ? "(no solve result)" : nd.resultStr;
        ri.ok        = nd.resultOk;
        out.results.push_back(ri);
    }

    // Nodes from fittings — at their origin
    const auto& fittings = doc.value("fittings", nlohmann::json::array());
    for (const auto& ft : fittings) {
        std::string fid = ft.value("id", "");
        const auto& orig = ft.value("origin", nlohmann::json::array());
        float ox = orig.size() >= 1 ? (float)(orig[0].get<double>() * 0.01) : 0.f;
        float oy = orig.size() >= 2 ? (float)(orig[1].get<double>() * 0.01) : 0.f;

        NetCanvasNode nd;
        nd.id       = fid;
        nd.label    = ft.value("type", std::string()) + " " + fid;
        nd.sublabel = "NPS " + std::to_string((int)ft.value<double>("nps", 0.0));
        nd.x = ox; nd.y = oy;
        out.nodes.push_back(nd);
    }

    // Simple sequential edges: connect runs in document order
    for (int i = 0; i + 1 < (int)runs.size(); ++i) {
        NetCanvasEdge e;
        e.fromId = runs[i].value("id", "");
        e.toId   = runs[i+1].value("id", "");
        e.dashed = false;
        out.edges.push_back(e);
    }

    normalizeNetCanvasLayout(out);
    out.state = NetCanvasPanelState::Ready;
}

}  // namespace aicad::ui
