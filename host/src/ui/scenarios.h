// Scripted screenshot/test scenarios (extracted from app.cpp's run_gui frame loop).
//
// ScenarioRunner hosts the frame-driven state machines keyed on AI_CAD_SHOT
// (m1 / sketch / asm / fea / evalmass / activity / multidoc / appearance). tick() runs once per
// frame before
// ImGui::NewFrame() and advances whichever scenario is active; endFrame() runs after
// ImGui_ImplOpenGL3_RenderDrawData and handles the per-step backbuffer captures plus the
// scenario-driven frame-loop control (swap+continue / swap+break). Behavior-preserving
// extraction — step order, settle counts, printf transcripts, and capture paths are
// unchanged from the former inline blocks; the function-local statics became members
// (run_gui constructs one runner per call, so init-once semantics hold).
#pragma once

#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace render { class SplatRenderer; }

namespace ui {

class PropertyManager;
class EvaluateDialogs;
class ActivityPanel;

class ScenarioRunner {
public:
    // What the frame loop must do after endFrame() (mirrors the former inline blocks'
    // bottom-of-loop control flow).
    enum class FrameAction {
        None,      // no scenario mode active — fall through to the generic shot/swap path
        Continue,  // buffers swapped; skip the rest of this loop iteration (was `continue`)
        Break,     // scenario complete; buffers swapped; leave the frame loop (was `break`)
    };

    // The runner borrows the run_gui-owned panels the scenarios drive (same references the
    // ribbon dispatcher captures); AppState carries everything else. The PropertyManager and
    // EvaluateDialogs are now PER-DOCUMENT — the scenarios resolve them from st.propertyManager /
    // st.evaluateDialogs (re-seated on the doc they create), so they are no longer passed here. The
    // SimManager is likewise per-document (st.simManager), so the FEA scenario resolves it too.
    // Ctor + dtor are BOTH out-of-line: rsRenderer is a unique_ptr to an incomplete type
    // (render::SplatRenderer), so the compiler-generated cleanup (which the inline ctor's
    // exception path also needs) must be emitted where that type is complete (scenarios.cpp).
    explicit ScenarioRunner(ActivityPanel& activityPanel);
    ~ScenarioRunner();

    // Advance the active scenario's step machine (no-op when no AI_CAD_SHOT scenario mode
    // is active). Call once per frame, before ImGui::NewFrame().
    void tick(AppState& st);

    // Per-step screenshot capture + frame-loop control for the scenario modes; call after
    // the frame was rendered. dw/dh = framebuffer size; screenshotPath = the caller's
    // --ui-screenshot target (the final step of each scenario is mirrored to it).
    FrameAction endFrame(AppState& st, int dw, int dh, const char* screenshotPath);

private:
    ActivityPanel& activityPanel;

    // ---------- C17 M1 integration scenario (AI_CAD_SHOT=m1) ----------
    int m1Step = 0;            // which scenario step we are on
    int m1Settle = 0;          // frames waited since the step's ops ran (let mesh repaint)
    std::string m1FilletId;    // feature id of the fillet (for the edit step)
    double m1FilletMsBeforeEdit = -1.0, m1BoxMsBeforeEdit = -1.0;
    std::string m1SavePath;
    bool m1DirReady = false;
    // When non-empty, endFrame() writes the current backbuffer to this path (a per-step
    // screenshot) and clears it. Lives across frames (the step machine sets it, the
    // capture consumes it).
    std::string m1Capture;

    // ---------- C18 sketch scenario (AI_CAD_SHOT=sketch) ----------
    int skStep = 0, skSettle = 0;
    std::string skSavePath, skFid;
    std::string skL0, skL1, skL2, skL3, skWidthCid;
    bool skDirReady = false;
    std::string sketchCapture;  // same per-step capture mechanism, separate path slot

    // ---------- 3D sketch scenario (AI_CAD_SHOT=sketch3d) ----------
    int s3Step = 0, s3Settle = 0;
    std::string s3SavePath, s3Fid;
    bool s3DirReady = false;
    std::string s3Capture;  // same per-step capture mechanism, separate path slot

    // ---------- C20b assembly scenario (AI_CAD_SHOT=asm) ----------
    int asmStep = 0, asmSettle = 0;
    std::string asmSavePath, asmBasePath, asmPegPath, asmDoc, asmK2;
    double asmSolvedZ = -999.0;
    bool asmDirReady = false;
    std::string asmCapture;  // same per-step capture mechanism, separate path slot

    // ---------- C19 FEA scenario (AI_CAD_SHOT=fea) ----------
    int feaStep = 0, feaSettle = 0, feaWaitFrames = 0;

    // ---------- external-flow scenario (AI_CAD_SHOT=flow) ----------
    int flowStep = 0, flowSettle = 0, flowWaitFrames = 0;

    // ---------- W17 engineering-drawing scenario (AI_CAD_SHOT=drawing) ----------
    void runDrawing(AppState& st);
    int dwgStep = 0, dwgSettle = 0;
    bool dwgDirReady = false;
    std::string dwgSavePath, dwgCapture;

    // ---------- C19 evalmass scenario (AI_CAD_SHOT=evalmass) ----------
    int evalStep = 0, evalSettle = 0;

