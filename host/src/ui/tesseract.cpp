// tesseract.cpp — see tesseract.h. A real 4-cube, projected every frame.
//
// Pipeline per vertex:  v4 (±1,±1,±1,±1)  ->  4D rotation (per style)  ->  4D->3D projection
// (perspective or orthographic)  ->  fixed 3/4 camera (yaw + pitch)  ->  3D->2D perspective  ->
// screen. Edges are the 32 vertex pairs differing in exactly one coordinate, painted back-to-front
// with a depth-driven alpha/width ramp so the mark reads as a solid object at 16 px.
//
// The projection distances and the final scale are FITTED ONCE per style (see fitStyle) by
// sampling a full turn: a per-frame normalize-to-bounds would make the logo breathe, and a
// hand-picked constant would either clip on the near pass or float in dead space. The fit is a few
// thousand flops, computed on first use and cached.
#include "tesseract.h"

#include <cmath>

namespace ui {
namespace {

struct Vec4 { float x, y, z, w; };
struct Vec3 { float x, y, z; };

// ---- topology: 16 vertices of the 4-cube, 32 edges (Hamming distance 1) ------------------------
constexpr int kNumVerts = 16;
constexpr int kNumEdges = 32;

Vec4 unitVertex(int i) {
    return Vec4{(i & 1) ? 1.0f : -1.0f,
                (i & 2) ? 1.0f : -1.0f,
                (i & 4) ? 1.0f : -1.0f,
                (i & 8) ? 1.0f : -1.0f};
}

struct EdgeTable {
    int a[kNumEdges], b[kNumEdges];
    EdgeTable() {
        int n = 0;
        for (int i = 0; i < kNumVerts; ++i)
            for (int bit = 1; bit < 16; bit <<= 1) {
                const int j = i ^ bit;
                if (j > i) { a[n] = i; b[n] = j; ++n; }
            }
        // 16 vertices * 4 incident edges / 2 == 32; the j>i guard emits each exactly once.
    }
};
const EdgeTable& edges() { static const EdgeTable t; return t; }

// ---- 4D rotation -------------------------------------------------------------------------------

// Rotate `p` by `ang` inside the plane spanned by the ORTHONORMAL pair (u, v). The orthogonal
// complement of that plane is the invariant ("axis") plane, so this is a simple rotation.
inline Vec4 rotateInPlane(const Vec4& p, const Vec4& u, const Vec4& v, float ang) {
    const float a = p.x * u.x + p.y * u.y + p.z * u.z + p.w * u.w;
    const float b = p.x * v.x + p.y * v.y + p.z * v.z + p.w * v.w;
    const float c = std::cos(ang), s = std::sin(ang);
    const float da = (a * c - b * s) - a;
    const float db = (a * s + b * c) - b;
    return Vec4{p.x + da * u.x + db * v.x,
                p.y + da * u.y + db * v.y,
                p.z + da * u.z + db * v.z,
                p.w + da * u.w + db * v.w};
}

// The invariant plane of the "simple rotation" styles bisects the figure front-left to back-right
// (the horizontal diagonal (1,0,1,0), with +x right and +z into the screen) and top to bottom (the
// vertical axis (0,1,0,0)). What turns is therefore its orthogonal complement: the plane spanned by
// the other horizontal diagonal (1,0,-1,0) and the fourth axis w.
const float kInvSqrt2 = 0.70710678f;
constexpr Vec4 kRotU_raw{1.0f, 0.0f, -1.0f, 0.0f};
inline Vec4 simpleRotU() { return Vec4{kRotU_raw.x * kInvSqrt2, 0.0f, kRotU_raw.z * kInvSqrt2, 0.0f}; }
inline Vec4 simpleRotV() { return Vec4{0.0f, 0.0f, 0.0f, 1.0f}; }

// Double rotation: two independent turns in the orthogonal coordinate planes xy and zw. The rates
// are deliberately incommensurate-ish (golden ratio) so the two planes never realign and the
// figure never settles into a short loop. No point of the figure is fixed for more than an
// instant: the xy plane momentarily returns to identity once per xy revolution and the zw plane
// once per zw revolution, but the two never coincide (except at t = 0), so unlike a simple
// rotation there is no direction that survives the whole animation.
const float kDoubleRateB = 0.6180340f;

// `a2` is only read by the double rotation; the simple styles ignore it.
Vec4 animate(const Vec4& p, TesseractAnim anim, float a1, float a2) {
    switch (anim) {
        case TesseractAnim::DoubleRotation: {
            const float c1 = std::cos(a1), s1 = std::sin(a1);
            const float c2 = std::cos(a2), s2 = std::sin(a2);
            return Vec4{p.x * c1 - p.y * s1,
                        p.x * s1 + p.y * c1,
                        p.z * c2 - p.w * s2,
                        p.z * s2 + p.w * c2};
        }
        case TesseractAnim::SimpleRotation:
        case TesseractAnim::EightCell:
        default:
            return rotateInPlane(p, simpleRotU(), simpleRotV(), a1);
    }
}

// The 8-cell style is the same rotation seen through an orthographic 4D->3D projection: with no
// w divide the two bounding cubes slide across one another instead of nesting.
inline bool orthographic4D(TesseractAnim anim) { return anim == TesseractAnim::EightCell; }

// ---- projection --------------------------------------------------------------------------------

// Fixed 3/4 camera. Modest angles: at a 16 px header mark a full isometric tilt turns the edge
// bundle into mush, while a head-on view hides the front-left/back-right diagonal the simple
// rotation is defined around.
const float kCamYaw   = 0.55f;   // about +y
const float kCamPitch = 0.32f;   // about +x

inline Vec3 camera(const Vec3& p) {
    const float cy = std::cos(kCamYaw), sy = std::sin(kCamYaw);
    const float cp = std::cos(kCamPitch), sp = std::sin(kCamPitch);
    const float x1 =  p.x * cy + p.z * sy;
    const float z1 = -p.x * sy + p.z * cy;
    const float y2 =  p.y * cp - z1 * sp;
    const float z2 =  p.y * sp + z1 * cp;
    return Vec3{x1, y2, z2};
}

// Fitted constants for one style: the two projection eye distances and the scale that makes the
// widest frame of a full turn exactly fill the requested box.
struct StyleFit {
    float d4 = 4.0f;    // 4D eye distance (unused when the style projects orthographically)
    float d3 = 8.0f;    // 3D eye distance
    float scale = 1.0f; // unit box half-extent -> 1.0
    float depth = 1.0f; // max |camera z|, for the depth ramp
};

inline Vec3 project4to3(const Vec4& p, float d4, bool ortho) {
    if (ortho) return Vec3{p.x, p.y, p.z};
    const float k = d4 / (d4 - p.w);
    return Vec3{p.x * k, p.y * k, p.z * k};
}

inline ImVec2 project3to2(const Vec3& p, float d3) {
    // +z points into the screen, so a larger z is farther away and shrinks.
    const float k = d3 / (d3 + p.z);
    return ImVec2(p.x * k, p.y * k);
}

StyleFit fitStyle(TesseractAnim anim) {
    const bool ortho = orthographic4D(anim);
    const bool dbl   = anim == TesseractAnim::DoubleRotation;
    // A simple rotation's configuration space is a circle, so 96 samples of one turn see every
    // frame it will ever show. The double rotation's is a 2-TORUS: its two planes turn at
    // incommensurate rates and never realign, so sampling only the line a2 = 0.618*a1 would
    // under-measure the widest frame and the mark would eventually clip out of its box.
    const int n1 = dbl ? 48 : 96;
    const int n2 = dbl ? 48 : 1;
    const float st1 = 6.2831853f / (float)n1;
    const float st2 = dbl ? 6.2831853f / (float)n2 : 0.0f;
    StyleFit fit;

    // Pass A — how far out of the 3-space does the figure reach? Sets the 4D eye distance so every
    // style gets the SAME perspective strength (eye at 2.4x the deepest vertex) instead of an
    // arbitrary literal that would either flatten one style or blow up near the pole.
    if (!ortho) {
        float maxW = 0.0f;
        for (int s1 = 0; s1 < n1; ++s1)
            for (int s2 = 0; s2 < n2; ++s2)
                for (int i = 0; i < kNumVerts; ++i) {
                    const float w =
                        std::fabs(animate(unitVertex(i), anim, s1 * st1, s2 * st2).w);
                    if (w > maxW) maxW = w;
                }
        fit.d4 = maxW * 2.4f;
    }

    // Pass B — the 3D extent after the 4D projection sets the 3D eye distance (mild perspective)
    // and the depth-ramp normalizer.
    float maxR3 = 0.0f, maxZ = 0.0f;
    for (int s1 = 0; s1 < n1; ++s1)
        for (int s2 = 0; s2 < n2; ++s2)
            for (int i = 0; i < kNumVerts; ++i) {
                const Vec3 c = camera(project4to3(
                    animate(unitVertex(i), anim, s1 * st1, s2 * st2), fit.d4, ortho));
                const float r = std::sqrt(c.x * c.x + c.y * c.y + c.z * c.z);
                if (r > maxR3) maxR3 = r;
                if (std::fabs(c.z) > maxZ) maxZ = std::fabs(c.z);
            }
    if (maxR3 < 1e-4f) maxR3 = 1.0f;
    fit.d3 = maxR3 * 3.0f;
    // Rounded up a hair: `depth` normalizes the painter's depth ramp, and a discrete sample grid
    // can miss the true extremum by a fraction of a percent. Keeping the denominator on the
    // generous side keeps near01 inside [0,1] without a clamp in the per-edge inner loop.
    fit.depth = maxZ > 1e-4f ? maxZ * 1.01f : 1.0f;

    // Pass C — the widest frame of the whole animation fills the box exactly.
    float maxR2 = 0.0f;
    for (int s1 = 0; s1 < n1; ++s1)
        for (int s2 = 0; s2 < n2; ++s2)
            for (int i = 0; i < kNumVerts; ++i) {
                const ImVec2 q = project3to2(
                    camera(project4to3(animate(unitVertex(i), anim, s1 * st1, s2 * st2),
                                       fit.d4, ortho)),
                    fit.d3);
                const float r = std::sqrt(q.x * q.x + q.y * q.y);
                if (r > maxR2) maxR2 = r;
            }
    // Same reason as `depth`: a hair of slack so the sampled maximum is never beaten at runtime.
    fit.scale = maxR2 > 1e-4f ? (1.0f / (maxR2 * 1.005f)) : 1.0f;
    return fit;
}

const StyleFit& styleFit(TesseractAnim anim) {
    static const StyleFit s_fit[(int)TesseractAnim::Count] = {
        fitStyle(TesseractAnim::SimpleRotation),
        fitStyle(TesseractAnim::DoubleRotation),
        fitStyle(TesseractAnim::EightCell),
    };
    return s_fit[(int)anim];
}

// Radians per second. Slow enough that the mark is ambient rather than attention-grabbing, fast
// enough that a glance shows it is alive. Busy indicators want obvious motion instead, so they
// run the same figure roughly three times faster (see drawTesseractSpinner).
const float kSpin     = 0.62f;
const float kSpinBusy = 1.9f;

// Below this drawn size the 8 edges joining the two bounding cells are shorter than the stroke is
// wide and just crosshatch the interior; the 24 that remain still read as an angular solid in
// motion. Picked off a rendered size ladder at 10/12/14/16/20/24/32 px: the reduced set is
// visibly cleaner from 18 px down, and indistinguishable above it.
const float kLodDropWAxisPx = 18.0f;

// Phase origin per style. The 8-cell's orthographic projection collapses the two bounding cubes
// whenever sin(theta) == 0, so at theta = 0 it is momentarily a plain 3-cube (8 of its 16 vertices
// coincide). That frame is authentic mid-animation, but it is the WRONG frame to open the app (or
// a settings preview) on, so its phase starts a third of the way into the opening quarter-turn.
inline float stylePhase(TesseractAnim anim) {
    return anim == TesseractAnim::EightCell ? 1.0471976f /* pi/3 */ : 0.0f;
}

// The two plane angles at a wall-clock time. Each is wrapped INDEPENDENTLY. Deriving the second
// from the already-wrapped first is the obvious shortcut and it is wrong: the zw plane would snap
// back 3.88 rad on every xy revolution, teleporting vertices ~13 px on a 32 px mark every 10 s.
// Not wrapping at all is also wrong: a float angle accumulated from uptime steps by 0.1 deg after
// a few hours, which reads as a stutter.
inline void animAngles(TesseractAnim anim, double timeSec, float& a1, float& a2) {
    const double kTau = 6.283185307179586;
    const double base = timeSec * (double)kSpin + (double)stylePhase(anim);
    a1 = (float)std::fmod(base, kTau);
    a2 = anim == TesseractAnim::DoubleRotation
             ? (float)std::fmod(base * (double)kDoubleRateB, kTau)
             : 0.0f;
}

// Shared painter. `dropWAxis` omits the 8 edges along the 4th axis (the ones joining the two
// bounding cells); below ~14 px those edges are shorter than the stroke is wide and the figure
// collapses into a solid patch, whereas the 24 that remain still read as a cube inside a cube.
void paint(ImDrawList* dl, ImVec2 center, float size, TesseractAnim anim, float a1, float a2,
           ImU32 col, float thickness, bool dropWAxis) {
    const StyleFit& fit = styleFit(anim);
    const bool  ortho = orthographic4D(anim);
    // 0.030 of the box: measured against rendered filmstrips at 96 px and 20 px. Anything near
    // 0.05 fuses the 32 edges into a white blob at header size.
    const float baseW = thickness > 0.0f ? thickness
                                         : (size * 0.030f < 1.0f ? 1.0f : size * 0.030f);
    // `size` is the box the caller gave us, and the fit puts the outermost vertex exactly on its
    // edge — so a stroke centred there would hang half its width outside. Inset by that half so
    // the INK fits the slot, which is what every caller actually means. The floor keeps a
    // pathological `thickness` from inverting the figure.
    const float rBox = size * 0.5f;
    const float rHalf = rBox - baseW * 0.5f;
    const float r = rHalf > rBox * 0.5f ? rHalf : rBox * 0.5f;

    ImVec2 pt[kNumVerts];
    float  depth[kNumVerts];   // camera z, +into the screen
    for (int i = 0; i < kNumVerts; ++i) {
        const Vec3 c = camera(project4to3(animate(unitVertex(i), anim, a1, a2), fit.d4, ortho));
        const ImVec2 q = project3to2(c, fit.d3);
        // ImGui screen space is y-down; the model is y-up.
        pt[i] = ImVec2(center.x + q.x * fit.scale * r, center.y - q.y * fit.scale * r);
        depth[i] = c.z;
    }

    // Back-to-front so the near edges of the near cell overlay the far ones. 32 items, already
    // near-sorted frame to frame — insertion sort is the right tool and costs nothing.
    const EdgeTable& e = edges();
    int   order[kNumEdges];
    float ez[kNumEdges];
    int   n = 0;
    for (int k = 0; k < kNumEdges; ++k) {
        if (dropWAxis && (e.a[k] ^ e.b[k]) == 8) continue;
        order[n] = k;
        ez[k] = (depth[e.a[k]] + depth[e.b[k]]) * 0.5f;
        ++n;
    }
    for (int k = 1; k < n; ++k) {
        const int   key = order[k];
        const float kz  = ez[key];
        int j = k - 1;
        while (j >= 0 && ez[order[j]] < kz) { order[j + 1] = order[j]; --j; }
        order[j + 1] = key;
    }

    const float alphaBase = (float)((col >> IM_COL32_A_SHIFT) & 0xFF) / 255.0f;
    const ImU32 rgb = col & ~IM_COL32_A_MASK;
    for (int k = 0; k < n; ++k) {
        const int idx = order[k];
        // near == -depth; map [-depth,+depth] -> [1,0] and ramp both alpha and stroke so the
        // silhouette stays legible while the far cell recedes instead of muddying it.
        const float near01 = 0.5f - ez[idx] / (2.0f * fit.depth);
        const float a = alphaBase * (0.34f + 0.66f * near01);
        const float w = baseW * (0.75f + 0.45f * near01);
        dl->AddLine(pt[e.a[idx]], pt[e.b[idx]],
                    rgb | ((ImU32)(a * 255.0f + 0.5f) << IM_COL32_A_SHIFT),
                    w < 1.0f ? 1.0f : w);
    }
}

}  // namespace

const char* tesseractAnimName(TesseractAnim anim) {
    switch (anim) {
        case TesseractAnim::DoubleRotation: return "Double rotation";
        case TesseractAnim::EightCell:      return "8-cell";
        case TesseractAnim::SimpleRotation:
        default:                            return "Simple rotation";
    }
}

const char* tesseractAnimBlurb(TesseractAnim anim) {
    switch (anim) {
        case TesseractAnim::DoubleRotation:
            return "Double rotation about two orthogonal planes in 4-dimensional space.";
        case TesseractAnim::EightCell:
            return "An 8-cell in a simple rotation about the same plane, projected without "
                   "4D perspective \xe2\x80\x94 the eight cubic cells slide through one another.";
        case TesseractAnim::SimpleRotation:
        default:
            return "Simple rotation about a plane that bisects the figure from front-left to "
                   "back-right and top to bottom.";
    }
}

void drawTesseract(ImDrawList* dl, ImVec2 center, float size, double timeSec,
                   TesseractAnim anim, ImU32 col, float thickness) {
    if (!dl || size <= 1.0f) return;
    if (anim < TesseractAnim::SimpleRotation || anim >= TesseractAnim::Count)
        anim = TesseractAnim::SimpleRotation;
    float a1, a2;
    animAngles(anim, timeSec, a1, a2);
    paint(dl, center, size, anim, a1, a2, col, thickness, size < kLodDropWAxisPx);
}

void drawTesseractSpinner(ImDrawList* dl, ImVec2 center, float size, double timeSec, ImU32 col,
                          float thickness) {
    if (!dl || size <= 1.0f) return;
    // Always the simple rotation, never the user's brand-mark choice: a busy indicator has to
    // read instantly at 14 px, and it is drawn from leaf widgets that have no Settings in scope.
    const TesseractAnim anim = TesseractAnim::SimpleRotation;
    const float ang = (float)std::fmod(timeSec * (double)kSpinBusy, 6.283185307179586);
    paint(dl, center, size, anim, ang, 0.0f, col, thickness, size < kLodDropWAxisPx);
}

}  // namespace ui
