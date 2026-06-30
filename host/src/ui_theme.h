// AI-CAD UI theme: named palette roles, two palettes (dark/teal + SolidWorks-light),
// runtime-switchable. applyTheme(Theme, scale) fills ImGuiStyle + the global palette;
// all panels read ONLY role fields — never hardcode ImVec4/ImU32 colors in panel code.
#pragma once
#include <cstdint>
#include <string>

#include "imgui.h"

namespace ui {

struct Fonts {
    ImFont* body = nullptr;    // Rubik Regular
    ImFont* medium = nullptr;  // Rubik Medium (labels/buttons)
    ImFont* h1 = nullptr;      // Rubik Bold (titles)
    ImFont* h2 = nullptr;      // Rubik SemiBold (section headers)
    ImFont* mono = nullptr;    // JetBrains Mono (code/console)
};

// Canonical theme identifiers.
enum class Theme { Dark, Light };

// ~40 named palette roles — the ONLY way panels should reference colors.
// All values are ImVec4-compatible floats (r,g,b,a in 0..1), or ImU32 for draw-list calls.
// Panels use the float[4] fields; for draw-list IM_COL32 use palette.toU32(field).
struct Palette {
    // ---- chrome ----
    float windowBg[4]      = {0.082f,0.090f,0.110f, 1};   // top-level window backgrounds
    float panelBg[4]       = {0.094f,0.106f,0.129f, 1};   // docked panel / child bg
    float headerBg[4]      = {0.110f,0.125f,0.157f, 1};   // menu bar / title bar
    float popupBg[4]       = {0.106f,0.118f,0.145f, 1};
    float border[4]        = {0.165f,0.184f,0.227f, 0.85f};
    float statusBg[4]      = {0.075f,0.086f,0.108f, 1};

    // ---- ribbon ----
    float ribbonBg[4]      = {0.110f,0.125f,0.157f, 1};
    float ribbonTabActive[4]= {0.094f,0.106f,0.129f, 1};  // active ribbon tab underline bg

    // ---- text ----
    float text[4]          = {0.898f,0.918f,0.945f, 1};   // primary text
    float textDim[4]       = {0.520f,0.560f,0.620f, 1};   // secondary / dim text
    float textDisabled[4]  = {0.471f,0.502f,0.549f, 1};

    // ---- accent ----
    float accent[4]        = {0.176f,0.831f,0.749f, 1};   // teal #2DD4BF
    float accentDim[4]     = {0.118f,0.553f,0.498f, 1};
    float accentSoft[4]    = {0.176f,0.831f,0.749f, 0.22f};
    float accentMid[4]     = {0.176f,0.831f,0.749f, 0.40f};

    // ---- interactive ----
    float button[4]        = {0.149f,0.176f,0.220f, 1};
    float buttonHovered[4] = {0.184f,0.224f,0.275f, 1};
    float buttonActive[4]  = {0.216f,0.259f,0.318f, 1};

    // ---- frame / input ----
    float frameBg[4]       = {0.129f,0.149f,0.184f, 1};
    float frameBgHovered[4]= {0.165f,0.192f,0.235f, 1};
    float frameBgActive[4] = {0.192f,0.224f,0.271f, 1};

    // ---- tree rows ----
    float treeRow[4]       = {0.000f,0.000f,0.000f, 0.0f};  // transparent (use panelBg)
    float treeRowAlt[4]    = {0.000f,0.000f,0.000f, 0.04f}; // very subtle stripe
    float treeRowSelected[4]= {0.176f,0.831f,0.749f, 0.18f};

