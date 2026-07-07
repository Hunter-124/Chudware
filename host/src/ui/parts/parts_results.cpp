// Parts Library — results (list table + grid cards) and per-item actions.
// GUI V2 phase A: the list is a real ImGui table (Name | Vendor | Part# | Material | Price,
// natural clipping instead of the old printf %-36.36s column hack) with an 18px domain icon
// before the name; hover shows the border treatment + the Download/Insert actions.
#include <algorithm>
#include <cmath>
#include <cstdio>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

static void itemActions(AppState& st, const PartsItem& item) {
    const bool isCatalog = item.id.rfind("catalog:", 0) == 0;
    if (isCatalog && !item.downloaded && ImGui::SmallButton("Download"))
        queueDownload(st, item);
    if (isCatalog) ImGui::SameLine();
    if (ImGui::SmallButton("Insert"))
        insertItem(st, item);
}

static void itemTooltipAt(const PartsItem& item, bool hovered) {
    if (!hovered) return;
    ImGui::BeginTooltip();
    ImGui::TextUnformatted(item.name.c_str());
    if (!item.description.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::PushTextWrapPos(360.0f * ui::uiScale);
        ImGui::TextUnformatted(item.description.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }
    if (!item.material.empty()) ImGui::Text("Material: %s", item.material.c_str());
    if (!item.standard.empty()) ImGui::Text("Standard: %s", item.standard.c_str());
    if (!item.license.empty())  ImGui::TextDisabled("License: %s", item.license.c_str());
    if (!item.datasheet.empty()) ImGui::TextDisabled("Datasheet: %s", item.datasheet.c_str());
    ImGui::TextDisabled("%s", item.source == "live" ? "Live vendor result" : "Local library");
    ImGui::EndTooltip();
}

static void itemTooltip(const PartsItem& item) {
    itemTooltipAt(item, ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal));
}

void drawResultsList(AppState& st) {
    const float sc = ui::uiScale;

    if (S().searching) { spinner("Searching"); return; }
    if (!S().searchErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Error: %s", S().searchErr.c_str());
        ImGui::PopStyleColor();
        return;
    }
    if (S().results.empty()) {
        if (S().searchDone)
            emptyState("cancel_x", "No results",
                       "Try a different search term, category, or loosen the filters.");
        else
            emptyState("parts_all", "Search for parts",
                       "Type in the search bar or pick a category to browse.");
        return;
    }

    const ImGuiTableFlags tblFlags =
        ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoBordersInBody;
    if (!ImGui::BeginTable("##parts_tbl", 6, tblFlags)) return;
    ImGui::TableSetupColumn("Name",     ImGuiTableColumnFlags_WidthStretch);
    ImGui::TableSetupColumn("Vendor",   ImGuiTableColumnFlags_WidthFixed, 110.0f * sc);
    ImGui::TableSetupColumn("Part #",   ImGuiTableColumnFlags_WidthFixed,  90.0f * sc);
    ImGui::TableSetupColumn("Material", ImGuiTableColumnFlags_WidthFixed,  90.0f * sc);
    ImGui::TableSetupColumn("Price",    ImGuiTableColumnFlags_WidthFixed,  80.0f * sc);
    ImGui::TableSetupColumn("##act",    ImGuiTableColumnFlags_WidthFixed, 140.0f * sc);

    // Header row (textDim, no ugly TableHeadersRow chrome).
    ImGui::TableNextRow();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    const char* heads[5] = {"Name", "Vendor", "Part #", "Material", "Price"};
    for (int c = 0; c < 5; ++c) {
        ImGui::TableSetColumnIndex(c);
        ImGui::TextUnformatted(heads[c]);
    }
    ImGui::PopStyleColor();

    const float rowH   = ImGui::GetFrameHeight();
    const float iconSz = 18.0f * sc;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    for (auto& item : S().results) {
        ImGui::PushID(item.id.c_str());
        ImGui::TableNextRow(ImGuiTableRowFlags_None, rowH);
        ImGui::TableSetColumnIndex(0);

        // Row-spanning selectable: gives us hover state; actions overlap it.
        ImVec2 rowPos = ImGui::GetCursorScreenPos();
        ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
        ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
        ImGui::Selectable("##row", false,
                          ImGuiSelectableFlags_SpanAllColumns |
                          ImGuiSelectableFlags_AllowOverlap,
                          ImVec2(0, rowH));
        ImGui::PopStyleColor(2);
        bool hovered = ImGui::IsItemHovered(
            ImGuiHoveredFlags_AllowWhenOverlappedByItem | ImGuiHoveredFlags_AllowWhenBlockedByActiveItem);
        ImVec2 rMin = ImGui::GetItemRectMin();
        ImVec2 rMax = ImGui::GetItemRectMax();
        if (hovered) {
            // Border treatment (hover): treeHover wash + accentMid border, inset 0.5px.
            dl->AddRectFilled(rMin, rMax, Palette::toU32(palette.treeHover), 4.0f * sc);
            dl->AddRect(ImVec2(rMin.x + 0.5f, rMin.y + 0.5f),
                        ImVec2(rMax.x - 0.5f, rMax.y - 0.5f),
                        Palette::toU32(palette.accentMid), 4.0f * sc, 0, 1.0f * sc);
        }
        itemTooltipAt(item, hovered && ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal |
                                                            ImGuiHoveredFlags_AllowWhenOverlappedByItem));

        // ---- Name cell: domain icon + status badges + name ----
        ImGui::SetCursorScreenPos(rowPos);
        partIconDraw(dl, ImVec2(rowPos.x + iconSz * 0.5f, rowPos.y + rowH * 0.5f),
                     iconSz, item.domain);
        ImGui::SetCursorScreenPos(ImVec2(rowPos.x + iconSz + 6.0f * sc,
                                         rowPos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f));
        if (item.downloaded) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1],
                       palette.statusOk[2], 1.0f));
            ImGui::TextUnformatted("[v]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        if (item.source == "live") {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.9f));
            ImGui::TextUnformatted("[live]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        ImGui::TextUnformatted(item.name.c_str());   // table clipping ellipsizes naturally

        auto dimCell = [&](int col, const char* text) {
            ImGui::TableSetColumnIndex(col);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(text);
            ImGui::PopStyleColor();
        };
        dimCell(1, item.vendor.c_str());
        dimCell(2, item.partNumber.c_str());
        dimCell(3, item.material.empty() ? "-" : item.material.c_str());

        ImGui::TableSetColumnIndex(4);
        if (item.priceUsd > 0.0)
            ImGui::Text("$%.2f/%s", item.priceUsd, item.unit.c_str());
        else
            ImGui::TextDisabled("—");

        ImGui::TableSetColumnIndex(5);
        if (hovered) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            itemActions(st, item);
            ImGui::PopStyleColor();
        }

        ImGui::PopID();
    }
    ImGui::EndTable();
}

