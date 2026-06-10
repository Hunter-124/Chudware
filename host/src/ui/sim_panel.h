// Simulation panel (C19): a dockable "Simulation" panel that defines and runs FEA / CFD studies
// against the live document, then drives the result visualisation (colormap + legend + deform
// slider for FEA; slices + streamlines + drag readout for CFD).
//
// Studies are local UI state; the heavy lifting is files + JSON handed to the SimClient (worker
// #2). A study's Run flow: doc_export_step (live bodies -> temp STEP) -> doc_face_points (resolve
// each fixture/load selector to a surface point cloud) -> sim_mesh -> sim_run_static/modal/flow.
// Progress events drive a progress bar (+ a live drag sparkline for flow). On completion the
// overlay AICM is loaded into the viewport with the colormap on and the legend populated.
//
// Reachable from the ribbon Simulation tab (commands routed here from app.cpp) and from View.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace core { class SimClient; }

namespace ui {

enum class StudyType { Static, Modal, Flow };

// One selector well's captured picks (resolved selector JSON + a short chip label).
struct SimWell {
    std::vector<nlohmann::json> selectors;  // resolved face selectors
    std::string label() const;
};

// One load entry (force vector in N, or pressure in MPa).
struct SimLoad {
    SimWell faces;
    bool pressure = false;          // false = force vector, true = pressure
    float vec[3] = {0, 0, -100};    // N (force)
    float pressureMpa = 0.1f;       // MPa (pressure)
};

// A study definition + its last result.
struct Study {
    StudyType type = StudyType::Static;
    std::string name;

    // FEA
    int materialIndex = 0;          // index into the loaded materials list
    SimWell fixtures;
    std::vector<SimLoad> loads;
    float meshDensity = 0.5f;
    int nModes = 6;                 // modal

    // Flow
    int flowDir = 0;                // 0..5 = +X,-X,+Y,-Y,+Z,-Z
    float flowSpeed = 10.0f;        // m/s
    int flowPreset = 0;             // 0 coarse,1 medium,2 fine,3 xfine

    // Result state
    bool hasResult = false;
    std::string summary;            // human result summary text
    std::string banner;            // permanent CFD qualitative-result banner (reynolds_note)
    std::vector<float> dragTrace;   // live CFD drag sparkline samples
};

class SimPanel {
public:
    SimPanel();

    // Draw the panel (dockable window). Reads/writes AppState; talks to st.simClient.
    void draw(AppState& st);

    // Handle a ribbon Simulation-tab command (sim_new_static / sim_new_modal / sim_new_flow /
    // sim_material / sim_fixture / sim_load_force / sim_load_pressure / sim_mesh / sim_run /
    // sim_results). Returns true if consumed. Opens/focuses the panel as needed.
    bool handleCommand(AppState& st, const std::string& cmdId);

    // Drain SimClient progress + completion on the main thread (call every frame). Loads the
    // result overlay + turns on the colormap/legend when a study finishes.
    void tick(AppState& st);

    int activeStudy() const { return active_; }

    // Scripted-shot helper: set the active study's mesh density + material index (no-op if none).
    void configureActive(float density, int materialIndex);
    // Find a loaded material's index by key (e.g. "steel_1018"); -1 if not present.
    int materialIndexByKey(const std::string& key) const;

private:
    void ensureMaterials(AppState& st);
    void newStudy(AppState& st, StudyType t);
    void runActive(AppState& st);
    void runStaticOrModal(AppState& st, Study& s);
    void runFlow(AppState& st, Study& s);
    nlohmann::json facePointsFor(AppState& st, const nlohmann::json& selector, std::string& err);
    void drawStaticForm(AppState& st, Study& s);
    void drawModalForm(AppState& st, Study& s);
    void drawFlowForm(AppState& st, Study& s);
    void drawResultControls(AppState& st, Study& s);
    void captureSelectionInto(AppState& st, SimWell& well);

    std::vector<Study> studies_;
    int active_ = -1;

    // Materials parsed from engine/sim/materials.json (key + display name + props).
    struct Material {
        std::string key, name;
        double E_mpa = 0, nu = 0, density_g_cm3 = 0, yield_mpa = 0;
    };
    std::vector<Material> materials_;
    bool materialsLoaded_ = false;

    std::string lastStepPath_;   // temp STEP from the last export (reused for the overlay)
    bool running_ = false;       // a study run is in flight (mirrors SimClient::busy)
};

}  // namespace ui
