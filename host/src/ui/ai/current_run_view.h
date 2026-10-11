#pragma once
struct AppState;
namespace ui {
// Every frame the AI workspace is drawn: registry poll, Current Run roster, and the Sub-agents
// hub's scope/roster (its nav badge reads subagentHubLiveCount()).
void pollCurrentRun(AppState& st);
int currentRunLiveCount();
void drawCurrentRunView(AppState& st);
// Sub-agents hub: every sub-agent of the active project (all its sessions, or the open session
// only), flat or grouped by parent, with a docked detail/lineage/recent-activity pane.
int subagentHubLiveCount();
void drawSubagentHubView(AppState& st);
}  // namespace ui
