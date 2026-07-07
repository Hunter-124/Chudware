// Borderless window chrome for Windows: strips the OS title bar + frame while keeping
// native resize, Aero Snap, and maximize. The GUI draws its own title bar and publishes
// which horizontal spans of that bar are interactive (menus / window buttons) so the
// native hit-test can tell them apart from the draggable caption.
#pragma once

#include <array>

struct GLFWwindow;

namespace chrome {

// An individual interactive span (a single button / tab / menu) within the title bar row.
// x0..x1 are window-relative pixel coords (exclusive end). interactiveRanges are iterated
// in WM_NCHITTEST / Linux tick to give every chip a click-through region.
struct HitSpan { int x0 = 0, x1 = 0; };

static constexpr int kMaxHitSpans = 32;

struct HitLayout {
    int titleBarHeight = 40;  // px height of the custom title bar (the draggable strip)
    int resizeBorder = 6;     // px edge thickness used for resize hit-testing

    // Legacy coarse spans (kept for backward compat — win_chrome uses interactiveRanges now).
    int menuLeft = 0, menuRight = 0;          // the menu / left-group items
    int controlsLeft = 0, controlsRight = 0;  // the minimize/maximize/close buttons

    // Fine-grained per-button interactive spans. Fill with addSpan(); clear each frame with
    // clearSpans(). The title bar code populates these; win_chrome iterates them.
    std::array<HitSpan, kMaxHitSpans> spans{};
    int spanCount = 0;

    void clearSpans() { spanCount = 0; }
    void addSpan(int x0, int x1) {
        if (spanCount < kMaxHitSpans) spans[spanCount++] = {x0, x1};
    }
    bool hitAny(int x) const {
        for (int i = 0; i < spanCount; ++i)
            if (x >= spans[i].x0 && x < spans[i].x1) return true;
        return false;
    }
};

// Install the custom frame. Call once, AFTER ImGui's GLFW backend is initialized so our
// WndProc sits on top of ImGui's. `layout` must outlive the window (read on the UI thread).
void install(GLFWwindow* win, HitLayout* layout);

// Per-frame chrome update. No-op on Windows (the WndProc hit-test drives drag/resize at the
// OS level); on Linux/macOS it polls the mouse and implements drag / edge-resize / double-
// click-maximize for the borderless window, using the same `layout` spans. Call once per
// frame after glfwPollEvents().
void tick(GLFWwindow* win, HitLayout* layout);

}  // namespace chrome
