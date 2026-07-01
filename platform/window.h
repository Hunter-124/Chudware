// Borderless window chrome: custom title bar + native resize / snap.
// OS-specific WndProc (Windows) or GLFW polling (Linux) lives in window_* .cpp only.
#pragma once

struct GLFWwindow;

namespace platform {

struct WindowChromeLayout {
    int titleBarHeight = 40;
    int resizeBorder = 6;
    int menuLeft = 0, menuRight = 0;
    int controlsLeft = 0, controlsRight = 0;
};

// Linux: prefer X11 for borderless self-move; falls back to GLFW_ANY_PLATFORM.
bool initGlfwPlatform();

// Borderless hints before glfwCreateWindow (screenshot mode hides + disables scale-to-monitor).
void applyBorderlessWindowHints(bool screenshotMode);

// Install custom frame after ImGui's GLFW backend init (WndProc chains on Windows).
void installWindowChrome(GLFWwindow* win, WindowChromeLayout* layout);

// Per-frame chrome update — no-op on Windows; polls mouse on Linux for drag/resize.
void tickWindowChrome(GLFWwindow* win, WindowChromeLayout* layout);

}  // namespace platform
