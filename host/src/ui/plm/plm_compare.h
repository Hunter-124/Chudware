// PLM Compare page — the rendered geometry diff between two revisions.
//
// "git diff for a 3D model": pick an item and two saved versions, and see the
// difference as an IMAGE (green = added in the newer version, red = removed,
// grey = unchanged) alongside the semantic change list.
//
// Data comes from one async RPC, plm_compare {path, item_id, commit_a,
// commit_b, visual:true}. Its `eval_compare` block carries absolute PNG paths
// written by the engine; this page loads them into GL textures on demand and
// frees them when the selection or project changes. The semantic half
// (`doc_diff` + `summary`) always works, so the Changes view stays available
// even when the visual block comes back degraded (no engine / no Pillow).
#pragma once

struct AppState;

namespace ui {

// Page body, dispatched from drawPlmWorkspace (PlmPage::Compare).
void plmDrawComparePage(AppState& st);

}  // namespace ui
