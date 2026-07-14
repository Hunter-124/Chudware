// Native MNA circuit simulator — C++ port of engine/sim/circuit.py +
// engine/sim/devices.py. Implements DC (Newton + gmin/source stepping), transient
// (trapezoidal / backward-Euler), AC, .dc sweep, tf/four/noise/step/temp/mc analyses,
// and nonlinear devices D/Q/M/S plus controlled sources E/G/F/H, mutual L (K),
// behavioral B, and OPAMP subckt. Deck parsing lives in sim/netlist.cpp.
//
// The Circuit dict + study + result shapes are the sim.md contract:
// spice_simulate(circuit, study) -> {convergence, op, dc, tran, ac, log, ...}.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::sim {

// Run the requested analyses of `study` on `circuit`. Never throws — a malformed
// device / singular matrix sets convergence.<type>=false with a log diagnostic.
nlohmann::json spice_simulate(const nlohmann::json& circuit, const nlohmann::json& study);

}  // namespace aicad::sim
