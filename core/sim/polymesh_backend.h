// Polymesh mesher backend — Polyhedral-FEA (github.com/Hunter-124/Polyhedral-FEA) driving
// chudware::sim::MeshOut.
//
// WHY a second mesher. Netgen is an advancing-front/Delaunay mesher on the exact BRep: it
// produces the better-shaped elements and it is the default. It also REFUSES some real
// STEP files outright (self-intersecting seams, sliver faces surviving the healing pass),
// and when it does, the FEA surface has nothing to fall back to except an STL
// re-tessellation of the same broken topology. Polymesh fills the bounding volume on a
// graded Cartesian lattice and only projects the boundary, so it is robust exactly where
// a front-marching mesher is fragile. Two independent meshers also mean a stress result
// can be checked for mesher dependence without leaving the app.
//
// The seam is deliberately narrow: this file converts geometry -> MeshOut and nothing
// else. Everything downstream (fea_static/modal/thermal, overlay skinning, the RPC
// payload, the viewport) stays backend-agnostic because MeshOut's contract does not move.
//
// UNITS. Chudware writes and reads STEP in MILLIMETRES, and OCCT's STEPControl_Reader
// keeps STEP file units, so polymesh's "metres" convention is really "whatever the file
// uses" for a file-loaded model. Coordinates therefore arrive in mm and are stored in
// MeshOut unscaled — the same as the Netgen path. Any element-size value handed to
// polymesh is likewise mm.
//
// LICENSE NOTE: BSD-3-Clause, statically linked. No dynamic-linking obligation (Netgen,
// LGPL-2.1) and no subprocess quarantine (CalculiX, GPL-2.0).
#pragma once

#include <string>

#include "sim/mesher.h"

namespace chudware::sim {

// Mesh a STEP file with the polymesh backend. Same contract as mesh_step: never throws,
// returns ok=false with a diagnostic on any failure. Honors options.fineness /
// target_size_mm / min_size_mm / order; options that describe Netgen-only machinery
// (curvature, structured hex, quad_surface, optimize) are not applicable and are
// reported in MeshOut::note rather than silently ignored.
//
// Compiled only under CHUDWARE_WITH_POLYMESH; otherwise a stub returns ok=false with a
// build-configuration message, so callers need no #ifdef.
MeshOut mesh_step_polymesh(const std::string& step_path, const MeshOptions& options);

}  // namespace chudware::sim
