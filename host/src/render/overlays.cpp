#include "overlays.h"

#include <glad/gl.h>

#include <cstdio>
#include <cstring>

namespace render {

namespace {

const char* kVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
out vec3 vWorld;
void main(){ vWorld = aPos; gl_Position = uMVP * vec4(aPos, 1.0); })";

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
    glShaderSource(s, 1, &src, nullptr);
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

void Overlays::setColors(const float hover[4], const float selection[4]) {
    for (int i = 0; i < 4; ++i) {
        hoverCol_[i] = hover[i];
        selCol_[i] = selection[i];
    }
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
    if (!prog_) prog_ = linkProgram(kVert, kFrag);
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
            faces_.push_back({Key{b.body_id, fr.face_id}, fb});
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
            edges_.push_back({Key{b.body_id, es.edge_id}, eb});
        }
        for (const auto& v : b.brep_verts) {
            verts_.push_back({Key{b.body_id, v.vertex_id},
                              std::array<float, 3>{v.xyz[0], v.xyz[1], v.xyz[2]}});
        }
    }
    return true;
}

const Overlays::FaceBuf* Overlays::findFace(uint32_t body, uint32_t face_id) const {
    for (const auto& kv : faces_)
        if (kv.first.body == body && kv.first.topo == face_id) return &kv.second;
    return nullptr;
}
const Overlays::EdgeBuf* Overlays::findEdge(uint32_t body, uint32_t edge_id) const {
    for (const auto& kv : edges_)
        if (kv.first.body == body && kv.first.topo == edge_id) return &kv.second;
    return nullptr;
}
bool Overlays::findVert(uint32_t body, uint32_t vert_id, float out[3]) const {
    for (const auto& kv : verts_)
        if (kv.first.body == body && kv.first.topo == vert_id) {
            out[0] = kv.second[0]; out[1] = kv.second[1]; out[2] = kv.second[2];
            return true;
        }
    return false;
}

void Overlays::drawHit(const Renderer::CameraXform& cam, const PickHit& h, const float rgba[4]) {
    int colLoc = glGetUniformLocation(prog_, "uColor");
    if (h.kind == PickKind::Face) {
        const FaceBuf* fb = findFace(h.body_id, h.topo_id);
        if (!fb) return;
        // Translucent wash on the face, biased slightly toward the camera so it reads on top.
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(-1.0f, -1.0f);
        glUniform4fv(colLoc, 1, rgba);
        glBindVertexArray(fb->vao);
        glDrawElements(GL_TRIANGLES, fb->idxCount, GL_UNSIGNED_INT, 0);
        glDisable(GL_POLYGON_OFFSET_FILL);
        glDisable(GL_BLEND);
    } else if (h.kind == PickKind::Edge) {
        const EdgeBuf* eb = findEdge(h.body_id, h.topo_id);
        if (!eb) return;
        float opaque[4] = {rgba[0], rgba[1], rgba[2], 1.0f};
        glUniform4fv(colLoc, 1, opaque);
        glDepthRange(0.0, 0.999);  // bias over the shaded surface
        glLineWidth(3.5f);
        glBindVertexArray(eb->vao);
        glDrawArrays(GL_LINES, 0, eb->vertCount);
        glDepthRange(0.0, 1.0);
    } else if (h.kind == PickKind::Vertex) {
        float xyz[3];
        if (!findVert(h.body_id, h.topo_id, xyz)) return;
        float opaque[4] = {rgba[0], rgba[1], rgba[2], 1.0f};
        glUniform4fv(colLoc, 1, opaque);
        glBindVertexArray(vao_);
        glBindBuffer(GL_ARRAY_BUFFER, vbo_);
        glBufferData(GL_ARRAY_BUFFER, 3 * sizeof(float), xyz, GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glEnable(GL_PROGRAM_POINT_SIZE);
        glPointSize(9.0f);
        glDepthRange(0.0, 0.997);
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
    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, cam.mvp.m);
    glUniform1i(glGetUniformLocation(prog_, "uClipCount"), clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(glGetUniformLocation(prog_, "uClipN"), clipCount_, clipNormals_[0]);
        glUniform1fv(glGetUniformLocation(prog_, "uClipOff"), clipCount_, clipOffsets_);
    }
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(-1.0f, -1.0f);
    int colLoc = glGetUniformLocation(prog_, "uColor");
    glUniform4fv(colLoc, 1, rgba);
    for (uint64_t key : faceKeys) {
        uint32_t body_id  = (uint32_t)(key >> 32);
        uint32_t face_id  = (uint32_t)(key & 0xFFFFFFFFu);
        const FaceBuf* fb = findFace(body_id, face_id);
        if (!fb) continue;  // stale key after rebuild — skip silently
        glBindVertexArray(fb->vao);
        glDrawElements(GL_TRIANGLES, fb->idxCount, GL_UNSIGNED_INT, 0);
    }
    glDisable(GL_POLYGON_OFFSET_FILL);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

void Overlays::drawSegments(const Renderer::CameraXform& cam, const std::vector<float>& xyz,
                            const float rgba[4], float lineWidth, bool overSurface) {
    if (xyz.size() < 6 || !prog_) return;
    if (!segVao_) { glGenVertexArrays(1, &segVao_); glGenBuffers(1, &segVbo_); }
    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, cam.mvp.m);
    glUniform1i(glGetUniformLocation(prog_, "uClipCount"), clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(glGetUniformLocation(prog_, "uClipN"), clipCount_, clipNormals_[0]);
        glUniform1fv(glGetUniformLocation(prog_, "uClipOff"), clipCount_, clipOffsets_);
    }
    glUniform4fv(glGetUniformLocation(prog_, "uColor"), 1, rgba);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    if (overSurface) glDepthRange(0.0, 0.999);  // bias over the shaded surface like edge highlights
    glBindVertexArray(segVao_);
    glBindBuffer(GL_ARRAY_BUFFER, segVbo_);
    glBufferData(GL_ARRAY_BUFFER, xyz.size() * sizeof(float), xyz.data(), GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    glLineWidth(lineWidth);
    glDrawArrays(GL_LINES, 0, (int)(xyz.size() / 3));
    if (overSurface) glDepthRange(0.0, 1.0);
    glBindVertexArray(0);
}

void Overlays::draw(const Renderer::CameraXform& cam, const PickHit& hover,
                    const std::vector<PickHit>& sel) {
    if (!prog_) return;
    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, cam.mvp.m);
    glUniform1i(glGetUniformLocation(prog_, "uClipCount"), clipCount_);
    if (clipCount_ > 0) {
        glUniform3fv(glGetUniformLocation(prog_, "uClipN"), clipCount_, clipNormals_[0]);
        glUniform1fv(glGetUniformLocation(prog_, "uClipOff"), clipCount_, clipOffsets_);
    }
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);

    // Selection first (persistent), hover on top (transient), so a hovered+selected item shows
    // the hover color.
    for (const auto& h : sel) drawHit(cam, h, selCol_);
    if (hover.valid()) drawHit(cam, hover, hoverCol_);

    glBindVertexArray(0);
}

}  // namespace render
