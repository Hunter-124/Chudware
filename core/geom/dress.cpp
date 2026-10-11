#include "geom/dress.h"
#include "geom/op_history.h"
#include "geom/shell_sections.h"
#include "util/process_isolation.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <chrono>
#include <cstdio>
#include <string>
#include <utility>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Curve2d.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRepOffset_Error.hxx>
#include <BRepOffset_MakeOffset.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRep_Tool.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepFilletAPI_MakeChamfer.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepOffsetAPI_DraftAngle.hxx>
#include <BRepOffsetAPI_MakeOffsetShape.hxx>
#include <BRepOffsetAPI_MakeThickSolid.hxx>
#include <BRepOffset_Mode.hxx>
#include <BRepLib.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
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
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Vec.hxx>

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

// Seam-aware watertight-solid test (mirrors document.cpp::dress_is_closed_solid). Every edge
// of a closed solid is shared by >= 2 faces; the only single-face exceptions are SEAMS (the
// 0/2pi closure of a cylinder/sphere/revolution skin) and DEGENERATE edges (a cone apex /
// sphere pole). Any OTHER edge with < 2 incident faces is a genuine FREE edge -> the shell is
// open. A partial blend that drops a too-tight edge mid-chain can hand back a body with a
// POSITIVE volume but an OPEN B-rep (BRepFilletAPI reports IsDone yet the surface didn't
// reseal). Such a body later fails eval_check / STEP re-validation / FEA, so we must never
// ACCEPT one — this is the acceptance gate that makes "round what you can" still yield a
// watertight solid instead of being rejected wholesale downstream (the seats-fillet symptom).
bool blend_is_watertight(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return false;
    try {
        TopTools_IndexedDataMapOfShapeListOfShape anc;
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc);
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(shape, TopAbs_EDGE, edges);
        if (edges.Extent() == 0) return false;
        for (int ei = 1; ei <= edges.Extent(); ++ei) {
            const TopoDS_Shape& es = edges.FindKey(ei);
            const int nf = anc.Contains(es) ? anc.FindFromKey(es).Extent() : 0;
            if (nf >= 2) continue;
            const TopoDS_Edge& e = TopoDS::Edge(es);
            if (BRep_Tool::Degenerated(e)) continue;
            bool seam = false;
            if (anc.Contains(es)) {
                const TopTools_ListOfShape& faces = anc.FindFromKey(es);
                for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next())
                    if (BRep_Tool::IsClosed(e, TopoDS::Face(it.Value()))) { seam = true; break; }
            }
            if (!seam) return false;
        }
        return true;
    } catch (const Standard_Failure&) {
        return false;
    }
}

// A blend build gets 5 s in its isolated child. A document-scale blend is milliseconds, so this
// is already three orders of magnitude of headroom; past it the child is assumed wedged (see
// process_isolation.cpp on the fork-in-a-multithreaded-process hazard) and the build is retried
// in-process rather than being reported as a failed feature.
constexpr std::chrono::seconds kBlendTimeout{5};

// Unique blendable edges of `shape`, optionally filtered to those parallel to `dir`.
// A raw explorer would visit each shared edge once per incident face (a box -> 24 hits
// for 12 edges); the indexed map de-dups so every edge is Add()ed exactly once.
std::vector<TopoDS_Edge> select_blendable(const TopoDS_Shape& shape,
                                         const std::optional<Eigen::Vector3d>& dir) {
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    std::vector<TopoDS_Edge> sel;
    sel.reserve((size_t)edges.Extent());
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (!is_blendable_edge(edge, edge_faces)) continue;  // skip seams/degenerates
        if (dir.has_value() && !edge_parallel_to(edge, *dir)) continue;
        sel.push_back(edge);
    }
    return sel;
}

