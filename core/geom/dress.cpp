#include "geom/dress.h"

#include <cmath>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRep_Tool.hxx>
#include <BRepFilletAPI_MakeChamfer.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepOffsetAPI_DraftAngle.hxx>
#include <BRepOffsetAPI_MakeThickSolid.hxx>
#include <BRepOffset_Mode.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_JoinType.hxx>
#include <GeomAbs_Shape.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <Standard_Failure.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>

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

// A seam edge (closed on its face — e.g. the 0°/360° closure of a full revolve) or a
// degenerate edge (a cone apex / sphere pole) is real B-rep topology but NOT a roundable
// model edge. Handing one to BRepFilletAPI makes Build() fail and nukes the whole feature,
// which is exactly the "fillet won't work on my revolved part" symptom. The tessellator
// hides these same edges (so the user never sees or picks one); skip them here too so an
// "all edges" / direction-filtered round on a revolved body can't trip over the seam.
bool is_blendable_edge(const TopoDS_Edge& edge,
                       const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    if (BRep_Tool::Degenerated(edge)) return false;
    if (edge_faces.Contains(edge)) {
        const TopTools_ListOfShape& faces = edge_faces.FindFromKey(edge);
        for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next()) {
            if (BRep_Tool::IsClosed(edge, TopoDS::Face(it.Value()))) return false;
        }
    }
    return true;
}

// Build a fillet/chamfer over `good`, dropping any edge that breaks the build instead of failing
// the whole feature. Fast path adds them all at once (the common, correct case); on failure it
// re-adds one at a time and keeps only the edges that still produce a valid solid. This is what
// the user expects when most edges are fine and one section is too tight for the radius (a rim near
// the axis, a corner OCCT can't resolve, radius > local half-width) — "round what you can" beats
// a hard error. `Builder` is BRepFilletAPI_MakeFillet/_MakeChamfer; `add(mk, edge)` applies the leg.
template <class Builder, class AddFn>
TopoDS_Shape blend_drop_failing(const TopoDS_Shape& shape,
                                const std::vector<TopoDS_Edge>& good, AddFn add,
                                int* n_dropped) {
    if (n_dropped) *n_dropped = 0;
    if (good.empty()) return TopoDS_Shape();
    try {
        Builder mk(shape);
        for (const TopoDS_Edge& e : good) add(mk, e);
        mk.Build();
        if (mk.IsDone()) return mk.Shape();
    } catch (const Standard_Failure&) { /* fall through to incremental */ }

    std::vector<TopoDS_Edge> accepted;
    accepted.reserve(good.size());
    for (const TopoDS_Edge& e : good) {
        try {
            Builder mk(shape);
            for (const TopoDS_Edge& a : accepted) add(mk, a);
            add(mk, e);
            mk.Build();
            if (mk.IsDone()) accepted.push_back(e);
        } catch (const Standard_Failure&) { /* skip this edge */ }
    }
    if (n_dropped) *n_dropped = (int)(good.size() - accepted.size());
    if (accepted.empty()) return TopoDS_Shape();
    try {
        Builder mk(shape);
        for (const TopoDS_Edge& e : accepted) add(mk, e);
        mk.Build();
        if (mk.IsDone()) return mk.Shape();
    } catch (const Standard_Failure&) {}
    return TopoDS_Shape();
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
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);

    int added = 0;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (!is_blendable_edge(edge, edge_faces)) continue;  // skip seams/degenerates
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
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);

    int added = 0;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (!is_blendable_edge(edge, edge_faces)) continue;  // skip seams/degenerates
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

// Filter a pick list to the blendable edges (drop seams/degenerates that slipped in).
static std::vector<TopoDS_Edge> blendable_only(const TopoDS_Shape& shape,
                                               const std::vector<TopoDS_Edge>& edges) {
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    std::vector<TopoDS_Edge> good;
    good.reserve(edges.size());
    for (const TopoDS_Edge& e : edges)
        if (is_blendable_edge(e, edge_faces)) good.push_back(e);
    return good;
}

TopoDS_Shape fillet_edges(const TopoDS_Shape& shape, double r,
                          const std::vector<TopoDS_Edge>& edges, int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    return blend_drop_failing<BRepFilletAPI_MakeFillet>(
        shape, blendable_only(shape, edges),
        [r](BRepFilletAPI_MakeFillet& mk, const TopoDS_Edge& e) { mk.Add(r, e); }, n_dropped);
}

TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           const std::vector<TopoDS_Edge>& edges, int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    // Symmetric chamfer: the single-distance Add(d, edge) sets both legs to d.
    return blend_drop_failing<BRepFilletAPI_MakeChamfer>(
        shape, blendable_only(shape, edges),
        [d](BRepFilletAPI_MakeChamfer& mk, const TopoDS_Edge& e) { mk.Add(d, e); }, n_dropped);
}

TopoDS_Shape fillet_edges_variable(const TopoDS_Shape& shape, double r1, double r2,
                                   const std::vector<TopoDS_Edge>& edges, int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    // Linear radius law r1 (edge start) -> r2 (edge end), matching dress.py::_variable_fillet.
    return blend_drop_failing<BRepFilletAPI_MakeFillet>(
        shape, blendable_only(shape, edges),
        [r1, r2](BRepFilletAPI_MakeFillet& mk, const TopoDS_Edge& e) { mk.Add(r1, r2, e); },
        n_dropped);
}

