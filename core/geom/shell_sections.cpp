#include "geom/shell_sections.h"
#include "geom/booleans.h"
#include "geom/face_normal.h"
#include "geom/op_history.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <deque>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepBndLib.hxx>
#include <BRepExtrema_ExtPC.hxx>
#include <BRepExtrema_ExtPF.hxx>
#include <Bnd_Box.hxx>
#include <BRepGProp.hxx>
#include <BRepOffsetAPI_ThruSections.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepTopAdaptor_FClass2d.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <GCPnts_AbscissaPoint.hxx>
#include <GProp_GProps.hxx>
#include <GeomAPI_ExtremaCurveCurve.hxx>
#include <GeomAPI_Interpolate.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <GeomAdaptor_Curve.hxx>
#include <GeomConvert.hxx>
#include <GeomConvert_CompCurveToBSplineCurve.hxx>
#include <Geom_BSplineCurve.hxx>
#include <Geom_Circle.hxx>
#include <Geom_Line.hxx>
#include <Geom_TrimmedCurve.hxx>
#include <ShapeFix_Solid.hxx>
#include <Standard_Failure.hxx>
#include <TColStd_HArray1OfReal.hxx>
#include <TColgp_HArray1OfPnt.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopTools_MapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax3.hxx>
#include <gp_Ax2.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Pln.hxx>

