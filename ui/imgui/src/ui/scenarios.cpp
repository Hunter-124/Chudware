// Scripted screenshot/test scenarios — AI-CAD2 port of host/src/ui/scenarios.cpp.
#include "ui/scenarios.h"

#include "ui/viewport.h"
#include "ui/sketch/sketch_mode.h"   // T-BL46: full type for sketchMode().elementDragActive()
#include "ui/sketch3d/sketch3d.h"    // 3D free-space sketcher shot seed (ws_sketch3d)
#include "viewport_impl.h"            // T-BL46: GlViewport full type (renderer().camera())
#include "renderer.h"                 // T-BL46: worldToScreen(), ViewportScreen

#include "ui/feature_tree.h"        // T-BL37: featureTreeUnhideOrigin/featureTreeSelectOrigin
#include "ui/ribbon.h"
#include "ui/settings_modal.h"
#include "ui/file_explorer.h"
#include "ui/preview_pane.h"
#include "ui/scaffold_demo_panel.h"
#include "ui/parts_panel.h"
#include "ui/cam_panel.h"            // T-U3 CAM workspace (AI_CAD_SHOT=cam_workspace)
#include "ui/cam/cam_panel.h"        // camLoadJob (Manufacture panel job adoption, AI_CAD_SHOT=cam)
#include "ui/drawing_panel.h"        // drawingActiveDocId/Doc + refresh (AI_CAD_SHOT=drawing detailing)
#include "ui/ecad/ecad_panel.h"      // ecadEditorMirror (AI_CAD_SHOT=ecad ai-design adoption)
#include "ui/ecad/ecad_mirror.h"     // EcadMirror::loadFromTree (complete type for the call above)
#include "ui/sim_manager.h"          // simManager() study flow (AI_CAD_SHOT=fea host parity)
#include "ui/bom_panel.h"
#include "ui/requirements_panel.h"   // T-U5
#include "ui/transparency_panel.h"
#include "ui/piping_panel.h"       // T-U7 piping viewer
#include "ui/hydraulic_panel.h"    // T-U7 hydraulic viewer
#include "ui/structural_panel.h"   // T-U7 structural viewer   // T-U8
#include "ui/gallery_panel.h"        // T-U9
#include "ui/mission_control_panel.h"  // T-F17
#include "ui/agent_inspector_panel.h"  // T-W17
#include "ui/watch_grid_panel.h"       // T-W17
#include "ui/agent_settings_panel.h"   // T-W17
#include "ui/ai_effort_section.h"      // T-W28
#include "ui/agent_dag_panel.h"        // T-DP17
#include "ui/sim_panel.h"                       // T-U6: sim studies (AI_CAD_SHOT=sim_studies)
#include "artifact_view.h"
#include "app/app_state.h"
#include "viewport/scene.h"
#include "jobs.h"

#include <platform/paths.h>

#include <glad/gl.h>
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "stb_image_write.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <vector>

#include "cpp_core.h"

namespace ui {
namespace {

ScenarioRunner* g_runner = nullptr;
ShotConfig* g_cfg = nullptr;

static std::string repoBase() {
    const char* repo = std::getenv("AI_CAD_REPO");
    return repo ? std::string(repo) : std::string(".");
}

static void loadMesh(app::AppState& st, const std::string& aicm) {
    if (aicm.empty()) return;
    if (st.scene) st.scene->loadAicm(aicm);
    st.viewMeshRev = st.engine.revision();
    st.buildTreeFromDoc();
}

static void writeBackbufferPng(const char* path, int dw, int dh, const char* mirrorPath = nullptr) {
    std::vector<unsigned char> px((size_t)dw * dh * 4);
    glReadBuffer(GL_BACK);
    glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    std::vector<unsigned char> flip((size_t)dw * dh * 4);
    for (int y = 0; y < dh; ++y)
        std::memcpy(&flip[(size_t)(dh - 1 - y) * dw * 4], &px[(size_t)y * dw * 4], (size_t)dw * 4);
    if (path) stbi_write_png(path, dw, dh, 4, flip.data(), dw * 4);
    if (mirrorPath) stbi_write_png(mirrorPath, dw, dh, 4, flip.data(), dw * 4);
}

static nlohmann::json dim(double v) { return nlohmann::json{{"value", v}, {"expr", nullptr}}; }

static void resetDemoState(app::AppState& st) {
    st.docs.clear();
    st.docTabs.clear();
    st.agent.blocks.clear();
    st.agent.running = false;
    st.inSketch = false;
    st.activeTool.clear();
    st.railTab = 0;
    st.workspace = cmd::Workspace::Model;
    st.tree = app::TreeNode{};
    st.bodies.clear();
    st.selectedFeature.clear();
    st.paletteOpen = false;
    settingsUi().openSettings = false;
    settingsUi().wantSettings = false;
}

static void seedChatShot(app::AppState& st) {
    using app::AgentBlock;
    using app::BlockKind;
    // T-BL39: seed modelName on assistant blocks so the chat shot shows real attribution.
    // Two turns with the same model (as the real single-provider path) — attribution must
    // appear on each bubble and must never read "Assistant".
    AgentBlock u; u.kind = BlockKind::UserMsg;
    u.body = "make it 30mm tall with a chamfered top edge";
    st.agent.blocks.push_back(u);
    AgentBlock t; t.kind = BlockKind::Thinking; t.title = "Thinking";
    t.body = "Edit the open hex standoff: 30mm tall + chamfered top.";
    AgentBlock a; a.kind = BlockKind::AssistantMsg;
    a.modelName = "Anthropic \xc2\xb7 claude-sonnet-4-6";  // mirrors ai_artifact model_info format
    a.body = "Done — height 30mm, 1mm chamfer on the top rim.";
    st.agent.blocks.insert(st.agent.blocks.end(), {t, a});
    // Second turn to show attribution is per-message (multi-turn conversation).
    AgentBlock u2; u2.kind = BlockKind::UserMsg;
    u2.body = "add 4 M3 mounting holes on the base";
    AgentBlock a2; a2.kind = BlockKind::AssistantMsg;
    a2.modelName = "Anthropic \xc2\xb7 claude-sonnet-4-6";
    a2.body = "Added a 4-hole M3 bolt circle on the bottom face.";
    a2.anim = 1.f;
    st.agent.blocks.insert(st.agent.blocks.end(), {u2, a2});
}

static void seedPlanShot(app::AppState& st) {
    using app::AgentBlock;
    using app::BlockKind;
    AgentBlock u; u.kind = BlockKind::UserMsg; u.body = "an M3 hex standoff, 30mm tall";
    st.agent.blocks.push_back(u);
    AgentBlock a; a.kind = BlockKind::Plan;
    a.title = "hex standoff";
    a.body = "extruded hex prism with a through bore, chamfered top rim";
    a.plan = {"M3 clearance bore r=1.7 through", "1mm chamfer on the top rim"};
    a.planDone = {false, false};
    st.agent.blocks.push_back(a);
}

static void seedAssemblyShot(app::AppState& st) {
    using app::AgentBlock;
    using app::BlockKind;
    AgentBlock u; u.kind = BlockKind::UserMsg;
    u.body = "a base plate, a lid 10mm above it, and a peg through both";
    st.agent.blocks.push_back(u);
    AgentBlock a; a.kind = BlockKind::AssistantMsg;
    a.body = "Built a 3-part assembly. The peg passes through both plates.";
    st.agent.blocks.push_back(a);
    st.workspace = cmd::Workspace::Assemble;
}

// T-BL40: seeds three Question blocks (one per required widget modality) so the headless
// screenshot harness captures each variant without a live agent or worker round-trip.
// The batch JSON follows the interaction contract §3 envelope exactly.
static void seedQuestionShot(app::AppState& st) {
    using app::AgentBlock;
    using app::BlockKind;
    using aicad::ui::parseAskBatch;

    // Context: user asked to design something; agent needs clarification.
    AgentBlock u; u.kind = BlockKind::UserMsg;
    u.body = "Design me a custom bracket for the motor mount.";
    u.anim = 1.f;
    st.agent.blocks.push_back(u);

    // Block 1: short answer (single-line text) + dimension — pushed first so it scrolls above.
    {
        const char* batchJson = R"({
            "schema": "aicad.interaction/1",
            "batch_id": "b_shot_text",
            "scope": "run",
            "run_id": "r_shot",
            "blocking": true,
            "questions": [
                {
                    "id": "label",
                    "kind": "text",
                    "prompt": "Short label to engrave on the bracket (e.g. part number)?",
                    "suggested": "BKT-001",
                    "required": false
                },
                {
                    "id": "flange_width",
                    "kind": "dimension",
                    "family": "length",
                    "prompt": "Flange width?",
                    "suggested": {"value": 25, "unit": "mm"},
                    "required": true
                }
            ]
        })";
        AgentBlock qb; qb.kind = BlockKind::Question; qb.title = "Agent question";
        qb.anim = 1.f;
        qb.askForm = parseAskBatch(batchJson);
        qb.askFields.resize(qb.askForm.fields.size());
        st.agent.blocks.push_back(std::move(qb));
    }

    // Block 2: open-ended (multiline text) + approval.
    {
        const char* batchJson = R"({
            "schema": "aicad.interaction/1",
            "batch_id": "b_shot_openended",
            "scope": "run",
            "run_id": "r_shot",
            "blocking": true,
            "questions": [
                {
                    "id": "special_notes",
                    "kind": "text",
                    "multiline": true,
                    "prompt": "Any special requirements or notes for this bracket?",
                    "suggested": "",
                    "required": false
                },
                {
                    "id": "proceed",
                    "kind": "approval",
                    "prompt": "Proceed with building the bracket using your answers above?",
                    "proposal": "Build bracket with selected options and dimensions",
                    "suggested": true,
                    "required": true
                }
            ]
        })";
        AgentBlock qb; qb.kind = BlockKind::Question; qb.title = "Agent question";
        qb.anim = 1.f;
        qb.askForm = parseAskBatch(batchJson);
        qb.askFields.resize(qb.askForm.fields.size());
        st.agent.blocks.push_back(std::move(qb));
    }

    // Block 3 (last = visible at bottom): multiple choice (single-select + multi-select).
    // The agent panel scrolls to bottom, so this block is guaranteed in-frame.
    {
        const char* batchJson = R"({
            "schema": "aicad.interaction/1",
            "batch_id": "b_shot_choice",
            "scope": "run",
            "run_id": "r_shot",
            "blocking": true,
            "questions": [
                {
                    "id": "material",
                    "kind": "choice",
                    "prompt": "What material should the bracket be made from?",
                    "why": "Drives wall thickness and surface finish options.",
                    "suggested": "aluminium_6061",
                    "required": true,
                    "options": [
                        {"value": "aluminium_6061", "label": "Aluminium 6061"},
                        {"value": "steel_mild",    "label": "Mild steel"},
                        {"value": "carbon_fibre",  "label": "Carbon fibre"}
                    ]
                },
                {
                    "id": "features",
                    "kind": "choice",
                    "prompt": "Which optional features do you need?",
                    "suggested": ["drain_slot"],
                    "required": false,
                    "multi": true,
                    "options": [
                        {"value": "drain_slot",  "label": "Drain slot"},
                        {"value": "gussets",     "label": "Gussets"},
                        {"value": "cable_clips", "label": "Cable clips"}
                    ]
                }
            ]
        })";
        AgentBlock qb; qb.kind = BlockKind::Question; qb.title = "Agent question";
        qb.anim = 1.f;
        qb.askForm = parseAskBatch(batchJson);
        qb.askFields.resize(qb.askForm.fields.size());
        st.agent.blocks.push_back(std::move(qb));
    }
}

// Seeds the T-F16 preview pane deterministically (no worker round-trip): a few representative
// artifact cards + a Ready PreviewModel pointing at a PNG that's guaranteed to exist on disk
// (the icon atlas already bundled under resourcesDir/icons/) so the shot renders a real texture.
static void seedPreviewShot(app::AppState& /*st*/) {
    using aicad::ui::ArtifactCard;
    using aicad::ui::PreviewModel;
    using aicad::ui::PreviewPage;
    using aicad::ui::PreviewState;

    std::vector<ArtifactCard> cards;
    ArtifactCard report;
    report.artifactId = "art_report"; report.kind = "report"; report.path = "out/report.docx";
    report.title = "Design Review Report"; report.createdTs = "2026-07-02T00:00:00Z"; report.runId = "run_1";
    cards.push_back(report);

    ArtifactCard deck;
    deck.artifactId = "art_deck"; deck.kind = "presentation"; deck.path = "out/deck.pptx";
    deck.title = "Customer Deck"; deck.createdTs = "2026-07-02T00:05:00Z"; deck.runId = "run_1";
    cards.push_back(deck);

    ArtifactCard bom;
    bom.artifactId = "art_bom"; bom.kind = "workbook"; bom.path = "out/bom.xlsx";
    bom.title = "Bill of Materials"; bom.createdTs = "2026-07-02T00:10:00Z"; bom.runId = "run_1";
    cards.push_back(bom);

    PreviewModel model;
    model.path = report.path;
    model.kind = "docx";
    model.state = PreviewState::Ready;
    model.resourceLink = "file://" + platform::joinPath(platform::resourcesDir(), "icons/atlas_dark.png");
    PreviewPage page;
    page.index = 1;
    page.imagePath = platform::joinPath(platform::resourcesDir(), "icons/atlas_dark.png");
    model.pages = {page};
    model.activePage = 0;

    ui::previewPaneSeedForShot(cards, model);
}

static bool findBoxAicm(std::string& out) {
    if (const char* env = std::getenv("AI_CAD_AICM")) { out = env; return !out.empty(); }
    const char* candidates[] = {
        "docs/contracts/fixtures/box.aicm",
        "../docs/contracts/fixtures/box.aicm",
        "../../docs/contracts/fixtures/box.aicm",
        "../../../docs/contracts/fixtures/box.aicm",
    };
    for (const char* c : candidates) {
        if (std::filesystem::exists(c)) { out = c; return true; }
    }
    return false;
}

// T-W17: locate the committed fixtures dir (docs/contracts/fixtures) relative to
// the binary's CWD — same multi-candidate walk-up as findBoxAicm.
static bool findFixturesDir(std::string& out) {
    if (const char* env = std::getenv("AI_CAD_FIXTURES")) { out = env; return !out.empty(); }
    const char* candidates[] = {
        "docs/contracts/fixtures",
        "../docs/contracts/fixtures",
        "../../docs/contracts/fixtures",
        "../../../docs/contracts/fixtures",
    };
    for (const char* c : candidates)
        if (std::filesystem::exists(c)) { out = c; return true; }
    return false;
}

static std::string readWholeFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::stringstream ss; ss << f.rdbuf(); return ss.str();
}

// T-DP16: shared populated-DAG fixture (multi-layer mission + 3 delegation
// sub-agents + explicit edges). Reused by both the agent_dag widget shot and the
// AI-Agent workspace shot (ws_ai_agent) so the takeover renders the real DAG
// centerpiece rather than the empty state.
static const std::string& agentDagFixtureJsonl() {
    static const std::string j = [] {
        const char* ts = "2026-07-05T10:22:00Z";
        std::string s;
        auto ev = [&](const char* body) {
            s += std::string("{\"ts\":\"") + ts + "\"," + body + "}\n";
        };
        // Mission run
        ev(R"j("kind":"dag.run.started","run_id":"dp17fix","graph_name":"Bracket: Guided Design Pipeline","graph_id":"bracket-guided-dp")j");
        // Orchestrator node (root)
        ev(R"j("kind":"dag.node.started","run_id":"dp17fix","node_id":"n_orch","node_type":"mission","node_name":"Orchestrator")j");
        ev(R"j("kind":"dag.node.done","run_id":"dp17fix","node_id":"n_orch","output_summary":"Pipeline dispatched — 3 sub-agents active")j");
        // Feature architect sub-agent (delegation)
        ev(R"j("kind":"delegation.spawn","parent_id":"n_orch","child_id":"n_feat","role":"feature_architect","model":"qwen3-4b","tokens_granted":8192)j");
        ev(R"j("kind":"delegation.update","id":"n_feat","status":"running","current_tool":"add_features","tokens_spent":1840,"ledger_slice":"stage=FUNCTIONAL_DECOMP")j");
        ev(R"j("kind":"delegation.update","id":"n_feat","status":"done","tokens_spent":3210)j");
        // Geometry designer sub-agent
        ev(R"j("kind":"delegation.spawn","parent_id":"n_orch","child_id":"n_geom","role":"geometry_designer","model":"qwen3-4b","tokens_granted":8192)j");
        ev(R"j("kind":"delegation.update","id":"n_geom","status":"running","current_tool":"fit_calc","tokens_spent":2048,"ledger_slice":"stage=GEOMETRY_REALIZATION feature=mount_boss")j");
        // Build strategist sub-agent
        ev(R"j("kind":"delegation.spawn","parent_id":"n_orch","child_id":"n_build","role":"build_strategist","model":"qwen3-4b","tokens_granted":6000)j");
        ev(R"j("kind":"delegation.update","id":"n_build","status":"blocked","tokens_spent":512,"ledger_slice":"waiting on geometry_designer")j");
        // Verify + gate nodes
        ev(R"j("kind":"dag.node.started","run_id":"dp17fix","node_id":"n_verify","node_type":"verify","node_name":"Verify: interference")j");
        ev(R"j("kind":"dag.node.failed","run_id":"dp17fix","node_id":"n_verify","error":"geometry_designer still running")j");
        ev(R"j("kind":"dag.node.started","run_id":"dp17fix","node_id":"n_gate","node_type":"gate","node_name":"Approve build plan")j");
        ev(R"j("kind":"dag.node.paused","run_id":"dp17fix","node_id":"n_gate","reason":"gate")j");
        ev(R"j("kind":"dag.node.approval_required","run_id":"dp17fix","node_id":"n_gate")j");
        // Explicit edges to build the graph hierarchy
        ev(R"j("kind":"dag.edge","from":"n_feat","to":"n_geom")j");
        ev(R"j("kind":"dag.edge","from":"n_geom","to":"n_build")j");
        ev(R"j("kind":"dag.edge","from":"n_build","to":"n_verify")j");
        ev(R"j("kind":"dag.edge","from":"n_build","to":"n_gate")j");
        return s;
    }();
    return j;
}

static void setupPickShot(app::AppState& st) {
    std::string aicm;
    if (!findBoxAicm(aicm)) {
        std::fprintf(stderr, "pick: box.aicm not found\n");
        return;
    }
    if (st.scene && st.scene->loadAicm(aicm)) st.viewMeshRev = 0;
    // Also feed the REAL GL viewport pipeline so the mesh (and the liquid-glass backdrop
    // derived from it) actually renders; st.scene is the legacy scaffold.
    if (ui::viewportLoadAicm(aicm))
        std::printf("pick: loaded %s\n", aicm.c_str());
}

// glassperf: CPU frame-time capture. File-static (single active scenario per process) per
// the harness contract; reset on the scenario's first tick (frame 1).
constexpr int kGlassperfRing = 512;
double g_gpPrevTime = 0.0;
int g_gpFrameCount = 0;
double g_gpMeanMs = 0.0;
double g_gpMaxMs = 0.0;
float g_gpRing[kGlassperfRing] = {};
int g_gpRingCount = 0;
bool g_gpProfStarted = false;
bool g_gpProfWarned = false;

float glassperfP99() {
    int n = std::min(g_gpRingCount, kGlassperfRing);
    if (n <= 0) return 0.f;
    std::vector<float> sorted(g_gpRing, g_gpRing + n);
    std::sort(sorted.begin(), sorted.end());
    int idx = static_cast<int>(0.99f * static_cast<float>(n - 1));
    if (idx < 0) idx = 0;
    if (idx >= n) idx = n - 1;
    return sorted[idx];
}

}  // namespace

void setScenarioContext(ScenarioRunner* runner, ShotConfig* cfg) {
    g_runner = runner;
    g_cfg = cfg;
}

bool isDPStreamShot() {
    return g_cfg && (g_cfg->wantDPStream || g_cfg->wantDPStreamEmpty);
}

int ShotConfig::captureFrameBudget() const {
    if (wantFlow && !flowShotDone) return 999999;
    if (wantFea && !feaShotDone) return 999999;
    if (wantFeaOverlay && !feaOverlayShotDone) return 999999;  // T-SIM2: spin until overlay settles
    if (wantCam && !camShotDone) return 999999;
    if (wantIconPipe && !iconPipeShotDone) return 999999;  // T-G25: spin until every recipe drains
    if (wantEcad && !ecadShotDone) return 150;
    if (wantBom && !bomShotDone) return 150;
    if (wantRequirements && !requirementsShotDone) return 150;  // T-U5
    if (wantTransparency && !transparencyShotDone) return 150;  // T-U8
    if ((wantDPStream || wantDPStreamEmpty) && !dpStreamShotDone) return 150;  // T-DP11
    if (wantSimStudies && !simStudiesShotDone) return 150;
    if (wantOrigin) return originShotDone ? 10 : 30;  // T-BL37
    if (wantTree) return treeShotDone ? 10 : 30;
    if (wantEquations) return equationsShotDone ? 10 : 30;
    if (wantPartsBrowser) return partsBrowserShotDone ? 10 : 30;
    if (isScenarioMode()) return 999999;
    if (wantGlassperf) return glassperfShotDone ? 10 : 400;
    if (wantQuestion) return 20;  // T-BL40: let layout stabilize (frame 1 has near-zero logW)
    if (wantAgentMultifile) return 20;  // T-DP13: let activity spinners animate a few frames
    return 6;
}

