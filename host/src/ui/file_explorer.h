// Explorer: the working folder's file browser.
//
// Layout (top to bottom): one header line (working-folder name + vault branch chip), a filter
// field with a compact toolbar (New folder / Refresh / Collapse all / Reveal active document),
// the file tree taking all remaining height, then collapsible Recent and Changes strips pinned
// at the bottom (both collapsed by default).
//
// The tree lists the ACTUAL contents of AppState::workspaceDir (its children at top level, all
// non-dot files; non-documents dimmed), folders first, natural numeric-aware order. A non-empty
// filter switches to a flat result list from a bounded incremental FsBackend walk. Rows show
// vault change code + revision at the right, an open-in-tab dot, and the active document
// highlighted. Changes carries M/A/D/U rows, the project-wide checkpoint and links into PLM.
// Explicit revision-named sibling CAD files are grouped visually as distinct, individually
// openable files, never auto-merged or deleted.
//
// Keys (pane focused): Ctrl+F filter, Esc clear/cancel, Up/Down/Left/Right navigate, Enter open,
// F2 rename, Del delete. Right-click: Open / History / Release in PLM / Copy path / Show in file
// manager / Rename / Copy / Paste / Delete; folders add New Folder and Set as working folder.
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
