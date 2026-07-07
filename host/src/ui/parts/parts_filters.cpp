// Parts Library — filter block (price / material / vendor / source + apply/reset).
// Phase A: extracted verbatim from the category pane; a later phase moves this into a
// dedicated right-hand facet pane driven by PartsState::facetValues.
#include <algorithm>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

void drawFiltersBlock(AppState& st, float paneW) {
    const float sc = ui::uiScale;

    sectionHeader("Filters");

    ImGui::TextDisabled("Price (USD)");
    ImGui::SetNextItemWidth((paneW - 40.0f * sc) * 0.5f);
    if (ImGui::InputFloat("##pmin", &S().priceMin, 0, 0, "%.2f"))
        S().priceMin = std::max(0.0f, S().priceMin);
    ImGui::SameLine();
    ImGui::TextDisabled("-");
    ImGui::SameLine();
    ImGui::SetNextItemWidth((paneW - 40.0f * sc) * 0.5f);
    if (ImGui::InputFloat("##pmax", &S().priceMax, 0, 0, "%.2f"))
        S().priceMax = std::max(0.0f, S().priceMax);

    ImGui::TextDisabled("Material");
    ImGui::SetNextItemWidth(paneW - 22.0f * sc);
    ImGui::InputTextWithHint("##fmat", "steel, brass, nylon…",
                             S().materialBuf, sizeof(S().materialBuf));

    ImGui::TextDisabled("Vendor");
    ImGui::SetNextItemWidth(paneW - 22.0f * sc);
    ImGui::InputTextWithHint("##fven", "any vendor",
                             S().vendorBuf, sizeof(S().vendorBuf));

    ImGui::TextDisabled("Source");
    ImGui::SetNextItemWidth(paneW - 22.0f * sc);
    ImGui::Combo("##fsrc", &S().sourceFilter, "All\0Local library\0Live vendors\0");

    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.75f));
    if (ImGui::Button("Apply Filters", ImVec2(paneW - 22.0f * sc, 0)))
        doSearch(st);
    ImGui::PopStyleColor();
    if (ImGui::SmallButton("Reset")) {
        S().priceMin = S().priceMax = 0.0f;
        S().materialBuf[0] = 0;
        S().vendorBuf[0]   = 0;
        S().sourceFilter   = 0;
        doSearch(st);
    }
}

}  // namespace parts
}  // namespace ui
