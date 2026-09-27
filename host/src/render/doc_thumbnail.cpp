#include "doc_thumbnail.h"

#include "gl_compat.h"
#include "ui/viewport/glsl_compat.h"
#include "vec_math.h"

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <vector>

namespace render {

namespace {

const char* kVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
uniform mat4 uMVP;
out vec3 vN;
out vec3 vPos;
void main(){ vN = aNormal; vPos = aPos; gl_Position = uMVP * vec4(aPos, 1.0); })";

// The viewport's studio shading (renderer.cpp kFrag) minus the section-clip / scalar / zebra /
// ghosting paths a still preview never uses: same lights, ambient and RealView reflection, so a
// card reads as the same material the user sees after opening the doc.
const char* kFrag = R"(#version 330 core
in vec3 vN;
in vec3 vPos;
uniform vec3 uKeyDir;
uniform vec3 uFillDir;
uniform vec3 uRimDir;
uniform vec3 uCamPos;
uniform vec3 uColor;
uniform float uMetallic;
uniform float uRoughness;
out vec4 frag;
vec3 env(vec3 R){
    float up = R.z;
    vec3 sky   = vec3(0.97, 0.98, 1.00);
    vec3 horiz = vec3(0.66, 0.68, 0.72);
    vec3 floorC= vec3(0.30, 0.30, 0.32);
    vec3 c = (up >= 0.0) ? mix(horiz, sky, smoothstep(0.0, 0.55, up))
                         : mix(horiz, floorC, smoothstep(0.0, -0.45, up));
    vec3 d1 = normalize(vec3(-0.35,  0.30, 0.88));
    vec3 d2 = normalize(vec3( 0.62,  0.10, 0.55));
    c += vec3(smoothstep(0.62, 0.985, dot(R, d1)) + smoothstep(0.70, 0.990, dot(R, d2)) * 0.6);
    return c;
}
void main(){
    vec3 N = normalize(vN);
    vec3 V = normalize(uCamPos - vPos);
    if (dot(N, V) < 0.0) N = -N;
    vec3 base = pow(uColor, vec3(2.2));
    const vec3 keyCol  = vec3(1.00, 0.99, 0.97);
    const vec3 fillCol = vec3(0.85, 0.88, 0.95);
    const vec3 rimCol  = vec3(0.90, 0.93, 1.00);
    const vec3 skyCol  = vec3(0.92, 0.94, 0.98);
    const vec3 gndCol  = vec3(0.34, 0.33, 0.31);
    float key  = max(dot(N, normalize(uKeyDir)),  0.0);
    float fill = max(dot(N, normalize(uFillDir)), 0.0);
    float rim  = max(dot(N, normalize(uRimDir)),  0.0);
    float hemi = N.z * 0.5 + 0.5;
    vec3 diffuse = keyCol * (key * 0.50) + fillCol * (fill * 0.18);
    vec3 ambient = mix(gndCol, skyCol, hemi) * 0.55;
    float edge = pow(1.0 - max(dot(N, V), 0.0), 2.0);
    vec3 rimLight = rimCol * (rim * edge * 0.10);
    float diffK = 1.0 - uMetallic * 0.7;
    vec3 H = normalize(normalize(uKeyDir) + V);
    float spec = pow(max(dot(N, H), 0.0), mix(100.0, 12.0, uRoughness)) * 0.13;
    vec3 c = base * ((diffuse + ambient) * diffK) + rimLight + keyCol * spec;
    vec3 R = reflect(-V, N);
    float fres = 0.04 + 0.96 * pow(1.0 - max(dot(N, V), 0.0), 5.0);
    vec3 F0 = mix(vec3(0.04), base * 0.6 + 0.4 * uMetallic, uMetallic);
    vec3 F  = F0 + (1.0 - F0) * pow(1.0 - max(dot(N, V), 0.0), 5.0);
    vec3 fresV = mix(vec3(fres), F, uMetallic);
    float k = mix(0.42, 0.80, uMetallic);
    vec3 envAmb = pow(mix(gndCol, skyCol, hemi), vec3(2.2));
    float blur = clamp((uRoughness - 0.5) * 2.0, 0.0, 1.0) * 0.7;
    c += mix(pow(env(R), vec3(2.2)), envAmb, blur) * fresV * k;
    frag = vec4(pow(max(c, 0.0), vec3(1.0 / 2.2)), 1.0);
})";

