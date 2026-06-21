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

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace core { class SimClient; }

namespace ui {

enum class StudyType { Static, Modal, Flow, Fatigue };

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
    float meshDensity = 0.5f;
    int meshOrder = 2;              // 1 = draft (linear tet4), 2 = high quality (quadratic tet10).
                                    // 2nd order captures bending accurately (linear tets shear-lock).
    float meshTargetMm = 0.0f;      // 0 = auto from density
    float meshMinMm = 0.0f;         // 0 = auto
    float meshMaxMm = 0.0f;         // 0 = auto
    bool meshOptimize = true;       // gmsh post-generation optimization pass
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

    // Flow
    int flowDir = 0;                // 0..5 = +X,-X,+Y,-Y,+Z,-Z
    float flowSpeed = 10.0f;        // m/s
    int flowPreset = 0;             // 0 coarse,1 medium,2 fine,3 xfine

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
    // Load arrows: world anchor (face-pick centroid) + direction for each force/pressure load of the
    // selected study. dirOut points the force vector (or -normal for pressure, approximated by vec).
    struct LoadArrow { float at[3]; float dir[3]; bool pressure; std::string mag; };
    void collectLoadArrows(AppState& st, std::vector<LoadArrow>& arrows) const;

    // Build world-space indicator geometry for the selected study's setup, sized to the model: red
    // arrows for force/pressure loads (force along the vector, pressure into the face) and green
    // ground/clamp glyphs at fixed faces. Anchors/normals come from st.faceNormalCache. Flat
    // [x0,y0,z0,x1,y1,z1,…] GL_LINES batches, ready to hand to Overlays::drawSegments.
    void collectIndicatorSegments(AppState& st, std::vector<float>& loadSegs,
                                  std::vector<float>& fixtureSegs) const;

    // Mesh-preview wireframe (surface element edges from the last "Preview mesh") for the selected
    // study, or an empty batch. Only valid while the Sim tab is active and the preview matches sel_.
    bool hasMeshPreview(AppState& st) const;
    const std::vector<float>& meshPreviewSegments() const { return meshPreviewSegs_; }

    // Number of studies the active document carries (mirror count after the last refresh). Used by
    // the tab-switch hook to auto-reveal the Simulation workspace when a part has studies.
    int  studyCount() const { return (int)studies_.size(); }

    // ---- scripted-shot helpers (AI_CAD_SHOT=fea), preserved from the old SimPanel ----
    int  activeStudy() const { return sel_; }
    void configureActive(AppState& st, float density, const std::string& materialKey);

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
    void drawFixturesForm(AppState& st, Study& s);
    void drawLoadsForm(AppState& st, Study& s);
    void drawMaterialCombo(AppState& st, Study& s);
    void drawMeshForm(AppState& st, Study& s);
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

    // Custom-material editor scratch (filled by the "+ New material…" popup).
    char cmName_[64] = "";
    float cmE_ = 70000.0f, cmNu_ = 0.30f, cmDensity_ = 2.7f, cmYield_ = 250.0f;
    float cmUlt_ = 310.0f, cmEnd_ = 96.0f;  // ultimate (Sut) + endurance (Se), MPa
};

}  // namespace ui
