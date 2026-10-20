#pragma once
#include <string>
struct AppState;
namespace ui {
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
