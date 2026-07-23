// Implementation of the WS-C reverse-engineering kernel primitives — see recon.h.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include "doc/recon.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <iomanip>
#include <sstream>
#include <vector>

#include "doc/session.h"  // OpError
#include "geom/plane.h"   // chudware::geom::SketchPlane — face-plane frame convention

#include <Bnd_Box.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepGProp.hxx>
#include <BRepTools.hxx>
#include <BRepTools_WireExplorer.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Geom_Curve.hxx>
#include <Geom_Plane.hxx>
#include <Geom_TrimmedCurve.hxx>
#include <GeomProjLib.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax3.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Elips.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>

using nlohmann::json;

namespace chudware::doc {

namespace {

// ------------------------------------------------------------- C1 helpers

double volume_or_zero(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0.0;
    try {
        GProp_GProps gp;
        BRepGProp::VolumeProperties(shape, gp);
        return std::max(0.0, gp.Mass());
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// Precise bbox (BRepTools::Clean + AddOptimal, mirrors interference_report's per-part bbox).
bool bbox_of(const TopoDS_Shape& shape, std::array<double, 3>& mn, std::array<double, 3>& mx) {
    if (shape.IsNull()) return false;
    try {
        TopoDS_Shape clean = shape;
        BRepTools::Clean(clean);
        Bnd_Box box;
        BRepBndLib::AddOptimal(clean, box);
        if (box.IsVoid()) return false;
        box.Get(mn[0], mn[1], mn[2], mx[0], mx[1], mx[2]);
        return true;
    } catch (const Standard_Failure&) {
        return false;
    }
}

// Explode `diff_shape` (an A-B or B-A cut result) into its TopAbs_SOLID children, appending
// one residual-region json per solid tagged `side` ("missing"|"extra"). Slivers < 1e-3 mm^3
// (tessellation/boolean noise) are dropped.
void collect_residual_regions(const TopoDS_Shape& diff_shape, const char* side,
                              std::vector<json>& out) {
    if (diff_shape.IsNull()) return;
    for (TopExp_Explorer ex(diff_shape, TopAbs_SOLID); ex.More(); ex.Next()) {
        const TopoDS_Shape solid = ex.Current();
        const double vol = volume_or_zero(solid);
        if (vol < 1e-3) continue;
        std::array<double, 3> mn{0, 0, 0}, mx{0, 0, 0};
        std::array<double, 3> center{0, 0, 0}, size{0, 0, 0};
        if (bbox_of(solid, mn, mx)) {
            for (int i = 0; i < 3; ++i) {
                center[i] = 0.5 * (mn[i] + mx[i]);
                size[i] = mx[i] - mn[i];
            }
        }
        out.push_back(json{
            {"side", side},
            {"volume_mm3", vol},
            {"center", {center[0], center[1], center[2]}},
            {"size", {size[0], size[1], size[2]}},
        });
    }
}

json bbox_json(const std::array<double, 3>& mn, const std::array<double, 3>& mx) {
    return json{{"min", {mn[0], mn[1], mn[2]}}, {"max", {mx[0], mx[1], mx[2]}}};
}

}  // namespace

json compare_shapes(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    const double vol_a = volume_or_zero(a);
    const double vol_b = volume_or_zero(b);

    std::array<double, 3> mn_a{0, 0, 0}, mx_a{0, 0, 0}, mn_b{0, 0, 0}, mx_b{0, 0, 0};
    const bool has_bbox_a = bbox_of(a, mn_a, mx_a);
    const bool has_bbox_b = bbox_of(b, mn_b, mx_b);

    auto degrade = [&](const std::string& why) -> json {
        double centroid_dist = -1.0;
        if (has_bbox_a && has_bbox_b) {
            const double dx = 0.5 * (mn_a[0] + mx_a[0]) - 0.5 * (mn_b[0] + mx_b[0]);
            const double dy = 0.5 * (mn_a[1] + mx_a[1]) - 0.5 * (mn_b[1] + mx_b[1]);
            const double dz = 0.5 * (mn_a[2] + mx_a[2]) - 0.5 * (mn_b[2] + mx_b[2]);
            centroid_dist = std::sqrt(dx * dx + dy * dy + dz * dz);
        }
        const double denom = std::max(vol_a, vol_b);
        const double ratio = denom > 1e-12 ? std::min(vol_a, vol_b) / denom : 1.0;
        std::ostringstream oss;
        oss << "boolean comparison failed (" << why << "); degraded to volume/bbox agreement "
            << "(volume ratio " << std::fixed << std::setprecision(1) << ratio * 100.0 << "%)";
        return json{
            {"ok", true},
            {"degraded", true},
            {"match_pct", nullptr},
            {"volume_a_mm3", vol_a},
            {"volume_b_mm3", vol_b},
            {"volume_ratio", ratio},
            {"bbox_a", has_bbox_a ? bbox_json(mn_a, mx_a) : json(nullptr)},
            {"bbox_b", has_bbox_b ? bbox_json(mn_b, mx_b) : json(nullptr)},
            {"centroid_dist_mm", centroid_dist},
            {"summary", oss.str()},
        };
    };

    if (a.IsNull() || b.IsNull()) return degrade("null shape");

    double common_vol = 0.0, missing_vol = 0.0, extra_vol = 0.0;
    TopoDS_Shape missing_shape, extra_shape;
    try {
        BRepAlgoAPI_Common common_op(a, b);
        common_op.SetFuzzyValue(1e-4);
        common_op.Build();
        if (!common_op.IsDone()) return degrade("A^B common did not converge");
        common_vol = volume_or_zero(common_op.Shape());

        BRepAlgoAPI_Cut cut_ab(a, b);
        cut_ab.SetFuzzyValue(1e-4);
        cut_ab.Build();
        if (!cut_ab.IsDone()) return degrade("A-B cut did not converge");
        missing_shape = cut_ab.Shape();
        missing_vol = volume_or_zero(missing_shape);

        BRepAlgoAPI_Cut cut_ba(b, a);
        cut_ba.SetFuzzyValue(1e-4);
        cut_ba.Build();
        if (!cut_ba.IsDone()) return degrade("B-A cut did not converge");
        extra_shape = cut_ba.Shape();
        extra_vol = volume_or_zero(extra_shape);
    } catch (const Standard_Failure& ex) {
        return degrade(std::string("kernel exception: ") + ex.GetMessageString());
    }

    const double denom = std::max(vol_a, vol_b);
    const double match_pct = denom > 1e-12 ? 100.0 * common_vol / denom : 100.0;

    std::vector<json> regions;
    collect_residual_regions(missing_shape, "missing", regions);
    collect_residual_regions(extra_shape, "extra", regions);

    // Region counts + the largest-per-side facts feed the summary line; compute them over the
    // FULL exploded set before capping the returned array to 10.
    std::size_t n_missing = 0, n_extra = 0;
    double big_missing_vol = 0.0, big_extra_vol = 0.0;
    std::array<double, 3> big_missing_c{0, 0, 0}, big_extra_c{0, 0, 0};
    for (const json& r : regions) {
        const double v = r["volume_mm3"].get<double>();
        const bool is_missing = r["side"] == "missing";
        if (is_missing) {
            ++n_missing;
            if (v > big_missing_vol) {
                big_missing_vol = v;
                const json& c = r["center"];
                big_missing_c = {c[0].get<double>(), c[1].get<double>(), c[2].get<double>()};
            }
        } else {
            ++n_extra;
            if (v > big_extra_vol) {
                big_extra_vol = v;
                const json& c = r["center"];
                big_extra_c = {c[0].get<double>(), c[1].get<double>(), c[2].get<double>()};
            }
        }
    }

    std::sort(regions.begin(), regions.end(), [](const json& x, const json& y) {
        return x["volume_mm3"].get<double>() > y["volume_mm3"].get<double>();
    });
    if (regions.size() > 10) regions.resize(10);

    std::ostringstream oss;
    oss << std::fixed << std::setprecision(1) << match_pct << "% match";
    oss << std::setprecision(0);
    if (n_missing > 0) {
        oss << "; " << n_missing << " missing region(s) (largest " << big_missing_vol
            << " mm3 near [" << big_missing_c[0] << "," << big_missing_c[1] << ","
            << big_missing_c[2] << "])";
    } else {
        oss << "; 0 missing";
    }
    if (n_extra > 0) {
        oss << "; " << n_extra << " extra region(s) (largest " << big_extra_vol << " mm3 near ["
            << big_extra_c[0] << "," << big_extra_c[1] << "," << big_extra_c[2] << "])";
    } else {
        oss << "; 0 extra";
    }

    json out_regions = json::array();
    for (json& r : regions) out_regions.push_back(std::move(r));

    return json{
        {"ok", true},
        {"degraded", false},
        {"volume_a_mm3", vol_a},
        {"volume_b_mm3", vol_b},
        {"common_mm3", common_vol},
        {"missing_mm3", missing_vol},
        {"extra_mm3", extra_vol},
        {"match_pct", match_pct},
        {"residual_regions", out_regions},
        {"summary", oss.str()},
    };
}

// ------------------------------------------------------------- C2 helpers

namespace {

double norm_deg(double deg) {
    double d = std::fmod(deg, 360.0);
    if (d < 0) d += 360.0;
    return d;
}

std::array<double, 2> project2(const chudware::geom::SketchPlane& plane, const gp_Pnt& p) {
    const Eigen::Vector2d uv = plane.project(Eigen::Vector3d(p.X(), p.Y(), p.Z()));
    return {uv.x(), uv.y()};
}

std::array<double, 2> project_dir2(const chudware::geom::SketchPlane& plane, const gp_Dir& d) {
    const Eigen::Vector2d uv = plane.projectDir(Eigen::Vector3d(d.X(), d.Y(), d.Z()));
    return {uv.x(), uv.y()};
}

// session.cpp::plane_frame — 16-float row-major matrix mapping sketch (u,v,0,1) -> world.
// Duplicated here (rather than shared) to keep this file Session/session.cpp-free; the
// convention MUST stay byte-identical to session.cpp's plane_frame().
std::vector<double> plane_frame_of(const chudware::geom::SketchPlane& p) {
    std::vector<double> out;
    out.reserve(16);
    for (int i = 0; i < 3; ++i) {
        out.push_back(p.uAxis[i]);
        out.push_back(p.vAxis[i]);
        out.push_back(p.normal[i]);
        out.push_back(p.origin[i]);
    }
    out.insert(out.end(), {0.0, 0.0, 0.0, 1.0});
    return out;
}

// True iff `wire` is exactly one topologically-closed edge (its two vertices coincide) — the
// shape a full circle/ellipse hole boundary takes. `out_edge` receives that sole edge.
bool wire_is_single_closed_edge(const TopoDS_Wire& wire, TopoDS_Edge& out_edge) {
    TopExp_Explorer ex(wire, TopAbs_EDGE);
    if (!ex.More()) return false;
    out_edge = TopoDS::Edge(ex.Current());
    ex.Next();
    if (ex.More()) return false;  // more than one edge in this wire
    TopoDS_Vertex v1, v2;
    TopExp::Vertices(out_edge, v1, v2, Standard_True);
    return !v1.IsNull() && !v2.IsNull() && v1.IsSame(v2);
}

// Sample a curve into a 24-segment "line" polyline from `p_start` to `p_end` (both already
// resolved endpoints — used verbatim as the first/last sample so the polyline meets its
// topological neighbours exactly). `forward` selects whether increasing curve parameter walks
// start->end (false samples end->start, i.e. the edge is reversed relative to the curve's own
// parametrization). Appends "line" entities to `entities`; returns their ids in order.
std::vector<std::string> sample_curve_polyline(json& entities, BRepAdaptor_Curve& ca,
                                               const chudware::geom::SketchPlane& plane,
                                               int& next_id, const gp_Pnt& p_start,
                                               const gp_Pnt& p_end, bool forward) {
    constexpr int kSegments = 24;
    const double t0 = ca.FirstParameter(), t1 = ca.LastParameter();
    std::vector<std::string> ids;
    ids.reserve(kSegments);
    std::array<double, 2> prev2 = project2(plane, p_start);
    for (int i = 1; i <= kSegments; ++i) {
        const double f = static_cast<double>(i) / kSegments;
        gp_Pnt p;
        if (i == kSegments) {
            p = p_end;
        } else {
            const double t = forward ? (t0 + f * (t1 - t0)) : (t1 - f * (t1 - t0));
            p = ca.Value(t);
        }
        const std::array<double, 2> cur2 = project2(plane, p);
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "line"}, {"construction", false},
                                 {"p1", {prev2[0], prev2[1]}}, {"p2", {cur2[0], cur2[1]}}});
        ids.push_back(id);
        prev2 = cur2;
    }
    return ids;
}

// One TRIMMED edge inside a multi-edge wire -> 1 entity (line/arc/ellipse_arc) or a sampled
// polyline for anything else. `entity_ids` is appended to `entities`; returns the id(s).
std::vector<std::string> emit_edge_entities(json& entities, const TopoDS_Edge& edge,
                                            const chudware::geom::SketchPlane& plane, int& next_id,
                                            bool& any_approx) {
    BRepAdaptor_Curve ca(edge);
    TopoDS_Vertex v1, v2;
    TopExp::Vertices(edge, v1, v2, Standard_True);  // respects edge Orientation
    const gp_Pnt p1_3d = !v1.IsNull() ? BRep_Tool::Pnt(v1) : ca.Value(ca.FirstParameter());
    const gp_Pnt p2_3d = !v2.IsNull() ? BRep_Tool::Pnt(v2) : ca.Value(ca.LastParameter());
    const GeomAbs_CurveType ct = ca.GetType();

    if (ct == GeomAbs_Line) {
        const std::array<double, 2> a2 = project2(plane, p1_3d);
        const std::array<double, 2> b2 = project2(plane, p2_3d);
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "line"}, {"construction", false},
                                 {"p1", {a2[0], a2[1]}}, {"p2", {b2[0], b2[1]}}});
        return {id};
    }
    if (ct == GeomAbs_Circle) {
        const gp_Circ circ = ca.Circle();
        const std::array<double, 2> c2 = project2(plane, circ.Location());
        const std::array<double, 2> a2 = project2(plane, p1_3d);
        const std::array<double, 2> b2 = project2(plane, p2_3d);
        const double ang1 = norm_deg(std::atan2(a2[1] - c2[1], a2[0] - c2[0]) * 180.0 / M_PI);
        const double ang2 = norm_deg(std::atan2(b2[1] - c2[1], b2[0] - c2[0]) * 180.0 / M_PI);
        // Disambiguate sweep direction with an actual traversal-midpoint sample, so `ccw` is
        // correct regardless of how the circle's own axis relates to the face normal.
        const gp_Pnt mid3d = ca.Value(0.5 * (ca.FirstParameter() + ca.LastParameter()));
        const std::array<double, 2> m2 = project2(plane, mid3d);
        const double ang_mid = norm_deg(std::atan2(m2[1] - c2[1], m2[0] - c2[0]) * 180.0 / M_PI);
        const double to_mid = norm_deg(ang_mid - ang1);
        const double to_end = norm_deg(ang2 - ang1);
        const bool ccw = to_end < 1e-9 ? true : (to_mid <= to_end);
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "arc"}, {"construction", false},
                                 {"center", {c2[0], c2[1]}}, {"radius", circ.Radius()},
                                 {"start", ang1}, {"end", ang2}, {"ccw", ccw}});
        return {id};
    }
    if (ct == GeomAbs_Ellipse) {
        const gp_Elips el = ca.Ellipse();
        const std::array<double, 2> c2 = project2(plane, el.Location());
        const std::array<double, 2> x2 = project_dir2(plane, el.XAxis().Direction());
        const double rot = norm_deg(std::atan2(x2[1], x2[0]) * 180.0 / M_PI);
        // a0/a1: the ellipse's OWN un-rotated-frame trim parameters (doc_schema.md §3.1) —
        // OCCT's ellipse parametrization already IS that frame, and FirstParameter <
        // LastParameter always holds for a valid trimmed edge, so no ccw flag is needed here.
        const double a0 = ca.FirstParameter() * 180.0 / M_PI;
        const double a1 = ca.LastParameter() * 180.0 / M_PI;
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "ellipse_arc"}, {"construction", false},
                                 {"center", {c2[0], c2[1]}}, {"rx", el.MajorRadius()},
                                 {"ry", el.MinorRadius()}, {"rot", rot}, {"a0", a0}, {"a1", a1}});
        return {id};
    }
    any_approx = true;
    const bool forward = p1_3d.Distance(ca.Value(ca.FirstParameter())) <=
                        p1_3d.Distance(ca.Value(ca.LastParameter()));
    return sample_curve_polyline(entities, ca, plane, next_id, p1_3d, p2_3d, forward);
}

