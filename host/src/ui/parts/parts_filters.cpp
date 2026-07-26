// Parts Library — right-hand filters pane (GUI V2 phase B).
// Global block (price / vendor / material / source) followed by per-selection facet widgets
// from the selected taxonomy node: range → paired non-negative min/max drags, enum → chips,
// text → InputTextWithHint. Every change stamps S().filterDirtyAt; the workspace loop fires
// doSearch once 0.35 s after the last change (debounce — no Apply button). The pane's fixed
// footer is the compact vendor strip (parts_vendors.cpp).
// V3: every framed widget in the pane wears the shared liquid-glass frame (glassFieldBegin /
// glassFieldEnd) in ONE hue taken from the selected category, and the global block sits on a
// hue-tinted card, so the filters read as part of the branch being browsed instead of a grey
// box bolted to its side.
#include <algorithm>
#include <cfloat>
#include <cstdio>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"    // ui::metrics::partsPadX — the ONE horizontal inset of this workspace

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

namespace {

inline void markDirty() { S().filterDirtyAt = ImGui::GetTime(); }

// fieldLabel and the facet chip used to be file-local here; both now come from
// parts_widgets.h. That is the whole point of the move: the chip is sized from its own text
// by chipWidth(), and the wrap arithmetic below measures with that SAME call. The old local
// copy carried its own padding term, the two drifted, and every chip lost its last glyph
// ("alloy steel" → "alloy stee").

// Active-filter tally. Feeds both the header badge and the Clear-all enable state, so the
// count the user reads can never disagree with the control that clears it. Counted over
// exactly the fields Clear-all resets — add a filter, add it here.
int activeFilterCount() {
    int n = 0;
    if (S().priceMin != 0.0f)  ++n;
    if (S().priceMax != 0.0f)  ++n;
    if (S().vendorBuf[0])      ++n;
    if (S().materialBuf[0])    ++n;
    if (S().sourceFilter != 0) ++n;
    for (const auto& kv : S().facetValues)
        if (kv.second.active()) ++n;
    return n;
}

// `tint` is the pane hue (see drawFiltersBlock) — every framed facet widget wears it, so a
// facet group and the global block above it read as one coloured surface.
void drawFacetWidgets(const TaxNode& node, const float* tint) {
    const float sc = ui::uiScale;
    // Same explicit rhythm as the global block above (see drawFiltersBlock): a label hugs
    // the field it names, the next facet starts a real gap away.
    const float gapLabel = 3.0f * sc;
    const float gapField = 9.0f * sc;
    for (const auto& f : node.facets) {
        FacetValue& fv = S().facetValues[f.id];
        ImGui::PushID(f.id.c_str());
        fieldLabel(f.label.c_str());
        ImGui::Dummy(ImVec2(0, gapLabel));

        if (f.type == "range") {
            char fmtMin[48], fmtMax[48];
            if (!f.unit.empty()) {
                std::snprintf(fmtMin, sizeof(fmtMin), "min %%.2f %s", f.unit.c_str());
                std::snprintf(fmtMax, sizeof(fmtMax), "max %%.2f %s", f.unit.c_str());
            } else {
                std::snprintf(fmtMin, sizeof(fmtMin), "min %%.2f");
                std::snprintf(fmtMax, sizeof(fmtMax), "max %%.2f");
            }
            // The RPC facet schema carries no per-facet bound (contracts/parts.md §7.2), so
            // the only honest floor is zero: dimensions, counts and prices are never negative.
            // 0 also means "omit this end" to the search serializer, which is why the two ends
            // are driven by separate drags instead of DragFloatRange2 — the stock widget hard-
            // ties min <= max, which pins min at an unset (0) max and kills min-only queries.
            const float gap  = ImGui::GetStyle().ItemInnerSpacing.x;
            const float half = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
            // glassFieldEnd reads the widget's REAL item rect and hover/active state, so it
            // must follow its own widget immediately — markDirty() waits until after.
            glassFieldBegin();
            ImGui::SetNextItemWidth(half);
            const bool minChanged = ImGui::DragFloat("##rmin", &fv.rmin, 0.25f, 0.0f,
                                                     fv.rmax > 0.0f ? fv.rmax : FLT_MAX, fmtMin,
                                                     ImGuiSliderFlags_AlwaysClamp);
            glassFieldEnd(tint);
            if (minChanged) markDirty();
            ImGui::SameLine(0.0f, gap);
            glassFieldBegin();
            ImGui::SetNextItemWidth(-FLT_MIN);
            const bool maxChanged = ImGui::DragFloat("##rmax", &fv.rmax, 0.25f, 0.0f, FLT_MAX,
                                                     fmtMax, ImGuiSliderFlags_AlwaysClamp);
            glassFieldEnd(tint);
            if (maxChanged) {
                // Dragging max down past min drags min with it rather than inverting the range.
                if (fv.rmax > 0.0f && fv.rmax < fv.rmin) fv.rmin = fv.rmax;
                markDirty();
            }
        } else if (f.type == "enum") {
            // Wrapping toggle chips with manual SameLine wrap. The measured width is
            // chipWidth() — the exact footprint chipToggle paints — so the wrap test and the
            // drawn pill read one number and can never disagree the way they used to.
            const float availR = ImGui::GetContentRegionAvail().x +
                                 ImGui::GetCursorPosX();  // right edge in window coords
            bool first = true;
            for (const auto& opt : f.options) {
                const float w = chipWidth(opt.c_str());
                if (!first) {
                    ImGui::SameLine(0.0f, 5.0f * sc);
                    if (ImGui::GetCursorPosX() + w > availR) {
                        ImGui::NewLine();
                        // Wrapped rows would otherwise stack at the body's tight base
                        // spacing and read as one solid slab of pills.
                        ImGui::Dummy(ImVec2(0, 2.0f * sc));
                    }
                }
                first = false;
                bool active = fv.multi.count(opt) > 0;
                ImGui::PushID(opt.c_str());
                if (chipToggle("##chip", opt.c_str(), active)) {
                    if (active) fv.multi.erase(opt);
                    else        fv.multi.insert(opt);
                    markDirty();
                }
                ImGui::PopID();
            }
        } else {  // "text" (and anything unknown degrades to a substring box)
            glassFieldBegin();
            ImGui::SetNextItemWidth(-FLT_MIN);
            const bool changed =
                ImGui::InputTextWithHint("##text", "contains…", fv.text, sizeof(fv.text));
            glassFieldEnd(tint);
            if (changed) markDirty();
        }

        ImGui::Dummy(ImVec2(0, gapField));
        ImGui::PopID();
    }
}

}  // namespace

