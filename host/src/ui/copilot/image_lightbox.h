#pragma once
#include <string>
#include "imgui.h"
struct AppState;
namespace ui {
namespace copilot {
// Call once at load time, before interactive work can exhaust the browser pthread pool.
// Image requests never create a worker lazily.
void initImageLoader();
// Background decode + bounded GL-thread LRU. Pending images return 0 and schedule another frame.
ImTextureID asyncImageTexture(const std::string& path, int maxEdge, int* outW, int* outH);
}
// Draw a square thumbnail of `path` at `sizePx`, falling back to a generic
// glyph/box when the image cannot be loaded. Returns true when CLICKED.
bool attachmentThumbnail(const std::string& path, float sizePx);
// Draw an aspect-preserving sent-message preview within the supplied bounds.
// Uses the thumbnail/viewer's texture cache; returns true when CLICKED.
bool attachmentImagePreview(const std::string& path, float maxWidth, float maxHeight);
// Request the full-size viewer for `path`. Safe to call any frame.
void openImageLightbox(const std::string& path);
// Draw the viewer if one is open. Call ONCE per frame, late, from the copilot
// panel. Handles Esc / click-outside / a close button internally.
void drawImageLightbox(AppState& st);
}
