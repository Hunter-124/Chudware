#include "geom/features.h"

#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepOffsetAPI_MakePipe.hxx>
#include <BRepOffsetAPI_ThruSections.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCone.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepPrimAPI_MakeRevol.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <BRepPrimAPI_MakeTorus.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax1.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

TopoDS_Shape make_box(double dx, double dy, double dz) {
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape();
}

TopoDS_Shape extrude_rect(double w, double h, double depth) {
    // Closed rectangular wire in the world XY plane, min corner at the origin.
    BRepBuilderAPI_MakePolygon poly;
    poly.Add(gp_Pnt(0.0, 0.0, 0.0));
    poly.Add(gp_Pnt(w, 0.0, 0.0));
    poly.Add(gp_Pnt(w, h, 0.0));
    poly.Add(gp_Pnt(0.0, h, 0.0));
    poly.Close();
    TopoDS_Wire wire = poly.Wire();

    // Planar face bounded by the wire, then prism it +Z by `depth`.
    TopoDS_Face face = BRepBuilderAPI_MakeFace(wire).Face();
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, depth)).Shape();
}

TopoDS_Shape extrude_face(const TopoDS_Face& face, const Eigen::Vector3d& dir,
                          double depth, bool mid_plane) {
    const gp_Vec full(dir.x() * depth, dir.y() * depth, dir.z() * depth);
    if (mid_plane) {
        // Center the solid on the face: shift the face back by half the depth,
        // then prism the full depth so it straddles the sketch plane symmetrically.
        gp_Trsf trsf;
        trsf.SetTranslation(gp_Vec(-0.5 * full.X(), -0.5 * full.Y(), -0.5 * full.Z()));
        TopoDS_Shape shifted = BRepBuilderAPI_Transform(face, trsf, true).Shape();
        return BRepPrimAPI_MakePrism(shifted, full).Shape();
    }
    return BRepPrimAPI_MakePrism(face, full).Shape();
}

TopoDS_Shape revolve_face(const TopoDS_Face& face, const Eigen::Vector3d& axis_pt,
                          const Eigen::Vector3d& axis_dir, double angle_rad) {
    gp_Ax1 axis(gp_Pnt(axis_pt.x(), axis_pt.y(), axis_pt.z()),
                gp_Dir(axis_dir.x(), axis_dir.y(), axis_dir.z()));
    return BRepPrimAPI_MakeRevol(face, axis, angle_rad).Shape();
}

TopoDS_Shape make_box_centered(double l, double w, double h) {
    if (!(l > 0 && w > 0 && h > 0)) return TopoDS_Shape();
    return BRepPrimAPI_MakeBox(gp_Pnt(-l / 2, -w / 2, -h / 2), l, w, h).Shape();
}

TopoDS_Shape make_cylinder_centered(double r, double h) {
    if (!(r > 0 && h > 0)) return TopoDS_Shape();
    gp_Ax2 ax(gp_Pnt(0, 0, -h / 2), gp_Dir(0, 0, 1));
    return BRepPrimAPI_MakeCylinder(ax, r, h).Shape();
}

TopoDS_Shape make_sphere(double r) {
    if (!(r > 0)) return TopoDS_Shape();
    return BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), r).Shape();
}

TopoDS_Shape make_cone_centered(double r1, double r2, double h) {
    if (!(h > 0) || r1 < 0 || r2 < 0 || (r1 == 0 && r2 == 0)) return TopoDS_Shape();
    gp_Ax2 ax(gp_Pnt(0, 0, -h / 2), gp_Dir(0, 0, 1));
    return BRepPrimAPI_MakeCone(ax, r1, r2, h).Shape();
}

TopoDS_Shape make_torus(double major_r, double minor_r) {
    if (!(major_r > 0 && minor_r > 0)) return TopoDS_Shape();
    gp_Ax2 ax(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    return BRepPrimAPI_MakeTorus(ax, major_r, minor_r).Shape();
}

TopoDS_Shape sweep_face(const TopoDS_Face& profile, const TopoDS_Wire& spine) {
    if (profile.IsNull() || spine.IsNull()) return TopoDS_Shape();
    BRepOffsetAPI_MakePipe mk(spine, profile);
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

TopoDS_Shape loft_sections(const std::vector<TopoDS_Wire>& sections, bool ruled) {
    if (sections.size() < 2) return TopoDS_Shape();
    // isSolid=true caps the ends so the loft is a closed solid; pres3d default tol.
    BRepOffsetAPI_ThruSections mk(/*isSolid=*/Standard_True,
                                  /*ruled=*/ruled ? Standard_True : Standard_False,
                                  /*pres3d=*/1.0e-6);
    for (const TopoDS_Wire& w : sections) {
        if (w.IsNull()) return TopoDS_Shape();
        mk.AddWire(w);
    }
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

}  // namespace geom
