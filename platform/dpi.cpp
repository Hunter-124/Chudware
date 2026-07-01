#include "platform/dpi.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <cstdlib>

namespace platform {

float contentScale(GLFWwindow* win, bool screenshotMode) {
    float xs = 1.0f, ys = 1.0f;
    if (win) glfwGetWindowContentScale(win, &xs, &ys);
    float scale = screenshotMode ? 1.0f : (xs > ys ? xs : ys);
    if (const char* envScale = std::getenv("AI_CAD_SCALE")) {
        float v = (float)std::atof(envScale);
        if (v >= 0.5f && v <= 4.0f) scale = v;
    }
    if (scale < 0.5f) scale = 1.0f;
    return scale;
}

}  // namespace platform
