// AI-CAD Section View — SolidWorks-style multi-plane section (C28-C).
#include "section_tool.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"

namespace ui {

namespace {

struct PresetDef { const char* label; float n[3]; };
const PresetDef kPresets[3] = {
    {"Front", {0, 0, 1}},
    {"Top",   {0, 1, 0}},
    {"Right", {1, 0, 0}},
};

// Default axis-aligned normals for planes 0/1/2 (used by AI_CAD_SECTION=2/3).
const float kAxisNormals[3][3] = {{1,0,0}, {0,1,0}, {0,0,1}};
const SectionTool::Preset kAxisPresets[3] = {
    SectionTool::Preset::Right, SectionTool::Preset::Top, SectionTool::Preset::Front
};

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
        if (st.renderer) st.renderer->setSectionPlane(m_planes[0].normal, 0, false, nullptr, false);
        return;
    }
    m_active = true;
    m_collapsed = false;
    m_planeCount = 1;
    for (int i = 0; i < kMaxPlanes; ++i) {
        m_planes[i] = PlaneState{};
        m_planes[i].enabled = (i == 0);
        m_planes[i].preset = Preset::Right;
        m_planes[i].normal[0] = 1; m_planes[i].normal[1] = 0; m_planes[i].normal[2] = 0;
    }
    if (st.renderer) applyPreset(*st.renderer, 0, Preset::Right);
}

void SectionTool::applyPreset(Renderer& renderer, int pi, Preset p) {
    PlaneState& pl = m_planes[pi];
    pl.preset = p;
    const float* n = kPresets[(int)p].n;
    float s = pl.flip ? -1.0f : 1.0f;
    pl.normal[0] = n[0]*s; pl.normal[1] = n[1]*s; pl.normal[2] = n[2]*s;
    Vec3 c = renderer.camera().center;
    pl.offset = c.x*pl.normal[0] + c.y*pl.normal[1] + c.z*pl.normal[2];
    pl.haveModel = renderer.hasMesh();
}

void SectionTool::pushPlanes(Renderer& renderer) {
    // Build arrays of only the enabled planes.
    float ns[kMaxPlanes][3];
    float offs[kMaxPlanes];
    int cnt = 0;
    for (int i = 0; i < m_planeCount; ++i) {
        if (!m_planes[i].enabled) continue;
        ns[cnt][0] = m_planes[i].normal[0];
        ns[cnt][1] = m_planes[i].normal[1];
        ns[cnt][2] = m_planes[i].normal[2];
        offs[cnt]  = m_planes[i].offset;
        ++cnt;
    }

    float cap[3] = {ui::palette.partDefault[0]*0.92f + 0.05f,
                    ui::palette.partDefault[1]*0.92f + 0.05f,
                    ui::palette.partDefault[2]*0.92f + 0.05f};
    if (cnt > 0) {
        renderer.setSectionPlanes(ns, offs, cnt, cap, m_showCap);
    } else {
        // All planes disabled — clear clipping but leave the tool active.
        renderer.setSectionPlane(m_planes[0].normal, m_planes[0].offset, false, cap, m_showCap);
    }
}

