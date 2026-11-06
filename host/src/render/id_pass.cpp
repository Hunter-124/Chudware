#include "id_pass.h"

#include "gl_compat.h"
#include "ui/viewport/glsl_compat.h"


#include <algorithm>
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
uniform float uPointSize;
out vec3 vWorld;
flat out uint vId;
void main(){ vWorld = aPos; vId = aId; gl_Position = uMVP * vec4(aPos, 1.0);
             gl_PointSize = uPointSize; })";

std::string fragmentSource(bool rgba8) {
    std::string src = R"(#version 330 core
flat in uint vId;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
uniform int uGuardThin;
uniform uvec3 uPassThrough;
uniform sampler2D uGuardDepth;
in vec3 vWorld;
layout(location=1) out vec4 depthColor;
vec4 packUint(uint bits) {
    return vec4(float(bits & 255u), float((bits >> 8u) & 255u),
                float((bits >> 16u) & 255u), float((bits >> 24u) & 255u)) / 255.0;
}
uint unpackUint(vec4 value) {
    uvec4 b = uvec4(round(value * 255.0));
    return b.r | (b.g << 8u) | (b.b << 16u) | (b.a << 24u);
}
)";
    src += rgba8 ? R"(
uniform sampler2D uGuardId;
layout(location=0) out vec4 frag;
uint guardId(ivec2 p) { return unpackUint(texelFetch(uGuardId, p, 0)); }
)" : R"(
#ifdef GL_ES
precision highp usampler2D;
#endif
uniform usampler2D uGuardId;
layout(location=0) out uint frag;
uint guardId(ivec2 p) { return texelFetch(uGuardId, p, 0).r; }
)";
    src += R"(
float guardDepth(ivec2 p) {
    return uintBitsToFloat(unpackUint(texelFetch(uGuardDepth, p, 0)));
}
bool passThrough(uint body) {
    return body != 0u &&
        (body == uPassThrough.x || body == uPassThrough.y || body == uPassThrough.z);
}
bool visibleThin() {
    gl_FragDepth = gl_FragCoord.z;
    if (uGuardThin == 0) return true;
    ivec2 p = ivec2(gl_FragCoord.xy);
    uint face = guardId(p);
    uint body = face >> 20u;
    if (face == 0u || (uGuardThin == 1 && passThrough(body)) ||
        (uGuardThin == 2 && !passThrough(body))) return true;
    float front = guardDepth(p);
    float tolerance = 4.0 / 16777215.0;
    bool sameBody = body == (vId >> 20u);
    if (sameBody) {
        // A one-pixel line can land up to a pixel from its mathematical center. Permit only
        // the face's local depth slope for this body's own coincident edges and vertices.
        ivec2 size = textureSize(uGuardDepth, 0);
        float slope = 0.0;
        for (int axis = 0; axis < 2; ++axis) {
            for (int direction = -1; direction <= 1; direction += 2) {
                ivec2 q = p + (axis == 0 ? ivec2(direction, 0) : ivec2(0, direction));
                if (q.x >= 0 && q.y >= 0 && q.x < size.x && q.y < size.y &&
                    guardId(q) == face)
                    slope = max(slope, abs(guardDepth(q) - front));
            }
        }
        tolerance += slope;
    }
    if (gl_FragCoord.z > front + tolerance) return false;
    if (sameBody) gl_FragDepth = min(gl_FragCoord.z, front);
    return true;
}
void main() {
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    if (!visibleThin()) discard;
)";
    src += rgba8 ? "    frag = packUint(vId);\n" : "    frag = vId;\n";
    src += "    depthColor = packUint(floatBitsToUint(gl_FragCoord.z));\n}\n";
    return src;
}

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

