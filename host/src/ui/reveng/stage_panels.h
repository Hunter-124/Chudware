// Stage panels for the Reverse Engineering workspace center column (contracts/reveng.md).
// All three are built entirely from the ui::reveng::kit visual kit (reveng_widgets.h) — glass
// cards, chips, honesty tags, previews, empty/blocked states — and size themselves from
// ui::metrics::re*(). No ad-hoc child cards, no bare error text, no pixel literals.
//
//   Scale   — segmented method chooser (aruco_mat / two_point / known_dim) -> reveng_set_scale
//             (§8.2). Every method SHOWS something: the printed mat as a HeroPreview, the
//             two-point pick drawn live on the source photo (click to set P1/P2, numeric
//             fields synced both ways), the known-dim source driving an honesty tier. The
//             always-visible result card mirrors session.scale and carries a plausibility
//             readout so a wrong scale is caught before it is confirmed.
//             `reason: "scale_needs_sparse"` renders as a BlockedState that jumps to Reconstruct.
//   Model   — reveng_fit_primitives rendered as one card per fit, grouped by kind, with the
//             cylinder role (hole/boss/shaft), through/blind, depth_mm, residual and inlier
//             count; sections[] folded in as cross-sections; the fitter's notes[] surfaced.
//             A Mesh preview slot shows the geometry being fitted and a Parametric slot the
//             built result. reveng_build_parametric is gated on the approximate honesty flag
//             with an explicit DangerButton override, and describe_reveng_result distinguishes
//             "not built yet" from "built but unscored".
//   Deliver — trust hero (preview + match + scale state + honesty tier), reveng_attach_result
//             and export_reveng_bundle with copyable result paths, the scale-mat card, and a
//             provenance summary an engineer can paste into the design record.
//
// Both fit/build/scale actions are gated on RevEngState::caps (the reveng_status mirror):
// an unprovisioned backend renders a DISABLED control naming the missing piece, never an
// enabled control that fails after the click.
//
// GL-thread only. NEVER sync worker calls in draw paths — all RPCs are async
// (st.rpc.call with a callback that the worker drains on the GL thread).
#pragma once

struct AppState;

namespace ui {
namespace reveng {

void drawScalePanel(AppState& st);
void drawModelPanel(AppState& st);
void drawDeliverPanel(AppState& st);

}  // namespace reveng
}  // namespace ui
