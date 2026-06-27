#include "theme/icons.h"
#include <glad/gl.h>
#include "imgui_internal.h"           // ImMax/ImCos/ImSin
#include <nlohmann/json.hpp>
#include "stb_image.h"
#include <cstring>
#include <cctype>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <unordered_map>
#include <string>

namespace icons {
namespace {

// ---- CAD icon atlas (reused from host/assets/icons) ----
struct Cell { ImVec2 uv0, uv1; };
struct Atlas {
    unsigned int texDark = 0, texLight = 0;
    bool dark = true;
    std::unordered_map<std::string, Cell> cells;   // base name (sans @32) -> UVs
    bool loaded = false;
    unsigned int tex() const { return dark ? texDark : texLight; }
};
Atlas g;

// IconId -> atlas cell base name, ONLY where they differ (direct matches resolve themselves).
const std::unordered_map<std::string, std::string>& aliasMap() {
    static const std::unordered_map<std::string, std::string> m = {
        {"extrude","extrude_boss"}, {"revolve","revolve_boss"}, {"hole","hole_wizard"},
        {"coil","curve_helix"}, {"pattern_linear","linear_pattern"}, {"pattern_circular","circular_pattern"},
        {"prim_box","box_feature"}, {"prim_cylinder","cylinder_feature"}, {"prim_sphere","sphere_feature"}, {"prim_cone","cone_feature"},
        {"analyze_thickness","thickness"}, {"analyze_draft","draft_analysis"}, {"analyze_curvature","curvature"},
        {"sk_line","line"}, {"sk_rect","rect_corner"}, {"sk_circle","circle"}, {"sk_arc","arc_3pt"},
        {"sk_polygon","polygon"}, {"sk_slot","slot"}, {"sk_spline","spline"}, {"sk_ellipse","ellipse"},
        {"sk_point","point_ent"}, {"sk_text","text_ent"}, {"sk_trim","trim"}, {"sk_extend","extend"},
        {"sk_offset","offset_ent"}, {"sk_convert","convert_ent"}, {"sk_construction","construction"},
        {"ws_sketch","sketch"}, {"ws_model","box_feature"}, {"ws_assemble","assembly"}, {"ws_simulate","activity"},
        {"ws_draft","section_props"}, {"ws_schematic","ai_panel"}, {"ws_board","extrude_boss"},
        {"ws_sheetmetal","shell"}, {"ws_weldment","sweep"}, {"ws_surface","boundary"},
        {"sim_static","study_static"}, {"sim_modal","study_modal"}, {"sim_flow","study_flow"},
        {"sim_thermal","study_flow"}, {"sim_fatigue","results"}, {"std_parts","smart_fasteners"},
        {"dwg_view","view_cube"}, {"dwg_projected","orientation"}, {"dwg_section","section_view"},
        {"dwg_detail","zoom_area"}, {"gdt","smart_dim"}, {"datum","point"}, {"balloon","circle"},
        {"note","text_ent"}, {"title_block","new_doc"}, {"sch_symbol","point_ent"}, {"sch_wire","line"},
        {"net_label","text_ent"}, {"power_port","construction"}, {"pcb_route","line"}, {"via","circle"},
        {"copper_zone","rect_center"}, {"autoroute","sweep"}, {"drc","interference"}, {"gerber","ref_plane"},
        {"board_3d","extrude_boss"}, {"ai_design","ai_panel"}, {"file_part","part"}, {"file_assembly","assembly"},
        {"file_drawing","new_doc"}, {"file_schematic","ai_panel"}, {"file_pcb","extrude_boss"}, {"folder","open"},
        {"rail_tree","history"}, {"rail_bodies","body_folder"}, {"rail_files","folder_new"}, {"rail_configs","settings"},
    };
    return m;
}

bool atlasLookup(const char* id, ImVec2& uv0, ImVec2& uv1) {
    if (!g.loaded || g.cells.empty()) return false;
    auto hit = g.cells.find(id);
    if (hit == g.cells.end()) {
        auto a = aliasMap().find(id);
        if (a == aliasMap().end()) return false;
        hit = g.cells.find(a->second);
        if (hit == g.cells.end()) return false;
    }
    uv0 = hit->second.uv0; uv1 = hit->second.uv1; return true;
}

unsigned int uploadAtlas(const std::string& path) {
    int w=0,h=0,n=0;
    unsigned char* px = stbi_load(path.c_str(), &w, &h, &n, 4);
    if (!px) return 0;
    unsigned int tex=0; glGenTextures(1,&tex); glBindTexture(GL_TEXTURE_2D,tex);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA8,w,h,0,GL_RGBA,GL_UNSIGNED_BYTE,px);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    stbi_image_free(px);
    return tex;
}

// ---- hand-drawn vector glyphs (chrome + agent). Returns true if it handled `name`. ----
bool drawGlyph(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col) {
    const float r = size * 0.5f;
    auto P = [&](float x, float y){ return ImVec2(c.x + x*r, c.y + y*r); };
    const float th = ImMax(1.4f, size*0.085f);
    auto is = [&](const char* n){ return std::strcmp(name, n) == 0; };

    if (is("ok_check") || is("success")) { ImVec2 p[3]={P(-0.5f,0.05f),P(-0.15f,0.42f),P(0.5f,-0.4f)}; dl->AddPolyline(p,3,col,0,th); return true; }
    if (is("cancel_x") || is("win_close") || is("failure") || is("tab_close")) {
        dl->AddLine(P(-0.4f,-0.4f),P(0.4f,0.4f),col,th); dl->AddLine(P(-0.4f,0.4f),P(0.4f,-0.4f),col,th); return true; }
    if (is("win_min")) { dl->AddLine(P(-0.42f,0.0f),P(0.42f,0.0f),col,th); return true; }
    if (is("win_max")) { dl->AddRect(P(-0.4f,-0.4f),P(0.4f,0.4f),col,1.5f,0,th); return true; }
    if (is("win_restore")) { dl->AddRect(P(-0.45f,-0.2f),P(0.2f,0.45f),col,1.5f,0,th); dl->AddRect(P(-0.2f,-0.45f),P(0.45f,0.2f),col,1.5f,0,th); return true; }
    if (is("menu")) { for (float y=-0.32f; y<=0.34f; y+=0.32f) dl->AddLine(P(-0.42f,y),P(0.42f,y),col,th); return true; }
    if (is("search") || is("rail_search")) { dl->AddCircle(P(-0.12f,-0.12f), r*0.42f, col, 0, th); dl->AddLine(P(0.2f,0.2f),P(0.45f,0.45f),col,th); return true; }
    if (is("tab_add") || is("plus")) { dl->AddLine(P(0,-0.4f),P(0,0.4f),col,th); dl->AddLine(P(-0.4f,0),P(0.4f,0),col,th); return true; }
    if (is("chevron_down") || is("expand")) { ImVec2 p[3]={P(-0.3f,-0.15f),P(0,0.2f),P(0.3f,-0.15f)}; dl->AddPolyline(p,3,col,0,th); return true; }
    if (is("agent_send")) { dl->AddLine(P(0,0.4f),P(0,-0.4f),col,th); ImVec2 a[3]={P(-0.3f,-0.1f),P(0,-0.42f),P(0.3f,-0.1f)}; dl->AddPolyline(a,3,col,0,th); return true; }
    if (is("agent_stop")) { dl->AddRectFilled(P(-0.3f,-0.3f),P(0.3f,0.3f),col,2.f); return true; }
    if (is("agent") || is("sparkles")) {
        const float tip=0.52f, w2=0.13f, nb=0.13f;
        dl->AddTriangleFilled(P(0,-tip),P(-w2,-nb),P(w2,-nb),col); dl->AddTriangleFilled(P(0,tip),P(-w2,nb),P(w2,nb),col);
        dl->AddTriangleFilled(P(-tip,0),P(-nb,-w2),P(-nb,w2),col); dl->AddTriangleFilled(P(tip,0),P(nb,-w2),P(nb,w2),col);
        dl->AddCircleFilled(c, r*0.1f, col); return true; }
    if (is("thinking") || is("running")) { for (int i=-1;i<=1;i++) dl->AddCircleFilled(P(i*0.34f,0.0f), r*0.12f, col); return true; }
    if (is("action")) {
        dl->AddCircle(c, r*0.30f, col, 0, th);
        for (int i=0;i<8;i++){ float a=i*(3.14159f/4.f); dl->AddLine(ImVec2(c.x+ImCos(a)*r*0.34f,c.y+ImSin(a)*r*0.34f), ImVec2(c.x+ImCos(a)*r*0.5f,c.y+ImSin(a)*r*0.5f),col,th); }
        return true; }
    return false;
}

void monogram(const char* name, char out[3]) {
    out[0]=out[1]=out[2]=0;
    if (!name || !*name) { out[0]='?'; return; }
    int n=0;
    for (const char* c=name; *c && n<2; ++c) {
        if (std::isalpha((unsigned char)*c)) out[n++] = (char)std::toupper((unsigned char)*c);
        if (*c=='_' && n>=1) break;
    }
    if (n==0) out[0]='?';
}

} // namespace

