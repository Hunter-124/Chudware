#pragma once
// sim_topology.h — Topology-optimization study UI: the typed SCALAR settings
// editor and the results / run-comparison panel. Free functions only; no
// SimManager internals.
//
// OWNERSHIP
//   This TU owns the scalar part of a topology study — the `topology{}` object
//   of the shared study envelope {type:"topology", ..., topology:{...}}
//   (topology contract, "Public study schema"). Face wells (fixtures, loads,
//   load cases, preserve/exclude selectors, thermal/flow boundary conditions),
//   mesh settings, Run/Cancel/progress, study persistence, artifact file
//   operations and viewport overlays belong to the integration owner
//   (SimManager). Nothing in this TU issues an RPC, touches the filesystem or
//   mutates AppState; AppState is read only for display units.
//
// CALL PROTOCOL (integration owner)
//   1. Capabilities. Fetch the native topology_capabilities() reply OFF the
//   draw path (async RPC),
//      call parseCapabilities() ONCE per reply and keep the returned
//      Capabilities. Until the reply arrives pass a default-constructed
//      Capabilities (state Loading); if the fetch fails pass
//      capabilitiesUnavailable(<user-facing reason>). Nothing is offered that
//      the matrix does not list as supported.
//   2. Settings. Each frame the setup form is visible:
//          bool commit = drawTopologySettings(st, study["topology"], caps,
//          settingsView, locked, groups, study, additiveControls);
//      `groups` (SettingsGroup bits) picks the sections: kGroupsSolver on the
//      Study page, kGroupsManufacturing on the Regions/Manufacturing page.
//      `topology` is edited IN PLACE. Only `method`, `physics`, `objective` and
//      the keys of the scalar table (scalarKeys()) are ever written or erased;
//      every other key — agent-authored, prior results, future fields — is
//      preserved untouched. A field cleared to empty ERASES its key (the solver
//      then applies its own documented default); nothing is silently defaulted.
//      Returns true exactly once per committed edit (field committed on
//      deactivation with a changed value, combo pick, toggle, Remove) — persist
//      the study then. Keystrokes in an active field are not committed. `locked` (nullptr
//      = editable) renders the form read-only with that reason, e.g. while a
//      run owns the study.
//   3. Run gating. Before starting a run call validateTopology(topology, caps, study)
//   and refuse while
//      hasBlocking(issues); show the issue messages. drawTopologySettings lists
//      the same issues.
//   4. Results. Each frame the results view is visible:
//          ResultsAction a = drawTopologyResults(st, runs, caps, resultsView);
//      then execute `a` after drawing (see ResultsAction). `runs` is the
//      retained run array (RunRecord shape below), oldest first. The panel
//      never deletes or rewrites a run.
//
// CAPABILITIES JSON (authoritative; produced by native topology_capabilities())
//   {
//     "methods":  [{"id":"simp","label"?:str,"formulation"?:str}, ...],
//     "physics":
//     [{"id":"structural","label"?:str,"formulation"?:str,"validity"?:str},
//     ...], "combinations": [{
//         "method":"simp", "physics":"structural", "supported":bool,
//         "reason"?:str,              // REQUIRED meaning when
//         supported==false: why not "formulation"?:str,         // exact
//         formulation label for this pair "volume_phase"?:"solid"|"fluid",  //
//         phase volume_fraction budgets; absent = fluid for
//                                     // flow/cht, solid otherwise (rho = 1 is
//                                     always solid)
//         "objectives":[str,...],     // offered objective ids, first =
//         preferred "limits":[str,...],         // offered optional
//         response-limit keys "parameters":[str,...],     // offered scalar
//         keys (scalarKeys() vocabulary) "defaults"?:{key:value}     // solver
//         defaults, shown as input hints only
//     }, ...]
//   }
//   A pair absent from `combinations` is unsupported. A supported pair listing
//   no objective is treated as unsupported (nothing could run).
//
// RESULT JSON (one native sim_run_topology reply; RunRecord.result)
//   { ok, converged, feasible, termination, termination_detail?, error?,
//   method, physics,
//     formulation?,
//     summary:{ objective, objective_units?, objective_baseline,
//     objective_final,
//               volume_phase?, volume_fraction_target?,
//               volume_fraction_baseline?, volume_fraction_final,
//               mass_baseline_g?, mass_final_g?, iterations,
//               max_constraint_violation, convergence_criterion, final_change?,
//               elapsed_s?, <other scalar evidence> },
//     history:{ iteration:[...], <series>:[...] },        // series same length
//     as iteration constraints:[{ name, label?, value, limit, units?,
//     violation, satisfied? }],   // g <= 0 manufacturing:{ checks:[{ id,
//     label?, requested?, measured?, units?,
//                               status:"pass"|"fail"|"not_measured", note? }],
//                     geometry_valid:bool, dfam:{ checked:bool, passed:bool,
//                     summary? }, print_ready?:bool (ignored: never sliced) },
//     provenance:{scalars}, mesh_quality:{scalars},
//     artifacts:{ <key>:{ path, format, sha256?, bytes?, label?, missing?:bool,
//     stale?:bool } } }
//   termination values distinguished: "converged", "max_iterations",
//   "infeasible", "cancelled"; any other value (line-search failure, design
//   stuck at bounds, stalled, ...) is shown verbatim with termination_detail
//   and is never treated as convergence. `ok` = numerical completion,
//   `converged` = real convergence, `feasible` = every active constraint holds.
//   The integration owner sets artifacts.<key>.missing / .stale from its own
//   (non-draw) check. Surface formats (stl/3mf/obj) are exportable only when
//   manufacturing.geometry_valid is true; an artifact with format "aicm" (or
//   key overlay_aicm) is the viewport overlay, not an export.
//
// RUN RECORD (retained comparison entry)
//   { run_id:str, label?:str, started_utc?:unix seconds, settings:{topology
//   snapshot used},
//     result:{RESULT JSON} }
#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {
namespace topology {

// ---- capabilities
// --------------------------------------------------------------------------------
struct CapabilityEntry {
  std::string id, label, formulation, validity;
};

struct Combination {
  std::string method, physics;
  bool supported = false;
  std::string reason; // why unsupported (always non-empty when !supported)
  std::string formulation;
  std::string volumePhase; // "solid" | "fluid": the phase
                           // topology.volume_fraction budgets
  std::vector<std::string> objectives, limits, parameters;
  nlohmann::json defaults = nlohmann::json::object();

