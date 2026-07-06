// Document tab strip — browser-style multi-document tabs inside the viewport band.
#pragma once

namespace app { struct AppState; }

namespace ui {

float docTabStripHeight(const app::AppState& state);

// Draw the strip at the current cursor inside the central view window; reserves its own height.
float drawDocTabs(app::AppState& state, float availW);

void openNewDocChooser();

// kind: 0=Part, 1=Assembly, 2=Drawing, 3=Schematic, 4=PCB
void createNewDoc(app::AppState& state, int kind);

enum class TabSaveResult { Saved, Nothing, Cancelled, Failed };

TabSaveResult saveDocTab(app::AppState& state, int idx, bool forceDialog);

// Section-view switcher — compact right-aligned chip row rendered inside the doc tab strip
// that enumerates the linked views of the active part: Model | CAM Job | Sim Study | Drawing.
// Links are derived from co-open tabs (drawingDocId) and app state (camActive, workspace).
// Phase-2 agents: call this from within drawDocTabs after the tab row, right-aligned.
// Returns the width consumed (0 if nothing to show — no active part doc open).
float drawSectionSwitcher(app::AppState& state, float bandR, float y0, float barH);

}  // namespace ui
