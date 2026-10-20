#include "id_pass.h"

#include "gl_compat.h"
#include "ui/viewport/glsl_compat.h"


#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace render {

namespace {

// R32UI path: integer color out, id carried as a per-vertex INTEGER attribute (location 1) and
// passed flat to the fragment shader — so an entire body batches into one draw call instead of
// one draw + uId-uniform update per face/edge/vertex.
const char* kVertU = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in uint aId;
uniform mat4 uMVP;
out vec3 vWorld;
flat out uint vId;
void main(){ vWorld = aPos; vId = aId; gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kFragU = R"(#version 330 core
flat in uint vId;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
in vec3 vWorld;
out uint frag;
void main(){
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    frag = vId;
})";

// RGBA8 fallback: same vertex shader, pack the full 32-bit id into RGBA bytes (little-endian-ish:
// r = low byte ... a = high byte). Readback reassembles all body/kind/topology bits.
const char* kFragRGBA = R"(#version 330 core
flat in uint vId;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
in vec3 vWorld;
out vec4 frag;
void main(){
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    uint id = vId;
    float r = float(id & 0xFFu) / 255.0;
    float g = float((id >> 8u) & 0xFFu) / 255.0;
    float b = float((id >> 16u) & 0xFFu) / 255.0;
    float a = float((id >> 24u) & 0xFFu) / 255.0;
    frag = vec4(r, g, b, a);
})";

unsigned int compile(unsigned int type, const char* src) {
    unsigned int s = glCreateShader(type);
#ifdef CHUDWARE_GLES3
    const std::string rewritten = shaderSrc(src);
    const char* p = rewritten.c_str();
#else
    const char* p = shaderSrc(src);
#endif
    glShaderSource(s, 1, &p, nullptr);
    glCompileShader(s);
    int ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024];
        glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        std::fprintf(stderr, "id_pass shader compile error: %s\n", log);
    }
    return s;
}
unsigned int linkProgram(const char* vs, const char* fs) {
    unsigned int v = compile(GL_VERTEX_SHADER, vs);
    unsigned int f = compile(GL_FRAGMENT_SHADER, fs);
    unsigned int p = glCreateProgram();
    glAttachShader(p, v);
    glAttachShader(p, f);
    glLinkProgram(p);
    glDeleteShader(v);
    glDeleteShader(f);
    return p;
}

}  // namespace

IdPass::~IdPass() {
    destroy();
    if (fbo_) glDeleteFramebuffers(1, &fbo_);
    if (idTex_) glDeleteTextures(1, &idTex_);
    if (depthRbo_) glDeleteRenderbuffers(1, &depthRbo_);
    if (prog_) glDeleteProgram(prog_);
}

void IdPass::setSectionClip(int count, const float normals[3][3], const float offsets[3]) {
    const int nextCount = count < 0 ? 0 : (count > 3 ? 3 : count);
    bool changed = nextCount != clipCount_;
    for (int i = 0; !changed && i < nextCount; ++i) {
        changed = clipNormals_[i][0] != normals[i][0] ||
                  clipNormals_[i][1] != normals[i][1] ||
                  clipNormals_[i][2] != normals[i][2] ||
                  clipOffsets_[i] != offsets[i];
    }
    if (!changed) return;

    clipCount_ = nextCount;
    for (int i = 0; i < clipCount_; ++i) {
        clipNormals_[i][0] = normals[i][0];
        clipNormals_[i][1] = normals[i][1];
        clipNormals_[i][2] = normals[i][2];
        clipOffsets_[i] = offsets[i];
    }
    haveCachedFrame_ = false;
    pickMemo_.valid = false;
}

void IdPass::destroy() {
    built_ = false;
    haveCachedFrame_ = false;
    pickMemo_.valid = false;
    auto freeDraws = [](std::vector<BodyDraw>& v) {
        for (auto& d : v) {
            if (d.vao) glDeleteVertexArrays(1, &d.vao);
            if (d.vboPos) glDeleteBuffers(1, &d.vboPos);
            if (d.vboId) glDeleteBuffers(1, &d.vboId);
            if (d.ebo) glDeleteBuffers(1, &d.ebo);
        }
        v.clear();
    };
    freeDraws(faceDraws_);
    freeDraws(edgeDraws_);
    freeDraws(vertDraws_);
}