// Build a fillet/chamfer over `good`, dropping any edge that breaks the build instead of failing
// the whole feature. Fast path adds them all at once (the common, correct case); on failure it
// re-adds one at a time and keeps only the edges that still produce a valid solid. This is what
// the user expects when most edges are fine and one section is too tight for the radius (a rim near
// the axis, a corner OCCT can't resolve, radius > local half-width) — "round what you can" beats
// a hard error. `Builder` is BRepFilletAPI_MakeFillet/_MakeChamfer; `add(mk, edge)` applies the leg.
// An edge is ACCEPTED only when its build is IsDone AND the result is still a WATERTIGHT solid:
// an edge whose drop (or whose own blend) opens the shell is rejected too, so the returned body
// is always closed — otherwise the downstream open-shell guard would reject the whole feature
// even though most edges rounded fine (the all-or-nothing "produced an open/invalid body" bug).
template <class Builder, class AddFn>
TopoDS_Shape blend_drop_failing(const TopoDS_Shape& shape,
                                const std::vector<TopoDS_Edge>& good, AddFn add,
                                int* n_dropped) {
    if (n_dropped) *n_dropped = 0;
    if (good.empty()) return TopoDS_Shape();

    // `prov` (non-null only for the build whose shape is RETURNED) receives the blend's face/
    // edge provenance for owner attribution; bisect probes skip it.
    auto build = [&](const std::vector<TopoDS_Edge>& edges, std::string* prov) -> TopoDS_Shape {
        auto result = chudware::util::isolate_shape_build([&]() -> TopoDS_Shape {
            try {
                Builder mk(shape);
                for (const TopoDS_Edge& e : edges) add(mk, e);
                mk.Build();
                if (!mk.IsDone()) return {};
                TopoDS_Shape s = mk.Shape();
                if (!blend_is_watertight(s)) return {};
                if (prov) *prov = op_provenance_encode(shape, s, mk);
                return s;
            } catch (const Standard_Failure&) {
                return {};
            }
        }, kBlendTimeout, prov);
        return result.ok ? result.shape : TopoDS_Shape();
    };
    const bool recording = op_history_recording();
    std::string prov;
    auto finish = [&](const TopoDS_Shape& out) {
        if (recording && !out.IsNull()) op_provenance_record(shape, out, prov);
        return out;
    };

    TopoDS_Shape all = build(good, recording ? &prov : nullptr);
    if (!all.IsNull()) return finish(all);

    // T-PERF10: bisect over the edge list instead of O(E²) one-edge-at-a-time rebuilds.
    // A crashing subset is indistinguishable from any other failed build, preserving the
    // established drop count and \"round what you can\" behavior.
    auto try_build = [&](const std::vector<TopoDS_Edge>& edges) -> bool {
        if (edges.empty()) return true;
        return !build(edges, nullptr).IsNull();
    };
    std::function<std::vector<TopoDS_Edge>(const std::vector<TopoDS_Edge>&)> select;
    select = [&](const std::vector<TopoDS_Edge>& span) -> std::vector<TopoDS_Edge> {
        if (span.empty()) return {};
        if (try_build(span)) return span;
        if (span.size() == 1) return {};
        const size_t mid = span.size() / 2;
        std::vector<TopoDS_Edge> left(span.begin(), span.begin() + (std::ptrdiff_t)mid);
        std::vector<TopoDS_Edge> right(span.begin() + (std::ptrdiff_t)mid, span.end());
        auto L = select(left);
        auto R = select(right);
        std::vector<TopoDS_Edge> merged = L;
        merged.insert(merged.end(), R.begin(), R.end());
        if (try_build(merged)) return merged;
        merged = L;
        for (const TopoDS_Edge& e : R) {
            std::vector<TopoDS_Edge> trial = merged;
            trial.push_back(e);
            if (try_build(trial)) merged.push_back(e);
        }
        return merged;
    };
    std::vector<TopoDS_Edge> accepted = select(good);
    if (n_dropped) *n_dropped = (int)(good.size() - accepted.size());
    return accepted.empty() ? TopoDS_Shape()
                            : finish(build(accepted, recording ? &prov : nullptr));
}

}  // namespace

