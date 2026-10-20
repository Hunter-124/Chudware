// Real-context ID picking benchmark. Compile this unchanged against either id_pass.cpp/header
// pair; --hits produces a timing-free CSV for byte-for-byte A/B comparison. --require-memoized
// additionally enforces the optimized hover and failed-render invalidation contracts.
#include <glad/gl.h>
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <stb_image_write.h>

#include "render/id_pass.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
using render::PickHit;
using render::PickKind;

struct Options {
    int width = 320, height = 240, supersample = 3;
    int iterations = 10000, gridStep = 17, tiles = 12;
    std::string mode = "both", hits, png;
    bool requireMemoized = false;
};

int positiveInt(const char* text) {
    char* end = nullptr;
    long value = std::strtol(text, &end, 10);
    if (!*text || *end || value <= 0 || value > 100000000)
        throw std::runtime_error(std::string("invalid positive integer: ") + text);
    return (int)value;
}

Options options(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--help") {
            std::cout << "profile_viewport [--mode both|r32ui|rgba8] [--width 320] [--height 240]\n"
                         "  [--supersample 3] [--iterations 10000] [--grid-step 17] [--tiles 12]\n"
                         "  [--hits hits.csv] [--png output-prefix] [--require-memoized]\n"
                         "JSON goes to stdout. CSV contains every grid/boundary hit, no timings.\n"
                         "PNG captures the actual supersampled ID attachment outside timed batches.\n";
            std::exit(0);
        }
        if (arg == "--require-memoized") { o.requireMemoized = true; continue; }
        if (i + 1 >= argc) throw std::runtime_error("missing value for " + arg);
        const char* value = argv[++i];
        if (arg == "--mode") o.mode = value;
        else if (arg == "--width") o.width = positiveInt(value);
        else if (arg == "--height") o.height = positiveInt(value);
        else if (arg == "--supersample") o.supersample = positiveInt(value);
        else if (arg == "--iterations") o.iterations = positiveInt(value);
        else if (arg == "--grid-step") o.gridStep = positiveInt(value);
        else if (arg == "--tiles") o.tiles = positiveInt(value);
        else if (arg == "--hits") o.hits = value;
        else if (arg == "--png") o.png = value;
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (o.mode != "both" && o.mode != "r32ui" && o.mode != "rgba8")
        throw std::runtime_error("--mode must be both, r32ui, or rgba8");
    if (o.width < 32 || o.height < 32 || o.width > 4096 || o.height > 4096 ||
        o.supersample > 8 || o.tiles > 64)
        throw std::runtime_error("dimensions must be 32..4096, supersample 1..8, tiles 1..64");
    return o;
}

struct Context {
    GLFWwindow* window = nullptr;
    Context() {
        glfwSetErrorCallback([](int code, const char* message) {
            std::cerr << "GLFW " << code << ": " << message << '\n';
        });
        if (!glfwInit()) throw std::runtime_error("glfwInit failed (use a display or xvfb-run)");
        glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE);
        glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
        glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
        glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
#ifdef __APPLE__
        glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GLFW_TRUE);
#endif
        window = glfwCreateWindow(64, 64, "Chudware picking profiler", nullptr, nullptr);
        if (!window) {
            glfwTerminate();
            throw std::runtime_error("hidden OpenGL 3.3 context creation failed");
        }
        glfwMakeContextCurrent(window);
        if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) {
            glfwDestroyWindow(window);
            glfwTerminate();
            throw std::runtime_error("gladLoadGL failed");
        }
        glfwSwapInterval(0);
        glDisable(GL_DITHER);
        glDisable(GL_MULTISAMPLE);
    }
    ~Context() { glfwDestroyWindow(window); glfwTerminate(); }
};

struct Counters {
    uint64_t calls = 0, pixels = 0;
};
Counters reads;
PFNGLREADPIXELSPROC realReadPixels = nullptr;
bool captureNextTarget = false;
GLint capturedTarget = 0;

void GLAD_API_PTR countedReadPixels(GLint x, GLint y, GLsizei width, GLsizei height,
                                   GLenum format, GLenum type, void* pixels) {
    ++reads.calls;
    reads.pixels += (uint64_t)width * (uint64_t)height;
    if (captureNextTarget) {
        glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &capturedTarget);
        captureNextTarget = false;
    }
    realReadPixels(x, y, width, height, format, type, pixels);
}

