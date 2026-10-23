// Chudware UI theme implementation.
// Two named palettes: Dark (original dark brand chrome) and Light (SolidWorks-style). The ACCENT
// pair is not part of either palette: it is a user-owned brand role installed over both by
// setAccentColors, so the same orange/blue reads identically in dark and light.
// applyTheme(Theme, scale) populates both ui::palette and ImGuiStyle from the chosen set.
// Panels read ONLY ui::palette — never hardcode color literals.
#include "ui_theme.h"

#include "imgui.h"
#include "imgui_internal.h"   // ImHashStr (panelId)
#include "ui/glyph_ranges.h"   // the atlas codepoint table (shared with agent_text + gui_lint)
#include "ui/tesseract.h"     // drawTesseractSpinner — the brand mark doubles as the busy indicator
#include "ui/icons.h"         // recolorIconAtlas — the baked 3D ribbon tiles track the accent pair

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <unordered_map>

namespace ui {

Palette palette;
Theme   activeTheme = Theme::Dark;
float   uiScale     = 1.0f;

const float evalEmphasisPalette[10][4] = {
    {0.90f, 0.20f, 0.18f, 1.0f},
    {0.95f, 0.55f, 0.15f, 1.0f},
    {0.95f, 0.80f, 0.20f, 1.0f},
    {0.30f, 0.75f, 0.30f, 1.0f},
    {0.18f, 0.78f, 0.72f, 1.0f},
    {0.20f, 0.65f, 0.90f, 1.0f},
    {0.25f, 0.45f, 0.90f, 1.0f},
    {0.55f, 0.35f, 0.85f, 1.0f},
    {0.85f, 0.30f, 0.70f, 1.0f},
    {0.60f, 0.40f, 0.25f, 1.0f},
};

// ---- small helper -------------------------------------------------------
static ImVec4 rgba(float r, float g, float b, float a = 1.0f) {
    return ImVec4(r, g, b, a);
}
static void set4(float dst[4], float r, float g, float b, float a = 1.0f) {
    dst[0]=r; dst[1]=g; dst[2]=b; dst[3]=a;
}
static ImVec4 fromRole(const float f[4]) { return ImVec4(f[0],f[1],f[2],f[3]); }

// =========================================================================
// Accent roles (user-owned, shared by both palettes)
// =========================================================================
const float kDefaultAccentPrimary[3]   = {0.949f, 0.510f, 0.086f};   // #F28216 orange
const float kDefaultAccentSecondary[3] = {0.000f, 0.471f, 0.843f};   // #0078D7 blue

// The LIVE pair, seeded from the defaults above (ordered init inside this TU, so the copy is
// well-defined) and overwritten in place by setAccentColors. fillDark/fillLight read this and
// never a literal: they re-run on every theme toggle and would otherwise silently revert the
// user's colors to whichever hue the theme happened to be authored with.
static float gAccentPrimary[3]   = {kDefaultAccentPrimary[0], kDefaultAccentPrimary[1],
                                    kDefaultAccentPrimary[2]};
static float gAccentSecondary[3] = {kDefaultAccentSecondary[0], kDefaultAccentSecondary[1],
                                    kDefaultAccentSecondary[2]};

// Hue-preserving darken for the *Dim members. 0.70 reproduces the hand-authored Dim literals both
// palettes used to carry (teal 0.665x, SW blue 0.75x) to within one 8-bit step — and unlike those,
// it cannot drift in hue when the base color moves, which is what made "edit eight literals in
// lockstep" the only way to recolor the app.
static constexpr float kAccentDimScale = 0.70f;

// Per-theme wash alphas for the accent families and every accent-DERIVED role. Only the alphas
// differ between palettes — the hue is the user's, identically in both — so this is the whole of
// what a theme still gets to say about the accent. Recoloring the app is now two RGB triples.
struct AccentAlphas {
    float soft, mid;          // accentSoft / accentMid (and their accent2 twins)
    float selection, hover;   // selection + hover face washes
    float overlayBorder;      // viewport pill-overlay border
    float chip;               // code/diff chip fill
    float treeHover;          // FeatureManager row hover wash
};
// Light needs the weaker set: the 0.40 wash that reads as a hint on near-black chrome reads as an
// opaque slab on near-white.
static constexpr AccentAlphas kDarkAccentAlphas {0.22f, 0.40f, 0.22f, 0.40f, 0.275f, 0.30f, 0.10f};
static constexpr AccentAlphas kLightAccentAlphas{0.18f, 0.35f, 0.18f, 0.35f, 0.400f, 0.22f, 0.08f};

// Derive one accent family (base / Dim / Soft / Mid) from a single RGB triple.
static void fillAccentFamily(float base[4], float dim[4], float soft[4], float mid[4],
                             const float rgb[3], float softA, float midA) {
    set4(base, rgb[0], rgb[1], rgb[2], 1);
    set4(dim,  rgb[0] * kAccentDimScale, rgb[1] * kAccentDimScale, rgb[2] * kAccentDimScale, 1);
    set4(soft, rgb[0], rgb[1], rgb[2], softA);
    set4(mid,  rgb[0], rgb[1], rgb[2], midA);
}

// Install an accent pair into a palette. Shared by fillDark/fillLight and setAccentColors, so the
// two families plus their derived washes are authored ONCE instead of eight literals per theme.
static void applyAccents(Palette& p, const float pri[3], const float sec[3],
                         const AccentAlphas& a) {
    fillAccentFamily(p.accent,  p.accentDim,  p.accentSoft,  p.accentMid,  pri, a.soft, a.mid);
    fillAccentFamily(p.accent2, p.accent2Dim, p.accent2Soft, p.accent2Mid, sec, a.soft, a.mid);
    // These five ARE the accent, just at a different wash weight — they used to repeat the accent
    // literal verbatim, which is why they silently stopped matching whenever one of them was tuned.
    // A user who picks orange expects the selection glow, the row hover and the HUD pill edge
    // orange too. (Roles that merely HAPPENED to equal the old accent — statusOk/consoleOk, which
    // mean SUCCESS, and the SolidWorks-blue datum-plane outline — are deliberately not here.)
    set4(p.selectionHighlight, pri[0], pri[1], pri[2], a.selection);
    set4(p.hoverHighlight,     pri[0], pri[1], pri[2], a.hover);
    set4(p.overlayBorder,      pri[0], pri[1], pri[2], a.overlayBorder);
    set4(p.chipBg,             pri[0], pri[1], pri[2], a.chip);
    set4(p.treeHover,          pri[0], pri[1], pri[2], a.treeHover);
}

// The ImGuiCol slots that resolve from the accent. Owned here rather than inlined into
// applyTheme's enum-ordered block, because setAccentColors has to re-point exactly these at
// runtime: while they lived inline, a live recolor left every checkmark, header, resize grip and
// tab-hover on the PREVIOUS accent until the user toggled the theme.
static void applyAccentStyleColors() {
    ImVec4* c = ImGui::GetStyle().Colors;
    const Palette& p = palette;
    c[ImGuiCol_ScrollbarGrabActive] = fromRole(p.accentMid);
    c[ImGuiCol_CheckMark]           = fromRole(p.accent);
    c[ImGuiCol_SliderGrab]          = fromRole(p.accent);
    c[ImGuiCol_SliderGrabActive]    = fromRole(p.accentDim);
    c[ImGuiCol_Header]              = fromRole(p.accentSoft);
    c[ImGuiCol_HeaderHovered]       = fromRole(p.accentMid);
    c[ImGuiCol_HeaderActive]        = fromRole(p.accentMid);
    c[ImGuiCol_SeparatorHovered]    = fromRole(p.accentMid);
    c[ImGuiCol_SeparatorActive]     = fromRole(p.accent);
    c[ImGuiCol_ResizeGrip]          = fromRole(p.accentSoft);
    c[ImGuiCol_ResizeGripHovered]   = fromRole(p.accentMid);
    c[ImGuiCol_ResizeGripActive]    = fromRole(p.accent);
    c[ImGuiCol_TabHovered]          = fromRole(p.accentMid);
    c[ImGuiCol_DockingPreview]      = fromRole(p.accentMid);
    c[ImGuiCol_PlotLines]           = fromRole(p.accent);
    c[ImGuiCol_PlotHistogram]       = fromRole(p.accent);
    c[ImGuiCol_TextSelectedBg]      = fromRole(p.accentSoft);
    c[ImGuiCol_NavHighlight]        = fromRole(p.accent);
}

// =========================================================================
// Dark palette (the original dark chrome, byte-for-byte where possible; accent comes from the
// live user pair, so nothing in here names a brand hue)
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
    // accent + accent2 families and every accent-derived wash (selection/hover/overlay border/
    // chip/tree hover). Colors come from the LIVE user pair, never a literal.
    applyAccents(p, gAccentPrimary, gAccentSecondary, kDarkAccentAlphas);
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
    set4(p.overlayText,   0.886f,0.906f,0.941f, 0.941f);
    // datum planes (light-blue, SolidWorks look — viewport is light in both themes)
    set4(p.planeFill,    0.380f,0.620f,0.910f, 0.140f);   // C25: 0.085 was imperceptible
    set4(p.planeOutline, 0.290f,0.540f,0.870f, 0.700f);
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
    set4(p.camStock,   0.55f,0.60f,0.68f, 0.50f);
    set4(p.camTool,    0.95f,0.80f,0.25f, 0.90f);
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
    set4(p.statusOk,   0.459f,0.859f,0.549f, 1);    // success green (was the accent literal)
    set4(p.statusWarn, 0.953f,0.761f,0.420f, 1);
    set4(p.statusErr,  0.961f,0.549f,0.420f, 1);
    // console
    set4(p.consoleDefault,0.780f,0.820f,0.880f,1);
    set4(p.consoleOk,   0.459f,0.859f,0.549f, 1);   // success green (was the accent literal)
    set4(p.consoleWarn, 0.953f,0.761f,0.420f, 1);
    set4(p.consoleErr,  0.961f,0.549f,0.420f, 1);
    set4(p.consoleDim,  0.620f,0.660f,0.720f, 1);
    // code/diff
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
    set4(p.glassShadow,    0.000f, 0.000f, 0.000f, 0.40f);  // in-rect bottom hairline / scrims
    // tree (feature-manager rows)
    set4(p.treeSection,    0.620f, 0.660f, 0.720f, 1.0f);   // muted section-header label
    // copilot blocks (chat action cards)
    set4(p.blockActionBg,  0.094f, 0.110f, 0.141f, 0.96f);  // near-opaque action card
    // Soft cool control edge. Dark frames sit only a few levels above the card glass, so the edge
    // carries the shape: at 0.60a an unchecked checkbox / empty field read as a borderless block.
    set4(p.controlBorder,  0.360f, 0.410f, 0.520f, 0.80f);
}

