// SimManager — per-document simulation studies (FEA / CFD) UI + run plumbing.
//
// Replaces the old global SimPanel. Each open Part/Assembly tab owns one SimManager on its
// DocSession, so studies are LINKED to the part: they live in the document (doc["studies"], via the
// sim_study_* RPCs), persist inside the .step+ file, and follow the tab on a switch. The studies
// list is presented as a SolidWorks-style tree in the left pane's "Simulation" tab (drawTreeBody);
// selecting a study or one of its sub-nodes (Material / Fixtures / Loads / Mesh / Results) opens an
// inline PropertyManager-style form (drawForm) with viewport face pickers, pre-run fixture/load
// previews, tooltips, and the Run action.
//
// The heavy solve still runs on the single global SimClient (worker #2); jobs are tagged with the
// owning doc id so a completion lands on the right document even if the user switched tabs mid-run.
#pragma once

#include <array>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "../render/aicm_loader.h"   // render::DocMesh for motion study body-transform cache

struct AppState;

namespace core { class SimClient; }

namespace ui {

enum class StudyType { Static, Modal, Flow, Fatigue, Thermal, Motion };

// Static failure theory used for the headline factor of safety + the FoS plot field.
enum class FailureCriterion { VonMises = 0, Tresca = 1, MaxNormal = 2 };

// One picked face: the resolved selector plus the viewport id-pass identity at pick time so the
// face can be highlighted in the preview overlay (and re-highlighted after a save/reopen — the
// identity is stashed in the selector under "_pick" so it survives the round-trip).
struct SimSel {
    nlohmann::json selector;     // resolved face selector (kind=="face")
    uint32_t pickBody = 0;       // AICM body id (1-based) — 0 = unknown (not highlightable)
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
    // Mesh controls. Fineness (0=coarse..1=fine) is the primary knob; the rest are
    // advanced overrides. See core/sim/mesher.h MeshOptions.
    float meshFineness = 0.5f;      // Coarse..Fine — base element size from bbox diagonal
    bool  meshHex = false;          // structured hexahedral mesh (box/prismatic parts only)
    bool  meshCurvature = true;     // curvature-adaptive refinement (holes/fillets)
    float meshCurvPerCircle = 12.0f;// advanced: elements per full turn of curvature
    int meshOrder = 2;              // 1 = draft (linear tet4), 2 = high quality (quadratic tet10).
                                    // 2nd order captures bending accurately (linear tets shear-lock).
    float meshTargetMm = 0.0f;      // advanced: explicit element size override (0 = auto)
    float meshMinMm = 0.0f;         // advanced: min element size override (0 = auto)
    int   meshAlgo = 1;             // advanced 3D algorithm knob (legacy; Netgen uses Delaunay)
    bool meshOptimize = true;       // Netgen surface + volume optimization pass
    // Mesh quality readout from the last Preview (transient; not persisted).
    bool  meshHasQuality = false;
    float meshAspectMin = 0, meshAspectAvg = 0, meshAspectMax = 0;
    float meshJacobianMin = 0, meshPctPoor = 0;
    std::vector<float> meshAspectHist;  // 10 buckets
    int nModes = 6;                 // modal

    // Static: which failure theory drives the headline FoS + the FoS plot field.
    FailureCriterion criterion = FailureCriterion::VonMises;

