// dim_review.h — RE-22: dimension-confirm review UI + deviation view for the RE workspace.
//
// Two panels driven from reveng_workspace.cpp when the active stage is Stage::Review:
//
//   drawDimReviewPanel(st)  — table of confirm_dimensions payloads (value, source tier,
//                             edit-box), Accept/Edit per row, submits via async RPC.
//   drawDeviationPanel(st)  — mesh-vs-model max/RMS + per-region heat readout from
//                             get_reveng_deviation (RE-16).
//
// GL-thread only. All service access via st.rpc.call (ASYNC) — NEVER sync worker calls
// in draw paths (known freeze pattern, see memory/gl-thread-rpc-freeze.md).
#pragma once

struct AppState;

namespace ui {
namespace reveng {

// Draw the dimension-confirm review table for the selected session's pending dims.
// Canned payload if no live session: shows three example rows so the shot works headless.
void drawDimReviewPanel(AppState& st);

// Draw the deviation heat summary (max_mm / rms_mm / buckets) for the selected session.
// Shows an informative empty-state when no deviation data is loaded yet.
void drawDeviationPanel(AppState& st);

// Seed the dim-review panel with a canned confirm_dimensions payload (used by
// AI_CAD_SHOT=reveng_review headless verification — injected before the first frame).
void seedCannedDimPayload();

}  // namespace reveng
}  // namespace ui
