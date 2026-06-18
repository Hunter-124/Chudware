// Curve / reference-feature builders (C++ port of engine/doc/features/curves.py).
//
// A curve feature produces a WIRE (a helix / flat spiral) consumed downstream by a
// sweep as its path — it never adds a solid body itself. These builders return a
// TopoDS_Wire in a LOCAL frame (axis +Z at the local origin); the document model
// lifts the wire through the curve's base plane before a sweep follows it.
//
// WHY a separate translation unit: features.cpp builds solid primitives, surfacing.cpp
// open sheets — these build 1-D spine curves. Keeping them apart mirrors the Python
// split (curves.py) and keeps each a clean, independently-testable function.
#pragma once

#include <array>
#include <vector>

#include <TopoDS_Wire.hxx>

namespace geom {

// Constant-pitch helix on a cylinder of `radius`, winding about local +Z from the
// origin. `pitch` is the axial advance per turn; `height` the total axial length;
// `cone_angle_deg` tapers the radius via a conical surface (0 = straight cylinder);
// `start_angle_deg` offsets the start about +Z; `lefthand` flips the handedness.
// Replicates build123d Edge.make_helix EXACTLY (Geom_Cylindrical/ConicalSurface +
// a 2D helix line wrapped onto it, BuildCurves3d) so a swept solid matches the
// oracle's. Both angles are in DEGREES (the curves.py contract). Null on failure.
TopoDS_Wire make_helix(double pitch, double height, double radius,
                       double cone_angle_deg, double start_angle_deg, bool lefthand);

// Variable-pitch / flat-spiral curve: a B-spline through `pts` (sampled densely along
// the analytic curve). Mirrors curves.py's Edge.make_spline path for the variable-
// pitch helix and the Archimedean flat spiral. Null wire on failure.
TopoDS_Wire make_spline_wire(const std::vector<std::array<double, 3>>& pts);

}  // namespace geom
