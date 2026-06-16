// Minimal OCCT feature builders (stand-ins for the real sketch->profile compilers,
// which arrive later with the doc model). Each returns a TopoDS_Shape suitable for
// geom::tessellate_body. Used by core/tests/test_geom_aicm.cpp.
#pragma once

#include <TopoDS_Shape.hxx>

namespace geom {

// Axis-aligned box of size dx x dy x dz with its min corner at the origin
// (BRepPrimAPI_MakeBox). A canonical 6-plane / 12-edge / 8-vertex solid.
TopoDS_Shape make_box(double dx, double dy, double dz);

// Extrude a w x h rectangle (in the world XY plane, min corner at origin) along +Z
// by `depth`: BRepBuilderAPI_MakePolygon -> MakeFace -> BRepPrimAPI_MakePrism.
// Geometrically equivalent to a box, but built through the sketch->prism path.
TopoDS_Shape extrude_rect(double w, double h, double depth);

}  // namespace geom