// A whole wire that is a single closed edge (full circle/ellipse/spline hole boundary) -> 1
// "circle"/"ellipse" entity, or a closed sampled polyline for anything else.
std::vector<std::string> emit_full_loop_entities(json& entities, const TopoDS_Edge& edge,
                                                 const chudware::geom::SketchPlane& plane,
                                                 int& next_id, bool& any_approx) {
    BRepAdaptor_Curve ca(edge);
    const GeomAbs_CurveType ct = ca.GetType();
    if (ct == GeomAbs_Circle) {
        const gp_Circ circ = ca.Circle();
        const std::array<double, 2> c2 = project2(plane, circ.Location());
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "circle"}, {"construction", false},
                                 {"center", {c2[0], c2[1]}}, {"radius", circ.Radius()}});
        return {id};
    }
    if (ct == GeomAbs_Ellipse) {
        const gp_Elips el = ca.Ellipse();
        const std::array<double, 2> c2 = project2(plane, el.Location());
        const std::array<double, 2> x2 = project_dir2(plane, el.XAxis().Direction());
        const double rot = norm_deg(std::atan2(x2[1], x2[0]) * 180.0 / M_PI);
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "ellipse"}, {"construction", false},
                                 {"center", {c2[0], c2[1]}}, {"rx", el.MajorRadius()},
                                 {"ry", el.MinorRadius()}, {"rot", rot}});
        return {id};
    }
    any_approx = true;
    const gp_Pnt p0 = ca.Value(ca.FirstParameter());
    return sample_curve_polyline(entities, ca, plane, next_id, p0, p0, /*forward=*/true);
}

}  // namespace

