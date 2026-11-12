#include "geom/dress.h"
#include "geom/blend_sweep.h"
#include "geom/booleans.h"
#include "geom/fillet_runoff.h"
#include "geom/op_history.h"
#include "geom/shell_sections.h"
#include "util/process_isolation.h"

#include <algorithm>
#include <array>
#include <map>
#include <cmath>
#include <functional>
#include <chrono>
#include <cstdio>
#include <iomanip>
#include <limits>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <Bnd_Box.hxx>
#include <BOPAlgo_CheckerSI.hxx>
#include <BOPDS_DS.hxx>
#include <BOPDS_MapOfPair.hxx>
#include <BRepBndLib.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Curve2d.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepBuilderAPI_Copy.hxx>
#include <BRepBuilderAPI_FindPlane.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepTools_History.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepClass_FaceClassifier.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRepOffset_Error.hxx>
#include <BRepOffset_MakeOffset.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRep_Tool.hxx>
#include <BRep_Builder.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepFilletAPI_MakeChamfer.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepOffsetAPI_DraftAngle.hxx>
#include <BRepOffsetAPI_MakeOffsetShape.hxx>
#include <BRepOffsetAPI_MakeThickSolid.hxx>
#include <BRepOffset_Mode.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepPrimAPI_MakeRevol.hxx>
#include <BRepLib.hxx>
#include <BRepGProp.hxx>
#include <ChFi2d_FilletAlgo.hxx>
#include <GCPnts_AbscissaPoint.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GProp_GProps.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_JoinType.hxx>
#include <GeomAbs_Shape.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <GeomLProp_SLProps.hxx>
#include <Geom_BSplineCurve.hxx>
#include <Geom_Plane.hxx>
#include <Geom_Surface.hxx>
#include <Geom_SurfaceOfLinearExtrusion.hxx>
#include <Message_ProgressIndicator.hxx>
#include <Precision.hxx>
#include <ShapeAnalysis_FreeBounds.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_HSequenceOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <Standard_Failure.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Circ.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Lin.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

std::vector<EdgeBoundaryUses> edge_boundary_uses(const TopoDS_Shape& shape,
                                                 const TopTools_IndexedMapOfShape& edges) {
    std::vector<EdgeBoundaryUses> uses(static_cast<std::size_t>(edges.Extent()) + 1);
    for (TopExp_Explorer faces(shape, TopAbs_FACE); faces.More(); faces.Next()) {
        for (TopExp_Explorer it(faces.Current(), TopAbs_EDGE); it.More(); it.Next()) {
            const TopoDS_Shape& edge = it.Current();
            EdgeBoundaryUses& use = uses[static_cast<std::size_t>(edges.FindIndex(edge))];
            if (edge.Orientation() == TopAbs_FORWARD || edge.Orientation() == TopAbs_REVERSED)
                ++use.boundary;
            else
                use.embedded = true;
        }
    }
    return uses;
}

// An OCCT blend can report IsDone despite leaving an open B-rep, so every blend result is
// closure-checked (all-edge fillets and best-effort chamfers alike).
bool is_watertight(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return false;
    try {
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(shape, TopAbs_EDGE, edges);
        if (edges.Extent() == 0) return false;
        const std::vector<EdgeBoundaryUses> uses = edge_boundary_uses(shape, edges);
        for (int ei = 1; ei <= edges.Extent(); ++ei)
            if (!BRep_Tool::Degenerated(TopoDS::Edge(edges(ei))) &&
                uses[static_cast<std::size_t>(ei)].is_free())
                return false;
        return true;
    } catch (const Standard_Failure&) {
        return false;
    }
}

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

// An isolated legacy blend probe (chamfers and best-effort edge dropping) has at
// most five seconds. Fillet probes are bounded by their search budget instead.
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

// The largest vertex or edge tolerance of `shape`, and where it is.
double max_tolerance(const TopoDS_Shape& shape, gp_Pnt* where = nullptr) {
    double worst = 0.0;
    for (TopExp_Explorer it(shape, TopAbs_VERTEX); it.More(); it.Next()) {
        const TopoDS_Vertex vertex = TopoDS::Vertex(it.Current());
        const double tolerance = BRep_Tool::Tolerance(vertex);
        if (tolerance > worst) {
            worst = tolerance;
            if (where) *where = BRep_Tool::Pnt(vertex);
        }
    }
    for (TopExp_Explorer it(shape, TopAbs_EDGE); it.More(); it.Next())
        worst = std::max(worst, BRep_Tool::Tolerance(TopoDS::Edge(it.Current())));
    return worst;
}

// A kernel blend can report success after "closing" a corner it could not build by
// widening a vertex or edge tolerance to the size of the gap (tens of millimetres at a
// hook's tip). No real blend of `size` needs more than a twentieth of it (or a micron).
bool blend_tolerance_blown(double source_tolerance, double result_tolerance, double size) {
    return result_tolerance > std::max({10.0 * source_tolerance, 0.05 * size, 1e-3});
}

