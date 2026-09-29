// Explorer: the working project's PLM changes/checkpoint and browsable file tree.
//
// The rail shows vault-derived M/A/D/U changes and revision/lifecycle labels,
// with a project-wide checkpoint action and links into the full PLM workspace.
// Explicit revision-named sibling CAD files are grouped visually as distinct,
// individually openable files, never auto-merged or deleted.
//
// The tree is lazy, rooted at AppState::workspaceDir and filtered to known
// CAD/ECAD extensions. Right-click Open / Rename / Copy / Paste / Delete /
// New Folder, inline F2 rename and Del-to-delete remain available.
#pragma once

#include <string>

struct AppState;

namespace ui {

// Resolve AppState::workspaceDir. Desktop creates it once per root change.
// Web discovers the gateway-owned root asynchronously: workspaceDir stays empty
// while pending, and no network write/yield occurs. Safe to call every frame.
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
