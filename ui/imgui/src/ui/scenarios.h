// Scripted screenshot/test scenarios for AI-CAD2 (ported from host/src/ui/scenarios.*).
// ShotConfig holds AI_CAD_SHOT flags; ScenarioRunner drives frame machines + capture timing.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct GLFWwindow;

namespace app { struct AppState; }

namespace ui {

// Per-shot flags and progress (mirrors host AppState want*/ *ShotDone fields).
struct ShotConfig {
    const char* name = nullptr;   // AI_CAD_SHOT value
    const char* capturePath = nullptr;

    bool active = false;
    int  frame = 0;

    // Simple one-frame UI shots
    bool wantSettings = false;
    bool wantChat = false;
    bool wantAssembly = false;
    bool wantPlan = false;
    bool wantPick = false;
    bool wantRibbon = false;
    bool wantExplorer = false;
    bool wantPmform = false;
    bool wantPreview = false;
    bool wantScaffoldDemo = false;
    bool wantCamWorkspace = false;            // T-U3: populate + screenshot the CAM workspace panel
    bool camWorkspaceShotDone = false;
    bool wantGallery = false;                 // T-U9: sample gallery (loads from disk, no RPC wait)
    bool wantQuestion = false;               // T-BL40: inline ask_user question cards

    // Worker one-shot shots (fire once when workerStarted)
    bool wantOrigin = false;        // T-BL37: origin glyph visible + selected (AI_CAD_SHOT=origin)
    bool originShotDone = false;
    bool wantTree = false;
    bool treeShotDone = false;
    bool wantEquations = false;
    bool equationsShotDone = false;
    bool wantEcad = false;
    bool ecadShotDone = false;
    bool wantPartsBrowser = false;
    bool partsBrowserShotDone = false;
    bool wantBom = false;
    bool bomShotDone = false;
    bool wantRequirements = false;          // T-U5: requirements & verification panel
    bool requirementsShotDone = false;
    bool wantTransparency = false;          // T-U8: model & telemetry transparency panel
    bool transparencyShotDone = false;
    bool wantSimStudies = false;                 // T-U6: sim studies panel (AI_CAD_SHOT=sim_studies)
    bool simStudiesShotDone = false;

    // T-U7: new-doc-kind viewers (piping, hydraulic, structural)
    bool wantPiping = false;
    bool pipingShotDone = false;
    bool wantHydraulic = false;
    bool hydraulicShotDone = false;
    bool wantStructural = false;
    bool structuralShotDone = false;
    bool wantMissionControl = false;        // T-F17: mission-control (DAG steering) panel
    bool missionControlShotDone = false;

    // T-W17: transparency inspector + watch grid + settings (fixture-seeded, no worker)
    bool wantAgentInspector = false;            // populated run dir (Turns, turn 0)
    bool wantAgentInspectorEmpty = false;       // empty run dir
    bool wantAgentInspectorFailure = false;     // select the fixer turn: failure card + audit
    bool wantAgentInspectorDelegation = false;  // force the Delegation tab
    bool agentInspectorShotDone = false;
    bool wantWatchGrid = false;             // 3-part workspace fixture
    bool watchGridShotDone = false;
    // T-DP13: multi-file agent view — 2 docs under concurrent agent work (fixture-seeded).
    bool wantAgentMultifile = false;
    bool agentMultifileShotDone = false;
    bool wantAgentSettings = false;         // valid routing view
    bool wantAgentSettingsError = false;    // validation-error routing view
    bool agentSettingsShotDone = false;

    // T-DP17: Agent DAG widget (fixture-seeded, no worker RPC)
    bool wantAgentDag        = false;   // full mode — node-graph + detail pane (node pre-selected)
    bool wantAgentDagCompact = false;   // compact mode — graph only
    bool agentDagShotDone    = false;

    // T-DP15: workspace-kind shots — prove overlay columns present/absent per workspace kind.
    // ws_overlay_hosting: Modeling workspace — glass left+right columns visible over viewport.
    // ws_full_viewport_ecad: Schematic workspace — ECAD editor fills screen, no glass columns.
    // ws_ai_agent: AI-Agent workspace — full-viewport shell, no glass columns.
    // ws_plm: PLM workspace — full-viewport shell, no glass columns.
    bool wantWsOverlayHosting   = false;
    bool wsOverlayHostingShotDone = false;
    bool wantWsFullViewportEcad = false;
    bool wsFullViewportEcadShotDone = false;
    bool wantWsAiAgent          = false;
    bool wsAiAgentShotDone      = false;
    bool wantWsPlm              = false;
    bool wsPlmShotDone          = false;

