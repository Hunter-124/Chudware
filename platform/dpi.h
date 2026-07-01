// DPI / content-scale helpers — no OS #ifdef; GLFW + env override only.
#pragma once

struct GLFWwindow;

namespace platform {

// Read GLFW content scale; in screenshot mode returns 1.0 before env override.
float contentScale(GLFWwindow* win, bool screenshotMode = false);

}  // namespace platform
