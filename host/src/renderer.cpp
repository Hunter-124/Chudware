#include "renderer.h"

#include <glad/gl.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <vector>

#include "math.h"

namespace {
const char* kVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
layout(location=2) in float aScalar;   // C19: per-vertex scalar (NaN = no data)
uniform mat4 uMVP;
out vec3 vN;
out vec3 vPos;
out float vScalar;
void main(){
    vN = aNormal;
    vPos = aPos;
    vScalar = aScalar;
    gl_Position = uMVP * vec4(aPos, 1.0);
})";

// Studio three-point lighting: warm key, cool fill, rim from behind, plus a hemisphere
// ambient (sky above, ground bounce below). Shading is done in linear space on the
// linearized part color and gamma-encoded on output, so viewport colors track the
// engine's contact-sheet palette.
const char* kFrag = R"(#version 330 core
in vec3 vN;
in vec3 vPos;
in float vScalar;
uniform vec3 uKeyDir;     // toward the key light (world)
uniform vec3 uFillDir;    // toward the fill light
uniform vec3 uRimDir;     // toward the rim light
uniform vec3 uCamPos;
uniform vec3 uColor;      // sRGB part color
uniform int  uUseScalar;  // C19: 1 = sample the LUT by the per-vertex scalar
uniform float uScalarMin, uScalarMax;
uniform sampler2D uLut;   // 256x1 jet ramp
uniform int   uClipCount;              // C28-C: 0 = off; 1..3 active planes
uniform vec3  uClipN[3];               // plane normals
uniform float uClipOff[3];            // plane offsets
uniform float uRealView;               // C30-C: 0 = off, 1 = environment reflections on
uniform float uMetallic;               // C32-A: 0 = dielectric, 1 = metal
uniform float uRoughness;              // C32-A: 0 = mirror, 1 = matte
out vec4 frag;

// C30-C: procedural studio environment sampled by a reflected direction R (Z-up world).
// Vertical gradient (near-white sky overhead, neutral grey at the horizon band, a soft darker
// floor below) plus a few elongated "softbox" lobes — the classic RealView look that makes a
// surface read as real material instead of flat plastic. Pure math, no textures.
vec3 env(vec3 R){
    float up = R.z;                                  // -1 (down) .. +1 (up)
    vec3 sky   = vec3(0.97, 0.98, 1.00);
    vec3 horiz = vec3(0.66, 0.68, 0.72);
    vec3 floorC= vec3(0.30, 0.30, 0.32);
    vec3 c = (up >= 0.0) ? mix(horiz, sky, smoothstep(0.0, 0.55, up))
                         : mix(horiz, floorC, smoothstep(0.0, -0.45, up));
    // Soft elongated softbox highlights at fixed studio directions (wide in azimuth, tight in tilt).
    vec3 d1 = normalize(vec3(-0.35,  0.30, 0.88));   // big overhead-left key softbox
    vec3 d2 = normalize(vec3( 0.62,  0.10, 0.55));   // right fill softbox
    float l1 = smoothstep(0.62, 0.985, dot(R, d1));
    float l2 = smoothstep(0.70, 0.990, dot(R, d2)) * 0.6;
    c += vec3(l1 + l2);                               // additive bright lobes (clamped on mix)
    return c;
}

void main(){
    for (int ci = 0; ci < uClipCount; ++ci)
        if (dot(vPos, uClipN[ci]) > uClipOff[ci]) discard;
    vec3 N = normalize(vN);
    vec3 V = normalize(uCamPos - vPos);
    if (dot(N, V) < 0.0) N = -N;            // two-sided
    vec3 srgbColor = uColor;
    if (uUseScalar == 1) {
        if (vScalar != vScalar) {           // NaN = no data -> neutral grey
            srgbColor = vec3(0.62, 0.64, 0.66);
        } else {
            float t = clamp((vScalar - uScalarMin) / max(uScalarMax - uScalarMin, 1e-9), 0.0, 1.0);
            srgbColor = texture(uLut, vec2(t, 0.5)).rgb;
        }
    }
    vec3 base = pow(srgbColor, vec3(2.2));  // sRGB -> linear

    // Tuned for a near-white studio background: bright neutral hemisphere ambient dominates,
    // a soft key adds top-down shaping, fill lifts the shadow side, rim is nearly off (halos
    // read wrong on a light bg), and specular is a small neutral highlight.
    const vec3 keyCol  = vec3(1.00, 0.99, 0.97);
    const vec3 fillCol = vec3(0.85, 0.88, 0.95);
    const vec3 rimCol  = vec3(0.90, 0.93, 1.00);
    const vec3 skyCol  = vec3(0.92, 0.94, 0.98);   // near-white sky bounce
    const vec3 gndCol  = vec3(0.34, 0.33, 0.31);   // warm-grey ground bounce

    float key  = max(dot(N, normalize(uKeyDir)),  0.0);
    float fill = max(dot(N, normalize(uFillDir)), 0.0);
    float rim  = max(dot(N, normalize(uRimDir)),  0.0);
    float hemi = N.z * 0.5 + 0.5;            // up-facing picks up sky, down-facing ground

    vec3 diffuse = keyCol * (key * 0.50) + fillCol * (fill * 0.18);
    vec3 ambient = mix(gndCol, skyCol, hemi) * 0.55;
    float edge = pow(1.0 - max(dot(N, V), 0.0), 2.0);
    vec3 rimLight = rimCol * (rim * edge * 0.10);

    // C32-A: a metal absorbs most of its diffuse term (the body color survives mainly as the tint of
    // the environment reflection below). Dielectric (metallic 0) keeps the full diffuse = C30 look.
    float diffK = 1.0 - uMetallic * 0.7;

    // C32-A: specular exponent narrows for a polished surface, widens for a rough one. Anchored so
    // roughness 0.5 == the C30 dielectric highlight (exp 56): mix(100,12,0.5) = 56, weight 0.13.
    vec3 H = normalize(normalize(uKeyDir) + V);
    float specExp = mix(100.0, 12.0, uRoughness);
    float spec = pow(max(dot(N, H), 0.0), specExp) * 0.13;

    vec3 c = base * ((diffuse + ambient) * diffK) + rimLight + keyCol * spec;

    // C30-C + C32-A: RealView environment reflection. Schlick fresnel weights a procedural studio
    // reflection. F0 lifts from the dielectric 0.04 toward a body-color-tinted 0.6 with metallic, and
    // the strength k climbs from 0.42 (plastic sheen) to ~0.8 (clear metal). Roughness fakes a blur
    // by mixing the sharp env(R) toward the hemisphere ambient. Gated off for the FEA colormap path.
    if (uRealView > 0.5 && uUseScalar == 0) {
        vec3 R = reflect(-V, N);
        float fres = 0.04 + 0.96 * pow(1.0 - max(dot(N, V), 0.0), 5.0);
        vec3 F0   = mix(vec3(0.04), base * 0.6 + 0.4 * uMetallic, uMetallic);
        vec3 F    = F0 + (1.0 - F0) * pow(1.0 - max(dot(N, V), 0.0), 5.0);
        vec3 fresV = mix(vec3(fres), F, uMetallic);   // dielectric -> scalar Schlick (C30 identity)
        float k   = mix(0.42, 0.80, uMetallic);
        vec3 envSharp = pow(env(R), vec3(2.2));
        vec3 envAmb   = pow(mix(gndCol, skyCol, hemi), vec3(2.2));  // matte = diffuse env, no mirror
        // Blur is anchored at roughness 0.5 (the C30 plastic baseline = sharp env, no blur): only
        // rougher-than-0.5 surfaces mix toward the ambient env, so the dielectric default is exact.
        float blur = clamp((uRoughness - 0.5) * 2.0, 0.0, 1.0) * 0.7;
        vec3 envRefl  = mix(envSharp, envAmb, blur);
        c += envRefl * fresV * k;
    }
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

// 3-stop vertical gradient: bottom floor glow -> cool mid band (~55% up) -> bright top.
const char* kBgFrag = R"(#version 330 core
in float vT;
uniform vec3 uTop;
uniform vec3 uMid;
uniform vec3 uBot;
out vec4 frag;
void main(){
    float t = clamp(vT * 0.5 + 0.5, 0.0, 1.0);   // 0 = bottom, 1 = top
    const float midPos = 0.55;
    vec3 c;
    if (t < midPos) c = mix(uBot, uMid, smoothstep(0.0, midPos, t));
    else            c = mix(uMid, uTop, smoothstep(midPos, 1.0, t));
    frag = vec4(c, 1.0);
})";

