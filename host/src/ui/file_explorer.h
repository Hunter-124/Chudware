// Explorer: the working folder's file browser, plus the left column's Changes tab.
//
// Layout (top to bottom): one header line (working-folder name, or the breadcrumb of the folder
// on screen in the tiles view with a Back button after it, + vault branch chip), a filter field
// with a compact toolbar (New folder / Refresh / Collapse all / Reveal active document /
// Tree-Tiles toggle), the file view taking all remaining height, then a collapsible Recent strip
// pinned at the bottom (collapsed by default; native documents as preview cards).
//
// Two views of the ACTUAL contents of AppState::workspaceDir (all non-dot files; non-documents
// dimmed; folders first, natural numeric-aware order):
//   tree  — the folder tree; native .step+ documents are card rows with a rendered preview
//           (file_thumbs.h), their name without extension and a kind · age subtitle.
//   tiles — one folder at a time as a grid of cards (folders, previews, file icons); double-click
//           a folder to enter it, the breadcrumb, Back, Backspace or the mouse Back button to go up.
// The view is automatic (tiles on the empty workspace home, workspace_home.h; tree otherwise)
// until the toolbar toggle picks one for the session. A non-empty filter switches either view to
// the flat results of a bounded incremental FsBackend walk. Rows show a tone dot for an
// uncheckpointed vault change (the tooltip names it) + the revision, an open-in-tab dot, and the
// active document highlighted. Explicit revision-named sibling CAD files are grouped visually in
// the tree as distinct, individually openable files, never auto-merged.
// Opening a part or assembly from the Explorer hands the pane to that document's FeatureManager
// once its tab is active.
//
// Changes (its own left-column tab): the working folder's uncheckpointed vault changes grouped
// Modified / Added / Deleted / Untracked, native documents as preview cards; double-click opens
// (a deleted file opens its PLM history), right-click Open / Show in Explorer / History /
// Release in PLM / Copy path. The checkpoint composer (message, Checkpoint N changes, Open PLM)
// is pinned under the list.
//
// Keys (pane focused): Ctrl+F filter, Esc clear/cancel, Up/Down/Left/Right navigate, Enter open,
// Backspace up a folder (tiles), F2 rename, Del delete. Right-click: Open / History / Release in
// PLM / Copy path / Show in file manager / Rename / Copy / Paste / Delete; folders add New Folder
// and Set as working folder.
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

// Draw the Changes tab body (no Begin/End). Call every frame from drawLeftPane's Changes case.
void drawChangesPaneBody(AppState& st);

// Uncheckpointed changes in the working folder as of the last vault scan (the scan refreshes
// while the Explorer or Changes tab is shown; 0 before the first one). For the tab's count badge.
int fileChangesCount(const AppState& st);

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

// CHUDWARE_SHOT=explorer: true once the capture may be taken (previews rendered, and any
// CHUDWARE_EXPLORER_SHOT_OPEN document active with its mesh).
bool fileExplorerShotSettled(const AppState& st);

}  // namespace ui
