// PLM workspace: full-viewport body for the PLM title-bar tab.
// Vault status, checkpoints, history timeline, release/lock state over services/plm.
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — fills the given band like
// drawViewportPanel does). Safe to call every frame; degrades to an informative
// empty-state when the PLM vault is not initialized.
void drawPlmWorkspace(AppState& st);

// Select a hub page (used by the CHUDWARE_SHOT=git headless capture).
void plmSelectGitPage();
void plmSelectPlanningPage();
void plmSelectTeamPage();
void plmSelectOverviewPage();
void plmSelectFilesPage();
void plmSelectVersionsPage();
void plmSelectGraphPage();
void plmSelectComparePage();
void plmSelectReleasesPage();

// Start (or restart) the Project Hub guided tour band — also lets a headless
// shot force the tour on without clicking through onboarding.
void plmStartGuidedTour();

// True while the active PLM page still has data in flight. The
// CHUDWARE_SHOT=git capture waits on this so it photographs real content
// instead of a spinner.
bool plmBusy();

}  // namespace ui