    // Fatigue (StudyType::Fatigue). The engine evaluates ALL mean-stress theories; fatigueMethod only
    // chooses which one colors the model + headlines. Loading is defined by load factors (the cyclic
    // stress runs lmin..lmax times the applied-load stress). Marin factors correct the endurance limit
    // (Se) from material + part + service conditions; reliability drives the confidence band.
    int   fatigueMethod = 0;        // plotted/headline theory: 0 Goodman 1 Gerber 2 Soderberg 3 ASME 4 Morrow
    int   fatigueLoadType = 0;      // 0 fully reversed, 1 zero-based, 2 ratio R, 3 custom min/max factor
    float fatigueRatio = -1.0f;     // load ratio R = Smin/Smax (used when fatigueLoadType==2)
    float fatigueLmin = -1.0f;      // custom min load factor (fatigueLoadType==3)
    float fatigueLmax = 1.0f;       // custom max load factor (fatigueLoadType==3)
    float fatigueSe = 0.0f;         // endurance limit Se' override (MPa); 0 = from material/auto
    float fatigueSut = 0.0f;        // ultimate override (MPa); 0 = from material
    // Marin endurance-correction inputs.
    int   fatigueSurface = 1;       // ka: 0 ground, 1 machined/cold-drawn, 2 hot-rolled, 3 as-forged
    int   fatigueLoadCase = 0;      // kb/kc: 0 bending, 1 axial, 2 torsion
    float fatigueSizeMm = 25.0f;    // kb: effective section size (mm)
    float fatigueTempC = 20.0f;     // kd: operating temperature (C)
    float fatigueReliability = 50.0f;  // design reliability % (confidence) for the headline + band

    // Flow (external wind/water tunnel). The base axis + AoA/sideslip compose the incoming flow
    // direction; fluid + roughness set the physics; ref area normalises the coefficients.
    int flowDir = 0;                // base flow axis: 0..5 = +X,-X,+Y,-Y,+Z,-Z
    float flowSpeed = 10.0f;        // free-stream speed, m/s
    int flowPreset = 0;             // grid resolution: 0 coarse,1 medium,2 fine,3 xfine
    int flowFluid = 0;              // 0 air, 1 water, 2 custom
    float flowDensity = 1.225f;     // fluid density (kg/m^3) — used when flowFluid==custom
    float flowViscosity = 1.5e-5f;  // kinematic viscosity (m^2/s) — used when flowFluid==custom
    float flowAoA = 0.0f;           // angle of attack (deg, pitch about the side axis)
    float flowSideslip = 0.0f;      // sideslip (deg, yaw about the up axis)
    int flowRoughness = 0;          // surface-roughness preset index (last = custom)
    float flowRoughnessUm = 0.0f;   // equivalent sand-grain roughness Ks (micrometres) — when custom
    float flowRefAreaMm2 = 0.0f;    // reference area for Cd/Cl (mm^2); 0 = auto (projected frontal)
    int flowSolver = 0;             // CFD engine: 0 LBM (fast), 1 OpenFOAM RANS (accurate, needs provision)

    // Thermal / heat transfer (OpenFOAM only).
    bool flowThermal = false;       // enable heat-transfer solve
    int flowThermalType = 0;        // 0 fluid (buoyantSimpleFoam, wall BC), 1 conjugate (CHT, solid+fluid)
    float flowInletTempC = 20.0f;   // free-stream / inlet air temperature (degC)
    // Fluid-thermal (type 0): wall BC applied to the whole part surface.
    int flowWallMode = 2;           // wall thermal BC: 0 fixed temp, 1 heat flux, 2 total power
    float flowWallTempC = 80.0f;    // mode 0: wall fixed temperature (degC)
    float flowWallFluxWm2 = 0.0f;   // mode 1: wall heat flux (W/m^2)
    float flowWallPowerW = 10.0f;   // mode 2: total dissipated power (W)
    // Conjugate (type 1): the solid part conducts heat from an internal dissipation.
    float flowSolidKappa = 200.0f;  // solid thermal conductivity (W/mK); 200 ~ aluminium
    float flowPowerW = 20.0f;       // power dissipated inside the solid (W)

    // ---- Motion study (StudyType::Motion) ----
    // Drives ONE dimensional assembly mate across [motionStart, motionStop] in `motionSteps`
    // samples; re-solves the mate graph at each step. The per-body 4×4 world transforms are
    // cached in motionTrajectory and played back with setBodyTransforms().
    std::string motionMateId;           // mate id to drive (empty = auto-pick single drivable mate)
    float motionStart = 0.0f;           // driver start value (deg or mm)
    float motionStop  = 360.0f;         // driver stop value
    int   motionSteps = 24;             // number of samples [2..120]
    bool  motionLoop  = true;           // append a closing sample back at start
    bool  motionCheckInterference = false;  // run interference check at each step
    float motionFps   = 24.0f;          // playback speed (frames per second)

