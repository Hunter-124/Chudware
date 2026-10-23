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
#include <cstdint>
#include <limits>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "../render/aicm_loader.h"   // render::DocMesh for motion study body-transform cache
#include "hydraulic_panel.h"         // hydraulic circuit design / sizing / simulation panel (Hydraulics tab)
#include "piping_panel.h"            // piping / internal-flow / industrial sizing panel (Piping tab)
#include "sim_topology.h"            // topology capabilities / settings + results views
#include "sim_topology_bc.h"         // topology boundary-condition editor view
#include "sim/sim_fw_scenario.h"

struct AppState;

namespace core {
class SimClient;
struct RpcResult;
}

namespace ui {

// One Simulate workspace hosts EVERY study kind. Flow/Thermal are the OpenFOAM/LBM
// CFD family; ThermalFea is the core conduction solver (sim_run_thermal, steady +
// transient); Buckling is engine linear buckling; Contact is a CalculiX box proxy;
// FreqResponse is modal superposition (core modal + worker vibration post-processing).
enum class StudyType { Static, Modal, Flow, Fatigue, Thermal, Motion,
                       ThermalFea, Buckling, Contact, FreqResponse, Topology };

// Static failure theory used for the headline factor of safety + the FoS plot field.
enum class FailureCriterion { VonMises = 0, Tresca = 1, MaxNormal = 2 };

// Optional core-advisor envelope carried by a solver result under the shared "advisor" key.
// ABSENCE IS THE NORM: a result without the key leaves `present` false and the UI silent, which
// is the pre-advisor behaviour. Everything here is ADVICE about how the run was set up (sizing,
// order, adaptation) plus the advisor's own self-assessment — it is never a claim about which
// solver ran. Chudware's solve path is tet-only; if the advisor names a polyhedral/VEM mesher the
// UI must report that as a recommendation the solver did NOT execute.
struct SimAdvisor {
    static constexpr double kUnset = std::numeric_limits<double>::quiet_NaN();

    bool present = false;          // an "advisor" object was present in the result payload
    bool available = false;        // advisor model was loaded and produced a recommendation
    bool applied = false;          // the recommendation actually shaped this run
    bool vetoed = false;           // a guard rejected the recommendation
    bool budgetRefusal = false;    // refused because the predicted cost exceeded the budget
    std::string mesher;            // recommended mesher (advice; may name a backend we cannot solve)
    double hRel = kUnset;          // recommended relative element size
    int    order = 0;              // recommended element order (0 = unstated)
    int    adaptPasses = -1;       // recommended adaptive passes (<0 = unstated)
    double etaTarget = kUnset;     // recommended error-indicator target
    double predictedDof = kUnset;  // predicted system size
    double predictedMeshMs = kUnset;
    double predictedSolveMs = kUnset;
    double failureProb = kUnset;   // predicted probability this configuration fails
    double oodDistance = kUnset;   // distance from the advisor's training distribution
    std::string modelRevision;     // advisor model revision that produced this
    std::string note;              // free-form advisor note

    static bool has(double v) { return v == v; }  // NaN-free test without <cmath> here
    bool anyDetail() const {
        return !mesher.empty() || has(hRel) || order > 0 || adaptPasses >= 0 ||
               has(etaTarget) || has(predictedDof) || has(predictedMeshMs) ||
               has(predictedSolveMs) || has(failureProb) || has(oodDistance) ||
               !modelRevision.empty() || !note.empty();
    }
};

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
    // ThermalFea loads only (structural studies ignore these): heat flux or convection.
    int   thermalKind = 0;          // 0 = heat flux (W/m^2), 1 = convection (h, t_amb)
    float fluxWm2 = 1000.0f;        // W/m^2 (thermalKind 0)
    float hWm2k = 10.0f;            // film coefficient W/(m^2 K) (thermalKind 1)
    float tAmbC = 25.0f;            // ambient temperature C (thermalKind 1)
    // Keep native per-load metadata attached to this entry across removal/reordering.
    nlohmann::json definition, typedBaseline;
};

struct MotionDriver {
    std::string mateId;
    double start = 0.0;
    double stop = 360.0;
};

// A study definition + its last (transient, not-persisted) result.
struct Study {
    std::string id;                 // engine-assigned ("study-XXXXXXXX")
    StudyType type = StudyType::Static;
    std::string name;
    // Exact native record plus the typed projection at ingestion. Serialization applies only
    // changed typed fields, retaining unsupported/agent-authored keys without claiming execution.
    nlohmann::json definition, typedBaseline;

    // FEA
    // materialKey mirrors the ACTIVE part material (doc.material.key via set_material). UI + run
    // re-resolve from the doc so studies don't hold a throwaway override; changing the combo writes
    // set_material. Still persisted on the study JSON for back-compat / offline display.
    std::string materialKey;        // material key (e.g. "steel_1018"); "" = none
    SimWell fixtures;
    std::vector<SimLoad> loads;
  // Polyhedral-FEA controls. Fineness is primary; explicit millimetre values
  // override it.
  float meshFineness = 0.5f;
  int meshOrder = 2;         // 1=tet4 draft, 2=tet10 quadratic
  float meshTargetMm = 0.0f; // 0 = automatic
  float meshMinMm = 0.0f;    // 0 = automatic
  bool learnedAdvisor = false;
  int advisorMaxDof = 0;     // 0 = no explicit advisor ceiling
  bool adaptEnabled = false; // static only: error-driven re-mesh/re-solve
  int adaptMaxPasses = 3;
  float adaptEtaTarget = 0.05f;
  // Mesh quality readout from the last Preview (transient; not persisted).
  bool meshHasQuality = false;
  float meshAspectMin = 0, meshAspectAvg = 0, meshAspectMax = 0;
  float meshJacobianMin = 0, meshPctPoor = 0;
  std::vector<float> meshAspectHist; // 10 buckets
  std::string meshTypeUsed;
  std::string meshNote;
    int nModes = 6;                 // modal / buckling

    // Static: which failure theory drives the headline FoS + the FoS plot field.
    FailureCriterion criterion = FailureCriterion::VonMises;

