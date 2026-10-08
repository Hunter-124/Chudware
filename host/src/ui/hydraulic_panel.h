// HydraulicPanel — the human surface for fluid-power (hydraulic circuit) design and simulation.
//
// Owned by each document's SimManager (so it follows the doc tab exactly like the FEA studies and
// the Piping panel) and opened from the Simulate workspace's "Hydraulics" ribbon tab (sim_hyd_*
// commands, routed by SimManager::handleCommand). It drives the SAME native RPCs the agent's
// hydraulic tools call (services/mcp/hydraulic_tools.py → core/hydraulic):
//
//   hydraulic_templates / hydraulic_new / hydraulic_add_component / hydraulic_add_hose  circuit edits
//   hydraulic_size_cylinder / hydraulic_size_pump / hydraulic_size_motor / pump_drive_size  sizing
//   hydraulic_simulate        lumped-parameter transient (valve schedule, loads, fluid)
//   hydraulic_bom             sourcing bill of materials
//   hydraulic_piping_handoff  hoses → aicad.piping/1 network (opened in the Piping panel)
//
// The circuit and every result persist into the document as ONE `hydraulic_circuit` study record
// per circuit (sim_study_add / sim_study_edit through ui::sim::StudyRecordStore), shape shared with
// the MCP layer:
//   {type:"hydraulic_circuit", name, engine:"cpp-hydraulic",
//    params:{circuit, valve_schedule, loads, fluid, initial, duration_s, output_dt_s,
//            layout3d:{component_id:[x_mm,y_mm,z_mm]}, max_step_s?,
//            piping_handoff?:{native hydraulic_piping_handoff inputs}, ...native keys},
//    results:{simulate?, bom?, piping_handoff?, sizing:{cylinders, pumps, motors, drives}}}
// Simulation series are downsampled to at most kMaxStoredSamples points before they are stored.
// Native JSON editors retain unmodeled parameters without claiming solver support. Circuit's
// inline palette supports drag-to-schematic and exact numeric placement; clicking or dragging
// between ports connects them immediately with editable physical hose geometry.
// 3D Layout leaves the native viewport in charge and publishes cached physical glyphs and hoses.
// Default positions preserve schematic XY ordering, with glyph-sized clearance (including full
// cylinder travel); explicit params.layout3d positions remain authoritative and are shared with
// hydraulic_layout_3d. Peak simulated node pressure colours hoses; rods use final stroke.
// The central 3D Layout view exposes Circuit's placement controls without a second view toggle.
// Select a component then Place in 3D, or double-click its glyph; one plane click places it.
// The viewport keeps only a bottom hint/legend HUD and reserves its rect for the triad.
// Modeled fluid scalars are validated atomically before adoption; native null/default fields and
// metadata remain authored. Zero hose length, handoff load pressure and verification tolerance
// are preserved; optional load-pressure presence is independent of its numeric value.
//
// Pane anatomy: kit::panel scaffold — pinned record header (Undo, Redo, Delete, Done), one row of
// Circuit · Design · Simulate · Results tabs, Design's Cylinder · Pump · Motor · Drive chips and
// Results' Outcome · BOM · To Piping chips. Circuit starts with native templates, then presents
// its component list, inline palette and selection editor, with hoses/manual connection folded.
// Simulate contains setup only; Results shows outcome, metrics, plots access and folded details.
// A pinned footer carries the page's ONE primary action, readiness blocker and request status.
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <string>
#include <vector>
#include <utility>

#include <nlohmann/json.hpp>

#include "sim/hydraulic_canvas_state.h"
#include "sim/sim_common.h"
#include "sim/sim_scene.h"
#include "sim/sim_pump_drive.h"

struct AppState;

namespace ui {

// Index of the "Hydraulics" tab in widgets/ribbon.cpp kRibbonTabs[] (append-only; Simulate only).
constexpr int kRibbonTabHydraulics = 23;

class HydraulicPanel {
public:
    enum class Form { Circuit, Design, Simulate, Results };
    enum class SizeKind { Cylinder, Pump, Motor, Drive };
    enum class ResultPage { Outcome, Bom, Piping };
    // Internal reveal targets; Circuit is one page, never a second navigation strip.
    enum class CircuitPage { Components, Add, Hoses, Connect };
    // What the central Viewport window shows while the panel is open: the full-size ISO 1219
    // schematic, the full-size simulation plots, or the normal 3D view.
    enum class CentralView { Schematic, Plots, View3D };

