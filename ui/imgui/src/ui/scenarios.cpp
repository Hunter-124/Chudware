// Scripted screenshot/test scenarios — AI-CAD2 port of host/src/ui/scenarios.cpp.
#include "ui/scenarios.h"

#include "ui/viewport.h"

#include "ui/ribbon.h"
#include "ui/settings_modal.h"
#include "ui/file_explorer.h"
#include "ui/preview_pane.h"
#include "ui/scaffold_demo_panel.h"
#include "ui/parts_panel.h"
#include "ui/cam_panel.h"            // T-U3 CAM workspace (AI_CAD_SHOT=cam_workspace)
#include "ui/cam/cam_panel.h"        // camLoadJob (Manufacture panel job adoption, AI_CAD_SHOT=cam)
#include "ui/bom_panel.h"
#include "ui/requirements_panel.h"   // T-U5
#include "ui/transparency_panel.h"
#include "ui/piping_panel.h"       // T-U7 piping viewer
#include "ui/hydraulic_panel.h"    // T-U7 hydraulic viewer
#include "ui/structural_panel.h"   // T-U7 structural viewer   // T-U8
#include "ui/gallery_panel.h"        // T-U9
#include "ui/mission_control_panel.h"  // T-F17
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
    AgentBlock u; u.kind = BlockKind::UserMsg;
    u.body = "make it 30mm tall with a chamfered top edge";
    st.agent.blocks.push_back(u);
    AgentBlock a; a.kind = BlockKind::AssistantMsg;
    a.body = "Raised the standoff to 30mm and chamfered the top rim.";
    AgentBlock t; t.kind = BlockKind::Thinking; t.title = "Thinking";
    t.body = "Edit the open hex standoff: 30mm tall + chamfered top.";
    a.body = "Done — height 30mm, 1mm chamfer on the top rim.";
    st.agent.blocks.insert(st.agent.blocks.end(), {t, a});
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

