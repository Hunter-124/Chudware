#include "id_pass.h"

#include <glad/gl.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace render {

namespace {

// R32UI path: integer color out, id passed as a uint uniform.
const char* kVertU = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
out vec3 vWorld;
void main(){ vWorld = aPos; gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kFragU = R"(#version 330 core
uniform uint uId;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
in vec3 vWorld;
out uint frag;
void main(){
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    frag = uId;
})";

// RGBA8 fallback: same vertex shader, pack the 32-bit id into RGBA bytes (little-endian-ish:
// r = low byte ... a = high byte). Readback reassembles. Only the low 24 bits carry topo data
// and 8 bits body, so all 32 bits are meaningful — we use all four channels.
const char* kFragRGBA = R"(#version 330 core
uniform uint uId;
uniform int uClipCount;
uniform vec3 uClipN[3];
uniform float uClipOff[3];
in vec3 vWorld;
out vec4 frag;
void main(){
    for (int i = 0; i < uClipCount; ++i)
        if (dot(vWorld, uClipN[i]) > uClipOff[i]) discard;
    uint id = uId;
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

IdPass::~IdPass() { destroy(); }

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
    for (auto& f : faces_) {
        if (f.vao) glDeleteVertexArrays(1, &f.vao);
        if (f.vboPos) glDeleteBuffers(1, &f.vboPos);
        if (f.ebo) glDeleteBuffers(1, &f.ebo);
    }
    faces_.clear();
    for (auto& e : edges_) {
        if (e.vao) glDeleteVertexArrays(1, &e.vao);
        if (e.vbo) glDeleteBuffers(1, &e.vbo);
    }
    edges_.clear();
    if (verts_.vao) glDeleteVertexArrays(1, &verts_.vao);
    if (verts_.vbo) glDeleteBuffers(1, &verts_.vbo);
    verts_ = VertDraw{};
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

    // ---- faces: one VAO/EBO per FACE range, drawing just that range's triangles ----
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        // shared position VBO per body (referenced by every face range of that body)
        for (const auto& fr : b.faces) {
            if (fr.tri_count == 0) continue;
            FaceDraw fd;
            fd.id = encodeId(b.body_id, PickKind::Face, fr.face_id);
            glGenVertexArrays(1, &fd.vao);
            glGenBuffers(1, &fd.vboPos);
            glGenBuffers(1, &fd.ebo);
            glBindVertexArray(fd.vao);
            glBindBuffer(GL_ARRAY_BUFFER, fd.vboPos);
            glBufferData(GL_ARRAY_BUFFER, b.pos.size() * sizeof(float), b.pos.data(),
                         GL_STATIC_DRAW);
            glEnableVertexAttribArray(0);
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
            // index subrange for this face
            const uint32_t* base = b.idx.data() + (size_t)fr.first_tri * 3;
            int count = (int)fr.tri_count * 3;
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, fd.ebo);
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, (size_t)count * sizeof(uint32_t), base,
                         GL_STATIC_DRAW);
            glBindVertexArray(0);
            fd.idxCount = count;
            faces_.push_back(fd);
        }

        // ---- edges: each EDGE segment -> line list (consecutive points) ----
        for (const auto& es : b.edges) {
            if (es.pt_count < 2) continue;
            std::vector<float> lines;
            lines.reserve((size_t)(es.pt_count - 1) * 6);
            for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
                const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
                const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
                lines.insert(lines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
            }
            EdgeDraw ed;
            ed.id = encodeId(b.body_id, PickKind::Edge, es.edge_id);
            glGenVertexArrays(1, &ed.vao);
            glGenBuffers(1, &ed.vbo);
            glBindVertexArray(ed.vao);
            glBindBuffer(GL_ARRAY_BUFFER, ed.vbo);
            glBufferData(GL_ARRAY_BUFFER, lines.size() * sizeof(float), lines.data(),
                         GL_STATIC_DRAW);
            glEnableVertexAttribArray(0);
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
            glBindVertexArray(0);
            ed.vertCount = (int)lines.size() / 3;
            edges_.push_back(ed);
        }
    }

    // ---- B-rep verts: all bodies in one VBO; ids drawn one point at a time ----
    {
        std::vector<float> pts;
        std::vector<uint32_t> ids;
        for (const auto& b : doc.bodies) {
            for (const auto& v : b.brep_verts) {
                pts.insert(pts.end(), {v.xyz[0], v.xyz[1], v.xyz[2]});
                ids.push_back(encodeId(b.body_id, PickKind::Vertex, v.vertex_id));
            }
        }
        if (!pts.empty()) {
            glGenVertexArrays(1, &verts_.vao);
            glGenBuffers(1, &verts_.vbo);
            glBindVertexArray(verts_.vao);
            glBindBuffer(GL_ARRAY_BUFFER, verts_.vbo);
            glBufferData(GL_ARRAY_BUFFER, pts.size() * sizeof(float), pts.data(), GL_STATIC_DRAW);
            glEnableVertexAttribArray(0);
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
            glBindVertexArray(0);
            verts_.count = (int)pts.size() / 3;
            verts_.ids = std::move(ids);
        }
    }

    built_ = true;
    return true;
}

void IdPass::render(const Renderer::CameraXform& cam) {
    if (!built_) return;
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
    int idLoc = glGetUniformLocation(prog_, "uId");

    // Faces first, pushed back so edges/verts win the depth test on coincident geometry.
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(1.0f, 1.0f);
    for (const auto& f : faces_) {
        if (bodyHidden(f.id)) continue;  // hidden body: unpickable
        glUniform1ui(idLoc, f.id);
        glBindVertexArray(f.vao);
        glDrawElements(GL_TRIANGLES, f.idxCount, GL_UNSIGNED_INT, 0);
    }
    glDisable(GL_POLYGON_OFFSET_FILL);

    // Edges over faces: a fat line so they're pickable, and a small negative depth-range bias so
    // an edge on a face surface reads as the edge.
    glDepthRange(0.0, 0.999);
    glLineWidth(3.0f);
    for (const auto& e : edges_) {
        if (bodyHidden(e.id)) continue;  // hidden body: unpickable
        glUniform1ui(idLoc, e.id);
        glBindVertexArray(e.vao);
        glDrawArrays(GL_LINES, 0, e.vertCount);
    }

    // Verts win over everything: largest depth bias, fat points, drawn individually so each
    // carries its own id.
    glDepthRange(0.0, 0.998);
    glEnable(GL_PROGRAM_POINT_SIZE);
#ifdef GL_POINT_SPRITE
    glEnable(GL_POINT_SPRITE);
#endif
    glPointSize(6.0f);
    if (verts_.count > 0) {
        glBindVertexArray(verts_.vao);
        for (int i = 0; i < verts_.count; ++i) {
            if (bodyHidden(verts_.ids[i])) continue;  // hidden body: unpickable
            glUniform1ui(idLoc, verts_.ids[i]);
            glDrawArrays(GL_POINTS, i, 1);
        }
    }

    glDepthRange(0.0, 1.0);
    glBindVertexArray(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
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

}  // namespace render
