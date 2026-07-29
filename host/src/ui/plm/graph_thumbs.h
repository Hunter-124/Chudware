// PLM Graph — on-demand iso thumbnail cache for the Grid view.
//
// Disk cache lives at <project>/.plm/thumbs/<key>.png where key is the doc's
// content hash (cad_graph meta.dh, blake2b-8 of document.json) so a doc edit
// naturally keys a NEW file and a warm cache loads with zero regeneration.
// When dh is absent (graph not yet rebuilt by the worker) the key falls back
// to a hash of abs path + mtime (meta.mt, else the file's own mtime).
//
// Misses enqueue ONE worker render per key per session (doc_open ->
// doc_render_png annotate=false -> doc_close), driven one job at a time from
// graphThumbsPump() on the frame loop — the GL thread never blocks and at
// most one render is in flight. Loaded textures sit in a 256-entry LRU.
#pragma once

#include <string>

struct AppState;

namespace ui {

struct GraphGridThumb {
  unsigned int tex = 0; // GL texture id (0 while pending/failed)
  int w = 0, h = 0;
  bool failed = false;  // render/decode failed this session — keep placeholder
  bool pending = false; // queued or rendering right now
};

// PNG file -> GL_RGBA8 texture (stb_image, 4 channels forced, linear filter,
// clamp-to-edge). Returns false and leaves `tex` untouched when the file is
// missing or undecodable. Shared with the PLM Compare page, which loads the
// diff renders the plm_compare RPC writes to disk.
bool plmLoadPngTexture(const std::string& path, unsigned int& tex, int& w, int& h);
// Delete a texture created by plmLoadPngTexture and zero the handle. No-op on 0.
void plmFreePngTexture(unsigned int& tex);

// Content key for one doc node. Empty only when there is no dh AND no path
// (nothing to render from).
std::string graphThumbKey(const std::string& dh, double mt,
                          const std::string& absPath);

// Look up (or kick off) the thumbnail for one doc. A cached file loads
// synchronously (cheap: one stat + decode on first touch, then texture-map
// hits). A missing file enqueues a worker render once per key per session and
// returns pending — callers always paint their placeholder immediately.
GraphGridThumb graphThumbEnsure(AppState& st, const std::string& projectDir,
                                const std::string& key,
                                const std::string& absPath);

// Drive at most one queued render per call; call once per frame from the
// graph page. No-op when the queue is empty, a job is in flight, or the
// worker is down (jobs wait).
void graphThumbsPump(AppState& st);

// Free every texture + drop session enqueue state (project switch).
void graphThumbsReset();

} // namespace ui
