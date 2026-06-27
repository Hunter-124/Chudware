// Icon rendering. Draws, in priority order:
//   1. hand-drawn vector glyphs for chrome (menu/search/window buttons/check/x/sparkle/...),
//   2. the reused pre-rendered CAD icon atlas (host/assets/icons, mapped by name),
//   3. a clean monogram placeholder (never magenta).
// Definitions live in icons.cpp so the atlas can own a GL texture. See app/resources/icons/ICONS.md.
#pragma once
#include <string>
#include "imgui.h"

namespace icons {

// Load the CAD icon atlas (atlas_<theme>.png + atlas.json) from <resourcesDir>/icons. GL context
// must be current. Safe to call once after init; no-op if the files are absent (falls back to glyphs).
void initAtlas(const std::string& resourcesDir, bool dark);
void setAtlasTheme(bool dark);   // swap dark/light atlas on theme change

// Draw an icon centered at `c`, fitting a `size`x`size` box, tinted `col` (atlas icons keep their
// baked color; hand-drawn glyphs honor `col`).
void draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col);

// Inline icon at the current cursor (advances layout by size).
void inlineIcon(const char* name, float size, ImU32 col);

} // namespace icons