void drawResultsGrid(AppState& st) {
    const float sc    = ui::uiScale;
    const float cardW = 168.0f * sc;
    const float cardH = 168.0f * sc;
    const float imgH  = 88.0f * sc;
    float availW      = ImGui::GetContentRegionAvail().x;
    int   cols        = std::max(1, (int)(availW / (cardW + 8.0f * sc)));

    if (S().searching) { spinner("Searching"); return; }
    if (S().results.empty()) {
        if (S().searchDone)
            emptyState("cancel_x", "No results", "Try a different search term or category.");
        else
            emptyState("parts_all", "Search for parts",
                       "Type in the search bar or pick a category.");
        return;
    }

    int i = 0;
    for (auto& item : S().results) {
        if (i % cols != 0) ImGui::SameLine();
        ImGui::PushID(item.id.c_str());

        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImVec2 p1(p0.x + cardW, p0.y + cardH);
        bool hov = ImGui::IsMouseHoveringRect(p0, p1);
        // Resting glass surface stays; hover reads by BORDER, not a stronger wash.
        ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 8.0f,
                               false, false, 0.20f);
        borderCardRect(ImGui::GetWindowDrawList(), p0, p1, /*selected=*/false, hov, 8.0f);

        ImGui::BeginGroup();

        // ---- preview: real thumbnail when available, domain icon/glyph otherwise ----
        requestThumb(st, item);
        auto th = S().thumbs.find(item.id);
        if (th != S().thumbs.end() && th->second.tex) {
            // Fit the image into the preview band, centred, preserving aspect.
            float iw = (float)th->second.w, ih = (float)th->second.h;
            float sc = std::min((cardW - 16.0f * sc) / iw, imgH / ih);
            float dw = iw * sc, dh = ih * sc;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (cardW - dw) * 0.5f);
            ImGui::SetCursorPosY(ImGui::GetCursorPosY() + (imgH - dh) * 0.5f);
            ImGui::Image((ImTextureID)(intptr_t)th->second.tex, ImVec2(dw, dh));
            float used = (imgH - dh) * 0.5f + dh;
            if (used < imgH) ImGui::Dummy(ImVec2(1, imgH - used));
        } else {
            ImVec2 c(p0.x + cardW * 0.5f, p0.y + imgH * 0.5f + 4.0f * sc);
            partIconDraw(ImGui::GetWindowDrawList(), c, 52.0f * sc, item.domain);
            ImGui::Dummy(ImVec2(cardW, imgH));
        }

        auto centre = [&](const char* t) {
            float tw = ImGui::CalcTextSize(t).x;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (cardW - tw) * 0.5f);
            ImGui::TextUnformatted(t);
        };
        char nbuf[40];
        std::snprintf(nbuf, sizeof(nbuf), "%.34s", item.name.c_str());
        centre(nbuf);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        centre(item.partNumber.c_str());
        ImGui::PopStyleColor();

        char pbuf[48];
        if (item.priceUsd > 0.0)
            std::snprintf(pbuf, sizeof(pbuf), "$%.2f%s", item.priceUsd,
                          item.source == "live" ? "  [live]" : "");
        else
            std::snprintf(pbuf, sizeof(pbuf), "%s",
                          item.source == "live" ? "[live]" : "—");
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        centre(pbuf);
        ImGui::PopStyleColor();

        // Hover action row inside the card.
        if (hov) {
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + 10.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            itemActions(st, item);
            ImGui::PopStyleColor();
        }
        ImGui::EndGroup();
        itemTooltip(item);

        // Keep card cells a fixed height so the flow layout stays aligned.
        ImVec2 cur = ImGui::GetItemRectMax();
        if (cur.y < p1.y) ImGui::Dummy(ImVec2(cardW, p1.y - cur.y));

        ImGui::PopID();
        ++i;
    }
}

}  // namespace parts
}  // namespace ui
