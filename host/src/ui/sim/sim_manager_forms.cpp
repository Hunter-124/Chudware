// SimManager study pane: the panel scaffold (drawForm: header, Setup -> Run -> Results stepper,
// scroll body, action footer), the setup-readiness model behind the stepper, and the FEA setup
// forms (material, fixtures, loads, mesh, study options, fatigue setup + fatigue result graphs).
// Every widget is ui::kit vocabulary (Simulation UI standard); ids are explicit and stable.
#include "sim_manager_internal.h"
#include "sim_interaction.h"

#include "imgui_internal.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "../../core/sim_client.h"
#include "../viewport_panel.h"
#include "../metrics.h"

namespace ui {

namespace {

namespace m = ui::metrics;

// ---- setup readiness -------------------------------------------------------------------------
// SetupStep::status: 0 ready, 1 optional / not set, 2 needs attention.
constexpr int kStepReady = 0, kStepOptional = 1, kStepAttention = 2;

kit::Status stepStatus(int status) {
  return status == kStepReady      ? kit::Status::Done
         : status == kStepOptional ? kit::Status::Pending
                                   : kit::Status::Retry;
}

bool finiteF(double v) { return std::isfinite(v); }

// A Thermal (CFD) study on a backend that definitively reports no OpenFOAM can never run: the
// setup is blocked permanently and the footer offers the Thermal FEA route instead. An unanswered
// capability probe never reads as "unavailable".
constexpr const char *kThermalCfdUnavailable =
    "Setup is complete, but Thermal CFD needs OpenFOAM, which is not installed.";
bool thermalCfdUnavailable(AppState &st, const Study &s) {
  if (s.type != StudyType::Thermal || !st.simClient)
    return false;
  const nlohmann::json caps = st.simClient->caps();
  return caps.is_object() && caps.contains("openfoam") && !caps.value("openfoam", false);
}

// Density in the display system: g/cm³ (SI) or lb/in³ (US customary).
constexpr double kLbIn3PerGcm3 = 0.0361273;
const char *densityUnit(const SimManager::UnitSys &u) {
  return kit::unitText(u.us ? kit::Unit::lb_in3 : kit::Unit::g_cm3);
}
double densityScale(const SimManager::UnitSys &u) { return u.us ? kLbIn3PerGcm3 : 1.0; }

// Page key for kit::panelBodyBegin: one scroll position per node.
const char *nodePage(SimNode n) {
  switch (n) {
  case SimNode::Material:
    return "material";
  case SimNode::Fixtures:
    return "fixtures";
  case SimNode::Load:
    return "loads";
  case SimNode::Mesh:
    return "mesh";
  case SimNode::Results:
    return "results";
  case SimNode::Boundaries:
    return "boundaries";
  case SimNode::Regions:
    return "regions";
  case SimNode::Study:
  default:
    return "study";
  }
}

// One-line summary for the collapsed "About this study" card.
const char *studyAbout(StudyType t) {
  switch (t) {
  case StudyType::Modal:
    return "Natural frequencies and mode shapes. Loads do not apply; fixtures are optional "
           "(without them the modes are free-free).";
  case StudyType::Fatigue:
    return "Fatigue life and safety factor under cyclic loading, scaled from the static stress "
           "of your loads (Goodman, Gerber, Soderberg, ASME, Morrow).";
  case StudyType::Buckling:
    return "Linear buckling load factors on the part mesh; a factor below 1 means instability "
           "below the applied load.";
  case StudyType::Contact:
    return "Nonlinear contact via CalculiX on a two-block proxy sized to the part; the reaction "
           "should equal the applied load.";
  case StudyType::FreqResponse:
    return "Harmonic base-excitation response by modal superposition (each mode an SDOF "
           "oscillator, combined by SRSS).";
  case StudyType::ThermalFea:
    return "Heat conduction in the part: fixed-temperature faces plus heat-flux or convection "
           "loads, steady or transient.";
  case StudyType::Flow:
    return "External flow (CFD): drag, lift and pressure from speed, direction and fluid. The "
           "structural material is not used.";
  case StudyType::Thermal:
    return "Convective heat transfer (OpenFOAM) with wall or heat-source boundary conditions. "
           "The structural material is not used.";
  case StudyType::Motion:
    return "Drive assembly joints together and check the sampled motion for interference.";
  case StudyType::Topology:
    return "Redistributes material inside the design domain for the chosen physics and goal. "
           "The source geometry is never replaced; results are kept per run.";
  case StudyType::Static:
  default:
    return "Linear static stress, displacement and factor of safety under the applied loads.";
  }
}

// Navigate the pane to a setup step (shared by the stepper chips and the footer "Go to" jump). An
// empty fixture well is armed so the next viewport click lands in it.
template <class Step>
void goToSetupStep(AppState &st, const Study &s, const Step &step, SimNode &node, int &loadIdx,
                   int &armedWell) {
  node = step.node;
  if (step.node == SimNode::Load)
    loadIdx = (loadIdx >= 0 && loadIdx < (int)s.loads.size()) ? loadIdx
                                                               : (s.loads.empty() ? -1 : 0);
  int arm = -1;
  if (step.node == SimNode::Fixtures && s.type != StudyType::Topology && s.fixtures.empty())
    arm = 0;
  if (arm >= 0 && arm != armedWell)
    st.selection.clear(); // fresh well: no carry-over from a previous pick
  armedWell = arm;
}

const char *setupFocus(SimNode node, StudyType type) {
  switch (node) {
  case SimNode::Material:
    return isFlowFamily(type) ? "fluid" : "material";
  case SimNode::Fixtures:
    return "fixtures";
  case SimNode::Load:
    return "load";
  case SimNode::Mesh:
    return isFlowFamily(type) ? "resolution" : "mesh";
  case SimNode::Boundaries:
    return isFlowFamily(type) ? "flow" : nullptr;
  case SimNode::Study:
    switch (type) {
    case StudyType::Modal:
    case StudyType::Buckling:
      return "modes";
    case StudyType::Fatigue:
      return "cyclic";
    case StudyType::ThermalFea:
      return "conduction";
    case StudyType::Contact:
      return "contact";
    case StudyType::FreqResponse:
      return "excitation";
    case StudyType::Motion:
      return "drivers";
    default:
      return nullptr;
    }
  default:
    return nullptr;
  }
}

// ---- fatigue helpers -----------------------------------------------------------------------------
// Fraction of the (reliability-corrected) endurance allowed at mean stress smv for theory t.
double haighFrac(int t, double smv, double Sut, double SyL) {
  switch (t) {
  case 1: // Gerber
    return 1.0 - (smv / Sut) * (smv / Sut);
  case 2: // Soderberg
    return 1.0 - smv / SyL;
  case 3: { // ASME elliptic
    const double r = smv / SyL;
    return std::sqrt(std::max(1.0 - r * r, 0.0));
  }
  case 4: // Morrow
    return 1.0 - smv / (Sut + 345.0);
  default: // Goodman
    return 1.0 - smv / Sut;
  }
}

// Card disclosure state (UI-session scoped, shared by every study).
bool g_aboutOpen = false;
bool g_meshAdvancedOpen = false;
bool g_meshAdaptiveOpen = false;
// Study whose run was in flight last frame: a run start (footer, ribbon, tree row) opens the Run
// page exactly once, so navigating away mid-run is never overridden.
std::string g_runSeenStudy;

} // namespace

// =================================================================================================
// setup readiness model (drives the stepper sub-chips, the tree rows and the footer "Go to" jump)
// =================================================================================================
std::vector<SimManager::SetupStep> SimManager::setupSteps(AppState &st, const Study &s) {
  std::vector<SetupStep> out;
  auto add = [&](const char *label, const char *title, SimNode node, int status,
                 std::string issue) {
    SetupStep step;
    step.label = label;
    step.title = title;
    step.node = node;
    step.status = status;
    step.issue = std::move(issue);
    out.push_back(std::move(step));
  };

  if (s.type == StudyType::Topology) {
    const auto bcIssues = topology::validateBoundaries(s.topo, topologyMaterialLookup());
    auto firstBlocking = [&](auto pred) -> std::string {
      for (const auto &is : bcIssues)
        if (is.severity == topology::Severity::Blocking && pred(is.key))
          return is.message;
      return {};
    };
    auto startsWith = [](const std::string &k, const char *p) { return k.rfind(p, 0) == 0; };
    std::string setupIssue;
    const nlohmann::json settings = s.topo.value("topology", nlohmann::json());
    if (topoCaps_.state != topology::Capabilities::State::Ready) {
      setupIssue = topoCaps_.state == topology::Capabilities::State::Loading
                       ? "Waiting for the topology solver\xe2\x80\xa6"
                       : topoCaps_.error;
    } else if (!settings.is_object()) {
      setupIssue = "Choose the study settings.";
    } else {
      std::string why;
      if (!topoCaps_.supports(settings.value("method", std::string()),
                              settings.value("physics", std::string()), &why))
        setupIssue = why.empty() ? "Choose a supported physics and method." : why;
      for (const auto &is : topology::validateTopology(settings, topoCaps_, s.topo))
        if (setupIssue.empty() && is.severity == topology::Severity::Blocking)
          setupIssue = is.message;
    }
    const std::string matIssue =
        firstBlocking([&](const std::string &k) { return startsWith(k, "/material"); });
    const std::string bcIssue = firstBlocking([&](const std::string &k) {
      return !startsWith(k, "/material") && !startsWith(k, "/preserve") &&
             !startsWith(k, "/exclude") && !startsWith(k, "/mesh");
    });
    const std::string regIssue = firstBlocking(
        [&](const std::string &k) { return startsWith(k, "/preserve") || startsWith(k, "/exclude"); });
    const std::string meshIssue =
        firstBlocking([&](const std::string &k) { return startsWith(k, "/mesh"); });
    auto issueStatus = [](const std::string &issue) {
      return issue.empty() ? kStepReady : kStepAttention;
    };
    add("Study", "Study settings", SimNode::Study, issueStatus(setupIssue), setupIssue);
    add("Material", "Material", SimNode::Material, issueStatus(matIssue), matIssue);
    add("Loads", "Supports and loads", SimNode::Boundaries, issueStatus(bcIssue), bcIssue);
    add("Regions", "Regions and manufacturing", SimNode::Regions, issueStatus(regIssue),
        regIssue);
    add("Mesh", "Mesh", SimNode::Mesh, issueStatus(meshIssue), meshIssue);
    return out;
  }

  if (s.type == StudyType::Motion) {
    std::string blocker;
    const bool ok =
        studyReady(s, blocker) && motionJointsReady(s, st.docMirror.tree(), blocker);
    add("Drivers", "Drivers", SimNode::Study, ok ? kStepReady : kStepAttention,
        ok ? std::string() : blocker);
    return out;
  }

  if (isFlowFamily(s.type)) {
    // Fluid / Boundaries / Resolution pages (drawFlowForm picks its page from node_): the same
    // checks studyReady() runs, each reported on the page that owns the field.
    float rho = 0.0f, nu = 0.0f;
    flowFluidProps(s, rho, nu);
    std::string fluidIssue;
    if (s.flowFluid < 0 || s.flowFluid > 2)
      fluidIssue = "Choose a fluid.";
    else if (!finiteF(rho) || !finiteF(nu) || rho <= 0.0f || nu <= 0.0f)
      fluidIssue = "Set a positive fluid density and viscosity.";
    std::string bcIssue;
    const float rough = flowRoughnessUm(s);
    if (!finiteF(s.flowSpeed) || s.flowSpeed <= 0.0f)
      bcIssue = "Set a flow speed above zero.";
    else if (!finiteF(s.flowAoA) || !finiteF(s.flowSideslip) || s.flowDir < 0 || s.flowDir > 5)
      bcIssue = "Fix the flow direction or angles.";
    else if (!finiteF(rough) || rough < 0.0f || !finiteF(s.flowRefAreaMm2) ||
             s.flowRefAreaMm2 < 0.0f)
      bcIssue = "Set roughness and reference area to 0 or more.";
    else if (s.type == StudyType::Thermal || (s.flowSolver == 1 && s.flowThermal)) {
      if (!finiteF(s.flowInletTempC))
        bcIssue = "Enter an inlet temperature.";
      else if (s.flowThermalType == 1 &&
               (!finiteF(s.flowSolidKappa) || s.flowSolidKappa <= 0.0f ||
                !finiteF(s.flowPowerW) || s.flowPowerW <= 0.0f))
        bcIssue = "Set a positive solid conductivity and power.";
      else if (s.flowThermalType != 1 &&
               ((s.flowWallMode == 0 && !finiteF(s.flowWallTempC)) ||
                (s.flowWallMode == 1 &&
                 (!finiteF(s.flowWallFluxWm2) || std::fabs(s.flowWallFluxWm2) < 1e-12f)) ||
                (s.flowWallMode == 2 && (!finiteF(s.flowWallPowerW) || s.flowWallPowerW <= 0.0f))))
        bcIssue = "Set a non-zero wall temperature, heat flux or power.";
    }
    std::string resIssue;
    // A missing OpenFOAM is not a setup problem: the chips stay truthful about the setup, and the
    // single unavailable state lives in the Fluid page card, the Setup step and the footer.
    if (s.flowPreset < 0 || s.flowPreset > 3)
      resIssue = "Choose a grid resolution.";
    auto status = [](const std::string &issue) {
      return issue.empty() ? kStepReady : kStepAttention;
    };
    add("Fluid", "Fluid", SimNode::Material, status(fluidIssue), fluidIssue);
    add("Boundaries", "Flow boundaries", SimNode::Boundaries, status(bcIssue), bcIssue);
    add("Resolution", "Grid resolution", SimNode::Mesh, status(resIssue), resIssue);
    return out;
  }

  // ---- study options (the Study node) ----
  switch (s.type) {
  case StudyType::Fatigue: {
    const int mi = materialIndexByKey(s.materialKey);
    double matSut = mi >= 0 ? materials()[mi].ultimate_mpa : 0.0;
    if (s.definition.contains("material"))
      finiteNumber(s.definition["material"], "ultimate_mpa", matSut);
    if (s.definition.contains("fatigue"))
      finiteNumber(s.definition["fatigue"], "ultimate_mpa", matSut);
    float lmin = 0.0f, lmax = 0.0f;
    fatigueLoadFactors(s, lmin, lmax);
    std::string issue;
    if ((!finiteF(s.fatigueSut) || s.fatigueSut <= 0.0f) && (!finiteF(matSut) || matSut <= 0.0))
      issue = "Enter an ultimate strength (Sut).";
    else if (!finiteF(lmin) || !finiteF(lmax) || lmax <= lmin)
      issue = "Make the maximum load factor exceed the minimum.";
    add("Cycle", "Load cycle", SimNode::Study, issue.empty() ? kStepReady : kStepAttention,
        issue);
    break;
  }
  case StudyType::Modal:
    add("Modes", "Modes", SimNode::Study, kStepReady, {});
    break;
  case StudyType::Buckling:
    add("Modes", "Modes", SimNode::Study,
        s.nModes >= 1 ? kStepReady : kStepAttention,
        s.nModes >= 1 ? std::string() : std::string("Set at least one mode."));
    break;
  case StudyType::ThermalFea: {
    std::string issue;
    if (!finiteF(s.thermK) || s.thermK <= 0.0f)
      issue = "Set a solid conductivity above zero.";
    else if (!finiteF(s.thermTInitC))
      issue = "Enter an initial temperature.";
    else if (s.thermTransient &&
             (!finiteF(s.thermCp) || s.thermCp <= 0.0f || !finiteF(s.thermTEndS) ||
              s.thermTEndS <= 0.0f || !finiteF(s.thermDtS) || s.thermDtS < 0.0f))
      issue = "Fix the transient time settings.";
    add("Study", "Conduction settings", SimNode::Study,
        issue.empty() ? kStepReady : kStepAttention, issue);
    break;
  }
  case StudyType::FreqResponse: {
    const bool ok = finiteF(s.frBaseG) && s.frBaseG > 0.0f && finiteF(s.frZeta) &&
                    s.frZeta > 0.0f && s.frZeta < 1.0f;
    add("Input", "Base excitation", SimNode::Study, ok ? kStepReady : kStepAttention,
        ok ? std::string()
           : std::string("Set base input and damping (0 to 1)."));
    break;
  }
  case StudyType::Contact: {
    const bool ok = finiteF(s.contactPressureMpa) && s.contactPressureMpa > 0.0f;
    add("Load", "Applied load", SimNode::Study, ok ? kStepReady : kStepAttention,
        ok ? std::string() : std::string("Set a contact pressure above zero."));
    break;
  }
  default:
    break;
  }

  // ---- material ----
  {
    std::string issue;
    (void)nativeMaterialReady(s, issue);
    add("Material", "Material", SimNode::Material, issue.empty() ? kStepReady : kStepAttention,
        issue);
  }
  if (s.type == StudyType::Contact)
    return out; // the contact proxy has no fixture/load/mesh setup

  // ---- fixtures ----
  {
    const bool thermal = s.type == StudyType::ThermalFea;
    bool hasConvection = false;
    for (const auto &ld : s.loads)
      hasConvection = hasConvection || ld.thermalKind == 1;
    if (!s.fixtures.empty()) {
      const bool ok = !thermal || finiteF(s.thermFixTempC);
      add(thermal ? "Temp" : "Fixtures", thermal ? "Fixed temperature" : "Fixtures",
          SimNode::Fixtures,
          ok ? kStepReady : kStepAttention,
          ok ? std::string() : std::string("Enter the fixed temperature."));
    } else if (s.type == StudyType::Modal || s.type == StudyType::FreqResponse) {
      add("Fixtures", "Fixtures", SimNode::Fixtures, kStepOptional,
          "Optional: without fixtures the modes are free-free.");
    } else if (thermal) {
      const bool needed = !s.thermTransient && !hasConvection;
      add("Temp", "Fixed temperature", SimNode::Fixtures,
          needed ? kStepAttention : kStepOptional,
          needed ? "Add a fixed-temp face or convection load."
                 : "Optional: faces held at a fixed temperature.");
    } else {
      add("Fixtures", "Fixtures", SimNode::Fixtures, kStepAttention,
          "Pick at least one fixed face.");
    }
  }

  // ---- loads ----
  if (s.type == StudyType::Static || s.type == StudyType::Fatigue ||
      s.type == StudyType::Buckling || s.type == StudyType::ThermalFea) {
    const bool thermal = s.type == StudyType::ThermalFea;
    std::string issue;
    int status = kStepReady;
    if (s.loads.empty()) {
      if (thermal) {
        status = s.fixtures.empty() ? kStepAttention : kStepOptional;
        issue = s.fixtures.empty() ? "Add a fixed-temp face or heat load."
                                   : "Optional: heat-flux or convection faces.";
      } else {
        status = kStepAttention;
        issue = "Add a force or pressure load.";
      }
    }
    bool driving = false;
    for (int i = 0; i < (int)s.loads.size() && issue.empty(); ++i) {
      const SimLoad &ld = s.loads[i];
      const std::string n = std::to_string(i + 1);
      if (!nativeLoadHasFaces(ld)) {
        issue = "Pick faces for load " + n + ".";
      } else if (thermal) {
        if (ld.thermalKind == 1 && (!finiteF(ld.hWm2k) || ld.hWm2k <= 0.0f || !finiteF(ld.tAmbC)))
          issue = "Set h and ambient temperature for convection " + n + ".";
        else if (ld.thermalKind != 1 && (!finiteF(ld.fluxWm2) || std::fabs(ld.fluxWm2) < 1e-12))
          issue = "Give heat flux " + n + " a non-zero value.";
      } else if (ld.pressure) {
        if (!finiteF(ld.pressureMpa))
          issue = "Set a finite pressure for load " + n + ".";
        driving = driving || ld.pressureMpa != 0.0f;
      } else {
        const double mag2 = (double)ld.vec[0] * ld.vec[0] + (double)ld.vec[1] * ld.vec[1] +
                            (double)ld.vec[2] * ld.vec[2];
        if (!finiteF(mag2))
          issue = "Give force " + n + " a finite vector.";
        driving = driving || mag2 > 0.0;
      }
      if (!issue.empty())
        status = kStepAttention;
    }
    if (!thermal && issue.empty() && !driving) {
      status = kStepAttention;
      issue = "Give at least one load a non-zero force or pressure.";
    }
    add(thermal ? "Heat" : "Loads", thermal ? "Heat loads" : "Loads", SimNode::Load, status,
        issue);
  }

  // ---- mesh ----
  {
    const bool ok = finiteF(s.meshFineness) && s.meshFineness >= 0.0f && s.meshFineness <= 1.0f &&
                    finiteF(s.meshTargetMm) && s.meshTargetMm >= 0.0f && finiteF(s.meshMinMm) &&
                    s.meshMinMm >= 0.0f &&
                    !(s.meshTargetMm > 0.0f && s.meshMinMm > s.meshTargetMm) &&
                    (s.meshOrder == 1 || s.meshOrder == 2);
    add("Mesh", "Mesh", SimNode::Mesh, ok ? kStepReady : kStepAttention,
        ok ? std::string()
           : std::string("Fix the mesh sizes: minimum exceeds target."));
  }
  return out;
}

// =================================================================================================
// material
// =================================================================================================
void SimManager::drawCustomMaterialPopup(AppState &st, Study &s) {
  static constexpr const char *kPopup = "New material###sim_new_material";
  // Width from layout tokens; height 0 auto-fits the content every frame.
  ImGui::SetNextWindowSize(ImVec2(m::pmLabelCol() + m::reFieldW() + m::U(16.0f), 0.0f),
                           ImGuiCond_Always);
  if (!ImGui::BeginPopupModal(kPopup, nullptr,
                              ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoSavedSettings))
    return;
  pushCardText(); // the modal fills with the opaque theme glass; re-pair the inherited ink
  const UnitSys u = unitSys(st);
  const char *stressUnit = kit::unitText(u.stress);
  kit::dimText("Saved to your library and available in every study.");
  {
    kit::FieldGroup g("cm", "Ultimate strength Sut");
    kit::fieldText("name", "Name", cmName_, sizeof(cmName_),
                   kit::FieldOpts().withRequired().withHint("e.g. 6061-T6 (supplier)"));
    kit::fieldNumber("E", "Young's modulus E", &cmE_,
                     kit::FieldOpts()
                         .withRequired()
                         .withUnit(stressUnit)
                         .withScale(u.stressFromMpa)
                         .withMin(0.0)
                         .withTip("Elastic stiffness. Steel ~200 GPa, aluminum ~69 GPa, most "
                                  "plastics 1.5-3.5 GPa."));
    kit::fieldNumber("nu", "Poisson ratio", &cmNu_,
                     kit::FieldOpts().withRequired().withRange(0.0, 0.499).withTip(
                         "Lateral contraction ratio, 0-0.5. Metals ~0.3; rubber approaches 0.5."));
    kit::fieldNumber("rho", "Density", &cmDensity_,
                     kit::FieldOpts()
                         .withUnit(densityUnit(u))
                         .withScale(densityScale(u))
                         .withMin(0.0)
                         .withTip("Mass density, used by modal and transient studies."));
    kit::fieldNumber("yield", "Yield strength", &cmYield_,
                     kit::FieldOpts()
                         .withUnit(stressUnit)
                         .withScale(u.stressFromMpa)
                         .withMin(0.0)
                         .withTip("Stress at which the material yields; drives the factor of "
                                  "safety."));
    kit::fieldNumber("sut", "Ultimate strength Sut", &cmUlt_,
                     kit::FieldOpts()
                         .withUnit(stressUnit)
                         .withScale(u.stressFromMpa)
                         .withMin(0.0)
                         .withTip("Ultimate tensile strength, used by fatigue studies."));
    kit::fieldNumber("se", "Endurance limit Se", &cmEnd_,
                     kit::FieldOpts()
                         .withUnit(stressUnit)
                         .withScale(u.stressFromMpa)
                         .withMin(0.0)
                         .withTip("Fatigue endurance limit. ~0.5 x Sut for steels; lower for "
                                  "aluminum and other non-ferrous alloys."));
  }
  const bool locked = st.docLockedByAgent(st.docMirror.docId());
  const std::string lockReason = locked ? st.agentLockReason(st.docMirror.docId()) : "";
  const char *reason = cmName_[0] == '\0' ? "Name the material."
                       : !(std::isfinite(cmE_) && cmE_ > 0.0f)
                           ? "Young's modulus must be greater than zero."
                       : !(std::isfinite(cmNu_) && cmNu_ > 0.0f && cmNu_ < 0.5f)
                           ? "Poisson ratio must be between 0 and 0.5."
                       : !(std::isfinite(cmDensity_) && cmDensity_ >= 0.0f &&
                           std::isfinite(cmYield_) && cmYield_ >= 0.0f &&
                           std::isfinite(cmUlt_) && cmUlt_ >= 0.0f && std::isfinite(cmEnd_) &&
                           cmEnd_ >= 0.0f)
                           ? "Density and strengths cannot be negative."
                           : nullptr;
  if (locked) reason = lockReason.c_str();
  if (reason)
    kit::inlineStatus(reason, kit::Tone::Warn);
  kit::gapRow();
  const float gap = ImGui::GetStyle().ItemSpacing.x;
  const float half = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
  if (kit::secondaryButton("Cancel###cm_cancel", true, nullptr, ImVec2(half, m::reBtnH())))
    ImGui::CloseCurrentPopup();
  ImGui::SameLine(0.0f, gap);
  if (kit::primaryButton("Add material###cm_add", reason == nullptr, reason,
                         ImVec2(half, m::reBtnH()))) {
    if (st.docLockedByAgent(st.docMirror.docId())) {
      st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
    } else {
      Material mat;
      mat.name = cmName_;
      mat.E_mpa = cmE_;
      mat.nu = cmNu_;
      mat.density_g_cm3 = cmDensity_;
      mat.yield_mpa = cmYield_;
      mat.ultimate_mpa = cmUlt_;
      mat.endurance_mpa = cmEnd_;
      // The custom material becomes the part's active material (set_material) too.
      const std::string key = addCustomMaterial(mat);
      if (key.empty()) {
        st.toast(ui::ToastLevel::Error, "Could not write the custom material library");
      } else if (applyPartMaterial(st, s, key)) {
        persist(st, s);
        ImGui::CloseCurrentPopup();
      }
    }
  }
  popCardText();
  ImGui::EndPopup();
}

void SimManager::drawMaterialCombo(AppState &st, Study &s) {
  const auto &mats = materials();
  const UnitSys u = unitSys(st);
  const int mi = materialIndexByKey(s.materialKey);
  const bool nativeMaterial = s.definition.contains("material") &&
                              s.definition["material"].is_object();

  const bool locked = st.docLockedByAgent(st.docMirror.docId());
  const std::string lockReason = locked ? st.agentLockReason(st.docMirror.docId()) : "";
  // Combo labels, rebuilt only when the library changes (a custom material was added).
  static std::vector<std::string> names;
  static const void *namesFor = nullptr;
  static std::size_t namesCount = 0;
  if (namesFor != mats.data() || namesCount != mats.size()) {
    names.clear();
    names.reserve(mats.size());
    for (const auto &mat : mats)
      names.push_back(mat.custom ? mat.name + " (custom)" : mat.name);
    namesFor = mats.data();
    namesCount = mats.size();
  }

  std::string issue;
  if (!nativeMaterial && mi < 0 && !s.materialKey.empty())
    issue = "The part material '" + s.materialKey +
            "' is not in the simulation library; pick a library material.";
  (void)takeFocus("material");
  if (kit::cardBegin("material", "Material", "material")) {
    kit::FieldGroup g("material", "Material");
    if (nativeMaterial) {
      kit::fieldReadout("native_material", "Native material", "Configured in parameters");
      if (kit::secondaryButton("Edit material parameters###native_material_edit"))
        handleCommand(st, "sim_parameters");
      kit::dimText("Selecting a library material below explicitly replaces the native material.");
    }
    int sel = mi;
    if (kit::fieldCombo(
            "mat", "Material", &sel, names,
            kit::FieldOpts()
                .withRequired(!nativeMaterial)
                .withHint(nativeMaterial ? "Native material" : "Select a material")
                .withIssue(issue.empty() ? nullptr : issue.c_str(), kit::Tone::Warn)
                .withDisabled(locked, lockReason.c_str())
                .withTip("Stiffness, Poisson ratio, density and strengths for the solve. "
                         "Changing it updates the part's active material (FeatureManager "
                         "Material row and Mass Properties).")) &&
        sel >= 0 && sel != mi) {
      if (applyPartMaterial(st, s, mats[sel].key))
        persist(st, s);
    }
    if (kit::secondaryButton("+ New material###newmat", !locked, lockReason.c_str())) {
      // Seed the editor from the current pick (or keep the last values) and open it.
      if (mi >= 0) {
        std::snprintf(cmName_, sizeof(cmName_), "%s (copy)", mats[mi].name.c_str());
        cmE_ = (float)mats[mi].E_mpa;
        cmNu_ = (float)mats[mi].nu;
        cmDensity_ = (float)mats[mi].density_g_cm3;
        cmYield_ = (float)mats[mi].yield_mpa;
        cmUlt_ = (float)mats[mi].ultimate_mpa;
        cmEnd_ = (float)mats[mi].endurance_mpa;
      }
      ImGui::OpenPopup("New material###sim_new_material");
    }
    kit::itemTooltip("Define a custom material, saved to your library.");
  }
  kit::cardEnd();
  drawCustomMaterialPopup(st, s);

  if (mi >= 0) {
    const auto &mat = mats[mi];
    const char *su = kit::unitText(u.stress);
    kit::keyValueCard(
        "matprops", "Properties", nullptr,
        {{"Young's modulus", kit::fmtQuantityScaled(mat.E_mpa * u.stressFromMpa, su)},
         {"Poisson ratio", kit::fmtValue(mat.nu, 3)},
         {"Yield strength", kit::fmtQuantityScaled(mat.yield_mpa * u.stressFromMpa, su)},
         {"Ultimate strength", mat.ultimate_mpa > 0.0
                                   ? kit::fmtQuantityScaled(mat.ultimate_mpa * u.stressFromMpa, su)
                                   : std::string("Not set")},
         {"Density", kit::fmtQuantity(mat.density_g_cm3 * densityScale(u), densityUnit(u), 3)}});
  }
}

// =================================================================================================
// face wells, fixtures, loads
// =================================================================================================
FaceWellClick faceWellControls(const char *label, int picked, bool armed, const char *tip) {
  FaceWellClick out;
  char lab[96];
  if (picked > 0)
    std::snprintf(lab, sizeof(lab), "%s (%d)", label, picked);
  else
    std::snprintf(lab, sizeof(lab), "%s", label);
  ImGui::TextUnformatted(lab);
  if (tip && tip[0])
    kit::itemTooltip(tip);
  const ImGuiStyle &style = ImGui::GetStyle();
  const float avail = ImGui::GetContentRegionAvail().x;
  // Same kit button family (one frame height): secondary at rest, accent while armed.
  out.toggled = armed ? kit::primaryButton("Picking\xe2\x80\xa6###arm")
                      : kit::secondaryButton("Pick faces###arm");
  kit::itemTooltip(armed ? "Stop picking faces for this field."
                         : "Arm this field, then click model faces in the viewport.");
  if (picked > 0) {
    const float clearW = ImGui::CalcTextSize("Clear").x + style.FramePadding.x * 2.0f;
    if (ImGui::GetItemRectSize().x + style.ItemSpacing.x + clearW <= avail)
      ImGui::SameLine(0.0f, style.ItemSpacing.x);
    out.clear = kit::secondaryButton("Clear###clear");
    kit::itemTooltip("Remove every face from this field.");
  }
  return out;
}

// Face wells accept copied geometry independently of the click-to-toggle picker. Chips keep their
// removal and hover behavior and can be dragged to another well without changing the source.
void SimManager::drawWellRow(AppState &st, const char *label, SimWell &well, int armId,
                             const char *tip) {
  ImGuiContext &g = *GImGui;
  ImGui::PushID(armId);
  const Study &study = studies_[sel_];
  const SimRunUi live = runUi();
  const bool editable = !live.running || live.studyId != study.id;
  const bool armed = editable && armedWell_ == armId;
  const int n = (int)well.picks.size();
  ImGui::BeginDisabled(!editable);
  const FaceWellClick click = faceWellControls(label, n, armed, tip);
  const bool toggled = click.toggled;
  const bool clear = click.clear;
  if (armed)
    kit::dimText("Click faces in the viewport to add or remove them.");

  kit::secondaryButton("Drop faces here###face_drop", editable, nullptr,
                       ImVec2(ImGui::GetContentRegionAvail().x, 0.0f));
  sim::FaceDrop dropped;
  const bool delivered = editable && sim::acceptFaceDrop(st.docMirror.docId(), dropped);
  kit::itemTooltip("Drag a model face or a face chip here to add a copy. Existing faces stay selected.");
  int removeIdx = -1;
  if (n > 0) {
    char chipLab[24];
    kit::chipFlowBegin("faces");
    for (int k = 0; k < n; ++k) {
      // chipFlowItem owns a scope until the next item; use a unique explicit chip id rather
      // than wrapping that flow scope in an outer PushID/PopID pair.
      char chipId[32];
      std::snprintf(chipId, sizeof(chipId), "face_%d", k);
      std::snprintf(chipLab, sizeof(chipLab),
                    well.picks[k].selector.contains("points") ? "Region %d" : "Face %d", k + 1);
      bool hovered = false;
      if (kit::removableChip(chipId, chipLab, "Remove this face", kit::Tone::Neutral, &hovered))
        removeIdx = k;
      ImGui::PushID(chipId);
      IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetID("##body"), chipLab,
                                 ImGui::GetCurrentContext()->LastItemData.StatusFlags);
      ImGui::PopID();
      if (editable && removeIdx != k)
        sim::faceDragSource(st.docMirror.docId(), well.picks[k].selector,
                            well.picks[k].pickBody, well.picks[k].pickTopo, chipLab);
      if (hovered)
        claimViewportHoverFace(well.picks[k].pickBody, well.picks[k].pickTopo);
    }
    kit::chipFlowEnd();
  }
  ImGui::EndDisabled();
  ImGui::PopID();

