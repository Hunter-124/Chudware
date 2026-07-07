// CamPanel — the CAM (computer-aided machining) workspace UI. Hosts the CAM manager tree
// (job -> setups -> operations + tool library), a toolbar to extract stock/geometry from the
// current part and add operations, a 2D toolpath backplot, a material-removal sim summary,
// and the post-processor (dialect -> G-code). It drives the C++ core's cam_* RPCs through
// WorkerClient and mirrors the job tree (cam_mirror.h) — identical engine calls to the host.
//
// Ported from host/src/ui/cam/cam_panel.{h,cpp} (H1). Body-only, no Begin/End — like
// preview_pane.h, the panel owns its state in a file-static singleton (no AppState fields)
// and is drawn inside the "CAM" dock window that layout.cpp pre-registers alongside
// "Viewport###viewport" / "ECAD" in the central (tab-bar-hidden) dock node. Since that node
// hides its tab bar (drawViewport sets ImGuiDockNodeFlags_NoTabBar), bringing the CAM tab
// forward requires ImGui::SetNextWindowFocus() before Begin — camTakeFocusRequest() is the
// seam application.cpp polls for that, mirroring ui::previewTakeFocusRequest().
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

struct CamBackplot;  // fwd (cam_mirror.h)

// Draw the CAM workspace body. Call every frame from inside ImGui::Begin("CAM"...)/End() in
// application.cpp's frame(). Renders an empty-job toolbar/tree when no job is open yet.
void drawCamManager(app::AppState& st);

// Thin workspace header strip: breadcrumb (Job / Setup / Operation) + Generate/Post shortcuts.
// Drawn ABOVE drawCamManager() by drawManufactureWorkspace() — NOT inside the "CAM" dock window.
void drawCamWorkspaceHeader(app::AppState& st);

// Operation-parameter editor form for the currently selected op (stepover/depth/heights/feeds).
// Drawn inside drawCamManager() when an op is selected. No-op when nothing is selected.
void drawCamOpEditor(app::AppState& st);

// Setup-sheet summary (stock dims, machine kind, cycle time, posted nc path).
// Drawn as a tab inside drawCamManager().
void drawCamSetupSheet(app::AppState& st);

// Reveal / bring-forward the CAM panel (called when a cam_* command runs, or the Manufacture
// workspace is entered once that ribbon wiring exists). Requests focus for a few frames.
void camEnsureVisible();
// Shot-only: request sustained focus so a headless AI_CAD_SHOT=cam capture can show the
// populated tree even though the central dock node's tab bar is hidden.
void camFloatForShot();
// One-shot-per-frame poll: true while a camEnsureVisible()/camFloatForShot()/camLoadJob()
// focus request is still pending. application.cpp calls ImGui::SetNextWindowFocus() when true,
// BEFORE ImGui::Begin("CAM"...).
bool camTakeFocusRequest();

// Adopt an EXISTING CAM job doc (e.g. one the copilot's CAM fast-path just built) into the
// panel: point the panel at docId, load its tree + generated toolpaths, and reveal it. Lets a
// core-side cam_* run surface in the GUI instead of leaving the Manufacture panel empty.
void camLoadJob(app::AppState& st, const std::string& docId);
// Handle a CAM ribbon command (cmd ids prefixed "cam_"). Opens the panel and performs the
// action (new job / extract / add operation / generate / simulate / post / ...).
void camHandleCommand(app::AppState& st, const std::string& cmd);

// Viewport overlay accessor: the active job's classified toolpath segments to draw over the
// part (rapid/cut/lead/plunge/arc), or nullptr when there is nothing to show. NOTE: not yet
// wired into ui/imgui/src/ui/viewport.cpp (out of the H1 write-set — see task report).
const CamBackplot* camActiveBackplot();

// ---- Cross-doc dependency (b) -----------------------------------------------
// Pull-model stale check: call once per frame when the Manufacture workspace is
// visible. Compares the cached source-part revision to the part's current revision
// (via doc_list_open). Returns true if the job geometry is now stale (part changed
// since last extract). Callers may show a "Part changed — regen?" toast.
bool camCheckPartStale(app::AppState& st);

// Acknowledge the stale state and force a re-extract + re-generate from the current part.
// Shows the result in g.status. Call when the user clicks "Regen from Part".
void camRegenFromPart(app::AppState& st);

// ---- Part↔CAM resolver (c) --------------------------------------------------
// Given a part docId, find the active CAM job for that part (g.docId when
// g.sourcePartDocId == partDocId) or create a new one. Activates the CAM panel.
// Returns the cam job docId (empty on error).
std::string camFindOrCreateJobForPart(app::AppState& st, const std::string& partDocId);

// Section-switcher integration (c): call this from doc_tabs.cpp drawSectionSwitcher()
// instead of the inline camActive check so the "CAM Job" chip appears for every
// Part/Assembly tab (not just when camActive is already true — circular).
//
// Returns true when a CAM job is available or can be created for partDocId.
// Always true for Part/Assembly tabs — the job is created on demand on click.
// isCurrent is set to true when the CAM panel is currently the active view.
//
// On click from the switcher: call camActivateForPart(st, partDocId) — this is
// the one-liner the integrator adds to doc_tabs.cpp drawSectionSwitcher's
// clicked-and-not-current branch for the "CAM Job" entry.
bool camHasJobForPart(const std::string& partDocId);
void camActivateForPart(app::AppState& st, const std::string& partDocId);

}  // namespace ui
