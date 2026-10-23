// Chudware UI theme: named palette roles, two palettes (dark + SolidWorks-light),
// runtime-switchable. applyTheme(Theme, scale) fills ImGuiStyle + the global palette;
// all panels read ONLY role fields — never hardcode ImVec4/ImU32 colors in panel code.
#pragma once
#include <cstdint>
#include <string>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (adaptivePanel/glassPanel rects), ImHashStr (panelId)

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

// Shared Measure reference and Interference emphasis colors, matching the MCP view.
extern const float evalEmphasisPalette[10][4];

// =========================================================================
// Shared UI contract (chud-ui-shared-v1) — assets/ui-shared-manifest.json
// =========================================================================
// Chudware and Chudcode share a product-UI *vocabulary* — semantic role names, Tone/Density
// naming, animation tokens, usage thresholds, and font provenance — never widget code. The
// manifest is a byte-identical copy of the Chudcode original; scripts/verify-ui-shared.py
// cross-checks it against these constants, ui/widgets/kit.h (Tone/Density), ui/anim.h
// (duration tokens), ui/icons.h (cell sizes), and the vendored font bytes. Rename or retune
// any of these only together with the manifest, or the verifier fails the drift exactly.
// CAD-only roles (viewport*, sketch*, sim*, cam*, plane*, …) are deliberately outside the
// shared vocabulary and stay product-owned.
inline constexpr const char* kUiSharedSchema = "chud-ui-shared-v1";
inline constexpr int kUiSharedContractVersion = 1;