  if (!editable)
    return;
  if (toggled) {
    armedWell_ = armed ? -1 : armId;
    if (!armed)
      st.selection.clear(); // fresh well: no carry-over from a previously picked face
  }
  bool changed = false;
  if (clear) {
    well.picks.clear();
    changed = true;
  } else if (removeIdx >= 0) {
    well.picks.erase(well.picks.begin() + removeIdx);
    changed = true;
  } else if (delivered) {
    nlohmann::json selector = dropped.selector;
    selector.erase("_pick");
    const bool duplicate = std::any_of(well.picks.begin(), well.picks.end(),
                                       [&](const SimSel &pick) {
      if (pick.pickBody == dropped.body && pick.pickTopo == dropped.topo)
        return true;
      nlohmann::json existing = pick.selector;
      existing.erase("_pick");
      return existing == selector;
    });
    if (!duplicate) {
      dropped.selector["_pick"] = {{"body", dropped.body}, {"topo", dropped.topo}};
      well.picks.push_back({std::move(dropped.selector), dropped.body, dropped.topo});
      changed = true;
    }
  }
  if (changed)
    persist(st, studies_[sel_]);
}

void SimManager::drawFixturesForm(AppState &st, Study &s) {
  const bool thermal = s.type == StudyType::ThermalFea;
  const bool optional = s.type == StudyType::Modal || s.type == StudyType::FreqResponse;
  const char *title = thermal    ? "Fixed temperature"
                      : optional ? "Fixtures (optional)"
                                 : "Fixtures";
  (void)takeFocus("fixtures");
  if (kit::cardBegin("fixtures", title, "fixture")) {
    kit::FieldGroup g("fx", "Fixed temperature");
    drawWellRow(st, thermal ? "Faces" : "Fixed faces", s.fixtures, 0,
                thermal    ? "Faces held at the fixed temperature below."
                : optional ? "Faces clamped in place (zero displacement). Without fixtures the "
                             "modes are free-free."
                           : "Faces clamped in place (zero displacement); they carry the "
                             "reaction to your loads.");
    if (thermal &&
        kit::fieldNumber("tfix", "Fixed temperature", &s.thermFixTempC,
                         kit::FieldOpts()
                             .withUnit(kit::Unit::DegC)
                             .withTip("Temperature held on the faces above (thermal FEA fixtures "
                                      "are fixed-temperature faces, not structural clamps).")))
      persist(st, s);
  }
  kit::cardEnd();
}

