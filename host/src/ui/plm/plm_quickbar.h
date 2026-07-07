// PLM quickbar — an embedded per-document PLM control for every workspace.
//
// A compact status chip in the status bar (current item's revision + lifecycle +
// unsaved dot) that opens a popover to Checkpoint / Release / open the Project
// Hub, without switching to the PLM workspace. Editing a Released document
// auto-branches to a new-revision draft silently, with an actionable "Discard
// draft" toast (Hunter's locked decision). Self-resolves the active document's
// item on document change (event-driven — never a per-frame RPC).
#pragma once

struct AppState;

namespace ui {

// Draw the quickbar chip inline in the status bar (call after the workspace
// readout). No-op in the PLM workspace (the hub is already showing) and when no
// project folder is resolvable.
void drawPlmQuickChip(AppState& st);

}  // namespace ui
