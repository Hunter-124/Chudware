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
        if (act || hov)
            theme::glassRowBackground(dl, p, ImVec2(p.x+btn, p.y+btn), 8*S, false, act);
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
    float availW = ImGui::GetContentRegionAvail().x;
    ImVec2 rowMax(p.x + availW, p.y + rowH);
    theme::glassRowBackground(ImGui::GetWindowDrawList(), p, rowMax, 3.5f*S, depth % 2 == 0, sel);

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
    const char* titles[] = {"Model Tree", "Bodies", "Files", "Configurations"};
    theme::glassRowAtCursor(4.f, false, false, 6.f);
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(TVEC(txt1), "%s", titles[s.railTab]);
    ImGui::PopFont();
    ImGui::Separator();

    if (s.railTab == 0) {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0,0));
        drawNode(s, s.tree, 0);
        ImGui::PopStyleVar();
    } else if (s.railTab == 1) {
        for (auto& b : s.bodies) {
            theme::glassRowAtCursor(3.5f, false, false, 5.f);
            icons::inlineIcon("file_part", 15*S, TCOL(txt1)); ImGui::SameLine(0,6*S);
            ImGui::TextColored(TVEC(txt0), "%s", b.c_str());
        }
    } else if (s.railTab == 2) {
        ImGui::TextColored(TVEC(txt2), "Workspace files");
        const char* demo[] = {"Bracket.step+", "Housing.step+", "Plate.dxf"};
        for (auto f : demo) {
            theme::glassRowAtCursor(3.5f, false, false, 5.f);
            icons::inlineIcon("file_part", 15*S, TCOL(txt1)); ImGui::SameLine(0,6*S);
            ImGui::TextColored(TVEC(txt1), "%s", f);
        }
    } else {
        theme::glassRowAtCursor(3.5f, false, false, 5.f);
        icons::inlineIcon("rail_configs", 15*S, TCOL(accent)); ImGui::SameLine(0,6*S);
        ImGui::TextColored(TVEC(txt0), "Default <active>");
    }
}

// ---------------- Appearance editor (color / material / finish / display) ----------------
namespace {
struct MatPreset { const char* name; float col[3]; float metal; float rough; };
// PBR swatches: metals (metallic=1) + plastics/finishes (dielectric). "Default" == kernel grey.
static const MatPreset kPresets[] = {
    { "Default",   {0.71f,0.76f,0.82f}, 0.0f, 0.50f },
    { "Aluminum",  {0.91f,0.92f,0.94f}, 1.0f, 0.35f },
    { "Steel",     {0.56f,0.57f,0.58f}, 1.0f, 0.45f },
    { "Stainless", {0.69f,0.70f,0.72f}, 1.0f, 0.28f },
    { "Titanium",  {0.62f,0.61f,0.60f}, 1.0f, 0.45f },
    { "Brass",     {0.88f,0.73f,0.40f}, 1.0f, 0.32f },
    { "Copper",    {0.95f,0.64f,0.54f}, 1.0f, 0.30f },
    { "Gold",      {1.00f,0.78f,0.34f}, 1.0f, 0.25f },
    { "Chrome",    {0.78f,0.80f,0.82f}, 1.0f, 0.10f },
    { "Plastic",   {0.90f,0.91f,0.93f}, 0.0f, 0.45f },
    { "ABS Black", {0.09f,0.10f,0.11f}, 0.0f, 0.55f },
    { "PLA Ivory", {0.87f,0.85f,0.78f}, 0.0f, 0.60f },
    { "Rubber",    {0.08f,0.08f,0.09f}, 0.0f, 0.92f },
    { "Glass",     {0.82f,0.90f,0.94f}, 0.0f, 0.06f },
};

// Persist the form via the engine set_appearance op (round-trips into .step+, re-bakes the AICM).
// clear=true sends color:null to drop the override back to the default grey.
static void appearancePersist(AppState& s, bool clear) {
    if (!s.engineReady) { s.status = clear ? "Appearance reset (demo)" : "Appearance applied (demo)"; return; }
    nlohmann::json ops = nlohmann::json::array();
    auto addOp = [&](uint32_t bid){
        nlohmann::json op = { {"op","set_appearance"}, {"id", std::string("b") + std::to_string(bid)} };
        if (clear) op["color"] = nullptr;
        else { op["color"] = { s.apColor[0], s.apColor[1], s.apColor[2] };
               op["metallic"] = s.apMetal; op["roughness"] = s.apRough; }
        ops.push_back(op);
    };
    if (s.apBody) addOp(s.apBody);
    else if (s.scene) for (int i = 0; i < s.scene->bodyCount(); ++i) addOp(s.scene->bodyIdAt(i));
    if (ops.empty()) { s.status = "No bodies to shade"; return; }
    eng::OpsResult r = s.engine.apply(ops);
    if (r.ok) { s.buildTreeFromDoc();   // viewMeshRev=-1 -> viewport reloads the re-baked AICM
                s.status = std::string(clear ? "Appearance reset" : "Appearance applied")
                         + "  (rev " + std::to_string(s.engine.revision()) + ")"; }
    else s.status = std::string("Engine error: ") + (r.errMsg.empty() ? s.engine.lastError() : r.errMsg);
}

static void drawAppearanceBody(AppState& s) {
    const float S = theme::S();
    int nb = s.scene ? s.scene->bodyCount() : 0;

    // Pull the target body's current look into the form when the editor opens / target changes.
    if (s.apNeedsInit) {
        uint32_t sample = s.apBody ? s.apBody : (nb > 0 && s.scene ? s.scene->bodyIdAt(0) : 0);
        float c[3]; float m, r;
        if (s.scene && sample && s.scene->getBodyAppearance(sample, c, m, r)) {
            s.apColor[0]=c[0]; s.apColor[1]=c[1]; s.apColor[2]=c[2]; s.apMetal=m; s.apRough=r;
        }
        s.apNeedsInit = false;
    }

    auto labelCol = [&](const char* t){ ImGui::TextColored(TVEC(txt1), "%s", t); ImGui::SameLine(96*S); ImGui::SetNextItemWidth(-FLT_MIN); };

    // Target body
    labelCol("Apply to");
    {
        std::string cur = s.apBody ? (std::string("Body ") + std::to_string(s.apBody)) : std::string("All Bodies");
        if (ImGui::BeginCombo("##aptarget", cur.c_str())) {
            if (ImGui::Selectable("All Bodies", s.apBody==0)) { s.apBody=0; s.apNeedsInit=true; }
            for (int i=0;i<nb;i++){ uint32_t bid = s.scene->bodyIdAt(i);
                std::string nm = std::string("Body ") + std::to_string(bid);
                if (ImGui::Selectable(nm.c_str(), s.apBody==bid)) { s.apBody=bid; s.apNeedsInit=true; }
            }
            ImGui::EndCombo();
        }
    }

    // Material preset swatches
    ImGui::Dummy(ImVec2(0,3*S));
    ImGui::TextColored(TVEC(txt1), "Material");
    {
        const float sw = 32*S;
        float avail = ImGui::GetContentRegionAvail().x;
        int perRow = ImMax(1, (int)(avail / (sw + 6*S)));
        int n = (int)(sizeof(kPresets)/sizeof(kPresets[0]));
        for (int i=0;i<n;i++){
            const MatPreset& mp = kPresets[i];
            if (i % perRow != 0) ImGui::SameLine(0, 6*S);
            ImGui::PushID(i);
            ImVec4 col(mp.col[0], mp.col[1], mp.col[2], 1.0f);
            ImGui::PushStyleColor(ImGuiCol_Button, col);
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, col);
            ImGui::PushStyleColor(ImGuiCol_ButtonActive, col);
            if (ImGui::Button("##sw", ImVec2(sw, sw))) {
                s.apColor[0]=mp.col[0]; s.apColor[1]=mp.col[1]; s.apColor[2]=mp.col[2];
                s.apMetal=mp.metal; s.apRough=mp.rough;
            }
            ImGui::PopStyleColor(3);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", mp.name);
            ImGui::PopID();
        }
    }

