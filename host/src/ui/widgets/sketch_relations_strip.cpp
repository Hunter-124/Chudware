#include "sketch_relations_strip.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <utility>

#include "../../ui_theme.h"   // ui::palette, glassRowBackground, coloredRowWash, context menu style
#include "../metrics.h"
#include "kit.h"               // kit::elide

namespace ui {
namespace {

SketchRelationsStripModel s_model;
SketchRelationsStripResult s_result;
std::string s_menuId;         // existing relation the context menu was opened on

float snapPx(float v) { return std::floor(v + 0.5f); }

ImU32 rgba(const float c[4], float alpha = 1.0f) {
    const float out[4] = {c[0], c[1], c[2], c[3] * alpha};
    return Palette::toU32(out);
}

// One square chip button at `pos`. Returns the click state; `hovered` reports hover.
bool chipButton(const char* id, ImVec2 pos, float side, bool& hovered) {
    ImGui::SetCursorScreenPos(pos);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(side, side));
    hovered = ImGui::IsItemHovered();
    return clicked;
}

// "+N" / "..." text chip used for list overflow.
bool overflowChip(const char* id, const char* text, ImVec2 pos, float side, const char* tip) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    bool hovered = false;
    const ImVec2 textSize = ImGui::CalcTextSize(text);
    const float w = std::max(side, textSize.x + metrics::U(1.5f));
    ImGui::SetCursorScreenPos(pos);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(w, side));
    hovered = ImGui::IsItemHovered();
    const float round = side * 0.22f;
    dl->AddRectFilled(pos, ImVec2(pos.x + w, pos.y + side),
                      rgba(hovered ? palette.buttonHovered : palette.button), round);
    dl->AddRect(pos, ImVec2(pos.x + w, pos.y + side), rgba(palette.border), round, 0,
                metrics::stroke(1.0f));
    dl->AddText(ImVec2(snapPx(pos.x + (w - textSize.x) * 0.5f),
                       snapPx(pos.y + (side - textSize.y) * 0.5f)),
                rgba(palette.text), text);
    if (hovered) ImGui::SetTooltip("%s", tip);
    return clicked;
}

}  // namespace

void publishSketchRelationsStrip(SketchRelationsStripModel model) {
    s_model = std::move(model);
}

SketchRelationsStripResult takeSketchRelationsStripResult() {
    return std::exchange(s_result, SketchRelationsStripResult{});
}

float sketchRelationsStripWidth() {
    return metrics::px(244.0f);
}

