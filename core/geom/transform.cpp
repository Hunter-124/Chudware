// Rigid-transform copy operators — see transform.h for the design rationale (these
// are the kernel primitives the doc model's pattern compilers replicate a seed body
// with). Both go through BRepBuilderAPI_Transform(copy=true) so the result is fresh
// topology independent of the source.

#include "geom/transform.h"

#include <BRepBuilderAPI_Transform.hxx>
#include <TopLoc_Location.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include <cmath>

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

// Mirror-copy: build a reflection gp_Trsf about the gp_Ax2 whose origin is the plane
// point and whose main Direction is the plane normal (so SetMirror(gp_Ax2) reflects
// across the plane), then make a fresh transformed copy of the shape (copy=true keeps
// `s` untouched). A mirror is a reflection (negative-determinant) transform; OCCT's
// BRepBuilderAPI_Transform produces a valid reflected solid.
TopoDS_Shape mirror_copy(const TopoDS_Shape& s, const Eigen::Vector3d& plane_point,
                         const Eigen::Vector3d& plane_normal) {
    gp_Ax2 ax(gp_Pnt(plane_point.x(), plane_point.y(), plane_point.z()),
              gp_Dir(plane_normal.x(), plane_normal.y(), plane_normal.z()));
    gp_Trsf t;
    t.SetMirror(ax);
    return BRepBuilderAPI_Transform(s, t, /*copy=*/true).Shape();
}

// Rigid instance: compose ONE rigid gp_Trsf (rotate-about-pivot first, then translate —
// the same order as translate_copy(rotate_copy(...))) and apply it as a TopLoc_Location
// via TopoDS_Shape::Moved. No geometry is copied: the result shares the source TShape, so
// identical placed bodies share triangulation + properties. `combined = T * R` applies R
// first, then T (OCCT composes right-to-left).
TopoDS_Shape rigid_instance(const TopoDS_Shape& s, const Eigen::Vector3d& pivot,
                            const Eigen::Vector3d& axis_dir, double angle_rad,
                            const Eigen::Vector3d& translate) {
    gp_Trsf rot;
    if (std::abs(angle_rad) > 1e-12)
        rot.SetRotation(gp_Ax1(gp_Pnt(pivot.x(), pivot.y(), pivot.z()),
                               gp_Dir(axis_dir.x(), axis_dir.y(), axis_dir.z())),
                        angle_rad);
    gp_Trsf trn;
    trn.SetTranslation(gp_Vec(translate.x(), translate.y(), translate.z()));
    gp_Trsf combined = trn.Multiplied(rot);  // rotate first, then translate
    return s.Moved(TopLoc_Location(combined));
}

}  // namespace geom
