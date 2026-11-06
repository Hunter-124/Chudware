#include "overlays.h"

#include "gl_compat.h"
#include "ui/viewport/glsl_compat.h"


#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>

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

const char* kLitVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
layout(location=2) in vec3 aColor;
uniform mat4 uMVP;
out vec3 vWorld;
out vec3 vNormal;
out vec3 vColor;
void main(){
    vWorld = aPos;
    vNormal = aNormal;
    vColor = aColor;
    gl_Position = uMVP * vec4(aPos, 1.0);
})";

const char* kLitFrag = R"(#version 330 core
uniform vec4 uColor;
uniform bool uHasColor;
uniform vec3 uViewDir;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
in vec3 vWorld;
in vec3 vNormal;
in vec3 vColor;
out vec4 frag;
void main(){
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    vec3 n = normalize(vNormal);
    float lambert = abs(dot(n, uViewDir));
    float specular = 0.12 * pow(lambert, 28.0);
    vec3 color = uHasColor ? vColor : uColor.rgb;
    frag = vec4(color * (0.28 + 0.72 * lambert) + color * specular, uColor.a);
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

Overlays::Overlays() {
    ensureCommonProgram();
    glGenVertexArrays(1, &vao_);
    glGenBuffers(1, &vbo_);
    glBindVertexArray(vao_);
    glBindBuffer(GL_ARRAY_BUFFER, vbo_);
    glBufferData(GL_ARRAY_BUFFER, 3 * sizeof(float), nullptr, GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    glBindVertexArray(0);
}

Overlays::~Overlays() {
    destroy();
    if (prog_) glDeleteProgram(prog_);
    if (litProg_) glDeleteProgram(litProg_);
    if (vao_) glDeleteVertexArrays(1, &vao_);
    if (vbo_) glDeleteBuffers(1, &vbo_);
    if (segVao_) glDeleteVertexArrays(1, &segVao_);
    if (segVbo_) glDeleteBuffers(1, &segVbo_);
    if (camMarkerVao_) glDeleteVertexArrays(1, &camMarkerVao_);
    if (camMarkerVbo_) glDeleteBuffers(1, &camMarkerVbo_);
}

void Overlays::destroy() {
    clearSimulationBuffers();
    clearGeometryBuffers();
    for (auto& body : bodies_) {
        if (body.vao) glDeleteVertexArrays(1, &body.vao);
        if (body.vbo) glDeleteBuffers(1, &body.vbo);
        if (body.ebo) glDeleteBuffers(1, &body.ebo);
    }
    bodies_.clear();
    bodyIndices_.clear();
    faces_.clear();
    edges_.clear();
    verts_.clear();
    faceBatchCounts_.clear();
    faceBatchOffsets_.clear();
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

void Overlays::ensureCommonProgram() {
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
    if (!litProg_) {
        litProg_ = linkProgram(kLitVert, kLitFrag);
        uLitMVP_ = glGetUniformLocation(litProg_, "uMVP");
        uLitColor_ = glGetUniformLocation(litProg_, "uColor");
        uLitHasColor_ = glGetUniformLocation(litProg_, "uHasColor");
        uLitViewDir_ = glGetUniformLocation(litProg_, "uViewDir");
        uLitClipCount_ = glGetUniformLocation(litProg_, "uClipCount");
        uLitClipN_ = glGetUniformLocation(litProg_, "uClipN");
        uLitClipOff_ = glGetUniformLocation(litProg_, "uClipOff");
    }
}

bool Overlays::setDoc(const DocMesh& doc, std::string& err) {
    int linked = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &linked);
    if (!linked) { err = "overlays program link failed"; return false; }
    glGetProgramiv(litProg_, GL_LINK_STATUS, &linked);
    if (!linked) { err = "overlays lit program link failed"; return false; }

    destroy();
    size_t faceCount = 0, edgeCount = 0, vertCount = 0;
    for (const auto& b : doc.bodies) {
        faceCount += b.faces.size();
        edgeCount += b.edges.size();
        vertCount += b.brep_verts.size();
    }
    bodies_.reserve(doc.bodies.size());
    bodyIndices_.reserve(doc.bodies.size());
    faces_.reserve(faceCount);
    edges_.reserve(edgeCount);
    verts_.reserve(vertCount);
    faceBatchCounts_.reserve(faceCount);
    faceBatchOffsets_.reserve(faceCount);

    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        const size_t bodyIndex = bodies_.size();
        bodies_.emplace_back();
        BodyBuf& body = bodies_.back();
        bodyIndices_[b.body_id] = bodyIndex;
        body.faceCounts.reserve(b.faces.size());
        body.faceOffsets.reserve(b.faces.size());

        // Parsed documents already carry the flattened edge lines from the loader thread.
        // Keep the same flattening for CPU-created documents without derived data.
        std::vector<float> fallbackLines;
        if (!doc.derivedReady) {
            size_t lineFloats = 0;
            for (const auto& es : b.edges)
                if (es.pt_count >= 2) lineFloats += (size_t)(es.pt_count - 1) * 6;
            fallbackLines.reserve(lineFloats);
            for (const auto& es : b.edges) {
                for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
                    const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
                    const float* c = a + 3;
                    fallbackLines.insert(fallbackLines.end(), {a[0], a[1], a[2],
                                                               c[0], c[1], c[2]});
                }
            }
        }
        const auto& lines = doc.derivedReady ? b.edgeLines : fallbackLines;
        glGenVertexArrays(1, &body.vao);
        glGenBuffers(1, &body.vbo);
        glGenBuffers(1, &body.ebo);
        glBindVertexArray(body.vao);
        glBindBuffer(GL_ARRAY_BUFFER, body.vbo);
        glBufferData(GL_ARRAY_BUFFER, (b.pos.size() + lines.size()) * sizeof(float),
                     nullptr, GL_STATIC_DRAW);
        glBufferSubData(GL_ARRAY_BUFFER, 0, b.pos.size() * sizeof(float), b.pos.data());
        if (!lines.empty())
            glBufferSubData(GL_ARRAY_BUFFER, b.pos.size() * sizeof(float),
                            lines.size() * sizeof(float), lines.data());
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, body.ebo);
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, b.idx.size() * sizeof(uint32_t), b.idx.data(),
                     GL_STATIC_DRAW);
        glBindVertexArray(0);

        for (const auto& fr : b.faces) {
            if (fr.tri_count == 0) continue;
            const size_t firstIndex = (size_t)fr.first_tri * 3;
            const int count = (int)fr.tri_count * 3;
            faces_[key(b.body_id, fr.face_id)] = FaceBuf{bodyIndex, firstIndex, count};
            const uintptr_t offset = (uintptr_t)firstIndex * sizeof(uint32_t);
            if (!body.faceCounts.empty() &&
                body.faceCounts.back() <= std::numeric_limits<int>::max() - count &&
                (uintptr_t)body.faceOffsets.back() +
                    (size_t)body.faceCounts.back() * sizeof(uint32_t) == offset) {
                body.faceCounts.back() += count;
            } else {
                body.faceCounts.push_back(count);
                body.faceOffsets.push_back(reinterpret_cast<const void*>(offset));
            }
        }
        int firstVertex = (int)(b.pos.size() / 3);
        for (const auto& es : b.edges) {
            if (es.pt_count < 2) continue;
            const int count = (int)(es.pt_count - 1) * 2;
            edges_[key(b.body_id, es.edge_id)] = EdgeBuf{bodyIndex, firstVertex, count};
            firstVertex += count;
        }
        for (const auto& v : b.brep_verts)
            verts_[key(b.body_id, v.vertex_id)] = {v.xyz[0], v.xyz[1], v.xyz[2]};
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

