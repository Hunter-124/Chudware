#include "renderer.h"

#include "gl_compat.h"
#ifdef CHUDWARE_GLES3
// GLES3 lacks polygon-offset-line (desktop-only); map to fill offset.
#ifndef GL_POLYGON_OFFSET_LINE
#  define GL_POLYGON_OFFSET_LINE GL_POLYGON_OFFSET_FILL
#endif
#endif


#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <vector>

#include "result_ramp.h"
#include "vec_math.h"
#include "ui_theme.h"
#include "ui/viewport/glsl_compat.h"


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
uniform int   uZebra;                  // Zebra: 1 = surface-continuity stripe mode on
uniform float uZebraFreq;              // Zebra: stripe count along the reflected-direction band
uniform float uAlpha;                  // edit-preview ghosting: 1 = opaque, < 1 = translucent
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

    // Zebra stripes (SolidWorks surface-continuity inspection): render the surface as if it
    // reflects a black/white striped environment. Stripe kinks reveal tangency (G1) breaks;
    // stripe jogs reveal curvature (G2) breaks. Takes precedence over the scalar LUT and the
    // RealView reflection path (it short-circuits before either runs), so zebra is exclusive.
    // We reflect the view vector about the normal and form the stripe coordinate from the
    // reflected direction's elevation (asin of the up component, world Z-up) — this lays the
    // bands out as roughly horizontal rings that flow continuously across a smooth surface,
    // which is the most legible orientation for reading continuity. fract() of (s * freq)
    // makes the repeating bands; smoothstep on the distance from a band edge anti-aliases them
    // (fwidth() is core in #version 330, so no extension is needed).
    if (uZebra == 1) {
        vec3 Rz   = reflect(-V, N);                       // reflected eye direction
        float s   = asin(clamp(Rz.z, -1.0, 1.0));         // elevation of R (Z-up) -> stripe coord
        float ph  = fract(s * uZebraFreq);                // 0..1 sawtooth along the band coord
        float d   = abs(ph - 0.5);                        // 0 at band center, 0.5 at the edges
        float aa  = max(fwidth(s * uZebraFreq), 1e-4);    // screen-space width of one phase unit
        float band = smoothstep(0.25 - aa, 0.25 + aa, d); // 0 = dark stripe, 1 = light stripe
        // Modulate lightly by the diffuse (key) term so the silhouette/shape still reads.
        float lit  = 0.55 + 0.45 * max(dot(N, normalize(uKeyDir)), 0.0);
        vec3 dark  = vec3(0.04);
        vec3 light = vec3(0.97);
        vec3 zc    = mix(dark, light, band) * lit;
        frag = vec4(clamp(zc, 0.0, 1.0), 1.0);
        return;
    }

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
    frag = vec4(c, uAlpha);
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

const char* kResolveVert = R"(#version 330 core
void main(){
    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);
})";

const char* kResolveFrag = R"(#version 330 core
uniform sampler2D uSrc;
uniform int uSS;
out vec4 frag;
void main(){
    ivec2 dst = ivec2(gl_FragCoord.xy);
    ivec2 base = dst * uSS;
    vec4 acc = vec4(0.0);
    for (int y = 0; y < uSS; ++y)
        for (int x = 0; x < uSS; ++x)
            acc += texelFetch(uSrc, base + ivec2(x, y), 0);
    frag = acc / float(uSS * uSS);
})";

const char* kDownsampleFrag = R"(#version 330 core
uniform sampler2D uSrc;
uniform vec2 uInvDstSize;
out vec4 frag;
void main(){
    // Fragment coordinates already address destination pixel centers. Normalize by the
    // destination size so a quarter-resolution backdrop covers the WHOLE source image.
    vec2 uv = gl_FragCoord.xy * uInvDstSize;
    frag = texture(uSrc, uv);
})";

const char* kKawaseFrag = R"(#version 330 core
uniform sampler2D uSrc;
uniform vec2 uTexel;
uniform float uOffset;
out vec4 frag;
void main(){
    vec2 uv = gl_FragCoord.xy * uTexel;
    vec2 d = uTexel * 0.5 * uOffset;
    vec4 c = texture(uSrc, uv);
    c += texture(uSrc, uv + vec2( d.x,  d.y));
    c += texture(uSrc, uv + vec2(-d.x,  d.y));
    c += texture(uSrc, uv + vec2( d.x, -d.y));
    c += texture(uSrc, uv + vec2(-d.x, -d.y));
    frag = c * 0.2;
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

// Cached origin-plane quad layout: per plane, 6 fill verts (two triangles) followed by the 4
// border verts (a line loop). Draw ranges are plane*kPlaneQuadVerts + {0, 6}.
constexpr int kPlaneQuadVerts = 10;

// The GL-free datum builder reports primitive modes as raw numbers so it needs no GL header;
// a drift between those and the real enums is a compile error, not garbled geometry.
static_assert(render::kModeLines == GL_LINES, "datum batch mode must match GL_LINES");
static_assert(render::kModeLineLoop == GL_LINE_LOOP, "datum batch mode must match GL_LINE_LOOP");
static_assert(render::kModeTriangles == GL_TRIANGLES, "datum batch mode must match GL_TRIANGLES");
}  // namespace

Renderer::~Renderer() {
    freeSubs();
    for (auto& [_, cached] : meshCache_) freeSubVector(cached.subs);
    meshCache_.clear();
    meshCacheOrder_.clear();
    clearGhost();   // free the component-edit ghost buffers if one is active at teardown
    if (fbo_) glDeleteFramebuffers(1, &fbo_);
    if (colorTex_) glDeleteTextures(1, &colorTex_);
    if (depthRbo_) glDeleteRenderbuffers(1, &depthRbo_);
#ifdef CHUDWARE_GLES3
    if (colorRbo_) glDeleteRenderbuffers(1, &colorRbo_);
#endif
    if (resolveFbo_) glDeleteFramebuffers(1, &resolveFbo_);
    if (resolvedTex_) glDeleteTextures(1, &resolvedTex_);
    // C27-D: View Selector preview thumbnail target (lazily created; may be 0).
    if (thumbFbo_) glDeleteFramebuffers(1, &thumbFbo_);
    if (thumbTex_) glDeleteTextures(1, &thumbTex_);
    if (thumbDepth_) glDeleteRenderbuffers(1, &thumbDepth_);
    if (prog_) glDeleteProgram(prog_);
    if (edgeProg_) glDeleteProgram(edgeProg_);
    if (bgProg_) glDeleteProgram(bgProg_);
    if (resolveProg_) glDeleteProgram(resolveProg_);
    if (downsampleProg_) glDeleteProgram(downsampleProg_);
    if (kawaseProg_) glDeleteProgram(kawaseProg_);
    if (blurDownTex_) glDeleteTextures(1, &blurDownTex_);
    if (blurDownFbo_) glDeleteFramebuffers(1, &blurDownFbo_);
    for (int i = 0; i < 2; ++i) {
        if (blurPingTex_[i]) glDeleteTextures(1, &blurPingTex_[i]);
        if (blurPingFbo_[i]) glDeleteFramebuffers(1, &blurPingFbo_[i]);
    }
    if (shadowProg_) glDeleteProgram(shadowProg_);
    if (planeProg_) glDeleteProgram(planeProg_);
    if (planeVbo_) glDeleteBuffers(1, &planeVbo_);
    if (planeVao_) glDeleteVertexArrays(1, &planeVao_);
    if (datumVbo_) glDeleteBuffers(1, &datumVbo_);
    if (datumVao_) glDeleteVertexArrays(1, &datumVao_);
    if (capProg_) glDeleteProgram(capProg_);
    if (capVbo_) glDeleteBuffers(1, &capVbo_);
    if (capVao_) glDeleteVertexArrays(1, &capVao_);
    if (lutTex_) glDeleteTextures(1, &lutTex_);
    // F1 async luminance pipeline: PBO ring + any outstanding fences.
    for (int i = 0; i < kLumaPboCount; ++i) {
        if (lumaFence_[i]) glDeleteSync(static_cast<GLsync>(lumaFence_[i]));
        if (lumaPbo_[i]) glDeleteBuffers(1, &lumaPbo_[i]);
    }
    // F1 GPU pass timing: double-buffered GL_TIME_ELAPSED query pairs.
    auto deleteGpuTimer = [](GpuTimerRing& t) { if (t.id[0]) glDeleteQueries(2, t.id); };
    deleteGpuTimer(sceneTimer_);
    deleteGpuTimer(blurTimer_);
    deleteGpuTimer(lumaTimer_);
}

// SolidWorks-style "jet/rainbow" result ramp from result_ramp.h (shared with the legend). Built
// once into a 256x1 RGB texture sampled by the shader's normalized scalar t in [0,1].
void Renderer::buildLut() {
    if (lutTex_) return;
    const int N = 256;
    std::vector<unsigned char> ramp(N * 3);
    for (int i = 0; i < N; ++i) {
        float rgb[3];
        result_ramp::sample((float)i / (float)(N - 1), rgb);
        for (int c = 0; c < 3; ++c) ramp[i * 3 + c] = (unsigned char)(rgb[c] * 255.0f + 0.5f);
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
    initQualityPolicy();  // sets rw_/rh_/sw_/sh_ + msaaRequest_/blurPasses_ for the step

    prog_ = linkProgram(kVert, kFrag);
    edgeProg_ = linkProgram(kEdgeVert, kEdgeFrag);
    bgProg_ = linkProgram(kBgVert, kBgFrag);
    resolveProg_ = linkProgram(kResolveVert, kResolveFrag);
    downsampleProg_ = linkProgram(kResolveVert, kDownsampleFrag);
    kawaseProg_ = linkProgram(kResolveVert, kKawaseFrag);
    shadowProg_ = linkProgram(kShadowVert, kShadowFrag);
    planeProg_ = linkProgram(kPlaneVert, kPlaneFrag);
    capProg_ = linkProgram(kCapVert, kCapFrag);
    int ok = 0;
    glGetProgramiv(prog_, GL_LINK_STATUS, &ok);
    if (!ok) { err = "program link failed"; return false; }
    glGetProgramiv(resolveProg_, GL_LINK_STATUS, &ok);
    if (!ok) { err = "resolve program link failed"; return false; }
    glGetProgramiv(downsampleProg_, GL_LINK_STATUS, &ok);
    if (!ok) { err = "downsample program link failed"; return false; }
    glGetProgramiv(kawaseProg_, GL_LINK_STATUS, &ok);
    if (!ok) { err = "kawase program link failed"; return false; }
    cacheUniformLocations();  // one name lookup per uniform, for the life of the program

    glGenVertexArrays(1, &bgVao_);

    // Origin reference-plane quads: 3 planes x (6 fill verts + 4 border verts), camera-independent
    // and refilled by refreshPlaneQuads() only when center_/radius_ change.
    glGenVertexArrays(1, &planeVao_);
    glGenBuffers(1, &planeVbo_);
    glBindVertexArray(planeVao_);
    glBindBuffer(GL_ARRAY_BUFFER, planeVbo_);
    glBufferData(GL_ARRAY_BUFFER, sizeof(float) * 3 * kPlaneQuadVerts * 3, nullptr,
                 GL_DYNAMIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 3 * sizeof(float), (void*)0);
    glBindVertexArray(0);

    // Batched datum overlay geometry (grown on demand by refreshDatumBatch()).
    glGenVertexArrays(1, &datumVao_);
    glGenBuffers(1, &datumVbo_);
    glBindVertexArray(datumVao_);
    glBindBuffer(GL_ARRAY_BUFFER, datumVbo_);
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
    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        err = "framebuffer incomplete";
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        return false;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, resolveFbo_);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        err = "resolve framebuffer incomplete";
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        return false;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glEnable(GL_DEPTH_TEST);
    return true;
}

// Resolve every uniform location once, right after the programs link. The draw paths then read
// ints instead of calling glGetUniformLocation (a driver-side name lookup) per uniform per frame.
// A uniform the compiler optimized out resolves to -1, which glUniform* ignores — identical to the
// previous behaviour, where the same -1 came back from every per-frame query.
void Renderer::cacheUniformLocations() {
    uFace_.mvp        = glGetUniformLocation(prog_, "uMVP");
    uFace_.keyDir     = glGetUniformLocation(prog_, "uKeyDir");
    uFace_.fillDir    = glGetUniformLocation(prog_, "uFillDir");
    uFace_.rimDir     = glGetUniformLocation(prog_, "uRimDir");
    uFace_.camPos     = glGetUniformLocation(prog_, "uCamPos");
    uFace_.color      = glGetUniformLocation(prog_, "uColor");
    uFace_.useScalar  = glGetUniformLocation(prog_, "uUseScalar");
    uFace_.scalarMin  = glGetUniformLocation(prog_, "uScalarMin");
    uFace_.scalarMax  = glGetUniformLocation(prog_, "uScalarMax");
    uFace_.metallic   = glGetUniformLocation(prog_, "uMetallic");
    uFace_.roughness  = glGetUniformLocation(prog_, "uRoughness");
    uFace_.lut        = glGetUniformLocation(prog_, "uLut");
    uFace_.clipCount  = glGetUniformLocation(prog_, "uClipCount");
    uFace_.clipN      = glGetUniformLocation(prog_, "uClipN");
    uFace_.clipOff    = glGetUniformLocation(prog_, "uClipOff");
    uFace_.realView   = glGetUniformLocation(prog_, "uRealView");
    uFace_.zebra      = glGetUniformLocation(prog_, "uZebra");
    uFace_.zebraFreq  = glGetUniformLocation(prog_, "uZebraFreq");
    uFace_.alpha      = glGetUniformLocation(prog_, "uAlpha");

    uEdge_.mvp        = glGetUniformLocation(edgeProg_, "uMVP");
    uEdge_.color      = glGetUniformLocation(edgeProg_, "uColor");
    uEdge_.clipCount  = glGetUniformLocation(edgeProg_, "uClipCount");
    uEdge_.clipN      = glGetUniformLocation(edgeProg_, "uClipN");
    uEdge_.clipOff    = glGetUniformLocation(edgeProg_, "uClipOff");

    uBg_.top          = glGetUniformLocation(bgProg_, "uTop");
    uBg_.mid          = glGetUniformLocation(bgProg_, "uMid");
    uBg_.bot          = glGetUniformLocation(bgProg_, "uBot");

    uShadow_.mvp      = glGetUniformLocation(shadowProg_, "uMVP");
    uShadow_.color    = glGetUniformLocation(shadowProg_, "uColor");
    uShadow_.maxAlpha = glGetUniformLocation(shadowProg_, "uMaxAlpha");

    uPlane_.mvp       = glGetUniformLocation(planeProg_, "uMVP");
    uPlane_.color     = glGetUniformLocation(planeProg_, "uColor");

    uCap_.mvp         = glGetUniformLocation(capProg_, "uMVP");
    uCap_.color       = glGetUniformLocation(capProg_, "uColor");
    uCap_.n           = glGetUniformLocation(capProg_, "uN");
    uCap_.keyDir      = glGetUniformLocation(capProg_, "uKeyDir");
    uCap_.clipCount   = glGetUniformLocation(capProg_, "uClipCount");
    uCap_.clipN       = glGetUniformLocation(capProg_, "uClipN");
    uCap_.clipOff     = glGetUniformLocation(capProg_, "uClipOff");
    uCap_.capExclude  = glGetUniformLocation(capProg_, "uCapExclude");

    uResolve_.src     = glGetUniformLocation(resolveProg_, "uSrc");
    uResolve_.ss      = glGetUniformLocation(resolveProg_, "uSS");

    uDown_.src        = glGetUniformLocation(downsampleProg_, "uSrc");
    uDown_.invDstSize = glGetUniformLocation(downsampleProg_, "uInvDstSize");

    uKawase_.src      = glGetUniformLocation(kawaseProg_, "uSrc");
    uKawase_.texel    = glGetUniformLocation(kawaseProg_, "uTexel");
    uKawase_.offset   = glGetUniformLocation(kawaseProg_, "uOffset");
}

