// SPICE netlist parser — C++ port of engine/sim/spice.py (parse_spice_deck) +
// engine/sim/subckt.py (.subckt/.ends collection + X-instance expansion).
//
// parse_netlist(text) tokenizes a SPICE deck into the FROZEN Circuit dict
// (docs/contracts/ecad/sim.md §1):
//     {nodes:[...], devices:[{kind,name,nodes,value,model,params}],
//      models:{name:{type,...}}, params:{TEMP:27,...}}
// matching the Python oracle's output byte-for-byte (modulo JSON key order +
// float formatting), so the result can feed the already-ported C++ simulator
// (aicad::sim::spice_simulate).
#pragma once

#include <nlohmann/json.hpp>
#include <string>

namespace aicad::sim {

// Parse a full SPICE deck into the Circuit dict (study is discarded, matching
// spice.parse_spice). Built-in subckts (OPAMP, NE555) are always available and
// X-instances are inlined exactly as the Python front-end does.
nlohmann::json parse_netlist(const std::string& text);

}  // namespace aicad::sim
