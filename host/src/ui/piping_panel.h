// PipingPanel — the human surface for piping, internal-flow CFD and industrial sizing.
//
// Owned by each document's SimManager (so it follows the doc tab exactly like the FEA studies)
// and opened from the Simulate workspace's "Piping" ribbon tab (sim_piping_* commands, routed by
// SimManager::handleCommand). It drives the SAME native RPCs the agent tools call:
//
//   piping_route        obstacle-avoiding orthogonal router (engine injects body obstacles for
//                       the active doc_id), returns the updated aicad.piping/1 network
//   piping_edit         atomic drawn-run, fitting and deletion edits, shared with agent tools
//   piping_fitting_k    deterministic K(Re) previews (correlation, constant, Hooper, Darby, Cv)
//   piping_fitting_k_cfd_start/status/cancel  asynchronous fitting calibration in the Python worker
//   piping_solve        1-D network hydraulics (same params as services/mcp/piping_tools.py)
//   piping_spec_check   ASME B31.3 wall / MAWP / flange rating / support span
//   piping_bom          line list, material take-off, spools
//   sim_run_pipe_flow   native internal-flow LBM, launched on the shared SimClient (cancellable)
//   vessel_size         ASME VIII-1 shell + head sizing
//   duct_size           equal-friction duct sizing
//   pump_drive_size     electric motor for the network's pump duty point (Solve > Pump drive)
//
// Every result is persisted into the document as a study record (sim_study_add/sim_study_edit;
// types piping_network / pipe_flow / vessel / duct_system), so it saves inside the .step+ and is
// reloaded from sim_study_list when the document opens (SimManager::refresh hands the records
// over through setStudies). The records also appear in the Simulation tree's study list.
//
// The pane follows the Simulation UI standard: kit::panel scaffold (pinned header + form tabs +
// the form's page switch, scroll body, pinned action footer with the current page's one primary
// action and its inline blocker), kit_form fields, removable cards for boundary rows / duct
// segments, read-only results as key/value cards, <= 4-column kit tables and a kit::linePlot.
// The network forms (Network, Draw Pipe, Fittings, Solve, Spec, Internal CFD, BOM) share the tabs; Vessel
// and Duct are their own records (own header icon, no network tabs). Long forms page their inputs
// and results (Solve: Conditions | Results | Pump drive; Spec / CFD / Vessel: inputs | Results;
// Duct: Segments | Design | Results). The footer status line is per form, one line, and stays
// quiet while the page's result card already says it. A saved network / vessel / duct system is
// deleted from the header's Delete icon (inline confirmation). Users read runs, nodes, spools and
// fittings by name ("Run 1", "Run 1 start", "Spool 1"); engine ids appear only in tooltips.
// Setup supports document-scoped face drops, run-end boundary/CFD drops, editable presets and duct reordering.
// Gesture payloads retain their original document/study/network and setup generations.
// Native fluid aliases and T-over-T_C precedence stay untouched until a structured fluid edit.
// Native parameter edits apply transactionally; unknown keys survive persistence without capability claims.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/sim_common.h"
#include "sim/sim_pump_drive.h"

struct AppState;

namespace ui {

// Index of the "Piping" tab in widgets/ribbon.cpp kRibbonTabs[] (append-only; Simulate only).
constexpr int kRibbonTabPiping = 22;

class PipingPanel {
public:
    enum class Form { Network, Route, Solve, Spec, Bom, Flow, Vessel, Duct, Fittings, Count };

    PipingPanel();

