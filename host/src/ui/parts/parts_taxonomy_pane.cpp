// Parts Library — left pane: category taxonomy (border-highlight rows + category icons).
// Phase A keeps the flat parent/child category list from parts_list_categories; the TaxNode
// tree in PartsState takes over in a later phase.
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

void drawTaxonomyPane(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 4 * sc));
    ImGui::BeginChild("##parts_cats", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Categories", /*firstInPane=*/true);

    const float rowH = ImGui::GetFrameHeight() * 1.15f;

    if (S().categoriesLoading) {
        spinner("Loading");
    } else if (S().categories.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("No catalog data. Start the parts service or check the network.");
        ImGui::PopStyleColor();
    } else {
        auto pick = [&](const std::string& id) {
            S().selectedCategory = id;
            doSearch(st);  // browse-on-click, even with an empty query
        };
        {
            bool sel = S().selectedCategory.empty();
            if (borderRow("##cat_all", "All Parts", "parts_all", sel,
                          ImVec2(0, rowH), /*centerText=*/false))
                pick("");
        }
        ImGui::Spacing();
        for (auto& c : S().categories) {
            if (!c.parentId.empty()) continue;  // top-level only in outer loop
            bool sel = (S().selectedCategory == c.id);
            if (borderRow(("##cat_" + c.id).c_str(), c.label.c_str(), partIcon(c.id), sel,
                          ImVec2(0, rowH), /*centerText=*/false))
                pick(sel ? "" : c.id);

            for (auto& ch : S().categories) {
                if (ch.parentId != c.id) continue;
                bool csel = (S().selectedCategory == ch.id);
                ImGui::Indent(14.0f * sc);
                if (borderRow(("##cat_" + ch.id).c_str(), ch.label.c_str(), partIcon(ch.id),
                              csel, ImVec2(0, rowH), /*centerText=*/false))
                    pick(csel ? "" : ch.id);
                ImGui::Unindent(14.0f * sc);
            }
        }
    }

    // ---- Filters (extracted to parts_filters.cpp; becomes the right pane later) ----
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    drawFiltersBlock(st, paneW);

    ImGui::EndChild();
}

}  // namespace parts
}  // namespace ui
