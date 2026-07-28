// AI workspace UI preferences — config.json-backed knobs for the command-center views.
// Persisted under agent.ui_* (same load-modify-write pattern as agent.max_subagent_depth).
// Defaults favor FULL visibility (user directive: everything visible while the OMP/agent
// integrations are being validated; dial back later via Settings or the workspace gear).
#pragma once

namespace ui {

// 0 = quiet (titles + status only), 1 = normal (last op), 2 = full (op tickers, counts,
// session strip, token/latency detail). Default 2.
int  aiUiVerbosity();
void setAiUiVerbosity(int v);

// Autopopulate: views follow the ACTIVE chat session / latest run instead of manual picking.
// Default true.
bool aiFollowActiveSession();
void setAiFollowActiveSession(bool on);

// Active Work: render iso preview textures for agent-touched docs (thumbnail worker jobs).
// Default true; when false the cards show the lightweight placeholder only.
bool aiActiveWorkPreviews();
void setAiActiveWorkPreviews(bool on);

// ---- chat transcript display (Settings -> AI -> Harness "Chat display") ---------------------
// These are read from inside per-frame draw code for EVERY activity row, so they go through the
// cached accessors below (one config.json read per process, invalidated by the setters) rather
// than loadAgentInt's read-on-every-call path.

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