    // What "Open in Piping panel" hands to SimManager (consumed once via takePipingOpen).
    struct PipingOpen {
        nlohmann::json pipingDoc;   // aicad.piping/1
        nlohmann::json boundary;    // piping boundary rows
        nlohmann::json fluid;       // resolved solver fluid {name, rho, mu}
        nlohmann::json solve;       // the handoff's live piping solve
    };

    HydraulicPanel();

    // ---- persisted records ------------------------------------------------------------------
    static bool isHydraulicStudyType(const std::string& type);
    // Replace the record mirror with this document's hydraulic_circuit records (sim_study_list).
    // Adopts the first record when the panel holds no circuit, and picks up records another
    // client (an agent) changed since the panel last saw them.
    void setStudies(AppState& st, const std::string& docId,
                    const std::vector<nlohmann::json>& records);
    int studyCount() const { return store_.count(); }
    // Rows under the Simulation tree's "Piping, hydraulics & industrial" group (header drawn by
    // SimManager).
    void drawStudyRows(AppState& st);

    // ---- commands + panel ----------------------------------------------------------------------
    // sim_hyd_new / _add / _hose / _size_cyl / _size_pump / _size_motor / _drive / _simulate /
    // _bom / _piping.
    bool handleCommand(AppState& st, const std::string& cmdId);
    // Non-running navigation, shared by the form tabs and scripted surface coverage.
    void openSimulationSetup() { open_ = true; setForm(Form::Simulate); }
    void openResults(ResultPage page = ResultPage::Outcome) {
        open_ = true; setForm(Form::Results); setResultPage(page);
    }
    bool isOpen() const { return open_; }
    void close() {
        open_ = false; dragging_ = -1; placing3d_ = false;
        canvas_.connectArmed = false; templatesFetched_ = false; confirmDelete_ = false;
    }
    // Draws the panel (takes over the Simulation pane like the Piping panel).
    void draw(AppState& st);

    // ---- central view (hosted by the Viewport window via SimManager) --------------------------
    // True while the open panel replaces the 3D view with its Schematic or Plots canvas.
    bool ownsCentralView() const { return open_ && central_ != CentralView::View3D; }
    CentralView centralView() const { return central_; }
    void setCentralView(CentralView v) {
        if (v == CentralView::View3D && central_ != v) frame3d_ = true;
        central_ = v; dragging_ = -1;
        if (v != CentralView::View3D) placing3d_ = false;
    }
    // Paints the Schematic / Plots canvas into the current (central Viewport) window, clear of the
    // pinned glass columns and the doc-tab band, under the Schematic | Plots | 3D toggle.
    void drawCentralView(AppState& st);
    // Over the native viewport: readable bottom hint/pressure chip, with its HUD rect reserved.
    // The scene hook publishes the shared colour-bar legend at 0..peak node pressure (bar).
    void drawViewToggleHud(AppState& st);
    // True once after the user asked to open the handoff network in the Piping panel.
    bool takePipingOpen(PipingOpen& out);

