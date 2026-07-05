// SimManager (H4) — per-document simulation studies (FEA / CFD) UI + run plumbing.
// Ported from host/src/ui/sim_manager.{h,cpp} into the new AI-CAD2 GUI tree.
//
// Each open Part/Assembly tab conceptually owns its own studies: they live in the document
// (doc["studies"], via the sim_study_* RPCs), persist inside the .step+ file, and are re-pulled
// (refresh) whenever the active doc/revision changes. The studies list is presented as a tree
// (drawTreeBody) in the Simulate-workspace overlay column; selecting a study or one of its
// sub-nodes (Material / Fixtures / Load / Mesh / Results) opens an inline form (drawForm) with
// face-selection wells, unit-aware numeric fields, and the Run action.
//
// The heavy solve runs on the single shared core::SimClient (the sim worker, `st.simClient`);
// jobs are tagged with the owning doc id so a completion lands on the right document even if the
// user switched tabs mid-run (tick()).
//
// Engine contract (study JSON shape, sim_study_*/sim_run_*/sim_mesh RPC methods + params) is kept
// BYTE-IDENTICAL to host/src/ui/sim_manager.cpp — see studyToJson/studyFromJson and the run*()
// methods. Adaptations from host (documented, not silent):
//   - Face wells are filled via an explicit "Add selected faces" button that captures the
//     ALREADY-selected faces from st.selection (core::Selection), instead of host's live
//     click-routes-to-an-armed-well flow. Routing viewport clicks into a sim well needs a call
//     site in the app-frame pick loop (app.cpp), which is outside this task's write-set
//     (ui/imgui/src/ui/sim_manager.* + the Simulate-workspace surfacing site in viewport.cpp).
//     wantsViewportFacePick/routeFacePick are kept (unused) so that wiring is a small follow-up.
//   - The 3D setup/result overlays host draws in the viewport (load-arrow / fixture-cone
//     indicator glyphs, the wind-tunnel flow-setup arrows, the mesh-preview wireframe, and the
//     flow streamline/slice line batches) are NOT ported — they need the shared
//     core::Renderer/Overlays draw-call plumbing wired from viewport.cpp, beyond the minimal
//     workspace-gate surfacing this task scopes. The DATA behind them (mesh quality stats, node/
//     element counts, drag trace, flow Cd/Cl/etc.) is fully computed and shown as text/plots.
//   - Result colormap + legend reuse the shared AICM overlay contract (render::DocMesh::scalars/
//     displacements, ui/viewport/render/aicm_loader.h) via new viewport.cpp entry points
//     (viewportLoadResultAicm / viewportSetColormapMode / viewportSetDeformScale) added
//     alongside the Simulate-workspace gate, and ui::viewportState() (legendOn/Min/Max/Title/
//     Units) — the SAME legend the rest of the app already draws every frame (ui/legend.cpp).
//   - Unit system: host reads st.simUnits + st.ips(); the new AppState carries neither, so the
//     display-unit toggle (SI/US/follow-doc) is kept as SimManager's own state (unitsMode_) and
//     ui::c5::ips(st) (appearance_common.h) stands in for st.ips().
//   - Fatigue Results: host draws a full Haigh (constant-life) diagram + an S-N curve with a
//     reliability band (drawFatigueGraphs, custom ImDrawList plots). Ported as a per-theory
//     FoS/life TABLE (same numbers, same math) instead of the two custom charts — a visual, not
//     data, simplification.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace app { struct AppState; }
struct ImVec2;

namespace ui {

enum class StudyType { Static, Modal, Flow, Fatigue, Thermal };

// Static failure theory used for the headline factor of safety + the FoS plot field.
enum class FailureCriterion { VonMises = 0, Tresca = 1, MaxNormal = 2 };

// One picked face: the resolved selector plus the viewport id-pass identity at pick time so the
// face can be highlighted / re-resolved after a save/reopen.
struct SimSel {
    nlohmann::json selector;     // resolved face selector (kind=="face")
    uint32_t pickBody = 0;       // AICM body id (1-based) — 0 = unknown
    uint32_t pickTopo = 0;       // face topo id (1-based)
};

// One selector well's captured face picks (+ a short chip label).
struct SimWell {
    std::vector<SimSel> picks;
    std::string label() const;
    bool empty() const { return picks.empty(); }
};

// One load entry (force vector in N, or pressure in MPa) over a set of faces.
struct SimLoad {
    SimWell faces;
    bool pressure = false;          // false = force vector, true = pressure
    float vec[3] = {0, 0, -100};    // N (force)
    float pressureMpa = 0.1f;       // MPa (pressure)
};

// A study definition + its last (transient, not-persisted) result.
struct Study {
    std::string id;                 // engine-assigned ("study-XXXXXXXX")
    StudyType type = StudyType::Static;
    std::string name;

