// Composed "Activity" view (AI workspace utility pane): the live Active Work grid on top, the
// session Documents panel below, split by one hairline boundary. Bodies live in their own TUs;
// this file only sequences them under shared section chrome.
//
// The split is explicit (a bounded top region + a fill-remaining bottom region) because BOTH
// bodies size themselves off GetContentRegionAvail(): stacked naively, Active Work's empty
// state would eat the pane and leave the Documents children a sliver.
#include "activity_view.h"

#include <algorithm>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include "ui_theme.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"

#include "active_work.h"
#include "docs_panel.h"

namespace ui {

void drawActivityView(AppState& st) {
    const float S = ui::uiScale;

    // Same adaptive surface the host pane resolves — idempotent per frame; gives the boundary
    // hairline and the pill scrollbars tones matched to the glass they sit on.
    const ImVec2 paneMin = ImGui::GetWindowPos();
    const ImRect paneRect(paneMin, ImVec2(paneMin.x + ImGui::GetWindowSize().x,
                                          paneMin.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_ws_view_glass"), paneRect, GlassGroup::Global);

    const float availH = ImGui::GetContentRegionAvail().y;
    const float topH   = std::clamp(availH * 0.44f, 170.0f * S, 380.0f * S);

    // ---- upper section: Active Work ---------------------------------------------------
    ui::kit::sectionHeader("Active Work", nullptr, nullptr, -1, /*first=*/true);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##activity_work", ImVec2(0.0f, topH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleColor();
    drawActiveWorkView(st);
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();

    // ---- boundary: one hairline, no second title (the Documents panel labels itself) ----
    ui::kit::gapSection();
    ui::kit::rule();
    ui::kit::gapRow();

    // ---- lower section: Documents ------------------------------------------------------
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##activity_docs", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleColor();
    drawDocsPanel(st);
    ImGui::EndChild();
}

}  // namespace ui
