// Stage panels for the Reverse Engineering workspace center column (contracts/reveng.md):
//   Scale   — aruco_mat / two_point / known_dim -> reveng_set_scale (§8.2), plus the
//             mirrored session.scale state and a "print scale mat" affordance.
//   Model   — reveng_fit_primitives table + reveng_build_parametric kick +
//             describe_reveng_result summary (§3/§8).
//   Deliver — reveng_attach_result, export_reveng_bundle, reveng_print_scale_mat.
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
