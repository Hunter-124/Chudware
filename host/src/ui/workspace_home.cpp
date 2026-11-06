// Workspace home (see workspace_home.h).
#include "workspace_home.h"

#include <algorithm>
#include <string>

#include "imgui_internal.h"   // ImHashStr, ImRect

#include "anim.h"
#include "doc_tabs.h"         // createNewDoc
#include "feature_tree.h"     // featureTreeOpenProgress
#include "property_manager.h"
#include "sketch3d_mode.h"
#include "sketch_mode.h"
#include "tesseract.h"
#include "widgets/kit.h"

#include "../core/app_state.h"
#include "../ui_theme.h"

namespace ui {

bool workspaceHomeActive(const AppState& st) {
    if (st.activeWorkspace != AppState::Workspace::Model) return false;
    if (st.propertyManager && st.propertyManager->active()) return false;
    if (st.sketchMode && st.sketchMode->active()) return false;
    if (st.sketch3dMode && st.sketch3dMode->active()) return false;
    if (st.inComponentEdit()) return false;
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return true;
    const AppState::DocTab& tab = st.docTabs[st.activeTab];
    if (tab.kind != AppState::DocTab::TabKind::Part || !tab.isStarter || tab.dirty ||
        !tab.path.empty())
        return false;
    // An agent may build straight into the starter: its first feature makes it a real document.
    if (st.docMirror.hasDoc() && st.docMirror.docId() == tab.docId) {
        const nlohmann::json& tree = st.docMirror.tree();
        const auto features = tree.is_object() ? tree.find("features") : tree.end();
        if (tree.is_object() && features != tree.end() && features->is_array() && !features->empty())
            return false;
    }
    return true;
}

float workspaceHomeBlend(const AppState& st) {
    static int s_frame = -1;
    static float s_value = 0.0f;
    const int frame = ImGui::GetFrameCount();
    if (frame == s_frame) return s_value;
    s_frame = frame;
    const float target = workspaceHomeActive(st) ? 1.0f : 0.0f;
    s_value = st.shot ? target
                      : anim::easeInOut(anim::toward(ImHashStr("##workspace_home"), target,
                                                     anim::kSlow));
    return s_value;
}

void drawWorkspaceHomeOverlay(AppState& st, ImVec2 vr0, ImVec2 vr1, ImVec2 cr0, ImVec2 cr1) {
    const float blend = workspaceHomeBlend(st);
    if (blend <= 0.01f) return;
    const float S = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // The bare starter scene (datum axes, triad) is noise here: frost it away.
    const ui::AdaptiveSurface& scrim =
        ui::adaptivePanel(ui::panelId("vp_workspace_home"), ImRect(vr0, vr1), ui::GlassGroup::Hud);
    ui::glassPanel(dl, ImRect(vr0, vr1), ui::GlassLevel::Regular, 0.0f, scrim, blend);

    const float freeW = cr1.x - cr0.x;
    if (freeW < 160.0f * S) return;
    const float cardW = std::min(440.0f * S, freeW - 32.0f * S);
    const bool agentBusy = st.agentV2Running;
    const std::string opening = featureTreeOpenProgress();

    // Card height is measured from last frame's content; the first frame centres on a guess.
    static float s_cardH = 260.0f;
    const ImVec2 ctr((cr0.x + cr1.x) * 0.5f, (cr0.y + cr1.y) * 0.5f);
    // Lift as it fades in: a short rise reads as the card arriving, not popping.
    const float rise = (1.0f - blend) * 14.0f * S;
    const ImVec2 c0(ctr.x - cardW * 0.5f, ctr.y - s_cardH * 0.5f + rise);

    ImGui::SetCursorScreenPos(c0);
    ImGui::PushStyleVar(ImGuiStyleVar_Alpha, ImGui::GetStyle().Alpha * blend);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(24.0f * S, 22.0f * S));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    // A child window, so the card's buttons win hover over the viewport's full-bleed input item.
    ImGui::BeginChild("##workspace_home_card", ImVec2(cardW, s_cardH),
                      ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                          ImGuiWindowFlags_NoSavedSettings);
    ImDrawList* cdl = ImGui::GetWindowDrawList();
    const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    const ImRect cardRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("vp_workspace_home_card"), cardRect, ui::GlassGroup::Hud);
    ui::glassPanel(cdl, cardRect, ui::GlassLevel::Thick, 16.0f, surf, blend);
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, surf.textPrimary.w * blend));
    const ImU32 inkDim = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textSecondary.x, surf.textSecondary.y, surf.textSecondary.z,
               surf.textSecondary.w * blend));
    const float* acc = ui::palette.accent;
    const ImU32 accent = ImGui::ColorConvertFloat4ToU32(ImVec4(acc[0], acc[1], acc[2], blend));

    const float innerW = ImGui::GetContentRegionAvail().x;
    const float startY = ImGui::GetCursorPosY();
    auto centredText = [&](const std::string& text, ImU32 col, ImFont* font = nullptr) {
        if (font) ImGui::PushFont(font);
        const std::string shown = kit::elide(text.c_str(), innerW);
        const ImVec2 sz = ImGui::CalcTextSize(shown.c_str());
        const ImVec2 p = ImGui::GetCursorScreenPos();
        cdl->AddText(ImVec2(p.x + (innerW - sz.x) * 0.5f, p.y), col, shown.c_str());
        ImGui::Dummy(ImVec2(innerW, sz.y));
        if (shown != text && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", text.c_str());
        if (font) ImGui::PopFont();
    };

    // Brand mark: turning while anything works on the user's behalf, at rest otherwise.
    {
        const float mark = 54.0f * S;
        const ImVec2 p = ImGui::GetCursorScreenPos();
        const ImVec2 mc(p.x + innerW * 0.5f, p.y + mark * 0.5f);
        if (agentBusy || !opening.empty()) {
            drawTesseractSpinner(cdl, mc, mark, ImGui::GetTime(), accent);
            anim::requestFramesAt(30.0f);
        } else {
            drawTesseract(cdl, mc, mark, 0.0, tesseractAnimFromInt(st.settings.brandAnim), accent);
        }
        ImGui::Dummy(ImVec2(innerW, mark + 14.0f * S));
    }

    std::string title = "Nothing open yet";
    std::string body = "Open a file from the Explorer, drop one here, or start something new.";
    if (!opening.empty()) {
        title = "Opening\xE2\x80\xA6";
        body = opening;
    } else if (agentBusy) {
        title = "The agent is working";
        body = "Its results show up here when it presents them. Open any file to follow along.";
    }
    centredText(title, ink, st.fonts.medium);
    ImGui::Dummy(ImVec2(0, 4.0f * S));
    if (agentBusy && opening.empty() && !st.agentV2Status.empty()) {
        centredText(st.agentV2Status, accent);
        ImGui::Dummy(ImVec2(0, 2.0f * S));
    }
    {   // Greedy word wrap, each line centred (ImGui's wrapped text is left-aligned).
        std::string line;
        size_t at = 0;
        while (at <= body.size()) {
            size_t end = body.find(' ', at);
            if (end == std::string::npos) end = body.size();
            const std::string word = body.substr(at, end - at);
            const std::string trial = line.empty() ? word : line + " " + word;
            if (!line.empty() && ImGui::CalcTextSize(trial.c_str()).x > innerW) {
                centredText(line, inkDim);
                line = word;
            } else {
                line = trial;
            }
            at = end + 1;
        }
        if (!line.empty()) centredText(line, inkDim);
    }
    ImGui::Dummy(ImVec2(0, 14.0f * S));

    // Actions, centred as one group.
    {
        const ImGuiStyle& style = ImGui::GetStyle();
        const char* labels[3] = {"New Part", "New Assembly", "Open\xE2\x80\xA6"};
        float widths[3], total = 0.0f;
        for (int i = 0; i < 3; ++i) {
            widths[i] = ImGui::CalcTextSize(labels[i]).x + style.FramePadding.x * 2.0f + 8.0f * S;
            total += widths[i];
        }
        const float gap = 8.0f * S;
        total += gap * 2.0f;
        const bool fits = total <= innerW;
        const bool ready = st.workerStarted;
        const char* why = "The engine is still starting";
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (fits ? (innerW - total) * 0.5f : 0.0f));
        if (kit::primaryButton(labels[0], ready, why, ImVec2(fits ? widths[0] : innerW, 0)))
            createNewDoc(st, 0);
        if (fits) ImGui::SameLine(0, gap);
        if (kit::secondaryButton(labels[1], ready, why, ImVec2(fits ? widths[1] : innerW, 0)))
            createNewDoc(st, 1);
        if (fits) ImGui::SameLine(0, gap);
        if (kit::secondaryButton(labels[2], true, nullptr, ImVec2(fits ? widths[2] : innerW, 0)))
            st.requestOpenDialog = true;
    }
    const float contentH = ImGui::GetCursorPosY() - startY;
    s_cardH = contentH + ImGui::GetStyle().WindowPadding.y * 2.0f;
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);
}

}  // namespace ui
