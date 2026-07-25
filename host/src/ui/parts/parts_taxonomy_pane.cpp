// Parts Library — left pane: recursive taxonomy tree (GUI V2 phase B).
// Tree mode (parts_taxonomy): depth-0 rows are bigger/bolder (fonts.medium, 30dp), children
// 24dp with a 14dp geometric indent per level; drawlist chevron rotates on expand; click =
// expand + select, re-click on the selected parent = collapse. Expanded set persists via
// settings.partsExpandedCats. Falls back to the flat parts_list_categories 2-level list when
// the worker doesn't speak parts_taxonomy.
#include <cstdio>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui/metrics.h"   // ui::metrics::partsPadX — the ONE horizontal gutter of this workspace
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
    // Facet keys are scoped to the node that declared them, and only the selected node's
    // facet widgets are drawn — so a value left over from the previous category is invisible
    // yet still live, silently filtering the new category down to zero rows. Re-selecting the
    // same node keeps its values.
    if (id != S().selectedCategory) S().facetValues.clear();
    S().selectedCategory = id;
    doSearch(st);  // browse-on-click, even with an empty query
}

// One geometric indent step per taxonomy level (dp; scale at the callsite).
constexpr float kIndentDp = 14.0f;

// Right-aligned expand chevron over the row rect (drawlist triangle; rotates when expanded).
// Expanded is tinted accent so an open branch is readable from the chevron alone — a uniformly
// disabled-grey chevron made the pane's open/closed state depend entirely on the rotation.
void rowChevron(ImVec2 mn, ImVec2 mx, bool expanded) {
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 c(mx.x - 12.0f * sc, (mn.y + mx.y) * 0.5f);
    const float r = 3.5f * sc;
    ImU32 col = Palette::toU32(expanded ? palette.accent : palette.textDim);
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
    const ImVec2 rowMin = ImGui::GetItemRectMin();
    const ImVec2 rowMax = ImGui::GetItemRectMax();
    if (kids) rowChevron(rowMin, rowMax, open);
    // Depth guide: every level is a bare 14dp geometric indent, so a deep branch read as a
    // ragged left edge with no hint of what owns what. One hairline per level, half a step
    // left of the row and stretched over the inter-row gap so the segments join into a rule.
    if (depth > 0) {
        const float gx = rowMin.x - kIndentDp * sc * 0.5f;
        ImGui::GetWindowDrawList()->AddLine(
            ImVec2(gx, rowMin.y - ImGui::GetStyle().ItemSpacing.y), ImVec2(gx, rowMax.y),
            Palette::toU32(palette.border), 1.0f * sc);
    }
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
        ImGui::Indent(kIndentDp * sc);
        for (const auto& c : node.children) drawNode(st, c, depth + 1);
        ImGui::Unindent(kIndentDp * sc);
    }
}

}  // namespace

void drawTaxonomyPane(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    // One gutter token for the whole workspace: the results column and the filters pane use
    // partsPadX too, so the three panes' content lines agree instead of drifting by a pixel.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::partsPadX(), 2.0f * sc));
    ImGui::BeginChild("##parts_cats", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Count rides in the header's trailing slot (never baked into the label) — nullptr while
    // the tree is still loading so the header does not flash a "0".
    char catCount[16];
    const char* catTrailing = nullptr;
    int rootCount = 0;
    if (S().taxonomyIsTree) {
        rootCount = (int)S().taxonomyRoot.children.size();
    } else {
        for (const auto& c : S().categories)
            if (c.parentId.empty()) ++rootCount;
    }
    if (rootCount > 0) {
        std::snprintf(catCount, sizeof(catCount), "%d", rootCount);
        catTrailing = catCount;
    }
    sectionHeader("Categories", /*firstInPane=*/true, "parts_all", catTrailing);

    const bool retrying = S().taxonomyAttempts > 0 && S().taxonomyRetryAt > 0.0;
    if (S().taxonomyLoading || (S().categoriesLoading && !S().taxonomyIsTree) || retrying) {
        // A retry is pending: the parts worker is most likely still booting. Say so rather
        // than flashing an error the user is expected to act on.
        spinner(S().taxonomyAttempts > 0 ? "Waiting for the parts service" : "Loading");
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
        ImGui::TextWrapped("No catalog data. The parts service did not respond.");
        ImGui::PopStyleColor();
        ImGui::Spacing();
        // Auto-retry gave up (kTaxonomyMaxAttempts exhausted). Clearing the attempt counter
        // re-arms both the one-shot guard and the backoff schedule.
        if (ImGui::SmallButton("Retry##cat_retry")) {
            S().taxonomyLoaded    = false;
            S().taxonomyIsTree    = false;
            S().categoriesLoaded  = false;
            S().taxonomyAttempts  = 0;
            S().taxonomyRetryAt   = 0.0;
        }
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

    // Bottom breathing room: without it the last root category sits flush against the pane
    // edge and reads as a clipped list rather than the end of one.
    ImGui::Dummy(ImVec2(0, 8.0f * sc));

    ImGui::EndChild();
}

}  // namespace parts
}  // namespace ui