void configureShot(app::AppState& st, ShotConfig& cfg, const char* shotEnv,
                   const char* capturePath) {
    resetDemoState(st);
    cfg = ShotConfig{};
    cfg.capturePath = capturePath;
    if (!shotEnv || !*shotEnv) {
        cfg.active = capturePath != nullptr;
        return;
    }
    cfg.name = shotEnv;
    cfg.active = true;

    auto eq = [&](const char* s) { return std::strcmp(shotEnv, s) == 0; };

    cfg.wantSettings = eq("settings");
    cfg.wantChat = eq("chat");
    cfg.wantAssembly = eq("assembly");
    cfg.wantPlan = eq("plan");
    cfg.wantPick = eq("pick");
    cfg.wantRibbon = eq("ribbon");
    cfg.wantOrigin = eq("origin");   // T-BL37: origin point glyph visible + selected
    cfg.wantTree = eq("tree");
    cfg.wantExplorer = eq("explorer");
    cfg.wantEquations = eq("equations");
    cfg.wantM1 = eq("m1");
    cfg.wantMultidoc = eq("multidoc");
    cfg.wantIconPipe = eq("iconpipe");  // T-G25: recipe-driven clean-viewport icon render
    cfg.wantEcad = eq("ecad");
    cfg.wantSketch = eq("sketch");
    cfg.wantSketchDispatch = eq("sketch_dispatch");  // T-BL48 proof: dispatch→drain→active_
    cfg.wantSketchAutohide = eq("sketch_autohide");  // T-BL41 proof: annotations hidden unless SmartDim
    cfg.wantSketchConvert = eq("sketch_convert");   // T-BL43 Convert Entities in-sketch shot
    cfg.wantFea = eq("fea");
    cfg.wantFeaOverlay = eq("fea_overlay");   // T-SIM2: precomputed-result contour render
    cfg.wantSimMaterial = eq("sim_material");   // T-SIM3: material-assignment dialog
    cfg.wantSimPrecheck = eq("sim_precheck");   // T-SIM3: advisor precheck advisory card
    cfg.wantSimPostcheck = eq("sim_postcheck"); // T-SIM3: advisor margin verdict on Results
    cfg.wantCam = eq("cam");
    cfg.wantFlow = eq("flow");
    cfg.wantDrawing = eq("drawing");
    cfg.wantEvalMass = eq("evalmass");
    cfg.wantActivity = eq("activity");
    cfg.wantAsm = eq("asm");
    cfg.wantPmform = eq("pmform");
    cfg.wantPreview = eq("preview");
    cfg.wantScaffoldDemo  = eq("scaffold_demo");
    cfg.wantPartsBrowser  = eq("parts_browser");
    cfg.wantBom          = eq("bom");
    cfg.wantRequirements = eq("requirements");   // T-U5
    cfg.wantTransparency  = eq("transparency");   // T-U8
    cfg.wantDPStream      = eq("dp_stream");       // T-DP11: populated action stream
    cfg.wantDPStreamEmpty = eq("dp_stream_empty"); // T-DP11: empty state
    cfg.wantPiping        = eq("piping_view");      // T-U7
    cfg.wantHydraulic    = eq("hydraulic_view");    // T-U7
    cfg.wantStructural   = eq("structural_view");   // T-U7
    cfg.wantGallery       = eq("gallery");
    cfg.wantQuestion      = eq("question");           // T-BL40: inline ask_user cards
    cfg.wantMissionControl   = eq("mission_control");   // T-F17
    cfg.wantAgentDag         = eq("agent_dag");         // T-DP17
    cfg.wantAgentDagCompact  = eq("agent_dag_compact"); // T-DP17
    // T-DP15: workspace-kind shots — overlay presence/absence per workspace kind
    cfg.wantWsOverlayHosting   = eq("ws_overlay_hosting");
    cfg.wantWsFullViewportEcad = eq("ws_full_viewport_ecad");
    cfg.wantWsAiAgent          = eq("ws_ai_agent");
    cfg.wantWsPlm              = eq("ws_plm");
    cfg.wantWsSketch3d         = eq("ws_sketch3d");
    cfg.wantAgentInspector      = eq("agent_inspector");        // T-W17
    cfg.wantAgentInspectorEmpty = eq("agent_inspector_empty");  // T-W17
    cfg.wantAgentInspectorFailure    = eq("agent_inspector_failure");    // T-W17
    cfg.wantAgentInspectorDelegation = eq("agent_inspector_delegation"); // T-W17
    cfg.wantWatchGrid           = eq("watch_grid");             // T-W17
    cfg.wantAgentMultifile      = eq("agent_multifile");        // T-DP13
    cfg.wantAgentSettings       = eq("agent_settings");         // T-W17
    cfg.wantAgentSettingsError  = eq("agent_settings_error");   // T-W17
    cfg.wantSimStudies   = eq("sim_studies");   // T-U6
    // T-G22: per-tab settings shots + composer dropdown + permissions popup
    cfg.wantSettingsGeneral    = eq("settings_general");
    cfg.wantSettingsAI         = eq("settings_ai");
    cfg.wantSettingsPrompts    = eq("settings_prompts");
    cfg.wantSettingsAdvanced   = eq("settings_advanced");
    cfg.wantComposerDropdown   = eq("composer_dropdown");
    cfg.wantPermissionsPopup   = eq("permissions_popup");
    // T-W28: AI Effort section shots (settings modal AI tab, fixture-seeded)
    cfg.wantAiEffortSimple   = eq("ai_effort_simple");
    cfg.wantAiEffortAdvanced = eq("ai_effort_advanced");
    cfg.wantAiEffortWarn     = eq("ai_effort_warn");
    // T-DP12: auto mode — workflow_router tier-defaults readout
    cfg.wantAiEffortAuto     = eq("ai_effort_auto");
    cfg.wantGlassperf = eq("glassperf");
    if (cfg.wantGlassperf) seedChatShot(st);  // bubble rendering included in the measured frame

    if (cfg.wantSettings) {
        settingsUi().wantSettings = true;
        settingsUi().openSettings = true;
    }
    // T-G22: per-tab settings shots — open modal on the appropriate tab
    if (cfg.wantSettingsGeneral) {
        settingsUi().openSettings = true;
        settingsUi().openSettingsTab = 0;
    }
    if (cfg.wantSettingsAI) {
        settingsUi().openSettings = true;
        settingsUi().openSettingsTab = 1;
    }
    if (cfg.wantSettingsPrompts) {
        settingsUi().openSettings = true;
        settingsUi().openSettingsTab = 2;
    }
    if (cfg.wantSettingsAdvanced) {
        settingsUi().openSettings = true;
        settingsUi().openSettingsTab = 3;
    }
    // T-W28 / T-DP12: AI Effort shots — open the modal on the AI tab; the effort
    // model is seeded from a committed fixture below (no python subprocess in shot mode).
    if (cfg.wantAiEffortSimple || cfg.wantAiEffortAdvanced || cfg.wantAiEffortWarn ||
        cfg.wantAiEffortAuto) {
        settingsUi().openSettings = true;
        settingsUi().openSettingsTab = 1;
    }
    if (cfg.wantComposerDropdown) {
        // Composer model dropdown: seed chat state so the composer is visible and the
        // dropdown opens on the next frame via the UI infrastructure.
        seedChatShot(st);
        settingsUi().openComposerModelDropdown = true;
    }
    if (cfg.wantPermissionsPopup) {
        // Permissions popup: seed chat so the prelude bar is visible, open the popup.
        seedChatShot(st);
        settingsUi().openPermissionsPopup = true;
    }
    if (cfg.wantExplorer) st.railTab = kFilesRailTab;
    if (cfg.wantRibbon) {
        st.workspace = cmd::Workspace::Model;
        ribbon::applyShotEnv(st);
        // T-BL31: suppress the on-load ShopCraft toast for ribbon-icon captures so it can't
        // overlap ribbon groups; re-enable with AI_CAD_SHOT_KEEPTOAST=1 for toast verification.
        const char* keep = std::getenv("AI_CAD_SHOT_KEEPTOAST");
        if (!(keep && *keep)) st.suppressToasts = true;
    }
    if (cfg.wantSketch || cfg.wantSketchConvert) {
        st.inSketch = true;
        st.workspace = cmd::Workspace::Sketch;
        ribbon::applyShotEnv(st);
    }
    if (cfg.wantFea || cfg.wantFlow || cfg.wantFeaOverlay ||
        cfg.wantSimMaterial || cfg.wantSimPrecheck || cfg.wantSimPostcheck) {
        st.workspace = cmd::Workspace::Simulate;
        ribbon::applyShotEnv(st);
    }
    if (cfg.wantCam) st.workspace = cmd::Workspace::Model;
    if (cfg.wantAsm) st.workspace = cmd::Workspace::Assemble;
    // wantDrawing: do NOT pre-switch to Drafting here — the Drafting panel self-creates a
    // drawing from the ACTIVE doc on first render, and at configure time that's the startup
    // demo doc, not the scenario's Plate (T-G9 harness finding: the captured drawing_new
    // sourced the wrong document). The drawing scenario flips the workspace itself after
    // building the Plate, matching host's part-first ordering.
    if (cfg.wantEcad) st.workspace = cmd::Workspace::Schematic;
    // T-DP15: workspace-kind shots — pre-switch to the target workspace before capture
    if (cfg.wantWsOverlayHosting)   st.workspace = cmd::Workspace::Model;
    if (cfg.wantWsFullViewportEcad) { st.workspace = cmd::Workspace::Schematic;
        st.docs.push_back({"Schematic1", cmd::DocType::Schematic});
        st.activeDoc = (int)st.docs.size() - 1; }
    if (cfg.wantWsAiAgent) {
        st.workspace = cmd::Workspace::AiAgent;
        // T-DP16: seed the populated DAG here (configureShot, no worker gate) so the
        // takeover renders the mission-DAG centerpiece — not the empty state. Same
        // fixture as the agent_dag shot; select a node so the detail pane renders.
        ui::seedAgentDagPanelFromJsonl(agentDagFixtureJsonl());
        ui::agentDagShotSelectNode("n_geom");
        cfg.wsAiAgentShotDone = true;
        std::printf("ws_ai_agent: AiAgent workspace — full-viewport takeover (DAG centerpiece seeded)\n");
        std::fflush(stdout);
    }
    if (cfg.wantWsPlm)      st.workspace = cmd::Workspace::PlmManagement;
    if (cfg.wantWsSketch3d) {
        // 3D free-space sketcher (OverlayHosting) — the live 3D scene keeps rendering
        // under the point/line overlay, so this stays a Part-doc Model-style shot.
        st.workspace = cmd::Workspace::Sketch3D;
        ui::sketch3dSeedDemo();
        std::printf("ws_sketch3d: Sketch3D workspace — 3D free-space points+lines over the live scene\n");
        std::fflush(stdout);
    }
    if (cfg.wantMultidoc) st.railTab = 0;
    if (cfg.wantActivity) st.railTab = 0;

    if (cfg.wantPmform) {
        st.railTab = 0;
        // pmform is a *form* screenshot: open the command's data-driven PropertyManager
        // form (drawInspector renders when activeTool is set), NOT dispatch() which routes
        // to actionBus and *executes* the command (leaving activeTool empty → no form).
        // pmApplyShotEnv() (drawInspector) then applies any AI_CAD_PMSET field presets.
        if (const char* cmd = std::getenv("AI_CAD_PMCMD"))
            if (*cmd) st.activeTool = cmd;
    }

    if (cfg.wantChat) seedChatShot(st);
    if (cfg.wantPlan) seedPlanShot(st);
    if (cfg.wantAssembly) seedAssemblyShot(st);
    if (cfg.wantPreview) seedPreviewShot(st);
    if (cfg.wantQuestion) seedQuestionShot(st);  // T-BL40
    if (cfg.wantScaffoldDemo)  ui::openScaffoldDemoPanel();
    if (cfg.wantPartsBrowser)  ui::openPartsPanel();
    cfg.wantCamWorkspace = eq("cam_workspace");
    if (cfg.wantCamWorkspace) ui::openCamWorkspacePanel();
    if (cfg.wantBom)          ui::openBomPanel();
    if (cfg.wantRequirements) ui::openRequirementsPanel();  // T-U5
    if (cfg.wantTransparency) ui::openTransparencyPanel();  // T-U8
    if (cfg.wantPiping) {       // T-U7 — seed fixture immediately (no worker needed)
        ui::openPipingPanel();
        const std::string pipDoc = R"({"schema":"aicad.piping/1","id":"fix-p","name":"Demo Piping","units":{"system":"MMGS"},"material":"A106-B","runs":[{"id":"r1","name":"Header","nps":2.0,"schedule":"40","path":[[0,0,0],[1000,0,0]],"end_type":"BW","material":null,"bend_radius":null,"suppressed":false},{"id":"r2","name":"Branch","nps":1.5,"schedule":"40","path":[[1000,0,0],[1000,500,0]],"end_type":"BW","material":null,"bend_radius":null,"suppressed":false}],"fittings":[{"id":"ft1","type":"tee","name":"Tee","nps":2.0,"nps2":null,"origin":[1000,0,0],"orient":[0,0,0],"schedule":"40","material":null,"catalog_ref":null,"params":{},"suppressed":false}]})";
        const std::string pipSolve = R"({"ok":true,"converged":true,"iterations":4,"links":[{"run_id":"r1","flow_m3h":15.2,"velocity_ms":1.7,"pressure_drop_kpa":8.3},{"run_id":"r2","flow_m3h":6.8,"velocity_ms":1.4,"pressure_drop_kpa":4.1}],"nodes":[{"node_id":"n0","pressure_kpa":120.0,"head_m":12.2}]})";
        ui::seedPipingPanelFromJson(pipDoc, pipSolve);
        cfg.pipingShotDone = true;
    }
    if (cfg.wantHydraulic) {   // T-U7
        ui::openHydraulicPanel();
        const std::string hydDoc = R"({"schema":"aicad.hydraulic/1","id":"fix-h","name":"Demo Circuit","units":{"pressure":"bar","flow":"L/min","force":"kN","position":"mm"},"components":[{"id":"c1","type":"reservoir","name":"Tank","x":0,"y":0,"params":{}},{"id":"c2","type":"pump","name":"Gear Pump","x":200,"y":0,"params":{}},{"id":"c3","type":"valve_relief","name":"Relief","x":400,"y":0,"params":{}},{"id":"c4","type":"valve_directional","name":"4/3 DCV","x":600,"y":0,"params":{}},{"id":"c5","type":"cylinder_linear","name":"Lift Cyl","x":800,"y":0,"params":{}}],"hoses":[{"id":"h1","from_port":{"comp_id":"c1","port":"generic"},"to_port":{"comp_id":"c2","port":"generic"}},{"id":"h2","from_port":{"comp_id":"c2","port":"pressure"},"to_port":{"comp_id":"c3","port":"generic"}},{"id":"h3","from_port":{"comp_id":"c2","port":"pressure"},"to_port":{"comp_id":"c4","port":"pressure"}},{"id":"h4","from_port":{"comp_id":"c4","port":"work_a"},"to_port":{"comp_id":"c5","port":"generic"}},{"id":"h5","from_port":{"comp_id":"c4","port":"tank"},"to_port":{"comp_id":"c1","port":"generic"}}],"annotations":[]})";
        const std::string hydSize = R"({"c2":{"displacement_cc_rev":13.793,"shaft_power_kw":7.843},"c5":{"extend_force_kn":37.306,"extend_flow_lpm":2.4}})";
        ui::seedHydraulicPanelFromJson(hydDoc, hydSize);
        cfg.hydraulicShotDone = true;
    }
    if (cfg.wantStructural) {  // T-U7
        ui::openStructuralPanel();
        const std::string strDoc = R"({"schema":"aicad.structure/1","id":"fix-s","name":"Demo Frame","elements":[{"id":"e1","element_type":"column","section_name":"W8x31","material":"steel","length_mm":3000,"transform":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]},{"id":"e2","element_type":"beam","section_name":"W10x45","material":"steel","length_mm":4000,"transform":[1,0,0,0,0,1,0,0,0,0,1,0,3000,0,0,1]},{"id":"e3","element_type":"column","section_name":"W8x31","material":"steel","length_mm":3000,"transform":[1,0,0,0,0,1,0,0,0,0,1,0,7000,0,0,1]}],"load_cases":[]})";
        const std::string strRes = R"({"e1":{"axial_n":-45000,"moment_max_nm":2800000,"deflection_max_mm":1.2,"dc_ratio":0.62,"ok":true},"e2":{"axial_n":0,"moment_max_nm":18000000,"deflection_max_mm":8.4,"dc_ratio":0.85,"ok":true},"e3":{"axial_n":-38000,"moment_max_nm":2200000,"deflection_max_mm":1.1,"dc_ratio":0.58,"ok":true}})";
        ui::seedStructuralPanelFromJson(strDoc, strRes);
        cfg.structuralShotDone = true;
    }
    if (cfg.wantGallery) {
        ui::openGalleryPanel();
        ui::selectAndOpenFirstSampleForShot();  // proves click -> doc_open -> editable tree
    }
    if (cfg.wantMissionControl) ui::openMissionControlPanel();  // T-F17
    if (cfg.wantAgentDag || cfg.wantAgentDagCompact)           // T-DP17
        ui::openAgentDagPanel();

    // T-W17 — seed from committed fixture run dirs immediately (no worker needed).
    if (cfg.wantAgentInspector || cfg.wantAgentInspectorEmpty ||
        cfg.wantAgentInspectorFailure || cfg.wantAgentInspectorDelegation) {
        std::string fx;
        if (findFixturesDir(fx)) {
            const std::string run = fx + (cfg.wantAgentInspectorEmpty ? "/runs/r_empty"
                                                                      : "/runs/r_demo_bracket");
            ui::openAgentInspectorPanel();
            ui::loadInspectorRunDir(run);
            // Failure/audit render only for the fixer turn (index 2); the
            // Delegation tab body renders only when active — force each so the
            // shots actually exercise those surfaces.
            if (cfg.wantAgentInspectorFailure)    ui::inspectorShotSelectTurn(2);
            if (cfg.wantAgentInspectorDelegation) ui::inspectorShotForceDelegationTab();
            std::printf("agent_inspector: loaded run dir %s\n", run.c_str());
        } else {
            std::printf("agent_inspector: fixtures dir not found\n");
        }
        std::fflush(stdout);
    }
    if (cfg.wantWatchGrid) {
        std::string fx;
        if (findFixturesDir(fx)) {
            const std::string ws = fx + "/runs/r_demo_bracket/workspace_status.json";
            ui::openWatchGridPanel();
            ui::loadWatchWorkspace(ws);
            std::printf("watch_grid: loaded workspace %s\n", ws.c_str());
        } else {
            std::printf("watch_grid: fixtures dir not found\n");
        }
        std::fflush(stdout);
    }
    // T-DP13: agent_multifile — 2 docs under concurrent agent work (fixture-seeded, no worker).
    // Opens the Watch Grid panel (reuse T-W17 substrate) seeded from the r_concurrent_agents
    // fixture: both docs are node_status=building so the live activity indicators are visible.
    // Doc tabs also have agentRunning=true wired in tickSimpleShots to show per-tab running dots.
    if (cfg.wantAgentMultifile) {
        std::string fx;
        if (findFixturesDir(fx)) {
            const std::string ws = fx + "/runs/r_concurrent_agents/workspace_status.json";
            ui::openWatchGridPanel();
            ui::loadWatchWorkspace(ws);
            std::printf("agent_multifile: loaded workspace %s\n", ws.c_str());
        } else {
            std::printf("agent_multifile: fixtures dir not found\n");
        }
        std::fflush(stdout);
    }
    if (cfg.wantAgentSettings || cfg.wantAgentSettingsError) {
        std::string fx;
        if (findFixturesDir(fx)) {
            const std::string vf = fx + (cfg.wantAgentSettingsError
                                             ? "/routing/routing_view_error.json"
                                             : "/routing/routing_view.json");
            const std::string json = readWholeFile(vf);
            // Routing editor is inline in the Settings modal AI tab (consolidation).
            ui::settingsUi().openSettings    = true;
            ui::settingsUi().openSettingsTab = 1;  // "AI Models & Routing"
            ui::seedAgentSettingsFromViewJson(json);
            std::printf("agent_settings: seeded from %s (%zu bytes)\n", vf.c_str(), json.size());
        } else {
            std::printf("agent_settings: fixtures dir not found\n");
        }
        std::fflush(stdout);
    }
    // T-W28 / T-DP12: seed the AI Effort section from a committed effort view fixture.
    if (cfg.wantAiEffortSimple || cfg.wantAiEffortAdvanced || cfg.wantAiEffortWarn || cfg.wantAiEffortAuto) {
        std::string fx;
        if (findFixturesDir(fx)) {
            const char* which = cfg.wantAiEffortWarn       ? "/effort/effort_view_warn.json"
                                : cfg.wantAiEffortAdvanced ? "/effort/effort_view_advanced.json"
                                : cfg.wantAiEffortAuto     ? "/effort/effort_view_auto.json"
                                                           : "/effort/effort_view.json";
            const std::string vf = fx + which;
            const std::string json = readWholeFile(vf);
            ui::seedAiEffortFromViewJson(json);
            std::printf("ai_effort: seeded from %s (%zu bytes)\n", vf.c_str(), json.size());
        } else {
            std::printf("ai_effort: fixtures dir not found\n");
        }
        std::fflush(stdout);
    }

    if (cfg.wantSimStudies)   ui::openSimStudiesPanel();  // T-U6
    if (cfg.wantPick) setupPickShot(st);
    if (cfg.wantGlassperf) setupPickShot(st);

    st.tryStartWorker();
    std::printf("shot: AI_CAD_SHOT=%s capture=%s\n",
                shotEnv, capturePath ? capturePath : "(none)");
    std::fflush(stdout);
}

void scenarioTick(app::AppState& st) {
    if (!g_runner || !g_cfg || !g_cfg->active) return;
    g_runner->tick(*g_cfg, st);
}

