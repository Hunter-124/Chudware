#include "ui/panels.h"
#include "ui/doc_tabs.h"
#include "ui/shortcut_bar.h"
#include "ui/sketch/sketch_mode.h"
#include "ui/sketch/sketch_seam.h"
#include "app/app_state.h"
#include "viewport/scene.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "renderer.h"
#include "render/id_pass.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <cmath>
#include <cstdio>

namespace ui {

using namespace app;

static Renderer* g_renderer = nullptr;
static render::IdPass* g_idPass = nullptr;
static ShortcutBar s_shortcutBar;
static SketchMode s_sketch;

void setViewportPipeline(Renderer* r, render::IdPass* idPass) {
    g_renderer = r;
    g_idPass = idPass;
}

void drawViewport(AppState& s) {
    const float S = theme::S();

    ImGuiDockNode* node = ImGui::GetWindowDockNode();
    if (node) node->LocalFlags |= ImGuiDockNodeFlags_NoTabBar;
    if (docTabStripHeight(s) > 0.0f) drawDocTabs(s, ImGui::GetContentRegionAvail().x);

    ImVec2 p = ImGui::GetCursorScreenPos();
    ImVec2 sz = ImGui::GetContentRegionAvail();
    if (sz.x < 8 || sz.y < 8) return;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 a = p, b(p.x+sz.x, p.y+sz.y);

    // refresh the mesh when the engine revision changes
    if (s.scene && s.engineReady && s.viewMeshRev != s.engine.revision() && !s.engine.aicmPath().empty()) {
        s.scene->loadAicm(s.engine.aicmPath());
        s.viewMeshRev = s.engine.revision();
    }
    bool live = s.scene && s.scene->hasMesh();
    bool imgHovered = false;

    if (live) {
        s.scene->setStyle(s.viewStyle);
        s.scene->resize((int)sz.x, (int)sz.y);
        s.scene->render();
        ImGui::SetNextItemAllowOverlap();
        ImGui::Image((ImTextureID)(intptr_t)s.scene->texture(), sz, ImVec2(0,1), ImVec2(1,0));
        imgHovered = ImGui::IsItemHovered();
        // navigation (middle = orbit, Ctrl+middle / right = pan, wheel = zoom)
        if (imgHovered) {
            ImGuiIO& io = ImGui::GetIO();
            bool dragging = ImGui::IsMouseDragging(ImGuiMouseButton_Middle, 0.f) ||
                            ImGui::IsMouseDragging(ImGuiMouseButton_Right, 0.f);
            if (ImGui::IsMouseDragging(ImGuiMouseButton_Middle, 0.f)) {
                if (io.KeyCtrl) s.scene->pan(io.MouseDelta.x, io.MouseDelta.y);
                else            s.scene->orbit(-io.MouseDelta.x*0.01f, io.MouseDelta.y*0.01f);
            } else if (ImGui::IsMouseDragging(ImGuiMouseButton_Right, 0.f)) {
                s.scene->pan(io.MouseDelta.x, io.MouseDelta.y);
            }
            if (io.MouseWheel != 0.f) s.scene->zoom(io.MouseWheel);
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Middle)) s.scene->homeView();

            // pick faces by default; edges when a fillet/chamfer tool is active
            int wantKind = (s.activeTool=="fillet" || s.activeTool=="chamfer") ? 1 : 0;
            if (s.scene->pickKind() != wantKind) { s.scene->setPickKind(wantKind); s.scene->clearSelection(); s.selCount = 0; }

            // picking: hover-highlight + left-click select (Shift = add)
            viewport::Scene::PickRef pr;
            if (!dragging && s.scene->pickAt((int)(io.MousePos.x - a.x), (int)(io.MousePos.y - a.y), pr)) {
                s.scene->setHovered(&pr);
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) s.scene->toggleSelect(pr);
            } else {
                s.scene->setHovered(nullptr);
                if (!dragging && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !io.KeyShift)
                    s.scene->clearSelection();
            }
            s.selCount = (int)s.scene->selection().size();
        } else {
            s.scene->setHovered(nullptr);
        }
    } else {
        dl->AddRectFilledMultiColor(a, b, TCOL(viewport0), TCOL(viewport0), TCOL(viewport1), TCOL(viewport1));
        ImU32 grid = (TCOL(line) & 0x00FFFFFF) | 0x22000000; float gs = 36*S;
        for (float x = a.x; x < b.x; x += gs) dl->AddLine(ImVec2(x,a.y), ImVec2(x,b.y), grid, 1.f);
        for (float y = a.y; y < b.y; y += gs) dl->AddLine(ImVec2(a.x,y), ImVec2(b.x,y), grid, 1.f);
        const char* hint = s.engineReady ? "Building preview..." : "3D viewport  -  engine offline (demo mode)";
        ImVec2 ts = ImGui::CalcTextSize(hint);
        dl->AddText(ImVec2((a.x+b.x)*0.5f-ts.x*0.5f, b.y-28*S), TCOL(txt2), hint);
        ImGui::InvisibleButton("##vp", sz);
        imgHovered = ImGui::IsItemHovered();
    }

    const int vw = (int)sz.x, vh = (int)sz.y;
    ImGuiIO& io = ImGui::GetIO();
    if (g_renderer) {
        auto host = sketch::makeHost(s, g_renderer);
        host.sketchPlanePickActive = s.inSketch;
        host.hasDocMesh = live;
        host.idPass = g_idPass;
        s_sketch.drawGhosts(host, a, b, vw, vh);
        if (s.inSketch)
            s_sketch.drawOverlay(host, io, a, b, vw, vh, imgHovered);
        else if (host.sketchPlanePickActive)
            s_sketch.drawOverlay(host, io, a, b, vw, vh, imgHovered);
    }

    s_shortcutBar.update(s, imgHovered, s.inSketch, a, b);
    s_shortcutBar.draw(s, s.inSketch);

    // ---- in-canvas toolbar (top-left): Fit, Shaded, Shaded+Edges, Wireframe ----
    {
        float bs = 30*S, pad = 6*S; int n = 4;
        ImVec2 t0(a.x+12*S, a.y+12*S);
        ImVec2 t1(t0.x + pad*2 + n*bs + (n-1)*4*S, t0.y + bs + pad*2);
        dl->AddRectFilled(t0, t1, (TCOL(bg1)&0xFFFFFF)|0xE6000000, 8*S);
        dl->AddRect(t0, t1, TCOL(line), 8*S);
        const char* ics[4] = {"zoom_fit", "ws_model", "section_props", "grp_pattern"};
        const char* tips[4] = {"Fit", "Shaded", "Shaded + Edges", "Wireframe"};
        for (int i = 0; i < 4; i++) {
            ImVec2 c(t0.x+pad+bs*0.5f + i*(bs+4*S), t0.y+pad+bs*0.5f);
            ImVec2 q0(c.x-bs*0.5f, c.y-bs*0.5f), q1(c.x+bs*0.5f, c.y+bs*0.5f);
            ImGui::SetCursorScreenPos(q0);
            ImGui::SetNextItemAllowOverlap();
            ImGui::PushID(i);
            bool clk = ImGui::InvisibleButton("vt", ImVec2(bs, bs));
            bool hv = ImGui::IsItemHovered();
            if (hv) ImGui::SetTooltip("%s", tips[i]);
            ImGui::PopID();
            bool activeStyle = (i >= 1 && s.viewStyle == i-1);
            if (hv || activeStyle) dl->AddRectFilled(q0, q1, activeStyle?TCOL(accentDim):TCOL(hover), 5*S);
            icons::draw(dl, ics[i], c, bs*0.5f, (hv||activeStyle)?TCOL(txt0):TCOL(txt1));
            if (clk) { if (i == 0) { if (s.scene) s.scene->homeView(); } else s.viewStyle = i-1; }
        }
    }

    // orientation gizmo (top-right) — generic axis triad
    {
        ImVec2 g(b.x - 52*S, a.y + 52*S); float L = 26*S;
        dl->AddLine(g, ImVec2(g.x+L, g.y), TCOL(bad), 2.5f*S);   dl->AddText(ImVec2(g.x+L+2*S, g.y-6*S), TCOL(bad), "X");
        dl->AddLine(g, ImVec2(g.x, g.y-L), TCOL(good), 2.5f*S);  dl->AddText(ImVec2(g.x-3*S, g.y-L-14*S), TCOL(good), "Z");
        dl->AddLine(g, ImVec2(g.x-L*0.6f, g.y+L*0.6f), TCOL(accent), 2.5f*S); dl->AddText(ImVec2(g.x-L*0.6f-12*S, g.y+L*0.6f), TCOL(accent), "Y");
        dl->AddCircleFilled(g, 3*S, TCOL(txt0));
    }

    // doc label bottom-left
    if (!s.docs.empty())
        dl->AddText(theme::font(theme::Font::H2), theme::font(theme::Font::H2)->FontSize,
                    ImVec2(a.x+14*S, a.y+sz.y-30*S), (TCOL(txt0)&0xFFFFFF)|0x99000000, s.doc().name.c_str());
}

} // namespace ui
