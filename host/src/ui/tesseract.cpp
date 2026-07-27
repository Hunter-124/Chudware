// tesseract.cpp — see tesseract.h. A real 4-cube, projected every frame.
//
// Pipeline per vertex:  v4 (±1,±1,±1,±1)  ->  4D rotation (per style)  ->  4D->3D perspective
// projection  ->  fixed 3/4 camera (yaw + pitch)  ->  3D->2D perspective  ->  screen. Edges are
// the 32 vertex pairs differing in exactly one coordinate, painted back-to-front with a
// depth-driven alpha/width ramp so the mark reads as a solid object at 16 px.
//
// A STYLE IS DATA, NOT CODE: each one is a short list of rotations in coordinate planes of
// 4-space driven by one or two independent phases (see AnimDef). One rotation mechanism, one
// fitter and one painter cover every style — adding a style is adding a table row.
//
// The projection distances and the final scale are FITTED ONCE per style (see fitStyle) by
// sampling the style's whole configuration space: a per-frame normalize-to-bounds would make the
// logo breathe, and a hand-picked constant would either clip on the near pass or float in dead
// space. The fit is a few thousand flops, computed on first use and cached.
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

// ---- animation styles ---------------------------------------------------------------------------
// One rotation in a coordinate plane of 4-space (axes x, y, z, w = 0..3), driven by one of the
// style's independent phases: angle = phase[spin.phase] * spin.rate.
struct PlaneSpin { int i, j; int phase; float rate; };
struct AnimDef {
    PlaneSpin spins[2];
    int     nSpins;
    float   phaseRate[2];   // phase p advances at kSpin * phaseRate[p]
    int     nPhases;
};

// Golden-ratio conjugate, incommensurate with 1: the double rotation's two planes never realign,
// so the motion has no short loop and — unlike a simple rotation — no direction of the figure
// stays fixed through it.
const float kDoubleRateB = 0.6180340f;

// EightCell      A SIMPLE rotation in the zw plane: the fourth axis folds through the viewing
//                direction, so the inner cell swells toward the camera, swallows the outer one
//                and the figure turns itself inside out — the canonical tesseract motion.
//                (Replaces the earlier bisecting-plane rotation fitted against 8-cell-simple.gif,
//                which kept a whole plane of the figure fixed and read as a flat shimmer at logo
//                size.)
//
// DoubleRotation A DOUBLE rotation: independent turns in the orthogonal coordinate planes xy and
//                zw at incommensurate rates (kDoubleRateB). The two planes momentarily return to
//                identity once per own revolution but never coincide (except at t = 0).
//
// Isoclinic      A double rotation whose two planes turn in LOCKSTEP off a single phase. Every
//                vertex then moves at the same speed along a circle — a Hopf fibre — so the
//                figure rolls smoothly and never turns inside out.
const AnimDef& animDef(TesseractAnim anim) {
    static const AnimDef defs[(int)TesseractAnim::Count] = {
        /* EightCell      */ { {{2, 3, 0, 1.0f}},                  1, {1.0f, 0.0f},       1 },
        /* DoubleRotation */ { {{0, 1, 0, 1.0f}, {2, 3, 1, 1.0f}}, 2, {1.0f, kDoubleRateB}, 2 },
        /* Isoclinic      */ { {{0, 1, 0, 1.0f}, {2, 3, 0, 1.0f}}, 2, {1.0f, 0.0f},       1 },
    };
    return defs[(int)anim];
}

// Rotate `p` by `ang` inside coordinate plane (i, j); the other two axes are untouched.
inline Vec4 rotatePlane(Vec4 p, int i, int j, float ang) {
    const float c = std::cos(ang), s = std::sin(ang);
    float* q = &p.x;   // Vec4 is x, y, z, w in declaration order
    const float a = q[i], b = q[j];
    q[i] = a * c - b * s;
    q[j] = a * s + b * c;
    return p;
}

Vec4 animate(Vec4 p, const AnimDef& def, const float* phase) {
    for (int s = 0; s < def.nSpins; ++s) {
        const PlaneSpin& e = def.spins[s];
        p = rotatePlane(p, e.i, e.j, phase[e.phase] * e.rate);
    }
    return p;
}

