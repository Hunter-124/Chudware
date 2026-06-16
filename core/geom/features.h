// Minimal OCCT feature builders (stand-ins for the real sketch->profile compilers,
// which arrive later with the doc model). Each returns a TopoDS_Shape suitable for
// geom::tessellate_body. Used by core/tests/test_geom_aicm.cpp.
#pragma once

#include <vector>

#include <Eigen/Dense>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Wire.hxx>

namespace geom {

// Axis-aligned box of size dx x dy x dz with its min corner at the origin
// (BRepPrimAPI_MakeBox). A canonical 6-plane / 12-edge / 8-vertex solid.
TopoDS_Shape make_box(double dx, double dy, double dz);

// Extrude a w x h rectangle (in the world XY plane, min corner at origin) along +Z
// by `depth`: BRepBuilderAPI_MakePolygon -> MakeFace -> BRepPrimAPI_MakePrism.
// Geometrically equivalent to a box, but built through the sketch->prism path.
TopoDS_Shape extrude_rect(double w, double h, double depth);

// Extrude a planar face along `dir` (unit) by `depth`. If `mid_plane`, the solid
// is centered on the face's plane: the face is first translated by -dir*depth/2
// and then prismed the full depth, so the result straddles the sketch symmetrically
// (SolidWorks "Mid Plane" end condition). BRepPrimAPI_MakePrism under the hood.
TopoDS_Shape extrude_face(const TopoDS_Face& face, const Eigen::Vector3d& dir,
                          double depth, bool mid_plane = false);

// Revolve a planar face `angle_rad` about the axis (point + unit direction):
// BRepPrimAPI_MakeRevol about a gp_Ax1. Angle in radians (CCW about axis_dir).
// A profile offset from the axis sweeps a tube/annulus (matches revolve.py).
TopoDS_Shape revolve_face(const TopoDS_Face& face, const Eigen::Vector3d& axis_pt,
                          const Eigen::Vector3d& axis_dir, double angle_rad);

// --- primitives (centered on the origin, matching engine/doc/features/primitives.py
//     default Align.CENTER) — the SolidWorks base-primitive features. ---
TopoDS_Shape make_box_centered(double l, double w, double h);
TopoDS_Shape make_cylinder_centered(double r, double h);   // axis +Z, z in [-h/2, h/2]
TopoDS_Shape make_sphere(double r);                         // centered at origin
TopoDS_Shape make_cone_centered(double r1, double r2, double h);  // axis +Z, centered
TopoDS_Shape make_torus(double major_r, double minor_r);   // axis +Z, centered

// Build a rib slab from an OPEN contour wire (the rib's top trace): each contour
// edge is extruded along the in-plane drop direction (perpendicular to the contour,
// flipped toward `toward`) by `reach` into a silhouette face, then thickened to
// `thickness` centered on the sketch plane (along `normal`). Returns the unfused,
// untrimmed slab (the caller trims to the body bbox + fuses). Empty on failure.
// Port of the parallel/centered path of engine/doc/features/rib.py.
TopoDS_Shape make_rib_slab(const TopoDS_Wire& contour, const Eigen::Vector3d& normal,
                           const Eigen::Vector3d& toward, double thickness, double reach);

// An axis-aligned box with min corner at (x,y,z) and size dx x dy x dz (for bbox
// clipping). Empty on non-positive size.
TopoDS_Shape make_box_at(double x, double y, double z, double dx, double dy, double dz);

// Sweep a planar profile face along a spine wire (BRepOffsetAPI_MakePipe). The
// profile should sit at the spine start, roughly perpendicular to its tangent.
// Empty shape on kernel failure. Port of engine/doc/features/sweep.py's MakePipe
// fast path (guides/twist/orientation/thin deferred).
TopoDS_Shape sweep_face(const TopoDS_Face& profile, const TopoDS_Wire& spine);

// Loft a solid through an ordered list of section wires (BRepOffsetAPI_ThruSections,
// isSolid=true). `ruled` makes straight transitions between sections; otherwise a
// smoothed surface. Needs >= 2 wires, ordered + consistently oriented along the loft
// direction. Empty shape on kernel failure. Port of engine/doc/features/loft.py's
// plain ThruSections path (guides/centerline/thin deferred).
TopoDS_Shape loft_sections(const std::vector<TopoDS_Wire>& sections, bool ruled);

}  // namespace geom