void SectionTool::updateAndDraw(AppState& st, Renderer& renderer, bool imgHovered, bool inSketch,
                                ImVec2 vr0, ImVec2 vr1) {
    // One-shot AI_CAD_SECTION knob: =1 → single default plane; =2/=3 → N axis-aligned planes.
    if (!m_envChecked) {
        m_envChecked = true;
        const char* e = std::getenv("AI_CAD_SECTION");
        if (e) {
            int n = e[0] - '0';
            if (n >= 1 && n <= 3) {
                m_active = true;
                m_collapsed = false;
                m_planeCount = n;
                for (int i = 0; i < kMaxPlanes; ++i) {
                    m_planes[i] = PlaneState{};
                    m_planes[i].enabled = (i < n);
                    m_planes[i].preset = kAxisPresets[i];
                    m_planes[i].normal[0] = kAxisNormals[i][0];
                    m_planes[i].normal[1] = kAxisNormals[i][1];
                    m_planes[i].normal[2] = kAxisNormals[i][2];
                }
            }
        }
    }

    if (!m_active) return;

    if (inSketch) {
        // Suspend clipping while sketching.
        renderer.setSectionPlane(m_planes[0].normal, 0, false, nullptr, false);
        return;
    }

    // Recenter planes on model load.
    for (int i = 0; i < m_planeCount; ++i) {
        if (!m_planes[i].haveModel && renderer.hasMesh())
            applyPreset(renderer, i, m_planes[i].preset);
    }

    ImGuiIO& io = ImGui::GetIO();
    const auto& pal = ui::palette;
    const float s = ui::uiScale;
    const float* mvpM = renderer.camera().mvp.m;
    const float radius = renderer.modelRadius();
    Vec3 center = renderer.camera().center;

    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Draw manipulators for all visible enabled planes.
    for (int pi = 0; pi < m_planeCount; ++pi) {
        PlaneState& pl = m_planes[pi];
        if (!pl.enabled) continue;

        Vec3 n(pl.normal[0], pl.normal[1], pl.normal[2]);
        float dCenter = center.x*n.x + center.y*n.y + center.z*n.z - pl.offset;
        Vec3 pc = center - n*dCenter;
        Vec3 up = (std::fabs(n.z) < 0.95f) ? Vec3(0,0,1) : Vec3(1,0,0);
        Vec3 u = normalize(cross(up, n));
        Vec3 v = normalize(cross(n, u));
        float e = radius * 2.2f;
        Vec3 q[4] = { pc - u*e - v*e, pc + u*e - v*e, pc + u*e + v*e, pc - u*e + v*e };

        ImVec2 sq[4]; bool quadOk = true;
        for (int i = 0; i < 4; ++i) if (!projectPt(mvpM, q[i], vr0, vr1, sq[i])) quadOk = false;
        if (quadOk) {
            dl->AddConvexPolyFilled(sq, 4, ui::Palette::toU32(pal.accentSoft));
            dl->AddPolyline(sq, 4, ui::Palette::toU32(pal.accent), ImDrawFlags_Closed, 1.6f*s);
        }

        // Arrow: active/hovered plane draws full orange; others slightly dimmer.
        Vec3 aTail = pc, aHead = pc + n*(radius*0.9f);
        ImVec2 sTail, sHead;
        bool arrowOk = projectPt(mvpM, aTail, vr0, vr1, sTail) &&
                       projectPt(mvpM, aHead, vr0, vr1, sHead);
        if (arrowOk) {
            // Determine if this arrow is "active" (hovered or being dragged).
            ImVec2 bbMin(std::fmin(sTail.x,sHead.x)-10*s, std::fmin(sTail.y,sHead.y)-10*s);
            ImVec2 bbMax(std::fmax(sTail.x,sHead.x)+10*s, std::fmax(sTail.y,sHead.y)+10*s);
            ImGui::SetCursorScreenPos(bbMin);
            char btnId[32]; std::snprintf(btnId, sizeof(btnId), "##secArr%d", pi);
            ImGui::InvisibleButton(btnId, ImVec2(bbMax.x-bbMin.x, bbMax.y-bbMin.y));
            bool arrowHov = ImGui::IsItemHovered();
            bool isActive = arrowHov || pl.dragging;

            float arrowAlpha = isActive ? 1.0f : 0.65f;
            ImVec4 warnV = ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], arrowAlpha);
            ImU32 orange = ImGui::ColorConvertFloat4ToU32(warnV);

            dl->AddLine(sTail, sHead, orange, 3.0f*s);
            ImVec2 dir(sHead.x-sTail.x, sHead.y-sTail.y);
            float len = std::sqrt(dir.x*dir.x + dir.y*dir.y);
            if (len > 1e-3f) {
                dir.x /= len; dir.y /= len;
                ImVec2 perp(-dir.y, dir.x);
                float hs = 9.0f*s;
                ImVec2 b1(sHead.x-dir.x*hs+perp.x*hs*0.55f, sHead.y-dir.y*hs+perp.y*hs*0.55f);
                ImVec2 b2(sHead.x-dir.x*hs-perp.x*hs*0.55f, sHead.y-dir.y*hs-perp.y*hs*0.55f);
                dl->AddTriangleFilled(sHead, b1, b2, orange);
            }
            dl->AddCircleFilled(sTail, 4.0f*s, orange);
            if (arrowHov)
                dl->AddCircle(sHead, 8.0f*s, ui::Palette::toU32(pal.accent), 0, 2.0f*s);

            // Begin drag for this plane's arrow.
            if (ImGui::IsItemActive() && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                ImVec2 p0, p1;
                if (projectPt(mvpM, pc, vr0, vr1, p0) && projectPt(mvpM, pc+n, vr0, vr1, p1)) {
                    float dx = p1.x-p0.x, dy = p1.y-p0.y;
                    float plen = std::sqrt(dx*dx+dy*dy);
                    if (plen > 1e-3f) {
                        pl.dragging = true;
                        pl.dragStartOffset = pl.offset;
                        pl.dragStartMouse = io.MousePos;
                        pl.dragDirX = dx/plen;
                        pl.dragDirY = dy/plen;
                        pl.dragWorldPerPx = 1.0f/plen;
                    }
                }
            }
        }

        if (pl.dragging) {
            if (ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                float mdx = io.MousePos.x - pl.dragStartMouse.x;
                float mdy = io.MousePos.y - pl.dragStartMouse.y;
                float along = mdx*pl.dragDirX + mdy*pl.dragDirY;
                pl.offset = pl.dragStartOffset + along*pl.dragWorldPerPx;
            } else {
                pl.dragging = false;
            }
        }
    }

    // ---- PM panel or collapsed chip ----
    float px = vr0.x + 12.0f*s;
    float py = vr0.y + 46.0f*s;
    ImGui::SetNextWindowPos(ImVec2(px, py), ImGuiCond_Always);
    ImGuiWindowFlags wf = ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoResize |
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_AlwaysAutoResize |
                          ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoFocusOnAppearing;

    // Count how many planes are enabled for the chip label.
    int enabledCount = 0;
    for (int i = 0; i < m_planeCount; ++i) if (m_planes[i].enabled) ++enabledCount;

    if (m_collapsed) {
        ImGui::Begin("Section##chip", nullptr, wf | ImGuiWindowFlags_NoTitleBar);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(pal.statusOk[0], pal.statusOk[1],
                                                     pal.statusOk[2], 1));
        char chipLbl[32];
        if (enabledCount > 1) std::snprintf(chipLbl, sizeof(chipLbl), "Section \xC3\x97%d \xE2\x9C\x93", enabledCount);
        else                  std::snprintf(chipLbl, sizeof(chipLbl), "Section \xE2\x9C\x93");
        if (ImGui::SmallButton(chipLbl)) m_collapsed = false;
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton("x##secchip")) toggle(st);
        ImGui::End();
    } else {
        ImGui::SetNextWindowBgAlpha(0.94f);
        ImGui::Begin("Section View##panel", nullptr, wf);

        // Draw one section row per visible plane index.
        for (int pi = 0; pi < m_planeCount; ++pi) {
            PlaneState& pl = m_planes[pi];

            // Collapsible header for plane 2 and 3; plane 1 is always open.
            bool showRow = true;
            if (pi > 0) {
                char hdrLbl[32]; std::snprintf(hdrLbl, sizeof(hdrLbl), "Section %d", pi+1);
                showRow = ImGui::CollapsingHeader(hdrLbl,
                              pi < m_planeCount-1 || m_planeCount < kMaxPlanes
                                  ? ImGuiTreeNodeFlags_None : ImGuiTreeNodeFlags_DefaultOpen);
                ImGui::SameLine();
                char chkId[32]; std::snprintf(chkId, sizeof(chkId), "##sec_en%d", pi);
                ImGui::Checkbox(chkId, &pl.enabled);
            } else {
                ImGui::TextUnformatted("Section 1");
            }

            if (!showRow) continue;

            ImGui::PushID(pi);

            // Preset buttons.
            for (int k = 0; k < 3; ++k) {
                bool sel = ((int)pl.preset == k);
                if (sel) ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 1));
                if (ImGui::Button(kPresets[k].label, ImVec2(56*s, 0)))
                    applyPreset(renderer, pi, (Preset)k);
                if (sel) ImGui::PopStyleColor();
                if (k < 2) ImGui::SameLine();
            }

            // Offset drag.
            ImGui::SetNextItemWidth(170*s);
            float step = radius > 0 ? radius*0.01f : 0.1f;
            ImGui::DragFloat("Offset", &pl.offset, step, 0.0f, 0.0f, "%.2f");

            // Flip + Show cap (cap shared across all planes).
            if (ImGui::Button("Flip", ImVec2(56*s, 0))) {
                pl.flip = !pl.flip;
                applyPreset(renderer, pi, pl.preset);
            }
            if (pi == 0) {
                ImGui::SameLine();
                ImGui::Checkbox("Show cap", &m_showCap);
            }

            ImGui::PopID();
            if (pi < m_planeCount-1) ImGui::Separator();
        }

        // Add plane buttons.
        ImGui::Separator();
        if (m_planeCount < kMaxPlanes) {
            char addLbl[32]; std::snprintf(addLbl, sizeof(addLbl), "+ Section %d", m_planeCount+1);
            if (ImGui::Button(addLbl, ImVec2(120*s, 0))) {
                int ni = m_planeCount;
                m_planes[ni] = PlaneState{};
                m_planes[ni].enabled = true;
                m_planes[ni].preset = kAxisPresets[ni];
                m_planes[ni].normal[0] = kAxisNormals[ni][0];
                m_planes[ni].normal[1] = kAxisNormals[ni][1];
                m_planes[ni].normal[2] = kAxisNormals[ni][2];
                applyPreset(renderer, ni, m_planes[ni].preset);
                m_planeCount++;
            }
        }
        if (m_planeCount > 1) {
            if (m_planeCount < kMaxPlanes) ImGui::SameLine();
            char remLbl[32]; std::snprintf(remLbl, sizeof(remLbl), "- Section %d", m_planeCount);
            if (ImGui::Button(remLbl, ImVec2(120*s, 0))) {
                m_planes[m_planeCount-1].enabled = false;
                m_planeCount--;
            }
        }

        ImGui::Separator();
        if (ImGui::Button("OK", ImVec2(56*s, 0))) m_collapsed = true;
        ImGui::SameLine();
        if (ImGui::Button("X", ImVec2(40*s, 0))) toggle(st);
        ImGui::End();
    }

    // Esc disables all.
    if (m_active && imgHovered && !io.WantTextInput && ImGui::IsKeyPressed(ImGuiKey_Escape))
        toggle(st);

    if (m_active) pushPlanes(renderer);
}

}  // namespace ui
