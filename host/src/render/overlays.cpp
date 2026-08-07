#include "overlays.h"

#include "gl_compat.h"
#include "ui/viewport/glsl_compat.h"


#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>

namespace render {

namespace {

const char* kVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
uniform vec3 uOffset;
out vec3 vWorld;
void main(){
    vWorld = aPos + uOffset;
    gl_Position = uMVP * vec4(vWorld, 1.0);
})";

const char* kFrag = R"(#version 330 core
uniform vec4 uColor;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
in vec3 vWorld;
out vec4 frag;
void main(){
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    frag = uColor;
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
        std::fprintf(stderr, "overlays shader compile error: %s\n", log);
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

Overlays::~Overlays() {
    destroy();
    if (prog_) glDeleteProgram(prog_);
    if (vao_) glDeleteVertexArrays(1, &vao_);
    if (vbo_) glDeleteBuffers(1, &vbo_);
    if (segVao_) glDeleteVertexArrays(1, &segVao_);
    if (segVbo_) glDeleteBuffers(1, &segVbo_);
    if (triVao_) glDeleteVertexArrays(1, &triVao_);
    if (triVbo_) glDeleteBuffers(1, &triVbo_);
    if (camMarkerVao_) glDeleteVertexArrays(1, &camMarkerVao_);
    if (camMarkerVbo_) glDeleteBuffers(1, &camMarkerVbo_);
}

void Overlays::destroy() {
    for (auto& kv : faces_) {
        if (kv.second.vao) glDeleteVertexArrays(1, &kv.second.vao);
        // kv.second.vbo is shared (owned by bodyVbos_) — do NOT delete it here.
        if (kv.second.ebo) glDeleteBuffers(1, &kv.second.ebo);
    }
    faces_.clear();
    for (unsigned int vbo : bodyVbos_)
        if (vbo) glDeleteBuffers(1, &vbo);
    bodyVbos_.clear();
    for (auto& kv : edges_) {
        if (kv.second.vao) glDeleteVertexArrays(1, &kv.second.vao);
        if (kv.second.vbo) glDeleteBuffers(1, &kv.second.vbo);
    }
    edges_.clear();
    verts_.clear();
}

void Overlays::setColors(const float hover[4], const float selection[4], const float feature[4]) {
    for (int i = 0; i < 4; ++i) {
        hoverCol_[i] = hover[i];
        selCol_[i] = selection[i];
        if (feature) featHlCol_[i] = feature[i];
    }
    // Selected+hovered tier tracks the live selection color: same hue, darkened and more opaque so
    // hovering an already-selected item reads as "still selected, under the cursor" — clearly darker
    // than plain selection and distinct from the (lighter) hover wash. Applies to all pick kinds.
    for (int i = 0; i < 3; ++i) selHoverCol_[i] = selCol_[i] * 0.55f;
    selHoverCol_[3] = selCol_[3] < 0.80f ? selCol_[3] + 0.20f : 1.0f;
}

void Overlays::setSectionClip(int count, const float normals[3][3], const float offsets[3]) {
    clipCount_ = count < 0 ? 0 : (count > 3 ? 3 : count);
    for (int i = 0; i < clipCount_; ++i) {
        clipNormals_[i][0] = normals[i][0];
        clipNormals_[i][1] = normals[i][1];
        clipNormals_[i][2] = normals[i][2];
        clipOffsets_[i] = offsets[i];
    }
}

bool Overlays::setDoc(const DocMesh& doc, std::string& err) {
    if (!prog_) {
        prog_ = linkProgram(kVert, kFrag);
        // Cache uniform locations once — every draw path used to re-query them by string.
        uMVP_       = glGetUniformLocation(prog_, "uMVP");
        uColor_     = glGetUniformLocation(prog_, "uColor");
        uOffset_    = glGetUniformLocation(prog_, "uOffset");
        uClipCount_ = glGetUniformLocation(prog_, "uClipCount");
        uClipN_     = glGetUniformLocation(prog_, "uClipN");
        uClipOff_   = glGetUniformLocation(prog_, "uClipOff");
    }
    int linked = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &linked);
    if (!linked) { err = "overlays program link failed"; return false; }

    if (!vao_) {
        glGenVertexArrays(1, &vao_);
        glGenBuffers(1, &vbo_);
    }

    destroy();  // drop per-body buffers only (prog_ and the scratch vao_/vbo_ are kept)

    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        // One shared position VBO per body — every face range of this body references it via its
        // own VAO+EBO, instead of re-uploading the whole vertex array once per face.
        unsigned int posVbo = 0;
        glGenBuffers(1, &posVbo);
        glBindBuffer(GL_ARRAY_BUFFER, posVbo);
        glBufferData(GL_ARRAY_BUFFER, b.pos.size() * sizeof(float), b.pos.data(), GL_STATIC_DRAW);
        bodyVbos_.push_back(posVbo);
        for (const auto& fr : b.faces) {
            if (fr.tri_count == 0) continue;
            FaceBuf fb;
            fb.vbo = posVbo;  // shared (non-owning)
            glGenVertexArrays(1, &fb.vao);
            glGenBuffers(1, &fb.ebo);
            glBindVertexArray(fb.vao);
            glBindBuffer(GL_ARRAY_BUFFER, fb.vbo);
            glEnableVertexAttribArray(0);
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
            const uint32_t* base = b.idx.data() + (size_t)fr.first_tri * 3;
            int count = (int)fr.tri_count * 3;
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, fb.ebo);
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, (size_t)count * sizeof(uint32_t), base,
                         GL_STATIC_DRAW);
            glBindVertexArray(0);
            fb.idxCount = count;
            faces_[key(b.body_id, fr.face_id)] = fb;
        }
        for (const auto& es : b.edges) {
            if (es.pt_count < 2) continue;
            std::vector<float> lines;
            lines.reserve((size_t)(es.pt_count - 1) * 6);
            for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
                const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
                const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
                lines.insert(lines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
            }
            EdgeBuf eb;
            glGenVertexArrays(1, &eb.vao);
            glGenBuffers(1, &eb.vbo);
            glBindVertexArray(eb.vao);
            glBindBuffer(GL_ARRAY_BUFFER, eb.vbo);
            glBufferData(GL_ARRAY_BUFFER, lines.size() * sizeof(float), lines.data(),
                         GL_STATIC_DRAW);
            glEnableVertexAttribArray(0);
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
            glBindVertexArray(0);
            eb.vertCount = (int)lines.size() / 3;
            edges_[key(b.body_id, es.edge_id)] = eb;
        }
        for (const auto& v : b.brep_verts) {
            verts_[key(b.body_id, v.vertex_id)] =
                std::array<float, 3>{v.xyz[0], v.xyz[1], v.xyz[2]};
        }
    }
    return true;
}

