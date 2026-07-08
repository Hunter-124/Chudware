// RE-28: 3D Gaussian Splat renderer — implementation. See splat_renderer.h for the design.
#include "splat_renderer.h"

#include <glad/gl.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <numeric>
#include <sstream>
#include <string>
#include <vector>

namespace render {

namespace {

// SH degree-0 basis constant: color = 0.5 + C0 * f_dc.
constexpr float kSH_C0 = 0.28209479177387814f;

float sigmoid(float x) { return 1.0f / (1.0f + std::exp(-x)); }
float clamp01(float x) { return x < 0.0f ? 0.0f : (x > 1.0f ? 1.0f : x); }

// A parsed .ply property: name + which primitive it is (only float/double supported here).
struct PlyProp {
    std::string name;
    std::string type;  // "float", "float32", "double", "float64", ...
};

// Map raw column values (already read in header order) into a Gaussian, applying activations.
void fillGaussian(Gaussian& g, const std::vector<PlyProp>& props, const std::vector<double>& vals) {
    auto get = [&](const char* n, double def) -> double {
        for (size_t i = 0; i < props.size() && i < vals.size(); ++i)
            if (props[i].name == n) return vals[i];
        return def;
    };
    g.pos = {(float)get("x", 0.0), (float)get("y", 0.0), (float)get("z", 0.0)};
    g.normal = {(float)get("nx", 0.0), (float)get("ny", 0.0), (float)get("nz", 1.0)};
    g.color[0] = clamp01(0.5f + kSH_C0 * (float)get("f_dc_0", 0.0));
    g.color[1] = clamp01(0.5f + kSH_C0 * (float)get("f_dc_1", 0.0));
    g.color[2] = clamp01(0.5f + kSH_C0 * (float)get("f_dc_2", 0.0));
    g.opacity = sigmoid((float)get("opacity", 4.0));  // sigmoid(4)~0.98 default -> opaque
    g.scale = {(float)std::exp(get("scale_0", 0.0)),
               (float)std::exp(get("scale_1", 0.0)),
               (float)std::exp(get("scale_2", 0.0))};
    float w = (float)get("rot_0", 1.0), x = (float)get("rot_1", 0.0);
    float y = (float)get("rot_2", 0.0), z = (float)get("rot_3", 0.0);
    float n = std::sqrt(w * w + x * x + y * y + z * z);
    if (n < 1e-8f) { w = 1.0f; x = y = z = 0.0f; n = 1.0f; }
    g.rot[0] = w / n; g.rot[1] = x / n; g.rot[2] = y / n; g.rot[3] = z / n;
}

}  // namespace

bool parseSplatPly(const char* data, size_t len, SplatCloud& out, std::string& err) {
    out.gaussians.clear();
    if (!data || len == 0) { err = "empty splat buffer"; return false; }
    std::istringstream stream(std::string(data, len));

    std::string line;
    if (!std::getline(stream, line) || line.rfind("ply", 0) != 0) {
        err = "not a .ply file (missing magic)";
        return false;
    }
    std::string format = "ascii";
    long vertexCount = -1;
    std::vector<PlyProp> props;
    bool inVertexElement = false;

    // --- header ---
    while (std::getline(stream, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::istringstream iss(line);
        std::string kw;
        iss >> kw;
        if (kw == "format") {
            iss >> format;  // ascii | binary_little_endian | binary_big_endian
        } else if (kw == "element") {
            std::string ename;
            long ecount = 0;
            iss >> ename >> ecount;
            inVertexElement = (ename == "vertex");
            if (inVertexElement) vertexCount = ecount;
        } else if (kw == "property" && inVertexElement) {
            PlyProp p;
            iss >> p.type >> p.name;
            props.push_back(p);
        } else if (kw == "end_header") {
            break;
        }
    }
    if (vertexCount < 0) { err = "no vertex element in .ply header"; return false; }
    if (props.empty()) { err = "no vertex properties in .ply header"; return false; }

    // Require the contract's mandatory columns.
    auto has = [&](const char* n) {
        for (const auto& p : props) if (p.name == n) return true;
        return false;
    };
    if (!has("x") || !has("y") || !has("z")) { err = "missing xyz columns"; return false; }
    if (!has("opacity")) { err = "missing opacity column"; return false; }

    out.gaussians.reserve((size_t)vertexCount);

    if (format == "ascii") {
        for (long v = 0; v < vertexCount; ++v) {
            if (!std::getline(stream, line)) { err = "truncated ascii vertex data"; return false; }
            if (!line.empty() && line.back() == '\r') line.pop_back();
            std::istringstream iss(line);
            std::vector<double> vals;
            vals.reserve(props.size());
            for (size_t i = 0; i < props.size(); ++i) {
                double d = 0.0;
                iss >> d;
                vals.push_back(d);
            }
            Gaussian g;
            fillGaussian(g, props, vals);
            out.gaussians.push_back(g);
        }
    } else if (format == "binary_little_endian") {
        // Byte width per property (we only read float/double; ints are still consumed by size).
        auto width = [](const std::string& t) -> size_t {
            if (t == "double" || t == "float64") return 8;
            if (t == "float" || t == "float32" || t == "int" || t == "int32" ||
                t == "uint" || t == "uint32") return 4;
            if (t == "short" || t == "ushort" || t == "int16" || t == "uint16") return 2;
            if (t == "char" || t == "uchar" || t == "int8" || t == "uint8") return 1;
            return 4;
        };
        // Remaining bytes after the header.
        std::string rest((std::istreambuf_iterator<char>(stream)),
                         std::istreambuf_iterator<char>());
        size_t off = 0;
        for (long v = 0; v < vertexCount; ++v) {
            std::vector<double> vals;
            vals.reserve(props.size());
            for (const auto& p : props) {
                size_t w = width(p.type);
                if (off + w > rest.size()) { err = "truncated binary vertex data"; return false; }
                double d = 0.0;
                if (p.type == "double" || p.type == "float64") {
                    double tmp; std::memcpy(&tmp, rest.data() + off, 8); d = tmp;
                } else {
                    float tmp = 0.0f; std::memcpy(&tmp, rest.data() + off, 4); d = tmp;
                }
                off += w;
                vals.push_back(d);
            }
            Gaussian g;
            fillGaussian(g, props, vals);
            out.gaussians.push_back(g);
        }
    } else {
        err = "unsupported .ply format: " + format;
        return false;
    }
    return true;
}

bool loadSplatPly(const std::string& path, SplatCloud& out, std::string& err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open " + path; return false; }
    std::string buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    return parseSplatPly(buf.data(), buf.size(), out, err);
}

std::vector<uint32_t> depthSortIndices(const SplatCloud& cloud, const Vec3& eye,
                                       const Vec3& viewDir) {
    std::vector<uint32_t> idx(cloud.gaussians.size());
    std::iota(idx.begin(), idx.end(), 0u);
    Vec3 vd = normalize(viewDir);
    // depth = signed distance along the view direction; back-to-front means farthest first.
    std::sort(idx.begin(), idx.end(), [&](uint32_t a, uint32_t b) {
        float da = dot(cloud.gaussians[a].pos - eye, vd);
        float db = dot(cloud.gaussians[b].pos - eye, vd);
        return da > db;  // larger depth (farther) drawn first
    });
    return idx;
}

// ---------------------------------------------------------------------------
// GL renderer
// ---------------------------------------------------------------------------

namespace {

const char* kVert = R"(#version 330 core
layout(location=0) in vec2 aCorner;      // unit quad corner in [-1,1]
layout(location=1) in vec3 iPos;         // instance center (world)
layout(location=2) in vec3 iColor;       // instance rgb
layout(location=3) in float iOpacity;    // instance alpha
layout(location=4) in vec2 iExtent;      // billboard half-extent (world units)
uniform mat4 uMVP;
uniform vec3 uCamRight;
uniform vec3 uCamUp;
out vec2 vUV;
out vec3 vColor;
out float vOpacity;
void main(){
    vUV = aCorner;
    vColor = iColor;
    vOpacity = iOpacity;
    vec3 world = iPos + uCamRight * (aCorner.x * iExtent.x)
                      + uCamUp    * (aCorner.y * iExtent.y);
    gl_Position = uMVP * vec4(world, 1.0);
})";

const char* kFrag = R"(#version 330 core
in vec2 vUV;
in vec3 vColor;
in float vOpacity;
out vec4 frag;
void main(){
    // 2D gaussian falloff over the billboard: exp(-r^2 / (2 sigma^2)), sigma chosen so the
    // quad edge (r=1) is ~3 sigma out and the splat fades smoothly to zero.
    float r2 = dot(vUV, vUV);
    float a = exp(-r2 * 4.5) * vOpacity;
    if (a < 0.004) discard;
    frag = vec4(vColor, a);
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
        std::fprintf(stderr, "splat shader compile error: %s\n", log);
    }
    return s;
}

}  // namespace