// =========================================================================
// Light palette (SolidWorks-style: near-white chrome, slate text; accent from the user pair)
// =========================================================================
static void fillLight(Palette& p) {
    // SolidWorks-ish neutral palette:
    //   ribbon / toolbar:  #F0F0F0  (very light grey)
    //   panel bg:          #FFFFFF
    //   window bg:         #F5F5F5
    //   header / menu:     #E8E8E8
    //   text:              #1E2329 (near-black slate)
    //   selection:         the user's PRIMARY accent (orange out of the box)
    //   datum planes:      #4A8ADE (a CAD convention, independent of the accent)

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
    // accent + accent2: same live user pair as the dark palette, at the lighter wash alphas.
    applyAccents(p, gAccentPrimary, gAccentSecondary, kLightAccentAlphas);
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
    set4(p.overlayText,   0.118f,0.137f,0.196f, 0.941f);
    // datum planes (same light-blue as dark theme — viewport gradient is identical)
    set4(p.planeFill,    0.380f,0.620f,0.910f, 0.140f);   // C25: 0.090 was imperceptible
    // planeOutline is the SolidWorks datum-plane blue, NOT the accent — it only looked like the
    // accent while the light accent happened to be SW blue. Pinned to the dark palette's value so
    // both themes draw datum planes identically (planeFill already matches).
    set4(p.planeOutline, 0.290f,0.540f,0.870f, 0.750f);
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
    set4(p.camStock,   0.35f,0.40f,0.50f, 0.50f);
    set4(p.camTool,    0.85f,0.65f,0.10f, 0.95f);
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
    set4(p.glassShadow,    0.000f, 0.000f, 0.000f, 0.18f);  // lighter bottom hairline on light bg
    // tree (feature-manager rows)
    set4(p.treeSection,    0.357f, 0.384f, 0.439f, 1.0f);   // darker grey section-header label
    // copilot blocks (chat action cards)
    set4(p.blockActionBg,  0.930f, 0.940f, 0.960f, 0.96f);  // near-opaque action card
    set4(p.controlBorder,  0.560f, 0.590f, 0.660f, 0.55f);  // soft grey-blue control edge
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

    // --- shape: rounded, compact-but-airy (same for both themes) ---
    // One control radius: FrameRounding == the kit's chip/button radius (6 dp), so a native
    // ImGui::Button and a kit::secondaryButton side by side share a corner. Grabs are pills
    // (rounding = half the grab), windows/popups a step rounder than the controls inside them.
    s.WindowRounding    = 10.0f;
    s.ChildRounding     = 8.0f;
    s.FrameRounding     = 6.0f;
    s.PopupRounding     = 8.0f;
    s.ScrollbarRounding = 10.0f;
    s.GrabRounding      = 7.0f;
    s.TabRounding       = 6.0f;

    // Compact density: one frame is ~2 dp shorter and rows sit 2 dp closer than before, while
    // FramePadding.x stays 12 (metrics::partsPadX() aligns pane gutters to it).
    s.WindowPadding     = ImVec2(12, 10);
    s.FramePadding      = ImVec2(12, 6);
    s.ItemSpacing       = ImVec2(9, 7);
    s.ItemInnerSpacing  = ImVec2(6, 5);  // gui-lint: allow (base style; ScaleAllSizes below)
    s.CellPadding       = ImVec2(8, 5);  // gui-lint: allow (base style; ScaleAllSizes below)
    s.ScrollbarSize     = 10.0f;
    s.GrabMinSize       = 14.0f;

    s.WindowBorderSize  = 1.0f;
    s.ChildBorderSize   = 1.0f;
    s.PopupBorderSize   = 1.0f;
    // Frame outlines in BOTH themes: dark frames are only a few levels above the panels they sit
    // on, so without an edge an unchecked native checkbox or empty field reads as a black block.
    s.FrameBorderSize   = 1.0f;
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
    // Popups, popovers, combos and tooltips float over live UI text, not just the 3D backdrop:
    // at the frosted-glass alpha (0.87 dark / 0.92 light) the rows underneath ghost through and
    // fight the popup's own text. Keep the glass hue (the subtle tint) but make the fill all but
    // opaque so a popup always reads as one clean surface.
    constexpr float kPopupFillA = 0.98f;
    c[ImGuiCol_PopupBg]             = ImVec4(p.glassBg[0], p.glassBg[1], p.glassBg[2],
                                             std::max(p.glassBg[3], kPopupFillA));
    // Subwindow / popup OS-border. p.border is tuned for SEPARATORS (soft, low-contrast) and
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
    c[ImGuiCol_Button]              = fromRole(p.button);
    c[ImGuiCol_ButtonHovered]       = fromRole(p.buttonHovered);
    c[ImGuiCol_ButtonActive]        = fromRole(p.buttonActive);
    c[ImGuiCol_Separator]           = fromRole(p.border);
    c[ImGuiCol_Tab]                 = fromRole(p.windowBg);
    c[ImGuiCol_TabActive]           = fromRole(p.button);
    c[ImGuiCol_TabUnfocused]        = fromRole(p.panelBg);
    c[ImGuiCol_TabUnfocusedActive]  = fromRole(p.frameBg);
    c[ImGuiCol_DockingEmptyBg]      = fromRole(p.windowBg);
    c[ImGuiCol_ModalWindowDimBg]    = (t == Theme::Light)
                                      ? rgba(0.1f, 0.1f, 0.1f, 0.45f)
                                      : rgba(0.02f, 0.027f, 0.039f, 0.65f);
    // Accent-derived slots last, from the palette the fill above already installed.
    applyAccentStyleColors();

    if (scale != 1.0f) s.ScaleAllSizes(scale);

    // The ribbon tiles bake the accent pair into their pixels. A theme toggle re-runs the fill
    // with the LIVE user pair, so re-derive the atlas from the same pair — never from the theme
    // default, which would quietly discard a custom accent on every dark/light switch. No-ops
    // before loadIcons has decoded an atlas (the very first applyTheme at startup).
    recolorIconAtlas(gAccentPrimary, gAccentSecondary);
}

