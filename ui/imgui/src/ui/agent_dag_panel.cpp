// T-DP17 Agent DAG widget — see agent_dag_panel.h.
//
// Architecture:
//   View-model : aicad::ui::AgentDagModel  (ui/abstract/agent_dag_view.h, no ImGui)
//   Scaffold   : ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   Layout     : deterministic layered layout — depth = longest-path from roots,
//                slot = position in layer.  ~10 fixture nodes; force-directed not needed.
//   Rendering  : ImDrawList directly on a pan-scroll canvas (same pattern as
//                net_canvas_panel.cpp — T-U7).  No third-party node-graph library.
//   Seeding    : inline JSONL (aicad.agent_events/1 + delegation.* events),
//                same pattern as mission_control_panel.cpp seedFromJsonlText().
//   Colors     : theme::pal() tokens only — no IM_COL32 / ImVec4 literal colors.

#include "agent_dag_panel.h"
#include "panel_scaffold.h"
#include "agent_dag_view.h"     // ui/abstract/ (no ImGui includes there)

#include "app/app_state.h"

#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include "imgui_internal.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace ui {

namespace {

// ── Panel state ───────────────────────────────────────────────────────────

bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::AgentDagModel gModel;

// Canvas pan state (full mode)
float gPanX = 0.f;
float gPanY = 0.f;
bool  gDragging = false;

// Shot-seam: force a specific render mode (set by agentDagShotForceMode before the frame).
aicad::ui::DagWidgetMode gForcedMode = aicad::ui::DagWidgetMode::Full;
bool gHasForcedMode = false;

// ── Layout constants ─────────────────────────────────────────────────────

constexpr float kNodeW       = 130.f;  // node box width  (logical px @ 1.0×)
constexpr float kNodeH       = 46.f;   // node box height
constexpr float kLayerGapY   = 50.f;   // vertical distance between layers (compact to fit 5+ layers)
constexpr float kSlotGapX    = 148.f;  // horizontal distance between nodes in a layer

// ── JSON helpers ─────────────────────────────────────────────────────────

static std::string jstr(const nlohmann::json& j, const char* key) {
    if (j.contains(key) && j[key].is_string()) return j[key].get<std::string>();
    return {};
}
static int jint(const nlohmann::json& j, const char* key, int def = 0) {
    if (j.contains(key) && j[key].is_number()) return j[key].get<int>();
    return def;
}

// ── Status/kind helpers ──────────────────────────────────────────────────

static aicad::ui::DagWidgetNodeStatus statusFromString(const std::string& s) {
    using S = aicad::ui::DagWidgetNodeStatus;
    if (s == "running")  return S::Running;
    if (s == "done")     return S::Done;
    if (s == "failed")   return S::Failed;
    if (s == "blocked")  return S::Blocked;
    if (s == "skipped")  return S::Skipped;
    return S::Queued;
}

static aicad::ui::DagWidgetNodeKind kindFromNodeType(const std::string& t) {
    using K = aicad::ui::DagWidgetNodeKind;
    if (t == "gate")        return K::Gate;
    if (t == "verify")      return K::Verify;
    if (t == "delegation")  return K::Delegation;
    if (t == "build_op")    return K::BuildOp;
    return K::Mission;
}

// ── Event projection (JSONL → view-model) ────────────────────────────────

static aicad::ui::DagWidgetNode* findOrMakeNode(const std::string& id) {
    for (auto& n : gModel.nodes)
        if (n.id == id) return &n;
    aicad::ui::DagWidgetNode n;
    n.id    = id;
    n.label = id;
    gModel.nodes.push_back(std::move(n));
    return &gModel.nodes.back();
}

static bool edgeExists(const std::string& from, const std::string& to) {
    for (auto& e : gModel.edges)
        if (e.fromId == from && e.toId == to) return true;
    return false;
}

static void applyEvent(const nlohmann::json& ev) {
    using NS = aicad::ui::DagWidgetNodeStatus;

    std::string k = jstr(ev, "kind");
    if (k.empty()) k = jstr(ev, "event");
    const nlohmann::json* d = &ev;
    if (ev.contains("data") && ev["data"].is_object()) d = &ev["data"];

    if (k == "dag.run.started") {
        gModel.runId = jstr(*d, "run_id");
        if (gModel.runId.empty()) gModel.runId = jstr(ev, "run_id");
        gModel.missionName = jstr(*d, "graph_name");
        if (gModel.missionName.empty()) gModel.missionName = jstr(*d, "graph_id");
        gModel.hasData = true;
    } else if (k == "dag.node.started") {
        const std::string nid = jstr(*d, "node_id");
        auto* n = findOrMakeNode(nid);
        n->status = NS::Running;
        const std::string nm = jstr(*d, "node_name");
        if (!nm.empty()) n->label = nm;
        n->kind = kindFromNodeType(jstr(*d, "node_type"));
        gModel.hasData = true;
    } else if (k == "dag.node.done") {
        const std::string nid = jstr(*d, "node_id");
        auto* n = findOrMakeNode(nid);
        n->status = NS::Done;
        n->statusLabel = jstr(*d, "output_summary");
    } else if (k == "dag.node.failed") {
        const std::string nid = jstr(*d, "node_id");
        auto* n = findOrMakeNode(nid);
        n->status = NS::Failed;
        n->statusLabel = jstr(*d, "error");
    } else if (k == "dag.node.paused" || k == "dag.node.approval_required") {
        const std::string nid = jstr(*d, "node_id");
        if (!nid.empty()) findOrMakeNode(nid)->status = NS::Blocked;
    } else if (k == "dag.run.done") {
        // final statuses in node_states map
        if (d->contains("node_states") && (*d)["node_states"].is_object()) {
            for (auto& [nid, ns] : (*d)["node_states"].items()) {
                const std::string st = ns.is_object() ? jstr(ns, "status")
                                     : (ns.is_string() ? ns.get<std::string>() : "");
                auto* n = findOrMakeNode(nid);
                if (st == "skipped") n->status = NS::Skipped;
                else if (st == "done") n->status = NS::Done;
                else if (st == "failed") n->status = NS::Failed;
            }
        }
    } else if (k == "agent.tool_call") {
        const std::string nid = jstr(*d, "node_id");
        if (!nid.empty()) {
            auto* n = findOrMakeNode(nid);
            const std::string tool = jstr(*d, "tool");
            if (!tool.empty()) n->currentTool = tool;
        }
    } else if (k == "dag.edge") {
        // explicit edge: {"kind":"dag.edge","from":"nodeA","to":"nodeB"}
        const std::string from = jstr(*d, "from");
        const std::string to   = jstr(*d, "to");
        if (!from.empty() && !to.empty() && !edgeExists(from, to))
            gModel.edges.push_back({from, to});
    } else if (k == "delegation.spawn") {
        // delegation event: agent spawned a sub-agent
        const std::string pid = jstr(*d, "parent_id");
        const std::string cid = jstr(*d, "child_id");
        if (pid.empty() || cid.empty()) return;
        auto* child = findOrMakeNode(cid);
        child->kind   = aicad::ui::DagWidgetNodeKind::Delegation;
        child->status = NS::Queued;
        const std::string role = jstr(*d, "role");
        if (!role.empty()) { child->role = role; child->label = role; }
        const std::string model = jstr(*d, "model");
        if (!model.empty()) child->model = model;
        child->tokensGranted = jint(*d, "tokens_granted");
        if (!edgeExists(pid, cid)) gModel.edges.push_back({pid, cid});
        gModel.hasData = true;
    } else if (k == "delegation.update") {
        const std::string cid = jstr(*d, "id");
        if (cid.empty()) return;
        auto* n = findOrMakeNode(cid);
        const std::string st = jstr(*d, "status");
        if (!st.empty()) n->status = statusFromString(st);
        n->tokensSpent  = jint(*d, "tokens_spent",   n->tokensSpent);
        n->currentTool  = jstr(*d, "current_tool").empty()
                          ? n->currentTool : jstr(*d, "current_tool");
        n->ledgerSlice  = jstr(*d, "ledger_slice").empty()
                          ? n->ledgerSlice : jstr(*d, "ledger_slice");
    } else if (k == "build_op") {
        // buildgraph node
        const std::string nid = jstr(*d, "node_id");
        if (nid.empty()) return;
        auto* n = findOrMakeNode(nid);
        n->kind = aicad::ui::DagWidgetNodeKind::BuildOp;
        const std::string nm = jstr(*d, "op");
        if (!nm.empty()) n->label = nm;
        n->status = statusFromString(jstr(*d, "status"));
        // edges from deps
        if (d->contains("deps") && (*d)["deps"].is_array()) {
            for (auto& dep : (*d)["deps"]) {
                if (dep.is_string()) {
                    const std::string depId = dep.get<std::string>();
                    if (!edgeExists(depId, nid)) gModel.edges.push_back({depId, nid});
                }
            }
        }
        gModel.hasData = true;
    }
}

// ── Layered layout ────────────────────────────────────────────────────────
// Assigns depth (layer) = longest-path from roots, then a slot within layer.

static void layoutDag(float scale) {
    if (gModel.nodes.empty()) return;

    // Build adjacency maps
    std::unordered_map<std::string, std::vector<std::string>> children;
    std::unordered_map<std::string, int> inDeg;
    for (auto& n : gModel.nodes) { inDeg[n.id] = 0; children[n.id] = {}; }
    for (auto& e : gModel.edges) {
        children[e.fromId].push_back(e.toId);
        inDeg[e.toId]++;
    }

    // BFS from roots (nodes with in-degree 0) — assign depth = longest path
    std::unordered_map<std::string, int> depth;
    for (auto& n : gModel.nodes) depth[n.id] = 0;

    // Topological order via Kahn
    std::vector<std::string> queue;
    for (auto& [id, d] : inDeg) if (d == 0) queue.push_back(id);
    std::vector<std::string> topo;
    std::unordered_map<std::string, int> inDegCopy = inDeg;
    while (!queue.empty()) {
        std::string cur = queue.back(); queue.pop_back();
        topo.push_back(cur);
        for (auto& child : children[cur]) {
            depth[child] = std::max(depth[child], depth[cur] + 1);
            if (--inDegCopy[child] == 0) queue.push_back(child);
        }
    }
    // Nodes not reached (cycle) get appended with depth 0
    for (auto& n : gModel.nodes)
        if (std::find(topo.begin(), topo.end(), n.id) == topo.end())
            topo.push_back(n.id);

    // Assign depth to model nodes
    for (auto& n : gModel.nodes) n.depth = depth.count(n.id) ? depth.at(n.id) : 0;

    // Count max layer
    int maxDepth = 0;
    for (auto& n : gModel.nodes) maxDepth = std::max(maxDepth, n.depth);

    // Assign slot within layer
    std::unordered_map<int, int> slotCounter;
    for (auto& id : topo) {
        for (auto& n : gModel.nodes) {
            if (n.id == id) {
                n.slotInLayer = slotCounter[n.depth]++;
                break;
            }
        }
    }

    // Compute canvas coordinates
    // Nodes at same depth → same Y; slot within depth → X
    const float nodeW = kNodeW * scale;
    const float nodeH = kNodeH * scale;
    const float gapY  = kLayerGapY * scale;
    const float gapX  = kSlotGapX * scale;
    for (auto& n : gModel.nodes) {
        // count how many nodes share this depth for centering
        int layerCount = 0;
        for (auto& m : gModel.nodes) if (m.depth == n.depth) layerCount++;
        const float totalW = layerCount * gapX;
        n.cx = -totalW * 0.5f + (n.slotInLayer + 0.5f) * gapX;
        n.cy = (n.depth + 0.5f) * (nodeH + gapY);
    }
}

// ── Status color (via theme tokens — no literal ImVec4/IM_COL32) ──────────

static ImU32 statusFillColor(aicad::ui::DagWidgetNodeStatus s) {
    const auto& p = theme::pal();
    using S = aicad::ui::DagWidgetNodeStatus;
    switch (s) {
    case S::Running:  return theme::roleU32A(p.accent,   0.45f);
    case S::Done:     return theme::roleU32A(p.good,     0.40f);
    case S::Failed:   return theme::roleU32A(p.bad,      0.50f);
    case S::Blocked:  return theme::roleU32A(p.warn,     0.45f);
    case S::Skipped:  return theme::roleU32A(p.txt2,     0.25f);
    case S::Queued:
    default:          return theme::roleU32A(p.bg3,      0.70f);
    }
}

static ImU32 statusBorderColor(aicad::ui::DagWidgetNodeStatus s) {
    const auto& p = theme::pal();
    using S = aicad::ui::DagWidgetNodeStatus;
    switch (s) {
    case S::Running:  return theme::rgba(p.accent);
    case S::Done:     return theme::rgba(p.good);
    case S::Failed:   return theme::rgba(p.bad);
    case S::Blocked:  return theme::rgba(p.warn);
    case S::Skipped:  return theme::roleU32A(p.txt2, 0.50f);
    case S::Queued:
    default:          return theme::rgba(p.glassBorder);
    }
}

static const char* statusStr(aicad::ui::DagWidgetNodeStatus s) {
    using S = aicad::ui::DagWidgetNodeStatus;
    switch (s) {
    case S::Running: return "running";
    case S::Done:    return "done";
    case S::Failed:  return "failed";
    case S::Blocked: return "blocked";
    case S::Skipped: return "skipped";
    default:         return "queued";
    }
}

// ── Kind badge label ─────────────────────────────────────────────────────

static const char* kindBadge(aicad::ui::DagWidgetNodeKind k) {
    using K = aicad::ui::DagWidgetNodeKind;
    switch (k) {
    case K::Gate:       return "GATE";
    case K::Verify:     return "VFY";
    case K::Delegation: return "SUB";
    case K::BuildOp:    return "OP";
    default:            return "MSN";
    }
}

// ── Detail pane (right panel in Full mode) ────────────────────────────────

static void drawDetailPane(const aicad::ui::DagWidgetNode& n, float paneW) {
    const auto& p = theme::pal();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 cursor = ImGui::GetCursorScreenPos();
    const float scale = theme::S();
    const float pad  = theme::U(2.f);
    const float panH = ImGui::GetContentRegionAvail().y;

    // Glass background for detail pane
    theme::glassBackground(dl,
        cursor,
        {cursor.x + paneW - pad, cursor.y + panH},
        6.f * scale);

    ImGui::SetCursorScreenPos({cursor.x + pad, cursor.y + pad});
    ImGui::BeginGroup();

    // Title row: label + status badge
    ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(p.txt0));
    ImGui::TextUnformatted(n.label.c_str());
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, statusBorderColor(n.status));
    ImGui::Text("[%s]", statusStr(n.status));
    ImGui::PopStyleColor();

    ImGui::Spacing();

    // Helper macro for key-value rows
    auto row = [&](const char* key, const char* val) {
        if (!val || !val[0]) return;
        ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(p.txt2));
        ImGui::TextUnformatted(key);
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(p.txt1));
        ImGui::TextUnformatted(val);
        ImGui::PopStyleColor();
    };

    row("Role:  ", n.role.c_str());
    row("Model: ", n.model.c_str());
    row("Tool:  ", n.currentTool.c_str());
    row("Ledger:", n.ledgerSlice.c_str());
    if (!n.statusLabel.empty()) row("Note:  ", n.statusLabel.c_str());

    if (n.tokensGranted > 0 || n.tokensSpent > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(p.txt2));
        ImGui::Text("Tokens: %d / %d", n.tokensSpent, n.tokensGranted);
        ImGui::PopStyleColor();
    }

    ImGui::EndGroup();
}

