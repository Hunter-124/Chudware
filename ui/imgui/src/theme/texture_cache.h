// Preview PNG -> GL texture cache (T-F16). Backs the universal preview pane's page images and
// artifact-card thumbnails. Same stbi_load + glTexImage2D(GL_RGBA) upload pattern as
// theme/icons.cpp's uploadTexture(); a plain unordered_map<path,GLuint> keeps re-renders and
// re-focused cards from re-decoding the PNG every frame.
#pragma once

#include "imgui.h"
#include <string>

namespace ui {

// Load (or fetch from cache) the texture for an absolute PNG path. Returns (ImTextureID)0 on
// failure (missing file / decode error) — callers show a placeholder instead of ImGui::Image().
// outW/outH (if non-null) receive the pixel dimensions on success. force=true evicts any cached
// texture first and reloads from disk (Regenerate / refresh).
ImTextureID loadPreviewTexture(const std::string& absPath, int* outW = nullptr, int* outH = nullptr,
                                bool force = false);

// Drop one cached texture (frees the GL texture). Safe to call for a path that isn't cached.
void evictPreviewTexture(const std::string& absPath);

// Free every cached preview texture (call on shutdown while the GL context is still current).
void clearPreviewTextures();

}  // namespace ui