    // Transient result (filled by runMotion, never persisted).
    bool  motionHasResult = false;
    bool  motionPlaying   = false;
    int   motionFrame     = 0;          // current displayed frame
    double motionLastTick = -1.0;       // ImGui::GetTime() at last frame advance
    nlohmann::json motionTrajectory;    // array of {i, value, converged, bodies:[{name, transform}]}
    std::string motionDriverUnit;       // "deg" or "mm"
    // Per-body translation magnitude trace (|t| per frame) for the position plot.
    // Keyed by body name; built once after runMotion and plotted in the Results panel.
    std::vector<std::string> motionBodyNames;   // body display order (from frame 0)
    int  motionPlotBody = 0;                    // which body to show in the trace plot

    // Result state (transient — regenerated on Run; never persisted).
    bool hasResult = false;
    std::string summary;            // human result summary text (headline, also cached in the doc)
    std::string banner;             // permanent CFD qualitative-result banner (reynolds_note)
    std::vector<float> dragTrace;   // live CFD drag sparkline samples

    // Rich result payload (transient) so the Results panel can show a full metric table, switch
    // colormapped fields with no re-solve, and surface what the solver actually did (transparency).
    nlohmann::json resultSummary;   // solver "summary" object (von Mises / principals / shear / disp / fos)
    std::string overlayPath;        // cached result overlay .aicm (re-loaded by field, no re-solve)
    int nNodes = 0, nElems = 0, nDof = 0;  // mesh / system size from the last run
    std::vector<float> freqs;       // modal natural frequencies (Hz)
    double dragN = 0, cd = 0, frontalMm2 = 0;  // parsed CFD numbers for the metric table
    // Extended flow results: full force/coefficient set + legend ranges (transient).
    double liftN = 0, sideN = 0, cl = 0, ld = 0, reynolds = 0, refAreaMm2 = 0;
    double cpMin = 0, cpMax = 0, vmaxMps = 0;  // legend ranges for the Cp / velocity plots
    std::vector<std::string> flowFields;       // scalar_fields available to plot (pressure_cp, velocity)
    std::vector<std::string> solverNotes;      // solver_notes echoed verbatim (pressure area, fallbacks…)
};

// Which sub-node of a study the tree has selected (drives the form + previews).
enum class SimNode { Study, Material, Fixtures, Load, Mesh, Results };

class SimManager {
public:
    SimManager();

    // ---- left-pane "Simulation" tab body (no Begin/End — called inside the tab child) ----
    void drawTreeBody(AppState& st);

    // ---- ribbon Simulation-tab commands (sim_new_static/modal/flow, sim_fixture, sim_load_*,
    //      sim_mesh, sim_run, sim_results, sim_material). Returns true if consumed. ----
    bool handleCommand(AppState& st, const std::string& cmdId);

    // Drain the SimClient progress + completion on the main thread (only consumes a job this
    // document owns). Loads the result overlay + turns on the colormap/legend when a study finishes.
    void tick(AppState& st);

    // Pull this document's studies from the engine (sim_study_list) into the local mirror. Called
    // on tab activation / first draw and after each mutation.
    void refresh(AppState& st);
    // Drop the local result overlay state (called when leaving the tab / deleting a study).
    void clearResultOverlay(AppState& st);
    // Reconcile the viewport with the current view: the result overlay (FEA mesh, colormap, deformed)
    // is shown ONLY while the Simulation tab + the selected study's Results node are active. The
    // moment focus moves elsewhere (another sub-node, another study, another left-pane tab) the real
    // editable model mesh is reloaded — otherwise the result mesh sticks (and it has no pickable
    // faces, so setup edits silently fail). Called every frame from tick().
    void syncResultView(AppState& st);

    // ---- viewport pick multiplexing (called from app.cpp's pick loop, AFTER the PM is checked) ----
    // True when the Simulation tab is active AND a fixture/load well is armed AND no PM form is
    // open (mutual exclusion with the PropertyManager pick consumer).
    bool wantsViewportFacePick(AppState& st) const;
    // Route a resolved FACE pick into the armed well. Returns true if consumed.
    bool routeFacePick(AppState& st, const nlohmann::json& selector, uint32_t body, uint32_t topo);

