// Shared drag-and-drop payload identifiers (Wave 3A item 5: drag-and-drop to open files).
//
// Two paths feed the same open mechanism (AppState::requestOpenPath, drained by app.cpp's GL-thread
// request pump):
//   1. OS file drop  — glfwSetDropCallback in app.cpp (Windows Explorer -> our window).
//   2. In-app drag   — the File Explorer panel (file_explorer.cpp) is an ImGui drag SOURCE that
//                      packs an absolute path under kFileDragPayload; the viewport (viewport_panel.cpp)
//                      is the matching drag TARGET.
//
// kFileDragPayload is the ImGui payload-type string both sides agree on. ImGui requires it to be at
// most 32 chars (incl. the null terminator); "AICAD_FILE" is 10.
#pragma once

namespace ui {

// ImGui drag-drop payload type for an in-app file drag (Explorer row -> viewport). The payload data
// is the dragged file's absolute path as a NUL-terminated char buffer (length includes the NUL).
inline constexpr const char* kFileDragPayload = "AICAD_FILE";

}  // namespace ui