    // ---- viewport ----
    // C25: the 3D viewport is light in BOTH themes (SolidWorks studio look).
    float viewportTop[4]   = {0.957f,0.965f,0.976f, 1};   // bg gradient top    (#F4F6F9)
    float viewportMid[4]   = {0.851f,0.867f,0.894f, 1};   // bg gradient mid    (#D9DDE4)
    float viewportBottom[4]= {0.933f,0.941f,0.953f, 1};   // bg floor glow      (#EEF0F3)
    float viewportEdge[4]  = {0.090f,0.100f,0.120f, 1};   // model feature-edge lines
    float partDefault[4]   = {0.776f,0.792f,0.847f, 1};   // default body color (#C6CAD8)
    float groundShadow[4]  = {0.200f,0.220f,0.260f, 0.35f}; // soft contact shadow (a = max)
    float overlayBg[4]     = {0.039f,0.055f,0.075f, 0.843f}; // pill overlays on 3D view
    float overlayBorder[4] = {0.176f,0.831f,0.749f, 0.275f};
    float overlayText[4]   = {0.886f,0.906f,0.941f, 0.941f};
    // W4-K: SolidWorks-style datum planes — subtle light-blue translucent fill + thin blue
    // outline (alpha in [3] is the base; the renderer scales it per emphasis state).
    float planeFill[4]     = {0.380f,0.620f,0.910f, 0.085f};
    float planeOutline[4]  = {0.290f,0.540f,0.870f, 0.700f};

    // ---- selection / hover ----
    float selectionHighlight[4] = {0.176f,0.831f,0.749f, 0.22f};
    float hoverHighlight[4]     = {0.176f,0.831f,0.749f, 0.40f};

    // ---- C24: orientation-triad axis colors (X/Y/Z) ----
    float axisX[4]         = {0.910f,0.380f,0.380f, 1};   // red
    float axisY[4]         = {0.470f,0.820f,0.450f, 1};   // green
    float axisZ[4]         = {0.420f,0.620f,0.960f, 1};   // blue

    // ---- C18 sketch mode ----
    float sketchUnder[4]   = {0.337f,0.612f,0.960f, 1};   // under-defined entity (SW-style blue)
    float sketchGrid[4]    = {0.520f,0.560f,0.620f, 0.18f}; // faint sketch grid lines
    // C40: ink drawn ON the 3D viewport (which is light in BOTH themes) — fully-defined
    // entities and dimension lines/text must be dark like SolidWorks, never the panel text
    // role (near-white in the dark theme = invisible on the white canvas).
    float sketchInk[4]     = {0.090f,0.100f,0.125f, 1};   // fully-defined entities, dim text
    float sketchDimLine[4] = {0.165f,0.184f,0.220f, 1};   // dim/extension lines + arrows
    // C41: relation-badge chips (SolidWorks-like): light green fill + darker green border;
    // the glyph itself is drawn with sketchInk (dark on the light canvas in both themes).
    float sketchRelBg[4]     = {0.550f,0.780f,0.550f, 0.90f};
    float sketchRelBorder[4] = {0.300f,0.520f,0.300f, 1};
    // C49: sketch inference ink (dashed alignment lines + snap markers). Amber so it reads on
    // the light canvas in both themes and never collides with the under-defined blue.
    float sketchInfer[4]     = {0.950f,0.650f,0.200f, 0.85f};

    // ---- status ----
    float statusOk[4]   = {0.176f,0.831f,0.749f, 1};   // same as accent
    float statusWarn[4] = {0.953f,0.761f,0.420f, 1};   // amber
    float statusErr[4]  = {0.961f,0.549f,0.420f, 1};   // red-orange

    // ---- console colors ----
    float consoleDefault[4]= {0.780f,0.820f,0.880f, 1};
    float consoleOk[4]     = {0.176f,0.831f,0.749f, 1};
    float consoleWarn[4]   = {0.953f,0.761f,0.420f, 1};
    float consoleErr[4]    = {0.961f,0.549f,0.420f, 1};
    float consoleDim[4]    = {0.620f,0.660f,0.720f, 1};

    // ---- code / diff ----
    float chipBg[4]        = {0.176f,0.831f,0.749f, 0.30f};
    float codeBg[4]        = {0.055f,0.065f,0.085f, 1};
    // NOTE: proseBg/stepCardBg are ≈ the Copilot pane's dark base (windowBg ~0.082) since the panes
    // went opaque-dark, so these cards now read by their BORDER, not fill contrast. If chat content
    // ever looks flat, lighten these ~0.03 above windowBg so the cards regain a visible surface.
    float proseBg[4]       = {0.085f,0.100f,0.130f, 1};
    float proseText[4]     = {0.700f,0.740f,0.800f, 1};
    float stepCardBg[4]    = {0.082f,0.094f,0.118f, 1};
    float planCardBg[4]    = {0.071f,0.102f,0.110f, 1};
    float setupWarnBg[4]   = {0.200f,0.130f,0.100f, 1};

