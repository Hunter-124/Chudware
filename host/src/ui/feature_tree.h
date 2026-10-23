// FeatureManager tree panel (C16).
// Renders the tree body of the pinned left glass column (ui::drawLeftPane) — fixed nodes
// (Equations, Material, planes, Origin) followed by the feature history with status badges,
// context menu (Rename/Suppress/Delete/Edit).
// Selecting a tree feature highlights its owned faces in the viewport via the selection set.
// The panel calls back into C17's edit dispatcher via the optional EditCallback slot.
#pragma once

#include <cstdint>
#include <functional>
#include <string>

#include <nlohmann/json.hpp>

#include "imgui.h"   // ImVec2 (FmRowRect) — shared FM row-styling API below

struct AppState;
namespace render { struct PickHit; }

namespace ui {

// ---- Shared FeatureManager row styling (the panel-wide standard look) ----
// Exported so sibling left-pane tabs (file_explorer.cpp Explorer) draw rows/icons/section rules
// PIXEL-IDENTICAL to the FeatureManager reference: frosted row pill + accent selection (soft fill
// + glow border) + accent-outline hover, 16px atlas icons vertically centered in the leaf-arrow
// slot, and the centered hairline section dividers ("Reference"/"Features" style).
//
// Usage per row: fmRowPill(selected) BEFORE the row's widget (pushes FramePadding + transparent
// Header colors), then the widget (TreeNodeEx/Selectable/InputText), then fmRowPillEnd(rect,
// selected) — pairs MUST balance. Call fmRowStripeReset() once at the top of a tab body so the
// alternating stripe phase restarts each frame.
struct FmRowRect { ImVec2 mn, mx; };
// `tint` (optional palette RGB, float[3]) dyes the row the "Layers"-style color wash (see
// ui::coloredRowWash). Pass a section/element color for color-coded rows; leave null for the
// neutral app-accent wash every row shares. Selection (every row kind alike) is a soft accent fill
// plus a crisp accent glow border, painted over the wash.
FmRowRect fmRowPill(bool selected = false, const float* tint = nullptr);
void      fmRowPillEnd(const FmRowRect& r, bool selected = false);
void      fmRowStripeReset();

// 16px atlas icon drawn as its own item (use before a TreeNodeEx that HAS a chevron; follow with
// SameLine(0, 4*uiScale)). Falls back to `fallback` text when the atlas lacks the glyph.
void fmTreeIcon(const char* name, const char* fallback, float size = 16.0f, float alpha = 1.0f);

// Atlas icon id the FeatureManager draws for a feature `type` ("extrude", "sketch", …); the
// feature's JSON, when given, picks the cut variant (extrude_cut vs extrude_boss).
const char* fmFeatureIcon(const std::string& type, const nlohmann::json* feature = nullptr);

// 16px atlas icon painted INSIDE the (empty) leaf-arrow slot of the TreeNodeEx just submitted;
// pass the cursor pos captured immediately BEFORE the node. Vertically centered in the row.
void fmTreeIconInArrowSlot(ImVec2 rowPos, const char* name, const char* fallback,
                           float alpha = 1.0f);

// Centered treeSection-colored label flanked by two hairlines (the "Reference"/"Features" rule).
void fmSectionDivider(const char* label);

// Section divider with a small square "+" button pinned at the right edge (the right hairline is cut
// short to clear it). Returns true when the button is clicked. See feature_tree.cpp.
bool fmSectionDividerAddButton(const char* label, const char* btnId, const char* tooltip);

// Collapsible section header: centered label + hairlines with an end-of-line chevron. The whole row
// toggles *open (returns *open). rightClicked, if non-null, reports a right-click for a context menu.
bool fmSectionDividerCollapse(const char* label, bool* open, bool* rightClicked = nullptr);

// 24px square glass icon button (FeatureManager top-toolbar style: resting 0.22 pill, 0.38 on
// hover, 18px SDF-crisp glyph). Returns true on click; disabled renders dimmed and never fires.
bool fmToolbarIconBtn(const char* id, const char* iconName, const char* tooltip, bool enabled);

// The FeatureManager row visibility "eye" glyph (open eye when shown; struck-through when hidden),
// drawn into the current window's draw list with the FM adaptive-surface ink. Exported so other
// left-column trees (the ECAD PCB/Schematic Manager) can render layer/plane visibility toggles that
// read identically to the Feature Manager's plane rows. Caller owns the hit-test + click handling.
void fmDrawEyeGlyph(ImVec2 mn, ImVec2 mx, bool shown, bool hovered);

// Pane-header chip ("Feature Manager"/"Explorer"/... title above the tab body): icon + title in
// ONE glass pill sized from its content — icon fully inside with even padding, the icon+text
// group centered on paneCenterX (screen-space; clamped into the content region). titleIcon
// "history" renders a purpose-built line-art feature-tree glyph (root node + two branch
// children, row-icon stroke weight); any other name draws that SDF/atlas icon. Advances the
// layout cursor past the chip. Intended call site: layout.cpp drawLeftPane's tab-body header.
void fmPaneHeaderChip(AppState& st, const char* title, const char* titleIcon, float paneCenterX);

// The purpose-built line-art feature-tree glyph (root node + two branch children) used by the
// FeatureManager pane-header chip. Exported so the left-pane rail tab button can render the same
// glyph for the Feature Manager tab instead of the atlas "history" icon. `mn` = top-left, `sz` =
// glyph box size, `ink` = stroke color.
void drawFeatureTreeGlyph(ImDrawList* dl, ImVec2 mn, float sz, ImU32 ink);

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

// Viewport -> FeatureManager sync. Call when a viewport pick is ADDED to the selection: selects the
// tree row that owns it and requests the one-shot scroll (st.treeScrollToSelection), forcing open the
// collapsed section/folder/parent row on the way. Part docs (incl. in-context component edit): the
// owning feature from the mirror's owner map, else the body's latest owning feature. Assembly docs:
// component rows light from the live selection (a pick's body -> its component), so this only asks
// for the scroll. Local and synchronous — it never waits on the engine's doc_resolve_pick reply.
void featureTreeRevealPick(AppState& st, const render::PickHit& hit);

// The assembly component (a tree["components"] entry) that owns AICM body `bodyId`: the body id is
// the 1-based ordinal among NON-suppressed components. nullptr for component-pattern instances
// (synthetic ids) and out-of-range ids.
const nlohmann::json* featureTreeAssemblyComponent(const nlohmann::json& tree, uint32_t bodyId);

// Record the engine's doc_resolve_pick answer for an assembly pick (`componentId`, "<seed>#<k>" for
// a pattern instance) so a pattern-instance body lights its seed component's row (ordinary bodies
// resolve locally). `ownerFeature` (the reply's optional owner_feature: the feature inside that
// component's own part owning the picked face/edge) lights that nested feature row with the
// selected style and opens + scrolls to it, while `hit` stays selected. Scoped to the doc revision.
void featureTreeNoteAssemblyPick(AppState& st, const render::PickHit& hit,
                                 const std::string& componentId,
                                 const std::string& ownerFeature = {});

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
bool        featureTreeOpenDrawingPath(AppState& st, const std::string& path);
std::string featureTreeSaveDoc(AppState& st, bool saveAs);
std::string featureTreeActivePath();
// Silent save of the active DocMirror doc to an absolute .step+ path (no dialog). Used by the
// agent run-finish path to auto-save a freshly named part. No-op if no doc / empty path / worker
// down. On success updates the active tab's path, clears dirty, and records Recent (async).
// When requireNonEmpty is true, refuses (toast "Not saving: part is empty") unless the mirrored
// tree has at least one solid feature/body — keeps zero-progress agent runs from writing empties.
void featureTreeSaveDocToPath(AppState& st, const std::string& path, bool requireNonEmpty = false);

// Open a component's source part in a document tab. Dedups by canonical path: if a tab
// already has that file open, switches to it; otherwise requests a new open. Used by the
// assembly FeatureManager (component / instance-group context menus) and the viewport
// component context menu.
void openComponentPart(AppState& st, const std::string& absPath);

// Live MCP handoff: a separate process (the Chudware MCP server Claude drives) builds parts in
// its OWN engine, so its docs are invisible here. It publishes the part it's working on to
// %APPDATA%\Chudware\handoff\ (live.step + a live.json carrying a monotonically rising "rev").
// Call this once per frame: when the rev changes it opens/refreshes a single "AI Live" tab from
// that file so the user SEES the part the AI is building. Cheap (an mtime check most frames);
// a no-op when the worker isn't up, an in-app run is active, or there's nothing new. Never throws.
void        pollAiCadHandoff(AppState& st);

}  // namespace ui
