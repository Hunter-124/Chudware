// AI workspace — Session Plan board.
//
// The active project's selected conversation owns the editable PlanState checklist.
// The header identifies its session title and linked document; selecting another CAD tab
// does not transfer ownership of the plan. Edits mutate st.chat and mark history dirty.
// While a turn is active, the existing queued-prompts path delivers a short plan note to
// the next turn, not to the already-running turn.
//
// The checkpoint strip is separate: it displays only the selected document's AI version
// history inside this project, with Restore routed through ui::aiVcRollback. It never
// requests a global timeline without a project document selected.
//
// planBoardSerialize/planBoardApply round-trip one conversation's plan snapshot to JSON
// (plan object shape matches conversations.cpp planStateToJson so parsePlanState accepts
// it); the orchestrator wires these into session save/load.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// The Session Plan tab body (drawn inside the DAG pane child; scrolls automatically).
void drawPlanBoardView(AppState& st);

// Snapshot the CURRENT plan (newest plan-carrying message) of the conversation `convId`
// to JSON: {conv_id, present, plan?}. present=false + no plan key when the conversation
// is unknown or carries no plan.
nlohmann::json planBoardSerialize(const AppState& st, const std::string& convId);

// Restore a snapshot produced by planBoardSerialize: overwrites the conversation's
// newest plan-carrying message when one exists, else attaches to the last assistant
// turn (creating a plan-only turn as a last resort). No-op for unknown conversations,
// malformed JSON, or present=false.
void planBoardApply(AppState& st, const std::string& convId, const nlohmann::json& j);

}  // namespace ui
