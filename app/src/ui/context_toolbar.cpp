#include "ui/panels.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <string>
#include <cstring>
#include <cstdio>

namespace ui {

using app::AppState;

// A tool button: icon over a label. Cell width auto-fits the label (min ~ cellH).
// Split-button chevron when it has flyout children.
bool toolButton(AppState& s, const char* cmdId, float cellH, bool* outArrow) {
    const cmd::Command* c = cmd::find(cmdId);
    if (!c) return false;
    if (outArrow) *outArrow = false;
    bool hasFly = !cmd::flyoutChildren(cmdId).empty();
    bool isActive = (s.activeTool == cmdId);
    const float S = theme::S();

    ImFont* f = theme::font(theme::Font::Body);
    float fs = 11.5f * S;
    ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, c->label);
    float w = ImMax(cellH * 0.92f, ts.x + 12*S);
    ImVec2 size(w, cellH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 p = ImGui::GetCursorScreenPos();

    ImGui::PushID(cmdId);
    ImGui::SetNextItemAllowOverlap();
    bool clicked = ImGui::InvisibleButton("b", size);
    bool hovered = ImGui::IsItemHovered();
    bool held    = ImGui::IsItemActive();

    ImU32 bg = isActive ? TCOL(accentDim) : held ? TCOL(active) : hovered ? TCOL(hover) : 0;
    if (bg) dl->AddRectFilled(p, ImVec2(p.x+w, p.y+cellH), bg, theme::ctx().m.rounding*S);
    if (isActive)
        dl->AddRect(p, ImVec2(p.x+w, p.y+cellH), TCOL(accent), theme::ctx().m.rounding*S, 0, ImMax(1.f, 1.5f*S));

    float iconSz = cellH * 0.42f;
    ImU32 ic = (hovered || isActive) ? TCOL(txt0) : TCOL(txt1);
    icons::draw(dl, c->icon, ImVec2(p.x+w*0.5f, p.y+cellH*0.34f), iconSz, ic);
    dl->AddText(f, fs, ImVec2(p.x + (w - ts.x)*0.5f, p.y + cellH*0.62f),
                (hovered||isActive)?TCOL(txt0):TCOL(txt1), c->label);

    // chevron is a pure hit-test (NOT a second item) so it never disturbs the row layout.
    bool arrowClicked = false;
    if (hasFly) {
        float as = cellH * 0.26f;
        ImVec2 ap0(p.x + w - as, p.y + cellH - as), ap1(p.x + w, p.y + cellH);
        bool ah = ImGui::IsMouseHoveringRect(ap0, ap1);
        ImVec2 cc(ap0.x + as*0.5f, ap0.y + as*0.55f);
        float t = as*0.16f;
        dl->AddTriangleFilled(ImVec2(cc.x-t,cc.y-t*0.5f), ImVec2(cc.x+t,cc.y-t*0.5f),
                              ImVec2(cc.x,cc.y+t*0.7f), (ah&&hovered)?TCOL(txt0):TCOL(txt2));
        if (clicked && ah) { arrowClicked = true; clicked = false; if (outArrow) *outArrow = true; }
    }

    if (hovered && !arrowClicked) ImGui::SetTooltip("%s", c->label);
    ImGui::PopID();
    return clicked;
}

static void flyoutPopup(AppState& s, const char* parentId) {
    std::string pid = std::string("fly_") + parentId;
    if (ImGui::BeginPopup(pid.c_str())) {
        const cmd::Command* parent = cmd::find(parentId);
        if (parent) {
            if (ImGui::MenuItem(parent->label)) { s.dispatch(parentId); }
            ImGui::Separator();
        }
        for (const cmd::Command* ch : cmd::flyoutChildren(parentId))
            if (ImGui::MenuItem(ch->label)) s.dispatch(ch->id);
        ImGui::EndPopup();
    }
}

void drawContextToolbar(AppState& s, float height) {
    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg2));
    ImGui::BeginChild("##toolbar", ImVec2(0, height), false, ImGuiWindowFlags_HorizontalScrollbar);

    const float S = theme::S();
    const float pad = 8.f * S;
    const float capH = 14.f * S;
    const float btn  = height - capH - 2*pad;
    const float gap  = 3.f * S;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    cmd::Workspace ws = s.effectiveWorkspace();
    auto groups = cmd::groupsFor(ws);
    auto tops   = cmd::topCommands(ws);

    ImGui::SetCursorPos(ImVec2(pad, pad));
    bool firstGroup = true;
    for (cmd::Group g : groups) {
        if (!firstGroup) {
            // vertical separator
            ImGui::SameLine(0, 8*S);
            ImVec2 sp = ImGui::GetCursorScreenPos();
            dl->AddLine(ImVec2(sp.x, sp.y+2*S), ImVec2(sp.x, sp.y+btn-2*S), TCOL(line), ImMax(1.f, S));
            ImGui::SameLine(0, 8*S);
        }
        firstGroup = false;

        ImGui::BeginGroup();
        bool firstBtn = true;
        for (const cmd::Command* c : tops) {
            if (c->group != g) continue;
            if (!firstBtn) ImGui::SameLine(0, gap);
            firstBtn = false;
            bool arrow = false;
            if (toolButton(s, c->id, btn, &arrow)) s.dispatch(c->id);
            if (arrow) ImGui::OpenPopup((std::string("fly_") + c->id).c_str());
            flyoutPopup(s, c->id);
        }
        ImGui::EndGroup();

        // group caption, centered under the group
        ImVec2 gmin = ImGui::GetItemRectMin(), gmax = ImGui::GetItemRectMax();
        const char* gl = cmd::groupLabel(g);
        ImFont* f = theme::font(theme::Font::Body);
        float fs = 11.f * S;
        ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, gl);
        float cx = (gmin.x + gmax.x) * 0.5f - ts.x * 0.5f;
        dl->AddText(f, fs, ImVec2(cx, gmin.y + btn + pad*0.4f), TCOL(txt2), gl);
    }

    // bottom hairline
    ImVec2 wp = ImGui::GetWindowPos(); ImVec2 wsz = ImGui::GetWindowSize();
    dl->AddLine(ImVec2(wp.x, wp.y+wsz.y-0.5f), ImVec2(wp.x+wsz.x, wp.y+wsz.y-0.5f), TCOL(line), 1.f);

    ImGui::EndChild();
    ImGui::PopStyleColor();
}

} // namespace ui
