// T-W17 Multi-viewport Watch Grid panel — see watch_grid_panel.h.
//
// Architecture:
//   - View-model: aicad::ui::WatchGridModel (ui/abstract/watch_grid_view.h)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Render thumbnails via theme/texture_cache.h loadPreviewTexture (same path
//     as preview_pane.cpp / artifact_cards.cpp).
//   - Data: workspace_status.json (T-W16 shape) from a run dir; render paths are
//     resolved relative to the json file's directory. No worker RPC needed.

#include "watch_grid_panel.h"
#include "panel_scaffold.h"
#include "watch_grid_view.h"   // ui/abstract/

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "theme/texture_cache.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cmath>
#include <fstream>
#include <sstream>
#include <string>

namespace ui {

namespace {

bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::WatchGridModel gModel;

using aicad::ui::WatchState;

std::string dirOf(const std::string& path) {
    const auto slash = path.find_last_of("/\\");
    return slash == std::string::npos ? std::string() : path.substr(0, slash);
}

ImVec4 statusColor(const std::string& st) {
    if (st == "failed")   return TVEC(bad);
    if (st == "building") return TVEC(warn);
    if (st == "done")     return TVEC(good);
    return TVEC(txt2);
}

}  // namespace

// ---- Public: load a workspace_status.json ----------------------------------

void loadWatchWorkspace(const std::string& absWorkspaceJsonPath) {
    gModel = aicad::ui::WatchGridModel{};
    std::ifstream f(absWorkspaceJsonPath, std::ios::binary);
    if (!f) { gModel.state = WatchState::Error; gModel.errorMsg = "workspace_status.json not found"; return; }
    std::stringstream ss; ss << f.rdbuf();
    nlohmann::json j;
    try { j = nlohmann::json::parse(ss.str()); }
    catch (...) { gModel.state = WatchState::Error; gModel.errorMsg = "malformed workspace_status.json"; return; }

    gModel.run_id = j.value("run_id", std::string());
    const std::string baseDir = dirOf(absWorkspaceJsonPath);
    if (j.contains("docs") && j["docs"].is_array()) {
        for (const auto& d : j["docs"]) {
            aicad::ui::WatchTile t;
            t.doc_id      = d.value("doc_id", std::string());
            t.title       = d.value("title", std::string());
            t.owner       = d.value("owner", std::string());
            t.model       = d.value("model", std::string());
            t.node_status = d.value("node_status", std::string());
            t.lease       = d.value("lease", std::string());
            const std::string rel = d.value("latest_render", std::string());
            if (!rel.empty())
                t.render_abs_path = (rel.front() == '/' ? rel : baseDir + "/" + rel);
            gModel.tiles.push_back(std::move(t));
        }
    }
    gModel.state = gModel.tiles.empty() ? WatchState::Empty : WatchState::Ready;
}

namespace {

void drawTile(app::AppState& s, int idx) {
    const float S = theme::S();
    // T-DP13: mutable ref so we can tick spinT each frame.
    auto& t = gModel.tiles[idx];
    const bool building = (t.node_status == "building");
    ImGui::PushID(idx);

    // T-DP13: tick spinner accumulator for live activity animation.
    if (building) t.spinT += ImGui::GetIO().DeltaTime;

    const float tileW = ImGui::GetContentRegionAvail().x;
    const ImVec2 mn = ImGui::GetCursorScreenPos();

    // Thumbnail (fixed aspect, letterboxed into the tile width).
    int tw = 0, th = 0;
    ImTextureID tex = t.render_abs_path.empty() ? (ImTextureID)0
                        : loadPreviewTexture(t.render_abs_path, &tw, &th);
    const float imgH = tileW * 0.62f;
    if (tex) {
        float w = tileW, h = imgH;
        if (tw > 0 && th > 0) {
            const float ar = (float)tw / (float)th;
            if (w / h > ar) w = h * ar; else h = w / ar;
        }
        // center the image in the tile column
        const float padX = (tileW - w) * 0.5f;
        ImGui::SetCursorScreenPos(ImVec2(mn.x + padX, mn.y));
        ImGui::Image(tex, ImVec2(w, h));
        ImGui::SetCursorScreenPos(ImVec2(mn.x, mn.y + imgH + 2.f * S));
    } else {
        ImGui::Dummy(ImVec2(tileW, imgH));
        ImGui::TextColored(TVEC(txt2), "(no render yet)");
    }

    // Title row.
    ImGui::TextUnformatted(t.title.empty() ? t.doc_id.c_str() : t.title.c_str());

    // T-DP13: live activity indicator row — spinning arc + "building…" text when
    // node_status == "building".  Reuses the same spinning-arc vocabulary as
    // agent_panel.cpp drawActionCard (ActionStatus::Running).
    if (building) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float arcR = 6.f * S;
        ImVec2 cp = ImGui::GetCursorScreenPos();
        cp.x += arcR + 2.f * S;
        cp.y += arcR + 1.f * S;
        const float angle = t.spinT * 3.8f;
        dl->PathClear();
        dl->PathArcTo(cp, arcR, angle, angle + 4.2f, 12);
        dl->PathStroke(TCOL(accent), 0, 2.f * S);
        ImGui::Dummy(ImVec2(arcR * 2.f + 4.f * S, arcR * 2.f + 2.f * S));
        ImGui::SameLine(0, 6.f * S);
        ImGui::TextColored(ImGui::ColorConvertU32ToFloat4(TCOL(accent)), "building\xe2\x80\xa6");
    }

