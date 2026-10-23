// core/piping/piping.h — single dispatch entry for the piping worker surface.
// C++ port of the services/mcp/piping_tools.py tool envelopes (which the MCP
// server routes through core_bridge): each method takes the tool's
// arguments as a params object and returns the tool's result envelope,
// byte-identical to the Python handler for the same inputs. Session state
// (the _PIPING last-solve bookkeeping) stays with the MCP layer — every
// method here takes explicit inputs. Returns false for methods not owned
// (caller falls through).
#pragma once

#include <string>

#include "piping/pyutil.h"

namespace chudware::piping {

// Methods: piping_solve, piping_report, piping_operating_point, piping_fluid,
// piping_cfd_boundary, piping_verify_cfd.
bool call(const std::string& method, const pyutil::ojson& params, pyutil::ojson& result);

}  // namespace chudware::piping
