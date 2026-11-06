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

#include <functional>
#include <string>

struct AppState;

namespace ui {

// Height the document-tab strip wants to occupy inside the central view window. Outside the
// workspace home it stays visible without fake tabs so the New Document button stays reachable.
float docTabStripHeight(const AppState& state);

// Draw the document tab strip at the current cursor inside the central view window, spanning
// `availW` and reserving its own height. One tab per open doc (active highlighted, * for unsaved,
// X to close, middle-click closes) plus a trailing "+" New chooser. Clicking a tab switches the
// active document (re-points DocMirror so viewport/tree/PM/selection all follow). A pending close
// on a dirty tab raises a confirmation modal handled inside.
float drawDocTabs(AppState& state, float availW);

// Wave 2 (GUI rework): request the "+" New-Document chooser popup open on the next strip draw. The
// chooser offers New Part / New Assembly / New Drawing / New KiCad Project. Used by app.cpp's
// Ctrl+N key hook (the "+" button opens it directly). Consumed even while the home hides the strip.
void openNewDocChooser();

// Create a document directly from menus/toolbars without drawing the old document strip.
// kind: 0=Part, 1=Assembly, 2=Drawing, 3=KiCad project.
void createNewDoc(AppState& state, int kind);

// Outcome of saving a single tab (for the close/quit/Save/Save-All flows).
enum class TabSaveResult {
    Pending,    // completion will be delivered on the UI thread
    Saved,      // written to disk this call
    Nothing,    // nothing needed saving (clean, or not a savable kind)
    Cancelled,  // user cancelled the Save-As dialog — abort any pending close/quit
    Failed,     // a save was attempted and failed (error toasted) — abort any pending close/quit
};

// Completion runs once, immediately for synchronous outcomes or later for pending RPCs.
using TabSaveDone = std::function<void(TabSaveResult)>;

// Save by the selected tab's stable identity without changing the active document.
TabSaveResult saveDocTab(AppState& state, int idx, bool forceDialog,
                         TabSaveDone done = {});

// Save a specific CAD document to an established destination. Automatic saves must
// not silently persist outstanding human edits; hostd enforces that condition.
TabSaveResult saveCadDocument(AppState& state, const std::string& docId,
                              const std::string& path, bool automatic,
                              TabSaveDone done = {});

}  // namespace ui