// ── Canvas draw ──────────────────────────────────────────────────────────

static void drawCanvas(float canvasW, float canvasH) {
    const float scale = theme::S();
    const auto& p = theme::pal();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Canvas child — clipped, captures scroll/click
    ImVec2 canvasPos = ImGui::GetCursorScreenPos();

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.f, 0.f, 0.f, 0.f));
    bool child = ImGui::BeginChild("##dag_canvas", {canvasW, canvasH},
                                   ImGuiChildFlags_None,
                                   ImGuiWindowFlags_NoScrollbar |
                                   ImGuiWindowFlags_NoScrollWithMouse);
    if (child) {
        // Pan via drag
        if (ImGui::IsWindowHovered() && ImGui::IsMouseDragging(ImGuiMouseButton_Middle, 2.f)) {
            ImVec2 delta = ImGui::GetIO().MouseDelta;
            gPanX += delta.x;
            gPanY += delta.y;
        }
        // Right-drag also pans (no selection conflict)
        if (ImGui::IsWindowHovered() && ImGui::IsMouseDragging(ImGuiMouseButton_Right, 2.f)) {
            ImVec2 delta = ImGui::GetIO().MouseDelta;
            gPanX += delta.x;
            gPanY += delta.y;
        }

        const float nodeW = kNodeW * scale;
        const float nodeH = kNodeH * scale;

        // Origin in canvas space (pan offset)
        const float ox = canvasPos.x + canvasW * 0.5f + gPanX;
        const float oy = canvasPos.y + 24.f * scale + gPanY;

        // ── Edges ────────────────────────────────────────────────────────
        const ImU32 edgeCol = theme::roleU32A(p.glassBorder, 0.80f);
        for (auto& e : gModel.edges) {
            const aicad::ui::DagWidgetNode* from = nullptr;
            const aicad::ui::DagWidgetNode* to   = nullptr;
            for (auto& n : gModel.nodes) {
                if (n.id == e.fromId) from = &n;
                if (n.id == e.toId)   to   = &n;
            }
            if (!from || !to) continue;
            ImVec2 p1{ox + from->cx, oy + from->cy + nodeH * 0.5f};
            ImVec2 p2{ox + to->cx,   oy + to->cy   - nodeH * 0.5f};
            // Bezier curve
            float cy1 = p1.y + (p2.y - p1.y) * 0.4f;
            float cy2 = p2.y - (p2.y - p1.y) * 0.4f;
            dl->AddBezierCubic(p1,
                               {p1.x, cy1},
                               {p2.x, cy2},
                               p2, edgeCol, 1.5f * scale);
        }

        // ── Nodes ─────────────────────────────────────────────────────────
        ImVec2 mousePos = ImGui::GetIO().MousePos;
        for (auto& n : gModel.nodes) {
            const float nx = ox + n.cx - nodeW * 0.5f;
            const float ny = oy + n.cy - nodeH * 0.5f;
            ImVec2 mn{nx, ny};
            ImVec2 mx{nx + nodeW, ny + nodeH};

            // Hit-test
            bool hovered = ImGui::IsWindowHovered() &&
                           mousePos.x >= mn.x && mousePos.x <= mx.x &&
                           mousePos.y >= mn.y && mousePos.y <= mx.y;
            if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                if (gModel.selectedNodeId == n.id)
                    gModel.selectedNodeId.clear();
                else
                    gModel.selectedNodeId = n.id;
            }

            const bool selected = (gModel.selectedNodeId == n.id);
            const float rounding = 6.f * scale;

            // Fill
            dl->AddRectFilled(mn, mx, statusFillColor(n.status), rounding);

            // Selection highlight (glass row selected alpha)
            if (selected || hovered) {
                ImU32 hlCol = selected
                    ? theme::roleU32A(p.accent, 0.22f)
                    : theme::roleU32A(p.accent, 0.10f);
                dl->AddRectFilled(mn, mx, hlCol, rounding);
            }

            // Border
            float bThick = selected ? 2.f * scale : 1.f * scale;
            ImU32 bCol = selected
                ? theme::rgba(p.accent)
                : statusBorderColor(n.status);
            dl->AddRect(mn, mx, bCol, rounding, 0, bThick);

            // Badge pill (kind) — top-right corner
            {
                const char* badge = kindBadge(n.kind);
                ImVec2 badgePos{mx.x - 34.f * scale, mn.y + 3.f * scale};
                ImGui::SetCursorScreenPos(badgePos);
                ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(p.txt2));
                ImGui::SetWindowFontScale(0.72f);
                ImGui::TextUnformatted(badge);
                ImGui::SetWindowFontScale(1.f);
                ImGui::PopStyleColor();
            }

            // Label (truncated)
            {
                std::string lbl = n.label;
                if (lbl.size() > 16) lbl = lbl.substr(0, 14) + "...";
                ImVec2 textPos{mn.x + 6.f * scale, mn.y + 7.f * scale};
                ImGui::SetCursorScreenPos(textPos);
                ImGui::PushStyleColor(ImGuiCol_Text, theme::rgba(p.txt0));
                ImGui::TextUnformatted(lbl.c_str());
                ImGui::PopStyleColor();
            }

            // Status dot + sub-label
            {
                ImVec2 dotPos{mn.x + 6.f * scale, ny + nodeH * 0.62f};
                ImGui::SetCursorScreenPos(dotPos);
                ImGui::PushStyleColor(ImGuiCol_Text, statusBorderColor(n.status));
                ImGui::SetWindowFontScale(0.78f);
                ImGui::Text("* %s", statusStr(n.status));
                ImGui::SetWindowFontScale(1.f);
                ImGui::PopStyleColor();
            }
        }
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

}  // namespace (anon)