    float diffDefault[4]   = {0.780f,0.820f,0.880f, 1};
    float diffHdr[4]       = {0.600f,0.660f,0.740f, 1};   // +++ / ---
    float diffHunk[4]      = {0.451f,0.741f,0.961f, 1};   // @@
    float diffAdd[4]       = {0.459f,0.859f,0.549f, 1};   // + lines
    float diffDel[4]       = {0.961f,0.502f,0.502f, 1};   // - lines

    // ---- fit readout ----
    float fitOk[4]         = {0.459f,0.859f,0.549f, 1};   // green
    float fitBad[4]        = {0.961f,0.549f,0.502f, 1};   // red
    float fitDim[4]        = {0.620f,0.660f,0.720f, 1};

    // ---- iteration badge ----
    float iterBuilt[4]     = {0.600f,0.780f,0.950f, 1};   // "built" in-progress badge

    // ---- chat ----
    float userBubbleBg[4]  = {0.176f,0.831f,0.749f, 0.07f};
    float userBubbleBorder[4]= {0.176f,0.831f,0.749f, 0.22f};
    float assistantName[4] = {0.780f,0.820f,0.900f, 1};
    float errSystem[4]     = {0.961f,0.600f,0.502f, 1};
    float errCad[4]        = {0.929f,0.800f,0.561f, 1};

    // ---- plan card accent bar ----
    float planAccentBar    = 150.0f;   // alpha 0..255 for IM_COL32 accent bar (float so we can lerp)

    // ---- dims text in plan card ----
    float planDims[4]      = {0.639f,0.722f,0.780f, 1};
    float planFeature[4]   = {0.600f,0.639f,0.700f, 1};

    // ---- parts list ----
    float partsDim[4]      = {0.620f,0.660f,0.720f, 1};

    // ---- window chrome buttons ----
    float captionFg        = 0.922f;   // greyscale for caption glyphs

    // ---- glass (floating widgets: tooltips, popovers, overlays) ----
    // glassBg:        main translucent fill — semi-opaque so the content behind bleeds through subtly.
    // glassBorder:    cooler/brighter border than the standard chrome border.
    // glassHighlight: white-tinted top-shine gradient + specular line (top inner edge).
    // glassShadow:    soft simulated drop-shadow beneath the panel.
    float glassBg[4]        = {0.118f, 0.133f, 0.167f, 0.87f};
    float glassBorder[4]    = {0.230f, 0.275f, 0.365f, 0.80f};
    float glassHighlight[4] = {1.000f, 1.000f, 1.000f, 0.09f};
    float glassShadow[4]    = {0.000f, 0.000f, 0.000f, 0.40f};

    // ---- tree (feature-manager rows: hover wash + section header label) ----
    // treeHover:   accent-tinted row hover wash (low alpha so text stays legible).
    // treeSection: muted label color for collapsible section headers.
    float treeHover[4]      = {0.176f, 0.831f, 0.749f, 0.10f};
    float treeSection[4]    = {0.620f, 0.660f, 0.720f, 1.0f};

    // ---- copilot blocks (chat thinking / action cards) ----
    // blockThinkBg:  thinking-block background — semi-transparent, darker than the card.
    // blockActionBg: action-card background — near-opaque so structured content reads clearly.
    float blockThinkBg[4]   = {0.071f, 0.082f, 0.106f, 0.90f};
    float blockActionBg[4]  = {0.094f, 0.110f, 0.141f, 0.96f};

