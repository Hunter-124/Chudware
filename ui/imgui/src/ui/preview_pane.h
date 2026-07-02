// Universal in-app preview pane (T-F16): a docked "Preview" window listing Copilot artifacts
// (artifact_cards.h) with a render_document-backed preview on the right. See ui/abstract/
// artifact_view.h for the view-model this renders from and services/worker/async_rpc.h for the
// non-blocking RPC seam (render_document / list_artifacts) that fills it in.
#pragma once

#include "artifact_view.h"

#include <string>
#include <vector>

namespace app { struct AppState; }

namespace ui {

// Body only (no Begin/End) — called inside the docked "Preview###preview" window.
void drawPreviewPane(app::AppState& s);

// Fetch + show one artifact's preview (async; render_document). Sets state=Loading immediately.
void requestArtifactPreview(app::AppState& s, const std::string& path);

// Refresh the artifact-card list (async; list_artifacts). Reentry-guarded.
void refreshArtifactList(app::AppState& s);

// Scenario seam: seed the pane deterministically, bypassing the worker (headless screenshots).
void previewPaneSeedForShot(const std::vector<aicad::ui::ArtifactCard>& cards,
                             const aicad::ui::PreviewModel& model);

// Ask the docked Preview window to take focus on its next draw (explorer "Preview" action,
// scenario seeding). Consumed by previewTakeFocusRequest().
void requestPreviewFocus();

// Open the (closable) Preview pane and focus its dock tab. The pane docks into the lower-right
// task-pane slot (layout.cpp) and costs nothing while closed.
void openPreviewPane();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open (close box).
bool* previewPaneOpen();

// One-shot: true the first call after requestPreviewFocus() / a scenario seeded the pane.
bool previewTakeFocusRequest();

}  // namespace ui
