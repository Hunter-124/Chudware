#pragma once
// img_tex.h — decode JPEG/PNG to an OpenGL texture (RE-08; thumbnail path + LRU cache RE-40)
// Requires an active GL context. Uses stb_image / stb_image_resize2 (compiled via stb_impl.cpp).
// GL-thread only.
#include <cstdint>
#include <string>
#include <vector>

namespace reveng {

// ---------------------------------------------------------------------------
// Full-resolution path (RE-08) — hero preview / full-size viewer only.
// ---------------------------------------------------------------------------

// Load a JPEG or PNG from disk into an RGBA GL_TEXTURE_2D.
// Returns the texture id (>0) on success; 0 on failure.
// Caller must call freeTexture() when done.
// A 4000x3000 photo becomes a ~48 MB texture here — use loadThumbTexFromFile() /
// cachedThumbTex() for anything drawn at contact-sheet size.
unsigned int loadTexFromFile(const std::string& path, int* outW = nullptr, int* outH = nullptr);

// Load a JPEG or PNG from an in-memory buffer.
unsigned int loadTexFromMemory(const unsigned char* data, int dataLen,
                               int* outW = nullptr, int* outH = nullptr);

// Decode JPEG/PNG from disk into a raw RGBA pixel buffer (no GL).
// Returns false on failure. Pixels are stbi-allocated; call freePixels().
bool decodeFile(const std::string& path, std::vector<unsigned char>& pixels,
                int& w, int& h);

// Decode JPEG/PNG from memory into a raw RGBA pixel buffer (no GL).
bool decodeMemory(const unsigned char* data, int dataLen,
                  std::vector<unsigned char>& pixels, int& w, int& h);

// Delete a GL texture previously returned by loadTex*.
// NO-OP for ids currently owned by the thumbnail cache (see thumbCacheOwns) — that is what
// keeps galleryFreeTextures() from double-freeing a texture the cache still hands out.
void freeTexture(unsigned int texId);

// ---------------------------------------------------------------------------
// Thumbnail path + LRU cache (RE-40)
// ---------------------------------------------------------------------------
// WHY: the reveng contact sheet shows dozens of full-resolution phone/webcam JPEGs at
// ~104 logical px. Decoding each to a full-size GL texture cost tens of MB of VRAM per
// image and re-decoded the whole set on every session switch and every rebuild of the
// gallery vector. The thumbnail path decodes once, resamples to a bounded edge with
// stb_image_resize2 and uploads a small texture; the LRU keeps the last N alive so
// scrolling and session switching are free.

// Decode `path` and downscale so neither edge exceeds `maxEdgePx` (values <= 0 mean "no
// bound"), then upload as RGBA. Images already inside the bound upload unscaled.
//   outW/outH     — the UPLOADED (thumbnail) size, i.e. what to draw with.
//   outSrcW/outSrcH — the ORIGINAL pixel size, for the metadata line ("4000 x 3000").
// Returns 0 on failure. NOT cached: the caller owns the id and must freeTexture() it.
unsigned int loadThumbTexFromFile(const std::string& path, int maxEdgePx,
                                  int* outW = nullptr, int* outH = nullptr,
                                  int* outSrcW = nullptr, int* outSrcH = nullptr);

// Cached form of loadThumbTexFromFile(), keyed by (path, maxEdgePx). Repeated calls with the
// same key return the same texture id and refresh its LRU position. The returned id is owned
// by the cache: NEVER glDeleteTextures() it and NEVER hold it across thumbCacheClear().
// Passing it to freeTexture() is safe (that call is a documented no-op for cached ids).
// A failed decode is NOT cached, so a file that appears later still loads.
unsigned int cachedThumbTex(const std::string& path, int maxEdgePx,
                            int* outW = nullptr, int* outH = nullptr,
                            int* outSrcW = nullptr, int* outSrcH = nullptr);

// Eviction bound: the cache holds at most this many textures, dropping the least recently
// used first. Default kThumbCacheDefaultLimit. Lowering the limit evicts immediately.
void setThumbCacheLimit(int maxEntries);
int  thumbCacheLimit();
constexpr int kThumbCacheDefaultLimit = 192;

// Live entry count (tests / diagnostics).
int  thumbCacheCount();

// True when (path, maxEdgePx) is already resident. Lets a caller distinguish a free cache hit
// from a decode it must budget for, without paying the decode to find out.
bool thumbCacheHas(const std::string& path, int maxEdgePx);

// True when `texId` is currently owned by the thumbnail cache. Ownership checks exist so a
// panel that stores a mix of cached and self-owned texture ids can tear down safely.
bool thumbCacheOwns(unsigned int texId);

// Exact staleness test: true when (path, maxEdgePx) is resident AND still mapped to `texId`.
// Stronger than thumbCacheOwns(), which cannot distinguish a live id from a recycled one — GL
// reuses freed texture names, so after an eviction a stale id can silently alias a DIFFERENT
// image. In a contact sheet whose whole job is telling frames apart, that is a correctness bug,
// not a cosmetic one. Revalidate held ids with this whenever the eviction epoch moved.
bool thumbCacheStillMaps(const std::string& path, int maxEdgePx, unsigned int texId);

// Bumped every time the cache destroys a texture (eviction, limit change, clear). While this is
// unchanged, no previously-returned id can have gone stale, so a caller holding hundreds of ids
// can skip revalidation entirely instead of rescanning the cache every frame.
unsigned long long thumbCacheEvictionEpoch();

// Delete every cached texture (freeAll teardown — GL shutdown, session wipe, memory pressure).
// Any id previously returned by cachedThumbTex() is dangling afterwards.
void thumbCacheClear();

} // namespace reveng