SplatRenderer::~SplatRenderer() { destroy(); }

void SplatRenderer::destroy() {
    if (quadVbo_) glDeleteBuffers(1, &quadVbo_);
    if (instVbo_) glDeleteBuffers(1, &instVbo_);
    if (orderVbo_) glDeleteBuffers(1, &orderVbo_);
    if (vao_) glDeleteVertexArrays(1, &vao_);
    if (prog_) glDeleteProgram(prog_);
    quadVbo_ = instVbo_ = orderVbo_ = vao_ = prog_ = 0;
    glReady_ = false;
}

bool SplatRenderer::load(const std::string& path, std::string& err) {
    SplatCloud c;
    if (!loadSplatPly(path, c, err)) return false;
    setCloud(std::move(c));
    return true;
}

void SplatRenderer::setCloud(SplatCloud cloud) {
    cloud_ = std::move(cloud);
    instCount_ = 0;               // force a re-upload of instance data on next draw
    lastEye_ = {1e30f, 1e30f, 1e30f};
    order_.clear();
}

bool SplatRenderer::ensureGL(std::string& err) {
    if (glReady_) return true;
    if (glFailed_) { err = "splat GL init previously failed"; return false; }

    unsigned int v = compile(GL_VERTEX_SHADER, kVert);
    unsigned int f = compile(GL_FRAGMENT_SHADER, kFrag);
    prog_ = glCreateProgram();
    glAttachShader(prog_, v);
    glAttachShader(prog_, f);
    glLinkProgram(prog_);
    glDeleteShader(v);
    glDeleteShader(f);
    int linked = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &linked);
    if (!linked) {
        char log[1024];
        glGetProgramInfoLog(prog_, sizeof(log), nullptr, log);
        std::fprintf(stderr, "splat program link error: %s\n", log);
        glDeleteProgram(prog_); prog_ = 0;
        glFailed_ = true; err = "splat program link failed";
        return false;
    }
    uMVP_ = glGetUniformLocation(prog_, "uMVP");
    uCamRight_ = glGetUniformLocation(prog_, "uCamRight");
    uCamUp_ = glGetUniformLocation(prog_, "uCamUp");

    glGenVertexArrays(1, &vao_);
    glBindVertexArray(vao_);

    // Static unit quad (two triangles) as a triangle strip in [-1,1].
    const float quad[8] = {-1, -1,  1, -1,  -1, 1,  1, 1};
    glGenBuffers(1, &quadVbo_);
    glBindBuffer(GL_ARRAY_BUFFER, quadVbo_);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 2 * sizeof(float), (void*)0);

    glGenBuffers(1, &instVbo_);
    glGenBuffers(1, &orderVbo_);
    glBindVertexArray(0);

    glReady_ = true;
    return true;
}

