// File Explorer panel (Wave 3, GUI rework).
//
// Renders the "Explorer" tab of the SolidWorks-faithful tabbed left pane (ui::drawLeftPane in
// layout.cpp routes kLeftPaneExplorer here). A workspace file browser: a recursive, lazy-expand
// directory tree rooted at AppState::workspaceDir (default Documents/AI-CAD), filtered to known
// CAD/ECAD extensions, plus a "Recent" section backed by AppState::recentFiles.
//
// Like drawFeatureTreeBody, this draws into the caller's container child — it does NOT open its own
// Begin/End. The standalone right-click context menu (Open / Rename / Copy / Paste / Delete /
// New Folder), inline rename (F2), and Del-to-delete are handled inside the body.
#pragma once

#include <string>

struct AppState;

namespace ui {

// Ensure AppState::workspaceDir is set + the directory exists. Idempotent; safe to call every
// frame. Defaults workspaceDir to <Documents>/AI-CAD (creating it) when empty, falling back to
// the app data dir if Documents can't be resolved. Never throws.
void ensureWorkspaceDir(AppState& st);

// Draw the Explorer tab body (no Begin/End). Call every frame from drawLeftPane's Explorer case.
void drawFileExplorerBody(AppState& st);

// ---- Wave 3: Explorer-tab clipboard, driven by the global Ctrl+C / Ctrl+V router in app.cpp ----
// fileExplorerActive: true when the Explorer tab is the active left-pane tab (so app.cpp routes
//   Ctrl+C/V to file copy/paste instead of FeatureManager feature copy/paste).
// fileExplorerCopySelection: stash the currently-selected Explorer path for a later paste.
//   Returns true if a path was copied.
// fileExplorerPaste: filesystem-copy the stashed path into the current Explorer folder (handles
//   name collisions with a " (copy)" suffix; toasts on permission errors). Returns true on success.
bool fileExplorerActive(const AppState& st);
bool fileExplorerCopySelection(AppState& st);
bool fileExplorerPaste(AppState& st);

}  // namespace ui
