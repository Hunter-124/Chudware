// core/piping/route.h — orthogonal obstacle-avoiding pipe routing (contract
// RPC `piping_route`, contracts/piping_schema.md §8.1). A bend-penalised 3-D
// A* over a rectilinear grid: uniform lines every grid_mm plus the exact
// coordinates of the endpoints, the nozzle stub tips and every inflated
// obstacle face, so routes hug obstacles at exactly the requested clearance.
// Obstacles are world AABBs inflated by clearance_mm + OD/2 (configuration
// space of the centreline); existing runs of the target document are added
// as obstacles too. The result is a new run (plus a 90° LR elbow fitting at
// every bend) appended to a given or new aicad.piping/1 document that passes
// validate_piping. Pure C++, OCCT-free.
#pragma once

#include <array>

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// params: {start:{point:[x,y,z], dir?:[dx,dy,dz]}, end:{point, dir?}, nps,
//          schedule="40", material?, obstacles:[{min,max,name?}],
//          clearance_mm=50, grid_mm?, bend_penalty?, piping?, name?,
//          ignore_bodies?:[names]}
// `start.dir` is the travel direction leaving the start point; `end.dir` is
// the travel direction arriving at the end point (a nozzle with outward
// normal n takes end.dir = -n). Both optional, snapped to the nearest axis.
// Success: {ok:true, piping, run_id, path, length_mm, bends,
// elbow_ids, min_clearance_mm, nearest_obstacle, grid_mm, bend_penalty_mm,
// search, warnings, hint, next_actions}; refusal {ok:false, reason, hint}
// with reason in no_route / start_inside_obstacle / end_inside_obstacle /
// invalid_params / unknown_pipe_size / invalid_piping /
// route_domain_too_large.
ojson piping_route(const ojson& params);

// Intrinsic-XYZ Euler angles (degrees) of the rotation taking the canonical
// elbow (in along +X, out along +Y — services/piping/threed.py _elbow_solid)
// onto the unit travel directions (d_in, d_out): the fitting `orient`
// convention of threed.py _place (build123d Rot = Rx·Ry·Rz).
std::array<double, 3> elbow_orient(const std::array<double, 3>& d_in,
                                   const std::array<double, 3>& d_out);

}  // namespace chudware::piping
