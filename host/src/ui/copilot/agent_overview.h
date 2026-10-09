// Copilot agent overview: the live orchestration block pinned just above the composer — one
// compact row per sub-agent of the viewed conversation (status, name, brief, ticking elapsed,
// tool calls, last step; nested children indented; click shows its recent steps inline) and the
// conversation's persistent todo list ("phase · k/N" checklist, active item highlighted, done
// items struck through). Open while the conversation runs, a one-line summary when idle; the
// user's disclosure choice holds until the next run starts.
//
// Sources: the AgentDag execution trace (ui/ai/agent_dag.h — live events, else the persisted
// trace / transcript reconstruction) and the newest PlanState in the viewed transcript, which
// stays shown across turns until every item is closed. Both persist with the conversation.
// GL thread only.

#pragma once

struct AppState;

namespace ui {

// Draws nothing (zero height) when the viewed conversation has neither sub-agents nor an open
// todo list. `width` is the block's width in px (the composer column's content width).
void drawAgentOverview(AppState& st, float width);

}  // namespace ui
