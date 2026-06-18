// Curve / reference-feature builders — see curves.h. Port of engine/doc/features/
// curves.py's wire builders, replicating build123d Edge.make_helix / Edge.make_spline.
#include "geom/curves.h"

#include <cmath>

#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepLib.hxx>
#include <Geom_ConicalSurface.hxx>
#include <Geom_CylindricalSurface.hxx>
#include <Geom_Surface.hxx>
#include <Geom2d_Line.hxx>
#include <Geom2d_TrimmedCurve.hxx>
#include <GeomAPI_Interpolate.hxx>
#include <Geom_BSplineCurve.hxx>
#include <GeomAbs_Shape.hxx>
#include <TColgp_HArray1OfPnt.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax3.hxx>
#include <gp_Dir.hxx>
#include <gp_Dir2d.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Trsf.hxx>

namespace geom {

namespace {
constexpr double kPi = 3.14159265358979323846;
inline double rad(double deg) { return deg * kPi / 180.0; }
}  // namespace

TopoDS_Wire make_helix(double pitch, double height, double radius, double cone_angle_deg,
                       double start_angle_deg, bool lefthand) {
    try {
        const gp_Ax3 ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
        Handle(Geom_Surface) surf;
        if (cone_angle_deg == 0.0) {
            surf = new Geom_CylindricalSurface(ax3, radius);
        } else {
            surf = new Geom_ConicalSurface(ax3, rad(cone_angle_deg), radius);
        }

        // 2D helix line in (u,v): direction normalize(line_sign*2pi, pitch); the
        // trimmed length is (height / line_dir.Y) / cos(cone_angle). (Edge.make_helix.)
        const double line_sign = lefthand ? -1.0 : 1.0;
        double dx = line_sign * 2.0 * kPi, dy = pitch;
        const double dlen = std::sqrt(dx * dx + dy * dy);
        if (dlen < 1e-12) return TopoDS_Wire();
        const double ndx = dx / dlen, ndy = dy / dlen;
        const double line_len = (height / ndy) / std::cos(rad(cone_angle_deg));

        Handle(Geom2d_Line) hline = new Geom2d_Line(gp_Pnt2d(0, 0), gp_Dir2d(ndx, ndy));
        Handle(Geom2d_TrimmedCurve) hcurve =
            new Geom2d_TrimmedCurve(hline, 0.0, line_len, Standard_True);

        BRepBuilderAPI_MakeEdge mk(hcurve, surf);
        if (!mk.IsDone()) return TopoDS_Wire();
        TopoDS_Edge edge = mk.Edge();
        BRepLib::BuildCurves3d(edge, 1e-9, GeomAbs_C1, 14, 2000);

        BRepBuilderAPI_MakeWire mw(edge);
        if (!mw.IsDone()) return TopoDS_Wire();
        TopoDS_Wire wire = mw.Wire();

        // start_angle: rotate the wire about local +Z (Axis.Z) by start_angle degrees
        // — exactly the curves.py wire.rotate(Axis.Z, start_angle) post-step.
        if (std::abs(start_angle_deg) > 1e-9) {
            gp_Trsf trsf;
            trsf.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), rad(start_angle_deg));
            wire = TopoDS::Wire(BRepBuilderAPI_Transform(wire, trsf, true).Shape());
        }
        return wire;
    } catch (...) {
        return TopoDS_Wire();
    }
}

TopoDS_Wire make_spline_wire(const std::vector<std::array<double, 3>>& pts) {
    if (pts.size() < 2) return TopoDS_Wire();
    try {
        // build123d Edge.make_spline INTERPOLATES through every point via
        // GeomAPI_Interpolate (periodic=false, tol=1e-6) — NOT an approximating
        // PointsToBSpline fit. Match it exactly so the swept-solid volume of a
        // variable-pitch helix / flat spiral agrees with the oracle to full tolerance.
        Handle(TColgp_HArray1OfPnt) arr =
            new TColgp_HArray1OfPnt(1, static_cast<int>(pts.size()));
        for (size_t i = 0; i < pts.size(); ++i)
            arr->SetValue(static_cast<int>(i) + 1, gp_Pnt(pts[i][0], pts[i][1], pts[i][2]));
        GeomAPI_Interpolate interp(arr, Standard_False, 1.0e-6);
        interp.Perform();
        if (!interp.IsDone()) return TopoDS_Wire();
        Handle(Geom_BSplineCurve) curve = interp.Curve();
        if (curve.IsNull()) return TopoDS_Wire();
        BRepBuilderAPI_MakeEdge mk(curve);
        if (!mk.IsDone()) return TopoDS_Wire();
        BRepBuilderAPI_MakeWire mw(mk.Edge());
        if (!mw.IsDone()) return TopoDS_Wire();
        return mw.Wire();
    } catch (...) {
        return TopoDS_Wire();
    }
}

}  // namespace geom