void setAccentColors(const float primary[3], const float secondary[3]) {
    for (int i = 0; i < 3; ++i) {
        gAccentPrimary[i]   = primary[i];
        gAccentSecondary[i] = secondary[i];
    }
    // Only the accent-owned roles are touched: re-running the whole theme fill here would reset
    // every unrelated chrome role and, at DPI != 100%, re-scale ImGuiStyle a second time.
    applyAccents(palette, gAccentPrimary, gAccentSecondary,
                 (activeTheme == Theme::Light) ? kLightAccentAlphas : kDarkAccentAlphas);
    applyAccentStyleColors();
    recolorIconAtlas(gAccentPrimary, gAccentSecondary);
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

    // No drop shadow. The old step 1 stacked two rects offset down-right by 2.5-4 dp: on every
    // control-sized caller (PLM tab buttons, Firmware toolbar, Parts filter fields via
    // glassCardWash, kit cards) it read as a dark embossed crescent that the backdrop path of
    // glassPanel() never drew, so identical controls looked flat in one workspace and raised in
    // the next. Depth comes from the in-rect edge treatment only (shine, border, specular) —
    // the spec's non-offset treatment; glassPanel() adds its own inner rim + bottom hairline.

    // 1. Base translucent fill.
    dl->AddRectFilled(mn, mx, A(p.glassBg, 1.0f), r);

    // 2. Inner vertical shine gradient — top 35% of height, glassHighlight -> transparent.
    {
        const float shineH = (mx.y - mn.y) * 0.35f;
        const ImVec2 shineMx(mx.x, mn.y + shineH);
        const uint32_t top = A(p.glassHighlight, 1.0f);
        const uint32_t btm = top & 0x00FFFFFFu;   // same RGB, alpha=0 (toU32 is ABGR: bits 24-31 = A)
        dl->AddRectFilledMultiColor(mn, shineMx, top, top, btm, btm);
    }

    // 3. Border.
    dl->AddRect(mn, mx, A(p.glassBorder, 1.0f), r, 0, 1.5f * s);

    // 4. Top specular line — 1.5 px inside the top border arc.
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

void pushContextMenuStyle() {
    const Palette& p = palette;
    const float s = uiScale;
    ImGui::PushStyleColor(ImGuiCol_PopupBg,       fromRole(p.popupBg));
    ImGui::PushStyleColor(ImGuiCol_Border,        fromRole(p.glassBorder));
    ImGui::PushStyleColor(ImGuiCol_Header,        fromRole(p.accentSoft));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, fromRole(p.accentSoft));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  fromRole(p.accentMid));
    // A MenuItem's hover band extends half of ItemSpacing past the content box on each side
    // (Selectable pad-with-half-spacing), so band inset = WindowPadding - ItemSpacing/2:
    // 12 - 8 = 4 dp horizontally, 8 - 4 = 4 dp vertically, labels 12 dp from the edge.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12.0f * s, 8.0f * s));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,   ImVec2(16.0f * s, 8.0f * s));
}

