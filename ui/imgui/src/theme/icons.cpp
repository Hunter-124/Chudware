// AI-CAD icon loader — ported from host/src/ui/icons.cpp (C14).
#include "theme/icons.h"

#include <glad/gl.h>
#include "imgui.h"
#include "imgui_internal.h"
#include "stb_image.h"
#include "theme/theme.h"

#include <nlohmann/json.hpp>

#include <cctype>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace ui {

struct Cell {
    float u0, v0, u1, v1;
};

struct AtlasState {
    GLuint texDark  = 0;
    GLuint texLight = 0;
    int    atlasW   = 0;
    int    atlasH   = 0;
    GLuint texPlaceholder = 0;
    std::unordered_map<std::string, Cell> cells;
    bool loaded = false;
};

static AtlasState gAtlas;
static std::unordered_set<std::string> gWarnedMissing;

static GLuint uploadTexture(unsigned char* px, int w, int h) {
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    glBindTexture(GL_TEXTURE_2D, 0);
    return tex;
}

void loadIcons(const std::string& assetsDir) {
    if (gAtlas.loaded) return;

    const std::string jsonPath = assetsDir + "/icons/atlas.json";
    FILE* f = std::fopen(jsonPath.c_str(), "rb");
    if (!f) {
        std::fprintf(stderr, "[icons] atlas.json not found at %s\n", jsonPath.c_str());
        return;
    }
    std::fseek(f, 0, SEEK_END);
    const long sz = std::ftell(f);
    std::rewind(f);
    std::string buf(static_cast<size_t>(sz), 0);
    const size_t nread = std::fread(&buf[0], 1, static_cast<size_t>(sz), f);
    std::fclose(f);
    if (nread != static_cast<size_t>(sz)) {
        std::fprintf(stderr, "[icons] atlas.json read error\n");
        return;
    }

    try {
        nlohmann::json jroot = nlohmann::json::parse(buf);
        if (!jroot.contains("atlas_px") || !jroot["atlas_px"].is_array() ||
            jroot["atlas_px"].size() < 2 || !jroot.contains("cell") ||
            !jroot["cell"].is_object()) {
            std::fprintf(stderr, "[icons] atlas.json schema error\n");
            return;
        }
        const auto atlasArr = jroot["atlas_px"];
        const int aw = atlasArr[0].get<int>();
        const int ah = atlasArr[1].get<int>();
        if (aw <= 0 || ah <= 0) {
            std::fprintf(stderr, "[icons] atlas.json invalid atlas_px\n");
            return;
        }
        gAtlas.atlasW = aw;
        gAtlas.atlasH = ah;

        const auto& cells = jroot["cell"];
        for (auto it = cells.begin(); it != cells.end(); ++it) {
            const auto& cell = it.value();
            if (!cell.is_object() || !cell.contains("x") || !cell.contains("y") ||
                !cell.contains("w") || !cell.contains("h"))
                continue;
            const int x = cell["x"].get<int>();
            const int y = cell["y"].get<int>();
            const int w = cell["w"].get<int>();
            const int h = cell["h"].get<int>();
            Cell c;
            c.u0 = static_cast<float>(x) / aw;
            c.v0 = static_cast<float>(y) / ah;
            c.u1 = static_cast<float>(x + w) / aw;
            c.v1 = static_cast<float>(y + h) / ah;
            gAtlas.cells[it.key()] = c;
        }
    } catch (...) {
        std::fprintf(stderr, "[icons] atlas.json parse error\n");
        return;
    }

    {
        const std::string path = assetsDir + "/icons/atlas_dark.png";
        int w = 0, h = 0, ch = 0;
        stbi_set_flip_vertically_on_load(0);
        unsigned char* px = stbi_load(path.c_str(), &w, &h, &ch, 4);
        if (px) {
            gAtlas.texDark = uploadTexture(px, w, h);
            stbi_image_free(px);
        } else {
            std::fprintf(stderr, "[icons] failed to load atlas_dark.png: %s\n", stbi_failure_reason());
        }
    }

    {
        const std::string path = assetsDir + "/icons/atlas_light.png";
        int w = 0, h = 0, ch = 0;
        unsigned char* px = stbi_load(path.c_str(), &w, &h, &ch, 4);
        if (px) {
            gAtlas.texLight = uploadTexture(px, w, h);
            stbi_image_free(px);
        } else {
            std::fprintf(stderr, "[icons] failed to load atlas_light.png: %s\n", stbi_failure_reason());
        }
    }

    {
        unsigned char mag[4] = {255, 0, 255, 255};
        gAtlas.texPlaceholder = uploadTexture(mag, 1, 1);
    }

    gAtlas.loaded = true;
}