    // Chips row: owner, model, lease — status badge shown only when not actively building
    // (the spinning arc + "building…" text above already communicates that state).
    if (!building) {
        ImGui::TextColored(statusColor(t.node_status), "%s", t.node_status.c_str());
        ImGui::SameLine();
    }
    ImGui::TextColored(TVEC(accent), "%s", t.owner.c_str());
    ImGui::TextColored(TVEC(txt2), "%s  \xc2\xb7  %s lease", t.model.c_str(), t.lease.c_str());

    // Click-through button spanning the tile.
    if (ImGui::Button("Open doc", ImVec2(-1, 0))) {
        gModel.selectedTile = idx;
        // Click-through hook: surface the doc id in the status footer (real doc
        // activation is a worker action wired by the workspace/doc-tabs seam;
        // here we record the intent so the shot + offline mode stay backend-free).
        gScaffoldState.statusText = "selected " + t.doc_id;
    }
    ImGui::Dummy(ImVec2(0, 6.f * S));
    ImGui::PopID();
}

void drawContent(app::AppState& s) {
    switch (gModel.state) {
    case WatchState::Idle:  drawPanelEmptyState("No workspace loaded", "start a multi-doc mission"); return;
    case WatchState::Error: drawPanelEmptyState("Cannot load workspace", gModel.errorMsg.c_str());   return;
    case WatchState::Empty: drawPanelEmptyState("No documents in this workspace", "nothing to watch yet"); return;
    default: break;
    }

    // Responsive grid: choose column count from available width.
    const float avail = ImGui::GetContentRegionAvail().x;
    const float minTile = 220.f * theme::S();
    int cols = (int)(avail / minTile);
    if (cols < 1) cols = 1;
    if (cols > 3) cols = 3;

    if (ImGui::BeginTable("##watchGrid", cols, ImGuiTableFlags_SizingStretchSame)) {
        for (int i = 0; i < (int)gModel.tiles.size(); ++i) {
            ImGui::TableNextColumn();
            drawTile(s, i);
        }
        ImGui::EndTable();
    }
}

}  // namespace

// ---- Public API ------------------------------------------------------------

void drawWatchGridPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id    = "watch_grid";
    spec.title = "Agent Watch";
    spec.icon  = "assembly";
    spec.hasSearch = false;

    if (gScaffoldState.statusText.empty())
        gScaffoldState.statusText = "Live part previews — one per agent doc";
    if (gModel.state == WatchState::Ready && gScaffoldState.statusText.rfind("selected", 0) != 0)
        gScaffoldState.statusText = std::to_string((int)gModel.tiles.size()) + " doc(s) watched";

    ui::drawPanelScaffold(spec, gScaffoldState, [&]() { drawContent(s); });
}

void openWatchGridPanel() { gOpen = true; }
bool* watchGridPanelOpen() { return &gOpen; }

}  // namespace ui
