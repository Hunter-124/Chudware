// Electrical Rules Check (ERC).
//
// run_erc(sch, netlist) returns a deterministic, sorted list of violation dicts.
// Pure + advisory: malformed designs are reported rather than thrown and inputs are
// never mutated. Shape: {rule, severity, net|null, pins:[pinref], xy:[x,y]|null,
// message}.
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// run_erc(sch, netlist) -> array of advisory violation dicts (deterministic order).
nlohmann::json run_erc(const nlohmann::json& sch, const nlohmann::json& netlist);

}  // namespace chudware::ecad