    // ---- Multiphysics study kinds -------------------------------------------------------
    // Contact (CalculiX two-block proxy): pressure on the upper block.
    float contactPressureMpa = 1.0f;
    // Frequency response (modal superposition over the core modal solve).
    int   frDir = 2;                // base-excitation axis: 0..2 = X, Y, Z
    float frZeta = 0.02f;           // modal damping ratio (assumption, echoed back)
    float frBaseG = 1.0f;           // harmonic base input amplitude (g)
    // Thermal FEA (core conduction solver): fixture faces = fixed temperature; loads =
    // flux/convection; transient via backward Euler.
    float thermK = 200.0f;          // conductivity W/(m K) (default ~aluminium)
    float thermCp = 900.0f;         // specific heat J/(kg K) — transient only
    float thermFixTempC = 20.0f;    // Dirichlet temperature on the fixture faces (C)
    bool  thermTransient = false;
    float thermTEndS = 60.0f;       // transient horizon (s)
    float thermDtS = 0.0f;          // step (s); 0 = solver default
    float thermTInitC = 20.0f;      // initial temperature (C)
    // Transient result payloads for the multiphysics kinds (never persisted). advResult holds
    // the raw worker/solver JSON (incl. any aicad.simhandoff/1 provenance under
    // "simhandoff"); frSweep/frAccel are the harmonic response plot arrays.
    nlohmann::json advResult;
    std::vector<float> frSweep, frAccel;

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
    bool flowUseFaceDirection = false; // arbitrary base direction from a picked inlet face
    float flowFaceDirection[3] = {1.0f, 0.0f, 0.0f};
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
    // Synchronized dimensional mates, sampled at the same normalized time.
    std::vector<MotionDriver> motionDrivers = {MotionDriver{}};
    int   motionSteps = 24;             // number of samples [2..120]
    bool  motionLoop  = true;           // wrap playback to the first sample
    bool  motionCheckInterference = true;
    double motionMinClearance = 0.0;    // mm; intersections always checked
    float motionFps   = 24.0f;          // playback speed (frames per second)

    // Transient result (filled by runMotion, never persisted).
    bool  motionHasResult = false;
    bool  motionRunning = false;
    uint64_t motionGeneration = 0;      // invalidates queued results after settings edits/deletion
    nlohmann::json motionSource;        // assembly inputs, excluding persisted study settings
    int motionSourceRevision = -1;     // last mirror revision checked against motionSource
    nlohmann::json motionInterference;  // complete/frames_checked/pair_failures from this run only
    nlohmann::json motionResultDrivers; // native type/unit metadata for all drivers
    std::vector<int> motionCollisionFrames;
    bool  motionPlaying   = false;
    int   motionFrame     = 0;          // current displayed frame
    double motionLastTick = -1.0;       // ImGui::GetTime() at last frame advance
    nlohmann::json motionTrajectory;    // [{i,value,converged,bodies:[{component,name,transform}]}]
    std::string motionDriverUnit;       // "deg" or "mm"
    std::string motionRenderError;      // non-empty when cache/transform validation blocks playback
    // Stable ids drive trajectory lookup; names are presentation-only labels in the Results combo.
    std::vector<std::string> motionBodyIds;
    std::vector<std::string> motionBodyLabels;
    int  motionPlotBody = 0;            // which stable component id to show in the trace plot

    // Result state (transient — regenerated on Run; never persisted).
    bool hasResult = false;
    std::string summary;            // human result summary text (headline, also cached in the doc)
    std::string banner;             // permanent CFD qualitative-result banner (reynolds_note)
    // Live CFD drag sparkline. BOUNDED AT INGESTION: a long OpenFOAM run streams one sample per
    // iteration for as long as it runs, so the raw stream is unbounded. `dragTrace` holds at most
    // kDragTraceCap samples, uniformly thinned by `dragTraceStride` (deterministic power-of-two
    // halving — every retained sample sits at an arrival index that is a multiple of the stride).
    // `dragTraceTotal` is the honest arrival count so the panel can say "showing X of Y".
    std::vector<float> dragTrace;
    uint64_t dragTraceTotal = 0;    // samples the solver actually streamed
    uint32_t dragTraceStride = 1;   // 1 = every sample kept; >1 = uniformly thinned

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
    // Adaptive static convergence history (transient; never persisted).
    struct AdaptPass {
        int pass = 0, nNodes = 0, nElems = 0, nDof = 0;
        double elementSizeMm = 0.0, globalEta = 0.0;
        double maxVonMisesMpa = 0.0, maxDispMm = 0.0;
    };
    std::vector<AdaptPass> adaptPasses;
    double adaptGlobalEta = 0.0;
    bool adaptEtaTargetMet = false;
    std::string adaptStopReason;

    // Optional core-advisor envelope from the last run (transient; never persisted). Absent by
    // default — `advisor.present == false` reproduces the pre-advisor UI exactly.
    SimAdvisor advisor;

    // ---- Topology (StudyType::Topology) ----
    // The canonical study record (sim_topology_bc.h schema) is owned VERBATIM: every GUI edit
    // mutates this JSON in place, so agent-authored keys, the retained `runs`, `last_job` and
    // sections of other physics survive (studyToJson returns it with id/type/name refreshed).
    // `fixtures`/`loads` above are a read-only mirror of its structural wells for the viewport
    // indicator glyphs; they are never serialized for a topology study.
    nlohmann::json topo;
    // Transient (never persisted): last prep/run status line and the runs decorated with the
    // off-draw artifact checks (missing / stale) for the results panel.
    std::string topoStatus;
    int topoStatusKind = 0;         // 0 info, 1 warn, 2 error
    nlohmann::json topoRunsView = nlohmann::json::array();
    std::string topoRunsCheckKey;   // inputs of the last artifact check ("" = never checked)
    std::string topoFieldLabel, topoFieldUnit;  // legend of the displayed overlay field
    double topoFieldFromSI = 1.0;
};

// Which sub-node of a study the tree has selected (drives the form + previews).
enum class SimNode { Study, Material, Fixtures, Load, Mesh, Results, Boundaries, Regions };

// Run state of the study pane's action footer (kit::actionFooter), refreshed from the SimClient
// job / preparation / worker / motion state this manager owns. `running` is false while ANOTHER
// document's job holds the solver (runBlocker() then says so).
struct SimRunUi {
    bool        running = false;
    float       progress = -1.0f;      // [0,1]; -1 = unknown (marquee)
    std::string stage;                 // "Preparing model", "Solving 42%", "Iteration 12 of 40"
    double      elapsed = -1.0;        // seconds since the run started; < 0 = unknown
    bool        cancellable = false;
    std::string cancelBlocker;         // why Cancel is unavailable (when !cancellable)
    bool        cancelRequested = false;
    std::string studyId;               // study the in-flight run belongs to ("" = none)
};

// THE human label for an engine progress stage (footer, Run page, status bar — one map). `stage`
// is a legacy key (voxelize|mesh|assemble|solve|stream|post) or a native phase id
// ("mesh_background_probe", "topology.reanalyze"); `detail` is the worker's progress text
// (SimClient::recentLog / SimProgress::text), read only for a meshing cell count.
//   ("mesh_background_probe", "… (pin) 1921/1922 cells, 29376 elements") -> "Meshing · 1,921 of 1,922 cells"
// Unknown ids are humanized ("foo_bar.baz" -> "Foo bar baz"); never returns a raw id.
std::string simStageLabel(const std::string& stage, const std::string& detail = {});
// Fraction [0,1] of a meshing stage carried by `detail` (cells done / total), else -1.
float simStageFraction(const std::string& stage, const std::string& detail);

