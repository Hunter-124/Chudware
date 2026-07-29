#pragma once
#include <string>
struct AppState;
namespace ui {
// Draw a square thumbnail of `path` at `sizePx`, falling back to a generic
// glyph/box when the image cannot be loaded. Returns true when CLICKED.
bool attachmentThumbnail(const std::string& path, float sizePx);
// Request the full-size viewer for `path`. Safe to call any frame.
void openImageLightbox(const std::string& path);
// Draw the viewer if one is open. Call ONCE per frame, late, from the copilot
// panel. Handles Esc / click-outside / a close button internally.
void drawImageLightbox(AppState& st);
}