    // T-G22: per-tab settings shots + composer dropdown + permissions popup
    bool wantSettingsGeneral = false;
    bool wantSettingsAI = false;
    bool wantSettingsPrompts = false;
    bool wantSettingsAdvanced = false;
    bool wantComposerDropdown = false;
    bool wantPermissionsPopup = false;

    // T-W28: AI Effort section shots (fixture-seeded, settings modal AI tab)
    bool wantAiEffortSimple = false;    // simple mode, 4-level dial
    bool wantAiEffortAdvanced = false;  // advanced mode, grouped knobs + overrides
    bool wantAiEffortWarn = false;      // advanced mode with warn-never-block advisories
    // T-DP12: auto mode — workflow_router tier-defaults readout
    bool wantAiEffortAuto = false;

    // Long-running scenario modes (ScenarioRunner step machines)
    bool wantM1 = false;
    bool m1ShotDone = false;
    bool wantSketch = false;
    bool sketchShotDone = false;
    bool wantSketchDispatch = false;   // T-BL47: prove dispatch→pendingSketchCmds→active_ path
    bool sketchDispatchShotDone = false;
    bool wantSketchAutohide = false;   // T-BL41: dim/relation annotations hidden unless SmartDim armed
    bool sketchAutohideShotDone = false;
    bool wantSketchConvert = false;         // T-BL43: Convert Entities in-sketch shot
    bool sketchConvertShotDone = false;
    bool wantAsm = false;
    bool asmShotDone = false;
    bool wantFea = false;
    bool feaShotDone = false;
    bool wantCam = false;
    bool camShotDone = false;
    bool wantFlow = false;
    bool flowShotDone = false;
    bool wantDrawing = false;
    bool drawingShotDone = false;
    bool wantEvalMass = false;
    bool evalMassShotDone = false;
    bool wantActivity = false;
    bool activityShotDone = false;
    bool wantMultidoc = false;
    bool multidocShotDone = false;

    // T-G25: icon pipeline (recipe-driven clean-viewport icon render, ported from host's T-G15).
    // Drains ALL recipes in a single tick (see ScenarioRunner::runIconPipe) — no per-frame settle.
    bool wantIconPipe = false;
    bool iconPipeShotDone = false;

    // Sim overlay hints (fea/flow wait for colormap)
    bool colormapOn = false;
    bool legendOn = false;

    // T-SIM2: fea_overlay — load a precomputed FEA result AICM (real solver+skinning output,
    // generated offline because Netgen is not provisionable in CI) and render the color-mapped
    // contour + legend, without needing the live mesher. Settle-based one-shot (no worker solve).
    bool wantFeaOverlay = false;
    bool feaOverlayShotDone = false;
    int  feaOverlaySettle = 0;

    // T-SIM3: sim UI overhaul shots. sim_material = material-assignment dialog open;
    // sim_precheck = advisor precheck advisory card after Run on an unconstrained study;
    // sim_postcheck = advisor margin-verdict row on Results. The postcheck shot seeds the
    // result NUMBERS as a fixture (Netgen cannot mesh headless — same honesty pattern as
    // fea_overlay), but the advisor precheck/postcheck subprocess calls are live.
    bool wantSimMaterial = false;
    bool simMaterialShotDone = false;
    bool wantSimPrecheck = false;
    bool simPrecheckShotDone = false;
    bool wantSimPostcheck = false;
    bool simPostcheckShotDone = false;

    // Perf harness (frame-driven; runs independent of workerStarted — see
    // ScenarioRunner::tickGlassperf).
    bool wantGlassperf = false;
    bool glassperfShotDone = false;
    int  glassperfFrame = 0;

    bool isScenarioMode() const {
        return wantM1 || wantSketch || wantSketchDispatch || wantSketchAutohide || wantSketchConvert
            || wantAsm || wantFea || wantFeaOverlay || wantFlow || wantCam || wantDrawing
            || wantEvalMass || wantActivity || wantMultidoc || wantIconPipe
            || wantSimMaterial || wantSimPrecheck || wantSimPostcheck;
    }

    // Frame budget for the generic capture path (flow allows ~240 s at 60 Hz).
    int captureFrameBudget() const;
};

class ScenarioRunner {
public:
    enum class FrameAction {
        None,
        Continue,
        Break,
    };

    void tick(ShotConfig& cfg, app::AppState& st);
    FrameAction endFrame(ShotConfig& cfg, app::AppState& st, GLFWwindow* win,
                         int dw, int dh, const char* screenshotPath);

private:
    void runMultidoc(ShotConfig& cfg, app::AppState& st);
    void runDrawing(ShotConfig& cfg, app::AppState& st);
    void runIconPipe(ShotConfig& cfg, app::AppState& st);
    void tickGlassperf(ShotConfig& cfg, app::AppState& st);
    void tickSimpleShots(ShotConfig& cfg, app::AppState& st);

