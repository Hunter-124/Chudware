// FeatureManager tree panel (C16).
// Renders the tree body of the pinned left glass column (ui::drawLeftPane) — fixed nodes
// (Equations, Material, planes, Origin) followed by the feature history with status badges,
// context menu (Rename/Suppress/Delete/Edit).
// Selecting a tree feature highlights its owned faces in the viewport via the selection set.
// The panel calls back into C17's edit dispatcher via the optional EditCallback slot.
#pragma once

#include <functional>
#include <string>

#include "imgui.h"   // ImVec2 (FmRowRect) — shared FM row-styling API below

struct AppState;

namespace ui {

// ---- Shared FeatureManager row styling (the panel-wide ratified look) ----
// Exported so sibling left-pane tabs (file_explorer.cpp Explorer) draw rows/icons/section rules
// PIXEL-IDENTICAL to the FeatureManager reference: frosted row pill + soft-blue selection card +
// accent-outline hover, 16px atlas icons vertically centered in the leaf-arrow slot, and the
// centered hairline section dividers ("Reference"/"Features" style).
//
// Usage per row: fmRowPill(selected) BEFORE the row's widget (pushes FramePadding + transparent
// Header colors), then the widget (TreeNodeEx/Selectable/InputText), then fmRowPillEnd(rect,
// selected) — pairs MUST balance. Call fmRowStripeReset() once at the top of a tab body so the
// alternating stripe phase restarts each frame.
struct FmRowRect { ImVec2 mn, mx; };
FmRowRect fmRowPill(bool selected = false);
void      fmRowPillEnd(const FmRowRect& r, bool selected = false);
void      fmRowStripeReset();

// 16px atlas icon drawn as its own item (use before a TreeNodeEx that HAS a chevron; follow with
// SameLine(0, 4*uiScale)). Falls back to `fallback` text when the atlas lacks the glyph.
void fmTreeIcon(const char* name, const char* fallback, float size = 16.0f, float alpha = 1.0f);

// 16px atlas icon painted INSIDE the (empty) leaf-arrow slot of the TreeNodeEx just submitted;
// pass the cursor pos captured immediately BEFORE the node. Vertically centered in the row.
void fmTreeIconInArrowSlot(ImVec2 rowPos, const char* name, const char* fallback,
                           float alpha = 1.0f);

// Centered treeSection-colored label flanked by two hairlines (the "Reference"/"Features" rule).
void fmSectionDivider(const char* label);

// 24px square glass icon button (FeatureManager top-toolbar style: resting 0.22 pill, 0.38 on
// hover, 18px SDF-crisp glyph). Returns true on click; disabled renders dimmed and never fires.
bool fmToolbarIconBtn(const char* id, const char* iconName, const char* tooltip, bool enabled);

// Pane-header chip ("Feature Manager"/"Explorer"/... title above the tab body): icon + title in
// ONE glass pill sized from its content — icon fully inside with even padding, the icon+text
// group centered on paneCenterX (screen-space; clamped into the content region). titleIcon
// "history" renders a purpose-built line-art feature-tree glyph (root node + two branch
// children, row-icon stroke weight); any other name draws that SDF/atlas icon. Advances the
// layout cursor past the chip. Intended call site: layout.cpp drawLeftPane's tab-body header.
void fmPaneHeaderChip(AppState& st, const char* title, const char* titleIcon, float paneCenterX);

// The tree's inner content WITHOUT its own Begin/End. The tabbed left pane (ui::drawLeftPane) calls
// this inside its own "LeftPane" container child so the FeatureManager and PropertyManager tabs share
// one dock window. editCallback is invoked when "Edit" is chosen from a feature's context menu; it
// receives the feature id.
void drawFeatureTreeBody(AppState& st,
                         const std::function<void(const std::string& featureId)>& editCallback = {});

// W1-B (item 5): clear the tree's in-progress transient state (the inline-rename id + buffer and any
// queued-but-unapplied tree ops). Called by AppState::clearViewportForNewDoc and any path that
// replaces the DocMirror wholesale, so a fresh document never inherits a stale rename/op from the
// previous one. The selection itself now lives on AppState (selectedFeatureId), cleared separately.
void featureTreeResetTransient();

// Clear ONLY the FeatureManager multi-select set (Ctrl+click set). Narrower than
// featureTreeResetTransient (which also drops the rename buffer + pending ops); safe to call during
// normal interaction. Used by AppState::clearSelection.
void featureClearTreeMultiSel();

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

// Wave-5-fill: Edit > Delete / Edit > Select All, exposed for the Edit menu (title_bar.cpp) and the
// global Del / Ctrl+A key routing. Both mirror featureCutSelection's contract — call from app.cpp's
// pre-draw key/menu pump (NOT mid tree-walk: delete applies a DocMirror op immediately, which
// replaces the tree json the walk reads). Each is a no-op (returns false) without a live doc.
//   - featureDeleteSelection: delete the FeatureManager selection (full multi-select if the primary
//     is in it, else just the primary), via the same delete_feature op the context menu uses.
//   - featureSelectAll: select every feature in the active part's FeatureManager (populates the
//     multi-select with all feature ids from the doc tree). No-op for assemblies / empty docs.
bool featureDeleteSelection(AppState& st);
bool featureSelectAll(AppState& st);

// ---- Wave 3: Save/Open helpers, exposed so the File menu (title_bar.cpp) and the File Explorer
// (file_explorer.cpp) reuse the existing dialog + RPC + mesh-refresh logic instead of duplicating
// it. All are no-ops / return "" on cancel / failure and never throw.
//   - featureTreeOpenPath:   open an absolute path directly (Explorer double-click / Recent).
//     asSolid=true sews a mesh import into a B-rep solid (the dialog path prompts the user for this).
//   - featureTreeOpenDialog: native "Open..."/"Import..." dialog -> open; returns the opened path or
//     "". For a mesh file it asks "import as solid?" first.
//   - featureTreeSaveDoc:    Save (saveAs=false) / Save As (saveAs=true); returns the saved path or "".
//   - featureTreeActivePath: the active doc's on-disk path ("" if never saved).
bool        featureTreeOpenPath(AppState& st, const std::string& path, bool asSolid = false);
std::string featureTreeOpenDialog(AppState& st);
std::string featureTreeSaveDoc(AppState& st, bool saveAs);
std::string featureTreeActivePath();

// Live MCP handoff: a separate process (the AI-CAD MCP server Claude drives) builds parts in
// its OWN engine, so its docs are invisible here. It publishes the part it's working on to
// %APPDATA%\AI-CAD\handoff\ (live.step + a live.json carrying a monotonically rising "rev").
// Call this once per frame: when the rev changes it opens/refreshes a single "AI Live" tab from
// that file so the user SEES the part the AI is building. Cheap (an mtime check most frames);
// a no-op when the worker isn't up, an in-app run is active, or there's nothing new. Never throws.
void        pollAiCadHandoff(AppState& st);

}  // namespace ui
