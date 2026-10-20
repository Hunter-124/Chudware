// Sensor stimulus scenarios — aicad.fwscenario/1 (T-RF15 port of
// engine_oracle/firmware/scenario.py + the fixtures library). Declarative
// time-series stimulus tracks compiled into Renode monitor commands spliced
// into the generated .resc. Pure parsing/validation/planning; the only I/O is
// materialize_aux_files. See contracts/firmware_rpc.md §9.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

inline constexpr const char* kScenarioSchema = "aicad.fwscenario/1";

struct ScenarioPlan {
    std::vector<std::string> setup_lines;
    std::vector<std::pair<double, std::string>> timeline;  // (t_ms, monitor line)
    std::map<std::string, std::string> aux_files;          // relpath -> content
    nlohmann::json resolved_links = nlohmann::json::array();
    std::vector<std::string> pre_platform_lines;
    double duration_ms = 0.0;
    std::vector<std::string> errors;
    std::vector<std::string> warnings;
};

// Scenario fixture library (engine_oracle/firmware/fixtures).
std::vector<std::string> list_fixtures();
nlohmann::json get_fixture(const std::string& name);  // null when unknown

// Accept a scenario as dict, JSON string, or fixture reference
// ("fixture:<name>" or a bare fixture name). Returns (scenario, errors) —
// scenario is null when errors is non-empty or no scenario was given
// (raw empty/null → {null, {}}).
std::pair<nlohmann::json, std::vector<std::string>> load_scenario(
    const nlohmann::json& raw);

// Structural validation of an aicad.fwscenario/1 dict. Pure.
std::vector<std::string> validate_scenario(const nlohmann::json& sc);

// Expand a waveform spec into 12-bit ADC counts (clamped 0..4095). Pure.
std::vector<int> waveform_samples(const nlohmann::json& wf, double sample_ms = 1.0,
                                  int n = 256);

// Compile a validated scenario into Renode monitor lines + aux files.
// `caps` is the T-FIN2 capability entry (platform_db.get_capabilities);
// `platform_text` the .repl content. Pure — aux files returned as content.
ScenarioPlan build_scenario_plan(const nlohmann::json& scenario, const nlohmann::json& caps,
                                 const std::string& platform_text, double horizon_ms = 0);

// Render a t_ms-sorted timeline into paused emulation RunFor intervals.
std::vector<std::string> timeline_resc_lines(
    const std::vector<std::pair<double, std::string>>& timeline, double run_seconds);

// Write plan aux files into `directory` and substitute {CHUDWARE_SCENARIO_DIR}
// in the plan's setup lines with its absolute path (mutates the plan).
void materialize_aux_files(ScenarioPlan& plan, const std::string& directory);

}  // namespace chudware::firmware
