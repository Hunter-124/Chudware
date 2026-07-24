// Parts Library — right-hand filters pane (GUI V2 phase B).
// Global block (price / vendor / material / source) followed by per-selection facet widgets
// from the selected taxonomy node: range → paired non-negative min/max drags, enum → chips,
// text → InputTextWithHint. Every change stamps S().filterDirtyAt; the workspace loop fires
// doSearch once 0.35 s after the last change (debounce — no Apply button). The pane's fixed
// footer is the compact vendor strip (parts_vendors.cpp).
#include <algorithm>
#include <cfloat>
#include <cstdio>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

namespace {

inline void markDirty() { S().filterDirtyAt = ImGui::GetTime(); }

// Wrapping toggle chip (enum facet value): small centered borderRow sized to the label,
// accent border + accentSoft fill when active. Caller handles PushID.
bool facetChip(const char* label, bool active) {
    const float sc = ui::uiScale;
    ImVec2 sz(ImGui::CalcTextSize(label).x + 14.0f * sc, 20.0f * sc);
    if (active) {
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::GetWindowDrawList()->AddRectFilled(
            p0, ImVec2(p0.x + sz.x, p0.y + sz.y),
            Palette::toU32(palette.accentSoft), 6.0f * sc);
    }
    return borderRow("##chip", label, nullptr, active, sz, /*centerText=*/true);
}

void drawFacetWidgets(const TaxNode& node) {
    const float sc = ui::uiScale;
    for (const auto& f : node.facets) {
        FacetValue& fv = S().facetValues[f.id];
        ImGui::PushID(f.id.c_str());
        ImGui::TextDisabled("%s", f.label.c_str());

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
            const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
            ImGui::SetNextItemWidth((ImGui::GetContentRegionAvail().x - gap) * 0.5f);
            if (ImGui::DragFloat("##rmin", &fv.rmin, 0.25f, 0.0f,
                                 fv.rmax > 0.0f ? fv.rmax : FLT_MAX, fmtMin,
                                 ImGuiSliderFlags_AlwaysClamp))
                markDirty();
            ImGui::SameLine(0.0f, gap);
            ImGui::SetNextItemWidth(-FLT_MIN);
            if (ImGui::DragFloat("##rmax", &fv.rmax, 0.25f, 0.0f, FLT_MAX, fmtMax,
                                 ImGuiSliderFlags_AlwaysClamp)) {
                // Dragging max down past min drags min with it rather than inverting the range.
                if (fv.rmax > 0.0f && fv.rmax < fv.rmin) fv.rmin = fv.rmax;
                markDirty();
            }
        } else if (f.type == "enum") {
            // Wrapping toggle chips with manual SameLine wrap.
            const float availR = ImGui::GetContentRegionAvail().x +
                                 ImGui::GetCursorPosX();  // right edge in window coords
            bool first = true;
            for (const auto& opt : f.options) {
                float w = ImGui::CalcTextSize(opt.c_str()).x + 14.0f * sc;
                if (!first) {
                    ImGui::SameLine(0.0f, 4.0f * sc);
                    if (ImGui::GetCursorPosX() + w > availR) ImGui::NewLine();
                }
                first = false;
                bool active = fv.multi.count(opt) > 0;
                ImGui::PushID(opt.c_str());
                if (facetChip(opt.c_str(), active)) {
                    if (active) fv.multi.erase(opt);
                    else        fv.multi.insert(opt);
                    markDirty();
                }
                ImGui::PopID();
            }
        } else {  // "text" (and anything unknown degrades to a substring box)
            ImGui::SetNextItemWidth(-FLT_MIN);
            if (ImGui::InputTextWithHint("##text", "contains…", fv.text, sizeof(fv.text)))
                markDirty();
        }

        ImGui::Spacing();
        ImGui::PopID();
    }
}

}  // namespace

void drawFiltersBlock(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 2 * sc));  // symmetric inner margins
    ImGui::BeginChild("##parts_filters", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Scrollable filter body above the fixed vendor-strip footer.
    const float stripH = 108.0f * sc;   // dots row + health line + connect + half-width row
    ImGui::BeginChild("##filters_scroll", ImVec2(0, -stripH), ImGuiChildFlags_None);

    sectionHeader("Filters", /*firstInPane=*/true);

    // ---- global filters ---------------------------------------------------------
    ImGui::TextDisabled("Price (USD)");
    ImGui::SetNextItemWidth((paneW - 40.0f * sc) * 0.5f);
    if (ImGui::InputFloat("##pmin", &S().priceMin, 0, 0, "%.2f")) {
        S().priceMin = std::max(0.0f, S().priceMin);
        markDirty();
    }
    ImGui::SameLine();
    ImGui::TextDisabled("-");
    ImGui::SameLine();
    ImGui::SetNextItemWidth((paneW - 40.0f * sc) * 0.5f);
    if (ImGui::InputFloat("##pmax", &S().priceMax, 0, 0, "%.2f")) {
        S().priceMax = std::max(0.0f, S().priceMax);
        markDirty();
    }

    ImGui::TextDisabled("Vendor");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::InputTextWithHint("##fven", "any vendor",
                                 S().vendorBuf, sizeof(S().vendorBuf)))
        markDirty();

    ImGui::TextDisabled("Material");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::InputTextWithHint("##fmat", "steel, brass, nylon…",
                                 S().materialBuf, sizeof(S().materialBuf)))
        markDirty();

    ImGui::TextDisabled("Source");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::Combo("##fsrc", &S().sourceFilter, "All\0Local library\0Live vendors\0"))
        markDirty();

    ImGui::Spacing();
    if (ImGui::SmallButton("Reset")) {
        S().priceMin = S().priceMax = 0.0f;
        S().materialBuf[0] = 0;
        S().vendorBuf[0]   = 0;
        S().sourceFilter   = 0;
        S().facetValues.clear();
        markDirty();
    }

    // ---- per-selection facets (from the selected taxonomy node) ------------------
    if (S().taxonomyIsTree && !S().selectedCategory.empty()) {
        const TaxNode* node = findTaxNode(S().taxonomyRoot, S().selectedCategory);
        if (node && !node->facets.empty()) {
            sectionHeader(node->label.c_str());
            drawFacetWidgets(*node);
        }
    }

    ImGui::EndChild();  // ##filters_scroll

    // ---- fixed footer: compact vendor strip --------------------------------------
    drawVendorStrip(st, paneW);

    ImGui::EndChild();  // ##parts_filters
}

}  // namespace parts
}  // namespace ui
