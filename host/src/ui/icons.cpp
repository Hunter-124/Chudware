// Chudware icon loader — C14.
// Uses stb_image (already vendored via stb_impl.cpp).  nlohmann/json for atlas.json.
// GOTCHA: never add host/src to the include path; use relative includes.
#include "icons.h"
#include "icons_sdf.h"

#include "gl_compat.h"
#include "imgui.h"

#include "stb_image.h"

#include <nlohmann/json.hpp>

#include "../ui_theme.h"  // ui::activeTheme, ui::Theme

#include <cmath>
#include <cstdio>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#include <cstring>

namespace ui {

// --- internal types --------------------------------------------------------

// UV rect within a raster atlas texture — shared layout with icons_sdf::CellUV (icons_sdf.cpp
// samples pixel data from the same atlas.json cells to derive SDF glyphs).
using Cell = icons_sdf::CellUV;

// Pixel rect of one atlas cell. The retint indexes texels, so it needs the integer rect from
// atlas.json, not the normalized UVs Cell carries.
struct PxRect { int x, y, w, h; };

struct AtlasState {
    GLuint texDark  = 0;
    GLuint texLight = 0;
    int    atlasW   = 0;
    int    atlasH   = 0;

    // placeholder: 1x1 magenta texture
    GLuint texPlaceholder = 0;

    std::unordered_map<std::string, Cell> cells;  // key = "name@32" or "name@20"
    bool loaded = false;

    // --- accent retint state (recolorIconAtlas) -----------------------------
    // Both decoded atlases are retained for the life of the process instead of being freed
    // right after upload. 280x1904x4 twice is ~4.3 MB — negligible against the app's footprint,
    // and far cheaper than re-running stbi_load on a PNG for every frame of a color-picker
    // drag. More importantly it is what makes the retint idempotent: every recolor is computed
    // from these ORIGINAL bake texels, never from the previous recolor's output. That is not a
    // theoretical nicety — re-running the rotation over its own output once, measured on the
    // real extrude_boss cells, moves accent texels a mean of 7 and a worst of 198 per channel,
    // because a second pass rotates an already-rotated hue again. A live color-picker drag fires
    // this dozens of times, and chaining would walk the accent right off its hue.
    std::vector<unsigned char> pristineDark, pristineLight;
    std::vector<unsigned char> scratch;       // recolor destination, reused across calls
    std::vector<PxRect>        accentRects;   // @32/@20 rects of the accent_tinted tiles
    float refPrimary[3]   = {0, 0, 0};        // atlas.json accent_ref
    float refSecondary[3] = {0, 0, 0};
    bool  accentMeta    = false;  // atlas.json carried usable accent_tinted + accent_ref
    bool  accentApplied = false;  // GL texels currently differ from the pristine decode
    // The startup order is applyTheme() -> recolorIconAtlas() -> ... -> loadIcons(), so the
    // first request for the user's saved accent arrives before there is anything to retint.
    // Recording it here and replaying it at the end of loadIcons() is what stops a user who
    // picked their own colors from booting to orange tiles until they touch the picker.
    float reqPrimary[3]   = {0, 0, 0};
    float reqSecondary[3] = {0, 0, 0};
    bool  accentRequested = false;
};

static AtlasState gAtlas;
static std::unordered_set<std::string> gWarnedMissing;

// ---------------------------------------------------------------------------

// Upload one atlas image. Returns 0 when the driver refused it.
//
// The size check is not paranoia. The atlas used to be packed as a single 56 px
// column, so it grew one 34 px row per icon and reached 9418 px tall — past the
// 8192 GL_MAX_TEXTURE_SIZE that ANGLE/SwiftShader and plenty of real GPUs report,
// and far past the 2048 that GLES3/WebGL2 merely GUARANTEE. glTexImage2D then
// failed with GL_INVALID_VALUE, the texture stayed incomplete, and every icon
// sampled as opaque black. Nothing in any log mentioned icons, which is why it
// took a pixel inspection to find. An oversized atlas now says so, by name.
static GLuint uploadTexture(unsigned char* px, int w, int h, const char* what) {
    GLint maxTex = 0;
    glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxTex);
    if (maxTex > 0 && (w > maxTex || h > maxTex)) {
        std::fprintf(stderr,
                     "[icons] %s is %dx%d but GL_MAX_TEXTURE_SIZE is %d — every icon "
                     "would draw as a black square. Re-pack the atlas "
                     "(python3 assets/make_icons.py --relayout).\n",
                     what, w, h, (int)maxTex);
        return 0;
    }
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    // A driver that rejected the level leaves an incomplete texture that samples
    // black; better to fall through to the magenta placeholder than to ship that.
    if (GLenum err = glGetError(); err != GL_NO_ERROR) {
        std::fprintf(stderr, "[icons] %s upload failed (%dx%d, GL error 0x%04X)\n",
                     what, w, h, (unsigned)err);
        glBindTexture(GL_TEXTURE_2D, 0);
        glDeleteTextures(1, &tex);
        return 0;
    }
    glBindTexture(GL_TEXTURE_2D, 0);
    return tex;
}

