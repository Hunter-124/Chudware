// Document tab strip (SolidWorks/browser-style multi-document).
//
// A thin horizontal strip drawn just under the ribbon: one button per open document (active one
// highlighted, a dot/asterisk for unsaved edits, an X to close, middle-click also closes), plus a
// "+" New-Part button at the end. Clicking a tab switches the active document; the viewport,
// FeatureManager tree, PropertyManager, and selection all follow because every switch re-points
// AppState::docMirror (see core/doc_tabs.cpp).
//
// The strip is hidden when zero or one document is open by default? No — it is shown whenever at
// least one document is open so single-document behavior is visually unchanged from before only in
// that a single tab appears; closing the last tab returns to the empty start state. The strip
// occupies no vertical space when no document is open (returns 0).
#pragma once

struct AppState;

namespace ui {

// Height the document-tab strip wants to occupy inside the central view window. It remains visible
// without fake tabs so the New Document button stays reachable.
float docTabStripHeight(const AppState& state);

// Draw the document tab strip at the current cursor inside the central view window, spanning
// `availW` and reserving its own height. One tab per open doc (active highlighted, * for unsaved,
// X to close, middle-click closes) plus a trailing "+" New chooser. Clicking a tab switches the
// active document (re-points DocMirror so viewport/tree/PM/selection all follow). A pending close
// on a dirty tab raises a confirmation modal handled inside.
float drawDocTabs(AppState& state, float availW);

// Wave 2 (GUI rework): request the "+" New-Document chooser popup open on the next strip draw. The
// chooser offers New Part / New Assembly / New Drawing / New Schematic / New PCB. Used by app.cpp's
// Ctrl+N key hook (the "+" button opens it directly). No-op if the strip isn't drawn (no document).
void openNewDocChooser();

// Create a document directly from menus/toolbars without drawing the old document strip.
// kind: 0=Part, 1=Assembly, 2=Drawing, 3=Schematic, 4=PCB.
void createNewDoc(AppState& state, int kind);

// Outcome of saving a single tab (for the close/quit/Save/Save-All flows).
enum class TabSaveResult {
    Saved,      // written to disk this call
    Nothing,    // nothing needed saving (clean, or not a savable kind)
    Cancelled,  // user cancelled the Save-As dialog — abort any pending close/quit
    Failed,     // a save was attempted and failed (error toasted) — abort any pending close/quit
};

// Save one document tab to disk, dispatching by kind:
//   Part/Assembly → DocMirror .step+ (re-saves to its path, or Save-As when never saved / forceDialog)
//   Schematic/PCB → ecad_save .ecad+ (to its path, or Save-As when never saved / forceDialog)
//   Drawing       → saves its SOURCE part (drawings embed in the part's .step+; persisting the part
//                   persists the drawing). No-op if the source part tab is gone.
// Clears the saved tab's dirty marker (and, for a part, every drawing embedded in it). Records the
// path in Recent. NOTE: CAD/Drawing saves switch the active document as a side effect (DocMirror must
// point at the doc being saved); callers that must preserve the active tab restore it afterward.
TabSaveResult saveDocTab(AppState& state, int idx, bool forceDialog);

}  // namespace ui