void ScenarioRunner::tickSimpleShots(ShotConfig& cfg, app::AppState& st) {
    if (!st.workerStarted) st.tryStartWorker();
    if (!st.workerStarted) return;

    // T-BL37: origin glyph rendered + selected evidence shot (AI_CAD_SHOT=origin).
    // Creates a minimal doc so the engine is live and the Reference/Origin row populates,
    // then unhides the origin point glyph and selects it so the screenshot shows both.
    if (cfg.wantOrigin && !cfg.originShotDone) {
        cfg.originShotDone = true;
        std::string err;
        if (st.docMirror.newPart(st.worker, err)) {
            st.addDocTab(false);
            auto r1 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},
                    {"params",{{"length",dim(80)},{"width",dim(60)},{"height",dim(40)}}}
                }}}
            }));
            loadMesh(st, r1.aicm_path);
            std::printf("origin: box ok=%d rev=%d\n", (int)r1.ok, r1.revision);
        } else {
            std::printf("origin: doc_new failed: %s\n", err.c_str());
        }
        // Un-hide the origin point glyph and select it — these drive syncRefDatums()
        // in viewport.cpp which calls r.setHiddenDatums / r.setSelectedDatumId each frame.
        featureTreeUnhideOrigin();
        featureTreeSelectOrigin();
        st.status = "Origin: point glyph visible + selected";
        std::printf("origin: glyph unhidden + selected\n");
        std::fflush(stdout);
    }

    if (cfg.wantTree && !cfg.treeShotDone) {
        cfg.treeShotDone = true;
        std::string err;
        if (st.docMirror.newPart(st.worker, err)) {
            st.addDocTab(false);
            auto r1 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},
                    {"params",{{"length",dim(50)},{"width",dim(40)},{"height",dim(30)}}}
                }}}
            }));
            loadMesh(st, r1.aicm_path);
            std::printf("tree: box ok=%d rev=%d\n", (int)r1.ok, r1.revision);
            nlohmann::json edgeRef = nlohmann::json::parse(
                R"({"kind":"edge","surf":"line","centroid":[0,0,15],"axis":[0,0,1],"area_or_len":30})");
            auto r2 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","add_feature"},{"feature",{
                    {"type","fillet"},{"name","Fillet1"},
                    {"params",{{"radius",dim(3)}}},
                    {"refs",{{"edges",nlohmann::json::array({edgeRef})}}}
                }}}
            }));
            loadMesh(st, r2.aicm_path);
            auto ru = st.docMirror.undo(st.worker);
            loadMesh(st, ru.aicm_path);
            auto rr = st.docMirror.redo(st.worker);
            loadMesh(st, rr.aicm_path);
            std::printf("tree: undo/redo ok=%d/%d\n", (int)ru.ok, (int)rr.ok);
        } else {
            std::printf("tree: doc_new failed: %s\n", err.c_str());
        }
        std::fflush(stdout);
    }

    if (cfg.wantEquations && !cfg.equationsShotDone) {
        cfg.equationsShotDone = true;
        std::string err;
        if (st.docMirror.newPart(st.worker, err)) {
            st.addDocTab(false);
            st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","eq_set"},{"name","width"},{"expr","20"}}}));
            st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","eq_set"},{"name","height"},{"expr","width*1.5"}}}));
            st.status = "Equations: width=20, height=width*1.5";
            std::printf("equations: globals seeded\n");
        }
        std::fflush(stdout);
    }

    // Parts browser: seed the panel model from a synchronous part_search call so the
    // screenshot shows populated results without needing an async RPC round-trip.
    // Follows the same worker-one-shot settle pattern as wantEquations / wantTree.
    // No step machine — avoids the T-G13/issue-#7 offscreen deadlock.
    if (cfg.wantPartsBrowser && !cfg.partsBrowserShotDone) {
        cfg.partsBrowserShotDone = true;
        nlohmann::json res;
        std::string err;
        if (st.worker.call("part_search", {{"query", "M8"}, {"limit", 20}}, res, err, 5000)) {
            // Directly seed the panel's file-static model so render shows real rows.
            ui::seedPartsPanelFromJson(res.dump(), "M8");
            std::printf("parts_browser: seed search ok (count=%d)\n",
                        res.value("count", 0));
        } else {
            std::printf("parts_browser: seed search failed (worker not ready): %s\n",
                        err.c_str());
        }
        std::fflush(stdout);
    }

    if (cfg.wantEcad && !cfg.ecadShotDone) {
        cfg.ecadShotDone = true;
        nlohmann::json res;
        std::string err;
        const char* kind = std::getenv("AI_CAD_ECAD_KIND");
        bool wantPcb = kind && std::strcmp(kind, "pcb") == 0;
        st.docs.push_back({wantPcb ? "Board1" : "Schematic1",
                           wantPcb ? cmd::DocType::Pcb : cmd::DocType::Schematic});
        st.activeDoc = (int)st.docs.size() - 1;
        st.workspace = wantPcb ? cmd::Workspace::Board : cmd::Workspace::Schematic;
        // Host-parity AI-design flow (T-G9 harness finding: this used to call
        // "ecad_orchestrate" — a method that does not exist in the engine — so the ecad
        // scenario produced no schematic and no netlist/ERC compile at all). Host's
        // ecadAiDesign: ecad_design(request) -> ecad_tree(doc_id) -> adopt into the editor.
        if (st.worker.call("ecad_design",
                {{"request", "5V resistive divider with 2.5V output"}}, res, err, 60000)) {
            const std::string id = res.is_object() ? res.value("doc_id", std::string())
                                                   : std::string();
            nlohmann::json tree;
            if (!id.empty() &&
                st.worker.call("ecad_tree", {{"doc_id", id}}, tree, err, 60000)) {
                ui::ecadEditorMirror().loadFromTree(tree);
                std::printf("ecad: ai-design ok doc=%s\n", id.c_str());
            } else {
                std::printf("ecad: ecad_tree failed (%s)\n", err.c_str());
            }
        } else {
            std::printf("ecad: ecad_design failed (%s)\n", err.c_str());
        }
        std::fflush(stdout);
    }

    // T-U3: CAM workspace panel screenshot — build a plate + run cam_program, then adopt
    // the job into the new ui/imgui cam_panel (camWorkspaceLoadJob). One-shot like wantTree.
    if (cfg.wantCamWorkspace && !cfg.camWorkspaceShotDone) {
        cfg.camWorkspaceShotDone = true;
        std::string err;
        if (st.docMirror.newPart(st.worker, err)) {
            st.addDocTab(false);
            auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Plate"},
                    {"params",{{"length",dim(80)},{"width",dim(60)},{"height",dim(15)}}}}}}}));
            loadMesh(st, r.aicm_path);
            nlohmann::json camRes;
            std::string e2;
            bool camOk = st.worker.call("cam_program",
                {{"doc_id", st.docMirror.docId()}, {"post", "grbl"}}, camRes, e2, 30000);
            std::string camDocId = (camOk && camRes.is_object())
                                   ? camRes.value("cam_doc_id", std::string()) : std::string();
            std::printf("cam_workspace-shot: ok=%d cam_doc=%s\n",
                        (int)camOk, camDocId.c_str());
            std::fflush(stdout);
            if (!camDocId.empty())
                ui::camWorkspaceLoadJob(st, camDocId);
            else
                ui::openCamWorkspacePanel();  // show empty panel if program failed
        } else {
            std::printf("cam_workspace-shot: doc_new failed: %s\n", err.c_str());
            std::fflush(stdout);
            ui::openCamWorkspacePanel();
        }
    }

    // T-U4: BOM & sourcing panel one-shot.
    // Seeds two bought lines (M3 screw + 608ZZ bearing) with MPNs so the panel renders
    // a realistic table even without a real assembly doc — prices will be BOM_UNPRICED
    // unless a sourcing provider is configured, which is the correct unpriced state.
    if (cfg.wantBom && !cfg.bomShotDone) {
        cfg.bomShotDone = true;
        // Try the live bom_price RPC first; if the engine.bom module is not provisioned
        // the call fails and we fall back to seeding deterministic fixture rows.
        nlohmann::json res;
        std::string err;
        bool liveOk = st.worker.call("bom_price", {{"doc_path", ""}}, res, err, 15000);
        if (liveOk && res.value("ok", false) && res.contains("bom") && res["bom"].is_array()
                && !res["bom"].empty()) {
            std::string resStr = res.dump();
            ui::seedBomPanelFromJson(resStr);
            std::printf("bom: live rollup ok (%d lines)\n", (int)res["bom"].size());
        } else {
            // Fixture: two bought lines with MPNs, null prices (correct unpriced state).
            const std::string fixture = R"({
                "ok": true,
                "bom": [
                    {"line_id":"fix_1","kind":"bought","quantity":4,"unit":"ea",
                     "description":"M3x10 Socket Head Cap Screw","mpn":"ISO4762-M3x10-A2",
                     "vendor_part":{"supplier":"McMaster-Carr"},
                     "unit_cost":null,"ext_cost":null},
                    {"line_id":"fix_2","kind":"bought","quantity":2,"unit":"ea",
                     "description":"608ZZ Deep Groove Bearing","mpn":"608ZZ",
                     "vendor_part":{"supplier":""},
                     "unit_cost":null,"ext_cost":null},
                    {"line_id":"fix_3","kind":"made","quantity":1,"unit":"ea",
                     "description":"6061-T6 Aluminum Plate 12mm","mpn":"",
                     "vendor_part":null,
                     "unit_cost":null,"ext_cost":null}
                ]
            })";
            ui::seedBomPanelFromJson(fixture);
            std::printf("bom: fixture seeded (live unavailable: %s)\n", err.c_str());
        }
        std::fflush(stdout);
    }

    // T-U5 Requirements & verification panel.
    // Tries a live req_report RPC; falls back to a deterministic fixture that exercises
    // every valid status (proposed/user_confirmed/verified/failed/waived) plus two battery
    // checks (one pass, one fail with evidence) so the screenshot proves all readout paths.
    if (cfg.wantRequirements && !cfg.requirementsShotDone) {
        cfg.requirementsShotDone = true;
        nlohmann::json res;
        std::string err;
        bool liveOk = st.worker.call("req_report", {{"doc_path", ""}}, res, err, 15000);
        if (liveOk && res.value("ok", false) &&
                res.contains("requirements") && res["requirements"].is_array() &&
                !res["requirements"].empty()) {
            std::string resStr = res.dump();
            ui::seedRequirementsPanelFromJson(resStr);
            std::printf("requirements: live req_report ok (%d reqs)\n",
                        (int)res["requirements"].size());
        } else {
            // Fixture: covers proposed/user_confirmed/verified/failed/waived statuses
            // and 2 battery checks (pass + fail) — proves every rendering branch.
            const std::string fixture = R"json({
                "ok": true,
                "requirements": [
                    {
                        "id": "req_001",
                        "kind": "dimensional",
                        "status": "verified",
                        "text": "Overall envelope shall not exceed bounding box",
                        "quantity": {"op": "<=", "value": 150, "unit": "mm"},
                        "verify_by": [{"id": "v_geom_01"}]
                    },
                    {
                        "id": "req_002",
                        "kind": "material",
                        "status": "proposed",
                        "text": "Body material shall be 6061-T6 aluminum",
                        "quantity": null,
                        "verify_by": []
                    },
                    {
                        "id": "req_003",
                        "kind": "functional",
                        "status": "failed",
                        "text": "Static deflection under 500 N load shall not exceed 0.5 mm",
                        "quantity": {"op": "<=", "value": 0.5, "unit": "mm"},
                        "verify_by": [{"id": "v_fea_01"}]
                    },
                    {
                        "id": "req_004",
                        "kind": "cost",
                        "status": "user_confirmed",
                        "text": "Unit production cost target",
                        "quantity": {"op": "<=", "value": 85, "unit": "USD"},
                        "verify_by": []
                    },
                    {
                        "id": "req_005",
                        "kind": "standard",
                        "status": "waived",
                        "text": "RoHS compliance (waived for prototype run)",
                        "quantity": null,
                        "verify_by": []
                    }
                ],
                "battery": {
                    "checks": [
                        {
                            "check": "interference",
                            "passed": true,
                            "summary": "No interference detected (0 pairs)",
                            "elapsed_ms": 42
                        },
                        {
                            "check": "budget",
                            "passed": false,
                            "summary": "Cost estimate $112 exceeds $85 target",
                            "elapsed_ms": 8
                        }
                    ]
                }
            })json";
            ui::seedRequirementsPanelFromJson(fixture);
            std::printf("requirements: fixture seeded (live unavailable: %s)\n", err.c_str());
        }
        std::fflush(stdout);
    }

    // T-U8 Model & telemetry transparency panel.
    // Tries a live serving_status RPC; falls back to a fixture that exercises every
    // rendering branch: two routes (one stable, one canary), honest "not-opted-in"
    // canary state, and real sidecar dir scan.
    if (cfg.wantTransparency && !cfg.transparencyShotDone) {
        cfg.transparencyShotDone = true;
        nlohmann::json res;
        std::string err;
        bool liveOk = st.worker.call("serving_status", nlohmann::json::object(),
                                     res, err, 5000);
        if (liveOk && res.value("ok", false) &&
                res.contains("routes") && res["routes"].is_array()) {
            ui::seedTransparencyPanelForShot(res.dump());
            std::printf("transparency: live serving_status ok (%d routes)\n",
                        (int)res["routes"].size());
        } else {
            // Fixture: one stable route + one canary route, user not opted in.
            // Exercises which-brain table, canary section, and the "not opted in"
            // honest state.  Sidecar dirs are scanned from the real filesystem.
            const std::string fixture = R"json({
                "ok": true,
                "routes": [
                    {
                        "insertion_point": "copilot",
                        "provider": "openai",
                        "model": "gpt-4o",
                        "adapter_version": "",
                        "canary_pct": 0,
                        "opt_in_count": 0,
                        "opt_in": [],
                        "fallback": {"mode": "none", "provider": "", "model": ""}
                    },
                    {
                        "insertion_point": "reviewer",
                        "provider": "vllm",
                        "model": "Qwen3-4B",
                        "adapter_version": "v0.2.1",
                        "canary_pct": 15,
                        "opt_in_count": 3,
                        "opt_in": [],
                        "fallback": {"mode": "provider", "provider": "openai", "model": "gpt-4o"}
                    }
                ]
            })json";
            ui::seedTransparencyPanelForShot(fixture);
            std::printf("transparency: fixture seeded (live unavailable: %s)\n",
                        err.c_str());
        }
        std::fflush(stdout);
    }

    // T-DP11: Design-Pipeline Action Stream — populated state.
    // Exercises every entry kind: advisory (1 blocker, 1 warn), audit (1 issue),
    // council, delegation (with budget bars), trigger_fired, agent_turn (tool calls).
    // Fixture events use real schema fields from the write-site sources; no invented keys.
    if (cfg.wantDPStream && !cfg.dpStreamShotDone) {
        cfg.dpStreamShotDone = true;
        const std::string fixture = R"json([
            {
                "seq": 0, "ts": "2026-07-05T14:00:01Z",
                "actor": "foreman", "kind": "advisory",
                "payload": {
                    "schema": "aicad.advisory/1",
                    "seq": 0, "stage": "geometry",
                    "user_override": false,
                    "items": [
                        {
                            "id": "adv_a1b2c3",
                            "severity": "blocker",
                            "check": "thin_wall",
                            "finding": "Wall at boss_1 is 0.6 mm — below 1.0 mm minimum for injection moulding.",
                            "refs": {"doc_id": "fix-part-001"},
                            "numbers": {"wall_mm": 0.6, "min_wall_mm": 1.0},
                            "candidate_fixes": [
                                {
                                    "action": "resize_feature",
                                    "params": {"feature": "boss_1", "wall_mm": 1.2},
                                    "basis": "process minimum from material table",
                                    "predicted_effect": "Wall increases to 1.2 mm — passes mould gate.",
                                    "confidence": "table"
                                }
                            ],
                            "helper_tools": ["wall_thickness"],
                            "source": {"kind": "kernel", "detail": "wall_thickness_checker"}
                        },
                        {
                            "id": "adv_d4e5f6",
                            "severity": "warn",
                            "check": "draft_angle",
                            "finding": "Face F12 has 0 deg draft — 1 deg minimum recommended.",
                            "refs": {"doc_id": "fix-part-001"},
                            "numbers": {"draft_deg": 0.0, "min_draft_deg": 1.0},
                            "candidate_fixes": [
                                {
                                    "action": "add_draft",
                                    "params": {"face": "F12", "angle_deg": 1.5},
                                    "basis": "DFM standard for injection moulding",
                                    "predicted_effect": "Face F12 gets 1.5 deg draft.",
                                    "confidence": "table"
                                }
                            ],
                            "helper_tools": ["draft_analysis"],
                            "source": {"kind": "kernel", "detail": "draft_checker"}
                        }
                    ],
                    "trigger_events": []
                }
            },
            {
                "seq": 1, "ts": "2026-07-05T14:00:12Z",
                "actor": "overseer", "kind": "audit",
                "payload": {
                    "schema": "aicad.audit/1",
                    "id": "aud_7f8a9b0c",
                    "scope": {"stage": "geometry", "trigger": "stage_exit"},
                    "verdicts": [
                        {
                            "class": "thin_wall",
                            "severity": "blocker",
                            "finding": "Boss_1 wall 0.6 mm violates process minimum — must be resolved before build.",
                            "evidence": {"feature": "boss_1", "wall_mm": 0.6},
                            "required_action": {"kind": "fix", "target": "boss_1"},
                            "targeted_reentry": {"stage": "geometry", "role": "geometry_designer"}
                        },
                        {
                            "class": "ok",
                            "severity": "ok",
                            "finding": "Overall geometry intent matches brief.",
                            "evidence": {},
                            "required_action": {"kind": "none"}
                        }
                    ],
                    "model": {"provider": "openai", "id": "gpt-4o"},
                    "tokens": 1240
                }
            },
            {
                "seq": 2, "ts": "2026-07-05T14:01:30Z",
                "actor": "council", "kind": "artifact",
                "payload": {
                    "id": "cncl_abc123",
                    "kind": "council",
                    "council": {
                        "id": "cncl_abc123",
                        "topic": "Should boss_1 be resized or redesigned as a snap-fit?",
                        "seats": [
                            {"persona": "manufacturing", "role": "manufacturing_engineer"},
                            {"persona": "design", "role": "part_designer"},
                            {"persona": "stress", "role": "fea_analyst"}
                        ],
                        "positions": [
                            {"persona": "manufacturing", "position": "Resize to 1.2 mm wall — minimum change, no redesign risk."},
                            {"persona": "design",        "position": "Resize is sufficient; snap-fit adds assembly complexity."},
                            {"persona": "stress",        "position": "Resize to 1.2 mm is structurally adequate."}
                        ],
                        "dissents": [],
                        "synthesis": {
                            "decision": "Resize boss_1 to 1.2 mm wall thickness.",
                            "rationale": "All three seats agree resize is the minimal, low-risk fix. No dissent.",
                            "rejected_alternatives": []
                        }
                    }
                }
            },
            {
                "seq": 3, "ts": "2026-07-05T14:02:05Z",
                "actor": "geometry_designer", "kind": "delegation",
                "payload": {
                    "schema": "aicad.delegation/1",
                    "node_id": "del_001",
                    "role": "geometry_designer",
                    "status": "running",
                    "depth": 1,
                    "kind": "sub_role",
                    "model": {"provider": "openai", "id": "gpt-4o"},
                    "budget": {
                        "token_budget": 8000,
                        "tokens_used": 3200,
                        "wall_budget_s": 120.0,
                        "elapsed_s": 48.5
                    },
                    "note": "Delegated geometry resize sub-task"
                }
            },
            {
                "seq": 4, "ts": "2026-07-05T14:02:10Z",
                "actor": "foreman", "kind": "trigger_fired",
                "payload": {
                    "trigger_id": "thin_wall_repeat",
                    "id": "thin_wall_repeat",
                    "context_summary": "Thin-wall finding repeated after resize attempt — escalating to council",
                    "severity": "blocker"
                }
            },
            {
                "seq": 5, "ts": "2026-07-05T14:02:55Z",
                "actor": "geometry_designer", "kind": "agent_turn",
                "payload": {
                    "turn_index": 2,
                    "tool_calls": [
                        {"name": "add_features", "args_digest": "resize boss_1 wall=1.2mm"},
                        {"name": "render_part", "args_digest": ""},
                        {"name": "wall_thickness", "args_digest": "face=F_boss_1"}
                    ],
                    "result_summary": "Wall thickness now 1.2 mm — passes minimum."
                }
            }
        ])json";
        ui::seedDPStreamForShot(fixture, "fixture-run-dp11-populated");
        std::printf("dp_stream: fixture seeded (%d events)\n", 6);
        std::fflush(stdout);
    }

    // T-DP11: Design-Pipeline Action Stream — empty state.
    if (cfg.wantDPStreamEmpty && !cfg.dpStreamShotDone) {
        cfg.dpStreamShotDone = true;
        // Seed with the empty-events array — exercises the "Stream empty" empty state.
        ui::seedDPStreamForShot("[]", "fixture-run-dp11-empty");
        std::printf("dp_stream_empty: empty-state fixture seeded\n");
        std::fflush(stdout);
    }

    if (cfg.wantPiping && !cfg.pipingShotDone) {
        cfg.pipingShotDone = true;
        // Fixture: a simple two-run water loop (header + branch) with a solved result.
        const std::string pipingDocFixture = R"({
            "schema": "aicad.piping/1",
            "id": "fix-piping-1",
            "name": "Demo Piping Loop",
            "units": {"system": "MMGS"},
            "material": "A106-B",
            "runs": [
                {"id":"r1","name":"Header","nps":2.0,"schedule":"40",
                 "path":[[0,0,0],[1000,0,0]],"end_type":"BW","material":null,"bend_radius":null,"suppressed":false},
                {"id":"r2","name":"Branch","nps":1.5,"schedule":"40",
                 "path":[[1000,0,0],[1000,500,0]],"end_type":"BW","material":null,"bend_radius":null,"suppressed":false}
            ],
            "fittings": [
                {"id":"ft1","type":"tee","name":"Main Tee","nps":2.0,"nps2":null,
                 "origin":[1000,0,0],"orient":[0,0,0],"schedule":"40","material":null,"catalog_ref":null,"params":{},"suppressed":false}
            ]
        })";
        const std::string pipingSolveFixture = R"({
            "ok": true,
            "converged": true,
            "iterations": 4,
            "links": [
                {"run_id":"r1","flow_m3h":15.2,"velocity_ms":1.7,"pressure_drop_kpa":8.3},
                {"run_id":"r2","flow_m3h":6.8,"velocity_ms":1.4,"pressure_drop_kpa":4.1}
            ],
            "nodes": [
                {"node_id":"n0","pressure_kpa":120.0,"head_m":12.2},
                {"node_id":"n1","pressure_kpa":111.7,"head_m":11.4}
            ]
        })";
        ui::seedPipingPanelFromJson(pipingDocFixture, pipingSolveFixture);
        std::printf("piping_view: fixture seeded\n");
        std::fflush(stdout);
    }

    if (cfg.wantHydraulic && !cfg.hydraulicShotDone) {
        cfg.hydraulicShotDone = true;
        const std::string hydDocFixture = R"({
            "schema": "aicad.hydraulic/1",
            "id": "fix-hyd-1",
            "name": "Demo Hydraulic Circuit",
            "units": {"pressure":"bar","flow":"L/min","force":"kN","position":"mm"},
            "components": [
                {"id":"c1","type":"reservoir","name":"Tank","x":0,"y":0,"params":{}},
                {"id":"c2","type":"pump","name":"Gear Pump","x":200,"y":0,"params":{"displacement_cc_rev":13.8}},
                {"id":"c3","type":"valve_relief","name":"Relief Valve","x":400,"y":0,"params":{"set_pressure_bar":200}},
                {"id":"c4","type":"valve_directional","name":"4/3 DCV","x":600,"y":0,"params":{}},
                {"id":"c5","type":"cylinder_linear","name":"Lift Cylinder","x":800,"y":0,"params":{"bore_mm":50,"rod_mm":25,"stroke_mm":300}}
            ],
            "hoses": [
                {"id":"h1","from_port":{"comp_id":"c1","port":"generic"},"to_port":{"comp_id":"c2","port":"generic"}},
                {"id":"h2","from_port":{"comp_id":"c2","port":"pressure"},"to_port":{"comp_id":"c3","port":"generic"}},
                {"id":"h3","from_port":{"comp_id":"c2","port":"pressure"},"to_port":{"comp_id":"c4","port":"pressure"}},
                {"id":"h4","from_port":{"comp_id":"c4","port":"work_a"},"to_port":{"comp_id":"c5","port":"generic"}},
                {"id":"h5","from_port":{"comp_id":"c4","port":"tank"},"to_port":{"comp_id":"c1","port":"generic"}}
            ],
            "annotations": []
        })";
        // Sizing results: pump + cylinder pre-computed
        const std::string hydSizingFixture = R"({
            "c2": {"displacement_cc_rev":13.793,"shaft_power_kw":7.843},
            "c5": {"extend_force_kn":37.306,"extend_flow_lpm":2.4,"retract_force_kn":28.2,"retract_flow_lpm":1.8}
        })";
        ui::seedHydraulicPanelFromJson(hydDocFixture, hydSizingFixture);
        std::printf("hydraulic_view: fixture seeded\n");
        std::fflush(stdout);
    }

    if (cfg.wantStructural && !cfg.structuralShotDone) {
        cfg.structuralShotDone = true;
        const std::string structDocFixture = R"({
            "schema": "aicad.structure/1",
            "id": "fix-struct-1",
            "name": "Demo Frame",
            "elements": [
                {"id":"e1","element_type":"column","section_name":"W8x31","material":"steel",
                 "length_mm":3000,"transform":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]},
                {"id":"e2","element_type":"beam","section_name":"W10x45","material":"steel",
                 "length_mm":4000,"transform":[1,0,0,0,0,1,0,0,0,0,1,0,3000,0,0,1]},
                {"id":"e3","element_type":"column","section_name":"W8x31","material":"steel",
                 "length_mm":3000,"transform":[1,0,0,0,0,1,0,0,0,0,1,0,7000,0,0,1]}
            ],
            "load_cases": []
        })";
        // Analysis results: D/C ratios + deflections
        const std::string structResultsFixture = R"({
            "e1": {"axial_n":-45000,"moment_max_nm":2800000,"deflection_max_mm":1.2,"dc_ratio":0.62,"ok":true},
            "e2": {"axial_n":0,"moment_max_nm":18000000,"deflection_max_mm":8.4,"dc_ratio":0.85,"ok":true},
            "e3": {"axial_n":-38000,"moment_max_nm":2200000,"deflection_max_mm":1.1,"dc_ratio":0.58,"ok":true}
        })";
        ui::seedStructuralPanelFromJson(structDocFixture, structResultsFixture);
        std::printf("structural_view: fixture seeded\n");
        std::fflush(stdout);
    }
    if (cfg.wantMissionControl && !cfg.missionControlShotDone) {
        cfg.missionControlShotDone = true;
        const char* ts = "2026-07-03T00:47:41Z";
        std::string j;
        auto ev = [&](const char* body) {
            j += std::string("{\"ts\":\"") + ts + "\"," + body + "}\n";
        };
        // Phase 1: thin-slice (design + gate), completed.
        ev(R"j("kind":"dag.run.started","run_id":"c8efe1ae","graph_id":"thin-slice-soil-sensor","node_count":2,"graph_name":"Thin-slice: design+gate")j");
        ev(R"j("kind":"dag.node.started","run_id":"c8efe1ae","node_id":"n_design","node_type":"design","node_name":"ECAD schematic stub")j");
        ev(R"j("kind":"dag.node.done","run_id":"c8efe1ae","node_id":"n_design","output_summary":"Stub ECAD design completed")j");
        ev(R"j("kind":"dag.node.started","run_id":"c8efe1ae","node_id":"n_gate_preorder","node_type":"gate","node_name":"Pre-order gate")j");
        ev(R"j("kind":"dag.node.paused","run_id":"c8efe1ae","node_id":"n_gate_preorder","reason":"gate")j");
        ev(R"j("kind":"dag.node.approval_required","run_id":"c8efe1ae","node_id":"n_gate_preorder")j");
        ev(R"j("kind":"dag.node.done","run_id":"c8efe1ae","node_id":"n_gate_preorder","output_summary":"Gate approved")j");
        ev(R"j("kind":"dag.run.done","run_id":"c8efe1ae","status":"done","node_states":{"n_design":{"status":"done"},"n_gate_preorder":{"status":"done"}})j");
        // Phase 2: full mission — ends paused at the pre-order gate (steering live).
        ev(R"j("kind":"dag.run.started","run_id":"1948942a","graph_id":"full-mission-soil-sensor","node_count":10,"graph_name":"Battery-Powered Soil-Moisture Sensor")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_ecad","node_type":"ecad","node_name":"ECAD schematic")j");
        ev(R"j("kind":"dag.node.done","run_id":"1948942a","node_id":"n_ecad","output_summary":"Schematic: ATtiny85 + SHT31, battery circuit")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_firmware","node_type":"firmware","node_name":"Firmware")j");
        ev(R"j("kind":"dag.node.done","run_id":"1948942a","node_id":"n_firmware","output_summary":"AVR-C firmware stub generated")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_structural","node_type":"structural","node_name":"Structural FEA")j");
        ev(R"j("kind":"dag.node.done","run_id":"1948942a","node_id":"n_structural","output_summary":"FEA passed - min FOS 2.4")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_source","node_type":"source","node_name":"BOM sourcing")j");
        ev(R"j("kind":"dag.node.done","run_id":"1948942a","node_id":"n_source","output_summary":"BOM: 12 parts, $4.23 estimated")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_verify_interference","node_type":"verify","node_name":"Verify: interference")j");
        ev(R"j("kind":"dag.node.failed","run_id":"1948942a","node_id":"n_verify_interference","error":"backend_unavailable (fail-closed)")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_verify_erc","node_type":"verify","node_name":"Verify: ERC")j");
        ev(R"j("kind":"dag.node.failed","run_id":"1948942a","node_id":"n_verify_erc","error":"backend_unavailable (fail-closed)")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_verify_drc","node_type":"verify","node_name":"Verify: DRC")j");
        ev(R"j("kind":"dag.node.failed","run_id":"1948942a","node_id":"n_verify_drc","error":"backend_unavailable (fail-closed)")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_verify_budget","node_type":"verify","node_name":"Verify: budget")j");
        ev(R"j("kind":"dag.node.failed","run_id":"1948942a","node_id":"n_verify_budget","error":"backend_unavailable (fail-closed)")j");
        ev(R"j("kind":"dag.node.started","run_id":"1948942a","node_id":"n_gate_preorder","node_type":"gate","node_name":"Pre-order gate")j");
        ev(R"j("kind":"dag.node.paused","run_id":"1948942a","node_id":"n_gate_preorder","reason":"gate")j");
        ev(R"j("kind":"dag.node.approval_required","run_id":"1948942a","node_id":"n_gate_preorder")j");
        ui::seedMissionControlPanelFromJsonl(j);
        std::printf("mission_control: fixture seeded (%zu bytes, paused at gate)\n",
                    j.size());
        std::fflush(stdout);
    }

    // T-DP17: Agent DAG widget — fixture-seeded with a multi-layer workflow:
    //   • 2 mission phases (design pipeline + verification pass)
    //   • 3 delegation sub-agents (feature_architect, geometry_designer, build_strategist)
    //   • explicit edges so the layered layout shows hierarchy
    //   Full shot: pre-selects "n_geom" so the detail pane renders.
    //   Compact shot: no selection, graph only.
    if ((cfg.wantAgentDag || cfg.wantAgentDagCompact) && !cfg.agentDagShotDone) {
        cfg.agentDagShotDone = true;
        // T-DP16: fixture factored into agentDagFixtureJsonl() so the AI-Agent
        // workspace shot (ws_ai_agent) can seed the same populated DAG.
        ui::seedAgentDagPanelFromJsonl(agentDagFixtureJsonl());

        // Force the render mode matching the shot name.
        // agentDagShotForceMode overrides the DagWidgetMode::Full default in application.cpp
        // so the compact shot actually exercises the Compact branch.
        if (cfg.wantAgentDag) {
            ui::agentDagShotForceMode(aicad::ui::DagWidgetMode::Full);
            ui::agentDagShotSelectNode("n_geom");
        } else {
            ui::agentDagShotForceMode(aicad::ui::DagWidgetMode::Compact);
        }

        std::printf("agent_dag: fixture seeded (%zu bytes) mode=%s\n",
                    agentDagFixtureJsonl().size(), cfg.wantAgentDag ? "full" : "compact");
        std::fflush(stdout);
    }

    // T-DP15: workspace-kind shots — one-frame captures with no worker RPC.
    // ws_overlay_hosting: Modeling workspace shows glass left+right columns over viewport.
    // ws_full_viewport_ecad: Schematic workspace fills screen, no glass columns.
    // ws_ai_agent: AI-Agent shell fills screen, no glass columns.
    // ws_plm: PLM shell fills screen, no glass columns.
    if (cfg.wantWsOverlayHosting && !cfg.wsOverlayHostingShotDone) {
        cfg.wsOverlayHostingShotDone = true;
        st.workspace = cmd::Workspace::Model;
        std::printf("ws_overlay_hosting: Model workspace — glass overlay columns present\n");
        std::fflush(stdout);
    }
    if (cfg.wantWsFullViewportEcad && !cfg.wsFullViewportEcadShotDone) {
        cfg.wsFullViewportEcadShotDone = true;
        st.workspace = cmd::Workspace::Schematic;
        std::printf("ws_full_viewport_ecad: Schematic workspace — full-viewport, no glass columns\n");
        std::fflush(stdout);
    }
    // T-DP16: ws_ai_agent is seeded in configureShot() (no worker gate) — tickSimpleShots
    // returns early when no worker is started, so the seed cannot live here.
    if (cfg.wantWsPlm && !cfg.wsPlmShotDone) {
        cfg.wsPlmShotDone = true;
        st.workspace = cmd::Workspace::PlmManagement;
        std::printf("ws_plm: PlmManagement workspace — full-viewport shell, no glass columns\n");
        std::fflush(stdout);
    }
    if (cfg.wantWsSketch3d && !cfg.wsSketch3dShotDone) {
        cfg.wsSketch3dShotDone = true;
        st.workspace = cmd::Workspace::Sketch3D;
        ui::sketch3dSeedDemo();
        std::printf("ws_sketch3d: Sketch3D workspace — 3D free-space points+lines over the live scene\n");
        std::fflush(stdout);
    }
}

