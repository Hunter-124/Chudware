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

// True once the workspace's startup fetches have resolved: the category tree (or its flat
// fallback) landed or the loader gave up, and vendor status landed. Also true when the worker
// is not running (nothing will ever load). Headless captures (CHUDWARE_SHOT=parts, the
// workspaces sweep) wait on this instead of photographing the first-frame spinners.
bool partsLibrarySettled(const AppState& st);

}  // namespace ui
