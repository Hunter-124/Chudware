// Interwebz-style chrome widgets — clean-room Chudware port (Liquid Glass v2 tokens).
// Metrics inspired by hub-menu density; geometry rewritten against ui::palette / uiScale.
// Zero global ImGui style mutation; zero hardcoded RGB outside palette helpers.
#include "interwebz.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

#include "imgui_internal.h"

#include "../icons.h"
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

// ---- Tab pill ---------------------------------------------------------------

static bool paintTabPill(const char* label, bool selected, ImVec2 size, bool vertical) {
    ImGuiWindow* window = ImGui::GetCurrentWindow();
    if (window->SkipItems) return false;

    const float S = ui::uiScale;
    const ImGuiID id = window->GetID(label);
    ImGuiContext& g = *GImGui;
    const ImGuiStyle& style = g.Style;

    const ImVec2 label_size = ImGui::CalcTextSize(label, nullptr, true);
    const float padX = (vertical ? 10.0f : 12.0f) * S;
    float w = size.x;
    float h = size.y;
    if (w <= 0.0f) w = label_size.x + padX * 2.0f;
    if (h <= 0.0f) h = (vertical ? 32.0f : 28.0f) * S;

    const ImVec2 pos = window->DC.CursorPos;
    const ImRect bb(pos, ImVec2(pos.x + w, pos.y + h));
    ImGui::ItemSize(bb, style.FramePadding.y);
    if (!ImGui::ItemAdd(bb, id)) return false;

    bool hovered = false, held = false;
    const bool pressed = ImGui::ButtonBehavior(bb, id, &hovered, &held);

    ImDrawList* dl = window->DrawList;
    const float r = 5.0f * S;

    ImU32 fill;
    if (selected)
        fill = colU32(palette.accentSoft);
    else if (held)
        fill = colU32(palette.buttonActive);
    else if (hovered)
        fill = colU32(palette.buttonHovered);
    else
        fill = colU32(palette.button);

    dl->AddRectFilled(bb.Min, bb.Max, fill, r);
    if (selected)
        dl->AddRect(bb.Min, bb.Max, colU32A(palette.accent, 0.85f), r, 0, 1.0f * S);
    else
        dl->AddRect(bb.Min, bb.Max, colU32(palette.border), r, 0, 1.0f * S);

    const ImU32 textCol = selected ? colU32(palette.accent)
                                   : (hovered ? colU32(palette.text) : colU32(palette.textDim));
    dl->AddText(ImVec2(bb.Min.x + (w - label_size.x) * 0.5f,
                       bb.Min.y + (h - label_size.y) * 0.5f),
                textCol, label);

    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    return pressed;
}

// ---- Slider -----------------------------------------------------------------

