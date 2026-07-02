// T-U4 BOM & sourcing panel — rollup table, price column, quote-out action, match review.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/bom_view.h (no ImGui includes there).  Screenshot-verifiable via
// AI_CAD_SHOT=bom (worker one-shot shot, same timing group as treeShotDone/ecadShotDone).
//
// Price contract: unit_cost/ext_cost cells display the em-dash sentinel (—) when unpriced.
// A fabricated price is NEVER shown.  See ui/abstract/bom_view.h BOM_UNPRICED.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window, same convention as
// drawPreviewPane() / drawScaffoldDemoPanel().
void drawBomPanel(app::AppState& s);

// Open the (closable) BOM pane.  Costs nothing while closed.
void openBomPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open (close box).
bool* bomPanelOpen();

// Seed the panel model from a bom_price JSON result (scenario/shot use only — populates
// gBomModel so the headless screenshot shows real rows without a live async RPC round-trip).
// json_result is the raw result dict returned by the bom_price worker method.
void seedBomPanelFromJson(const std::string& json_result);

}  // namespace ui
