// Sources stage — the two intake lanes. Scan: webcam / photos / video / phone capture + gallery
// (RE-19 capture suite). From reference: search for an existing CAD model first
// (reveng_find_models → reveng_use_model), and only then build one from documents
// (reveng_research_source / reveng_ingest_datasheet). GL-thread only; all service calls are ASYNC.
#pragma once

#include <nlohmann/json_fwd.hpp>

struct AppState;

namespace ui {
namespace reveng {

// Draw the Sources entry panel into the current ImGui child context.
// Called from reveng_workspace.cpp when the active stage == Stage::Source.
void drawSourcesPanel(AppState& st);

// Mirror a session.reference object (get_reveng_session) into RevEngState::ref, so a reopened
// session shows its last model search and chosen model. No-op while a search is in flight.
void mirrorReference(const nlohmann::json& reference);

}  // namespace reveng
}  // namespace ui