    // ---- pre-run previews (called from the viewport draw block) ----
    bool previewActive(AppState& st) const;      // Sim tab active + a study selected + no colormap
    void collectFixtureFaces(std::vector<uint64_t>& keys) const;  // (body<<32)|topo
    void collectLoadFaces(std::vector<uint64_t>& keys) const;

    // Build world-space indicator geometry for the selected study's setup, sized to the model: red
    // arrows for force/pressure loads (force along the vector, pressure into the face) and green
    // ground/clamp glyphs at fixed faces. Anchors/normals come from st.faceNormalCache. Flat
    // [x0,y0,z0,x1,y1,z1,…] GL_LINES batches, ready to hand to Overlays::drawSegments. When the
    // optional triangle batches are supplied the arrowheads / fixture cones are emitted as SOLID
    // triangulated glyphs (flat xyz triples for Overlays::drawTriangles) instead of the legacy
    // 4-line wireframe heads — pass both so shafts and heads stay in sync.
    void collectIndicatorSegments(AppState& st, std::vector<float>& loadSegs,
                                  std::vector<float>& fixtureSegs,
                                  std::vector<float>* loadTris = nullptr,
                                  std::vector<float>* fixtureTris = nullptr) const;

    // Mesh-preview wireframe (surface element edges from the last "Preview mesh") for the selected
    // study, or an empty batch. Only valid while the Sim tab is active and the preview matches sel_.
    bool hasMeshPreview(AppState& st) const;
    const std::vector<float>& meshPreviewSegments() const { return meshPreviewSegs_; }

    // ---- flow-study setup preview (drawn while a Flow study is selected, before/without a result) ----
    // True when the selected study is a Flow study and no result colormap is up (so the wind-tunnel
    // setup overlay reads without muddying a result). Drawn from the viewport block.
    bool flowSetupActive(AppState& st) const;
    // Build the wind-tunnel setup overlay sized to the model: the incoming-flow arrows (oriented by
    // the base axis + AoA + sideslip), the flow-domain box, and the inlet plane. Flat GL_LINES
    // batches; `arrowTris` (optional) receives solid triangulated arrowhead cones for the flow
    // arrows (Overlays::drawTriangles) — without it the legacy wireframe heads are emitted.
    void collectFlowSetup(AppState& st, std::vector<float>& arrowSegs, std::vector<float>& domainSegs,
                          std::vector<float>& inletSegs, std::vector<float>* arrowTris = nullptr) const;

    // ---- flow-result streamline / slice lines (drawn over the model while the Results node is up) ----
    // One speed-banded line batch: a pointer to the (manager-owned) flat GL_LINES xyz for that band +
    // its RGBA colour. The pointer stays valid until the next rebuildFlowLines.
    struct FlowLineBatch { const std::vector<float>* segs; float color[4]; };
    // True when a Flow study's Results node is active and a result with line overlays is loaded.
    bool flowResultLinesActive(AppState& st) const;
    // The speed-banded streamline + slice batches for the selected flow result (world coords). Honors
    // the show-streamlines / show-slices toggles. Rebuilt lazily from the cached overlay AICM.
    void collectFlowResultLines(AppState& st, std::vector<FlowLineBatch>& out);

    // Number of studies the active document carries (mirror count after the last refresh). Used by
    // the tab-switch hook to auto-reveal the Simulation workspace when a part has studies.
    int  studyCount() const { return (int)studies_.size(); }

    // ---- scripted-shot helpers (AI_CAD_SHOT=fea), preserved from the old SimPanel ----
    void configureActive(AppState& st, float density, const std::string& materialKey);

