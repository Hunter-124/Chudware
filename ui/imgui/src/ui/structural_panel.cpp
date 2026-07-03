// T-U7 structural frame viewer — see structural_panel.h.
#include "structural_panel.h"
#include "net_canvas_panel.h"
#include "net_canvas_view.h"
#include "structural_view.h"  // ui/abstract adapter

#include "app/app_state.h"
#include <string>

namespace ui {

namespace {
bool gStructuralOpen = false;
aicad::ui::NetCanvasModel gStructuralModel;
}  // namespace

void seedStructuralPanelFromJson(const std::string& doc_json, const std::string& results_json) {
    aicad::ui::structuralDocToCanvas(doc_json, results_json, gStructuralModel);
    gStructuralModel.state = aicad::ui::NetCanvasPanelState::Ready;
}

void openStructuralPanel() { gStructuralOpen = true; }
bool* structuralPanelOpen() { return &gStructuralOpen; }

void drawStructuralPanel(app::AppState& /*s*/) {
    // Begin/End is in application.cpp — just render the canvas body here.
    ui::drawNetCanvas(gStructuralModel);
}

}  // namespace ui
