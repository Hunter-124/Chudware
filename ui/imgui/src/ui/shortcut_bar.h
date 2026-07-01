#pragma once
// Press 'S' while the viewport is hovered (no text input) → floating command toolbar at the mouse.
#include "imgui.h"

namespace app { struct AppState; }

namespace ui {

class ShortcutBar {
public:
    void update(app::AppState& st, bool imgHovered, bool inSketch, ImVec2 vr0, ImVec2 vr1);
    void draw(app::AppState& st, bool inSketch);
    bool visible() const { return m_visible; }

private:
    bool   m_visible = false;
    ImVec2 m_pos{0.f, 0.f};
    void dismiss() { m_visible = false; }
};

} // namespace ui
