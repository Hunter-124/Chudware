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

enum class StudyType { Static, Modal, Flow };

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
    int nModes = 6;                 // modal

    // Flow
    int flowDir = 0;                // 0..5 = +X,-X,+Y,-Y,+Z,-Z
    float flowSpeed = 10.0f;        // m/s
    int flowPreset = 0;             // 0 coarse,1 medium,2 fine,3 xfine

    // Result state (transient — regenerated on Run; never persisted).
    bool hasResult = false;
    std::string summary;            // human result summary text
    std::string banner;             // permanent CFD qualitative-result banner (reynolds_note)
    std::vector<float> dragTrace;   // live CFD drag sparkline samples
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

    // ---- scripted-shot helpers (AI_CAD_SHOT=fea), preserved from the old SimPanel ----
    int  activeStudy() const { return sel_; }
    void configureActive(AppState& st, float density, const std::string& materialKey);

    // Material lookup (read-only singleton parsed from engine/sim/materials.json).
    struct Material {
        std::string key, name;
        double E_mpa = 0, nu = 0, density_g_cm3 = 0, yield_mpa = 0;
    };
    static const std::vector<Material>& materials();
    static int materialIndexByKey(const std::string& key);

private:
    // tree + form
    void drawStudyRow(AppState& st, int i);
    void drawForm(AppState& st, Study& s);
    void drawFixturesForm(AppState& st, Study& s);
    void drawLoadsForm(AppState& st, Study& s);
    void drawMaterialCombo(AppState& st, Study& s);
    void drawResultControls(AppState& st, Study& s);
    void drawWellRow(AppState& st, const char* label, SimWell& well, int armId, const char* tip);

    // engine mutation
    void newStudy(AppState& st, StudyType t);
    void persist(AppState& st, Study& s);          // sim_study_edit for one study
    void deleteStudy(AppState& st, int i);
    static nlohmann::json studyToJson(const Study& s);
    static Study studyFromJson(const nlohmann::json& j);

    // run
    void run(AppState& st, Study& s);
    void runStaticOrModal(AppState& st, Study& s);
    void runFlow(AppState& st, Study& s);
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

    std::string lastDocId_;         // doc id the mirror was last refreshed for (re-pull on change)
    long lastRevision_ = -1;        // engine revision at last refresh
};

}  // namespace ui