    // m1
    int m1Step = 0, m1Settle = 0;
    std::string m1FilletId;
    double m1FilletMsBeforeEdit = -1.0, m1BoxMsBeforeEdit = -1.0;
    std::string m1SavePath, m1Capture;
    bool m1DirReady = false;

    // sketch
    int skStep = 0, skSettle = 0;
    std::string skSavePath, skFid, skL0, skL1, skL2, skL3, skWidthCid;
    bool skDirReady = false;
    std::string sketchCapture;

    // sketch_dispatch (T-BL48: prove the real interactive entry path via dispatch→drain)
    // Extended by T-BL44: steps 10-13 inject Space key and verify normal-to orient.
    int sdStep = 0, sdSettle = 0;
    std::string sdSavePath, sdFid, sdL0, sdL1, sdL2, sdL3;
    bool sdDirReady = false;
    std::string sketchDispatchCapture;
    float sdPreSpaceYaw = 0.f, sdPreSpacePitch = 0.f;  // T-BL44: camera state before Space
    // T-BL46: drag discriminator test state (plain floats — no ImVec2 dependency in header)
    float sdElemPtX  = 0.f, sdElemPtY  = 0.f;   // screen pos of projected edge midpoint (element hit)
    float sdEmptyPtX = 0.f, sdEmptyPtY = 0.f;   // screen pos of empty-space anchor (no entity hit)
    float sdDragPreYaw  = 0.f, sdDragPrePitch  = 0.f;  // camera before element drag
    float sdEmptyPreYaw = 0.f, sdEmptyPrePitch = 0.f;  // camera before empty-space drag

    // sketch_autohide (T-BL41: dim/relation annotations hidden by default; SmartDim reveals)
    int saStep = 0, saSettle = 0;
    std::string saSavePath, saFid, saL0, saL1, saL2, saL3;
    bool saDirReady = false;
    std::string sketchAutohideCapture;
    // sketch_convert (T-BL43)
    int scvStep = 0, scvSettle = 0;
    std::string scvSavePath, scvFid;
    bool scvDirReady = false;
    std::string scvCapture;

    // asm
    int asmStep = 0, asmSettle = 0;
    std::string asmSavePath, asmBasePath, asmPegPath, asmDoc, asmK2;
    double asmSolvedZ = -999.0;
    bool asmDirReady = false;
    std::string asmCapture;

    // fea / flow
    int feaStep = 0, feaSettle = 0, feaWaitFrames = 0;
    int flowStep = 0, flowSettle = 0, flowWaitFrames = 0;

    // T-SIM3 sim UI shots (material dialog / precheck card / postcheck verdict)
    int simUiStep = 0, simUiSettle = 0;

    // evalmass
    int evalStep = 0, evalSettle = 0;

    // cam
    int camStep = 0, camSettle = 0;

    // activity
    int actStep = 0, actSettle = 0, actWaitFrames = 0;
    std::string actCapture, actSavePath;
    bool actDirReady = false;

    // multidoc
    int mdStep = 0, mdSettle = 0;
    std::string mdSavePath, mdDocA, mdDocB, mdDocC, mdCapture;
    bool mdDirReady = false;
    int mdIdxA = -1, mdIdxB = -1, mdIdxC = -1;
    bool mdPassPm = false, mdPassDoc = false, mdPassMask = false, mdPassSel = false;
    bool mdPassBIndep = false, mdPassBMeasure = false, mdPassBFresh = false, mdPassCFresh = false;
    bool mdPassChop[4] = {};

    // drawing
    int dwgStep = 0, dwgSettle = 0;
    bool dwgDirReady = false;
    std::string dwgSavePath, dwgCapture;

    // T-G25 icon pipeline (ported from host's T-G15 runIconPipe)
    bool ipInit = false, ipWant = false;
    int ipIndex = 0, ipPx = 128;
    std::string ipOutDir;
    std::vector<nlohmann::json> ipRecipes;
};

// Bind runner + config for the frame loop (main.cpp calls once after configureShot).
void setScenarioContext(ScenarioRunner* runner, ShotConfig* cfg);

// Read AI_CAD_SHOT (+ related env) and prime AppState for the requested scenario.
void configureShot(app::AppState& st, ShotConfig& cfg, const char* shotEnv,
                     const char* capturePath);

// Per-frame setup before ImGui::NewFrame (application.cpp).
void scenarioTick(app::AppState& st);

}  // namespace ui