void ScenarioRunner::tickGlassperf(ShotConfig& cfg, app::AppState& st) {
    (void)st;
    ++cfg.glassperfFrame;
    const int f = cfg.glassperfFrame;

    if (f == 1) {
        g_gpFrameCount = 0;
        g_gpMeanMs = 0.0;
        g_gpMaxMs = 0.0;
        g_gpRingCount = 0;
        g_gpProfStarted = false;
        g_gpProfWarned = false;
    }

    ui::viewportState().yaw += 0.014f;

    double now = glfwGetTime();
    if (f > 1) {
        double dtMs = (now - g_gpPrevTime) * 1000.0;
        ++g_gpFrameCount;
        g_gpMeanMs += (dtMs - g_gpMeanMs) / static_cast<double>(g_gpFrameCount);
        if (dtMs > g_gpMaxMs) g_gpMaxMs = dtMs;
        g_gpRing[g_gpRingCount % kGlassperfRing] = static_cast<float>(dtMs);
        ++g_gpRingCount;
    }
    g_gpPrevTime = now;

    if (f == 30) {
        nlohmann::json res;
        std::string err;
        if (core::cpp_core().call("prof_start", {{"hz", 997}, {"mode", "all"}}, res, err)) {
            g_gpProfStarted = true;
        } else if (!g_gpProfWarned) {
            std::printf("glassperf: prof unavailable (%s)\n", err.c_str());
            g_gpProfWarned = true;
        }
    }

    // Register the GUI thread as the sampler target for the WHOLE 30..209 frame window (push
    // once, pop once): set_target() persists across calls, so ImGui + GL render + swap between
    // scenario ticks are all sampled. A per-tick push/pop pair would only cover the microseconds
    // inside this function (measured: 9 samples instead of ~3000).
    if (g_gpProfStarted && f == 30) {
        nlohmann::json res;
        std::string err;
        core::cpp_core().call("prof_target_push", nlohmann::json::object(), res, err);
    }
    if (g_gpProfStarted && f == 210) {
        nlohmann::json res;
        std::string err;
        core::cpp_core().call("prof_target_pop", nlohmann::json::object(), res, err);
    }

    if (f == 210) {
        nlohmann::json res;
        std::string err;
        if (core::cpp_core().call("prof_stop", {{"top", 40}}, res, err) && cfg.capturePath) {
            std::ofstream out(std::string(cfg.capturePath) + ".prof.json");
            if (out) out << res.dump(2);
        }
        ui::GlassTimings gt = ui::viewportGlassTimings();
        std::printf(
            "glassperf: frames=%d avg_ms=%.2f p99_ms=%.2f gpu_scene_ms=%.2f gpu_blur_ms=%.2f "
            "gpu_luma_ms=%.2f\n",
            g_gpFrameCount, g_gpMeanMs, glassperfP99(),
            gt.valid ? gt.sceneMs : 0.f, gt.valid ? gt.blurMs : 0.f, gt.valid ? gt.lumaMs : 0.f);
        std::fflush(stdout);
    }

    if (f == 220) cfg.glassperfShotDone = true;
}