void SplatRenderer::uploadOrder(const std::vector<uint32_t>& order) {
    // Build an interleaved instance buffer in draw order (back-to-front). Layout per instance:
    // pos(3) color(3) opacity(1) extent(2) = 9 floats.
    std::vector<float> inst;
    inst.reserve(order.size() * 9);
    for (uint32_t gi : order) {
        const Gaussian& g = cloud_.gaussians[gi];
        // billboard half-extent: largest gaussian scale axis, scaled to ~3 sigma coverage.
        float sx = std::max(g.scale.x, std::max(g.scale.y, g.scale.z));
        float ex = sx * 3.0f;
        inst.insert(inst.end(), {g.pos.x, g.pos.y, g.pos.z,
                                 g.color[0], g.color[1], g.color[2],
                                 g.opacity, ex, ex});
    }
    glBindVertexArray(vao_);
    glBindBuffer(GL_ARRAY_BUFFER, instVbo_);
    glBufferData(GL_ARRAY_BUFFER, inst.size() * sizeof(float),
                 inst.empty() ? nullptr : inst.data(), GL_DYNAMIC_DRAW);
    const GLsizei stride = 9 * sizeof(float);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, stride, (void*)0);
    glVertexAttribDivisor(1, 1);
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 3, GL_FLOAT, GL_FALSE, stride, (void*)(3 * sizeof(float)));
    glVertexAttribDivisor(2, 1);
    glEnableVertexAttribArray(3);
    glVertexAttribPointer(3, 1, GL_FLOAT, GL_FALSE, stride, (void*)(6 * sizeof(float)));
    glVertexAttribDivisor(3, 1);
    glEnableVertexAttribArray(4);
    glVertexAttribPointer(4, 2, GL_FLOAT, GL_FALSE, stride, (void*)(7 * sizeof(float)));
    glVertexAttribDivisor(4, 1);
    glBindVertexArray(0);
    instCount_ = (int)order.size();
}