void SimManager::drawLoadsForm(AppState &st, Study &s) {
  const bool thermal = s.type == StudyType::ThermalFea;
  const UnitSys u = unitSys(st);
  // Focus anchors ABOVE what it reveals (SetScrollHereY targets the previous line): the first
  // load (or none) lands with the section header in view, a later one under its predecessor.
  if (s.loads.empty() || loadIdx_ <= 0 || loadIdx_ >= (int)s.loads.size())
    (void)takeFocus("load");
  kit::sectionHeader(thermal ? "Heat loads" : "Loads", thermal ? "load_heat" : "load_force",
                     nullptr, (int)s.loads.size(), true);
  if (s.loads.empty())
    kit::emptyState(thermal ? "load_heat" : "load_force",
                    thermal ? "No heat loads yet" : "No loads yet",
                    thermal ? "Add a heat flux or a convection boundary below, then pick the "
                              "faces it acts on."
                            : "Add a force or a pressure below, then pick the faces it acts on.");

  int removeIdx = -1;
  for (int li = 0; li < (int)s.loads.size(); ++li) {
    SimLoad &ld = s.loads[li];
    char title[48];
    const char *kind = thermal ? (ld.thermalKind == 1 ? "Convection" : "Heat flux")
                               : (ld.pressure ? "Pressure" : "Force");
    std::snprintf(title, sizeof(title), "%s %d", kind, li + 1);
    const char *icon = thermal ? "load_heat" : (ld.pressure ? "load_pressure" : "load_force");
    ImGui::PushID(li);
    bool removed = false;
    if (li == loadIdx_ && li > 0)
      (void)takeFocus("load");
    if (kit::removableCard("load", title, icon, li == loadIdx_ ? ui::palette.accent : nullptr,
                           "Remove this load", &removed)) {
      kit::FieldGroup g("ld", "Film coefficient h");
      if (ld.definition.contains("faces") && ld.definition["faces"].is_object()) {
        kit::fieldReadout("native_faces", "Faces", "Native faceset");
        kit::dimText("Resolved geometry and region settings are retained in parameters.");
        if (kit::secondaryButton("Edit faceset parameters###native_faces_edit"))
          handleCommand(st, "sim_parameters");
      } else {
        drawWellRow(st, "Faces", ld.faces, li + 1, "Faces this load acts on.");
      }
      if (thermal) {
        if (ld.thermalKind == 1) {
          if (kit::fieldNumber("h", "Film coefficient h", &ld.hWm2k,
                               kit::FieldOpts()
                                   .withUnit(kit::Unit::W_m2K)
                                   .withMin(0.0)
                                   .withIssue(ld.hWm2k > 0.0f ? nullptr
                                                              : "Must be greater than zero.")
                                   .withTip("Convective film coefficient. Still air ~5-25, "
                                            "forced air ~25-250.")))
            persist(st, s);
          if (kit::fieldNumber("tamb", "Ambient temperature", &ld.tAmbC,
                               kit::FieldOpts().withUnit(kit::Unit::DegC).withTip(
                                   "Temperature of the surrounding fluid.")))
            persist(st, s);
        } else if (kit::fieldNumber("q", "Heat flux", &ld.fluxWm2,
                                    kit::FieldOpts()
                                        .withUnit(kit::Unit::W_m2)
                                        .withIssue(std::fabs(ld.fluxWm2) > 1e-12f
                                                       ? nullptr
                                                       : "Must be non-zero.")
                                        .withTip("Heat flux into the faces (negative = out).")))
          persist(st, s);
      } else if (ld.pressure) {
        if (kit::fieldNumber("p", "Pressure", &ld.pressureMpa,
                             kit::FieldOpts()
                                 .withUnit(kit::unitText(u.press))
                                 .withScale(u.pressFromMpa)
                                 .withIssue(finiteF(ld.pressureMpa) ? nullptr
                                                                   : "Enter a finite pressure.")
                                 .withTip("Uniform pressure: positive pushes into the faces; "
                                          "negative pulls outward.")))
          persist(st, s);
      } else {
        if (kit::fieldVec3("f", "Force", ld.vec,
                           kit::FieldOpts()
                               .withUnit(kit::unitText(u.force))
                               .withScale(u.forceFromN)
                               .withIssue(finiteF(ld.vec[0]) && finiteF(ld.vec[1]) &&
                                                  finiteF(ld.vec[2])
                                              ? nullptr : "Enter a finite force vector.")
                               .withTip("Total force along world X / Y / Z, spread evenly over "
                                        "the faces.")))
          persist(st, s);
        static const char *const kDirections[] = {"+X", "-X", "+Y", "-Y", "+Z", "-Z"};
        int preset = -1;
        if (kit::fieldEnum("direction", "Align force", &preset, kDirections, 6,
                           kit::FieldOpts().withTip(
                               "Align the current force magnitude to a world axis. The vector "
                               "above remains fully editable."))) {
          const double magnitude = std::sqrt((double)ld.vec[0] * ld.vec[0] +
                                             (double)ld.vec[1] * ld.vec[1] +
                                             (double)ld.vec[2] * ld.vec[2]);
          ld.vec[0] = ld.vec[1] = ld.vec[2] = 0.0f;
          ld.vec[preset / 2] = (float)((preset % 2 ? -1.0 : 1.0) * magnitude);
          persist(st, s);
        }
      }
    }
    kit::cardEnd();
    ImGui::PopID();
    if (removed)
      removeIdx = li;
  }
  if (removeIdx >= 0) {
    s.loads.erase(s.loads.begin() + removeIdx);
    // Keep the armed well and the selected load pointing at the same entries.
    if (armedWell_ == removeIdx + 1)
      armedWell_ = -1;
    else if (armedWell_ > removeIdx + 1)
      --armedWell_;
    if (loadIdx_ == removeIdx)
      loadIdx_ = -1;
    else if (loadIdx_ > removeIdx)
      --loadIdx_;
    persist(st, s);
  }

  kit::gapRow();
  auto addLoad = [&](SimLoad l) {
    s.loads.push_back(std::move(l));
    persist(st, s);
    st.selection.clear();
    node_ = SimNode::Load;
    loadIdx_ = (int)s.loads.size() - 1;
    armedWell_ = (int)s.loads.size(); // arm the new load's face well
  };
  const char *addA = thermal ? "+ Heat flux###add_a" : "+ Force###add_a";
  const char *addB = thermal ? "+ Convection###add_b" : "+ Pressure###add_b";
  if (kit::secondaryButton(addA)) {
    SimLoad l;
    l.thermalKind = 0;
    addLoad(l);
  }
  kit::itemTooltip(thermal ? "Add a heat flux into a set of faces."
                           : "Add a force vector on a set of faces.");
  const float nextW = ImGui::CalcTextSize(thermal ? "+ Convection" : "+ Pressure").x +
                      ImGui::GetStyle().FramePadding.x * 2.0f;
  if (ImGui::GetItemRectSize().x + ImGui::GetStyle().ItemSpacing.x + nextW <=
      ImGui::GetContentRegionAvail().x)
    ImGui::SameLine();
  if (kit::secondaryButton(addB)) {
    SimLoad l;
    if (thermal)
      l.thermalKind = 1;
    else
      l.pressure = true;
    addLoad(l);
  }
  kit::itemTooltip(thermal ? "Add a convection boundary (film coefficient + ambient)."
                           : "Add a uniform pressure on a set of faces.");
}

