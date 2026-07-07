// T-U2 Parts Browser panel — federated search, detail card, insert via part_place (Pillar 17).
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/parts_view.h (no ImGui includes there).  Screenshot-verifiable via
// AI_CAD_SHOT=parts_browser (worker one-shot shot, same timing group as equationsShotDone).
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window, same convention as
// drawPreviewPane() / drawScaffoldDemoPanel().
void drawPartsPanel(app::AppState& s);

// Open the (closable) parts browser pane.  Costs nothing while closed.
void openPartsPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open (close box).
bool* partsPanelOpen();

// Set the active domain filter from the nav rail (workspace_parts.cpp).
// Applied on the next drawPartsPanel() call; re-fires the current query with the new domain.
void partsSetNavDomain(const std::string& domain);

// Seed the panel model from a part_search JSON result (scenario/shot use only — populates
// gModel so the headless screenshot shows real results without needing a live async RPC round).
// json_result is the raw JSON string returned by the part_search worker method.
void seedPartsPanelFromJson(const std::string& json_result, const std::string& query);

}  // namespace ui