TopoDS_Shape chamfer_edges_two_distance(const TopoDS_Shape& shape, double d1, double d2,
                                        const std::vector<TopoDS_Edge>& edges, int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    // The asymmetric Add needs the reference FACE the d1 leg is measured on (build123d
    // chamfer(length,length2) picks the first incident face). Build an edge->faces map, then route
    // through the SAME drop-failing builder as fillet/symmetric-chamfer so a too-tight edge is
    // dropped (and reported) instead of nuking the whole feature.
    TopTools_IndexedDataMapOfShapeListOfShape ef;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, ef);
    return blend_drop_failing<BRepFilletAPI_MakeChamfer>(
        shape, blendable_only(shape, edges),
        [d1, d2, &ef](BRepFilletAPI_MakeChamfer& mk, const TopoDS_Edge& e) {
            if (ef.Contains(e) && !ef.FindFromKey(e).IsEmpty())
                mk.Add(d1, d2, e, TopoDS::Face(ef.FindFromKey(e).First()));
        },
        n_dropped);
}

// --- shell ----------------------------------------------------------------------
TopoDS_Shape shell_solid(const TopoDS_Shape& body, double t, bool outward,
                         const std::vector<TopoDS_Face>& remove) {
    if (body.IsNull() || !(t > 0.0)) return TopoDS_Shape();
    // SolidWorks default = inward wall (offset NEGATIVE, material kept inside the
    // original surface); `outward` flips to +t. Mode = Skin (offset the skin).
    const Standard_Real offset = outward ? t : -t;
    // Strategy table. The default (Arc join / no face-intersection / tight tolerance)
    // matches dress.py's build123d offset and handles prismatic solids exactly like
    // SolidWorks. Lofted / swept / revolved bodies carry B-spline faces whose offsets
    // self-intersect at the rounded corners → the Arc join fails ("empty shape").
    // Re-trying with an Intersection join + the face-intersection flag + a looser
    // tolerance recovers many freeform bodies (OCCT's recommended path for offsetting
    // curved skins). We return on the first strategy that yields a non-null solid.
    struct Strat { GeomAbs_JoinType join; Standard_Boolean inter; Standard_Real tol; };
    static const Strat strats[] = {
        {GeomAbs_Arc,          Standard_False, 1.0e-6},
        {GeomAbs_Intersection, Standard_True,  1.0e-6},
        {GeomAbs_Intersection, Standard_True,  1.0e-3},
        {GeomAbs_Arc,          Standard_True,  1.0e-3},
    };
    for (const Strat& s : strats) {
        try {
            TopTools_ListOfShape faces_to_remove;
            for (const TopoDS_Face& f : remove) faces_to_remove.Append(f);
            BRepOffsetAPI_MakeThickSolid mk;
            mk.MakeThickSolidByJoin(body, faces_to_remove, offset, s.tol,
                                    BRepOffset_Skin, s.inter, Standard_False, s.join);
            mk.Build();
            if (mk.IsDone()) {
                TopoDS_Shape out = mk.Shape();
                if (!out.IsNull()) return out;
            }
        } catch (const Standard_Failure&) {
            // This strategy threw inside OCCT — fall through to the next one.
        }
    }
    return TopoDS_Shape();
}

// True if any face of `shape` is a freeform / non-analytic surface (B-spline or
// Bezier) — the surface types a loft/sweep/revolve produces and on which the OCCT
// thick-solid offset routinely fails. Used to turn the shell "empty shape" failure
// into an actionable "build the wall into the profile instead" message.
bool has_freeform_faces(const TopoDS_Shape& shape) {
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        BRepAdaptor_Surface as(TopoDS::Face(ex.Current()));
        const GeomAbs_SurfaceType st = as.GetType();
        if (st == GeomAbs_BSplineSurface || st == GeomAbs_BezierSurface ||
            st == GeomAbs_SurfaceOfRevolution || st == GeomAbs_SurfaceOfExtrusion)
            return true;
    }
    return false;
}

// --- draft ----------------------------------------------------------------------
TopoDS_Shape draft_faces(const TopoDS_Shape& body, const std::vector<TopoDS_Face>& faces,
                         const Eigen::Vector3d& neutral_pt, const Eigen::Vector3d& neutral_n,
                         const Eigen::Vector3d& pull, double angle_rad) {
    if (body.IsNull() || faces.empty()) return TopoDS_Shape();
    if (!(neutral_n.norm() > 1e-12) || !(pull.norm() > 1e-12)) return TopoDS_Shape();
    const gp_Pln neutral(gp_Pnt(neutral_pt.x(), neutral_pt.y(), neutral_pt.z()),
                         gp_Dir(neutral_n.x(), neutral_n.y(), neutral_n.z()));
    const gp_Dir pull_dir(pull.x(), pull.y(), pull.z());
    BRepOffsetAPI_DraftAngle mk(body);
    for (const TopoDS_Face& f : faces) mk.Add(f, pull_dir, angle_rad, neutral);
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

}  // namespace geom
