#include "ui/panels.h"
#include "ui/command_registry.h"
#include "ui/file_explorer.h"
#include "ui/feature_tree.h"
#include "ui/property_manager.h"
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
    // A1: the rail is a distinct visual pane from the Browser dock (it sits beside the dockspace,
    // never inside "Browser###left"), so it gets its own panelId rather than sharing "browser_glass"
    // — adaptivePanel's per-rect luminance sampling would otherwise be driven by whichever pane
    // calls it first in a frame (A2 idempotency), corrupting the Browser pane's own sample.
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect railRect(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    const theme::AdaptiveSurface& surf = theme::adaptivePanel(theme::panelId("rail_glass"), railRect);
    const bool railLive = theme::viewportBackdrop().valid();
    const ImU32 railHalo = railLive
        ? ImGui::ColorConvertFloat4ToU32(ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0x50 / 255.f))
        : 0;
    for (int i = 0; i < 4; ++i) {
        ImGui::PushID(i);
        ImGui::SetCursorPosX(6*S);
        ImVec2 p = ImGui::GetCursorScreenPos();
        bool act = (s.railTab == i);
        if (ImGui::InvisibleButton("r", ImVec2(btn, btn))) s.railTab = i;
        bool hov = ImGui::IsItemHovered();
        const float wash = act ? theme::rowWash::selected : (hov ? theme::rowWash::hover : theme::rowWash::idle);
        theme::glassRowBackground(dl, p, ImVec2(p.x+btn, p.y+btn), 8*S, false, act, wash);
        if (act) dl->AddRectFilled(ImVec2(p.x-6*S, p.y+btn*0.2f), ImVec2(p.x-6*S+3*S, p.y+btn*0.8f), TCOL(accent), 2*S);
        ImU32 iconCol = ImGui::ColorConvertFloat4ToU32(act ? surf.textPrimary : surf.textSecondary);
        icons::drawGlass(dl, rails[i].icon, ImVec2(p.x+btn*0.5f, p.y+btn*0.5f), btn*0.5f, iconCol, railHalo);
        if (hov) ImGui::SetTooltip("%s", rails[i].tip);
        ImGui::SetCursorPosY(ImGui::GetCursorPosY() + 6*S);
        ImGui::PopID();
    }
}

// ---------------- Model tree (FeatureManager body) ----------------

void drawLeftDock(AppState& s) {
    const float S = theme::S();
    const char* titles[] = {"Model Tree", "Bodies", "Files", "Configurations"};

    // A1: paint the ONE UltraThin glass surface for the whole Browser pane, ONCE, before any
    // rail/tree/explorer content; feature_tree.cpp / file_explorer.cpp fetch this SAME surface via
    // the SAME panelId (A2 idempotency makes their fetch a cached lookup, not a re-sample) instead
    // of painting their own background.
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect paneRect(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    const theme::AdaptiveSurface& surf = theme::adaptivePanel(theme::panelId("browser_glass"), paneRect);
    theme::glassPanel(ImGui::GetWindowDrawList(), paneRect, theme::MaterialLevel::UltraThin, -1.f, surf);

    theme::glassRowAtCursor(4.f, false, false, 6.f);
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(surf.textPrimary, "%s", titles[s.railTab]);
    ImGui::PopFont();
    ImGui::Separator();

    if (s.railTab == 0) {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 0));
        drawFeatureTreeBody(s);
        ImGui::PopStyleVar();
    } else if (s.railTab == 1) {
        for (auto& b : s.bodies) {
            theme::glassRowAtCursor(3.5f, false, false, 5.f);
            icons::inlineIcon("file_part", 15*S, TCOL(txt1)); ImGui::SameLine(0,6*S);
            ImGui::TextColored(surf.textPrimary, "%s", b.c_str());
        }
    } else if (s.railTab == kFilesRailTab) {
        drawFileExplorerBody(s);
    } else {
        theme::glassRowAtCursor(3.5f, false, false, 5.f);
        icons::inlineIcon("rail_configs", 15*S, TCOL(accent)); ImGui::SameLine(0,6*S);
        ImGui::TextColored(surf.textPrimary, "Default <active>");
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

// ---------------- Inspector (Property Manager) ----------------
void drawInspector(AppState& s) {
    const float S = theme::S();
    PropertyManager& pm = propertyManager();
    static bool shotEnvDone = false;
    if (!shotEnvDone) { pmApplyShotEnv(s); shotEnvDone = true; }

    if (s.activeTool.empty()) {
        if (pm.active()) pm.closeIfActive();
        return;
    }

    // Appearance editor keeps its bespoke panel (not in panel_spec).
    if (s.activeTool == "appearance_edit") {
        const cmd::Command* c = cmd::find(s.activeTool.c_str());
        std::string label = c ? c->label : s.activeTool;
        ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg2));
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 8*S);
        ImGui::BeginChild("##inspector", ImVec2(0, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
        icons::inlineIcon("settings", 18*S, TCOL(accent)); ImGui::SameLine(0,8*S);
        ImGui::PushFont(theme::font(theme::Font::H2));
        ImGui::TextColored(TVEC(txt0), "%s", label.c_str());
        ImGui::PopFont();
        ImGui::Separator();
        drawAppearanceBody(s);
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 8*S));
        return;
    }

    static std::string lastTool;
    if (s.activeTool != lastTool) {
        if (!pm.openForCommand(s, s.activeTool))
            lastTool.clear();
        else
            lastTool = s.activeTool;
    }

    if (!pm.active()) {
        ImGui::TextColored(TVEC(txt2), "No property form for '%s'.", s.activeTool.c_str());
        return;
    }

    pm.tick(s);

    // mat.regular glass card per GLASS_DESIGN_SPEC §6 (Property Manager).
    ImVec2 cardMin = ImGui::GetCursorScreenPos();
    float cardW = ImGui::GetContentRegionAvail().x;
    float estH = 420.f * S;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("browser_glass"),
                             ImRect(cardMin, ImVec2(cardMin.x + cardW, cardMin.y + estH)));
    theme::glassPanel(dl, ImRect(cardMin, ImVec2(cardMin.x + cardW, cardMin.y + estH)),
                      theme::MaterialLevel::Regular, theme::U(1.25f), surf);
    theme::pushGlassChildStyle(theme::U(1.25f));
    ImGui::BeginChild("##inspector", ImVec2(0, 0), ImGuiChildFlags_AutoResizeY);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);  // ambient PM text adapts
    pm.drawBody(s);
    ImGui::PopStyleColor();
    ImGui::EndChild();
    theme::popGlassChildStyle();
    if (!pm.active()) {
        s.activeTool.clear();
        lastTool.clear();
        s.status = "Ready";
    }
    ImGui::Dummy(ImVec2(0, 8*S));
}

} // namespace ui
