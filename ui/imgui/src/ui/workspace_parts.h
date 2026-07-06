// Parts Library workspace body — standard parts / catalog browser (FullViewport kind).
//
// Phase-2 hook: fill drawPartsWorkspace() with Parts-specific chrome. The parts
// panel backend is drawPartsPanel() (parts_panel.h — T-U2, full federated search
// + detail card + insert-via-part_place). This workspace gives the catalog browser
// a full-screen home instead of a side-panel pane.
//
// Wiring: viewport.cpp calls drawPartsWorkspace() when workspace == PartsLibrary
// (full-viewport content-swap — no 3D scene beneath).
#pragma once

namespace app { struct AppState; }

namespace ui {

// Parts Library workspace body. Called from viewport.cpp content-swap when
// workspace == PartsLibrary. Full-viewport takeover (FullViewport kind).
// Phase-2: expand the header bar — add a category nav rail (Fasteners / Bearings /
// Seals / Custom…), a filter chip row, and wire the Insert button to part_place dispatch.
void drawPartsWorkspace(app::AppState& s);

} // namespace ui