void popContextMenuStyle() {
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(5);
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

// Lerp two packed colours channel-wise (non-premultiplied, matching ImGui's blend).
static inline ImU32 lerpU32(ImU32 a, ImU32 b, float t) {
    const float u = 1.0f - t;
    const int r = (int)(((a >> IM_COL32_R_SHIFT) & 0xFF) * u + ((b >> IM_COL32_R_SHIFT) & 0xFF) * t + 0.5f);
    const int g = (int)(((a >> IM_COL32_G_SHIFT) & 0xFF) * u + ((b >> IM_COL32_G_SHIFT) & 0xFF) * t + 0.5f);
    const int bl= (int)(((a >> IM_COL32_B_SHIFT) & 0xFF) * u + ((b >> IM_COL32_B_SHIFT) & 0xFF) * t + 0.5f);
    const int al= (int)(((a >> IM_COL32_A_SHIFT) & 0xFF) * u + ((b >> IM_COL32_A_SHIFT) & 0xFF) * t + 0.5f);
    return IM_COL32(r, g, bl, al);
}

// Fill a CONVEX polygon with a horizontal gradient as ONE mesh (per-vertex colour lerped along x),
// with the same 1px alpha-0 fringe ring ImDrawList::AddConvexPolyFilled builds. This exists because
// a translucent tint CANNOT be assembled from several abutting fills: every shared edge either
// double-covers (alpha compounds into a visible crescent) or under-covers (AA hairline). One mesh
// touches every pixel exactly once, so there is nothing to seam and nothing to snap.
static void fillConvexHGradient(ImDrawList* dl, const ImVec2* pts, int n,
                                float gx0, float gx1, ImU32 colL, ImU32 colR) {
    if (n < 3) return;
    const ImVec2 uv = dl->_Data->TexUvWhitePixel;
    const float span = gx1 - gx0;
    auto colAt = [&](float x) {
        const float t = (span > 0.0f) ? ImClamp((x - gx0) / span, 0.0f, 1.0f) : 0.0f;
        return lerpU32(colL, colR, t);
    };
    auto edgeNormal = [&](int i0, int i1) {
        float dx = pts[i1].x - pts[i0].x, dy = pts[i1].y - pts[i0].y;
        const float d2 = dx * dx + dy * dy;
        if (d2 > 0.0f) { const float inv = ImRsqrt(d2); dx *= inv; dy *= inv; }
        return ImVec2(dy, -dx);
    };

    if (dl->Flags & ImDrawListFlags_AntiAliasedFill) {
        const float aa = dl->_FringeScale;
        dl->PrimReserve((n - 2) * 3 + n * 6, n * 2);
        const unsigned int inner = dl->_VtxCurrentIdx;      // vertex 2*i   (opaque, inset by aa/2)
        const unsigned int outer = dl->_VtxCurrentIdx + 1;  // vertex 2*i+1 (alpha 0, outset by aa/2)
        for (int i = 2; i < n; ++i) {                        // interior fan over the inner ring
            dl->_IdxWritePtr[0] = (ImDrawIdx)inner;
            dl->_IdxWritePtr[1] = (ImDrawIdx)(inner + ((i - 1) << 1));
            dl->_IdxWritePtr[2] = (ImDrawIdx)(inner + (i << 1));
            dl->_IdxWritePtr += 3;
        }
        ImVec2 nPrev = edgeNormal(n - 1, 0);
        for (int i1 = 0, i0 = n - 1; i1 < n; i0 = i1++) {
            const ImVec2 nNext = edgeNormal(i1, (i1 + 1) % n);
            ImVec2 dm((nPrev.x + nNext.x) * 0.5f, (nPrev.y + nNext.y) * 0.5f);
            float dm2 = dm.x * dm.x + dm.y * dm.y;          // miter fix-up (IM_FIXNORMAL2F)
            if (dm2 < 0.5f) dm2 = 0.5f;
            const float inv = (aa * 0.5f) / dm2;
            dm.x *= inv; dm.y *= inv;
            const ImU32 c = colAt(pts[i1].x);
            dl->_VtxWritePtr[0].pos = ImVec2(pts[i1].x - dm.x, pts[i1].y - dm.y);
            dl->_VtxWritePtr[0].uv = uv; dl->_VtxWritePtr[0].col = c;
            dl->_VtxWritePtr[1].pos = ImVec2(pts[i1].x + dm.x, pts[i1].y + dm.y);
            dl->_VtxWritePtr[1].uv = uv; dl->_VtxWritePtr[1].col = c & ~IM_COL32_A_MASK;
            dl->_VtxWritePtr += 2;
            dl->_IdxWritePtr[0] = (ImDrawIdx)(inner + (i1 << 1));
            dl->_IdxWritePtr[1] = (ImDrawIdx)(inner + (i0 << 1));
            dl->_IdxWritePtr[2] = (ImDrawIdx)(outer + (i0 << 1));
            dl->_IdxWritePtr[3] = (ImDrawIdx)(outer + (i0 << 1));
            dl->_IdxWritePtr[4] = (ImDrawIdx)(outer + (i1 << 1));
            dl->_IdxWritePtr[5] = (ImDrawIdx)(inner + (i1 << 1));
            dl->_IdxWritePtr += 6;
            nPrev = nNext;
        }
        dl->_VtxCurrentIdx += (unsigned int)(n * 2);
    } else {
        dl->PrimReserve((n - 2) * 3, n);
        const unsigned int base = dl->_VtxCurrentIdx;
        for (int i = 0; i < n; ++i) {
            dl->_VtxWritePtr[0].pos = pts[i];
            dl->_VtxWritePtr[0].uv = uv;
            dl->_VtxWritePtr[0].col = colAt(pts[i].x);
            dl->_VtxWritePtr += 1;
        }
        for (int i = 2; i < n; ++i) {
            dl->_IdxWritePtr[0] = (ImDrawIdx)base;
            dl->_IdxWritePtr[1] = (ImDrawIdx)(base + i - 1);
            dl->_IdxWritePtr[2] = (ImDrawIdx)(base + i);
            dl->_IdxWritePtr += 3;
        }
        dl->_VtxCurrentIdx += (unsigned int)n;
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
    // ONE MESH, painted once: the rounded-pill outline is filled as a single per-vertex-coloured
    // triangle fan plus an AA fringe ring. The earlier 3-slab + 4-corner-wedge construction could
    // not avoid the artifact it was built to hide — the deliberate wedge/slab overlap compounded
    // alpha into a visible arc in every corner.
    const float aL = strength;
    const float aR = std::max(0.02f, strength * 0.15f);
    const float rnd = ImClamp(rr, 0.0f, ImMin(mx.x - mn.x, mx.y - mn.y) * 0.5f);

    dl->PathClear();
    dl->PathRect(mn, mx, rnd, ImDrawFlags_RoundCornersAll);
    fillConvexHGradient(dl, dl->_Path.Data, dl->_Path.Size, mn.x, mx.x, dye(aL), dye(aR));
    dl->PathClear();

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
// Codepoints come from ui/glyph_ranges.h — the one table the chat fold and gui_lint also read.
static_assert(glyphs::maxCodepoint() <= IM_UNICODE_CODEPOINT_MAX,
              "glyph_ranges.h requests a codepoint ImWchar cannot hold");
static constexpr auto kUiGlyphRanges = glyphs::fontRanges<ImWchar>(glyphs::kBody);
static constexpr auto kCadSymbolRanges = glyphs::fontRanges<ImWchar>(glyphs::kCadSymbols);

static ImFont* addFont(ImGuiIO& io,
                       const std::string& path, float px, ImFont* fallback,
                       const std::string& cadSymPath /*may be empty*/) {
    ImFont* f = io.Fonts->AddFontFromFileTTF(path.c_str(), px, nullptr, kUiGlyphRanges.v);
    if (!f) return fallback;
    if (!cadSymPath.empty()) {
        ImFontConfig merge; merge.MergeMode = true; merge.PixelSnapH = true;
        // Match the host face's vertical metrics so merged glyphs sit on the same baseline.
        io.Fonts->AddFontFromFileTTF(cadSymPath.c_str(), px, &merge, kCadSymbolRanges.v);
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

// =========================================================================
// Control faces (kit buttons, combo frames, segmented pills, checkboxes)
// =========================================================================
namespace {
constexpr float kFaceTopLift    = 0.09f;  // top edge RGB pulled this far toward glassHighlight
constexpr float kFaceBottomSink = 0.07f;  // bottom edge RGB pulled this far toward glassShadow
constexpr float kFaceLipDark    = 0.50f;  // bottom lip = glassShadow alpha x this (dark theme)
constexpr float kFaceLipLight   = 0.80f;  // ... (light theme: its glassShadow is already softer)
constexpr float kFaceSheenDark  = 0.10f;  // top inner hairline alpha (glassHighlight RGB)
constexpr float kFaceSheenLight = 0.45f;

// `c` with its RGB pulled `t` of the way toward `role`'s RGB; c's alpha kept.
ImU32 mixRgb(ImU32 c, const float role[4], float t) {
    ImVec4 v = ImGui::ColorConvertU32ToFloat4(c);
    v.x += (role[0] - v.x) * t;
    v.y += (role[1] - v.y) * t;
    v.z += (role[2] - v.z) * t;
    return ImGui::ColorConvertFloat4ToU32(v);
}
}  // namespace

void controlFace(ImDrawList* dl, ImVec2 mn, ImVec2 mx, ImU32 fill, ImU32 border,
                 float roundingPx, float lift) {
    if (!dl || !(mx.x > mn.x) || !(mx.y > mn.y)) return;
    const Palette& p      = palette;
    const bool     dark   = activeTheme != Theme::Light;
    const float    hair   = std::max(1.0f, uiScale);
    const float    k      = clampF(lift, 0.f, 1.f);
    const float    r      = std::max(0.f, roundingPx);
    const float    fillA  = (float)((fill >> IM_COL32_A_SHIFT) & 0xFF) / 255.f;
    const float    styleA = ImGui::GetStyle().Alpha;

    if (fillA > 0.f) {
        // 1. Elevation: a 1-px lip peeking out under the bottom edge. Scaled by fill alpha
        //    squared so a faint glass-tinted field never darkens through its own translucency.
        if (k > 0.f) {
            const float lipA = p.glassShadow[3] * (dark ? kFaceLipDark : kFaceLipLight) * k *
                               fillA * fillA * styleA;
            dl->AddRectFilled(ImVec2(mn.x, mn.y + hair), ImVec2(mx.x, mx.y + hair),
                              roleAlphaU32(p.glassShadow, lipA), r);
        }
        // 2. Fill with a gentle vertical tint (lighter top, deeper bottom); vertex alpha kept so
        //    the anti-aliased fringe stays soft.
        const int v0 = dl->VtxBuffer.Size;
        dl->AddRectFilled(mn, mx, fill, r);
        if (k > 0.f) {
            ImGui::ShadeVertsLinearColorGradientKeepAlpha(
                dl, v0, dl->VtxBuffer.Size, mn, ImVec2(mn.x, mx.y),
                mixRgb(fill, p.glassHighlight, kFaceTopLift * k),
                mixRgb(fill, p.glassShadow, kFaceBottomSink * k));
            // 3. Top inner sheen, kept clear of the corner arcs.
            const float inset = std::max(r, hair * 2.0f);
            if (mx.x - mn.x > inset * 2.0f) {
                const float y = mn.y + hair;
                dl->AddLine(ImVec2(mn.x + inset, y), ImVec2(mx.x - inset, y),
                            roleAlphaU32(p.glassHighlight, (dark ? kFaceSheenDark : kFaceSheenLight) *
                                                               k * fillA * styleA),
                            hair);
            }
        }
    }
    // 4. Soft 1-px edge.
    if ((border >> IM_COL32_A_SHIFT) & 0xFF) dl->AddRect(mn, mx, border, r, 0, hair);
}

ImU32 controlBorderU32(float alphaMul) {
    return roleAlphaU32(palette.controlBorder,
                        palette.controlBorder[3] * alphaMul * ImGui::GetStyle().Alpha);
}

void setViewportBackdrop(const ViewportBackdropInfo& b) { gViewportBackdrop = b; }
const ViewportBackdropInfo& viewportBackdrop() { return gViewportBackdrop; }

void setLuminanceSampler(LuminanceSampler fn) { gLuminanceSampler = fn; }
float sampleLuminance(ImVec2 mn, ImVec2 mx) {
    const float lum = gLuminanceSampler ? gLuminanceSampler(mn, mx) : -1.f;
    return std::isfinite(lum) && lum >= 0.f && lum <= 1.f ? lum : -1.f;
}

void setBackdropStatsSampler(BackdropStatsSampler fn) { gBackdropStatsSampler = fn; }
bool sampleBackdropStats(ImVec2 mn, ImVec2 mx, BackdropStats& out) {
    BackdropStats stats;
    if (!gBackdropStatsSampler || !gBackdropStatsSampler(mn, mx, stats) ||
        !std::isfinite(stats.lum) || stats.lum < 0.f || stats.lum > 1.f ||
        !std::isfinite(stats.variance) || stats.variance < 0.f || stats.variance > 0.25f ||
        !std::isfinite(stats.hue) || stats.hue < 0.f || stats.hue > 360.f ||
        !std::isfinite(stats.sat) || stats.sat < 0.f || stats.sat > 1.f) {
        out = BackdropStats{};
        return false;
    }
    out = stats;
    return true;
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
    if (!std::isfinite(avgLuminance) || avgLuminance < 0.f || avgLuminance > 1.f) {
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

    State& st = states[id];
    const int frame = ImGui::GetFrameCount();
    if (st.lastFrame == frame) return st.cur;   // cache fallback as well as live surfaces
    st.lastFrame = frame;

    // Dark theme: panes, pinned columns and dialogs follow the THEME, not the backdrop. The
    // viewport keeps its light studio gradient in both themes (C25), so a sampled surface came
    // back light with dark ink inside dark chrome (mixed theme, theme-colored frames reading as
    // black blocks), and a stale sample from a viewport hidden behind PLM/AI/CAM bodies left
    // those panes light or their ink dark-on-dark. Only HUD chips drawn ON the viewport adapt.
    if (activeTheme == Theme::Dark && group != GlassGroup::Hud) {
        st.lum    = -1.f;   // a later switch to light snaps to its first live sample
        st.dynHue = -1.f;
        st.dynSat = st.dynVar = 0.f;
        st.cur    = adaptiveSurface(-1.f, group);
        st.cur.themeTone = true;
        return st.cur;
    }

    const float sampled = sampleLuminance(rect.Min, rect.Max);
    if (sampled < 0.f) {
        // A live texture can temporarily lack a grid (resize, pending/failed readback).
        // Keep its last contrast pair rather than switching only the ink to palette white.
        if (viewportBackdrop().valid() && st.lum >= 0.f) return st.cur;
        st.lum = -1.f;
        st.dynHue = -1.f;
        st.dynSat = st.dynVar = 0.f;
        st.cur = adaptiveSurface(-1.f, group);
        return st.cur;
    }

    const float dt = ImGui::GetIO().DeltaTime;
    if (st.lum < 0.f) {
        st.lum = sampled;   // snap on first sample for this id
        st.dark = sampled > 0.50f;
    } else {
        st.lum = approach(st.lum, sampled, dt, kAdaptiveTau);
    }

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
    if (bd.valid() && !surf.themeTone) {
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
        // Layered fallback when no live backdrop (first frame / hidden viewport), and the theme
        // glass for themeTone surfaces (dark theme panes must not show the light backdrop).
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

void pinnedColumnSurface(ImDrawList* dl, const ImRect& rect, GlassGroup group) {
    if (activeTheme != Theme::Dark || !dl || rect.GetWidth() <= 0.f || rect.GetHeight() <= 0.f)
        return;
    // Same bubble radius rule as the column windows: the group's rounding knob (applied inside
    // glassPanel) wins, else the 12 dp column default.
    constexpr float kColumnRoundingDp = 12.0f;
    AdaptiveSurface surf = adaptiveSurface(-1.f, group);
    surf.themeTone = true;
    glassPanel(dl, rect, GlassLevel::Regular, kColumnRoundingDp, surf);
}

// "Pill on a hairline track" scrollbar — shared look for the doc-tab file strip and the
// FeatureManager body (see ui_theme.h). A thin, low-alpha full-length track line with a thicker,
// fully-rounded pill thumb riding on it. Pure draw; caller owns geometry + drag.
void drawScrollPill(ImDrawList* dl, const ImVec2& trackMin, const ImVec2& trackMax,
                    float thumbA, float thumbB, bool vertical, bool hovered,
                    const AdaptiveSurface& surf, float thickness) {
    const float s = ui::uiScale;
    // A thin track runs down the CENTER of the band; the thumb is the SAME thickness as the track
    // (no fatter oval bulge — the program-wide default), just a brighter, rounded segment of
    // the same line so it reads as a minimal hairline scrollbar. Callers may widen both together.
    const float trackThin = thickness > 0.0f ? thickness * s : 2.0f * s;
    const float pillThick = trackThin;
    const float outlineW = 1.0f * s;
    const ImU32 trackCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textTertiary.x, surf.textTertiary.y, surf.textTertiary.z, 0.16f));
    // Accent-tinted pill with a thin, higher-alpha outline of the same hue so the bar reads as a
    // distinct colored control against the glass background instead of a barely-there gray sliver.
    const ImU32 pillCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], hovered ? 0.85f : 0.55f));
    const ImU32 pillOutline = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], hovered ? 1.0f : 0.85f));
    if (vertical) {
        const float cx = (trackMin.x + trackMax.x) * 0.5f;
        dl->AddRectFilled(ImVec2(cx - trackThin * 0.5f, trackMin.y),
                          ImVec2(cx + trackThin * 0.5f, trackMax.y), trackCol, trackThin * 0.5f);
        const ImVec2 pmn(cx - pillThick * 0.5f, thumbA), pmx(cx + pillThick * 0.5f, thumbB);
        dl->AddRectFilled(pmn, pmx, pillCol, pillThick * 0.5f);
        dl->AddRect(pmn, pmx, pillOutline, pillThick * 0.5f, 0, outlineW);
    } else {
        const float cy = (trackMin.y + trackMax.y) * 0.5f;
        dl->AddRectFilled(ImVec2(trackMin.x, cy - trackThin * 0.5f),
                          ImVec2(trackMax.x, cy + trackThin * 0.5f), trackCol, trackThin * 0.5f);
        const ImVec2 pmn(thumbA, cy - pillThick * 0.5f), pmx(thumbB, cy + pillThick * 0.5f);
        dl->AddRectFilled(pmn, pmx, pillCol, pillThick * 0.5f);
        dl->AddRect(pmn, pmx, pillOutline, pillThick * 0.5f, 0, outlineW);
    }
}