const char* kEdgeVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
void main(){ gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kEdgeFrag = R"(#version 330 core
uniform vec3 uColor;
out vec4 frag;
void main(){ frag = vec4(uColor, 1.0); })";

// Full-target triangle; vT runs +1 at the IMAGE top. The image is stored top-left origin, so
// GL's bottom row (ndc y = -1) is the displayed top — hence the negation.
const char* kBgVert = R"(#version 330 core
out float vT;
void main(){
    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    vec2 ndc = pos * 2.0 - 1.0;
    vT = -ndc.y;
    gl_Position = vec4(ndc, 0.999, 1.0);
})";

const char* kBgFrag = R"(#version 330 core
in float vT;
uniform vec3 uTop;
uniform vec3 uMid;
uniform vec3 uBot;
out vec4 frag;
void main(){
    float t = clamp(vT * 0.5 + 0.5, 0.0, 1.0);
    const float midPos = 0.55;
    vec3 c = (t < midPos) ? mix(uBot, uMid, smoothstep(0.0, midPos, t))
                          : mix(uMid, uTop, smoothstep(midPos, 1.0, t));
    frag = vec4(c, 1.0);
})";

constexpr int kSupersample = 2;

struct Gl {
    unsigned int prog = 0, edgeProg = 0, bgProg = 0, bgVao = 0;
    int uMvp = -1, uKey = -1, uFill = -1, uRim = -1, uCam = -1, uColor = -1, uMetal = -1,
        uRough = -1;
    int uEdgeMvp = -1, uEdgeColor = -1;
    int uBgTop = -1, uBgMid = -1, uBgBot = -1;
    unsigned int ssFbo = 0, ssColor = 0, ssDepth = 0, dstFbo = 0;
    int ssW = 0, ssH = 0;
    bool failed = false;
};
Gl g;

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
        std::fprintf(stderr, "doc_thumbnail shader compile error: %s\n", log);
    }
    return s;
}

unsigned int link(const char* vs, const char* fs) {
    const unsigned int v = compile(GL_VERTEX_SHADER, vs);
    const unsigned int f = compile(GL_FRAGMENT_SHADER, fs);
    const unsigned int p = glCreateProgram();
    glAttachShader(p, v);
    glAttachShader(p, f);
    glLinkProgram(p);
    glDeleteShader(v);
    glDeleteShader(f);
    int ok = 0;
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    if (!ok) {
        glDeleteProgram(p);
        return 0;
    }
    return p;
}

bool ensurePrograms(std::string& err) {
    if (g.failed) { err = "preview shaders unavailable"; return false; }
    if (g.prog) return true;
    g.prog = link(kVert, kFrag);
    g.edgeProg = link(kEdgeVert, kEdgeFrag);
    g.bgProg = link(kBgVert, kBgFrag);
    if (!g.prog || !g.edgeProg || !g.bgProg) {
        g.failed = true;
        err = "preview shaders unavailable";
        return false;
    }
    g.uMvp   = glGetUniformLocation(g.prog, "uMVP");
    g.uKey   = glGetUniformLocation(g.prog, "uKeyDir");
    g.uFill  = glGetUniformLocation(g.prog, "uFillDir");
    g.uRim   = glGetUniformLocation(g.prog, "uRimDir");
    g.uCam   = glGetUniformLocation(g.prog, "uCamPos");
    g.uColor = glGetUniformLocation(g.prog, "uColor");
    g.uMetal = glGetUniformLocation(g.prog, "uMetallic");
    g.uRough = glGetUniformLocation(g.prog, "uRoughness");
    g.uEdgeMvp   = glGetUniformLocation(g.edgeProg, "uMVP");
    g.uEdgeColor = glGetUniformLocation(g.edgeProg, "uColor");
    g.uBgTop = glGetUniformLocation(g.bgProg, "uTop");
    g.uBgMid = glGetUniformLocation(g.bgProg, "uMid");
    g.uBgBot = glGetUniformLocation(g.bgProg, "uBot");
    glGenVertexArrays(1, &g.bgVao);
    return true;
}

