#pragma once
// kit_panel.h — the panel scaffold of the ui::kit vocabulary: pinned header, form tabs, stepper,
// scroll body, action footer, removable cards, framed canvases and the grouped "New ..." picker.
// ONE implementation each (kit_panel.cpp). Every simulation pane (study editors, Piping,
// Hydraulics, EM, Electronics, Firmware, Handoff, Evaluate takeovers) is built from these, so the
// panes cannot drift apart again (see the Simulation UI standard §2, §4, §6, §8).
//
// ID RULE: every interactive primitive takes an explicit, STABLE `id` and scopes everything it
// submits under PushID(id). Never derive the id from visible or data-derived text (counts, units,
// user names); in loops PushID(index) around the call or pass a stable record id. Two primitives
// with identical visible labels therefore never collide.
//
// Canonical pane:
//
//   kit::panelBegin("sim_study");
//   kit::PanelHeaderSpec h;  h.icon = "study_static";  h.title = s.name.c_str();
//   h.typeChip = "Static";   h.doneTip = "Close the setup panel. Edits are saved as you make them.";
//   if (kit::panelHeader("hdr", h)) close();
//   kit::stepper("steps", steps, 3, &step, setupNodes, nNodes, &node);   // or kit::formTabs(...)
//   kit::panelBodyBegin(pageName);            // one scroll position per page
//     ... cards / fields / results ...
//   kit::panelBodyEnd();
//   kit::panelFooterBegin();
//     switch (kit::actionFooter("run", a)) { case kit::FooterAction::Primary: run(); break; ... }
//   kit::panelEnd();
//
// GL/GUI thread only.

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include "../../ui_theme.h"   // ui::AdaptiveSurface
#include "kit.h"              // Tone, Status (kit.h includes this file after its own declarations)

