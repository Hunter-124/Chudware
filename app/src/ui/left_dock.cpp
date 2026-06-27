#include "ui/panels.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "viewport/scene.h"
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
    static float pbL=40, pbW=30, pbH=15, pcR=15, pcH=30, psR=20, pkR1=15, pkR2=0, pkH=25;
    static int   exProfi=0, exPlani=1, exOpi=0; static float exW=40, exH=30, exR=15;  // extrude
    static bool reverse=false, merge=true;

    if (s.activeTool == "prim_box") {
        labelCol("Length"); ImGui::DragFloat("##pbl",&pbL,0.1f,0.01f,1e5f,"%.2f");
        labelCol("Width");  ImGui::DragFloat("##pbw",&pbW,0.1f,0.01f,1e5f,"%.2f");
        labelCol("Height"); ImGui::DragFloat("##pbh",&pbH,0.1f,0.01f,1e5f,"%.2f");
    } else if (s.activeTool == "prim_cylinder") {
        labelCol("Radius"); ImGui::DragFloat("##pcr",&pcR,0.1f,0.01f,1e5f,"%.2f");
        labelCol("Height"); ImGui::DragFloat("##pch",&pcH,0.1f,0.01f,1e5f,"%.2f");
    } else if (s.activeTool == "prim_sphere") {
        labelCol("Radius"); ImGui::DragFloat("##psr",&psR,0.1f,0.01f,1e5f,"%.2f");
    } else if (s.activeTool == "prim_cone") {
        labelCol("Bottom R"); ImGui::DragFloat("##pk1",&pkR1,0.1f,0.f,1e5f,"%.2f");
        labelCol("Top R");    ImGui::DragFloat("##pk2",&pkR2,0.1f,0.f,1e5f,"%.2f");
        labelCol("Height");   ImGui::DragFloat("##pkh",&pkH,0.1f,0.01f,1e5f,"%.2f");
    } else if (s.activeTool == "extrude_boss") {
        const char* ops[] = {"New Body","Add","Cut","Intersect"};
        labelCol("Operation"); ImGui::Combo("##op", &exOpi, ops, 4);
        const char* profs[] = {"Rectangle","Circle"};
        labelCol("Profile"); ImGui::Combo("##prof", &exProfi, profs, 2);
        const char* planes[] = {"Front","Top","Right"};
        labelCol("Plane"); ImGui::Combo("##pl", &exPlani, planes, 3);
        if (exProfi == 0) { labelCol("Width");  ImGui::DragFloat("##exw",&exW,0.1f,0.01f,1e5f,"%.2f");
                            labelCol("Height"); ImGui::DragFloat("##exh",&exH,0.1f,0.01f,1e5f,"%.2f"); }
        else              { labelCol("Radius"); ImGui::DragFloat("##exr",&exR,0.1f,0.01f,1e5f,"%.2f"); }
        labelCol("Depth (mm)"); ImGui::DragFloat("##d", &depth, 0.1f, 0.f, 1e5f, "%.2f");
        ImGui::TextColored(TVEC(txt2), "Sketches the profile and extrudes it.");
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
    if (ImGui::Button("OK", ImVec2(bw, 0))) {
        std::string tool = s.activeTool;
        if (tool.rfind("prim_", 0) == 0) {
            if (s.engineReady) {
                eng::OpsResult r;
                if (tool == "prim_box")           r = s.engine.addBox(pbL, pbW, pbH);
                else if (tool == "prim_cylinder") r = s.engine.addCylinder(pcR, pcH);
                else if (tool == "prim_sphere")   r = s.engine.addSphere(psR);
                else                              r = s.engine.addCone(pkR1, pkR2, pkH);
                if (r.ok) { s.buildTreeFromDoc(); s.status = label + " added (rev " + std::to_string(s.engine.revision()) + ")"; }
                else s.status = std::string("Engine error: ") + (r.errMsg.empty() ? s.engine.lastError() : r.errMsg);
            } else s.status = label + " added (demo)";
        } else if (tool == "shell" && s.scene && !s.scene->selection().empty() && s.engineReady) {
            // shell, removing the faces selected in the viewport
            nlohmann::json refs = nlohmann::json::array();
            for (const auto& pr : s.scene->selection()) {
                nlohmann::json sel = s.engine.resolvePick(pr.body_id, "face", pr.topo_id);
                if (!sel.is_null()) refs.push_back(sel);
            }
            if (refs.empty()) s.status = "Could not resolve the selected faces";
            else {
                eng::OpsResult r = s.engine.addShellFaces(thick, refs);
                if (r.ok) { s.scene->clearSelection(); s.selCount = 0; s.buildTreeFromDoc();
                            s.status = "Shell - removed " + std::to_string((int)refs.size()) + " selected face(s)"; }
                else s.status = std::string("Engine error: ") + (r.errMsg.empty() ? s.engine.lastError() : r.errMsg);
            }
        } else if ((tool == "fillet" || tool == "chamfer") && s.scene && !s.scene->selection().empty() && s.engineReady) {
            // fillet/chamfer the edges selected in the viewport
            nlohmann::json refs = nlohmann::json::array();
            for (const auto& pr : s.scene->selection()) {
                if (pr.kind != 1) continue;  // edges only
                nlohmann::json sel = s.engine.resolvePick(pr.body_id, "edge", pr.topo_id);
                if (!sel.is_null()) refs.push_back(sel);
            }
            if (refs.empty()) s.status = "Select edges in the viewport first";
            else {
                eng::OpsResult r = (tool == "fillet") ? s.engine.addFilletEdges(radius, refs)
                                                      : s.engine.addChamferEdges(radius, refs);
                if (r.ok) { s.scene->clearSelection(); s.selCount = 0; s.buildTreeFromDoc();
                            s.status = std::string(tool == "fillet" ? "Fillet" : "Chamfer") + " - " +
                                       std::to_string((int)refs.size()) + " edge(s)"; }
                else s.status = std::string("Engine error: ") + (r.errMsg.empty() ? s.engine.lastError() : r.errMsg);
            }
        } else if (tool == "extrude_boss" && s.engineReady) {
            std::string plane = exPlani == 0 ? "plane.front" : (exPlani == 1 ? "plane.top" : "plane.right");
            std::string mode  = (exOpi == 2) ? "cut" : "boss";   // New Body/Add/Intersect -> boss; Cut -> cut
            eng::OpsResult r = (exProfi == 0) ? s.engine.sketchRectExtrude(plane, exW, exH, depth, mode)
                                              : s.engine.sketchCircleExtrude(plane, exR, depth, mode);
            if (r.ok) { s.buildTreeFromDoc(); s.status = "Extrude added (rev " + std::to_string(s.engine.revision()) + ")"; }
            else s.status = std::string("Engine error: ") + (r.errMsg.empty() ? s.engine.lastError() : r.errMsg);
        } else {
            double v1 = radius;
            if (tool == "shell") v1 = thick;
            else if (tool == "hole_wizard") v1 = dia;
            s.commitTool(tool, v1, 0);
        }
        s.activeTool.clear();
    }
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(bw, 0))) { s.status = "Ready"; s.activeTool.clear(); }

    ImGui::EndChild();
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 8*S));
}

} // namespace ui