namespace geom {

namespace {

// Heights closer than this are one level (mm). Vertex levels of a modelled part are exact
// to the kernel's 1e-7; this leaves room for STEP-imported bodies.
constexpr double kLevelTol = 1e-5;
// Stations per freeform (non-prismatic) segment. The cavity surface interpolates the exact
// sections between stations; 20 intervals keep the wall within 0.1% of t on the loft duct.
constexpr int kFreeformIntervals = 20;
// Points each inner side curve is resampled to, so every station's curve for a side shares
// one knot vector and the lofted cavity stays a compact surface.
constexpr int kSidePoints = 17;
constexpr int kRingPoints = 48;
// Accepted wall error for the self-check, as a fraction of t. Away from band junctions the
// lofted wall is within ~0.01% of t; at a junction an exact prismatic band ends at the
// junction level while the true wall kinks t·tan(θ/2) off it (~1% of t on a loft duct's
// collars), so this catches a wrong cavity without rejecting that sliver.
constexpr double kWallTolFrac = 0.02;
// Height of the ruled transition kept at each junction end of a band, as a fraction of t.
constexpr double kTransitionFrac = 0.5;
// Ruled steps across that transition: the true wall's kink lies somewhere inside it, and
// each step bounds how far a straight ruling can cut across the kink.
constexpr int kTransitionSteps = 4;

struct Axis {
    gp_Pnt o;
    gp_Dir d, x, y;
    double h(const gp_Pnt& p) const { return gp_Vec(o, p).Dot(gp_Vec(d)); }
    gp_Pln plane(double z) const { return gp_Pln(gp_Ax3(o.Translated(gp_Vec(d) * z), d, x)); }
    // In-plane inward normal of a CCW loop with tangent `tan` (interior on the left).
    gp_Vec inward(const gp_Vec& tan) const {
        gp_Vec flat = tan - gp_Vec(d) * tan.Dot(gp_Vec(d));
        gp_Vec m = gp_Vec(d).Crossed(flat);
        const double n = m.Magnitude();
        return n > 1e-12 ? m / n : gp_Vec();
    }
};

// Point-to-skin distance with the closest point and the supporting sub-shapes. Holds one
// initialised extrema per face and edge: the sampling grid of each surface is built once,
// not once per query, and boxes skip entities that cannot beat the best distance so far.
class Dist {
public:
    explicit Dist(const TopoDS_Shape& skin) {
        TopTools_IndexedMapOfShape fm, em, vm;
        TopExp::MapShapes(skin, TopAbs_FACE, fm);
        TopExp::MapShapes(skin, TopAbs_EDGE, em);
        TopExp::MapShapes(skin, TopAbs_VERTEX, vm);
        for (int i = 1; i <= fm.Extent(); ++i) {
            auto& f = faces_.emplace_back();
            f.face = TopoDS::Face(fm(i));
            f.ext.Initialize(f.face, Extrema_ExtFlag_MIN);
            BRepBndLib::Add(f.face, f.box);
        }
        for (int i = 1; i <= em.Extent(); ++i) {
            const TopoDS_Edge& e = TopoDS::Edge(em(i));
            if (BRep_Tool::Degenerated(e)) continue;
            auto& x = edges_.emplace_back();
            x.edge = e;
            x.ext.Initialize(e);
            BRepBndLib::Add(e, x.box);
        }
        for (int i = 1; i <= vm.Extent(); ++i)
            verts_.emplace_back(TopoDS::Vertex(vm(i)), BRep_Tool::Pnt(TopoDS::Vertex(vm(i))));
    }
    // Distance from p to the skin; d is -1 when no extremum was found (returns false). The
    // gradient (unit, skin -> p) is set only when d > 0. `supports` lists every face, edge
    // or vertex the minimum is attained on.
    bool at(const gp_Pnt& p, double& d, gp_Vec& grad, std::vector<TopoDS_Shape>* supports = nullptr) {
        d = -1.0;
        grad = gp_Vec();
        struct Hit { double d; gp_Pnt foot; TopoDS_Shape support; };
        std::vector<Hit> hits;
        double best = 1e300;
        auto offer = [&](double dist, const gp_Pnt& foot, const TopoDS_Shape& s) {
            hits.push_back({dist, foot, s});
            best = std::min(best, dist);
        };
        try {
            for (const auto& [v, q] : verts_) offer(p.Distance(q), q, v);
            Bnd_Box pb;
            pb.Add(p);
            const TopoDS_Vertex pv = BRepBuilderAPI_MakeVertex(p).Vertex();
            for (auto& x : edges_) {
                if (x.box.Distance(pb) > best + kSameDist) continue;
                x.ext.Perform(pv);
                if (!x.ext.IsDone()) continue;
                for (int i = 1; i <= x.ext.NbExt(); ++i)
                    if (x.ext.IsMin(i)) offer(std::sqrt(x.ext.SquareDistance(i)), x.ext.Point(i), x.edge);
            }
            for (auto& f : faces_) {
                if (f.box.Distance(pb) > best + kSameDist) continue;
                f.ext.Perform(pv, f.face);
                if (!f.ext.IsDone()) continue;
                for (int i = 1; i <= f.ext.NbExt(); ++i)
                    offer(std::sqrt(f.ext.SquareDistance(i)), f.ext.Point(i), f.face);
            }
        } catch (const Standard_Failure&) {
            return false;
        }
        if (hits.empty()) return false;
        d = best;
        const Hit* first = nullptr;
        if (supports) supports->clear();
        for (const Hit& h : hits) {
            if (h.d > best + kSameDist) continue;
            if (!first) first = &h;
            if (supports) supports->push_back(h.support);
        }
        if (d > 1e-12) grad = gp_Vec(first->foot, p) / d;
        return true;
    }

private:
    // Distances within this of the minimum are the same minimum (a point equidistant from
    // a face and its boundary edge reports both).
    static constexpr double kSameDist = 1e-7;
    struct FaceX {
        TopoDS_Face face;
        BRepExtrema_ExtPF ext;
        Bnd_Box box;
    };
    struct EdgeX {
        TopoDS_Edge edge;
        BRepExtrema_ExtPC ext;
        Bnd_Box box;
    };
    std::deque<FaceX> faces_;
    std::deque<EdgeX> edges_;
    std::vector<std::pair<TopoDS_Vertex, gp_Pnt>> verts_;
};

TopoDS_Compound compound_of(const std::vector<TopoDS_Face>& faces) {
    TopoDS_Compound c;
    BRep_Builder b;
    b.MakeCompound(c);
    for (const TopoDS_Face& f : faces) b.Add(c, f);
    return c;
}

void edge_height_range(const TopoDS_Edge& e, const Axis& ax, double& lo, double& hi) {
    BRepAdaptor_Curve c(e);
    for (int k = 0; k <= 32; ++k) {
        const double u = c.FirstParameter() + (c.LastParameter() - c.FirstParameter()) * k / 32.0;
        const double z = ax.h(c.Value(u));
        lo = std::min(lo, z);
        hi = std::max(hi, z);
    }
}

void face_height_range(const TopoDS_Face& f, const Axis& ax, double& lo, double& hi) {
    lo = 1e300;
    hi = -1e300;
    for (TopExp_Explorer e(f, TopAbs_EDGE); e.More(); e.Next()) {
        const TopoDS_Edge& ed = TopoDS::Edge(e.Current());
        if (!BRep_Tool::Degenerated(ed)) edge_height_range(ed, ax, lo, hi);
    }
}

// Lateral faces whose slices are constant along the axis: their segment needs only its two
// end sections, joined by ruled faces.
bool prismatic(const TopoDS_Face& f, const Axis& ax) {
    BRepAdaptor_Surface s(f, Standard_False);
    switch (s.GetType()) {
        case GeomAbs_Plane: return std::abs(s.Plane().Axis().Direction().Dot(ax.d)) < 1e-9;
        case GeomAbs_Cylinder: return s.Cylinder().Axis().Direction().IsParallel(ax.d, 1e-9);
        case GeomAbs_SurfaceOfExtrusion: return s.Direction().IsParallel(ax.d, 1e-9);
        default: return false;
    }
}

struct Segment {
    double lo = 0.0, hi = 0.0;
    std::vector<int> faces;  // indices into the lateral list, ascending
    bool prism = true;
};

// One outer curve of a station loop, in loop order.
struct Piece {
    TopoDS_Edge edge;
    bool reversed = false;
    bool body_edge = false;  // a real edge of the body (end / junction station)
    gp_Pnt first() const {
        BRepAdaptor_Curve c(edge);
        return c.Value(reversed ? c.LastParameter() : c.FirstParameter());
    }
    gp_Pnt last() const {
        BRepAdaptor_Curve c(edge);
        return c.Value(reversed ? c.FirstParameter() : c.LastParameter());
    }
};

// A lateral face's slice at one station, in loop order.
struct Side {
    int face = -1;  // lateral index
    std::vector<Piece> pieces;
    gp_Pnt first() const { return pieces.front().first(); }
    gp_Pnt last() const { return pieces.back().last(); }
    void reverse() {
        std::reverse(pieces.begin(), pieces.end());
        for (Piece& p : pieces) p.reversed = !p.reversed;
    }
};

// Order edges into one path (open or closed) by matching endpoints.
std::optional<std::vector<Piece>> chain(std::vector<TopoDS_Edge> edges, bool body_edges, double tol) {
    std::vector<Piece> out;
    if (edges.empty()) return std::nullopt;
    out.push_back({edges.front(), false, body_edges});
    edges.erase(edges.begin());
    while (!edges.empty()) {
        bool grown = false;
        for (std::size_t i = 0; i < edges.size() && !grown; ++i) {
            Piece p{edges[i], false, body_edges};
            Piece r{edges[i], true, body_edges};
            if (out.back().last().Distance(p.first()) < tol) { out.push_back(p); grown = true; }
            else if (out.back().last().Distance(r.first()) < tol) { out.push_back(r); grown = true; }
            else if (out.front().first().Distance(p.last()) < tol) { out.insert(out.begin(), p); grown = true; }
            else if (out.front().first().Distance(r.last()) < tol) { out.insert(out.begin(), r); grown = true; }
            if (grown) edges.erase(edges.begin() + static_cast<long>(i));
        }
        if (!grown) return std::nullopt;
    }
    return out;
}

struct Context {
    double t = 0.0;
    Axis ax;
    std::vector<TopoDS_Face> laterals;
    std::vector<TopTools_MapOfShape> own;  // per lateral: entities whose nearness is "its own"
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    std::optional<Dist> skin;  // all lateral faces
    // Inner vertex / edge per body vertex / edge at end and junction stations: the two
    // segments meeting at a junction loft from the very same section geometry.
    std::vector<std::pair<TopoDS_Vertex, TopoDS_Vertex>> vertex_cache;
    std::vector<std::pair<TopoDS_Edge, TopoDS_Edge>> edge_cache;
    double scale = 1.0;
    std::string why;
};

int lateral_index(const Context& cx, const TopoDS_Shape& f) {
    for (std::size_t i = 0; i < cx.laterals.size(); ++i)
        if (cx.laterals[i].IsSame(f)) return static_cast<int>(i);
    return -1;
}

// The slice of lateral face `fi` at height z: its boundary edges when z is one of its end
// levels, else a plane section.
std::optional<Side> face_slice(Context& cx, int fi, double z, double flo, double fhi) {
    const TopoDS_Face& f = cx.laterals[static_cast<std::size_t>(fi)];
    std::vector<TopoDS_Edge> edges;
    bool body_edges = false;
    if (std::abs(z - flo) < kLevelTol || std::abs(z - fhi) < kLevelTol) {
        body_edges = true;
        TopTools_IndexedMapOfShape em;
        TopExp::MapShapes(f, TopAbs_EDGE, em);
        for (int i = 1; i <= em.Extent(); ++i) {
            const TopoDS_Edge& e = TopoDS::Edge(em(i));
            if (BRep_Tool::Degenerated(e)) continue;
            double lo = 1e300, hi = -1e300;
            edge_height_range(e, cx.ax, lo, hi);
            if (std::abs(lo - z) < kLevelTol && std::abs(hi - z) < kLevelTol) edges.push_back(e);
        }
    } else {
        try {
            BRepAlgoAPI_Section sec(f, cx.ax.plane(z), Standard_False);
            sec.Approximation(Standard_True);
            sec.ComputePCurveOn1(Standard_False);
            sec.Build();
            if (!sec.IsDone()) return std::nullopt;
            for (TopExp_Explorer e(sec.Shape(), TopAbs_EDGE); e.More(); e.Next())
                edges.push_back(TopoDS::Edge(e.Current()));
        } catch (const Standard_Failure&) {
            return std::nullopt;
        }
    }
    auto pieces = chain(std::move(edges), body_edges, 1e-4 * cx.scale);
    if (!pieces) return std::nullopt;
    Side s;
    s.face = fi;
    s.pieces = std::move(*pieces);
    return s;
}

// A point of `seam` (an edge closed on a ring face) at height z.
std::optional<gp_Pnt> seam_point(const Context& cx, const TopoDS_Edge& seam, double z) {
    BRepAdaptor_Curve c(seam);
    double a = c.FirstParameter(), b = c.LastParameter();
    double za = cx.ax.h(c.Value(a)) - z, zb = cx.ax.h(c.Value(b)) - z;
    if (za * zb > 0.0) return std::nullopt;
    for (int it = 0; it < 80; ++it) {
        const double m = 0.5 * (a + b);
        const double zm = cx.ax.h(c.Value(m)) - z;
        if ((zm <= 0.0) == (za <= 0.0)) { a = m; za = zm; }
        else { b = m; }
    }
    return c.Value(0.5 * (a + b));
}

// Rotate a closed single-face loop so it starts at the face's seam. A body-edge loop starts
// at the piece leaving the seam vertex; a section loop is split at the seam point.
bool start_ring_at_seam(Context& cx, Side& s, double z) {
    const TopoDS_Face& f = cx.laterals[static_cast<std::size_t>(s.face)];
    TopoDS_Edge seam;
    for (TopExp_Explorer e(f, TopAbs_EDGE); e.More() && seam.IsNull(); e.Next())
        if (BRep_Tool::IsClosed(TopoDS::Edge(e.Current()), f)) seam = TopoDS::Edge(e.Current());
    if (seam.IsNull()) return false;
    const auto sp = seam_point(cx, seam, z);
    if (!sp) return false;
    const double tol = 1e-4 * cx.scale;
    for (std::size_t i = 0; i < s.pieces.size(); ++i) {
        if (s.pieces[i].first().Distance(*sp) < tol) {
            std::rotate(s.pieces.begin(), s.pieces.begin() + static_cast<long>(i), s.pieces.end());
            return true;
        }
    }
    if (s.pieces.size() != 1) return false;
    // One closed section edge starting elsewhere: split it at the seam point.
    const Piece p = s.pieces.front();
    Standard_Real a, b;
    Handle(Geom_Curve) crv = BRep_Tool::Curve(p.edge, a, b);
    if (crv.IsNull()) return false;
    GeomAPI_ProjectPointOnCurve proj(*sp, crv, a, b);
    if (proj.NbPoints() < 1) return false;
    const double u = proj.LowerDistanceParameter();
    if (u - a < 1e-9 || b - u < 1e-9) return true;  // already at an end
    Piece p1{BRepBuilderAPI_MakeEdge(crv, u, b).Edge(), false, false};
    Piece p2{BRepBuilderAPI_MakeEdge(crv, a, u).Edge(), false, false};
    s.pieces = p.reversed ? std::vector<Piece>{Piece{p2.edge, true, false}, Piece{p1.edge, true, false}}
                          : std::vector<Piece>{p1, p2};
    return true;
}

// The station loop of a segment at height z: one side per lateral face, CCW about the axis,
// starting at the segment's first face (a ring starts at its seam).
std::optional<std::vector<Side>> station_loop(Context& cx, const Segment& seg, double z,
                                              const std::vector<std::pair<double, double>>& ranges) {
    std::vector<Side> sides;
    for (int fi : seg.faces) {
        auto s = face_slice(cx, fi, z, ranges[static_cast<std::size_t>(fi)].first,
                            ranges[static_cast<std::size_t>(fi)].second);
        if (!s) {
            cx.why = "a lateral face does not slice into one curve at a station";
            return std::nullopt;
        }
        sides.push_back(std::move(*s));
    }
    const double tol = 1e-4 * cx.scale;
    std::vector<Side> loop;
    loop.push_back(sides.front());
    sides.erase(sides.begin());
    while (!sides.empty()) {
        bool grown = false;
        for (std::size_t i = 0; i < sides.size() && !grown; ++i) {
            if (loop.back().last().Distance(sides[i].first()) < tol) grown = true;
            else if (loop.back().last().Distance(sides[i].last()) < tol) { sides[i].reverse(); grown = true; }
            if (grown) {
                loop.push_back(sides[i]);
                sides.erase(sides.begin() + static_cast<long>(i));
            }
        }
        if (!grown) {
            cx.why = "a station's slice is not one closed loop";
            return std::nullopt;
        }
    }
    if (loop.back().last().Distance(loop.front().first()) > tol) {
        cx.why = "a station's slice is not one closed loop";
        return std::nullopt;
    }
    // Orientation: shoelace over sampled points in the station plane.
    double area2 = 0.0;
    std::vector<gp_Pnt> poly;
    for (const Side& s : loop)
        for (const Piece& p : s.pieces) {
            BRepAdaptor_Curve c(p.edge);
            for (int k = 0; k < 16; ++k) {
                const double w = p.reversed ? 1.0 - k / 16.0 : k / 16.0;
                poly.push_back(c.Value(c.FirstParameter() + (c.LastParameter() - c.FirstParameter()) * w));
            }
        }
    for (std::size_t i = 0; i < poly.size(); ++i) {
        const gp_Vec a(cx.ax.o, poly[i]), b(cx.ax.o, poly[(i + 1) % poly.size()]);
        area2 += gp_Vec(cx.ax.d).Dot(a.Crossed(b));
    }
    if (area2 < 0.0) {
        std::reverse(loop.begin() + 1, loop.end());
        for (Side& s : loop) s.reverse();
    }
    if (loop.size() == 1 && !start_ring_at_seam(cx, loop.front(), z)) {
        cx.why = "a closed lateral face has no seam to start its sections at";
        return std::nullopt;
    }
    return loop;
}

bool supports_owned(const std::vector<TopoDS_Shape>& sup, const TopTools_MapOfShape& own) {
    for (const TopoDS_Shape& s : sup)
        if (!own.Contains(s)) return false;
    return true;
}

// March from outer point q along the inward normal m to the point exactly t from the skin.
// Rejected (nullopt) when the nearest skin is not `own` — the point then belongs to a
// neighbouring side's inner curve, near a corner.
std::optional<gp_Pnt> march(Context& cx, const gp_Pnt& q, const gp_Vec& m,
                            const TopTools_MapOfShape* own) {
    double s = cx.t;
    std::vector<TopoDS_Shape> sup;
    for (int it = 0; it < 40; ++it) {
        const gp_Pnt p = q.Translated(m * s);
        double d;
        gp_Vec g;
        if (!cx.skin->at(p, d, g, &sup) || !(d > 1e-12)) return std::nullopt;
        const double f = d - cx.t;
        if (std::abs(f) < 1e-9 * std::max(1.0, cx.t)) {
            if (own && !supports_owned(sup, *own)) return std::nullopt;
            return p;
        }
        const double gm = g.Dot(m);
        if (gm < 0.3) return std::nullopt;
        s -= f / gm;
        if (!(s > 0.0) || s > 6.0 * cx.t) return std::nullopt;
    }
    return std::nullopt;
}

// The inner corner between two sides: the point t from both face sets. Starts from the
// bisector march (exact boundary point); a 2-D Newton on the two set distances then moves it
// onto the corner unless the corner is near-tangent (ill-conditioned) or fails.
std::optional<gp_Pnt> corner(Context& cx, const gp_Pnt& q, const gp_Vec& ma, const gp_Vec& mb,
                             const std::vector<TopoDS_Face>& set_a,
                             const std::vector<TopoDS_Face>& set_b) {
    gp_Vec mc = ma + mb;
    if (mc.Magnitude() < 1e-6) return std::nullopt;
    mc.Normalize();
    const auto p1 = march(cx, q, mc, nullptr);
    if (!p1) return std::nullopt;
    if (ma.Crossed(mb).Magnitude() < 0.05) return p1;  // within ~3° of tangent
    Dist da(compound_of(set_a)), db(compound_of(set_b));
    gp_Pnt p = *p1;
    const gp_Vec ex(cx.ax.x), ey(cx.ax.y);
    for (int it = 0; it < 30; ++it) {
        double fa, fb;
        gp_Vec ga, gb;
        if (!da.at(p, fa, ga) || !db.at(p, fb, gb) || !(fa > 1e-12) || !(fb > 1e-12)) return p1;
        fa -= cx.t;
        fb -= cx.t;
        if (std::abs(fa) < 1e-9 && std::abs(fb) < 1e-9) {
            double d;
            gp_Vec g;
            if (p.Distance(*p1) < 2.0 * cx.t && cx.skin->at(p, d, g) && d > cx.t - 1e-7) return p;
            return p1;
        }
        const double j11 = ga.Dot(ex), j12 = ga.Dot(ey), j21 = gb.Dot(ex), j22 = gb.Dot(ey);
        const double det = j11 * j22 - j12 * j21;
        if (std::abs(det) < 1e-6) return p1;
        double dx = -(j22 * fa - j12 * fb) / det, dy = -(-j21 * fa + j11 * fb) / det;
        const double step = std::hypot(dx, dy);
        if (step > cx.t) { dx *= cx.t / step; dy *= cx.t / step; }
        p.Translate(ex * dx + ey * dy);
    }
    return p1;
}

// Lateral faces next to a body edge.
std::vector<TopoDS_Face> edge_laterals(const Context& cx, const TopoDS_Edge& e) {
    std::vector<TopoDS_Face> out;
    const int i = cx.edge_faces.FindIndex(e);
    if (i == 0) return out;
    for (const TopoDS_Shape& f : cx.edge_faces(i))
        if (lateral_index(cx, f) >= 0 &&
            std::none_of(out.begin(), out.end(), [&](const TopoDS_Face& g) { return g.IsSame(f); }))
            out.push_back(TopoDS::Face(f));
    return out;
}

gp_Vec piece_tangent(const Piece& p, double u) {
    BRepAdaptor_Curve c(p.edge);
    gp_Pnt pt;
    gp_Vec d1;
    c.D1(u, pt, d1);
    if (p.reversed) d1.Reverse();
    return d1;
}

// Samples per piece: deterministic from the edge alone, so a junction edge yields the
// same inner points for the segments on both sides of it.
int piece_samples(const Piece& p, double t) {
    BRepAdaptor_Curve c(p.edge);
    double len = 0.0;
    try {
        len = GCPnts_AbscissaPoint::Length(c);
    } catch (const Standard_Failure&) {
        len = c.Value(c.FirstParameter()).Distance(c.Value(c.LastParameter()));
    }
    return std::clamp(static_cast<int>(std::ceil(len / std::max(2.0 * t, 1e-3))), 8, 48);
}

TopTools_MapOfShape own_of_edge(const Context& cx, const TopoDS_Edge& e) {
    TopTools_MapOfShape own;
    own.Add(e);
    for (const TopoDS_Face& f : edge_laterals(cx, e)) {
        const int i = lateral_index(cx, f);
        for (TopTools_MapOfShape::Iterator it(cx.own[static_cast<std::size_t>(i)]); it.More(); it.Next())
            own.Add(it.Key());
    }
    return own;
}

Handle(Geom_BSplineCurve) interpolate(const std::vector<gp_Pnt>& pts, bool periodic, int resample) {
    // Drop near-duplicates (a march can land on a corner).
    std::vector<gp_Pnt> p;
    for (const gp_Pnt& q : pts)
        if (p.empty() || p.back().Distance(q) > 1e-5) p.push_back(q);
    if (periodic && p.size() > 2 && p.back().Distance(p.front()) < 1e-5) p.pop_back();
    if (p.size() < 2) return nullptr;
    auto build = [&](const std::vector<gp_Pnt>& q, const std::vector<double>* params) -> Handle(Geom_BSplineCurve) {
        Handle(TColgp_HArray1OfPnt) arr = new TColgp_HArray1OfPnt(1, static_cast<int>(q.size()));
        for (std::size_t i = 0; i < q.size(); ++i) arr->SetValue(static_cast<int>(i) + 1, q[i]);
        std::optional<GeomAPI_Interpolate> ip;
        if (params) {
            Handle(TColStd_HArray1OfReal) par =
                new TColStd_HArray1OfReal(1, static_cast<int>(params->size()));
            for (std::size_t i = 0; i < params->size(); ++i) par->SetValue(static_cast<int>(i) + 1, (*params)[i]);
            ip.emplace(arr, par, periodic, 1e-9);
        } else {
            ip.emplace(arr, periodic, 1e-9);
        }
        ip->Perform();
        return ip->IsDone() ? ip->Curve() : nullptr;
    };
    Handle(Geom_BSplineCurve) first = build(p, nullptr);
    if (first.IsNull()) return nullptr;
    // Resample by arc length with fixed uniform parameters: every station's curve for this
    // side then shares one knot vector.
    GeomAdaptor_Curve ac(first);
    const double len = GCPnts_AbscissaPoint::Length(ac);
    std::vector<gp_Pnt> q;
    std::vector<double> par;
    const int count = periodic ? resample : resample;  // periodic: count distinct points
    for (int i = 0; i < count; ++i) {
        const double frac = periodic ? static_cast<double>(i) / count
                                     : static_cast<double>(i) / (count - 1);
        if (i == 0) { q.push_back(first->Value(first->FirstParameter())); }
        else if (!periodic && i == count - 1) { q.push_back(first->Value(first->LastParameter())); }
        else {
            GCPnts_AbscissaPoint ab(ac, frac * len, first->FirstParameter());
            if (!ab.IsDone()) return first;
            q.push_back(first->Value(ab.Parameter()));
        }
        par.push_back(frac);
    }
    if (periodic) par.push_back(1.0);
    Handle(Geom_BSplineCurve) second = build(q, &par);
    return second.IsNull() ? first : second;
}

std::optional<gp_Pnt> vertex_corner(Context& cx, const Piece& in, const Piece& out_) {
    BRepAdaptor_Curve ci(in.edge), co(out_.edge);
    const double ui = in.reversed ? ci.FirstParameter() : ci.LastParameter();
    const double uo = out_.reversed ? co.LastParameter() : co.FirstParameter();
    const gp_Pnt q = ci.Value(ui);
    const gp_Vec ma = cx.ax.inward(piece_tangent(in, ui)), mb = cx.ax.inward(piece_tangent(out_, uo));
    return corner(cx, q, ma, mb, edge_laterals(cx, in.edge), edge_laterals(cx, out_.edge));
}

// Inner points marched from samples of one piece, in the piece's loop direction.
std::vector<gp_Pnt> march_piece(Context& cx, const Piece& p, const TopTools_MapOfShape& own) {
    std::vector<gp_Pnt> pts;
    BRepAdaptor_Curve c(p.edge);
    const int ns = piece_samples(p, cx.t);
    for (int j = 1; j < ns; ++j) {
        // Sample in the edge's own parameter direction, then emit in loop order.
        const int jj = p.reversed ? ns - j : j;
        const double u = c.FirstParameter() + (c.LastParameter() - c.FirstParameter()) * jj / ns;
        const gp_Vec m = cx.ax.inward(piece_tangent(p, u));
        if (m.Magnitude() < 0.5) continue;
        if (const auto ip = march(cx, c.Value(u), m, &own)) pts.push_back(*ip);
    }
    return pts;
}

// The body vertex two consecutive body-edge pieces share.
TopoDS_Vertex shared_vertex(const Piece& in, const Piece& out_) {
    BRepAdaptor_Curve ci(in.edge);
    const gp_Pnt q = ci.Value(in.reversed ? ci.FirstParameter() : ci.LastParameter());
    TopoDS_Vertex best;
    double bd = 1e300;
    for (TopExp_Explorer e(out_.edge, TopAbs_VERTEX); e.More(); e.Next()) {
        const TopoDS_Vertex& v = TopoDS::Vertex(e.Current());
        const double d = BRep_Tool::Pnt(v).Distance(q);
        if (d < bd) { bd = d; best = v; }
    }
    return best;
}

std::optional<TopoDS_Vertex> cached_inner_vertex(const Context& cx, const TopoDS_Vertex& bv) {
    for (const auto& kv : cx.vertex_cache)
        if (kv.first.IsSame(bv)) return kv.second;
    return std::nullopt;
}

// Inner vertex at a body vertex of a junction/end loop, cached so both segments at the
// junction use the same one.
std::optional<TopoDS_Vertex> inner_vertex(Context& cx, const Piece& in, const Piece& out_) {
    const TopoDS_Vertex bv = shared_vertex(in, out_);
    if (bv.IsNull()) return std::nullopt;
    if (auto hit = cached_inner_vertex(cx, bv)) return hit;
    const auto p = vertex_corner(cx, in, out_);
    if (!p) return std::nullopt;
    const TopoDS_Vertex iv = BRepBuilderAPI_MakeVertex(*p).Vertex();
    cx.vertex_cache.emplace_back(bv, iv);
    return iv;
}

// Inner edge of one body edge (in the body edge's parameter direction), cached so both
// segments at a junction loft to the very same curve.
std::optional<TopoDS_Edge> inner_edge(Context& cx, const Piece& p) {
    for (const auto& kv : cx.edge_cache)
        if (kv.first.IsSame(p.edge)) return kv.second;
    BRepAdaptor_Curve c(p.edge);
    const gp_Pnt p0 = c.Value(c.FirstParameter());
    TopoDS_Vertex a, b;
    TopExp::Vertices(p.edge, a, b);
    if (BRep_Tool::Pnt(a).Distance(p0) > BRep_Tool::Pnt(b).Distance(p0)) std::swap(a, b);
    const auto ia = cached_inner_vertex(cx, a), ib = cached_inner_vertex(cx, b);
    if (!ia || !ib) return std::nullopt;
    // March with the loop's inward side, then store the points in parameter order.
    std::vector<gp_Pnt> pts = march_piece(cx, p, own_of_edge(cx, p.edge));
    if (p.reversed) std::reverse(pts.begin(), pts.end());
    pts.insert(pts.begin(), BRep_Tool::Pnt(*ia));
    Handle(Geom_BSplineCurve) crv;
    if (a.IsSame(b)) {
        crv = interpolate(pts, true, kRingPoints);
    } else {
        pts.push_back(BRep_Tool::Pnt(*ib));
        crv = interpolate(pts, false, kSidePoints);
    }
    if (crv.IsNull()) return std::nullopt;
    BRepBuilderAPI_MakeEdge me(crv, *ia, *ib);
    if (!me.IsDone()) return std::nullopt;
    cx.edge_cache.emplace_back(p.edge, me.Edge());
    return me.Edge();
}

// Join oriented edges into one B-spline edge between `v0` and `v1` (equal for a ring).
std::optional<TopoDS_Edge> join_edges(const std::vector<TopoDS_Edge>& edges, const TopoDS_Vertex& v0,
                                      const TopoDS_Vertex& v1) {
    if (edges.size() == 1) return edges.front();
    GeomConvert_CompCurveToBSplineCurve cc;
    for (const TopoDS_Edge& e : edges) {
        Standard_Real a, b;
        Handle(Geom_Curve) g = BRep_Tool::Curve(e, a, b);
        if (g.IsNull()) return std::nullopt;
        Handle(Geom_BSplineCurve) bs = GeomConvert::CurveToBSplineCurve(new Geom_TrimmedCurve(g, a, b));
        if (bs.IsNull()) return std::nullopt;
        if (bs->IsPeriodic()) bs->SetNotPeriodic();
        if (e.Orientation() == TopAbs_REVERSED) bs->Reverse();
        if (!cc.Add(bs, 1e-6, Standard_True)) return std::nullopt;
    }
    Handle(Geom_BSplineCurve) c = cc.BSplineCurve();
    BRepBuilderAPI_MakeEdge me(c, v0, v1, c->FirstParameter(), c->LastParameter());
    if (!me.IsDone()) return std::nullopt;
    return me.Edge();
}

// --- exact sections of prismatic bands ------------------------------------------------

// The exact inner curve of a prismatic lateral face at height z — its slice offset by t —
// parameterised in the loop's direction: a line for a plane, a circle for a cylinder (axis
// +d for a boss the loop rounds counter-clockwise, -d for a bore it rounds clockwise). Null
// for any other surface.
Handle(Geom_Curve) prism_inner_curve(const Context& cx, const TopoDS_Face& f, double z) {
    BRepAdaptor_Surface s(f, Standard_True);
    if (s.GetType() == GeomAbs_Plane) {
        const gp_Pln pl = s.Plane();
        const gp_Dir n = chudware::geom::plane_outward_normal(f, pl);
        gp_Pnt p0 = pl.Location();
        p0.Translate(gp_Vec(cx.ax.d) * (z - cx.ax.h(p0)) - gp_Vec(n) * cx.t);
        // Loop tangent T with the interior on its left: d x T = -n, so T = d x n.
        return new Geom_Line(p0, gp_Dir(gp_Vec(cx.ax.d).Crossed(gp_Vec(n))));
    }
    if (s.GetType() == GeomAbs_Cylinder) {
        const gp_Cylinder cyl = s.Cylinder();
        const bool boss = chudware::geom::surface_outward_sign(f, cyl.Position()) > 0.0;
        const double r = cyl.Radius() + (boss ? -cx.t : cx.t);
        if (!(r > 1e-6)) return nullptr;
        gp_Pnt c = cyl.Location();
        c.Translate(gp_Vec(cx.ax.d) * (z - cx.ax.h(c)));
        return new Geom_Circle(gp_Ax2(c, boss ? cx.ax.d : cx.ax.d.Reversed()), r);
    }
    return nullptr;
}

std::optional<double> param_on(const Handle(Geom_Curve)& c, const gp_Pnt& p) {
    GeomAPI_ProjectPointOnCurve pr(p, c);
    if (pr.NbPoints() < 1) return std::nullopt;
    return pr.LowerDistanceParameter();
}

// Parameter range on `c` from u0 to u1 in the loop's direction (a circle wraps).
std::optional<std::pair<double, double>> loop_range(const Handle(Geom_Curve)& c, double u0, double u1,
                                                    bool full) {
    if (c->IsPeriodic()) {
        const double per = c->Period();
        while (u1 <= u0 + 1e-12) u1 += per;
        while (u1 > u0 + per + 1e-12) u1 -= per;
        if (full) u1 = u0 + per;
    }
    if (!(u1 > u0 + 1e-9)) return std::nullopt;
    return std::make_pair(u0, u1);
}

// Body vertices of a body-edge piece in loop order.
std::pair<TopoDS_Vertex, TopoDS_Vertex> piece_vertices(const Piece& p) {
    BRepAdaptor_Curve c(p.edge);
    const gp_Pnt p0 = c.Value(c.FirstParameter());
    TopoDS_Vertex a, b;
    TopExp::Vertices(p.edge, a, b);
    if (BRep_Tool::Pnt(a).Distance(p0) > BRep_Tool::Pnt(b).Distance(p0)) std::swap(a, b);
    if (p.reversed) std::swap(a, b);
    return {a, b};
}

// The exact inner section of a prismatic band at height z: offset lines and arcs meeting
// where neighbouring offsets intersect. Nullopt when a side is neither plane nor cylinder.
// At an end/junction station the caches receive the same geometry per body vertex and edge,
// so a freeform band meeting this one lofts from exactly this section.
std::optional<TopoDS_Wire> prism_wire(Context& cx, const std::vector<Side>& loop, double z) {
    const std::size_t n = loop.size();
    std::vector<Handle(Geom_Curve)> curves(n);
    for (std::size_t i = 0; i < n; ++i) {
        curves[i] = prism_inner_curve(cx, cx.laterals[static_cast<std::size_t>(loop[i].face)], z);
        if (curves[i].IsNull()) return std::nullopt;
    }
    // corner i = start of side i (end of side i-1).
    std::vector<gp_Pnt> corners(n);
    for (std::size_t i = 0; i < n; ++i) {
        const gp_Pnt q = loop[i].first();
        if (n == 1) {
            const auto u = param_on(curves[0], q);
            if (!u) return std::nullopt;
            corners[0] = curves[0]->Value(*u);
            continue;
        }
        GeomAPI_ExtremaCurveCurve ex(curves[(i + n - 1) % n], curves[i]);
        std::optional<gp_Pnt> best;
        if (ex.NbExtrema() > 0 && ex.IsParallel()) {
            // Collinear neighbours (no corner): the offset of the outer point.
            if (ex.LowerDistance() < 1e-7 * cx.scale) {
                const auto u = param_on(curves[i], q);
                if (u) best = curves[i]->Value(*u);
            }
        } else {
            for (int k = 1; k <= ex.NbExtrema(); ++k) {
                if (ex.Distance(k) > 1e-7 * cx.scale) continue;
                gp_Pnt a, b;
                ex.Points(k, a, b);
                const gp_Pnt m((a.XYZ() + b.XYZ()) * 0.5);
                if (!best || m.Distance(q) < best->Distance(q)) best = m;
            }
        }
        if (!best || best->Distance(q) > 4.0 * cx.t) return std::nullopt;
        corners[i] = *best;
    }
    std::vector<TopoDS_Vertex> v(n);
    for (std::size_t i = 0; i < n; ++i) v[i] = BRepBuilderAPI_MakeVertex(corners[i]).Vertex();
    BRepBuilderAPI_MakeWire mw;
    for (std::size_t i = 0; i < n; ++i) {
        const auto u0 = param_on(curves[i], corners[i]);
        const auto u1 = param_on(curves[i], corners[(i + 1) % n]);
        if (!u0 || !u1) return std::nullopt;
        const auto range = loop_range(curves[i], *u0, *u1, n == 1);
        if (!range) return std::nullopt;
        BRepBuilderAPI_MakeEdge me(curves[i], v[i], v[(i + 1) % n], range->first, range->second);
        if (!me.IsDone()) return std::nullopt;
        mw.Add(me.Edge());

        // Cache the same curve per body edge of this side, for the band across the junction.
        const Side& s = loop[i];
        if (!s.pieces.front().body_edge) continue;
        for (std::size_t k = 0; k < s.pieces.size(); ++k) {
            const Piece& p = s.pieces[k];
            if (std::any_of(cx.edge_cache.begin(), cx.edge_cache.end(),
                            [&](const auto& kv) { return kv.first.IsSame(p.edge); }))
                continue;
            auto [bs, be] = piece_vertices(p);
            auto inner_of = [&](const TopoDS_Vertex& bv, bool at_start, bool at_end)
                -> std::optional<TopoDS_Vertex> {
                if (auto hit = cached_inner_vertex(cx, bv)) return hit;
                TopoDS_Vertex iv;
                if (at_start) iv = v[i];
                else if (at_end) iv = v[(i + 1) % n];
                else {
                    const auto u = param_on(curves[i], BRep_Tool::Pnt(bv));
                    if (!u) return std::nullopt;
                    iv = BRepBuilderAPI_MakeVertex(curves[i]->Value(*u)).Vertex();
                }
                cx.vertex_cache.emplace_back(bv, iv);
                return iv;
            };
            const auto ia = inner_of(bs, k == 0, false);
            const auto ib = inner_of(be, false, k + 1 == s.pieces.size());
            if (!ia || !ib) return std::nullopt;
            const auto pa = param_on(curves[i], BRep_Tool::Pnt(*ia));
            const auto pb = param_on(curves[i], BRep_Tool::Pnt(*ib));
            if (!pa || !pb) return std::nullopt;
            const auto pr = loop_range(curves[i], *pa, *pb, n == 1 && s.pieces.size() == 1);
            if (!pr) return std::nullopt;
            BRepBuilderAPI_MakeEdge pe(curves[i], *ia, *ib, pr->first, pr->second);
            if (!pe.IsDone()) return std::nullopt;
            // The cache holds edges in the body edge's parameter direction.
            cx.edge_cache.emplace_back(p.edge, p.reversed ? TopoDS::Edge(pe.Edge().Reversed()) : pe.Edge());
        }
    }
    if (!mw.IsDone()) return std::nullopt;
    return mw.Wire();
}

// The cavity section of one station loop. At a body-edge station (an end or a junction)
// every inner vertex and edge comes from the shared caches; at a section station the side
// curves are fitted through marched points and corner points directly.
std::optional<TopoDS_Wire> station_wire(Context& cx, const std::vector<Side>& loop) {
    const std::size_t n = loop.size();
    BRepBuilderAPI_MakeWire mw;
    if (loop.front().pieces.front().body_edge) {
        // Every consecutive piece pair around the loop meets at a body vertex.
        std::vector<const Piece*> seq;
        for (const Side& s : loop)
            for (const Piece& p : s.pieces) seq.push_back(&p);
        for (std::size_t i = 0; i < seq.size(); ++i)
            if (!inner_vertex(cx, *seq[(i + seq.size() - 1) % seq.size()], *seq[i])) {
                cx.why = "an inner corner of a section could not be placed";
                return std::nullopt;
            }
        for (std::size_t i = 0; i < n; ++i) {
            const Side& s = loop[i];
            std::vector<TopoDS_Edge> edges;
            for (const Piece& p : s.pieces) {
                const auto e = inner_edge(cx, p);
                if (!e) {
                    cx.why = "an inner edge of a section could not be fitted";
                    return std::nullopt;
                }
                edges.push_back(p.reversed ? TopoDS::Edge(e->Reversed()) : *e);
            }
            const auto v0 = inner_vertex(cx, loop[(i + n - 1) % n].pieces.back(), s.pieces.front());
            const auto v1 = inner_vertex(cx, s.pieces.back(), loop[(i + 1) % n].pieces.front());
            const auto joined = join_edges(edges, *v0, *v1);
            if (!joined) {
                cx.why = "a section side could not be joined into one curve";
                return std::nullopt;
            }
            mw.Add(*joined);
        }
    } else {
        std::vector<gp_Pnt> corners(n);
        for (std::size_t i = 0; i < n; ++i) {
            const Piece& pi = loop[(i + n - 1) % n].pieces.back();
            const Piece& po = loop[i].pieces.front();
            BRepAdaptor_Curve ci(pi.edge), co(po.edge);
            const double ui = pi.reversed ? ci.FirstParameter() : ci.LastParameter();
            const double uo = po.reversed ? co.LastParameter() : co.FirstParameter();
            const gp_Vec ma = cx.ax.inward(piece_tangent(pi, ui)), mb = cx.ax.inward(piece_tangent(po, uo));
            std::optional<gp_Pnt> c;
            if (n == 1) {
                c = march(cx, ci.Value(ui), ma, &cx.own[static_cast<std::size_t>(loop[i].face)]);
            } else {
                c = corner(cx, ci.Value(ui), ma, mb,
                           {cx.laterals[static_cast<std::size_t>(loop[(i + n - 1) % n].face)]},
                           {cx.laterals[static_cast<std::size_t>(loop[i].face)]});
            }
            if (!c) {
                cx.why = "an inner corner of a section could not be placed";
                return std::nullopt;
            }
            corners[i] = *c;
        }
        std::vector<TopoDS_Vertex> v(n);
        for (std::size_t i = 0; i < n; ++i) v[i] = BRepBuilderAPI_MakeVertex(corners[i]).Vertex();
        for (std::size_t i = 0; i < n; ++i) {
            const Side& s = loop[i];
            std::vector<gp_Pnt> pts{corners[i]};
            for (const Piece& p : s.pieces) {
                const auto mp = march_piece(cx, p, cx.own[static_cast<std::size_t>(s.face)]);
                pts.insert(pts.end(), mp.begin(), mp.end());
            }
            Handle(Geom_BSplineCurve) crv;
            if (n == 1) {
                crv = interpolate(pts, true, kRingPoints);
            } else {
                pts.push_back(corners[(i + 1) % n]);
                crv = interpolate(pts, false, kSidePoints);
            }
            BRepBuilderAPI_MakeEdge me;
            if (!crv.IsNull()) me.Init(crv, v[i], v[(i + 1) % n]);
            if (crv.IsNull() || !me.IsDone()) {
                cx.why = "a section side could not be fitted";
                return std::nullopt;
            }
            mw.Add(me.Edge());
        }
    }
    if (!mw.IsDone()) {
        cx.why = "a cavity section did not close into a wire";
        return std::nullopt;
    }
    return mw.Wire();
}

double volume_of(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}

bool watertight(const TopoDS_Shape& s) {
    TopTools_IndexedDataMapOfShapeListOfShape ef;
    TopExp::MapShapesAndAncestors(s, TopAbs_EDGE, TopAbs_FACE, ef);
    for (int i = 1; i <= ef.Extent(); ++i) {
        if (BRep_Tool::Degenerated(TopoDS::Edge(ef.FindKey(i)))) continue;
        if (ef(i).Extent() < 2) return false;
    }
    return true;
}

// Every sampled point of a result face that is off the original skin must lie t from the
// kept skin. Returns the worst deviation (negative when no inner point was sampled).
double wall_deviation(const TopoDS_Shape& result, const TopoDS_Shape& kept_skin, double t,
                      const TopoDS_Shape& removed, int* samples) {
    Dist dist(kept_skin);
    std::optional<Dist> to_removed;
    if (!removed.IsNull()) to_removed.emplace(removed);
    double worst = -1.0;
    int n = 0;
    for (TopExp_Explorer ex(result, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face f = TopoDS::Face(ex.Current());
        BRepAdaptor_Surface s(f);
        BRepTopAdaptor_FClass2d cls(f, 1e-7);
        constexpr int N = 9;
        for (int i = 0; i < N; ++i)
            for (int j = 0; j < N; ++j) {
                const double u = s.FirstUParameter() + (s.LastUParameter() - s.FirstUParameter()) * (i + 0.5) / N;
                const double v = s.FirstVParameter() + (s.LastVParameter() - s.FirstVParameter()) * (j + 0.5) / N;
                if (cls.Perform(gp_Pnt2d(u, v)) != TopAbs_IN) continue;
                const gp_Pnt p = s.Value(u, v);
                double d, dr;
                gp_Vec g;
                if (!dist.at(p, d, g)) return 1e300;
                if (d < 1e-4 * t) continue;  // an outer face
                // The opened end rims lie on the removed faces' planes, not t from the skin.
                if (to_removed && to_removed->at(p, dr, g) && dr < 1e-6) continue;
                worst = std::max(worst, std::abs(d - t));
                ++n;
            }
    }
    if (samples) *samples = n;
    return worst;
}

// Everything one hollowing attempt needs once the body has been classified.
struct Plan {
    const TopoDS_Shape& body;
    const std::vector<TopoDS_Face>& remove;
    double t;
    std::vector<Segment> segs;
    std::vector<std::pair<double, double>> ranges;  // per lateral: height range
    std::vector<TopoDS_Face> caps;                  // closed planar ends
    bool open_lo, open_hi, cap_lo, cap_hi;
};

// One hollowing attempt: loft the cavity band by band, sew it, cut it from the body and
// prove the wall. `exact_prisms` builds prismatic bands from analytic offset sections (inner
// planes and cylinders). Null with the reason in cx.why on any failure.
TopoDS_Shape hollow(Context& cx, const Plan& plan, bool exact_prisms) {
    cx.vertex_cache.clear();
    cx.edge_cache.clear();
    cx.why.clear();
    auto bail = [&](const std::string& reason) {
        cx.why = reason;
        return TopoDS_Shape();
    };
    const double t = plan.t;
    const std::vector<Segment>& segs = plan.segs;
    const auto& ranges = plan.ranges;
    // A prismatic band (planes and cylinders along the axis) gets its exact offset: two
    // analytic sections joined by ruled faces, i.e. inner planes and cylinders. Those bands
    // go first, so the sections they leave at a junction are the ones a freeform band starts
    // from. A freeform band lofts exact eroded sections; where it meets another band the
    // true inner wall kinks a little off the junction level (t·tan(θ/2) for a convex
    // dihedral θ), so it keeps a short ruled transition there and the junction section only
    // bends the cavity inside that transition.
    BRepBuilderAPI_Sewing sew(1e-4 * cx.scale / 100.0);
    std::optional<TopoDS_Wire> bottom_wire, top_wire;
    auto station = [&](const Segment& s, double z) -> std::optional<std::vector<Side>> {
        // Faces more than 3t off this station cannot be nearest to a point t from the skin;
        // leaving them out keeps every distance query small.
        std::vector<TopoDS_Face> nearby_faces;
        for (std::size_t i = 0; i < cx.laterals.size(); ++i)
            if (ranges[i].first < z + 3.0 * t && ranges[i].second > z - 3.0 * t)
                nearby_faces.push_back(cx.laterals[i]);
        cx.skin.emplace(compound_of(nearby_faces));
        return station_loop(cx, s, z, ranges);
    };
    auto band_range = [&](std::size_t k) {
        double lo = segs[k].lo, hi = segs[k].hi;
        if (k == 0 && plan.cap_lo) lo += t;
        if (k + 1 == segs.size() && plan.cap_hi) hi -= t;
        return std::make_pair(lo, hi);
    };
    auto add_faces = [&](const TopoDS_Shape& s) {
        for (TopExp_Explorer e(s, TopAbs_FACE); e.More(); e.Next()) sew.Add(e.Current());
    };
    // Exact build of a prismatic band; false (nothing added) when a side is not analytic.
    auto build_prism = [&](std::size_t k) -> bool {
        const auto [lo, hi] = band_range(k);
        std::vector<TopoDS_Wire> ends;
        for (const double z : {lo, hi}) {
            auto loop = station(segs[k], z);
            if (!loop) return false;
            auto w = prism_wire(cx, *loop, z);
            if (!w) return false;
            ends.push_back(*w);
        }
        BRepOffsetAPI_ThruSections ts(Standard_False, Standard_True, 1e-6);
        ts.CheckCompatibility(Standard_False);
        ts.AddWire(ends[0]);
        ts.AddWire(ends[1]);
        ts.Build();
        if (!ts.IsDone()) return false;
        add_faces(ts.Shape());
        if (k == 0) bottom_wire = ends[0];
        if (k + 1 == segs.size()) top_wire = ends[1];
        return true;
    };
    // Sampled build: exact eroded sections lofted, ruled transitions at junctions.
    auto build_sampled = [&](std::size_t k) -> std::optional<std::string> {
        const Segment& s = segs[k];
        const auto [lo, hi] = band_range(k);
        const double band = std::min(kTransitionFrac * t, 0.25 * (hi - lo));
        const double blo = k > 0 ? band : 0.0;
        const double bhi = k + 1 < segs.size() ? band : 0.0;
        // Runs of stations lofted together: {z..., ruled}.
        std::vector<std::pair<std::vector<double>, bool>> runs;
        const double ilo = lo + blo, ihi = hi - bhi;
        auto ramp = [](double a, double b) {
            std::vector<double> z;
            for (int j = 0; j <= kTransitionSteps; ++j) z.push_back(a + (b - a) * j / kTransitionSteps);
            return z;
        };
        if (blo > 0.0) runs.push_back({ramp(lo, ilo), true});
        std::vector<double> mid;
        const int intervals = s.prism ? 1 : kFreeformIntervals;
        for (int j = 0; j <= intervals; ++j) mid.push_back(ilo + (ihi - ilo) * j / intervals);
        runs.push_back({mid, s.prism});
        if (bhi > 0.0) runs.push_back({ramp(ihi, hi), true});
        std::optional<TopoDS_Wire> carried;  // last wire of the previous run
        std::size_t n_sides = 0;
        for (std::size_t r = 0; r < runs.size(); ++r) {
            const auto& [zs, ruled] = runs[r];
            BRepOffsetAPI_ThruSections ts(Standard_False, ruled ? Standard_True : Standard_False, 1e-6);
            ts.CheckCompatibility(Standard_False);
            for (std::size_t j = 0; j < zs.size(); ++j) {
                TopoDS_Wire w;
                if (j == 0 && carried) {
                    w = *carried;
                } else {
                    auto loop = station(s, zs[j]);
                    if (!loop) return cx.why;
                    if (n_sides == 0) n_sides = loop->size();
                    if (loop->size() != n_sides) return std::string("the side count changes inside a height band");
                    auto sw = station_wire(cx, *loop);
                    if (!sw) return cx.why.empty() ? std::string("a cavity section could not be fitted") : cx.why;
                    w = *sw;
                }
                ts.AddWire(w);
                if (k == 0 && r == 0 && j == 0) bottom_wire = w;
                if (k + 1 == segs.size() && r + 1 == runs.size() && j + 1 == zs.size()) top_wire = w;
                if (j + 1 == zs.size()) carried = w;
            }
            ts.Build();
            if (!ts.IsDone()) return std::string("lofting the cavity through its sections failed");
            add_faces(ts.Shape());
        }
        return std::nullopt;
    };
    std::vector<bool> built(segs.size(), false);
    for (std::size_t k = 0; k < segs.size(); ++k) {
        const auto [lo, hi] = band_range(k);
        if (!(hi - lo > 1e-3 * t)) return bail("the wall leaves no room for a cavity at a closed end");
        if (exact_prisms && segs[k].prism) built[k] = build_prism(k);
    }
    for (std::size_t k = 0; k < segs.size(); ++k)
        if (!built[k])
            if (auto err = build_sampled(k)) return bail(*err);

    // --- ends: run the cavity out through each opened end; close it at a capped one ----
    auto end_faces = [&](const TopoDS_Wire& w, bool open, double dir) -> bool {
        if (open) {
            const double ext = std::max(t, 1.0);
            BRepPrimAPI_MakePrism pr(w, gp_Vec(cx.ax.d) * (dir * ext));
            if (!pr.IsDone()) return false;
            add_faces(pr.Shape());
            BRepBuilderAPI_MakeFace cap(TopoDS::Wire(pr.LastShape()), Standard_True);
            if (!cap.IsDone()) return false;
            sew.Add(cap.Face());
        } else {
            BRepBuilderAPI_MakeFace cap(w, Standard_True);
            if (!cap.IsDone()) return false;
            sew.Add(cap.Face());
        }
        return true;
    };
    if (!end_faces(*bottom_wire, plan.open_lo, -1.0) || !end_faces(*top_wire, plan.open_hi, 1.0))
        return bail("the cavity's end faces could not be built");
    sew.Perform();
    const TopoDS_Shape sewed = sew.SewedShape();
    TopoDS_Shell shell;
    int n_shells = 0;
    for (TopExp_Explorer e(sewed, TopAbs_SHELL); e.More(); e.Next()) {
        shell = TopoDS::Shell(e.Current());
        ++n_shells;
    }
    if (n_shells != 1 || sew.NbFreeEdges() != 0) return bail("the cavity did not sew into one closed shell");
    BRepBuilderAPI_MakeSolid ms(shell);
    if (!ms.IsDone()) return bail("the cavity shell did not make a solid");
    ShapeFix_Solid fix(ms.Solid());
    fix.Perform();
    TopoDS_Shape cavity = fix.Solid();
    if (volume_of(cavity) < 0.0) cavity.Reverse();
    if (!(volume_of(cavity) > 0.0)) return bail("the cavity has no volume");

    // --- cut and prove the wall -----------------------------------------------------------
    TopoDS_Shape out = cut_unify(plan.body, cavity);
    if (out.IsNull()) return bail("cutting the cavity from the body failed");
    int n_solids = 0;
    for (TopExp_Explorer e(out, TopAbs_SOLID); e.More(); e.Next()) ++n_solids;
    if (n_solids != 1) return bail("the hollowed body is not one solid");
    if (!BRepCheck_Analyzer(out).IsValid()) return bail("the hollowed body failed BRepCheck validation");
    if (!watertight(out)) return bail("the hollowed body is not watertight");
    std::vector<TopoDS_Face> kept = cx.laterals;
    kept.insert(kept.end(), plan.caps.begin(), plan.caps.end());
    int samples = 0;
    const double dev = wall_deviation(out, compound_of(kept), t, compound_of(plan.remove), &samples);
    if (samples == 0) return bail("no inner wall to verify");
    if (dev > kWallTolFrac * t) {
        char buf[160];
        std::snprintf(buf, sizeof(buf), "the sectional wall strays %.4g mm from %.4g mm", dev, t);
        return bail(buf);
    }
    return out;
}

}  // namespace

TopoDS_Shape shell_by_sections(const TopoDS_Shape& body, double t,
                               const std::vector<TopoDS_Face>& remove, std::string* why) {
    auto fail = [&](const std::string& reason) {
        if (why) *why = reason;
        return TopoDS_Shape();
    };
    if (body.IsNull() || !(t > 0.0) || remove.empty()) return fail("nothing to open");
    try {
        // --- axis: the opened faces are parallel planes ---------------------------------
        BRepAdaptor_Surface r0(remove.front(), Standard_False);
        if (r0.GetType() != GeomAbs_Plane) return fail("an opened face is not planar");
        Axis ax;
        ax.d = r0.Plane().Axis().Direction();
        ax.o = r0.Plane().Location();
        ax.x = r0.Plane().XAxis().Direction();
        ax.y = gp_Dir(gp_Vec(ax.d).Crossed(gp_Vec(ax.x)));
        for (const TopoDS_Face& f : remove) {
            BRepAdaptor_Surface s(f, Standard_False);
            if (s.GetType() != GeomAbs_Plane ||
                !s.Plane().Axis().Direction().IsParallel(ax.d, 1e-9))
                return fail("the opened faces are not parallel planes");
        }
        Context cx;
        cx.t = t;
        cx.ax = ax;
        TopExp::MapShapesAndAncestors(body, TopAbs_EDGE, TopAbs_FACE, cx.edge_faces);

        // --- body height range and vertex levels ---------------------------------------
        std::vector<double> levels;
        double zmin = 1e300, zmax = -1e300;
        {
            TopTools_IndexedMapOfShape vm;
            TopExp::MapShapes(body, TopAbs_VERTEX, vm);
            for (int i = 1; i <= vm.Extent(); ++i)
                levels.push_back(ax.h(BRep_Tool::Pnt(TopoDS::Vertex(vm(i)))));
            TopTools_IndexedMapOfShape em;
            TopExp::MapShapes(body, TopAbs_EDGE, em);
            for (int i = 1; i <= em.Extent(); ++i)
                if (!BRep_Tool::Degenerated(TopoDS::Edge(em(i))))
                    edge_height_range(TopoDS::Edge(em(i)), ax, zmin, zmax);
        }
        cx.scale = std::max(1.0, zmax - zmin);
        std::sort(levels.begin(), levels.end());
        std::vector<double> uniq;
        for (double z : levels)
            if (uniq.empty() || z - uniq.back() > kLevelTol) uniq.push_back(z);
        levels = uniq;
        if (levels.size() < 2 || std::abs(levels.front() - zmin) > kLevelTol ||
            std::abs(levels.back() - zmax) > kLevelTol)
            return fail("the body's extreme heights are not at vertex levels");

        // --- classify faces --------------------------------------------------------------
        bool open_lo = false, open_hi = false, cap_lo = false, cap_hi = false;
        for (const TopoDS_Face& f : remove) {
            const double z = ax.h(BRepAdaptor_Surface(f, Standard_False).Plane().Location());
            if (std::abs(z - zmin) < kLevelTol) open_lo = true;
            else if (std::abs(z - zmax) < kLevelTol) open_hi = true;
            else return fail("an opened face is not an end of the body along its normal");
        }
        std::vector<TopoDS_Face> caps;
        std::vector<std::pair<double, double>> ranges;
        TopTools_IndexedMapOfShape fm;
        TopExp::MapShapes(body, TopAbs_FACE, fm);
        for (int i = 1; i <= fm.Extent(); ++i) {
            const TopoDS_Face& f = TopoDS::Face(fm(i));
            if (std::any_of(remove.begin(), remove.end(), [&](const TopoDS_Face& r) { return r.IsSame(f); }))
                continue;
            BRepAdaptor_Surface s(f, Standard_False);
            if (s.GetType() == GeomAbs_Plane && s.Plane().Axis().Direction().IsParallel(ax.d, 1e-9)) {
                const double z = ax.h(s.Plane().Location());
                if (std::abs(z - zmin) < kLevelTol && !open_lo) { cap_lo = true; caps.push_back(f); continue; }
                if (std::abs(z - zmax) < kLevelTol && !open_hi) { cap_hi = true; caps.push_back(f); continue; }
                return fail("the body has a step or ledge face across the axis");
            }
            double lo, hi;
            face_height_range(f, ax, lo, hi);
            cx.laterals.push_back(f);
            ranges.emplace_back(lo, hi);
        }
        if (cx.laterals.empty()) return fail("no side wall to hollow");
        if (!(open_lo || open_hi)) return fail("nothing opened");
        if ((cap_lo && open_lo) || (cap_hi && open_hi)) return fail("an end is both open and capped");

        // --- segments: every lateral face spans exactly one band between vertex levels ----
        std::vector<Segment> segs;
        for (std::size_t k = 0; k + 1 < levels.size(); ++k) segs.push_back({levels[k], levels[k + 1], {}, true});
        for (std::size_t i = 0; i < cx.laterals.size(); ++i) {
            bool placed = false;
            for (Segment& s : segs) {
                if (std::abs(ranges[i].first - s.lo) < kLevelTol && std::abs(ranges[i].second - s.hi) < kLevelTol) {
                    s.faces.push_back(static_cast<int>(i));
                    s.prism = s.prism && prismatic(cx.laterals[i], ax);
                    placed = true;
                    break;
                }
            }
            if (!placed) return fail("a side face does not span one height band of the body");
        }
        for (const Segment& s : segs)
            if (s.faces.empty()) return fail("a height band of the body has no side wall");

        // --- ownership: a face owns its non-seam edges and the faces across them -----------
        cx.own.resize(cx.laterals.size());
        for (std::size_t i = 0; i < cx.laterals.size(); ++i) {
            TopTools_MapOfShape& own = cx.own[i];
            own.Add(cx.laterals[i]);
            for (TopExp_Explorer e(cx.laterals[i], TopAbs_EDGE); e.More(); e.Next()) {
                const TopoDS_Edge& ed = TopoDS::Edge(e.Current());
                if (BRep_Tool::IsClosed(ed, cx.laterals[i])) { own.Add(ed); continue; }
                const int idx = cx.edge_faces.FindIndex(ed);
                if (idx == 0) continue;
                bool seam = false;
                for (const TopoDS_Shape& g : cx.edge_faces(idx)) {
                    const int gi = lateral_index(cx, g);
                    if (gi < 0 || static_cast<std::size_t>(gi) == i) continue;
                    double glo = ranges[static_cast<std::size_t>(gi)].first, ghi = ranges[static_cast<std::size_t>(gi)].second;
                    if (std::abs(glo - ranges[i].first) < kLevelTol && std::abs(ghi - ranges[i].second) < kLevelTol)
                        seam = true;  // a loop neighbour in the same band
                }
                if (seam) continue;
                own.Add(ed);
                for (const TopoDS_Shape& g : cx.edge_faces(idx)) own.Add(g);
            }
        }

        Plan plan{body, remove, t, std::move(segs), std::move(ranges), std::move(caps),
                  open_lo, open_hi, cap_lo, cap_hi};
        // Exact (analytic) prismatic bands first; when the wall they leave at a junction
        // strays past the tolerance (a steep junction), every band falls back to sampled
        // sections, whose junction error stays inside the ruled transitions. Only the
        // accepted attempt's cut is recorded in the caller's op history.
        const bool any_prism = std::any_of(plan.segs.begin(), plan.segs.end(),
                                           [](const Segment& s) { return s.prism; });
        const bool recording = op_history_recording();
        std::string first_why;
        for (const bool exact : {true, false}) {
            if (exact && !any_prism) continue;
            std::vector<Handle(BRepTools_History)> hist;
            TopoDS_Shape out;
            {
                OpHistoryScope local(recording);
                out = hollow(cx, plan, exact);
                if (!out.IsNull()) hist = local.histories();
            }
            if (!out.IsNull()) {
                for (const auto& h : hist) op_history_record(h);
                if (why) why->clear();
                return out;
            }
            if (first_why.empty()) first_why = cx.why;
        }
        return fail(first_why == cx.why ? cx.why : first_why + "; with sampled junctions: " + cx.why);
    } catch (const Standard_Failure& f) {
        return fail(std::string("OCCT threw ") + f.GetMessageString());
    }
}

}  // namespace geom