// Fraction-of-budget ladder shared by every "how much is used" readout (context-window
// gauge, usage rings, plan progress arcs): Ok below warning, Warn in [warning, critical),
// Err at or above critical. Values are the shared contract's usageThresholds.
inline constexpr float kUsageWarnFrac = 0.50f;
inline constexpr float kUsageCritFrac = 0.90f;

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
    float textDim[4]       = {0.620f,0.660f,0.720f, 1};   // secondary / dim text (glass rows)
    float textDisabled[4]  = {0.471f,0.502f,0.549f, 1};

    // ---- accent (PRIMARY: what an operation PRODUCED) ----
    // Shared by both palettes and user-overridable — see setAccentColors. Dim/Soft/Mid are
    // DERIVED from the base at install time; the literals below are only the out-of-box pair
    // (#F28216 orange / #0078D7 blue) so a default-constructed Palette matches what ships.
    float accent[4]        = {0.949f,0.510f,0.086f, 1};   // orange #F28216
    float accentDim[4]     = {0.664f,0.357f,0.060f, 1};
    float accentSoft[4]    = {0.949f,0.510f,0.086f, 0.22f};
    float accentMid[4]     = {0.949f,0.510f,0.086f, 0.40f};

    // ---- accent2 (SECONDARY: direction / annotation cue) ----
    // The ribbon's 3D icon tiles bake direction arrows in this hue while the operation's result
    // is baked in the primary, so the chrome must carry BOTH roles or a retinted ribbon reads as
    // a different product than the panel it sits on. Same Dim/Soft/Mid contract as the accent.
    float accent2[4]       = {0.000f,0.471f,0.843f, 1};   // blue #0078D7
    float accent2Dim[4]    = {0.000f,0.330f,0.590f, 1};
    float accent2Soft[4]   = {0.000f,0.471f,0.843f, 0.22f};
    float accent2Mid[4]    = {0.000f,0.471f,0.843f, 0.40f};

    // ---- interactive ----
    float button[4]        = {0.149f,0.176f,0.220f, 1};
    float buttonHovered[4] = {0.184f,0.224f,0.275f, 1};
    float buttonActive[4]  = {0.216f,0.259f,0.318f, 1};

    // ---- frame / input ----
    float frameBg[4]       = {0.129f,0.149f,0.184f, 1};
    float frameBgHovered[4]= {0.165f,0.192f,0.235f, 1};
    float frameBgActive[4] = {0.192f,0.224f,0.271f, 1};

    // ---- viewport ----
    // C25: the 3D viewport is light in BOTH themes (SolidWorks studio look).
    float viewportTop[4]   = {0.957f,0.965f,0.976f, 1};   // bg gradient top    (#F4F6F9)
    float viewportMid[4]   = {0.851f,0.867f,0.894f, 1};   // bg gradient mid    (#D9DDE4)
    float viewportBottom[4]= {0.933f,0.941f,0.953f, 1};   // bg floor glow      (#EEF0F3)
    float viewportEdge[4]  = {0.090f,0.100f,0.120f, 1};   // model feature-edge lines
    float partDefault[4]   = {0.776f,0.792f,0.847f, 1};   // default body color (#C6CAD8)
    float groundShadow[4]  = {0.200f,0.220f,0.260f, 0.35f}; // soft contact shadow (a = max)
    float overlayBg[4]     = {0.039f,0.055f,0.075f, 0.843f}; // pill overlays on 3D view
    float overlayBorder[4] = {0.949f,0.510f,0.086f, 0.275f};   // accent-derived (setAccentColors)
    float overlayText[4]   = {0.886f,0.906f,0.941f, 0.941f};
    // W4-K: SolidWorks-style datum planes — subtle light-blue translucent fill + thin blue
    // outline (alpha in [3] is the base; the renderer scales it per emphasis state).
    float planeFill[4]     = {0.380f,0.620f,0.910f, 0.085f};
    float planeOutline[4]  = {0.290f,0.540f,0.870f, 0.700f};

    // ---- selection / hover ----
    // Selection is the persistent state and must read as more emphatic than the transient hover
    // wash, so its alpha sits above hoverHighlight's (previously inverted: 0.22 vs 0.40, which made
    // a selected-but-unhovered face look lighter/weaker than a merely-hovered one).
    float selectionHighlight[4] = {0.949f,0.510f,0.086f, 0.50f};
    float hoverHighlight[4]     = {0.949f,0.510f,0.086f, 0.40f};

    // ---- C24: orientation-triad axis colors (X/Y/Z) ----
    float axisX[4]         = {0.910f,0.380f,0.380f, 1};   // red
    float axisY[4]         = {0.470f,0.820f,0.450f, 1};   // green
    float axisZ[4]         = {0.420f,0.620f,0.960f, 1};   // blue

    // ---- in-viewport simulation / flow / CAM overlay roles (viewport_panel.cpp) ----
    // Promoted from viewport_panel file-statics so themes can tune them; the light theme darkens
    // the translucent washes slightly so they hold on the light canvas.
    float simFixture[4]    = {0.18f,0.80f,0.36f, 0.45f}; // fixture-face wash (green)
    float simLoad[4]       = {0.95f,0.27f,0.20f, 0.45f}; // load-face wash (red)
    float simArrow[4]      = {0.95f,0.27f,0.20f, 1};     // load arrows
    float simGlyph[4]      = {0.10f,0.66f,0.30f, 1};     // fixture ground glyphs
    float simMesh[4]       = {0.55f,0.74f,0.95f, 0.55f}; // mesh-preview wireframe
    float flowArrow[4]     = {0.28f,0.70f,0.98f, 1};     // incoming-flow arrows (bright blue)
    float flowDomain[4]    = {0.45f,0.55f,0.68f, 0.55f}; // wind-tunnel domain box
    float flowInlet[4]     = {0.30f,0.80f,0.85f, 0.75f}; // inlet plane (teal)
    float camRapid[4]      = {0.78f,0.24f,0.20f, 0.55f}; // CAM backplot: rapid (faint red)
    float camLead[4]       = {0.31f,0.78f,0.47f, 0.95f}; //   lead in/out (green)
    float camPlunge[4]     = {0.31f,0.71f,0.86f, 0.95f}; //   plunge (cyan)
    float camCut[4]        = {0.35f,0.59f,0.94f, 1};     //   cut (blue)
    float camArc[4]        = {0.59f,0.51f,0.94f, 1};     //   arc (violet)
    float camStock[4]      = {0.55f,0.60f,0.68f, 0.50f}; //   stock box (grey-blue)
    float camTool[4]       = {0.95f,0.80f,0.25f, 0.90f}; //   tool marker (steel yellow)

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
    // Associative Convert Entities — entities owned by an external ref render in a
    // distinct violet (fully-defined ink would hide their provenance); a STALE ref (source no
    // longer resolves; last-good geometry kept) renders dashed in the warning amber.
    float sketchExternal[4]      = {0.500f,0.380f,0.820f, 1};   // live external-ref entities
    float sketchExternalStale[4] = {0.953f,0.610f,0.220f, 1};   // stale (dashed) entities

    // ---- status ----
    // statusOk is SUCCESS, not brand: it used to be the accent literal verbatim, so a user
    // recoloring the app would have silently recolored "ok" too. It is green in both palettes now.
    float statusOk[4]   = {0.459f,0.859f,0.549f, 1};   // green
    float statusWarn[4] = {0.953f,0.761f,0.420f, 1};   // amber
    float statusErr[4]  = {0.961f,0.549f,0.420f, 1};   // red-orange

    // ---- console colors ----
    float consoleDefault[4]= {0.780f,0.820f,0.880f, 1};
    float consoleOk[4]     = {0.459f,0.859f,0.549f, 1};   // success green, not the accent
    float consoleWarn[4]   = {0.953f,0.761f,0.420f, 1};
    float consoleErr[4]    = {0.961f,0.549f,0.420f, 1};
    float consoleDim[4]    = {0.620f,0.660f,0.720f, 1};

    // ---- code / diff ----
    float chipBg[4]        = {0.949f,0.510f,0.086f, 0.30f};   // accent-derived (setAccentColors)
    float codeBg[4]        = {0.055f,0.065f,0.085f, 1};
    // NOTE: proseBg is ≈ the Copilot pane's dark base (windowBg ~0.082) since the panes
    // went opaque-dark, so prose cards now read by their BORDER, not fill contrast. If chat
    // content ever looks flat, lighten ~0.03 above windowBg so the cards regain a surface.
    float proseBg[4]       = {0.085f,0.100f,0.130f, 1};
    float proseText[4]     = {0.780f,0.820f,0.880f, 1};
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
    float assistantName[4] = {0.820f,0.860f,0.920f, 1};
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
    // glassShadow:    in-rect bottom hairline (glassPanel edge) and the shared dim-scrim black.
    float glassBg[4]        = {0.118f, 0.133f, 0.167f, 0.87f};
    float glassBorder[4]    = {0.230f, 0.275f, 0.365f, 0.80f};
    float glassHighlight[4] = {1.000f, 1.000f, 1.000f, 0.09f};
    float glassShadow[4]    = {0.000f, 0.000f, 0.000f, 0.40f};

    // ---- tree (feature-manager rows: hover wash + section header label) ----
    // treeHover:   accent-tinted row hover wash (low alpha so text stays legible).
    // treeSection: muted label color for collapsible section headers.
    float treeHover[4]      = {0.949f, 0.510f, 0.086f, 0.10f};   // accent-derived
    float treeSection[4]    = {0.620f, 0.660f, 0.720f, 1.0f};

    // ---- copilot blocks (chat action cards) ----
    // blockActionBg: action-card background — near-opaque so structured content reads clearly.
    float blockActionBg[4]  = {0.094f, 0.110f, 0.141f, 0.96f};

    // ---- controls (buttons, input/combo frames, checkboxes, segmented tracks) ----
    // controlBorder: the soft 1-px edge of every kit control — quieter than the window/popup
    // outline (ImGuiCol_Border), firmer than a separator (border). Paired with controlFace().
    float controlBorder[4]  = {0.290f, 0.330f, 0.420f, 0.60f};

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