template <typename T>
static bool sliderScalarT(const char* label, ImGuiDataType data_type, T* v, T v_min, T v_max,
                          const char* format) {
    ImGuiWindow* window = ImGui::GetCurrentWindow();
    if (window->SkipItems) return false;

    ImGuiContext& g = *GImGui;
    const ImGuiStyle& style = g.Style;
    const ImGuiID id = window->GetID(label);
    const float S = ui::uiScale;

    const float w = ImGui::CalcItemWidth();
    const ImVec2 label_size = ImGui::CalcTextSize(label, nullptr, true);

    const float label_h = (label_size.x > 0.0f) ? (label_size.y + 2.0f * S) : 0.0f;
    const float track_h = 8.0f * S;
    const float grab_h  = 16.0f * S;
    const float total_h = label_h + grab_h;

    const ImVec2 pos = window->DC.CursorPos;
    const ImRect total_bb(pos, ImVec2(pos.x + w, pos.y + total_h));
    ImGui::ItemSize(total_bb, style.FramePadding.y);
    if (!ImGui::ItemAdd(total_bb, id, &total_bb)) return false;

    const float track_y0 = pos.y + label_h + (grab_h - track_h) * 0.5f;
    const ImRect frame_bb(ImVec2(pos.x, track_y0), ImVec2(pos.x + w, track_y0 + track_h));
    const ImRect interact_bb(ImVec2(frame_bb.Min.x, pos.y + label_h),
                             ImVec2(frame_bb.Max.x, pos.y + label_h + grab_h));

    // Activate on click so SliderBehavior receives ActiveId.
    const bool hovered = ImGui::ItemHoverable(interact_bb, id, g.LastItemData.ItemFlags);
    if ((hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) || g.NavActivateId == id) {
        ImGui::SetActiveID(id, window);
        ImGui::SetFocusID(id, window);
        ImGui::FocusWindow(window);
        g.ActiveIdUsingNavDirMask |= (1 << ImGuiDir_Left) | (1 << ImGuiDir_Right);
    }

    ImRect grab_bb;
    const bool value_changed =
        ImGui::SliderBehavior(interact_bb, id, data_type, v, &v_min, &v_max, format,
                              ImGuiSliderFlags_None, &grab_bb);
    if (value_changed) ImGui::MarkItemEdited(id);

    ImDrawList* dl = window->DrawList;
    const float tr = 3.0f * S;

    dl->AddRectFilled(frame_bb.Min, frame_bb.Max, colU32(palette.frameBg), tr);
    dl->AddRect(frame_bb.Min, frame_bb.Max, colU32(palette.border), tr, 0, 1.0f * S);

    const float grab_cx = (grab_bb.Min.x + grab_bb.Max.x) * 0.5f;
    const float fill_x1 = ImClamp(grab_cx, frame_bb.Min.x, frame_bb.Max.x);
    if (fill_x1 > frame_bb.Min.x + 0.5f) {
        const ImVec2 f0 = frame_bb.Min;
        const ImVec2 f1(fill_x1, frame_bb.Max.y);
        dl->AddRectFilledMultiColor(f0, f1,
                                    colU32(palette.accent), colU32(palette.accentDim),
                                    colU32(palette.accentDim), colU32(palette.accent));
        // Soft left-rounded overlay so the fill doesn't square-cut the track ends.
        dl->AddRectFilled(f0, f1, colU32A(palette.accent, 0.30f), tr,
                          ImDrawFlags_RoundCornersLeft);
    }

    const float gh = grab_h * 0.72f;
    const float gw = 6.0f * S;
    const ImVec2 g0(grab_cx - gw * 0.5f, pos.y + label_h + (grab_h - gh) * 0.5f);
    const ImVec2 g1(grab_cx + gw * 0.5f, g0.y + gh);
    const bool active = (g.ActiveId == id);
    const ImU32 grabFill = (active || hovered) ? colU32(palette.accent) : colU32(palette.accentDim);
    dl->AddRectFilled(g0, g1, grabFill, 2.0f * S);
    dl->AddRect(g0, g1, colU32A(palette.text, 0.35f), 2.0f * S, 0, 1.0f * S);

    if (label_size.x > 0.0f)
        dl->AddText(ImVec2(pos.x, pos.y), colU32(palette.textDim), label);

    char value_buf[64];
    if (data_type == ImGuiDataType_Float)
        std::snprintf(value_buf, sizeof(value_buf), format, (double)static_cast<float>(*v));
    else
        std::snprintf(value_buf, sizeof(value_buf), format, static_cast<int>(*v));
    const ImVec2 vsz = ImGui::CalcTextSize(value_buf);
    dl->AddText(ImVec2(frame_bb.Max.x + 6.0f * S,
                       pos.y + label_h + (grab_h - vsz.y) * 0.5f),
                colU32(palette.text), value_buf);

    return value_changed;
}

// ---- Combo ------------------------------------------------------------------

static int s_comboNest = 0;

