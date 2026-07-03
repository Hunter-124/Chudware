// EcadPanel — the ECAD (schematic/PCB) workspace UI. Hosts the schematic/board 2D canvas
// (ecad_canvas.h), a left sub-panel (PCB layer visibility / schematic net-and-symbol list), a
// canvas tool toolbar (Select/Place/Route + part/footprint picker), a property inspector for the
// selected symbol/footprint, an ERC/DRC results strip, and a native-simulator waveform plot
// (schematic only). It drives the C++ core's ecad_* RPCs through WorkerClient and mirrors the open
// document (ecad_mirror.h) — identical engine calls to the host.
//
// Ported from host/src/ui/ecad/ecad_panel.{h,cpp} (H2). Body-only, no Begin/End — like cam_panel.h,
// the panel owns its state in a file-static singleton (no AppState fields; host's Wave-2 per-DocTab
// EcadEditorState is collapsed back to a single active document, matching CAM's single-active-job
// model) and is drawn inside the "ECAD" dock window that layout.cpp pre-registers alongside
// "Viewport###viewport" / "CAM" in the central (tab-bar-hidden) dock node. Since that node hides its
// tab bar (drawViewport sets ImGuiDockNodeFlags_NoTabBar), bringing the ECAD tab forward requires
// ImGui::SetNextWindowFocus() before Begin — ecadTakeFocusRequest() is the seam application.cpp
// polls for that, mirroring ui::camTakeFocusRequest()/ui::previewTakeFocusRequest().
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

class EcadMirror;  // fwd (ecad_mirror.h)

// Draw the ECAD workspace body. Call every frame from inside ImGui::Begin("ECAD"...)/End() in
// application.cpp's frame(). Renders the "design with AI / start blank" launchpad when no ECAD
// document is open yet.
void drawEcadEditor(app::AppState& st);

// Reveal / bring-forward the ECAD panel (called when an ecad_* command runs). Requests focus for a
// few frames, mirroring ui::camEnsureVisible().
void ecadEnsureVisible();
// Shot-only: request sustained focus so a headless AI_CAD_SHOT=ecad capture can show the panel even
// though the central dock node's tab bar is hidden.
void ecadFloatForShot();
// One-shot-per-frame poll: true while an ecadEnsureVisible()/ecadFloatForShot() focus request is
// still pending. application.cpp calls ImGui::SetNextWindowFocus() when true, BEFORE
// ImGui::Begin("ECAD"...).
bool ecadTakeFocusRequest();

// Accessor for the panel's editor mirror, so external code (a ribbon command handler, a scripted
// shot) can inspect it without reaching through ImGui.
EcadMirror& ecadEditorMirror();

// Create a new blank ECAD document (kind "sch" | "pcb") via the worker's ecad_new RPC, adopt it into
// the editor mirror, and reveal the panel. This is the dispatch-site delegate app_state.cpp's
// AppState::dispatch("ecad_new_sch"/"ecad_new_pcb") branches call — see ecad_panel.cpp header note.
void ecadHandleNewDoc(app::AppState& st, const char* kind);

}  // namespace ui
