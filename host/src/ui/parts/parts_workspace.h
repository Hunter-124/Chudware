// Parts Library workspace: full-viewport body for the PartsLibrary title-bar tab.
// Catalog browse/search over the scraped standard-parts services, vendor login
// browsers (McMaster-Carr etc.), and the background download queue.
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — fills the given band like
// drawViewportPanel does). Safe to call every frame; degrades to an informative
// empty-state when the parts services are not running.
void drawPartsLibraryWorkspace(AppState& st);

}  // namespace ui
