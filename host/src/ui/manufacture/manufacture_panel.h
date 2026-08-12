#pragma once

#include <cstddef>
#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Manufacture workspace bootstrap. The operator surface no longer floats over the
// native viewport: the Offline/Live mode strip is real chrome drawn by app.cpp
// directly under the ribbon (drawManufactureModeBar) and Devices / Run / Attention
// are tabs of the pinned left column (ui/layout.cpp). This function only ensures the
// mirror, queues the bootstrap RPCs and draws the modal approval dialog — it opens no
// windows of its own.
void drawManufacturePanel(AppState& st);

// Safety-critical Offline/Live mode strip. app.cpp draws this as REAL chrome directly under the
// ribbon and reserves its height before the dockspace + overlay band are computed, so it can never
// cover the viewport, the doc-tab strip, or the ribbon. Returns 0 when it must not draw.
float manufactureModeBarHeight(const AppState& st);
void  drawManufactureModeBar(AppState& st, float x, float y, float w, float h);

// Operator views hosted by the pinned left column (ui/layout.cpp Manufacture rail).
// Each draws widgets DIRECTLY into the caller's window (no Begin/BeginChild of its own),
// exactly like ui::drawCamTreeBody / ui::drawCamOutputBody.
void drawManufactureDevicesBody(AppState& st);
void drawManufactureRunBody(AppState& st);
void drawManufactureAttentionBody(AppState& st);

// Unacknowledged critical attention items — drives the rail tab's attention dot.
std::size_t manufactureCriticalCount(const AppState& st);

// App-frame dispatch hook. Panels enqueue intent; this function sends one async
// RPC at a time and feeds only returned projections into MfgMirror.
void dispatchManufactureRequests(AppState& st);
void queueManufactureRequest(AppState& st, std::string method,
                             nlohmann::json params, std::string context = {});

// Deterministic CHUDWARE_SHOT=manufacture fixture.
void seedManufactureScenario(AppState& st);

}  // namespace ui
