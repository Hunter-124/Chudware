// Chudware mouse gesture wheel (C25).
#include "mouse_gestures.h"

#include <cmath>
#include <string>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"

#include "camera_anim.h"

namespace ui {

// ---- view preset helper --------------------------------------------------------
// Single source of truth for yaw/pitch presets (HUD toolbar + gesture wheel route here). W2-D:
// every preset now animates via requestCameraAnimation so the camera eases into place. ZoomFit
// keeps the current orientation and tweens zoom -> 1 and pan -> 0; the orientation presets keep the
// current zoom (pan is reset so the model recenters).
void applyViewPreset(AppState& st, ViewPreset preset) {
    static const float kZeroPan[3] = {0, 0, 0};
    switch (preset) {
        case ViewPreset::Front:     requestCameraAnimation(st,   0.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Back:      requestCameraAnimation(st, 180.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Left:      requestCameraAnimation(st, -90.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Right:     requestCameraAnimation(st,  90.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Top:       requestCameraAnimation(st,   0.0f,  89.5f, -1.0f, kZeroPan); break;
        case ViewPreset::Bottom:    requestCameraAnimation(st,   0.0f, -89.5f, -1.0f, kZeroPan); break;
        case ViewPreset::Isometric: requestCameraAnimation(st,  35.0f,  30.0f, -1.0f, kZeroPan); break;
        case ViewPreset::ZoomFit:   requestCameraAnimation(st, st.yaw, st.pitch, 1.0f, kZeroPan); break;
    }
}

// ---- segment layout ------------------------------------------------------------
// 8 segments, clockwise from top (index 0 = top = "Front").
// Segment centres at 0°, 45°, 90°, ... measured from positive-Y (up on screen).
// atan2 in ImGui coords (y down) needs care: we convert to math coords first.
struct SegmentDef { const char* label; ViewPreset preset; };
static const SegmentDef kSegs[8] = {
    { "Front",     ViewPreset::Front     },  // 0  top      (270° math / -90°)
    { "Iso",       ViewPreset::Isometric },  // 1  top-right (315° math)
    { "Right",     ViewPreset::Right     },  // 2  right     (0° math)
    { "Bottom",    ViewPreset::Bottom    },  // 3  bot-right  (45° math)
    { "Back",      ViewPreset::Back      },  // 4  bottom    (90° math)
    { "Zoom Fit",  ViewPreset::ZoomFit   },  // 5  bot-left   (135° math)
    { "Left",      ViewPreset::Left      },  // 6  left      (180° math)
    { "Top",       ViewPreset::Top       },  // 7  top-left   (225° math)
};

static constexpr float kPi     = 3.14159265f;
static constexpr float kInner  = 28.0f;   // dead-zone radius (pre-scale)
static constexpr float kOuter  = 80.0f;   // wheel outer radius (pre-scale)
static constexpr float kDragThreshold = 18.0f;  // px before wheel appears (pre-scale)

int MouseGestures::segmentAt(ImVec2 mousePos) const {
    const float s = ui::uiScale;
    float dx = mousePos.x - m_center.x;
    float dy = -(mousePos.y - m_center.y);  // flip to math Y-up
    float dist = std::sqrt(dx * dx + dy * dy);
    if (dist < kInner * s) return -1;       // dead-zone
    if (dist > kOuter * s) return -1;       // outside wheel
    // atan2 gives [-pi, pi]; convert so that "up" (dy>0, screen-up) = segment 0.
    float angle = std::atan2(dx, dy);       // note: atan2(x,y) so 0 = up
    if (angle < 0) angle += 2.0f * kPi;    // [0, 2pi)
    int seg = (int)(angle / (2.0f * kPi / 8.0f) + 0.5f) % 8;
    return seg;
}

// ---- update --------------------------------------------------------------------
void MouseGestures::update(AppState& st, bool imgHovered, ImVec2 vr0, ImVec2 vr1) {
    const float s = ui::uiScale;
    ImGuiIO& io = ImGui::GetIO();

    if (!m_active) {
        if (!imgHovered) return;
        if (!ImGui::IsMouseClicked(ImGuiMouseButton_Right)) return;
        // Record press center; the wheel won't show until drag threshold is exceeded.
        m_center  = io.MousePos;
        m_segment = -1;
        // We mark active=true only after threshold is crossed (below), but we need
        // to remember the press happened so we track drag next frame.
        m_active = true;
        return;
    }

    // Active: right button released → fire or cancel.
    if (!ImGui::IsMouseDown(ImGuiMouseButton_Right)) {
        if (m_segment >= 0) {
            // Fire the segment's preset.
            applyViewPreset(st, kSegs[m_segment].preset);
        } else {
            // kRmbContextMenuHook: right-release without meaningful drag → open viewport context menu.
            st.wantContextMenu = true;
        }
        m_active  = false;
        m_segment = -1;
        return;
    }

    // Still held: check whether we've crossed the drag threshold.
    float dx = io.MousePos.x - m_center.x;
    float dy = io.MousePos.y - m_center.y;
    float dist = std::sqrt(dx * dx + dy * dy);
    if (dist < kDragThreshold * s) {
        m_segment = -1;  // still within threshold; wheel hidden
        return;
    }

    // Clamp center so the wheel stays inside the viewport.
    float clampedX = m_center.x;
    float clampedY = m_center.y;
    float outer = kOuter * s;
    if (clampedX - outer < vr0.x) clampedX = vr0.x + outer;
    if (clampedX + outer > vr1.x) clampedX = vr1.x - outer;
    if (clampedY - outer < vr0.y) clampedY = vr0.y + outer;
    if (clampedY + outer > vr1.y) clampedY = vr1.y - outer;
    m_center.x = clampedX;
    m_center.y = clampedY;

    m_segment = segmentAt(io.MousePos);
}

// ---- draw ----------------------------------------------------------------------
void MouseGestures::draw(ImDrawList* dl) {
    if (!m_active) return;

    // Only draw once the drag threshold has been crossed (m_segment != -1 OR dist exceeded,
    // but we gate on m_center having been set and dist check done in update already).
    // Simpler: draw whenever m_active and the mouse has moved at least threshold pixels.
    ImGuiIO& io = ImGui::GetIO();
    float dx = io.MousePos.x - m_center.x;
    float dy = io.MousePos.y - m_center.y;
    float dist = std::sqrt(dx * dx + dy * dy);
    if (dist < kDragThreshold * ui::uiScale) return;  // still within threshold, stay hidden

    const float s     = ui::uiScale;
    const auto& pal   = ui::palette;
    const float inner = kInner * s;
    const float outer = kOuter * s;
    const int   segs  = 20;  // circle tessellation segments per arc

    ImU32 bgColor     = ui::Palette::toU32(pal.overlayBg);
    ImU32 borderColor = ui::Palette::toU32(pal.overlayBorder);
    ImU32 textColor   = ui::Palette::toU32(pal.overlayText);
    ImU32 accentFill  = ui::Palette::toU32(pal.accentSoft);
    ImU32 accentLine  = ui::Palette::toU32(pal.accent);

    // Radial background disc, then the inner/outer rings that read it as a donut. (There is no
    // literal punch-out: a fully transparent AddCircleFilled is a no-op in ImGui.)
    dl->AddCircleFilled(m_center, outer, bgColor, segs * 2);
    dl->AddCircle(m_center, outer, borderColor, segs * 2, 1.5f);
    dl->AddCircle(m_center, inner, borderColor, segs, 1.0f);

    // Draw each of the 8 sector dividers and labels.
    const float segAngle = 2.0f * kPi / 8.0f;
    for (int i = 0; i < 8; ++i) {
        // Sector boundary lines (dividers).
        float boundAngle = ((float)i - 0.5f) * segAngle - kPi * 0.5f;
        // atan2(x,y) convention: angle 0 = screen-up = -pi/2 in standard math.
        // Re-derive: seg 0 at top means centre at angle = -pi/2 (screen up).
        // Boundary at i: startAngle = -pi/2 + i*segAngle - segAngle/2.
        float bx = m_center.x + std::cos(boundAngle) * outer;
        float by = m_center.y + std::sin(boundAngle) * outer;
        float ix = m_center.x + std::cos(boundAngle) * inner;
        float iy = m_center.y + std::sin(boundAngle) * inner;
        dl->AddLine(ImVec2(ix, iy), ImVec2(bx, by), borderColor, 1.0f);

        // Sector centre angle for label + hovered fill.
        float centreAngle = -kPi * 0.5f + (float)i * segAngle;
        float midR = (inner + outer) * 0.5f;
        float lx = m_center.x + std::cos(centreAngle) * midR;
        float ly = m_center.y + std::sin(centreAngle) * midR;

        // Hovered sector fill (fan-shaped approximation via triangles).
        if (m_segment == i) {
            float a0 = centreAngle - segAngle * 0.5f;
            float a1 = centreAngle + segAngle * 0.5f;
            const int fanSteps = 6;
            for (int f = 0; f < fanSteps; ++f) {
                float fa0 = a0 + (a1 - a0) * (float)f       / (float)fanSteps;
                float fa1 = a0 + (a1 - a0) * (float)(f + 1) / (float)fanSteps;
                // outer arc triangle
                dl->AddTriangleFilled(
                    ImVec2(m_center.x + std::cos(fa0) * inner,
                           m_center.y + std::sin(fa0) * inner),
                    ImVec2(m_center.x + std::cos(fa1) * inner,
                           m_center.y + std::sin(fa1) * inner),
                    ImVec2(m_center.x + std::cos(fa0) * outer,
                           m_center.y + std::sin(fa0) * outer),
                    accentFill);
                dl->AddTriangleFilled(
                    ImVec2(m_center.x + std::cos(fa1) * inner,
                           m_center.y + std::sin(fa1) * inner),
                    ImVec2(m_center.x + std::cos(fa1) * outer,
                           m_center.y + std::sin(fa1) * outer),
                    ImVec2(m_center.x + std::cos(fa0) * outer,
                           m_center.y + std::sin(fa0) * outer),
                    accentFill);
            }
            // Hovered border.
            dl->AddCircle(m_center, outer, accentLine, segs * 2, 1.5f);
        }

        // Label.
        const char* lbl = kSegs[i].label;
        ImVec2 ts = ImGui::CalcTextSize(lbl);
        ImU32 lblColor = (m_segment == i) ? accentLine : textColor;
        dl->AddText(ImVec2(lx - ts.x * 0.5f, ly - ts.y * 0.5f), lblColor, lbl);
    }

    // Dead-zone centre dot.
    dl->AddCircleFilled(m_center, inner, bgColor, segs);
    dl->AddCircle(m_center, inner, borderColor, segs, 1.0f);
}

}  // namespace ui
