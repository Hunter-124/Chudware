// AI-CAD UI theme implementation.
// Two named palettes: Dark (original teal-on-dark brand) and Light (SolidWorks-style).
// applyTheme(Theme, scale) populates both ui::palette and ImGuiStyle from the chosen set.
// Panels read ONLY ui::palette — never hardcode color literals.
#include "ui_theme.h"

#include "imgui.h"
#include "imgui_internal.h"   // ImHashStr (panelId)

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <unordered_map>

namespace ui {

Palette palette;
Theme   activeTheme = Theme::Dark;
float   uiScale     = 1.0f;

// ---- small helper -------------------------------------------------------
static ImVec4 rgba(float r, float g, float b, float a = 1.0f) {
    return ImVec4(r, g, b, a);
}
static void set4(float dst[4], float r, float g, float b, float a = 1.0f) {
    dst[0]=r; dst[1]=g; dst[2]=b; dst[3]=a;
}
static ImVec4 fromRole(const float f[4]) { return ImVec4(f[0],f[1],f[2],f[3]); }

// =========================================================================
// Dark palette (original teal-on-dark, byte-for-byte where possible)
// =========================================================================
static void fillDark(Palette& p) {
    // chrome
    set4(p.windowBg,      0.082f,0.090f,0.110f, 1);
    set4(p.panelBg,       0.094f,0.106f,0.129f, 1);
    set4(p.headerBg,      0.110f,0.125f,0.157f, 1);
    set4(p.popupBg,       0.106f,0.118f,0.145f, 1);
    set4(p.border,        0.165f,0.184f,0.227f, 0.85f);
    set4(p.statusBg,      0.075f,0.086f,0.108f, 1);
    // ribbon
    set4(p.ribbonBg,      0.110f,0.125f,0.157f, 1);
    set4(p.ribbonTabActive,0.094f,0.106f,0.129f,1);
    // text
    set4(p.text,          0.898f,0.918f,0.945f, 1);
    set4(p.textDim,       0.620f,0.660f,0.720f, 1);   // brighter on glass rows over viewport bleed
    set4(p.textDisabled,  0.471f,0.502f,0.549f, 1);
    // accent  #2DD4BF
    set4(p.accent,        0.176f,0.831f,0.749f, 1);
    set4(p.accentDim,     0.118f,0.553f,0.498f, 1);
    set4(p.accentSoft,    0.176f,0.831f,0.749f, 0.22f);
    set4(p.accentMid,     0.176f,0.831f,0.749f, 0.40f);
    // interactive
    set4(p.button,        0.149f,0.176f,0.220f, 1);
    set4(p.buttonHovered, 0.184f,0.224f,0.275f, 1);
    set4(p.buttonActive,  0.216f,0.259f,0.318f, 1);
    // frame
    set4(p.frameBg,       0.129f,0.149f,0.184f, 1);
    set4(p.frameBgHovered,0.165f,0.192f,0.235f, 1);
    set4(p.frameBgActive, 0.192f,0.224f,0.271f, 1);
    // viewport — C25: light studio gradient in BOTH themes (SolidWorks look)
    set4(p.viewportTop,   0.957f,0.965f,0.976f, 1);   // #F4F6F9
    set4(p.viewportMid,   0.851f,0.867f,0.894f, 1);   // #D9DDE4
    set4(p.viewportBottom,0.933f,0.941f,0.953f, 1);   // #EEF0F3 floor glow
    set4(p.viewportEdge,  0.090f,0.100f,0.120f, 1);
    set4(p.partDefault,   0.776f,0.792f,0.847f, 1);   // #C6CAD8
    set4(p.groundShadow,  0.200f,0.220f,0.260f, 0.35f);
    set4(p.overlayBg,     0.039f,0.055f,0.075f, 0.843f);
    set4(p.overlayBorder, 0.176f,0.831f,0.749f, 0.275f);
    set4(p.overlayText,   0.886f,0.906f,0.941f, 0.941f);
    // datum planes (light-blue, SolidWorks look — viewport is light in both themes)
    set4(p.planeFill,    0.380f,0.620f,0.910f, 0.140f);   // C25: 0.085 was imperceptible
    set4(p.planeOutline, 0.290f,0.540f,0.870f, 0.700f);
    // selection
    set4(p.selectionHighlight, 0.176f,0.831f,0.749f, 0.22f);
    set4(p.hoverHighlight,     0.176f,0.831f,0.749f, 0.40f);
    // C24 orientation-triad axes
    set4(p.axisX, 0.910f,0.380f,0.380f, 1);
    set4(p.axisY, 0.470f,0.820f,0.450f, 1);
    set4(p.axisZ, 0.420f,0.620f,0.960f, 1);
    // in-viewport sim / flow / CAM overlay roles (values = the former viewport_panel literals)
    set4(p.simFixture, 0.18f,0.80f,0.36f, 0.45f);
    set4(p.simLoad,    0.95f,0.27f,0.20f, 0.45f);
    set4(p.simArrow,   0.95f,0.27f,0.20f, 1);
    set4(p.simGlyph,   0.10f,0.66f,0.30f, 1);
    set4(p.simMesh,    0.55f,0.74f,0.95f, 0.55f);
    set4(p.flowArrow,  0.28f,0.70f,0.98f, 1);
    set4(p.flowDomain, 0.45f,0.55f,0.68f, 0.55f);
    set4(p.flowInlet,  0.30f,0.80f,0.85f, 0.75f);
    set4(p.camRapid,   0.78f,0.24f,0.20f, 0.55f);
    set4(p.camLead,    0.31f,0.78f,0.47f, 0.95f);
    set4(p.camPlunge,  0.31f,0.71f,0.86f, 0.95f);
    set4(p.camCut,     0.35f,0.59f,0.94f, 1);
    set4(p.camArc,     0.59f,0.51f,0.94f, 1);
    // C18 sketch mode
    set4(p.sketchUnder, 0.337f,0.612f,0.960f, 1);
    set4(p.sketchGrid,  0.520f,0.560f,0.620f, 0.18f);
    // C40: dark sketch ink — the viewport canvas is light in both themes.
    set4(p.sketchInk,     0.090f,0.100f,0.125f, 1);
    set4(p.sketchDimLine, 0.165f,0.184f,0.220f, 1);
    // C41: relation-badge chips (light green fill + darker green border; glyph = sketchInk).
    set4(p.sketchRelBg,     0.550f,0.780f,0.550f, 0.90f);
    set4(p.sketchRelBorder, 0.300f,0.520f,0.300f, 1);
    // C49: inference ink (dashed alignment lines + snap markers) — amber on the light canvas.
    set4(p.sketchInfer,     0.950f,0.650f,0.200f, 0.85f);
    // status
    set4(p.statusOk,   0.176f,0.831f,0.749f, 1);
    set4(p.statusWarn, 0.953f,0.761f,0.420f, 1);
    set4(p.statusErr,  0.961f,0.549f,0.420f, 1);
    // console
    set4(p.consoleDefault,0.780f,0.820f,0.880f,1);
    set4(p.consoleOk,   0.176f,0.831f,0.749f, 1);
    set4(p.consoleWarn, 0.953f,0.761f,0.420f, 1);
    set4(p.consoleErr,  0.961f,0.549f,0.420f, 1);
    set4(p.consoleDim,  0.620f,0.660f,0.720f, 1);
    // code/diff
    set4(p.chipBg,     0.176f,0.831f,0.749f, 0.30f);
    set4(p.codeBg,     0.055f,0.065f,0.085f, 1);
    set4(p.proseBg,    0.085f,0.100f,0.130f, 1);
    set4(p.proseText,  0.780f,0.820f,0.880f, 1);
    set4(p.setupWarnBg,0.200f,0.130f,0.100f, 1);
    set4(p.diffDefault,0.780f,0.820f,0.880f,1);
    set4(p.diffHdr,    0.600f,0.660f,0.740f,1);
    set4(p.diffHunk,   0.451f,0.741f,0.961f,1);
    set4(p.diffAdd,    0.459f,0.859f,0.549f,1);
    set4(p.diffDel,    0.961f,0.502f,0.502f,1);
    // fit
    set4(p.fitOk,  0.459f,0.859f,0.549f,1);
    set4(p.fitBad, 0.961f,0.549f,0.502f,1);
    set4(p.fitDim, 0.620f,0.660f,0.720f,1);
    // iteration badge
    set4(p.iterBuilt, 0.600f,0.780f,0.950f,1);
    // chat
    set4(p.assistantName,   0.820f,0.860f,0.920f,1);
    set4(p.errSystem,       0.961f,0.600f,0.502f,1);
    set4(p.errCad,          0.929f,0.800f,0.561f,1);
    // misc
    p.planAccentBar = 150.0f;
    set4(p.planDims,    0.639f,0.722f,0.780f,1);
    set4(p.planFeature, 0.600f,0.639f,0.700f,1);
    set4(p.partsDim,    0.620f,0.660f,0.720f,1);
    p.captionFg = 0.922f;
    // glass (floating panels: tooltips, popovers, overlays)
    set4(p.glassBg,        0.118f, 0.133f, 0.167f, 0.87f);  // translucent dark-blue-gray
    set4(p.glassBorder,    0.230f, 0.275f, 0.365f, 0.80f);  // cool bright border
    set4(p.glassHighlight, 1.000f, 1.000f, 1.000f, 0.09f);  // subtle white top shine
    set4(p.glassShadow,    0.000f, 0.000f, 0.000f, 0.40f);  // drop shadow
    // tree (feature-manager rows)
    set4(p.treeHover,      0.176f, 0.831f, 0.749f, 0.10f);  // accent-tinted hover wash
    set4(p.treeSection,    0.620f, 0.660f, 0.720f, 1.0f);   // muted section-header label
    // copilot blocks (chat action cards)
    set4(p.blockActionBg,  0.094f, 0.110f, 0.141f, 0.96f);  // near-opaque action card
}

// =========================================================================
// Light palette (SolidWorks-style: near-white chrome, slate text, #0078D7 accent)
// =========================================================================
static void fillLight(Palette& p) {
    // SolidWorks-ish neutral palette:
    //   ribbon / toolbar:  #F0F0F0  (very light grey)
    //   panel bg:          #FFFFFF
    //   window bg:         #F5F5F5
    //   header / menu:     #E8E8E8
    //   text:              #1E2329 (near-black slate)
    //   selection:         #0078D7 (Windows blue)
    //   accent:            #1A8C7F (muted teal from icons contract)

    // chrome
    set4(p.windowBg,      0.961f,0.961f,0.961f, 1);   // #F5F5F5
    set4(p.panelBg,       1.000f,1.000f,1.000f, 1);   // #FFFFFF
    set4(p.headerBg,      0.910f,0.910f,0.910f, 1);   // #E8E8E8
    set4(p.popupBg,       0.980f,0.980f,0.980f, 1);   // #FAFAFA
    set4(p.border,        0.780f,0.780f,0.780f, 0.90f);
    set4(p.statusBg,      0.878f,0.878f,0.878f, 1);   // #E0E0E0
    // ribbon
    set4(p.ribbonBg,      0.941f,0.941f,0.941f, 1);   // #F0F0F0
    set4(p.ribbonTabActive,1.000f,1.000f,1.000f, 1);
    // text
    set4(p.text,          0.118f,0.137f,0.196f, 1);   // #1E2332 slate
    set4(p.textDim,       0.357f,0.384f,0.439f, 1);   // secondary grey (a touch darker = more legible)
    set4(p.textDisabled,  0.561f,0.580f,0.612f, 1);   // disabled (slightly stronger than before)
    // accent: use SW blue #0078D7 for selection, muted teal for accent highlights
    set4(p.accent,        0.000f,0.471f,0.843f, 1);   // #0078D7
    set4(p.accentDim,     0.000f,0.353f,0.631f, 1);   // darker blue
    set4(p.accentSoft,    0.000f,0.471f,0.843f, 0.18f);
    set4(p.accentMid,     0.000f,0.471f,0.843f, 0.35f);
    // interactive
    set4(p.button,        0.922f,0.922f,0.922f, 1);
    set4(p.buttonHovered, 0.831f,0.886f,0.969f, 1);   // light-blue tint
    set4(p.buttonActive,  0.733f,0.831f,0.961f, 1);
    // frame
    set4(p.frameBg,       0.949f,0.949f,0.949f, 1);
    set4(p.frameBgHovered,0.878f,0.918f,0.980f, 1);
    set4(p.frameBgActive, 0.800f,0.867f,0.961f, 1);
    // viewport — C25: same light studio gradient as dark theme (SolidWorks look)
    set4(p.viewportTop,   0.957f,0.965f,0.976f, 1);   // #F4F6F9
    set4(p.viewportMid,   0.851f,0.867f,0.894f, 1);   // #D9DDE4
    set4(p.viewportBottom,0.933f,0.941f,0.953f, 1);   // #EEF0F3 floor glow
    set4(p.viewportEdge,  0.090f,0.100f,0.120f, 1);
    set4(p.partDefault,   0.776f,0.792f,0.847f, 1);   // #C6CAD8
    set4(p.groundShadow,  0.200f,0.220f,0.260f, 0.35f);
    set4(p.overlayBg,     0.961f,0.961f,0.961f, 0.882f);
    set4(p.overlayBorder, 0.000f,0.471f,0.843f, 0.40f);
    set4(p.overlayText,   0.118f,0.137f,0.196f, 0.941f);
    // datum planes (same light-blue as dark theme — viewport gradient is identical)
    set4(p.planeFill,    0.380f,0.620f,0.910f, 0.140f);   // C25: 0.090 was imperceptible
    set4(p.planeOutline, 0.000f,0.471f,0.843f, 0.750f);
    // selection
    set4(p.selectionHighlight, 0.000f,0.471f,0.843f, 0.18f);
    set4(p.hoverHighlight,     0.000f,0.471f,0.843f, 0.35f);
    // in-viewport sim / flow / CAM overlay roles — saturated strokes carry over; the translucent
    // washes (mesh preview, domain box) darken slightly so they hold on the light canvas.
    set4(p.simFixture, 0.14f,0.62f,0.28f, 0.45f);
    set4(p.simLoad,    0.85f,0.20f,0.15f, 0.45f);
    set4(p.simArrow,   0.85f,0.18f,0.13f, 1);
    set4(p.simGlyph,   0.07f,0.52f,0.24f, 1);
    set4(p.simMesh,    0.25f,0.45f,0.75f, 0.60f);
    set4(p.flowArrow,  0.10f,0.48f,0.80f, 1);
    set4(p.flowDomain, 0.35f,0.42f,0.55f, 0.60f);
    set4(p.flowInlet,  0.10f,0.58f,0.64f, 0.80f);
    set4(p.camRapid,   0.70f,0.18f,0.15f, 0.55f);
    set4(p.camLead,    0.16f,0.60f,0.32f, 0.95f);
    set4(p.camPlunge,  0.13f,0.53f,0.68f, 0.95f);
    set4(p.camCut,     0.16f,0.40f,0.80f, 1);
    set4(p.camArc,     0.44f,0.34f,0.82f, 1);
    // C24 orientation-triad axes (slightly deeper on the light studio bg for contrast)
    set4(p.axisX, 0.800f,0.180f,0.180f, 1);
    set4(p.axisY, 0.180f,0.580f,0.220f, 1);
    set4(p.axisZ, 0.000f,0.380f,0.820f, 1);
    // C18 sketch mode (SW blue under-defined, darker grid on the light studio)
    set4(p.sketchUnder, 0.000f,0.471f,0.843f, 1);
    set4(p.sketchGrid,  0.420f,0.447f,0.502f, 0.22f);
    // C40: dark sketch ink (same in both themes — the canvas is always light).
    set4(p.sketchInk,     0.090f,0.100f,0.125f, 1);
    set4(p.sketchDimLine, 0.165f,0.184f,0.220f, 1);
    // C41: relation-badge chips (same in both themes — drawn on the light canvas).
    set4(p.sketchRelBg,     0.550f,0.780f,0.550f, 0.90f);
    set4(p.sketchRelBorder, 0.300f,0.520f,0.300f, 1);
    // C49: inference ink (same in both themes — drawn on the light canvas).
    set4(p.sketchInfer,     0.950f,0.650f,0.200f, 0.85f);
    // status
    set4(p.statusOk,   0.039f,0.573f,0.176f, 1);    // #0A9230 green
    set4(p.statusWarn, 0.784f,0.549f,0.000f, 1);    // #C88C00 amber
    set4(p.statusErr,  0.831f,0.118f,0.094f, 1);    // #D41E18 red
    // console (light bg console — keep text readable)
    set4(p.consoleDefault,0.118f,0.137f,0.196f,1);
    set4(p.consoleOk,   0.039f,0.573f,0.176f, 1);
    set4(p.consoleWarn, 0.784f,0.549f,0.000f, 1);
    set4(p.consoleErr,  0.831f,0.118f,0.094f, 1);
    set4(p.consoleDim,  0.420f,0.447f,0.502f, 1);
    // code/diff
    set4(p.chipBg,     0.000f,0.471f,0.843f, 0.22f);
    set4(p.codeBg,     0.933f,0.941f,0.957f, 1);    // #EEF0F4
    set4(p.proseBg,    0.949f,0.953f,0.965f, 1);
    set4(p.proseText,  0.235f,0.259f,0.318f, 1);
    set4(p.setupWarnBg,0.980f,0.949f,0.886f, 1);    // pale amber
    set4(p.diffDefault,0.118f,0.137f,0.196f,1);
    set4(p.diffHdr,    0.353f,0.400f,0.467f,1);
    set4(p.diffHunk,   0.000f,0.380f,0.706f,1);
    set4(p.diffAdd,    0.039f,0.490f,0.157f,1);
    set4(p.diffDel,    0.706f,0.118f,0.118f,1);
    // fit
    set4(p.fitOk,  0.039f,0.490f,0.157f,1);
    set4(p.fitBad, 0.706f,0.118f,0.118f,1);
    set4(p.fitDim, 0.420f,0.447f,0.502f,1);
    // iteration badge
    set4(p.iterBuilt, 0.100f,0.420f,0.780f,1);   // deeper blue on light bg
    // chat
    set4(p.assistantName,   0.118f,0.137f,0.196f,1);
    set4(p.errSystem,       0.706f,0.118f,0.118f,1);
    set4(p.errCad,          0.627f,0.412f,0.000f,1);
    // misc
    p.planAccentBar = 180.0f;
    set4(p.planDims,    0.200f,0.310f,0.502f,1);
    set4(p.planFeature, 0.310f,0.353f,0.431f,1);
    set4(p.partsDim,    0.420f,0.447f,0.502f,1);
    p.captionFg = 0.118f;
    // glass (light theme: frosted near-white surface)
    set4(p.glassBg,        0.970f, 0.975f, 0.988f, 0.92f);  // frosted near-white, slight blue tint
    set4(p.glassBorder,    0.620f, 0.650f, 0.730f, 0.72f);  // grey-blue border
    set4(p.glassHighlight, 1.000f, 1.000f, 1.000f, 0.35f);  // pronounced shine (2.2x => 0.77 for specular line, not blinding)
    set4(p.glassShadow,    0.000f, 0.000f, 0.000f, 0.18f);  // lighter shadow on light bg
    // tree (feature-manager rows)
    set4(p.treeHover,      0.000f, 0.471f, 0.843f, 0.08f);  // SW-blue hover wash (lower alpha)
    set4(p.treeSection,    0.357f, 0.384f, 0.439f, 1.0f);   // darker grey section-header label
    // copilot blocks (chat action cards)
    set4(p.blockActionBg,  0.930f, 0.940f, 0.960f, 0.96f);  // near-opaque action card
}

// =========================================================================
// Apply: fill ImGuiStyle from the chosen theme, then update ui::palette
// =========================================================================
void applyTheme(Theme t, float scale) {
    activeTheme = t;
    uiScale     = scale;   // expose the content scale to custom-drawn widgets (ribbon, etc.)

    // Step 1: fill the palette struct
    if (t == Theme::Light)
        fillLight(palette);
    else
        fillDark(palette);

    // Step 2: set ImGuiStyle. Reset first so runtime toggles and repair calls are
    // idempotent; otherwise ScaleAllSizes and any roles not explicitly overwritten
    // can inherit stale light/dark values.
    ImGuiStyle& s = ImGui::GetStyle();
    s = ImGuiStyle();
    if (t == Theme::Light)
        ImGui::StyleColorsLight(&s);
    else
        ImGui::StyleColorsDark(&s);

    // --- shape: rounded + roomy (same for both themes) ---
    s.WindowRounding    = 9.0f;
    s.ChildRounding     = 9.0f;
    s.FrameRounding     = 7.0f;
    s.PopupRounding     = 8.0f;
    s.ScrollbarRounding = 10.0f;
    s.GrabRounding      = 7.0f;
    s.TabRounding       = 8.0f;

    s.WindowPadding     = ImVec2(15, 13);
    s.FramePadding      = ImVec2(12, 7);
    s.ItemSpacing       = ImVec2(10, 9);
    s.ItemInnerSpacing  = ImVec2(8, 6);
    s.CellPadding       = ImVec2(8, 6);
    s.ScrollbarSize     = 12.0f;
    s.GrabMinSize       = 11.0f;

    s.WindowBorderSize  = 1.0f;
    s.ChildBorderSize   = 1.0f;
    s.PopupBorderSize   = 1.0f;
    s.FrameBorderSize   = (t == Theme::Light) ? 1.0f : 0.0f;
    s.TabBorderSize     = 0.0f;
    s.WindowTitleAlign         = ImVec2(0.02f, 0.5f);
    s.WindowMenuButtonPosition = ImGuiDir_None;
    s.AntiAliasedLines  = true;
    s.AntiAliasedFill   = true;
    s.DisabledAlpha     = 0.45f;

    // --- colors from palette roles ---
    ImVec4* c = s.Colors;
    const auto& p = palette;

    c[ImGuiCol_Text]                = fromRole(p.text);
    c[ImGuiCol_TextDisabled]        = fromRole(p.textDisabled);
    c[ImGuiCol_WindowBg]            = fromRole(p.windowBg);
    c[ImGuiCol_ChildBg]             = fromRole(p.panelBg);
    c[ImGuiCol_PopupBg]             = fromRole(p.glassBg);
    // T-G14: subwindow / popup OS-border. p.border is tuned for SEPARATORS (soft, low-contrast) and
    // read as "fully transparent" on floating windows — especially in light mode where a light-grey
    // border sits on a near-white window over the light viewport. Give the WINDOW/POPUP border its
    // own higher-contrast value (separators stay on the softer p.border below) so every floating
    // subwindow reads a crisp outline in BOTH themes. Not a new palette role (contract is
    // append-only); resolved inline from the theme.
    c[ImGuiCol_Border]              = (t == Theme::Light) ? rgba(0.55f, 0.58f, 0.64f, 1.0f)
                                                          : rgba(0.34f, 0.39f, 0.49f, 0.95f);
    c[ImGuiCol_BorderShadow]        = rgba(0,0,0,0);
    c[ImGuiCol_FrameBg]             = fromRole(p.frameBg);
    c[ImGuiCol_FrameBgHovered]      = fromRole(p.frameBgHovered);
    c[ImGuiCol_FrameBgActive]       = fromRole(p.frameBgActive);
    // Floating-window title bar chrome. Was fully transparent so ImGui title text sat on
    // the frosted body (BOM / Profiler / other Begin()-with-title windows: text bleeds
    // through, chrome looks missing). headerBg is the palette role for menu/title bars
    // and is fully opaque in both themes — distinct from glass body fill. Pinned
    // FeatureManager + Copilot columns use NoTitleBar|NoBackground and never sample
    // these colors (they paint their own glass over a transparent WindowBg locally).
    c[ImGuiCol_TitleBg]             = fromRole(p.headerBg);
    c[ImGuiCol_TitleBgActive]       = fromRole(p.headerBg);
    c[ImGuiCol_TitleBgCollapsed]    = fromRole(p.headerBg);
    c[ImGuiCol_MenuBarBg]           = fromRole(p.headerBg);
    c[ImGuiCol_ScrollbarBg]         = rgba(0,0,0,0);
    c[ImGuiCol_ScrollbarGrab]       = fromRole(p.button);
    c[ImGuiCol_ScrollbarGrabHovered]= fromRole(p.buttonHovered);
    c[ImGuiCol_ScrollbarGrabActive] = fromRole(p.accentMid);
    c[ImGuiCol_CheckMark]           = fromRole(p.accent);
    c[ImGuiCol_SliderGrab]          = fromRole(p.accent);
    c[ImGuiCol_SliderGrabActive]    = fromRole(p.accentDim);
    c[ImGuiCol_Button]              = fromRole(p.button);
    c[ImGuiCol_ButtonHovered]       = fromRole(p.buttonHovered);
    c[ImGuiCol_ButtonActive]        = fromRole(p.buttonActive);
    c[ImGuiCol_Header]              = fromRole(p.accentSoft);
    c[ImGuiCol_HeaderHovered]       = fromRole(p.accentMid);
    c[ImGuiCol_HeaderActive]        = fromRole(p.accentMid);
    c[ImGuiCol_Separator]           = fromRole(p.border);
    c[ImGuiCol_SeparatorHovered]    = fromRole(p.accentMid);
    c[ImGuiCol_SeparatorActive]     = fromRole(p.accent);
    c[ImGuiCol_ResizeGrip]          = fromRole(p.accentSoft);
    c[ImGuiCol_ResizeGripHovered]   = fromRole(p.accentMid);
    c[ImGuiCol_ResizeGripActive]    = fromRole(p.accent);
    c[ImGuiCol_Tab]                 = fromRole(p.windowBg);
    c[ImGuiCol_TabHovered]          = fromRole(p.accentMid);
    c[ImGuiCol_TabActive]           = fromRole(p.button);
    c[ImGuiCol_TabUnfocused]        = fromRole(p.panelBg);
    c[ImGuiCol_TabUnfocusedActive]  = fromRole(p.frameBg);
    c[ImGuiCol_DockingPreview]      = fromRole(p.accentMid);
    c[ImGuiCol_DockingEmptyBg]      = fromRole(p.windowBg);
    c[ImGuiCol_PlotLines]           = fromRole(p.accent);
    c[ImGuiCol_PlotHistogram]       = fromRole(p.accent);
    c[ImGuiCol_TextSelectedBg]      = fromRole(p.accentSoft);
    c[ImGuiCol_NavHighlight]        = fromRole(p.accent);
    c[ImGuiCol_ModalWindowDimBg]    = (t == Theme::Light)
                                      ? rgba(0.1f, 0.1f, 0.1f, 0.45f)
                                      : rgba(0.02f, 0.027f, 0.039f, 0.65f);

    if (scale != 1.0f) s.ScaleAllSizes(scale);
}

// =========================================================================
// Glassmorphism helpers (consumed by feature-manager, copilot, ribbon, etc.)
// =========================================================================
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, float alpha) {
    const Palette& p = palette;
    const float s = uiScale;
    const float r = rounding * s;
    // Alpha-scaled role -> ImU32 (multiplies the role's own alpha by `alpha`, then by `mul`).
    auto A = [&](const float c[4], float mul) -> uint32_t {
        float t[4]; Palette::withAlpha(c, t, mul * alpha); return Palette::toU32(t);
    };

    // 1. Drop shadow: two stacked rects to fake a blurred shadow.
    {
        const float off  = 4.0f * s;
        const float off2 = 2.5f * s;
        dl->AddRectFilled(
            ImVec2(mn.x + off2 * 0.4f, mn.y + off2),
            ImVec2(mx.x + off,         mx.y + off),
            A(p.glassShadow, 0.35f), r + off * 0.5f);
        dl->AddRectFilled(
            ImVec2(mn.x + 1.0f * s, mn.y + 2.0f * s),
            ImVec2(mx.x + off2,     mx.y + off2),
            A(p.glassShadow, 1.0f), r + 0.5f * s);
    }

    // 2. Base translucent fill.
    dl->AddRectFilled(mn, mx, A(p.glassBg, 1.0f), r);

    // 3. Inner vertical shine gradient — top 35% of height, glassHighlight -> transparent.
    {
        const float shineH = (mx.y - mn.y) * 0.35f;
        const ImVec2 shineMx(mx.x, mn.y + shineH);
        const uint32_t top = A(p.glassHighlight, 1.0f);
        const uint32_t btm = top & 0x00FFFFFFu;   // same RGB, alpha=0 (toU32 is ABGR: bits 24-31 = A)
        dl->AddRectFilledMultiColor(mn, shineMx, top, top, btm, btm);
    }

    // 4. Border.
    dl->AddRect(mn, mx, A(p.glassBorder, 1.0f), r, 0, 1.5f * s);

    // 5. Top specular line — 1.5 px inside the top border arc.
    {
        const float lineY = mn.y + 1.5f * s;
        const float inset = r * 0.6f;   // stay clear of the rounded corners
        dl->AddLine(
            ImVec2(mn.x + inset, lineY),
            ImVec2(mx.x - inset, lineY),
            A(p.glassHighlight, 2.2f), 1.5f * s);
    }
}

