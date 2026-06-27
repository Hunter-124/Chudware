#include "ui/panels.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <GLFW/glfw3.h>
#include <string>

namespace ui {

using app::AppState;

static void appMenu(AppState& s) {
    if (ImGui::BeginPopup("##appmenu")) {
        if (ImGui::BeginMenu("File")) {
            if (ImGui::BeginMenu("New")) {
                if (ImGui::MenuItem("Part"))      s.doNewPart();
                if (ImGui::MenuItem("Assembly"))  { s.docs.push_back({"Assem"+std::to_string(s.docs.size()+1), cmd::DocType::Assembly}); s.activeDoc=(int)s.docs.size()-1; s.workspace=cmd::Workspace::Assemble; }
                if (ImGui::MenuItem("Drawing"))   s.dispatch("make_drawing");
                if (ImGui::MenuItem("Schematic")) s.dispatch("ecad_new_sch");
                if (ImGui::MenuItem("PCB"))       s.dispatch("ecad_new_pcb");
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Open...", "Ctrl+O"))        s.doOpen();
            if (ImGui::MenuItem("Save", "Ctrl+S"))           s.doSave(false);
            if (ImGui::MenuItem("Save As...", "Ctrl+Shift+S")) s.doSave(true);
            ImGui::Separator();
            if (ImGui::MenuItem("Quit")) glfwSetWindowShouldClose(s.window, 1);
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("Edit")) {
            ImGui::MenuItem("Undo"); ImGui::MenuItem("Redo"); ImGui::Separator();
            ImGui::MenuItem("Cut"); ImGui::MenuItem("Copy"); ImGui::MenuItem("Paste");
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("View")) {
            if (ImGui::MenuItem(s.dark ? "Light theme" : "Dark theme")) { s.dark = !s.dark; theme::setDark(s.dark); icons::setAtlasTheme(s.dark); }
            ImGui::MenuItem("Reset Layout");
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("Insert")) {
            for (const cmd::Command* c : cmd::topCommands(cmd::Workspace::Model))
                if (c->group == cmd::Group::Create || c->group == cmd::Group::Construct)
                    if (ImGui::MenuItem(c->label)) s.dispatch(c->id);
            ImGui::EndMenu();
        }
        if (ImGui::BeginMenu("Help")) { ImGui::MenuItem("Documentation"); ImGui::MenuItem("About AI-CAD"); ImGui::EndMenu(); }
        ImGui::EndPopup();
    }
}

void drawTitleBar(AppState& s, float height) {
    const float S = theme::S();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg0));
    ImGui::BeginChild("##title", ImVec2(0, height), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 wp = ImGui::GetWindowPos(), wsz = ImGui::GetWindowSize();

    float pad = 8.f * S;
    float btnH = height - 10.f*S;
    float capW = height * 1.2f;
    bool maxed = glfwGetWindowAttrib(s.window, GLFW_MAXIMIZED) != 0;

    // --- window drag zone: submit FIRST (full title minus the window buttons) so the
    //     menu/tabs/search drawn after it win hover and stay clickable. ---
    ImGui::SetCursorPos(ImVec2(0,0));
    ImGui::SetNextItemAllowOverlap();
    ImGui::InvisibleButton("##dragzone", ImVec2(wsz.x - capW*3, height));
    if (ImGui::IsItemActive() && !maxed) {
        ImGuiIO& io = ImGui::GetIO();
        if (io.MouseDelta.x != 0 || io.MouseDelta.y != 0) {
            int wx, wy; glfwGetWindowPos(s.window, &wx, &wy);
            glfwSetWindowPos(s.window, wx + (int)io.MouseDelta.x, wy + (int)io.MouseDelta.y);
        }
    }
    if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(0)) {
        if (maxed) glfwRestoreWindow(s.window); else glfwMaximizeWindow(s.window);
    }

    ImGui::SetCursorPos(ImVec2(pad, (height - btnH)*0.5f));

    // hamburger menu
    if (ImGui::Button("###menu", ImVec2(btnH, btnH))) ImGui::OpenPopup("##appmenu");
    { ImVec2 mp = ImGui::GetItemRectMin(); ImVec2 c(mp.x+btnH*0.5f, mp.y+btnH*0.5f);
      icons::draw(dl, "menu", c, btnH*0.5f, TCOL(txt1)); }
    appMenu(s);