// ---- Adaptive quality ladder ----
namespace {
struct QualitySpec { int msaa; float scale; int blur; };
QualitySpec qualitySpecFor(Renderer::QualityStep s) {
    switch (s) {
        case Renderer::QualityStep::High: return {2, 1.00f, 4};
        case Renderer::QualityStep::Med:  return {0, 1.00f, 2};
        case Renderer::QualityStep::Low:  return {0, 0.60f, 1};
        case Renderer::QualityStep::Full:
        default:                        return {4, 1.00f, 4};
    }
}
}  // namespace

const char* Renderer::qualityStepName() const {
    switch (qualityStep_) {
        case QualityStep::High: return "HIGH";
        case QualityStep::Med:  return "MED";
        case QualityStep::Low:  return "LOW";
        case QualityStep::Full:
        default:                return "FULL";
    }
}

void Renderer::initQualityPolicy() {
#ifdef __EMSCRIPTEN__
    // WASM is GPU-fill-bound on weak iGPUs (PERF_AUDIT_2026-07-16): start ADAPTIVE at MED
    // and let the ladder climb to FULL if the EMA says there's headroom.
    adaptiveQuality_ = true;
    qualityStep_ = QualityStep::Med;
#else
    // Native desktop: pinned FULL — zero behavior change unless explicitly overridden.
    adaptiveQuality_ = false;
    qualityStep_ = QualityStep::Full;
#endif
    if (const char* e = std::getenv("CHUDWARE_QUALITY_ADAPTIVE")) {
        if (e[0] == '1') adaptiveQuality_ = true;
        else if (e[0] == '0') adaptiveQuality_ = false;
    }
    if (const char* e = std::getenv("CHUDWARE_QUALITY_STEP")) {
        QualityStep s = qualityStep_;
        if (!std::strcmp(e, "full"))      s = QualityStep::Full;
        else if (!std::strcmp(e, "high")) s = QualityStep::High;
        else if (!std::strcmp(e, "med"))  s = QualityStep::Med;
        else if (!std::strcmp(e, "low"))  s = QualityStep::Low;
        else std::fprintf(stderr, "[quality] unknown CHUDWARE_QUALITY_STEP='%s' (ignored)\n", e);
        if (!std::strcmp(e, "full") || !std::strcmp(e, "high") ||
            !std::strcmp(e, "med") || !std::strcmp(e, "low")) {
            qualityStep_ = s;
            qualityPinned_ = true;
        }
    }
    if (const char* e = std::getenv("CHUDWARE_QUALITY_LOG"))
        qualityLog_ = (e[0] == '1');
    if (const char* e = std::getenv("CHUDWARE_QUALITY_FORCE_MS")) {
        const float v = (float)std::atof(e);
        if (v > 0.0f) qualityForceMs_ = v;
    }
    // Adaptive without a pin starts at MED ("measure first"): the ladder climbs to FULL
    // within a few seconds when the EMA shows headroom, instead of dropping frames at
    // FULL while it learns the GPU is slow.
    if (adaptiveQuality_ && !qualityPinned_) qualityStep_ = QualityStep::Med;
    applyQualityStep();
    rw_ = std::max(1, (int)std::lround(w_ * renderScale_));
    rh_ = std::max(1, (int)std::lround(h_ * renderScale_));
    sw_ = rw_ * SS; sh_ = rh_ * SS;
    if (qualityLog_)
        std::fprintf(stderr,
                     "[quality] init: adaptive=%d pinned=%d step=%s scale=%.2f msaaReq=%d blur=%d forceMs=%.1f\n",
                     adaptiveQuality_ ? 1 : 0, qualityPinned_ ? 1 : 0, qualityStepName(),
                     renderScale_, msaaRequest_, blurPasses_, qualityForceMs_);
}

void Renderer::applyQualityStep() {
    const QualitySpec qs = qualitySpecFor(qualityStep_);
    msaaRequest_ = qs.msaa;
    renderScale_ = qs.scale;
    blurPasses_ = qs.blur;
}

void Renderer::setQualityStep(QualityStep s) {
    if (s == qualityStep_) return;
    const char* from = qualityStepName();
    const float prevScale = renderScale_;
    const int prevMsaa = msaaRequest_;
    qualityStep_ = s;
    applyQualityStep();
    if (qualityLog_)
        std::fprintf(stderr, "[quality] step %s -> %s (ema %.2f ms, scale %.2f, msaa %d, blur %d)\n",
                     from, qualityStepName(), frameMsEma_, renderScale_, msaaRequest_,
                     blurPasses_);
    // Realloc render targets only when the MSAA count or render scale actually changed.
    if (renderScale_ != prevScale || msaaRequest_ != prevMsaa) {
        rw_ = std::max(1, (int)std::lround(w_ * renderScale_));
        rh_ = std::max(1, (int)std::lround(h_ * renderScale_));
        sw_ = rw_ * SS; sh_ = rh_ * SS;
        allocTargets();
    }
}

void Renderer::noteFrame(float dtMs) {
    const float ms = (qualityForceMs_ > 0.0f) ? qualityForceMs_ : dtMs;
    if (!(ms > 0.0f) || ms > 1000.0f) return;  // ignore garbage (tab restore, zero dt)
    frameMsEma_ += (ms - frameMsEma_) * (1.0f / 30.0f);  // ~30-frame EMA window
    if (qualityLog_ && ++qualityLogFrames_ >= 60) {
        qualityLogFrames_ = 0;
        std::fprintf(stderr, "[quality] step=%s ema=%.2f ms frame=%.2f ms scale=%.2f msaa=%d blur=%d\n",
                     qualityStepName(), frameMsEma_, ms, renderScale_, msaaRequest_, blurPasses_);
    }
    if (!adaptiveQuality_ || qualityPinned_) return;
    // Thresholds: sustained EMA above 20 ms can't hold a 60 Hz present; below 12.5 ms there
    // is clear headroom under the 16.7 ms budget. Hysteresis: step DOWN fast (0.25 s of
    // breach) but require ~1 s of continuous headroom before stepping back UP, so the
    // ladder can't oscillate across the vsync cliff.
    constexpr float kDownMs = 20.0f, kUpMs = 12.5f;
    constexpr float kDwellDownS = 0.25f, kDwellUpS = 1.0f;
    float dtS = dtMs * 0.001f;
    if (!(dtS > 0.0f)) dtS = 0.0f;
    if (dtS > 0.25f) dtS = 0.25f;  // clamp hitches so one stall can't fast-forward dwell
    if (frameMsEma_ > kDownMs && qualityStep_ != QualityStep::Low) {
        dwellDownS_ += dtS; dwellUpS_ = 0.0f;
        if (dwellDownS_ >= kDwellDownS) {
            setQualityStep(static_cast<QualityStep>(static_cast<int>(qualityStep_) + 1));
            dwellDownS_ = 0.0f;
        }
    } else if (frameMsEma_ < kUpMs && qualityStep_ != QualityStep::Full) {
        dwellUpS_ += dtS; dwellDownS_ = 0.0f;
        if (dwellUpS_ >= kDwellUpS) {
            setQualityStep(static_cast<QualityStep>(static_cast<int>(qualityStep_) - 1));
            dwellUpS_ = 0.0f;
        }
    } else {
        dwellDownS_ = dwellUpS_ = 0.0f;
    }
}

void Renderer::allocTargets() {
    if (!fbo_) glGenFramebuffers(1, &fbo_);
    if (!colorTex_) glGenTextures(1, &colorTex_);
    if (!depthRbo_) glGenRenderbuffers(1, &depthRbo_);
    if (!resolveFbo_) glGenFramebuffers(1, &resolveFbo_);
    if (!resolvedTex_) glGenTextures(1, &resolvedTex_);
#ifdef CHUDWARE_GLES3
    if (!colorRbo_) glGenRenderbuffers(1, &colorRbo_);
#endif

    // Final-size resolve target (ImGui samples this). Always a single-sample texture.
    glBindTexture(GL_TEXTURE_2D, resolvedTex_);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, rw_, rh_, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

#ifdef CHUDWARE_GLES3
    // WebGL2: prefer a multisampled offscreen FBO at presentation size (SS=1). Samples come
    // from the quality ladder (msaaRequest_), clamped to GL_MAX_SAMPLES; <=1 takes the
    // single-sample path. If MSAA FBO fails completeness, fall back to single-sample.
    GLint maxSamples = 0;
    glGetIntegerv(GL_MAX_SAMPLES, &maxSamples);
    int samples = msaaRequest_;
    if (maxSamples > 0 && samples > maxSamples) samples = maxSamples;
    if (samples < 1) samples = 1;

    msaaSamples_ = 0;
    if (msaaRequest_ > 1 && samples > 1) {
        glBindRenderbuffer(GL_RENDERBUFFER, colorRbo_);
        glRenderbufferStorageMultisample(GL_RENDERBUFFER, samples, GL_RGBA8, rw_, rh_);
        glBindRenderbuffer(GL_RENDERBUFFER, depthRbo_);
        glRenderbufferStorageMultisample(GL_RENDERBUFFER, samples, GL_DEPTH24_STENCIL8, rw_, rh_);

        glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, colorRbo_);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_RENDERBUFFER,
                                  depthRbo_);
        if (glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
            msaaSamples_ = samples;
            // Keep colorTex_ as a 1x1 placeholder so resolve's null-check stays simple;
            // MSAA resolve uses glBlitFramebuffer, not the texture sample path.
            glBindTexture(GL_TEXTURE_2D, colorTex_);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 1, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        }
    }
    if (msaaSamples_ <= 0) {
        // Single-sample fallback (no MSAA available / incomplete FBO).
        glBindTexture(GL_TEXTURE_2D, colorTex_);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, sw_, sh_, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glBindRenderbuffer(GL_RENDERBUFFER, depthRbo_);
        glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH24_STENCIL8, sw_, sh_);
        glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, colorTex_, 0);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_RENDERBUFFER,
                                  depthRbo_);
    }
#else
    glBindTexture(GL_TEXTURE_2D, colorTex_);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, sw_, sh_, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    // W3-I: packed depth+stencil so the section cap can be stencil-masked.
    glBindRenderbuffer(GL_RENDERBUFFER, depthRbo_);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH24_STENCIL8, sw_, sh_);

    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, colorTex_, 0);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_RENDERBUFFER,
                              depthRbo_);
#endif
    glBindFramebuffer(GL_FRAMEBUFFER, resolveFbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, resolvedTex_, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    resolvedDirty_ = true;
    backdropDirty_ = true;
    blurW_ = blurH_ = 0;
    // Existing grids/fences describe the old render targets, even if GL reuses their names.
    lumaSrcFbo_ = 0;
}

void Renderer::resize(int width, int height) {
    if (width == w_ && height == h_) return;
    w_ = width; h_ = height;
    rw_ = std::max(1, (int)std::lround(w_ * renderScale_));
    rh_ = std::max(1, (int)std::lround(h_ * renderScale_));
    sw_ = rw_ * SS; sh_ = rh_ * SS;
    allocTargets();
}

void Renderer::freeSubVector(std::vector<Sub>& subs) {
    for (auto& s : subs) {
        if (s.vao) glDeleteVertexArrays(1, &s.vao);
        if (s.vboPos) glDeleteBuffers(1, &s.vboPos);
        if (s.vboNrm) glDeleteBuffers(1, &s.vboNrm);
        if (s.ebo) glDeleteBuffers(1, &s.ebo);
        if (s.edgeVao) glDeleteVertexArrays(1, &s.edgeVao);
        if (s.vboEdge) glDeleteBuffers(1, &s.vboEdge);
        if (s.vboScalar) glDeleteBuffers(1, &s.vboScalar);
    }
    subs.clear();
}

void Renderer::freeSubs() {
    freeSubVector(subs_);
}

void Renderer::meshCacheTouch(const std::string& key) {
    meshCacheOrder_.remove(key);
    meshCacheOrder_.push_front(key);
}

void Renderer::meshCacheEvictIfNeeded() {
    while (meshCache_.size() > kMeshCacheMaxEntries && !meshCacheOrder_.empty()) {
        const std::string& oldest = meshCacheOrder_.back();
        auto it = meshCache_.find(oldest);
        if (it != meshCache_.end()) {
            freeSubVector(it->second.subs);
            meshCache_.erase(it);
        }
        meshCacheOrder_.pop_back();
    }
    // Drop any order entries that no longer map (defensive).
    while (!meshCacheOrder_.empty() && meshCache_.find(meshCacheOrder_.back()) == meshCache_.end())
        meshCacheOrder_.pop_back();
}

void Renderer::setActiveMeshIdentity(const std::string& docId, int revision) {
    if (docId.empty() || revision < 0) {
        activeMeshDocId_.clear();
        activeMeshRevision_ = -1;
        return;
    }
    // A newly uploaded revision supersedes every parked revision of the same document.
    discardCachedMeshes(docId);
    activeMeshDocId_ = docId;
    activeMeshRevision_ = revision;
}

