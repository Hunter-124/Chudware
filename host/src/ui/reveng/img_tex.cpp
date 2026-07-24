// img_tex.cpp — image decode + GL texture util (RE-08) + thumbnail LRU cache (RE-40)
// stb_image / stb_image_resize2 are compiled in host/src/stb_impl.cpp; only include the
// headers here. GL-thread only.
#include "img_tex.h"

#include "stb_image.h"
#include "stb_image_resize2.h"
#include "gl_compat.h"

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

namespace reveng {

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

static unsigned int uploadRGBA(const unsigned char* px, int w, int h) {
    if (!px || w <= 0 || h <= 0) return 0;
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    glBindTexture(GL_TEXTURE_2D, 0);
    return static_cast<unsigned int>(tex);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool decodeFile(const std::string& path, std::vector<unsigned char>& pixels,
                int& w, int& h) {
    int channels = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load failed for '%s': %s\n",
                     path.c_str(), stbi_failure_reason());
        return false;
    }
    pixels.assign(px, px + static_cast<size_t>(w) * h * 4);
    stbi_image_free(px);
    return true;
}

bool decodeMemory(const unsigned char* data, int dataLen,
                  std::vector<unsigned char>& pixels, int& w, int& h) {
    int channels = 0;
    stbi_uc* px = stbi_load_from_memory(data, dataLen, &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load_from_memory failed: %s\n",
                     stbi_failure_reason());
        return false;
    }
    pixels.assign(px, px + static_cast<size_t>(w) * h * 4);
    stbi_image_free(px);
    return true;
}

unsigned int loadTexFromFile(const std::string& path, int* outW, int* outH) {
    int w = 0, h = 0, channels = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load failed for '%s': %s\n",
                     path.c_str(), stbi_failure_reason());
        return 0;
    }
    unsigned int tex = uploadRGBA(px, w, h);
    stbi_image_free(px);
    if (outW) *outW = w;
    if (outH) *outH = h;
    return tex;
}

unsigned int loadTexFromMemory(const unsigned char* data, int dataLen,
                               int* outW, int* outH) {
    int w = 0, h = 0, channels = 0;
    stbi_uc* px = stbi_load_from_memory(data, dataLen, &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load_from_memory failed: %s\n",
                     stbi_failure_reason());
        return 0;
    }
    unsigned int tex = uploadRGBA(px, w, h);
    stbi_image_free(px);
    if (outW) *outW = w;
    if (outH) *outH = h;
    return tex;
}

void freeTexture(unsigned int texId) {
    if (!texId) return;
    // The thumbnail cache hands the same id to many callers; deleting it here would leave the
    // cache pointing at a dead texture and the next caller double-freeing it. Ownership wins.
    if (thumbCacheOwns(texId)) return;
    GLuint t = static_cast<GLuint>(texId);
    glDeleteTextures(1, &t);
}

// ---------------------------------------------------------------------------
// Thumbnail decode (RE-40)
// ---------------------------------------------------------------------------

unsigned int loadThumbTexFromFile(const std::string& path, int maxEdgePx,
                                  int* outW, int* outH, int* outSrcW, int* outSrcH) {
    int sw = 0, sh = 0, channels = 0;
    stbi_uc* px = stbi_load(path.c_str(), &sw, &sh, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] thumb stbi_load failed for '%s': %s\n",
                     path.c_str(), stbi_failure_reason());
        return 0;
    }
    if (outSrcW) *outSrcW = sw;
    if (outSrcH) *outSrcH = sh;

    // Fit inside a square bound, preserving aspect. Never upscale: a 64x64 icon stays 64x64.
    int tw = sw, th = sh;
    const int longEdge = (sw > sh) ? sw : sh;
    if (maxEdgePx > 0 && longEdge > maxEdgePx) {
        const double k = static_cast<double>(maxEdgePx) / static_cast<double>(longEdge);
        tw = static_cast<int>(sw * k + 0.5);
        th = static_cast<int>(sh * k + 0.5);
        if (tw < 1) tw = 1;
        if (th < 1) th = 1;
    }

    unsigned int tex = 0;
    if (tw == sw && th == sh) {
        tex = uploadRGBA(px, sw, sh);
    } else {
        std::vector<unsigned char> small(static_cast<size_t>(tw) * th * 4);
        const unsigned char* resized =
            stbir_resize_uint8_srgb(px, sw, sh, 0, small.data(), tw, th, 0, STBIR_RGBA);
        if (resized) {
            tex = uploadRGBA(small.data(), tw, th);
        } else {
            // Resampler refused (degenerate size) — upload full res rather than show a hole.
            std::fprintf(stderr, "[img_tex] thumb resize %dx%d -> %dx%d failed for '%s'\n",
                         sw, sh, tw, th, path.c_str());
            tex = uploadRGBA(px, sw, sh);
            tw = sw;
            th = sh;
        }
    }
    stbi_image_free(px);

    if (outW) *outW = tw;
    if (outH) *outH = th;
    return tex;
}