void freeIcons() {
    if (gAtlas.texDark)        { glDeleteTextures(1, &gAtlas.texDark);        gAtlas.texDark = 0; }
    if (gAtlas.texLight)       { glDeleteTextures(1, &gAtlas.texLight);       gAtlas.texLight = 0; }
    if (gAtlas.texPlaceholder) { glDeleteTextures(1, &gAtlas.texPlaceholder); gAtlas.texPlaceholder = 0; }
    gAtlas.cells.clear();
    gAtlas.loaded = false;
}

IconUV getIcon(const char* name, int size) {
    IconUV out;

    const bool lightTheme = !theme::ctx().dark;
    GLuint activeTex = lightTheme ? gAtlas.texLight : gAtlas.texDark;
    if (!activeTex) activeTex = gAtlas.texPlaceholder;
    out.texId = (ImTextureID)(intptr_t)activeTex;

    if (!gAtlas.loaded || !name) {
        out.valid = false;
        out.texId = (ImTextureID)(intptr_t)gAtlas.texPlaceholder;
        return out;
    }

    char key[128];
    std::snprintf(key, sizeof(key), "%s@%d", name, size);

    auto it = gAtlas.cells.find(key);
    if (it == gAtlas.cells.end()) {
        const int altSize = (size == 32) ? 20 : 32;
        char altKey[128];
        std::snprintf(altKey, sizeof(altKey), "%s@%d", name, altSize);
        it = gAtlas.cells.find(altKey);
    }

    if (it == gAtlas.cells.end()) {
        if (gWarnedMissing.find(name) == gWarnedMissing.end()) {
            std::fprintf(stderr, "[icons] unknown icon: '%s' (size %d)\n", name, size);
            gWarnedMissing.insert(name);
        }
        out.valid = false;
        out.texId = (ImTextureID)(intptr_t)gAtlas.texPlaceholder;
        out.uv0 = {0, 0};
        out.uv1 = {1, 1};
        return out;
    }

    const Cell& c = it->second;
    out.uv0 = {c.u0, c.v0};
    out.uv1 = {c.u1, c.v1};
    out.valid = true;
    return out;
}

void syncIconTheme() {
    // getIcon() reads theme::ctx().dark each call.
}

}  // namespace ui

