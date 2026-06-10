#include "renderer.h"

#include <glad/gl.h>

#include <cmath>
#include <cstdio>

#include "math.h"

namespace {
const char* kVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
uniform mat4 uMVP;
out vec3 vN;
out vec3 vPos;
void main(){
    vN = aNormal;
    vPos = aPos;
    gl_Position = uMVP * vec4(aPos, 1.0);
})";

// Studio three-point lighting: warm key, cool fill, rim from behind, plus a hemisphere
// ambient (sky above, ground bounce below). Shading is done in linear space on the
// linearized part color and gamma-encoded on output, so viewport colors track the
// engine's contact-sheet palette.
const char* kFrag = R"(#version 330 core
in vec3 vN;
in vec3 vPos;
uniform vec3 uKeyDir;     // toward the key light (world)
uniform vec3 uFillDir;    // toward the fill light
uniform vec3 uRimDir;     // toward the rim light
uniform vec3 uCamPos;
uniform vec3 uColor;      // sRGB part color
out vec4 frag;
void main(){
    vec3 N = normalize(vN);
    vec3 V = normalize(uCamPos - vPos);
    if (dot(N, V) < 0.0) N = -N;            // two-sided
    vec3 base = pow(uColor, vec3(2.2));     // sRGB -> linear

    const vec3 keyCol  = vec3(1.00, 0.97, 0.92);
    const vec3 fillCol = vec3(0.62, 0.70, 0.86);
    const vec3 rimCol  = vec3(0.85, 0.90, 1.00);
    const vec3 skyCol  = vec3(0.32, 0.36, 0.43);
    const vec3 gndCol  = vec3(0.13, 0.12, 0.11);

    float key  = max(dot(N, normalize(uKeyDir)),  0.0);
    float fill = max(dot(N, normalize(uFillDir)), 0.0);
    float rim  = max(dot(N, normalize(uRimDir)),  0.0);
    float hemi = N.z * 0.5 + 0.5;            // up-facing picks up sky, down-facing ground

    vec3 diffuse = keyCol * (key * 0.85) + fillCol * (fill * 0.30);
    vec3 ambient = mix(gndCol, skyCol, hemi) * 0.55;
    float edge = pow(1.0 - max(dot(N, V), 0.0), 2.0);
    vec3 rimLight = rimCol * (rim * edge * 0.45);

    vec3 H = normalize(normalize(uKeyDir) + V);
    float spec = pow(max(dot(N, H), 0.0), 48.0) * 0.30;
    vec3 Hf = normalize(normalize(uFillDir) + V);
    spec += pow(max(dot(N, Hf), 0.0), 24.0) * 0.06;

    vec3 c = base * (diffuse + ambient) + rimLight + keyCol * spec;
    c = pow(max(c, 0.0), vec3(1.0 / 2.2));  // linear -> sRGB
    frag = vec4(c, 1.0);
})";

const char* kBgVert = R"(#version 330 core
out float vT;
void main(){
    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    vec2 ndc = pos * 2.0 - 1.0;
    vT = ndc.y;
    gl_Position = vec4(ndc, 0.999, 1.0);
})";

const char* kBgFrag = R"(#version 330 core
in float vT;
uniform vec3 uTop;
uniform vec3 uBot;
out vec4 frag;
void main(){
    float t = clamp(vT * 0.5 + 0.5, 0.0, 1.0);
    frag = vec4(mix(uBot, uTop, t), 1.0);
})";

const char* kEdgeVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
void main(){ gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kEdgeFrag = R"(#version 330 core
uniform vec3 uColor;
out vec4 frag;
void main(){ frag = vec4(uColor, 1.0); })";

unsigned int compile(unsigned int type, const char* src) {
    unsigned int s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    int ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024];
        glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        std::fprintf(stderr, "shader compile error: %s\n", log);
    }
    return s;
}

