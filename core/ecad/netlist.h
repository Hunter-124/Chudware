// Deterministic schematic connectivity and netlist builder.
//
// Connectivity is sheet-local at a 1e-6 mm grid except for global labels,
// power ports, and explicit hierarchy bridges. Scalar wires and buses use
// independent geometric connectivity spaces. The result includes pin and
// object readback maps, grouped components, net metadata, expanded buses, and
// warnings. OCCT-free and pure: the input document is never mutated.
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// build_netlist(sch) -> {"nets", "by_pin", "by_object", "components",
//                        "net_info", "buses", "warnings"}.
nlohmann::json build_netlist(const nlohmann::json& sch);

}  // namespace chudware::ecad