// Chamfers retain their established best-effort subset behavior. Fillets use a
// separate all-selected-edge builder below and never enter this fallback. When the
// kernel cannot build every edge, prismatic edges are first chamfered exactly as swept
// cross-sections (blend_sweep.h) and the kernel builds the rest on that body.
template <class Builder, class AddFn>
TopoDS_Shape blend_drop_failing(
    const TopoDS_Shape& shape, const std::vector<TopoDS_Edge>& good, AddFn add, int* n_dropped,
    double size, const std::function<SweepBlendProfile(const TopoDS_Edge&)>& sweep_profile) {
    if (n_dropped) *n_dropped = 0;
    if (good.empty()) return TopoDS_Shape();
    const double source_tolerance = max_tolerance(shape);

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
                if (!is_watertight(s) ||
                    blend_tolerance_blown(source_tolerance, max_tolerance(s), size) ||
                    !BRepCheck_Analyzer(s).IsValid())
                    return {};
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

    if (sweep_profile) {
        TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
        std::size_t previous = 0;
        for (const SweepBlendScope scope : {SweepBlendScope::isolated, SweepBlendScope::all}) {
            const std::vector<std::size_t> chosen = sweep_blend_choose(good, edge_faces, scope);
            if (chosen.size() <= previous) continue;
            previous = chosen.size();
            std::vector<TopoDS_Edge> swept, unswept;
            std::vector<SweepBlendProfile> profiles;
            for (std::size_t i = 0, next = 0; i < good.size(); ++i) {
                if (next < chosen.size() && chosen[next] == i) {
                    swept.push_back(good[i]);
                    profiles.push_back(sweep_profile(good[i]));
                    ++next;
                } else {
                    unswept.push_back(good[i]);
                }
            }
            std::string* sidecar = recording ? &prov : nullptr;
            auto result = chudware::util::isolate_shape_build([&]() -> TopoDS_Shape {
                try {
                    OpHistoryScope history(recording);
                    const SweepBlendRun run = sweep_blend_edges(
                        shape, swept, [&](std::size_t i) { return profiles[i]; }, unswept);
                    if (run.shape.IsNull()) return {};
                    TopoDS_Shape s = run.shape;
                    if (!run.remaining.empty()) {
                        Builder mk(run.shape);
                        for (const TopoDS_Edge& e : run.remaining) add(mk, e);
                        mk.Build();
                        if (!mk.IsDone()) return {};
                        s = mk.Shape();
                        if (blend_tolerance_blown(source_tolerance, max_tolerance(s), size)) return {};
                        if (recording) {
                            TopTools_ListOfShape args;
                            args.Append(run.shape);
                            op_history_record(new BRepTools_History(args, mk));
                        }
                    }
                    if (!is_watertight(s) || !BRepCheck_Analyzer(s).IsValid()) return {};
                    if (sidecar) *sidecar = op_provenance_encode(shape, s, history.histories());
                    return s;
                } catch (const Standard_Failure&) {
                    return {};
                }
            }, kBlendTimeout, sidecar);
            if (result.ok && !result.shape.IsNull()) return finish(result.shape);
        }
    }

    // Bisect over the edge list instead of O(E²) one-edge-at-a-time rebuilds.
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

// --- contact-edge (joining) fillets ---
// Two solids that meet only along a line share that edge: it has four faces, two per solid,
// and BRepFilletAPI (two faces per edge) cannot blend it. Around such an edge the normal plane
// alternates solid A, an empty corner, solid B, an empty corner. Each empty corner narrower than
// 180 degrees gets the rolling-ball fillet's cross-section (the region between the two faces and
// the tangent arc), swept along the edge, and the sweep is fused in. The faces must run straight
// away from the edge so one cross-section is exact along its whole length: a straight edge is
// swept by a prism, a circular edge by a revolution about the circle's axis.

struct ContactFace {
    TopoDS_Face face;
    int solid = -1;      // index into the edge's two solids
    gp_Vec dir;          // unit direction into the face, normal to the edge, at the edge midpoint
    double angle = 0.0;  // of `dir` in the edge's normal-plane frame
};

// Projects `p` onto `face`'s surface: kOnFace when it lies within `tol` of the surface and
// inside (or on the boundary of) the face.
enum class FacePoint { kOnFace, kOffSurface, kOutside };

FacePoint classify_on_face(const TopoDS_Face& face, const gp_Pnt& p, double tol) {
    GeomAPI_ProjectPointOnSurf proj(p, BRep_Tool::Surface(face));
    if (proj.NbPoints() == 0 || proj.LowerDistance() > tol) return FacePoint::kOffSurface;
    Standard_Real u = 0, v = 0;
    proj.LowerDistanceParameters(u, v);
    BRepClass_FaceClassifier cls(face, gp_Pnt2d(u, v), tol);
    const TopAbs_State st = cls.State();
    return st == TopAbs_IN || st == TopAbs_ON ? FacePoint::kOnFace : FacePoint::kOutside;
}

enum class ContactFailure { none, size, topology, construction };

// These are interior points, not topology-history IDs: a Boolean union can
// rebuild a solid without reporting it as a Modified image of either input.
struct ContactEvidence {
    std::array<gp_Pnt, 2> source_points;
    std::array<bool, 2> has_source{false, false};
    // Four sectors include at least one interior sector for each source, so
    // at most two sectors are swept, with three witnesses per sweep.
    std::array<gp_Pnt, 6> material_points;
    std::size_t material_count = 0;
};

struct ContactMaterial {
    std::array<TopoDS_Shape, 2> sweeps;
    std::size_t count = 0;
};

// The fillet material for every concave corner around contact edge `e`.
// A size failure is distinguished from unsupported topology and construction
// failures; none of them can be returned as a successful partial join.
ContactMaterial contact_fillet_material(
        const TopoDS_Edge& e, double r,
        const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
        const TopTools_IndexedDataMapOfShapeListOfShape& face_solids,
        std::string& why, ContactFailure& failure, ContactEvidence& evidence) {
    failure = ContactFailure::topology;
    TopTools_IndexedMapOfShape solids;
    std::array<ContactFace, 4> faces;
    std::size_t face_count = 0;
    TopTools_IndexedMapOfShape seen;
    for (TopTools_ListIteratorOfListOfShape it(edge_faces.FindFromKey(e)); it.More(); it.Next()) {
        if (seen.Contains(it.Value())) continue;
        seen.Add(it.Value());
        if (!face_solids.Contains(it.Value()) || face_solids.FindFromKey(it.Value()).Extent() != 1) {
            why = "the shared edge must border exactly two solid bodies";
            return {};
        }
        if (face_count == faces.size()) {
            why = "the shared edge must border exactly two solid bodies, two faces each";
            return {};
        }
        ContactFace& cf = faces[face_count++];
        cf.face = TopoDS::Face(it.Value());
        cf.solid = solids.Add(face_solids.FindFromKey(it.Value()).First()) - 1;
    }
    if (face_count != faces.size() || solids.Extent() != 2 ||
        std::count_if(faces.begin(), faces.end(), [](const ContactFace& f) { return f.solid == 0; }) != 2) {
        why = "the shared edge must border exactly two solid bodies, two faces each";
        return {};
    }

    const BRepAdaptor_Curve curve(e);
    const GeomAbs_CurveType kind = curve.GetType();
    if (kind != GeomAbs_Line && kind != GeomAbs_Circle) {
        why = "a joining fillet needs a straight or circular shared edge";
        return {};
    }
    const double u0 = curve.FirstParameter(), u1 = curve.LastParameter();
    const double um = 0.5 * (u0 + u1);
    const double length = GCPnts_AbscissaPoint::Length(curve);
    if (!(length > Precision::Confusion())) {
        why = "the shared edge has no length";
        return {};
    }
    gp_Pnt pm;
    gp_Vec tm;
    curve.D1(um, pm, tm);
    if (tm.Magnitude() < 1e-12) {
        why = "the shared edge has no tangent";
        return {};
    }
    tm.Normalize();
    // Transport a midpoint direction to parameter `u`: identity along a line, the circle's
    // rotation along an arc (faces of a revolved corner keep their angle to the radial plane).
    auto transport = [&](const gp_Vec& v, double u) {
        if (kind == GeomAbs_Line) return v;
        gp_Trsf rot;
        rot.SetRotation(curve.Circle().Axis(), u - um);
        return v.Transformed(rot);
    };

    // Choose a local in-face probe, shrinking it when a long edge or a large
    // requested radius would put the sample beyond a narrow adjacent face.
    double face_tol = 1e-6;
    for (const ContactFace& cf : faces) face_tol = std::max(face_tol, BRep_Tool::Tolerance(cf.face));
    const double minimum_probe = 8.0 * face_tol;
    double probe = std::max(1e-3 * std::min(r, length), minimum_probe);
    for (ContactFace& cf : faces) {
        const Handle(Geom_Surface) surf = BRep_Tool::Surface(cf.face);
        GeomAPI_ProjectPointOnSurf proj(pm, surf);
        if (proj.NbPoints() == 0) {
            why = "could not resolve the faces around the shared edge";
            return {};
        }
        Standard_Real su = 0, sv = 0;
        proj.LowerDistanceParameters(su, sv);
        GeomLProp_SLProps props(surf, su, sv, 1, Precision::Confusion());
        if (!props.IsNormalDefined()) {
            why = "could not resolve the faces around the shared edge";
            return {};
        }
        gp_Vec dir = tm.Crossed(gp_Vec(props.Normal()));
        if (dir.Magnitude() < 1e-9) {
            why = "a face is tangent to the shared edge's direction";
            return {};
        }
        dir.Normalize();
        bool found = false;
        for (int step = 0; step < 48; ++step) {
            const double tol = std::max(1e-6, BRep_Tool::Tolerance(cf.face)) + 1e-3 * probe;
            if (classify_on_face(cf.face, pm.Translated(dir * probe), tol) == FacePoint::kOnFace) {
                found = true;
                break;
            }
            if (classify_on_face(cf.face, pm.Translated(dir * -probe), tol) == FacePoint::kOnFace) {
                dir.Reverse();
                found = true;
                break;
            }
            if (probe <= minimum_probe) break;
            probe = std::max(minimum_probe, probe * 0.5);
        }
        if (!found) {
            // This can depend on the requested size; it is not proof that the
            // contact's curve or face topology is unsupported.
            failure = ContactFailure::construction;
            why = "could not resolve directions on the faces around the shared edge";
            return {};
        }
        cf.dir = dir;
    }
    const gp_Vec xd = faces[0].dir;
    const gp_Vec yd = tm.Crossed(xd);
    for (ContactFace& cf : faces) {
        double a = std::atan2(cf.dir.Dot(yd), cf.dir.Dot(xd));
        cf.angle = a < 0 ? a + 2 * M_PI : a;
    }
    std::sort(faces.begin(), faces.end(),
              [](const ContactFace& a, const ContactFace& b) { return a.angle < b.angle; });

    std::array<double, 4> spans;
    std::array<gp_Vec, 4> bisectors;
    for (size_t k = 0; k < faces.size(); ++k) {
        double span = faces[(k + 1) % faces.size()].angle - faces[k].angle;
        if (span <= 0) span += 2 * M_PI;
        spans[k] = span;
        const double mid = faces[k].angle + 0.5 * span;
        bisectors[k] = xd * std::cos(mid) + yd * std::sin(mid);
    }
    BRepClass3d_SolidClassifier source_a(solids(1)), source_b(solids(2));
    const std::array<BRepClass3d_SolidClassifier*, 2> classifiers{&source_a, &source_b};
    std::array<Bnd_Box, 2> source_bounds;
    for (int s = 0; s < 2; ++s)
        BRepBndLib::Add(solids(s + 1), source_bounds[s], Standard_False);
    // A solid classifier can report IN outside a very slender solid. A
    // conservative, tolerance-expanded bound is definitive in that case.
    auto source_state = [&](int s, const gp_Pnt& point) {
        if (!source_bounds[s].IsVoid() && source_bounds[s].IsOut(point)) return TopAbs_OUT;
        classifiers[s]->Perform(point, Precision::Confusion());
        return classifiers[s]->State();
    };
    bool overlap_checked = false, overlapping_volume = false;
    auto confirmed_overlap = [&]() {
        if (overlap_checked) return overlapping_volume;
        overlap_checked = true;
        try {
            TopTools_ListOfShape args, tools;
            args.Append(solids(1));
            tools.Append(solids(2));
            BRepAlgoAPI_Common common;
            common.SetArguments(args);
            common.SetTools(tools);
            common.SetNonDestructive(Standard_True);
            common.Build();
            if (!common.IsDone() || common.Shape().IsNull()) return false;
            for (TopExp_Explorer ex(common.Shape(), TopAbs_SOLID); ex.More(); ex.Next()) {
                if (!BRepCheck_Analyzer(ex.Current()).IsValid()) continue;
                Bnd_Box bounds;
                BRepBndLib::AddOptimal(ex.Current(), bounds, Standard_False, Standard_False);
                const double span_tolerance = 2.0 * minimum_probe;
                if (bounds.IsVoid() || bounds.IsOpen() ||
                    bounds.IsXThin(span_tolerance) || bounds.IsYThin(span_tolerance) ||
                    bounds.IsZThin(span_tolerance))
                    continue;
                GProp_GProps volume;
                BRepGProp::VolumeProperties(ex.Current(), volume);
                if (std::isfinite(volume.Mass()) &&
                    volume.Mass() > minimum_probe * minimum_probe * minimum_probe) {
                    overlapping_volume = true;
                    break;
                }
            }
        } catch (const Standard_Failure&) {
            // An inconclusive Boolean is not evidence of unsupported topology.
        }
        return overlapping_volume;
    };
    std::array<bool, 4> filled_corners{};
    bool resolved = false;
    // The in-face directions alone do not bound a solid's interior: a narrow
    // triangular face may terminate before its sector bisector. Shrink the
    // sample until both sources and all empty corners are unambiguous.
    for (int step = 0; step < 48; ++step) {
        evidence.has_source = {false, false};
        bool ambiguous = false;
        for (size_t k = 0; k < faces.size(); ++k) {
            const gp_Pnt sample = pm.Translated(bisectors[k] * probe);
            int owner = -1;
            bool on_boundary = false;
            for (int s = 0; s < 2; ++s) {
                const TopAbs_State state = source_state(s, sample);
                if (state == TopAbs_IN) {
                    if (owner >= 0) {
                        if (confirmed_overlap()) {
                            why = "the bodies overlap in volume at the shared edge";
                            return {};
                        }
                        // Two IN classifications without independent volume
                        // evidence are ambiguous, not unsupported topology.
                        owner = -1;
                        on_boundary = true;
                        break;
                    }
                    owner = s;
                } else if (state != TopAbs_OUT) {
                    on_boundary = true;
                }
            }
            filled_corners[k] = owner >= 0;
            if (owner >= 0) {
                evidence.source_points[owner] = sample;
                evidence.has_source[owner] = true;
            } else if (on_boundary) {
                ambiguous = true;
            }
        }
        if (!ambiguous && evidence.has_source[0] && evidence.has_source[1]) {
            resolved = true;
            break;
        }
        if (probe <= minimum_probe) break;
        probe = std::max(minimum_probe, probe * 0.5);
    }
    if (!resolved) {
        failure = ContactFailure::construction;
        why = "could not classify both bodies and the empty corners at the shared edge";
        return {};
    }

    ContactMaterial material;
    const gp_Pnt p0 = curve.Value(u0);
    for (size_t k = 0; k < faces.size(); ++k) {
        const ContactFace& fa = faces[k];
        const ContactFace& fb = faces[(k + 1) % faces.size()];
        const double span = spans[k];
        const gp_Vec& bis = bisectors[k];
        if (filled_corners[k] || span >= M_PI - 1e-6) continue;
        if (fa.solid == fb.solid) {
            failure = ContactFailure::topology;
            why = "a corner at the shared edge is bounded by one body on both sides";
            return {};
        }
        const double tangent = r / std::tan(0.5 * span);   // edge to tangent line, along each face
        const double center = r / std::sin(0.5 * span);    // edge to arc center, along the bisector
        // The cross-section is exact only if each face contains the straight leg from the edge
        // to the tangent line everywhere along the edge.
        for (double frac : {0.0, 0.25, 0.5, 0.75, 1.0}) {
            const double u = u0 + frac * (u1 - u0);
            const gp_Pnt p = curve.Value(u);
            for (const ContactFace* f : {&fa, &fb}) {
                const gp_Vec dir = transport(f->dir, u);
                const double tol = std::max(1e-6, BRep_Tool::Tolerance(f->face)) + 1e-7 * r;
                for (double leg : {0.5, 1.0}) {
                    const FacePoint fp = classify_on_face(f->face, p.Translated(dir * (leg * tangent)), tol);
                    if (fp == FacePoint::kOffSurface) {
                        failure = ContactFailure::topology;
                        why = "the faces at the shared edge curve away from it; a joining fillet "
                              "needs faces that run straight from the edge (planes, cylinders, cones)";
                        return {};
                    }
                    if (fp == FacePoint::kOutside) {
                        failure = ContactFailure::size;
                        char buf[160];
                        std::snprintf(buf, sizeof(buf),
                                      "radius %.4g mm is too large for the faces at the shared edge",
                                      r);
                        why = buf;
                        return {};
                    }
                }
            }
        }
        // Cross-section in the normal plane at the edge start: edge point, tangent line on face
        // a, the arc nearest the edge, tangent line on face b.
        const gp_Vec da = transport(fa.dir, u0), db = transport(fb.dir, u0);
        const gp_Vec dbis = transport(bis, u0);
        const gp_Pnt ta = p0.Translated(da * tangent), tb = p0.Translated(db * tangent);
        const gp_Pnt apex = p0.Translated(dbis * (center - r));
        failure = ContactFailure::construction;
        try {
            GC_MakeArcOfCircle arc(ta, apex, tb);
            if (!arc.IsDone()) {
                why = "could not build the fillet cross-section";
                return {};
            }
            BRepBuilderAPI_MakeWire wire(BRepBuilderAPI_MakeEdge(p0, ta).Edge(),
                                         BRepBuilderAPI_MakeEdge(arc.Value()).Edge(),
                                         BRepBuilderAPI_MakeEdge(tb, p0).Edge());
            BRepBuilderAPI_MakeFace section(wire.Wire(), Standard_True);
            if (!section.IsDone()) {
                why = "could not build the fillet cross-section";
                return {};
            }
            TopoDS_Shape swept;
            if (kind == GeomAbs_Line) {
                swept = BRepPrimAPI_MakePrism(section.Face(), gp_Vec(p0, curve.Value(u1))).Shape();
            } else {
                swept = BRepPrimAPI_MakeRevol(section.Face(), curve.Circle().Axis(), u1 - u0).Shape();
            }
            GProp_GProps props;
            BRepGProp::VolumeProperties(swept, props);
            if (swept.IsNull() || !BRepCheck_Analyzer(swept).IsValid() || !(props.Mass() > 0)) {
                why = "the fillet sweep along the shared edge is invalid";
                return {};
            }
            BRepClass3d_SolidClassifier witness_check(swept);
            for (double frac : {0.25, 0.5, 0.75}) {
                const double u = u0 + frac * (u1 - u0);
                const gp_Pnt witness = curve.Value(u).Translated(transport(bis, u) * (0.5 * (center - r)));
                witness_check.Perform(witness, Precision::Confusion());
                if (witness_check.State() != TopAbs_IN) {
                    why = "could not locate an interior point in a shared-edge fillet sweep";
                    return {};
                }
                for (int s = 0; s < 2; ++s) {
                    if (source_state(s, witness) != TopAbs_OUT) {
                        why = "the shared-edge fillet sweep does not add material in its corner";
                        return {};
                    }
                }
                evidence.material_points[evidence.material_count++] = witness;
            }
            material.sweeps[material.count++] = swept;
        } catch (const Standard_Failure&) {
            why = "could not build the fillet along the shared edge";
            return {};
        }
    }
    if (material.count == 0) why = "the shared edge has no concave corner to fillet";
    else failure = ContactFailure::none;
    return material;
}

int solid_count(const TopoDS_Shape& s) {
    int n = 0;
    for (TopExp_Explorer ex(s, TopAbs_SOLID); ex.More(); ex.Next()) ++n;
    return n;
}

// Prove each selected contact still joins its own two input solids and contains
// every constructed corner sweep. Solid-level Modified() history is not a
// reliable Boolean-union relation; classify known interior points instead.
bool contact_evidence_valid(const TopoDS_Shape& shape,
                            const std::vector<ContactEvidence>& contacts,
                            std::string& why) {
    TopTools_IndexedMapOfShape solids;
    TopExp::MapShapes(shape, TopAbs_SOLID, solids);
    for (std::size_t n = 0; n < contacts.size(); ++n) {
        const ContactEvidence& evidence = contacts[n];
        bool found = false;
        for (int i = 1; i <= solids.Extent() && !found; ++i) {
            BRepClass3d_SolidClassifier classifier(solids(i));
            auto inside = [&](const gp_Pnt& point) {
                classifier.Perform(point, Precision::Confusion());
                return classifier.State() == TopAbs_IN;
            };
            if (!inside(evidence.source_points[0]) || !inside(evidence.source_points[1]))
                continue;
            found = std::all_of(evidence.material_points.begin(),
                                evidence.material_points.begin() + evidence.material_count, inside);
        }
        if (!found) {
            why = "selected contact edge " + std::to_string(n + 1) +
                  ": its two source bodies and every fillet sweep are not in the same result solid";
            return false;
        }
    }
    return true;
}

// Joins `shape` across its contact edges (see above) and maps `manifold` (the other picks)
// onto the joined shape. `joined` counts contact edges that were filled; `failed` counts
// contact edges plus manifold picks that could not be carried over.
struct ContactJoin {
    TopoDS_Shape shape;
    std::vector<TopoDS_Edge> manifold;
    std::vector<ContactEvidence> evidence;
    int joined = 0;
    int failed = 0;
    std::string why;
    ContactFailure failure = ContactFailure::none;
};

ContactJoin join_contact_edges(const TopoDS_Shape& shape, double r,
                               const std::vector<TopoDS_Edge>& contact,
                               const std::vector<TopoDS_Edge>& manifold,
                               const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                               const TopTools_IndexedDataMapOfShapeListOfShape& face_solids) {
    ContactJoin out;
    out.shape = shape;
    out.manifold = manifold;
    TopTools_ListOfShape tools;
    out.evidence.reserve(contact.size());
    int filled_edges = 0;
    for (const TopoDS_Edge& e : contact) {
        std::string why;
        ContactFailure failure = ContactFailure::none;
        ContactEvidence evidence;
        const ContactMaterial material =
            contact_fillet_material(e, r, edge_faces, face_solids, why, failure, evidence);
        if (material.count == 0) {
            out.failed = static_cast<int>(contact.size());
            out.why = why;
            out.failure = failure;
            return out;
        }
        ++filled_edges;
        out.evidence.push_back(std::move(evidence));
        for (std::size_t i = 0; i < material.count; ++i) tools.Append(material.sweeps[i]);
    }
    if (tools.IsEmpty()) return out;

    auto fail_all = [&](const std::string& why) {
        out.failed += filled_edges;
        if (out.why.empty()) out.why = why;
        out.failure = ContactFailure::construction;
        return out;
    };
    try {
        TopTools_ListOfShape args;
        args.Append(shape);
        BRepAlgoAPI_Fuse fuse;
        fuse.SetArguments(args);
        fuse.SetTools(tools);
        fuse.SetNonDestructive(Standard_True);
        fuse.Build();
        if (!fuse.IsDone()) return fail_all("the kernel could not fuse the joining fillet");
        // Unify a topology copy: UnifySameDomain edits its input's edges in place, and the
        // NonDestructive fuse shares untouched TShapes with `shape` (see
        // geom::unify_same_domain).
        BRepBuilderAPI_Copy copier(fuse.Shape(), Standard_False, Standard_False);
        ShapeUpgrade_UnifySameDomain unify(copier.Shape(), Standard_True, Standard_True,
                                           Standard_False);
        keep_degenerate_patches(unify, copier.Shape());
        // Preserve every selected ordinary edge (and its endpoints) through
        // unification so a collinear neighbour cannot swallow a picked edge.
        if (!manifold.empty()) {
            TopTools_IndexedMapOfShape fused_edges;
            TopExp::MapShapes(fuse.Shape(), TopAbs_EDGE, fused_edges);
            const std::vector<Handle(BRepTools_History)> fuse_history{fuse.History()};
            for (const TopoDS_Edge& edge : manifold) {
                TopTools_ListOfShape images;
                op_history_images(edge, fuse_history, images);
                for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next()) {
                    if (it.Value().ShapeType() != TopAbs_EDGE || !fused_edges.Contains(it.Value()))
                        continue;
                    const TopoDS_Edge copy = TopoDS::Edge(copier.ModifiedShape(it.Value()));
                    unify.KeepShape(copy);
                    const TopoDS_Vertex first = TopExp::FirstVertex(copy);
                    const TopoDS_Vertex last = TopExp::LastVertex(copy);
                    if (!first.IsNull()) unify.KeepShape(first);
                    if (!last.IsNull()) unify.KeepShape(last);
                }
            }
        }
        unify.Build();
        const TopoDS_Shape joined = unify.Shape();
        if (joined.IsNull())
            return fail_all("the joining fillet unification returned a null shape");
        if (solid_count(joined) >= solid_count(shape))
            return fail_all("the joining fillet did not merge the selected bodies");
        if (!BRepCheck_Analyzer(joined).IsValid())
            return fail_all("the joining fillet produced an invalid solid");
        std::vector<Handle(BRepTools_History)> histories;
        if (!manifold.empty() || op_history_recording()) {
            TopTools_ListOfShape fused_args;
            fused_args.Append(fuse.Shape());
            histories = {fuse.History(), new BRepTools_History(fused_args, copier), unify.History()};
        }
        std::string contact_why;
        if (!contact_evidence_valid(joined, out.evidence, contact_why))
            return fail_all(contact_why);
        if (op_history_recording())
            for (const auto& h : histories) op_history_record(h);
        out.manifold.clear();
        if (!manifold.empty()) {
            TopTools_IndexedMapOfShape joined_edges, claimed_images;
            TopExp::MapShapes(joined, TopAbs_EDGE, joined_edges);
            for (const TopoDS_Edge& e : manifold) {
                GProp_GProps source_length;
                BRepGProp::LinearProperties(e, source_length);
                double mapped_length = 0.0;
                double length_tolerance = std::max(4.0 * Precision::Confusion(),
                                                   4.0 * BRep_Tool::Tolerance(e));
                TopTools_ListOfShape images;
                op_history_images(e, histories, images);
                bool kept = false;
                for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next()) {
                    if (it.Value().ShapeType() != TopAbs_EDGE || !joined_edges.Contains(it.Value()))
                        continue;
                    const TopoDS_Edge image = TopoDS::Edge(joined_edges(joined_edges.FindIndex(it.Value())));
                    if (claimed_images.Contains(image))
                        return fail_all("distinct ordinary selected edges mapped to the same edge after the contact join");
                    claimed_images.Add(image);
                    GProp_GProps image_length;
                    BRepGProp::LinearProperties(image, image_length);
                    mapped_length += image_length.Mass();
                    length_tolerance = std::max(length_tolerance, 4.0 * BRep_Tool::Tolerance(image));
                    out.manifold.push_back(image);
                    kept = true;
                }
                if (!kept) {
                    ++out.failed;
                    out.failure = ContactFailure::construction;
                    out.why = "an ordinary selected edge could not be mapped after the contact join";
                } else {
                    const double original_length = source_length.Mass();
                    length_tolerance = std::max(length_tolerance, std::abs(original_length) * 1e-7);
                    if (!std::isfinite(original_length) || !std::isfinite(mapped_length) ||
                        std::abs(mapped_length - original_length) > length_tolerance)
                        return fail_all("an ordinary selected edge was only partially retained by the contact join");
                }
            }
        }
        out.shape = joined;
        out.joined = filled_edges;
        return out;
    } catch (const Standard_Failure&) {
        return fail_all("the kernel could not fuse the joining fillet");
    }
}

// The public geometry API is also used without the document compiler's more
// detailed validator. In that case perform the same essential solid checks here.
class FilletValidityBudget : public Message_ProgressIndicator {
public:
    explicit FilletValidityBudget(std::chrono::steady_clock::time_point deadline)
        : deadline_(deadline) {}
    bool interrupted() const { return interrupted_; }
protected:
    Standard_Boolean UserBreak() override {
        if (std::chrono::steady_clock::now() < deadline_) return Standard_False;
        interrupted_ = true;
        return Standard_True;
    }
    void Show(const Message_ProgressScope&, const Standard_Boolean) override {}
private:
    std::chrono::steady_clock::time_point deadline_;
    bool interrupted_ = false;
};

