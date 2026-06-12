// Cursor manager (W1-B).
//
// GLFW standard cursors created once at startup. Per-frame request model: a feature that wants a
// non-default cursor calls st.requestCursor(kind) during its draw; app.cpp consumes the request at
// the end of the frame (see ui::applyCursorRequest). When a non-default cursor was requested this
// frame, ImGui's own cursor management is suspended (ImGuiConfigFlags_NoMouseCursorChange) and the
// GLFW cursor is set directly; otherwise ImGui keeps driving the cursor as usual.
//
// Wave-2 agents call st.requestCursor() from sketch tools / PM selector wells; Wave-1 wires one
// consumer (viewport pan-drag) to prove the path.
#pragma once

struct GLFWwindow;
struct AppState;

namespace ui {

enum class CursorKind { Default, Crosshair, Hand, ResizeH, ResizeV };

// Create the GLFW standard cursors once. Call after the window exists (run_gui startup). Safe to
// call without a window (no-op); safe to call twice (rebuilds).
void initCursors(GLFWwindow* win);

// Free the cursors at shutdown (optional; GLFW frees them on terminate anyway).
void destroyCursors();

// Consume this frame's cursor request: if a non-default cursor was requested, suspend ImGui cursor
// management and set the GLFW cursor; otherwise restore ImGui management. Resets the request to
// Default for the next frame. Called once at end-of-frame from app.cpp.
void applyCursorRequest(AppState& st, GLFWwindow* win);

}  // namespace ui
