// ECAD workspace body — delegates to the ECAD editor panel + adds workspace chrome.
//
// Backend: drawEcadEditor(s) (ecad/ecad_panel.h) renders the full schematic /
// PCB canvas (KiCad-style canvas, component palette, net inspector). This file
// wraps that call so phase-2 agents can add workspace-level chrome (a title strip
// with Schematic/PCB sub-workspace toggle, ERC/DRC buttons) without editing
// ecad_panel.cpp or viewport.cpp.
//
// Phase-2 agents: add a compact workspace header BEFORE the drawEcadEditor call:
//   - "ECAD" title on the left
//   - Schematic | PCB toggle chips (switch s.workspace between Schematic and Board)
//   - ERC / DRC run buttons (dispatch "ecad_run_erc" / "ecad_run_drc")
//   - Export Gerbers / BOM shortcuts (dispatch "ecad_export_gerber" / "ecad_export_bom")
// The header height should be small (~28dp) so the canvas retains most vertical space.
#include "ui/workspace_ecad.h"
#include "ui/ecad/ecad_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"

namespace ui {

void drawEcadWorkspace(app::AppState& s) {
    // TODO(phase-2 ecad): add workspace header bar here — "ECAD" title strip,
    // Schematic/PCB sub-workspace toggle, ERC/DRC buttons, export shortcuts.
    // See ecad/ecad_panel.h drawEcadEditor for the canvas entry point.

    // Delegate to the existing ECAD editor (live backend — do not replace).
    ui::drawEcadEditor(s);
}

} // namespace ui
