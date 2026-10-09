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

// True once startup categories (or their fallback/final failure), vendor status, and the
// real initial catalog browse have resolved, including index build and live results.
// Also true without a worker. CHUDWARE_SHOT=parts and the workspaces sweep wait here
// instead of photographing an empty or synthetic result set.
bool partsLibrarySettled(const AppState& st);

}  // namespace ui