// =================================================================================================
// mesh
// =================================================================================================
void SimManager::drawMeshForm(AppState &st, Study &s) {
  const UnitSys u = unitSys(st);
  const bool modal = s.type == StudyType::Modal;
  const bool topologyStudy = s.type == StudyType::Topology;
  const bool linearOnly = modal || topologyStudy;

  // Fineness tip names the resolution band the slider sits in (no separate readout row).
  const char *fineWord = s.meshFineness < 0.2f   ? "Coarse"
                         : s.meshFineness < 0.4f ? "Coarse-medium"
                         : s.meshFineness < 0.6f ? "Medium"
                         : s.meshFineness < 0.8f ? "Fine-medium"
                                                 : "Fine";
  const std::string fineTip =
      std::string("Resolution: ") + fineWord +
      ". Polyhedral-FEA fills the part with graded tetrahedra sized from its bounding box: 0 % "
      "is coarse and fast, 100 % fine and accurate. Finer meshes resolve stress concentrations "
      "better but take longer.";
  (void)takeFocus("mesh");
  if (kit::cardBegin("mesh", "Mesh", "mesh")) {
    kit::FieldGroup g("mesh", "Element order");
    if (kit::fieldSlider("fine", "Fineness", &s.meshFineness, 0.0f, 1.0f,
                         kit::FieldOpts()
                             .withScale(100.0)
                             .withUnit(kit::Unit::Percent)
                             .withSigFigs(2)
                             .withTip(fineTip.c_str()))) {
      persist(st, s);
      clearMeshPreview();
    }
    if (linearOnly) {
      // Forced, not a choice: a read-only value with the reason, never a greyed combo.
      const char *why = modal ? "Modal studies always solve on linear tetrahedra."
                              : "Topology optimization always solves on linear tetrahedra.";
      kit::fieldReadout("order", "Element order", "Linear tetrahedra",
                        kit::FieldOpts().withTip(why));
      kit::dimText(why);
    } else {
      static const char *const kOrder[2] = {"Linear", "Quadratic"};
      static const char *const kOrderTips[2] = {
          "Linear tetrahedra (tet4): fast draft.",
          "Quadratic tetrahedra (tet10): captures bending and curved boundaries more accurately."};
      int orderIndex = s.meshOrder == 2 ? 1 : 0;
      if (kit::fieldEnum("order", "Element order", &orderIndex, kOrder, 2,
                         kit::FieldOpts().withItemTips(kOrderTips))) {
        s.meshOrder = orderIndex ? 2 : 1;
        persist(st, s);
        clearMeshPreview();
      }
    }
    // The card's own action, right under its fields so it is never below the fold.
    kit::gapRow();
    const bool busy = st.simClient && st.simClient->busy();
    const char *previewLabel =
        meshPreviewPending_ ? "Meshing\xe2\x80\xa6###preview" : "Preview mesh###preview";
    if (kit::secondaryButton(previewLabel, !busy && !meshPreviewPending_,
                             meshPreviewPending_ ? "A mesh preview is already in progress."
                                                 : "The solver is busy with a run.",
                             ImVec2(ImGui::GetContentRegionAvail().x, 0.0f)))
      previewMesh(st, s);
    kit::itemTooltip("Mesh the part now and show the element wireframe, counts and quality "
                     "without solving.");
  }
  kit::cardEnd();

  if (topologyStudy) {
    const nlohmann::json order = s.topo.value("mesh_order", nlohmann::json(1));
    if (!(order.is_number_integer() && order.get<int>() == 1)) {
      kit::inlineStatus("Saved with quadratic elements; topology needs linear ones.",
                        kit::Tone::Err);
      if (kit::secondaryButton("Use linear elements###topo_tet4")) {
        s.topo["mesh_order"] = 1;
        persist(st, s);
        clearMeshPreview();
      }
    }
  }

  if (kit::cardBegin("advanced", "Advanced", nullptr, &g_meshAdvancedOpen)) {
    kit::FieldGroup g("adv", "Advisor DOF budget");
    const char *lenUnit = kit::unitText(u.len);
    bool targetSet = s.meshTargetMm > 0.0f;
    bool minSet = s.meshMinMm > 0.0f;
    const bool minTooBig = s.meshTargetMm > 0.0f && s.meshMinMm > s.meshTargetMm;
    if (kit::fieldNumber("target", "Target size", &s.meshTargetMm,
                         kit::FieldOpts()
                             .withPresent(&targetSet)
                             .withHint("auto")
                             .withUnit(lenUnit)
                             .withScale(u.lenFromMm)
                             .withMin(0.0)
                             .withTip("Explicit target edge length; overrides fineness. Leave "
                                      "empty for automatic."))) {
      if (!targetSet)
        s.meshTargetMm = 0.0f;
      persist(st, s);
      clearMeshPreview();
    }
    if (kit::fieldNumber("min", "Minimum size", &s.meshMinMm,
                         kit::FieldOpts()
                             .withPresent(&minSet)
                             .withHint("auto")
                             .withUnit(lenUnit)
                             .withScale(u.lenFromMm)
                             .withMin(0.0)
                             .withIssue(minTooBig ? "Cannot exceed the target size." : nullptr)
                             .withTip("Smallest edge length for graded refinement. Leave empty "
                                      "for automatic."))) {
      if (!minSet)
        s.meshMinMm = 0.0f;
      persist(st, s);
      clearMeshPreview();
    }
    if (supportsLearnedAdvisor(s.type)) {
      if (kit::fieldCheckbox("advisor", "Use learned mesh advisor", &s.learnedAdvisor,
                             kit::FieldOpts().withTip(
                                 "Polyhedral-FEA's optional learned sizing and order advice. "
                                 "Chudware still enforces resource, mesh-quality and solver "
                                 "gates."))) {
        persist(st, s);
        clearMeshPreview();
      }
      if (s.learnedAdvisor) {
        bool dofSet = s.advisorMaxDof > 0;
        if (kit::fieldInt("dof", "Advisor DOF budget", &s.advisorMaxDof,
                          kit::FieldOpts()
                              .withPresent(&dofSet)
                              .withHint("unset")
                              .withMin(0)
                              .withTip("Upper bound on degrees of freedom the advisor may "
                                       "recommend. Leave empty for no ceiling."))) {
          if (!dofSet)
            s.advisorMaxDof = 0;
          persist(st, s);
          clearMeshPreview();
        }
      }
    }
  }
  kit::cardEnd();

  if (s.type == StudyType::Static) {
    if (kit::cardBegin("adaptive", "Adaptive refinement", nullptr, &g_meshAdaptiveOpen)) {
      kit::FieldGroup g("adapt", "Error indicator target");
      if (kit::fieldCheckbox("on", "Refine adaptively", &s.adaptEnabled,
                             kit::FieldOpts().withTip(
                                 "Re-mesh and re-solve toward the largest recovered "
                                 "discretization error until the target or the pass limit is "
                                 "reached."))) {
        persist(st, s);
        clearMeshPreview();
      }
      if (s.adaptEnabled) {
        if (kit::fieldInt("passes", "Maximum passes", &s.adaptMaxPasses,
                          kit::FieldOpts().withRange(1, 6).withTip(
                              "Upper bound on re-mesh / re-solve passes."))) {
          persist(st, s);
          clearMeshPreview();
        }
        if (kit::fieldNumber("eta", "Error indicator target", &s.adaptEtaTarget,
                             kit::FieldOpts().withMin(0.0).withTip(
                                 "Stop once the global error indicator drops below this "
                                 "value (0.05 = 5 %)."))) {
          persist(st, s);
          clearMeshPreview();
        }
      }
    }
    kit::cardEnd();
  }

  if (s.nNodes > 0 || s.nElems > 0) {
    std::vector<kit::KeyValueItem> rows = {
        {"Nodes", groupedCount((uint64_t)std::max(0, s.nNodes))},
        {"Elements", groupedCount((uint64_t)std::max(0, s.nElems))}};
    if (!s.meshTypeUsed.empty())
      rows.push_back({"Element type", s.meshTypeUsed});
    rows.push_back({"Mesher", "Polyhedral-FEA"});
    if (!s.meshNote.empty())
      rows.push_back({"Note", s.meshNote});
    kit::keyValueCard("meshstats", "Mesh statistics", "mesh", rows);
    if (!meshPreviewSegs_.empty() && meshPreviewStudyId_ == s.id) {
      // Honest LOD disclosure: the preview may be decimated; the solve never is.
      if (meshPreviewEdgesShown_ < meshPreviewEdgesTotal_ || meshPreviewCoarse_) {
        const std::string lod =
            "Showing " + groupedCount((uint64_t)meshPreviewEdgesShown_) + " of " +
            groupedCount((uint64_t)meshPreviewEdgesTotal_) + " edges" +
            (meshPreviewCoarse_ ? " (coarse LOD)." : " (display budget).");
        kit::inlineStatus(lod.c_str(), kit::Tone::Warn);
        if (meshPreviewCoarse_) {
          const std::string sampled =
              "The coarse LOD samples " + groupedCount((uint64_t)meshPreviewElemsUsed_) +
              " of " + groupedCount((uint64_t)meshPreviewElemsTotal_) +
              " elements: an indicative wire cloud, not the exact surface mesh.";
          kit::dimText(sampled.c_str());
        }
        kit::dimText("The solve uses the full mesh; only the preview is decimated.");
      } else {
        kit::dimText("Wireframe shown in the viewport.");
      }
    }
  }

  if (s.meshHasQuality) {
    const kit::Tone poorTone = s.meshPctPoor < 1.0f   ? kit::Tone::Ok
                               : s.meshPctPoor < 5.0f ? kit::Tone::Warn
                                                      : kit::Tone::Err;
    kit::keyValueCard(
        "meshquality", "Mesh quality", nullptr,
        {{"Aspect ratio",
          kit::fmtValue(s.meshAspectAvg, 3) + " avg \xc2\xb7 " + kit::fmtValue(s.meshAspectMax, 3) +
              " worst"},
         {"Min scaled Jacobian", kit::fmtValue(s.meshJacobianMin, 3),
          s.meshJacobianMin <= 0.0f ? kit::Tone::Err : kit::Tone::Neutral},
         {"Poor elements", kit::fmtQuantity(s.meshPctPoor, "%", 3), poorTone}});
    if (s.meshAspectHist.size() == 10) {
      static const char *const kBins[10] = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10+"};
      kit::BarSpec b;
      b.value.title = "Elements";
      b.value.integer = true;
      b.categoryTitle = "Aspect ratio (1 = ideal; above 10 is poor)";
      for (int i = 0; i < 10; ++i) {
        kit::PlotBar bar;
        bar.label = kBins[i];
        bar.value = s.meshAspectHist[i];
        bar.toned = i >= 9;
        bar.tone = kit::Tone::Warn;
        b.bars.push_back(bar);
      }
      kit::barPlot("aspect_hist", b);
    }
  }
}