    // FEA
    std::string materialKey;        // material key (e.g. "steel_1018"); "" = none
    SimWell fixtures;
    std::vector<SimLoad> loads;
    // Mesh controls. Fineness (0=coarse..1=fine) is the primary knob; the rest are advanced.
    float meshFineness = 0.5f;
    bool  meshHex = false;
    bool  meshCurvature = true;
    float meshCurvPerCircle = 12.0f;
    int meshOrder = 2;              // 1 draft (tet4), 2 high quality (tet10)
    float meshTargetMm = 0.0f;
    float meshMinMm = 0.0f;
    int   meshAlgo = 1;
    bool meshOptimize = true;
    // Mesh quality readout from the last Preview (transient; not persisted).
    bool  meshHasQuality = false;
    float meshAspectMin = 0, meshAspectAvg = 0, meshAspectMax = 0;
    float meshJacobianMin = 0, meshPctPoor = 0;
    std::vector<float> meshAspectHist;  // 10 buckets
    int nModes = 6;                 // modal

    FailureCriterion criterion = FailureCriterion::VonMises;

    // Fatigue.
    int   fatigueMethod = 0;        // 0 Goodman 1 Gerber 2 Soderberg 3 ASME 4 Morrow
    int   fatigueLoadType = 0;      // 0 fully reversed, 1 zero-based, 2 ratio R, 3 custom min/max
    float fatigueRatio = -1.0f;
    float fatigueLmin = -1.0f;
    float fatigueLmax = 1.0f;
    float fatigueSe = 0.0f;
    float fatigueSut = 0.0f;
    int   fatigueSurface = 1;       // ka: 0 ground,1 machined,2 hot-rolled,3 as-forged
    int   fatigueLoadCase = 0;      // kb/kc: 0 bending,1 axial,2 torsion
    float fatigueSizeMm = 25.0f;
    float fatigueTempC = 20.0f;
    float fatigueReliability = 50.0f;

    // Flow (external wind/water tunnel).
    int flowDir = 0;                // 0..5 = +X,-X,+Y,-Y,+Z,-Z
    float flowSpeed = 10.0f;
    int flowPreset = 0;             // 0 coarse,1 medium,2 fine,3 xfine
    int flowFluid = 0;              // 0 air,1 water,2 custom
    float flowDensity = 1.225f;
    float flowViscosity = 1.5e-5f;
    float flowAoA = 0.0f;
    float flowSideslip = 0.0f;
    int flowRoughness = 0;
    float flowRoughnessUm = 0.0f;
    float flowRefAreaMm2 = 0.0f;
    int flowSolver = 0;             // 0 LBM (fast), 1 OpenFOAM RANS
    bool flowThermal = false;
    int flowThermalType = 0;        // 0 fluid, 1 conjugate
    float flowInletTempC = 20.0f;
    int flowWallMode = 2;           // 0 fixed temp, 1 heat flux, 2 total power
    float flowWallTempC = 80.0f;
    float flowWallFluxWm2 = 0.0f;
    float flowWallPowerW = 10.0f;
    float flowSolidKappa = 200.0f;
    float flowPowerW = 20.0f;

    // Thermal (steady-state conduction FEA, T-SIM1). Distinct from the OpenFOAM-CHT flow
    // "thermal" fields above — these drive the sim_run_thermal conduction backend. Fixtures
    // carry the fixed hot temperature; a cold face is set by a second fixture temp, a
    // convection film, or a heat flux (mirrors force/pressure load choices).
    float thermalHotTempC = 100.0f;     // Dirichlet temperature applied to fixture faces
    bool  thermalUseColdFace = false;   // second Dirichlet (cold) face uses a load slot as the target
    float thermalColdTempC = 20.0f;
    bool  thermalUseConvection = true;  // convection (Robin) film on the load face(s)
    float thermalConvH = 25.0f;         // film coefficient, W/(m^2·K)
    float thermalAmbC = 20.0f;          // ambient/bulk temperature, °C
    bool  thermalUseFlux = false;       // heat-flux (Neumann) load on the load face(s)
    float thermalFluxWm2 = 0.0f;        // applied surface flux, W/m^2 (>0 = into the body)
    float thermalKwmk = 0.0f;           // conductivity used (resolved from the table; 0 = auto)

