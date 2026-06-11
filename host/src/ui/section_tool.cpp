// AI-CAD Section View — SolidWorks-style planar section (C25, wave W3-I).
#include "section_tool.h"

#include <cmath>
#include <cstdlib>
#include <vector>

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"

namespace ui {

namespace {

// Section-plane presets, named to match the reference-plane labels in viewport_panel.cpp:
//   Front Plane → normal +Z,  Top Plane → normal +Y,  Right Plane → normal +X.
// A section "on the Right plane" therefore cuts along +X (matches SW's default Right preset).
struct PresetDef { const char* label; float n[3]; };
const PresetDef kPresets[3] = {
    {"Front", {0, 0, 1}},
    {"Top",   {0, 1, 0}},
    {"Right", {1, 0, 0}},
};

// Project a world point to screen with the live camera MVP (same template as view_selector /
// the plane-name chips). Returns false if behind the camera.
bool projectPt(const float* m, const Vec3& w, ImVec2 vr0, ImVec2 vr1, ImVec2& out) {
    float x  = m[0]*w.x + m[4]*w.y + m[8]*w.z  + m[12];
    float y  = m[1]*w.x + m[5]*w.y + m[9]*w.z  + m[13];
    float wc = m[3]*w.x + m[7]*w.y + m[11]*w.z + m[15];
    if (wc <= 1e-4f) return false;
    out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
    out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
    return true;
}

}  // namespace

void SectionTool::toggle(AppState& st) {
    if (m_active) {
        m_active = false;
        m_collapsed = false;
        if (st.renderer) st.renderer->setSectionPlane(m_normal, m_offset, false, nullptr, m_showCap);
        return;
    }
    m_active = true;
    m_collapsed = false;
    m_flip = false;
    m_preset = Preset::Right;
    m_haveModel = false;  // applyPreset on first frame recenters the offset on the model
    if (st.renderer) applyPreset(*st.renderer, m_preset);
}

void SectionTool::applyPreset(Renderer& renderer, Preset p) {
    m_preset = p;
    const float* n = kPresets[(int)p].n;
    float s = m_flip ? -1.0f : 1.0f;
    m_normal[0] = n[0] * s; m_normal[1] = n[1] * s; m_normal[2] = n[2] * s;
    // Offset that passes the plane through the model center: dot(center, n).
    Vec3 c = renderer.camera().center;
    m_offset = c.x * m_normal[0] + c.y * m_normal[1] + c.z * m_normal[2];
    m_haveModel = renderer.hasMesh();
}

void SectionTool::pushPlane(Renderer& renderer) {
    // Cap color: the default-part role tinted a touch brighter so the cut reads as fresh material.
    float cap[3] = {ui::palette.partDefault[0] * 0.92f + 0.05f,
                    ui::palette.partDefault[1] * 0.92f + 0.05f,
                    ui::palette.partDefault[2] * 0.92f + 0.05f};
    renderer.setSectionPlane(m_normal, m_offset, m_active, cap, m_showCap);
}

void SectionTool::updateAndDraw(AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                                ImVec2 vr0, ImVec2 vr1) {
    // One-shot AID knob: AI_CAD_SECTION=1 auto-activates with defaults at startup (verification
    // hook; mirrors the other AI_CAD_* knobs).
    if (!m_envChecked) {
        m_envChecked = true;
        const char* e = std::getenv("AI_CAD_SECTION");
        if (e && e[0] == '1') toggle(st);
    }

    if (!m_active) return;

    // Sketch mode owns the camera/view — suspend the clip while sketching, restore after.
    if (inSketch) {
        renderer.setSectionPlane(m_normal, m_offset, false, nullptr, m_showCap);
        return;
    }

    // First frame after activation (or after a model loads): recenter the offset on the model.
    if (!m_haveModel && renderer.hasMesh()) applyPreset(renderer, m_preset);

    ImGuiIO& io = ImGui::GetIO();
    const auto& pal = ui::palette;
    const float s = ui::uiScale;
    const float* m = renderer.camera().mvp.m;
    const float radius = renderer.modelRadius();
    Vec3 center = renderer.camera().center;
    Vec3 n(m_normal[0], m_normal[1], m_normal[2]);

    // ---- in-viewport manipulator: translucent plane quad + orange offset arrow ----
    // Plane center = model center projected onto the section plane.
    float dCenter = (center.x*n.x + center.y*n.y + center.z*n.z) - m_offset;
    Vec3 pc = center - n * dCenter;
    // In-plane basis for the quad.
    Vec3 up = (std::fabs(n.z) < 0.95f) ? Vec3(0,0,1) : Vec3(1,0,0);
    Vec3 u = normalize(cross(up, n));
    Vec3 v = normalize(cross(n, u));
    float e = radius * 2.2f;
    Vec3 q[4] = { pc - u*e - v*e, pc + u*e - v*e, pc + u*e + v*e, pc - u*e + v*e };

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 sq[4]; bool quadOk = true;
    for (int i = 0; i < 4; ++i) if (!projectPt(m, q[i], vr0, vr1, sq[i])) quadOk = false;
    if (quadOk) {
        // Translucent blue fill (accentSoft) + accent border.
        dl->AddConvexPolyFilled(sq, 4, ui::Palette::toU32(pal.accentSoft));
        dl->AddPolyline(sq, 4, ui::Palette::toU32(pal.accent), ImDrawFlags_Closed, 1.6f * s);
    }

    // Orange offset arrow from the plane center along the normal.
    Vec3 aTail = pc;
    Vec3 aHead = pc + n * (radius * 0.9f);
    ImVec2 sTail, sHead;
    bool arrowOk = projectPt(m, aTail, vr0, vr1, sTail) && projectPt(m, aHead, vr0, vr1, sHead);
    if (arrowOk) {
        const ImU32 orange = ui::Palette::toU32(pal.statusWarn);
        dl->AddLine(sTail, sHead, orange, 3.0f * s);
        // Arrowhead.
        ImVec2 dir(sHead.x - sTail.x, sHead.y - sTail.y);
        float len = std::sqrt(dir.x*dir.x + dir.y*dir.y);
        if (len > 1e-3f) {
            dir.x /= len; dir.y /= len;
            ImVec2 perp(-dir.y, dir.x);
            float hs = 9.0f * s;
            ImVec2 b1(sHead.x - dir.x*hs + perp.x*hs*0.55f, sHead.y - dir.y*hs + perp.y*hs*0.55f);
            ImVec2 b2(sHead.x - dir.x*hs - perp.x*hs*0.55f, sHead.y - dir.y*hs - perp.y*hs*0.55f);
            dl->AddTriangleFilled(sHead, b1, b2, orange);
        }
        dl->AddCircleFilled(sTail, 4.0f * s, orange);

        // Invisible drag button over the arrow's projected bbox.
        ImVec2 bbMin(std::fmin(sTail.x, sHead.x) - 10*s, std::fmin(sTail.y, sHead.y) - 10*s);
        ImVec2 bbMax(std::fmax(sTail.x, sHead.x) + 10*s, std::fmax(sTail.y, sHead.y) + 10*s);
        ImGui::SetCursorScreenPos(bbMin);
        ImGui::InvisibleButton("##sectionArrow", ImVec2(bbMax.x - bbMin.x, bbMax.y - bbMin.y));
        bool arrowHov = ImGui::IsItemHovered();
        if (arrowHov)
            dl->AddCircle(sHead, 8.0f * s, ui::Palette::toU32(pal.accent), 0, 2.0f * s);

        // Begin drag: capture the world-per-screen-y scaling along the arrow direction so the
        // offset tracks the cursor. We move 1 world unit when the projected head moves by the
        // screen length of the unit-normal step.
        if (ImGui::IsItemActive() && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            // Screen displacement per +1 world unit along the normal (project pc and pc+n).
            ImVec2 p0, p1;
            if (projectPt(m, pc, vr0, vr1, p0) && projectPt(m, pc + n, vr0, vr1, p1)) {
                float dx = p1.x - p0.x, dy = p1.y - p0.y;
                float plen = std::sqrt(dx*dx + dy*dy);
                if (plen > 1e-3f) {
                    m_dragging = true;
                    m_dragStartOffset = m_offset;
                    m_dragStartMouse = io.MousePos;
                    m_dragDirX = dx / plen;          // unit screen dir of +normal
                    m_dragDirY = dy / plen;
                    m_dragWorldPerPx = 1.0f / plen;  // 1 world unit == plen screen px
                }
            }
        }
    }

    if (m_dragging) {
        if (ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            // Project the total mouse delta onto the arrow's screen direction.
            float mdx = io.MousePos.x - m_dragStartMouse.x;
            float mdy = io.MousePos.y - m_dragStartMouse.y;
            float alongPx = mdx * m_dragDirX + mdy * m_dragDirY;
            m_offset = m_dragStartOffset + alongPx * m_dragWorldPerPx;
        } else {
            m_dragging = false;
        }
    }

    // ---- PM-style panel (top-left, under the HUD) or collapsed chip ----
    float px = vr0.x + 12.0f * s;
    float py = vr0.y + 46.0f * s;   // below the C24 heads-up toolbar
    ImGui::SetNextWindowPos(ImVec2(px, py), ImGuiCond_Always);
    ImGuiWindowFlags wf = ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoResize |
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_AlwaysAutoResize |
                          ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoFocusOnAppearing;

    if (m_collapsed) {
        ImGui::Begin("Section##chip", nullptr, wf | ImGuiWindowFlags_NoTitleBar);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.statusOk[0], pal.statusOk[1],
                                                    pal.statusOk[2], 1));
        if (ImGui::SmallButton("Section \xE2\x9C\x93")) m_collapsed = false;  // reopen on click
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton("x##secchip")) toggle(st);
        ImGui::End();
    } else {
        ImGui::SetNextWindowBgAlpha(0.94f);
        ImGui::Begin("Section View##panel", nullptr, wf);
        // W4-K: the window title bar already reads "Section View"; the duplicate header text was
        // removed. The presets row follows directly.

        // Plane presets.
        for (int i = 0; i < 3; ++i) {
            bool sel = ((int)m_preset == i);
            if (sel) ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(pal.buttonActive[0],
                         pal.buttonActive[1], pal.buttonActive[2], 1));
            if (ImGui::Button(kPresets[i].label, ImVec2(56 * s, 0))) applyPreset(renderer, (Preset)i);
            if (sel) ImGui::PopStyleColor();
            if (i < 2) ImGui::SameLine();
        }

        // Offset drag.
        ImGui::SetNextItemWidth(170 * s);
        float step = radius > 0 ? radius * 0.01f : 0.1f;
        ImGui::DragFloat("Offset", &m_offset, step, 0.0f, 0.0f, "%.2f");

        if (ImGui::Button("Flip", ImVec2(56 * s, 0))) { m_flip = !m_flip; applyPreset(renderer, m_preset); }
        ImGui::SameLine();
        ImGui::Checkbox("Show cap", &m_showCap);

        ImGui::Separator();
        if (ImGui::Button("OK", ImVec2(56 * s, 0))) m_collapsed = true;   // keep clip, collapse
        ImGui::SameLine();
        if (ImGui::Button("X", ImVec2(40 * s, 0))) { toggle(st); }        // disable clip
        ImGui::End();
    }

    // Esc cancels (only when the viewport is hovered/active and no text field is being edited).
    if (m_active && imgHovered && !io.WantTextInput && ImGui::IsKeyPressed(ImGuiKey_Escape))
        toggle(st);

    // Push the current plane to the renderer for this frame.
    if (m_active) pushPlane(renderer);
}

}  // namespace ui
