// Theme runtime: palette/metrics/scale, ImGuiStyle mapping, liquid-glass draw helpers.
#pragma once
#include <cstdint>
#include <string>
#include "imgui.h"
#include "theme/tokens.h"

namespace theme {

enum class Font { Body, Label, H2, H1, Mono, Icon, _count };

struct Ctx {
    Palette pal;
    Metrics m;
    float   scale = 1.f;
    bool    dark  = true;
    bool    reduceMotion = false;
    ImFont* fonts[(int)Font::_count] = {};
};

Ctx&      ctx();
inline const Palette& pal() { return ctx().pal; }
inline float S() { return ctx().scale; }
inline float U(float n = 1.f) { return n * ctx().m.grid * ctx().scale; }
ImFont*   font(Font f);

void init(const std::string& resourcesDir, float scale, bool dark);
void apply();
void setDark(bool dark);

// ---- viewport backdrop (F1 Kawase blur; stub OK until wired) ----------------
struct ViewportBackdrop {
    std::uintptr_t textureId = 0;
    ImVec2 uvMin{0.f, 0.f};
    ImVec2 uvMax{1.f, 1.f};
    ImVec2 screenMin{0.f, 0.f};   // viewport image rect, absolute screen coords
    ImVec2 screenMax{0.f, 0.f};
    bool   flipV = true;          // GL bottom-left texture: v runs bottom-up
    bool valid() const { return textureId != 0 && screenMax.x > screenMin.x && screenMax.y > screenMin.y; }
};

void setViewportBackdrop(const ViewportBackdrop& backdrop);
const ViewportBackdrop& viewportBackdrop();

// Luminance bridge (viewport registers a sampler; theme consumes). Screen coords in, 0..1 out,
// negative => unavailable.
using LuminanceSampler = float (*)(ImVec2 mn, ImVec2 mx);
void  setLuminanceSampler(LuminanceSampler fn);
float sampleLuminance(ImVec2 mn, ImVec2 mx);   // -1.f when no sampler registered

// ---- adaptive contrast (GLASS_DESIGN_SPEC §5) ---------------------------------
struct AdaptiveSurface {
    ImVec4 tint;            // scrim color+alpha to draw over the blurred backdrop
    ImVec4 textPrimary, textSecondary, textTertiary;
    bool   darkText = false;   // true => dark-on-bright
    float  scrim = 0.f;        // extra contrast-boost alpha already folded into tint.w
};

AdaptiveSurface adaptiveSurface(float avgLuminance);

// Panel-surface ids MUST be window-independent (A1): stable hash across windows/TUs.
ImGuiID panelId(const char* label);

// Hysteretic + temporally smoothed per-panel adapter. Samples luminance under rect via the
// registered sampler. id = any stable ImGuiID. Safe to call every frame; GUI thread only.
const AdaptiveSurface& adaptivePanel(ImGuiID id, const ImRect& rect);

// Draw text with a contrast halo (1px offset soft shadow in the OPPOSITE tone). font may be null
// (current font). Use for text painted directly on the viewport (legend, HUD labels).
void glassText(ImDrawList* dl, ImFont* font, float size, ImVec2 pos,
               const AdaptiveSurface& s, const char* text, const char* textEnd = nullptr);

// ---- liquid glass (ported from host/ui_theme.*) --------------------------------
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding = 8.f);
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, float alpha);

void pushGlassTooltipStyle();
void popGlassTooltipStyle();
void pushGlassWindowStyle();
void popGlassWindowStyle();
void pushGlassChildStyle(float rounding = 8.f);
void popGlassChildStyle();

void glassRowBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding = 4.f,
                        bool stripe = false, bool selected = false,
                        float washAlpha = rowWash::idle);
void glassRowAtCursor(float rounding = 4.f, bool stripe = false, bool selected = false,
                      float rowPadV = 5.f);

// Frosted panel over viewport (blur when backdrop valid) or layered fallback.
// avgLuminance < 0 → skip adaptive tint (use palette defaults).
void glassPanel(ImDrawList* dl, const ImRect& rect, MaterialLevel level,
                float rounding = -1.f, float avgLuminance = -1.f, float alpha = 1.f);

// glassPanel overload that uses a precomputed surface (preferred for panels):
void glassPanel(ImDrawList* dl, const ImRect& rect, MaterialLevel level, float rounding,
                const AdaptiveSurface& surf, float alpha = 1.f);

} // namespace theme

#define TCOL(field)  theme::rgba(theme::pal().field)
#define TVEC(field)  theme::vec (theme::pal().field)
