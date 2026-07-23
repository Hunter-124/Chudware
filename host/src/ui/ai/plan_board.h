// AI workspace — Session Plan board.
//
// Interactive replacement for the static buildgraph "Session Plan" view: the active
// conversation's current PlanState (the same per-message plan the Copilot plan section
// renders — PlanState in core/app_state.h) presented as an EDITABLE checklist with
// per-item status cycling, inline rename, add/remove, and up/down reordering. Edits
// mutate the live plan in st.chat and flag the chat history dirty; while an agent turn
// is running a concise "Plan updated: ..." note is pushed through the existing
// queued-prompts path (drawComposer drains it into the next turn — no fabricated steer).
//
// Below the checklist, an ai_vc (agent version control) timeline strip lists the active
// document's silent checkpoints (plm_timeline over st.rpc, throttled) with a per-row
// Restore that routes through the shared ui::aiVcRollback (honors chat_truncate_to,
// refreshes the doc mirror, toasts). Every state degrades honestly: worker down, ai_vc
// method absent, no plan, no checkpoints.
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