    // ---------- appearance scenario (AI_CAD_SHOT=appearance) ----------
    // Model a part, open the Settings modal on the Appearance tab (glass knobs + live glass
    // preview over the viewport), capture. Self-contained: the runner reads AI_CAD_SHOT itself
    // (no AppState want-flag), seeded once on the first tick (apInit).
    void runAppearance(AppState& st);
    bool apInit = false, apWant = false, apDone = false;
    int apStep = 0, apSettle = 0;
    std::string apSavePath, apCapture;
    bool apDirReady = false;

    // ---------- T-G15 icon pipeline (AI_CAD_SHOT=iconpipe) ----------
    // Recipe-driven clean-viewport icon generator: read a JSON recipe list (env AI_CAD_ICON_RECIPES),
    // and for each recipe build a demo part (newPart + applyOps), render it through the renderer's
    // transparent thumbnail path (no chrome, no gizmo, alpha=0 background) at the recipe camera, and
    // write out_dir/<name>.png. Doubles as a smoke test that every tool's op actually runs.
    // All recipes drain in ONE tick (everything on the path is synchronous; no backbuffer capture),
    // so headless runs don't pay the per-frame present cost.
    void runIconPipe(AppState& st);
    bool ipInit = false, ipWant = false;
    int ipIndex = 0, ipPx = 128;
    std::string ipOutDir;
    std::vector<nlohmann::json> ipRecipes;

    // ---------- RE-28 3DGS splat scenario (AI_CAD_SHOT=reveng_splat) ----------
    // Load a canned Gaussian-splat .ply fixture and draw it as camera-facing billboards into the
    // backbuffer (real render::SplatRenderer draw path), then capture. Self-contained: reads
    // AI_CAD_SHOT itself, no doc/worker geometry needed. Proves the loader + GL draw don't crash.
    void runRevengSplat(AppState& st);
    bool rsInit = false, rsWant = false, rsDone = false;
    int rsSettle = 0;
    std::string rsSavePath, rsCapture;
    bool rsDirReady = false, rsLoaded = false;
    std::unique_ptr<render::SplatRenderer> rsRenderer;

    // ---------- C22b activity scenario (AI_CAD_SHOT=activity) ----------
    int actStep = 0, actSettle = 0, actWaitFrames = 0;
    std::string actCapture;
    std::string actSavePath;
    bool actDirReady = false;

    // ---------- multi-document scenario (AI_CAD_SHOT=multidoc) ----------
    // Verifies the per-document DocSession refactor: open a feature-edit PM form mid-edit on Part A
    // (with a viewport selection + a hidden body), switch to Part B and measure, switch back to A —
    // A's PM form / selection / hidden-body mask are intact and B is independent. Same step-machine /
    // kSettle / per-step backbuffer-capture shape as m1; runMultidoc(st) drives it from tick().
    void runMultidoc(AppState& st);
    int mdStep = 0, mdSettle = 0;
    std::string mdSavePath;
    bool mdDirReady = false;
    int mdIdxA = -1, mdIdxB = -1;        // tab indices of Part A / Part B
    std::string mdDocA, mdDocB;          // worker doc ids of Part A / Part B
    bool mdPassPm = false, mdPassDoc = false, mdPassMask = false, mdPassSel = false;
    bool mdPassBIndep = false, mdPassBMeasure = false, mdPassBFresh = false;
    std::string mdCapture;               // same per-step capture mechanism, separate path slot
    // N-document scaling: a THIRD doc C with a DIFFERENT mid-edit (chamfer form + its own selection)
    // proves the DocSession handles "parts C etc" and survives A<->C round-trips. Mirror the A/B
    // slots above. mdPassChop[k] holds the per-hop verdict for the A<->C round-trip (4 hops).
    int mdIdxC = -1;                     // tab index of Part C
    std::string mdDocC;                  // worker doc id of Part C
    bool mdPassCFresh = false;           // C's PM was fresh (inactive) before we opened its form
    bool mdPassChop[4] = {false, false, false, false};  // A<->C hop verdicts (A,C,A,C)

    // ---------- workspaces scenario (AI_CAD_SHOT=workspaces) ----------
    // Cycles all 8 workspace tabs (Model, Drawing, Simulate, Electronics, Manufacture, PLM,
    // PartsLibrary, AI), capturing one screenshot per workspace. Acceptance gate for the
    // workspace routing switch. Per-step backbuffer-capture pattern matches multidoc/m1.
    void runWorkspaces(AppState& st);
    int wsStep = 0, wsSettle = 0;
    bool wsDirReady = false;
    std::string wsSavePath, wsCapture;

    // ---------- motion scenario (AI_CAD_SHOT=motion) ----------
    // Builds a 2-body assembly with an angle mate, runs a Motion study, captures t0/t1/t2
    // where body positions differ. Proves kinematic motion playback is end-to-end live.
    void runMotion(AppState& st);
    bool mtInit = false, mtWant = false, mtDone = false;
    int  mtStep = 0, mtSettle = 0;
    bool mtDirReady = false;
    std::string mtSavePath, mtCapture;
    std::string mtBasePath, mtArmPath;   // authored part AICM paths
    std::string mtAsmDoc;                // assembly doc id
    std::string mtK2;                    // second component id (the arm)
    bool mtRunOk = false;                // motion solve succeeded
};

}  // namespace ui
