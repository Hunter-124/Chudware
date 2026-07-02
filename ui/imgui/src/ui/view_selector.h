#pragma once
// SolidWorks-style glass-box view selector (Space).
#include "imgui.h"

namespace app { struct AppState; }
class Renderer;

namespace ui {

class ViewSelector {
public:
    void update(app::AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                ImVec2 vr0, ImVec2 vr1);
    void draw(app::AppState& st, Renderer& renderer, ImDrawList* dl, ImVec2 vr0, ImVec2 vr1);

    bool active() const { return m_active; }
    void open() {
        m_active = true;
        m_hoverZone = -1;
        m_hoverDwell = 0.0f;
        m_previewReady = false;
    }
    void openFramed(app::AppState& st);

private:
    void close() { m_active = false; m_hoverZone = -1; }

    bool m_active = false;
    int  m_hoverZone = -1;
    float m_hoverYaw = 0.0f, m_hoverPitch = 0.0f;
    float m_hoverDwell = 0.0f;
    bool  m_previewReady = false;
};

}  // namespace ui
