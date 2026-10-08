// core/hydraulic/sim.h — lumped-parameter transient simulation of an
// aicad.hydraulic/1 fluid-power circuit (contract: hydraulic_schema.md §8).
// Hose-connected ports form pressure nodes with hydraulic capacitance
// V/β_eff (entrained-air model + optional hose wall compliance); pumps are
// flow sources (optionally pressure-compensated), valves are orifice / spring
// elements, cylinders/rotary actuators/motors carry mechanical states with
// end stops, accumulators follow a polytropic gas law. Integrated with an
// adaptive L-stable implicit Euler scheme (Newton, finite-difference
// Jacobian) with exact per-step energy bookkeeping.
#pragma once

#include "piping/pyutil.h"

namespace chudware::hydraulic {

using pyutil::ojson;

// params: {doc, duration_s=5, max_step_s?, output_dt_s=0.01, valve_schedule,
// loads, fluid, initial}. Returns the hydraulic_simulate envelope
// ({ok:true, time_s, nodes, components, summary, solver, warnings, hint,
// next_actions} or {ok:false, reason, hint, ...}).
ojson simulate(const ojson& params);

}  // namespace chudware::hydraulic