// NOTE: the "round/bevel EVERY edge" (dir-filter) overloads below are intentionally
// ALL-OR-NOTHING. An oversize radius on the whole-body round is a mistake the caller wants
// flagged (a clear "too large" error leaving the body intact), NOT silently turned into a
// partial round of whichever subset happens to fit. The drop-failing/best-effort behaviour is
// reserved for the EXPLICIT-edge selector overloads (the doc model's `refs.edges` path — the
// "round these specific edges, skip the few too tight" case the rover seats hit).
// The two whole-body overloads route through the SAME fault isolation as the explicit-edge
// path: the crash class lives in OCCT's corner resolver, which a "round every edge" call
// reaches just as readily. Semantics are unchanged — all-or-nothing, an empty shape on
// failure — because an oversize radius on a whole-body round is a mistake the caller wants
// flagged, not silently turned into a partial round.
TopoDS_Shape fillet_edges(const TopoDS_Shape& shape, double r,
                          std::optional<Eigen::Vector3d> dir) {
    const std::vector<TopoDS_Edge> sel = select_blendable(shape, dir);
    if (sel.empty()) return TopoDS_Shape();  // nothing selected -> nothing to do
    const bool recording = op_history_recording();
    std::string prov;
    auto built = chudware::util::isolate_shape_build([&]() -> TopoDS_Shape {
        try {
            BRepFilletAPI_MakeFillet mk(shape);
            for (const TopoDS_Edge& e : sel) mk.Add(r, e);
            mk.Build();
            if (!mk.IsDone()) return {};
            if (recording) prov = op_provenance_encode(shape, mk.Shape(), mk);
            return mk.Shape();
        } catch (const Standard_Failure&) {
            return {};
        }
    }, kBlendTimeout, recording ? &prov : nullptr);
    if (!built.ok || built.shape.IsNull()) return TopoDS_Shape();
    if (recording) op_provenance_record(shape, built.shape, prov);
    return built.shape;
}

TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           std::optional<Eigen::Vector3d> dir) {
    const std::vector<TopoDS_Edge> sel = select_blendable(shape, dir);
    if (sel.empty()) return TopoDS_Shape();
    const bool recording = op_history_recording();
    std::string prov;
    auto built = chudware::util::isolate_shape_build([&]() -> TopoDS_Shape {
        try {
            BRepFilletAPI_MakeChamfer mk(shape);
            // Symmetric chamfer: the single-distance Add(d, edge) sets both legs to d.
            for (const TopoDS_Edge& e : sel) mk.Add(d, e);
            mk.Build();
            if (!mk.IsDone()) return {};
            if (recording) prov = op_provenance_encode(shape, mk.Shape(), mk);
            return mk.Shape();
        } catch (const Standard_Failure&) {
            return {};
        }
    }, kBlendTimeout, recording ? &prov : nullptr);
    if (!built.ok || built.shape.IsNull()) return TopoDS_Shape();
    if (recording) op_provenance_record(shape, built.shape, prov);
    return built.shape;
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

TopoDS_Shape chamfer_edges_distance_angle(const TopoDS_Shape& shape, double d,
                                          double angle_rad,
                                          const std::vector<TopoDS_Edge>& edges,
                                          const std::optional<TopoDS_Face>& reference_face,
                                          int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    return blend_drop_failing<BRepFilletAPI_MakeChamfer>(
        shape, blendable_only(shape, edges),
        [d, angle_rad, &edge_faces, &reference_face](BRepFilletAPI_MakeChamfer& mk,
                                                     const TopoDS_Edge& edge) {
            if (!edge_faces.Contains(edge) || edge_faces.FindFromKey(edge).IsEmpty()) return;
            const TopoDS_Face& face = reference_face
                ? *reference_face : TopoDS::Face(edge_faces.FindFromKey(edge).First());
            mk.AddDA(d, angle_rad, edge, face);
        },
        n_dropped);
}

TopoDS_Shape chamfer_vertices(const TopoDS_Shape& shape, double d,
                              const std::vector<TopoDS_Vertex>& vertices,
                              std::string* diag) {
    if (diag) diag->clear();
    if (vertices.empty()) {
        if (diag) *diag = "vertex type requires selected vertices";
        return {};
    }
    struct Corner {
        std::array<gp_Pnt, 4> points;
        double volume;
    };
    std::vector<Corner> corners;
    corners.reserve(vertices.size());
    try {
        TopTools_IndexedDataMapOfShapeListOfShape vertex_edges;
        TopExp::MapShapesAndAncestors(shape, TopAbs_VERTEX, TopAbs_EDGE, vertex_edges);
        for (const TopoDS_Vertex& vertex : vertices) {
            if (!vertex_edges.Contains(vertex)) {
                if (diag) *diag = "vertex type requires three incident straight edges";
                return {};
            }
            TopTools_IndexedMapOfShape incident;
            for (TopTools_ListIteratorOfListOfShape it(vertex_edges.FindFromKey(vertex));
                 it.More(); it.Next())
                incident.Add(it.Value());
            if (incident.Extent() != 3) {
                if (diag) *diag = "vertex type requires exactly three incident edges";
                return {};
            }
            const gp_Pnt origin = BRep_Tool::Pnt(vertex);
            Corner corner{{origin, gp_Pnt(), gp_Pnt(), gp_Pnt()}, 0.0};
            for (int i = 1; i <= 3; ++i) {
                const TopoDS_Edge edge = TopoDS::Edge(incident(i));
                if (BRep_Tool::Degenerated(edge) ||
                    BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line) {
                    if (diag) *diag = "vertex type requires straight non-degenerate edges";
                    return {};
                }
                TopoDS_Vertex first, last;
                TopExp::Vertices(edge, first, last);
                const TopoDS_Vertex& other = first.IsSame(vertex) ? last : first;
                const gp_Vec ray(origin, BRep_Tool::Pnt(other));
                if (!(ray.Magnitude() > d + 1e-7)) {
                    if (diag) *diag = "vertex chamfer distance must be shorter than every incident edge";
                    return {};
                }
                corner.points[i] = origin.Translated(ray.Multiplied(d / ray.Magnitude()));
            }
            const gp_Vec a(origin, corner.points[1]);
            const gp_Vec b(origin, corner.points[2]);
            const gp_Vec c(origin, corner.points[3]);
            const double determinant = a.Dot(b.Crossed(c));
            if (std::abs(determinant) <= 1e-9) {
                if (diag) *diag = "vertex corner has coplanar incident edges";
                return {};
            }
            if (determinant < 0.0) std::swap(corner.points[2], corner.points[3]);
            corner.volume = std::abs(determinant) / 6.0;
            corners.push_back(corner);
        }
    } catch (const Standard_Failure&) {
        if (diag) *diag = "vertex corner topology could not be resolved";
        return {};
    }

    const bool recording = op_history_recording();
    std::string prov;
    auto built = chudware::util::isolate_shape_build([&]() -> TopoDS_Shape {
        try {
            TopoDS_Shape current = shape;
            std::vector<Handle(BRepTools_History)> chain;
            for (const Corner& corner : corners) {
                const auto& p = corner.points;
                BRepBuilderAPI_Sewing sew;
                const int triangles[4][3] = {{1, 2, 3}, {0, 2, 1},
                                             {0, 3, 2}, {0, 1, 3}};
                for (const auto& tri : triangles) {
                    BRepBuilderAPI_MakePolygon poly;
                    for (int index : tri) poly.Add(p[index]);
                    poly.Close();
                    if (!poly.IsDone()) return {};
                    BRepBuilderAPI_MakeFace face(poly.Wire());
                    if (!face.IsDone()) return {};
                    sew.Add(face.Face());
                }
                sew.Perform();
                TopExp_Explorer shells(sew.SewedShape(), TopAbs_SHELL);
                if (!shells.More()) return {};
                const TopoDS_Shell shell = TopoDS::Shell(shells.Current());
                shells.Next();
                if (shells.More()) return {};
                BRepBuilderAPI_MakeSolid solid_builder(shell);
                if (!solid_builder.IsDone()) return {};
                TopoDS_Solid cutter = solid_builder.Solid();
                if (!BRepLib::OrientClosedSolid(cutter)) return {};
                GProp_GProps before;
                BRepGProp::VolumeProperties(current, before);
                BRepAlgoAPI_Cut cut(current, cutter);
                cut.Build();
                if (!cut.IsDone() || cut.Shape().IsNull()) return {};
                TopoDS_Shape next = cut.Shape();
                if (recording) chain.push_back(cut.History());
                GProp_GProps after;
                BRepGProp::VolumeProperties(next, after);
                const double removed = before.Mass() - after.Mass();
                if (std::abs(removed - corner.volume) >
                    std::max(1e-6, corner.volume * 1e-4) || !blend_is_watertight(next))
                    return {};
                current = next;
            }
            if (recording) prov = op_provenance_encode(shape, current, chain);
            return current;
        } catch (const Standard_Failure&) {
            return {};
        }
    }, kBlendTimeout, recording ? &prov : nullptr);
    if (!built.ok || built.shape.IsNull()) {
        if (diag) *diag = "vertex corner cut failed; select a convex three-edge corner and reduce the distance";
        return {};
    }
    if (recording) op_provenance_record(shape, built.shape, prov);
    return built.shape;
}

// --- shell ----------------------------------------------------------------------

namespace {

// One rung of the join/intersection/tolerance ladder both shell paths walk. The default
// (Arc join / no face-intersection / tight tolerance) matches dress.py's build123d offset
// and handles prismatic solids exactly like SolidWorks; the Intersection join + the
// face-intersection flag + a looser tolerance recovers many curved/B-spline skins whose
// Arc-joined offset self-intersects at rounded corners (OCCT's recommended path).
struct ShellStrat { GeomAbs_JoinType join; Standard_Boolean inter; Standard_Real tol; };
constexpr ShellStrat kShellStrats[] = {
    {GeomAbs_Arc,          Standard_False, 1.0e-6},
    {GeomAbs_Intersection, Standard_True,  1.0e-6},
    {GeomAbs_Intersection, Standard_True,  1.0e-3},
    {GeomAbs_Arc,          Standard_True,  1.0e-3},
};

std::string shell_strat_label(const ShellStrat& s) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s%s tol %.0e",
                  s.join == GeomAbs_Arc ? "arc" : "intersection",
                  s.inter ? "+face-intersection" : "", s.tol);
    return buf;
}

