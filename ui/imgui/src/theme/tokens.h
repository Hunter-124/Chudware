// Design tokens — single source of color/spacing/shape for the unified GUI shell.
// Panels read tokens only; no literal ImVec4/IM_COL32 in ui/imgui/src/ui/**.
// See docs/GLASS_DESIGN_SPEC.md.
#pragma once
#include <math.h>
#include <cmath>
#include "imgui.h"
#include "imgui_internal.h"

namespace theme {

// 0xRRGGBBAA -> ImU32 / ImVec4 helpers.
inline ImU32  rgba(unsigned int hex) { return IM_COL32((hex>>24)&0xFF,(hex>>16)&0xFF,(hex>>8)&0xFF,hex&0xFF); }
inline ImVec4 vec (unsigned int hex) {
    return ImVec4(((hex>>24)&0xFF)/255.f,((hex>>16)&0xFF)/255.f,((hex>>8)&0xFF)/255.f,(hex&0xFF)/255.f);
}
inline ImVec4 roleVec(unsigned int hex, float alphaMul = 1.f) {
    ImVec4 v = vec(hex);
    v.w *= alphaMul;
    return v;
}
inline ImU32 roleU32(unsigned int hex, float alphaMul = 1.f) {
    return ImGui::ColorConvertFloat4ToU32(roleVec(hex, alphaMul));
}
inline ImU32 roleU32A(unsigned int hex, float a) {
    ImVec4 v = vec(hex);
    v.w = a;
    return ImGui::ColorConvertFloat4ToU32(v);
}

// iOS UIBlurEffect material semantics (docs/GLASS_DESIGN_SPEC.md §2).
enum class MaterialLevel { UltraThin, Thin, Regular, Thick };

struct MaterialToken {
    float tintAlpha;   // translucent wash alpha over backdrop
    int   blurPasses;  // Kawase passes (F1 viewport); unused when no backdrop
};

inline MaterialToken materialToken(MaterialLevel m) {
    switch (m) {
    case MaterialLevel::UltraThin: return {0.12f, 2};
    case MaterialLevel::Thin:      return {0.22f, 3};
    case MaterialLevel::Regular:   return {0.35f, 4};
    case MaterialLevel::Thick:     return {0.52f, 5};
    }
    return {0.35f, 4};
}

// Named palette roles — swap the struct to swap the whole theme.
struct Palette {
    // surfaces (deepest -> raised)
    unsigned int bg0, bg1, bg2, bg3, hover, active, line;
    // text
    unsigned int txt0, txt1, txt2;
    // semantic
    unsigned int accent, accentDim, accentSoft, accentMid, good, warn, bad;
    // viewport clear
    unsigned int viewport0, viewport1;
    // glass (5-layer glassBackground + row pills)
    unsigned int glassBg, glassBorder, glassHighlight, glassShadow;
    // vibrancy text over glass (light-on-dark viewport vs dark-on-bright bleed)
    unsigned int vibrancyLightPrimary, vibrancyLightSecondary, vibrancyLightTertiary;
    unsigned int vibrancyDarkPrimary,  vibrancyDarkSecondary,  vibrancyDarkTertiary;
};

inline Palette darkPalette() {
    Palette p{};
    p.bg0=0x15171CFF; p.bg1=0x181C24FF; p.bg2=0x1E2430FF; p.bg3=0x252D3AFF;
    p.hover=0x2E3848FF; p.active=0x364252FF; p.line=0x2A3444D9;
    p.txt0=0xE5EAF0FF; p.txt1=0x9EA8B8FF; p.txt2=0x788596FF;
    p.accent=0x2DD4BFFF; p.accentDim=0x1E8D7FFF; p.accentSoft=0x2DD4BF38; p.accentMid=0x2DD4BF66;
    p.good=0x2DD4BFFF; p.warn=0xF3C26BFF; p.bad=0xF58C6BFF;
    p.viewport0=0xF4F6F9FF; p.viewport1=0xEEF0F3FF;
    // host dark glass roles (ui_theme.cpp fillDark)
    p.glassBg=0x1E222BDE; p.glassBorder=0x3B465DCC; p.glassHighlight=0xFFFFFF17; p.glassShadow=0x00000066;
    p.vibrancyLightPrimary=0xE5EAF0FF; p.vibrancyLightSecondary=0xB8C2D0FF; p.vibrancyLightTertiary=0x8A96A8FF;
    p.vibrancyDarkPrimary=0x1E2332FF;  p.vibrancyDarkSecondary=0x3D4654FF;  p.vibrancyDarkTertiary=0x5C6678FF;
    return p;
}

inline Palette lightPalette() {
    Palette p{};
    p.bg0=0xF5F5F5FF; p.bg1=0xFFFFFFFF; p.bg2=0xFAFAFAFF; p.bg3=0xE8E8E8FF;
    p.hover=0xD4E4F7FF; p.active=0xBBD3F5FF; p.line=0xC7C7C7E6;
    p.txt0=0x1E2332FF; p.txt1=0x5B626FFF; p.txt2=0x8F959EFF;
    p.accent=0x0078D7FF; p.accentDim=0x005AA0FF; p.accentSoft=0x0078D72E; p.accentMid=0x0078D759;
    p.good=0x0A9230FF; p.warn=0xC88C00FF; p.bad=0xD41E18FF;
    p.viewport0=0xF4F6F9FF; p.viewport1=0xEEF0F3FF;
    // host light glass roles
    p.glassBg=0xF7F9FCEB; p.glassBorder=0x9EA6BAB8; p.glassHighlight=0xFFFFFF59; p.glassShadow=0x0000002E;
    p.vibrancyLightPrimary=0xF5F7FAFF; p.vibrancyLightSecondary=0xD8DEE8FF; p.vibrancyLightTertiary=0xA8B0BCFF;
    p.vibrancyDarkPrimary=0x1E2332FF;  p.vibrancyDarkSecondary=0x3D4654FF;  p.vibrancyDarkTertiary=0x5C6678FF;
    return p;
}

// 8pt spacing grid (logical px @1.0×; scale via theme::S() / theme::U()).
struct Space {
    float xs = 4.f;
    float sm = 8.f;
    float md = 16.f;
    float lg = 24.f;
    float xl = 32.f;
};

// SF-like type scale (logical px @1.0×).
struct TypeScale {
    float largeTitle = 22.f;
    float title      = 17.f;
    float headline   = 15.f;
    float body       = 13.f;
    float caption    = 11.f;
    float mono       = 12.f;
};

// Geometry tokens (logical px @1.0×; scaled once at apply).
struct Metrics {
    float titleBarH   = 38.f;
    float workspaceH  = 34.f;
    float toolbarH    = 76.f;
    float railW       = 48.f;
    float statusH     = 26.f;
    float bigBtn      = 52.f;
    float rounding    = 6.f;     // row pills
    float roundingLg  = 10.f;    // panels
    float grid        = 8.f;     // base spacing unit (was 4.f pre-F2)
    Space   space;
    TypeScale type;
};

// Row pill wash alphas (GLASS_DESIGN_SPEC §5 + host regression fix).
namespace rowWash {
    constexpr float idle     = 0.26f;
    constexpr float hover    = 0.32f;
    constexpr float selected = 0.42f;
}

// Motion tokens (seconds).
namespace motion {
    constexpr float micro    = 0.090f;
    constexpr float standard = 0.160f;
    constexpr float entrance = 0.220f;
}

inline float easeOut(float t){ t=t<0?0:(t>1?1:t); float u=1-t; return 1-u*u*u; }
inline float approach(float cur,float target,float dt,float tau=0.06f){
    float a = 1.f - std::exp(-dt/ (tau<1e-4f?1e-4f:tau));
    return cur + (target-cur)*a;
}

} // namespace theme