    // Color + RealView finish
    ImGui::Dummy(ImVec2(0,4*S));
    labelCol("Color");     ImGui::ColorEdit3("##apcol", s.apColor,
                              ImGuiColorEditFlags_NoInputs | ImGuiColorEditFlags_NoLabel | ImGuiColorEditFlags_PickerHueWheel);
    labelCol("Metallic");  ImGui::SliderFloat("##apmet", &s.apMetal, 0.0f, 1.0f, "%.2f");
    labelCol("Roughness"); ImGui::SliderFloat("##aprou", &s.apRough, 0.0f, 1.0f, "%.2f");

    // Display style (model display options)
    ImGui::Dummy(ImVec2(0,2*S));
    labelCol("Display");
    { const char* styles[] = {"Shaded","Shaded + Edges","Wireframe"};
      if (ImGui::Combo("##apstyle", &s.viewStyle, styles, 3) && s.scene) s.scene->setStyle(s.viewStyle); }

    // Live preview — cheap viewport override (no re-tessellation) while the form is open.
    if (s.scene) s.scene->setBodyAppearance(s.apBody, s.apColor, s.apMetal, s.apRough);

    ImGui::Dummy(ImVec2(0,4*S));
    ImGui::Separator();
    float bw = 64*S;
    if (ImGui::Button("OK", ImVec2(bw,0)))    { appearancePersist(s, false); s.activeTool.clear(); }
    ImGui::SameLine();
    if (ImGui::Button("Apply", ImVec2(bw,0))) { appearancePersist(s, false); }
    ImGui::SameLine();
    if (ImGui::Button("Reset", ImVec2(bw,0))) {
        s.apColor[0]=0.71f; s.apColor[1]=0.76f; s.apColor[2]=0.82f; s.apMetal=0.f; s.apRough=0.5f;
        appearancePersist(s, true);
    }
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(bw,0))) {
        // revert the live preview to the last-baked appearance
        if (s.scene && s.engineReady && !s.engine.aicmPath().empty()) s.scene->loadAicm(s.engine.aicmPath());
        s.status = "Ready"; s.activeTool.clear();
    }
}
} // namespace

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

    // The Appearance editor is a full panel of its own (presets, color, finish, display).
    if (s.activeTool == "appearance_edit") {
        drawAppearanceBody(s);
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 8*S));
        return;
    }

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
        if (!s.toolExtrudeOp.empty()) {
            if (s.toolExtrudeOp == "Cut") exOpi = 2;
            else if (s.toolExtrudeOp == "Add") exOpi = 1;
            else if (s.toolExtrudeOp == "Intersect") exOpi = 3;
            else exOpi = 0;
            s.toolExtrudeOp.clear();
        }
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