    // ---- persisted records ------------------------------------------------------------------
    // True for the study types this panel owns (SimManager keeps them out of its FEA mirror).
    static bool isPipingStudyType(const std::string& type);
    // Replace the record mirror with this document's piping records (from sim_study_list).
    // Adopts the first network / vessel / duct record when the panel has none yet, and picks up
    // records another client (an agent) changed since the panel last saw them.
    void setStudies(AppState& st, const std::string& docId,
                    const std::vector<nlohmann::json>& records);
    int  studyCount() const { return store_.count(); }
    // Rows of the Simulation tree's "Piping, hydraulics & industrial" group (header drawn by
    // SimManager, which also lists the Hydraulics panel's circuits there).
    void drawStudyRows(AppState& st);
    // Open the saved record named `name` exactly as a click on its row does (its own form; a
    // result on its Results page). False when no record has that name (CHUDWARE_SHOT=demoreview).
    bool openStudyNamed(AppState& st, const std::string& name);
    // Open a generated network (the Hydraulics panel's hose-to-piping handoff) as a NEW network
    // study: its boundary rows, its resolved fluid ({name, rho, mu}, e.g. hydraulic oil) and the
    // handoff's live solve. Opens the panel on the Solve form.
    void openHandoff(AppState& st, const nlohmann::json& pipingDoc, const nlohmann::json& boundary,
                     const nlohmann::json& fluid, const nlohmann::json& solve);

    // ---- commands + panel --------------------------------------------------------------------
    // sim_piping_new / _draw / _route / _fitting / _solve / _spec / _bom / _flow / _vessel / _duct.
    bool handleCommand(AppState& st, const std::string& cmdId);
    bool isOpen() const { return open_; }
    void close() { open_ = false; armed_ = 0; tool_ = Tool::None; }
    // Draws the panel (takes over the Simulation pane like the EM / SPICE panels).
    void draw(AppState& st);

    // ---- viewport ----------------------------------------------------------------------------
    // A route endpoint is armed for a viewport face pick.
    bool wantsFacePick() const { return open_ && armed_ != 0; }
    // Consume a face pick: the face centroid (offset out by the clearance) becomes the armed
    // endpoint and the face normal its direction.
    bool routeFacePick(AppState& st, const nlohmann::json& selector, uint32_t body, uint32_t topo);
    bool setRouteFace(AppState& st, int endpoint, const nlohmann::json& selector,
                      uint32_t body, uint32_t topo);
    // Canonical native setup, also used by the advanced editor and gesture scenarios.
    nlohmann::json setupDefinition(Form form) const;
    bool configureSetup(AppState& st, Form form, const nlohmann::json& definition, std::string& error);
    bool assignBoundaryPort(AppState& st, int row, const std::string& runId, int end);
    bool assignFlowPort(AppState& st, bool inlet, const std::string& runId, int end);
    bool reorderDuct(AppState& st, const std::string& segmentId, const std::string& beforeId);
    bool applySetupPreset(AppState& st, Form form, int preset);
    nlohmann::json routeEndpoints() const;
    // Gesture origins are captured once; delivery must still belong to this exact setup.
    nlohmann::json setupDragOrigin(const AppState& st, Form form) const;
    bool acceptsSetupDrag(const AppState& st, Form form, const nlohmann::json& origin) const;

    // ---- SimClient completion (sim_run_pipe_flow) --------------------------------------------
    bool flowJobActive() const { return flowRunning_; }
    void finishFlowJob(AppState& st, bool ok, bool cancelled, const nlohmann::json& result,
                       const std::string& error, const std::string& ownerDocId);

