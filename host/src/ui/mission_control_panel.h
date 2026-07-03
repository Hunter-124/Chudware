// Mission-control panel (T-F17): live plan/action stream for DAG runs.
// Parses dag.* events from events.jsonl and renders a run tree + event trail.
// Steering controls (pause/approve/reject/inject/autonomy-dial) are shown when
// a run is active; design-brief approval uses the ask_dialog flow.
//
// Shot-harness scenario: AI_CAD_SHOT=mission_control
//   Loads a pre-written capstone events.jsonl, replays it into the view, screenshots.
#pragma once

#include <string>
#include <vector>

#include "ui/abstract/mission_control_view.h"
#include <nlohmann/json.hpp>

// Forward declarations — use global-namespace AppState for host/src compatibility.
struct AppState;
namespace theme { struct AdaptiveSurface; }

namespace ui {

class MissionControlPanel {
public:
    // Feed one live dag.* event (called from the agent-run dispatch path).
    void onEvent(const std::string& kind, const nlohmann::json& data);

    // Force-load a specific events.jsonl for testing/replay.
    bool loadReplay(const std::string& jsonlPath);

    void clearLive();
    void draw(AppState& st);
    void drawBody(AppState& st, const theme::AdaptiveSurface* surf = nullptr);

    // Access the loaded view for screenshots/tests.
    const aicad::ui::DagRunView& view() const { return view_; }

private:
    aicad::ui::DagRunView view_;

    // Replay list (scanned from cache/runs/)
    std::vector<aicad::ui::ReplayEntry> replayList_;
    int  selectedReplay_ = -1;
    bool replayLoaded_   = false;
    long long lastListMs_ = 0;

    // Panel UI state
    bool showThoughts_ = true;   // whether agent.thought entries are visible

    // Helpers
    void refreshReplayList();
    void parseJsonl(const std::vector<nlohmann::json>& events);
    void applyEvent(const nlohmann::json& ev);

    aicad::ui::DagNodeStatus statusFromString(const std::string& s);
    aicad::ui::EventEntryKind kindFromString(const std::string& k);
    std::string headlineFor(const std::string& kind, const nlohmann::json& ev);

    void drawRunTree();
    void drawEventStream();
    void drawSteering(AppState& st);
    void drawReplaySelector();
};

MissionControlPanel& missionControlPanel();

}  // namespace ui
