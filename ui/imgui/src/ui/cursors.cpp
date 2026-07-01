// Cursor manager (W1-B). Ported from host/src/ui/cursors.cpp.
#include "ui/cursors.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"

namespace ui {

static GLFWcursor* s_crosshair = nullptr;
static GLFWcursor* s_hand      = nullptr;
static GLFWcursor* s_resizeH   = nullptr;
static GLFWcursor* s_resizeV   = nullptr;

static CursorKind  s_request = CursorKind::Default;
static bool        s_imguiCursorSuspended = false;

void initCursors(GLFWwindow* /*win*/) {
    destroyCursors();
    s_crosshair = glfwCreateStandardCursor(GLFW_CROSSHAIR_CURSOR);
    s_hand      = glfwCreateStandardCursor(GLFW_HAND_CURSOR);
    s_resizeH   = glfwCreateStandardCursor(GLFW_HRESIZE_CURSOR);
    s_resizeV   = glfwCreateStandardCursor(GLFW_VRESIZE_CURSOR);
}

void destroyCursors() {
    if (s_crosshair) { glfwDestroyCursor(s_crosshair); s_crosshair = nullptr; }
    if (s_hand)      { glfwDestroyCursor(s_hand);      s_hand = nullptr; }
    if (s_resizeH)   { glfwDestroyCursor(s_resizeH);   s_resizeH = nullptr; }
    if (s_resizeV)   { glfwDestroyCursor(s_resizeV);   s_resizeV = nullptr; }
}

void requestCursor(CursorKind k) {
    s_request = k;
}

static GLFWcursor* forKind(CursorKind k) {
    switch (k) {
        case CursorKind::Crosshair: return s_crosshair;
        case CursorKind::Hand:      return s_hand;
        case CursorKind::ResizeH:   return s_resizeH;
        case CursorKind::ResizeV:   return s_resizeV;
        default:                    return nullptr;
    }
}

void applyCursorRequest(GLFWwindow* win) {
    ImGuiIO& io = ImGui::GetIO();
    if (s_request != CursorKind::Default) {
        io.ConfigFlags |= ImGuiConfigFlags_NoMouseCursorChange;
        GLFWcursor* c = forKind(s_request);
        if (win) glfwSetCursor(win, c);
        s_imguiCursorSuspended = true;
    } else if (s_imguiCursorSuspended) {
        io.ConfigFlags &= ~ImGuiConfigFlags_NoMouseCursorChange;
        if (win) glfwSetCursor(win, nullptr);
        s_imguiCursorSuspended = false;
    }
    s_request = CursorKind::Default;
}

}  // namespace ui
