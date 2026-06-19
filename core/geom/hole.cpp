// Hole cutting-tool builder — see hole.h for the design rationale (the simplified
// shaft + optional cbore-cylinder / csink-cone model, ported from
// engine/doc/features/hole.py::_make_hole_tool).
//
// All pieces share the same axis (gp_Ax2 at `origin` pointing along `drill_dir`), so
// the shaft, the counterbore cylinder, and the countersink cone are coaxial; the
// caller positions the origin just above the target face (a small lift) so the tool
// cleanly clears the face's coplanarity when the doc model cuts it from the body.

#include "geom/hole.h"

#include <cmath>

#include <BRepPrimAPI_MakeCone.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include "geom/booleans.h"

namespace geom {

namespace {
constexpr double kPi = 3.14159265358979323846;
inline double radians(double deg) { return deg * kPi / 180.0; }
}  // namespace

TopoDS_Shape make_hole_tool(const Eigen::Vector3d& origin,
                            const Eigen::Vector3d& drill_dir,
                            HoleStyle style, const HoleDims& dims) {
    // Coaxial frame: cylinder/cone build along +axis from `origin` toward the
    // material (drill_dir). gp_Dir normalises drill_dir (it need not be unit-length).
    gp_Ax2 ax(gp_Pnt(origin.x(), origin.y(), origin.z()),
              gp_Dir(drill_dir.x(), drill_dir.y(), drill_dir.z()));

    // Shaft: the bore. radius = diameter/2, height = depth (already includes the
    // caller's lift so the mouth clears the face).
    const double r = dims.diameter / 2.0;
    TopoDS_Shape tool = BRepPrimAPI_MakeCylinder(ax, r, dims.depth).Shape();
    if (tool.IsNull()) return TopoDS_Shape();

    if (style == HoleStyle::Cbore) {
        // Wider, shallow cylinder at the mouth (the counterbore pocket).
        const double cb_r = dims.cbore_d / 2.0;
        TopoDS_Shape cbore = BRepPrimAPI_MakeCylinder(ax, cb_r, dims.cbore_depth).Shape();
        if (cbore.IsNull()) return TopoDS_Shape();
        tool = fuse(tool, cbore);
    } else if (style == HoleStyle::Csink) {
        // Cone at the mouth: base radius csink_d/2 (at origin), narrowing to 0 over a
        // height that gives the requested included angle: cone_h = (csink_d/2)/tan(a/2).
        const double cs_r = dims.csink_d / 2.0;
        const double half = radians(dims.csink_angle_deg / 2.0);
        const double t = std::tan(half);
        const double cone_h = cs_r / (t > 1e-9 ? t : 1e-9) + dims.csink_lift;
        TopoDS_Shape cone = BRepPrimAPI_MakeCone(ax, cs_r, 0.0, cone_h).Shape();
        if (cone.IsNull()) return TopoDS_Shape();
        tool = fuse(tool, cone);
    }

    return tool;
}

}  // namespace geom
