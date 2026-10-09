// Parts Library — left pane: recursive taxonomy tree (GUI V3: colour-coded liquid glass).
// Every row is a ui::parts::glassRow painted in its category's own hue (categoryTint): a
// resting glassCardWash gradient + a 3dp left rail, so the pane reads as coloured bands and
// selection is a hue glow rather than a teal border. Depth reads as WEIGHT, not just indent —
// depth-0 rows are 32dp on fonts.medium at full emphasis, children 26dp at half emphasis with
// a 14dp geometric step and a hairline guide tinted in the branch's colour.
// Long root labels are ellipsized by fitLabel() so they stop before the chevron gutter instead
// of running underneath the chevron (the "Adhesives & Consumables" clip).
// Click = expand + select, re-click on the selected parent = collapse. Expanded set persists
// via settings.partsExpandedCats. Falls back to the flat parts_list_categories 2-level list
// when the worker doesn't speak parts_taxonomy.
#include <cstdio>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui/icons.h"     // hasIconSdf / getIcon — mirror glassRow's icon test for label budget
#include "ui/metrics.h"   // ui::metrics::partsPadX — the ONE horizontal gutter of this workspace
#include "ui_theme.h"
#include "ui/widgets/kit.h"   // ui::kit::blockedState — the categories-unavailable state

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
// Horizontal lane reserved at the row's right edge for the expand chevron (dp). The chevron is
// drawn ON the row surface, so without a reserved lane the row's own label runs under it —
// every row that gets a chevron passes its label through fitLabel() with this subtracted.
constexpr float kChevronGutterDp = 20.0f;
// Row heights (dp): a root is deliberately much heavier than a child.
constexpr float kRootRowDp  = 32.0f;
constexpr float kChildRowDp = 26.0f;
// Child rows carry half the wash of a root so depth reads as weight, not just indent.
constexpr float kChildEmphasis = 0.50f;

// Longest prefix of `label` that fits in `budgetPx` (measured with the CURRENT font stack,
// so call this inside any PushFont scope), plus a trailing ellipsis. Returns the label
// unchanged when it already fits. Binary chop on the byte index, then snapped back to a UTF-8
// lead byte so a multi-byte codepoint is never cut in half.
std::string fitLabel(const std::string& label, float budgetPx) {
    if (label.empty() || budgetPx <= 0.0f) return label;
    const char* s = label.c_str();
    const int   n = (int)label.size();
    if (ImGui::CalcTextSize(s, s + n).x <= budgetPx) return label;

    static const char* kEllipsis = "\xE2\x80\xA6";  // U+2026
    const float ellW = ImGui::CalcTextSize(kEllipsis).x;
    const float body = budgetPx - ellW;
    if (body <= 0.0f) return std::string(kEllipsis);

    int lo = 0, hi = n;                 // invariant: the first `lo` bytes fit in `body`
    while (lo < hi) {
        const int mid = (lo + hi + 1) / 2;
        if (ImGui::CalcTextSize(s, s + mid).x <= body) lo = mid;
        else                                           hi = mid - 1;
    }
    while (lo > 0 && ((unsigned char)s[lo] & 0xC0) == 0x80) --lo;   // don't split a codepoint
    while (lo > 0 && s[lo - 1] == ' ')                      --lo;   // no "Adhesives & …"
    return label.substr(0, (size_t)lo) + kEllipsis;
}

// Text budget of a glassRow of width `rowW`: glassRow starts the label at partsPadX (plus the
// 18dp icon + 6dp gap when the icon actually resolves) — mirror that arithmetic exactly, then
// take the chevron lane off the right.
float labelBudget(float rowW, const char* icon) {
    const float sc = ui::uiScale;
    const bool haveIcon = icon && icon[0] && (ui::hasIconSdf(icon) || ui::getIcon(icon).valid);
    const float textX = ui::metrics::partsPadX() + (haveIcon ? (18.0f + 6.0f) * sc : 0.0f);
    return rowW - textX - kChevronGutterDp * sc;
}

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

