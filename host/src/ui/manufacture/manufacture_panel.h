#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Draws the mode banner and operator drawer over the existing native viewport.
// No geometry surface or controller/run state machine is created here.
void drawManufacturePanel(AppState& st);

// App-frame dispatch hook. Panels enqueue intent; this function sends one async
// RPC at a time and feeds only returned projections into MfgMirror.
void dispatchManufactureRequests(AppState& st);
void queueManufactureRequest(AppState& st, std::string method,
                             nlohmann::json params, std::string context = {});

// Deterministic CHUDWARE_SHOT=manufacture fixture.
void seedManufactureScenario(AppState& st);

}  // namespace ui