void drawFiltersBlock(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    // One explicit vertical rhythm for the pane: a label sits gapLabel above the field it
    // names (so the pair reads as ONE control) and gapField below the previous field. Every
    // gap in the body is one of these two Dummy() heights over the tight base spacing pushed
    // below — the pane used to spend the theme's single 9 px paragraph gap on every seam,
    // which is what flattened it into a stack of same-weight dim lines.
    const float gapLabel = 3.0f * sc;
    const float gapField = 9.0f * sc;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::partsPadX(), 2.0f * sc));  // pane gutter
    ImGui::BeginChild("##parts_filters", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Scrollable filter body above the fixed vendor-strip footer.
    const float stripH = 108.0f * sc;   // dots row + health line + connect + half-width row
    // WindowPadding 0: a nested child re-applies the DEFAULT style padding (15 px), which
    // pushed every filter widget 15 px right of the vendor-strip footer drawn below in the
    // OUTER child — one pane, two left edges. The scroll region must inherit the gutter,
    // not add a second one.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    ImGui::BeginChild("##filters_scroll", ImVec2(0, -stripH), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Tight base spacing, body only. ItemSpacing.y is spent BEFORE and AFTER every item, so
    // under the theme's 9 px an explicit 3 px label gap actually measures 21 px and every gap
    // in the pane converges on one height however it is written. Running the body tight (the
    // copilot menus do the same) lets the gapLabel/gapField Dummies own the rhythm.
    // ItemSpacing.x passes through untouched — the price-row split arithmetic reads it.
    // Popped before the vendor-strip footer, which keeps the theme rhythm.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 3.0f * sc));

    // ---- pane hue ----------------------------------------------------------------
    // ONE colour for the whole pane, taken from the category being browsed, so the filters
    // read as part of that branch rather than a separate grey box beside it. categoryTint()
    // hashes an unmapped id, but an EMPTY id is not a category at all — nothing is selected —
    // so that case falls back to the app accent instead of a hash of "".
    const float accentTint[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
    const float* tint = S().selectedCategory.empty() ? accentTint
                                                     : categoryTint(S().selectedCategory);

    // "N active" in the header: a filter that scrolled out of view (or one set on a facet of
    // a category the user has since collapsed) is still visible from the top of the pane.
    const int activeCount = activeFilterCount();
    char activeBuf[24];
    std::snprintf(activeBuf, sizeof(activeBuf), "%d active", activeCount);
    sectionHeader("Filters", /*firstInPane=*/true, "filter_funnel",
                  activeCount > 0 ? activeBuf : nullptr);

    // ---- global filters ---------------------------------------------------------
    // The four global fields sit on ONE hue-tinted liquid-glass card so they read as a group.
    // The gradient must be painted BEFORE the block — painted after, it would cover its own
    // labels — and the deferred-channel idiom (plmCardBegin/plmCardEnd in ui/plm/plm_pages.cpp)
    // is unavailable here because glassFieldBegin already owns this draw list's single channel
    // split and ImDrawListSplitter cannot nest. So the block measures its own height at the
    // end and the card is painted from that number on the next frame: the group is fixed in
    // content, so it is exact from frame two and self-corrects on a DPI or font change. Height
    // 0 (first frame only) simply skips the paint.
    static float s_globalCardH = 0.0f;
    const float  cardPad  = 6.0f * sc;
    const ImVec2 cardTop  = ImGui::GetCursorScreenPos();
    const float  cardW    = ImGui::GetContentRegionAvail().x;
    if (s_globalCardH > 0.0f)
        ui::glassCardWash(ImGui::GetWindowDrawList(),
                          ImVec2(cardTop.x - cardPad, cardTop.y - cardPad * 0.5f),
                          ImVec2(cardTop.x + cardW + cardPad,
                                 cardTop.y + s_globalCardH + cardPad * 0.5f),
                          8.0f, tint, 0.13f, 0.45f);

    fieldLabel("Price (USD)");
    ImGui::Dummy(ImVec2(0, gapLabel));
    // Both ends split the content width that is actually left, minus the separator and its
    // two spacings — a hardcoded pane-width guess overflowed the pane once the gutter
    // changed. sepW is the width of the rule drawn below, so the split and the paint agree.
    const float sepW   = 8.0f * sc;
    const float fieldW = std::max(ui::metrics::U(8),
        (ImGui::GetContentRegionAvail().x - sepW -
         ImGui::GetStyle().ItemSpacing.x * 2.0f) * 0.5f);
    // The "$" is chrome drawn into the frame's left inset, NOT part of the format string.
    // InputScalar prints the buffer with the decorated format but parses it back with a bare
    // "%f" (imgui_widgets.cpp, DataTypeApplyFromText): a "$%.2f" field hands sscanf "$12.00",
    // the scan fails, and every edit that does not replace the whole auto-selection is
    // silently thrown away — the field snaps back to its old value on deactivate. A suffix
    // would parse, a prefix cannot, so the glyph lives in the padding and the format stays
    // clean. FramePadding.x is widened by exactly the glyph so the value never sits under it.
    const float curW = ImGui::CalcTextSize("$").x;
    auto priceField = [&](const char* id, float* v) {
        // Captured BY VALUE: GetStyle() returns a reference whose FramePadding the PushStyleVar
        // below mutates, and the "$" belongs at the BASE inset, not the widened one.
        const float basePadX = ImGui::GetStyle().FramePadding.x;
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                            ImVec2(basePadX + curW + 2.0f * sc,
                                   ImGui::GetStyle().FramePadding.y));
        glassFieldBegin();
        ImGui::SetNextItemWidth(fieldW);
        const bool changed = ImGui::InputFloat(id, v, 0, 0, "%.2f");
        ImGui::PopStyleVar();
        const ImVec2 r0 = ImGui::GetItemRectMin();
        const float  rh = ImGui::GetItemRectSize().y;
        // Still on glassFieldBegin's FOREGROUND channel (1): glassFieldEnd paints the gradient
        // into channel 0, so a "$" emitted after the merge would be buried under the frame.
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(r0.x + basePadX, r0.y + (rh - ImGui::GetTextLineHeight()) * 0.5f),
            Palette::toU32(palette.textDim), "$");
        glassFieldEnd(tint);
        if (changed) *v = std::max(0.0f, *v);   // 0 = "this end unset" to the serializer
        return changed;
    };
    if (priceField("##pmin", &S().priceMin)) markDirty();
    ImGui::SameLine();
    // A drawn rule rather than a "-" glyph: the glyph hangs on the text baseline, well above
    // the centre of the two boxes it joins, and left the row reading as three loose items.
    {
        const ImVec2 sp = ImGui::GetCursorScreenPos();
        const float  cy = sp.y + ImGui::GetFrameHeight() * 0.5f;
        ImGui::GetWindowDrawList()->AddLine(ImVec2(sp.x, cy), ImVec2(sp.x + sepW, cy),
                                            Palette::toU32(palette.textDisabled),
                                            ui::metrics::stroke(1));
        ImGui::Dummy(ImVec2(sepW, ImGui::GetFrameHeight()));
    }
    ImGui::SameLine();
    if (priceField("##pmax", &S().priceMax)) markDirty();

    ImGui::Dummy(ImVec2(0, gapField));
    fieldLabel("Vendor");
    ImGui::Dummy(ImVec2(0, gapLabel));
    glassFieldBegin();
    ImGui::SetNextItemWidth(-FLT_MIN);
    const bool venChanged = ImGui::InputTextWithHint("##fven", "any vendor",
                                                     S().vendorBuf, sizeof(S().vendorBuf));
    glassFieldEnd(tint);
    if (venChanged) markDirty();

    ImGui::Dummy(ImVec2(0, gapField));
    fieldLabel("Material");
    ImGui::Dummy(ImVec2(0, gapLabel));
    glassFieldBegin();
    ImGui::SetNextItemWidth(-FLT_MIN);
    const bool matChanged = ImGui::InputTextWithHint("##fmat", "steel, brass, nylon…",
                                                     S().materialBuf, sizeof(S().materialBuf));
    glassFieldEnd(tint);
    if (matChanged) markDirty();

    ImGui::Dummy(ImVec2(0, gapField));
    fieldLabel("Source");
    ImGui::Dummy(ImVec2(0, gapLabel));
    glassFieldBegin();
    ImGui::SetNextItemWidth(-FLT_MIN);
    const bool srcChanged =
        ImGui::Combo("##fsrc", &S().sourceFilter, "All\0Local library\0Live vendors\0");
    glassFieldEnd(tint);
    if (srcChanged) markDirty();

    ImGui::Dummy(ImVec2(0, gapField));
    // Right-aligned clear, dead until something is actually set. SmallButton clipped its own
    // label at this pane width and stayed live with nothing to clear, so the pane always
    // looked like it was asking to be reset.
    const float clearX0 = ImGui::GetCursorPosX();
    ImGui::SetCursorPosX(std::max(clearX0,
        clearX0 + ImGui::GetContentRegionAvail().x - chipWidth("Clear all")));
    // A resettable filter set is an ACTION, not a label: the live chip gets a statusErr wash
    // UNDER its own pill (chipToggle paints straight into the draw list, so "under" means a
    // local two-channel split — legal here, no glass field is open inside it).
    ImDrawList* clearDl = ImGui::GetWindowDrawList();
    const bool  clearLive = activeCount > 0;
    if (clearLive) {
        clearDl->ChannelsSplit(2);
        clearDl->ChannelsSetCurrent(1);
    }
    ImGui::BeginDisabled(activeCount == 0);
    if (chipToggle("##reset", "Clear all", /*active=*/false)) {
        S().priceMin = S().priceMax = 0.0f;
        S().materialBuf[0] = 0;
        S().vendorBuf[0]   = 0;
        S().sourceFilter   = 0;
        S().facetValues.clear();
        markDirty();
    }
    ImGui::EndDisabled();
    const ImVec2 c0 = ImGui::GetItemRectMin(), c1 = ImGui::GetItemRectMax();
    if (clearLive) {
        clearDl->ChannelsSetCurrent(0);
        const float err[3] = {palette.statusErr[0], palette.statusErr[1], palette.statusErr[2]};
        // glassCardWash takes a LOGICAL dp radius — divide the pill radius back out of uiScale.
        ui::glassCardWash(clearDl, c0, c1, (c1.y - c0.y) * 0.5f / sc, err, 0.22f, 0.5f);
        clearDl->ChannelsMerge();
    } else {
        // chipToggle paints straight into the draw list, so BeginDisabled's style alpha never
        // reaches it — same wash the vendor strip uses on its gated rows, otherwise a dead chip
        // is pixel-identical to a live one.
        float wash[4];
        Palette::withAlpha(palette.panelBg, wash, 0.55f);
        clearDl->AddRectFilled(c0, c1, Palette::toU32(wash), (c1.y - c0.y) * 0.5f);
    }
    // Height of the global block, for NEXT frame's card (see s_globalCardH above).
    s_globalCardH = ImGui::GetCursorScreenPos().y - cardTop.y;

    // ---- per-selection facets (from the selected taxonomy node) ------------------
    bool drewFacets = false;
    if (S().taxonomyIsTree && !S().selectedCategory.empty()) {
        const TaxNode* node = findTaxNode(S().taxonomyRoot, S().selectedCategory);
        if (node && !node->facets.empty()) {
            sectionHeader(node->label.c_str(), /*firstInPane=*/false,
                          node->icon.empty() ? partIcon(node->id) : node->icon.c_str());
            drawFacetWidgets(*node, tint);
            drewFacets = true;
        }
    }
    // No facet region left the bottom two thirds of the pane as a blank slab above the vendor
    // strip — the pane looked broken rather than empty. Say what would go there instead. Held
    // back while the taxonomy is still in flight (the region is about to be filled) and while
    // a stale facet value is still narrowing the search, since claiming "no extra filters"
    // while one is active would be a lie.
    // emptyState() spends 35% of the REMAINING height centring itself, so it is only honest
    // when there is a gap to fill: below one empty-state's worth of room there is no dead
    // zone, and the leading Dummy would go negative and drag the cursor back up the pane.
    if (!drewFacets && !S().taxonomyLoading &&
        ImGui::GetContentRegionAvail().y > 96.0f * sc) {
        bool anyFacetValue = false;
        for (const auto& kv : S().facetValues)
            if (kv.second.active()) { anyFacetValue = true; break; }
        if (!anyFacetValue)
            emptyState("filter_funnel", "No extra filters here",
                       "Pick a category to filter by its own specs.");
    }

    // The scroll child ends flush against the fixed vendor-strip footer, so the last facet
    // group was sliced mid-word with nothing left to scroll into view. This is that tail.
    ImGui::Dummy(ImVec2(0, 12.0f * sc));
    ImGui::PopStyleVar();   // body ItemSpacing

    ImGui::EndChild();  // ##filters_scroll

    // ---- fixed footer: compact vendor strip --------------------------------------
    drawVendorStrip(st, paneW);

    ImGui::EndChild();  // ##parts_filters
}

}  // namespace parts
}  // namespace ui
