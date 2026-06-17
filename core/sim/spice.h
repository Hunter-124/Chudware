// Native MNA circuit simulator — C++ port of engine/sim/circuit.py (+ the linear device
// stamps of engine/sim/devices.py). FIRST increment: the linear DC operating point for
// R / C / L / V / I / E(VCVS) / G(VCCS). Nonlinear devices (D/Q/M + Newton), transient
// (trapezoidal), AC sweep, and the SPICE netlist parser are deferred to follow-ups.
//
// The Circuit dict + study + result shapes are the sim.md contract, consumed/produced
// byte-for-byte: simulate(circuit, study) -> {convergence, op, dc, tran, ac, log}.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::sim {

// Run the requested analyses of `study` on `circuit`. This increment handles the "op"
// analysis (linear DC); other analyses report convergence:false with a log note until
// they are ported. Never throws — a malformed device / singular matrix sets
// convergence.op=false with a log diagnostic.
nlohmann::json spice_simulate(const nlohmann::json& circuit, const nlohmann::json& study);

}  // namespace aicad::sim