int ShotConfig::captureFrameBudget() const {
    if (wantFlow && !flowShotDone) return 999999;
    if (wantFea && !feaShotDone) return 999999;
    if (wantCam && !camShotDone) return 999999;
    if (wantEcad && !ecadShotDone) return 150;
    if (wantBom && !bomShotDone) return 150;
    if (wantRequirements && !requirementsShotDone) return 150;  // T-U5
    if (wantTransparency && !transparencyShotDone) return 150;  // T-U8
    if (wantSimStudies && !simStudiesShotDone) return 150;
    if (wantTree) return treeShotDone ? 10 : 30;
    if (wantEquations) return equationsShotDone ? 10 : 30;
    if (wantPartsBrowser) return partsBrowserShotDone ? 10 : 30;
    if (isScenarioMode()) return 999999;
    if (wantGlassperf) return glassperfShotDone ? 10 : 400;
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
    cfg.wantTree = eq("tree");
    cfg.wantExplorer = eq("explorer");
    cfg.wantEquations = eq("equations");
    cfg.wantM1 = eq("m1");
    cfg.wantMultidoc = eq("multidoc");
    cfg.wantEcad = eq("ecad");
    cfg.wantSketch = eq("sketch");
    cfg.wantFea = eq("fea");
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
    cfg.wantTransparency = eq("transparency");   // T-U8
    cfg.wantPiping       = eq("piping_view");       // T-U7
    cfg.wantHydraulic    = eq("hydraulic_view");    // T-U7
    cfg.wantStructural   = eq("structural_view");   // T-U7
    cfg.wantGallery       = eq("gallery");
    cfg.wantMissionControl = eq("mission_control");  // T-F17
    cfg.wantSimStudies   = eq("sim_studies");   // T-U6
    cfg.wantGlassperf = eq("glassperf");
    if (cfg.wantGlassperf) seedChatShot(st);  // bubble rendering included in the measured frame

    if (cfg.wantSettings) {
        settingsUi().wantSettings = true;
        settingsUi().openSettings = true;
    }
    if (cfg.wantExplorer) st.railTab = kFilesRailTab;
    if (cfg.wantRibbon) {
        st.workspace = cmd::Workspace::Model;
        ribbon::applyShotEnv(st);
    }
    if (cfg.wantSketch) {
        st.inSketch = true;
        st.workspace = cmd::Workspace::Sketch;
        ribbon::applyShotEnv(st);
    }
    if (cfg.wantFea || cfg.wantFlow) {
        st.workspace = cmd::Workspace::Simulate;
        ribbon::applyShotEnv(st);
    }
    if (cfg.wantCam) st.workspace = cmd::Workspace::Model;
    if (cfg.wantAsm) st.workspace = cmd::Workspace::Assemble;
    if (cfg.wantDrawing) st.workspace = cmd::Workspace::Drafting;
    if (cfg.wantEcad) st.workspace = cmd::Workspace::Schematic;
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
        if (st.worker.call("ecad_orchestrate",
                {{"recipe", "divider"}, {"kind", wantPcb ? "pcb" : "sch"}}, res, err, 60000))
            std::printf("ecad: orchestrate ok\n");
        else
            std::printf("ecad: orchestrate skipped (%s)\n", err.c_str());
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

    // ---------- fea ----------
    if (cfg.wantFea && st.workerStarted && !cfg.feaShotDone) {
        if (feaSettle > 0) { --feaSettle; return; }
        switch (feaStep) {
            case 0: {
                std::string err;
                st.docMirror.newPart(st.worker, err);
                st.addDocTab(false);
                auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
                    {{"op","add_feature"},{"feature",{
                        {"type","box"},{"name","Box1"},
                        {"params",{{"length",dim(60)},{"width",dim(40)},{"height",dim(10)}},
                                   {"centered",true}}}}}}));
                loadMesh(st, r.aicm_path);
                st.workspace = cmd::Workspace::Simulate;
                feaSettle = 3; ++feaStep; break;
            }
            case 1: {
                std::string stepPath = repoBase() + "/scratch/fea_part.step";
                nlohmann::json ex, er; std::string e;
                st.worker.call("doc_export_step",
                    {{"doc_id", st.docMirror.docId()}, {"path", stepPath}}, ex, e, 60000);
                nlohmann::json fixFace, loadFace;
                double bestMin = 1e30, bestMax = -1e30;
                for (int tid = 1; tid <= 12; ++tid) {
                    std::string se;
                    std::string s = st.docMirror.resolvePick(st.worker, 1, "face", tid, se);
                    if (s.empty()) continue;
                    try {
                        auto o = nlohmann::json::parse(s);
                        if (!o.contains("centroid")) continue;
                        double cx = o["centroid"][0].get<double>();
                        if (cx < bestMin) { bestMin = cx; fixFace = o; }
                        if (cx > bestMax) { bestMax = cx; loadFace = o; }
                    } catch (...) {}
                }
                nlohmann::json study = {
                    {"step_path", stepPath},
                    {"material", {{"E_mpa", 210000}, {"nu", 0.3}, {"density_g_cm3", 7.85}}},
                    {"mesh", {{"fineness", 0.2}, {"order", 2}}},
                    {"fixtures", nlohmann::json::array()},
                    {"loads", nlohmann::json::array()}};
                std::string note;
                st.simClient.ensureStarted(note);
                st.simClient.launch("sim_run_static", {{"study", study}}, note, st.docMirror.docId());
                feaSettle = 2; ++feaStep; break;
            }
            case 2: {
                ++feaWaitFrames;
                if (st.simClient.job().ready.load() || feaWaitFrames > 5400) {
                    cfg.colormapOn = st.simClient.job().ok;
                    cfg.feaShotDone = true;
                    std::printf("fea: done ok=%d frames=%d\n",
                                (int)st.simClient.job().ok, feaWaitFrames);
                }
                break;
            }
            default: cfg.feaShotDone = true; break;
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
                settingsUi().openSettingsTab = 3;
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
    switch (dwgStep) {
        case 0: {
            std::string err;
            st.docMirror.newPart(st.worker, err);
            st.addDocTab(false);
            st.docMirror.applyOps(st.worker, nlohmann::json::array({
                {{"op","add_feature"},{"feature",{
                    {"type","box"},{"name","Plate"},
                    {"params",{{"length",dim(80)},{"width",dim(50)},{"height",dim(12)}},
                               {"centered",true}}}}},
                {{"op","add_feature"},{"feature",{
                    {"type","cylinder"},{"name","Hole"},
                    {"params",{{"radius",dim(6)},{"height",dim(16)},{"centered",true},
                               {"mode","cut"}}}}}}}));
            st.dispatch("make_drawing");
            dwgSettle = kSettle; ++dwgStep; break;
        }
        case 1:
            dwgCapture = dwgSavePath + "/drawing_01_detailed.png";
            dwgSettle = kSettle; ++dwgStep; break;
        default:
            cfg.drawingShotDone = true;
            break;
    }
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

    return FrameAction::None;
}

}  // namespace ui