    // wordmark
    ImGui::SameLine(0, 10*S);
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::SetCursorPosY((height - ImGui::GetFontSize())*0.5f);
    ImGui::TextColored(TVEC(txt0), "AI-CAD");
    ImGui::PopFont();
    ImGui::SameLine(0, 6*S);
    ImGui::SetCursorPosY((height - ImGui::GetTextLineHeight())*0.5f);
    ImGui::TextColored(TVEC(accent), "2");

    // doc tabs
    ImGui::SameLine(0, 16*S);
    for (int i = 0; i < (int)s.docs.size(); ++i) {
        ImGui::PushID(i);
        bool act = (i == s.activeDoc);
        ImVec2 tp = ImGui::GetCursorScreenPos();
        std::string lbl = s.docs[i].name;
        ImVec2 ts = ImGui::CalcTextSize(lbl.c_str());
        float tw = ts.x + 26*S;
        ImGui::SetCursorPosY((height - (height-12*S))*0.5f);
        if (ImGui::InvisibleButton("tab", ImVec2(tw, height-12*S))) s.activeDoc = i;
        bool th = ImGui::IsItemHovered();
        ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
        dl->AddRectFilled(a, b, act ? TCOL(bg2) : (th?TCOL(hover):0u), 7*S);
        if (act) dl->AddRectFilled(ImVec2(a.x, b.y-2*S), b, TCOL(accent), 0);
        dl->AddText(ImVec2(a.x+10*S, (a.y+b.y)*0.5f - ts.y*0.5f), act?TCOL(txt0):TCOL(txt1), lbl.c_str());
        ImGui::PopID();
        ImGui::SameLine(0, 4*S);
    }
    // new-doc +
    if (ImGui::Button("+", ImVec2(height-14*S, height-14*S))) ImGui::OpenPopup("##appmenu");

    // ----- right cluster: window buttons -----
    float rightX = wsz.x - capW*3;
    // search pill (left of window buttons)
    float searchW = 220*S;
    float searchX = rightX - searchW - 12*S;
    if (searchX > ImGui::GetCursorPosX()) {
        ImGui::SetCursorPos(ImVec2(searchX, (height-btnH)*0.5f));
        if (ImGui::Button("##search", ImVec2(searchW, btnH))) s.paletteOpen = true;
        ImVec2 sp = ImGui::GetItemRectMin();
        icons::draw(dl, "search", ImVec2(sp.x+btnH*0.5f, sp.y+btnH*0.5f), btnH*0.42f, TCOL(txt2));
        dl->AddText(ImVec2(sp.x+btnH, sp.y+btnH*0.5f-ImGui::GetTextLineHeight()*0.5f), TCOL(txt2), "Search commands");
        dl->AddText(theme::font(theme::Font::Mono), 12*S, ImVec2(sp.x+searchW-36*S, sp.y+btnH*0.5f-7*S), TCOL(txt2), "Ctrl K");
    }

    // window controls
    auto capBtn = [&](const char* id, const char* icon, ImU32 hoverCol)->bool {
        bool clicked = ImGui::Button(id, ImVec2(capW, height));
        ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
        if (ImGui::IsItemHovered()) dl->AddRectFilled(a, b, hoverCol, 0);
        icons::draw(dl, icon, ImVec2((a.x+b.x)*0.5f,(a.y+b.y)*0.5f), height*0.34f, TCOL(txt1));
        return clicked;
    };
    ImGui::SetCursorPos(ImVec2(wsz.x - capW*3, 0));
    if (capBtn("##min", "win_min", TCOL(hover)))  glfwIconifyWindow(s.window);
    ImGui::SameLine(0,0);
    if (capBtn("##max", maxed?"win_restore":"win_max", TCOL(hover))) { if (maxed) glfwRestoreWindow(s.window); else glfwMaximizeWindow(s.window); }
    ImGui::SameLine(0,0);
    if (capBtn("##close","win_close", TCOL(bad))) glfwSetWindowShouldClose(s.window, 1);

    dl->AddLine(ImVec2(wp.x, wp.y+wsz.y-0.5f), ImVec2(wp.x+wsz.x, wp.y+wsz.y-0.5f), TCOL(line), 1.f);
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

} // namespace ui