std::string offset_error_text(BRepOffset_Error e) {
    switch (e) {
        case BRepOffset_NoError: return "no result shape";
        case BRepOffset_BadNormalsOnGeometry:
            return "BRepOffset_BadNormalsOnGeometry (a face normal is undefined somewhere: "
                   "a pole or degenerate point)";
        case BRepOffset_C0Geometry:
            return "BRepOffset_C0Geometry (a face's surface is only C0 continuous)";
        case BRepOffset_NullOffset: return "BRepOffset_NullOffset";
        case BRepOffset_NotConnectedShell: return "BRepOffset_NotConnectedShell";
        case BRepOffset_CannotTrimEdges:
            return "BRepOffset_CannotTrimEdges (the offset faces could not be trimmed "
                   "against each other)";
        case BRepOffset_CannotFuseVertices: return "BRepOffset_CannotFuseVertices";
        case BRepOffset_CannotExtentEdge: return "BRepOffset_CannotExtentEdge";
        case BRepOffset_UserBreak: return "BRepOffset_UserBreak";
        case BRepOffset_MixedConnectivity:
            return "BRepOffset_MixedConnectivity (an edge whose faces meet at a crease along "
                   "part of it and tangentially along another part — OCCT's offset refuses "
                   "such an edge outright, for every join type and tolerance)";
        default: return "BRepOffset_UnknownError";
    }
}

// Locate the BRepOffset_MixedConnectivity edges: manifold edges whose dihedral angle is a
// crease at some samples and tangent at others (a rectangle-to-circle loft's corner seams
// run from a 90° crease at the rectangle to G1 at the circle). One "(x,y,z)->(x,y,z)
// a°->b°" description per offending edge, so the refusal names WHERE, not just what.
std::vector<std::string> mixed_connectivity_edges(const TopoDS_Shape& body) {
    std::vector<std::string> out;
    TopTools_IndexedDataMapOfShapeListOfShape ef;
    TopExp::MapShapesAndAncestors(body, TopAbs_EDGE, TopAbs_FACE, ef);
    for (int i = 1; i <= ef.Extent(); ++i) {
        const TopoDS_Edge& e = TopoDS::Edge(ef.FindKey(i));
        const TopTools_ListOfShape& fl = ef(i);
        if (BRep_Tool::Degenerated(e) || fl.Extent() != 2) continue;
        const TopoDS_Face f1 = TopoDS::Face(fl.First());
        const TopoDS_Face f2 = TopoDS::Face(fl.Last());
        if (f1.IsSame(f2)) continue;  // a seam: one face on both sides
        try {
            BRepAdaptor_Curve c(e);
            BRepAdaptor_Surface s1(f1), s2(f2);
            BRepAdaptor_Curve2d p1(e, f1), p2(e, f2);
            constexpr int kSamples = 16;
            double amin = 1e9, amax = -1.0, a_first = -1.0, a_last = -1.0;
            for (int k = 0; k <= kSamples; ++k) {
                const double u = c.FirstParameter() +
                                 (c.LastParameter() - c.FirstParameter()) * k / kSamples;
                const gp_Pnt2d uv1 = p1.Value(u), uv2 = p2.Value(u);
                BRepLProp_SLProps l1(s1, uv1.X(), uv1.Y(), 1, 1e-9);
                BRepLProp_SLProps l2(s2, uv2.X(), uv2.Y(), 1, 1e-9);
                if (!l1.IsNormalDefined() || !l2.IsNormalDefined()) continue;
                gp_Dir n1 = l1.Normal(), n2 = l2.Normal();
                if (f1.Orientation() == TopAbs_REVERSED) n1.Reverse();
                if (f2.Orientation() == TopAbs_REVERSED) n2.Reverse();
                const double a = n1.Angle(n2) * (180.0 / 3.14159265358979323846);
                amin = std::min(amin, a);
                amax = std::max(amax, a);
                if (a_first < 0.0) a_first = a;
                a_last = a;
            }
            if (!(amin < 0.5 && amax > 2.0)) continue;
            const gp_Pnt v0 = BRep_Tool::Pnt(TopExp::FirstVertex(e));
            const gp_Pnt v1 = BRep_Tool::Pnt(TopExp::LastVertex(e));
            char buf[160];
            std::snprintf(buf, sizeof(buf), "(%.4g,%.4g,%.4g)->(%.4g,%.4g,%.4g) %.0f°->%.0f°",
                          v0.X(), v0.Y(), v0.Z(), v1.X(), v1.Y(), v1.Z(), a_first, a_last);
            out.emplace_back(buf);
        } catch (const Standard_Failure&) {
        }
    }
    return out;
}