    // ---- scripted-shot helpers (AI_CAD_SHOT=motion) ----
    // True when the selected study is a Motion type with a successful result.
    bool motionHasResult() const;
    // Number of frames in the active motion trajectory (0 when no result).
    int  motionFrameCount() const;
    // Advance the active motion study to a specific frame and upload transforms to the viewport.
    // No-op when motionHasResult() is false.
    void applyMotionFramePublic(AppState& st, int frame);
    // Scripted-shot helper (AI_CAD_SHOT=flow): set the active flow study's fluid / angles / resolution.
    void configureActiveFlow(AppState& st, int fluid, float aoaDeg, float sideslipDeg, int preset);

    // Material lookup (singleton: built-in engine/sim/materials.json + user custom_materials.json).
    struct Material {
        std::string key, name;
        double E_mpa = 0, nu = 0, density_g_cm3 = 0, yield_mpa = 0;
        double ultimate_mpa = 0, endurance_mpa = 0;  // Sut + fatigue limit Se (for fatigue studies)
        bool custom = false;        // true = user-defined (from custom_materials.json)
    };
    static const std::vector<Material>& materials();
    static int materialIndexByKey(const std::string& key);

    // Add a user-defined material (persisted to %APPDATA%/AI-CAD/custom_materials.json) and merge it
    // into the live material list. Returns the assigned key.
    static std::string addCustomMaterial(const Material& m);

    // Effective simulation display-unit system (resolves AppState::simUnits + doc units to concrete
    // labels + SI conversion factors). Stress/pressure -> psi or MPa; length -> in or mm; force ->
    // lbf or N. Multiply an SI value by *FromSI to display; divide a typed value to store.
    struct UnitSys {
        bool us = false;
        const char* stress = "MPa"; double stressFromMpa = 1.0;
        const char* len = "mm";     double lenFromMm = 1.0;
        const char* force = "N";    double forceFromN = 1.0;
        const char* press = "MPa";  double pressFromMpa = 1.0;
    };
    static UnitSys unitSys(const AppState& st);

private:
    // tree + form
    void drawStudyRow(AppState& st, int i);
    void drawForm(AppState& st, Study& s);
    void drawStudyChecklist(AppState& st, Study& s);  // stepwise setup overview (all structural nodes)
    void drawFixturesForm(AppState& st, Study& s);
    void drawLoadsForm(AppState& st, Study& s);
    void drawMaterialCombo(AppState& st, Study& s);
    void drawMeshForm(AppState& st, Study& s);
    void drawFlowForm(AppState& st, Study& s);     // wind-tunnel setup: fluid, AoA, roughness, ref area
    void drawResultControls(AppState& st, Study& s);
    void drawWellRow(AppState& st, const char* label, SimWell& well, int armId, const char* tip);
    void drawCustomMaterialPopup(AppState& st, Study& s);

    // result-field switching (re-load the cached overlay coloured by the chosen named scalar — no solve)
    void applyResultField(AppState& st, Study& s);
    // mesh preview (sim_mesh -> surface-element wireframe)
    void previewMesh(AppState& st, Study& s);
    void clearMeshPreview();

    // engine mutation
    void newStudy(AppState& st, StudyType t);
    void persist(AppState& st, Study& s);          // sim_study_edit for one study
    void deleteStudy(AppState& st, int i);
    static nlohmann::json studyToJson(const Study& s);
    static Study studyFromJson(const nlohmann::json& j);

    // fatigue form + result graphs (Goodman / Gerber / Soderberg diagram + S-N curve)
    void drawFatigueForm(AppState& st, Study& s);
    void drawFatigueGraphs(AppState& st, Study& s);