void drawSketchRelationsStrip(const ImVec2& min, const ImVec2& max, float contentH) {
    s_result.hoveredId.clear();
    if (max.x - min.x <= 0.0f || max.y - min.y <= 0.0f) return;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImGuiIO& io = ImGui::GetIO();

    // Group pill + caption, matching the ribbon's own groups.
    const float gap = metrics::U(1);
    const ImVec2 g0(snapPx(min.x + gap), snapPx(min.y));
    const ImVec2 g1(snapPx(max.x - gap), snapPx(max.y));
    const float tint[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
    glassRowBackground(dl, g0, g1, metrics::px(6.0f), false, false, 0.30f);
    coloredRowWash(dl, g0, g1, metrics::px(6.0f), tint, 0.16f, false);
    {
        const char* caption = "Relations";
        const ImVec2 ts = ImGui::CalcTextSize(caption);
        const float capTop = min.y + contentH;
        dl->AddText(ImVec2(snapPx(g0.x + ((g1.x - g0.x) - ts.x) * 0.5f),
                           snapPx(capTop + ((max.y - capTop) - ts.y) * 0.5f)),
                    rgba(palette.textDim), caption);
    }

    const float padX = metrics::U(2);
    const float x0 = g0.x + padX, x1 = g1.x - padX;
    const float rowH = contentH / 3.0f;
    if (!s_model.hasSelection) {
        const std::string hint = kit::elide("Select sketch entities", x1 - x0);
        const ImVec2 ts = ImGui::CalcTextSize(hint.c_str());
        dl->AddText(ImVec2(snapPx(x0 + (x1 - x0 - ts.x) * 0.5f),
                           snapPx(min.y + (contentH - ts.y) * 0.5f)),
                    rgba(palette.textDim), hint.c_str());
        return;
    }

    const char* existingLabel = "Existing";
    const char* addLabel = "Add";
    const float labelW = std::max(ImGui::CalcTextSize(existingLabel).x,
                                  ImGui::CalcTextSize(addLabel).x) + metrics::U(2);
    const float side = std::min(snapPx(rowH - metrics::U(1)), metrics::px(20.0f));
    const float step = side + gap;
    const float chipsX = x0 + labelW;
    const int perRow = std::max(1, (int)std::floor((x1 - chipsX + gap) / step));
    const float lineH = ImGui::GetTextLineHeight();
    auto rowTop = [&](int row) { return min.y + rowH * row; };
    auto label = [&](int row, const char* text) {
        dl->AddText(ImVec2(x0, snapPx(rowTop(row) + (rowH - lineH) * 0.5f)),
                    rgba(palette.textDim), text);
    };
    auto chipPos = [&](int row, int col) {
        return ImVec2(snapPx(chipsX + col * step), snapPx(rowTop(row) + (rowH - side) * 0.5f));
    };
    auto noneText = [&](int row) {
        dl->AddText(ImVec2(chipsX, snapPx(rowTop(row) + (rowH - lineH) * 0.5f)),
                    rgba(palette.textDisabled), "None");
    };
    const float round = side * 0.22f;

    ImGui::PushID("##sketch_relations_strip");

    // Row 0: relations already on the selection, drawn like the viewport badges.
    label(0, existingLabel);
    const auto& existing = s_model.existing;
    if (existing.empty()) noneText(0);
    const int existingSlots = (int)existing.size() > perRow ? perRow - 1 : (int)existing.size();
    for (int i = 0; i < existingSlots; ++i) {
        const auto& rel = existing[i];
        ImGui::PushID(rel.id.c_str());
        const ImVec2 p = chipPos(0, i);
        bool hovered = false;
        if (chipButton("##existing", p, side, hovered)) {
            s_result.toggleId = rel.id;
            s_result.additive = io.KeyCtrl;
        }
        if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) {
            s_menuId = rel.id;
            ImGui::OpenPopup("##sketch_relations_strip_menu");
        }
        if (hovered) {
            s_result.hoveredId = rel.id;
            ImGui::SetTooltip("%s\nClick: select (Ctrl: toggle)  Right-click: delete", rel.tip.c_str());
        }
        const bool hot = rel.selected || hovered;
        const ImVec2 q(p.x + side, p.y + side);
        dl->AddRectFilled(p, q, hot ? rgba(palette.accent, 0.85f) : rgba(palette.sketchRelBg), round);
        dl->AddRect(p, q, rel.problem ? rgba(palette.statusErr) :
                    hot ? rgba(palette.accent) : rgba(palette.sketchRelBorder),
                    round, 0, metrics::stroke((rel.problem || hot) ? 1.8f : 1.2f));
        if (s_model.drawGlyph)
            s_model.drawGlyph(dl, ImVec2(p.x + side * 0.5f, p.y + side * 0.5f), side,
                              rel.type.c_str(),
                              rel.problem ? rgba(palette.statusErr) : hot ? rgba(palette.statusBg) :
                              rgba(palette.sketchInk, rel.automatic ? 0.6f : 1.0f));
        ImGui::PopID();
    }
    if (existingSlots < (int)existing.size()) {
        char text[16];
        std::snprintf(text, sizeof(text), "+%d", (int)existing.size() - existingSlots);
        if (overflowChip("##existing_more", text, chipPos(0, existingSlots), side,
                         "Show every relation on the selection (Display/Delete Relations)"))
            s_result.openPanel = true;
    }

    // Rows 1-2: relations the Add Relation rules offer for this selection.
    label(1, addLabel);
    const auto& possible = s_model.possible;
    if (possible.empty()) noneText(1);
    const int capacity = perRow * 2;
    const int possibleSlots = (int)possible.size() > capacity ? capacity - 1 : (int)possible.size();
    for (int i = 0; i < possibleSlots; ++i) {
        const auto& rel = possible[i];
        ImGui::PushID(i);
        const ImVec2 p = chipPos(1 + i / perRow, i % perRow);
        bool hovered = false;
        if (chipButton("##possible", p, side, hovered)) s_result.addIndex = i;
        if (hovered) ImGui::SetTooltip("Add %s", rel.label.c_str());
        const ImVec2 q(p.x + side, p.y + side);
        dl->AddRectFilled(p, q, rgba(hovered ? palette.buttonHovered : palette.button), round);
        dl->AddRect(p, q, hovered ? rgba(palette.accent) : rgba(palette.border), round, 0,
                    metrics::stroke(1.0f));
        if (s_model.drawGlyph)
            s_model.drawGlyph(dl, ImVec2(p.x + side * 0.5f, p.y + side * 0.5f), side,
                              rel.type.c_str(), rgba(palette.text));
        ImGui::PopID();
    }
    if (possibleSlots < (int)possible.size()) {
        if (overflowChip("##possible_more", "...", chipPos(1 + possibleSlots / perRow,
                                                           possibleSlots % perRow), side,
                         "More relations (Add Relation)"))
            s_result.openAddRelation = true;
    }

    pushContextMenuStyle();
    if (ImGui::BeginPopup("##sketch_relations_strip_menu")) {
        if (ImGui::MenuItem("Select")) { s_result.toggleId = s_menuId; s_result.additive = false; }
        if (ImGui::MenuItem("Delete")) s_result.deleteId = s_menuId;
        ImGui::EndPopup();
    }
    popContextMenuStyle();
    ImGui::PopID();
}

}  // namespace ui
