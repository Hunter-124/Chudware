// PLM Compare page — the rendered geometry diff between a file's adjacent
// versions.
//
// "git diff for a 3D model": pick a FILE in the left rail and step through that
// file's own versions; each step's diff is resolved AUTOMATICALLY against the
// version before it (green = added in the newer version, red = removed, grey =
// unchanged) alongside the semantic change list. No revision pickers, no
// Compare button, no dropdowns.
//
// The page owns two async RPCs: plm_history {path, item_id} for the selected
// file's own timeline (newest-first; the last entry is the file's creation and
// is a terminal step, not a broken diff), and plm_compare {path, item_id,
// commit_a, commit_b, visual:true} for the diff itself. The compare result's
// `eval_compare` block carries absolute PNG paths written by the engine; this
// page loads them into GL textures on demand and frees them when the selection
// or project changes. The semantic half (`doc_diff` + `summary`) always works,
// so the Changes view stays available even when the visual block comes back
// degraded (no engine / no Pillow).
#pragma once

struct AppState;

namespace ui {

// Page body, dispatched from drawPlmWorkspace (PlmPage::Compare).
void plmDrawComparePage(AppState& st);

// True while this page has an RPC in flight (per-item history or the compare
// itself). Used by the CHUDWARE_SHOT=git headless capture to wait for real
// content instead of photographing a spinner.
bool plmCompareBusy();

}  // namespace ui