    // ---- scripted entry points (CHUDWARE_SHOT=hydraulics) — the same calls the buttons make ----
    bool newCircuit(AppState& st, const std::string& name);
    // Same native template adoption path used by the Start cards and screenshot scenarios.
    bool applyTemplate(AppState& st, const std::string& templateId);
    bool addComponent(AppState& st, const std::string& type, const std::string& name, double x,
                      double y, const nlohmann::json& params);
    // ends: {component_id, port} pairs (>= 2). Geometry is stored on the hose (id_mm/length_mm
    // are read by the simulator for line volume and by the piping handoff).
    bool addHose(AppState& st, const std::string& netName,
                 const std::vector<std::pair<std::string, std::string>>& ends, double lengthMm,
                 double idMm, double ratingBar);
    // Canvas and expert editors share these canonical mutation paths. Coordinates are native
    // schematic millimetres; JSON uses the native key units (not display-unit overrides).
    bool selectPortEndpoint(AppState& st, const std::string& componentId, const std::string& port);
    bool setComponentParams(AppState& st, const std::string& componentId,
                            const nlohmann::json& params);
    bool configureHose(AppState& st, const std::string& hoseId,
                       const nlohmann::json& definition, std::string& error);
    nlohmann::json simulationDefinition() const;
    bool configureSimulation(AppState& st, const nlohmann::json& definition, std::string& error);
    nlohmann::json handoffDefinition() const;
    bool configureHandoff(AppState& st, const nlohmann::json& definition, std::string& error);
    std::pair<float, float> schematicScreenPosition(double x, double y) const;
    std::pair<float, float> portScreenPosition(const std::string& componentId,
                                             const std::string& port) const;
    void setCylinderInputs(const std::string& componentId, double boreMm, double rodMm,
                           double pressureBar, double strokeMm, double velocityMmS);
    void setDriveFromPump(const std::string& pumpComponentId);
    bool runSizeCylinder(AppState& st);
    bool applyCylinderSizing(AppState& st);
    bool runSizePump(AppState& st);
    bool runSizeMotor(AppState& st);
    bool runPumpDrive(AppState& st);
    void clearSchedule();
    void addScheduleRow(double tS, const std::string& valveId, const std::string& position);
    void setLoad(const std::string& componentId, double forceN, double massKg);
    void setDuration(double seconds);
    bool runSimulate(AppState& st);
    bool runBom(AppState& st);
    bool runPipingHandoff(AppState& st);
    // ---- editing seams shared by the side pane and the schematic canvas ----
    // Circuit-level undo/redo: every topology/parameter/position mutation pushes the prior
    // circuit (+ layout3d) snapshot; undo/redo restore it through the normal persist path.
    bool canUndo() const;
    bool canRedo() const;
    bool undo(AppState& st);
    bool redo(AppState& st);
    // One-gesture connection from the canvas: joins (componentA, portA) and (componentB, portB)
    // with a new hose using the current default hose geometry; an endpoint already on a hose
    // joins that hose (tee) instead of creating a parallel one.
    bool connectPorts(AppState& st, const std::string& componentA, const std::string& portA,
                      const std::string& componentB, const std::string& portB);
    // Removes the selected component (and its dangling hose ends) or the selected hose.
    bool deleteSelection(AppState& st);
    bool busy() const { return store_.inflight() > 0 || slot_.addPending; }
    const nlohmann::json& circuit() const { return circuit_; }
    const nlohmann::json& simulateResult() const { return sim_; }
    const nlohmann::json& cylinderResult() const { return cylRes_; }
    const nlohmann::json& driveResult() const { return driveRes_; }
    const nlohmann::json& bomResult() const { return bom_; }
    const nlohmann::json& handoffResult() const { return handoff_; }
    // Id of the component in circuit() named `name` ("" when absent).
    std::string componentIdByName(const std::string& name) const;
    // Last refusal/failure text of the most recent action ("" = none).
    const std::string& lastError() const { return store_.lastError(); }
    const std::string& studyId() const { return slot_.id; }
    int selectedComponentIndex() const { return selected_; }
    // Physical layout lives in params.layout3d, never in the native schematic component schema.
    const nlohmann::json& layout3d() const { return layout3d_; }
    std::uint64_t circuitRevision() const { return circuitGeneration_; }
    std::uint64_t resultRevision() const { return resultGeneration_; }
    std::uint64_t layoutRevision() const { return layoutGeneration_; }
    std::string selectedComponentId() const;
    void selectComponent3d(const std::string& id);
    void armPlacement3d(bool armed);
    bool placing3d() const { return placing3d_; }
    bool moveComponent3d(AppState& st, const std::string& id, const float position[3]);
    void componentPosition3d(const nlohmann::json& component, float position[3]) const;
    // Conservative local envelope of the rendered glyph, including ports and full actuator travel.
    static void componentEnvelope3d(const nlohmann::json& component, float lo[3], float hi[3]);
    simscene::Plane placementPlane = simscene::Plane::XY;
    double placementOffsetMm = 0.0, placementGridMm = 10.0;
    bool placementSnap = true;
    bool takeFrame3d() { const bool request = frame3d_; frame3d_ = false; return request; }