void Overlays::appendFaceRange(const FaceBuf& face) {
    const uintptr_t offset = (uintptr_t)face.firstIndex * sizeof(uint32_t);
    if (!faceBatchCounts_.empty() &&
        faceBatchCounts_.back() <= std::numeric_limits<int>::max() - face.idxCount &&
        (uintptr_t)faceBatchOffsets_.back() +
            (size_t)faceBatchCounts_.back() * sizeof(uint32_t) == offset) {
        faceBatchCounts_.back() += face.idxCount;
        return;
    }
    if (faceBatchCounts_.size() >= faces_.size()) flushFaceRanges(face.body);
    faceBatchCounts_.push_back(face.idxCount);
    faceBatchOffsets_.push_back(reinterpret_cast<const void*>(offset));
}

void Overlays::drawFaceRanges(const BodyBuf& body, const std::vector<int>& counts,
                              const std::vector<const void*>& offsets) {
    if (counts.empty()) return;
    glBindVertexArray(body.vao);
#if defined(__EMSCRIPTEN__) || defined(CHUDWARE_GLES3)
    for (size_t i = 0; i < counts.size(); ++i)
        glDrawElements(GL_TRIANGLES, counts[i], GL_UNSIGNED_INT, offsets[i]);
#else
    if (counts.size() == 1)
        glDrawElements(GL_TRIANGLES, counts[0], GL_UNSIGNED_INT, offsets[0]);
    else
        glMultiDrawElements(GL_TRIANGLES, counts.data(), GL_UNSIGNED_INT, offsets.data(),
                            (int)counts.size());
#endif
}

