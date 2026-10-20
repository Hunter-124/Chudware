// Pure-CPU tests for the topology study settings/results model
// (host/src/ui/sim_topology.cpp built with CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY).
// No ImGui, renderer, AppState or engine is linked. Pins the behaviours a human
// edit must not break: agent-authored keys survive GUI edits, the capability
// matrix decides what may run, and a result is never shown as more than it
// proves.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "ui/sim_topology.h"
#include "ui/sim_topology_bc.h"

namespace {

using nlohmann::json;
namespace topo = ui::topology;

[[noreturn]] void fail(const char *expression, const char *file, int line) {
  std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", file, line, expression);
  std::exit(1);
}

#define CHECK(expression)                                                      \
  ((expression) ? static_cast<void>(0) : fail(#expression, __FILE__, __LINE__))

bool hasIssue(const std::vector<topo::Issue> &issues, const std::string &key) {
  for (const topo::Issue &i : issues)
    if (i.key == key && i.severity == topo::Severity::Blocking)
      return true;
  return false;
}

json capsJson() {
  const json structuralParams = json::array(
      {"volume_fraction", "max_iterations", "change_tolerance", "move_limit",
       "filter_radius_mm", "projection_beta", "density_threshold",
       "min_density", "penalty", "symmetry_axes", "extrusion_axis",
       "min_member_mm", "build_direction", "overhang_angle_deg",
       "overhang_penalty", "no_enclosed_voids"});
  return {
      {"methods",
       json::array(
           {{{"id", "simp"}}, {{"id", "beso"}}, {{"id", "level_set"}}})},
      {"physics",
       json::array(
           {{{"id", "structural"}}, {{"id", "flow"}}, {{"id", "thermal"}}})},
      {"combinations",
       json::array({
           {{"method", "simp"},
            {"physics", "structural"},
            {"supported", true},
            {"objectives", json::array({"compliance", "mass"})},
            {"limits",
             json::array({"stress_limit_mpa", "max_displacement_mm"})},
            {"parameters", structuralParams}},
           {{"method", "beso"},
            {"physics", "structural"},
            {"supported", true},
            {"objectives", json::array({"compliance"})},
            {"parameters",
             json::array({"volume_fraction", "max_iterations",
                          "filter_radius_mm", "beso_evolution_rate"})}},
           {{"method", "simp"},
            {"physics", "flow"},
            {"supported", true},
            {"objectives", json::array({"dissipation"})},
            {"limits", json::array({"max_pressure_drop_pa"})},
            {"parameters", json::array({"volume_fraction", "max_iterations"})}},
           {{"method", "beso"}, {"physics", "flow"}, {"supported", false}},
           {{"method", "level_set"},
            {"physics", "structural"},
            {"supported", true},
            {"objectives", json::array()}},
           {{"method", "simp"},
            {"physics", "thermal"},
            {"supported", true},
            {"volume_phase",
             "fluid"}, // deliberately non-default: a reported phase wins
            {"objectives", json::array({"thermal_compliance"})},
            {"parameters", json::array({"volume_fraction"})}},
       })},
  };
}

void testCapabilityMatrix() {
  const topo::Capabilities caps = topo::parseCapabilities(capsJson());
  CHECK(caps.state == topo::Capabilities::State::Ready);
  CHECK(caps.supports("simp", "structural"));

  // Listed unsupported without a reason still explains itself; a supported pair
  // offering no objective could never run, so it is unsupported; an unlisted
  // pair is unsupported.
  std::string reason;
  CHECK(!caps.supports("beso", "flow", &reason));
  CHECK(!reason.empty());
  reason.clear();
  CHECK(!caps.supports("level_set", "structural", &reason));
  CHECK(reason.find("no objective") != std::string::npos);
  reason.clear();
  CHECK(!caps.supports("level_set", "flow", &reason));
  CHECK(!reason.empty());

  // volume_fraction budgets fluid for flow (contract rule) unless the solver
  // states the phase.
  CHECK(caps.find("simp", "flow")->volumePhase == "fluid");
  CHECK(caps.find("simp", "structural")->volumePhase == "solid");
  CHECK(caps.find("simp", "thermal")->volumePhase == "fluid");

  const topo::Capabilities bad =
      topo::parseCapabilities(json{{"methods", json::array()}});
  CHECK(bad.state == topo::Capabilities::State::Unavailable);
  CHECK(!bad.error.empty());
  CHECK(!bad.supports("simp", "structural"));

  const json fresh = topo::newTopologyConfig(caps, "flow");
  CHECK(fresh.value("method", "") == "simp");
  CHECK(fresh.value("objective", "") == "dissipation");
  CHECK(fresh.size() == 3); // never invents numeric settings
  CHECK(topo::newTopologyConfig(caps, "modal").empty());
}

void testSwitchPreservesUnownedKeys() {
  const topo::Capabilities caps = topo::parseCapabilities(capsJson());
  json t = {{"physics", "structural"},
            {"method", "simp"},
            {"objective", "mass"},
            {"volume_fraction", 0.3},
            {"penalty", 3.0},
            {"stress_limit_mpa", 120.0},
            {"agent_note", "keep the bolt bosses"},
            {"runs", json::array({{{"run_id", "r1"}}})}};

  // Unsupported pair: refused, topology untouched.
  const json before = t;
  const topo::SwitchResult refused =
      topo::selectCombination(t, caps, "beso", "flow");
  CHECK(!refused.applied);
  CHECK(!refused.refusal.empty());
  CHECK(t == before);

  // Supported pair: objective and keys the pair does not offer go; agent keys
  // stay verbatim.
  const topo::SwitchResult r =
      topo::selectCombination(t, caps, "beso", "structural");
  CHECK(r.applied);
  CHECK(t["method"] == "beso");
  CHECK(t["objective"] == "compliance");
  CHECK(r.objectiveBefore == "mass" && r.objectiveAfter == "compliance");
  CHECK(!t.contains("penalty"));
  CHECK(!t.contains("stress_limit_mpa"));
  CHECK(t["volume_fraction"] == 0.3);
  CHECK(t["agent_note"] == "keep the bolt bosses");
  CHECK(t["runs"] == before["runs"]);
  CHECK(r.removedKeys.size() == 2);
  CHECK(topo::validateTopology(t, caps).empty());
}

void testValidation() {
  const topo::Capabilities caps = topo::parseCapabilities(capsJson());
  json ok = {{"physics", "structural"},
             {"method", "simp"},
             {"objective", "compliance"},
             {"volume_fraction", 1.0},
             {"max_iterations", 200.0}, // integral float is a whole number
             {"symmetry_axes", json::array({"x", "z"})},
             {"build_direction", json::array({0, 0, 1})},
             {"overhang_angle_deg", 45},
             {"agent_custom", "unknown keys are not judged"}};
  CHECK(!topo::hasBlocking(topo::validateTopology(ok, caps)));

  json bad = ok;
  bad["volume_fraction"] = 0.0;
  bad["max_iterations"] = 2.5;
  bad["min_density"] = 1.0;
  bad["symmetry_axes"] = json::array({"x", "x"});
  bad.erase("build_direction");     // overhang without a build direction
  bad["beso_evolution_rate"] = 0.1; // not offered for SIMP + structural
  const std::vector<topo::Issue> issues = topo::validateTopology(bad, caps);
  CHECK(hasIssue(issues, "volume_fraction"));
  CHECK(hasIssue(issues, "max_iterations"));
  CHECK(hasIssue(issues, "min_density"));
  CHECK(hasIssue(issues, "symmetry_axes"));
  CHECK(hasIssue(issues, "build_direction"));
  CHECK(hasIssue(issues, "beso_evolution_rate"));
  CHECK(!hasIssue(issues, "agent_custom"));

  // Mass minimization without any response limit has nothing to bound it.
  json mass = {
      {"physics", "structural"}, {"method", "simp"}, {"objective", "mass"}};
  CHECK(hasIssue(topo::validateTopology(mass, caps), "objective"));
  mass["max_displacement_mm"] = 0.5;
  mass["volume_fraction"] = 0.35;
  CHECK(!topo::hasBlocking(topo::validateTopology(mass, caps)));

  json unsupported = {
      {"physics", "flow"}, {"method", "beso"}, {"objective", "dissipation"}};
  CHECK(hasIssue(topo::validateTopology(unsupported, caps), "method"));

  CHECK(topo::hasBlocking(topo::validateTopology(ok, topo::Capabilities{})));
  CHECK(topo::hasBlocking(topo::validateTopology(json(), caps)));
}

void testPrintSetup() {
  const auto caps = topo::parseCapabilities(capsJson());
  json study = {{"topology", topo::newTopologyConfig(caps, "structural")}};
  auto issues = topo::validateTopology(study["topology"], caps, study);
  CHECK(hasIssue(issues, "volume_fraction"));
  CHECK(issues.front().message == "Set the solid volume fraction (Goal).");
  const json fluid = topo::newTopologyConfig(caps, "flow");
  CHECK(topo::validateTopology(fluid, caps).front().message ==
        "Set the fluid volume fraction (Goal).");
  json chtMatrix = capsJson();
  chtMatrix["physics"].push_back({{"id", "cht"}});
  json chtPair = chtMatrix["combinations"][2];
  chtPair["physics"] = "cht";
  chtMatrix["combinations"].push_back(chtPair);
  const auto chtCaps = topo::parseCapabilities(chtMatrix);
  CHECK(topo::validateTopology(topo::newTopologyConfig(chtCaps, "cht"), chtCaps)
            .front().message == "Set the fluid volume fraction (Goal).");
  json noVolumeCaps = capsJson();
  noVolumeCaps["combinations"][0]["parameters"] = json::array();
  CHECK(!hasIssue(topo::validateTopology(study["topology"],
                         topo::parseCapabilities(noVolumeCaps)), "volume_fraction"));

  study["topology"]["volume_fraction"] = 0.35;
  study["topology"]["agent_note"] = "keep";
  study["topology"]["overhang_angle_deg"] = 30;
  study["topology"]["min_member_mm"] = 3.0;
  const json preset = {
      {"id", "powder"}, {"topology", {{"min_member_mm", 1.0},
          {"overhang_angle_deg", nullptr}, {"overhang_penalty", nullptr},
          {"no_enclosed_voids", true}, {"unoffered", 7}}}};
  CHECK(topo::applyPrintProcess(study, preset, caps));
  CHECK(study["print_process"] == "powder");
  CHECK(study["topology"]["min_member_mm"] == 1.0);
  CHECK(study["topology"]["no_enclosed_voids"] == true);
  CHECK(!study["topology"].contains("overhang_angle_deg"));
  CHECK(!study["topology"].contains("unoffered"));
  CHECK(study["topology"]["agent_note"] == "keep");
  json limited = {{"topology", {{"physics", "structural"}, {"method", "beso"},
                    {"objective", "compliance"}, {"overhang_angle_deg", 30}}}};
  CHECK(topo::applyPrintProcess(limited, preset, caps));
  CHECK(limited["print_process"] == "powder");
  CHECK(!limited["topology"].contains("min_member_mm"));
  CHECK(limited["topology"]["overhang_angle_deg"] == 30);

  study["topology"]["overhang_angle_deg"] = 45;
  study["topology"]["overhang_penalty"] = 1.0;
  CHECK(hasIssue(topo::validateTopology(study["topology"], caps, study), "build_direction"));
  study["build_plate"] = {{"faces", json::array()}};
  CHECK(hasIssue(topo::validateTopology(study["topology"], caps, study), "build_direction"));
  const json selector = {{"kind", "face"}, {"_pick", {{"body", 1}, {"topo", 6}}}};
  study["build_plate"] = {{"faces", json::array({selector})}, {"label", "Bottom"}};
  CHECK(!hasIssue(topo::validateTopology(study["topology"], caps, study), "build_direction"));
  study["topology"].erase("overhang_angle_deg"); // native default is 45 degrees
  CHECK(!topo::hasBlocking(topo::validateTopology(study["topology"], caps, study)));
  const auto selectors = topo::selectorsToResolve(study);
  CHECK(selectors.size() == 1);
  std::vector<std::uint64_t> preview;
  topo::collectFaceKeys(study, topo::WellRole::Preserve, preview);
  CHECK(preview.size() == 1 && preview[0] == ((std::uint64_t(1) << 32) | 6));
  const json faces = {{"points", {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}}},
                      {"tol_mm", 0.1}, {"face_normal", {0, 0, -1}}};
  std::map<std::string, json> resolved = {{topo::selectorKey(selector), faces}};
  json envelope;
  std::string error;
  CHECK(!topo::buildRunEnvelope(study, {}, "/part.step", {}, envelope, error));
  CHECK(topo::buildRunEnvelope(study, resolved, "/part.step", {}, envelope, error));
  CHECK(envelope["build_plate"]["faces"]["points"] == faces["points"]);
  CHECK(envelope["build_plate"]["label"] == "Bottom");
  const std::string plateKey = topo::setupKey(study);
  study.erase("build_plate");
  CHECK(topo::setupKey(study) != plateKey);
  CHECK(topo::buildRunEnvelope(study, {}, "/part.step", {}, envelope, error));
  CHECK(!envelope.contains("build_plate"));
}

void testDfamMergeAndPrintSurface() {
  json run = {{"run_id", "gui-run"}, {"result", {
      {"manufacturing", {{"geometry_valid", true},
          {"dfam", {{"checked", false}, {"passed", false}}}}},
      {"artifacts", {
          {"surface_stl", {{"path", "/raw.stl"}}},
          {"surface_print_stl", {{"path", "/print.stl"}}}}}}}};
  const json reply = {{"ok", true}, {"dfam", {{"checked", true}, {"passed", false},
      {"checks", json::array({{{"check", "overhang"}, {"status", "fail"},
                               {"note", "Needs supports"}}})}}},
      {"print_ready_reason", "Needs supports and slicing"}};
  CHECK(topo::mergeDfamResult(run, reply));
  const auto &mfg = run["result"]["manufacturing"];
  CHECK(mfg["dfam"] == reply["dfam"]);
  CHECK(mfg["geometry_valid"] == true && mfg["print_ready"] == false);
  CHECK(mfg["print_ready_reason"] == reply["print_ready_reason"]);
  const json retained = run;
  CHECK(!topo::mergeDfamResult(run, {{"ok", false}}, "offline"));
  CHECK(run == retained);
  CHECK(topo::surfaceArtifactKey(run["result"]) == "surface_print_stl");
  run["result"]["artifacts"]["surface_print_stl"]["stale"] = true;
  CHECK(topo::surfaceArtifactKey(run["result"]) == "surface_stl");
  run["result"]["artifacts"]["surface_print_stl"]["stale"] = false;
  run["result"]["artifacts"]["surface_print_stl"]["missing"] = true;
  CHECK(topo::surfaceArtifactKey(run["result"]) == "surface_stl");
  run["result"]["manufacturing"].erase("dfam");
  CHECK(topo::mergeDfamResult(run, {{"ok", false}, {"error", "worker unavailable"}}));
  const auto report = topo::manufacturingReport(run["result"], {});
  CHECK(!report.dfamChecked && !report.dfamPassed && !report.printReady);
  CHECK(report.printReadyReason == "DFAM check could not run: worker unavailable");
}

// Resolved facesets are canonical native inputs, whether a fixture is bare or
// an entry wraps selectors in `faces`. Editing other settings must not rewrite
// their samples, tolerances, normals or analytic descriptors.
json resolvedStudy(const json &faceset) {
  return {
      {"id", "topology-resolved"},
      {"type", "topology"},
      {"name", "Resolved study"},
      {"material", {{"E_mpa", 70000.0}, {"nu", 0.3}}},
      {"topology", {{"physics", "structural"}, {"method", "simp"},
                    {"objective", "compliance"}, {"volume_fraction", 0.35}}},
      {"fixtures", json::array({
                       faceset,
                       {{"faces", json::array({faceset})}, {"dofs", {0, 1, 2}}}})},
      {"load_cases", json::array({
                         {{"name", "Service"},
                          {"fixtures", json::array({
                                           faceset,
                                           {{"faces", json::array({faceset})}}})},
                          {"loads", json::array({
                                        {{"faces", json::array({faceset})},
                                         {"type", "force"},
                                         {"vec", {0, 0, -100}}}})}}})},
      {"runs", json::array({{{"run_id", "retained"}}})}};
}

const std::vector<json::json_pointer> &resolvedLocations() {
  static const std::vector<json::json_pointer> locations = {
      json::json_pointer("/fixtures/0"),
      json::json_pointer("/fixtures/1/faces/0"),
      json::json_pointer("/load_cases/0/fixtures/0"),
      json::json_pointer("/load_cases/0/fixtures/1/faces/0"),
      json::json_pointer("/load_cases/0/loads/0/faces/0")};
  return locations;
}

void testResolvedFacesetDefinitionEdits() {
  const topo::Capabilities caps = topo::parseCapabilities(capsJson());
  const json flat = {{"points", {0, 0, 0, 1, 0, 0, 0, 1, 0}}};
  std::vector<json> accepted = {
      flat,
      {{"points", json::array({{0, 0, 0}, {1, 0, 0}, {0, 1, 0}})}},
      {{"points", {0, 0, 0}}},
      // Native validation counts a nested sample as three scalar components.
      {{"points", json::array({json::array({0, 0, 0}), 1, 0, 0})}},
      {{"points", json::array({{0, 0, 0, 7}})}},
      {{"points", flat["points"]}, {"tol_mm", 1e-100},
       {"perp_tol_mm", 1e100}, {"face_normal", {0, 0, 2, 7}}}};
  const std::vector<json> surfaces = {
      {{"type", "sphere"}, {"origin", {0, 0, 0, 7}}, {"radius", 2}},
      {{"type", "cylinder"}, {"origin", {0, 0, 0}},
       {"axis", {0, 0, 2, 7}}, {"radius", 2}},
      {{"type", "cone"}, {"origin", {0, 0, 0}},
       {"axis", {0, 0, 2}}, {"semi_angle_rad", 1.5707962}},
      // Native accepts either radius ordering; it does not impose major > minor.
      {{"type", "torus"}, {"origin", {0, 0, 0}},
       {"axis", {0, 0, 2}}, {"major_radius", 1}, {"minor_radius", 2}}};
  for (const json &surface : surfaces) {
    json faceset = flat;
    faceset["surface"] = surface;
    accepted.push_back(std::move(faceset));
  }
  for (const json &faceset : accepted) {
    json study = resolvedStudy(flat);
    const json definition = resolvedStudy(faceset);
    std::string error;
    CHECK(topo::applyStudyDefinition(study, definition, caps, {}, error));
    CHECK(error.empty());
    CHECK(study == definition);

    json edited = study;
    edited["name"] = "Renamed resolved study";
    edited["topology"]["max_iterations"] = 75;
    CHECK(topo::applyStudyDefinition(study, edited, caps, {}, error));
    CHECK(error.empty());
    CHECK(study == edited);
    for (const auto &location : resolvedLocations())
      CHECK(study.at(location) == faceset);
  }
}

void testInvalidResolvedFacesetDefinitionsAreTransactional() {
  const topo::Capabilities caps = topo::parseCapabilities(capsJson());
  const json flat = {{"points", {0, 0, 0, 1, 0, 0, 0, 1, 0}}};
  const json original = resolvedStudy(flat);
  const double inf = std::numeric_limits<double>::infinity();
  const double nan = std::numeric_limits<double>::quiet_NaN();
  std::vector<json> rejected;
  for (const json &points : std::vector<json>{
           "invalid", nullptr, json::object(), json::array(),
           json::array({0, 0}), json::array({0, 0, 0, 1}),
           json::array({json::array({0, 0})}),
           json::array({json::array({0, 0, "invalid"})}),
           json::array({json::array({0, 0, 0}), 1}),
           json::array({0, 0, inf}), json::array({0, 0, nan}),
           json::array({json::array({0, inf, 0})}),
           json::array({json::array({nan, 0, 0})})})
    rejected.push_back(json{{"points", points}});
  auto rejectField = [&](const char *key, const json &value) {
    json faceset = flat;
    faceset[key] = value;
    rejected.push_back(std::move(faceset));
  };
  for (const char *key : {"tol_mm", "perp_tol_mm"})
    for (const json &value : std::vector<json>{nullptr, "invalid", 0, -1, inf, nan})
      rejectField(key, value);
  for (const json &normal : std::vector<json>{
           nullptr, "invalid", json::array({0, 0}), json::array({0, 0, 0}),
           json::array({0, 0, "invalid"}), json::array({0, inf, 0}),
           json::array({nan, 0, 0}), json::array({1e308, 0, 0}),
           json::array({1e-300, 0, 0})})
    rejectField("face_normal", normal);
  for (const json &surface : std::vector<json>{
           nullptr, "invalid", json::object(),
           {{"type", 1}, {"origin", {0, 0, 0}}},
           {{"type", "sphere"}, {"radius", 1}},
           {{"type", "plane"}, {"origin", {0, 0, 0}}},
           {{"type", "sphere"}, {"origin", {0, 0}}, {"radius", 1}},
           {{"type", "sphere"}, {"origin", {0, inf, 0}}, {"radius", 1}}})
    rejectField("surface", surface);
  const std::vector<json> surfaces = {
      {{"type", "sphere"}, {"origin", {0, 0, 0}}, {"radius", 1}},
      {{"type", "cylinder"}, {"origin", {0, 0, 0}},
       {"axis", {0, 0, 1}}, {"radius", 1}},
      {{"type", "cone"}, {"origin", {0, 0, 0}},
       {"axis", {0, 0, 1}}, {"semi_angle_rad", 0.5}},
      {{"type", "torus"}, {"origin", {0, 0, 0}},
       {"axis", {0, 0, 1}}, {"major_radius", 1}, {"minor_radius", 2}}};
  for (const json &surface : surfaces) {
    for (const char *key : {"radius", "semi_angle_rad", "major_radius",
                            "minor_radius"}) {
      if (!surface.contains(key))
        continue;
      for (const json &value : std::vector<json>{nullptr, "invalid", 0, -1, inf, nan}) {
        json invalid = surface;
        invalid[key] = value;
        rejectField("surface", invalid);
      }
      json missing = surface;
      missing.erase(key);
      rejectField("surface", missing);
    }
    if (surface.contains("axis")) {
      for (const json &axis : std::vector<json>{
               nullptr, json::array({0, 0}), json::array({0, 0, 0}),
               json::array({0, 0, inf}), json::array({nan, 0, 0}),
               json::array({1e308, 0, 0}), json::array({1e-300, 0, 0})}) {
        json invalid = surface;
        invalid["axis"] = axis;
        rejectField("surface", invalid);
      }
      json missing = surface;
      missing.erase("axis");
      rejectField("surface", missing);
    }
    if (surface["type"] == "cone") {
      json invalid = surface;
      invalid["semi_angle_rad"] = 1.5707963;
      rejectField("surface", invalid);
    }
  }
  for (const auto &location : resolvedLocations())
    for (const json &faceset : rejected) {
      json study = original;
      json definition = original;
      definition["name"] = "Invalid candidate";
      definition["topology"]["max_iterations"] = 75;
      definition[location] = faceset;
      std::string error;
      CHECK(!topo::applyStudyDefinition(study, definition, caps, {}, error));
      CHECK(!error.empty());
      CHECK(study == original);
    }
}

void testApplyRunSettings() {
  json study = {{"physics", "flow"},
                {"method", "simp"},
                {"objective", "dissipation"},
                {"max_pressure_drop_pa", 50.0},
                {"agent_note", "from the model"}};
  const json snapshot = {{"physics", "structural"},
                         {"method", "simp"},
                         {"objective", "compliance"},
                         {"volume_fraction", 0.4},
                         {"result_dir", "/elsewhere"}};
  topo::applyRunSettings(study, snapshot);
  CHECK(study["physics"] == "structural");
  CHECK(study["volume_fraction"] == 0.4);
  CHECK(!study.contains(
      "max_pressure_drop_pa")); // owned and absent from the snapshot
  CHECK(study["agent_note"] == "from the model"); // unowned: preserved
  CHECK(
      !study.contains("result_dir")); // unowned snapshot keys are not imported
}

void testOutcomeHonesty() {
  using topo::Outcome;
  const json base = {
      {"ok", true},
      {"feasible", true},
      {"converged", true},
      {"termination", "converged"},
      {"summary", {{"convergence_criterion", "max change < 0.01"}}}};
  topo::OutcomeSummary o = topo::classifyResult(base);
  CHECK(o.outcome == Outcome::Converged && o.converged);

  json limit = base;
  limit["termination"] = "max_iterations";
  o = topo::classifyResult(limit);
  CHECK(o.outcome == Outcome::Inconsistent && !o.converged);
  limit["converged"] = false;
  o = topo::classifyResult(limit);
  CHECK(o.outcome == Outcome::IterationLimit && !o.converged &&
        o.computationOk);

  json infeasible = base;
  infeasible["converged"] = false;
  infeasible["feasible"] = false;
  infeasible["constraints"] =
      json::array({{{"name", "stress"}, {"violation", 0.2}},
                   {{"name", "disp"}, {"violation", -0.1}}});
  o = topo::classifyResult(infeasible);
  CHECK(o.outcome == Outcome::Infeasible && !o.converged);
  CHECK(std::fabs(o.maxViolation - 0.2) < 1e-12);

  json noFeasibility = base;
  noFeasibility.erase("feasible");
  CHECK(topo::classifyResult(noFeasibility).outcome == Outcome::Inconsistent);

  const json failed = {
      {"ok", false}, {"converged", true}, {"error", "singular stiffness"}};
  o = topo::classifyResult(failed);
  CHECK(o.outcome == Outcome::Failed && !o.converged &&
        o.detail == "singular stiffness");

  json lineSearch = base;
  lineSearch["converged"] = false;
  lineSearch["termination"] = "line_search_failed";
  CHECK(topo::classifyResult(lineSearch).outcome == Outcome::NotConverged);

  CHECK(topo::classifyResult(json{{"ok", false}, {"termination", "cancelled"}})
            .outcome == Outcome::Cancelled);
}

void testManufacturingIsMeasuredNotRequested() {
  const json settings = {{"min_member_mm", 3.0},
                         {"overhang_angle_deg", 45.0},
                         {"no_enclosed_voids", true}};
  json result = {{"manufacturing",
                  {{"geometry_valid", true},
                   {"print_ready", true},
                   {"checks", json::array({{{"id", "overhang_angle_deg"},
                                            {"status", "pass"}}})}}}};
  topo::ManufacturingReport rep = topo::manufacturingReport(result, settings);
  CHECK(!rep.printReady); // DFAM never ran: the optimizer's own flag is not
                          // enough
  CHECK(rep.rows.size() == 3);
  for (const topo::ManufacturingRow &r : rep.rows) {
    CHECK(r.status ==
          topo::CheckStatus::NotMeasured); // pass without a measurement too
    CHECK(!r.requested.is_null());
  }

  result["manufacturing"]["dfam"] = {{"checked", true}, {"passed", true}};
  result["manufacturing"]["checks"] = json::array(
      {{{"id", "min_member_mm"}, {"measured", 2.1}, {"status", "fail"}}});
  rep = topo::manufacturingReport(result, settings);
  CHECK(!rep.printReady);
  bool sawMember = false;
  for (const topo::ManufacturingRow &r : rep.rows)
    if (r.key == "min_member_mm") {
      sawMember = true;
      CHECK(r.status == topo::CheckStatus::Fail);
      CHECK(r.requested == 3.0 && r.measured == 2.1);
    }
  CHECK(sawMember);

  result["manufacturing"]["checks"] = json::array(
      {{{"id", "min_member_mm"}, {"measured", 3.4}, {"status", "pass"}},
       {{"id", "overhang_angle_deg"}, {"measured", 41.0}, {"status", "pass"}},
       {{"id", "no_enclosed_voids"}, {"measured", true}, {"status", "pass"}}});
  rep = topo::manufacturingReport(result, settings);
  // Geometry + DFAM evidence is kept, but no optimizer record carries the
  // mandatory slice_part G-code validation: even an old/manual print_ready=true
  // record with every check passing must not be promoted to print-ready.
  CHECK(rep.geometryValidated && rep.dfamChecked && rep.dfamPassed);
  CHECK(!rep.printReady);
  CHECK(!rep.printReadyReason.empty());
  result["manufacturing"]["geometry_valid"] = false;
  CHECK(!topo::manufacturingReport(result, settings).printReady);
  CHECK(!topo::manufacturingReport(json::object(), settings).printReady);
}

void testRunComparison() {
  auto run = [](const char *physics, const char *objective, double obj,
                double vf) {
    return json{{"settings", {{"physics", physics}, {"objective", objective}}},
                {"result",
                 {{"physics", physics},
                  {"summary",
                   {{"objective", objective},
                    {"objective_final", obj},
                    {"volume_fraction_final", vf},
                    {"iterations", 80}}}}}};
  };
  auto find = [](const std::vector<topo::MetricDelta> &d, const char *key) {
    for (const topo::MetricDelta &m : d)
      if (m.key == key)
        return m;
    fail(key, __FILE__, __LINE__);
  };
  std::vector<topo::MetricDelta> d =
      topo::compareRuns(run("structural", "compliance", 10.0, 0.3),
                        run("structural", "compliance", 8.0, 0.3));
  CHECK(find(d, "objective_final").comparable);
  CHECK(find(d, "objective_final").selected == 8.0);

  d = topo::compareRuns(run("structural", "compliance", 10.0, 0.3),
                        run("structural", "mass", 2.0, 0.2));
  CHECK(!find(d, "objective_final").comparable);
  CHECK(find(d, "volume_fraction_final").comparable);

  // A fluid budget and a solid budget are different quantities.
  d = topo::compareRuns(run("structural", "compliance", 10.0, 0.3),
                        run("flow", "dissipation", 1.0, 0.3));
  CHECK(!find(d, "volume_fraction_final").comparable);
}

void testRetainedSourceIdentity() {
  const json original = {
      {"id", "doc-cpp00000001"}, {"name", "Bracket"},
      {"schema", "aicad.part/1"}, {"units", {{"system", "MMGS"}}},
      {"material", {{"key", "alu_6061"}}},
      {"features", json::array({{{"id", "f1"}, {"type", "box"},
                                 {"params", {{"width", 40.0}}}}})},
      {"studies", json::array()}};
  const std::string key = topo::geometryKey(original);
  json reopened = original;
  reopened["id"] = "doc-cpp00000009";
  reopened["name"] = "Bracket saved as";
  reopened["studies"] = json::array({{{"runs", json::array({{{"ok", true}}})}}});
  CHECK(topo::geometryKey(reopened) == key);

  json changed = reopened;
  changed["features"][0]["params"]["width"] = 41.0;
  CHECK(topo::geometryKey(changed) != key);
  changed = reopened;
  changed["features"][0]["id"] = "f2"; // Face/feature associations changed.
  CHECK(topo::geometryKey(changed) != key);
  changed = reopened;
  changed["units"]["system"] = "IPS";
  CHECK(topo::geometryKey(changed) != key);
  changed = reopened;
  changed["material"]["key"] = "steel_1018";
  CHECK(topo::geometryKey(changed) != key);
}

} // namespace

int main() {
  testCapabilityMatrix();
  testSwitchPreservesUnownedKeys();
  testValidation();
  testPrintSetup();
  testDfamMergeAndPrintSurface();
  testResolvedFacesetDefinitionEdits();
  testInvalidResolvedFacesetDefinitionsAreTransactional();
  testApplyRunSettings();
  testOutcomeHonesty();
  testManufacturingIsMeasuredNotRequested();
  testRunComparison();
  testRetainedSourceIdentity();
  std::puts("test_sim_topology: ok");
  return 0;
}