void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding) {
    glassBackground(dl, mn, mx, rounding, 1.0f);
}

void glassCardWash(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding,
                   const float tint[3], float topAlpha, float bottomAlphaScale) {
    const float rnd = rounding * ui::uiScale;
    glassBackground(dl, mn, mx, rounding);   // frosted translucent base + shine
    const ImU32 top = ImGui::GetColorU32(ImVec4(tint[0], tint[1], tint[2], topAlpha));
    const ImU32 bot = ImGui::GetColorU32(ImVec4(tint[0], tint[1], tint[2], topAlpha * bottomAlphaScale));
    // The wash must cover the FULL rounded card (corners included): a rounded base
    // (owns every corner at the faint bottom colour), a rounded-top strip so the top
    // corners get the bright colour, then a full-width vertical fade in the interior.
    dl->AddRectFilled(mn, mx, bot, rnd);
    dl->AddRectFilled(mn, ImVec2(mx.x, mn.y + rnd), top, rnd, ImDrawFlags_RoundCornersTop);
    if (mx.y - rnd > mn.y + rnd)
        dl->AddRectFilledMultiColor(ImVec2(mn.x, mn.y + rnd), ImVec2(mx.x, mx.y - rnd),
                                    top, top, bot, bot);
}

void pushGlassTooltipStyle() {
    const float s = uiScale;
    // Suppress ImGui's native PopupBg rect fill so glassBackground() layers are not double-filled.
    ImGui::PushStyleColor(ImGuiCol_PopupBg, ImVec4(0.0f, 0.0f, 0.0f, 0.0f));
    // Compact padding and matching rounding for tooltip windows.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,  ImVec2(10.0f * s, 8.0f * s));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding,  8.0f * s);
}