bool ensureTargets(int ssW, int ssH, std::string& err) {
    if (!g.ssFbo) glGenFramebuffers(1, &g.ssFbo);
    if (!g.dstFbo) glGenFramebuffers(1, &g.dstFbo);
    if (!g.ssColor) glGenRenderbuffers(1, &g.ssColor);
    if (!g.ssDepth) glGenRenderbuffers(1, &g.ssDepth);
    if (ssW == g.ssW && ssH == g.ssH) return true;
    glBindRenderbuffer(GL_RENDERBUFFER, g.ssColor);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, ssW, ssH);
    glBindRenderbuffer(GL_RENDERBUFFER, g.ssDepth);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, ssW, ssH);
    glBindFramebuffer(GL_FRAMEBUFFER, g.ssFbo);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, g.ssColor);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, g.ssDepth);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        g.ssW = g.ssH = 0;
        err = "preview framebuffer incomplete";
        return false;
    }
    g.ssW = ssW;
    g.ssH = ssH;
    return true;
}

// Everything this pass changes, captured up front and put back on every exit path.
struct SavedGl {
    int drawFbo = 0, readFbo = 0, viewport[4] = {0, 0, 0, 0}, program = 0, vao = 0,
        arrayBuf = 0, rbo = 0, tex = 0, depthFunc = GL_LESS;
    float clear[4] = {0, 0, 0, 0};
    unsigned char depthTest = 0, blend = 0, cull = 0, scissor = 0, polyOffset = 0,
        depthMask = 1;
    SavedGl() {
        glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &drawFbo);
        glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &readFbo);
        glGetIntegerv(GL_VIEWPORT, viewport);
        glGetIntegerv(GL_CURRENT_PROGRAM, &program);
        glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
        glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &arrayBuf);
        glGetIntegerv(GL_RENDERBUFFER_BINDING, &rbo);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex);
        glGetIntegerv(GL_DEPTH_FUNC, &depthFunc);
        glGetFloatv(GL_COLOR_CLEAR_VALUE, clear);
        depthTest  = glIsEnabled(GL_DEPTH_TEST);
        blend      = glIsEnabled(GL_BLEND);
        cull       = glIsEnabled(GL_CULL_FACE);
        scissor    = glIsEnabled(GL_SCISSOR_TEST);
        polyOffset = glIsEnabled(GL_POLYGON_OFFSET_FILL);
        GLboolean dm = GL_TRUE;
        glGetBooleanv(GL_DEPTH_WRITEMASK, &dm);
        depthMask = dm;
    }
    static void cap(unsigned int c, unsigned char on) { on ? glEnable(c) : glDisable(c); }
    ~SavedGl() {
        glBindVertexArray((unsigned int)vao);
        glBindBuffer(GL_ARRAY_BUFFER, (unsigned int)arrayBuf);
        glUseProgram((unsigned int)program);
        glBindRenderbuffer(GL_RENDERBUFFER, (unsigned int)rbo);
        glBindTexture(GL_TEXTURE_2D, (unsigned int)tex);
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, (unsigned int)drawFbo);
        glBindFramebuffer(GL_READ_FRAMEBUFFER, (unsigned int)readFbo);
        glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
        glClearColor(clear[0], clear[1], clear[2], clear[3]);
        glDepthFunc((unsigned int)depthFunc);
        glDepthMask(depthMask ? GL_TRUE : GL_FALSE);
        cap(GL_DEPTH_TEST, depthTest);
        cap(GL_BLEND, blend);
        cap(GL_CULL_FACE, cull);
        cap(GL_SCISSOR_TEST, scissor);
        cap(GL_POLYGON_OFFSET_FILL, polyOffset);
    }
};