// Soft elliptical contact shadow on a virtual ground plane. The quad carries a planar uv in
// [-1,1]; alpha falls off radially with a smooth shoulder so it reads as a diffuse blob.
const char* kShadowVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec2 aUV;
uniform mat4 uMVP;
out vec2 vUV;
void main(){ vUV = aUV; gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kShadowFrag = R"(#version 330 core
in vec2 vUV;
uniform vec3 uColor;
uniform float uMaxAlpha;
out vec4 frag;
void main(){
    float r = length(vUV);
    float a = uMaxAlpha * (1.0 - smoothstep(0.45, 1.0, r));
    frag = vec4(uColor, a);
})";

const char* kEdgeVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
out vec3 vPos;
void main(){ vPos = aPos; gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kEdgeFrag = R"(#version 330 core
in vec3 vPos;
uniform vec3 uColor;
uniform int   uClipCount;
uniform vec3  uClipN[3];
uniform float uClipOff[3];
out vec4 frag;
void main(){
    for (int ci = 0; ci < uClipCount; ++ci)
        if (dot(vPos, uClipN[ci]) > uClipOff[ci]) discard;
    frag = vec4(uColor, 1.0);
})";

// Reference-plane shader: a flat RGBA quad (uColor carries alpha for translucency).
const char* kPlaneVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
void main(){ gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kPlaneFrag = R"(#version 330 core
uniform vec4 uColor;
out vec4 frag;
void main(){ frag = uColor; })";

// C28-C: section-cap quad. Flat-lit by its plane normal. Also clipped by the OTHER active planes
// (cross-plane clipping) so intersecting cuts read as solid material at their intersection line.
// uCapExclude is the index of THIS cap's plane — that plane is skipped in the discard loop.
const char* kCapVert = R"(#version 330 core
layout(location=0) in vec3 aPos;
uniform mat4 uMVP;
out vec3 vPos;
void main(){ vPos = aPos; gl_Position = uMVP * vec4(aPos, 1.0); })";

const char* kCapFrag = R"(#version 330 core
in vec3 vPos;
uniform vec3  uColor;
uniform vec3  uN;           // this cap's plane normal
uniform vec3  uKeyDir;
uniform int   uClipCount;   // total active planes
uniform vec3  uClipN[3];    // all plane normals
uniform float uClipOff[3];  // all plane offsets
uniform int   uCapExclude;  // index of THIS cap's plane (not re-applied)
out vec4 frag;
void main(){
    for (int ci = 0; ci < uClipCount; ++ci)
        if (ci != uCapExclude && dot(vPos, uClipN[ci]) > uClipOff[ci]) discard;
    vec3 N = normalize(uN);
    vec3 base = pow(uColor, vec3(2.2));
    float key = max(dot(N, normalize(uKeyDir)), 0.0);
    float amb = 0.62;
    vec3 c = base * (amb + 0.45 * key);
    c = pow(max(c, 0.0), vec3(1.0 / 2.2));
    frag = vec4(c, 1.0);
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
    // C27-D: View Selector preview thumbnail target (lazily created; may be 0).
    if (thumbFbo_) glDeleteFramebuffers(1, &thumbFbo_);
    if (thumbTex_) glDeleteTextures(1, &thumbTex_);
    if (thumbDepth_) glDeleteRenderbuffers(1, &thumbDepth_);
    if (prog_) glDeleteProgram(prog_);
    if (edgeProg_) glDeleteProgram(edgeProg_);
    if (bgProg_) glDeleteProgram(bgProg_);
    if (shadowProg_) glDeleteProgram(shadowProg_);
    if (planeProg_) glDeleteProgram(planeProg_);
    if (planeVbo_) glDeleteBuffers(1, &planeVbo_);
    if (planeVao_) glDeleteVertexArrays(1, &planeVao_);
    if (capProg_) glDeleteProgram(capProg_);
    if (capVbo_) glDeleteBuffers(1, &capVbo_);
    if (capVao_) glDeleteVertexArrays(1, &capVao_);
    if (lutTex_) glDeleteTextures(1, &lutTex_);
}

