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

}  // namespace ui
