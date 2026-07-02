// Universal artifact card (T-F16) — one Copilot-produced deliverable, rendered as a glass card
// with a thumbnail + Preview/Open/Regenerate actions. Used by ui/preview_pane.cpp; kept
// standalone (no agent_panel.h dependency — that file is a hot-spot, see CLAUDE brief) so other
// panels can reuse it later.
#pragma once

#include "artifact_view.h"

#include <functional>
#include <string>

namespace ui {

struct ArtifactCardActions {
    std::function<void()> onPreview;
    std::function<void()> onOpen;
    std::function<void()> onRegenerate;
};

// Renders one card at the current ImGui cursor (caller controls layout / scrolling region).
// thumbAbsPath: absolute on-disk PNG path for a thumbnail, or empty to skip the image.
void drawArtifactCard(const aicad::ui::ArtifactCard& card, const std::string& thumbAbsPath,
                       const ArtifactCardActions& acts);

}  // namespace ui
