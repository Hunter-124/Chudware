// PLM workspace: full-viewport body for the PLM title-bar tab.
// Vault status, checkpoints, history timeline, release/lock state over services/plm.
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — fills the given band like
// drawViewportPanel does). Safe to call every frame; degrades to an informative
// empty-state when the PLM vault is not initialized.
void drawPlmWorkspace(AppState& st);

}  // namespace ui
