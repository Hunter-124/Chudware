// Compact PLM overview for the left manager column (Model tab strip).
//
// Square "PLM" tab sits left of Explorer. Body: active project directory header,
// asset tree (parts/assemblies/drawings/boards) with status dots, and a recency
// sorted change feed (PLM history + AI-VC timeline). File click filters the feed.
// Degrades to a plain project-directory listing when PLM RPCs are absent.
#pragma once

struct AppState;

namespace ui {

// Left-pane tab id for the compact PLM overview. Numeric slot 5 — kept out of
// AppState::LeftPaneTab so this wave does not touch app_state.h; leftPaneTab is
// an int and accepts it. Must not collide with kLeftPane* 0..4 in layout.h.
enum { kLeftPanePLM = 5, kLeftPaneGraph = 6 };

// Draw the PLM overview tab body (no Begin/End). Call from drawLeftPane when the
// PLM tab is active. Async RPC only; safe on the GL thread.
void drawPlmOverviewBody(AppState& st);

}  // namespace ui
