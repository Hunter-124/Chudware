// Electrical Rules Check (ERC) — C++ port of engine/ecad/erc.py.
//
// run_erc(sch, netlist) returns a deterministic, sorted list of violation dicts.
// Pure + advisory: never raises on a bad design, never mutates inputs. Violation
// shape: {rule, severity, net|null, pins:[pinref], xy:[x,y]|null, message}.
// Ordering mirrors the oracle (final sort by rule, net, pins, xy). OCCT-free.
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// run_erc(sch, netlist) -> array of violation dicts (deterministic order).
nlohmann::json run_erc(const nlohmann::json& sch, const nlohmann::json& netlist);

}  // namespace chudware::ecad