void ScenarioRunner::tick(ShotConfig& cfg, app::AppState& st) {
    tickSimpleShots(cfg, st);

    // ---------- sim_studies (T-U6) — no worker needed; fixture seeds directly ----------
    // Runs unconditionally (not gated on workerStarted) so the panel populates on frame 1.
    // The panel auto-reloads via AsyncRpc when a real doc is opened later.
    if (cfg.wantSimStudies && !cfg.simStudiesShotDone) {
        cfg.simStudiesShotDone = true;
        const std::string fixture = R"json({
            "ok": true,
            "studies": [
                {
                    "id": "fix_fea_1",
                    "type": "fea_static",
                    "name": "Static stress (steel)",
                    "engine": "cpp",
                    "material": {"name": "steel", "yield_mpa": 250.0},
                    "mesh_fineness": 0.5,
                    "n_dof": 18432,
                    "results": {
                        "max_von_mises_mpa": 73.4,
                        "max_disp_mm": 0.127,
                        "min_fos": 3.41
                    }
                },
                {
                    "id": "fix_cfd_1",
                    "type": "cfd_flow",
                    "name": "CFD wind tunnel (air, 10 m/s, aoa 0)",
                    "engine": "cpp-lbm",
                    "params": {"velocity": 10.0, "aoa": 0.0, "fluid": "air"},
                    "results": {
                        "drag_n": 0.214,
                        "lift_n": 0.003,
                        "cd": 1.872,
                        "reynolds": 68000.0,
                        "converged": true
                    }
                }
            ]
        })json";
        ui::seedSimStudiesPanelFromJson(fixture);
        std::printf("sim_studies: fixture seeded\n");
        std::fflush(stdout);
    }

    // ---------- preview (T-F16) ----------
    // Re-arm the Preview dock-tab focus every frame: the window is created on frame 1 and only
    // docked (behind the viewport tab) on frame 2, so a single-shot request would be consumed
    // before the tab exists. SetNextWindowFocus each frame guarantees the capture frame shows it.
    if (cfg.wantPreview) ui::openPreviewPane();

    // ---------- glassperf (frame-driven perf harness; no worker needed) ----------
    if (cfg.wantGlassperf && !cfg.glassperfShotDone) tickGlassperf(cfg, st);

    // ---------- m1 ----------
    if (cfg.wantM1 && st.workerStarted && !cfg.m1ShotDone) {
        if (!m1DirReady) {
            m1DirReady = true;
            m1SavePath = repoBase() + "/scratch/m1_shots";
            std::error_code ec; std::filesystem::create_directories(m1SavePath, ec);
        }
        auto statusMs = [](const core::OpsResponse& r, const std::string& fid) -> double {
            if (!r.feature_status.is_array()) return -1.0;
            for (const auto& s : r.feature_status)
                if (s.is_object() && s.value("id", std::string()) == fid)
                    return s.value("ms", -1.0);
            return -1.0;
        };
        const int kSettle = 4;
        if (m1Settle > 0) { --m1Settle; return; }
        if (!m1Capture.empty()) return;
        std::string err;
        switch (m1Step) {
            case 0:
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                m1Capture = m1SavePath + "/m1_01_newpart.png"; m1Settle = kSettle; break;
            case 1: {
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},
                        {"params",{{"length",dim(60)},{"width",dim(40)},{"height",dim(10)}},
                                   {"centered",true}}}}}}));
                loadMesh(st, r.aicm_path);
                m1Capture = m1SavePath + "/m1_02_box.png"; m1Settle = kSettle; break;
            }
            case 2: {
                std::vector<nlohmann::json> edges;
                for (int tid = 1; tid <= 12 && (int)edges.size() < 2; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, se);
                    if (s.empty()) continue;
                    try {
                        auto o = nlohmann::json::parse(s);
                        if (o.contains("axis") && std::fabs(o["axis"][2].get<double>()) > 0.9)
                            edges.push_back(o);
                    } catch (...) {}
                }
                nlohmann::json arr = nlohmann::json::array();
                for (auto& e : edges) arr.push_back(e);
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{
                        {"type","fillet"},{"name","Fillet1"},
                        {"params",{{"radius",dim(4)}}},
                        {"refs",{{"edges",arr}}}}}}}));
                loadMesh(st, r.aicm_path);
                m1FilletId = r.id_map.is_object() && r.id_map.contains("$0")
                           ? r.id_map["$0"].get<std::string>() : "f2";
                m1Capture = m1SavePath + "/m1_03_fillet.png"; m1Settle = kSettle; break;
            }
            case 3: {
                nlohmann::json top; double bestZ = -1e30;
                for (int tid = 1; tid <= 40; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                    if (s.empty()) break;
                    try {
                        auto o = nlohmann::json::parse(s);
                        if (!o.contains("centroid")) continue;
                        double z = o["centroid"][2].get<double>();
                        if (z > bestZ) { bestZ = z; top = o; }
                    } catch (...) {}
                }
                nlohmann::json remove = nlohmann::json::array();
                if (top.is_object()) remove.push_back(top);
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{
                        {"type","shell"},{"name","Shell1"},
                        {"params",{{"thickness",dim(2)}}},
                        {"refs",{{"remove",remove}}}}}}}));
                loadMesh(st, r.aicm_path);
                m1FilletMsBeforeEdit = statusMs(r, m1FilletId);
                m1BoxMsBeforeEdit = statusMs(r, "f1");
                m1Capture = m1SavePath + "/m1_04_shell.png"; m1Settle = kSettle; break;
            }
            case 4: {
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","edit_feature"},{"id",m1FilletId},
                     {"params",{{"radius",dim(8)}}},
                     {"refs",nlohmann::json::object()}}}));
                loadMesh(st, r.aicm_path);
                st.activeTool = "fillet";
                m1Capture = m1SavePath + "/m1_05_edit.png"; m1Settle = kSettle; break;
            }
            case 5: {
                st.activeTool.clear();
                std::string sp = m1SavePath + "/part.step+";
                std::string serr;
                st.docMirror.save(st.worker, sp, serr);
                st.docMirror.open(st.worker, sp, serr);
                m1Capture = m1SavePath + "/m1_06_reopen.png"; m1Settle = kSettle; break;
            }
            default:
                cfg.m1ShotDone = true;
                std::printf("m1: scenario complete\n");
                break;
        }
        if (m1Step <= 5) ++m1Step;
    }

    // ---------- sketch (host-parity solver op-batch: incremental rect + drag + dim + solve) ----
    // T-G9 harness finding: the previous "simplified" flow here (one batched
    // sketchRectExtrude) exercised none of the solver op-batch host exercises. This is a
    // faithful port of host/src/ui/scenarios.cpp: entities/constraints added one op at a
    // time, drag + commit, set_dim 80->100, sketch_solve, enter/exit — the RPC sequence
    // tools/gui_parity/run_all.py sketch diffs against host.
    if (cfg.wantSketch && st.workerStarted && !cfg.sketchShotDone && cfg.frame >= 5) {
        if (!skDirReady) {
            skDirReady = true;
            skSavePath = repoBase() + "/scratch/sketch_shots";
            std::error_code ec; std::filesystem::create_directories(skSavePath, ec);
        }
        auto applyOne = [&](const nlohmann::json& op) {
            return st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        };
        const int kSettle = 4;
        if (skSettle > 0) { --skSettle; return; }
        if (!sketchCapture.empty()) return;
        std::string err;
        switch (skStep) {
            case 0: {  // new part + sketch + rect (entities/relations/dims) + enter
                if (!st.docMirror.newPart(st.worker, err)) {
                    std::printf("sketch: new_part FAILED: %s\n", err.c_str()); std::fflush(stdout);
                    cfg.sketchShotDone = true; break;
                }
                st.addDocTab(false);
                auto r = applyOne({{"op","add_feature"},{"feature",{
                    {"type","sketch"},{"name","Sketch1"},
                    {"refs",{{"plane","plane.top"}}},
                    {"params",{{"entities",nlohmann::json::array()},
                               {"constraints",nlohmann::json::array()}}}}}});
                skFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : "f1";
                auto addLine = [&](double x1,double y1,double x2,double y2,bool horiz,
                                   const std::string& prev)->std::string{
                    nlohmann::json cs = nlohmann::json::array();
                    cs.push_back({{"type",horiz?"horizontal":"vertical"},{"refs",{"$self"}}});
                    if (!prev.empty())
                        cs.push_back({{"type","coincident"},{"refs",{prev+".p2","$self.p1"}}});
                    auto rr = applyOne({{"op","sketch_add_entity"},{"sketch",skFid},
                        {"entity",{{"type","line"},{"p1",{x1,y1}},{"p2",{x2,y2}}}},{"constraints",cs}});
                    return (rr.id_map.is_object() && rr.id_map.contains("$0"))
                           ? rr.id_map["$0"].get<std::string>() : "";
                };
                skL0 = addLine(0,0,80,0,true,"");
                skL1 = addLine(80,0,80,40,false,skL0);
                skL2 = addLine(80,40,0,40,true,skL1);
                skL3 = addLine(0,40,0,0,false,skL2);
                applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                    {"constraint",{{"type","coincident"},{"refs",{skL3+".p2",skL0+".p1"}}}}});
                applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                    {"constraint",{{"type","fix"},{"refs",{skL0+".p1"}}}}});
                applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                    {"constraint",{{"type","distance"},{"refs",{skL0+".p1",skL0+".p2"}},
                                   {"value",{{"value",80.0},{"expr",nullptr}}}}}});
                applyOne({{"op","sketch_add_constraint"},{"sketch",skFid},
                    {"constraint",{{"type","distance"},{"refs",{skL1+".p1",skL1+".p2"}},
                                   {"value",{{"value",40.0},{"expr",nullptr}}}}}});
                // find the width constraint id (the distance on skL0) by scanning the tree
                skWidthCid.clear();
                {
                    const auto& tr = st.docMirror.tree();
                    if (tr.contains("features"))
                      for (auto& f : tr["features"])
                        if (f.value("id",std::string())==skFid && f.contains("params"))
                          for (auto& c : f["params"].value("constraints",nlohmann::json::array()))
                            if (c.value("type",std::string())=="distance" && c.contains("refs") &&
                                c["refs"].size()==2 && c["refs"][0].get<std::string>()==skL0+".p1")
                              skWidthCid = c.value("id",std::string());
                }
                // sketch mode entry (host: SketchMode::enterForFeature -> sketch_enter RPC;
                // the AI-CAD2 overlay renders from doc state via the sketch seam)
                nlohmann::json eres;
                st.worker.call("sketch_enter",
                    {{"doc_id", st.docMirror.docId()}, {"sketch", skFid}}, eres, err);
                st.inSketch = true;
                std::printf("sketch: new sketch %s rect built (width_cid=%s) entered\n",
                            skFid.c_str(), skWidthCid.c_str());
                std::fflush(stdout);
                sketchCapture = skSavePath + "/sketch_01_enter.png"; skSettle = kSettle;
                break;
            }
            case 1: {  // drag a corner + commit, then set width 80 -> 100
                nlohmann::json dres;
                nlohmann::json dragP = {{"doc_id",st.docMirror.docId()},{"sketch",skFid},
                    {"handle",skL1+".p2"},{"xy",nlohmann::json::array({85.0,45.0})}};
                st.worker.call("sketch_drag", dragP, dres, err);
                nlohmann::json cres;
                nlohmann::json cP = {{"doc_id",st.docMirror.docId()},{"sketch",skFid}};
                st.worker.call("sketch_drag_commit", cP, cres, err);
                st.docMirror.resync(st.worker);
                if (!skWidthCid.empty())
                    st.docMirror.applyOps(st.worker, nlohmann::json::array({
                        {{"op","sketch_set_dim"},{"sketch",skFid},{"id",skWidthCid},{"value",100.0}}}));
                nlohmann::json sres;
                st.worker.call("sketch_solve",
                    {{"doc_id",st.docMirror.docId()},{"sketch",skFid}}, sres, err);
                nlohmann::json eres;  // host re-enters to refresh overlay entities/status
                st.worker.call("sketch_enter",
                    {{"doc_id", st.docMirror.docId()}, {"sketch", skFid}}, eres, err);
                std::printf("sketch: drag+commit, width 80->100, re-solved; dof=%d\n",
                            sres.value("dof",-1));
                std::fflush(stdout);
                sketchCapture = skSavePath + "/sketch_02_dim.png"; skSettle = kSettle;
                break;
            }
            case 2: {  // exit + extrude region depth 10
                nlohmann::json xres;
                st.worker.call("sketch_exit",
                    {{"doc_id", st.docMirror.docId()}, {"sketch", skFid}}, xres, err);
                st.inSketch = false;
                st.docMirror.resync(st.worker);
                nlohmann::json region = {{"kind","region"},{"sketch",skFid},
                    {"centroid",nlohmann::json::array({50.0,20.0})},{"hint_index",0}};
                auto r = applyOne({{"op","add_feature"},{"feature",{
                    {"type","extrude"},{"name","Boss-Extrude1"},
                    {"params",{{"depth",{{"value",10.0},{"expr",nullptr}}},{"end","blind"},{"mode","boss"}}},
                    {"refs",{{"profile",nlohmann::json::array({region})}}}}}});
                loadMesh(st, r.aicm_path);
                bool exok = false;
                if (r.feature_status.is_array() && !r.feature_status.empty())
                    exok = r.feature_status.back().value("ok",false);
                std::printf("sketch: exit + extrude region depth 10 ok=%d\n", (int)exok);
                std::fflush(stdout);
                sketchCapture = skSavePath + "/sketch_03_solid.png"; skSettle = kSettle;
                break;
            }
            default:
                cfg.sketchShotDone = true;
                std::printf("sketch: scenario complete\n");
                break;
        }
        if (skStep <= 2) ++skStep;
    }

    // ---------- sketch_dispatch (T-BL48: prove the REAL interactive entry path) ----------
    // Unlike `sketch` (which force-sets st.inSketch and drives via direct sketch_enter RPCs,
    // bypassing dispatch), this scenario NEVER touches st.inSketch itself. Entry happens only
    // through the dispatch → AppState::pendingSketchCmds/pendingSketchEditFid channel, drained
    // in ui/viewport.cpp where the single s_sketch + SketchHost live. The drain runs
    // onRibbonCommand→enterNewOnSelection→enterForFeature (active_=true) / enterForFeature (edit),
    // then sets s.inSketch = s_sketch.active() || planePicking(). The overlay is double-gated
    // (if(s.inSketch) in viewport.cpp AND if(!active_) return in sketch_overlay.cpp), so a
    // rendered overlay PNG proves active_ became true via dispatch. Because inSketch only flips
    // on the NEXT frame's drain, every drain-dependent transition (enter/exit/reedit) dispatches
    // on one step + settles, then captures on the following step once the drain has run.
    if (cfg.wantSketchDispatch && st.workerStarted && !cfg.sketchDispatchShotDone && cfg.frame >= 5) {
        if (!sdDirReady) {
            sdDirReady = true;
            sdSavePath = repoBase() + "/scratch/sketch_dispatch_shots";
            std::error_code ec; std::filesystem::create_directories(sdSavePath, ec);
        }
        auto applyOne = [&](const nlohmann::json& op) {
            return st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        };
        const int kSettle = 4;
        if (sdSettle > 0) { --sdSettle; return; }
        if (!sketchDispatchCapture.empty()) return;
        std::string err;
        switch (sdStep) {
            case 0: {  // new part + sketch feature + 4-line rectangle profile (no entry yet)
                if (!st.docMirror.newPart(st.worker, err)) {
                    std::printf("sketch_dispatch: new_part FAILED: %s\n", err.c_str());
                    std::fflush(stdout);
                    cfg.sketchDispatchShotDone = true; break;
                }
                st.addDocTab(false);
                auto r = applyOne({{"op","add_feature"},{"feature",{
                    {"type","sketch"},{"name","Sketch1"},
                    {"refs",{{"plane","plane.top"}}},
                    {"params",{{"entities",nlohmann::json::array()},
                               {"constraints",nlohmann::json::array()}}}}}});
                sdFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : "f1";
                // A closed rectangle so the step-9 region extrude has a real profile to consume.
                auto addLine = [&](double x1,double y1,double x2,double y2,bool horiz,
                                   const std::string& prev)->std::string{
                    nlohmann::json cs = nlohmann::json::array();
                    cs.push_back({{"type",horiz?"horizontal":"vertical"},{"refs",{"$self"}}});
                    if (!prev.empty())
                        cs.push_back({{"type","coincident"},{"refs",{prev+".p2","$self.p1"}}});
                    auto rr = applyOne({{"op","sketch_add_entity"},{"sketch",sdFid},
                        {"entity",{{"type","line"},{"p1",{x1,y1}},{"p2",{x2,y2}}}},{"constraints",cs}});
                    return (rr.id_map.is_object() && rr.id_map.contains("$0"))
                           ? rr.id_map["$0"].get<std::string>() : "";
                };
                sdL0 = addLine(0,0,80,0,true,"");
                sdL1 = addLine(80,0,80,40,false,sdL0);
                sdL2 = addLine(80,40,0,40,true,sdL1);
                sdL3 = addLine(0,40,0,0,false,sdL2);
                applyOne({{"op","sketch_add_constraint"},{"sketch",sdFid},
                    {"constraint",{{"type","coincident"},{"refs",{sdL3+".p2",sdL0+".p1"}}}}});
                st.docMirror.resync(st.worker);
                std::printf("sketch_dispatch: sketch %s + rect built (NOT yet entered)\n",
                            sdFid.c_str());
                std::fflush(stdout);
                break;  // no capture, no settle: advance immediately to the dispatch step
            }
            case 1: {  // pre-select the plane, then dispatch("sketch_new") — DIRECT entry
                // Pre-selecting a datum plane routes enterNewOnSelection → createSketchOnPlane
                // → enterForFeature directly (no plane-pick). The viewport propagates
                // host.selectedFeatureId = st.selectedFeature before running onRibbonCommand.
                st.selectedFeature = "plane.top";
                st.dispatch("sketch_new");  // pushes to pendingSketchCmds; drain runs next frame
                std::printf("sketch_dispatch: dispatched sketch_new (plane.top pre-selected); "
                            "awaiting drain\n");
                std::fflush(stdout);
                sdSettle = kSettle;  // let the viewport drain run enterNewOnSelection → active_
                break;
            }
            case 2: {  // drain has run: st.inSketch must now be true — capture the overlay
                std::printf("sketch_dispatch: after drain inSketch=%d (overlay proves active_)\n",
                            (int)st.inSketch);
                std::fflush(stdout);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_01_enter.png";
                sdSettle = kSettle;
                break;
            }
            case 3: {  // activate the Line tool via dispatch; profile already visible
                st.dispatch("sk_line");  // tool activation; does not move inSketch
                std::printf("sketch_dispatch: dispatched sk_line (draw tool active)\n");
                std::fflush(stdout);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_02_draw.png";
                sdSettle = kSettle;
                break;
            }
            case 4: {  // dispatch("sketch_exit") — drain runs exitSketch next frame
                st.dispatch("sketch_exit");
                std::printf("sketch_dispatch: dispatched sketch_exit; awaiting drain\n");
                std::fflush(stdout);
                sdSettle = kSettle;
                break;
            }
            case 5: {  // drain ran exitSketch: st.inSketch must now be false — capture
                std::printf("sketch_dispatch: after exit drain inSketch=%d\n", (int)st.inSketch);
                std::fflush(stdout);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_03_exit.png";
                sdSettle = kSettle;
                break;
            }
            case 6: {  // MY delta: edit-existing via pendingSketchEditFid → enterForFeature
                st.pendingSketchEditFid = sdFid;  // drain runs enterForFeature (active_ true) next frame
                std::printf("sketch_dispatch: armed pendingSketchEditFid=%s; awaiting drain\n",
                            sdFid.c_str());
                std::fflush(stdout);
                sdSettle = kSettle;
                break;
            }
            case 7: {  // drain re-entered the sketch: st.inSketch true again — capture reedit
                std::printf("sketch_dispatch: after reedit drain inSketch=%d\n", (int)st.inSketch);
                std::fflush(stdout);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_04_reedit.png";
                sdSettle = kSettle;
                break;
            }
            case 8: {  // exit the reedit cleanly (drain) before extruding so the solid is unobscured
                st.dispatch("sketch_exit");
                std::printf("sketch_dispatch: dispatched sketch_exit after reedit; awaiting drain\n");
                std::fflush(stdout);
                sdSettle = kSettle;
                break;
            }
            case 9: {  // downstream consumption: extrude the sketch region depth 10
                st.docMirror.resync(st.worker);
                nlohmann::json region = {{"kind","region"},{"sketch",sdFid},
                    {"centroid",nlohmann::json::array({50.0,20.0})},{"hint_index",0}};
                auto r = applyOne({{"op","add_feature"},{"feature",{
                    {"type","extrude"},{"name","Boss-Extrude1"},
                    {"params",{{"depth",{{"value",10.0},{"expr",nullptr}}},{"end","blind"},{"mode","boss"}}},
                    {"refs",{{"profile",nlohmann::json::array({region})}}}}}});
                loadMesh(st, r.aicm_path);
                bool exok = false;
                if (r.feature_status.is_array() && !r.feature_status.empty())
                    exok = r.feature_status.back().value("ok",false);
                std::printf("sketch_dispatch: extrude region depth 10 ok=%d\n", (int)exok);
                std::fflush(stdout);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_05_extrude.png";
                sdSettle = kSettle;
                break;
            }
            // T-BL44: Space → orient view normal-to the active sketch plane.
            // Steps 10-13 re-enter the sketch, rotate the camera to a clearly non-normal
            // angle (yaw=99, pitch=55), inject a Space key press via ImGui IO, then verify
            // on the following frame that g_vpState snapped to cameraLock()'s normal-to
            // yaw/pitch (plane.top normal = yaw=0, pitch=90 by convention). We capture
            // before (rotated) + after (normal-to) screenshots as visual evidence.
            case 10: {  // re-enter the sketch (drain → active_ + inSketch)
                st.pendingSketchEditFid = sdFid;
                std::printf("sketch_dispatch[T-BL44]: re-entering sketch for Space test; "
                            "awaiting drain\n");
                std::fflush(stdout);
                sdSettle = kSettle;
                break;
            }
            case 11: {  // drain ran; rotate camera away from normal and capture "before" screenshot
                std::printf("sketch_dispatch[T-BL44]: after re-enter inSketch=%d; "
                            "rotating camera to (yaw=99 pitch=55) for Space snap test\n",
                            (int)st.inSketch);
                std::fflush(stdout);
                // Directly write g_vpState so syncCameraToGl pushes it to the GL camera.
                // We record the pre-Space values here; Space fires NEXT step after the
                // orbit read-back has settled at 99/55.
                ui::viewportState().yaw   = 99.f;
                ui::viewportState().pitch = 55.f;
                sdPreSpaceYaw   = 99.f;
                sdPreSpacePitch = 55.f;
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_06_before_space.png";
                sdSettle = 2;  // let the GL camera settle at 99/55 before Space
                break;
            }
            case 12: {  // camera settled at 99/55; inject Space — handler reads g_vpState via cameraLock
                std::printf("sketch_dispatch[T-BL44]: current yaw=%.1f pitch=%.1f; injecting Space\n",
                            ui::viewportState().yaw, ui::viewportState().pitch);
                std::fflush(stdout);
                // Keep the rotated camera for this frame so the orbit read-back starts at 99/55.
                ui::viewportState().yaw   = 99.f;
                ui::viewportState().pitch = 55.f;
                // Inject Space key press/release into ImGui IO so the handler in viewport.cpp
                // fires this drawViewport call (after the orbit read-back that sets 99/55).
                ImGui::GetIO().AddKeyEvent(ImGuiKey_Space, true);
                ImGui::GetIO().AddKeyEvent(ImGuiKey_Space, false);
                sdSettle = 2;  // let the snap settle into g_vpState
                break;
            }
            case 13: {  // Space has fired; capture after-state and log yaw/pitch delta
                float yw = ui::viewportState().yaw;
                float pt = ui::viewportState().pitch;
                std::printf("sketch_dispatch[T-BL44]: after Space: yaw=%.1f pitch=%.1f "
                            "(was yaw=%.1f pitch=%.1f) inSketch=%d\n",
                            yw, pt, sdPreSpaceYaw, sdPreSpacePitch, (int)st.inSketch);
                // cameraLock for plane.top is yaw=0/180 pitch=~90 (normal-to XY plane).
                // Verify the snap moved significantly away from our rotated (99,55) angle.
                bool snapped = (std::fabs(yw - sdPreSpaceYaw) > 5.f ||
                                std::fabs(pt - sdPreSpacePitch) > 5.f);
                std::printf("sketch_dispatch[T-BL44]: Space snap %s (delta yaw=%.1f pitch=%.1f)\n",
                            snapped ? "PASS" : "WARN-no-change",
                            std::fabs(yw - sdPreSpaceYaw), std::fabs(pt - sdPreSpacePitch));
                std::fflush(stdout);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_07_space_normal_to.png";
                sdSettle = kSettle;
                break;
            }
            case 14: {  // exit cleanly after Space test; T-BL46 drag tests begin at case 15
                st.dispatch("sketch_exit");
                std::printf("sketch_dispatch[T-BL44]: exiting sketch after Space test\n");
                std::fflush(stdout);
                sdSettle = kSettle;
                break;
            }
            // ---- T-BL46: drag dead-zone / element-drag capture priority ----
            // Root-cause fix: s_sketch.elementDragActive() gates camera nav at the
            // ImGui left-button drag threshold, so an element drag captures before the
            // camera can compete.  The four tests below inject real ImGui mouse events
            // (AddMousePosEvent + AddMouseButtonEvent) so the sketch overlay's own
            // hit-test arms dragArmed_/groupDragArmed_ on mouse-down; we do NOT force
            // those flags directly.  The two verify metrics:
            //   element drag → camera Δyaw + Δpitch ≈ 0  (capture-priority working)
            //   empty-space drag → camera Δyaw or Δpitch > 0 (view nav still alive)
            case 15: {  // re-enter the sketch so overlay hit-test is live
                st.pendingSketchEditFid = sdFid;
                std::printf("sketch_dispatch[T-BL46]: re-entering sketch for drag tests; "
                            "awaiting drain\n");
                std::fflush(stdout);
                sdSettle = kSettle;
                break;
            }
            case 16: {  // compute element screen coords via sketchMode().uvToScreen; record camera
                // Use uvToScreen(80,0) — the EXACT same projection hitTestHandle() uses for the
                // rect's bottom-right corner endpoint (.p2 of L0, UV=(80,0) in sketch space).
                // Using worldToScreen was wrong: sketch UV != world coords (depends on frame_).
                ImVec2 bMin = ui::viewportImageMin();
                ImVec2 bMax = ui::viewportImageMax();
                std::printf("sketch_dispatch[T-BL46]: imageRect bMin=(%.0f,%.0f) bMax=(%.0f,%.0f)\n",
                            bMin.x, bMin.y, bMax.x, bMax.y);
                // uvToScreen is only valid if basisOk_ (buildBasis ran at least once after
                // entering the sketch).  If the rect is too small it hasn't run yet.
                if (bMax.x > bMin.x && bMax.y > bMin.y) {
                    // Project all four corners via handleScreenPos() — the same affine basis
                    // that hitTestHandle() uses. Pick whichever is inside the image rect.
                    // Prefer (80,0) → (80,40) → (0,40) → (0,0) (far corner first).
                    struct { float u, v; } corners[] = {{80,0},{80,40},{0,40},{0,0}};
                    bool found = false;
                    for (auto& c : corners) {
                        ImVec2 s = ui::sketchMode().handleScreenPos(c.u, c.v);
                        if (s.x >= bMin.x && s.x <= bMax.x && s.y >= bMin.y && s.y <= bMax.y) {
                            sdElemPtX = s.x; sdElemPtY = s.y; found = true;
                            std::printf("sketch_dispatch[T-BL46]: handle UV=(%.0f,%.0f) → screen=(%.0f,%.0f)\n",
                                        c.u, c.v, s.x, s.y);
                            break;
                        }
                        std::printf("sketch_dispatch[T-BL46]: UV=(%.0f,%.0f) → screen=(%.0f,%.0f) [outside rect]\n",
                                    c.u, c.v, s.x, s.y);
                    }
                    if (!found) {
                        // Nothing projected inside — use image centre (won't arm a handle, but at
                        // least imgHovered=true so the camera-still test via dead-zone still runs).
                        sdElemPtX = (bMin.x + bMax.x) * 0.5f;
                        sdElemPtY = (bMin.y + bMax.y) * 0.5f;
                        std::printf("sketch_dispatch[T-BL46]: no corner in rect — using image centre\n");
                    }
                    // Empty-space anchor: guaranteed inside the image rect but away from the
                    // element.  Use a fixed fractional offset from bMin so it survives any
                    // camera orientation — the overlay hit-test will find nothing there.
                    sdEmptyPtX = bMin.x + (bMax.x - bMin.x) * 0.85f;
                    sdEmptyPtY = bMin.y + (bMax.y - bMin.y) * 0.85f;
                } else {
                    // viewport not wired (rare in headless): use safe screen fallbacks
                    sdElemPtX = 400.f; sdElemPtY = 300.f;
                    sdEmptyPtX = 80.f; sdEmptyPtY = 80.f;
                    std::printf("sketch_dispatch[T-BL46]: viewport not wired — using fallback coords\n");
                }
                // Record camera before element drag
                sdDragPreYaw   = ui::viewportState().yaw;
                sdDragPrePitch = ui::viewportState().pitch;
                // Position mouse over the handle endpoint; hover populates hitTestHandle next frame
                ImGui::GetIO().AddMousePosEvent(sdElemPtX, sdElemPtY);
                std::printf("sketch_dispatch[T-BL46]: element handle screen pos=(%.0f,%.0f) "
                            "empty pos=(%.0f,%.0f) camera yaw=%.1f pitch=%.1f\n",
                            sdElemPtX, sdElemPtY, sdEmptyPtX, sdEmptyPtY,
                            sdDragPreYaw, sdDragPrePitch);
                std::fflush(stdout);
                sdSettle = 3;  // hover needs 3 frames to populate hitTestHandle result
                break;
            }
            case 17: {  // LMB down at element pos — overlay arms dragArmed_/groupDragArmed_
                ImGui::GetIO().AddMousePosEvent(sdElemPtX, sdElemPtY);
                ImGui::GetIO().AddMouseButtonEvent(0, true);   // LMB press
                std::printf("sketch_dispatch[T-BL46]: LMB down at element (%.0f,%.0f); "
                            "elementDragActive=%d\n",
                            sdElemPtX, sdElemPtY,
                            (int)ui::sketchMode().elementDragActive());
                std::fflush(stdout);
                sdSettle = 1;  // one frame for arm to register
                break;
            }
            case 18: {  // inject slow drag: +20px right, LMB still held; read result next tick
                // A 20px move exceeds the overlay's 4px arm threshold → dragging_ becomes true.
                // drawViewport processes the drag AFTER this tick, so we read camera on case 19.
                float dragX = sdElemPtX + 20.f;
                float dragY = sdElemPtY;
                ImGui::GetIO().AddMousePosEvent(dragX, dragY);
                std::printf("sketch_dispatch[T-BL46]: injecting slow-drag +20px → (%.0f,%.0f); "
                            "elementDragActive=%d; camera read on next tick\n",
                            dragX, dragY, (int)ui::sketchMode().elementDragActive());
                std::fflush(stdout);
                sdSettle = 1;  // let drawViewport process delta before reading camera
                break;
            }
            case 19: {  // READ slow-drag result (after drawViewport processed it)
                {
                    float yaw   = ui::viewportState().yaw;
                    float pitch = ui::viewportState().pitch;
                    float dYaw   = std::fabs(yaw   - sdDragPreYaw);
                    float dPitch = std::fabs(pitch - sdDragPrePitch);
                    bool elemActive = ui::sketchMode().elementDragActive();
                    std::printf("sketch_dispatch[T-BL46]: slow-drag result: "
                                "elementDragActive=%d camera Δyaw=%.2f Δpitch=%.2f → %s\n",
                                (int)elemActive, dYaw, dPitch,
                                (dYaw < 0.5f && dPitch < 0.5f) ? "PASS(cam_still)"
                                                                : "WARN(cam_moved)");
                    std::fflush(stdout);
                }
                // Now inject fast drag: large jump (+60/+30) to stress-test the dead-zone path.
                // drawViewport reads MouseDelta from the CURRENT frame's injected pos;
                // we read the result on case 20 (next tick).
                float fastX = sdElemPtX + 60.f;
                float fastY = sdElemPtY + 30.f;
                ImGui::GetIO().AddMousePosEvent(fastX, fastY);
                std::printf("sketch_dispatch[T-BL46]: injecting fast-drag +60px/+30px → (%.0f,%.0f); "
                            "elementDragActive=%d; camera read on next tick\n",
                            fastX, fastY, (int)ui::sketchMode().elementDragActive());
                std::fflush(stdout);
                sdSettle = 1;  // let drawViewport process this large delta
                break;
            }
            case 20: {  // READ fast-drag result; release LMB; capture screenshot
                float yaw   = ui::viewportState().yaw;
                float pitch = ui::viewportState().pitch;
                float dYaw   = std::fabs(yaw   - sdDragPreYaw);
                float dPitch = std::fabs(pitch - sdDragPrePitch);
                bool elemActive = ui::sketchMode().elementDragActive();
                std::printf("sketch_dispatch[T-BL46]: fast-drag result: "
                            "elementDragActive=%d camera Δyaw=%.2f Δpitch=%.2f → %s\n",
                            (int)elemActive, dYaw, dPitch,
                            (dYaw < 0.5f && dPitch < 0.5f) ? "PASS(cam_still)"
                                                            : "WARN(cam_moved)");
                std::fflush(stdout);
                ImGui::GetIO().AddMouseButtonEvent(0, false);  // release LMB
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_08_elem_drag.png";
                sdSettle = kSettle;
                break;
            }
            case 21: {  // empty-space drag: record pre-drag camera state, position mouse
                // Record camera state BEFORE any empty-space drag. Settle 2 frames so the
                // overlay hover-test sees no entity at sdEmptyPt (elementDragActive stays false).
                sdEmptyPreYaw   = ui::viewportState().yaw;
                sdEmptyPrePitch = ui::viewportState().pitch;
                ImGui::GetIO().AddMousePosEvent(sdEmptyPtX, sdEmptyPtY);
                std::printf("sketch_dispatch[T-BL46]: empty-space hover start; "
                            "camera yaw=%.1f pitch=%.1f inSketch=%d elementDragActive=%d\n",
                            sdEmptyPreYaw, sdEmptyPrePitch, (int)st.inSketch,
                            (int)ui::sketchMode().elementDragActive());
                std::fflush(stdout);
                sdSettle = 2;  // hover settle — overlay hit-test must NOT arm element drag
                break;
            }
            case 22: {  // LMB down at empty-space pos — no element arm expected
                ImGui::GetIO().AddMousePosEvent(sdEmptyPtX, sdEmptyPtY);
                ImGui::GetIO().AddMouseButtonEvent(0, true);
                std::printf("sketch_dispatch[T-BL46]: LMB down at empty pos (%.0f,%.0f); "
                            "elementDragActive=%d (must be 0)\n",
                            sdEmptyPtX, sdEmptyPtY,
                            (int)ui::sketchMode().elementDragActive());
                std::fflush(stdout);
                sdSettle = 1;  // let button-down frame register
                break;
            }
            case 23: {  // inject large empty-space move — orbit fires in drawViewport this frame
                // The orbit call: g_glVp->orbit(-io.MouseDelta.x*0.01, io.MouseDelta.y*0.01)
                // runs in drawViewport AFTER this tick. We read the result on case 24.
                float emX = sdEmptyPtX + 80.f;   // large delta clears 8px dead-zone easily
                float emY = sdEmptyPtY + 60.f;
                ImGui::GetIO().AddMousePosEvent(emX, emY);
                std::printf("sketch_dispatch[T-BL46]: injecting empty-space move to (%.0f,%.0f); "
                            "orbit fires in drawViewport; read result on next tick\n", emX, emY);
                std::fflush(stdout);
                sdSettle = 1;  // let drawViewport orbit call update g_vpState
                break;
            }
            case 24: {  // READ empty-space result — Δ>0 proves view nav still works
                float yaw   = ui::viewportState().yaw;
                float pitch = ui::viewportState().pitch;
                float dYaw   = std::fabs(yaw   - sdEmptyPreYaw);
                float dPitch = std::fabs(pitch - sdEmptyPrePitch);
                bool elemActive = ui::sketchMode().elementDragActive();
                bool viewMoved  = (dYaw > 0.1f || dPitch > 0.1f);
                // sketchCapture=false when elementDragActive=0 → camera nav unblocked.
                // A non-zero orbit delta proves the 8px gate + sketchCapture gate both clear.
                std::printf("sketch_dispatch[T-BL46]: empty-space orbit result: "
                            "elementDragActive=%d camera Δyaw=%.2f Δpitch=%.2f → %s\n",
                            (int)elemActive, dYaw, dPitch,
                            viewMoved ? "PASS(cam_moved)" : "WARN(cam_still—imgHovered?)");
                std::fflush(stdout);
                ImGui::GetIO().AddMouseButtonEvent(0, false);
                sketchDispatchCapture = sdSavePath + "/sketch_dispatch_09_empty_drag.png";
                sdSettle = kSettle;
                // Exit sketch cleanly
                st.dispatch("sketch_exit");
                break;
            }
            default:
                cfg.sketchDispatchShotDone = true;
                std::printf("sketch_dispatch: scenario complete\n");
                std::fflush(stdout);
                break;
        }
        if (sdStep <= 24) ++sdStep;
    }

    // ---------- sketch_autohide (T-BL41: annotations hidden unless SmartDim armed) ----------
    // Proves the auto-hide DoD headlessly: build a genuinely CONSTRAINED sketch (rectangle with
    // horizontal/vertical + coincident relations AND a driving 80 mm distance dimension), enter it
    // via the real dispatch path (tool defaults to SketchTool::None), and capture two static states:
    //   (a) tool OFF  -> clean geometry, NO dimension/relation glyphs (auto-hidden)
    //   (b) SmartDim  -> ALL dimensions + relations revealed
    // Entry/drain timing mirrors sketch_dispatch: inSketch only flips on the NEXT frame's viewport
    // drain, and a rendered overlay PNG proves active_ became true via dispatch (double-gated).
    if (cfg.wantSketchAutohide && st.workerStarted && !cfg.sketchAutohideShotDone && cfg.frame >= 5) {
        if (!saDirReady) {
            saDirReady = true;
            saSavePath = repoBase() + "/scratch/sketch_autohide_shots";
            std::error_code ec; std::filesystem::create_directories(saSavePath, ec);
        }
        auto applyOne = [&](const nlohmann::json& op) {
            return st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
        };
        const int kSettle = 4;
        if (saSettle > 0) { --saSettle; return; }
        if (!sketchAutohideCapture.empty()) return;
        std::string err;
        switch (saStep) {
            case 0: {  // new part + sketch feature + constrained rectangle + a distance dimension
                if (!st.docMirror.newPart(st.worker, err)) {
                    std::printf("sketch_autohide: new_part FAILED: %s\n", err.c_str());
                    std::fflush(stdout);
                    cfg.sketchAutohideShotDone = true; break;
                }
                st.addDocTab(false);
                auto r = applyOne({{"op","add_feature"},{"feature",{
                    {"type","sketch"},{"name","Sketch1"},
                    {"refs",{{"plane","plane.top"}}},
                    {"params",{{"entities",nlohmann::json::array()},
                               {"constraints",nlohmann::json::array()}}}}}});
                saFid = (r.id_map.is_object() && r.id_map.contains("$0"))
                        ? r.id_map["$0"].get<std::string>() : "f1";
                // Rectangle with per-edge horizontal/vertical + chained coincident RELATIONS —
                // these are the geometric-relation glyphs the auto-hide must suppress by default.
                auto addLine = [&](double x1,double y1,double x2,double y2,bool horiz,
                                   const std::string& prev)->std::string{
                    nlohmann::json cs = nlohmann::json::array();
                    cs.push_back({{"type",horiz?"horizontal":"vertical"},{"refs",{"$self"}}});
                    if (!prev.empty())
                        cs.push_back({{"type","coincident"},{"refs",{prev+".p2","$self.p1"}}});
                    auto rr = applyOne({{"op","sketch_add_entity"},{"sketch",saFid},
                        {"entity",{{"type","line"},{"p1",{x1,y1}},{"p2",{x2,y2}}}},{"constraints",cs}});
                    return (rr.id_map.is_object() && rr.id_map.contains("$0"))
                           ? rr.id_map["$0"].get<std::string>() : "";
                };
                saL0 = addLine(0,0,80,0,true,"");
                saL1 = addLine(80,0,80,40,false,saL0);
                saL2 = addLine(80,40,0,40,true,saL1);
                saL3 = addLine(0,40,0,0,false,saL2);
                applyOne({{"op","sketch_add_constraint"},{"sketch",saFid},
                    {"constraint",{{"type","coincident"},{"refs",{saL3+".p2",saL0+".p1"}}}}});
                // A driving 80 mm distance DIMENSION on the bottom edge — the dimensional glyph the
                // auto-hide must suppress by default and SmartDim must reveal. Placement offsets it
                // clear of the geometry so it reads in the capture.
                applyOne({{"op","sketch_add_constraint"},{"sketch",saFid},
                    {"constraint",{{"type","distance"},{"refs",{saL0+".p1",saL0+".p2"}},
                                   {"value",{{"value",80.0},{"expr",nullptr}}},
                                   {"placement",nlohmann::json::array({40.0,-15.0})}}}});
                st.docMirror.resync(st.worker);
                std::printf("sketch_autohide: constrained rect + 80mm dim built (fid=%s)\n",
                            saFid.c_str());
                std::fflush(stdout);
                break;  // advance immediately to the dispatch step
            }
            case 1: {  // enter the EXISTING sketch we built (not a new empty one) via edit-fid path.
                // drain routes pendingSketchEditFid → enterForFeature (active_=true, tool_=None),
                // loading saFid's rectangle + dimension + relations. (dispatch("sketch_new") would
                // instead create a NEW empty sketch — wrong doc, nothing to show/hide.)
                st.pendingSketchEditFid = saFid;
                std::printf("sketch_autohide: armed pendingSketchEditFid=%s; awaiting drain\n",
                            saFid.c_str());
                std::fflush(stdout);
                saSettle = kSettle;
                break;
            }
            case 2: {  // drain ran: inSketch true, tool=None -> capture the AUTO-HIDDEN state
                std::printf("sketch_autohide: after drain inSketch=%d tool=None (glyphs HIDDEN)\n",
                            (int)st.inSketch);
                std::fflush(stdout);
                sketchAutohideCapture = saSavePath + "/sketch_autohide_01_tooloff.png";
                saSettle = kSettle;
                break;
            }
            case 3: {  // arm Smart Dimension via dispatch -> ALL dims + relations revealed
                st.dispatch("smart_dim");  // routes onRibbonCommand → tool_ = SketchTool::SmartDim
                std::printf("sketch_autohide: dispatched smart_dim (SmartDim armed → glyphs SHOWN)\n");
                std::fflush(stdout);
                sketchAutohideCapture = saSavePath + "/sketch_autohide_02_toolon.png";
                saSettle = kSettle;
                break;
            }
            default:
                cfg.sketchAutohideShotDone = true;
                std::printf("sketch_autohide: scenario complete\n");
                std::fflush(stdout);
                break;
        }
        if (saStep <= 3) ++saStep;
    }

    // ---------- sketch_convert (T-BL43 Convert Entities: external face loop -> in-sketch) ----
    // Builds a 40x40x20 box, opens an EMPTY sketch on its top face, then fires the exact
    // `sketch_convert` op the GUI Convert tool emits (host + ui/imgui applyConvertRef) with
    // the BOTTOM face as the external ref — the projected loop (4 real, fix-pinned lines)
    // renders in the open sketch and is captured. NOT part of the gui_parity `sketch` diff
    // set (the host scenario has no convert step). Light vs dark: the --light CLI flag.
    if (cfg.wantSketchConvert && st.workerStarted && !cfg.sketchConvertShotDone &&
        cfg.frame >= 5) {
        if (!scvDirReady) {
            scvDirReady = true;
            scvSavePath = repoBase() + "/scratch/sketch_convert_shots";
            std::error_code ec; std::filesystem::create_directories(scvSavePath, ec);
        }
        const int kSettle = 4;
        if (scvSettle > 0) { --scvSettle; return; }
        if (!scvCapture.empty()) return;
        std::string err;
        switch (scvStep) {
            case 0: {  // box -> empty sketch ON its top face -> sketch_convert(bottom) -> enter
                if (!st.docMirror.newPart(st.worker, err)) {
                    std::printf("sketch_convert: new_part FAILED: %s\n", err.c_str());
                    std::fflush(stdout);
                    cfg.sketchConvertShotDone = true; break;
                }
                st.addDocTab(false);
                nlohmann::json ents = nlohmann::json::array({
                    {{"id","e1"},{"type","line"},{"construction",false},{"p1",{0.0,0.0}},{"p2",{40.0,0.0}}},
                    {{"id","e2"},{"type","line"},{"construction",false},{"p1",{40.0,0.0}},{"p2",{40.0,40.0}}},
                    {{"id","e3"},{"type","line"},{"construction",false},{"p1",{40.0,40.0}},{"p2",{0.0,40.0}}},
                    {{"id","e4"},{"type","line"},{"construction",false},{"p1",{0.0,40.0}},{"p2",{0.0,0.0}}}});
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{{"type","sketch"},{"name","Base"},
                        {"refs",{{"plane","plane.top"}}},
                        {"params",{{"entities",ents},{"constraints",nlohmann::json::array()}}}}}},
                    {{"op","add_feature"},{"feature",{{"type","extrude"},{"name","Box"},
                        {"params",{{"mode","boss"},{"end","blind"},
                                   {"depth",{{"value",20.0},{"expr",nullptr}}}}},
                        {"refs",{{"profile",nlohmann::json::array({{{"kind","region"},
                            {"sketch","$0"},{"centroid",{20.0,20.0}},{"hint_index",0}}})}}}}}}}));
                if (!r.ok) {
                    std::printf("sketch_convert: box build FAILED\n"); std::fflush(stdout);
                    cfg.sketchConvertShotDone = true; break;
                }
                loadMesh(st, r.aicm_path);
                // Resolver-owned selectors: top face = the sketch plane, bottom face = the
                // EXTERNAL reference Convert Entities projects.
                nlohmann::json qt, qb;
                st.worker.call("doc_query", {{"doc_id",st.docMirror.docId()},
                    {"query",{{"of","face"},{"side","top"}}}}, qt, err);
                st.worker.call("doc_query", {{"doc_id",st.docMirror.docId()},
                    {"query",{{"of","face"},{"side","bottom"}}}}, qb, err);
                if (!qt.contains("matches") || qt["matches"].empty() ||
                    !qb.contains("matches") || qb["matches"].empty()) {
                    std::printf("sketch_convert: face query FAILED\n"); std::fflush(stdout);
                    cfg.sketchConvertShotDone = true; break;
                }
                auto r2 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{{"type","sketch"},{"name","SkTop"},
                        {"refs",{{"plane",qt["matches"][0]}}},
                        {"params",{{"entities",nlohmann::json::array()},
                                   {"constraints",nlohmann::json::array()}}}}}}}));
                scvFid = (r2.id_map.is_object() && r2.id_map.contains("$0"))
                         ? r2.id_map["$0"].get<std::string>() : "f3";
                // THE op the Convert Entities tool emits — T-BL43 implemented it in the engine.
                auto r3 = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","sketch_convert"},{"sketch",scvFid},
                     {"refs",nlohmann::json::array({{{"kind","face"},
                                                     {"selector",qb["matches"][0]}}})}}}));
                std::printf("sketch_convert: convert op %s (sketch=%s)\n",
                            r3.ok ? "OK" : "FAILED", scvFid.c_str());
                nlohmann::json eres;
                st.worker.call("sketch_enter",
                    {{"doc_id", st.docMirror.docId()}, {"sketch", scvFid}}, eres, err);
                st.inSketch = true;
                std::fflush(stdout);
                scvCapture = scvSavePath + "/sketch_convert_01_insketch.png";
                scvSettle = kSettle;
                break;
            }
            default:
                cfg.sketchConvertShotDone = true;
                std::printf("sketch_convert: scenario complete\n");
                break;
        }
        if (scvStep < 1) ++scvStep;
    }

    // ---------- fea (host-parity SimManager study flow) ----------
    // T-G9 harness finding: the old flow here bypassed the SimManager entirely (raw
    // sim_run_static with empty fixtures/loads and a worktree-relative STEP netgen could
    // not read), while host drives the panel: sim_new_static + configureActive +
    // pick-resolved fixture/load faces + sim_run. Faithful port of host's scenario.
    if (cfg.wantFea && st.workerStarted && !cfg.feaShotDone) {
        ui::simManager().tick(st);  // pump so completion lands while we wait
        if (feaSettle > 0) { --feaSettle; return; }
        switch (feaStep) {
            case 0: {  // New part + box
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},{"params",{
                        {"length",dim(60)},{"width",dim(40)},{"height",dim(10)}}},
                    {"centered",true}}}});
                auto r = st.docMirror.applyOps(st.worker, ops);
                loadMesh(st, r.aicm_path);
                std::printf("fea: box ok=%d rev=%d\n", (int)r.ok, r.revision);
                std::fflush(stdout);
                st.workspace = cmd::Workspace::Simulate;
                feaSettle = 3; ++feaStep; break;
            }
            case 1: {  // Static study + resolved fixture/load faces (-X end / +X end)
                ui::simManager().handleCommand(st, "sim_new_static");
                ui::simManager().configureActive(st, 0.2f, "steel_1018");
                auto faceAtExtremeX = [&](bool wantMax) -> nlohmann::json {
                    nlohmann::json best; double bestC = wantMax ? -1e30 : 1e30;
                    for (int tid = 1; tid <= 12; ++tid) {
                        std::string se;
                        std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                        if (s.empty()) continue;
                        try {
                            auto o = nlohmann::json::parse(s);
                            if (!o.contains("centroid")) continue;
                            double cx = o["centroid"][0].get<double>();
                            if ((wantMax && cx > bestC) || (!wantMax && cx < bestC)) { bestC = cx; best = o; }
                        } catch (...) {}
                    }
                    return best;
                };
                nlohmann::json fixFace = faceAtExtremeX(false);
                nlohmann::json loadFace = faceAtExtremeX(true);
                // Inject the resolved selectors via the selection -> well capture path.
                st.selection.clear();
                { core::SelItem it; it.selector = fixFace.dump(); it.pending_resolve = false;
                  st.selection.mutableItems().push_back(it); }
                ui::simManager().handleCommand(st, "sim_fixture");
                st.selection.clear();
                { core::SelItem it; it.selector = loadFace.dump(); it.pending_resolve = false;
                  st.selection.mutableItems().push_back(it); }
                ui::simManager().handleCommand(st, "sim_load_force");
                st.selection.clear();
                std::printf("fea: faces fixCx=%.1f loadCx=%.1f\n",
                            fixFace.contains("centroid") ? fixFace["centroid"][0].get<double>() : 0.0,
                            loadFace.contains("centroid") ? loadFace["centroid"][0].get<double>() : 0.0);
                std::fflush(stdout);
                feaSettle = 2; ++feaStep; break;
            }
            case 2: {  // Run
                ui::simManager().handleCommand(st, "sim_run");
                std::printf("fea: study launched (density 0.4, steel, 100 N)\n");
                std::fflush(stdout);
                feaWaitFrames = 0; ++feaStep; break;
            }
            case 3: {  // Wait for completion (cap ~90 s)
                ++feaWaitFrames;
                if (st.simClient.job().ready.load() || feaWaitFrames > 5400) {
                    cfg.colormapOn = st.simClient.job().ok;
                    cfg.feaShotDone = true;
                    std::printf("fea: done ok=%d frames=%d\n",
                                (int)st.simClient.job().ok, feaWaitFrames);
                    std::fflush(stdout);
                }
                break;
            }
            default: cfg.feaShotDone = true; break;
        }
    }

    // ---------- fea_overlay (T-SIM2): render a precomputed FEA result contour ----------
    // Netgen (the live tet mesher) is not provisionable in this environment, so the fully-live
    // meshed solve cannot run here. This scenario instead loads a result AICM produced offline
    // by the REAL fea_static_solve + static_overlay (surface skinning + nodal-field mapping +
    // the T-SIM2 size cap) and drives the SAME viewport contour path the app uses at runtime
    // (viewportLoadResultAicm -> C19 colormap LUT + deformed shape + legend). No worker needed.
    if (cfg.wantFeaOverlay && !cfg.feaOverlayShotDone) {
        // Re-assert the result overlay EVERY frame until capture. The viewport's per-frame
        // ensureDocLoaded() reloads the editable model mesh whenever the engine revision moves
        // (tab/doc setup during startup), which would clobber a one-shot overlay load — the real
        // app avoids this by re-applying the overlay each frame from syncResultView() while the
        // Results node is active. We mirror that here, and pin viewMeshRev to a sentinel so
        // ensureDocLoaded() does not fight us on the settle frames.
        const std::string aicm = repoBase() + "/contracts/fixtures/sim2_cantilever_result.aicm";
        float vmin = 0.f, vmax = 1.f, dispHint = 1.f;
        const bool ok = viewportLoadResultAicm(aicm, "von_mises", vmin, vmax, dispHint);
        if (ok) {
            // Make ensureDocLoaded() return early (viewMeshRev == revision && hasMesh()) so it does
            // NOT re-derive the editable model mesh over our just-loaded result overlay.
            st.viewMeshRev = st.engine.revision();
            auto& vs = viewportState();
            vs.legendOn    = true;
            vs.legendTitle = "von Mises";
            vs.legendUnits = "MPa";
            vs.legendMin   = vmin;
            vs.legendMax   = vmax;
            if (dispHint > 0.f) viewportSetDeformScale(dispHint);  // SolidWorks-style deformed shape
            st.workspace = cmd::Workspace::Simulate;
        }
        if (cfg.feaOverlaySettle == 0) {
            std::printf("fea_overlay: load ok=%d vmin=%.3g vmax=%.3g dispHint=%.3g (%s)\n",
                        (int)ok, vmin, vmax, dispHint, aicm.c_str());
            std::fflush(stdout);
            cfg.feaOverlaySettle = 8;  // hold a few frames so the colormap upload + camera frame settle
        } else if (--cfg.feaOverlaySettle == 0) {
            cfg.feaOverlayShotDone = true;
        }
    }

    // ---------- T-SIM3: sim UI overhaul shots (material dialog / precheck card / postcheck) ----
    // Shared doc+study setup mirrors the fea scenario. No live solve is attempted (Netgen is
    // not provisionable headless — see fea_overlay above); the postcheck shot seeds the result
    // NUMBERS as a fixture via SimManager::debugSeedStaticResult, while the advisor
    // precheck/postcheck calls themselves run the real python sim_advisor via the CLI shim.
    if ((cfg.wantSimMaterial || cfg.wantSimPrecheck || cfg.wantSimPostcheck) && st.workerStarted &&
        !cfg.simMaterialShotDone && !cfg.simPrecheckShotDone && !cfg.simPostcheckShotDone) {
        if (simUiSettle > 0) { --simUiSettle; return; }
        switch (simUiStep) {
            case 0: {  // New part + box, Simulate workspace
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                nlohmann::json ops = nlohmann::json::array();
                ops.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Box1"},{"params",{
                        {"length",dim(60)},{"width",dim(40)},{"height",dim(10)}}},
                    {"centered",true}}}});
                auto r = st.docMirror.applyOps(st.worker, ops);
                loadMesh(st, r.aicm_path);
                st.workspace = cmd::Workspace::Simulate;
                std::printf("sim_ui: box ok=%d rev=%d\n", (int)r.ok, r.revision);
                std::fflush(stdout);
                simUiSettle = 3; ++simUiStep; break;
            }
            case 1: {  // Static study (+ resolved BC faces for the postcheck variant)
                ui::simManager().handleCommand(st, "sim_new_static");
                // sim_precheck stays UNCONSTRAINED (no fixtures/loads) with a very coarse
                // mesh so the precheck emits its BC findings + the mesh suggestion.
                ui::simManager().configureActive(st, cfg.wantSimPrecheck ? 0.15f : 0.4f,
                                                 "steel_1018");
                if (cfg.wantSimPostcheck) {
                    auto faceAtExtremeX = [&](bool wantMax) -> nlohmann::json {
                        nlohmann::json best; double bestC = wantMax ? -1e30 : 1e30;
                        for (int tid = 1; tid <= 12; ++tid) {
                            std::string se;
                            std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                            if (s.empty()) continue;
                            try {
                                auto o = nlohmann::json::parse(s);
                                if (!o.contains("centroid")) continue;
                                double cx = o["centroid"][0].get<double>();
                                if ((wantMax && cx > bestC) || (!wantMax && cx < bestC)) { bestC = cx; best = o; }
                            } catch (...) {}
                        }
                        return best;
                    };
                    nlohmann::json fixFace = faceAtExtremeX(false);
                    nlohmann::json loadFace = faceAtExtremeX(true);
                    st.selection.clear();
                    { core::SelItem it; it.selector = fixFace.dump(); it.pending_resolve = false;
                      st.selection.mutableItems().push_back(it); }
                    ui::simManager().handleCommand(st, "sim_fixture");
                    st.selection.clear();
                    { core::SelItem it; it.selector = loadFace.dump(); it.pending_resolve = false;
                      st.selection.mutableItems().push_back(it); }
                    ui::simManager().handleCommand(st, "sim_load_force");
                    st.selection.clear();
                }
                simUiSettle = 2; ++simUiStep; break;
            }
            case 2: {  // Drive the surface under test
                if (cfg.wantSimMaterial) {
                    ui::simManager().handleCommand(st, "sim_material");  // navigates + opens dialog
                    std::printf("sim_ui: material dialog opened\n");
                } else if (cfg.wantSimPrecheck) {
                    // Run on the unconstrained study: precheck card populates; the static
                    // dispatch then aborts on its own no-fixture guard (advise-never-veto).
                    ui::simManager().handleCommand(st, "sim_run");
                    std::printf("sim_ui: precheck run issued\n");
                } else {
                    // steel_1018 yield 370 / ultimate 440 MPa; 300 x SF1.5 = 450 > 440 -> FAIL,
                    // exercising the loud-but-not-modal verdict styling.
                    ui::simManager().debugSeedStaticResult(st, 300.0, 0.42, 1.23);
                    std::printf("sim_ui: postcheck seeded (vM=300 MPa on steel_1018)\n");
                }
                std::fflush(stdout);
                simUiSettle = 12; ++simUiStep; break;
            }
            default:
                if (cfg.wantSimMaterial)  cfg.simMaterialShotDone = true;
                if (cfg.wantSimPrecheck)  cfg.simPrecheckShotDone = true;
                if (cfg.wantSimPostcheck) cfg.simPostcheckShotDone = true;
                std::printf("sim_ui: scenario complete (%s)\n", cfg.name ? cfg.name : "?");
                std::fflush(stdout);
                break;
        }
    }

    // ---------- flow (240 s cap at 60 Hz) ----------
    if (cfg.wantFlow && st.workerStarted && !cfg.flowShotDone) {
        if (flowSettle > 0) { --flowSettle; return; }
        switch (flowStep) {
            case 0: {
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Hull"},
                        {"params",{{"length",dim(80)},{"width",dim(20)},{"height",dim(20)}},
                                   {"centered",true}}}}}}));
                loadMesh(st, r.aicm_path);
                st.workspace = cmd::Workspace::Simulate;
                flowSettle = 3; ++flowStep; break;
            }
            case 1: {
                std::string stlPath = repoBase() + "/scratch/flow_hull.stl";
                nlohmann::json ex; std::string e;
                st.worker.call("doc_export_step",
                    {{"doc_id", st.docMirror.docId()}, {"path", stlPath}}, ex, e, 60000);
                nlohmann::json params = {
                    {"stl_path", stlPath},
                    {"direction", {1, 0, 0}},
                    {"speed_mps", 10.0}, {"preset", "coarse"},
                    {"aoa_deg", 10.0}, {"sideslip_deg", 0.0},
                    {"fluid", {{"name", "water"}, {"density_kgm3", 998.0},
                              {"kinematic_visc_m2s", 1.0e-6}}},
                    {"solver", "lbm"}};
                std::string note;
                st.simClient.ensureStarted(note);
                st.simClient.launch("sim_run_flow", params, note, st.docMirror.docId());
                flowSettle = 2; ++flowStep; break;
            }
            case 2: {
                ++flowWaitFrames;
                // 240 s @ 60 Hz
                if (st.simClient.job().ready.load() || flowWaitFrames > 14400) {
                    cfg.colormapOn = st.simClient.job().ok;
                    cfg.flowShotDone = true;
                    std::printf("flow: done ok=%d frames=%d\n",
                                (int)st.simClient.job().ok, flowWaitFrames);
                }
                break;
            }
            default: cfg.flowShotDone = true; break;
        }
    }

    // ---------- cam ----------
    // T-G13 (issue #7): cam used to do all its work in one tick and set camShotDone immediately,
    // but it has no wantCam branch in endFrame() and captureFrameBudget() is 999999 for every
    // isScenarioMode() scenario — so the generic budget path never fired and cam could NEVER
    // write a PNG (it printed cam-shot: ok=1 and then spun to the outer timeout, even worker-
    // present). Make it a tiny settle-based one-shot like sketch: build + loadMesh the plate,
    // settle a few frames so the mesh renders, THEN set camShotDone. endFrame's new wantCam
    // branch captures on camShotDone.
    if (cfg.wantCam && st.workerStarted && !cfg.camShotDone && cfg.frame >= 5) {
        if (camSettle > 0) { --camSettle; return; }
        switch (camStep) {
            case 0: {
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                // Byte-parity with the host cam recipe (T-G9 harness finding: the old
                // init-list here misnested {"centered",true} INSIDE the params braces —
                // nlohmann silently turned the whole feature into an array of pairs — and
                // dropped host's Bores hole op entirely, so the CAM job ran on a different
                // part than host's).
                nlohmann::json camOps = nlohmann::json::array();
                camOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Plate"},{"params",{
                        {"length",dim(80)},{"width",dim(60)},{"height",dim(15)}}},
                    {"centered",true}}}});
                {
                    nlohmann::json hole;
                    hole["op"] = "add_feature";
                    hole["feature"]["type"] = "hole";
                    hole["feature"]["name"] = "Bores";
                    hole["feature"]["params"]["style"] = "simple";
                    hole["feature"]["params"]["standard"] = "custom";
                    hole["feature"]["params"]["diameter"] = dim(5);
                    hole["feature"]["params"]["end"] = "through_all";
                    hole["feature"]["params"]["positions"] = {{-30, -20}, {30, -20}, {30, 20}, {-30, 20}};
                    hole["feature"]["refs"]["face"] = {{"of", "face"}, {"side", "top"}};
                    camOps.push_back(hole);
                }
                auto r = st.docMirror.applyOps(st.worker, camOps);
                loadMesh(st, r.aicm_path);
                nlohmann::json res; std::string e2;
                st.worker.call("cam_program",
                    {{"doc_id", st.docMirror.docId()}, {"post", "fanuc"}}, res, e2);
                st.workspace = cmd::Workspace::Model;
                st.camActive = true;   // surface CAM panel via viewport content-swap (H1 fix)
                // Adopt the job into the Manufacture panel exactly like host does (T-G9
                // harness finding: without this the panel showed "No setups yet" and the
                // cam_tree/cam_generate RPCs host fires were missing from the stream).
                {
                    std::string camId = res.is_object()
                                        ? res.value("cam_doc_id", std::string()) : std::string();
                    if (!camId.empty()) ui::camLoadJob(st, camId);
                }
                std::printf("cam-shot: ok=%d\n", (int)!res.contains("code"));
                std::fflush(stdout);
                camSettle = 4; ++camStep; break;
            }
            default:
                cfg.camShotDone = true;
                std::printf("cam: scenario complete\n");
                std::fflush(stdout);
                break;
        }
    }

    // ---------- evalmass ----------
    if (cfg.wantEvalMass && st.workerStarted && !cfg.evalMassShotDone) {
        if (evalSettle > 0) { --evalSettle; return; }
        switch (evalStep) {
            case 0: {
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},
                        {"params",{{"length",dim(50)},{"width",dim(30)},{"height",dim(20)}},
                                   {"centered",true}}}}}}));
                loadMesh(st, r.aicm_path);
                evalSettle = 3; ++evalStep; break;
            }
            case 1: {
                st.rpc.call("eval_mass",
                    {{"doc_id", st.docMirror.docId()}, {"density_g_cm3", 7.85}},
                    [&cfg](const core::RpcResult& r) {
                        (void)r;
                        cfg.evalMassShotDone = true;
                    });
                ++evalStep; break;
            }
            default: cfg.evalMassShotDone = true; break;
        }
    }

    // ---------- activity ----------
    if (cfg.wantActivity && st.workerStarted && !cfg.activityShotDone && cfg.frame >= 5) {
        if (!actDirReady) {
            actDirReady = true;
            actSavePath = repoBase() + "/scratch/activity_shots";
            std::error_code ec; std::filesystem::create_directories(actSavePath, ec);
        }
        const int kSettle = 4;
        if (actSettle > 0) { --actSettle; return; }
        if (!actCapture.empty()) return;
        switch (actStep) {
            case 0: {
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                actSettle = kSettle; ++actStep; break;
            }
            case 1: {
                launchAgentRunV2(st.worker, st.job,
                    "a bracket with a filleted top edge", st.docMirror.docId());
                st.agent.running = true;
                actWaitFrames = 0; ++actStep; break;
            }
            case 2: {
                ++actWaitFrames;
                if (!st.job.running.load() || actWaitFrames > 1800) {
                    actSettle = kSettle * 2; ++actStep;
                }
                break;
            }
            case 3:
                actCapture = actSavePath + "/activity_01_run.png";
                actSettle = kSettle; ++actStep; break;
            case 4:
                settingsUi().openSettings = true;
                settingsUi().openSettingsTab = 2;  // T-G22: tab 2 = Prompts (pendingPromptRole drives jumpToAgents)
                settingsUi().pendingPromptRole = "modeler";
                actSettle = kSettle * 2; ++actStep; break;
            case 5:
                actCapture = actSavePath + "/activity_02_agents.png";
                actSettle = kSettle; ++actStep; break;
            default:
                cfg.activityShotDone = true;
                break;
        }
    }

    // ---------- asm (simplified: author parts + assembly + mates via worker) ----------
    if (cfg.wantAsm && st.workerStarted && !cfg.asmShotDone && cfg.frame >= 5) {
        if (!asmDirReady) {
            asmDirReady = true;
            asmSavePath = repoBase() + "/scratch/asm_shots";
            asmBasePath = asmSavePath + "/base.step+";
            asmPegPath  = asmSavePath + "/peg.step+";
            std::error_code ec; std::filesystem::create_directories(asmSavePath, ec);
        }
        auto makePart = [&](const std::string& name, const nlohmann::json& ops,
                            const std::string& path) -> bool {
            nlohmann::json res; std::string err;
            if (!st.worker.call("doc_new", {{"kind", "part"}, {"name", name}}, res, err)) return false;
            std::string did = res.value("doc_id", std::string());
            int rev = res.value("revision", 0);
            nlohmann::json ap2;
            if (!st.worker.call("doc_apply_ops",
                    {{"doc_id", did}, {"base_revision", rev}, {"ops", ops}}, ap2, err)) return false;
            nlohmann::json sv;
            if (!st.worker.call("doc_save", {{"doc_id", did}, {"path", path}}, sv, err)) return false;
            nlohmann::json cl;
            st.worker.call("doc_close", {{"doc_id", did}}, cl, err);
            return true;
        };
        const int kSettle = 4;
        if (asmSettle > 0) { --asmSettle; return; }
        if (!asmCapture.empty()) return;
        switch (asmStep) {
            case 0: {
                nlohmann::json baseOps = nlohmann::json::array();
                baseOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Plate"},
                    {"params",{{"length",dim(40)},{"width",dim(40)},{"height",dim(10)}},
                               {"centered",true}}}}});
                baseOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","cylinder"},{"name","Hole"},
                    {"params",{{"radius",dim(4)},{"height",dim(12)},{"centered",true},
                               {"mode","cut"}}}}}});
                nlohmann::json pegOps = nlohmann::json::array();
                pegOps.push_back({{"op","add_feature"},{"feature",{
                    {"type","cylinder"},{"name","Peg"},
                    {"params",{{"radius",dim(3.9)},{"height",dim(20)},{"centered",true}}}}}});
                makePart("base", baseOps, asmBasePath);
                makePart("peg", pegOps, asmPegPath);
                asmSettle = 2; ++asmStep; break;
            }
            case 1: {
                std::string err;
                st.docMirror.newAssembly(st.worker, err);
                st.addDocTab(true);
                asmDoc = st.docMirror.docId();
                st.workspace = cmd::Workspace::Assemble;
                asmSettle = kSettle; ++asmStep; break;
            }
            case 2:
                asmCapture = asmSavePath + "/asm_01_seated.png";
                asmSettle = kSettle; ++asmStep; break;
            default:
                cfg.asmShotDone = true;
                std::printf("asm: scenario complete\n");
                break;
        }
    }

    runMultidoc(cfg, st);
    runDrawing(cfg, st);
    runIconPipe(cfg, st);
}