void popGlassTooltipStyle() {
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(1);
}

void pushGlassWindowStyle() {
    const Palette& p = palette;
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
                          ImVec4(p.glassBg[0], p.glassBg[1], p.glassBg[2], p.glassBg[3]));
}

void popGlassWindowStyle() { ImGui::PopStyleColor(1); }

void pushGlassChildStyle(float rounding) {
    const Palette& p = palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
                          ImVec4(p.glassBg[0], p.glassBg[1], p.glassBg[2], p.glassBg[3]));
    ImGui::PushStyleColor(ImGuiCol_Border,
                          ImVec4(p.glassBorder[0], p.glassBorder[1], p.glassBorder[2], p.glassBorder[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, rounding * uiScale);
}

void popGlassChildStyle() {
    ImGui::PopStyleVar(1);
    ImGui::PopStyleColor(2);
}

void glassRowBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, bool stripe,
                        bool selected, float washAlpha) {
    const Palette& p = palette;
    const float s = uiScale;
    const float r = rounding * s;
    auto A = [&](const float c[4], float mul) -> uint32_t {
        float t[4]; Palette::withAlpha(c, t, mul); return Palette::toU32(t);
    };
    const float inset = 2.0f * s;
    ImVec2 pillMin(mn.x + inset, mn.y + inset);
    ImVec2 pillMax(mx.x - inset, mx.y - inset);
    // Frosted row wash — strong enough for legible text over bright viewport bleed-through.
    float wash[4]; Palette::withAlpha(p.glassBg, wash, washAlpha);
    dl->AddRectFilled(pillMin, pillMax, Palette::toU32(wash), r);
    // Faint border helps row pills read on a light canvas without a full panel wash.
    if (washAlpha > 0.08f) {
        float edge[4]; Palette::withAlpha(p.glassBorder, edge, washAlpha * 0.55f);
        dl->AddRect(pillMin, pillMax, Palette::toU32(edge), r, 0, std::max(0.5f, 0.75f * s));
    }
    if (stripe) {
        float stripeCol[4]; Palette::withAlpha(p.glassHighlight, stripeCol, 0.35f);
        dl->AddRectFilled(pillMin, pillMax, Palette::toU32(stripeCol), r);
    }
    if (selected) {
        // Selection is now a thin GLOW BORDER (no fill slab, no left accent bar) — see
        // glassSelectionGlow. A faint accent halo keeps the row legible without a heavy card.
        glassSelectionGlow(dl, mn, mx, rounding, p.accent);   // outer rect; glow self-insets to match
    }
}

