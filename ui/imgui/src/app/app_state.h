// Lean application state for the new shell. Routes doc ops through services/worker
// (WorkerClient + DocMirror + libaicad with Python-oracle fallback).
#pragma once
#include <string>
#include <vector>
#include "ui/command_registry.h"
#include "view_model.h"
#include "action_bus.h"
#include "app/doc_engine.h"
#include "ask_form.h"   // AskForm: interaction contract §3 batch model

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

enum class BlockKind { UserMsg, Thinking, Action, AssistantMsg, Plan, Usage, Error, Question };
enum class ActionStatus { Running, Ok, Failed };

// Per-field inline widget state for a Question block.
// Allocated once when the block is created; updated each frame by drawQuestionCard().
struct InlineFieldState {
    double              dimValue   = 0.0;
    int                 dimUnitIdx = 0;
    int                 choiceIdx  = 0;          // single-select
    std::vector<char>   choiceChecked;           // multi-select (parallel to AskForm options); char not bool to avoid vector<bool> proxy
    bool                approved   = false;
    std::string         textBuf;                 // text / file-request (heap, no fixed cap)
    bool                seeded     = false;      // true once initInlineField() has run
};

struct AgentBlock {
    BlockKind    kind;
    std::string  title;
    std::string  body;
    std::string  args;
    std::string  result;
    std::string  modelName;  // attribution: model that produced this block (empty = unknown)
    ActionStatus status = ActionStatus::Ok;
    bool         expanded = false;
    float        anim = 0.f;
    float        spin = 0.f;
    std::vector<std::string> plan;
    std::vector<bool>        planDone;
    // Question block state (kind == BlockKind::Question only).
    aicad::ui::AskForm              askForm;
    std::vector<InlineFieldState>   askFields;   // parallel to askForm.fields
    bool                            askAnswered = false;  // true once Submit pressed
    std::string                     askAnswersJson;       // §4 answer envelope when answered
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
    // Real token counts from the last completed agent turn (0 = no data yet).
    int lastInputTokens  = 0;
    int lastOutputTokens = 0;

    // T-DP18: live mission ETA from the backend estimator.
    // etaLabel is pre-formatted for the status-bar chip (e.g. "ETA ~2m 30s").
    // etaSeconds < 0 means no estimate available (no history / all unknown).
    // etaLowS / etaHighS bound the uncertainty range; both < 0 when unknown.
    // Written by the agent service loop; read by the render thread (status_bar.cpp).
    std::string etaLabel;        // "ETA ~Xs" / "ETA Xs – Ys" / "ETA unknown" / ""
    float       etaSeconds = -1.f;
    float       etaLowS    = -1.f;
    float       etaHighS   = -1.f;
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

    // T-BL47: pending sketch ribbon commands — drained each frame in viewport.cpp
    // where the SketchHost + s_sketch static exist. Push here from dispatch(), drain there.
    std::vector<std::string> pendingSketchCmds;

    // T-BL48: edit-an-existing-sketch entry. The ribbon command queue above carries only
    // command-id strings (sketch_new has no target); editing a *specific* sketch row from the
    // Feature Manager needs the target feature id. The feature tree pushes the fid here (double
    // click / "Edit Sketch" ctx-menu); the viewport drain calls s_sketch.enterForFeature(fid).
    // "Edit Sketch Plane/Face" re-parents an existing sketch, so it routes through the reparent
    // field which arms s_sketch.beginPlaneEdit(fid). This is the one shared sketch-mode state
    // that feature tree + ribbon + viewport all reach — it supersedes feature_tree's old
    // `void* sketchMode=nullptr` placeholder (removed: the queue push IS the state handoff).
    std::string pendingSketchEditFid;      // enter the sketcher on this existing sketch feature
    std::string pendingSketchReparentFid;  // arm plane-pick to move this sketch onto a new plane

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
    std::string agentLastRequest_;  // original user request for agent replay on answer

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
