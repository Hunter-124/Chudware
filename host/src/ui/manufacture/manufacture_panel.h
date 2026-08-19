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
// cover the viewport, the doc-tab strip, or the ribbon. It is ONE compact frosted row — a kit chip
// carrying the mode, one dim explanation line that shortens in tiers rather than being clipped
// mid-sentence, and a right-aligned action. Offline Simulation is the SAFE state: calm neutral
// glass, Ok chip, secondary "Enter Live Machine…". LIVE MACHINE is the hazardous one: it is the
// only state that tints the chrome (statusErr wash), the only Err chip, and the only danger
// button. Height returns 0 when it must not draw, and reads nothing produced later in the frame
// (notably never st.mfgMirror, which is still null on the first frame).
float manufactureModeBarHeight(const AppState& st);
void  drawManufactureModeBar(AppState& st, float x, float y, float w, float h);

// Operator views hosted by the pinned left column (ui/layout.cpp Manufacture rail).
// Each draws widgets DIRECTLY into the caller's window (no Begin/BeginChild of its own),
// exactly like ui::drawCamTreeBody / ui::drawCamOutputBody. All three open with the same
// operator notices — the sticky request error as a kit inlineStatus, an unreachable gateway as a
// kit blockedState — because the rail shows one tab at a time and a notice parked on a sibling
// tab is invisible exactly when the operator needs it.
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
