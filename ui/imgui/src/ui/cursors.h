// Cursor manager (W1-B). Ported from host/src/ui/cursors.h.
//
// GLFW standard cursors created once at startup. Per-frame request model: a feature that wants a
// non-default cursor calls ui::requestCursor(kind) during its draw; application.cpp consumes the
// request at the end of the frame (see ui::applyCursorRequest). When a non-default cursor was
// requested this frame, ImGui's own cursor management is suspended (ImGuiConfigFlags_NoMouseCursorChange)
// and the GLFW cursor is set directly; otherwise ImGui keeps driving the cursor as usual.
#pragma once

struct GLFWwindow;

namespace ui {

enum class CursorKind { Default, Crosshair, Hand, ResizeH, ResizeV };

void initCursors(GLFWwindow* win);
void destroyCursors();

// Request a non-default cursor for THIS frame. Last request before end-of-frame wins.
void requestCursor(CursorKind k);

// Consume this frame's cursor request. Resets to Default for the next frame.
void applyCursorRequest(GLFWwindow* win);

}  // namespace ui
