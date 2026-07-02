#include "view_selector.h"

#include <algorithm>
#include <cmath>
#include <vector>

#include "renderer.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "app/app_state.h"
#include "camera_anim.h"
#include "ui/viewport.h"

namespace ui {

namespace {

constexpr float kBoxScale  = 1.35f;
constexpr float kChamfer   = 0.275f;
constexpr float kSelectorFitZoom = 1.0f / (1.95f * kBoxScale);

struct ViewAngle { const char* name; float yaw, pitch; };

const ViewAngle kFaceView[6] = {
    {"Right",  90.0f,  0.0f},
    {"Left",  -90.0f,  0.0f},
    {"Back",  180.0f,  0.0f},
    {"Front",   0.0f,  0.0f},
    {"Top",     0.0f, 89.5f},
    {"Bottom",  0.0f,-89.5f},
};
const Vec3 kFaceNormal[6] = {
    {1,0,0}, {-1,0,0}, {0,1,0}, {0,-1,0}, {0,0,1}, {0,0,-1},
};

constexpr float kIsoPitch = 35.264f;
ViewAngle cornerView(int sx, int sy, int sz) {
    float yaw;
    if (sx > 0 && sy < 0)      yaw =  45.0f;
    else if (sx > 0 && sy > 0) yaw = 135.0f;
    else if (sx < 0 && sy > 0) yaw = -135.0f;
    else                       yaw = -45.0f;
    float pitch = (sz > 0) ? kIsoPitch : -kIsoPitch;
    return {"Iso", yaw, pitch};
}

const ViewAngle kPopup[7] = {
    {"Front",   0.0f,  0.0f}, {"Back",  180.0f,  0.0f},
    {"Left",  -90.0f,  0.0f}, {"Right",  90.0f,  0.0f},
    {"Top",     0.0f, 89.5f}, {"Bottom", 0.0f, -89.5f},
    {"Iso",    35.0f, 30.0f},
};

struct PopupLayout {
    float x0, y0, w, h;
    float btnX[7], btnW[7];
    float btnY, btnH;
};

PopupLayout popupLayout(ImVec2 vr0, ImVec2 vr1) {
    const float s = theme::S();
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
    L.y0 = vr0.y + 48.0f * s;
    L.btnY = L.y0 + padY;
    float x = L.x0 + padX;
    for (int i = 0; i < 7; ++i) { L.btnX[i] = x; x += L.btnW[i] + gap; }
    return L;
}

struct Zone {
    std::vector<ImVec2> poly;
    ViewAngle view;
    bool front = false;
    bool isFace = false;
};

bool pointInPoly(const std::vector<ImVec2>& p, ImVec2 m) {
    bool in = false;
    for (size_t i = 0, j = p.size() - 1; i < p.size(); j = i++) {
        if (((p[i].y > m.y) != (p[j].y > m.y)) &&
            (m.x < (p[j].x - p[i].x) * (m.y - p[i].y) / (p[j].y - p[i].y) + p[i].x))
            in = !in;
    }
    return in;
}

bool buildZones(Renderer& renderer, ImVec2 vr0, ImVec2 vr1, std::vector<Zone>& zones) {
    const Renderer::CameraXform& cam = renderer.camera();
    const float h = cam.radius * kBoxScale;
    if (h <= 1e-5f) return false;
    const float c = h * kChamfer;
    const Vec3  C = renderer.modelCenter();
    const Vec3  eye = cam.eye;
    const float* m = cam.mvp.m;

    auto project = [&](const Vec3& w, ImVec2& out) -> bool {
        float x  = m[0]*w.x + m[4]*w.y + m[8]*w.z  + m[12];
        float y  = m[1]*w.x + m[5]*w.y + m[9]*w.z  + m[13];
        float wc = m[3]*w.x + m[7]*w.y + m[11]*w.z + m[15];
        if (wc <= 1e-4f) return false;
        out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
        out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
        return true;
    };

    auto cornerVert = [&](int sx, int sy, int sz, int axis) -> Vec3 {
        Vec3 v = C + Vec3((float)sx * h, (float)sy * h, (float)sz * h);
        if (axis == 0) v.x -= (float)sx * c;
        if (axis == 1) v.y -= (float)sy * c;
        if (axis == 2) v.z -= (float)sz * c;
        return v;
    };

    auto facesCamera = [&](const Vec3& faceCenter, const Vec3& normal) -> bool {
        return dot(normal, eye - faceCenter) > 0.0f;
    };

    for (int f = 0; f < 6; ++f) {
        const Vec3& n = kFaceNormal[f];
        int fixed = (n.x != 0) ? 0 : (n.y != 0) ? 1 : 2;
        int sFixed = (n.x + n.y + n.z) > 0 ? 1 : -1;
        int a = (fixed + 1) % 3, b = (fixed + 2) % 3;
        Vec3 faceCenter = C + n * h;
        bool front = facesCamera(faceCenter, n);
        const int quad[4][2] = {{+1,+1},{-1,+1},{-1,-1},{+1,-1}};
        Zone z; z.isFace = true; z.front = front; z.view = kFaceView[f];
        bool ok = true;
        for (int q = 0; q < 4 && ok; ++q) {
            int sgn[3];
            sgn[fixed] = sFixed; sgn[a] = quad[q][0]; sgn[b] = quad[q][1];
            Vec3 va = cornerVert(sgn[0], sgn[1], sgn[2], a);
            Vec3 vb = cornerVert(sgn[0], sgn[1], sgn[2], b);
            ImVec2 pa, pb;
            if (!project(va, pa) || !project(vb, pb)) { ok = false; break; }
            z.poly.push_back(pa);
            z.poly.push_back(pb);
        }
        if (!ok) continue;
        ImVec2 ctr(0, 0);
        for (const ImVec2& p : z.poly) { ctr.x += p.x; ctr.y += p.y; }
        ctr.x /= (float)z.poly.size(); ctr.y /= (float)z.poly.size();
        std::sort(z.poly.begin(), z.poly.end(), [&](const ImVec2& A, const ImVec2& B) {
            return std::atan2(A.y - ctr.y, A.x - ctr.x) < std::atan2(B.y - ctr.y, B.x - ctr.x);
        });
        zones.push_back(std::move(z));
    }

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

void ViewSelector::openFramed(app::AppState& st) {
    open();
    ViewportState& vs = viewportState();
    const float zeroPan[3] = {0.0f, 0.0f, 0.0f};
    requestCameraAnimation(st, vs.yaw, vs.pitch, kSelectorFitZoom, zeroPan);
}

void ViewSelector::update(app::AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                          ImVec2 vr0, ImVec2 vr1) {
    ImGuiIO& io = ImGui::GetIO();

    if (!m_active) {
        (void)inSketch;
        if (imgHovered && !io.WantTextInput &&
            ImGui::IsKeyPressed(ImGuiKey_Space) && !cameraAnimating())
            openFramed(st);
        return;
    }

    if (ImGui::IsKeyPressed(ImGuiKey_Space) || ImGui::IsKeyPressed(ImGuiKey_Escape)) {
        close();
        return;
    }

    std::vector<Zone> zones;
    if (!buildZones(renderer, vr0, vr1, zones)) { close(); return; }

    m_hoverZone = -1;
    if (imgHovered) {
        for (int pass = 0; pass < 2 && m_hoverZone < 0; ++pass) {
            bool wantFace = (pass == 1);
            for (int i = 0; i < (int)zones.size(); ++i) {
                if (!zones[i].front || zones[i].isFace != wantFace) continue;
                if (pointInPoly(zones[i].poly, io.MousePos)) { m_hoverZone = i; break; }
            }
        }
    }

    if (m_hoverZone >= 0) {
        float y = zones[m_hoverZone].view.yaw, p = zones[m_hoverZone].view.pitch;
        if (y == m_hoverYaw && p == m_hoverPitch)
            m_hoverDwell += io.DeltaTime;
        else {
            m_hoverDwell = 0.0f;
            m_previewReady = false;
        }
        m_hoverYaw = y; m_hoverPitch = p;
        if (m_hoverDwell >= 0.12f) m_previewReady = true;
    } else {
        m_hoverDwell = 0.0f;
        m_previewReady = false;
    }

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
        }
    }
}

void ViewSelector::draw(app::AppState& st, Renderer& renderer, ImDrawList* dl, ImVec2 vr0, ImVec2 vr1) {
    (void)st;
    if (!m_active) return;

    std::vector<Zone> zones;
    if (!buildZones(renderer, vr0, vr1, zones)) return;

    const float s = theme::S();
    const ImU32 faceFill   = theme::roleU32A(theme::pal().viewport0, 0.18f);
    const ImU32 edgeCol    = theme::roleU32A(theme::pal().line, 0.55f);
    const ImU32 edgeDim    = theme::roleU32A(theme::pal().line, 0.18f);
    const ImU32 hoverFill  = TCOL(accentSoft);
    const ImU32 hoverLine  = TCOL(accent);

    ImGuiIO& io = ImGui::GetIO();
    int hover = -1;
    for (int pass = 0; pass < 2 && hover < 0; ++pass) {
        bool wantFace = (pass == 1);
        for (int i = 0; i < (int)zones.size(); ++i) {
            if (!zones[i].front || zones[i].isFace != wantFace) continue;
            if (pointInPoly(zones[i].poly, io.MousePos)) { hover = i; break; }
        }
    }

    for (const auto& z : zones) {
        if (z.front || z.poly.size() < 3) continue;
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), edgeDim, ImDrawFlags_Closed, 1.0f * s);
    }
    for (const auto& z : zones) {
        if (!z.front || !z.isFace || z.poly.size() < 3) continue;
        dl->AddConvexPolyFilled(z.poly.data(), (int)z.poly.size(), faceFill);
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), edgeCol, ImDrawFlags_Closed, 1.2f * s);
    }
    for (const auto& z : zones) {
        if (!z.front || z.isFace || z.poly.size() < 3) continue;
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), edgeCol, ImDrawFlags_Closed, 1.0f * s);
    }
    if (hover >= 0) {
        const Zone& z = zones[hover];
        dl->AddConvexPolyFilled(z.poly.data(), (int)z.poly.size(), hoverFill);
        dl->AddPolyline(z.poly.data(), (int)z.poly.size(), hoverLine, ImDrawFlags_Closed, 2.0f * s);
        const char* cap = z.view.name;
        ImVec2 ts = ImGui::CalcTextSize(cap);
        ImVec2 cp(io.MousePos.x + 14.0f * s, io.MousePos.y + 14.0f * s);
        if (cp.x + ts.x + 12 * s > vr1.x) cp.x = vr1.x - ts.x - 12 * s;
        if (cp.y + ts.y + 8 * s  > vr1.y) cp.y = vr1.y - ts.y - 8 * s;
        ImRect capRect(cp, ImVec2(cp.x + ts.x + 12 * s, cp.y + ts.y + 8 * s));
        const theme::AdaptiveSurface& capSurf =
            theme::adaptivePanel(theme::panelId("viewsel_caption"), capRect);
        theme::glassPanel(dl, capRect, theme::MaterialLevel::UltraThin, 4.0f, capSurf);
        theme::glassText(dl, nullptr, ImGui::GetFontSize(), ImVec2(cp.x + 6 * s, cp.y + 4 * s),
                         capSurf, cap);
    }

    if (m_previewReady && renderer.hasMesh()) {
        const float thumb = 168.0f * s;
        const int   thumbPx = (int)(thumb + 0.5f);
        unsigned int tex = renderer.renderThumbnail(m_hoverYaw, m_hoverPitch, thumbPx);
        if (tex) {
            const char* cap = "Click a face or corner to switch views";
            const float pad = 8.0f * s;
            const float panelW = thumb + pad * 2.0f;
            ImVec2 capSz = ImGui::CalcTextSize(cap, nullptr, false, panelW - pad * 2.0f);
            const float panelH = thumb + pad * 3.0f + capSz.y;
            float px0 = vr1.x - panelW - 14.0f * s;
            float py0 = vr0.y + 14.0f * s;
            if (px0 < vr0.x + 6.0f * s) px0 = vr0.x + 6.0f * s;
            ImVec2 p0(px0, py0), p1(px0 + panelW, py0 + panelH);
            ImRect prevRect(p0, p1);
            const theme::AdaptiveSurface& prevSurf =
                theme::adaptivePanel(theme::panelId("viewsel_preview"), prevRect);
            theme::glassPanel(dl, prevRect, theme::MaterialLevel::Thin, 6.0f, prevSurf);
            ImVec2 i0(p0.x + pad, p0.y + pad);
            ImVec2 i1(i0.x + thumb, i0.y + thumb);
            dl->AddImage((ImTextureID)(intptr_t)tex, i0, i1, ImVec2(0, 1), ImVec2(1, 0));
            dl->AddRect(i0, i1, TCOL(glassBorder), 3.0f * s);
            theme::glassText(dl, nullptr, ImGui::GetFontSize(), ImVec2(i0.x, i1.y + pad),
                             prevSurf, cap);
        }
    }

    {
        const char* hint = "Click a face or corner — Esc to close";
        ImVec2 ts = ImGui::CalcTextSize(hint);
        float w = ts.x + 22.0f * s, hh = ts.y + 12.0f * s;
        ImVec2 p0((vr0.x + vr1.x) * 0.5f - w * 0.5f, vr1.y - hh - 16.0f * s);
        ImVec2 p1(p0.x + w, p0.y + hh);
        ImRect hintRect(p0, p1);
        const theme::AdaptiveSurface& hintSurf =
            theme::adaptivePanel(theme::panelId("viewsel_hint"), hintRect);
        theme::glassPanel(dl, hintRect, theme::MaterialLevel::UltraThin, hh * 0.5f, hintSurf);
        theme::glassText(dl, nullptr, ImGui::GetFontSize(),
                         ImVec2(p0.x + 11.0f * s, p0.y + 6.0f * s), hintSurf, hint);
    }

    {
        PopupLayout L = popupLayout(vr0, vr1);
        ImRect popRect(ImVec2(L.x0, L.y0), ImVec2(L.x0 + L.w, L.y0 + L.h));
        const theme::AdaptiveSurface& popSurf =
            theme::adaptivePanel(theme::panelId("viewsel_popup"), popRect);
        theme::glassPanel(dl, popRect, theme::MaterialLevel::Regular, L.h * 0.30f, popSurf);
        ImGui::PushID("##viewsel_popup");
        for (int i = 0; i < 7; ++i) {
            float bxi = L.btnX[i], byi = L.btnY;
            ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
            ImGui::PushID(i);
            bool clicked = ImGui::InvisibleButton("##vb", ImVec2(L.btnW[i], L.btnH));
            bool hov = ImGui::IsItemHovered();
            if (hov)
                dl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + L.btnW[i], byi + L.btnH),
                                  TCOL(hover), 3.0f);
            ImVec2 lts = ImGui::CalcTextSize(kPopup[i].name);
            ImVec2 lp(bxi + (L.btnW[i] - lts.x) * 0.5f, byi + (L.btnH - lts.y) * 0.5f);
            if (hov)
                dl->AddText(lp, hoverLine, kPopup[i].name);
            else
                theme::glassText(dl, nullptr, ImGui::GetFontSize(), lp, popSurf, kPopup[i].name);
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
