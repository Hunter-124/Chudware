#include "ui/panels.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <string>

namespace ui {

using namespace app;

// ---------------- Activity rail ----------------
void drawActivityRail(AppState& s, float width) {
    const float S = theme::S();
    struct R { const char* icon; const char* tip; };
    static const R rails[] = {
        {"rail_tree", "Model Tree"}, {"rail_bodies", "Bodies"},
        {"rail_files", "Files"}, {"rail_configs", "Configurations"},
    };
    float btn = width - 12*S;
    ImGui::SetCursorPosY(8*S);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    for (int i = 0; i < 4; ++i) {
        ImGui::PushID(i);
        ImGui::SetCursorPosX(6*S);
        ImVec2 p = ImGui::GetCursorScreenPos();
        bool act = (s.railTab == i);
        if (ImGui::InvisibleButton("r", ImVec2(btn, btn))) s.railTab = i;
        bool hov = ImGui::IsItemHovered();
        if (act) dl->AddRectFilled(p, ImVec2(p.x+btn, p.y+btn), TCOL(bg2), 8*S);
        else if (hov) dl->AddRectFilled(p, ImVec2(p.x+btn, p.y+btn), TCOL(hover), 8*S);
        if (act) dl->AddRectFilled(ImVec2(p.x-6*S, p.y+btn*0.2f), ImVec2(p.x-6*S+3*S, p.y+btn*0.8f), TCOL(accent), 2*S);
        icons::draw(dl, rails[i].icon, ImVec2(p.x+btn*0.5f, p.y+btn*0.5f), btn*0.5f, act?TCOL(txt0):TCOL(txt1));
        if (hov) ImGui::SetTooltip("%s", rails[i].tip);
        ImGui::SetCursorPosY(ImGui::GetCursorPosY() + 6*S);
        ImGui::PopID();
    }
}

// ---------------- Model tree ----------------
static void drawNode(AppState& s, TreeNode& n, int depth) {
    const float S = theme::S();
    ImGui::PushID(&n);
    bool hasKids = !n.children.empty();
    float rowH = ImGui::GetTextLineHeight() + 7*S;
    float indent = 8*S + depth * 16*S;
    ImVec2 p = ImGui::GetCursorScreenPos();
    bool sel = (s.selectedFeature == n.name && depth > 0);

    if (ImGui::Selectable("##row", sel, ImGuiSelectableFlags_SpanAllColumns, ImVec2(0, rowH))) {
        float mx = ImGui::GetIO().MousePos.x - p.x;
        if (hasKids && mx >= indent - 4*S && mx < indent + rowH) n.open = !n.open;
        else if (depth > 0) s.selectedFeature = n.name;
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    float cy = p.y + rowH*0.5f;
    if (hasKids) {
        float ax = p.x + indent, t = 3.2f*S;
        ImVec2 c(ax + 4*S, cy);
        if (n.open) dl->AddTriangleFilled(ImVec2(c.x-t,c.y-t), ImVec2(c.x+t,c.y-t), ImVec2(c.x,c.y+t), TCOL(txt2));
        else        dl->AddTriangleFilled(ImVec2(c.x-t,c.y-t), ImVec2(c.x+t,c.y), ImVec2(c.x-t,c.y+t), TCOL(txt2));
    }
    float ix = p.x + indent + 16*S;
    icons::draw(dl, n.icon.c_str(), ImVec2(ix + 7*S, cy), 15*S, sel?TCOL(accent):TCOL(txt1));
    dl->AddText(ImVec2(ix + 20*S, cy - ImGui::GetTextLineHeight()*0.5f),
                sel?TCOL(txt0):TCOL(txt0), n.name.c_str());

    if (n.open) for (auto& ch : n.children) drawNode(s, ch, depth+1);
    ImGui::PopID();
}

void drawLeftDock(AppState& s) {
    const float S = theme::S();
    ImGui::PushFont(theme::font(theme::Font::H2));
    const char* titles[] = {"Model Tree", "Bodies", "Files", "Configurations"};
    ImGui::TextColored(TVEC(txt1), "%s", titles[s.railTab]);
    ImGui::PopFont();
    ImGui::Separator();

    if (s.railTab == 0) {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0,0));
        drawNode(s, s.tree, 0);
        ImGui::PopStyleVar();
    } else if (s.railTab == 1) {
        for (auto& b : s.bodies) {
            icons::inlineIcon("file_part", 15*S, TCOL(txt1)); ImGui::SameLine(0,6*S);
            ImGui::TextColored(TVEC(txt0), "%s", b.c_str());
        }
    } else if (s.railTab == 2) {
        ImGui::TextColored(TVEC(txt2), "Workspace files");
        const char* demo[] = {"Bracket.step+", "Housing.step+", "Plate.dxf"};
        for (auto f : demo) {
            icons::inlineIcon("file_part", 15*S, TCOL(txt1)); ImGui::SameLine(0,6*S);
            ImGui::TextColored(TVEC(txt1), "%s", f);
        }
    } else {
        icons::inlineIcon("rail_configs", 15*S, TCOL(accent)); ImGui::SameLine(0,6*S);
        ImGui::TextColored(TVEC(txt0), "Default <active>");
    }
}