std::string fillet_solid_rejection(const TopoDS_Shape& shape,
                                    std::chrono::steady_clock::time_point deadline) {
    if (shape.IsNull() || !is_watertight(shape) || !BRepCheck_Analyzer(shape).IsValid())
        return "fillet result is not a valid closed solid";
    TopTools_IndexedMapOfShape solids, faces;
    TopExp::MapShapes(shape, TopAbs_SOLID, solids);
    TopExp::MapShapes(shape, TopAbs_FACE, faces);
    if (solids.IsEmpty()) return "fillet result contains no solid";
    for (int i = 1; i <= faces.Extent(); ++i) {
        GProp_GProps area;
        BRepGProp::SurfaceProperties(faces(i), area);
        if (!std::isfinite(area.Mass()) || area.Mass() <= Precision::SquareConfusion())
            return "fillet result contains a zero-area face";
    }
    Handle(FilletValidityBudget) budget = new FilletValidityBudget(deadline);
    for (int i = 1; i <= solids.Extent(); ++i) {
        BOPAlgo_CheckerSI checker;
        TopTools_ListOfShape args;
        args.Append(solids(i));
        checker.SetArguments(args);
        checker.SetLevelOfCheck(9);
        checker.SetNonDestructive(Standard_True);
        checker.SetRunParallel(Standard_False);
        checker.Perform(budget->Start());
        if (budget->interrupted()) return "fillet self-intersection validation timed out";
        if (checker.HasErrors()) {
            std::ostringstream errors;
            checker.DumpErrors(errors);
            return "fillet self-intersection validation failed: " + errors.str();
        }
        if (checker.PDS() == nullptr)
            return "fillet self-intersection validation returned no data structure";
        const BOPDS_DS& ds = *checker.PDS();
        for (BOPDS_MapOfPair::Iterator it(ds.Interferences()); it.More(); it.Next()) {
            Standard_Integer a = 0, b = 0;
            it.Value().Indices(a, b);
            if (!ds.IsNewShape(a) && !ds.IsNewShape(b))
                return "fillet result has self-intersecting sub-shapes";
        }
    }
    return {};
}

struct FilletSelection {
    TopTools_IndexedMapOfShape body_edges;
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopTools_IndexedDataMapOfShapeListOfShape face_solids;
    std::vector<TopoDS_Edge> contact;
    std::vector<TopoDS_Edge> manifold;  // populated only for a mixed contact selection
    // Per authored endpoint, over just the selected edges and their adjacent faces.
    std::array<double, 2> endpoint_resolution{Precision::Confusion(), Precision::Confusion()};
};

FilletFailure classify_fillet_selection(const TopoDS_Shape& shape,
                                        const std::vector<TopoDS_Edge>& edges,
                                        bool variable, FilletSelection& selection,
                                        std::string& why) {
    if (shape.IsNull() || edges.empty()) {
        why = "fillet requires a body and at least one selected edge";
        return FilletFailure::invalid_selection;
    }
    TopTools_IndexedMapOfShape seen;
    if (selection.body_edges.IsEmpty())
        TopExp::MapShapes(shape, TopAbs_EDGE, selection.body_edges);
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, selection.edge_faces);
    TopExp::MapShapesAndAncestors(shape, TopAbs_FACE, TopAbs_SOLID, selection.face_solids);
    for (std::size_t i = 0; i < edges.size(); ++i) {
        const TopoDS_Edge& e = edges[i];
        auto reject = [&](FilletFailure kind, const char* message) {
            why = "selected edge " + std::to_string(i + 1) + ": " + message;
            return kind;
        };
        if (e.IsNull() || !selection.body_edges.Contains(e) || seen.Contains(e)) {
            return reject(FilletFailure::invalid_selection,
                          "edge is missing from the body or selected more than once");
        }
        seen.Add(e);
        if (BRep_Tool::Degenerated(e)) {
            return reject(FilletFailure::unsupported_topology, "degenerate edges cannot be filleted");
        }
        if (!is_blendable_edge(e, selection.edge_faces)) {
            return reject(FilletFailure::unsupported_topology, "seam edges cannot be filleted");
        }
        if (!selection.edge_faces.Contains(e)) {
            return reject(FilletFailure::unsupported_topology, "edge has no adjacent faces");
        }
        // Only the distinction between one, two and more ancestors is needed.
        // Avoid allocating two indexed maps for every ordinary selected edge.
        TopoDS_Shape first_face, second_face, first_solid, second_solid;
        int face_count = 0, solid_count = 0;
        auto add_distinct = [](const TopoDS_Shape& s, TopoDS_Shape& first,
                               TopoDS_Shape& second, int& count) {
            if (count == 0) { first = s; count = 1; }
            else if (!s.IsSame(first) && (count == 1 || !s.IsSame(second))) {
                if (count == 1) second = s;
                count = std::min(3, count + 1);
            }
        };
        for (TopTools_ListIteratorOfListOfShape f(selection.edge_faces.FindFromKey(e)); f.More(); f.Next()) {
            add_distinct(f.Value(), first_face, second_face, face_count);
            if (selection.face_solids.Contains(f.Value()))
                for (TopTools_ListIteratorOfListOfShape s(selection.face_solids.FindFromKey(f.Value()));
                     s.More(); s.Next())
                    add_distinct(s.Value(), first_solid, second_solid, solid_count);
        }
        if (solid_count > 1) {
            if (variable) {
                return reject(FilletFailure::unsupported_topology,
                              "variable-radius contact joins are unsupported");
            }
            selection.contact.push_back(e);
        } else if (solid_count != 1 || face_count != 2) {
            return reject(FilletFailure::unsupported_topology, "fillet requires a two-face solid edge");
        }
        if (variable) {
            const double common = std::max({Precision::Confusion(), BRep_Tool::Tolerance(e),
                BRep_Tool::Tolerance(TopoDS::Face(first_face)),
                BRep_Tool::Tolerance(TopoDS::Face(second_face))});
            const TopoDS_Vertex first = TopExp::FirstVertex(e, Standard_True);
            const TopoDS_Vertex last = TopExp::LastVertex(e, Standard_True);
            selection.endpoint_resolution[0] = std::max(selection.endpoint_resolution[0], common);
            selection.endpoint_resolution[1] = std::max(selection.endpoint_resolution[1], common);
            if (!first.IsNull())
                selection.endpoint_resolution[0] = std::max(selection.endpoint_resolution[0],
                                                            BRep_Tool::Tolerance(first));
            if (!last.IsNull())
                selection.endpoint_resolution[1] = std::max(selection.endpoint_resolution[1],
                                                            BRep_Tool::Tolerance(last));
        }
    }
    if (!selection.contact.empty()) {
        selection.manifold.reserve(edges.size() - selection.contact.size());
        for (const TopoDS_Edge& e : edges)
            if (std::none_of(selection.contact.begin(), selection.contact.end(),
                             [&](const TopoDS_Edge& c) { return c.IsSame(e); }))
                selection.manifold.push_back(e);
    }
    return FilletFailure::none;
}

// Only used after the builder rejects an edge: regularity metadata is not
// always present on imported shapes, so also inspect the adjacent face normals.
bool fillet_tangent_edge(const TopoDS_Edge& edge,
                         const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    if (!edge_faces.Contains(edge)) return false;
    const auto& adjacent = edge_faces.FindFromKey(edge);
    if (adjacent.Extent() != 2) return false;
    TopTools_ListIteratorOfListOfShape it(adjacent);
    const TopoDS_Face first = TopoDS::Face(it.Value());
    it.Next();
    const TopoDS_Face second = TopoDS::Face(it.Value());
    if (first.IsSame(second)) return false;
    try {
        if (BRep_Tool::HasContinuity(edge, first, second) &&
            BRep_Tool::Continuity(edge, first, second) >= GeomAbs_G1)
            return true;
        BRepAdaptor_Curve2d curves[] = {BRepAdaptor_Curve2d(edge, first), BRepAdaptor_Curve2d(edge, second)};
        BRepAdaptor_Surface surfaces[] = {BRepAdaptor_Surface(first), BRepAdaptor_Surface(second)};
        for (double fraction : {0.25, 0.5, 0.75}) {
            gp_Vec normals[2];
            for (int i = 0; i < 2; ++i) {
                const double t = curves[i].FirstParameter() +
                    fraction * (curves[i].LastParameter() - curves[i].FirstParameter());
                const gp_Pnt2d uv = curves[i].Value(t);
                gp_Pnt point;
                gp_Vec du, dv;
                surfaces[i].D1(uv.X(), uv.Y(), point, du, dv);
                normals[i] = du.Crossed(dv);
                if (normals[i].SquareMagnitude() <= gp::Resolution()) return false;
                normals[i].Normalize();
            }
            if (normals[0].Crossed(normals[1]).Magnitude() > 1e-6) return false;
        }
        return true;
    } catch (const Standard_Failure&) {
        return false;
    }
}

std::string fillet_no_contour_reason(
    const TopoDS_Edge& edge, const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    if (fillet_tangent_edge(edge, edge_faces))
        return "joins two tangent faces (a smooth seam, such as the edge of an existing fillet), "
               "so there is no corner to round. Pick a sharp edge instead, or edit the existing fillet";
    return "could not be added to a fillet contour; it may be a smooth seam or already part of "
           "another contour. Pick a sharp edge instead, or edit the existing fillet";
}

// Result-face tolerance applies to both ends. On straight edges, boundary
// edges and vertices apply to their nearer oriented source endpoint; curved
// edges and ambiguous vertices conservatively apply to both.
std::array<double, 2> variable_result_resolution(const TopoDS_Edge& source,
                                                  const TopoDS_Face& face) {
    const double face_tolerance = BRep_Tool::Tolerance(face);
    std::array<double, 2> resolution{face_tolerance, face_tolerance};
    const TopoDS_Vertex first = TopExp::FirstVertex(source, Standard_True);
    const TopoDS_Vertex last = TopExp::LastVertex(source, Standard_True);
    const bool distinct = !first.IsNull() && !last.IsNull() && !first.IsSame(last) &&
                          BRepAdaptor_Curve(source).GetType() == GeomAbs_Line;
    const gp_Pnt start = distinct ? BRep_Tool::Pnt(first) : gp_Pnt();
    const gp_Pnt end = distinct ? BRep_Tool::Pnt(last) : gp_Pnt();
    auto endpoint_mask = [&](const TopoDS_Vertex& vertex) {
        if (!distinct || vertex.IsNull()) return 3;
        const gp_Pnt point = BRep_Tool::Pnt(vertex);
        const double d1 = point.Distance(start), d2 = point.Distance(end);
        const double tol = std::max({Precision::Confusion(), BRep_Tool::Tolerance(first),
                                     BRep_Tool::Tolerance(last), BRep_Tool::Tolerance(vertex)});
        if (std::abs(d1 - d2) <= tol) return 3;
        return d1 < d2 ? 1 : 2;
    };
    auto add_tolerance = [&](int mask, double tolerance) {
        if (mask & 1) resolution[0] = std::max(resolution[0], tolerance);
        if (mask & 2) resolution[1] = std::max(resolution[1], tolerance);
    };
    TopTools_IndexedMapOfShape edges, vertices;
    TopExp::MapShapes(face, TopAbs_EDGE, edges);
    TopExp::MapShapes(face, TopAbs_VERTEX, vertices);
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        const int mask = endpoint_mask(TopExp::FirstVertex(edge)) |
                         endpoint_mask(TopExp::LastVertex(edge));
        add_tolerance(mask, BRep_Tool::Tolerance(edge));
    }
    for (int i = 1; i <= vertices.Extent(); ++i) {
        const TopoDS_Vertex vertex = TopoDS::Vertex(vertices(i));
        add_tolerance(endpoint_mask(vertex), BRep_Tool::Tolerance(vertex));
    }
    return resolution;
}

// OCCT's 3-D contour builder treats faces less than 0.1 radians apart as
// tangent even when they form a genuine C0 crease. A straight crease between
// surfaces extruded along it admits an exact planar fillet swept along the
// crease. Restrict this path to one solid and an unambiguous local removal.
struct ShallowFillet {
    TopoDS_Shape shape;
    std::vector<TopoDS_Edge> remaining;
    std::vector<TopoDS_Face> blend_faces;
    std::array<std::vector<TopoDS_Face>, 2> supports;
    gp_Pnt corner;
    gp_Dir axis;
    gp_Circ arc;
    double tolerance = 0.0;
    FilletFailure failure = FilletFailure::unsupported_topology;
    std::string reason;
};

TopoDS_Face section_face(const gp_Pln& plane, const std::vector<TopoDS_Edge>& edges,
                         double tolerance) {
    Handle(TopTools_HSequenceOfShape) input = new TopTools_HSequenceOfShape;
    Handle(TopTools_HSequenceOfShape) wires = new TopTools_HSequenceOfShape;
    for (const TopoDS_Edge& edge : edges) input->Append(edge);
    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(input, tolerance, Standard_False, wires);
    if (wires->Length() != 1 || wires->Value(1).ShapeType() != TopAbs_WIRE) return {};
    const TopoDS_Wire wire = TopoDS::Wire(wires->Value(1));
    if (!wire.Closed()) return {};
    BRepBuilderAPI_MakeFace maker(plane, wire, Standard_True);
    if (!maker.IsDone()) return {};
    return maker.Face();
}

// A section of a linear extrusion perpendicular to its direction is an exact
// translated copy of its basis curve. OCCT's generic section builder may
// approximate that same curve with a thousand degree-one segments, shifting
// the fillet's tangent. Replace only this transient section edge, never the
// authored support surface.
TopoDS_Edge exact_section_support(const TopoDS_Face& face, const TopoDS_Edge& section,
                                  const gp_Pnt& corner, const gp_Dir& axis, double tolerance) {
    if (BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane) return section;
    const Handle(Geom_SurfaceOfLinearExtrusion) extrusion =
        Handle(Geom_SurfaceOfLinearExtrusion)::DownCast(BRep_Tool::Surface(face));
    if (extrusion.IsNull() || extrusion->BasisCurve().IsNull()) return {};
    Handle(Geom_Curve) curve = Handle(Geom_Curve)::DownCast(extrusion->BasisCurve()->Copy());
    if (curve.IsNull()) return {};
    const double f = curve->FirstParameter(), l = curve->LastParameter();
    double sample = 0.0;
    if (std::isfinite(f) && f > -1e50) sample = std::max(sample, f);
    if (std::isfinite(l) && l < 1e50) sample = std::min(sample, l);
    const gp_Vec direction(axis);
    const double offset = gp_Vec(curve->Value(sample), corner).Dot(direction);
    curve->Translate(direction * offset);
    BRepAdaptor_Curve approx(section);
    const double p0 = approx.FirstParameter(), p1 = approx.LastParameter();
    const double allowed = std::max(tolerance, 2.0 * BRep_Tool::Tolerance(section));
    auto locate = [&](const gp_Pnt& point, double& parameter) {
        GeomAPI_ProjectPointOnCurve projection(point, curve);
        if (projection.NbPoints() == 0 || projection.LowerDistance() > allowed) return false;
        parameter = projection.LowerDistanceParameter();
        for (int i = 1; i <= projection.NbPoints(); ++i)
            if (projection.Distance(i) <= allowed &&
                std::abs(projection.Parameter(i) - parameter) > Precision::PConfusion())
                return false;
        return true;
    };
    double u0 = 0.0, u1 = 0.0;
    if (!locate(approx.Value(p0), u0) || !locate(approx.Value(p1), u1)) return {};
    const double first = std::min(u0, u1), last = std::max(u0, u1);
    if (!(last - first > Precision::PConfusion())) return {};
    GeomAPI_ProjectPointOnCurve middle(approx.Value((p0 + p1) * 0.5), curve, first, last);
    if (middle.NbPoints() == 0 || middle.LowerDistance() > allowed) return {};
    BRepBuilderAPI_MakeEdge maker(curve, first, last);
    return maker.IsDone() ? maker.Edge() : TopoDS_Edge();
}

// Keep the exact parameterization while trimming the underlying B-spline too.
// Merely setting the edge's range leaves its entire knot vector in the
// 2-D intersection algorithm, which can scan thousands of irrelevant spans.
TopoDS_Edge section_segment(const TopoDS_Edge& edge, double first, double last) {
    double source_first = 0.0, source_last = 0.0;
    Handle(Geom_Curve) curve = BRep_Tool::Curve(edge, source_first, source_last);
    if (curve.IsNull()) return {};
    first = std::max(first, source_first);
    last = std::min(last, source_last);
    if (!(last - first > Precision::PConfusion())) return {};
    const Handle(Geom_BSplineCurve) spline = Handle(Geom_BSplineCurve)::DownCast(curve);
    if (!spline.IsNull()) {
        Handle(Geom_BSplineCurve) local = Handle(Geom_BSplineCurve)::DownCast(spline->Copy());
        local->Segment(first, last);
        curve = local;
    }
    BRepBuilderAPI_MakeEdge maker(curve, first, last);
    return maker.IsDone() ? maker.Edge() : TopoDS_Edge();
}

