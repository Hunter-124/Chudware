// Sketch mode UI (C18) implementation. See sketch_mode.h for the design overview.
#include "sketch_mode.h"

#include "camera_anim.h"   // C34: restore the pre-sketch camera on exit

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <set>
#include <unordered_set>

#include "../../extern/mapbox/earcut.hpp"   // C56c: hole-aware region triangulation

#include "../core/app_state.h"
#include "../core/sketch_consumers.h"
#include "../renderer.h"
#include "../worker_client.h"
#include "../ui_theme.h"
#include "icons.h"              // drawIconGlass (per-tool cursor badge)
#include "layout.h"             // ui::overlayColumnEdge (sketch overlay dodges the pinned columns)
#include "viewport_panel.h"     // ui::viewportClearRect (overlay clip + HUD anchor)
#include "ribbon_def.h"         // getRibbonCmds (armed-tool cmd id -> icon name)
#include "property_manager.h"   // pmParseDim (Smart Dimension value parsing)
#include "airfoil.h"            // NACA 4-/5-digit + .dat profile generator (SketchTool::Airfoil)
#include "file_dialog.h"        // openFileDialog (.dat import)
#include "widgets/kit.h"        // ui::kit primitives + bounded text elision

namespace ui {

namespace {
extern bool g_skIps;
std::string fmtLen(double mm);
std::string fmtAng(double deg);

const char* modelPickKind(render::PickKind kind) {
    return kind == render::PickKind::Vertex ? "vertex" :
           kind == render::PickKind::Edge ? "edge" :
           kind == render::PickKind::Face ? "face" : nullptr;
}

// Selection observers have already resolved most picks asynchronously. Only a genuinely new,
// tool-owned click needs the synchronous fallback; never resolve a populated selector again.
nlohmann::json modelPickReference(AppState& st, const render::PickHit& hit, std::string& error) {
    const char* kind = modelPickKind(hit.kind);
    if (!kind) return {};
    std::string text;
    for (const auto& item : st.selection.items())
        if (item.hit.kind == hit.kind && item.hit.body_id == hit.body_id &&
            item.hit.topo_id == hit.topo_id && !item.selector.empty()) {
            text = item.selector;
            break;
        }
    if (text.empty()) text = st.docMirror.resolvePick(st.worker, hit.body_id, kind, hit.topo_id, error);
    auto selector = nlohmann::json::parse(text, nullptr, false);
    return selector.is_object() ? nlohmann::json{{"kind", kind}, {"selector", std::move(selector)}} :
                                  nlohmann::json();
}

// M-1: an interpolating "spline" entity passes THROUGH its points, so drawing straight
// segments between them makes a smooth curve look sharp-cornered — and a smooth swept/
// revolved path then doesn't match what the viewport showed. Densify the through-points
// with a centripetal Catmull-Rom so the viewport shows the ACTUAL curve. (style_spline is
// a by-poles curve whose points are control poles — keep showing its control polygon, the
// SolidWorks convention.) Returns UV points (same space as the input through-points).
inline std::vector<ImVec2> catmullRomUV(const std::vector<ImVec2>& p, int perSeg = 16) {
    if (p.size() < 3) return p;  // 2 points = a straight line already
    std::vector<ImVec2> out;
    out.reserve((p.size() - 1) * perSeg + 1);
    auto at = [&](int i) -> ImVec2 {
        return p[(size_t)std::min(std::max(i, 0), (int)p.size() - 1)];
    };
    for (size_t i = 0; i + 1 < p.size(); ++i) {
        ImVec2 p0 = at((int)i - 1), p1 = p[i], p2 = p[i + 1], p3 = at((int)i + 2);
        for (int s = 0; s < perSeg; ++s) {
            float t = s / (float)perSeg, t2 = t * t, t3 = t2 * t;
            // Uniform Catmull-Rom basis (tension 0.5).
            float bx = 0.5f * ((2 * p1.x) + (-p0.x + p2.x) * t +
                               (2 * p0.x - 5 * p1.x + 4 * p2.x - p3.x) * t2 +
                               (-p0.x + 3 * p1.x - 3 * p2.x + p3.x) * t3);
            float by = 0.5f * ((2 * p1.y) + (-p0.y + p2.y) * t +
                               (2 * p0.y - 5 * p1.y + 4 * p2.y - p3.y) * t2 +
                               (-p0.y + 3 * p1.y - 3 * p2.y + p3.y) * t3);
            out.push_back(ImVec2(bx, by));
        }
    }
    out.push_back(p.back());
    return out;
}

constexpr float kSnapPx = 6.0f;        // endpoint coincident snap radius (screen px)
constexpr float kRimInlinePx = 14.0f;  // rim point inline with origin/center: reach along rim (px)
constexpr float kHandlePx = 8.0f;      // drag hit-test radius (screen px)
constexpr float kHVDeg = 5.0f;         // horizontal/vertical auto-relation tolerance (deg)
constexpr float kGridStep = 10.0f;     // sketch grid spacing (mm)
// C49 inference tolerances:
constexpr float kInferHVDeg = 4.0f;    // Line rubber-band h/v direction-snap tolerance (deg)
constexpr float kTangentDeg = 6.0f;    // Line-on-rim tangent direction-snap tolerance (deg)

// C49: toolSnaps_ entries are either a plain coincident point handle ("e3.p2" / "e4.center" /
// "e5.p"), or a prefixed inference snap: "mid:<lineId>" (line midpoint), "rim:<circleId>"
// (point on a circle/arc rim), "tan:<circleId>" (on-rim + tangent direction matched),
// "align:<uHandle>|<vHandle>" (vertical/horizontal alignment guide). A rim/tan record may carry
// an alignment rider after ';' ("rim:e5;align:|origin" = on e5's rim, inline with the origin);
// snapPrimary() strips it. coincRef() returns the handle only for the plain coincident form.
inline bool snapHasPrefix(const std::string& s, const char* pfx) { return s.rfind(pfx, 0) == 0; }
inline std::string snapPrimary(const std::string& s) { return s.substr(0, s.find(';')); }
inline std::string coincRef(const std::string& s) {
    if (snapHasPrefix(s, "mid:") || snapHasPrefix(s, "rim:") || snapHasPrefix(s, "tan:") ||
        snapHasPrefix(s, "align:"))
        return std::string();
    return s;
}

// Project a world point through the camera MVP to screen pixels inside [imgMin, imgMin+(vw,vh)].
// The viewport image is drawn flipped vertically (UV 0,1 -> 1,0), so screen-y uses (1 - ndcY).
// Returns false if the point is behind the camera.
bool worldToScreen(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
                   float wx, float wy, float wz, ImVec2& out) {
    const float* m = cam.mvp.m;  // column-major: clip[r] = sum_c m[c*4+r]*p[c]
    float p[4] = {wx, wy, wz, 1.0f};
    float c[4];
    for (int r = 0; r < 4; ++r)
        c[r] = m[0 * 4 + r] * p[0] + m[1 * 4 + r] * p[1] + m[2 * 4 + r] * p[2] + m[3 * 4 + r] * p[3];
    if (c[3] <= 1e-6f) return false;
    float ndcX = c[0] / c[3];
    float ndcY = c[1] / c[3];
    out.x = imgMin.x + (ndcX * 0.5f + 0.5f) * (float)vw;
    out.y = imgMin.y + (1.0f - (ndcY * 0.5f + 0.5f)) * (float)vh;
    return true;
}

// Inverse of a column-major 4x4 (OpenGL convention) — the classic cofactor invert. Returns false
// for a singular matrix. Used to unproject the cursor into a world-space ray.
bool invert4x4(const float* m, float* inv) {
    inv[0]  =  m[5]*m[10]*m[15] - m[5]*m[11]*m[14] - m[9]*m[6]*m[15] + m[9]*m[7]*m[14] + m[13]*m[6]*m[11] - m[13]*m[7]*m[10];
    inv[4]  = -m[4]*m[10]*m[15] + m[4]*m[11]*m[14] + m[8]*m[6]*m[15] - m[8]*m[7]*m[14] - m[12]*m[6]*m[11] + m[12]*m[7]*m[10];
    inv[8]  =  m[4]*m[9]*m[15]  - m[4]*m[11]*m[13] - m[8]*m[5]*m[15] + m[8]*m[7]*m[13] + m[12]*m[5]*m[11] - m[12]*m[7]*m[9];
    inv[12] = -m[4]*m[9]*m[14]  + m[4]*m[10]*m[13] + m[8]*m[5]*m[14] - m[8]*m[6]*m[13] - m[12]*m[5]*m[10] + m[12]*m[6]*m[9];
    inv[1]  = -m[1]*m[10]*m[15] + m[1]*m[11]*m[14] + m[9]*m[2]*m[15] - m[9]*m[3]*m[14] - m[13]*m[2]*m[11] + m[13]*m[3]*m[10];
    inv[5]  =  m[0]*m[10]*m[15] - m[0]*m[11]*m[14] - m[8]*m[2]*m[15] + m[8]*m[3]*m[14] + m[12]*m[2]*m[11] - m[12]*m[3]*m[10];
    inv[9]  = -m[0]*m[9]*m[15]  + m[0]*m[11]*m[13] + m[8]*m[1]*m[15] - m[8]*m[3]*m[13] - m[12]*m[1]*m[11] + m[12]*m[3]*m[9];
    inv[13] =  m[0]*m[9]*m[14]  - m[0]*m[10]*m[13] - m[8]*m[1]*m[14] + m[8]*m[2]*m[13] + m[12]*m[1]*m[10] - m[12]*m[2]*m[9];
    inv[2]  =  m[1]*m[6]*m[15]  - m[1]*m[7]*m[14]  - m[5]*m[2]*m[15] + m[5]*m[3]*m[14] + m[13]*m[2]*m[7]  - m[13]*m[3]*m[6];
    inv[6]  = -m[0]*m[6]*m[15]  + m[0]*m[7]*m[14]  + m[4]*m[2]*m[15] - m[4]*m[3]*m[14] - m[12]*m[2]*m[7]  + m[12]*m[3]*m[6];
    inv[10] =  m[0]*m[5]*m[15]  - m[0]*m[7]*m[13]  - m[4]*m[1]*m[15] + m[4]*m[3]*m[13] + m[12]*m[1]*m[7]  - m[12]*m[3]*m[5];
    inv[14] = -m[0]*m[5]*m[14]  + m[0]*m[6]*m[13]  + m[4]*m[1]*m[14] - m[4]*m[2]*m[13] - m[12]*m[1]*m[6]  + m[12]*m[2]*m[5];
    inv[3]  = -m[1]*m[6]*m[11]  + m[1]*m[7]*m[10]  + m[5]*m[2]*m[11] - m[5]*m[3]*m[10] - m[9]*m[2]*m[7]   + m[9]*m[3]*m[6];
    inv[7]  =  m[0]*m[6]*m[11]  - m[0]*m[7]*m[10]  - m[4]*m[2]*m[11] + m[4]*m[3]*m[10] + m[8]*m[2]*m[7]   - m[8]*m[3]*m[6];
    inv[11] = -m[0]*m[5]*m[11]  + m[0]*m[7]*m[9]   + m[4]*m[1]*m[11] - m[4]*m[3]*m[9]  - m[8]*m[1]*m[7]   + m[8]*m[3]*m[5];
    inv[15] =  m[0]*m[5]*m[10]  - m[0]*m[6]*m[9]   - m[4]*m[1]*m[10] + m[4]*m[2]*m[9]  + m[8]*m[1]*m[6]   - m[8]*m[2]*m[5];
    float det = m[0]*inv[0] + m[1]*inv[4] + m[2]*inv[8] + m[3]*inv[12];
    if (std::fabs(det) < 1e-20f) return false;
    det = 1.0f / det;
    for (int i = 0; i < 16; ++i) inv[i] *= det;
    return true;
}

// Unproject the cursor to a world-space ray (origin on the near plane, normalized direction). Robust
// where screen-space corner projection fails — a large plane can have corners behind the camera.
bool screenRay(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
               ImVec2 mouse, Vec3& outOrig, Vec3& outDir) {
    float inv[16];
    if (!invert4x4(cam.mvp.m, inv)) return false;
    float ndcX = ((mouse.x - imgMin.x) / (float)vw) * 2.0f - 1.0f;
    float ndcY = 1.0f - ((mouse.y - imgMin.y) / (float)vh) * 2.0f;
    auto unproj = [&](float ndcZ, Vec3& o) -> bool {
        float cl[4] = {ndcX, ndcY, ndcZ, 1.0f}, w[4];
        for (int r = 0; r < 4; ++r)
            w[r] = inv[0*4+r]*cl[0] + inv[1*4+r]*cl[1] + inv[2*4+r]*cl[2] + inv[3*4+r]*cl[3];
        if (std::fabs(w[3]) < 1e-12f) return false;
        o = Vec3(w[0]/w[3], w[1]/w[3], w[2]/w[3]);
        return true;
    };
    Vec3 pNear, pFar;
    if (!unproj(-1.0f, pNear) || !unproj(1.0f, pFar)) return false;
    Vec3 d = pFar - pNear;
    if (length(d) < 1e-12f) return false;
    outOrig = pNear;
    outDir  = normalize(d);
    return true;
}

ImU32 col(const float role[4], float a = 1.0f) {
    float c[4] = {role[0], role[1], role[2], role[3] * a};
    return ui::Palette::toU32(c);
}

// Canvas-safe accent ink for strokes/glyphs painted DIRECTLY on the (always light) viewport.
// The dark theme's accent teal is itself light (Rec.709 lum ~0.69), so hover/selection ink
// drawn with it reads WORSE than the resting dark ink; darken it toward the on-canvas ink
// family while keeping the hue. The light theme accent is already dark — passes through.
ImU32 canvasAccent(float a = 1.0f) {
    const float* r = ui::palette.accent;
    float lum = 0.2126f * r[0] + 0.7152f * r[1] + 0.0722f * r[2];
    float k = lum > 0.5f ? 0.55f : 1.0f;
    float c[4] = {r[0] * k, r[1] * k, r[2] * k, r[3] * a};
    return ui::Palette::toU32(c);
}

// Light backing chip behind on-canvas annotation text (dimension labels). Near-invisible over
// the light canvas (it IS the canvas tone) but keeps the dark ink readable when a label crosses
// a dark model face — the same rescue the relation badges get from their green chip fill.
void labelChip(ImDrawList* dl, ImVec2 mn, ImVec2 mx) {
    const float s = ui::uiScale;
    dl->AddRectFilled(ImVec2(mn.x - 3.0f * s, mn.y - 1.5f * s),
                      ImVec2(mx.x + 3.0f * s, mx.y + 1.5f * s),
                      col(ui::palette.viewportTop, 0.78f), 3.0f * s);
}

// Adaptive tessellation for (u,v)-sampled circles/arcs: segment count from the projected
// radius so zoomed-in circles stay round and small ones stay cheap.
inline int circleSegs(float radiusPx) {
    int n = (int)(radiusPx * 0.7f);
    return n < 24 ? 24 : (n > 256 ? 256 : n);
}

// C-bug2 follow-up: vector mini-glyphs for the relation badges (and the live inference chips).
// The old ASCII tokens ("//", "col", "cr", "L"...) read as cramped letters, not constraint
// icons; these are real AA primitives scaled to the chip. `c` = chip center, `bs` = chip side.
void drawRelationGlyph(ImDrawList* dl, ImVec2 c, float bs, const char* type, ImU32 ink) {
    const float h = bs * 0.30f;                              // glyph half-extent
    const float w = std::max(1.2f, 1.4f * ui::uiScale);      // stroke width
    auto L = [&](float x0, float y0, float x1, float y1) {
        dl->AddLine(ImVec2(c.x + x0 * h, c.y + y0 * h),
                    ImVec2(c.x + x1 * h, c.y + y1 * h), ink, w);
    };
    if      (!std::strcmp(type, "horizontal"))    { L(-1, 0, 1, 0); }
    else if (!std::strcmp(type, "vertical"))      { L(0, -1, 0, 1); }
    else if (!std::strcmp(type, "parallel"))      { L(-0.85f, 1, 0.15f, -1); L(-0.15f, 1, 0.85f, -1); }
    else if (!std::strcmp(type, "perpendicular")) { L(-1, 1, 1, 1); L(0, 1, 0, -1); }
    else if (!std::strcmp(type, "equal"))         { L(-1, -0.45f, 1, -0.45f); L(-1, 0.45f, 1, 0.45f); }
    else if (!std::strcmp(type, "coincident"))    { dl->AddCircleFilled(c, h * 0.45f, ink);
                                                    dl->AddCircle(c, h, ink, 0, w); }
    else if (!std::strcmp(type, "concentric"))    { dl->AddCircle(c, h * 0.5f, ink, 0, w);
                                                    dl->AddCircle(c, h, ink, 0, w); }
    else if (!std::strcmp(type, "tangent"))       { dl->AddCircle(ImVec2(c.x, c.y + h * 0.3f),
                                                                  h * 0.65f, ink, 0, w);
                                                    L(-1, -0.35f, 1, -0.35f); }
    else if (!std::strcmp(type, "collinear"))     { L(-1, 0, -0.15f, 0); L(0.15f, 0, 1, 0); }
    else if (!std::strcmp(type, "coradial"))      { dl->PathArcTo(c, h, 0.5f, 5.8f);
                                                    dl->PathStroke(ink, 0, w);
                                                    dl->PathArcTo(c, h * 0.55f, 0.5f, 5.8f);
                                                    dl->PathStroke(ink, 0, w); }
    else if (!std::strcmp(type, "midpoint"))      { L(-1, 0.4f, 1, 0.4f);
        ImVec2 d[4] = {ImVec2(c.x, c.y - h), ImVec2(c.x + h * 0.6f, c.y - h * 0.2f),
                       ImVec2(c.x, c.y + h * 0.6f), ImVec2(c.x - h * 0.6f, c.y - h * 0.2f)};
        dl->AddQuadFilled(d[0], d[1], d[2], d[3], ink); }
    else if (!std::strcmp(type, "symmetric"))     { L(0, -1, 0, 1);
        L(-1, 0, -0.3f, 0);  L(-0.3f, 0, -0.65f, -0.35f); L(-0.3f, 0, -0.65f, 0.35f);
        L(1, 0, 0.3f, 0);    L(0.3f, 0, 0.65f, -0.35f);   L(0.3f, 0, 0.65f, 0.35f); }
    else if (!std::strcmp(type, "fix"))           { L(0, -1, 0, 0.55f); L(-1, 0.55f, 1, 0.55f);
        L(-0.85f, 1, -0.45f, 0.55f); L(-0.2f, 1, 0.2f, 0.55f); L(0.45f, 1, 0.85f, 0.55f); }
    else if (!std::strcmp(type, "point_on"))      { L(-1, 0.4f, 1, 0.4f);
        dl->AddCircleFilled(ImVec2(c.x, c.y + h * 0.4f), h * 0.38f, ink); }
    else if (!std::strcmp(type, "intersection"))  { L(-1, -1, 1, 1); L(-1, 1, 1, -1);
        dl->AddCircle(c, h * 0.4f, ink, 0, w); }
    else if (!std::strcmp(type, "pierce"))        { L(-1, 0, 1, 0); L(0, -1, 0, 1);
        dl->AddCircle(c, h * 0.65f, ink, 0, w); }
    else if (!std::strcmp(type, "smooth"))        {
        dl->AddBezierCubic(ImVec2(c.x-h, c.y+h), ImVec2(c.x, c.y+h),
                           ImVec2(c.x, c.y-h), ImVec2(c.x+h, c.y-h), ink, w);
        L(-0.4f, 0.7f, 0.4f, -0.7f); }
    else if (!std::strcmp(type, "equal_curve_length")) {
        dl->PathArcTo(ImVec2(c.x, c.y+h*0.7f), h, 3.5f, 5.9f);
        dl->PathStroke(ink, 0, w); L(-0.8f, 0.4f, 0.8f, 0.4f);
        L(-0.8f, 0.9f, 0.8f, 0.9f); }
    else { dl->AddCircleFilled(c, h * 0.4f, ink); }          // unknown relation: neutral dot
}

// Glass v2 HUD pill for sketch banners/hints — adaptivePanel + glassPanel + glassText (the
// viewport_panel "vp_pick_status" reference pattern), replacing the legacy fixed
// overlayBg/overlayText rects so the pill tracks the LIVE backdrop in both themes.
// `borderCol` != 0 draws a semantic ring (sketch status / error states).
void glassHudPill(ImDrawList* dl, const char* id, ImVec2 p0, const char* text,
                  ImU32 borderCol = 0) {
    const float s = ui::uiScale;
    ImVec2 ts = ImGui::CalcTextSize(text);
    ImVec2 p1(p0.x + ts.x + 24.0f * s, p0.y + ts.y + 10.0f * s);
    ImRect r(p0, p1);
    const ui::AdaptiveSurface& surf = ui::adaptivePanel(ui::panelId(id), r, ui::GlassGroup::Hud);
    ui::glassPanel(dl, r, ui::GlassLevel::UltraThin, 6.0f, surf);
    if (borderCol) dl->AddRect(p0, p1, borderCol, 6.0f * s, 0, 1.5f * s);
    ui::glassText(dl, nullptr, ImGui::GetFontSize(),
                  ImVec2(p0.x + 12.0f * s, p0.y + 5.0f * s), surf, text);
}
// Centered variant (top-center banners: plane pick, PM pickers, paste placement, tool hint).
// Keep the whole pill inside its viewport ownership rectangle; short/narrow web layouts share
// this path with desktop, so bounds are enforced here rather than by each caller.
void glassHudBanner(ImDrawList* dl, const char* id, const ImVec2& imgMin, const ImVec2& imgMax,
                    float y, const char* text, ImU32 borderCol = 0) {
    const float s = ui::uiScale;
    const float edgeMargin = 8.0f * s;
    const float padX = 12.0f * s;
    const float viewW = imgMax.x - imgMin.x;
    const float viewH = imgMax.y - imgMin.y;
    const float maxTextW = viewW - 2.0f * (edgeMargin + padX);
    if (maxTextW <= 0.0f || viewH <= 0.0f) return;

    const std::string shown = ui::kit::elide(text, maxTextW);
    if (shown.empty()) return;
    const ImVec2 ts = ImGui::CalcTextSize(shown.c_str());
    const float pillW = ts.x + 2.0f * padX;
    const float pillH = ts.y + 10.0f * s;
    if (pillW > viewW || pillH > viewH) return;


    float minY = imgMin.y + edgeMargin;
    float maxY = imgMax.y - edgeMargin - pillH;
    if (maxY < minY) {
        minY = imgMin.y;
        maxY = imgMax.y - pillH;
    }
    y = std::clamp(y, minY, maxY);

    const float cx = (imgMin.x + imgMax.x) * 0.5f;
    const float x = std::clamp(cx - pillW * 0.5f,
                               imgMin.x + edgeMargin,
                               imgMax.x - edgeMargin - pillW);
    glassHudPill(dl, id, ImVec2(x, y), shown.c_str(), borderCol);
}

// Sketch HUD / overlay ownership rectangle: the viewport's CLEAR rect — between the pinned
// FeatureManager / Copilot columns and below the floating doc-tab band (which the full-bleed 3D
// image runs under). Before the viewport has published it, fall back to the per-frame column
// edges and the tab-band bottom; a degenerate result falls back to the full image.
void skOverlayRect(const ImVec2& imgMin, const ImVec2& imgMax, ImVec2& outMin, ImVec2& outMax) {
    outMin = imgMin;
    outMax = imgMax;
    ImVec2 cMin, cMax;
    if (ui::viewportClearRect(cMin, cMax)) {
        outMin = ImVec2(std::max(cMin.x, imgMin.x), std::max(cMin.y, imgMin.y));
        outMax = ImVec2(std::min(cMax.x, imgMax.x), std::min(cMax.y, imgMax.y));
    } else {
        const float fmEdge = ui::overlayColumnEdge(0);   // FeatureManager right edge
        const float cpEdge = ui::overlayColumnEdge(1);   // Copilot left edge
        const float tabBot = ui::docTabBandBottom();     // floating doc-tab strip bottom
        if (fmEdge > imgMin.x) outMin.x = fmEdge;
        if (cpEdge > 0.0f && cpEdge < imgMax.x) outMax.x = cpEdge;
        if (tabBot > imgMin.y && tabBot < imgMax.y) outMin.y = tabBot;
    }
    if (outMax.x <= outMin.x || outMax.y <= outMin.y) {
        outMin = imgMin;
        outMax = imgMax;
    }
}

// Circumcircle of three (u,v) points. Returns false when (near-)collinear.
bool circumcircle(const ImVec2& a, const ImVec2& b, const ImVec2& c,
                  float& cu, float& cv, float& r) {
    float d = 2.0f * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y));
    if (std::fabs(d) < 1e-6f) return false;
    float a2 = a.x * a.x + a.y * a.y, b2 = b.x * b.x + b.y * b.y, c2 = c.x * c.x + c.y * c.y;
    cu = (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / d;
    cv = (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / d;
    r = std::hypot(a.x - cu, a.y - cv);
    return true;
}

// Draw a dashed line segment a->b onto dl.  dash/gap are in screen pixels; scale them by
// ui::uiScale before passing.  SolidWorks default: dash=6, gap=4 at scale 1.
void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col_,
                    float thickness, float dash, float gap) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float L = std::hypot(dx, dy);
    if (L < 1.0f) return;
    float step = dash + gap;
    for (float t = 0.0f; t < L; t += step) {
        float e = std::fmin(t + dash, L);
        dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                    ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), col_, thickness);
    }
}

// Draw a dashed polyline through `pts` (screen-space) with continuous phase across segments.
// Used for construction circles/arcs/ellipses/splines so dashes don't restart at each tesselation
// vertex (which would look like a dotted line).
void drawDashedPolyline(ImDrawList* dl, const std::vector<ImVec2>& pts, ImU32 col_,
                        float thickness, float dash, float gap) {
    if (pts.size() < 2) return;
    float phase = 0.0f;   // distance walked in the current ON or OFF span
    bool on = true;
    for (size_t i = 1; i < pts.size(); ++i) {
        ImVec2 a = pts[i - 1], b = pts[i];
        float dx = b.x - a.x, dy = b.y - a.y;
        float segLen = std::hypot(dx, dy);
        if (segLen < 1e-4f) continue;
        float walked = 0.0f;   // distance walked along THIS segment
        while (walked < segLen) {
            float span = on ? dash : gap;
            float rem = span - phase;          // remaining in this dash/gap slot
            float avail = segLen - walked;
            float step = std::fmin(rem, avail);
            if (on) {
                float f0 = walked / segLen, f1 = (walked + step) / segLen;
                dl->AddLine(ImVec2(a.x + dx * f0, a.y + dy * f0),
                            ImVec2(a.x + dx * f1, a.y + dy * f1), col_, thickness);
            }
            walked += step;
            phase += step;
            if (phase >= span - 1e-4f) { on = !on; phase = 0.0f; }
        }
    }
}

// arcSweepContains is defined later in this TU's anonymous namespace (with the trim helpers);
// forward-declare it here so SketchMode::hitTestHandle below can gate an arc's draggable rim
// handle to the drawn sweep.
bool arcSweepContains(const SketchEntity& arc, float ang);

}  // namespace

// ---------------------------------------------------------------------------
// projection basis (camera is locked fronto-parallel, so the plane maps affinely to screen)
// ---------------------------------------------------------------------------

void SketchMode::buildBasis(const Renderer& r, const ImVec2& imgMin, int vw, int vh) {
    basisOk_ = false;
    const auto& cam = r.camera();
    // World positions of the sketch-plane origin and the +1u / +1v points.
    // world = frame * [u,v,0,1]; columns of the 4x4 (row-major frame_): col j is frame_[k*4+j].
    auto uvWorld = [&](float u, float v, float& wx, float& wy, float& wz) {
        wx = frame_[0] * u + frame_[1] * v + frame_[3];
        wy = frame_[4] * u + frame_[5] * v + frame_[7];
        wz = frame_[8] * u + frame_[9] * v + frame_[11];
    };
    float ox, oy, oz, ux, uy, uz, vx, vy, vz;
    uvWorld(0, 0, ox, oy, oz);
    uvWorld(1, 0, ux, uy, uz);
    uvWorld(0, 1, vx, vy, vz);
    ImVec2 sO, sU, sV;
    if (!worldToScreen(cam, imgMin, vw, vh, ox, oy, oz, sO)) return;
    if (!worldToScreen(cam, imgMin, vw, vh, ux, uy, uz, sU)) return;
    if (!worldToScreen(cam, imgMin, vw, vh, vx, vy, vz, sV)) return;
    originS_ = sO;
    uAxisS_ = ImVec2(sU.x - sO.x, sU.y - sO.y);
    vAxisS_ = ImVec2(sV.x - sO.x, sV.y - sO.y);
    det_ = uAxisS_.x * vAxisS_.y - uAxisS_.y * vAxisS_.x;
    basisOk_ = std::fabs(det_) > 1e-6f;
}

ImVec2 SketchMode::uvToScreen(float u, float v) const {
    return ImVec2(originS_.x + uAxisS_.x * u + vAxisS_.x * v,
                  originS_.y + uAxisS_.y * u + vAxisS_.y * v);
}

void SketchMode::screenToUV(const ImVec2& s, float& u, float& v) const {
    // Solve [uAxis vAxis] * [u v]^T = (s - origin)  (2x2 inverse).
    float dx = s.x - originS_.x, dy = s.y - originS_.y;
    if (std::fabs(det_) < 1e-6f) { u = v = 0; return; }
    u = (vAxisS_.y * dx - vAxisS_.x * dy) / det_;
    v = (-uAxisS_.y * dx + uAxisS_.x * dy) / det_;
}

// ---------------------------------------------------------------------------
// worker round-trips
// ---------------------------------------------------------------------------

bool SketchMode::callSketch(AppState& st, const char* method, const nlohmann::json& extra,
                            nlohmann::json& result) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return false;
    nlohmann::json params = extra;
    params["doc_id"] = st.docMirror.docId();
    params["sketch"] = fid_;
    std::string err;
    if (!st.worker.call(method, params, result, err)) {
        st.addLog(std::string("[sketch] ") + method + " transport error: " + err);
        return false;
    }
    if (result.contains("code")) {  // structured error envelope
        st.addLog(std::string("[sketch] ") + method + " error: " +
                  result.value("message", std::string("?")));
        return false;
    }
    return true;
}

// C50f: read-only over-define pre-flight. Wraps the sketch_probe RPC. Returns true and fills the
// out-params on success; false on transport / RPC error (callSketch already logged it) so the
// caller can fall back to committing normally — a probe failure must never block the user.
bool SketchMode::sketchProbe(AppState& st, const nlohmann::json& action, bool& wouldOverDefine,
                             std::vector<std::string>& conflicting,
                             std::vector<std::string>& redundant) {
    nlohmann::json res;
    nlohmann::json extra = {{"action", action}};
    if (!callSketch(st, "sketch_probe", extra, res)) return false;
    // sketch_probe reports failure as {"ok": false, "error": "..."} (NOT the structured envelope
    // callSketch already screens). Treat that as "could not probe" -> fall back.
    if (res.contains("ok") && res["ok"].is_boolean() && !res["ok"].get<bool>()) {
        st.addLog(std::string("[sketch] sketch_probe rejected: ") +
                  res.value("error", std::string("?")));
        return false;
    }
    wouldOverDefine = res.value("would_over_define", false);
    conflicting.clear();
    if (res.contains("conflicting") && res["conflicting"].is_array())
        for (const auto& c : res["conflicting"]) if (c.is_string()) conflicting.push_back(c.get<std::string>());
    redundant.clear();
    if (res.contains("redundant") && res["redundant"].is_array())
        for (const auto& c : res["redundant"]) if (c.is_string()) redundant.push_back(c.get<std::string>());
    return true;
}

// Parse a sketch response's "entities" (+ optional "statuses") array into SketchEntity records.
// Shared by applyEnterResult (live sketch) and the C40 ghost-sketch cache.
static void parseEntities(const nlohmann::json& res, std::vector<SketchEntity>& out) {
    out.clear();
    const nlohmann::json* stats = res.contains("statuses") ? &res["statuses"] : nullptr;
    if (!res.contains("entities") || !res["entities"].is_array()) return;
    // T-BL49: eid -> {ref, stale} ownership map for associative Convert Entities styling.
    const nlohmann::json* ext = res.contains("external") && res["external"].is_object()
                                    ? &res["external"] : nullptr;
    for (const auto& e : res["entities"]) {
        SketchEntity se;
        se.id = e.value("id", std::string());
        se.type = e.value("type", std::string());
        se.construction = e.value("construction", false);
        if (ext && ext->contains(se.id)) {
            se.external = true;
            se.externalStale = (*ext)[se.id].value("stale", false);
            const auto source = (*ext)[se.id].find("source");
            se.externalPierce = source != (*ext)[se.id].end() && source->is_object() &&
                                source->value("mode", std::string()) == "pierce";
        }
        if (stats && stats->contains(se.id)) se.status = (*stats)[se.id].get<std::string>();
        else se.status = "under";
        auto v2 = [&](const char* k, float o[2]) {
            if (e.contains(k) && e[k].is_array() && e[k].size() == 2) {
                o[0] = e[k][0].get<float>(); o[1] = e[k][1].get<float>();
            }
        };
        v2("p1", se.p1); v2("p2", se.p2); v2("center", se.center);
        v2("p", se.p1);   // C42: point/text store their anchor as "p" — reuse p1
        se.radius = e.value("radius", 0.0f);
        se.start = e.value("start", 0.0f);
        se.end = e.value("end", 0.0f);
        se.ccw = e.value("ccw", true);
        se.width = e.value("width", 0.0f);
        se.rx = e.value("rx", 0.0f);          // C42: ellipse
        se.ry = e.value("ry", 0.0f);
        se.rot = e.value("rot", 0.0f);
        se.pts.clear();                       // C42: spline through-points
        if (e.contains("points") && e["points"].is_array())
            for (const auto& q : e["points"])
                if (q.is_array() && q.size() == 2)
                    se.pts.push_back(ImVec2(q[0].get<float>(), q[1].get<float>()));
        if (se.type == "arc_slot") {   // arc_slot stores spine angles as a0/a1 (CCW)
            se.start = e.value("a0", 0.0f);
            se.end = e.value("a1", 0.0f);
            se.ccw = true;
        }
        out.push_back(se);
    }
}

void SketchMode::applyEnterResult(const nlohmann::json& res) {
    dof_ = res.value("dof", 0);
    status_ = res.value("status", std::string("under"));
    conflicting_.clear();
    if (res.contains("conflicting") && res["conflicting"].is_array())
        for (const auto& c : res["conflicting"]) if (c.is_string()) conflicting_.push_back(c.get<std::string>());
    redundant_.clear();
    if (res.contains("redundant") && res["redundant"].is_array())
        for (const auto& c : res["redundant"]) if (c.is_string()) redundant_.push_back(c.get<std::string>());
    skipped_.clear();
    if (res.contains("skipped_constraints") && res["skipped_constraints"].is_array())
        for (const auto& c : res["skipped_constraints"])
            if (c.is_object()) skipped_[c.value("id", std::string())] =
                c.value("reason", std::string("Relation could not be applied"));
    // C50f: latch the conflict-freeze flag. solve_rejected:true means the system is contradictory
    // and `entities` carry last-valid (frozen) coords; cleared the moment a solve succeeds again.
    // Drag responses always report solve_rejected:false, so they release a prior freeze too.
    solveRejected_ = res.value("solve_rejected", false);
    if (res.contains("plane_frame") && res["plane_frame"].is_array() &&
        res["plane_frame"].size() == 16) {
        for (int i = 0; i < 16; ++i) frame_[i] = res["plane_frame"][i].get<float>();
    }
    parseEntities(res, ents_);
    entIndex_.clear();
    entIndex_.reserve(ents_.size());
    for (size_t i = 0; i < ents_.size(); ++i) entIndex_.emplace(ents_[i].id, i);
    const auto& geometry = res.contains("entities") ? res["entities"] : nlohmann::json();
    if (geometry != entitiesSnapshot_) {
        entitiesSnapshot_ = geometry;
        ++entitiesRevision_;
        regionsDirty_ = true;
    }
    applyConstraints(res);   // C37-D: store the constraints array for dim annotations
    // C41 (T2) / C50: drop selected handles whose OWNING entity no longer exists (deleted/undone).
    // Virtual refs (sketch origin + datum axes) are not in ents_ but stay selectable as references.
    selIds_.erase(std::remove_if(selIds_.begin(), selIds_.end(),
                                 [&](const std::string& id) {
                                     const std::string base = id.substr(0, id.find('.'));
                                     if (base == "origin" || id.rfind("axis.", 0) == 0) return false;
                                     return entById(base) == nullptr;
                                 }),
                  selIds_.end());
    // C51: drop selected relations whose constraint vanished (deleted/undone/re-solved away).
    selRelIds_.erase(std::remove_if(selRelIds_.begin(), selRelIds_.end(),
                                    [&](const std::string& cid) {
                                        for (const auto& c : cons_) if (c.id == cid) return false;
                                        return true;
                                    }),
                     selRelIds_.end());
    // C38 (S1) / C50: ents_ was just refilled — prune Smart Dimension picks SURGICALLY instead of
    // clearing the buffer. A projection of external geometry (addProjectedDimRef) applies ops and
    // resolves MID-PICK, and the wholesale clear was throwing away the user's first pick. Picks
    // whose entity vanished are dropped; point picks get their (u,v) refreshed from the solved
    // entity so the preview math doesn't use stale coordinates. The Modify box (if open) already
    // captured its placement + refs, so it survives this refresh untouched.
    if (!dimPopupOpen_) {
        auto baseOf = [](const std::string& h) { return h.substr(0, h.find('.')); };
        dimPicks_.erase(std::remove_if(dimPicks_.begin(), dimPicks_.end(),
            [&](DimPick& p) {
                if (p.isPoint) {
                    std::string base = baseOf(p.handle);
                    if (base == "origin") { p.u = 0; p.v = 0; return false; }
                    const SketchEntity* e = entById(base);
                    if (!e) return true;
                    if (p.handle.find(".p1") != std::string::npos)      { p.u = e->p1[0]; p.v = e->p1[1]; }
                    else if (p.handle.find(".p2") != std::string::npos) { p.u = e->p2[0]; p.v = e->p2[1]; }
                    else if (p.handle.find(".center") != std::string::npos) { p.u = e->center[0]; p.v = e->center[1]; }
                    else if (p.handle.find(".p") != std::string::npos)  { p.u = e->p1[0]; p.v = e->p1[1]; }
                    return false;
                }
                p.entIdx = entIndexById(p.entId);
                return p.entIdx < 0;
            }), dimPicks_.end());
        dimHasPending_ = false;     // rebuilt next frame from the surviving picks
        dimPending_ = nullptr;
    }
}

// C37-D: pull the constraints array (worker_rpc.md §"constraints") off any sketch response.
void SketchMode::applyConstraints(const nlohmann::json& res) {
    badgesRevision_ = size_t(-1);
    cons_.clear();
    if (!res.contains("constraints") || !res["constraints"].is_array()) return;
    for (const auto& c : res["constraints"]) {
        SketchConstraint sc;
        sc.id = c.value("id", std::string());
        sc.type = c.value("type", std::string());
        if (c.contains("refs") && c["refs"].is_array())
            for (const auto& r : c["refs"]) if (r.is_string()) sc.refs.push_back(r.get<std::string>());
        if (c.contains("value") && c["value"].is_number()) { sc.hasValue = true; sc.value = c["value"].get<double>(); }
        if (c.contains("expr") && c["expr"].is_string()) { sc.hasExpr = true; sc.expr = c["expr"].get<std::string>(); }
        sc.driven = c.value("driven", false);
        if (c.contains("placement") && c["placement"].is_array() && c["placement"].size() == 2) {
            sc.hasPlacement = true;
            sc.placement[0] = c["placement"][0].get<float>();
            sc.placement[1] = c["placement"][1].get<float>();
        }
        if (c.contains("measured") && c["measured"].is_number()) { sc.hasMeasured = true; sc.measured = c["measured"].get<double>(); }
        if (c.contains("axis") && c["axis"].is_string()) sc.axis = c["axis"].get<std::string>();
        if (c.contains("cond") && c["cond"].is_array())
            for (const auto& q : c["cond"]) sc.cond.push_back(q.is_string() ? q.get<std::string>() : "center");
        cons_.push_back(sc);
    }
}

const SketchEntity* SketchMode::entById(const std::string& id) const {
    const auto it = entIndex_.find(id);
    return it != entIndex_.end() && it->second < ents_.size() ? &ents_[it->second] : nullptr;
}

bool SketchMode::relationPointUV(const std::string& h, float& u, float& v) const {
    if (h == "origin" || h.rfind("axis.", 0) == 0) { u = v = 0; return true; }
    const auto dot = h.find('.');
    const auto* e = entById(h.substr(0, dot));
    if (!e) return false;
    const auto sub = dot == std::string::npos ? std::string() : h.substr(dot + 1);
    if (e->type == "spline" || e->type == "style_spline") {
        if (e->pts.empty()) return false;
        size_t i = e->pts.size() / 2;
        if (sub == "start") i = 0;
        else if (sub == "end") i = e->pts.size() - 1;
        else if (sub.size() > 1 && sub[0] == 'p') {
            i = 0;
            for (size_t k = 1; k < sub.size(); ++k) {
                if (sub[k] < '0' || sub[k] > '9') return false;
                i = i * 10 + size_t(sub[k] - '0');
                if (i >= e->pts.size()) return false;
            }
        }
        u = e->pts[i].x; v = e->pts[i].y;
    } else if (e->type == "point" || sub == "p" ||
               ((e->type == "line" || e->type == "slot") && (sub == "p1" || sub == "start"))) {
        u = e->p1[0]; v = e->p1[1];
    } else if ((e->type == "line" || e->type == "slot") && (sub == "p2" || sub == "end")) {
        u = e->p2[0]; v = e->p2[1];
    } else if ((e->type == "arc" || e->type == "ellipse_arc") &&
               (sub == "start" || sub == "end" || sub == "mid")) {
        float sweep = e->end - e->start;
        if (e->ccw) { while (sweep < 0) sweep += 360; }
        else { while (sweep > 0) sweep -= 360; }
        const float a = (sub == "start" ? e->start : sub == "end" ? e->end :
                         e->start + sweep * 0.5f) * 3.14159265f / 180.0f;
        const float x = (e->type == "arc" ? e->radius : e->rx) * std::cos(a);
        const float y = (e->type == "arc" ? e->radius : e->ry) * std::sin(a);
        const float r = e->rot * 3.14159265f / 180.0f;
        u = e->center[0] + x * std::cos(r) - y * std::sin(r);
        v = e->center[1] + x * std::sin(r) + y * std::cos(r);
    } else if (e->type == "line" || e->type == "slot") {
        u = (e->p1[0] + e->p2[0]) * 0.5f; v = (e->p1[1] + e->p2[1]) * 0.5f;
    } else {
        u = e->center[0]; v = e->center[1];
    }
    return true;
}

bool SketchMode::relationLineUV(const std::string& h, ImVec2& a, ImVec2& b) const {
    if (h == "axis.h" || h == "axis.v") {
        a = ImVec2(0, 0); b = h == "axis.h" ? ImVec2(1, 0) : ImVec2(0, 1);
        return true;
    }
    const auto* e = entById(h);
    if (!e || (e->type != "line" && e->type != "slot")) return false;
    a = ImVec2(e->p1[0], e->p1[1]); b = ImVec2(e->p2[0], e->p2[1]);
    return true;
}

// C41 (T2): is this entity id in the in-sketch selection?
bool SketchMode::isSelected(const std::string& id) const {
    return std::find(selIds_.begin(), selIds_.end(), id) != selIds_.end();
}

// ---- Wave 4C/4D: sketch entity copy / paste -------------------------------------------------
// The base entity id behind a selection handle ("e3.p2" -> "e3"; a bare "e3" -> "e3").
static std::string skBaseId(const std::string& handle) {
    return handle.substr(0, handle.find('.'));
}

bool SketchMode::sketchHasSelection() const {
    return active_ && !selIds_.empty();
}

// The active sketch feature's raw params object in the live doc tree (entities + constraints with
// EVERY field intact — richer than the parsed SketchEntity). nullptr if the sketch isn't found.
static const nlohmann::json* sketchParamsInTree(AppState& st, const std::string& fid) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return nullptr;
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return nullptr;
    for (const auto& f : *fa) {
        if (!f.is_object()) continue;
        if (f.value("id", std::string()) != fid) continue;
        auto p = f.find("params");
        return (p != f.end() && p->is_object()) ? &(*p) : nullptr;
    }
    return nullptr;
}

// Features that consume `sketchFid` via profile/path/guide (or other sketch) refs.
// Walk matches the FeatureManager nesting scan: region selectors {"kind":"region","sketch":…}
// plus bare sketch-id strings under profile/path/guide/*sketch* keys. Returns display names.
static void collectSketchDependents(AppState& st, const std::string& sketchFid,
                                    std::vector<std::string>& outNames) {
    outNames.clear();
    if (sketchFid.empty() || !st.docMirror.hasDoc()) return;
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return;

    std::function<bool(const nlohmann::json&, const std::string&)> mentions =
        [&](const nlohmann::json& node, const std::string& key) -> bool {
        if (node.is_object()) {
            if (node.value("kind", std::string()) == "region" &&
                node.value("sketch", std::string()) == sketchFid)
                return true;
            for (auto it = node.begin(); it != node.end(); ++it)
                if (mentions(it.value(), it.key())) return true;
        } else if (node.is_array()) {
            for (const auto& e : node)
                if (mentions(e, key)) return true;
        } else if (node.is_string() && node.get<std::string>() == sketchFid) {
            // Bare sketch id under a profile/path/guide/sketch-ish key.
            if (key.find("sketch") != std::string::npos ||
                key == "profile" || key == "path" || key == "guide" ||
                key == "guides" || key == "centerline" || key == "sections")
                return true;
        }
        return false;
    };

    for (const auto& f : *fa) {
        if (!f.is_object()) continue;
        if (f.value("type", std::string()) == "sketch") continue;
        if (f.value("id", std::string()) == sketchFid) continue;
        auto refs = f.find("refs");
        if (refs == f.end()) continue;
        if (!mentions(*refs, std::string())) continue;
        std::string name = f.value("name", std::string());
        if (name.empty()) name = f.value("id", std::string("feature"));
        outNames.push_back(std::move(name));
    }
}

static std::string featureDisplayName(AppState& st, const std::string& fid) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
        for (const auto& f : tree["features"])
            if (f.is_object() && f.value("id", std::string()) == fid) {
                std::string n = f.value("name", std::string());
                return n.empty() ? fid : n;
            }
    return fid;
}

// Warn toast when a consumed sketch is emptied: dependents stay (red badge) but need a profile.
static std::string emptyProfileToast(const std::vector<std::string>& depNames,
                                     const std::string& sketchName) {
    std::string joined;
    for (size_t i = 0; i < depNames.size(); ++i) {
        if (i) joined += ", ";
        joined += depNames[i];
    }
    return joined + " has no profile \xE2\x80\x94 add a closed profile in " +
           sketchName + " to restore it";
}

// Copy the selected entities (+ their internal constraints) into st.clipboard as an entities bundle.
// Entities are pulled RAW from the doc tree so all type-specific fields survive the round-trip; the
// constraint set keeps only relations whose every handle references a SELECTED entity (external /
// origin relations are dropped — the engine drops them at paste anyway). copyOrigin = the min-corner
// of the selected geometry in sketch (u,v), so paste can offset relative to a stable anchor.
bool SketchMode::sketchCopySelection(AppState& st) {
    if (!active_ || selIds_.empty()) return false;
    const nlohmann::json* params = sketchParamsInTree(st, fid_);
    if (!params) { st.toast(ui::ToastLevel::Warn, "Copy: sketch not found"); return false; }

    // Selected base entity ids (deduped, preserving the raw-entity order below).
    std::unordered_map<std::string, bool> wanted;
    for (const auto& h : selIds_) wanted[skBaseId(h)] = true;

    nlohmann::json entities = nlohmann::json::array();
    auto ents = params->find("entities");
    if (ents != params->end() && ents->is_array()) {
        for (const auto& e : *ents) {
            if (!e.is_object()) continue;
            std::string id = e.value("id", std::string());
            if (!id.empty() && wanted.count(id)) entities.push_back(e);
        }
    }
    if (entities.empty()) { st.toast(ui::ToastLevel::Warn, "Nothing to copy"); return false; }

    // Internal constraints only: every handle base must be a copied entity.
    nlohmann::json constraints = nlohmann::json::array();
    auto cons = params->find("constraints");
    if (cons != params->end() && cons->is_array()) {
        for (const auto& c : *cons) {
            if (!c.is_object()) continue;
            auto refs = c.find("refs");
            if (refs == c.end() || !refs->is_array() || refs->empty()) continue;
            bool allInternal = true;
            for (const auto& r : *refs) {
                if (!r.is_string() || !wanted.count(skBaseId(r.get<std::string>()))) {
                    allInternal = false; break;
                }
            }
            if (allInternal) constraints.push_back(c);
        }
    }

    // Copy-origin = min (u,v) corner of the selected entities (from the solved coords we already
    // hold). Gives a stable, intuitive anchor so the paste preview tracks the cursor naturally.
    bool any = false;
    float mnU = 0, mnV = 0;
    auto acc = [&](float u, float v) {
        if (!any) { mnU = u; mnV = v; any = true; }
        else { if (u < mnU) mnU = u; if (v < mnV) mnV = v; }
    };
    for (const auto& e : ents_) {
        if (!wanted.count(e.id)) continue;
        acc(e.p1[0], e.p1[1]); acc(e.p2[0], e.p2[1]);
        acc(e.center[0], e.center[1]);
        for (const auto& q : e.pts) acc(q.x, q.y);
    }

    st.clipboard.clear();
    st.clipboard.kind = AppState::Clipboard::Kind::SketchEntities;
    st.clipboard.srcDocId = st.docMirror.docId();
    st.clipboard.payload = nlohmann::json{{"entities", entities}, {"constraints", constraints}};
    st.clipboard.copyOriginU = any ? mnU : 0.0f;
    st.clipboard.copyOriginV = any ? mnV : 0.0f;
    st.addLog("[sketch] copied " + std::to_string(entities.size()) + " entit(ies)");
    st.toast(ui::ToastLevel::Info,
             "Copied " + std::to_string(entities.size()) + " sketch entit(ies)");
    return true;
}

// Arm the paste-placement mode from a SketchEntities clipboard. The pasted outlines then follow the
// cursor (drawPastePreview) until the next viewport click commits, or Esc cancels. Snapshots the
// bundle + anchor so a later clipboard change can't disturb the in-progress placement.
bool SketchMode::sketchPasteBegin(AppState& st) {
    if (!active_) return false;
    if (st.clipboard.kind != AppState::Clipboard::Kind::SketchEntities) return false;
    const nlohmann::json& pl = st.clipboard.payload;
    if (!pl.is_object() || !pl.contains("entities") || !pl["entities"].is_array() ||
        pl["entities"].empty())
        return false;
    // Cancel any armed drawing tool / Smart-Dim pick so placement owns the next click cleanly.
    resetTool();
    pasteBundle_ = pl;
    pasteAnchorUV_ = ImVec2(st.clipboard.copyOriginU, st.clipboard.copyOriginV);
    pastePlacing_ = true;
    st.toast(ui::ToastLevel::Info, "Click to place pasted entities (Esc to cancel)");
    return true;
}

// Commit the pasted bundle shifted by (offsetX, offsetY) via the sketch_paste_entities op. Routed
// through the normal ops path (revision bump + undo) like sketch_add_entity, then re-solve so the
// new geometry shows. Op errors are toasted and leave the sketch untouched.
void SketchMode::commitPasteEntities(AppState& st, float offsetX, float offsetY) {
    if (!pasteBundle_.is_object()) { pastePlacing_ = false; return; }
    nlohmann::json op = {
        {"op", "sketch_paste_entities"},
        {"sketch", fid_},
        {"entities_bundle", pasteBundle_},
        {"offset_x", offsetX},
        {"offset_y", offsetY},
    };
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    pastePlacing_ = false;
    pasteBundle_ = nlohmann::json();
    if (!r.ok) { st.toast(ui::ToastLevel::Error, "Paste: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    st.addLog("[sketch] pasted entities (dx=" + std::to_string(offsetX) +
              ", dy=" + std::to_string(offsetY) + ")");
}

// Follow-cursor preview of the paste bundle: parse its raw entities, translate them by
// (cursorUV - anchor), and stroke each outline in the accent color. The cursor delta is computed in
// sketch (u,v) so the preview lands exactly where the commit op will place the geometry.
void SketchMode::drawPastePreview(AppState& st, ImDrawList* dl, const ImVec2& cursor) {
    if (!pasteBundle_.is_object() || !basisOk_) return;
    (void)st;
    float cu = 0, cv = 0; screenToUV(cursor, cu, cv);
    float du = cu - pasteAnchorUV_.x, dv = cv - pasteAnchorUV_.y;
    // Parse the bundle entities into temporary SketchEntity records, then shift each in (u,v).
    std::vector<SketchEntity> preview;
    nlohmann::json wrap = nlohmann::json{{"entities", pasteBundle_["entities"]}};
    parseEntities(wrap, preview);
    const ImU32 c = col(ui::palette.accent, 0.9f);
    for (auto& e : preview) {
        e.p1[0] += du; e.p1[1] += dv;
        e.p2[0] += du; e.p2[1] += dv;
        e.center[0] += du; e.center[1] += dv;
        for (auto& q : e.pts) { q.x += du; q.y += dv; }
        strokeEntityOutline(dl, e, c, std::max(1.5f, 2.0f * ui::uiScale));
    }
}

// C38 (S1): resolve a stored entity id to its CURRENT index in ents_ (-1 if gone). DimPicks store
// the id (stable) rather than the index (refilled by applyEnterResult/resolve mid-pick).
int SketchMode::entIndexById(const std::string& id) const {
    const auto it = entIndex_.find(id);
    return it != entIndex_.end() && it->second < ents_.size() ? (int)it->second : -1;
}

void SketchMode::resolve(AppState& st) {
    nlohmann::json res;
    if (callSketch(st, "sketch_solve", nlohmann::json::object(), res)) applyEnterResult(res);
}

// sketch_drag_commit / sketch_exit bump the worker's document revision out from under the mirror.
// C24: DocMirror::resync() pulls the fresh tree + revision (doc_tree) and fires the listener so
// the FeatureManager repaints — replaces the old throwaway resolvePick(1,"body",1) trick.
static void resyncMirror(AppState& st) {
    st.docMirror.resync(st.worker);
}

void SketchMode::commitDrag(AppState& st) {
    // Drop any in-flight / pending transient drag samples — commit is the authority.
    ++dragGen_;
    dragPending_ = false;
    dragInFlight_ = false;
    nlohmann::json res;
    if (!callSketch(st, "sketch_drag_commit", nlohmann::json::object(), res)) return;
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    resyncMirror(st);
    resolve(st);   // refresh statuses against the committed doc
}

void SketchMode::addEntity(AppState& st, const nlohmann::json& entity,
                           const nlohmann::json& constraints) {
    // sketch_add_entity goes through the normal ops path (revision bump + undo) via DocMirror.
    nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
    if (!constraints.is_null() && constraints.is_array() && !constraints.empty())
        op["constraints"] = constraints;
    nlohmann::json ops = nlohmann::json::array({op});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) {
        st.addLog("[sketch] add_entity failed: " + r.error_message);
        st.toast(ui::ToastLevel::Warn, r.error_message);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

// Shared snap->constraint translation (see header). The Line tool keeps its own inline variant
// because it also threads tangent emission through a per-segment flag; every other tool routes its
// center/endpoint snaps through here so a snapped placement leaves a real relation behind.
void SketchMode::pushSnapConstraint(const std::string& snap, const std::string& pt,
                                    nlohmann::json& cs) const {
    if (snap.empty()) return;
    if (const auto semi = snap.find(';'); semi != std::string::npos) {   // rim + alignment rider
        pushSnapConstraint(snap.substr(0, semi), pt, cs);
        pushSnapConstraint(snap.substr(semi + 1), pt, cs);
        return;
    }
    if (snapHasPrefix(snap, "align:")) {
        // "align:<uHandle>|<vHandle>" — vertical (same u/x) to uHandle, horizontal (same v/y) to
        // vHandle; either side may be empty. Two-point form: refs = {source, this point}.
        std::string body = snap.substr(6);
        auto bar = body.find('|');
        std::string uH = bar == std::string::npos ? body : body.substr(0, bar);
        std::string vH = bar == std::string::npos ? std::string() : body.substr(bar + 1);
        if (!uH.empty()) cs.push_back({{"type", "vertical"},   {"refs", {uH, pt}}});
        if (!vH.empty()) cs.push_back({{"type", "horizontal"}, {"refs", {vH, pt}}});
        return;
    }
    if (snapHasPrefix(snap, "mid:")) {
        cs.push_back({{"type", "midpoint"}, {"refs", {pt, snap.substr(4)}}});
    } else if (snapHasPrefix(snap, "rim:") || snapHasPrefix(snap, "tan:")) {
        // The new entity's point `pt` lands on the snapped circle/arc rim.
        cs.push_back({{"type", "point_on"}, {"refs", {pt, snap.substr(4)}}});
    } else {
        // Plain endpoint/center/origin handle -> coincident.
        cs.push_back({{"type", "coincident"}, {"refs", {snap, pt}}});
    }
}

std::string SketchMode::emitConstructionEntity(AppState& st, const nlohmann::json& entity,
                                               const nlohmann::json& constraints) {
    nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
    if (constraints.is_array() && !constraints.empty()) op["constraints"] = constraints;
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/true);
    if (!r.ok) { st.addLog("[sketch] construction entity failed: " + r.error_message); return {}; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    return (r.id_map.is_object() && r.id_map.contains("$0"))
         ? r.id_map["$0"].get<std::string>() : std::string();
}

void SketchMode::emitConstructionConstraint(AppState& st, const nlohmann::json& constraint) {
    nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", constraint}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/true);
    if (!r.ok) { st.addLog("[sketch] construction constraint failed: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
}

void SketchMode::applyRelationOps(AppState& st, const nlohmann::json& ops,
                                  const std::string& label) {
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) {
        st.toast(ui::ToastLevel::Warn, label + ": " + r.error_message);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

void SketchMode::addRelationOps(AppState& st, const nlohmann::json& ops,
                                const std::string& label) {
    if (!ops.is_array() || ops.empty()) return;
    const bool projects = std::any_of(ops.begin(), ops.end(), [](const auto& op) {
        return op.is_object() && op.value("op", std::string()) == "sketch_convert";
    });

    for (const auto& op : ops) {
        if (!op.is_object() || op.value("op", std::string()) != "sketch_add_constraint")
            continue;
        if (!op.contains("constraint") || !op["constraint"].is_object())
            continue;
        // New projected IDs exist only inside the applyOps batch; a separate probe cannot
        // resolve them. The normal transactional apply path validates the whole batch.
        if (projects) continue;
        bool wouldOver = false;
        std::vector<std::string> pc, pr;
        if (sketchProbe(st, nlohmann::json{{"add_constraint", op["constraint"]}},
                        wouldOver, pc, pr) && wouldOver) {
            relOverDefOps_ = ops;
            relOverDefLabel_ = label;
            relOverDefPos_ = relPopupPos_;
            relOverDefOpen_ = true;
            relPopupOpen_ = false;
            relIds_.clear();
            return;
        }
    }

    applyRelationOps(st, ops, label);
    relPopupOpen_ = false;
    relIds_.clear();
    relModelPicking_ = false;
    relModelRefs_.clear(); relModelKey_.clear();
}

void SketchMode::fullyDefine(AppState& st) {
    nlohmann::json op = {{"op", "sketch_fully_define"}, {"sketch", fid_},
                         {"relations", true}, {"dimensions", true}, {"anchor", "origin"}};
    std::set<std::string> entities;
    for (const auto& h : selIds_)
        if (const auto* e = entById(skBaseId(h))) entities.insert(e->id);
    if (!entities.empty()) op["entities"] = entities;
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        st.toast(ui::ToastLevel::Warn, "Fully Define Sketch: " + r.error_message);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    st.toast(dof_ == 0 ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
             "Fully Define Sketch: " + std::to_string(dof_) + " DOF remaining");
}

void SketchMode::drawRelationList(AppState& st, const ImVec2& lo, const ImVec2& hi) {
    panelHoverRel_.clear();
    if (!relationsPanel_.open) return;
    std::vector<RelationRow> rows;
    rows.reserve(cons_.size());
    for (const auto& c : cons_) {
        RelationRow row;
        row.id = c.id; row.type = c.type;
        row.dimension = c.isDimensional(); row.driven = c.driven;
        for (const auto& h : c.refs) {
            if (!row.label.empty()) row.label += ", ";
            const auto* e = entById(skBaseId(h));
            row.label += e ? e->type + " " + h : h;
            row.external |= e && e->external;
            if (e && e->externalPierce && c.type == "coincident") row.type = "pierce";
            for (const auto& selected : selIds_)
                if (selected == h || skBaseId(selected) == skBaseId(h))
                    row.touchesSelection = true;
        }
        if (row.dimension) {
            const double value = c.hasMeasured ? c.measured : c.value;
            row.value = c.hasExpr ? "=" + c.expr :
                        c.type == "angle" ? fmtAng(value) : fmtLen(value);
            if (c.type != "angle") row.value += g_skIps ? " in" : " mm";
        }
        if (std::find(redundant_.begin(), redundant_.end(), c.id) != redundant_.end())
            row.state = RelationRow::State::Redundant;
        if (std::find(conflicting_.begin(), conflicting_.end(), c.id) != conflicting_.end())
            row.state = RelationRow::State::Conflicting;
        const auto skipped = skipped_.find(c.id);
        if (skipped != skipped_.end()) {
            row.state = RelationRow::State::Skipped;
            row.stateReason = skipped->second;
        }
        rows.push_back(std::move(row));
    }
    auto result = drawRelationsPanel(relationsPanel_, rows, selRelIds_, lo, hi);
    if (result.selectionChanged) selRelIds_ = std::move(result.selectIds);
    panelHoverRel_ = std::move(result.hoveredId);
    if (!result.deleteIds.empty()) {
        nlohmann::json ops = nlohmann::json::array();
        for (const auto& id : result.deleteIds)
            ops.push_back({{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", id}});
        applyRelationOps(st, ops, "Delete relations");
    }
}

// World axes parallel to a sketch basis direction use the solver's fixed virtual lines.
// All other axes go through the same associative conversion as model geometry.
static std::string sketchAxisHandle(const std::string& h, const float* frame) {
    if (h == "axis.h" || h == "axis.v") return h;
    const int a = h == "axis.x" ? 0 : h == "axis.y" ? 1 : h == "axis.z" ? 2 : -1;
    if (a < 0) return {};
    if (std::abs(frame[a]) > 1.0f - 1e-6f) return "axis.h";
    if (std::abs(frame[4+a]) > 1.0f - 1e-6f) return "axis.v";
    return {};
}

void SketchMode::beginRelations(AppState& st) {
    const auto selected = selIds_;
    resetTool();
    selIds_ = selected;
    relIds_ = selected;
    relPopupOpen_ = true;
    // A keyboard/ribbon invocation with no pointer over the window (or a headless run) has no
    // valid mouse position; anchor at the window center instead of off screen.
    relPopupPos_ = ImGui::IsMousePosValid() ? ImGui::GetMousePos()
                                            : ImGui::GetMainViewport()->GetCenter();
    refreshRelationModelRefs(st);
}

void SketchMode::refreshRelationModelRefs(AppState& st) {
    nlohmann::json refs = nlohmann::json::array();
    for (const auto& h : relIds_)
        if (h.rfind("axis.", 0) == 0 && sketchAxisHandle(h, frame_).empty())
            refs.push_back({{"datum", h}});
    bool pending = false;
    for (const auto& item : st.selection.items()) {
        const auto kind = item.hit.kind;
        const char* name = kind == render::PickKind::Vertex ? "vertex" :
                           kind == render::PickKind::Edge ? "edge" :
                           kind == render::PickKind::Face ? "face" : nullptr;
        if (!name) continue;
        if (item.selector.empty()) { pending = true; continue; }
        auto selector = nlohmann::json::parse(item.selector, nullptr, false);
        if (selector.is_object()) refs.push_back({{"kind", name}, {"selector", selector}});
    }
    const std::string key = std::to_string(st.docMirror.revision()) + ":" + refs.dump() +
                            (pending ? ":pending" : "");
    if (key == relModelKey_) return;
    relModelKey_ = key;
    relModelRefs_.clear();
    for (const auto& ref : refs) {
        RelationModelRef model;
        model.source = ref;
        nlohmann::json res;
        if (callSketch(st, "sketch_project_entities",
                       {{"refs", nlohmann::json::array({ref})}}, res)) {
            parseEntities(res, model.entities);
        }
        model.labels.reserve(model.entities.size());
        for (size_t i = 0; i < model.entities.size(); ++i)
            model.labels.push_back(model.entities[i].type + " " + std::to_string(i + 1));
        if (ref.value("kind", std::string()) == "edge") {
            auto pierce = ref;
            pierce["mode"] = "pierce";
            // A read-only probe rejects coplanar/no-crossing edges; those must not offer Pierce.
            nlohmann::json probe;
            std::string error;
            model.canPierce = st.worker.call("sketch_project_ref",
                {{"doc_id", st.docMirror.docId()}, {"sketch", fid_}, {"ref", pierce}},
                probe, error) && !probe.contains("code") && probe.contains("entity");
        }
        relModelRefs_.push_back(std::move(model));
    }
}

void SketchMode::drawAddRelations(AppState& st, bool& escHandled) {
    if (!relPopupOpen_ && !relModelPicking_) return;
    refreshRelationModelRefs(st);
    if (relModelPicking_) {
        ImGui::SetNextWindowPos(relPopupPos_, ImGuiCond_Appearing);
        if (ImGui::Begin("Select relation references##SketchRelPick", nullptr,
                         ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
            ui::kit::dimText("Pick model vertices, edges or faces. Ctrl-click adds more.");
            ui::kit::dimText("Sketch selection is retained while picking model geometry.");
            if (ui::kit::primaryButton("Use selected geometry##relation_use_model")) {
                relModelPicking_ = false; relPopupOpen_ = true;
                relPopupPos_ = ImGui::GetMousePos();
            }
            if (ui::kit::secondaryButton("Cancel##relation_cancel_model") ||
                ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                relModelPicking_ = false; relPopupOpen_ = false; escHandled = true;
            }
        }
        ImGui::End();
        return;
    }

    struct Target {
        std::string ref, type;
        bool point = false, splineEnd = false;
        int source = -1;
    };
    std::vector<Target> targets;
    auto addTarget = [&](const std::string& h, const SketchEntity* e, int source) {
        Target t;
        t.ref = h; t.source = source;
        t.type = e ? e->type : "line";
        const auto dot = h.find('.');
        const auto sub = dot == std::string::npos ? std::string() : h.substr(dot + 1);
        t.point = h == "origin" || (e && (e->type == "point" || !sub.empty()));
        if (e && (e->type == "spline" || e->type == "style_spline"))
            t.splineEnd = sub == "start" || sub == "end" || sub == "p0" ||
                          (!e->pts.empty() && sub == "p" + std::to_string(e->pts.size()-1));
        targets.push_back(std::move(t));
    };
    for (const auto& h : relIds_) {
        if (h.rfind("axis.", 0) == 0) {
            const auto axis = sketchAxisHandle(h, frame_);
            if (!axis.empty()) addTarget(axis, nullptr, -1);
        } else if (h == "origin") addTarget(h, nullptr, -1);
        else if (const auto* e = entById(skBaseId(h))) addTarget(h, e, -1);
    }
    size_t flat = 0;
    for (size_t i = 0; i < relModelRefs_.size(); ++i) {
        const auto& model = relModelRefs_[i];
        for (size_t j = 0; j < model.entities.size(); ++j, ++flat) {
            if ((int)j != model.selectedEntity) continue;
            const auto ref = flat == 0 ? "$0" : "$0#" + std::to_string(flat);
            addTarget(ref, &model.entities[j], (int)i);
        }
    }
    auto line = [](const Target& t) { return !t.point && (t.type == "line" || t.type == "slot"); };
    auto circle = [](const Target& t) { return !t.point && (t.type == "circle" || t.type == "arc"); };
    auto ellipse = [](const Target& t) { return !t.point && (t.type == "ellipse" || t.type == "ellipse_arc"); };
    auto curve = [](const Target& t) {
        return !t.point && (t.type == "line" || t.type == "circle" || t.type == "arc" ||
            t.type == "ellipse" || t.type == "ellipse_arc" || t.type == "spline" ||
            t.type == "style_spline" || t.type == "conic" || t.type == "parabola");
    };
    auto lengthCurve = [](const Target& t) { return !t.point && (t.type == "line" || t.type == "arc"); };
    ImGui::SetNextWindowPos(relPopupPos_, ImGuiCond_Appearing);
    if (ImGui::Begin("Add Relation##SketchRel", nullptr,
                     ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        ui::kit::sectionHeader("Relations");
        bool referencesChanged = false;
        const bool referencesPending = std::any_of(st.selection.items().begin(), st.selection.items().end(),
            [](const auto& item) { return modelPickKind(item.hit.kind) && item.selector.empty(); });
        for (size_t i = 0; i < relModelRefs_.size(); ++i) {
            auto& model = relModelRefs_[i];
            if (model.entities.size() < 2) continue;
            ImGui::PushID((int)i);
            referencesChanged |= ui::kit::fieldCombo("boundary", "Projected boundary",
                &model.selectedEntity, model.labels);
            ImGui::PopID();
        }
        bool projectionsReady = true;
        for (const auto& model : relModelRefs_) {
            if (!model.entities.empty()) continue;
            projectionsReady = false;
            ui::kit::dimText(model.canPierce ? "This edge only supports a Pierce reference here." :
                                             "This reference does not project into the sketch.");
        }
        if (referencesPending) {
            bool failed = false;
            for (const auto& item : st.selection.items())
                if (modelPickKind(item.hit.kind) && !item.resolution_error.empty()) {
                    ui::kit::dimText(item.resolution_error.c_str()); failed = true;
                }
            if (!failed) ui::kit::dimText("Resolving selected model geometry...");
        }
        bool any = false, applied = false;
        const auto& snapshot = targets;
        auto offer = [&](const char* label, const char* type,
                         const std::vector<std::vector<std::string>>& groups) {
            any = true;
            if (applied || !ui::kit::secondaryButton(label,
                    projectionsReady && !referencesPending && !referencesChanged,
                    referencesPending ? "Waiting for selected model geometry" :
                    !projectionsReady ? "Choose a projectable reference" : nullptr)) return;
            nlohmann::json ops = nlohmann::json::array(), refs = nlohmann::json::array();
            for (const auto& model : relModelRefs_) refs.push_back(model.source);
            if (!refs.empty())
                ops.push_back({{"op", "sketch_convert"}, {"sketch", fid_}, {"refs", refs},
                               {"construction", true}, {"fix", true}, {"associative", true}});
            for (const auto& group : groups)
                ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                               {"constraint", {{"type", type}, {"refs", group}}}});
            applied = true;
            addRelationOps(st, ops, label);
        };
        const size_t n = snapshot.size();
        auto all = [&](const auto& pred) {
            return n > 0 && std::all_of(snapshot.begin(), snapshot.end(), pred);
        };
        auto chain = [&](const char* label, const char* type) {
            std::vector<std::vector<std::string>> groups;
            for (size_t i = 1; i < n; ++i) groups.push_back({snapshot[0].ref, snapshot[i].ref});
            offer(label, type, groups);
        };
        auto each = [&](const char* label, const char* type) {
            std::vector<std::vector<std::string>> groups;
            for (const auto& t : snapshot)
                if (t.source < 0 && t.ref != "origin" && t.ref.rfind("axis.", 0) != 0)
                    groups.push_back({t.ref});
            if (!groups.empty()) offer(label, type, groups);
        };
        if (n >= 2 && all([](const Target& t) { return t.point; })) {
            chain("Coincident / Merge", "coincident");
            chain("Horizontal (align Y)", "horizontal");
            chain("Vertical (align X)", "vertical");
        }
        if (all(line)) {
            if (all([](const Target& t) { return t.type == "line"; })) {
                each("Horizontal (each)", "horizontal");
                each("Vertical (each)", "vertical");
            }
            if (n >= 2) {
                chain("Collinear", "collinear"); chain("Parallel", "parallel");
                if (n == 2) chain("Perpendicular", "perpendicular");
                if (all([](const Target& t) { return t.type == "line"; })) chain("Equal", "equal");
            }
        }
        if (n >= 2 && all(circle)) {
            chain("Equal", "equal"); chain("Coradial", "coradial");
        }
        if (n >= 2 && all(ellipse)) {
            chain("Equal", "equal");
        }
        if (n >= 2 && all([](const Target& t) { return !t.point && t.type == "slot"; }))
            chain("Equal slots (length and width)", "equal");
        if (n >= 2 && all([](const Target& t) { return !t.point && t.type == "arc_slot"; })) {
            chain("Equal arc slots (radius and width)", "equal");
        }
        if (n >= 2 && all([&](const Target& t) { return circle(t) || ellipse(t) ||
                                                        (!t.point && t.type == "arc_slot"); })) {
            chain("Concentric", "concentric");
        }
        if (n == 2) {
            const auto& a = snapshot[0]; const auto& b = snapshot[1];
            if (all(lengthCurve)) chain("Equal curve length", "equal_curve_length");
            if ((a.splineEnd && (curve(b) || b.splineEnd)) ||
                (b.splineEnd && (curve(a) || a.splineEnd))) {
                const auto& end = a.splineEnd ? a : b;
                const auto& other = a.splineEnd ? b : a;
                offer("Tangent at spline endpoint", "tangent", {{end.ref, other.ref}});
                if (other.splineEnd || other.type == "line" || circle(other))
                    offer("Smooth (equal curvature)", "smooth", {{end.ref, other.ref}});
            } else if (!a.point && !b.point &&
                       ((a.type == "line" && (circle(b) || ellipse(b))) ||
                        (b.type == "line" && (circle(a) || ellipse(a))) ||
                        ((circle(a) || ellipse(a)) && (circle(b) || ellipse(b))))) {
                chain("Tangent", "tangent");
            }
            if (a.point != b.point) {
                const auto& p = a.point ? a : b; const auto& c = a.point ? b : a;
                if (curve(c))
                    offer(c.source >= 0 ? "On Edge" : "On curve", "point_on", {{p.ref, c.ref}});
                if (c.type == "line" || c.type == "arc")
                    offer("Midpoint", "midpoint", {{p.ref, c.ref}});
                if (circle(c) || ellipse(c) || c.type == "arc_slot") {
                    offer("Coincident to center", "coincident", {{p.ref, c.ref + ".center"}});
                    offer("Concentric with point", "concentric", {{p.ref, c.ref}});
                }
            }
        }
        if (n == 3) {
            std::vector<const Target*> points, curves;
            for (const auto& t : snapshot) {
                if (t.point) points.push_back(&t);
                else if (curve(t)) curves.push_back(&t);
            }
            if (points.size() == 1 && curves.size() == 2)
                offer("Intersection", "intersection", {{points[0]->ref, curves[0]->ref, curves[1]->ref}});
            // An explicit virtual axis takes precedence; otherwise the last selected line is axis.
            int axis = -1;
            for (int i = 0; i < 3; ++i)
                if (snapshot[i].ref.rfind("axis.", 0) == 0) axis = i;
            if (axis < 0)
                for (int i = 0; i < 3; ++i)
                    if (snapshot[i].type == "line" && !snapshot[i].point) axis = i;
            if (axis >= 0) {
                std::vector<const Target*> subjects;
                for (int i = 0; i < 3; ++i) if (i != axis) subjects.push_back(&snapshot[i]);
                if ((subjects[0]->point && subjects[1]->point) ||
                    (circle(*subjects[0]) && circle(*subjects[1])) ||
                    (!subjects[0]->point && !subjects[1]->point &&
                     subjects[0]->type == "line" && subjects[1]->type == "line"))
                    offer("Symmetric (last line / selected axis)", "symmetric",
                          {{subjects[0]->ref, subjects[1]->ref, snapshot[axis].ref}});
            }
        }
        // Pierce is a coincident relation to the associative edge/plane intersection point.
        if (!applied && relIds_.size() == 1 && relModelRefs_.size() == 1 &&
            relModelRefs_[0].canPierce) {
            float u = 0, v = 0;
            const auto& h = relIds_[0];
            const auto* e = entById(skBaseId(h));
            if ((h == "origin" || (e && (e->type == "point" || h.find('.') != std::string::npos))) &&
                relationPointUV(h, u, v)) {
                any = true;
                if (ui::kit::secondaryButton("Pierce##relation_pierce")) {
                    auto ref = relModelRefs_[0].source;
                    ref["mode"] = "pierce"; ref["near"] = {u, v};
                    auto ops = nlohmann::json::array({
                        {{"op", "sketch_convert"}, {"sketch", fid_}, {"refs", nlohmann::json::array({ref})},
                         {"construction", true}, {"fix", true}, {"associative", true}},
                        {{"op", "sketch_add_constraint"}, {"sketch", fid_},
                         {"constraint", {{"type", "coincident"}, {"refs", {h, "$0"}}}}}});
                    addRelationOps(st, ops, "Pierce"); applied = true;
                }
            }
        }
        if (n == 1 && relModelRefs_.empty()) each("Fix", "fix");
        if (!applied && n == 1 && relModelRefs_.empty() && !snapshot[0].point &&
            snapshot[0].type == "arc" &&
            ui::kit::secondaryButton("Arc length dimension...##relation_arc_length")) {
            const auto* e = entById(snapshot[0].ref);
            if (e) {
                float sweep = e->end - e->start;
                if (e->ccw) { while (sweep < 0) sweep += 360.0f; }
                else { while (sweep > 0) sweep -= 360.0f; }
                dimType_ = "arc_length"; dimMeasured_ = e->radius * std::abs(sweep) * 3.141592653589793 / 180.0;
                dimPending_ = {{"type", dimType_}, {"refs", {e->id}}};
                SketchConstraint con; con.type = dimType_; con.refs = {e->id};
                defaultPlacement(con, dimPlaceU_, dimPlaceV_);
                tool_ = SketchTool::SmartDim; relPopupOpen_ = false; relIds_.clear();
                dimOpenModify(ImGui::GetMousePos()); applied = true;
            }
        }
        if (!any) ui::kit::dimText("No relations for this selection.");
        if (!applied && ui::kit::secondaryButton("Select model geometry...##relation_model_pick")) {
            relModelPicking_ = true; relPopupOpen_ = false;
        }
        if (!applied && (ui::kit::secondaryButton("Cancel##relation_cancel") ||
                        ImGui::IsKeyPressed(ImGuiKey_Escape))) {
            escHandled = true; relPopupOpen_ = false; relIds_.clear();
        }
    }
    ImGui::End();
}

bool SketchMode::smartDimPicking() const {
    return active_ && tool_ == SketchTool::SmartDim && !dimPopupOpen_ && dimPicks_.size() < 2;
}

// Project external geometry (model edge / planar face / datum plane) into the sketch as a FIXED
// construction entity and chain it into the Smart Dimension pick buffer. The worker does the 3D
// math (sketch_project_ref); the entity goes through the normal ops path so it undoes cleanly.
std::string SketchMode::addProjectedDimRef(AppState& st, const nlohmann::json& ref,
                                           const std::string& srcKey) {
    std::string eid;
    auto it = projRefs_.find(srcKey);
    if (it != projRefs_.end() && entById(it->second)) {
        eid = it->second;   // same source picked before — reuse its reference entity
    } else {
        nlohmann::json res;
        if (!callSketch(st, "sketch_project_ref", {{"ref", ref}}, res)) {
            st.toast(ui::ToastLevel::Warn,
                     "Can't dimension to that reference (projection not supported)");
            return std::string();
        }
        if (!res.contains("entity") || !res["entity"].is_object()) return std::string();
        nlohmann::json ent = res["entity"];
        ent["construction"] = true;
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", ent},
            {"constraints", nlohmann::json::array(
                {{{"type", "fix"}, {"refs", nlohmann::json::array({"$self"})}}})}};
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        if (!r.ok) { st.addLog("[sketch] project ref failed: " + r.error_message); return std::string(); }
        eid = (r.id_map.is_object() && r.id_map.contains("$0"))
            ? r.id_map["$0"].get<std::string>() : std::string();
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
        if (eid.empty()) return eid;
        projRefs_[srcKey] = eid;
    }
    const SketchEntity* e = entById(eid);
    if (!e) return std::string();
    DimPick p;
    if (e->type == "point") { p.isPoint = true; p.handle = eid + ".p"; p.u = e->p1[0]; p.v = e->p1[1]; }
    else { p.entId = eid; p.entIdx = entIndexById(eid); }
    for (const auto& q : dimPicks_)
        if (q.isPoint == p.isPoint && (p.isPoint ? q.handle == p.handle : q.entId == p.entId))
            return eid;   // already in the buffer
    dimPicks_.push_back(p);
    return eid;
}

void SketchMode::addPlaneDimRef(AppState& st, int planeIdx) {
    static const char* kDatum[3] = {"front", "top", "right"};
    if (planeIdx < 0 || planeIdx > 2 || !smartDimPicking()) return;
    addProjectedDimRef(st, nlohmann::json{{"datum", kDatum[planeIdx]}},
                       std::string("datum:") + kDatum[planeIdx]);
}

// A rejected compile returns an ops envelope with the original revision, not an RPC error.
static bool compoundBatchFailed(AppState& st, const core::OpsResponse& r, int beforeRevision,
                                const char* name) {
    if (r.ok && r.revision != beforeRevision) return false;
    std::string msg = r.error_message;
    if (msg.empty() && r.feature_status.is_array()) {
        for (const auto& status : r.feature_status) {
            if (status.is_object() && !status.value("ok", true)) {
                msg = status.value("error", std::string());
                if (!msg.empty()) break;
            }
        }
    }
    if (msg.empty()) msg = std::string(name) + " could not be built";
    st.addLog("[sketch] " + std::string(name) + " failed: " + msg);
    st.toast(ui::ToastLevel::Warn, msg);
    return true;
}

// Emit 4 lines as a closed loop (corners A,B,C,D). Each edge i (corners[i]->corners[(i+1)%4]) gets
// a chained coincident to the previous edge's p2, plus the per-edge relation in relations[i] (one
// of "horizontal"/"vertical"/"parallel"/"perpendicular"/"equal"/"". Batch placeholders
// keep the entities and every relation in one atomic document mutation.
std::vector<std::string> SketchMode::addQuadLoop(AppState& st, const ImVec2 corners[4],
                             const char* relations[4], const std::string* cornerSnaps,
                             const ImVec2* center, const std::string& centerSnap) {
    nlohmann::json ops = nlohmann::json::array();
    auto ref = [](int i) { return "$" + std::to_string(i); };
    for (int i = 0; i < 4; ++i) {
        int j = (i + 1) % 4;
        nlohmann::json cs = nlohmann::json::array();
        const char* rel = relations[i];
        if (rel && (std::strcmp(rel, "horizontal") == 0 || std::strcmp(rel, "vertical") == 0))
            cs.push_back({{"type", rel}, {"refs", {"$self"}}});
        if (i)
            cs.push_back({{"type", "coincident"}, {"refs", {ref(i - 1) + ".p2", "$self.p1"}}});
        if (i == 3)
            cs.push_back({{"type", "coincident"}, {"refs", {"$self.p2", "$0.p1"}}});
        if (cornerSnaps) pushSnapConstraint(cornerSnaps[i], "$self.p1", cs);
        nlohmann::json ent = {{"type", "line"},
            {"p1", nlohmann::json::array({corners[i].x, corners[i].y})},
            {"p2", nlohmann::json::array({corners[j].x, corners[j].y})}};
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", fid_},
                       {"entity", ent}, {"constraints", cs}});
    }
    // Opposite-edge relations are applied after all referenced edges have been minted.
    auto pair = [&](const char* type, int a, int b) {
        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                       {"constraint", {{"type", type}, {"refs", {ref(a), ref(b)}}}}});
    };
    for (int i = 0; i < 2; ++i) {
        const char* rel = relations[i];
        if (!rel) continue;
        std::string tok(rel);
        if (tok.find("parallel") != std::string::npos) pair("parallel", i, i + 2);
        if (tok.find("equal") != std::string::npos) pair("equal", i, i + 2);
        if (tok.find("perpendicular") != std::string::npos) pair("perpendicular", i, i + 1);
    }
    if (center) {
        // Center rectangle's construction point and diagonal belong to the same transaction.
        const std::string pointRef = ref(static_cast<int>(ops.size()));
        nlohmann::json pcs = nlohmann::json::array();
        pushSnapConstraint(centerSnap, "$self.p", pcs);
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", fid_},
                       {"entity", {{"type", "point"}, {"construction", true},
                                   {"p", {center->x, center->y}}}}, {"constraints", pcs}});
        nlohmann::json dcs = nlohmann::json::array({
            {{"type", "coincident"}, {"refs", {"$self.p1", "$0.p1"}}},
            {{"type", "coincident"}, {"refs", {"$self.p2", "$2.p1"}}},
            {{"type", "midpoint"}, {"refs", {pointRef + ".p", "$self"}}}});
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", fid_},
                       {"entity", {{"type", "line"}, {"construction", true},
                                   {"p1", {corners[0].x, corners[0].y}},
                                   {"p2", {corners[2].x, corners[2].y}}}},
                       {"constraints", dcs}});
    }
    const int beforeRevision = st.docMirror.revision();
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (compoundBatchFailed(st, r, beforeRevision, "Rectangle")) return {};
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    std::vector<std::string> ids;
    ids.reserve(4);
    for (int i = 0; i < 4; ++i) ids.push_back(r.id_map[ref(i)].get<std::string>());
    return ids;
}

// C42: N-line closed loop (generalized addQuadLoop without the per-edge h/v relations). Each edge
// gets a chained coincident to its predecessor's p2 (+ loop-closing coincident); equalEdges adds
// "equal" between consecutive edges so a Polygon stays regular-ish under drag.
std::vector<std::string> SketchMode::addPolyLoop(AppState& st, const std::vector<ImVec2>& corners,
                                                 bool equalEdges, const ImVec2* center,
                                                 const std::string& centerSnap,
                                                 const std::string& vertexSnap) {
    const size_t n = corners.size();
    if (n < 3) return {};
    nlohmann::json ops = nlohmann::json::array();
    auto ref = [](size_t i) { return "$" + std::to_string(i); };
    for (size_t i = 0; i < n; ++i) {
        size_t j = (i + 1) % n;
        nlohmann::json cs = nlohmann::json::array();
        if (i)
            cs.push_back({{"type", "coincident"}, {"refs", {ref(i - 1) + ".p2", "$self.p1"}}});
        if (i == n - 1)
            cs.push_back({{"type", "coincident"}, {"refs", {"$self.p2", "$0.p1"}}});
        if (i == 0 && center) pushSnapConstraint(vertexSnap, "$self.p1", cs);
        nlohmann::json ent = {{"type", "line"},
            {"p1", nlohmann::json::array({corners[i].x, corners[i].y})},
            {"p2", nlohmann::json::array({corners[j].x, corners[j].y})}};
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", fid_},
                       {"entity", ent}, {"constraints", cs}});
    }
    if (equalEdges) {
        for (size_t i = 0; i + 1 < n; ++i)
            ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                           {"constraint", {{"type", "equal"}, {"refs", {ref(i), ref(i + 1)}}}}});
    }
    if (center) {
        const std::string circleRef = ref(ops.size());
        const float radius = std::hypot(corners[0].x - center->x, corners[0].y - center->y);
        nlohmann::json cs = nlohmann::json::array();
        pushSnapConstraint(centerSnap, "$self.center", cs);
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", fid_},
                       {"entity", {{"type", "circle"}, {"construction", true},
                                   {"center", {center->x, center->y}}, {"radius", radius}}},
                       {"constraints", cs}});
        for (size_t i = 0; i < n; ++i)
            ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                           {"constraint", {{"type", "point_on"},
                                           {"refs", {ref(i) + ".p1", circleRef}}}}});
    }
    const int beforeRevision = st.docMirror.revision();
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (compoundBatchFailed(st, r, beforeRevision, "Polygon")) return {};
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    std::vector<std::string> ids;
    ids.reserve(n);
    for (size_t i = 0; i < n; ++i) ids.push_back(r.id_map[ref(i)].get<std::string>());
    return ids;
}

// C51: straight slot as real geometry (see header). Builds the capsule from the spine endpoints
// p1/p2 (the arc centers) and the full width: 2 side lines tangent-offset by half-width, 2 cap
// arcs (the outer semicircles), and a construction centerline. Endpoints are chained by coincident
// closure; the centerline ends pin to the arc centers; the cap radii are equal. One atomic batch
// commits all entities and relations as a single undo step.
void SketchMode::addDecomposedSlot(AppState& st, const ImVec2& p1, const ImVec2& p2, float width,
                                   const std::string& snap1, const std::string& snap2) {
    const float R2D = 57.29577951f, D2R = 1.0f / 57.29577951f;
    float hw = width * 0.5f;
    float axisDeg = std::atan2(p2.y - p1.y, p2.x - p1.x) * R2D;
    float nx = -std::sin(axisDeg * D2R), ny = std::cos(axisDeg * D2R);
    ImVec2 A1(p1.x + nx * hw, p1.y + ny * hw), B1(p2.x + nx * hw, p2.y + ny * hw);
    ImVec2 A2(p1.x - nx * hw, p1.y - ny * hw), B2(p2.x - nx * hw, p2.y - ny * hw);

    nlohmann::json ops = nlohmann::json::array();
    auto add = [&](const nlohmann::json& entity) {
        ops.push_back({{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}});
    };
    add({{"type", "line"}, {"p1", {A1.x, A1.y}}, {"p2", {B1.x, B1.y}}}); // $0
    add({{"type", "line"}, {"p1", {A2.x, A2.y}}, {"p2", {B2.x, B2.y}}}); // $1
    add({{"type", "arc"}, {"center", {p1.x, p1.y}}, {"radius", hw},
         {"start", axisDeg + 90.0f}, {"end", axisDeg - 90.0f}, {"ccw", true}}); // $2
    add({{"type", "arc"}, {"center", {p2.x, p2.y}}, {"radius", hw},
         {"start", axisDeg - 90.0f}, {"end", axisDeg + 90.0f}, {"ccw", true}}); // $3
    add({{"type", "line"}, {"construction", true},
         {"p1", {p1.x, p1.y}}, {"p2", {p2.x, p2.y}}}); // $4
    auto con = [&](const nlohmann::json& c) {
        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", c}});
    };
    con({{"type", "coincident"}, {"refs", {"$0.p1", "$2.start"}}});
    con({{"type", "coincident"}, {"refs", {"$0.p2", "$3.end"}}});
    con({{"type", "coincident"}, {"refs", {"$1.p1", "$2.end"}}});
    con({{"type", "coincident"}, {"refs", {"$1.p2", "$3.start"}}});
    con({{"type", "coincident"}, {"refs", {"$4.p1", "$2.center"}}});
    con({{"type", "coincident"}, {"refs", {"$4.p2", "$3.center"}}});
    con({{"type", "equal"}, {"refs", {"$2", "$3"}}});
    // Tangency at both caps holds each side tangent while the slot is dragged.
    con({{"type", "tangent"}, {"refs", {"$0", "$2"}}});
    con({{"type", "tangent"}, {"refs", {"$0", "$3"}}});
    con({{"type", "tangent"}, {"refs", {"$1", "$2"}}});
    con({{"type", "tangent"}, {"refs", {"$1", "$3"}}});
    nlohmann::json endCs = nlohmann::json::array();
    pushSnapConstraint(snap1, "$2.center", endCs);
    pushSnapConstraint(snap2, "$3.center", endCs);
    for (const auto& c : endCs) con(c);

    const int beforeRevision = st.docMirror.revision();
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (compoundBatchFailed(st, r, beforeRevision, "Slot")) return;
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

// ---------------------------------------------------------------------------
// enter / exit
// ---------------------------------------------------------------------------

bool SketchMode::cameraLock(float& yaw, float& pitch) const {
    if (!active_) return false;
    yaw = camYaw_; pitch = camPitch_;
    return true;
}

bool SketchMode::enterForFeature(AppState& st, const std::string& sketchFid) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    if (sketchFid.empty()) return false;
    fid_ = sketchFid;
    nlohmann::json res;
    if (!callSketch(st, "sketch_enter", nlohmann::json::object(), res)) { fid_.clear(); return false; }
    applyEnterResult(res);
    active_ = true;
    docId_ = st.docMirror.docId();
    isNewEmpty_ = false;
    resetTool();
    // C34: remember the pre-sketch camera so exit can restore the user's 3D view
    // (SolidWorks returns to the previous orientation when a sketch closes).
    prevYaw_ = st.yaw; prevPitch_ = st.pitch; prevZoom_ = st.zoom;
    prevPan_[0] = st.pan[0]; prevPan_[1] = st.pan[1]; prevPan_[2] = st.pan[2];
    havePrevCam_ = true;
    // Recentre on the sketch: zero the pan so the orbit-centre override (the sketch geometry centre,
    // set each frame by the viewport) sits at screen centre — the sketch, not the origin, is framed.
    // prevPan_ is restored on exit, so the 3D view returns to where it was.
    st.pan[0] = st.pan[1] = st.pan[2] = 0.0f;
    // Lock the camera looking down the plane normal (frame_ column 2 = z_dir in world).
    float nz0 = frame_[2], nz1 = frame_[6], nz2 = frame_[10];
    // The renderer's orbit convention (viewport_panel/renderer drawCurrent) is
    //   dir(yaw,pitch) = (cos(p)*sin(y), -cos(p)*cos(y), sin(p))   [target->eye]
    // For a fronto-parallel view we need dir == +normal:
    //   pitch = asin(nz), yaw = atan2(nx, -ny).
    // (The old atan2(ny, nx) put Front/Right plane sketches EDGE-ON: the u-axis projected to
    // ~0 screen px, buildBasis went degenerate, and every sketch tool silently died.)
    camPitch_ = std::asin(std::fmax(-1.0f, std::fmin(1.0f, nz2))) * 57.2957795f;
    camYaw_ = std::atan2(nz0, -nz1) * 57.2957795f;
    if (std::fabs(camPitch_) > 88.9f) camPitch_ = camPitch_ > 0 ? 89.0f : -89.0f;
    st.yaw = camYaw_; st.pitch = camPitch_;
    // C37-D: snapshot the doc so the confirmation-corner ✗ (cancel) can revert the whole session.
    // Best-effort: if the worker lacks doc_checkpoint the corner falls back to a plain exit.
    cancelCheckpoint_.clear();
    if (st.docMirror.hasDoc() && st.workerStarted) {
        nlohmann::json cres, cp = {{"doc_id", st.docMirror.docId()}};
        std::string cerr;
        if (st.worker.call("doc_checkpoint", cp, cres, cerr) && cres.contains("checkpoint"))
            cancelCheckpoint_ = cres.value("checkpoint", std::string());
    }
    st.addLog("[sketch] entered " + fid_ + "  (dof=" + std::to_string(dof_) + ")");
    return true;
}

// C37-D: create a sketch on an explicit plane ref and enter it. Returns the new feature id.
std::string SketchMode::createSketchOnPlane(AppState& st, const nlohmann::json& planeRef) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return {};
    }
    if (!st.docMirror.hasDoc()) {
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) { st.addLog("[sketch] newPart failed: " + err); return {}; }
        st.clearViewportForNewDoc();
    }
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "sketch"}, {"name", "Sketch"},
            {"refs", {{"plane", planeRef}}},
            {"params", {{"entities", nlohmann::json::array()},
                        {"constraints", nlohmann::json::array()}}}}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) { st.addLog("[sketch] create sketch failed: " + r.error_message); return {}; }
    std::string newFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                       ? r.id_map["$0"].get<std::string>() : std::string();
    if (newFid.empty()) { st.addLog("[sketch] create sketch: no id"); return {}; }
    if (!enterForFeature(st, newFid)) return {};
    isNewEmpty_ = true;
    return newFid;
}

void SketchMode::beginPlaneEdit(AppState& st, const std::string& sketchFid) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return;
    }
    if (sketchFid.empty()) return;
    // The plane-pick overlay can't share the screen with an open sketcher — leave any active sketch
    // first (keep its entities), then arm the reparent pick.
    if (active_) exitSketch(st, /*keep=*/true);
    planeEditFid_ = sketchFid;
    docId_ = st.docMirror.docId();
    planePicking_ = true;
    planeHover_ = -1;
    st.sketchPlanePickActive = true;
    st.requestCursor(ui::CursorKind::Crosshair);
    st.toast(ui::ToastLevel::Info, "Pick the new sketch plane (or a planar face)");
}

std::string SketchMode::consumePlanePick(AppState& st, const nlohmann::json& planeRef) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        planePicking_ = false;
        planeEditFid_.clear();
        st.sketchPlanePickActive = false;
        return {};
    }
    // New-sketch path: defer to createSketchOnPlane and let the CALLER own the pick-flag lifecycle
    // (so a non-planar face leaves the user still in pick mode, as before).
    if (planeEditFid_.empty()) return createSketchOnPlane(st, planeRef);

    // Re-parent path: there's no sketch to enter afterwards, so clear the pick-arm flags here or the
    // overlay would stay up. Then patch refs.plane on the existing sketch and rebuild the document.
    // edit_feature merges refs, so the sketch (and every feature consuming it) recompiles on the new
    // plane.
    planePicking_ = false;
    st.sketchPlanePickActive = false;
    std::string fid = planeEditFid_;
    planeEditFid_.clear();
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "edit_feature"}, {"id", fid}, {"refs", {{"plane", planeRef}}}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) {
        st.addLog("[sketch] reparent plane failed: " + r.error_message);
        st.toast(ui::ToastLevel::Warn, "Couldn't move the sketch to that plane");
        return {};
    }
    if (!r.aicm_path.empty()) {
        std::string le;
        st.loadDocMesh(r.aicm_path, le);
        if (!le.empty()) st.addLog("[err] loadDocMesh (reparent): " + le);
    }
    resyncMirror(st);
    st.selectedFeatureId = fid;
    st.toast(ui::ToastLevel::Info, "Sketch plane updated");
    return fid;
}

bool SketchMode::enterNewOnSelection(AppState& st) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    // C38 (S2): a double-fire of the ribbon "New Sketch" (sketch_new) while a sketch is already
    // entered would start a plane-pick over the live sketch and leave a zombie state. Refuse to
    // re-enter when already active — the user must exit the current sketch first.
    if (active_) return false;
    // C37-D: if the selection already names a usable plane/planar face, enter directly. Otherwise
    // start the SolidWorks plane-pick step (drawOverlay handles the ray-pick while the flag is set).
    nlohmann::json planeRef;
    bool haveRef = false;
    for (const auto& it : st.selection.items()) {
        if (!it.selector.empty()) {
            try {
                nlohmann::json sel = nlohmann::json::parse(it.selector);
                std::string k = sel.value("kind", std::string());
                if (k == "face" || k == "datum") { planeRef = sel; haveRef = true; break; }
            } catch (...) {}
        }
    }
    // Pre-selected plane (SolidWorks "select a plane, then Sketch"): a plane held in
    // st.selectedFeatureId — either a default origin plane ("plane.front/top/right") or a created
    // datum-plane feature — starts the sketch directly on it. Routed as a BARE string so the engine
    // resolves it as a plane name / feature id (same form createSketchOnPlane gets from a tree click).
    if (!haveRef && !st.selectedFeatureId.empty()) {
        const std::string& sfid = st.selectedFeatureId;
        bool isPlane = (sfid == "plane.front" || sfid == "plane.top" || sfid == "plane.right");
        if (!isPlane) {
            const nlohmann::json& tree = st.docMirror.tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid &&
                        tf.value("type", std::string()) == "plane") { isPlane = true; break; }
        }
        if (isPlane) { planeRef = sfid; haveRef = true; }
    }
    if (haveRef) {
        // Issue #20: the pre-selected plane is now CONSUMED — clear it so it can't silently drive
        // the NEXT "New Sketch" onto the same plane. Both the FeatureManager plane row
        // (feature_tree.cpp) and the viewport plane chip (viewport_panel.cpp) set
        // st.selectedFeatureId / st.selection to pre-select a plane, but nothing cleared it after
        // use — so a second sketch inherited the first sketch's plane and the plane-picker never
        // engaged (which also made planes un-clickable in the viewport). Clearing here makes a fresh
        // New Sketch re-present the picker unless the user deliberately pre-selects a plane again.
        st.selectedFeatureId.clear();
        st.selection.clear();
        return !createSketchOnPlane(st, planeRef).empty();
    }
    // No usable selection: enter the interactive plane-pick (front/top/right or a planar face).
    docId_ = st.docMirror.docId();
    planePicking_ = true;
    planeHover_ = -1;
    st.sketchPlanePickActive = true;
    st.requestCursor(ui::CursorKind::Crosshair);
    return false;   // not yet in a sketch — the pick click finishes the job
}

void SketchMode::exitSketch(AppState& st, bool keep) {
    if (!active_) return;
    cancelTool();
    nlohmann::json res;
    callSketch(st, "sketch_exit", nlohmann::json::object(), res);
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    // Resync the mirror revision (sketch_exit bumps it server-side).
    resyncMirror(st);
    // Empty-sketch cleanup: Finish removes empty sketches; Cancel removes only a new empty sketch.
    // If dependents exist, keep the feature and warn rather than cascading their deletion.
    if (!st.docLockedByAgent(docId_) && ents_.empty() && (keep || isNewEmpty_)) {
        std::vector<std::string> deps;
        collectSketchDependents(st, fid_, deps);
        if (deps.empty()) {
            nlohmann::json ops = nlohmann::json::array({
                {{"op", "delete_feature"}, {"id", fid_}, {"cascade", true}}});
            st.docMirror.applyOps(st.worker, ops);
        } else {
            st.toast(ui::ToastLevel::Warn,
                     emptyProfileToast(deps, featureDisplayName(st, fid_)));
        }
    }
    st.addLog("[sketch] exited " + fid_);
    active_ = false;
    isNewEmpty_ = false;
    fid_.clear();
    docId_.clear();
    ents_.clear();
    cons_.clear();              // C37-D
    autoPlace_.clear(); projRefs_.clear();         // C37-D
    cancelCheckpoint_.clear();  // C37-D
    status_.clear();
    dof_ = 0;
    // C38 (S2): unconditionally clear any plane-pick state so a stale pick flag can't survive an
    // exit (e.g. exit raced against an armed plane-pick) and trap the next session in pick mode.
    planePicking_ = false; planeHover_ = -1; st.sketchPlanePickActive = false;
    resetTool();
    // C34: glide back to the pre-sketch camera (the viewport resumes ticking the
    // animation now that sketch mode is off).
    if (havePrevCam_) {
        requestCameraAnimation(st, prevYaw_, prevPitch_, prevZoom_, prevPan_);
        havePrevCam_ = false;
    }
}

// C37-D: cancel the sketch session — revert to the entry checkpoint and leave sketch mode. The
// doc_revert response already carries the rebuilt mesh/tree (it bumps the revision), so we wire
// that directly instead of a second rebuild, then mirror the exit-path teardown.
void SketchMode::cancelSketch(AppState& st) {
    if (!active_) return;
    if (st.docLockedByAgent(docId_)) {
        exitSketch(st, /*keep=*/true);
        return;
    }
    cancelTool();
    if (cancelCheckpoint_.empty() || !st.docMirror.hasDoc() || !st.workerStarted) {
        // No checkpoint available — fall back to a plain exit (deletes an empty new sketch).
        exitSketch(st, /*keep=*/false);
        return;
    }
    // C38 (S6): exit the worker's active sketch BEFORE reverting. doc_revert rewinds the document but
    // leaves the worker's active_sketch dangling, so the next sketch_enter errors with "already in
    // sketch". sketch_exit clears it; we ignore its tess payload because the revert below rebuilds.
    {
        nlohmann::json exitRes;
        callSketch(st, "sketch_exit", nlohmann::json::object(), exitRes);
    }
    nlohmann::json res, params = {{"doc_id", st.docMirror.docId()}, {"checkpoint", cancelCheckpoint_}};
    std::string err;
    bool ok = st.worker.call("doc_revert", params, res, err) && !res.contains("code");
    if (!ok) {
        st.toast(ui::ToastLevel::Warn, "Sketch cancel: revert failed — exited without revert");
        exitSketch(st, /*keep=*/true);
        return;
    }
    // The revert response is an ops-style envelope: load its mesh + resync the mirror revision.
    if (res.contains("tess") && res["tess"].is_object()) {
        std::string aicm = res["tess"].value("aicm_path", std::string());
        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    }
    resyncMirror(st);
    // The checkpoint was taken right after a NEW sketch's (empty) feature was created, so the
    // revert above leaves that empty feature sitting in the tree — delete it too (matches the
    // plain-exit path's empty-new-sketch cleanup).
    if (isNewEmpty_) {
        nlohmann::json ops = nlohmann::json::array({
            {{"op", "delete_feature"}, {"id", fid_}, {"cascade", true}}});
        auto dr = st.docMirror.applyOps(st.worker, ops);
        if (dr.ok && !dr.aicm_path.empty()) { std::string le; st.loadDocMesh(dr.aicm_path, le); }
    }
    st.addLog("[sketch] cancelled " + fid_ + " (reverted to entry checkpoint)");
    st.toast(ui::ToastLevel::Info, "Sketch cancelled");
    // Teardown (mirror of exitSketch, minus the sketch_exit call — revert already rebuilt).
    active_ = false;
    isNewEmpty_ = false;
    fid_.clear();
    docId_.clear();
    ents_.clear();
    cons_.clear();
    autoPlace_.clear(); projRefs_.clear();
    cancelCheckpoint_.clear();
    status_.clear();
    dof_ = 0;
    // C38 (S2): same unconditional plane-pick clear as exitSketch.
    planePicking_ = false; planeHover_ = -1; st.sketchPlanePickActive = false;
    resetTool();
    if (havePrevCam_) {
        requestCameraAnimation(st, prevYaw_, prevPitch_, prevZoom_, prevPan_);
        havePrevCam_ = false;
    }
}

// ---------------------------------------------------------------------------
// ribbon dispatch
// ---------------------------------------------------------------------------

bool SketchMode::onRibbonCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "sketch_new") { enterNewOnSelection(st); return true; }
    if (!active_) return false;  // the rest of the sketch tools require an active sketch
    if (cmdId == "sketch_exit") { exitSketch(st, /*keep=*/true); return true; }
    if (cmdId == "sketch_cancel") { cancelSketch(st); return true; }  // ribbon commit cluster ✗
    if (cmdId == "sk_line")        { resetTool(); tool_ = SketchTool::Line;       return true; }
    // Centerline: same click-collect UX as Line, but commits a construction line + midpoint marker.
    if (cmdId == "sk_centerline")  { resetTool(); tool_ = SketchTool::Line; centerline_ = true; return true; }
    if (cmdId == "sk_rect_corner") { resetTool(); tool_ = SketchTool::RectCorner; return true; }
    if (cmdId == "sk_circle")      { resetTool(); tool_ = SketchTool::Circle;     return true; }
    if (cmdId == "sk_arc_center")  { resetTool(); tool_ = SketchTool::ArcCenter;  return true; }
    if (cmdId == "sk_slot")        { resetTool(); tool_ = SketchTool::Slot;       return true; }
    if (cmdId == "smart_dim")      { resetTool(); tool_ = SketchTool::SmartDim;   return true; }
    // C28-B flyout variants:
    if (cmdId == "sk_rect_3pt_corner") { resetTool(); tool_ = SketchTool::Rect3ptCorner;  return true; }
    if (cmdId == "sk_rect_3pt_center") { resetTool(); tool_ = SketchTool::Rect3ptCenter;  return true; }
    if (cmdId == "sk_parallelogram")   { resetTool(); tool_ = SketchTool::Parallelogram;  return true; }
    if (cmdId == "sk_circle_perimeter"){ resetTool(); tool_ = SketchTool::CirclePerimeter;return true; }
    if (cmdId == "sk_slot_ctr_str")    { resetTool(); tool_ = SketchTool::SlotCtrStr;     return true; }
    if (cmdId == "sk_slot_arc_ctr")    { resetTool(); tool_ = SketchTool::SlotArcCtr;     return true; }
    if (cmdId == "sk_slot_arc_3pt")    { resetTool(); tool_ = SketchTool::SlotArc3pt;     return true; }
    if (cmdId == "sk_ellipse_partial") { resetTool(); tool_ = SketchTool::EllipsePartial; return true; }
    if (cmdId == "sk_parabola")        { resetTool(); tool_ = SketchTool::Parabola;       return true; }
    if (cmdId == "sk_conic")           { resetTool(); tool_ = SketchTool::Conic;          return true; }
    // C29-B sketch-editing tools (engine ops sketch_trim / sketch_extend / sketch_offset):
    if (cmdId == "sk_trim")            { resetTool(); tool_ = SketchTool::Trim;           return true; }
    if (cmdId == "sk_extend")          { resetTool(); tool_ = SketchTool::Extend;         return true; }
    if (cmdId == "sk_offset_ent")      { resetTool(); tool_ = SketchTool::Offset;
                                         offsetStage_ = 0; offsetChain_.clear();           return true; }
    // C52: line-line corner fillet / chamfer (radius/distance persist across invocations).
    if (cmdId == "sk_fillet")          { resetTool(); tool_ = SketchTool::SkFillet;  corner1Id_.clear(); return true; }
    if (cmdId == "sk_chamfer")         { resetTool(); tool_ = SketchTool::SkChamfer; corner1Id_.clear(); return true; }
    // C53: mirror / linear+circular pattern / convert (numeric dims persist across invocations).
    if (cmdId == "sk_mirror")          { resetTool(); tool_ = SketchTool::Mirror;
                                         mpStage_ = 0; mpSel_.clear();                     return true; }
    if (cmdId == "sk_lin_pattern")     { resetTool(); tool_ = SketchTool::LinPattern;
                                         mpStage_ = 0; mpSel_.clear();                     return true; }
    if (cmdId == "sk_circ_pattern")    { resetTool(); tool_ = SketchTool::CircPattern;
                                         mpStage_ = 0; mpSel_.clear();                     return true; }
    // Convert the complete model selection in a single operation and undo step.
    if (cmdId == "sk_convert_ent") {
        resetTool();
        nlohmann::json refs = nlohmann::json::array();
        for (const auto& it : st.selection.items()) {
            const render::PickHit& h = it.hit;
            const char* kind = h.kind == render::PickKind::Vertex ? "vertex" :
                               h.kind == render::PickKind::Edge ? "edge" :
                               h.kind == render::PickKind::Face ? "face" : nullptr;
            if (!kind) continue;
            std::string perr;
            const std::string selStr = !it.selector.empty() ? it.selector :
                st.docMirror.resolvePick(st.worker, h.body_id, kind, h.topo_id, perr);
            auto selector = nlohmann::json::parse(selStr, nullptr, false);
            if (selector.is_object()) refs.push_back({{"kind", kind}, {"selector", selector}});
        }
        if (!refs.empty()) {
            applyConvertRefs(st, refs);
            st.selection.clear();
            tool_ = SketchTool::None;
        } else {
            tool_ = SketchTool::Convert;
        }
        return true;
    }
    // C54: Move/Copy entities (engine op sketch_move) — reuses the mirror/pattern selection stage.
    if (cmdId == "sk_move")            { resetTool(); tool_ = SketchTool::Move;
                                         mpStage_ = 0; mpSel_.clear(); moveCopy_ = false;   return true; }
    // Gizmo-driven bulk transforms (engine ops sketch_move / sketch_rotate); shared select stage.
    if (cmdId == "sk_move_entities")   { resetTool(); tool_ = SketchTool::MoveEntities;
                                         mpStage_ = 0; mpSel_.clear(); gizmoOriginInit_ = false;
                                         gzDragging_ = false; gzDragAxis_ = -1;
                                         moveDx_ = moveDy_ = 0.0f;                          return true; }
    if (cmdId == "sk_rotate_entities") { resetTool(); tool_ = SketchTool::RotateEntities;
                                         mpStage_ = 0; mpSel_.clear(); gizmoOriginInit_ = false;
                                         gzDragging_ = false; gzDragAxis_ = -1;
                                         rotCurAng_ = 0.0f; rotDidType_ = false;            return true; }
    // C54: Tangent Arc — draws an arc starting tangent to a prior/selected entity's free endpoint.
    if (cmdId == "sk_arc_tangent")     { resetTool(); tool_ = SketchTool::ArcTangent;      return true; }
    // C42: remaining draw tools.
    if (cmdId == "sk_arc_3pt")         { resetTool(); tool_ = SketchTool::Arc3pt;         return true; }
    if (cmdId == "sk_polygon")         { resetTool(); tool_ = SketchTool::Polygon;        return true; }
    // Polygon flyout variants — set polySides_ then arm the Polygon tool.
    if (cmdId == "sk_poly_tri")  { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 3; return true; }
    if (cmdId == "sk_poly_quad") { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 4; return true; }
    if (cmdId == "sk_poly_pent") { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 5; return true; }
    if (cmdId == "sk_poly_hex")  { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 6; return true; }
    if (cmdId == "sk_poly_oct")  { resetTool(); tool_ = SketchTool::Polygon; polySides_ = 8; return true; }
    if (cmdId == "sk_poly_star") { resetTool(); tool_ = SketchTool::Star;                  return true; }
    if (cmdId == "sk_rect_center")     { resetTool(); tool_ = SketchTool::RectCenter;     return true; }
    if (cmdId == "sk_point")           { resetTool(); tool_ = SketchTool::Point;          return true; }
    if (cmdId == "sk_ellipse" || cmdId == "sk_ellipse_big") {
        resetTool(); tool_ = SketchTool::Ellipse; return true;
    }
    if (cmdId == "sk_spline")          { resetTool(); tool_ = SketchTool::Spline;         return true; }
    // Style Spline — click-collect control poles (same UX as Spline); emits "style_spline".
    if (cmdId == "sk_spline_style")    { resetTool(); tool_ = SketchTool::StyleSpline;    return true; }
    // Sketch Text — arm a one-click placement; the click opens the text popup (string + height).
    if (cmdId == "sk_text")            { resetTool(); tool_ = SketchTool::Text;
                                         textPopupOpen_ = false;                           return true; }
    // Equation-Driven Curve — no viewport pick; open the x(t)/y(t)/range popup right away.
    if (cmdId == "sk_spline_equation") { resetTool(); tool_ = SketchTool::EqCurve;
                                         eqPopupOpen_ = true; eqPopupPos_ = ImGui::GetMousePos();
                                         eqFocus_ = true;                                  return true; }
    // Airfoil calculator — no viewport pick; open the NACA/.dat parameter popup right away.
    if (cmdId == "sk_airfoil")         { resetTool(); tool_ = SketchTool::Airfoil;
                                         afPopupOpen_ = true; afPopupPos_ = ImGui::GetMousePos();
                                         afFocus_ = true;                                  return true; }
    // C42: Construction — immediate toggle on the current selection (engine op
    // sketch_toggle_construction). Not a tool: the selection survives (entity ids persist).
    if (cmdId == "sk_construction") {
        if (selIds_.empty()) {
            st.toast(ui::ToastLevel::Info,
                     "Construction: select sketch entities first (click them with no tool armed)");
            return true;
        }
        nlohmann::json ops = nlohmann::json::array();
        for (const auto& id : selIds_) {
            if (id == "origin" || id.rfind("axis.", 0) == 0) continue;  // virtual refs
            if (!entById(id.substr(0, id.find('.')))) continue;
            ops.push_back({{"op", "sketch_toggle_construction"}, {"sketch", fid_}, {"id", id}});
        }
        if (ops.empty()) {
            st.toast(ui::ToastLevel::Info, "Construction: select sketch entities first");
            return true;
        }
        auto r = st.docMirror.applyOps(st.worker, ops);
        if (!r.ok) st.toast(ui::ToastLevel::Error, "Construction: " + r.error_message);
        else {
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            resolve(st);
        }
        return true;
    }
    if (cmdId == "sk_display_relations") {
        relationsPanel_.open = !relationsPanel_.open;
        return true;
    }
    if (cmdId == "sk_fully_define") { fullyDefine(st); return true; }
    if (cmdId == "sk_relations") { beginRelations(st); return true; }
    if (cmdId == "sk_fix") {
        // SolidWorks "Fix" relation as a one-click ribbon button: pin every selected entity/point
        // (the engine `fix` constraint zeroes that entity's DOF — sketch_solver.py). This is the
        // direct shortcut for the Fix that already lives in the Add-Relation popup; it routes
        // through addRelationOps so an over-defining Fix surfaces the same "Over Defined" prompt.
        if (selIds_.empty()) {
            st.toast(ui::ToastLevel::Info, "Fix: select sketch entities or points first");
            return true;
        }
        relPopupPos_ = ImGui::GetMousePos();   // anchor the over-def prompt at the cursor
        nlohmann::json ops = nlohmann::json::array();
        for (const auto& h : selIds_) {
            const std::string base = h.substr(0, h.find('.'));
            if (base == "origin" || base.rfind("axis.", 0) == 0) continue;
            if (!entById(base) && h.find('.') == std::string::npos) continue;
            ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_},
                {"constraint", {{"type", "fix"}, {"refs", nlohmann::json::array({h})}}}});
        }
        if (ops.empty()) {
            st.toast(ui::ToastLevel::Info, "Fix: select sketch entities or points first");
            return true;
        }
        addRelationOps(st, ops, "Fix");
        return true;
    }
    return false;
}

// C40: armed tool -> ribbon cmd id (inverse of onRibbonCommand). "" when no tool armed.
const char* SketchMode::activeToolCmd() const {
    if (!active_) return "";
    switch (tool_) {
        case SketchTool::Line:            return "sk_line";
        case SketchTool::RectCorner:      return "sk_rect_corner";
        case SketchTool::Circle:          return "sk_circle";
        case SketchTool::ArcCenter:       return "sk_arc_center";
        case SketchTool::Slot:            return "sk_slot";
        case SketchTool::SmartDim:        return "smart_dim";
        case SketchTool::Rect3ptCorner:   return "sk_rect_3pt_corner";
        case SketchTool::Rect3ptCenter:   return "sk_rect_3pt_center";
        case SketchTool::Parallelogram:   return "sk_parallelogram";
        case SketchTool::CirclePerimeter: return "sk_circle_perimeter";
        case SketchTool::SlotCtrStr:      return "sk_slot_ctr_str";
        case SketchTool::SlotArcCtr:      return "sk_slot_arc_ctr";
        case SketchTool::SlotArc3pt:      return "sk_slot_arc_3pt";
        case SketchTool::EllipsePartial:  return "sk_ellipse_partial";
        case SketchTool::Parabola:        return "sk_parabola";
        case SketchTool::Conic:           return "sk_conic";
        case SketchTool::Trim:            return "sk_trim";
        case SketchTool::Extend:          return "sk_extend";
        case SketchTool::Offset:          return "sk_offset_ent";
        case SketchTool::SkFillet:        return "sk_fillet";
        case SketchTool::SkChamfer:       return "sk_chamfer";
        case SketchTool::Mirror:          return "sk_mirror";
        case SketchTool::LinPattern:      return "sk_lin_pattern";
        case SketchTool::CircPattern:     return "sk_circ_pattern";
        case SketchTool::Convert:         return "sk_convert_ent";
        case SketchTool::Move:            return "sk_move";
        case SketchTool::MoveEntities:    return "sk_move_entities";
        case SketchTool::RotateEntities:  return "sk_rotate_entities";
        case SketchTool::ArcTangent:      return "sk_arc_tangent";
        case SketchTool::Arc3pt:          return "sk_arc_3pt";
        case SketchTool::Polygon:         return "sk_polygon";
        case SketchTool::RectCenter:      return "sk_rect_center";
        case SketchTool::Point:           return "sk_point";
        case SketchTool::Ellipse:         return "sk_ellipse_big";
        case SketchTool::Spline:          return "sk_spline";
        case SketchTool::StyleSpline:     return "sk_spline_style";
        case SketchTool::Text:            return "sk_text";
        case SketchTool::EqCurve:         return "sk_spline_equation";
        case SketchTool::Airfoil:         return "sk_airfoil";
        case SketchTool::Star:            return "sk_polygon";  // keeps parent button highlighted
        default:                          return "";
    }
}

// ---------------------------------------------------------------------------
// tool helpers
// ---------------------------------------------------------------------------

void SketchMode::clearEntitySelection() {
    selIds_.clear();
    selRelIds_.clear();
    hoverRelCid_.clear();
}

void SketchMode::selectEntities(const std::vector<std::string>& handles) {
    selIds_.clear();
    for (const auto& h : handles)
        if (h == "origin" || entById(skBaseId(h))) selIds_.push_back(h);
}

void SketchMode::resetTool() { tool_ = SketchTool::None; toolUV_.clear(); toolSnaps_.clear();
                               centerline_ = false;   // centerline flavor clears on any tool change
                               conicRho_ = 0.5f; chainPrevId_.clear();
                               selIds_.clear();   // C41 (T2): selection dies with any tool change/exit
                               boxArmed_ = boxSelecting_ = false;  // C51
                               groupDragArmed_ = groupDragging_ = false; groupDragTargets_.clear();
                               groupClickKind_ = 0; groupClickEnt_.clear(); groupClickRegion_ = -1;
                               hotRegion_ = entityLoopRegion_ = -1; dwellAccum_ = 0.0f; dwellEnt_.clear();
                               selRelIds_.clear(); hoverRelCid_.clear();
                               relPopupOpen_ = false; relIds_.clear();   // C42: relation popup too
                               relModelPicking_ = false; relModelRefs_.clear(); relModelKey_.clear();
                               offsetStage_ = 0; offsetChain_.clear(); offsetFocusInput_ = false;
                               corner1Id_.clear();   // C52: drop a held first corner pick (keep radius/distance)
                               mpStage_ = 0; mpSel_.clear();   // C53: drop mirror/pattern selection (keep dims)
                               // C37-D: clear the Smart Dimension state machine.
                               dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                               dimType_.clear(); dimAxis_.clear(); dimEditCid_.clear();
                               dimPopupOpen_ = false; dimError_.clear(); dimPopupDriven_ = false;
                               overDefOpen_ = false; overDefPendingCon_ = nullptr;   // C50f: add-path over-define dialog
                               editOverDefOpen_ = false; editOverDefOp_ = nullptr;   // C50f: edit-path over-define dialog
                               relOverDefOpen_ = false; relOverDefOps_ = nullptr; relOverDefLabel_.clear();   // C50g
                               textPopupOpen_ = false; textFocus_ = false;          // Sketch Text popup
                               eqPopupOpen_ = false; eqFocus_ = false;              // Eq-Driven Curve popup
                               afPopupOpen_ = false; afFocus_ = false;              // Airfoil calculator popup
                               pastePlacing_ = false; pasteBundle_ = nlohmann::json(); }  // Wave 4C paste-placement
void SketchMode::cancelTool() { resetTool(); }

// ---------------------------------------------------------------------------
// Sketch Text + Equation-Driven Curve parameter popups (engine entities text / eq_curve)
// ---------------------------------------------------------------------------

// Sketch Text: a small borderless popup (mirrors the Add-Relation popup) collecting the string +
// cap height for a "text" entity placed at the click-captured lower-left anchor (textAnchorUV_).
// OK emits {"type":"text","construction":false,"p":[u,v],"text":...,"height":...} (font omitted ->
// engine default, safer cross-machine). Empty text disables OK. Esc/Cancel closes; the tool stays
// armed for another placement.
void SketchMode::drawTextPopup(AppState& st, bool& escHandled) {
    if (!textPopupOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(textPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchText", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Sketch Text");
        ImGui::PopStyleColor();
        ImGui::Separator();
        ImGui::TextUnformatted("Text");
        ImGui::SetNextItemWidth(220);
        if (textFocus_) { ImGui::SetKeyboardFocusHere(); textFocus_ = false; }
        bool enter = ImGui::InputText("##textstr", textBuf_, sizeof(textBuf_),
                                      ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SetNextItemWidth(120);
        // Bounded value -> drag entry (Ctrl+click still types an exact number).
        ImGui::DragFloat("Height (mm)", &textHeight_, 0.25f, 0.001f, 1000.0f, "%.3f",
                         ImGuiSliderFlags_AlwaysClamp);
        ImGui::Separator();
        const bool empty = (textBuf_[0] == '\0');
        ImGui::BeginDisabled(empty);
        bool ok = ImGui::Button("OK") || (enter && !empty);
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would resetTool)
        if (empty && enter)
            st.toast(ui::ToastLevel::Info, "Text: enter a non-empty string");
        if (ok && !empty) {
            nlohmann::json ent = {{"type", "text"}, {"construction", false},
                {"p", nlohmann::json::array({textAnchorUV_.x, textAnchorUV_.y})},
                {"text", std::string(textBuf_)},
                {"height", (double)textHeight_}};
            addEntity(st, ent, nlohmann::json::array());
            textPopupOpen_ = false;
            resetTool(); tool_ = SketchTool::Text;   // stay armed for another placement (one-shot)
        } else if (cancel) {
            textPopupOpen_ = false;
            // keep the tool armed so the next click can place again
        }
    }
    ImGui::End();
}

// Equation-Driven Curve: a borderless popup collecting x(t)/y(t) expression strings + the t range
// for an "eq_curve" entity (origin-relative; no viewport pick). OK emits
// {"type":"eq_curve","construction":false,"xt":...,"yt":...,"t1":...,"t2":...}. Preset buttons fill
// the fields with a circle / sine wave. A closed curve (cos/sin over 0..2pi) extrudes as a region.
void SketchMode::drawEqCurvePopup(AppState& st, bool& escHandled) {
    if (!eqPopupOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(eqPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchEqCurve", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Equation Driven Curve");
        ImGui::PopStyleColor();
        ImGui::Separator();
        // Presets fill the expression fields (parameter t in radians).
        if (ImGui::SmallButton("Circle")) {
            std::snprintf(eqXt_, sizeof(eqXt_), "5*cos(t)");
            std::snprintf(eqYt_, sizeof(eqYt_), "5*sin(t)");
            eqT1_ = 0.0f; eqT2_ = 6.2831853f;
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Sine wave")) {
            std::snprintf(eqXt_, sizeof(eqXt_), "t");
            std::snprintf(eqYt_, sizeof(eqYt_), "5*sin(t)");
            eqT1_ = 0.0f; eqT2_ = 6.2831853f;
        }
        ImGui::Separator();
        ImGui::SetNextItemWidth(220);
        if (eqFocus_) { ImGui::SetKeyboardFocusHere(); eqFocus_ = false; }
        ImGui::InputText("x(t)", eqXt_, sizeof(eqXt_));
        ImGui::SetNextItemWidth(220);
        ImGui::InputText("y(t)", eqYt_, sizeof(eqYt_));
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("t start", &eqT1_, 0.0f, 0.0f, "%.6f");
        ImGui::SetNextItemWidth(120);
        ImGui::InputFloat("t end (rad)", &eqT2_, 0.0f, 0.0f, "%.6f");
        ImGui::Separator();
        const bool empty = (eqXt_[0] == '\0' || eqYt_[0] == '\0');
        const bool badRange = (eqT2_ == eqT1_);
        ImGui::BeginDisabled(empty || badRange);
        bool ok = ImGui::Button("OK");
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would also resetTool)
        if (ok && !empty && !badRange) {
            nlohmann::json ent = {{"type", "eq_curve"}, {"construction", false},
                {"xt", std::string(eqXt_)}, {"yt", std::string(eqYt_)},
                {"t1", (double)eqT1_}, {"t2", (double)eqT2_}};
            addEntity(st, ent, nlohmann::json::array());
            eqPopupOpen_ = false;
            resetTool();   // one entity per invocation, like Polygon/Ellipse
        } else if (cancel) {
            eqPopupOpen_ = false;
            resetTool();
        }
    }
    ImGui::End();
}

// Airfoil calculator: generate a NACA 4-/5-digit section (or load a .dat file), scaled to the
// chord and rotated by the angle of attack, with a live preview. OK emits the profile as chained
// splines via addAirfoil. Origin-relative (the leading edge sits at the sketch origin), so there
// is no viewport pick — the popup opens straight from the ribbon command, like Eq-Driven Curve.
void SketchMode::drawAirfoilPopup(AppState& st, bool& escHandled) {
    if (!afPopupOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(afPopupPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##SketchAirfoil", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::TextUnformatted("Airfoil Calculator");
        ImGui::PopStyleColor();
        ImGui::Separator();

        const char* kSeries[] = {"NACA 4-digit", "NACA 5-digit", "Load .dat file"};
        ImGui::SetNextItemWidth(220);
        ImGui::Combo("Series", &afSeries_, kSeries, IM_ARRAYSIZE(kSeries));

        if (afSeries_ == 2) {
            // .dat import: a Browse button + the chosen file name.
            if (ImGui::Button("Browse .dat\xE2\x80\xA6")) {
                std::string pick = ui::openFileDialog(
                    "Airfoil coordinates (*.dat)\0*.dat\0All files (*.*)\0*.*\0");
                if (!pick.empty()) afDatPath_ = pick;
            }
            ImGui::SameLine();
            if (afDatPath_.empty()) ImGui::TextDisabled("no file chosen");
            else {
                size_t slash = afDatPath_.find_last_of("/\\");
                ImGui::TextUnformatted(slash == std::string::npos ? afDatPath_.c_str()
                                                                  : afDatPath_.c_str() + slash + 1);
            }
        } else {
            // NACA code (presets help the user remember the layout).
            ImGui::SetNextItemWidth(120);
            if (afFocus_) { ImGui::SetKeyboardFocusHere(); afFocus_ = false; }
            ImGui::InputText(afSeries_ == 0 ? "Code (MPTT)" : "Code (LPQTT)",
                             afCode_, sizeof(afCode_), ImGuiInputTextFlags_CharsDecimal);
            if (afSeries_ == 0) {
                ImGui::SameLine();
                if (ImGui::SmallButton("2412")) std::snprintf(afCode_, sizeof(afCode_), "2412");
                ImGui::SameLine();
                if (ImGui::SmallButton("0012")) std::snprintf(afCode_, sizeof(afCode_), "0012");
            } else {
                ImGui::SameLine();
                if (ImGui::SmallButton("23012")) std::snprintf(afCode_, sizeof(afCode_), "23012");
            }
            ImGui::SetNextItemWidth(120);
            ImGui::SliderInt("Points/surface", &afPoints_, 20, 200);
            ImGui::Checkbox("Closed (sharp) trailing edge", &afClosedTE_);
        }

        ImGui::SetNextItemWidth(120);
        // Bounded values -> drag entry (Ctrl+click types an exact number).
        ImGui::DragFloat("Chord (mm)", &afChord_, 1.0f, 0.001f, 100000.0f, "%.2f",
                         ImGuiSliderFlags_AlwaysClamp);
        ImGui::SetNextItemWidth(120);
        ImGui::DragFloat("Angle of attack", &afAoA_, 0.25f, -45.0f, 45.0f, "%.2f\xC2\xB0",
                         ImGuiSliderFlags_AlwaysClamp);

        // Generate the profile from the current parameters (cheap; recomputed each frame so the
        // preview tracks edits live).
        airfoil::Profile prof;
        if (afSeries_ == 0)      prof = airfoil::naca4(afCode_, afChord_, afAoA_, afPoints_, afClosedTE_);
        else if (afSeries_ == 1) prof = airfoil::naca5(afCode_, afChord_, afAoA_, afPoints_, afClosedTE_);
        else if (afDatPath_.empty()) prof.error = "choose a .dat file";
        else                     prof = airfoil::fromDatFile(afDatPath_, afChord_, afAoA_);

        // ---- live preview ----
        // Mini canvas: light viewport tone + the sketcher's under-defined blue stroke, so the
        // preview matches what lands on the real (always light) canvas in both themes.
        ImGui::Separator();
        const float pw = 240.0f * ui::uiScale, ph = 90.0f * ui::uiScale;
        ImVec2 org = ImGui::GetCursorScreenPos();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRectFilled(org, ImVec2(org.x + pw, org.y + ph), col(pal.viewportTop), 4.0f);
        dl->AddRect(org, ImVec2(org.x + pw, org.y + ph), col(pal.sketchDimLine, 0.5f), 4.0f);
        if (prof.ok()) {
            // Fit the profile bbox into the preview rect (uniform scale, Y up).
            double xmn = 1e30, xmx = -1e30, ymn = 1e30, ymx = -1e30;
            auto bound = [&](const std::vector<std::array<double, 2>>& s) {
                for (const auto& q : s) { xmn = std::min(xmn, q[0]); xmx = std::max(xmx, q[0]);
                                          ymn = std::min(ymn, q[1]); ymx = std::max(ymx, q[1]); }
            };
            bound(prof.upper); bound(prof.lower);
            double sx = (xmx - xmn) > 1e-9 ? (pw - 16) / (xmx - xmn) : 1.0;
            double sy = (ymx - ymn) > 1e-9 ? (ph - 16) / (ymx - ymn) : 1.0;
            double s = std::min(sx, sy);
            double cx = 0.5 * (xmn + xmx), cy = 0.5 * (ymn + ymx);
            auto map = [&](const std::array<double, 2>& q) -> ImVec2 {
                return ImVec2(float(org.x + pw * 0.5 + (q[0] - cx) * s),
                              float(org.y + ph * 0.5 - (q[1] - cy) * s));   // flip Y
            };
            const ImU32 strokeCol = col(pal.sketchUnder);
            const float strokeW = 1.5f * ui::uiScale;
            auto stroke = [&](const std::vector<std::array<double, 2>>& sv) {
                for (size_t i = 1; i < sv.size(); ++i)
                    dl->AddLine(map(sv[i - 1]), map(sv[i]), strokeCol, strokeW);
            };
            stroke(prof.upper); stroke(prof.lower);
            // close the TE visually
            dl->AddLine(map(prof.upper.back()), map(prof.lower.back()), strokeCol, strokeW);
        } else {
            dl->AddText(ImVec2(org.x + 8, org.y + ph * 0.5f - 7), col(pal.statusErr),
                        prof.error.c_str());
        }
        ImGui::Dummy(ImVec2(pw, ph));
        if (prof.ok())
            ImGui::TextDisabled("%s  \xC2\xB7  %d pts  \xC2\xB7  %s TE", prof.name.c_str(),
                                int(prof.upper.size() + prof.lower.size()),
                                prof.sharpTE ? "sharp" : "open");

        ImGui::Separator();
        ImGui::BeginDisabled(!prof.ok());
        bool ok = ImGui::Button("OK");
        ImGui::EndDisabled();
        ImGui::SameLine();
        bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool cancel = ImGui::Button("Cancel") || escPressed;
        if (escPressed) escHandled = true;   // suppress the catch-all Esc (it would also resetTool)
        if (ok && prof.ok()) {
            addAirfoil(st, prof.upper, prof.lower, prof.sharpTE);
            st.toast(ui::ToastLevel::Info, prof.name + " airfoil added");
            afPopupOpen_ = false;
            resetTool();   // one profile per invocation
        } else if (cancel) {
            afPopupOpen_ = false;
            resetTool();
        }
    }
    ImGui::End();
}

// Emit a generated airfoil as real sketch geometry: the upper and lower surfaces become two
// through-point splines (LE->TE each), chained into one closed loop. A short trailing-edge line
// closes an open TE; a sharp TE shares the endpoint directly. Coincident relations tie the
// surfaces at the leading and trailing edges. All batches coalesce into ONE undo step, mirroring
// addDecomposedSlot. The C++ core profile builder now lifts the closed spline loop into a face,
// so the result extrudes/revolves/lofts like any other closed profile.
void SketchMode::addAirfoil(AppState& st, const std::vector<std::array<double, 2>>& upper,
                            const std::vector<std::array<double, 2>>& lower, bool sharpTE) {
    if (upper.size() < 2 || lower.size() < 2) return;
    bool first = true;
    auto add = [&](const nlohmann::json& entity) -> std::string {
        nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_}, {"entity", entity}};
        auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/!first);
        first = false;
        if (!r.ok) { st.addLog("[sketch] airfoil part failed: " + r.error_message); return std::string(); }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        return (r.id_map.is_object() && r.id_map.contains("$0"))
             ? r.id_map["$0"].get<std::string>() : std::string();
    };
    auto ptsJson = [](const std::vector<std::array<double, 2>>& s) {
        nlohmann::json a = nlohmann::json::array();
        for (const auto& q : s) a.push_back(nlohmann::json::array({q[0], q[1]}));
        return a;
    };
    std::string up = add({{"type", "spline"}, {"points", ptsJson(upper)}});
    std::string lo = add({{"type", "spline"}, {"points", ptsJson(lower)}});
    std::string te;   // optional trailing-edge closing line (open TE only)
    if (!sharpTE)
        te = add({{"type", "line"},
                  {"p1", {upper.back()[0], upper.back()[1]}},
                  {"p2", {lower.back()[0], lower.back()[1]}}});
    if (up.empty() || lo.empty() || (!sharpTE && te.empty())) { resolve(st); return; }

    auto con = [&](const nlohmann::json& c) {
        nlohmann::json op = {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", c}};
        st.docMirror.applyOps(st.worker, nlohmann::json::array({op}), /*coalesce=*/true);
    };
    const std::string uLE = up + ".p0";
    const std::string uTE = up + ".p" + std::to_string(upper.size() - 1);
    const std::string lLE = lo + ".p0";
    const std::string lTE = lo + ".p" + std::to_string(lower.size() - 1);
    con({{"type", "coincident"}, {"refs", {uLE, lLE}}});           // shared leading edge
    if (sharpTE) {
        con({{"type", "coincident"}, {"refs", {uTE, lTE}}});       // shared sharp trailing edge
    } else {
        con({{"type", "coincident"}, {"refs", {te + ".p1", uTE}}});
        con({{"type", "coincident"}, {"refs", {te + ".p2", lTE}}});
    }
    resolve(st);
}

// C52: apply one sketch_fillet / sketch_chamfer op for the held corner (e1=corner1Id_, e2=line2).
// The engine round-trips like every other sketch-editing op (applyOps -> loadDocMesh -> resolve).
// On an OpError (unsupported_entity / fillet_failed / chamfer_failed / invalid_op) the sketch is
// NOT mutated; we toast the engine message and reset the first pick so the user starts a fresh
// pair. On success the new arc/line appears and the sketch re-solves; we stay armed for the next
// corner (SolidWorks keeps filleting until Esc).
void SketchMode::applyCornerOp(AppState& st, const std::string& line2) {
    const bool fillet = (tool_ == SketchTool::SkFillet);
    nlohmann::json op = {{"op", fillet ? "sketch_fillet" : "sketch_chamfer"},
                         {"sketch", fid_}, {"e1", corner1Id_}, {"e2", line2}};
    if (fillet) op["radius"]   = skFilletRadius_;
    else        op["distance"] = skChamferDist_;
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        // Engine refused (bad corner / unsupported entity): surface the message, keep tool armed.
        std::string msg = r.error_message.empty()
            ? std::string(fillet ? "Sketch fillet failed" : "Sketch chamfer failed")
            : r.error_message;
        st.addLog(std::string("[sketch] ") + (fillet ? "fillet" : "chamfer") + " failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
        corner1Id_.clear();   // start a fresh pair (match Offset's reset-on-error UX)
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);              // refresh entities/statuses (same path as trim/extend/offset)
    corner1Id_.clear();       // ready for the next corner; stay in the tool
}

// C53: apply sketch_mirror_entities (entities=mpSel_, line=lineId) and refresh. On engine OpError
// the message is toasted and the selection is reset (SolidWorks lets you start a fresh mirror).
void SketchMode::applyMirrorOp(AppState& st, const std::string& lineId) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_mirror_entities"}, {"sketch", fid_},
                         {"entities", eids}, {"line", lineId}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Mirror failed") : r.error_message;
        st.addLog("[sketch] mirror failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another mirror
}

// C53: apply sketch_pattern_entities (linear or circular) for the current selection + dims.
void SketchMode::applyPatternOp(AppState& st, bool circular) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_pattern_entities"}, {"sketch", fid_},
                         {"mode", circular ? "circular" : "linear"},
                         {"entities", eids}, {"count", patternCount_}};
    if (circular) {
        op["center"] = nlohmann::json::array({patternCenterU_, patternCenterV_});
        op["angle"] = patternAngle_;
        op["equal_spacing"] = patternEqualSpacing_;
    } else {
        op["dir"] = nlohmann::json::array({patternDirU_, patternDirV_});
        op["spacing"] = patternSpacing_;
    }
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Pattern failed") : r.error_message;
        st.addLog("[sketch] pattern failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another pattern
}

// Project all references together so conversion scales with the selection, not recompiles.
void SketchMode::applyConvertRefs(AppState& st, const nlohmann::json& refs) {
    if (!refs.is_array() || refs.empty()) return;
    nlohmann::json op = {{"op", "sketch_convert"}, {"sketch", fid_},
                         {"refs", refs}, {"associative", true}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Convert failed") : r.error_message;
        st.addLog("[sketch] convert failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);   // the projected entity appears; stay armed for more converts
}

// C54: apply sketch_move (translate or copy) for the current selection + delta.
void SketchMode::applyMoveOp(AppState& st, float dx, float dy, bool copy) {
    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_move"}, {"sketch", fid_},
                         {"entities", eids}, {"dx", dx}, {"dy", dy}, {"copy", copy}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Move failed") : r.error_message;
        st.addLog("[sketch] move failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
    } else {
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        resolve(st);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another move
}

// (u,v) centroid of the bounding box of the current mpSel_ selection. False if empty.
bool SketchMode::computeSelCentroid(ImVec2& out) const {
    bool any = false;
    float lo0 = 0, lo1 = 0, hi0 = 0, hi1 = 0;
    auto acc = [&](float u, float v) {
        if (!any) { lo0 = hi0 = u; lo1 = hi1 = v; any = true; }
        else { lo0 = std::min(lo0, u); hi0 = std::max(hi0, u);
               lo1 = std::min(lo1, v); hi1 = std::max(hi1, v); }
    };
    for (const auto& id : mpSel_) {
        const SketchEntity* e = entById(id);
        if (!e) continue;
        if (e->type == "line") { acc(e->p1[0], e->p1[1]); acc(e->p2[0], e->p2[1]); }
        else { acc(e->center[0] - e->radius, e->center[1] - e->radius);
               acc(e->center[0] + e->radius, e->center[1] + e->radius); }
    }
    if (!any) return false;
    out = ImVec2(0.5f * (lo0 + hi0), 0.5f * (lo1 + hi1));
    return true;
}

// Apply sketch_rotate (in-place or copy) for the current selection about (cx,cy) by angleDeg.
// Snapshots horizontal/vertical relations wholly inside the selection before/after so the user
// is told what the engine swapped (90/270) or dropped (non-axis angles).
void SketchMode::applyRotateOp(AppState& st, float cx, float cy, float angleDeg, bool copy) {
    // Snapshot pre-op H/V relations wholly within the selection (id -> type).
    std::set<std::string> sel(mpSel_.begin(), mpSel_.end());
    auto wholly_in = [&](const SketchConstraint& c) {
        if (c.refs.empty()) return false;
        for (const auto& r : c.refs) {
            std::string eid = r; auto dot = eid.find('.');
            if (dot != std::string::npos) eid = eid.substr(0, dot);
            if (!sel.count(eid)) return false;
        }
        return true;
    };
    std::map<std::string, std::string> preHV;
    for (const auto& c : cons_)
        if ((c.type == "horizontal" || c.type == "vertical") && wholly_in(c)) preHV[c.id] = c.type;

    nlohmann::json eids = nlohmann::json::array();
    for (const auto& id : mpSel_) eids.push_back(id);
    nlohmann::json op = {{"op", "sketch_rotate"}, {"sketch", fid_}, {"entities", eids},
                         {"cx", cx}, {"cy", cy}, {"angle", angleDeg}, {"copy", copy}};
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? std::string("Rotate failed") : r.error_message;
        st.addLog("[sketch] rotate failed: " + msg);
        st.toast(ui::ToastLevel::Warn, msg);
        mpStage_ = 0; mpSel_.clear();
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);   // refreshes cons_ from the solved sketch

    // Diff: which pre-op H/V relations are gone (dropped) vs. flipped type (swapped)?
    int dropped = 0, swapped = 0;
    for (const auto& [id, ptype] : preHV) {
        const SketchConstraint* now = nullptr;
        for (const auto& c : cons_) if (c.id == id) { now = &c; break; }
        if (!now) ++dropped;
        else if (now->type != ptype) ++swapped;
    }
    if (dropped || swapped) {
        std::string m = "Rotated selection";
        if (swapped) m += " — swapped " + std::to_string(swapped) + " H/V relation" +
                          (swapped == 1 ? "" : "s");
        // Non-axis rotation retires the H/V locks; the engine re-imposes parallel/perpendicular
        // so the shape stays rigid (it may become free to spin). Report that, not a bare "dropped".
        if (dropped) m += (swapped ? "," : " —") + std::string(" converted ") +
                          std::to_string(dropped) + " H/V lock" + (dropped == 1 ? "" : "s") +
                          " to parallel/perpendicular (shape kept rigid)";
        st.toast(ui::ToastLevel::Info, m);
    }
    mpStage_ = 0; mpSel_.clear();   // stay in the tool, ready for another rotate
}

// Viewport HUD state accessor (read by viewport_panel to draw the offset/angle + check/X panel).
SketchMode::TransformHud SketchMode::transformHudState() const {
    TransformHud h;
    if (tool_ == SketchTool::MoveEntities && mpStage_ >= 1 && !mpSel_.empty()) {
        h.kind = 1; h.dx = moveDx_; h.dy = moveDy_;
    } else if (tool_ == SketchTool::RotateEntities && mpStage_ >= 1 && !mpSel_.empty()) {
        h.kind = 2; h.angle = rotCurAng_;
    }
    return h;
}

void SketchMode::commitTransform(AppState& /*st*/) { hudFinish_ = true; }
void SketchMode::cancelTransform() { hudCancel_ = true; }

// C54: resolve the tangent-arc source — the line/circle/arc nearest the cursor, the endpoint of it
// nearest the cursor (the arc start), and the unit tangent direction there (pointing into the arc).
// Prefers the last selected entity, then the entity under the cursor, then the last drawn entity.
bool SketchMode::tangentArcSource(const ImVec2& cursorScreen, TangentArcSrc& out) const {
    const SketchEntity* src = nullptr;
    // 1) a held selection wins (its last-picked entity is the source).
    for (auto it = selIds_.rbegin(); it != selIds_.rend() && !src; ++it) {
        const SketchEntity* e = entById(*it);
        if (e && (e->type == "line" || e->type == "arc" || e->type == "circle")) src = e;
    }
    // 2) else the entity under the cursor.
    if (!src) {
        int h = hitTestEntity(cursorScreen);
        if (h >= 0 && (ents_[h].type == "line" || ents_[h].type == "arc" ||
                       ents_[h].type == "circle"))
            src = &ents_[h];
    }
    // 3) else the most-recently-drawn supported entity (last in the list).
    if (!src) {
        for (auto it = ents_.rbegin(); it != ents_.rend() && !src; ++it)
            if (it->type == "line" || it->type == "arc" || it->type == "circle") src = &*it;
    }
    if (!src) return false;

    const float D2R = 3.14159265f / 180.0f;
    if (src->type == "line") {
        ImVec2 p1(src->p1[0], src->p1[1]), p2(src->p2[0], src->p2[1]);
        ImVec2 s1 = uvToScreen(p1.x, p1.y), s2 = uvToScreen(p2.x, p2.y);
        bool useP1 = std::hypot(s1.x - cursorScreen.x, s1.y - cursorScreen.y) <=
                     std::hypot(s2.x - cursorScreen.x, s2.y - cursorScreen.y);
        ImVec2 endpt = useP1 ? p1 : p2;
        ImVec2 other = useP1 ? p2 : p1;
        // Tangent continues the segment outward (away from the line body).
        float dx = endpt.x - other.x, dy = endpt.y - other.y;
        float L = std::hypot(dx, dy);
        if (L < 1e-6f) return false;
        out.id = src->id;
        out.handle = src->id + (useP1 ? ".p1" : ".p2");
        out.start = endpt;
        out.tdir = ImVec2(dx / L, dy / L);
        return true;
    }
    // arc / circle: tangent is perpendicular to the radius at the endpoint.
    // A full circle has no free endpoint — fall back to the point on it nearest the cursor.
    float a0 = src->start, a1 = src->end;
    ImVec2 c(src->center[0], src->center[1]);
    float rad = src->radius;
    if (src->type == "arc") {
        ImVec2 pa(c.x + rad * std::cos(a0 * D2R), c.y + rad * std::sin(a0 * D2R));
        ImVec2 pb(c.x + rad * std::cos(a1 * D2R), c.y + rad * std::sin(a1 * D2R));
        ImVec2 sa = uvToScreen(pa.x, pa.y), sb = uvToScreen(pb.x, pb.y);
        bool useStart = std::hypot(sa.x - cursorScreen.x, sa.y - cursorScreen.y) <=
                        std::hypot(sb.x - cursorScreen.x, sb.y - cursorScreen.y);
        float ang = (useStart ? a0 : a1) * D2R;
        ImVec2 endpt(c.x + rad * std::cos(ang), c.y + rad * std::sin(ang));
        // Radial unit, then rotate +90 for the tangent; orient it along the arc's sweep so the new
        // arc continues smoothly outward (ccw => +tangent at end / -tangent at start).
        float rx = std::cos(ang), ry = std::sin(ang);
        float tx = -ry, ty = rx;             // +90 rotation of the radial
        bool ccw = src->ccw;
        float sgn = (useStart ? -1.0f : 1.0f) * (ccw ? 1.0f : -1.0f);
        out.id = src->id;
        out.handle = src->id + (useStart ? ".start" : ".end");
        out.start = endpt;
        out.tdir = ImVec2(tx * sgn, ty * sgn);
        return true;
    }
    // circle: nearest rim point to the cursor; tangent perpendicular to the radius (ccw sense).
    float cu, cv; screenToUV(cursorScreen, cu, cv);
    float ang = std::atan2(cv - c.y, cu - c.x);
    ImVec2 endpt(c.x + rad * std::cos(ang), c.y + rad * std::sin(ang));
    out.id = src->id;
    out.handle = src->id;                    // whole-circle ref (no endpoint sub-handle)
    out.start = endpt;
    out.tdir = ImVec2(-std::sin(ang), std::cos(ang));   // ccw tangent
    return true;
}

// C54: solve the arc tangent to `tdir` at `start`, passing through `end`. The center lies on the
// line perpendicular to tdir at start (start + r*N). Requiring |C-end| == |C-start| == |r| gives
// r = |end-start|^2 / (2 * (end-start)·N), with N the left-normal of tdir. Returns false when the
// end is on the tangent line through start (denominator ~0 -> infinite radius) or coincident.
bool SketchMode::tangentArcThrough(const ImVec2& start, const ImVec2& tdir, const ImVec2& end,
                                   float& cu, float& cv, float& rad,
                                   float& a0deg, float& a1deg, bool& ccw) {
    float tx = tdir.x, ty = tdir.y;
    float tl = std::hypot(tx, ty);
    if (tl < 1e-9f) return false;
    tx /= tl; ty /= tl;
    float nx = -ty, ny = tx;                 // left-normal of the tangent
    float ex = end.x - start.x, ey = end.y - start.y;
    float d2 = ex * ex + ey * ey;
    if (d2 < 1e-12f) return false;           // start == end
    float denom = 2.0f * (ex * nx + ey * ny);
    if (std::fabs(denom) < 1e-9f) return false;   // end on the tangent line -> straight, no arc
    float r = d2 / denom;                    // signed radius (sign picks the side)
    cu = start.x + r * nx;
    cv = start.y + r * ny;
    rad = std::fabs(r);
    const float R2D = 57.29577951f;
    a0deg = std::atan2(start.y - cv, start.x - cu) * R2D;
    a1deg = std::atan2(end.y - cv, end.x - cu) * R2D;
    // The sweep direction is set by the tangent: cross(radial_at_start, tangent) sign. radial at
    // start is (start-center) = -r*N; tangent is T. ccw when cross(radial, T) > 0.
    float radx = start.x - cu, rady = start.y - cv;
    ccw = (radx * ty - rady * tx) > 0.0f;
    return true;
}

void SketchMode::snapPoint(float& u, float& v, std::string& snapHandle, SnapInfer* infer,
                           const char* excludeHandle,
                           const std::set<std::string>* excludeEnts) const {
    snapHandle.clear();
    const float snapPx = kSnapPx * ui::uiScale;   // snap reach tracks DPI
    const ImVec2 cur = uvToScreen(u, v);   // raw cursor (snap distances measure from here)
    // C56 drag-snap exclusions: excludeHandle drops one exact point handle (the dragged point
    // tracks the cursor and would self-snap); excludeEnts drops whole entities (group drags).
    auto excluded = [&](const std::string& handle) {
        if (excludeHandle && handle == excludeHandle) return true;
        if (excludeEnts) {
            std::string base = handle.substr(0, handle.find('.'));
            if (excludeEnts->count(base)) return true;
        }
        return false;
    };
    // Visit every snappable POINT (handle, u, v) — point snap + alignment share this set.
    auto forEachPoint = [&](const std::function<void(const std::string&, float, float)>& fn) {
        fn("origin", 0.0f, 0.0f);   // the sketch origin is a first-class snap/anchor target
        for (const auto& e : ents_) {
            if (excludeEnts && excludeEnts->count(e.id)) continue;
            if (e.type == "line" || e.type == "slot") {
                fn(e.id + ".p1", e.p1[0], e.p1[1]);
                fn(e.id + ".p2", e.p2[0], e.p2[1]);
            } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                       e.type == "ellipse") {
                fn(e.id + ".center", e.center[0], e.center[1]);
            } else if (e.type == "point") {
                fn(e.id + ".p", e.p1[0], e.p1[1]);
            } else if (e.type == "spline" || e.type == "style_spline") {  // C42: sub-handles p0..pN (sketch_solver convention; style_spline = control poles)
                for (size_t i = 0; i < e.pts.size(); ++i)
                    fn(e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y);
            }
        }
    };
    // 1) Endpoint/center/point coincident snap within snapPx (screen space) — highest priority.
    float best = snapPx * snapPx;
    forEachPoint([&](const std::string& handle, float eu, float ev) {
        if (excluded(handle)) return;
        ImVec2 s = uvToScreen(eu, ev);
        float dx = s.x - cur.x, dy = s.y - cur.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; snapHandle = handle; u = eu; v = ev; }
    });
    if (!inferenceOn_) return;   // master toggle: everything below is C49 inference
    // 2) Line-midpoint snap ("mid:<id>") — only when no point snap fired.
    if (snapHandle.empty()) {
        best = snapPx * snapPx;
        for (const auto& e : ents_) {
            if (e.type != "line") continue;
            if (excludeEnts && excludeEnts->count(e.id)) continue;
            float mu2 = (e.p1[0] + e.p2[0]) * 0.5f, mv2 = (e.p1[1] + e.p2[1]) * 0.5f;
            ImVec2 s = uvToScreen(mu2, mv2);
            float dx = s.x - cur.x, dy = s.y - cur.y;
            float d2 = dx * dx + dy * dy;
            if (d2 < best) { best = d2; snapHandle = "mid:" + e.id; u = mu2; v = mv2; }
        }
    }
    // 3) Circle/arc RIM snap ("rim:<id>"): within snapPx of the rim (full-circle metric, like
    //    hitTestEntity), pulled to the exact rim along the center->cursor ray. Tiny circles
    //    (rim within 2*snapPx of the center) are skipped — the center snap owns those.
    if (snapHandle.empty()) {
        best = snapPx;          // compare |dist - r| in px (1D)
        for (const auto& e : ents_) {
            if (e.type != "circle" && e.type != "arc") continue;
            if (excludeEnts && excludeEnts->count(e.id)) continue;
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rimRef = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
            if (rpx < 2.0f * snapPx) continue;
            float dm = std::hypot(cur.x - cc.x, cur.y - cc.y);
            float d = std::fabs(dm - rpx);
            if (d < best) {
                float a = std::atan2(v - e.center[1], u - e.center[0]);
                best = d; snapHandle = "rim:" + e.id;
                u = e.center[0] + e.radius * std::cos(a);
                v = e.center[1] + e.radius * std::sin(a);
            }
        }
    }
    // 3b) Rim ∩ inline snap: while on a circle/arc rim, pull the point along the rim to where it
    //     sits vertically/horizontally inline with the sketch origin, or with the curve's own
    //     center (the quadrant points). Reported through `infer` like an alignment guide, with
    //     snapHandle kept as "rim:<id>", so the placement records point_on + vertical/horizontal.
    //     The origin is tried first, so it owns the tie when the curve is centered on it.
    if (infer && snapHasPrefix(snapHandle, "rim:")) {
        const SketchEntity* ce = entById(snapHandle.substr(4));
        if (ce && ce->radius > 1e-5f) {
            const float cx = ce->center[0], cy = ce->center[1], r = ce->radius;
            float bestD = kRimInlinePx * ui::uiScale;
            float bu = u, bv = v;
            bool hitU = false, hitV = false;
            std::string hitHandle;
            ImVec2 hitSrc{0, 0};
            auto tryInline = [&](const std::string& handle, float pu, float pv) {
                if (excluded(handle)) return;
                for (int axis = 0; axis < 2; ++axis) {      // 0: same u (vertical), 1: same v
                    const float off = axis == 0 ? pu - cx : pv - cy;
                    if (std::fabs(off) > r) continue;       // that line misses the rim
                    const float h = std::sqrt(r * r - off * off);
                    for (float sgn : {-1.0f, 1.0f}) {
                        const float cu = axis == 0 ? pu : cx + sgn * h;
                        const float cv = axis == 0 ? cy + sgn * h : pv;
                        if (ce->type == "arc" &&
                            !arcSweepContains(*ce, std::atan2(cv - cy, cu - cx) * 57.2957795f))
                            continue;
                        const ImVec2 s = uvToScreen(cu, cv);
                        const ImVec2 ss = uvToScreen(pu, pv);
                        // The rim passes through the source itself: that is a point snap's job.
                        if (std::hypot(s.x - ss.x, s.y - ss.y) < 2.0f * snapPx) continue;
                        const float d = std::hypot(s.x - cur.x, s.y - cur.y);
                        if (d < bestD) {
                            bestD = d; bu = cu; bv = cv;
                            hitU = axis == 0; hitV = axis == 1;
                            hitHandle = handle; hitSrc = ImVec2(pu, pv);
                        }
                    }
                }
            };
            tryInline("origin", 0.0f, 0.0f);
            tryInline(ce->id + ".center", cx, cy);
            if (hitU || hitV) {
                u = bu; v = bv;
                if (hitU) { infer->alignU = true; infer->srcU = hitSrc; infer->srcUHandle = hitHandle; }
                if (hitV) { infer->alignV = true; infer->srcV = hitSrc; infer->srcVHandle = hitHandle; }
            }
        }
    }
    // 4) Alignment inference (positional only — SolidWorks-style dashed lines, no constraint):
    //    cursor u (or v) within snapPx of a source point's u (or v), the point itself clearly
    //    OUTSIDE the coincident radius (else the dashed line degenerates / fights the point snap).
    if (infer && snapHandle.empty()) {
        const float pxPerU = std::hypot(uAxisS_.x, uAxisS_.y);
        const float pxPerV = std::hypot(vAxisS_.x, vAxisS_.y);
        float bestU = snapPx, bestV = snapPx;   // screen-px distance along each axis
        forEachPoint([&](const std::string& handle, float eu, float ev) {
            if (excluded(handle)) return;
            ImVec2 s = uvToScreen(eu, ev);
            float d2 = (s.x - cur.x) * (s.x - cur.x) + (s.y - cur.y) * (s.y - cur.y);
            if (d2 < (2.0f * snapPx) * (2.0f * snapPx)) return;  // too close to the point itself
            float dU = std::fabs(eu - u) * pxPerU;
            float dV = std::fabs(ev - v) * pxPerV;
            if (dU < bestU) { bestU = dU; infer->alignU = true; infer->srcU = ImVec2(eu, ev);
                              infer->srcUHandle = handle; }
            if (dV < bestV) { bestV = dV; infer->alignV = true; infer->srcV = ImVec2(eu, ev);
                              infer->srcVHandle = handle; }
        });
        if (infer->alignU) u = infer->srcU.x;
        if (infer->alignV) v = infer->srcV.y;
    }
}

int SketchMode::hitTestHandle(const ImVec2& s, std::string& handleOut, float& uOut, float& vOut) const {
    float best = (kHandlePx * ui::uiScale) * (kHandlePx * ui::uiScale);
    int hit = -1;
    auto consider = [&](const std::string& handle, float eu, float ev) {
        ImVec2 p = uvToScreen(eu, ev);
        float dx = p.x - s.x, dy = p.y - s.y;
        float d2 = dx * dx + dy * dy;
        if (d2 < best) { best = d2; handleOut = handle; uOut = eu; vOut = ev; hit = 1; }
    };
    for (const auto& e : ents_) {
        if (e.type == "line" || e.type == "slot") {
            consider(e.id + ".p1", e.p1[0], e.p1[1]);
            consider(e.id + ".p2", e.p2[0], e.p2[1]);
        } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                   e.type == "ellipse") {
            consider(e.id + ".center", e.center[0], e.center[1]);
            // C-bug1: a draggable RIM handle (resize radius) for circles/arcs, plus arc endpoint
            // angle handles. Computed in screen space then mapped back to (u,v) so the rim handle
            // tracks the actual circle under the cursor regardless of zoom / anisotropic basis.
            if (e.type == "circle" || e.type == "arc") {
                ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
                ImVec2 rimRef = uvToScreen(e.center[0] + e.radius, e.center[1]);
                float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
                if (rpx > 3.0f) {   // skip sub-pixel circles
                    float dxS = s.x - cc.x, dyS = s.y - cc.y;
                    float dS = std::hypot(dxS, dyS);
                    if (dS > 1e-3f) {
                        ImVec2 rimS(cc.x + dxS / dS * rpx, cc.y + dyS / dS * rpx);
                        float rimU, rimV; screenToUV(rimS, rimU, rimV);
                        if (e.type == "circle") {
                            consider(e.id + ".rim", rimU, rimV);
                        } else {
                            // Arc: endpoint handles always; the rim handle only inside the sweep.
                            // Registered center-first, endpoints-before-rim so endpoints win a tie.
                            const float D2R = 3.14159265f / 180.0f;
                            consider(e.id + ".start_angle",
                                     e.center[0] + e.radius * std::cos(e.start * D2R),
                                     e.center[1] + e.radius * std::sin(e.start * D2R));
                            consider(e.id + ".end_angle",
                                     e.center[0] + e.radius * std::cos(e.end * D2R),
                                     e.center[1] + e.radius * std::sin(e.end * D2R));
                            float rimAng = std::atan2(rimV - e.center[1],
                                                      rimU - e.center[0])
                                           * 180.0f / 3.14159265f;
                            if (rimAng < 0) rimAng += 360.0f;
                            if (arcSweepContains(e, rimAng))
                                consider(e.id + ".rim", rimU, rimV);
                        }
                    }
                }
            }
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        } else if (e.type == "spline" || e.type == "style_spline") {   // C42: draggable through-points / control poles p0..pN
            for (size_t i = 0; i < e.pts.size(); ++i)
                consider(e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y);
        }
    }
    // The sketch origin (0,0) is a real, pickable point handle so it can be selected and given
    // relations (coincident / horizontal / vertical / point_on …) against other geometry. It is a
    // virtual entity — not in ents_ — and the engine resolves the bare ref "origin" to a fixed (0,0).
    // Considered LAST so a real vertex sitting exactly on the origin wins the tie (strict d2<best);
    // the origin is then picked only when no geometry handle is nearer. It is fixed, so it is
    // selectable but never promotes to a drag (see the drag-promotion guard).
    consider("origin", 0.0f, 0.0f);
    return hit;
}

// Nearest whole entity under the screen point. Distance is measured to the projected geometry:
// segment for lines/slots, |dist-to-center - radius_px| for circles/arcs, and the same rim metric
// (no angular clamp) for arcs — a deliberately simple approximation matching the Smart-Dimension
// picker. Returns the ents_ index or -1 when nothing is within maxPx.
int SketchMode::hitTestEntity(const ImVec2& s, float maxPx) const {
    maxPx *= ui::uiScale;   // pick reach tracks DPI (callers pass logical px)
    float best = maxPx * maxPx;
    int hit = -1;
    for (size_t i = 0; i < ents_.size(); ++i) {
        const SketchEntity& e = ents_[i];
        float d2 = 1e30f;
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            float vx = b.x - a.x, vy = b.y - a.y;
            float wx = s.x - a.x, wy = s.y - a.y;
            float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
            t = std::fmax(0.0f, std::fmin(1.0f, t));
            float px = a.x + t * vx, py = a.y + t * vy;
            d2 = (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
        } else if (e.type == "slot" || e.type == "arc_slot") {
            // Distance to the visible capsule OUTLINE (its body) AND the centerline spine, so the
            // slot selects whether you click its edge OR its centerline (the spine is real, pickable
            // geometry — SolidWorks lets you dimension it). Segment-distance over both.
            auto segD2 = [&](ImVec2 a, ImVec2 b) {
                float vx = b.x - a.x, vy = b.y - a.y;
                float t = std::fmax(0.0f, std::fmin(1.0f,
                    ((s.x-a.x)*vx + (s.y-a.y)*vy) / std::fmax(1e-6f, vx*vx + vy*vy)));
                float px = a.x + t * vx, py = a.y + t * vy;
                return (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
            };
            std::vector<ImVec2> uv; bool cl; entityBoundaryUV(e, uv, cl);
            for (size_t k = 1; k < uv.size(); ++k)
                d2 = std::fmin(d2, segD2(uvToScreen(uv[k-1].x, uv[k-1].y), uvToScreen(uv[k].x, uv[k].y)));
            if (e.type == "slot")   // straight centerline spine
                d2 = std::fmin(d2, segD2(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1])));
        } else if (e.type == "circle" || e.type == "arc") {
            ImVec2 cc = uvToScreen(e.center[0], e.center[1]);
            ImVec2 rim = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float rpx = std::hypot(rim.x - cc.x, rim.y - cc.y);
            float dm = std::hypot(s.x - cc.x, s.y - cc.y);
            d2 = (dm - rpx) * (dm - rpx);
        } else if (e.type == "point") {                       // C42: distance to the point itself
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            d2 = (p.x - s.x) * (p.x - s.x) + (p.y - s.y) * (p.y - s.y);
        } else if (e.type == "ellipse" || e.type == "spline" || e.type == "style_spline") {
            // C42: min distance to a sampled polyline (32-seg ellipse / spline through-points /
            // style_spline control polygon — the displayed editable polyline, SW-style).
            auto seg2 = [&](ImVec2 a, ImVec2 b) {
                float vx = b.x - a.x, vy = b.y - a.y;
                float wx = s.x - a.x, wy = s.y - a.y;
                float t = (vx * wx + vy * wy) / std::fmax(1e-6f, vx * vx + vy * vy);
                t = std::fmax(0.0f, std::fmin(1.0f, t));
                float px = a.x + t * vx, py = a.y + t * vy;
                return (px - s.x) * (px - s.x) + (py - s.y) * (py - s.y);
            };
            if (e.type == "ellipse") {
                const float D2R = 3.14159265f / 180.0f;
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                ImVec2 prev;
                for (int k = 0; k <= 32; ++k) {
                    float a = k / 32.0f * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    ImVec2 p = uvToScreen(e.center[0] + lx * ca - ly * sa,
                                          e.center[1] + lx * sa + ly * ca);
                    if (k) d2 = std::fmin(d2, seg2(prev, p));
                    prev = p;
                }
            } else {
                for (size_t k = 1; k < e.pts.size(); ++k)
                    d2 = std::fmin(d2, seg2(uvToScreen(e.pts[k - 1].x, e.pts[k - 1].y),
                                            uvToScreen(e.pts[k].x, e.pts[k].y)));
            }
        }
        if (d2 < best) { best = d2; hit = (int)i; }
    }
    return hit;
}

// ---------------------------------------------------------------------------
// C51: closed-profile regions + box-select geometry
// ---------------------------------------------------------------------------
namespace {
// Even-odd point-in-polygon test on a (u,v) polygon (implicitly closed last->first).
bool pointInPolyUV(const std::vector<ImVec2>& poly, float x, float y) {
    bool in = false;
    size_t n = poly.size();
    if (n < 3) return false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        bool cross = (poly[i].y > y) != (poly[j].y > y);
        if (cross && x < (poly[j].x - poly[i].x) * (y - poly[i].y) /
                             (poly[j].y - poly[i].y) + poly[i].x)
            in = !in;
    }
    return in;
}
// |signed area| of a (u,v) polygon (shoelace).
float polyAreaUV(const std::vector<ImVec2>& p) {
    float a = 0.0f;
    for (size_t i = 0, j = p.size() - 1; i < p.size(); j = i++)
        a += (p[j].x + p[i].x) * (p[j].y - p[i].y);
    return std::fabs(a) * 0.5f;
}

// ---- C56c: hole-aware polygon triangulation (screen space) ----
// ImGui's concave tessellator has no hole support, so the profile shading can't "punch" holes
// with a flat color (the sketch backdrop is the live 3D viewport — no flat color ever matches).
// mapbox earcut triangulates the outer loop + hole loops into true-hole triangles the canvas
// shows through. (Validated: 0/2000 fuzz failures on contained disjoint holes.)
bool skTriangulateWithHoles(const std::vector<ImVec2>& outer,
                            const std::vector<std::vector<ImVec2>>& holes,
                            std::vector<ImVec2>& tris) {
    if (outer.size() < 3) return false;
    std::vector<std::vector<std::array<float, 2>>> poly;
    poly.reserve(1 + holes.size());
    std::vector<std::array<float, 2>> ring;
    ring.reserve(outer.size());
    for (const auto& p : outer) ring.push_back({p.x, p.y});
    poly.push_back(std::move(ring));
    for (const auto& h : holes) {
        if (h.size() < 3) continue;
        ring.clear();
        ring.reserve(h.size());
        for (const auto& p : h) ring.push_back({p.x, p.y});
        poly.push_back(ring);
    }
    const std::vector<uint32_t> idx = mapbox::earcut<uint32_t>(poly);
    if (idx.empty()) return false;
    tris.reserve(tris.size() + idx.size());
    for (uint32_t i : idx) {
        size_t off = 0;
        const std::array<float, 2>* pt = nullptr;
        if (i < poly[0].size()) pt = &poly[0][i];
        else {
            off = poly[0].size();
            for (size_t r = 1; r < poly.size(); ++r) {
                if (i < off + poly[r].size()) { pt = &poly[r][i - off]; break; }
                off += poly[r].size();
            }
        }
        if (!pt) return false;
        tris.push_back(ImVec2((*pt)[0], (*pt)[1]));
    }
    return !tris.empty();
}

// Representative interior point: circle centre, else polygon centroid (shoelace).
ImVec2 loopCentroidUV(const std::vector<ImVec2>& poly, bool isCircle,
                      float cx0, float cy0) {
    if (isCircle) return ImVec2(cx0, cy0);
    if (poly.size() < 3) return ImVec2(0, 0);
    float a = 0.0f, cx = 0.0f, cy = 0.0f;
    for (size_t i = 0, j = poly.size() - 1; i < poly.size(); j = i++) {
        float cross = poly[j].x * poly[i].y - poly[i].x * poly[j].y;
        a += cross;
        cx += (poly[j].x + poly[i].x) * cross;
        cy += (poly[j].y + poly[i].y) * cross;
    }
    if (std::fabs(a) < 1e-12f) {
        float sx = 0, sy = 0;
        for (const auto& p : poly) { sx += p.x; sy += p.y; }
        return ImVec2(sx / (float)poly.size(), sy / (float)poly.size());
    }
    return ImVec2(cx / (3.0f * a), cy / (3.0f * a));
}
// Does outer strictly contain inner? Circle-in-circle uses radius containment
// (mirrors core/geom/profile.cpp loop_contains); mixed/poly cases use point-in-poly.
bool loopContainsUV(bool outerIsCircle, float oCx, float oCy, float oR,
                    const std::vector<ImVec2>& outerPoly,
                    bool innerIsCircle, float iCx, float iCy, float iR,
                    const std::vector<ImVec2>& innerPoly) {
    const float kEps = 1e-3f;
    if (outerIsCircle && oR > kEps) {
        if (innerIsCircle && iR > kEps) {
            float dx = iCx - oCx, dy = iCy - oCy;
            float d = std::sqrt(dx * dx + dy * dy);
            return d + iR < oR - kEps;
        }
        ImVec2 c = loopCentroidUV(innerPoly, innerIsCircle, iCx, iCy);
        float dx = c.x - oCx, dy = c.y - oCy;
        return (dx * dx + dy * dy) < oR * oR;
    }
    ImVec2 c = loopCentroidUV(innerPoly, innerIsCircle, iCx, iCy);
    return pointInPolyUV(outerPoly, c.x, c.y);
}
bool ptInRect(const ImVec2& p, const ImVec2& lo, const ImVec2& hi) {
    return p.x >= lo.x && p.x <= hi.x && p.y >= lo.y && p.y <= hi.y;
}
// Do segments p1p2 and p3p4 (screen space) properly cross?
bool segSegCross(const ImVec2& p1, const ImVec2& p2, const ImVec2& p3, const ImVec2& p4) {
    auto cr = [](const ImVec2& a, const ImVec2& b, const ImVec2& c) {
        return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
    };
    float d1 = cr(p3, p4, p1), d2 = cr(p3, p4, p2);
    float d3 = cr(p1, p2, p3), d4 = cr(p1, p2, p4);
    return ((d1 > 0) != (d2 > 0)) && ((d3 > 0) != (d4 > 0));
}
// Does segment a-b cross any edge of the axis-aligned rect [lo,hi]?
bool segCrossesRect(const ImVec2& a, const ImVec2& b, const ImVec2& lo, const ImVec2& hi) {
    ImVec2 c00(lo.x, lo.y), c10(hi.x, lo.y), c11(hi.x, hi.y), c01(lo.x, hi.y);
    return segSegCross(a, b, c00, c10) || segSegCross(a, b, c10, c11) ||
           segSegCross(a, b, c11, c01) || segSegCross(a, b, c01, c00);
}
}  // namespace

// Sample one entity's boundary into sketch-(u,v) points (no duplicated closing vertex).
void SketchMode::entityBoundaryUV(const SketchEntity& e, std::vector<ImVec2>& out,
                                  bool& closed) const {
    out.clear();
    closed = false;
    const float D2R = 3.14159265f / 180.0f;
    if (e.type == "line") {
        out.push_back(ImVec2(e.p1[0], e.p1[1]));
        out.push_back(ImVec2(e.p2[0], e.p2[1]));
    } else if (e.type == "point") {
        out.push_back(ImVec2(e.p1[0], e.p1[1]));
    } else if (e.type == "arc") {
        float a0 = e.start, a1 = e.end;
        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
        if (a1 < a0) a1 += 360.0f;
        int seg = 32;
        for (int i = 0; i <= seg; ++i) {
            float a = (a0 + (a1 - a0) * (i / (float)seg)) * D2R;
            out.push_back(ImVec2(e.center[0] + e.radius * std::cos(a),
                                 e.center[1] + e.radius * std::sin(a)));
        }
    } else if (e.type == "circle") {
        int seg = 48; closed = true;
        for (int i = 0; i < seg; ++i) {
            float a = i / (float)seg * 6.2831853f;
            out.push_back(ImVec2(e.center[0] + e.radius * std::cos(a),
                                 e.center[1] + e.radius * std::sin(a)));
        }
    } else if (e.type == "ellipse") {
        int seg = 48; closed = true;
        float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
        for (int i = 0; i < seg; ++i) {
            float a = i / (float)seg * 6.2831853f;
            float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
            out.push_back(ImVec2(e.center[0] + lx * ca - ly * sa,
                                 e.center[1] + lx * sa + ly * ca));
        }
    } else if (e.type == "slot") {
        // Capsule perimeter walked in order: side line (+normal) p1->p2, round cap bulging past p2,
        // side line (-normal) p2->p1, round cap bulging past p1. The cap interiors (i=1..11) skip
        // the side-line endpoints so no vertex is duplicated. axis = spine direction p1->p2; the
        // +normal endpoint sits at angle axis+90, the -normal endpoint at axis-90.
        closed = true;
        const float HALF_PI = 1.5707963f, PI = 3.14159265f;
        float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
        float L = std::hypot(dx, dy), hw = e.width * 0.5f;
        float axis = std::atan2(dy, dx);
        float nx = -std::sin(axis), ny = std::cos(axis);   // unit +normal
        out.push_back(ImVec2(e.p1[0] + nx * hw, e.p1[1] + ny * hw));
        out.push_back(ImVec2(e.p2[0] + nx * hw, e.p2[1] + ny * hw));
        for (int i = 1; i < 12; ++i) {      // cap at p2: axis+90 -> axis-90 (bulge toward +axis)
            float t = axis + HALF_PI - PI * (i / 12.0f);
            out.push_back(ImVec2(e.p2[0] + hw * std::cos(t), e.p2[1] + hw * std::sin(t)));
        }
        out.push_back(ImVec2(e.p2[0] - nx * hw, e.p2[1] - ny * hw));
        out.push_back(ImVec2(e.p1[0] - nx * hw, e.p1[1] - ny * hw));
        for (int i = 1; i < 12; ++i) {      // cap at p1: axis-90 -> axis-270 (bulge toward -axis)
            float t = axis - HALF_PI - PI * (i / 12.0f);
            out.push_back(ImVec2(e.p1[0] + hw * std::cos(t), e.p1[1] + hw * std::sin(t)));
        }
        (void)L;
    } else if (e.type == "arc_slot") {
        closed = true;
        float hw = e.width * 0.5f;
        float a0 = e.start, a1 = e.end; if (a1 < a0) a1 += 360.0f;
        for (int i = 0; i <= 32; ++i) {     // outer arc
            float a = (a0 + (a1 - a0) * (i / 32.0f)) * D2R;
            out.push_back(ImVec2(e.center[0] + (e.radius + hw) * std::cos(a),
                                 e.center[1] + (e.radius + hw) * std::sin(a)));
        }
        for (int i = 32; i >= 0; --i) {     // inner arc, back
            float a = (a0 + (a1 - a0) * (i / 32.0f)) * D2R;
            out.push_back(ImVec2(e.center[0] + (e.radius - hw) * std::cos(a),
                                 e.center[1] + (e.radius - hw) * std::sin(a)));
        }
    } else if (e.type == "spline" || e.type == "style_spline") {
        for (const auto& q : e.pts) out.push_back(q);
    }
}

// Detect closed loops among the live non-construction entities. Mirrors the engine connectivity
// walk (features/sketch.py _detect_loops): self-closed shapes are one-entity regions; line/arc
// edges are chained by shared endpoints until they return to the loop head. Greedy and single-use
// per edge (an edge consumed by a failed open chain won't re-seed another), matching the engine.
void SketchMode::rebuildRegions() {
    regions_.clear();
    const float EPS = 1e-3f;   // uv (mm) endpoint coincidence tolerance
    auto near2 = [&](const ImVec2& a, const ImVec2& b) {
        return std::fabs(a.x - b.x) < EPS && std::fabs(a.y - b.y) < EPS;
    };
    auto finish = [&](SketchRegion&& r) {
        // Strip consecutive near-duplicate vertices and a trailing vertex equal to the head. The
        // connectivity walk re-appends the loop head when it closes (line A..D then D->A leaves
        // [A,B,C,D,A]); that repeated corner makes the ear-clip fill collapse, so drop it here.
        std::vector<ImVec2> clean;
        for (const auto& p : r.poly) {
            if (!clean.empty() && std::fabs(clean.back().x - p.x) < EPS &&
                std::fabs(clean.back().y - p.y) < EPS) continue;
            clean.push_back(p);
        }
        while (clean.size() >= 2 &&
               std::fabs(clean.front().x - clean.back().x) < EPS &&
               std::fabs(clean.front().y - clean.back().y) < EPS)
            clean.pop_back();
        r.poly.swap(clean);
        if (r.poly.size() < 3) return;
        // Circles use exact pi r^2 (matches profile.cpp loop_area) so concentric nesting
        // compares radii rather than tessellated shoelace noise.
        if (r.isCircle && r.circleRadius > 1e-6f)
            r.area = 3.14159265f * r.circleRadius * r.circleRadius;
        else
            r.area = polyAreaUV(r.poly);
        if (r.area < 1e-4f) return;   // degenerate
        regions_.push_back(std::move(r));
    };

    // 1) self-closed entities -> single-entity regions.
    for (const auto& e : ents_) {
        if (e.construction) continue;
        if (e.type == "circle" || e.type == "ellipse" || e.type == "slot" || e.type == "arc_slot") {
            SketchRegion r; r.ids.push_back(e.id);
            if (e.type == "circle" && e.radius > 1e-6f) {
                r.isCircle = true;
                r.circleCenter[0] = e.center[0];
                r.circleCenter[1] = e.center[1];
                r.circleRadius = e.radius;
            }
            bool cl; entityBoundaryUV(e, r.poly, cl);
            finish(std::move(r));
        }
    }

    // 2) chainable line/arc edges -> connectivity walk.
    struct Edge { int idx; std::vector<ImVec2> pts; };   // pts: boundary, pts.front()/back() = ends
    std::vector<Edge> edges;
    for (size_t i = 0; i < ents_.size(); ++i) {
        const auto& e = ents_[i];
        if (e.construction) continue;
        if (e.type != "line" && e.type != "arc") continue;
        Edge ed; ed.idx = (int)i; bool cl;
        entityBoundaryUV(e, ed.pts, cl);
        if (ed.pts.size() >= 2) edges.push_back(std::move(ed));
    }
    std::vector<char> used(edges.size(), 0);
    for (size_t s = 0; s < edges.size(); ++s) {
        if (used[s]) continue;
        used[s] = 1;
        SketchRegion r;
        r.ids.push_back(ents_[edges[s].idx].id);
        r.poly = edges[s].pts;                       // a..b in natural order
        ImVec2 head = edges[s].pts.front();
        ImVec2 tip = edges[s].pts.back();
        bool closedLoop = false;
        while (true) {
            int found = -1; bool rev = false;
            for (size_t j = 0; j < edges.size(); ++j) {
                if (used[j]) continue;
                if (near2(edges[j].pts.front(), tip)) { found = (int)j; rev = false; break; }
                if (near2(edges[j].pts.back(), tip))  { found = (int)j; rev = true;  break; }
            }
            if (found < 0) break;
            used[found] = 1;
            r.ids.push_back(ents_[edges[found].idx].id);
            const auto& fp = edges[found].pts;
            // append the new edge's points (skip the shared junction vertex), oriented to flow
            // away from the current tip.
            if (rev) { for (size_t k = fp.size(); k-- > 0; ) if (k != fp.size() - 1) r.poly.push_back(fp[k]); tip = fp.front(); }
            else     { for (size_t k = 1; k < fp.size(); ++k) r.poly.push_back(fp[k]); tip = fp.back(); }
            if (near2(tip, head)) { closedLoop = true; break; }
        }
        if (closedLoop) finish(std::move(r));
    }

    // 3) Even-odd nesting (mirrors core/geom/profile.cpp nest_loops): parent = smallest-area
    // enclosing loop; depth = # of enclosers; isMaterial = (depth % 2 == 0). Direct children of
    // a material region are holes to punch when shading.
    const size_t n = regions_.size();
    for (size_t i = 0; i < n; ++i) {
        float bestArea = 0.0f;
        int best = -1;
        int depth = 0;
        for (size_t j = 0; j < n; ++j) {
            if (i == j || regions_[j].area <= regions_[i].area) continue;
            const auto& O = regions_[j];
            const auto& I = regions_[i];
            if (loopContainsUV(O.isCircle, O.circleCenter[0], O.circleCenter[1], O.circleRadius,
                               O.poly,
                               I.isCircle, I.circleCenter[0], I.circleCenter[1], I.circleRadius,
                               I.poly)) {
                depth++;
                if (best < 0 || O.area < bestArea) {
                    bestArea = O.area;
                    best = (int)j;
                }
            }
        }
        regions_[i].parent = best;
        regions_[i].depth = depth;
        regions_[i].isMaterial = (depth % 2 == 0);
        regions_[i].holeChildren.clear();
    }
    for (size_t i = 0; i < n; ++i) {
        int p = regions_[i].parent;
        if (p >= 0 && regions_[p].isMaterial && !regions_[i].isMaterial)
            regions_[p].holeChildren.push_back((int)i);
    }
}

// Material-region hit under the screen point. Among material loops containing the point, the
// smallest (innermost wall) wins. A point inside a void (odd-depth hole) is NOT a solid-profile
// hit — return -1 so loop-select does not treat the hole interior as filled material.
int SketchMode::hitTestRegion(const ImVec2& s) const {
    float u, v; screenToUV(s, u, v);
    int hit = -1; float best = 1e30f;
    bool inVoid = false;
    for (size_t i = 0; i < regions_.size(); ++i) {
        const auto& rg = regions_[i];
        bool inside = false;
        if (rg.isCircle && rg.circleRadius > 1e-6f) {
            float dx = u - rg.circleCenter[0], dy = v - rg.circleCenter[1];
            inside = (dx * dx + dy * dy) < rg.circleRadius * rg.circleRadius;
        } else {
            inside = pointInPolyUV(rg.poly, u, v);
        }
        if (!inside) continue;
        if (!rg.isMaterial) {
            // Innermost void wins over any larger material that also contains the point.
            if (rg.area < best) { best = rg.area; hit = -1; inVoid = true; }
            continue;
        }
        // Smaller material wins (island inside a void, or wall of a washer).
        if (rg.area < best) {
            best = rg.area;
            hit = (int)i;
            inVoid = false;
        }
    }
    return inVoid ? -1 : hit;
}

// The MULTI-entity loop (rectangle/polygon/custom chain) that entId is part of, or -1. Single-entity
// regions (a lone circle/slot) don't count — there's no "loop" to preview beyond the entity itself.
int SketchMode::regionContaining(const std::string& entId) const {
    for (size_t i = 0; i < regions_.size(); ++i) {
        if (regions_[i].ids.size() < 2) continue;
        if (std::find(regions_[i].ids.begin(), regions_[i].ids.end(), entId) != regions_[i].ids.end())
            return (int)i;
    }
    return -1;
}

// Stroke an entity's outline on top of the geometry (hover pre-highlight / loop preview).
void SketchMode::strokeEntityOutline(ImDrawList* dl, const SketchEntity& e, ImU32 c, float th) const {
    std::vector<ImVec2> uv; bool closed; entityBoundaryUV(e, uv, closed);
    if (uv.empty()) return;
    if (uv.size() == 1) { dl->AddCircle(uvToScreen(uv[0].x, uv[0].y), 5.0f * ui::uiScale, c, 0, th); return; }
    std::vector<ImVec2> sp; sp.reserve(uv.size());
    for (const auto& p : uv) sp.push_back(uvToScreen(p.x, p.y));
    for (size_t i = 1; i < sp.size(); ++i) dl->AddLine(sp[i - 1], sp[i], c, th);
    if (closed) dl->AddLine(sp.back(), sp.front(), c, th);
}

bool SketchMode::isRelSelected(const std::string& cid) const {
    return std::find(selRelIds_.begin(), selRelIds_.end(), cid) != selRelIds_.end();
}

// Topmost relation badge (last drawn = on top) whose screen rect contains s, or -1.
int SketchMode::hitTestRelationBadge(const ImVec2& s) const {
    for (int i = (int)relHits_.size() - 1; i >= 0; --i) {
        const RelHit& r = relHits_[i];
        if (s.x >= r.lo.x && s.x <= r.hi.x && s.y >= r.lo.y && s.y <= r.hi.y) return i;
    }
    return -1;
}

// Select every entity the screen rect (a,b) encloses (crossing=false: fully inside) or touches
// (crossing=true: any vertex inside or any boundary segment crossing the rect). additive keeps
// the current selection; otherwise it is replaced.
void SketchMode::boxSelect(const ImVec2& a, const ImVec2& b, bool crossing, bool additive) {
    ImVec2 lo(std::fmin(a.x, b.x), std::fmin(a.y, b.y));
    ImVec2 hi(std::fmax(a.x, b.x), std::fmax(a.y, b.y));
    if (!additive) { selIds_.clear(); selRelIds_.clear(); }
    std::vector<ImVec2> uv; bool closed;
    for (const auto& e : ents_) {
        entityBoundaryUV(e, uv, closed);
        if (uv.empty()) continue;
        std::vector<ImVec2> sp; sp.reserve(uv.size());
        for (const auto& p : uv) sp.push_back(uvToScreen(p.x, p.y));
        bool hit = false;
        if (crossing) {
            for (const auto& p : sp) if (ptInRect(p, lo, hi)) { hit = true; break; }
            if (!hit) {
                size_t n = sp.size();
                for (size_t i = 1; i < n && !hit; ++i)
                    if (segCrossesRect(sp[i - 1], sp[i], lo, hi)) hit = true;
                if (!hit && closed && n >= 2 && segCrossesRect(sp[n - 1], sp[0], lo, hi)) hit = true;
            }
        } else {
            hit = true;
            for (const auto& p : sp) if (!ptInRect(p, lo, hi)) { hit = false; break; }
        }
        if (hit && !isSelected(e.id)) selIds_.push_back(e.id);
    }
}

// The point handles that rigidly translate an entity, with their current (u,v). Lines/slots move
// by both endpoints; centered shapes (circle/arc/arc_slot/ellipse) by their center; splines by
// every through-point; a lone point by itself. Targeting all of them at orig+delta translates the
// entity without reshaping it (the engine keeps constraints exact).
void SketchMode::entityDragHandles(const SketchEntity& e,
                                   std::vector<std::pair<std::string, ImVec2>>& out) const {
    if (e.type == "line" || e.type == "slot") {
        out.emplace_back(e.id + ".p1", ImVec2(e.p1[0], e.p1[1]));
        out.emplace_back(e.id + ".p2", ImVec2(e.p2[0], e.p2[1]));
    } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" || e.type == "ellipse") {
        out.emplace_back(e.id + ".center", ImVec2(e.center[0], e.center[1]));
    } else if (e.type == "point") {
        out.emplace_back(e.id + ".p", ImVec2(e.p1[0], e.p1[1]));
    } else if (e.type == "spline" || e.type == "style_spline") {
        for (size_t i = 0; i < e.pts.size(); ++i)
            out.emplace_back(e.id + ".p" + std::to_string(i), e.pts[i]);
    }
}

// Capture the translation handles (+ origin coords) of every entity in `ids` for a group drag.
void SketchMode::beginGroupDrag(const std::vector<std::string>& ids, float startU, float startV) {
    groupDragTargets_.clear();
    for (const auto& id : ids) {
        const SketchEntity* e = entById(id);
        if (e) entityDragHandles(*e, groupDragTargets_);
    }
    groupDragStartUV_ = ImVec2(startU, startV);
    groupDragging_ = true;
}

// T-PERF2: latest-wins async sketch_drag — never block the GL thread on a solve.
void SketchMode::queueSketchDrag(AppState& st, nlohmann::json extra) {
    dragPendingExtra_ = std::move(extra);
    dragPending_ = true;
    kickSketchDragAsync(st);
}

void SketchMode::kickSketchDragAsync(AppState& st) {
    if (dragInFlight_ || !dragPending_) return;
    if (!st.docMirror.hasDoc() || !st.workerStarted || fid_.empty()) return;
    dragPending_ = false;
    dragInFlight_ = true;
    const int gen = ++dragGen_;
    nlohmann::json params = dragPendingExtra_;
    params["doc_id"] = st.docMirror.docId();
    params["sketch"] = fid_;
    st.rpc.call("sketch_drag", std::move(params),
                [this, &st, gen](const core::RpcResult& r) {
                    // Always clear in-flight so a superseded drag can re-kick if still pending.
                    dragInFlight_ = false;
                    if (gen != dragGen_) {
                        // Bumped by cancel/exit — drop; re-kick only if still pending for a live drag.
                        if (dragPending_ && (dragging_ || groupDragging_)) kickSketchDragAsync(st);
                        return;
                    }
                    if (r.ok && r.result.is_object() && !r.result.contains("code"))
                        applyEnterResult(r.result);
                    else if (!r.ok)
                        st.addLog(std::string("[sketch] sketch_drag transport error: ") + r.err);
                    // Next sample (or the one that arrived while this was in flight).
                    if (dragPending_ && (dragging_ || groupDragging_)) kickSketchDragAsync(st);
                }, /*timeoutMs=*/30000);
}

// Push one transient frame of the group translation: every captured handle soft-targeted at its
// original (u,v) plus the cursor delta. Throttled to once per frame by the caller (dragFrame_);
// the RPC itself is latest-wins async (T-PERF2).
void SketchMode::updateGroupDrag(AppState& st, float curU, float curV) {
    if (groupDragTargets_.empty()) return;
    float dx = curU - groupDragStartUV_.x, dy = curV - groupDragStartUV_.y;
    nlohmann::json targets = nlohmann::json::array();
    for (const auto& t : groupDragTargets_)
        targets.push_back({{"handle", t.first},
                           {"xy", nlohmann::json::array({t.second.x + dx, t.second.y + dy})}});
    queueSketchDrag(st, {{"targets", targets}});
}

// C56: snap a live group translate. (gu,gv) is the raw cursor uv; on return it's adjusted so the
// fired snap lands exactly. Priority: keypoint coincident > u/v alignment > collinear. Preview
// state (dragSnap* members) is filled for the draw pass + applyDragSnapRelations on drop.
void SketchMode::groupDragSnap(float& gu, float& gv) {
    dragSnapLive_ = false; dragSnapHandle_.clear(); groupSnapSrc_.clear();
    dragRelType_.clear(); dragRelOther_.clear(); dragRelEnt_.clear();
    dragAlignSrcU_.clear(); dragAlignSrcV_.clear();
    dragSnapInfer_ = SnapInfer{};
    if (groupDragTargets_.empty()) return;
    const float snapPx = kSnapPx * ui::uiScale;
    const float dx0 = gu - groupDragStartUV_.x, dy0 = gv - groupDragStartUV_.y;
    std::set<std::string> dragged;
    for (const auto& t : groupDragTargets_)
        dragged.insert(t.first.substr(0, t.first.find('.')));
    auto origOf = [&](const std::string& h) -> ImVec2 {
        for (const auto& t : groupDragTargets_) if (t.first == h) return t.second;
        return ImVec2(0, 0);
    };
    // Snap targets: origin + keypoints (+ line midpoints) of everything NOT being dragged.
    struct Tgt { std::string handle; float u, v; };
    std::vector<Tgt> tgts = {{"origin", 0.0f, 0.0f}};
    for (const auto& e : ents_) {
        if (dragged.count(e.id)) continue;
        if (e.type == "line" || e.type == "slot") {
            tgts.push_back({e.id + ".p1", e.p1[0], e.p1[1]});
            tgts.push_back({e.id + ".p2", e.p2[0], e.p2[1]});
            if (e.type == "line" && inferenceOn_)
                tgts.push_back({"mid:" + e.id, (e.p1[0] + e.p2[0]) * 0.5f,
                                (e.p1[1] + e.p2[1]) * 0.5f});
        } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot" ||
                   e.type == "ellipse") {
            tgts.push_back({e.id + ".center", e.center[0], e.center[1]});
        } else if (e.type == "point") {
            tgts.push_back({e.id + ".p", e.p1[0], e.p1[1]});
        } else if (e.type == "spline" || e.type == "style_spline") {
            for (size_t i = 0; i < e.pts.size(); ++i)
                tgts.push_back({e.id + ".p" + std::to_string(i), e.pts[i].x, e.pts[i].y});
        }
    }
    // 1) Keypoint coincident snap: closest (dragged keypoint, target) pair in screen space.
    {
        float best = snapPx * snapPx;
        const Tgt* bt = nullptr;
        std::string bsrc;
        for (const auto& t : groupDragTargets_) {
            ImVec2 a = uvToScreen(t.second.x + dx0, t.second.y + dy0);
            for (const auto& g : tgts) {
                ImVec2 b = uvToScreen(g.u, g.v);
                float ddx = a.x - b.x, ddy = a.y - b.y;
                float d2 = ddx * ddx + ddy * ddy;
                if (d2 < best) { best = d2; bt = &g; bsrc = t.first; }
            }
        }
        if (bt) {
            ImVec2 o = origOf(bsrc);
            gu += bt->u - (o.x + dx0);
            gv += bt->v - (o.y + dy0);
            dragSnapHandle_ = bt->handle; groupSnapSrc_ = bsrc;
            dragSnapUV_ = ImVec2(bt->u, bt->v); dragSnapLive_ = true;
            return;
        }
    }
    if (!inferenceOn_) return;
    const float pxPerU = std::hypot(uAxisS_.x, uAxisS_.y);
    const float pxPerV = std::hypot(vAxisS_.x, vAxisS_.y);
    // 2b) Tangent snap: a dragged circle/arc translates until it touches a static line or
    //     another circle/arc (external or internal contact), and a dragged line translates
    //     until it touches a static circle/arc. The user's "circle in a square dragged to an
    //     edge" case (and its dual) — commits a tangent relation on drop.
    for (const auto& e : ents_) {
        if (!dragged.count(e.id)) continue;
        if (e.type == "line") {
            // Dragged LINE -> static circle/arc: |distance(circle center, line)| == R with the
            // foot on the dragged segment.
            float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
            float L = std::hypot(dx, dy);
            if (L < 1e-5f) continue;
            float nx = -dy / L, ny = dx / L;
            const float lp1u = e.p1[0] + dx0, lp1v = e.p1[1] + dy0;
            for (const auto& oe : ents_) {
                if (dragged.count(oe.id) || (oe.type != "circle" && oe.type != "arc")) continue;
                float sd = (oe.center[0] - lp1u) * nx + (oe.center[1] - lp1v) * ny;
                float t = ((oe.center[0] - lp1u) * dx + (oe.center[1] - lp1v) * dy) / (L * L);
                if (t < 0.0f || t > 1.0f) continue;
                float gap = std::fabs(sd) - oe.radius;
                if (std::fabs(gap) * pxPerU >= snapPx || std::fabs(sd) < 1e-5f) continue;
                float corr = gap * (sd >= 0 ? 1.0f : -1.0f);   // push the line out to tangency
                gu += nx * corr; gv += ny * corr;
                dragRelType_ = "tangent"; dragRelEnt_ = e.id; dragRelOther_ = oe.id;
                float n1u = lp1u + nx * corr, n1v = lp1v + ny * corr;
                float s2 = (oe.center[0] - n1u) * nx + (oe.center[1] - n1v) * ny;
                dragSnapUV_ = ImVec2(oe.center[0] - nx * s2, oe.center[1] - ny * s2);
                dragSnapLive_ = true;
                return;
            }
            continue;
        }
        if (e.type != "circle" && e.type != "arc") continue;
        const float R = e.radius;
        const float lcx = e.center[0] + dx0, lcy = e.center[1] + dy0;
        for (const auto& oe : ents_) {
            if (dragged.count(oe.id)) continue;
            if (oe.type == "line") {
                float dx = oe.p2[0] - oe.p1[0], dy = oe.p2[1] - oe.p1[1];
                float L = std::hypot(dx, dy);
                if (L < 1e-5f) continue;
                float nx = -dy / L, ny = dx / L;                    // unit normal
                float sd = (lcx - oe.p1[0]) * nx + (lcy - oe.p1[1]) * ny;  // signed distance
                float t = ((lcx - oe.p1[0]) * dx + (lcy - oe.p1[1]) * dy) / (L * L);
                if (t < 0.0f || t > 1.0f) continue;   // contact point must be on the segment
                float gap = std::fabs(sd) - R;
                if (std::fabs(gap) * pxPerU >= snapPx || std::fabs(sd) < 1e-5f) continue;
                float corr = -gap * (sd >= 0 ? 1.0f : -1.0f);   // close the gap along the normal
                gu += nx * corr; gv += ny * corr;
                dragRelType_ = "tangent"; dragRelEnt_ = e.id; dragRelOther_ = oe.id;
                // Marker at the contact point (center shifted by R along the normal, post-snap).
                float ncx = lcx + nx * corr, ncy = lcy + ny * corr;
                float s2 = (ncx - oe.p1[0]) * nx + (ncy - oe.p1[1]) * ny;
                dragSnapUV_ = ImVec2(ncx - nx * s2, ncy - ny * s2);
                dragSnapLive_ = true;
                return;
            }
            if (oe.type == "circle" || oe.type == "arc") {
                const float Ro = oe.radius;
                const float dcx = oe.center[0] - lcx, dcy = oe.center[1] - lcy;
                const float dc = std::hypot(dcx, dcy);
                if (dc < 1e-5f) continue;   // concentric — coincident center snap owns this
                float targets[2] = {R + Ro, std::fabs(R - Ro)};   // external / internal contact
                for (float tgt : targets) {
                    float gap = dc - tgt;
                    if (std::fabs(gap) * pxPerU >= snapPx || tgt < 1e-4f) continue;
                    float ux = dcx / dc, uy = dcy / dc;
                    gu += ux * gap; gv += uy * gap;   // move along the center line onto contact
                    dragRelType_ = "tangent"; dragRelEnt_ = e.id; dragRelOther_ = oe.id;
                    float ncx = lcx + ux * gap, ncy = lcy + uy * gap;
                    dragSnapUV_ = ImVec2(ncx + ux * R, ncy + uy * R);
                    dragSnapLive_ = true;
                    return;
                }
            }
        }
    }
    // 2c) Single-axis alignment: a dragged keypoint's u (or v) matches a target point's u (or v).
    {
        float bestU = snapPx, bestV = snapPx;
        const Tgt *btU = nullptr, *btV = nullptr;
        std::string srcU, srcV;
        for (const auto& t : groupDragTargets_) {
            float pu = t.second.x + dx0, pv = t.second.y + dy0;
            for (const auto& g : tgts) {
                if (snapHasPrefix(g.handle, "mid:")) continue;   // alignment vs. real points only
                float dU = std::fabs(pu - g.u) * pxPerU;
                float dV = std::fabs(pv - g.v) * pxPerV;
                if (dU < bestU) { bestU = dU; btU = &g; srcU = t.first; }
                if (dV < bestV) { bestV = dV; btV = &g; srcV = t.first; }
            }
        }
        if (btU || btV) {
            if (btU) {
                ImVec2 o = origOf(srcU);
                gu += btU->u - (o.x + dx0);
                dragSnapInfer_.alignU = true; dragSnapInfer_.srcU = ImVec2(btU->u, btU->v);
                dragSnapInfer_.srcUHandle = btU->handle; dragAlignSrcU_ = srcU;
            }
            if (btV) {
                ImVec2 o = origOf(srcV);
                gv += btV->v - (o.y + dy0);
                dragSnapInfer_.alignV = true; dragSnapInfer_.srcV = ImVec2(btV->u, btV->v);
                dragSnapInfer_.srcVHandle = btV->handle; dragAlignSrcV_ = srcV;
            }
            const std::string& anchor = btU ? srcU : srcV;
            ImVec2 o = origOf(anchor);
            dragSnapUV_ = ImVec2(o.x + (gu - groupDragStartUV_.x),
                                 o.y + (gv - groupDragStartUV_.y));
            dragSnapLive_ = true;
            return;
        }
    }
    // 3) Collinear snap: a dragged line whose direction is parallel to a static line and whose
    //    supporting line is within snap range snaps perpendicular so the two lines coincide.
    for (const auto& e : ents_) {
        if (!dragged.count(e.id) || e.type != "line") continue;
        float dux = e.p2[0] - e.p1[0], duv = e.p2[1] - e.p1[1];
        float L = std::hypot(dux, duv);
        if (L < 1e-5f) continue;
        dux /= L; duv /= L;
        const float live1u = e.p1[0] + dx0, live1v = e.p1[1] + dy0;
        for (const auto& oe : ents_) {
            if (dragged.count(oe.id) || oe.type != "line") continue;
            float ox = oe.p2[0] - oe.p1[0], oy = oe.p2[1] - oe.p1[1];
            float oL = std::hypot(ox, oy);
            if (oL < 1e-5f) continue;
            ox /= oL; oy /= oL;
            float ang = std::acos(std::fmin(1.0f, std::fabs(dux * ox + duv * oy))) * 57.2957795f;
            if (ang > kInferHVDeg) continue;   // not parallel — a translate can't fix that
            // Signed perpendicular distance from oe.p1 to the dragged line's supporting line.
            float cross = (oe.p1[0] - live1u) * duv - (oe.p1[1] - live1v) * dux;
            float crossPx = std::fabs(cross) * pxPerU;
            if (crossPx > snapPx || crossPx < 0.01f) continue;   // out of range / already exact
            gu += cross * duv;   // shift along the normal so the supporting lines coincide
            gv += -cross * dux;
            dragRelType_ = "collinear"; dragRelEnt_ = e.id; dragRelOther_ = oe.id;
            dragSnapUV_ = ImVec2((live1u + e.p2[0] + dx0) * 0.5f + cross * duv,
                                 (live1v + e.p2[1] + dy0) * 0.5f - cross * dux);
            dragSnapLive_ = true;
            return;
        }
    }
}

// C56: snap a circle/arc radius drag. `r` is the raw radius from the cursor; on return it's the
// snapped radius. Priority (best screen-px radius delta wins): rim through a point handle
// (point_on) > tangent to a line > tangent to another circle/arc > equal radius. Preview state
// (dragSnap* members) is filled for the draw pass + applyDragSnapRelations on drop.
void SketchMode::rimDragSnap(const SketchEntity& e, float& r) {
    dragSnapLive_ = false; dragSnapHandle_.clear();
    dragRelType_.clear(); dragRelOther_.clear(); dragRelEnt_.clear();
    dragAlignSrcU_.clear(); dragAlignSrcV_.clear();
    dragSnapInfer_ = SnapInfer{};
    const float snapPx = kSnapPx * ui::uiScale;
    const float pxPerU = std::hypot(uAxisS_.x, uAxisS_.y);
    const float cx = e.center[0], cy = e.center[1];
    float bestPx = snapPx;
    int kind = 0;                    // 1=through-point 2=tangent line 3=tangent circle 4=equal
    std::string ref;                 // point handle (1) or entity id (2/3/4)
    float snapR = r;
    ImVec2 marker(cx + r, cy);
    // 1) Rim through a point handle (origin / endpoints / centers / points / spline points).
    auto considerPoint = [&](const std::string& h, float pu, float pv) {
        float d = std::hypot(pu - cx, pv - cy);
        float dpx = std::fabs(d - r) * pxPerU;
        if (dpx < bestPx) { bestPx = dpx; kind = 1; ref = h; snapR = d; marker = ImVec2(pu, pv); }
    };
    considerPoint("origin", 0.0f, 0.0f);
    for (const auto& oe : ents_) {
        if (oe.id == e.id) continue;   // own handles ride the rim/center — not useful targets
        if (oe.type == "line" || oe.type == "slot") {
            considerPoint(oe.id + ".p1", oe.p1[0], oe.p1[1]);
            considerPoint(oe.id + ".p2", oe.p2[0], oe.p2[1]);
        } else if (oe.type == "circle" || oe.type == "arc" || oe.type == "arc_slot" ||
                   oe.type == "ellipse") {
            considerPoint(oe.id + ".center", oe.center[0], oe.center[1]);
        } else if (oe.type == "point") {
            considerPoint(oe.id + ".p", oe.p1[0], oe.p1[1]);
        } else if (oe.type == "spline" || oe.type == "style_spline") {
            for (size_t i = 0; i < oe.pts.size(); ++i)
                considerPoint(oe.id + ".p" + std::to_string(i), oe.pts[i].x, oe.pts[i].y);
        }
    }
    for (const auto& oe : ents_) {
        if (oe.id == e.id) continue;
        if (oe.type == "line") {
            // 2) Tangent to a line: radius == perpendicular distance center&#8594;line (foot on segment).
            float dx = oe.p2[0] - oe.p1[0], dy = oe.p2[1] - oe.p1[1];
            float L = std::hypot(dx, dy);
            if (L < 1e-5f) continue;
            float cross = (cx - oe.p1[0]) * dy - (cy - oe.p1[1]) * dx;
            float d = std::fabs(cross) / L;
            if (d < 1e-5f) continue;    // center ON the line — r=0 tangency is degenerate
            float t = ((cx - oe.p1[0]) * dx + (cy - oe.p1[1]) * dy) / (L * L);
            if (t < 0.0f || t > 1.0f) continue;   // tangency point would fall off the segment
            float dpx = std::fabs(d - r) * pxPerU;
            if (dpx < bestPx) {
                bestPx = dpx; kind = 2; ref = oe.id; snapR = d;
                float fx = oe.p1[0] + t * dx, fy = oe.p1[1] + t * dy;
                marker = ImVec2(fx, fy);
            }
        } else if (oe.type == "circle" || oe.type == "arc") {
            // 3) Tangent to another circle/arc: r == dc - R (external), r == R - dc (inside it),
            //    or r == dc + R (enclosing it, internal contact).
            const float R = oe.radius;
            const float dc = std::hypot(oe.center[0] - cx, oe.center[1] - cy);
            if (dc < 1e-5f) continue;   // concentric — equal (4) owns this pair
            float cands[3] = {dc - R, R - dc, dc + R};
            for (float cand : cands) {
                if (cand <= 1e-4f) continue;
                float dpx = std::fabs(cand - r) * pxPerU;
                if (dpx < bestPx) {
                    bestPx = dpx; kind = 3; ref = oe.id; snapR = cand;
                    float ux = (oe.center[0] - cx) / dc, uy = (oe.center[1] - cy) / dc;
                    marker = ImVec2(cx + ux * cand, cy + uy * cand);
                }
            }
            // 4) Equal radius.
            float dpx = std::fabs(R - r) * pxPerU;
            if (dpx < bestPx) {
                bestPx = dpx; kind = 4; ref = oe.id; snapR = R;
                float ux = (oe.center[0] - cx) / dc, uy = (oe.center[1] - cy) / dc;
                marker = ImVec2(cx + ux * R, cy + uy * R);
            }
        }
    }
    if (!kind) return;
    r = snapR;
    dragSnapLive_ = true;
    dragSnapUV_ = marker;
    dragRelEnt_ = e.id;
    if (kind == 1) { dragRelType_ = "point_on"; dragRelOther_ = ref; }  // other = point handle
    else if (kind == 4) { dragRelType_ = "equal"; dragRelOther_ = ref; }
    else { dragRelType_ = "tangent"; dragRelOther_ = ref; }
}

// C56: is this relation already on the sketch? Compares type + refs as an unordered set so a
// drag drop never double-adds a constraint that already exists (e.g. re-dropping onto the same
// origin the entity is already coincident with).
bool SketchMode::constraintExists(const std::string& type,
                                  const std::vector<std::string>& refs) const {
    for (const SketchConstraint& c : cons_) {
        if (c.type != type || c.refs.size() != refs.size()) continue;
        std::vector<std::string> a = c.refs, b = refs;
        std::sort(a.begin(), a.end());
        std::sort(b.begin(), b.end());
        if (a == b) return true;
    }
    return false;
}

// C56: commit the previewed drag-snap relations as real constraints (Settings -> Modeling).
// Runs right after sketch_drag_commit so the geometry is already at the snapped position; the
// solver only has to record the relation. One applyOps batch = one undo step.
void SketchMode::applyDragSnapRelations(AppState& st, bool groupDrag) {
    const bool live = dragSnapLive_;
    const std::string snap = dragSnapHandle_;
    const SnapInfer infer = dragSnapInfer_;
    const std::string relType = dragRelType_, relOther = dragRelOther_, relEnt = dragRelEnt_;
    const std::string src = groupDrag ? groupSnapSrc_ : dragHandle_;
    const std::string alignSrcU = dragAlignSrcU_, alignSrcV = dragAlignSrcV_;
    dragSnapLive_ = false; dragSnapHandle_.clear(); dragRelType_.clear();
    dragRelOther_.clear(); dragRelEnt_.clear(); groupSnapSrc_.clear();
    dragAlignSrcU_.clear(); dragAlignSrcV_.clear();
    dragSnapInfer_ = SnapInfer{};
    if (!live || !st.settings.sketchDragSnapRelations) return;

    nlohmann::json cs = nlohmann::json::array();
    auto push = [&](const char* type, std::vector<std::string> refs) {
        if (!constraintExists(type, refs))
            cs.push_back({{"type", type}, {"refs", std::move(refs)}});
    };
    // Point snap / alignment relations need the dragged point handle; a group drag that only
    // fired a collinear/parallel line relation has no keypoint snap and skips these.
    if (!snap.empty() && !src.empty()) {
        if (snapHasPrefix(snap, "mid:"))
            push("midpoint", {src, snap.substr(4)});
        else if (snapHasPrefix(snap, "rim:"))
            push("point_on", {src, snap.substr(4)});
        else
            push("coincident", {snap, src});
    }
    // Alignment guides (alone, or riding a rim snap) -> two-point vertical (same u) /
    // horizontal (same v) relations.
    if ((snap.empty() || snapHasPrefix(snap, "rim:")) &&
        (!src.empty() || !alignSrcU.empty() || !alignSrcV.empty())) {
        const std::string srcU = alignSrcU.empty() ? src : alignSrcU;
        const std::string srcV = alignSrcV.empty() ? src : alignSrcV;
        if (infer.alignU && !infer.srcUHandle.empty() && !srcU.empty())
            push("vertical", {infer.srcUHandle, srcU});
        if (infer.alignV && !infer.srcVHandle.empty() && !srcV.empty())
            push("horizontal", {infer.srcVHandle, srcV});
    }
    if (!relType.empty() && !relEnt.empty()) {
        if (relType == "horizontal" || relType == "vertical")
            push(relType.c_str(), {relEnt});
        else if (relType == "point_on" && !relOther.empty())
            push("point_on", {relOther, relEnt});   // other = the point handle, ent = circle/arc
        else if ((relType == "parallel" || relType == "collinear" || relType == "tangent" ||
                  relType == "equal") && !relOther.empty())
            push(relType.c_str(), {relEnt, relOther});
    }
    if (cs.empty()) return;
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& c : cs)
        ops.push_back({{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", c}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) { st.addLog("[sketch] drag-snap relation failed: " + r.error_message); return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
}

// ---------------------------------------------------------------------------
// Trim doomed-span preview (client-side mirror of engine sketch_geom.split_entity)
// ---------------------------------------------------------------------------
// The engine splits the target at every in-range crossing with the OTHER lines/circles/arcs and
// deletes the span nearest the click. Replicating that math here lets the Trim tool highlight
// exactly what a click will remove (SolidWorks power-trim red segment) instead of the whole
// entity. Geometry only — the engine stays authoritative on the actual edit.
namespace {
struct TrimHit { float ta; float tb; float px, py; };  // params on target/other + point

// All crossings of two entities among line/circle/arc. ta/tb are the natural parameters
// (line: 0..1; circle/arc: angle in degrees, [0,360)). Mirrors sketch_geom.intersect.
void trimIntersect(const SketchEntity& A, const SketchEntity& B, std::vector<TrimHit>& out) {
    auto angOf = [](float cx, float cy, float x, float y) {
        float a = std::atan2(y - cy, x - cx) * 57.29577951f;
        return a < 0 ? a + 360.0f : a;
    };
    bool aLine = (A.type == "line"), bLine = (B.type == "line");
    if (aLine && bLine) {
        float d1x = A.p2[0] - A.p1[0], d1y = A.p2[1] - A.p1[1];
        float d2x = B.p2[0] - B.p1[0], d2y = B.p2[1] - B.p1[1];
        float den = d1x * d2y - d1y * d2x;
        if (std::fabs(den) < 1e-9f) return;
        float fx = B.p1[0] - A.p1[0], fy = B.p1[1] - A.p1[1];
        float t = (fx * d2y - fy * d2x) / den;
        float u = (fx * d1y - fy * d1x) / den;
        out.push_back({t, u, A.p1[0] + d1x * t, A.p1[1] + d1y * t});
        return;
    }
    if (aLine != bLine) {
        const SketchEntity& L = aLine ? A : B;
        const SketchEntity& C = aLine ? B : A;
        float dx = L.p2[0] - L.p1[0], dy = L.p2[1] - L.p1[1];
        float L2 = dx * dx + dy * dy;
        if (L2 < 1e-14f) return;
        float fx = L.p1[0] - C.center[0], fy = L.p1[1] - C.center[1];
        float b = 2.0f * (fx * dx + fy * dy);
        float c = fx * fx + fy * fy - C.radius * C.radius;
        float disc = b * b - 4.0f * L2 * c;
        if (disc < 0) return;
        float sq = std::sqrt(std::fmax(disc, 0.0f));
        float prevX = 1e30f, prevY = 1e30f;
        for (float t : {(-b - sq) / (2.0f * L2), (-b + sq) / (2.0f * L2)}) {
            float x = L.p1[0] + dx * t, y = L.p1[1] + dy * t;
            if (std::fabs(x - prevX) < 1e-7f && std::fabs(y - prevY) < 1e-7f) continue;  // tangent
            prevX = x; prevY = y;
            float ang = angOf(C.center[0], C.center[1], x, y);
            if (aLine) out.push_back({t, ang, x, y});
            else       out.push_back({ang, t, x, y});
        }
        return;
    }
    // both curved (circle/arc): circle-circle intersection on the underlying circles
    float dxc = B.center[0] - A.center[0], dyc = B.center[1] - A.center[1];
    float d = std::hypot(dxc, dyc);
    if (d < 1e-9f) return;  // concentric
    if (d > A.radius + B.radius + 1e-7f || d < std::fabs(A.radius - B.radius) - 1e-7f) return;
    float a = (A.radius * A.radius - B.radius * B.radius + d * d) / (2.0f * d);
    float h2 = A.radius * A.radius - a * a;
    float h = std::sqrt(std::fmax(h2, 0.0f));
    float ux = dxc / d, uy = dyc / d;
    float mx = A.center[0] + ux * a, my = A.center[1] + uy * a;
    float prevX = 1e30f, prevY = 1e30f;
    for (float sgn : {1.0f, -1.0f}) {
        float x = mx + sgn * h * (-uy), y = my + sgn * h * ux;
        if (std::fabs(x - prevX) < 1e-7f && std::fabs(y - prevY) < 1e-7f) continue;
        prevX = x; prevY = y;
        out.push_back({angOf(A.center[0], A.center[1], x, y),
                       angOf(B.center[0], B.center[1], x, y), x, y});
    }
}

// Is angle (deg) within the arc's drawn sweep? Mirrors sketch_geom._arc_sweep_contains.
bool arcSweepContains(const SketchEntity& arc, float ang) {
    float a0 = std::fmod(arc.start, 360.0f); if (a0 < 0) a0 += 360.0f;
    float a1 = std::fmod(arc.end, 360.0f);   if (a1 < 0) a1 += 360.0f;
    float a = std::fmod(ang, 360.0f);        if (a < 0)  a += 360.0f;
    float span, rel;
    if (arc.ccw) { span = std::fmod(a1 - a0 + 360.0f, 360.0f); rel = std::fmod(a - a0 + 360.0f, 360.0f); }
    else         { span = std::fmod(a0 - a1 + 360.0f, 360.0f); rel = std::fmod(a0 - a + 360.0f, 360.0f); }
    if (span < 1e-7f) span = 360.0f;
    return rel <= span + 1e-5f;
}

bool trimParamInRange(const SketchEntity& e, float t) {
    if (e.type == "line")   return t >= -1e-7f && t <= 1.0f + 1e-7f;
    if (e.type == "circle") return true;
    if (e.type == "arc")    return arcSweepContains(e, t);
    return false;
}
}  // namespace

bool SketchMode::trimDoomedSpan(int idx, float mu, float mv,
                                std::vector<ImVec2>& outUV, bool& wholeEntity) const {
    outUV.clear(); wholeEntity = false;
    if (idx < 0 || idx >= (int)ents_.size()) return false;
    const SketchEntity& T = ents_[idx];
    if (T.type != "line" && T.type != "circle" && T.type != "arc") return false;

    // Cut parameters on the target from every other line/circle/arc (in-range on BOTH).
    std::vector<float> cuts;
    for (size_t i = 0; i < ents_.size(); ++i) {
        if ((int)i == idx) continue;
        const SketchEntity& O = ents_[i];
        if (O.type != "line" && O.type != "circle" && O.type != "arc") continue;
        std::vector<TrimHit> hits;
        trimIntersect(T, O, hits);
        for (const auto& h : hits)
            if (trimParamInRange(O, h.tb) && trimParamInRange(T, h.ta)) cuts.push_back(h.ta);
    }

    auto sampleArc = [&](float cx, float cy, float r, float aa0, float aa1) {
        int seg = 48;
        const float D2R = 3.14159265f / 180.0f;
        for (int i = 0; i <= seg; ++i) {
            float a = (aa0 + (aa1 - aa0) * (i / (float)seg)) * D2R;
            outUV.push_back(ImVec2(cx + r * std::cos(a), cy + r * std::sin(a)));
        }
    };

    if (T.type == "line") {
        // interior cuts sorted, endpoints excluded (engine _dedupe_sorted)
        std::sort(cuts.begin(), cuts.end());
        std::vector<float> inner;
        for (float t : cuts) {
            if (t <= 1e-7f || t >= 1.0f - 1e-7f) continue;
            if (!inner.empty() && std::fabs(t - inner.back()) <= 1e-6f) continue;
            inner.push_back(t);
        }
        if (inner.empty()) { wholeEntity = true; return true; }
        std::vector<float> bounds; bounds.push_back(0.0f);
        for (float t : inner) bounds.push_back(t);
        bounds.push_back(1.0f);
        float dx = T.p2[0] - T.p1[0], dy = T.p2[1] - T.p1[1];
        int best = 0; float bestD = 1e30f;
        for (size_t i = 0; i + 1 < bounds.size(); ++i) {
            float tm = (bounds[i] + bounds[i + 1]) * 0.5f;
            float x = T.p1[0] + dx * tm, y = T.p1[1] + dy * tm;
            float dd = std::hypot(x - mu, y - mv);
            if (dd < bestD) { bestD = dd; best = (int)i; }
        }
        float t0 = bounds[best], t1 = bounds[best + 1];
        outUV.push_back(ImVec2(T.p1[0] + dx * t0, T.p1[1] + dy * t0));
        outUV.push_back(ImVec2(T.p1[0] + dx * t1, T.p1[1] + dy * t1));
        return true;
    }

    if (T.type == "circle") {
        // distinct cut angles; need >= 2 to form removable arcs (engine _split_circle)
        std::vector<float> angs;
        for (float a : cuts) { a = std::fmod(a, 360.0f); if (a < 0) a += 360.0f; angs.push_back(a); }
        std::sort(angs.begin(), angs.end());
        std::vector<float> ded;
        for (float a : angs) {
            if (!ded.empty() && std::fabs(a - ded.back()) <= 1e-6f) continue;
            ded.push_back(a);
        }
        if (ded.size() >= 2 && std::fabs((ded.front() + 360.0f) - ded.back()) <= 1e-6f) ded.pop_back();
        if (ded.size() < 2) { wholeEntity = true; return true; }
        const float D2R = 3.14159265f / 180.0f;
        int n = (int)ded.size(); int best = 0; float bestD = 1e30f;
        for (int i = 0; i < n; ++i) {
            float a0 = ded[i], a1 = ded[(i + 1) % n];
            float span = std::fmod(a1 - a0 + 360.0f, 360.0f); if (span < 1e-7f) span = 360.0f;
            float am = (a0 + span * 0.5f) * D2R;
            float x = T.center[0] + T.radius * std::cos(am), y = T.center[1] + T.radius * std::sin(am);
            float dd = std::hypot(x - mu, y - mv);
            if (dd < bestD) { bestD = dd; best = i; }
        }
        float a0 = ded[best], a1 = ded[(best + 1) % n];
        if (a1 <= a0) a1 += 360.0f;
        sampleArc(T.center[0], T.center[1], T.radius, a0, a1);
        return true;
    }

    // arc: relative sweep offsets within (0, full), spans in the drawn direction (engine _split_arc)
    float a0 = std::fmod(T.start, 360.0f); if (a0 < 0) a0 += 360.0f;
    float a1 = std::fmod(T.end, 360.0f);   if (a1 < 0) a1 += 360.0f;
    float full = T.ccw ? std::fmod(a1 - a0 + 360.0f, 360.0f) : std::fmod(a0 - a1 + 360.0f, 360.0f);
    if (full < 1e-7f) full = 360.0f;
    std::vector<float> rels;
    for (float a : cuts) {
        float rel = T.ccw ? std::fmod(a - a0 + 360.0f, 360.0f) : std::fmod(a0 - a + 360.0f, 360.0f);
        if (rel > 1e-7f && rel < full - 1e-7f) rels.push_back(rel);
    }
    std::sort(rels.begin(), rels.end());
    std::vector<float> ded;
    for (float r2 : rels) {
        if (!ded.empty() && std::fabs(r2 - ded.back()) <= 1e-6f) continue;
        ded.push_back(r2);
    }
    if (ded.empty()) { wholeEntity = true; return true; }
    std::vector<float> bounds; bounds.push_back(0.0f);
    for (float r2 : ded) bounds.push_back(r2);
    bounds.push_back(full);
    const float D2R = 3.14159265f / 180.0f;
    int best = 0; float bestD = 1e30f;
    for (size_t i = 0; i + 1 < bounds.size(); ++i) {
        float rm = (bounds[i] + bounds[i + 1]) * 0.5f;
        float am = (T.ccw ? a0 + rm : a0 - rm) * D2R;
        float x = T.center[0] + T.radius * std::cos(am), y = T.center[1] + T.radius * std::sin(am);
        float dd = std::hypot(x - mu, y - mv);
        if (dd < bestD) { bestD = dd; best = (int)i; }
    }
    float s0 = T.ccw ? a0 + bounds[best] : a0 - bounds[best];
    float s1 = T.ccw ? a0 + bounds[best + 1] : a0 - bounds[best + 1];
    sampleArc(T.center[0], T.center[1], T.radius, s0, s1);
    return true;
}

// ---------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------

std::string SketchMode::statusLine() const {
    if (!active_) return {};
    // C41 (T4): a sketch with no geometry isn't meaningfully "Fully Defined DOF: 0" — invite
    // the user to draw instead.
    if (ents_.empty()) return "Empty sketch \xE2\x80\x94 pick a tool to draw";
    const char* s = status_ == "full" ? "Fully Defined"
                  : status_ == "over" ? "Over Defined" : "Under Defined";
    char buf[96];
    // C50f: surface the conflict/redundancy count when over-defined (SolidWorks-style).
    if (status_ == "over" && !conflicting_.empty())
        std::snprintf(buf, sizeof(buf), "%s \xE2\x80\x94 %d conflicting   DOF: %d",
                      s, (int)conflicting_.size(), dof_);
    else if (status_ == "over" && !redundant_.empty())
        std::snprintf(buf, sizeof(buf), "%s \xE2\x80\x94 %d redundant   DOF: %d",
                      s, (int)redundant_.size(), dof_);
    else
        std::snprintf(buf, sizeof(buf), "%s   DOF: %d", s, dof_);
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// C37-D: Smart Dimension state machine + dimension annotation rendering
// ---------------------------------------------------------------------------

namespace {
// Active document-unit display for the sketcher's on-canvas dimensions + Modify box. Set once per
// frame from AppState.ips() at the top of drawOverlay so the free-function formatters below (which
// have no AppState) follow the MMGS<->IPS toggle. Sketch measured values are canonical mm; angles
// stay in degrees in both systems.
bool g_skIps = false;
const char* skLenUnit() { return g_skIps ? "in" : "mm"; }

// Format a length (mm) for a dimension label in the active document length unit (mm / inch),
// trimming trailing zeros. The unit suffix is added by the caller.
std::string fmtLen(double mm) {
    double v = g_skIps ? mm / 25.4 : mm;
    char b[48]; std::snprintf(b, sizeof(b), g_skIps ? "%.4g" : "%.2f", v);
    std::string s(b);
    if (s.find('.') != std::string::npos) { while (s.back() == '0') s.pop_back(); if (s.back() == '.') s.pop_back(); }
    return s;
}
std::string fmtAng(double deg) {
    char b[48]; std::snprintf(b, sizeof(b), "%.2f", deg);
    std::string s(b);
    if (s.find('.') != std::string::npos) { while (s.back() == '0') s.pop_back(); if (s.back() == '.') s.pop_back(); }
    return s + "\xC2\xB0";   // degree sign
}
// Filled triangle arrowhead at tip, pointing along (dirx,diry) (unit), size px (caller scales
// by ui::uiScale). 0.45 aspect — the old 0.35 darts aliased into slivers at small sizes.
void arrowHead(ImDrawList* dl, ImVec2 tip, float dx, float dy, float sz, ImU32 c) {
    float px = -dy, py = dx;   // perpendicular
    ImVec2 a(tip.x - dx * sz + px * sz * 0.45f, tip.y - dy * sz + py * sz * 0.45f);
    ImVec2 b(tip.x - dx * sz - px * sz * 0.45f, tip.y - dy * sz - py * sz * 0.45f);
    dl->AddTriangleFilled(tip, a, b, c);
}
// Shared annotation metrics (one block so every dimension part scales consistently on HiDPI).
inline float dimArrowSz() { return 8.0f * ui::uiScale; }   // arrowhead length
inline float dimGapPx()   { return 4.0f * ui::uiScale; }   // ext-line gap from geometry
inline float dimExtPx()   { return 6.0f * ui::uiScale; }   // ext-line overshoot past dim line
inline float dimThick(bool hovered) { return (hovered ? 2.2f : 1.4f) * ui::uiScale; }
}  // namespace

// Label text for a constraint (driven dims are parenthesized, SolidWorks-style).
static std::string dimLabel(const SketchConstraint& c) {
    double v = c.hasMeasured ? c.measured : (c.hasValue ? c.value : 0.0);
    std::string body;
    if (c.type == "angle")          body = fmtAng(v);
    else if (c.type == "radius")    body = "R" + fmtLen(v);
    else if (c.type == "diameter")  body = "\xE2\x8C\x80" + fmtLen(v);   // ⌀ U+2300 (CADSymbols)
    else                            body = fmtLen(v);
    return c.driven ? "(" + body + ")" : body;
}

// A default annotation anchor in (u,v) for a constraint that has no stored placement: offset the
// geometry's natural midpoint a bit so the line/arrows clear the entity.
bool SketchMode::defaultPlacement(const SketchConstraint& c, float& pu, float& pv) const {
    if ((c.type == "length" || c.type == "distance") && c.refs.size() == 1) {
        const auto* e = entById(c.refs[0]);
        if (!e || e->type != "line") return false;
        const float dx = e->p2[0] - e->p1[0], dy = e->p2[1] - e->p1[1];
        const float length = std::hypot(dx, dy);
        pu = (e->p1[0] + e->p2[0]) * 0.5f + (length > 1e-6f ? -dy / length : 0) * 12.0f;
        pv = (e->p1[1] + e->p2[1]) * 0.5f + (length > 1e-6f ? dx / length : 1) * 12.0f;
        return true;
    }
    if (c.type == "arc_length" && c.refs.size() == 1) {
        float u, v;
        if (!relationPointUV(c.refs[0] + ".mid", u, v)) return false;
        const auto* e = entById(c.refs[0]);
        if (!e) return false;
        pu = e->center[0] + (u - e->center[0]) * 1.3f;
        pv = e->center[1] + (v - e->center[1]) * 1.3f;
        return true;
    }
    auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
    auto pt = [&](const std::string& h, float& u, float& v) -> bool {
        return relationPointUV(h, u, v);
    };
    if (c.type == "radius" || c.type == "diameter") {
        const SketchEntity* e = c.refs.empty() ? nullptr : entById(baseId(c.refs[0]));
        if (!e) return false;
        if (e->type == "slot") {   // C51: width dim sits off the spine midpoint, past the side
            float mx = (e->p1[0]+e->p2[0])*0.5f, my = (e->p1[1]+e->p2[1])*0.5f;
            float sx = e->p2[0]-e->p1[0], sy = e->p2[1]-e->p1[1]; float SL = std::hypot(sx, sy);
            float nnx = SL>1e-6f? -sy/SL : 0.0f, nny = SL>1e-6f? sx/SL : 1.0f;
            pu = mx + nnx * (e->width * 0.5f + 12.0f); pv = my + nny * (e->width * 0.5f + 12.0f);
            return true;
        }
        pu = e->center[0] + e->radius * 0.707f; pv = e->center[1] + e->radius * 0.707f;
        return true;
    }
    if (c.type == "angle") {
        if (c.refs.size() < 2) return false;
        ImVec2 a1, a2, b1, b2;
        if (!relationLineUV(c.refs[0], a1, a2) || !relationLineUV(c.refs[1], b1, b2)) return false;
        pu = (a1.x + a2.x + b1.x + b2.x) * 0.25f;
        pv = (a1.y + a2.y + b1.y + b2.y) * 0.25f;
        return true;
    }
    // distance: midpoint of the two refs, pushed perpendicular by a fixed sketch offset.
    if (c.refs.size() < 2) return false;
    float u0, v0, u1, v1;
    bool r0 = pt(c.refs[0], u0, v0);
    // point-line distance: second ref may be a bare entity id (line)
    const SketchEntity* le = entById(baseId(c.refs[1]));
    bool secondIsLine = le && le->type == "line" && c.refs[1].find('.') == std::string::npos;
    if (!r0) return false;
    if (secondIsLine) { u1 = (le->p1[0] + le->p2[0]) * 0.5f; v1 = (le->p1[1] + le->p2[1]) * 0.5f; }
    else if (!pt(c.refs[1], u1, v1)) return false;
    float mu = (u0 + u1) * 0.5f, mv = (v0 + v1) * 0.5f;
    float du = u1 - u0, dv = v1 - v0; float L = std::hypot(du, dv);
    float nx = L > 1e-6f ? -dv / L : 0, ny = L > 1e-6f ? du / L : 1;
    pu = mu + nx * 12.0f; pv = mv + ny * 12.0f;
    return true;
}

// Render one dimension annotation. (pu,pv) is the placement in (u,v).
void SketchMode::drawDimension(ImDrawList* dl, const SketchConstraint& c, ImU32 lineCol,
                               ImU32 textCol, bool hovered, float pu, float pv, DimHit* hitOut) const {
    // C50g: helper to record the rendered text box (padded) + a dim-line segment for picking.
    auto recText = [&](ImVec2 pos, ImVec2 size) {
        if (!hitOut) return;
        const float pad = 4.0f * ui::uiScale;
        hitOut->textMin = ImVec2(pos.x - pad, pos.y - pad);
        hitOut->textMax = ImVec2(pos.x + size.x + pad, pos.y + size.y + pad);
    };
    auto recSeg = [&](ImVec2 a, ImVec2 b) {
        if (!hitOut || hitOut->nSeg >= 2) return;
        hitOut->seg[hitOut->nSeg][0] = a; hitOut->seg[hitOut->nSeg][1] = b; hitOut->nSeg++;
    };
    auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
    auto handlePt = [&](const std::string& h, ImVec2& s) -> bool {
        float u, v;
        if (!relationPointUV(h, u, v)) return false;
        s = uvToScreen(u, v);
        return true;
    };
    ImVec2 place = uvToScreen(pu, pv);
    std::string lbl = dimLabel(c);
    ImVec2 ts = ImGui::CalcTextSize(lbl.c_str());
    const float th = dimThick(hovered);
    const float asz = dimArrowSz();
    if (c.type == "arc_length" && c.refs.size() == 1) {
        float u, v;
        if (!relationPointUV(c.refs[0] + ".mid", u, v)) return;
        const ImVec2 anchor = uvToScreen(u, v);
        const std::string text = "Arc " + lbl;
        const ImVec2 size = ImGui::CalcTextSize(text.c_str());
        const ImVec2 tp(place.x + 4.0f * ui::uiScale, place.y - size.y * 0.5f);
        dl->AddLine(anchor, place, lineCol, th); recSeg(anchor, place);
        const float dx = place.x - anchor.x, dy = place.y - anchor.y;
        const float length = std::hypot(dx, dy);
        if (length > 1e-6f) arrowHead(dl, anchor, -dx / length, -dy / length, asz, lineCol);
        labelChip(dl, tp, ImVec2(tp.x + size.x, tp.y + size.y));
        dl->AddText(tp, textCol, text.c_str()); recText(tp, size);
        return;
    }

    // C51: a slot WIDTH dim (diameter/radius constraint on a slot) renders as a LINEAR dimension
    // across the slot at its spine midpoint — not a circle leader — with a plain (un-⌀'d) value.
    if ((c.type == "diameter" || c.type == "radius") && !c.refs.empty()) {
        const SketchEntity* se = entById(baseId(c.refs[0]));
        if (se && se->type == "slot") {
            float mx = (se->p1[0] + se->p2[0]) * 0.5f, my = (se->p1[1] + se->p2[1]) * 0.5f;
            float sx = se->p2[0] - se->p1[0], sy = se->p2[1] - se->p1[1];
            float SL = std::hypot(sx, sy);
            float nnx = SL > 1e-6f ? -sy / SL : 0.0f, nny = SL > 1e-6f ? sx / SL : 1.0f;
            float hw = se->width * 0.5f;
            ImVec2 A = uvToScreen(mx + nnx * hw, my + nny * hw);
            ImVec2 B = uvToScreen(mx - nnx * hw, my - nny * hw);
            float dx = B.x - A.x, dy = B.y - A.y; float L = std::hypot(dx, dy);
            float ux = L > 1e-6f ? dx / L : 1, uy = L > 1e-6f ? dy / L : 0;
            float nx = -uy, ny = ux;
            float off = (place.x - A.x) * nx + (place.y - A.y) * ny;
            ImVec2 dA(A.x + nx * off, A.y + ny * off), dB(B.x + nx * off, B.y + ny * off);
            const float gap = dimGapPx(), ext = dimExtPx();
            auto extLine = [&](ImVec2 from, ImVec2 to) {
                float ex = to.x - from.x, ey = to.y - from.y; float el = std::hypot(ex, ey);
                if (el < 1e-3f) return; float gx = ex / el, gy = ey / el;
                dl->AddLine(ImVec2(from.x + gx * gap, from.y + gy * gap),
                            ImVec2(to.x + gx * ext, to.y + gy * ext), lineCol, th);
            };
            extLine(A, dA); extLine(B, dB);
            dl->AddLine(dA, dB, lineCol, th);
            arrowHead(dl, dA, -ux, -uy, asz, lineCol);
            arrowHead(dl, dB, ux, uy, asz, lineCol);
            recSeg(dA, dB);
            double v = c.hasMeasured ? c.measured : (c.hasValue ? c.value : (double)se->width);
            std::string wl = fmtLen(c.type == "radius" ? v : v);
            if (c.driven) wl = "(" + wl + ")";
            ImVec2 wts = ImGui::CalcTextSize(wl.c_str());
            ImVec2 tp(place.x + 4.0f * ui::uiScale, place.y - wts.y * 0.5f);
            labelChip(dl, tp, ImVec2(tp.x + wts.x, tp.y + wts.y));
            dl->AddText(tp, textCol, wl.c_str());
            recText(tp, wts);
            return;
        }
    }

    if (c.type == "radius" || c.type == "diameter") {
        const SketchEntity* e = c.refs.empty() ? nullptr : entById(baseId(c.refs[0]));
        if (!e) return;
        ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
        // Leader from the rim toward the placement (radius) or across the circle (diameter).
        float ang = std::atan2(place.y - cc.y, place.x - cc.x);
        ImVec2 rimDir(std::cos(ang), std::sin(ang));
        float rpx = std::hypot(uvToScreen(e->center[0] + e->radius, e->center[1]).x - cc.x,
                               uvToScreen(e->center[0] + e->radius, e->center[1]).y - cc.y);
        ImVec2 rim(cc.x + rimDir.x * rpx, cc.y + rimDir.y * rpx);
        if (c.type == "diameter") {
            ImVec2 rim2(cc.x - rimDir.x * rpx, cc.y - rimDir.y * rpx);
            dl->AddLine(rim2, place, lineCol, th);
            arrowHead(dl, rim2, -rimDir.x, -rimDir.y, asz, lineCol);
            recSeg(rim2, place);
        } else {
            dl->AddLine(cc, place, lineCol, th);
            recSeg(cc, place);
        }
        dl->AddLine(rim, place, lineCol, th);
        arrowHead(dl, rim, rimDir.x, rimDir.y, asz, lineCol);
        recSeg(rim, place);
        ImVec2 tp(place.x + 4.0f * ui::uiScale, place.y - ts.y * 0.5f);
        labelChip(dl, tp, ImVec2(tp.x + ts.x, tp.y + ts.y));
        dl->AddText(tp, textCol, lbl.c_str());
        recText(tp, ts);
        return;
    }

    if (c.type == "angle") {
        if (c.refs.size() < 2) return;
        ImVec2 a1, a2, b1, b2;
        if (!relationLineUV(c.refs[0], a1, a2) || !relationLineUV(c.refs[1], b1, b2)) return;
        a1 = uvToScreen(a1.x, a1.y); a2 = uvToScreen(a2.x, a2.y);
        b1 = uvToScreen(b1.x, b1.y); b2 = uvToScreen(b2.x, b2.y);
        float dax = a2.x - a1.x, day = a2.y - a1.y, dbx = b2.x - b1.x, dby = b2.y - b1.y;
        float den = dax * dby - day * dbx;
        ImVec2 vtx;
        if (std::fabs(den) > 1e-4f) {
            float t = ((b1.x - a1.x) * dby - (b1.y - a1.y) * dbx) / den;
            vtx = ImVec2(a1.x + dax * t, a1.y + day * t);
        } else vtx = ImVec2((a1.x + a2.x + b1.x + b2.x) * 0.25f, (a1.y + a2.y + b1.y + b2.y) * 0.25f);
        float r = std::hypot(place.x - vtx.x, place.y - vtx.y); if (r < 14) r = 14;
        float ang0 = std::atan2(a2.y - a1.y, a2.x - a1.x);
        float ang1 = std::atan2(b2.y - b1.y, b2.x - b1.x);
        // small arc between the two line directions through the placement side
        dl->PathArcTo(vtx, r, ang0, ang1, circleSegs(r) / 2);
        dl->PathStroke(lineCol, 0, th);
        ImVec2 tp(place.x + 4.0f * ui::uiScale, place.y - ts.y * 0.5f);
        labelChip(dl, tp, ImVec2(tp.x + ts.x, tp.y + ts.y));
        dl->AddText(tp, textCol, lbl.c_str());
        recText(tp, ts);
        // Approximate the arc's pickable span by its chord (start->end rim points).
        recSeg(ImVec2(vtx.x + std::cos(ang0) * r, vtx.y + std::sin(ang0) * r),
               ImVec2(vtx.x + std::cos(ang1) * r, vtx.y + std::sin(ang1) * r));
        return;
    }

    // distance (point-point incl h/v, or point-line)
    ImVec2 P0, P1;
    const auto* singleLine = c.refs.size() == 1 ? entById(c.refs[0]) : nullptr;
    if (singleLine && singleLine->type != "line") return;
    const SketchEntity* le = c.refs.size() > 1 ? entById(baseId(c.refs[1])) : nullptr;
    const bool secondIsLine = le && le->type == "line" && c.refs[1].find('.') == std::string::npos;
    if (singleLine) {
        P0 = uvToScreen(singleLine->p1[0], singleLine->p1[1]);
        P1 = uvToScreen(singleLine->p2[0], singleLine->p2[1]);
    } else if (c.refs.empty() || !handlePt(c.refs[0], P0)) return;
    else if (secondIsLine) {
        // perpendicular foot of P0 onto the line
        ImVec2 l1 = uvToScreen(le->p1[0], le->p1[1]), l2 = uvToScreen(le->p2[0], le->p2[1]);
        float vx = l2.x - l1.x, vy = l2.y - l1.y; float L2 = vx * vx + vy * vy;
        float t = L2 > 1e-6f ? ((P0.x - l1.x) * vx + (P0.y - l1.y) * vy) / L2 : 0;
        P1 = ImVec2(l1.x + vx * t, l1.y + vy * t);
    } else if (c.refs.size() < 2 || !handlePt(c.refs[1], P1)) return;

    // For h/v variants, the dimension is measured along the axis: collapse one component.
    ImVec2 A = P0, B = P1;
    if (c.axis == "h")      B.y = A.y;   // horizontal: ignore vertical delta
    else if (c.axis == "v") B.x = A.x;   // vertical

    // Arc conditions: a "min"/"max" circle ref attaches at its rim instead of the centre. A
    // DIRECTIONAL (h/v) dimension attaches at the TANGENT LINE — centre±r along the axis (matches
    // the engine's coordinate-based tangent-line residual; works for concentric/nested circles
    // where the old "toward/away the other centre" shift collapsed to nothing). An ALIGNED
    // dimension uses the near/far rim along the A->B centre line (engine adds ±r to the scalar gap).
    if (!c.cond.empty()) {
        const bool directional = (c.axis == "h" || c.axis == "v");
        if (directional) {
            auto tangent = [&](size_t i, ImVec2& P) {
                if (c.cond.size() <= i || c.refs.size() <= i) return;
                const std::string& cd = c.cond[i];
                if (cd != "min" && cd != "max") return;
                const SketchEntity* e = entById(baseId(c.refs[i]));
                if (!e || (e->type != "circle" && e->type != "arc")) return;
                float s = (cd == "min") ? -1.0f : 1.0f;
                float ru = e->center[0] + (c.axis == "h" ? s * e->radius : 0.0f);
                float rv = e->center[1] + (c.axis == "v" ? s * e->radius : 0.0f);
                P = uvToScreen(ru, rv);
            };
            tangent(0, A); tangent(1, B);
            if (c.axis == "h")      B.y = A.y;   // re-collapse onto the axis after the rim shift
            else if (c.axis == "v") B.x = A.x;
        } else {
            auto condShift = [&](size_t i, ImVec2& P, const ImVec2& other) {
                if (c.cond.size() <= i || c.refs.size() <= i) return;
                const std::string& cd = c.cond[i];
                if (cd != "min" && cd != "max") return;
                const SketchEntity* e = entById(baseId(c.refs[i]));
                if (!e || (e->type != "circle" && e->type != "arc")) return;
                ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
                ImVec2 rimRef = uvToScreen(e->center[0] + e->radius, e->center[1]);
                float rpx = std::hypot(rimRef.x - cc.x, rimRef.y - cc.y);
                float ddx = other.x - P.x, ddy = other.y - P.y;
                float LL = std::hypot(ddx, ddy);
                if (LL < 1e-3f) return;
                float sgn = (cd == "min") ? 1.0f : -1.0f;
                P.x += ddx / LL * rpx * sgn; P.y += ddy / LL * rpx * sgn;
            };
            ImVec2 A0 = A, B0 = B;
            condShift(0, A, B0);
            condShift(1, B, A0);
        }
    }

    // Dimension line passes through the placement point, parallel to A->B; extension lines run from
    // A and B out to that offset line.
    float dx = B.x - A.x, dy = B.y - A.y; float L = std::hypot(dx, dy);
    float ux = L > 1e-6f ? dx / L : 1, uy = L > 1e-6f ? dy / L : 0;
    float nx = -uy, ny = ux;
    // signed offset of the placement from the A->B line
    float off = (place.x - A.x) * nx + (place.y - A.y) * ny;
    ImVec2 dA(A.x + nx * off, A.y + ny * off);
    ImVec2 dB(B.x + nx * off, B.y + ny * off);
    const float gap = dimGapPx(), ext = dimExtPx();   // ext-line gap from geometry / overshoot
    auto extLine = [&](ImVec2 from, ImVec2 to) {
        // shorten near the geometry end by gap, extend past the dim line by ext
        float ex = to.x - from.x, ey = to.y - from.y; float el = std::hypot(ex, ey);
        if (el < 1e-3f) return;
        float gx = ex / el, gy = ey / el;
        dl->AddLine(ImVec2(from.x + gx * gap, from.y + gy * gap),
                    ImVec2(to.x + gx * ext, to.y + gy * ext), lineCol, th);
    };
    extLine(A, dA);
    extLine(B, dB);
    dl->AddLine(dA, dB, lineCol, th);
    arrowHead(dl, dA, -ux, -uy, asz, lineCol);
    arrowHead(dl, dB, ux, uy, asz, lineCol);
    recSeg(dA, dB);
    // text centered on the dim line, nudged to the placement side
    ImVec2 mid((dA.x + dB.x) * 0.5f, (dA.y + dB.y) * 0.5f);
    ImVec2 tp(mid.x - ts.x * 0.5f + nx * 8.0f * ui::uiScale,
              mid.y - ts.y * 0.5f + ny * 8.0f * ui::uiScale);
    labelChip(dl, tp, ImVec2(tp.x + ts.x, tp.y + ts.y));
    dl->AddText(tp, textCol, lbl.c_str());
    recText(tp, ts);
}

// Smart Dimension pick: points (endpoints/centers) by priority within ~8px, then whole entities.
bool SketchMode::dimHitTest(const ImVec2& s, DimPick& out) const {
    // 1) points
    const float pr = 8.0f * ui::uiScale;   // pick radius tracks DPI
    float best = pr * pr; bool got = false;
    auto consider = [&](const std::string& h, float u, float v) {
        ImVec2 p = uvToScreen(u, v);
        float d2 = (p.x - s.x) * (p.x - s.x) + (p.y - s.y) * (p.y - s.y);
        if (d2 < best) { best = d2; out.handle = h; out.isPoint = true; out.entIdx = -1; out.entId.clear(); out.u = u; out.v = v; got = true; }
    };
    consider("origin", 0.0f, 0.0f);   // sketch origin: dimension FROM the origin marker
    for (const auto& e : ents_) {
        if (e.type == "line" || e.type == "slot") {
            consider(e.id + ".p1", e.p1[0], e.p1[1]);
            consider(e.id + ".p2", e.p2[0], e.p2[1]);
        } else if (e.type == "circle" || e.type == "arc" || e.type == "arc_slot") {
            consider(e.id + ".center", e.center[0], e.center[1]);
        } else if (e.type == "point") {
            consider(e.id + ".p", e.p1[0], e.p1[1]);
        }
    }
    if (got) return true;
    // 2) whole entity — C38 (S1): record the id (stable) alongside the per-frame index.
    int ei = hitTestEntity(s, 12.0f);
    if (ei >= 0) {
        const SketchEntity& e = ents_[ei];
        // C50: SolidWorks line-MIDPOINT target. The midpoint is only offered while the line body
        // is hovered (we're inside this whole-entity branch), and only when the cursor is within a
        // comfortable radius of the midpoint — otherwise the click selects the whole line. The
        // midpoint marker itself is drawn by the hover code when a line is hovered.
        if (e.type == "line") {
            float mu = (e.p1[0] + e.p2[0]) * 0.5f, mv = (e.p1[1] + e.p2[1]) * 0.5f;
            ImVec2 mp = uvToScreen(mu, mv);
            if (std::hypot(mp.x - s.x, mp.y - s.y) < 11.0f * ui::uiScale) {
                out.handle = e.id + ".mid"; out.isPoint = true; out.entIdx = -1;
                out.entId.clear(); out.u = mu; out.v = mv; return true;
            }
        }
        out.handle.clear(); out.isPoint = false; out.entIdx = ei;
        out.entId = e.id; out.cond.clear();
        // C51: on a slot, tag whether the cursor hit a rounded CAP (→ width dim) vs the body/
        // centerline (→ length dim). Cap = the cursor's projection onto the spine falls beyond
        // the endpoints.
        if (e.type == "slot") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            float vx = b.x - a.x, vy = b.y - a.y;
            float t = (vx * (s.x - a.x) + vy * (s.y - a.y)) / std::fmax(1e-6f, vx * vx + vy * vy);
            out.cond = (t < 0.0f || t > 1.0f) ? "wid" : "len";
        }
        return true;
    }
    return false;
}

// Build the pending dimension from the current pick buffer + the live cursor. Returns true when the
// picks form a valid combo (and dimPending_/dimType_/dimMeasured_ are filled). cu,cv = cursor uv (for
// the h/v/aligned variant choice on point-point).
bool SketchMode::dimBuildPending(const ImVec2& cursor, float cu, float cv, bool keepAxis) {
    dimHasPending_ = false;
    if (!keepAxis) dimAxis_.clear();
    // C38 (S1): resolve every pick's stored id to a CURRENT entity ptr. If any entity-pick has
    // vanished from ents_ (refilled mid-pick), bail — the combo is no longer valid.
    auto entOf = [&](const DimPick& p) -> const SketchEntity* {
        if (p.isPoint) return nullptr;
        return entById(p.entId);
    };
    for (const auto& p : dimPicks_)
        if (!p.isPoint && !entById(p.entId)) return false;
    auto handleUV = [&](const DimPick& p, float& u, float& v) {
        if (p.isPoint) { u = p.u; v = p.v; return; }
        const SketchEntity& e = *entOf(p);
        u = e.center[0]; v = e.center[1];
    };
    // Circle/arc entity picks act as their CENTER in distance combos, with an optional per-pick
    // arc condition: "min" (near rim / tangent) or "max" (far rim) adds -r/+r to the measured
    // and enforced distance (engine `cond` field). Default is the center (SolidWorks default).
    auto isCirc = [&](const DimPick& p) {
        if (p.isPoint) return false;
        const SketchEntity* e = entOf(p);
        return e && (e->type == "circle" || e->type == "arc");
    };
    auto condOf = [&](const DimPick& p) -> std::string {
        return p.cond.empty() ? std::string("center") : p.cond;
    };
    auto condAdj = [&](const DimPick& p) -> double {
        if (!isCirc(p)) return 0.0;
        std::string c = condOf(p);
        if (c == "min") return -(double)entOf(p)->radius;
        if (c == "max") return  (double)entOf(p)->radius;
        return 0.0;
    };
    auto refOf = [&](const DimPick& p) -> std::string {
        return p.isPoint ? p.handle : p.entId;
    };
    // Attach the cond array when any ref is a circle/arc with a non-center condition.
    auto attachCond = [&](const DimPick& a, const DimPick& b) {
        if ((isCirc(a) && condOf(a) != "center") || (isCirc(b) && condOf(b) != "center"))
            dimPending_["cond"] = nlohmann::json::array(
                {isCirc(a) ? condOf(a) : "center", isCirc(b) ? condOf(b) : "center"});
    };
    size_t n = dimPicks_.size();
    if (n == 1) {
        const DimPick& a = dimPicks_[0];
        if (!a.isPoint) {
            const SketchEntity& e = *entOf(a);
            if (e.type == "line") {                 // length: point-point on endpoints, aligned
                double Lm = std::hypot(e.p2[0] - e.p1[0], e.p2[1] - e.p1[1]);
                dimType_ = "distance"; dimMeasured_ = Lm;
                dimPending_ = {{"type", "distance"},
                    {"refs", nlohmann::json::array({e.id + ".p1", e.id + ".p2"})}};
                dimHasPending_ = true; return true;
            } else if (e.type == "circle") {
                dimType_ = "diameter"; dimMeasured_ = e.radius * 2.0;
                dimPending_ = {{"type", "diameter"}, {"refs", nlohmann::json::array({e.id})}};
                dimHasPending_ = true; return true;
            } else if (e.type == "arc") {
                dimType_ = "radius"; dimMeasured_ = e.radius;
                dimPending_ = {{"type", "radius"}, {"refs", nlohmann::json::array({e.id})}};
                dimHasPending_ = true; return true;
            } else if (e.type == "slot") {
                // C51: a whole-slot pick dimensions either its centerline LENGTH (center-to-center,
                // the default) or its WIDTH (when the cap end was picked — cond "wid", set by
                // dimHitTest). Width is a diameter constraint on the slot's width variable.
                if (a.cond == "wid") {
                    dimType_ = "diameter"; dimMeasured_ = e.width;
                    dimPending_ = {{"type", "diameter"}, {"refs", nlohmann::json::array({e.id})}};
                } else {
                    dimType_ = "distance";
                    dimMeasured_ = std::hypot(e.p2[0] - e.p1[0], e.p2[1] - e.p1[1]);
                    dimPending_ = {{"type", "distance"},
                        {"refs", nlohmann::json::array({e.id + ".p1", e.id + ".p2"})}};
                }
                dimHasPending_ = true; return true;
            }
        }
        return false;   // a single point alone is not a dimension yet
    }
    if (n == 2) {
        const DimPick& a = dimPicks_[0]; const DimPick& b = dimPicks_[1];
        bool aLine = !a.isPoint && entOf(a)->type == "line";
        bool bLine = !b.isPoint && entOf(b)->type == "line";
        // A pick is "point-like" when it's a point handle OR a circle/arc (anchored at center).
        bool aPt = a.isPoint || isCirc(a);
        bool bPt = b.isPoint || isCirc(b);
        // Same-entity guard (e.g. a circle's center handle + the same circle entity): no combo.
        auto base = [](const std::string& h) { return h.substr(0, h.find('.')); };
        if (base(refOf(a)) == base(refOf(b))) return false;
        if (aPt && bPt) {        // point/circle <-> point/circle distance (variant chosen at place)
            float au, av, bu, bv; handleUV(a, au, av); handleUV(b, bu, bv);
            // SolidWorks variant by cursor zone: beyond the vertical span -> horizontal; beyond the
            // horizontal span -> vertical; inside both -> aligned. keepAxis re-runs (Modify-box arc
            // condition change) leave the already-placed axis untouched.
            if (!keepAxis) {
                float ymin = std::fmin(av, bv), ymax = std::fmax(av, bv);
                float xmin = std::fmin(au, bu), xmax = std::fmax(au, bu);
                if (cv < ymin || cv > ymax) dimAxis_ = "h";
                else if (cu < xmin || cu > xmax) dimAxis_ = "v";
                else dimAxis_.clear();
            }
            // h/v is a DIRECTIONAL tangent-line distance: each circle's rim offset attaches to its
            // own centre coordinate (engine _add_distance h/v branch), so a concentric/nested
            // wall thickness is |(au+adjA) - (bu+adjB)| rather than |Δ| - rA - rB (which would
            // shove the circles apart). Aligned keeps the centre-line sum.
            double m;
            if (dimAxis_ == "h")      m = std::fabs((au + condAdj(a)) - (bu + condAdj(b)));
            else if (dimAxis_ == "v") m = std::fabs((av + condAdj(a)) - (bv + condAdj(b)));
            else m = std::hypot(bu - au, bv - av) + condAdj(a) + condAdj(b);
            dimType_ = "distance"; dimMeasured_ = m;
            dimPending_ = {{"type", "distance"},
                           {"refs", nlohmann::json::array({refOf(a), refOf(b)})}};
            attachCond(a, b);
            if (!dimAxis_.empty()) dimPending_["axis"] = dimAxis_;
            dimHasPending_ = true; return true;
        }
        if ((aPt && bLine) || (aLine && bPt)) {   // point/circle <-> line perpendicular distance
            const DimPick& pp = aPt ? a : b;
            const DimPick& ll = aPt ? b : a;
            const SketchEntity& le = *entOf(ll);
            float pu, pv; handleUV(pp, pu, pv);
            // perpendicular foot distance (center-based), plus the arc condition
            float dx = le.p2[0] - le.p1[0], dy = le.p2[1] - le.p1[1]; float L = std::hypot(dx, dy);
            float nx = L > 1e-6f ? -dy / L : 0, ny = L > 1e-6f ? dx / L : 1;
            double m = std::fabs((pu - le.p1[0]) * nx + (pv - le.p1[1]) * ny) + condAdj(pp);
            dimType_ = "distance"; dimMeasured_ = m;
            dimPending_ = {{"type", "distance"},
                           {"refs", nlohmann::json::array({refOf(pp), le.id})}};
            attachCond(pp, ll);
            dimHasPending_ = true; return true;
        }
        if (aLine && bLine) {                      // two lines: parallel -> distance, else angle
            const SketchEntity& la = *entOf(a); const SketchEntity& lb = *entOf(b);
            float ax = la.p2[0] - la.p1[0], ay = la.p2[1] - la.p1[1];
            float bx = lb.p2[0] - lb.p1[0], by = lb.p2[1] - lb.p1[1];
            float cross = ax * by - ay * bx, dot = ax * bx + ay * by;
            float ang = std::atan2(std::fabs(cross), dot) * 57.2957795f;   // [0,180)
            float par = std::fmin(ang, 180.0f - ang);                       // angle from parallel
            if (par < 2.0f) {                        // nearly parallel -> point-line distance
                float L = std::hypot(bx, by);
                float nx = L > 1e-6f ? -by / L : 0, ny = L > 1e-6f ? bx / L : 1;
                double m = std::fabs((la.p1[0] - lb.p1[0]) * nx + (la.p1[1] - lb.p1[1]) * ny);
                dimType_ = "distance"; dimMeasured_ = m;
                dimPending_ = {{"type", "distance"}, {"refs", nlohmann::json::array({la.id + ".p1", lb.id})}};
                dimHasPending_ = true; return true;
            } else {                                 // angle between the two lines
                dimType_ = "angle"; dimMeasured_ = ang;
                dimPending_ = {{"type", "angle"}, {"refs", nlohmann::json::array({la.id, lb.id})}};
                dimHasPending_ = true; return true;
            }
        }
    }
    return false;
}

void SketchMode::dimOpenModify(const ImVec2& at) {
    dimError_.clear();
    dimPopupOpen_ = true;
    dimPopupFocus_ = true;
    dimPopupPos_ = at;
    dimIsAngle_ = (dimType_ == "angle");
    dimPopupStep_ = dimIsAngle_ ? 1.0 : 1.0;
    // C38 (S5): when there's no real measured value, leave the buffer EMPTY so a stray Enter can't
    // silently commit a fake "0" — the user must type a value (pmParseDim rejects empty). When there
    // IS a value, seed the plain number; for angles dimMeasured_ is already in DEGREES (set by
    // dimBuildPending), so fmtLen yields the correct degree number — the "°" unit is shown as the box
    // suffix label, NOT baked into the buffer (a trailing "°" would make pmParseDim reject the value).
    if (std::isfinite(dimMeasured_) && std::fabs(dimMeasured_) > 1e-9)
        std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
    else
        dimBuf_[0] = '\0';
}

// C50f: apply a sketch_set_dim op (the shared EDIT-commit body), reload the mesh, re-solve, and
// tear down the Modify box. Used by the dimCommit edit path and by "Apply anyway" on the edit
// over-define modal.
void SketchMode::applyDimEditOp(AppState& st, const nlohmann::json& op) {
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) { st.toast(ui::ToastLevel::Error, "Dimension: " + r.error_message); dimError_ = r.error_message; return; }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);
    // C38 (S3): full pick-buffer cleanup. A double-click edit leaves stale picks otherwise, which
    // dimBuildPending consumes on the next frame.
    dimPopupOpen_ = false; dimEditCid_.clear();
    dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
    dimAxis_.clear(); dimPopupDriven_ = false;
}

// Commit the Modify box: parse value/expr, apply, and (on success) keep the tool armed.
void SketchMode::dimCommit(AppState& st) {
    std::string txt = dimBuf_.data();
    nlohmann::json con = dimPending_;
    bool isExpr = (!txt.empty() && txt[0] == '=');
    double mm = dimMeasured_;
    if (!isExpr) {
        std::string perr;
        // A bare value in the Modify box follows the document length unit (mm / inch); angles
        // stay degrees. An explicit suffix the user types still wins via pmParseDim.
        const double bareScale = (g_skIps && !dimIsAngle_) ? 25.4 : 1.0;
        if (!pmParseDim(txt, mm, perr, dimIsAngle_, bareScale)) { dimError_ = perr.empty() ? "bad value" : perr; return; }
    }
    if (!dimEditCid_.empty()) {
        // Editing an EXISTING dim: sketch_set_dim. C50f: pre-flight the new value with sketch_probe
        // so a value that contradicts other relations is caught BEFORE it over-defines the sketch
        // (the user's main bug — the edit path had zero over-define handling). Driven dims can't
        // over-define (they're references), so they skip the probe.
        nlohmann::json op = {{"op", "sketch_set_dim"}, {"sketch", fid_}, {"id", dimEditCid_}};
        if (isExpr) op["expr"] = txt.substr(1); else op["value"] = mm;
        bool editDriven = false;
        for (const auto& c : cons_) if (c.id == dimEditCid_) { editDriven = c.driven; break; }
        if (!editDriven) {
            nlohmann::json setAction = {{"id", dimEditCid_}};
            if (isExpr) { setAction["expr"] = txt.substr(1); setAction["value"] = mm; }
            else        setAction["value"] = mm;
            bool wouldOver = false; std::vector<std::string> pc, pr;
            if (sketchProbe(st, nlohmann::json{{"set_dim", setAction}}, wouldOver, pc, pr) && wouldOver) {
                // Stash the op; the modal's "Apply anyway" applies it, "Cancel" keeps the old value.
                editOverDefOp_ = op;
                editOverDefOpen_ = true;
                editOverDefPos_ = dimPopupPos_;
                dimPopupOpen_ = false;   // hide the Modify box behind the modal
                return;
            }
            // probe failure (RPC/transport) falls through and commits normally — never block.
        }
        applyDimEditOp(st, op);
        return;
    }
    // New dim: attach value/expr + placement, then (after the over-define pre-flight) driven.
    con["value"] = isExpr ? nlohmann::json{{"value", dimMeasured_}, {"expr", txt.substr(1)}}
                          : nlohmann::json{{"value", mm}, {"expr", nullptr}};
    con["placement"] = nlohmann::json::array({dimPlaceU_, dimPlaceV_});
    // C50f: SolidWorks over-define pre-flight. Probe the add BEFORE committing it. If the user
    // already ticked "Driven" in the Modify box, skip the probe/dialog and just commit driven
    // (a reference dim can't over-define). Otherwise probe; if would_over_define, open the
    // three-choice dialog. A probe failure falls through to a normal driving commit (never block).
    if (!dimPopupDriven_) {
        bool wouldOver = false; std::vector<std::string> pc, pr;
        if (sketchProbe(st, nlohmann::json{{"add_constraint", con}}, wouldOver, pc, pr) && wouldOver) {
            overDefPendingCon_ = con;      // commit happens on the dialog button (driving|driven)
            overDefOpen_ = true;
            overDefPos_ = dimPopupPos_;
            dimPopupOpen_ = false;         // hide the Modify box behind the dialog
            return;
        }
    }
    if (dimPopupDriven_) con["driven"] = true;
    // probe said fine (or the Driven box was ticked) -> commit con as-is. con already carries
    // "driven" when ticked, so pass driven=false (overDefCommit only ADDS driven, never removes it).
    overDefPendingCon_ = con;
    overDefCommit(st, /*driven=*/false);
}

// C50f: commit the pending new-dimension constraint (overDefPendingCon_ — already carries
// value/expr/placement). driven=true converts it to a driven (reference) dim before adding. Shared
// by dimCommit's "fine" path and the over-define dialog's Make Driven / Keep Driving buttons. After
// applying it re-solves and tears down the Smart Dimension pick buffer (tool stays armed).
void SketchMode::overDefCommit(AppState& st, bool driven) {
    nlohmann::json con = overDefPendingCon_;
    if (driven) con["driven"] = true;
    nlohmann::json ops = nlohmann::json::array({
        {{"op", "sketch_add_constraint"}, {"sketch", fid_}, {"constraint", con}}});
    auto r = st.docMirror.applyOps(st.worker, ops);
    if (!r.ok) {
        // Over-define protection: surface the engine error, never silently drop.
        st.toast(ui::ToastLevel::Error, "Dimension: " + r.error_message);
        dimError_ = r.error_message;
        return;
    }
    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    resolve(st);   // refreshes conflicting_ + redundant_ + status_ + dof_ + solveRejected_
    // Stay in Smart Dimension; clear the pick buffer for the next dimension.
    overDefPendingCon_ = nullptr;
    dimPopupOpen_ = false; dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
    dimAxis_.clear(); dimPopupDriven_ = false;
}

// C50f: the SolidWorks over-define pre-flight dialog for a NEW dimension. sketch_probe flagged the
// pending add (overDefPendingCon_) as over-defining BEFORE anything was committed. Three choices,
// matching SolidWorks' "Make Dimension Driven?" prompt: Make Driven (default/highlighted) commits
// the dim as a reference dim; Leave Driving commits it as a driving dim (sketch goes over-defined);
// Cancel commits nothing. The chosen button drives overDefCommit; nothing has touched the doc yet.
void SketchMode::drawOverDefinePrompt(AppState& st) {
    if (!overDefOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(overDefPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##OverDefine", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
        ImGui::TextUnformatted("Make Dimension Driven?");
        ImGui::PopStyleColor();
        ImGui::TextUnformatted("Adding this dimension makes the sketch over defined.");
        ImGui::Spacing();
        auto finish = [&]() { overDefOpen_ = false; overDefPendingCon_ = nullptr;
                              dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                              dimAxis_.clear(); dimPopupDriven_ = false; };
        // Default action (Make this dimension driven) — commits a reference dimension. The
        // recommended choice wears the app's ONE primary-button treatment.
        if (ui::kit::primaryButton("Make this dimension driven")) {
            overDefCommit(st, /*driven=*/true);
            overDefOpen_ = false;
        }
        ImGui::SameLine();
        if (ImGui::Button("Leave this dimension driving")) {
            // Commit as a driving dim; the sketch becomes over defined (reads red, frozen geometry).
            overDefCommit(st, /*driven=*/false);
            overDefOpen_ = false;
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) {
            // Commit nothing — the dimension input is discarded entirely.
            finish();
        }
    }
    ImGui::End();
}

// C50f: over-define pre-flight dialog for a dimension EDIT. sketch_probe found the new value
// contradicts other relations. Cancel (default) keeps the old value and sends nothing; Apply anyway
// pushes the stashed set_dim op (the sketch goes over-defined with frozen geometry).
void SketchMode::drawEditOverDefinePrompt(AppState& st) {
    if (!editOverDefOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(editOverDefPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##EditOverDefine", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
        ImGui::TextUnformatted("Over Defined");
        ImGui::PopStyleColor();
        ImGui::TextUnformatted("This value over-defines the sketch \xE2\x80\x94 the geometry");
        ImGui::TextUnformatted("cannot satisfy all relations.");
        ImGui::Spacing();
        auto finish = [&]() { editOverDefOpen_ = false; editOverDefOp_ = nullptr;
                              dimEditCid_.clear();
                              dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
                              dimAxis_.clear(); dimPopupDriven_ = false; };
        // Default action (Cancel) — keep the old value, send nothing. Highlighted as recommended.
        if (ui::kit::primaryButton("Cancel")) finish();
        ImGui::SameLine();
        if (ImGui::Button("Apply anyway")) {
            nlohmann::json op = editOverDefOp_;
            editOverDefOpen_ = false; editOverDefOp_ = nullptr;
            applyDimEditOp(st, op);   // sends it; sketch goes over-defined with frozen geometry
        }
    }
    ImGui::End();
}

void SketchMode::drawRelationOverDefinePrompt(AppState& st) {
    if (!relOverDefOpen_) return;
    const auto& pal = ui::palette;
    ImGui::SetNextWindowPos(relOverDefPos_, ImGuiCond_Appearing);
    ImGui::SetNextWindowBgAlpha(0.98f);
    if (ImGui::Begin("##RelationOverDefine", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
        ImGui::TextUnformatted("Over Defined");
        ImGui::PopStyleColor();
        ImGui::TextUnformatted("Adding this relation makes the sketch over defined.");
        ImGui::Spacing();
        auto finish = [&]() {
            relOverDefOpen_ = false;
            relOverDefOps_ = nullptr;
            relOverDefLabel_.clear();
        };
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.windowBg[0], pal.windowBg[1], pal.windowBg[2], 1));
        bool cancel = ImGui::Button("Cancel");
        ImGui::PopStyleColor(4);
        if (cancel) finish();
        ImGui::SameLine();
        if (ImGui::Button("Add anyway")) {
            nlohmann::json ops = relOverDefOps_;
            std::string label = relOverDefLabel_.empty() ? "Relation" : relOverDefLabel_;
            finish();
            applyRelationOps(st, ops, label);
        }
    }
    ImGui::End();
}

// C37-D: in-sketch Ctrl+Z. Mid-stroke -> pop the last placed point. Else doc undo + re-solve; if the
// sketch vanished, exit gracefully.
void SketchMode::inSketchUndo(AppState& st) {
    if (tool_ != SketchTool::None && tool_ != SketchTool::SmartDim && !toolUV_.empty()) {
        toolUV_.pop_back();
        if (!toolSnaps_.empty()) toolSnaps_.pop_back();
        return;
    }
    st.docUndo();
    afterSketchHistory(st);
}

void SketchMode::inSketchRedo(AppState& st) {
    st.docRedo();
    afterSketchHistory(st);
}

// Shared post-undo/redo refresh: resync the mirror, re-solve the active sketch, or close cleanly
// if the sketch feature itself was undone/redone away.
void SketchMode::afterSketchHistory(AppState& st) {
    resyncMirror(st);
    // Does the sketch still exist? Re-solving an absent sketch returns an error envelope.
    nlohmann::json res;
    if (!callSketch(st, "sketch_solve", nlohmann::json::object(), res)) {
        st.toast(ui::ToastLevel::Info, "Sketch removed by undo — closing");
        // The sketch feature is gone; drop out cleanly without re-exiting the worker sketch.
        active_ = false; isNewEmpty_ = false; fid_.clear();
        ents_.clear(); cons_.clear(); autoPlace_.clear(); projRefs_.clear(); cancelCheckpoint_.clear();
        status_.clear(); dof_ = 0;
        conflicting_.clear(); redundant_.clear(); solveRejected_ = false;   // C50f
        relOverDefOpen_ = false; relOverDefOps_ = nullptr; relOverDefLabel_.clear();   // C50g
        // C38 (S2): clear plane-pick state in the emergency teardown too.
        planePicking_ = false; planeHover_ = -1; st.sketchPlanePickActive = false;
        resetTool();
        if (havePrevCam_) { requestCameraAnimation(st, prevYaw_, prevPitch_, prevZoom_, prevPan_); havePrevCam_ = false; }
        return;
    }
    applyEnterResult(res);
}

// ---------------------------------------------------------------------------
// C40: ghost rendering of inactive sketches
// ---------------------------------------------------------------------------

void SketchMode::refreshGhosts(AppState& st) {
    const std::string docId = st.docMirror.docId();
    const int rev = st.docMirror.revision();
    // C48: the in-flight create-mode PM preview (Extrude/Revolve...) consumes its profile
    // sketch in the doc, but the curves must STAY visible until commit (SolidWorks shows the
    // profile through the preview). A change of the exempt id also forces a refresh.
    std::string exemptFid;
    if (st.propertyManager) exemptFid = st.propertyManager->previewFeatureId();
    if (docId == ghostsDocId_ && rev == ghostsRevision_ && exemptFid == ghostsExemptFid_) return;
    ghostsDocId_ = docId;
    ghostsRevision_ = rev;
    ghostsExemptFid_ = exemptFid;
    ++ghostsGen_;
    // C48: keep already-loaded snapshots across the refresh — during PM preview churn the
    // revision bumps faster than the async overview fetch completes, and clearing here made
    // every unconsumed sketch flicker out (or vanish for the whole preview session). Stale
    // coords draw until the fresh fetch lands.
    std::vector<GhostSketch> old;
    old.swap(ghosts_);
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object() || !tree.contains("features") || !tree["features"].is_array()) return;
    const auto& feats = tree["features"];
    // Per-sketch consumer scan: a sketch is consumed when a DIFFERENT feature builds from it
    // (core::consumedSketches — profile lists, region selectors, *_sketch/path/sections keys;
    // the same rule the FeatureManager uses to nest it) AND that consumer is actually built —
    // sketches referenced by an errored or rolled-back feature still ghost (the user needs to
    // see them to fix things), as does the PM's exempt preview feature.
    const nlohmann::json& statusArr = st.docMirror.featureStatus();
    const std::unordered_set<std::string> sketchIds = core::sketchIdsOf(feats);
    std::unordered_set<std::string> consumed;
    for (const auto& f : feats) {
        if (!f.is_object()) continue;
        const std::string cid = f.value("id", std::string());
        if (cid.empty() || cid == exemptFid) continue;
        std::unordered_set<std::string> used = core::consumedSketches(f, sketchIds);
        if (used.empty()) continue;
        // Consumer found — does it actually build? (missing status record = assume ok)
        bool ok = true, rolledBack = false;
        if (statusArr.is_array()) {
            for (const auto& s : statusArr) {
                if (!s.is_object() || s.value("id", std::string()) != cid) continue;
                ok = s.value("ok", true);
                rolledBack = s.value("rolled_back", false);
                break;
            }
        }
        if (ok && !rolledBack) consumed.insert(used.begin(), used.end());
    }
    auto consumerCounts = [&](const std::string& fid) -> bool { return consumed.count(fid) > 0; };
    for (const auto& f : feats) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
        if (f.value("suppressed", false)) continue;
        const std::string fid = f.value("id", std::string());
        if (fid.empty()) continue;
        if (!f.contains("params") || !f["params"].is_object()) continue;
        const auto& ents = f["params"].value("entities", nlohmann::json::array());
        if (!ents.is_array() || ents.empty()) continue;
        if (consumerCounts(fid)) continue;
        ghosts_.push_back(GhostSketch{});
        ghosts_.back().fid = fid;
        // Carry over the previous snapshot (stale-but-visible) while the refresh is in flight.
        for (auto& o : old) {
            if (o.fid != fid || !o.loaded) continue;
            std::memcpy(ghosts_.back().frame, o.frame, sizeof(o.frame));
            ghosts_.back().ents = std::move(o.ents);
            ghosts_.back().loaded = true;
            break;
        }
        // Fetch the solved coords + plane frame asynchronously; the callback runs on the GL
        // thread (AsyncRpc::drain) and is dropped if the doc moved on (gen guard) — harmless
        // now, since the carried-over snapshot keeps drawing until a fresh fetch lands.
        const int gen = ghostsGen_;
        st.rpc.call("doc_sketch_overview", {{"doc_id", docId}, {"sketch", fid}},
                    [this, gen, fid](const core::RpcResult& r) {
                        if (gen != ghostsGen_) return;
                        if (!r.ok || !r.result.is_object() || r.result.contains("code")) return;
                        for (auto& g : ghosts_) {
                            if (g.fid != fid) continue;
                            if (r.result.contains("plane_frame") &&
                                r.result["plane_frame"].is_array() &&
                                r.result["plane_frame"].size() == 16)
                                for (int i = 0; i < 16; ++i)
                                    g.frame[i] = r.result["plane_frame"][i].get<float>();
                            parseEntities(r.result, g.ents);
                            g.loaded = true;
                        }
                    });
    }
}

void SketchMode::drawGhosts(AppState& st, const ImVec2& imgMin, const ImVec2& imgMax,
                            int vw, int vh) {
    if (!st.renderer || !st.workerStarted || !st.docMirror.hasDoc()) {
        if (!ghosts_.empty()) { ghosts_.clear(); ghostsDocId_.clear(); ghostsRevision_ = -999; }
        return;
    }
    refreshGhosts(st);
    if (ghosts_.empty()) return;
    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = ui::palette;
    for (const auto& g : ghosts_) {
        if (!g.loaded) continue;
        if (active_ && g.fid == fid_) continue;   // the live overlay owns the active sketch
        if (st.hiddenFeatures.count(g.fid)) continue;   // FeatureManager eye toggled this sketch off
        // Project sketch (u,v) through this ghost's own plane frame (no shared basis).
        auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
            float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
            float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
            float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
            return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
        };
        auto poly = [&](auto pointAt, int seg) {     // sample pointAt(t) for t in [0,1]
            ImVec2 prev; bool havePrev = false;
            for (int i = 0; i <= seg; ++i) {
                float u, v; pointAt(i / (float)seg, u, v);
                ImVec2 p;
                if (!uv2s(u, v, p)) { havePrev = false; continue; }
                if (havePrev)
                    dl->AddLine(prev, p, col(pal.sketchInk, g.fid == fid_ ? 0.9f : 0.55f),
                                1.5f * ui::uiScale);
                prev = p; havePrev = true;
            }
        };
        const float D2R = 3.14159265f / 180.0f;
        for (const auto& e : g.ents) {
            if (e.construction) continue;   // ghost view: skip construction geometry
            if (e.type == "line") {
                poly([&](float t, float& u, float& v) {
                    u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                    v = e.p1[1] + (e.p2[1] - e.p1[1]) * t;
                }, 1);
            } else if (e.type == "slot") {
                // Closed capsule: two side lines (tangent, offset by half-width) + two end-cap
                // half-circles — the same profile the engine extrudes.
                float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
                float L = std::hypot(dx, dy);
                float hw = e.width * 0.5f;
                float nx = L > 1e-6f ? -dy / L : 0.0f, ny = L > 1e-6f ? dx / L : 0.0f;
                for (float s : {1.0f, -1.0f})
                    poly([&](float t, float& u, float& v) {
                        u = (e.p1[0] + nx * hw * s) + (e.p2[0] - e.p1[0]) * t;
                        v = (e.p1[1] + ny * hw * s) + (e.p2[1] - e.p1[1]) * t;
                    }, 1);
                if (hw > 0.0f) {
                    float ax1 = std::atan2(-dy, -dx), ax2 = std::atan2(dy, dx);
                    for (auto pr : {std::make_pair(e.p1, ax1), std::make_pair(e.p2, ax2)}) {
                        const float* c = pr.first; float axis = pr.second;
                        poly([&](float t, float& u, float& v) {
                            float a = axis - 1.5707963f + 3.14159265f * t;
                            u = c[0] + hw * std::cos(a);
                            v = c[1] + hw * std::sin(a);
                        }, 24);
                    }
                }
            } else if (e.type == "circle") {
                poly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 48);
            } else if (e.type == "arc" || e.type == "arc_slot") {
                float a0 = e.start, a1 = e.end;
                if (e.type == "arc" && !e.ccw) { float t = a0; a0 = a1; a1 = t; }
                if (a1 < a0) a1 += 360.0f;
                auto arcAt = [&](float rad, float t, float& u, float& v) {
                    float a = (a0 + (a1 - a0) * t) * D2R;
                    u = e.center[0] + rad * std::cos(a);
                    v = e.center[1] + rad * std::sin(a);
                };
                if (e.type == "arc") {
                    poly([&](float t, float& u, float& v) { arcAt(e.radius, t, u, v); }, 40);
                } else {
                    float hw = e.width * 0.5f;
                    poly([&](float t, float& u, float& v) { arcAt(e.radius + hw, t, u, v); }, 40);
                    poly([&](float t, float& u, float& v) { arcAt(e.radius - hw, t, u, v); }, 40);
                    for (float aDeg : {e.start, e.end}) {
                        float sa = aDeg * D2R;
                        float cx = e.center[0] + e.radius * std::cos(sa);
                        float cy = e.center[1] + e.radius * std::sin(sa);
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            u = cx + hw * std::cos(a);
                            v = cy + hw * std::sin(a);
                        }, 24);
                    }
                }
            } else if (e.type == "ellipse") {
                // C42: rotated parametric sample (rot in degrees).
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                poly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    u = e.center[0] + lx * ca - ly * sa;
                    v = e.center[1] + lx * sa + ly * ca;
                }, 48);
            } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                // C42/M-1: a "spline" ghosts its SMOOTH curve (Catmull-Rom through-points) so it
                // doesn't read as sharp-cornered; a "style_spline" ghosts its control polygon.
                const std::vector<ImVec2>& gp =
                    (e.type == "spline") ? catmullRomUV(e.pts) : e.pts;
                for (size_t i = 1; i < gp.size(); ++i) {
                    ImVec2 a, b;
                    if (uv2s(gp[i - 1].x, gp[i - 1].y, a) && uv2s(gp[i].x, gp[i].y, b))
                        dl->AddLine(a, b, col(pal.sketchInk, 0.55f), 1.5f * ui::uiScale);
                }
            } else if (e.type == "point") {
                // C42: small ghost cross.
                ImVec2 p;
                if (uv2s(e.p1[0], e.p1[1], p)) {
                    const ImU32 gc = col(pal.sketchInk, 0.55f);
                    const float gx = 4.0f * ui::uiScale, gw = 1.5f * ui::uiScale;
                    dl->AddLine(ImVec2(p.x - gx, p.y), ImVec2(p.x + gx, p.y), gc, gw);
                    dl->AddLine(ImVec2(p.x, p.y - gx), ImVec2(p.x, p.y + gx), gc, gw);
                }
            }
            // conic-family ghosts: not yet sampled (rare as standalone profiles).
        }
    }

    // --- persistent selected-path highlight (sweep / loft) ----------------------
    // Keep the chosen Path + Guides geometry boldly highlighted while the form is open,
    // even when the live preview is failing — so the user always sees what's selected.
    if (auto* pm = st.propertyManager; pm && pm->active()) {
        std::set<std::string> hi;
        for (const auto& id : pm->pathHighlightFeatureIds()) hi.insert(id);
        if (!hi.empty()) {
            const ImU32 hc = col(pal.accent, 1.0f);
            const float D2R = 3.14159265f / 180.0f;
            // (a) sketch paths/guides: redraw the matching ghost sketches bold.
            for (const auto& g : ghosts_) {
                if (!g.loaded || !hi.count(g.fid)) continue;
                auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
                    float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
                    float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
                    float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
                    return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
                };
                auto poly = [&](auto pointAt, int seg) {
                    ImVec2 prev; bool have = false;
                    for (int i = 0; i <= seg; ++i) {
                        float u, v; pointAt(i / (float)seg, u, v);
                        ImVec2 p;
                        if (!uv2s(u, v, p)) { have = false; continue; }
                        if (have) dl->AddLine(prev, p, hc, 2.75f * ui::uiScale);
                        prev = p; have = true;
                    }
                };
                for (const auto& e : g.ents) {
                    if (e.construction) continue;
                    if (e.type == "line" || e.type == "slot" || e.type == "arc_slot") {
                        poly([&](float t, float& u, float& v) {
                            u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                            v = e.p1[1] + (e.p2[1] - e.p1[1]) * t; }, 1);
                    } else if (e.type == "circle") {
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            u = e.center[0] + e.radius * std::cos(a);
                            v = e.center[1] + e.radius * std::sin(a); }, 48);
                    } else if (e.type == "arc") {
                        float a0 = e.start, a1 = e.end;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 360.0f;
                        poly([&](float t, float& u, float& v) {
                            float a = (a0 + (a1 - a0) * t) * D2R;
                            u = e.center[0] + e.radius * std::cos(a);
                            v = e.center[1] + e.radius * std::sin(a); }, 40);
                    } else if (e.type == "ellipse") {
                        float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                        poly([&](float t, float& u, float& v) {
                            float a = t * 6.2831853f;
                            float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                            u = e.center[0] + lx * ca - ly * sa;
                            v = e.center[1] + lx * sa + ly * ca; }, 48);
                    } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                        for (size_t i = 1; i < e.pts.size(); ++i) {
                            ImVec2 a, b;
                            if (uv2s(e.pts[i - 1].x, e.pts[i - 1].y, a) && uv2s(e.pts[i].x, e.pts[i].y, b))
                                dl->AddLine(a, b, hc, 2.75f * ui::uiScale);
                        }
                    }
                }
            }
            // (b) curve paths (helix/spiral): draw the matching curve datum bold.
            const nlohmann::json& datums = st.docMirror.refDatums();
            if (datums.is_array()) {
                for (const auto& d : datums) {
                    if (!d.is_object() || d.value("kind", std::string()) != "curve") continue;
                    if (!hi.count(d.value("id", std::string()))) continue;
                    const auto& pts = d.contains("points") ? d["points"] : nlohmann::json();
                    if (!pts.is_array()) continue;
                    ImVec2 prev; bool have = false;
                    for (const auto& p : pts) {
                        ImVec2 s; bool ok = p.is_array() && p.size() >= 3 &&
                            worldToScreen(cam, imgMin, vw, vh, (float)p[0].get<double>(),
                                          (float)p[1].get<double>(), (float)p[2].get<double>(), s);
                        if (!ok) { have = false; continue; }
                        if (have) dl->AddLine(prev, s, hc, 2.75f * ui::uiScale);
                        prev = s; have = true;
                    }
                }
            }
        }
    }
    dl->PopClipRect();
}

// ---------------------------------------------------------------------------
// C51: feature-PM region picker — hover-highlight + click-to-toggle profile regions
// ---------------------------------------------------------------------------

void SketchMode::refreshRegionPicker(AppState& st) {
    const std::string docId = st.docMirror.docId();
    const int rev = st.docMirror.revision();
    auto* pm = st.propertyManager;
    std::vector<std::string> cands = pm ? pm->regionCandidateSketches(st)
                                        : std::vector<std::string>();
    std::string key;
    for (const auto& c : cands) { key += c; key += ','; }
    if (docId == pickDocId_ && rev == pickRevision_ && key == pickCandidatesKey_) return;
    pickDocId_ = docId; pickRevision_ = rev; pickCandidatesKey_ = key;
    ++pickGen_;
    // Carry stale snapshots across the refresh (avoid flicker during PM preview revision churn).
    std::vector<RegionSketch> old; old.swap(pickRegions_);
    for (const auto& fid : cands) {
        pickRegions_.push_back(RegionSketch{});
        pickRegions_.back().fid = fid;
        for (auto& o : old) {
            if (o.fid != fid || !o.loaded) continue;
            std::memcpy(pickRegions_.back().frame, o.frame, sizeof(o.frame));
            pickRegions_.back().regions = std::move(o.regions);
            pickRegions_.back().loaded = true;
            break;
        }
        const int gen = pickGen_;
        st.rpc.call("doc_sketch_regions", {{"doc_id", docId}, {"sketch", fid}},
            [this, gen, fid](const core::RpcResult& r) {
                if (gen != pickGen_) return;
                if (!r.ok || !r.result.is_object() || r.result.contains("code")) return;
                for (auto& rs : pickRegions_) {
                    if (rs.fid != fid) continue;
                    if (r.result.contains("plane_frame") && r.result["plane_frame"].is_array() &&
                        r.result["plane_frame"].size() == 16)
                        for (int i = 0; i < 16; ++i)
                            rs.frame[i] = r.result["plane_frame"][i].get<float>();
                    rs.regions.clear();
                    if (r.result.contains("regions") && r.result["regions"].is_array()) {
                        for (const auto& jr : r.result["regions"]) {
                            PickRegion pr;
                            pr.index = jr.value("index", 0);
                            pr.isMaterial = jr.value("is_material", true);
                            if (jr.contains("centroid") && jr["centroid"].is_array() &&
                                jr["centroid"].size() == 2) {
                                pr.centroid[0] = jr["centroid"][0].get<float>();
                                pr.centroid[1] = jr["centroid"][1].get<float>();
                            }
                            auto flat2 = [](const nlohmann::json& arr, std::vector<float>& out) {
                                if (!arr.is_array()) return;
                                for (const auto& p : arr)
                                    if (p.is_array() && p.size() == 2) {
                                        out.push_back(p[0].get<float>());
                                        out.push_back(p[1].get<float>());
                                    }
                            };
                            if (jr.contains("outer")) flat2(jr["outer"], pr.outer);
                            if (jr.contains("holes") && jr["holes"].is_array())
                                for (const auto& h : jr["holes"]) {
                                    pr.holes.emplace_back();
                                    flat2(h, pr.holes.back());
                                }
                            if (jr.contains("tris") && jr["tris"].is_array())
                                for (const auto& t : jr["tris"])
                                    if (t.is_array() && t.size() == 3)
                                        for (const auto& p : t)
                                            if (p.is_array() && p.size() == 2) {
                                                pr.tris.push_back(p[0].get<float>());
                                                pr.tris.push_back(p[1].get<float>());
                                            }
                            rs.regions.push_back(std::move(pr));
                        }
                    }
                    rs.loaded = true;
                }
            });
    }
}

// Screen-space point-in-polygon over a flat u,v list already projected to screen pts.
static bool pmPtInScreenPoly(const std::vector<ImVec2>& poly, const ImVec2& p) {
    bool in = false;
    size_t n = poly.size();
    if (n < 3) return false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        const ImVec2& a = poly[i];
        const ImVec2& b = poly[j];
        if (((a.y > p.y) != (b.y > p.y)) &&
            (p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y + 1e-9f) + a.x))
            in = !in;
    }
    return in;
}

bool SketchMode::drawRegionPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                  const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                 // editing a sketch: the overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted || !st.docMirror.hasDoc() ||
        !pm || !pm->active() || !pm->wantsRegionPicker()) {
        if (!pickRegions_.empty()) { pickRegions_.clear(); pickCandidatesKey_.clear(); }
        return false;
    }
    refreshRegionPicker(st);
    if (pickRegions_.empty()) return false;

    const auto& cam = st.renderer->camera();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = ui::palette;
    const bool ordered = pm->sectionsAreOrdered();

    // Parse the current profile/sections selection.
    std::vector<nlohmann::json> picks = pm->profilePicks();
    auto isWhole = [&](const std::string& fid) -> bool {
        for (const auto& s : picks)
            if (s.value("kind", std::string()) == "region" &&
                s.value("sketch", std::string()) == fid && !s.contains("index"))
                return true;
        return false;
    };
    auto explicitHas = [&](const std::string& fid, int idx) -> bool {
        for (const auto& s : picks)
            if (s.value("kind", std::string()) == "region" &&
                s.value("sketch", std::string()) == fid && s.contains("index") &&
                s["index"].is_number() && (int)s["index"].get<double>() == idx)
                return true;
        return false;
    };
    auto isSelected = [&](const std::string& fid, const PickRegion& pr) -> bool {
        if (explicitHas(fid, pr.index)) return true;
        return isWhole(fid) && pr.isMaterial;   // whole sketch = all material regions
    };

    // Project a region's outer + hole polylines for the given sketch frame.
    auto projectPoly = [&](const float frame[16], const std::vector<float>& uv,
                           std::vector<ImVec2>& out) {
        out.clear();
        for (size_t i = 0; i + 1 < uv.size(); i += 2) {
            float u = uv[i], v = uv[i + 1];
            float wx = frame[0] * u + frame[1] * v + frame[3];
            float wy = frame[4] * u + frame[5] * v + frame[7];
            float wz = frame[8] * u + frame[9] * v + frame[11];
            ImVec2 p;
            if (worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, p)) out.push_back(p);
        }
    };

    // ---- hover detection (screen space, even-odd hole aware) ----
    int hovS = -1, hovR = -1;
    float hovArea = 1e30f;
    const ImVec2 mouse = io.MousePos;
    const bool canHover = imgHovered && mouse.x >= imgMin.x && mouse.x <= imgMax.x &&
                          mouse.y >= imgMin.y && mouse.y <= imgMax.y;
    std::vector<ImVec2> outerS, holeS;
    if (canHover) {
        for (size_t si = 0; si < pickRegions_.size(); ++si) {
            const auto& rs = pickRegions_[si];
            if (!rs.loaded) continue;
            for (size_t ri = 0; ri < rs.regions.size(); ++ri) {
                const auto& pr = rs.regions[ri];
                projectPoly(rs.frame, pr.outer, outerS);
                if (!pmPtInScreenPoly(outerS, mouse)) continue;
                bool inHole = false;
                for (const auto& h : pr.holes) {
                    projectPoly(rs.frame, h, holeS);
                    if (pmPtInScreenPoly(holeS, mouse)) { inHole = true; break; }
                }
                if (inHole) continue;
                // Screen-area tie-break: prefer the smallest region under the cursor.
                float a = 0.0f;
                for (size_t k = 0, j = outerS.size() - 1; k < outerS.size(); j = k++)
                    a += (outerS[j].x + outerS[k].x) * (outerS[j].y - outerS[k].y);
                a = std::fabs(a) * 0.5f;
                if (a < hovArea) { hovArea = a; hovS = (int)si; hovR = (int)ri; }
            }
        }
    }

    // ---- draw: fills (selected/hover) + outlines ----
    std::vector<ImVec2> tri(3);
    for (size_t si = 0; si < pickRegions_.size(); ++si) {
        const auto& rs = pickRegions_[si];
        if (!rs.loaded) continue;
        for (size_t ri = 0; ri < rs.regions.size(); ++ri) {
            const auto& pr = rs.regions[ri];
            const bool sel = isSelected(rs.fid, pr);
            const bool hov = ((int)si == hovS && (int)ri == hovR);
            // Hole-aware fill from engine triangles when selected or hovered.
            if ((sel || hov) && pr.tris.size() >= 6) {
                ImU32 fillc = sel ? col(pal.accent, hov ? 0.42f : 0.28f)
                                  : col(pal.hoverHighlight, 0.5f);
                for (size_t t = 0; t + 5 < pr.tris.size(); t += 6) {
                    bool ok = true;
                    for (int k = 0; k < 3; ++k) {
                        float u = pr.tris[t + k * 2], v = pr.tris[t + k * 2 + 1];
                        float wx = rs.frame[0] * u + rs.frame[1] * v + rs.frame[3];
                        float wy = rs.frame[4] * u + rs.frame[5] * v + rs.frame[7];
                        float wz = rs.frame[8] * u + rs.frame[9] * v + rs.frame[11];
                        if (!worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, tri[k])) { ok = false; break; }
                    }
                    if (ok) dl->AddTriangleFilled(tri[0], tri[1], tri[2], fillc);
                }
            }
            // Outline: outer + holes. Selected = accent bold, else faint dim line.
            ImU32 lc = sel ? col(pal.accent, 0.95f)
                           : (hov ? col(pal.accent, 0.6f) : col(pal.sketchDimLine, 0.5f));
            float th = (sel ? 2.2f : (hov ? 1.8f : 1.2f)) * ui::uiScale;
            projectPoly(rs.frame, pr.outer, outerS);
            if (outerS.size() >= 2) dl->AddPolyline(outerS.data(), (int)outerS.size(), lc, ImDrawFlags_Closed, th);
            for (const auto& h : pr.holes) {
                projectPoly(rs.frame, h, holeS);
                if (holeS.size() >= 2) dl->AddPolyline(holeS.data(), (int)holeS.size(), lc, ImDrawFlags_Closed, th);
            }
        }
    }
    dl->PopClipRect();

    // ---- click: toggle hovered region, or dismiss the selection process on empty space ----
    // User: clicking off a profile (white space / other UI) ends the SELECT process — but only
    // when at least one profile is already selected. With an empty profile well the picker stays
    // armed so the first pick can still land. Picks themselves are KEPT (killing them would drop
    // the extrude preview and immediately re-arm, which is the opposite of "done selecting").
    if (hovS >= 0 && canHover && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        st.requestCursor(ui::CursorKind::Hand);
        const std::string fid = pickRegions_[hovS].fid;
        const int idx = pickRegions_[hovS].regions[hovR].index;

        std::vector<nlohmann::json> out;
        if (ordered) {
            // Loft sections: clicks APPEND in order; re-clicking removes.
            bool removed = false;
            for (const auto& s : picks) {
                if (s.value("kind", std::string()) == "region" &&
                    s.value("sketch", std::string()) == fid && s.contains("index") &&
                    s["index"].is_number() && (int)s["index"].get<double>() == idx) {
                    removed = true; continue;  // drop it
                }
                out.push_back(s);
            }
            if (!removed)
                out.push_back({{"kind", "region"}, {"sketch", fid}, {"index", idx}});
        } else {
            // Profile: toggle within an effective per-sketch set (whole = all material).
            // Build effective sets for every candidate sketch, toggle the clicked one, re-emit.
            for (const auto& rs : pickRegions_) {
                // effective selected indices for this sketch
                std::vector<int> eff;
                std::vector<int> material;
                for (const auto& pr : rs.regions) {
                    if (pr.isMaterial) material.push_back(pr.index);
                    bool on = explicitHas(rs.fid, pr.index) || (isWhole(rs.fid) && pr.isMaterial);
                    if (on) eff.push_back(pr.index);
                }
                if (rs.fid == fid) {
                    auto it = std::find(eff.begin(), eff.end(), idx);
                    if (it != eff.end()) eff.erase(it); else eff.push_back(idx);
                }
                std::sort(eff.begin(), eff.end());
                // Compact to a whole-sketch pick when eff == the material set (non-empty).
                std::vector<int> mat = material; std::sort(mat.begin(), mat.end());
                if (!eff.empty() && eff == mat) {
                    out.push_back({{"kind", "region"}, {"sketch", rs.fid}});
                } else {
                    for (int i : eff)
                        out.push_back({{"kind", "region"}, {"sketch", rs.fid}, {"index", i}});
                }
            }
            // Never leave a profile empty (would dangle) — fall back to whole clicked sketch.
            if (out.empty())
                out.push_back({{"kind", "region"}, {"sketch", fid}});
        }
        pm->setProfilePicks(st, out);
        return true;   // consumed the region click
    } else if (hovS >= 0 && canHover) {
        st.requestCursor(ui::CursorKind::Hand);
        return true;   // hover owns the cursor over a region
    }
    // Empty-space / off-region click while a profile is already selected AND the well is still
    // armed: end the selection process (deactivate wells) without clearing picks. Empty well
    // stays armed so the first profile can still be chosen. After dismiss, subsequent empty
    // clicks fall through (return false) so model pick / clear-selection work normally.
    if (canHover && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !picks.empty() &&
        pm->hasActiveSelectorWell()) {
        pm->endProfileSelection();
        return true;   // consume THIS click only
    }
    return false;


}

// ---------------------------------------------------------------------------
// datum-plane picker for feature PM wells (reference axis / plane reference geometry)
// ---------------------------------------------------------------------------

// Shared hit-test: user-defined datum PLANES (docMirror refDatums kind=="plane") under the cursor.
// Builds each datum plane's quad EXACTLY as Renderer::drawRefDatums does (origin ± xdir/ydir*ps,
// ps = cam.radius*0.55) and barycentric depth-tests it, so the clickable area coincides with the
// drawn quad. Returns the FRONTMOST hit's feature id + name + camera-distance² (so callers can
// compare it against the three origin-plane depths and pick the single frontmost target), plus the
// winning quad's screen corners for a hover tint. This is what lets the user sketch on / reference
// their OWN datum planes, not just front/top/right.
static bool hitTestDatumPlane(AppState& st, ImVec2 mouse, const ImVec2& imgMin, int vw, int vh,
                              std::string& outFid, std::string& outName, float& outDepth,
                              ImVec2 outQuad[4]) {
    outDepth = 1e30f;
    if (!st.renderer || !st.docMirror.hasDoc()) return false;
    const nlohmann::json& datums = st.docMirror.refDatums();
    if (!datums.is_array() || datums.empty()) return false;
    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    const float ps = cam.radius * 0.55f;   // matches drawRefDatums datum-plane half-extent
    auto v3 = [](const nlohmann::json& a, Vec3 dflt) -> Vec3 {
        if (a.is_array() && a.size() == 3 && a[0].is_number())
            return Vec3((float)a[0].get<double>(), (float)a[1].get<double>(), (float)a[2].get<double>());
        return dflt;
    };
    auto edge = [](ImVec2 p, ImVec2 q, ImVec2 rr) {
        return (p.x - rr.x) * (q.y - rr.y) - (q.x - rr.x) * (p.y - rr.y);
    };
    static const float uv[4][2] = {{-1, -1}, {1, -1}, {1, 1}, {-1, 1}};
    bool found = false;
    for (const auto& d : datums) {
        if (!d.is_object() || d.value("kind", std::string()) != "plane") continue;
        if (st.hiddenFeatures.count(d.value("id", std::string()))) continue;  // hidden datum: not pickable
        Vec3 o  = v3(d.value("origin", nlohmann::json()), Vec3(0, 0, 0));
        Vec3 xd = v3(d.value("x_dir",  nlohmann::json()), Vec3(1, 0, 0));
        Vec3 yd = v3(d.value("y_dir",  nlohmann::json()), Vec3(0, 1, 0));
        Vec3 c4[4]; ImVec2 s4[4]; bool ok = true;
        for (int i = 0; i < 4; ++i) {
            c4[i] = o + xd * (uv[i][0] * ps) + yd * (uv[i][1] * ps);
            if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, s4[i])) { ok = false; break; }
        }
        if (!ok) continue;
        auto hit = [&](int a, int b, int cc, float& dp) -> bool {
            float area = edge(s4[a], s4[b], s4[cc]);
            if (area == 0.0f) return false;
            float w0 = edge(mouse, s4[b], s4[cc]) / area;
            float w1 = edge(mouse, s4[cc], s4[a]) / area;
            float w2 = edge(mouse, s4[a], s4[b]) / area;
            if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
            Vec3 wp(w0 * c4[a].x + w1 * c4[b].x + w2 * c4[cc].x,
                    w0 * c4[a].y + w1 * c4[b].y + w2 * c4[cc].y,
                    w0 * c4[a].z + w1 * c4[b].z + w2 * c4[cc].z);
            float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
            dp = dx * dx + dy * dy + dz * dz;
            return true;
        };
        float dp;
        if ((hit(0, 1, 2, dp) || hit(0, 2, 3, dp)) && dp < outDepth) {
            outDepth = dp;
            outFid  = d.value("id", std::string());
            outName = d.value("name", std::string());
            for (int i = 0; i < 4; ++i) outQuad[i] = s4[i];
            found = true;
        }
    }
    return found;
}

// Hit-test the three default origin planes (front=0/top=1/right=2) that are currently SHOWN, the
// same quads Renderer::planeQuad draws. Returns the frontmost-under-cursor plane's index (+ its
// reserved selector "plane.front/top/right", display name, camera-depth, and screen quad). Lets the
// general viewport selection treat an origin plane like any other pickable so it can be preselected
// for a sketch/feature. Only SHOWN planes are tested, so hidden origin planes never shadow model
// picks during normal modeling.
static bool hitTestOriginPlane(AppState& st, ImVec2 mouse, const ImVec2& imgMin, int vw, int vh,
                               std::string& outSel, std::string& outName, float& outDepth,
                               Vec3 outCorners[4]) {
    outDepth = 1e30f;
    if (!st.renderer) return false;
    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    Vec3 ro, rd;
    if (!screenRay(cam, imgMin, vw, vh, mouse, ro, rd)) return false;
    static const char* kSel[3]  = {"plane.front", "plane.top", "plane.right"};
    static const char* kName[3] = {"X/Z", "X/Y", "Y/Z"};
    bool found = false;
    for (int p = 0; p < 3; ++p) {
        if (!st.planeVisEff[p]) continue;   // only a visible plane is clickable in the graphics area
        Vec3 c4[4];
        if (!r.planeQuad(p, c4)) continue;
        // Ray vs. the plane of the quad, then a bounds check in the quad's own (u,v) frame. World-
        // space so a quad with corners behind the camera (large origin planes) is still hittable.
        Vec3 uE = c4[1] - c4[0], vE = c4[3] - c4[0];
        Vec3 n  = cross(uE, vE);
        float denom = dot(n, rd);
        if (std::fabs(denom) < 1e-9f) continue;       // ray parallel to plane
        float t = dot(n, c4[0] - ro) / denom;
        if (t <= 0.0f) continue;                       // behind the camera
        Vec3 hit = ro + rd * t;
        Vec3 d = hit - c4[0];
        float uLen = length(uE), vLen = length(vE);
        if (uLen < 1e-9f || vLen < 1e-9f) continue;
        float u = dot(d, uE) / (uLen * uLen);          // normalized 0..1 across the quad
        float v = dot(d, vE) / (vLen * vLen);
        if (u < 0.0f || u > 1.0f || v < 0.0f || v > 1.0f) continue;
        // Squared distance from the camera eye — same metric hitTestDatumPlane returns, so the
        // caller can pick the frontmost across created + origin planes.
        Vec3 he = hit - cam.eye;
        float dp = dot(he, he);
        if (dp < outDepth) {
            outDepth = dp;
            outSel  = kSel[p];
            outName = kName[p];
            for (int i = 0; i < 4; ++i) outCorners[i] = c4[i];
            found = true;
        }
    }
    return found;
}

bool SketchMode::drawPlanePicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                 const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    pmPlaneHover_ = -1;
    if (active_) return false;                  // editing a sketch: that overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted || !st.docMirror.hasDoc()) return false;
    // Two modes: (1) a feature PM well wants a plane -> a click ROUTES the datum into that well;
    // (2) no well -> plain graphics-area SELECT (issue #21): a click sets the plane as the
    // FeatureManager selection (mirrors the plane chip), so it highlights and a later New Sketch
    // starts on it. Select mode only runs when at least one origin plane is actually visible.
    const bool wellMode = pm && pm->active() && pm->activeWellWantsPlane();
    if (!wellMode && !(st.planeVisEff[0] || st.planeVisEff[1] || st.planeVisEff[2]))
        return false;

    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    const auto& pal = ui::palette;

    // Project + hit-test the three origin-plane quads exactly as the new-sketch plane pick does
    // (Renderer::planeQuad geometry, frontmost barycentric depth test) so the highlighted plane and
    // the drawn plane always coincide and the overlapping origin planes resolve to the one nearest
    // the camera under the cursor (SolidWorks behavior). See drawOverlay's plane-pick step.
    const char* planeSel[3] = {"plane.front", "plane.top", "plane.right"};
    int hovered = -1;
    float hoveredDepth = 1e30f;
    ImVec2 quadS[3][4];
    bool quadVis[3] = {false, false, false};
    Vec3 quadW[3][4];
    for (int p = 0; p < 3; ++p) {
        if (!st.planeVisEff[p]) continue;   // hidden origin plane: not a valid sketch-plane pick
        Vec3 c4[4];
        if (!r.planeQuad(p, c4)) continue;
        for (int i = 0; i < 4; ++i) quadW[p][i] = c4[i];
        bool ok = true;
        for (int i = 0; i < 4; ++i)
            if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, quadS[p][i])) { ok = false; break; }
        quadVis[p] = ok;
        if (!ok || !imgHovered) continue;
        auto hitDepth = [&](ImVec2 P, int a, int b, int cc, float& depthOut) -> bool {
            ImVec2 sa = quadS[p][a], sb = quadS[p][b], sc = quadS[p][cc];
            auto edge = [](ImVec2 pp, ImVec2 qq, ImVec2 rr) {
                return (pp.x - rr.x) * (qq.y - rr.y) - (qq.x - rr.x) * (pp.y - rr.y);
            };
            float area = edge(sa, sb, sc);
            if (area == 0.0f) return false;
            float w0 = edge(P, sb, sc) / area;
            float w1 = edge(P, sc, sa) / area;
            float w2 = edge(P, sa, sb) / area;
            if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
            Vec3 Wa = quadW[p][a], Wb = quadW[p][b], Wc = quadW[p][cc];
            Vec3 wp(w0 * Wa.x + w1 * Wb.x + w2 * Wc.x,
                    w0 * Wa.y + w1 * Wb.y + w2 * Wc.y,
                    w0 * Wa.z + w1 * Wb.z + w2 * Wc.z);
            float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
            depthOut = dx * dx + dy * dy + dz * dz;
            return true;
        };
        float d;
        if ((hitDepth(io.MousePos, 0, 1, 2, d) || hitDepth(io.MousePos, 0, 2, 3, d)) &&
            d < hoveredDepth) {
            hoveredDepth = d;
            hovered = p;
        }
    }
    // Also hit-test the user's OWN datum planes; the frontmost target (origin OR datum plane) wins,
    // so a created plane in front occludes an origin plane behind it, SolidWorks-style.
    std::string datumFid, datumName; float datumDepth = 1e30f; ImVec2 datumQuad[4];
    bool datumHit = imgHovered && hitTestDatumPlane(st, io.MousePos, imgMin, vw, vh,
                                                    datumFid, datumName, datumDepth, datumQuad);
    bool datumWins = datumHit && datumDepth < hoveredDepth;
    if (datumWins) hovered = -1;   // a datum plane is in front; drop the origin-plane emphasis
    pmPlaneHover_ = hovered;
    // Hover tint on the one plane under the cursor (the renderer already draws the three planes +
    // emphasis brightening; viewport_panel feeds pmPlaneHover() as the emphasis index).
    if (datumWins) {
        wdl->AddQuadFilled(datumQuad[0], datumQuad[1], datumQuad[2], datumQuad[3], col(pal.accent, 0.16f));
        wdl->AddQuad(datumQuad[0], datumQuad[1], datumQuad[2], datumQuad[3], col(pal.accent, 0.90f), 2.0f * ui::uiScale);
    } else if (hovered >= 0 && quadVis[hovered]) {
        wdl->AddQuadFilled(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                           quadS[hovered][3], col(pal.accent, 0.16f));
        wdl->AddQuad(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                     quadS[hovered][3], col(pal.accent, 0.90f), 2.0f * ui::uiScale);
    }
    // Compact hint pill (below the heads-up view toolbar, same slot as the new-sketch banner).
    // Only in well mode — plain select shouldn't nag a banner every time a plane is on screen.
    if (wellMode) {
        ImVec2 hudMin, hudMax;
        skOverlayRect(imgMin, imgMax, hudMin, hudMax);
        glassHudBanner(wdl, "sk_pm_plane_banner", hudMin, hudMax,
                       hudMin.y + ImGui::GetTextLineHeight() + 26.0f * ui::uiScale,
                       "Click a reference plane (incl. your datum planes) \xE2\x80\x94 or a planar face");
    }
    wdl->PopClipRect();

    // Click a plane -> route its datum ref into the active well (a created datum plane routes its
    // feature id + name; an origin plane its reserved string). Returning true tells the viewport
    // this frame's click landed on a plane, so model picking doesn't also fire.
    if ((datumWins || hovered >= 0) && imgHovered) {
        st.requestCursor(ui::CursorKind::Hand);
        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            if (wellMode) {
                if (datumWins) pm->routeDatumPlane(datumFid, datumName);
                else           pm->routeDatumPlane(planeSel[hovered]);
            } else {
                // Plain select: mirror the plane chip (viewport_panel.cpp) — set it as the
                // FeatureManager selection + emphasis so it highlights and a later New Sketch
                // consumes it. Not added to st.selection (datum-for-measure/mate is a follow-up).
                st.selection.clear();
                st.selectedFeatureId = datumWins ? datumFid : std::string(planeSel[hovered]);
                st.planeEmphasis = datumWins ? -1 : hovered;
            }
        }
    }
    return hovered >= 0 || datumWins;
}

// ---------------------------------------------------------------------------
// datum-axis picker for feature PM wells (revolve / circular-pattern axis)
// ---------------------------------------------------------------------------

// Hit-test the displayed datum-axis lines in screen space. Consumes renderer-injected axes
// (defaults + auto cache + user) — never re-solves plane pairs. Returns true + the hovered
// axis's screen endpoints + feature id/name when the cursor is within the
// pick radius. Shared by the PM axis picker (route into a well) and general datum selection.
// Pick segment half-length L must match the DRAWN axis half-length in Renderer::drawRefDatums:
// plane half-extent e = (radius_>0 ? radius_ : kEmptySceneRadius), L = e * 1.0 (not the old e*1.6).
// cam.radius is published as that same sceneR (renderer.cpp drawCurrent), so use it directly —
// modelRadius() is 0 with no solid body (e.g. during Revolve: sketch + axis, no body).
static bool hitTestDatumAxis(AppState& st, ImVec2 mouse, const ImVec2& imgMin, int vw, int vh,
                             ImVec2& outA, ImVec2& outB, std::string& outFid, std::string& outName) {
    if (!st.renderer || !st.docMirror.hasDoc()) return false;
    const Renderer& r = *st.renderer;
    const auto& cam = r.camera();
    // Match DRAWN axis half-length: e = plane half-extent, L = e (factor 1.0).
    const float L = cam.radius;
    auto distToSeg = [](ImVec2 p, ImVec2 a, ImVec2 b) -> float {
        float vx = b.x - a.x, vy = b.y - a.y, wx = p.x - a.x, wy = p.y - a.y;
        float len2 = vx * vx + vy * vy;
        float t = len2 > 1e-6f ? (wx * vx + wy * vy) / len2 : 0.0f;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
        float dx = a.x + t * vx - p.x, dy = a.y + t * vy - p.y;
        return std::sqrt(dx * dx + dy * dy);
    };

    // While a PM axis well is active, hidden axes stay pickable (force-show for picking).
    // Non-axis wells / general selection keep the hide-skip behavior.
    auto* pm = st.propertyManager;
    const bool forcePickHidden =
        pm && pm->active() && pm->activeWellWantsAxis();

    struct AxisCand { std::string id, name; Vec3 P, D; };
    std::vector<AxisCand> cands;
    std::unordered_set<std::string> seen;

    auto addCand = [&](const std::string& id, const std::string& name,
                       const Vec3& P, const Vec3& D) {
        if (id.empty() || seen.count(id)) return;
        if (!forcePickHidden && st.hiddenFeatures.count(id)) return;
        Vec3 dir = D;
        if (length(dir) < 1e-9f) return;
        dir = normalize(dir);
        cands.push_back({id, name.empty() ? id : name, P, dir});
        seen.insert(id);
    };

    // Prefer inject geometry already on the renderer (defaults + auto + user axes).
    const auto& rds = r.refDatums();
    bool anyAxis = false;
    for (const auto& d : rds) {
        if (d.kind != Renderer::RefDatum::Kind::Axis) continue;
        anyAxis = true;
        addCand(d.id, d.name, d.origin, d.dir);
    }
    // Pre-inject / empty-renderer fallback: only predefined X/Y/Z (never re-solve autos).
    if (!anyAxis) {
        addCand("axis.x", "X Axis", Vec3(0, 0, 0), Vec3(1, 0, 0));
        addCand("axis.y", "Y Axis", Vec3(0, 0, 0), Vec3(0, 1, 0));
        addCand("axis.z", "Z Axis", Vec3(0, 0, 0), Vec3(0, 0, 1));
    }

    float best = 9.0f * ui::uiScale;   // pixel pick radius around the line
    bool found = false;
    for (const auto& c : cands) {
        ImVec2 sa, sb;
        if (!worldToScreen(cam, imgMin, vw, vh,
                           c.P.x - c.D.x * L, c.P.y - c.D.y * L, c.P.z - c.D.z * L, sa))
            continue;
        if (!worldToScreen(cam, imgMin, vw, vh,
                           c.P.x + c.D.x * L, c.P.y + c.D.y * L, c.P.z + c.D.z * L, sb))
            continue;
        float dist = distToSeg(mouse, sa, sb);
        if (dist < best) {
            best = dist; found = true; outA = sa; outB = sb;
            outFid = c.id;
            outName = c.name;
        }
    }
    return found;
}

bool SketchMode::drawAxisPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                  // editing a sketch: that overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.workerStarted || !pm || !pm->active() || !pm->activeWellWantsAxis() || !imgHovered)
        return false;
    ImVec2 a, b; std::string fid, name;
    if (!hitTestDatumAxis(st, io.MousePos, imgMin, vw, vh, a, b, fid, name)) return false;

    const auto& pal = ui::palette;
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    wdl->AddLine(a, b, col(pal.accent, 0.95f), 4.0f * ui::uiScale);
    {
        ImVec2 hudMin, hudMax;
        skOverlayRect(imgMin, imgMax, hudMin, hudMax);
        glassHudBanner(wdl, "sk_pm_axis_banner", hudMin, hudMax,
                       hudMin.y + ImGui::GetTextLineHeight() + 26.0f * ui::uiScale,
                       "Click the reference axis");
    }
    wdl->PopClipRect();

    st.requestCursor(ui::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) pm->routeDatumAxis(st, fid, name);
    return true;
}

// T-BL37: hit-test displayed point datums (world origin + user-created point features) in screen
// space. A 2D distance test against the projected world point, within a grab radius. On click,
// routes the point datum into the active PM well via routeDatumPoint().
bool SketchMode::drawPointPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                 const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted || !pm || !pm->active() ||
        !pm->activeWellWantsPoint() || !imgHovered)
        return false;

    const auto& cam = st.renderer->camera();
    const ImVec2 mouse = io.MousePos;
    const float grabRadius = 10.0f * ui::uiScale;

    // Collect all point-kind datums that are not hidden (including the synthetic origin).
    // The renderer's refDatums_ is not directly accessible, so we rebuild from docMirror
    // (user-created) plus the synthetic origin. Origin gates on originVisEff — same "drawn
    // iff pickable" contract as drawPointPicker's sketch-plane sibling and the FM eye.
    struct PointCandidate { float wx, wy, wz; std::string fid, name; };
    std::vector<PointCandidate> candidates;

    // Synthetic world origin — only when the triad is actually drawn this frame.
    if (st.originVisEff)
        candidates.push_back({0.0f, 0.0f, 0.0f, "point.origin", "Origin"});

    // User-created point-datum features from docMirror.
    const nlohmann::json& datums = st.docMirror.refDatums();
    if (datums.is_array()) {
        for (const auto& d : datums) {
            if (!d.is_object() || d.value("kind", std::string()) != "point") continue;
            std::string fid = d.value("id", std::string());
            if (fid.empty() || st.hiddenFeatures.count(fid)) continue;
            const auto& pt = d.contains("point") ? d["point"] : nlohmann::json();
            if (!pt.is_array() || pt.size() < 3) continue;
            candidates.push_back({(float)pt[0].get<double>(),
                                   (float)pt[1].get<double>(),
                                   (float)pt[2].get<double>(),
                                   fid, d.value("name", fid)});
        }
    }

    // Find the closest candidate within grab radius.
    float best = grabRadius;
    std::string bestFid, bestName;
    ImVec2 bestScreen;
    for (const auto& c : candidates) {
        ImVec2 s;
        if (!worldToScreen(cam, imgMin, vw, vh, c.wx, c.wy, c.wz, s)) continue;
        float dx = mouse.x - s.x, dy = mouse.y - s.y;
        float d = std::sqrt(dx * dx + dy * dy);
        if (d < best) {
            best = d;
            bestFid = c.fid;
            bestName = c.name;
            bestScreen = s;
        }
    }
    if (bestFid.empty()) return false;

    // Highlight: draw an accent circle around the projected point glyph.
    const auto& pal = ui::palette;
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    wdl->AddCircle(bestScreen, grabRadius * 0.8f, col(pal.accent, 0.95f), 24, 2.0f * ui::uiScale);
    {
        ImVec2 hudMin, hudMax;
        skOverlayRect(imgMin, imgMax, hudMin, hudMax);
        glassHudBanner(wdl, "sk_pm_point_banner", hudMin, hudMax,
                       hudMin.y + ImGui::GetTextLineHeight() + 26.0f * ui::uiScale,
                       "Click the reference point");
    }
    wdl->PopClipRect();

    st.requestCursor(ui::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left))
        pm->routeDatumPoint(bestFid, bestName);
    return true;
}

bool SketchMode::drawCurvePathPicker(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                     const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (active_) return false;                  // editing a sketch: that overlay owns the canvas
    auto* pm = st.propertyManager;
    if (!st.renderer || !st.workerStarted || !pm || !pm->active() ||
        !pm->activeWellWantsPathFeature() || !imgHovered)
        return false;
    const auto& cam = st.renderer->camera();
    const ImVec2 mouse = io.MousePos;

    auto distToSeg = [](ImVec2 p, ImVec2 a, ImVec2 b) -> float {
        float vx = b.x - a.x, vy = b.y - a.y, wx = p.x - a.x, wy = p.y - a.y;
        float len2 = vx * vx + vy * vy;
        float t = len2 > 1e-6f ? (wx * vx + wy * vy) / len2 : 0.0f;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
        float dx = a.x + t * vx - p.x, dy = a.y + t * vy - p.y;
        return std::sqrt(dx * dx + dy * dy);
    };
    float best = 9.0f * ui::uiScale;   // pixel grab radius
    bool found = false, bestIsCurve = false;
    std::string bestFid, bestName;
    std::vector<std::pair<ImVec2, ImVec2>> bestSegs;   // screen segments to highlight

    // --- 1) curve datums (helix / spiral / spline) -------------------------------
    const nlohmann::json& datums = st.docMirror.refDatums();
    if (datums.is_array()) {
        for (const auto& d : datums) {
            if (!d.is_object() || d.value("kind", std::string()) != "curve") continue;
            const auto& pts = d.contains("points") ? d["points"] : nlohmann::json();
            if (!pts.is_array() || pts.size() < 2) continue;
            std::vector<std::pair<ImVec2, ImVec2>> segs;
            float dlocal = 1e9f;
            ImVec2 prev; bool have = false;
            for (const auto& p : pts) {
                ImVec2 s; bool ok = p.is_array() && p.size() >= 3 &&
                    worldToScreen(cam, imgMin, vw, vh, (float)p[0].get<double>(),
                                  (float)p[1].get<double>(), (float)p[2].get<double>(), s);
                if (!ok) { have = false; continue; }
                if (have) { segs.push_back({prev, s}); dlocal = std::min(dlocal, distToSeg(mouse, prev, s)); }
                prev = s; have = true;
            }
            if (dlocal < best) {
                best = dlocal; found = true; bestIsCurve = true;
                bestFid = d.value("id", std::string());
                bestName = d.value("name", bestFid);
                bestSegs = std::move(segs);
            }
        }
    }

    // --- 2) ghost sketches (a whole SKETCH used as the path) ---------------------
    // The sketch the user drew for the path is rendered as a ghost (drawGhosts); hit-test
    // its projected entities so a viewport click on the sketch fills the path well. Skip the
    // sketch already consumed as the Profile so the user can't accidentally self-sweep.
    std::set<std::string> profileSketches;
    for (const auto& sel : pm->profilePicks())
        if (sel.is_object() && sel.contains("sketch") && sel["sketch"].is_string())
            profileSketches.insert(sel["sketch"].get<std::string>());
    auto ghostName = [&](const std::string& fid) -> std::string {
        const nlohmann::json& tree = st.docMirror.tree();
        if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
            for (const auto& tf : tree["features"])
                if (tf.is_object() && tf.value("id", std::string()) == fid)
                    return tf.value("name", fid);
        return fid;
    };
    const float D2R = 3.14159265f / 180.0f;
    for (const auto& g : ghosts_) {
        if (!g.loaded || profileSketches.count(g.fid)) continue;
        auto uv2s = [&](float u, float v, ImVec2& o) -> bool {
            float wx = g.frame[0] * u + g.frame[1] * v + g.frame[3];
            float wy = g.frame[4] * u + g.frame[5] * v + g.frame[7];
            float wz = g.frame[8] * u + g.frame[9] * v + g.frame[11];
            return worldToScreen(cam, imgMin, vw, vh, wx, wy, wz, o);
        };
        std::vector<std::pair<ImVec2, ImVec2>> segs;
        float dlocal = 1e9f;
        auto samplePoly = [&](auto pointAt, int seg) {
            ImVec2 prev; bool have = false;
            for (int i = 0; i <= seg; ++i) {
                float u, v; pointAt(i / (float)seg, u, v);
                ImVec2 p;
                if (!uv2s(u, v, p)) { have = false; continue; }
                if (have) { segs.push_back({prev, p}); dlocal = std::min(dlocal, distToSeg(mouse, prev, p)); }
                prev = p; have = true;
            }
        };
        for (const auto& e : g.ents) {
            if (e.construction) continue;
            if (e.type == "line" || e.type == "slot" || e.type == "arc_slot") {
                samplePoly([&](float t, float& u, float& v) {
                    u = e.p1[0] + (e.p2[0] - e.p1[0]) * t;
                    v = e.p1[1] + (e.p2[1] - e.p1[1]) * t;
                }, 1);
            } else if (e.type == "circle") {
                samplePoly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 48);
            } else if (e.type == "arc") {
                float a0 = e.start, a1 = e.end;
                if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                if (a1 < a0) a1 += 360.0f;
                samplePoly([&](float t, float& u, float& v) {
                    float a = (a0 + (a1 - a0) * t) * D2R;
                    u = e.center[0] + e.radius * std::cos(a);
                    v = e.center[1] + e.radius * std::sin(a);
                }, 40);
            } else if (e.type == "ellipse") {
                float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
                samplePoly([&](float t, float& u, float& v) {
                    float a = t * 6.2831853f;
                    float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                    u = e.center[0] + lx * ca - ly * sa;
                    v = e.center[1] + lx * sa + ly * ca;
                }, 48);
            } else if ((e.type == "spline" || e.type == "style_spline") && e.pts.size() >= 2) {
                for (size_t i = 1; i < e.pts.size(); ++i) {
                    ImVec2 a, b;
                    if (uv2s(e.pts[i - 1].x, e.pts[i - 1].y, a) && uv2s(e.pts[i].x, e.pts[i].y, b)) {
                        segs.push_back({a, b}); dlocal = std::min(dlocal, distToSeg(mouse, a, b));
                    }
                }
            }
        }
        if (dlocal < best) {
            best = dlocal; found = true; bestIsCurve = false;
            bestFid = g.fid; bestName = ghostName(g.fid);
            bestSegs = std::move(segs);
        }
    }
    if (!found) return false;

    const auto& pal = ui::palette;
    ImDrawList* wdl = ImGui::GetWindowDrawList();
    wdl->PushClipRect(imgMin, imgMax, true);
    for (const auto& s : bestSegs)
        wdl->AddLine(s.first, s.second, col(pal.accent, 0.95f), 4.0f * ui::uiScale);
    {
        ImVec2 hudMin, hudMax;
        skOverlayRect(imgMin, imgMax, hudMin, hudMax);
        glassHudBanner(wdl, "sk_pm_path_banner", hudMin, hudMax,
                       hudMin.y + ImGui::GetTextLineHeight() + 26.0f * ui::uiScale,
                       "Click to set this as the sweep path");
    }
    wdl->PopClipRect();

    st.requestCursor(ui::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left))
        pm->routePathFeature(bestFid, bestName, bestIsCurve);
    return true;
}

bool SketchMode::drawDatumSelect(AppState& st, ImGuiIO& io, const ImVec2& imgMin,
                                 const ImVec2& imgMax, int vw, int vh, bool imgHovered) {
    if (!st.renderer || active_ || !imgHovered) return false;
    auto* pm = st.propertyManager;
    // A PM picker (axis/plane well) owns datum picks while open — don't also general-select.
    if (pm && pm->active() && (pm->activeWellWantsAxis() || pm->activeWellWantsPlane())) return false;

    ImDrawList* wdl = ImGui::GetWindowDrawList();
    std::string fid, name;

    // A datum POINT (the synthetic world origin + any user point-datums) is the tightest target of
    // all, and the origin sits exactly where the three origin planes intersect — so it must win over
    // both the axes and the planes, or a coincident plane always swallows the click and the origin is
    // never selectable. Same grab-radius hit-test as drawPointPicker; the origin is only pickable via
    // its glyph, never via a user-placed plane or model geometry.
    {
        const auto& cam = st.renderer->camera();
        const ImVec2 mouse = io.MousePos;
        // The origin octahedron renders only a few px wide but sits amid full-screen origin planes,
        // so a tight target loses every click to the coincident plane. Use a generous radius (like a
        // SolidWorks origin pick) so a deliberate click near the glyph reliably takes the point.
        const float grabRadius = 20.0f * ui::uiScale;
        struct PointCandidate { float wx, wy, wz; std::string fid, name; };
        std::vector<PointCandidate> pts;
        // Gate on originVisEff (what the renderer actually DRAWS), not hiddenFeatures: the origin is
        // clickable exactly when it is visible — never through a body that auto-hid it, never in the
        // default-hidden empty state. Mirrors how plane hit-tests gate on planeVisEff.
        if (st.originVisEff)
            pts.push_back({0.0f, 0.0f, 0.0f, "point.origin", "Origin"});
        const nlohmann::json& datums = st.docMirror.refDatums();
        if (datums.is_array()) {
            for (const auto& d : datums) {
                if (!d.is_object() || d.value("kind", std::string()) != "point") continue;
                std::string pfid = d.value("id", std::string());
                if (pfid.empty() || st.hiddenFeatures.count(pfid)) continue;
                const auto& pt = d.contains("point") ? d["point"] : nlohmann::json();
                if (!pt.is_array() || pt.size() < 3) continue;
                pts.push_back({(float)pt[0].get<double>(), (float)pt[1].get<double>(),
                               (float)pt[2].get<double>(), pfid, d.value("name", pfid)});
            }
        }
        float best = grabRadius; ImVec2 bestScreen;
        for (const auto& c : pts) {
            ImVec2 s;
            if (!worldToScreen(cam, imgMin, vw, vh, c.wx, c.wy, c.wz, s)) continue;
            // DIAG (CHUDWARE_ORIGIN_PICK_DEBUG): mark exactly where the code thinks each datum point is,
            // plus the grab ring, so we can see alignment vs. the click. Remove once verified.
            // Deliberately a non-palette magenta: diagnostic ink must be impossible to mistake for
            // real sketch ink in a screenshot, so it must NOT track the theme.
            static const bool kDbg = std::getenv("CHUDWARE_ORIGIN_PICK_DEBUG") != nullptr;
            if (kDbg) {
                wdl->PushClipRect(imgMin, imgMax, true);
                wdl->AddCircle(s, grabRadius, IM_COL32(255, 0, 255, 200), 24, 1.5f * ui::uiScale);  // gui-lint: allow debug-only diagnostic ink
                wdl->AddLine(ImVec2(s.x - 6, s.y), ImVec2(s.x + 6, s.y), IM_COL32(255, 0, 255, 255), 1.5f);  // gui-lint: allow debug-only diagnostic ink
                wdl->AddLine(ImVec2(s.x, s.y - 6), ImVec2(s.x, s.y + 6), IM_COL32(255, 0, 255, 255), 1.5f);  // gui-lint: allow debug-only diagnostic ink
                wdl->PopClipRect();
            }
            float dx = mouse.x - s.x, dy = mouse.y - s.y;
            float d = std::sqrt(dx * dx + dy * dy);
            if (d < best) { best = d; fid = c.fid; name = c.name; bestScreen = s; }
        }
        if (!fid.empty()) {
            wdl->PushClipRect(imgMin, imgMax, true);
            wdl->AddCircle(bestScreen, grabRadius * 0.8f, col(ui::palette.accent, 0.95f),
                           24, 2.0f * ui::uiScale);
            wdl->PopClipRect();
            st.requestCursor(ui::CursorKind::Hand);
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                if (!io.KeyCtrl) st.selection.clear();
                st.selectedFeatureId = fid;
                st.treeScrollToSelection = true;
            }
            return true;
        }
    }

    // A datum AXIS is a thin target, so it wins when the cursor is on the line (a big datum plane
    // behind it would otherwise swallow the click). Fall back to a created datum PLANE so a plane
    // can be selected in the viewport and then preselected for a sketch/feature (SolidWorks pick).
    ImVec2 a, b;
    bool axisHit = hitTestDatumAxis(st, io.MousePos, imgMin, vw, vh, a, b, fid, name);
    if (axisHit) {
        wdl->PushClipRect(imgMin, imgMax, true);
        wdl->AddLine(a, b, col(ui::palette.accent, 0.90f), 4.0f * ui::uiScale);
        wdl->PopClipRect();
    } else {
        // Pick the frontmost plane under the cursor across BOTH created datum planes and the (shown)
        // default origin planes, so either can be selected in the graphics area and preselected for a
        // sketch/feature. selectedFeatureId is the created plane's feature id or a reserved
        // "plane.front/top/right" token — both resolve as a sketch/feature plane.
        float cDepth = 1e30f, oDepth = 1e30f;
        ImVec2 cQuad[4]; Vec3 oCorners[4];
        std::string cFid, cName, oSel, oName;
        bool cHit = hitTestDatumPlane(st, io.MousePos, imgMin, vw, vh, cFid, cName, cDepth, cQuad) &&
                    !cFid.empty();
        bool oHit = hitTestOriginPlane(st, io.MousePos, imgMin, vw, vh, oSel, oName, oDepth, oCorners);
        if (!cHit && !oHit) return false;
        const bool originWins = oHit && (!cHit || oDepth < cDepth);
        fid  = originWins ? oSel  : cFid;
        name = originWins ? oName : cName;
        wdl->PushClipRect(imgMin, imgMax, true);
        if (originWins) {
            // Project the origin-plane corners for the outline; skip any edge with an endpoint
            // behind the camera (large quads can wrap) so we never draw a stray line.
            const auto& cam = st.renderer->camera();
            ImVec2 s[4]; bool ok[4];
            for (int i = 0; i < 4; ++i)
                ok[i] = worldToScreen(cam, imgMin, vw, vh, oCorners[i].x, oCorners[i].y,
                                      oCorners[i].z, s[i]);
            for (int i = 0; i < 4; ++i) {
                int j = (i + 1) & 3;
                if (ok[i] && ok[j]) wdl->AddLine(s[i], s[j], col(ui::palette.accent, 0.90f), 2.5f * ui::uiScale);
            }
        } else {
            for (int i = 0; i < 4; ++i)
                wdl->AddLine(cQuad[i], cQuad[(i + 1) & 3], col(ui::palette.accent, 0.90f), 2.5f * ui::uiScale);
        }
        wdl->PopClipRect();
    }

    st.requestCursor(ui::CursorKind::Hand);
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        if (!io.KeyCtrl) st.selection.clear();   // plain click = single-select the datum/plane
        st.selectedFeatureId = fid;              // tree row highlights; renderer emphasizes the datum
        // Keep the origin-plane tree emphasis in step when a default plane was picked in the viewport.
        st.planeEmphasis = (fid == "plane.front") ? 0 : (fid == "plane.top") ? 1
                          : (fid == "plane.right") ? 2 : st.planeEmphasis;
        st.treeScrollToSelection = true;
    }
    return true;
}

bool SketchMode::entityDragActive() const {
    return dragArmed_ || dragging_ || groupDragArmed_ || groupDragging_ || dimDragging_;
}

bool SketchMode::sketchOrbitCenter(Vec3& world) const {
    if (!active_) return false;
    // 2D bbox over the sketch entities (plane u,v); empty sketch -> plane origin (u=v=0).
    float mnu = 1e30f, mnv = 1e30f, mxu = -1e30f, mxv = -1e30f;
    bool any = false;
    auto inc = [&](float u, float v) {
        if (u < mnu) mnu = u; if (v < mnv) mnv = v;
        if (u > mxu) mxu = u; if (v > mxv) mxv = v; any = true;
    };
    for (const auto& e : ents_) {
        inc(e.p1[0], e.p1[1]);
        if (e.type == "line" || e.type == "slot" || e.type == "arc_slot") inc(e.p2[0], e.p2[1]);
        if (e.type == "circle" || e.type == "arc" || e.type == "ellipse" || e.type == "arc_slot") {
            float rr = e.radius > 0 ? e.radius : std::fmax(e.rx, e.ry);
            inc(e.center[0] - rr, e.center[1] - rr);
            inc(e.center[0] + rr, e.center[1] + rr);
        }
        for (const auto& p : e.pts) inc(p.x, p.y);
    }
    float cu = any ? (mnu + mxu) * 0.5f : 0.0f;
    float cv = any ? (mnv + mxv) * 0.5f : 0.0f;
    // Lift (cu,cv,0) to world via frame_ (row-major, sketch(u,v,0)->world) — same pattern as buildBasis.
    const float* fr = frame_;
    world.x = fr[0] * cu + fr[1] * cv + fr[3];
    world.y = fr[4] * cu + fr[5] * cv + fr[7];
    world.z = fr[8] * cu + fr[9] * cv + fr[11];
    return true;
}

// ---------------------------------------------------------------------------
// overlay draw + interaction
// ---------------------------------------------------------------------------

void SketchMode::drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                             int vw, int vh, bool imgHovered) {
    if ((active_ || planePicking_) && st.docLockedByAgent(
            docId_.empty() ? st.docMirror.docId() : docId_)) {
        std::string part = "part";
        const std::string did = docId_.empty() ? st.docMirror.docId() : docId_;
        for (const auto& tab : st.docTabs)
            if (tab.docId == did) { part = tab.title; break; }
        if (active_) exitSketch(st, /*keep=*/true);
        planePicking_ = false;
        planeEditFid_.clear();
        st.sketchPlanePickActive = false;
        st.toast(ui::ToastLevel::Info,
                 "Agent started editing " + part + " — your sketch was kept and closed");
        return;
    }
    if (!st.renderer) return;
    // GUI-picking overhaul: default to "hot" (blocks the viewport's model-pick fallback) any time a
    // drawing/edit tool owns the cursor (tool_ != None) or before the idle-hover pass below runs;
    // the tool_==None branch narrows this to the actual per-frame hit-test result.
    entityHoverHot_ = (tool_ != SketchTool::None);
    // Sketch dimensions + Modify box follow the document unit system (mm / inch); refreshed each
    // frame so a status-bar units toggle reformats on-canvas dims and the Modify box live.
    g_skIps = st.ips();
    const auto& pal = ui::palette;

    // Sketch-overlay safe area: clip all on-canvas drawing (geometry, relations, dimensions, plane
    // labels…) and anchor the top-center HUD inside the viewport's clear rect (skOverlayRect), so
    // nothing draws under the pinned panes and clashes with their text. imgMin/imgMax stay the TRUE
    // image bounds for coordinate projection + hit-testing — only the DRAW clip is inset.
    ImVec2 skClipMin, skClipMax;
    skOverlayRect(imgMin, imgMax, skClipMin, skClipMax);
    // Top-center HUD stack: status/DOF island first, banners/tool hints one row below it.
    const float skHudTopY = skClipMin.y + 8.0f * ui::uiScale;
    const float skHudRow2Y = skHudTopY + ImGui::GetTextLineHeight() + 18.0f * ui::uiScale;

    // ---- C37-D: SolidWorks plane-pick step (runs while !active_, before a sketch exists) ----
    // The viewport keeps calling drawOverlay (and force-shows the origin planes) while
    // st.sketchPlanePickActive is set; we ray-hit-test the three origin-plane quads + planar faces.
    if (planePicking_ && st.sketchPlanePickActive) {
        st.requestCursor(ui::CursorKind::Crosshair);
        const Renderer& r = *st.renderer;
        const auto& cam = r.camera();
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        wdl->PushClipRect(skClipMin, skClipMax, true);   // dodge the pinned columns

        // Project each origin-plane quad (front=0/top=1/right=2). The corners come straight from
        // Renderer::planeQuad — the SAME geometry drawReferencePlanes renders — so the hit-test
        // polygon and the visible plane always coincide (the old mirrored copy used cam.center,
        // which includes the pan offset, and drifted away from the drawn planes after any pan).
        const char* planeSel[3] = {"plane.front", "plane.top", "plane.right"};
        int hovered = -1;
        float hoveredDepth = 1e30f;   // C52: pick the FRONTMOST plane under the cursor (SW behavior)
        ImVec2 quadS[3][4];
        bool quadVis[3] = {false, false, false};
        Vec3 quadW[3][4];
        for (int p = 0; p < 3; ++p) {
            if (!st.planeVisEff[p]) continue;   // hidden origin plane: not a valid sketch-plane pick
            Vec3 c4[4];
            if (!r.planeQuad(p, c4)) continue;
            for (int i = 0; i < 4; ++i) quadW[p][i] = c4[i];
            bool ok = true;
            for (int i = 0; i < 4; ++i) {
                if (!worldToScreen(cam, imgMin, vw, vh, c4[i].x, c4[i].y, c4[i].z, quadS[p][i])) { ok = false; break; }
            }
            quadVis[p] = ok;
            if (!ok || !imgHovered) continue;
            // C52 plane-pick fix: the three origin planes all pass through the origin and overlap
            // heavily on screen, so a simple point-in-quad test that keeps the LAST matching index
            // always resolved the overlap region to Right (YZ, index 2) — the user's "wrong plane"
            // bug. Instead, point-in-triangle with BARYCENTRIC weights so we can interpolate the
            // world-space hit point and compare DEPTHS, choosing the plane whose surface is closest
            // to the camera at this pixel (frontmost) — exactly how SolidWorks highlights the plane
            // under the cursor.
            auto hitDepth = [&](ImVec2 P, int a, int b, int cc, float& depthOut) -> bool {
                ImVec2 sa = quadS[p][a], sb = quadS[p][b], sc = quadS[p][cc];
                auto edge = [](ImVec2 pp, ImVec2 qq, ImVec2 rr) {
                    return (pp.x - rr.x) * (qq.y - rr.y) - (qq.x - rr.x) * (pp.y - rr.y);
                };
                float area = edge(sa, sb, sc);
                if (area == 0.0f) return false;
                float w0 = edge(P, sb, sc) / area;   // weight of vertex a
                float w1 = edge(P, sc, sa) / area;   // weight of vertex b
                float w2 = edge(P, sa, sb) / area;   // weight of vertex c
                // Inside-test is winding-agnostic: dividing by the signed area normalises the sign,
                // so an interior point has all three weights >= 0 for both CW and CCW screen winding.
                if (w0 < 0.0f || w1 < 0.0f || w2 < 0.0f) return false;
                Vec3 Wa = quadW[p][a], Wb = quadW[p][b], Wc = quadW[p][cc];
                Vec3 wp(w0 * Wa.x + w1 * Wb.x + w2 * Wc.x,
                        w0 * Wa.y + w1 * Wb.y + w2 * Wc.y,
                        w0 * Wa.z + w1 * Wb.z + w2 * Wc.z);
                float dx = wp.x - cam.eye.x, dy = wp.y - cam.eye.y, dz = wp.z - cam.eye.z;
                depthOut = dx * dx + dy * dy + dz * dz;
                return true;
            };
            float d;
            if ((hitDepth(io.MousePos, 0, 1, 2, d) || hitDepth(io.MousePos, 0, 2, 3, d)) &&
                d < hoveredDepth) {
                hoveredDepth = d;
                hovered = p;
            }
        }
        // Also hit-test the user's OWN datum planes so a sketch can be started on one (not just the
        // three origin planes). The frontmost target (origin OR datum plane) wins.
        std::string sDatumFid, sDatumName; float sDatumDepth = 1e30f; ImVec2 sDatumQuad[4];
        bool sDatumHit = imgHovered && hitTestDatumPlane(st, io.MousePos, imgMin, vw, vh,
                                                         sDatumFid, sDatumName, sDatumDepth, sDatumQuad);
        // A planar SOLID FACE under the cursor (SolidWorks "sketch on a face"). The id-buffer pick
        // (st.hover) tells us WHICH face; we compute its depth by intersecting the cursor ray with the
        // face's supporting plane (anchor + normal from faceNormalCache), then dist^2 from the hit point
        // to cam.eye — the SAME metric hoveredDepth / sDatumDepth use — so all three compare directly.
        // A face in FRONT of a plane must win the pick, not fall through to the plane behind it.
        float sFaceDepth = 1e30f;
        bool sFaceHit = false;
        // The id-buffer draws every body regardless of Solid-Bodies hide/show (it needs to keep
        // picking geometry-stable for feature edits), so a hidden body's face still shows up as
        // st.hover here. Reject it explicitly — a face you can't see must not be pickable as a
        // sketch plane.
        if (imgHovered && st.hover.valid() && st.hover.kind == render::PickKind::Face &&
            r.bodyVisibleById(st.hover.body_id)) {
            uint64_t fkey = ((uint64_t)st.hover.body_id << 32) | (uint64_t)st.hover.topo_id;
            auto fit = st.faceNormalCache.find(fkey);
            if (fit != st.faceNormalCache.end()) {
                const auto& fn = fit->second;   // {nx,ny,nz, cx,cy,cz}
                Vec3 n(fn[0], fn[1], fn[2]);
                Vec3 anchor(fn[3], fn[4], fn[5]);
                Vec3 ro, rd;
                if (screenRay(cam, imgMin, vw, vh, io.MousePos, ro, rd)) {
                    float denom = dot(rd, n);
                    if (std::fabs(denom) > 1e-6f) {   // not grazing/parallel
                        float t = dot(anchor - ro, n) / denom;
                        if (t > 0.0f) {               // hit is in front of the ray origin
                            Vec3 hit = ro + rd * t;
                            float dx = hit.x - cam.eye.x, dy = hit.y - cam.eye.y, dz = hit.z - cam.eye.z;
                            sFaceDepth = dx * dx + dy * dy + dz * dz;
                            sFaceHit = true;
                        }
                    }
                }
            }
        }
        // Three-way frontmost among {origin plane, datum plane, solid face} — a strict priority chain
        // would reintroduce the "plane wins if it registers at all" bug, so rank purely by depth.
        float originDepth = (hovered >= 0) ? hoveredDepth : 1e30f;
        bool sFaceWins = sFaceHit && sFaceDepth < originDepth &&
                         (!sDatumHit || sFaceDepth < sDatumDepth);
        bool sDatumWins = sDatumHit && !sFaceWins && sDatumDepth < originDepth;
        if (sDatumWins || sFaceWins) hovered = -1;   // a plane/face is in front; drop origin emphasis
        planeHover_ = hovered;
        // The renderer already draws all three datum planes (fill + outline, with emphasis
        // brightening the hovered one — viewport_panel passes planeHover() as the emphasis).
        // Painting a second translucent fill per plane on top made the overlap muddy and
        // unreadable (user bug report), so the overlay adds ONLY a hover tint on the one
        // plane under the cursor.
        if (sDatumWins) {
            wdl->AddQuadFilled(sDatumQuad[0], sDatumQuad[1], sDatumQuad[2], sDatumQuad[3], col(pal.accent, 0.16f));
            wdl->AddQuad(sDatumQuad[0], sDatumQuad[1], sDatumQuad[2], sDatumQuad[3], col(pal.accent, 0.90f), 2.0f * ui::uiScale);
        } else if (hovered >= 0 && quadVis[hovered]) {
            wdl->AddQuadFilled(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                               quadS[hovered][3], col(pal.accent, 0.16f));
            wdl->AddQuad(quadS[hovered][0], quadS[hovered][1], quadS[hovered][2],
                         quadS[hovered][3], col(pal.accent, 0.90f), 2.0f * ui::uiScale);
        }
        // Click: a datum plane wins if frontmost; else an origin plane; else the hovered planar face.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            if (sFaceWins && st.hover.valid() && st.hover.kind == render::PickKind::Face) {
                // Planar-face pick: resolve via the same path enterNewOnSelection uses. We try the
                // hovered face's selector; the engine rejects non-planar faces (toast + stay).
                std::string perr;
                std::string sel = st.docMirror.resolvePick(st.worker, st.hover.body_id, "face",
                                                           st.hover.topo_id, perr);
                if (!sel.empty()) {
                    nlohmann::json js;
                    try { js = nlohmann::json::parse(sel); } catch (...) {}
                    if (js.is_object()) {
                        std::string newFid = consumePlanePick(st, js);
                        if (newFid.empty())
                            st.toast(ui::ToastLevel::Warn, "That face can't host a sketch — pick a planar face or plane");
                        else { planePicking_ = false; st.sketchPlanePickActive = false; }
                    }
                }
            } else if (sDatumWins) {
                // A created datum plane: refs.plane = the bare feature-id STRING (the engine's sketch
                // compiler looks it up in the results map as a PlaneResult — see document.cpp).
                planePicking_ = false; st.sketchPlanePickActive = false;
                consumePlanePick(st, nlohmann::json(sDatumFid));
            } else if (hovered >= 0) {
                planePicking_ = false; st.sketchPlanePickActive = false;
                consumePlanePick(st, planeSel[hovered]);
            }
        }
        // Top-center banner. Own the same inter-column span as the clip rect: centering against
        // the full image and then clipping to skClipMin/skClipMax left only a text fragment
        // peeking out from behind the collapsed Feature Manager on narrow web viewports.
        const char* banner = planeEditFid_.empty()
            ? "Select a plane or a planar face for the sketch \xE2\x80\x94 Esc to cancel"
            : "Select the new sketch plane or a planar face \xE2\x80\x94 Esc to cancel";
        glassHudBanner(wdl, "sk_plane_pick_banner", skClipMin, skClipMax, skHudRow2Y, banner);
        wdl->PopClipRect();
        // Esc cancels the whole thing (new-sketch pick OR an Edit-Sketch-Plane reparent).
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            planePicking_ = false; st.sketchPlanePickActive = false;
            bool wasReparent = !planeEditFid_.empty();
            planeEditFid_.clear();
            st.toast(ui::ToastLevel::Info, wasReparent ? "Edit plane cancelled" : "Sketch cancelled");
        }
        return;
    }

    if (!active_) return;
    buildBasis(*st.renderer, imgMin, vw, vh);
    if (!basisOk_) return;
    drawRelationList(st, skClipMin, skClipMax);
    char relationsName[96];
    std::snprintf(relationsName, sizeof(relationsName), "Display/Delete Relations###sketch_relations_%p",
                  static_cast<void*>(&relationsPanel_));
    const ImGuiWindow* relationsWindow = relationsPanel_.open ? ImGui::FindWindowByName(relationsName) : nullptr;
    if (relationsWindow && relationsWindow->Rect().Contains(io.MousePos)) imgHovered = false;
    const bool relationsOwnKeyboard = relationsWindow && GImGui->NavWindow &&
        GImGui->NavWindow->RootWindow == relationsWindow->RootWindow;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(skClipMin, skClipMax, true);   // dodge the pinned FeatureManager / Copilot columns

    // C37-D: crosshair cursor whenever a drawing tool or Smart Dimension is active.
    if (tool_ != SketchTool::None) st.requestCursor(ui::CursorKind::Crosshair);

    // C37-D: the in-viewport ✓/✗ confirmation corner was relocated to the Sketch-tab ribbon, so the
    // viewport's top-right no longer needs a hover-exclusion carve-out (the whole image is live).

    // C37-D / C50: in-sketch undo + redo (the global app handler skips both while a sketch is
    // active). Ctrl+Z undoes (mid-stroke pops the last point); Ctrl+Y or Ctrl+Shift+Z redoes.
    // Suppressed while a text field (the Modify box) owns the keyboard. Returns early if the
    // sketch vanished from the undo/redo.
    if (!io.WantTextInput && io.KeyCtrl) {
        if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            inSketchRedo(st);
            if (!active_) { dl->PopClipRect(); return; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            inSketchUndo(st);
            if (!active_) { dl->PopClipRect(); return; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false)) {
            inSketchRedo(st);
            if (!active_) { dl->PopClipRect(); return; }
        }
    }

    // ---- Wave 4C/4D: sketch entity copy / paste keyboard routing ----
    // Ctrl+C copies the current selection; Ctrl+V arms paste-placement from a SketchEntities
    // clipboard. Suppressed while a text field owns input, while placement is already in progress
    // (Ctrl+V then is a no-op), and (Ctrl+C) only when there's a selection. The Ctrl+X/cut path is
    // intentionally NOT bound here — deleting sketch entities already has Delete; copy+delete on
    // Ctrl+X would surprise. The global app router only forwards Ctrl+C/V to sketch mode.
    if (!io.WantTextInput && io.KeyCtrl && !pastePlacing_) {
        if (ImGui::IsKeyPressed(ImGuiKey_C, false) && !selIds_.empty())
            sketchCopySelection(st);
        else if (ImGui::IsKeyPressed(ImGuiKey_V, false) &&
                 st.clipboard.kind == AppState::Clipboard::Kind::SketchEntities)
            sketchPasteBegin(st);
    }

    // ---- Wave 4C/4D: paste-placement mode (preview drawn at the END so the existing geometry still
    // renders underneath). While placing we suppress all tool/selection INTERACTION by treating the
    // canvas as not-hovered, so a placement click never also selects/places — but grid + entities +
    // dimensions still draw normally below. The follow-cursor preview + commit/cancel run after the
    // geometry pass (search "paste-placement preview").
    if (pastePlacing_) imgHovered = false;

    // C41 (T1): Esc consumption flag. The specific Esc behaviors that KEEP the tool armed
    // (Smart Dim pick-buffer clear, Offset stage back-out, Modify box close) set this so the
    // hover-independent catch-all at the end of drawOverlay doesn't also disarm the tool.
    bool escHandled = false;
    // The relation popup is not modal; keep its clicks off the canvas below.
    if (relPopupOpen_) imgHovered = false;

    // ---- faint grid (lines along sketch u/v through the origin neighbourhood) ----
    // Off by default (SolidWorks ships with the sketch grid off); right-click > Grid toggles it.
    const ImU32 gridCol = col(pal.sketchGrid);
    const ImU32 axisCol = col(pal.sketchGrid, 2.2f);
    if (showGrid_) {
        // Estimate the visible (u,v) span from the four image corners.
        float umin = 1e9f, umax = -1e9f, vmin = 1e9f, vmax = -1e9f;
        ImVec2 corners[4] = {imgMin, ImVec2(imgMax.x, imgMin.y), ImVec2(imgMin.x, imgMax.y), imgMax};
        for (auto& c : corners) {
            float u, v; screenToUV(c, u, v);
            umin = std::fmin(umin, u); umax = std::fmax(umax, u);
            vmin = std::fmin(vmin, v); vmax = std::fmax(vmax, v);
        }
        // Clamp the line count so a zoomed-out view doesn't draw thousands of lines.
        const float gthA = 1.4f * ui::uiScale, gth = 1.0f * ui::uiScale;
        int count = 0;
        float gu0 = std::floor(umin / kGridStep) * kGridStep;
        for (float u = gu0; u <= umax && count < 400; u += kGridStep, ++count) {
            ImVec2 a = uvToScreen(u, vmin), b = uvToScreen(u, vmax);
            dl->AddLine(a, b, std::fabs(u) < 1e-3f ? axisCol : gridCol, std::fabs(u) < 1e-3f ? gthA : gth);
        }
        count = 0;
        float gv0 = std::floor(vmin / kGridStep) * kGridStep;
        for (float v = gv0; v <= vmax && count < 400; v += kGridStep, ++count) {
            ImVec2 a = uvToScreen(umin, v), b = uvToScreen(umax, v);
            dl->AddLine(a, b, std::fabs(v) < 1e-3f ? axisCol : gridCol, std::fabs(v) < 1e-3f ? gthA : gth);
        }
    }

    // ---- sketch origin marker (always visible, SolidWorks red L-arrows at (0,0)) ----
    // The origin is real, pickable geometry: snapPoint, hitTestHandle and the Smart Dimension hit-
    // test all expose it as the "origin" handle, so entities can be anchored / dimensioned to it and
    // relations added between it and other sketch geometry. When selected it renders in the accent
    // color with a filled selection ring (matching selected entities), red otherwise.
    {
        const bool oSel = isSelected("origin");
        const ImU32 oc = oSel ? canvasAccent() : col(pal.statusErr, 0.95f);
        const float th = (oSel ? 2.6f : 1.8f) * ui::uiScale;
        ImVec2 o = uvToScreen(0, 0);
        // unit u/v directions in screen space (normalized axis deltas)
        auto unit = [](ImVec2 d) {
            float L = std::hypot(d.x, d.y);
            return L > 1e-6f ? ImVec2(d.x / L, d.y / L) : ImVec2(1, 0);
        };
        ImVec2 du = unit(uAxisS_), dv = unit(vAxisS_);
        const float len = 22.0f * ui::uiScale;
        ImVec2 tu(o.x + du.x * len, o.y + du.y * len);
        ImVec2 tv(o.x + dv.x * len, o.y + dv.y * len);
        if (oSel) dl->AddCircleFilled(o, 5.5f * ui::uiScale, col(pal.accent, 0.30f), 0);
        dl->AddLine(o, tu, oc, th);
        dl->AddLine(o, tv, oc, th);
        arrowHead(dl, tu, du.x, du.y, 7.0f * ui::uiScale, oc);
        arrowHead(dl, tv, dv.x, dv.y, 7.0f * ui::uiScale, oc);
        dl->AddCircle(o, 3.5f * ui::uiScale, oc, 0, th);
    }

    // Selected datum axes (virtual sketch refs like "axis.x"): emphasize with accent stroke so
    // the pick stays visible after click, matching the origin selection treatment.
    if (st.renderer && !selIds_.empty()) {
        const auto& cam = st.renderer->camera();
        const float L = cam.radius;   // matches hitTestDatumAxis / drawn half-length (factor 1.0)
        const auto& rds = st.renderer->refDatums();
        for (const auto& sid : selIds_) {
            if (sid.rfind("axis.", 0) != 0) continue;
            Vec3 P(0, 0, 0), D(0, 0, 0);
            bool found = false;
            for (const auto& d : rds) {
                if (d.kind != Renderer::RefDatum::Kind::Axis || d.id != sid) continue;
                P = d.origin; D = d.dir; found = true; break;
            }
            if (!found) {
                if (sid == "axis.x") { D = Vec3(1, 0, 0); found = true; }
                else if (sid == "axis.y") { D = Vec3(0, 1, 0); found = true; }
                else if (sid == "axis.z") { D = Vec3(0, 0, 1); found = true; }
            }
            if (!found || length(D) < 1e-9f) continue;
            D = normalize(D);
            ImVec2 sa, sb;
            if (!worldToScreen(cam, imgMin, vw, vh,
                               P.x - D.x * L, P.y - D.y * L, P.z - D.z * L, sa))
                continue;
            if (!worldToScreen(cam, imgMin, vw, vh,
                               P.x + D.x * L, P.y + D.y * L, P.z + D.z * L, sb))
                continue;
            dl->AddLine(sa, sb, canvasAccent(), 3.5f * ui::uiScale);
        }
    }
    // ---- C51: closed-profile region shading (under the entities) ----
    // Loops detected from the live geometry are filled faintly so a closed profile reads as a
    // selectable face; the one the cursor has dwelled in (hotRegion_) brightens + gets an outline,
    // signalling that a click will grab its whole loop. Drawn BEFORE the entities so the boundary
    // lines stay crisp on top. ONLY material (even-depth) regions are shaded. C56c: the fill is
    // TRIANGULATED with true holes (bridge + ear clip) so a nested closed profile reads as a real
    // donut hole with the live canvas showing through — no opaque punch (the sketch backdrop is
    // the 3D viewport, which no flat punch color can match). Legacy fill+punch remains as the
    // fallback for pathological loops the triangulator rejects.
    if (regionsDirty_) {
        rebuildRegions();
        for (auto& rg : regions_) {
            std::vector<std::vector<ImVec2>> holes;
            holes.reserve(rg.holeChildren.size());
            for (int hi : rg.holeChildren)
                if (hi >= 0 && hi < (int)regions_.size()) holes.push_back(regions_[(size_t)hi].poly);
            skTriangulateWithHoles(rg.poly, holes, rg.triangles);
        }
        regionsDirty_ = false;
    }
    if (hotRegion_ >= (int)regions_.size()) hotRegion_ = -1;
    {
        const ImU32 fillCol = col(pal.accent, 0.13f);
        const ImU32 hotFill = col(pal.accent, 0.28f);
        const ImU32 hotEdge = col(pal.accent, 0.65f);
        const ImU32 holePunch = col(pal.viewportTop, 1.0f);   // legacy fallback only
        // Draw shallow material first so deeper islands land on top of outer holes (triple-nested:
        // outer ring / void / island).
        std::vector<int> matOrder;
        matOrder.reserve(regions_.size());
        for (size_t ri = 0; ri < regions_.size(); ++ri)
            if (regions_[ri].isMaterial) matOrder.push_back((int)ri);
        std::sort(matOrder.begin(), matOrder.end(), [&](int a, int b) {
            return regions_[(size_t)a].depth < regions_[(size_t)b].depth;
        });
        for (int ri : matOrder) {
            const auto& rg = regions_[(size_t)ri];
            bool hot = (tool_ == SketchTool::None && ri == hotRegion_);
            std::vector<ImVec2> sp; sp.reserve(rg.poly.size());
            for (const auto& p : rg.poly) sp.push_back(uvToScreen(p.x, p.y));
            std::vector<std::vector<ImVec2>> holePolys;
            for (int hi : rg.holeChildren) {
                if (hi < 0 || hi >= (int)regions_.size()) continue;
                std::vector<ImVec2> hp;
                for (const auto& p : regions_[(size_t)hi].poly) hp.push_back(uvToScreen(p.x, p.y));
                if (hp.size() >= 3) holePolys.push_back(std::move(hp));
            }
            const ImU32 fill = hot ? hotFill : fillCol;
            if (!rg.triangles.empty()) {
                for (size_t t = 0; t + 2 < rg.triangles.size(); t += 3)
                    dl->AddTriangleFilled(uvToScreen(rg.triangles[t].x, rg.triangles[t].y),
                                          uvToScreen(rg.triangles[t+1].x, rg.triangles[t+1].y),
                                          uvToScreen(rg.triangles[t+2].x, rg.triangles[t+2].y), fill);
            } else {
                // Fallback: fill the outer loop and punch holes with opaque canvas.
                dl->AddConcavePolyFilled(sp.data(), (int)sp.size(), fill);
                for (const auto& hp : holePolys)
                    dl->AddConcavePolyFilled(hp.data(), (int)hp.size(), holePunch);
            }
            if (hot) dl->AddPolyline(sp.data(), (int)sp.size(), hotEdge, ImDrawFlags_Closed,
                                     1.5f * ui::uiScale);
        }
    }

    // ---- C-bug2: relation badges (geometric / non-dimensional constraints) ----
    // Drawn HERE, BENEATH the entity lines, so geometry stays crisp on top (the user complaint was
    // badges sitting on the lines). Three phases: (1) collect each badge's initial chip center plus
    // the geometry point it annotates, (2) relax overlapping chips apart, (3) draw a hairline leader
    // from the geometry to any displaced chip, then the chip. Dimension annotations still render
    // AFTER the entity loop (they read better on top of geometry).
    {
        const float bs = 16.0f * ui::uiScale;                 // chip side
        const ImU32 relBg        = col(pal.sketchRelBg);
        const ImU32 relBorder    = col(pal.sketchRelBorder);
        const ImU32 relInk       = col(pal.sketchInk);
        const ImU32 relSelBg     = col(pal.accent, 0.85f);
        const ImU32 relSelBorder = col(pal.accent);
        const ImU32 relSelInk    = col(pal.statusBg);         // dark glyph reads on the accent fill
        const ImU32 dimErrCol    = col(pal.statusErr);        // conflicting/redundant relations -> red
        const ImU32 leaderCol    = col(pal.sketchRelBorder, 0.4f);
        auto isConflict = [&](const std::string& cid) {
            return std::find(conflicting_.begin(), conflicting_.end(), cid) != conflicting_.end()
                || std::find(redundant_.begin(),  redundant_.end(),  cid) != redundant_.end();
        };
        auto baseId = [](const std::string& h) { return h.substr(0, h.find('.')); };
        // Screen point a ref handle resolves to ("e3" entity, "e3.p2" point handle, or "origin").
        auto refScreen = [&](const std::string& h, ImVec2& s) -> bool {
            float u, v;
            if (!relationPointUV(h, u, v)) return false;
            s = uvToScreen(u, v);
            return true;
        };
        // Geometry point a badge POINTS AT: entity midpoint / the point / arc rim mid-sweep.
        auto refAttach = [&](const std::string& h, ImVec2& s) -> bool {
            if (h.find('.') != std::string::npos || baseId(h) == "origin") return refScreen(h, s);
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            if (e->type == "line" || e->type == "slot") {
                ImVec2 a = uvToScreen(e->p1[0], e->p1[1]), b = uvToScreen(e->p2[0], e->p2[1]);
                s = ImVec2((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
                return true;
            }
            if (e->type == "circle" || e->type == "arc" || e->type == "arc_slot") {
                float aMid = 45.0f;
                if (e->type != "circle") {
                    float a0 = e->start, a1 = e->end; if (a1 < a0) a1 += 360.0f;
                    aMid = (a0 + a1) * 0.5f;
                }
                float ar = aMid * 3.14159265f / 180.0f;
                s = uvToScreen(e->center[0] + e->radius * std::cos(ar),
                               e->center[1] + e->radius * std::sin(ar));
                return true;
            }
            return refScreen(h, s);
        };
        // Initial chip center: the attachment point pushed clear of the geometry line.
        auto badgeAnchor = [&](const std::string& h, ImVec2& out) -> bool {
            const float off = 10.0f * ui::uiScale + bs * 0.5f;
            if (h.find('.') != std::string::npos || baseId(h) == "origin") {
                ImVec2 s; if (!refScreen(h, s)) return false;
                out = ImVec2(s.x + off * 0.8f, s.y - off * 0.8f);
                return true;
            }
            const SketchEntity* e = entById(baseId(h));
            if (!e) return false;
            if (e->type == "line" || e->type == "slot") {
                ImVec2 a = uvToScreen(e->p1[0], e->p1[1]), b = uvToScreen(e->p2[0], e->p2[1]);
                ImVec2 m((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
                float dx = b.x - a.x, dy = b.y - a.y; float L = std::hypot(dx, dy);
                float nx = L > 1e-3f ? -dy / L : 0.0f, ny = L > 1e-3f ? dx / L : -1.0f;
                out = ImVec2(m.x + nx * off, m.y + ny * off);
                return true;
            }
            if (e->type == "circle" || e->type == "arc" || e->type == "arc_slot") {
                float aMid = 45.0f;
                if (e->type != "circle") {
                    float a0 = e->start, a1 = e->end; if (a1 < a0) a1 += 360.0f;
                    aMid = (a0 + a1) * 0.5f;
                }
                float ar = aMid * 3.14159265f / 180.0f;
                ImVec2 cc = uvToScreen(e->center[0], e->center[1]);
                ImVec2 rim = uvToScreen(e->center[0] + e->radius * std::cos(ar),
                                        e->center[1] + e->radius * std::sin(ar));
                float dx = rim.x - cc.x, dy = rim.y - cc.y; float L = std::hypot(dx, dy);
                if (L > 1e-3f) out = ImVec2(rim.x + dx / L * off, rim.y + dy / L * off);
                else           out = ImVec2(cc.x + off, cc.y - off);
                return true;
            }
            ImVec2 s; if (!refScreen(h, s)) return false;
            out = ImVec2(s.x + off * 0.8f, s.y - off * 0.8f);
            return true;
        };
        // Hover/selection/status only affect paint, not geometry-and-camera badge layout.
        const std::array<float, 7> camera = {originS_.x, originS_.y, uAxisS_.x, uAxisS_.y,
                                            vAxisS_.x, vAxisS_.y, ui::uiScale};
        if (badgesRevision_ != entitiesRevision_ || badgesCamera_ != camera) {
            relationBadges_.clear();
            auto pushBadge = [&](const SketchConstraint& c, const std::string& h) {
                ImVec2 pos, anchor;
                if (!badgeAnchor(h, pos)) return;
                if (!refAttach(h, anchor)) anchor = pos;
                std::string type = c.type;
                if (type == "coincident")
                    for (const auto& ref : c.refs)
                        if (const auto* e = entById(skBaseId(ref)); e && e->externalPierce)
                            type = "pierce";
                relationBadges_.push_back({c.id, std::move(type), pos, anchor});
            };
            for (const auto& c : cons_) {
                if (c.isDimensional() || c.refs.empty()) continue;
                const bool pair = c.type == "parallel" || c.type == "perpendicular" ||
                    c.type == "equal" || c.type == "tangent" || c.type == "concentric" ||
                    c.type == "collinear" || c.type == "coradial" || c.type == "symmetric" ||
                    c.type == "smooth" || c.type == "equal_curve_length";
                if (pair) { for (const auto& h : c.refs) pushBadge(c, h); }
                else pushBadge(c, c.refs.front());
            }
            // Spatial occupancy with path-compressed next-free rows avoids all-pairs scans.
            const float cell = bs + 2.0f * ui::uiScale;
            std::unordered_map<uint64_t, int> next;
            next.reserve(relationBadges_.size());
            auto key = [](int x, int y) {
                return (uint64_t(uint32_t(x)) << 32) | uint32_t(y);
            };
            for (auto& b : relationBadges_) {
                const int x = (int)std::floor(b.pos.x / cell);
                const int start = (int)std::floor(b.pos.y / cell);
                int y = start;
                for (;;) {
                    const auto it = next.find(key(x, y));
                    if (it == next.end()) break;
                    y = it->second;
                }
                for (int row = start; row != y;) {
                    auto& jump = next[key(x, row)];
                    const int old = jump; jump = y + 1; row = old;
                }
                next[key(x, y)] = y + 1;
                b.pos = ImVec2((x + 0.5f) * cell, (y + 0.5f) * cell);
            }
            badgesRevision_ = entitiesRevision_;
            badgesCamera_ = camera;
        }
        // (3) Draw: hairline leader (only when the chip drifted off its anchor) + chip + hit-rect.
        relHits_.clear();
        for (const auto& b : relationBadges_) {
            const ImVec2 gp = b.pos;
            if (std::hypot(gp.x - b.anchor.x, gp.y - b.anchor.y) > bs)
                dl->AddLine(b.anchor, gp, leaderCol, 0.8f * ui::uiScale);
            ImVec2 c0(gp.x - bs * 0.5f, gp.y - bs * 0.5f), c1(gp.x + bs * 0.5f, gp.y + bs * 0.5f);
            const bool hot = isRelSelected(b.cid) || hoverRelCid_ == b.cid || panelHoverRel_ == b.cid;
            const bool conflict = isConflict(b.cid);
            const auto skipped = skipped_.find(b.cid);
            const ImU32 problem = conflict ? dimErrCol : col(pal.statusWarn);
            const bool flagged = conflict || skipped != skipped_.end();
            dl->AddRectFilled(c0, c1, hot ? relSelBg : relBg, 3.5f * ui::uiScale);
            dl->AddRect(c0, c1, flagged ? problem : (hot ? relSelBorder : relBorder),
                        3.5f * ui::uiScale, 0, ((flagged || hot) ? 1.8f : 1.2f) * ui::uiScale);
            drawRelationGlyph(dl, gp, bs, b.type.c_str(),
                              flagged ? problem : (hot ? relSelInk : relInk));
            relHits_.push_back({b.cid, c0, c1});
            if (imgHovered && io.MousePos.x >= c0.x && io.MousePos.x <= c1.x &&
                io.MousePos.y >= c0.y && io.MousePos.y <= c1.y)
                ImGui::SetTooltip("%s%s%s", b.type.c_str(), skipped != skipped_.end() ? ": " : "",
                                  skipped != skipped_.end() ? skipped->second.c_str() : "");
        }
    }

    // ---- entities (color by solver status via theme roles) ----
    auto statusCol = [&](const SketchEntity& e) -> ImU32 {
        if (e.status == "over") return col(pal.statusErr);
        // T-BL49: external-ref (Convert Entities) provenance beats the solved-ink color —
        // violet when live, warning amber when the source no longer resolves (stale).
        if (e.externalStale) return col(pal.sketchExternalStale);
        if (e.external) return col(pal.sketchExternal);
        if (e.status == "full") return col(pal.sketchInk);
        return col(pal.sketchUnder);   // under-defined (blue-ish)
    };
    // Construction dashes: SolidWorks centerline style, dash=6px gap=4px at scale 1.
    const float kConDash = 6.0f * ui::uiScale;
    const float kConGap  = 4.0f * ui::uiScale;
    // Stroke one segment — solid for normal geometry, dashed for construction.
    // addSeg accumulates points into a local vector so the polyline dash phase is continuous.
    // DPI-scaled marker sizes shared by the entity loop (endpoint/center dots, point crosses).
    const float dotR = 3.0f * ui::uiScale, ctrR = 2.5f * ui::uiScale;
    for (const auto& e0 : ents_) {
        // T-BL49: a STALE external-ref entity draws DASHED (warning styling) — reuse the
        // construction dash path on a per-draw copy so every entity-type branch below
        // inherits it without touching the stored construction flag.
        SketchEntity e = e0;
        if (e.externalStale) e.construction = true;
        // C41 (T2): selected entities render in the (canvas-darkened) accent, slightly thicker.
        bool sel = !selIds_.empty() && isSelected(e.id);
        ImU32 cc = sel ? canvasAccent() : statusCol(e);
        float th = ((e0.construction ? 1.0f : 2.0f) + (sel ? 1.0f : 0.0f)) * ui::uiScale;
        // Helper: stroke a polyline; uses dashed rendering for construction entities.
        auto strokePoly = [&](const std::vector<ImVec2>& pts) {
            if (e.construction) {
                drawDashedPolyline(dl, pts, cc, th, kConDash, kConGap);
            } else {
                for (size_t i = 1; i < pts.size(); ++i)
                    dl->AddLine(pts[i-1], pts[i], cc, th);
            }
        };
        if (e.type == "line") {
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            if (e.construction) {
                drawDashedLine(dl, a, b, cc, th, kConDash, kConGap);
            } else {
                dl->AddLine(a, b, cc, th);
            }
            dl->AddCircleFilled(a, dotR, cc);
            dl->AddCircleFilled(b, dotR, cc);
        } else if (e.type == "circle") {
            // Sample the circle in (u,v) so it follows the plane mapping exactly; segment count
            // tracks the projected radius so zoomed-in circles never show facets.
            ImVec2 sc0 = uvToScreen(e.center[0], e.center[1]);
            ImVec2 sr0 = uvToScreen(e.center[0] + e.radius, e.center[1]);
            int seg = circleSegs(std::hypot(sr0.x - sc0.x, sr0.y - sc0.y));
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                pts.push_back(uvToScreen(e.center[0] + e.radius * std::cos(a),
                                         e.center[1] + e.radius * std::sin(a)));
            }
            strokePoly(pts);
            dl->AddCircleFilled(sc0, ctrR, cc);
        } else if (e.type == "arc") {
            float a0 = e.start, a1 = e.end;
            if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
            if (a1 < a0) a1 += 360.0f;
            ImVec2 sc0 = uvToScreen(e.center[0], e.center[1]);
            ImVec2 sr0 = uvToScreen(e.center[0] + e.radius, e.center[1]);
            float sweepFrac = (a1 - a0) / 360.0f;
            int seg = std::max(12, (int)(circleSegs(std::hypot(sr0.x - sc0.x, sr0.y - sc0.y)) *
                                         sweepFrac));
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = (a0 + (a1 - a0) * (i / (float)seg)) * 3.14159265f / 180.0f;
                pts.push_back(uvToScreen(e.center[0] + e.radius * std::cos(a),
                                         e.center[1] + e.radius * std::sin(a)));
            }
            strokePoly(pts);
        } else if (e.type == "slot") {
            // Closed capsule profile: two side lines tangent to the caps (parallel to the spine,
            // offset by the half-width = end radius) plus a trimmed semicircle arc at each end.
            // The spine itself renders as a construction centerline, and the trimmed-away inner
            // halves of each end circle render as construction too (as if trimmed by hand).
            float dx = e.p2[0] - e.p1[0], dy = e.p2[1] - e.p1[1];
            float L = std::hypot(dx, dy);
            float hw = e.width * 0.5f;
            float nx = L > 1e-6f ? -dy / L : 0.0f, ny = L > 1e-6f ? dx / L : 0.0f;
            ImVec2 s1a = uvToScreen(e.p1[0] + nx * hw, e.p1[1] + ny * hw);
            ImVec2 s1b = uvToScreen(e.p2[0] + nx * hw, e.p2[1] + ny * hw);
            ImVec2 s2a = uvToScreen(e.p1[0] - nx * hw, e.p1[1] - ny * hw);
            ImVec2 s2b = uvToScreen(e.p2[0] - nx * hw, e.p2[1] - ny * hw);
            float axis1 = std::atan2(e.p1[1] - e.p2[1], e.p1[0] - e.p2[0]);  // outward at p1
            float axis2 = std::atan2(e.p2[1] - e.p1[1], e.p2[0] - e.p1[0]);  // outward at p2
            // Sample a half-circle arc (180 deg centered on `axis`) around end `c`.
            auto halfArc = [&](const float* c, float axis) {
                std::vector<ImVec2> pts; pts.reserve(25);
                for (int i = 0; i <= 24; ++i) {
                    float ang = axis - 1.5707963f + 3.14159265f * (i / 24.0f);
                    pts.push_back(uvToScreen(c[0] + hw * std::cos(ang), c[1] + hw * std::sin(ang)));
                }
                return pts;
            };
            ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
            if (e.construction) {
                drawDashedLine(dl, s1a, s1b, cc, th, kConDash, kConGap);
                drawDashedLine(dl, s2a, s2b, cc, th, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p1, axis1), cc, th, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p2, axis2), cc, th, kConDash, kConGap);
            } else {
                dl->AddLine(s1a, s1b, cc, th);
                dl->AddLine(s2a, s2b, cc, th);
                strokePoly(halfArc(e.p1, axis1));   // outer (kept) cap halves
                strokePoly(halfArc(e.p2, axis2));
                // construction: centerline spine + the trimmed-away inner half of each end circle
                const ImU32 conCol = sel ? cc : col(pal.sketchUnder, 0.85f);
                const float conTh = 1.0f * ui::uiScale;
                drawDashedLine(dl, a, b, conCol, conTh, kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p1, axis1 + 3.14159265f), conCol, conTh,
                                   kConDash, kConGap);
                drawDashedPolyline(dl, halfArc(e.p2, axis2 + 3.14159265f), conCol, conTh,
                                   kConDash, kConGap);
            }
        } else if (e.type == "arc_slot") {
            // Capsule outline around a circular spine (center, radius; CCW a0->a1 in start/end).
            // Outer arc (r+hw), inner arc (r-hw), and a half-circle cap at each spine endpoint.
            const float D2R = 3.14159265f / 180.0f;
            float hw = e.width * 0.5f;
            float a0 = e.start, a1 = e.end; if (a1 < a0) a1 += 360.0f;
            auto arcPts = [&](float rad, int seg) {
                std::vector<ImVec2> pts; pts.reserve(seg + 1);
                for (int i = 0; i <= seg; ++i) {
                    float a = (a0 + (a1 - a0) * (i / (float)seg)) * D2R;
                    pts.push_back(uvToScreen(e.center[0] + rad * std::cos(a),
                                             e.center[1] + rad * std::sin(a)));
                }
                return pts;
            };
            auto capPts = [&](float aDeg) {
                float sa = aDeg * D2R;
                float sx = e.center[0] + e.radius * std::cos(sa);
                float sy = e.center[1] + e.radius * std::sin(sa);
                int seg = 24;
                std::vector<ImVec2> pts; pts.reserve(seg + 1);
                for (int i = 0; i <= seg; ++i) {
                    float t = (aDeg - 90.0f + 180.0f * (i / (float)seg)) * D2R;
                    pts.push_back(uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t)));
                }
                return pts;
            };
            strokePoly(arcPts(e.radius + hw, 64));
            strokePoly(arcPts(e.radius - hw, 64));
            strokePoly(capPts(e.start));
            strokePoly(capPts(e.end));
        } else if (e.type == "point") {
            // C42: dot + small cross so a lone point reads as geometry, not a stray handle.
            ImVec2 p = uvToScreen(e.p1[0], e.p1[1]);
            const float cx6 = 6.0f * ui::uiScale, cw = 1.2f * ui::uiScale;
            dl->AddCircleFilled(p, dotR, cc);
            dl->AddLine(ImVec2(p.x - cx6, p.y), ImVec2(p.x + cx6, p.y), cc, cw);
            dl->AddLine(ImVec2(p.x, p.y - cx6), ImVec2(p.x, p.y + cx6), cc, cw);
        } else if (e.type == "ellipse") {
            // C42: parametric sample with the rotation applied (rot stored in degrees).
            const float D2R = 3.14159265f / 180.0f;
            float ca = std::cos(e.rot * D2R), sa = std::sin(e.rot * D2R);
            int seg = 64;
            std::vector<ImVec2> pts; pts.reserve(seg + 1);
            for (int i = 0; i <= seg; ++i) {
                float a = i / (float)seg * 6.2831853f;
                float lx = e.rx * std::cos(a), ly = e.ry * std::sin(a);
                pts.push_back(uvToScreen(e.center[0] + lx * ca - ly * sa,
                                         e.center[1] + lx * sa + ly * ca));
            }
            strokePoly(pts);
            dl->AddCircleFilled(uvToScreen(e.center[0], e.center[1]), ctrR, cc);
        } else if (e.type == "spline" || e.type == "style_spline") {
            // C42/M-1: an interpolating "spline" draws the SMOOTH curve through its points
            // (Catmull-Rom), so the viewport matches the geometry the engine sweeps/revolves.
            // A "style_spline" is a by-poles curve — keep showing its control polygon (the
            // SolidWorks convention) plus its pole markers.
            const std::vector<ImVec2>& src =
                (e.type == "spline") ? catmullRomUV(e.pts) : e.pts;
            std::vector<ImVec2> pts; pts.reserve(src.size());
            for (const auto& q : src) pts.push_back(uvToScreen(q.x, q.y));
            strokePoly(pts);
            for (const auto& q : e.pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), ctrR, cc);
        }
    }

    // C50: selected POINT handles (vertices/centers picked for relations) — filled accent squares.
    // Entity-level selection already thickens the whole entity above; this marks sub-handle picks.
    for (const auto& selh : selIds_) {
        auto dot = selh.find('.');
        if (dot == std::string::npos) continue;   // bare entity id, handled in the entity loop
        const SketchEntity* e = entById(selh.substr(0, dot));
        if (!e) continue;
        std::string sub = selh.substr(dot + 1);
        ImVec2 p;
        if (sub == "p1")          p = uvToScreen(e->p1[0], e->p1[1]);
        else if (sub == "p2")     p = uvToScreen(e->p2[0], e->p2[1]);
        else if (sub == "center") p = uvToScreen(e->center[0], e->center[1]);
        else if (sub == "p")      p = uvToScreen(e->p1[0], e->p1[1]);
        else if (sub == "start_angle") {
            const float D2R = 3.14159265f / 180.0f;
            p = uvToScreen(e->center[0] + e->radius * std::cos(e->start * D2R),
                           e->center[1] + e->radius * std::sin(e->start * D2R));
        }
        else if (sub == "end_angle") {
            const float D2R = 3.14159265f / 180.0f;
            p = uvToScreen(e->center[0] + e->radius * std::cos(e->end * D2R),
                           e->center[1] + e->radius * std::sin(e->end * D2R));
        }
        else if (sub == "rim") { continue; }   // floating cursor-dependent handle, not persisted
        else continue;
        const float hs = 4.0f * ui::uiScale;
        dl->AddRectFilled(ImVec2(p.x - hs, p.y - hs), ImVec2(p.x + hs, p.y + hs),
                          canvasAccent(), 1.5f * ui::uiScale);
    }

    // ---- C37-D: dimension annotations (always rendered, ON TOP of entity lines) ----
    {
        const ImU32 dimLineCol = col(pal.sketchDimLine, 0.85f);
        const ImU32 dimTextCol = col(pal.sketchInk);
        // Driven (reference) dims: dimmed CANVAS ink — consoleDim is a panel role and is light
        // grey in the dark theme, which washed out on the always-light canvas.
        const ImU32 dimDrivenCol = col(pal.sketchInk, 0.55f);
        const ImU32 dimHoverCol = canvasAccent();           // dark enough on the light canvas
        const ImU32 dimErrCol = col(pal.statusErr);         // C50f: conflicting dims/badges in red
        // C50f: is this constraint id one the solver flagged as conflicting? Conflicting dims/badges
        // render in the error color (SolidWorks paints fighting relations red), matching the entity
        // coloring already done by statusCol.
        auto isConflict = [&](const std::string& cid) {
            // Redundant driving dims count too: a consistent-but-redundant dim still over-defines
            // (SolidWorks paints it red the moment you keep it driving), and the redundant tag is
            // the only hint which dim of the dependent set to delete or make driven.
            return std::find(conflicting_.begin(), conflicting_.end(), cid) != conflicting_.end()
                || std::find(redundant_.begin(),  redundant_.end(),  cid) != redundant_.end();
        };
        // Dimension annotations. C50g: SolidWorks-style picking — a dim is hovered when the
        // mouse is inside its TEXT box (padded ~4px*uiScale) or within ~6px*uiScale of its
        // DIMENSION LINE (not the extension lines). We draw every dim non-hovered first while
        // collecting per-dim hit geometry, resolve the hover (text beats line, then nearest),
        // then re-draw just the winner on top in the hover color. Drawing the geometry is
        // independent of the hover flag (only thickness/color differ), so the overlap is exact
        // and the placement math lives in one place (drawDimension) — no drift.
        struct DimDrawn { const SketchConstraint* c; float pu, pv; bool conflict; DimHit hit; };
        std::vector<DimDrawn> drawn;
        drawn.reserve(cons_.size());
        for (const auto& c : cons_) {
            if (!c.isDimensional()) continue;
            // placement: stored -> autoPlace_ cache -> default (persist into the cache lazily).
            float pu, pv;
            bool dragging = dimDragging_ && dimDragCid_ == c.id;
            if (dragging) { pu = dimDragPlace_.x; pv = dimDragPlace_.y; }
            else if (c.hasPlacement) { pu = c.placement[0]; pv = c.placement[1]; }
            else {
                auto it = autoPlace_.find(c.id);
                if (it != autoPlace_.end()) { pu = it->second.x; pv = it->second.y; }
                else if (defaultPlacement(c, pu, pv)) autoPlace_[c.id] = ImVec2(pu, pv);
                else continue;
            }
            bool conflict = isConflict(c.id);
            const bool skipped = skipped_.find(c.id) != skipped_.end();
            ImU32 lc = conflict ? dimErrCol : skipped ? col(pal.statusWarn) : dimLineCol;
            ImU32 tc = conflict ? dimErrCol : skipped ? col(pal.statusWarn) :
                       (c.driven ? dimDrivenCol : dimTextCol);
            DimHit hit; hit.cid = c.id;
            drawDimension(dl, c, lc, tc, false, pu, pv, &hit);
            drawn.push_back({&c, pu, pv, conflict, hit});
        }
        // Resolve hover from the collected geometry. Prefer a TEXT-box hit over a line hit; among
        // equals, nearest wins (text: distance to box center; line: perpendicular distance).
        std::string newHover;
        if (imgHovered) {
            const ImVec2 m = io.MousePos;
            const float lineThresh = 6.0f * ui::uiScale;
            const float lineThresh2 = lineThresh * lineThresh;
            auto segDist2 = [](const ImVec2& p, const ImVec2& a, const ImVec2& b) {
                float vx = b.x - a.x, vy = b.y - a.y; float L2 = vx * vx + vy * vy;
                float t = L2 > 1e-6f ? ((p.x - a.x) * vx + (p.y - a.y) * vy) / L2 : 0.0f;
                if (t < 0) t = 0; else if (t > 1) t = 1;
                float cx = a.x + vx * t, cy = a.y + vy * t;
                return (p.x - cx) * (p.x - cx) + (p.y - cy) * (p.y - cy);
            };
            bool haveText = false; float bestText2 = 0, bestLine2 = 0;
            for (const auto& d : drawn) {
                const DimHit& h = d.hit;
                bool inText = m.x >= h.textMin.x && m.x <= h.textMax.x &&
                              m.y >= h.textMin.y && m.y <= h.textMax.y;
                if (inText) {
                    float cx = (h.textMin.x + h.textMax.x) * 0.5f, cy = (h.textMin.y + h.textMax.y) * 0.5f;
                    float d2 = (m.x - cx) * (m.x - cx) + (m.y - cy) * (m.y - cy);
                    if (!haveText || d2 < bestText2) { haveText = true; bestText2 = d2; newHover = h.cid; }
                    continue;
                }
                if (haveText) continue;   // a text hit already beats any line hit
                float ld2 = lineThresh2;
                for (int s = 0; s < h.nSeg; ++s)
                    ld2 = std::min(ld2, segDist2(m, h.seg[s][0], h.seg[s][1]));
                if (ld2 < lineThresh2 && (newHover.empty() || ld2 < bestLine2)) {
                    bestLine2 = ld2; newHover = h.cid;
                }
            }
        }
        // Re-draw the hovered dim on top in the hover color (exact overlap with its first pass).
        if (!newHover.empty()) {
            for (const auto& d : drawn) {
                if (d.c->id != newHover) continue;
                drawDimension(dl, *d.c, dimHoverCol, dimHoverCol, true, d.pu, d.pv);
                break;
            }
        }
        dimHoverCid_ = newHover;
    }

    if (!panelHoverRel_.empty()) {
        for (const auto& c : cons_) {
            if (c.id != panelHoverRel_) continue;
            for (const auto& h : c.refs) {
                const auto* e = entById(skBaseId(h));
                if (h == "axis.h" || h == "axis.v") {
                    const ImVec2 origin = uvToScreen(0, 0);
                    const ImVec2 direction = h == "axis.h" ? uAxisS_ : vAxisS_;
                    const float length = std::hypot(direction.x, direction.y);
                    if (length > 1e-6f) {
                        const float span = (imgMax.x - imgMin.x + imgMax.y - imgMin.y) / length;
                        dl->AddLine(ImVec2(origin.x - direction.x * span, origin.y - direction.y * span),
                                    ImVec2(origin.x + direction.x * span, origin.y + direction.y * span),
                                    canvasAccent(), 2.0f * ui::uiScale);
                    }
                } else if (e && e->type != "point" && h.find('.') == std::string::npos)
                    strokeEntityOutline(dl, *e, canvasAccent(), 3.0f * ui::uiScale);
                else {
                    float u, v;
                    if (relationPointUV(h, u, v))
                        dl->AddCircle(uvToScreen(u, v), 6.0f * ui::uiScale,
                                      canvasAccent(), 0, 2.0f * ui::uiScale);
                }
            }
        }
    }

    // ---- selection / drag handles + interaction ----
    float mu = 0, mv = 0;
    if (imgHovered) screenToUV(io.MousePos, mu, mv);

    // ---- C37-D: dimension text drag + double-click-to-edit + Delete ----
    // C47: also active while Smart Dimension is armed (SolidWorks lets you drag/edit existing
    // dims mid-dimensioning); the SmartDim pick-click below skips text-hovered clicks.
    if ((tool_ == SketchTool::None || tool_ == SketchTool::SmartDim) && !dimPopupOpen_ &&
        !modelSelectionAllowed()) {
        // Begin a drag on the hovered dim text. C38 (S4): the second click of a double-click also
        // fires IsMouseClicked; exclude it so a double-click-to-edit never spawns a phantom drag
        // (which would commit a stray sketch_set_dim placement on release).
        if (imgHovered && !dimDragging_ && !dragging_ && !dimHoverCid_.empty() &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            !ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            dimDragging_ = true; dimDragCid_ = dimHoverCid_; dimDragPlace_ = ImVec2(mu, mv);
        }
        if (dimDragging_) {
            dimDragPlace_ = ImVec2(mu, mv);
            if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                // Persist the new placement via sketch_set_dim {placement}.
                nlohmann::json op = {{"op", "sketch_set_dim"}, {"sketch", fid_}, {"id", dimDragCid_},
                    {"placement", nlohmann::json::array({dimDragPlace_.x, dimDragPlace_.y})}};
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                if (!r.ok) st.toast(ui::ToastLevel::Warn, "Move dimension: " + r.error_message);
                else { autoPlace_[dimDragCid_] = dimDragPlace_;
                       if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                       resolve(st); }
                dimDragging_ = false; dimDragCid_.clear();
            }
        }
        // Double-click a dim text -> reopen the Modify box prefilled.
        if (imgHovered && !dimHoverCid_.empty() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
            // C38 (S4): if a drag somehow began on the first click of this double-click, cancel it so
            // its release-handler can't also fire a placement op behind the edit box.
            dimDragging_ = false; dimDragCid_.clear();
            for (const auto& c : cons_) {
                if (c.id != dimHoverCid_) continue;
                dimEditCid_ = c.id; dimType_ = c.type; dimIsAngle_ = (c.type == "angle");
                dimMeasured_ = c.hasMeasured ? c.measured : (c.hasValue ? c.value : 0.0);
                dimPopupDriven_ = c.driven;
                if (c.hasExpr) std::snprintf(dimBuf_.data(), dimBuf_.size(), "=%s", c.expr.c_str());
                else std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
                dimError_.clear(); dimPopupOpen_ = true; dimPopupFocus_ = true; dimPopupPos_ = io.MousePos;
                break;
            }
        }
        // C41 (T2): Delete/Backspace removes the selected entities (one ops array for ALL of
        // them, then re-solve). Virtual refs (origin / datum axes) are skipped. A hovered
        // dimension text takes priority (handler below) — keep this gated on dimHoverCid_.empty()
        // and BEFORE that handler (it clears the hover id).
        if (!relationsOwnKeyboard && (!selIds_.empty() || !selRelIds_.empty()) &&
            dimHoverCid_.empty() && !io.WantTextInput &&
            (ImGui::IsKeyPressed(ImGuiKey_Delete) || ImGui::IsKeyPressed(ImGuiKey_Backspace))) {
            // C50: selection may hold point handles ("e3.p2") — delete the OWNING entity, deduped.
            // C51: selected relations are deleted in the same batch via sketch_delete_constraint.
            nlohmann::json ops = nlohmann::json::array();
            std::vector<std::string> done;
            for (const auto& sel : selIds_) {
                std::string base = sel.substr(0, sel.find('.'));
                // Virtual sketch refs (origin, datum axes) cannot be deleted as entities.
                if (base == "origin" || base.rfind("axis.", 0) == 0) continue;
                if (std::find(done.begin(), done.end(), base) != done.end()) continue;
                // Only emit deletes for entities that still exist (stale handles are no-ops).
                if (!entById(base)) continue;
                done.push_back(base);
                ops.push_back({{"op", "sketch_delete_entity"}, {"sketch", fid_}, {"id", base}});
            }
            for (const auto& cid : selRelIds_)
                ops.push_back({{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", cid}});
            // Empty ops (only virtual refs selected) — just clear selection, no RPC.
            if (!ops.empty()) {
                auto r = st.docMirror.applyOps(st.worker, ops);
                if (!r.ok) st.toast(ui::ToastLevel::Error, "Delete: " + r.error_message);
                else {
                    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    resolve(st);
                    // Emptied a consumed sketch (select-all + delete, etc.): warn once.
                    // Dependents stay with rebuild errors; don't cascade-delete them.
                    if (ents_.empty()) {
                        std::vector<std::string> deps;
                        collectSketchDependents(st, fid_, deps);
                        if (!deps.empty()) {
                            st.toast(ui::ToastLevel::Warn,
                                     emptyProfileToast(deps, featureDisplayName(st, fid_)));
                        }
                    }
                }
            }
            selIds_.clear(); selRelIds_.clear();
        }
        // Delete key removes the hovered dim.
        if (!relationsOwnKeyboard && !dimHoverCid_.empty() && ImGui::IsKeyPressed(ImGuiKey_Delete)) {
            nlohmann::json op = {{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", dimHoverCid_}};
            auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
            if (!r.ok) st.toast(ui::ToastLevel::Error, "Delete dimension: " + r.error_message);
            else { if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                   autoPlace_.erase(dimHoverCid_); resolve(st); }
            dimHoverCid_.clear();
        }
    }

    // Drag (only when no drawing tool is active): hit-test handles, drag with sketch_drag, release
    // commits. At most one sketch_drag per frame (throttled by dragFrame_).
    if (tool_ == SketchTool::None && !modelSelectionAllowed()) {
        // C37-D: a hovered/dragging dimension text claims the click (dim drag/edit above handles it).
        auto toggleSel = [&](const std::string& id) {
            auto it = std::find(selIds_.begin(), selIds_.end(), id);
            if (io.KeyCtrl) { if (it != selIds_.end()) selIds_.erase(it); else selIds_.push_back(id); }
            else { selIds_.clear(); selRelIds_.clear(); selIds_.push_back(id); }   // C51: drop relations too
        };
        // C51: also toggle a geometric RELATION into the relation selection (separate from the
        // entity selIds_ buffer). A non-Ctrl pick replaces both selections.
        auto toggleRel = [&](const std::string& cid) {
            auto it = std::find(selRelIds_.begin(), selRelIds_.end(), cid);
            if (io.KeyCtrl) { if (it != selRelIds_.end()) selRelIds_.erase(it); else selRelIds_.push_back(cid); }
            else { selIds_.clear(); selRelIds_.clear(); selRelIds_.push_back(cid); }
        };

        // ---- C51: hover target (what a click will grab). Priority handle > relation badge >
        // entity (or its dwelled loop) > region interior > datum axis. Computed once and reused
        // for the pre-highlight AND the press, so the cursor's affordance and its action always agree.
        bool idleHover = imgHovered && !dragging_ && !boxSelecting_ && !groupDragging_ &&
                         !dragArmed_ && !groupDragArmed_ && !dimDragging_ && dimHoverCid_.empty();
        std::string hovHandle; float hovHu = 0, hovHv = 0;
        std::string hovRel, hovEnt, hovAxis;
        ImVec2 hovAxisA, hovAxisB;
        int hovRegion = -1;
        if (idleHover) {
            if (hitTestHandle(io.MousePos, hovHandle, hovHu, hovHv) > 0) {
                // point handle wins
            } else {
                int rb = hitTestRelationBadge(io.MousePos);
                if (rb >= 0) hovRel = relHits_[rb].cid;
                else {
                    int ei = hitTestEntity(io.MousePos);
                    if (ei >= 0) hovEnt = ents_[ei].id;
                    else {
                        int r = hitTestRegion(io.MousePos);
                        if (regionValid(r)) hovRegion = r;
                        else {
                            // No sketch geometry under the cursor — allow selecting a drawn
                            // datum axis (X/Y/Z + injected auto axes) as a sketch reference,
                            // same idea as the virtual "origin" handle. Uses the shared
                            // hitTestDatumAxis (renderer refDatums Kind::Axis).
                            std::string axFid, axName;
                            ImVec2 aa, ab;
                            if (hitTestDatumAxis(st, io.MousePos, imgMin, vw, vh,
                                                 aa, ab, axFid, axName)) {
                                hovAxis = axFid;
                                hovAxisA = aa; hovAxisB = ab;
                            }
                        }
                    }
                }
            }
        }
        hoverRelCid_ = hovRel;   // feeds the badge tint on the next frame's render pass
        // GUI-picking overhaul: "hot" whenever a sketch entity/handle/relation/region/axis claims
        // the cursor, or a drag/box-select is armed/live (idleHover false covers those cases) —
        // either way the viewport's fallback model-pick must not also fire this frame.
        entityHoverHot_ = !idleHover || !hovHandle.empty() || !hovRel.empty() ||
                          !hovEnt.empty() || hovRegion >= 0 || !hovAxis.empty();

        // C51: entity-edge dwell -> loop preview. Resting on the same loop-member edge for kLoopDwell
        // grows the pick from that lone edge to its whole closed loop (SolidWorks-ish loop pick).
        // Clicking the INSIDE of a profile selects the loop with no delay (handled via hovRegion).
        constexpr float kLoopDwell = 0.7f;   // ~"a second" of rest to expand an edge to its loop
        bool slow = std::hypot(io.MouseDelta.x, io.MouseDelta.y) < 2.5f * ui::uiScale;
        int loopOfHov = hovEnt.empty() ? -1 : regionContaining(hovEnt);
        if (!hovEnt.empty() && hovEnt == dwellEnt_ && slow && loopOfHov >= 0) dwellAccum_ += io.DeltaTime;
        else { dwellEnt_ = hovEnt; dwellAccum_ = 0.0f; }
        entityLoopRegion_ = (loopOfHov >= 0 && dwellAccum_ >= kLoopDwell) ? loopOfHov : -1;

        // The loop a click would grab: a hovered interior immediately, else a dwelled edge's loop.
        int effRegion = hovRegion >= 0 ? hovRegion : entityLoopRegion_;

        // ---- C51: pre-highlight — outline exactly what a click will select, drawn on top. ----
        const ImU32 hiCol = canvasAccent();
        const float hiTh = 2.6f * ui::uiScale;
        if (!hovHandle.empty()) {
            dl->AddCircle(uvToScreen(hovHu, hovHv), 6.0f * ui::uiScale, col(pal.hoverHighlight),
                          0, 2.0f * ui::uiScale);
            st.requestCursor(ui::CursorKind::Hand);
        } else if (effRegion >= 0) {
            for (const auto& id : regions_[effRegion].ids)
                if (const SketchEntity* e = entById(id)) strokeEntityOutline(dl, *e, hiCol, hiTh);
            hotRegion_ = effRegion;          // brighten the region fill (next frame)
            st.requestCursor(ui::CursorKind::Hand);
        } else if (!hovEnt.empty()) {
            if (const SketchEntity* e = entById(hovEnt)) strokeEntityOutline(dl, *e, hiCol, hiTh);
            hotRegion_ = -1;
            st.requestCursor(ui::CursorKind::Hand);
        } else if (!hovAxis.empty()) {
            // Highlight the hovered datum axis along its drawn segment (accent, thicker).
            dl->AddLine(hovAxisA, hovAxisB, col(pal.accent, 0.95f), 4.0f * ui::uiScale);
            hotRegion_ = -1;
            st.requestCursor(ui::CursorKind::Hand);
        } else {
            if (hovRel.empty()) hotRegion_ = -1;   // badge hover is tinted in the render pass
            else st.requestCursor(ui::CursorKind::Hand);
        }

        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !dragging_ &&
            !dragArmed_ && !groupDragArmed_ && !dimDragging_ && dimHoverCid_.empty()) {
            if (!hovHandle.empty()) {
                // C50: ARM a potential drag on this point handle — release without moving selects
                // the handle; a move past threshold reshapes (single point).
                dragArmed_ = true; pendingHandle_ = hovHandle; dragDownPos_ = io.MousePos;
            } else if (!hovRel.empty()) {
                toggleRel(hovRel);                    // relations select on click (no drag)
            } else if (effRegion >= 0) {
                // C51: profile interior, or a dwelled edge's loop -> arm a group drag of the loop;
                // release without moving selects the whole loop.
                groupDragArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;
                groupClickKind_ = 2; groupClickRegion_ = effRegion; groupClickEnt_.clear();
            } else if (!hovEnt.empty()) {
                // C51: entity body -> arm a group drag (the whole selection if this entity is in
                // it, else just this entity); release without moving selects it.
                groupDragArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;
                groupClickKind_ = 1; groupClickEnt_ = hovEnt; groupClickRegion_ = -1;
            } else if (!hovAxis.empty()) {
                // Datum axis as sketch reference (virtual id like "axis.x") — select, no drag.
                toggleSel(hovAxis);
                st.selectedFeatureId = hovAxis;   // FM + renderer emphasis stay in step
            } else {
                boxArmed_ = true; boxStart_ = io.MousePos; boxCtrl_ = io.KeyCtrl;   // empty canvas
            }
        }
        // Arm->drag promotion threshold: DPI-scaled (matches the viewport's LMB threshold style)
        // and a touch generous at 5px so a sloppy click still reads as a click, not a micro-drag.
        const float kDragArmPx = 5.0f * ui::uiScale;
        // Armed handle: promote to a real (single-point reshape) drag past the threshold. The
        // origin is fixed at (0,0) — it stays armed (never promotes), so releasing simply selects it.
        if (dragArmed_ && !dragging_ && pendingHandle_ != "origin" &&
            ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - dragDownPos_.x, io.MousePos.y - dragDownPos_.y) > kDragArmPx) {
                dragging_ = true; dragHandle_ = pendingHandle_; dragFrame_ = -1; dragArmed_ = false;
            }
        }
        // C51: armed group -> promote to a live profile/selection translate past the threshold.
        if (groupDragArmed_ && !groupDragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - boxStart_.x, io.MousePos.y - boxStart_.y) > kDragArmPx) {
                std::vector<std::string> set;
                if (groupClickKind_ == 2 && groupClickRegion_ >= 0 &&
                    groupClickRegion_ < (int)regions_.size()) {
                    set = regions_[groupClickRegion_].ids;
                } else if (!groupClickEnt_.empty() && isSelected(groupClickEnt_)) {
                    for (const auto& s : selIds_) set.push_back(s.substr(0, s.find('.')));
                } else if (!groupClickEnt_.empty()) {
                    set.push_back(groupClickEnt_);
                }
                float su, sv; screenToUV(boxStart_, su, sv);
                beginGroupDrag(set, su, sv);
                groupDragArmed_ = false; dragFrame_ = -1;
            }
        }
        // C51: armed box -> promote to a live rubber band past the threshold.
        if (boxArmed_ && !boxSelecting_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (std::hypot(io.MousePos.x - boxStart_.x, io.MousePos.y - boxStart_.y) > kDragArmPx)
                boxSelecting_ = true;
        }
        // C51: feed the live group translate (throttled once/frame, like the point drag). Compute
        // uv straight from the cursor so the drag tracks even when it strays past the image edge.
        if (groupDragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            int frame = ImGui::GetFrameCount();
            if (frame != dragFrame_) {
                dragFrame_ = frame;
                float gu, gv; screenToUV(io.MousePos, gu, gv);
                // C56: snap the translate to origin/keypoints/midpoints, alignment guides, and
                // collinear lines; the previewed relation is committed on drop.
                if (st.settings.sketchDragSnapRelations) groupDragSnap(gu, gv);
                else dragSnapLive_ = false;
                updateGroupDrag(st, gu, gv);
            }
            st.requestCursor(ui::CursorKind::Hand);
        }
        if (dragging_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            int frame = ImGui::GetFrameCount();
            if (frame != dragFrame_ && imgHovered) {
                dragFrame_ = frame;
                // Most handles target the cursor model point (u,v). The new scalar handles
                // (.rim / .start_angle / .end_angle) instead target a single solver variable, so the
                // scalar goes in xy[0]; xy[1] is ignored by the engine's 1-variable soft-target drag.
                nlohmann::json extra;
                auto hSuf = [](const std::string& h, const char* suf) {
                    auto p = h.rfind('.');
                    return p != std::string::npos && h.substr(p + 1) == suf;
                };
                std::string hEid = dragHandle_.substr(0, dragHandle_.find('.'));
                const SketchEntity* hEnt = entById(hEid);
                if (hSuf(dragHandle_, "rim") && hEnt) {
                    double dr = std::hypot((double)mu - hEnt->center[0],
                                           (double)mv - hEnt->center[1]);
                    // C56: snap the radius — rim through a point, tangent to a line/circle,
                    // or equal to another radius. Previewed + committed on drop.
                    if (st.settings.sketchDragSnapRelations) {
                        float rs = (float)dr;
                        rimDragSnap(*hEnt, rs);
                        dr = rs;
                    } else {
                        dragSnapLive_ = false;
                    }
                    extra = {{"handle", dragHandle_},
                             {"xy", nlohmann::json::array({dr, 0.0})}};
                } else if ((hSuf(dragHandle_, "start_angle") ||
                            hSuf(dragHandle_, "end_angle")) && hEnt) {
                    double ang = std::atan2((double)mv - hEnt->center[1],
                                            (double)mu - hEnt->center[0])
                                 * 180.0 / 3.14159265358979;
                    if (ang < 0) ang += 360.0;
                    extra = {{"handle", dragHandle_},
                             {"xy", nlohmann::json::array({ang, 0.0})}};
                } else {
                    // C56: snap the dragged point to origin/endpoints/centers/midpoints/rims and
                    // u/v alignment guides (excludes itself); line endpoints additionally infer
                    // horizontal/vertical/parallel/collinear direction. Committed on drop.
                    float su = mu, sv = mv;
                    dragSnapLive_ = false; dragSnapHandle_.clear();
                    dragRelType_.clear(); dragRelOther_.clear(); dragRelEnt_.clear();
                    dragAlignSrcU_.clear(); dragAlignSrcV_.clear();
                    dragSnapInfer_ = SnapInfer{};
                    if (st.settings.sketchDragSnapRelations) {
                        snapPoint(su, sv, dragSnapHandle_, &dragSnapInfer_,
                                  dragHandle_.c_str(), nullptr);
                        dragSnapLive_ = true;
                        // Line-endpoint direction inference (only when no point snap fired).
                        if (dragSnapHandle_.empty() && hEnt && hEnt->type == "line" &&
                            (hSuf(dragHandle_, "p1") || hSuf(dragHandle_, "p2"))) {
                            const bool isP1 = hSuf(dragHandle_, "p1");
                            const float ax = isP1 ? hEnt->p2[0] : hEnt->p1[0];
                            const float ay = isP1 ? hEnt->p2[1] : hEnt->p1[1];
                            const float du = su - ax, dv = sv - ay;
                            const float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                            if (ang < kInferHVDeg && (du != 0.0f || dv != 0.0f)) {
                                sv = ay; dragRelType_ = "horizontal"; dragRelEnt_ = hEid;
                                if (dragSnapInfer_.alignV && dragSnapInfer_.srcV.y != ay)
                                    dragSnapInfer_.alignV = false;
                            } else if (ang > 90.0f - kInferHVDeg) {
                                su = ax; dragRelType_ = "vertical"; dragRelEnt_ = hEid;
                                if (dragSnapInfer_.alignU && dragSnapInfer_.srcU.x != ax)
                                    dragSnapInfer_.alignU = false;
                            } else {
                                // Parallel / collinear to another line: project the cursor onto
                                // the snapped direction ray from the fixed endpoint.
                                const float segL = std::hypot(du, dv);
                                if (segL > 1e-5f) {
                                    const float pxPerU = std::hypot(uAxisS_.x, uAxisS_.y);
                                    const float snapPx = kSnapPx * ui::uiScale;
                                    float bestPx = snapPx;
                                    float bestU = 0, bestV = 0;
                                    const SketchEntity* bestOe = nullptr;
                                    bool bestCol = false;
                                    for (const auto& oe : ents_) {
                                        if (oe.id == hEid || oe.type != "line") continue;
                                        float ox = oe.p2[0] - oe.p1[0], oy = oe.p2[1] - oe.p1[1];
                                        float oL = std::hypot(ox, oy);
                                        if (oL < 1e-5f) continue;
                                        ox /= oL; oy /= oL;
                                        float dot = du * ox + dv * oy;
                                        float sdirx = dot >= 0 ? ox : -ox, sdiry = dot >= 0 ? oy : -oy;
                                        float cosang = std::fabs(dot) / segL;
                                        if (std::acos(std::fmin(1.0f, cosang)) * 57.2957795f >
                                            kInferHVDeg) continue;
                                        float t = du * sdirx + dv * sdiry;
                                        if (t <= 0.0f) continue;
                                        float cu = ax + sdirx * t, cv = ay + sdiry * t;
                                        float offPx = std::hypot(su - cu, sv - cv) * pxPerU;
                                        if (offPx >= bestPx) continue;
                                        // Collinear when the ray's supporting line passes
                                        // through the other line (perp distance ~ 0).
                                        float cross = (oe.p1[0] - ax) * sdiry -
                                                      (oe.p1[1] - ay) * sdirx;
                                        bestPx = offPx; bestU = cu; bestV = cv; bestOe = &oe;
                                        bestCol = std::fabs(cross) * pxPerU <= snapPx;
                                    }
                                    if (bestOe) {
                                        su = bestU; sv = bestV;
                                        dragRelType_ = bestCol ? "collinear" : "parallel";
                                        dragRelEnt_ = hEid; dragRelOther_ = bestOe->id;
                                        dragSnapInfer_.alignU = dragSnapInfer_.alignV = false;
                                    }
                                }
                            }
                        }
                        dragSnapUV_ = ImVec2(su, sv);
                        dragSnapLive_ = !dragSnapHandle_.empty() || dragSnapInfer_.alignU ||
                                        dragSnapInfer_.alignV || !dragRelType_.empty();
                    }
                    extra = {{"handle", dragHandle_},
                             {"xy", nlohmann::json::array({(double)su, (double)sv})}};
                }
                queueSketchDrag(st, std::move(extra));
            }
        }
        // ---- Drag edge auto-pan (Hunter UX). While an entity/handle/group is being LMB-dragged,
        // the camera holds perfectly still in the central "dead zone"; only when the cursor enters
        // a band along a visible viewport edge does the view pan toward that edge, with the speed
        // ramping quadratically from 0 at the band's inner boundary to a modest max at the edge.
        // Tuning: kDragPanBandPx = band width; kDragPanMaxPx = full-speed px/s (540 px/s crosses a
        // 1080p-tall viewport in ~2 s). Frame-rate independent via io.DeltaTime.
        if ((dragging_ || groupDragging_) && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            const float kDragPanBandPx = 48.0f * ui::uiScale;    // edge band width (screen px)
            const float kDragPanMaxPx  = 540.0f * ui::uiScale;   // max auto-pan speed (px/s)
            // Visible sketch region = image bounds minus the pinned columns (skClip*), so the band
            // hugs the edges the user actually sees.
            auto ramp = [](float t) { t = t < 0.f ? 0.f : (t > 1.f ? 1.f : t); return t * t; };
            // Outward direction (screen space, y down), magnitude 0..1 per axis.
            float ox = ramp((skClipMin.x + kDragPanBandPx - io.MousePos.x) / kDragPanBandPx) * -1.f
                     + ramp((io.MousePos.x - (skClipMax.x - kDragPanBandPx)) / kDragPanBandPx);
            float oy = ramp((skClipMin.y + kDragPanBandPx - io.MousePos.y) / kDragPanBandPx) * -1.f
                     + ramp((io.MousePos.y - (skClipMax.y - kDragPanBandPx)) / kDragPanBandPx);
            if ((ox != 0.0f || oy != 0.0f) && st.renderer) {
                float dt = io.DeltaTime < 0.1f ? io.DeltaTime : 0.1f;   // stall guard: no lurches
                // Grab-pan convention (mirrors the in-sketch Ctrl+MMB pan in viewport_panel.cpp):
                // a grab delta of (+dx) slides content right / camera left. To REVEAL space toward
                // the edge, feed the negated outward vector as the grab delta.
                float gx = -ox * kDragPanMaxPx * dt;
                float gy = -oy * kDragPanMaxPx * dt;
                // Camera seam: worldPerPixel + published basis (works for ortho and perspective).
                float worldPerPx = st.renderer->worldPerPixel(vh);
                const auto& cam = st.renderer->camera();
                Vec3 R = cam.right;
                Vec3 U = cam.up;
                st.pan[0] += (-R.x * gx + U.x * gy) * worldPerPx;
                st.pan[1] += (-R.y * gx + U.y * gy) * worldPerPx;
                st.pan[2] += (-R.z * gx + U.z * gy) * worldPerPx;
            }
        }
        // ---- C56: drag snap preview — marker at the snapped point, dashed alignment guides,
        // and chips showing exactly which relation(s) the drop will add.
        if ((dragging_ || groupDragging_) && dragSnapLive_ &&
            ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            const ImU32 inferCol = col(pal.sketchInfer);
            const ImU32 halo = col(pal.viewportTop, 0.65f);
            ImVec2 sCur = uvToScreen(dragSnapUV_.x, dragSnapUV_.y);
            const float dd = 6.0f * ui::uiScale, dg = 4.0f * ui::uiScale, dw = 1.2f * ui::uiScale;
            // Alignment guides fire alone or riding a rim snap (snapPoint never sets them under
            // any other point snap).
            if (dragSnapInfer_.alignU)
                drawDashedLine(dl, uvToScreen(dragSnapInfer_.srcU.x, dragSnapInfer_.srcU.y),
                               sCur, inferCol, dw, dd, dg);
            if (dragSnapInfer_.alignV)
                drawDashedLine(dl, uvToScreen(dragSnapInfer_.srcV.x, dragSnapInfer_.srcV.y),
                               sCur, inferCol, dw, dd, dg);
            // Snap marker — same vocabulary as the drawing tools (dot / ring / diamond).
            const float ms = 5.0f * ui::uiScale;
            if (!dragSnapHandle_.empty()) {
                dl->AddCircleFilled(sCur, ms * 2.4f, halo, 0);
                if (snapHasPrefix(dragSnapHandle_, "mid:")) {
                    ImVec2 d[4] = {ImVec2(sCur.x, sCur.y - ms * 1.5f), ImVec2(sCur.x + ms * 1.5f, sCur.y),
                                   ImVec2(sCur.x, sCur.y + ms * 1.5f), ImVec2(sCur.x - ms * 1.5f, sCur.y)};
                    dl->AddQuad(d[0], d[1], d[2], d[3], inferCol, 1.6f * ui::uiScale);
                } else if (snapHasPrefix(dragSnapHandle_, "rim:")) {
                    dl->AddCircle(sCur, ms * 1.4f, inferCol, 0, 1.6f * ui::uiScale);
                } else if (dragSnapHandle_.find(".center") != std::string::npos) {
                    dl->AddCircle(sCur, ms, inferCol, 0, 1.6f * ui::uiScale);
                    dl->AddCircle(sCur, ms * 1.9f, inferCol, 0, 1.2f * ui::uiScale);
                } else {   // endpoint / point / origin
                    dl->AddCircleFilled(sCur, ms * 0.7f, inferCol);
                    dl->AddCircle(sCur, ms * 1.4f, inferCol, 0, 1.2f * ui::uiScale);
                }
            } else {
                dl->AddCircle(sCur, 4.0f * ui::uiScale, inferCol, 0, 1.5f * ui::uiScale);
            }
            // Pending-relation chips below-right of the cursor (same chip style as the tools).
            const float bs2 = 16.0f * ui::uiScale;
            float chipX = io.MousePos.x + bs2;
            const float chipY = io.MousePos.y + bs2;
            auto chip = [&](const char* relType) {
                ImVec2 gp(chipX, chipY);
                ImVec2 c0(gp.x - bs2 * 0.5f, gp.y - bs2 * 0.5f), c1(gp.x + bs2 * 0.5f, gp.y + bs2 * 0.5f);
                dl->AddRectFilled(c0, c1, col(pal.sketchRelBg), 3.5f * ui::uiScale);
                dl->AddRect(c0, c1, col(pal.sketchRelBorder), 3.5f * ui::uiScale, 0,
                            1.2f * ui::uiScale);
                drawRelationGlyph(dl, gp, bs2, relType, col(pal.sketchInk));
                chipX += bs2 + 2.0f * ui::uiScale;
            };
            if (!dragSnapHandle_.empty()) {
                if (snapHasPrefix(dragSnapHandle_, "mid:")) chip("midpoint");
                else if (snapHasPrefix(dragSnapHandle_, "rim:")) chip("point_on");
                else chip("coincident");
            }
            if (dragSnapInfer_.alignU) chip("vertical");
            if (dragSnapInfer_.alignV) chip("horizontal");
            if (!dragRelType_.empty()) chip(dragRelType_.c_str());
        }
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            if (dragging_) { dragging_ = false; commitDrag(st); applyDragSnapRelations(st, false); }
            else if (groupDragging_) {
                // C51: profile/selection translate finished -> persist it as one undoable batch.
                groupDragging_ = false; commitDrag(st); applyDragSnapRelations(st, true);
            }
            else if (dragArmed_) {
                // Released without dragging -> select the point handle (vertex/center pick).
                // The drag-only rim/arc-angle handles (.rim/.start_angle/.end_angle) are not real
                // sub-entities: a click without a drag should select the OWNING entity instead, so
                // toggle-construction / relations get a resolvable id (not "c1.rim").
                std::string selH = pendingHandle_;
                auto dot = selH.rfind('.');
                if (dot != std::string::npos) {
                    std::string suf = selH.substr(dot);
                    if (suf == ".rim" || suf == ".start_angle" || suf == ".end_angle")
                        selH = selH.substr(0, dot);
                }
                toggleSel(selH);
            }
            else if (groupDragArmed_) {
                // C51: press without a drag -> plain click. Select the pressed entity, or the
                // whole loop if the press was inside a hot profile.
                if (groupClickKind_ == 2 && groupClickRegion_ >= 0 &&
                    groupClickRegion_ < (int)regions_.size()) {
                    if (!boxCtrl_) { selIds_.clear(); selRelIds_.clear(); }
                    for (const auto& id : regions_[groupClickRegion_].ids)
                        if (!isSelected(id)) selIds_.push_back(id);
                } else if (!groupClickEnt_.empty()) {
                    toggleSel(groupClickEnt_);
                }
            }
            // C51: finish a box drag, or a plain empty-canvas click.
            else if (boxSelecting_) {
                bool crossing = io.MousePos.x < boxStart_.x;   // right->left = crossing
                boxSelect(boxStart_, io.MousePos, crossing, boxCtrl_);
            } else if (boxArmed_ && !boxCtrl_) {
                selIds_.clear(); selRelIds_.clear();   // C41 (T2): empty-canvas click clears selection
            }
            dragArmed_ = false;
            boxArmed_ = boxSelecting_ = false;
            groupDragArmed_ = false; groupClickKind_ = 0; groupClickEnt_.clear(); groupClickRegion_ = -1;
        }
        // C51: live rubber band. Solid accent = window (L->R, fully-enclosed); dashed amber =
        // crossing (R->L, touch), mirroring SolidWorks' two box-select modes.
        if (boxSelecting_) {
            bool crossing = io.MousePos.x < boxStart_.x;
            ImVec2 lo(std::fmin(boxStart_.x, io.MousePos.x), std::fmin(boxStart_.y, io.MousePos.y));
            ImVec2 hi(std::fmax(boxStart_.x, io.MousePos.x), std::fmax(boxStart_.y, io.MousePos.y));
            ImU32 edge = crossing ? col(pal.sketchInfer) : col(pal.accent, 0.9f);
            dl->AddRectFilled(lo, hi, crossing ? col(pal.sketchInfer, 0.10f) : col(pal.accent, 0.10f));
            const float bw = 1.4f * ui::uiScale;
            const float bd = 6.0f * ui::uiScale, bg = 4.0f * ui::uiScale;
            if (crossing) {
                drawDashedLine(dl, lo, ImVec2(hi.x, lo.y), edge, bw, bd, bg);
                drawDashedLine(dl, ImVec2(hi.x, lo.y), hi, edge, bw, bd, bg);
                drawDashedLine(dl, hi, ImVec2(lo.x, hi.y), edge, bw, bd, bg);
                drawDashedLine(dl, ImVec2(lo.x, hi.y), lo, edge, bw, bd, bg);
            } else {
                dl->AddRect(lo, hi, edge, 0.0f, 0, bw);
            }
        }
        // (Handle/entity/loop/relation hover highlight is drawn by the C51 pre-highlight above.)

        // C37-D: right-click with no tool active -> small context menu (gestures are off in-sketch,
        // so the RMB is free; consume it before anything else can).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right) && !dimPopupOpen_)
            ImGui::OpenPopup("##SketchCtx");
    }
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopup("##SketchCtx")) {
        // Count deletable sketch entities in the selection (virtual origin/axes don't count).
        int nDelEnt = 0;
        {
            std::vector<std::string> seen;
            for (const auto& sel : selIds_) {
                std::string base = sel.substr(0, sel.find('.'));
                if (base == "origin" || base.rfind("axis.", 0) == 0) continue;
                if (!entById(base)) continue;
                if (std::find(seen.begin(), seen.end(), base) != seen.end()) continue;
                seen.push_back(base);
            }
            nDelEnt = (int)seen.size();
        }
        const int nDelRel = (int)selRelIds_.size();
        const bool canDelete = nDelEnt > 0 || nDelRel > 0;
        // "Delete all" when every real sketch entity is selected (and there is at least one).
        int nRealEnts = 0;
        for (const auto& e : ents_) ++nRealEnts;
        const bool allSelected = nRealEnts > 0 && nDelEnt >= nRealEnts;

        if (!selIds_.empty()) {
            if (ImGui::MenuItem("Add Relation...")) beginRelations(st);
            ImGui::Separator();
        }
        if (canDelete) {
            const char* delLabel = allSelected ? "Delete all" : "Delete";
            if (ImGui::MenuItem(delLabel)) {
                // Same multi-delete batch as the Delete/Backspace key handler.
                nlohmann::json ops = nlohmann::json::array();
                std::vector<std::string> done;
                for (const auto& sel : selIds_) {
                    std::string base = sel.substr(0, sel.find('.'));
                    if (base == "origin" || base.rfind("axis.", 0) == 0) continue;
                    if (std::find(done.begin(), done.end(), base) != done.end()) continue;
                    if (!entById(base)) continue;
                    done.push_back(base);
                    ops.push_back({{"op", "sketch_delete_entity"}, {"sketch", fid_}, {"id", base}});
                }
                for (const auto& cid : selRelIds_)
                    ops.push_back({{"op", "sketch_delete_constraint"}, {"sketch", fid_}, {"id", cid}});
                if (!ops.empty()) {
                    auto r = st.docMirror.applyOps(st.worker, ops);
                    if (!r.ok) st.toast(ui::ToastLevel::Error, "Delete: " + r.error_message);
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                        if (ents_.empty()) {
                            std::vector<std::string> deps;
                            collectSketchDependents(st, fid_, deps);
                            if (!deps.empty()) {
                                st.toast(ui::ToastLevel::Warn,
                                         emptyProfileToast(deps, featureDisplayName(st, fid_)));
                            }
                        }
                    }
                }
                selIds_.clear(); selRelIds_.clear();
            }
            ImGui::Separator();
        }
        if (ImGui::MenuItem("Select")) resetTool();
        if (ImGui::MenuItem("Smart Dimension")) { resetTool(); tool_ = SketchTool::SmartDim; }
        if (ImGui::MenuItem("Fully Define Sketch")) fullyDefine(st);
        if (ImGui::MenuItem("Display/Delete Relations", nullptr, relationsPanel_.open))
            relationsPanel_.open = !relationsPanel_.open;
        if (ImGui::MenuItem("Grid", nullptr, showGrid_)) showGrid_ = !showGrid_;
        if (ImGui::MenuItem("Automatic Relations", nullptr, autoAlignRelations_)) {
            autoAlignRelations_ = !autoAlignRelations_;
            st.toast(ui::ToastLevel::Info, autoAlignRelations_
                     ? "Automatic relations: On (snapped alignments add vertical/horizontal relations)"
                     : "Automatic relations: Off (alignment guides stay positional)");
        }
        ImGui::Separator();
        if (ImGui::MenuItem("Exit Sketch")) { ImGui::CloseCurrentPopup(); ImGui::EndPopup();
                                              ui::popContextMenuStyle();
                                              exitSketch(st, /*keep=*/true); dl->PopClipRect(); return; }
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();

    // ---- C29-B sketch-editing tools (Trim / Extend / Offset) ----
    // These have bespoke pick semantics (whole-entity hover + ops), so they run BEFORE the generic
    // drawing-tool block and short-circuit it. Each refreshes the entity set via the ops response
    // path (loadDocMesh + resolve), exactly like sketch_add_entity; op errors are logged and the
    // tool stays active. Esc exits.
    bool editTool = (tool_ == SketchTool::Trim || tool_ == SketchTool::Extend ||
                     tool_ == SketchTool::Offset ||
                     tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer ||
                     tool_ == SketchTool::Mirror || tool_ == SketchTool::LinPattern ||
                     tool_ == SketchTool::CircPattern || tool_ == SketchTool::Move ||
                     tool_ == SketchTool::MoveEntities || tool_ == SketchTool::RotateEntities);
    if (editTool) {
        int hovered = imgHovered ? hitTestEntity(io.MousePos) : -1;

        // ----- TRIM (power trim): highlight the exact doomed SPAN in red; click trims it; -----
        // ----- holding LMB and sweeping across entities trims each one crossed (SW style). -----
        if (tool_ == SketchTool::Trim) {
            const ImU32 rc = col(pal.statusErr, 0.95f);
            // Preview: the span a click would remove (client-side mirror of the engine split).
            // Spans of unsupported types (slot etc.) draw nothing — the click logs the error.
            if (hovered >= 0) {
                std::vector<ImVec2> span; bool whole = false;
                if (trimDoomedSpan(hovered, mu, mv, span, whole)) {
                    if (whole) {
                        // No interior crossing: the whole entity goes (engine deletes it).
                        const SketchEntity& e = ents_[hovered];
                        if (e.type == "line") {
                            dl->AddLine(uvToScreen(e.p1[0], e.p1[1]),
                                        uvToScreen(e.p2[0], e.p2[1]), rc, 3.5f * ui::uiScale);
                        } else {
                            float a0 = 0, a1 = 6.2831853f;
                            if (e.type == "arc") {
                                a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                                if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                                if (a1 < a0) a1 += 6.2831853f;
                            }
                            ImVec2 prev; int seg = 64;
                            for (int i = 0; i <= seg; ++i) {
                                float a = a0 + (a1 - a0) * (i / (float)seg);
                                ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                                      e.center[1] + e.radius * std::sin(a));
                                if (i) dl->AddLine(prev, p, rc, 3.5f * ui::uiScale); prev = p;
                            }
                        }
                    } else {
                        for (size_t i = 1; i < span.size(); ++i)
                            dl->AddLine(uvToScreen(span[i - 1].x, span[i - 1].y),
                                        uvToScreen(span[i].x, span[i].y), rc, 3.5f * ui::uiScale);
                    }
                }
            }
            // One trim op at the current cursor (shared by click + sweep).
            auto fireTrim = [&](int idx) -> bool {
                const SketchEntity& e = ents_[idx];
                if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                    st.addLog("[sketch] Trim: unsupported entity type '" + e.type + "'");
                    st.toast(ui::ToastLevel::Info,
                             "Trim works on lines, circles, and arcs — not " + e.type);
                    return false;
                }
                nlohmann::json op = {{"op", "sketch_trim"}, {"sketch", fid_},
                    {"entity", e.id}, {"point", nlohmann::json::array({mu, mv})},
                    {"keep_construction", trimKeepConstruction_}};
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                if (!r.ok) {
                    st.addLog("[sketch] trim failed: " + r.error_message);
                    st.toast(ui::ToastLevel::Warn, r.error_message);
                    return false;
                }
                if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                resolve(st);   // refresh entities/statuses (same path as add_entity)
                return true;
            };
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                if (fireTrim(hovered)) { trimSweepLast_ = io.MousePos; trimSweeping_ = true; }
            } else if (trimSweeping_ && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                // Power-trim sweep: each entity the cursor crosses while LMB is held gets trimmed.
                // A small pixel gate between ops avoids re-firing on the same spot while the
                // entity list refills (ids change across every trim).
                float moved = std::hypot(io.MousePos.x - trimSweepLast_.x,
                                         io.MousePos.y - trimSweepLast_.y);
                if (imgHovered && moved > 10.0f && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type == "line" || e.type == "circle" || e.type == "arc")
                        if (fireTrim(hovered)) trimSweepLast_ = io.MousePos;
                }
            }
            if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) trimSweeping_ = false;
            // 'C' toggles keep-as-construction: the trimmed-away span survives as construction
            // geometry (still constrained to its neighbours) rather than being deleted.
            if (ImGui::IsKeyPressed(ImGuiKey_C)) trimKeepConstruction_ = !trimKeepConstruction_;
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
        }

        // ----- EXTEND: highlight the nearer end; click sends sketch_extend with start|end. -----
        else if (tool_ == SketchTool::Extend) {
            const char* whichEnd = nullptr;     // "start"|"end" for line/arc, by nearer endpoint
            ImVec2 endPx;
            if (hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                const ImU32 ac = col(pal.accent, 0.95f);
                // Faint accent over the whole entity, bright dot on the nearer pickable end.
                if (e.type == "line") {
                    ImVec2 a = uvToScreen(e.p1[0], e.p1[1]), b = uvToScreen(e.p2[0], e.p2[1]);
                    dl->AddLine(a, b, ac, 2.5f * ui::uiScale);
                    float da = std::hypot(a.x - io.MousePos.x, a.y - io.MousePos.y);
                    float db = std::hypot(b.x - io.MousePos.x, b.y - io.MousePos.y);
                    whichEnd = da < db ? "start" : "end"; endPx = da < db ? a : b;
                } else if (e.type == "arc") {
                    ImVec2 a = uvToScreen(e.center[0] + e.radius * std::cos(e.start * 3.14159265f / 180.0f),
                                          e.center[1] + e.radius * std::sin(e.start * 3.14159265f / 180.0f));
                    ImVec2 b = uvToScreen(e.center[0] + e.radius * std::cos(e.end * 3.14159265f / 180.0f),
                                          e.center[1] + e.radius * std::sin(e.end * 3.14159265f / 180.0f));
                    float da = std::hypot(a.x - io.MousePos.x, a.y - io.MousePos.y);
                    float db = std::hypot(b.x - io.MousePos.x, b.y - io.MousePos.y);
                    whichEnd = da < db ? "start" : "end"; endPx = da < db ? a : b;
                }
                if (whichEnd) dl->AddCircleFilled(endPx, 6.0f, ac);
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (!whichEnd) {
                    st.addLog("[sketch] Extend: unsupported entity type '" + e.type + "'");
                    st.toast(ui::ToastLevel::Info,
                             "Extend works on lines and arcs — not " + e.type);
                } else {
                    nlohmann::json op = {{"op", "sketch_extend"}, {"sketch", fid_},
                        {"entity", e.id}, {"end", whichEnd}};
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                    if (!r.ok) {
                        st.addLog("[sketch] extend failed: " + r.error_message);
                        st.toast(ui::ToastLevel::Warn, r.error_message);
                    }
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                    }
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
        }

        // ----- OFFSET: pick chain -> Enter/[Offset] chip -> type distance -> click side. -----
        else if (tool_ == SketchTool::Offset) {
            auto inChain = [&](const std::string& id) {
                return std::find(offsetChain_.begin(), offsetChain_.end(), id) != offsetChain_.end();
            };
            // Tint already-selected chain entities in accent.
            for (const auto& e : ents_) {
                if (!inChain(e.id)) continue;
                const ImU32 ac = col(pal.accent, 0.95f);
                if (e.type == "line" || e.type == "slot") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), ac, 3.5f * ui::uiScale);
                } else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, ac, 3.5f * ui::uiScale); prev = p;
                    }
                }
            }
            if (offsetStage_ == 0) {
                // Hover preview (faint) over the would-be-added entity.
                if (hovered >= 0 && !inChain(ents_[hovered].id)) {
                    const SketchEntity& e = ents_[hovered];
                    const ImU32 hc = col(pal.hoverHighlight, 0.9f);
                    if (e.type == "line" || e.type == "slot")
                        dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hc, 3.0f);
                    else if (e.type == "circle" || e.type == "arc")
                        dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                      std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                                 uvToScreen(e.center[0], e.center[1]).x, 0.0f),
                                      hc, 48, 3.0f);
                }
                // Click toggles an entity in/out of the chain.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.addLog("[sketch] Offset: unsupported entity type '" + e.type + "'");
                    } else {
                        auto it = std::find(offsetChain_.begin(), offsetChain_.end(), e.id);
                        if (it != offsetChain_.end()) offsetChain_.erase(it);
                        else offsetChain_.push_back(e.id);
                    }
                }
                // Enter advances to the distance stage when the chain is non-empty.
                if (!offsetChain_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) {
                    offsetStage_ = 1; offsetFocusInput_ = true; offsetOverlayPos_ = io.MousePos;
                }
            } else if (offsetStage_ == 2) {
                // Side pick: the next click's uv is the side hint.
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    nlohmann::json eids = nlohmann::json::array();
                    for (const auto& id : offsetChain_) eids.push_back(id);
                    nlohmann::json op = {{"op", "sketch_offset"}, {"sketch", fid_},
                        {"entities", eids}, {"distance", offsetDist_},
                        {"side", nlohmann::json::array({mu, mv})}};
                    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                    if (!r.ok) {
                        st.addLog("[sketch] offset failed: " + r.error_message);
                        st.toast(ui::ToastLevel::Warn, r.error_message);
                    }
                    else {
                        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                        resolve(st);
                    }
                    // Reset for another offset (stay in the tool), keep the distance default.
                    offsetStage_ = 0; offsetChain_.clear();
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;   // C41 (T1): stage back-out keeps the tool armed
                // Esc backs out one stage; from the chain stage it exits the tool.
                if (offsetStage_ == 2) { offsetStage_ = 1; offsetFocusInput_ = true; }
                else if (offsetStage_ == 1) offsetStage_ = 0;
                else cancelTool();
            }
        }

        // ----- SKETCH FILLET / CHAMFER (C52): pick two lines; second pick builds the op. -----
        // Engine handles LINE-LINE corners only (sketch_fillet radius / sketch_chamfer distance).
        // The radius/distance numeric field lives in a small always-on overlay pinned top-left
        // (drawn after the interaction pass) and persists across corners. On the second pick the op
        // is applied; success appends the new arc/line + re-solves and we stay armed for another
        // corner. A non-line pick toasts and is ignored. Esc clears a held first pick (or exits the
        // tool if none is held).
        else if (tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            // Tint the already-picked first line in accent.
            const SketchEntity* first = corner1Id_.empty() ? nullptr : entById(corner1Id_);
            if (first && first->type == "line")
                dl->AddLine(uvToScreen(first->p1[0], first->p1[1]),
                            uvToScreen(first->p2[0], first->p2[1]), ac, 3.5f * ui::uiScale);
            // Hover preview (faint) over the would-be-picked line (skip the already-picked one).
            if (hovered >= 0 && ents_[hovered].type == "line" && ents_[hovered].id != corner1Id_) {
                const SketchEntity& e = ents_[hovered];
                dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hc, 3.0f);
            }
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                const SketchEntity& e = ents_[hovered];
                if (e.type != "line") {
                    const char* what = tool_ == SketchTool::SkFillet ? "Sketch Fillet" : "Sketch Chamfer";
                    st.addLog(std::string("[sketch] ") + what + ": pick a line, not " + e.type);
                    st.toast(ui::ToastLevel::Info,
                             std::string(what) + " works on two lines — not " + e.type);
                } else if (corner1Id_.empty()) {
                    corner1Id_ = e.id;                 // first line picked; wait for the second
                } else if (e.id == corner1Id_) {
                    // Clicking the same line again deselects it (pick a different first line).
                    corner1Id_.clear();
                } else {
                    applyCornerOp(st, e.id);           // second line: build + apply, then stay armed
                }
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;   // C41 (T1): clear a held first pick, else exit the tool
                if (!corner1Id_.empty()) corner1Id_.clear();
                else cancelTool();
            }
        }

        // ----- C53 MIRROR / LINEAR PATTERN / CIRCULAR PATTERN: collect an entity selection -----
        // Stage 0 collects line/circle/arc entities (click toggles, like the Offset chain). Enter
        // advances: Mirror -> stage 1 pick a construction line (the mirror axis); Circular -> stage 1
        // pick the center point; Linear -> stage 1 the numeric panel. The op is applied from the
        // numeric panel / line pick (drawn after the interaction pass). Esc backs out a stage.
        else if (tool_ == SketchTool::Mirror || tool_ == SketchTool::LinPattern ||
                 tool_ == SketchTool::CircPattern) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            auto strokeEnt = [&](const SketchEntity& e, ImU32 c, float th) {
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), c, th);
                else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            // Tint the current selection.
            for (const auto& e : ents_) if (inSel(e.id)) strokeEnt(e, ac, 3.5f * ui::uiScale);

            if (mpStage_ == 0) {
                if (hovered >= 0 && !inSel(ents_[hovered].id)) strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::ToastLevel::Info,
                                 "Mirror/Pattern works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                if (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) mpStage_ = 1;
            } else if (tool_ == SketchTool::Mirror && mpStage_ == 1) {
                // Pick the mirror line (must be a line entity; a construction line is the norm).
                if (hovered >= 0 && ents_[hovered].type == "line" && !inSel(ents_[hovered].id))
                    strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line")
                        st.toast(ui::ToastLevel::Info, "Pick a line as the mirror axis, not " + e.type);
                    else if (inSel(e.id))
                        st.toast(ui::ToastLevel::Info, "The mirror line can't be one of the mirrored entities");
                    else
                        applyMirrorOp(st, e.id);   // build + apply, reset selection, stay armed
                }
            } else if (tool_ == SketchTool::CircPattern && mpStage_ == 1) {
                // Pick the center point: snap to a vertex/center if near one, else the raw cursor.
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                dl->AddCircle(uvToScreen(su, sv), 5.0f, col(pal.accent, 0.95f), 16, 2.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    patternCenterU_ = su; patternCenterV_ = sv;
                    mpStage_ = 2;   // advance to the numeric panel
                }
            }
            // Visualize the chosen circular center while in the panel stage.
            if (tool_ == SketchTool::CircPattern && mpStage_ == 2)
                dl->AddCircle(uvToScreen(patternCenterU_, patternCenterV_), 5.0f,
                              col(pal.accent, 0.95f), 16, 2.0f);

            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;
                if (mpStage_ > 0) { mpStage_ = (tool_ == SketchTool::CircPattern) ? mpStage_ - 1 : 0; }
                else cancelTool();
            }
        }

        // ----- C54 MOVE/COPY: stage 0 collect entities (Enter advances) -> stage 1 pick a -----
        // ----- FROM point -> stage 2 live drag to a TO point; the second click applies         -----
        // ----- sketch_move {entities, dx, dy, copy} with delta = to - from. Ctrl => copy.       -----
        else if (tool_ == SketchTool::Move) {
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            auto strokeEnt = [&](const SketchEntity& e, ImU32 c, float th) {
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), c, th);
                else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = uvToScreen(e.center[0] + e.radius * std::cos(a),
                                              e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            for (const auto& e : ents_) if (inSel(e.id)) strokeEnt(e, ac, 3.5f * ui::uiScale);

            if (mpStage_ == 0) {
                if (hovered >= 0 && !inSel(ents_[hovered].id)) strokeEnt(ents_[hovered], hc, 3.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hovered >= 0) {
                    const SketchEntity& e = ents_[hovered];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::ToastLevel::Info,
                                 "Move works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                if (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter)) mpStage_ = 1;
            } else if (mpStage_ == 1) {
                // Pick the FROM anchor (snap to a vertex/center if near one).
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                dl->AddCircle(uvToScreen(su, sv), 5.0f, ac, 16, 2.0f);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    moveFromUV_ = ImVec2(su, sv);
                    mpStage_ = 2;
                }
            } else if (mpStage_ == 2) {
                // Live drag to the TO point; second click applies the move (Ctrl => copy).
                std::string snapH; float su = mu, sv = mv;
                snapPoint(su, sv, snapH);
                bool copy = io.KeyCtrl;
                dl->AddLine(uvToScreen(moveFromUV_.x, moveFromUV_.y), uvToScreen(su, sv),
                            ac, 1.6f);
                dl->AddCircle(uvToScreen(moveFromUV_.x, moveFromUV_.y), 4.0f, ac, 16, 1.6f);
                // Ghost-preview the selection translated by the live delta.
                float pdx = su - moveFromUV_.x, pdy = sv - moveFromUV_.y;
                const ImU32 gc = col(pal.sketchUnder, 0.8f);
                for (const auto& e : ents_) {
                    if (!inSel(e.id)) continue;
                    if (e.type == "line") {
                        dl->AddLine(uvToScreen(e.p1[0] + pdx, e.p1[1] + pdy),
                                    uvToScreen(e.p2[0] + pdx, e.p2[1] + pdy), gc, 2.0f);
                    } else if (e.type == "circle" || e.type == "arc") {
                        float a0 = 0, a1 = 6.2831853f;
                        if (e.type == "arc") {
                            a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                            if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                            if (a1 < a0) a1 += 6.2831853f;
                        }
                        ImVec2 prev; int seg = 64;
                        for (int i = 0; i <= seg; ++i) {
                            float a = a0 + (a1 - a0) * (i / (float)seg);
                            ImVec2 p = uvToScreen(e.center[0] + pdx + e.radius * std::cos(a),
                                                  e.center[1] + pdy + e.radius * std::sin(a));
                            if (i) dl->AddLine(prev, p, gc, 2.0f); prev = p;
                        }
                    }
                }
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    applyMoveOp(st, su - moveFromUV_.x, sv - moveFromUV_.y, copy);
                    // applyMoveOp resets the selection + stage 0 for another move.
                }
            }

            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                escHandled = true;
                if (mpStage_ > 0) mpStage_ = mpStage_ - 1;   // back out from to-pick -> from-pick -> select
                else cancelTool();
            }
        }
        // ----- Gizmo MOVE / ROTATE ENTITIES: stage 0 selects (like Move); stage 1 shows an -----
        // ----- in-plane manipulator at the (draggable) selection centroid. Move = two axis -----
        // ----- arrows + a free center square; Rotate = a bent-arrow ring. Relation snapping -----
        // ----- on the moved anchors; a viewport HUD (check/X + live offset/angle) finishes.  -----
        else if (tool_ == SketchTool::MoveEntities || tool_ == SketchTool::RotateEntities) {
            const bool isRot = (tool_ == SketchTool::RotateEntities);
            const ImU32 ac = col(pal.accent, 0.95f);
            const ImU32 hc = col(pal.hoverHighlight, 0.9f);
            const ImU32 gc = col(pal.sketchUnder, 0.85f);
            const ImU32 uCol = col(pal.axisX, 1.0f);   // u / first in-plane axis (red)
            const ImU32 vCol = col(pal.axisY, 1.0f);   // v / second in-plane axis (green)
            const float uis = ui::uiScale;
            auto inSel = [&](const std::string& id) {
                return std::find(mpSel_.begin(), mpSel_.end(), id) != mpSel_.end();
            };
            // Draw an entity through a (u,v)->(u,v) transform, in colour c, thickness th.
            auto strokeXf = [&](const SketchEntity& e, ImU32 c, float th,
                                const std::function<ImVec2(float, float)>& tf) {
                if (e.type == "line") {
                    dl->AddLine(tf(e.p1[0], e.p1[1]), tf(e.p2[0], e.p2[1]), c, th);
                } else if (e.type == "circle" || e.type == "arc") {
                    float a0 = 0, a1 = 6.2831853f;
                    if (e.type == "arc") {
                        a0 = e.start * 3.14159265f / 180.0f; a1 = e.end * 3.14159265f / 180.0f;
                        if (!e.ccw) { float t = a0; a0 = a1; a1 = t; }
                        if (a1 < a0) a1 += 6.2831853f;
                    }
                    ImVec2 prev; int seg = 64;
                    for (int i = 0; i <= seg; ++i) {
                        float a = a0 + (a1 - a0) * (i / (float)seg);
                        ImVec2 p = tf(e.center[0] + e.radius * std::cos(a),
                                      e.center[1] + e.radius * std::sin(a));
                        if (i) dl->AddLine(prev, p, c, th); prev = p;
                    }
                }
            };
            auto identityTf = [&](float u, float v) { return uvToScreen(u, v); };
            for (const auto& e : ents_) if (inSel(e.id)) strokeXf(e, ac, 3.5f * uis, identityTf);

            if (mpStage_ == 0) {
                int hov2 = hovered;
                if (hov2 >= 0 && !inSel(ents_[hov2].id)) strokeXf(ents_[hov2], hc, 3.0f, identityTf);
                if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && hov2 >= 0) {
                    const SketchEntity& e = ents_[hov2];
                    if (e.type != "line" && e.type != "circle" && e.type != "arc") {
                        st.toast(ui::ToastLevel::Info,
                                 std::string(isRot ? "Rotate" : "Move") +
                                 " works on lines, circles, and arcs — not " + e.type);
                    } else {
                        auto it = std::find(mpSel_.begin(), mpSel_.end(), e.id);
                        if (it != mpSel_.end()) mpSel_.erase(it); else mpSel_.push_back(e.id);
                    }
                }
                bool advance = (!mpSel_.empty() && ImGui::IsKeyPressed(ImGuiKey_Enter));
                if (advance) {
                    computeSelCentroid(gizmoOrigin_); gizmoOriginInit_ = true;
                    moveDx_ = moveDy_ = 0.0f; rotCurAng_ = 0.0f;
                    gzDragging_ = false; gzDragAxis_ = -1;
                    mpStage_ = 1;
                }
                if (ImGui::IsKeyPressed(ImGuiKey_Escape)) { escHandled = true; cancelTool(); }
            } else {   // mpStage_ == 1 : manipulate
                if (!gizmoOriginInit_) { computeSelCentroid(gizmoOrigin_); gizmoOriginInit_ = true; }
                const ImVec2 oScr = uvToScreen(gizmoOrigin_.x, gizmoOrigin_.y);
                // Screen-space unit directions of the two in-plane sketch axes.
                float uLen = std::hypot(uAxisS_.x, uAxisS_.y), vLen = std::hypot(vAxisS_.x, vAxisS_.y);
                ImVec2 uDir = uLen > 1e-6f ? ImVec2(uAxisS_.x / uLen, uAxisS_.y / uLen) : ImVec2(1, 0);
                ImVec2 vDir = vLen > 1e-6f ? ImVec2(vAxisS_.x / vLen, vAxisS_.y / vLen) : ImVec2(0, 1);
                const float armLen = 72.0f * uis, headLen = 13.0f * uis, headW = 6.0f * uis;
                const float grabPx = 8.0f * uis, ringR = 58.0f * uis;
                // distance from point p to segment [a,b]
                auto distSeg = [](ImVec2 p, ImVec2 a, ImVec2 b) {
                    float vx = b.x - a.x, vy = b.y - a.y, wx = p.x - a.x, wy = p.y - a.y;
                    float L2 = vx * vx + vy * vy; float t = L2 > 1e-6f ? (wx * vx + wy * vy) / L2 : 0.0f;
                    t = t < 0 ? 0 : (t > 1 ? 1 : t);
                    float cx = a.x + t * vx - p.x, cy = a.y + t * vy - p.y;
                    return std::hypot(cx, cy);
                };
                // Anchor-based relation snapping: nudge a (du,dv) so a moved selection point lands
                // exactly on a nearby snap target (endpoint/center/midpoint/rim/origin).
                auto snapDelta = [&](float& du, float& dv) {
                    float best = 1e30f, cu = 0, cv = 0; bool found = false;
                    auto tryPt = [&](float au, float av) {
                        float su = au + du, sv = av + dv; std::string h; snapPoint(su, sv, h);
                        if (!h.empty()) {
                            float corr = std::hypot(su - (au + du), sv - (av + dv));
                            if (corr < best) { best = corr; cu = su - (au + du); cv = sv - (av + dv); found = true; }
                        }
                    };
                    for (const auto& e : ents_) {
                        if (!inSel(e.id)) continue;
                        if (e.type == "line") {
                            tryPt(e.p1[0], e.p1[1]); tryPt(e.p2[0], e.p2[1]);
                            tryPt((e.p1[0] + e.p2[0]) * 0.5f, (e.p1[1] + e.p2[1]) * 0.5f);
                        } else { tryPt(e.center[0], e.center[1]); }
                    }
                    if (found) { du += cu; dv += cv; }
                };

                // ---- pick up / release a handle ----
                bool lmbDown = imgHovered && ImGui::IsMouseDown(ImGuiMouseButton_Left);
                if (!gzDragging_ && imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    int axis = -1;
                    float dCenter = std::hypot(io.MousePos.x - oScr.x, io.MousePos.y - oScr.y);
                    if (!isRot) {
                        ImVec2 uEnd(oScr.x + uDir.x * armLen, oScr.y + uDir.y * armLen);
                        ImVec2 vEnd(oScr.x + vDir.x * armLen, oScr.y + vDir.y * armLen);
                        if (dCenter < 11.0f * uis) axis = 2;                 // center square: free move
                        else if (distSeg(io.MousePos, oScr, uEnd) < grabPx) axis = 0;
                        else if (distSeg(io.MousePos, oScr, vEnd) < grabPx) axis = 1;
                    } else {
                        if (dCenter < 12.0f * uis) axis = 2;                 // center dot: move rot axis
                        else if (std::fabs(dCenter - ringR) < grabPx + 2.0f * uis) axis = 3;  // ring: rotate
                    }
                    if (axis >= 0) {
                        gzDragging_ = true; gzDragAxis_ = axis;
                        gzDragStartUV_ = ImVec2(mu, mv);
                        gzOriginAtDragStart_ = ImVec2(moveDx_, moveDy_);   // base offset snapshot
                        if (isRot && axis == 3)
                            rotStartAng_ = std::atan2(io.MousePos.y - oScr.y, io.MousePos.x - oScr.x);
                        if (isRot && axis == 3) gzOriginAtDragStart_ = ImVec2(rotCurAng_, 0);
                    }
                }
                if (gzDragging_ && !lmbDown) { gzDragging_ = false; gzDragAxis_ = -1; }

                // ---- live drag update ----
                if (gzDragging_) {
                    if (!isRot) {
                        float du = mu - gzDragStartUV_.x, dv = mv - gzDragStartUV_.y;
                        if (gzDragAxis_ == 0) dv = 0.0f;              // constrain to u
                        else if (gzDragAxis_ == 1) du = 0.0f;        // constrain to v
                        du += gzOriginAtDragStart_.x; dv += gzOriginAtDragStart_.y;
                        snapDelta(du, dv);
                        if (gzDragAxis_ == 0) dv = 0.0f;             // re-lock after snap
                        else if (gzDragAxis_ == 1) du = 0.0f;
                        moveDx_ = du; moveDy_ = dv;
                    } else if (gzDragAxis_ == 3) {
                        float ang = std::atan2(io.MousePos.y - oScr.y, io.MousePos.x - oScr.x);
                        float d = (ang - rotStartAng_) * 180.0f / 3.14159265f;
                        float a = gzOriginAtDragStart_.x + d;
                        // angle snapping: strong pull to 90s, softer to 15s (unless typing overrides)
                        auto nearMul = [&](float step, float tol) {
                            float m = std::round(a / step) * step;
                            return std::fabs(a - m) < tol ? m : a;
                        };
                        if (!io.KeyShift) { float s90 = nearMul(90.0f, 6.0f);
                            a = (s90 != a) ? s90 : nearMul(15.0f, 2.5f); }
                        rotCurAng_ = a; rotDidType_ = false;
                    } else if (gzDragAxis_ == 2) {
                        // reposition the rotation axis / move origin (snaps to vertices/centers)
                        float su = mu, sv = mv; std::string h; snapPoint(su, sv, h);
                        gizmoOrigin_ = ImVec2(su, sv);
                    }
                }

                // ---- ghost preview of the pending transform ----
                std::function<ImVec2(float, float)> tf;
                if (!isRot) {
                    float dxu = moveDx_, dyu = moveDy_;
                    tf = [&, dxu, dyu](float u, float v) { return uvToScreen(u + dxu, v + dyu); };
                } else {
                    float a = rotCurAng_ * 3.14159265f / 180.0f, ca = std::cos(a), sa = std::sin(a);
                    ImVec2 o = gizmoOrigin_;
                    tf = [&, ca, sa, o](float u, float v) {
                        float x = u - o.x, y = v - o.y;
                        return uvToScreen(o.x + x * ca - y * sa, o.y + x * sa + y * ca);
                    };
                }
                for (const auto& e : ents_) if (inSel(e.id)) strokeXf(e, gc, 2.0f, tf);

                // ---- draw the manipulator ----
                if (!isRot) {
                    auto arrow = [&](ImVec2 dir, ImU32 c, int hot) {
                        ImVec2 tip(oScr.x + dir.x * armLen, oScr.y + dir.y * armLen);
                        float th = (hot ? 3.2f : 2.2f) * uis;
                        dl->AddLine(oScr, tip, c, th);
                        ImVec2 n(-dir.y, dir.x);
                        ImVec2 base(tip.x - dir.x * headLen, tip.y - dir.y * headLen);
                        dl->AddTriangleFilled(tip, ImVec2(base.x + n.x * headW, base.y + n.y * headW),
                                              ImVec2(base.x - n.x * headW, base.y - n.y * headW), c);
                    };
                    int hotAxis = gzDragging_ ? gzDragAxis_ : -1;
                    arrow(uDir, uCol, hotAxis == 0); arrow(vDir, vCol, hotAxis == 1);
                    float hs = 5.0f * uis;
                    dl->AddRectFilled(ImVec2(oScr.x - hs, oScr.y - hs), ImVec2(oScr.x + hs, oScr.y + hs),
                                      hotAxis == 2 ? ac : col(pal.accent, 0.6f));
                } else {
                    // rotation ring with two arrowheads (bent arrows) + a live radial angle line
                    int seg = 72; ImVec2 prev; ImU32 rc = (gzDragging_ && gzDragAxis_ == 3) ? ac : uCol;
                    for (int i = 0; i <= seg; ++i) {
                        float a = (i / (float)seg) * 6.2831853f;
                        ImVec2 p(oScr.x + ringR * std::cos(a), oScr.y + ringR * std::sin(a));
                        if (i) dl->AddLine(prev, p, rc, 2.2f * uis); prev = p;
                    }
                    // two little tangential arrowheads at 45 and 225 deg to read as "rotate"
                    for (float base : {0.7853982f, 3.9269908f}) {
                        ImVec2 p(oScr.x + ringR * std::cos(base), oScr.y + ringR * std::sin(base));
                        ImVec2 tan(-std::sin(base), std::cos(base));   // ccw tangent
                        ImVec2 nrm(std::cos(base), std::sin(base));
                        ImVec2 tip(p.x + tan.x * 10.0f * uis, p.y + tan.y * 10.0f * uis);
                        dl->AddTriangleFilled(
                            tip, ImVec2(p.x + nrm.x * 5.0f * uis, p.y + nrm.y * 5.0f * uis),
                            ImVec2(p.x - nrm.x * 5.0f * uis, p.y - nrm.y * 5.0f * uis), rc);
                    }
                    float la = rotCurAng_ * 3.14159265f / 180.0f;
                    dl->AddLine(oScr, ImVec2(oScr.x + ringR * std::cos(la), oScr.y + ringR * std::sin(la)),
                                col(pal.accent, 0.9f), 1.6f * uis);
                    float ds = 6.0f * uis;
                    dl->AddCircleFilled(oScr, ds, (gzDragging_ && gzDragAxis_ == 2) ? ac
                                                 : col(pal.accent, 0.6f));
                }

                // ---- finish / cancel (HUD buttons, Enter, Esc) ----
                bool finish = hudFinish_ || ImGui::IsKeyPressed(ImGuiKey_Enter);
                bool cancel = hudCancel_ || ImGui::IsKeyPressed(ImGuiKey_Escape);
                hudFinish_ = false; hudCancel_ = false;
                if (cancel) {
                    escHandled = true;
                    mpStage_ = 0; gzDragging_ = false; gzDragAxis_ = -1;
                    moveDx_ = moveDy_ = 0.0f; rotCurAng_ = 0.0f;   // stay in tool, back to select
                } else if (finish) {
                    if (!isRot) {
                        if (std::fabs(moveDx_) > 1e-6f || std::fabs(moveDy_) > 1e-6f)
                            applyMoveOp(st, moveDx_, moveDy_, io.KeyCtrl);
                        else { mpStage_ = 0; mpSel_.clear(); }
                    } else {
                        if (std::fabs(rotCurAng_) > 1e-4f)
                            applyRotateOp(st, gizmoOrigin_.x, gizmoOrigin_.y, rotCurAng_, io.KeyCtrl);
                        else { mpStage_ = 0; mpSel_.clear(); }
                    }
                    gizmoOriginInit_ = false; gzDragging_ = false; gzDragAxis_ = -1;
                    moveDx_ = moveDy_ = 0.0f; rotCurAng_ = 0.0f;
                }
            }
        }
    }

    // ---- C53 CONVERT ENTITIES: pick MODEL edges/faces in the viewport, project each into ----
    // ---- the sketch (sketch_convert). Like Smart Dim's external-ref pick, but it applies the ----
    // ---- op immediately and stays armed for more converts. Esc exits the tool. ----
    if (tool_ == SketchTool::Convert) {
        render::PickHit extHit{};
        if (imgHovered && st.hasDocMesh && st.idPass) {
            int px = (int)(io.MousePos.x - imgMin.x), py = (int)(io.MousePos.y - imgMin.y);
            // Edge-priority pick: a model edge on a coplanar face (normal-to a sketched face) is
            // 1 px wide and otherwise loses the center pixel to the face. ~7 px grab radius (scaled).
            int grab = (int)std::lround(7.0f * ui::uiScale);
            render::PickHit h = st.idPass->pickFinalPreferEdge(px, py, vw, vh, grab);
            if (h.valid() && modelPickKind(h.kind))
                extHit = h;
        }
        st.hover = extHit;   // standard 3D hover highlight on the pickable edge/face
        if (extHit.valid() && imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            std::string perr;
            const auto ref = modelPickReference(st, extHit, perr);
            if (ref.is_object()) {
                nlohmann::json refs = nlohmann::json::array();
                for (const auto& item : st.selection.items()) {
                    if (!modelPickKind(item.hit.kind)) continue;
                    const auto selected = modelPickReference(st, item.hit, perr);
                    if (selected.is_object() && std::find(refs.begin(), refs.end(), selected) == refs.end())
                        refs.push_back(selected);
                }
                if (std::find(refs.begin(), refs.end(), ref) == refs.end()) refs.push_back(ref);
                applyConvertRefs(st, refs);
                st.selection.clear();
            } else {
                st.toast(ui::ToastLevel::Warn, "Can't convert that geometry" +
                         (perr.empty() ? std::string() : ": " + perr));
            }
        }
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) { escHandled = true; cancelTool(); }
    }

    // ---- drawing tools: rubber-band preview + click handling ----
    if (!editTool && tool_ != SketchTool::None && tool_ != SketchTool::SmartDim &&
        tool_ != SketchTool::Convert) {
        // Snap the live cursor for preview + inference glyphs (C49: point snap + midpoint/rim
        // snap + alignment inference, see snapPoint).
        float su = mu, sv = mv; std::string snapH;
        SnapInfer infer;
        if (imgHovered) snapPoint(su, sv, snapH, &infer);

        // C49: direction inference for the Line tool's SECOND point only (polygon/spline legs
        // and the 3-pt arc chord deliberately stay free). Priority: point snap > tangent > h/v.
        bool hvH = false, hvV = false;       // live h/v chip + exact-direction force
        bool tanSnap = false;                // live tangent chip
        bool tanStartUpgrade = false;        // placement click upgrades toolSnaps_[0] rim->tan
        if (inferenceOn_ && imgHovered && tool_ == SketchTool::Line && toolUV_.size() == 1) {
            const ImVec2 a = toolUV_[0];
            const float D2R = 3.14159265f / 180.0f;
            if (snapHasPrefix(snapH, "rim:")) {
                // (a) END point on a circle/arc rim: if the segment is nearly tangent there,
                // pull it to the EXACT tangent contact seen from the fixed start point.
                const SketchEntity* ce = entById(snapH.substr(4));
                if (ce && (ce->type == "circle" || ce->type == "arc") && ce->radius > 1e-5f) {
                    float cx = ce->center[0], cy = ce->center[1], r = ce->radius;
                    float dx = a.x - cx, dy = a.y - cy;
                    float d = std::hypot(dx, dy);
                    float segx = su - a.x, segy = sv - a.y;
                    float segL = std::hypot(segx, segy);
                    if (d > r + 1e-4f && segL > 1e-5f) {       // start outside; tangent exists
                        // tangent direction at the current rim point = perp(rim - center)
                        float tx = -(sv - cy), ty = su - cx;
                        float tL = std::hypot(tx, ty);
                        float cosang = std::fabs(segx * tx + segy * ty) / (segL * tL);
                        if (std::acos(std::fmin(1.0f, cosang)) < kTangentDeg * D2R) {
                            // exact contacts: center angle alpha +/- beta, cos(beta) = r/d
                            float alpha = std::atan2(dy, dx);
                            float beta = std::acos(std::fmax(-1.0f, std::fmin(1.0f, r / d)));
                            float u1 = cx + r * std::cos(alpha + beta), v1 = cy + r * std::sin(alpha + beta);
                            float u2 = cx + r * std::cos(alpha - beta), v2 = cy + r * std::sin(alpha - beta);
                            bool first = std::hypot(u1 - su, v1 - sv) <= std::hypot(u2 - su, v2 - sv);
                            su = first ? u1 : u2; sv = first ? v1 : v2;
                            snapH = "tan:" + ce->id;
                            tanSnap = true;
                            infer.alignU = infer.alignV = false;  // tangent contact owns the point
                        }
                    }
                }
            } else if (snapH.empty()) {
                // (b) START point sits on a rim: keep the segment tangent at that contact.
                if (!toolSnaps_.empty() && (snapHasPrefix(toolSnaps_[0], "rim:") ||
                                            snapHasPrefix(toolSnaps_[0], "tan:"))) {
                    const SketchEntity* ce = entById(snapPrimary(toolSnaps_[0]).substr(4));
                    if (ce && (ce->type == "circle" || ce->type == "arc")) {
                        float tx = -(a.y - ce->center[1]), ty = a.x - ce->center[0];
                        float tL = std::hypot(tx, ty);
                        float segx = su - a.x, segy = sv - a.y;
                        float segL = std::hypot(segx, segy);
                        if (tL > 1e-5f && segL > 1e-5f) {
                            float cosang = std::fabs(segx * tx + segy * ty) / (segL * tL);
                            if (std::acos(std::fmin(1.0f, cosang)) < kTangentDeg * D2R) {
                                float s = (segx * tx + segy * ty) / (tL * tL);  // project on tangent
                                su = a.x + tx * s; sv = a.y + ty * s;
                                tanSnap = true; tanStartUpgrade = true;
                                infer.alignU = infer.alignV = false;  // direction owns the point now
                            }
                        }
                    }
                }
                // (c) plain h/v direction snap (kInferHVDeg) — skipped when tangent fired.
                if (!tanSnap) {
                    float du = su - a.x, dv = sv - a.y;
                    float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                    if (ang < kInferHVDeg && (du != 0.0f || dv != 0.0f)) {
                        // drop a conflicting v-alignment (h/v force wins its own axis; an
                        // alignment on the OTHER axis combines — SW-style intersection).
                        if (infer.alignV && infer.srcV.y != a.y) infer.alignV = false;
                        sv = a.y; hvH = true;
                    } else if (ang > 90.0f - kInferHVDeg) {
                        if (infer.alignU && infer.srcU.x != a.x) infer.alignU = false;
                        su = a.x; hvV = true;
                    }
                }
            }
        }
        ImVec2 sCur = uvToScreen(su, sv);
        const ImU32 prevCol = col(pal.accent, 0.9f);
        const float pth = 1.6f * ui::uiScale;          // rubber-band / variant preview width
        const float toolDotR = 3.0f * ui::uiScale;     // placed-point preview dots

        // Rubber band from the last placed point.
        if (!toolUV_.empty()) {
            ImVec2 a = uvToScreen(toolUV_.back().x, toolUV_.back().y);
            // Slot: only rubber-band the spine while the length is still being set (1 click
            // placed). Once both spine endpoints exist, the width-stage capsule preview below
            // owns the drawing — otherwise this would draw a stray line to the cursor.
            if (tool_ == SketchTool::Line ||
                (tool_ == SketchTool::Slot && toolUV_.size() == 1)) {
                dl->AddLine(a, sCur, prevCol, pth);
            } else if (tool_ == SketchTool::RectCorner) {
                ImVec2 p0 = a, p1 = sCur;
                ImVec2 c1 = uvToScreen(su, toolUV_.back().y);
                ImVec2 c2 = uvToScreen(toolUV_.back().x, sv);
                dl->AddLine(p0, c1, prevCol, pth); dl->AddLine(c1, p1, prevCol, pth);
                dl->AddLine(p1, c2, prevCol, pth); dl->AddLine(c2, p0, prevCol, pth);
            } else if (tool_ == SketchTool::Circle) {
                float rpx = std::hypot(sCur.x - a.x, sCur.y - a.y);
                dl->AddCircle(a, rpx, prevCol, 0, pth);
            } else if (tool_ == SketchTool::ArcCenter) {
                float rpx = std::hypot(sCur.x - a.x, sCur.y - a.y);
                dl->AddCircle(a, rpx, prevCol, 0, 1.2f * ui::uiScale);
            }
        }

        // C28-B variant previews (drawn in the sketch under-defined color, matching the placed
        // entities while still in-tool). Each branch builds the live geometry from the prior clicks
        // in toolUV_ + the snapped cursor (su,sv).
        {
            const ImU32 uCol = col(pal.sketchUnder, 0.95f);
            auto pl = [&](const std::vector<ImVec2>& uv) {  // draw a (u,v) polyline
                for (size_t i = 1; i < uv.size(); ++i)
                    dl->AddLine(uvToScreen(uv[i - 1].x, uv[i - 1].y),
                                uvToScreen(uv[i].x, uv[i].y), uCol, pth);
            };
            // Arc-slot capsule preview: outer/inner arcs (r+/-hw, a0..a1 deg CCW) + two end caps.
            auto drawCapsule = [&](float cu, float cv, float rad, float a0d, float a1d, float hw) {
                const float D2R = 3.14159265f / 180.0f;
                if (a1d < a0d) a1d += 360.0f;
                auto arcPoly = [&](float rr) {
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float a = (a0d + (a1d - a0d) * (i / 64.0f)) * D2R;
                        ImVec2 q = uvToScreen(cu + rr * std::cos(a), cv + rr * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                };
                arcPoly(rad + hw); arcPoly(rad - hw);
                auto cap = [&](float aDeg) {
                    float sx = cu + rad * std::cos(aDeg * D2R), sy = cv + rad * std::sin(aDeg * D2R);
                    ImVec2 prev;
                    for (int i = 0; i <= 24; ++i) {
                        float t = (aDeg - 90.0f + 180.0f * (i / 24.0f)) * D2R;
                        ImVec2 q = uvToScreen(sx + hw * std::cos(t), sy + hw * std::sin(t));
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                };
                cap(a0d); cap(a1d);
            };
            // Straight-slot capsule preview: two side lines tangent to the end caps (parallel to
            // the spine, offset by the half-width) plus a 180-degree cap arc at each spine end.
            auto drawStraightSlot = [&](ImVec2 p1, ImVec2 p2, float hw) {
                float dx = p2.x - p1.x, dy = p2.y - p1.y; float L = std::hypot(dx, dy);
                if (L < 1e-6f) return;
                float nx = -dy / L, ny = dx / L;          // unit perpendicular to the spine
                dl->AddLine(uvToScreen(p1.x + nx * hw, p1.y + ny * hw),
                            uvToScreen(p2.x + nx * hw, p2.y + ny * hw), uCol, pth);
                dl->AddLine(uvToScreen(p1.x - nx * hw, p1.y - ny * hw),
                            uvToScreen(p2.x - nx * hw, p2.y - ny * hw), uCol, pth);
                auto cap = [&](ImVec2 c, float axis) {       // outward-facing 180-deg arc
                    ImVec2 prev;
                    for (int i = 0; i <= 24; ++i) {
                        float a = axis - 1.5707963f + 3.14159265f * (i / 24.0f);
                        ImVec2 q = uvToScreen(c.x + hw * std::cos(a), c.y + hw * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                };
                cap(p1, std::atan2(p1.y - p2.y, p1.x - p2.x));
                cap(p2, std::atan2(p2.y - p1.y, p2.x - p1.x));
                // centerline previewed as the (dashed) construction spine
                drawDashedLine(dl, uvToScreen(p1.x, p1.y), uvToScreen(p2.x, p2.y),
                               col(pal.sketchUnder, 0.7f), 1.0f, 6.0f, 4.0f);
            };
            const size_t n = toolUV_.size();
            if (tool_ == SketchTool::Rect3ptCorner && n >= 1) {
                ImVec2 A = toolUV_[0];
                ImVec2 B = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(A.x, A.y), sCur, uCol, pth); }
                else {
                    // edge A-B fixed; third click sets width (perp offset of cursor from A-B).
                    float ex = B.x - A.x, ey = B.y - A.y; float L = std::hypot(ex, ey);
                    float nx = L > 1e-6f ? -ey / L : 0, ny = L > 1e-6f ? ex / L : 0;
                    float w = (su - A.x) * nx + (sv - A.y) * ny;
                    ImVec2 D(A.x + nx * w, A.y + ny * w), C(B.x + nx * w, B.y + ny * w);
                    pl({A, B, C, D, A});
                }
            } else if (tool_ == SketchTool::Rect3ptCenter && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 mid = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    // ctr->mid is a half-edge direction (half-height); cursor sets half-width.
                    float hx = mid.x - ctr.x, hy = mid.y - ctr.y; float L = std::hypot(hx, hy);
                    float px = L > 1e-6f ? -hy / L : 0, py = L > 1e-6f ? hx / L : 0;
                    float hw = (su - ctr.x) * px + (sv - ctr.y) * py;
                    ImVec2 wv(px * hw, py * hw);
                    ImVec2 P0(ctr.x + hx + wv.x, ctr.y + hy + wv.y);
                    ImVec2 P1(ctr.x + hx - wv.x, ctr.y + hy - wv.y);
                    ImVec2 P2(ctr.x - hx - wv.x, ctr.y - hy - wv.y);
                    ImVec2 P3(ctr.x - hx + wv.x, ctr.y - hy + wv.y);
                    pl({P0, P1, P2, P3, P0});
                }
            } else if (tool_ == SketchTool::Parallelogram && n >= 1) {
                ImVec2 A = toolUV_[0];
                ImVec2 B = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(A.x, A.y), sCur, uCol, pth); }
                else {
                    ImVec2 C(su, sv);                 // third corner sets the shear
                    ImVec2 D(A.x + (C.x - B.x), A.y + (C.y - B.y));
                    pl({A, B, C, D, A});
                }
            } else if (tool_ == SketchTool::CirclePerimeter) {
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), toolDotR, uCol);
                if (pts.size() == 3) {
                    float cu, cv, rad;
                    if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = i / 48.0f * 6.2831853f;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                        }
                    }
                }
            } else if (tool_ == SketchTool::Slot && n >= 2) {
                // spine fixed by clicks 1 & 2; cursor sets the width (perp distance from spine).
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                float dx = p2.x - p1.x, dy = p2.y - p1.y; float L = std::hypot(dx, dy);
                float hw = L > 1e-6f ? std::fabs((su - p1.x) * (-dy) + (sv - p1.y) * dx) / L : 5.0f;
                if (hw < 1e-4f) hw = 1e-4f;
                drawStraightSlot(p1, p2, hw);
            } else if (tool_ == SketchTool::SlotCtrStr && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 end = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    // centerpoint slot: spine endpoints are ctr +/- (end-ctr); cursor sets width.
                    ImVec2 e1(ctr.x + (end.x - ctr.x), ctr.y + (end.y - ctr.y));
                    ImVec2 e2(ctr.x - (end.x - ctr.x), ctr.y - (end.y - ctr.y));
                    float dx = e1.x - e2.x, dy = e1.y - e2.y; float L = std::hypot(dx, dy);
                    float hw = L > 1e-6f ? std::fabs((su - e2.x) * (-dy) + (sv - e2.y) * dx) / L : 5.0f;
                    if (hw < 1e-4f) hw = 1e-4f;
                    drawStraightSlot(e1, e2, hw);
                }
            } else if (tool_ == SketchTool::SlotArcCtr && n >= 1) {
                // click 1 = arc center; 2 = spine start (sets radius+a0); 3 = spine end (a1);
                // 4 = width. Live preview at each stage with the snapped cursor.
                const float R2D = 57.29577951f;
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), toolDotR, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    ImVec2 s0 = toolUV_[1];
                    float rad = std::hypot(s0.x - ctr.x, s0.y - ctr.y);
                    float a0 = std::atan2(s0.y - ctr.y, s0.x - ctr.x) * R2D;
                    ImVec2 endPt = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    float a1 = std::atan2(endPt.y - ctr.y, endPt.x - ctr.x) * R2D;
                    float hw = 0.0f;
                    if (n >= 3) {  // width stage: perp distance of cursor from the spine radius
                        hw = std::fabs(std::hypot(su - ctr.x, sv - ctr.y) - rad);
                        if (hw < 1e-3f) hw = rad * 0.15f;
                    } else { hw = rad * 0.15f; }
                    drawCapsule(ctr.x, ctr.y, rad, a0, a1, hw);
                }
            } else if (tool_ == SketchTool::SlotArc3pt && n >= 1) {
                // clicks 1,2,3 = points ON the spine arc (circumcircle); 4 = width.
                const float R2D = 57.29577951f;
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), toolDotR, uCol);
                float cu, cv, rad;
                if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                    float a0 = std::atan2(pts[0].y - cv, pts[0].x - cu) * R2D;
                    float amid = std::atan2(pts[1].y - cv, pts[1].x - cu) * R2D;
                    float a1 = std::atan2(pts[2].y - cv, pts[2].x - cu) * R2D;
                    // Choose CCW a0->a1 that passes through the middle pick.
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    float sweep = norm(a1 - a0), midSw = norm(amid - a0);
                    if (midSw > sweep) { float t = a0; a0 = a1; a1 = t; }  // go the other way
                    float hw = (n >= 3) ? std::fabs(std::hypot(su - cu, sv - cv) - rad) : rad * 0.15f;
                    if (hw < 1e-3f) hw = rad * 0.15f;
                    drawCapsule(cu, cv, rad, a0, a1, hw);
                }
            } else if (tool_ == SketchTool::EllipsePartial && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 rxp = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    // ry from the third pick's perpendicular distance; live cursor while n==2.
                    ImVec2 ryp = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                    if (ry < 1e-4f) ry = rx * 0.5f;
                    // angle sweep: a0 from the 4th pick, a1 from the 5th/live pick.
                    auto angOf = [&](ImVec2 q) {
                        float lx = (q.x - ctr.x) * ca + (q.y - ctr.y) * sa;
                        float ly = -(q.x - ctr.x) * sa + (q.y - ctr.y) * ca;
                        return std::atan2(ly / std::fmax(ry, 1e-6f), lx / std::fmax(rx, 1e-6f));
                    };
                    float a0 = (n >= 4) ? angOf(toolUV_[3]) : 0.0f;
                    float a1 = (n >= 4) ? angOf(ImVec2(su, sv)) : 6.2831853f;
                    if (a1 < a0) a1 += 6.2831853f;
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float t = a0 + (a1 - a0) * (i / 64.0f);
                        float lx = rx * std::cos(t), ly = ry * std::sin(t);
                        ImVec2 q = uvToScreen(ctr.x + lx * ca - ly * sa, ctr.y + lx * sa + ly * ca);
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Parabola && n >= 1) {
                ImVec2 focus = toolUV_[0];
                ImVec2 vertex = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                dl->AddCircleFilled(uvToScreen(focus.x, focus.y), toolDotR, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(focus.x, focus.y), sCur, uCol, pth); }
                else {
                    // axis = vertex->focus; f = |vertex-focus|. Sample y = x^2/(4f) in axis frame
                    // over the [start..end] parametric range set by the 3rd/4th (or live) picks.
                    float ax = focus.x - vertex.x, ay = focus.y - vertex.y;
                    float f = std::hypot(ax, ay); if (f < 1e-4f) f = 1.0f;
                    float ux = ax / f, uy = ay / f;     // axis dir
                    float px = -uy, py = ux;            // perp (opening) dir
                    auto tOf = [&](ImVec2 q) { return (q.x - vertex.x) * px + (q.y - vertex.y) * py; };
                    float t0 = (n >= 3) ? tOf(toolUV_[2]) : -2.0f * f;
                    float t1 = (n >= 3) ? tOf(ImVec2(su, sv)) : 2.0f * f;
                    ImVec2 prev;
                    for (int i = 0; i <= 48; ++i) {
                        float t = t0 + (t1 - t0) * (i / 48.0f);
                        float d = t * t / (4.0f * f);    // distance along axis from vertex
                        ImVec2 q = uvToScreen(vertex.x + ux * d + px * t, vertex.y + uy * d + py * t);
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Conic && n >= 1) {
                ImVec2 P0 = toolUV_[0];
                ImVec2 P1 = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                dl->AddCircleFilled(uvToScreen(P0.x, P0.y), toolDotR, uCol);
                if (n < 2) { dl->AddLine(uvToScreen(P0.x, P0.y), sCur, uCol, pth); }
                else {
                    ImVec2 apex = (n >= 3) ? toolUV_[2] : ImVec2(su, sv);
                    // rho: 0..1 weight of the apex (shoulder). Live-pick while n==3 (cursor sets it),
                    // committed in conicRho_ once the apex is placed.
                    float rho = conicRho_;
                    if (n == 3) {  // live rho preview: project cursor onto the mid->apex segment
                        ImVec2 mid((P0.x + P1.x) * 0.5f, (P0.y + P1.y) * 0.5f);
                        float dx = apex.x - mid.x, dy = apex.y - mid.y; float L2 = dx * dx + dy * dy;
                        rho = L2 > 1e-9f ? ((su - mid.x) * dx + (sv - mid.y) * dy) / L2 : 0.5f;
                        rho = std::fmax(0.01f, std::fmin(0.99f, rho));
                    }
                    // Rational Bezier conic: B(t)=((1-t)^2 P0 + 2(1-t)t w Apex + t^2 P1)/den,
                    // den=(1-t)^2 + 2(1-t)t w + t^2, with w = rho/(1-rho).
                    float w = rho / std::fmax(1e-3f, 1.0f - rho);
                    ImVec2 prev;
                    for (int i = 0; i <= 48; ++i) {
                        float t = i / 48.0f, mt = 1.0f - t;
                        float b0 = mt * mt, b1 = 2.0f * mt * t * w, b2 = t * t;
                        float den = b0 + b1 + b2;
                        float qx = (b0 * P0.x + b1 * apex.x + b2 * P1.x) / den;
                        float qy = (b0 * P0.y + b1 * apex.y + b2 * P1.y) / den;
                        ImVec2 q = uvToScreen(qx, qy);
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                    dl->AddCircle(uvToScreen(apex.x, apex.y), 4.0f, uCol, 12, 1.2f);
                }

            // ---- C42 tool previews ----
            } else if (tool_ == SketchTool::Arc3pt && n >= 1) {
                // picks: arc start, arc end, point ON the arc (live cursor fills the missing one).
                std::vector<ImVec2> pts;
                for (size_t i = 0; i < n && i < 3; ++i) pts.push_back(toolUV_[i]);
                if (pts.size() < 3) pts.push_back(ImVec2(su, sv));
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), toolDotR, uCol);
                if (n == 1) { dl->AddLine(uvToScreen(pts[0].x, pts[0].y), sCur, uCol, pth); }
                else {
                    float cu, cv, rad;
                    if (circumcircle(pts[0], pts[1], pts[2], cu, cv, rad)) {
                        const float R2D = 57.29577951f;
                        float aS = std::atan2(pts[0].y - cv, pts[0].x - cu) * R2D;
                        float aE = std::atan2(pts[1].y - cv, pts[1].x - cu) * R2D;
                        float aM = std::atan2(pts[2].y - cv, pts[2].x - cu) * R2D;
                        auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                        bool fwd = norm(aM - aS) <= norm(aE - aS);   // CCW start->end passes thru mid?
                        float a0 = fwd ? aS : aE, a1 = fwd ? aE : aS;
                        if (a1 < a0) a1 += 360.0f;
                        const float D2R = 3.14159265f / 180.0f;
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = (a0 + (a1 - a0) * (i / 48.0f)) * D2R;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                        }
                    }
                }
            } else if (tool_ == SketchTool::ArcTangent) {
                // C54: derive the source + free endpoint each frame; the arc starts there tangent to
                // the source and sweeps to the live cursor. Highlight the snapped start; if no source
                // exists we fall back to a plain 2-point chord (treated as a near-straight arc).
                TangentArcSrc src;
                if (tangentArcSource(io.MousePos, src)) {
                    // bright dot on the tangent start + faint tangent direction tick
                    ImVec2 sS = uvToScreen(src.start.x, src.start.y);
                    dl->AddCircleFilled(sS, 4.0f, col(pal.accent, 0.95f));
                    ImVec2 tEnd = uvToScreen(src.start.x + src.tdir.x * 6.0f,
                                             src.start.y + src.tdir.y * 6.0f);
                    dl->AddLine(sS, tEnd, col(pal.accent, 0.6f), 1.2f);
                    float cu, cv, rad, a0, a1; bool ccw;
                    if (tangentArcThrough(src.start, src.tdir, ImVec2(su, sv),
                                          cu, cv, rad, a0, a1, ccw)) {
                        if (a1 < a0) a1 += 360.0f;
                        float lo = ccw ? a0 : a1, hi = ccw ? a1 : a0;
                        if (hi < lo) hi += 360.0f;
                        const float D2R = 3.14159265f / 180.0f;
                        ImVec2 prev;
                        for (int i = 0; i <= 48; ++i) {
                            float a = (lo + (hi - lo) * (i / 48.0f)) * D2R;
                            ImVec2 q = uvToScreen(cu + rad * std::cos(a), cv + rad * std::sin(a));
                            if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                        }
                    } else {
                        // Degenerate (cursor on the tangent line): preview a straight chord.
                        dl->AddLine(sS, sCur, uCol, pth);
                    }
                } else {
                    // No source -> behave like a 3-pt arc's first leg (rubber chord from cursor).
                    dl->AddCircleFilled(sCur, toolDotR, uCol);
                }
            } else if (tool_ == SketchTool::Polygon && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), toolDotR, uCol);
                float vx = su - ctr.x, vy = sv - ctr.y;
                float R = std::hypot(vx, vy);
                if (R < 1e-4f) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    float a0 = std::atan2(vy, vx);
                    ImVec2 prev;
                    for (int i = 0; i <= polySides_; ++i) {
                        float a = a0 + 6.2831853f * i / (float)polySides_;
                        ImVec2 q = uvToScreen(ctr.x + R * std::cos(a), ctr.y + R * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::Star && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), toolDotR, uCol);
                float vx = su - ctr.x, vy = sv - ctr.y;
                float R = std::hypot(vx, vy);
                if (R < 1e-4f) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    float a0 = std::atan2(vy, vx);
                    float Ri = R * polyStarRatio_;
                    int pts = polyStarPoints_;
                    ImVec2 prev;
                    for (int i = 0; i <= 2 * pts; ++i) {
                        float a = a0 + (float)i * 3.14159265f / (float)pts;
                        float r = (i & 1) ? Ri : R;
                        ImVec2 q = uvToScreen(ctr.x + r * std::cos(a), ctr.y + r * std::sin(a));
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                }
            } else if (tool_ == SketchTool::RectCenter && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                dl->AddCircleFilled(uvToScreen(ctr.x, ctr.y), toolDotR, uCol);
                float dx = std::fabs(su - ctr.x), dy = std::fabs(sv - ctr.y);
                ImVec2 A(ctr.x - dx, ctr.y - dy), B(ctr.x + dx, ctr.y - dy);
                ImVec2 C(ctr.x + dx, ctr.y + dy), D(ctr.x - dx, ctr.y + dy);
                pl({A, B, C, D, A});
            } else if (tool_ == SketchTool::Ellipse && n >= 1) {
                ImVec2 ctr = toolUV_[0];
                ImVec2 rxp = (n >= 2) ? toolUV_[1] : ImVec2(su, sv);
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                if (n < 2) { dl->AddLine(uvToScreen(ctr.x, ctr.y), sCur, uCol, pth); }
                else {
                    float ry = std::fabs(-(su - ctr.x) * sa + (sv - ctr.y) * ca);
                    if (ry < 1e-4f) ry = rx * 0.5f;
                    ImVec2 prev;
                    for (int i = 0; i <= 64; ++i) {
                        float a = i / 64.0f * 6.2831853f;
                        float lx = rx * std::cos(a), ly = ry * std::sin(a);
                        ImVec2 q = uvToScreen(ctr.x + lx * ca - ly * sa, ctr.y + lx * sa + ly * ca);
                        if (i) dl->AddLine(prev, q, uCol, pth); prev = q;
                    }
                }
            } else if ((tool_ == SketchTool::Spline || tool_ == SketchTool::StyleSpline) && n >= 1) {
                std::vector<ImVec2> pts(toolUV_.begin(), toolUV_.end());
                pts.push_back(ImVec2(su, sv));
                pl(pts);
                for (auto& q : pts) dl->AddCircleFilled(uvToScreen(q.x, q.y), toolDotR, uCol);
            }
        }

        // ---- C49: inference feedback (dashed alignment lines, snap markers, relation chips) ----
        const ImU32 inferCol = col(pal.sketchInfer);
        // Soft light backing under a snap marker so the amber ink stays legible over dense
        // geometry / dark faces (glassIconHalo-style, canvas-toned).
        const ImU32 markerHalo = col(pal.viewportTop, 0.65f);
        if (infer.alignU || infer.alignV) {
            // Dashed line from the alignment source point to the cursor (positional only).
            auto dashed = [&](ImVec2 a, ImVec2 b) {
                float dx = b.x - a.x, dy = b.y - a.y;
                float L = std::hypot(dx, dy);
                if (L < 1.0f) return;
                const float dash = 6.0f * ui::uiScale, gap = 4.0f * ui::uiScale;
                for (float t = 0; t < L; t += dash + gap) {
                    float e = std::fmin(t + dash, L);
                    dl->AddLine(ImVec2(a.x + dx * (t / L), a.y + dy * (t / L)),
                                ImVec2(a.x + dx * (e / L), a.y + dy * (e / L)), inferCol,
                                1.2f * ui::uiScale);
                }
            };
            if (infer.alignU) dashed(uvToScreen(infer.srcU.x, infer.srcU.y), sCur);
            if (infer.alignV) dashed(uvToScreen(infer.srcV.x, infer.srcV.y), sCur);
        }
        // Cursor marker.
        dl->AddCircle(sCur, 4.0f * ui::uiScale, prevCol, 0, 1.5f * ui::uiScale);
        // Snap markers (unified, ui::uiScale-scaled): yellow-ish dot = endpoint/point snap,
        // concentric rings = center snap, diamond = midpoint snap, plain ring = rim contact.
        if (!snapH.empty()) {
            const float ms = 5.0f * ui::uiScale;
            const float w16 = 1.6f * ui::uiScale, w12 = 1.2f * ui::uiScale;
            dl->AddCircleFilled(sCur, ms * 2.4f, markerHalo, 0);
            if (snapHasPrefix(snapH, "mid:")) {
                ImVec2 d[4] = {ImVec2(sCur.x, sCur.y - ms * 1.5f), ImVec2(sCur.x + ms * 1.5f, sCur.y),
                               ImVec2(sCur.x, sCur.y + ms * 1.5f), ImVec2(sCur.x - ms * 1.5f, sCur.y)};
                dl->AddQuad(d[0], d[1], d[2], d[3], inferCol, w16);
            } else if (snapHasPrefix(snapH, "rim:") || snapHasPrefix(snapH, "tan:")) {
                dl->AddCircle(sCur, ms * 1.4f, inferCol, 0, w16);
            } else if (snapH.find(".center") != std::string::npos) {
                dl->AddCircle(sCur, ms, inferCol, 0, w16);
                dl->AddCircle(sCur, ms * 1.9f, inferCol, 0, w12);
            } else {  // endpoint / point / spline through-point
                dl->AddCircleFilled(sCur, ms * 0.7f, inferCol);
                dl->AddCircle(sCur, ms * 1.4f, inferCol, 0, w12);
            }
        }
        // Live relation chips near the cursor (same style + vector glyphs as the C41 badges).
        if (hvH || hvV || tanSnap) {
            const float bs2 = 16.0f * ui::uiScale;
            float chipX = sCur.x + bs2;                       // below-right of the cursor
            auto chip = [&](const char* relType) {
                ImVec2 gp(chipX, sCur.y + bs2);
                ImVec2 c0(gp.x - bs2 * 0.5f, gp.y - bs2 * 0.5f), c1(gp.x + bs2 * 0.5f, gp.y + bs2 * 0.5f);
                dl->AddRectFilled(c0, c1, col(pal.sketchRelBg), 3.5f * ui::uiScale);
                dl->AddRect(c0, c1, col(pal.sketchRelBorder), 3.5f * ui::uiScale, 0,
                            1.2f * ui::uiScale);
                drawRelationGlyph(dl, gp, bs2, relType, col(pal.sketchInk));
                chipX += bs2 + 2.0f * ui::uiScale;
            };
            if (hvH) chip("horizontal");
            if (hvV) chip("vertical");
            if (tanSnap) chip("tangent");
        }

        // Click placement. EqCurve takes no viewport pick (origin-relative; its popup opens on the
        // ribbon command). Text takes exactly one placement click and then defers to its popup, so
        // suppress further captures while that popup is open.
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            tool_ != SketchTool::EqCurve && tool_ != SketchTool::Airfoil &&
            !(tool_ == SketchTool::Text && textPopupOpen_)) {
            // When no point snap fired but the click landed on a v/h alignment guide, record the
            // alignment so the commit can write it as a real vertical/horizontal relation (the new
            // point's handle is filled in per-tool via pushSnapConstraint). A rim snap pulled
            // inline with the origin/center keeps the rim record and carries the alignment as a
            // ';' rider. Positional-only if the toggle is off — then only the pulled coordinate
            // (and any rim relation) persists.
            std::string clickSnap = snapH;
            if ((clickSnap.empty() || snapHasPrefix(clickSnap, "rim:")) && autoAlignRelations_ &&
                inferenceOn_ && (infer.alignU || infer.alignV)) {
                std::string uH = infer.alignU ? infer.srcUHandle : std::string();
                std::string vH = infer.alignV ? infer.srcVHandle : std::string();
                if (!uH.empty() || !vH.empty())
                    clickSnap += (clickSnap.empty() ? "align:" : ";align:") + uH + "|" + vH;
            }
            toolUV_.push_back(ImVec2(su, sv));
            toolSnaps_.push_back(clickSnap);
            // Sketch Text — the single click captures the lower-left anchor; open the popup that
            // collects the string + cap height. The entity is emitted from the popup's OK.
            if (tool_ == SketchTool::Text && toolUV_.size() == 1) {
                textAnchorUV_ = toolUV_[0];
                textPopupPos_ = io.MousePos;
                textPopupOpen_ = true;
                textFocus_ = true;
                toolUV_.clear(); toolSnaps_.clear();   // anchor stashed; stop click capture
            } else
            // Tools that complete on a fixed number of clicks:
            // C54: Tangent Arc — ONE click places the end. The start + tangent come from the source
            // entity (resolved at click time). We commit an arc tangent to the source there, joined
            // by a coincident at the shared endpoint and a tangent relation (arc <-> source) so it
            // stays tangent under drag. No source / degenerate geometry -> toast a hint and reset.
            if (tool_ == SketchTool::ArcTangent && toolUV_.size() == 1) {
                ImVec2 endPt = toolUV_[0];
                TangentArcSrc src;
                float cu, cv, rad, a0, a1; bool ccw;
                if (!tangentArcSource(io.MousePos, src)) {
                    st.toast(ui::ToastLevel::Info,
                             "Tangent Arc: draw or select a line/arc first, then click the arc end");
                    resetTool(); tool_ = SketchTool::ArcTangent;
                } else if (!tangentArcThrough(src.start, src.tdir, endPt,
                                              cu, cv, rad, a0, a1, ccw)) {
                    st.toast(ui::ToastLevel::Info,
                             "Tangent Arc: the end is on the tangent line (no arc) — pick a point off it");
                    resetTool(); tool_ = SketchTool::ArcTangent;
                } else {
                    // The solved arc always sweeps start->end; store CCW with the matching angle order
                    // (solver drops the ccw flag, so a false flag would re-render mirrored). When the
                    // geometric sweep is CW, store the CCW arc end->start (same curve) and join the
                    // source endpoint to whichever arc handle landed on the start.
                    float sStart = a0, sEnd = a1;
                    const char* startHandle = "$self.start";
                    if (!ccw) { float t = sStart; sStart = sEnd; sEnd = t; startHandle = "$self.end"; }
                    nlohmann::json ent = {{"type", "arc"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad},
                        {"start", sStart}, {"end", sEnd}, {"ccw", true}};
                    // Relations: coincident joining the source endpoint to the arc's start handle +
                    // tangent(arc, source). For a whole-circle source there's no endpoint sub-handle
                    // (src.handle == src.id) so we skip the coincident and keep only the tangent.
                    nlohmann::json cs = nlohmann::json::array();
                    if (src.handle != src.id)
                        cs.push_back({{"type", "coincident"}, {"refs", {src.handle, startHandle}}});
                    cs.push_back({{"type", "tangent"}, {"refs", {"$self", src.id}}});
                    addEntity(st, ent, cs);
                    resetTool(); tool_ = SketchTool::ArcTangent;   // stay armed for another tangent arc
                }
            } else if (tool_ == SketchTool::Line && toolUV_.size() == 2) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1];
                nlohmann::json cs = nlohmann::json::array();
                // C49: a rim-snapped START whose placement direction matched gets the tangent too.
                if (tanStartUpgrade && snapHasPrefix(toolSnaps_[0], "rim:"))
                    toolSnaps_[0] = "tan:" + toolSnaps_[0].substr(4);
                bool tangentSeg = snapHasPrefix(toolSnaps_[0], "tan:") ||
                                  snapHasPrefix(toolSnaps_[1], "tan:");
                // Auto h/v relation (inference only). When the END landed on a snapped point that
                // point wins: only add h/v if the result is EXACTLY axis-aligned. A tangent-snapped
                // direction never gets h/v stacked on top.
                float du = p2.x - p1.x, dv = p2.y - p1.y;
                if (inferenceOn_ && !tangentSeg) {
                    float ang = std::atan2(std::fabs(dv), std::fabs(du)) * 57.2957795f;
                    float tol = toolSnaps_[1].empty() ? kHVDeg : 0.01f;
                    if (ang < tol) cs.push_back({{"type", "horizontal"}, {"refs", {"$self"}}});
                    else if (ang > 90.0f - tol) cs.push_back({{"type", "vertical"}, {"refs", {"$self"}}});
                }
                // C49: snap-derived constraint for one endpoint (pt = "$self.p1"/"$self.p2"):
                // plain handle -> coincident; "mid:" -> midpoint; "rim:"/"tan:" -> point_on
                // (+ one tangent per segment).
                bool tangentEmitted = false;
                auto snapCons = [&](const std::string& rec, const char* pt) {
                    const std::string snap = snapPrimary(rec);
                    if (snap.size() < rec.size())   // alignment rider ("rim:e5;align:|origin")
                        pushSnapConstraint(rec.substr(snap.size() + 1), pt, cs);
                    if (snap.empty()) return;
                    if (snapHasPrefix(snap, "align:")) { pushSnapConstraint(snap, pt, cs); return; }
                    if (snapHasPrefix(snap, "mid:")) {
                        cs.push_back({{"type", "midpoint"}, {"refs", {pt, snap.substr(4)}}});
                    } else if (snapHasPrefix(snap, "rim:") || snapHasPrefix(snap, "tan:")) {
                        std::string eid = snap.substr(4);
                        cs.push_back({{"type", "point_on"}, {"refs", {pt, eid}}});
                        if (snapHasPrefix(snap, "tan:") && !tangentEmitted) {
                            cs.push_back({{"type", "tangent"}, {"refs", {"$self", eid}}});
                            tangentEmitted = true;
                        }
                    } else {
                        cs.push_back({{"type", "coincident"}, {"refs", {snap, pt}}});
                    }
                };
                // C37-D: start endpoint — from the chained predecessor's p2 (so a continued chain
                // stays constrained) OR from the start click's snap.
                if (!chainPrevId_.empty())
                    cs.push_back({{"type", "coincident"}, {"refs", {chainPrevId_ + ".p2", "$self.p1"}}});
                else
                    snapCons(toolSnaps_[0], "$self.p1");
                // C49: the END click's snap is now constrained too (was positional-only before).
                snapCons(toolSnaps_[1], "$self.p2");
                // Centerline flavor: construction line (dashed "for construction only"). Captured
                // now because resetTool() below clears centerline_ before the chain re-arms.
                const bool wasCenterline = centerline_;
                nlohmann::json ent = {{"type", "line"},
                    {"p1", nlohmann::json::array({p1.x, p1.y})},
                    {"p2", nlohmann::json::array({p2.x, p2.y})}};
                if (wasCenterline) ent["construction"] = true;
                // Apply directly so we can capture the new line id for the next chain segment.
                nlohmann::json op = {{"op", "sketch_add_entity"}, {"sketch", fid_},
                                     {"entity", ent}, {"constraints", cs}};
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
                std::string newId;
                if (!r.ok) {
                    st.toast(ui::ToastLevel::Error,
                             std::string(wasCenterline ? "Centerline: " : "Line: ") + r.error_message);
                } else {
                    if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
                    newId = (r.id_map.is_object() && r.id_map.contains("$0"))
                          ? r.id_map["$0"].get<std::string>() : std::string();
                    // Centerline: exaggerated midpoint marker — a construction point pinned to the
                    // segment's midpoint (no distinct engine centerline entity; this is the marker).
                    if (wasCenterline && !newId.empty()) {
                        ImVec2 mid((p1.x + p2.x) * 0.5f, (p1.y + p2.y) * 0.5f);
                        std::string mp = emitConstructionEntity(st,
                            {{"type", "point"}, {"construction", true},
                             {"p", nlohmann::json::array({mid.x, mid.y})}},
                            nlohmann::json::array());
                        if (!mp.empty())
                            emitConstructionConstraint(st,
                                {{"type", "midpoint"}, {"refs", {mp + ".p", newId}}});
                    }
                    resolve(st);
                }
                // Chain: keep drawing from the new endpoint, remembering this segment's id so the
                // next segment's p1 gets a coincident to its p2.
                ImVec2 chainStart = p2;
                resetTool(); tool_ = SketchTool::Line;
                centerline_ = wasCenterline;   // continued segments stay centerlines
                chainPrevId_ = newId;
                toolUV_.push_back(chainStart);
                toolSnaps_.push_back("");
            } else if (tool_ == SketchTool::RectCorner && toolUV_.size() == 2) {
                ImVec2 a = toolUV_[0], b = toolUV_[1];
                // Axis-aligned rect: 4 coincident-chained lines with per-edge h/v relations.
                // The complete loop, snaps, and relations commit atomically in one undo step.
                ImVec2 quad[4] = {{a.x, a.y}, {b.x, a.y}, {b.x, b.y}, {a.x, b.y}};
                const char* rel[4] = {"horizontal", "vertical", "horizontal", "vertical"};
                // The two clicked corners are quad[0] (first pick) and quad[2] (opposite); keep their
                // placement snaps so a corner dropped on the origin/a vertex stays coincident.
                std::string cornerSnaps[4] = {toolSnaps_[0], "", toolSnaps_[1], ""};
                addQuadLoop(st, quad, rel, cornerSnaps);
                resetTool();
            } else if (tool_ == SketchTool::Circle && toolUV_.size() == 2) {
                ImVec2 c = toolUV_[0], rim = toolUV_[1];
                float rad = std::hypot(rim.x - c.x, rim.y - c.y);
                nlohmann::json ent = {{"type", "circle"},
                    {"center", nlohmann::json::array({c.x, c.y})}, {"radius", rad}};
                // Persist the placement snaps: center -> $self.center; the rim click's snapped
                // point (a plain vertex) lies ON the circle (point_on). A rim snap onto another
                // curve at the radius pick is left positional (ambiguous curve-curve relation).
                nlohmann::json cs = nlohmann::json::array();
                pushSnapConstraint(toolSnaps_[0], "$self.center", cs);
                if (const std::string onRef = coincRef(toolSnaps_[1]); !onRef.empty())
                    cs.push_back({{"type", "point_on"}, {"refs", {onRef, "$self"}}});
                addEntity(st, ent, cs);
                resetTool();
            } else if (tool_ == SketchTool::ArcCenter && toolUV_.size() == 3) {
                ImVec2 c = toolUV_[0], p1 = toolUV_[1], p2 = toolUV_[2];
                float rad = std::hypot(p1.x - c.x, p1.y - c.y);
                float a0 = std::atan2(p1.y - c.y, p1.x - c.x) * 57.2957795f;
                float a1 = std::atan2(p2.y - c.y, p2.x - c.x) * 57.2957795f;
                nlohmann::json ent = {{"type", "arc"},
                    {"center", nlohmann::json::array({c.x, c.y})}, {"radius", rad},
                    {"start", a0}, {"end", a1}, {"ccw", true}};
                // Persist placement snaps: center pick -> $self.center, the two rim picks ->
                // $self.start / $self.end (p1 defines the radius, p2 the end angle; both stay CCW).
                nlohmann::json cs = nlohmann::json::array();
                pushSnapConstraint(toolSnaps_[0], "$self.center", cs);
                pushSnapConstraint(toolSnaps_[1], "$self.start", cs);
                pushSnapConstraint(toolSnaps_[2], "$self.end", cs);
                addEntity(st, ent, cs);
                resetTool();
            } else if (tool_ == SketchTool::Slot && toolUV_.size() == 3) {
                ImVec2 p1 = toolUV_[0], p2 = toolUV_[1], wpt = toolUV_[2];
                // width = perpendicular distance from wpt to the p1-p2 line.
                float dx = p2.x - p1.x, dy = p2.y - p1.y;
                float L = std::hypot(dx, dy);
                float w = L > 1e-6f ? std::fabs((wpt.x - p1.x) * (-dy) + (wpt.y - p1.y) * dx) / L : 5.0f;
                if (w < 1e-3f) w = 5.0f;
                // C51: emit a decomposed slot (2 lines + 2 arcs + construction centerline) so every
                // piece is individually selectable / dimensionable. The two spine endpoints carry
                // their placement snaps (toolSnaps_[0]/[1]); the width pick (toolSnaps_[2]) doesn't.
                addDecomposedSlot(st, p1, p2, w * 2.0f, toolSnaps_[0], toolSnaps_[1]);
                resetTool();

            // ---- C28-B variants ----
            } else if (tool_ == SketchTool::Rect3ptCorner && toolUV_.size() == 3) {
                // A, B (one edge + angle), C (width by perp offset). Rotated rect: opposite sides
                // parallel+equal, adjacent perpendicular.
                ImVec2 A = toolUV_[0], B = toolUV_[1], C3 = toolUV_[2];
                float ex = B.x - A.x, ey = B.y - A.y; float L = std::hypot(ex, ey);
                float nx = L > 1e-6f ? -ey / L : 0, ny = L > 1e-6f ? ex / L : 0;
                float w = (C3.x - A.x) * nx + (C3.y - A.y) * ny;
                ImVec2 D(A.x + nx * w, A.y + ny * w), C(B.x + nx * w, B.y + ny * w);
                ImVec2 quad[4] = {A, B, C, D};
                const char* rel[4] = {"parallel perpendicular", "parallel", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::Rect3ptCenter && toolUV_.size() == 3) {
                // center, edge-midpoint dir (half-height), half-width pick.
                ImVec2 ctr = toolUV_[0], mid = toolUV_[1], wpk = toolUV_[2];
                float hx = mid.x - ctr.x, hy = mid.y - ctr.y; float L = std::hypot(hx, hy);
                float px = L > 1e-6f ? -hy / L : 0, py = L > 1e-6f ? hx / L : 0;
                float hw = (wpk.x - ctr.x) * px + (wpk.y - ctr.y) * py;
                ImVec2 wv(px * hw, py * hw);
                ImVec2 P0(ctr.x + hx + wv.x, ctr.y + hy + wv.y);
                ImVec2 P1(ctr.x + hx - wv.x, ctr.y + hy - wv.y);
                ImVec2 P2(ctr.x - hx - wv.x, ctr.y - hy - wv.y);
                ImVec2 P3(ctr.x - hx + wv.x, ctr.y - hy + wv.y);
                ImVec2 quad[4] = {P0, P1, P2, P3};
                const char* rel[4] = {"parallel perpendicular", "parallel", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::Parallelogram && toolUV_.size() == 3) {
                // 3 corners -> opposite sides parallel+equal, NO perpendicular.
                ImVec2 A = toolUV_[0], B = toolUV_[1], C = toolUV_[2];
                ImVec2 D(A.x + (C.x - B.x), A.y + (C.y - B.y));
                ImVec2 quad[4] = {A, B, C, D};
                const char* rel[4] = {"parallel equal", "parallel equal", nullptr, nullptr};
                addQuadLoop(st, quad, rel);
                resetTool();
            } else if (tool_ == SketchTool::CirclePerimeter && toolUV_.size() == 3) {
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    // collinear/degenerate guard: drop the 3rd pick, keep first two, hint via log.
                    st.addLog("[sketch] Perimeter Circle: 3 points are collinear — pick a 3rd off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    nlohmann::json ent = {{"type", "circle"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::SlotCtrStr && toolUV_.size() == 3) {
                // center, end-direction/length, width. Spine = center +/- (end-center).
                ImVec2 ctr = toolUV_[0], end = toolUV_[1], wpk = toolUV_[2];
                ImVec2 e1(ctr.x + (end.x - ctr.x), ctr.y + (end.y - ctr.y));
                ImVec2 e2(ctr.x - (end.x - ctr.x), ctr.y - (end.y - ctr.y));
                float dx = e1.x - e2.x, dy = e1.y - e2.y; float L = std::hypot(dx, dy);
                float w = L > 1e-6f ? std::fabs((wpk.x - e2.x) * (-dy) + (wpk.y - e2.y) * dx) / L : 5.0f;
                if (w < 1e-3f) w = 5.0f;
                // C51: decomposed slot (real lines + arcs + construction centerline). e1 is the
                // clicked end (carries toolSnaps_[1]); e2 is its mirror about the center (no pick).
                addDecomposedSlot(st, e1, e2, w * 2.0f, toolSnaps_[1], "");
                resetTool();
            } else if (tool_ == SketchTool::SlotArcCtr && toolUV_.size() == 4) {
                // center, spine start (radius+a0), spine end dir (a1), width.
                const float R2D = 57.29577951f;
                ImVec2 ctr = toolUV_[0], s0 = toolUV_[1], endPt = toolUV_[2], wpk = toolUV_[3];
                float rad = std::hypot(s0.x - ctr.x, s0.y - ctr.y);
                if (rad < 1e-4f) { st.addLog("[sketch] Arc Slot: center == start"); resetTool(); }
                else {
                    float a0 = std::atan2(s0.y - ctr.y, s0.x - ctr.x) * R2D;
                    float a1 = std::atan2(endPt.y - ctr.y, endPt.x - ctr.x) * R2D;
                    if (a1 <= a0) a1 += 360.0f;            // enforce CCW a0 -> a1
                    // width = total capsule width = 2 * |dist(wpk, center) - radius|.
                    float w = 2.0f * std::fabs(std::hypot(wpk.x - ctr.x, wpk.y - ctr.y) - rad);
                    if (w < 1e-3f || w >= 2.0f * rad) w = std::fmin(rad * 0.3f, rad * 0.9f);
                    nlohmann::json ent = {{"type", "arc_slot"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"radius", rad}, {"a0", a0}, {"a1", a1}, {"width", w}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::SlotArc3pt && toolUV_.size() == 4) {
                // 3 points on the spine arc (circumcircle), then width.
                const float R2D = 57.29577951f;
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    st.addLog("[sketch] 3Pt Arc Slot: 3 points are collinear — pick a 3rd off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();   // drop the width pick, keep retrying
                } else {
                    ImVec2 p0 = toolUV_[0], pm = toolUV_[1], p2 = toolUV_[2], wpk = toolUV_[3];
                    float a0 = std::atan2(p0.y - cv, p0.x - cu) * R2D;
                    float amid = std::atan2(pm.y - cv, pm.x - cu) * R2D;
                    float a1 = std::atan2(p2.y - cv, p2.x - cu) * R2D;
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    float sweep = norm(a1 - a0), midSw = norm(amid - a0);
                    if (midSw > sweep) { float t = a0; a0 = a1; a1 = t; }  // CCW must pass thru mid
                    if (a1 <= a0) a1 += 360.0f;
                    float w = 2.0f * std::fabs(std::hypot(wpk.x - cu, wpk.y - cv) - rad);
                    if (w < 1e-3f || w >= 2.0f * rad) w = std::fmin(rad * 0.3f, rad * 0.9f);
                    nlohmann::json ent = {{"type", "arc_slot"},
                        {"center", nlohmann::json::array({cu, cv})},
                        {"radius", rad}, {"a0", a0}, {"a1", a1}, {"width", w}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::EllipsePartial && toolUV_.size() == 5) {
                // center, rx point, ry/rot point, start-angle, end-angle.
                ImVec2 ctr = toolUV_[0], rxp = toolUV_[1], ryp = toolUV_[2];
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                if (rx < 1e-4f || ry < 1e-4f) { st.addLog("[sketch] Partial Ellipse: degenerate radii"); resetTool(); }
                else {
                    auto angOf = [&](ImVec2 q) {
                        float lx = (q.x - ctr.x) * ca + (q.y - ctr.y) * sa;
                        float ly = -(q.x - ctr.x) * sa + (q.y - ctr.y) * ca;
                        return std::atan2(ly / ry, lx / rx);
                    };
                    // Contract (doc_schema.md §3.1): rot/a0/a1 are DEGREES, angles in the
                    // un-rotated ellipse frame, CCW a0 -> a1.
                    const float kR2D = 57.29577951f;
                    float a0 = angOf(toolUV_[3]) * kR2D, a1 = angOf(toolUV_[4]) * kR2D;
                    if (a1 <= a0) a1 += 360.0f;   // enforce CCW sweep
                    nlohmann::json ent = {{"type", "ellipse_arc"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"rx", rx}, {"ry", ry}, {"rot", rot * kR2D}, {"a0", a0}, {"a1", a1}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::Parabola && toolUV_.size() == 4) {
                // focus, vertex, start, end picks. Contract (doc_schema.md §3.1) stores the
                // range as t0/t1: signed extents along w = perp(unit(focus - vertex)), i.e.
                // t = (P - vertex)·w in the parabola's own frame.
                ImVec2 focus = toolUV_[0], vertex = toolUV_[1], s0 = toolUV_[2], s1 = toolUV_[3];
                float ux = focus.x - vertex.x, uy = focus.y - vertex.y;
                float ul = std::hypot(ux, uy);
                if (ul < 1e-4f) { st.addLog("[sketch] Parabola: focus == vertex"); resetTool(); }
                else {
                    ux /= ul; uy /= ul;
                    float wx = -uy, wy = ux;   // perp(u)
                    float t0 = (s0.x - vertex.x) * wx + (s0.y - vertex.y) * wy;
                    float t1 = (s1.x - vertex.x) * wx + (s1.y - vertex.y) * wy;
                    nlohmann::json ent = {{"type", "parabola"},
                        {"vertex", nlohmann::json::array({vertex.x, vertex.y})},
                        {"focus", nlohmann::json::array({focus.x, focus.y})},
                        {"t0", t0}, {"t1", t1}};
                    addEntity(st, ent, nlohmann::json::array());
                    resetTool();
                }
            } else if (tool_ == SketchTool::Conic && toolUV_.size() == 4) {
                // p0, p1, apex, then a 4th click sets rho (projection onto mid->apex).
                ImVec2 P0 = toolUV_[0], P1 = toolUV_[1], apex = toolUV_[2], rhoPk = toolUV_[3];
                ImVec2 mid((P0.x + P1.x) * 0.5f, (P0.y + P1.y) * 0.5f);
                float dx = apex.x - mid.x, dy = apex.y - mid.y; float L2 = dx * dx + dy * dy;
                float rho = L2 > 1e-9f ? ((rhoPk.x - mid.x) * dx + (rhoPk.y - mid.y) * dy) / L2 : 0.5f;
                rho = std::fmax(0.01f, std::fmin(0.99f, rho));
                nlohmann::json ent = {{"type", "conic"},
                    {"p0", nlohmann::json::array({P0.x, P0.y})},
                    {"p1", nlohmann::json::array({P1.x, P1.y})},
                    {"apex", nlohmann::json::array({apex.x, apex.y})},
                    {"rho", rho}};
                addEntity(st, ent, nlohmann::json::array());
                resetTool();

            // ---- C42 tool completions ----
            } else if (tool_ == SketchTool::Arc3pt && toolUV_.size() == 3) {
                // p1 = arc start, p2 = arc end, p3 = point ON the arc -> circumcircle; ccw chosen
                // so the sweep from start to end passes through p3.
                float cu, cv, rad;
                if (!circumcircle(toolUV_[0], toolUV_[1], toolUV_[2], cu, cv, rad)) {
                    st.addLog("[sketch] 3-Pt Arc: points are collinear \xE2\x80\x94 pick the on-arc point off the line");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    const float R2D = 57.29577951f;
                    float aS = std::atan2(toolUV_[0].y - cv, toolUV_[0].x - cu) * R2D;
                    float aE = std::atan2(toolUV_[1].y - cv, toolUV_[1].x - cu) * R2D;
                    float aM = std::atan2(toolUV_[2].y - cv, toolUV_[2].x - cu) * R2D;
                    auto norm = [](float a) { while (a < 0) a += 360.0f; while (a >= 360.0f) a -= 360.0f; return a; };
                    // Always emit a CCW arc (the solver's solved records drop "ccw", so a false
                    // flag would re-render wrong after the first solve): if the CCW sweep p1->p2
                    // misses p3, store the arc as CCW p2->p1 instead (same geometry).
                    bool fwd = norm(aM - aS) <= norm(aE - aS);
                    float a0 = fwd ? aS : aE, a1 = fwd ? aE : aS;
                    // Endpoint snaps -> coincidents onto the arc's start/end handles (like Line);
                    // when reversed, p1 sits on the "end" handle and p2 on "start".
                    // C49: only PLAIN point snaps become coincidents here — midpoint/rim snaps on
                    // arc picks stay positional (coincRef filters the prefixed records).
                    nlohmann::json cs = nlohmann::json::array();
                    std::string s0 = coincRef(toolSnaps_[0]), s1 = coincRef(toolSnaps_[1]);
                    if (!s0.empty())
                        cs.push_back({{"type", "coincident"},
                                      {"refs", {s0, fwd ? "$self.start" : "$self.end"}}});
                    if (!s1.empty())
                        cs.push_back({{"type", "coincident"},
                                      {"refs", {s1, fwd ? "$self.end" : "$self.start"}}});
                    nlohmann::json ent = {{"type", "arc"},
                        {"center", nlohmann::json::array({cu, cv})}, {"radius", rad},
                        {"start", a0}, {"end", a1}, {"ccw", true}};
                    addEntity(st, ent, cs);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Polygon && toolUV_.size() == 2) {
                // center + one vertex -> inscribed N-gon (closed equal-edged line loop).
                ImVec2 ctr = toolUV_[0], vtx = toolUV_[1];
                float R = std::hypot(vtx.x - ctr.x, vtx.y - ctr.y);
                if (R < 1e-4f) {
                    st.addLog("[sketch] Polygon: vertex == center \xE2\x80\x94 pick a vertex away from the center");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    float a0 = std::atan2(vtx.y - ctr.y, vtx.x - ctr.x);
                    std::vector<ImVec2> corners;
                    for (int i = 0; i < polySides_; ++i) {
                        float a = a0 + 6.2831853f * i / (float)polySides_;
                        corners.push_back(ImVec2(ctr.x + R * std::cos(a), ctr.y + R * std::sin(a)));
                    }
                    // The construction circle and its center/vertex relations commit with the loop.
                    addPolyLoop(st, corners, /*equalEdges=*/true, &ctr,
                                toolSnaps_[0], toolSnaps_[1]);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Star && toolUV_.size() == 2) {
                // center + one tip vertex -> 2*polyStarPoints_ alternating outer/inner corners.
                ImVec2 ctr = toolUV_[0], vtx = toolUV_[1];
                float R = std::hypot(vtx.x - ctr.x, vtx.y - ctr.y);
                if (R < 1e-4f) {
                    st.addLog("[sketch] Star: vertex == center \xE2\x80\x94 pick a tip away from the center");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    float a0 = std::atan2(vtx.y - ctr.y, vtx.x - ctr.x);
                    float Ri = R * polyStarRatio_;
                    int pts = polyStarPoints_;
                    std::vector<ImVec2> corners;
                    corners.reserve(2 * pts);
                    for (int i = 0; i < 2 * pts; ++i) {
                        float a = a0 + (float)i * 3.14159265f / (float)pts;
                        float r = (i & 1) ? Ri : R;
                        corners.push_back(ImVec2(ctr.x + r * std::cos(a), ctr.y + r * std::sin(a)));
                    }
                    addPolyLoop(st, corners, /*equalEdges=*/false);
                    resetTool();
                }
            } else if (tool_ == SketchTool::RectCenter && toolUV_.size() == 2) {
                // center + corner -> axis-aligned rect (same per-edge h/v relations as RectCorner).
                ImVec2 ctr = toolUV_[0], cor = toolUV_[1];
                float dx = std::fabs(cor.x - ctr.x), dy = std::fabs(cor.y - ctr.y);
                if (dx < 1e-4f || dy < 1e-4f) {
                    st.addLog("[sketch] Center Rectangle: zero width/height \xE2\x80\x94 pick a corner off the axes");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    ImVec2 quad[4] = {ImVec2(ctr.x - dx, ctr.y - dy), ImVec2(ctr.x + dx, ctr.y - dy),
                                      ImVec2(ctr.x + dx, ctr.y + dy), ImVec2(ctr.x - dx, ctr.y + dy)};
                    const char* rel[4] = {"horizontal", "vertical", "horizontal", "vertical"};
                    // Include the center point and construction diagonal with the four edges.
                    addQuadLoop(st, quad, rel, nullptr, &ctr, toolSnaps_[0]);
                    resetTool();
                }
            } else if (tool_ == SketchTool::Point && toolUV_.size() == 1) {
                nlohmann::json ent = {{"type", "point"},
                    {"p", nlohmann::json::array({toolUV_[0].x, toolUV_[0].y})}};
                // C49: a snapped Point pick gets the matching relation (its handle is "$self.p").
                nlohmann::json cs = nlohmann::json::array();
                pushSnapConstraint(toolSnaps_[0], "$self.p", cs);
                addEntity(st, ent, cs);
                toolUV_.clear(); toolSnaps_.clear();   // stay armed (SW point tool repeats)
            } else if (tool_ == SketchTool::Ellipse && toolUV_.size() == 3) {
                // center, major-axis end (rx + rot), minor point (ry = perp distance).
                ImVec2 ctr = toolUV_[0], rxp = toolUV_[1], ryp = toolUV_[2];
                float rot = std::atan2(rxp.y - ctr.y, rxp.x - ctr.x);
                float rx = std::hypot(rxp.x - ctr.x, rxp.y - ctr.y);
                float ca = std::cos(rot), sa = std::sin(rot);
                float ry = std::fabs(-(ryp.x - ctr.x) * sa + (ryp.y - ctr.y) * ca);
                if (rx < 1e-4f || ry < 1e-4f) {
                    st.addLog("[sketch] Ellipse: degenerate radii \xE2\x80\x94 pick the minor point off the major axis");
                    toolUV_.pop_back(); toolSnaps_.pop_back();
                } else {
                    const float kR2D = 57.29577951f;
                    nlohmann::json ent = {{"type", "ellipse"},
                        {"center", nlohmann::json::array({ctr.x, ctr.y})},
                        {"rx", rx}, {"ry", ry}, {"rot", rot * kR2D}};   // rot in degrees (schema)
                    // Persist the center pick's snap (coincident / alignment) onto $self.center.
                    nlohmann::json cs = nlohmann::json::array();
                    pushSnapConstraint(toolSnaps_[0], "$self.center", cs);
                    addEntity(st, ent, cs);
                    resetTool();
                }
            }
        }

        // C42: Polygon side count while the tool is armed.
        //   • number keys 3-9 (top row or keypad) quick-set 3..9 sides
        //   • +/= or ]  add a side, -/_ or [  remove one (clamped 3..64) -> octagons, decagons,
        //     dodecagons and beyond, not just the default hexagon.
        if (tool_ == SketchTool::Polygon && !io.WantTextInput) {
            for (int d = 3; d <= 9; ++d) {
                if (ImGui::IsKeyPressed((ImGuiKey)(ImGuiKey_0 + d)) ||
                    ImGui::IsKeyPressed((ImGuiKey)(ImGuiKey_Keypad0 + d)))
                    polySides_ = d;
            }
            const bool inc = ImGui::IsKeyPressed(ImGuiKey_Equal)        // '=' (shift gives '+')
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadAdd)
                          || ImGui::IsKeyPressed(ImGuiKey_RightBracket);
            const bool dec = ImGui::IsKeyPressed(ImGuiKey_Minus)
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadSubtract)
                          || ImGui::IsKeyPressed(ImGuiKey_LeftBracket);
            if (inc && polySides_ < 64) ++polySides_;
            if (dec && polySides_ > 3)  --polySides_;
        }
        // Star tip count: +/-/[/] adjust polyStarPoints_ (clamped 3..12).
        if (tool_ == SketchTool::Star && !io.WantTextInput) {
            const bool inc = ImGui::IsKeyPressed(ImGuiKey_Equal)
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadAdd)
                          || ImGui::IsKeyPressed(ImGuiKey_RightBracket);
            const bool dec = ImGui::IsKeyPressed(ImGuiKey_Minus)
                          || ImGui::IsKeyPressed(ImGuiKey_KeypadSubtract)
                          || ImGui::IsKeyPressed(ImGuiKey_LeftBracket);
            if (inc && polyStarPoints_ < 12) ++polyStarPoints_;
            if (dec && polyStarPoints_ > 3)  --polyStarPoints_;
        }

        // C42: Spline finishing — Enter / right-click / double-click commits (>= 2 points); a
        // right-click with fewer just drops the in-progress points (mirrors the Line-chain RMB).
        // Style Spline shares the exact same collect/finish UX (>= 2 poles) and only differs in the
        // emitted entity type ("style_spline" — control poles, not interpolated through-points).
        if (tool_ == SketchTool::Spline || tool_ == SketchTool::StyleSpline) {
            bool finish = false;
            if (imgHovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
                toolUV_.size() >= 3) {
                // The double-click's 2nd click pushed a duplicate point this frame — drop it.
                toolUV_.pop_back(); toolSnaps_.pop_back();
                finish = true;
            }
            if (!io.WantTextInput && toolUV_.size() >= 2 &&
                (ImGui::IsKeyPressed(ImGuiKey_Enter) || ImGui::IsKeyPressed(ImGuiKey_KeypadEnter)))
                finish = true;
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
                if (toolUV_.size() >= 2) finish = true;
                else { toolUV_.clear(); toolSnaps_.clear(); }
            }
            if (finish && toolUV_.size() >= 2) {
                nlohmann::json pts = nlohmann::json::array();
                for (const auto& q : toolUV_) pts.push_back(nlohmann::json::array({q.x, q.y}));
                const char* entType = (tool_ == SketchTool::StyleSpline) ? "style_spline" : "spline";
                nlohmann::json ent = {{"type", entType}, {"points", pts}};
                // Persist each through-point/pole pick's snap onto $self.p<i> (endpoints landing on
                // a vertex are the usual case; alignment guides on any point carry too).
                nlohmann::json cs = nlohmann::json::array();
                for (size_t i = 0; i < toolSnaps_.size() && i < toolUV_.size(); ++i)
                    pushSnapConstraint(toolSnaps_[i], "$self.p" + std::to_string(i), cs);
                addEntity(st, ent, cs);
                resetTool();
            }
        }
        // C37-D: right-click ends the current chain/segment but keeps the tool armed (SW muscle
        // memory: the Line tool drops its in-progress points and waits for a fresh first point).
        // mouse_gestures is disabled in-sketch (viewport_panel), so the RMB is free here; consume it.
        // C42: Spline / Style Spline own their RMB (finish/drop, handled above).
        if (tool_ != SketchTool::Spline && tool_ != SketchTool::StyleSpline && imgHovered &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            toolUV_.clear(); toolSnaps_.clear(); chainPrevId_.clear();
        }
        // ESC ends the tool.
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) cancelTool();
    }

    // ---- C37-D: SolidWorks-grade Smart Dimension state machine ----
    if (tool_ == SketchTool::SmartDim) {
        st.requestCursor(ui::CursorKind::Crosshair);
        const float sdS = ui::uiScale;
        const ImU32 hiCol = canvasAccent();          // dark-on-canvas hover/pick emphasis
        const ImU32 pickCol = canvasAccent(0.95f);
        // Highlight already-picked targets. C38 (S1): resolve the picked entity by id (ents_ may have
        // been refilled since the pick) — skip the highlight when it's gone.
        for (const auto& p : dimPicks_) {
            if (p.isPoint) { dl->AddCircle(uvToScreen(p.u, p.v), 6.0f * sdS, pickCol, 0, 2.0f * sdS); continue; }
            const SketchEntity* ep = entById(p.entId);
            if (ep) {
                const SketchEntity& e = *ep;
                if (e.type == "line")
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), pickCol,
                                3.0f * sdS);
                else if (e.type == "circle" || e.type == "arc")
                    dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                  std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                             uvToScreen(e.center[0], e.center[1]).x, 0.0f), pickCol,
                                  0, 3.0f * sdS);
            }
        }
        // A pick can still be chained while fewer than 2 targets are collected — even when a
        // single-pick combo (diameter / line length) is already previewing: clicking ANOTHER
        // target replaces it with the two-pick combo (SolidWorks behavior; this also makes the
        // line-line angle and all circle-distance combos actually reachable).
        const bool canAddPick = !dimPopupOpen_ && !overDefOpen_ && dimPicks_.size() < 2;
        DimPick hoverPick; bool hoverHit = false;
        if (canAddPick && imgHovered) hoverHit = dimHitTest(io.MousePos, hoverPick);
        if (hoverHit) {
            // A midpoint pick (handle ".mid") draws as a small filled diamond — the SolidWorks
            // midpoint glyph — to distinguish it from an endpoint circle.
            bool isMid = hoverPick.isPoint && hoverPick.handle.size() > 4 &&
                         hoverPick.handle.compare(hoverPick.handle.size() - 4, 4, ".mid") == 0;
            if (isMid) {
                ImVec2 m = uvToScreen(hoverPick.u, hoverPick.v); float r = 5.0f * sdS;
                dl->AddQuadFilled(ImVec2(m.x, m.y - r), ImVec2(m.x + r, m.y),
                                  ImVec2(m.x, m.y + r), ImVec2(m.x - r, m.y), hiCol);
            } else if (hoverPick.isPoint) {
                dl->AddCircle(uvToScreen(hoverPick.u, hoverPick.v), 6.0f * sdS, hiCol, 0, 2.0f * sdS);
            } else if (hoverPick.entIdx >= 0) {
                const SketchEntity& e = ents_[hoverPick.entIdx];
                if (e.type == "line") {
                    dl->AddLine(uvToScreen(e.p1[0], e.p1[1]), uvToScreen(e.p2[0], e.p2[1]), hiCol,
                                2.5f * sdS);
                    // SolidWorks shows the midpoint marker whenever a line is hovered, so the user
                    // knows they can snap to it — a hollow diamond at the center.
                    ImVec2 m = uvToScreen((e.p1[0]+e.p2[0])*0.5f, (e.p1[1]+e.p2[1])*0.5f);
                    float r = 5.0f * sdS;
                    dl->AddQuad(ImVec2(m.x, m.y - r), ImVec2(m.x + r, m.y),
                                ImVec2(m.x, m.y + r), ImVec2(m.x - r, m.y), hiCol, 1.6f * sdS);
                } else if (e.type == "circle" || e.type == "arc")
                    dl->AddCircle(uvToScreen(e.center[0], e.center[1]),
                                  std::hypot(uvToScreen(e.center[0] + e.radius, e.center[1]).x -
                                             uvToScreen(e.center[0], e.center[1]).x, 0.0f), hiCol,
                                  0, 2.5f * sdS);
            }
        }

        // External (model) geometry: when nothing sketch-side is under the cursor, probe the
        // id-buffer for a model edge/face. st.hover drives the standard 3D hover highlight
        // (rendered next frame); a click projects the reference into the sketch as a FIXED
        // construction entity (worker sketch_project_ref) and chains it as a pick.
        std::string datumAxis;
        if (canAddPick && imgHovered && !hoverHit) {
            std::string axisName;
            ImVec2 a, b;
            if (hitTestDatumAxis(st, io.MousePos, imgMin, vw, vh, a, b, datumAxis, axisName) &&
                (datumAxis == "axis.x" || datumAxis == "axis.y" || datumAxis == "axis.z")) {
                dl->AddLine(a, b, hiCol, 2.5f * sdS);
            } else datumAxis.clear();
        }
        render::PickHit extHit{};
        if (canAddPick && imgHovered && !hoverHit && datumAxis.empty() && st.hasDocMesh && st.idPass) {
            int px = (int)(io.MousePos.x - imgMin.x), py = (int)(io.MousePos.y - imgMin.y);
            render::PickHit h = st.idPass->pickFinalPreferEdge(px, py, vw, vh,
                (int)std::lround(7.0f * ui::uiScale));
            if (h.valid() && modelPickKind(h.kind))
                extHit = h;
        }
        st.hover = extHit;   // empty hit = no highlight; cleared when SmartDim disarms (below)

        // Phase A: collecting picks (no full combo yet). A click adds the hovered target.
        if (!dimPopupOpen_ && !overDefOpen_) {
            // Rebuild the pending dim from the current picks + live cursor (drives the preview AND
            // the live h/v/aligned variant choice for point-point).
            dimBuildPending(io.MousePos, mu, mv);

            bool clicked = imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left);
            bool pickAdded = false;
            if (clicked && canAddPick && dimHoverCid_.empty()) {
                // C47: dimHoverCid_ guard — a click on an existing dim's text belongs to the
                // drag / double-click-edit handler above, not a new pick.
                if (hoverHit) {
                    // Avoid duplicate picks of the same target. C38 (S1): compare entity picks by
                    // id (stable) rather than ents_ index (per-frame, may have shifted).
                    bool dup = false;
                    for (const auto& q : dimPicks_)
                        if (q.isPoint == hoverPick.isPoint &&
                            (hoverPick.isPoint ? q.handle == hoverPick.handle
                                               : q.entId == hoverPick.entId)) dup = true;
                    if (!dup) {
                        // Shift+click a circle/arc = tangent (near-rim "min") anchor; plain click
                        // anchors at the center. Either way the Modify box can switch it after.
                        if (!hoverPick.isPoint) {
                            const SketchEntity* ce = entById(hoverPick.entId);
                            if (ce && (ce->type == "circle" || ce->type == "arc"))
                                hoverPick.cond = io.KeyShift ? "min" : "center";
                        }
                        dimPicks_.push_back(hoverPick);
                        pickAdded = true;
                        dimBuildPending(io.MousePos, mu, mv);   // refresh the combo immediately
                    }
                } else if (!datumAxis.empty()) {
                    if (!addProjectedDimRef(st, {{"datum", datumAxis}}, "datum:" + datumAxis).empty()) {
                        pickAdded = true;
                        dimBuildPending(io.MousePos, mu, mv);
                    }
                } else if (extHit.valid()) {
                    std::string perr;
                    const auto ref = modelPickReference(st, extHit, perr);
                    if (ref.is_object()) {
                        if (!addProjectedDimRef(st, ref, ref.dump()).empty()) {
                            pickAdded = true;
                            dimBuildPending(io.MousePos, mu, mv);
                        }
                    } else {
                        st.toast(ui::ToastLevel::Warn, "Can't reference that geometry" +
                                 (perr.empty() ? std::string() : ": " + perr));
                    }
                }
            }
            if (dimHasPending_) {
                // LIVE PREVIEW: a full annotation following the cursor until the placement click.
                SketchConstraint pc;
                pc.type = dimType_; pc.axis = dimAxis_;
                pc.hasMeasured = true; pc.measured = dimMeasured_;
                if (dimPending_.contains("refs"))
                    for (const auto& rr : dimPending_["refs"]) pc.refs.push_back(rr.get<std::string>());
                if (dimPending_.contains("cond"))
                    for (const auto& q : dimPending_["cond"]) pc.cond.push_back(q.get<std::string>());
                drawDimension(dl, pc, hiCol, hiCol, true, mu, mv);
                // Placement click (a click that didn't chain a pick) stores the placement and
                // opens the Modify box.
                if (clicked && !pickAdded) {
                    dimPlaceU_ = mu; dimPlaceV_ = mv;
                    dimOpenModify(io.MousePos);
                }
            }
        }

        // Esc semantics: with the Modify box open, item-4 handler closes it (below). With picks in
        // the buffer, first Esc clears the buffer; second Esc exits the tool.
        if (!dimPopupOpen_ && ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            escHandled = true;   // C41 (T1): pick-buffer clear keeps the tool armed
            if (!dimPicks_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
            else cancelTool();
        }
        // Right-click also clears the pick buffer (consume it; gestures off in-sketch).
        if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
            dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr;
        }
    } else if (st.hover.valid()) {
        // Smart Dimension was the only in-sketch writer of st.hover (model-ref picking); clear a
        // stale highlight once the tool disarms (the viewport's own picking is off in-sketch).
        st.hover = render::PickHit{};
    }

    // ---- Wave 4C/4D: paste-placement preview (drawn LAST, on top of the existing geometry) ----
    // imgHovered was forced false above so no tool/selection interaction consumed the placement
    // click. The follow-cursor outline preview is shifted by (cursor - copyOrigin); a left-click over
    // the canvas commits the paste at that offset, Esc cancels. We re-derive canvas-hover from the
    // image rect (the suppressed imgHovered can't be reused for the commit click).
    if (pastePlacing_) {
        st.requestCursor(ui::CursorKind::Crosshair);
        drawPastePreview(st, dl, io.MousePos);
        glassHudBanner(dl, "sk_paste_banner", skClipMin, skClipMax, skHudRow2Y,
                       "Click to place \xE2\x80\x94 Esc to cancel");
        bool overCanvas = ImGui::IsMouseHoveringRect(imgMin, imgMax);
        // (The ✓/✗ confirmation corner moved to the Sketch-tab ribbon; no in-viewport carve-out.)
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            pastePlacing_ = false; pasteBundle_ = nlohmann::json();
            st.toast(ui::ToastLevel::Info, "Paste cancelled");
        } else if (overCanvas && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            float cu = 0, cv = 0; screenToUV(io.MousePos, cu, cv);
            commitPasteEntities(st, cu - pasteAnchorUV_.x, cv - pasteAnchorUV_.y);
        }
    }

    // ---- per-tool cursor badge ----
    // The armed tool's ribbon icon rides below-right of the OS crosshair (the SolidWorks
    // pencil-glyph affordance) so the active tool is always visible at the cursor. SDF draw
    // with a light canvas-toned halo keeps it AA, uiScale-crisp and readable over geometry.
    // Drawn last (still clipped to the image) so no sketch ink paints over it.
    if (tool_ != SketchTool::None && imgHovered && !pastePlacing_) {
        const char* cmd = activeToolCmd();
        if (cmd && *cmd) {
            int cmdCount = 0;
            const RibbonCmd* cmds = getRibbonCmds(cmdCount);
            const char* icon = nullptr;
            for (int i = 0; i < cmdCount; ++i)
                if (std::strcmp(cmds[i].cmd_id, cmd) == 0) { icon = cmds[i].icon; break; }
            if (icon) {
                const float s = ui::uiScale;
                ui::drawIconGlass(dl, icon, ImVec2(io.MousePos.x + 16.0f * s,
                                                   io.MousePos.y + 18.0f * s),
                                  15.0f * s, col(pal.sketchInk), col(pal.viewportTop, 0.85f));
            }
        }
    }

    dl->PopClipRect();

    // C37-D: the ✓ keep / ✗ cancel confirmation corner that used to float in the viewport's
    // top-right was relocated to the Sketch-tab ribbon as a right-aligned Finish/Cancel commit
    // cluster (ribbon.cpp, shown only while s_sketchActive; fires sketch_exit / sketch_cancel).

    // ---- tool hint chip (top-center) ----
    if (tool_ != SketchTool::None) {
        const char* tn = tool_ == SketchTool::Line ? "Line" :
                         tool_ == SketchTool::RectCorner ? "Rectangle" :
                         tool_ == SketchTool::Circle ? "Circle" :
                         tool_ == SketchTool::ArcCenter ? "Center Arc" :
                         tool_ == SketchTool::Slot ? "Slot" :
                         tool_ == SketchTool::SmartDim ? "Smart Dimension" :
                         tool_ == SketchTool::Rect3ptCorner ? "3 Point Corner Rectangle" :
                         tool_ == SketchTool::Rect3ptCenter ? "3 Point Center Rectangle" :
                         tool_ == SketchTool::Parallelogram ? "Parallelogram" :
                         tool_ == SketchTool::CirclePerimeter ? "Perimeter Circle" :
                         tool_ == SketchTool::SlotCtrStr ? "Centerpoint Slot" :
                         tool_ == SketchTool::SlotArcCtr ? "Centerpoint Arc Slot" :
                         tool_ == SketchTool::SlotArc3pt ? "3 Point Arc Slot" :
                         tool_ == SketchTool::EllipsePartial ? "Partial Ellipse" :
                         tool_ == SketchTool::Parabola ? "Parabola" :
                         tool_ == SketchTool::Conic ? "Conic" :
                         tool_ == SketchTool::Trim ? "Trim" :
                         tool_ == SketchTool::Extend ? "Extend" :
                         tool_ == SketchTool::Offset ? "Offset" :
                         tool_ == SketchTool::SkFillet ? "Sketch Fillet" :
                         tool_ == SketchTool::SkChamfer ? "Sketch Chamfer" :
                         tool_ == SketchTool::Mirror ? "Mirror Entities" :
                         tool_ == SketchTool::LinPattern ? "Linear Pattern" :
                         tool_ == SketchTool::CircPattern ? "Circular Pattern" :
                         tool_ == SketchTool::Convert ? "Convert Entities" :
                         tool_ == SketchTool::Move ? "Move Entities" :
                         tool_ == SketchTool::MoveEntities ? "Move Entities" :
                         tool_ == SketchTool::RotateEntities ? "Rotate Entities" :
                         tool_ == SketchTool::ArcTangent ? "Tangent Arc" :
                         tool_ == SketchTool::Arc3pt ? "3 Point Arc" :
                         tool_ == SketchTool::Polygon ? "Polygon" :
                         tool_ == SketchTool::RectCenter ? "Center Rectangle" :
                         tool_ == SketchTool::Point ? "Point" :
                         tool_ == SketchTool::Ellipse ? "Ellipse" :
                         tool_ == SketchTool::Spline ? "Spline" :
                         tool_ == SketchTool::StyleSpline ? "Style Spline" :
                         tool_ == SketchTool::Text ? "Text" :
                         tool_ == SketchTool::EqCurve ? "Equation Driven Curve" :
                         tool_ == SketchTool::Star ? "Star" : "";
        // Per-stage step prompt (status-bar style) for the multi-click variants.
        const size_t n = toolUV_.size();
        const char* step = nullptr;
        switch (tool_) {
            case SketchTool::Line:   // C37-D: SW muscle-memory hint
                step = n == 0 ? "click start point" : "click next point \xC2\xB7 right-click to end chain"; break;
            case SketchTool::RectCorner:
                step = n == 0 ? "click first corner" : "click opposite corner"; break;
            case SketchTool::Circle:
                step = n == 0 ? "click center" : "click radius"; break;
            case SketchTool::ArcCenter:
                step = n == 0 ? "click center" : n == 1 ? "click start" : "click end"; break;
            case SketchTool::Slot:
                step = n == 0 ? "click start" : n == 1 ? "click end" : "click width"; break;
            case SketchTool::SmartDim:
                step = dimPicks_.empty()
                     ? "click an entity, point, model edge/face, or a tree plane (Shift+circle = tangent)"
                     : dimPicks_.size() < 2
                     ? "click another entity/point/model ref, or click empty space to place"
                     : "click to place the dimension"; break;
            case SketchTool::Rect3ptCorner:
                step = n == 0 ? "click corner" : n == 1 ? "click edge end" : "click width"; break;
            case SketchTool::Rect3ptCenter:
                step = n == 0 ? "click center" : n == 1 ? "click edge midpoint" : "click width"; break;
            case SketchTool::Parallelogram:
                step = n == 0 ? "click corner 1" : n == 1 ? "click corner 2" : "click corner 3"; break;
            case SketchTool::CirclePerimeter:
                step = n < 3 ? "click 3 points on the circumference" : nullptr; break;
            case SketchTool::SlotCtrStr:
                step = n == 0 ? "click center" : n == 1 ? "click end (length/dir)" : "click width"; break;
            case SketchTool::SlotArcCtr:
                step = n == 0 ? "click arc center" : n == 1 ? "click spine start" :
                       n == 2 ? "click spine end" : "click width"; break;
            case SketchTool::SlotArc3pt:
                step = n < 3 ? "click 3 points on the arc spine" : "click width"; break;
            case SketchTool::EllipsePartial:
                step = n == 0 ? "click center" : n == 1 ? "click major (rx/rot)" :
                       n == 2 ? "click minor (ry)" : n == 3 ? "click start angle" : "click end angle"; break;
            case SketchTool::Parabola:
                step = n == 0 ? "click focus" : n == 1 ? "click vertex" :
                       n == 2 ? "click start" : "click end"; break;
            case SketchTool::Conic:
                step = n == 0 ? "click endpoint 1" : n == 1 ? "click endpoint 2" :
                       n == 2 ? "click apex" : "click to set rho (fullness)"; break;
            case SketchTool::Trim:
                step = "hover a segment (red), click to trim"; break;
            case SketchTool::Extend:
                step = "hover near an end, click to extend"; break;
            case SketchTool::Offset:
                step = offsetStage_ == 0 ? "click entities for the chain, then Enter"
                     : offsetStage_ == 1 ? "type the offset distance, Enter"
                     : "click the side to offset toward"; break;
            case SketchTool::SkFillet: {
                static char fb[64];
                std::snprintf(fb, sizeof(fb), corner1Id_.empty()
                                  ? "pick two lines (radius %.3g mm)"
                                  : "pick the second line (radius %.3g mm)", skFilletRadius_);
                step = fb; break;
            }
            case SketchTool::SkChamfer: {
                static char cb[64];
                std::snprintf(cb, sizeof(cb), corner1Id_.empty()
                                  ? "pick two lines (distance %.3g mm)"
                                  : "pick the second line (distance %.3g mm)", skChamferDist_);
                step = cb; break;
            }
            case SketchTool::Mirror:
                step = mpStage_ == 0 ? "click entities to mirror, then Enter"
                                     : "click the mirror line (construction line)"; break;
            case SketchTool::LinPattern:
                step = mpStage_ == 0 ? "click entities to pattern, then Enter"
                                     : "set count / spacing / direction, then Apply"; break;
            case SketchTool::CircPattern:
                step = mpStage_ == 0 ? "click entities to pattern, then Enter"
                     : mpStage_ == 1 ? "click the pattern center point"
                                     : "set count / angle, then Apply"; break;
            case SketchTool::Convert:
                step = "click a model edge or face to project into the sketch"; break;
            case SketchTool::Move:
                step = mpStage_ == 0 ? "click entities to move, then Enter"
                     : mpStage_ == 1 ? "click the from point (anchor)"
                                     : "click the to point \xC2\xB7 hold Ctrl to copy"; break;
            case SketchTool::MoveEntities:
                step = mpStage_ == 0 ? "click entities to move, then Enter"
                                     : "drag the arrows (or center) \xC2\xB7 \xE2\x9C\x93 / Enter to finish \xC2\xB7 Ctrl copies"; break;
            case SketchTool::RotateEntities:
                step = mpStage_ == 0 ? "click entities to rotate, then Enter"
                                     : "drag the ring or type an angle \xC2\xB7 \xE2\x9C\x93 / Enter to finish"; break;
            case SketchTool::ArcTangent:
                step = "click the arc end (it starts tangent to the prior/selected entity)"; break;
            case SketchTool::Arc3pt:
                step = n == 0 ? "click arc start" : n == 1 ? "click arc end"
                     : "click a point on the arc"; break;
            case SketchTool::Polygon:
                step = n == 0 ? "click center" : "click a vertex"; break;
            case SketchTool::RectCenter:
                step = n == 0 ? "click center" : "click a corner"; break;
            case SketchTool::Point:
                step = "click to place a point"; break;
            case SketchTool::Ellipse:
                step = n == 0 ? "click center" : n == 1 ? "click major-axis end" : "click minor (ry)"; break;
            case SketchTool::Spline:
                step = n < 2 ? "click points; Enter / right-click / double-click to finish"
                             : "click more points \xC2\xB7 Enter / right-click / double-click finishes"; break;
            case SketchTool::StyleSpline:
                step = n < 2 ? "click control poles; Enter / right-click / double-click to finish"
                             : "click more poles \xC2\xB7 Enter / right-click / double-click finishes"; break;
            case SketchTool::Text:
                step = textPopupOpen_ ? "type the text + height, then OK"
                                      : "click to place the text"; break;
            case SketchTool::EqCurve:
                step = "enter x(t) / y(t) and the t range, then OK"; break;
            case SketchTool::Airfoil:
                step = "set the NACA code / .dat + chord, then OK"; break;
            case SketchTool::Star:
                step = n == 0 ? "click center" : "click a tip vertex"; break;
            default: break;
        }
        std::string hint = std::string(tn) + (step ? std::string(" \xE2\x80\x94 ") + step : std::string());
        if (tool_ == SketchTool::Polygon) {   // C42: live side count + how to change it
            char sb[80];
            std::snprintf(sb, sizeof(sb), "  [Sides: %d \xC2\xB7 keys 3-9 or +/- to adjust]", polySides_);
            hint += sb;
        }
        if (tool_ == SketchTool::Star) {
            char sb[80];
            std::snprintf(sb, sizeof(sb), "  [Tips: %d \xC2\xB7 +/- to adjust, ratio %.2f]",
                          polyStarPoints_, polyStarRatio_);
            hint += sb;
        }
        if (tool_ == SketchTool::Trim)   // C29-B: keep-as-construction toggle (key 'C')
            hint += trimKeepConstruction_ ? "  [Keep as construction: ON \xC2\xB7 C toggles]"
                                          : "  [Keep as construction: off \xC2\xB7 C toggles]";
        hint += "  (ESC to end)";
        // Stacks one row BELOW the top-center status/DOF island; centered on the clear rect so it
        // never slides under the pinned columns or the doc-tab strip.
        glassHudBanner(ImGui::GetWindowDrawList(), "sk_tool_hint", skClipMin, skClipMax,
                       skHudRow2Y, hint.c_str());
    }

    // ---- status/DOF chip (top-CENTER "dynamic island") ----
    // Glass v2 pill: the frost + glassText body track the live backdrop; the solver state stays
    // color-coded via the semantic ring (the raw status colors as TEXT washed out over the light
    // frost in the dark theme — same failure mode as the toast level tags). Floats top-center like
    // an iPhone dynamic island; the active-tool hint (below) stacks under it at y+52.
    {
        const float s = ui::uiScale;
        std::string sl = statusLine();
        // C41 (T4): neutral pill while the sketch is empty (statusLine shows the draw hint).
        ImU32 sc = ents_.empty()  ? 0
                 : status_ == "over" ? col(pal.statusErr)
                 : status_ == "full" ? col(pal.statusOk) : col(pal.sketchUnder);
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        const float statusY = skHudTopY;   // first HUD row inside the clear rect (below the tabs)
        glassHudBanner(wdl, "sk_status_chip", skClipMin, skClipMax, statusY, sl.c_str(), sc);
        float pillBot = statusY + ImGui::CalcTextSize(sl.c_str()).y + 10.0f * s;

        // C50f: conflict-freeze warning bar. When the last solve came back solve_rejected:true the
        // engine froze geometry at the last valid coordinates (no least-squares mangling); tell the
        // user the sketch couldn't be solved and point at the red (conflicting) relations to edit.
        // Sits just below the status pill, error-ringed. Cleared automatically when a later solve
        // succeeds (applyEnterResult sets solveRejected_ = false).
        if (solveRejected_) {
            const char* warn = "Sketch could not be solved \xE2\x80\x94 geometry frozen at last valid "
                               "state. Delete or edit a conflicting (red) relation.";
            glassHudBanner(wdl, "sk_solve_warn", skClipMin, skClipMax, pillBot + 8.0f * s, warn,
                           col(pal.statusErr));
        }
    }

    // ---- Smart Dimension Modify box (C50 redesign) ----
    // SolidWorks-style compact panel: titled header with the dim type + live readout, a prominent
    // value field flanked by steppers with the unit suffix, Driven + Arc-condition controls, an
    // inline error line, and a primary OK / Cancel footer. Auto-focused + text pre-selected; Enter
    // commits, Esc closes without committing (pick buffer cleared) but stays in the tool. Expression
    // input ("=...") and full unit math ("100mm+2in/0.5m") keep working.
    if (dimPopupOpen_) {
        const float sc = ui::uiScale;
        ImGui::SetNextWindowPos(dimPopupPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.99f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 10 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(7 * sc, 7 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f);
        if (ImGui::Begin("##SketchDim", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            const bool isAngle = dimIsAngle_;
            const char* suffix = isAngle ? "\xC2\xB0" : skLenUnit();
            // Friendly title + symbol per dim type.
            const char* title = dimType_ == "diameter" ? "\xE2\x8C\x80 Diameter"
                              : dimType_ == "radius"   ? "R Radius"
                              : dimType_ == "angle"    ? "\xE2\x88\xA0 Angle"
                              : dimType_ == "arc_length" ? "Arc length"
                              : "Distance";
            // ---- header row: title (accent) + live measured readout on the right ----
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
            ImGui::TextUnformatted(dimEditCid_.empty() ? title : (std::string("Edit ") + title).c_str());
            ImGui::PopStyleColor();
            {
                std::string meas = isAngle ? fmtAng(dimMeasured_)
                                           : (fmtLen(dimMeasured_) + " " + skLenUnit());
                std::string lab = "now " + meas;
                float w = ImGui::CalcTextSize(lab.c_str()).x;
                ImGui::SameLine(0, 0);
                ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                    std::max(14.0f * sc, ImGui::GetContentRegionAvail().x - w));
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
                ImGui::TextUnformatted(lab.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::Separator();

            // ---- value row: [-] [ value ] unit [+] ----
            auto stepVal = [&](double delta) {
                std::string txt = dimBuf_.data();
                if (!txt.empty() && txt[0] == '=') return;   // don't mutate expressions
                // Steps are one DISPLAY unit: in IPS a length step is 1 inch (= 25.4 mm canonical).
                const double bareScale = (g_skIps && !isAngle) ? 25.4 : 1.0;
                double cur = dimMeasured_; std::string perr;
                if (pmParseDim(txt, cur, perr, isAngle, bareScale)) {}  // parse current text if valid
                cur += delta * ((isAngle) ? 1.0 : bareScale);
                std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(cur).c_str());
            };
            const float btn = ImGui::GetFrameHeight();
            if (ImGui::Button("-", ImVec2(btn, btn))) stepVal(-dimPopupStep_);
            ImGui::SameLine();
            ImGui::SetNextItemWidth(150 * sc);
            if (dimPopupFocus_) ImGui::SetKeyboardFocusHere();
            bool enter = ImGui::InputText("##dimval", dimBuf_.data(), dimBuf_.size(),
                                          ImGuiInputTextFlags_EnterReturnsTrue |
                                          (dimPopupFocus_ ? ImGuiInputTextFlags_AutoSelectAll : 0));
            dimPopupFocus_ = false;
            ImGui::SameLine(0, 4 * sc);
            ImGui::TextUnformatted(suffix);
            ImGui::SameLine();
            if (ImGui::Button("+", ImVec2(btn, btn))) stepVal(dimPopupStep_);

            // ---- options row: Driven + Arc condition ----
            ImGui::Checkbox("Driven (reference)", &dimPopupDriven_);
            if (dimEditCid_.empty() && dimType_ == "distance") {
                static const char* kConds[3] = {"Center", "Min (near rim)", "Max (far rim)"};
                bool condChanged = false;
                int shown = 0;
                for (auto& p : dimPicks_) {
                    if (p.isPoint) continue;
                    const SketchEntity* ce = entById(p.entId);
                    if (!ce || (ce->type != "circle" && ce->type != "arc")) continue;
                    int cur = p.cond == "min" ? 1 : p.cond == "max" ? 2 : 0;
                    ImGui::SetNextItemWidth(150 * sc);
                    std::string lbl = std::string(shown == 0 ? "Arc condition" : "Arc condition 2")
                                      + "##cond" + p.entId;
                    if (ImGui::Combo(lbl.c_str(), &cur, kConds, 3)) {
                        p.cond = cur == 1 ? "min" : cur == 2 ? "max" : "center";
                        condChanged = true;
                    }
                    ++shown;
                }
                if (condChanged) {
                    // Re-derive refs/cond AND the measured value from the updated picks, keeping the
                    // already-chosen axis. This routes through the same tangent-line math as the
                    // live preview, so min/max on overlapping or concentric circles reads correctly
                    // (the old incremental "+= adjNew - adjOld" assumed the additive centre-line
                    // model and was wrong for the directional h/v case).
                    dimBuildPending(dimPopupPos_, dimPlaceU_, dimPlaceV_, /*keepAxis=*/true);
                    std::snprintf(dimBuf_.data(), dimBuf_.size(), "%s", fmtLen(dimMeasured_).c_str());
                }
            }
            if (!dimError_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1));
                ImGui::Text("! %s", dimError_.c_str());
                ImGui::PopStyleColor();
            }

            // ---- footer: primary OK (accent) + Cancel, right-aligned ----
            ImGui::Separator();
            const float okW = 64 * sc, cancelW = 64 * sc;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                std::max(0.0f, ImGui::GetContentRegionAvail().x - okW - cancelW - 7 * sc));
            bool ok = ui::kit::primaryButton("OK", true, nullptr, ImVec2(okW, 0)) || enter;
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel", ImVec2(cancelW, 0));
            if (ok) dimCommit(st);
            if (cancel || (ImGui::IsKeyPressed(ImGuiKey_Escape) && !ImGui::IsKeyDown(ImGuiKey_LeftCtrl))) {
                escHandled = true;   // C41 (T1): box close keeps Smart Dimension armed
                dimPopupOpen_ = false; dimError_.clear();
                if (dimEditCid_.empty()) { dimPicks_.clear(); dimHasPending_ = false; dimPending_ = nullptr; }
                dimEditCid_.clear();
            }
        }
        ImGui::End();
        ImGui::PopStyleVar(3);
    }

    // C50f: SolidWorks over-define pre-flight dialogs (add path: "Make Dimension Driven?";
    // edit path: "This value over-defines the sketch"). Both are opened by sketch_probe before
    // anything is committed.
    drawOverDefinePrompt(st);
    drawEditOverDefinePrompt(st);
    drawRelationOverDefinePrompt(st);

    // ---- Offset distance overlay (small InputFloat near the cursor; Enter -> side-pick stage) ----
    if (tool_ == SketchTool::Offset && offsetStage_ == 1) {
        ImGui::SetNextWindowPos(offsetOverlayPos_, ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin("##SketchOffsetDist", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted("Offset distance (mm)");
            ImGui::SetNextItemWidth(120 * ui::uiScale);
            if (offsetFocusInput_) { ImGui::SetKeyboardFocusHere(); offsetFocusInput_ = false; }
            // Bounded value -> drag entry (Ctrl+click types; Enter still advances the stage).
            ImGui::DragFloat("##offdist", &offsetDist_, 0.25f, 0.001f, 100000.0f, "%.3f",
                             ImGuiSliderFlags_AlwaysClamp);
            bool enter = ImGui::IsItemDeactivatedAfterEdit() &&
                         (ImGui::IsKeyPressed(ImGuiKey_Enter, false) ||
                          ImGui::IsKeyPressed(ImGuiKey_KeypadEnter, false));
            bool ok = ImGui::Button("Next") || enter;
            ImGui::SameLine();
            bool cancel = ImGui::Button("Cancel");
            if (ok) { offsetDist_ = std::fabs(offsetDist_); offsetStage_ = 2; }
            if (cancel) { offsetStage_ = 0; }
        }
        ImGui::End();
    }

    // ---- C52: Sketch Fillet / Chamfer numeric input (always shown while the tool is armed) ----
    // A small always-on overlay near the status chip holds the radius (fillet) / distance (chamfer);
    // the value persists across corners and invocations. Editing it never disturbs the pick state —
    // the two lines are still picked by clicking in the viewport. Pinned top-left so it doesn't fight
    // the cursor while the user clicks corners (unlike Offset's one-shot box near the cursor).
    if (tool_ == SketchTool::SkFillet || tool_ == SketchTool::SkChamfer) {
        const bool fillet = (tool_ == SketchTool::SkFillet);
        ImGui::SetNextWindowPos(ImVec2(imgMin.x + 12, imgMin.y + 52), ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin(fillet ? "##SketchFilletParam" : "##SketchChamferParam", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextUnformatted(fillet ? "Fillet radius (mm)" : "Chamfer distance (mm)");
            ImGui::SetNextItemWidth(120 * ui::uiScale);
            float* val = fillet ? &skFilletRadius_ : &skChamferDist_;
            // Bounded value -> drag entry; the clamp keeps the engine's strictly-positive rule.
            ImGui::DragFloat(fillet ? "##skfilletr" : "##skchamferd", val, 0.25f, 0.001f,
                             100000.0f, "%.3f", ImGuiSliderFlags_AlwaysClamp);
        }
        ImGui::End();
    }

    // ---- C53: Linear / Circular Pattern numeric panel (count + spacing/angle, then Apply) ----
    // Shown once the entity selection is locked (Enter): Linear at stage 1, Circular at stage 2
    // (after the center pick). The dims persist across invocations. Apply builds the op; Cancel
    // backs out to re-pick. The viewport selection is frozen while the panel is up.
    bool linPanel  = (tool_ == SketchTool::LinPattern  && mpStage_ == 1);
    bool circPanel = (tool_ == SketchTool::CircPattern && mpStage_ == 2);
    if (linPanel || circPanel) {
        ImGui::SetNextWindowPos(ImVec2(imgMin.x + 12, imgMin.y + 52), ImGuiCond_Appearing);
        ImGui::SetNextWindowBgAlpha(0.98f);
        if (ImGui::Begin(linPanel ? "##SketchLinPattern" : "##SketchCircPattern", nullptr,
                         ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::Text(linPanel ? "Linear Pattern (%d entities)" : "Circular Pattern (%d entities)",
                        (int)mpSel_.size());
            ImGui::Separator();
            // Bounded values -> drag entry (Ctrl+click types an exact number).
            ImGui::SetNextItemWidth(120 * ui::uiScale);
            ImGui::DragInt("Count", &patternCount_, 0.1f, 2, 999, "%d",
                           ImGuiSliderFlags_AlwaysClamp);   // engine requires count >= 2
            if (linPanel) {
                ImGui::SetNextItemWidth(120 * ui::uiScale);
                ImGui::DragFloat("Spacing (mm)", &patternSpacing_, 0.5f, 0.001f, 100000.0f,
                                 "%.3f", ImGuiSliderFlags_AlwaysClamp);
                ImGui::SetNextItemWidth(180 * ui::uiScale);
                ImGui::InputFloat2("Direction (u,v)", &patternDirU_, "%.2f");
            } else {
                ImGui::SetNextItemWidth(120 * ui::uiScale);
                ImGui::DragFloat(patternEqualSpacing_ ? "Total angle (deg)" : "Step angle (deg)",
                                 &patternAngle_, 0.5f, 0.01f, 360.0f, "%.2f",
                                 ImGuiSliderFlags_AlwaysClamp);
                ImGui::Checkbox("Equal spacing (full span)", &patternEqualSpacing_);
            }
            ImGui::Separator();
            if (ImGui::Button("Apply")) applyPatternOp(st, circPanel);
            ImGui::SameLine();
            if (ImGui::Button("Cancel")) { mpStage_ = circPanel ? 1 : 0; }
        }
        ImGui::End();
    }

    // ---- Sketch Text + Equation-Driven Curve parameter popups (engine entities text / eq_curve) ----
    drawTextPopup(st, escHandled);
    drawEqCurvePopup(st, escHandled);
    drawAirfoilPopup(st, escHandled);

    drawAddRelations(st, escHandled);

    // ---- C41 (T1): catch-all Esc — disarm the armed tool / in-progress chain even when the
    // viewport image is NOT hovered. The more specific handlers above run first: the ones that
    // fully cancel leave tool_ == None (no-op here); the ones that keep the tool armed (Modify
    // box close, Smart Dim pick clear, Offset stage back-out) mark escHandled. Suppressed while
    // any text field owns the keyboard. Esc with no tool armed does nothing here.
    if (!escHandled && tool_ != SketchTool::None && !io.WantTextInput &&
        ImGui::IsKeyPressed(ImGuiKey_Escape))
        cancelTool();
}

}  // namespace ui