// Out-of-box accent pair. PRIMARY is the color an operation's RESULT is drawn in (and the hue the
// baked 3D ribbon tiles paint new geometry with); SECONDARY is the direction cue (arrows, axes,
// annotation). Settings persists the user's override; these are the values it falls back to and
// the values the Appearance tab's "Reset to defaults" restores.
extern const float kDefaultAccentPrimary[3];    // #F28216 orange
extern const float kDefaultAccentSecondary[3];  // #0078D7 blue

// Install an accent pair: writes the accent*/accent2* families plus every accent-DERIVED wash
// (selection, hover, overlay border, chip fill, tree hover), re-points the ~18 accent-dependent
// ImGuiCol slots, and retints the baked icon atlas via ui::recolorIconAtlas so the ribbon tracks
// the chrome. The pair is remembered and re-installed by applyTheme(), because a theme switch
// re-runs the palette fill and would otherwise stomp the user's colors back to the default.
// Call once at startup (after loadSettings AND loadIcons) and on every Appearance-tab edit.
// GUI thread only.
void setAccentColors(const float primary[3], const float secondary[3]);

Fonts loadFonts(const std::string& assetsDir, float pxScale = 1.0f);

// Draw a glassmorphism panel background onto 'dl' covering [mn, mx].
// Layers (back to front): translucent base fill, vertical inner-shine gradient, border, top
// specular line. Deliberately NO offset drop shadow — controls at rest render flat. 'rounding' is
// a logical dp value (multiplied by ui::uiScale internally, so 8.0f matches the PopupRounding set
// by applyTheme at any DPI).
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding = 8.0f);
// Alpha-scaled variant for animated widgets (e.g. toasts): `alpha` (0..1) multiplies every glass
// role's alpha before drawing, so a fading card dims all five layers coherently.
void glassBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, float alpha);