void ScenarioRunner::runMultidoc(ShotConfig& cfg, app::AppState& st) {
    if (!(cfg.wantMultidoc && st.workerStarted && !cfg.multidocShotDone)) return;
    if (!mdDirReady) {
        mdDirReady = true;
        mdSavePath = repoBase() + "/scratch/multidoc_shots";
        std::error_code ec; std::filesystem::create_directories(mdSavePath, ec);
    }
    auto buildBox = [&]() {
        return st.docMirror.applyOps(st.worker, nlohmann::json::array({
            {{"op","add_feature"},{"feature",{
                {"type","box"},{"name","Box1"},
                {"params",{{"length",dim(60)},{"width",dim(40)},{"height",dim(10)}},
                           {"centered",true}}}}}}));
    };
    const int kSettle = 4;
    if (mdSettle > 0) { --mdSettle; return; }
    if (!mdCapture.empty()) return;
    switch (mdStep) {
        case 0: {
            std::string err;
            st.docMirror.newPart(st.worker, err);
            st.addDocTab(false);
            buildBox();
            mdIdxA = st.activeTab;
            mdDocA = st.docMirror.docId();
            mdSettle = kSettle; ++mdStep; break;
        }
        case 1: {
            st.activeTool = "fillet";
            mdCapture = mdSavePath + "/multidoc_01_A_editing.png";
            mdSettle = kSettle; ++mdStep; break;
        }
        case 2: {
            std::string err;
            st.docMirror.newPart(st.worker, err);
            st.addDocTab(false);
            mdPassBFresh = st.activeTool.empty();
            buildBox();
            mdIdxB = st.activeTab;
            mdDocB = st.docMirror.docId();
            mdCapture = mdSavePath + "/multidoc_02_B.png";
            mdSettle = kSettle; ++mdStep; break;
        }
        case 3: {
            st.switchToTab(mdIdxA);
            mdPassPm = !st.activeTool.empty();
            mdPassDoc = (st.docMirror.docId() == mdDocA);
            mdCapture = mdSavePath + "/multidoc_03_A_restored.png";
            mdSettle = kSettle; ++mdStep; break;
        }
        case 4: {
            st.switchToTab(mdIdxB);
            mdPassBIndep = st.activeTool.empty();
            mdCapture = mdSavePath + "/multidoc_04_B_independent.png";
            mdSettle = kSettle; ++mdStep; break;
        }
        default:
            cfg.multidocShotDone = true;
            std::printf("multidoc: scenario complete\n");
            break;
    }
}

