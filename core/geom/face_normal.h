// Outward normal of an elementary (gp_Ax3-framed) B-rep face.
//
// The surface normal of a gp_Pln is XDirection × YDirection (= du × dv of its
// parameterisation). That equals Axis().Direction() ONLY for a right-handed
// (Direct) gp_Ax3 frame. OCCT legitimately builds LEFT-handed frames — every
// revolve (BRepPrimAPI_MakeRevol) end/shoulder face whose profile edge runs away
// from the axis gets one — and there the surface normal is -Axis().Direction().
// The old idiom "Axis().Direction(), flipped when the face is REVERSED" therefore
// reported the x=0 end face of a revolved shaft as +X (inward), drilled holes on
// it outward into air, and so on. Cylinders/cones obey the same rule for their
// radial direction (du × dv = ±radial by frame handedness).
//
// The face's topological orientation is then applied on top: a REVERSED face's
// material normal is the opposite of its surface normal.
#pragma once

#include <TopAbs_Orientation.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Ax3.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>

namespace chudware::geom {

// ±1 turning a framed surface's "frame-positive" normal (plane: frame Z; cylinder/cone:
// radial-outward from the axis) into the face's OUTWARD material normal: -1 for a
// left-handed frame, and -1 again for a REVERSED face.
inline double surface_outward_sign(const TopoDS_Face& face, const gp_Ax3& frame) {
    double s = frame.Direct() ? 1.0 : -1.0;
    if (face.Orientation() == TopAbs_REVERSED) s = -s;
    return s;
}

// Signed outward (material) unit normal of a planar face whose adapted plane is
// `pln` (BRepAdaptor_Surface(face).Plane(), location already applied).
inline gp_Dir plane_outward_normal(const TopoDS_Face& face, const gp_Pln& pln) {
    gp_Dir n = pln.Axis().Direction();
    if (surface_outward_sign(face, pln.Position()) < 0.0) n.Reverse();
    return n;
}

}  // namespace chudware::geom