namespace ui {
namespace kit {

// ---- panel header ------------------------------------------------------------------------------
// One row: [icon] Title (elided, full text on hover)  [type chip] [status chip]   [actions] [Done]
// The right cluster (extra icon actions + secondary "Done") never overflows the pane. Priority is
// title > status chip > type chip: the type chip is dropped as soon as the FULL title does not fit
// beside the chips (and always when the title already starts with the type, "Static 3"); the
// status chip is dropped before the title would elide below max(8 em, 60% of its width); only
// then does the title elide. Emits headGap() below itself.
struct HeaderAction {
    const char* id      = nullptr;   // stable id ("units", "help")
    const char* icon    = nullptr;   // contracts/icons.md name (unused when `label` is set)
    const char* tooltip = nullptr;   // REQUIRED (iconButton rule)
    bool        enabled = true;
    Tone        tone    = Tone::Neutral;   // label chips: non-Neutral tints it (Accent = override)
    const char* label   = nullptr;   // set: a compact TEXT chip button ("SI", "US") instead of the icon
};
struct PanelHeaderSpec {
    const char*         icon        = nullptr;   // study/panel icon (atlas name)
    const char*         title       = nullptr;   // record name ONLY, never "Name - Section"
    const char*         typeChip    = nullptr;   // dim type chip ("Static", "Topology"), optional
    const char*         statusChip  = nullptr;   // optional status chip ("Stale", "Solved")
    Tone                statusTone  = Tone::Neutral;
    const char*         statusTip   = nullptr;
    bool                showDone    = true;      // the close action is always labelled "Done"
    const char*         doneTip     = nullptr;
    const HeaderAction* actions     = nullptr;   // extra header icon buttons, left of Done
    int                 actionCount = 0;
    int*                actionPressed = nullptr; // out: index of the clicked action, else -1
    ImFont*             titleFont   = nullptr;   // e.g. st.fonts.medium; null = current font
};
// Returns true on the frame "Done" is clicked.
//   kit::HeaderAction acts[] = {{"units", "settings", "Display units for this study"}};
//   h.actions = acts; h.actionCount = 1; int hit = -1; h.actionPressed = &hit;
//   if (kit::panelHeader("hdr", h)) panelOpen_ = false;
//   if (hit == 0) ImGui::OpenPopup("##units");
bool panelHeader(const char* id, const PanelHeaderSpec& spec);

// ---- form tabs -----------------------------------------------------------------------------------
// Segmented form switch for multi-form panels (Piping, Hydraulics, EM modes). Fills the available
// width. Uses `fullLabels` when every segment fits on one row, else `shortLabels` (may be null),
// else WRAPS onto as many rows as needed (full labels when they fit the wrapped grid, short
// otherwise); every row spans the full width, so a shorter last row gets wider cells. Never
// scrolls, never clips. A segment whose shown label differs from its full label tooltips
// `tooltips[i]` (or the full label). Returns true when *selected changed.
//   static const char* kFull[]  = {"Circuit", "Sizing", "Simulate", "BOM", "To Piping"};
//   static const char* kShort[] = {"Circuit", "Size", "Run", "BOM", "Piping"};
//   if (kit::formTabs("forms", kFull, kShort, 5, &f, kTips)) form_ = (Form)f;
bool formTabs(const char* id, const char* const* fullLabels, const char* const* shortLabels,
              int count, int* selected, const char* const* tooltips = nullptr);

// ---- stepper -------------------------------------------------------------------------------------
// Setup -> Run -> Results track (one compact row, one text line tall) with an optional sub-chip
// row for the setup nodes. Sub-chips use their natural widths on one row when they fit; otherwise
// they WRAP onto the fewest balanced rows (an aligned equal-width grid when every chip fits its
// cell) — labels never elide unless a single chip is wider than the pane. Every step and
// chip tooltips its `tooltip`, or "Label: Ready / Needs attention / Not started ..." so the marks
// are explained. `status` drives the mark: Done = ready, Retry = needs attention, Failed =
// blocking, Running = in progress, Pending = not started, Skipped = not applicable.
struct StepItem {
    const char* label   = nullptr;
    Status      status  = Status::Pending;
    bool        enabled = true;
    const char* tooltip = nullptr;
};
// Clicking step i sets *current = i and *currentSub = -1. Clicking sub-chip j sets
// *current = subOf and *currentSub = j. Returns true when either changed.
//   kit::StepItem steps[] = {{"Setup", setupOk ? Status::Done : Status::Retry},
//                            {"Run", runSt}, {"Results", resSt, s.hasResult, "Run the study first."}};
//   kit::StepItem subs[]  = {{"Material", matSt}, {"Fixtures", fixSt}, {"Loads", ldSt}, {"Mesh", mSt}};
//   if (kit::stepper("steps", steps, 3, &step, subs, 4, &sub)) navigate(step, sub);
bool stepper(const char* id, const StepItem* steps, int count, int* current,
             const StepItem* substeps = nullptr, int subCount = 0, int* currentSub = nullptr,
             int subOf = 0);

// Compact one-line variant for list rows: a status mark per step, labels only when they fit.
// Height = one text line. Clickable (returns true and sets *current). Reserve
// stepperCompactWidth() in RowStyle::actionsW, then position the cursor and call it.
bool  stepperCompact(const char* id, const StepItem* steps, int count, int* current);
float stepperCompactWidth(const StepItem* steps, int count, bool withLabels = true);

// ---- panel scaffold ------------------------------------------------------------------------------
// panelBegin(id)           opens the scope; header / stepper / tabs drawn next are PINNED.
// panelBodyBegin(page)     hairline (only when something pinned was drawn since panelBegin — a
//                          header-less dialog gets no stray rule), then the scroll body child
//                          (NoScrollbar + pill scrollbar) sized so it fills the pane exactly above
//                          the footer. The right ui::kChatScrollbarLane is ALWAYS reserved for the
//                          pill, so it never overlays cards and the content width never jumps when
//                          a page starts/stops overflowing. `page` (optional) keys the scroll
//                          position: pass the node/form name for one scroll per page.
// panelBodyEnd()           pill scrollbar + EndChild, then scroll-edge cues: a fade into the pane
//                          surface at an edge with more content beyond it and a small down chevron
//                          while content continues below (no input, no layout effect).
// panelFooterBegin()       optional: hairline, then PINNED footer content (actionFooter, ...).
//                          Its height is measured from its own content and used next frame; footer
//                          status is capped at TWO wrapped lines (actionFooter / footerStatus), so
//                          the height only changes with state or message, never unboundedly.
// panelEnd()               closes the scope. Always paired with panelBegin.
// `surf` (optional) is the pane's adaptive surface for the pill scrollbar; null derives it from
// the current ink, which is correct on every themed pane.
void panelBegin(const char* id, const AdaptiveSurface* surf = nullptr);
void panelBodyBegin(const char* page = nullptr);
void panelBodyEnd();
void panelFooterBegin();
void panelEnd();

// ---- action footer -------------------------------------------------------------------------------
// The pane's ONE primary action. Idle: the blocker (when !primaryEnabled) is shown INLINE above
// a full-width primary button, word-wrapped to at most two lines (only text past the second line
// elides; then the full text shows on hover, and always as the button's tooltip). Write blockers
// short and action-first ("Pick at least one fixed face."). Running: a "stage ... elapsed"
// caption line (stage left, elapsed right) over a full-width progressBar(progress), then a
// full-width dangerButton "Cancel", or a "Cannot cancel: reason" status when !cancellable.
// `primaryAsSecondary` renders the primary as a secondary button (Results step "Re-run"). A
// disabled primary uses the kit's neutral disabled look (grey fill, border, dim text). An optional
// secondary action (Discard, Open in ...) shares the button row on the left when both labels fit
// unclipped, else the two stack full-width (secondary above, main below). Labels never clip: they
// elide (tooltip) only when a full row is too narrow. Button IDs are stable ("###") so labels may
// change freely.
enum class FooterAction { None, Primary, Cancel, Secondary };
struct ActionSpec {
    const char* primaryLabel       = "Run";
    bool        primaryEnabled     = true;
    const char* blocker            = nullptr;   // why the primary is disabled (shown inline)
    Tone        blockerTone        = Tone::Warn;
    bool        primaryAsSecondary = false;
    const char* note               = nullptr;   // optional dim status (<= 2 lines) above the buttons
    // running state
    bool        running            = false;
    float       progress           = -1.0f;     // [0,1] determinate; anything else = marquee
    const char* stage              = nullptr;   // "Solving", "Iteration 12 of 40"
    double      elapsed            = -1.0;      // seconds; < 0 omits the elapsed part
    bool        cancellable        = true;
    bool        cancelRequested    = false;     // disabled "Cancel requested..." until it lands
    const char* cancelBlocker      = nullptr;   // why it cannot cancel (when !cancellable)
    // optional secondary
    const char* secondaryLabel     = nullptr;
    bool        secondaryEnabled   = true;
    const char* secondaryReason    = nullptr;
};
//   kit::ActionSpec a;  a.primaryLabel = "Run";  a.primaryEnabled = ready && !busy;
//   a.blocker = blocker.c_str();  a.running = ownsJob;  a.progress = pct;  a.stage = stage.c_str();
//   a.elapsed = client->elapsedSeconds();  a.cancelRequested = client->cancelRequested();
//   switch (kit::actionFooter("run", a)) {
//       case kit::FooterAction::Primary: run(st, s); break;
//       case kit::FooterAction::Cancel:  client->cancel(); break;
//       default: break;
//   }
FooterAction actionFooter(const char* id, const ActionSpec& a);

// Footer/status message: tone dot + text word-wrapped to at most two lines (text past the second
// line elides, full text on hover). No-op on empty. For panel-level status in a pinned footer
// (record stores, "Saved", "Routed ..."), so the footer stays bounded.
//   kit::panelFooterBegin();  kit::footerStatus(store.status().c_str(), store.statusTone());
void footerStatus(const char* msg, Tone t);

// ---- removable card ------------------------------------------------------------------------------
// cardBegin + a header-right delete iconButton (tooltip `removeTip`): a frame-height square
// (glyph on the header icon tier) right-aligned one padX inside the card border and vertically
// centred on the title row, overlaid so it never grows the header. Returns like cardBegin (false
// = collapsed: skip the body); ALWAYS call cardEnd(). *removed is set true on the click frame (it
// is never cleared) — erase the item after the loop, not inside the card.
//   for (int i = 0; i < (int)loads.size(); ++i) {
//       ImGui::PushID(i);  bool rm = false;
//       if (kit::removableCard("load", loads[i].name.c_str(), "force", nullptr, "Remove this load", &rm))
//           drawLoadBody(loads[i]);
//       kit::cardEnd();  ImGui::PopID();
//       if (rm) removeIdx = i;
//   }
bool removableCard(const char* id, const char* title, const char* icon, const float tint[3],
                   const char* removeTip, bool* removed, bool* open = nullptr);

// ---- canvas --------------------------------------------------------------------------------------
// Framed canvas (frameBg plate + hairline border) with a bottom hint/key strip. `size` follows
// ImGui item sizing (0 = fill available, negative = available minus). The whole frame is ONE hit
// item (left + right mouse buttons); its state is copied into the result. `keyW` reserves the
// right end of the strip for a caller-drawn key (strip.Max.x - padX() - keyW .. strip.Max.x -
// padX()); the hint elides into the rest (full hint on hover of the strip when cut). Draw content
// inside `inner` (clip it yourself with dl->PushClipRect(inner.Min, inner.Max, true)).
struct Canvas {
    ImGuiID     id      = 0;
    ImDrawList* dl      = nullptr;
    ImRect      frame;            // full framed rect
    ImRect      inner;            // drawing area above the strip
    ImRect      strip;            // bottom strip (zero-height when no hint and keyW == 0)
    float       keyX    = 0.0f;   // left edge of the caller's key area in the strip
    bool        hovered = false;
    bool        active  = false;
    bool        clicked = false;  // left-button release on the canvas
};
//   kit::Canvas c = kit::canvasFrame("schematic", ImVec2(0, 0), "Click a symbol to edit it", keyW);
//   if (comps.empty()) { if (kit::canvasEmpty(c, "study_hydraulics", "No components yet",
//                                             "Add one from Hydraulics > Add Component.")) ... }
//   else { c.dl->PushClipRect(c.inner.Min, c.inner.Max, true); drawSymbols(c); c.dl->PopClipRect(); }
Canvas canvasFrame(const char* id, const ImVec2& size, const char* hint = nullptr,
                   float keyW = 0.0f);
// emptyState centred inside the canvas's inner rect (instead of raw draw-list text). Call right
// after canvasFrame. Returns true when the optional action is clicked.
bool canvasEmpty(const Canvas& canvas, const char* icon, const char* title,
                 const char* body = nullptr, const char* actionLabel = nullptr,
                 bool actionEnabled = true);

// ---- grouped picker ------------------------------------------------------------------------------
// Popup CONTENT for "New study" / "New ..." menus: per group a compact caption, per item a row
// with icon, title and a description WRAPPED to at most two lines (text past that elides, full
// text on hover) at a fixed width (`maxWidth` px, 0 = the default ~380 dp). Disabled rows are
// dimmed, not clickable, and carry an inline Warn reason chip that WRAPS (never truncated). The
// list is its own scroll child with the app pill scrollbar in a reserved lane (no ImGui default
// bar); its height is capped so the WHOLE containing window stays within `maxHeight` px measured
// from the window's top (0 = 70% of the main viewport's work height; any value is also clamped to
// that 70%) and the list never runs past the work area's bottom. Call inside a popup you opened
// (popoverBegin / BeginPopup) and do NOT cap or scroll the popup yourself — it auto-fits. A pick
// closes that popup. Returns the picked item's FLAT index (items numbered in order across all
// groups), else -1.
struct PickerItem {
    const char* icon    = nullptr;
    const char* title   = nullptr;
    const char* desc    = nullptr;
    bool        enabled = true;
    const char* reason  = nullptr;   // why disabled
};
struct PickerGroup {
    const char*       title = nullptr;
    const PickerItem* items = nullptr;
    int               count = 0;
};
//   static const kit::PickerItem kStruct[] = {{"study_static", "Static", "Stress and deflection."}, ...};
//   const kit::PickerGroup groups[] = {{"Structural", kStruct, 6}, {"Thermal", kTherm, 2}, ...};
//   if (ImGui::BeginPopup("##new_study")) {
//       const int k = kit::groupedPicker("new_study", groups, 7, 0.0f, spaceBelowAnchor);
//       if (k >= 0) addStudy(kFlatTypes[k]);
//       ImGui::EndPopup();
//   }
int groupedPicker(const char* id, const PickerGroup* groups, int groupCount,
                  float maxWidth = 0.0f, float maxHeight = 0.0f);

}  // namespace kit
}  // namespace ui
