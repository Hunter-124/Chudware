#pragma once
// kit.h — the app-wide visual vocabulary. ONE implementation of each composite widget.
//
// WHY THIS EXISTS
// ---------------
// Chudware grew four parallel, independent widget toolkits — copilot (ui/copilot/ui_common.cpp),
// PLM (ui/plm/plm_hub.h), Parts (ui/parts/parts_widgets.h) and Reverse Engineering
// (ui/reveng/reveng_widgets.h) — plus three more private ones inside ui/ai/. An audit counted
// EIGHT distinct "empty state" painters, SIX "section header" painters (three of them literally
// named sectionHeader with three different signatures), five chip painters, six progress/spinner
// idioms and fifteen inline copies of the card chrome. Every one of them used correct palette
// roles, so this was never a color bug: it is horizontal duplication, and it is why the product
// looks subtly different tab to tab.
//
// This header is the single vocabulary. The per-workspace helpers survive as thin domain
// wrappers (they carry sensible per-domain defaults and hundreds of call sites spell them that
// way), but they MUST forward here — no workspace paints its own card, header, chip, empty state,
// progress bar or status glyph any more.
//
// HARD RULES
//   * Colors come from ui::palette / the AdaptiveSurface passed in — never a literal ImU32/ImVec4.
//   * Sizes come from the density accessors below or ui::metrics::* — never a fresh pixel literal.
//   * Icons come from contracts/icons.md by name — never a Unicode glyph. The app font has no
//     box-drawing, arrow, check or bullet codepoints and renders them as '?'.
//   * Motion comes from ui::anim — never a hand-rolled smoothstep or a bare duration literal.
//
// GL/GUI thread only.

#include <string>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include "../../ui_theme.h"   // ui::palette, ui::AdaptiveSurface, ui::uiScale

