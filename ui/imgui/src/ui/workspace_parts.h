// Parts Library workspace body — standard parts / catalog browser (FullViewport kind).
//
// Wiring: viewport.cpp calls drawPartsWorkspace() when workspace == PartsLibrary
// (full-viewport content-swap — no 3D scene beneath).
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Parts Library workspace body. Called from viewport.cpp content-swap when
// workspace == PartsLibrary. Full-viewport takeover (FullViewport kind).
// Contains:
//   - Left nav rail: category quick-filter (Fasteners / Bearings / Sections / …)
//   - Right main area: drawPartsPanel() (search bar + domain chips + result rows + detail card)
void drawPartsWorkspace(app::AppState& s);

// Set the active domain filter from the nav rail.  Called by workspace_parts.cpp
// when the user clicks a category; parts_panel.cpp picks it up on the next frame
// to re-fire the search with the new domain.
void partsSetNavDomain(const std::string& domain);

} // namespace ui
