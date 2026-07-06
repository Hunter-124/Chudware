// T-R5: in-editor PLM surface — history timeline panel ("History"), checkpoint/
// release controls (FM strip + popovers), compare view, doc-tab revision badges,
// and the released-doc silent auto-branch (toast with "back to rev X" affordance).
//
// Backed by the plm_* worker methods (services/plm/worker_rpc.py, contract
// aicad.plm/1) — the same single Vault implementation as the plm_* MCP tools.
// View-model structs live in ui/abstract/plm_view.h (T-U1 convention).
//
// Ease-of-use law (contract §8.1): no git vocabulary in any string here.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Panel visibility (application.cpp Begin/End convention).
bool* plmPanelOpen();
bool* plmComparePanelOpen();

// Panel bodies (call inside an ImGui::Begin window).
void drawPlmPanel(app::AppState& st);
void drawPlmComparePanel(app::AppState& st);

// FM strip controls: rev/lifecycle line + [Checkpoint...] [History] row, plus
// the checkpoint/release popovers. Call from the Feature Manager bottom strip.
void drawPlmStripControls(app::AppState& st, const std::string& docPath);

// Per-frame tick (application.cpp, cheap): drains toast actions (discard-draft)
// and drives the checkpoint/release popover windows.
void plmFrameTick(app::AppState& st);

// Vault lifecycle hooks.
// After a successful on-disk save: opens/creates the project vault for the
// file's folder and records the automatic history entry (contract §3.1).
void plmOnDocSaved(app::AppState& st, const std::string& path);
// On every doc mutation: session-changes log + the T-R5 locked decision —
// first edit of a Released doc silently starts the next-rev draft and toasts
// with a "Back to rev X (discard draft)" action. Never blocks, never prompts.
void plmOnDocMutation(app::AppState& st);

// Doc-tab revision badge (doc_tabs.cpp): text + tooltip for a tab's file path.
// valid=false → no badge (not part of a project / never saved).
struct PlmTabBadge {
    std::string text;      // "A" (released letter) or "B" (draft letter)
    std::string tooltip;   // plain-language lifecycle line
    bool released = false; // accent styling for Released
    bool valid = false;
};
PlmTabBadge plmTabBadgeForPath(const std::string& path);

// ---- shot seams (AI_CAD_SHOT scenarios; scenarios.cpp) ----
// Point the PLM state at a project folder (opens the vault, refreshes status +
// history) and open the History panel.
void plmShotOpenProject(app::AppState& st, const std::string& projectDir);
// Open the checkpoint popover (drafted-message flow) for the active doc.
void plmShotOpenCheckpoint(app::AppState& st);
// Open the compare view between the two most recent story entries.
void plmShotOpenCompare(app::AppState& st);

}  // namespace ui