// =================================================================================================
// fatigue setup
// =================================================================================================
void SimManager::drawFatigueForm(AppState &st, Study &s) {
  const UnitSys u = unitSys(st);
  const char *su = kit::unitText(u.stress);
  const int mati = materialIndexByKey(s.materialKey);
  double matSut = mati >= 0 ? materials()[mati].ultimate_mpa : 0.0;
  double matSe = mati >= 0 ? materials()[mati].endurance_mpa : 0.0;
  if (s.definition.contains("material")) {
    finiteNumber(s.definition["material"], "ultimate_mpa", matSut);
    finiteNumber(s.definition["material"], "endurance_mpa", matSe);
  }
  if (s.definition.contains("fatigue")) {
    finiteNumber(s.definition["fatigue"], "ultimate_mpa", matSut);
    finiteNumber(s.definition["fatigue"], "se_base_mpa", matSe);
  }

  // ---- cyclic loading: the stress cycle as factors of the applied-load stress ----
  (void)takeFocus("cyclic");
  if (kit::cardBegin("cyclic", "Cyclic loading", studyTypeIcon(StudyType::Fatigue))) {
    float lmin = 0.0f, lmax = 0.0f;
    {
      kit::FieldGroup g("cyc", "Load factor range");
      // Short items so the closed combo never truncates; the R value lives in each item's tip.
      static const char *const kLT[4] = {"Fully reversed", "Zero to max", "Custom R",
                                         "Custom range"};
      static const char *const kLTTips[4] = {
          "R = -1: the stress swings between minus and plus the peak (worst case).",
          "R = 0: the stress cycles between zero and the peak.",
          "Set the ratio R = min / max of the cycle.",
          "Set explicit minimum and maximum multiples of the applied load."};
      int lt = (s.fatigueLoadType < 0 || s.fatigueLoadType > 3) ? 0 : s.fatigueLoadType;
      if (kit::fieldEnum("type", "Cycle", &lt, kLT, 4,
                         kit::FieldOpts().withItemTips(kLTTips).withTip(
                             "How the applied load cycles. The stress from your loads is the "
                             "peak; this sets how it swings."))) {
        s.fatigueLoadType = lt;
        persist(st, s);
      }
      if (s.fatigueLoadType == 2) {
        if (kit::fieldSlider("R", "Load ratio R", &s.fatigueRatio, -1.0f, 0.95f,
                             kit::FieldOpts().withSigFigs(3).withTip(
                                 "R = min / max. -1 fully reversed, 0 zero-based, approaching "
                                 "1 nearly static.")))
          persist(st, s);
      } else if (s.fatigueLoadType == 3) {
        if (kit::fieldRange("lmm", "Load factor range", &s.fatigueLmin, &s.fatigueLmax,
                            kit::FieldOpts().withUnit("\xc3\x97").withTip(
                                "The cycle runs between these multiples of the applied load.")))
          persist(st, s);
      }
      // Derived cycle facts as read-only rows aligned with the fields above.
      fatigueLoadFactors(s, lmin, lmax);
      const double amp = (lmax - lmin) * 0.5, mean = (lmax + lmin) * 0.5;
      // "1 × load": the multiplier reads with its value (a lone "×" in the unit lane did not).
      kit::fieldReadout("amp", "Amplitude", (kit::fmtValue(amp, 3) + " \xc3\x97 load").c_str(),
                        kit::FieldOpts().withTip(
                            "Half the swing, as a multiple of the applied-load stress."));
      kit::fieldReadout("mean", "Mean", (kit::fmtValue(mean, 3) + " \xc3\x97 load").c_str(),
                        kit::FieldOpts().withTip(
                            "Mid-point of the cycle, as a multiple of the applied-load stress."));
      if (s.fatigueLoadType != 2)
        kit::fieldReadout("ratio", "Load ratio R",
                          kit::fmtValue(std::fabs(lmax) > 1e-6f ? lmin / lmax : -1.0, 3).c_str(),
                          kit::FieldOpts().withTip("R = min / max of the cycle."));
    }
    // Cycle sketch: 1.5 periods of the load factor about its mean (dashed), on real axes.
    const double amp = (lmax - lmin) * 0.5, mean = (lmax + lmin) * 0.5;
    constexpr int kN = 61;
    double xs[kN], ys[kN], ms[kN];
    for (int i = 0; i < kN; ++i) {
      xs[i] = 1.5 * i / (kN - 1);
      ys[i] = mean + amp * std::sin(xs[i] * 6.283185307179586);
      ms[i] = mean;
    }
    kit::PlotSpec p;
    p.x = kit::PlotData(xs, kN);
    kit::PlotSeries load;
    load.label = "Load factor";
    load.y = kit::PlotData(ys, kN);
    p.series.push_back(load);
    kit::PlotSeries meanLine;
    meanLine.label = "Mean";
    meanLine.y = kit::PlotData(ms, kN);
    meanLine.toned = true;
    meanLine.tone = kit::Tone::Warn;
    meanLine.dashed = true;
    p.series.push_back(meanLine);
    p.xAxis.title = "Cycles";
    p.xAxis.min = 0.0;
    p.xAxis.max = 1.5;
    p.yAxis.title = "Load factor";
    p.yAxis.unit = "\xc3\x97";
    p.height = ImGui::GetTextLineHeightWithSpacing() * 8.0f;
    kit::linePlot("cycle", p);
  }
  kit::cardEnd();

  // ---- strength: Sut + Se' (overrides; defaults from the material) — the ribbon's "S-N" target ----
  (void)takeFocus("strength");
  if (kit::cardBegin("strength", "Strength", "material")) {
    kit::FieldGroup g("str", "Ultimate strength Sut");
    bool sutSet = s.fatigueSut > 0.0f;
    bool seSet = s.fatigueSe > 0.0f;
    const std::string sutHint =
        matSut > 0.0 ? kit::fmtValue(matSut * u.stressFromMpa) + " (material)" : std::string();
    const std::string seHint =
        matSe > 0.0 ? kit::fmtValue(matSe * u.stressFromMpa) + " (material)" : "auto (0.5 Sut)";
    const bool sutMissing = !sutSet && matSut <= 0.0;
    if (kit::fieldNumber("sut", "Ultimate strength Sut", &s.fatigueSut,
                         kit::FieldOpts()
                             .withPresent(&sutSet)
                             .withHint(sutHint.c_str())
                             .withRequired(matSut <= 0.0)
                             .withUnit(su)
                             .withScale(u.stressFromMpa)
                             .withMin(0.0)
                             .withIssue(sutMissing ? "The material has no ultimate strength; "
                                                     "enter one."
                                                   : nullptr)
                             .withTip("Ultimate tensile strength. Leave empty to use the "
                                      "material's value."))) {
      if (!sutSet)
        s.fatigueSut = 0.0f;
      persist(st, s);
    }
    if (kit::fieldNumber("se", "Endurance limit Se'", &s.fatigueSe,
                         kit::FieldOpts()
                             .withPresent(&seSet)
                             .withHint(seHint.c_str())
                             .withUnit(su)
                             .withScale(u.stressFromMpa)
                             .withMin(0.0)
                             .withTip("Uncorrected (test-specimen) endurance limit; the Marin "
                                      "factors below correct it. Leave empty to use the "
                                      "material's value."))) {
      if (!seSet)
        s.fatigueSe = 0.0f;
      persist(st, s);
    }
  }
  kit::cardEnd();

  // ---- endurance correction (Marin factors) ----
  if (kit::cardBegin("marin", "Endurance correction", nullptr)) {
    kit::FieldGroup g("marin", "Design reliability");
    static const char *const kSurf[4] = {"Ground", "Machined / cold-drawn", "Hot-rolled",
                                         "As-forged"};
    static const char *const kCase[3] = {"Bending", "Axial", "Torsion"};
    int surf = (s.fatigueSurface < 0 || s.fatigueSurface > 3) ? 1 : s.fatigueSurface;
    if (kit::fieldEnum("surf", "Surface finish", &surf, kSurf, 4,
                       kit::FieldOpts().withTip("Surface factor ka."))) {
      s.fatigueSurface = surf;
      persist(st, s);
    }
    int lc = (s.fatigueLoadCase < 0 || s.fatigueLoadCase > 2) ? 0 : s.fatigueLoadCase;
    if (kit::fieldEnum("case", "Loading", &lc, kCase, 3,
                       kit::FieldOpts().withTip(
                           "Load factor kc and size factor kb (axial has no size effect)."))) {
      s.fatigueLoadCase = lc;
      persist(st, s);
    }
    if (kit::fieldNumber("size", "Section size", &s.fatigueSizeMm,
                         kit::FieldOpts()
                             .withUnit(kit::unitText(u.len))
                             .withScale(u.lenFromMm)
                             .withRange(1.0, 250.0)
                             .withTip("Effective diameter for the size factor kb (section "
                                      "thickness or diameter). The correlation holds for "
                                      "about 2-250 mm.")))
      persist(st, s);
    if (kit::fieldNumber("temp", "Temperature", &s.fatigueTempC,
                         kit::FieldOpts()
                             .withUnit(kit::Unit::DegC)
                             .withRange(-50.0, 500.0)
                             .withTip("Operating temperature for the temperature factor kd.")))
      persist(st, s);
    kit::fieldReadout("rel", "Design reliability",
                      kit::fmtQuantity(s.fatigueReliability, "%").c_str(),
                      kit::FieldOpts().withTip(
                          "Set on the Results page, where it re-bands life live without "
                          "re-solving."));
    const double Sut = s.fatigueSut > 0 ? s.fatigueSut : matSut;
    if (Sut > 0) {
      const double Seb = seBase(s, matSe, Sut);
      const double ka = marin_ka(Sut, s.fatigueSurface), kb = marin_kb(s.fatigueSizeMm, s.fatigueLoadCase),
                   kc = marin_kc(s.fatigueLoadCase), kd = marin_kd(s.fatigueTempC);
      const std::string factors = "ka " + kit::fmtValue(ka, 3) + " \xc2\xb7 kb " +
                                  kit::fmtValue(kb, 3) + " \xc2\xb7 kc " + kit::fmtValue(kc, 3) +
                                  " \xc2\xb7 kd " + kit::fmtValue(kd, 3);
      kit::fieldReadout("factors", "Marin factors", factors.c_str());
      kit::fieldReadout("sec", "Corrected Se",
                        kit::fmtValue(ka * kb * kc * kd * Seb * u.stressFromMpa).c_str(),
                        kit::FieldOpts().withUnit(su).withTip(
                            "Se = ka \xc2\xb7 kb \xc2\xb7 kc \xc2\xb7 kd \xc2\xb7 Se'. The "
                            "endurance limit used for life and factor of safety (reliability "
                            "is applied on top, in Results)."));
    }
  }
  kit::cardEnd();
}