void glassSelectionGlow(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, const float accent[4]) {
    const float s = uiScale;
    const float r = rounding * s;
    // Self-inset by the SAME 2dp glassRowBackground/coloredRowWash use, so the glow border sits
    // exactly on the pill edge (callers pass the identical OUTER rect to all three). Without this the
    // glow sat 2px outside the frosted base and read as a doubled vertical edge on the left corner.
    const float inset = 2.0f * s;
    mn = ImVec2(mn.x + inset, mn.y + inset);
    mx = ImVec2(mx.x - inset, mx.y - inset);
    if (mx.x <= mn.x || mx.y <= mn.y) return;
    // Crisp accent border on the pill edge.
    float edge[4]; Palette::withAlpha(accent, edge, 0.92f);
    dl->AddRect(mn, mx, Palette::toU32(edge), r, 0, std::max(1.0f, 1.3f * s));
    // Soft glow = thin 1px rings stepping INWARD, each fainter. One px per ring (concentric radii
    // r-1, r-2, …) so the rings tile without overlapping — the halo stays EVEN around the rounded
    // corners instead of piling alpha onto the short corner arc (the old stacked wide strokes read
    // as a heavy blob exactly where the curve starts). Inward (not outward) so it never bleeds into
    // a neighbouring packed row.
    const int rings = std::max(2, (int)std::lround(3.0f * s));
    for (int i = 1; i <= rings; ++i) {
        const float e = (float)i;
        if (mx.x - e <= mn.x + e || mx.y - e <= mn.y + e) break;
        float g[4]; Palette::withAlpha(accent, g, 0.16f * (float)(rings - i + 1) / (float)rings);
        dl->AddRect(ImVec2(mn.x + e, mn.y + e), ImVec2(mx.x - e, mx.y - e),
                    Palette::toU32(g), std::max(0.0f, r - e), 0, std::max(1.0f, 1.0f * s));
    }
}

