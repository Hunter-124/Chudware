#pragma once
// AI-CAD 'S' shortcut bar (C25).
// Press 'S' while the viewport is hovered (and no text input is active) →
// a small floating toolbar appears at the mouse position with context-aware commands.
// Model mode: sketch_new, extrude_boss, extrude_cut, revolve_boss, fillet, chamfer.
// Sketch mode: sk_line, sk_circle, sk_rect_corner, sk_arc_3pt, smart_dim, sketch_exit.
#include "imgui.h"

struct AppState;

namespace ui {

class ShortcutBar {
public:
    // Call every frame while viewport is hovered.
    // inSketch: true while a sketch is being edited.
    // vr0/vr1: viewport image rect in screen coords (for clamping).
    void update(AppState& st, bool imgHovered, bool inSketch, ImVec2 vr0, ImVec2 vr1);
    void draw(AppState& st, bool inSketch);

    bool visible() const { return m_visible; }

private:
    bool   m_visible  = false;
    ImVec2 m_pos      = {0, 0};   // top-left of the bar (clamped inside viewport)

    void dismiss() { m_visible = false; }
};

}  // namespace ui