struct ReadbackHook {
    ReadbackHook() { realReadPixels = glad_glReadPixels; glad_glReadPixels = countedReadPixels; }
    ~ReadbackHook() { glad_glReadPixels = realReadPixels; }
};

uint64_t fold(uint64_t hash, uint32_t value) {
    // Explicit byte order makes fingerprints independent of the CPU's endianness.
    for (int i = 0; i < 4; ++i) {
        hash ^= (value >> (i * 8)) & 255u;
        hash *= 1099511628211ull;
    }
    return hash;
}
constexpr uint64_t kHashStart = 1469598103934665603ull;
uint32_t packed(PickHit hit) { return render::encodeId(hit.body_id, hit.kind, hit.topo_id); }
std::string hex(uint64_t value) {
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << value;
    return out.str();
}
std::string json(const std::string& text) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << (int)c;
        else out << c;
    }
    out << '"';
    return out.str();
}
std::string glString(GLenum name) {
    const auto* text = glGetString(name);
    return text ? reinterpret_cast<const char*>(text) : "unavailable";
}
std::vector<unsigned> glErrors() {
    std::vector<unsigned> errors;
    for (GLenum error; (error = glGetError()) != GL_NO_ERROR;) errors.push_back(error);
    return errors;
}
double elapsedNs(Clock::time_point start) {
    return std::chrono::duration<double, std::nano>(Clock::now() - start).count();
}

void tiledBody(render::DocMesh& doc, uint32_t bodyId, float left, float bottom,
               float right, float top, float z, int nx, int ny, uint32_t idBase) {
    render::BodyMesh body;
    body.body_id = bodyId;
    for (int row = 0; row < ny; ++row) {
        for (int col = 0; col < nx; ++col) {
            float x0 = left + (right - left) * col / nx;
            float x1 = left + (right - left) * (col + 1) / nx;
            float y0 = bottom + (top - bottom) * row / ny;
            float y1 = bottom + (top - bottom) * (row + 1) / ny;
            uint32_t vertex = (uint32_t)body.pos.size() / 3;
            uint32_t tri = (uint32_t)body.idx.size() / 3;
            const float corners[4][3] = {{x0,y0,z}, {x1,y0,z}, {x1,y1,z}, {x0,y1,z}};
            uint32_t id = idBase + (uint32_t)(row * nx + col) * 8;
            for (const auto& corner : corners)
                body.pos.insert(body.pos.end(), corner, corner + 3);
            body.idx.insert(body.idx.end(), {vertex, vertex + 1, vertex + 2,
                                            vertex, vertex + 2, vertex + 3});
            body.faces.push_back({id, tri, 2, 0});
            for (int k = 0; k < 4; ++k) {
                uint32_t first = (uint32_t)body.edge_pts.size() / 3;
                body.edge_pts.insert(body.edge_pts.end(), corners[k], corners[k] + 3);
                body.edge_pts.insert(body.edge_pts.end(), corners[(k + 1) % 4],
                                     corners[(k + 1) % 4] + 3);
                body.edges.push_back({id + (uint32_t)k + 1, first, 2, 0});
                body.brep_verts.push_back({id + (uint32_t)k + 1,
                                           {corners[k][0], corners[k][1], z}});
            }
        }
    }
    body.n_verts = (uint32_t)body.pos.size() / 3;
    body.n_tris = (uint32_t)body.idx.size() / 3;
    doc.bodies.push_back(std::move(body));
}

render::DocMesh fixture(int tiles, uint32_t topologyOffset = 0) {
    render::DocMesh doc;
    doc.version = 1;
    tiledBody(doc, 1, -.92f, -.88f, .92f, .88f, .45f,
              tiles, std::max(1, tiles * 2 / 3), 1 + topologyOffset);
    // High body and topology bits exercise all four RGBA bytes, not just small IDs.
    tiledBody(doc, 2049, -.64f, -.58f, -.12f, .66f, -.20f, 2, 3, 262000 + topologyOffset);
    tiledBody(doc, 4095, .08f, -.56f, .72f, .56f, -.35f, 3, 2, 65536 + topologyOffset);
    return doc;
}