// ---------------------------------------------------------------------------
// Thumbnail LRU cache (RE-40)
// ---------------------------------------------------------------------------
// Entry counts are in the low hundreds, so a flat vector with a linear LRU scan beats a
// map + intrusive list on both cache lines and code size.

namespace {

struct ThumbEntry {
    std::string   key;              // path + '|' + maxEdgePx
    unsigned int  tex  = 0;
    int           w    = 0, h    = 0;   // uploaded thumbnail size
    int           srcW = 0, srcH = 0;   // original image size
    std::uint64_t stamp = 0;            // last-use tick (LRU ordering)
};

std::vector<ThumbEntry> g_thumbs;
std::uint64_t           g_thumbTick  = 0;
int                     g_thumbLimit = kThumbCacheDefaultLimit;
// Bumped on every destroyed texture. Callers that hold ids watch this instead of rescanning:
// no eviction means no id can have gone stale (and GL never recycles a name we still own).
std::uint64_t           g_thumbEvictions = 0;

std::string thumbKey(const std::string& path, int maxEdgePx) {
    std::string k;
    k.reserve(path.size() + 8);
    k.append(path).push_back('|');
    k.append(std::to_string(maxEdgePx));
    return k;
}

void deleteEntryTex(ThumbEntry& e) {
    if (e.tex) {
        GLuint t = static_cast<GLuint>(e.tex);
        glDeleteTextures(1, &t);
        e.tex = 0;
        ++g_thumbEvictions;
    }
}

void evictToLimit() {
    while (!g_thumbs.empty() && static_cast<int>(g_thumbs.size()) > g_thumbLimit) {
        size_t lru = 0;
        for (size_t i = 1; i < g_thumbs.size(); ++i)
            if (g_thumbs[i].stamp < g_thumbs[lru].stamp) lru = i;
        deleteEntryTex(g_thumbs[lru]);
        g_thumbs.erase(g_thumbs.begin() + static_cast<std::ptrdiff_t>(lru));
    }
}

}  // namespace

unsigned int cachedThumbTex(const std::string& path, int maxEdgePx,
                            int* outW, int* outH, int* outSrcW, int* outSrcH) {
    if (path.empty()) return 0;
    const std::string key = thumbKey(path, maxEdgePx);

    for (auto& e : g_thumbs) {
        if (e.key != key) continue;
        e.stamp = ++g_thumbTick;
        if (outW)    *outW    = e.w;
        if (outH)    *outH    = e.h;
        if (outSrcW) *outSrcW = e.srcW;
        if (outSrcH) *outSrcH = e.srcH;
        return e.tex;
    }

    ThumbEntry e;
    e.key = key;
    e.tex = loadThumbTexFromFile(path, maxEdgePx, &e.w, &e.h, &e.srcW, &e.srcH);
    if (!e.tex) return 0;   // failures are not cached — a file that appears later still loads

    e.stamp = ++g_thumbTick;
    if (outW)    *outW    = e.w;
    if (outH)    *outH    = e.h;
    if (outSrcW) *outSrcW = e.srcW;
    if (outSrcH) *outSrcH = e.srcH;

    const unsigned int tex = e.tex;
    g_thumbs.push_back(std::move(e));
    evictToLimit();
    return tex;
}

void setThumbCacheLimit(int maxEntries) {
    g_thumbLimit = (maxEntries < 0) ? 0 : maxEntries;
    evictToLimit();
}

int thumbCacheLimit() { return g_thumbLimit; }

int thumbCacheCount() { return static_cast<int>(g_thumbs.size()); }

bool thumbCacheHas(const std::string& path, int maxEdgePx) {
    if (path.empty()) return false;
    const std::string key = thumbKey(path, maxEdgePx);
    for (const auto& e : g_thumbs)
        if (e.key == key) return true;
    return false;
}

bool thumbCacheOwns(unsigned int texId) {
    if (!texId) return false;
    for (const auto& e : g_thumbs)
        if (e.tex == texId) return true;
    return false;
}

bool thumbCacheStillMaps(const std::string& path, int maxEdgePx, unsigned int texId) {
    if (!texId || path.empty()) return false;
    const std::string key = thumbKey(path, maxEdgePx);
    for (const auto& e : g_thumbs)
        if (e.key == key) return e.tex == texId;
    return false;
}

unsigned long long thumbCacheEvictionEpoch() {
    return static_cast<unsigned long long>(g_thumbEvictions);
}

void thumbCacheClear() {
    for (auto& e : g_thumbs) deleteEntryTex(e);
    g_thumbs.clear();
}

} // namespace reveng