void coloredRowWash(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding,
                    const float tint[3], float strength, bool selected) {
    const float s = uiScale;
    const float rr = rounding * s;
    // Self-inset by the SAME 2dp glassRowBackground uses, so the wash rect is always concentric with
    // the frosted base drawn underneath it (callers pass the identical outer rect to both). This
    // removes the seam where a non-nested corner of the base peeked out from under the wash.
    const ImVec2 omn = mn, omx = mx;   // outer rect, handed to the glow (which self-insets)
    const float inset = 2.0f * s;
    mn = ImVec2(mn.x + inset, mn.y + inset);
    mx = ImVec2(mx.x - inset, mx.y - inset);
    if (mx.x <= mn.x || mx.y <= mn.y) return;

    auto dye = [&](float a) {
        return ImGui::ColorConvertFloat4ToU32(ImVec4(tint[0], tint[1], tint[2], a));
    };
    // Translucent left->right tint over the frosted glass, bounded to the rounded pill: bright at
    // the left edge (`strength`), fading to a faint right edge (~15% of strength, min 0.02).
    // Same class of fix as ribbon_vGradRounded: never draw a rounded cap of width==radius
    // (ImGui clamps corner radius to half the short side → square corner pips) and never let
    // square-cornered AddRectFilledMultiColor poke past the rounded outline.
    const float aL = strength;
    const float aR = std::max(0.02f, strength * 0.15f);
    const float W  = mx.x - mn.x;
    const float H  = mx.y - mn.y;
    const float rnd = ImClamp(rr, 0.0f, ImMin(W, H) * 0.5f);

    // Full rounded base in the faint (right) colour — owns every corner.
    dl->AddRectFilled(mn, mx, dye(aR), rnd);

    // Bright left strip. Width ≥ 2·rnd so ImGui does not clamp the corner radius into
    // square corner pips (width==rnd was the artifact source).
    const float leftW = ImMax(2.0f * rnd, 1.0f);
    if (W > leftW + rnd) {
        const float tJoin = leftW / W;
        const ImU32 cLeft = dye(aL);
        const ImU32 cJoin = dye(aL * (1.0f - tJoin) + aR * tJoin);
        const ImU32 cRight = dye(aR);

        dl->AddRectFilled(mn, ImVec2(mn.x + leftW, mx.y), cLeft, rnd,
                          ImDrawFlags_RoundCornersLeft);

        // Interior L→R fade. AddRectFilledMultiColor is SQUARE-cornered — keep it
        // strictly inside the rounded outline (below the top arc, above the bottom
        // arc, right of the left strip, left of the right radius).
        const float x0 = mn.x + leftW;
        const float x1 = mx.x - rnd;
        const float y0 = mn.y + rnd;
        const float y1 = mx.y - rnd;
        if (x1 > x0 && y1 > y0) {
            dl->AddRectFilledMultiColor(ImVec2(x0, y0), ImVec2(x1, y1),
                                        cJoin, cRight, cRight, cJoin);
        }
    } else {
        // Too narrow for strip+band: single bright rounded fill.
        dl->AddRectFilled(mn, mx, dye(aL), rnd);
    }

    if (selected) {
        const float glow[4] = {tint[0], tint[1], tint[2], 1.0f};
        glassSelectionGlow(dl, omn, omx, rounding, glow);   // outer rect; glow self-insets to match
    }
}

void glassRowAtCursor(float rounding, bool stripe, bool selected, float rowPadV) {
    const float s = uiScale;
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    float availW = ImGui::GetContentRegionAvail().x;
    const float rowH = ImGui::GetTextLineHeight() + rowPadV * s;
    ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
    glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, rounding, stripe, selected);
}

// =========================================================================
// Font loading (unchanged)
// =========================================================================
// Glyphs covered by Rubik / JetBrains Mono (the body faces).
// Latin-1 (incl. ° U+00B0, Ø U+00D8), general punctuation, superscripts/subscripts, bullet.
// NOTE: arrows (0x2190-0x21FF) and geometric-shapes (0x25A0-0x25FF) are intentionally NOT
// requested — Rubik does not ship them, so leaving them out keeps the atlas small and avoids
// allocating notdef boxes. CAD-specific math/shape glyphs come from CADSymbols below.
static const ImWchar* uiGlyphRanges() {
    static const ImWchar r[] = {
        0x0020, 0x00FF,
        0x2010, 0x2027,
        0x2070, 0x209F,
        0,
    };
    return r;
}

// CAD-specific symbols sourced from CADSymbols.ttf (merged over the body face).
// Codepoints: ∅ U+2205, ∥ U+2225, ≡ U+2261, ⊕ U+2295, ⊙ U+2299, ⊥ U+22A5,
// ⌀ U+2300, ◆ U+25C6, ◇ U+25C7. Keep this in sync with the font's cmap.
static const ImWchar* cadSymbolRanges() {
    static const ImWchar r[] = {
        0x2205, 0x2205,
        0x2225, 0x2225,
        0x2261, 0x2261,
        0x2295, 0x2295,
        0x2299, 0x2299,
        0x22A5, 0x22A5,
        0x2300, 0x2300,
        0x25C6, 0x25C7,
        0,
    };
    return r;
}

static ImFont* addFont(ImGuiIO& io,
                       const std::string& path, float px, ImFont* fallback,
                       const std::string& cadSymPath /*may be empty*/) {
    ImFont* f = io.Fonts->AddFontFromFileTTF(path.c_str(), px, nullptr, uiGlyphRanges());
    if (!f) return fallback;
    if (!cadSymPath.empty()) {
        ImFontConfig merge; merge.MergeMode = true; merge.PixelSnapH = true;
        // Match the host face's vertical metrics so merged glyphs sit on the same baseline.
        io.Fonts->AddFontFromFileTTF(cadSymPath.c_str(), px, &merge, cadSymbolRanges());
    }
    return f;
}

Fonts loadFonts(const std::string& assetsDir, float pxScale) {
    ImGuiIO& io = ImGui::GetIO();
    const std::string fd  = assetsDir + "/fonts/";  // '/' works on Windows + POSIX for file I/O
    const std::string cad = fd + "CADSymbols.ttf";
    Fonts f;
    ImFont* def = io.Fonts->AddFontDefault();
    f.body   = addFont(io, fd + "Rubik-Regular.ttf",       17.5f * pxScale, def,    cad);
    f.medium = addFont(io, fd + "Rubik-Medium.ttf",        17.5f * pxScale, f.body, cad);
    f.h1     = addFont(io, fd + "Rubik-Bold.ttf",          30.0f * pxScale, f.body, cad);
    f.h2     = addFont(io, fd + "Rubik-SemiBold.ttf",      19.0f * pxScale, f.body, cad);
    f.mono   = addFont(io, fd + "JetBrainsMono-Regular.ttf",15.5f * pxScale, f.body, cad);
    io.Fonts->Build();
    io.FontDefault = f.body;
    return f;
}