    // Helper: convert float[4] to ImU32 (for draw-list calls).
    // Usage: palette.toU32(palette.accent)
    static inline uint32_t toU32(const float f[4]) {
        auto clamp = [](float v) -> uint8_t {
            int i = (int)(v * 255.0f + 0.5f);
            return (uint8_t)(i < 0 ? 0 : i > 255 ? 255 : i);
        };
        return ((uint32_t)clamp(f[3]) << 24) | ((uint32_t)clamp(f[2]) << 16) |
               ((uint32_t)clamp(f[1]) << 8)  |  (uint32_t)clamp(f[0]);
    }
    // Helper: build alpha-scaled copy of a role.
    static inline void withAlpha(const float src[4], float dst[4], float a) {
        dst[0]=src[0]; dst[1]=src[1]; dst[2]=src[2]; dst[3]=src[3]*a;
    }
};

// Fill ImGuiStyle + the global palette for the given theme.
// Must be called after ImGui::CreateContext() and whenever the theme is toggled.
void applyTheme(Theme t, float scale = 1.0f);

Fonts loadFonts(const std::string& assetsDir, float pxScale = 1.0f);

// Draw a glassmorphism panel background onto 'dl' covering [mn, mx].
// Layers (back to front): drop-shadow, translucent base fill, vertical inner-shine gradient,
// border, top specular line. 'rounding' is a logical dp value (multiplied by ui::uiScale
// internally, so 8.0f matches the PopupRounding set by applyTheme at any DPI).
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding = 8.0f);
// Alpha-scaled variant for animated widgets (e.g. toasts): `alpha` (0..1) multiplies every glass
// role's alpha before drawing, so a fading card dims all five layers coherently.
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, float alpha);

// Push/pop ImGuiStyle overrides for the BeginTooltip() + glassBackground() pattern.
// pushGlassTooltipStyle() sets PopupBg=transparent so ImGui's native rect fill does not
// compete with the custom-drawn glass layers. popGlassTooltipStyle() restores all pushed state.
// Callers of SetTooltip() do NOT need push/pop — ImGuiCol_PopupBg==glassBg globally.
// Usage pattern:
//   ui::pushGlassTooltipStyle();
//   if (ImGui::BeginTooltip()) {
//       ImVec2 p0 = ImGui::GetWindowPos();
//       ImVec2 p1 = p0 + ImGui::GetWindowSize();
//       ui::glassBackground(ImGui::GetWindowDrawList(), p0, p1);
//       /* content */
//       ImGui::EndTooltip();
//   }
//   ui::popGlassTooltipStyle();
void pushGlassTooltipStyle();
void popGlassTooltipStyle();

// Push/pop WindowBg=glassBg around a floating ImGui::Begin() window (translucent glass panel).
// Pair the calls immediately around Begin()/End().
void pushGlassWindowStyle();
void popGlassWindowStyle();

// Push/pop ChildBg=glassBg + Border=glassBorder + ChildRounding around a BeginChild() card.
// `rounding` is logical dp (scaled by uiScale internally). Pair around BeginChild()/EndChild().
void pushGlassChildStyle(float rounding = 8.0f);
void popGlassChildStyle();

// Subtle frosted pill behind a single text/icon row (FeatureManager, Copilot blocks). Keeps the
// panel itself maximally transparent while preserving row readability. `stripe` adds a faint
// alternating tint; `selected` draws a restrained accent card.
void glassRowBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding = 4.0f,
                        bool stripe = false, bool selected = false);

// Paint a row pill at the current cursor (full content width). Call immediately BEFORE the row's
// widgets so the pill sits behind text/icons.
void glassRowAtCursor(float rounding = 4.0f, bool stripe = false, bool selected = false,
                      float rowPadV = 5.0f);

// The active palette — read-only from panels.
extern Palette palette;

// The active theme — readable from any code that needs to branch on dark vs. light.
extern Theme activeTheme;

// The active UI content scale (1.0 at 100% DPI, 1.5 at 150%, etc). Set by applyTheme().
// Custom-drawn widgets (e.g. the ribbon) multiply their pixel geometry by this so it tracks
// the pre-scaled font atlas. Prefer this over ImGui::GetIO().FontGlobalScale, which the app
// leaves at 1.0 (fonts are scaled at load time, not via FontGlobalScale).
extern float uiScale;

}  // namespace ui