const Overlays::FaceBuf* Overlays::findFace(uint32_t body, uint32_t face_id) const {
    auto it = faces_.find(key(body, face_id));
    return it == faces_.end() ? nullptr : &it->second;
}
const Overlays::EdgeBuf* Overlays::findEdge(uint32_t body, uint32_t edge_id) const {
    auto it = edges_.find(key(body, edge_id));
    return it == edges_.end() ? nullptr : &it->second;
}
bool Overlays::findVert(uint32_t body, uint32_t vert_id, float out[3]) const {
    auto it = verts_.find(key(body, vert_id));
    if (it == verts_.end()) return false;
    out[0] = it->second[0]; out[1] = it->second[1]; out[2] = it->second[2];
    return true;
}

// Bind the flat-color program + the per-frame camera / section-clip uniforms shared by every
// draw path (draw / drawFaceWash / drawSegments / drawTriangles).
void Overlays::bindCommon(const Renderer::CameraXform& cam) {
    glUseProgram(prog_);
    glUniformMatrix4fv(uMVP_, 1, GL_FALSE, cam.mvp.m);
    const float zeroOffset[3] = {0.0f, 0.0f, 0.0f};
    glUniform3fv(uOffset_, 1, zeroOffset);
    glUniform1i(uClipCount_, clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(uClipN_, clipCount_, clipNormals_[0]);
        glUniform1fv(uClipOff_, clipCount_, clipOffsets_);
    }
}