// =================================================================================================
// fatigue results: confidence, per-theory table, life bands, Haigh diagram, S-N curve
// =================================================================================================
void SimManager::drawFatigueGraphs(AppState &st, Study &s) {
  if (s.resultSummary.empty()) {
    // The footer owns the run action and any blocker: the body only says what will appear.
    kit::emptyState(studyTypeIcon(StudyType::Fatigue), "No fatigue results yet",
                    "Run the study to see life and safety factors for every mean-stress "
                    "theory.");
    return;
  }
  const auto &sm = s.resultSummary;
  const UnitSys u = unitSys(st);
  const char *su = kit::unitText(u.stress);
  const double k = u.stressFromMpa;
  const double SeC = sm.value("se_corrected_mpa", 0.0); // Marin-corrected (no reliability)
  const double Sut = sm.value("sut_mpa", 0.0), Sy = sm.value("sy_mpa", 0.0);
  const double sa = sm.value("crit_sigma_a", 0.0), smean = sm.value("crit_sigma_m", 0.0);
  if (Sut <= 0 || SeC <= 0 || !sm.contains("theories")) {
    kit::blockedState("No fatigue strength data",
                      "The result carries no ultimate strength or endurance limit.",
                      "Set Sut / Se' in the study's Cycle step, then re-run.");
    return;
  }
  auto saEq = [&](int t) -> double {
    const char *key = kTheoryKeys[t];
    return sm["theories"].contains(key) ? sm["theories"][key].value("sa_eq_mpa", 0.0) : 0.0;
  };
  int m = (s.fatigueMethod < 0 || s.fatigueMethod > 4) ? 0 : s.fatigueMethod;

  // ---- confidence: re-bands life live with no re-solve ----
  if (kit::cardBegin("confidence", "Confidence", nullptr)) {
    kit::FieldGroup g("conf", "Design reliability");
    if (kit::fieldSlider("rel", "Design reliability", &s.fatigueReliability, 50.0f, 99.99f,
                         kit::FieldOpts()
                             .withUnit(kit::Unit::Percent)
                             .withSigFigs(4)
                             .withTip("Higher reliability uses a lower allowable endurance "
                                      "(ke): more conservative life. The bands span 50 % "
                                      "(optimistic) to 99.9 % (conservative).")))
      persist(st, s);
    if (kit::fieldEnum("theory", "Headline theory", &m, kTheoryNames, 5,
                       kit::FieldOpts().withTip(
                           "The mean-stress theory that colors the model, headlines the "
                           "result and is emphasised in the charts below."))) {
      s.fatigueMethod = m;
      persist(st, s);
    }
  }
  kit::cardEnd();
  const double SeD = reliability_ke(s.fatigueReliability) * SeC;
  const double SeLo = reliability_ke(99.9) * SeC;  // conservative endurance
  const double SeOpt = reliability_ke(50.0) * SeC; // optimistic endurance

  {
    const std::string rel = kit::fmtQuantity(s.fatigueReliability, "%");
    kit::keyValueCard("endurance", "Endurance", nullptr,
                      {{"Corrected Se", kit::fmtQuantity(SeC * k, su)},
                       {"Se at " + rel, kit::fmtQuantity(SeD * k, su)}});
  }

  // ---- per-theory table ----
  {
    const std::string saHdr = std::string("Sa,eq (") + su + ")";
    if (kit::tableBegin("ftheory", {{"Theory"},
                                    {saHdr.c_str(), kit::TableAlign::Right},
                                    {"FoS", kit::TableAlign::Right},
                                    {"Life", kit::TableAlign::Right}})) {
      for (int t = 0; t < 5; ++t) {
        const double se = saEq(t);
        const double fos = se > 1e-9 ? SeD / se : 15.0;
        const double life = sn_life(se, SeD, Sut);
        kit::tableRow();
        kit::tableCell(kTheoryNames[t], t == m ? kit::Tone::Accent : kit::Tone::Neutral,
                       t == m ? "Headline theory" : nullptr);
        kit::tableCell(kit::fmtValue(se * k, 3).c_str());
        kit::tableCell(kit::fmtValue(fos, 3).c_str(), fos < 1.0 ? kit::Tone::Err : kit::Tone::Neutral);
        kit::tableCell(life >= 1e6 ? "inf" : kit::fmtValue(life, 3).c_str(),
                       life >= 1e6 ? kit::Tone::Ok : kit::Tone::Neutral,
                       life >= 1e6 ? "Runout: below the endurance limit." : nullptr);
      }
    }
    kit::tableEnd();
  }

  // ---- life band per theory (99.9 % .. 50 %, dot = design reliability) ----
  kit::sectionHeader("Life estimate");
  {
    kit::BarSpec b;
    b.horizontal = true;
    b.value.title = "Life";
    b.value.unit = "cycles";
    b.value.log = true;
    b.value.min = 1.0;
    b.value.max = 1e6;
    for (int t = 0; t < 5; ++t) {
      const double se = saEq(t);
      const double lHi = std::min(sn_life(se, SeOpt, Sut), 1e6);
      const double lMed = std::min(sn_life(se, SeD, Sut), 1e6);
      const double lLo = std::min(sn_life(se, SeLo, Sut), 1e6);
      kit::PlotBar bar;
      bar.label = kTheoryNames[t];
      bar.value = std::max(lHi, 1.0);
      bar.lo = std::max(lLo, 1.0);
      bar.mark = std::max(lMed, 1.0);
      bar.note = lHi >= 1e6 ? "inf" : nullptr;
      bar.ramp = t;
      bar.dim = t != m;
      b.bars.push_back(bar);
    }
    kit::barPlot("life_bands", b);
  }
  kit::dimText("Bar = 99.9 % to 50 % reliability; dot = design reliability.");

  // ---- constant-life (Haigh) diagram, every theory ----
  kit::sectionHeader("Constant-life (Haigh) diagram");
  {
    double xMax = std::max(Sut, smean * 1.1);
    if (xMax <= 0)
      xMax = 1.0;
    const double yMax = std::max(SeD, sa) * 1.2 > 0 ? std::max(SeD, sa) * 1.2 : SeD;
    const double SyL = Sy > 0 ? Sy : Sut;
    constexpr int kN = 41;
    double hx[kN], hy[5][kN];
    for (int i = 0; i < kN; ++i) {
      const double smv = xMax * i / (kN - 1);
      hx[i] = smv * k;
      for (int t = 0; t < 5; ++t)
        hy[t][i] = SeD * std::max(haighFrac(t, smv, Sut, SyL), 0.0) * k;
    }
    const double lx[2] = {0.0, Sy * k}, ly[2] = {Sy * k, 0.0};
    kit::PlotSpec p;
    p.x = kit::PlotData(hx, kN);
    for (int t = 0; t < 5; ++t) {
      kit::PlotSeries sr;
      sr.label = kTheoryNames[t];
      sr.y = kit::PlotData(hy[t], kN);
      sr.ramp = t;
      sr.dim = t != m;
      p.series.push_back(sr);
    }
    if (Sy > 0) {
      kit::PlotSeries langer;
      langer.label = "Langer (yield)";
      langer.x = kit::PlotData(lx, 2);
      langer.y = kit::PlotData(ly, 2);
      langer.toned = true;
      langer.tone = kit::Tone::Warn;
      langer.dashed = true;
      p.series.push_back(langer);
    }
    p.bands.push_back(kit::PlotBand::between(kit::PlotData(hx, kN), kit::PlotData(),
                                             kit::PlotData(hy[m], kN), "Safe region"));
    p.markers.push_back({smean * k, sa * k, "Critical point", kit::Tone::Err});
    p.xAxis.title = "Mean stress Sm";
    p.xAxis.unit = su;
    p.xAxis.min = 0.0;
    p.xAxis.max = xMax * k;
    p.yAxis.title = "Alternating stress Sa";
    p.yAxis.unit = su;
    p.yAxis.min = 0.0;
    p.yAxis.max = yMax * k;
    kit::linePlot("haigh", p);
  }

  // ---- S-N curve with the reliability band ----
  kit::sectionHeader("S-N curve");
  const double seM = saEq(m), lifeM = sn_life(seM, SeD, Sut);
  {
    const double s1k = 0.9 * Sut, yMax = s1k * 1.1;
    const double logM = std::log10(std::min(std::max(lifeM, 1.0), 1e7));
    const double xLo = logM < 3.0 ? 10.0 : 1e3; // keep a low-cycle operating point on-graph
    const double nx[3] = {1e3, 1e6, 1e7};
    const double design[3] = {s1k * k, SeD * k, SeD * k};
    const double bandLo[3] = {s1k * k, SeLo * k, SeLo * k};
    const double bandHi[3] = {s1k * k, SeOpt * k, SeOpt * k};
    kit::PlotSpec p;
    p.x = kit::PlotData(nx, 3);
    kit::PlotSeries sr;
    sr.label = kTheoryNames[m];
    sr.y = kit::PlotData(design, 3);
    sr.ramp = m;
    p.series.push_back(sr);
    kit::PlotBand band = kit::PlotBand::between(kit::PlotData(nx, 3), kit::PlotData(bandLo, 3),
                                                kit::PlotData(bandHi, 3), "50-99.9 % reliability");
    band.ramp = m;
    p.bands.push_back(band);
    p.refs.push_back(kit::PlotRef::h(SeD * k, "Se", kit::Tone::Ok));
    p.markers.push_back({std::min(std::max(lifeM, xLo), 1e7), std::min(seM, yMax) * k,
                         "Operating point", kit::Tone::Err});
    p.xAxis.title = "Life";
    p.xAxis.unit = "cycles";
    p.xAxis.log = true;
    p.xAxis.min = xLo;
    p.xAxis.max = 1e7;
    p.yAxis.title = "Stress amplitude";
    p.yAxis.unit = su;
    p.yAxis.min = 0.0;
    p.yAxis.max = yMax * k;
    kit::linePlot("sn", p);
  }
  {
    const std::string key = std::string(kTheoryNames[m]) + " life at " +
                            kit::fmtQuantity(s.fatigueReliability, "%");
    kit::keyValueRowId("life_headline", key.c_str(),
                       lifeM >= 1e6 ? "Infinite (runout)"
                                    : (kit::fmtValue(lifeM, 3) + " cycles").c_str(),
                       lifeM >= 1e6 ? kit::Tone::Ok : kit::Tone::Neutral);
  }
}

