// AI-CAD icon loader (C14).
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

// Call after applyTheme() — swaps the active texture ID so all subsequent getIcon()
// calls use the new atlas.  UVs stay constant.
void syncIconTheme();

}  // namespace ui