    // Result state (transient — regenerated on Run; never persisted).
    bool hasResult = false;
    // T-SIM2: per-study "Show contour" toggle. Lives on the Study (not one SimManager member) so
    // it is per-study by construction — toggling contour off on study A never leaks to study B's
    // Results view. Default on: a fresh result shows its contour immediately (SolidWorks style).
    bool contourOn = true;
    std::string summary;
    std::string banner;
    std::vector<float> dragTrace;

    nlohmann::json resultSummary;
    std::string overlayPath;
    int nNodes = 0, nElems = 0, nDof = 0;
    std::vector<float> freqs;
    double dragN = 0, cd = 0, frontalMm2 = 0;
    double liftN = 0, sideN = 0, cl = 0, ld = 0, reynolds = 0, refAreaMm2 = 0;
    double cpMin = 0, cpMax = 0, vmaxMps = 0;
    std::vector<std::string> flowFields;
    std::vector<std::string> solverNotes;

    // Thermal result scalars (T-SIM1; transient, regenerated on Run).
    double maxTempC = 0.0, minTempC = 0.0;

    // T-SIM3: sim-advisor postcheck result (transient, regenerated on Run completion).
    // Verdict is PASS/WARN/FAIL vs engine_oracle/sim/margin_policy.json; narration is the
    // advisor's multi-line explanation (narrate_postcheck). Empty verdict = advisor not run.
    std::string advisorVerdict;
    std::string advisorMatClass;
    double advisorSf = 0.0;
    std::string advisorNarration;
    nlohmann::json advisorItems;     // aicad.advisory/1 items from postcheck
};

// Which sub-node of a study the tree has selected (drives the form).
enum class SimNode { Study, Material, Fixtures, Load, Mesh, Results };

class SimManager {
public:
    SimManager();

    // ---- Simulate-workspace overlay column (no outer Begin/End — called inside the Viewport
    //      window; pins a bordered child column at the LEFT edge of [vpMin,vpMax], mirroring the
    //      FeatureManager/Copilot pinned-column convention). ----
    void drawEmbedded(app::AppState& st, const ImVec2& vpMin, const ImVec2& vpMax);

    // ---- ribbon Simulation-tab commands (sim_new_static/modal/flow/thermal/fatigue, sim_material,
    //      sim_mesh, sim_results). Returns true if consumed. Not wired to a ribbon in this task. ----
    bool handleCommand(app::AppState& st, const std::string& cmdId);

    // Drain the SimClient progress + completion on the main thread (only consumes a job this
    // document owns). Loads the result overlay + turns on the colormap/legend when a study finishes.
    void tick(app::AppState& st);

    // Pull this document's studies from the engine (sim_study_list) into the local mirror.
    void refresh(app::AppState& st);
    // Drop the local result overlay state.
    void clearResultOverlay(app::AppState& st);
    // Reconcile the viewport with the current view: the result overlay is shown ONLY while the
    // Results node of the selected study is active; otherwise the editable model mesh is restored.
    void syncResultView(app::AppState& st);

    // ---- viewport pick mux (kept for a future app.cpp pick-loop wire-up; unused by this port) ----
    bool wantsViewportFacePick(app::AppState& st) const;
    bool routeFacePick(app::AppState& st, const nlohmann::json& selector, uint32_t body, uint32_t topo);

    // Number of studies the active document carries (mirror count after the last refresh).
    int  studyCount() const { return (int)studies_.size(); }

    // ---- scripted-shot helpers (AI_CAD_SHOT=fea/flow), preserved from host's SimPanel ----
    void configureActive(app::AppState& st, float density, const std::string& materialKey);
    void configureActiveFlow(app::AppState& st, int fluid, float aoaDeg, float sideslipDeg, int preset);
    // T-SIM3 shot seams: open the material-assignment dialog for the selected study, and
    // seed a completed static result on it then run the REAL advisor postcheck over it
    // (fixture-seeded numbers because Netgen cannot mesh headless — same honesty pattern
    // as the fea_overlay / sim_studies scenarios; the advisor call itself is live).
    void openMaterialDialog() { matDialogOpen_ = true; }
    void debugSeedStaticResult(app::AppState& st, double vmMpa, double dispMm, double minFos);

    // Material lookup (singleton: built-in engine/sim/materials.json + user custom_materials.json).
    struct Material {
        std::string key, name;
        double E_mpa = 0, nu = 0, density_g_cm3 = 0, yield_mpa = 0;
        double ultimate_mpa = 0, endurance_mpa = 0;
        bool custom = false;
    };
    static const std::vector<Material>& materials();
    static int materialIndexByKey(const std::string& key);
    static std::string addCustomMaterial(const Material& m);