std::vector<std::pair<int, int>> positions(const Options& o, const Renderer::CameraXform& cam,
                                          int width, int height) {
    std::vector<std::pair<int, int>> points;
    for (int y = 0; y < height; y += o.gridStep)
        for (int x = 0; x < width; x += o.gridStep) points.emplace_back(x, y);
    for (int x : {0, 1, width - 2, width - 1})
        for (int y : {0, 1, height / 2, height - 2, height - 1}) points.emplace_back(x, y);
    for (int y : {0, 1, height - 2, height - 1})
        for (int x : {0, 1, width / 2, width - 2, width - 1}) points.emplace_back(x, y);
    // Both sides of corners, thin lines, overlapping-body boundaries, and equidistant targets.
    const float landmarks[][2] = {{-.92f,-.88f}, {.92f,.88f}, {-.64f,-.58f}, {-.12f,.66f},
        {-.64f,.66f}, {-.12f,-.58f}, {-.38f,.04f}, {.08f,-.56f}, {.72f,.56f},
        {.08f,.56f}, {.72f,-.56f}, {.4f,0}, {0,0}, {-.92f,0}, {.92f,0}};
    for (const auto& p : landmarks) {
        float x = cam.mvp.m[0] * p[0] + cam.mvp.m[4] * p[1] + cam.mvp.m[12];
        float y = cam.mvp.m[1] * p[0] + cam.mvp.m[5] * p[1] + cam.mvp.m[13];
        int px = (int)std::floor((x + 1) * .5f * width);
        int py = (int)std::floor((1 - y) * .5f * height);
        for (int dy = -2; dy <= 2; ++dy)
            for (int dx = -2; dx <= 2; ++dx)
                if (px + dx >= 0 && px + dx < width && py + dy >= 0 && py + dy < height)
                    points.emplace_back(px + dx, py + dy);
    }
    std::sort(points.begin(), points.end());
    points.erase(std::unique(points.begin(), points.end()), points.end());
    return points;
}

struct Batch {
    std::string method;
    int radius = 0;
    uint64_t queries = 0, readbacks = 0, pixels = 0, fingerprint = kHashStart;
    std::array<uint64_t, 4> kinds{};
    double nsPerQuery = 0;
};
struct Stage {
    std::string name;
    int width = 0, height = 0, ssWidth = 0, ssHeight = 0;
    bool facesOnly = false;
    uint64_t anchorReadbacks = 0;
    uint32_t anchorHit = 0;
    double renderMs = 0;
    std::vector<Batch> batches;
    std::vector<unsigned> errors;
};
struct Stable {
    std::string name;
    int iterations = 0;
    uint64_t firstReadbacks = 0, repeatReadbacks = 0, repeatPixels = 0;
    uint32_t hit = 0;
    double nsPerQuery = 0;
};
struct Guard { std::string name; bool passed; };
struct Result {
    std::string mode, png;
    uint64_t fingerprint = kHashStart;
    std::vector<Stage> stages;
    std::vector<Stable> stable;
    std::vector<Guard> guards;
};

void savePng(const std::string& path, bool rgba8, int width, int height) {
    if (!capturedTarget) throw std::runtime_error("no actual ID framebuffer was captured");
    GLint oldTarget = 0;
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &oldTarget);
    glBindFramebuffer(GL_READ_FRAMEBUFFER, (GLuint)capturedTarget);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    std::vector<uint32_t> ids((size_t)width * height);
    if (rgba8) {
        std::vector<unsigned char> bytes(ids.size() * 4);
        glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, bytes.data());
        for (size_t i = 0; i < ids.size(); ++i)
            ids[i] = (uint32_t)bytes[i*4] | ((uint32_t)bytes[i*4+1] << 8) |
                     ((uint32_t)bytes[i*4+2] << 16) | ((uint32_t)bytes[i*4+3] << 24);
    } else glReadPixels(0, 0, width, height, GL_RED_INTEGER, GL_UNSIGNED_INT, ids.data());
    glBindFramebuffer(GL_READ_FRAMEBUFFER, (GLuint)oldTarget);
    std::vector<unsigned char> rgb(ids.size() * 3);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            uint32_t id = ids[(size_t)(height - 1 - y) * width + x];
            uint32_t color = id * 2654435761u;
            size_t offset = ((size_t)y * width + x) * 3;
            for (int c = 0; c < 3; ++c)
                rgb[offset + c] = id ? (unsigned char)(48 + ((color >> (c * 8)) & 127)) : 0;
            if (render::decodeId(id).kind == PickKind::Edge) rgb[offset + 1] = 255;
            if (render::decodeId(id).kind == PickKind::Vertex) rgb[offset] = rgb[offset + 2] = 255;
        }
    }
    if (!stbi_write_png(path.c_str(), width, height, 3, rgb.data(), width * 3))
        throw std::runtime_error("could not write PNG: " + path);
}