void IdPass::allocTargets(int w, int h) {
    if (w == w_ && h == h_ && fbo_) return;
    w_ = w; h_ = h;
    if (!fbo_) glGenFramebuffers(1, &fbo_);
    if (!idTex_) glGenTextures(1, &idTex_);
    if (!depthRbo_) glGenRenderbuffers(1, &depthRbo_);

    glBindTexture(GL_TEXTURE_2D, idTex_);
    if (rgba8_)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w_, h_, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    else
        glTexImage2D(GL_TEXTURE_2D, 0, GL_R32UI, w_, h_, 0, GL_RED_INTEGER, GL_UNSIGNED_INT,
                     nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glBindRenderbuffer(GL_RENDERBUFFER, depthRbo_);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, w_, h_);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, idTex_, 0);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, depthRbo_);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

bool IdPass::setDoc(const DocMesh& doc, std::string& err) {
    destroy();  // drop old per-body buffers (keep prog_ + targets)
    built_ = false;
    ++docGen_;
    haveCachedFrame_ = false;
    // The RGBA8 WebGL2 path has the same 32-bit budget as R32UI. Reject oversized IDs
    // before encoding so picking never silently aliases a different body or topology item.
    if (const char* reason = pickIdOverflow(doc)) {
        err = reason;
        static bool logged = false;
        if (!logged) {
            std::fprintf(stderr, "id_pass: %s\n", reason);
            logged = true;
        }
        return false;
    }
    // Decide encoding once (env-gated fallback) and build the matching program.
    const char* mode = std::getenv("CHUDWARE_IDBUF");
#ifdef __EMSCRIPTEN__
    // WebGL2 has no reliable R32UI integer color attachment — force the RGBA8 path.
    rgba8_ = true;
    (void)mode;
#else
    rgba8_ = (mode && std::strcmp(mode, "rgba8") == 0);
#endif

    if (!prog_) {
        prog_ = linkProgram(kVertU, rgba8_ ? kFragRGBA : kFragU);
        mvpLoc_ = glGetUniformLocation(prog_, "uMVP");
        clipCountLoc_ = glGetUniformLocation(prog_, "uClipCount");
        clipNormalsLoc_ = glGetUniformLocation(prog_, "uClipN");
        clipOffsetsLoc_ = glGetUniformLocation(prog_, "uClipOff");
    }
    int linked = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &linked);
    if (!linked) { err = "id_pass program link failed"; return false; }

    // Bind pos (location 0, float3) + id (location 1, uint) attribute buffers into a fresh VAO and
    // optionally an element buffer. Returns the BodyDraw (count set by caller). Integer ids MUST go
    // through glVertexAttribIPointer so they reach the shader as a real uint (not float-converted).
    auto buildVao = [](const std::vector<float>& pos, const std::vector<uint32_t>& ids,
                       const uint32_t* idx, size_t idxCount, uint32_t body_id) -> BodyDraw {
        BodyDraw d;
        d.body_id = body_id;
        glGenVertexArrays(1, &d.vao);
        glGenBuffers(1, &d.vboPos);
        glGenBuffers(1, &d.vboId);
        glBindVertexArray(d.vao);
        glBindBuffer(GL_ARRAY_BUFFER, d.vboPos);
        glBufferData(GL_ARRAY_BUFFER, pos.size() * sizeof(float), pos.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindBuffer(GL_ARRAY_BUFFER, d.vboId);
        glBufferData(GL_ARRAY_BUFFER, ids.size() * sizeof(uint32_t), ids.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(1);
        glVertexAttribIPointer(1, 1, GL_UNSIGNED_INT, sizeof(uint32_t), (void*)0);
        if (idx && idxCount) {
            glGenBuffers(1, &d.ebo);
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, d.ebo);
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, idxCount * sizeof(uint32_t), idx, GL_STATIC_DRAW);
        }
        glBindVertexArray(0);
        return d;
    };

    for (const auto& b : doc.bodies) {
        // ---- faces: ONE VAO per body. Shared position VBO + a per-vertex id attribute (every
        // vertex tagged with its owning face's encoded id — face node blocks are disjoint per
        // face in the tessellation) + the body's full index buffer, drawn in a single call. ----
        if (!b.pos.empty() && !b.idx.empty()) {
            std::vector<uint32_t> faceIds(b.pos.size() / 3, 0u);
            for (const auto& fr : b.faces) {
                if (fr.tri_count == 0) continue;
                uint32_t fid = encodeId(b.body_id, PickKind::Face, fr.face_id);
                const uint32_t* base = b.idx.data() + (size_t)fr.first_tri * 3;
                size_t n = (size_t)fr.tri_count * 3;
                for (size_t k = 0; k < n; ++k) faceIds[base[k]] = fid;
            }
            BodyDraw fd = buildVao(b.pos, faceIds, b.idx.data(), b.idx.size(), b.body_id);
            fd.count = (int)b.idx.size();
            faceDraws_.push_back(fd);
        }

        // ---- edges: ONE VAO per body. All edge polylines flattened to a single line list with a
        // per-vertex id (each segment endpoint tagged with its edge's encoded id). ----
        {
            std::vector<float> lines;
            std::vector<uint32_t> ids;
            for (const auto& es : b.edges) {
                if (es.pt_count < 2) continue;
                uint32_t eid = encodeId(b.body_id, PickKind::Edge, es.edge_id);
                for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
                    const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
                    const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
                    lines.insert(lines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
                    ids.push_back(eid);
                    ids.push_back(eid);
                }
            }
            if (!lines.empty()) {
                BodyDraw ed = buildVao(lines, ids, nullptr, 0, b.body_id);
                ed.count = (int)lines.size() / 3;
                edgeDraws_.push_back(ed);
            }
        }

        // ---- B-rep verts: ONE VAO per body. All endpoint points with a per-vertex id. ----
        {
            std::vector<float> pts;
            std::vector<uint32_t> ids;
            for (const auto& v : b.brep_verts) {
                pts.insert(pts.end(), {v.xyz[0], v.xyz[1], v.xyz[2]});
                ids.push_back(encodeId(b.body_id, PickKind::Vertex, v.vertex_id));
            }
            if (!pts.empty()) {
                BodyDraw vd = buildVao(pts, ids, nullptr, 0, b.body_id);
                vd.count = (int)pts.size() / 3;
                vertDraws_.push_back(vd);
            }
        }
    }

    built_ = true;
    return true;
}