IdPass::IdPass() {
    const char* mode = std::getenv("CHUDWARE_IDBUF");
#ifdef __EMSCRIPTEN__
    rgba8_ = true;
    (void)mode;
#else
    rgba8_ = mode && std::strcmp(mode, "rgba8") == 0;
#endif
    const std::string fs = fragmentSource(rgba8_);
    prog_ = linkProgram(kVertU, fs.c_str());
    mvpLoc_ = glGetUniformLocation(prog_, "uMVP");
    pointSizeLoc_ = glGetUniformLocation(prog_, "uPointSize");
    clipCountLoc_ = glGetUniformLocation(prog_, "uClipCount");
    clipNormalsLoc_ = glGetUniformLocation(prog_, "uClipN");
    clipOffsetsLoc_ = glGetUniformLocation(prog_, "uClipOff");
    guardThinLoc_ = glGetUniformLocation(prog_, "uGuardThin");
    guardIdLoc_ = glGetUniformLocation(prog_, "uGuardId");
    guardDepthLoc_ = glGetUniformLocation(prog_, "uGuardDepth");
    passThroughLoc_ = glGetUniformLocation(prog_, "uPassThrough");
}

IdPass::~IdPass() {
    destroy();
    for (auto& target : targets_) {
        if (target.fbo) glDeleteFramebuffers(1, &target.fbo);
        if (target.texture) glDeleteTextures(1, &target.texture);
        if (target.depthColor) glDeleteTextures(1, &target.depthColor);
        if (target.depth) glDeleteRenderbuffers(1, &target.depth);
    }
    for (auto& read : hoverReads_)
        if (read.pbo) glDeleteBuffers(1, &read.pbo);
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
    invalidateFrames();
}

void IdPass::clearHoverReads() {
    for (auto& read : hoverReads_) {
        if (read.fence) glDeleteSync(static_cast<GLsync>(read.fence));
        read.fence = nullptr;
    }
    hoverMemo_.valid = false;
}

void IdPass::invalidateFrames() {
    for (auto& target : targets_) target.valid = false;
    pickMemo_.valid = false;
    clearHoverReads();
}

void IdPass::destroy() {
    built_ = false;
    invalidateFrames();
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

void IdPass::allocTarget(Target& target, int w, int h) {
    if (w == target.w && h == target.h && target.fbo) return;
    target.w = w; target.h = h;
    if (!target.fbo) glGenFramebuffers(1, &target.fbo);
    if (!target.texture) glGenTextures(1, &target.texture);
    if (!target.depthColor) glGenTextures(1, &target.depthColor);
    if (!target.depth) glGenRenderbuffers(1, &target.depth);

    glBindTexture(GL_TEXTURE_2D, target.texture);
    if (rgba8_)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    else
        glTexImage2D(GL_TEXTURE_2D, 0, GL_R32UI, w, h, 0, GL_RED_INTEGER, GL_UNSIGNED_INT,
                     nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glBindTexture(GL_TEXTURE_2D, target.depthColor);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    glBindRenderbuffer(GL_RENDERBUFFER, target.depth);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, w, h);
    glBindFramebuffer(GL_FRAMEBUFFER, target.fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, target.texture, 0);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT1, GL_TEXTURE_2D, target.depthColor, 0);
    const GLenum buffers[] = {GL_COLOR_ATTACHMENT0, GL_COLOR_ATTACHMENT1};
    glDrawBuffers(2, buffers);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, target.depth);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

