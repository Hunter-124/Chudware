// Offscreen shaded preview of an immutable DocMesh — independent of the live Renderer.
//
// Renderer::renderThumbnail() only draws the CURRENTLY bound document; background agent docs
// (AI workspace > Activity) each need their own picture of the parsed mesh their DocSession
// already holds (DocSession::cachedDocMesh, prewarmed per agent revision). This draws any
// DocMesh with a fixed iso camera fitted to its bounding box, the viewport's studio lighting
// and gradient backdrop, and feature edges, into a caller-owned RGBA8 texture.
//
// GL thread only. Per call the body buffers are uploaded, drawn once (2x supersampled, box
// resolved by a linear blit) and released, so nothing mesh-sized stays resident; callers bound
// how many renders run per frame. The texture is TOP-LEFT origin (the projection is flipped),
// so it draws with ImGui's default uv0=(0,0) / uv1=(1,1). Every piece of GL state the pass
// touches (framebuffer bindings, viewport, program, VAO, buffers, caps, clear color) is
// restored before return.
#pragma once

#include <string>

#include "render/aicm_loader.h"

namespace render {

struct DocThumbStyle {
    float bgTop[3] = {0.96f, 0.97f, 0.98f};   // vertical backdrop gradient (viewport roles)
    float bgMid[3] = {0.85f, 0.87f, 0.89f};
    float bgBot[3] = {0.93f, 0.94f, 0.95f};
    float edge[3]  = {0.09f, 0.10f, 0.12f};   // feature-edge ink
    float yawDeg   = 35.0f;                   // Renderer's named iso view
    float pitchDeg = 30.0f;
};

// Render `doc` into `tex` (generated when 0, (re)specified to w*h RGBA8 every call). Returns
// false with `err` set when the GL program/framebuffer is unavailable; `tex` is left untouched
// on failure. An empty doc still renders the backdrop.
bool renderDocThumbnail(const DocMesh& doc, int w, int h, const DocThumbStyle& style,
                        unsigned int& tex, std::string& err);

// Delete a texture produced above and zero the handle.
void deleteDocThumbnail(unsigned int& tex);

}  // namespace render