unsigned int linkProgram(const char* vsrc, const char* fsrc) {
    unsigned int vs = compile(GL_VERTEX_SHADER, vsrc);
    unsigned int fs = compile(GL_FRAGMENT_SHADER, fsrc);
    unsigned int p = glCreateProgram();
    glAttachShader(p, vs);
    glAttachShader(p, fs);
    glLinkProgram(p);
    glDeleteShader(vs);
    glDeleteShader(fs);
    return p;
}

void eyeForView(const std::string& v, float& yaw, float& pitch) {
    // yaw around +Z (up), pitch above the horizon
    if (v == "front") { yaw = 0.0f;   pitch = 0.0f; }
    else if (v == "right") { yaw = 90.0f;  pitch = 0.0f; }
    else if (v == "top")   { yaw = 0.0f;   pitch = 89.5f; }
    else { yaw = 35.0f; pitch = 30.0f; }  // iso
}
}  // namespace

Renderer::~Renderer() {
    freeSubs();
    if (fbo_) glDeleteFramebuffers(1, &fbo_);
    if (colorTex_) glDeleteTextures(1, &colorTex_);
    if (depthRbo_) glDeleteRenderbuffers(1, &depthRbo_);
    if (prog_) glDeleteProgram(prog_);
    if (edgeProg_) glDeleteProgram(edgeProg_);
    if (bgProg_) glDeleteProgram(bgProg_);
}

void Renderer::setBackground(float tr, float tg, float tb, float br, float bg, float bb) {
    bgTop_[0] = tr; bgTop_[1] = tg; bgTop_[2] = tb;
    bgBot_[0] = br; bgBot_[1] = bg; bgBot_[2] = bb;
}

bool Renderer::init(int width, int height, std::string& err) {
    w_ = width; h_ = height;
    sw_ = w_ * SS; sh_ = h_ * SS;

    prog_ = linkProgram(kVert, kFrag);
    edgeProg_ = linkProgram(kEdgeVert, kEdgeFrag);
    bgProg_ = linkProgram(kBgVert, kBgFrag);
    int ok = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &ok);
    if (!ok) { err = "program link failed"; return false; }

    glGenVertexArrays(1, &bgVao_);

    allocTargets();
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        err = "framebuffer incomplete";
        return false;
    }
    glEnable(GL_DEPTH_TEST);
    return true;
}

void Renderer::allocTargets() {
    if (!fbo_) glGenFramebuffers(1, &fbo_);
    if (!colorTex_) glGenTextures(1, &colorTex_);
    if (!depthRbo_) glGenRenderbuffers(1, &depthRbo_);

    glBindTexture(GL_TEXTURE_2D, colorTex_);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, sw_, sh_, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);

    glBindRenderbuffer(GL_RENDERBUFFER, depthRbo_);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, sw_, sh_);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, colorTex_, 0);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, depthRbo_);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void Renderer::resize(int width, int height) {
    if (width == w_ && height == h_) return;
    w_ = width; h_ = height;
    sw_ = w_ * SS; sh_ = h_ * SS;
    allocTargets();
}

void Renderer::freeSubs() {
    for (auto& s : subs_) {
        if (s.vao) glDeleteVertexArrays(1, &s.vao);
        if (s.vboPos) glDeleteBuffers(1, &s.vboPos);
        if (s.vboNrm) glDeleteBuffers(1, &s.vboNrm);
        if (s.ebo) glDeleteBuffers(1, &s.ebo);
        if (s.edgeVao) glDeleteVertexArrays(1, &s.edgeVao);
        if (s.vboEdge) glDeleteBuffers(1, &s.vboEdge);
    }
    subs_.clear();
}

void Renderer::setMesh(const Mesh& m) {
    PartMesh p;
    p.mesh = m;
    setMeshes({p});
}

