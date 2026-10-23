// PLM quickbar — an embedded per-document PLM control for every workspace.
//
// A compact status chip in the status bar (current item's revision + lifecycle +
// unsaved dot) that opens a popover to Checkpoint / Release / open the Project
// Hub, without switching to the PLM workspace. Editing a Released document
// auto-branches to a new-revision draft silently, with an actionable "Discard
// draft" toast (locked design decision). Self-resolves the active document's
// item on document change (event-driven — never a per-frame RPC).
#pragma once

#include <string>

struct AppState;

namespace ui {

// Draw the quickbar chip inline in the status bar (call after the workspace
// readout). No-op in the PLM workspace (the hub is already showing) and when no
// project folder is resolvable.
void drawPlmQuickChip(AppState& st);

// Record a just-completed save into project history when auto-record is on
// (contract plm.md auto_commit). Guards: a resolvable project folder + the saved
// path under it + the cached auto_commit flag. Async, never blocks or fails the
// save; a failure surfaces one Warn toast. *savedAbsPath* is the absolute path
// that was just written.
void plmAutoRecordSave(AppState& st, const std::string& savedAbsPath);
// Spool a just-written file for the optional Box.com mirror. Gated by the persisted
// Box enable flag and a non-empty path; asynchronous and never affects save success.
void boxMirrorNotifySave(AppState& st, const std::string& savedAbsPath);

// Active document's PLM state for the FeatureManager badge. Returns true and
// fills rev/lifecycle/dirty when the active doc is a tracked item; false (and
// leaves the outputs untouched) when it is not tracked.
bool plmActiveDocPlmState(AppState& st, std::string& rev, std::string& lifecycle,
                          bool& dirty);

}  // namespace ui
