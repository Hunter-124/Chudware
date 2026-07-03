// T-U7 piping network viewer — see piping_panel.h.
#include "piping_panel.h"
#include "net_canvas_panel.h"
#include "net_canvas_view.h"
#include "piping_view.h"   // ui/abstract adapter

#include "app/app_state.h"
#include <string>

namespace ui {

namespace {
bool gPipingOpen = false;
aicad::ui::NetCanvasModel gPipingModel;
}  // namespace

void seedPipingPanelFromJson(const std::string& doc_json, const std::string& solve_json) {
    aicad::ui::pipingDocToCanvas(doc_json, solve_json, gPipingModel);
    gPipingModel.state = aicad::ui::NetCanvasPanelState::Ready;
}

void openPipingPanel() { gPipingOpen = true; }
bool* pipingPanelOpen() { return &gPipingOpen; }

void drawPipingPanel(app::AppState& /*s*/) {
    // Begin/End is in application.cpp — just render the canvas body here.
    ui::drawNetCanvas(gPipingModel);
}

}  // namespace ui
