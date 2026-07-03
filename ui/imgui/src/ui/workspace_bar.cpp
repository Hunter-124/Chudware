#include "ui/panels.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"

namespace ui {

using app::AppState;

static bool chip(const char* id, const char* icon, const char* label, bool active, bool primary) {
    const float S = theme::S();
    ImGui::PushID(id);
    ImFont* f = theme::font(theme::Font::Label);
    ImVec2 ts = f->CalcTextSizeA(f->FontSize, FLT_MAX, 0, label);
    float h = 24*S, icSz = 15*S;
    float w = icSz + 8*S + ts.x + 20*S;
    ImVec2 p = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton("c", ImVec2(w, h));
    bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 bg = active ? TCOL(accent) : hov ? TCOL(hover) : 0u;
    if (bg) dl->AddRectFilled(p, ImVec2(p.x+w, p.y+h), bg, h*0.5f);
    ImU32 fg = active ? TCOL(onAccent) : primary ? TCOL(txt0) : TCOL(txt1);
    icons::draw(dl, icon, ImVec2(p.x+10*S+icSz*0.5f, p.y+h*0.5f), icSz, fg);
    dl->AddText(f, f->FontSize, ImVec2(p.x+10*S+icSz+6*S, p.y+h*0.5f-ts.y*0.5f), fg, label);
    ImGui::PopID();
    ImGui::SameLine(0, 6*S);
    return clicked;
}

void drawWorkspaceBar(AppState& s, float height) {
    const float S = theme::S();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg1));
    ImGui::BeginChild("##wsbar", ImVec2(0, height), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImGui::SetCursorPos(ImVec2(12*S, (height-24*S)*0.5f));

    if (s.inSketch) {
        chip("sk", "ws_sketch", "Sketch1", true, true);
        if (chip("skx", "ok_check", "Exit Sketch", false, true)) s.dispatch("sketch_exit");
    } else {
        auto wss = cmd::workspacesFor(s.docType());
        for (size_t i = 0; i < wss.size(); ++i) {
            cmd::Workspace w = wss[i];
            bool active = (s.workspace == w);
            bool primary = (i == 0);   // first workspace is the primary one
            if (chip(cmd::workspaceLabel(w), cmd::workspaceIcon(w), cmd::workspaceLabel(w), active, primary))
                { s.workspace = w; s.activeTool.clear(); s.camActive = false; }
            if (i == 0 && wss.size() > 1) {
                // a faint divider between the primary workspace and the specialized workbenches
                ImVec2 sp = ImGui::GetCursorScreenPos();
                dl->AddLine(ImVec2(sp.x, sp.y+4*S), ImVec2(sp.x, sp.y+height-8*S), TCOL(line), ImMax(1.f,S));
                ImGui::SameLine(0, 10*S);
            }
        }
    }

    ImVec2 wp = ImGui::GetWindowPos(), wsz = ImGui::GetWindowSize();
    dl->AddLine(ImVec2(wp.x, wp.y+wsz.y-0.5f), ImVec2(wp.x+wsz.x, wp.y+wsz.y-0.5f),
                theme::roleU32A(theme::pal().line, 0.35f), 1.f);
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

} // namespace ui