TopoDS_Edge section_window(const TopoDS_Edge& edge, const gp_Pnt& corner, double width) {
    BRepAdaptor_Curve curve(edge);
    const double first = curve.FirstParameter(), last = curve.LastParameter();
    const bool starts = curve.Value(first).Distance(corner) < curve.Value(last).Distance(corner);
    const double start = starts ? first : last;
    double end = starts ? last : first;
    const double length = GCPnts_AbscissaPoint::Length(curve);
    if (!std::isfinite(length) || length <= Precision::Confusion()) return {};
    if (width < length) {
        GCPnts_AbscissaPoint point(curve, starts ? width : -width, start);
        if (!point.IsDone()) return {};
        end = point.Parameter();
    }
    return section_segment(edge, std::min(start, end), std::max(start, end));
}

TopoDS_Edge corner_section_segment(const TopoDS_Edge& source, const TopoDS_Edge& trimmed,
                                   const gp_Pnt& corner) {
    BRepAdaptor_Curve original(source), remainder(trimmed);
    const double first = original.FirstParameter(), last = original.LastParameter();
    const double start = original.Value(first).Distance(corner) < original.Value(last).Distance(corner)
        ? first : last;
    const double a = remainder.FirstParameter(), b = remainder.LastParameter();
    const double tangent = remainder.Value(a).Distance(corner) < remainder.Value(b).Distance(corner)
        ? a : b;
    return section_segment(source, std::min(start, tangent), std::max(start, tangent));
}

bool trimmed_section_tangent(const TopoDS_Edge& original, const TopoDS_Edge& trimmed,
                             const TopoDS_Edge& arc, const gp_Pnt& corner,
                             double radius, double tolerance, int& arc_end) {
    BRepAdaptor_Curve before(original), after(trimmed), round(arc);
    const double before_par[2] = {before.FirstParameter(), before.LastParameter()};
    const double after_par[2] = {after.FirstParameter(), after.LastParameter()};
    const double arc_par[2] = {round.FirstParameter(), round.LastParameter()};
    const int old_corner = before.Value(before_par[0]).Distance(corner) <
                           before.Value(before_par[1]).Distance(corner) ? 0 : 1;
    const gp_Pnt remote = before.Value(before_par[1 - old_corner]);
    const int new_remote = after.Value(after_par[0]).Distance(remote) <
                           after.Value(after_par[1]).Distance(remote) ? 0 : 1;
    if (after.Value(after_par[new_remote]).Distance(remote) > tolerance) return false;
    const gp_Pnt tangent = after.Value(after_par[1 - new_remote]);
    if (tangent.Distance(corner) > 2.0 * radius + tolerance) return false;
    arc_end = round.Value(arc_par[0]).Distance(tangent) <
              round.Value(arc_par[1]).Distance(tangent) ? 0 : 1;
    if (round.Value(arc_par[arc_end]).Distance(tangent) > tolerance) return false;
    gp_Pnt p;
    gp_Vec a, b;
    after.D1(after_par[1 - new_remote], p, a);
    round.D1(arc_par[arc_end], p, b);
    if (a.SquareMagnitude() <= gp::Resolution() || b.SquareMagnitude() <= gp::Resolution())
        return false;
    return std::abs(a.Normalized().Dot(b.Normalized())) >= 1.0 - 1e-6;
}

bool on_shallow_cylinder(const TopoDS_Face& face, const ShallowFillet& fillet) {
    BRepAdaptor_Surface surface(face);
    if (surface.GetType() != GeomAbs_Cylinder) return false;
    const gp_Cylinder cylinder = surface.Cylinder();
    return std::abs(cylinder.Radius() - fillet.arc.Radius()) <= fillet.tolerance &&
           cylinder.Axis().Direction().IsParallel(fillet.axis, 1e-6) &&
           gp_Lin(cylinder.Axis()).Distance(fillet.arc.Location()) <= fillet.tolerance;
}

ShallowFillet shallow_fillet(const TopoDS_Shape& shape,
                             const std::vector<TopoDS_Edge>& edges, std::size_t picked,
                             const FilletSelection& selection, double radius) {
    ShallowFillet out;
    const TopoDS_Edge& edge = edges[picked];
    if (solid_count(shape) != 1 || BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line ||
        !selection.edge_faces.Contains(edge)) return out;
    std::array<TopoDS_Face, 2> faces;
    int count = 0;
    for (TopTools_ListIteratorOfListOfShape it(selection.edge_faces.FindFromKey(edge)); it.More(); it.Next()) {
        const TopoDS_Face face = TopoDS::Face(it.Value());
        if (count && face.IsSame(faces[0])) continue;
        if (count == 2) return out;
        faces[count++] = face;
    }
    if (count != 2) return out;
    const TopoDS_Vertex first = TopExp::FirstVertex(edge), last = TopExp::LastVertex(edge);
    if (first.IsNull() || last.IsNull() || first.IsSame(last)) return out;
    const gp_Pnt p = BRep_Tool::Pnt(first), q = BRep_Tool::Pnt(last);
    const double length = p.Distance(q);
    if (length <= Precision::Confusion()) return out;
    out.axis = gp_Dir(gp_Vec(p, q));
    out.corner = gp_Pnt((p.X() + q.X()) * 0.5, (p.Y() + q.Y()) * 0.5,
                        (p.Z() + q.Z()) * 0.5);
    out.tolerance = std::max({10.0 * Precision::Confusion(),
                              10.0 * BRep_Tool::Tolerance(edge),
                              10.0 * BRep_Tool::Tolerance(faces[0]),
                              10.0 * BRep_Tool::Tolerance(faces[1])});
    std::array<gp_Vec, 2> normals;
    for (int i = 0; i < 2; ++i) {
        BRepAdaptor_Surface surface(faces[i]);
        if (surface.GetType() == GeomAbs_Plane) {
            if (std::abs(surface.Plane().Axis().Direction().Dot(out.axis)) > 1e-7) return out;
        } else if (surface.GetType() == GeomAbs_SurfaceOfExtrusion) {
            if (!surface.Direction().IsParallel(out.axis, 1e-7)) return out;
        } else {
            return out;
        }
        BRepAdaptor_Curve2d curve(edge, faces[i]);
        const gp_Pnt2d uv = curve.Value((curve.FirstParameter() + curve.LastParameter()) * 0.5);
        gp_Pnt point;
        gp_Vec du, dv;
        surface.D1(uv.X(), uv.Y(), point, du, dv);
        normals[i] = du.Crossed(dv);
        if (normals[i].SquareMagnitude() <= gp::Resolution()) return out;
        normals[i].Normalize();
    }
    const double angle = std::atan2(normals[0].Crossed(normals[1]).Magnitude(),
                                    std::abs(normals[0].Dot(normals[1])));
    if (!(angle > 1e-6 && angle < 0.1)) return out;

    out.failure = FilletFailure::construction_failed;
    out.reason = "the shallow-edge section failed or reported kernel warnings";
    const gp_Pln plane(out.corner, out.axis);
    BRepAlgoAPI_Section section(shape, plane, Standard_False);
    section.SetNonDestructive(Standard_True);
    section.Build();
    if (!section.IsDone() || section.HasErrors() || section.HasWarnings()) return out;
    TopTools_IndexedMapOfShape section_edges;
    TopExp::MapShapes(section.Shape(), TopAbs_EDGE, section_edges);
    out.reason = "could not identify the shallow edge's exact section supports";
    std::vector<TopoDS_Edge> profile;
    std::array<int, 2> at_corner{-1, -1};
    std::array<bool, 2> matched{false, false};
    int corner_count = 0;
    for (int i = 1; i <= section_edges.Extent(); ++i) {
        const TopoDS_Edge e = TopoDS::Edge(section_edges(i));
        profile.push_back(e);
        const TopoDS_Vertex a = TopExp::FirstVertex(e), b = TopExp::LastVertex(e);
        if (a.IsNull() || b.IsNull()) return out;
        if (std::min(BRep_Tool::Pnt(a).Distance(out.corner),
                     BRep_Tool::Pnt(b).Distance(out.corner)) > out.tolerance) continue;
        if (corner_count == 2) return out;
        TopoDS_Shape ancestor;
        if (!section.HasAncestorFaceOn1(e, ancestor)) return out;
        const int owner = ancestor.IsSame(faces[0]) ? 0 : ancestor.IsSame(faces[1]) ? 1 : -1;
        if (owner < 0 || matched[owner]) return out;
        const TopoDS_Edge exact = exact_section_support(faces[owner], e, out.corner, out.axis,
                                                        out.tolerance);
        if (exact.IsNull()) return out;
        profile.back() = exact;
        matched[owner] = true;
        at_corner[corner_count++] = i - 1;
    }
    if (corner_count != 2 || !matched[0] || !matched[1]) return out;
    out.reason = "could not construct a tangent arc on the shallow-edge section";
    TopoDS_Edge local_a, local_b, a, b, arc;
    // The shallow turn usually fits within a small fraction of the radius.
    // Expand the exact local windows if curvature needs more room.
    double window = std::max(radius * 0.125, 32.0 * out.tolerance);
    const double limit = std::max(2.0 * radius, window);
    for (;;) {
        local_a = section_window(profile[at_corner[0]], out.corner, window);
        local_b = section_window(profile[at_corner[1]], out.corner, window);
        if (local_a.IsNull() || local_b.IsNull()) {
            out.reason = "could not extract a bounded shallow-edge section window";
            return out;
        }
        auto construct_arc = [&](const gp_Pln& work_plane) {
            a = local_a;
            b = local_b;
            ChFi2d_FilletAlgo fillet(a, b, work_plane);
            if (!fillet.Perform(radius)) {
                out.reason = "the kernel found no shallow-edge planar arc";
                return false;
            }
            if (fillet.NbResults(out.corner) != 1) {
                out.reason = "the shallow-edge planar arc is ambiguous";
                return false;
            }
            arc = fillet.Result(out.corner, a, b);
            int arc_a = -1, arc_b = -1;
            if (!arc.IsNull() && BRepAdaptor_Curve(arc).GetType() == GeomAbs_Circle &&
                trimmed_section_tangent(local_a, a, arc, out.corner, radius, out.tolerance, arc_a) &&
                trimmed_section_tangent(local_b, b, arc, out.corner, radius, out.tolerance, arc_b) &&
                arc_a != arc_b)
                return true;
            out.reason = "the shallow-edge planar arc failed endpoint or tangency validation";
            arc.Nullify();
            return false;
        };
        if (construct_arc(plane)) break;

        // OCCT's iterative 2-D solver can miss a root solely because of the
        // chosen coordinate frame. Try its own edge-derived frame only when it
        // describes the same section plane; all geometric checks still apply.
        BRep_Builder group_builder;
        TopoDS_Compound group;
        group_builder.MakeCompound(group);
        group_builder.Add(group, local_a);
        group_builder.Add(group, local_b);
        BRepBuilderAPI_FindPlane finder(group);
        if (finder.Found() && !finder.Plane().IsNull()) {
            const gp_Pln fitted = finder.Plane()->Pln();
            if (fitted.Axis().Direction().IsParallel(out.axis, 1e-7) &&
                fitted.Distance(out.corner) <= out.tolerance && construct_arc(fitted))
                break;
        }
        if (!(window < limit)) return out;
        window = std::min(limit, window * 2.0);
    }
    out.reason = "the shallow-edge planar arc has an unexpected radius or axis";
    out.arc = BRepAdaptor_Curve(arc).Circle();
    if (std::abs(out.arc.Radius() - radius) > out.tolerance ||
        !out.arc.Axis().Direction().IsParallel(out.axis, 1e-6)) return out;
    out.reason = "the shallow-edge arc did not produce a valid local material patch";
    const TopoDS_Edge short_a = corner_section_segment(local_a, a, out.corner);
    const TopoDS_Edge short_b = corner_section_segment(local_b, b, out.corner);
    if (short_a.IsNull() || short_b.IsNull()) return out;
    const TopoDS_Face patch = section_face(plane, {short_a, short_b, arc}, out.tolerance);
    if (patch.IsNull() || !BRepCheck_Analyzer(patch).IsValid()) return out;
    GProp_GProps removal_area;
    BRepGProp::SurfaceProperties(patch, removal_area);
    if (!std::isfinite(removal_area.Mass()) ||
        !(removal_area.Mass() > out.tolerance * out.tolerance)) return out;

    // The bounded patch must lie entirely in the original single-loop section:
    // this rejects concave/additive solutions and any other intervening boundary.
    // The patch itself and the final three-dimensional solid are independently
    // validated; an expensive self-intersection pass on the full intermediate
    // section is unnecessary.
    out.reason = "could not prove the shallow-edge patch lies inside the source section";
    const TopoDS_Face original = section_face(plane, profile, out.tolerance);
    if (original.IsNull()) return out;
    TopTools_ListOfShape patch_args, section_tools;
    patch_args.Append(patch);
    section_tools.Append(original);
    BRepAlgoAPI_Cut outside;
    outside.SetArguments(patch_args);
    outside.SetTools(section_tools);
    outside.SetNonDestructive(Standard_True);
    outside.Build();
    if (!outside.IsDone() || outside.HasErrors() || outside.HasWarnings()) return out;
    GProp_GProps outside_area;
    BRepGProp::SurfaceProperties(outside.Shape(), outside_area);
    if (!std::isfinite(outside_area.Mass()) ||
        outside_area.Mass() > std::max(out.tolerance * out.tolerance, removal_area.Mass() * 1e-6))
        return out;
    out.reason = "the shallow-edge material tool could not be restricted to the source edge";

    Bnd_Box bounds;
    BRepBndLib::Add(shape, bounds);
    if (bounds.IsVoid() || bounds.IsWhole()) return out;
    double x0, y0, z0, x1, y1, z1;
    bounds.Get(x0, y0, z0, x1, y1, z1);
    const double span = std::hypot(std::hypot(x1 - x0, y1 - y0), z1 - z0) + 2.0 * radius;
    if (!std::isfinite(span) || !(span > length)) return out;
    gp_Trsf shift;
    shift.SetTranslation(gp_Vec(out.axis) * -span);
    const TopoDS_Shape moved = BRepBuilderAPI_Transform(patch, shift, Standard_True).Shape();
    const TopoDS_Shape tool =
        BRepPrimAPI_MakePrism(moved, gp_Vec(out.axis) * (2.0 * span), Standard_False,
                              Standard_True).Shape();
    if (tool.IsNull()) return out;
    TopTools_ListOfShape args, tools;
    args.Append(shape);
    tools.Append(tool);
    BRepAlgoAPI_Common affected;
    affected.SetArguments(args);
    affected.SetTools(tools);
    affected.SetNonDestructive(Standard_True);
    affected.Build();
    if (!affected.IsDone() || affected.HasErrors() || affected.HasWarnings() ||
        solid_count(affected.Shape()) != 1) return out;
    GProp_GProps removed_volume;
    BRepGProp::VolumeProperties(affected.Shape(), removed_volume);
    if (!(removed_volume.Mass() > out.tolerance * out.tolerance * out.tolerance)) return out;
    Bnd_Box local;
    BRepBndLib::Add(affected.Shape(), local);
    if (local.IsVoid() || local.IsWhole()) return out;
    local.Get(x0, y0, z0, x1, y1, z1);
    const gp_Vec direction(out.axis);
    const double margin = std::max(0.1 * radius, 10.0 * out.tolerance);
    for (double x : {x0, x1}) for (double y : {y0, y1}) for (double z : {z0, z1}) {
        const gp_Vec delta(out.corner, gp_Pnt(x, y, z));
        const double axial = delta.Dot(direction);
        if (std::abs(axial) > length * 0.5 + margin ||
            (delta - direction * axial).Magnitude() > 2.0 * radius + out.tolerance)
            return out;
    }
    out.reason = "the shallow-edge material cut failed, warned, or produced an invalid solid";

    BRepAlgoAPI_Cut cut;
    cut.SetArguments(args);
    cut.SetTools(tools);
    cut.SetNonDestructive(Standard_True);
    cut.Build();
    if (!cut.IsDone() || cut.HasErrors() || cut.HasWarnings() || cut.Shape().IsNull() ||
        solid_count(cut.Shape()) != 1 || !is_watertight(cut.Shape()) ||
        !BRepCheck_Analyzer(cut.Shape()).IsValid()) return out;
    out.reason = "could not trace the shallow blend and every remaining selected edge";
    TopTools_IndexedMapOfShape result_faces, result_edges, tool_faces, claimed;
    TopExp::MapShapes(cut.Shape(), TopAbs_FACE, result_faces);
    TopExp::MapShapes(cut.Shape(), TopAbs_EDGE, result_edges);
    TopExp::MapShapes(tool, TopAbs_FACE, tool_faces);
    for (int i = 1; i <= tool_faces.Extent(); ++i) {
        const TopoDS_Face tool_face = TopoDS::Face(tool_faces(i));
        if (!on_shallow_cylinder(tool_face, out)) continue;
        const TopTools_ListOfShape& images = cut.Modified(tool_face);
        for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next())
            if (it.Value().ShapeType() == TopAbs_FACE && result_faces.Contains(it.Value()) &&
                on_shallow_cylinder(TopoDS::Face(it.Value()), out))
                out.blend_faces.push_back(TopoDS::Face(it.Value()));
    }
    if (out.blend_faces.empty()) return out;
    for (int i = 0; i < 2; ++i) {
        const TopTools_ListOfShape& images = cut.Modified(faces[i]);
        for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next())
            if (it.Value().ShapeType() == TopAbs_FACE && result_faces.Contains(it.Value()))
                out.supports[i].push_back(TopoDS::Face(it.Value()));
        if (out.supports[i].empty() && result_faces.Contains(faces[i]))
            out.supports[i].push_back(faces[i]);
        if (out.supports[i].empty()) return out;
    }
    for (std::size_t i = 0; i < edges.size(); ++i) {
        if (i == picked) continue;
        const TopTools_ListOfShape& images = cut.Modified(edges[i]);
        bool mapped = false;
        for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next()) {
            if (it.Value().ShapeType() != TopAbs_EDGE || !result_edges.Contains(it.Value())) continue;
            if (claimed.Contains(it.Value())) return out;
            claimed.Add(it.Value());
            out.remaining.push_back(TopoDS::Edge(it.Value()));
            mapped = true;
        }
        if (!mapped && result_edges.Contains(edges[i]) && !claimed.Contains(edges[i])) {
            claimed.Add(edges[i]);
            out.remaining.push_back(edges[i]);
            mapped = true;
        }
        if (!mapped) return out;
    }
    if (op_history_recording()) op_history_record(cut.History());
    out.shape = cut.Shape();
    out.failure = FilletFailure::none;
    out.reason.clear();
    return out;
}

