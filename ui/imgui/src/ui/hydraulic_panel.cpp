// T-U7 hydraulic schematic viewer — see hydraulic_panel.h.
#include "hydraulic_panel.h"
#include "net_canvas_panel.h"
#include "net_canvas_view.h"
#include "hydraulic_view.h"  // ui/abstract adapter

#include "app/app_state.h"
#include <string>

namespace ui {

namespace {
bool gHydraulicOpen = false;
aicad::ui::NetCanvasModel gHydraulicModel;
}  // namespace

void seedHydraulicPanelFromJson(const std::string& doc_json, const std::string& sizing_json) {
    aicad::ui::hydraulicDocToCanvas(doc_json, sizing_json, gHydraulicModel);
    gHydraulicModel.state = aicad::ui::NetCanvasPanelState::Ready;
}

void openHydraulicPanel() { gHydraulicOpen = true; }
bool* hydraulicPanelOpen() { return &gHydraulicOpen; }

void drawHydraulicPanel(app::AppState& /*s*/) {
    // Begin/End is in application.cpp — just render the canvas body here.
    ui::drawNetCanvas(gHydraulicModel);
}

}  // namespace ui