// Soft-elevated control face shared by every kit control (buttons, combo frames, segmented pills,
// popover triggers, checkbox boxes). Back to front: a 1-px bottom lip in glassShadow (raised
// only), the rounded fill with a gentle vertical tint (lighter top, slightly deeper bottom), a top
// inner highlight hairline, then the 1-px `border` (alpha 0 = none). `fill`/`border` arrive
// already resolved for hover/press/disabled by the caller. `lift` 1 = resting raised control,
// 0 = flat (disabled, inset track); values between blend (a press sinks toward flat).
// `roundingPx` is PIXELS (metrics/kit tokens), NOT logical dp — unlike glassBackground.
void controlFace(ImDrawList* dl, ImVec2 mn, ImVec2 mx, ImU32 fill, ImU32 border,
                 float roundingPx, float lift = 1.0f);
// The theme's soft control edge (palette.controlBorder) at `alphaMul`, honouring style.Alpha.
ImU32 controlBorderU32(float alphaMul = 1.0f);

// Push/pop ImGuiStyle overrides for the BeginTooltip() + glassBackground() pattern.
// pushGlassTooltipStyle() sets PopupBg=transparent so ImGui's native rect fill does not
// compete with the custom-drawn glass layers. popGlassTooltipStyle() restores all pushed state.
// Callers of SetTooltip() do NOT need push/pop — ImGuiCol_PopupBg is the glassBg hue at a
// near-opaque alpha globally (applyTheme), so popups never let the rows beneath ghost through.
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

// Push/pop the chrome shared by every right-click context menu: opaque popupBg surface, glass
// border, and padding/item spacing sized so a MenuItem's hover band sits evenly inset 4 dp from
// every popup edge while labels sit 12 dp in. Hover/open rows use the soft accent wash, press
// the mid wash. Push BEFORE BeginPopup*/BeginPopupContext* and pop AFTER the if-block — the
// styles must stay live for the popup's whole draw, and the pair is balanced every frame
// whether or not the menu is open. Submenus (BeginMenu) inherit it.
void pushContextMenuStyle();
void popContextMenuStyle();

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
                        bool stripe = false, bool selected = false, float washAlpha = 0.42f);

// Selection emphasis = a thin GLOW BORDER around [mn,mx] (no fill, no left accent bar). A soft
// wide low-alpha stroke on the pill edge reads as a glow, with a crisp thin border on top. `accent`
// is a palette RGBA float[4] — pass palette.accent for the app color, or any per-element color to
// get a color-coded selection. This is the single source of the "selected" look across the app;
// recolor by passing a different `accent`.
void glassSelectionGlow(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding, const float accent[4]);

// Reusable COLOR-CODED row/element background — the "Layers"-style wash used across the app. Paints
// a translucent left->right tint of `tint` (RGB 0..1) over the frosted glass, bounded to the rounded
// pill. `strength` scales the tint alpha (0..1; the left edge is `strength`, the right edge fades to
// ~15%). When `selected`, a glow border in `tint` frames the element (via glassSelectionGlow). This
// is the single abstraction for color-coded surfaces — change the color simply by passing a different
// `tint`. Pass tint=palette.accent for the neutral app color.
void coloredRowWash(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding,
                    const float tint[3], float strength = 0.34f, bool selected = false);