namespace icons {
namespace {

const std::unordered_map<std::string, std::string>& aliasMap() {
    static const std::unordered_map<std::string, std::string> m = {
        {"extrude", "extrude_boss"}, {"revolve", "revolve_boss"}, {"hole", "hole_wizard"},
        {"coil", "curve_helix"}, {"pattern_linear", "linear_pattern"}, {"pattern_circular", "circular_pattern"},
        {"prim_box", "box_feature"}, {"prim_cylinder", "cylinder_feature"}, {"prim_sphere", "sphere_feature"}, {"prim_cone", "cone_feature"},
        {"analyze_thickness", "thickness"}, {"analyze_draft", "draft_analysis"}, {"analyze_curvature", "curvature"},
        {"sk_line", "line"}, {"sk_rect", "rect_corner"}, {"sk_circle", "circle"}, {"sk_arc", "arc_3pt"},
        {"sk_polygon", "polygon"}, {"sk_slot", "slot"}, {"sk_spline", "spline"}, {"sk_ellipse", "ellipse"},
        {"sk_point", "point_ent"}, {"sk_text", "text_ent"}, {"sk_trim", "trim"}, {"sk_extend", "extend"},
        {"sk_offset", "offset_ent"}, {"sk_convert", "convert_ent"}, {"sk_construction", "construction"},
        {"ws_sketch", "sketch"}, {"ws_model", "box_feature"}, {"ws_assemble", "assembly"}, {"ws_simulate", "activity"},
        {"ws_draft", "section_props"}, {"ws_schematic", "ai_panel"}, {"ws_board", "extrude_boss"},
        {"ws_sheetmetal", "shell"}, {"ws_weldment", "sweep"}, {"ws_surface", "boundary"},
        {"sim_static", "study_static"}, {"sim_modal", "study_modal"}, {"sim_flow", "study_flow"},
        {"sim_thermal", "study_flow"}, {"sim_fatigue", "results"}, {"std_parts", "smart_fasteners"},
        {"dwg_view", "view_cube"}, {"dwg_projected", "orientation"}, {"dwg_section", "section_view"},
        {"dwg_detail", "zoom_area"}, {"gdt", "smart_dim"}, {"datum", "point"}, {"balloon", "circle"},
        {"note", "text_ent"}, {"title_block", "new_doc"}, {"sch_symbol", "point_ent"}, {"sch_wire", "line"},
        {"net_label", "text_ent"}, {"power_port", "construction"}, {"pcb_route", "line"}, {"via", "circle"},
        {"copper_zone", "rect_center"}, {"autoroute", "sweep"}, {"drc", "interference"}, {"gerber", "ref_plane"},
        {"board_3d", "extrude_boss"}, {"ai_design", "ai_panel"}, {"file_part", "part"}, {"file_assembly", "assembly"},
        {"file_drawing", "new_doc"}, {"file_schematic", "ai_panel"}, {"file_pcb", "extrude_boss"}, {"folder", "open"},
        {"rail_tree", "history"}, {"rail_bodies", "body_folder"}, {"rail_files", "folder_new"}, {"rail_configs", "settings"},
    };
    return m;
}

const char* resolveAtlasName(const char* id) {
    if (!id) return id;
    auto it = aliasMap().find(id);
    return (it == aliasMap().end()) ? id : it->second.c_str();
}

int pickCellSize(float drawSize) {
    return (drawSize >= 24.f) ? 32 : 20;
}

bool drawGlyph(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col) {
    const float r = size * 0.5f;
    auto P = [&](float x, float y) { return ImVec2(c.x + x * r, c.y + y * r); };
    const float th = ImMax(1.4f, size * 0.085f);
    auto is = [&](const char* n) { return std::strcmp(name, n) == 0; };

    if (is("ok_check") || is("success")) {
        ImVec2 p[3] = {P(-0.5f, 0.05f), P(-0.15f, 0.42f), P(0.5f, -0.4f)};
        dl->AddPolyline(p, 3, col, 0, th);
        return true;
    }
    if (is("cancel_x") || is("win_close") || is("failure") || is("tab_close")) {
        dl->AddLine(P(-0.4f, -0.4f), P(0.4f, 0.4f), col, th);
        dl->AddLine(P(-0.4f, 0.4f), P(0.4f, -0.4f), col, th);
        return true;
    }
    if (is("win_min")) { dl->AddLine(P(-0.42f, 0.0f), P(0.42f, 0.0f), col, th); return true; }
    if (is("win_max")) { dl->AddRect(P(-0.4f, -0.4f), P(0.4f, 0.4f), col, 1.5f, 0, th); return true; }
    if (is("win_restore")) {
        dl->AddRect(P(-0.45f, -0.2f), P(0.2f, 0.45f), col, 1.5f, 0, th);
        dl->AddRect(P(-0.2f, -0.45f), P(0.45f, 0.2f), col, 1.5f, 0, th);
        return true;
    }
    if (is("menu")) {
        for (float y = -0.32f; y <= 0.34f; y += 0.32f) dl->AddLine(P(-0.42f, y), P(0.42f, y), col, th);
        return true;
    }
    if (is("search") || is("rail_search")) {
        dl->AddCircle(P(-0.12f, -0.12f), r * 0.42f, col, 0, th);
        dl->AddLine(P(0.2f, 0.2f), P(0.45f, 0.45f), col, th);
        return true;
    }
    if (is("tab_add") || is("plus")) {
        dl->AddLine(P(0, -0.4f), P(0, 0.4f), col, th);
        dl->AddLine(P(-0.4f, 0), P(0.4f, 0), col, th);
        return true;
    }
    if (is("chevron_down") || is("expand")) {
        ImVec2 p[3] = {P(-0.3f, -0.15f), P(0, 0.2f), P(0.3f, -0.15f)};
        dl->AddPolyline(p, 3, col, 0, th);
        return true;
    }
    if (is("agent_send")) {
        dl->AddLine(P(0, 0.4f), P(0, -0.4f), col, th);
        ImVec2 a[3] = {P(-0.3f, -0.1f), P(0, -0.42f), P(0.3f, -0.1f)};
        dl->AddPolyline(a, 3, col, 0, th);
        return true;
    }
    if (is("agent_stop")) { dl->AddRectFilled(P(-0.3f, -0.3f), P(0.3f, 0.3f), col, 2.f); return true; }
    if (is("agent") || is("sparkles")) {
        const float tip = 0.52f, w2 = 0.13f, nb = 0.13f;
        dl->AddTriangleFilled(P(0, -tip), P(-w2, -nb), P(w2, -nb), col);
        dl->AddTriangleFilled(P(0, tip), P(-w2, nb), P(w2, nb), col);
        dl->AddTriangleFilled(P(-tip, 0), P(-nb, -w2), P(-nb, w2), col);
        dl->AddTriangleFilled(P(tip, 0), P(nb, -w2), P(nb, w2), col);
        dl->AddCircleFilled(c, r * 0.1f, col);
        return true;
    }
    if (is("thinking") || is("running")) {
        for (int i = -1; i <= 1; i++) dl->AddCircleFilled(P(i * 0.34f, 0.0f), r * 0.12f, col);
        return true;
    }
    if (is("action")) {
        dl->AddCircle(c, r * 0.30f, col, 0, th);
        for (int i = 0; i < 8; i++) {
            const float a = i * (3.14159f / 4.f);
            dl->AddLine(ImVec2(c.x + ImCos(a) * r * 0.34f, c.y + ImSin(a) * r * 0.34f),
                        ImVec2(c.x + ImCos(a) * r * 0.5f, c.y + ImSin(a) * r * 0.5f), col, th);
        }
        return true;
    }
    return false;
}

void monogram(const char* name, char out[3]) {
    out[0] = out[1] = out[2] = 0;
    if (!name || !*name) { out[0] = '?'; return; }
    int n = 0;
    for (const char* c = name; *c && n < 2; ++c) {
        if (std::isalpha(static_cast<unsigned char>(*c))) out[n++] = static_cast<char>(std::toupper(static_cast<unsigned char>(*c)));
        if (*c == '_' && n >= 1) break;
    }
    if (n == 0) out[0] = '?';
}

}  // namespace

void initAtlas(const std::string& resourcesDir, bool /*dark*/) {
    ui::loadIcons(resourcesDir);
}

void setAtlasTheme(bool /*dark*/) {
    ui::syncIconTheme();
}

void draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col) {
    if (drawGlyph(dl, name, c, size, col)) return;

    const char* atlasName = resolveAtlasName(name);
    const ui::IconUV icon = ui::getIcon(atlasName, pickCellSize(size));
    if (icon.valid) {
        const float r = size * 0.5f;
        dl->AddImage(icon.texId, ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r),
                     icon.uv0, icon.uv1, IM_COL32_WHITE);
        return;
    }

    const float r2 = size * 0.5f;
    const ImVec2 a(c.x - r2, c.y - r2), b(c.x + r2, c.y + r2);
    dl->AddRect(a, b, (col & 0x00FFFFFF) | 0x40000000, size * 0.22f, 0, ImMax(1.0f, size * 0.06f));
    char mg[3];
    monogram(name, mg);
    ImFont* f = ImGui::GetFont();
    const float fs = size * 0.46f;
    const ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, mg);
    dl->AddText(f, fs, ImVec2(c.x - ts.x * 0.5f, c.y - ts.y * 0.5f), col, mg);
}

void inlineIcon(const char* name, float size, ImU32 col) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(size, size));
    draw(ImGui::GetWindowDrawList(), name, ImVec2(p.x + size * 0.5f, p.y + size * 0.5f), size, col);
}

}  // namespace icons
