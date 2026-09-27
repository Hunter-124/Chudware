// Composed Insights view: global Model leaderboard (trailing note "All projects") and
// per-project token spend, with an opt-in "Include all projects" toggle on the latter.
#include "insights_view.h"

#include "imgui.h"

#include "ui/widgets/kit.h"

#include "ui/leaderboard_ui.h"
#include "metrics_view.h"

namespace ui {

void drawInsightsView(AppState& st) {
    ui::kit::sectionHeader("Model leaderboard", nullptr, "All projects",
                           /*count=*/-1, /*first=*/true);
    static ui::leaderboard::Board s_board;
    static bool s_loaded = false;
    if (!s_loaded) {
        s_board = ui::leaderboard::load();
        s_loaded = true;
    }
    bool wantRefresh = false;
    ui::leaderboard::drawFullPane(s_board, wantRefresh);
    if (wantRefresh) {
        s_board = ui::leaderboard::load();
        s_loaded = true;
    }

    // ---- boundary between the two sections ---------------------------------------------
    ui::kit::gapSection();
    ui::kit::rule();
    ui::kit::sectionHeader("Token spend");

    drawMetricsView(st);
}

}  // namespace ui
