// AI-CAD icon loader — C14.
// Uses stb_image (already vendored via stb_impl.cpp).  nlohmann/json for atlas.json.
// GOTCHA: never add host/src to the include path; use relative includes.
#include "icons.h"

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

struct Cell {
    float u0, v0, u1, v1;  // UV coords, 0..1
};

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
    std::string jsonPath = assetsDir + "\\icons\\atlas.json";
    FILE* f = std::fopen(jsonPath.c_str(), "rb");
    if (!f) {
        std::fprintf(stderr, "[icons] atlas.json not found at %s\n", jsonPath.c_str());
        return;
    }
    std::fseek(f, 0, SEEK_END);
    long sz = std::ftell(f);
    std::rewind(f);
    std::string buf(sz, 0);
    std::fread(&buf[0], 1, sz, f);
    std::fclose(f);

    nlohmann::json jroot;
    try {
        jroot = nlohmann::json::parse(buf);
    } catch (...) {
        std::fprintf(stderr, "[icons] atlas.json parse error\n");
        return;
    }

    auto atlasArr = jroot["atlas_px"];
    int aw = atlasArr[0].get<int>();
    int ah = atlasArr[1].get<int>();
    gAtlas.atlasW = aw;
    gAtlas.atlasH = ah;

    const auto& cells = jroot["cell"];
    for (auto it = cells.begin(); it != cells.end(); ++it) {
        int x = it.value()["x"].get<int>();
        int y = it.value()["y"].get<int>();
        int w = it.value()["w"].get<int>();
        int h = it.value()["h"].get<int>();
        Cell c;
        c.u0 = (float)x / aw;
        c.v0 = (float)y / ah;
        c.u1 = (float)(x + w) / aw;
        c.v1 = (float)(y + h) / ah;
        gAtlas.cells[it.key()] = c;
    }

    // --- load dark atlas ---
    {
        std::string path = assetsDir + "\\icons\\atlas_dark.png";
        int w, h, ch;
        stbi_set_flip_vertically_on_load(0);
        unsigned char* px = stbi_load(path.c_str(), &w, &h, &ch, 4);
        if (px) {
            gAtlas.texDark = uploadTexture(px, w, h);
            stbi_image_free(px);
        } else {
            std::fprintf(stderr, "[icons] failed to load atlas_dark.png: %s\n", stbi_failure_reason());
        }
    }

    // --- load light atlas ---
    {
        std::string path = assetsDir + "\\icons\\atlas_light.png";
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
    if (gAtlas.texDark)        { glDeleteTextures(1, &gAtlas.texDark);        gAtlas.texDark = 0; }
    if (gAtlas.texLight)       { glDeleteTextures(1, &gAtlas.texLight);       gAtlas.texLight = 0; }
    if (gAtlas.texPlaceholder) { glDeleteTextures(1, &gAtlas.texPlaceholder); gAtlas.texPlaceholder = 0; }
    gAtlas.cells.clear();
    gAtlas.loaded = false;
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

}  // namespace ui
