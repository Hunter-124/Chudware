// Lean application state for the new shell. Routes doc ops through services/worker
// (WorkerClient + DocMirror + libaicad with Python-oracle fallback).
#pragma once
#include <string>
#include <vector>
#include "ui/command_registry.h"
#include "view_model.h"
#include "action_bus.h"
#include "app/doc_engine.h"

#include "worker_client.h"
#include "doc_mirror.h"
#include "async_rpc.h"
#include "selection.h"
#include "sim_client.h"
#include "worker_types.h"
#include "doc_tabs.h"
#include "worker_command_dispatcher.h"

struct GLFWwindow;
namespace viewport { class Scene; }

namespace app {

enum class BlockKind { UserMsg, Thinking, Action, AssistantMsg, Plan, Usage, Error };
enum class ActionStatus { Running, Ok, Failed };

struct AgentBlock {
    BlockKind    kind;
    std::string  title;
    std::string  body;
    std::string  args;
    std::string  result;
    ActionStatus status = ActionStatus::Ok;
    bool         expanded = false;
    float        anim = 0.f;
    float        spin = 0.f;
    std::vector<std::string> plan;
    std::vector<bool>        planDone;
};

struct AgentThread {
    std::vector<AgentBlock> blocks;
    bool        running = false;
    std::string composer;
    int         model = 0;
    bool        showThinking = true;
    int   demoStep = -1;
    float demoTimer = 0.f;
    bool  demoOpOk = false;
    std::string demoOpMsg;
};

struct TreeNode {
    std::string name;
    std::string icon;
    bool        isFolder = false;
    bool        open = true;
    std::vector<TreeNode> children;
};

struct DocTabState {
    std::string   name;
    cmd::DocType  type = cmd::DocType::Part;
};

struct AppState {
    GLFWwindow* window = nullptr;

    // ---- worker seam (libaicad + Python fallback) ----
    WorkerClient worker;
    core::DocMirror docMirror;
    core::AsyncRpc rpc{worker};
    core::SimClient simClient;
    JobState job;
    core::Selection selection;
    aicad::services::WorkerCommandDispatcher commandDispatcher{worker, docMirror};
    std::string workerErr;
    bool workerStarted = false;

    // eng::Engine facade (panels use s.engine.* — backed by worker + DocMirror)
    eng::Engine engine{worker, docMirror};
    bool engineReady = false;

    viewport::Scene* scene = nullptr;
    int   viewMeshRev = -1;
    int   viewStyle = 1;

    bool  dark = true;
    bool  maximized = false;

    // legacy doc strip (UI panels) + worker doc tabs (multi-doc sessions)
    std::vector<DocTabState> docs;
    std::vector<core::DocTab> docTabs;
    int   activeDoc = 0;
    int   activeTab = -1;
    bool  suppressTabDirty = false;

    cmd::Workspace workspace = cmd::Workspace::Model;
    bool  inSketch = false;
    bool  sketchPlanePickActive = false;
    bool  camActive = false;   // H1 CAM content-swap: true while the CAM panel should be forward

    std::string activeTool;
    std::string toolExtrudeOp;

    bool     apNeedsInit = false;
    uint32_t apBody = 0;
    float    apColor[3] = {0.71f, 0.76f, 0.82f};
    float    apMetal = 0.f;
    float    apRough = 0.5f;

    int   railTab = 0;
    std::string selectedFeature;
    TreeNode tree;
    std::vector<std::string> bodies;

    std::string savePath;

    std::string status = "Ready";
    std::string units = "mm";
    int   selCount = 0;
    float rebuildPct = -1.f;

    bool  paletteOpen = false;
    char  paletteQuery[128] = {0};
    int   paletteSel = 0;

    AgentThread agent;
    bool agentIsReal = false;

    aicad::ui::DocViewModel viewModel;
    aicad::ui::ActionBus actionBus;

    // Interaction contract §3/§8: in-app-dialog transport pending state (ui/ask_dialog.h).
    std::string pendingAskBatchJson;  // set to raise an ask_user batch (empty = none pending)
    std::string askAnswersJson;       // set by ui::drawAskDialog on Submit (§4 answers envelope)
    bool askDialogOpen = false;

    DocTabState& doc() {
        static DocTabState fallback{"Part", cmd::DocType::Part};
        if (docs.empty()) return fallback;
        return docs[activeDoc < (int)docs.size() ? activeDoc : 0];
    }
    cmd::DocType docType() { return docs.empty() ? cmd::DocType::Part : doc().type; }
    cmd::Workspace effectiveWorkspace() { return inSketch ? cmd::Workspace::Sketch : workspace; }

    void dispatch(const char* cmdId);
    void seedDemo();
    bool tryStartWorker();
    void syncViewModel();
    void syncDocsFromTabs();
    void onDocMutation();
    void addDocTab(bool isAssembly);
    void switchToTab(int idx);
    void doNewPart();
    void doSave(bool saveAs);
    void doOpen();
    void buildTreeFromDoc();
    bool commitTool(const std::string& tool, double v1, double v2);
    void startDemoAgent(const std::string& userText);
    void tickDemoAgent(float dt);
};

} // namespace app
