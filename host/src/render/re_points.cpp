// RE-21: Sparse point-cloud + camera-frusta overlay — implementation.
// See re_points.h for the API contract.
#include "re_points.h"

#include <glad/gl.h>
#include "ui/viewport/glsl_compat.h"


#include <cmath>
#include <cstdio>
#include <cstring>

namespace render {

// ---- shader (flat colour + per-vertex colour switch) ---------------------------

namespace {

const char* kReVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aColor;  // used when uUsePVC == 1
uniform mat4 uMVP;
uniform int  uUsePVC;
uniform vec4 uColor;
out vec4 vColor;
void main() {
    gl_Position = uMVP * vec4(aPos, 1.0);
    if (uUsePVC == 1)
        vColor = vec4(aColor, 1.0);
    else
        vColor = uColor;
})";

const char* kReFrag = R"(#version 330 core
in vec4 vColor;
out vec4 frag;
void main() { frag = vColor; })";

unsigned int compileShader(unsigned int type, const char* src) {
    unsigned int s = glCreateShader(type);
#ifdef BILLET_GLES3
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
        char log[1024]; glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        std::fprintf(stderr, "re_points shader error: %s\n", log);
    }
    return s;
}

unsigned int linkProgram(const char* vs, const char* fs) {
    unsigned int v = compileShader(GL_VERTEX_SHADER, vs);
    unsigned int f = compileShader(GL_FRAGMENT_SHADER, fs);
    unsigned int p = glCreateProgram();
    glAttachShader(p, v); glAttachShader(p, f);
    glLinkProgram(p);
    glDeleteShader(v); glDeleteShader(f);
    return p;
}

}  // namespace

// ---- lifecycle -----------------------------------------------------------------

RePoints::~RePoints() { destroyGL(); }

void RePoints::destroyGL() {
    if (ptVao_)    { glDeleteVertexArrays(1, &ptVao_);    ptVao_    = 0; }
    if (ptVbo_)    { glDeleteBuffers(1, &ptVbo_);         ptVbo_    = 0; }
    if (lineVao_)  { glDeleteVertexArrays(1, &lineVao_);  lineVao_  = 0; }
    if (lineVbo_)  { glDeleteBuffers(1, &lineVbo_);       lineVbo_  = 0; }
    if (prog_)     { glDeleteProgram(prog_);               prog_     = 0; }
}