    // At most this many samples per stored simulation series.
    static constexpr int kMaxStoredSamples = 400;

private:
    struct ScheduleRow {
        double t = 0.0;
        std::string valve;         // component id of a valve_directional
        int pos = 0;               // 0 a, 1 b, 2 center, 3 proportional
        double frac = 0.0;         // proportional opening in [-1, 1] (+ = a side)
        nlohmann::json native = nlohmann::json::object(); // retain unmodeled native row keys
        bool edited = false;
    };
    using Done = void (HydraulicPanel::*)(AppState&, bool, const nlohmann::json&, const std::string&);

    // ---- drawing ----
    void syncForDraw();                     // normalise draw-time state before any widget
    void setForm(Form f);
    void setCircuitPage(CircuitPage p);
    void setSizeKind(SizeKind k);
    void setResultPage(ResultPage p);
    void drawSubPages();                    // pinned chip row under the form tabs
    bool drawViewToggle();
    void drawFooter(AppState& st);
    void drawCircuitForm(AppState& st);
    void drawStart(AppState& st);
    void drawLayout3d();
    void drawSchematic(AppState& st, float width, float height);
    void drawPlots(AppState& st, float width, float height);
    void selectFromCanvas(int component, int hose);
    void drawComponentEditor(AppState& st);
    void drawAddComponent(AppState& st);
    void drawHoses(AppState& st, int onlyIndex = -1);
    void drawConnect(AppState& st);
    bool addPickedComponent(AppState& st);  // inline palette's Add action
    bool connectEnds(AppState& st);         // manual connection's own action
    void drawSizingForm(AppState& st);
    void drawCylinderSizing(AppState& st);
    void drawPumpSizing(AppState& st);
    void drawMotorSizing(AppState& st);
    void drawDriveSizing(AppState& st);
    void drawSimulateForm(AppState& st);
    void drawSimResults(AppState& st);
    void drawResultsForm(AppState& st);
    void drawBomForm(AppState& st);
    void drawPipingForm(AppState& st);

    // ---- readiness: "" = the action can run, else the reason it cannot (shown inline) ----
    std::string cylinderBlocker() const;
    std::string pumpBlocker() const;
    std::string motorBlocker() const;
    std::string driveBlocker() const;
    std::string simulateBlocker() const;
    std::string bomBlocker() const;
    std::string handoffBlocker() const;
    std::string connectBlocker() const;
    // Circuit's next step: Add (no components), Connect (open required ports or no hoses),
    // otherwise Simulate.
    enum class NextStep { Add, Connect, Simulate };
    NextStep circuitNextStep() const;

    // ---- circuit helpers ----
    int componentIndex(const std::string& id) const;
    // Display name of component `id` (its name, else the type label, else the id).
    std::string componentName(const std::string& id) const;
    std::vector<int> componentsOfType(const char* type) const;
    void removeComponent(AppState& st, int index);
    void removeHose(AppState& st, int index);
    void circuitChanged(AppState& st);   // topology/params edit: drop stale simulate/bom/handoff
    // Also used on schematic drag release: pushes the prior committed snapshot exactly once.
    void commitCircuitMutation(AppState& st, bool invalidate = true);
    struct Snapshot {
        nlohmann::json circuit, layout3d, simulation;
    };
    Snapshot snapshot() const;
    void resetHistory();
    void restoreSnapshot(AppState& st, Snapshot state);
    void invalidateResults();
    void seedConnectDefaults();
    void seedHandoffDefaults();
    double pumpFlowLpm(const nlohmann::json& pump) const;
    double reliefPressureBar() const;
    nlohmann::json simulateParams() const;
    nlohmann::json scheduleJson() const;

    // ---- persistence ----
    nlohmann::json record() const;
    void adopt(const nlohmann::json& record);
    void persist(AppState& st);
    void storeSizing(AppState& st, const char* group, const std::string& key,
                     const nlohmann::json& result);

