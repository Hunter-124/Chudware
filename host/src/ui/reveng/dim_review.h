// dim_review.h — Reverse Engineering "Review" stage: the dimension-confirm ledger and the
// rebuild-vs-scan deviation report (RE-40 overhaul of RE-22).
//
// This is the human gate of the RE pipeline. Everything upstream produces NUMBERS; this stage
// is where an engineer decides whether those numbers are trustworthy enough to print a
// replacement part from:
//
//   drawDimReviewPanel(st) — the per-session dimension ledger from list_reveng_dims, confirmed
//                            through confirm_dimensions. Fit-critical rows are visually
//                            dominant and must be confirmed individually; a fit-critical row
//                            whose provenance is `guessed` is an ALARM (contracts/reveng.md §0
//                            forbids silent guessing on a dimension that decides fit).
//   drawDeviationPanel(st) — get_reveng_deviation rendered as an error field + bucket histogram
//                            + a plain-language verdict against the documented 0.1 / 0.5 mm
//                            tiers, not as three bare numbers.
//   drawReviewStage(st)    — the stage entry point reveng_workspace.cpp calls. Owns the split
//                            between the two cards and their collapse state.
//
// GL-thread only. All service access via st.rpc.call (ASYNC) — NEVER sync worker calls in draw
// paths (known freeze pattern, see memory/gl-thread-rpc-freeze.md).
#pragma once

struct AppState;

namespace ui {
namespace reveng {

// Draw the whole Review stage body: dimension ledger above, deviation report below, each a
// collapsible glass card. The ledger is capped at a share of the stage height and scrolls
// internally so the deviation report is always reachable; collapsing either card hands its
// space to the other.
void drawReviewStage(AppState& st);

// The dimension ledger on its own (list_reveng_dims → confirm_dimensions). Renders an
// EmptyState for a session that legitimately has no dimensions yet — never demo rows.
void drawDimReviewPanel(AppState& st);

// The deviation report on its own (get_reveng_deviation). Instructive refusals (no reference
// mesh, B-Rep auto-tessellation not wired) render as a BlockedState with the fix inline.
void drawDeviationPanel(AppState& st);

// Seed the canned dimension + deviation fixture used by the CHUDWARE_SHOT=reveng_review headless
// capture. HARD-GATED: returns immediately unless $CHUDWARE_SHOT is set, so demo rows can never
// reach a real session. When the fixture is live the card header says so.
void seedCannedDimPayload();

}  // namespace reveng
}  // namespace ui