void Renderer::parkActiveMeshes() {
    if (activeMeshDocId_.empty() || activeMeshRevision_ < 0) {
        freeSubs();
        return;
    }
    const std::string activeKey =
        activeMeshDocId_ + '\x1f' + std::to_string(activeMeshRevision_);
    auto old = meshCache_.find(activeKey);
    if (old != meshCache_.end()) {
        freeSubVector(old->second.subs);
        meshCache_.erase(old);
    }
    meshCache_.emplace(activeKey, CachedMesh{std::move(subs_), center_, radius_});
    meshCacheTouch(activeKey);
    meshCacheEvictIfNeeded();
    activeMeshDocId_.clear();
    activeMeshRevision_ = -1;
}

void Renderer::cacheMeshes(const std::string& docId, int revision,
                           const std::vector<PartMesh>& parts) {
    if (docId.empty() || revision < 0) return;
    if (activeMeshDocId_ == docId && activeMeshRevision_ == revision) return;

    // Upload into a temporary active slot, then restore the visible document unchanged. This runs
    // on the GL thread after background AICM parsing and makes the later tab switch a handle swap.
    std::vector<Sub> visibleSubs = std::move(subs_);
    const Vec3 visibleCenter = center_;
    const float visibleRadius = radius_;
    const std::string visibleDocId = activeMeshDocId_;
    const int visibleRevision = activeMeshRevision_;
    const bool wasFrozen = framingFrozen_;
    subs_.clear();
    activeMeshDocId_.clear();
    activeMeshRevision_ = -1;
    framingFrozen_ = false;
    setMeshes(parts);
    CachedMesh uploaded{std::move(subs_), center_, radius_};
    subs_ = std::move(visibleSubs);
    center_ = visibleCenter;
    radius_ = visibleRadius;
    activeMeshDocId_ = visibleDocId;
    activeMeshRevision_ = visibleRevision;
    framingFrozen_ = wasFrozen;

    discardCachedMeshes(docId);
    const std::string key = docId + '\x1f' + std::to_string(revision);
    meshCache_.emplace(key, std::move(uploaded));
    meshCacheTouch(key);
    meshCacheEvictIfNeeded();
}

bool Renderer::bindCachedMeshes(const std::string& docId, int revision) {
    if (docId.empty() || revision < 0) return false;
    if (activeMeshDocId_ == docId && activeMeshRevision_ == revision) return true;
    const std::string targetKey = docId + '\x1f' + std::to_string(revision);
    if (meshCache_.find(targetKey) == meshCache_.end()) return false;

    parkActiveMeshes();  // may rehash meshCache_; reacquire the target iterator afterwards
    auto target = meshCache_.find(targetKey);
    if (target == meshCache_.end()) return false;
    subs_ = std::move(target->second.subs);
    center_ = target->second.center;
    radius_ = target->second.radius;
    meshCache_.erase(target);
    meshCacheOrder_.remove(targetKey);
    activeMeshDocId_ = docId;
    activeMeshRevision_ = revision;
    ++meshesRevision_;
    return true;
}

void Renderer::discardCachedMeshes(const std::string& docId) {
    if (docId.empty()) return;
    const std::string prefix = docId + '\x1f';
    for (auto it = meshCache_.begin(); it != meshCache_.end();) {
        if (it->first.compare(0, prefix.size(), prefix) == 0) {
            freeSubVector(it->second.subs);
            meshCacheOrder_.remove(it->first);
            it = meshCache_.erase(it);
        } else {
            ++it;
        }
    }
}

// In-context component edit: steal the currently-uploaded active mesh (assembly) into the ghost
// buffer. This is a pure GPU-handle move — NO re-tessellate, NO file re-read — the assembly mesh is
// already on the GPU in subs_. The subsequent setMeshes(component) → freeSubs() only frees subs_,
// so the ghost survives the switch. Any prior ghost is freed first.
void Renderer::captureCurrentAsGhost() {
    clearGhost();
    ghostSubs_ = std::move(subs_);   // move GL handles; no reupload
    subs_.clear();                   // moved-from vector -> explicit empty (defensive)
    activeMeshDocId_.clear();        // ghost is intentionally not a switch-cache entry
    activeMeshRevision_ = -1;
    showGhost_ = true;
    ++meshesRevision_;
}

// Free the ghost buffers and stop drawing the ghost (exit component edit / renderer teardown).
void Renderer::clearGhost() {
    for (auto& s : ghostSubs_) {
        if (s.vao) glDeleteVertexArrays(1, &s.vao);
        if (s.vboPos) glDeleteBuffers(1, &s.vboPos);
        if (s.vboNrm) glDeleteBuffers(1, &s.vboNrm);
        if (s.ebo) glDeleteBuffers(1, &s.ebo);
        if (s.edgeVao) glDeleteVertexArrays(1, &s.edgeVao);
        if (s.vboEdge) glDeleteBuffers(1, &s.vboEdge);
        if (s.vboScalar) glDeleteBuffers(1, &s.vboScalar);
    }
    ghostSubs_.clear();
    showGhost_ = false;
    ghostXform_ = Mat4{};   // back to identity (no ghost shift) until the next component-edit sets one
    ++meshesRevision_;
}

// CAM remnant-stock aux mesh: translucent body drawn with the ghost-style alpha pass.
// Owned by the CAM viewport playhead — uploaded per sim frame index, cleared when the
// Manufacture workspace deactivates. Identity model matrix (CAM coords are world/WCS).
void Renderer::setAuxMesh(const Mesh& m, float alpha) {
    clearAuxMesh();
    if (m.empty()) return;
    auxAlpha_ = (alpha < 0.0f) ? 0.0f : (alpha > 1.0f ? 1.0f : alpha);
    Sub s;
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
    // complete the VAO with a NaN scalar buffer like the main upload (shader ignores it)
    glGenBuffers(1, &s.vboScalar);
    glBindBuffer(GL_ARRAY_BUFFER, s.vboScalar);
    std::vector<float> nans(m.positions.size() / 3, std::numeric_limits<float>::quiet_NaN());
    glBufferData(GL_ARRAY_BUFFER, nans.size() * sizeof(float), nans.data(), GL_STATIC_DRAW);
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 1, GL_FLOAT, GL_FALSE, sizeof(float), (void*)0);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s.ebo);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, m.indices.size() * sizeof(unsigned int),
                 m.indices.data(), GL_STATIC_DRAW);
    glBindVertexArray(0);
    s.idxCount = static_cast<int>(m.indices.size());
    auxSubs_.push_back(std::move(s));
    showAux_ = true;
}

void Renderer::clearAuxMesh() {
    for (auto& s : auxSubs_) {
        if (s.vao) glDeleteVertexArrays(1, &s.vao);
        if (s.vboPos) glDeleteBuffers(1, &s.vboPos);
        if (s.vboNrm) glDeleteBuffers(1, &s.vboNrm);
        if (s.ebo) glDeleteBuffers(1, &s.ebo);
        if (s.edgeVao) glDeleteVertexArrays(1, &s.edgeVao);
        if (s.vboEdge) glDeleteBuffers(1, &s.vboEdge);
        if (s.vboScalar) glDeleteBuffers(1, &s.vboScalar);
    }
    auxSubs_.clear();
    showAux_ = false;
}

void Renderer::setPreviewIsolation(const std::vector<uint64_t>& editFaceKeys, float alpha) {
    previewFaces_.clear();
    if (alpha >= 1.0f || editFaceKeys.empty()) { previewAlpha_ = 1.0f; return; }
    previewAlpha_ = alpha < 0.0f ? 0.0f : alpha;
    previewFaces_.reserve(editFaceKeys.size());
    for (uint64_t k : editFaceKeys) previewFaces_.insert(k);
}