// SolidWorks-style "jet/rainbow" result ramp: blue -> cyan -> green -> yellow -> red. Built once
// into a 256x1 RGB texture sampled by the shader's normalized scalar t in [0,1].
void Renderer::buildLut() {
    if (lutTex_) return;
    const int N = 256;
    std::vector<unsigned char> ramp(N * 3);
    // Control stops (t, r, g, b) in sRGB 0..1.
    struct Stop { float t, r, g, b; };
    static const Stop stops[] = {
        {0.00f, 0.00f, 0.10f, 0.55f},  // deep blue
        {0.25f, 0.00f, 0.70f, 0.90f},  // cyan
        {0.50f, 0.10f, 0.80f, 0.20f},  // green
        {0.75f, 0.95f, 0.85f, 0.00f},  // yellow
        {1.00f, 0.85f, 0.10f, 0.10f},  // red
    };
    const int NS = (int)(sizeof(stops) / sizeof(stops[0]));
    for (int i = 0; i < N; ++i) {
        float t = (float)i / (float)(N - 1);
        int k = 0;
        while (k < NS - 2 && t > stops[k + 1].t) ++k;
        float span = std::max(1e-6f, stops[k + 1].t - stops[k].t);
        float f = (t - stops[k].t) / span;
        float r = stops[k].r + (stops[k + 1].r - stops[k].r) * f;
        float g = stops[k].g + (stops[k + 1].g - stops[k].g) * f;
        float b = stops[k].b + (stops[k + 1].b - stops[k].b) * f;
        ramp[i * 3 + 0] = (unsigned char)(r * 255.0f + 0.5f);
        ramp[i * 3 + 1] = (unsigned char)(g * 255.0f + 0.5f);
        ramp[i * 3 + 2] = (unsigned char)(b * 255.0f + 0.5f);
    }
    glGenTextures(1, &lutTex_);
    glBindTexture(GL_TEXTURE_2D, lutTex_);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, N, 1, 0, GL_RGB, GL_UNSIGNED_BYTE, ramp.data());
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glBindTexture(GL_TEXTURE_2D, 0);
}

void Renderer::setBackground(float tr, float tg, float tb, float br, float bg, float bb) {
    bgTop_[0] = tr; bgTop_[1] = tg; bgTop_[2] = tb;
    bgBot_[0] = br; bgBot_[1] = bg; bgBot_[2] = bb;
    // 2-stop convenience: synthesize a mid stop halfway between top and bottom.
    for (int i = 0; i < 3; ++i) bgMid_[i] = 0.5f * (bgTop_[i] + bgBot_[i]);
}

void Renderer::setBackground3(const float top[3], const float mid[3], const float bot[3]) {
    for (int i = 0; i < 3; ++i) { bgTop_[i] = top[i]; bgMid_[i] = mid[i]; bgBot_[i] = bot[i]; }
}

void Renderer::setEdgeStyle(float r, float g, float b, float widthPx) {
    edgeColor_[0] = r; edgeColor_[1] = g; edgeColor_[2] = b;
    edgeWidthPx_ = widthPx;
}

void Renderer::setDefaultPartColor(float r, float g, float b) {
    defaultPart_[0] = r; defaultPart_[1] = g; defaultPart_[2] = b;
}

void Renderer::setGroundShadow(float r, float g, float b, float maxAlpha) {
    groundShadow_[0] = r; groundShadow_[1] = g; groundShadow_[2] = b; groundShadow_[3] = maxAlpha;
}

bool Renderer::init(int width, int height, std::string& err) {
    w_ = width; h_ = height;
    sw_ = w_ * SS; sh_ = h_ * SS;

    prog_ = linkProgram(kVert, kFrag);
    edgeProg_ = linkProgram(kEdgeVert, kEdgeFrag);
    bgProg_ = linkProgram(kBgVert, kBgFrag);
    shadowProg_ = linkProgram(kShadowVert, kShadowFrag);
    planeProg_ = linkProgram(kPlaneVert, kPlaneFrag);
    capProg_ = linkProgram(kCapVert, kCapFrag);
    int ok = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &ok);
    if (!ok) { err = "program link failed"; return false; }

    glGenVertexArrays(1, &bgVao_);

    // Dynamic buffer for reference-plane quads (6 verts/quad; re-filled per plane per frame).
    glGenVertexArrays(1, &planeVao_);
    glGenBuffers(1, &planeVbo_);
    glBindVertexArray(planeVao_);
    glBindBuffer(GL_ARRAY_BUFFER, planeVbo_);
    glBufferData(GL_ARRAY_BUFFER, sizeof(float) * 3 * 6, nullptr, GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    glBindVertexArray(0);

    // W3-I: dynamic buffer for the section-cap quad (6 verts, refilled per frame).
    glGenVertexArrays(1, &capVao_);
    glGenBuffers(1, &capVbo_);
    glBindVertexArray(capVao_);
    glBindBuffer(GL_ARRAY_BUFFER, capVbo_);
    glBufferData(GL_ARRAY_BUFFER, sizeof(float) * 3 * 6, nullptr, GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    glBindVertexArray(0);

    buildLut();  // C19 result colormap ramp
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

    // W3-I: packed depth+stencil so the section cap can be stencil-masked.
    glBindRenderbuffer(GL_RENDERBUFFER, depthRbo_);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH24_STENCIL8, sw_, sh_);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, colorTex_, 0);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_RENDERBUFFER,
                              depthRbo_);
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
        if (s.vboScalar) glDeleteBuffers(1, &s.vboScalar);
    }
    subs_.clear();
}

// Re-upload a Sub's positions (and edge lines) as base + deformScale_ * disp. No-op if the body
// carries no displacement field.
void Renderer::uploadDeformed(Sub& s) {
    if (s.disp.empty() || s.disp.size() != s.basePos.size() || s.vboPos == 0) return;
    std::vector<float> p(s.basePos.size());
    for (size_t i = 0; i < p.size(); ++i) p[i] = s.basePos[i] + deformScale_ * s.disp[i];
    glBindBuffer(GL_ARRAY_BUFFER, s.vboPos);
    glBufferSubData(GL_ARRAY_BUFFER, 0, p.size() * sizeof(float), p.data());
    glBindBuffer(GL_ARRAY_BUFFER, 0);
    // Edge lines: displace each endpoint by the nearest base vertex's displacement. Approximate
    // (edges are a small line list) — snap each edge endpoint to its matching base vertex by exact
    // position match would be costly; instead we leave undisplaced edges, which reads fine since
    // the deformed surface dominates the view. (Edges stay on the undeformed cage as a reference.)
}

void Renderer::setDeformScale(float sc) {
    deformScale_ = sc;
    for (auto& s : subs_) uploadDeformed(s);
}

