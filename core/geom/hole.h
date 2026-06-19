// Hole cutting-tool builder (C++ port of the geometry core of
// engine/doc/features/hole.py::_make_hole_tool, in the SIMPLIFIED model). This is
// the kernel primitive the doc model's `hole` feature compiler lays down: a single
// drilling tool (shaft cylinder + an optional counterbore cylinder or countersink
// cone at the mouth) positioned at a world `origin` and pointed along a unit
// `drill_dir` INTO the material. The doc model builds one tool per hole position,
// unions them, and cuts the union from the target body.
//
// WHY a separate translation unit (not part of features/booleans):
//   features.cpp builds PRIMITIVES from sketch profiles; booleans.cpp COMBINES
//   finished solids; this builds the DRILL TOOL the boolean then subtracts. Keeping
//   it a clean (origin, dir, dims) -> shape function mirrors the Python split and
//   keeps the tool independently testable.
//
// The SIMPLIFIED model carries NO thread tables / cosmetic threads / grid params /
// sketch positions (the Python's richer machinery) — just the three geometric
// styles and explicit diameter / cbore / csink dims.
#pragma once

#include <Eigen/Dense>
#include <TopoDS_Shape.hxx>

namespace geom {

// The three drilled-hole geometries (SolidWorks Hole Wizard, simplified — no tap).
enum class HoleStyle { Simple, Cbore, Csink };

// Resolved hole dimensions (mm / degrees). Only the fields a style needs are read:
//   Simple -> diameter, depth.
//   Cbore  -> + cbore_d (> diameter), cbore_depth.
//   Csink  -> + csink_d (> diameter), csink_angle_deg (included angle).
struct HoleDims {
    double diameter = 0.0;        // shaft (bore) diameter
    double depth = 0.0;           // shaft length along drill_dir (into material)
    double cbore_d = 0.0;         // counterbore diameter (Cbore)
    double cbore_depth = 0.0;     // counterbore depth at the mouth (Cbore)
    double csink_d = 0.0;         // countersink mouth diameter (Csink)
    double csink_angle_deg = 90.0;// countersink included angle in degrees (Csink)
    double csink_lift = 0.0;      // over-drive added to the cone height (face-clearance lift)
};

// Build one hole cutting tool at `origin`, drilling along unit `drill_dir` (INTO the
// material). The shaft is a cylinder (radius diameter/2, height `depth`) along the
// drill axis. Cbore adds a wider shallow cylinder (radius cbore_d/2, height
// cbore_depth) at the mouth (same origin/axis). Csink adds a cone (base radius
// csink_d/2 at the mouth narrowing to 0, height = (csink_d/2)/tan(angle/2)) unioned
// at the mouth. The cbore/csink pieces are fused onto the shaft. Returns the tool
// solid (empty TopoDS_Shape on a kernel failure, mirroring the geom boolean
// discipline). The axis is gp_Ax2(gp_Pnt(origin), gp_Dir(drill_dir)).
TopoDS_Shape make_hole_tool(const Eigen::Vector3d& origin,
                            const Eigen::Vector3d& drill_dir,
                            HoleStyle style, const HoleDims& dims);

}  // namespace geom