// `first` = this node is the first child under its parent, so only one ItemSpacing sits above
// it; every later sibling is preceded by the rhythm Dummy too. The depth guide has to span
// exactly that gap or the per-level segments stop joining into a rule.
void drawNode(AppState& st, const TaxNode& node, int depth, bool first) {
    const float sc   = ui::uiScale;
    const bool  root = (depth == 0);
    const float rowH = (root ? kRootRowDp : kChildRowDp) * sc;
    const bool  sel  = (S().selectedCategory == node.id);
    const bool  kids = !node.children.empty();
    const bool  open = kids && S().expandedCats.count(node.id) > 0;
    const float* tint = categoryTint(node.id);
    // Right-hand gutter reserved for the pane's kChatScrollbar pill. Reserved UNCONDITIONALLY
    // (not only when the tree overflows) so a row never changes width the moment the list
    // grows past one screen. Indent() moves DC.CursorPos right and GetContentRegionAvail() is
    // ContentRegionRect.Max.x - CursorPos.x, so subtracting the lane keeps every row's RIGHT
    // edge on the same line at any depth.
    const float laneW = ui::metrics::px(ui::kChatScrollbarLane);
    const float rowW  = ImGui::GetContentRegionAvail().x - laneW;

    ImGui::PushID(node.id.c_str());
    if (root && S().fonts && S().fonts->medium) ImGui::PushFont(S().fonts->medium);
    const char* icon = !node.icon.empty() ? node.icon.c_str() : partIcon(node.id);
    // Only a row that actually gets a chevron needs the gutter reserved; a leaf keeps the
    // full width (and glassRow's own padX clip) so nothing is ellipsized for free.
    std::string  fitted;
    const char*  shown = node.label.c_str();
    if (kids) {
        fitted = fitLabel(node.label, labelBudget(rowW, icon));
        shown  = fitted.c_str();
    }
    bool clicked = glassRow("##node", shown, icon, sel, ImVec2(rowW, rowH), tint,
                            root ? 1.0f : kChildEmphasis, root ? 7.0f : 6.0f);
    if (root && S().fonts && S().fonts->medium) ImGui::PopFont();
    const ImVec2 rowMin = ImGui::GetItemRectMin();
    const ImVec2 rowMax = ImGui::GetItemRectMax();
    if (kids) rowChevron(rowMin, rowMax, open);
    // Depth guide: every level is a bare 14dp geometric indent, so a deep branch read as a
    // ragged left edge with no hint of what owns what. One hairline per level, half a step
    // left of the row and stretched over the inter-row gap so the segments join into a rule.
    // Tinted in the branch's OWN category hue (a neutral border hairline was the one grey
    // left in a coloured tree) at a low alpha so it guides without competing with the rails.
    if (depth > 0) {
        const float gx = rowMin.x - kIndentDp * sc * 0.5f;
        const float gapUp = ImGui::GetStyle().ItemSpacing.y * (first ? 1.0f : 2.0f)
                          + (first ? 0.0f : 1.0f * sc);
        float guide[4] = {tint[0], tint[1], tint[2], 0.35f};
        ImGui::GetWindowDrawList()->AddLine(ImVec2(gx, rowMin.y - gapUp),
                                            ImVec2(gx, rowMax.y),
                                            Palette::toU32(guide), 1.0f * sc);
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
        bool firstChild = true;
        for (const auto& c : node.children) {
            drawNode(st, c, depth + 1, firstChild);
            firstChild = false;
        }
        ImGui::Unindent(kIndentDp * sc);
    }

    // Rhythm: roots breathe (a coloured band per top-level category), children stay tight so
    // an open branch reads as ONE block rather than a second list.
    ImGui::Dummy(ImVec2(0, (root ? 3.0f : 1.0f) * sc));
}

}  // namespace