// ── Public entry points ───────────────────────────────────────────────────

void drawAgentDagPanel(app::AppState& /*s*/, aicad::ui::DagWidgetMode mode) {
    const float scale = theme::S();
    const auto& p = theme::pal();

    // Shot seam: allow scenarios.cpp to override the render mode before the frame.
    if (gHasForcedMode) { mode = gForcedMode; }

    // Lay out every frame (cheap for ~10 nodes)
    layoutDag(scale);

    if (!gModel.hasData) {
        ui::PanelScaffoldSpec spec{ "agent_dag", "Agent DAG", nullptr, false };
        ui::drawPanelScaffold(spec, gScaffoldState, [&]() {
            ui::drawPanelEmptyState("No agent workflow running",
                                    "Start a mission to see the live DAG.");
        });
        return;
    }

    const bool isCompact = (mode == aicad::ui::DagWidgetMode::Compact);
    const ImVec2 avail = ImGui::GetContentRegionAvail();

    if (isCompact) {
        // Compact mode: canvas only, smaller
        drawCanvas(avail.x, avail.y);
        return;
    }

    // Full mode: header + canvas (left) + detail pane (right) for selected node
    ui::PanelScaffoldSpec spec{
        "agent_dag", "Agent DAG", nullptr, false
    };
    gScaffoldState.statusText = gModel.missionName.empty()
        ? std::string() : ("Mission: " + gModel.missionName);

    ui::drawPanelScaffold(spec, gScaffoldState, [&]() {
        const ImVec2 contentAvail = ImGui::GetContentRegionAvail();

        const aicad::ui::DagWidgetNode* selectedNode = nullptr;
        if (!gModel.selectedNodeId.empty()) {
            for (auto& n : gModel.nodes)
                if (n.id == gModel.selectedNodeId) { selectedNode = &n; break; }
        }

        const float detailW = (selectedNode != nullptr) ? 240.f * scale : 0.f;
        const float canvasW = contentAvail.x - detailW;

        // Canvas column
        drawCanvas(canvasW, contentAvail.y);

        // Detail pane column
        if (selectedNode) {
            ImGui::SameLine(0.f, 0.f);
            ImGui::SetCursorPosX(ImGui::GetCursorPosX());
            drawDetailPane(*selectedNode, detailW);
        }
    });
}

void openAgentDagPanel() { gOpen = true; }

bool* agentDagPanelOpen() { return &gOpen; }

void seedAgentDagPanelFromJsonl(const std::string& jsonl_text) {
    gModel = aicad::ui::AgentDagModel{};
    gPanX = 0.f; gPanY = 0.f;
    std::istringstream in(jsonl_text);
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        try {
            applyEvent(nlohmann::json::parse(line));
        } catch (...) {}
    }
}

void agentDagShotSelectNode(const std::string& nodeId) {
    gModel.selectedNodeId = nodeId;
}

void agentDagShotForceMode(aicad::ui::DagWidgetMode mode) {
    gForcedMode    = mode;
    gHasForcedMode = true;
}

}  // namespace ui
