// core/tolerance/stackup.h — 1D tolerance stack-up analysis (C++ port of
// engine_oracle/tolerance/stackup.py). Worst-case + RSS over a dimension chain
// (Fischer, "Mechanical Tolerance Stackup and Analysis," 2nd ed., CRC 2011, Ch. 3).
// PURE: no OCCT, no doc/agent linkage. Result envelopes are byte-identical to the MCP
// stackup_analyze tool (services/mcp/tolerance_tools.py).
#pragma once

#include <stdexcept>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware {
namespace tolerance {

using nlohmann::json;

// Carries the exact message of the Python oracle's ValueError.
struct StackupError : std::runtime_error { using std::runtime_error::runtime_error; };

// The standard preliminary-analysis disclaimer (byte-exact — tests assert it).
extern const char* const kDisclaimer;

// The oracle's StackupResult.to_dict(): {"ok":true, "nominal_gap", "worst_case",
// "rss", "contributors" (sorted desc by contrib_wc, stable), "assumed_dims",
// "disclaimer"}. All rounding mirrors Python's round(x, 8) / round(x, 4) (half-even).
// `dims` is an array of {name?, nominal, tol, direction?, ref?, assumed?}.
// Throws StackupError on an empty chain / missing-or-invalid nominal|tol / tol < 0.
json stackup_analyze(const json& dims);

// ---- single dispatch entry ------------------------------------------------------
// Owns "stackup_analyze" (params: {"dims": [...], "mode": "worst_case"|"rss"|"both"}).
// Returns false when the method is not ours (caller falls through). Validation
// failures land in `result` as the MCP wrapper's {"ok":false,"error","code"} envelope
// ("invalid_mode" / "invalid_input"); `err` stays empty.
bool call(const std::string& method, const json& params, json& result, std::string& err);

}  // namespace tolerance
}  // namespace chudware