void drawTaxonomyPane(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    // One gutter token for the whole workspace: the results column and the filters pane use
    // partsPadX too, so the three panes' content lines agree instead of drifting by a pixel.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::partsPadX(), 6.0f * sc));
    // NoScrollbar: ImGui's default bar is replaced by the app-wide pill-on-track scrollbar
    // (ui::drawWindowPillScrollbarV) drawn just before EndChild, like every other list pane.
    ImGui::BeginChild("##parts_cats", ImVec2(paneW, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();

    // Right-hand gutter for the thick chat-style pill drawn just before EndChild. Every
    // full-width row below subtracts it so no pill is ever painted under the scrollbar.
    const float laneW = ui::metrics::px(ui::kChatScrollbarLane);

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
    ImGui::Dummy(ImVec2(0, 2.0f * sc));   // first row is not welded to the header rule

    // "All Parts" is not a taxonomy node, so it has no category hue — it wears the app accent,
    // which is exactly the "everything" colour the rest of the workspace already uses.
    float allTint[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};

    // Loading covers every state in which a reply is still owed: the call in flight, the flat
    // fallback in flight, a backoff retry pending, and the one frame between a Retry click and
    // the loader re-issuing (taxonomyLoaded cleared, nothing sent yet) — so the error state
    // below only ever shows once the loader has actually given up.
    const bool retrying = S().taxonomyAttempts > 0 && S().taxonomyRetryAt > 0.0;
    const bool awaitingReply = S().taxonomyLoading || !S().taxonomyLoaded ||
                               (S().categoriesLoading && !S().taxonomyIsTree) || retrying;
    if (awaitingReply && !S().taxonomyIsTree && S().categories.empty()) {
        // A retry is pending: the parts worker is most likely still booting. Say so rather
        // than flashing an error the user is expected to act on.
        spinner(S().taxonomyAttempts > 0 ? "Waiting for the parts service"
                                         : "Loading categories");
    } else if (S().taxonomyIsTree) {
        // ---- recursive taxonomy tree ------------------------------------------------
        {
            bool sel = S().selectedCategory.empty();
            if (S().fonts && S().fonts->medium) ImGui::PushFont(S().fonts->medium);
            if (glassRow("##cat_all", "All Parts", "parts_all", sel,
                         ImVec2(ImGui::GetContentRegionAvail().x - laneW, kRootRowDp * sc),
                         allTint, /*emphasis=*/1.0f))
                pickNode(st, "");
            if (S().fonts && S().fonts->medium) ImGui::PopFont();
        }
        ImGui::Spacing();
        bool firstRoot = true;
        for (const auto& n : S().taxonomyRoot.children) {
            drawNode(st, n, 0, firstRoot);
            firstRoot = false;
        }
    } else if (S().categories.empty()) {
        // Every load attempt failed and the backoff gave up (kTaxonomyMaxAttempts, ~75 s).
        // Say what failed and offer the one action that helps; Retry clears the attempt
        // counter, which re-arms both the one-shot guard and the backoff schedule.
        const std::string why = S().taxonomyError.empty()
            ? std::string("The parts service did not respond.") : S().taxonomyError;
        if (ui::kit::blockedState("Categories unavailable", why.c_str(),
                                  "Automatic retries have stopped. Retry once the parts "
                                  "service is running.",
                                  "Retry")) {
            S().taxonomyLoaded    = false;
            S().taxonomyIsTree    = false;
            S().categoriesLoaded  = false;
            S().taxonomyAttempts  = 0;
            S().taxonomyRetryAt   = 0.0;
            S().taxonomyError.clear();
        }
    } else {
        // ---- fallback: flat parts_list_categories 2-level list -----------------------
        // Same colour language as the tree; the flat list has no chevrons, so no row here
        // needs the chevron gutter reserved.
        {
            bool sel = S().selectedCategory.empty();
            if (glassRow("##cat_all", "All Parts", "parts_all", sel,
                         ImVec2(ImGui::GetContentRegionAvail().x - laneW, kRootRowDp * sc),
                         allTint, /*emphasis=*/1.0f))
                pickNode(st, "");
        }
        ImGui::Spacing();
        for (auto& c : S().categories) {
            if (!c.parentId.empty()) continue;  // top-level only in outer loop
            bool sel = (S().selectedCategory == c.id);
            ImGui::PushID(c.id.c_str());
            if (glassRow("##cat", c.label.c_str(), partIcon(c.id), sel,
                         ImVec2(ImGui::GetContentRegionAvail().x - laneW, kRootRowDp * sc),
                         categoryTint(c.id), /*emphasis=*/1.0f))
                pickNode(st, sel ? "" : c.id);
            ImGui::PopID();

            for (auto& ch : S().categories) {
                if (ch.parentId != c.id) continue;
                bool csel = (S().selectedCategory == ch.id);
                ImGui::Indent(kIndentDp * sc);
                ImGui::PushID(ch.id.c_str());
                if (glassRow("##cat", ch.label.c_str(), partIcon(ch.id), csel,
                             ImVec2(ImGui::GetContentRegionAvail().x - laneW,
                                    kChildRowDp * sc),
                             categoryTint(ch.id), kChildEmphasis, 6.0f))
                    pickNode(st, csel ? "" : ch.id);
                ImGui::PopID();
                ImGui::Unindent(kIndentDp * sc);
                ImGui::Dummy(ImVec2(0, 1.0f * sc));
            }
            ImGui::Dummy(ImVec2(0, 3.0f * sc));
        }
    }

    // Bottom breathing room: without it the last root category sits flush against the pane
    // edge and reads as a clipped list rather than the end of one.
    ImGui::Dummy(ImVec2(0, 8.0f * sc));

    // The copilot chat's thick pill, flush on the pane's right edge (no-op unless the tree
    // overflows). Rows above reserve kChatScrollbarLane so none is drawn under it.
    const ImVec2 cwp = ImGui::GetWindowPos();
    const ImVec2 cws = ImGui::GetWindowSize();
    const ui::AdaptiveSurface& surf = ui::adaptivePanel(
        ui::panelId("parts_cats_glass"),
        ImRect(cwp, ImVec2(cwp.x + cws.x, cwp.y + cws.y)));
    ui::drawWindowPillScrollbarV(surf, ui::kChatScrollbar);

    ImGui::EndChild();
}

}  // namespace parts
}  // namespace ui
