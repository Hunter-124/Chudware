// Design tokens — the single source of color/spacing/shape for the new GUI.
// Dark-first with a light variant. See app/DESIGN.md §7.
#pragma once
// NOTE: MSVC 14.44's <cmath> no longer self-includes <math.h> and relies on the C math
// functions (acosf/sinf/...) already being in the global namespace. Include <math.h> first
// so every TU that funnels through this header gets a working <cmath>.
#include <math.h>
#include <cmath>
#include "imgui.h"
#include "imgui_internal.h"   // ImMax/ImMin/ImClamp/ImLerp/ImRect used across the UI

namespace theme {

// 0xRRGGBBAA -> ImU32 / ImVec4 helpers.
inline ImU32  rgba(unsigned int hex) { return IM_COL32((hex>>24)&0xFF,(hex>>16)&0xFF,(hex>>8)&0xFF,hex&0xFF); }
inline ImVec4 vec (unsigned int hex) {
    return ImVec4(((hex>>24)&0xFF)/255.f,((hex>>16)&0xFF)/255.f,((hex>>8)&0xFF)/255.f,(hex&0xFF)/255.f);
}

// The full palette. Swapping this struct swaps the whole theme — no widget ever
// hardcodes a color; it reads a token here (the old code's per-widget-color bug is designed out).
struct Palette {
    // surfaces (deepest -> raised)
    unsigned int bg0, bg1, bg2, bg3, hover, active, line;
    // text
    unsigned int txt0, txt1, txt2;
    // semantic
    unsigned int accent, accentDim, good, warn, bad;
    // viewport clear
    unsigned int viewport0, viewport1;   // gradient top/bottom
};

inline Palette darkPalette() {
    Palette p{};
    p.bg0=0x0E1116FF; p.bg1=0x151A21FF; p.bg2=0x1C232CFF; p.bg3=0x232C37FF;
    p.hover=0x2A333EFF; p.active=0x313C49FF; p.line=0x2C3744FF;
    p.txt0=0xE6EAF0FF; p.txt1=0x9AA7B5FF; p.txt2=0x5E6B7AFF;
    p.accent=0x4C8DFFFF; p.accentDim=0x2E4A78FF;
    p.good=0x46C08DFF; p.warn=0xE2B33EFF; p.bad=0xE5604DFF;
    p.viewport0=0x222A33FF; p.viewport1=0x141A20FF;
    return p;
}

inline Palette lightPalette() {
    Palette p{};
    p.bg0=0xF4F6F9FF; p.bg1=0xFBFCFDFF; p.bg2=0xFFFFFFFF; p.bg3=0xEDF1F5FF;
    p.hover=0xE6ECF2FF; p.active=0xDCE5EEFF; p.line=0xD7DEE6FF;
    p.txt0=0x1B232CFF; p.txt1=0x5A6776FF; p.txt2=0x95A1AEFF;
    p.accent=0x2F73E8FF; p.accentDim=0xBFD3F5FF;
    p.good=0x1F9D6BFF; p.warn=0xC68A12FF; p.bad=0xCE4632FF;
    p.viewport0=0xDDE4EBFF; p.viewport1=0xC3CDD7FF;
    return p;
}

// Geometry tokens (logical px @1.0×; scaled once at apply).
struct Metrics {
    float titleBarH   = 38.f;
    float workspaceH  = 34.f;
    float toolbarH    = 76.f;   // context toolbar (big tool buttons)
    float railW       = 48.f;
    float statusH     = 26.f;
    float bigBtn      = 52.f;   // context toolbar button square
    float rounding    = 6.f;
    float roundingLg  = 10.f;
    float grid        = 4.f;    // base spacing unit
};

// Motion tokens (seconds).
namespace motion {
    constexpr float micro    = 0.090f;
    constexpr float standard = 0.160f;
    constexpr float entrance = 0.220f;
}

// easeOutCubic
inline float easeOut(float t){ t=t<0?0:(t>1?1:t); float u=1-t; return 1-u*u*u; }
// frame-rate independent exponential approach toward target (a = 1-exp(-dt/tau))
inline float approach(float cur,float target,float dt,float tau=0.06f){
    float a = 1.f - std::exp(-dt/ (tau<1e-4f?1e-4f:tau));
    return cur + (target-cur)*a;
}

} // namespace theme
