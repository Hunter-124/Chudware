// AI-CAD UI theme implementation.
// Two named palettes: Dark (original teal-on-dark brand) and Light (SolidWorks-style).
// applyTheme(Theme, scale) populates both ui::palette and ImGuiStyle from the chosen set.
// Panels read ONLY ui::palette — never hardcode color literals.
#include "ui_theme.h"

#include "imgui.h"

#include <cstdio>

namespace ui {

Palette palette;
Theme   activeTheme = Theme::Dark;

// ---- small helper -------------------------------------------------------
static ImVec4 rgba(float r, float g, float b, float a = 1.0f) {
    return ImVec4(r, g, b, a);
}
static ImVec4 rgb8(int r, int g, int b, float a = 1.0f) {
    return ImVec4(r / 255.0f, g / 255.0f, b / 255.0f, a);
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
    set4(p.textDim,       0.520f,0.560f,0.620f, 1);
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
    // tree
    set4(p.treeRow,       0,0,0, 0);
    set4(p.treeRowAlt,    0,0,0, 0.04f);
    set4(p.treeRowSelected,0.176f,0.831f,0.749f,0.18f);
    // viewport
    set4(p.viewportTop,   0.170f,0.190f,0.230f, 1);
    set4(p.viewportBottom,0.085f,0.095f,0.115f, 1);
    set4(p.overlayBg,     0.039f,0.055f,0.075f, 0.843f);
    set4(p.overlayBorder, 0.176f,0.831f,0.749f, 0.275f);
    set4(p.overlayText,   0.886f,0.906f,0.941f, 0.941f);
    // selection
    set4(p.selectionHighlight, 0.176f,0.831f,0.749f, 0.22f);
    set4(p.hoverHighlight,     0.176f,0.831f,0.749f, 0.40f);
    // C24 orientation-triad axes
    set4(p.axisX, 0.910f,0.380f,0.380f, 1);
    set4(p.axisY, 0.470f,0.820f,0.450f, 1);
    set4(p.axisZ, 0.420f,0.620f,0.960f, 1);
    // C18 sketch mode
    set4(p.sketchUnder, 0.337f,0.612f,0.960f, 1);
    set4(p.sketchGrid,  0.520f,0.560f,0.620f, 0.18f);
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
    set4(p.proseText,  0.700f,0.740f,0.800f, 1);
    set4(p.stepCardBg, 0.082f,0.094f,0.118f, 1);
    set4(p.planCardBg, 0.071f,0.102f,0.110f, 1);
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
    set4(p.userBubbleBg,    0.176f,0.831f,0.749f,0.07f);
    set4(p.userBubbleBorder,0.176f,0.831f,0.749f,0.22f);
    set4(p.assistantName,   0.780f,0.820f,0.900f,1);
    set4(p.errSystem,       0.961f,0.600f,0.502f,1);
    set4(p.errCad,          0.929f,0.800f,0.561f,1);
    // misc
    p.planAccentBar = 150.0f;
    set4(p.planDims,    0.639f,0.722f,0.780f,1);
    set4(p.planFeature, 0.600f,0.639f,0.700f,1);
    set4(p.partsDim,    0.620f,0.660f,0.720f,1);
    p.captionFg = 0.922f;
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
    set4(p.textDim,       0.420f,0.447f,0.502f, 1);   // medium grey
    set4(p.textDisabled,  0.631f,0.647f,0.675f, 1);
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
    // tree
    set4(p.treeRow,       0,0,0, 0);
    set4(p.treeRowAlt,    0,0,0, 0.025f);
    set4(p.treeRowSelected,0.000f,0.471f,0.843f,0.18f);
    // viewport: light-gray studio gradient (SW-style)
    set4(p.viewportTop,   0.847f,0.847f,0.847f, 1);   // #D8D8D8
    set4(p.viewportBottom,0.706f,0.706f,0.706f, 1);   // #B4B4B4
    set4(p.overlayBg,     0.961f,0.961f,0.961f, 0.882f);
    set4(p.overlayBorder, 0.000f,0.471f,0.843f, 0.40f);
    set4(p.overlayText,   0.118f,0.137f,0.196f, 0.941f);
    // selection
    set4(p.selectionHighlight, 0.000f,0.471f,0.843f, 0.18f);
    set4(p.hoverHighlight,     0.000f,0.471f,0.843f, 0.35f);
    // C24 orientation-triad axes (slightly deeper on the light studio bg for contrast)
    set4(p.axisX, 0.800f,0.180f,0.180f, 1);
    set4(p.axisY, 0.180f,0.580f,0.220f, 1);
    set4(p.axisZ, 0.000f,0.380f,0.820f, 1);
    // C18 sketch mode (SW blue under-defined, darker grid on the light studio)
    set4(p.sketchUnder, 0.000f,0.471f,0.843f, 1);
    set4(p.sketchGrid,  0.420f,0.447f,0.502f, 0.22f);
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
    set4(p.stepCardBg, 0.957f,0.961f,0.969f, 1);
    set4(p.planCardBg, 0.929f,0.953f,0.980f, 1);    // light blue tint for plan card
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
    set4(p.userBubbleBg,    0.000f,0.471f,0.843f,0.06f);
    set4(p.userBubbleBorder,0.000f,0.471f,0.843f,0.25f);
    set4(p.assistantName,   0.118f,0.137f,0.196f,1);
    set4(p.errSystem,       0.706f,0.118f,0.118f,1);
    set4(p.errCad,          0.627f,0.412f,0.000f,1);
    // misc
    p.planAccentBar = 180.0f;
    set4(p.planDims,    0.200f,0.310f,0.502f,1);
    set4(p.planFeature, 0.310f,0.353f,0.431f,1);
    set4(p.partsDim,    0.420f,0.447f,0.502f,1);
    p.captionFg = 0.118f;
}

// =========================================================================
// Apply: fill ImGuiStyle from the chosen theme, then update ui::palette
// =========================================================================
void applyTheme(Theme t, float scale) {
    activeTheme = t;

    // Step 1: fill the palette struct
    if (t == Theme::Light)
        fillLight(palette);
    else
        fillDark(palette);

    // Step 2: set ImGuiStyle
    ImGuiStyle& s = ImGui::GetStyle();

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
    s.FrameBorderSize   = 0.0f;
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
    c[ImGuiCol_PopupBg]             = fromRole(p.popupBg);
    c[ImGuiCol_Border]              = fromRole(p.border);
    c[ImGuiCol_BorderShadow]        = rgba(0,0,0,0);
    c[ImGuiCol_FrameBg]             = fromRole(p.frameBg);
    c[ImGuiCol_FrameBgHovered]      = fromRole(p.frameBgHovered);
    c[ImGuiCol_FrameBgActive]       = fromRole(p.frameBgActive);
    c[ImGuiCol_TitleBg]             = fromRole(p.headerBg);
    c[ImGuiCol_TitleBgActive]       = fromRole(p.headerBg);
    c[ImGuiCol_TitleBgCollapsed]    = fromRole(p.windowBg);
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
// Font loading (unchanged)
// =========================================================================
static const ImWchar* uiGlyphRanges() {
    static const ImWchar r[] = {
        0x0020, 0x00FF,
        0x2010, 0x2027,
        0x2070, 0x209F,
        0x2190, 0x21FF,
        0x2022, 0x2022,
        0x25A0, 0x25FF,
        0,
    };
    return r;
}

static ImFont* addFont(ImGuiIO& io, const std::string& path, float px, ImFont* fallback) {
    ImFont* f = io.Fonts->AddFontFromFileTTF(path.c_str(), px, nullptr, uiGlyphRanges());
    return f ? f : fallback;
}

Fonts loadFonts(const std::string& assetsDir, float pxScale) {
    ImGuiIO& io = ImGui::GetIO();
    const std::string fd = assetsDir + "\\fonts\\";
    Fonts f;
    ImFont* def = io.Fonts->AddFontDefault();
    f.body   = addFont(io, fd + "Rubik-Regular.ttf",      17.5f * pxScale, def);
    f.medium = addFont(io, fd + "Rubik-Medium.ttf",       17.5f * pxScale, f.body);
    f.h1     = addFont(io, fd + "Rubik-Bold.ttf",         30.0f * pxScale, f.body);
    f.h2     = addFont(io, fd + "Rubik-SemiBold.ttf",     19.0f * pxScale, f.body);
    f.mono   = addFont(io, fd + "JetBrainsMono-Regular.ttf",15.5f * pxScale, f.body);
    io.Fonts->Build();
    io.FontDefault = f.body;
    return f;
}

}  // namespace ui
