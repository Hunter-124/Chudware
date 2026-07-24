// reveng_widgets.h — shared visual kit for the Reverse Engineering workspace (RE-40).
//
// WHY THIS EXISTS
// The RE tab is a six-stage wizard split across five TUs (sources_panel, stage_panels,
// dim_review, image_gallery, reveng_workspace). Each grew its own paddings, card corners,
// status colouring and empty states, so the stages read as unrelated screens and previews were
// inconsistent or absent. This header is the single vocabulary every RE panel draws with.
//
// HARD RULES for every RE panel
//   • Sizes come from ui::metrics::re*() — never a fresh pixel literal.
//   • Colours come from ui::theme().palette / this kit — never a hardcoded ImU32
//     (scripts/gui_lint.py --rule theme enforces this).
//   • Every asynchronous action shows one of: Busy / Ok / Warn / Error via StatusChip or
//     InlineStatus — never a silent no-op and never a bare ImGui::Text error.
//   • Every stage that produces an artifact SHOWS that artifact (HeroPreview / ThumbTile /
//     Viewport3DPreview). "It worked, trust me" is not an acceptable stage outcome.
//
// GL-thread only, like the rest of ui/reveng.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (3D preview slot rects)

struct AppState;

namespace ui {
namespace reveng {
namespace kit {

// ---- semantic status ---------------------------------------------------------------------------
// Maps onto palette.statusOk / statusWarn / statusErr / textDim / accent. Panels choose a Level;
// the kit owns the colour so "provisioned" looks identical in Sources, Scale and Deliver.
enum class Level {
    Neutral,   // informational, no judgement
    Ok,        // succeeded / available / within tolerance
    Warn,      // degraded / approximate / out of tolerance but usable
    Error,     // failed / unavailable / blocking
    Busy,      // in flight (renders an animated spinner glyph)
};

ImU32 levelColor(Level l);
ImVec4 levelColorVec(Level l);

// ---- cards -------------------------------------------------------------------------------------
// A glass card is THE container for every logical group in the RE tab. Always paired.
// `title` may be empty for an untitled card. `icon` is a contracts/icons.md name or nullptr.
// Returns false when the card is collapsed — skip the body and still call CardEnd().
bool  CardBegin(const char* id, const char* title = nullptr, const char* icon = nullptr,
                bool collapsible = false, bool* open = nullptr);
void  CardEnd();

// Right-aligns the following widget(s) on the card's title row (status chips, small buttons).
// Call immediately after CardBegin() returns true, before any body content.
void  CardHeaderRight(float widgetsWidth);

// A section header inside a card: label + hairline rule + the metrics::reHeadGap() below it.
void  SectionHeader(const char* label, const char* icon = nullptr);

// ---- chips / badges ------------------------------------------------------------------------------
// Small rounded pill with a coloured dot and a label. The single way status is shown in the RE tab.
void  StatusChip(const char* label, Level l, const char* tooltip = nullptr);

// Chip variant carrying a numeric readout, e.g. ("residual", "0.42 %", Level::Ok).
void  ValueChip(const char* label, const char* value, Level l, const char* tooltip = nullptr);

// Honesty tag — the reveng-specific chip for approximate / mesh-fallback / photo-derived data.
// `tier` is one of "high" | "medium" | "low" | "approximate" | "exact" (contracts/reveng.md §0).
// Renders with the tier's own colour and an explanatory tooltip. NEVER silently omit this on
// any dimension or geometry that did not come from a measured, scaled source.
void  HonestyTag(const char* tier, const char* detail = nullptr);

// ---- inline feedback -----------------------------------------------------------------------------
// One-line status under a control. No-op when `msg` is empty, so callers can pass a sticky
// error string unconditionally.
void  InlineStatus(const char* msg, Level l);

// "(?)" marker with a hover tooltip. Use for anything a mechanical engineer would reasonably
// not know (what an ArUco mat is for, why autofocus must be locked, what match_pct means).
void  HelpMarker(const char* text);

// ---- form rows -----------------------------------------------------------------------------------
// Two-column label/control row with a consistent label column. Call FieldLabel() then the widget;
// the widget's width is already set to metrics::reFieldW() unless you override it after.
void  FieldLabel(const char* label, const char* help = nullptr);

// Full-width labelled row helpers for read-only data (properties column, session details).
void  KeyValueRow(const char* key, const char* value, Level valueLevel = Level::Neutral);

// ---- buttons ---------------------------------------------------------------------------------------
// Accent-filled primary action (one per card, maximum). `enabled` false renders a disabled
// button plus `disabledReason` as its tooltip — never hide an action the user is looking for.
bool  PrimaryButton(const char* label, bool enabled = true, const char* disabledReason = nullptr,
                    const ImVec2& size = ImVec2(0, 0));
bool  SecondaryButton(const char* label, bool enabled = true, const char* disabledReason = nullptr,
                      const ImVec2& size = ImVec2(0, 0));

// Destructive / irreversible action (delete session, discard capture set).
bool  DangerButton(const char* label, bool enabled = true, const char* disabledReason = nullptr);

// A segmented control — the RE tab's replacement for stacked radio buttons and cramped combos
// (source lane, scale method, reconstruct quality). Returns true when the selection changed.
bool  SegmentedControl(const char* id, const char* const* labels, int count, int* selected,
                       float width = 0.0f);

// ---- progress -----------------------------------------------------------------------------------
// Thin accent progress bar with an optional right-aligned caption. `pct` in 0..1; pass a
// negative value for an indeterminate marquee (job started, no progress reported yet).
void  ProgressBar(float pct, const char* caption = nullptr, Level l = Level::Neutral);

// Multi-step horizontal progress used by the Reconstruct stage's sparse/dense/mesh sub-steps
// and by the top stage stepper. `states[i]` is the Level for step i; `active` is the index
// currently running (-1 for none). Returns the index clicked, or -1.
int   StepTrack(const char* id, const char* const* labels, const Level* states, int count,
                int active, bool clickable, float height);

// ---- previews ------------------------------------------------------------------------------------
// THE core of the "visual previews for everything" requirement.

// Letterboxed image preview. Preserves aspect, centres inside `box`, draws a glass frame and a
// caption strip. `texId` 0 renders a labelled placeholder rather than a blank hole.
// Returns true when clicked (callers open a full-size viewer).
bool  HeroPreview(const char* id, unsigned int texId, int texW, int texH, const ImVec2& box,
                  const char* caption = nullptr, const char* emptyLabel = nullptr);

// Contact-sheet tile: square thumbnail, selection ring, corner quality badge, index label.
// `badge` is "" | "ok" | "blurred" | "duplicate" | "synthetic". Returns true when clicked.
bool  ThumbTile(const char* id, unsigned int texId, float side, bool selected,
                const char* badge = nullptr, const char* label = nullptr);

// Hover magnifier for a thumbnail — shows a large tooltip preview of the same texture plus
// metadata lines. Call immediately after ThumbTile() while it is hovered.
void  ThumbHoverPreview(unsigned int texId, int texW, int texH,
                        const std::vector<std::string>& metaLines);

// Reserves a region for a GL 3D preview (point cloud / splat / mesh / deviation heatmap) and
// publishes its screen rect so the post-ImGui GL pass can render into it. `slot` identifies
// which renderer draws there. Returns the reserved rect in screen space.
enum class PreviewSlot { None, SparseCloud, Splat, Mesh, Deviation, Parametric };
ImRect Viewport3DPreview(const char* id, const ImVec2& box, PreviewSlot slot,
                         const char* emptyLabel = nullptr);

// Rect published by the most recent Viewport3DPreview() for `slot` this frame, plus whether it
// was drawn at all. app.cpp's post-ImGui GL pass consumes these instead of hardcoding regions.
bool  previewSlotRect(PreviewSlot slot, ImRect& out);
void  resetPreviewSlots();   // call once per frame before drawing the workspace

// ---- empty / error states ------------------------------------------------------------------------
// Centred icon + title + explanatory body + optional call-to-action. Every stage MUST render one
// of these instead of a blank panel when it has no data. Returns true when the action is clicked.
bool  EmptyState(const char* icon, const char* title, const char* body,
                 const char* actionLabel = nullptr, bool actionEnabled = true);

// Blocking-problem state: same shape as EmptyState but Error-toned, with a "what to do" hint.
// Used for backend_not_provisioned, no camera, unreachable worker.
bool  BlockedState(const char* title, const char* reason, const char* hint,
                   const char* actionLabel = nullptr);

// ---- capability surfacing --------------------------------------------------------------------------
// One row of the capability panel: name, availability, and the fix-it hint from reveng_status().
// This is how the user learns COLMAP is missing BEFORE clicking Reconstruct.
void  CapabilityRow(const char* name, bool available, const char* hint);

// ---- misc ------------------------------------------------------------------------------------------
// Full-width hairline separator honouring the card padding.
void  Rule();

// Vertical rhythm helpers so panels stop calling ImGui::Dummy() with ad-hoc sizes.
void  GapCards();      // metrics::reCardGap()
void  GapSection();    // metrics::reSectionGap()
void  GapControl();    // metrics::reCtlGap()

// Format a byte count / duration / millimetre value the same way everywhere.
std::string fmtMm(double mm, int decimals = 2);
std::string fmtPct(double pct01, int decimals = 1);
std::string fmtDuration(double seconds);

}  // namespace kit
}  // namespace reveng
}  // namespace ui