void Overlays::flushFaceRanges(size_t body) {
    if (faceBatchCounts_.empty()) return;
    drawFaceRanges(bodies_[body], faceBatchCounts_, faceBatchOffsets_);
    faceBatchCounts_.clear();
    faceBatchOffsets_.clear();
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
        glPolygonOffset(0.0f, -1.0f);
        glUniform4fv(uColor_, 1, rgba);
        glBindVertexArray(bodies_[fb->body].vao);
        glDrawElements(GL_TRIANGLES, fb->idxCount, GL_UNSIGNED_INT,
                       reinterpret_cast<const void*>((uintptr_t)fb->firstIndex * sizeof(uint32_t)));
        glDisable(GL_POLYGON_OFFSET_FILL);
        glDisable(GL_BLEND);
    } else if (h.kind == PickKind::Edge) {
        const EdgeBuf* eb = findEdge(h.body_id, h.topo_id);
        if (!eb) return;
        float opaque[4] = {rgba[0], rgba[1], rgba[2], 1.0f};
        glUniform4fv(uColor_, 1, opaque);
        glLineWidth(3.5f * widthScale_);
        glBindVertexArray(bodies_[eb->body].vao);
        glDrawArrays(GL_LINES, eb->firstVertex, eb->vertCount);
    } else if (h.kind == PickKind::Vertex) {
        float xyz[3];
        if (!findVert(h.body_id, h.topo_id, xyz)) return;
        glBindVertexArray(vao_);
        glBindBuffer(GL_ARRAY_BUFFER, vbo_);
        glBufferSubData(GL_ARRAY_BUFFER, 0, 3 * sizeof(float), xyz);
#ifndef CHUDWARE_GLES3
        glEnable(GL_PROGRAM_POINT_SIZE);
#endif
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
    size_t body = 0;
    for (uint64_t k : faceKeys) {
        const FaceBuf* fb = findFace((uint32_t)(k >> 32), (uint32_t)(k & 0xFFFFFFFFu));
        if (!fb) continue;  // stale key after rebuild — skip silently
        // Keep caller order: only consecutive ranges of the same body are batched, so coplanar
        // translucent washes retain the same blending and depth ordering as individual draws.
        if (!faceBatchCounts_.empty() && body != fb->body) flushFaceRanges(body);
        body = fb->body;
        appendFaceRange(*fb);
    }
    flushFaceRanges(body);
    glDisable(GL_POLYGON_OFFSET_FILL);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

// Wash every face of one body with an explicit RGBA (interference-view body tint). Same GL state
// as drawFaceWash but keyed on the body high-word so callers needn't enumerate face ids.
void Overlays::drawBodyWash(const Renderer::CameraXform& cam, uint32_t bodyId, const float rgba[4]) {
    auto it = bodyIndices_.find(bodyId);
    if (it == bodyIndices_.end() || !prog_) return;
    const BodyBuf& body = bodies_[it->second];
    bindCommon(cam);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(-1.0f, -1.0f);
    glUniform4fv(uColor_, 1, rgba);
    drawFaceRanges(body, body.faceCounts, body.faceOffsets);
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

bool Overlays::updateSourceStamp(SourceStamp& stamp, const std::vector<float>& values) {
    uint64_t sample = 1469598103934665603ull ^ (uint64_t)values.size();
    const size_t samples = std::min<size_t>(16, values.size());
    for (size_t i = 0; i < samples; ++i) {
        const size_t index = samples == 1 ? 0 : i * (values.size() - 1) / (samples - 1);
        uint32_t bits = 0;
        std::memcpy(&bits, &values[index], sizeof(bits));
        sample = (sample ^ bits) * 1099511628211ull;
    }
    // Temporary preview vectors can reuse an address and size while changing an unsampled float.
    // A vectorized byte comparison keeps that case exact without the old serial full-payload hash.
    const bool unchanged = stamp.size == values.size() && stamp.sample == sample &&
                           stamp.snapshot.size() == values.size() &&
                           (values.empty() || std::memcmp(stamp.snapshot.data(), values.data(),
                                                         values.size() * sizeof(float)) == 0);
    stamp.data = values.data();
    stamp.size = values.size();
    stamp.sample = sample;
    if (!unchanged) stamp.snapshot = values;
    return unchanged;
}

Overlays::GeometryBuf& Overlays::prepareGeometryBuffer(const std::vector<float>& xyz,
                                                       unsigned int primitive) {
    GeometryBuf* slot = nullptr;
    for (auto& buf : geometryBufs_) {
        if (buf.source == &xyz && buf.primitive == primitive) {
            slot = &buf;
            break;
        }
    }
    if (!slot) {
        slot = &*std::min_element(geometryBufs_.begin(), geometryBufs_.end(),
                                 [](const GeometryBuf& a, const GeometryBuf& b) {
                                     return a.lastUse < b.lastUse;
                                 });
    }
    const uint64_t generation = camFrameActive_ ? camGeneration_ : 0;
    bool changed = slot->source != &xyz || slot->primitive != primitive ||
                   slot->generation != generation;
    if (camFrameActive_) {
        changed = changed || slot->content.data != xyz.data() || slot->content.size != xyz.size();
        slot->content.data = xyz.data();
        slot->content.size = xyz.size();
        slot->content.snapshot = {};
    } else {
        const bool unchanged = updateSourceStamp(slot->content, xyz);
        changed = changed || !unchanged;
    }
    if (!slot->vao) glGenVertexArrays(1, &slot->vao);
    if (!slot->vbo) glGenBuffers(1, &slot->vbo);
    if (changed) {
        glBindVertexArray(slot->vao);
        glBindBuffer(GL_ARRAY_BUFFER, slot->vbo);
        glBufferData(GL_ARRAY_BUFFER, xyz.size() * sizeof(float), xyz.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindVertexArray(0);
        slot->source = &xyz;
        slot->primitive = primitive;
        slot->generation = generation;
    }
    if (++geometryUse_ == 0) {
        geometryUse_ = 1;
        for (auto& buf : geometryBufs_) buf.lastUse = 0;
    }
    slot->lastUse = geometryUse_;
    return *slot;
}

void Overlays::clearGeometryBuffers() {
    for (auto& buf : geometryBufs_) {
        if (buf.vao) glDeleteVertexArrays(1, &buf.vao);
        if (buf.vbo) glDeleteBuffers(1, &buf.vbo);
        buf = GeometryBuf{};
    }
    geometryUse_ = 0;
    camGeneration_ = 0;
    camFrameActive_ = false;
}

void Overlays::beginCamFrame(uint64_t generation) {
    camGeneration_ = generation;
    camFrameActive_ = generation != 0;
}

void Overlays::endCamFrame() {
    camFrameActive_ = false;
}

void Overlays::beginSimulationFrame(uint64_t generation, uint64_t documentScope) {
    // Simulation scenes are valid before setDoc has ever been called (empty new part).
    ensureCommonProgram();
    if (generation != simulationGeneration_ || documentScope != simulationDocumentScope_) {
        clearSimulationBuffers();
        simulationGeneration_ = generation;
        simulationDocumentScope_ = documentScope;
    }
    simulationFrameActive_ = true;
    ++simulationFrame_;
    if (simulationFrame_ == 0) {
        // Keep zero as the never-used marker if the application survives a uint64 wrap.
        simulationFrame_ = 1;
        for (auto& kv : simulationBufs_) kv.second.lastFrame = 0;
    }
}

Overlays::SimulationBuf* Overlays::prepareSimulationBuffer(
    uint64_t batchKey, unsigned int primitive, const std::vector<float>& xyz,
    uint64_t sourceRevision) {
    if (!simulationFrameActive_ || xyz.empty() || !prog_) return nullptr;

    SimulationBuf& buf = simulationBufs_[batchKey];
    const bool contentChanged = sourceRevision == 0 && !updateSourceStamp(buf.positions, xyz);
    const int vertCount = (int)(xyz.size() / 3);
    const bool needsUpload = !buf.vao || !buf.vbo || buf.lit || buf.primitive != primitive ||
                             buf.vertCount != vertCount ||
                             buf.sourceRevision != sourceRevision || contentChanged;
    if (!buf.vao) glGenVertexArrays(1, &buf.vao);
    if (!buf.vbo) glGenBuffers(1, &buf.vbo);
    if (needsUpload) {
        glBindVertexArray(buf.vao);
        glBindBuffer(GL_ARRAY_BUFFER, buf.vbo);
        glBufferData(GL_ARRAY_BUFFER, xyz.size() * sizeof(float), xyz.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindVertexArray(0);
        buf.primitive = primitive;
        buf.vertCount = vertCount;
        buf.sourceRevision = sourceRevision;
        buf.lit = false;
    }
    buf.lastFrame = simulationFrame_;
    return &buf;
}

void Overlays::drawSimulationSegments(const Renderer::CameraXform& cam, uint64_t batchKey,
                                      const std::vector<float>& xyz, const float rgba[4],
                                      float lineWidth, bool overSurface,
                                      uint64_t sourceRevision) {
    if (xyz.size() < 6) return;
    SimulationBuf* buf =
        prepareSimulationBuffer(batchKey, GL_LINES, xyz, sourceRevision);
    if (!buf) return;

    bindCommon(cam);
    glUniform4fv(uColor_, 1, rgba);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    if (overSurface) glDepthRange(0.0, 0.999);
    glBindVertexArray(buf->vao);
    glLineWidth(lineWidth * widthScale_);
    glDrawArrays(GL_LINES, 0, buf->vertCount);
    if (overSurface) glDepthRange(0.0, 1.0);
    glBindVertexArray(0);
}

void Overlays::drawSimulationTriangles(const Renderer::CameraXform& cam, uint64_t batchKey,
                                       const std::vector<float>& xyz, const float rgba[4],
                                       bool overSurface, uint64_t sourceRevision) {
    if (xyz.size() < 9) return;
    SimulationBuf* buf =
        prepareSimulationBuffer(batchKey, GL_TRIANGLES, xyz, sourceRevision);
    if (!buf) return;

    bindCommon(cam);
    glUniform4fv(uColor_, 1, rgba);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    if (overSurface) glDepthRange(0.0, 0.999);
    glBindVertexArray(buf->vao);
    glDrawArrays(GL_TRIANGLES, 0, buf->vertCount);
    if (overSurface) glDepthRange(0.0, 1.0);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

void Overlays::drawSimulationLitTriangles(
    const Renderer::CameraXform& cam, uint64_t batchKey, const std::vector<float>& xyz,
    const std::vector<float>& nrm, const std::vector<float>* rgb, const float rgba[4],
    uint64_t sourceRevision) {
    if (!simulationFrameActive_ || xyz.size() < 9 || xyz.size() % 9 != 0 ||
        nrm.size() != xyz.size() || (rgb && rgb->size() != xyz.size())) return;
    SimulationBuf& buf = simulationBufs_[batchKey];
    bool contentChanged = false;
    if (sourceRevision == 0) {
        const bool positionsMatch = updateSourceStamp(buf.positions, xyz);
        const bool normalsMatch = updateSourceStamp(buf.normals, nrm);
        const bool colorsMatch = !rgb || updateSourceStamp(buf.colors, *rgb);
        contentChanged = !positionsMatch || !normalsMatch || !colorsMatch;
    }
    const int vertCount = (int)(xyz.size() / 3);
    const bool hasColor = rgb != nullptr;
    const bool needsUpload = !buf.vao || !buf.vbo || !buf.nrmVbo || !buf.lit ||
                             buf.vertCount != vertCount || buf.hasColor != hasColor ||
                             buf.sourceRevision != sourceRevision || contentChanged;
    if (!buf.vao) glGenVertexArrays(1, &buf.vao);
    if (!buf.vbo) glGenBuffers(1, &buf.vbo);
    if (!buf.nrmVbo) glGenBuffers(1, &buf.nrmVbo);
    if (hasColor && !buf.rgbVbo) glGenBuffers(1, &buf.rgbVbo);
    if (needsUpload) {
        glBindVertexArray(buf.vao);
        auto upload = [](unsigned int vbo, unsigned int attrib,
                         const std::vector<float>& values) {
            glBindBuffer(GL_ARRAY_BUFFER, vbo);
            glBufferData(GL_ARRAY_BUFFER, values.size() * sizeof(float), values.data(),
                         GL_STATIC_DRAW);
            glEnableVertexAttribArray(attrib);
            glVertexAttribPointer(attrib, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        };
        upload(buf.vbo, 0, xyz);
        upload(buf.nrmVbo, 1, nrm);
        if (hasColor) upload(buf.rgbVbo, 2, *rgb);
        else glDisableVertexAttribArray(2);
        buf.primitive = GL_TRIANGLES;
        buf.lit = true;
        buf.hasColor = hasColor;
        buf.vertCount = vertCount;
        buf.sourceRevision = sourceRevision;
    }
    buf.lastFrame = simulationFrame_;
    glUseProgram(litProg_);
    glUniformMatrix4fv(uLitMVP_, 1, GL_FALSE, cam.mvp.m);
    glUniform4fv(uLitColor_, 1, rgba);
    glUniform1i(uLitHasColor_, hasColor ? 1 : 0);
    glUniform3f(uLitViewDir_, cam.dir.x, cam.dir.y, cam.dir.z);
    glUniform1i(uLitClipCount_, clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(uLitClipN_, clipCount_, clipNormals_[0]);
        glUniform1fv(uLitClipOff_, clipCount_, clipOffsets_);
    }
    // Renderer::beginOverlay establishes two-sided drawing for all overlay batches.
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    if (rgba[3] >= 1.0f) {
        glDisable(GL_BLEND);
        glDepthMask(GL_TRUE);
    } else {
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glDepthMask(GL_FALSE);
    }
    glBindVertexArray(buf.vao);
    glDrawArrays(GL_TRIANGLES, 0, buf.vertCount);
    glBindVertexArray(0);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
}

void Overlays::endSimulationFrame() {
    if (!simulationFrameActive_) return;
    for (auto it = simulationBufs_.begin(); it != simulationBufs_.end();) {
        if (it->second.lastFrame == simulationFrame_) {
            ++it;
            continue;
        }
        if (it->second.vao) glDeleteVertexArrays(1, &it->second.vao);
        if (it->second.vbo) glDeleteBuffers(1, &it->second.vbo);
        if (it->second.nrmVbo) glDeleteBuffers(1, &it->second.nrmVbo);
        if (it->second.rgbVbo) glDeleteBuffers(1, &it->second.rgbVbo);
        it = simulationBufs_.erase(it);
    }
    simulationFrameActive_ = false;
}

void Overlays::clearSimulationBuffers() {
    for (auto& kv : simulationBufs_) {
        if (kv.second.vao) glDeleteVertexArrays(1, &kv.second.vao);
        if (kv.second.vbo) glDeleteBuffers(1, &kv.second.vbo);
        if (kv.second.nrmVbo) glDeleteBuffers(1, &kv.second.nrmVbo);
        if (kv.second.rgbVbo) glDeleteBuffers(1, &kv.second.rgbVbo);
    }
    simulationBufs_.clear();
    simulationGeneration_ = 0;
    simulationDocumentScope_ = 0;
    simulationFrame_ = 0;
    simulationFrameActive_ = false;
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

    GeometryBuf& buf = prepareGeometryBuffer(xyz, GL_LINES);
    bindCommon(cam);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDepthMask(GL_FALSE);
    glBindVertexArray(buf.vao);

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
    GeometryBuf& buf = prepareGeometryBuffer(xyz, GL_TRIANGLES);
    bindCommon(cam);
    glUniform4fv(uColor_, 1, rgba);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    // Renderer::beginOverlay establishes two-sided drawing for glyph cones and quads.
    if (overSurface) glDepthRange(0.0, 0.999);
    glBindVertexArray(buf.vao);
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
    // Selection washes are visual only: a translucent mate reference must not write depth
    // and hide the hover highlight of a component that can be picked through it.
    glDepthMask(GL_FALSE);

    // Selection first (persistent), hover on top (transient). A hover over an UNselected item shows
    // the lighter hover wash; a hover over an already-SELECTED item shows the darker selected+hover
    // wash so "what's selected" stays readable. Applies uniformly to faces, edges and vertices.
    for (size_t i = 0; i < sel.size();) {
        if (sel[i].kind != PickKind::Face) {
            drawHit(cam, sel[i++], selCol_);
            continue;
        }
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(0.0f, -1.0f);
        glUniform4fv(uColor_, 1, selCol_);
        size_t body = 0;
        do {
            const PickHit& h = sel[i++];
            if (const FaceBuf* face = findFace(h.body_id, h.topo_id)) {
                if (!faceBatchCounts_.empty() && body != face->body) flushFaceRanges(body);
                body = face->body;
                appendFaceRange(*face);
            }
        } while (i < sel.size() && sel[i].kind == PickKind::Face);
        flushFaceRanges(body);
        glDisable(GL_POLYGON_OFFSET_FILL);
        glDisable(GL_BLEND);
    }
    if (hover.valid()) {
        bool hoverSelected = false;
        for (const auto& h : sel) { if (h == hover) { hoverSelected = true; break; } }
        drawHit(cam, hover, hoverSelected ? selHoverCol_ : hoverCol_);
    }
    glDepthMask(GL_TRUE);

    glBindVertexArray(0);
}

void Overlays::beginScreenClip(const Renderer::CameraXform& cam, float u0, float v0, float u1,
                               float v1) {
    if (screenClipActive_ || cam.ssWidth <= 0 || cam.ssHeight <= 0) return;
    u0 = std::clamp(u0, 0.0f, 1.0f); u1 = std::clamp(u1, u0, 1.0f);
    v0 = std::clamp(v0, 0.0f, 1.0f); v1 = std::clamp(v1, v0, 1.0f);
    // GL scissor origin is bottom-left; the image-space v runs down.
    const int x0 = (int)std::floor(u0 * (float)cam.ssWidth);
    const int x1 = (int)std::ceil(u1 * (float)cam.ssWidth);
    const int y0 = (int)std::floor((1.0f - v1) * (float)cam.ssHeight);
    const int y1 = (int)std::ceil((1.0f - v0) * (float)cam.ssHeight);
    glEnable(GL_SCISSOR_TEST);
    glScissor(x0, y0, std::max(0, x1 - x0), std::max(0, y1 - y0));
    screenClipActive_ = true;
}

void Overlays::endScreenClip() {
    if (!screenClipActive_) return;
    glDisable(GL_SCISSOR_TEST);
    screenClipActive_ = false;
}

}  // namespace render