// One body's transient GPU copy: positions + normals + triangle indices, plus feature-edge
// polylines expanded to a line-list index over edge_pts.
struct BodyGpu {
    unsigned int vao = 0, pos = 0, nrm = 0, ebo = 0;
    unsigned int edgeVao = 0, edgeVbo = 0, edgeEbo = 0;
    int edgeIdx = 0;
    BodyGpu() = default;
    BodyGpu(const BodyGpu&) = delete;
    BodyGpu& operator=(const BodyGpu&) = delete;
    ~BodyGpu() {
        if (vao) glDeleteVertexArrays(1, &vao);
        if (edgeVao) glDeleteVertexArrays(1, &edgeVao);
        unsigned int bufs[5] = {pos, nrm, ebo, edgeVbo, edgeEbo};
        for (unsigned int b : bufs)
            if (b) glDeleteBuffers(1, &b);
    }
};

void upload(const BodyMesh& b, BodyGpu& d) {
    glGenVertexArrays(1, &d.vao);
    glBindVertexArray(d.vao);
    glGenBuffers(1, &d.pos);
    glBindBuffer(GL_ARRAY_BUFFER, d.pos);
    glBufferData(GL_ARRAY_BUFFER, (long)(b.pos.size() * sizeof(float)), b.pos.data(),
                 GL_STREAM_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    if (b.nrm.size() == b.pos.size()) {
        glGenBuffers(1, &d.nrm);
        glBindBuffer(GL_ARRAY_BUFFER, d.nrm);
        glBufferData(GL_ARRAY_BUFFER, (long)(b.nrm.size() * sizeof(float)), b.nrm.data(),
                     GL_STREAM_DRAW);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    } else {
        glDisableVertexAttribArray(1);
        glVertexAttrib3f(1, 0.0f, 0.0f, 1.0f);
    }
    glGenBuffers(1, &d.ebo);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, d.ebo);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, (long)(b.idx.size() * sizeof(uint32_t)), b.idx.data(),
                 GL_STREAM_DRAW);

    const uint32_t nPts = (uint32_t)(b.edge_pts.size() / 3);
    std::vector<uint32_t> lines;
    for (const auto& e : b.edges) {
        if (e.pt_count < 2 || e.first_pt + e.pt_count > nPts) continue;
        for (uint32_t i = 0; i + 1 < e.pt_count; ++i) {
            lines.push_back(e.first_pt + i);
            lines.push_back(e.first_pt + i + 1);
        }
    }
    if (!lines.empty()) {
        glGenVertexArrays(1, &d.edgeVao);
        glBindVertexArray(d.edgeVao);
        glGenBuffers(1, &d.edgeVbo);
        glBindBuffer(GL_ARRAY_BUFFER, d.edgeVbo);
        glBufferData(GL_ARRAY_BUFFER, (long)(b.edge_pts.size() * sizeof(float)),
                     b.edge_pts.data(), GL_STREAM_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glGenBuffers(1, &d.edgeEbo);
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, d.edgeEbo);
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, (long)(lines.size() * sizeof(uint32_t)),
                     lines.data(), GL_STREAM_DRAW);
        d.edgeIdx = (int)lines.size();
    }
    glBindVertexArray(0);
}

bool validBody(const BodyMesh& b) {
    if (b.pos.empty() || b.idx.empty() || b.pos.size() % 3 != 0) return false;
    const uint32_t n = (uint32_t)(b.pos.size() / 3);
    return std::all_of(b.idx.begin(), b.idx.end(), [n](uint32_t i) { return i < n; });
}

}  // namespace

