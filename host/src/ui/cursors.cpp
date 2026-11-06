// Cursor manager (W1-B). See cursors.h.
#include "cursors.h"
#include "anim.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"

#include "../core/app_state.h"

namespace ui {

// GLFW standard cursors, created once. File-static: one viewport, created at startup.
static GLFWcursor* s_crosshair = nullptr;
static GLFWcursor* s_hand      = nullptr;
static GLFWcursor* s_resizeH   = nullptr;
static GLFWcursor* s_resizeV   = nullptr;
static GLFWwindow* s_overrideWindow = nullptr;
static CursorKind s_overrideKind = CursorKind::Default;

void initCursors(GLFWwindow* win) {
    destroyCursors();
    if (!win) return;
    s_crosshair = glfwCreateStandardCursor(GLFW_CROSSHAIR_CURSOR);
    s_hand      = glfwCreateStandardCursor(GLFW_HAND_CURSOR);
    s_resizeH   = glfwCreateStandardCursor(GLFW_HRESIZE_CURSOR);
    s_resizeV   = glfwCreateStandardCursor(GLFW_VRESIZE_CURSOR);
    anim::setLoopWakeEnabled(true);
}

void destroyCursors() {
    anim::setLoopWakeEnabled(false);
    s_overrideWindow = nullptr;
    s_overrideKind = CursorKind::Default;
    if (s_crosshair) { glfwDestroyCursor(s_crosshair); s_crosshair = nullptr; }
    if (s_hand)      { glfwDestroyCursor(s_hand);      s_hand = nullptr; }
    if (s_resizeH)   { glfwDestroyCursor(s_resizeH);   s_resizeH = nullptr; }
    if (s_resizeV)   { glfwDestroyCursor(s_resizeV);   s_resizeV = nullptr; }
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

void applyCursorRequest(AppState& st, GLFWwindow* win) {
    ImGuiIO& io = ImGui::GetIO();
    if (st.cursorRequest != CursorKind::Default) {
        // Take over: stop ImGui from setting the cursor this frame, set our GLFW cursor directly.
        io.ConfigFlags |= ImGuiConfigFlags_NoMouseCursorChange;
        GLFWcursor* c = forKind(st.cursorRequest);
        if (win && (s_overrideWindow != win || s_overrideKind != st.cursorRequest)) {
            glfwSetCursor(win, c);  // c may be null -> arrow, which is fine
            s_overrideWindow = win;
            s_overrideKind = st.cursorRequest;
        }
        st.imguiCursorSuspended = true;
    } else if (st.imguiCursorSuspended) {
        // No request this frame: hand cursor management back to ImGui and clear our override.
        io.ConfigFlags &= ~ImGuiConfigFlags_NoMouseCursorChange;
        if (win) glfwSetCursor(win, nullptr);
        st.imguiCursorSuspended = false;
        s_overrideWindow = nullptr;
        s_overrideKind = CursorKind::Default;
    }
    // Reset for next frame; consumers re-request every frame they want a non-default cursor.
    st.cursorRequest = CursorKind::Default;
}

}  // namespace ui