// =================================================================================================
// Study-node body per study type
// =================================================================================================
void SimManager::drawStudyOptions(AppState &st, Study &s) {
  const UnitSys u = unitSys(st);
  switch (s.type) {
  case StudyType::Topology:
    drawTopologyForm(st, s);
    return;
  case StudyType::Motion:
    drawMotionForm(st, s);
    return;
  case StudyType::Flow:
  case StudyType::Thermal:
    drawFlowForm(st, s);
    return;
  case StudyType::Fatigue:
    drawFatigueForm(st, s);
    return;
  case StudyType::Modal:
    (void)takeFocus("modes");
    if (kit::cardBegin("modes", "Modes", studyTypeIcon(s.type))) {
      kit::FieldGroup g("modes", "Modes to solve");
      if (kit::fieldInt("n", "Modes to solve", &s.nModes,
                        kit::FieldOpts().withRange(1, 12).withTip(
                            "How many natural frequencies and mode shapes to compute.")))
        persist(st, s);
    }
    kit::cardEnd();
    return;
  case StudyType::Buckling:
    (void)takeFocus("modes");
    if (kit::cardBegin("modes", "Buckling modes", studyTypeIcon(s.type))) {
      kit::FieldGroup g("modes", "Modes to solve");
      if (kit::fieldInt("n", "Modes to solve", &s.nModes,
                        kit::FieldOpts().withRange(1, 8).withTip(
                            "How many buckling load factors to compute. Each scales the "
                            "applied load pattern; a factor below 1 means instability below "
                            "the applied load.")))
        persist(st, s);
    }
    kit::cardEnd();
    return;
  case StudyType::ThermalFea:
    (void)takeFocus("conduction");
    if (kit::cardBegin("conduction", "Conduction", studyTypeIcon(s.type))) {
      kit::FieldGroup g("cond", "Thermal conductivity k");
      if (kit::fieldNumber("k", "Thermal conductivity k", &s.thermK,
                           kit::FieldOpts()
                               .withRequired()
                               .withUnit(kit::Unit::W_mK)
                               .withMin(0.0)
                               .withIssue(s.thermK > 0.0f ? nullptr : "Must be greater than zero.")
                               .withTip("Conductivity of the solid, entered here: the material "
                                        "library carries no thermal conductivity. ~200 "
                                        "aluminum, ~50 steel, ~400 copper, ~0.3 FR4 / "
                                        "plastics.")))
        persist(st, s);
      // The Material step carries no conductivity: say so at the field, not only in its tooltip.
      kit::dimText("Entered here: the material library has no conductivity.");
      if (kit::fieldCheckbox("transient", "Transient (time-domain)", &s.thermTransient,
                             kit::FieldOpts().withTip(
                                 "Off: steady state. On: integrate C dT/dt + K T = f(t) over "
                                 "the duration below (backward Euler).")))
        persist(st, s);
      if (s.thermTransient) {
        bool dtSet = s.thermDtS > 0.0f;
        if (kit::fieldNumber("tend", "Duration", &s.thermTEndS,
                             kit::FieldOpts().withUnit(kit::Unit::s).withMin(0.0).withIssue(
                                 s.thermTEndS > 0.0f ? nullptr : "Must be greater than zero.")))
          persist(st, s);
        if (kit::fieldNumber("dt", "Time step", &s.thermDtS,
                             kit::FieldOpts()
                                 .withPresent(&dtSet)
                                 .withHint("auto")
                                 .withUnit(kit::Unit::s)
                                 .withMin(0.0)
                                 .withTip("Leave empty for the solver's default step. The "
                                          "native solver shortens the final step to the duration."))) {
          if (!dtSet)
            s.thermDtS = 0.0f;
          persist(st, s);
        }
        if (kit::fieldNumber("tinit", "Initial temperature", &s.thermTInitC,
                             kit::FieldOpts().withUnit(kit::Unit::DegC)))
          persist(st, s);
        if (kit::fieldNumber("cp", "Specific heat", &s.thermCp,
                             kit::FieldOpts()
                                 .withUnit(kit::Unit::J_kgK)
                                 .withMin(0.0)
                                 .withIssue(s.thermCp > 0.0f ? nullptr
                                                             : "Must be greater than zero.")
                                 .withTip("~900 aluminum, ~490 steel.")))
          persist(st, s);
      }
    }
    kit::cardEnd();
    return;
  case StudyType::Contact: {
    // A missing CalculiX is the footer's blocker: not repeated here.
    (void)takeFocus("contact");
    if (kit::cardBegin("contact", "Applied load", studyTypeIcon(s.type))) {
      kit::FieldGroup g("ct", "Pressure");
      if (kit::fieldNumber("p", "Pressure", &s.contactPressureMpa,
                           kit::FieldOpts()
                               .withRequired()
                               .withUnit(kit::unitText(u.press))
                               .withScale(u.pressFromMpa)
                               .withMin(0.0)
                               .withIssue(s.contactPressureMpa > 0.0f
                                              ? nullptr
                                              : "Must be greater than zero.")
                               .withTip("Downward pressure on the upper block's top face.")))
        persist(st, s);
      kit::inlineStatus("Runs on a two-block proxy sized to the part, not on your faces.",
                        kit::Tone::Info);
    }
    kit::cardEnd();
    return;
  }
  case StudyType::FreqResponse:
    (void)takeFocus("excitation");
    if (kit::cardBegin("excitation", "Excitation", studyTypeIcon(s.type))) {
      kit::FieldGroup g("fr", "Damping ratio");
      static const char *const kAxes[3] = {"X", "Y", "Z"};
      int ax = (s.frDir < 0 || s.frDir > 2) ? 2 : s.frDir;
      if (kit::fieldEnum("axis", "Axis", &ax, kAxes, 3,
                         kit::FieldOpts().withTip("Base-excitation direction (the shaker "
                                                  "axis)."))) {
        s.frDir = ax;
        persist(st, s);
      }
      if (kit::fieldNumber("g", "Base input", &s.frBaseG,
                           kit::FieldOpts()
                               .withUnit(kit::Unit::gAccel)
                               .withMin(0.0)
                               .withIssue(s.frBaseG > 0.0f ? nullptr : "Must be greater than zero.")
                               .withTip("Harmonic base acceleration amplitude.")))
        persist(st, s);
      if (kit::fieldNumber("zeta", "Damping ratio", &s.frZeta,
                           kit::FieldOpts()
                               .withRange(0.0, 0.99)
                               .withIssue(s.frZeta > 0.0f ? nullptr
                                                          : "Must be between 0 and 1.")
                               .withTip("Modal damping ratio (an assumption echoed on the "
                                        "result). 0.02 = Q of 25.")))
        persist(st, s);
      if (kit::fieldInt("modes", "Modes", &s.nModes,
                        kit::FieldOpts().withRange(1, 12).withTip(
                            "Modes in the superposition basis.")))
        persist(st, s);
    }
    kit::cardEnd();
    return;
  case StudyType::Static:
  default: {
    // Static has no study-level inputs. Readiness is said once — by the stepper chips and the
    // footer blocker with its "Go to" jump — so the Setup root only orients.
    bool ready = true;
    for (const auto &step : setupSteps(st, s))
      ready = ready && step.status != kStepAttention;
    if (ready)
      kit::emptyState(studyTypeIcon(s.type), "Ready to run",
                      "Material, fixtures, loads and mesh are set. Run the study below.");
    else
      kit::emptyState(studyTypeIcon(s.type), "Set up the study",
                      "Work through the steps above: material, fixtures, loads, then mesh.");
    return;
  }
  }
}

// =================================================================================================
// stepper "Run" page: live progress first, then live traces, then the last run
// =================================================================================================
void SimManager::drawRunPage(AppState &st, Study &s) {
  const SimRunUi ru = runUi();
  const bool mine = ru.running && ru.studyId == s.id;
  const bool hasResults = s.type == StudyType::Topology
                              ? (s.topo.contains("runs") && s.topo["runs"].is_array() &&
                                 !s.topo["runs"].empty())
                          : s.type == StudyType::Motion ? s.motionHasResult
                                                        : s.hasResult;

  // ---- live progress: the page's headline while this study runs ----
  if (mine) {
    if (kit::cardBegin("progress", "Progress", studyTypeIcon(s.type))) {
      ImGui::TextUnformatted(ru.stage.empty() ? "Solving\xe2\x80\xa6" : ru.stage.c_str());
      kit::progressBar(ru.progress, nullptr);
      const bool determinate = ru.progress >= 0.0f && ru.progress <= 1.0f;
      if (determinate)
        kit::keyValueRowId("pct", "Complete",
                           (std::to_string((int)std::lround(ru.progress * 100.0f)) + "%").c_str());
      if (ru.elapsed >= 0.0)
        kit::keyValueRowId("elapsed", "Elapsed", kit::fmtDuration(ru.elapsed).c_str());
      if (ru.cancelRequested)
        kit::inlineStatus("Cancelling: waiting for a solver checkpoint.", kit::Tone::Warn);
    }
    kit::cardEnd();
  }

  // ---- live traces ----
  if (s.type == StudyType::Topology)
    drawTopologyLiveStatus(st, s); // status banner + convergence sparklines
  if (!s.dragTrace.empty()) {
    kit::sectionHeader("Drag convergence");
    kit::PlotSpec p;
    p.mode = kit::PlotMode::Sparkline;
    kit::PlotSeries drag;
    drag.label = "Drag";
    drag.y = s.dragTrace;
    p.series.push_back(drag);
    p.caption = "Drag";
    kit::linePlot("drag", p);
    if (s.dragTraceStride > 1) {
      const std::string note = "Showing " + groupedCount((uint64_t)s.dragTrace.size()) + " of " +
                               groupedCount(s.dragTraceTotal) + " iterations (1 in " +
                               std::to_string(s.dragTraceStride) + ").";
      kit::dimText(note.c_str());
    }
  }
  if (mine)
    return;
  if (s.type == StudyType::Motion && hasResults) {
    drawMotionTimeline(st, s);
    kit::gapRow();
  }

  // ---- last run (human summary) or what the next run needs ----
  // A topology error is already the status banner above: say it once.
  const bool topoSaid =
      s.type == StudyType::Topology && s.topoStatusKind == 2 && !s.topoStatus.empty();
  if (!s.summary.empty() && !topoSaid) {
    if (kit::cardBegin("last_run", "Last run", nullptr)) {
      if (hasResults) {
        // A recorded topology run can stop short (iteration limit): its glyph says so.
        kit::statusGlyphInline(s.type == StudyType::Topology && s.topoStatusKind == 1
                                   ? kit::Status::Retry
                                   : kit::Status::Done);
        ImGui::SameLine(0.0f, ImGui::GetStyle().ItemSpacing.x);
        ImGui::TextUnformatted(s.summary.c_str());
        if (kit::secondaryButton("View results###view_results")) {
          node_ = SimNode::Results;
          armedWell_ = -1;
          runPage_ = RunPageView{};
        }
      } else {
        kit::inlineStatus(s.summary.c_str(), kit::Tone::Warn);
      }
    }
    kit::cardEnd();
  } else if (s.summary.empty() && s.dragTrace.empty() && !topoSaid) {
    // Nothing run yet: one state. The footer carries the blocker and its "Go to" jump; this
    // only names the steps still open.
    std::string open;
    for (const auto &step : setupSteps(st, s))
      if (step.status == kStepAttention)
        open += std::string(open.empty() ? "" : ", ") + (step.title ? step.title : step.label);
    if (open.empty())
      kit::emptyState(studyTypeIcon(s.type), "Ready to run",
                      "Setup is complete. Progress and live traces appear here once the run "
                      "starts.");
    else
      kit::emptyState(studyTypeIcon(s.type), "Finish setup first",
                      ("Still open: " + open + ".").c_str());
  }
}

