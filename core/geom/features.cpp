#include "geom/features.h"

#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepOffsetAPI_ThruSections.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepPrimAPI_MakeRevol.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>
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
