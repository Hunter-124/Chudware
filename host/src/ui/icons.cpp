// AI-CAD icon loader — C14.
// Uses stb_image (already vendored via stb_impl.cpp).  nlohmann/json for atlas.json.
// GOTCHA: never add host/src to the include path; use relative includes.
#include "icons.h"
#include "icons_sdf.h"

#include <glad/gl.h>
#include "imgui.h"

#include "stb_image.h"

#include <nlohmann/json.hpp>

#include "../ui_theme.h"  // ui::activeTheme, ui::Theme

#include <cstdio>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <cstring>

namespace ui {

// --- internal types --------------------------------------------------------

// UV rect within a raster atlas texture — shared layout with icons_sdf::CellUV (icons_sdf.cpp
// samples pixel data from the same atlas.json cells to derive SDF glyphs).
using Cell = icons_sdf::CellUV;

struct AtlasState {
    GLuint texDark  = 0;
    GLuint texLight = 0;
    int    atlasW   = 0;
    int    atlasH   = 0;

    // placeholder: 1x1 magenta texture
    GLuint texPlaceholder = 0;

    std::unordered_map<std::string, Cell> cells;  // key = "name@32" or "name@20"
    bool loaded = false;
};

static AtlasState gAtlas;
static std::unordered_set<std::string> gWarnedMissing;

// ---------------------------------------------------------------------------

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

    // --- load atlas.json ---
    std::string jsonPath = assetsDir + "/icons/atlas.json";
    FILE* f = std::fopen(jsonPath.c_str(), "rb");
    if (!f) {
        std::fprintf(stderr, "[icons] atlas.json not found at %s\n", jsonPath.c_str());
        return;
    }
    std::fseek(f, 0, SEEK_END);
    long sz = std::ftell(f);
    std::rewind(f);
    std::string buf(sz, 0);
    const size_t nread = std::fread(&buf[0], 1, sz, f);
    std::fclose(f);
    if (nread != static_cast<size_t>(sz)) {
        std::fprintf(stderr, "[icons] atlas.json read error\n");
        return;
    }

    std::unordered_set<std::string> rasterOnly;
    nlohmann::json jroot;
    try {
        jroot = nlohmann::json::parse(buf);
        if (!jroot.contains("atlas_px") || !jroot["atlas_px"].is_array() ||
            jroot["atlas_px"].size() < 2 || !jroot.contains("cell") ||
            !jroot["cell"].is_object()) {
            std::fprintf(stderr, "[icons] atlas.json schema error\n");
            return;
        }
        auto atlasArr = jroot["atlas_px"];
        int aw = atlasArr[0].get<int>();
        int ah = atlasArr[1].get<int>();
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
            int x = cell["x"].get<int>();
            int y = cell["y"].get<int>();
            int w = cell["w"].get<int>();
            int h = cell["h"].get<int>();
            Cell c;
            c.u0 = (float)x / aw;
            c.v0 = (float)y / ah;
            c.u1 = (float)(x + w) / aw;
            c.v1 = (float)(y + h) / ah;
            gAtlas.cells[it.key()] = c;
        }
        // T-G15: photographic/screenshot tiles opt out of SDF derivation (see icons_sdf.h).
        if (jroot.contains("raster_only") && jroot["raster_only"].is_array())
            for (const auto& n : jroot["raster_only"])
                if (n.is_string()) rasterOnly.insert(n.get<std::string>());
    } catch (...) {
        std::fprintf(stderr, "[icons] atlas.json parse error\n");
        return;
    }

    // --- load dark atlas ---
    {
        std::string path = assetsDir + "/icons/atlas_dark.png";
        int w, h, ch;
        stbi_set_flip_vertically_on_load(0);
        unsigned char* px = stbi_load(path.c_str(), &w, &h, &ch, 4);
        if (px) {
            gAtlas.texDark = uploadTexture(px, w, h);
            // SDF derivation reads the still-live pixel buffer (shape is theme-independent, so
            // deriving once from the dark atlas covers both themes) — freed right after.
            icons_sdf::build(px, w, h, gAtlas.cells, rasterOnly);
            stbi_image_free(px);
        } else {
            std::fprintf(stderr, "[icons] failed to load atlas_dark.png: %s\n", stbi_failure_reason());
        }
    }

    // --- load light atlas ---
    {
        std::string path = assetsDir + "/icons/atlas_light.png";
        int w, h, ch;
        unsigned char* px = stbi_load(path.c_str(), &w, &h, &ch, 4);
        if (px) {
            gAtlas.texLight = uploadTexture(px, w, h);
            stbi_image_free(px);
        } else {
            std::fprintf(stderr, "[icons] failed to load atlas_light.png: %s\n", stbi_failure_reason());
        }
    }

    // --- placeholder: 1x1 magenta ---
    {
        unsigned char mag[4] = {255, 0, 255, 255};
        gAtlas.texPlaceholder = uploadTexture(mag, 1, 1);
    }

    gAtlas.loaded = true;
}