// Rewrite the texels of an ALREADY-uploaded atlas, keeping the same GL texture name.
// A retint must never allocate a second texture: the ImTextureID handed out by getIcon() is
// baked into every ImDrawCmd the ribbon emitted this frame, so swapping names mid-frame would
// draw stale art, and doing it per color-picker frame would churn GL names for nothing.
static void refreshTexture(GLuint tex, const std::vector<unsigned char>& px) {
    if (!tex || px.empty()) return;
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, gAtlas.atlasW, gAtlas.atlasH, GL_RGBA,
                    GL_UNSIGNED_BYTE, px.data());
    glBindTexture(GL_TEXTURE_2D, 0);
}

// atlas.json accent_ref colors are 3-element 0..1 float arrays. A malformed entry disables the
// retint entirely rather than letting a half-read reference rotate hues to garbage.
static bool readRgb(const nlohmann::json& j, const char* key, float out[3]) {
    if (!j.contains(key) || !j[key].is_array() || j[key].size() != 3) return false;
    for (int i = 0; i < 3; ++i) {
        if (!j[key][i].is_number()) return false;
        out[i] = j[key][i].get<float>();
    }
    return true;
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
        // photographic/screenshot tiles opt out of SDF derivation (see icons_sdf.h).
        if (jroot.contains("raster_only") && jroot["raster_only"].is_array())
            for (const auto& n : jroot["raster_only"])
                if (n.is_string()) rasterOnly.insert(n.get<std::string>());

        // Accent retint metadata, written by scripts/icon_pipeline.py::inject(): which rendered
        // tiles carry an accent role, and the reference colors their bodies were baked with.
        // BOTH keys are optional and a missing/malformed one only disables recolorIconAtlas() —
        // an atlas baked before this metadata existed must still load and draw its icons, just
        // permanently in the shipped colors.
        if (jroot.contains("accent_tinted") && jroot["accent_tinted"].is_array() &&
            jroot.contains("accent_ref") && jroot["accent_ref"].is_object() &&
            readRgb(jroot["accent_ref"], "primary", gAtlas.refPrimary) &&
            readRgb(jroot["accent_ref"], "secondary", gAtlas.refSecondary)) {
            // Resolve the tile names to integer cell rects once, here. The retint then walks a
            // flat rect list instead of doing a string build + two hash lookups per tinted tile
            // on every frame of a color-picker drag.
            for (const auto& n : jroot["accent_tinted"]) {
                if (!n.is_string()) continue;
                const std::string base = n.get<std::string>();
                for (int size : {kIconCellLarge, kIconCellSmall}) {
                    const std::string key = base + "@" + std::to_string(size);
                    if (!cells.contains(key)) continue;
                    const auto& cell = cells.at(key);
                    if (!cell.is_object() || !cell.contains("x")) continue;
                    PxRect r{cell["x"].get<int>(), cell["y"].get<int>(),
                             cell["w"].get<int>(), cell["h"].get<int>()};
                    // A rect that escapes the atlas would have the retint write outside the
                    // decoded buffer; drop it rather than trust the json.
                    if (r.x < 0 || r.y < 0 || r.w <= 0 || r.h <= 0 ||
                        r.x + r.w > aw || r.y + r.h > ah)
                        continue;
                    gAtlas.accentRects.push_back(r);
                }
            }
            gAtlas.accentMeta = !gAtlas.accentRects.empty();
        }
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
            gAtlas.texDark = uploadTexture(px, w, h, "atlas_dark.png");
            // Retain the decode for recolorIconAtlas(), but only when the image really matches
            // atlas_px: the accent rects were resolved against those dimensions, so retinting a
            // differently-sized decode would rewrite arbitrary texels.
            if (gAtlas.texDark && w == gAtlas.atlasW && h == gAtlas.atlasH)
                gAtlas.pristineDark.assign(px, px + (size_t)w * h * 4);
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
            gAtlas.texLight = uploadTexture(px, w, h, "atlas_light.png");
            if (gAtlas.texLight && w == gAtlas.atlasW && h == gAtlas.atlasH)
                gAtlas.pristineLight.assign(px, px + (size_t)w * h * 4);
            stbi_image_free(px);
        } else {
            std::fprintf(stderr, "[icons] failed to load atlas_light.png: %s\n", stbi_failure_reason());
        }
    }

    // --- placeholder: 1x1 magenta ---
    {
        unsigned char mag[4] = {255, 0, 255, 255};
        gAtlas.texPlaceholder = uploadTexture(mag, 1, 1, "icon placeholder");
    }

    gAtlas.loaded = true;

    // Replay an accent request that arrived before the atlas existed (applyTheme() runs at
    // startup ahead of loadIcons()). No-op when nobody asked for anything but the bake colors.
    if (gAtlas.accentRequested)
        recolorIconAtlas(gAtlas.reqPrimary, gAtlas.reqSecondary);
}

