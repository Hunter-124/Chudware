#include "geom/dress.h"

#include <cmath>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepFilletAPI_MakeChamfer.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <GeomAbs_CurveType.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Dir.hxx>

namespace geom {

namespace {

// True if `edge` is a straight line whose direction is parallel to `dir` (either
// orientation: |dir·edgeDir| ≈ 1). Curved edges (circles/arcs/splines) return false —
// they have no single direction, so a `dir` filter can never select them. This is the
// same line-direction test dress.py's _line_dir / parallel checks use, via
// BRepAdaptor_Curve.Line().Direction().
bool edge_parallel_to(const TopoDS_Edge& edge, const Eigen::Vector3d& dir) {
    BRepAdaptor_Curve ac(edge);
    if (ac.GetType() != GeomAbs_Line) return false;
    const gp_Dir d = ac.Line().Direction();
    // dir is assumed (near-)unit from the caller; normalise defensively so the |dot|≈1
    // test is meaningful regardless of the caller's magnitude.
    Eigen::Vector3d ref = dir;
    const double n = ref.norm();
    if (n < 1e-12) return false;
    ref /= n;
    const double dot = d.X() * ref.x() + d.Y() * ref.y() + d.Z() * ref.z();
    return std::abs(std::abs(dot) - 1.0) < 1e-6;
}

}  // namespace

TopoDS_Shape fillet_edges(const TopoDS_Shape& shape, double r,
                          std::optional<Eigen::Vector3d> dir) {
    BRepFilletAPI_MakeFillet mk(shape);

    // Enumerate UNIQUE edges. A raw explorer would visit each shared edge once per
    // incident face (a box -> 24 hits for 12 edges); the indexed map de-dups so every
    // edge is Add()ed to the builder exactly once.
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);

    int added = 0;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (dir.has_value() && !edge_parallel_to(edge, *dir)) continue;
        mk.Add(r, edge);
        ++added;
    }
    if (added == 0) return TopoDS_Shape();  // nothing selected -> nothing to do

    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           std::optional<Eigen::Vector3d> dir) {
    BRepFilletAPI_MakeChamfer mk(shape);

    // Same unique-edge enumeration as fillet_edges (see note there).
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);

    int added = 0;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (dir.has_value() && !edge_parallel_to(edge, *dir)) continue;
        // Symmetric chamfer: the single-distance Add(d, edge) sets both legs to d.
        mk.Add(d, edge);
        ++added;
    }
    if (added == 0) return TopoDS_Shape();

    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

// --- explicit-edge overloads ---
// The doc model's edge-selector path: `edges` are already the unique body edges
// resolved by doc/picks::resolve_edge_picks, so we just Add() each to the builder (no
// re-enumeration, no dir filter). Empty `edges` -> empty shape (nothing to do).

TopoDS_Shape fillet_edges(const TopoDS_Shape& shape, double r,
                          const std::vector<TopoDS_Edge>& edges) {
    if (edges.empty()) return TopoDS_Shape();
    BRepFilletAPI_MakeFillet mk(shape);
    for (const TopoDS_Edge& edge : edges) mk.Add(r, edge);
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           const std::vector<TopoDS_Edge>& edges) {
    if (edges.empty()) return TopoDS_Shape();
    BRepFilletAPI_MakeChamfer mk(shape);
    // Symmetric chamfer: the single-distance Add(d, edge) sets both legs to d.
    for (const TopoDS_Edge& edge : edges) mk.Add(d, edge);
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

}  // namespace geom