// Re-upload a Sub's positions (and edge lines) as base + deformScale_ * disp. No-op if the body
// carries no displacement field.
void Renderer::uploadDeformed(Sub& s) {
    // Diagnostic for a "colored but flat" FEA result: a displacement field is present but its length
    // doesn't match the render vertices, so the deform is silently dropped. Fires only on that mismatch
    // (never on the normal empty-disp bodies), so it's a one-liner not a spammer.
    if (!s.disp.empty() && s.disp.size() != s.basePos.size())
        std::fprintf(stderr, "[render] deform skipped: disp=%zu != basePos=%zu (body %u)\n",
                     s.disp.size(), s.basePos.size(), s.body_id);
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

// Per-body hide/show (Solid Bodies folder). Bumps the mesh revision so the View Selector
// thumbnail cache redraws with the new visibility set.
void Renderer::setBodyVisible(size_t index, bool visible) {
    if (index >= subs_.size() || subs_[index].visible == visible) return;
    subs_[index].visible = visible;
    ++meshesRevision_;
}

void Renderer::setMesh(const Mesh& m) {
    PartMesh p;
    p.mesh = m;
    p.useDefaultColor = true;  // a single STL body carries no baked colour -> theme default part colour
    setMeshes({p});
}

void Renderer::drawSubFaces(const Sub& s, uint32_t firstTri, uint32_t triCount, int editing) {
    auto draw = [](uint32_t first, uint32_t count) {
        if (count)
            glDrawElements(GL_TRIANGLES, static_cast<GLsizei>(count * 3), GL_UNSIGNED_INT,
                           reinterpret_cast<const void*>(size_t(first) * 3 * sizeof(unsigned int)));
    };
    if (editing < 0 || s.faces.empty()) {
        if (editing != 1) draw(firstTri, triCount);
        return;
    }
    const uint64_t hi = uint64_t(s.body_id) << 32;
    const uint64_t endTri = uint64_t(firstTri) + triCount;
    uint32_t runFirst = 0, runCount = 0;
    auto face = std::lower_bound(s.faces.begin(), s.faces.end(), firstTri,
        [](const FaceSpan& f, uint32_t first) {
            return uint64_t(f.first_tri) + f.tri_count <= first;
        });
    for (; face != s.faces.end() && face->first_tri < endTri; ++face) {
        if ((previewFaces_.count(hi | face->face_id) != 0) != (editing != 0)) continue;
        const uint32_t first = std::max(firstTri, face->first_tri);
        const uint32_t end = static_cast<uint32_t>(
            std::min(endTri, uint64_t(face->first_tri) + face->tri_count));
        if (end <= first) continue;
        if (runCount && runFirst + runCount != first) {
            draw(runFirst, runCount);
            runCount = 0;
        }
        if (!runCount) runFirst = first;
        runCount += end - first;
    }
    draw(runFirst, runCount);
}

void Renderer::drawSubMaterials(const Sub& s, bool isolate) {
    if (s.materials.empty()) {
        glUniform3fv(uFace_.color, 1, s.color);
        glUniform1f(uFace_.metallic, s.metallic);
        glUniform1f(uFace_.roughness, s.roughness);
        drawSubFaces(s, 0, static_cast<uint32_t>(s.idxCount / 3), isolate ? 0 : -1);
        return;
    }
    for (const auto& material : s.materials) {
        glUniform3fv(uFace_.color, 1, material.color);
        glUniform1f(uFace_.metallic, material.metallic);
        glUniform1f(uFace_.roughness, material.roughness);
        drawSubFaces(s, material.first_tri, material.tri_count, isolate ? 0 : -1);
    }
}

void Renderer::setMeshes(const std::vector<PartMesh>& parts) {
    freeSubs();
    // setMeshes is a content change/rebuild, not a tab bind. The caller marks the new immutable
    // doc+revision identity after the upload succeeds.
    activeMeshDocId_.clear();
    activeMeshRevision_ = -1;
    Vec3 bbMin, bbMax;
    bool first = true;
    for (const auto& part : parts) {
        const Mesh& m = part.mesh;
        if (m.empty()) continue;
        Sub s;
        // Per-body color is authoritative: show the AICM-baked colour verbatim (the set_appearance
        // colour, or the engine's default grey when none is set) so an appearance change is visible.
        // Only a colour-less mesh (setMesh()) opts into the theme default part colour. Using an
        // explicit flag — not a sentinel colour value — means a legitimately-set colour equal to the
        // old struct default is no longer silently overridden ("set that grey-blue -> nothing").
        const bool useDefault = part.useDefaultColor;
        s.color[0] = useDefault ? defaultPart_[0] : part.color[0];
        s.color[1] = useDefault ? defaultPart_[1] : part.color[1];
        s.color[2] = useDefault ? defaultPart_[2] : part.color[2];
        s.metallic = part.metallic;     // C32-A RealView material
        s.roughness = part.roughness;
        // Per-feature preview isolation: keep the body id + face triangle-runs (sorted by first_tri
        // so adjacent same-class faces can be merged into one draw range during the split pass).
        s.body_id = part.body_id;
        s.faces = part.faces;
        std::sort(s.faces.begin(), s.faces.end(),
                  [](const FaceSpan& a, const FaceSpan& b) { return a.first_tri < b.first_tri; });
        if (!part.appearance_regions.empty()) {
            // APRG is sorted by first_face by the loader. Resolve IDs only on upload;
            // draw time walks compact triangle runs over the unchanged index buffer.
            auto append = [&](uint32_t firstTri, uint32_t triCount, const float* color,
                              float metallic, float roughness) {
                if (!triCount) return;
                if (!s.materials.empty()) {
                    auto& last = s.materials.back();
                    if (last.first_tri + last.tri_count == firstTri &&
                        std::equal(last.color, last.color + 3, color) &&
                        last.metallic == metallic && last.roughness == roughness) {
                        last.tri_count += triCount;
                        return;
                    }
                }
                MaterialSpan span;
                span.first_tri = firstTri;
                span.tri_count = triCount;
                std::copy(color, color + 3, span.color);
                span.metallic = metallic;
                span.roughness = roughness;
                s.materials.push_back(span);
            };
            const uint32_t totalTris = static_cast<uint32_t>(m.indices.size() / 3);
            uint32_t nextTri = 0;
            for (const auto& face : s.faces) {
                const uint32_t firstTri = std::min(face.first_tri, totalTris);
                const uint32_t endTri = static_cast<uint32_t>(std::min(
                    uint64_t(face.first_tri) + face.tri_count, uint64_t(totalTris)));
                if (firstTri > nextTri)
                    append(nextTri, firstTri - nextTri, s.color, s.metallic, s.roughness);
                const uint32_t start = std::max(firstTri, nextTri);
                if (endTri <= start) continue;
                auto region = std::upper_bound(
                    part.appearance_regions.begin(), part.appearance_regions.end(), face.face_id,
                    [](uint32_t id, const render::AppearanceRegion& r) { return id < r.first_face; });
                const render::AppearanceRegion* material = nullptr;
                if (region != part.appearance_regions.begin()) {
                    --region;
                    if (uint64_t(face.face_id) < uint64_t(region->first_face) + region->face_count)
                        material = &*region;
                }
                append(start, endTri - start, material ? material->color : s.color,
                       material ? material->metallic : s.metallic,
                       material ? material->roughness : s.roughness);
                nextTri = endTri;
            }
            if (nextTri < totalTris)
                append(nextTri, totalTris - nextTri, s.color, s.metallic, s.roughness);
            // Uniform authored bodies retain the original one-draw fast path.
            if (s.materials.size() == 1) {
                const auto& material = s.materials.front();
                std::copy(material.color, material.color + 3, s.color);
                s.metallic = material.metallic;
                s.roughness = material.roughness;
                s.materials.clear();
            }
        }
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
    // While framing is frozen (sketch editing), keep the camera frame stable so live rebuilds of
    // downstream features don't re-frame the view ("bouncing"). Geometry above was still uploaded;
    // only the bbox-derived center_/radius_ are held.
    if (framingFrozen_) {
        // keep center_/radius_ from before entering sketch mode
    } else if (!first) {
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

void Renderer::setAuxSceneBounds(const float lo[3], const float hi[3]) {
    if (!hasAuxSceneBounds_ || auxSceneLo_.x != lo[0] || auxSceneLo_.y != lo[1] ||
        auxSceneLo_.z != lo[2] || auxSceneHi_.x != hi[0] || auxSceneHi_.y != hi[1] ||
        auxSceneHi_.z != hi[2]) {
        auxSceneLo_ = Vec3(lo[0], lo[1], lo[2]);
        auxSceneHi_ = Vec3(hi[0], hi[1], hi[2]);
        auxSceneRadius_ = -1.0f;
    }
    hasAuxSceneBounds_ = true;
}

float Renderer::effectiveSceneRadius() const {
    const float baseRadius = std::max(radius_ > 0.0f ? radius_ : kEmptySceneRadius,
                                     auxSceneRadiusFloor_);
    if (!hasAuxSceneBounds_) return baseRadius;
    if (auxSceneRadius_ < 0.0f || auxRadiusCenter_.x != center_.x ||
        auxRadiusCenter_.y != center_.y || auxRadiusCenter_.z != center_.z) {
        const float dx = std::max(std::fabs(auxSceneLo_.x - center_.x),
                                  std::fabs(auxSceneHi_.x - center_.x));
        const float dy = std::max(std::fabs(auxSceneLo_.y - center_.y),
                                  std::fabs(auxSceneHi_.y - center_.y));
        const float dz = std::max(std::fabs(auxSceneLo_.z - center_.z),
                                  std::fabs(auxSceneHi_.z - center_.z));
        auxSceneRadius_ = std::sqrt(dx * dx + dy * dy + dz * dz);
        auxRadiusCenter_ = center_;
    }
    return std::max(baseRadius, auxSceneRadius_);
}

// C25: world radius used when the document has no geometry yet (a fresh "new part") —
// keeps the camera, datum planes, and label anchors at a sensible scale. ~35mm reads like
// SolidWorks's default plane size. Single source of truth lives in renderer.h (camera math in
// viewport_panel shares it via effectiveSceneRadius()); this file-scope alias keeps existing
// in-file references terse.
static constexpr float kEmptySceneRadius = Renderer::kEmptySceneRadius;

void Renderer::drawCurrent(float yawDeg, float pitchDeg, float zoom) {
    beginGpuTimer(sceneTimer_);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo_);
    glViewport(0, 0, sw_, sh_);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT | GL_STENCIL_BUFFER_BIT);

    // gradient studio background (fullscreen triangle, depth-independent)
    glDisable(GL_DEPTH_TEST);
    glUseProgram(bgProg_);
    glUniform3fv(uBg_.top, 1, bgTop_);
    glUniform3fv(uBg_.mid, 1, bgMid_);
    glUniform3fv(uBg_.bot, 1, bgBot_);
    glBindVertexArray(bgVao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindVertexArray(0);
    glEnable(GL_DEPTH_TEST);

    // C25: an EMPTY document still has a camera, datum planes, and overlay anchors (the
    // SolidWorks "new part" starting state) — only the geometry passes need meshes (their
    // loops over subs_ are natural no-ops). Previously this returned right here, leaving
    // cam_ stale: plane labels projected from garbage and the planes never drew.
    const float sceneR = effectiveSceneRadius();

    const float deg = 3.14159265f / 180.0f;
    float yaw = yawDeg * deg, pitch = pitchDeg * deg;
    Vec3 dir(std::cos(pitch) * std::sin(yaw), -std::cos(pitch) * std::cos(yaw), std::sin(pitch));
    // Orbit distance is always the historical perspective framing distance so eye placement
    // (and therefore shading lights) stays identical across modes; only the PROJECTION changes.
    // Ortho half-height = sceneR*1.05/zoom matches the perspective silhouette at the target plane.
    // framingScale_ (>= 1, set from the viewport's clear region) widens BOTH so zoom=1 fits the
    // framing sphere inside the un-occluded region rather than the full-window image.
    const float fov = kFovDeg * deg;
    const float zc  = std::max(0.05f, zoom);
    const float fitR = sceneR * framingScale_;
    float dist = fitR / std::sin(fov * 0.5f) * 1.05f / zc;
    // W2-D: pan shifts the orbit target (and therefore the eye) in world space. While a sketch is
    // open, orbitOverride_ replaces the model-bbox centre with the sketch's geometric centre so the
    // view orbits/frames around the sketch (which is not part of the body bbox), not the origin.
    Vec3 target = (orbitOverride_ ? orbitCenter_ : center_) + pan_;
    Vec3 eye = target + dir * dist;
    // Continuous turntable up-vector: the direction of increasing pitch (d(dir)/d(pitch)). For
    // pitch in (-90,90) this is exactly the world +Z component projected onto the view plane, so
    // existing views are pixel-identical; but it stays well-defined AT and BEYOND the poles, which
    // lets the camera orbit freely over the top/bottom with no gimbal flip (the old (0,1,0) hack
    // discontinuously snapped the roll there). Required now that the pitch clamp is gone.
    Vec3 up = normalize(Vec3(-std::sin(pitch) * std::sin(yaw),
                              std::sin(pitch) * std::cos(yaw),
                              std::cos(pitch)));

    const float aspect = (float)sw_ / (float)sh_;
    // World half-height of the view volume at the orbit target. Perspective uses dist*tan(fov/2)
    // (classic frustum); orthographic uses fitR*1.05/zoom so zoom=1 frames the same sphere.
    // Both are proportional to sceneR/zoom: rescaling zoom with the framing radius preserves
    // the eye, target-plane scale and lens shift in either projection.
    const float halfH = orthographic_
        ? (fitR * 1.05f / zc)
        : (dist * std::tan(fov * 0.5f));
    const float halfW = halfH * aspect;

    Mat4 proj;
    // Include the effective scene sphere even when radius growth was compensated by zoom
    // (and dist stayed unchanged). Pan/orbit overrides can move the target off that sphere.
    const float depthReach = 10.0f * sceneR + length(target - center_);
    if (orthographic_) {
        // Generous near/far in VIEW space (lookAt puts the eye at the origin looking -Z, so
        // the target sits at front-distance dist). The band must FOLLOW the eye: the orbit
        // distance grows with 1/zoom, so a fixed ±10*sceneR around the eye leaves the whole
        // scene beyond the far plane once zoom drops below ~0.35 — the datum quads, being the
        // largest geometry, get sliced first (the "planes vanish / get cut off when zooming
        // out in ortho" bug). dist±10*sceneR brackets the model, pan, and datum quads at any
        // zoom; a negative near (zoomed in close) is harmless for an ortho volume.
        // lensShiftX_/Y_ are NDC offsets: convert to world offsets = shift * halfW/halfH so the
        // asymmetric ortho volume re-centers the part in the clear region (same contract as
        // the perspective proj.m[8]/m[9]).
        const float shiftW = lensShiftX_ * halfW;
        const float shiftH = lensShiftY_ * halfH;
        proj = makeOrtho(-halfW + shiftW, halfW + shiftW,
                         -halfH + shiftH, halfH + shiftH,
                         dist - depthReach, dist + depthReach);
    } else {
        const float nearPlane = std::max(0.001f, std::min(dist * 0.01f, sceneR * 0.001f));
        proj = perspective(fov, aspect, nearPlane, dist + depthReach);
        // W2 full-bleed viewport: bake the lens shift into the projection's unused m[8]/m[9]
        // slots BEFORE building mvp, so the asymmetric frustum re-centers the part in the clear
        // region and the shift travels through cam_.mvp to the id-pass + overlays (pixel-exact).
        proj.m[8] = lensShiftX_;
        proj.m[9] = lensShiftY_;
    }
    Mat4 view = lookAt(eye, target, up);
    Mat4 mvp = mul(proj, view);

    // Camera-relative basis (published for pan/zoom helpers so they never re-derive FOV).
    Vec3 V = normalize(dir);                       // model -> camera
    Vec3 R = normalize(cross(up, V));
    Vec3 U = normalize(cross(V, R));

    // Publish the transform so the ID-buffer pass and overlays (C15) align to this exact frame,
    // and so every screen<->world helper can read halfHeight / worldPerPixel without hardcoding FOV.
    cam_.mvp = mvp;
    cam_.proj = proj;
    cam_.view = view;
    cam_.eye = eye;
    cam_.center = target;
    cam_.dir = V;
    cam_.right = R;
    cam_.up = U;
    cam_.radius = sceneR;
    cam_.dist = dist;
    cam_.halfHeight = halfH;
    cam_.zoom = zc;
    cam_.orthographic = orthographic_;
    cam_.ssWidth = sw_;
    cam_.ssHeight = sh_;
    cam_.vpWidth = w_;
    cam_.vpHeight = h_;

    // Soft ground contact shadow under the model (after bg, under geometry).
    drawGroundShadow(mvp, eye, up);

    // Camera-relative studio rig: soft key from the upper-left, fill from the right; the rim is
    // nearly off (tuned for the near-white studio bg where halos read wrong).
    // (V/R/U already computed above for cam_ publish.)
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
    glUniformMatrix4fv(uFace_.mvp, 1, GL_FALSE, mvp.m);
    glUniform3f(uFace_.keyDir, keyDir.x, keyDir.y, keyDir.z);
    glUniform3f(uFace_.fillDir, fillDir.x, fillDir.y, fillDir.z);
    glUniform3f(uFace_.rimDir, rimDir.x, rimDir.y, rimDir.z);
    glUniform3f(uFace_.camPos, eye.x, eye.y, eye.z);
    const int colorLoc = uFace_.color;
    const int useScalarLoc = uFace_.useScalar;
    const int sminLoc = uFace_.scalarMin;
    const int smaxLoc = uFace_.scalarMax;
    const int metallicLoc = uFace_.metallic;    // C32-A RealView material
    const int roughnessLoc = uFace_.roughness;
    // Bind the colormap LUT to texture unit 0.
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, lutTex_);
    glUniform1i(uFace_.lut, 0);
    // C28-C: multi-plane section clip (faces). Push all active planes as arrays.
    {
        int cnt = section_.count;
        glUniform1i(uFace_.clipCount, cnt);
        if (cnt > 0) {
            glUniform3fv(uFace_.clipN,   cnt, section_.normals[0]);
            glUniform1fv(uFace_.clipOff, cnt, section_.offsets);
        }
    }
    glUniform1f(uFace_.realView, realView_ ? 1.0f : 0.0f);  // C30-C
    glUniform1i(uFace_.zebra, zebra_ ? 1 : 0);              // Zebra stripes
    glUniform1f(uFace_.zebraFreq, zebraFreq_);
    const int alphaLoc = uFace_.alpha;
    // Per-feature edit-preview isolation: when a feature is being previewed (previewAlpha_ < 1 with a
    // non-empty owned-face set) ONLY that feature's faces draw translucent, in a second pass over the
    // opaque model. HLR forces everything opaque (its faces only write depth, no color).
    const bool isolate = (previewAlpha_ < 0.999f) && !previewFaces_.empty() && !hlr;


    // Solids, pushed back a touch so the feature edges win the depth test on top of them.
    // In HLR styles the faces are painted in the background color so they only occlude (depth) —
    // the classic hidden-line look. (Wireframe skips faces entirely.)
    if (drawFaces) {
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(1.0f, 1.0f);
        if (hlr) glUniform1i(useScalarLoc, 0);
        if (hlr) glColorMask(GL_FALSE, GL_FALSE, GL_FALSE, GL_FALSE);

        // Pass 1 — (semi-)opaque: every body's faces, minus the edited feature's when isolating.
        // bodyAlpha_ < 1 (Mass Properties) draws the whole part translucent so COM/I/J markers show.
        const bool bodyGhost = !isolate && !hlr && (bodyAlpha_ < 0.999f);
        if (bodyGhost) {
            glEnable(GL_BLEND);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            glDepthMask(GL_FALSE);
            glUniform1f(alphaLoc, bodyAlpha_);
        } else {
            glUniform1f(alphaLoc, 1.0f);
        }
        for (const auto& s : subs_) {
            if (!s.visible) continue;   // Solid Bodies: hidden body draws nothing
            bool useScalar = !hlr && colormap_ && s.hasScalar;
            glUniform1i(useScalarLoc, useScalar ? 1 : 0);
            if (useScalar) {
                glUniform1f(sminLoc, s.scalarMin);
                glUniform1f(smaxLoc, s.scalarMax);
            }
            glBindVertexArray(s.vao);
            if (!hlr && !useScalar) {
                drawSubMaterials(s, isolate);
            } else {
                glUniform3fv(colorLoc, 1, hlr ? bgMid_ : s.color);
                glUniform1f(metallicLoc, s.metallic);
                glUniform1f(roughnessLoc, s.roughness);
                drawSubFaces(s, 0, static_cast<uint32_t>(s.idxCount / 3), isolate ? 0 : -1);
            }
        }
        if (bodyGhost) {
            glUniform1f(alphaLoc, 1.0f);
            glDepthMask(GL_TRUE);
            glDisable(GL_BLEND);
        }
        if (hlr) glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);

        // Pass 2 — translucent: ONLY the edited feature's faces, as a uniform coloured ghost over the
        // opaque model (SolidWorks-style edit preview). The faces are drawn in a fixed amber TINT —
        // not the body's own shade — so the in-progress feature reads as a clearly-marked preview.
        // Depth-WRITE is OFF so every layer blends evenly and the whole feature is consistently
        // see-through (depth-write ON made the nearest face read opaque while only the far side showed
        // through). Depth-TEST stays on, so opaque geometry IN FRONT still correctly hides it.
        if (isolate) {
            const float tint[3] = {0.98f, 0.55f, 0.12f};   // amber/orange preview ghost
            glEnable(GL_BLEND);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            glDepthMask(GL_FALSE);
            glUniform1i(useScalarLoc, 0);                  // a flat tint, never the FEA colormap
            glUniform1f(alphaLoc, previewAlpha_);
            glUniform3f(colorLoc, tint[0], tint[1], tint[2]);
            glUniform1f(metallicLoc, 0.0f);                // matte dielectric for a clean tint
            glUniform1f(roughnessLoc, 0.5f);
            for (const auto& s : subs_) {
                if (!s.visible || s.faces.empty()) continue;
                glBindVertexArray(s.vao);
                drawSubFaces(s, 0, static_cast<uint32_t>(s.idxCount / 3), 1);
            }
            glUniform1f(alphaLoc, 1.0f);
            glDepthMask(GL_TRUE);
            glDisable(GL_BLEND);
        }

        // In-context component edit: the parked assembly mesh renders as a dimmed static GHOST behind
        // the live component, so the user keeps assembly context (SolidWorks "Edit Component"). Same
        // translucent shaded pass as the edit-preview ghost above — a flat neutral tint at ghostAlpha_,
        // depth-write OFF so it blends evenly, depth-TEST on so the opaque component correctly occludes
        // it. Non-pickable by construction (ghostSubs_ is never fed to id_pass). HLR skips it (the
        // ghost is only meaningful in a shaded style). Reuses the already-bound prog_ + camera/lights.
        if (showGhost_ && !ghostSubs_.empty() && !hlr) {
            const float gtint[3] = {0.55f, 0.60f, 0.68f};   // neutral cool grey ghost
            // Relocate the ghost by ghostXform_ (identity unless in component-edit): apply it to the
            // ghost vertices BEFORE the camera, i.e. uMVP_ghost = mvp * ghostXform_ (mul() is a*b in
            // column-major, so ghostXform_ is the innermost/first factor on the vertex). The caller
            // feeds inverse(editedComponent.placement) so the ghost's copy of the edited component
            // lands on the live part at its own origin. Restored to mvp after the loop.
            const Mat4 ghostMvp = mul(mvp, ghostXform_);
            glUniformMatrix4fv(uFace_.mvp, 1, GL_FALSE, ghostMvp.m);
            glEnable(GL_BLEND);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            glDepthMask(GL_FALSE);
            glUniform1i(useScalarLoc, 0);                   // flat tint, never the FEA colormap
            glUniform1f(alphaLoc, ghostAlpha_);
            glUniform3f(colorLoc, gtint[0], gtint[1], gtint[2]);
            glUniform1f(metallicLoc, 0.0f);                 // matte dielectric for a clean tint
            glUniform1f(roughnessLoc, 0.6f);
            for (const auto& s : ghostSubs_) {
                if (!s.visible) continue;
                glBindVertexArray(s.vao);
                glDrawElements(GL_TRIANGLES, s.idxCount, GL_UNSIGNED_INT, 0);
            }
            glUniform1f(alphaLoc, 1.0f);
            glDepthMask(GL_TRUE);
            glDisable(GL_BLEND);
            // Restore the unshifted MVP for any later prog_ pass in this frame (cheap insurance).
            glUniformMatrix4fv(uFace_.mvp, 1, GL_FALSE, mvp.m);
        }

        // CAM remnant-stock aux mesh (playhead remnant): same translucent shaded pass as
        // the ghost — flat neutral tint, depth-write OFF, identity model matrix (CAM
        // coords are world/WCS). Non-pickable (never fed to id_pass). HLR skips it.
        if (showAux_ && !auxSubs_.empty() && !hlr) {
            const float atint[3] = {0.55f, 0.60f, 0.68f};   // neutral stock grey
            glEnable(GL_BLEND);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            glDepthMask(GL_FALSE);
            glUniform1i(useScalarLoc, 0);
            glUniform1f(alphaLoc, auxAlpha_);
            glUniform3f(colorLoc, atint[0], atint[1], atint[2]);
            glUniform1f(metallicLoc, 0.0f);
            glUniform1f(roughnessLoc, 0.7f);
            for (const auto& s : auxSubs_) {
                if (!s.visible) continue;
                glBindVertexArray(s.vao);
                glDrawElements(GL_TRIANGLES, s.idxCount, GL_UNSIGNED_INT, 0);
            }
            glUniform1f(alphaLoc, 1.0f);
            glDepthMask(GL_TRUE);
            glDisable(GL_BLEND);
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
        glUniformMatrix4fv(uEdge_.mvp, 1, GL_FALSE, mvp.m);
        const int eColorLoc = uEdge_.color;
        // C28-C: multi-plane clip for edges.
        {
            int cnt = section_.count;
            glUniform1i(uEdge_.clipCount, cnt);
            if (cnt > 0) {
                glUniform3fv(uEdge_.clipN,   cnt, section_.normals[0]);
                glUniform1fv(uEdge_.clipOff, cnt, section_.offsets);
            }
        }
        glLineWidth(edgeWidthPx_ * (float)SS);  // screen-space width after downsample

        // HiddenLinesVisible: first draw occluded edges dim (depth GL_GREATER), then visible edges.
        if (ds == DisplayStyle::HiddenLinesVisible) {
            glDepthFunc(GL_GREATER);
            glUniform3f(eColorLoc, 0.62f, 0.65f, 0.70f);  // dim grey for hidden edges
            for (const auto& s : subs_) {
                if (!s.visible || s.edgeVerts <= 0) continue;
                glBindVertexArray(s.edgeVao);
                glDrawArrays(GL_LINES, 0, s.edgeVerts);
            }
            glDepthFunc(GL_LESS);
        }

        glUniform3fv(eColorLoc, 1, edgeColor_);
        for (const auto& s : subs_) {
            if (!s.visible || s.edgeVerts <= 0) continue;
            glBindVertexArray(s.edgeVao);
            glDrawArrays(GL_LINES, 0, s.edgeVerts);
        }
        glBindVertexArray(0);
    }

    // ---- reference overlays (translucent; depthMask off so they never occlude solids) ----
    // Origin planes first, then non-axis datums, then axes LAST. Planes write no depth, so
    // draw order alone decides compositing: axes must land ON TOP of translucent plane fills
    // or they wash out to near-invisible grey under the quads. Axes still depth-test against
    // solid faces (depthMask stays off so they never occlude the mesh).
    drawReferencePlanes(mvp);
    drawRefDatums(mvp, /*axesOnly=*/false);
    {
        // Datum axes stay inside the viewport's clear rect when one is set (setDatumAxisClip):
        // the image runs under the glass columns and crisp dashed axes would cross pane text.
        // Caller scissor state is saved and restored exactly.
        const bool axisClip = datumAxisClip_ && sw_ > 0 && sh_ > 0;
        GLint prevBox[4] = {0, 0, 0, 0};
        GLboolean prevScissor = GL_FALSE;
        if (axisClip) {
            prevScissor = glIsEnabled(GL_SCISSOR_TEST);
            glGetIntegerv(GL_SCISSOR_BOX, prevBox);
            const float* c = datumAxisClipUV_;
            const int x0 = (int)std::floor(c[0] * (float)sw_);
            const int x1 = (int)std::ceil(c[2] * (float)sw_);
            const int y0 = (int)std::floor((1.0f - c[3]) * (float)sh_);   // GL origin bottom-left
            const int y1 = (int)std::ceil((1.0f - c[1]) * (float)sh_);
            glEnable(GL_SCISSOR_TEST);
            glScissor(x0, y0, std::max(0, x1 - x0), std::max(0, y1 - y0));
        }
        drawRefDatums(mvp, /*axesOnly=*/true);
        if (axisClip) {
            glScissor(prevBox[0], prevBox[1], prevBox[2], prevBox[3]);
            if (!prevScissor) glDisable(GL_SCISSOR_TEST);
        }
    }

    endGpuTimer(sceneTimer_, sceneMs_, sceneTimingValid_);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    resolvedDirty_ = true;
    backdropDirty_ = true;
}

// C27-D: render the current meshes from a candidate orientation into a small dedicated FBO and
// return its color-texture id (for the View Selector hover preview). No supersampling, no ground
// shadow / section / datum planes — just the lit solids + feature edges over a solid light bg, so
// the candidate view reads at a glance. Cheap: a (yaw,pitch,meshes-revision) cache skips the
// re-render when nothing changed. CRITICALLY restores every GL state the main pass depends on:
// the bound framebuffer (0), the full-window viewport is re-set by the next drawCurrent() (which
// always binds fbo_ + sets glViewport), but we still leave GL in a clean, predictable state here.
unsigned int Renderer::renderThumbnail(float yawDeg, float pitchDeg, int px, bool transparentBg) {
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

    // Cache: re-use the existing texture if the orientation, meshes and bg mode are unchanged.
    if (thumbValid_ && thumbMeshRev_ == meshesRevision_ &&
        thumbYaw_ == yawDeg && thumbPitch_ == pitchDeg && thumbTransparent_ == transparentBg) {
        return thumbTex_;
    }

    // ---- render the thumbnail ----
    glBindFramebuffer(GL_FRAMEBUFFER, thumbFbo_);
    glViewport(0, 0, px, px);
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LESS);
    if (transparentBg) glClearColor(0.0f, 0.0f, 0.0f, 0.0f);  // transparent icon tile
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

    if (transparentBg) {
        // Icon pipeline: no studio fill — leave the tile transparent so the lit part
        // composites over alpha=0. Part fragments are opaque (uAlpha=1 below); silhouettes are
        // hard-edged (no supersample) so alpha stays a clean 0/255 mask.
        glDisable(GL_BLEND);
    } else {
        // Solid light studio background (single fill via the gradient shader with all three stops
        // equal — avoids a separate clear-color path and keeps the near-white look of the main view).
        glDisable(GL_DEPTH_TEST);
        glUseProgram(bgProg_);
        glUniform3fv(uBg_.top, 1, bgTop_);
        glUniform3fv(uBg_.mid, 1, bgMid_);
        glUniform3fv(uBg_.bot, 1, bgBot_);
        glBindVertexArray(bgVao_);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        glBindVertexArray(0);
        glEnable(GL_DEPTH_TEST);
    }

    const float sceneR = (radius_ > 0.0f) ? radius_ : kEmptySceneRadius;
    const float deg = 3.14159265f / 180.0f;
    float yaw = yawDeg * deg, pitch = pitchDeg * deg;
    Vec3 dir(std::cos(pitch) * std::sin(yaw), -std::cos(pitch) * std::cos(yaw), std::sin(pitch));
    const float fov = kFovDeg * deg;
    // Orbit distance matches live view (zoom=1 fit, no pan). Ortho half-height matches perspective
    // framing so the thumbnail silhouette is continuous across projection modes.
    float dist = sceneR / std::sin(fov * 0.5f) * 1.05f;
    Vec3 target = center_;
    Vec3 eye = target + dir * dist;
    // Same continuous up-vector as drawCurrent (see note there) so thumbnails match the live view.
    Vec3 up = normalize(Vec3(-std::sin(pitch) * std::sin(yaw),
                              std::sin(pitch) * std::cos(yaw),
                              std::cos(pitch)));

    const float halfH = orthographic_ ? (sceneR * 1.05f) : (dist * std::tan(fov * 0.5f));
    Mat4 proj;
    if (orthographic_) {
        proj = makeOrtho(-halfH, halfH, -halfH, halfH,
                         -10.0f * sceneR, 10.0f * sceneR);  // square aspect
    } else {
        proj = perspective(fov, 1.0f, dist * 0.01f, dist * 10.0f);  // square aspect
    }
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
    glUniformMatrix4fv(uFace_.mvp, 1, GL_FALSE, mvp.m);
    glUniform3f(uFace_.keyDir, keyDir.x, keyDir.y, keyDir.z);
    glUniform3f(uFace_.fillDir, fillDir.x, fillDir.y, fillDir.z);
    glUniform3f(uFace_.rimDir, rimDir.x, rimDir.y, rimDir.z);
    glUniform3f(uFace_.camPos, eye.x, eye.y, eye.z);
    glUniform1i(uFace_.useScalar, 0);
    glUniform1i(uFace_.clipCount, 0);   // thumbnail: no section clip
    glUniform1f(uFace_.realView, 1.0f);  // C30-C: reflections on in previews
    glUniform1i(uFace_.zebra, 0);        // thumbnail: never striped
    glUniform1f(uFace_.alpha, 1.0f);     // thumbnail: always opaque
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(1.0f, 1.0f);
    for (const auto& s : subs_) {
        if (!s.visible) continue;   // Solid Bodies: thumbnail mirrors viewport visibility
        glBindVertexArray(s.vao);
        drawSubMaterials(s);
    }
    glBindVertexArray(0);
    glDisable(GL_POLYGON_OFFSET_FILL);

    // Feature edges (so the preview reads like the main ShadedEdges view).
    glUseProgram(edgeProg_);
    glUniformMatrix4fv(uEdge_.mvp, 1, GL_FALSE, mvp.m);
    glUniform3fv(uEdge_.color, 1, edgeColor_);
    glUniform1i(uEdge_.clipCount, 0);   // thumbnail: no clip
    glLineWidth(1.0f);  // no supersample here, so 1px lines
    for (const auto& s : subs_) {
        if (!s.visible || s.edgeVerts <= 0) continue;
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
    thumbTransparent_ = transparentBg;
    thumbValid_ = true;
    return thumbTex_;
}

// Read back the thumbnail FBO (the last renderThumbnail target) as px*px RGBA, flipped to
// top-left origin — the clean, transparent-capable icon capture the pipeline saves as a PNG.
std::vector<unsigned char> Renderer::readThumbnailRGBA(int px) const {
    if (px < 16) px = 16;
    std::vector<unsigned char> raw((size_t)px * px * 4, 0);
    if (!thumbFbo_) return raw;
    glBindFramebuffer(GL_FRAMEBUFFER, thumbFbo_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, px, px, GL_RGBA, GL_UNSIGNED_BYTE, raw.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    std::vector<unsigned char> out((size_t)px * px * 4, 0);
    for (int y = 0; y < px; ++y) {
        const unsigned char* src = &raw[(size_t)y * px * 4];
        std::copy(src, src + (size_t)px * 4, &out[(size_t)(px - 1 - y) * px * 4]);
    }
    return out;
}

// Soft elliptical contact shadow on a virtual ground plane just below the model bbox. Z is up
// in this scene, so the plane sits at the model's lowest Z and is sized to the bounding radius.
// Drawn with alpha blending, depth-tested but not depth-writing (it sits under the geometry).
void Renderer::drawGroundShadow(const Mat4& mvp, const Vec3& eye, const Vec3& up) {
    if (subs_.empty() || groundShadow_[3] <= 0.0f) return;
    // Solid Bodies: the blob is one aggregate (not per-mesh) — drop it only when EVERYTHING is
    // hidden, so an empty-looking viewport doesn't keep a floating shadow.
    bool anyVisible = false;
    for (const auto& s : subs_) if (s.visible) { anyVisible = true; break; }
    if (!anyVisible) return;
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
    // Own VAO/VBO with a 5-float stride (pos + uv); the shadow shader binds
    // location 0 (pos) and 1 (uv). Lazily created once.
    glUseProgram(shadowProg_);
    glUniformMatrix4fv(uShadow_.mvp, 1, GL_FALSE, mvp.m);
    glUniform3fv(uShadow_.color, 1, groundShadow_);
    glUniform1f(uShadow_.maxAlpha, groundShadow_[3]);

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

int Renderer::sectionClip(float normals[3][3], float offsets[3]) const {
    for (int i = 0; i < section_.count && i < kMaxSectionPlanes; ++i) {
        normals[i][0] = section_.normals[i][0];
        normals[i][1] = section_.normals[i][1];
        normals[i][2] = section_.normals[i][2];
        offsets[i] = section_.offsets[i];
    }
    return section_.count;
}

// Four world-space corners of a plane quad of half-extent e, centered at center_.
// The index convention MUST match the engine's reserved datums (engine/doc/features/datum.py
// reserved_datums, canonical Z-up) AND the camera view presets (renderer eyeForView):
//   0 = Front (XZ, normal -Y)  — the "Front" view looks along +Y at this plane
//   1 = Top   (XY, normal +Z)  — the "Top" view looks down -Z at this plane
//   2 = Right (YZ, normal +X)  — the "Right" view looks along -X at this plane
// The host previously drew 0=XY (Front) / 1=XZ (Top) — the SolidWorks *Y-up* naming — which
// disagreed with the engine's Z-up plane.front=XZ / plane.top=XY. The datum overlay therefore
// highlighted/labelled one plane while the click sketched on the swapped plane (the reported
// "it highlights a vertical plane but sketches on Top" bug). Geometry now matches the engine.
void Renderer::planeCorners(int plane, float e, Vec3 out[4]) const {
    static const float uv[4][2] = {{-1,-1}, {1,-1}, {1,1}, {-1,1}};
    // The default datum planes pass through the WORLD ORIGIN (engine SketchPlane::top/front/right
    // origin = (0,0,0)); the quad is only CENTERED on the model in its two in-plane axes so it stays
    // near the part for visibility. The out-of-plane axis is pinned to 0 — using center_ there made
    // the Top plane float to the model's mid-height (the "Plane1 base displays halfway" bug) and
    // disagreed with where a sketch on that plane actually lands.
    for (int i = 0; i < 4; ++i) {
        float u = uv[i][0] * e, v = uv[i][1] * e;
        if (plane == 0)      out[i] = Vec3(center_.x + u, 0.0f,          center_.z + v);   // XZ  Front (y=0)
        else if (plane == 1) out[i] = Vec3(center_.x + u, center_.y + v, 0.0f);            // XY  Top   (z=0)
        else                 out[i] = Vec3(0.0f,          center_.y + u, center_.z + v);   // YZ  Right (x=0)
    }
}

bool Renderer::planeQuad(int plane, Vec3 out[4]) const {
    if (plane < 0 || plane > 2) return false;
    planeCorners(plane, effectiveSceneRadius() * 1.25f, out);
    return true;
}

// Rebuild the cached origin-plane quad geometry. The quads are CAMERA-INDEPENDENT — they depend
// only on center_ and effectiveSceneRadius() — so orbiting, zooming, emphasis and palette changes all
// reuse the same upload. Layout per plane: 6 fill verts (two triangles) then 4 border verts.
void Renderer::refreshPlaneQuads() {
    const float e = effectiveSceneRadius() * 1.25f;
    if (planeQuadExtent_ == e && planeQuadCenter_.x == center_.x &&
        planeQuadCenter_.y == center_.y && planeQuadCenter_.z == center_.z)
        return;
    // Fill is (c0,c1,c2) (c0,c2,c3); border is the 4 corners as a line loop.
    static const int kOrder[kPlaneQuadVerts] = {0, 1, 2, 0, 2, 3, 0, 1, 2, 3};
    float v[3 * kPlaneQuadVerts * 3];
    for (int p = 0; p < 3; ++p) {
        Vec3 c[4];
        planeCorners(p, e, c);
        float* dst = v + (size_t)p * kPlaneQuadVerts * 3;
        for (int i = 0; i < kPlaneQuadVerts; ++i) {
            const Vec3& src = c[kOrder[i]];
            dst[i * 3 + 0] = src.x;
            dst[i * 3 + 1] = src.y;
            dst[i * 3 + 2] = src.z;
        }
    }
    glBindBuffer(GL_ARRAY_BUFFER, planeVbo_);
    glBufferSubData(GL_ARRAY_BUFFER, 0, sizeof(v), v);
    planeQuadExtent_ = e;
    planeQuadCenter_ = center_;
}

void Renderer::drawReferencePlanes(const Mat4& mvp) {
    if (!(showPlane_[0] || showPlane_[1] || showPlane_[2])) return;

    // W4-K: SolidWorks-style datum planes — one subtle light-blue fill + thin blue outline for all
    // three planes (palette.planeFill / planeOutline), instead of per-axis red/green/blue tints that
    // washed out the light viewport. Emphasis brightens the selected plane and dims the others.
    // C25: empty doc falls back to kEmptySceneRadius so a fresh part shows its planes.
    const float* fillC = planeFill_;
    const float* lineC = planeOutline_;

    refreshPlaneQuads();   // no-op unless the model bbox / scene radius moved

    glUseProgram(planeProg_);
    glUniformMatrix4fv(uPlane_.mvp, 1, GL_FALSE, mvp.m);
    const int colLoc = uPlane_.color;

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_DEPTH_TEST);          // must occlude behind solid faces (CASE B bleed-through)
    glDepthMask(GL_FALSE);            // test against geometry but don't occlude it
    // Face culling is never enabled anywhere in the host (GL's default is off, and ImGui's GL3
    // backend saves/restores the flag around its own draw), so the glDisable(GL_CULL_FACE) that
    // used to sit in every overlay pass could not change state — these two-sided quads, cones and
    // caps are drawn from either side regardless.
    // Match the solid-face glPolygonOffset(1,1) so we compare against the SAME depth values the
    // shaded pass wrote. Without this, solid depths are pushed farther while planes sit at true
    // depth and always win GL_LESS — planes (and the origin triad) bleed through the mesh.
    glEnable(GL_POLYGON_OFFSET_FILL);
    glEnable(GL_POLYGON_OFFSET_LINE);
    glPolygonOffset(1.0f, 1.0f);
    glBindVertexArray(planeVao_);
    glLineWidth(1.5f);

    for (int p = 0; p < 3; ++p) {
        if (!showPlane_[p]) continue;
        bool emph = (planeEmphasis_ == p);
        bool dim  = (planeEmphasis_ >= 0 && !emph);
        // Scale the palette base alpha: emphasized plane brighter, others dimmer.
        float fScale = emph ? 1.8f : (dim ? 0.5f : 1.0f);
        float lScale = emph ? 1.0f : (dim ? 0.55f : 0.85f);
        float fillA = fillC[3] * fScale;
        float lineA = lineC[3] * lScale;
        const int base = p * kPlaneQuadVerts;

        glUniform4f(colLoc, fillC[0], fillC[1], fillC[2], fillA);
        glDrawArrays(GL_TRIANGLES, base, 6);
        glUniform4f(colLoc, lineC[0], lineC[1], lineC[2], lineA);
        glDrawArrays(GL_LINE_LOOP, base + 6, 4);
    }
    glBindVertexArray(0);
    glDisable(GL_POLYGON_OFFSET_LINE);
    glDisable(GL_POLYGON_OFFSET_FILL);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
}

float Renderer::refAxisHalfLength(const std::string& id) const {
    // Match drawReferencePlanes / drawRefDatums: e = sceneRadius * 1.25. The rule itself lives with
    // the GL-free datum builder so draw and hit-test can never drift apart.
    return render::refAxisHalfLength(id, effectiveSceneRadius() * 1.25f);
}

// Rebuild + re-upload the batched datum geometry when any input changed. Inputs are the datum set
// (revision), the FeatureManager-hidden ids, the selected / PM-edited datum, the scene half-extent
// e, and the palette axis/accent roles — everything the build reads. The camera is NOT an input, so
// orbiting, zooming, panning and section changes all reuse the upload.
void Renderer::refreshDatumBatch() {
    const float e = effectiveSceneRadius() * 1.25f;
    render::RefDatumPalette pal;
    for (int i = 0; i < 4; ++i) {
        pal.axisX[i] = ui::palette.axisX[i];
        pal.axisY[i] = ui::palette.axisY[i];
        pal.axisZ[i] = ui::palette.axisZ[i];
        pal.accent[i] = ui::palette.accent[i];
    }
    if (datumBatchValid_ && datumBatchRevision_ == datumsRevision_ && datumBatchExtent_ == e &&
        datumBatchSelected_ == selectedDatumId_ && datumBatchEditing_ == editingDatumId_ &&
        datumBatchPalette_ == pal && datumBatchHidden_ == hiddenDatums_)
        return;

    render::buildRefDatumBatch(refDatums_, hiddenDatums_, selectedDatumId_, editingDatumId_, e, pal,
                               datumBatch_);
    const size_t floats = datumBatch_.verts.size();
    if (floats > 0) {
        glBindBuffer(GL_ARRAY_BUFFER, datumVbo_);
        if (floats > datumVboFloats_) {
            // Grow (and orphan the old storage) only when the batch outgrew the buffer.
            glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(floats * sizeof(float)),
                         datumBatch_.verts.data(), GL_DYNAMIC_DRAW);
            datumVboFloats_ = floats;
        } else {
            glBufferSubData(GL_ARRAY_BUFFER, 0, (GLsizeiptr)(floats * sizeof(float)),
                            datumBatch_.verts.data());
        }
    }
    datumBatchRevision_ = datumsRevision_;
    datumBatchExtent_ = e;
    datumBatchSelected_ = selectedDatumId_;
    datumBatchEditing_ = editingDatumId_;
    datumBatchHidden_ = hiddenDatums_;
    datumBatchPalette_ = pal;
    datumBatchValid_ = true;
}

void Renderer::setDatumAxisClip(float u0, float v0, float u1, float v1) {
    u0 = std::clamp(u0, 0.0f, 1.0f); u1 = std::clamp(u1, u0, 1.0f);
    v0 = std::clamp(v0, 0.0f, 1.0f); v1 = std::clamp(v1, v0, 1.0f);
    datumAxisClipUV_[0] = u0; datumAxisClipUV_[1] = v0;
    datumAxisClipUV_[2] = u1; datumAxisClipUV_[3] = v1;
    datumAxisClip_ = true;
}

// Feature reference geometry: datum axes (lines), planes (quads), points (markers), and coordinate
// systems (3 axes). These build no body, so the engine ships their geometry out-of-band (setRefDatums
// from doc_mirror::refDatums). Reuses the flat uMVP+uColor plane shader. Datums depth-test against
// the solid (depthMask off so they never occlude it) — same contract as the origin planes. Polygon
// offset matches the shaded-pass offset so we don't always win GL_LESS against pushed solid depths
// (origin triad bleed-through when force-shown over a body).
//
// Two-pass draw from drawCurrent: non-axis datums after origin planes, then axesOnly=true LAST so
// dashed axes composite OVER translucent plane fills (planes write no depth). Axes use per-axis
// RGB colors at high opacity; selectedDatumId_ draws bold/bright (set each frame from selection).
//
// The geometry itself is built by render::buildRefDatumBatch into ONE vertex buffer with a merged
// draw list (render/ref_datum_geom.cpp) and cached; this function is pure GL state + a handful of
// glDrawArrays. It used to upload a 2..6-vertex range and issue a draw call per primitive — per
// dash, per cone triangle, per octahedron face — every frame.
void Renderer::drawRefDatums(const Mat4& mvp, bool axesOnly) {
    if (refDatums_.empty() || planeProg_ == 0) return;
    refreshDatumBatch();
    const std::vector<render::RefDatumDraw>& cmds =
        axesOnly ? datumBatch_.axis : datumBatch_.nonAxis;
    if (cmds.empty()) return;

    glUseProgram(planeProg_);
    glUniformMatrix4fv(uPlane_.mvp, 1, GL_FALSE, mvp.m);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_DEPTH_TEST);          // occlude parts of the triad that sit behind solid faces
    glDepthMask(GL_FALSE);
    glEnable(GL_POLYGON_OFFSET_FILL);
    glEnable(GL_POLYGON_OFFSET_LINE);
    glPolygonOffset(1.0f, 1.0f);      // match solid-face offset (see drawReferencePlanes)
    glBindVertexArray(datumVao_);

    // Draw order inside the pass is the build order, which is the old per-primitive order — the
    // pass blends with depth-write off, so order is the only observable besides the pixels.
    float lineWidth = -1.0f;
    for (const render::RefDatumDraw& c : cmds) {
        if (c.lineWidth > 0.0f && c.lineWidth != lineWidth) {
            glLineWidth(c.lineWidth);
            lineWidth = c.lineWidth;
        }
        glUniform4fv(uPlane_.color, 1, c.rgba);
        glDrawArrays(c.mode, c.first, c.count);
    }

    glLineWidth(1.0f);
    glBindVertexArray(0);
    glDisable(GL_POLYGON_OFFSET_LINE);
    glDisable(GL_POLYGON_OFFSET_FILL);
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
    glStencilFunc(GL_ALWAYS, 0, 0xFF);
    glStencilOpSeparate(GL_FRONT, GL_KEEP, GL_KEEP, GL_INCR_WRAP);
    glStencilOpSeparate(GL_BACK,  GL_KEEP, GL_KEEP, GL_DECR_WRAP);

    // Reuse the face program — all active clip planes already set from drawCurrent.
    glUseProgram(prog_);
    glUniformMatrix4fv(uFace_.mvp, 1, GL_FALSE, mvp.m);
    glUniform1i(uFace_.clipCount, section_.count);
    glUniform3fv(uFace_.clipN,   section_.count, section_.normals[0]);
    glUniform1fv(uFace_.clipOff, section_.count, section_.offsets);
    glUniform1i(uFace_.useScalar, 0);
    for (const auto& s : subs_) {
        if (!s.visible) continue;   // hidden body never marks the cap stencil
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
    glUniformMatrix4fv(uCap_.mvp, 1, GL_FALSE, mvp.m);
    glUniform3fv(uCap_.color, 1, section_.capRgb);
    glUniform3fv(uCap_.n,     1, pNorm);
    glUniform3f(uCap_.keyDir, capKeyDir_.x, capKeyDir_.y, capKeyDir_.z);
    // Cross-plane clipping in the cap shader (excludes this plane's own half-space).
    glUniform1i(uCap_.clipCount,  section_.count);
    glUniform3fv(uCap_.clipN,     section_.count, section_.normals[0]);
    glUniform1fv(uCap_.clipOff,   section_.count, section_.offsets);
    glUniform1i(uCap_.capExclude, planeIdx);

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
    resolvedDirty_ = true;
    backdropDirty_ = true;
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

void Renderer::resolveColorTexture() const {
    if (!resolvedDirty_ || !resolveFbo_ || !resolvedTex_) return;
#ifdef CHUDWARE_GLES3
    // Multisampled path: resolve via blit (WebGL2 requires NEAREST for MSAA resolve).
    if (msaaSamples_ > 0) {
        GLint prevFbo = 0;
        glGetIntegerv(GL_FRAMEBUFFER_BINDING, &prevFbo);
        glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo_);
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, resolveFbo_);
        glBlitFramebuffer(0, 0, rw_, rh_, 0, 0, rw_, rh_, GL_COLOR_BUFFER_BIT, GL_NEAREST);
        glBindFramebuffer(GL_FRAMEBUFFER, prevFbo);
        resolvedDirty_ = false;
        backdropDirty_ = true;
        return;
    }
    // Single-sample fallback (SS==1): 1:1 box filter via resolveProg_.
#endif
    if (!colorTex_ || !resolveProg_) return;

    GLint prevFbo = 0, prevViewport[4] = {0, 0, 0, 0}, prevProgram = 0;
    GLint prevActiveTex = 0, prevTex0 = 0;
    GLboolean prevDepthTest = glIsEnabled(GL_DEPTH_TEST);
    GLboolean prevBlend = glIsEnabled(GL_BLEND);
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &prevFbo);
    glGetIntegerv(GL_VIEWPORT, prevViewport);
    glGetIntegerv(GL_CURRENT_PROGRAM, &prevProgram);
    glGetIntegerv(GL_ACTIVE_TEXTURE, &prevActiveTex);
    glActiveTexture(GL_TEXTURE0);
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &prevTex0);

    glBindFramebuffer(GL_FRAMEBUFFER, resolveFbo_);
    glViewport(0, 0, rw_, rh_);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    glUseProgram(resolveProg_);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, colorTex_);
    glUniform1i(uResolve_.src, 0);
    glUniform1i(uResolve_.ss, SS);
    glBindVertexArray(bgVao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindVertexArray(0);

    glBindFramebuffer(GL_FRAMEBUFFER, prevFbo);
    glViewport(prevViewport[0], prevViewport[1], prevViewport[2], prevViewport[3]);
    glUseProgram((GLuint)prevProgram);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, (GLuint)prevTex0);
    glActiveTexture((GLenum)prevActiveTex);
    if (prevDepthTest) glEnable(GL_DEPTH_TEST); else glDisable(GL_DEPTH_TEST);
    if (prevBlend) glEnable(GL_BLEND); else glDisable(GL_BLEND);

    resolvedDirty_ = false;
    backdropDirty_ = true;
}

