// AI workspace — Plan board.
//
// The active project's selected session owns the editable PlanState checklist.
// The header identifies its session title and linked document; selecting another CAD tab
// does not transfer ownership of the plan. Edits mutate st.chat and mark history dirty.
// While this session's run is live, each edit is steered into that run (agentControlDispatch)
// with the full edited plan. Otherwise the edit stays local and the next user turn in the
// session carries the edited plan (planBoardTakeEditsForTurn) — a plan edit never queues a
// prompt or starts a turn by itself.
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

// The Plan view body (drawn inside the AI workspace view pane; scrolls automatically).
void drawPlanBoardView(AppState& st);

// Plan edits made in `convId` while no run could receive them, as a note for the next user turn
// in that session: "" when the plan was not edited since the last turn (or was already steered
// into a live run). Clears the pending flag, so call it once per turn, from the send path,
// appending the note to the prompt sent to the agent (not to the visible user bubble).
std::string planBoardTakeEditsForTurn(AppState& st, const std::string& convId);

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
