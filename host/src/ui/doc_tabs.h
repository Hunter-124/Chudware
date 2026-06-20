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

// Height the document-tab strip wants to occupy (0 when no document is open, so the empty start
// state keeps the plain dock tab bar). Callers reserve this much space at the top of the central
// dock node before drawing the strip overlay over it.
float docTabStripHeight(const AppState& state);

// Draw the document tab strip INLINE at the current cursor inside the central dock window (Viewport
// 3D view / drawing sheet / ECAD editor), spanning `availW` and reserving its own height below so
// the view flows under it. One tab per open doc (active highlighted, * for unsaved, X to close,
// middle-click closes) plus a trailing "+" New chooser. Clicking a tab switches the active document
// (re-points DocMirror so viewport/tree/PM/selection all follow). Returns the strip height (0 when
// no document is open). A pending close on a dirty tab raises a confirmation modal handled inside.
float drawDocTabs(AppState& state, float availW);

// Wave 2 (GUI rework): request the "+" New-Document chooser popup open on the next strip draw. The
// chooser offers New Part / New Assembly / New Drawing / New Schematic / New PCB. Used by app.cpp's
// Ctrl+N key hook (the "+" button opens it directly). No-op if the strip isn't drawn (no document).
void openNewDocChooser();

// Create a document directly from menus/toolbars without drawing the old document strip.
// kind: 0=Part, 1=Assembly, 2=Drawing, 3=Schematic, 4=PCB.
void createNewDoc(AppState& state, int kind);

}  // namespace ui