bool Renderer::scalarRange(float& vmin, float& vmax) const {
    for (const auto& s : subs_) {
        if (s.hasScalar) { vmin = s.scalarMin; vmax = s.scalarMax; return true; }
    }
    return false;
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
        // A body left at the PartMesh default color {0.62,0.70,0.80} is treated as "no explicit
        // color" and falls back to the theme default part color (palette.partDefault).
        bool unset = part.color[0] == 0.62f && part.color[1] == 0.70f && part.color[2] == 0.80f;
        s.color[0] = unset ? defaultPart_[0] : part.color[0];
        s.color[1] = unset ? defaultPart_[1] : part.color[1];
        s.color[2] = unset ? defaultPart_[2] : part.color[2];
        s.metallic = part.metallic;     // C32-A RealView material
        s.roughness = part.roughness;
        // C19: keep CPU copies so the deform slider can re-upload displaced positions.
        s.basePos = m.positions;
        s.baseEdge = m.edgeLines;
        s.disp = part.disp;  // 3*n or empty

        glGenVertexArrays(1, &s.vao);
        glGenBuffers(1, &s.vboPos);
        glGenBuffers(1, &s.vboNrm);
        glGenBuffers(1, &s.ebo);
        glBindVertexArray(s.vao);
        glBindBuffer(GL_ARRAY_BUFFER, s.vboPos);
        glBufferData(GL_ARRAY_BUFFER, m.positions.size() * sizeof(float), m.positions.data(),
                     GL_DYNAMIC_DRAW);  // dynamic: deform re-uploads
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindBuffer(GL_ARRAY_BUFFER, s.vboNrm);
        glBufferData(GL_ARRAY_BUFFER, m.normals.size() * sizeof(float), m.normals.data(),
                     GL_STATIC_DRAW);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);

        // C19: per-vertex scalar attribute (location 2). Always bound so the VAO is complete; a
        // body without a field uploads a NaN-filled buffer (shader renders neutral). When colormap
        // mode is off the shader ignores it entirely.
        glGenBuffers(1, &s.vboScalar);
        glBindBuffer(GL_ARRAY_BUFFER, s.vboScalar);
        size_t nverts = m.positions.size() / 3;
        if (!part.scalar.empty() && part.scalar.size() == nverts) {
            glBufferData(GL_ARRAY_BUFFER, part.scalar.size() * sizeof(float), part.scalar.data(),
                         GL_STATIC_DRAW);
            s.hasScalar = true;
            s.scalarMin = part.scalarMin;
            s.scalarMax = part.scalarMax;
        } else {
            std::vector<float> nans(nverts, std::numeric_limits<float>::quiet_NaN());
            glBufferData(GL_ARRAY_BUFFER, nans.size() * sizeof(float), nans.data(), GL_STATIC_DRAW);
            s.hasScalar = false;
        }
        glEnableVertexAttribArray(2);
        glVertexAttribPointer(2, 1, GL_FLOAT, GL_FALSE, sizeof(float), (void*)0);

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
                     GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
        glBindVertexArray(0);

        s.idxCount = static_cast<int>(m.indices.size());
        s.edgeVerts = static_cast<int>(m.edgeLines.size() / 3);
        subs_.push_back(std::move(s));

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
    } else {
        // C34: cleared to an EMPTY scene — reset the frame so the empty-doc fallback
        // (kEmptySceneRadius camera + datum planes) takes over instead of a stale bbox.
        center_ = Vec3(0, 0, 0);
        radius_ = 0.0f;
    }
    // C19: apply any standing deformation scale to the freshly uploaded bodies.
    if (deformScale_ != 0.0f)
        for (auto& s : subs_) uploadDeformed(s);

    // C27-D: invalidate the View Selector thumbnail cache — the meshes changed.
    ++meshesRevision_;
}

// C25: world radius used when the document has no geometry yet (a fresh "new part") —
// keeps the camera, datum planes, and label anchors at a sensible scale. ~35mm reads like
// SolidWorks's default plane size.
static constexpr float kEmptySceneRadius = 35.0f;

