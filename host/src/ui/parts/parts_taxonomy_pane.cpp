// Parts Library — left pane: recursive taxonomy tree (GUI V2 phase B).
// Tree mode (parts_taxonomy): depth-0 rows are bigger/bolder (fonts.medium, 30dp), children
// 24dp with a 14dp geometric indent per level; drawlist chevron rotates on expand; click =
// expand + select, re-click on the selected parent = collapse. Expanded set persists via
// settings.partsExpandedCats. Falls back to the flat parts_list_categories 2-level list when
// the worker doesn't speak parts_taxonomy.
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

const TaxNode* findTaxNode(const TaxNode& root, const std::string& id) {
    if (root.id == id) return &root;
    for (const auto& c : root.children)
        if (const TaxNode* n = findTaxNode(c, id)) return n;
    return nullptr;
}

namespace {

void persistExpanded(AppState& st) {
    st.settings.partsExpandedCats.assign(S().expandedCats.begin(), S().expandedCats.end());
    saveSettings(st.settings);
}

void pickNode(AppState& st, const std::string& id) {
    S().selectedCategory = id;
    doSearch(st);  // browse-on-click, even with an empty query
}

// Right-aligned expand chevron over the row rect (drawlist triangle; rotates when expanded).
void rowChevron(ImVec2 mn, ImVec2 mx, bool expanded) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 c(mx.x - 12.0f * sc, (mn.y + mx.y) * 0.5f);
    const float r = 3.5f * sc;
    ImU32 col = Palette::toU32(palette.textDisabled);
    if (expanded)  // pointing down
        dl->AddTriangleFilled(ImVec2(c.x - r, c.y - r * 0.6f), ImVec2(c.x + r, c.y - r * 0.6f),
                              ImVec2(c.x, c.y + r), col);
    else           // pointing right
        dl->AddTriangleFilled(ImVec2(c.x - r * 0.6f, c.y - r), ImVec2(c.x + r, c.y),
                              ImVec2(c.x - r * 0.6f, c.y + r), col);
}

void drawNode(AppState& st, const TaxNode& node, int depth) {
    const float sc   = ui::uiScale;
    const float rowH = (depth == 0 ? 30.0f : 24.0f) * sc;
    const bool  sel  = (S().selectedCategory == node.id);
    const bool  kids = !node.children.empty();
    const bool  open = kids && S().expandedCats.count(node.id) > 0;

    ImGui::PushID(node.id.c_str());
    if (depth == 0 && S().fonts && S().fonts->medium) ImGui::PushFont(S().fonts->medium);
    const char* icon = !node.icon.empty() ? node.icon.c_str() : partIcon(node.id);
    bool clicked = borderRow("##node", node.label.c_str(), icon, sel,
                             ImVec2(0, rowH), /*centerText=*/false);
    if (depth == 0 && S().fonts && S().fonts->medium) ImGui::PopFont();
    if (kids) rowChevron(ImGui::GetItemRectMin(), ImGui::GetItemRectMax(), open);
    ImGui::PopID();

    if (clicked) {
        if (kids) {
            // Click = expand + select; re-click on the selected, open parent = collapse.
            if (sel && open) S().expandedCats.erase(node.id);
            else             S().expandedCats.insert(node.id);
            persistExpanded(st);
        }
        pickNode(st, node.id);
    }

    if (open) {
        ImGui::Indent(14.0f * sc);
        for (const auto& c : node.children) drawNode(st, c, depth + 1);
        ImGui::Unindent(14.0f * sc);
    }
}

}  // namespace

void drawTaxonomyPane(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 2 * sc));
    ImGui::BeginChild("##parts_cats", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Categories", /*firstInPane=*/true);

    if (S().taxonomyLoading || (S().categoriesLoading && !S().taxonomyIsTree)) {
        spinner("Loading");
    } else if (S().taxonomyIsTree) {
        // ---- recursive taxonomy tree ------------------------------------------------
        {
            bool sel = S().selectedCategory.empty();
            if (S().fonts && S().fonts->medium) ImGui::PushFont(S().fonts->medium);
            if (borderRow("##cat_all", "All Parts", "parts_all", sel,
                          ImVec2(0, 30.0f * sc), /*centerText=*/false))
                pickNode(st, "");
            if (S().fonts && S().fonts->medium) ImGui::PopFont();
        }
        ImGui::Spacing();
        for (const auto& n : S().taxonomyRoot.children) drawNode(st, n, 0);
    } else if (S().categories.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("No catalog data. Start the parts service or check the network.");
        ImGui::PopStyleColor();
    } else {
        // ---- fallback: flat parts_list_categories 2-level list -----------------------
        const float rowH = ImGui::GetFrameHeight() * 1.15f;
        {
            bool sel = S().selectedCategory.empty();
            if (borderRow("##cat_all", "All Parts", "parts_all", sel,
                          ImVec2(0, rowH), /*centerText=*/false))
                pickNode(st, "");
        }
        ImGui::Spacing();
        for (auto& c : S().categories) {
            if (!c.parentId.empty()) continue;  // top-level only in outer loop
            bool sel = (S().selectedCategory == c.id);
            ImGui::PushID(c.id.c_str());
            if (borderRow("##cat", c.label.c_str(), partIcon(c.id), sel,
                          ImVec2(0, rowH), /*centerText=*/false))
                pickNode(st, sel ? "" : c.id);
            ImGui::PopID();

            for (auto& ch : S().categories) {
                if (ch.parentId != c.id) continue;
                bool csel = (S().selectedCategory == ch.id);
                ImGui::Indent(14.0f * sc);
                ImGui::PushID(ch.id.c_str());
                if (borderRow("##cat", ch.label.c_str(), partIcon(ch.id),
                              csel, ImVec2(0, rowH), /*centerText=*/false))
                    pickNode(st, csel ? "" : ch.id);
                ImGui::PopID();
                ImGui::Unindent(14.0f * sc);
            }
        }
    }

    ImGui::EndChild();
}

}  // namespace parts
}  // namespace ui
