#pragma once
// Framework-agnostic graph-canvas view-model shared by piping_view, hydraulic_view,
// and structural_view panels (T-U7). Pure structs; no ImGui/GL includes.
// Three adapters populate NetCanvasModel from each doc kind's JSON schema; the
// ui/imgui backend renders from it unchanged.
//
// Coordinate space: schematic 2-D (x,y) in arbitrary mm units; z is ignored for
// piping/structural (projected to plan/elevation). Canvas panning/zooming is purely
// a render-time concern stored in NetCanvasPanState (separate from this view-model).

#include <algorithm>
#include <string>
#include <vector>

namespace aicad::ui {

// ---- Node -----------------------------------------------------------------------

struct NetCanvasNode {
    std::string id;         // stable id (run_id, component id, element id)
    std::string label;      // short display label (NPS, type, section_name)
    std::string sublabel;   // second line (material, schedule, …)
    float       x = 0.f;   // canvas position (mm)
    float       y = 0.f;

    // Optional solve-result overlay (empty = not solved / unavailable).
    std::string resultStr;  // e.g. "P=12.3 kPa", "F=37.3 kN", "D/C=0.82"
    bool        resultOk = true;   // false → red accent
};

// ---- Edge -----------------------------------------------------------------------

struct NetCanvasEdge {
    std::string fromId;     // node id
    std::string toId;       // node id
    std::string label;      // optional mid-edge label (flow, force, ...)
    bool        dashed = false;  // dashed = hose/connection; solid = member
};

// ---- Selections + results side-list ---------------------------------------------

struct NetCanvasResultItem {
    std::string elementId;
    std::string label;      // "Run r1 — NPS 1.5"
    std::string value;      // "Q=4.2 m³/h, v=1.8 m/s, dP=3.1 kPa"
    bool        ok = true;
};

// ---- Panel state ----------------------------------------------------------------

enum class NetCanvasPanelState {
    Idle,       // no doc loaded
    Ready,      // nodes+edges populated (from worker RPC or fixture)
    Loading,    // RPC in-flight
    Error,
};

// ---- Model (per-panel) ----------------------------------------------------------

struct NetCanvasModel {
    NetCanvasPanelState         state = NetCanvasPanelState::Idle;
    std::string                 errorMsg;

    std::vector<NetCanvasNode>  nodes;
    std::vector<NetCanvasEdge>  edges;
    std::vector<NetCanvasResultItem> results;   // side-list, same order as DoD

    // Which element is selected (index into nodes; -1 = none).
    int selectedNode = -1;

    // Canvas pan/zoom (render-side only; kept here for persistence across frames).
    float panX = 0.f, panY = 0.f, zoom = 1.f;
    // False until the renderer has auto-fitted the content into the canvas once
    // (reset to false whenever the model is (re)seeded so new docs re-fit).
    bool  viewFitted = false;

    // Solve summary strip
    bool        solved = false;
    bool        converged = false;
    std::string solveStatus;    // "Solved (converged)" | "Solved (not converged)" | ""
};

// ---- Layout normalization ---------------------------------------------------------
// Doc schemas use wildly different coordinate scales (mm paths, schematic grid units,
// transform translations). Adapters call this after populating nodes so the canvas
// always receives a well-spread layout in a fixed target space. Degenerate axes
// (all nodes on one line) are spread by index / centered.
inline void normalizeNetCanvasLayout(NetCanvasModel& m,
                                     float targetW = 700.f, float targetH = 360.f) {
    if (m.nodes.empty()) return;
    float minX = m.nodes[0].x, maxX = minX;
    float minY = m.nodes[0].y, maxY = minY;
    for (const auto& n : m.nodes) {
        minX = std::min(minX, n.x); maxX = std::max(maxX, n.x);
        minY = std::min(minY, n.y); maxY = std::max(maxY, n.y);
    }
    const float spanX = maxX - minX;
    const float spanY = maxY - minY;
    const float idxStep = (m.nodes.size() > 1)
        ? targetW / static_cast<float>(m.nodes.size() - 1) : 0.f;
    for (size_t i = 0; i < m.nodes.size(); ++i) {
        auto& n = m.nodes[i];
        n.x = (spanX > 1e-6f) ? (n.x - minX) / spanX * targetW
                              : static_cast<float>(i) * idxStep;
        n.y = (spanY > 1e-6f) ? (n.y - minY) / spanY * targetH
                              : targetH * 0.5f;
    }
}

// ---- "Open Copilot" affordance text (v1 read-only fence) ------------------------
inline constexpr const char* NET_CANVAS_EDIT_HINT =
    "Editing opens in Copilot — v1 view only";

}  // namespace aicad::ui