// =================================================================================================
// the study pane scaffold
// =================================================================================================
void SimManager::drawForm(AppState &st, Study &s) {
  kit::panelBegin("sim_study", &simSurf());
  if (!drawPanelHeader(st, s)) {
    panelOpen_ = false;
    armedWell_ = -1;
    runPage_ = RunPageView{};
    kit::panelEnd();
    return;
  }

  const SimRunUi ru = runUi();
  const bool mineRunning = ru.running && ru.studyId == s.id;
  const bool hasResults = s.type == StudyType::Topology
                              ? (s.topo.contains("runs") && s.topo["runs"].is_array() &&
                                 !s.topo["runs"].empty())
                          : s.type == StudyType::Motion ? s.motionHasResult
                                                        : s.hasResult;
  const bool flow = isFlowFamily(s.type);
  const bool cfdUnavailable = thermalCfdUnavailable(st, s);

  // ---- Run page bookkeeping: a run of this study that starts anywhere (footer, ribbon, tree row)
  // opens the Run page once; it stays while the snapshot matches. A run that ends with results
  // moves on to Results, a failed one stays to show why.
  if (mineRunning && g_runSeenStudy != s.id && runPage_.studyId != s.id) {
    runPage_ = RunPageView{s.id, node_, true};
    armedWell_ = -1;
  }
  if (mineRunning)
    g_runSeenStudy = s.id;
  else if (g_runSeenStudy == s.id)
    g_runSeenStudy.clear();
  if (runPage_.studyId != s.id || runPage_.node != node_)
    runPage_ = RunPageView{};
  bool runPage = !runPage_.studyId.empty();
  if (runPage) {
    if (mineRunning) {
      runPage_.sawRunning = true;
    } else if (runPage_.sawRunning) {
      runPage_ = RunPageView{};
      runPage = false;
      if (hasResults) {
        node_ = SimNode::Results;
        armedWell_ = -1;
      }
    }
  }

  // ---- stepper: Setup (sub-chips per setup node, Setup step only) -> Run -> Results ----
  const std::vector<SetupStep> steps = setupSteps(st, s);
  const SetupStep *firstIssue = nullptr;
  for (const auto &step : steps)
    if (step.status == kStepAttention && !firstIssue)
      firstIssue = &step;
  kit::StepItem track[3];
  track[0].label = "Setup";
  // Unavailable is not a setup error (every chip can be green): show the step as waiting, not
  // failed; the Fluid page explains the missing solver and the footer offers Thermal FEA.
  track[0].status = firstIssue       ? kit::Status::Retry
                    : cfdUnavailable ? kit::Status::Pending
                                     : kit::Status::Done;
  track[0].tooltip = firstIssue       ? firstIssue->issue.c_str()
                     : cfdUnavailable ? kThermalCfdUnavailable
                                      : "Setup is complete.";
  track[1].label = "Run";
  track[1].status = mineRunning ? kit::Status::Running
                    : hasResults ? kit::Status::Done
                                 : kit::Status::Pending;
  track[1].tooltip = "Live progress and the last run.";
  track[2].label = "Results";
  track[2].status = hasResults ? kit::Status::Done : kit::Status::Pending;
  track[2].enabled = hasResults || node_ == SimNode::Results;
  track[2].tooltip = hasResults ? "Inspect the results." : "Run the study first.";
  int step = runPage ? 1 : node_ == SimNode::Results ? 2 : 0;
  // Sub-chips only on the Setup step, so Run and Results keep the body height.
  std::vector<kit::StepItem> subs;
  int sub = -1;
  if (step == 0 && steps.size() >= 2) {
    subs.reserve(steps.size());
    for (const auto &setupStep : steps) {
      kit::StepItem item;
      item.label = setupStep.label;
      item.status = stepStatus(setupStep.status);
      // Needs attention: the issue. Otherwise the full name when the chip label is short.
      item.tooltip = !setupStep.issue.empty() ? setupStep.issue.c_str()
                     : setupStep.title && std::strcmp(setupStep.title, setupStep.label) != 0
                         ? setupStep.title
                         : nullptr;
      subs.push_back(item);
    }
    // A flow study's Study node shows the Fluid page (drawFlowForm), so its chip lights.
    const SimNode shown = flow && node_ == SimNode::Study ? SimNode::Material : node_;
    for (int i = 0; i < (int)steps.size(); ++i)
      if (steps[i].node == shown) {
        sub = i;
        break;
      }
  }
  if (kit::stepper("steps", track, 3, &step, subs.empty() ? nullptr : subs.data(),
                   (int)subs.size(), &sub, 0)) {
    if (step == 0) {
      runPage_ = RunPageView{};
      if (sub >= 0 && sub < (int)steps.size()) {
        goToSetupStep(st, s, steps[sub], node_, loadIdx_, armedWell_);
        requestFocus(setupFocus(node_, s.type));
        if (mineRunning)
          armedWell_ = -1;
      } else {
        node_ = SimNode::Study;
        armedWell_ = -1;
      }
    } else if (step == 1) {
      runPage_ = RunPageView{s.id, node_, mineRunning};
      armedWell_ = -1;
    } else {
      runPage_ = RunPageView{};
      node_ = SimNode::Results;
      armedWell_ = -1;
    }
    runPage = !runPage_.studyId.empty();
  }

  // ---- scroll body ----
  const std::size_t studyCount = studies_.size();
  // Scroll position per study AND node: a new study must open at the top of its own setup, not
  // inherit the previous study's scroll (which could land straight in Advanced parameters).
  const std::string bodyPage = std::string(runPage ? "run" : nodePage(node_)) + "/" + s.id;
  kit::panelBodyBegin(bodyPage.c_str());
  ImGui::PushTextWrapPos(0.0f);
  const bool setupBody = !runPage && node_ != SimNode::Results;
  ImGui::BeginDisabled(setupBody && mineRunning);
  // Geometry tools apply only to studies that solve face boundary conditions. Contact stays a
  // proxy editor; flow, motion and topology own their family-specific interaction.
  if (setupBody && !flow && s.type != StudyType::Contact &&
      s.type != StudyType::Motion && s.type != StudyType::Topology) {
    if (kit::cardBegin("setup_tools", "Setup tools", "fixture")) {
      struct Tool {
        const char *label, *token, *command;
      };
      const bool thermal = s.type == StudyType::ThermalFea;
      const bool loads = thermal || s.type == StudyType::Static ||
                         s.type == StudyType::Fatigue || s.type == StudyType::Buckling;
      const Tool tools[] = {
          {thermal ? "Fixed temperature###temperature" : "Fixture###fixture",
           thermal ? "temperature" : "fixture", "sim_fixture"},
          {thermal ? "Heat flux###flux" : "Force###force",
           thermal ? "flux" : "force", thermal ? "sim_load_heat" : "sim_load_force"},
          {thermal ? "Convection###convection" : "Pressure###pressure",
           thermal ? "convection" : "pressure", thermal ? nullptr : "sim_load_pressure"}};
      int clickedTool = -1;
      kit::chipFlowBegin("tools");
      for (int i = 0; i < (loads ? 3 : 1); ++i) {
        const Tool &tool = tools[i];
        const char *hidden = std::strstr(tool.label, "###");
        const float textW = ImGui::CalcTextSize(tool.label, hidden).x;
        kit::chipFlowItem(textW + ImGui::GetStyle().FramePadding.x * 2.0f);
        if (kit::secondaryButton(tool.label))
          clickedTool = i;
        if (!mineRunning && ImGui::BeginDragDropSource()) {
          sim::scopeSetupToolDrag(st.docMirror.docId());
          ImGui::SetDragDropPayload(sim::kSetupToolDragPayload, tool.token,
                                    std::strlen(tool.token) + 1);
          ImGui::TextUnformatted(tool.label, hidden);
          ImGui::EndDragDropSource();
        }
        kit::itemTooltip("Click to open its editor, or drag onto a model face to apply it.");
      }
      kit::chipFlowEnd();
      if (!mineRunning && clickedTool >= 0) {
        const Tool &tool = tools[clickedTool];
        if (tool.command) {
          handleCommand(st, tool.command);
        } else {
          SimLoad load;
          load.thermalKind = 1;
          captureSelectionInto(st, load.faces);
          s.loads.push_back(std::move(load));
          persist(st, s);
          st.selection.clear();
          node_ = SimNode::Load;
          loadIdx_ = (int)s.loads.size() - 1;
          armedWell_ = (int)s.loads.size();
          requestFocus("load");
        }
      }
    }
    kit::cardEnd();
  }
  if (runPage) {
    drawRunPage(st, s);
  } else if (flow && (node_ == SimNode::Material || node_ == SimNode::Boundaries ||
                      node_ == SimNode::Mesh)) {
    drawFlowForm(st, s); // Fluid / Boundaries / Resolution page, chosen from node_
  } else {
    switch (node_) {
    case SimNode::Results:
      drawResultControls(st, s);
      break;
    case SimNode::Material:
      if (s.type == StudyType::Topology) {
        drawTopologyForm(st, s);
        break;
      }
      drawMaterialCombo(st, s);
      if (s.type == StudyType::Static && kit::cardBegin("criterion", "Failure criterion", nullptr)) {
        static const char *const kCrit[3] = {"von Mises", "Tresca", "Rankine"};
        static const char *const kCritTips[3] = {
            "Maximum distortion energy: ductile metals.",
            "Maximum shear stress: ductile metals, more conservative.",
            "Maximum normal stress: brittle materials."};
        kit::FieldGroup g("crit", "Failure criterion");
        int ci = (int)s.criterion;
        if (ci < 0 || ci > 2)
          ci = 0;
        if (kit::fieldEnum("crit", "Theory", &ci, kCrit, 3,
                           kit::FieldOpts().withItemTips(kCritTips).withTip(
                               "Theory behind the headline factor of safety."))) {
          s.criterion = (FailureCriterion)ci;
          persist(st, s);
        }
      }
      if (s.type == StudyType::Static)
        kit::cardEnd();
      break;
    case SimNode::Fixtures:
      drawFixturesForm(st, s);
      if (s.type == StudyType::Static || s.type == StudyType::Buckling ||
          s.type == StudyType::Fatigue || s.type == StudyType::ThermalFea)
        drawLoadsForm(st, s);
      break;
    case SimNode::Load:
      drawFixturesForm(st, s);
      drawLoadsForm(st, s);
      break;
    case SimNode::Mesh:
      drawMeshForm(st, s);
      break;
    case SimNode::Boundaries:
    case SimNode::Regions:
      if (s.type == StudyType::Topology) {
        drawTopologyForm(st, s);
        break;
      }
      [[fallthrough]];
    case SimNode::Study:
    default:
      if (kit::cardBegin("about", "About this study", studyTypeIcon(s.type), &g_aboutOpen))
        kit::dimText(studyAbout(s.type));
      kit::cardEnd();
      drawStudyOptions(st, s);
      break;
    }
  }
  if (setupBody && studies_.size() == studyCount) {
    // Reuse the stepper/footer readiness model; these rows only navigate to the existing editors.
    if (node_ == SimNode::Study && steps.size() > 1) {
      if (kit::cardBegin("setup_checklist", "Setup checklist", studyTypeIcon(s.type))) {
        for (int i = 0; i < (int)steps.size(); ++i) {
          const SetupStep &setupStep = steps[i];
          ImGui::PushID(i);
          kit::RowStyle row;
          // The row's second column is narrow: optional steps say just "Optional" and every
          // explanation is in full in the tooltip, so nothing is only readable truncated.
          const bool optional = setupStep.status == kStepOptional;
          row.secondary = optional                   ? "Optional"
                          : !setupStep.issue.empty() ? setupStep.issue.c_str()
                                                     : "Ready";
          const std::string tip = setupStep.issue.empty()
                                      ? std::string("Open this setup editor.")
                                      : setupStep.issue + " Click to open this setup editor.";
          row.tooltip = tip.c_str();
          if (kit::row("edit_step", setupStep.title ? setupStep.title : setupStep.label, row)) {
            goToSetupStep(st, s, setupStep, node_, loadIdx_, armedWell_);
            requestFocus(setupFocus(node_, s.type));
          }
          ImGui::PopID();
        }
      }
      kit::cardEnd();
    }
    drawParameterEditor(st, s);
  }
  ImGui::EndDisabled();
  ImGui::PopTextWrapPos();
  kit::panelBodyEnd();
  if (studies_.size() != studyCount) {
    // A form action created a study (Thermal CFD -> Thermal FEA) and reallocated studies_: `s`
    // is stale. The new study's pane draws from the next frame.
    kit::panelEnd();
    return;
  }

  // ---- pinned action footer ----
  kit::panelFooterBegin();
  if (s.type == StudyType::Topology) {
    drawTopologyRunArea(st, s);
  } else if (cfdUnavailable && !mineRunning) {
    // Thermal CFD can never run here: the footer's one action is the Thermal FEA route.
    kit::ActionSpec a;
    a.primaryLabel = "Switch to Thermal FEA";
    if (kit::actionFooter("run", a) == kit::FooterAction::Primary) {
      armedWell_ = -1;
      runPage_ = RunPageView{};
      newStudy(st, StudyType::ThermalFea); // reallocates studies_: `s` is stale from here
    }
  } else {
    const std::string blocker = runBlocker(s);
    const bool onResults = node_ == SimNode::Results && !runPage;
    // "Re-run" only once there is something to re-run; an empty Results step offers the run.
    const bool rerun = onResults && hasResults;
    const std::string runLabel = std::string("Run ") + studyTypeLabel(s.type);
    kit::ActionSpec a;
    a.primaryLabel = rerun ? "Re-run" : runLabel.c_str();
    a.primaryAsSecondary = rerun;
    a.primaryEnabled = blocker.empty();
    a.blocker = blocker.empty() ? nullptr : blocker.c_str();
    // A setup blocker raised away from its page gets a jump to the step that fixes it.
    const SimNode shownNode = flow && node_ == SimNode::Study ? SimNode::Material : node_;
    const bool offerFix = !blocker.empty() && firstIssue && !mineRunning &&
                          (runPage || firstIssue->node != shownNode);
    const std::string fixLabel = offerFix ? std::string("Go to ") + firstIssue->label : "";
    if (offerFix)
      a.secondaryLabel = fixLabel.c_str();
    a.running = mineRunning;
    a.progress = ru.progress;
    a.stage = ru.stage.empty() ? nullptr : ru.stage.c_str();
    a.elapsed = ru.elapsed;
    a.cancellable = ru.cancellable;
    a.cancelRequested = ru.cancelRequested;
    a.cancelBlocker = ru.cancelBlocker.empty() ? nullptr : ru.cancelBlocker.c_str();
    switch (kit::actionFooter("run", a)) {
    case kit::FooterAction::Primary:
      armedWell_ = -1;
      run(st, s);
      runPage_ = RunPageView{s.id, node_, false};
      break;
    case kit::FooterAction::Secondary:
      if (offerFix) {
        runPage_ = RunPageView{};
        goToSetupStep(st, s, *firstIssue, node_, loadIdx_, armedWell_);
        requestFocus(setupFocus(node_, s.type));
      }
      break;
    case kit::FooterAction::Cancel:
      requestCancel(st);
      break;
    default:
      break;
    }
  }
  kit::panelEnd();
}

} // namespace ui
