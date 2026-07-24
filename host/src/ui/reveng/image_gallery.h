// image_gallery.h — reverse-engineering capture contact sheet (RE-19; overhauled RE-40)
//
// WHAT THIS IS
// The gallery is the visual backbone of the capture stage: photogrammetry only works when the
// user can SEE the capture set and cull the bad frames out of it. So the panel is a rich contact
// sheet, not a thumbnail strip — responsive tile grid, hover magnifier with metadata, a full-size
// viewer, quality/synthetic badges, a filter + count summary, and per-image cull/reveal actions.
//
// HOUSE RULES (see reveng_widgets.h)
//   • Every visual comes from ui::reveng::kit — no bespoke cards, chips or empty states here.
//   • Sizes from ui::metrics::re*(); colours from ui::theme().palette. No literals of either.
//   • Thumbnails are DOWNSCALED decodes served from reveng::cachedThumbTex (img_tex.h). Only the
//     full-size viewer pays for a full-resolution decode.
//
// GL-thread only.
#pragma once

#include <string>
#include <vector>

struct AppState;

namespace ui {
namespace reveng {

// One entry in the gallery.
//
// `quality` vocabulary (set by the reveng_add_images / get_reveng_coverage responses — the
// gallery never invents it):
//   ""          unrated — not scored yet; renders WITHOUT a badge and counts as "unrated" in the
//               summary. Never draw an unrated frame as if it passed.
//   "ok"        sharp, kept.
//   "blurred"   below the sharpness threshold; a reconstruction-quality risk.
//   "duplicate" near-identical to another frame; adds cost, no parallax.
//   "synthetic" not a real photograph (stub-camera / generated frame). Dimensionally worthless.
//               Also settable via the `synthetic` flag, which wins over `quality`.
struct GalleryImage {
    std::string path;            // filesystem path
    std::string quality;         // see vocabulary above
    unsigned int texId = 0;      // THUMBNAIL texture, owned by reveng's thumb cache (0 = unloaded)
    int    thumbW = 0;           // uploaded thumbnail size (what texId actually is)
    int    thumbH = 0;
    int    srcW = 0;             // original image pixel size (metadata line: "4000 x 3000")
    int    srcH = 0;
    double sharpness = -1.0;     // variance-of-Laplacian blur score; < 0 = unknown
    bool   synthetic = false;    // frame came from a stub camera — badged loudly, never trusted
    bool   texFailed = false;    // decode failed; the tile shows a broken-file placeholder
};

// Per-frame outputs of drawImageGallery(). All three are CLEARED on entry, so a caller can keep
// one instance on the stack. The gallery performs the local/visual half of each action itself;
// these vectors exist so the caller can fire the matching worker RPC.
struct GalleryActions {
    // Paths dropped onto the grid (kFileDragPayload) or picked through the empty-state browse
    // button. Already appended to `images` as unrated entries — the caller should send them to
    // reveng_add_images and surface the result (which is what finally fills in `quality`).
    std::vector<std::string> dropped;

    // Paths the user culled. Already ERASED from `images` by the gallery. The reveng contract has
    // no remove-image method today, so the caller's job is to reflect the cull however it can
    // (toast / manifest rewrite / re-send of the surviving set) — never to assume it happened
    // worker-side.
    std::vector<std::string> removed;

    // Paths the user asked to show in the OS file manager. The gallery has ALREADY performed the
    // reveal; the vector is reported so the caller can log or toast it.
    std::vector<std::string> revealed;
};

// Draw the capture contact sheet.
//   session_id  may be empty — the gallery still renders (phone capture can produce photos before
//               a session exists) but says, in a Warn state, that nothing will be attached.
//   images      mutated in place: drops append, culls erase, thumbnails are filled in.
//   actions     output, see GalleryActions.
//
// Single-click selects a tile and writes RevEngState::selectedImage (the Scale stage's two-point
// picker and the stage hero preview both read it). Double-click opens the full-size viewer.
void drawImageGallery(AppState& st,
                      const std::string& session_id,
                      std::vector<GalleryImage>& images,
                      GalleryActions& actions);

// Fill in thumbnails for images that have a path but no texture yet, bounded to a few new decodes
// per frame so dropping 200 photos does not stall a frame. Cache hits are free and unbounded.
// Call once per frame from the draw path, before drawImageGallery.
void galleryLoadPendingThumbs(std::vector<GalleryImage>& images);

// Detach every thumbnail from `images` (session switch / teardown). Thumbnail textures live in
// the shared reveng thumb cache, so this releases the REFERENCES, not the textures — which is
// exactly what keeps a session switch from double-freeing a texture another view still shows.
// Use reveng::thumbCacheClear() (img_tex.h) for the real GL teardown.
void galleryFreeTextures(std::vector<GalleryImage>& images);

}  // namespace reveng
}  // namespace ui