// Custom vertical scrollbar for the current window/child (see ui_theme.h). Reads the live scroll
// state, draws the pill overlay on the right edge, and drags to scroll. No-op if nothing overflows.
void drawWindowPillScrollbarV(const AdaptiveSurface& surf, const PillScrollbarStyle& style) {
    const float scrollMax = ImGui::GetScrollMaxY();
    // Ignore sub-padding phantom overflow: a child with AlwaysUseWindowPadding + a fixed height can
    // report a few px of ScrollMaxY even when the content visually fits (e.g. collapsed tree
    // sections), which showed a spurious bar. Only show it for a real overflow bigger than THIS
    // window's own bottom padding (not the global style's: a zero-padding scroll body such as
    // kit::panelBodyBegin would otherwise hide a real overflow of up to ~13 px with no bar). Hosts
    // that key anything off "the bar shows" must use the same test (kit_panel's scroll cues do).
    const float phantom = ImGui::GetCurrentWindow()->WindowPadding.y + 4.0f * ui::uiScale;
    if (scrollMax <= phantom) return;   // fits — no bar (hidden entirely, not just faded)
    const float s = ui::uiScale;
    const ImVec2 wpos = ImGui::GetWindowPos();
    const ImVec2 wsz = ImGui::GetWindowSize();
    const float scrollY = ImGui::GetScrollY();
    const float viewH = wsz.y;
    const float contentH = viewH + scrollMax;

    // Track hugs the right edge; a little top/bottom margin so the pill's rounded ends aren't
    // flush with the window corners. The DRAG strip stays comfortably wide even when the drawn
    // pill is thin, so a hairline bar is still grabbable.
    const float thick = style.thickness > 0.0f ? style.thickness * s : 2.0f * s;
    const float bandW = std::max(10.0f * s, thick + 4.0f * s);
    const float margin = 3.0f * s;
    const float rightX = wpos.x + wsz.x;
    const float trackX0 = rightX - bandW;
    const float trackTop = wpos.y + margin;
    const float trackBot = wpos.y + wsz.y - margin;
    const float trackLen = trackBot - trackTop;
    // Pill band: exactly `thick` wide so drawScrollPill centres the line on it. Flush against the
    // window's right edge when asked, otherwise centred in the drag strip (the historical look).
    const float pillX1 = style.flushRight ? rightX : trackX0 + (bandW + thick) * 0.5f;
    const float pillX0 = pillX1 - thick;

    const float thumbLen = std::max(24.0f * s, trackLen * (viewH / contentH));
    const float thumbTop = trackTop + (scrollY / scrollMax) * (trackLen - thumbLen);
    const float thumbBot = thumbTop + thumbLen;

    // Drag interaction — a fixed-position (screen-space) hit strip along the track, so it doesn't
    // move with the scrolled content.
    //
    // The strip MUST NOT contribute to the window's content size. It is placed at a SCREEN
    // position, so its local offset is (scrollY + …): ImGui folds that into CursorMaxPos and
    // hence ContentSize, making ScrollMaxY grow by roughly (WindowPadding.y - margin) on top of
    // whatever the caller already scrolled to. That ratchets a few px of extra range every frame,
    // which is the "I can keep scrolling past the end into blank space" bug — in the Settings
    // modal and every other pane that paints this bar. Snapshot CursorMaxPos and the cursor
    // around the item and restore both, so the bar is purely an overlay.
    ImGuiWindow* win = ImGui::GetCurrentWindow();
    const ImVec2 savedCursorMax = win->DC.CursorMaxPos;
    const ImVec2 savedCursor = ImGui::GetCursorPos();
    ImGui::SetCursorScreenPos(ImVec2(trackX0, trackTop));
    ImGui::InvisibleButton("##pillscroll_v", ImVec2(bandW, trackLen));
    const bool hov = ImGui::IsItemHovered() || ImGui::IsItemActive();
    if (ImGui::IsItemActive()) {
        const float my = ImGui::GetIO().MousePos.y;
        const float t = (my - trackTop - thumbLen * 0.5f) / std::max(1.0f, trackLen - thumbLen);
        ImGui::SetScrollY(std::min(std::max(t, 0.0f), 1.0f) * scrollMax);
    }
    win->DC.CursorMaxPos = savedCursorMax;
    ImGui::SetCursorPos(savedCursor);
    drawScrollPill(ImGui::GetWindowDrawList(), ImVec2(pillX0, trackTop),
                   ImVec2(pillX1, trackBot), thumbTop, thumbBot,
                   /*vertical=*/true, hov, surf, style.thickness);
}

