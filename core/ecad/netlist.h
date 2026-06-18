// Netlist builder — C++ port of engine/ecad/netlist.py.
//
// Pure function of a schematic document (aicad.sch/1): walks wires / junctions /
// pins / labels / power ports, computes connectivity by a union-find over points
// snapped to a 1e-6 mm grid, names each net by priority (power > global label >
// local/hier label/sheet pin > auto N$<n>), and emits {nets, by_pin, components,
// warnings}. Deterministic / byte-stable — every set/map iteration is sorted by id
// or coordinate so json.dumps is identical to the Python oracle. OCCT-free.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::ecad {

// build_netlist(sch) -> {"nets", "by_pin", "components", "warnings"}.
// Pure: does not mutate `sch`. Mirrors engine/ecad/netlist.py::build_netlist.
nlohmann::json build_netlist(const nlohmann::json& sch);

}  // namespace aicad::ecad