void result_face_images(BRepFilletAPI_MakeFillet* mk, const TopoDS_Shape& source,
                        const TopTools_IndexedMapOfShape& faces,
                        std::vector<TopoDS_Shape>& images) {
    if (mk) {
        const TopTools_ListOfShape& modified = mk->Modified(source);
        for (TopTools_ListIteratorOfListOfShape it(modified); it.More(); it.Next())
            if (it.Value().ShapeType() == TopAbs_FACE && faces.Contains(it.Value()))
                images.push_back(it.Value());
    }
    if (images.empty() && faces.Contains(source)) images.push_back(source);
}

bool face_borders_supports(const TopoDS_Face& face,
                           const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                           const std::array<std::vector<TopoDS_Shape>, 2>& supports) {
    std::array<bool, 2> touches{false, false};
    for (TopExp_Explorer e(face, TopAbs_EDGE); e.More(); e.Next()) {
        if (!edge_faces.Contains(e.Current())) continue;
        const TopTools_ListOfShape& neighbours = edge_faces.FindFromKey(e.Current());
        for (TopTools_ListIteratorOfListOfShape n(neighbours); n.More(); n.Next())
            for (int side = 0; side < 2; ++side)
                for (const TopoDS_Shape& support : supports[side])
                    if (n.Value().IsSame(support)) touches[side] = true;
    }
    return touches[0] && touches[1];
}

bool shallow_fillet_preserved(
    const ShallowFillet& shallow, BRepFilletAPI_MakeFillet* mk,
    const TopTools_IndexedMapOfShape& faces,
    const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    std::array<std::vector<TopoDS_Shape>, 2> supports;
    for (int i = 0; i < 2; ++i) {
        for (const TopoDS_Face& source : shallow.supports[i])
            result_face_images(mk, source, faces, supports[i]);
        if (supports[i].empty()) return false;
    }
    std::vector<TopoDS_Shape> images;
    for (const TopoDS_Face& source : shallow.blend_faces) {
        images.clear();
        result_face_images(mk, source, faces, images);
        for (const TopoDS_Shape& image : images) {
            const TopoDS_Face face = TopoDS::Face(image);
            if (!on_shallow_cylinder(face, shallow) ||
                !face_borders_supports(face, edge_faces, supports)) continue;
            GProp_GProps area;
            BRepGProp::SurfaceProperties(face, area);
            if (!(area.Mass() > shallow.tolerance * shallow.tolerance)) continue;
            const TopoDS_Vertex point = BRepBuilderAPI_MakeVertex(shallow.corner);
            BRepExtrema_DistShapeShape distance(point, face);
            if (distance.IsDone() &&
                distance.Value() <= 2.0 * shallow.arc.Radius() + shallow.tolerance)
                return true;
        }
    }
    return false;
}

// OCCT sometimes attributes the faces on a tangent contour to a neighbouring
// edge, not their own source edge. Accept its contour-specific surface only if
// a result face borders images of both source supports and follows the edge.
bool contour_face_covers_edge(
    BRepFilletAPI_MakeFillet& mk,
    const TopTools_IndexedMapOfShape& result_faces,
    const TopTools_IndexedDataMapOfShapeListOfShape& source_faces,
    const TopTools_IndexedDataMapOfShapeListOfShape& result_edge_faces,
    const TopoDS_Edge& edge, double radius, TopoDS_Face& covered) {
    if (!source_faces.Contains(edge)) return false;
    std::array<std::vector<TopoDS_Shape>, 2> supports;
    int count = 0;
    TopoDS_Shape previous;
    for (TopTools_ListIteratorOfListOfShape it(source_faces.FindFromKey(edge)); it.More(); it.Next()) {
        if (count && it.Value().IsSame(previous)) continue;
        if (count == 2) return false;
        previous = it.Value();
        result_face_images(&mk, it.Value(), result_faces, supports[count]);
        if (supports[count].empty()) return false;
        ++count;
    }
    if (count != 2) return false;
    BRepAdaptor_Curve source(edge);
    const double first = source.FirstParameter(), last = source.LastParameter();
    const double tolerance = 10.0 * BRep_Tool::Tolerance(edge);
    const int contour = mk.Contour(edge);
    for (int s = 1; s <= mk.NbComputedSurfaces(contour); ++s) {
        const Handle(Geom_Surface) surface = mk.ComputedSurface(contour, s);
        if (surface.IsNull()) continue;
        for (int i = 1; i <= result_faces.Extent(); ++i) {
            const TopoDS_Face face = TopoDS::Face(result_faces(i));
            if (BRep_Tool::Surface(face) != surface ||
                !face_borders_supports(face, result_edge_faces, supports)) continue;
            bool within_radius = true;
            for (double t : {0.25, 0.5, 0.75}) {
                const TopoDS_Vertex point = BRepBuilderAPI_MakeVertex(source.Value(first + t * (last - first)));
                BRepExtrema_DistShapeShape distance(point, face);
                if (!distance.IsDone() || distance.Value() > radius + tolerance) {
                    within_radius = false;
                    break;
                }
            }
            if (within_radius) { covered = face; return true; }
        }
    }
    return false;
}

constexpr char kPrecisionSidecar[] = "@fillet_precision ";

std::string fillet_point_text(const gp_Pnt& p) {
    char buf[96];
    std::snprintf(buf, sizeof(buf), "(%.3f, %.3f, %.3f)", p.X(), p.Y(), p.Z());
    return buf;
}

// OCCT reports which contours and corners it could not blend. Naming them by the
// caller's selection order and a model-space point turns "could not build" into a
// location the caller can inspect, reorder around, or reshape. `contours` holds each
// selected edge's contour index read before Build; only the null-safe faulty-contour
// and faulty-vertex lists are read afterwards (a failed stripe may have no spine).
std::string fillet_build_failure(const BRepFilletAPI_MakeFillet& mk,
                                 const std::vector<TopoDS_Edge>& selected,
                                 const std::vector<int>& contours) {
    std::string out = "the kernel could not build all selected edges";
    try {
        int listed = 0;
        for (int i = 1; i <= mk.NbFaultyContours() && listed < 3; ++i) {
            const int contour = mk.FaultyContour(i);
            if (contour <= 0) continue;
            const auto pick = std::find(contours.begin(), contours.end(), contour);
            if (pick == contours.end()) continue;
            const std::size_t index = static_cast<std::size_t>(pick - contours.begin());
            BRepAdaptor_Curve curve(selected[index]);
            const gp_Pnt mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()));
            out += listed++ ? "; " : ": ";
            out += "the blend along selected edge " + std::to_string(index + 1) + " near " +
                   fillet_point_text(mid) + " failed";
        }
        for (int v = 1; v <= mk.NbFaultyVertices() && listed < 4; ++v) {
            out += listed++ ? "; " : ": ";
            out += "the corner at " + fillet_point_text(BRep_Tool::Pnt(mk.FaultyVertex(v))) +
                   " could not be closed";
        }
    } catch (const Standard_Failure&) {
    }
    return out;
}

