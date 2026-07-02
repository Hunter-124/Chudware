// Preview PNG -> GL texture cache (T-F16). See texture_cache.h.
#include "theme/texture_cache.h"

#include <glad/gl.h>
#include "stb_image.h"

#include <cstdio>
#include <unordered_map>

namespace ui {

namespace {

struct CachedTex {
    GLuint tex = 0;
    int w = 0;
    int h = 0;
};

std::unordered_map<std::string, CachedTex> gCache;

// Same upload pattern as theme/icons.cpp's file-local uploadTexture().
GLuint uploadTexture(unsigned char* px, int w, int h) {
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

void freeCached(CachedTex& c) {
    if (c.tex) { glDeleteTextures(1, &c.tex); c.tex = 0; }
}

}  // namespace

ImTextureID loadPreviewTexture(const std::string& absPath, int* outW, int* outH, bool force) {
    if (absPath.empty()) return (ImTextureID)0;

    if (force) evictPreviewTexture(absPath);

    auto it = gCache.find(absPath);
    if (it != gCache.end()) {
        if (outW) *outW = it->second.w;
        if (outH) *outH = it->second.h;
        return (ImTextureID)(intptr_t)it->second.tex;
    }

    int w = 0, h = 0, ch = 0;
    unsigned char* px = stbi_load(absPath.c_str(), &w, &h, &ch, 4);
    if (!px) {
        std::fprintf(stderr, "[texture_cache] failed to load %s: %s\n",
                     absPath.c_str(), stbi_failure_reason());
        gCache[absPath] = CachedTex{};  // negative-cache: no disk retry per frame;
        return (ImTextureID)0;          // evictPreviewTexture clears it on a fresh render
    }

    CachedTex c;
    c.tex = uploadTexture(px, w, h);
    c.w = w;
    c.h = h;
    stbi_image_free(px);

    if (!c.tex) return (ImTextureID)0;

    gCache[absPath] = c;
    if (outW) *outW = w;
    if (outH) *outH = h;
    return (ImTextureID)(intptr_t)c.tex;
}

void evictPreviewTexture(const std::string& absPath) {
    auto it = gCache.find(absPath);
    if (it == gCache.end()) return;
    freeCached(it->second);
    gCache.erase(it);
}

void clearPreviewTextures() {
    for (auto& kv : gCache) freeCached(kv.second);
    gCache.clear();
}

}  // namespace ui
