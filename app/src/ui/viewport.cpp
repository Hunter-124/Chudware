#include "ui/panels.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <cmath>
#include <cstdio>

namespace ui {

using namespace app;

void drawViewport(AppState& s) {
    const float S = theme::S();
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImVec2 sz = ImGui::GetContentRegionAvail();
    if (sz.x < 4 || sz.y < 4) return;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 a = p, b(p.x+sz.x, p.y+sz.y);

    // gradient background (placeholder for the reused OpenGL FBO texture)
    dl->AddRectFilledMultiColor(a, b, TCOL(viewport0), TCOL(viewport0), TCOL(viewport1), TCOL(viewport1));

    // floor grid
    ImU32 grid = (TCOL(line) & 0x00FFFFFF) | 0x22000000;
    float gs = 36*S;
    for (float x = a.x; x < b.x; x += gs) dl->AddLine(ImVec2(x, a.y), ImVec2(x, b.y), grid, 1.f);
    for (float y = a.y; y < b.y; y += gs) dl->AddLine(ImVec2(a.x, y), ImVec2(b.x, y), grid, 1.f);

    // centered placeholder body silhouette
    ImVec2 c((a.x+b.x)*0.5f, (a.y+b.y)*0.5f);
    float r = ImMin(sz.x, sz.y) * 0.16f;
    ImU32 fill = (TCOL(accent) & 0x00FFFFFF) | 0x33000000;
    dl->AddRectFilled(ImVec2(c.x-r, c.y-r*0.7f), ImVec2(c.x+r, c.y+r*0.7f), fill, 10*S);
    dl->AddRect(ImVec2(c.x-r, c.y-r*0.7f), ImVec2(c.x+r, c.y+r*0.7f), TCOL(accent), 10*S, 0, 2*S);
    const char* hint = "3D viewport  -  the reused OpenGL renderer mounts here";
    ImVec2 ts = ImGui::CalcTextSize(hint);
    dl->AddText(ImVec2(c.x-ts.x*0.5f, b.y - 28*S), TCOL(txt2), hint);

    // in-canvas mini toolbar (top-left, floating)
    {
        float bs = 30*S, n = 4, pad = 6*S;
        ImVec2 t0(a.x+12*S, a.y+12*S);
        ImVec2 t1(t0.x + pad*2 + n*bs + (n-1)*4*S, t0.y + bs + pad*2);
        dl->AddRectFilled(t0, t1, (TCOL(bg1)&0xFFFFFF)|0xE0000000, 8*S);
        dl->AddRect(t0, t1, TCOL(line), 8*S);
        const char* ics[] = {"zoom_fit","ws_model","section_props","theme"};
        for (int i=0;i<4;i++) {
            ImVec2 ic(t0.x+pad+bs*0.5f + i*(bs+4*S), t0.y+pad+bs*0.5f);
            icons::draw(dl, ics[i], ic, bs*0.5f, TCOL(txt1));
        }
    }

    // orientation gizmo (top-right) — generic axis triad, distinct from SW ViewCube
    {
        ImVec2 g(b.x - 52*S, a.y + 52*S);
        float L = 26*S;
        dl->AddLine(g, ImVec2(g.x+L, g.y), TCOL(bad), 2.5f*S);    dl->AddText(ImVec2(g.x+L+2*S, g.y-6*S), TCOL(bad), "X");
        dl->AddLine(g, ImVec2(g.x, g.y-L), TCOL(good), 2.5f*S);   dl->AddText(ImVec2(g.x-3*S, g.y-L-14*S), TCOL(good), "Z");
        dl->AddLine(g, ImVec2(g.x-L*0.6f, g.y+L*0.6f), TCOL(accent), 2.5f*S); dl->AddText(ImVec2(g.x-L*0.6f-12*S, g.y+L*0.6f), TCOL(accent), "Y");
        dl->AddCircleFilled(g, 3*S, TCOL(txt0));
    }

    // doc label bottom-left
    char lbl[128]; snprintf(lbl, sizeof(lbl), "%s", s.docs.empty()?"":s.doc().name.c_str());
    dl->AddText(theme::font(theme::Font::H2), theme::font(theme::Font::H2)->FontSize,
                ImVec2(a.x+14*S, a.y+sz.y-30*S), (TCOL(txt0)&0xFFFFFF)|0x88000000, lbl);

    // consume the area so dock/resize works
    ImGui::InvisibleButton("##vp", sz);
}

} // namespace ui
