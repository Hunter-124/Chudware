// Shared vertical-collapse animation for the pinned FeatureManager and Copilot glass columns.
// Both columns used to shrink to a small square pill in both axes; they now roll UP
// toward their header strip instead — the header (tabs/title row + the collapse control) stays
// pinned at the column's fixed position while only the window HEIGHT animates down to the
// header's height, leaving the viewport visible underneath. One helper so both columns share
// identical motion instead of two hand-tuned implementations.
#pragma once

#include "imgui.h"
#include "anim.h"

#include <algorithm>

#include <cmath>

namespace ui {

struct PanelCollapseAnim {
    // Header-strip height, captured via measureHeader() right after the header content is drawn
    // each frame (one-frame lag — same trick the Copilot composer already uses for s_composerH).
    float headerH = 0.0f;
    // Current window height (px) returned by the last tick(); negative = not yet ticked.
    float h = -1.0f;
    // Animated collapse fraction: 0 = fully expanded, 1 = rolled up to the header; negative = not
    // yet initialized (snaps to the requested state).
    float t = -1.0f;
    // snap(collapsed) ran before the header was ever measured: hold a 1 px sliver (not fullH) until
    // measureHeader() supplies the real header height.
    bool tinyUntilMeasured = false;

    // Call BEFORE SetNextWindowSize/Begin: returns the window height to use this frame.
    // Only the collapse/expand TRANSITION animates. The expanded height is the live `fullH` every
    // frame, so a ribbon/status-bar height change re-pins the column's bottom edge to the status
    // bar immediately instead of easing past it (overrunning) or stopping short for several frames.
    float tick(bool collapsed, float fullH) {
        const float goal = collapsed ? 1.0f : 0.0f;
        if (t < 0.0f) {
            t = goal;
        } else if (t != goal) {
            const float a = 1.0f - std::exp(-anim::dt() / anim::kFast);
            t += (goal - t) * a;
            if (std::fabs(t - goal) < 0.002f) t = goal;
            else anim::requestFrames();
        }
        if (headerH > 0.0f) tinyUntilMeasured = false;
        const float rolledH = std::min(fullH,
            headerH > 0.0f ? headerH : (tinyUntilMeasured ? 1.0f : fullH));
        h = fullH + (rolledH - fullH) * t;
        return h;
    }

    // Snap immediately to the collapsed or expanded target with no animation — use from code paths
    // that force a state deterministically (e.g. CHUDWARE_FM_COLLAPSED env var in screenshot mode)
    // so headless captures don't depend on how many frames have elapsed.
    // For collapsed: sets h to headerH if already measured, otherwise to 1.0f (tiny, so bodyVisible()
    // returns false immediately) — tick() will hold near headerH once it's measured the next frame.
    // For expanded: sets h directly to fullH.
    void snap(bool collapsed, float fullH) {
        t = collapsed ? 1.0f : 0.0f;
        if (!collapsed) { h = fullH; tinyUntilMeasured = false; return; }
        // If headerH is known, snap exactly; otherwise use 1px so bodyVisible() is already false
        // and tick() will snap to headerH as soon as it's measured (one-frame lag, same as init).
        tinyUntilMeasured = headerH <= 0.0f;
        h = headerH > 0.0f ? headerH : 1.0f;
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
