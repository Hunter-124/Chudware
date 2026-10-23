// SPICE netlist parser — C++ port of engine/sim/spice.py (parse_spice_deck) +
// engine/sim/subckt.py (.subckt/.ends collection + X-instance expansion).
//
// parse_netlist(text) tokenizes a SPICE deck into the FROZEN Circuit dict
// (contracts/ecad/sim.md §1):
//     {nodes:[...], devices:[{kind,name,nodes,value,model,params}],
//      models:{name:{type,...}}, params:{TEMP:27,...}}
// matching the Python oracle's output byte-for-byte (modulo JSON key order +
// float formatting), so the result can feed the already-ported C++ simulator
// (chudware::sim::spice_simulate).
#pragma once

#include <nlohmann/json.hpp>
#include <string>

namespace chudware::sim {

// Parse a full SPICE deck into the Circuit dict (study is discarded, matching
// spice.parse_spice). Built-in subckts (OPAMP, NE555) are always available and
// X-instances are inlined exactly as the Python front-end does.
nlohmann::json parse_netlist(const std::string& text);

// Map a schematic netlist (engine/ecad/netlist.py::build_netlist output, per
// netlist.md §1) into a (Circuit, warnings) pair — C++ port of
// engine/sim/spice.py::netlist_to_spice_circuit. Each component that carries a
// "sim" mapping (OR resolvable from the starter SYMBOL library by libid) is
// converted to a Device; nodes are ordered by sim["pin_order"] (a pin-NAME entry
// resolves against the number-keyed netlist pins via the symbol's name->number map,
// or the component's optional "pin_name_to_number" override); net "GND"/"0" -> "0".
// SUBCKT/OPAMP-backed parts emit an X instance expanded against the built-in
// subckt library. `sim_params` is folded into Circuit["params"].
//
// Sim-model resolution mirrors the Python oracle exactly: an INLINE "sim" mapping
// on the component takes precedence; absent that, the component's sim model is
// resolved from the starter SYMBOL library (engine/ecad/library/starter.py
// SYMBOLS[libid].sim), ported to core/sim/netlist.cpp::symbol_sim_table(). A
// component with neither an inline sim nor a known libid yields the oracle's exact
// "no sim model — skipped" warning.
//
// Returns {"circuit": <Circuit dict>, "warnings": [ ... ]}.
nlohmann::json netlist_to_spice_circuit(const nlohmann::json& netlist,
                                        const nlohmann::json& sim_params);

}  // namespace chudware::sim