namespace ui {
namespace kit {

// ---- semantic tone ---------------------------------------------------------------------------
// The kit owns the mapping from meaning to color, so "failed" looks identical in the Copilot
// task list, the PLM graph inspector and the RevEng stage stepper.
enum class Tone {
    Neutral,   // informational, no judgement       -> textDim
    Accent,    // the app's own emphasis            -> accent
    Ok,        // succeeded / healthy / in tolerance -> statusOk
    Warn,      // degraded / approximate / stale    -> statusWarn
    Err,       // failed / unavailable / blocking   -> statusErr
    Info,      // secondary emphasis, not a status  -> iterBuilt
};
ImU32  toneU32(Tone t, float alphaMul = 1.0f);
ImVec4 toneVec(Tone t, float alphaMul = 1.0f);
// Tone resolved for legibility ON a given adaptive surface (the surface may be a bright glass
// panel over the light viewport, where raw statusWarn washes out).
ImVec4 toneOn(Tone t, const AdaptiveSurface& surf);

// ---- density ---------------------------------------------------------------------------------
// The same widget is drawn at three rhythms in this product, which is precisely why metrics.h
// carries three near-identical token families (cpMenu*, cam*, re*) and cpChat* on top. Rather
// than four copies of every widget, the kit resolves its own padding from an ambient density
// the host pushes once around a pane.
//
//   Compact — a streaming conversation: cards land in dense runs (cpChat* rhythm).
//   Cozy    — a docked pane or menu view: the app default (cpMenu* / cam* rhythm).
//   Roomy   — a full-width wizard or workspace body (re* rhythm).
//
// pushDensity/popDensity nest. Default is Cozy.
enum class Density { Compact, Cozy, Roomy };
void    pushDensity(Density d);
void    popDensity();
Density density();

// Resolved geometry for the ACTIVE density. All already multiplied by ui::uiScale.
float padX();        // inside a row/card, horizontal
float padY();        // inside a row/card, vertical
float rowGap();      // between sibling rows/cards
float rowRound();    // row/card corner radius
float chipRound();   // chip/pill corner radius
float sectionGap();  // above a section header
float headGap();     // below a section header
float gutter();      // pane content inset

// ---- cards -----------------------------------------------------------------------------------
// A glass card is THE container for a logical group. Always paired with cardEnd(), including on
// the collapsed path.
//
//   `title`       may be null/empty for an untitled container.
//   `icon`        a contracts/icons.md name, or null.
//   `open`        non-null makes the card collapsible and persists the state through the caller;
//                 the disclosure animates through ui::anim, so pass the same pointer every frame.
//   `tint`        a palette float[3] for a color-coded card wash (glassCardWash), or null for the
//                 neutral glass. This is how a card carries meaning without a colored border.
//
// Returns false when the card is collapsed — skip the body, still call cardEnd().
bool cardBegin(const char* id, const char* title = nullptr, const char* icon = nullptr,
               bool* open = nullptr, const float tint[3] = nullptr);
void cardEnd();

// Right-aligns the next `widgetsWidth` px of widgets on the card's title row (status chips, small
// buttons). Call immediately after cardBegin() returns true, before any body content.
void cardHeaderRight(float widgetsWidth);

// ---- section header ----------------------------------------------------------------------------
// Accent label, optional leading icon, optional right-aligned trailing text, optional count.
// Emits sectionGap() above and headGap() below so callers stop hand-placing Dummy() spacers.
// `first` suppresses the leading gap for the first header in a pane.
void sectionHeader(const char* label, const char* icon = nullptr,
                   const char* trailing = nullptr, int count = -1, bool first = false);

// ---- rows ----------------------------------------------------------------------------------------
// The single list-row primitive: an optional leading icon or thumbnail, a title line, an optional
// dim secondary line beneath it, an optional right-aligned meta string, and trailing actions.
// One hover/selection treatment for the whole app, animated through ui::anim.
//
// Callers that need custom content inside the row use rowBegin/rowEnd and draw between them; the
// kit still owns the background, hover wash, selection glow, hit test and vertical rhythm.
struct RowStyle {
    const char* icon      = nullptr;  // contracts/icons.md name drawn in the leading slot
    ImTextureID thumb     = 0;        // overrides `icon` when non-zero (artifact/part previews)
    Tone        iconTone  = Tone::Neutral;
    const char* secondary = nullptr;  // dim second line
    const char* meta      = nullptr;  // right-aligned dim string (size, time, count)
    const char* tooltip   = nullptr;
    bool        selected  = false;
    bool        disabled  = false;
    float       actionsW  = 0.0f;     // px reserved at the right edge for caller-drawn actions
};
// Returns true when the row body was clicked (activated).
bool row(const char* id, const char* title, const RowStyle& style = RowStyle{});

// Lower-level pair for rows with bespoke bodies. rowBegin() paints the background and reserves
// `height` (0 = one text line plus padY()*2), returning true when clicked. Between the calls the
// cursor sits at the row's content origin.
bool rowBegin(const char* id, float height = 0.0f, bool selected = false);
void rowEnd();

// ---- chips / badges ---------------------------------------------------------------------------
// A chip is a small rounded pill. `dot` prefixes a tone-colored dot (status), otherwise the label
// itself carries the tone. valueChip pairs a dim key with an emphasized value.
void chip(const char* label, Tone t = Tone::Neutral, bool dot = false,
          const char* tooltip = nullptr);
void valueChip(const char* label, const char* value, Tone t = Tone::Neutral,
               const char* tooltip = nullptr);

// Interactive filter chip. Returns true when toggled. The fill animates through ui::anim.
bool toggleChip(const char* label, bool* on, Tone t = Tone::Accent,
                const char* tooltip = nullptr);

// ---- buttons -----------------------------------------------------------------------------------
// `enabled == false` renders a DISABLED button plus `disabledReason` as its tooltip. Never hide an
// action the user is looking for — tell them why it is unavailable.
bool primaryButton(const char* label, bool enabled = true, const char* disabledReason = nullptr,
                   const ImVec2& size = ImVec2(0, 0));
bool secondaryButton(const char* label, bool enabled = true, const char* disabledReason = nullptr,
                     const ImVec2& size = ImVec2(0, 0));
bool dangerButton(const char* label, bool enabled = true, const char* disabledReason = nullptr,
                  const ImVec2& size = ImVec2(0, 0));

// Square icon-only button (toolbars, row actions). `tooltip` is REQUIRED — an unlabelled glyph
// with no tooltip is not a discoverable control.
bool iconButton(const char* id, const char* icon, const char* tooltip, bool enabled = true,
                Tone t = Tone::Neutral, float size = 0.0f);

// Segmented control — the replacement for stacked radios and cramped combos. Returns true when
// the selection changed. `width` 0 sizes to content.
bool segmented(const char* id, const char* const* labels, int count, int* selected,
               float width = 0.0f, const char* const* tooltips = nullptr);

// A toolbar popover trigger: a labelled button that opens a popup, showing `activeCount` as a
// badge when non-zero (so collapsed filters never hide the fact that filters are on). Returns
// true while the popup is open — draw its contents, then call popoverEnd().
bool popoverBegin(const char* id, const char* label, const char* icon = nullptr,
                  int activeCount = 0);
void popoverEnd();

// ---- status ---------------------------------------------------------------------------------------
// The single status vocabulary. Running renders the brand tesseract in motion (falling back to a
// comet arc below the legibility floor); the rest are vector marks, never text glyphs.
enum class Status { Pending, Running, Done, Failed, Retry, Skipped };
Tone  statusTone(Status s);
// Pure draw at `center` with radius `r`. `surf` supplies the on-glass ink tone.
void  statusGlyph(ImDrawList* dl, ImVec2 center, float r, Status s, const AdaptiveSurface& surf);
// Layout-participating variant: advances the cursor like a text item of the same height.
void  statusGlyphInline(Status s);

// One-line status message under a control. No-op when `msg` is empty, so callers can pass a
// sticky error string unconditionally.
void inlineStatus(const char* msg, Tone t);

// Determinate progress bar; `frac` outside [0,1] renders an indeterminate marquee (job started,
// no progress reported yet) rather than a misleading empty or full bar.
void progressBar(float frac, const char* caption = nullptr, Tone t = Tone::Accent,
                 float height = 0.0f);

// Inline "working" indicator: the brand mark turning, then `label`. Thin wrapper over
// ui::busyLabel so every workspace's spinner is the same mark.
void spinner(const char* label, Tone t = Tone::Accent);

// ---- empty / blocked states ----------------------------------------------------------------------
// Every pane MUST render one of these instead of a blank region. An empty panel is indistinguishable
// from a broken one.
//
// emptyState  — nothing here YET; the body explains what will appear and how to make it appear.
// blockedState— something is WRONG or missing; `reason` states it, `hint` says what to do.
//
// Both return true when the optional action button is clicked.
bool emptyState(const char* icon, const char* title, const char* body = nullptr,
                const char* actionLabel = nullptr, bool actionEnabled = true);
bool blockedState(const char* title, const char* reason, const char* hint = nullptr,
                  const char* actionLabel = nullptr);

// ---- text ------------------------------------------------------------------------------------------
// Dim/secondary body text with wrapping. `label`/`value` is the canonical read-only property row —
// the value ELIDES with an ellipsis and gains a tooltip rather than wrapping mid-token, which is
// what unbroken ids and paths do inside a stretch column.
void dimText(const char* text);
void keyValueRow(const char* key, const char* value, Tone valueTone = Tone::Neutral,
                 bool copyable = false);

// Elide `text` to `maxW` px, appending an ellipsis. Returns the elided string; empty input yields
// empty output. Use with a tooltip carrying the full text.
std::string elide(const char* text, float maxW);

// ---- rhythm --------------------------------------------------------------------------------------
void rule();        // full-width hairline honouring the active padding
void gapRow();      // rowGap()
void gapSection();  // sectionGap()

// ---- formatting ------------------------------------------------------------------------------------
// One spelling per quantity, everywhere. "2.4 MB", "3 min ago", "1.2k", "4.3 s".
std::string fmtBytes(unsigned long long bytes);
std::string fmtAgo(double unixSeconds);       // relative to now; "just now" under 10 s
std::string fmtCount(long long n);            // 1200 -> "1.2k"
std::string fmtDuration(double seconds);

}  // namespace kit
}  // namespace ui
