// Shared vertical-collapse animation for the pinned FeatureManager and Copilot glass columns
// (T-G21). Both columns used to shrink to a small square pill in both axes; they now roll UP
// toward their header strip instead — the header (tabs/title row + the collapse control) stays
// pinned at the column's fixed position while only the window HEIGHT animates down to the
// header's height, leaving the viewport visible underneath. One helper so both columns share
// identical motion instead of two hand-tuned implementations.
#pragma once

#include "imgui.h"

#include <cmath>

namespace ui {

struct PanelCollapseAnim {
    // Header-strip height, captured via measureHeader() right after the header content is drawn
    // each frame (one-frame lag — same trick the Copilot composer already uses for s_composerH).
    float headerH = 0.0f;
    // Current animated window height (px); negative = not yet initialized (snaps to target).
    float h = -1.0f;

    // Call BEFORE SetNextWindowSize/Begin: returns the window height to use this frame.
    float tick(bool collapsed, float fullH) {
        const float target = collapsed ? (headerH > 0.0f ? headerH : fullH) : fullH;
        const float dt = ImGui::GetIO().DeltaTime;
        if (h < 0.0f) { h = target; return h; }
        const float tau = 0.12f;  // exponential approach, ~120ms feel (matches ui_theme's approach())
        const float a = 1.0f - std::exp(-dt / tau);
        h += (target - h) * a;
        if (std::fabs(h - target) < 0.5f) h = target;
        return h;
    }

    // Call right after drawing the header content (before anything else in the body): records
    // this frame's header height from the current cursor position, symmetric top/bottom padded,
    // so a fully collapsed window shows exactly the header and nothing else.
    void measureHeader() {
        headerH = ImGui::GetCursorPosY() + ImGui::GetStyle().WindowPadding.y;
    }

    // True once the column has enough animated height left to bother drawing/interacting with
    // the body below the header. Gates the rest of the column's content so a collapsing/collapsed
    // panel doesn't keep building (and eating input for) content nobody can see.
    bool bodyVisible() const { return h > headerH + 2.0f; }
};

}  // namespace ui
