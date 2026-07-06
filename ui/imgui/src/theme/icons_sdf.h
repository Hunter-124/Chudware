// SDF icon derivation + CPU re-raster cache (Liquid Glass v2, N3 contract §3).
// Internal to the icons_sdf/icons TUs — not part of the public ui:: / icons:: API surface
// (icons.h exposes icons::drawGlass / icons::hasSdf, which forward here).
#pragma once

#include "imgui.h"

#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace icons_sdf {

// UV rect within a raster atlas texture. Layout-identical to (and reused as) the private
// `ui::Cell` in icons.cpp: {u0,v0,u1,v1} in [0,1] atlas-normalized coordinates.
struct CellUV {
    float u0, v0, u1, v1;
};

// Builds SDF glyphs for every atlas cell that qualifies as monochrome-safe line art (chroma
// spread <= 40/255 among alpha>32 pixels of its @32 cell, falling back to @20). MUST be called
// once, synchronously, from ui::loadIcons() while `rgba` (the already stbi_load'd pixel buffer of
// ONE atlas image, e.g. atlas_dark.png) is still valid — SDF shape is theme-independent, so a
// single atlas image is enough. GUI thread only (uploads a GL_R8 texture).
// `rasterOnly` (atlas.json "raster_only", written by scripts/icon_pipeline.py) names glyphs that
// opt OUT of SDF derivation: their neutral-grey shading can pass the chroma gate, but an SDF
// re-raster would flatten the multi-color raster (e.g. CAM toolpath tiles) to a tinted silhouette
// — they keep the raster path per the T-G15/T-G16 shared visual contract.
void build(const unsigned char* rgba, int atlasW, int atlasH,
           const std::unordered_map<std::string, CellUV>& cells,
           const std::unordered_set<std::string>& rasterOnly = {});

// Deletes the SDF + CPU re-raster cache GL textures and clears all CPU state. Call from
// ui::freeIcons().
void shutdown();

// True when `name` (already alias-resolved by the caller) qualified for an SDF glyph at build().
bool has(const char* name);

// Renders `name` centered at `c`, `size` px square, tinted `col`, via the CPU re-raster cache
// (rasterized on first use per (name, roundedPx, halo) key, then reused). When `haloCol` has
// nonzero alpha, a soft dilated backing is drawn first, tinted `haloCol`, beneath the glyph.
// Returns false (draws nothing) when `name` has no SDF glyph — caller falls back to the raster
// atlas path.
bool draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col, ImU32 haloCol);

}  // namespace icons_sdf
