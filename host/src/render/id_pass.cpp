#include "id_pass.h"

#include <glad/gl.h>

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

// RGBA8 fallback: same vertex shader, pack the 32-bit id into RGBA bytes (little-endian-ish:
// r = low byte ... a = high byte). Readback reassembles. Only the low 24 bits carry topo data
// and 8 bits body, so all 32 bits are meaningful — we use all four channels.
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
    glShaderSource(s, 1, &src, nullptr);
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
    clipCount_ = count < 0 ? 0 : (count > 3 ? 3 : count);
    for (int i = 0; i < clipCount_; ++i) {
        clipNormals_[i][0] = normals[i][0];
        clipNormals_[i][1] = normals[i][1];
        clipNormals_[i][2] = normals[i][2];
        clipOffsets_[i] = offsets[i];
    }
}

void IdPass::destroy() {
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
    // Decide encoding once (env-gated fallback) and build the matching program.
    const char* mode = std::getenv("AI_CAD_IDBUF");
    rgba8_ = (mode && std::strcmp(mode, "rgba8") == 0);

    if (!prog_) prog_ = linkProgram(kVertU, rgba8_ ? kFragRGBA : kFragU);
    int linked = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &linked);
    if (!linked) { err = "id_pass program link failed"; return false; }

    destroy();  // drop old per-body buffers (keep prog_ + targets)
    ++docGen_;                 // new geometry: invalidate the frame cache (task 01)
    haveCachedFrame_ = false;

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

void IdPass::render(const Renderer::CameraXform& cam) {
    if (!built_) return;

    // Frame cache (task 01: selection lag). Fingerprint everything the draws depend on; if it
    // matches the last rendered frame, the offscreen id buffer is still valid and we skip the
    // O(bodies) redraw entirely. This is what keeps hover/pick instant in large assemblies — a
    // static camera no longer re-renders every body's faces/edges/verts each frame.
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
    if (haveCachedFrame_ && key == lastKey_) return;  // cached id buffer still valid — skip redraw

    allocTargets(cam.ssWidth, cam.ssHeight);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
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
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, cam.mvp.m);
    glUniform1i(glGetUniformLocation(prog_, "uClipCount"), clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(glGetUniformLocation(prog_, "uClipN"), clipCount_, clipNormals_[0]);
        glUniform1fv(glGetUniformLocation(prog_, "uClipOff"), clipCount_, clipOffsets_);
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
    glEnable(GL_PROGRAM_POINT_SIZE);
#ifdef GL_POINT_SPRITE
    glEnable(GL_POINT_SPRITE);
#endif
    glPointSize(6.0f);
    for (const auto& v : vertDraws_) {
        if (bodyHiddenId(v.body_id)) continue;  // hidden body: unpickable
        glBindVertexArray(v.vao);
        glDrawArrays(GL_POINTS, 0, v.count);
    }

    glDepthRange(0.0, 1.0);
    glBindVertexArray(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // The id buffer now reflects `key`; reuse it until one of these inputs changes (task 01).
    lastKey_ = key;
    haveCachedFrame_ = true;
}

PickHit IdPass::pickFinal(int px, int py, int finalW, int finalH) {
    PickHit empty;
    if (!built_ || finalW <= 0 || finalH <= 0) return empty;
    if (px < 0 || py < 0 || px >= finalW || py >= finalH) return empty;

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
    return decodeId(id);
}

PickHit IdPass::pickFinalPreferEdge(int px, int py, int finalW, int finalH, int radiusPx) {
    PickHit center = pickFinal(px, py, finalW, finalH);
    // A vertex/edge already under the cursor wins outright (it's exactly what was asked for).
    if (center.kind == PickKind::Edge || center.kind == PickKind::Vertex) return center;
    if (!built_ || finalW <= 0 || finalH <= 0 || radiusPx <= 0) return center;
    if (px < 0 || py < 0 || px >= finalW || py >= finalH) return center;

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
    if (bw <= 0 || bh <= 0) return center;

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    std::vector<uint32_t> block((size_t)bw * bh, 0u);
    if (rgba8_) {
        std::vector<unsigned char> rgba((size_t)bw * bh * 4, 0);
        glReadPixels(x0, y0, bw, bh, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
        for (size_t i = 0; i < block.size(); ++i)
            block[i] = (uint32_t)rgba[i * 4] | ((uint32_t)rgba[i * 4 + 1] << 8) |
                       ((uint32_t)rgba[i * 4 + 2] << 16) | ((uint32_t)rgba[i * 4 + 3] << 24);
    } else {
        glReadPixels(x0, y0, bw, bh, GL_RED_INTEGER, GL_UNSIGNED_INT, block.data());
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // Nearest EDGE/VERTEX pixel to the cursor, within the (circular) radius, skipping hidden bodies.
    long bestD2 = (long)rss * rss + 1;
    PickHit best;
    for (int j = 0; j < bh; ++j) {
        for (int i = 0; i < bw; ++i) {
            uint32_t v = block[(size_t)j * bw + i];
            if (v == 0 || bodyHidden(v)) continue;
            PickKind k = PickKind((v >> 22) & 0x3u);
            if (k != PickKind::Edge && k != PickKind::Vertex) continue;
            long dx = (long)(x0 + i) - cx, dy = (long)(y0 + j) - cy;
            long d2 = dx * dx + dy * dy;
            if (d2 < bestD2) { bestD2 = d2; best = decodeId(v); }
        }
    }
    return best.valid() ? best : center;
}

}  // namespace render
