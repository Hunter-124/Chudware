// Fillet run-off: an inside-corner blend that is wider than the face it rolls on.
//
// BRepFilletAPI_MakeFillet needs the whole blend to stay on its two support faces, and
// does not always manage when the blend crosses a face's edge. A boss that stands closer
// to a plate's edge than the fillet radius, or a rib that stops short of a flange's edge,
// can therefore fail outright. A designer expects the blend to run off the edge of the
// face and end flush with it. The kernel has no such operation, so it is built from three
// it does have:
//
//   1. extend the support past its open (convex, perpendicular) rim, so the blend has face
//      to roll on, in one of two ways:
//        ring: a planar support grows by a ring of material around its footprint;
//        cap:  the face that ends a prism-shaped support (a boss's top, a plate's side,
//              its chamfer or edge round) is swept outward, which extends a cylindrical
//              wall, a thin rib or a plate with its exact cross-section;
//   2. blend the extended body as an ordinary fillet;
//   3. cut the result back: remove everything the extension and the blend put beyond the
//      original body's open end, keeping any material the body already had there. A cap is
//      cut back flush with the plane through the support's rim, square to the support.
//
// Only inside-corner (additive) blends terminate this way. Rounding an outside edge wider
// than its face has no meaningful run-off, and keeps the radius correction.
#pragma once

#include <string>
#include <vector>

#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

namespace geom {

// ring, cap: see above. separate: blends that collide when built together (two bosses closer
// than a radius) but build one contour at a time are built alone, and the material each
// adds is united; the blends then merge where they meet.
enum class FilletRunoffMode { ring = 1, cap = 2, separate = 3 };

struct FilletRunoffCap {
    TopoDS_Face face;   // the face ending the support (a face of `shape`)
    gp_Vec direction;   // unit vector to extend along, away from the support
    gp_Pnt origin;      // a point of the plane (normal `direction`) the blend is cut back to
};

struct FilletRunoffPlan {
    std::vector<TopoDS_Face> rings;  // planar supports to extend by a ring (faces of `shape`)
    std::vector<FilletRunoffCap> caps;
    bool separable = false;          // several edges, so possibly several contours
    std::string why;                 // set when run-off does not apply at all

    bool applicable(FilletRunoffMode mode) const {
        if (!why.empty()) return false;
        switch (mode) {
            case FilletRunoffMode::ring: return !rings.empty();
            case FilletRunoffMode::cap: return !caps.empty();
            case FilletRunoffMode::separate: return separable;
        }
        return false;
    }
};

// Pure analysis, no booleans. The three modes are tried in the order ring, cap, separate. `edge_faces` maps the edges of `shape` to their faces. A
// support is extended only across a rim the blend can reach (within two radii of a
// selected edge's interior, off to its side).
FilletRunoffPlan fillet_runoff_plan(const TopoDS_Shape& shape,
                                    const std::vector<TopoDS_Edge>& edges,
                                    const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                                    double radius);

struct FilletRunoff {
    TopoDS_Shape extended;           // `shape` with skirts, same-domain faces merged
    std::vector<TopoDS_Shape> trim;  // per support: the region the blend may not keep
    std::vector<TopoDS_Edge> edges;  // the selected edges, as edges of `extended`
    std::string why;                 // set (and `extended` null) on failure
};

FilletRunoff fillet_runoff_extend(const TopoDS_Shape& shape,
                                  const std::vector<TopoDS_Edge>& edges,
                                  const FilletRunoffPlan& plan, double radius,
                                  FilletRunoffMode mode);

// The filleted `extended` body with the overflow removed, as one solid. Null, with `why`,
// when the trim does not leave exactly one solid.
TopoDS_Shape fillet_runoff_trim(const FilletRunoff& runoff, const TopoDS_Shape& filleted,
                                std::string& why);

}  // namespace geom