// Inline busy indicator (see ui_theme.h). One ImGui item pair — a reserved square that the mark
// is painted into, then the label — so callers keep whatever layout they already had around the
// old text-only spinner.
void busyLabel(const char* label, ImU32 col) {
    if (col == 0)
        col = ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    const float box = ImGui::GetTextLineHeight();
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(box, box));
    // Below the legibility floor the wireframe is a smudge; a filled accent dot at least reads as
    // a state marker. Only reachable at very small font scales.
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 c(p.x + box * 0.5f, p.y + box * 0.5f);
    if (box >= kTesseractSpinnerMinPx)
        drawTesseractSpinner(dl, c, box, ImGui::GetTime(), col);
    else
        dl->AddCircleFilled(c, box * 0.25f, col);
    if (label && *label) {
        ImGui::SameLine(0.0f, ImGui::GetStyle().ItemInnerSpacing.x);
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextUnformatted(label);
        ImGui::PopStyleColor();
    }
}

// Circular fraction gauge (see ui_theme.h). Pure draw: a full track ring plus an arc that
// sweeps clockwise from 12 o'clock for `frac` of the circle. Segment count scales with the
// radius so a 7px composer ring and a 20px settings ring are both smooth without overdrawing.
void drawFractionRing(ImDrawList* dl, const ImVec2& center, float radius, float thickness,
                      float frac, ImU32 trackCol, ImU32 arcCol) {
    if (!dl || radius <= 0.0f) return;
    if (frac < 0.0f) frac = 0.0f;
    if (frac > 1.0f) frac = 1.0f;
    const float t = thickness < 1.0f ? 1.0f : thickness;
    const int segs = (int)std::clamp(radius * 2.4f, 16.0f, 64.0f);
    dl->PathArcTo(center, radius, 0.0f, IM_PI * 2.0f, segs);
    dl->PathStroke(trackCol, ImDrawFlags_None, t);
    if (frac <= 0.0f) return;
    // -pi/2 == 12 o'clock in ImGui's y-down screen space.
    const float a0 = -IM_PI * 0.5f;
    dl->PathArcTo(center, radius, a0, a0 + IM_PI * 2.0f * frac,
                  std::max(3, (int)(segs * frac) + 2));
    dl->PathStroke(arcCol, ImDrawFlags_None, t);
}