void Renderer::drawCurrent(float yawDeg, float pitchDeg, float zoom) {
    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glViewport(0, 0, sw_, sh_);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT | GL_STENCIL_BUFFER_BIT);

    // gradient studio background (fullscreen triangle, depth-independent)
    glDisable(GL_DEPTH_TEST);
    glUseProgram(bgProg_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uTop"), 1, bgTop_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uMid"), 1, bgMid_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uBot"), 1, bgBot_);
    glBindVertexArray(bgVao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindVertexArray(0);
    glEnable(GL_DEPTH_TEST);

    // C25: an EMPTY document still has a camera, datum planes, and overlay anchors (the
    // SolidWorks "new part" starting state) — only the geometry passes need meshes (their
    // loops over subs_ are natural no-ops). Previously this returned right here, leaving
    // cam_ stale: plane labels projected from garbage and the planes never drew.
    const float sceneR = (radius_ > 0.0f) ? radius_ : kEmptySceneRadius;

    const float deg = 3.14159265f / 180.0f;
    float yaw = yawDeg * deg, pitch = pitchDeg * deg;
    Vec3 dir(std::cos(pitch) * std::sin(yaw), -std::cos(pitch) * std::cos(yaw), std::sin(pitch));
    float fov = kFovDeg * deg;
    float dist = sceneR / std::sin(fov * 0.5f) * 1.05f / std::max(0.05f, zoom);
    // W2-D: pan shifts the orbit target (and therefore the eye) in world space.
    Vec3 target = center_ + pan_;
    Vec3 eye = target + dir * dist;
    Vec3 up(0, 0, 1);
    if (std::fabs(pitch) > 89.0f * deg) up = Vec3(0, 1, 0);  // avoid gimbal at top view

    Mat4 proj = perspective(fov, (float)sw_ / (float)sh_, dist * 0.01f, dist * 10.0f);
    Mat4 view = lookAt(eye, target, up);
    Mat4 mvp = mul(proj, view);

    // Publish the transform so the ID-buffer pass and overlays (C15) align to this exact frame.
    cam_.mvp = mvp;
    cam_.eye = eye;
    cam_.center = target;
    cam_.radius = sceneR;
    cam_.ssWidth = sw_;
    cam_.ssHeight = sh_;

    // Soft ground contact shadow under the model (after bg, under geometry).
    drawGroundShadow(mvp, eye, up);

    // Camera-relative studio rig: soft key from the upper-left, fill from the right; the rim is
    // nearly off (tuned for the near-white studio bg where halos read wrong).
    Vec3 V = normalize(dir);                       // model -> camera
    Vec3 R = normalize(cross(up, V));
    Vec3 U = normalize(cross(V, R));
    Vec3 keyDir = normalize(V + R * -0.45f + U * 0.85f);
    Vec3 fillDir = normalize(V + R * 0.85f + U * 0.10f);
    Vec3 rimDir = normalize(V * -1.0f + U * 0.55f);

    // Display-style flags.
    const DisplayStyle ds = displayStyle_;
    const bool drawFaces = (ds != DisplayStyle::Wireframe);
    const bool hlr = (ds == DisplayStyle::HiddenLinesRemoved ||
                      ds == DisplayStyle::HiddenLinesVisible);
    const bool drawEdges = (ds != DisplayStyle::Shaded);

    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3f(glGetUniformLocation(prog_, "uKeyDir"), keyDir.x, keyDir.y, keyDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uFillDir"), fillDir.x, fillDir.y, fillDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uRimDir"), rimDir.x, rimDir.y, rimDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uCamPos"), eye.x, eye.y, eye.z);
    int colorLoc = glGetUniformLocation(prog_, "uColor");
    int useScalarLoc = glGetUniformLocation(prog_, "uUseScalar");
    int sminLoc = glGetUniformLocation(prog_, "uScalarMin");
    int smaxLoc = glGetUniformLocation(prog_, "uScalarMax");
    int metallicLoc = glGetUniformLocation(prog_, "uMetallic");   // C32-A RealView material
    int roughnessLoc = glGetUniformLocation(prog_, "uRoughness");
    // Bind the colormap LUT to texture unit 0.
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, lutTex_);
    glUniform1i(glGetUniformLocation(prog_, "uLut"), 0);
    // C28-C: multi-plane section clip (faces). Push all active planes as arrays.
    {
        int cnt = section_.count;
        glUniform1i(glGetUniformLocation(prog_, "uClipCount"), cnt);
        if (cnt > 0) {
            glUniform3fv(glGetUniformLocation(prog_, "uClipN"),   cnt, section_.normals[0]);
            glUniform1fv(glGetUniformLocation(prog_, "uClipOff"), cnt, section_.offsets);
        }
    }
    glUniform1f(glGetUniformLocation(prog_, "uRealView"), realView_ ? 1.0f : 0.0f);  // C30-C

    // Solids, pushed back a touch so the feature edges win the depth test on top of them.
    // In HLR styles the faces are painted in the background color so they only occlude (depth) —
    // the classic hidden-line look. (Wireframe skips faces entirely.)
    if (drawFaces) {
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(1.0f, 1.0f);
        if (hlr) glUniform1i(useScalarLoc, 0);
        for (const auto& s : subs_) {
            bool useScalar = !hlr && colormap_ && s.hasScalar;
            glUniform1i(useScalarLoc, useScalar ? 1 : 0);
            if (useScalar) {
                glUniform1f(sminLoc, s.scalarMin);
                glUniform1f(smaxLoc, s.scalarMax);
            }
            if (hlr) glUniform3fv(colorLoc, 1, bgMid_);
            else     glUniform3f(colorLoc, s.color[0], s.color[1], s.color[2]);
            glUniform1f(metallicLoc, s.metallic);    // C32-A per-body RealView material
            glUniform1f(roughnessLoc, s.roughness);
            glBindVertexArray(s.vao);
            glDrawElements(GL_TRIANGLES, s.idxCount, GL_UNSIGNED_INT, 0);
        }
        glBindVertexArray(0);
        glDisable(GL_POLYGON_OFFSET_FILL);
    }

    // C28-C: filled section caps (stencil-masked per plane). One cap pass per active plane;
    // skipped in Wireframe (SW does the same).
    if (section_.count > 0 && section_.showCap && drawFaces) {
        capKeyDir_ = keyDir;
        for (int pi = 0; pi < section_.count; ++pi) drawSectionCap(mvp, pi);
    }

    if (drawEdges) {
        glUseProgram(edgeProg_);
        glUniformMatrix4fv(glGetUniformLocation(edgeProg_, "uMVP"), 1, GL_FALSE, mvp.m);
        int eColorLoc = glGetUniformLocation(edgeProg_, "uColor");
        // C28-C: multi-plane clip for edges.
        {
            int cnt = section_.count;
            glUniform1i(glGetUniformLocation(edgeProg_, "uClipCount"), cnt);
            if (cnt > 0) {
                glUniform3fv(glGetUniformLocation(edgeProg_, "uClipN"),   cnt, section_.normals[0]);
                glUniform1fv(glGetUniformLocation(edgeProg_, "uClipOff"), cnt, section_.offsets);
            }
        }
        glLineWidth(edgeWidthPx_ * (float)SS);  // screen-space width after downsample

        // HiddenLinesVisible: first draw occluded edges dim (depth GL_GREATER), then visible edges.
        if (ds == DisplayStyle::HiddenLinesVisible) {
            glDepthFunc(GL_GREATER);
            glUniform3f(eColorLoc, 0.62f, 0.65f, 0.70f);  // dim grey for hidden edges
            for (const auto& s : subs_) {
                if (s.edgeVerts <= 0) continue;
                glBindVertexArray(s.edgeVao);
                glDrawArrays(GL_LINES, 0, s.edgeVerts);
            }
            glDepthFunc(GL_LESS);
        }

        glUniform3fv(eColorLoc, 1, edgeColor_);
        for (const auto& s : subs_) {
            if (s.edgeVerts <= 0) continue;
            glBindVertexArray(s.edgeVao);
            glDrawArrays(GL_LINES, 0, s.edgeVerts);
        }
        glBindVertexArray(0);
    }

    // ---- translucent reference planes (after the opaque pass so they test against geometry) ----
    drawReferencePlanes(mvp);

    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