// ---------------- Inspector (was "PropertyManager") ----------------
void drawInspector(AppState& s) {
    const float S = theme::S();
    const cmd::Command* c = cmd::find(s.activeTool.c_str());
    std::string label = c ? c->label : s.activeTool;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg2));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 8*S);
    ImGui::BeginChild("##inspector", ImVec2(0, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);

    // header
    icons::inlineIcon(c ? c->icon : "settings", 18*S, TCOL(accent)); ImGui::SameLine(0,8*S);
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(TVEC(txt0), "%s", label.c_str());
    ImGui::PopFont();
    ImGui::Separator();

    auto labelCol = [&](const char* t){ ImGui::TextColored(TVEC(txt1), "%s", t); ImGui::SameLine(96*S); ImGui::SetNextItemWidth(-FLT_MIN); };

    static float depth = 25.f, draft = 0.f, radius = 5.f, thick = 2.f, dia = 6.f;
    static bool reverse=false, merge=true;

    if (s.activeTool == "extrude_boss") {
        const char* ops[] = {"New Body","Add","Cut","Intersect","Surface"};
        static int opi = 0;
        labelCol("Operation"); ImGui::Combo("##op", &opi, ops, 5); s.toolExtrudeOp = ops[opi];
        labelCol("End"); { static int endi=0; const char* ends[]={"Blind","Through All","Up To Next","Up To Surface"}; ImGui::Combo("##end", &endi, ends, 4); }
        labelCol("Depth (mm)"); ImGui::DragFloat("##d", &depth, 0.1f, 0.f, 1000.f, "%.2f");
        labelCol("Draft (deg)"); ImGui::DragFloat("##dr", &draft, 0.1f, -89.f, 89.f, "%.1f");
        ImGui::Checkbox("Reverse direction", &reverse); ImGui::SameLine(); ImGui::Checkbox("Merge result", &merge);
    } else if (s.activeTool == "fillet") {
        labelCol("Radius (mm)"); ImGui::DragFloat("##r", &radius, 0.1f, 0.f, 1000.f, "%.2f");
        ImGui::TextColored(TVEC(txt2), "Select edges in the viewport...");
    } else if (s.activeTool == "chamfer") {
        labelCol("Distance (mm)"); ImGui::DragFloat("##c", &radius, 0.1f, 0.f, 1000.f, "%.2f");
    } else if (s.activeTool == "shell") {
        labelCol("Thickness"); ImGui::DragFloat("##t", &thick, 0.1f, 0.f, 100.f, "%.2f");
        ImGui::TextColored(TVEC(txt2), "Select faces to remove...");
    } else if (s.activeTool == "hole_wizard") {
        labelCol("Type"); { static int hi=0; const char* h[]={"Simple","Counterbore","Countersink","Tapped"}; ImGui::Combo("##h", &hi, h, 4); }
        labelCol("Diameter"); ImGui::DragFloat("##dia", &dia, 0.1f, 0.f, 200.f, "%.2f");
        labelCol("Depth"); ImGui::DragFloat("##hd", &depth, 0.1f, 0.f, 1000.f, "%.2f");
    } else {
        ImGui::TextColored(TVEC(txt2), "Select references in the viewport, then OK.");
        labelCol("Value"); ImGui::DragFloat("##v", &radius, 0.1f);
    }

    ImGui::Dummy(ImVec2(0, 4*S));
    ImGui::Separator();
    float bw = 70*S;
    if (ImGui::Button("OK", ImVec2(bw, 0))) { s.status = label + " applied"; s.activeTool.clear(); }
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(bw, 0))) { s.status = "Ready"; s.activeTool.clear(); }

    ImGui::EndChild();
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 8*S));
}

} // namespace ui