void freeIcons() {
    icons_sdf::shutdown();
    if (gAtlas.texDark)        { glDeleteTextures(1, &gAtlas.texDark);        gAtlas.texDark = 0; }
    if (gAtlas.texLight)       { glDeleteTextures(1, &gAtlas.texLight);       gAtlas.texLight = 0; }
    if (gAtlas.texPlaceholder) { glDeleteTextures(1, &gAtlas.texPlaceholder); gAtlas.texPlaceholder = 0; }
    gAtlas.cells.clear();
    gAtlas.pristineDark.clear();
    gAtlas.pristineLight.clear();
    gAtlas.scratch.clear();
    gAtlas.accentRects.clear();
    gAtlas.accentMeta    = false;
    gAtlas.accentApplied = false;
    gAtlas.loaded = false;
}

// Icon aliases: names referenced by the ribbon/toolbar that draw another atlas glyph instead of their
// own cell (or lack one), so nothing ever falls to the magenta placeholder. This table
// is the single source of truth for those fallbacks; the atlas-coverage guard (assets/test_icons.py)
// treats an aliased name as "covered". Prefer adding a real rendered tile (scripts/icon_pipeline.py)
// over a permanent alias — replace the entry here once the dedicated glyph lands.
const char* resolveIconAlias(const char* name) {
    if (!name) return name;
    struct Alias { const char* from; const char* to; };
    static const Alias kAliases[] = {
        // Both names also have rendered atlas tiles (contracts/icons.md, 2026-08-17); these rows
        // shadow them and keep the established ribbon look until those tiles are visually signed off.
        {"rotate_ent",          "pattern_circ_ent"},  // Sketch▸Rotate Ent.
        {"stock",               "cat_stock_metal"},   // Manufacture▸Stock — metal-block tile
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
        int altSize = (size == kIconCellLarge) ? kIconCellSmall : kIconCellLarge;
        char altKey[128];
        std::snprintf(altKey, sizeof(altKey), "%s@%d", name, altSize);
        it = gAtlas.cells.find(altKey);
    }

    if (it == gAtlas.cells.end()) {
#ifdef __EMSCRIPTEN__
        // NOT a subset: host/CMakeLists.txt --preload-file's the very same
        // assets/icons/atlas.json + atlas_{dark,light}.png the desktop reads off
        // disk, so both targets load a byte-identical atlas. A miss here is a
        // genuine contract bug on BOTH targets (a name with no cell), never an
        // expected web gap — an earlier version of this comment claimed "web
        // ships a subset of desktop tiles", and that false licence is exactly
        // why a real divergence went unquestioned for ten days: prod served an
        // atlas linked 2026-08-07 while 69690e57 (2026-08-17) had replaced every
        // ribbon tool icon with an engine-rendered 3D tile. Byte-parity between a
        // linked bundle and the repo is now enforced by
        // scripts/check_web_parity.py; staleness is that gate's job, not a thing
        // to shrug at here. The web branch stays silent purely on console-spam
        // volume — a missing name is hit every frame by every ribbon draw and
        // would flood devtools during boot; it still falls through to procedural
        // TbIcon art via valid=false, exactly as on desktop.
        gWarnedMissing.insert(name);
#else
        if (gWarnedMissing.find(name) == gWarnedMissing.end()) {
            std::fprintf(stderr, "[icons] unknown icon: '%s' (size %d)\n", name, size);
            gWarnedMissing.insert(name);
        }
#endif
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
    // The accent retint deliberately needs nothing here either: recolorIconAtlas() rewrites BOTH
    // atlas textures, so whichever one this swap makes active already carries the user's colors.
}

// --- accent retint ---------------------------------------------------------

// Hue in degrees [0,360) — ImGui's converters use 0..1 for hue, which is inconvenient to reason
// about when the whole algorithm is stated in degrees of rotation.
struct Hsv { float h, s, v; };

static inline Hsv toHsv(float r, float g, float b) {
    float h, s, v;
    ImGui::ColorConvertRGBtoHSV(r, g, b, h, s, v);
    return {h * 360.0f, s, v};
}

static inline float wrapHue(float h) {
    h = std::fmod(h, 360.0f);
    return h < 0.0f ? h + 360.0f : h;
}

// This TU deliberately includes only imgui.h (ImClamp lives in imgui_internal.h), and every
// clamp the retint needs is to the unit range anyway.
static inline float clamp01(float x) { return x < 0.0f ? 0.0f : (x > 1.0f ? 1.0f : x); }

// Signed shortest way round the hue circle, in [-180,180]. Every hue arithmetic step below goes
// through this: a plain subtraction is wrong across the 360/0 seam, which is exactly where the
// orange reference sits.
static inline float hueDelta(float from, float to) {
    const float d = wrapHue(to - from);
    return d > 180.0f ? d - 360.0f : d;
}

static inline float smoothstep(float lo, float hi, float x) {
    if (hi <= lo) return x < lo ? 0.0f : 1.0f;
    const float t = clamp01((x - lo) / (hi - lo));
    return t * t * (3.0f - 2.0f * t);
}

// Byte-resolution equality: anything closer than half a 8-bit step cannot change a texel.
static inline bool sameRgb(const float a[3], const float b[3]) {
    for (int i = 0; i < 3; ++i)
        if (std::fabs(a[i] - b[i]) > 1.0f / 512.0f) return false;
    return true;
}

// Saturation window that separates "an accent role painted this texel" from "this is stock and
// shading". The recipes paint stock as steel grey (S ~= 0.05) and both accent roles at S >= 0.85,
// so saturation alone classifies every texel and no per-pixel mask has to be baked or shipped.
//
// The window is a smoothstep rather than a hard cutoff, and that is load-bearing: an accent body
// is anti-aliased against the stock and against transparency, so its edge texels land mid-window.
// A threshold there leaves a one-texel fringe of the OLD hue ringing every retinted body, which
// at 32 px reads as a dirty outline rather than as anti-aliasing.
static constexpr float kAccentSatLo = 0.15f;
static constexpr float kAccentSatHi = 0.32f;

// Rotate the accent texels of one decoded atlas from the bake references to the target colors.
// `pristine` is never modified — see AtlasState for why every recolor restarts from the bake.
static void recolorAtlasPixels(const std::vector<unsigned char>& pristine,
                               std::vector<unsigned char>& out,
                               const Hsv ref[2], const Hsv target[2]) {
    out = pristine;  // every non-accent cell then stays byte-identical to the shipped atlas
    const size_t stride = (size_t)gAtlas.atlasW * 4;
    for (const PxRect& r : gAtlas.accentRects) {
        for (int y = r.y; y < r.y + r.h; ++y) {
            unsigned char* p = out.data() + (size_t)y * stride + (size_t)r.x * 4;
            for (int x = 0; x < r.w; ++x, p += 4) {
                if (p[3] == 0) continue;  // fully transparent: no color to rotate
                const Hsv px = toHsv(p[0] / 255.0f, p[1] / 255.0f, p[2] / 255.0f);
                const float w = smoothstep(kAccentSatLo, kAccentSatHi, px.s);
                if (w <= 0.0f) continue;  // stock steel, shadow, neutral shading — leave alone

                // Which role painted it? Nearest reference by circular hue distance. The two
                // references are ~185 deg apart by construction (assets/test_icons.py fails the
                // build if a future pair drops under 90), so this cannot realistically
                // misclassify, and it costs no extra baked metadata.
                const int role = std::fabs(hueDelta(ref[0].h, px.h)) <=
                                 std::fabs(hueDelta(ref[1].h, px.h)) ? 0 : 1;

                // Rigid ROTATION, not assignment. The render's own hue variation — specular
                // highlights and bounce light pull an accent body several degrees either side of
                // the material hue — is what makes the tile read as a lit 3D solid. Flattening
                // every accent texel onto the target hue instead turns it into a paper cut-out.
                const float rotH = wrapHue(target[role].h + hueDelta(ref[role].h, px.h));
                // S and V scale rather than rotate: a target that is darker or less saturated
                // than the reference has to darken the whole body, highlights included.
                const float sK = ref[role].s > 0.0f ? target[role].s / ref[role].s : 1.0f;
                const float vK = ref[role].v > 0.0f ? target[role].v / ref[role].v : 1.0f;
                const float rotS = clamp01(px.s * sK);
                const float rotV = clamp01(px.v * vK);

                // Blend back toward the original by w so half-saturated edge texels move only
                // half way. Hue is blended the short way round the circle for the same reason
                // the classifier is circular: lerping 350 -> 10 the long way sweeps through green.
                const float outH = wrapHue(px.h + hueDelta(px.h, rotH) * w);
                const float outS = px.s + (rotS - px.s) * w;
                const float outV = px.v + (rotV - px.v) * w;

                float rr, gg, bb;
                ImGui::ColorConvertHSVtoRGB(outH / 360.0f, outS, outV, rr, gg, bb);
                p[0] = (unsigned char)(clamp01(rr) * 255.0f + 0.5f);
                p[1] = (unsigned char)(clamp01(gg) * 255.0f + 0.5f);
                p[2] = (unsigned char)(clamp01(bb) * 255.0f + 0.5f);
                // p[3] untouched: alpha is the tile's silhouette, not part of its color, and
                // rewriting it would erode the anti-aliased rim.
            }
        }
    }
}

void recolorIconAtlas(const float primary[3], const float secondary[3]) {
    if (!primary || !secondary) return;

    // Remember the request even when it cannot be served yet: applyTheme() legitimately runs
    // before loadIcons() at startup, and loadIcons() replays this at the end of its own load.
    std::memcpy(gAtlas.reqPrimary, primary, sizeof(gAtlas.reqPrimary));
    std::memcpy(gAtlas.reqSecondary, secondary, sizeof(gAtlas.reqSecondary));
    gAtlas.accentRequested = true;

    // Nothing decoded yet, or an atlas whose json predates the accent metadata: silently do
    // nothing. Both are normal states, not errors — the caller must be free to fire this from
    // applyTheme() without knowing the icon load order or the atlas vintage.
    if (!gAtlas.loaded || !gAtlas.accentMeta) return;
    if (gAtlas.pristineDark.empty() && gAtlas.pristineLight.empty()) return;

    // The overwhelmingly common case is a user who never changed the colors, so comparing
    // against the bake reference short-circuits the entire pass; it only pays an upload when a
    // previous retint is actually being undone.
    if (sameRgb(primary, gAtlas.refPrimary) && sameRgb(secondary, gAtlas.refSecondary)) {
        if (!gAtlas.accentApplied) return;
        refreshTexture(gAtlas.texDark, gAtlas.pristineDark);
        refreshTexture(gAtlas.texLight, gAtlas.pristineLight);
        gAtlas.accentApplied = false;
        return;
    }

    const Hsv ref[2]    = {toHsv(gAtlas.refPrimary[0], gAtlas.refPrimary[1], gAtlas.refPrimary[2]),
                           toHsv(gAtlas.refSecondary[0], gAtlas.refSecondary[1], gAtlas.refSecondary[2])};
    const Hsv target[2] = {toHsv(primary[0], primary[1], primary[2]),
                           toHsv(secondary[0], secondary[1], secondary[2])};

    // Both atlases are retinted here rather than only the one the active theme happens to bind.
    // syncIconTheme() is a no-op by design (getIcon() picks texDark/texLight off activeTheme on
    // every call), so there is no single "bound" atlas to lazily fix up; a deferred scheme would
    // have to remember the last colors AND hook the theme switch, and getting either wrong shows
    // up as one theme quietly keeping the old accent. Doing both up front removes that state.
    //
    // Cost: ~170 tinted tiles x (32^2 + 20^2) = ~240k texels per atlas, ~480k for the pair, plus
    // two ~2 MB memcpys and two glTexSubImage2D uploads. Sub-millisecond, so calling this on
    // every frame of a color-picker drag for a live preview is entirely fine.
    if (!gAtlas.pristineDark.empty()) {
        recolorAtlasPixels(gAtlas.pristineDark, gAtlas.scratch, ref, target);
        refreshTexture(gAtlas.texDark, gAtlas.scratch);
    }
    if (!gAtlas.pristineLight.empty()) {
        recolorAtlasPixels(gAtlas.pristineLight, gAtlas.scratch, ref, target);
        refreshTexture(gAtlas.texLight, gAtlas.scratch);
    }
    gAtlas.accentApplied = true;
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
    const int cellSize = (sizePx >= 24.f) ? kIconCellLarge : kIconCellSmall;
    const IconUV ic = getIcon(name, cellSize);
    const float r = sizePx * 0.5f;
    dl->AddImage(ic.texId, ImVec2(center.x - r, center.y - r), ImVec2(center.x + r, center.y + r),
                 ic.uv0, ic.uv1, col);
}

}  // namespace ui