json face_wires(const TopoDS_Face& face) {
    BRepAdaptor_Surface surf(face);
    if (surf.GetType() != GeomAbs_Plane)
        throw OpError("invalid_op", "convert entities requires a planar face",
                      {{"reason", "non-planar face"}});

    // Plane frame: area-centroid origin + orientation-aware normal — the SAME convention
    // resolve_sketch_plane's face-sketch branch uses (session.cpp), so a sketch built from
    // this wire set lands exactly where "sketch on this face" would.
    GProp_GProps props;
    BRepGProp::SurfaceProperties(face, props);
    const gp_Pnt centroid = props.CentreOfMass();
    gp_Dir normal_dir = surf.Plane().Axis().Direction();
    if (face.Orientation() == TopAbs_REVERSED) normal_dir.Reverse();

    const chudware::geom::SketchPlane plane = chudware::geom::SketchPlane::from(
        Eigen::Vector3d(centroid.X(), centroid.Y(), centroid.Z()),
        Eigen::Vector3d(normal_dir.X(), normal_dir.Y(), normal_dir.Z()));

    const TopoDS_Wire outer_wire = BRepTools::OuterWire(face);

    json entities = json::array();
    json wires = json::array();
    bool any_approx = false;
    int next_id = 1;

    for (TopExp_Explorer wex(face, TopAbs_WIRE); wex.More(); wex.Next()) {
        const TopoDS_Wire wire = TopoDS::Wire(wex.Current());
        json entity_ids = json::array();
        TopoDS_Edge full_edge;
        if (wire_is_single_closed_edge(wire, full_edge)) {
            for (const std::string& id :
                 emit_full_loop_entities(entities, full_edge, plane, next_id, any_approx))
                entity_ids.push_back(id);
        } else {
            for (BRepTools_WireExplorer wire_ex(wire, face); wire_ex.More(); wire_ex.Next()) {
                const TopoDS_Edge edge = TopoDS::Edge(wire_ex.Current());
                for (const std::string& id :
                     emit_edge_entities(entities, edge, plane, next_id, any_approx))
                    entity_ids.push_back(id);
            }
        }
        wires.push_back(json{{"outer", wire.IsSame(outer_wire)}, {"entity_ids", entity_ids}});
    }

    return json{
        {"ok", true},
        {"plane_frame", plane_frame_of(plane)},
        {"entities", entities},
        {"wires", wires},
        {"approx", any_approx},
    };
}

