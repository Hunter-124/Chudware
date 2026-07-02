// See scaffold_demo_panel.h.
#include "scaffold_demo_panel.h"

#include "panel_scaffold.h"
#include "scaffold_demo_view.h"  // ui/abstract — pure view-model, no ImGui includes
#include "theme/theme.h"

#include "imgui.h"

namespace ui {

namespace {

bool gOpen = false;
ui::PanelScaffoldState gState;

// Seed data: a plausible stand-in for what T-U2 (parts browser) or T-U4 (BOM) would populate from
// a worker RPC result. Fixed here since this panel exists to demo the scaffold, not fetch data.
aicad::ui::ScaffoldDemoModel demoModel() {
    aicad::ui::ScaffoldDemoModel m;
    m.rows.push_back({"row_1", "M3x10 Socket Head Cap Screw", "ISO 4762 · steel · zinc"});
    m.rows.push_back({"row_2", "608ZZ Deep Groove Bearing", "8mm bore · sealed"});
    m.rows.push_back({"row_3", "6061-T6 Aluminum Plate", "12mm · mill stock"});
    return m;
}

}  // namespace

void drawScaffoldDemoPanel(app::AppState& /*s*/) {
    static const aicad::ui::ScaffoldDemoModel model = demoModel();

    PanelScaffoldSpec spec;
    spec.id = "scaffold_demo";
    spec.title = "Scaffold Demo";
    spec.hasSearch = true;
    spec.searchHint = "Filter rows...";

    gState.statusText = "T-U1 kit demo - not a shipped feature";

    drawPanelScaffold(spec, gState, [&]() {
        const auto rows = aicad::ui::filterScaffoldDemoRows(model, gState.searchText);
        if (rows.empty()) {
            drawPanelEmptyState("No matching rows", "try a different search term");
            return;
        }
        const float S = theme::S();
        for (const auto& row : rows) {
            ImGui::PushID(row.id.c_str());
            // Background rect is added to the draw list BEFORE the text below, so it paints
            // behind (draw-list painter's algorithm) — see feature_tree.cpp's glassRowAtCursor
            // note: the helper is draw-only and does not advance the cursor.
            const ImVec2 rowMn = ImGui::GetCursorScreenPos();
            const float rowH = ImGui::GetTextLineHeightWithSpacing() * 2.f + 6.f * S;
            const ImVec2 rowMx(rowMn.x + ImGui::GetContentRegionAvail().x, rowMn.y + rowH);
            theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMn, rowMx, 4.f * S);

            ImGui::Indent(8.f * S);
            ImGui::Dummy(ImVec2(0, 3.f * S));
            ImGui::TextUnformatted(row.label.c_str());
            ImGui::TextColored(TVEC(txt2), "%s", row.detail.c_str());
            ImGui::Unindent(8.f * S);
            ImGui::Dummy(ImVec2(0, theme::U(0.25f)));
            ImGui::PopID();
        }
    });
}

void openScaffoldDemoPanel() { gOpen = true; }

bool* scaffoldDemoPanelOpen() { return &gOpen; }

}  // namespace ui
