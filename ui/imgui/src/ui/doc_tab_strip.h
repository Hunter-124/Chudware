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

}  // namespace ui