// The first defect of an unusable fillet result, with where it is.
std::string fillet_invalid_solid_reason(const TopoDS_Shape& shape) {
    std::string out = "fillet result is not a valid closed solid";
    try {
        const auto at = [](const TopoDS_Shape& sub) {
            Bnd_Box box;
            BRepBndLib::Add(sub, box);
            if (box.IsVoid()) return std::string();
            double x0, y0, z0, x1, y1, z1;
            box.Get(x0, y0, z0, x1, y1, z1);
            return " near " + fillet_point_text(gp_Pnt((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2));
        };
        if (solid_count(shape) == 0) return out + " (it contains no solid)";
        BRepCheck_Analyzer analyzer(shape);
        if (!analyzer.IsValid()) {
            for (const TopAbs_ShapeEnum kind : {TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE}) {
                for (TopExp_Explorer ex(shape, kind); ex.More(); ex.Next()) {
                    if (analyzer.IsValid(ex.Current())) continue;
                    const char* name = kind == TopAbs_VERTEX ? "vertex" : kind == TopAbs_EDGE ? "edge" : "face";
                    return out + " (invalid " + name + at(ex.Current()) + ")";
                }
            }
            return out + " (fails the B-rep validity check)";
        }
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(shape, TopAbs_EDGE, edges);
        const std::vector<EdgeBoundaryUses> uses = edge_boundary_uses(shape, edges);
        for (int i = 1; i <= edges.Extent(); ++i) {
            const TopoDS_Edge& edge = TopoDS::Edge(edges(i));
            if (!BRep_Tool::Degenerated(edge) && uses[static_cast<std::size_t>(i)].is_free())
                return out + " (open: free edge" + at(edge) + ")";
        }
    } catch (const Standard_Failure&) {
    }
    return out;
}

std::string fillet_precision_sidecar(double first, double second) {
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << static_cast<int>(FilletFailure::construction_failed) << '\n'
        << kPrecisionSidecar << std::setprecision(17) << first << ' ' << second;
    return out.str();
}

// Concave blends that collide when built together (two bosses closer than a radius) can
// still build one contour at a time. The union of the material each adds is the merged
// blend: the rolling ball of each reaches the other, and the surfaces trim each other.
TopoDS_Shape fillet_blend_union(const TopoDS_Shape& shape, const std::vector<TopoDS_Edge>& edges,
                                double radius, std::string& why) {
    std::map<int, std::vector<TopoDS_Edge>> groups;
    {
        BRepFilletAPI_MakeFillet probe(shape);
        for (const TopoDS_Edge& edge : edges) probe.Add(radius, edge);
        for (const TopoDS_Edge& edge : edges) {
            const int contour = probe.Contour(edge);
            if (contour <= 0) {
                why = "a selected edge could not be added to a fillet contour";
                return {};
            }
            groups[contour].push_back(edge);
        }
    }
    if (groups.size() < 2 || groups.size() > 16) {
        why = groups.size() < 2
            ? "the selected edges form one contour, so there is nothing to build separately"
            : "too many contours to build separately";
        return {};
    }
    std::vector<TopoDS_Shape> materials;
    for (const auto& group : groups) {
        BRepFilletAPI_MakeFillet mk(shape);
        for (const TopoDS_Edge& edge : group.second) mk.Add(radius, edge);
        mk.Build();
        const TopoDS_Shape blended = mk.IsDone() ? mk.Shape() : TopoDS_Shape();
        if (blended.IsNull() || !is_watertight(blended) || !BRepCheck_Analyzer(blended).IsValid()) {
            why = "contour " + std::to_string(group.first) + " does not build even alone";
            return {};
        }
        if (op_history_recording()) {
            TopTools_ListOfShape args;
            args.Append(shape);
            op_history_record(new BRepTools_History(args, mk));
        }
        const TopoDS_Shape added = cut(blended, shape);
        if (added.IsNull()) {
            why = "could not isolate the material one contour adds";
            return {};
        }
        materials.push_back(added);
    }
    const TopoDS_Shape merged = fuse_many(shape, materials);
    if (merged.IsNull()) {
        why = "could not merge the separately built blends";
        return {};
    }
    return unify_same_domain(merged);
}

struct FilletProbe {
    TopoDS_Shape shape;
    FilletFailure failure = FilletFailure::none;
    std::string reason;
    std::string provenance;
    std::array<double, 2> required_precision{0.0, 0.0};
};

// Strict isolation transports a one-digit failure and diagnostic, or successful
// provenance. A precision failure also carries the two local bounds needed by
// the parent search; it never turns an isolation failure into a radius bound.
FilletProbe fillet_probe(const TopoDS_Shape& shape,
                         const std::vector<TopoDS_Edge>& edges,
                         const FilletSelection& selection, double r1,
                         std::optional<double> r2, bool unequal_requested,
                         const FilletValidation& validate,
                         std::chrono::steady_clock::time_point deadline,
                         chudware::util::ShapeBuildExecSession* worker,
                         std::chrono::steady_clock::time_point probe_deadline,
                         int runoff = 0, int sweep = 0) {
    using chudware::util::ShapeBuildFailure;
    const bool recording = op_history_recording();
    std::string sidecar;
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now());
    if (remaining.count() <= 0)
        return {{}, FilletFailure::budget_exhausted, "fillet search time budget exhausted", {}};
    const auto timeout = remaining;
    auto build = [&]() -> TopoDS_Shape {
        OpHistoryScope probe_history(recording);
        auto fail = [&](FilletFailure kind, const std::string& reason) -> TopoDS_Shape {
            sidecar = std::to_string(static_cast<int>(kind)) + '\n' + reason;
            return {};
        };
        auto fail_precision = [&](double first, double second) -> TopoDS_Shape {
            sidecar = fillet_precision_sidecar(first, second);
            return {};
        };
        try {
            if (r2 && (r1 <= selection.endpoint_resolution[0] ||
                       *r2 <= selection.endpoint_resolution[1])) {
                return fail_precision(r1 <= selection.endpoint_resolution[0]
                                          ? selection.endpoint_resolution[0] : 0.0,
                                      *r2 <= selection.endpoint_resolution[1]
                                          ? selection.endpoint_resolution[1] : 0.0);
            }
            TopoDS_Shape current = shape;
            const std::vector<TopoDS_Edge>* ordinary = &edges;
            // Run-off: blend a body whose support faces are extended past their open rims,
            // then trim back. The ordinary edges are the selection on that extended body.
            FilletRunoff extension;
            const std::vector<TopoDS_Edge> no_edges;
            if (runoff) {
                if (r2 || !selection.contact.empty())
                    return fail(FilletFailure::unsupported_topology,
                                "run-off applies to constant-radius manifold fillets only");
                const auto mode = static_cast<FilletRunoffMode>(runoff);
                const FilletRunoffPlan plan =
                    fillet_runoff_plan(shape, edges, selection.edge_faces, r1);
                if (!plan.applicable(mode))
                    return fail(FilletFailure::unsupported_topology,
                                plan.why.empty() ? "no support face ends at an open edge" : plan.why);
                if (mode == FilletRunoffMode::separate) {
                    std::string why;
                    current = fillet_blend_union(shape, edges, r1, why);
                    if (current.IsNull()) return fail(FilletFailure::construction_failed, why);
                    ordinary = &no_edges;
                } else {
                    extension = fillet_runoff_extend(shape, edges, plan, r1, mode);
                    if (extension.extended.IsNull())
                        return fail(FilletFailure::construction_failed, extension.why);
                    current = extension.extended;
                    ordinary = &extension.edges;
                }
            }
            // Swept blends: the chosen prismatic edges are blended exactly, one at a time;
            // the kernel builder then blends the other selected edges on that body.
            std::vector<TopoDS_Edge> unswept;
            const bool used_sweep = sweep != 0;
            if (sweep) {
                if (r2 || runoff || !selection.contact.empty())
                    return fail(FilletFailure::unsupported_topology,
                                "swept blends apply to constant-radius manifold fillets only");
                const std::vector<std::size_t> chosen = sweep_blend_choose(
                    edges, selection.edge_faces, static_cast<SweepBlendScope>(sweep));
                if (chosen.empty())
                    return fail(FilletFailure::unsupported_topology,
                                "no selected edge is straight between faces constant along it");
                std::vector<TopoDS_Edge> swept;
                for (std::size_t i = 0, next = 0; i < edges.size(); ++i) {
                    if (next < chosen.size() && chosen[next] == i) {
                        swept.push_back(edges[i]);
                        ++next;
                    } else {
                        unswept.push_back(edges[i]);
                    }
                }
                SweepBlendProfile profile;
                profile.size = r1;
                SweepBlendRun run = sweep_blend_edges(
                    shape, swept, [&](std::size_t) { return profile; }, unswept);
                if (run.shape.IsNull())
                    return fail(FilletFailure::construction_failed,
                                run.edge_failed
                                    ? "selected edge " + std::to_string(chosen[run.failed] + 1) +
                                          ": " + run.why
                                    : run.why);
                current = run.shape;
                unswept = std::move(run.remaining);
                ordinary = &unswept;
            }
            ContactJoin join;
            if (!selection.contact.empty()) {
                join = join_contact_edges(shape, r1, selection.contact, selection.manifold,
                                          selection.edge_faces, selection.face_solids);
                if (join.failed || join.joined != static_cast<int>(selection.contact.size())) {
                    const FilletFailure kind = join.failure == ContactFailure::topology
                        ? FilletFailure::unsupported_topology : FilletFailure::construction_failed;
                    return fail(kind, join.why.empty() ? "could not join every selected contact edge" : join.why);
                }
                current = join.shape;
                ordinary = &join.manifold;
            }

            std::string provenance;
            if (!ordinary->empty()) {
                std::optional<BRepFilletAPI_MakeFillet> builder;
                builder.emplace(current);
                BRepFilletAPI_MakeFillet* mk = &*builder;
                ShallowFillet shallow;
                bool used_shallow = false;
                struct VertexRadius { int contour; TopoDS_Vertex vertex; double radius; };
                std::vector<VertexRadius> vertex_radii;
                if (r2 && ((unequal_requested && r1 == *r2) ||
                           (r1 != *r2 && std::abs(r1 - *r2) <= Precision::Confusion())))
                    return fail(FilletFailure::construction_failed,
                                "unequal variable endpoints are below OCCT radius resolution");
                if (r2 && r1 != *r2) vertex_radii.reserve(ordinary->size() * 2);
                for (std::size_t i = 0; i < ordinary->size(); ++i) {
                    const TopoDS_Edge& edge = (*ordinary)[i];
                    if (r2) mk->Add(r1, *r2, edge);
                    else mk->Add(r1, edge);
                    const int contour = mk->Contour(edge);
                    if (contour <= 0) {
                        auto no_contour_reason = [&](const TopoDS_Edge& rejected) {
                            if (selection.edge_faces.Contains(rejected))
                                return fillet_no_contour_reason(rejected, selection.edge_faces);
                            TopTools_IndexedDataMapOfShapeListOfShape current_faces;
                            TopExp::MapShapesAndAncestors(current, TopAbs_EDGE, TopAbs_FACE, current_faces);
                            return fillet_no_contour_reason(rejected, current_faces);
                        };
                        if (r2 || runoff || used_sweep || !selection.contact.empty())
                            return fail(FilletFailure::unsupported_topology,
                                        "selected edge " + std::to_string(i + 1) + ": " +
                                        no_contour_reason(edge));
                        shallow = shallow_fillet(current, *ordinary, i, selection, r1);
                        if (shallow.failure != FilletFailure::none)
                            return fail(shallow.failure, "selected edge " + std::to_string(i + 1) +
                                ": " + (shallow.reason.empty() ? no_contour_reason(edge) : shallow.reason));
                        current = shallow.shape;
                        ordinary = &shallow.remaining;
                        used_shallow = true;
                        builder.emplace(current);
                        mk = &*builder;
                        for (const TopoDS_Edge& mapped : *ordinary) {
                            mk->Add(r1, mapped);
                            if (mk->Contour(mapped) <= 0)
                                return fail(FilletFailure::unsupported_topology,
                                            "another selected edge: " + no_contour_reason(mapped));
                        }
                        break;
                    }
                    if (r2 && r1 != *r2) {
                        const TopoDS_Vertex first = TopExp::FirstVertex(edge, Standard_True);
                        const TopoDS_Vertex last = TopExp::LastVertex(edge, Standard_True);
                        if (first.IsNull() || last.IsNull() || first.IsSame(last))
                            return fail(FilletFailure::unsupported_topology,
                                        "unequal variable radii cannot meet at the same closed-edge vertex");
                        for (const auto& endpoint : {VertexRadius{contour, first, r1},
                                                     VertexRadius{contour, last, *r2}}) {
                            auto prior = std::find_if(vertex_radii.begin(), vertex_radii.end(),
                                [&](const VertexRadius& p) {
                                    return p.contour == endpoint.contour && p.vertex.IsSame(endpoint.vertex);
                                });
                            if (prior != vertex_radii.end() && prior->radius != endpoint.radius)
                                return fail(FilletFailure::unsupported_topology,
                                            "selected edges demand conflicting radii at a shared contour vertex");
                            if (prior == vertex_radii.end()) vertex_radii.push_back(endpoint);
                        }
                    }
                }
                const TopoDS_Shape builder_input = current;
                TopTools_IndexedMapOfShape faces;
                TopTools_IndexedDataMapOfShapeListOfShape source_faces, result_edge_faces;
                if (!ordinary->empty()) {
                    // Reapply at actual vertices, including on tangent contours.
                    for (const VertexRadius& v : vertex_radii)
                        mk->SetRadius(v.radius, v.contour, v.vertex);
                    const bool small_variable = r2 && std::max(r1, *r2) < 1e-2;
                    // Contour indices must be read before Build: after a failed build
                    // OCCT can leave a faulty stripe without a spine, and any spine
                    // query (NbEdges, Edge, StripeStatus, Contour) then dereferences null.
                    std::vector<int> contours;
                    contours.reserve(ordinary->size());
                    for (const TopoDS_Edge& edge : *ordinary) contours.push_back(mk->Contour(edge));
                    for (bool precise = false;; precise = true) {
                        if (precise) {
                            // OCCT's default 1e-4 approximation can exceed a tiny
                            // blend. Tighten construction, never its acceptance.
                            mk->Reset();
                            mk->SetParams(1e-2, Precision::Confusion(), Precision::PConfusion(),
                                          Precision::Confusion(), Precision::PConfusion(), 1e-3);
                            faces.Clear();
                            source_faces.Clear();
                            result_edge_faces.Clear();
                        }
                        mk->Build();
                        if (!mk->IsDone()) {
                            if (!precise && small_variable) continue;
                            return fail(FilletFailure::construction_failed,
                                        fillet_build_failure(*mk, *ordinary, contours));
                        }
                        current = mk->Shape();
                        if (current.IsNull())
                            return fail(FilletFailure::construction_failed, "the fillet builder returned no shape");
                        gp_Pnt gap_at;
                        const double gap = max_tolerance(current, &gap_at);
                        if (blend_tolerance_blown(max_tolerance(builder_input), gap,
                                                  std::min(r1, r2.value_or(r1)))) {
                            char size[32];
                            std::snprintf(size, sizeof(size), "%.3g", gap);
                            return fail(FilletFailure::invalid_geometry,
                                        "the kernel could not close the blend's corner near " +
                                            fillet_point_text(gap_at) + " (it bridged a " + size +
                                            " mm gap with tolerance)");
                        }
                        TopExp::MapShapes(current, TopAbs_FACE, faces);
                        std::array<double, 2> required_precision{0.0, 0.0};
                        for (const TopoDS_Edge& edge : *ordinary) {
                            if (mk->Contour(edge) <= 0)
                                return fail(FilletFailure::invalid_geometry, "a selected edge lost its builder contour");
                            bool generated = false;
                            const auto check_face = [&](const TopoDS_Face& face) {
                                generated = true;
                                if (!r2) return;
                                const auto local = variable_result_resolution(edge, face);
                                if (r1 <= local[0]) required_precision[0] = std::max(required_precision[0], local[0]);
                                if (*r2 <= local[1]) required_precision[1] = std::max(required_precision[1], local[1]);
                            };
                            const TopTools_ListOfShape& images = mk->Generated(edge);
                            for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next()) {
                                if (it.Value().ShapeType() != TopAbs_FACE || !faces.Contains(it.Value()))
                                    continue;
                                check_face(TopoDS::Face(it.Value()));
                                if (!r2) break;
                            }
                            if (!generated) {
                                if (source_faces.IsEmpty()) {
                                    TopExp::MapShapesAndAncestors(builder_input, TopAbs_EDGE,
                                                                 TopAbs_FACE, source_faces);
                                    TopExp::MapShapesAndAncestors(current, TopAbs_EDGE,
                                                                 TopAbs_FACE, result_edge_faces);
                                }
                                TopoDS_Face covered;
                                if (contour_face_covers_edge(*mk, faces, source_faces,
                                                            result_edge_faces, edge,
                                                            std::max(r1, r2.value_or(r1)), covered))
                                    check_face(covered);
                            }
                            if (!generated)
                                return fail(FilletFailure::invalid_geometry,
                                            "the builder did not generate a result face for every selected edge");
                        }
                        if (required_precision[0] > 0.0 || required_precision[1] > 0.0) {
                            if (!precise && small_variable) continue;
                            return fail_precision(required_precision[0], required_precision[1]);
                        }
                        break;
                    }
                }
                if (runoff) {
                    // The blend, then the trim: histories chain in the order the ops ran.
                    if (recording) {
                        TopTools_ListOfShape args;
                        args.Append(builder_input);
                        op_history_record(new BRepTools_History(args, *mk));
                    }
                    std::string why;
                    current = fillet_runoff_trim(extension, current, why);
                    if (current.IsNull())
                        return fail(FilletFailure::invalid_geometry, why);
                }
                if (used_shallow) {
                    if (faces.IsEmpty()) TopExp::MapShapes(current, TopAbs_FACE, faces);
                    if (result_edge_faces.IsEmpty())
                        TopExp::MapShapesAndAncestors(current, TopAbs_EDGE,
                                                     TopAbs_FACE, result_edge_faces);
                    if (!shallow_fillet_preserved(shallow, ordinary->empty() ? nullptr : mk,
                                                  faces, result_edge_faces))
                        return fail(FilletFailure::invalid_geometry,
                                    "the shallow selected edge did not retain its requested fillet");
                }
                if (recording) {
                    if (!ordinary->empty() && !used_shallow && !runoff && !used_sweep &&
                        selection.contact.empty()) {
                        provenance = op_provenance_encode(shape, current, *mk);
                    } else {
                        if (!ordinary->empty() && !runoff) {
                            TopTools_ListOfShape args;
                            args.Append(builder_input);
                            op_history_record(new BRepTools_History(args, *mk));
                        }
                        provenance = op_provenance_encode(shape, current, probe_history.histories());
                    }
                }
            } else if (recording) {
                provenance = op_provenance_encode(shape, current, probe_history.histories());
            }
            if (!join.evidence.empty() && !ordinary->empty()) {
                std::string contact_why;
                if (!contact_evidence_valid(current, join.evidence, contact_why))
                    return fail(FilletFailure::invalid_geometry, contact_why);
            }
            if (!is_watertight(current) || !BRepCheck_Analyzer(current).IsValid() ||
                solid_count(current) == 0)
                return fail(FilletFailure::invalid_geometry, fillet_invalid_solid_reason(current));
            FilletValidationResult validation;
            if (validate.in_process) {
                validation = validate.in_process(current);
            } else if (validate.policy == FilletValidationPolicy::document_solid) {
                return fail(FilletFailure::isolation_failed, "document fillet validation is unavailable");
            } else {
                validation.reason = fillet_solid_rejection(current, deadline);
                if (!validation.reason.empty()) validation.status = FilletValidationStatus::rejected;
            }
            if (validation.status == FilletValidationStatus::timed_out ||
                std::chrono::steady_clock::now() >= deadline)
                return fail(FilletFailure::timed_out,
                            validation.status == FilletValidationStatus::timed_out &&
                                    !validation.reason.empty()
                                ? validation.reason : "fillet validation exceeded its time budget");
            if (validation.status == FilletValidationStatus::rejected)
                return fail(FilletFailure::invalid_geometry,
                            validation.reason.empty() ? "fillet result failed geometric validation"
                                                      : validation.reason);
            if (validation.status != FilletValidationStatus::valid)
                return fail(FilletFailure::invalid_input, "fillet validator returned an invalid outcome");
            sidecar = "0\n" + provenance;
            return current;
        } catch (const Standard_Failure& failure) {
            const char* msg = failure.GetMessageString();
            return fail(FilletFailure::construction_failed,
                        std::string("fillet construction failed: ") + (msg && *msg ? msg : "OCCT failure"));
        }
    };
    chudware::util::IsolatedShapeResult built;
    if (worker) {
        chudware::util::FilletWorkerParameters parameters;
        parameters.radius = r1;
        parameters.radius2 = r2.value_or(0.0);
        parameters.variable = r2.has_value();
        parameters.unequal_requested = unequal_requested;
        parameters.recording = recording;
        parameters.document_validation = validate.policy == FilletValidationPolicy::document_solid;
        parameters.runoff = static_cast<unsigned>(runoff);
        parameters.sweep = static_cast<unsigned>(sweep);
        parameters.deadline = probe_deadline;
        built = worker->run(parameters, sidecar);
    } else {
        built = chudware::util::isolate_shape_build(build, timeout, &sidecar, false);
    }
    if (built.failure == ShapeBuildFailure::timed_out)
        return {{}, FilletFailure::timed_out, "fillet kernel build timed out", {}};
    if (built.failure == ShapeBuildFailure::crash)
        return {{}, FilletFailure::isolation_failed, "fillet kernel build crashed in isolation", {}};
    if (built.failure == ShapeBuildFailure::unavailable || sidecar.size() < 2 || sidecar[1] != '\n')
        return {{}, FilletFailure::isolation_failed, "fillet isolation or result transport failed", {}};
    const int status = sidecar[0] - '0';
    if (status < 0 || status > static_cast<int>(FilletFailure::unsupported_precision))
        return {{}, FilletFailure::isolation_failed, "invalid fillet diagnostic from isolation", {}};
    if (!built.ok) {
        const auto kind = static_cast<FilletFailure>(status);
        if (kind == FilletFailure::construction_failed &&
            sidecar.compare(2, sizeof(kPrecisionSidecar) - 1, kPrecisionSidecar) == 0) {
            std::array<double, 2> precision{0.0, 0.0};
            std::istringstream values(sidecar.substr(2 + sizeof(kPrecisionSidecar) - 1));
            values.imbue(std::locale::classic());
            if (!(values >> precision[0] >> precision[1]) ||
                !std::isfinite(precision[0]) || !std::isfinite(precision[1]) ||
                precision[0] < 0.0 || precision[1] < 0.0 ||
                (precision[0] == 0.0 && precision[1] == 0.0))
                return {{}, FilletFailure::isolation_failed, "invalid fillet precision result", {}};
            return {{}, kind, "fillet endpoint radius is below local model precision", {}, precision};
        }
        return {{}, kind == FilletFailure::none ? FilletFailure::construction_failed : kind,
                sidecar.substr(2), {}};
    }
    if (status != 0 || built.shape.IsNull())
        return {{}, FilletFailure::isolation_failed, "inconsistent fillet isolation result", {}};
    return {std::move(built.shape), FilletFailure::none, {}, sidecar.substr(2)};
}

constexpr int kFilletMaxProbes = 72;
constexpr std::chrono::seconds kFilletSearchBudget{30};
// Largest lower bound on a downward radius correction (see clamp_floor).
constexpr double kFilletMinCorrected = 0.05;