Result runMode(const Options& o, const std::string& mode, std::ostream* hits) {
#ifdef _WIN32
    if (_putenv_s("CHUDWARE_IDBUF", mode.c_str()) != 0)
#else
    if (setenv("CHUDWARE_IDBUF", mode.c_str(), 1) != 0)
#endif
        throw std::runtime_error("could not set CHUDWARE_IDBUF");
    render::IdPass pass;
    std::string error;
    auto doc = fixture(o.tiles);
    if (!pass.setDoc(doc, error)) throw std::runtime_error(error);
    Renderer::CameraXform cam;
    cam.ssWidth = o.width * o.supersample;
    cam.ssHeight = o.height * o.supersample;
    cam.vpWidth = o.width;
    cam.vpHeight = o.height;
    int width = o.width, height = o.height;
    bool facesOnly = false;
    Result result;
    result.mode = mode;
    auto guard = [&](const std::string& name, bool passed) { result.guards.push_back({name, passed}); };
    auto unrenderedGuard = [&](const std::string& name) {
        Counters before = reads;
        bool empty = !pass.pickFinal(width / 2, height / 2, width, height).valid() &&
                     !pass.pickFinalPreferEdge(width / 2, height / 2, width, height, 7).valid();
        guard(name, empty && reads.calls == before.calls);
    };
    auto stage = [&](const std::string& name) {
        Stage s;
        s.name = name;
        s.width = width; s.height = height;
        s.ssWidth = cam.ssWidth; s.ssHeight = cam.ssHeight;
        s.facesOnly = facesOnly;
        glFinish();
        auto renderStart = Clock::now();
        if (!pass.render(cam, facesOnly)) throw std::runtime_error("render failed: " + name);
        glFinish();
        s.renderMs = elapsedNs(renderStart) / 1e6;
        uint64_t anchorStart = reads.calls;
        s.anchorHit = packed(pass.pickFinalPreferEdge(o.width / 2, o.height / 2, width, height, 7));
        s.anchorReadbacks = reads.calls - anchorStart;
        result.fingerprint = fold(result.fingerprint, s.anchorHit);
        if (hits) *hits << mode << ',' << name << ",held-hover,7," << o.width / 2 << ','
                        << o.height / 2 << ',' << width << ',' << height << ',' << s.anchorHit << '\n';
        if (o.requireMemoized) guard(name + ":fresh-held-hover", s.anchorReadbacks == 1);
        if (name == "initial" && !o.png.empty()) {
            captureNextTarget = true;
            pass.pickFinal(0, 0, width, height);
            result.png = o.png + "-" + mode + ".png";
            savePng(result.png, mode == "rgba8", cam.ssWidth, cam.ssHeight);
        }
        auto points = positions(o, cam, width, height);
        for (int method = 0; method < 6; ++method) {
            Batch b;
            b.method = method == 0 ? "exact" : "prefer_edge";
            const int radii[] = {0, -2, 0, 1, 7, 14};
            b.radius = radii[method];
            std::vector<PickHit> sampled(points.size());
            Counters before = reads;
            auto start = Clock::now();
            for (size_t i = 0; i < points.size(); ++i) {
                auto p = points[i];
                sampled[i] = method == 0 ? pass.pickFinal(p.first, p.second, width, height) :
                    pass.pickFinalPreferEdge(p.first, p.second, width, height, b.radius);
            }
            b.queries = points.size();
            b.nsPerQuery = elapsedNs(start) / (double)b.queries;
            b.readbacks = reads.calls - before.calls;
            b.pixels = reads.pixels - before.pixels;
            // Hashing, histograms and optional CSV I/O are outside the measured query loop.
            for (size_t i = 0; i < points.size(); ++i) {
                auto p = points[i];
                uint32_t id = packed(sampled[i]);
                b.fingerprint = fold(fold(fold(b.fingerprint, (uint32_t)p.first),
                                          (uint32_t)p.second), id);
                ++b.kinds[(unsigned)sampled[i].kind];
                if (hits) *hits << mode << ',' << name << ',' << b.method << ',' << b.radius << ','
                                << p.first << ',' << p.second << ',' << width << ',' << height
                                << ',' << id << '\n';
            }
            result.fingerprint = fold(fold(result.fingerprint, (uint32_t)b.fingerprint),
                                      (uint32_t)(b.fingerprint >> 32));
            if (facesOnly) guard(name + ":" + b.method + ":faces-only",
                                 b.kinds[2] == 0 && b.kinds[3] == 0);
            s.batches.push_back(std::move(b));
        }
        // Leave this exact query cached so the next state transition must refresh a held hover,
        // not merely a different coordinate that would have missed the one-entry memo anyway.
        pass.pickFinalPreferEdge(o.width / 2, o.height / 2, width, height, 7);
        s.errors = glErrors();
        result.stages.push_back(std::move(s));
    };
    auto stable = [&](const std::string& name, int x, int y, int radius) {
        Stable s;
        s.name = name; s.iterations = o.iterations;
        pass.pickFinal(0, 0, width, height);  // force a different lookup before the first hover
        auto pick = [&]() {
            if (!pass.render(cam, facesOnly)) throw std::runtime_error("stable render failed");
            return radius < 0 ? pass.pickFinal(x, y, width, height) :
                pass.pickFinalPreferEdge(x, y, width, height, radius);
        };
        glFinish();
        uint64_t before = reads.calls;
        PickHit expected = pick();
        s.firstReadbacks = reads.calls - before;
        s.hit = packed(expected);
        Counters repeatStart = reads;
        auto start = Clock::now();
        for (int i = 0; i < o.iterations; ++i)
            if (pick() != expected) throw std::runtime_error("unchanged hover changed its hit");
        s.nsPerQuery = elapsedNs(start) / o.iterations;
        s.repeatReadbacks = reads.calls - repeatStart.calls;
        s.repeatPixels = reads.pixels - repeatStart.pixels;
        result.fingerprint = fold(result.fingerprint, s.hit);
        result.stable.push_back(s);
    };

    unrenderedGuard("new-document");
    stage("initial");
    stable("exact", width * 3 / 5, height * 2 / 5, -1);
    stable("prefer-face", width * 3 / 5, height * 2 / 5, 7);
    stable("prefer-boundary", (int)(width * .54f), height / 2, 14);
    Counters before = reads;
    guard("invalid-query", !pass.pickFinal(-1, 0, width, height).valid() &&
          !pass.pickFinal(width, 0, width, height).valid() &&
          !pass.pickFinal(0, height, width, height).valid() &&
          !pass.pickFinal(0, 0, 0, height).valid() &&
          !pass.pickFinalPreferEdge(0, -1, width, height, 7).valid() &&
          !pass.pickFinalPreferEdge(0, 0, width, 0, 7).valid() && reads.calls == before.calls);

    auto probe = [&](const std::string& name, int x, int y, int fw, int fh,
                     bool exact, int radius, uint64_t expectedReads) {
        uint64_t start = reads.calls;
        PickHit hit = exact ? pass.pickFinal(x, y, fw, fh) :
                              pass.pickFinalPreferEdge(x, y, fw, fh, radius);
        result.fingerprint = fold(result.fingerprint, packed(hit));
        if (hits) *hits << mode << ",memo-key," << name << ',' << radius << ',' << x << ',' << y
                        << ',' << fw << ',' << fh << ',' << packed(hit) << '\n';
        if (o.requireMemoized) guard(name, reads.calls - start == expectedReads);
    };
    int ax = width / 2, ay = height / 2;
    pass.pickFinal(0, 0, width, height);
    probe("exact-first", ax, ay, width, height, true, 0, 1);
    probe("exact-repeat", ax, ay, width, height, true, 0, 0);
    probe("x-change", ax + 1, ay, width, height, true, 0, 1);
    probe("y-change", ax + 1, ay + 1, width, height, true, 0, 1);
    probe("width-change", ax + 1, ay + 1, width + 13, height, true, 0, 1);
    probe("height-change", ax + 1, ay + 1, width + 13, height - 7, true, 0, 1);
    probe("radius-zero", ax + 1, ay + 1, width + 13, height - 7, false, 0, 0);
    probe("negative-radius", ax + 1, ay + 1, width + 13, height - 7, false, -2, 0);
    probe("radius-one", ax + 1, ay + 1, width + 13, height - 7, false, 1, 1);
    probe("radius-seven", ax + 1, ay + 1, width + 13, height - 7, false, 7, 1);
    probe("radius-fourteen", ax + 1, ay + 1, width + 13, height - 7, false, 14, 1);
    probe("radius-repeat", ax + 1, ay + 1, width + 13, height - 7, false, 14, 0);
    probe("exact-after-radius", ax + 1, ay + 1, width + 13, height - 7, true, 0, 1);
    probe("radius-after-exact", ax + 1, ay + 1, width + 13, height - 7, false, 14, 1);
    probe("radius-repeat-again", ax + 1, ay + 1, width + 13, height - 7, false, 14, 0);
    pass.pickFinalPreferEdge(o.width / 2, o.height / 2, width, height, 7);

    cam.mvp.m[12] = .137f;
    cam.mvp.m[13] = -.071f;
    stage("camera");
    pass.setHiddenBodies({4095});
    unrenderedGuard("visibility-dirty");
    stage("visibility-hidden");
    pass.setHiddenBodies({});
    stage("visibility-restored");
    float normals[3][3] = {{1,0,0}, {0,-1,0}, {0,0,1}};
    float offsets[3] = {.25f, .30f, 1};
    pass.setSectionClip(1, normals, offsets);
    unrenderedGuard("clip-dirty");
    stage("clip-one");
    normals[0][0] = .8f; normals[0][1] = .6f;
    offsets[0] = .1f;
    pass.setSectionClip(2, normals, offsets);
    stage("clip-normal-offset-count");
    pass.setSectionClip(0, normals, offsets);
    stage("clip-disabled");
    doc = fixture(o.tiles, 17);
    if (!pass.setDoc(doc, error)) throw std::runtime_error(error);
    unrenderedGuard("topology-dirty");
    stage("topology");
    width += 13; height -= 7;
    // Same rendered generation, different final-pixel mapping must not reuse a lookup.
    stage("final-dimensions");
    cam.ssWidth += 29; cam.ssHeight += 17;
    stage("frame-dimensions");
    facesOnly = true;
    stage("faces-only");
    facesOnly = false;
    stage("full-topology-restored");
    auto badCam = cam;
    badCam.ssWidth = 0;
    guard("failed-render-return", !pass.render(badCam));
    // Old code leaves a readable stale frame here; report it without contaminating A/B hit CSVs.
    unrenderedGuard("failed-render-invalidates");
    stage("failed-render-recovered");
    auto badDoc = doc;
    badDoc.bodies[0].body_id = render::kMaxPickBodyId + 1;
    guard("rejected-document-return", !pass.setDoc(badDoc, error));
    unrenderedGuard("rejected-document-invalidates");
    if (!pass.setDoc(doc, error)) throw std::runtime_error(error);
    stage("document-recovered");
    return result;
}

