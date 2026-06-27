// Icon rendering. High-visibility chrome/agent icons are hand-drawn vector glyphs; everything
// else (CAD ops, workspaces) falls back to a clean monogram PLACEHOLDER — never magenta, no asset
// dependency (see app/resources/icons/ICONS.md). Swapping in the real atlas later only edits draw().
#pragma once
#include <cstring>
#include <cctype>
#include "imgui.h"
#include "imgui_internal.h"   // ImCos/ImSin/ImMax

namespace icons {

inline void monogram(const char* name, char out[3]) {
    out[0]=out[1]=out[2]=0;
    if (!name || !*name) { out[0]='?'; return; }
    int n=0;
    for (const char* c=name; *c && n<2; ++c) {
        if (std::isalpha((unsigned char)*c)) out[n++] = (char)std::toupper((unsigned char)*c);
        if (*c=='_' && n>=1) break;
    }
    if (n==0) out[0]='?';
}

// Draw an icon centered at `c`, fitting a `size`×`size` box, tinted `col`.
inline void draw(ImDrawList* dl, const char* name, ImVec2 c, float size, ImU32 col) {
    const float r = size * 0.5f;
    auto P = [&](float x, float y){ return ImVec2(c.x + x*r, c.y + y*r); };
    const float th = ImMax(1.4f, size*0.085f);
    auto is = [&](const char* n){ return std::strcmp(name, n) == 0; };

    // ---- check / x / window controls ----
    if (is("ok_check") || is("success")) {
        ImVec2 p[3]={P(-0.5f,0.05f),P(-0.15f,0.42f),P(0.5f,-0.4f)}; dl->AddPolyline(p,3,col,0,th); return;
    }
    if (is("cancel_x") || is("win_close") || is("failure") || is("tab_close")) {
        dl->AddLine(P(-0.4f,-0.4f),P(0.4f,0.4f),col,th); dl->AddLine(P(-0.4f,0.4f),P(0.4f,-0.4f),col,th); return;
    }
    if (is("win_min")) { dl->AddLine(P(-0.42f,0.0f),P(0.42f,0.0f),col,th); return; }
    if (is("win_max")) { dl->AddRect(P(-0.4f,-0.4f),P(0.4f,0.4f),col,1.5f,0,th); return; }
    if (is("win_restore")) {
        dl->AddRect(P(-0.45f,-0.2f),P(0.2f,0.45f),col,1.5f,0,th);
        dl->AddRect(P(-0.2f,-0.45f),P(0.45f,0.2f),col,1.5f,0,th); return;
    }
    if (is("menu")) {
        for (float y=-0.32f; y<=0.34f; y+=0.32f) dl->AddLine(P(-0.42f,y),P(0.42f,y),col,th); return;
    }
    if (is("search") || is("rail_search")) {
        dl->AddCircle(P(-0.12f,-0.12f), r*0.42f, col, 0, th);
        dl->AddLine(P(0.2f,0.2f),P(0.45f,0.45f),col,th); return;
    }
    if (is("tab_add") || is("plus")) {
        dl->AddLine(P(0,-0.4f),P(0,0.4f),col,th); dl->AddLine(P(-0.4f,0),P(0.4f,0),col,th); return;
    }
    if (is("chevron_down") || is("expand")) {
        ImVec2 p[3]={P(-0.3f,-0.15f),P(0,0.2f),P(0.3f,-0.15f)}; dl->AddPolyline(p,3,col,0,th); return;
    }
    if (is("agent_send")) {
        dl->AddLine(P(0,0.4f),P(0,-0.4f),col,th);
        ImVec2 a[3]={P(-0.3f,-0.1f),P(0,-0.42f),P(0.3f,-0.1f)}; dl->AddPolyline(a,3,col,0,th); return;
    }
    if (is("agent_stop") || is("running")) {
        if (is("agent_stop")) { dl->AddRectFilled(P(-0.3f,-0.3f),P(0.3f,0.3f),col,2.f); return; }
    }

    // ---- AI sparkle (agent / ai_design) ----
    if (is("agent") || is("ai_design") || is("sparkles")) {
        const float tip=0.52f, w2=0.13f, nb=0.13f;
        ImVec2 up[3]   ={P(0,-tip),P(-w2,-nb),P(w2,-nb)};
        ImVec2 dn[3]   ={P(0, tip),P(-w2, nb),P(w2, nb)};
        ImVec2 lf[3]   ={P(-tip,0),P(-nb,-w2),P(-nb,w2)};
        ImVec2 rt[3]   ={P( tip,0),P( nb,-w2),P( nb,w2)};
        dl->AddTriangleFilled(up[0],up[1],up[2],col); dl->AddTriangleFilled(dn[0],dn[1],dn[2],col);
        dl->AddTriangleFilled(lf[0],lf[1],lf[2],col); dl->AddTriangleFilled(rt[0],rt[1],rt[2],col);
        dl->AddCircleFilled(c, r*0.1f, col); return;
    }
    // ---- thinking / usage dots ----
    if (is("thinking") || is("running")) {
        for (int i=-1;i<=1;i++) dl->AddCircleFilled(P(i*0.34f, 0.0f), r*0.12f, col); return;
    }
    // ---- action (gear) / settings ----
    if (is("action") || is("settings") || is("refresh")) {
        dl->AddCircle(c, r*0.30f, col, 0, th);
        for (int i=0;i<8;i++){ float a=i*(3.14159f/4.f); dl->AddLine(ImVec2(c.x+ImCos(a)*r*0.34f,c.y+ImSin(a)*r*0.34f),
            ImVec2(c.x+ImCos(a)*r*0.5f,c.y+ImSin(a)*r*0.5f),col,th); } return;
    }
    // ---- rail glyphs ----
    if (is("rail_tree")) {
        for (float y=-0.3f;y<=0.32f;y+=0.3f){ dl->AddCircleFilled(P(-0.32f,y),r*0.1f,col); dl->AddLine(P(-0.12f,y),P(0.4f,y),col,th);} return;
    }
    if (is("rail_bodies") || is("file_assembly")) {
        dl->AddRect(P(-0.4f,-0.4f),P(0.15f,0.15f),col,1.5f,0,th);
        dl->AddRect(P(-0.15f,-0.15f),P(0.4f,0.4f),col,1.5f,0,th); return;
    }
    if (is("rail_files") || is("folder")) {
        dl->AddRect(P(-0.42f,-0.25f),P(0.42f,0.35f),col,2.f,0,th);
        dl->AddLine(P(-0.42f,-0.25f),P(-0.05f,-0.25f),col,th); return;
    }
    if (is("rail_configs")) {
        for (float y=-0.28f;y<=0.3f;y+=0.28f){ dl->AddLine(P(-0.4f,y),P(0.4f,y),col,th); dl->AddCircleFilled(P((y)*0.6f,y),r*0.12f,col);} return;
    }
    if (is("zoom_fit")) {
        dl->AddRect(P(-0.4f,-0.4f),P(0.4f,0.4f),col,2.f,0,th); return;
    }

    // ---- fallback: monogram placeholder ----
    float r2 = size*0.5f;
    ImVec2 a(c.x-r2, c.y-r2), b(c.x+r2, c.y+r2);
    dl->AddRect(a, b, (col & 0x00FFFFFF) | 0x40000000, size*0.22f, 0, ImMax(1.0f, size*0.06f));
    char mg[3]; monogram(name, mg);
    ImFont* f = ImGui::GetFont();
    float fs = size * 0.46f;
    ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, mg);
    dl->AddText(f, fs, ImVec2(c.x - ts.x*0.5f, c.y - ts.y*0.5f), col, mg);
}

inline void inlineIcon(const char* name, float size, ImU32 col) {
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(size, size));
    draw(ImGui::GetWindowDrawList(), name, ImVec2(p.x+size*0.5f, p.y+size*0.5f), size, col);
}

} // namespace icons
