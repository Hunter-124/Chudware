// Manufacture workspace body — workspace header strip + CAM manager panel.
//
// Wiring: viewport.cpp calls drawManufactureWorkspace() when workspace == Manufacture
// (or camActive is true). The 3D scene still renders beneath (OverlayHosting kind).
//
// Layout:
//   1. Workspace header strip: breadcrumb + Generate/Simulate/Post shortcuts +
//      stale-part banner (cross-doc auto-update, feat(xdoc)).
//   2. drawCamManager(s): the full CAM manager with job tree, Op Editor, Toolpaths,
//      Simulate, Setup Sheet, G-code tabs.
//
// Cross-doc pull: camCheckPartStale() is called each frame so the stale banner in
// the header strip lights up immediately when the source part rebuilds.
#include "ui/workspace_manufacture.h"
#include "ui/cam/cam_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"

namespace ui {

void drawManufactureWorkspace(app::AppState& s) {
    // 1. Workspace header strip: breadcrumb + action shortcuts + stale banner.
    //    camCheckPartStale() is the pull-model cross-doc staleness check (feat(xdoc)):
    //    it polls doc_list_open ~once/second and sets a stale flag when the source part
    //    has been rebuilt since the last cam_extract. The banner in the header strip
    //    responds immediately; clicking "Regen from Part" calls camRegenFromPart().
    camCheckPartStale(s);
    drawCamWorkspaceHeader(s);

    ImGui::Spacing();

    // 2. Full CAM manager body: job tree (left) + tabbed right pane (Toolpaths /
    //    Op Editor / Simulate / Setup Sheet / G-code). Live backend — do not replace.
    ui::drawCamManager(s);
}

} // namespace ui
