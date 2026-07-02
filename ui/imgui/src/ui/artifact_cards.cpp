// One artifact card (T-F16). See artifact_cards.h.
//
// Visual pattern mirrors agent_panel.cpp's drawActionCard (glass card, label + small buttons),
// but beginGlassBubble()/endGlassBubble()/inlineIconGlass()/agentSurf()/GlassBubble are all
// file-local statics in agent_panel.cpp (a hot-spot we must not touch or extract from) — so this
// falls back to the plain-ImGui path: theme::pushGlassChildStyle/popGlassChildStyle (the public
// glass-child helper theme.h already exports) + a plain bordered child + ImGui::Text/SmallButton.
#include "artifact_cards.h"

#include "theme/texture_cache.h"
#include "theme/theme.h"

#include "imgui.h"

#include <filesystem>

namespace ui {

namespace {

std::string stemOf(const std::string& path) {
    if (path.empty()) return path;
    std::filesystem::path p(path);
    std::string stem = p.stem().string();
    return stem.empty() ? path : stem;
}

}  // namespace

void drawArtifactCard(const aicad::ui::ArtifactCard& card, const std::string& thumbAbsPath,
                       const ArtifactCardActions& acts) {
    const float S = theme::S();
    ImGui::PushID(card.artifactId.empty() ? card.path.c_str() : card.artifactId.c_str());

    theme::pushGlassChildStyle(8.f * S);
    ImGui::BeginChild("##artifactCard", ImVec2(0, 0),
                       ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY |
                       ImGuiChildFlags_AlwaysUseWindowPadding);

    ImGui::PushFont(theme::font(theme::Font::Label));
    ImGui::TextColored(TVEC(accent), "%s", card.kind.empty() ? "artifact" : card.kind.c_str());
    ImGui::PopFont();

    const std::string title = card.title.empty() ? stemOf(card.path) : card.title;
    ImGui::PushTextWrapPos(0);
    ImGui::TextUnformatted(title.c_str());
    ImGui::PopTextWrapPos();

    ImGui::TextColored(ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled), "%s", card.path.c_str());

    if (!thumbAbsPath.empty()) {
        int tw = 0, th = 0;
        ImTextureID tex = loadPreviewTexture(thumbAbsPath, &tw, &th);
        if (tex && tw > 0 && th > 0) {
            const float h = 96.f * S;
            const float w = h * (float)tw / (float)th;
            ImGui::Image(tex, ImVec2(w, h));
        }
    }

    if (ImGui::SmallButton("Preview") && acts.onPreview) acts.onPreview();
    ImGui::SameLine();
    if (ImGui::SmallButton("Open") && acts.onOpen) acts.onOpen();
    ImGui::SameLine();
    if (ImGui::SmallButton("Regenerate") && acts.onRegenerate) acts.onRegenerate();

    ImGui::EndChild();
    theme::popGlassChildStyle();
    ImGui::PopID();
}

}  // namespace ui