void printResult(const Options& o, const std::vector<Result>& results) {
    std::cout << std::setprecision(12)
              << "{\"schema\":1,\"vendor\":" << json(glString(GL_VENDOR))
              << ",\"renderer\":" << json(glString(GL_RENDERER))
              << ",\"version\":" << json(glString(GL_VERSION))
              << ",\"width\":" << o.width << ",\"height\":" << o.height
              << ",\"supersample\":" << o.supersample << ",\"tiles\":" << o.tiles
              << ",\"grid_step\":" << o.gridStep << ",\"iterations\":" << o.iterations
              << ",\"results\":[";
    bool firstMode = true;
    for (const auto& r : results) {
        if (!firstMode) std::cout << ',';
        firstMode = false;
        std::cout << "{\"mode\":" << json(r.mode) << ",\"fingerprint\":" << json(hex(r.fingerprint))
                  << ",\"png\":" << json(r.png) << ",\"stages\":[";
        bool firstStage = true;
        for (const auto& s : r.stages) {
            if (!firstStage) std::cout << ',';
            firstStage = false;
            std::cout << "{\"name\":" << json(s.name) << ",\"width\":" << s.width
                      << ",\"height\":" << s.height << ",\"ss_width\":" << s.ssWidth
                      << ",\"ss_height\":" << s.ssHeight << ",\"faces_only\":"
                      << (s.facesOnly ? "true" : "false") << ",\"render_ms\":" << s.renderMs
                      << ",\"anchor_hit\":" << s.anchorHit
                      << ",\"anchor_readbacks\":" << s.anchorReadbacks << ",\"gl_errors\":[";
            for (size_t i = 0; i < s.errors.size(); ++i) {
                if (i) std::cout << ',';
                std::cout << s.errors[i];
            }
            std::cout << "],\"batches\":[";
            bool firstBatch = true;
            for (const auto& b : s.batches) {
                if (!firstBatch) std::cout << ',';
                firstBatch = false;
                std::cout << "{\"method\":" << json(b.method) << ",\"radius\":" << b.radius
                          << ",\"queries\":" << b.queries << ",\"ns_per_query\":" << b.nsPerQuery
                          << ",\"readbacks\":" << b.readbacks << ",\"readback_pixels\":" << b.pixels
                          << ",\"fingerprint\":" << json(hex(b.fingerprint)) << ",\"kinds\":[";
                for (size_t i = 0; i < b.kinds.size(); ++i) {
                    if (i) std::cout << ',';
                    std::cout << b.kinds[i];
                }
                std::cout << "]}";
            }
            std::cout << "]}";
        }
        std::cout << "],\"stable\":[";
        for (size_t i = 0; i < r.stable.size(); ++i) {
            if (i) std::cout << ',';
            const auto& s = r.stable[i];
            std::cout << "{\"name\":" << json(s.name) << ",\"iterations\":" << s.iterations
                      << ",\"first_readbacks\":" << s.firstReadbacks
                      << ",\"repeat_readbacks\":" << s.repeatReadbacks
                      << ",\"repeat_readback_pixels\":" << s.repeatPixels
                      << ",\"ns_per_query\":" << s.nsPerQuery << ",\"hit\":" << s.hit << '}';
        }
        std::cout << "],\"guards\":[";
        for (size_t i = 0; i < r.guards.size(); ++i) {
            if (i) std::cout << ',';
            std::cout << "{\"name\":" << json(r.guards[i].name) << ",\"passed\":"
                      << (r.guards[i].passed ? "true" : "false") << '}';
        }
        std::cout << "]}";
    }
    std::cout << "]}\n";
}
}  // namespace

