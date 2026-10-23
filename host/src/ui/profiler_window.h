// In-app sampling-profiler window — a text report + an interactive flame graph.
//
// Gated by AppState::showProfilerWindow. Start/Stop (and Refresh) drive the libchudware prof_*
// RPCs through core::cpp_core() — the SAME seam host/src/app.cpp's Evaluate>Profiler toggle
// uses (prof_start {hz,mode} / prof_stop {top,...} / prof_report {top,...}). The returned
// report renders as module self-time + flat self/total tables, plus a flame graph parsed from
// the report's "folded" stacks (cached, rebuilt only when a new report arrives). The host links
// nothing new: everything goes through the already-loaded libchudware.{so,dll}.
#pragma once

struct AppState;

namespace ui {

// Draw the profiler window when st.showProfilerWindow is true (no-op otherwise). Call once per
// frame from the main draw loop, alongside the other panels/dialogs. While open, also draws a
// labeled live FPS counter (window header + top-right overlay).
void drawProfilerWindow(AppState& st);

}  // namespace ui
