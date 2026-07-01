// Runtime resource loading (window icon, bundled asset roots).
#pragma once

#include <string>

struct GLFWwindow;

namespace platform {

// Load a PNG and set GLFW window icons (multi-size when possible).
void setWindowIcon(GLFWwindow* win, const std::string& pngPath);

}  // namespace platform
