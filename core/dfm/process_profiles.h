// core/dfm/process_profiles.h — manufacturing-process profiles + the rule-based
// process resolver (FDM-default) with the ambiguity gate (C++ port of
// engine_oracle/dfm/process_profiles.py). The single owner of the DfM "fit brain":
// process numbers, the ambiguity gate and the fit-clearance math are identical on
// every path (MCP intake/dimension-scheme tools, the in-app agent geometry helpers).
// PURE: no OCCT, no doc/agent linkage; default densities come from core/materials
// (the unified registry) with the oracle's literal fallbacks.
#pragma once

#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware {
namespace dfm {

using nlohmann::json;

// Thrown with the exact message of the Python oracle's ValueError.
struct DfmError : std::runtime_error { using std::runtime_error::runtime_error; };

// Public list of every known process id, in the oracle's PROFILES order:
// fdm, cnc_mill, lathe, sheet_metal, laser, injection.
std::vector<std::string> known_processes();

// get_profile(process, overrides=null): a fresh copy of the doc-resident
// ProcessProfile {"process","material","params"}. Overrides: "hole_comp" replaces
// params.hole_comp_mm; "xy_comp" is recorded as params.xy_comp_mm and added onto
// tolerance_band_mm (rounded half-even to 4); unknown keys are stashed under
// params._extra_overrides. Throws DfmError on an unknown process.
json get_profile(const std::string& process, const json* overrides = nullptr);

// fit_clearance(process, fit_class): the diametral clearance (mm) to add for one fit
// class ("slip"/"location"/"press"/"free") on a process. Throws DfmError on an
// unknown process, unknown fit class, or a class the process does not define.
double fit_clearance(const std::string& process, const std::string& fit_class);

// modeled_dim(nominal_mm, fit_class, process, is_hole): the stacked, build-ready
// dimension -> {"modeled_mm","clearance_mm","hole_comp_mm","rationale"}.
json modeled_dim(double nominal_mm, const std::string& fit_class, const std::string& process,
                 bool is_hole = false);

// resolve_process(request_or_spec, material=null, quantity=null): the FDM-default
// resolver + ambiguity gate. `request_or_spec` is a string OR a normalized-intake
// dict. Returns the oracle's resolution dict {"chosen","confidence","runner_up",
// "ambiguous","must_block","clarification","assumptions","why"}.
json resolve_process(const json& request_or_spec, const json& material = json(),
                     const json& quantity = json());

// process_block_text(profile): the human-readable PROCESS BLOCK appended to the
// planner prompt. `profile` is a get_profile output (possibly with overrides +
// material/assumed layered on).
std::string process_block_text(const json& profile);

// ---- single dispatch entry ------------------------------------------------------
// Owns "dfm_known_processes", "dfm_get_profile", "dfm_resolve_process",
// "dfm_fit_clearance", "dfm_modeled_dim", "dfm_process_block_text". Returns false
// when the method is not ours. Success -> {"ok":true,"result":<value>}; oracle
// ValueErrors -> _err()'s {"ok":false,"error"} (no code). `err` stays empty.
bool call(const std::string& method, const json& params, json& result, std::string& err);

}  // namespace dfm
}  // namespace chudware
