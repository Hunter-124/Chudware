#pragma once

struct AppState;

namespace ui {

// Device list + immutable capability truth projection. Returns no decisions;
// buttons only enqueue gateway requests.
void drawManufactureDevicePanel(AppState& st);

}  // namespace ui