    // Issue an engine RPC through the record store (completion runs only while the panel lives)
    // and start the footer's elapsed clock.
    void call(AppState& st, const char* method, nlohmann::json params, Done done, int timeoutMs);
    void onNew(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void fetchTemplates(AppState& st);
    void onTemplates(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onAdd(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onHose(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onCylinder(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onPump(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onMotor(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onDrive(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onSimulate(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onBom(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onHandoff(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void setStatus(const std::string& text, int tone) { store_.setStatus(text, tone); }
    // "Cylinder sized." (ok) or "Cylinder sized · 1 warning." (warn): a warn tone always says why.
    void setDoneStatus(const char* what, const nlohmann::json& result);

    // Fresh calculations and setup blockers scroll their matching card into view once.
    enum class Reveal { None, Cylinder, Pump, Motor, Drive, Simulate, Handoff };
    void revealHere(Reveal r);
    Reveal reveal_ = Reveal::None;

    sim::StudyRecordStore store_;
    sim::StudyRecordStore::Slot slot_;
    bool open_ = false;
    Form form_ = Form::Circuit;
    SizeKind sizeKind_ = SizeKind::Cylinder;
    ResultPage resultPage_ = ResultPage::Outcome;
    bool startVisible_ = true, addOpen_ = false, hosesOpen_ = false, connectOpen_ = false;
    bool confirmDelete_ = false;
    bool templatesFetched_ = false, templatesPending_ = false, templateApplying_ = false, templateChoice_ = false;
    nlohmann::json templates_ = nlohmann::json::array();
    std::string templatesError_;
    bool focusSimulationBlocker_ = false;
    hydcanvas::State canvas_;
    CentralView central_ = CentralView::Schematic;
    double callStart_ = 0.0;       // ImGui time the in-flight call started (footer elapsed)

    // ---- circuit ----
    nlohmann::json circuit_;       // aicad.hydraulic/1
    std::uint64_t circuitGeneration_ = 0;
    std::uint64_t resultGeneration_ = 1, layoutGeneration_ = 1;
    nlohmann::json layout3d_ = nlohmann::json::object();
    Snapshot committed_;
    std::vector<Snapshot> undo_, redo_;
    bool restoring_ = false;
    void ensureDefaultLayout3d() const;
    mutable std::uint64_t defaultCircuitRevision_ = ~std::uint64_t{0};
    mutable std::uint64_t defaultLayoutRevision_ = ~std::uint64_t{0};
    mutable std::map<std::string, std::array<float, 3>> defaultLayout3d_;
    bool placing3d_ = false;
    bool frame3d_ = true;
    struct SchematicSeg { ImVec2 a, b; int hose; };
    struct SchematicRoute { std::vector<ImVec2> pts; int hose; };
    std::vector<SchematicSeg> schematicSegs_;
    std::vector<SchematicRoute> schematicRoutes_;
    std::vector<std::pair<ImVec2, int>> schematicJunctions_;
    std::vector<std::pair<std::string, std::string>> schematicConnected_;
    std::uint64_t schematicRevision_ = ~std::uint64_t{0};
    float schematicRect_[4] = {}, schematicH_ = 0.0f;
    std::map<std::string, float> schematicPressurePeaks_;
    std::uint64_t schematicPressureRevision_ = 0;
    int selected_ = -1;            // selected component index
    int selectedHose_ = -1;
    int dragging_ = -1;            // component index being dragged on the schematic
    bool dragMoved_ = false;
    // Schematic px/mm, centre (mm) and symbol half-size (px); frozen while a symbol is dragged.
    double viewK_ = 0.0, viewCx_ = 0.0, viewCy_ = 0.0;
    float viewH_ = 0.0f;
    bool focusEditor_ = false;     // scroll the selected component's editor into view once
    bool focusHose_ = false;       // scroll the selected hose's card into view once
    float viewMidX_ = 0.0f, viewMidY_ = 0.0f;
    std::string endpointComponent_, endpointPort_; // pending click/drag connection source

    // ---- add component form ----
    int addType_ = 0;
    std::string addName_;
    double addX_ = 0.0, addY_ = 0.0;
    int addParamsType_ = -1;
    nlohmann::json addParams_ = nlohmann::json::object();
    struct JsonDraft {
        nlohmann::json source;
        std::string text, error;
    };
    std::map<std::string, JsonDraft> jsonDrafts_;
    bool drawNativeJson(const char* id, const char* title, const nlohmann::json& value,
                        nlohmann::json& edited, bool disclosed = false);

    // ---- connect form ----
    struct End {
        int comp = -1;             // component index
        int port = 0;              // index into the component's port list
    };
    std::vector<End> ends_;
    std::string hoseNet_;
    double hoseLengthMm_ = 1000.0, hoseIdMm_ = 12.7, hoseRatingBar_ = 280.0;
    nlohmann::json pendingHoseGeom_; // geometry for the hose the in-flight add creates

    // ---- sizing ----
    std::string cylTarget_, pumpTarget_, motorTarget_, driveTarget_;
    double cylBore_ = 63.0, cylRod_ = 36.0, cylPressure_ = 160.0, cylStroke_ = 300.0;
    double cylVelocity_ = 100.0, cylEff_ = 0.95, cylForceKn_ = 30.0;
    std::string boreNote_;         // "Suggest bore from force" outcome, shown in the Cylinder card
    double pumpFlow_ = 20.0, pumpPressure_ = 160.0, pumpEff_ = 0.85, pumpSpeed_ = 1450.0;
    double motTorque_ = 200.0, motSpeed_ = 300.0, motPressure_ = 150.0;
    double motMech_ = 0.9, motVol_ = 0.95;
    int motType_ = 0;              // 0 any, 1 gear, 2 vane, 3 piston, 4 orbital
    sim::PumpDriveInputs drive_;   // the shared pump-drive form's inputs
    nlohmann::json cylRes_, pumpRes_, motorRes_, driveRes_;   // last result shown per form
    // results.sizing: {cylinders, pumps, motors, drives}, each keyed by component id ("manual"
    // when the sizing is not linked to a component).
    nlohmann::json sizing_ = nlohmann::json::object();

    // ---- simulate ----
    std::vector<ScheduleRow> schedule_;
    nlohmann::json loads_ = nlohmann::json::object();    // {cid:{force_n, mass_kg, friction_n, torque_nm, inertia_kgm2}}
    nlohmann::json initial_ = nlohmann::json::object();  // {cid:{position_mm}}
    double durationS_ = 5.0, outputDtS_ = 0.01;
    double fluidRho_ = 870.0, fluidBeta_ = 15000.0, fluidCst_ = 32.0;
    nlohmann::json fluid_ = nlohmann::json::object();
    nlohmann::json simulationExtra_ = nlohmann::json::object();
    nlohmann::json simulationJsonCache_; // rebuild the expert editor snapshot only after edits
    bool simulationJsonDirty_ = true;
    bool parametersDirty_ = false; // legacy scripted setters persist on the next draw/save
    bool simDetailsOpen_ = false;  // Simulate results "Details" card (collapsed by default)
    nlohmann::json sim_;           // stored (downsampled) hydraulic_simulate result
    int seriesPick_ = 0;           // "Component series" plot selection
    struct PlotCell {
        const char* title;
        const char* icon;
        const char* id;
        std::string yTitle, unit;
        std::vector<std::string> labels;
        std::vector<std::vector<float>> ys;
        std::vector<int> ramps;
        bool picker = false, reliefRef = false;
        void add(std::string label, std::vector<float> y, int ramp) {
            labels.push_back(std::move(label));
            ys.push_back(std::move(y));
            ramps.push_back(ramp);
        }
    };
    std::vector<float> plotTime_;
    std::vector<PlotCell> plotCells_;
    std::vector<std::string> plotKeys_;
    std::uint64_t plotResultRevision_ = 0, plotCircuitRevision_ = 0;
    int plotPick_ = -1;

    // ---- BOM / piping handoff ----
    nlohmann::json bom_;
    std::map<std::string, bool> srcOpen_;   // BOM sourcing card open state per component id
    double hoSupplyBar_ = 160.0, hoFlowLpm_ = 20.0, hoLoadBar_ = 0.0, hoTolPct_ = 30.0;
    bool hoLoadPresent_ = false;
    nlohmann::json handoffParams_ = nlohmann::json::object();
    bool hoSeeded_ = false;
    nlohmann::json handoff_;
    bool pipingOpenReq_ = false;
};

}  // namespace ui
