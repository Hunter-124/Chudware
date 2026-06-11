#pragma once
// AI-CAD View Selector — SolidWorks-style "glass box" (C25, wave W2-E).
// Press Space while the viewport is hovered (and no text input is active, not in sketch mode) →
// a translucent corner-truncated box is drawn around the model. Hovering a face or corner
// highlights it; clicking animates the camera to that standard orientation.
// Space / Esc / click-on-empty dismisses. Pure ImDrawList; trivial per-frame cost.
#include "imgui.h"

struct AppState;
class Renderer;

namespace ui {

class ViewSelector {
public:
    // Call every frame, in the viewport overlay section (after the heads-up toolbar block).
    // imgHovered: ImGui::IsItemHovered() on the 3D image.
    // inSketch:   true while a sketch is being edited (the selector is disabled then).
    // vr0/vr1:    viewport image rect in screen coords.
    void update(AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                ImVec2 vr0, ImVec2 vr1);
    void draw(AppState& st, ImDrawList* dl, ImVec2 vr0, ImVec2 vr1);

    // True while the glass box is shown — hook code uses this to suppress normal click-picking.
    bool active() const { return m_active; }

private:
    void close() { m_active = false; m_hoverZone = -1; }

    bool m_active = false;
    int  m_hoverZone = -1;   // index into the per-frame zone table, or -1
};

}  // namespace ui
