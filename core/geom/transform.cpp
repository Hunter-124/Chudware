// Rigid-transform copy operators — see transform.h for the design rationale (these
// are the kernel primitives the doc model's pattern compilers replicate a seed body
// with). Both go through BRepBuilderAPI_Transform(copy=true) so the result is fresh
// topology independent of the source.

#include "geom/transform.h"

#include <BRepBuilderAPI_Transform.hxx>
#include <gp_Ax1.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

// Translate-copy: build a pure-translation gp_Trsf from the Eigen vector, then make a
// fresh transformed copy of the shape (copy=true keeps `s` untouched).
TopoDS_Shape translate_copy(const TopoDS_Shape& s, const Eigen::Vector3d& v) {
    gp_Trsf t;
    t.SetTranslation(gp_Vec(v.x(), v.y(), v.z()));
    return BRepBuilderAPI_Transform(s, t, /*copy=*/true).Shape();
}

// Rotate-copy: build a rotation gp_Trsf about the gp_Ax1 (point + direction), then
// make a fresh transformed copy of the shape (copy=true keeps `s` untouched). Angle
// is CCW about axis_dir, in radians.
TopoDS_Shape rotate_copy(const TopoDS_Shape& s, const Eigen::Vector3d& axis_pt,
                         const Eigen::Vector3d& axis_dir, double angle_rad) {
    gp_Ax1 axis(gp_Pnt(axis_pt.x(), axis_pt.y(), axis_pt.z()),
                gp_Dir(axis_dir.x(), axis_dir.y(), axis_dir.z()));
    gp_Trsf t;
    t.SetRotation(axis, angle_rad);
    return BRepBuilderAPI_Transform(s, t, /*copy=*/true).Shape();
}

}  // namespace geom
