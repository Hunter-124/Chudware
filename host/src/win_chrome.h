// Borderless window chrome for Windows: strips the OS title bar + frame while keeping
// native resize, Aero Snap, and maximize. The GUI draws its own title bar and publishes
// which horizontal spans of that bar are interactive (menus / window buttons) so the
// native hit-test can tell them apart from the draggable caption.
#pragma once

struct GLFWwindow;

namespace chrome {

struct HitLayout {
    int titleBarHeight = 40;  // px height of the custom title bar (the draggable strip)
    int resizeBorder = 6;     // px edge thickness used for resize hit-testing
    // Window-relative px spans that should receive clicks instead of dragging the window.
    int menuLeft = 0, menuRight = 0;          // the menu items
    int controlsLeft = 0, controlsRight = 0;  // the minimize/maximize/close buttons
};

// Install the custom frame. Call once, AFTER ImGui's GLFW backend is initialized so our
// WndProc sits on top of ImGui's. `layout` must outlive the window (read on the UI thread).
void install(GLFWwindow* win, HitLayout* layout);

}  // namespace chrome
