// AI workspace UI preferences — config.json-backed knobs for the command-center views.
// Persisted under agent.ui_* (same load-modify-write pattern as agent.max_subagent_depth).
// Every accessor is cached (one config.json read per process, invalidated by its setter), so
// per-frame reads are cheap and AI > Options and Settings > AI always agree.
#pragma once

namespace ui {

// 0 = quiet (titles + status only), 1 = normal (last op), 2 = full (op tickers, counts,
// session strip, token/latency detail). Default 2.
int  aiUiVerbosity();
void setAiUiVerbosity(int v);

// Follow live runs: the AI workspace jumps to Current Run when the viewed session starts a new
// run (never on a plain session switch). Default true.
bool aiFollowActiveSession();
void setAiFollowActiveSession(bool on);

// Active Work: render preview thumbnails for agent-touched docs.
// Default true; when false the cards show the lightweight placeholder only.
bool aiActiveWorkPreviews();
void setAiActiveWorkPreviews(bool on);

// The AI workspace's selected nav view (0 Current Run, 1 Plan, 2 Activity, 3 Sessions,
// 4 Insights, 5 Options), restored across restarts. Default 0.
int  aiWorkspaceView();
void setAiWorkspaceView(int v);

// One label set for the workspace-visibility knobs, shared by AI > Options and Settings > AI.
namespace aiPrefCopy {
inline constexpr const char* kDetailLabel   = "Detail level";
inline constexpr const char* kDetailHelp    =
    "How much detail the Current Run, Plan and Active Work views show. Quiet = titles and "
    "status. Normal = the latest step. Full = every step, counts and timing.";
inline constexpr const char* kDetailLevels[3] = {"Quiet", "Normal", "Full"};
inline constexpr const char* kFollowLabel   = "Follow live runs";
inline constexpr const char* kFollowHelp    =
    "Switch to Current Run when the session you are viewing starts a new run.";
inline constexpr const char* kPreviewsLabel = "Active Work previews";
inline constexpr const char* kPreviewsHelp  =
    "Show preview thumbnails of the documents agents are editing. Off = a lightweight "
    "placeholder only.";
}  // namespace aiPrefCopy

// ---- chat transcript display (Settings -> AI -> Harness "Chat display") ---------------------
// Read from per-frame draw code for EVERY activity row — the cached accessors make that cheap.

// How much of a tool call the transcript shows.
//   0 = simplified  — human sentence only; no raw JSON, no engine ids, no latency
//   1 = normal      — human sentence + expandable raw drawer (default)
//   2 = diagnostic  — everything expanded-capable: raw args, engine doc ids, latency, digests
// Default 1.
int  chatToolDetail();
void setChatToolDetail(int v);

// Animate thinking/tool boxes closing when the next step starts (vs. snapping shut). Default true.
bool chatCollapseAnim();
void setChatCollapseAnim(bool on);

// Fold consecutive calls of the SAME tool into one grouped card. Default true.
bool chatGroupToolCalls();
void setChatGroupToolCalls(bool on);

// Show the agent's own render images inline in the transcript and the Artifacts panel.
// Default true.
bool chatShowRenders();
void setChatShowRenders(bool on);

// Substitute engine doc ids ("doc-cpp0000001f") with the document's human name in agent text.
// Off => raw ids are left verbatim (diagnostic). Default true.
bool chatHumanizeDocIds();
void setChatHumanizeDocIds(bool on);

}  // namespace ui
