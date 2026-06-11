#pragma once
// AI-CAD Section View — SolidWorks-style multi-plane section (C28-C, wave C28).
// Up to 3 simultaneous clip planes; the visible solid is their intersection (zonal).
// Each plane has its own preset/offset/flip controls and an orange drag arrow.
// Panel: Section 1 row (always on), + collapsible Section 2 / Section 3 rows.
// OK collapses to "Section ×N ✓" chip; X/Esc disables all planes.
// The renderer owns the actual clip + cap (Renderer::setSectionPlanes).
#include "imgui.h"

struct AppState;
class Renderer;

namespace ui {

class SectionTool {
public:
    void toggle(AppState& st);
    bool active() const { return m_active; }

    void updateAndDraw(AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                       ImVec2 vr0, ImVec2 vr1);

    // Public so the file-local anonymous namespace in section_tool.cpp can reference it.
    enum class Preset { Front, Top, Right };

private:
    static constexpr int kMaxPlanes = 3;

    struct PlaneState {
        bool   enabled = true;
        bool   flip = false;
        Preset preset = Preset::Right;
        float  normal[3] = {1, 0, 0};
        float  offset = 0.0f;
        bool   haveModel = false;
        // arrow drag state
        bool   dragging = false;
        float  dragStartOffset = 0;
        ImVec2 dragStartMouse{0, 0};
        float  dragWorldPerPx = 0;
        float  dragDirX = 0, dragDirY = 0;
    };

    void applyPreset(Renderer& renderer, int pi, Preset p);
    void pushPlanes(Renderer& renderer);

    bool  m_active = false;
    bool  m_collapsed = false;
    bool  m_showCap = true;
    int   m_planeCount = 1;         // 1..3: how many plane rows are visible
    PlaneState m_planes[kMaxPlanes];
    bool  m_envChecked = false;
};

}  // namespace ui