// C27-D: render the current meshes from a candidate orientation into a small dedicated FBO and
// return its color-texture id (for the View Selector hover preview). No supersampling, no ground
// shadow / section / datum planes — just the lit solids + feature edges over a solid light bg, so
// the candidate view reads at a glance. Cheap: a (yaw,pitch,meshes-revision) cache skips the
// re-render when nothing changed. CRITICALLY restores every GL state the main pass depends on:
// the bound framebuffer (0), the full-window viewport is re-set by the next drawCurrent() (which
// always binds fbo_ + sets glViewport), but we still leave GL in a clean, predictable state here.
unsigned int Renderer::renderThumbnail(float yawDeg, float pitchDeg, int px) {
    if (px < 16) px = 16;

    // Lazily (re)allocate the dedicated thumb target when missing or the size changed.
    if (thumbPx_ != px || thumbFbo_ == 0) {
        if (!thumbFbo_)   glGenFramebuffers(1, &thumbFbo_);
        if (!thumbTex_)   glGenTextures(1, &thumbTex_);
        if (!thumbDepth_) glGenRenderbuffers(1, &thumbDepth_);
        glBindTexture(GL_TEXTURE_2D, thumbTex_);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, px, px, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glBindTexture(GL_TEXTURE_2D, 0);
        glBindRenderbuffer(GL_RENDERBUFFER, thumbDepth_);
        glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, px, px);
        glBindRenderbuffer(GL_RENDERBUFFER, 0);
        glBindFramebuffer(GL_FRAMEBUFFER, thumbFbo_);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, thumbTex_, 0);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, thumbDepth_);
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        thumbPx_ = px;
        thumbValid_ = false;  // size change forces a re-render below
    }

    // Cache: re-use the existing texture if the orientation and meshes are unchanged.
    if (thumbValid_ && thumbMeshRev_ == meshesRevision_ &&
        thumbYaw_ == yawDeg && thumbPitch_ == pitchDeg) {
        return thumbTex_;
    }

    // ---- render the thumbnail ----
    glBindFramebuffer(GL_FRAMEBUFFER, thumbFbo_);
    glViewport(0, 0, px, px);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LESS);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

    // Solid light studio background (single fill via the gradient shader with all three stops equal
    // — avoids a separate clear-color path and keeps the same near-white look as the main view).
    glDisable(GL_DEPTH_TEST);
    glUseProgram(bgProg_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uTop"), 1, bgTop_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uMid"), 1, bgMid_);
    glUniform3fv(glGetUniformLocation(bgProg_, "uBot"), 1, bgBot_);
    glBindVertexArray(bgVao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindVertexArray(0);
    glEnable(GL_DEPTH_TEST);

    const float sceneR = (radius_ > 0.0f) ? radius_ : kEmptySceneRadius;
    const float deg = 3.14159265f / 180.0f;
    float yaw = yawDeg * deg, pitch = pitchDeg * deg;
    Vec3 dir(std::cos(pitch) * std::sin(yaw), -std::cos(pitch) * std::cos(yaw), std::sin(pitch));
    float fov = kFovDeg * deg;
    float dist = sceneR / std::sin(fov * 0.5f) * 1.05f;  // zoom=1 fit, no pan
    Vec3 target = center_;
    Vec3 eye = target + dir * dist;
    Vec3 up(0, 0, 1);
    if (std::fabs(pitch) > 89.0f * deg) up = Vec3(0, 1, 0);  // avoid gimbal at top/bottom

    Mat4 proj = perspective(fov, 1.0f, dist * 0.01f, dist * 10.0f);  // square aspect
    Mat4 view = lookAt(eye, target, up);
    Mat4 mvp = mul(proj, view);

    // Camera-relative studio rig (same as drawCurrent so the thumbnail shades identically).
    Vec3 V = normalize(dir);
    Vec3 R = normalize(cross(up, V));
    Vec3 U = normalize(cross(V, R));
    Vec3 keyDir = normalize(V + R * -0.45f + U * 0.85f);
    Vec3 fillDir = normalize(V + R * 0.85f + U * 0.10f);
    Vec3 rimDir = normalize(V * -1.0f + U * 0.55f);

    // Solids (flat part colors — skip the colormap LUT path for clarity; section clip off).
    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3f(glGetUniformLocation(prog_, "uKeyDir"), keyDir.x, keyDir.y, keyDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uFillDir"), fillDir.x, fillDir.y, fillDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uRimDir"), rimDir.x, rimDir.y, rimDir.z);
    glUniform3f(glGetUniformLocation(prog_, "uCamPos"), eye.x, eye.y, eye.z);
    glUniform1i(glGetUniformLocation(prog_, "uUseScalar"), 0);
    glUniform1i(glGetUniformLocation(prog_, "uClipCount"), 0);  // thumbnail: no section clip
    glUniform1f(glGetUniformLocation(prog_, "uRealView"), 1.0f);  // C30-C: reflections on in previews
    int colorLoc = glGetUniformLocation(prog_, "uColor");
    int metallicLoc = glGetUniformLocation(prog_, "uMetallic");   // C32-A RealView material
    int roughnessLoc = glGetUniformLocation(prog_, "uRoughness");
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(1.0f, 1.0f);
    for (const auto& s : subs_) {
        glUniform3f(colorLoc, s.color[0], s.color[1], s.color[2]);
        glUniform1f(metallicLoc, s.metallic);
        glUniform1f(roughnessLoc, s.roughness);
        glBindVertexArray(s.vao);
        glDrawElements(GL_TRIANGLES, s.idxCount, GL_UNSIGNED_INT, 0);
    }
    glBindVertexArray(0);
    glDisable(GL_POLYGON_OFFSET_FILL);

    // Feature edges (so the preview reads like the main ShadedEdges view).
    glUseProgram(edgeProg_);
    glUniformMatrix4fv(glGetUniformLocation(edgeProg_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3fv(glGetUniformLocation(edgeProg_, "uColor"), 1, edgeColor_);
    glUniform1i(glGetUniformLocation(edgeProg_, "uClipCount"), 0);  // thumbnail: no clip
    glLineWidth(1.0f);  // no supersample here, so 1px lines
    for (const auto& s : subs_) {
        if (s.edgeVerts <= 0) continue;
        glBindVertexArray(s.edgeVao);
        glDrawArrays(GL_LINES, 0, s.edgeVerts);
    }
    glBindVertexArray(0);

    // Restore the main render state: unbind our FBO. (The next drawCurrent() rebinds fbo_ and
    // re-sets the supersampled viewport, but leave GL bound to the default FBO as the safe default,
    // matching drawCurrent's own epilogue.)
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    thumbYaw_ = yawDeg; thumbPitch_ = pitchDeg;
    thumbMeshRev_ = meshesRevision_;
    thumbValid_ = true;
    return thumbTex_;
}

// Soft elliptical contact shadow on a virtual ground plane just below the model bbox. Z is up
// in this scene, so the plane sits at the model's lowest Z and is sized to the bounding radius.
// Drawn with alpha blending, depth-tested but not depth-writing (it sits under the geometry).
void Renderer::drawGroundShadow(const Mat4& mvp, const Vec3& eye, const Vec3& up) {
    if (subs_.empty() || groundShadow_[3] <= 0.0f) return;
    (void)eye; (void)up;
    // Lowest Z of the model bbox; nudge down a hair to avoid z-fighting with the part's base.
    float groundZ = center_.z - radius_ * 1.0f;
    float e = radius_ * 2.2f;  // shadow blob extent (a little larger than the part footprint)
    float cx = center_.x, cy = center_.y;
    // Quad: pos.xyz + uv (uv in [-1,1] for the radial falloff). Two triangles.
    float v[6][5] = {
        {cx - e, cy - e, groundZ, -1.f, -1.f},
        {cx + e, cy - e, groundZ,  1.f, -1.f},
        {cx + e, cy + e, groundZ,  1.f,  1.f},
        {cx - e, cy - e, groundZ, -1.f, -1.f},
        {cx + e, cy + e, groundZ,  1.f,  1.f},
        {cx - e, cy + e, groundZ, -1.f,  1.f},
    };
    // Reuse the plane VAO/VBO but with a 5-float stride (pos + uv); the shadow shader binds
    // location 0 (pos) and 1 (uv).
    glUseProgram(shadowProg_);
    glUniformMatrix4fv(glGetUniformLocation(shadowProg_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3fv(glGetUniformLocation(shadowProg_, "uColor"), 1, groundShadow_);
    glUniform1f(glGetUniformLocation(shadowProg_, "uMaxAlpha"), groundShadow_[3]);

    static unsigned int shadowVao = 0, shadowVbo = 0;
    if (!shadowVao) {
        glGenVertexArrays(1, &shadowVao);
        glGenBuffers(1, &shadowVbo);
        glBindVertexArray(shadowVao);
        glBindBuffer(GL_ARRAY_BUFFER, shadowVbo);
        glBufferData(GL_ARRAY_BUFFER, sizeof(v), nullptr, GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 5 * sizeof(float), (void*)0);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 5 * sizeof(float),
                              (void*)(3 * sizeof(float)));
        glBindVertexArray(0);
    }
    glBindVertexArray(shadowVao);
    glBindBuffer(GL_ARRAY_BUFFER, shadowVbo);
    glBufferSubData(GL_ARRAY_BUFFER, 0, sizeof(v), v);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDepthMask(GL_FALSE);
    glDisable(GL_CULL_FACE);
    glDrawArrays(GL_TRIANGLES, 0, 6);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
    glBindVertexArray(0);
}

void Renderer::setPlaneColors(const float fill[4], const float outline[4]) {
    for (int i = 0; i < 4; ++i) { planeFill_[i] = fill[i]; planeOutline_[i] = outline[i]; }
}

void Renderer::setPlanes(const bool show[3], int emphasized) {
    showPlane_[0] = show[0]; showPlane_[1] = show[1]; showPlane_[2] = show[2];
    planeEmphasis_ = emphasized;
}

void Renderer::setSectionPlanes(const float n[][3], const float off[], int count,
                                const float capRgb[3], bool showCap) {
    count = count < 1 ? 1 : (count > kMaxSectionPlanes ? kMaxSectionPlanes : count);
    section_.count = count;
    section_.showCap = showCap;
    for (int i = 0; i < count; ++i) {
        Vec3 nv = normalize(Vec3(n[i][0], n[i][1], n[i][2]));
        section_.normals[i][0] = nv.x; section_.normals[i][1] = nv.y; section_.normals[i][2] = nv.z;
        section_.offsets[i] = off[i];
    }
    if (capRgb) { section_.capRgb[0] = capRgb[0]; section_.capRgb[1] = capRgb[1];
                  section_.capRgb[2] = capRgb[2]; }
}

void Renderer::setSectionPlane(const float normal[3], float offset, bool enabled,
                               const float capRgb[3], bool showCap) {
    if (!enabled) { section_.count = 0; return; }
    const float ns[1][3] = {{normal[0], normal[1], normal[2]}};
    const float offs[1] = {offset};
    setSectionPlanes(ns, offs, 1, capRgb, showCap);
}

// Four world-space corners of a plane quad of half-extent e, centered at center_.
// plane 0 = Front (XY, normal Z); 1 = Top (XZ, normal Y); 2 = Right (YZ, normal X).
void Renderer::planeCorners(int plane, float e, Vec3 out[4]) const {
    static const float uv[4][2] = {{-1,-1}, {1,-1}, {1,1}, {-1,1}};
    for (int i = 0; i < 4; ++i) {
        float u = uv[i][0] * e, v = uv[i][1] * e;
        if (plane == 0)      out[i] = Vec3(center_.x + u, center_.y + v, center_.z);       // XY
        else if (plane == 1) out[i] = Vec3(center_.x + u, center_.y,     center_.z + v);   // XZ
        else                 out[i] = Vec3(center_.x,     center_.y + u, center_.z + v);   // YZ
    }
}

bool Renderer::planeAnchor(int plane, Vec3& world) const {
    if (plane < 0 || plane > 2) return false;
    Vec3 c[4];
    planeCorners(plane, ((radius_ > 0.0f) ? radius_ : kEmptySceneRadius) * 1.25f, c);
    // Spread labels to different corners per plane so they rarely overlap on screen.
    static const int cornerFor[3] = {3, 2, 1};
    world = c[cornerFor[plane]];
    return true;
}

void Renderer::drawReferencePlanes(const Mat4& mvp) {
    if (!(showPlane_[0] || showPlane_[1] || showPlane_[2])) return;

    // W4-K: SolidWorks-style datum planes — one subtle light-blue fill + thin blue outline for all
    // three planes (palette.planeFill / planeOutline), instead of per-axis red/green/blue tints that
    // washed out the light viewport. Emphasis brightens the selected plane and dims the others.
    // C25: empty doc falls back to kEmptySceneRadius so a fresh part shows its planes.
    const float* fillC = planeFill_;
    const float* lineC = planeOutline_;
    const float e = ((radius_ > 0.0f) ? radius_ : kEmptySceneRadius) * 1.25f;

    glUseProgram(planeProg_);
    glUniformMatrix4fv(glGetUniformLocation(planeProg_, "uMVP"), 1, GL_FALSE, mvp.m);
    GLint colLoc = glGetUniformLocation(planeProg_, "uColor");

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDepthMask(GL_FALSE);            // test against geometry but don't occlude it
    glDisable(GL_CULL_FACE);
    glBindVertexArray(planeVao_);

    for (int p = 0; p < 3; ++p) {
        if (!showPlane_[p]) continue;
        bool emph = (planeEmphasis_ == p);
        bool dim  = (planeEmphasis_ >= 0 && !emph);
        // Scale the palette base alpha: emphasized plane brighter, others dimmer.
        float fScale = emph ? 1.8f : (dim ? 0.5f : 1.0f);
        float lScale = emph ? 1.0f : (dim ? 0.55f : 0.85f);
        float fillA = fillC[3] * fScale;
        float lineA = lineC[3] * lScale;

        Vec3 c[4];
        planeCorners(p, e, c);

        // Fill: two triangles (c0,c1,c2) (c0,c2,c3).
        float tri[18] = {
            c[0].x,c[0].y,c[0].z, c[1].x,c[1].y,c[1].z, c[2].x,c[2].y,c[2].z,
            c[0].x,c[0].y,c[0].z, c[2].x,c[2].y,c[2].z, c[3].x,c[3].y,c[3].z,
        };
        glBindBuffer(GL_ARRAY_BUFFER, planeVbo_);
        glBufferSubData(GL_ARRAY_BUFFER, 0, sizeof(tri), tri);
        glUniform4f(colLoc, fillC[0], fillC[1], fillC[2], fillA);
        glDrawArrays(GL_TRIANGLES, 0, 6);

        // Border: line loop of the 4 corners.
        float loop[12] = {
            c[0].x,c[0].y,c[0].z, c[1].x,c[1].y,c[1].z,
            c[2].x,c[2].y,c[2].z, c[3].x,c[3].y,c[3].z,
        };
        glBufferSubData(GL_ARRAY_BUFFER, 0, sizeof(loop), loop);
        glUniform4f(colLoc, lineC[0], lineC[1], lineC[2], lineA);
        glLineWidth(1.5f);
        glDrawArrays(GL_LINE_LOOP, 0, 4);
    }

    glBindVertexArray(0);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
}

// C28-C: filled section cap for plane[planeIdx]. Stencil-counts the clipped solid to find the
// cut interior, then paints a flat-lit quad masked by the stencil. The stencil pass applies ALL
// active planes (so only the visible intersection is marked). The cap quad is clipped by the OTHER
// planes in the cap shader so intersecting cuts never bleed through each other.
void Renderer::drawSectionCap(const Mat4& mvp, int planeIdx) {
    if (subs_.empty() || planeIdx < 0 || planeIdx >= section_.count) return;

    const float* pNorm = section_.normals[planeIdx];
    float pOff = section_.offsets[planeIdx];

    // ---- pass A: stencil mask using ALL active planes (full intersection) ----
    glEnable(GL_STENCIL_TEST);
    glClear(GL_STENCIL_BUFFER_BIT);
    glColorMask(GL_FALSE, GL_FALSE, GL_FALSE, GL_FALSE);
    glDepthMask(GL_FALSE);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glStencilFunc(GL_ALWAYS, 0, 0xFF);
    glStencilOpSeparate(GL_FRONT, GL_KEEP, GL_KEEP, GL_INCR_WRAP);
    glStencilOpSeparate(GL_BACK,  GL_KEEP, GL_KEEP, GL_DECR_WRAP);

    // Reuse the face program — all active clip planes already set from drawCurrent.
    glUseProgram(prog_);
    glUniformMatrix4fv(glGetUniformLocation(prog_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform1i(glGetUniformLocation(prog_, "uClipCount"), section_.count);
    glUniform3fv(glGetUniformLocation(prog_, "uClipN"),   section_.count, section_.normals[0]);
    glUniform1fv(glGetUniformLocation(prog_, "uClipOff"), section_.count, section_.offsets);
    glUniform1i(glGetUniformLocation(prog_, "uUseScalar"), 0);
    for (const auto& s : subs_) {
        glBindVertexArray(s.vao);
        glDrawElements(GL_TRIANGLES, s.idxCount, GL_UNSIGNED_INT, 0);
    }
    glBindVertexArray(0);

    // ---- pass B: paint the cap quad where stencil != 0 ----
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glDepthMask(GL_TRUE);
    glEnable(GL_DEPTH_TEST);
    glStencilFunc(GL_NOTEQUAL, 0, 0xFF);
    glStencilOp(GL_KEEP, GL_KEEP, GL_KEEP);

    Vec3 n(pNorm[0], pNorm[1], pNorm[2]);
    Vec3 up = (std::fabs(n.z) < 0.95f) ? Vec3(0, 0, 1) : Vec3(1, 0, 0);
    Vec3 u = normalize(cross(up, n));
    Vec3 v = normalize(cross(n, u));
    float d = dot(center_, n) - pOff;
    Vec3 c = center_ - n * d;
    float e = radius_ * 2.2f;
    Vec3 q[4] = {
        c - u*e - v*e, c + u*e - v*e, c + u*e + v*e, c - u*e + v*e,
    };
    float tri[18] = {
        q[0].x,q[0].y,q[0].z, q[1].x,q[1].y,q[1].z, q[2].x,q[2].y,q[2].z,
        q[0].x,q[0].y,q[0].z, q[2].x,q[2].y,q[2].z, q[3].x,q[3].y,q[3].z,
    };

    glUseProgram(capProg_);
    glUniformMatrix4fv(glGetUniformLocation(capProg_, "uMVP"), 1, GL_FALSE, mvp.m);
    glUniform3fv(glGetUniformLocation(capProg_, "uColor"), 1, section_.capRgb);
    glUniform3fv(glGetUniformLocation(capProg_, "uN"),     1, pNorm);
    glUniform3f(glGetUniformLocation(capProg_, "uKeyDir"), capKeyDir_.x, capKeyDir_.y, capKeyDir_.z);
    // Cross-plane clipping in the cap shader (excludes this plane's own half-space).
    glUniform1i(glGetUniformLocation(capProg_, "uClipCount"),  section_.count);
    glUniform3fv(glGetUniformLocation(capProg_, "uClipN"),     section_.count, section_.normals[0]);
    glUniform1fv(glGetUniformLocation(capProg_, "uClipOff"),   section_.count, section_.offsets);
    glUniform1i(glGetUniformLocation(capProg_, "uCapExclude"), planeIdx);

    glBindVertexArray(capVao_);
    glBindBuffer(GL_ARRAY_BUFFER, capVbo_);
    glBufferSubData(GL_ARRAY_BUFFER, 0, sizeof(tri), tri);
    glDrawArrays(GL_TRIANGLES, 0, 6);
    glBindVertexArray(0);

    glDisable(GL_STENCIL_TEST);
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
    pan_ = Vec3();  // named views look at the model center
    drawCurrent(yaw, pitch, 1.0f);
}

void Renderer::renderOrbit(float yawDeg, float pitchDeg, float zoom) {
    pan_ = Vec3();
    drawCurrent(yawDeg, pitchDeg, zoom);
}

void Renderer::renderOrbit(float yawDeg, float pitchDeg, float zoom, const float pan[3]) {
    pan_ = pan ? Vec3(pan[0], pan[1], pan[2]) : Vec3();
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