void ScenarioRunner::runDrawing(ShotConfig& cfg, app::AppState& st) {
    if (!(cfg.wantDrawing && st.workerStarted && !cfg.drawingShotDone && cfg.frame >= 5)) return;
    if (!dwgDirReady) {
        dwgDirReady = true;
        dwgSavePath = repoBase() + "/scratch/drawing_shots";
        std::error_code ec; std::filesystem::create_directories(dwgSavePath, ec);
    }
    const int kSettle = 5;
    if (dwgSettle > 0) { --dwgSettle; return; }
    if (!dwgCapture.empty()) return;
    auto vid_of = [](const nlohmann::json& v) -> std::string {
        std::string id = v.value("view_id", std::string());
        return id.empty() ? v.value("id", std::string()) : id;
    };
    std::string err;
    switch (dwgStep) {
        case 0: {  // new part + plate (80x50x12) with a central through hole — host recipe,
                   // object-form features (T-G9 harness finding: the old init-list misnested
                   // {"centered",true} inside the params braces -> array-of-pairs on the wire)
            if (!st.docMirror.newPart(st.worker, err)) {
                std::printf("drawing: new_part FAILED: %s\n", err.c_str()); std::fflush(stdout);
                cfg.drawingShotDone = true; break;
            }
            st.addDocTab(false);
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op","add_feature"},{"feature",{
                {"type","box"},{"name","Plate"},{"params",{
                    {"length",dim(80)},{"width",dim(50)},{"height",dim(12)},
                    {"centered",true}}}}}});
            ops.push_back({{"op","add_feature"},{"feature",{
                {"type","cylinder"},{"name","Hole"},{"params",{
                    {"radius",dim(6)},{"height",dim(16)},
                    {"centered",true},{"mode","cut"}}}}}});
            auto r = st.docMirror.applyOps(st.worker, ops);
            loadMesh(st, r.aicm_path);
            std::printf("drawing: plate+hole ok=%d rev=%d\n", (int)r.ok, r.revision);
            std::fflush(stdout);
            // Now enter Drafting: the panel self-creates the drawing from the ACTIVE (Plate)
            // doc on next render — host's makeDrawing RPC sequence, right source doc.
            st.workspace = cmd::Workspace::Drafting;
            dwgSettle = kSettle; ++dwgStep; break;
        }
        case 1: {  // Detailing (host case 2): model items + hole callout + critical toleranced
                   // dim + title block + the copilot drawing_make one-shot.
            const std::string dwgId = ui::drawingActiveDocId();
            if (dwgId.empty()) {
                std::printf("drawing: NO self-created drawing yet\n"); std::fflush(stdout);
                cfg.drawingShotDone = true; break;
            }
            nlohmann::json dwgDoc = ui::drawingActiveDoc();
            // Find a view with a closed projected circle (the hole in the top view).
            std::string circVid; double cx = 0, cy = 0; bool haveCirc = false;
            if (dwgDoc.contains("sheets") && dwgDoc["sheets"].is_array() &&
                !dwgDoc["sheets"].empty()) {
                for (const auto& v : dwgDoc["sheets"][0].value("views", nlohmann::json::array())) {
                    for (const auto& a : v.value("arcs", nlohmann::json::array()))
                        if (a.value("closed", false) && a.contains("center") &&
                            a["center"].is_array() && a["center"].size() == 2) {
                            circVid = vid_of(v);
                            cx = a["center"][0].get<double>(); cy = a["center"][1].get<double>();
                            haveCirc = true; break;
                        }
                    if (haveCirc) break;
                }
            }
            std::string mvid = circVid;
            if (mvid.empty() && dwgDoc.contains("sheets")) {
                const auto& vs = dwgDoc["sheets"][0].value("views", nlohmann::json::array());
                if (vs.is_array() && !vs.empty()) mvid = vid_of(vs[0]);
            }
            nlohmann::json res;
            if (!mvid.empty())
                st.worker.call("drawing_model_items",
                               {{"doc_id", dwgId}, {"view_id", mvid}}, res, err);
            if (haveCirc) {
                nlohmann::json r2;
                st.worker.call("drawing_add_callout",
                               {{"doc_id", dwgId}, {"view_id", circVid}, {"kind", "hole"},
                                {"a", nlohmann::json::array({cx, cy})}}, r2, err);
            }
            nlohmann::json r3;
            st.worker.call("drawing_set_titleblock",
                           {{"doc_id", dwgId}, {"sheet_id", "s1"},
                            {"fields", {{"title", "PLATE"}, {"drawn_by", "AI-CAD"},
                                        {"material", "AL 6061"}, {"scale", "1:1"}, {"rev", "A"}}}},
                           r3, err);
            ui::drawingRefreshActive(st);
            // Make the first linear dim a critical +/-0.1 tolerance.
            dwgDoc = ui::drawingActiveDoc();
            std::string tvid, tann;
            if (dwgDoc.contains("sheets"))
                for (const auto& v : dwgDoc["sheets"][0].value("views", nlohmann::json::array())) {
                    for (const auto& a : v.value("annotations", nlohmann::json::array()))
                        if (a.value("type", std::string()) == "linear_dim") {
                            tvid = vid_of(v); tann = a.value("id", std::string()); break;
                        }
                    if (!tann.empty()) break;
                }
            if (!tann.empty()) {
                nlohmann::json r4;
                st.worker.call("drawing_set_annotation",
                               {{"doc_id", dwgId}, {"view_id", tvid}, {"ann_id", tann},
                                {"tol_type", "symmetric"}, {"tol_upper", 0.1}, {"precision", 2},
                                {"critical", true}}, r4, err);
            }
            ui::drawingRefreshActive(st);
            int nann = 0;
            dwgDoc = ui::drawingActiveDoc();
            if (dwgDoc.contains("sheets"))
                for (const auto& v : dwgDoc["sheets"][0].value("views", nlohmann::json::array()))
                    nann += (int)v.value("annotations", nlohmann::json::array()).size();
            std::printf("drawing: detail done circle=%d critical_dim=%s annotations=%d\n",
                        (int)haveCirc, tann.empty() ? "none" : tann.c_str(), nann);
            std::fflush(stdout);
            // Copilot one-shot path: agent-grade drawing_make from the source part.
            {
                nlohmann::json r5;
                nlohmann::json spec = {
                    {"views", nlohmann::json::array({"front", "top", "right", "iso"})},
                    {"model_items", true}, {"hole_callouts", true},
                    {"title", {{"title", "COPILOT"}, {"material", "STEEL"}, {"rev", "A"}}}};
                st.worker.call("drawing_make",
                               {{"source_doc_id", st.docMirror.docId()}, {"spec", spec}}, r5, err);
            }
            dwgSettle = kSettle; ++dwgStep; break;
        }
        case 2:
            dwgCapture = dwgSavePath + "/drawing_01_detailed.png";
            dwgSettle = kSettle; ++dwgStep; break;
        default:
            cfg.drawingShotDone = true;
            break;
    }
}