int main(int argc, char** argv) {
    try {
        Options o = options(argc, argv);
        Context context;
        GLint maxSize = 0;
        glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxSize);
        if ((int64_t)o.width * o.supersample + 29 > maxSize ||
            (int64_t)o.height * o.supersample + 17 > maxSize)
            throw std::runtime_error("requested framebuffer exceeds GL_MAX_TEXTURE_SIZE");
        ReadbackHook hook;
        std::ofstream hits;
        if (!o.hits.empty()) {
            hits.open(o.hits);
            if (!hits) throw std::runtime_error("could not open hit CSV: " + o.hits);
            hits << "mode,state,method,radius,x,y,final_width,final_height,encoded_id\n";
        }
        std::vector<Result> results;
        for (const std::string mode : {"r32ui", "rgba8"})
            if (o.mode == "both" || o.mode == mode)
                results.push_back(runMode(o, mode, hits.is_open() ? &hits : nullptr));
        if (hits.is_open()) {
            hits.flush();
            if (!hits) throw std::runtime_error("hit CSV write failed");
        }
        printResult(o, results);
        bool passed = true;
        for (const auto& result : results) {
            for (const auto& stage : result.stages)
                if (!stage.errors.empty()) passed = false;
            for (const auto& g : result.guards)
                if (!g.passed && (o.requireMemoized || g.name != "failed-render-invalidates"))
                    passed = false;
            if (o.requireMemoized)
                for (const auto& s : result.stable)
                    if (s.firstReadbacks != 1 || s.repeatReadbacks != 0) passed = false;
        }
        if (results.size() == 2 && results[0].fingerprint != results[1].fingerprint) {
            std::cerr << "R32UI and RGBA8 produced different hit fingerprints\n";
            passed = false;
        }
        return passed ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "profile_viewport: " << error.what() << '\n';
        return 1;
    }
}
