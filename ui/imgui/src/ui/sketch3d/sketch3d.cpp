// 3D free-space sketcher — FIRST SLICE. See sketch3d.h for the design intent.
#include "ui/sketch3d/sketch3d.h"

#include <cmath>

#include "app/app_state.h"
#include "imgui.h"
#include "vec_math.h"                     // Vec3, dot()
#include "ui/layout.h"                    // overlayColumnEdge() — clear the left glass column
#include "ui/sketch/sketch_internal.h"    // worldToScreen(), screenRay(), col(), sketchPalette()

namespace ui {

Sketch3DModel& sketch3d() {
    static Sketch3DModel s_model;
    return s_model;
}

namespace {

// Unit normal of the active work plane (through the origin).
Vec3 planeNormal(int plane) {
    switch (plane) {
        case 1:  return {1, 0, 0};   // YZ
        case 2:  return {0, 1, 0};   // XZ
        default: return {0, 0, 1};   // XY
    }
}

// Intersect a screen-cast ray with the active work plane through the origin.
// Returns false for a near-parallel ray (no stable hit).
bool rayHitPlane(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
                 ImVec2 mouse, int plane, S3Point& out) {
    Vec3 o, d;
    if (!ui::sketch::screenRay(cam, imgMin, vw, vh, mouse, o, d)) return false;
    Vec3 n = planeNormal(plane);
    float denom = dot(n, d);
    if (std::fabs(denom) < 1e-6f) return false;   // ray parallel to the plane
    float t = -dot(n, o) / denom;                 // plane passes through origin
    if (t < 0.0f) return false;                   // behind the camera
    Vec3 p = o + d * t;
    out = {p.x, p.y, p.z};
    return true;
}

}  // namespace

void sketch3dSeedDemo() {
    Sketch3DModel& m = sketch3d();
    m.clear();
    // A wireframe cube-corner path around the origin — visible from the default
    // shot camera framing (radius ~ a few units). Kept small + centered so it
    // lands inside the shot camera's view.
    const float s = 24.0f;
    m.pts = {
        {0,  0,  0}, {s,  0,  0}, {s,  s,  0}, {0,  s,  0},   // base square (XY)
        {0,  0,  s}, {s,  0,  s}, {s,  s,  s}, {0,  s,  s},   // top square
    };
    m.segs = {
        {0, 1}, {1, 2}, {2, 3}, {3, 0},   // base
        {4, 5}, {5, 6}, {6, 7}, {7, 4},   // top
        {0, 4}, {1, 5}, {2, 6}, {3, 7},   // verticals
    };
    m.pendingLineStart = -1;
}

namespace {

// The pinned top-left glass tools palette. Returns true if the palette captured
// the mouse this frame (so the viewport click should not place an entity).
bool drawPalette(Sketch3DModel& m, const ImVec2& imgMin) {
    const auto& pal = ui::sketch::sketchPalette();
    const float S = ui::sketch::uiScale();

    ImGui::PushStyleColor(ImGuiCol_WindowBg,  ImColor(pal.toU32(pal.overlayBg)).Value);
    ImGui::PushStyleColor(ImGuiCol_Border,    ImColor(pal.toU32(pal.overlayBorder)).Value);
    ImGui::PushStyleColor(ImGuiCol_Text,      ImColor(pal.toU32(pal.overlayText)).Value);
    ImGui::PushStyleColor(ImGuiCol_Button,    ImColor(pal.toU32(pal.overlayBg, 0.6f)).Value);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImColor(pal.toU32(pal.accent, 0.35f)).Value);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImColor(pal.toU32(pal.accent, 0.55f)).Value);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 8.0f * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 1.0f);

    // Anchor past the left glass FeatureManager column so the palette is not occluded
    // by it. overlayColumnEdge(0) is the column's right edge in screen coords (-1 when
    // the column is collapsed/hidden — then we fall back to the viewport left edge).
    float leftEdge = ui::overlayColumnEdge(0);
    float px = (leftEdge > imgMin.x ? leftEdge : imgMin.x) + 14.0f * S;
    ImGui::SetNextWindowPos(ImVec2(px, imgMin.y + 14.0f * S), ImGuiCond_Always);
    ImGui::SetNextWindowBgAlpha(pal.overlayBg[3]);
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_AlwaysAutoResize |
                             ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoFocusOnAppearing |
                             ImGuiWindowFlags_NoNav;
    bool captured = false;
    if (ImGui::Begin("3D Sketch##sketch3d_palette", nullptr, flags)) {
        ImGui::TextUnformatted("3D Sketch (first slice)");
        ImGui::Separator();

        // Tool selector.
        ImGui::TextUnformatted("Tool");
        auto toolBtn = [&](const char* lbl, Sketch3DModel::Tool t) {
            bool sel = (m.tool == t);
            if (sel) ImGui::PushStyleColor(ImGuiCol_Button, ImColor(pal.toU32(pal.accent, 0.5f)).Value);
            if (ImGui::Button(lbl)) m.tool = t;
            if (sel) ImGui::PopStyleColor();
        };
        toolBtn("Select", Sketch3DModel::Tool::Select); ImGui::SameLine();
        toolBtn("Point",  Sketch3DModel::Tool::Point);  ImGui::SameLine();
        toolBtn("Line",   Sketch3DModel::Tool::Line);

        // Work-plane selector.
        ImGui::TextUnformatted("Work plane");
        const char* planeLbl[3] = {"XY", "YZ", "XZ"};
        for (int i = 0; i < 3; ++i) {
            bool sel = (m.plane == i);
            if (sel) ImGui::PushStyleColor(ImGuiCol_Button, ImColor(pal.toU32(pal.accent, 0.5f)).Value);
            if (ImGui::Button(planeLbl[i])) { m.plane = i; m.pendingLineStart = -1; }
            if (sel) ImGui::PopStyleColor();
            if (i < 2) ImGui::SameLine();
        }

        ImGui::Separator();
        if (ImGui::Button("Clear")) m.clear();
        ImGui::TextUnformatted("Free-space 3D sketch (first slice) - no constraints yet");

        captured = ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) ||
                   ImGui::IsAnyItemActive();
    }
    ImGui::End();

    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(6);
    return captured;
}

}  // namespace