FilletResult fillet_selected(const TopoDS_Shape& shape, double requested,
                             const std::vector<TopoDS_Edge>& edges,
                             std::optional<double> requested2, const FilletValidation& validate) {
    FilletResult result;
    result.requested_radius = result.effective_radius = requested;
    result.requested_radius2 = result.effective_radius2 = requested2;
    result.selected_edges = static_cast<int>(edges.size());
    if (!std::isfinite(requested) || (requested2 && !std::isfinite(*requested2))) {
        result.failure = FilletFailure::invalid_input;
        result.reason = "fillet radii must be finite";
        return result;
    }

    const auto deadline = std::chrono::steady_clock::now() + kFilletSearchBudget;
    FilletSelection selection;
    try {
        result.failure = classify_fillet_selection(shape, edges, requested2.has_value(),
                                                   selection, result.reason);
    } catch (const Standard_Failure&) {
        result.failure = FilletFailure::invalid_selection;
        result.reason = "could not inspect the selected edge topology";
    }
    if (result.failure != FilletFailure::none) return result;
    if (requested2 && requested > selection.endpoint_resolution[0] &&
        *requested2 > selection.endpoint_resolution[1] && requested != *requested2 &&
        std::abs(requested - *requested2) <= Precision::Confusion()) {
        result.failure = FilletFailure::unsupported_precision;
        result.reason = "unequal variable endpoints are below OCCT radius resolution";
        return result;
    }
    const bool isolated = chudware::util::strict_shape_isolation_enabled();
    if (validate.policy != FilletValidationPolicy::basic_solid &&
        validate.policy != FilletValidationPolicy::document_solid) {
        result.failure = FilletFailure::invalid_input;
        result.reason = "unknown fillet validation policy";
        return result;
    }
    if (isolated && validate.policy == FilletValidationPolicy::basic_solid && validate.in_process) {
        result.failure = FilletFailure::isolation_failed;
        result.reason = "custom in-process validation cannot run in an isolated fillet worker";
        return result;
    }
    if (!isolated && validate.policy == FilletValidationPolicy::document_solid &&
        !validate.in_process) {
        result.failure = FilletFailure::isolation_failed;
        result.reason = "document fillet validation is unavailable";
        return result;
    }
    std::vector<chudware::util::ShapeWorkerEdge> worker_edges;
    if (isolated) {
        try {
            worker_edges.reserve(edges.size());
            for (const TopoDS_Edge& edge : edges) {
                const int index = selection.body_edges.FindIndex(edge);
                if (index <= 0) {
                    result.failure = FilletFailure::isolation_failed;
                    result.reason = "could not map a selected fillet edge for isolation";
                    return result;
                }
                worker_edges.push_back({static_cast<std::uint32_t>(index),
                                        static_cast<std::uint32_t>(edge.Orientation())});
            }
        } catch (...) {
            result.failure = FilletFailure::isolation_failed;
            result.reason = "could not prepare selected fillet edges for isolation";
            return result;
        }
    }
    std::optional<chudware::util::ShapeBuildExecSession> worker;


    FilletProbe best;
    double best1 = 0.0, best2 = 0.0;
    bool have_best = false, terminal = false;
    FilletFailure last_failure = FilletFailure::construction_failed;
    std::string last_reason, requested_reason, runoff_reason, sweep_reason;
    double last_radius = 0.0;
    // Smallest accepted downward correction of a constant radius; set once the body
    // scale is known (a fast-path success never corrects). Variable endpoints keep
    // their precision-bounded search.
    double clamp1 = 0.0;
    const auto mm = [](double value) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%.6g", value);
        return std::string(buf);
    };
    // A budget stop reports what the search had established, not a kernel timeout.
    const auto budget_reason = [&](const std::string& what) {
        std::string out = what + " after " + std::to_string(result.probes) + " probe(s)";
        if (!requested_reason.empty())
            out += "; at the requested " + mm(requested) + " mm: " + requested_reason;
        if (!last_reason.empty() && last_reason != requested_reason)
            out += "; at " + mm(last_radius) + " mm: " + last_reason;
        return out;
    };
    const auto& precision_bound = selection.endpoint_resolution;
    std::array<double, 2> last_required{0.0, 0.0};
    struct ObservedUpper { double radius = 0.0, other = 0.0; bool present = false; };
    std::array<ObservedUpper, 2> upper;
    std::array<double, kFilletMaxProbes> constant_failures;
    int constant_failure_count = 0;
    auto attempt = [&](double r1, double r2, int runoff = 0, int sweep = 0) {
        if (result.probes >= kFilletMaxProbes || std::chrono::steady_clock::now() >= deadline) {
            terminal = true;
            result.failure = FilletFailure::budget_exhausted;
            result.reason = budget_reason(result.probes >= kFilletMaxProbes
                ? "fillet search reached its probe limit"
                : "fillet search used its " + std::to_string(kFilletSearchBudget.count()) +
                      " s budget");
            return false;
        }
        last_required = {0.0, 0.0};
        ++result.probes;
        // A run-off or swept build is an extra attempt, not the search: it may use a quarter
        // of the budget, so a slow one cannot starve the radius search that follows.
        const auto limit = runoff || sweep
            ? std::min(deadline, std::chrono::steady_clock::now() + kFilletSearchBudget / 4)
            : deadline;
        FilletProbe probe;
        try {
            if (isolated) {
                // A probe may use the whole remaining budget: one real build plus
                // full-solid validation of a large spline body can take well over
                // five seconds, and a per-probe cap made such features fail or
                // succeed with machine load. An isolated probe is still killed at
                // the search deadline.
                const auto probe_deadline = limit;
                if (!worker) worker.emplace(shape, worker_edges);
                probe = fillet_probe(shape, edges, selection, r1,
                                     requested2 ? std::optional<double>(r2) : std::nullopt,
                                     requested2 && requested != *requested2, validate, limit,
                                     &*worker, probe_deadline, runoff, sweep);
            } else {
                // Unlike an expendable worker, an explicitly in-process build
                // shares its input with the caller and later radius probes.
                BRepBuilderAPI_Copy copy(shape, Standard_True, Standard_True);
                TopoDS_Shape copied_shape = copy.Shape();
                std::vector<TopoDS_Edge> copied_edges;
                copied_edges.reserve(edges.size());
                for (const TopoDS_Edge& edge : edges) {
                    TopoDS_Shape image = copy.ModifiedShape(edge);
                    if (image.IsNull() || image.ShapeType() != TopAbs_EDGE)
                        throw std::runtime_error("unmapped copied edge");
                    image.Orientation(edge.Orientation());
                    copied_edges.push_back(TopoDS::Edge(image));
                }
                FilletSelection copied_selection;
                std::string why;
                if (copied_shape.IsNull() ||
                    classify_fillet_selection(copied_shape, copied_edges, requested2.has_value(),
                                              copied_selection, why) != FilletFailure::none)
                    throw std::runtime_error("invalid copied selection");
                probe = fillet_probe(copied_shape, copied_edges, copied_selection, r1,
                                     requested2 ? std::optional<double>(r2) : std::nullopt,
                                     requested2 && requested != *requested2, validate, limit,
                                     nullptr, limit, runoff, sweep);
            }
        } catch (...) {
            probe = {{}, FilletFailure::isolation_failed, "could not prepare fillet probe input", {}};
        }
        if (probe.failure == FilletFailure::none) {
            best = std::move(probe);
            best1 = r1;
            best2 = r2;
            have_best = true;
            return true;
        }
        if (probe.failure == FilletFailure::timed_out &&
            std::chrono::steady_clock::now() >= deadline) {
            // The search deadline stopped this probe; the build itself was not
            // shown to hang.
            probe.failure = FilletFailure::budget_exhausted;
            probe.reason = budget_reason(
                "fillet search used its " + std::to_string(kFilletSearchBudget.count()) +
                " s budget while building " + mm(r1) + " mm");
        }
        if ((runoff || sweep) && !(probe.failure == FilletFailure::budget_exhausted &&
                                   std::chrono::steady_clock::now() >= deadline)) {
            // A failed run-off or swept blend only says the blend could not be built that
            // way; the radius search that follows is unchanged, so no search state is
            // touched. Even a crash or timeout of this extra build must not end the search.
            if (probe.failure != FilletFailure::unsupported_topology)
                (runoff ? runoff_reason : sweep_reason) = std::move(probe.reason);
            return false;
        }
        last_required = probe.required_precision;
        last_failure = probe.failure;
        last_reason = std::move(probe.reason);
        last_radius = r1;
        if (last_failure == FilletFailure::unsupported_topology ||
            last_failure == FilletFailure::unsupported_precision ||
            last_failure == FilletFailure::invalid_selection ||
            last_failure == FilletFailure::invalid_input ||
            last_failure == FilletFailure::isolation_failed ||
            last_failure == FilletFailure::timed_out ||
            last_failure == FilletFailure::budget_exhausted) {
            terminal = true;
            result.failure = last_failure;
            result.reason = last_reason;
        }
        if (!terminal && !requested2 && r1 > 0.0)
            constant_failures[constant_failure_count++] = r1;
        if (!terminal && requested2 && last_required[0] == 0.0 && last_required[1] == 0.0) {
            const auto record = [&](int axis, double radius, double other) {
                if (!upper[axis].present || upper[axis].other != other || radius < upper[axis].radius)
                    upper[axis] = {radius, other, true};
            };
            record(0, r1, r2);
            record(1, r2, r1);
        }
        return false;
    };
    auto finish = [&]() {
        // A correction to a radius no real part could carry is a failure, not a
        // fillet: such sizes come from a failure that does not depend on radius.
        if (!requested2 && best1 < requested && best1 < clamp1) {
            result.failure = FilletFailure::construction_failed;
            result.reason = "the largest radius that built a valid fillet, " + mm(best1) +
                " mm, is below the " + mm(clamp1) + " mm minimum for a corrected radius";
            if (!requested_reason.empty())
                result.reason += "; at the requested " + mm(requested) + " mm: " + requested_reason;
            return result;
        }
        if (terminal && (result.failure == FilletFailure::timed_out ||
                         result.failure == FilletFailure::budget_exhausted)) {
            // The interrupted probe is not a radius bound. Report a conservative
            // resolution rather than implying the refinement reached its target.
            const auto distance = [](double a, double b) {
                const double half = std::abs(a * 0.5 - b * 0.5);
                const double max = std::numeric_limits<double>::max();
                return half > max * 0.5 ? max : half * 2.0;
            };
            result.tolerance_mm = std::max(result.tolerance_mm, distance(requested, best1));
            if (requested2)
                result.tolerance_mm = std::max(result.tolerance_mm, distance(*requested2, best2));
        }
        result.shape = std::move(best.shape);
        result.effective_radius = best1;
        if (requested2) result.effective_radius2 = best2;
        result.covered_edges = result.selected_edges;
        result.failure = FilletFailure::none;
        result.reason.clear();
        result.document_validated = validate.policy == FilletValidationPolicy::document_solid;
        if (op_history_recording()) op_provenance_record(shape, result.shape, best.provenance);
        return result;
    };

    // A valid request takes exactly one build and does not compute model bounds.
    const bool request_attempted = requested > 0.0 && (!requested2 || *requested2 > 0.0);
    if (request_attempted && attempt(requested, requested2.value_or(0.0))) return finish();
    if (terminal) return result;
    if (request_attempted) requested_reason = last_reason;
    // An inside-corner blend wider than the face it rolls on runs off that face's open edge
    // instead of shrinking: build the request as asked before correcting the radius.
    if (request_attempted && !requested2 && selection.contact.empty()) {
        const FilletRunoffPlan plan =
            fillet_runoff_plan(shape, edges, selection.edge_faces, requested);
        for (const FilletRunoffMode mode : {FilletRunoffMode::ring, FilletRunoffMode::cap,
                                            FilletRunoffMode::separate}) {
            if (!plan.applicable(mode)) continue;
            if (attempt(requested, 0.0, static_cast<int>(mode))) return finish();
            if (terminal) return result;
        }
        if (!runoff_reason.empty())
            requested_reason += "; run-off past the support face: " + runoff_reason;
        // A straight edge between faces constant along it is blended exactly as a swept
        // cross-section where the kernel cannot close its ends: first the edges that meet no
        // other selected edge, then every such edge.
        const auto isolated_edges =
            sweep_blend_choose(edges, selection.edge_faces, SweepBlendScope::isolated);
        const auto prismatic_edges =
            sweep_blend_choose(edges, selection.edge_faces, SweepBlendScope::all);
        if (!isolated_edges.empty()) {
            if (attempt(requested, 0.0, 0, static_cast<int>(SweepBlendScope::isolated)))
                return finish();
            if (terminal) return result;
        }
        if (prismatic_edges.size() > isolated_edges.size()) {
            if (attempt(requested, 0.0, 0, static_cast<int>(SweepBlendScope::all))) return finish();
            if (terminal) return result;
        }
        if (!sweep_reason.empty()) requested_reason += "; swept blend: " + sweep_reason;
    }

    double scale = 0.0, thinnest = 0.0, floor = Precision::Confusion() * 2.0;
    try {
        Bnd_Box bounds;
        BRepBndLib::Add(shape, bounds);
        if (bounds.IsVoid() || bounds.IsWhole()) {
            result.failure = FilletFailure::invalid_geometry;
            result.reason = "cannot determine finite fillet search bounds for this body";
            return result;
        }
        double x0, y0, z0, x1, y1, z1;
        bounds.Get(x0, y0, z0, x1, y1, z1);
        scale = std::max({x1 - x0, y1 - y0, z1 - z0});
        thinnest = std::min({x1 - x0, y1 - y0, z1 - z0});
        for (const TopoDS_Edge& e : edges) floor = std::max(floor, 2.0 * BRep_Tool::Tolerance(e));
    } catch (const Standard_Failure&) {
        result.failure = FilletFailure::invalid_geometry;
        result.reason = "cannot determine fillet search scale";
        return result;
    }
    if (!std::isfinite(scale) || !std::isfinite(floor) || scale <= 0.0 || floor >= scale) {
        result.failure = FilletFailure::invalid_geometry;
        result.reason = "body scale or edge tolerance cannot support a fillet search";
        return result;
    }
    result.tolerance_mm = std::max(floor, scale * 1e-5);
    // A downward correction stops at a size that is still a real fillet: a tenth of
    // the request, 0.1% of the body's thinnest extent, or 0.05 mm, whichever is
    // smallest. A genuinely small or thin body keeps its small corrections.
    const auto clamp_floor = [&](double request) {
        return std::max(floor, std::min({kFilletMinCorrected, thinnest * 1e-3, request * 0.1}));
    };
    if (!requested2) clamp1 = clamp_floor(requested);

    // Search only between bounds observed with the same opposite endpoint.
    // Construction rejection is an observation, not a monotonicity proof.
    auto search_interval = [&](int axis, double fixed, double lower, double high,
                               bool coupled, int max_steps) {
        if (!(high > lower)) return;
        for (int n = 0; n < max_steps && !have_best && !terminal; ++n) {
            const double mid = lower + (high - lower) * 0.5;
            if (!(mid > lower && mid < high)) break;
            if (!coupled && requested2 && requested != *requested2 &&
                std::abs(mid - fixed) <= Precision::Confusion()) {
                if (high > fixed + Precision::Confusion())
                    lower = std::max(lower, fixed + Precision::Confusion());
                else
                    high = std::min(high, fixed - Precision::Confusion());
                continue;
            }
            if (coupled) attempt(mid, mid);
            else if (axis == 0) attempt(mid, fixed);
            else attempt(fixed, mid);
            if (have_best || terminal) break;
            if (!coupled && last_required[1 - axis] > 0.0) break;
            if (last_required[axis] > 0.0 || (coupled && last_required[1 - axis] > 0.0)) {
                lower = std::max(lower, mid);
                lower = std::max(lower, precision_bound[axis]);
                if (coupled) lower = std::max(lower, precision_bound[1 - axis]);
            } else {
                high = mid;
            }
        }
    };

    // When both unequal endpoints are below resolution, neither can be held
    // at its request. Sample their joint interval without collapsing them to
    // the same radius; the ordinary single-axis search handles the other case.
    auto search_pair_interval = [&](double high1, double high2, int max_steps) {
        const double low1 = precision_bound[0], low2 = precision_bound[1];
        if (!(high1 > low1 && high2 > low2)) return;
        double low_fraction = 0.0, high_fraction = 1.0;
        for (int n = 0; n < max_steps && !have_best && !terminal; ++n) {
            const double fraction = low_fraction + (high_fraction - low_fraction) * 0.5;
            if (!(fraction > low_fraction && fraction < high_fraction)) break;
            double a = low1 + fraction * (high1 - low1);
            double b = low2 + fraction * (high2 - low2);
            if (std::abs(a - b) <= Precision::Confusion()) {
                if (requested < *requested2) b = a + floor;
                else a = b + floor;
            }
            if (!(a < high1 && b < high2)) {
                high_fraction = fraction;
                continue;
            }
            attempt(a, b);
            if (have_best || terminal) break;
            if (last_required[0] > 0.0 || last_required[1] > 0.0)
                low_fraction = fraction;
            else
                high_fraction = fraction;
        }
    };

    // A deficient endpoint is first increased from topology tolerance while
    // retaining any already-positive endpoint exactly.
    bool low1 = requested <= floor || (requested2 && requested <= precision_bound[0]);
    bool low2 = requested2 && (*requested2 <= floor || *requested2 <= precision_bound[1]);
    if (requested2 && requested == *requested2 && (low1 || low2)) low1 = low2 = true;
    // A large fixed endpoint may itself be infeasible. Find a sound pair
    // before spending the search budget raising only the deficient end.
    const bool try_pair_first = requested2 &&
        ((low1 && !low2 && *requested2 > scale / 8.0) ||
         (low2 && !low1 && requested > scale / 8.0));
    std::array<double, 2> pair_upper{0.0, 0.0};
    if ((low1 || low2) && !try_pair_first) {
        std::array<bool, 2> bracketed{false, false};
        bool pair_bracketed = false;
        double small = floor;
        for (int n = 0; n < 22 && small <= scale && !have_best && !terminal; ++n) {
            double a = low1 ? (requested2 ? std::max(small, 2.0 * precision_bound[0]) : small)
                            : requested;
            double b = requested2 ? (low2 ? std::max(small, 2.0 * precision_bound[1])
                                           : *requested2) : 0.0;
            if (requested2 && requested == *requested2) {
                a = b = std::max(a, b);
            } else if (requested2 && low1 && low2 &&
                       std::abs(a - b) <= Precision::Confusion()) {
                if (requested < *requested2) b = std::max(b, a + floor);
                else a = std::max(a, b + floor);
            }
            attempt(a, b);
            if (requested2 && !have_best && !terminal &&
                last_required[0] == 0.0 && last_required[1] == 0.0) {
                const bool coupled = requested == *requested2;
                if (low1 && low2 && !coupled && !pair_bracketed) {
                    pair_bracketed = true;
                    pair_upper = {a, b};
                    search_pair_interval(a, b, 1);
                }
                if (low1 && !bracketed[0] && !have_best && !terminal) {
                    bracketed[0] = true;
                    const double lower = coupled ? std::max(precision_bound[0], precision_bound[1])
                                                 : precision_bound[0];
                    search_interval(0, b, lower, a, coupled, 1);
                }
                if (low2 && !coupled && !bracketed[1] && !have_best && !terminal) {
                    bracketed[1] = true;
                    search_interval(1, a, precision_bound[1], b, false, 1);
                }
            }
            if (requested2) {
                low1 = low1 || requested <= precision_bound[0];
                low2 = low2 || *requested2 <= precision_bound[1];
                if (requested == *requested2 && (low1 || low2)) low1 = low2 = true;
            }
            if (small > scale / 2.0) break;
            small *= 2.0;
        }
    }
    if (requested2 && !have_best && !terminal && (low1 || low2) && !try_pair_first) {
        const bool coupled = requested == *requested2;
        if (low1 && low2 && !coupled && pair_upper[0] > 0.0 && pair_upper[1] > 0.0)
            search_pair_interval(pair_upper[0], pair_upper[1], 16);
        if (low1 && !have_best && !terminal && upper[0].present) {
            const ObservedUpper observed = upper[0];
            const double lower = coupled ? std::max(precision_bound[0], precision_bound[1])
                                         : precision_bound[0];
            search_interval(0, observed.other, lower, observed.radius, coupled, 16);
        }
        if (low2 && !coupled && !have_best && !terminal && upper[1].present) {
            const ObservedUpper observed = upper[1];
            search_interval(1, observed.other, precision_bound[1], observed.radius, false, 16);
        }
    }
    if (terminal) return result;

    // An extent-scaled seed avoids halving DBL_MAX hundreds of times. This is
    // just an observed candidate, not a claim that all feasible sets are monotone.
    if (!have_best) {
        double a = std::max(floor, std::min(requested > 0 ? requested : scale / 8.0, scale / 8.0));
        double b = requested2
            ? std::max(floor, std::min(*requested2 > 0 ? *requested2 : scale / 8.0, scale / 8.0))
            : 0.0;
        if (try_pair_first) {
            if (low1) a = std::max(floor, b * 0.5);
            else b = std::max(floor, a * 0.5);
        }
        // Constant-radius feasibility can have gaps: halve to find small
        // candidates, but also sample between two rejected consecutive sizes.
        // Avoid spending a second probe on the already-rejected request.
        bool skip_requested = !requested2 && request_attempted && !low1 && a == requested;
        double previous_failed = !requested2 && request_attempted && !low1 &&
                                 a < requested && a >= requested * 0.5 ? requested : 0.0;
        int preserved_axis_steps = 0;
        while (!have_best && !terminal) {
            if (requested2 && requested != *requested2 &&
                std::abs(a - b) <= Precision::Confusion()) {
                if (requested < *requested2) b = a + floor;
                else a = b + floor;
            }
            if (skip_requested) skip_requested = false;
            else attempt(a, b);
            if (terminal || have_best) break;
            if (!requested2 && previous_failed > a) {
                const double mid = a + (previous_failed - a) * 0.5;
                if (mid > a && mid < previous_failed) attempt(mid, 0.0);
                if (terminal || have_best) break;
            }
            if (!requested2) previous_failed = a;
            if (requested2 && (last_required[0] > 0.0 || last_required[1] > 0.0)) {
                const bool need_first = last_required[0] > 0.0;
                const bool need_second = last_required[1] > 0.0;
                const bool coupled = requested == *requested2;
                bool searched = false;
                if (coupled && upper[0].present && upper[0].other == upper[0].radius) {
                    const ObservedUpper observed = upper[0];
                    search_interval(0, observed.other, std::max(precision_bound[0], precision_bound[1]),
                                    observed.radius, true, 16);
                    searched = true;
                } else if (!coupled && need_first && need_second && upper[0].present) {
                    const ObservedUpper observed = upper[0];
                    search_pair_interval(observed.radius, observed.other, 16);
                    searched = true;
                } else if (!coupled && need_first && !need_second &&
                           upper[0].present && upper[0].other == b) {
                    const ObservedUpper observed = upper[0];
                    search_interval(0, b, precision_bound[0], observed.radius, false, 16);
                    searched = true;
                } else if (!coupled && need_second && !need_first &&
                           upper[1].present && upper[1].other == a) {
                    const ObservedUpper observed = upper[1];
                    search_interval(1, a, precision_bound[1], observed.radius, false, 16);
                    searched = true;
                }
                if (terminal || have_best) break;
                if (need_first) a = std::max(a, 2.0 * precision_bound[0]);
                if (need_second) b = std::max(b, 2.0 * precision_bound[1]);
                if (coupled) {
                    a = b = std::max(a, b);
                    if (searched) break;
                } else if (searched && need_first && !need_second) {
                    if (b <= floor) break;
                    b = std::max(floor, b * 0.5);
                } else if (searched && need_second && !need_first) {
                    if (a <= floor) break;
                    a = std::max(floor, a * 0.5);
                } else if (searched && need_first && need_second) {
                    break;
                }
                continue;
            }
            if (requested2 && requested != *requested2 && preserved_axis_steps < 4) {
                // Try a few corrections with the smaller endpoint unchanged,
                // then descend both: either endpoint may be oversized.
                if (a >= b && a > floor)
                    a = std::max(floor, a * 0.5);
                else if (b > floor)
                    b = std::max(floor, b * 0.5);
                else if (a > floor)
                    a = std::max(floor, a * 0.5);
                else
                    break;
                ++preserved_axis_steps;
            } else {
                if (requested2 && requested != *requested2) {
                    // Probe inside a precision-crossing interval before moving
                    // the opposite endpoint, so its rejection remains a valid
                    // bound for this fixed geometry.
                    const bool cross_first =
                        a > precision_bound[0] && a * 0.5 <= precision_bound[0];
                    const bool cross_second =
                        b > precision_bound[1] && b * 0.5 <= precision_bound[1];
                    if (cross_first && cross_second)
                        search_pair_interval(a, b, 2);
                    else if (cross_first)
                        search_interval(0, b, precision_bound[0], a, false, 2);
                    else if (cross_second)
                        search_interval(1, a, precision_bound[1], b, false, 2);
                    if (terminal || have_best) break;
                }
                const double low = requested2 ? floor : clamp1;
                if (a <= low && (!requested2 || b <= floor)) break;
                a = std::max(low, a * 0.5);
                if (requested2) b = std::max(floor, b * 0.5);
            }
        }
    }
    if (terminal) return result;
    if (!have_best) {
        result.failure = last_failure;
        if (!requested2 && request_attempted && result.probes > 1) {
            result.reason = "no radius from " + mm(requested) + " down to " + mm(clamp1) +
                " mm built a valid fillet on every selected edge (" +
                std::to_string(result.probes) + " probes); at the requested " + mm(requested) +
                " mm: " + requested_reason;
            if (!last_reason.empty() && last_reason != requested_reason)
                result.reason += "; at " + mm(last_radius) + " mm: " + last_reason;
        } else {
            result.reason = last_reason.empty() ? "no feasible all-edge fillet found" : last_reason;
        }
        return result;
    }

    // Equal variable endpoints stay equal. Otherwise coordinate refinement holds
    // the other endpoint fixed. Exponential stepping finds nearby observed
    // failures without treating a crash or timeout as a numerical bound.
    auto refine = [&](bool second, double request) {
        auto current = [&]() { return second ? best2 : best1; };
        auto try_axis = [&](double value) {
            if (requested2 && requested == *requested2) return attempt(value, value);
            return second ? attempt(best1, value) : attempt(value, best2);
        };
        const double goal = std::max(0.0, request);
        if (goal == current()) return;
        // The original constant-radius request was already rejected. Reuse
        // that observation, and the closest larger rejected candidate found
        // during descent, instead of rebuilding the same expensive sizes.
        if (goal > 0.0 && (requested2 || !request_attempted) && try_axis(goal)) return;
        if (terminal) return;
        double bad = goal;
        if (!requested2 && goal > current()) {
            for (int i = 0; i < constant_failure_count; ++i) {
                const double rejected = constant_failures[i];
                if (rejected > current() && rejected < bad) bad = rejected;
            }
        }
        if (goal > current()) {
            while (!terminal) {
                const double good = current();
                const double next = std::min(bad, std::max(good * 2.0, good + result.tolerance_mm));
                if (!(next > good) || next >= bad) break;
                if (!try_axis(next)) {
                    bad = next;
                    break;
                }
            }
        }
        while (!terminal && std::abs(bad - current()) > result.tolerance_mm) {
            const double good = current();
            const double mid = good + (bad - good) * 0.5;
            if (mid == good || mid == bad) break;
            if (!try_axis(mid)) bad = mid;
        }
    };
    refine(false, requested);
    if (requested2 && requested != *requested2 && !terminal) {
        const double before_second = best2;
        refine(true, *requested2);
        if (!terminal && best2 != before_second) {
            const double before_first = best1;
            refine(false, requested);
            if (!terminal && best1 != before_first) refine(true, *requested2);
        }
    }
    if (terminal && (result.failure != FilletFailure::timed_out &&
                     result.failure != FilletFailure::budget_exhausted)) return result;
    return finish();
}

}  // namespace

