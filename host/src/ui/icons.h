// Chudware icon loader (C14).
// Loads atlas_dark.png / atlas_light.png + atlas.json at startup (stb_image vendored).
// ui::Icon(name, size) -> IconUV{ImTextureID, uv0, uv1}.
// Theme switch swaps the active texture; UVs are constant.
// Missing name -> magenta placeholder + one console warning per unknown name.
#pragma once

#include "imgui.h"
#include <string>

namespace ui {

struct IconUV {
    ImTextureID texId;
    ImVec2      uv0;
    ImVec2      uv1;
    bool        valid;
    IconUV() : texId((ImTextureID)0), uv0(0,0), uv1(1,1), valid(false) {}
};

// Load both atlases and parse atlas.json.  Call once after GL context is live,
// before the first frame.  assetsDir = the directory that contains icons/.
void loadIcons(const std::string& assetsDir);

// Release GL textures.  Call before ImGui/GL teardown.
void freeIcons();

// Query an icon.  size should be 32 or 20.
// Returns a placeholder (magenta) and emits one stderr warning if name is unknown.
IconUV getIcon(const char* name, int size = 32);

// Maps a referenced icon name to an existing atlas glyph when it has no dedicated cell (single
// source of truth for icon fallbacks; see icons.cpp). Returns `name` unchanged if not aliased.
const char* resolveIconAlias(const char* name);

// Call after applyTheme() — swaps the active texture ID so all subsequent getIcon()
// calls use the new atlas.  UVs stay constant.
void syncIconTheme();

// Retint every atlas cell listed in atlas.json "accent_tinted" from the reference hues it
// was baked with (atlas.json "accent_ref") to the caller's colors. Idempotent: always
// recomputed from a pristine copy of the decoded atlas, never from the previous result.
// rgb arrays are 3 floats in 0..1.
//
// This is the ONLY path by which a user-chosen accent reaches the engine-rendered ribbon
// tiles: they are photographic bakes, so icons_sdf.cpp excludes them (raster_only) from the
// tintable SDF path and ribbon.cpp draws them with an identity white tint. Passing the bake
// reference colors back in restores the pristine atlas. No-op on an atlas whose json predates
// the accent metadata. GUI thread only (touches GL).
void recolorIconAtlas(const float primary[3], const float secondary[3]);

// Liquid Glass v2 host port (H3 contract): SDF icon path derived at load time from the
// already-decoded atlas alpha of monochrome-safe line-art glyphs (see icons_sdf.cpp). Renders
// crisp at any pixel size via a CPU re-raster cache; falls back to the raster atlas for
// multi-color glyphs that did not qualify.

// True when `name` renders via the SDF path instead of the raster atlas.
bool hasIconSdf(const char* name);

// SDF-crisp draw with an optional contrast halo (soft dilated backing tinted `haloCol`, drawn
// beneath the glyph) — for icons painted directly over the frosted viewport backdrop. haloCol
// alpha 0 => no halo. Falls back to a plain getIcon()+AddImage draw (no halo) when `name` has no
// SDF glyph (multi-color raster icons keep their raster path).
void drawIconGlass(ImDrawList* dl, const char* name, ImVec2 center, float sizePx, ImU32 col,
                    ImU32 haloCol);

}  // namespace ui