// The "liquid glass" tinted card wash: frosted glassBackground() base plus a top-bright ->
// bottom-faint vertical gradient in `tint` (palette float[3], RGB 0..1). This is the exact
// style used for PLM Task/Issue/Milestone cards (see plmCardBegin/plmCardEnd in plm_pages.cpp)
// factored out here so any panel in the program can reuse the same background. `topAlpha` is
// the wash alpha at the top edge; the bottom edge fades to `topAlpha * bottomAlphaScale`.
void glassCardWash(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding,
                   const float tint[3], float topAlpha, float bottomAlphaScale = 0.30f);

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

// =========================================================================
// Liquid Glass v2 — adaptive contrast + backdrop-projected glass panels
// Ported from ui/imgui/src/theme/theme.{h,cpp} (see local://glass_host.md for the host
// contract). Over-viewport surfaces (left dock, Copilot, viewport HUD, doc-tab band, toasts)
// use these instead of the theme-dependent glass* helpers above, so text contrast tracks the
// LIVE viewport content identically in dark and light theme.
// =========================================================================

// iOS UIBlurEffect-style material levels — tint alphas 0.12 / 0.22 / 0.35 / 0.52.
enum class GlassLevel { UltraThin, Thin, Regular, Thick };

// ---- Colored liquid glass (v2.1): per-group user styling -------------------
// Panel groups that can be styled independently. Global is the base; the other groups inherit
// any field they leave at its sentinel (see GlassStyle). Pass the group that OWNS a surface to
// adaptivePanel()/adaptiveSurface(); untagged call sites default to Global.
enum class GlassGroup : uint8_t { Global = 0, FeatureManager, Copilot, Hud, Count };

// One group's user knobs. SENTINELS (-1) mean 'inherit from Global'; for Global itself they
// resolve to the built-in default shown in the trailing comment. Default-constructed GlassStyle
// == today's glass GEOMETRY/opacity with dynamic color ON (the out-of-box look).
struct GlassStyle {
    float tintHue           = -1.f;   // scrim hue, degrees 0..360; <0 inherit (Global: 210)
    float tintSat           = -1.f;   // scrim saturation 0..1; <0 inherit (Global: 0 = neutral)
    float tintStrength      = -1.f;   // 0..1 tint mix into the scrim; <0 inherit (Global: 0)
    int   glassLevel        = -1;     // 0..3 forces UltraThin..Thick; -1 inherit (Global: call-site level)
    float opacityMul        = -1.f;   // scrim-alpha multiplier 0.25..2; <0 inherit (Global: 1)
    float rounding          = -1.f;   // logical dp override; <0 inherit (Global: call-site rounding)
    float textContrastBoost = -1.f;   // 0..1 extra scrim + halo; <0 inherit (Global: 0)
    int   dynamicColor      = -1;     // 1 on / 0 off / -1 inherit (Global: ON — auto max-contrast
                                      // tint/text vs the live backdrop is the out-of-box default)
};

// Whole user config: one style per group (index by (size_t)GlassGroup). Settings owns the
// persisted copy (config.json "appearance.glass"); ui_theme owns the active copy below.
struct GlassConfig {
    GlassStyle group[(size_t)GlassGroup::Count];
};

// Install the active config. Cheap + idempotent (early-outs when nothing changed), so the frame
// loop may push Settings every frame: ui::setGlassConfig(st.settings.glassCfg). Call once after
// loadSettings at startup and on every Appearance-tab edit. Independent of applyTheme() — theme
// toggles never reset glass knobs. GUI thread only.
void               setGlassConfig(const GlassConfig& cfg);
const GlassConfig& glassConfig();

