// core/hydraulic/hydraulic.h — single dispatch entry for the hydraulic
// worker surface. C++ port of the services/mcp/hydraulic_tools.py tool
// envelopes (which the MCP server routes through core_bridge): each
// method takes the tool's arguments as a params object and returns the
// tool's result envelope, byte-identical to the Python handler for the same
// inputs. Session state (the _HYD active-document / last-handoff
// bookkeeping) stays with the MCP layer — every method here takes explicit
// inputs. Returns false for methods not owned (caller falls through).
#pragma once

#include <string>

#include "piping/pyutil.h"

namespace chudware::hydraulic {

// Methods: hydraulic_new, hydraulic_add_component, hydraulic_add_hose,
// hydraulic_size_cylinder, hydraulic_size_pump, hydraulic_size_accumulator,
// hydraulic_valve_flow, hydraulic_simulate (sim.h), hydraulic_size_motor and
// pump_drive_size (motor.h), hydraulic_bom, hydraulic_piping_handoff,
// hydraulic_verify_piping, hydraulic_fea_handoff, hydraulic_verify_fea.
bool call(const std::string& method, const pyutil::ojson& params, pyutil::ojson& result);

}  // namespace chudware::hydraulic