void Renderer::updateBackdropBlur() const {
    if (!backdropDirty_ || !resolvedTex_ || !downsampleProg_ || !kawaseProg_) return;
    const int bw = std::max(1, rw_ / 4);
    const int bh = std::max(1, rh_ / 4);
    if (bw != blurW_ || bh != blurH_) {
        blurW_ = bw;
        blurH_ = bh;
        auto allocTex = [](unsigned int& tex, int tw, int th) {
            if (!tex) glGenTextures(1, &tex);
            glBindTexture(GL_TEXTURE_2D, tex);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, tw, th, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                         nullptr);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        };
        auto allocFbo = [](unsigned int& fbo, unsigned int tex) {
            if (!fbo) glGenFramebuffers(1, &fbo);
            glBindFramebuffer(GL_FRAMEBUFFER, fbo);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
        };
        allocTex(blurDownTex_, bw, bh);
        allocFbo(blurDownFbo_, blurDownTex_);
        allocTex(blurPingTex_[0], bw, bh);
        allocFbo(blurPingFbo_[0], blurPingTex_[0]);
        allocTex(blurPingTex_[1], bw, bh);
        allocFbo(blurPingFbo_[1], blurPingTex_[1]);
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
    }

    GLint prevFbo = 0, prevViewport[4] = {0, 0, 0, 0}, prevProgram = 0;
    GLint prevActiveTex = 0, prevTex0 = 0;
    GLboolean prevDepthTest = glIsEnabled(GL_DEPTH_TEST);
    GLboolean prevBlend = glIsEnabled(GL_BLEND);
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &prevFbo);
    glGetIntegerv(GL_VIEWPORT, prevViewport);
    glGetIntegerv(GL_CURRENT_PROGRAM, &prevProgram);
    glGetIntegerv(GL_ACTIVE_TEXTURE, &prevActiveTex);
    glActiveTexture(GL_TEXTURE0);
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &prevTex0);

    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    glBindVertexArray(bgVao_);

    // Downsample resolved color to ¼ resolution.
    glBindFramebuffer(GL_FRAMEBUFFER, blurDownFbo_);
    glViewport(0, 0, blurW_, blurH_);
    glUseProgram(downsampleProg_);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, resolvedTex_);
    glUniform1i(uDown_.src, 0);
    glUniform2f(uDown_.invDstSize, 1.0f / (float)blurW_, 1.0f / (float)blurH_);
    glDrawArrays(GL_TRIANGLES, 0, 3);

    unsigned int srcTex = blurDownTex_;
    for (int pass = 0; pass < blurPasses_; ++pass) {
        unsigned int dstFbo = blurPingFbo_[pass & 1];
        unsigned int dstTex = blurPingTex_[pass & 1];
        glBindFramebuffer(GL_FRAMEBUFFER, dstFbo);
        glViewport(0, 0, blurW_, blurH_);
        glUseProgram(kawaseProg_);
        glBindTexture(GL_TEXTURE_2D, srcTex);
        glUniform1i(uKawase_.src, 0);
        glUniform2f(uKawase_.texel, 1.0f / (float)blurW_, 1.0f / (float)blurH_);
        glUniform1f(uKawase_.offset, 1.0f + (float)pass);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        srcTex = dstTex;
    }

    glBindFramebuffer(GL_FRAMEBUFFER, prevFbo);
    glViewport(prevViewport[0], prevViewport[1], prevViewport[2], prevViewport[3]);
    glUseProgram((GLuint)prevProgram);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, (GLuint)prevTex0);
    glActiveTexture((GLenum)prevActiveTex);
    glBindVertexArray(0);
    if (prevDepthTest) glEnable(GL_DEPTH_TEST); else glDisable(GL_DEPTH_TEST);
    if (prevBlend) glEnable(GL_BLEND); else glDisable(GL_BLEND);

    backdropDirty_ = false;
}

