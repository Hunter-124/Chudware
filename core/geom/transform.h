// Rigid-transform copy operators: translate + rotate a finished solid into a NEW
// shape, leaving the source untouched. These are the kernel primitives the doc
// model's pattern compilers (linear_pattern / circular_pattern) lay down: a seed
// body is copied to each instance pose, then the copies are fused into one result.
//
// WHY a separate translation unit (not part of features/booleans/dress):
//   features.cpp builds PRIMITIVES, booleans.cpp COMBINES finished solids, dress.cpp
//   MODIFIES a solid's edges — and these just MOVE a finished solid rigidly. Keeping
//   them apart mirrors the kernel-layer split (each builder a clean shape->shape
//   function, independently testable) and keeps the doc model the only place that
//   knows the feature-tree contract.
//
// Both ops go through BRepBuilderAPI_Transform with copy=true, so the returned shape
// is a fresh, independent topology — the seed can be transformed many times for the
// many instances of a pattern without aliasing.
#pragma once

#include <Eigen/Dense>
#include <TopoDS_Shape.hxx>

namespace geom {

// Translate-copy: return a fresh copy of `s` shifted by the vector `v`
// (gp_Trsf::SetTranslation(gp_Vec) + BRepBuilderAPI_Transform copy). The input is
// left unmodified — used for a linear pattern's grid offsets.
TopoDS_Shape translate_copy(const TopoDS_Shape& s, const Eigen::Vector3d& v);

// Rotate-copy: return a fresh copy of `s` rotated by `angle_rad` (CCW about
// `axis_dir`) around the axis through `axis_pt` (gp_Trsf::SetRotation(gp_Ax1, angle)
// + BRepBuilderAPI_Transform copy). The input is left unmodified — used for a
// circular pattern's angular steps. Angle is in RADIANS.
TopoDS_Shape rotate_copy(const TopoDS_Shape& s, const Eigen::Vector3d& axis_pt,
                         const Eigen::Vector3d& axis_dir, double angle_rad);

// Mirror-copy: return a fresh copy of `s` reflected across the plane through
// `plane_point` perpendicular to `plane_normal` (gp_Trsf::SetMirror(gp_Ax2) +
// BRepBuilderAPI_Transform copy). The gp_Ax2's main Direction is the plane normal,
// so a mirror about that axis-system reflects across the plane. The input is left
// unmodified — used for the doc model's mirror feature compiler. `plane_normal` must
// be non-degenerate (it need not be unit-length; gp_Dir normalises it).
TopoDS_Shape mirror_copy(const TopoDS_Shape& s, const Eigen::Vector3d& plane_point,
                         const Eigen::Vector3d& plane_normal);

}  // namespace geom