double solid_volume(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}

// Accept gate for one strategy's result: a BRepCheck-valid, watertight solid. Returns the
// rejection reason, or "" when the B-rep is sound (the volume test is the caller's).
std::string shell_brep_rejection(const TopoDS_Shape& out) {
    try {
        if (!BRepCheck_Analyzer(out).IsValid()) return "result failed BRepCheck validation";
    } catch (const Standard_Failure&) {
        return "result failed BRepCheck validation";
    }
    if (!blend_is_watertight(out)) return "result is an open (non-watertight) shell";
    return std::string();
}

// Fold per-strategy reasons into one diag: identical reasons collapse to a single line.
std::string shell_diag(const std::vector<std::pair<std::string, std::string>>& tried,
                       bool mixed, const TopoDS_Shape& body) {
    std::string d;
    bool same = !tried.empty();
    for (const auto& r : tried) same = same && r.second == tried.front().second;
    if (same) {
        d = "every offset strategy (";
        for (std::size_t i = 0; i < tried.size(); ++i)
            d += (i ? ", " : "") + tried[i].first;
        d += ") failed: " + tried.front().second;
    } else {
        for (std::size_t i = 0; i < tried.size(); ++i)
            d += (i ? "; " : "") + tried[i].first + ": " + tried[i].second;
    }
    if (mixed) {
        const std::vector<std::string> edges = mixed_connectivity_edges(body);
        if (!edges.empty()) {
            d += " — " + std::to_string(edges.size()) +
                 " edge(s) run from a crease into tangency: " + edges.front();
            if (edges.size() > 1) d += " (and " + std::to_string(edges.size() - 1) + " more)";
        }
    }
    return d;
}

// CLOSED-shell builder (no faces removed = a fully-sealed hollow part, e.g. a hollow
// ball / a sealed enclosure). MakeThickSolidByJoin with an EMPTY ClosingFaces list
// no-ops on a closed solid: with no mouth to open, the offset shell coincides with the
// original skin and OCCT hands back the ORIGINAL solid (a 40^3 box shells to a 36^3
// SOLID block, not a hollow wall). So the closed case takes a different, robust path:
// build an offset *solid* (the whole body grown/shrunk by `t` via
// BRepOffsetAPI_MakeOffsetShape::PerformByJoin on the solid) and BOOLEAN-CUT it against
// the original to leave a closed wall of thickness `t`:
//   inward  (SolidWorks default): inner = body offset by -t;  result = body - inner
//   outward                     : outer = body offset by +t;  result = outer - body
// For a 40^3 box at t=2: inner=36^3, result volume = 40^3 - 36^3 = 17344 (a sealed wall).
// Returns a null shape on kernel failure so the caller's guards report it cleanly.
TopoDS_Shape closed_shell_offset_cut(const TopoDS_Shape& body, double t, bool outward,
                                     std::string* diag) {
    const Standard_Real off = outward ? t : -t;
    std::vector<std::pair<std::string, std::string>> tried;
    bool mixed = false;
    for (const ShellStrat& s : kShellStrats) {
        const std::string label = shell_strat_label(s);
        try {
            BRepOffsetAPI_MakeOffsetShape mk;
            mk.PerformByJoin(body, off, s.tol, BRepOffset_Skin, s.inter,
                             Standard_False, s.join);
            mk.Build();
            if (!mk.IsDone() || mk.Shape().IsNull()) {
                const BRepOffset_Error err = mk.MakeOffset().Error();
                mixed = mixed || err == BRepOffset_MixedConnectivity;
                tried.emplace_back(label, offset_error_text(err));
                continue;
            }
            TopoDS_Shape offset_solid = mk.Shape();
            // Wall = the larger solid minus the smaller. Inward: body - inner(smaller).
            // Outward: outer(larger) - body. The cut yields the closed wall of thickness t.
            const TopoDS_Shape& big   = outward ? offset_solid : body;
            const TopoDS_Shape& small = outward ? body         : offset_solid;
            BRepAlgoAPI_Cut cut(big, small);
            cut.Build();
            if (!cut.IsDone() || cut.Shape().IsNull()) {
                tried.emplace_back(label, "the wall boolean (cut) failed");
                continue;
            }
            TopoDS_Shape wall = cut.Shape();
            // A genuine sealed wall has positive, REDUCED-from-`big` volume. Reject a
            // degenerate cut (empty / coincident offset that left the whole big solid).
            const double vbig = solid_volume(big), vwall = solid_volume(wall);
            const double eps = 1e-6 * std::max(1.0, std::abs(vbig));
            if (!(vwall > eps && vwall < vbig - eps)) {
                tried.emplace_back(label, "the offset left no wall (volume unchanged)");
                continue;
            }
            const std::string why = shell_brep_rejection(wall);
            if (!why.empty()) { tried.emplace_back(label, why); continue; }
            return wall;
        } catch (const Standard_Failure& f) {
            tried.emplace_back(label, std::string("OCCT threw ") + f.GetMessageString());
        }
    }
    if (diag) *diag = shell_diag(tried, mixed, body);
    return TopoDS_Shape();
}

}  // namespace

