// Interaction state of the Hydraulics central schematic canvas (sim/hydraulic_canvas.cpp).
// HydraulicPanel owns one instance (`canvas_`). The canvas TU owns this struct; panel code
// (command routing, forms) may only read/write the fields documented as shared below.
#pragma once

#include <nlohmann/json.hpp>

#include <string>

namespace ui::hydcanvas {

struct State {
    // Shared with the panel: the ribbon's Connect command arms repeat port-to-port connection.
    // Completing a connection keeps this mode armed; Esc or toggling the tool stops it.
    bool connectArmed = false;
    // Click-to-place captures the same document/study/revision-scoped payload as palette dragging.
    nlohmann::json placement;
    bool focused = false;
    bool portPressed = false;
    std::string pressedComponent, pressedPort;
};

}  // namespace ui::hydcanvas