    // ---- scripted entry points (CHUDWARE_SHOT=piping) — the same calls the form buttons make ----
    void setRouteEndpoints(const double start[3], const double startDir[3], const double end[3],
                           const double endDir[3]);
    void setRouteSpec(double nps, const std::string& schedule, const std::string& material,
                      double clearanceMm);
    bool runRoute(AppState& st);
    bool runSolve(AppState& st);
    bool runSpecCheck(AppState& st);
    bool runBom(AppState& st);
    // Pump drive (pump_drive_size) for the solved duty point or the manual flow/head; stored in
    // the network study as results.pump_drive[<run id>].
    bool runPumpDrive(AppState& st);
    const nlohmann::json& pumpDrives() const { return pumpDrive_; }
    // What a click on the current form's page switch / the "Color pipes by" control does
    // (scripted review, CHUDWARE_SHOT=demoreview). Pages: Solve 0 Conditions, 1 Results, 2 Pump
    // drive; Spec 0 Design basis, 1 Results. colorBy: 0 none, 1 pressure, 2 velocity.
    void setPage(Form f, int page);
    void setColorBy(int c) { colorBy_ = c < 0 ? 0 : c > 2 ? 2 : c; }
    bool busy() const { return store_.inflight() > 0 || flowRunning_ || flowPreparing_ ||
                               endpointResolving_[0] || endpointResolving_[1]; }
    const nlohmann::json& network() const { return network_; }
    const nlohmann::json& lastRoute() const { return route_; }
    const nlohmann::json& solveResult() const { return solve_; }
    const nlohmann::json& specResult() const { return spec_; }
    const nlohmann::json& bomResult() const { return bom_; }
    // CFD calibration uses the current fitting draft; no preliminary persistence is required.
    // Read-only job snapshot for UI diagnostics and scenario failure evidence.
    nlohmann::json fittingCfdState() const;
    // Last refusal/failure text of the most recent action ("" = none).
    const std::string& lastError() const { return store_.lastError(); }
    const std::string& networkStudyId() const { return netSlot_.id; }

    // ---- 3D drawing + fitting editing (piping_edit RPC; consumed by sim/pipe_scene.cpp) -------
    // Tool::DrawRun: left clicks on the working plane add polyline points (a nice 3D line tool);
    // Tool::InsertFitting: a click on a run places drawSettings().insertType there.
    enum class Tool { None, DrawRun, InsertFitting };
    struct DrawSettings {
        int plane = 0;               // ui::simscene::Plane: 0 XY, 1 XZ, 2 YZ (last explicit choice)
        // Auto: after the first point the working plane through the last point is the axis plane
        // most facing the camera. An explicit pick (Tab / chip / panel) sets false; the sketch tool
        // returns to Auto when a run completes or is cancelled.
        bool autoPlane = true;
        double planeOffsetMm = 0.0;  // working-plane offset along its normal (elevation)
        double gridMm = 50.0;        // snap pitch (<= 0 = off)
        bool snapGrid = true, snapAxis = true, snapPipes = true;
        std::string insertType = "valve";                         // fitting type to place
        nlohmann::json insertParams = nlohmann::json::object();  // e.g. {"valve_type":"gate"}
    };
    Tool tool() const { return tool_; }
    // Arming opens Draw Pipe or Fittings and creates an empty network when none exists.
    void setTool(AppState& st, Tool t);
    DrawSettings& drawSettings() { return draw_; }
    const DrawSettings& drawSettings() const { return draw_; }
    // Run spec a newly drawn run gets: {nps, schedule, material, bend_radius} from the route form.
    nlohmann::json newRunSpec() const;
    // Allocation-free views of newRunSpec() for per-frame previews.
    double newRunNps() const { return nps_; }
    bool newRunShortRadius() const { return bendRadius_ != 0; }
    // Apply piping_edit ops atomically. On success: adopt result.piping, persist the network,
    // bump networkRevision(), drop solve/spec/BOM results, select the first created run/fitting,
    // say() `note`. Returns false (nothing sent) while another edit is in flight.
    bool submitEdit(AppState& st, nlohmann::json ops, const std::string& note);
    bool editPending() const { return editInflight_; }
    // Bumps on every network adoption / edit (viewport mesh cache key).
    std::uint64_t networkRevision() const { return networkRevision_; }
    // Selection by engine id ("" = none).
    std::string selectedRunId() const;
    const std::string& selectedFittingId() const { return selectedFitting_; }
    void selectRun(AppState& st, const std::string& runId);
    void selectFitting(AppState& st, const std::string& fittingId);
    // Viewport colouring of runs from the last solve: 0 none, 1 pressure, 2 velocity. Vessel and
    // duct forms show no pipe legend, so their pipes draw uncoloured rather than unexplained.
    int colorBy() const { return form_ == Form::Vessel || form_ == Form::Duct ? 0 : colorBy_; }