TopoDS_Shape shell_solid(const TopoDS_Shape& body, double t, bool outward,
                         const std::vector<TopoDS_Face>& remove, std::string* diag) {
    if (diag) diag->clear();
    if (body.IsNull() || !(t > 0.0)) return TopoDS_Shape();
    // CLOSED shell (no mouth opened): MakeThickSolidByJoin no-ops with an empty face
    // list, so route to the offset-solid + boolean-cut path that builds a real sealed
    // wall (a hollow ball / sealed box).
    if (remove.empty()) return closed_shell_offset_cut(body, t, outward, diag);
    // SolidWorks default = inward wall (offset NEGATIVE, material kept inside the
    // original surface); `outward` flips to +t. Mode = Skin (offset the skin).
    const Standard_Real offset = outward ? t : -t;
    const double vol_in = solid_volume(body);
    const double eps = 1e-6 * std::max(1.0, std::abs(vol_in));
    TopTools_ListOfShape faces_to_remove;
    for (const TopoDS_Face& f : remove) faces_to_remove.Append(f);
    // Walk the WHOLE ladder until a strategy's result validates. Returning the first
    // non-null result used to ship whatever the Arc join made — on a freeform skin that
    // could be an open or BRepCheck-invalid body while a later rung would have built a
    // sound one — and the caller then refused the whole feature.
    std::vector<std::pair<std::string, std::string>> tried;
    bool mixed = false;
    TopoDS_Shape unchanged;  // a valid no-op result (thickness too large) for the caller
    for (const ShellStrat& s : kShellStrats) {
        const std::string label = shell_strat_label(s);
        try {
            BRepOffsetAPI_MakeThickSolid mk;
            mk.MakeThickSolidByJoin(body, faces_to_remove, offset, s.tol,
                                    BRepOffset_Skin, s.inter, Standard_False, s.join);
            mk.Build();
            if (!mk.IsDone() || mk.Shape().IsNull()) {
                const BRepOffset_Error err = mk.MakeOffset().Error();
                mixed = mixed || err == BRepOffset_MixedConnectivity;
                tried.emplace_back(label, offset_error_text(err));
                continue;
            }
            const TopoDS_Shape out = mk.Shape();
            const std::string why = shell_brep_rejection(out);
            if (!why.empty()) { tried.emplace_back(label, why); continue; }
            const double vol_out = solid_volume(out);
            if (std::abs(vol_out - vol_in) <= eps) {
                if (unchanged.IsNull()) unchanged = out;
                tried.emplace_back(label, "the offset returned the body unchanged");
                continue;
            }
            if (!(vol_out > eps) || (outward ? vol_out < vol_in : vol_out > vol_in)) {
                tried.emplace_back(label, "the result's volume moved the wrong way");
                continue;
            }
            return out;
        } catch (const Standard_Failure& f) {
            tried.emplace_back(label, std::string("OCCT threw ") + f.GetMessageString());
        }
    }
    // Every offset strategy failed. An inward shell of a tube-like body (planar opened ends
    // across one axis) still has the sectional builder, which never offsets a face and so is
    // immune to BRepOffset_MixedConnectivity (a rectangle-to-circle loft's corner seams).
    if (!outward) {
        std::string why;
        TopoDS_Shape sect = shell_by_sections(body, t, remove, &why);
        if (!sect.IsNull()) return sect;
        tried.emplace_back("sectional", why);
    }
    if (diag) *diag = shell_diag(tried, mixed, body);
    return unchanged;
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
                         const Eigen::Vector3d& pull, double angle_rad, std::string* diag) {
    if (diag) diag->clear();
    if (body.IsNull() || faces.empty()) {
        if (diag) *diag = "no body or no faces to draft";
        return TopoDS_Shape();
    }
    if (!(neutral_n.norm() > 1e-12) || !(pull.norm() > 1e-12)) {
        if (diag) *diag = "degenerate neutral-plane normal or pull direction";
        return TopoDS_Shape();
    }
    const gp_Pln neutral(gp_Pnt(neutral_pt.x(), neutral_pt.y(), neutral_pt.z()),
                         gp_Dir(neutral_n.x(), neutral_n.y(), neutral_n.z()));
    const gp_Dir pull_dir(pull.x(), pull.y(), pull.z());
    // PRE-FILTER the selection to the faces OCCT can actually taper about this neutral
    // plane. A planar face that is roughly PARALLEL to the neutral plane — e.g. the
    // top/bottom cap of a box about a horizontal plane — has no intersection line to
    // pivot about and cannot be drafted; left in the builder it makes Build() THROW a
    // Standard_Failure (which, not being a std::exception, surfaced as the opaque
    // "unknown error during feature compile"). Dropping such caps UP FRONT (never adding
    // them, so the builder state stays clean) lets a mixed selection like {"of":"face"}
    // (all 6 box faces) still draft the 4 side walls — the SolidWorks behaviour.
    const gp_Dir neutral_dir(neutral_n.x(), neutral_n.y(), neutral_n.z());
    std::vector<TopoDS_Face> draftable;
    int rejected = 0;
    for (const TopoDS_Face& f : faces) {
        bool ok = true;
        try {
            BRepAdaptor_Surface surf(f);
            if (surf.GetType() == GeomAbs_Plane) {
                // |n . neutral| ~ 1  =>  the face is parallel to the neutral plane.
                const double d = std::abs(surf.Plane().Axis().Direction().Dot(neutral_dir));
                if (d > 0.999) ok = false;
            }
        } catch (const Standard_Failure&) { ok = true; }  // unknown surface: let OCCT try
        if (ok) draftable.push_back(f);
        else ++rejected;
    }
    if (draftable.empty()) {
        if (diag)
            *diag = "none of the selected faces can be tapered about this neutral plane "
                    "— a draftable face must be roughly PERPENDICULAR to the plane "
                    "(you cannot draft a face that is parallel to it, e.g. the top/bottom "
                    "of a box about a horizontal plane). Select the SIDE walls, or move "
                    "the neutral plane so the faces meet it.";
        return TopoDS_Shape();
    }

    BRepOffsetAPI_DraftAngle mk(body);
    try {
        for (const TopoDS_Face& f : draftable) mk.Add(f, pull_dir, angle_rad, neutral);
        mk.Build();
    } catch (const Standard_Failure& e) {
        if (diag) {
            const char* m = e.GetMessageString();
            *diag = std::string("the kernel could not taper the selected face(s) about "
                                "this neutral plane") +
                    (m && *m ? std::string(" (") + m + ")" : std::string()) +
                    " — check that the pull direction is roughly normal to the neutral "
                    "plane and the faces meet it.";
        }
        return TopoDS_Shape();
    }
    if (!mk.IsDone()) {
        if (diag)
            *diag = "the draft did not complete — check that the neutral plane intersects "
                    "the selected faces and the pull direction is roughly normal to it.";
        return TopoDS_Shape();
    }
    if (diag && rejected > 0)
        *diag = std::to_string(draftable.size()) + " face(s) drafted; " +
                std::to_string(rejected) + " skipped (parallel to the neutral plane)";
    return mk.Shape();
}

}  // namespace geom
