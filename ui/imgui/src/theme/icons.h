// AI-CAD icon loader (C14). Host-faithful atlas API (ui::) plus shell draw helpers (icons::).
// Loads atlas_dark.png / atlas_light.png + atlas.json from <resourcesDir>/icons/.
#pragma once

#include "imgui.h"
#include <string>

namespace ui {

struct IconUV {
    ImTextureID texId;
    ImVec2      uv0;
    ImVec2      uv1;
    bool        valid;
    IconUV() : texId((ImTextureID)0), uv0(0, 0), uv1(1, 1), valid(false) {}
};

// Load both atlases and parse atlas.json. Call once after GL context is live.
void loadIcons(const std::string& assetsDir);
void freeIcons();

// Query an icon. size should be 32 or 20 (falls back to the other size).
IconUV getIcon(const char* name, int size = 32);

// Call after theme::setDark() — getIcon() reads theme::ctx().dark each call.
void syncIconTheme();

}  // namespace ui

namespace icons {

// Shell convenience: loadIcons + optional theme hint (theme comes from theme::ctx() at query time).
void initAtlas(const std::string& resourcesDir, bool dark);
void setAtlasTheme(bool dark);

// Draw priority: hand-drawn chrome glyph → atlas (alias-resolved) → monogram placeholder.
void draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col);
void inlineIcon(const char* name, float size, ImU32 col);

}  // namespace icons