    // ---- display units ----------------------------------------------------------------------
    // Display AND input follow the Simulation pane's unit choice (SimManager::unitSys: Follow
    // document / SI / US customary): US shows in, ft, psi, °F, gpm, lb, ...; SI shows the
    // network's native mm, m, kPa, °C, m³/h, kg. Engine values and every stored member stay SI.
    // syncUnits() re-resolves the system (draw, study rows, setStudies and the viewport tool call
    // it) and refreshes the cached row text when it changes.
    void syncUnits(const AppState& st);
    // Display length unit ("in" / "mm") and its size in canonical mm (25.4 / 1): a typed bare
    // length is in this unit.
    const char* lengthUnit() const;
    double lengthUnitMm() const { return us_ ? 25.4 : 1.0; }

private:
    using Slot = sim::StudyRecordStore::Slot;

    // ---- form drawing ----
    std::string headerTitle() const;
    // The visible page of the current form is its Results page.
    bool onResultsPage() const;
    // The form has a result to show on its Results page.
    bool hasResult(Form f) const;
    void drawFooter(AppState& st);
    void drawNetworkForm(AppState& st);
    void drawRouteForm(AppState& st);
    void drawFittingsForm(AppState& st);
    void refreshRows() const;
    void loadFittingDraft();
    nlohmann::json fittingDefinition() const;
    void tickFittingRequests(AppState& st);
    void startFittingCfd(AppState& st);
    void cancelFittingCfd(AppState& st);
    void drawSolveForm(AppState& st);
    void drawSolveResults();
    void drawNetworkLegend(AppState& st);
    void drawSpecForm(AppState& st);
    void drawBomForm(AppState& st);
    void drawFlowForm(AppState& st);
    void drawFlowSetup(AppState& st);
    void drawVesselForm(AppState& st);
    void drawDuctForm(AppState& st);
    void drawPumpDrive(AppState& st);
    // Results page with nothing yet: what to do (the footer carries the Run action).
    void drawNoResult(const char* icon, const char* title, const char* body);
    void drawNativeEditor(AppState& st);
    void drawRunPorts(AppState& st);
    bool acceptRunPort(AppState& st, std::string& runId, int& end);
    void invalidateSetup(AppState& st, Form form);
    nlohmann::json vesselDefinition() const;
    nlohmann::json ductDefinition() const;

    // ---- blockers: why the form's primary action cannot run ("" = ready) ----
    std::string routeBlocker() const;
    std::string solveBlocker() const;
    std::string specBlocker() const;
    std::string bomBlocker() const;
    std::string flowBlocker(const AppState& st) const;
    std::string vesselBlocker() const;
    std::string ductBlocker() const;
    std::string pumpDriveBlocker() const;

    // ---- actions ----
    // Switch the visible form and seed what it needs (route endpoints, boundary rows, flow ports,
    // duct segments); a real switch drops the previous form's status line. The one place forms
    // are entered: ribbon commands, the form tabs and the study rows.
    void enterForm(AppState& st, Form f);
    // Bring the panel up in the Simulate workspace on the Piping ribbon tab.
    void showPanel(AppState& st);
    // Status line owned by form `f` (shown only while `f` is the visible form).
    void say(Form f, const std::string& text, int tone);
    // say() an action's outcome: Ok, or Warn with result["warnings"] listed under the line.
    void sayWarn(Form f, const std::string& text, const nlohmann::json& result);
    void newNetwork(AppState& st);
    void deleteNetwork(AppState& st);
    // The saved record the header's Delete action removes on this form: 0 none, 1 network,
    // 2 vessel, 3 duct system. deleteCurrent() removes it once the inline confirmation is accepted.
    int deleteKind() const;
    void deleteCurrent(AppState& st);
    bool runFlow(AppState& st);
    bool runVessel(AppState& st);
    bool runDuct(AppState& st);
    void removeRun(AppState& st, int runIndex);
    void seedRouteDefaults(AppState& st);
    void seedBoundaryDefaults();
    void seedFlowPorts();
    nlohmann::json solveParams() const;
    nlohmann::json flowCallParams() const;
    nlohmann::json fluidJson() const;
    void resolveFluidDisplay(AppState& st, bool flow);
    std::uint64_t& setupGeneration(Form form);
    const std::uint64_t& setupGeneration(Form form) const;
    bool gasNetwork() const;
    // Pump duty point from the solve: {run, flow_m3h, head_m, source} (empty when unsolved).
    nlohmann::json solvedDutyPoint() const;
    // The duty point the drive is sized for: the solved one, or the manual run/flow/head.
    nlohmann::json driveDutyPoint() const;
    // Issue an engine request for form `f` through the store; `stage` captions that form's footer
    // while it runs.
    void request(AppState& st, Form f, const char* method, nlohmann::json params, const char* stage,
                 void (PipingPanel::*done)(AppState&, bool, const nlohmann::json&, const std::string&),
                 int timeoutMs);
    void onPumpDrive(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);

