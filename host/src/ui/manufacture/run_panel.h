#pragma once

struct AppState;

namespace ui {

// Run state, freshness-strict telemetry and seq-stamped two-stage gate report.
void drawManufactureRunPanel(AppState& st);

}  // namespace ui
