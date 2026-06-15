// FeatureManager tree panel (C16).
// Renders the left dock panel — fixed nodes (Equations, Material, planes, Origin) followed
// by the feature history with status badges, context menu (Rename/Suppress/Delete/Edit).
// Selecting a tree feature highlights its owned faces in the viewport via the selection set.
// The panel calls back into C17's edit dispatcher via the optional EditCallback slot.
#pragma once

#include <functional>
#include <string>

struct AppState;

namespace ui {

// Draw the FeatureManager tree panel. Call every frame from the main loop.
// editCallback is invoked when "Edit" is chosen from a feature's context menu;
// it receives the feature id. Pass {} until C17 wires it.
// Thin compatibility wrapper: Begin("FeatureManager") -> drawFeatureTreeBody -> End().
void drawFeatureTree(AppState& st,
                     const std::function<void(const std::string& featureId)>& editCallback = {});

// Wave 1D: the tree's inner content WITHOUT its own Begin/End. The Wave 1D tabbed left pane
// (ui::drawLeftPane) calls this inside its own "LeftPane" container child so the FeatureManager and
// PropertyManager tabs share one dock window. drawFeatureTree() is the standalone wrapper around it.
void drawFeatureTreeBody(AppState& st,
                         const std::function<void(const std::string& featureId)>& editCallback = {});

// W1-B (item 5): clear the tree's in-progress transient state (the inline-rename id + buffer and any
// queued-but-unapplied tree ops). Called by AppState::clearViewportForNewDoc and any path that
// replaces the DocMirror wholesale, so a fresh document never inherits a stale rename/op from the
// previous one. The selection itself now lives on AppState (selectedFeatureId), cleared separately.
void featureTreeResetTransient();

// C38 (T1): rebuild st.featureHighlightFaces from the CURRENT owner map for st.selectedFeatureId
// (clearing it when the feature is absent or there's no selection). Face tags are retagged on every
// applyOps/undo/redo/resync rebuild, so the cached highlight goes stale; call this after any such
// rebuild (docUndo/docRedo in app_state.cpp, and the central DocMirror listener in app.cpp).
void refreshFeatureHighlight(AppState& st);

// Wave 4C/4D: FeatureManager copy / cut / paste, driven by the global Ctrl+C/X/V routing and the
// "edit_copy" / "edit_cut" / "edit_paste" dispatcher ids. Call from app.cpp's pre-draw key handler
// (NOT mid tree-walk — these apply ops immediately). Each acts on the FeatureManager selection
// (the full multi-select if the primary is in it, else just the primary) and is a no-op (returns
// false) without an eligible selection / clipboard / live doc.
//   - featureCopySelection: copy the selected feature(s) into st.clipboard (read-only engine op).
//   - featureCutSelection:  copy, then delete the selected feature(s).
//   - featurePasteSelection: append st.clipboard's feature bundle(s) into the active doc.
bool featureCopySelection(AppState& st);
bool featureCutSelection(AppState& st);
bool featurePasteSelection(AppState& st);

// ---- Wave 3: Save/Open helpers, exposed so the File menu (title_bar.cpp) and the File Explorer
// (file_explorer.cpp) reuse the existing dialog + RPC + mesh-refresh logic instead of duplicating
// it. All are no-ops / return "" on cancel / failure and never throw.
//   - featureTreeOpenPath:   open an absolute path directly (Explorer double-click / Recent).
//   - featureTreeOpenDialog: native "Open..." dialog -> open; returns the opened path or "".
//   - featureTreeSaveDoc:    Save (saveAs=false) / Save As (saveAs=true); returns the saved path or "".
//   - featureTreeActivePath: the active doc's on-disk path ("" if never saved).
bool        featureTreeOpenPath(AppState& st, const std::string& path);
std::string featureTreeOpenDialog(AppState& st);
std::string featureTreeSaveDoc(AppState& st, bool saveAs);
std::string featureTreeActivePath();

}  // namespace ui
