#include "ui/context_toolbar.h"
#include "ui/ribbon.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <string>

namespace ui {

using app::AppState;

// Compact tool button (shared with inspector / other panels).
bool toolButton(AppState& s, const char* cmdId, float cellH, bool* outArrow) {
    const cmd::Command* c = cmd::find(cmdId);
    if (!c) return false;
    if (outArrow) *outArrow = false;
    bool hasFly = !cmd::flyoutChildren(cmdId).empty();
    bool isActive = (s.activeTool == cmdId);
    const float S = theme::S();

    ImFont* f = theme::font(theme::Font::Body);
    float fs = theme::ctx().m.type.caption * S;
    ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, c->label);
    float w = ImMax(cellH * 0.92f, ts.x + 12.f * S);
    ImVec2 size(w, cellH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 p = ImGui::GetCursorScreenPos();

    ImGui::PushID(cmdId);
    ImGui::SetNextItemAllowOverlap();
    bool clicked = ImGui::InvisibleButton("b", size);
    bool hovered = ImGui::IsItemHovered();
    bool held    = ImGui::IsItemActive();

    if (isActive || held || hovered) {
        theme::glassRowBackground(dl, p, ImVec2(p.x + w, p.y + cellH),
                                  theme::ctx().m.rounding * S, false, isActive,
                                  isActive ? theme::rowWash::selected
                                           : (held ? theme::rowWash::hover : theme::rowWash::idle));
    }
    if (isActive)
        dl->AddRect(p, ImVec2(p.x + w, p.y + cellH), TCOL(accent),
                    theme::ctx().m.rounding * S, 0, ImMax(1.f, 1.5f * S));

    float iconSz = cellH * 0.42f;
    ImU32 ic = (hovered || isActive) ? TCOL(txt0) : TCOL(txt1);
    icons::draw(dl, c->icon, ImVec2(p.x + w * 0.5f, p.y + cellH * 0.34f), iconSz, ic);
    dl->AddText(f, fs, ImVec2(p.x + (w - ts.x) * 0.5f, p.y + cellH * 0.62f),
                (hovered || isActive) ? TCOL(txt0) : TCOL(txt1), c->label);

    bool arrowClicked = false;
    if (hasFly) {
        float as = cellH * 0.26f;
        ImVec2 ap0(p.x + w - as, p.y + cellH - as), ap1(p.x + w, p.y + cellH);
        bool ah = ImGui::IsMouseHoveringRect(ap0, ap1);
        ImVec2 cc(ap0.x + as * 0.5f, ap0.y + as * 0.55f);
        float t = as * 0.16f;
        dl->AddTriangleFilled(ImVec2(cc.x - t, cc.y - t * 0.5f), ImVec2(cc.x + t, cc.y - t * 0.5f),
                              ImVec2(cc.x, cc.y + t * 0.7f), (ah && hovered) ? TCOL(txt0) : TCOL(txt2));
        if (clicked && ah) { arrowClicked = true; clicked = false; if (outArrow) *outArrow = true; }
    }

    if (hovered && !arrowClicked) ImGui::SetTooltip("%s", c->label);
    ImGui::PopID();
    return clicked;
}

void drawContextToolbar(AppState& s, float height) {
    ribbon::drawBody(s, height);
}

} // namespace ui
