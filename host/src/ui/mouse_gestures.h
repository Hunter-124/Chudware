#pragma once
// Chudware mouse gesture wheel (C25).
// Right-drag in the 3D viewport → 8-segment radial wheel → view preset shortcuts.
// Right-click WITHOUT drag is reserved for a future context menu (see kRmbContextMenuHook).
#include "imgui.h"

struct AppState;

namespace ui {

enum class ViewPreset { Front, Back, Left, Right, Top, Bottom, Isometric, ZoomFit };

// Apply a view preset to AppState (single source of truth shared with the HUD toolbar).
void applyViewPreset(AppState& st, ViewPreset preset);

class MouseGestures {
public:
    // Call every frame after picking, before other overlays.
    // imgHovered: ImGui::IsItemHovered() on the 3D image.
    // vr0/vr1: viewport image rect (screen coords).
    void update(AppState& st, bool imgHovered, ImVec2 vr0, ImVec2 vr1);
    void draw(ImDrawList* dl);

    bool active() const { return m_active; }

private:
    bool   m_active  = false;
    ImVec2 m_center  = {0, 0};
    int    m_segment = -1;   // -1 = dead-zone / none; 0..7 = hovered segment index

    // Returns segment index (0..7) for angle, or -1 if inside dead-zone.
    int segmentAt(ImVec2 mousePos) const;
};

}  // namespace ui