// =========================================================================
// Liquid Glass v2 — adaptive contrast + backdrop-projected glass panels
// Ported from ui/imgui/src/theme/theme.cpp (see local://glass_host.md for the host contract).
// Vibrancy constants match ui/imgui/src/theme/tokens.h verbatim: theme-INDEPENDENT (identical
// values regardless of activeTheme) — only the SAMPLED LUMINANCE picks light-on-dark vs.
// dark-on-bright text.
// =========================================================================
namespace {
constexpr unsigned int kVibrancyLightPrimary   = 0xF5F7FAFF;
constexpr unsigned int kVibrancyLightSecondary = 0xD8DEE8E6;
constexpr unsigned int kVibrancyLightTertiary  = 0xA8B0BCCC;
constexpr unsigned int kVibrancyDarkPrimary    = 0x14181FFF;
constexpr unsigned int kVibrancyDarkSecondary  = 0x39424EE6;
constexpr unsigned int kVibrancyDarkTertiary   = 0x5C6678CC;
constexpr float        kAdaptiveTau            = 0.24f;  // luminance smoothing tau (s)
constexpr float        kDefaultPanelRounding   = 10.0f;  // used when glassPanel's rounding < 0

// ---- Colored liquid glass (v2.1): dynamic-color tuning ------------------------------------
constexpr float kDynHueOffsetDeg   = 180.f;  // tint = complement of the backdrop hue
constexpr float kDynMinBackdropSat = 0.12f;  // below: backdrop is neutral, keep the user tint
constexpr float kDynSatBase = 0.30f, kDynSatGain = 0.45f;   // tint sat follows backdrop chroma
constexpr float kDynTintStrength   = 0.35f;  // floor tint mix while dynamic color is steering
constexpr float kDynVarBoostGain = 2.2f, kDynVarBoostMax = 0.35f;  // busy backdrop => stronger scrim/halo
constexpr float kDynHueTau = 0.60f;          // hue smoothing tau (s) — slower than kAdaptiveTau

ImVec4 hexVec(unsigned int hex) {
    return ImVec4(((hex >> 24) & 0xFF) / 255.f, ((hex >> 16) & 0xFF) / 255.f,
                  ((hex >> 8) & 0xFF) / 255.f, (hex & 0xFF) / 255.f);
}

float glassLevelTintAlpha(GlassLevel level) {
    switch (level) {
    case GlassLevel::UltraThin: return 0.12f;
    case GlassLevel::Thin:      return 0.22f;
    case GlassLevel::Regular:   return 0.35f;
    case GlassLevel::Thick:     return 0.52f;
    }
    return 0.35f;
}

// RGB from a palette role, alpha set EXPLICITLY (the role's own baked-in alpha is ignored) —
// mirrors ui/imgui/src/theme/tokens.h's roleU32A().
uint32_t roleAlphaU32(const float role[4], float alpha) {
    float t[4] = {role[0], role[1], role[2], alpha};
    return Palette::toU32(t);
}

float approach(float cur, float target, float dt, float tau) {
    const float a = 1.f - std::exp(-dt / (tau < 1e-4f ? 1e-4f : tau));
    return cur + (target - cur) * a;
}

ViewportBackdropInfo gViewportBackdrop;
LuminanceSampler     gLuminanceSampler = nullptr;
BackdropStatsSampler gBackdropStatsSampler = nullptr;

// v2.1 active glass config + eagerly-resolved per-group styles. Default-constructed
// GlassStyleResolved == the default config's resolution, so every helper is safe to call
// before the first setGlassConfig (first-frame order independence). GUI thread only.
GlassConfig        gGlassConfig;
GlassStyleResolved gGlassResolved[(size_t)GlassGroup::Count];

bool sameGlassStyle(const GlassStyle& a, const GlassStyle& b) {
    return a.tintHue == b.tintHue && a.tintSat == b.tintSat &&
           a.tintStrength == b.tintStrength && a.glassLevel == b.glassLevel &&
           a.opacityMul == b.opacityMul && a.rounding == b.rounding &&
           a.textContrastBoost == b.textContrastBoost && a.dynamicColor == b.dynamicColor;
}

float clampF(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
float wrapHue(float h) { h = std::fmod(h, 360.f); return h < 0.f ? h + 360.f : h; }
}  // namespace

void setViewportBackdrop(const ViewportBackdropInfo& b) { gViewportBackdrop = b; }
const ViewportBackdropInfo& viewportBackdrop() { return gViewportBackdrop; }

void setLuminanceSampler(LuminanceSampler fn) { gLuminanceSampler = fn; }
float sampleLuminance(ImVec2 mn, ImVec2 mx) {
    return gLuminanceSampler ? gLuminanceSampler(mn, mx) : -1.f;
}

void setBackdropStatsSampler(BackdropStatsSampler fn) { gBackdropStatsSampler = fn; }
bool sampleBackdropStats(ImVec2 mn, ImVec2 mx, BackdropStats& out) {
    return gBackdropStatsSampler ? gBackdropStatsSampler(mn, mx, out) : false;
}

// ---- v2.1 colored glass config (see ui_theme.h GlassStyle for sentinel semantics) ----------
void setGlassConfig(const GlassConfig& cfg) {
    // Idempotent early-out so the frame loop can push Settings every frame for free.
    bool same = true;
    for (size_t gi = 0; gi < (size_t)GlassGroup::Count; ++gi)
        if (!sameGlassStyle(cfg.group[gi], gGlassConfig.group[gi])) { same = false; break; }
    static bool resolvedOnce = false;
    if (same && resolvedOnce) return;
    resolvedOnce = true;
    gGlassConfig = cfg;

    // Resolve Global first: sentinels -> built-in defaults (dynamic color ON out of the box).
    {
        const GlassStyle& s = cfg.group[(size_t)GlassGroup::Global];
        GlassStyleResolved& r = gGlassResolved[(size_t)GlassGroup::Global];
        r.tintHue           = s.tintHue      < 0.f ? 210.f : wrapHue(s.tintHue);
        r.tintSat           = s.tintSat      < 0.f ? 0.f   : clampF(s.tintSat, 0.f, 1.f);
        r.tintStrength      = s.tintStrength < 0.f ? 0.f   : clampF(s.tintStrength, 0.f, 1.f);
        r.glassLevel        = (s.glassLevel >= 0 && s.glassLevel <= 3) ? s.glassLevel : -1;
        r.opacityMul        = s.opacityMul < 0.f ? 1.f  : clampF(s.opacityMul, 0.25f, 2.f);
        r.rounding          = s.rounding   < 0.f ? -1.f : clampF(s.rounding, 0.f, 24.f);
        r.textContrastBoost = s.textContrastBoost < 0.f ? 0.f
                                                        : clampF(s.textContrastBoost, 0.f, 1.f);
        r.dynamicColor      = (s.dynamicColor < 0) ? true : (s.dynamicColor == 1);
    }
    // Other groups: any field still at its sentinel takes Global's RESOLVED value.
    const GlassStyleResolved& g0 = gGlassResolved[(size_t)GlassGroup::Global];
    for (size_t gi = 1; gi < (size_t)GlassGroup::Count; ++gi) {
        const GlassStyle& s = cfg.group[gi];
        GlassStyleResolved& r = gGlassResolved[gi];
        r.tintHue           = s.tintHue      < 0.f ? g0.tintHue      : wrapHue(s.tintHue);
        r.tintSat           = s.tintSat      < 0.f ? g0.tintSat      : clampF(s.tintSat, 0.f, 1.f);
        r.tintStrength      = s.tintStrength < 0.f ? g0.tintStrength : clampF(s.tintStrength, 0.f, 1.f);
        r.glassLevel        = (s.glassLevel >= 0 && s.glassLevel <= 3) ? s.glassLevel : g0.glassLevel;
        r.opacityMul        = s.opacityMul < 0.f ? g0.opacityMul : clampF(s.opacityMul, 0.25f, 2.f);
        r.rounding          = s.rounding   < 0.f ? g0.rounding   : clampF(s.rounding, 0.f, 24.f);
        r.textContrastBoost = s.textContrastBoost < 0.f ? g0.textContrastBoost
                                                        : clampF(s.textContrastBoost, 0.f, 1.f);
        r.dynamicColor      = (s.dynamicColor < 0) ? g0.dynamicColor : (s.dynamicColor == 1);
    }
}

const GlassConfig& glassConfig() { return gGlassConfig; }

const GlassStyleResolved& glassStyleResolved(GlassGroup g) {
    const size_t i = (size_t)g;
    return gGlassResolved[i < (size_t)GlassGroup::Count ? i : (size_t)GlassGroup::Global];
}

ImGuiID panelId(const char* label) { return ImHashStr(label); }

// Shared core for adaptiveSurface()/adaptivePanel(): builds the surface for one luminance
// sample under an EXPLICIT resolved style (adaptivePanel substitutes a dynamic-color style).
// Luminance still picks the base tone + text; the style's hue/sat only colorize the scrim, and
// its opacity/contrast knobs scale it — a default style is a provable no-op vs. the old code.
static AdaptiveSurface adaptiveSurfaceStyled(float avgLuminance, GlassGroup group,
                                             const GlassStyleResolved& rs) {
    AdaptiveSurface out{};
    out.group = group;
    out.contrastBoost = rs.textContrastBoost;
    if (avgLuminance < 0.f) {
        // No live backdrop: theme-dependent palette fallback (chrome). Dark theme's `text` role
        // is LIGHT (darkText=false); light theme's `text` role is dark (darkText=true) — keeps
        // glassText()'s halo tone correct for palette-fallback surfaces.
        out.tint          = fromRole(palette.glassBg);
        out.textPrimary   = fromRole(palette.text);
        out.textSecondary = fromRole(palette.textDim);
        out.textTertiary  = fromRole(palette.textDisabled);
        out.darkText      = (activeTheme == Theme::Light);
        out.scrim         = 0.f;
        // v2.1: user knobs also style chrome fallback surfaces — colorize toward the tint at
        // the fallback tone (V = the glassBg value component), scale alpha by the opacity knob.
        if (rs.tintStrength > 0.f && rs.tintSat > 0.f) {
            const float v = std::max(out.tint.x, std::max(out.tint.y, out.tint.z));
            float r, g, b;
            ImGui::ColorConvertHSVtoRGB(rs.tintHue / 360.f, rs.tintSat, v, r, g, b);
            const float k = rs.tintStrength;
            out.tint.x += (r - out.tint.x) * k;
            out.tint.y += (g - out.tint.y) * k;
            out.tint.z += (b - out.tint.z) * k;
        }
        out.tint.w = std::min(1.f, out.tint.w * rs.opacityMul + 0.22f * out.contrastBoost);
        return out;
    }

    // Live backdrop: theme-INDEPENDENT — tone comes solely from the sampled luminance.
    const float l = avgLuminance;
    out.darkText      = l > 0.50f;
    out.textPrimary   = out.darkText ? hexVec(kVibrancyDarkPrimary)   : hexVec(kVibrancyLightPrimary);
    out.textSecondary = out.darkText ? hexVec(kVibrancyDarkSecondary) : hexVec(kVibrancyLightSecondary);
    out.textTertiary  = out.darkText ? hexVec(kVibrancyDarkTertiary)  : hexVec(kVibrancyLightTertiary);

    // Contrast scrim: strongest at mid-luminance (ambiguous backdrop), tapering at the extremes;
    // then the opacity knob scales it and the contrast boost thickens it.
    float scrim = 0.10f + 0.26f * (1.f - fabsf(l - 0.5f) * 2.f);
    scrim = std::min(1.f, scrim * rs.opacityMul + 0.22f * out.contrastBoost);
    out.scrim = scrim;
    ImVec4 neutral = out.darkText ? ImVec4(0.97f, 0.98f, 1.00f, scrim)
                                  : ImVec4(0.055f, 0.075f, 0.10f, scrim);
    if (rs.tintStrength > 0.f && rs.tintSat > 0.f) {
        float r, g, b;   // V follows the tone so tint never fights the luminance pick
        ImGui::ColorConvertHSVtoRGB(rs.tintHue / 360.f, rs.tintSat,
                                    out.darkText ? 0.97f : 0.10f, r, g, b);
        const float k = rs.tintStrength;
        neutral.x += (r - neutral.x) * k;
        neutral.y += (g - neutral.y) * k;
        neutral.z += (b - neutral.z) * k;
    }
    out.tint = neutral;
    return out;
}

AdaptiveSurface adaptiveSurface(float avgLuminance, GlassGroup group) {
    return adaptiveSurfaceStyled(avgLuminance, group, glassStyleResolved(group));
}

const AdaptiveSurface& adaptivePanel(ImGuiID id, const ImRect& rect, GlassGroup group) {
    struct State {
        int             lastFrame = -1;
        float           lum = -1.f;
        bool            dark = false;
        AdaptiveSurface cur;
        // v2.1 dynamic-color memory (hue smoothed on the shortest arc, see below).
        float           dynHue = -1.f, dynSat = 0.f, dynVar = 0.f;
    };
    static std::unordered_map<ImGuiID, State> states;
    static AdaptiveSurface stateless;

    const float sampled = sampleLuminance(rect.Min, rect.Max);
    if (sampled < 0.f) {
        // Stateless palette fallback — no persistent id lookup, recomputed fresh (cheap).
        // Pass the group through so fallback surfaces still get tint/opacity knobs.
        stateless = adaptiveSurface(-1.f, group);
        return stateless;
    }

    State& st = states[id];
    const int frame = ImGui::GetFrameCount();
    if (st.lastFrame == frame) return st.cur;   // idempotent per frame
    st.lastFrame = frame;

    const float dt = ImGui::GetIO().DeltaTime;
    if (st.lum < 0.f)
        st.lum = sampled;   // snap on first sample for this id
    else
        st.lum = approach(st.lum, sampled, dt, kAdaptiveTau);

    // Hysteresis: only flip tone once the smoothed luminance clears the far threshold.
    st.dark = st.lum > 0.58f ? true : (st.lum < 0.42f ? false : st.dark);

    // v2.1 dynamic color: GUARD ORDER — check the group's knob BEFORE sampling stats so a
    // config with dynamic color off everywhere never touches the stats bridge (zero cost).
    // Text tone stays 100% luminance-driven (hysteresis above untouched) — dynamic mode only
    // steers the tint hue/sat and boosts the scrim on busy backdrops.
    const GlassStyleResolved& rs = glassStyleResolved(group);
    GlassStyleResolved eff = rs;
    BackdropStats stats;
    if (rs.dynamicColor && sampleBackdropStats(rect.Min, rect.Max, stats)) {
        // Shortest-arc hue smoothing toward the backdrop COMPLEMENT (max chroma contrast);
        // slow tau so the tint never flickers or rainbow-spins.
        const float target = std::fmod(stats.hue + kDynHueOffsetDeg, 360.f);
        if (st.dynHue < 0.f) {
            st.dynHue = target;
        } else {
            const float dh = std::fmod(target - st.dynHue + 540.f, 360.f) - 180.f;
            st.dynHue = std::fmod(st.dynHue + dh * (1.f - std::exp(-dt / kDynHueTau)) + 360.f,
                                  360.f);
        }
        st.dynSat = approach(st.dynSat, stats.sat, dt, kDynHueTau);
        st.dynVar = approach(st.dynVar, stats.variance, dt, kAdaptiveTau);

        eff.tintHue = st.dynHue;
        if (st.dynSat >= kDynMinBackdropSat) {
            // Chromatic backdrop: complement tint, saturation follows the backdrop's chroma.
            // (Neutral backdrop keeps the user's own hue/sat/strength — grey stays grey.)
            eff.tintSat      = std::min(1.f, kDynSatBase + kDynSatGain * st.dynSat);
            eff.tintStrength = std::max(rs.tintStrength, kDynTintStrength);
        }
        // Busy backdrop => stronger scrim + text/icon halo.
        eff.textContrastBoost = std::min(
            1.f, rs.textContrastBoost + std::min(kDynVarBoostMax, st.dynVar * kDynVarBoostGain));
    }

    // Decisive VIRTUAL luminance for text tone (never mid-gray); REAL smoothed lum drives scrim.
    const float virt = st.dark ? std::max(st.lum, 0.66f) : std::min(st.lum, 0.34f);
    AdaptiveSurface surf = adaptiveSurfaceStyled(virt, group, eff);
    float realScrim = 0.10f + 0.26f * (1.f - fabsf(st.lum - 0.5f) * 2.f);
    realScrim   = std::min(1.f, realScrim * eff.opacityMul + 0.22f * surf.contrastBoost);
    surf.scrim  = realScrim;
    surf.tint.w = realScrim;

    st.cur = surf;
    return st.cur;
}

ImU32 glassIconHalo(const AdaptiveSurface& surf, float alphaMul) {
    // Opposite vibrancy tone: light halo under dark text, dark halo under light text.
    // v2.1: the contrast boost (user knob + dynamic busyness term) strengthens the halo.
    ImVec4 v = surf.darkText ? ImVec4(0.961f, 0.969f, 0.980f, 1.f)   // vibrancy light primary
                             : ImVec4(0.078f, 0.094f, 0.122f, 1.f);  // vibrancy dark primary
    v.w = std::min(0.55f, (0x50 / 255.f) * (1.f + surf.contrastBoost)) *
          (alphaMul < 0.f ? 0.f : (alphaMul > 1.f ? 1.f : alphaMul));
    return ImGui::ColorConvertFloat4ToU32(v);
}

void glassText(ImDrawList* dl, ImFont* font, float size, ImVec2 pos,
              const AdaptiveSurface& s, const char* text, const char* textEnd) {
    // v2.1: contrast boost thickens the halo (boost 0 == today's 0.30/0.38 alphas).
    const float boost = 0.35f * s.contrastBoost;
    const ImU32 haloCol = s.darkText
        ? ImGui::ColorConvertFloat4ToU32(ImVec4(1.f, 1.f, 1.f, std::min(0.55f, 0.30f + boost)))
        : ImGui::ColorConvertFloat4ToU32(ImVec4(0.f, 0.f, 0.f, std::min(0.65f, 0.38f + boost)));
    dl->AddText(font, size, ImVec2(pos.x, pos.y + 1.f * uiScale), haloCol, text, textEnd);
    dl->AddText(font, size, pos, ImGui::ColorConvertFloat4ToU32(s.textPrimary), text, textEnd);
}

ImVec4 maxContrastTextOn(const ImVec4& surface) {
    // Rec.709 luminance split at 0.5 — same threshold adaptiveSurface() uses for its tone pick,
    // same vibrancy primaries, so custom-tinted chips match adaptive panels exactly.
    const float l = 0.2126f * surface.x + 0.7152f * surface.y + 0.0722f * surface.z;
    return (l > 0.5f) ? hexVec(kVibrancyDarkPrimary) : hexVec(kVibrancyLightPrimary);
}

void glassPanel(ImDrawList* dl, const ImRect& rect, GlassLevel level, float rounding,
               const AdaptiveSurface& surf, float alpha) {
    // v2.1: per-group user overrides ride in through surf.group (signature unchanged) — a
    // forced material level, a rounding override, and the opacity multiplier. All-default
    // resolved styles reproduce the pre-v2.1 output byte-for-byte.
    const GlassStyleResolved& rs = glassStyleResolved(surf.group);
    const GlassLevel lvl = (rs.glassLevel >= 0) ? (GlassLevel)rs.glassLevel : level;
    const float tintAlpha = glassLevelTintAlpha(lvl);
    const float s = uiScale;
    const float rDp = (rs.rounding >= 0.f) ? rs.rounding
                                           : (rounding < 0.f ? kDefaultPanelRounding : rounding);
    const float r = rDp * s;
    const ImVec2 mn = rect.Min;
    const ImVec2 mx = rect.Max;

    const ViewportBackdropInfo& bd = viewportBackdrop();
    if (bd.valid()) {
        // Project the panel rect into backdrop UV space (per-rect, not the whole texture):
        // clamped 0..1 so a rect spilling past the viewport image clamps into the edge texel
        // band — that IS the intended frosted edge-bleed for docked side panels.
        const float invW = 1.f / (bd.screenMax.x - bd.screenMin.x);
        const float invH = 1.f / (bd.screenMax.y - bd.screenMin.y);
        auto projectUV = [&](const ImVec2& p) -> ImVec2 {
            float u = (p.x - bd.screenMin.x) * invW;
            float v = (p.y - bd.screenMin.y) * invH;
            u = std::min(1.f, std::max(0.f, u));
            v = std::min(1.f, std::max(0.f, v));
            if (bd.flipV) v = 1.f - v;
            return ImVec2(bd.uvMin.x + (bd.uvMax.x - bd.uvMin.x) * u,
                          bd.uvMin.y + (bd.uvMax.y - bd.uvMin.y) * v);
        };
        const ImVec2 uv0 = projectUV(mn);
        const ImVec2 uv1 = projectUV(mx);
        const ImU32 imgCol = IM_COL32(255, 255, 255, (int)(255.f * alpha));
        dl->AddImageRounded((ImTextureID)(intptr_t)bd.textureId, mn, mx, uv0, uv1, imgCol, r);

        // Material wash + the surface's own contrast scrim, capped — the blur already carries
        // most legibility so this stays a light finishing tint. surf.scrim already carries the
        // opacity knob + contrast boost from adaptiveSurface — only the wash is scaled here.
        float tintA = tintAlpha * 0.55f * rs.opacityMul + surf.scrim;
        tintA = std::min(tintA, 0.62f + 0.18f * surf.contrastBoost);
        ImVec4 tint = surf.tint;
        tint.w = tintA * alpha;
        dl->AddRectFilled(mn, mx, ImGui::ColorConvertFloat4ToU32(tint), r);
    } else {
        // Layered fallback when no live backdrop (first frame / hidden viewport).
        glassBackground(dl, mn, mx, rDp, alpha * std::min(1.f, rs.opacityMul));
        ImVec4 tint = surf.tint;
        tint.w = tintAlpha * 0.5f * rs.opacityMul * alpha;
        if (tint.w > 0.001f)
            dl->AddRectFilled(mn, mx, ImGui::ColorConvertFloat4ToU32(tint), r);
    }

    // Edge effects (both paths): (1) top specular (2) inner rim (3) bottom shadow (4) outer
    // border — all widths * uiScale.
    const float hairline = 1.f * s;
    const float strokeW  = 1.5f * s;
    const float inset    = r * 0.6f;
    const bool  dark     = (activeTheme == Theme::Dark);

    const float hiA = dark ? 0.11f : 0.14f;
    const float lineY = mn.y + strokeW;
    dl->AddLine(ImVec2(mn.x + inset, lineY), ImVec2(mx.x - inset, lineY),
                roleAlphaU32(palette.glassHighlight, hiA * alpha), strokeW);

    const float rimA = (dark ? 0.06f : 0.10f) * alpha;
    const ImVec2 rimMn(mn.x + hairline, mn.y + hairline);
    const ImVec2 rimMx(mx.x - hairline, mx.y - hairline);
    const float rimR = std::max(0.f, r - hairline);
    dl->AddRect(rimMn, rimMx, roleAlphaU32(palette.glassHighlight, rimA), rimR, 0, hairline);

    const float shY = mx.y - hairline * 0.5f;
    dl->AddLine(ImVec2(mn.x + inset, shY), ImVec2(mx.x - inset, shY),
                roleAlphaU32(palette.glassShadow, 0.22f * alpha), hairline);

    float borderCol[4];
    Palette::withAlpha(palette.glassBorder, borderCol, 0.75f * alpha);
    dl->AddRect(mn, mx, Palette::toU32(borderCol), r, 0, strokeW);
}

// "Pill on a hairline track" scrollbar — shared look for the doc-tab file strip and the
// FeatureManager body (see ui_theme.h). A thin, low-alpha full-length track line with a thicker,
// fully-rounded pill thumb riding on it. Pure draw; caller owns geometry + drag.
void drawScrollPill(ImDrawList* dl, const ImVec2& trackMin, const ImVec2& trackMax,
                    float thumbA, float thumbB, bool vertical, bool hovered,
                    const AdaptiveSurface& surf) {
    const float s = ui::uiScale;
    // A thin track runs down the CENTER of the band; the thumb is the SAME thickness as the track
    // (no fatter oval bulge — Hunter's program-wide default), just a brighter, rounded segment of
    // the same line so it reads as a minimal hairline scrollbar.
    const float trackThin = 2.0f * s;
    const float pillThick = trackThin;
    const ImU32 trackCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textTertiary.x, surf.textTertiary.y, surf.textTertiary.z, 0.16f));
    const ImU32 pillCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, hovered ? 0.58f : 0.34f));
    if (vertical) {
        const float cx = (trackMin.x + trackMax.x) * 0.5f;
        dl->AddLine(ImVec2(cx, trackMin.y), ImVec2(cx, trackMax.y), trackCol, trackThin);
        dl->AddRectFilled(ImVec2(cx - pillThick * 0.5f, thumbA),
                          ImVec2(cx + pillThick * 0.5f, thumbB), pillCol, pillThick * 0.5f);
    } else {
        const float cy = (trackMin.y + trackMax.y) * 0.5f;
        dl->AddLine(ImVec2(trackMin.x, cy), ImVec2(trackMax.x, cy), trackCol, trackThin);
        dl->AddRectFilled(ImVec2(thumbA, cy - pillThick * 0.5f),
                          ImVec2(thumbB, cy + pillThick * 0.5f), pillCol, pillThick * 0.5f);
    }
}