int fillet_worker_main(int argc, char** argv, FilletValidationFn document_validate) {
    using chudware::util::FilletWorkerParameters;
    using chudware::util::IsolatedShapeResult;
    using chudware::util::ShapeBuildFailure;
    using chudware::util::ShapeWorkerEdge;
    return chudware::util::shape_build_worker_main(argc, argv,
        [document_validate](const TopoDS_Shape& source, const std::vector<ShapeWorkerEdge>& refs,
                            const FilletWorkerParameters& parameters,
                            std::string& sidecar) -> IsolatedShapeResult {
            if (parameters.document_validation && !document_validate) return {};
            FilletSelection selection;
            TopTools_IndexedMapOfShape& body_edges = selection.body_edges;
            TopExp::MapShapes(source, TopAbs_EDGE, body_edges);
            if (refs.empty() || refs.size() > static_cast<std::size_t>(body_edges.Extent()))
                return {};
            std::vector<TopoDS_Edge> edges;
            edges.reserve(refs.size());
            for (const auto& ref : refs) {
                if (ref.index == 0 || ref.index > static_cast<std::uint32_t>(body_edges.Extent()) ||
                    ref.orientation > static_cast<std::uint32_t>(TopAbs_EXTERNAL)) return {};
                TopoDS_Edge edge = TopoDS::Edge(body_edges(static_cast<int>(ref.index)));
                edge.Orientation(static_cast<TopAbs_Orientation>(ref.orientation));
                edges.push_back(edge);
            }
            std::string why;
            const FilletFailure failure = classify_fillet_selection(
                source, edges, parameters.variable, selection, why);
            if (failure != FilletFailure::none) {
                sidecar = std::to_string(static_cast<int>(failure)) + '\n' + why;
                return {false, {}, ShapeBuildFailure::construction};
            }
            const FilletValidation validation{
                parameters.document_validation ? FilletValidationPolicy::document_solid
                                               : FilletValidationPolicy::basic_solid,
                parameters.document_validation ? document_validate : nullptr};
            OpHistoryScope history(parameters.recording);
            FilletProbe probe = fillet_probe(
                source, edges, selection, parameters.radius,
                parameters.variable ? std::optional<double>(parameters.radius2) : std::nullopt,
                parameters.unequal_requested, validation, parameters.deadline, nullptr,
                parameters.deadline, static_cast<int>(parameters.runoff),
                static_cast<int>(parameters.sweep));
            if (probe.failure == FilletFailure::none) {
                if (probe.shape.IsNull()) return {};
                sidecar = "0\n" + probe.provenance;
                return {true, std::move(probe.shape), ShapeBuildFailure::none};
            }
            if (probe.required_precision[0] > 0.0 || probe.required_precision[1] > 0.0) {
                sidecar = fillet_precision_sidecar(probe.required_precision[0],
                                                   probe.required_precision[1]);
            } else {
                sidecar = std::to_string(static_cast<int>(probe.failure)) + '\n' + probe.reason;
            }
            return {false, {}, ShapeBuildFailure::construction};
        });
}

FilletResult fillet_edges(const TopoDS_Shape& shape, double radius,
                          std::optional<Eigen::Vector3d> dir,
                          std::optional<double> radius2, const FilletValidation& validate) {
    if (dir && (!dir->allFinite() || dir->norm() < 1e-12)) {
        FilletResult result;
        result.requested_radius = result.effective_radius = radius;
        result.requested_radius2 = result.effective_radius2 = radius2;
        result.failure = FilletFailure::invalid_input;
        result.reason = "fillet direction must be finite and nonzero";
        return result;
    }
    try {
        return fillet_selected(shape, radius, shape.IsNull() ? std::vector<TopoDS_Edge>()
                                                            : select_blendable(shape, dir),
                               radius2, validate);
    } catch (const Standard_Failure&) {
        FilletResult result;
        result.requested_radius = result.effective_radius = radius;
        result.requested_radius2 = result.effective_radius2 = radius2;
        result.failure = FilletFailure::invalid_selection;
        result.reason = "could not enumerate fillet edges";
        return result;
    }
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

// Explicit chamfer picks retain their historical blendable-edge filter.

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

FilletResult fillet_edges(const TopoDS_Shape& shape, double radius,
                          const std::vector<TopoDS_Edge>& edges,
                          std::optional<double> radius2, const FilletValidation& validate) {
    return fillet_selected(shape, radius, edges, radius2, validate);
}

TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           const std::vector<TopoDS_Edge>& edges, int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    // Symmetric chamfer: the single-distance Add(d, edge) sets both legs to d.
    return blend_drop_failing<BRepFilletAPI_MakeChamfer>(
        shape, blendable_only(shape, edges),
        [d](BRepFilletAPI_MakeChamfer& mk, const TopoDS_Edge& e) { mk.Add(d, e); }, n_dropped, d,
        [d](const TopoDS_Edge&) {
            SweepBlendProfile profile;
            profile.kind = SweepBlendProfile::Kind::chamfer;
            profile.size = profile.size2 = d;
            return profile;
        });
}


TopoDS_Shape chamfer_edges_two_distance(const TopoDS_Shape& shape, double d1, double d2,
                                        const std::vector<TopoDS_Edge>& edges, int* n_dropped) {
    if (edges.empty()) return TopoDS_Shape();
    // The asymmetric Add needs the reference FACE the d1 leg is measured on (build123d
    // chamfer(length,length2) picks the first incident face). Build an edge->faces map, then route
    // through the SAME drop-failing builder as symmetric chamfer so a too-tight edge is
    // dropped (and reported) instead of nuking the whole feature.
    TopTools_IndexedDataMapOfShapeListOfShape ef;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, ef);
    return blend_drop_failing<BRepFilletAPI_MakeChamfer>(
        shape, blendable_only(shape, edges),
        [d1, d2, &ef](BRepFilletAPI_MakeChamfer& mk, const TopoDS_Edge& e) {
            if (ef.Contains(e) && !ef.FindFromKey(e).IsEmpty())
                mk.Add(d1, d2, e, TopoDS::Face(ef.FindFromKey(e).First()));
        },
        n_dropped, std::max(d1, d2), [d1, d2, &ef](const TopoDS_Edge& e) {
            SweepBlendProfile profile;
            profile.kind = SweepBlendProfile::Kind::chamfer;
            profile.size = d1;
            profile.size2 = d2;
            if (ef.Contains(e) && !ef.FindFromKey(e).IsEmpty())
                profile.has_reference = sweep_blend_face_normal(
                    TopoDS::Face(ef.FindFromKey(e).First()), e, profile.reference_normal);
            return profile;
        });
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
        n_dropped, d, [d, angle_rad, &edge_faces, &reference_face](const TopoDS_Edge& edge) {
            SweepBlendProfile profile;
            profile.kind = SweepBlendProfile::Kind::chamfer_angle;
            profile.size = d;
            profile.size2 = angle_rad;
            if (edge_faces.Contains(edge) && !edge_faces.FindFromKey(edge).IsEmpty()) {
                const TopoDS_Face& face = reference_face
                    ? *reference_face : TopoDS::Face(edge_faces.FindFromKey(edge).First());
                profile.has_reference = sweep_blend_face_normal(face, edge, profile.reference_normal);
            }
            return profile;
        });
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
                    std::max(1e-6, corner.volume * 1e-4) || !is_watertight(next))
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
    if (!is_watertight(out)) return "result is an open (non-watertight) shell";
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