ViewportBackdrop Renderer::backdrop() const {
    resolveColorTexture();
    if (backdropEnabled_) {
        beginGpuTimer(blurTimer_);
        updateBackdropBlur();
        endGpuTimer(blurTimer_, blurMs_, blurTimingValid_);

        beginGpuTimer(lumaTimer_);
        issueLumaReadback();
        endGpuTimer(lumaTimer_, lumaMs_, lumaTimingValid_);
    }
    ViewportBackdrop bd;
    if (!backdropEnabled_ || blurW_ <= 0 || blurH_ <= 0) return bd;
    // Final blur texture is whichever ping-pong buffer holds the last pass output.
    unsigned int tex = blurPingTex_[(blurPasses_ - 1) & 1];
    if (!tex) tex = blurDownTex_;
    bd.textureId = static_cast<std::uintptr_t>(tex);
    bd.uvMin[0] = 0.f;
    bd.uvMin[1] = 0.f;
    bd.uvMax[0] = 1.f;
    bd.uvMax[1] = 1.f;
    return bd;
}

// F1: pure CPU box-average of the cached async luminance grid (see reduceLumaPbo()). ZERO GL
// calls — (x0,y0,x1,y1) is viewport-local top-left px coords, matching the grid's row convention.
float Renderer::averageLuminance(float x0, float y0, float x1, float y1) const {
    // No grid yet (first frames after launch, or the readback is disabled): report UNKNOWN, not a
    // neutral 0.5. ui_theme's adaptiveSurfaceStyled() branches on `l > 0.50f` to pick the ink
    // tone, so a 0.5 sentinel lost the tie and handed every glass panel LIGHT text — white-on-
    // white until the first async readback landed. -1 routes it to the theme-correct palette
    // fallback instead (`avgLuminance < 0` in ui_theme.cpp), which is what "unknown" means there.
    if (!lumaEverRead_ || lumaGrid_.empty() || lumaW_ <= 0 || lumaH_ <= 0 || w_ <= 0 || h_ <= 0)
        return -1.0f;

    if (x1 < x0) std::swap(x0, x1);
    if (y1 < y0) std::swap(y0, y1);
    x0 = std::max(0.0f, std::min(x0, (float)w_));
    x1 = std::max(0.0f, std::min(x1, (float)w_));
    y0 = std::max(0.0f, std::min(y0, (float)h_));
    y1 = std::max(0.0f, std::min(y1, (float)h_));

    const float sx = (float)lumaW_ / (float)w_;
    const float sy = (float)lumaH_ / (float)h_;
    int gx0 = std::max(0, std::min(lumaW_ - 1, (int)std::floor(x0 * sx)));
    int gy0 = std::max(0, std::min(lumaH_ - 1, (int)std::floor(y0 * sy)));
    int gx1 = std::max(gx0 + 1, std::min(lumaW_, (int)std::ceil(x1 * sx)));
    int gy1 = std::max(gy0 + 1, std::min(lumaH_, (int)std::ceil(y1 * sy)));

    double sum = 0.0;
    int count = 0;
    for (int gy = gy0; gy < gy1; ++gy) {
        const float* row = &lumaGrid_[(size_t)gy * (size_t)lumaW_];
        for (int gx = gx0; gx < gx1; ++gx) { sum += row[gx]; ++count; }
    }
    // Same reasoning as the no-grid early-out above: a degenerate rect that sampled zero cells is
    // UNKNOWN, not neutral. Returning 0.5f here reintroduced the exact tie adaptiveSurfaceStyled()
    // loses, for the narrow case of a rect that clamped to nothing.
    return count > 0 ? (float)(sum / count) : -1.0f;
}

