// AI-CAD View Selector — SolidWorks-style "glass box" (C25, wave W2-E).
#include "view_selector.h"

#include <algorithm>
#include <cmath>
#include <vector>

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"

#include "camera_anim.h"   // ui::requestCameraAnimation / ui::cameraAnimating (camera agent owns it)

namespace ui {

namespace {

// ---- box geometry constants (pre-scale; geometry is in world units, scaled to the model) ----
constexpr float kBoxScale  = 1.35f;   // box half-extent = bounding radius * this
constexpr float kChamfer   = 0.22f;   // corner truncation as a fraction of the half-extent

// A standard view: caption + the yaw/pitch the camera animates to (degrees).
struct ViewAngle { const char* name; float yaw, pitch; };

// 6 cube faces, indexed by which axis/sign points OUT of the face. The view shown when that
// face is dead-on toward the camera. (Front = -Y faces the camera at yaw0/pitch0 — see the
// orientation-triad convention in viewport_panel.cpp.)
//   +X -> Right, -X -> Left, +Y -> Back, -Y -> Front, +Z -> Top, -Z -> Bottom
const ViewAngle kFaceView[6] = {
    {"Right",  90.0f,  0.0f},   // 0: +X
    {"Left",  -90.0f,  0.0f},   // 1: -X
    {"Back",  180.0f,  0.0f},   // 2: +Y
    {"Front",   0.0f,  0.0f},   // 3: -Y
    {"Top",     0.0f, 89.5f},   // 4: +Z
    {"Bottom",  0.0f,-89.5f},   // 5: -Z
};
// Outward normals matching kFaceView order.
const Vec3 kFaceNormal[6] = {
    {1,0,0}, {-1,0,0}, {0,1,0}, {0,-1,0}, {0,0,1}, {0,0,-1},
};

// 8 corner views (iso family): yaw in {45,135,-135,-45}, pitch +/-35.264 (the cube-diagonal tilt).
// Indexed by corner sign (sx,sy,sz) each +/-1. The yaw chosen so the camera looks down that
// body diagonal toward the origin.
constexpr float kIsoPitch = 35.264f;
ViewAngle cornerView(int sx, int sy, int sz) {
    // Camera sits at (sx,sy,sz)*h looking at the origin. Yaw measured like the triad (about Z),
    // pitch positive when the corner has +Z.
    float yaw;
    // map (sx,sy) quadrant to a 45-deg-family yaw. -Y is "front" (yaw 0), +X is "right" (yaw 90).
    if (sx > 0 && sy < 0)      yaw =  45.0f;   // front-right
    else if (sx > 0 && sy > 0) yaw = 135.0f;   // back-right
    else if (sx < 0 && sy > 0) yaw = -135.0f;  // back-left
    else                       yaw = -45.0f;   // front-left
    float pitch = (sz > 0) ? kIsoPitch : -kIsoPitch;
    return {"Iso", yaw, pitch};
}

// The 7 quick-orientation buttons in the top-center popup.
const ViewAngle kPopup[7] = {
    {"Front",   0.0f,  0.0f}, {"Back",  180.0f,  0.0f},
    {"Left",  -90.0f,  0.0f}, {"Right",  90.0f,  0.0f},
    {"Top",     0.0f, 89.5f}, {"Bottom", 0.0f, -89.5f},
    {"Iso",    35.0f, 30.0f},
};

// Layout of the top-center orientation popup. Deterministic from the viewport rect + uiScale, so
// update() (dismiss-click guard) and draw() (button placement) agree without sharing state.
struct PopupLayout {
    float x0, y0;           // bar top-left (screen)
    float w, h;             // bar size
    float btnX[7], btnW[7]; // per-button left + width
    float btnY, btnH;       // shared button top + height
};
PopupLayout popupLayout(ImVec2 vr0, ImVec2 vr1) {
    const float s = ui::uiScale;
    const float btnPadX = 9.0f * s, btnH = 22.0f * s, gap = 3.0f * s;
    const float padX = 8.0f * s, padY = 4.0f * s;
    PopupLayout L; L.btnH = btnH;
    float barW = padX * 2;
    for (int i = 0; i < 7; ++i) {
        L.btnW[i] = ImGui::CalcTextSize(kPopup[i].name).x + btnPadX * 2;
        barW += L.btnW[i] + (i ? gap : 0);
    }
    L.w = barW; L.h = btnH + padY * 2;
    L.x0 = (vr0.x + vr1.x) * 0.5f - barW * 0.5f;
    L.y0 = vr0.y + 48.0f * s;   // sits just below the C24 heads-up toolbar
    L.btnY = L.y0 + padY;
    float x = L.x0 + padX;
    for (int i = 0; i < 7; ++i) { L.btnX[i] = x; x += L.btnW[i] + gap; }
    return L;
}

// ---- per-frame projected geometry ----
// A clickable zone: a screen-space polygon, its caption, target view, and facing flag.
struct Zone {
    std::vector<ImVec2> poly;   // projected outline (CW or CCW; point-in-poly is orientation-free)
    ViewAngle view;
    bool      front = false;    // faces the camera (hoverable/clickable) vs. back-facing (dim only)
    bool      isFace = false;   // face (octagon) vs. corner triangle — controls fill weight
};

// Standard even-odd point-in-polygon test in screen space.
bool pointInPoly(const std::vector<ImVec2>& p, ImVec2 m) {
    bool in = false;
    for (size_t i = 0, j = p.size() - 1; i < p.size(); j = i++) {
        if (((p[i].y > m.y) != (p[j].y > m.y)) &&
            (m.x < (p[j].x - p[i].x) * (m.y - p[i].y) / (p[j].y - p[i].y) + p[i].x))
            in = !in;
    }
    return in;
}

}  // namespace

// ---- shared geometry build -----------------------------------------------------
// Projects the truncated box for the current camera and fills `zones`. Returns false if the
// model/camera isn't ready. Geometry is rebuilt every frame so it tracks the live orbit (the
// camera MVP already folds in yaw/pitch/zoom/pan — same template as the plane-name chips).
namespace {

bool buildZones(Renderer& renderer, ImVec2 vr0, ImVec2 vr1,
                std::vector<Zone>& zones) {
    if (!renderer.hasMesh()) return false;
    const Renderer::CameraXform& cam = renderer.camera();
    const float h = cam.radius * kBoxScale;
    if (h <= 1e-5f) return false;
    const float c = h * kChamfer;
    const Vec3  C = cam.center;
    const Vec3  eye = cam.eye;
    const float* m = cam.mvp.m;   // column-major model->clip

    // Project a world point to screen; returns false if behind the camera.
    auto project = [&](const Vec3& w, ImVec2& out) -> bool {
        float x  = m[0]*w.x + m[4]*w.y + m[8]*w.z  + m[12];
        float y  = m[1]*w.x + m[5]*w.y + m[9]*w.z  + m[13];
        float wc = m[3]*w.x + m[7]*w.y + m[11]*w.z + m[15];
        if (wc <= 1e-4f) return false;
        out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
        out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
        return true;
    };

    // The 8 cube corners (signs), each truncated into 3 vertices pulled toward neighbours by c.
    // World vertex for cube corner (sx,sy,sz) shifted toward the neighbour along `axis` (0/1/2).
    auto cornerVert = [&](int sx, int sy, int sz, int axis) -> Vec3 {
        Vec3 v = C + Vec3((float)sx * h, (float)sy * h, (float)sz * h);
        // move inward along the chamfered edge: reduce the coordinate of `axis` toward 0 by c.
        if (axis == 0) v.x -= (float)sx * c;
        if (axis == 1) v.y -= (float)sy * c;
        if (axis == 2) v.z -= (float)sz * c;
        return v;
    };

    auto facesCamera = [&](const Vec3& faceCenter, const Vec3& normal) -> bool {
        return dot(normal, eye - faceCenter) > 0.0f;
    };

    // ---- 6 octagonal faces ----
    // For each axis-aligned face, walk its 4 cube corners; at each corner take the 2 chamfer
    // vertices that lie ON this face (the two edges of this face meeting at that corner). That
    // yields 8 ordered vertices (an octagon).
    for (int f = 0; f < 6; ++f) {
        const Vec3& n = kFaceNormal[f];
        int fixed = (n.x != 0) ? 0 : (n.y != 0) ? 1 : 2;     // axis held at +/-h
        int sFixed = (n.x + n.y + n.z) > 0 ? 1 : -1;
        int a = (fixed + 1) % 3, b = (fixed + 2) % 3;        // the two in-plane axes
        Vec3 faceCenter = C + n * h;
        bool front = facesCamera(faceCenter, n);

        // 4 in-plane corner sign pairs, ordered around the face.
        const int quad[4][2] = {{+1,+1},{-1,+1},{-1,-1},{+1,-1}};
        Zone z; z.isFace = true; z.front = front; z.view = kFaceView[f];
        bool ok = true;
        for (int q = 0; q < 4 && ok; ++q) {
            int sgn[3];
            sgn[fixed] = sFixed; sgn[a] = quad[q][0]; sgn[b] = quad[q][1];
            // two chamfer verts on this face at this corner: shifted along a, then along b.
            Vec3 va = cornerVert(sgn[0], sgn[1], sgn[2], a);
            Vec3 vb = cornerVert(sgn[0], sgn[1], sgn[2], b);
            ImVec2 pa, pb;
            if (!project(va, pa) || !project(vb, pb)) { ok = false; break; }
            z.poly.push_back(pa);
            z.poly.push_back(pb);
        }
        if (!ok) continue;
        // Order the 8 projected vertices CCW around their centroid so the polygon is convex for
        // both AddConvexPolyFilled and the point-in-poly test (emit order isn't guaranteed convex).
        ImVec2 ctr(0, 0);
        for (const ImVec2& p : z.poly) { ctr.x += p.x; ctr.y += p.y; }
        ctr.x /= (float)z.poly.size(); ctr.y /= (float)z.poly.size();
        std::sort(z.poly.begin(), z.poly.end(), [&](const ImVec2& A, const ImVec2& B) {
            return std::atan2(A.y - ctr.y, A.x - ctr.x) < std::atan2(B.y - ctr.y, B.x - ctr.x);
        });
        zones.push_back(std::move(z));
    }

    // ---- 8 corner triangles ----
    for (int sx = -1; sx <= 1; sx += 2)
    for (int sy = -1; sy <= 1; sy += 2)
    for (int sz = -1; sz <= 1; sz += 2) {
        Vec3 v0 = cornerVert(sx, sy, sz, 0);
        Vec3 v1 = cornerVert(sx, sy, sz, 1);
        Vec3 v2 = cornerVert(sx, sy, sz, 2);
        Vec3 triCenter = (v0 + v1 + v2) * (1.0f / 3.0f);
        Vec3 cornerDir = normalize(Vec3((float)sx, (float)sy, (float)sz));
        bool front = facesCamera(triCenter, cornerDir);
        ImVec2 p0, p1, p2;
        if (!project(v0, p0) || !project(v1, p1) || !project(v2, p2)) continue;
        Zone z; z.isFace = false; z.front = front; z.view = cornerView(sx, sy, sz);
        z.poly = {p0, p1, p2};
        zones.push_back(std::move(z));
    }
    return true;
}

}  // namespace

// ---- update --------------------------------------------------------------------
void ViewSelector::update(AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                          ImVec2 vr0, ImVec2 vr1) {
    ImGuiIO& io = ImGui::GetIO();

    if (!m_active) {
        // Activate on Space while the viewport is hovered, not typing, not sketching.
        if (imgHovered && !inSketch && !io.WantTextInput &&
            ImGui::IsKeyPressed(ImGuiKey_Space) && !cameraAnimating()) {
            m_active = true;
            m_hoverZone = -1;
        }
        return;
    }

    // Active: Space or Esc dismiss.
    if (ImGui::IsKeyPressed(ImGuiKey_Space) || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
        close();
        return;
    }

    // Rebuild zones for this frame and resolve the hovered (front-facing) zone.
    std::vector<Zone> zones;
    if (!buildZones(renderer, vr0, vr1, zones)) { close(); return; }

    m_hoverZone = -1;
    if (imgHovered) {
        // Prefer corner triangles over faces when both contain the cursor (corners sit on faces).
        for (int pass = 0; pass < 2 && m_hoverZone < 0; ++pass) {
            bool wantFace = (pass == 1);
            for (int i = 0; i < (int)zones.size(); ++i) {
                if (!zones[i].front || zones[i].isFace != wantFace) continue;
                if (pointInPoly(zones[i].poly, io.MousePos)) { m_hoverZone = i; break; }
            }
        }
    }

    // Click: fire the hovered zone's view, else close (click-on-empty dismisses). Clicks inside the
    // top-center orientation popup are left for draw()'s InvisibleButtons to handle (don't dismiss).
    if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        PopupLayout L = popupLayout(vr0, vr1);
        bool inPopup = io.MousePos.x >= L.x0 && io.MousePos.x <= L.x0 + L.w &&
                       io.MousePos.y >= L.y0 && io.MousePos.y <= L.y0 + L.h;
        if (!inPopup) {
            if (m_hoverZone >= 0) {
                const ViewAngle& v = zones[m_hoverZone].view;
                requestCameraAnimation(st, v.yaw, v.pitch);
            }
            close();
            return;
        }
    }
    // (draw() rebuilds zones and re-resolves the hover from the same projection — cheap, and keeps
    // the drawn fill/outline pixel-consistent with the click test above without caching geometry.)
}

