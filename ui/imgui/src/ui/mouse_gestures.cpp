#include "mouse_gestures.h"

#include <cmath>

#include "imgui.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "camera_anim.h"
#include "ui/viewport.h"

namespace ui {

void applyViewPreset(app::AppState& st, ViewPreset preset) {
    (void)st;
    static const float kZeroPan[3] = {0, 0, 0};
    switch (preset) {
        case ViewPreset::Front:     requestCameraAnimation(st,   0.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Back:      requestCameraAnimation(st, 180.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Left:      requestCameraAnimation(st, -90.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Right:     requestCameraAnimation(st,  90.0f,   0.0f, -1.0f, kZeroPan); break;
        case ViewPreset::Top:       requestCameraAnimation(st,   0.0f,  89.5f, -1.0f, kZeroPan); break;
        case ViewPreset::Bottom:    requestCameraAnimation(st,   0.0f, -89.5f, -1.0f, kZeroPan); break;
        case ViewPreset::Isometric: requestCameraAnimation(st,  35.0f,  30.0f, -1.0f, kZeroPan); break;
        case ViewPreset::ZoomFit: {
            ViewportState& vs = viewportState();
            requestCameraAnimation(st, vs.yaw, vs.pitch, 1.0f, kZeroPan);
            break;
        }
    }
}

namespace {

struct SegmentDef { const char* label; ViewPreset preset; };
const SegmentDef kSegs[8] = {
    { "Front",     ViewPreset::Front     },
    { "Iso",       ViewPreset::Isometric },
    { "Right",     ViewPreset::Right     },
    { "Bottom",    ViewPreset::Bottom    },
    { "Back",      ViewPreset::Back      },
    { "Zoom Fit",  ViewPreset::ZoomFit   },
    { "Left",      ViewPreset::Left      },
    { "Top",       ViewPreset::Top       },
};

constexpr float kPi     = 3.14159265f;
constexpr float kInner  = 28.0f;
constexpr float kOuter  = 80.0f;
constexpr float kDragThreshold = 18.0f;

}  // namespace

int MouseGestures::segmentAt(ImVec2 mousePos) const {
    const float s = theme::S();
    float dx = mousePos.x - m_center.x;
    float dy = -(mousePos.y - m_center.y);
    float dist = std::sqrt(dx * dx + dy * dy);
    if (dist < kInner * s || dist > kOuter * s) return -1;
    float angle = std::atan2(dx, dy);
    if (angle < 0) angle += 2.0f * kPi;
    return (int)(angle / (2.0f * kPi / 8.0f) + 0.5f) % 8;
}

void MouseGestures::update(app::AppState& st, bool imgHovered, ImVec2 vr0, ImVec2 vr1) {
    const float s = theme::S();
    ImGuiIO& io = ImGui::GetIO();

    if (!m_active) {
        if (!imgHovered || !ImGui::IsMouseClicked(ImGuiMouseButton_Right)) return;
        m_center  = io.MousePos;
        m_segment = -1;
        m_active = true;
        return;
    }

    if (!ImGui::IsMouseDown(ImGuiMouseButton_Right)) {
        if (m_segment >= 0)
            applyViewPreset(st, kSegs[m_segment].preset);
        else
            viewportState().wantContextMenu = true;
        m_active  = false;
        m_segment = -1;
        return;
    }

    float dx = io.MousePos.x - m_center.x;
    float dy = io.MousePos.y - m_center.y;
    float dist = std::sqrt(dx * dx + dy * dy);
    if (dist < kDragThreshold * s) {
        m_segment = -1;
        return;
    }

    float clampedX = m_center.x, clampedY = m_center.y;
    float outer = kOuter * s;
    if (clampedX - outer < vr0.x) clampedX = vr0.x + outer;
    if (clampedX + outer > vr1.x) clampedX = vr1.x - outer;
    if (clampedY - outer < vr0.y) clampedY = vr0.y + outer;
    if (clampedY + outer > vr1.y) clampedY = vr1.y - outer;
    m_center.x = clampedX;
    m_center.y = clampedY;

    m_segment = segmentAt(io.MousePos);
}

void MouseGestures::draw(ImDrawList* dl) {
    if (!m_active) return;

    ImGuiIO& io = ImGui::GetIO();
    float dx = io.MousePos.x - m_center.x;
    float dy = io.MousePos.y - m_center.y;
    float dist = std::sqrt(dx * dx + dy * dy);
    if (dist < kDragThreshold * theme::S()) return;

    const float s     = theme::S();
    const float inner = kInner * s;
    const float outer = kOuter * s;
    const int   segs  = 20;

    ImU32 bgColor     = TCOL(glassBg);
    ImU32 borderColor = TCOL(glassBorder);
    ImU32 textColor   = TCOL(txt0);
    ImU32 accentFill  = TCOL(accentSoft);
    ImU32 accentLine  = TCOL(accent);

    dl->AddCircleFilled(m_center, outer, bgColor, segs * 2);
    dl->AddCircleFilled(m_center, inner, IM_COL32(0, 0, 0, 0), segs);
    dl->AddCircle(m_center, outer, borderColor, segs * 2, 1.5f);
    dl->AddCircle(m_center, inner, borderColor, segs, 1.0f);

    const float segAngle = 2.0f * kPi / 8.0f;
    for (int i = 0; i < 8; ++i) {
        float boundAngle = ((float)i - 0.5f) * segAngle - kPi * 0.5f;
        float bx = m_center.x + std::cos(boundAngle) * outer;
        float by = m_center.y + std::sin(boundAngle) * outer;
        float ix = m_center.x + std::cos(boundAngle) * inner;
        float iy = m_center.y + std::sin(boundAngle) * inner;
        dl->AddLine(ImVec2(ix, iy), ImVec2(bx, by), borderColor, 1.0f);

        float centreAngle = -kPi * 0.5f + (float)i * segAngle;
        float midR = (inner + outer) * 0.5f;
        float lx = m_center.x + std::cos(centreAngle) * midR;
        float ly = m_center.y + std::sin(centreAngle) * midR;

        if (m_segment == i) {
            float a0 = centreAngle - segAngle * 0.5f;
            float a1 = centreAngle + segAngle * 0.5f;
            const int fanSteps = 6;
            for (int f = 0; f < fanSteps; ++f) {
                float fa0 = a0 + (a1 - a0) * (float)f       / (float)fanSteps;
                float fa1 = a0 + (a1 - a0) * (float)(f + 1) / (float)fanSteps;
                dl->AddTriangleFilled(
                    ImVec2(m_center.x + std::cos(fa0) * inner, m_center.y + std::sin(fa0) * inner),
                    ImVec2(m_center.x + std::cos(fa1) * inner, m_center.y + std::sin(fa1) * inner),
                    ImVec2(m_center.x + std::cos(fa0) * outer, m_center.y + std::sin(fa0) * outer),
                    accentFill);
                dl->AddTriangleFilled(
                    ImVec2(m_center.x + std::cos(fa1) * inner, m_center.y + std::sin(fa1) * inner),
                    ImVec2(m_center.x + std::cos(fa1) * outer, m_center.y + std::sin(fa1) * outer),
                    ImVec2(m_center.x + std::cos(fa0) * outer, m_center.y + std::sin(fa0) * outer),
                    accentFill);
            }
            dl->AddCircle(m_center, outer, accentLine, segs * 2, 1.5f);
        }

        const char* lbl = kSegs[i].label;
        ImVec2 ts = ImGui::CalcTextSize(lbl);
        ImU32 lblColor = (m_segment == i) ? accentLine : textColor;
        dl->AddText(ImVec2(lx - ts.x * 0.5f, ly - ts.y * 0.5f), lblColor, lbl);
    }

    dl->AddCircleFilled(m_center, inner, bgColor, segs);
    dl->AddCircle(m_center, inner, borderColor, segs, 1.0f);
}

}  // namespace ui