    // Effective simulation display-unit system. Multiply an SI value by *FromSI to display;
    // divide a typed value to store.
    struct UnitSys {
        bool us = false;
        const char* stress = "MPa"; double stressFromMpa = 1.0;
        const char* len = "mm";     double lenFromMm = 1.0;
        const char* force = "N";    double forceFromN = 1.0;
        const char* press = "MPa";  double pressFromMpa = 1.0;
    };
    UnitSys unitSys(const app::AppState& st) const;

private:
    void drawStudyRow(app::AppState& st, int i);
    void drawForm(app::AppState& st, Study& s);
    void drawFixturesForm(app::AppState& st, Study& s);
    void drawLoadsForm(app::AppState& st, Study& s);
    void drawMaterialCombo(app::AppState& st, Study& s);
    void drawMeshForm(app::AppState& st, Study& s);
    void drawFlowForm(app::AppState& st, Study& s);
    void drawThermalForm(app::AppState& st, Study& s);  // T-SIM1 conduction inputs
    void drawResultControls(app::AppState& st, Study& s);
    void drawWellRow(app::AppState& st, const char* label, SimWell& well, const char* tip);
    void drawCustomMaterialPopup(app::AppState& st, Study& s);
    // T-SIM3: modal material-assignment dialog (doc material + global library + custom).
    void drawMaterialDialog(app::AppState& st, Study& s);
    // T-SIM3: sim-advisor (T-W9) wiring. Precheck returns true when dispatch may proceed —
    // advise-never-veto: only AI_CAD_SIM_PRECHECK_BLOCK=1 turns blocker findings into a stop.
    bool advisorPrecheck(app::AppState& st, Study& s);
    void advisorPostcheck(app::AppState& st, Study& s);
    nlohmann::json advisorSpec(app::AppState& st, const Study& s, bool withResults) const;
    void drawAdvisoryCard(app::AppState& st, Study& s);
    void drawFatigueForm(app::AppState& st, Study& s);
    void drawFatigueResults(app::AppState& st, Study& s);   // per-theory FoS/life table

    // result-field switching (re-load the cached overlay coloured by the chosen named scalar).
    void applyResultField(app::AppState& st, Study& s);
    // mesh preview (sim_mesh -> node/element counts + quality stats; no 3D wireframe — see header note).
    void previewMesh(app::AppState& st, Study& s);

    // engine mutation
    void newStudy(app::AppState& st, StudyType t);
    void persist(app::AppState& st, Study& s);
    void deleteStudy(app::AppState& st, int i);
    static nlohmann::json studyToJson(const Study& s);
    static Study studyFromJson(const nlohmann::json& j);

    // run
    void run(app::AppState& st, Study& s);
    void runStaticOrModal(app::AppState& st, Study& s);
    void runFlow(app::AppState& st, Study& s);
    void runFatigue(app::AppState& st, Study& s);
    void runThermal(app::AppState& st, Study& s);  // T-SIM1: steady-state conduction dispatch
    nlohmann::json facePointsFor(app::AppState& st, const nlohmann::json& selector, std::string& err);

    // pick capture: append every currently-selected face into `well` (st.selection).
    void captureSelectionInto(app::AppState& st, SimWell& well);

    void restoreModelMesh(app::AppState& st);

    std::vector<Study> studies_;
    int sel_ = -1;
    SimNode node_ = SimNode::Study;
    int loadIdx_ = -1;

    std::string lastStepPath_;
    bool running_ = false;
    int  runningStudy_ = -1;
    std::string runningStudyId_;

    std::string lastDocId_;
    long lastRevision_ = -1;

    bool resultShown_ = false;
    std::string shownStudyId_;

    // Which named scalar chunk currently colors the loaded result overlay (host: st.activeScalarField,
    // an AppState field the new tree doesn't carry — kept here instead, see header note).
    std::string activeScalarField_;

    int unitsMode_ = 0;  // 0 follow document, 1 SI, 2 US — SimManager's own state (see header note)

    char cmName_[64] = "";
    float cmE_ = 70000.0f, cmNu_ = 0.30f, cmDensity_ = 2.7f, cmYield_ = 250.0f;
    float cmUlt_ = 310.0f, cmEnd_ = 96.0f;

    // T-SIM3: material-assignment dialog state.
    bool matDialogOpen_ = false;
    char matFilter_[64] = "";
    int  matDialogSel_ = -1;

    // T-SIM3: last precheck advisory (transient; belongs to study precheckStudyId_).
    nlohmann::json precheckItems_ = nlohmann::json::array();
    std::string precheckStudyId_;
    bool precheckBlocked_ = false;
    std::string advisorNote_;   // advisor-unavailable note ("" = advisor reachable)
};

// File-static singleton accessor (mirrors ui::propertyManager()).
SimManager& simManager();

}  // namespace ui
