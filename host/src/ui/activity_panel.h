// Chudware Agent Activity panel (C22b).
//
// Displays a tree of run -> stage -> attempt built from streamed orchestrator events
// (run_started / stage_started / agent_call / agent_output / stage_result / run_finished)
// and from events.jsonl replay (file-browser combo of %APPDATA%/Chudware/cache/runs/*).
#pragma once
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// ActivityPanel: dockable "Agent Activity" window.
// Must be owned by run_gui alongside all other panels.
class ActivityPanel {
public:
    // Feed one live orchestrator event (called from the event-drain loop on the GL thread).
    // kind is the event kind (run_started / stage_started / agent_call / agent_output /
    // stage_result / run_finished); data is the event's "data" object.
    void onEvent(const std::string& kind, const nlohmann::json& data);

    // Clear the live model (called at run start so the old tree does not linger).
    void clearLive();

    // Draw the dockable window.  Call every frame.
    void draw(AppState& st);

    // Draw just the panel body (no Begin/End) — for hosting inside the consolidated AI panel tab.
    void drawBody(AppState& st);

private:
    // ---- data model ----
    struct AttemptNode {
        std::string role;
        std::string promptFile;
        int contextChars = 0;
        int contextTokensEst = 0;
        std::string sentPrompt;  // exact system+user prompt sent to the model (agent_output.sent)
        std::string reply;       // raw model reply text (agent_output.raw)
        int durationMs = 0;
        std::string verdict;   // "pass" | "retry" | "fail" | "skip" | ""
        std::string reason;
        std::string rawOutput;  // capped at 16 KB from the event; full text on disk
        bool parsedOk = true;
        std::string parseError;
        int opsCount = 0;
        int tokensIn = 0, tokensOut = 0;
        bool showRaw = false;   // collapsible raw output
        // prompt-file click opens the editor (in settings_modal via promptEditorRole)
    };
    struct StageNode {
        std::string name;     // stage name (intake / plan / model / review / fix / memory)
        std::string role;
        std::vector<AttemptNode> attempts;
        std::string finalVerdict;
        std::string finalReason;
        bool active = false;  // is this the currently running stage?
    };
    struct RunNode {
        std::string runId;
        std::string request;
        std::string docId;
        std::string status;       // "running" | "done" | "failed" | "cancelled"
        std::string stopReason;
        int revision = 0;
        std::vector<StageNode> stages;
        // replay-only runs also populate stages/attempts from jsonl
    };

    RunNode liveRun_;
    bool    hasLiveRun_ = false;
    // Index of the currently active stage (live only)
    int     activeStageIdx_ = -1;

    // ---- jsonl replay ----
    struct ReplayEntry {
        std::string runId;
        std::string label;  // e.g. "run_abc123 · 2024-06-10 14:02"
        std::string jsonlPath;
    };
    std::vector<ReplayEntry> replayList_;
    int  selectedReplay_ = -1;
    bool replayLoaded_   = false;
    RunNode replayRun_;
    // mtime cache so we don't re-list every frame
    long long lastListMs_ = 0;

    // ---- animation ----
    float pulseT_ = 0.0f;

    // ---- helpers ----
    void refreshReplayList();
    void loadReplay(const std::string& path);
    RunNode parseEvents(const std::vector<nlohmann::json>& events);
    void drawRunTree(AppState& st, RunNode& run, bool live);
    void drawStage(AppState& st, StageNode& stage, bool isActive, bool live);
    void drawAttempt(AppState& st, AttemptNode& attempt, int idx);
};

}  // namespace ui
