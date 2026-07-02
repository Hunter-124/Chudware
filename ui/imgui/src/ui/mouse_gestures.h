#pragma once
// Right-drag radial gesture wheel → view presets.
#include "imgui.h"

namespace app { struct AppState; }

namespace ui {

enum class ViewPreset { Front, Back, Left, Right, Top, Bottom, Isometric, ZoomFit };

void applyViewPreset(app::AppState& st, ViewPreset preset);

class MouseGestures {
public:
    void update(app::AppState& st, bool imgHovered, ImVec2 vr0, ImVec2 vr1);
    void draw(ImDrawList* dl);
    bool active() const { return m_active; }

private:
    bool   m_active  = false;
    ImVec2 m_center  = {0, 0};
    int    m_segment = -1;
    int segmentAt(ImVec2 mousePos) const;
};

}  // namespace ui
