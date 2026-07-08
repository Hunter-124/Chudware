// image_gallery.h — thumbnail grid with drag-drop + quality badges (RE-19)
// Draws a scrollable ImGui child panel showing captured image thumbnails.
// Supports kFileDragPayload drops to add images from the OS file manager.
// Quality badges: green=ok, amber=blurred, red=duplicate.
// GL-thread only.
#pragma once

#include <string>
#include <vector>

struct AppState;

namespace ui {
namespace reveng {

// One entry in the gallery.
struct GalleryImage {
    std::string path;           // filesystem path
    std::string quality;        // "ok" | "blurred" | "duplicate" | ""
    unsigned int texId = 0;     // GL texture (0 = not loaded yet)
    int thumbW = 0;
    int thumbH = 0;
    bool texLoading = false;    // async load in-flight
};

// Draw the image gallery child panel.  session_id may be empty (gallery shows but
// the "Add to session" async call is suppressed).
// dropped_paths is an output: paths dropped via drag-drop this frame (caller handles RPC).
void drawImageGallery(AppState& st,
                      const std::string& session_id,
                      std::vector<GalleryImage>& images,
                      std::vector<std::string>& dropped_paths);

// Async-load thumbnails for images that have path set but texId==0.
// Call once per frame from the draw path before drawImageGallery.
void galleryLoadPendingThumbs(std::vector<GalleryImage>& images);

// Release all GL textures held by the gallery.
void galleryFreeTextures(std::vector<GalleryImage>& images);

}  // namespace reveng
}  // namespace ui