bool IdPass::setDoc(const DocMesh& doc, std::string& err) {
    destroy();  // drop old per-body buffers (keep prog_ + targets)
    built_ = false;
    ++docGen_;
    invalidateFrames();
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
            std::vector<uint32_t> fallbackFaceIds;
            if (!doc.derivedReady) {
                fallbackFaceIds.assign(b.pos.size() / 3, 0u);
                for (const auto& fr : b.faces) {
                    if (fr.tri_count == 0) continue;
                    const uint32_t fid = encodeId(b.body_id, PickKind::Face, fr.face_id);
                    const uint32_t* base = b.idx.data() + (size_t)fr.first_tri * 3;
                    for (size_t k = 0; k < (size_t)fr.tri_count * 3; ++k)
                        fallbackFaceIds[base[k]] = fid;
                }
            }
            const auto& faceIds = doc.derivedReady ? b.faceIds : fallbackFaceIds;
            BodyDraw fd = buildVao(b.pos, faceIds, b.idx.data(), b.idx.size(), b.body_id);
            fd.count = (int)b.idx.size();
            faceDraws_.push_back(fd);
        }

        // ---- edges: ONE VAO per body. All edge polylines flattened to a single line list with a
        // per-vertex id (each segment endpoint tagged with its edge's encoded id). ----
        {
            std::vector<float> lines;
            std::vector<uint32_t> ids;
            if (!doc.derivedReady) {
                for (const auto& es : b.edges) {
                    if (es.pt_count < 2) continue;
                    const uint32_t eid = encodeId(b.body_id, PickKind::Edge, es.edge_id);
                    for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
                        const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
                        const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
                        lines.insert(lines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
                        ids.push_back(eid);
                        ids.push_back(eid);
                    }
                }
            }
            const auto& edgeLines = doc.derivedReady ? b.edgeLines : lines;
            const auto& edgeIds = doc.derivedReady ? b.edgeIds : ids;
            if (!edgeLines.empty()) {
                BodyDraw ed = buildVao(edgeLines, edgeIds, nullptr, 0, b.body_id);
                ed.count = (int)edgeLines.size() / 3;
                edgeDraws_.push_back(ed);
            }
        }

        // ---- B-rep verts: ONE VAO per body. All endpoint points with a per-vertex id. ----
        {
            std::vector<float> pts;
            std::vector<uint32_t> ids;
            if (!doc.derivedReady) {
                for (const auto& v : b.brep_verts) {
                    pts.insert(pts.end(), {v.xyz[0], v.xyz[1], v.xyz[2]});
                    ids.push_back(encodeId(b.body_id, PickKind::Vertex, v.vertex_id));
                }
            }
            const auto& points = doc.derivedReady ? b.vertexPoints : pts;
            const auto& pointIds = doc.derivedReady ? b.vertexIds : ids;
            if (!points.empty()) {
                BodyDraw vd = buildVao(points, pointIds, nullptr, 0, b.body_id);
                vd.count = (int)points.size() / 3;
                vertDraws_.push_back(vd);
            }
        }
    }

    built_ = true;
    return true;
}

