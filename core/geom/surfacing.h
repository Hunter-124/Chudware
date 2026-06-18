// Surfacing feature geometry — C++ port of engine/doc/features/surf_*.py.
//
// These build OPEN surface bodies (Face / Shell / Compound of shells) — sheet
// bodies with NO enclosed volume — plus the two surface->solid bridges
// (surf_thicken, and surf_knit when the sewn shell is watertight). The
// differential parity gate compares EXACT surface AREA + face/edge counts for the
// open-shell ops (volume ~= 0 is meaningless there), and exact VOLUME for the
// solid-producing ones (thicken / closed-knit), mirroring tools/cpp_surf_parity.py.
//
// PARITY DISCIPLINE: every builder mirrors the oracle's OCCT call exactly —
//   surf_planar    : the bounding sketch FACE (no thickness).
//   surf_extrude   : BRepPrimAPI_MakePrism over each boundary WIRE (side wall, no caps).
//   surf_revolve   : BRepPrimAPI_MakeRevol over the profile OUTER WIRE (open shell).
//   surf_sweep     : BRepOffsetAPI_MakePipe of the outer wire (open swept wall).
//   surf_loft      : BRepOffsetAPI_ThruSections(isSolid=false) through section wires.
//   surf_ruled     : BRepFill::Face_s between two single edges, else ruled ThruSections.
//   surf_offset    : BRepOffsetAPI_MakeOffsetShape::PerformBySimple on the face/shell.
//   surf_fill      : BRepFill_Filling over the bounding edges (patch face).
//   surf_boundary  : BRepFill_Filling over the boundary net (C0 pass-through).
//   surf_knit      : BRepBuilderAPI_Sewing -> shell; watertight -> MakeSolid.
//   surf_trim      : BRepAlgoAPI_Splitter face-by-face, keep a side, re-sew.
//   surf_delete_face: explode the solid, sew the KEPT faces into an open shell.
//   surf_replace_face: BOP-split the solid by the surface, keep the retained side.
//   surf_thicken   : BRepOffset_MakeOffset(Thickening=true) sheet -> solid.
#pragma once

#include <string>
#include <vector>

#include <Eigen/Dense>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Wire.hxx>

namespace geom {

// --- surf_extrude: sweep each boundary wire of a profile face into a side-wall
// shell (no caps). `dir` need not be unit; `amount` is the linear extent. When
// `mid_plane`, the wall straddles the sketch plane (-amount/2 .. +amount/2).
// Multiple profile faces / inner holes each contribute a shell into a compound.
// Empty on failure.
TopoDS_Shape surf_extrude_walls(const std::vector<TopoDS_Face>& faces,
                                const Eigen::Vector3d& dir, double amount,
                                bool mid_plane);

// --- surf_revolve: revolve each profile face's OUTER WIRE about the axis into an
// open shell (surface of revolution). `angle_rad` CCW about `axis_dir`.
TopoDS_Shape surf_revolve_walls(const std::vector<TopoDS_Face>& faces,
                                const Eigen::Vector3d& axis_pt,
                                const Eigen::Vector3d& axis_dir, double angle_rad);

// --- surf_sweep: sweep each profile face's outer wire along the spine into an
// open swept shell (BRepOffsetAPI_MakePipe, no caps).
TopoDS_Shape surf_sweep_walls(const std::vector<TopoDS_Face>& faces,
                              const TopoDS_Wire& spine);

// --- surf_loft: open lofted shell through >=2 section wires
// (BRepOffsetAPI_ThruSections, isSolid=false). `ruled` -> straight transitions.
TopoDS_Shape surf_loft_shell(const std::vector<TopoDS_Wire>& sections, bool ruled);

// --- surf_ruled: ruled surface between two rails. Two single-edge rails ->
// BRepFill::Face_s (a Face); a multi-edge rail -> ruled ThruSections (a Shell).
TopoDS_Shape surf_ruled_surface(const TopoDS_Wire& rail_a, const TopoDS_Wire& rail_b);

// --- surf_offset: parallel surface offset of the face set, signed `distance`
// along the face normals (BRepOffsetAPI_MakeOffsetShape::PerformBySimple). >1 face
// is sewn into a shell first.
TopoDS_Shape surf_offset_surface(const std::vector<TopoDS_Face>& faces, double distance);

// --- surf_fill / surf_boundary: fit a patch face inside a set of boundary edges
// (BRepFill_Filling). `continuity` 0/1/2 -> GeomAbs_C0/C1/C2.
TopoDS_Shape surf_fill_patch(const std::vector<TopoDS_Edge>& edges, int continuity);

// --- surf_knit: sew the surface bodies into one shell; if watertight, promote to
// a solid. `tol` is the sew tolerance. `out_solid` is set true when the result is
// a closed solid (volume meaningful), false for an open shell (area meaningful).
TopoDS_Shape surf_knit_bodies(const std::vector<TopoDS_Shape>& bodies, double tol,
                              bool& out_solid);

// --- surf_delete_face: explode `solid` into faces, sew back every face whose
// centroid is NOT in `delete_centroids` (within tol) -> an open shell.
TopoDS_Shape surf_delete_faces(const TopoDS_Shape& solid,
                               const std::vector<Eigen::Vector3d>& delete_centroids,
                               double tol);

// --- surf_trim: split each face of `surface` by `tool_face`, bucket the pieces
// along the tool normal, keep `keep` ("positive"|"negative"|"both"), re-sew. Sets
// `out_split_count` to the number of faces the tool actually divided.
TopoDS_Shape surf_trim_surface(const TopoDS_Shape& surface, const TopoDS_Face& tool_face,
                               const std::string& keep, int& out_split_count);

// --- surf_replace_face: split `solid` by the replacement `surface`, keep the
// piece(s) that do NOT carry any face at `target_centroids` (the retained side),
// fuse them into one solid. `diag` is the solid's bbox diagonal (tolerance scale).
// Empty on a failure to heal a positive-volume solid.
TopoDS_Shape surf_replace_face_solid(const TopoDS_Shape& solid,
                                     const std::vector<Eigen::Vector3d>& target_centroids,
                                     const TopoDS_Shape& surface, double diag);

// --- surf_thicken: offset an open sheet (Face/Shell) into a solid. `side` ∈
// "one"|"both"|"mid"; `thickness` > 0; `flip` reverses the one-side direction.
TopoDS_Shape surf_thicken_solid(const TopoDS_Shape& surface, double thickness,
                                const std::string& side, bool flip);

// --- helpers shared with document.cpp ---------------------------------------
// Surface area (GProp SurfaceProperties) of a shape.
double shape_area(const TopoDS_Shape& shape);
// Face / edge counts (TopExp::MapShapes deduped maps).
int shape_face_count(const TopoDS_Shape& shape);
int shape_edge_count(const TopoDS_Shape& shape);
// Area centroid of a single face (for delete_face / trim bucketing).
Eigen::Vector3d face_centroid(const TopoDS_Face& f);

}  // namespace geom