// Dynamic-color stats (Liquid Glass v2.1): mean RGB + mean luminance + luminance variance under
// the rect, same cached-grid box-average as averageLuminance (ZERO GL calls). false until the
// async pipeline has published at least one grid — consumers then treat dynamic color as off.
bool Renderer::averageBackdropStats(float x0, float y0, float x1, float y1,
                                    float outRgb[3], float& outLum, float& outVariance) const {
    if (!lumaEverRead_ || lumaGrid_.empty() || lumaW_ <= 0 || lumaH_ <= 0 || w_ <= 0 || h_ <= 0 ||
        lumaRgbGrid_.size() != lumaGrid_.size() * 3)
        return false;

    if (x1 < x0) std::swap(x0, x1);
    if (y1 < y0) std::swap(y0, y1);
    x0 = std::max(0.0f, std::min(x0, (float)w_));
    x1 = std::max(0.0f, std::min(x1, (float)w_));
    y0 = std::max(0.0f, std::min(y0, (float)h_));
    y1 = std::max(0.0f, std::min(y1, (float)h_));

    const float sx = (float)lumaW_ / (float)w_;
    const float sy = (float)lumaH_ / (float)h_;
    int gx0 = std::max(0, std::min(lumaW_ - 1, (int)std::floor(x0 * sx)));
    int gy0 = std::max(0, std::min(lumaH_ - 1, (int)std::floor(y0 * sy)));
    int gx1 = std::max(gx0 + 1, std::min(lumaW_, (int)std::ceil(x1 * sx)));
    int gy1 = std::max(gy0 + 1, std::min(lumaH_, (int)std::ceil(y1 * sy)));

    double sumR = 0.0, sumG = 0.0, sumB = 0.0, sumL = 0.0, sumL2 = 0.0;
    int count = 0;
    for (int gy = gy0; gy < gy1; ++gy) {
        const float* lrow = &lumaGrid_[(size_t)gy * (size_t)lumaW_];
        const float* crow = &lumaRgbGrid_[(size_t)gy * (size_t)lumaW_ * 3];
        for (int gx = gx0; gx < gx1; ++gx) {
            const float l = lrow[gx];
            sumL += l;
            sumL2 += (double)l * (double)l;
            sumR += crow[gx * 3 + 0];
            sumG += crow[gx * 3 + 1];
            sumB += crow[gx * 3 + 2];
            ++count;
        }
    }
    if (count <= 0) return false;
    const double inv = 1.0 / count;
    outRgb[0] = (float)(sumR * inv);
    outRgb[1] = (float)(sumG * inv);
    outRgb[2] = (float)(sumB * inv);
    const double meanL = sumL * inv;
    outLum = (float)meanL;
    outVariance = (float)std::max(0.0, sumL2 * inv - meanL * meanL);
    return true;
}

// ---- F1 GPU pass timing: 2 queries per phase, ping-ponged so a result never blocks ----
void Renderer::beginGpuTimer(GpuTimerRing& t) {
#ifdef CHUDWARE_GLES3
    (void)t;  // WebGL2 has no reliable TIME_ELAPSED timer queries
#else
    if (!t.id[0]) glGenQueries(2, t.id);
    glBeginQuery(GL_TIME_ELAPSED, t.id[t.cur]);
#endif
}