bool IdPass::render(const Renderer::CameraXform& cam, bool facesOnly) {
    if (!built_ || cam.ssWidth <= 0 || cam.ssHeight <= 0) {
        haveCachedFrame_ = false;
        pickMemo_.valid = false;
        return false;
    }

    // Fingerprint every input consulted by the draw. The viewport invokes this only when it is
    // about to read a pick (or service the scripted pick driver), so non-viewport interaction does
    // not even enter the pass. Repeated hover with unchanged inputs reaches this cheap comparison;
    // camera, framebuffer size, topology upload, visibility, or active clip changes force a redraw.
    IdPassFrameKey key;
    std::memcpy(key.mvp, cam.mvp.m, sizeof(key.mvp));
    key.w = cam.ssWidth;
    key.h = cam.ssHeight;
    key.clipCount = clipCount_;
    for (int i = 0; i < clipCount_; ++i) {
        key.clipN[i * 3 + 0] = clipNormals_[i][0];
        key.clipN[i * 3 + 1] = clipNormals_[i][1];
        key.clipN[i * 3 + 2] = clipNormals_[i][2];
        key.clipOff[i] = clipOffsets_[i];
    }
    key.hiddenHash = idPassHiddenHash(hidden_);
    key.docGen = docGen_;
    key.facesOnly = facesOnly;
    if (haveCachedFrame_ && key == lastKey_) return true;
    haveCachedFrame_ = false;
    pickMemo_.valid = false;

    allocTargets(cam.ssWidth, cam.ssHeight);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        return false;
    }
    glViewport(0, 0, w_, h_);

    // Clear color (id = 0 == background) + depth. R32UI needs the integer clear path.
    if (rgba8_) {
        glClearColor(0, 0, 0, 0);
        glClear(GL_COLOR_BUFFER_BIT);
    } else {
        GLuint zero[4] = {0, 0, 0, 0};
        glClearBufferuiv(GL_COLOR, 0, zero);
    }
    glClear(GL_DEPTH_BUFFER_BIT);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glDisable(GL_BLEND);

    glUseProgram(prog_);
    glUniformMatrix4fv(mvpLoc_, 1, GL_FALSE, cam.mvp.m);
    glUniform1i(clipCountLoc_, clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(clipNormalsLoc_, clipCount_, clipNormals_[0]);
        glUniform1fv(clipOffsetsLoc_, clipCount_, clipOffsets_);
    }

    // Faces first, pushed back so edges/verts win the depth test on coincident geometry. Each body
    // is one draw call — the per-vertex id attribute carries the face id, hidden bodies are skipped.
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(1.0f, 1.0f);
    for (const auto& f : faceDraws_) {
        if (bodyHiddenId(f.body_id)) continue;  // hidden body: unpickable
        glBindVertexArray(f.vao);
        glDrawElements(GL_TRIANGLES, f.count, GL_UNSIGNED_INT, 0);
    }
    glDisable(GL_POLYGON_OFFSET_FILL);

    if (!facesOnly) {
    // Edges over faces: a fat line so they're pickable, and a small negative depth-range bias so
    // an edge on a face surface reads as the edge. One draw call per body.
    glDepthRange(0.0, 0.999);
    glLineWidth(3.0f);
    for (const auto& e : edgeDraws_) {
        if (bodyHiddenId(e.body_id)) continue;  // hidden body: unpickable
        glBindVertexArray(e.vao);
        glDrawArrays(GL_LINES, 0, e.count);
    }

    // Verts win over everything: largest depth bias, fat points. One draw call per body — each
    // point carries its own id via the attribute, so no per-point uniform churn.
    glDepthRange(0.0, 0.998);
#ifndef CHUDWARE_GLES3
    glEnable(GL_PROGRAM_POINT_SIZE);
#ifdef GL_POINT_SPRITE
    glEnable(GL_POINT_SPRITE);
#endif
    glPointSize(6.0f);
#endif
    for (const auto& v : vertDraws_) {
        if (bodyHiddenId(v.body_id)) continue;  // hidden body: unpickable
        glBindVertexArray(v.vao);
        glDrawArrays(GL_POINTS, 0, v.count);
    }
    }

    glDepthRange(0.0, 1.0);
    glBindVertexArray(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // The id buffer now reflects `key`; reuse it until one of these inputs changes.
    ++renderGen_;
    lastKey_ = key;
    haveCachedFrame_ = true;
    return true;
}