// Fully-resolved (sentinel-free) style for a group, cached by setGlassConfig(). Safe to call
// before the first setGlassConfig (static defaults == default config resolution).
struct GlassStyleResolved {
    float tintHue = 210.f, tintSat = 0.f, tintStrength = 0.f;
    int   glassLevel = -1;          // -1 = keep the call-site GlassLevel
    float opacityMul = 1.f;
    float rounding = -1.f;          // -1 = keep the call-site rounding
    float textContrastBoost = 0.f;
    bool  dynamicColor = true;      // out-of-box: auto-tint from the live backdrop
};
const GlassStyleResolved& glassStyleResolved(GlassGroup g);

// Precomputed contrast surface for one panel/rect — see adaptivePanel() / adaptiveSurface().
struct AdaptiveSurface {
    ImVec4 tint;                             // scrim color+alpha to draw over the blurred backdrop
    ImVec4 textPrimary, textSecondary, textTertiary;
    bool   darkText = false;                 // true => dark-on-bright text
    float  scrim = 0.f;                      // extra contrast alpha already folded into tint.w
    // v2.1: which group's GlassStyleResolved applies (set by adaptivePanel/adaptiveSurface);
    // glassPanel reads level/rounding/opacity overrides through this.
    GlassGroup group = GlassGroup::Global;
    // v2.1: resolved textContrastBoost (+ the dynamic-color busyness term when active), consumed
    // by glassText()/glassIconHalo() halo alpha. 0 = today's halo.
    float contrastBoost = 0.f;
    // Tone pinned to the active theme (palette chrome) rather than sampled from the backdrop —
    // adaptivePanel() sets it for every non-HUD surface in the dark theme. glassPanel() then paints
    // the layered theme glass instead of the (light) viewport backdrop image.
    bool themeTone = false;
};

// Published once per frame by the viewport (after its render) so glassPanel()/adaptivePanel()
// can project any panel rect into backdrop UVs. screenMin/Max = the viewport IMAGE rect in
// absolute screen coords.
struct ViewportBackdropInfo {
    std::uintptr_t textureId = 0;
    ImVec2 uvMin{0, 0}, uvMax{1, 1};
    ImVec2 screenMin{0, 0}, screenMax{0, 0};
    bool   flipV = true;   // GL bottom-left texture: v runs bottom-up
    bool valid() const {
        return textureId != 0 && screenMax.x > screenMin.x && screenMax.y > screenMin.y;
    }
};
void                        setViewportBackdrop(const ViewportBackdropInfo& b);
const ViewportBackdropInfo& viewportBackdrop();

// Luminance bridge: the viewport registers a sampler; theme code consumes it. Screen coords in,
// 0..1 luminance out; negative => no live backdrop under that rect.
using LuminanceSampler = float (*)(ImVec2 mn, ImVec2 mx);
void  setLuminanceSampler(LuminanceSampler fn);
float sampleLuminance(ImVec2 mn, ImVec2 mx);   // -1 when unavailable or non-finite/out of range

// v2.1 dynamic color: aggregate backdrop statistics under a rect. hue/sat are the HSV of the
// MEAN RGB (hue of the mean, not mean of hues — circular-safe and cheap); variance is the
// luminance variance across the sampled grid cells (busyness proxy; the source grid is built
// from the BLURRED quarter-res buffer, so treat it as a low-frequency measure). The scalar
// LuminanceSampler above stays registered and untouched — this is a second, richer bridge the
// viewport registers alongside it.
struct BackdropStats {
    float lum = -1.f;      // mean Rec.709 luminance; -1 = unavailable
    float variance = 0.f;  // luminance variance across grid cells, 0..~0.25
    float hue = 0.f;       // mean-color hue, degrees 0..360
    float sat = 0.f;       // mean-color saturation 0..1
};
using BackdropStatsSampler = bool (*)(ImVec2 mn, ImVec2 mx, BackdropStats& out);
void setBackdropStatsSampler(BackdropStatsSampler fn);
// false => unavailable or invalid statistics; callers then behave exactly as if dynamicColor
// were off. On failure `out` is reset to its unavailable defaults.
bool sampleBackdropStats(ImVec2 mn, ImVec2 mx, BackdropStats& out);

// Stable, window-independent panel-surface id (ImHashStr) — the same id resolves to the same
// AdaptiveSurface no matter which window/TU looks it up (e.g. a pane vs. its own rows).
ImGuiID panelId(const char* label);

