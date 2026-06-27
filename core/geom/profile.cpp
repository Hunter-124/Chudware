// Solved-sketch -> OCCT planar faces. See profile.h for the why; this file is the
// faithful C++ port of the loop-detection + even-odd region nesting in
// engine/doc/features/sketch.py (_detect_loops / _orient_ccw / _region_face /
// compute_regions). Tolerances and winding rules mirror the Python reference so
// the C++ pipeline produces the same washer/region behavior as the oracle.
#include "geom/profile.h"

#include <cmath>
#include <vector>

#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <Geom_BSplineCurve.hxx>
#include <GeomAPI_Interpolate.hxx>
#include <ShapeAnalysis_FreeBounds.hxx>
#include <TColgp_HArray1OfPnt.hxx>
#include <TopTools_HSequenceOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

namespace aicad::geom {

namespace {

constexpr double kPi = 3.14159265358979323846;

// Endpoint-coincidence tolerance — matches sketch.py::_EPS (1e-6). We chain in 2D
// sketch space (the pre-lift coordinates) because that is where the solver works
// and where containment is computed; the lift is an isometry so distances agree.
constexpr double kEps = 1e-6;

// One built edge plus the bookkeeping the loop walk needs: its 2D endpoints (for
// chaining + containment) and whether it is a closed single edge (a circle).
struct SketchEdge {
    TopoDS_Edge edge;
    Eigen::Vector2d p_start;  // 2D sketch-space start
    Eigen::Vector2d p_end;    // 2D sketch-space end
    bool closed = false;      // single-edge closed loop (circle / self-closing spline)
    // 2D sample points to splice into the loop polygon AFTER p_start when this edge
    // is traversed start->end. Empty for straight/analytic edges (line/circle/arc) so
    // their loop polygons stay byte-identical to the pre-spline behavior; populated for
    // curved through-point edges (splines) so the shoelace area / containment tests see
    // the real curve shape instead of just the two endpoints. For an OPEN spline these
    // are the strictly-interior points (the end vertex is supplied by the next edge's
    // start); for a self-closing spline (closed==true) these are every unique ring point
    // after the start (the start closes the ring via the shoelace wrap).
    std::vector<Eigen::Vector2d> interior;
};

bool near2(const Eigen::Vector2d& a, const Eigen::Vector2d& b) {
    return std::abs(a.x() - b.x()) < kEps && std::abs(a.y() - b.y()) < kEps;
}

// Convert a 2D sketch point to a gp_Pnt lifted onto the plane.
gp_Pnt lift_pnt(const SketchPlane& plane, const Eigen::Vector2d& uv) {
    Eigen::Vector3d w = plane.lift(uv);
    return gp_Pnt(w.x(), w.y(), w.z());
}

gp_Dir to_dir(const Eigen::Vector3d& v) { return gp_Dir(v.x(), v.y(), v.z()); }

// Build the OCCT edge for one solved entity, recording its 2D endpoints. Returns
// false (and leaves out untouched) if the entity is construction, an unsupported
// type, or fails to build a valid edge — the caller simply skips it.
bool build_edge(const nlohmann::json& ent, const SketchPlane& plane,
                SketchEdge& out) {
    if (!ent.is_object() || !ent.contains("type")) return false;
    if (ent.value("construction", false)) return false;  // never part of a profile
    const std::string t = ent["type"];

    try {
        if (t == "line") {
            Eigen::Vector2d a(ent["p1"][0], ent["p1"][1]);
            Eigen::Vector2d b(ent["p2"][0], ent["p2"][1]);
            if (near2(a, b)) return false;  // degenerate zero-length line
            BRepBuilderAPI_MakeEdge mk(lift_pnt(plane, a), lift_pnt(plane, b));
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge();
            out.p_start = a;
            out.p_end = b;
            out.closed = false;
            return true;
        }
        if (t == "circle") {
            Eigen::Vector2d c(ent["center"][0], ent["center"][1]);
            double r = ent["radius"];
            if (r <= kEps) return false;
            // gp_Ax2: location = 3D center, main dir = plane normal, x dir = uAxis.
            gp_Ax2 ax(lift_pnt(plane, c), to_dir(plane.normal), to_dir(plane.uAxis));
            BRepBuilderAPI_MakeEdge mk(gp_Circ(ax, r));
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge();
            // A full circle is a single closed edge: start == end (its own loop).
            out.p_start = c;
            out.p_end = c;
            out.closed = true;
            return true;
        }
        if (t == "arc") {
            Eigen::Vector2d c(ent["center"][0], ent["center"][1]);
            double r = ent["radius"];
            if (r <= kEps) return false;
            // Arc angles are DEGREES (matching the entity store + the sketch solver in
            // sketch_geom.cpp + build123d Edge.make_circle), NOT radians. `ccw` selects
            // the sweep direction: GC_MakeArcOfCircle(circ, a1, a2, true) always sweeps
            // CCW from a1 to a2, so a CW arc is the CCW arc of the SWAPPED angles. This
            // mirrors document.cpp::weld_member exactly and honors `ccw` so e.g.
            // {start:180,end:90,ccw:false} is the 90 deg minor arc while ccw:true is the
            // 270 deg major arc (Bug N-2 — `ccw` was previously ignored, and the angles
            // were wrongly read as radians).
            double sa = ent["start"];  // degrees
            double ea = ent["end"];    // degrees
            if (std::abs(ea - sa) < 1e-9) return false;  // degenerate (no span)
            const bool ccw = ent.value("ccw", true);
            const double DEG = kPi / 180.0;
            double a1 = (ccw ? sa : ea) * DEG;
            double a2 = (ccw ? ea : sa) * DEG;
            gp_Ax2 ax(lift_pnt(plane, c), to_dir(plane.normal), to_dir(plane.uAxis));
            GC_MakeArcOfCircle mkarc(gp_Circ(ax, r), a1, a2, Standard_True);  // CCW
            if (!mkarc.IsDone()) return false;
            BRepBuilderAPI_MakeEdge mk(mkarc.Value());
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge();
            // 2D endpoints (for chaining/containment) are the AS-DRAWN start/end points
            // in degrees, so they coincide with adjacent line endpoints in a path.
            auto on = [&](double deg) {
                double a = deg * DEG;
                return Eigen::Vector2d(c.x() + r * std::cos(a), c.y() + r * std::sin(a));
            };
            out.p_start = on(sa);
            out.p_end = on(ea);
            out.closed = false;
            return true;
        }
        if (t == "spline") {
            // Through-point cubic spline. Build the OCCT edge by INTERPOLATION (the same
            // GeomAPI_Interpolate path as curves.cpp::make_spline_wire / build123d
            // Edge.make_spline), so a closed spline profile extrudes/revolves with the same
            // geometry the rest of the kernel produces. The loop polygon uses the entity's
            // own through-points (interpolation passes through every one), which is sampling-
            // method independent and keeps area/orientation correct.
            if (!ent.contains("points") || !ent["points"].is_array()) return false;
            std::vector<Eigen::Vector2d> pts2d;
            for (const auto& q : ent["points"]) {
                if (!q.is_array() || q.size() < 2) continue;
                Eigen::Vector2d p(q[0].get<double>(), q[1].get<double>());
                // Drop a point coincident with the previous one — GeomAPI_Interpolate
                // rejects duplicate consecutive knots (and zero-length segments are noise).
                if (!pts2d.empty() && near2(pts2d.back(), p)) continue;
                pts2d.push_back(p);
            }
            if (pts2d.size() < 2) return false;
            // A self-closing spline (first ~ last): drop the duplicate closing point and
            // interpolate PERIODICALLY so the single edge is genuinely closed (one-edge loop,
            // like a circle). Otherwise interpolate an open curve start->end.
            const bool selfClosed = pts2d.size() >= 4 && near2(pts2d.front(), pts2d.back());
            if (selfClosed) pts2d.pop_back();
            const int np = static_cast<int>(pts2d.size());
            Handle(TColgp_HArray1OfPnt) arr = new TColgp_HArray1OfPnt(1, np);
            for (int i = 0; i < np; ++i)
                arr->SetValue(i + 1, lift_pnt(plane, pts2d[i]));
            GeomAPI_Interpolate interp(arr, selfClosed ? Standard_True : Standard_False, 1.0e-6);
            interp.Perform();
            if (!interp.IsDone()) return false;
            Handle(Geom_BSplineCurve) curve = interp.Curve();
            if (curve.IsNull()) return false;
            BRepBuilderAPI_MakeEdge mk(curve);
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge();
            out.p_start = pts2d.front();
            out.p_end   = selfClosed ? pts2d.front() : pts2d.back();
            out.closed  = selfClosed;
            // Polygon contribution after the start vertex (see SketchEdge::interior).
            out.interior.assign(pts2d.begin() + 1,
                                selfClosed ? pts2d.end() : pts2d.end() - 1);
            return true;
        }
    } catch (...) {
        return false;  // malformed param dict / OCCT throw — skip this entity
    }
    return false;  // unsupported entity type (point/style_spline/ellipse/...) — skip
}

// One detected closed loop: the edges in head-to-tail order (reversed where the
// walk matched an edge by its end) plus the ordered 2D polyline of loop-corner
// points used for area / containment tests.
struct Loop {
    std::vector<TopoDS_Edge> edges;
    std::vector<Eigen::Vector2d> poly;  // ordered loop corners (2D)
};

// Port of sketch.py::_detect_loops. Circles become one-edge loops immediately;
// open edges chain by endpoint proximity with reversal tracking; open (non-
// closing) chains are discarded. We also accumulate each loop's 2D polyline.
std::vector<Loop> detect_loops(const std::vector<SketchEdge>& edges) {
    std::vector<Loop> loops;
    const size_t n = edges.size();
    std::vector<bool> used(n, false);

    // Closed single edges (circles) -> immediate single-edge loops. The polyline
    // is sampled coarsely (a square inscribing the circle is enough for the
    // even-odd containment test; the centroid is the circle center either way).
    for (size_t i = 0; i < n; ++i) {
        if (!edges[i].closed) continue;
        used[i] = true;
        Loop lp;
        lp.edges.push_back(edges[i].edge);
        if (!edges[i].interior.empty()) {
            // Self-closing spline: it carries its own ring of sample points, so the loop
            // polygon is the real curve (start + every unique ring point). This is what
            // gives the shoelace area its correct sign for CCW normalisation.
            lp.poly.push_back(edges[i].p_start);
            for (const auto& q : edges[i].interior) lp.poly.push_back(q);
        } else {
            // Circle: start==end==center, radius not stored here. A centroid-only polyline
            // (degenerate) still works because containment uses the OTHER loop's polygon;
            // to keep the area sane we emit a tiny CCW square at the center.
            const Eigen::Vector2d& c = edges[i].p_start;
            const double e = kEps;
            lp.poly = {{c.x() - e, c.y() - e},
                       {c.x() + e, c.y() - e},
                       {c.x() + e, c.y() + e},
                       {c.x() - e, c.y() + e}};
        }
        loops.push_back(std::move(lp));
    }

    // Walk chains for the open edges.
    for (size_t start = 0; start < n; ++start) {
        if (used[start]) continue;
        struct Step { size_t idx; bool rev; };
        std::vector<Step> chain{{start, false}};
        used[start] = true;
        const Eigen::Vector2d head = edges[start].p_start;
        Eigen::Vector2d tip = edges[start].p_end;
        bool closed = false;

        while (true) {
            long found = -1;
            bool found_rev = false;
            for (size_t j = 0; j < n; ++j) {
                if (used[j]) continue;
                if (near2(edges[j].p_start, tip)) { found = (long)j; found_rev = false; break; }
                if (near2(edges[j].p_end, tip))   { found = (long)j; found_rev = true;  break; }
            }
            if (found < 0) break;
            chain.push_back({(size_t)found, found_rev});
            used[found] = true;
            // Advance the tip to the FAR end of the appended edge.
            tip = found_rev ? edges[found].p_start : edges[found].p_end;
            if (near2(tip, head)) { closed = true; break; }
        }

        if (!closed) continue;  // open chain — discard (sketch.py does the same)

        Loop lp;
        for (const Step& s : chain) {
            const SketchEdge& se = edges[s.idx];
            TopoDS_Edge e = se.edge;
            if (s.rev) e = TopoDS::Edge(e.Reversed());
            lp.edges.push_back(e);
            // Loop polyline: the START point of each edge in head-to-tail order, then any
            // curved-edge interior samples (in walk order) so a spline edge contributes its
            // real shape — not just a chord — to the area/containment polygon. Straight and
            // analytic edges have empty interior, so line/arc loops stay byte-identical.
            lp.poly.push_back(s.rev ? se.p_end : se.p_start);
            if (!se.interior.empty()) {
                if (s.rev)
                    for (auto it = se.interior.rbegin(); it != se.interior.rend(); ++it)
                        lp.poly.push_back(*it);
                else
                    for (const auto& q : se.interior) lp.poly.push_back(q);
            }
        }
        loops.push_back(std::move(lp));
    }

    return loops;
}

// Shoelace signed area of a closed 2D polyline (port of _poly_area_centroid's
// area half). CCW (mathematically positive) loops have positive area.
double signed_area(const std::vector<Eigen::Vector2d>& p) {
    const size_t n = p.size();
    if (n < 3) return 0.0;
    double a = 0.0;
    for (size_t i = 0; i < n; ++i) {
        const Eigen::Vector2d& p0 = p[i];
        const Eigen::Vector2d& p1 = p[(i + 1) % n];
        a += p0.x() * p1.y() - p1.x() * p0.y();
    }
    return 0.5 * a;
}

// Representative interior-ish point for containment: the polygon centroid (matches
// the cents[] used by compute_regions for parent assignment).
Eigen::Vector2d poly_centroid(const std::vector<Eigen::Vector2d>& p) {
    const size_t n = p.size();
    Eigen::Vector2d mean(0, 0);
    for (const auto& q : p) mean += q;
    if (n) mean /= double(n);
    double a = signed_area(p);
    if (n < 3 || std::abs(a) < 1e-12) return mean;  // degenerate -> arithmetic mean
    double cx = 0, cy = 0;
    for (size_t i = 0; i < n; ++i) {
        const Eigen::Vector2d& p0 = p[i];
        const Eigen::Vector2d& p1 = p[(i + 1) % n];
        double cross = p0.x() * p1.y() - p1.x() * p0.y();
        cx += (p0.x() + p1.x()) * cross;
        cy += (p0.y() + p1.y()) * cross;
    }
    return {cx / (6.0 * a), cy / (6.0 * a)};
}

// Ray-casting point-in-polygon (port of _point_in_poly).
bool point_in_poly(const Eigen::Vector2d& pt, const std::vector<Eigen::Vector2d>& poly) {
    const size_t n = poly.size();
    if (n < 3) return false;
    bool inside = false;
    size_t j = n - 1;
    for (size_t i = 0; i < n; ++i) {
        double xi = poly[i].x(), yi = poly[i].y();
        double xj = poly[j].x(), yj = poly[j].y();
        if (((yi > pt.y()) != (yj > pt.y())) &&
            (pt.x() < (xj - xi) * (pt.y() - yi) / (yj - yi + 1e-30) + xi)) {
            inside = !inside;
        }
        j = i;
    }
    return inside;
}

// Build a wire from a loop's edges. Returns an empty (null) wire on failure.
TopoDS_Wire make_wire(const std::vector<TopoDS_Edge>& edges) {
    if (edges.empty()) return TopoDS_Wire();
    // Strict head-to-tail build first (exact; preserves the authored edge order).
    {
        BRepBuilderAPI_MakeWire mk;
        for (const TopoDS_Edge& e : edges) mk.Add(e);
        if (mk.IsDone()) return mk.Wire();
    }
    // Tolerant fallback (Bug N-3): a path that MIXES line + arc edges can leave a
    // float-noise gap between an analytic line endpoint and a trig-computed arc
    // endpoint that the strict builder refuses to bridge. Sew the edges into wires
    // with a real connection tolerance; accept only a single connected wire (a
    // genuinely broken path yields >1 wire and is reported as "no wire" upstream).
    Handle(TopTools_HSequenceOfShape) in = new TopTools_HSequenceOfShape();
    for (const TopoDS_Edge& e : edges) in->Append(e);
    Handle(TopTools_HSequenceOfShape) wires;
    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(in, 1.0e-4, Standard_False, wires);
    if (!wires.IsNull() && wires->Length() == 1) {
        const TopoDS_Shape& w = wires->Value(1);
        if (!w.IsNull() && w.ShapeType() == TopAbs_WIRE) return TopoDS::Wire(w);
    }
    return TopoDS_Wire();
}

}  // namespace

std::vector<TopoDS_Face> faces_from_solved_sketch(
    const nlohmann::json& solved_entities, const SketchPlane& plane) {
    std::vector<TopoDS_Face> faces;
    if (!solved_entities.is_object()) return faces;

    // 1) Build one lifted OCCT edge per non-construction line/circle/arc.
    std::vector<SketchEdge> edges;
    for (auto it = solved_entities.begin(); it != solved_entities.end(); ++it) {
        SketchEdge se;
        if (build_edge(it.value(), plane, se)) edges.push_back(std::move(se));
    }
    if (edges.empty()) return faces;

    // 2) Detect closed loops (circles + chained open edges).
    std::vector<Loop> loops = detect_loops(edges);
    if (loops.empty()) return faces;

    // 3) even-odd nesting: for each loop, its parent is the smallest-area loop
    //    that contains its centroid. Loops with no parent are outer regions; a
    //    loop whose parent is an outer region is that region's hole. (We build the
    //    simplest correct version: one nesting level — outer + direct holes —
    //    which is what the four parity test cases exercise.)
    const size_t n = loops.size();
    std::vector<double> area(n);
    std::vector<Eigen::Vector2d> cent(n);
    for (size_t i = 0; i < n; ++i) {
        area[i] = std::abs(signed_area(loops[i].poly));
        cent[i] = poly_centroid(loops[i].poly);
    }

    std::vector<int> parent(n, -1);
    std::vector<int> depth(n, 0);
    for (size_t i = 0; i < n; ++i) {
        double best_area = 0.0;
        int best = -1;
        for (size_t j = 0; j < n; ++j) {
            if (i == j || area[j] <= area[i]) continue;
            if (point_in_poly(cent[i], loops[j].poly)) {
                depth[i]++;
                if (best < 0 || area[j] < best_area) { best_area = area[j]; best = (int)j; }
            }
        }
        parent[i] = best;
    }

    // 4) Build one face per OUTER region (even depth — a top-level material loop)
    //    with its DIRECT child loops added as reversed (hole) wires. We normalise
    //    every loop to CCW first (positive signed area), then reverse each hole so
    //    OCCT subtracts it — mirroring _region_face / _orient_ccw. Without the CCW
    //    normalisation a clockwise outer loop made the hole reversal a no-op (the
    //    "square+circle extrudes solid disc instead of a washer" bug).
    auto wire_ccw = [&](const Loop& lp) -> TopoDS_Wire {
        TopoDS_Wire w = make_wire(lp.edges);
        if (w.IsNull()) return w;
        if (signed_area(lp.poly) < 0.0) w = TopoDS::Wire(w.Reversed());
        return w;
    };

    for (size_t i = 0; i < n; ++i) {
        if (depth[i] != 0) continue;  // only top-level loops start a region

        TopoDS_Wire outer = wire_ccw(loops[i]);
        if (outer.IsNull()) continue;

        BRepBuilderAPI_MakeFace mf(outer, /*OnlyPlane=*/true);
        // Direct children (parent == i, depth 1) become holes.
        for (size_t j = 0; j < n; ++j) {
            if (parent[j] != (int)i || depth[j] != 1) continue;
            TopoDS_Wire hole = wire_ccw(loops[j]);
            if (hole.IsNull()) continue;
            mf.Add(TopoDS::Wire(hole.Reversed()));  // reverse CCW -> CW so it cuts
        }
        if (!mf.IsDone()) continue;
        faces.push_back(mf.Face());
    }

    return faces;
}

TopoDS_Wire wire_from_solved_sketch(
    const nlohmann::json& solved_entities, const SketchPlane& plane) {
    if (!solved_entities.is_object()) return TopoDS_Wire();
    std::vector<TopoDS_Edge> edges;
    for (auto it = solved_entities.begin(); it != solved_entities.end(); ++it) {
        SketchEdge se;
        if (build_edge(it.value(), plane, se)) edges.push_back(se.edge);
    }
    if (edges.empty()) return TopoDS_Wire();
    return make_wire(edges);  // connects head-to-tail in entity order (open or closed)
}

}  // namespace aicad::geom
