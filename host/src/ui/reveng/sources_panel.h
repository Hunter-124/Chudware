// RE-30: Sources entry panel — part-number/URL/query field (→ reveng_research_source),
// datasheet dropzone (→ reveng_ingest_datasheet / img_tex), and scan/webcam entry
// (→ RE-19 capture suite).  GL-thread only; all service calls are ASYNC.
#pragma once

struct AppState;

namespace ui {
namespace reveng {

// Draw the Sources entry panel into the current ImGui child context.
// Called from reveng_workspace.cpp when the active stage == Stage::Source.
void drawSourcesPanel(AppState& st);

}  // namespace reveng
}  // namespace ui