  bool offersObjective(const std::string &objective) const;
  bool offersKey(const std::string &key) const; // in limits or parameters
};

struct Capabilities {
  enum class State { Loading, Unavailable, Ready };
  State state = State::Loading;
  std::string error; // user-facing reason when Unavailable
  std::vector<CapabilityEntry> methods, physics;
  std::vector<Combination> combinations;

  const CapabilityEntry *method(const std::string &id) const;
  const CapabilityEntry *physicsEntry(const std::string &id) const;
  const Combination *find(const std::string &method,
                          const std::string &physics) const;
  // True when the pair is listed and supported; otherwise fills `reason` (if
  // non-null).
  bool supports(const std::string &method, const std::string &physics,
                std::string *reason = nullptr) const;
};

Capabilities parseCapabilities(const nlohmann::json &caps);
Capabilities capabilitiesUnavailable(const std::string &reason);

// ---- labels (fixed vocabulary; unknown ids fall back to the capability label,
// then the id) -----
std::string methodLabel(const std::string &id,
                        const Capabilities *caps = nullptr);
std::string physicsLabel(const std::string &id,
                         const Capabilities *caps = nullptr);
std::string objectiveLabel(const std::string &objective,
                           const std::string &physics);
// Result vocabulary for users: a termination token ("max_iterations" ->
// "Iteration limit"), a solver formulation string ("mma(svanberg1987; ...)" ->
// "Method of moving asymptotes (MMA)") and a convergence criterion formula
// in words ("" when the format is unknown). Raw text belongs in a tooltip.
std::string terminationLabel(const std::string &termination);
std::string formulationLabel(const std::string &formulation);
std::string criterionText(const std::string &criterion);
// Every scalar key this TU owns (written/erased by the editor), excluding
// method/physics/objective.
const std::vector<std::string> &scalarKeys();

// ---- settings model
// --------------------------------------------------------------------------------
enum class Severity { Blocking, Warning };
struct Issue {
  std::string key; // offending key; empty for whole-setup issues
  Severity severity = Severity::Blocking;
  std::string message; // user-facing (names its entry: footer, run blocker)
  // Short form for the issue line under the field INSIDE its entry's card,
  // where the card title already names the entry ("Needs a force vector.").
  // Empty = use `message`.
  std::string fieldMessage;
};

// Validates `topology` against the contract ranges and the capability matrix.
// Keys outside scalarKeys() are not judged (the service boundary validates
// them).
std::vector<Issue> validateTopology(const nlohmann::json &topology,
                                    const Capabilities &caps,
                                    const nlohmann::json &study = nlohmann::json());
bool hasBlocking(const std::vector<Issue> &issues);

// A minimal new config for `physics`: {physics, method, objective} using the
// first supported method and its first offered objective in capability order.
// Empty object when no method supports `physics`. Writes no numeric value.
nlohmann::json newTopologyConfig(const Capabilities &caps,
                                 const std::string &physics);

// Explicit preset application: overwrite offered topology keys, erase nulls,
// and retain the preset id on the canonical study.
bool applyPrintProcess(nlohmann::json &study, const nlohmann::json &preset,
                       const Capabilities &caps);
// Merge a completed worker DFAM reply without replacing existing evidence.
bool mergeDfamResult(nlohmann::json &run, const nlohmann::json &reply,
                     const std::string &error = {});
// Prefer a current print surface; otherwise use the raw reconstructed surface.
std::string surfaceArtifactKey(const nlohmann::json &result);

struct SwitchResult {
  bool applied = false;
  std::string refusal; // why not applied
  std::vector<std::string>
      removedKeys; // owned keys the new pair does not offer
  std::string objectiveBefore, objectiveAfter;
};
// Switches method/physics in place. Refuses an unsupported pair (topology
// untouched). Keeps the objective when offered, else picks the pair's first
// objective; erases owned scalar keys the pair does not offer (an unsupported
// input is never sent silently). Unowned keys are preserved.
SwitchResult selectCombination(nlohmann::json &topology,
                               const Capabilities &caps,
                               const std::string &method,
                               const std::string &physics);

// Copies method/physics/objective and the owned scalar keys of a retained run's
// settings snapshot into `topology`: owned keys absent from the snapshot are
// erased, unowned keys are preserved.
void applyRunSettings(nlohmann::json &topology,
                      const nlohmann::json &runSettings);

// ---- results model
// -----------------------------------------------------------------------------------
// Stable source identity for retained runs. Runtime document IDs, display names
// and study history do not change the source; geometry, units and material do.
std::string geometryKey(const nlohmann::json &tree);

enum class Outcome {
  Failed,         // ok != true
  Cancelled,      // termination "cancelled"
  Converged,      // ok, converged, feasible, not iteration-limited
  IterationLimit, // stopped at max_iterations: never converged
  Infeasible,     // completed but a constraint is violated: never converged
  NotConverged,   // completed without meeting the convergence criterion
  Inconsistent,   // the result contradicts itself (e.g. converged + infeasible)
};
struct OutcomeSummary {
  Outcome outcome = Outcome::Failed;
  bool computationOk = false; // the computation finished without failure
  bool converged = false;     // EFFECTIVE convergence after consistency rules
  bool feasibilityReported = false;
  bool feasible = false;
  double maxViolation = 0.0; // NaN when not reported
  std::string termination;
  std::string headline, detail; // user-facing
};
OutcomeSummary classifyResult(const nlohmann::json &result);

enum class CheckStatus { Pass, Fail, NotMeasured };
struct ManufacturingRow {
  std::string key, label, units, note;
  nlohmann::json requested; // null when nothing was requested
  nlohmann::json measured;  // null when not measured
  CheckStatus status = CheckStatus::NotMeasured;
};
struct ManufacturingReport {
  std::vector<ManufacturingRow> rows;
  bool geometryValidated = false, dfamChecked = false, dfamPassed = false;
  // Never set from a topology record: print readiness needs a sliced part whose
  // G-code passed validation, which no optimizer run produces (a stored
  // print_ready is ignored).
  bool printReady = false;
  std::string
      printReadyReason; // why not, incl. what remains once all checks passed
};
// `settings` is the run's topology snapshot: requested manufacturing intent the
// solver did not measure is reported as NotMeasured, never as satisfied.
ManufacturingReport manufacturingReport(const nlohmann::json &result,
                                        const nlohmann::json &settings);

struct MetricDelta {
  std::string key, label, units;
  double baseline = 0.0, selected = 0.0; // NaN when the run does not report it
  bool comparable = true;
  std::string note; // why not comparable
};
// Baseline-vs-selected metrics of two run records. Objective values compare
// only when both runs share physics, objective and objective units.
std::vector<MetricDelta> compareRuns(const nlohmann::json &baselineRun,
                                     const nlohmann::json &selectedRun);

// ---- views
// -----------------------------------------------------------------------------------------
struct SettingsView {
  // Build direction: "Custom" was chosen, so the X/Y/Z cells stay visible even
  // while the stored vector equals a preset (or nothing has been typed yet).
  bool customDirection = false;
  std::string switchNote; // last method/physics switch consequence
  // One-shot: scroll the Additive manufacturing card to the top of the body.
  bool focusManufacturing = false;
  int printProcessIndex = -1;
  // Disclosure of the collapsed "Formulation" card under Problem (the solver's
  // formulation and validity text; collapsed so Objective stays above the fold).
  bool formulationOpen = false;
};

// Which parts of the settings form drawTopologySettings draws. The Study page
// draws the solver groups; the Regions/Manufacturing page draws only the
// manufacturing groups (no second Problem block).
enum SettingsGroup : unsigned {
  kGroupProblem = 1u << 0,     // physics, method, objective (+ switch note)
  kGroupGoal = 1u << 1,        // volume budget
  kGroupLimits = 1u << 2,      // response limits
  kGroupConvergence = 1u << 3, // iterations, tolerance, move limit
  kGroupFilter = 1u << 4,      // filter and projection
  kGroupMethod = 1u << 5,      // method-specific parameters
  kGroupSymmetry = 1u << 6,    // symmetry, extrusion, passive skin depths
  kGroupAdditive = 1u << 7,    // additive manufacturing
  kGroupsSolver = kGroupProblem | kGroupGoal | kGroupLimits |
                  kGroupConvergence | kGroupFilter | kGroupMethod,
  kGroupsManufacturing = kGroupSymmetry | kGroupAdditive,
  kGroupsAll = kGroupsSolver | kGroupsManufacturing,
};

// Run ids: a record's run_id, or "#<index>" when the record carries none.
struct ResultsView {
  std::string selectedRunId, baselineRunId, plotSeries;
  // The ribbon's Compare command shows the Compare view (baseline vs selected
  // run) INSTEAD of the results; with fewer than two runs it is an empty state
  // that says so. Cleared by the owner on any other navigation.
  bool compareMode = false;
  // One-shot: the next draw of the Compare view scrolls to its top, then clears.
  bool focusCompare = false;
  // Disclosure of the collapsed detail cards (run details, setup snapshot,
  // mesh quality, provenance).
  bool detailsOpen = false, setupOpen = false, meshOpen = false,
       provenanceOpen = false;
};

struct ResultsAction {
  enum class Kind {
    None,
    ShowRun,        // display runId's density overlay / fields in the viewport
    ExportArtifact, // export runs[runId].result.artifacts[artifactKey] (owner
                    // picks path)
    ContinueInCad,  // import the validated surface into a new editable solid
    ApplyRunSettings, // applyRunSettings(study.topology, run.settings), then
                      // persist
  };
  Kind kind = Kind::None;
  std::string runId, artifactKey;
};

// ---- drawing (GUI thread; draws into the caller's current window/child)
// ---------------------
// `groups` (SettingsGroup bits) selects the sections drawn; the locked banner
// and whole-setup issues are drawn with the Problem group.
// `study` supplies the bed-face context; `additiveControls` draws the owner's
// print-process and bed-face controls inside the Additive manufacturing card.
bool drawTopologySettings(AppState &st, nlohmann::json &topology,
                          const Capabilities &caps, SettingsView &view,
                          const char *lockedReason = nullptr,
                          unsigned groups = kGroupsAll,
                          const nlohmann::json &study = nlohmann::json(),
                          const std::function<void()> &additiveControls = {});
// Results order (standard §5): outcome card, key metrics, then the caller's
// `displayControls` (viewport field selector; may be empty), the runs list
// (+ "Compare runs" with two or more), then the details (constraints, history,
// manufacturing, export, collapsed run details / setup / mesh / provenance).
// With view.compareMode and >= 2 runs it draws ONLY the Compare view.
ResultsAction
drawTopologyResults(AppState &st, const nlohmann::json &runs,
                    const Capabilities &caps, ResultsView &view,
                    const std::function<void()> &displayControls = {});
// X/Y/Z kit checkboxes on one frame-height row: the value of a kit field
// (between kit::fieldBegin and kit::fieldEnd). Symmetry axes and fixture DOFs
// share this one control. `tips` (3 entries) may be null. True when toggled.
bool axisToggles(bool on[3], const char *const *tips = nullptr);

} // namespace topology
} // namespace ui