void Renderer::setMeshes(const std::vector<PartMesh>& parts) {
    freeSubs();
    Vec3 bbMin, bbMax;
    bool first = true;
    for (const auto& part : parts) {
        const Mesh& m = part.mesh;
        if (m.empty()) continue;
        Sub s;
        s.color[0] = part.color[0];
        s.color[1] = part.color[1];
        s.color[2] = part.color[2];

        glGenVertexArrays(1, &s.vao);
        glGenBuffers(1, &s.vboPos);
        glGenBuffers(1, &s.vboNrm);
        glGenBuffers(1, &s.ebo);
        glBindVertexArray(s.vao);
        glBindBuffer(GL_ARRAY_BUFFER, s.vboPos);
        glBufferData(GL_ARRAY_BUFFER, m.positions.size() * sizeof(float), m.positions.data(),
                     GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindBuffer(GL_ARRAY_BUFFER, s.vboNrm);
        glBufferData(GL_ARRAY_BUFFER, m.normals.size() * sizeof(float), m.normals.data(),
                     GL_STATIC_DRAW);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s.ebo);
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, m.indices.size() * sizeof(unsigned int),
                     m.indices.data(), GL_STATIC_DRAW);
        glBindVertexArray(0);

        // feature edges (line list)
        glGenVertexArrays(1, &s.edgeVao);
        glGenBuffers(1, &s.vboEdge);
        glBindVertexArray(s.edgeVao);
        glBindBuffer(GL_ARRAY_BUFFER, s.vboEdge);
        glBufferData(GL_ARRAY_BUFFER, m.edgeLines.size() * sizeof(float), m.edgeLines.data(),
                     GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindVertexArray(0);

        s.idxCount = static_cast<int>(m.indices.size());
        s.edgeVerts = static_cast<int>(m.edgeLines.size() / 3);
        subs_.push_back(s);

        if (first) { bbMin = m.bbMin; bbMax = m.bbMax; first = false; }
        else {
            bbMin.x = std::fmin(bbMin.x, m.bbMin.x);
            bbMin.y = std::fmin(bbMin.y, m.bbMin.y);
            bbMin.z = std::fmin(bbMin.z, m.bbMin.z);
            bbMax.x = std::fmax(bbMax.x, m.bbMax.x);
            bbMax.y = std::fmax(bbMax.y, m.bbMax.y);
            bbMax.z = std::fmax(bbMax.z, m.bbMax.z);
        }
    }
    if (!first) {
        center_ = Vec3((bbMin.x + bbMax.x) * 0.5f, (bbMin.y + bbMax.y) * 0.5f,
                       (bbMin.z + bbMax.z) * 0.5f);
        Vec3 half(bbMax.x - center_.x, bbMax.y - center_.y, bbMax.z - center_.z);
        radius_ = std::sqrt(half.x * half.x + half.y * half.y + half.z * half.z);
        if (radius_ < 1e-6f) radius_ = 1.0f;
    }
}