void SplatRenderer::draw(const Renderer::CameraXform& cam) {
    if (cloud_.empty()) return;  // null-safe: nothing loaded
    std::string err;
    if (!ensureGL(err)) return;

    // Recompute the depth sort when the camera eye moved appreciably (or first draw / new cloud).
    Vec3 viewDir = normalize(cam.center - cam.eye);
    float moved = length(cam.eye - lastEye_);
    if (moved > 1e-4f || instCount_ != (int)cloud_.gaussians.size()) {
        // NOTE: sort is pure/GL-free — in the live app this runs on a worker; here inline is fine.
        order_ = depthSortIndices(cloud_, cam.eye, viewDir);
        uploadOrder(order_);   // GL-thread upload of the sorted instance buffer
        lastEye_ = cam.eye;
    }
    if (instCount_ == 0) return;

    // Alpha pass: blend over opaque geometry, write no depth (splats are order-dependent).
    GLboolean depthMask = GL_TRUE;
    glGetBooleanv(GL_DEPTH_WRITEMASK, &depthMask);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_FALSE);

    glUseProgram(prog_);
    glUniformMatrix4fv(uMVP_, 1, GL_FALSE, cam.mvp.m);
    // Camera basis for billboarding: derive right/up from the view direction.
    Vec3 up0{0.0f, 0.0f, 1.0f};
    if (std::fabs(dot(viewDir, up0)) > 0.99f) up0 = Vec3{0.0f, 1.0f, 0.0f};
    Vec3 right = normalize(cross(viewDir, up0));
    Vec3 up = normalize(cross(right, viewDir));
    glUniform3f(uCamRight_, right.x, right.y, right.z);
    glUniform3f(uCamUp_, up.x, up.y, up.z);

    glBindVertexArray(vao_);
    glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, instCount_);
    glBindVertexArray(0);

    glDepthMask(depthMask);
    glDisable(GL_BLEND);
}

}  // namespace render