void freeIcons() {
    icons_sdf::shutdown();
    if (gAtlas.texDark)        { glDeleteTextures(1, &gAtlas.texDark);        gAtlas.texDark = 0; }
    if (gAtlas.texLight)       { glDeleteTextures(1, &gAtlas.texLight);       gAtlas.texLight = 0; }
    if (gAtlas.texPlaceholder) { glDeleteTextures(1, &gAtlas.texPlaceholder); gAtlas.texPlaceholder = 0; }
    gAtlas.cells.clear();
    gAtlas.loaded = false;
}

// Icon aliases: names referenced by the ribbon/toolbar that have no dedicated atlas cell borrow a
// semantically-close existing glyph, so nothing ever falls to the magenta placeholder. This table
// is the single source of truth for those fallbacks; the atlas-coverage guard (assets/test_icons.py)
// treats an aliased name as "covered". Prefer adding a real rendered tile (scripts/icon_pipeline.py)
// over a permanent alias — replace the entry here once the dedicated glyph lands.
const char* resolveIconAlias(const char* name) {
    if (!name) return name;
    struct Alias { const char* from; const char* to; };
    static const Alias kAliases[] = {
        {"rotate_ent",          "pattern_circ_ent"},  // TODO: dedicated rotate-entities glyph
        {"stock",               "cat_stock_metal"},   // Manufacture▸Stock — reuse the metal-block tile
        // (Features▸Thread now has its own atlas glyph "thread" — interim cam_thread_mill alias removed.)
        {"name_group_features", "folder_new"},         // group selected features into a named folder
        {"new_part",            "part"},
        {"open_part",           "open"},
        {"appr_steel",          "appr_metal"},         // appearance metals share the polished-metal render
        {"appr_brass",          "appr_metal"},
        {"appr_dark",           "appr_metal"},
    };
    for (const Alias& a : kAliases)
        if (std::strcmp(name, a.from) == 0) return a.to;
    return name;
}

IconUV getIcon(const char* name, int size) {
    IconUV out;

    // Select texture based on active theme.
    GLuint activeTex = (activeTheme == Theme::Light) ? gAtlas.texLight : gAtlas.texDark;
    if (!activeTex) activeTex = gAtlas.texPlaceholder;
    out.texId = (ImTextureID)(intptr_t)activeTex;

    if (!gAtlas.loaded || !name) {
        out.valid = false;
        out.texId = (ImTextureID)(intptr_t)gAtlas.texPlaceholder;
        return out;
    }

    name = resolveIconAlias(name);

    // Key = "name@32" or "name@20"
    char key[128];
    std::snprintf(key, sizeof(key), "%s@%d", name, size);

    auto it = gAtlas.cells.find(key);
    if (it == gAtlas.cells.end()) {
        // Try the other size as a fallback before giving up.
        int altSize = (size == 32) ? 20 : 32;
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
    out.uv0  = {c.u0, c.v0};
    out.uv1  = {c.u1, c.v1};
    out.valid = true;
    return out;
}

void syncIconTheme() {
    // Nothing to precompute — getIcon() reads activeTheme each call.
    // This hook exists so callers can force a cache invalidation if needed in the future.
}

bool hasIconSdf(const char* name) {
    return icons_sdf::has(name);
}

void drawIconGlass(ImDrawList* dl, const char* name, ImVec2 center, float sizePx, ImU32 col,
                    ImU32 haloCol) {
    name = resolveIconAlias(name);
    if (icons_sdf::draw(dl, name, center, sizePx, col, haloCol)) return;

    // Fallback: no SDF glyph (multi-color raster icon) — plain atlas draw, no halo. Cell-size
    // pick mirrors the v2 icons::pickCellSize convention (ui/imgui/src/theme/icons.cpp).
    const int cellSize = (sizePx >= 24.f) ? 32 : 20;
    const IconUV ic = getIcon(name, cellSize);
    const float r = sizePx * 0.5f;
    dl->AddImage(ic.texId, ImVec2(center.x - r, center.y - r), ImVec2(center.x + r, center.y + r),
                 ic.uv0, ic.uv1, col);
}

}  // namespace ui
