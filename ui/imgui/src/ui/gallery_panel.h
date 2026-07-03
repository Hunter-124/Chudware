// T-U9 Sample Gallery panel — browse the T-Q3 sample library (samples/index.json), open a
// sample's editable document via the worker, launch its guided tour script if it has one.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model in
// ui/abstract/gallery_view.h (no ImGui includes there). Screenshot-verifiable via
// AI_CAD_SHOT=gallery.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window, same convention as
// drawPartsPanel() / drawScaffoldDemoPanel().
void drawGalleryPanel(app::AppState& s);

// Open the (closable) gallery pane. Costs nothing while closed.
void openGalleryPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open (close box).
bool* galleryPanelOpen();

// Queue opening `docAbsPath` via the worker (doc_open). Retries every frame until
// s.engineReady (the worker may not be up yet — e.g. right after first-run) rather than
// silently dropping the request the way file_explorer's one-shot takeExplorerOpenRequest()
// does. Call ui::tickGalleryOpenRequest(s) once per frame (application.cpp, alongside
// s.rpc.drain()) to service it.
void requestOpenSample(const std::string& docAbsPath);

// Per-frame pump for a pending requestOpenSample() call. No-op when nothing is queued.
void tickGalleryOpenRequest(app::AppState& s);

// Scenario seam (AI_CAD_SHOT=gallery): select the first loaded sample and queue opening it,
// so the shot proves the full click -> doc_open -> editable-tree path, not just the thumbnail
// grid. No-op if samples/index.json hasn't loaded yet (call after openGalleryPanel(), same
// frame drawGalleryPanel() first runs its lazy load).
void selectAndOpenFirstSampleForShot();

}  // namespace ui
