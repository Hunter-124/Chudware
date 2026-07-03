// Scripted screenshot/test scenarios for AI-CAD2 (ported from host/src/ui/scenarios.*).
// ShotConfig holds AI_CAD_SHOT flags; ScenarioRunner drives frame machines + capture timing.
#pragma once

#include <string>

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

    // Worker one-shot shots (fire once when workerStarted)
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

    // Long-running scenario modes (ScenarioRunner step machines)
    bool wantM1 = false;
    bool m1ShotDone = false;
    bool wantSketch = false;
    bool sketchShotDone = false;
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

    // Sim overlay hints (fea/flow wait for colormap)
    bool colormapOn = false;
    bool legendOn = false;

    // Perf harness (frame-driven; runs independent of workerStarted — see
    // ScenarioRunner::tickGlassperf).
    bool wantGlassperf = false;
    bool glassperfShotDone = false;
    int  glassperfFrame = 0;

    bool isScenarioMode() const {
        return wantM1 || wantSketch || wantAsm || wantFea || wantFlow || wantCam
            || wantDrawing || wantEvalMass || wantActivity || wantMultidoc;
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

    // asm
    int asmStep = 0, asmSettle = 0;
    std::string asmSavePath, asmBasePath, asmPegPath, asmDoc, asmK2;
    double asmSolvedZ = -999.0;
    bool asmDirReady = false;
    std::string asmCapture;

    // fea / flow
    int feaStep = 0, feaSettle = 0, feaWaitFrames = 0;
    int flowStep = 0, flowSettle = 0, flowWaitFrames = 0;

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
};

// Bind runner + config for the frame loop (main.cpp calls once after configureShot).
void setScenarioContext(ScenarioRunner* runner, ShotConfig* cfg);

// Read AI_CAD_SHOT (+ related env) and prime AppState for the requested scenario.
void configureShot(app::AppState& st, ShotConfig& cfg, const char* shotEnv,
                     const char* capturePath);

// Per-frame setup before ImGui::NewFrame (application.cpp).
void scenarioTick(app::AppState& st);

}  // namespace ui