bool renderDocThumbnail(const DocMesh& doc, int w, int h, const DocThumbStyle& style,
                        unsigned int& tex, std::string& err) {
    w = std::clamp(w, 16, 2048);
    h = std::clamp(h, 16, 2048);
    SavedGl saved;
    if (!ensurePrograms(err)) return false;
    const int ssW = w * kSupersample, ssH = h * kSupersample;
    if (!ensureTargets(ssW, ssH, err)) return false;

    // ---- camera: renderer iso direction, ortho, fitted to the projected bbox ----
    std::vector<const BodyMesh*> bodies;
    bodies.reserve(doc.bodies.size());
    Vec3 lo(FLT_MAX, FLT_MAX, FLT_MAX), hi(-FLT_MAX, -FLT_MAX, -FLT_MAX);
    for (const auto& b : doc.bodies) {
        if (!validBody(b)) continue;
        bodies.push_back(&b);
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            lo.x = std::min(lo.x, b.pos[i]);     hi.x = std::max(hi.x, b.pos[i]);
            lo.y = std::min(lo.y, b.pos[i + 1]); hi.y = std::max(hi.y, b.pos[i + 1]);
            lo.z = std::min(lo.z, b.pos[i + 2]); hi.z = std::max(hi.z, b.pos[i + 2]);
        }
    }
    const float deg = 3.14159265f / 180.0f;
    const float yaw = style.yawDeg * deg, pitch = style.pitchDeg * deg;
    const Vec3 V = normalize(Vec3(std::cos(pitch) * std::sin(yaw),
                                  -std::cos(pitch) * std::cos(yaw), std::sin(pitch)));
    const Vec3 up = normalize(Vec3(-std::sin(pitch) * std::sin(yaw),
                                   std::sin(pitch) * std::cos(yaw), std::cos(pitch)));
    const Vec3 R = normalize(cross(up, V));
    const Vec3 U = normalize(cross(V, R));
    Mat4 mvp;
    Vec3 eye;
    if (!bodies.empty()) {
        const Vec3 c = (lo + hi) * 0.5f;
        const float radius = std::max(length(hi - lo) * 0.5f, 1e-4f);
        float minX = FLT_MAX, maxX = -FLT_MAX, minY = FLT_MAX, maxY = -FLT_MAX;
        for (int k = 0; k < 8; ++k) {
            const Vec3 p((k & 1) ? hi.x : lo.x, (k & 2) ? hi.y : lo.y, (k & 4) ? hi.z : lo.z);
            const Vec3 d = p - c;
            minX = std::min(minX, dot(d, R)); maxX = std::max(maxX, dot(d, R));
            minY = std::min(minY, dot(d, U)); maxY = std::max(maxY, dot(d, U));
        }
        const Vec3 target = c + R * (0.5f * (minX + maxX)) + U * (0.5f * (minY + maxY));
        constexpr float kMargin = 1.12f;
        float halfW = std::max(0.5f * (maxX - minX), radius * 0.02f) * kMargin;
        float halfH = std::max(0.5f * (maxY - minY), radius * 0.02f) * kMargin;
        const float aspect = (float)w / (float)h;
        if (halfW / halfH > aspect) halfH = halfW / aspect;
        else                        halfW = halfH * aspect;
        const float dist = radius * 3.0f;
        eye = target + V * dist;
        Mat4 proj = makeOrtho(-halfW, halfW, -halfH, halfH, dist - radius * 1.5f,
                              dist + radius * 1.5f);
        // Flip Y so row 0 of the texture is the image TOP (ImGui's default uv orientation).
        proj.m[1] = -proj.m[1]; proj.m[5] = -proj.m[5];
        proj.m[9] = -proj.m[9]; proj.m[13] = -proj.m[13];
        mvp = mul(proj, lookAt(eye, target, U));
    }

    // ---- draw into the supersampled target ----
    glBindFramebuffer(GL_FRAMEBUFFER, g.ssFbo);
    glViewport(0, 0, ssW, ssH);
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_BLEND);
    glDisable(GL_CULL_FACE);
    glDepthMask(GL_TRUE);
    glClearColor(style.bgMid[0], style.bgMid[1], style.bgMid[2], 1.0f);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

    glDisable(GL_DEPTH_TEST);
    glUseProgram(g.bgProg);
    glUniform3fv(g.uBgTop, 1, style.bgTop);
    glUniform3fv(g.uBgMid, 1, style.bgMid);
    glUniform3fv(g.uBgBot, 1, style.bgBot);
    glBindVertexArray(g.bgVao);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glEnable(GL_DEPTH_TEST);

    if (!bodies.empty()) {
        const Vec3 keyDir  = normalize(V + R * -0.45f + U * 0.85f);
        const Vec3 fillDir = normalize(V + R * 0.85f + U * 0.10f);
        const Vec3 rimDir  = normalize(V * -1.0f + U * 0.55f);
        std::vector<BodyGpu> gpu(bodies.size());
        for (size_t i = 0; i < bodies.size(); ++i) upload(*bodies[i], gpu[i]);

        glUseProgram(g.prog);
        glUniformMatrix4fv(g.uMvp, 1, GL_FALSE, mvp.m);
        glUniform3f(g.uKey, keyDir.x, keyDir.y, keyDir.z);
        glUniform3f(g.uFill, fillDir.x, fillDir.y, fillDir.z);
        glUniform3f(g.uRim, rimDir.x, rimDir.y, rimDir.z);
        glUniform3f(g.uCam, eye.x, eye.y, eye.z);
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(1.0f, 1.0f);
        glDepthFunc(GL_LEQUAL);   // authored regions redraw their faces over the body color
        for (size_t i = 0; i < bodies.size(); ++i) {
            const BodyMesh& b = *bodies[i];
            glBindVertexArray(gpu[i].vao);
            glUniform3fv(g.uColor, 1, b.color);
            glUniform1f(g.uMetal, b.metallic);
            glUniform1f(g.uRough, b.roughness);
            glDrawElements(GL_TRIANGLES, (int)b.idx.size(), GL_UNSIGNED_INT, (void*)0);
            const uint32_t nTris = (uint32_t)(b.idx.size() / 3);
            for (const auto& reg : b.appearance_regions) {
                if (reg.face_count == 0) continue;
                glUniform3fv(g.uColor, 1, reg.color);
                glUniform1f(g.uMetal, reg.metallic);
                glUniform1f(g.uRough, reg.roughness);
                const uint32_t endFace = reg.first_face + reg.face_count;
                for (const auto& f : b.faces) {
                    if (f.face_id < reg.first_face || f.face_id >= endFace || f.tri_count == 0 ||
                        f.first_tri + f.tri_count > nTris) continue;
                    glDrawElements(GL_TRIANGLES, (int)(f.tri_count * 3), GL_UNSIGNED_INT,
                                   (void*)((uintptr_t)f.first_tri * 3 * sizeof(uint32_t)));
                }
            }
        }
        glDisable(GL_POLYGON_OFFSET_FILL);

        glUseProgram(g.edgeProg);
        glUniformMatrix4fv(g.uEdgeMvp, 1, GL_FALSE, mvp.m);
        glUniform3fv(g.uEdgeColor, 1, style.edge);
        for (const auto& d : gpu) {
            if (!d.edgeVao) continue;
            glBindVertexArray(d.edgeVao);
            glDrawElements(GL_LINES, d.edgeIdx, GL_UNSIGNED_INT, (void*)0);
        }
        glBindVertexArray(0);
    }

    // ---- box-resolve into the caller's texture ----
    const bool fresh = tex == 0;
    if (fresh) glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glBindFramebuffer(GL_FRAMEBUFFER, g.dstFbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
        if (fresh) deleteDocThumbnail(tex);
        err = "preview texture incomplete";
        return false;
    }
    glBindFramebuffer(GL_READ_FRAMEBUFFER, g.ssFbo);
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, g.dstFbo);
    glBlitFramebuffer(0, 0, ssW, ssH, 0, 0, w, h, GL_COLOR_BUFFER_BIT, GL_LINEAR);
    // Detach so the texture is never simultaneously a render target and an ImGui sample source.
    glBindFramebuffer(GL_FRAMEBUFFER, g.dstFbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
    return true;
}

void deleteDocThumbnail(unsigned int& tex) {
    if (tex) glDeleteTextures(1, &tex);
    tex = 0;
}

}  // namespace render