bool IdPass::render(const Renderer::CameraXform& cam, bool facesOnly) {
    activeTarget_ = facesOnly ? 1 : 0;
    auto& target = targets_[activeTarget_];
    const int finalW = cam.vpWidth > 0 ? cam.vpWidth : cam.ssWidth;
    const int finalH = cam.vpHeight > 0 ? cam.vpHeight : cam.ssHeight;
    if (!built_ || finalW <= 0 || finalH <= 0) {
        target.valid = false;
        pickMemo_.valid = false;
        return false;
    }

    // Fingerprint every input consulted by the draw. The viewport invokes this only when it is
    // about to read a pick (or service the scripted pick driver), so non-viewport interaction does
    // not even enter the pass. Repeated hover with unchanged inputs reaches this cheap comparison;
    // camera, framebuffer size, topology upload, visibility, or active clip changes force a redraw.
    IdPassFrameKey key;
    std::memcpy(key.mvp, cam.mvp.m, sizeof(key.mvp));
    key.w = finalW;
    key.h = finalH;
    key.clipCount = clipCount_;
    for (int i = 0; i < clipCount_; ++i) {
        key.clipN[i * 3 + 0] = clipNormals_[i][0];
        key.clipN[i * 3 + 1] = clipNormals_[i][1];
        key.clipN[i * 3 + 2] = clipNormals_[i][2];
        key.clipOff[i] = clipOffsets_[i];
    }
    key.hiddenHash = idPassHiddenHash(hidden_);
    key.docGen = docGen_;
    key.passThrough = passThrough_;
    key.facesOnly = facesOnly;
    if (target.valid && key == target.key) return true;
    target.valid = false;
    pickMemo_.valid = false;
    if (!facesOnly) clearHoverReads();

    // The unbiased face-only image guards thin primitives against the real frontmost surface.
    // Its cache is independent, so face-only clients and full picks can share it.
    if (!facesOnly) {
        const bool faceReady = render(cam, true);
        activeTarget_ = 0;
        if (!faceReady) return false;
    }

    allocTarget(target, finalW, finalH);

    glBindFramebuffer(GL_FRAMEBUFFER, target.fbo);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        return false;
    }
    glViewport(0, 0, finalW, finalH);

    // The ID pass cannot inherit depth/scissor/color state from a translucent overlay.
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_STENCIL_TEST);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glDepthMask(GL_TRUE);
    glDepthRange(0.0, 1.0);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glDisable(GL_BLEND);
    const bool dither = glIsEnabled(GL_DITHER) == GL_TRUE;
    glDisable(GL_DITHER);
    const GLfloat clearColor[4] = {0, 0, 0, 0};
    const GLuint clearId[4] = {0, 0, 0, 0};
    const GLfloat clearDepth = 1.0f;
    if (rgba8_) glClearBufferfv(GL_COLOR, 0, clearColor);
    else glClearBufferuiv(GL_COLOR, 0, clearId);
    glClearBufferfv(GL_COLOR, 1, clearColor);
    glClearBufferfv(GL_DEPTH, 0, &clearDepth);

    glUseProgram(prog_);
    glUniformMatrix4fv(mvpLoc_, 1, GL_FALSE, cam.mvp.m);
    glUniform1f(pointSizeLoc_, 1.0f);
    glUniform1i(guardThinLoc_, 0);
    glUniform1i(guardIdLoc_, 0);
    glUniform1i(guardDepthLoc_, 1);
    glUniform3ui(passThroughLoc_, passThrough_[0], passThrough_[1], passThrough_[2]);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, facesOnly ? 0 : targets_[1].texture);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, facesOnly ? 0 : targets_[1].depthColor);
    glUniform1i(clipCountLoc_, clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(clipNormalsLoc_, clipCount_, clipNormals_[0]);
        glUniform1fv(clipOffsetsLoc_, clipCount_, clipOffsets_);
    }

    // Rasterize thin targets at their actual position; the CPU search supplies the full grab
    // radius. A wide primitive can otherwise stick out past an occluder's silhouette.
    const auto isPassThrough = [&](uint32_t id) {
        return id != 0 && std::find(passThrough_.begin(), passThrough_.end(), id) != passThrough_.end();
    };
    auto drawLayer = [&](bool transparent) {
        // Faces are unbiased. Thin primitives are guarded against this same face image below.
        glUniform1i(guardThinLoc_, 0);
        glDisable(GL_POLYGON_OFFSET_FILL);
        for (const auto& f : faceDraws_) {
            if (bodyHiddenId(f.body_id) || isPassThrough(f.body_id) != transparent) continue;
            glBindVertexArray(f.vao);
            glDrawElements(GL_TRIANGLES, f.count, GL_UNSIGNED_INT, 0);
        }
        if (facesOnly) return;

        glUniform1i(guardThinLoc_, transparent ? 2 : 1);
        glLineWidth(1.0f);
        for (const auto& e : edgeDraws_) {
            if (bodyHiddenId(e.body_id) || isPassThrough(e.body_id) != transparent) continue;
            glBindVertexArray(e.vao);
            glDrawArrays(GL_LINES, 0, e.count);
        }
#ifndef CHUDWARE_GLES3
        glEnable(GL_PROGRAM_POINT_SIZE);
#ifdef GL_POINT_SPRITE
        glEnable(GL_POINT_SPRITE);
#endif
#endif
        for (const auto& v : vertDraws_) {
            if (bodyHiddenId(v.body_id) || isPassThrough(v.body_id) != transparent) continue;
            glBindVertexArray(v.vao);
            glDrawArrays(GL_POINTS, 0, v.count);
        }
    };
    if (passThrough_[0] || passThrough_[1] || passThrough_[2]) {
        drawLayer(true);
        // Preserve the transparent IDs only where no opaque geometry is drawn.
        glClearBufferfv(GL_DEPTH, 0, &clearDepth);
    }
    drawLayer(false);
    if (dither) glEnable(GL_DITHER);
    glActiveTexture(GL_TEXTURE0);
    glBindVertexArray(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // The id buffer now reflects `key`; reuse it until one of these inputs changes.
    target.generation = ++renderGen_;
    target.key = key;
    target.valid = true;
    return true;
}

