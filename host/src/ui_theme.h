// AI-CAD UI theme: named palette roles, two palettes (dark/teal + SolidWorks-light),
// runtime-switchable. applyTheme(Theme, scale) fills ImGuiStyle + the global palette;
// all panels read ONLY role fields — never hardcode ImVec4/ImU32 colors in panel code.
#pragma once
#include <cstdint>
#include <string>

struct ImFont;

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
    float viewportTop[4]   = {0.170f,0.190f,0.230f, 1};   // renderer bg gradient top
    float viewportBottom[4]= {0.085f,0.095f,0.115f, 1};   // renderer bg gradient bottom
    float overlayBg[4]     = {0.039f,0.055f,0.075f, 0.843f}; // pill overlays on 3D view
    float overlayBorder[4] = {0.176f,0.831f,0.749f, 0.275f};
    float overlayText[4]   = {0.886f,0.906f,0.941f, 0.941f};

    // ---- selection / hover ----
    float selectionHighlight[4] = {0.176f,0.831f,0.749f, 0.22f};
    float hoverHighlight[4]     = {0.176f,0.831f,0.749f, 0.40f};

    // ---- C18 sketch mode ----
    float sketchUnder[4]   = {0.337f,0.612f,0.960f, 1};   // under-defined entity (SW-style blue)
    float sketchGrid[4]    = {0.520f,0.560f,0.620f, 0.18f}; // faint sketch grid lines

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

// Convenience overload: apply dark theme (matches old call sites in app.cpp).
inline void applyTheme(float scale = 1.0f) {
    applyTheme(Theme::Dark, scale);
}

Fonts loadFonts(const std::string& assetsDir, float pxScale = 1.0f);

// The active palette — read-only from panels.
extern Palette palette;

// The active theme — readable from any code that needs to branch on dark vs. light.
extern Theme activeTheme;

}  // namespace ui