void draw3DSketch(app::AppState& st, const Renderer::CameraXform& cam,
                  const ImVec2& imgMin, int vw, int vh, bool imgHovered) {
    (void)st;
    Sketch3DModel& m = sketch3d();
    const auto& pal = ui::sketch::sketchPalette();
    const float S = ui::sketch::uiScale();
    ImDrawList* dl = ImGui::GetForegroundDrawList();

    // --- render existing entities (world -> screen) --------------------------
    auto project = [&](const S3Point& p, ImVec2& out) -> bool {
        return ui::sketch::worldToScreen(cam, imgMin, vw, vh, p.x, p.y, p.z, out);
    };

    const ImU32 lineCol  = ui::sketch::col(pal.accent, 0.95f);
    const ImU32 ptCol    = ui::sketch::col(pal.accent, 1.0f);
    const ImU32 ptRing   = ui::sketch::col(pal.overlayText, 0.85f);
    const ImU32 pendCol  = ui::sketch::col(pal.sketchOver, 1.0f);

    for (const auto& sg : m.segs) {
        if (sg.a < 0 || sg.b < 0 || sg.a >= (int)m.pts.size() || sg.b >= (int)m.pts.size()) continue;
        ImVec2 sa, sb;
        if (project(m.pts[sg.a], sa) && project(m.pts[sg.b], sb))
            dl->AddLine(sa, sb, lineCol, 2.0f * S);
    }
    for (int i = 0; i < (int)m.pts.size(); ++i) {
        ImVec2 sp;
        if (!project(m.pts[i], sp)) continue;
        bool pending = (i == m.pendingLineStart);
        dl->AddCircleFilled(sp, 4.0f * S, pending ? pendCol : ptCol, 16);
        dl->AddCircle(sp, 4.0f * S, ptRing, 16, 1.0f);
    }

    // --- tools palette (drawn first so it wins the click) --------------------
    bool paletteCaptured = drawPalette(m, imgMin);

    // --- click-to-place on the active work plane -----------------------------
    ImGuiIO& io = ImGui::GetIO();
    bool canPlace = imgHovered && !paletteCaptured && !io.WantCaptureMouse &&
                    (m.tool == Sketch3DModel::Tool::Point || m.tool == Sketch3DModel::Tool::Line);
    if (canPlace && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        S3Point hit;
        if (rayHitPlane(cam, imgMin, vw, vh, io.MousePos, m.plane, hit)) {
            if (m.tool == Sketch3DModel::Tool::Point) {
                m.pts.push_back(hit);
            } else {  // Line
                m.pts.push_back(hit);
                int idx = (int)m.pts.size() - 1;
                if (m.pendingLineStart < 0) {
                    m.pendingLineStart = idx;
                } else {
                    m.segs.push_back({m.pendingLineStart, idx});
                    m.pendingLineStart = -1;
                }
            }
        }
    }
}

}  // namespace ui