void Overlays::drawHit(const Renderer::CameraXform& cam, const PickHit& h, const float rgba[4]) {
    if (h.kind == PickKind::Face) {
        const FaceBuf* fb = findFace(h.body_id, h.topo_id);
        if (!fb) return;
        // Translucent wash on the face, biased slightly toward the camera so it reads on top.
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(-1.0f, -1.0f);
        glUniform4fv(uColor_, 1, rgba);
        glBindVertexArray(fb->vao);
        glDrawElements(GL_TRIANGLES, fb->idxCount, GL_UNSIGNED_INT, 0);
        glDisable(GL_POLYGON_OFFSET_FILL);
        glDisable(GL_BLEND);
    } else if (h.kind == PickKind::Edge) {
        const EdgeBuf* eb = findEdge(h.body_id, h.topo_id);
        if (!eb) return;
        float opaque[4] = {rgba[0], rgba[1], rgba[2], 1.0f};
        glUniform4fv(uColor_, 1, opaque);
        glDepthRange(0.0, 0.999);  // bias over the shaded surface
        glLineWidth(3.5f * widthScale_);
        glBindVertexArray(eb->vao);
        glDrawArrays(GL_LINES, 0, eb->vertCount);
        glDepthRange(0.0, 1.0);
    } else if (h.kind == PickKind::Vertex) {
        float xyz[3];
        if (!findVert(h.body_id, h.topo_id, xyz)) return;
        glBindVertexArray(vao_);
        glBindBuffer(GL_ARRAY_BUFFER, vbo_);
        glBufferData(GL_ARRAY_BUFFER, 3 * sizeof(float), xyz, GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
#ifndef CHUDWARE_GLES3
        glEnable(GL_PROGRAM_POINT_SIZE);
#endif
        glDepthRange(0.0, 0.997);
        // Contrast rim first (a larger dark point behind), then the highlight dot on top — the
        // bare 9px point downsampled to ~3px used to be the weakest highlight kind.
        float rim[4] = {0.06f, 0.07f, 0.09f, 1.0f};
        glUniform4fv(uColor_, 1, rim);
#ifndef CHUDWARE_GLES3
        glPointSize(13.0f * widthScale_);
#endif
        glDrawArrays(GL_POINTS, 0, 1);
        float opaque[4] = {rgba[0], rgba[1], rgba[2], 1.0f};
        glUniform4fv(uColor_, 1, opaque);
#ifndef CHUDWARE_GLES3
        glPointSize(9.0f * widthScale_);
#endif
        glDrawArrays(GL_POINTS, 0, 1);
        glDepthRange(0.0, 1.0);
    }
}

// C37-C: draw a set of faces with the feature-highlight tint.
void Overlays::drawFeatureHighlight(const Renderer::CameraXform& cam,
                                    const std::vector<uint64_t>& faceKeys) {
    drawFaceWash(cam, faceKeys, featHlCol_);
}

// Draw a set of faces filled with an explicit RGBA wash (simulation fixture/load previews).
void Overlays::drawFaceWash(const Renderer::CameraXform& cam,
                            const std::vector<uint64_t>& faceKeys, const float rgba[4]) {
    if (faceKeys.empty() || !prog_) return;
    bindCommon(cam);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(-1.0f, -1.0f);
    glUniform4fv(uColor_, 1, rgba);
    for (uint64_t k : faceKeys) {
        const FaceBuf* fb = findFace((uint32_t)(k >> 32), (uint32_t)(k & 0xFFFFFFFFu));
        if (!fb) continue;  // stale key after rebuild — skip silently
        glBindVertexArray(fb->vao);
        glDrawElements(GL_TRIANGLES, fb->idxCount, GL_UNSIGNED_INT, 0);
    }
    glDisable(GL_POLYGON_OFFSET_FILL);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

// Wash every face of one body with an explicit RGBA (interference-view body tint). Same GL state
// as drawFaceWash but keyed on the body high-word so callers needn't enumerate face ids.
void Overlays::drawBodyWash(const Renderer::CameraXform& cam, uint32_t bodyId, const float rgba[4]) {
    if (faces_.empty() || !prog_) return;
    bindCommon(cam);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(-1.0f, -1.0f);
    glUniform4fv(uColor_, 1, rgba);
    for (const auto& kv : faces_) {
        if ((uint32_t)(kv.first >> 32) != bodyId) continue;
        glBindVertexArray(kv.second.vao);
        glDrawElements(GL_TRIANGLES, kv.second.idxCount, GL_UNSIGNED_INT, 0);
    }
    glDisable(GL_POLYGON_OFFSET_FILL);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

void Overlays::drawSegments(const Renderer::CameraXform& cam, const std::vector<float>& xyz,
                            const float rgba[4], float lineWidth, bool overSurface) {
    if (xyz.size() < 6 || !prog_) return;
    if (!segVao_) { glGenVertexArrays(1, &segVao_); glGenBuffers(1, &segVbo_); }
    bindCommon(cam);
    glUniform4fv(uColor_, 1, rgba);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    if (overSurface) glDepthRange(0.0, 0.999);  // bias over the shaded surface like edge highlights
    glBindVertexArray(segVao_);
    glBindBuffer(GL_ARRAY_BUFFER, segVbo_);
    glBufferData(GL_ARRAY_BUFFER, xyz.size() * sizeof(float), xyz.data(), GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    // Scale by supersample x uiScale so the requested width survives the SS downsample (the
    // renderer's own feature edges do the same; unscaled widths landed at 1/3 the authored size).
    glLineWidth(lineWidth * widthScale_);
    glDrawArrays(GL_LINES, 0, (int)(xyz.size() / 3));
    if (overSurface) glDepthRange(0.0, 1.0);
    glBindVertexArray(0);
}

void Overlays::drawCamSegments(const Renderer::CameraXform& cam,
                               const std::vector<float>& xyz,
                               const std::vector<float>& endTimes, double playTime,
                               bool progressActive, const float rgba[4], float lineWidth) {
    const size_t segmentCount = xyz.size() / 6;
    if (segmentCount == 0 || !prog_) return;

    size_t completed = segmentCount;
    if (progressActive) {
        const size_t timedCount = std::min(segmentCount, endTimes.size());
        completed = (size_t)(std::upper_bound(endTimes.begin(),
                                             endTimes.begin() + (std::ptrdiff_t)timedCount,
                                             (float)playTime) -
                             endTimes.begin());
    }

    if (!segVao_) {
        glGenVertexArrays(1, &segVao_);
        glGenBuffers(1, &segVbo_);
    }
    bindCommon(cam);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDepthMask(GL_FALSE);
    glBindVertexArray(segVao_);
    glBindBuffer(GL_ARRAY_BUFFER, segVbo_);
    glBufferData(GL_ARRAY_BUFFER, segmentCount * 6 * sizeof(float), xyz.data(), GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);

    const size_t pending = segmentCount - completed;
    const float pendingGlow[4] = {rgba[0], rgba[1], rgba[2], rgba[3] * 0.10f};
    const float completeGlow[4] = {rgba[0], rgba[1], rgba[2], rgba[3] * 0.22f};
    const float pendingCore[4] = {rgba[0], rgba[1], rgba[2], rgba[3] * 0.32f};
    auto drawRange = [&](size_t firstSegment, size_t count, const float color[4], float width) {
        if (count == 0) return;
        glUniform4fv(uColor_, 1, color);
        glLineWidth(width * widthScale_);
        glDrawArrays(GL_LINES, (int)(firstSegment * 2), (int)(count * 2));
    };

    // Wide low-alpha passes soften the supersampled line silhouette. The core passes retain the
    // class color, while the not-yet-played suffix stays visible without competing with progress.
    drawRange(completed, pending, pendingGlow, lineWidth + 2.0f);
    drawRange(0, completed, completeGlow, lineWidth + 2.0f);
    drawRange(completed, pending, pendingCore, lineWidth);
    drawRange(0, completed, rgba, lineWidth);

    glBindVertexArray(0);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
}

void Overlays::drawCamToolMarker(const Renderer::CameraXform& cam, const float position[3],
                                 const float rgba[4]) {
    if (!prog_ || !position) return;

    constexpr int kSlices = 12;
    constexpr int kCutterVerts = kSlices * 9;   // cone side + cutter shaft
    constexpr int kSpindleVerts = kSlices * 15; // shoulder + spindle shell + top cap
    if (!camMarkerVao_) {
        static constexpr float kRing[kSlices][2] = {
            { 1.0000000f,  0.0000000f}, { 0.8660254f,  0.5000000f},
            { 0.5000000f,  0.8660254f}, { 0.0000000f,  1.0000000f},
            {-0.5000000f,  0.8660254f}, {-0.8660254f,  0.5000000f},
            {-1.0000000f,  0.0000000f}, {-0.8660254f, -0.5000000f},
            {-0.5000000f, -0.8660254f}, { 0.0000000f, -1.0000000f},
            { 0.5000000f, -0.8660254f}, { 0.8660254f, -0.5000000f},
        };
        std::array<float, (kCutterVerts + kSpindleVerts) * 3> verts{};
        size_t used = 0;
        auto vertex = [&](float x, float y, float z) {
            verts[used++] = x;
            verts[used++] = y;
            verts[used++] = z;
        };
        auto triangle = [&](float ax, float ay, float az,
                            float bx, float by, float bz,
                            float cx, float cy, float cz) {
            vertex(ax, ay, az);
            vertex(bx, by, bz);
            vertex(cx, cy, cz);
        };
        auto quad = [&](float ax, float ay, float az,
                        float bx, float by, float bz,
                        float cx, float cy, float cz,
                        float dx, float dy, float dz) {
            triangle(ax, ay, az, bx, by, bz, cx, cy, cz);
            triangle(ax, ay, az, cx, cy, cz, dx, dy, dz);
        };

        constexpr float cutterRadius = 2.0f;
        constexpr float spindleRadius = 4.0f;
        for (int i = 0; i < kSlices; ++i) {
            const int j = (i + 1) % kSlices;
            const float x0 = kRing[i][0], y0 = kRing[i][1];
            const float x1 = kRing[j][0], y1 = kRing[j][1];
            triangle(0.0f, 0.0f, 0.0f,
                     x1 * cutterRadius, y1 * cutterRadius, 8.0f,
                     x0 * cutterRadius, y0 * cutterRadius, 8.0f);
            quad(x0 * cutterRadius, y0 * cutterRadius, 8.0f,
                 x1 * cutterRadius, y1 * cutterRadius, 8.0f,
                 x1 * cutterRadius, y1 * cutterRadius, 18.0f,
                 x0 * cutterRadius, y0 * cutterRadius, 18.0f);
        }
        for (int i = 0; i < kSlices; ++i) {
            const int j = (i + 1) % kSlices;
            const float x0 = kRing[i][0], y0 = kRing[i][1];
            const float x1 = kRing[j][0], y1 = kRing[j][1];
            quad(x0 * cutterRadius, y0 * cutterRadius, 18.0f,
                 x1 * cutterRadius, y1 * cutterRadius, 18.0f,
                 x1 * spindleRadius, y1 * spindleRadius, 18.0f,
                 x0 * spindleRadius, y0 * spindleRadius, 18.0f);
            quad(x0 * spindleRadius, y0 * spindleRadius, 18.0f,
                 x1 * spindleRadius, y1 * spindleRadius, 18.0f,
                 x1 * spindleRadius, y1 * spindleRadius, 30.0f,
                 x0 * spindleRadius, y0 * spindleRadius, 30.0f);
            triangle(0.0f, 0.0f, 30.0f,
                     x0 * spindleRadius, y0 * spindleRadius, 30.0f,
                     x1 * spindleRadius, y1 * spindleRadius, 30.0f);
        }

        glGenVertexArrays(1, &camMarkerVao_);
        glGenBuffers(1, &camMarkerVbo_);
        glBindVertexArray(camMarkerVao_);
        glBindBuffer(GL_ARRAY_BUFFER, camMarkerVbo_);
        glBufferData(GL_ARRAY_BUFFER, used * sizeof(float), verts.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindVertexArray(0);
    }

    bindCommon(cam);
    glUniform3fv(uOffset_, 1, position);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDepthMask(GL_FALSE);
    glDisable(GL_CULL_FACE);
    glBindVertexArray(camMarkerVao_);
    const float spindle[4] = {rgba[0], rgba[1], rgba[2], rgba[3] * 0.48f};
    glUniform4fv(uColor_, 1, spindle);
    glDrawArrays(GL_TRIANGLES, kCutterVerts, kSpindleVerts);
    glUniform4fv(uColor_, 1, rgba);
    glDrawArrays(GL_TRIANGLES, 0, kCutterVerts);
    glBindVertexArray(0);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
    const float zeroOffset[3] = {0.0f, 0.0f, 0.0f};
    glUniform3fv(uOffset_, 1, zeroOffset);
}

void Overlays::drawTriangles(const Renderer::CameraXform& cam, const std::vector<float>& xyz,
                             const float rgba[4], bool overSurface) {
    if (xyz.size() < 9 || !prog_) return;
    if (!triVao_) { glGenVertexArrays(1, &triVao_); glGenBuffers(1, &triVbo_); }
    bindCommon(cam);
    glUniform4fv(uColor_, 1, rgba);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDisable(GL_CULL_FACE);  // glyph cones/quads are viewed from either side
    if (overSurface) glDepthRange(0.0, 0.999);
    glBindVertexArray(triVao_);
    glBindBuffer(GL_ARRAY_BUFFER, triVbo_);
    glBufferData(GL_ARRAY_BUFFER, xyz.size() * sizeof(float), xyz.data(), GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    glDrawArrays(GL_TRIANGLES, 0, (int)(xyz.size() / 3));
    if (overSurface) glDepthRange(0.0, 1.0);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

void Overlays::draw(const Renderer::CameraXform& cam, const PickHit& hover,
                    const std::vector<PickHit>& sel) {
    if (!prog_) return;
    bindCommon(cam);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);

    // Selection first (persistent), hover on top (transient). A hover over an UNselected item shows
    // the lighter hover wash; a hover over an already-SELECTED item shows the darker selected+hover
    // wash so "what's selected" stays readable. Applies uniformly to faces, edges and vertices.
    for (const auto& h : sel) drawHit(cam, h, selCol_);
    if (hover.valid()) {
        bool hoverSelected = false;
        for (const auto& h : sel) { if (h == hover) { hoverSelected = true; break; } }
        drawHit(cam, hover, hoverSelected ? selHoverCol_ : hoverCol_);
    }

    glBindVertexArray(0);
}

}  // namespace render