void Renderer::endGpuTimer(GpuTimerRing& t, float& outMs, bool& outValid) {
#ifdef CHUDWARE_GLES3
    (void)t; (void)outMs; outValid = false;
#else
    glEndQuery(GL_TIME_ELAPSED);
    t.pending[t.cur] = true;
    const int other = t.cur ^ 1;
    t.cur = other;
    if (t.pending[other]) {
        GLuint avail = 0;
        glGetQueryObjectuiv(t.id[other], GL_QUERY_RESULT_AVAILABLE, &avail);
        if (avail) {
            GLuint64 ns = 0;
            glGetQueryObjectui64v(t.id[other], GL_QUERY_RESULT, &ns);
            outMs = (float)((double)ns / 1e6);
            outValid = true;
            t.pending[other] = false;
        }
        // Not yet available: never block — keep reporting the previous outMs this frame.
    }
#endif
}

GlassTimings Renderer::glassTimings() const {
    GlassTimings t;
    t.sceneMs = sceneMs_;
    t.blurMs = blurMs_;
    t.lumaMs = lumaMs_;
    t.valid = sceneTimingValid_ && blurTimingValid_ && lumaTimingValid_;
    return t;
}

void Renderer::setBackdropEnabled(bool on) {
    if (on && !backdropEnabled_) lumaSrcFbo_ = 0;  // refresh after a hidden/drawing workspace
    backdropEnabled_ = on;
}

// A9/A12: source selection + cadence for the async luminance readback. Called once per
// backdrop() frame while enabled; backdrop() brackets this with the luma GPU timer.
void Renderer::issueLumaReadback() const {
    if (!backdropEnabled_) return;  // disabled means no readback at all.

    // Select the source before polling: old-generation fences must never publish into a
    // resized viewport or a different quality step's blur output.
    const bool blurAvailable = blurW_ > 0 && blurH_ > 0 && downsampleProg_ && kawaseProg_;
    unsigned int srcFbo = 0;
    int srcW = 0, srcH = 0;
    if (blurAvailable) {
        // Match backdrop()'s final texture, including LOW's single (odd) blur pass.
        srcFbo = blurPingFbo_[(blurPasses_ - 1) & 1];
        srcW = blurW_;
        srcH = blurH_;
    } else {
        // Blur chain permanently unavailable (link failure / never sized) -> fall back to a
        // direct small read of the full resolved color buffer, throttled to every 8th frame so it
        // never becomes a per-frame full-res stall source; still async via the same PBO ring.
        if (!resolveFbo_ || rw_ <= 0 || rh_ <= 0) return;
        ++lumaFallbackCounter_;
        if ((lumaFallbackCounter_ % 8) != 0) return;
        srcFbo = resolveFbo_;
        srcW = rw_;
        srcH = rh_;
    }
    if (!srcFbo || srcW <= 0 || srcH <= 0) return;
    if (srcFbo != lumaSrcFbo_ || srcW != lumaSrcW_ || srcH != lumaSrcH_) {
        for (int i = 0; i < kLumaPboCount; ++i) {
            if (lumaFence_[i]) {
                glDeleteSync(static_cast<GLsync>(lumaFence_[i]));
                lumaFence_[i] = nullptr;
            }
            lumaPboPending_[i] = false;
        }
        lumaEverRead_ = false;
        lumaSrcFbo_ = srcFbo;
    }
    pollLumaPbos();
    issueLumaPboRead(srcFbo, srcW, srcH);
    // A replacement canvas invalidates contrast history. Do not publish a new pale native
    // backdrop while its only contrast information is still "unknown" (palette-white ink).
    // Establish its first grid from the actual rendered source; every subsequent read stays
    // asynchronous. A resize/source transition needs the same one-time bootstrap.
    if (!lumaEverRead_) pollLumaPbos(/*bootstrap=*/true);
}

// A11: issues one glReadPixels into the next PBO ring slot + a fence. Detects a source-size
// change (blur resize, or a switch between the blur/fallback sources) and re-specs every slot's
// storage before issuing, dropping any outstanding fences from the old generation WITHOUT mapping
// them first (their data no longer matches the new dimensions).
void Renderer::issueLumaPboRead(unsigned int srcFbo, int srcW, int srcH) const {
#ifdef __EMSCRIPTEN__
    // WebGL2 cannot map a PBO for readback. Read the active blur output into CPU scratch;
    // unavailable/failed reads must never be reduced as black pixels or a previous image.
    if (srcW <= 0 || srcH <= 0) return;
    // A synchronous glReadPixels forces a GPU pipeline flush (measurable jank on weak iGPUs),
    // so throttle unchanged-source reads. A resize/quality/source transition invalidates
    // lumaEverRead_ above and MUST refresh immediately instead of reusing the old grid.
    if (lumaEverRead_ && (++lumaWebCounter_ % 32) != 0) return;
    lumaSrcW_ = srcW;
    lumaSrcH_ = srcH;
    const size_t need = (size_t)srcW * (size_t)srcH * 4;
    if (lumaCpuBuf_.size() < need) lumaCpuBuf_.resize(need);
    glBindFramebuffer(GL_FRAMEBUFFER, srcFbo);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        lumaEverRead_ = false;
        return;
    }
    glReadPixels(0, 0, srcW, srcH, GL_RGBA, GL_UNSIGNED_BYTE, lumaCpuBuf_.data());
    const GLenum readError = glGetError();
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    if (readError != GL_NO_ERROR) {
        lumaEverRead_ = false;
        std::fprintf(stderr, "[glass] backdrop readback failed: GL error 0x%x\n",
                     static_cast<unsigned int>(readError));
        return;
    }
    const unsigned char* px = lumaCpuBuf_.data();
    const int stepX = std::max(1, (srcW + kLumaGridMaxW - 1) / kLumaGridMaxW);
    const int stepY = std::max(1, (srcH + kLumaGridMaxH - 1) / kLumaGridMaxH);
    const int gw = std::min(kLumaGridMaxW, (srcW + stepX - 1) / stepX);
    const int gh = std::min(kLumaGridMaxH, (srcH + stepY - 1) / stepY);
    lumaStaging_.assign((size_t)gw * (size_t)gh, 0.5f);
    lumaRgbStaging_.assign((size_t)gw * (size_t)gh * 3, 0.5f);
    for (int gy = 0; gy < gh; ++gy) {
        const int sy = std::max(0, srcH - 1 - gy * stepY);  // flip: grid row 0 = viewport top
        for (int gx = 0; gx < gw; ++gx) {
            const int sx = std::min(srcW - 1, gx * stepX);
            const unsigned char* p = px + ((size_t)sy * (size_t)srcW + (size_t)sx) * 4;
            const float r = p[0] / 255.0f, g = p[1] / 255.0f, b = p[2] / 255.0f;
            const size_t cell = (size_t)gy * (size_t)gw + (size_t)gx;
            lumaStaging_[cell] = 0.2126f * r + 0.7152f * g + 0.0722f * b;
            lumaRgbStaging_[cell * 3 + 0] = r;
            lumaRgbStaging_[cell * 3 + 1] = g;
            lumaRgbStaging_[cell * 3 + 2] = b;
        }
    }
    lumaGrid_.swap(lumaStaging_);
    lumaRgbGrid_.swap(lumaRgbStaging_);
    lumaW_ = gw; lumaH_ = gh; lumaEverRead_ = true;
    return;
#endif
    if (srcW != lumaSrcW_ || srcH != lumaSrcH_) {
        for (int i = 0; i < kLumaPboCount; ++i) {
            if (lumaFence_[i]) {
                glDeleteSync(static_cast<GLsync>(lumaFence_[i]));
                lumaFence_[i] = nullptr;
            }
            lumaPboPending_[i] = false;
            if (!lumaPbo_[i]) glGenBuffers(1, &lumaPbo_[i]);
            glBindBuffer(GL_PIXEL_PACK_BUFFER, lumaPbo_[i]);
            glBufferData(GL_PIXEL_PACK_BUFFER, (size_t)srcW * (size_t)srcH * 4, nullptr,
                         GL_STREAM_READ);
            lumaPboW_[i] = srcW;
            lumaPboH_[i] = srcH;
        }
        lumaSrcW_ = srcW;
        lumaSrcH_ = srcH;
    }

    const int slot = lumaPboNext_;
    lumaPboNext_ = (lumaPboNext_ + 1) % kLumaPboCount;
    if (lumaFence_[slot]) {
        // Slot still had an unread result (a slow GPU catching up) — drop it rather than block.
        glDeleteSync(static_cast<GLsync>(lumaFence_[slot]));
        lumaFence_[slot] = nullptr;
        lumaPboPending_[slot] = false;
    }

    glBindFramebuffer(GL_FRAMEBUFFER, srcFbo);
    glBindBuffer(GL_PIXEL_PACK_BUFFER, lumaPbo_[slot]);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, srcW, srcH, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);  // lands in the bound PBO
    lumaPboW_[slot] = srcW;
    lumaPboH_[slot] = srcH;
    lumaFence_[slot] = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
    lumaPboPending_[slot] = true;

    // A5: never leave the PACK buffer or FBO bound past this call.
    glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

// Poll every ring slot without blocking in steady state. A new source's first grid waits once
// so resumed native geometry and its glass contrast are published as the same actual image.
void Renderer::pollLumaPbos(bool bootstrap) const {
    // Oldest first: numeric slot order can publish an older frame after a newer one
    // when the ring wraps and several fences become ready together.
    for (int n = 0; n < kLumaPboCount; ++n) {
        const int i = (lumaPboNext_ + n) % kLumaPboCount;
        if (!lumaPboPending_[i] || !lumaFence_[i]) continue;
        const GLenum res = glClientWaitSync(static_cast<GLsync>(lumaFence_[i]),
                                            bootstrap ? GL_SYNC_FLUSH_COMMANDS_BIT : 0,
                                            bootstrap ? GL_TIMEOUT_IGNORED : 0);
        if (res == GL_ALREADY_SIGNALED || res == GL_CONDITION_SATISFIED) {
            glDeleteSync(static_cast<GLsync>(lumaFence_[i]));
            lumaFence_[i] = nullptr;
            lumaPboPending_[i] = false;
            reduceLumaPbo(i);
        } else if (res == GL_WAIT_FAILED) {
            glDeleteSync(static_cast<GLsync>(lumaFence_[i]));
            lumaFence_[i] = nullptr;
            lumaPboPending_[i] = false;
        }
        // GL_TIMEOUT_EXPIRED: still in flight — leave pending, poll again next frame.
    }
}

// Maps slot `slot`, downsamples by a fixed step to a <=64x40 Rec.709 grid (grid row 0 = the TOP
// of the viewport, matching averageLuminance()'s top-left rect convention — the source PBO rows
// are GL bottom-left-origin, so the vertical index is flipped here), then publishes it with a
// single vector swap (never a partial in-place mutation, per the no-tearing contract rule).
void Renderer::reduceLumaPbo(int slot) const {
    const int sw = lumaPboW_[slot];
    const int sh = lumaPboH_[slot];
    if (sw <= 0 || sh <= 0) return;

    glBindBuffer(GL_PIXEL_PACK_BUFFER, lumaPbo_[slot]);
#ifdef __EMSCRIPTEN__
    // WebGL2 has no MAP_READ on PIXEL_PACK_BUFFER. Leave prior luma grid.
    glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    return;
#else
    const void* mapped =
        glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, (size_t)sw * (size_t)sh * 4, GL_MAP_READ_BIT);
    if (mapped) {
        const unsigned char* px = static_cast<const unsigned char*>(mapped);
        const int stepX = std::max(1, (sw + kLumaGridMaxW - 1) / kLumaGridMaxW);
        const int stepY = std::max(1, (sh + kLumaGridMaxH - 1) / kLumaGridMaxH);
        const int gw = std::min(kLumaGridMaxW, (sw + stepX - 1) / stepX);
        const int gh = std::min(kLumaGridMaxH, (sh + stepY - 1) / stepY);
        lumaStaging_.assign((size_t)gw * (size_t)gh, 0.5f);
        lumaRgbStaging_.assign((size_t)gw * (size_t)gh * 3, 0.5f);  // mean-RGB twin (backdrop stats)
        for (int gy = 0; gy < gh; ++gy) {
            const int sy = std::max(0, sh - 1 - gy * stepY);  // flip: grid row 0 = viewport top
            for (int gx = 0; gx < gw; ++gx) {
                const int sx = std::min(sw - 1, gx * stepX);
                const unsigned char* p = px + ((size_t)sy * (size_t)sw + (size_t)sx) * 4;
                const float r = p[0] / 255.0f, g = p[1] / 255.0f, b = p[2] / 255.0f;
                const size_t cell = (size_t)gy * (size_t)gw + (size_t)gx;
                lumaStaging_[cell] = 0.2126f * r + 0.7152f * g + 0.0722f * b;
                lumaRgbStaging_[cell * 3 + 0] = r;
                lumaRgbStaging_[cell * 3 + 1] = g;
                lumaRgbStaging_[cell * 3 + 2] = b;
            }
        }
        glUnmapBuffer(GL_PIXEL_PACK_BUFFER);
        lumaGrid_.swap(lumaStaging_);  // one swap; a future reader never sees a torn grid
        lumaRgbGrid_.swap(lumaRgbStaging_);  // swapped in lockstep — the two grids never diverge
        lumaW_ = gw;
        lumaH_ = gh;
        lumaEverRead_ = true;
    }
    // A5: never leave the PACK buffer bound past this call.
    glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
#endif
}

unsigned int Renderer::resolvedColorTexture() const {
    resolveColorTexture();
    return resolvedTex_;
}

std::vector<unsigned char> Renderer::readRGBA() const {
    resolveColorTexture();

    std::vector<unsigned char> glPixels(rw_ * rh_ * 4);
    glBindFramebuffer(GL_FRAMEBUFFER, resolveFbo_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, rw_, rh_, GL_RGBA, GL_UNSIGNED_BYTE, glPixels.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // The texture is already box-resolved to presentation size; just flip to top-left origin.
    std::vector<unsigned char> out(rw_ * rh_ * 4);
    for (int y = 0; y < rh_; ++y) {
        const unsigned char* src = &glPixels[y * rw_ * 4];
        unsigned char* dst = &out[(rh_ - 1 - y) * rw_ * 4];
        std::copy(src, src + rw_ * 4, dst);
    }
    return out;
}
