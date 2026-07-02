// Agent Activity panel — run → stage → attempt tree from orchestrator events.
#pragma once
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace app { struct AppState; }

namespace theme { struct AdaptiveSurface; }

namespace ui {

class ActivityPanel {
public:
    void onEvent(const std::string& kind, const nlohmann::json& data);
    void clearLive();
    void draw(app::AppState& st);
    // surf != nullptr => embedded in an over-viewport pane (Copilot Activity tab):
    // non-semantic label text adapts to the pane's glass surface.
    void drawBody(app::AppState& st, const theme::AdaptiveSurface* surf = nullptr);

private:
    struct AttemptNode {
        std::string role;
        std::string promptFile;
        int contextChars = 0;
        int contextTokensEst = 0;
        int durationMs = 0;
        std::string verdict;
        std::string reason;
        std::string rawOutput;
        bool parsedOk = true;
        std::string parseError;
        int opsCount = 0;
        int tokensIn = 0, tokensOut = 0;
        bool showRaw = false;
    };
    struct StageNode {
        std::string name;
        std::string role;
        std::vector<AttemptNode> attempts;
        std::string finalVerdict;
        std::string finalReason;
        bool active = false;
    };
    struct RunNode {
        std::string runId;
        std::string request;
        std::string docId;
        std::string status;
        std::string stopReason;
        int revision = 0;
        std::vector<StageNode> stages;
    };

    RunNode liveRun_;
    bool    hasLiveRun_ = false;
    int     activeStageIdx_ = -1;

    struct ReplayEntry {
        std::string runId;
        std::string label;
        std::string jsonlPath;
    };
    std::vector<ReplayEntry> replayList_;
    int  selectedReplay_ = -1;
    bool replayLoaded_   = false;
    RunNode replayRun_;
    long long lastListMs_ = 0;
    float pulseT_ = 0.f;

    void refreshReplayList();
    void loadReplay(const std::string& path);
    RunNode parseEvents(const std::vector<nlohmann::json>& events);
    void drawRunTree(app::AppState& st, RunNode& run, bool live);
    void drawStage(app::AppState& st, StageNode& stage, bool isActive, bool live);
    void drawAttempt(app::AppState& st, AttemptNode& attempt, int idx);
};

ActivityPanel& activityPanel();

}  // namespace ui