static bool beginComboImpl(const char* label, const char* preview) {
    ImGuiWindow* window = ImGui::GetCurrentWindow();
    if (window->SkipItems) return false;

    ImGuiContext& g = *GImGui;
    const ImGuiStyle& style = g.Style;
    const ImGuiID id = window->GetID(label);
    const float S = ui::uiScale;

    const ImVec2 label_size = ImGui::CalcTextSize(label, nullptr, true);
    const float w = ImGui::CalcItemWidth();
    const float label_h = (label_size.x > 0.0f) ? (label_size.y + 2.0f * S) : 0.0f;
    const float field_h = 28.0f * S;

    const ImVec2 pos = window->DC.CursorPos;
    const ImRect total_bb(pos, ImVec2(pos.x + w, pos.y + label_h + field_h));
    ImGui::ItemSize(total_bb, style.FramePadding.y);
    if (!ImGui::ItemAdd(total_bb, id)) return false;

    const ImRect field_bb(ImVec2(pos.x, pos.y + label_h),
                          ImVec2(pos.x + w, pos.y + label_h + field_h));

    bool hovered = false, held = false;
    const bool pressed = ImGui::ButtonBehavior(field_bb, id, &hovered, &held);

    const bool popup_open = ImGui::IsPopupOpen(id, ImGuiPopupFlags_None);
    if (pressed && !popup_open)
        ImGui::OpenPopupEx(id, ImGuiPopupFlags_None);
    // Re-click while open: ClosePopupToLevel (CloseCurrentPopup requires BeginPopup scope).
    if (pressed && popup_open && g.OpenPopupStack.Size > 0)
        ImGui::ClosePopupToLevel(g.OpenPopupStack.Size - 1, true);

    ImDrawList* dl = window->DrawList;
    const float r = 4.0f * S;
    ImU32 fill = hovered ? colU32(palette.frameBgHovered) : colU32(palette.frameBg);
    if (held || ImGui::IsPopupOpen(id, ImGuiPopupFlags_None))
        fill = colU32(palette.frameBgActive);
    dl->AddRectFilled(field_bb.Min, field_bb.Max, fill, r);
    const bool open_now = ImGui::IsPopupOpen(id, ImGuiPopupFlags_None);
    dl->AddRect(field_bb.Min, field_bb.Max,
                open_now ? colU32A(palette.accent, 0.7f) : colU32(palette.border),
                r, 0, 1.0f * S);

    if (label_size.x > 0.0f)
        dl->AddText(ImVec2(pos.x, pos.y), colU32(palette.textDim), label);

    const float caret_w = 14.0f * S;
    const float tpad = 8.0f * S;
    if (preview && preview[0]) {
        ImGui::PushStyleColor(ImGuiCol_Text, colV4(palette.text));
        ImGui::RenderTextClipped(ImVec2(field_bb.Min.x + tpad, field_bb.Min.y),
                                 ImVec2(field_bb.Max.x - caret_w - 4.0f * S, field_bb.Max.y),
                                 preview, nullptr, nullptr, ImVec2(0.0f, 0.5f), &field_bb);
        ImGui::PopStyleColor();
    }

    {
        const float cx = field_bb.Max.x - caret_w * 0.55f;
        const float cy = (field_bb.Min.y + field_bb.Max.y) * 0.5f;
        const float ts = 3.5f * S;
        const ImU32 caretCol = colU32(palette.textDim);
        if (open_now) {
            dl->AddTriangleFilled(ImVec2(cx - ts, cy + ts * 0.35f),
                                  ImVec2(cx + ts, cy + ts * 0.35f),
                                  ImVec2(cx, cy - ts * 0.75f), caretCol);
        } else {
            dl->AddTriangleFilled(ImVec2(cx - ts, cy - ts * 0.35f),
                                  ImVec2(cx + ts, cy - ts * 0.35f),
                                  ImVec2(cx, cy + ts * 0.75f), caretCol);
        }
    }

    if (!ImGui::IsPopupOpen(id, ImGuiPopupFlags_None)) return false;

    ImGui::SetNextWindowSizeConstraints(ImVec2(w, 0.0f), ImVec2(FLT_MAX, 280.0f * S));

    ImGui::PushStyleColor(ImGuiCol_PopupBg, colV4(palette.popupBg));
    ImGui::PushStyleColor(ImGuiCol_Border, colV4(palette.glassBorder));
    ImGui::PushStyleColor(ImGuiCol_Header, colV4(palette.accentSoft));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered,
                          ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.28f));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, colV4(palette.accentMid));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 6.0f * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(6.0f * S, 6.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(4.0f * S, 2.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupBorderSize, 1.0f);
    s_comboNest++;

    if (!ImGui::BeginPopupEx(id, ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoTitleBar |
                                     ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoSavedSettings |
                                     ImGuiWindowFlags_NoMove)) {
        ImGui::PopStyleVar(4);
        ImGui::PopStyleColor(5);
        s_comboNest--;
        return false;
    }
    return true;
}

