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

// Draw the document tab strip at screen y = topY spanning the full window width. Returns the strip
// height in pixels (0 when nothing should be drawn) so the caller can offset the dockspace below it.
// A pending close (X / middle-click) on a dirty tab raises a confirmation modal handled inside.
float drawDocTabs(AppState& state, float topY);

}  // namespace ui