// ---- projection --------------------------------------------------------------------------------

// Fixed camera, fitted against the reference animation's frames (see tesseract.h). Only slightly
// off head-on: face-on the two nested cells sit concentric and the turn reads as a flat shimmer,
// while a full 3/4 isometric tilt buries the inside-out motion and turns the edge bundle to mush
// at header size.
const float kCamYaw   = 0.35f;   // about +y
const float kCamPitch = 0.22f;   // about +x

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
// widest frame of the whole animation exactly fill the requested box.
struct StyleFit {
    float d4;    // 4D eye distance
    float d3;    // 3D eye distance
    float scale; // unit box half-extent -> 1.0
    float depth; // max |camera z|, for the depth ramp
};

inline Vec3 project4to3(const Vec4& p, float d4) {
    const float k = d4 / (d4 - p.w);
    return Vec3{p.x * k, p.y * k, p.z * k};
}

inline ImVec2 project3to2(const Vec3& p, float d3) {
    // +z points into the screen, so a larger z is farther away and shrinks.
    const float k = d3 / (d3 + p.z);
    return ImVec2(p.x * k, p.y * k);
}

StyleFit fitStyle(const AnimDef& def) {
    // Sample every phase over its full circle. One phase (8-cell, isoclinic) makes the
    // configuration space a circle — 96 samples see every frame it will ever show. Two phases
    // (double rotation) make it a 2-TORUS: the planes turn at incommensurate rates and never
    // realign, so sampling only the trajectory line a2 = 0.618*a1 would under-measure the widest
    // frame and the mark would eventually clip out of its box.
    const int n1 = def.nPhases > 1 ? 48 : 96;
    const int n2 = def.nPhases > 1 ? 48 : 1;
    const float st1 = 6.2831853f / (float)n1;
    const float st2 = def.nPhases > 1 ? 6.2831853f / (float)n2 : 0.0f;
    StyleFit fit{};

    // Pass A — how far out of the 3-space does the figure reach? Sets the 4D eye distance so all
    // styles get the SAME perspective strength instead of an arbitrary literal that would either
    // flatten one of them or blow up near the pole. The 1.63 multiplier gives the near cell
    // roughly twice the far cell's projected size — enough that the inside-out reads at header
    // size without a near-pole blow-up (d4 - max|w| stays ~0.89).
    float maxW = 0.0f;
    for (int s1 = 0; s1 < n1; ++s1)
        for (int s2 = 0; s2 < n2; ++s2) {
            const float phase[2] = {s1 * st1, s2 * st2};
            for (int i = 0; i < kNumVerts; ++i) {
                const float w = std::fabs(animate(unitVertex(i), def, phase).w);
                if (w > maxW) maxW = w;
            }
        }
    fit.d4 = maxW * 1.63f;

    // Pass B — the 3D extent after the 4D projection sets the 3D eye distance (mild perspective)
    // and the depth-ramp normalizer.
    float maxR3 = 0.0f, maxZ = 0.0f;
    for (int s1 = 0; s1 < n1; ++s1)
        for (int s2 = 0; s2 < n2; ++s2) {
            const float phase[2] = {s1 * st1, s2 * st2};
            for (int i = 0; i < kNumVerts; ++i) {
                const Vec3 c = camera(project4to3(animate(unitVertex(i), def, phase), fit.d4));
                const float r = std::sqrt(c.x * c.x + c.y * c.y + c.z * c.z);
                if (r > maxR3) maxR3 = r;
                if (std::fabs(c.z) > maxZ) maxZ = std::fabs(c.z);
            }
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
        for (int s2 = 0; s2 < n2; ++s2) {
            const float phase[2] = {s1 * st1, s2 * st2};
            for (int i = 0; i < kNumVerts; ++i) {
                const ImVec2 q = project3to2(
                    camera(project4to3(animate(unitVertex(i), def, phase), fit.d4)), fit.d3);
                const float r = std::sqrt(q.x * q.x + q.y * q.y);
                if (r > maxR2) maxR2 = r;
            }
        }
    // Same reason as `depth`: a hair of slack so the sampled maximum is never beaten at runtime.
    fit.scale = maxR2 > 1e-4f ? (1.0f / (maxR2 * 1.005f)) : 1.0f;
    return fit;
}

const StyleFit& styleFit(TesseractAnim anim) {
    static const StyleFit s_fit[(int)TesseractAnim::Count] = {
        fitStyle(animDef(TesseractAnim::EightCell)),
        fitStyle(animDef(TesseractAnim::DoubleRotation)),
        fitStyle(animDef(TesseractAnim::Isoclinic)),
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

// The phase angles at a wall-clock time, each wrapped INDEPENDENTLY in double. Deriving a later
// phase from the already-wrapped first is the obvious shortcut and it is wrong: the zw plane
// would snap back 3.88 rad on every xy revolution, teleporting vertices ~13 px on a 32 px mark
// every 10 s. Not wrapping at all is also wrong: a float angle accumulated from uptime steps by
// 0.1 deg after a few hours, which reads as a stutter.
inline void animAngles(const AnimDef& def, double timeSec, float* phase) {
    const double kTau = 6.283185307179586;
    for (int p = 0; p < def.nPhases; ++p)
        phase[p] = (float)std::fmod(timeSec * (double)(kSpin * def.phaseRate[p]), kTau);
}

// Shared painter. `dropWAxis` omits the 8 edges along the 4th axis (the ones joining the two
// bounding cells); below ~14 px those edges are shorter than the stroke is wide and the figure
// collapses into a solid patch, whereas the 24 that remain still read as a cube inside a cube.
void paint(ImDrawList* dl, ImVec2 center, float size, const AnimDef& def, const StyleFit& fit,
           const float* phase, ImU32 col, float thickness, bool dropWAxis) {
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
        const Vec3 c = camera(project4to3(animate(unitVertex(i), def, phase), fit.d4));
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
        case TesseractAnim::Isoclinic:      return "Isoclinic";
        case TesseractAnim::EightCell:
        default:                            return "8-cell";
    }
}

const char* tesseractAnimBlurb(TesseractAnim anim) {
    switch (anim) {
        case TesseractAnim::DoubleRotation:
            return "A 3D projection of a tesseract performing a double rotation about two "
                   "orthogonal planes in 4-dimensional space.";
        case TesseractAnim::Isoclinic:
            return "An isoclinic rotation: both orthogonal 4D planes turn in lockstep, so every "
                   "vertex traces a perfect circle and the figure rolls smoothly without ever "
                   "turning inside out.";
        case TesseractAnim::EightCell:
        default:
            return "A simple 4D rotation that folds the fourth axis through the viewing "
                   "direction: the inner cube swells toward you, swallows the outer one, and the "
                   "figure turns itself inside out.";
    }
}

void drawTesseract(ImDrawList* dl, ImVec2 center, float size, double timeSec,
                   TesseractAnim anim, ImU32 col, float thickness) {
    if (!dl || size <= 1.0f) return;
    if (anim < TesseractAnim::EightCell || anim >= TesseractAnim::Count)
        anim = TesseractAnim::EightCell;
    const AnimDef& def = animDef(anim);
    float phase[2];
    animAngles(def, timeSec, phase);
    paint(dl, center, size, def, styleFit(anim), phase, col, thickness,
          size < kLodDropWAxisPx);
}

void drawTesseractSpinner(ImDrawList* dl, ImVec2 center, float size, double timeSec, ImU32 col,
                          float thickness) {
    if (!dl || size <= 1.0f) return;
    // Always the 8-cell's inside-out rotation, never the user's brand-mark choice: a busy
    // indicator has to read instantly at 14 px, and it is drawn from leaf widgets with no
    // Settings in scope.
    const AnimDef& def = animDef(TesseractAnim::EightCell);
    const float phase[1] = {(float)std::fmod(timeSec * (double)kSpinBusy, 6.283185307179586)};
    paint(dl, center, size, def, styleFit(TesseractAnim::EightCell), phase, col, thickness,
          size < kLodDropWAxisPx);
}

}  // namespace ui