static void endComboImpl() {
    ImGui::EndPopup();
    ImGui::PopStyleVar(4);
    ImGui::PopStyleColor(5);
    s_comboNest--;
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

bool InterwebzTabPill(const char* label, bool selected, ImVec2 size) {
    return paintTabPill(label, selected, size, /*vertical=*/false);
}

bool InterwebzVTabRailItem(const char* label, const char* icon, bool selected, float height) {
    ImGuiWindow* window = ImGui::GetCurrentWindow();
    if (window->SkipItems) return false;

    const float S = ui::uiScale;
    ImGuiContext& g = *GImGui;
    const ImGuiStyle& style = g.Style;

    const ImGuiID id = window->GetID(label);
    const float h = (height > 0.0f) ? height : 32.0f * S;
    const float w = ImGui::GetContentRegionAvail().x;
    const ImVec2 pos = window->DC.CursorPos;
    const ImRect bb(pos, ImVec2(pos.x + w, pos.y + h));
    ImGui::ItemSize(bb, style.FramePadding.y);
    if (!ImGui::ItemAdd(bb, id)) return false;

    bool hovered = false, held = false;
    const bool pressed = ImGui::ButtonBehavior(bb, id, &hovered, &held);

    ImDrawList* dl = window->DrawList;
    const float r = 6.0f * S;

    if (selected) {
        const float tint[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
        coloredRowWash(dl, bb.Min, bb.Max, r, tint, 0.28f, /*selected=*/true);
        const float barW = 2.0f * S;
        dl->AddRectFilled(ImVec2(bb.Min.x, bb.Min.y + 4.0f * S),
                          ImVec2(bb.Min.x + barW, bb.Max.y - 4.0f * S),
                          colU32(palette.accent), 1.0f * S);
    } else if (hovered || held) {
        glassRowBackground(dl, bb.Min, bb.Max, r, /*stripe=*/false, /*selected=*/false, 0.55f);
    }

    float x = bb.Min.x + 10.0f * S + (selected ? 2.0f * S : 0.0f);
    const float cy = (bb.Min.y + bb.Max.y) * 0.5f;
    if (icon && icon[0]) {
        const float isz = 16.0f * S;
        const ImU32 ic = selected ? colU32(palette.accent) : colU32(palette.textDim);
        drawIconGlass(dl, icon, ImVec2(x + isz * 0.5f, cy), isz, ic, 0);
        x += isz + 8.0f * S;
    }

    const ImVec2 tsz = ImGui::CalcTextSize(label, nullptr, true);
    const ImU32 tc = selected ? colU32(palette.text) : colU32(palette.textDim);
    dl->AddText(ImVec2(x, cy - tsz.y * 0.5f), tc, label);

    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    return pressed;
}

bool InterwebzVTabRail(const char* str_id, const char* const* labels, const char* const* icons,
                       int count, int* selected, float width) {
    if (!labels || count <= 0 || !selected) return false;

    const float S = ui::uiScale;
    const float w = (width > 0.0f) ? width : 132.0f * S;

    ImGui::PushID(str_id);
    ImGui::BeginGroup();
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0.0f, 2.0f * S));

    ImGui::BeginChild("##iz_vrail", ImVec2(w, 0.0f), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);

    bool changed = false;
    if (*selected < 0) *selected = 0;
    if (*selected >= count) *selected = count - 1;

    for (int i = 0; i < count; ++i) {
        ImGui::PushID(i);
        const char* icon = (icons && icons[i]) ? icons[i] : nullptr;
        if (InterwebzVTabRailItem(labels[i], icon, i == *selected)) {
            if (*selected != i) {
                *selected = i;
                changed = true;
            }
        }
        ImGui::PopID();
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();
    ImGui::EndGroup();
    ImGui::PopID();
    return changed;
}

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
    // Leave room for slider value text hanging past the track.
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

bool InterwebzSliderFloat(const char* label, float* v, float v_min, float v_max,
                          const char* format) {
    if (!v) return false;
    if (!format) format = "%.3f";
    return sliderScalarT<float>(label, ImGuiDataType_Float, v, v_min, v_max, format);
}

bool InterwebzSliderInt(const char* label, int* v, int v_min, int v_max, const char* format) {
    if (!v) return false;
    if (!format) format = "%d";
    return sliderScalarT<int>(label, ImGuiDataType_S32, v, v_min, v_max, format);
}

bool InterwebzBeginCombo(const char* label, const char* preview_value) {
    return beginComboImpl(label, preview_value);
}

void InterwebzEndCombo() { endComboImpl(); }

bool InterwebzCombo(const char* label, int* current, const char* const items[], int items_count) {
    if (!current || !items || items_count <= 0) return false;
    if (*current < 0) *current = 0;
    if (*current >= items_count) *current = items_count - 1;

    const char* preview = items[*current] ? items[*current] : "";
    bool changed = false;
    if (InterwebzBeginCombo(label, preview)) {
        for (int i = 0; i < items_count; ++i) {
            const bool sel = (i == *current);
            if (ImGui::Selectable(items[i], sel)) {
                if (*current != i) {
                    *current = i;
                    changed = true;
                }
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        InterwebzEndCombo();
    }
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