IdPass::PickBlock IdPass::pickBlock(int px, int py, int finalW, int finalH, int radiusPx,
                                    bool vertices) const {
    const auto& target = targets_[activeTarget_];
    PickBlock block;
    block.cx = std::clamp((int)((px + 0.5f) * target.w / finalW), 0, target.w - 1);
    block.cy = std::clamp(target.h - 1 - (int)((py + 0.5f) * target.h / finalH), 0, target.h - 1);
    block.radius = std::max(0, (int)std::ceil((float)radiusPx * target.w / finalW));
    block.x = std::max(0, block.cx - block.radius);
    block.y = std::max(0, block.cy - block.radius);
    block.w = std::min(target.w - 1, block.cx + block.radius) - block.x + 1;
    block.h = std::min(target.h - 1, block.cy + block.radius) - block.y + 1;
    block.vertices = vertices;
    return block;
}

PickHit IdPass::decodeBlock(const PickBlock& block, const void* pixels) const {
    auto idAt = [&](size_t i) {
        if (!rgba8_) return static_cast<const uint32_t*>(pixels)[i];
        const auto* p = static_cast<const unsigned char*>(pixels) + i * 4;
        return uint32_t(p[0]) | (uint32_t(p[1]) << 8) |
               (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
    };
    const size_t centerIndex = (size_t)(block.cy - block.y) * block.w + block.cx - block.x;
    const uint32_t centerId = idAt(centerIndex);
    const PickHit center = decodeId(centerId);
    if (center.kind == PickKind::Edge || block.radius == 0 ||
        (center.kind == PickKind::Vertex && block.vertices))
        return center;
    // Depth is encoded in a second RGBA8 attachment, including on WebGL where depth-buffer
    // readback is unavailable. Do not snap across the silhouette to a rear component.
    auto depthAt = [&](size_t i) {
        const auto* p = static_cast<const unsigned char*>(pixels) +
                        ((size_t)block.w * block.h + i) * 4;
        const uint32_t bits = uint32_t(p[0]) | (uint32_t(p[1]) << 8) |
                              (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
        float depth;
        std::memcpy(&depth, &bits, sizeof(depth));
        return depth;
    };
    const bool opaqueCenter = center.valid() &&
        std::find(passThrough_.begin(), passThrough_.end(), center.body_id) == passThrough_.end();
    const float centerDepth = opaqueCenter ? depthAt(centerIndex) : 1.0f;
    float slopeX = 0.0f, slopeY = 0.0f;
    if (opaqueCenter && center.kind == PickKind::Face) {
        const int x = block.cx - block.x, y = block.cy - block.y;
        auto slope = [&](int nx, int ny) {
            if (nx < 0 || nx >= block.w || ny < 0 || ny >= block.h) return 0.0f;
            const size_t n = (size_t)ny * block.w + nx;
            return idAt(n) == centerId ? std::fabs(depthAt(n) - centerDepth) : 0.0f;
        };
        slopeX = std::max(slope(x - 1, y), slope(x + 1, y));
        slopeY = std::max(slope(x, y - 1), slope(x, y + 1));
    }
    long bestVD2 = (long)block.radius * block.radius + 1;
    long bestED2 = bestVD2;
    PickHit bestV, bestE;
    for (int j = 0; j < block.h; ++j) {
        for (int i = 0; i < block.w; ++i) {
            const uint32_t id = idAt((size_t)j * block.w + i);
            if (!id || bodyHidden(id)) continue;
            const PickKind kind = PickKind((id >> 18) & 3u);
            const long dx = block.x + i - block.cx, dy = block.y + j - block.cy;
            const long d2 = dx * dx + dy * dy;
            if (opaqueCenter && (id >> 20) != center.body_id) {
                const float tolerance = 4.0f / 16777215.0f +
                    slopeX * (float)std::abs(dx) + slopeY * (float)std::abs(dy);
                if (depthAt((size_t)j * block.w + i) > centerDepth + tolerance) continue;
            }
            if (kind == PickKind::Vertex && block.vertices && d2 < bestVD2) {
                bestVD2 = d2; bestV = decodeId(id);
            } else if (kind == PickKind::Edge && d2 < bestED2) {
                bestED2 = d2; bestE = decodeId(id);
            }
        }
    }
    return bestV.valid() ? bestV : bestE.valid() ? bestE : center;
}

PickHit IdPass::pickFinal(int px, int py, int finalW, int finalH) {
    return pickFinalPreferEdge(px, py, finalW, finalH, 0);
}

PickHit IdPass::pickFinalPreferEdge(int px, int py, int finalW, int finalH, int radiusPx,
                                    bool vertices) {
    const auto& target = targets_[activeTarget_];
    if (!built_ || !target.valid || finalW <= 0 || finalH <= 0 ||
        px < 0 || py < 0 || px >= finalW || py >= finalH) return {};
    radiusPx = std::max(0, radiusPx);
    if (pickMemo_.matches(target.generation, px, py, finalW, finalH, radiusPx, vertices))
        return pickMemo_.hit;
    const PickBlock block = pickBlock(px, py, finalW, finalH, radiusPx, vertices);
    // Tool radii are small; keep reusable scratch bounded by the actual target dimensions.
    const size_t count = (size_t)block.w * block.h;
    readbackPixels_.resize(count * (block.radius > 0 ? 2 : 1));
    glBindFramebuffer(GL_FRAMEBUFFER, target.fbo);
    glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadBuffer(GL_COLOR_ATTACHMENT0);
    glReadPixels(block.x, block.y, block.w, block.h,
                 rgba8_ ? GL_RGBA : GL_RED_INTEGER,
                 rgba8_ ? GL_UNSIGNED_BYTE : GL_UNSIGNED_INT, readbackPixels_.data());
    if (block.radius > 0) {
        glReadBuffer(GL_COLOR_ATTACHMENT1);
        glReadPixels(block.x, block.y, block.w, block.h, GL_RGBA, GL_UNSIGNED_BYTE,
                     readbackPixels_.data() + count);
        glReadBuffer(GL_COLOR_ATTACHMENT0);
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    const PickHit hit = decodeBlock(block, readbackPixels_.data());
    pickMemo_ = {target.generation, px, py, finalW, finalH, radiusPx, vertices, hit, true};
    return hit;
}

bool IdPass::hoverPickPending() const {
    for (const auto& read : hoverReads_)
        if (read.fence) return true;
    return false;
}

PickHit IdPass::pickFinalPreferEdgeAsync(int px, int py, int finalW, int finalH, int radiusPx,
                                         bool vertices) {
#ifdef __EMSCRIPTEN__
    // WebGL2 has no mapped PBO reads; retain synchronous browser picking.
    return pickFinalPreferEdge(px, py, finalW, finalH, radiusPx, vertices);
#else
    const auto& target = targets_[activeTarget_];
    if (!built_ || !target.valid || finalW <= 0 || finalH <= 0 ||
        px < 0 || py < 0 || px >= finalW || py >= finalH) return {};
    radiusPx = std::max(0, radiusPx);
    const PickBlock wanted = pickBlock(px, py, finalW, finalH, radiusPx, vertices);
    auto covers = [&](const HoverRead& read) {
        return read.request.valid && read.request.generation == target.generation &&
               read.request.finalW == finalW && read.request.finalH == finalH &&
               read.request.radius == radiusPx && read.request.vertices == vertices &&
               read.block.x <= wanted.x && read.block.y <= wanted.y &&
               read.block.x + read.block.w >= wanted.x + wanted.w &&
               read.block.y + read.block.h >= wanted.y + wanted.h;
    };
    HoverRead* newest = nullptr;
    for (auto& read : hoverReads_) {
        if (!read.fence) continue;
        const GLenum state = glClientWaitSync(static_cast<GLsync>(read.fence), 0, 0);
        if (state == GL_TIMEOUT_EXPIRED) continue;
        glDeleteSync(static_cast<GLsync>(read.fence));
        read.fence = nullptr;
        if ((state == GL_ALREADY_SIGNALED || state == GL_CONDITION_SATISFIED) &&
            covers(read) && read.serial > completedHoverSerial_ &&
            (!newest || read.serial > newest->serial))
            newest = &read;
    }
    bool prefetch = false;
    if (newest) {
        glBindBuffer(GL_PIXEL_PACK_BUFFER, newest->pbo);
        const void* pixels = glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0,
            (size_t)newest->block.w * newest->block.h * sizeof(uint32_t) * 2, GL_MAP_READ_BIT);
        if (pixels) {
            PickBlock current = newest->block;
            current.cx = wanted.cx;
            current.cy = wanted.cy;
            current.radius = wanted.radius;
            const PickHit hit = decodeBlock(current, pixels);
            const bool valid = glUnmapBuffer(GL_PIXEL_PACK_BUFFER) == GL_TRUE;
            if (valid) {
                prefetch = newest->request.px != px || newest->request.py != py;
                hoverMemo_ = {target.generation, px, py, finalW, finalH, radiusPx, vertices, hit, true};
                completedHoverSerial_ = newest->serial;
            }
        }
        glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    }
    const bool haveHit = hoverMemo_.matches(target.generation, px, py, finalW, finalH,
                                            radiusPx, vertices);
    if (haveHit && !prefetch) return hoverMemo_.hit;
    HoverRead* freeSlot = nullptr;
    bool alreadyPending = false;
    for (auto& read : hoverReads_) {
        if (!read.fence) { if (!freeSlot) freeSlot = &read; }
        else if (covers(read)) alreadyPending = true;
    }
    // Never recycle a busy buffer: glBufferData on an in-flight slot may stall its driver.
    if (!alreadyPending && freeSlot) {
        auto& read = *freeSlot;
        // Margin permits a completed read to be re-centered on a moving cursor without ever
        // publishing the old cursor's potentially occluded hit.
        read.block = pickBlock(px, py, finalW, finalH, radiusPx + 8, vertices);
        read.request = {target.generation, px, py, finalW, finalH, radiusPx, vertices, {}, true};
        read.serial = ++hoverSerial_;
        if (!read.pbo) glGenBuffers(1, &read.pbo);
        glBindBuffer(GL_PIXEL_PACK_BUFFER, read.pbo);
        const size_t planeBytes = (size_t)read.block.w * read.block.h * sizeof(uint32_t);
        const size_t bytes = planeBytes * 2;
        if (read.capacity < bytes) {
            glBufferData(GL_PIXEL_PACK_BUFFER, bytes, nullptr, GL_STREAM_READ);
            read.capacity = bytes;
        }
        glBindFramebuffer(GL_FRAMEBUFFER, target.fbo);
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        glReadBuffer(GL_COLOR_ATTACHMENT0);
        glReadPixels(read.block.x, read.block.y, read.block.w, read.block.h,
                     rgba8_ ? GL_RGBA : GL_RED_INTEGER,
                     rgba8_ ? GL_UNSIGNED_BYTE : GL_UNSIGNED_INT, nullptr);
        glReadBuffer(GL_COLOR_ATTACHMENT1);
        glReadPixels(read.block.x, read.block.y, read.block.w, read.block.h, GL_RGBA,
                     GL_UNSIGNED_BYTE, reinterpret_cast<void*>(planeBytes));
        glReadBuffer(GL_COLOR_ATTACHMENT0);
        read.fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
    }
    return haveHit ? hoverMemo_.hit : PickHit{};
#endif
}

}  // namespace render