void drawStackedFractionRing(ImDrawList* dl, const ImVec2& center, float radius, float thickness,
                             const RingSpan* spans, int count, ImU32 trackCol) {
    if (!dl || radius <= 0.0f) return;
    const float t = thickness < 1.0f ? 1.0f : thickness;
    const int segs = (int)std::clamp(radius * 2.4f, 16.0f, 64.0f);
    dl->PathArcTo(center, radius, 0.0f, IM_PI * 2.0f, segs);
    dl->PathStroke(trackCol, ImDrawFlags_None, t);
    if (!spans || count <= 0) return;
    // Clamp the TOTAL, not each span: an over-budget sum must saturate the ring rather than wrap
    // past 12 o'clock, which would draw a nearly-empty gauge for a nearly-full window.
    float used = 0.0f;
    const float a0 = -IM_PI * 0.5f;   // 12 o'clock in ImGui's y-down screen space
    for (int i = 0; i < count; ++i) {
        float f = spans[i].frac;
        if (f <= 0.0f) continue;
        if (used >= 1.0f) break;
        if (used + f > 1.0f) f = 1.0f - used;
        const float from = a0 + IM_PI * 2.0f * used;
        const float to   = from + IM_PI * 2.0f * f;
        dl->PathArcTo(center, radius, from, to, std::max(3, (int)(segs * f) + 2));
        dl->PathStroke(spans[i].col, ImDrawFlags_None, t);
        used += f;
    }
}

