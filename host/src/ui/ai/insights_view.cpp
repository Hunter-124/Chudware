// Composed "Insights" view (AI workspace utility pane): the aicad.leaderboard/1 board followed
// by the token-spend aggregation on one scrolling surface, split by one hairline + header
// boundary. The leaderboard cache moved here from ai_workspace.cpp's view switch (same
// load-once + on-demand-refresh shape). The board titles itself ("Model Leaderboard"), so only
// the lower section gets a section header — no doubled titles.
#include "insights_view.h"

#include "imgui.h"

#include "ui/widgets/kit.h"

#include "ui/leaderboard_ui.h"
#include "metrics_view.h"

namespace ui {

void drawInsightsView(AppState& st) {
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