// ------------------------------------------------------------- C3: project external edges

namespace {

// Build the OCCT Geom_Plane matching a SketchPlane frame (origin + u/v/normal). Used as the
// projection target for GeomProjLib::ProjectOnPlane (orthographic drop along the sketch normal).
Handle(Geom_Plane) geom_plane_of(const chudware::geom::SketchPlane& p) {
    gp_Pnt o(p.origin.x(), p.origin.y(), p.origin.z());
    gp_Dir n(p.normal.x(), p.normal.y(), p.normal.z());
    gp_Dir u(p.uAxis.x(), p.uAxis.y(), p.uAxis.z());
    return new Geom_Plane(gp_Ax3(o, n, u));
}

// True iff the whole edge collapses to (near) a single point once projected onto `plane` — i.e.
// the edge is (near) perpendicular to the plane, so its orthographic image is a point, not a
// curve. Callers emit a "point" reference entity (or skip) rather than a zero-length curve.
bool projected_edge_is_point(const TopoDS_Edge& edge, const chudware::geom::SketchPlane& plane,
                             std::array<double, 2>& pt_out) {
    BRepAdaptor_Curve ca(edge);
    const double t0 = ca.FirstParameter(), t1 = ca.LastParameter();
    const std::array<double, 2> a = project2(plane, ca.Value(t0));
    double maxd = 0.0;
    for (int i = 1; i <= 8; ++i) {
        const std::array<double, 2> q =
            project2(plane, ca.Value(t0 + (t1 - t0) * (static_cast<double>(i) / 8.0)));
        maxd = std::max(maxd, std::hypot(q[0] - a[0], q[1] - a[1]));
    }
    pt_out = a;
    return maxd < 1e-7;
}

// Project ONE source 3D edge orthographically onto `plane` (along the plane normal), rebuild a
// TopoDS_Edge from the projected curve, and hand it to the existing in-plane emitters. Because
// the projected curve genuinely LIES in the plane, GeomAbs classification is correct BY
// CONSTRUCTION: a tilted circle projects to a Geom_Ellipse and comes back as an "ellipse"/
// "ellipse_arc"; a line stays a line; anything unclassifiable falls through to the sampled
// polyline path (approx). Closed source edges (full circles/ellipses) emit a single closed
// "circle"/"ellipse" entity via emit_full_loop_entities. Perpendicular edges collapse to a
// "point" reference entity. Appends to `entities`; returns the new entity ids.
std::vector<std::string> emit_projected_edge(json& entities, const TopoDS_Edge& src_edge,
                                             const chudware::geom::SketchPlane& plane, int& next_id,
                                             bool& any_approx) {
    // Perpendicular edge -> single reference point (nothing to project into a curve).
    std::array<double, 2> pt{};
    if (projected_edge_is_point(src_edge, plane, pt)) {
        const std::string id = "e" + std::to_string(next_id++);
        entities.push_back(json{{"id", id}, {"type", "point"}, {"construction", true},
                                 {"p", {pt[0], pt[1]}}});
        return {id};
    }

    Standard_Real f = 0.0, l = 0.0;
    Handle(Geom_Curve) c3d = BRep_Tool::Curve(src_edge, f, l);
    if (c3d.IsNull()) {
        // No 3D curve (degenerate/seam edge) — nothing to project.
        return {};
    }
    Handle(Geom_Plane) gpln = geom_plane_of(plane);
    gp_Dir ndir(plane.normal.x(), plane.normal.y(), plane.normal.z());
    Handle(Geom_Curve) proj;
    try {
        proj = GeomProjLib::ProjectOnPlane(new Geom_TrimmedCurve(c3d, f, l), gpln, ndir,
                                           /*KeepParametrization=*/Standard_True);
    } catch (const Standard_Failure&) {
        proj.Nullify();
    }
    if (proj.IsNull()) return {};

    // Rebuild an edge over the same [f,l] param window so the emitters see the exact trim.
    TopoDS_Edge pedge;
    try {
        pedge = BRepBuilderAPI_MakeEdge(proj, proj->FirstParameter(), proj->LastParameter());
    } catch (const Standard_Failure&) {
        return {};
    }
    if (pedge.IsNull()) return {};

    // A closed source edge (full circle/ellipse/closed spline) stays a single closed loop.
    if (BRep_Tool::IsClosed(pedge) || src_edge.Closed() ||
        proj->Value(proj->FirstParameter()).Distance(proj->Value(proj->LastParameter())) < 1e-7) {
        std::vector<std::string> ids = emit_full_loop_entities(entities, pedge, plane, next_id,
                                                               any_approx);
        for (const std::string& id : ids)
            for (json& e : entities)
                if (e.value("id", std::string()) == id) e["construction"] = true;
        return ids;
    }
    std::vector<std::string> ids = emit_edge_entities(entities, pedge, plane, next_id, any_approx);
    for (const std::string& id : ids)
        for (json& e : entities)
            if (e.value("id", std::string()) == id) e["construction"] = true;
    return ids;
}

}  // namespace

std::vector<ProjectedEntity> project_edges_to_plane(const std::vector<TopoDS_Edge>& edges,
                                                    const chudware::geom::SketchPlane& plane,
                                                    bool& any_approx) {
    json entities = json::array();
    int next_id = 1;
    any_approx = false;
    std::vector<ProjectedEntity> out;
    for (const TopoDS_Edge& e : edges) {
        const std::size_t before = entities.size();
        emit_projected_edge(entities, e, plane, next_id, any_approx);
        for (std::size_t k = before; k < entities.size(); ++k) {
            json ent = entities[k];
            ent.erase("id");  // caller re-assigns from the sketch's own id generator on insert
            out.push_back(ProjectedEntity{std::move(ent)});
        }
    }
    return out;
}

std::vector<TopoDS_Edge> face_boundary_edges(const TopoDS_Face& face) {
    std::vector<TopoDS_Edge> edges;
    for (TopExp_Explorer ex(face, TopAbs_EDGE); ex.More(); ex.Next())
        edges.push_back(TopoDS::Edge(ex.Current()));
    return edges;
}

}  // namespace chudware::doc