// Magic-keyword ink (see ui_theme.h). HSL so the keyword table's hue sweeps (omp's
// magic-keywords.ts rows) port verbatim. omp paints S=90% L=62% on a dark terminal; the same
// stop on bright glass washes out, so a bright surface gets a deeper, slightly less saturated
// stop that keeps body-text contrast. The glint lifts lightness toward white on dark glass and
// toward the saturated mid-tone on bright glass, so the shimmer reads on both.
ImU32 magicKeywordInk(float hueDeg, bool brightSurface, float glint, float alpha) {
    float h = std::fmod(hueDeg, 360.0f);
    if (h < 0.0f) h += 360.0f;
    glint = std::clamp(glint, 0.0f, 1.0f);
    const float s = brightSurface ? 0.78f : 0.90f;
    const float l = brightSurface ? 0.42f + 0.12f * glint : 0.64f + 0.20f * glint;
    const float c = (1.0f - std::fabs(2.0f * l - 1.0f)) * s;
    const float hp = h / 60.0f;
    const float x = c * (1.0f - std::fabs(std::fmod(hp, 2.0f) - 1.0f));
    float r = 0, g = 0, b = 0;
    if (hp < 1.0f)      { r = c; g = x; }
    else if (hp < 2.0f) { r = x; g = c; }
    else if (hp < 3.0f) { g = c; b = x; }
    else if (hp < 4.0f) { g = x; b = c; }
    else if (hp < 5.0f) { r = x; b = c; }
    else                { r = c; b = x; }
    const float m = l - c * 0.5f;
    return ImGui::ColorConvertFloat4ToU32(
        ImVec4(r + m, g + m, b + m, std::clamp(alpha, 0.0f, 1.0f)));
}

}  // namespace ui