void initAtlas(const std::string& resourcesDir, bool dark) {
    g.dark = dark;
    g.texDark  = uploadAtlas(resourcesDir + "/icons/atlas_dark.png");
    g.texLight = uploadAtlas(resourcesDir + "/icons/atlas_light.png");

    std::ifstream f(resourcesDir + "/icons/atlas.json");
    if (!f) { std::fprintf(stderr, "[icons] atlas.json not found in %s/icons\n", resourcesDir.c_str()); return; }
    std::stringstream ss; ss << f.rdbuf();
    try {
        nlohmann::json j = nlohmann::json::parse(ss.str());
        float W = j["atlas_px"][0].get<float>(), H = j["atlas_px"][1].get<float>();
        for (auto& [name, cell] : j["cell"].items()) {
            // keep only the @32 (high-res) variant; store under the base name
            auto at = name.rfind("@32");
            if (at == std::string::npos) continue;
            std::string base = name.substr(0, at);
            float x=cell["x"].get<float>(), y=cell["y"].get<float>(), w=cell["w"].get<float>(), h=cell["h"].get<float>();
            g.cells[base] = Cell{ ImVec2(x/W, y/H), ImVec2((x+w)/W, (y+h)/H) };
        }
        g.loaded = !g.cells.empty();
    } catch (...) { std::fprintf(stderr, "[icons] atlas.json parse failed\n"); }
}