// Stateless surface builder. avgLuminance >= 0: theme-INDEPENDENT vibrancy tokens (identical
// constants in dark+light theme) — tone comes solely from the sampled luminance. avgLuminance
// < 0: palette fallback (chrome, no live backdrop); darkText = (activeTheme == Theme::Light).
// `group` selects the user GlassStyle (tint / opacity / contrast) applied to the scrim.
AdaptiveSurface adaptiveSurface(float avgLuminance, GlassGroup group = GlassGroup::Global);

// Hysteretic (.42/.58) + temporally smoothed (tau 0.24s) per-panel adapter. Samples luminance
// under `rect` via the registered sampler. `id` = any stable ImGuiID (use panelId()). Safe to
// call every frame; live AND fallback results are idempotent within a frame, GUI thread only.
// Missing readbacks retain the last live contrast pair while the backdrop remains live.
// `group` selects GlassStyle; pass the SAME group every frame for a given id.
// Dark theme: every group except Hud resolves to the theme surface (palette fallback,
// themeTone = true) — panes, pinned columns and dialogs follow the theme; only the HUD chips
// drawn ON the viewport (light studio gradient in both themes) keep sampling the backdrop.
const AdaptiveSurface& adaptivePanel(ImGuiID id, const ImRect& rect,
                                     GlassGroup group = GlassGroup::Global);

// Frosted panel: projects `rect` into the live backdrop's UV space (clamped => frosted
// edge-bleed for rects that spill past the viewport image) and tints with `surf`; falls back to
// the layered glassBackground() above when no backdrop is live or `surf.themeTone` is set.
// `rounding` < 0 uses the default panel rounding.
void glassPanel(ImDrawList* dl, const ImRect& rect, GlassLevel level, float rounding,
                const AdaptiveSurface& surf, float alpha = 1.f);

// Pinned overlay column surface (FeatureManager / Copilot columns, whose windows are
// NoBackground). Light theme: paints nothing — the column stays transparent over the light
// viewport and only its rows/cards carry glass. Dark theme: one theme glass slab over the whole
// column, so its theme-toned (light) ink never lands on the light viewport. Call right after the
// column's Begin, before any content, with the window rect and the column's group.
void pinnedColumnSurface(ImDrawList* dl, const ImRect& rect, GlassGroup group);

// Draw text with a contrast halo (1px offset soft shadow in the opposite tone) — for text
// painted directly on the viewport (legend, HUD labels, doc-name overlay).
void glassText(ImDrawList* dl, ImFont* font, float size, ImVec2 pos,
              const AdaptiveSurface& s, const char* text, const char* textEnd = nullptr);

// Contrast halo color for an icon/glyph drawn over a frosted adaptive surface: the vibrancy
// tone OPPOSITE the surface's text tone at ~0x50 alpha. alphaMul scales the alpha (HUD fades).
ImU32 glassIconHalo(const AdaptiveSurface& surf, float alphaMul = 1.0f);

// v2.1: max-contrast text color over an ARBITRARY tinted surface color (Rec.709 luminance
// split, same 0.5 threshold as adaptiveSurface's tone pick). Use for text painted on a custom
// tint that did not come from adaptiveSurface/adaptivePanel — those already carry textPrimary.
ImVec4 maxContrastTextOn(const ImVec4& surface);

// Shared "pill on a hairline track" scrollbar look — a thin full-length track line with a thicker
// rounded pill thumb riding on it. PURE draw (no interaction): the caller owns the geometry + any
// drag handling. `trackMin`/`trackMax` bound the full track rect; `thumbA`/`thumbB` are the pill's
// start/end along the scroll axis (screen coords). `vertical` picks the axis. Used by the doc-tab
// file strip and the FeatureManager body so both scrollbars read identically.
// `thickness` (unscaled px, 0 = the app default hairline) overrides the pill/track width.
void drawScrollPill(ImDrawList* dl, const ImVec2& trackMin, const ImVec2& trackMax,
                    float thumbA, float thumbB, bool vertical, bool hovered,
                    const AdaptiveSurface& surf, float thickness = 0.0f);