// T-G25 icon pipeline (AI_CAD_SHOT=iconpipe). Ported from host/src/ui/scenarios.cpp
// ScenarioRunner::runIconPipe. Recipe-driven clean-viewport icon generator: reads a JSON recipe
// list from AI_CAD_ICON_RECIPES { out_dir?, px?, recipes:[{name,yaw,pitch,steps:[...]}] } and, one
// recipe per pass, builds the demo part (docMirror.newPart + applyOps — the SAME engine contract
// the other scenarios use) then renders it through the GL viewport's transparent thumbnail path
// (no chrome, no gizmo, no datum planes, alpha=0 background) at the recipe camera, writing
// out_dir/<name>.png. The capture is a thumbnail-FBO readback (NOT the backbuffer), so it is
// independent of window size and free of UI. Doubles as a smoke test that each tool's op actually
// runs on the engine.
void ScenarioRunner::runIconPipe(ShotConfig& cfg, app::AppState& st) {
    if (!ipInit) {
        ipInit = true;
        const char* sm = std::getenv("AI_CAD_SHOT");
        ipWant = cfg.wantIconPipe && sm && std::strcmp(sm, "iconpipe") == 0;
        if (ipWant) {
            ipOutDir = repoBase() + "/scratch/icon_render";
            const char* rp = std::getenv("AI_CAD_ICON_RECIPES");
            if (rp) {
                try {
                    std::ifstream f(rp);
                    nlohmann::json j; f >> j;
                    if (j.contains("out_dir") && j["out_dir"].is_string())
                        ipOutDir = j["out_dir"].get<std::string>();
                    if (j.contains("px") && j["px"].is_number())
                        ipPx = j["px"].get<int>();
                    if (j.contains("recipes") && j["recipes"].is_array())
                        for (auto& r : j["recipes"]) ipRecipes.push_back(r);
                } catch (const std::exception& e) {
                    std::printf("iconpipe: FAILED to parse recipes '%s': %s\n", rp, e.what());
                    std::fflush(stdout);
                }
            } else {
                std::printf("iconpipe: AI_CAD_ICON_RECIPES not set — nothing to render\n");
                std::fflush(stdout);
            }
            std::error_code ec; std::filesystem::create_directories(ipOutDir, ec);
            std::printf("iconpipe: %zu recipes px=%d out=%s\n", ipRecipes.size(), ipPx,
                        ipOutDir.c_str());
            std::fflush(stdout);
        }
    }
    if (!(ipWant && st.workerStarted && !cfg.iconPipeShotDone)) return;

    // Drain EVERY recipe in this single tick. Everything on the recipe path is synchronous on this
    // thread (applyOps -> in-process libaicad/worker; viewportLoadAicm -> AICM parse + setMeshes GL
    // upload; renderThumbnail -> immediate FBO draw; readThumbnailRGBA -> glReadPixels), so no
    // settle frames or per-frame stepping are needed. This matters headless: full GUI frames are
    // pathologically slow on an unattended desktop, so one-tick draining is the difference between
    // seconds and half an hour. The other scenarios step per-frame because they capture the
    // BACKBUFFER (needs presented frames); this one never does.
    for (ipIndex = 0; ipIndex < (int)ipRecipes.size(); ++ipIndex) {
        const nlohmann::json& r = ipRecipes[ipIndex];
        const std::string name = r.value("name", std::string("icon_") + std::to_string(ipIndex));

        // Build the demo part for this recipe from its ordered "steps". Each step is either a raw
        // op (applied verbatim) or a {feature, selrefs} pair whose edge/face references are
        // resolved at runtime from the built geometry (dress-up ops like fillet/chamfer/shell need
        // real topo picks — a static recipe can't hardcode them). Reuses the m1 selector patterns
        // above so the recipe table stays pure data (owned by scripts/icon_pipeline.py).
        std::string err;
        if (!st.docMirror.newPart(st.worker, err)) {
            std::printf("iconpipe[%s]: new_part FAILED: %s\n", name.c_str(), err.c_str());
            std::fflush(stdout);
            continue;  // skip this recipe, keep going
        }
        // Resolve a named selector to a JSON array of pick objects (edges/faces).
        auto resolveSel = [&](const std::string& sel, int n) -> nlohmann::json {
            nlohmann::json out = nlohmann::json::array();
            if (sel == "vertical_edges" || sel == "all_edges") {
                for (int tid = 1; tid <= 12 && (int)out.size() < n; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "edge", tid, se);
                    if (s.empty()) continue;
                    try {
                        nlohmann::json o = nlohmann::json::parse(s);
                        if (sel == "all_edges") { out.push_back(o); continue; }
                        if (o.contains("axis") && o["axis"].is_array() && o["axis"].size() == 3 &&
                            std::fabs(o["axis"][2].get<double>()) > 0.9)
                            out.push_back(o);
                    } catch (...) {}
                }
            } else if (sel == "top_face") {  // face with the highest centroid Z (m1's topFace)
                nlohmann::json best; double bestZ = -1e30;
                for (int tid = 1; tid <= 40; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                    if (s.empty()) break;
                    try {
                        nlohmann::json o = nlohmann::json::parse(s);
                        if (!o.contains("centroid")) continue;
                        double z = o["centroid"][2].get<double>();
                        if (z > bestZ) { bestZ = z; best = o; }
                    } catch (...) {}
                }
                if (best.is_object()) out.push_back(best);
            }
            return out;
        };
        const nlohmann::json steps = r.value("steps", nlohmann::json::array());
        bool any = false;
        std::string lastAicm;
        for (const auto& step : steps) {
            nlohmann::json feat = step.value("feature", nlohmann::json::object());
            nlohmann::json ops = nlohmann::json::array();
            if (feat.is_object() && !feat.empty()) {
                if (step.contains("selrefs") && step["selrefs"].is_object()) {
                    nlohmann::json refs = feat.value("refs", nlohmann::json::object());
                    for (auto it = step["selrefs"].begin(); it != step["selrefs"].end(); ++it) {
                        std::string sel = it.value().value("select", std::string());
                        int nn = it.value().value("n", 64);
                        refs[it.key()] = resolveSel(sel, nn);
                    }
                    feat["refs"] = refs;
                }
                ops.push_back({{"op", "add_feature"}, {"feature", feat}});
            } else if (step.contains("op")) {
                ops.push_back(step);
            } else {
                continue;
            }
            auto resp = st.docMirror.applyOps(st.worker, ops);
            any = true;
            if (resp.ok && !resp.aicm_path.empty()) { lastAicm = resp.aicm_path; loadMesh(st, lastAicm); }
            std::printf("iconpipe[%s]: step '%s' ok=%d rev=%d\n", name.c_str(),
                        feat.value("type", step.value("op", std::string("?"))).c_str(),
                        (int)resp.ok, resp.revision);
            std::fflush(stdout);
        }
        if (!any) {
            std::printf("iconpipe[%s]: no build steps\n", name.c_str()); std::fflush(stdout);
            continue;
        }

        // Render the transparent thumbnail and write the PNG (same tick — see drain note above).
        // loadMesh() above only pushed the mesh into st.scene (the legacy pick scaffold); the
        // thumbnail render needs it in the REAL GL viewport pipeline (ui/viewport/renderer.h),
        // the same seam setupPickShot() uses to feed that renderer outside of ensureDocLoaded().
        if (!lastAicm.empty()) ui::viewportLoadAicm(lastAicm);
        const float yaw   = r.value("yaw", 35.0f);
        const float pitch = r.value("pitch", 28.0f);
        auto* gv = ui::viewportGl();
        if (gv) {
            gv->renderer().renderThumbnail(yaw, pitch, ipPx, /*transparentBg=*/true);
            std::vector<unsigned char> rgba = gv->renderer().readThumbnailRGBA(ipPx);
            // Opacity sanity: count non-transparent pixels so an empty render shows in the log.
            size_t opaque = 0; for (size_t i = 3; i < rgba.size(); i += 4) if (rgba[i] > 8) ++opaque;
            std::string out = ipOutDir + "/" + name + ".png";
            stbi_write_png(out.c_str(), ipPx, ipPx, 4, rgba.data(), ipPx * 4);
            std::printf("iconpipe[%s]: wrote %s (%dx%d, %zu/%d opaque px)\n", name.c_str(),
                        out.c_str(), ipPx, ipPx, opaque, ipPx * ipPx);
            std::fflush(stdout);
        } else {
            std::printf("iconpipe[%s]: GL viewport pipeline not wired — skipped render\n",
                        name.c_str());
            std::fflush(stdout);
        }
    }

    cfg.iconPipeShotDone = true;  // frees the frame loop to exit (see endFrame below)
    std::printf("iconpipe: complete — %d icon(s) written to %s\n", ipIndex, ipOutDir.c_str());
    std::fflush(stdout);
}

ScenarioRunner::FrameAction ScenarioRunner::endFrame(ShotConfig& cfg, app::AppState& st,
                                                      GLFWwindow* win, int dw, int dh,
                                                      const char* screenshotPath) {
    ++cfg.frame;

    auto mirrorLast = [&](const std::string& cap, const char* tag) {
        const char* mirror = cap.find(tag) != std::string::npos ? screenshotPath : nullptr;
        writeBackbufferPng(cap.c_str(), dw, dh, mirror);
        std::printf("screenshot -> %s (%dx%d)\n", cap.c_str(), dw, dh);
        std::fflush(stdout);
    };

    // T-G13 (issue #7): worker-absent backstop. Every step-machine (isScenarioMode) scenario
    // gates its body on st.workerStarted and only Breaks once its xShotDone is set; with no
    // Python worker provisioned that never happens, so endFrame would return Continue forever
    // (captureFrameBudget() is 999999 for these) and the --shot loop spins until the outer
    // timeout kills it with no PNG — the exact deadlock in issue #7. When the worker never
    // started, capture the (chrome-only) frame once ImGui has laid out and Break so the shot
    // always terminates. Inert when the worker is up (workerStarted true): the real step machine
    // runs and captures normally, so this can NEVER pre-empt the long-running fea/flow sims
    // (which only reach their wait loops worker-present).
    if (cfg.isScenarioMode() && !st.workerStarted && cfg.frame >= 12) {
        writeBackbufferPng(screenshotPath, dw, dh);
        if (win) glfwSwapBuffers(win);
        std::printf("%s: worker absent — captured chrome-only shot (no part)\n",
                    cfg.name ? cfg.name : "scenario");
        std::fflush(stdout);
        return FrameAction::Break;
    }

    if (cfg.wantM1) {
        if (!m1Capture.empty()) { mirrorLast(m1Capture, "m1_06_reopen"); m1Capture.clear(); }
        if (win) glfwSwapBuffers(win);
        return cfg.m1ShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantSketch) {
        if (!sketchCapture.empty()) { mirrorLast(sketchCapture, "sketch_03_solid"); sketchCapture.clear(); }
        if (win) glfwSwapBuffers(win);
        return cfg.sketchShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantSketchDispatch) {
        if (!sketchDispatchCapture.empty()) {
            mirrorLast(sketchDispatchCapture, "sketch_dispatch_09_empty_drag");
            sketchDispatchCapture.clear();
        }
        if (win) glfwSwapBuffers(win);
        return cfg.sketchDispatchShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantSketchAutohide) {
        if (!sketchAutohideCapture.empty()) {
            mirrorLast(sketchAutohideCapture, "sketch_autohide_02_toolon");
            sketchAutohideCapture.clear();
        }
        if (win) glfwSwapBuffers(win);
        return cfg.sketchAutohideShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantSketchConvert) {
        if (!scvCapture.empty()) { mirrorLast(scvCapture, "sketch_convert_01_insketch"); scvCapture.clear(); }
        if (win) glfwSwapBuffers(win);
        return cfg.sketchConvertShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantAsm) {
        if (!asmCapture.empty() && !st.rpc.busy()) { mirrorLast(asmCapture, "asm_01_seated"); asmCapture.clear(); }
        if (win) glfwSwapBuffers(win);
        return cfg.asmShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantFea) {
        if (cfg.feaShotDone) {
            writeBackbufferPng(screenshotPath, dw, dh);
            if (win) glfwSwapBuffers(win);
            return FrameAction::Break;
        }
        if (win) glfwSwapBuffers(win);
        return FrameAction::Continue;
    }
    if (cfg.wantFlow) {
        if (cfg.flowShotDone) {
            writeBackbufferPng(screenshotPath, dw, dh);
            if (win) glfwSwapBuffers(win);
            return FrameAction::Break;
        }
        if (win) glfwSwapBuffers(win);
        return FrameAction::Continue;
    }
    if (cfg.wantFeaOverlay) {  // T-SIM2: capture the precomputed-result contour once it settles
        if (cfg.feaOverlayShotDone) {
            writeBackbufferPng(screenshotPath, dw, dh);
            if (win) glfwSwapBuffers(win);
            return FrameAction::Break;
        }
        if (win) glfwSwapBuffers(win);
        return FrameAction::Continue;
    }
    if (cfg.wantSimMaterial || cfg.wantSimPrecheck || cfg.wantSimPostcheck) {  // T-SIM3
        const bool done = cfg.simMaterialShotDone || cfg.simPrecheckShotDone ||
                          cfg.simPostcheckShotDone;
        if (done) {
            writeBackbufferPng(screenshotPath, dw, dh);
            if (win) glfwSwapBuffers(win);
            return FrameAction::Break;
        }
        if (win) glfwSwapBuffers(win);
        return FrameAction::Continue;
    }
    if (cfg.wantDrawing) {
        if (!dwgCapture.empty() && !st.rpc.busy()) {
            writeBackbufferPng(dwgCapture.c_str(), dw, dh, screenshotPath);
            dwgCapture.clear();
        }
        if (win) glfwSwapBuffers(win);
        return cfg.drawingShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantEvalMass) {
        if (cfg.evalMassShotDone && cfg.frame > 6 && !st.rpc.busy()) {
            writeBackbufferPng(screenshotPath, dw, dh);
            if (win) glfwSwapBuffers(win);
            return FrameAction::Break;
        }
        if (win) glfwSwapBuffers(win);
        return FrameAction::Continue;
    }
    if (cfg.wantActivity) {
        if (!actCapture.empty() && !st.rpc.busy()) {
            mirrorLast(actCapture, "activity_02_agents");
            actCapture.clear();
        }
        if (win) glfwSwapBuffers(win);
        return cfg.activityShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    if (cfg.wantMultidoc) {
        if (!mdCapture.empty() && !st.rpc.busy()) { mirrorLast(mdCapture, "multidoc_04_B"); mdCapture.clear(); }
        if (win) glfwSwapBuffers(win);
        return cfg.multidocShotDone ? FrameAction::Break : FrameAction::Continue;
    }
    // T-G13 (issue #7): cam had no capture branch — add one so it captures on camShotDone
    // (mirrors fea/flow) instead of falling through to the 999999 generic budget and hanging.
    if (cfg.wantCam) {
        if (cfg.camShotDone) {
            writeBackbufferPng(screenshotPath, dw, dh);
            if (win) glfwSwapBuffers(win);
            return FrameAction::Break;
        }
        if (win) glfwSwapBuffers(win);
        return FrameAction::Continue;
    }
    // T-G25: icon pipeline writes its own per-recipe PNGs (runIconPipe) — the --shot throwaway
    // path is irrelevant here, so skip the normal capturePNG-and-quit entirely and just Break
    // once every recipe has drained. If the worker never starts, the T-G13 backstop above
    // already Breaks at frame 12 so this can't hang either.
    if (cfg.wantIconPipe) {
        if (win) glfwSwapBuffers(win);
        return cfg.iconPipeShotDone ? FrameAction::Break : FrameAction::Continue;
    }

    return FrameAction::None;
}

}  // namespace ui