    // ---- persistence ----
    nlohmann::json boundaryJson() const;
    void persistNetwork(AppState& st);
    void persistVessel(AppState& st);
    void persistDuct(AppState& st);
    void persistFlow(AppState& st);
    nlohmann::json networkRecord() const;
    void adoptNetwork(AppState& st, const nlohmann::json& record);
    void adoptVessel(const nlohmann::json& record);
    void adoptDuct(const nlohmann::json& record);
    void adoptFlow(AppState& st, const nlohmann::json& record);
    // A study row click: adopt the record (a copy: adoption re-reads the mirror) and show it.
    void openRecord(AppState& st, nlohmann::json rec);

    void onRoute(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onSolve(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onSpec(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onBom(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onVessel(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);
    void onDuct(AppState& st, bool ok, const nlohmann::json& r, const std::string& why);

    // Records mirror, add→edit save sequencing, engine calls and the status line.
    sim::StudyRecordStore store_;
    // Liveness for the fluid-volume export callback, which must release its SimClient
    // reservation even after the panel is gone (store callbacks are dropped instead).
    std::shared_ptr<int> alive_;
    bool open_ = false;
    Form form_ = Form::Network;
    static constexpr int kFormCount = static_cast<int>(Form::Count);
    int page_[kFormCount] = {};    // visible page per form
    Form statusForm_ = Form::Network;  // form the status line belongs to
    Form busyForm_ = Form::Network;    // form that issued the in-flight engine request
    std::string busyStage_;        // footer caption of the in-flight engine request
    double busySince_ = 0.0;       // ImGui time the request was issued
    int confirmDelete_ = 0;        // deleteKind() awaiting its inline confirmation (0 = none)

    // ---- network ----
    Slot netSlot_;
    nlohmann::json network_;       // aicad.piping/1
    double designPressureKpa_ = 1000.0;
    double designTempC_ = 40.0;
    int selectedRun_ = -1;
    nlohmann::json route_, solve_, spec_, bom_;
    nlohmann::json networkParams_ = nlohmann::json::object();
    bool setupEdited_ = false;
    std::uint64_t networkGeneration_ = 0, flowGeneration_ = 0;
    std::uint64_t vesselGeneration_ = 0, ductGeneration_ = 0;
    std::uint64_t fluidDisplayGeneration_[2] = {};
    std::string mirroredDocId_;
    bool nativeEditorOpen_[kFormCount] = {};
    std::string nativeDraft_[kFormCount], nativeError_[kFormCount];

    // ---- route form ----
    double start_[3] = {0, 0, 0}, startDir_[3] = {1, 0, 0};
    double end_[3] = {1000, 0, 0}, endDir_[3] = {1, 0, 0};
    bool routeSeeded_ = false;
    double nps_ = 2.0;
    int scheduleIdx_ = 0;
    int materialIdx_ = 0;
    double clearanceMm_ = 50.0;
    double gridMm_ = 0.0;          // 0 = engine default
    std::string runName_;
    int armed_ = 0;                // 0 none, 1 start, 2 end
    nlohmann::json endpointFace_[2];
    std::uint64_t endpointGeneration_[2] = {};
    bool endpointResolving_[2] = {};
    bool routePathOpen_ = false;   // "Path points" card disclosure
    // Footer: the status line that announced warnings (sayWarn), those warnings (listed under the
    // line behind an expandable "N warnings" chip) and whether the list is open.
    std::string warnStatus_;
    std::vector<std::string> warnList_;
    bool warnOpen_ = false;
    bool us_ = false;              // US customary display/input (see syncUnits)

    // ---- solve form ----
    struct Boundary {
        int run = 0;               // index into network_.runs
        int end = 0;               // 0 start, 1 end
        int kind = 0;              // liquid: 0 pressure kPa, 1 head m, 2 demand m3/h; gas: 0 pressure, 1 mass kg/h
        double value = 0.0;
        nlohmann::json native = nullptr;
    };
    std::vector<Boundary> boundary_;
    int fluidIdx_ = 0;
    double fluidTempC_ = 20.0;
    // Native fluid inputs are retained in networkParams_; this flags explicit rho/mu display.
    nlohmann::json fluidOverride_;

    // ---- pump drive (Solve form) ----
    nlohmann::json pumpDrive_ = nlohmann::json::object();   // results.pump_drive {run_id: {...}}
    int driveSource_ = 0;          // 0 solved duty point, 1 manual
    int driveRun_ = 0;             // manual: run index the pump sits on
    sim::PumpDriveInputs driveIn_; // kind = centrifugal; manual duty in flowM3h / headM
    std::string drivePendingRun_;  // run id of the in-flight pump_drive_size
    nlohmann::json drivePendingOp_;

    // ---- spec check form ----
    double corrosionMm_ = 1.5, millTol_ = 0.125, jointFactor_ = 1.0;
    double fluidDensity_ = 1000.0, insulationKgm_ = 0.0;

    // ---- BOM form ----
    double maxSpoolMm_ = 12000.0;

    // ---- internal flow form ----
    Slot flowSlot_;
    std::string flowSlotKey_;      // network|run the flow slot belongs to
    int flowRun_ = -1;
    int flowSource_ = 0;           // 0 active document solid, 1 STL file
    std::string flowStl_;
    int flowFluid_ = 0;            // built-in fluid index; countOf(kFluidKeys) = custom
    double flowDensity_ = 1000.0, flowViscosity_ = 1.0e-3;
    int flowRateMode_ = 0;         // 0 mean velocity m/s, 1 volume flow m3/h
    double flowVelocity_ = 1.0, flowRateM3h_ = 10.0;
    double flowVoxelMm_ = 0.0;     // 0 = engine default
    int flowMaxSteps_ = 0;         // 0 = engine default
    double inlet_[3] = {0, 0, 0}, inletN_[3] = {1, 0, 0};
    double outlet_[3] = {0, 0, 0}, outletN_[3] = {-1, 0, 0};
    bool flowRunning_ = false;     // launched on the SimClient, completion pending
    bool flowPreparing_ = false;   // STL export in flight (SimClient reserved)
    nlohmann::json flowLaunchParams_;  // engine params + network_id / run_id / source_stl of the run
    nlohmann::json flow_;

    // ---- vessel form ----
    Slot vesselSlot_;
    double vesselIdMm_ = 1500.0;
    int vesselSizeBy_ = 0;         // 0 tan-tan length, 1 volume
    double vesselTtMm_ = 4000.0, vesselVolM3_ = 8.0;
    double vesselPKpa_ = 1000.0, vesselTC_ = 40.0;
    int vesselMaterial_ = 0;
    double vesselCaMm_ = 3.0, vesselE_ = 0.85;
    int vesselHead_ = 0;
    double vesselRho_ = 1000.0, vesselFill_ = 0.8;
    int vesselOrient_ = 0;         // 0 vertical, 1 horizontal
    nlohmann::json vesselParams_;  // inputs of the sizing that produced vessel_
    nlohmann::json vessel_;

    // ---- duct form ----
    Slot ductSlot_;
    struct DuctSeg {
        std::string id;
        int parent = -1;           // index into ductSegs_ (-1 = fan / root)
        double airflow = 1.0;      // m3/s
        double length = 10.0;      // m
        double k = 0.0;
        int shape = 0;             // 0 round, 1 rect
        double aspect = 2.0;
        nlohmann::json native = nullptr;
    };
    std::vector<DuctSeg> ductSegs_;
    double ductFriction_ = 1.0, ductVmax_ = 10.0, ductTempC_ = 20.0, ductRoughMm_ = 0.09;
    nlohmann::json ductParams_;    // inputs of the sizing that produced duct_
    nlohmann::json duct_;

    // ---- 3D drawing + fitting editing ----
    Tool tool_ = Tool::None;
    DrawSettings draw_;
    bool editInflight_ = false;
    std::uint64_t networkRevision_ = 1;
    std::string selectedFitting_;
    int colorBy_ = 0;
    std::uint64_t legendRevision_ = 0;
    const void* legendLinks_ = nullptr;
    const void* legendNodes_ = nullptr;
    int legendColorBy_ = -1;
    bool legendUs_ = false;
    bool legendRangeValid_ = false;
    float pipeLegendMin_ = 0, pipeLegendMax_ = 0;
    std::string pipeLegendTitle_, pipeLegendUnits_;
    int routeMode_ = 0, bendRadius_ = 0;
    struct RunRow {
        std::string id, label, detail, portLabel[2], portButton[2];
        nlohmann::json native;
    };
    struct FittingRow { std::string id, label, runLabel; nlohmann::json native; };
    mutable std::uint64_t rowsRevision_ = 0;
    mutable bool rowsUs_ = false;
    mutable std::vector<RunRow> runRows_;
    mutable std::vector<FittingRow> fittingRows_;
    mutable std::vector<std::string> runLabels_, flowRunLabels_;
    mutable std::string fittingsCount_;
    struct StudyRow { std::string id, title, summary; };
    std::vector<StudyRow> studyRows_;
    int fittingPalette_ = 3, fittingValve_ = 0, fittingSource_ = 0, fittingQuality_ = 0;
    int paletteValve_ = 0, fittingRadius_ = 0, fittingFlangeClass_ = 0, fittingFlangeFace_ = 0;
    double paletteNps2_ = 1, paletteBeta_ = 0.5, paletteK_ = 1, fittingBranchNps_ = 2;
    double fittingAngle_ = 90, fittingNps2_ = 1, fittingBeta_ = 0.5;
    double fittingK_ = 1, fittingK1_ = 800, fittingKinf_ = 0.2;
    double fittingKi_ = 0.2, fittingKd_ = 1, fittingCv_ = 10, fittingRe_ = 100000;
    std::string fittingName_, fittingLabel_, fittingDraftId_, fittingError_, fittingMethod_;
    nlohmann::json fittingDraft_ = nlohmann::json::object(), fittingPreview_, fittingCfdResult_;
    std::uint64_t fittingDraftRevision_ = 0, previewGeneration_ = 0;
    bool fittingDirty_ = false, previewDirty_ = true, previewInflight_ = false;
    double previewDue_ = 0, cfdPollDue_ = 0, fittingCfdPct_ = 0;
    bool fittingCfdRunning_ = false, fittingCfdCall_ = false, fittingCfdCancel_ = false;
    std::string fittingCfdJob_, fittingCfdTarget_, fittingCfdStage_, fittingCfdError_;
    std::string fittingCfdState_ = "idle";
    std::string fittingCfdNetwork_, fittingCfdDoc_;
    std::vector<double> previewRe_, previewK_, cfdRe_, cfdK_;
    kit::PlotSpec previewPlot_, cfdPlot_;
    int fittingGeometrySource_ = 0;
    std::string fittingStl_, fittingBody_;
    double fittingInlet_[3] = {}, fittingOutlet_[3] = {};
    double fittingInletN_[3] = {1, 0, 0}, fittingOutletN_[3] = {-1, 0, 0};
    double fittingBoreMm_ = 52.5;
};

}  // namespace ui