PickHit IdPass::pickFinal(int px, int py, int finalW, int finalH) {
    PickHit empty;
    if (!built_ || !haveCachedFrame_ || finalW <= 0 || finalH <= 0) return empty;
    if (px < 0 || py < 0 || px >= finalW || py >= finalH) return empty;
    if (pickMemo_.matches(renderGen_, px, py, finalW, finalH, 0)) return pickMemo_.hit;

    // Map final (top-left) pixel -> supersampled (bottom-left) pixel. Sample the SS-block center.
    float sx = (px + 0.5f) * (float)w_ / (float)finalW;
    float sy = (py + 0.5f) * (float)h_ / (float)finalH;
    int gx = (int)sx;
    int gy = h_ - 1 - (int)sy;  // flip Y to GL origin
    if (gx < 0) gx = 0; if (gx >= w_) gx = w_ - 1;
    if (gy < 0) gy = 0; if (gy >= h_) gy = h_ - 1;

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    uint32_t id = 0;
    if (rgba8_) {
        unsigned char rgba[4] = {0, 0, 0, 0};
        glReadPixels(gx, gy, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
        id = (uint32_t)rgba[0] | ((uint32_t)rgba[1] << 8) | ((uint32_t)rgba[2] << 16) |
             ((uint32_t)rgba[3] << 24);
    } else {
        glReadPixels(gx, gy, 1, 1, GL_RED_INTEGER, GL_UNSIGNED_INT, &id);
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    PickHit hit = decodeId(id);
    pickMemo_ = {renderGen_, px, py, finalW, finalH, 0, hit, true};
    return hit;
}

PickHit IdPass::pickFinalPreferEdge(int px, int py, int finalW, int finalH, int radiusPx) {
    if (radiusPx <= 0) return pickFinal(px, py, finalW, finalH);
    PickHit empty;
    if (!built_ || !haveCachedFrame_ || finalW <= 0 || finalH <= 0) return empty;
    if (px < 0 || py < 0 || px >= finalW || py >= finalH) return empty;
    if (pickMemo_.matches(renderGen_, px, py, finalW, finalH, radiusPx)) return pickMemo_.hit;
    auto remember = [&](PickHit hit) {
        pickMemo_ = {renderGen_, px, py, finalW, finalH, radiusPx, hit, true};
        return hit;
    };

    // Map the cursor + search radius into supersampled (GL bottom-left) space and read one block.
    float sx = (px + 0.5f) * (float)w_ / (float)finalW;
    float sy = (py + 0.5f) * (float)h_ / (float)finalH;
    int cx = (int)sx;
    int cy = h_ - 1 - (int)sy;  // GL origin
    int rss = (int)std::ceil((float)radiusPx * (float)w_ / (float)finalW);
    if (rss < 1) rss = 1;
    int x0 = cx - rss, y0 = cy - rss;
    int x1 = cx + rss, y1 = cy + rss;
    if (x0 < 0) x0 = 0; if (y0 < 0) y0 = 0;
    if (x1 >= w_) x1 = w_ - 1; if (y1 >= h_) y1 = h_ - 1;
    int bw = x1 - x0 + 1, bh = y1 - y0 + 1;
    if (bw <= 0 || bh <= 0) return pickFinal(px, py, finalW, finalH);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    const size_t pixelCount = (size_t)bw * bh;
    if (readbackIds_.size() < pixelCount) readbackIds_.resize(pixelCount);
    if (rgba8_) {
        if (readbackRgba_.size() < pixelCount * 4) readbackRgba_.resize(pixelCount * 4);
        glReadPixels(x0, y0, bw, bh, GL_RGBA, GL_UNSIGNED_BYTE, readbackRgba_.data());
        for (size_t i = 0; i < pixelCount; ++i)
            readbackIds_[i] = (uint32_t)readbackRgba_[i * 4] |
                              ((uint32_t)readbackRgba_[i * 4 + 1] << 8) |
                              ((uint32_t)readbackRgba_[i * 4 + 2] << 16) |
                              ((uint32_t)readbackRgba_[i * 4 + 3] << 24);
    } else {
        glReadPixels(x0, y0, bw, bh, GL_RED_INTEGER, GL_UNSIGNED_INT, readbackIds_.data());
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // Use the very same pixel as pickFinal, including its boundary clamps. Do not let a nearby
    // vertex replace an exact center edge: that early precedence is part of the picking contract.
    int gx = cx, gy = cy;
    if (gx < 0) gx = 0; if (gx >= w_) gx = w_ - 1;
    if (gy < 0) gy = 0; if (gy >= h_) gy = h_ - 1;
    PickHit center = decodeId(readbackIds_[(size_t)(gy - y0) * bw + (gx - x0)]);
    if (center.kind == PickKind::Edge || center.kind == PickKind::Vertex)
        return remember(center);

    // Two-tier nearest search within the (circular) radius, skipping hidden bodies. Vertices are a
    // smaller target than edges, which are a smaller target than faces, so precedence is strictly
    // by KIND first, distance second: ANY vertex in range beats EVERY edge, and any edge/vertex beats
    // the center face. Within a tier we take the pixel nearest the cursor. (A center pixel that was
    // already Edge/Vertex returned above, unchanged — Convert Entities relies on that.)
    long bestVD2 = (long)rss * rss + 1;
    long bestED2 = (long)rss * rss + 1;
    PickHit bestV, bestE;
    for (int j = 0; j < bh; ++j) {
        for (int i = 0; i < bw; ++i) {
            uint32_t v = readbackIds_[(size_t)j * bw + i];
            if (v == 0 || bodyHidden(v)) continue;
            PickKind k = PickKind((v >> 18) & 0x3u);
            if (k != PickKind::Edge && k != PickKind::Vertex) continue;
            long dx = (long)(x0 + i) - cx, dy = (long)(y0 + j) - cy;
            long d2 = dx * dx + dy * dy;
            if (k == PickKind::Vertex) {
                if (d2 < bestVD2) { bestVD2 = d2; bestV = decodeId(v); }
            } else {  // Edge
                if (d2 < bestED2) { bestED2 = d2; bestE = decodeId(v); }
            }
        }
    }
    if (bestV.valid()) return remember(bestV);  // vertex tier wins outright
    if (bestE.valid()) return remember(bestE);  // else nearest edge
    return remember(center);                  // else the exact-pixel face (or background)
}

}  // namespace render
