// Swept blends: an exact fillet or chamfer for a straight edge whose two faces are constant
// along it.
//
// BRepFilletAPI_MakeFillet and _MakeChamfer build a blend together with its end caps, and the
// caps fail where an end vertex joins more than one face besides the two supports: the
// builder either refuses the edge at every size or reports success with vertex tolerances
// tens of millimetres wide (a hook's rounded tip meeting a wall where a second curved face of
// the hook also ends). A straight edge whose two faces are each swept along it (a plane that
// contains the edge direction, a cylinder or a linear extrusion along it) has one exact
// cross-section along its whole length: the region between the two section curves and the
// rolling-ball arc, or the chamfer segment. Such a blend is built from operations that do not
// depend on the end topology:
//
//   1. the cross-section region, in the plane through the edge's midpoint normal to it;
//   2. a prism of that region, extended past both ends of the edge;
//   3. split by the surfaces of the faces that end the edge (extended across the blend),
//      keeping the pieces on the edge's side of them;
//   4. cut from the body (an outside edge) or fused to it (an inside edge); the new end faces
//      that lie on an end face's surface are merged into that face.
//
// Each step is checked (tangency, the region on the material side the edge's convexity
// demands, the exchanged volume equal to the clipped prism's), so a configuration this
// construction cannot represent fails with a reason instead of returning a wrong solid.
#pragma once

#include <cstddef>
#include <functional>
#include <string>
#include <vector>

#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>

namespace geom {

struct SweepBlendProfile {
    enum class Kind { round, chamfer, chamfer_angle };
    Kind kind = Kind::round;
    // round: the radius. chamfer: the setback on the reference face. chamfer_angle: the
    // setback on the reference face.
    double size = 0.0;
    // chamfer: the setback on the other face. chamfer_angle: the angle between the chamfer
    // and the reference face, in radians, in (0, pi/2).
    double size2 = 0.0;
    // Chamfers: the reference face is the support whose outward normal at the edge is
    // closest to this direction. Unset: the first face in the edge's ancestor order.
    bool has_reference = false;
    gp_Dir reference_normal;
};

// Pure analysis: `edge` is straight, borders exactly two distinct faces, and each face is a
// plane containing the edge direction, a cylinder whose axis is parallel to it, or a linear
// extrusion along it, and the faces are not tangent along the edge.
bool sweep_blend_prismatic(const TopoDS_Edge& edge,
                           const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces);

// The outward normal of `face` at the midpoint of `edge`, one of its boundary edges.
bool sweep_blend_face_normal(const TopoDS_Face& face, const TopoDS_Edge& edge, gp_Dir& normal);

struct SweepBlendRun {
    TopoDS_Shape shape;                  // null on failure
    std::vector<TopoDS_Edge> remaining;  // `others`, as edges of `shape` (all pieces of a split edge)
    std::string why;                     // the first failure
    std::size_t failed = 0;              // the index into `edges` that failed, when it was one
    bool edge_failed = false;
};

// Blend `edges` of the solid `shape` one at a time, in order, edge i with profile(i). Like the
// kernel's contour, a blend runs on along sharp edges that continue its edge in a straight line
// between faces of the same cross-section; a selected edge on a run already blended is covered
// by it. An edge changed by an earlier blend of the run (shortened where the blends meet) is
// blended as it then is. `others` are carried over to the result. When an OpHistoryScope is
// open, every boolean's history is recorded.
SweepBlendRun sweep_blend_edges(const TopoDS_Shape& shape, const std::vector<TopoDS_Edge>& edges,
                                const std::function<SweepBlendProfile(std::size_t)>& profile,
                                const std::vector<TopoDS_Edge>& others);

// Which selected edges a swept-blend attempt takes over from the kernel builder.
enum class SweepBlendScope {
    isolated = 1,  // prismatic edges sharing no vertex with another selected edge
    all = 2        // every prismatic edge
};

// The indices of `edges` the scope takes; empty when it takes none.
std::vector<std::size_t> sweep_blend_choose(
    const std::vector<TopoDS_Edge>& edges,
    const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces, SweepBlendScope scope);

}  // namespace geom