// Geometry overrides for drawWindowPillScrollbarV. The defaults reproduce the app-wide
// hairline look: a 2px pill running down the centre of a 10px band at the window's right
// edge. The drag strip stays wide enough to grab regardless of the pill's drawn width.
struct PillScrollbarStyle {
    float thickness = 0.0f;    // unscaled px; 0 = app default (2px)
    bool flushRight = false;   // true: pill sits ON the right edge instead of band-centred
};

// The copilot/agent chat's bar: 9px, sitting ON the right edge. Shared by the docked copilot
// panel and the AI tab's conversation pane so the same chat never looks different in two hosts.
// The pill is drawn INSIDE the host child's clip rect, so the host MUST reserve a right-hand
// lane for it (see kChatScrollbarLane) or content runs under the bar.
inline constexpr PillScrollbarStyle kChatScrollbar{9.0f, true};

// Right-hand gutter, in LOGICAL px, that a chat scroll child must reserve for kChatScrollbar:
// the 9px pill plus 4px of clearance so bubbles/cards never touch or slide under it. Callers
// scale it with ui::metrics::px().
inline constexpr float kChatScrollbarLane = 13.0f;

// FeatureManager-style custom vertical scrollbar for the CURRENT window/child. Call just before
// EndChild; reads GetScrollY/GetScrollMaxY + the window rect, draws the pill overlay on the right
// edge and handles drag (SetScrollY). No-op when the content doesn't overflow. Requires the child
// to carry ImGuiWindowFlags_NoScrollbar (so it replaces, not doubles, the default bar).
void drawWindowPillScrollbarV(const AdaptiveSurface& surf,
                              const PillScrollbarStyle& style = PillScrollbarStyle{});

// Circular fraction gauge: a full track ring plus a clockwise arc from 12 o'clock covering
// `frac` (0..1) of the circle. PURE draw — no layout, no interaction, no ImGui item. Shared by
// the Copilot composer's context-window / provider-limit rings and the Settings Accounts rows
// so every "how much of a budget is used" readout in the app has ONE shape.
void drawFractionRing(ImDrawList* dl, const ImVec2& center, float radius, float thickness,
                      float frac, ImU32 trackCol, ImU32 arcCol);

// One consecutive arc per span, laid clockwise from 12 o'clock over a single shared track.
// Same shape and thickness as drawFractionRing, so a stacked gauge and a plain one read as the
// same control; the point is that two DIFFERENT kinds of number (e.g. the fixed system-prompt
// baseline vs the conversation that grew on top of it) stay visually distinguishable instead of
// being summed into one indistinct arc. Fractions are of the WHOLE circle and are drawn in the
// order given; the total is clamped so an over-budget sum cannot wrap past 12 o'clock and read as
// nearly empty. PURE draw — no layout, no interaction, no ImGui item.
struct RingSpan {
    float frac = 0.0f;
    ImU32 col  = 0;
};
void drawStackedFractionRing(ImDrawList* dl, const ImVec2& center, float radius, float thickness,
                             const RingSpan* spans, int count, ImU32 trackCol);

// Inline "working" indicator: the brand mark turning in a text-line-sized box, then `label` on the
// same baseline. Replaces the ASCII dot / braille / |-\ cycles that used to be spliced into the
// label string itself — those could not be styled, could not be measured apart from the text, and
// each panel had invented its own. Advances the cursor like an ImGui::Text of the same content,
// so it drops into existing layout. Pass a null/empty `label` for the mark alone.
//
// `col` 0 (the default) uses the accent at the same 0.85 alpha the old ASCII spinners used.
void busyLabel(const char* label, ImU32 col = 0);

// Magic-keyword gradient ink: one stop of a keyword's HSL hue sweep (ui/copilot/magic_keywords.h),
// tuned for the surface it is painted on — lighter on dark glass, deeper on bright glass
// (`brightSurface` = AdaptiveSurface::darkText) so the word keeps body-text contrast in both
// themes. `glint` 0..1 lifts the stop for a shimmer's travelling highlight; `alpha` multiplies.
ImU32 magicKeywordInk(float hueDeg, bool brightSurface, float glint = 0.0f, float alpha = 1.0f);

}  // namespace ui
