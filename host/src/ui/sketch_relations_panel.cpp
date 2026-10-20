#include "sketch_relations_panel.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>

#include "icons.h"
#include "metrics.h"
#include "widgets/kit.h"
#include "../ui_theme.h"

namespace ui {
namespace {

const char* relationName(const RelationRow& row) {
    static const struct { const char* type; const char* name; } names[] = {
        {"coincident", "Coincident"}, {"horizontal", "Horizontal"},
        {"vertical", "Vertical"}, {"parallel", "Parallel"},
        {"perpendicular", "Perpendicular"}, {"collinear", "Collinear"},
        {"tangent", "Tangent"}, {"smooth", "Smooth (G2)"},
        {"equal", "Equal"}, {"equal_curve_length", "Equal curve length"},
        {"concentric", "Concentric"}, {"coradial", "Coradial"},
        {"midpoint", "Midpoint"}, {"symmetric", "Symmetric"},
        {"point_on", "Point on curve"}, {"intersection", "Intersection"},
        {"pierce", "Pierce"}, {"fix", "Fixed"}, {"distance", "Distance"},
        {"length", "Length"}, {"angle", "Angle"}, {"radius", "Radius"},
        {"diameter", "Diameter"}, {"arc_length", "Arc length"}
    };
    for (const auto& name : names)
        if (row.type == name.type) return name.name;
    return row.type.c_str();
}

bool containsFolded(const char* text, const char* query) {
    if (!query[0]) return true;
    const auto equal = [](char a, char b) {
        return std::tolower(static_cast<unsigned char>(a)) ==
               std::tolower(static_cast<unsigned char>(b));
    };
    const char* end = text + std::strlen(text);
    return std::search(text, end, query, query + std::strlen(query), equal) != end;
}

const char* stateName(RelationRow::State state) {
    switch (state) {
        case RelationRow::State::Redundant: return "Redundant";
        case RelationRow::State::Conflicting: return "Conflicting";
        case RelationRow::State::Skipped: return "Skipped";
        default: return "";
    }
}

bool matches(const RelationRow& row, const RelationsPanelState& state) {
    switch (state.filter) {
        case 1: if (row.dimension) return false; break;
        case 2: if (!row.dimension) return false; break;
        case 3: if (row.state == RelationRow::State::Ok) return false; break;
        case 4: if (!row.external) return false; break;
        case 5: if (!row.touchesSelection) return false; break;
        default: break;
    }
    return containsFolded(row.id.c_str(), state.search) ||
           containsFolded(row.type.c_str(), state.search) ||
           containsFolded(relationName(row), state.search) ||
           containsFolded(row.label.c_str(), state.search) ||
           containsFolded(row.value.c_str(), state.search) ||
           containsFolded(stateName(row.state), state.search) ||
           containsFolded(row.stateReason.c_str(), state.search) ||
           (row.external && containsFolded("External", state.search)) ||
           (row.driven && containsFolded("Driven", state.search));
}

bool includes(const std::vector<std::string>& ids, const std::string& id) {
    return std::find(ids.begin(), ids.end(), id) != ids.end();
}

}  // namespace

RelationsPanelResult drawRelationsPanel(RelationsPanelState& state,
                                        const std::vector<RelationRow>& rows,
                                        const std::vector<std::string>& selected,
                                        const ImVec2& viewportMin, const ImVec2& viewportMax) {
    RelationsPanelResult result;
    if (!state.open) return result;

    const ImVec2 extent(std::max(1.0f, viewportMax.x - viewportMin.x),
                        std::max(1.0f, viewportMax.y - viewportMin.y));
    const float inset = std::min(metrics::U(3), std::min(extent.x, extent.y) * 0.05f);
    const ImVec2 minPos(viewportMin.x + inset, viewportMin.y + inset);
    const ImVec2 maxSize(std::max(1.0f, extent.x - inset * 2),
                         std::max(1.0f, extent.y - inset * 2));
    const ImVec2 initialSize(std::min(metrics::px(440), maxSize.x),
                             std::min(metrics::px(560), maxSize.y));
    char windowName[96];
    std::snprintf(windowName, sizeof(windowName),
                  "Display/Delete Relations###sketch_relations_%p", static_cast<void*>(&state));
    ImGui::SetNextWindowSize(initialSize, ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSizeConstraints(
        ImVec2(std::min(metrics::px(340), maxSize.x), std::min(metrics::px(300), maxSize.y)),
        maxSize);
    if (const ImGuiWindow* previous = ImGui::FindWindowByName(windowName)) {
        const ImVec2 size(std::min(previous->Size.x, maxSize.x),
                          std::min(previous->Size.y, maxSize.y));
        const ImVec2 pos(std::clamp(previous->Pos.x, minPos.x, minPos.x + maxSize.x - size.x),
                         std::clamp(previous->Pos.y, minPos.y, minPos.y + maxSize.y - size.y));
        if (pos.x != previous->Pos.x || pos.y != previous->Pos.y)
            ImGui::SetNextWindowPos(pos);
    } else {
        ImGui::SetNextWindowPos(ImVec2(minPos.x + maxSize.x - initialSize.x, minPos.y),
                                ImGuiCond_FirstUseEver);
    }

    pushGlassWindowStyle();
    const bool visible = ImGui::Begin(windowName, &state.open,
        ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoScrollbar |
        ImGuiWindowFlags_NoScrollWithMouse);
    if (!visible) {
        ImGui::End();
        popGlassWindowStyle();
        return result;
    }
    kit::pushDensity(kit::Density::Cozy);

    static const char* filters[] = {"All", "Relations", "Dimensions", "Problems", "External", "Selected"};
    static const char* tips[] = {
        "All geometric relations and dimensions", "Geometric relations only", "Dimensions only",
        "Redundant, conflicting or skipped constraints", "Relations against external geometry",
        "Relations touching the selected sketch entities"
    };
    state.filter = std::clamp(state.filter, 0, 5);
    kit::chipFlowBegin("filters");
    for (int i = 0; i < 6; ++i) {
        ImGui::PushID(i);
        bool active = state.filter == i;
        if (kit::flowToggleChip(filters[i], &active, kit::Tone::Accent, tips[i])) state.filter = i;
        ImGui::PopID();
    }
    kit::chipFlowEnd();
    kit::gapRow();
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##search", "Search names, references, values or states...",
                             state.search, sizeof(state.search));

    std::vector<int> filtered;
    filtered.reserve(rows.size());
    int selectedCount = 0;
    for (int i = 0; i < static_cast<int>(rows.size()); ++i) {
        if (matches(rows[i], state)) filtered.push_back(i);
        if (includes(selected, rows[i].id)) ++selectedCount;
    }
    ImGui::Text("%zu of %zu constraints", filtered.size(), rows.size());
    kit::gapRow();

    const float footerHeight = ImGui::GetFrameHeight() * 2 + ImGui::GetTextLineHeight() +
                               kit::rowGap() * 3 + kit::headGap();
    const float listHeight = std::max(metrics::U(1), ImGui::GetContentRegionAvail().y - footerHeight);
    const bool listVisible = ImGui::BeginChild("##relations_list", ImVec2(0, listHeight),
        ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    const bool panelFocused = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
    if (listVisible) {
        if (filtered.empty()) {
            if (rows.empty()) {
                kit::emptyState("relations", "No relations or dimensions",
                    "Add geometric relations and dimensions to define your sketch.");
            } else if (kit::emptyState("relations", "No matching constraints",
                       "Try another filter or search, or select sketch entities for the Selected filter.",
                       "Clear filters")) {
                state.filter = 0;
                state.search[0] = '\0';
            }
        } else {
            const float lineH = ImGui::GetTextLineHeight();
            const float rowHeight = lineH * 2 + metrics::U(0.5f) + ImGui::GetFrameHeight() + kit::padY() * 2;
            ImGuiStorage* storage = ImGui::GetStateStorage();
            const ImGuiID anchorKey = ImGui::GetID("range_anchor");
            ImGuiListClipper clipper;
            clipper.Begin(static_cast<int>(filtered.size()), rowHeight + ImGui::GetStyle().ItemSpacing.y);
            while (clipper.Step()) {
                for (int at = clipper.DisplayStart; at < clipper.DisplayEnd; ++at) {
                    const RelationRow& row = rows[filtered[at]];
                    const auto& selection = result.selectionChanged ? result.selectIds : selected;
                    const bool isSelected = includes(selection, row.id);
                    const ImVec2 rowMin = ImGui::GetCursorScreenPos();
                    const ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x, rowMin.y + rowHeight);
                    const bool clicked = kit::rowBegin(row.id.c_str(), rowHeight, isSelected);
                    const bool hovered = ImGui::IsItemHovered();
                    if (hovered) result.hoveredId = row.id;
                    if (clicked) {
                        const ImGuiIO& io = ImGui::GetIO();
                        int anchor = -1;
                        const ImGuiID anchorId = static_cast<ImGuiID>(storage->GetInt(anchorKey));
                        if (io.KeyShift) {
                            for (int j = 0; j < static_cast<int>(filtered.size()); ++j)
                                if (ImHashStr(rows[filtered[j]].id.c_str()) == anchorId) { anchor = j; break; }
                            if (anchor < 0 && !selection.empty()) {
                                for (int j = 0; j < static_cast<int>(filtered.size()); ++j)
                                    if (rows[filtered[j]].id == selection.back()) { anchor = j; break; }
                            }
                        }
                        if (io.KeyCtrl) result.selectIds = selection;
                        else result.selectIds.clear();
                        if (io.KeyShift && anchor >= 0) {
                            for (int j = std::min(anchor, at); j <= std::max(anchor, at); ++j) {
                                const auto& id = rows[filtered[j]].id;
                                if (!includes(result.selectIds, id)) result.selectIds.push_back(id);
                            }
                        } else if (io.KeyCtrl && isSelected) {
                            result.selectIds.erase(std::remove(result.selectIds.begin(), result.selectIds.end(), row.id),
                                                   result.selectIds.end());
                        } else if (!includes(result.selectIds, row.id)) {
                            result.selectIds.push_back(row.id);
                        }
                        if (!io.KeyShift || anchor < 0)
                            storage->SetInt(anchorKey, static_cast<int>(ImHashStr(row.id.c_str())));
                        result.selectionChanged = true;
                    }

                    ImGui::PushClipRect(rowMin, rowMax, true);
                    const ImVec2 content = ImGui::GetCursorScreenPos();
                    const float right = rowMax.x - kit::padX();
                    const float iconSize = lineH;
                    drawIconGlass(ImGui::GetWindowDrawList(), row.dimension ? "smart_dim" :
                        row.type == "fix" ? "fix" : "relations",
                        ImVec2(content.x + iconSize * 0.5f, content.y + lineH * 0.5f),
                        iconSize, kit::toneU32(kit::Tone::Neutral), 0);
                    const float titleX = content.x + iconSize + metrics::U(1);
                    float titleRight = right;
                    if (row.dimension && !row.value.empty()) {
                        const std::string value = kit::elide(row.value.c_str(), std::max(0.0f, (right - titleX) * 0.45f));
                        const float valueW = ImGui::CalcTextSize(value.c_str()).x;
                        ImGui::GetWindowDrawList()->AddText(ImVec2(right - valueW, content.y),
                            kit::toneU32(kit::Tone::Accent), value.c_str());
                        titleRight = right - valueW - metrics::U(2);
                    }
                    const std::string name = kit::elide(relationName(row), std::max(0.0f, titleRight - titleX));
                    ImGui::GetWindowDrawList()->AddText(ImVec2(titleX, content.y),
                        Palette::toU32(palette.text), name.c_str());
                    const std::string label = kit::elide(row.label.c_str(), std::max(0.0f, right - content.x));
                    ImGui::SetCursorScreenPos(ImVec2(content.x, content.y + lineH + metrics::U(0.5f)));
                    ImGui::TextColored(kit::toneVec(kit::Tone::Neutral), "%s", label.c_str());
                    ImGui::SetCursorScreenPos(ImVec2(content.x, content.y + lineH * 2 + metrics::U(0.5f)));
                    ImGui::TextColored(kit::toneVec(kit::Tone::Neutral), "%s", row.id.c_str());
                    if (row.state != RelationRow::State::Ok) {
                        ImGui::SameLine();
                        const char* reason = row.stateReason.empty() ?
                            (row.state == RelationRow::State::Redundant ? "This constraint duplicates existing design intent." :
                             row.state == RelationRow::State::Conflicting ? "This constraint conflicts with other constraints." :
                             "The solver could not apply this constraint.") : row.stateReason.c_str();
                        kit::chip(stateName(row.state), row.state == RelationRow::State::Conflicting ?
                                  kit::Tone::Err : kit::Tone::Warn, false, reason);
                    }
                    if (row.driven) {
                        ImGui::SameLine();
                        kit::chip("Driven", kit::Tone::Info, false,
                                  "Reference dimension: reports geometry without constraining it.");
                    }
                    if (row.external) {
                        ImGui::SameLine();
                        kit::chip("External", kit::Tone::Info, false,
                                  "References geometry outside this sketch.");
                    }
                    if (hovered && !ImGui::IsItemHovered()) {
                        ImGui::BeginTooltip();
                        ImGui::Text("%s · %s", row.id.c_str(), relationName(row));
                        if (!row.label.empty()) ImGui::TextUnformatted(row.label.c_str());
                        if (!row.value.empty()) ImGui::TextUnformatted(row.value.c_str());
                        if (!row.stateReason.empty()) ImGui::TextWrapped("%s", row.stateReason.c_str());
                        ImGui::EndTooltip();
                    }
                    ImGui::PopClipRect();
                    kit::rowEnd();
                }
            }
        }
        const ImVec2 childPos = ImGui::GetWindowPos();
        const ImVec2 childSize = ImGui::GetWindowSize();
        const AdaptiveSurface& surface = adaptivePanel(ImGui::GetID("scroll_surface"),
            ImRect(childPos, ImVec2(childPos.x + childSize.x, childPos.y + childSize.y)));
        drawWindowPillScrollbarV(surface);
    }
    ImGui::EndChild();
    kit::gapRow();
    const auto& selection = result.selectionChanged ? result.selectIds : selected;
    if (result.selectionChanged) {
        selectedCount = 0;
        for (const auto& row : rows) if (includes(selection, row.id)) ++selectedCount;
    }
    ImGui::Text("%d selected · Ctrl: toggle · Shift: range", selectedCount);
    const bool deleteKey = panelFocused && !ImGui::GetIO().WantTextInput &&
        !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId) && ImGui::IsKeyPressed(ImGuiKey_Delete, false);
    const bool deleteButton = kit::dangerButton("Delete selected##delete_selected", selectedCount > 0,
        "Select one or more relations or dimensions to delete.", ImVec2(-1, 0));
    if (deleteButton || (deleteKey && selectedCount > 0)) {
        for (const auto& row : rows)
            if (includes(selection, row.id)) result.deleteIds.push_back(row.id);
    }
    if (kit::secondaryButton("Delete all in filter##delete_filter", !filtered.empty(),
        "There are no constraints in the current filter.", ImVec2(-1, 0)))
        ImGui::OpenPopup("Delete filtered constraints?");

    const ImVec2 panelPos = ImGui::GetWindowPos();
    const ImVec2 panelSize = ImGui::GetWindowSize();
    ImGui::SetNextWindowPos(ImVec2(panelPos.x + panelSize.x * 0.5f, panelPos.y + panelSize.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSizeConstraints(ImVec2(0, 0), maxSize);
    if (ImGui::BeginPopupModal("Delete filtered constraints?", nullptr, ImGuiWindowFlags_AlwaysAutoResize)) {
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + std::min(metrics::px(340), maxSize.x - kit::gutter() * 2));
        ImGui::Text("Delete all %zu constraints in the current filter?", filtered.size());
        kit::dimText("This removes their design intent from the sketch. You can undo the deletion.");
        ImGui::PopTextWrapPos();
        kit::gapRow();
        if (kit::dangerButton("Delete all##confirm", !filtered.empty())) {
            result.deleteIds.clear();
            for (int index : filtered) result.deleteIds.push_back(rows[index].id);
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (kit::secondaryButton("Cancel##cancel")) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }

    kit::popDensity();
    ImGui::End();
    popGlassWindowStyle();
    return result;
}

}  // namespace ui
