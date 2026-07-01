// In-app sampling profiler — libaicad prof_* RPCs + flame graph. See docs/PROFILER.md.
#pragma once

namespace app { struct AppState; }

namespace ui {

void drawProfilerWindow(app::AppState& st);

}  // namespace ui