void RePoints::ensureGL() {
    if (!prog_) {
        prog_   = linkProgram(kReVert, kReFrag);
        uMVP_   = glGetUniformLocation(prog_, "uMVP");
        uColor_ = glGetUniformLocation(prog_, "uColor");
        uUsePVC_= glGetUniformLocation(prog_, "uUsePVC");
    }

    if (!ptVao_)   { glGenVertexArrays(1, &ptVao_);   glGenBuffers(1, &ptVbo_);   }
    if (!lineVao_) { glGenVertexArrays(1, &lineVao_); glGenBuffers(1, &lineVbo_); }

    if (!dirty_) return;
    dirty_ = false;

    // Upload point cloud — stride 6 floats: [x, y, z, r, g, b].
    ptVerts_.clear();
    ptVerts_.reserve(pts_.size() * 6);
    for (const auto& p : pts_) {
        ptVerts_.push_back(p.x);
        ptVerts_.push_back(p.y);
        ptVerts_.push_back(p.z);
        ptVerts_.push_back(p.r / 255.0f);
        ptVerts_.push_back(p.g / 255.0f);
        ptVerts_.push_back(p.b / 255.0f);
    }
    glBindVertexArray(ptVao_);
    glBindBuffer(GL_ARRAY_BUFFER, ptVbo_);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(ptVerts_.size() * sizeof(float)),
                 ptVerts_.empty() ? nullptr : ptVerts_.data(), GL_DYNAMIC_DRAW);
    if (!ptVerts_.empty()) {
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 6 * sizeof(float), (void*)0);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 6 * sizeof(float),
                              (void*)(3 * sizeof(float)));
    }
    glBindVertexArray(0);

    // Upload frustum line segments.
    // Each frustum → 8 edges: 4 rays from centre to corners + 4 edges around near-plane rect.
    frustaLines_.clear();
    frustaLines_.reserve(frusta_.size() * 8 * 6);
    auto push3 = [&](float x, float y, float z) {
        frustaLines_.push_back(x); frustaLines_.push_back(y); frustaLines_.push_back(z);
    };
    for (const auto& f : frusta_) {
        // Rays from centre to each corner.
        push3(f.cx,  f.cy,  f.cz);  push3(f.c0x, f.c0y, f.c0z);
        push3(f.cx,  f.cy,  f.cz);  push3(f.c1x, f.c1y, f.c1z);
        push3(f.cx,  f.cy,  f.cz);  push3(f.c2x, f.c2y, f.c2z);
        push3(f.cx,  f.cy,  f.cz);  push3(f.c3x, f.c3y, f.c3z);
        // Near-plane rectangle.
        push3(f.c0x, f.c0y, f.c0z); push3(f.c1x, f.c1y, f.c1z);
        push3(f.c1x, f.c1y, f.c1z); push3(f.c2x, f.c2y, f.c2z);
        push3(f.c2x, f.c2y, f.c2z); push3(f.c3x, f.c3y, f.c3z);
        push3(f.c3x, f.c3y, f.c3z); push3(f.c0x, f.c0y, f.c0z);
    }
    glBindVertexArray(lineVao_);
    glBindBuffer(GL_ARRAY_BUFFER, lineVbo_);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(frustaLines_.size() * sizeof(float)),
                 frustaLines_.empty() ? nullptr : frustaLines_.data(), GL_DYNAMIC_DRAW);
    if (!frustaLines_.empty()) {
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glDisableVertexAttribArray(1);
    }
    glBindVertexArray(0);
}

// ---- data setters --------------------------------------------------------------

void RePoints::setCloud(const std::vector<SparsePoint>& pts) {
    pts_   = pts;
    dirty_ = true;
}

void RePoints::setFrusta(const std::vector<CameraFrustum>& frusta) {
    frusta_ = frusta;
    dirty_  = true;
}

// ---- draw ----------------------------------------------------------------------

void RePoints::draw(const Renderer::CameraXform& cam) {
    if (pts_.empty() && frusta_.empty()) return;
    ensureGL();
    if (!prog_) return;

    glUseProgram(prog_);
    glUniformMatrix4fv(uMVP_, 1, GL_FALSE, cam.mvp.m);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LEQUAL);
    glDepthRange(0.0, 0.999);  // slight bias so points sit on top of shaded surface

    // Draw points with per-vertex colour.
    if (!ptVerts_.empty()) {
        glUniform1i(uUsePVC_, 1);
        glBindVertexArray(ptVao_);
#ifndef BILLET_GLES3
        glPointSize(pointSize_ * widthScale_);
#endif
        glDrawArrays(GL_POINTS, 0, (GLsizei)(ptVerts_.size() / 6));
        glBindVertexArray(0);
    }

    // Draw frustum outlines in yellow.
    if (!frustaLines_.empty()) {
        const float yellow[4] = {1.0f, 0.85f, 0.15f, 0.9f};
        glUniform1i(uUsePVC_, 0);
        glUniform4fv(uColor_, 1, yellow);
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glBindVertexArray(lineVao_);
        glLineWidth(lineWidth_ * widthScale_);
        glDrawArrays(GL_LINES, 0, (GLsizei)(frustaLines_.size() / 3));
        glBindVertexArray(0);
        glDisable(GL_BLEND);
    }

    glDepthRange(0.0, 1.0);
    glUseProgram(0);
}

}  // namespace render
// buildCannedSparseFixture lives in re_points_fixture.cpp (GL-free, linked by the ctest).