void setAtlasTheme(bool dark) { g.dark = dark; }

void draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col) {
    if (drawGlyph(dl, name, c, size, col)) return;     // 1. hand-drawn chrome glyph

    ImVec2 uv0, uv1;                                    // 2. pre-rendered CAD atlas
    if (atlasLookup(name, uv0, uv1) && g.tex()) {
        float r = size * 0.5f;
        dl->AddImage((ImTextureID)(intptr_t)g.tex(), ImVec2(c.x-r, c.y-r), ImVec2(c.x+r, c.y+r), uv0, uv1, IM_COL32_WHITE);
        return;
    }

    // 3. monogram placeholder
    float r2 = size*0.5f;
    ImVec2 a(c.x-r2, c.y-r2), b(c.x+r2, c.y+r2);
    dl->AddRect(a, b, (col & 0x00FFFFFF) | 0x40000000, size*0.22f, 0, ImMax(1.0f, size*0.06f));
    char mg[3]; monogram(name, mg);
    ImFont* f = ImGui::GetFont();
    float fs = size * 0.46f;
    ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, mg);
    dl->AddText(f, fs, ImVec2(c.x - ts.x*0.5f, c.y - ts.y*0.5f), col, mg);
}

void inlineIcon(const char* name, float size, ImU32 col) {
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(size, size));
    draw(ImGui::GetWindowDrawList(), name, ImVec2(p.x+size*0.5f, p.y+size*0.5f), size, col);
}

} // namespace icons