void Renderer::drawCurrent(float yawDeg, float pitchDeg, float zoom) {
    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glViewport(0, 0, sw_, sh_);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

    // gradient studio background (fullscreen triangle, depth-independent)
    glDisable(GL_DEPTH_TEST);
    glUseProgram(bgProg_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uTop"), 1, bgTop_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uBot"), 1, bgBot_);
    glBindVertexArray(bgVao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindVertexArray(0);
    glEnable(GL_DEPTH_TEST);

    if (subs_.empty()) { glBindFramebuffer(GL_FRAMEBUFFER, 0); return; }

    const float deg = 3.14159265f / 180.0f;
    float yaw = yawDeg * deg, pitch = pitchDeg * deg;
    Vec3 dir(std::cos(pitch) * std::sin(yaw), -std::cos(pitch) * std::cos(yaw), std::sin(pitch));
    float fov = 35.0f * deg;
    float dist = radius_ / std::sin(fov * 0.5f) * 1.05f / std::max(0.05f, zoom);
    Vec3 eye = center_ + dir * dist;
    Vec3 up(0, 0, 1);
    if (std::fabs(pitch) > 89.0f * deg) up = Vec3(0, 1, 0);  // avoid gimbal at top view

    Mat4 proj = perspective(fov, (float)sw_ / (float)sh_, dist * 0.01f, dist * 10.0f);
    Mat4 view = lookAt(eye, center_, up);
    Mat4 mvp = mul(proj, view);

    // Publish the transform so the ID-buffer pass and overlays (C15) align to this exact frame.
    cam_.mvp = mvp;
    cam_.eye = eye;
    cam_.center = center_;
    cam_.radius = radius_;
    cam_.ssWidth = sw_;
    cam_.ssHeight = sh_;

    // Camera-relative studio rig: key over the left shoulder, fill from the right,
    // rim from behind-above so silhouettes separate from the dark background.
    Vec3 V = normalize(dir);                       // model -> camera
    Vec3 R = normalize(cross(up, V));
    Vec3 U = normalize(cross(V, R));
    Vec3 keyDir = normalize(V + R * -0.55f + U * 0.80f);
    Vec3 fillDir = normalize(V + R * 0.85f + U * 0.10f);
    Vec3 rimDir = normalize(V * -1.0f + U * 0.55f);

    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3f(glGetUniformLocation(prog_, "uKeyDir"), keyDir.x, keyDir.y, keyDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uFillDir"), fillDir.x, fillDir.y, fillDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uRimDir"), rimDir.x, rimDir.y, rimDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uCamPos"), eye.x, eye.y, eye.z);
    int colorLoc = glGetUniformLocation(prog_, "uColor");

    // Solids, pushed back a touch so the feature edges win the depth test on top of them.
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(1.0f, 1.0f);
    for (const auto& s : subs_) {
        glUniform3f(colorLoc, s.color[0], s.color[1], s.color[2]);
        glBindVertexArray(s.vao);
        glDrawElements(GL_TRIANGLES, s.idxCount, GL_UNSIGNED_INT, 0);
    }
    glBindVertexArray(0);
    glDisable(GL_POLYGON_OFFSET_FILL);

    glUseProgram(edgeProg_);
    glUniformMatrix4fv(glGetUniformLocation(edgeProg_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3f(glGetUniformLocation(edgeProg_, "uColor"), 0.11f, 0.16f, 0.21f);
    glLineWidth(1.5f);
    for (const auto& s : subs_) {
        if (s.edgeVerts <= 0) continue;
        glBindVertexArray(s.edgeVao);
        glDrawArrays(GL_LINES, 0, s.edgeVerts);
    }
    glBindVertexArray(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void Renderer::beginOverlay() {
    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glViewport(0, 0, sw_, sh_);
}

void Renderer::endOverlay() {
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void Renderer::renderView(const std::string& view) {
    float yaw, pitch;
    eyeForView(view, yaw, pitch);
    drawCurrent(yaw, pitch, 1.0f);
}

void Renderer::renderOrbit(float yawDeg, float pitchDeg, float zoom) {
    drawCurrent(yawDeg, pitchDeg, zoom);
}

std::vector<unsigned char> Renderer::readRGBA() const {
    std::vector<unsigned char> ss(sw_ * sh_ * 4);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, sw_, sh_, GL_RGBA, GL_UNSIGNED_BYTE, ss.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // Box-downsample SSxSS -> 1, and flip vertically (GL origin is bottom-left).
    std::vector<unsigned char> out(w_ * h_ * 4);
    for (int y = 0; y < h_; ++y) {
        for (int x = 0; x < w_; ++x) {
            int acc[4] = {0, 0, 0, 0};
            for (int dy = 0; dy < SS; ++dy)
                for (int dx = 0; dx < SS; ++dx) {
                    int sx = x * SS + dx, sy = y * SS + dy;
                    const unsigned char* p = &ss[(sy * sw_ + sx) * 4];
                    for (int c = 0; c < 4; ++c) acc[c] += p[c];
                }
            unsigned char* d = &out[((h_ - 1 - y) * w_ + x) * 4];
            for (int c = 0; c < 4; ++c) d[c] = (unsigned char)(acc[c] / (SS * SS));
        }
    }
    return out;
}
