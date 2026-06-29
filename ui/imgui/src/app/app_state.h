// Lean application state for the new shell. Owns UI state + (later) the reused engine
// bridge. NOT the old 1237-line AppState — only what the new IA needs.
#pragma once
#include <string>
#include <vector>
#include "ui/command_registry.h"
#include "engine/engine.h"
#include "view_model.h"
#include "action_bus.h"

struct GLFWwindow;
namespace viewport { class Scene; }

namespace app {

// ---- Agent transparency model (DESIGN.md §5): the agent run renders as a timeline of typed blocks.
enum class BlockKind { UserMsg, Thinking, Action, AssistantMsg, Plan, Usage, Error };
enum class ActionStatus { Running, Ok, Failed };

struct AgentBlock {
    BlockKind    kind;
    std::string  title;      // header line (tool label, "Thinking", etc.)
    std::string  body;       // markdown / prose / reasoning
    std::string  args;       // Action: raw op args (JSON), shown when expanded
    std::string  result;     // Action: result summary / error
    ActionStatus status = ActionStatus::Ok;
    bool         expanded = false;
    float        anim = 0.f;            // entrance animation 0..1
    float        spin = 0.f;            // running spinner phase
    std::vector<std::string> plan;      // Plan: checklist items
    std::vector<bool>        planDone;  // Plan: tick state
};

struct AgentThread {
    std::vector<AgentBlock> blocks;
    bool        running = false;
    std::string composer;
    int         model = 0;              // 0 Claude, 1 Gemini, 2 Local
    bool        showThinking = true;
    // demo scripted-response driver (until wired to agent_run_v2 / events.jsonl).
    // The demo applies a REAL engine op so you watch the agent drive the kernel transparently.
    int   demoStep = -1;
    float demoTimer = 0.f;
    bool  demoOpOk = false;
    std::string demoOpMsg;
};

// ---- A lightweight feature-tree node (mirrors DocMirror::tree() later; demo now). ----
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

    // engine (real libaicad C++ kernel; falls back to demo data if unavailable)
    eng::Engine engine;
    bool  engineReady = false;

    // 3D viewport (owned by Application; null until GL is up)
    viewport::Scene* scene = nullptr;
    int   viewMeshRev = -1;             // engine revision currently uploaded to the scene
    int   viewStyle = 1;               // 0 shaded, 1 shaded+edges, 2 wireframe

    // theme / window
    bool  dark = true;
    bool  maximized = false;

    // documents
    std::vector<DocTabState> docs;
    int   activeDoc = 0;

    // workspace / mode
    cmd::Workspace workspace = cmd::Workspace::Model;
    bool  inSketch = false;             // sketch mode swaps the whole context toolbar

    // command UI
    std::string activeTool;             // open inspector tool (cmd id), empty = none
    std::string toolExtrudeOp;          // demo: op variant for the unified Extrude tool

    // appearance editor (Appearance group): per-body color + RealView finish.
    bool     apNeedsInit = false;          // pull the target body's appearance into the form on open
    uint32_t apBody = 0;                    // target body ordinal (engine "b<n>"); 0 = all bodies
    float    apColor[3] = {0.71f, 0.76f, 0.82f};
    float    apMetal = 0.f;
    float    apRough = 0.5f;

    // left dock
    int   railTab = 0;                  // 0 Tree, 1 Bodies, 2 Files, 3 Configs
    std::string selectedFeature;
    TreeNode tree;                      // demo model tree
    std::vector<std::string> bodies;

    // document file lifecycle
    std::string savePath;              // last saved/opened .step+ path ("" = untitled)

    // status
    std::string status = "Ready";
    std::string units = "mm";
    int   selCount = 0;
    float rebuildPct = -1.f;            // <0 = idle

    // command palette
    bool  paletteOpen = false;
    char  paletteQuery[128] = {0};
    int   paletteSel = 0;

    // agent
    AgentThread agent;
    bool agentIsReal = false;  // true when the current run uses the real engine (not the scripted demo)

    // framework-agnostic presentation model + action routing (ui/abstract seam)
    aicad::ui::DocViewModel viewModel;
    aicad::ui::ActionBus actionBus;

    // ---- helpers ----
    DocTabState& doc() {
        static DocTabState fallback{"Part", cmd::DocType::Part};
        if (docs.empty()) return fallback;
        return docs[activeDoc < (int)docs.size() ? activeDoc : 0];
    }
    cmd::DocType docType() { return docs.empty() ? cmd::DocType::Part : doc().type; }

    // effective workspace shown by the toolbar (sketch mode overrides).
    cmd::Workspace effectiveWorkspace() {
        return inSketch ? cmd::Workspace::Sketch : workspace;
    }

    void dispatch(const char* cmdId);   // central command handler (app_state.cpp)
    void seedDemo();                    // populate docs/tree/agent (real engine or demo fallback)
    void doNewPart();                   // File > New Part
    void doSave(bool saveAs);           // File > Save / Save As (native dialog)
    void doOpen();                      // File > Open (native dialog)
    void buildTreeFromDoc();            // rebuild the UI tree from engine.tree()
    bool commitTool(const std::string& tool, double v1, double v2);  // apply a real op (Inspector OK)
    void startDemoAgent(const std::string& userText);
    void tickDemoAgent(float dt);
};

} // namespace app
