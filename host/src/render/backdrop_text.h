#pragma once

struct ImDrawData;

namespace ui {

// Call after ImGui::Render(), immediately before rendering this draw data.
// Wraps Copilot glyph runs after their preceding fills have been composited.
void adaptCopilotTextToBackdrop(ImDrawData* data);

// Call while the rendering GL context is still current.
void shutdownBackdropText();

}  // namespace ui