// Custom vertical scrollbar for the current window/child (see ui_theme.h). Reads the live scroll
// state, draws the pill overlay on the right edge, and drags to scroll. No-op if nothing overflows.
void drawWindowPillScrollbarV(const AdaptiveSurface& surf) {
    const float scrollMax = ImGui::GetScrollMaxY();
    // Ignore sub-padding phantom overflow: a child with AlwaysUseWindowPadding + a fixed height can
    // report a few px of ScrollMaxY even when the content visually fits (e.g. collapsed tree
    // sections), which showed a spurious bar. Only show it for a real overflow bigger than the
    // window's own bottom padding.
    const float phantom = ImGui::GetStyle().WindowPadding.y + 4.0f * ui::uiScale;
    if (scrollMax <= phantom) return;   // fits — no bar (hidden entirely, not just faded)
    const float s = ui::uiScale;
    const ImVec2 wpos = ImGui::GetWindowPos();
    const ImVec2 wsz = ImGui::GetWindowSize();
    const float scrollY = ImGui::GetScrollY();
    const float viewH = wsz.y;
    const float contentH = viewH + scrollMax;

    // Track hugs the right edge, inset a hair; a little top/bottom margin so the pill's rounded
    // ends aren't flush with the window corners.
    const float bandW = 10.0f * s;
    const float margin = 3.0f * s;
    const float trackX0 = wpos.x + wsz.x - bandW;
    const float trackTop = wpos.y + margin;
    const float trackBot = wpos.y + wsz.y - margin;
    const float trackLen = trackBot - trackTop;

    const float thumbLen = std::max(24.0f * s, trackLen * (viewH / contentH));
    const float thumbTop = trackTop + (scrollY / scrollMax) * (trackLen - thumbLen);
    const float thumbBot = thumbTop + thumbLen;

    // Drag interaction — a fixed-position (screen-space) hit strip along the track, so it doesn't
    // move with the scrolled content.
    ImGui::SetCursorScreenPos(ImVec2(trackX0, trackTop));
    ImGui::InvisibleButton("##pillscroll_v", ImVec2(bandW, trackLen));
    const bool hov = ImGui::IsItemHovered() || ImGui::IsItemActive();
    if (ImGui::IsItemActive()) {
        const float my = ImGui::GetIO().MousePos.y;
        const float t = (my - trackTop - thumbLen * 0.5f) / std::max(1.0f, trackLen - thumbLen);
        ImGui::SetScrollY(std::min(std::max(t, 0.0f), 1.0f) * scrollMax);
    }
    drawScrollPill(ImGui::GetWindowDrawList(), ImVec2(trackX0, trackTop),
                   ImVec2(trackX0 + bandW, trackBot), thumbTop, thumbBot,
                   /*vertical=*/true, hov, surf);
}

}  // namespace ui