// ---- draw ----------------------------------------------------------------------
void ViewSelector::draw(AppState& st, ImDrawList* dl, ImVec2 vr0, ImVec2 vr1) {
    if (!m_active) return;
    Renderer& renderer = *st.renderer;

    std::vector<Zone> zones;
    if (!buildZones(renderer, vr0, vr1, zones)) return;

    const auto& pal = ui::palette;
    const float s = ui::uiScale;

    // Color roles (never hardcoded). Faces: subtle light translucent fill + dark edge.
    float faceFillF[4];  ui::Palette::withAlpha(pal.viewportTop, faceFillF, 0.18f);
    const ImU32 faceFill   = ui::Palette::toU32(faceFillF);
    float edgeF[4];      ui::Palette::withAlpha(pal.viewportEdge, edgeF, 0.55f);
    const ImU32 edgeCol    = ui::Palette::toU32(edgeF);
    float edgeDimF[4];   ui::Palette::withAlpha(pal.viewportEdge, edgeDimF, 0.18f);
    const ImU32 edgeDim    = ui::Palette::toU32(edgeDimF);
    const ImU32 hoverFill  = ui::Palette::toU32(pal.accentSoft);
    const ImU32 hoverLine  = ui::Palette::toU32(pal.accent);
    const ImU32 overlayBg   = ui::Palette::toU32(pal.overlayBg);
    const ImU32 overlayBord = ui::Palette::toU32(pal.overlayBorder);
    const ImU32 overlayTxt  = ui::Palette::toU32(pal.overlayText);

    // Re-resolve the hovered zone here so fill/outline match the exact projected polys we draw.
    ImGuiIO& io = ImGui::GetIO();
    int hover = -1;
    for (int pass = 0; pass < 2 && hover < 0; ++pass) {
        bool wantFace = (pass == 1);
        for (int i = 0; i < (int)zones.size(); ++i) {
            if (!zones[i].front || zones[i].isFace != wantFace) continue;
            if (pointInPoly(zones[i].poly, io.MousePos)) { hover = i; break; }
        }
    }

    // Pass 1: back-facing zones — dim outline only (so the far edges of the glass box read through).
    for (const auto& z : zones) {
        if (z.front || z.poly.size() < 3) continue;
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), edgeDim, ImDrawFlags_Closed, 1.0f * s);
    }
    // Pass 2: front-facing faces — translucent fill + dark outline.
    for (const auto& z : zones) {
        if (!z.front || !z.isFace || z.poly.size() < 3) continue;
        dl->AddConvexPolyFilled(z.poly.data(), (int)z.poly.size(), faceFill);
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), edgeCol, ImDrawFlags_Closed, 1.2f * s);
    }
    // Pass 3: front-facing corner triangles — outline only (filled on hover below).
    for (const auto& z : zones) {
        if (!z.front || z.isFace || z.poly.size() < 3) continue;
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), edgeCol, ImDrawFlags_Closed, 1.0f * s);
    }
    // Pass 4: hovered zone — accent fill + thick accent outline + caption near the cursor.
    if (hover >= 0) {
        const Zone& z = zones[hover];
        dl->AddConvexPolyFilled(z.poly.data(), (int)z.poly.size(), hoverFill);
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), hoverLine, ImDrawFlags_Closed, 2.0f * s);
        const char* cap = z.view.name;
        ImVec2 ts = ImGui::CalcTextSize(cap);
        ImVec2 cp(io.MousePos.x + 14.0f * s, io.MousePos.y + 14.0f * s);
        if (cp.x + ts.x + 12 > vr1.x) cp.x = vr1.x - ts.x - 12;
        if (cp.y + ts.y + 8  > vr1.y) cp.y = vr1.y - ts.y - 8;
        dl->AddRectFilled(cp, ImVec2(cp.x + ts.x + 12, cp.y + ts.y + 8), overlayBg, 4.0f);
        dl->AddRect(cp, ImVec2(cp.x + ts.x + 12, cp.y + ts.y + 8), overlayBord, 4.0f);
        dl->AddText(ImVec2(cp.x + 6, cp.y + 4), overlayTxt, cap);
    }

    // ---- caption pill, bottom-center ----
    {
        const char* hint = "Click a face or corner — Esc to close";
        ImVec2 ts = ImGui::CalcTextSize(hint);
        float w = ts.x + 22.0f * s, hh = ts.y + 12.0f * s;
        ImVec2 p0((vr0.x + vr1.x) * 0.5f - w * 0.5f, vr1.y - hh - 16.0f * s);
        ImVec2 p1(p0.x + w, p0.y + hh);
        dl->AddRectFilled(p0, p1, overlayBg, hh * 0.5f);
        dl->AddRect(p0, p1, overlayBord, hh * 0.5f);
        dl->AddText(ImVec2(p0.x + 11.0f * s, p0.y + 6.0f * s), overlayTxt, hint);
    }

    // ---- orientation popup, top-center (below the heads-up toolbar) ----
    {
        const int n = 7;
        PopupLayout L = popupLayout(vr0, vr1);
        dl->AddRectFilled(ImVec2(L.x0, L.y0), ImVec2(L.x0 + L.w, L.y0 + L.h),
                          overlayBg, L.h * 0.30f);
        dl->AddRect(ImVec2(L.x0, L.y0), ImVec2(L.x0 + L.w, L.y0 + L.h),
                    overlayBord, L.h * 0.30f);

        ImGui::PushID("##viewsel_popup");
        for (int i = 0; i < n; ++i) {
            float bxi = L.btnX[i], byi = L.btnY;
            ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
            ImGui::PushID(i);
            bool clicked = ImGui::InvisibleButton("##vb", ImVec2(L.btnW[i], L.btnH));
            bool hov = ImGui::IsItemHovered();
            if (hov)
                dl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + L.btnW[i], byi + L.btnH),
                                  ui::Palette::toU32(pal.buttonHovered), 3.0f);
            ImVec2 lts = ImGui::CalcTextSize(kPopup[i].name);
            dl->AddText(ImVec2(bxi + (L.btnW[i] - lts.x) * 0.5f, byi + (L.btnH - lts.y) * 0.5f),
                        hov ? hoverLine : overlayTxt, kPopup[i].name);
            if (clicked) {
                requestCameraAnimation(st, kPopup[i].yaw, kPopup[i].pitch);
                close();
            }
            ImGui::PopID();
        }
        ImGui::PopID();
    }
}

}  // namespace ui
