// SDF icon derivation + CPU re-raster cache (Liquid Glass v2 host port, H3 contract).
// Ported verbatim from ui/imgui/src/theme/icons_sdf.cpp. Built once at atlas load from the
// already-decoded atlas RGBA (monochrome-safe glyphs only); rendered by re-rasterizing each
// (name, roundedPx, halo) combo into a dynamic RGBA cache atlas on first use, then reusing the
// cached quad thereafter. All GL on the calling (GUI) thread.
#include "icons_sdf.h"

#include "gl_compat.h"
#include "imgui.h"
#include "imgui_internal.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace icons_sdf {
namespace {

// ---- build-time SDF atlas state ----------------------------------------------------------------
constexpr float kSpread    = 6.0f;  // px, contract §3
constexpr int   kSdfAtlasW = 512;   // fixed shelf-pack width for the static (build-once) SDF atlas

struct SdfGlyph { int x, y, w, h; };

std::unordered_map<std::string, int> gNameToId;
std::vector<SdfGlyph> gGlyphs;
std::vector<uint8_t>  gSdfCpu;      // R8, gSdfW*gSdfH — kept resident for CPU re-raster sampling
int    gSdfW = 0, gSdfH = 0;
GLuint gSdfTex = 0;

// ---- 8SSEDT: two-pass, 8-neighbor signed sequential Euclidean distance transform ----------------
struct Pt { int dx, dy; };
// Sentinel for "no seed found yet". MUST be small enough that distSq() of a sentinel (plus the
// +-1 neighbour offsets compare() adds) cannot overflow int: with the previous 1<<20 sentinel,
// (1<<20)^2 * 2 wrapped int32 to 0 / NEGATIVE, so garbage sentinels WON the min-compare, the
// field filled with negative distSq, sqrt() of it produced NaN, the encoded SDF collapsed and
// every qualifying glyph rasterized to alpha=0 — i.e. all SDF-path icons (weld_gusset,
// display_hlr, appr_metal/appr_glass, display_edges, view_cube) were silently INVISIBLE in the
// ribbon. 1<<12 = 4096 px is far
// beyond any atlas cell (<=64 px) and its distSq (2*(4096+1)^2 ~ 3.4e7) fits int comfortably.
constexpr int kFar = 1 << 12;
inline int distSq(Pt p) { return p.dx * p.dx + p.dy * p.dy; }

void compare(std::vector<Pt>& g, int w, int h, Pt& p, int x, int y, int ox, int oy) {
    const int nx = x + ox, ny = y + oy;
    if (nx < 0 || ny < 0 || nx >= w || ny >= h) return;
    Pt other = g[(size_t)ny * w + nx];
    other.dx += ox;
    other.dy += oy;
    if (distSq(other) < distSq(p)) p = other;
}

// Unsigned distance transform: for every pixel, the offset (px) to the nearest seed pixel (seeds
// are pre-initialized to {0,0} in `g`; everything else starts at the `kFar` sentinel).
void transform(std::vector<Pt>& g, int w, int h) {
    for (int y = 0; y < h; ++y) {
        for (int x = 0; x < w; ++x) {
            Pt p = g[(size_t)y * w + x];
            compare(g, w, h, p, x, y, -1, 0);
            compare(g, w, h, p, x, y, 0, -1);
            compare(g, w, h, p, x, y, -1, -1);
            compare(g, w, h, p, x, y, 1, -1);
            g[(size_t)y * w + x] = p;
        }
        for (int x = w - 1; x >= 0; --x) {
            Pt p = g[(size_t)y * w + x];
            compare(g, w, h, p, x, y, 1, 0);
            g[(size_t)y * w + x] = p;
        }
    }
    for (int y = h - 1; y >= 0; --y) {
        for (int x = w - 1; x >= 0; --x) {
            Pt p = g[(size_t)y * w + x];
            compare(g, w, h, p, x, y, 1, 0);
            compare(g, w, h, p, x, y, 0, 1);
            compare(g, w, h, p, x, y, 1, 1);
            compare(g, w, h, p, x, y, -1, 1);
            g[(size_t)y * w + x] = p;
        }
        for (int x = 0; x < w; ++x) {
            Pt p = g[(size_t)y * w + x];
            compare(g, w, h, p, x, y, -1, 0);
            g[(size_t)y * w + x] = p;
        }
    }
}

// Thresholds `mask` (0/1, w*h) into a spread-clamped signed distance field, R8-encoded (0=outside
// by >=spread px, 128=edge, 255=inside by >=spread px), written into `dst` (stride dstW) at
// (dstX,dstY).
void encodeSdf(const uint8_t* mask, int w, int h, uint8_t* dst, int dstW, int dstX, int dstY) {
    std::vector<Pt> gOutside((size_t)w * h), gInside((size_t)w * h);
    for (int i = 0; i < w * h; ++i) {
        const bool inside = mask[i] != 0;
        gOutside[i] = inside ? Pt{kFar, kFar} : Pt{0, 0};
        gInside[i]  = inside ? Pt{0, 0} : Pt{kFar, kFar};
    }
    transform(gOutside, w, h);
    transform(gInside, w, h);

    for (int y = 0; y < h; ++y) {
        for (int x = 0; x < w; ++x) {
            const int i = y * w + x;
            const bool inside = mask[i] != 0;
            const float distOut = std::sqrt((float)distSq(gOutside[i]));
            const float distIn  = std::sqrt((float)distSq(gInside[i]));
            const float signedPx = inside ? distOut : -distIn;
            float enc = 0.5f + signedPx / (2.f * kSpread);
            enc = ImClamp(enc, 0.f, 1.f);
            dst[(size_t)(dstY + y) * dstW + (dstX + x)] = (uint8_t)(enc * 255.f + 0.5f);
        }
    }
}

// ---- CPU re-raster cache: dynamic, shelf-packed RGBA atlas --------------------------------------
enum class Variant : uint8_t { Glyph = 0, Halo = 1 };
struct CacheEntry { float u0, v0, u1, v1; };

constexpr int kCacheAtlasW  = 1024;
constexpr int kCacheAtlasH0 = 256;

std::unordered_map<uint64_t, CacheEntry> gCache;
std::vector<uint8_t> gCacheCpu;   // RGBA staging buffer, gCacheW*gCacheH*4 (source of truth for regrow)
int    gCacheW = 0, gCacheH = 0;
int    gCacheCursorX = 0, gCacheShelfY = 0, gCacheShelfH = 0;
GLuint gCacheTex = 0;

inline uint64_t cacheKey(int id, int px, Variant v) {
    return ((uint64_t)(uint32_t)id << 24) | ((uint64_t)(uint16_t)px << 8) | (uint64_t)(uint8_t)v;
}

void uploadFullCacheTexture() {
    if (gCacheTex) { glDeleteTextures(1, &gCacheTex); gCacheTex = 0; }
    glGenTextures(1, &gCacheTex);
    glBindTexture(GL_TEXTURE_2D, gCacheTex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, gCacheW, gCacheH, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                 gCacheCpu.empty() ? nullptr : gCacheCpu.data());
    glBindTexture(GL_TEXTURE_2D, 0);
}

void ensureCacheAtlas() {
    if (gCacheTex) return;
    gCacheW = kCacheAtlasW;
    gCacheH = kCacheAtlasH0;
    gCacheCpu.assign((size_t)gCacheW * gCacheH * 4, 0);
    uploadFullCacheTexture();
}

void growCacheAtlas(int neededH) {
    int newH = gCacheH > 0 ? gCacheH : kCacheAtlasH0;
    while (newH < neededH) newH *= 2;
    std::vector<uint8_t> grown((size_t)gCacheW * newH * 4, 0);
    if (!gCacheCpu.empty()) std::memcpy(grown.data(), gCacheCpu.data(), gCacheCpu.size());
    gCacheCpu.swap(grown);
    gCacheH = newH;
    uploadFullCacheTexture();
}

// Shelf-allocates a (w,h) rect in the dynamic cache atlas, growing (recreating the GL texture) if
// the current shelf row is full or the atlas has run out of vertical room. Bounded in practice:
// names x ~6 rounded sizes x 2 variants (contract §3) — never evicted.
void allocCacheRect(int w, int h, int& outX, int& outY) {
    ensureCacheAtlas();
    if (gCacheCursorX + w > gCacheW) {
        gCacheCursorX = 0;
        gCacheShelfY += gCacheShelfH;
        gCacheShelfH = 0;
    }
    if (gCacheShelfY + h > gCacheH) growCacheAtlas(gCacheShelfY + h);
    outX = gCacheCursorX;
    outY = gCacheShelfY;
    gCacheCursorX += w;
    gCacheShelfH = std::max(gCacheShelfH, h);
}

// Bilinear-samples the packed SDF atlas at normalized (u,v) within glyph `g` (matches the
// GL_LINEAR filtering the SDF texture is uploaded with; sampled on the CPU copy that produced the
// texture, so no GPU readback is needed).
float sampleSdf(const SdfGlyph& g, float u, float v) {
    u = ImClamp(u, 0.f, 1.f);
    v = ImClamp(v, 0.f, 1.f);
    const float fx = u * (float)(g.w - 1);
    const float fy = v * (float)(g.h - 1);
    const int x0 = (int)fx, y0 = (int)fy;
    const int x1 = std::min(x0 + 1, g.w - 1), y1 = std::min(y0 + 1, g.h - 1);
    const float tx = fx - (float)x0, ty = fy - (float)y0;
    auto at = [&](int x, int y) -> float {
        return (float)gSdfCpu[(size_t)(g.y + y) * gSdfW + (g.x + x)];
    };
    const float top = at(x0, y0) + (at(x1, y0) - at(x0, y0)) * tx;
    const float bot = at(x0, y1) + (at(x1, y1) - at(x0, y1)) * tx;
    return top + (bot - top) * ty;
}

inline float smoothstepAA(float e0, float e1, float x) {
    if (e1 <= e0) return x >= e0 ? 1.f : 0.f;
    const float t = ImClamp((x - e0) / (e1 - e0), 0.f, 1.f);
    return t * t * (3.f - 2.f * t);
}

// Rasterizes glyph `id` at `sizePx` square for `variant` into the dynamic RGBA cache atlas (white
// texel, alpha=coverage — tinted at draw time via the existing AddImage vertex-color mechanism);
// builds + uploads it on first use, returns the cached UV rect thereafter.
const CacheEntry& rasterize(int id, int sizePx, Variant variant) {
    const uint64_t key = cacheKey(id, sizePx, variant);
    const auto it = gCache.find(key);
    if (it != gCache.end()) return it->second;

    const SdfGlyph& g = gGlyphs[(size_t)id];
    const int px = std::max(1, sizePx);

    // Resolution-correct SDF antialiasing (chrome-polish pass): one TARGET pixel spans
    // (glyphPx / px) source pixels, and one source pixel is 1/(2*kSpread) in encoded-distance
    // units, so a constant ~0.7 target px of edge smoothing is
    //     aa = 0.7071 * (glyphPx / px) / (2 * kSpread).
    // The previous kSpread/px form was off by the glyph/target ratio and over-blurred small
    // sizes ~2x (a 20px icon got ~1.6 target px of half-width smear -> mushy strokes).
    const float srcPerTarget = (float)std::max(std::max(g.w, g.h), 1) / (float)px;
    float aa = 0.7071f * srcPerTarget / (2.0f * kSpread);
    aa = ImClamp(aa, 0.02f, 0.25f);
    // Optical-weight compensation: downscaled below ~22px the 1.5-2px source strokes drop under
    // a target pixel and grey out, so small glyphs read lighter than their 32px parents. Dilate
    // the coverage threshold by up to 0.35 target px (ramped in as px shrinks) to hold apparent
    // stroke weight constant across sizes. In encoded units t target px = t*srcPerTarget/(2*spread).
    float boldTargetPx = 0.0f;
    if (variant == Variant::Glyph && px < 22)
        boldTargetPx = ImMin(0.35f, (22.0f - (float)px) * 0.045f);
    // Halo = dilated backing: shift the coverage threshold outward (contract dilate d0 =
    // 0.5 - 2.5/spread) so the halo silhouette reads visibly larger/softer than the glyph on top.
    const float center = (variant == Variant::Halo)
                             ? (0.5f - 2.5f / kSpread)
                             : (0.5f - boldTargetPx * srcPerTarget / (2.0f * kSpread));

    std::vector<uint8_t> pixels((size_t)px * px * 4);
    for (int y = 0; y < px; ++y) {
        for (int x = 0; x < px; ++x) {
            const float u = ((float)x + 0.5f) / (float)px;
            const float v = ((float)y + 0.5f) / (float)px;
            const float d = sampleSdf(g, u, v) / 255.f;
            const float coverage = smoothstepAA(center - aa, center + aa, d);
            uint8_t* p = &pixels[((size_t)y * px + x) * 4];
            p[0] = p[1] = p[2] = 255;
            p[3] = (uint8_t)(coverage * 255.f + 0.5f);
        }
    }

    int ax = 0, ay = 0;
    allocCacheRect(px, px, ax, ay);
    glBindTexture(GL_TEXTURE_2D, gCacheTex);
    glTexSubImage2D(GL_TEXTURE_2D, 0, ax, ay, px, px, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
    glBindTexture(GL_TEXTURE_2D, 0);
    // Mirror into the CPU staging buffer too — it is the source copied forward on the next regrow.
    for (int y = 0; y < px; ++y) {
        std::memcpy(&gCacheCpu[((size_t)(ay + y) * gCacheW + ax) * 4], &pixels[(size_t)y * px * 4],
                    (size_t)px * 4);
    }

    const CacheEntry entry{(float)ax / (float)gCacheW, (float)ay / (float)gCacheH,
                            (float)(ax + px) / (float)gCacheW, (float)(ay + px) / (float)gCacheH};
    return gCache.emplace(key, entry).first->second;
}

}  // namespace

void build(const unsigned char* rgba, int atlasW, int atlasH,
           const std::unordered_map<std::string, CellUV>& cells,
           const std::unordered_set<std::string>& rasterOnly) {
    if (gSdfTex || !gGlyphs.empty()) return;  // idempotent — ui::loadIcons() also guards this
    if (!rgba || atlasW <= 0 || atlasH <= 0) return;

    struct Candidate { std::string name; int w, h; std::vector<uint8_t> mask; };

    // Every unique base icon name, preferring its @32 cell (fallback @20 — contract §3).
    std::unordered_map<std::string, const CellUV*> by32, by20;
    for (const auto& kv : cells) {
        const std::string& key = kv.first;
        const size_t at = key.rfind('@');
        if (at == std::string::npos) continue;
        const std::string base = key.substr(0, at);
        const std::string sz = key.substr(at + 1);
        if (sz == "32") by32[base] = &kv.second;
        else if (sz == "20") by20[base] = &kv.second;
    }

    std::vector<Candidate> candidates;
    candidates.reserve(by32.size());
    auto tryAdd = [&](const std::string& base, const CellUV& uv) {
        // Photographic/screenshot tiles: explicit raster-only opt-out — their neutral-grey
        // shading can pass the chroma gate, but an SDF re-raster would flatten them to a tinted
        // silhouette (contract: photographic icons stay multi-color raster).
        if (rasterOnly.count(base)) return;
        int px0 = (int)std::lround(uv.u0 * atlasW);
        int py0 = (int)std::lround(uv.v0 * atlasH);
        int pw  = (int)std::lround((uv.u1 - uv.u0) * atlasW);
        int ph  = (int)std::lround((uv.v1 - uv.v0) * atlasH);
        if (pw <= 0 || ph <= 0) return;
        px0 = std::max(0, std::min(px0, atlasW - 1));
        py0 = std::max(0, std::min(py0, atlasH - 1));
        pw = std::min(pw, atlasW - px0);
        ph = std::min(ph, atlasH - py0);

        int chromaMax = 0;
        bool anyOpaque = false;
        std::vector<uint8_t> mask((size_t)pw * ph, 0);
        for (int y = 0; y < ph; ++y) {
            for (int x = 0; x < pw; ++x) {
                const unsigned char* p = rgba + ((size_t)(py0 + y) * atlasW + (px0 + x)) * 4;
                const int a = p[3];
                if (a > 32) {
                    anyOpaque = true;
                    const int r = p[0], gr = p[1], b = p[2];
                    const int spread = std::max(r, std::max(gr, b)) - std::min(r, std::min(gr, b));
                    if (spread > chromaMax) chromaMax = spread;
                }
                mask[(size_t)y * pw + x] = (a >= 96) ? 1 : 0;
            }
        }
        // Monochrome-safe allowlist (contract §3): auto-excludes badges/axis-triads/appearance
        // swatches by their multi-hue content — no hardcoded name lists.
        if (!anyOpaque || chromaMax > 40) return;

        candidates.push_back(Candidate{base, pw, ph, std::move(mask)});
    };
    for (const auto& kv : by32) tryAdd(kv.first, *kv.second);
    for (const auto& kv : by20) if (!by32.count(kv.first)) tryAdd(kv.first, *kv.second);

    if (candidates.empty()) return;

    // Shelf-pack the qualifying glyphs into a fixed-width, grow-to-fit-height atlas — packed once
    // at load, so (unlike the re-raster cache) the final size is known up front.
    gSdfW = kSdfAtlasW;
    std::vector<std::pair<int, int>> placement(candidates.size());
    int cursorX = 0, shelfY = 0, shelfH = 0;
    for (size_t i = 0; i < candidates.size(); ++i) {
        const Candidate& c = candidates[i];
        if (cursorX + c.w > gSdfW) { shelfY += shelfH; cursorX = 0; shelfH = 0; }
        placement[i] = {cursorX, shelfY};
        cursorX += c.w;
        shelfH = std::max(shelfH, c.h);
    }
    gSdfH = shelfY + shelfH;
    if (gSdfH <= 0) return;
    gSdfCpu.assign((size_t)gSdfW * gSdfH, 0);

    gGlyphs.reserve(candidates.size());
    gNameToId.reserve(candidates.size());
    for (size_t i = 0; i < candidates.size(); ++i) {
        const Candidate& c = candidates[i];
        const int gx = placement[i].first, gy = placement[i].second;
        encodeSdf(c.mask.data(), c.w, c.h, gSdfCpu.data(), gSdfW, gx, gy);
        gNameToId[c.name] = (int)gGlyphs.size();
        gGlyphs.push_back(SdfGlyph{gx, gy, c.w, c.h});
    }

    // Upload as a single-channel R8 texture. gSdfW / row byte counts are not guaranteed 4-byte
    // aligned, so set UNPACK_ALIGNMENT=1 for this upload and restore the GL default (4)
    // immediately after — leaking a PACK/UNPACK alignment change would corrupt every other
    // texture upload/readback in the app.
    GLint prevUnpack = 4;
    glGetIntegerv(GL_UNPACK_ALIGNMENT, &prevUnpack);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glGenTextures(1, &gSdfTex);
    glBindTexture(GL_TEXTURE_2D, gSdfTex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, gSdfW, gSdfH, 0, GL_RED, GL_UNSIGNED_BYTE, gSdfCpu.data());
    glBindTexture(GL_TEXTURE_2D, 0);
    glPixelStorei(GL_UNPACK_ALIGNMENT, prevUnpack);
}

void shutdown() {
    if (gSdfTex) { glDeleteTextures(1, &gSdfTex); gSdfTex = 0; }
    if (gCacheTex) { glDeleteTextures(1, &gCacheTex); gCacheTex = 0; }
    gGlyphs.clear();
    gNameToId.clear();
    gSdfCpu.clear();
    gSdfW = gSdfH = 0;
    gCache.clear();
    gCacheCpu.clear();
    gCacheW = gCacheH = 0;
    gCacheCursorX = gCacheShelfY = gCacheShelfH = 0;
}

bool has(const char* name) {
    return name && gNameToId.find(name) != gNameToId.end();
}

bool draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col, ImU32 haloCol) {
    if (!dl || !name) return false;
    const auto it = gNameToId.find(name);
    if (it == gNameToId.end()) return false;

    const int id = it->second;
    const int px = std::max(1, (int)(size + 0.5f));
    const float r = size * 0.5f;
    const ImVec2 mn(c.x - r, c.y - r), mx(c.x + r, c.y + r);

    // Re-fetch gCacheTex per AddImage call (not hoisted): rasterize() may grow/recreate the cache
    // texture in between the halo and glyph passes below.
    if ((haloCol & IM_COL32_A_MASK) != 0) {
        const CacheEntry& halo = rasterize(id, px, Variant::Halo);
        dl->AddImage((ImTextureID)(intptr_t)gCacheTex, mn, mx,
                     ImVec2(halo.u0, halo.v0), ImVec2(halo.u1, halo.v1), haloCol);
    }
    const CacheEntry& glyph = rasterize(id, px, Variant::Glyph);
    dl->AddImage((ImTextureID)(intptr_t)gCacheTex, mn, mx,
                 ImVec2(glyph.u0, glyph.v0), ImVec2(glyph.u1, glyph.v1), col);
    return true;
}

}  // namespace icons_sdf