// THE face-well control row (Simulation UI standard §4), shared by FEA fixtures/loads and the
// topology wells: the label (with the pick count) on its own line, then [Pick faces] (secondary)
// or [Picking…] (accent, armed) and [Clear] — kit buttons of one frame height, left-aligned; Clear
// drops under the toggle only when the pane is too narrow for both. Ids "###arm"/"###clear" live
// in the caller's ID scope. The caller draws the picked-face chips beneath.
struct FaceWellClick { bool toggled = false; bool clear = false; };
FaceWellClick faceWellControls(const char* label, int picked, bool armed, const char* tip);

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
    // Another client (an agent's run_fea_* / simulate_motion) added or edited a study: re-pull
    // the list on the next tree draw. Driven by hostd's doc.studies_changed event.
    void markStudiesStale() { lastRevision_ = -1; listErrorDocId_.clear(); }
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
    bool routeSetupDrop(AppState& st, const std::string& tool,
                        const nlohmann::json& selector, uint32_t body, uint32_t topo);
    nlohmann::json activeStudyDefinition() const;
    // Exactly the JSON the Advanced parameters editor renders for a study definition: only
    // the keys that study's kind uses (from its "type"), floats in shortest round-trip form.
    // Apply merges the hidden keys and exact stored floats back from the definition.
    static nlohmann::json parameterEditorView(const nlohmann::json& definition);
    nlohmann::json activeStudyResult() const {
        return sel_ >= 0 && sel_ < static_cast<int>(studies_.size()) ?
               studies_[sel_].advResult : nlohmann::json();
    }
    bool configureActiveStudy(AppState& st, const nlohmann::json& definition, std::string& error);
    nlohmann::json activeEmParameters() const;
    bool configureEmParameters(const nlohmann::json& parameters, std::string& error);
    nlohmann::json electronicsSetup() const;
    nlohmann::json activeFirmwareScenario() const { return fwsim_.scenario; }

    // ---- pre-run previews (called from the viewport draw block) ----
    bool previewActive(AppState& st) const;      // Sim tab active + a study selected + no colormap
    void collectFixtureFaces(std::vector<uint64_t>& keys) const;  // (body<<32)|topo
    void collectLoadFaces(std::vector<uint64_t>& keys) const;
    // Topology preview extras: preserve / exclude / flow-port face washes and region box
    // wireframes of the selected topology study (empty for every other study kind).
    void collectTopologyPreview(std::vector<uint64_t>& preserveFaces,
                                std::vector<uint64_t>& excludeFaces,
                                std::vector<uint64_t>& portFaces,
                                std::vector<float>& preserveBoxSegs,
                                std::vector<float>& excludeBoxSegs) const;

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

    // ---- cached sim overlay geometry generation (shared UI/renderer contract) -------------------
    // Monotonically increasing counter, starting at 1, bumped ONLY when the manager's CACHED sim
    // overlay geometry actually changes: the mesh-preview wireframe (meshPreviewSegs_) and the flow
    // result line bands (flowStreamSegs_ / flowSliceSegs_). Setup indicator geometry is rebuilt per
    // frame and is deliberately NOT covered. A consumer that caches GPU buffers can compare this
    // against its last-seen value and re-upload only on a change. Per-SimManager (per-document), so
    // it never resets or goes backwards for the life of one document's manager.
    uint64_t overlayGeneration() const { return overlayGeneration_; }

    // Collision faces are already transformed to the selected sampled pose.
    bool motionCollisionHighlightActive() const { return motionHighlightPose_; }

    // ---- honest presentation-budget disclosure ---------------------------------------------------
    // One short line describing the decimation currently applied to the SELECTED study's cached
    // preview / flow-line geometry ("Preview: showing 148,392 of 2,310,554 edges (coarse LOD)"), or
    // "" when nothing is decimated. Presentation only — the solver payload is never thinned.
    std::string previewLodStatus() const;
    // One short line describing the SELECTED study's advisor state ("Advisor: applied"), or "" when
    // no advisor envelope was present. Never asserts which solver ran, and it spells out a
    // polyhedral/VEM mesher recommendation as advice the tet-only solve path did NOT execute.
    std::string advisorStatusLine() const;

    // ---- flow-study setup preview (drawn while a Flow study is selected, before/without a result) ----
    // True when the selected study is a Flow study and no result colormap is up (so the wind-tunnel
    // setup overlay reads without muddying a result). Drawn from the viewport block.
    bool flowSetupActive(AppState& st) const;
    // Build the flow setup overlay hugging the model: the incoming-flow arrows (base axis + AoA +
    // sideslip) and the inlet / outlet plane markers. Flat GL_LINES batches; `arrowTris` (optional)
    // receives solid arrowhead cones (Overlays::drawTriangles) — without it wireframe heads are
    // emitted.
    void collectFlowSetup(AppState& st, std::vector<float>& arrowSegs, std::vector<float>& inletSegs,
                          std::vector<float>* arrowTris = nullptr) const;

    // ---- flow-result streamline / slice lines (drawn over the model while the Results node is up) ----
    // One speed-banded line batch: a pointer to the (manager-owned) flat GL_LINES xyz for that band +
    // its RGBA colour. The pointer stays valid until the next rebuildFlowLines.
    struct FlowLineBatch { const std::vector<float>* segs; float color[4]; };
    // True when a Flow study's Results node is active and a result with line overlays is loaded.
    bool flowResultLinesActive(AppState& st) const;
    // The speed-banded streamline + slice batches for the selected flow result (world coords). Honors
    // the show-streamlines / show-slices toggles. Rebuilt lazily from the cached overlay AICM.
    void collectFlowResultLines(AppState& st, std::vector<FlowLineBatch>& out);

    // Number of studies the active document carries (mirror count after the last refresh, the
    // piping / vessel / duct / hydraulic-circuit records included). Used by the tab-switch hook
    // to auto-reveal the Simulation workspace when a part has studies.
    int  studyCount() const {
        return (int)studies_.size() + piping_.studyCount() + hydraulic_.studyCount();
    }

    // The Piping tab's panel (sim_piping_* commands; persisted piping/vessel/duct/pipe-flow
    // records). Scenarios drive it through the same entry points as its buttons.
    PipingPanel& piping() { return piping_; }
    // The Hydraulics tab's panel (sim_hyd_* commands; persisted hydraulic_circuit records).
    HydraulicPanel& hydraulic() { return hydraulic_; }
    // ---- central-view seam (viewport_panel.cpp drawViewportPanel) ----
    // While the Hydraulics panel shows its Schematic or Plots view it owns the central Viewport
    // window: drawCentralView paints it there in place of the 3D scene (the same branch shape as a
    // Drawing tab's sheet). In its 3D view, drawViewportHud keeps the Schematic | Plots | 3D toggle
    // over the model so the canvas stays one click away.
    bool centralViewActive() const { return hydraulic_.ownsCentralView(); }
    void drawCentralView(AppState& st) { hydraulic_.drawCentralView(st); }
    void drawViewportHud(AppState& st) { hydraulic_.drawViewToggleHud(st); }

    // ---- scripted-shot helpers (CHUDWARE_SHOT=fea), preserved from the old SimPanel ----
    void configureActive(AppState& st, float density, const std::string& materialKey);

    // Scripted-shot helper (CHUDWARE_SHOT=simdisclosure*/simaudit/topology): hold the grouped
    // "New study" picker open so headless shots capture the offered study kinds.
    bool shotForcePicker = false;

    // ---- study-pane action footer contract (kit::actionFooter) ----
    // Live run state of this manager (see SimRunUi). Cheap; call every frame.
    SimRunUi runUi() const;
    // Why `s` cannot run right now ("" = ready): setup readiness, backend capability, and a
    // solver busy with this or another document. The same gate run() enforces.
    std::string runBlocker(const Study& s) const;
    // Cancel the in-flight run when runUi().cancellable (cooperative solver cancel, topology
    // preparation cancel, or discarding a pending Motion result).
    void requestCancel(AppState& st);

    // ---- scripted-shot helpers (CHUDWARE_SHOT=motion) ----
    // True when the selected study is a Motion type with a successful result.
    bool motionHasResult() const;
    // Number of frames in the active motion trajectory (0 when no result).
    int  motionFrameCount() const;
    // Advance the active motion study to a specific frame and upload transforms to the viewport.
    // No-op when motionHasResult() is false.
    void applyMotionFramePublic(AppState& st, int frame);
    // Scripted-shot helper: set the selected motion study's dimensional sweep.
    void configureActiveMotion(AppState& st, float start, float stop, int steps);
    // Scripted-shot helper (CHUDWARE_SHOT=flow): set the active flow study's fluid / angles / resolution.
    void configureActiveFlow(AppState& st, int fluid, float aoaDeg, float sideslipDeg, int preset);
    // True when the selected flow study has a successful numeric result.  The fast
    // C++ LBM route may legitimately have no contour overlay, so scripted/live
    // verification must not equate "no colormap" with solver failure.
    bool flowHasResult() const;

    // ---- topology (human UI + scripted scenarios share these) ----
    // Deep-merge `patch` into the SELECTED topology study (objects merge recursively, arrays and
    // scalars replace, null erases a key; id/type ignored), then persist it. False when the
    // selected study is not a topology study or the patch is not an object.
    bool configureActiveTopology(AppState& st, const nlohmann::json& patch);
    bool applyActiveTopologyPrintProcess(AppState& st, const std::string& id);
    // Canonical JSON of the selected topology study (null otherwise).
    nlohmann::json activeTopologyStudy() const;
    // Latest retained run record {run_id,label,started_utc,settings,source,result} of the
    // selected topology study (null when it has no run).
    nlohmann::json activeTopologyResult() const;
    int  topologyRunCount() const;
    // True while this manager's topology preparation or solve is in flight.
    bool topologyRunActive() const;
    // Native topology_capabilities() matrix (Loading until the async reply lands).
    const topology::Capabilities& topologyCapabilities() const { return topoCaps_; }
    // Select a study by its engine id (false when absent). Resets the sub-node to Study.
    bool selectStudyById(AppState& st, const std::string& id);
    std::string activeStudyId() const;

    // Material lookup (singleton: built-in engine/sim/materials.json + user custom_materials.json).
    struct Material {
        std::string key, name;
        double E_mpa = 0, nu = 0, density_g_cm3 = 0, yield_mpa = 0;
        double ultimate_mpa = 0, endurance_mpa = 0;  // Sut + fatigue limit Se (for fatigue studies)
        bool custom = false;        // true = user-defined (from custom_materials.json)
    };
    static const std::vector<Material>& materials();
    static int materialIndexByKey(const std::string& key);

    // Add a user-defined material (persisted to %APPDATA%/Chudware/custom_materials.json) and merge it
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
    void drawParameterEditor(AppState& st, Study& s);
    std::string parameterDocId_, parameterStudyId_, parameterDraft_, parameterBaseText_, parameterError_;
    nlohmann::json parameterBase_;
    uint64_t parameterSourceGeneration_ = 0, parameterSeenGeneration_ = 0;
    long parameterSeenRevision_ = -1;
    std::string parameterSeenMaterial_, parameterSeenSummary_;
    bool parameterStale_ = false, parameterWasVisible_ = false;
    // Clean drafts follow native setup; edited drafts retain their exact transactional baseline.
    bool parameterDraftDirty_ = false;
    int parameterInputGeneration_ = 0;
    bool parameterEditorOpen_ = false;
    // Study pane scaffold (sim_manager_forms.cpp): stepper Setup sub-chips from the readiness logic.
    struct SetupStep {
        const char* label = nullptr;   // short chip label that fits five to a row ("Temp", "Loads")
        const char* title = nullptr;   // full name: tree row, chip tooltip ("Fixed temperature")
        SimNode     node  = SimNode::Study;
        int         status = 0;        // 0 ready, 1 optional / not set, 2 needs attention
        std::string issue;             // first problem of that node ("" when ready)
    };
    std::vector<SetupStep> setupSteps(AppState& st, const Study& s);
    void drawStudyOptions(AppState& st, Study& s);   // Study-node body per study type
    void drawRunPage(AppState& st, Study& s);        // stepper "Run" page (live progress, traces, last run)
    struct RunPageView { std::string studyId; SimNode node = SimNode::Study; bool sawRunning = false; };
    RunPageView runPage_;                            // Run page shown while studyId/node match
    void drawFixturesForm(AppState& st, Study& s);
    void drawLoadsForm(AppState& st, Study& s);
    void drawMaterialCombo(AppState& st, Study& s);
    void drawMeshForm(AppState& st, Study& s);
    void drawFlowForm(AppState& st, Study& s);     // wind-tunnel setup: fluid, AoA, roughness, ref area
    void syncFlowStudy(AppState& st, Study& s); // normalise flow/thermal-CFD setup outside draw (defined in sim_manager_flow.cpp)
    void drawResultControls(AppState& st, Study& s);
    // Results step with no result: spinner while this study runs, else emptyState "Run <type>"
    // (or blockedState + "Go to <step>") — sim_manager_results.cpp.
    void drawResultsEmpty(AppState& st, Study& s);
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
    void persist(AppState& st, const std::string& docId, Study& s);
    void deleteStudy(AppState& st, int i);
    static nlohmann::json studyToJson(const Study& s);
    static nlohmann::json typedStudyJson(const Study& s);
    static Study studyFromJson(const nlohmann::json& j);

    // fatigue form + result graphs (Goodman / Gerber / Soderberg diagram + S-N curve)
    void drawFatigueForm(AppState& st, Study& s);
    void drawFatigueGraphs(AppState& st, Study& s);

    // run
    void run(AppState& st, Study& s);
    void drainSimClientJob(AppState& st);
    void runStaticOrModal(AppState& st, Study& s);
    void runFlow(AppState& st, Study& s);
    void runFatigue(AppState& st, Study& s);
    // AsyncRpc owns the long worker calls; completion is pinned to document/study/generation.
    void runMotion(AppState& st, Study& s);
    struct MotionRun;
    std::shared_ptr<MotionRun> motionRun_;
    Study* motionRunStudy(AppState& st, const std::shared_ptr<MotionRun>& run);
    void finishMotionRun(AppState& st, const std::shared_ptr<MotionRun>& run,
                         const core::RpcResult& result);
    void invalidateMotion(Study& s);
    // Cache one activation's world-space AICM and stable body/component transform join.
    bool prepareMotionCache(Study& s, const nlohmann::json& activation,
                            const std::string& docId,
                            std::shared_ptr<const render::DocMesh> mesh, std::string& err);
    // Apply frameWorld * inverse(cachedBaseWorld) to the cached world-space vertices, validate the
    // complete frame, then atomically re-upload it. Returns false without touching the renderer.
    bool applyMotionFrame(AppState& st, Study& s, int frame);
    // Validate the per-component position-magnitude trace inputs.
    void buildMotionTrace(Study& s);
    // Inline form for named driver rows / ranges / samples / interference / fps / loop.
    void drawMotionForm(AppState& st, Study& s);
    // Timeline transport bar (rewind / step-back / play-pause / step-fwd / scrub slider).
    void drawMotionTimeline(AppState& st, Study& s);
    void highlightMotionCollisions(AppState& st, const Study& s,
                                   const nlohmann::json* pair = nullptr);
    // Full results panel for a completed motion study (timeline + trace plot).
    void drawMotionResults(AppState& st, Study& s);
    // Advance the playback frame on the main thread (called from tick()).
    void tickMotion(AppState& st);

    // pick capture: append every currently-selected face into `well`.
    void captureSelectionInto(AppState& st, SimWell& well);

    // Piping / internal flow / vessel / duct panel. Its records live in doc["studies"] next to
    // the FEA studies but are kept out of studies_ (refresh hands them over).
    PipingPanel piping_;
    // Hydraulic circuit panel; its hydraulic_circuit records are kept out of studies_ the same way.
    HydraulicPanel hydraulic_;
    std::vector<Study> studies_;
    int sel_ = -1;                  // selected study index (-1 = none)
    SimNode node_ = SimNode::Study; // selected sub-node of sel_
    int loadIdx_ = -1;              // which load is selected when node_==Load

    // Tool-invoked setup panel: a study editor takes over the pane (PropertyManager-style) when a
    // ribbon tool is invoked, a picker kind is chosen or a row is double-clicked, and closes via
    // the header's "Done". While closed the pane shows the study list.
    bool panelOpen_ = false;
    // One-shot ribbon focus (UI standard decision 5): a setup command navigates node_ and names
    // the card it targets; the form that draws that card calls takeFocus("<id>") right before
    // its kit::cardBegin. The first matching call scrolls the pane body so the card sits at the
    // top, clears the request and returns true (callers may flash/open the card). Unclaimed
    // requests expire after a short grace period so a stale target never jumps the page later.
    // Card ids: "material", "fixtures", "load" (the card of loadIdx_), "mesh", "modes" (Modal /
    // Buckling), "strength" (Fatigue S-N data), "conduction", "excitation", "contact",
    // "fluid", "flow" (flow boundaries), "heat" (Thermal CFD heat transfer), "resolution",
    // "drivers" (Motion). Topology entries use topoBcView_.focusPointer instead.
    void requestFocus(const char* cardId);
    bool takeFocus(const char* cardId);
    std::string focusCard_;
    int focusFrame_ = -1;
    // Per-frame pane context (tick): feeds the ribbon the selected study kind and scopes the
    // status-bar line a run of this manager posted to that study — switching study, closing the
    // editor or handing the pane to another panel clears it ("Static FEA complete" never lingers
    // over a Modal / Flow / Hydraulics pane). Defined in sim_manager.cpp.
    void syncPaneContext(AppState& st);
    std::string simStatusText_;     // st.status text attributed to a run of this manager
    std::string simStatusStudyId_;  // study that run belonged to
    std::string lastStatusSeen_;    // st.status as of the previous tick
    std::string lastRunStudyId_;    // study of the last active run
    std::string scopeDocId_, scopeStudyId_; // pane scope at the previous tick: doc, study,
    int scopeFlags_ = -1;                   // editor/takeover/workspace bits (-1 = unset)
    bool statusRunActive_ = false;  // a run of this manager was active at the previous tick
    // Select a study of type t for a type-scoped ribbon tool (prefer the current selection when it
    // already matches, else the first study of that type). Returns false when none exists.
    bool selectStudyOfType(StudyType t);
    // ONE header for every simulation pane (kit::panelHeader): study icon, elided name, dim type
    // chip, status chip, a units popover and "Done". Draws nothing else (the body scaffold owns
    // the hairline). Returns false on the frame "Done" is clicked.
    bool drawPanelHeader(AppState& st, Study& s);
    // Grouped "New study" picker (kit::groupedPicker). openNewStudyPicker() closes any open
    // panel and pops the picker on the next tree draw (pane button + ribbon sim_new_picker).
    void openNewStudyPicker();
    void drawNewStudyPicker(AppState& st);
    bool newPickerRequested_ = false;
    // Row-level delete waits for an inline confirmation (study id; "" = none pending).
    std::string pendingDeleteStudyId_;
    // Sync step (tick): keep studies' material keys in step with the part material and normalise
    // setup the forms used to fix up mid-draw (modal studies carry no loads).
    void syncStudies(AppState& st);
    // Shared Run gate behind run() and runBlocker().
    std::string runBlockerFor(AppState& st, const Study& s) const;
    // AppState the manager last ticked/drew with (runUi()/runBlocker() read it; AppState
    // outlives every DocSession-owned SimManager).
    AppState* appSt_ = nullptr;
    // Footer progress bookkeeping (tick/drain only).
    double runStartTime_ = -1.0;       // ImGui::GetTime() when a run of this manager started
    bool   runWasActive_ = false;
    int    runPct_ = -1;               // last streamed percentage (<0 none)
    std::string runStageText_;         // last streamed stage/text line
    std::string advRunningStudyId_;    // study of the in-flight advRunning_ worker call
    std::string advStage_;             // its stage line
    std::string motionDiscardStudyId_; // Motion study whose pending result Cancel discarded

    // EM (electromagnetics) gating: 0 = not probed yet, 1 = em_* backend available, -1 = missing.
    // Probed ONCE asynchronously (st.rpc) when the Simulate workspace first becomes active; the
    // result also drives ui::setRibbonDisabledPrefix("sim_em_", ...) for the honest disabled state.
    int  emStatus_ = 0;
    bool emProbeInflight_ = false;
    nlohmann::json emStatusResult_;   // cached em_status payload (solvers/defaults/note) for the panel
    void probeEmStatus(AppState& st);
    bool handleEmCommand(AppState& st, const std::string& cmdId);

    // ---- EM (electromagnetics) study UI ----------------------------------------------------------
    // EM is NOT a doc-persisted Study (the em_* backend is stateless one-shot analysis RPCs, no
    // sim_study_* persistence). It gets its own PropertyManager-style panel that takes over the left
    // pane — three forms (eigenmode / driven S-params / time domain), each run async via st.rpc with
    // a dedicated in-flight guard and results parsed onto the manager (plotted with kit::linePlot).
    enum class EmForm { Eigenmode, Driven, TimeDomain };
    struct EmState {
        // Shared waveguide selection. stdIdx: -1 = custom, else index into the WR-xx preset table.
        int   stdIdx = 7;          // default WR-90 (X band) — index into kEmStandards
        int   shape = 0;           // custom cross-section: 0 = rect, 1 = circular
        double a_mm = 22.86;       // rect broad wall (custom)
        double b_mm = 10.16;       // rect narrow wall (custom)
        double radius_mm = 8.0;    // circular radius (custom)

        double epsR = 1.0;
        std::string exactMode;
        nlohmann::json nativeParameters[3] = {nlohmann::json::object(), nlohmann::json::object(), nlohmann::json::object()};
        std::string parameterDraft[3], parameterError[3];
        bool parameterEditorOpen = false;
        nlohmann::json rawResults[3];
        bool parameterDraftLoaded[3] = {false, false, false};
        std::vector<std::string> backendNames[3];
        // Eigenmode inputs.
        int   nModes = 6;
        int   resolution = 64;

        // Driven / time-domain shared geometry length + mode.
        double lengthMm = 50.0;
        int   modeIdx = 0;         // index into kEmModes (TE10 default)

        // Driven sweep.
        double fStartGhz = 8.0;
        double fStopGhz = 12.0;
        int   nFreq = 101;
        char  stlPath[256] = "";   // optional geometry STL (driven)

        // Time domain.
        double f0Ghz = 10.0;
        double bwGhz = 4.0;

        // Per-form results (transient). ok<0 unrun, 0 error, 1 ok.
        int  eigResult = -1, drvResult = -1, tdResult = -1;
        std::string eigError, drvError, tdError;
        nlohmann::json eigModes;                       // array of {name,type,fc_ghz,kc_per_m,analytic_err_pct}
        std::vector<float> drvFreq, drvS11, drvS21;    // driven S-parameter arrays
        std::vector<float> tdTime, tdPort1, tdPort2;   // time-domain traces
    };
    EmState em_;
    bool emPanelOpen_ = false;
    EmForm emForm_ = EmForm::Eigenmode;
    bool emRunning_ = false;               // an em_* run launched by this manager is in flight
    // Form validation is shared by dispatch and draw-time gating. The path cache keeps the
    // optional STL existence probe off the per-frame hot path until the input changes.
    mutable std::string emStlCheckedPath_;
    mutable bool emStlPathExists_ = true;
    const char* emRunBlocker() const;
    void openEmForm(AppState& st, EmForm f);
    void drawEmPanel(AppState& st);        // full panel (header + form + results); returns via emPanelOpen_
    void drawEmForm(AppState& st);         // the active form's inputs
    void runEmForm(AppState& st);          // dispatch the active form's em_* RPC (async)
    nlohmann::json emWaveguideSpec() const; // {standard:...} or {waveguide:{...}} from em_
    nlohmann::json buildEmRequest() const;
    void invalidateEmResults();
    void drawEmParameterEditor();

    // ---- Multiphysics capability doctor + panels ------------------------------
    // sim_caps probe (worker multiphysics seam): CalculiX presence (contact), the
    // vibration post leg, and which cross-sim handoff endpoints are servable. Probed once
    // asynchronously when the Simulate workspace activates; drives honest gating (fw_status
    // pattern: absent legs show doctor guidance, never dead buttons).
    int  capsStatus_ = 0;             // 0 = not probed, 1 = loaded, -1 = probe failed
    bool capsInflight_ = false;
    nlohmann::json caps_;             // cached sim_caps payload
    void probeCaps(AppState& st);
    // fw_status probe for the Emulation study kind (canEmulate + guidance banner).
    int  fwStatus_ = 0;               // 0 = not probed, 1 = loaded, -1 = failed
    bool fwProbeInflight_ = false;
    bool fwCanEmulate_ = false;
    std::string fwHint_;
    void probeFwStatus(AppState& st);

    // Electronics (SPICE) panel — stateless like EM (the ecad_simulate backend runs against
    // the open Schematic doc, not a part study). Analyses: op / dc / ac / tran.
    struct ElState {
        int  docTab = -1;             // docTabs index of the schematic to simulate
        int  analysis = 3;            // 0 op, 1 dc, 2 ac, 3 tran
        float tranStopMs = 5.0f, tranStepUs = 50.0f;
        char  dcSource[64] = "V1";
        float dcStart = 0.0f, dcStop = 5.0f, dcStep = 0.1f;
        float acStartHz = 10.0f, acStopHz = 1e6f; int acPoints = 61;
        std::string docId, paletteDocId, paletteError;
        bool palettePending = false;
        nlohmann::json circuit;
        std::vector<std::string> setupProbes;
        char manualProbe[128] = "";
        double reltol = 1e-3, vntol = 1e-6, gmin = 1e-12, tempC = 27, tranStartMs = 0;
        int maxiter = 100, acScale = 0;
        bool tranUic = false;
        char simParamsJson[32768] = "{}";
        std::string simParamsError;
        // Result (transient). ok<0 unrun, 0 error, 1 ok. resultDocId/resultDocTitle
        // bind the payload to the schematic that launched it even if tabs are reordered or
        // the user selects another schematic before the async callback drains.
        int  result = -1;
        int  resultAnalysis = -1;
        std::string error, headline, resultDocId, resultDocTitle;
        std::vector<std::string> probes;   // trace names available in the last result
        int  probeIdx = 0;
        std::vector<float> xs, ys;         // active trace (tran/dc voltage, ac dB)
        nlohmann::json raw;                // full ecad_simulate payload
    };
    ElState el_;
    bool elPanelOpen_ = false;
    bool elRunning_ = false;
    void openElectronicsPanel(AppState& st);
    void drawElectronicsPanel(AppState& st);
    void runElectronics(AppState& st);
    const char* electronicsRunBlocker(bool dispatchText) const;
    void selectElTrace(int idx);          // rebuild xs/ys from raw for probes[idx]

    // Firmware emulation panel — runs fw_sim_run against an on-disk firmware project
    // (fw_projects), honestly gated on fw_status.can_emulate.
    struct FwSimState {
        bool projectsLoaded = false, projectsPending = false;
        std::vector<std::string> projectIds, projectNames;
        int  projIdx = 0;
        char asserts[256] = "";
        float durationMs = 1000.0f; // target virtual run horizon; host timeout is independent
        sim::FwScenarioEditor scenarioEditor;
        nlohmann::json scenario = nlohmann::json::object(), raw;
        int backend = 0;
        float timeoutS = 0;
        std::string scenarioError;
        char elfPath[2048] = "";
        bool powerProfile = false;
        int  result = -1;             // <0 unrun, 0 error, 1 ok
        std::string error, headline;
        std::vector<std::string> uart; // UART console tail from the run
        std::vector<std::pair<std::string, bool>> assertResults; // (assertion, passed) from the last fw_sim_run
    };
    FwSimState fwsim_;
    bool fwPanelOpen_ = false;
    bool fwRunning_ = false;
    void openEmulationPanel(AppState& st);
    void drawEmulationPanel(AppState& st);
    void runEmulation(AppState& st);

    // Cross-sim handoff panel (aicad.simhandoff/1 seam): ECAD board -> thermal FEA
    // (ecad_thermal_handoff worker mirror) + the heatsink pick forward seam.
    struct HandoffState {
        // Stable ECAD identity. The docTabs vector is presentation order only and may
        // reorder or reuse an index after the source board closes.
        std::string ecadDocId;
        std::string ecadDocTitle;
        int  envIdx = 0;              // 0 still_air, 1 forced_air, 2 cold_plate
        bool traceHeating = false;
        bool running = false;
        std::string error;
        nlohmann::json result;        // last ecad_thermal_handoff payload
        nlohmann::json heatsink;      // last heatsink_select payload
        std::string heatsinkComponentRef;
        int heatsinkTargetKind = 0;   // 0 junction (Tj), 1 case (Tcase)
        float heatsinkTargetC = 0.0f;
        bool heatsinkContextReady = false;
        bool customEnvironment = false, dryRun = false;
        double ambientC = 25, filmWm2K = 10, meshFineness = 0.35;
        char transientJson[8192] = "";
        std::string transientError;
        double timThicknessMm = 0.1, timConductivityWmK = 3;
        bool heatsinkLibrary = true, heatsinkParametric = true;
        int maxCandidates = 8;
        bool heatsinkRunning = false;
    };
    HandoffState ho_;
    bool handoffPanelOpen_ = false;
    void openHandoffPanel(AppState& st, int pcbTab);
    void drawHandoffPanel(AppState& st);
    void runEcadThermalHandoff(AppState& st);
    void runHeatsinkSelect(AppState& st);
    int resolveHandoffDocTab(const AppState& st) const;
    void deriveHeatsinkTarget(const std::string& componentRef);
    std::string heatsinkSetupBlocker(const AppState& st) const;

    // Multiphysics runs.
    void runThermalFea(AppState& st, Study& s);       // SimClient sim_run_thermal
    void runContact(AppState& st, Study& s);          // worker sim_run_contact (async rpc)
    void runFreqResponse(AppState& st, Study& s);     // modal solve, post in tick completion
    void postFreqResponse(AppState& st, Study& s, const nlohmann::json& modalRes,
                          const std::string& docId);
    bool advRunning_ = false;                          // an async worker study run in flight
    // Model bbox from the face-centroid cache (proxy dimensions for contact).
    bool modelBbox(AppState& st, float lo[3], float hi[3]) const;
    // Draw the results block for the multiphysics kinds (tables + line plots, no overlay).
    void drawAdvancedResults(AppState& st, Study& s);
    // One "Provenance: <coupling> — <domain>/<tool>" line when a result carries a
    // simhandoff envelope (study provenance display).
    static void drawProvenance(const nlohmann::json& result);

    // Armed well for viewport picking: -1 none, 0 = fixtures, >=1 = loads[armId-1]. Tied to sel_.
    int armedWell_ = -1;
    static constexpr int kArmFlowDirection = (1 << 20) + 1;

    bool running_ = false;          // a run launched by THIS manager is in flight
    int  runningStudy_ = -1;        // index of the study that launched the in-flight run
    std::string runningStudyId_;    // stable id of the launched study (survives refresh/reorder)
    // Export and selector resolution are AsyncRpc continuations pinned to the
    // source document and a SimClient reservation (also covering flow/thermal/fatigue).
    struct StaticModalPrep;
    void beginExportPrep(AppState& st, Study& s, const std::shared_ptr<StaticModalPrep>& prep);
    void failStaticModalPrep(AppState& st, const std::shared_ptr<StaticModalPrep>& prep,
                             const std::string& message);
    void finishStaticModalPrep(AppState& st, const std::shared_ptr<StaticModalPrep>& prep);
    bool prepFaceResult(AppState& st, const std::shared_ptr<StaticModalPrep>& prep,
                        const core::RpcResult& result, nlohmann::json& destination);
    std::shared_ptr<StaticModalPrep> staticModalPrep_;
    bool prepInFlight_ = false;
    // After loadDocMeshAsync for a result overlay, finish legend/deform once drain commits.
    std::string pendingOverlayStudyId_;

    // ---- topology study integration ------------------------------------------------------------
    static constexpr int kArmTopology = 1 << 20;   // armedWell_ value: topoBcView_.armedPointer
    topology::Capabilities topoCaps_;              // Loading until the async reply lands
    bool topoCapsRequested_ = false, topoCapsInflight_ = false;
    void probeTopologyCaps(AppState& st, bool retry = false);
    topology::SettingsView topoSettingsView_;
    topology::ResultsView topoResultsView_;
    topology::BcView topoBcView_;
    std::string topoViewStudyId_;                  // study the three views belong to
    void syncTopologyViews(const Study& s);        // reset views when the selected study changes
    // Results-panel action deferred from draw to tick (file probes / overlay loads off draw).
    struct TopoPendingAction {
        std::string studyId;
        topology::ResultsAction action;
    };
    std::vector<TopoPendingAction> topoActions_;
    void runTopology(AppState& st, Study& s);
    void cancelTopologyPrep(AppState& st);
    void drawTopologyForm(AppState& st, Study& s);
    void drawTopologyResults(AppState& st, Study& s);
    void drawTopologyRunArea(AppState& st, Study& s);
    void drawTopologyLiveStatus(AppState& st, Study& s); // topology body top: status line + live convergence sparklines (defined in sim/sim_manager_topology.cpp)
    void finishTopologyRun(AppState& st, Study& s, const std::string& ownerDocId, bool uiActive);
    void checkTopologyDfam(AppState& st, Study& s, const std::string& ownerDocId,
                           const std::string& runId, const std::string& printProcess);
    void processTopologyActions(AppState& st);
    void refreshTopologyRunsView(AppState& st, Study& s, bool force = false);
    bool showTopologyRun(AppState& st, Study& s, const std::string& runId);
    void exportTopologyArtifact(AppState& st, Study& s, const std::string& runId,
                                const std::string& artifactKey);
    void continueTopologyInCad(AppState& st, Study& s, const std::string& runId);
    topology::MaterialLookup topologyMaterialLookup() const;
    std::string topologyGeometryKey(AppState& st); // hash of the doc tree minus studies
    // Run gate shared by the panel button, ribbon sim_run and scenarios: capability matrix,
    // method/physics support, scalar settings and boundary/material validation.
    bool topologyReady(AppState& st, const Study& s, std::string& blocker) const;
    std::string topoSetupKeyNow_;                  // setupKey() of the selected study (tick-sampled)
    std::string topoSetupKeyStudy_;
    double topoSetupKeyTime_ = -1.0;
    std::string topoAutoShowKey_;                  // run auto-shown on entering Results
    long topoGeomRevision_ = -1;
    std::string topoGeomDocId_, topoGeomKey_;
    // Run identity carried from prep to completion (the reservation serializes runs).
    struct TopoRunMeta {
        std::string studyId, runId, setupKey, geometryKey, label, printProcess;
        double startedUtc = 0.0;
        nlohmann::json settings;
    };
    TopoRunMeta topoRunMeta_;
    uint64_t topoIterRevision_ = 0;                // last SimClient::iterationRevision() copied
    int topoLiveMaxIter_ = 0;                      // last reported max iterations (0 = unknown)
    std::vector<double> topoLiveIter_, topoLiveObjective_, topoLiveVolume_, topoLiveChange_;
    // Background artifact export (copy off the GUI thread; completion reported in tick()).
    struct TopoExport;
    std::shared_ptr<TopoExport> topoExport_;

    std::string lastDocId_;         // doc id the mirror was last refreshed for (re-pull on change)
    long lastRevision_ = -1;        // engine revision at last refresh
    // Last failed sim_study_list, fenced by (doc, revision): the tree draw does not re-issue the
    // (synchronous) list every frame after a failure — it shows the error with Retry and re-pulls
    // on a new revision, a studies_changed event (markStudiesStale) or the Retry click.
    std::string listErrorDocId_;
    long listErrorRevision_ = -1;
    std::string listError_;

    // Result-view state: whether the FEA result overlay is currently loaded into the viewport, and
    // for which study, so syncResultView only reloads the overlay / restores the model mesh on an
    // actual transition (not every frame). See syncResultView.
    bool resultShown_ = false;
    std::string shownStudyId_;
    // Restore a collision pose's selection overlay before switching studies or model views.
    void restoreMotionHighlight(AppState& st);
    // Re-tessellate the current document and reload its (editable, face-bearing) model mesh,
    // replacing a result overlay. Turns colormap/deformation off first.
    void restoreModelMesh(AppState& st);
    // Finish legend/deform after an overlay mesh commit (sync path or async drain).
    void finishResultOverlay(AppState& st, Study& s);

    // ---- presentation display budget -------------------------------------------------------------
    // Cached overlay geometry is what the GPU has to draw every frame, so it is capped independently
    // of the solver payload. Both caches below are built under a FIXED segment budget with uniform,
    // deterministic strides (same input -> byte-identical output), and both retain the pre-decimation
    // totals so the UI can say "showing X of Y" instead of silently blanking or silently lying.
    static constexpr size_t kPreviewSegmentBudget = 150000;   // emitted mesh-preview GL_LINES segments
    static constexpr size_t kPreviewBoundaryElems = 250000;   // above this, sample elements first
    static constexpr size_t kPreviewSampleElems   = 100000;   // elements kept by that coarse sampling
    static constexpr size_t kFlowSegmentBudget    = 120000;   // emitted flow line+barb segments

    // Mesh-preview state (built by previewMesh from sim_mesh; drawn in the viewport while setting up).
    std::vector<float> meshPreviewSegs_;   // flat GL_LINES xyz pairs (surface element edges)
    std::string meshPreviewStudyId_;       // study the preview belongs to ("" = none)
    bool meshPreviewPending_ = false;      // sim_mesh async in flight
    // Honest LOD bookkeeping for the wireframe above (0 = nothing built).
    size_t meshPreviewElemsTotal_ = 0;     // elements the mesher actually produced
    size_t meshPreviewElemsUsed_  = 0;     // elements this wireframe was derived from
    size_t meshPreviewEdgesTotal_ = 0;     // unique candidate edges before the budget stride
    size_t meshPreviewEdgesShown_ = 0;     // edges actually emitted
    bool   meshPreviewCoarse_ = false;     // true = element-sampled cloud, not the exact boundary

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
    size_t flowSegsTotal_ = 0;             // candidate line segments in the overlay
    size_t flowSegsShown_ = 0;             // retained source field segments (decorative barbs excluded)
    uint32_t flowSegStride_ = 1;           // uniform ingestion stride applied to hit the budget
    void rebuildFlowLines(AppState& st, Study& s);  // (re)parse the overlay AICM into the band arrays

    // Bumped by every path that mutates meshPreviewSegs_ / flowStreamSegs_ / flowSliceSegs_.
    uint64_t overlayGeneration_ = 1;
    void bumpOverlayGeneration() { ++overlayGeneration_; }

    // Motion study: this SimManager is owned by one DocSession, so keeping the cache here preserves
    // playback across workspace/document-tab switches without leaking it to another document.
    // AICM vertices are world-space; base transforms are keyed by stable component id and removed
    // from each requested frame before upload.
    std::shared_ptr<const render::DocMesh> motionDocMesh_;
    std::string motionCacheDocId_;
    std::map<uint32_t, std::string> motionComponentByBodyId_;
    std::map<std::string, std::array<double, 16>> motionInverseBaseWorldByComponentId_;
    uint64_t modelRestoreGeneration_ = 0;
    bool motionHighlightPose_ = false; // shared selection overlay temporarily carries a sampled pose
    std::string motionHighlightStudyId_; // identity of the pose currently held by the selection overlay

    // Custom-material editor scratch (filled by the "+ New material…" popup).
    char cmName_[64] = "";
    float cmE_ = 70000.0f, cmNu_ = 0.30f, cmDensity_ = 2.7f, cmYield_ = 250.0f;
    float cmUlt_ = 310.0f, cmEnd_ = 96.0f;  // ultimate (Sut) + endurance (Se), MPa
};

}  // namespace ui
