// Interwebz-style chrome widgets — clean-room Chudware port (Liquid Glass v2 tokens).
// Geometry rewritten against ui::palette / uiScale.
// Zero global ImGui style mutation; zero hardcoded RGB outside palette helpers.
#include "interwebz.h"

#include "imgui_internal.h"

#include "../../ui_theme.h"

namespace ui {
namespace {

static ImU32 colU32(const float f[4]) { return Palette::toU32(f); }

static ImU32 colU32A(const float f[4], float aMul) {
    float t[4];
    Palette::withAlpha(f, t, aMul);
    return Palette::toU32(t);
}

static ImVec4 colV4(const float f[4]) {
    return ImVec4(f[0], f[1], f[2], f[3]);
}

// ---- Two-column state -------------------------------------------------------

struct TwoColState {
    float gap = 0.f;
    float col_w = 0.f;
    float avail_x = 0.f;
    bool stack = false;
};
static TwoColState s_twoCol;

}  // namespace

// =============================================================================
// Public API
// =============================================================================

bool InterwebzGroupBegin(const char* id, const char* title, ImVec2 size) {
    const float S = ui::uiScale;

    if (title && title[0]) {
        ImGui::PushStyleColor(ImGuiCol_Text, colV4(palette.accent));
        ImGui::TextUnformatted(title);
        ImGui::PopStyleColor();
        const ImVec2 p0 = ImGui::GetItemRectMin();
        const ImVec2 p1 = ImGui::GetItemRectMax();
        ImGui::GetWindowDrawList()->AddLine(
            ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
            colU32A(palette.accent, 0.60f), 1.0f * S);
        ImGui::Dummy(ImVec2(0, 4.0f * S));
    }

    ui::pushGlassChildStyle(8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14.0f * S, 12.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(8.0f * S, 8.0f * S));

    return ImGui::BeginChild(id, size, ImGuiChildFlags_Borders);
}

void InterwebzGroupEnd() {
    ImGui::EndChild();
    ImGui::PopStyleVar(2);
    ui::popGlassChildStyle();
}

void InterwebzTwoColumnBegin(float gap_dp) {
    const float S = ui::uiScale;
    s_twoCol.gap = gap_dp * S;
    s_twoCol.avail_x = ImGui::GetContentRegionAvail().x;
    s_twoCol.stack = (s_twoCol.avail_x < 520.0f * S);

    if (s_twoCol.stack)
        s_twoCol.col_w = s_twoCol.avail_x;
    else
        s_twoCol.col_w = (s_twoCol.avail_x - s_twoCol.gap) * 0.5f;

    ImGui::BeginGroup();
    // Items keep a 56dp right margin inside each column.
    ImGui::PushItemWidth(ImMax(40.0f * S, s_twoCol.col_w - 56.0f * S));
}

void InterwebzTwoColumnNext() {
    const float S = ui::uiScale;
    ImGui::PopItemWidth();
    ImGui::EndGroup();

    if (s_twoCol.stack)
        ImGui::Dummy(ImVec2(0, 10.0f * S));
    else
        ImGui::SameLine(0.0f, s_twoCol.gap);

    ImGui::BeginGroup();
    ImGui::PushItemWidth(ImMax(40.0f * S, s_twoCol.col_w - 56.0f * S));
}

void InterwebzTwoColumnEnd() {
    ImGui::PopItemWidth();
    ImGui::EndGroup();
}

bool InterwebzCheckbox(const char* label, bool* v) {
    ImGuiWindow* window = ImGui::GetCurrentWindow();
    if (window->SkipItems || !v) return false;

    ImGuiContext& g = *GImGui;
    const ImGuiStyle& style = g.Style;
    const ImGuiID id = window->GetID(label);
    const float S = ui::uiScale;

    const float box = 16.0f * S;
    const ImVec2 label_size = ImGui::CalcTextSize(label, nullptr, true);
    const float h = ImMax(box, label_size.y);
    const float pad = 8.0f * S;

    const ImVec2 pos = window->DC.CursorPos;
    const float total_w = box + pad + label_size.x;
    const ImRect total_bb(pos, ImVec2(pos.x + total_w, pos.y + h));
    ImGui::ItemSize(total_bb, style.FramePadding.y);
    if (!ImGui::ItemAdd(total_bb, id)) return false;

    bool hovered = false, held = false;
    const bool pressed = ImGui::ButtonBehavior(total_bb, id, &hovered, &held);
    bool changed = false;
    if (pressed) {
        *v = !(*v);
        changed = true;
        ImGui::MarkItemEdited(id);
    }

    const float box_y = pos.y + (h - box) * 0.5f;
    const ImRect box_bb(ImVec2(pos.x, box_y), ImVec2(pos.x + box, box_y + box));
    ImDrawList* dl = window->DrawList;
    const float r = 3.0f * S;

    if (*v) {
        dl->AddRectFilledMultiColor(box_bb.Min, box_bb.Max,
                                    colU32(palette.accent), colU32(palette.accentDim),
                                    colU32(palette.accentDim), colU32(palette.accent));
        dl->AddRect(box_bb.Min, box_bb.Max, colU32A(palette.accent, 0.9f), r, 0, 1.0f * S);
        const float pad_ck = 3.0f * S;
        ImGui::RenderCheckMark(dl, ImVec2(box_bb.Min.x + pad_ck, box_bb.Min.y + pad_ck),
                               colU32(palette.text), box - pad_ck * 2.0f);
    } else {
        ImU32 fill = hovered ? colU32(palette.frameBgHovered) : colU32(palette.frameBg);
        if (held) fill = colU32(palette.frameBgActive);
        dl->AddRectFilled(box_bb.Min, box_bb.Max, fill, r);
        dl->AddRect(box_bb.Min, box_bb.Max, colU32(palette.border), r, 0, 1.0f * S);
    }

    if (label_size.x > 0.0f) {
        dl->AddText(ImVec2(box_bb.Max.x + pad, pos.y + (h - label_size.y) * 0.5f),
                    colU32(palette.text), label);
    }

    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    return changed;
}

void InterwebzKeyValueRow(const char* key, const char* value) {
    const float S = ui::uiScale;
    ImGuiWindow* window = ImGui::GetCurrentWindow();
    if (window->SkipItems) return;

    const float avail = ImGui::GetContentRegionAvail().x;
    const ImVec2 ksz = ImGui::CalcTextSize(key ? key : "");
    const ImVec2 vsz = ImGui::CalcTextSize(value ? value : "");
    const float h = ImMax(ksz.y, vsz.y) + 4.0f * S;
    const ImVec2 pos = window->DC.CursorPos;
    const ImRect bb(pos, ImVec2(pos.x + avail, pos.y + h));

    // Unique id per key so multiple rows coexist.
    ImGui::PushID(key ? key : "##iz_kv");
    const ImGuiID id = window->GetID("##row");
    ImGui::ItemSize(bb);
    ImGui::ItemAdd(bb, id);
    ImGui::PopID();

    ImDrawList* dl = window->DrawList;
    if (key)
        dl->AddText(ImVec2(pos.x, pos.y + (h - ksz.y) * 0.5f), colU32(palette.text), key);
    if (value) {
        const float vx = pos.x + avail - vsz.x;
        dl->AddText(ImVec2(vx, pos.y + (h - vsz.y) * 0.5f), colU32(palette.textDim), value);
    }
}

}  // namespace ui