    // run
    void run(AppState& st, Study& s);
    void runStaticOrModal(AppState& st, Study& s);
    void runFlow(AppState& st, Study& s);
    void runFatigue(AppState& st, Study& s);
    // Motion study: call simulate_motion synchronously on the worker, parse the trajectory,
    // show frame 0 and navigate to Results. runMotion is called once from run().
    void runMotion(AppState& st, Study& s);
    // Apply one trajectory frame to the renderer: CPU-transform each body's cached positions by
    // the row-major 4×4 from the trajectory, then re-upload via st.setBodyTransforms().
    void applyMotionFrame(AppState& st, Study& s, int frame);
    // Build the per-body position-magnitude trace for the plot from the stored trajectory.
    void buildMotionTrace(Study& s);
    // Inline form for a Motion study (mate-id / range / steps / interference / fps / loop).
    void drawMotionForm(AppState& st, Study& s);
    // Timeline transport bar (rewind / step-back / play-pause / step-fwd / scrub slider).
    void drawMotionTimeline(AppState& st, Study& s);
    // Full results panel for a completed motion study (timeline + trace plot).
    void drawMotionResults(AppState& st, Study& s);
    // Advance the playback frame on the main thread (called from tick()).
    void tickMotion(AppState& st);
    nlohmann::json facePointsFor(AppState& st, const nlohmann::json& selector, std::string& err);

    // pick capture: append every currently-selected face into `well`.
    void captureSelectionInto(AppState& st, SimWell& well);

    std::vector<Study> studies_;
    int sel_ = -1;                  // selected study index (-1 = none)
    SimNode node_ = SimNode::Study; // selected sub-node of sel_
    int loadIdx_ = -1;              // which load is selected when node_==Load

    // Armed well for viewport picking: -1 none, 0 = fixtures, >=1 = loads[armId-1]. Tied to sel_.
    int armedWell_ = -1;

    std::string lastStepPath_;      // temp STEP from the last export
    bool running_ = false;          // a run launched by THIS manager is in flight
    int  runningStudy_ = -1;        // index of the study that launched the in-flight run
    std::string runningStudyId_;    // stable id of the launched study (survives refresh/reorder)

    std::string lastDocId_;         // doc id the mirror was last refreshed for (re-pull on change)
    long lastRevision_ = -1;        // engine revision at last refresh

    // Result-view state: whether the FEA result overlay is currently loaded into the viewport, and
    // for which study, so syncResultView only reloads the overlay / restores the model mesh on an
    // actual transition (not every frame). See syncResultView.
    bool resultShown_ = false;
    std::string shownStudyId_;
    // Re-tessellate the current document and reload its (editable, face-bearing) model mesh, replacing
    // a result overlay. Turns the colormap/deformation off first.
    void restoreModelMesh(AppState& st);

    // Mesh-preview state (built by previewMesh from sim_mesh; drawn in the viewport while setting up).
    std::vector<float> meshPreviewSegs_;   // flat GL_LINES xyz pairs (surface element edges)
    std::string meshPreviewStudyId_;       // study the preview belongs to ("" = none)

    // Flow-result line overlays (streamlines + velocity slices) parsed from the cached overlay AICM
    // and split into speed bands so drawSegments (flat-colour) can fake a speed colormap. Rebuilt when
    // the shown study / toggles change. Drawn over the (visible) model while the Results node is up.
    static constexpr int kFlowBands = 8;
    std::vector<float> flowStreamSegs_[kFlowBands];  // streamlines per speed band
    std::vector<float> flowSliceSegs_[kFlowBands];   // slice gridlines per speed band
    std::string flowLinesBuiltFor_;        // study id the line bands were built for ("" = none)
    bool flowShowStreamlines_ = true;      // result-panel toggles
    bool flowShowSlices_ = false;
    double flowLinesVmax_ = 1.0;           // speed range the bands were built against
    void rebuildFlowLines(AppState& st, Study& s);  // (re)parse the overlay AICM into the band arrays

    // Motion study: CPU-side cached DocMesh so applyMotionFrame can re-upload with per-body
    // transforms without re-parsing the AICM each frame. Populated in runMotion(), cleared in
    // clearResultOverlay(). The PartMesh rebuilding mirrors AppState::loadDocMesh's shaded path.
    render::DocMesh motionDocMesh_;

    // Custom-material editor scratch (filled by the "+ New material…" popup).
    char cmName_[64] = "";
    float cmE_ = 70000.0f, cmNu_ = 0.30f, cmDensity_ = 2.7f, cmYield_ = 250.0f;
    float cmUlt_ = 310.0f, cmEnd_ = 96.0f;  // ultimate (Sut) + endurance (Se), MPa
};

}  // namespace ui
